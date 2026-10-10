"""What each storage provider can and cannot do, as far as drive-merge cares.

Source: https://rclone.org/overview/ (rclone v1.75). The key fact driving the
design: no hash type is shared by all four clouds, so cross-cloud duplicate
detection needs hash *bridging* (see identity.py / hash_bridges table).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

HASH_TYPES: tuple[str, ...] = ("md5", "sha1", "sha256", "dropbox", "quickxor")


@dataclass(frozen=True)
class Provider:
    key: str
    native_hashes: tuple[str, ...]  # hash types the provider stores (free to list)
    case_insensitive: bool
    allows_duplicate_paths: bool    # Drive: several files may share one path
    server_side_move: bool          # move/rename within the remote without download
    server_side_copy: bool
    has_native_docs: bool           # objects with no bytes/hash (Google Docs, ...)
    notes: str = ""


# A local disk is case-insensitive by default on macOS (APFS) and Windows (NTFS), and not on Linux; a mount that
# differs (exFAT, a case-folded ext4) says so with `case_insensitive` in its [[remotes]] entry.
LOCAL_CASE_INSENSITIVE = sys.platform in ("darwin", "win32")

PROVIDERS: dict[str, Provider] = {
    "drive": Provider(
        "drive", ("md5", "sha1", "sha256"), case_insensitive=False, allows_duplicate_paths=True,
        server_side_move=True, server_side_copy=True, has_native_docs=True,
        notes="rclone's shared client_id is being retired in 2026: configure your own client_id. "
              "Use --drive-skip-shortcuts when scanning to avoid double counting.",
    ),
    "dropbox": Provider(
        "dropbox", ("dropbox",), case_insensitive=True, allows_duplicate_paths=False,
        server_side_move=True, server_side_copy=True, has_native_docs=False,
        notes="No ListR: recursive listing is slower than Drive.",
    ),
    "onedrive": Provider(
        "onedrive", ("quickxor",), case_insensitive=True, allows_duplicate_paths=False,
        server_side_move=True, server_side_copy=True, has_native_docs=False,
        notes="QuickXorHash for both Personal and Business since rclone 1.62.",
    ),
    "iclouddrive": Provider(
        "iclouddrive", (), case_insensitive=False, allows_duplicate_paths=False,
        server_side_move=False, server_side_copy=False, has_native_docs=False,
        notes="Experimental rclone backend; no hashes; trust token expires every 30 days; "
              "every copy/move is a download + upload. Treat as a source, not a target.",
    ),
    # Local disks and rclone's in-memory backend: used for tests and for a
    # Mac's synced iCloud folder. rclone computes any hash by reading the file.
    "local": Provider(
        "local", ("md5", "sha1", "sha256", "dropbox", "quickxor"), case_insensitive=LOCAL_CASE_INSENSITIVE,
        allows_duplicate_paths=False, server_side_move=True, server_side_copy=True, has_native_docs=False,
        notes="Hashes are computed by reading every byte: scanning with hashes is slow on big disks.",
    ),
    "memory": Provider(
        "memory", ("md5",), case_insensitive=False, allows_duplicate_paths=False,
        server_side_move=True, server_side_copy=True, has_native_docs=False,
    ),
    "other": Provider(
        "other", (), case_insensitive=False, allows_duplicate_paths=False,
        server_side_move=False, server_side_copy=False, has_native_docs=False,
    ),
}


def get(key: str) -> Provider:
    return PROVIDERS.get(key, PROVIDERS["other"])
