"""The ``.obfus`` container (https://github.com/themrsung/file-obfuscation).

A port of ``obfus_core.py`` (CC0), byte-compatible with it::

    blob = XOR(MAGIC || plaintext, keystream(seed))     seed: 4 bits, never stored
    keystream(seed) = sha256(seed_byte || u32le(0)) || sha256(seed_byte || u32le(1)) || …

This is obfuscation, **not encryption**: anyone can recover the plaintext
by trying all 16 seeds, which is exactly what :func:`unpack` does. Nothing
here keeps a file secret.

Differences from the reference, behaviour-preserving:

* the seed is found by checking only the 6 header bytes under each of the
  16 keystreams (the reference XORs the whole blob up to 16 times), and
* the XOR is done on big integers instead of byte by byte.

Both return exactly what the reference returns, including which seed wins
if two ever matched (the lowest).
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

MAGIC = b"\x93\x51obf1"
KEYBITS = 4
KEYSPACE = 1 << KEYBITS
EXT = ".obfus"


def keystream(seed: int, n: int) -> bytes:
    out = bytearray()
    ctr = 0
    sb = bytes([seed])
    sha = hashlib.sha256
    while len(out) < n:
        out += sha(sb + ctr.to_bytes(4, "little")).digest()
        ctr += 1
    return bytes(out[:n])


def _xor(data: bytes, seed: int) -> bytes:
    n = len(data)
    if n == 0:
        return b""
    ks = keystream(seed, n)
    return (int.from_bytes(data, "little") ^ int.from_bytes(ks, "little")).to_bytes(n, "little")


#: XOR of MAGIC with each seed's first keystream bytes: a blob made with
#: seed s starts with _HEADS[s].
_HEADS = tuple(bytes(a ^ b for a, b in zip(MAGIC, keystream(s, len(MAGIC)))) for s in range(KEYSPACE))


def find_seed(head: bytes) -> Optional[int]:
    """The seed whose header matches the first 6 bytes of a blob, or None."""
    head = head[: len(MAGIC)]
    if len(head) < len(MAGIC):
        return None
    for s, h in enumerate(_HEADS):
        if head == h:
            return s
    return None


def is_obfus(head: bytes) -> bool:
    return find_seed(head) is not None


def unpack(blob: bytes) -> bytes:
    """bytes → bytes; raises ``ValueError("no key matched")`` like the reference."""
    seed = find_seed(blob)
    if seed is None:
        raise ValueError("no key matched")
    return _xor(blob, seed)[len(MAGIC):]


def unpack_with_seed(blob: bytes) -> tuple[bytes, int]:
    seed = find_seed(blob)
    if seed is None:
        raise ValueError("no key matched")
    return _xor(blob, seed)[len(MAGIC):], seed


def pack(plaintext: bytes, seed: Optional[int] = None) -> bytes:
    """bytes → bytes, 6 bytes longer. A random seed unless one is given (tests)."""
    if seed is None:
        seed = os.urandom(1)[0] & (KEYSPACE - 1)
    if not 0 <= seed < KEYSPACE:
        raise ValueError("seed must be 0..15")
    return _xor(MAGIC + plaintext, seed)


def inner_name(name: str) -> str:
    """``report.pdf.obfus`` → ``report.pdf``; other names are kept."""
    return name[: -len(EXT)] if name.endswith(EXT) and len(name) > len(EXT) else name
