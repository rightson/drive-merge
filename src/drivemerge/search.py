"""Catalog queries: full-text search, lookups, the change feed and stats.

Reads the catalog only; nothing here touches a remote.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from typing import Any

HASH_KEYS = ("md5", "sha1", "sha256", "dropbox", "quickxor")

_COLS = (
    "e.id, e.remote_id, r.name AS remote, e.path, e.name, e.ext, e.category, e.is_dir, e.size, "
    "e.mtime, e.mime, e.md5, e.sha1, e.sha256, e.dropbox, e.quickxor, e.content_group, "
    "e.present, e.is_native_doc, e.row_version"
)
_FROM = "FROM entries e JOIN remotes r ON r.id = e.remote_id"
_FTS_FROM = "FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid JOIN remotes r ON r.id = e.remote_id"
_DUPLICATED = (
    "e.content_group IN (SELECT d.content_group FROM entries d "
    "WHERE d.present = 1 AND d.is_dir = 0 AND d.content_group IS NOT NULL "
    "GROUP BY d.content_group HAVING COUNT(*) >= 2)"
)


def entry_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Public dict for a row selected with the entries columns joined to remotes."""
    return {
        "id": row["id"],
        "remote_id": row["remote_id"],
        "remote": row["remote"],
        "path": row["path"],
        "name": row["name"],
        "ext": row["ext"],
        "category": row["category"],
        "is_dir": bool(row["is_dir"]),
        "size": row["size"],
        "mtime": row["mtime"],
        "mime": row["mime"],
        "hashes": {k: row[k] for k in HASH_KEYS if row[k] is not None},
        "content_group": row["content_group"],
        "present": bool(row["present"]),
        "is_native_doc": bool(row["is_native_doc"]),
        "row_version": row["row_version"],
    }


def _terms(q: str | None) -> list[str]:
    return unicodedata.normalize("NFC", q).split() if q else []


def _fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(conn: sqlite3.Connection, q: str | None = None, *, remote: str | None = None,
           category: str | None = None, ext: str | None = None, min_size: int | None = None,
           max_size: int | None = None, modified_after: str | None = None,
           modified_before: str | None = None, path_prefix: str | None = None,
           include_dirs: bool = False, include_missing: bool = False,
           duplicates_only: bool = False, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Search the catalog. Live files only unless the flags say otherwise.

    Each whitespace-separated term in `q` must match the name or the path.
    Terms of 3+ characters use the trigram FTS index and are ranked by bm25;
    shorter terms use LIKE and results fall back to remote, path order.
    `modified_after` is inclusive and `modified_before` exclusive. Size
    filters apply to files with a known size.
    """
    terms = _terms(q)
    conds: list[str] = []
    params: list[Any] = []

    fts = [t for t in terms if len(t) >= 3]
    if fts:
        src, order = _FTS_FROM, "bm25(entries_fts), e.path, r.name, e.id"
        conds.append("entries_fts MATCH ?")
        params.append(" ".join(_fts_phrase(t) for t in fts))
    else:
        src, order = _FROM, "r.name, e.path, e.id"
    for t in terms:
        if len(t) < 3:
            conds.append(r"(e.name_norm LIKE ? ESCAPE '\' OR e.path LIKE ? ESCAPE '\')")
            params += [_like(t.casefold()), _like(t)]

    if not include_missing:
        conds.append("e.present = 1")
    if not include_dirs:
        conds.append("e.is_dir = 0")
    if remote:
        conds.append("r.name = ?")
        params.append(remote)
    if category:
        conds.append("e.category = ?")
        params.append(category)
    ext_norm = (ext or "").strip().lower().removeprefix(".")
    if ext_norm:
        conds.append("e.ext = ?")
        params.append(ext_norm)
    if min_size is not None or max_size is not None:
        conds.append("e.is_dir = 0")
    if min_size is not None:
        conds.append("e.size >= ?")
        params.append(min_size)
    if max_size is not None:
        conds.append("e.size <= ?")
        params.append(max_size)
    if modified_after:
        conds.append("e.mtime >= ?")
        params.append(modified_after)
    if modified_before:
        conds.append("e.mtime < ?")
        params.append(modified_before)
    prefix = (path_prefix or "").strip("/")
    if prefix:
        conds.append("(e.path = ? OR substr(e.path, 1, ?) = ?)")
        params += [prefix, len(prefix) + 1, prefix + "/"]
    if duplicates_only:
        conds.append(_DUPLICATED)

    where = " AND ".join(conds) or "1"
    sql = f"SELECT {_COLS} {src} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?"
    limit = min(max(int(limit), 1), 1000)
    rows = conn.execute(sql, [*params, limit, max(int(offset), 0)]).fetchall()
    return [entry_to_dict(r) for r in rows]


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    """One entry by id, tombstones included; None if it does not exist."""
    row = conn.execute(f"SELECT {_COLS} {_FROM} WHERE e.id = ?", (entry_id,)).fetchone()
    return entry_to_dict(row) if row else None


def entries_at(conn: sqlite3.Connection, remote_id: int, path: str) -> list[dict[str, Any]]:
    """Live entries (files and dirs) at an exact path. Drive can hold several."""
    rows = conn.execute(
        f"SELECT {_COLS} {_FROM} WHERE e.remote_id = ? AND e.path = ? AND e.present = 1 ORDER BY e.id",
        (remote_id, path.strip("/")),
    ).fetchall()
    return [entry_to_dict(r) for r in rows]


def _parse_cursor(cursor: str | None) -> tuple[int, int]:
    if not cursor:
        return 0, 0
    try:
        rv, rid = cursor.split(":")
        return int(rv), int(rid)
    except ValueError as exc:
        raise ValueError(f"invalid change cursor {cursor!r}") from exc


def changes(conn: sqlite3.Connection, cursor: str | None = None,
            limit: int = 1000) -> tuple[list[dict[str, Any]], str]:
    """Rows changed after `cursor` ("<row_version>:<id>"), tombstones included.

    Returns (rows, next_cursor). With no new rows, next_cursor is the input.
    """
    limit = min(max(int(limit), 1), 10000)
    rv, rid = _parse_cursor(cursor)
    rows = conn.execute(
        f"SELECT {_COLS} {_FROM} WHERE (e.row_version, e.id) > (?, ?) "
        "ORDER BY e.row_version, e.id LIMIT ?",
        (rv, rid, limit),
    ).fetchall()
    out = [entry_to_dict(r) for r in rows]
    if not out:
        return out, cursor or ""
    last = out[-1]
    return out, f"{last['row_version']}:{last['id']}"


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per-remote and total counts, duplicate groups and per-category totals (live only)."""
    last_scans = {
        r["remote_id"]: {"id": r["id"], "status": r["status"], "finished_at": r["finished_at"]}
        for r in conn.execute(
            "SELECT s.id, s.remote_id, s.status, s.finished_at FROM scans s "
            "JOIN (SELECT remote_id, MAX(id) AS id FROM scans GROUP BY remote_id) m ON m.id = s.id"
        )
    }
    remotes = []
    for r in conn.execute(
        """SELECT r.id, r.name, r.provider,
             COALESCE(SUM(CASE WHEN e.is_dir = 0 AND e.present = 1 THEN 1 END), 0) AS files,
             COALESCE(SUM(CASE WHEN e.is_dir = 1 AND e.present = 1 THEN 1 END), 0) AS dirs,
             COALESCE(SUM(CASE WHEN e.is_dir = 0 AND e.present = 1 THEN e.size END), 0) AS bytes
           FROM remotes r LEFT JOIN entries e ON e.remote_id = r.id
           GROUP BY r.id ORDER BY r.name"""
    ):
        remotes.append({
            "id": r["id"], "name": r["name"], "provider": r["provider"],
            "files": r["files"], "dirs": r["dirs"], "bytes": r["bytes"],
            "last_scan": last_scans.get(r["id"]),
        })

    files, total_bytes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM entries WHERE is_dir = 0 AND present = 1"
    ).fetchone()

    groups, redundant_files, redundant_bytes = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(copies - 1), 0), COALESCE(SUM((copies - 1) * size), 0)
           FROM (SELECT COUNT(*) AS copies, MAX(size) AS size FROM entries
                 WHERE is_dir = 0 AND present = 1 AND content_group IS NOT NULL
                 GROUP BY content_group HAVING COUNT(*) >= 2)"""
    ).fetchone()

    categories = [
        {"category": r["category"], "files": r["files"], "bytes": r["bytes"]}
        for r in conn.execute(
            "SELECT category, COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM entries "
            "WHERE is_dir = 0 AND present = 1 GROUP BY category ORDER BY bytes DESC, category"
        )
    ]

    return {
        "remotes": remotes,
        "totals": {"files": files, "bytes": total_bytes},
        "duplicates": {"groups": groups, "redundant_files": redundant_files,
                       "redundant_bytes": redundant_bytes},
        "categories": categories,
    }
