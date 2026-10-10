"""Compute every provider's hash type from one pass over the bytes (hash bridging).

STUB — implemented in wave 1. Contract:

    HASH_TYPES = ("md5", "sha1", "sha256", "dropbox", "quickxor")

    class MultiHasher:
        size: int                         # bytes consumed so far
        def update(self, data: bytes) -> None
        def hexdigests(self) -> dict[str, str]   # one lowercase-hex value per HASH_TYPES key

    def hash_stream(f: BinaryIO, chunk_size: int = 1 << 20) -> dict   # hexdigests + {"size": int}
    def hash_file(path: str | Path) -> dict                           # same, from a local file

Values must equal what `rclone hashsum <type>` prints for the same bytes.
"""
