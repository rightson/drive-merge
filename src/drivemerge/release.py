"""Free space on one cloud: which of its files are provably safe elsewhere (docs/ONBOARDING.md §6, contract §C).

Read-only. The report sorts every live file of a cloud (optionally a folder of it) into buckets:

proven     the catalog's content group (strong hash or bridge, DESIGN.md §5) has a live copy on another cloud,
           outside quarantine, not at an ambiguous path, and on one of `keep_on` when that is given
probable   no proof, but another cloud holds a file of the same size and name, or of the same size and an equal
           MD5/SHA-1/QuickXorHash (and no hash of the two contradicts); verification can settle it
only_here  nothing anywhere else: freeing it would lose it
unjudged   native documents, objects without a size, files at an ambiguous Drive path (nothing can address them)

Extra bucket `elsewhere_unusable`: the content does live on another cloud, but no copy can serve as a witness
(only outside `keep_on`, or only at an ambiguous path). Such a file is neither proven nor "only here".
A file is counted once, per file: duplicates inside the chosen cloud are not merged (so `only_here` can exceed
coverage.coverage()'s distinct-content figure for the same cloud when the cloud holds identical files twice).
`build_release` turns the proven bucket into a plan of quarantine deletes, each naming a witness.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from . import builders, db, identity
from .builders import BuildError, BuildResult

PROBABLE_ID_CAP = 5000
_COLS = ("md5", "sha1", "sha256", "dropbox", "quickxor")      # providers.HASH_TYPES order is irrelevant here
_STRONG = frozenset(identity.STRONG_TYPES)
_WEAK_IDX = tuple(i for i, t in enumerate(_COLS) if t not in _STRONG)


def _outside(quarantine: str, alias: str) -> str:
    q = quarantine.strip("/")
    if not q:
        return "1"
    lit = q.replace("'", "''")
    return f"NOT ({alias}path = '{lit}' OR substr({alias}path, 1, {len(q) + 1}) = '{lit}/')"


def _contradict(a: tuple, b: tuple) -> bool:
    """Two files that report different values of one hash type are different bytes."""
    return any(x and y and x.lower() != y.lower() for x, y in zip(a, b))


class _Bridged:
    """Every hash an entry is known to have: its own columns, completed from the hash_bridges row of a download
    of these very bytes (found by the entry, or by an equal strong hash). Lets a verified file contradict a
    candidate whose provider reports a hash the file's own provider does not (Dropbox vs Drive's SHA-256)."""

    def __init__(self, conn: sqlite3.Connection):
        self.by_entry: dict[int, tuple] = {}
        self.by_strong: dict[tuple[int, str], tuple] = {}
        rows = {r[0]: tuple(r[2:7]) + (r[1],) for r in conn.execute(
            "SELECT id, size, md5, sha1, sha256, dropbox, quickxor FROM hash_bridges")}
        for bid, row in rows.items():
            for i, t in enumerate(_COLS):
                if t in _STRONG and row[i]:
                    self.by_strong[(i, row[i].lower())] = row
        for bid, eid, size in conn.execute("SELECT bridge_id, entry_id, size FROM bridge_sources"):
            row = rows.get(bid)
            if row and row[5] == size:
                self.by_entry[eid] = row

    def hashes(self, entry_id: int, size: int, own: tuple) -> tuple:
        row = self.by_entry.get(entry_id)
        if row is None:
            for i, v in enumerate(own):
                if v and _COLS[i] in _STRONG:
                    row = self.by_strong.get((i, v.lower()))
                    if row:
                        break
        if row is None or row[5] != size:
            return own
        return tuple(o or b for o, b in zip(own, row[:5]))


def _ambiguous(conn: sqlite3.Connection, rid: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT path FROM entries WHERE remote_id = ? AND present = 1 GROUP BY path HAVING COUNT(*) > 1", (rid,))}


def _analyze(conn: sqlite3.Connection, remote: str, path_prefix: str | None, keep_on: list[str] | None,
             quarantine: str | None = None) -> dict[str, Any]:
    remotes = builders._remote_map(conn)
    me = builders._named(remotes, remote)
    keep = [r.name for r in builders._names(remotes, keep_on) if r.id != me.id]
    if keep_on and not keep:
        # keep_on named only the cloud itself: nothing else may vouch for anything
        allowed: set[int] = set()
    else:
        allowed = {r.id for r in remotes.values() if r.id != me.id and (not keep or r.name in keep)}
    prefix = builders._clean_prefix(path_prefix)
    q = db.quarantine_dir(conn) if quarantine is None else quarantine
    live_me = f"e.present = 1 AND e.is_dir = 0 AND {_outside(q, 'e.')}"
    live_other = f"e.present = 1 AND e.is_dir = 0 AND e.is_native_doc = 0 AND e.size IS NOT NULL AND {_outside(q, 'e.')}"
    scope, sargs = ("", ()) if not prefix else (
        " AND (e.path = ? OR (e.path >= ? AND e.path < ?))", (prefix, prefix + "/", prefix + "0"))

    ambiguous_me = _ambiguous(conn, me.id)
    targets = conn.execute(
        "SELECT e.id, e.path, e.name_norm, e.size, e.content_group, e.is_native_doc, "
        "e.md5, e.sha1, e.sha256, e.dropbox, e.quickxor FROM entries e "
        f"WHERE e.remote_id = ? AND {live_me}{scope} ORDER BY e.path, e.id", (me.id, *sargs)).fetchall()

    unjudged: list[tuple[int, str, int | None, str]] = []   # (id, path, size, code)
    judged = []
    for t in targets:
        if t[5]:
            unjudged.append((t[0], t[1], t[3], "NATIVE_DOC"))
        elif t[3] is None:
            unjudged.append((t[0], t[1], None, "SIZE_UNKNOWN"))
        elif t[1] in ambiguous_me:
            unjudged.append((t[0], t[1], t[3], "AMBIGUOUS_PATH"))
        else:
            judged.append(t)

    groups = {t[4] for t in judged if t[4] is not None}
    namekeys = {(t[3], t[2]) for t in judged}
    weakkeys = {(t[3], i, t[6 + i]) for t in judged for i in _WEAK_IDX if t[6 + i]}

    mates: dict[int, list[tuple]] = defaultdict(list)     # content_group -> other-cloud copies (any cloud)
    by_name: dict[tuple, list[tuple]] = defaultdict(list)
    by_weak: dict[tuple, list[tuple]] = defaultdict(list)
    if judged and len(remotes) > 1:
        cur = conn.execute(
            "SELECT e.id, e.remote_id, e.path, e.mtime, e.size, e.name_norm, e.content_group, "
            f"e.md5, e.sha1, e.sha256, e.dropbox, e.quickxor FROM entries e WHERE e.remote_id <> ? AND {live_other}",
            (me.id,))
        for row in cur:
            g = row[6]
            if g is not None and g in groups:
                mates[g].append(row)
            if row[1] in allowed:
                if (row[4], row[5]) in namekeys:
                    by_name[(row[4], row[5])].append(row)
                for i in _WEAK_IDX:
                    if row[7 + i] and (row[4], i, row[7 + i]) in weakkeys:
                        by_weak[(row[4], i, row[7 + i])].append(row)

    bridged = _Bridged(conn)
    amb_cache: dict[int, set[str]] = {}

    def usable(row: tuple) -> bool:
        if row[1] not in allowed:
            return False
        a = amb_cache.get(row[1])
        if a is None:
            a = amb_cache[row[1]] = _ambiguous(conn, row[1])
        return row[2] not in a

    providers = builders._provider_map(remotes)
    names = {r.id: r.name for r in remotes.values()}
    proven: dict[int, tuple] = {}            # target entry id -> chosen witness row
    proven_rows: list[tuple] = []
    probable: list[tuple] = []
    only_here: list[tuple] = []
    unusable: list[tuple] = []
    witness_remotes: set[int] = set()
    candidate_remotes: set[int] = set()

    for t in judged:
        size = t[3]
        ms = [m for m in mates.get(t[4], ()) if m[4] == size] if t[4] is not None else []
        if ms:
            good = [m for m in ms if usable(m)]
            if good:
                if len(good) == 1:
                    w = good[0]
                else:
                    pick = builders.choose_keeper(
                        [{"entry_id": m[0], "remote_id": m[1], "remote": names[m[1]], "path": m[2], "mtime": m[3]}
                         for m in good], prefer_remotes=keep, providers=providers)
                    w = next(m for m in good if m[0] == pick["entry_id"])
                proven[t[0]] = w
                proven_rows.append(t)
                witness_remotes.add(w[1])
            else:
                unusable.append(t)
            continue
        mine = bridged.hashes(t[0], size, t[6:11])
        cands = list(by_name.get((size, t[2]), ()))
        for i in _WEAK_IDX:
            if t[6 + i]:
                cands.extend(by_weak.get((size, i, t[6 + i]), ()))
        hits = {c[0]: c for c in cands if not _contradict(mine, bridged.hashes(c[0], c[4], c[7:12]))}
        if hits:
            probable.append(t)
            candidate_remotes.update(c[1] for c in hits.values())
        else:
            only_here.append(t)
    return {
        "me": me, "remotes": remotes, "keep": keep, "prefix": prefix, "names": names,
        "proven": proven_rows, "witness": proven, "probable": probable, "only_here": only_here,
        "unusable": unusable, "unjudged": unjudged, "total": len(targets),
        "total_bytes": sum(t[3] or 0 for t in targets),
        "involved": witness_remotes | candidate_remotes,
    }


def _bytes(rows) -> int:
    return sum(r[3] or 0 for r in rows)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def release_report(conn: sqlite3.Connection, remote: str, *, path_prefix: str | None = None,
                   keep_on: list[str] | None = None, stale_after_hours: int = 24,
                   now: datetime | None = None) -> dict:
    return _report(conn, _analyze(conn, remote, path_prefix, keep_on), stale_after_hours, now)


def _report(conn: sqlite3.Connection, a: dict, stale_after_hours: int, now: datetime | None) -> dict:
    me, names = a["me"], a["names"]
    by_remote: dict[str, int] = {}
    for t in a["proven"]:
        n = names[a["witness"][t[0]][1]]
        by_remote[n] = by_remote.get(n, 0) + t[3]
    probable_ids = [t[0] for t in a["probable"]]

    now = now or datetime.now(timezone.utc)
    last = {r[0]: r[1] for r in conn.execute(
        "SELECT s.remote_id, s.finished_at FROM scans s JOIN (SELECT remote_id, MAX(id) AS id FROM scans "
        "WHERE status = 'complete' GROUP BY remote_id) m ON m.id = s.id")}
    freshness = {}
    for rid in sorted({me.id} | a["involved"], key=lambda r: (r != me.id, names[r])):
        at = last.get(rid)
        d = _parse(at)
        freshness[names[rid]] = {"synced_at": at, "stale": d is None or now - d > timedelta(hours=stale_after_hours)}

    def bucket(rows) -> dict:
        return {"files": len(rows), "bytes": _bytes(rows)}

    return {
        "remote": me.name, "path_prefix": a["prefix"], "keep_on": a["keep"] or None,
        "writable": me.writable,
        "total": {"files": a["total"], "bytes": a["total_bytes"]},
        "proven": {**bucket(a["proven"]), "by_remote": by_remote},
        "probable": {**bucket(a["probable"]), "entry_ids": probable_ids[:PROBABLE_ID_CAP],
                     "entry_ids_total": len(probable_ids)},
        "only_here": bucket(a["only_here"]),
        "unjudged": {"files": len(a["unjudged"]), "bytes": sum(u[2] or 0 for u in a["unjudged"])},
        "elsewhere_unusable": bucket(a["unusable"]),
        "freshness": freshness,
        "stale": any(f["stale"] for f in freshness.values()),
    }


_REASON = {"NATIVE_DOC": "native documents have no bytes to free", "SIZE_UNKNOWN": "size unknown",
           "AMBIGUOUS_PATH": "several entries share this path"}


def build_release(conn: sqlite3.Connection, *, remote: str, path_prefix: str | None = None,
                  keep_on: list[str] | None = None, quarantine_dir: str | None = None) -> BuildResult:
    a = _analyze(conn, remote, path_prefix, keep_on, quarantine_dir)
    me, names = a["me"], a["names"]
    if not me.writable:
        raise BuildError(f"{remote} has no write remote: nothing can be freed there", "READ_ONLY", remote=remote)
    result = BuildResult()
    rows = builders._rows_by_id(conn, (t[0] for t in a["proven"]))
    freed = 0
    for t in a["proven"]:
        w = a["witness"][t[0]]
        result.ops.append(builders.op_from_entry(
            rows[t[0]], "delete", witness_remote_id=w[1], witness_path=w[2],
            reason=f"release {me.name}: copy on {names[w[1]]}:{w[2]}"))
        freed += t[3]
    for eid, path, _size, code in a["unjudged"]:
        result.skipped.append({"entry_id": eid, "location": f"{me.name}:{path}", "code": code,
                               "reason": _REASON[code]})
    report = _report(conn, a, 24, None)
    result.stats = {**{k: report[k] for k in ("proven", "probable", "only_here", "unjudged", "elsewhere_unusable")},
                    "deletes": len(result.ops), "bytes_reclaimed": freed, "skipped": len(result.skipped)}
    return result


__all__ = ["release_report", "build_release"]
