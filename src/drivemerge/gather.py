"""Gather: put one copy of each selected content into a single folder (docs/BULK.md §D).

A gather is *one* ordinary `move` action staged in the open workspace draft (`workspace.add_action`), so it has the
same checks, warnings and undo as a drag, and becomes a plan only through the workspace preview. Nothing here writes a
cloud or the catalog; `plan_gather` is read-only and `gather` writes only the draft.

Per content the plan picks one copy (the one already on the destination cloud, the keeper policy deciding among several;
with `cross_cloud`, the best copy elsewhere otherwise), never renames, and skips what cannot land cleanly, saying why
with a code and params for the page to word.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from . import builders, workspace
from .compare import chunks
from .explore import _KEY, _resolve_keys, key_order
from .workspace import WorkspaceError, _Cat, _Quarantine, _at, _fold, _name, _parent, _space_for

GATHER_LIMIT = 2000          # files picked by one gather
SKIP_CODES = ("ALREADY_THERE", "NAME_TAKEN", "NAME_CLASH", "NOT_ON_TARGET", "READ_ONLY", "NATIVE_DOC",
              "AMBIGUOUS_PATH", "GONE")
CODES = ("NOTHING_TO_GATHER", "GATHER_TOO_MANY")        # raised as workspace.WorkspaceError (409)
ANSWER_CAP = 200             # picked / skipped items sent back (the counts are exact)


def _native_ids(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    out: set[int] = set()
    for part in chunks(ids):
        out.update(r[0] for r in conn.execute(
            f"SELECT id FROM entries WHERE is_native_doc = 1 AND id IN ({','.join('?' * len(part))})", part))
    return out


def plan_gather(conn: sqlite3.Connection, draft_id: int, keys: Sequence[str], dst: dict, *,
                cross_cloud: bool = False, prefer_remotes: Sequence[str] = ()) -> dict[str, Any]:
    """What gathering `keys` into `dst` ({"remote", "path"}) would stage in the open draft. Read-only."""
    workspace._open_draft(conn, draft_id)
    rem = workspace._remotes(conn)
    if not isinstance(dst, dict) or not isinstance(dst.get("remote"), str) or not isinstance(dst.get("path", ""), str):
        raise WorkspaceError("BAD_REQUEST", "dst must be {remote, path}")
    target = workspace._remote_by_name(rem, dst["remote"])
    if target is None:
        raise WorkspaceError("NOT_FOUND", f"unknown remote {dst['remote']!r}", remote=dst["remote"])
    dpath = workspace._clean(dst.get("path"))
    dloc = (target.id, dpath)
    quarantine = _Quarantine(conn, rem)
    if quarantine.inside(*dloc):
        raise WorkspaceError("QUARANTINE", f"{target.name}:{dpath} is in the quarantine folder",
                             item=_at(rem, dloc))
    cat = _Cat(conn)
    space = _space_for(cat, workspace._acts(conn, draft_id, "active"))
    if dpath and "dir" not in space.kinds_at(*dloc):
        raise WorkspaceError("DST_NOT_DIR", f"{target.name}:{dpath} is not a folder", item=_at(rem, dloc))

    wanted = list(dict.fromkeys(keys))
    for k in wanted:
        if not isinstance(k, str) or not _KEY.match(k):
            raise WorkspaceError("BAD_REQUEST", f"{k!r} is not a content key", key=str(k))
    wanted.sort(key=key_order)
    contents = _resolve_keys(conn, wanted)
    all_rows = [r for rows in contents.values() for r in rows]
    where = workspace.locate(conn, draft_id, [(r["remote_id"], r["path"]) for r in all_rows])
    after = {r["id"]: loc for r, loc in zip(all_rows, where)}
    native = _native_ids(conn, [r["id"] for r in all_rows])
    ambiguous: dict[int, set[str]] = {}

    def is_ambiguous(row: sqlite3.Row) -> bool:
        rid = row["remote_id"]
        if rid not in ambiguous:
            ambiguous[rid] = builders._ambiguous_paths(conn, rid)
        return row["path"] in ambiguous[rid]

    def same_folder(a: str, b: str) -> bool:
        return _fold(a) == _fold(b) if target.ci else a == b

    children = {(_fold(_name(z)) if target.ci else _name(z)) for z in space.children(*dloc)}
    providers = {r.name: r.provider for r in rem.values()}
    taken: dict[str, dict] = {}               # folded target name -> the earlier pick's source
    picked: list[dict] = []
    skipped: list[dict] = []
    by_code: dict[str, int] = {}

    def skip(key: str, label: str | None, code: str, **params: Any) -> None:
        skipped.append({"key": key, "name": label, "code": code, "params": params})
        by_code[code] = by_code.get(code, 0) + 1

    for key in wanted:
        rows = contents.get(key)
        if not rows:
            skip(key, None, "GONE")
            continue
        rows = sorted(rows, key=lambda r: (r["remote_id"], r["path"], r["id"]))
        label = _name(after[rows[0]["id"]][1])
        there = next((r for r in rows if after[r["id"]][0] == target.id
                      and same_folder(_parent(after[r["id"]][1]), dpath)), None)
        if there is not None:
            skip(key, label, "ALREADY_THERE", name=_name(after[there["id"]][1]))
            continue
        cands = [r for r in rows if r["remote_id"] == target.id]
        if not cands:
            if not cross_cloud:
                skip(key, label, "NOT_ON_TARGET", remote=target.name)
                continue
            cands = [r for r in rows if r["id"] not in native]
            if not cands:
                skip(key, label, "NATIVE_DOC")
                continue
        if not target.writable:
            skip(key, label, "READ_ONLY", remote=target.name)
            continue
        usable = [r for r in cands if rem[r["remote_id"]].writable]
        if not usable:
            skip(key, label, "READ_ONLY", remote=rem[cands[0]["remote_id"]].name)
            continue
        clear = [r for r in usable if not is_ambiguous(r)]
        if not clear:
            skip(key, label, "AMBIGUOUS_PATH", remote=rem[usable[0]["remote_id"]].name, path=usable[0]["path"])
            continue
        copies = [{"remote": rem[r["remote_id"]].name, "remote_id": r["remote_id"], "path": after[r["id"]][1],
                   "mtime": r["mtime"], "entry_id": r["id"]} for r in clear]
        best = builders.choose_keeper(copies, target=target.name, prefer_remotes=prefer_remotes,
                                      providers=providers)
        row = next(r for r in clear if r["id"] == best["entry_id"])
        frm = after[row["id"]]
        name = _name(frm[1])
        folded = _fold(name) if target.ci else name
        if folded in children:
            skip(key, name, "NAME_TAKEN", name=name)
            continue
        if folded in taken:
            skip(key, name, "NAME_CLASH", name=name, **{"with": taken[folded]})
            continue
        source = {"remote": rem[frm[0]].name, "path": frm[1]}
        taken[folded] = source
        picked.append({"key": key, "from": source, "to": {"remote": target.name, "path": workspace._join(dpath, name)},
                       "size": row["size"] or 0, "cross_cloud": frm[0] != target.id})

    if len(picked) > GATHER_LIMIT:
        raise WorkspaceError("GATHER_TOO_MANY", f"{len(picked)} files is more than one gather moves "
                             f"({GATHER_LIMIT})", files=len(picked), limit=GATHER_LIMIT)
    crossing = [p for p in picked if p["cross_cloud"]]
    return {
        "contents": len(wanted), "files": len(picked), "bytes": sum(p["size"] for p in picked),
        "cross_cloud": {"files": len(crossing), "bytes": sum(p["size"] for p in crossing)},
        "picked": picked[:ANSWER_CAP],
        "skipped": {"by_code": by_code, "items": skipped[:ANSWER_CAP]},
        "items": [dict(p["from"]) for p in picked],
    }


def gather(conn: sqlite3.Connection, draft_id: int, keys: Sequence[str], dst: dict, *, cross_cloud: bool = False,
           prefer_remotes: Sequence[str] = (), note: str | None = None, created_by: str) -> dict[str, Any]:
    """Stage the plan of `plan_gather` as one move action of the open draft (one undo step)."""
    plan = plan_gather(conn, draft_id, keys, dst, cross_cloud=cross_cloud, prefer_remotes=prefer_remotes)
    if not plan["files"]:
        raise WorkspaceError("NOTHING_TO_GATHER", "nothing in the selection can be gathered there",
                             contents=plan["contents"], skipped=plan["skipped"]["by_code"])
    staged = workspace.add_action(conn, draft_id, {"kind": "move", "items": plan["items"],
                                                   "dst": {"remote": dst["remote"], "path": dst.get("path", "")},
                                                   "note": note or "explore:gather"}, created_by=created_by)
    return {**{k: v for k, v in plan.items() if k != "items"}, "action": staged["action"],
            "draft_version": staged["draft_version"], "warnings": staged["warnings"]}


__all__ = ["plan_gather", "gather", "GATHER_LIMIT", "SKIP_CODES", "CODES"]
