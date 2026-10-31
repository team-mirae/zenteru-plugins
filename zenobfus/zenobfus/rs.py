"""Reed–Solomon over GF(2^8), a port of obfus-react's ``src/engine/rs.ts``.

Systematic encoding; Berlekamp–Massey + Chien + Forney decoding. Corrects
up to ``nsym // 2`` bad bytes per block at unknown positions. obfus-react
uses RS(255, 243): 12 parity bytes per 243 data bytes.

Same conventions as the TypeScript, which interop depends on:

* GF(256), primitive polynomial 0x11d, generator 2.
* A block is ``data || parity``. As a polynomial its first byte is the
  highest-degree term.
* Syndromes are S_i = C(a^i) for i in [0, nsym); the first root is a^0.
"""

from __future__ import annotations

from functools import lru_cache

EXP = [0] * 512
LOG = [0] * 256


def _init() -> None:
    x = 1
    for i in range(255):
        EXP[i] = x
        LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        EXP[i] = EXP[i - 255]


_init()


class RsUncorrectable(ValueError):
    """More errors than the code can correct."""


def mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("rs: divide by zero")
    return 0 if a == 0 else EXP[LOG[a] + 255 - LOG[b]]


def _exp_neg(i: int) -> int:
    return EXP[(255 - (i % 255)) % 255]


def _eval_le(p: list[int], x: int) -> int:
    y = 0
    for c in reversed(p):
        y = mul(y, x) ^ c
    return y


def _poly_mul(a: list[int], b: list[int]) -> list[int]:
    out = [0] * (len(a) + len(b) - 1)
    for i, ai in enumerate(a):
        if ai == 0:
            continue
        la = LOG[ai]
        for j, bj in enumerate(b):
            if bj:
                out[i + j] ^= EXP[la + LOG[bj]]
    return out


@lru_cache(maxsize=None)
def generator(nsym: int) -> tuple[int, ...]:
    """prod(x - a^i) for i in [0, nsym), highest degree first."""
    g = [1]
    for i in range(nsym):
        g = _poly_mul(g, [1, EXP[i]])
    return tuple(g)


@lru_cache(maxsize=None)
def _rows(nsym: int) -> tuple[int, ...]:
    """For each coefficient c: c·g[1..nsym] packed big-endian into an int."""
    g = generator(nsym)
    rows = []
    for c in range(256):
        v = 0
        for j in range(1, len(g)):
            v = (v << 8) | mul(c, g[j])
        rows.append(v)
    return tuple(rows)


def encode(data: bytes, nsym: int) -> bytes:
    """``nsym`` parity bytes for one block of data (synthetic division)."""
    rows = _rows(nsym)
    shift = 8 * (nsym - 1)
    mask = (1 << (8 * nsym)) - 1
    p = 0
    for d in data:
        coef = d ^ (p >> shift)
        p = ((p << 8) & mask) ^ rows[coef]
    return p.to_bytes(nsym, "big")


def syndromes(block: bytes, nsym: int) -> list[int]:
    s = []
    for i in range(nsym):
        li = i  # LOG of a^i
        y = 0
        for c in block:
            y = (EXP[LOG[y] + li] if y else 0) ^ c
        s.append(y)
    return s


def _berlekamp_massey(S: list[int], nsym: int) -> list[int]:
    lam = [1]
    B = [1]
    L = 0
    m = 1
    b = 1
    for n in range(nsym):
        d = S[n]
        for i in range(1, L + 1):
            d ^= mul(lam[i], S[n - i])
        if d == 0:
            m += 1
        elif 2 * L <= n:
            T = list(lam)
            scale = div(d, b)
            while len(lam) < m + len(B):
                lam.append(0)
            for j, bj in enumerate(B):
                lam[m + j] ^= mul(scale, bj)
            L = n + 1 - L
            B = T
            b = d
            m = 1
        else:
            scale = div(d, b)
            while len(lam) < m + len(B):
                lam.append(0)
            for j, bj in enumerate(B):
                lam[m + j] ^= mul(scale, bj)
            m += 1
    while len(lam) > 1 and lam[-1] == 0:
        lam.pop()
    return lam


def decode_block(block: bytearray, nsym: int) -> int:
    """Verify and repair one ``data || parity`` block in place.

    Returns the number of bytes corrected; raises :class:`RsUncorrectable`
    when the block is beyond the code's reach.
    """
    n = len(block)
    S = syndromes(block, nsym)
    if not any(S):
        return 0
    lam = _berlekamp_massey(S, nsym)
    nerr = len(lam) - 1
    if nerr <= 0 or nerr > nsym >> 1:
        raise RsUncorrectable()
    positions, loc_exp = [], []
    for i in range(n):
        if _eval_le(lam, _exp_neg(i)) == 0:
            positions.append(n - 1 - i)
            loc_exp.append(i)
    if len(positions) != nerr:
        raise RsUncorrectable()
    omega = _poly_mul(S, lam)[:nsym]
    lam_prime = [0] * max(0, len(lam) - 1)
    for j in range(1, len(lam)):
        if j % 2 == 1:
            lam_prime[j - 1] = lam[j]
    for pos, i in zip(positions, loc_exp):
        X = EXP[i % 255]
        Xinv = _exp_neg(i)
        num = _eval_le(omega, Xinv)
        den = _eval_le(lam_prime, Xinv)
        if den == 0:
            raise RsUncorrectable()
        block[pos] ^= mul(X, div(num, den))
    if any(syndromes(block, nsym)):
        raise RsUncorrectable()
    return len(positions)


# -- region ECC (ecc.ts): parity for a region, kept in one trailer ------------

RS_K = 243
RS_NSYM = 12


def parity_len(data_len: int, k: int = RS_K, nsym: int = RS_NSYM) -> int:
    return -(-data_len // k) * nsym


def ecc_encode(data: bytes, k: int = RS_K, nsym: int = RS_NSYM) -> bytes:
    return b"".join(encode(data[i:i + k], nsym) for i in range(0, len(data), k))


def ecc_repair(data: bytearray, parity: bytes, k: int = RS_K, nsym: int = RS_NSYM) -> tuple[int, list[int]]:
    """Repair ``data`` in place against ``parity``.

    Returns (bytes corrected, indexes of blocks past the correction radius).
    """
    corrected = 0
    failed: list[int] = []
    blocks = -(-len(data) // k)
    for i in range(blocks):
        start = i * k
        end = min(start + k, len(data))
        par = parity[i * nsym:(i + 1) * nsym]
        if len(par) < nsym:
            failed.append(i)
            continue
        # Systematic code: a block is a valid codeword exactly when its data
        # re-encodes to its parity. That check is much cheaper in Python than
        # the syndromes, so the decoder only runs on blocks that changed.
        if encode(data[start:end], nsym) == par:
            continue
        block = bytearray(data[start:end]) + par
        try:
            n = decode_block(block, nsym)
        except RsUncorrectable:
            failed.append(i)
            continue
        if n:
            corrected += n
            data[start:end] = block[: end - start]
    return corrected, failed
