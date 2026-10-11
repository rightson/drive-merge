"""How the clouds overlap: for every distinct content, on which set of clouds does it live.

Read-only (docs/EXPLORE.md §C). A content is an exact content group; a live file without one counts as its
own content (so the overlap of a cloud with files that have no comparable hash is *under*-counted, which
`unverified_files` reports). Empty files are placeholders, not content, and are left out.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .compare import CompareError, load_remotes


def coverage(conn: sqlite3.Connection, *, category: str | None = None, max_bars: int = 24) -> dict[str, Any]:
    """Per-cloud numbers and the intersection of clouds, optionally for one category of file.

    `bytes` of a content is its size once, `stored_bytes` the sum over its copies. The first `max_bars`
    combinations of clouds (biggest bytes first) are listed; the rest are summed into `other`.
    """
    if not isinstance(max_bars, int) or max_bars < 1:
        raise CompareError("BAD_QUERY", "max_bars must be a positive integer", param="max_bars")
    remotes = load_remotes(conn)
    order = {r.id: i for i, r in enumerate(remotes)}
    live = f"e.present = 1 AND e.is_dir = 0 AND {db.outside_quarantine(conn, 'e.')}"
    cat_sql, cat_args = ("AND e.category = ?", [category]) if category else ("", [])

    # Per remote: all live files (the card's size).
    per_remote = {
        rid: (files, size) for rid, files, size in conn.execute(
            f"SELECT e.remote_id, COUNT(*), COALESCE(SUM(e.size), 0) FROM entries e WHERE {live} {cat_sql} "
            "GROUP BY e.remote_id", cat_args)
    }
    unverified: dict[int, int] = {}

    # combo (frozenset of remote ids) -> [contents, bytes, copies, stored_bytes]
    combos: dict[frozenset[int], list[int]] = {}

    def add(key: frozenset[int], contents: int, size: int, copies: int, stored: int) -> None:
        acc = combos.setdefault(key, [0, 0, 0, 0])
        acc[0] += contents
        acc[1] += size
        acc[2] += copies
        acc[3] += stored

    for ids, size, copies, stored in conn.execute(
            f"SELECT GROUP_CONCAT(DISTINCT e.remote_id), MAX(e.size), COUNT(*), SUM(e.size) FROM entries e "
            f"WHERE {live} AND e.size > 0 AND e.content_group IS NOT NULL {cat_sql} GROUP BY e.content_group",
            cat_args):
        add(frozenset(int(i) for i in ids.split(",")), 1, size, copies, stored)
    for rid, n, size in conn.execute(
            f"SELECT e.remote_id, COUNT(*), SUM(e.size) FROM entries e WHERE {live} AND e.size > 0 "
            f"AND e.content_group IS NULL {cat_sql} GROUP BY e.remote_id", cat_args):
        unverified[rid] = n
        add(frozenset((rid,)), n, size, n, size)

    def names_of(key: frozenset[int]) -> list[str]:
        return [remotes[i].name for i in sorted(order[r] for r in key)]

    only_here: dict[int, list[int]] = {}
    for key, acc in combos.items():
        if len(key) == 1:
            only_here[next(iter(key))] = acc

    ranked = sorted(((names_of(k), a) for k, a in combos.items()),
                    key=lambda t: (-t[1][1], -t[1][0], t[0]))
    shown, rest = ranked[:max_bars], ranked[max_bars:]

    def bar(names: list[str], acc: list[int]) -> dict:
        return {"remotes": names, "contents": acc[0], "bytes": acc[1], "copies": acc[2], "stored_bytes": acc[3]}

    other = None
    if rest:
        other = {"combinations": len(rest), "contents": sum(a[0] for _, a in rest), "bytes": sum(a[1] for _, a in rest),
                 "copies": sum(a[2] for _, a in rest), "stored_bytes": sum(a[3] for _, a in rest)}
    contents = sum(a[0] for a in combos.values())
    total_bytes = sum(a[1] for a in combos.values())
    stored = sum(a[3] for a in combos.values())

    last_scans = {
        r[0]: {"id": r[1], "status": r[2], "finished_at": r[3]} for r in conn.execute(
            "SELECT s.remote_id, s.id, s.status, s.finished_at FROM scans s "
            "JOIN (SELECT remote_id, MAX(id) AS id FROM scans WHERE status = 'complete' GROUP BY remote_id) m "
            "ON m.id = s.id")
    }
    return {
        "remotes": [{
            "name": r.name, "provider": r.provider, "writable": r.writable,
            "files": per_remote.get(r.id, (0, 0))[0], "bytes": per_remote.get(r.id, (0, 0))[1],
            "only_here": {"contents": only_here.get(r.id, [0, 0])[0], "bytes": only_here.get(r.id, [0, 0])[1]},
            "unverified_files": unverified.get(r.id, 0),
            "last_scan": last_scans.get(r.id),
        } for r in remotes],
        "totals": {"contents": contents, "bytes": total_bytes, "stored_bytes": stored,
                   "redundant_bytes": stored - total_bytes, "copies": sum(a[2] for a in combos.values())},
        "intersections": [bar(n, a) for n, a in shown],
        "other": other,
    }


__all__ = ["coverage"]
