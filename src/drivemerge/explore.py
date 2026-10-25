"""Explore: where is it? Search results grouped by content, with one presence column per cloud.

Read-only (docs/EXPLORE.md §D). Hits are the live files `search.search` would find (the same WHERE builder),
minus the quarantine folder. Hits are grouped by content key; every group loads *all* its live copies, renamed
ones included, so a copy the query does not match is still listed under its content (`matched: false`). Presence
filters and copy counts judge the whole copy set, not only the hits.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, NamedTuple, Sequence

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


class _Found(NamedTuple):
    """Every filtered group of a query before sorting and paging (internal; rows are sqlite3.Row)."""
    groups: list[dict[str, Any]]
    remotes: list
    order: dict[int, int]
    filter: search.Filter
    matched: set[int]
    truncated: bool


def _filtered(conn: sqlite3.Connection, q: str | None, *, in_remote: str | None, category: str | None,
              ext: str | None, min_size: int | None, max_size: int | None, modified_after: str | None,
              modified_before: str | None, path_prefix: str | None, on: Sequence[str], not_on: Sequence[str],
              only_on: Sequence[str] | None, copies_min: int | None, copies_max: int | None,
              scan_cap: int) -> _Found:
    """The hit query, grouping and presence/copies filtering shared by `explore` and `select`."""
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
    return _Found(groups, remotes, order, f, matched, truncated)


def explore(conn: sqlite3.Connection, q: str | None = None, *, in_remote: str | None = None,
            category: str | None = None, ext: str | None = None, min_size: int | None = None,
            max_size: int | None = None, modified_after: str | None = None, modified_before: str | None = None,
            path_prefix: str | None = None, on: Sequence[str] = (), not_on: Sequence[str] = (),
            only_on: Sequence[str] | None = None, copies_min: int | None = None, copies_max: int | None = None,
            sort: str = "relevance", limit: int = 50, offset: int = 0, scan_cap: int = 20000,
            draft_locate: Callable[[list[tuple[int, str]]], list[tuple[int, str]]] | None = None) -> dict[str, Any]:
    """Group the live files matching `q` and the filters by content and answer with one result per content.

    `in_remote` restricts the *hits* to one cloud. `on` (every listed cloud holds a copy), `not_on` (none
    does), `only_on` (exactly these clouds) and `copies_min/max` (live copies) apply to the full copy set.
    At most `scan_cap` hits are read; `truncated` says there were more. Sorts: relevance (bm25 of the best
    matching copy when the query has a full-text term, else name), size desc, mtime desc, name, copies desc.

    `draft_locate` (given by the service) maps catalog locations (remote id, path) to where an open draft puts
    them; with it each listed copy gains `staged` ({remote, path}, or None when the draft does not move it) and
    each result `staged`, the number of its copies that move. Results, facets and presence still describe the catalog.
    """
    if sort not in SORTS:
        raise CompareError("BAD_QUERY", f"sort must be one of {', '.join(SORTS)}", param="sort")
    limit = min(_int(limit, "limit", lo=1) or 1, MAX_LIMIT)
    offset = _int(offset, "offset") or 0
    found = _filtered(conn, q, in_remote=in_remote, category=category, ext=ext, min_size=min_size,
                      max_size=max_size, modified_after=modified_after, modified_before=modified_before,
                      path_prefix=path_prefix, on=on, not_on=not_on, only_on=only_on, copies_min=copies_min,
                      copies_max=copies_max, scan_cap=scan_cap)
    groups, remotes, order, f = found.groups, found.remotes, found.order, found.filter
    matched, truncated = found.matched, found.truncated

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
    page_rows = []
    for g in groups[offset:offset + limit]:
        rows = g.pop("_rows")
        page_rows.append(rows)
        g.pop("_rank"), g.pop("_presence")
        g["copies"] = [{"entry_id": r["id"], "remote": remotes[order[r["remote_id"]]].name, "path": r["path"],
                        "name": r["name"], "mtime": r["mtime"], "matched": r["id"] in matched} for r in rows]
        results.append(g)
    if draft_locate is not None:
        flat = [(r["remote_id"], r["path"]) for g in page_rows for r in g]
        after = iter(draft_locate(flat)) if flat else iter(())
        for res, rows in zip(results, page_rows):
            res["staged"] = 0
            for copy, r in zip(res["copies"], rows):
                rid, path = next(after)
                moved = (rid, path) != (r["remote_id"], r["path"])
                copy["staged"] = {"remote": remotes[order[rid]].name, "path": path} if moved else None
                res["staged"] += moved
    return {"total": len(groups), "truncated": truncated, "facets": facets, "results": results}


# --------------------------------------------------------------------------- selections (docs/BULK.md §B)

PICK_LIMIT = 500        # hand-picked keys, and `except` keys
SELECT_LIMIT = 5000     # resolved contents of a selection
SELECTION_CODES = ("BAD_SELECTION", "BAD_KEY", "TOO_MANY_KEYS", "SELECTION_EMPTY", "SELECTION_TOO_LARGE",
                   "SELECTION_TRUNCATED", "SELECTION_CHANGED")
_KEY = re.compile(r"^(g|e):[1-9][0-9]{0,17}$")
_QUERY_KEYS = ("q", "in", "category", "ext", "min_size", "max_size", "modified_after", "modified_before",
               "path_prefix", "on", "not_on", "only_on", "copies_min", "copies_max")
_STR_PARAMS = ("q", "in", "category", "ext", "modified_after", "modified_before", "path_prefix")


class SelectionError(CompareError):
    """A selection the server refuses: `code` is one of SELECTION_CODES (or the NOT_FOUND / BAD_QUERY of its query)."""

    @property
    def status(self) -> int:
        return 404 if self.code == "NOT_FOUND" else 400 if self.code in (
            "BAD_QUERY", "BAD_SELECTION", "BAD_KEY", "TOO_MANY_KEYS") else 409


def key_order(key: str) -> tuple[int, int]:
    """Natural order of content keys: exact groups first, then single files, each by number."""
    return (0 if key[0] == "g" else 1, int(key[2:]))


def _keys(value: Any, param: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
        raise SelectionError("BAD_SELECTION", f"{param} must be a list of content keys", param=param)
    if len(value) > PICK_LIMIT:
        raise SelectionError("TOO_MANY_KEYS", f"{param} holds {len(value)} keys; the limit is {PICK_LIMIT}",
                             param=param, count=len(value), limit=PICK_LIMIT)
    return value


def _check_keys(keys: list[str]) -> None:
    for k in keys:
        if not _KEY.match(k):
            raise SelectionError("BAD_KEY", f"{k!r} is not a content key", key=k)


def _query_args(query: Any) -> dict[str, Any]:
    if not isinstance(query, dict):
        raise SelectionError("BAD_SELECTION", "query must be an object", param="query")
    for k in query:
        if k not in _QUERY_KEYS:
            raise SelectionError("BAD_SELECTION", f"unknown query parameter {k!r}", param=str(k))
    for k in _STR_PARAMS:
        if query.get(k) is not None and not isinstance(query[k], str):
            raise SelectionError("BAD_QUERY", f"{k} must be a string", param=k)
    lists = {}
    for k in ("on", "not_on", "only_on"):
        v = query.get(k)
        if v is not None and (not isinstance(v, list) or not all(isinstance(n, str) for n in v)):
            raise SelectionError("BAD_QUERY", f"{k} must be a list of cloud names", param=k)
        lists[k] = v
    return {
        "q": query.get("q") or None, "in_remote": query.get("in") or None, "category": query.get("category") or None,
        "ext": query.get("ext") or None, "min_size": query.get("min_size"), "max_size": query.get("max_size"),
        "modified_after": query.get("modified_after") or None, "modified_before": query.get("modified_before") or None,
        "path_prefix": query.get("path_prefix") or None, "on": lists["on"] or (), "not_on": lists["not_on"] or (),
        "only_on": lists["only_on"] or None, "copies_min": query.get("copies_min"),
        "copies_max": query.get("copies_max"),
    }


def _resolve_keys(conn: sqlite3.Connection, keys: list[str]) -> dict[str, list[sqlite3.Row]]:
    """Live copies of each content key, one query per chunk; a key with no copy is absent."""
    live = f"e.present = 1 AND e.is_dir = 0 AND {db.outside_quarantine(conn, 'e.')}"
    out: dict[str, list[sqlite3.Row]] = {}
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    gs = [int(k[2:]) for k in keys if k[0] == "g"]
    es = [int(k[2:]) for k in keys if k[0] == "e"]
    for part in chunks(gs):
        for row in cur.execute(f"SELECT {_COLS} FROM entries e WHERE {live} AND e.size > 0 "
                               f"AND e.content_group IN ({','.join('?' * len(part))})", part):
            out.setdefault(f"g:{row['content_group']}", []).append(row)
    for part in chunks(es):
        # An entry is its own content only when explore would key it so: empty, or without a content group.
        for row in cur.execute(f"SELECT {_COLS} FROM entries e WHERE {live} AND e.id IN ({','.join('?' * len(part))}) "
                               f"AND (e.content_group IS NULL OR e.size IS NULL OR e.size <= 0)", part):
            out[f"e:{row['id']}"] = [row]
    return out


def select(conn: sqlite3.Connection, selection: dict, *, limit: int = SELECT_LIMIT, with_keys: bool = False,
           scan_cap: int = 20000) -> dict[str, Any]:
    """Resolve a selection (docs/BULK.md §A) into what it holds. Read-only; see SELECTION_CODES for the refusals."""
    if not isinstance(selection, dict):
        raise SelectionError("BAD_SELECTION", "selection must be an object", param="selection")
    for k in selection:
        if k not in ("query", "keys", "except", "expect_total"):
            raise SelectionError("BAD_SELECTION", f"unknown selection field {k!r}", param=str(k))
    if ("query" in selection) == ("keys" in selection):
        raise SelectionError("BAD_SELECTION", "a selection has exactly one of query and keys",
                             param="keys" if "query" in selection else "query")
    mode = "query" if "query" in selection else "keys"
    if mode == "keys":
        for k in ("except", "expect_total"):
            if k in selection:
                raise SelectionError("BAD_SELECTION", f"{k} only goes with a query", param=k)
        picked = _keys(selection["keys"], "keys")
        except_keys: list[str] = []
        expect = None
    else:
        picked = []
        except_keys = _keys(selection["except"], "except") if "except" in selection else []
        expect = selection.get("expect_total")
        if "expect_total" in selection and (isinstance(expect, bool) or not isinstance(expect, int) or expect < 0):
            raise SelectionError("BAD_SELECTION", "expect_total must be an integer >= 0", param="expect_total")
    _check_keys(picked)
    _check_keys(except_keys)
    args = _query_args(selection["query"]) if mode == "query" else None

    gone = 0
    if mode == "query":
        found = _filtered(conn, **args, scan_cap=scan_cap)
        if found.truncated:
            raise SelectionError("SELECTION_TRUNCATED", f"the query matches more than {scan_cap} files; narrow it",
                                 scan_cap=scan_cap)
        skip = set(except_keys)
        contents = {g["key"]: g["_rows"] for g in found.groups if g["key"] not in skip}
        remotes, order = found.remotes, found.order
        if expect is not None and expect != len(contents):
            raise SelectionError("SELECTION_CHANGED", f"the selection held {expect} contents and now holds "
                                 f"{len(contents)}", expected=expect, total=len(contents))
    else:
        remotes = load_remotes(conn)
        order = {r.id: i for i, r in enumerate(remotes)}
        unique = list(dict.fromkeys(picked))
        contents = _resolve_keys(conn, unique)
        gone = len(unique) - len(contents)
    if len(contents) > limit:
        raise SelectionError("SELECTION_TOO_LARGE", f"the selection holds {len(contents)} contents; the limit is "
                             f"{limit}", total=len(contents), limit=limit)
    if not contents:
        raise SelectionError("SELECTION_EMPTY", "nothing in the selection exists any more")

    per = {r.id: {"contents": 0, "copies": 0, "bytes": 0} for r in remotes}
    copies = total_bytes = red_copies = red_bytes = 0
    for rows in contents.values():
        size = rows[0]["size"] or 0
        copies += len(rows)
        total_bytes += size
        red_copies += len(rows) - 1
        red_bytes += size * (len(rows) - 1)
        for rid in {r["remote_id"] for r in rows}:
            per[rid]["contents"] += 1
        for r in rows:
            per[r["remote_id"]]["copies"] += 1
            per[r["remote_id"]]["bytes"] += r["size"] or 0
    grouped = sum(1 for k in contents if k[0] == "g")
    out = {"mode": mode, "total": len(contents), "grouped": grouped, "ungrouped": len(contents) - grouped,
           "gone": gone, "copies": copies, "bytes": total_bytes,
           "by_remote": [{"remote": r.name, **per[r.id]} for r in remotes],
           "redundant": {"copies": red_copies, "bytes": red_bytes}, "staged": None}
    if with_keys:
        out["keys"] = sorted(contents, key=key_order)
    return out


__all__ = ["explore", "select", "SORTS", "SelectionError", "SELECTION_CODES", "PICK_LIMIT", "SELECT_LIMIT",
           "key_order"]
