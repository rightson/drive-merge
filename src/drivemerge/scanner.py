"""Ingest `rclone lsjson -R` listings into the catalog (incremental, streaming).

STUB — implemented in wave 1. Contract:

    @dataclass
    class ScanResult: scan_id, remote, root, status, files, dirs, bytes, tombstoned, error

    def normalize_item(item: dict, root: str = "") -> dict   # pure: lsjson object -> entries column values
    def scan_remote(conn, rclone, remote: RemoteConfig, *, root: str = "", progress=None) -> ScanResult
"""
