"""Explore: where is it? Search results grouped by content, with one presence column per cloud.

Read-only (docs/EXPLORE.md §D). Hits are the live files `search.search` would find (the same WHERE builder),
minus the quarantine folder. Hits are grouped by content key; every group loads *all* its live copies, renamed
ones included, so a copy the query does not match is still listed under its content (`matched: false`). Presence
filters and copy counts judge the whole copy set, not only the hits.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from . import db, search
from .compare import CompareError, chunks, load_remotes, remote_named

SORTS = ("relevance", "size", "mtime", "name", "copies")
MAX_LIMIT = 200
_TOP_EXTS = 12
_COLS = "e.id, e.remote_id, e.path, e.name, e.ext, e.category, e.size, e.mtime, e.content_group"


def _int(value: Any, param: str, *, lo: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < lo:
        raise CompareError("BAD_QUERY", f"{param} must be an integer >= {lo}", param=param)
    return value


def explore(conn: sqlite3.Connection, q: str | None = None, *, in_remote: str | None = None,
            category: str | None = None, ext: str | None = None, min_size: int | None = None,
            max_size: int | None = None, modified_after: str | None = None, modified_before: str | None = None,
            path_prefix: str | None = None, on: Sequence[str] = (), not_on: Sequence[str] = (),
            only_on: Sequence[str] | None = None, copies_min: int | None = None, copies_max: int | None = None,
            sort: str = "relevance", limit: int = 50, offset: int = 0, scan_cap: int = 20000) -> dict[str, Any]:
    """Group the live files matching `q` and the filters by content and answer with one result per content.

    `in_remote` restricts the *hits* to one cloud. `on` (every listed cloud holds a copy), `not_on` (none
    does), `only_on` (exactly these clouds) and `copies_min/max` (live copies) apply to the full copy set.
    At most `scan_cap` hits are read; `truncated` says there were more. Sorts: relevance (bm25 of the best
    matching copy when the query has a full-text term, else name), size desc, mtime desc, name, copies desc.
    """
    if sort not in SORTS:
        raise CompareError("BAD_QUERY", f"sort must be one of {', '.join(SORTS)}", param="sort")
    limit = min(_int(limit, "limit", lo=1) or 1, MAX_LIMIT)
    offset = _int(offset, "offset") or 0
    scan_cap = _int(scan_cap, "scan_cap", lo=1) or 1
    min_size, max_size = _int(min_size, "min_size"), _int(max_size, "max_size")
    copies_min, copies_max = _int(copies_min, "copies_min"), _int(copies_max, "copies_max")

    remotes = load_remotes(conn)
    order = {r.id: i for i, r in enumerate(remotes)}
    if in_remote:
        remote_named(remotes, in_remote)
    on_ids = {remote_named(remotes, n).id for n in on}
    not_ids = {remote_named(remotes, n).id for n in not_on}
    only_ids = None if only_on is None else {remote_named(remotes, n).id for n in only_on}

    f = search.build_filter(q, remote=in_remote or None, category=category, ext=ext, min_size=min_size,
                            max_size=max_size, modified_after=modified_after, modified_before=modified_before,
                            path_prefix=path_prefix)
    conds = [*f.conds, db.outside_quarantine(conn, "e.")]
    rank = ", bm25(entries_fts)" if f.fts else ", 0"
    hits = conn.execute(
        f"SELECT e.id, e.content_group, e.size{rank} {f.src} WHERE {' AND '.join(conds)} "
        f"ORDER BY {f.order} LIMIT ?", [*f.params, scan_cap + 1]).fetchall()
    truncated = len(hits) > scan_cap
    hits = hits[:scan_cap]

    # An empty file is a placeholder, not content: it stands alone instead of joining every other empty file.
    def key_of(entry_id: int, group: int | None, size: int | None) -> str:
        return f"g:{group}" if group is not None and size else f"e:{entry_id}"

    matched: set[int] = set()
    position: dict[int, int] = {}           # matching entry id -> index in hit order
    best_rank: dict[str, float] = {}
    group_ids: set[int] = set()
    single_ids: list[int] = []
    for entry_id, group, size, rk in hits:
        key = key_of(entry_id, group, size)
        matched.add(entry_id)
        position[entry_id] = len(position)
        best_rank[key] = min(best_rank.get(key, rk), rk)
        if key[0] == "g":
            group_ids.add(group)
        else:
            single_ids.append(entry_id)

    live = f"e.present = 1 AND e.is_dir = 0 AND {db.outside_quarantine(conn, 'e.')}"
    copies: dict[str, list[sqlite3.Row]] = {}
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    for part in chunks(sorted(group_ids)):
        for row in cur.execute(f"SELECT {_COLS} FROM entries e WHERE {live} AND e.size > 0 "
                               f"AND e.content_group IN ({','.join('?' * len(part))})", part):
            copies.setdefault(f"g:{row['content_group']}", []).append(row)
    for part in chunks(single_ids):
        for row in cur.execute(f"SELECT {_COLS} FROM entries e WHERE e.id IN ({','.join('?' * len(part))})", part):
            copies[f"e:{row['id']}"] = [row]

    groups: list[dict[str, Any]] = []
    for key, rows in copies.items():
        presence = {r["remote_id"] for r in rows}
        n = len(rows)
        if on_ids and not on_ids <= presence:
            continue
        if not_ids & presence:
            continue
        if only_ids is not None and presence != only_ids:
            continue
        if (copies_min is not None and n < copies_min) or (copies_max is not None and n > copies_max):
            continue
        rows.sort(key=lambda r: (order[r["remote_id"]], r["path"], r["id"]))
        # The name shown is the one most matching copies carry (the first hit breaks ties).
        hit_rows = sorted((r for r in rows if r["id"] in position), key=lambda r: position[r["id"]])
        tally: dict[str, int] = {}
        for r in hit_rows:
            tally[r["name"]] = tally.get(r["name"], 0) + 1
        top = max(tally.values())
        rep = next(r for r in hit_rows if tally[r["name"]] == top)
        groups.append({
            "key": key, "name": rep["name"], "size": rep["size"], "category": rep["category"], "ext": rep["ext"],
            "mtime": max((r["mtime"] for r in rows if r["mtime"]), default=None),
            "verified": key[0] == "g", "remotes": [remotes[i].name for i in sorted(order[p] for p in presence)],
            "copies_count": n, "_rows": rows, "_rank": best_rank[key], "_presence": presence,
        })

    # ---- facets over the filtered groups, before pagination
    per_remote = {r.id: 0 for r in remotes}
    buckets = {"1": 0, "2": 0, "3+": 0}
    cats: dict[str, int] = {}
    exts: dict[str, int] = {}
    for g in groups:
        for rid in g["_presence"]:
            per_remote[rid] += 1
        buckets["1" if g["copies_count"] == 1 else "2" if g["copies_count"] == 2 else "3+"] += 1
        cats[g["category"]] = cats.get(g["category"], 0) + 1
        if g["ext"]:
            exts[g["ext"]] = exts.get(g["ext"], 0) + 1
    facets = {
        "remotes": [{"name": r.name, "contents": per_remote[r.id]} for r in remotes],
        "copies": buckets,
        "categories": [{"category": c, "contents": n} for c, n in sorted(cats.items(), key=lambda t: (-t[1], t[0]))],
        "exts": [{"ext": e, "contents": n} for e, n in sorted(exts.items(), key=lambda t: (-t[1], t[0]))[:_TOP_EXTS]],
    }

    # ---- order, page
    tie = lambda g: (g["name"].casefold(), g["name"], g["key"])      # noqa: E731
    if sort == "size":
        groups.sort(key=lambda g: (-(g["size"] or 0), *tie(g)))
    elif sort == "mtime":
        groups.sort(key=tie)
        groups.sort(key=lambda g: g["mtime"] or "", reverse=True)       # stable: ties stay in name order
    elif sort == "copies":
        groups.sort(key=lambda g: (-g["copies_count"], *tie(g)))
    elif sort == "relevance" and f.fts:
        groups.sort(key=lambda g: (g["_rank"], *tie(g)))
    else:
        groups.sort(key=tie)

    results = []
    for g in groups[offset:offset + limit]:
        rows = g.pop("_rows")
        g.pop("_rank"), g.pop("_presence")
        g["copies"] = [{"entry_id": r["id"], "remote": remotes[order[r["remote_id"]]].name, "path": r["path"],
                        "name": r["name"], "mtime": r["mtime"], "matched": r["id"] in matched} for r in rows]
        results.append(g)
    return {"total": len(groups), "truncated": truncated, "facets": facets, "results": results}


__all__ = ["explore", "SORTS"]
