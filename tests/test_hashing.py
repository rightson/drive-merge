"""Hash bridging: every digest must equal what `rclone hashsum <type>` prints,
no matter how the bytes are chunked."""

from __future__ import annotations

import hashlib
import io
import random
import shutil
import subprocess

import pytest

from drivemerge import hashing
from drivemerge.hashing import HASH_TYPES, MultiHasher, hash_file, hash_stream

MiB = 1 << 20

requires_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")


def _random_bytes(n: int, seed: int = 20260101) -> bytes:
    return random.Random(seed).randbytes(n)


def _feed(data: bytes, sizes) -> dict[str, str]:
    """Hash `data` in the given chunk sizes (any remainder goes in as one last chunk)."""
    h = MultiHasher()
    pos = 0
    for size in sizes:
        h.update(data[pos:pos + size])
        pos += size
    h.update(data[pos:])
    assert h.size == len(data)
    return h.hexdigests()


def _fixed(size: int, total: int):
    return [size] * (total // size)


def _irregular(total: int, seed: int, top: int):
    rng = random.Random(seed)
    sizes, left = [], total
    while left > 0:
        n = min(left, rng.choice([0, 1, 2, 159, 160, 161, rng.randint(1, top)]))
        sizes.append(n)
        left -= n
    return sizes


def naive_quickxor(data: bytes) -> str:
    """Byte-at-a-time QuickXorHash straight from Microsoft's spec."""
    mask = (1 << 160) - 1
    v = 0
    for p, b in enumerate(data):
        x = b << ((11 * p) % 160)
        v ^= (x & mask) | (x >> 160)
    v ^= len(data) << 96
    return v.to_bytes(20, "little").hex()


def naive_dropbox(data: bytes, block: int = 4 * MiB) -> str:
    digests = b"".join(hashlib.sha256(data[i:i + block]).digest() for i in range(0, len(data), block))
    return hashlib.sha256(digests).hexdigest()


def _rclone_hashsum(kind: str, path) -> str:
    out = subprocess.run(["rclone", "hashsum", kind, str(path)], check=True, capture_output=True, text=True).stdout
    return out.split()[0]


# --- oracle: rclone ---------------------------------------------------------

ORACLE_SIZES = [0, 1, 159, 160, 161, 1000, 4 * MiB - 1, 4 * MiB, 4 * MiB + 1, 8 * MiB, 10 * MiB + 12345]


@requires_rclone
@pytest.mark.rclone
@pytest.mark.parametrize("size", ORACLE_SIZES)
def test_matches_rclone(tmp_path, size):
    path = tmp_path / f"f{size}.bin"
    path.write_bytes(_random_bytes(size))
    got = hash_file(path)
    assert got["size"] == size
    assert {t: got[t] for t in HASH_TYPES} == {t: _rclone_hashsum(t, path) for t in HASH_TYPES}


@requires_rclone
@pytest.mark.rclone
def test_matches_rclone_with_awkward_chunking(tmp_path):
    data = _random_bytes(4 * MiB + 4321)
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    got = _feed(data, _irregular(len(data), seed=7, top=300_000))
    assert got == {t: _rclone_hashsum(t, path) for t in HASH_TYPES}


# --- rclone-free references --------------------------------------------------

def test_hex_digests_shape():
    got = hash_stream(io.BytesIO(b"abc"))
    assert set(got) == set(HASH_TYPES) | {"size"}
    assert got["size"] == 3
    assert got["md5"] == hashlib.md5(b"abc").hexdigest()
    assert got["sha1"] == hashlib.sha1(b"abc").hexdigest()
    assert got["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert got["dropbox"] == hashlib.sha256(hashlib.sha256(b"abc").digest()).hexdigest()
    for t in HASH_TYPES:
        assert got[t] == got[t].lower()
        int(got[t], 16)


def test_empty_input():
    got = hash_stream(io.BytesIO(b""))
    assert got["size"] == 0
    assert got["quickxor"] == "00" * 20
    assert got["dropbox"] == hashlib.sha256(b"").hexdigest()
    assert got["md5"] == "d41d8cd98f00b204e9800998ecf8427e"


@pytest.mark.parametrize("size", [1, 14, 15, 20, 21, 29, 159, 160, 161, 319, 320, 321, 3000])
def test_quickxor_matches_naive_reference(size):
    data = _random_bytes(size)
    h = MultiHasher()
    h.update(data)
    assert h.hexdigests()["quickxor"] == naive_quickxor(data)


def test_quickxor_wraparound_bytes():
    # Bytes at stream positions 14 and 29 straddle the 160-bit boundary.
    for pos in (14, 29, 43, 160 + 14):
        data = bytearray(pos + 1)
        data[pos] = 0xFF
        h = MultiHasher()
        h.update(bytes(data))
        assert h.hexdigests()["quickxor"] == naive_quickxor(bytes(data))


def test_dropbox_block_boundaries(monkeypatch):
    block = 1000
    monkeypatch.setattr(hashing, "DROPBOX_BLOCK", block)
    for size in (0, 1, block - 1, block, block + 1, 3 * block, 3 * block + 17):
        data = _random_bytes(size)
        for sizes in ([size or 1], [1], [7], [block], [333], _irregular(size, seed=size, top=2500)):
            h = MultiHasher()
            pos = 0
            for n in sizes:
                h.update(data[pos:pos + n])
                pos += n
            h.update(data[pos:])
            assert h.hexdigests()["dropbox"] == naive_dropbox(data, block), (size, sizes[:3])


def test_dropbox_real_block_size_matches_reference():
    data = _random_bytes(2 * 4 * MiB + 5)
    h = MultiHasher()
    h.update(data)
    assert h.hexdigests()["dropbox"] == naive_dropbox(data)


# --- chunking invariance -----------------------------------------------------

@pytest.mark.parametrize("chunk", [1, 7, 160, 4096, 1 << 20])
def test_chunking_invariance_small(chunk):
    data = _random_bytes(12_345)
    assert _feed(data, _fixed(chunk, len(data))) == _feed(data, [])


@pytest.mark.parametrize("chunk", [160, 4096, 1 << 20, (1 << 20) + 13])
def test_chunking_invariance_across_dropbox_block(chunk):
    data = _random_bytes(4 * MiB + 777)
    assert _feed(data, _fixed(chunk, len(data))) == _feed(data, [])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_chunking_invariance_irregular(seed):
    data = _random_bytes(4 * MiB + 99, seed=seed)
    assert _feed(data, _irregular(len(data), seed, top=200_000)) == _feed(data, [])


# --- API behaviour ---------------------------------------------------------

def test_update_accepts_buffers_and_ignores_empty():
    data = _random_bytes(5000)
    want = _feed(data, [])
    h = MultiHasher()
    h.update(b"")
    h.update(bytearray(data[:1234]))
    h.update(memoryview(data)[1234:])
    h.update(b"")
    assert h.size == len(data)
    assert h.hexdigests() == want


def test_hexdigests_does_not_consume_state():
    data = _random_bytes(4 * MiB + 10)
    h = MultiHasher()
    h.update(data[:1000])
    h.hexdigests()
    h.update(data[1000:])
    assert h.hexdigests() == h.hexdigests() == _feed(data, [])


def test_hash_stream_without_readinto():
    class ReadOnly:
        def __init__(self, data):
            self._b = io.BytesIO(data)

        def read(self, n=-1):
            return self._b.read(n)

    data = _random_bytes(10_000)
    got = hash_stream(ReadOnly(data), chunk_size=333)
    assert got == {**_feed(data, []), "size": len(data)}


def test_hash_stream_chunk_size_irrelevant():
    data = _random_bytes(20_000)
    assert hash_stream(io.BytesIO(data), chunk_size=1) == hash_stream(io.BytesIO(data), chunk_size=1 << 20)


def test_hash_file_accepts_str_and_path(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(_random_bytes(777))
    assert hash_file(path) == hash_file(str(path))
    assert hash_file(path)["size"] == 777
