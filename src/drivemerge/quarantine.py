"""What the executed plans parked in quarantine, and whether it can be restored or purged (docs/QUARANTINE.md).

Read-only: nothing here writes the catalog, and nothing here touches a cloud. The module answers three questions
from plan_ops, the journal and the catalog:

  quarantine_report   per cloud and per plan: how many files and bytes are held, how many are due
  plan_items          one plan's items, with their state and what the catalog knows about them
  check               what a restore or a purge of a selection would attempt, and what it would keep

An **item** is a `plan_ops` row of a plan that has run (`done` or `failed`) whose source the executor moved into
`<quarantine_dir>/<plan id>/<original path>`: a `done` delete, or a `done` move that met identical content (it has a
`quarantine` journal row). A restored item is a `skipped` op with a `restore` journal row whose status is
`restored`; a purged item has a `purge` journal row whose status is `purged`. Everything else in the quarantine
folder is *unknown*: listed, never actionable.

The catalog is as old as the last scan of each cloud, so every figure here is advice. The executor decides live.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from . import identity

CODES = ("NOT_FOUND", "NOTHING_QUARANTINED", "BAD_OPS", "BAD_QUERY")
STATES = ("held", "restored", "purged", "all")
ACTIONS = ("restore", "purge")
MAX_LIMIT = 500
MAX_BAD_OPS = 20                 # offending op ids a BAD_OPS error lists
RECENT_DAYS = 7                  # a purge of something quarantined this recently gets a warning
EXAMPLES = 3                     # unknown files listed per cloud
_BATCH = 400                     # variables per IN (...) list
_TS = "%Y-%m-%dT%H:%M:%S.%fZ"    # db.utcnow(): fixed width, so strings compare chronologically
_STRONG = tuple(identity.STRONG_TYPES)
_HASH_COLUMNS = ("md5", "sha1", "sha256", "dropbox", "quickxor")     # the entries columns of a hash


class QuarantineError(ValueError):
    """A refusal with a machine-readable `code` and the `params` a page words it with."""

    def __init__(self, code: str, message: str, **params: object):
        super().__init__(message)
        self.code = code
        self.params = params


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_TS)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _qdir(quarantine_dir: str) -> str:
    return (quarantine_dir or "").strip("/")


def _qpath(qdir: str, plan_id: int, path: str) -> str:
    return f"{qdir}/{plan_id}/{path}"


def _fold(path: str, ci: bool) -> str:
    path = unicodedata.normalize("NFC", path)
    return path.casefold() if ci else path


def _natural(text: str) -> tuple:
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", text) if p)


def _path_key(path: str) -> tuple:
    """Natural, case-insensitive order that keeps a folder's files together."""
    return tuple(_natural(unicodedata.normalize("NFC", seg).casefold()) for seg in path.split("/")), path


def _chunks(seq: Sequence, size: int = _BATCH) -> Iterable[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _bad_query(param: str, message: str) -> QuarantineError:
    return QuarantineError("BAD_QUERY", message, param=param)


def _remotes(conn: sqlite3.Connection) -> tuple[dict[int, str], dict[int, bool]]:
    rows = conn.execute("SELECT id, name, case_insensitive FROM remotes ORDER BY id").fetchall()
    return {r[0]: r[1] for r in rows}, {r[0]: bool(r[2]) for r in rows}


# --------------------------------------------------------------------------- #
# The items: one SQL statement with the journal summarised in one pass
# --------------------------------------------------------------------------- #

def _json_status(action: str, status: str) -> str:
    return (f"MAX(CASE WHEN action = '{action}' AND json_valid(detail) "
            f"AND json_extract(detail, '$.status') = '{status}' THEN ts END)")


def _items_sql(*, plan: bool = False, op_ids: int = 0) -> str:
    """`WITH ... SELECT` over every item (one row per quarantined op), optionally of one plan (`?`) and/or of `op_ids` ops."""
    jplan = " AND plan_id = ?" if plan else ""
    oplan = " AND o.plan_id = ?" if plan else ""
    oids = f" AND o.id IN ({','.join('?' * op_ids)})" if op_ids else ""
    return f"""
WITH j AS (
    SELECT op_id,
           MAX(CASE WHEN action = 'quarantine' THEN ts END) AS q_ts,
           {_json_status('restore', 'restored')} AS r_ts,
           {_json_status('purge', 'purged')} AS p_ts
    FROM journal
    WHERE op_id IS NOT NULL AND action IN ('quarantine', 'restore', 'purge'){jplan}
    GROUP BY op_id
), items AS (
    SELECT o.id AS op_id, o.plan_id, o.seq, o.op, o.src_remote_id AS rid, o.src_path AS path,
           COALESCE(o.expect_size, 0) AS size, o.expect_hash, o.reason, o.dst_remote_id, o.dst_path,
           o.witness_remote_id, o.witness_path,
           CASE WHEN o.status = 'skipped' AND j.r_ts IS NOT NULL THEN 'restored'
                WHEN j.p_ts IS NOT NULL THEN 'purged' ELSE 'held' END AS state,
           CASE WHEN o.status = 'skipped' THEN COALESCE(j.q_ts, o.executed_at)
                ELSE COALESCE(o.executed_at, j.q_ts) END AS qat,
           j.r_ts AS restored_at, j.p_ts AS purged_at
    FROM plan_ops o
    JOIN plans p ON p.id = o.plan_id
    LEFT JOIN j ON j.op_id = o.id
    WHERE p.status IN ('done', 'failed'){oplan}{oids}
      AND ((o.status = 'done' AND (o.op = 'delete' OR (o.op = 'move' AND j.q_ts IS NOT NULL)))
           OR (o.status = 'skipped' AND j.r_ts IS NOT NULL))
)
"""


def _plan_row(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT id, title, kind, status, created_by, params, finished_at FROM plans WHERE id = ?",
                       (plan_id,)).fetchone()
    if row is None:
        raise QuarantineError("NOT_FOUND", f"no plan {plan_id}", plan=plan_id)
    return row


def _scrubbed_params(text: str | None) -> dict:
    from .service import scrub_secrets          # service imports this module
    try:
        params = json.loads(text or "{}")
    except ValueError:
        return {}
    return scrub_secrets(params) if isinstance(params, dict) else {}


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

def quarantine_report(conn: sqlite3.Connection, *, quarantine_dir: str, retention_days: int = 30,
                      now: datetime | None = None) -> dict:
    """Held files per cloud and per plan, what is due, and the files nothing accounts for. Read-only."""
    now = _now(now)
    cutoff = _stamp(now - timedelta(days=retention_days))
    qdir = _qdir(quarantine_dir)
    names, ci = _remotes(conn)

    # (plan, remote, state, due?) -> files, bytes, oldest, earliest not-yet-due quarantine time: one pass.
    agg = conn.execute(_items_sql() + """
        SELECT plan_id, rid, state, (qat IS NOT NULL AND qat <= ?) AS due, COUNT(*), SUM(size), MIN(qat)
        FROM items GROUP BY plan_id, rid, state, due""", (cutoff,)).fetchall()

    plans_info: dict[int, dict] = {}
    remotes_acc = {rid: {"remote": name, "files": 0, "bytes": 0, "due_files": 0, "due_bytes": 0, "plans": set()}
                   for rid, name in names.items()}
    totals = {"files": 0, "bytes": 0, "due_files": 0, "due_bytes": 0}
    for plan_id, rid, state, due, files, size, oldest in agg:
        p = plans_info.setdefault(plan_id, {
            "held": {"files": 0, "bytes": 0}, "due": {"files": 0, "bytes": 0}, "restored": 0, "purged": 0,
            "oldest": None, "next": None, "remotes": set()})
        p["remotes"].add(rid)
        if state == "restored":
            p["restored"] += files
        elif state == "purged":
            p["purged"] += files
        else:
            p["held"]["files"] += files
            p["held"]["bytes"] += size
            if oldest is not None and (p["oldest"] is None or oldest < p["oldest"]):
                p["oldest"] = oldest
            r = remotes_acc[rid]
            r["files"] += files
            r["bytes"] += size
            r["plans"].add(plan_id)
            totals["files"] += files
            totals["bytes"] += size
            if due:
                p["due"]["files"] += files
                p["due"]["bytes"] += size
                r["due_files"] += files
                r["due_bytes"] += size
                totals["due_files"] += files
                totals["due_bytes"] += size
            elif oldest is not None and (p["next"] is None or oldest < p["next"]):
                p["next"] = oldest

    plan_rows = {}
    if plans_info:
        for row in conn.execute(
                f"SELECT id, title, kind, status, created_by, params, finished_at FROM plans "
                f"WHERE id IN ({','.join('?' * len(plans_info))})", list(plans_info)):
            plan_rows[row[0]] = row

    def due_at(ts: str | None) -> str | None:
        when = _parse(ts)
        return _stamp(when + timedelta(days=retention_days)) if when else None

    plans_out = []
    for plan_id, info in plans_info.items():
        row = plan_rows[plan_id]
        held = info["held"]["files"] > 0
        plans_out.append({
            "plan_id": plan_id, "title": row["title"], "kind": row["kind"], "status": row["status"],
            "created_by": row["created_by"], "params": _scrubbed_params(row["params"]),
            "finished_at": row["finished_at"], "remotes": [names[r] for r in sorted(info["remotes"])],
            "held": info["held"], "due": info["due"], "restored": info["restored"], "purged": info["purged"],
            "oldest_at": info["oldest"] if held else None,
            "next_due_at": due_at(info["next"]) if held else None,
        })
    plans_out.sort(key=lambda p: (0, p["oldest_at"] or "", p["plan_id"]) if p["held"]["files"]
                   else (1, _desc(p["finished_at"]), p["plan_id"]))

    scans = {r[0]: {"id": r[1], "finished_at": r[2]} for r in conn.execute(
        """SELECT s.remote_id, s.id, s.finished_at FROM scans s
           WHERE s.status = 'complete' AND s.id = (SELECT s2.id FROM scans s2 WHERE s2.remote_id = s.remote_id
                                                   AND s2.status = 'complete'
                                                   ORDER BY s2.finished_at DESC, s2.id DESC LIMIT 1)""")}
    remotes_out = [{"remote": r["remote"], "files": r["files"], "bytes": r["bytes"], "due_files": r["due_files"],
                    "due_bytes": r["due_bytes"], "plans": len(r["plans"]), "last_scan": scans.get(rid)}
                   for rid, r in remotes_acc.items()]
    return {
        "quarantine_dir": qdir, "retention_days": retention_days, "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": totals, "remotes": remotes_out, "plans": plans_out,
        "unknown": _unknown(conn, qdir, names, ci),
    }


def _desc(ts: str | None) -> str:
    """A sort key that puts later timestamps first (and missing ones last) when sorted ascending."""
    if not ts:
        return "￿"
    return "".join(chr(0x10FFFF - ord(c)) for c in ts)


def _unknown(conn: sqlite3.Connection, qdir: str, names: dict[int, str], ci: dict[int, bool]) -> list[dict]:
    """Live files in the quarantine folder that no held item accounts for, per cloud."""
    if not qdir:
        return []
    low, high = qdir + "/", qdir + "0"          # '0' follows '/': every path below the folder, by index range
    live = conn.execute(
        "SELECT remote_id, path, COALESCE(size, 0) FROM entries "
        "WHERE present = 1 AND is_dir = 0 AND path >= ? AND path < ? ORDER BY remote_id, path", (low, high)).fetchall()
    if not live:
        return []
    accounted: dict[int, set[str]] = defaultdict(set)
    for rid, plan_id, path in conn.execute(_items_sql() + "SELECT rid, plan_id, path FROM items WHERE state = 'held'"):
        if rid in ci:
            accounted[rid].add(_fold(_qpath(qdir, plan_id, path), ci[rid]))
    found: dict[int, dict] = {}
    for rid, path, size in live:
        if rid not in ci or _fold(path, ci[rid]) in accounted[rid]:
            continue
        u = found.setdefault(rid, {"remote": names[rid], "files": 0, "bytes": 0, "examples": []})
        u["files"] += 1
        u["bytes"] += size
        if len(u["examples"]) < EXAMPLES:
            u["examples"].append(path)
    return [found[rid] for rid in names if rid in found]


def due_items(conn: sqlite3.Connection, *, quarantine_dir: str, retention_days: int = 30, remote: str | None = None,
              now: datetime | None = None) -> dict[int, list[int]]:
    """{plan id: op ids of its held items whose retention has passed}, oldest plan first (on `remote` when given).

    What `Service.purge_due` walks. An item with no known quarantine time is never due.
    """
    names, _ = _remotes(conn)
    where, args = ["state = 'held'", "qat IS NOT NULL", "qat <= ?"], [_stamp(_now(now) - timedelta(days=retention_days))]
    if remote:
        rid = next((r for r, name in names.items() if name == remote), None)
        if rid is None:
            raise _bad_query("remote", f"unknown cloud {remote!r}")
        where.append("rid = ?")
        args.append(rid)
    out: dict[int, list[int]] = {}
    for plan_id, op_id in conn.execute(_items_sql() + f"SELECT plan_id, op_id FROM items WHERE {' AND '.join(where)} "
                                       "ORDER BY plan_id, op_id", args):
        out.setdefault(plan_id, []).append(op_id)
    oldest = {p: r for p, r in ((row[0], row[1]) for row in conn.execute(
        _items_sql() + "SELECT plan_id, MIN(qat) FROM items WHERE state = 'held' GROUP BY plan_id"))}
    return {p: out[p] for p in sorted(out, key=lambda p: (oldest.get(p) or "", p))}


# --------------------------------------------------------------------------- #
# What the catalog knows about a page of items
# --------------------------------------------------------------------------- #

def _live_files(conn: sqlite3.Connection, ci: dict[int, bool],
                wanted: Iterable[tuple[int, str]]) -> dict[tuple[int, str], sqlite3.Row]:
    """{(remote id, folded path): the live catalog file} for the wanted locations, with few queries.

    Exact paths are looked up by index, in batches. A location with no exact hit can still be spelled otherwise in
    the catalog (another case on a case-insensitive cloud, NFD from macOS or iCloud), so those are looked up
    again by `name_norm`, which is NFC and casefolded, and compared by the cloud's own rule.
    """
    by_remote: dict[int, set[str]] = defaultdict(set)
    for rid, path in wanted:
        by_remote[rid].add(path)
    out: dict[tuple[int, str], sqlite3.Row] = {}
    cols = "id, remote_id, path, size, content_group, name_norm, " + ", ".join(_HASH_COLUMNS)
    for rid, paths in by_remote.items():
        rule = ci.get(rid, False)
        listed = sorted(paths)
        for chunk in _chunks(listed):
            for row in conn.execute(
                    f"SELECT {cols} FROM entries WHERE remote_id = ? AND present = 1 AND is_dir = 0 "
                    f"AND path IN ({','.join('?' * len(chunk))})", (rid, *chunk)):
                out.setdefault((rid, _fold(row["path"], rule)), row)
        # Only a path that the catalog could spell differently needs the second look.
        retry = [p for p in listed if (rid, _fold(p, rule)) not in out
                 and (rule or not p.isascii())]
        norms = sorted({unicodedata.normalize("NFC", p.rsplit("/", 1)[-1]).casefold() for p in retry})
        wanted_keys = {_fold(p, rule) for p in retry}
        for chunk in _chunks(norms):
            for row in conn.execute(
                    f"SELECT {cols} FROM entries WHERE remote_id = ? AND present = 1 AND is_dir = 0 "
                    f"AND name_norm IN ({','.join('?' * len(chunk))})", (rid, *chunk)):
                key = _fold(row["path"], rule)
                if key in wanted_keys:
                    out.setdefault((rid, key), row)
    return out


def _same_content(a: sqlite3.Row | None, b: sqlite3.Row | None) -> bool | None:
    """Do two catalog files hold the same bytes? None when the catalog cannot say."""
    if a is None or b is None:
        return None
    if a["content_group"] is not None and a["content_group"] == b["content_group"]:
        return True
    for t in _STRONG:
        if a[t] and b[t]:
            return a[t] == b[t]
    if a["content_group"] is not None and b["content_group"] is not None:
        return False
    return None


def _same_as_expected(expect_hash: str | None, witness: sqlite3.Row) -> bool | None:
    """The witness against the hash the op recorded for its source (used when the quarantined file is not in the catalog)."""
    kind, _, want = (expect_hash or "").partition(":")
    kind, want = kind.lower(), want.lower()
    if kind not in _HASH_COLUMNS or not want or not witness[kind]:
        return None
    if witness[kind].lower() != want:
        return False
    return True if kind in _STRONG else None


def _flags(conn: sqlite3.Connection, items: list[dict], qdir: str, ci: dict[int, bool]) -> None:
    """Fill `catalog` of every item, with one batched look-up of the catalog for the whole list."""
    wanted = {(i["rid"], i["qpath"]) for i in items} | {(i["rid"], i["path"]) for i in items}
    wanted |= {(i["wit_rid"], i["wit_path"]) for i in items if i["wit_rid"] is not None and i["wit_path"]}
    live = _live_files(conn, ci, wanted)
    for i in items:
        rule = ci.get(i["rid"], False)
        held = live.get((i["rid"], _fold(i["qpath"], rule)))
        taken = live.get((i["rid"], _fold(i["path"], rule)))
        witness_live: bool | None = None
        witness_same: bool | None = None
        if i["wit_rid"] is not None and i["wit_path"]:
            wit = live.get((i["wit_rid"], _fold(i["wit_path"], ci.get(i["wit_rid"], False))))
            witness_live = wit is not None
            if wit is not None:
                witness_same = _same_content(held, wit) if held is not None else _same_as_expected(i["expect_hash"], wit)
        i["catalog"] = {"seen": held is not None, "original_taken": taken is not None,
                        "witness_live": witness_live, "witness_same": witness_same}


# --------------------------------------------------------------------------- #
# One plan's items
# --------------------------------------------------------------------------- #

def _item_dict(row: sqlite3.Row, names: dict[int, str], qdir: str, retention_days: int, now_s: str) -> dict:
    if row["op"] == "delete":
        wit_rid, wit_path = row["witness_remote_id"], row["witness_path"]
    else:                                   # a move that met identical content: the destination vouches
        wit_rid, wit_path = row["dst_remote_id"], row["dst_path"]
    qat = row["qat"]
    due_dt = _parse(qat)
    due_dt = due_dt + timedelta(days=retention_days) if due_dt else None
    return {
        "op_id": row["op_id"], "seq": row["seq"], "op": row["op"], "remote": names[row["rid"]], "path": row["path"],
        "quarantine_path": _qpath(qdir, row["plan_id"], row["path"]), "size": row["size"], "reason": row["reason"],
        "witness": {"remote": names.get(wit_rid, f"#{wit_rid}"), "path": wit_path}
        if wit_rid is not None and wit_path else None,
        "state": row["state"], "quarantined_at": qat, "due_at": _stamp(due_dt) if due_dt else None,
        "due": bool(due_dt and _stamp(due_dt) <= now_s),
        "restored_at": row["restored_at"] if row["state"] == "restored" else None,
        "purged_at": row["purged_at"] if row["state"] == "purged" else None,
        "catalog": None,
        # kept for the catalog look-up, removed before the item leaves this module
        "rid": row["rid"], "qpath": _qpath(qdir, row["plan_id"], row["path"]), "wit_rid": wit_rid,
        "wit_path": wit_path, "expect_hash": row["expect_hash"],
    }


_PRIVATE = ("rid", "qpath", "wit_rid", "wit_path", "expect_hash")


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in _PRIVATE}


def plan_items(conn: sqlite3.Connection, plan_id: int, *, quarantine_dir: str, retention_days: int = 30,
               state: str = "held", remote: str | None = None, path_prefix: str | None = None,
               offset: int = 0, limit: int = 200, now: datetime | None = None) -> dict:
    """One page of a plan's items (default: those still held), sorted by cloud, then natural original path.

    `remote` and `path_prefix` (a whole-segment prefix of the original path) filter. A plan with nothing
    quarantined lists nothing; only `check` refuses it. Read-only.
    """
    now = _now(now)
    if state not in STATES:
        raise _bad_query("state", f"state must be one of {', '.join(STATES)}")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise _bad_query("limit", f"limit must be between 1 and {MAX_LIMIT}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise _bad_query("offset", "offset must not be negative")
    plan = _plan_row(conn, plan_id)
    names, ci = _remotes(conn)
    qdir = _qdir(quarantine_dir)
    rid_filter = None
    if remote:
        rid_filter = next((rid for rid, name in names.items() if name == remote), None)
        if rid_filter is None:
            raise _bad_query("remote", f"unknown cloud {remote!r}")
    prefix = (path_prefix or "").strip("/")

    where, args = [], [plan_id, plan_id]
    if state != "all":
        where.append("state = ?")
        args.append(state)
    if rid_filter is not None:
        where.append("rid = ?")
        args.append(rid_filter)
    if prefix:
        where.append("(path = ? OR substr(path, 1, ?) = ?)")
        args += [prefix, len(prefix) + 1, prefix + "/"]
    rows = conn.execute(
        _items_sql(plan=True) + "SELECT op_id, rid, path, size FROM items"
        + (" WHERE " + " AND ".join(where) if where else ""), args).fetchall()
    rows.sort(key=lambda r: (r["rid"], _path_key(r["path"]), r["op_id"]))
    page_ids = [r["op_id"] for r in rows[offset:offset + limit]]

    items: list[dict] = []
    if page_ids:
        order = {op_id: n for n, op_id in enumerate(page_ids)}
        detail = conn.execute(_items_sql(plan=True, op_ids=len(page_ids)) + "SELECT * FROM items",
                              [plan_id, plan_id, *page_ids]).fetchall()
        now_s = _stamp(now)
        items = [_item_dict(r, names, qdir, retention_days, now_s) for r in sorted(detail, key=lambda r: order[r["op_id"]])]
        _flags(conn, items, qdir, ci)
    return {
        "plan": {"id": plan["id"], "title": plan["title"], "kind": plan["kind"], "status": plan["status"],
                 "created_by": plan["created_by"], "finished_at": plan["finished_at"]},
        "total": len(rows), "bytes": sum(r["size"] for r in rows), "offset": offset, "limit": limit,
        "items": [_public(i) for i in items],
    }


# --------------------------------------------------------------------------- #
# The check before a restore or a purge
# --------------------------------------------------------------------------- #

def _held_items(conn: sqlite3.Connection, plan_id: int) -> dict[int, sqlite3.Row]:
    """The plan's held items by op id."""
    sql = _items_sql(plan=True) + "SELECT * FROM items WHERE state = 'held' ORDER BY rid, op_id"
    return {r["op_id"]: r for r in conn.execute(sql, [plan_id, plan_id])}


def _tally(items: Iterable[dict]) -> dict:
    files = bytes_ = 0
    for i in items:
        files += 1
        bytes_ += i["size"]
    return {"files": files, "bytes": bytes_}


def _resolve_ops(plan_id: int, held: dict[int, sqlite3.Row], op_ids: Sequence[int] | None) -> list[sqlite3.Row]:
    if op_ids is None:
        return list(held.values())
    wanted = list(dict.fromkeys(op_ids))
    bad = [o for o in wanted if isinstance(o, bool) or not isinstance(o, int) or o not in held]
    if bad or not wanted:
        raise QuarantineError("BAD_OPS", f"plan {plan_id}: these are not held items of the plan: "
                                         f"{bad[:MAX_BAD_OPS] or 'an empty selection'}",
                              plan=plan_id, op_ids=bad[:MAX_BAD_OPS])
    return [held[o] for o in wanted]


def check(conn: sqlite3.Connection, plan_id: int, action: str, *, quarantine_dir: str,
          op_ids: Sequence[int] | None = None, older_than_days: int = 0, retention_days: int = 30,
          now: datetime | None = None) -> dict:
    """What a restore or purge of these items (`op_ids`, None = every held item) would attempt and keep.

    Read-only advice from the catalog; the executor looks again, live, at everything. Raises
    NOT_FOUND, NOTHING_QUARANTINED (no held item at all), BAD_OPS (an id that is not a held item of the plan,
    or an empty list) and BAD_QUERY (`action`, a negative `older_than_days`).
    """
    now = _now(now)
    plan = _plan_row(conn, plan_id)
    if action not in ACTIONS:
        raise _bad_query("action", "action must be 'restore' or 'purge'")
    if not isinstance(older_than_days, int) or isinstance(older_than_days, bool) or older_than_days < 0:
        raise _bad_query("older_than_days", "older_than_days must not be negative")
    names, ci = _remotes(conn)
    qdir = _qdir(quarantine_dir)
    held = _held_items(conn, plan_id)
    if not held:
        raise QuarantineError("NOTHING_QUARANTINED", f"plan {plan['id']} has nothing in quarantine", plan=plan_id)
    chosen = _resolve_ops(plan_id, held, op_ids)
    now_s = _stamp(now)
    items = [_item_dict(r, names, qdir, retention_days, now_s) for r in chosen]
    for batch in _chunks(items, 2000):
        _flags(conn, list(batch), qdir, ci)

    if action == "restore":
        return {
            "action": "restore", "plan_id": plan_id, **_tally(items),
            "original_taken": _tally(i for i in items if i["catalog"]["original_taken"]),
            "not_seen": _tally(i for i in items if not i["catalog"]["seen"]),
            "cross_remote_copies": _cross_remote_copies(conn, plan_id, [i["op_id"] for i in items]),
            "remotes": _remote_names(names, items), "op_ids": [i["op_id"] for i in items],
        }

    cutoff = _stamp(now - timedelta(days=older_than_days))
    recent_cutoff = _stamp(now - timedelta(days=RECENT_DAYS))
    rows = {r["op_id"]: r for r in chosen}
    attempted, too_recent = [], []
    for i in items:
        (attempted if rows[i["op_id"]]["qat"] is not None and rows[i["op_id"]]["qat"] <= cutoff else too_recent).append(i)
    gone, unproven = [], []
    for i in attempted:
        c = i["catalog"]
        if c["witness_live"] is not True:            # not in the catalog, or the op names none: it will be kept
            gone.append(i)
        elif c["witness_same"] is not True:
            unproven.append(i)
    recent = [i for i in attempted if rows[i["op_id"]]["qat"] > recent_cutoff]
    by_remote = []
    for rid, name in names.items():
        part = [i for i in attempted if i["rid"] == rid]
        if part:
            by_remote.append({"remote": name, **_tally(part)})
    return {
        "action": "purge", "plan_id": plan_id, **_tally(attempted), "too_recent": _tally(too_recent),
        "witness_gone": _tally(gone),
        "witness_unproven": {**_tally(unproven),
                             "download_bytes": sum(i["size"] for i in unproven if i["catalog"]["witness_same"] is None)},
        "recent": _tally(recent), "by_remote": by_remote, "op_ids": [i["op_id"] for i in attempted],
    }


def _remote_names(names: dict[int, str], items: list[dict]) -> list[str]:
    present = {i["rid"] for i in items}
    return [name for rid, name in names.items() if rid in present]


def _cross_remote_copies(conn: sqlite3.Connection, plan_id: int, op_ids: list[int]) -> int:
    """Items whose plan also copied the very same file to another cloud: after a restore the content exists twice."""
    total = 0
    for batch in _chunks(op_ids):
        total += conn.execute(
            f"""SELECT COUNT(*) FROM plan_ops o WHERE o.plan_id = ? AND o.id IN ({','.join('?' * len(batch))})
                AND EXISTS (SELECT 1 FROM plan_ops c WHERE c.plan_id = o.plan_id AND c.op = 'copy'
                            AND c.status IN ('done', 'skipped') AND c.src_remote_id = o.src_remote_id
                            AND c.src_path = o.src_path AND c.dst_remote_id IS NOT NULL
                            AND c.dst_remote_id <> o.src_remote_id)""", (plan_id, *batch)).fetchone()[0]
    return total


__all__ = ["CODES", "QuarantineError", "quarantine_report", "plan_items", "check", "due_items"]
