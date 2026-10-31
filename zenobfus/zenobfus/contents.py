"""What is inside an unpacked blob: type sniffing, previews, archive trees."""

from __future__ import annotations

import io
import posixpath
import struct
import tarfile
import zipfile
from dataclasses import dataclass
from typing import Optional

from . import obfus
from .browserpanel import Entry, decode_text, hex_preview, human_size, text_preview

IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "svg", "ico"}
AUDIO_EXT = {"mp3", "aac", "m4a", "ogg", "opus", "flac", "wav", "aiff", "aif"}
VIDEO_EXT = {"mp4", "m4v", "mov", "mkv", "webm", "avi"}


@dataclass
class Kind:
    #: text, image, audio, video, pdf, zip, tar, obfus, binary
    family: str
    label: str

    @property
    def is_archive(self) -> bool:
        return self.family in ("zip", "tar")

    @property
    def openable(self) -> bool:
        return self.is_archive or self.family == "obfus"


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[1].lower() if "." in name.lstrip(".") else ""


def sniff(name: str, data: bytes) -> Kind:
    """Classify ``data`` (named ``name``) by magic bytes, then extension."""
    h = data[:16]
    if obfus.is_obfus(data):
        return Kind("obfus", "obfus blob")
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return Kind("image", "PNG image")
    if h.startswith(b"\xff\xd8\xff"):
        return Kind("image", "JPEG image")
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return Kind("image", "GIF image")
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return Kind("image", "WebP image")
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return Kind("audio", "WAV audio")
    if h[:4] == b"RIFF" and h[8:12] == b"AVI ":
        return Kind("video", "AVI video")
    if h[:2] == b"BM":
        if _ext(name) == "bmp":
            return Kind("image", "BMP image")
    if h[:4] in (b"II*\0", b"MM\0*"):
        return Kind("image", "TIFF image")
    if h.startswith(b"%PDF"):
        return Kind("pdf", "PDF document")
    if h[:4] == b"PK\x03\x04" or h[:4] == b"PK\x05\x06":
        return Kind("zip", "zip archive")
    if h[:2] == b"\x1f\x8b":
        return Kind("tar", "gzip archive") if _is_tar(data) else Kind("binary", "gzip data")
    if h[:3] == b"BZh":
        return Kind("tar", "bzip2 archive") if _is_tar(data) else Kind("binary", "bzip2 data")
    if h[:6] == b"\xfd7zXZ\0":
        return Kind("tar", "xz archive") if _is_tar(data) else Kind("binary", "xz data")
    if len(data) > 262 and data[257:262] == b"ustar":
        return Kind("tar", "tar archive")
    if h[:4] == b"fLaC":
        return Kind("audio", "FLAC audio")
    if h[:4] == b"OggS":
        return Kind("audio" if _ext(name) not in VIDEO_EXT else "video", "Ogg media")
    if h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xE0 == 0xE0 and _ext(name) in ("mp3", "aac")):
        return Kind("audio", "MP3/AAC audio")
    if h[4:8] == b"ftyp":
        brand = h[8:12].decode("latin-1")
        fam = "audio" if brand.startswith("M4A") or _ext(name) == "m4a" else "video"
        return Kind(fam, f"MPEG-4 media ({brand.strip()})")
    if h[:4] == b"\x1aE\xdf\xa3":
        return Kind("video", "Matroska/WebM video")
    if h[:4] == b"FORM" and h[8:12] in (b"AIFF", b"AIFC"):
        return Kind("audio", "AIFF audio")
    text = decode_text(data[:65536])
    if text is not None:
        e = _ext(name)
        if text.lstrip().startswith("-----BEGIN "):
            return Kind("text", "PEM text")
        return Kind("text", f"{e.upper()} text" if e else "text")
    e = _ext(name)
    if e in IMAGE_EXT:
        return Kind("image", f"{e.upper()} image")
    if e in AUDIO_EXT:
        return Kind("audio", f"{e.upper()} audio")
    if e in VIDEO_EXT:
        return Kind("video", f"{e.upper()} video")
    return Kind("binary", "binary data")


def _is_tar(data: bytes) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
            return t.next() is not None
    except (tarfile.TarError, OSError, EOFError):
        return False


def image_size(data: bytes) -> Optional[tuple[int, int]]:
    try:
        if data.startswith(b"\x89PNG") and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return w, abs(h)
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    except struct.error:
        return None
    return None


# ---------------------------------------------------------------------------
# archives as virtual directories


class Tree:
    """A read-only directory tree over a zip or tar held in memory."""

    def __init__(self, label: str) -> None:
        self.label = label
        #: dir path ("" is the root) -> {name: (is_dir, size, member)}
        self.dirs: dict[str, dict[str, tuple[bool, Optional[int], Optional[str]]]] = {"": {}}

    def _add_dir(self, path: str) -> None:
        path = path.strip("/")
        if path in self.dirs:
            return
        parent, _, name = path.rpartition("/")
        self._add_dir(parent)
        self.dirs[parent][name] = (True, None, None)
        self.dirs[path] = {}

    def _add_file(self, path: str, size: int, member: str) -> None:
        path = path.strip("/")
        parent, _, name = path.rpartition("/")
        if not name:
            return
        self._add_dir(parent)
        self.dirs[parent][name] = (False, size, member)

    def entries(self, path: str) -> list[Entry]:
        d = self.dirs.get(path.strip("/"), {})
        out = [Entry(n, is_dir, size, None, data=member) for n, (is_dir, size, member) in d.items()]
        return sorted(out, key=lambda e: (not e.is_dir, e.name.lower(), e.name))

    def read(self, member: str) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    @staticmethod
    def clean(name: str) -> Optional[str]:
        """Normalize a member name; None for names that escape the root."""
        n = posixpath.normpath("/" + name.replace("\\", "/")).lstrip("/")
        return n or None


class ZipTree(Tree):
    def __init__(self, label: str, data: bytes) -> None:
        super().__init__(label)
        self.zf = zipfile.ZipFile(io.BytesIO(data))
        for info in self.zf.infolist():
            n = self.clean(info.filename)
            if n is None:
                continue
            if info.is_dir():
                self._add_dir(n)
            else:
                self._add_file(n, info.file_size, info.filename)

    def read(self, member: str) -> bytes:
        return self.zf.read(member)


class TarTree(Tree):
    def __init__(self, label: str, data: bytes) -> None:
        super().__init__(label)
        self.tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        for m in self.tf.getmembers():
            n = self.clean(m.name)
            if n is None:
                continue
            if m.isdir():
                self._add_dir(n)
            elif m.isfile():
                self._add_file(n, m.size, m.name)

    def read(self, member: str) -> bytes:
        f = self.tf.extractfile(member)
        return f.read() if f is not None else b""


def open_tree(label: str, kind: Kind, data: bytes) -> Tree:
    if kind.family == "zip":
        return ZipTree(label, data)
    if kind.family == "tar":
        return TarTree(label, data)
    raise ValueError(f"{label} is not an archive")


# ---------------------------------------------------------------------------
# preview text


def describe(name: str, data: bytes, kind: Kind) -> str:
    """The body of the preview for unpacked contents."""
    size = human_size(len(data))
    if kind.family == "text":
        return f"{kind.label}, {size}\n{'─' * 24}\n{text_preview(decode_text(data) or '', len(data))}"
    if kind.family == "image":
        dims = image_size(data)
        d = f", {dims[0]}×{dims[1]}" if dims else ""
        return f"{kind.label}{d}, {size}\nQuick Look shows the picture."
    if kind.family in ("audio", "video"):
        return f"{kind.label}, {size}\nQuick Look plays it."
    if kind.family == "pdf":
        return f"{kind.label}, {size}\nQuick Look shows the pages."
    if kind.is_archive:
        try:
            tree = open_tree(name, kind, data)
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as e:
            return f"{kind.label}, {size} — can't be read: {e}"
        files = sum(1 for d in tree.dirs.values() for v in d.values() if not v[0])
        top = tree.entries("")
        lines = [("▸ " + e.name + "/") if e.is_dir else ("· " + e.name + "  " + human_size(e.size)) for e in top[:20]]
        if len(top) > 20:
            lines.append(f"… and {len(top) - 20} more")
        return f"{kind.label}, {size}, {files} file(s)\nOpen browses it.\n{'─' * 24}\n" + "\n".join(lines)
    if kind.family == "obfus":
        return f"Another obfus layer inside ({size}). Open unpacks it again."
    return f"{kind.label}, {size}\n{'─' * 24}\n{hex_preview(data)}"
