"""Compute every provider's hash type from one pass over the bytes (hash bridging).

No hash type is shared by all clouds (Drive: md5/sha1/sha256, Dropbox: its
content hash, OneDrive: QuickXorHash, iCloud: none), so to prove two files on
different clouds equal we download one copy once and derive all five digests
from the same bytes. Values equal what `rclone hashsum <type>` prints.

    HASH_TYPES = ("md5", "sha1", "sha256", "dropbox", "quickxor")

    h = MultiHasher(); h.update(chunk) ...; h.hexdigests()   # {type: lowercase hex}
    hash_stream(f)  /  hash_file(path)                       # hexdigests + {"size": int}
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

HASH_TYPES: tuple[str, ...] = ("md5", "sha1", "sha256", "dropbox", "quickxor")

DROPBOX_BLOCK = 4 * 1024 * 1024

# QuickXorHash: 160-bit state, each byte rotated left by a further 11 bits.
_QX_BITS = 160
_QX_DIGEST_BYTES = _QX_BITS // 8
_QX_SHIFT = 11
_QX_MASK = (1 << _QX_BITS) - 1
# 160 * 11 is a multiple of 160, so the bit offset of the byte at stream
# position p depends only on p % 160: bytes of one residue class can be
# XOR-reduced first. A "row" is one period of 160 bytes.
_QX_ROW = _QX_BITS
_QX_OFFSETS = tuple((_QX_SHIFT * j) % _QX_BITS for j in range(_QX_ROW))
# Rows folded per step; keeps the big ints small enough to stay in cache.
_QX_BLOCK_ROWS = 512


def _fold_rows(view: memoryview) -> int:
    """XOR a whole number of 160-byte rows into one row (as a little-endian int).

    Halving the integer (high half ^ low half) is a handful of big-int ops that
    run at memory speed, versus one Python-level step per byte.
    """
    rows = len(view) // _QX_ROW
    split = (rows + 1) // 2 * _QX_ROW
    x = int.from_bytes(view[:split], "little") ^ int.from_bytes(view[split:], "little")
    rows = split // _QX_ROW
    while rows > 1:
        lo_rows = (rows + 1) // 2
        bits = lo_rows * _QX_ROW * 8
        x = (x & ((1 << bits) - 1)) ^ (x >> bits)
        rows = lo_rows
    return x


class MultiHasher:
    """md5, sha1, sha256, Dropbox content hash and QuickXorHash in one pass.

    `update` takes chunks of any size; block and 160-byte-period boundaries are
    tracked by absolute stream position, so chunking never changes the result.
    """

    def __init__(self) -> None:
        self.size = 0
        self._md5 = hashlib.md5(usedforsecurity=False)
        self._sha1 = hashlib.sha1(usedforsecurity=False)
        self._sha256 = hashlib.sha256()
        self._db_outer = hashlib.sha256()   # sha256 over the per-block digests
        self._db_block = hashlib.sha256()
        self._db_fill = 0                   # bytes in the current 4 MiB block
        # XOR of all bytes by stream position mod 160 (byte j of this int is
        # residue j); rotated into place only when the digest is requested.
        self._qx_rows = 0

    def update(self, data: bytes | bytearray | memoryview) -> None:
        view = memoryview(data).cast("B")
        n = len(view)
        if not n:
            return
        self._md5.update(view)
        self._sha1.update(view)
        self._sha256.update(view)
        self._update_dropbox(view)
        self._update_quickxor(view)
        self.size += n

    def _update_dropbox(self, view: memoryview) -> None:
        pos = 0
        n = len(view)
        while pos < n:
            take = min(n - pos, DROPBOX_BLOCK - self._db_fill)
            self._db_block.update(view[pos:pos + take])
            self._db_fill += take
            pos += take
            if self._db_fill == DROPBOX_BLOCK:
                self._db_outer.update(self._db_block.digest())
                self._db_block = hashlib.sha256()
                self._db_fill = 0

    def _update_quickxor(self, view: memoryview) -> None:
        n = len(view)
        start = self.size % _QX_ROW
        pos = 0
        if start:  # finish the partially filled row
            pos = min(n, _QX_ROW - start)
            self._qx_rows ^= int.from_bytes(view[:pos], "little") << (8 * start)
        step = _QX_BLOCK_ROWS * _QX_ROW
        while n - pos >= _QX_ROW:
            end = pos + min(n - pos, step) // _QX_ROW * _QX_ROW
            self._qx_rows ^= _fold_rows(view[pos:end])
            pos = end
        if pos < n:  # trailing partial row starts at residue 0 by construction
            self._qx_rows ^= int.from_bytes(view[pos:], "little")

    def _quickxor_hex(self) -> str:
        acc = 0
        for j, b in enumerate(self._qx_rows.to_bytes(_QX_ROW, "little")):
            if b:
                acc ^= b << _QX_OFFSETS[j]
        acc = (acc & _QX_MASK) ^ (acc >> _QX_BITS)  # bytes straddling bit 160 wrap around
        acc ^= (self.size & 0xFFFFFFFFFFFFFFFF) << (_QX_BITS - 64)  # length goes in the last 8 bytes
        return acc.to_bytes(_QX_DIGEST_BYTES, "little").hex()

    def _dropbox_hex(self) -> str:
        outer = self._db_outer.copy()
        if self._db_fill:  # trailing partial block; an empty file has no blocks at all
            outer.update(self._db_block.digest())
        return outer.hexdigest()

    def hexdigests(self) -> dict[str, str]:
        """Lowercase hex per HASH_TYPES key. Does not consume the hasher."""
        return {
            "md5": self._md5.hexdigest(),
            "sha1": self._sha1.hexdigest(),
            "sha256": self._sha256.hexdigest(),
            "dropbox": self._dropbox_hex(),
            "quickxor": self._quickxor_hex(),
        }


def hash_stream(f: BinaryIO, chunk_size: int = 1 << 20) -> dict:
    """Hash a binary stream to EOF; returns the five hexdigests plus "size"."""
    h = MultiHasher()
    buf = bytearray(chunk_size)
    view = memoryview(buf)
    readinto = getattr(f, "readinto", None)
    while True:
        if readinto is not None:
            n = readinto(buf)
            if not n:
                break
            h.update(view[:n])
        else:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return {**h.hexdigests(), "size": h.size}


def hash_file(path: str | Path) -> dict:
    """Hash a local file (same result shape as `hash_stream`)."""
    with open(path, "rb") as f:
        return hash_stream(f)
