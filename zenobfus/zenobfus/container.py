"""obfus-react's chunked container with CRC and Reed–Solomon ECC.

A port of ``src/engine/container.ts`` from https://github.com/themrsung/obfus-react
(format version 1)::

    container header   32 B   "OBFSCTR1", version, flags, RS params, chunk size, chunk count, original size
    [header parity]    12 B   RS over the header with the fixed RS(255,243), when ECC is on
    chunk record × N
      record header     8 B   u32le storedLen, u32le crc32(blob)
      blob                    obfus pack(chunk), its own random seed
      [parity]                RS parity over (record header || blob), when ECC is on

A file that is neither chunked nor ECC-protected is a bare blob (the
upstream format) with no container around it.

Reading matches obfus-react's ``unpackSource``, with one difference: a
chunk whose CRC already matches is not run through the RS decoder. The
output is the same (an intact block has zero syndromes, so RS would change
nothing), and pure Python is spared the decoder on clean data. Damaged
chunks, and a header whose magic was damaged, are repaired from parity
exactly as obfus-react does. A chunk that can't be repaired is filled with
zeros, so the file keeps its length and offsets, and the chunk is reported.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional

from . import obfus
from .rs import RS_K, RS_NSYM, ecc_encode, ecc_repair, parity_len

CONTAINER_MAGIC = b"OBFSCTR1"
HEADER_LEN = 32
RECORD_HEADER_LEN = 8
CONTAINER_VERSION = 1
FLAG_ECC = 1
#: The header's own parity always uses the fixed code, never the header's fields.
HEADER_PARITY_LEN = parity_len(HEADER_LEN)
#: How much of a file to read to classify it.
PROBE_LEN = HEADER_LEN + HEADER_PARITY_LEN


class ContainerError(ValueError):
    pass


@dataclass
class Header:
    version: int
    ecc: bool
    rs_k: int
    rs_nsym: int
    chunk_size: int
    chunk_count: int
    original_size: int

    def pack(self) -> bytes:
        return (
            CONTAINER_MAGIC
            + struct.pack(
                "<BBHHIIQH",
                self.version,
                FLAG_ECC if self.ecc else 0,
                self.rs_nsym if self.ecc else 0,
                self.rs_k if self.ecc else 0,
                self.chunk_size,
                self.chunk_count,
                self.original_size,
                0,
            )
        )

    @classmethod
    def parse(cls, b: bytes) -> "Header":
        version, flags, nsym, k, chunk_size, count, size, _ = struct.unpack_from("<BBHHIIQH", b, 8)
        if version != CONTAINER_VERSION:
            raise ContainerError(f"unsupported container version {version}")
        ecc = bool(flags & FLAG_ECC)
        return cls(version, ecc, k if ecc else 0, nsym if ecc else 0, chunk_size, count, size)


@dataclass
class Unpacked:
    data: bytes
    container: bool
    ecc: bool = False
    chunk_count: int = 1
    chunk_size: int = 0
    #: Bytes corrected by ECC, header included.
    repaired: int = 0
    header_repaired: bool = False
    #: Chunks that failed their CRC even after repair (zero-filled in ``data``).
    damaged: list[int] = field(default_factory=list)
    #: The bare blob's seed (bare blobs only).
    seed: Optional[int] = None

    @property
    def intact(self) -> bool:
        return not self.damaged


def is_container(head: bytes) -> bool:
    return len(head) >= HEADER_LEN and head[: len(CONTAINER_MAGIC)] == CONTAINER_MAGIC


def repair_header(head: bytes, parity: bytes) -> Optional[tuple[bytes, int]]:
    """The header repaired from its parity, if that yields the magic."""
    if len(head) < HEADER_LEN or len(parity) < HEADER_PARITY_LEN:
        return None
    copy = bytearray(head[:HEADER_LEN])
    corrected, failed = ecc_repair(copy, parity[:HEADER_PARITY_LEN])
    if failed or not is_container(bytes(copy)):
        return None
    return bytes(copy), corrected


def detect(probe: bytes) -> Optional[str]:
    """``"container"``, ``"bare"`` or None, in obfus-react's order: exact
    magic, then a bare blob, then a header that needs its parity."""
    if is_container(probe):
        return "container"
    if obfus.is_obfus(probe):
        return "bare"
    if repair_header(probe[:HEADER_LEN], probe[HEADER_LEN:]) is not None:
        return "container"
    return None


def describe_probe(probe: bytes) -> Optional[str]:
    """Short tag for a listing: ``obfus``, ``obfus ecc``, ``obfus chunked``."""
    kind = detect(probe)
    if kind == "bare":
        return "obfus"
    if kind == "container":
        fixed = repair_header(probe[:HEADER_LEN], probe[HEADER_LEN:])
        head = fixed[0] if fixed is not None else probe[:HEADER_LEN]
        try:
            h = Header.parse(head)
        except (ContainerError, struct.error):
            return "obfus container?"
        return "obfus ecc" if h.ecc else "obfus chunked"
    return None


def _check_header(h: Header, total: int, max_bytes: Optional[int]) -> None:
    if h.chunk_size <= 0 or h.chunk_count <= 0:
        raise ContainerError("container header has no chunks")
    if h.chunk_count != max(1, -(-h.original_size // h.chunk_size)):
        raise ContainerError("container header is inconsistent (size, chunk size and count disagree)")
    if h.ecc and not (1 <= h.rs_nsym and 1 <= h.rs_k and h.rs_k + h.rs_nsym <= 255):
        raise ContainerError(f"container header has an invalid RS code ({h.rs_k}, {h.rs_nsym})")
    if max_bytes is not None and h.original_size > max_bytes:
        raise ContainerError(f"contents are {h.original_size} bytes, more than the {max_bytes} allowed")


def unpack(data: bytes, max_bytes: Optional[int] = None) -> Unpacked:
    """Unpack a bare blob or a container. Raises ``ValueError`` if it is neither."""
    probe = data[:PROBE_LEN]
    head: Optional[bytes] = None
    header_fixed = 0
    header_repaired = False
    if is_container(probe) or not obfus.is_obfus(probe):
        # Repair the header from its parity when that works and describes an
        # ECC container (only those carry header parity). With the magic
        # intact this also catches damaged fields, which obfus-react doesn't.
        fixed = repair_header(probe[:HEADER_LEN], probe[HEADER_LEN:])
        if fixed is not None and _says_ecc(fixed[0]):
            head, header_fixed = fixed
            header_repaired = header_fixed > 0
        elif is_container(probe):
            head = probe[:HEADER_LEN]

    if head is None:
        plain, seed = obfus.unpack_with_seed(data)  # ValueError("no key matched")
        return Unpacked(plain, container=False, seed=seed)

    h = Header.parse(head)
    _check_header(h, len(data), max_bytes)
    off = HEADER_LEN + (HEADER_PARITY_LEN if h.ecc else 0)
    out = bytearray()
    repaired = header_fixed
    damaged: list[int] = []
    for i in range(h.chunk_count):
        plain_len = min(h.chunk_size, h.original_size - i * h.chunk_size)
        if plain_len < 0:
            raise ContainerError("container header is inconsistent")
        # A damaged length must not drive the reader: every chunk's length
        # follows from the header.
        stored = plain_len + obfus.MAGIC_LEN
        data_len = RECORD_HEADER_LEN + stored
        plen = parity_len(data_len, h.rs_k, h.rs_nsym) if h.ecc else 0
        record = bytearray(data[off:off + data_len])
        ok = False
        if len(record) == data_len:
            ok = _record_ok(record)
            if not ok and h.ecc:
                parity = data[off + data_len:off + data_len + plen]
                n, _failed = ecc_repair(record, parity, h.rs_k, h.rs_nsym)
                repaired += n
                ok = _record_ok(record)
        if ok:
            out += obfus.unpack(bytes(record[RECORD_HEADER_LEN:]))
        else:
            damaged.append(i)
            out += bytes(plain_len)
        off += data_len + plen
    return Unpacked(bytes(out), container=True, ecc=h.ecc, chunk_count=h.chunk_count, chunk_size=h.chunk_size,
                    repaired=repaired, header_repaired=header_repaired, damaged=damaged)


def _says_ecc(head: bytes) -> bool:
    try:
        return Header.parse(head).ecc
    except (ContainerError, struct.error):
        return False


def _record_ok(record: bytearray) -> bool:
    blob = bytes(record[RECORD_HEADER_LEN:])
    want = struct.unpack_from("<I", record, 4)[0]
    return zlib.crc32(blob) & 0xFFFFFFFF == want and obfus.is_obfus(blob)


def pack(data: bytes, chunk_size: int = 1 << 20, ecc: bool = True) -> bytes:
    """Write a container the way obfus-react's ``packSource`` does.

    Like obfus-react, a single chunk without ECC is written as a bare blob.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    total = len(data)
    count = max(1, -(-total // chunk_size))
    if not ecc and count == 1:
        return obfus.pack(data)
    header = Header(CONTAINER_VERSION, ecc, RS_K, RS_NSYM, chunk_size, count, total).pack()
    parts = [header]
    if ecc:
        parts.append(ecc_encode(header))
    for i in range(count):
        chunk = data[i * chunk_size:(i + 1) * chunk_size]
        blob = obfus.pack(chunk)
        record = struct.pack("<II", len(blob), zlib.crc32(blob) & 0xFFFFFFFF) + blob
        parts.append(record)
        if ecc:
            parts.append(ecc_encode(record))
    return b"".join(parts)


def packed_size(size: int, chunk_size: int = 1 << 20, ecc: bool = True) -> int:
    count = max(1, -(-size // chunk_size))
    if not ecc and count == 1:
        return size + obfus.MAGIC_LEN
    total = HEADER_LEN + (HEADER_PARITY_LEN if ecc else 0)
    for i in range(count):
        dl = RECORD_HEADER_LEN + min(chunk_size, size - i * chunk_size) + obfus.MAGIC_LEN
        total += dl + (parity_len(dl) if ecc else 0)
    return total
