"""Recursive per-directory aggregates of live entries, stored in dir_stats.

The visual workspace shows file counts, sizes and duplicate counts next to
every folder; summing whole subtrees per request would be slow on large
catalogs. refresh() rebuilds the rows of the given remotes in one pass after a
scan or identity rebuild, and get() reads one row back.
"""

from __future__ import annotations

import sqlite3
import time

from . import db

# Slots of one accumulator: [files, dirs, bytes, dup_files, mtime_max].
_FILES, _DIRS, _BYTES, _DUP, _MTIME = range(5)


def _ancestors(parent: str, cache: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """`parent` and every directory above it, nearest first, ending with the root ''.

    Cached per parent: a catalog has far fewer directories than files, and all
    files in one directory share the same chain.
    """
    chain = cache.get(parent)
    if chain is None:
        out = [parent]
        p = parent
        while p:
            p = p.rpartition("/")[0]
            out.append(p)
        chain = cache[parent] = tuple(out)
    return chain


def _slot(agg: dict[tuple[int, str], list], key: tuple[int, str]) -> list:
    rec = agg.get(key)
    if rec is None:
        rec = agg[key] = [0, 0, 0, 0, None]
    return rec


def refresh(conn: sqlite3.Connection, *, remote_ids: list[int] | None = None) -> dict:
    """Recompute dir_stats for the given remotes (all remotes when None).

    Only live entries (present = 1) count. dup_files is judged against the whole
    catalog: a copy on a remote that is not being refreshed still makes a file a
    duplicate, but empty files never are (as in identity.duplicate_groups). Old
    rows are replaced in one transaction, so readers never see a half-built
    remote. mtimes are canonical fixed-width UTC strings, so plain string max()
    is the real maximum.
    """
    started = time.perf_counter()
    if remote_ids is None:
        rids = [int(r[0]) for r in conn.execute("SELECT id FROM remotes ORDER BY id")]
    else:
        rids = sorted({int(r) for r in remote_ids})
    if not rids:
        return {"remotes": 0, "dirs": 0, "seconds": round(time.perf_counter() - started, 3)}
    marks = ",".join("?" * len(rids))

    shared = {
        r[0] for r in conn.execute(
            "SELECT content_group FROM entries WHERE present = 1 AND is_dir = 0 AND size > 0 "
            "AND content_group IS NOT NULL GROUP BY content_group HAVING COUNT(*) >= 2"
        )
    }
    # Every remote gets a root row, even an empty one.
    agg: dict[tuple[int, str], list] = {(rid, ""): [0, 0, 0, 0, None] for rid in rids}
    chains: dict[str, tuple[str, ...]] = {}
    rows = conn.execute(
        "SELECT remote_id, path, parent, is_dir, size, mtime, content_group FROM entries "
        f"WHERE present = 1 AND remote_id IN ({marks})",
        rids,
    )
    for rid, path, parent, is_dir, size, mtime, group in rows:
        chain = _ancestors(parent, chains)
        if is_dir:
            # A live directory gets its own row even when empty, and counts once
            # toward each directory above it.
            _slot(agg, (rid, path))
            for a in chain:
                _slot(agg, (rid, a))[_DIRS] += 1
        else:
            dup = 1 if group in shared else 0
            for a in chain:
                rec = _slot(agg, (rid, a))
                rec[_FILES] += 1
                rec[_BYTES] += size or 0
                rec[_DUP] += dup
                if mtime is not None and (rec[_MTIME] is None or mtime > rec[_MTIME]):
                    rec[_MTIME] = mtime

    with db.transaction(conn):
        conn.execute(f"DELETE FROM dir_stats WHERE remote_id IN ({marks})", rids)
        conn.executemany(
            "INSERT INTO dir_stats(remote_id, path, files, dirs, bytes, dup_files, mtime_max) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ((rid, path, *rec) for (rid, path), rec in agg.items()),
        )
    return {"remotes": len(rids), "dirs": len(agg), "seconds": round(time.perf_counter() - started, 3)}


def get(conn: sqlite3.Connection, remote_id: int, path: str) -> dict | None:
    """The dir_stats row for one directory, or None if no refresh has produced it."""
    row = conn.execute(
        "SELECT remote_id, path, files, dirs, bytes, dup_files, mtime_max FROM dir_stats "
        "WHERE remote_id = ? AND path = ?",
        (remote_id, path),
    ).fetchone()
    return dict(row) if row is not None else None
