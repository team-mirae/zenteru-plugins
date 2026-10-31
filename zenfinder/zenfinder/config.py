"""zenfinder settings: ``~/Library/Application Support/zenteru/plugin-data/zenfinder/config.json``."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass

EXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,15}\Z")


def data_dir() -> str:
    base = os.environ.get("ZENFINDER_DATA_DIR")
    if base:
        return base
    return os.path.expanduser("~/Library/Application Support/zenteru/plugin-data/zenfinder")


@dataclass
class Config:
    #: Extension for clipboard text saved with "Paste text" (no dot).
    paste_ext: str = "txt"
    show_hidden: bool = False
    #: Where files downloaded from ssh hosts go.
    download_dir: str = "~/Downloads"

    @classmethod
    def load(cls) -> "Config":
        path = os.path.join(data_dir(), "config.json")
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return cls()
        c = cls()
        if isinstance(raw, dict):
            ext = raw.get("paste_ext")
            if isinstance(ext, str):
                try:
                    c.paste_ext = normalize_ext(ext)
                except ValueError:
                    pass
            if isinstance(raw.get("show_hidden"), bool):
                c.show_hidden = raw["show_hidden"]
            if isinstance(raw.get("download_dir"), str) and raw["download_dir"].strip():
                c.download_dir = raw["download_dir"]
        return c

    def save(self) -> None:
        d = data_dir()
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".config-", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
            f.write("\n")
        os.replace(tmp, os.path.join(d, "config.json"))

    @property
    def download_path(self) -> str:
        return os.path.expanduser(self.download_dir)


def normalize_ext(ext: str) -> str:
    """``".MD "`` → ``"MD"``; raise ``ValueError`` if it isn't a plain extension."""
    e = ext.strip().lstrip(".")
    if not EXT_RE.match(e):
        raise ValueError(f"{ext.strip()!r} is not a file extension (letters, digits, . _ + -; up to 16)")
    return e


def paste_name(name: str, ext: str) -> str:
    """The file name for pasted text: ``ext`` is added when ``name`` has none."""
    name = name.strip()
    stem = name.lstrip(".")
    if "." not in stem:
        return f"{name}.{ext}"
    return name
