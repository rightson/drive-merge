"""Shared fixtures. `catalog` builds an in-memory DB with hand-made entries so
module tests do not depend on rclone or on the scanner."""

from __future__ import annotations

import posixpath
import shutil
import unicodedata

import pytest

from drivemerge import db, providers
from drivemerge.categories import category_of


class Catalog:
    """Tiny builder for test catalogs.

        cat = Catalog(conn)
        g = cat.remote("gdrive", "drive")
        e = cat.file(g, "Photos/a.jpg", size=10, md5="aa")
    """

    def __init__(self, conn):
        self.conn = conn
        self._scan: dict[int, int] = {}

    def remote(self, name: str, provider: str = "local", *, writable: bool = True,
               case_insensitive: bool | None = None) -> int:
        """A `local` remote is case-sensitive here on every platform (the real default follows the OS)."""
        prov = providers.get(provider)
        if case_insensitive is None:
            case_insensitive = False if provider == "local" else prov.case_insensitive
        rid = self.conn.execute(
            "INSERT INTO remotes(name, provider, read_remote, write_remote, case_insensitive, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (name, provider, f"{name}:", f"{name}:" if writable else None,
             int(case_insensitive), db.utcnow()),
        ).fetchone()[0]
        self._scan[rid] = self.conn.execute(
            "INSERT INTO scans(remote_id, status, started_at, finished_at) VALUES (?, 'complete', ?, ?) RETURNING id",
            (rid, db.utcnow(), db.utcnow()),
        ).fetchone()[0]
        return rid

    def _insert(self, rid: int, path: str, *, is_dir: bool, size=None, mtime=None, mime=None,
                provider_id=None, native_doc=False, present=True, **hashes) -> int:
        name = posixpath.basename(path)
        ext = name.rsplit(".", 1)[1].lower() if "." in name and not is_dir else ""
        key = f"id:{provider_id}" if provider_id else f"p:{path}"
        rv = db.next_row_version(self.conn)
        scan = self._scan[rid]
        return self.conn.execute(
            """INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category,
                 is_dir, size, mtime, mime, provider_id, md5, sha1, sha256, dropbox, quickxor,
                 is_native_doc, present, first_seen_scan, last_seen_scan, row_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
            (rid, key, path, posixpath.dirname(path), name,
             unicodedata.normalize("NFC", name).casefold(), ext,
             "other" if is_dir else category_of(ext, mime), int(is_dir), size,
             mtime or "2024-01-01T00:00:00Z", mime, provider_id,
             hashes.get("md5"), hashes.get("sha1"), hashes.get("sha256"),
             hashes.get("dropbox"), hashes.get("quickxor"), int(native_doc), int(present),
             scan, scan, rv),
        ).fetchone()[0]

    def file(self, rid: int, path: str, *, size: int = 1, **kw) -> int:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if not self.conn.execute(
                "SELECT 1 FROM entries WHERE remote_id = ? AND path = ? AND is_dir = 1", (rid, d)
            ).fetchone():
                self._insert(rid, d, is_dir=True)
        return self._insert(rid, path, is_dir=False, size=size, **kw)

    def dir(self, rid: int, path: str) -> int:
        return self._insert(rid, path, is_dir=True)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def catalog(conn):
    return Catalog(conn)


requires_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")
