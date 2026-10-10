"""Thin, testable wrapper around the rclone binary.

STUB — implemented in wave 1. Contract:

    class RcloneError(Exception): returncode: int; stderr: str; args: list[str]

    class Rclone:
        def __init__(self, binary="rclone", *, config=None, extra_args=(), runner=None)
        @staticmethod
        def join(remote: str, path: str) -> str          # 'gd:' + 'a/b' -> 'gd:a/b'; 'gd:root' + 'a' -> 'gd:root/a'
        def lsjson(self, remote, path="", *, recursive=True, hash_types=(), extra_args=()) -> Iterator[dict]
        def stat(self, remote, path, *, hash_types=()) -> dict | None   # None when the object does not exist
        def copyto(self, src_remote, src_path, dst_remote, dst_path) -> None
        def moveto(self, src_remote, src_path, dst_remote, dst_path) -> None
        def download(self, remote, path, local_path) -> None
        def about(self, remote) -> dict | None           # {'total','used','free'} or None if unsupported
        def version(self) -> str
"""
