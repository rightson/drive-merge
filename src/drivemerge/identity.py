"""Content identity: which catalog entries hold the same bytes, and how sure we are.

Exact identity is a union-find over live files. Two files are joined when they
share a hash value of any one type *and* the same size, or when one row of
hash_bridges (all five hashes of a single download) matches both. A bridge is
the only way two hash domains (Drive md5 / Dropbox / OneDrive QuickXor) ever
meet, so exactness is never inferred from names, sizes or mtimes.

`content_group` is the smallest entry id of the component and is set for every
live file that is identified (singletons included): it has at least one hash,
or it is the source of a bridge that still describes it. A file without hashes
(iCloud) is exact only through such a bridge, i.e. only after its own bytes
were downloaded and hashed, and only while its size and mtime are unchanged;
otherwise it can be at most a "probable" duplicate.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from array import array
from collections import defaultdict
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from . import db, hashing, providers

HASH_TYPES = providers.HASH_TYPES
_ALL_TYPES_MASK = (1 << len(HASH_TYPES)) - 1
_NO_GROUP = -(1 << 62)          # array('q') stand-in for NULL
_HEX = re.compile(r"[0-9a-f]+")
# Cheapest download first; providers not listed rank last.
_DOWNLOAD_ORDER = {"local": 0, "drive": 1, "dropbox": 2, "onedrive": 3, "iclouddrive": 4}


def _live(conn: sqlite3.Connection, a: str = "") -> str:
    """SQL for 'a file that exists, has bytes and is not parked in quarantine'; `a` is an optional alias like 'e.'.

    A quarantined file is a deleted file waiting for purge: counting it would make every cleaned-up
    copy look like a duplicate again (and offer it as the witness for its own replacement).
    """
    return (f"{a}present = 1 AND {a}is_dir = 0 AND {a}is_native_doc = 0 AND {a}size IS NOT NULL "
            f"AND {db.outside_quarantine(conn, a)}")


def _hashed(a: str = "") -> str:
    return "(" + " OR ".join(f"COALESCE({a}{t}, '') <> ''" for t in HASH_TYPES) + ")"


def _rows(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """Cursor yielding sqlite3.Row regardless of the connection's row_factory."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


def _tuples(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """Cursor yielding plain tuples: the million-row scans are twice as fast without Row."""
    cur = conn.cursor()
    cur.row_factory = None
    return cur


# --------------------------------------------------------------------------- #
# Exact groups
# --------------------------------------------------------------------------- #

@dataclass
class GroupStats:
    files_hashed: int = 0         # identified live files: have a hash or a valid bridge link (all got a content_group)
    groups: int = 0               # distinct content_groups, singletons included
    duplicate_groups: int = 0     # groups with >= 2 live files
    redundant_files: int = 0      # sum(members - 1) over duplicate groups
    redundant_bytes: int = 0      # sum(size * (members - 1)) over duplicate groups
    changed: int = 0              # rows whose content_group was rewritten
    size_conflicts: int = 0       # hash values seen with more than one size (never unioned)
    hash_conflicts: int = 0       # groups holding two different values of one hash type


class _Components:
    """Union-find over ids that take part in at least one edge.

    The root is always the smallest id of its component, which is exactly the
    value content_group must hold; ids that never appear are their own root.
    """

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        parent = self.parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:                      # path compression
            parent[x], x = root, parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        parent = self.parent
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = self.find(a), self.find(b)
        if ra < rb:
            parent[rb] = ra
        elif rb < ra:
            parent[ra] = rb


def _union_by_hash(conn: sqlite3.Connection, comps: _Components) -> int:
    """Join consecutive rows with equal (hash, size); returns the size-conflict count.

    Sorting by (hash, size) turns the join into one linear scan per hash type,
    with no pairwise comparison (a covering-index pre-filter skips hashes that
    occur only once). The same hash with another size is not an
    identity (corrupt listing or a crafted collision), so it is counted instead.
    """
    conflicts = 0
    for t in HASH_TYPES:
        cur = _tuples(conn)
        cur.execute(                                    # values that occur once cannot link anything
            f"SELECT id, size, {t} FROM entries "
            f"WHERE {t} IN (SELECT {t} FROM entries WHERE {t} IS NOT NULL GROUP BY {t} HAVING COUNT(*) > 1) "
            f"AND {t} <> '' AND {_live(conn)} ORDER BY {t}, size"
        )
        prev_hash = prev_size = first = None
        flagged = False
        for eid, size, h in cur:
            if h != prev_hash:
                prev_hash, prev_size, first, flagged = h, size, eid, False
            elif size == prev_size:
                comps.union(first, eid)
            else:
                if not flagged:
                    conflicts += 1
                    flagged = True
                prev_size, first = size, eid
    return conflicts


def _bridge_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _rows(conn).execute(
        f"SELECT id, size, source_entry_id, source_mtime, {', '.join(HASH_TYPES)} FROM hash_bridges"
    ).fetchall()


def _valid_source(conn: sqlite3.Connection, bridge: Mapping[str, Any]) -> int | None:
    """The bridge's source entry id while the bridge still describes it, else None.

    The bytes that were hashed are only known to be the entry's current bytes
    while it is live and its size and mtime equal what was downloaded (any
    rescan that sees a change breaks the link), and while no hash it stores
    contradicts the bridge.
    """
    sid = bridge["source_entry_id"]
    if sid is None or bridge["source_mtime"] is None:
        return None
    e = _rows(conn).execute(
        f"SELECT size, mtime, {', '.join(HASH_TYPES)} FROM entries e WHERE e.id = ? AND {_live(conn, 'e.')}", (sid,)
    ).fetchone()
    if e is None or e["size"] != bridge["size"] or e["mtime"] != bridge["source_mtime"]:
        return None
    if any(e[t] and e[t].lower() != bridge[t] for t in HASH_TYPES):
        return None
    return sid


def _union_by_bridges(conn: sqlite3.Connection, comps: _Components) -> None:
    """A bridge row carries every hash of one content, so it joins all entries matching any of them.

    Its still-valid source entry joins too: that is how a file with no stored
    hash (iCloud) becomes exact, even when it is the only member for now.
    `+size` keeps SQLite on the (selective) hash index instead of the size index.
    """
    for b in _bridge_rows(conn):
        ids: set[int] = set()
        source = _valid_source(conn, b)
        if source is not None:
            comps.parent.setdefault(source, source)
            ids.add(source)
        for t in HASH_TYPES:
            for (eid,) in _tuples(conn).execute(
                f"SELECT id FROM entries WHERE {t} = ? AND +size = ? AND {_live(conn)}", (b[t], b["size"])
            ):
                ids.add(eid)
        if len(ids) > 1:
            first, *rest = sorted(ids)
            for eid in rest:
                comps.union(first, eid)


def rebuild_groups(conn: sqlite3.Connection) -> GroupStats:
    """Recompute entries.content_group for the whole catalog in one transaction.

    Only rows whose value changes are written (with one shared row_version), so
    a rebuild on an unchanged catalog leaves the change feed untouched.
    """
    stats = GroupStats()
    comps = _Components()
    with db.transaction(conn):
        stats.size_conflicts = _union_by_hash(conn, comps)
        _union_by_bridges(conn, comps)

        parent = comps.parent
        find = comps.find
        ids, new_groups = array("q"), array("q")
        comp_count: dict[int, int] = defaultdict(int)
        comp_size: dict[int, int] = {}
        seen_values: dict[int, str] = {}
        conflicted: set[int] = set()

        cur = _tuples(conn)
        cur.execute(
            f"SELECT id, size, content_group, md5, sha1, sha256, dropbox, quickxor FROM entries WHERE {_live(conn)}"
        )
        for row in cur:
            eid = row[0]
            if eid in parent or row[3] or row[4] or row[5] or row[6] or row[7]:
                stats.files_hashed += 1
                new = eid
                if eid in parent:
                    new = find(eid)
                    comp_count[new] += 1
                    comp_size[new] = row[1]
                    for i in range(3, 8):                      # two values of one type in a component = anomaly
                        v = row[i]
                        if v:
                            key = new * 8 + i
                            prev = seen_values.setdefault(key, v)
                            if prev != v:
                                conflicted.add(new)
            else:
                new = None
            if new != row[2]:
                ids.append(eid)
                new_groups.append(_NO_GROUP if new is None else new)

        for (eid,) in _tuples(conn).execute(
            f"SELECT id FROM entries WHERE content_group IS NOT NULL AND NOT ({_live(conn)})"
        ):
            ids.append(eid)
            new_groups.append(_NO_GROUP)

        if ids:
            rv = db.next_row_version(conn)
            conn.executemany(
                "UPDATE entries SET content_group = ?, row_version = ? WHERE id = ?",
                ((None if g == _NO_GROUP else g, rv, i) for i, g in zip(ids, new_groups)),
            )

    stats.changed = len(ids)
    multi = {r: n for r, n in comp_count.items() if n > 1}
    stats.groups = stats.files_hashed - sum(comp_count.values()) + len(comp_count)
    stats.duplicate_groups = len(multi)
    stats.redundant_files = sum(n - 1 for n in multi.values())
    stats.redundant_bytes = sum((n - 1) * comp_size[r] for r, n in multi.items())
    stats.hash_conflicts = len(conflicted)
    return stats


# --------------------------------------------------------------------------- #
# Reading groups
# --------------------------------------------------------------------------- #

@dataclass
class Copy:
    entry_id: int
    remote_id: int
    remote: str
    path: str
    size: int
    mtime: str | None


@dataclass
class DupGroup:
    key: str                    # 'g:<content_group>' (exact) or 'p:<size>:<name_norm>' (probable)
    confidence: str             # 'exact' | 'probable'
    size: int
    copies: list[Copy]          # ordered by remote name, then path
    remotes: list[str]          # distinct, sorted
    redundant_bytes: int        # size * (len(copies) - 1)


_COPY_SELECT = (
    "SELECT e.id, e.remote_id, r.name AS remote, e.path, e.size, e.mtime "
    "FROM entries e JOIN remotes r ON r.id = e.remote_id"
)
_COPY_ORDER = "ORDER BY r.name, e.path, e.id"


def _copies(conn: sqlite3.Connection, where: str, params: tuple) -> list[Copy]:
    cur = _tuples(conn)
    cur.execute(f"{_COPY_SELECT} WHERE {_live(conn, 'e.')} AND {where} {_COPY_ORDER}", params)
    return [Copy(*r) for r in cur]


def _group_filters(conn: sqlite3.Connection, remote: str | None, cross_remote_only: bool,
                   path_prefix: str | None) -> tuple[str, dict[str, Any]]:
    """HAVING fragments shared by exact and probable listings ('at least one copy ...' semantics)."""
    parts: list[str] = []
    params: dict[str, Any] = {}
    if cross_remote_only:
        parts.append("COUNT(DISTINCT e.remote_id) >= 2")
    if remote is not None:
        params["rid"] = db.remote_id(conn, remote)
        parts.append("MAX(e.remote_id = :rid) = 1")
    prefix = (path_prefix or "").strip("/")
    if prefix:
        # Whole-segment prefix, compared literally (no LIKE wildcards, case-sensitive).
        params["pfx"], params["pfx_slash"], params["pfx_len"] = prefix, prefix + "/", len(prefix) + 1
        parts.append("MAX(e.path = :pfx OR substr(e.path, 1, :pfx_len) = :pfx_slash) = 1")
    return "".join(f" AND {p}" for p in parts), params


def duplicate_groups(conn: sqlite3.Connection, *, confidence: str = "exact", remote: str | None = None,
                     cross_remote_only: bool = False, min_size: int = 1, path_prefix: str | None = None,
                     limit: int = 100, offset: int = 0) -> list[DupGroup]:
    """Groups of live files that are duplicates, biggest reclaimable bytes first.

    exact     content_group groups with >= 2 live copies (proof: matching hashes).
    probable  (size, name_norm) groups with >= 2 live copies that are not all in
              one exact group (an unhashed copy, or copies in different/unknown
              groups). Candidates only, never proof.

    `remote` / `path_prefix` keep a group when at least one copy matches, but
    every copy is listed. `path_prefix` matches whole path segments. Unknown
    `remote` raises KeyError. Ties on redundant_bytes order by key (as text).
    """
    if confidence not in ("exact", "probable"):
        raise ValueError(f"confidence must be 'exact' or 'probable', not {confidence!r}")
    having, params = _group_filters(conn, remote, cross_remote_only, path_prefix)
    params.update(min_size=min_size, limit=max(limit, 0), offset=max(offset, 0))
    live = _live(conn, "e.")
    cur = _tuples(conn)
    out: list[DupGroup] = []
    if confidence == "exact":
        cur.execute(
            f"SELECT e.content_group FROM entries e "
            f"WHERE {live} AND e.content_group IS NOT NULL AND e.size >= :min_size "
            f"GROUP BY e.content_group HAVING COUNT(*) >= 2{having} "
            f"ORDER BY MIN(e.size) * (COUNT(*) - 1) DESC, 'g:' || e.content_group "
            f"LIMIT :limit OFFSET :offset", params)
        for (g,) in cur.fetchall():
            out.append(_dup_group(f"g:{g}", "exact", _copies(conn, "e.content_group = ?", (g,))))
    else:
        cur.execute(
            f"SELECT e.size, e.name_norm FROM entries e WHERE {live} AND e.size >= :min_size "
            f"GROUP BY e.size, e.name_norm "
            f"HAVING COUNT(*) >= 2 "
            f"AND NOT (COUNT(e.content_group) = COUNT(*) AND MIN(e.content_group) = MAX(e.content_group)){having} "
            f"ORDER BY e.size * (COUNT(*) - 1) DESC, 'p:' || e.size || ':' || e.name_norm "
            f"LIMIT :limit OFFSET :offset", params)
        for size, name_norm in cur.fetchall():
            out.append(_dup_group(f"p:{size}:{name_norm}", "probable",
                                  _copies(conn, "e.size = ? AND e.name_norm = ?", (size, name_norm))))
    return out


def _dup_group(key: str, confidence: str, copies: list[Copy]) -> DupGroup:
    size = copies[0].size
    return DupGroup(key=key, confidence=confidence, size=size, copies=copies,
                    remotes=sorted({c.remote for c in copies}),
                    redundant_bytes=size * (len(copies) - 1))


def content_key(row: Mapping[str, Any] | sqlite3.Row) -> str:
    """Stable identity of an entry's content: its exact group, else the entry itself."""
    group = row["content_group"]
    return f"g:{group}" if group is not None else f"e:{row['id']}"


def locations(conn: sqlite3.Connection, entry_id: int) -> list[Copy]:
    """Every live copy with exactly the same content as `entry_id`, itself included.

    An ungrouped file is only itself. A missing or non-live entry has no
    locations: a tombstone must never be counted as a surviving copy.
    """
    row = conn.execute(
        f"SELECT content_group FROM entries e WHERE e.id = ? AND {_live(conn, 'e.')}", (entry_id,)
    ).fetchone()
    if row is None:
        return []
    if row[0] is None:
        return _copies(conn, "e.id = ?", (entry_id,))
    return _copies(conn, "e.content_group = ?", (row[0],))


def same_content(conn: sqlite3.Connection, a: int, b: int) -> bool:
    """True only when both are live files proven identical by hash (exact, never probable)."""
    if a == b:
        return bool(conn.execute(f"SELECT 1 FROM entries e WHERE e.id = ? AND {_live(conn, 'e.')}", (a,)).fetchone())
    rows = conn.execute(
        f"SELECT content_group FROM entries e WHERE e.id IN (?, ?) AND {_live(conn, 'e.')}", (a, b)
    ).fetchall()
    return len(rows) == 2 and rows[0][0] is not None and rows[0][0] == rows[1][0]


# --------------------------------------------------------------------------- #
# Bridging
# --------------------------------------------------------------------------- #

@dataclass
class BridgeCandidate:
    entry_id: int
    remote: str
    path: str
    size: int
    unresolved_copies: int
    reason: str                 # 'name+size' | 'size'


class _File(NamedTuple):
    id: int
    name: str
    path: str
    group: int | None
    remote: str
    provider: str
    remote_id: int
    hashes: tuple[str | None, ...]      # aligned with HASH_TYPES


def bridge_candidates(conn: sqlite3.Connection, *, max_bytes: int | None = None, min_size: int = 1,
                      limit: int = 100) -> list[BridgeCandidate]:
    """Which files to download once, so that one hash set settles the most ambiguity.

    Per size, files are units (a content_group, or a lone file). A unit's hash
    domain is the set of hash types its files carry; a unit matched by a bridge
    (or whose file is a bridge's valid source) counts as holding all five. Two
    units with disjoint domains cannot be compared, so such a size class is
    ambiguous. Two kinds of candidate come out of it:

    * hashed domains, one per class: a download from unit U (with all five
      hashes) settles U against every unit disjoint from it, and
      `unresolved_copies` counts those files. Unit choice: a copy sharing a
      name with an unresolved copy first, then the most unresolved copies, then
      the cheapest provider (local > drive > dropbox > onedrive > iclouddrive,
      more native hashes breaking ties).
    * every unhashed file (iCloud) with a same-size counterpart on another
      remote or another unhashed file, unless a valid bridge already covers
      it. A bridge cannot prove it equal to anything unless its own bytes were
      hashed, so each needs its own download; `unresolved_copies` counts the
      counterparts. Once linked it carries all five hashes, which is why it no
      longer takes part in the hashed-domain pairs.

    Candidates are ranked name+size before size-only, then by
    size * unresolved_copies, and filled greedily within `max_bytes`.
    """
    bridges = _bridge_rows(conn)
    bridged = {(t, b[t], b["size"]) for b in bridges for t in HASH_TYPES}
    linked = {sid for b in bridges if (sid := _valid_source(conn, b)) is not None}
    sig = " + ".join(f"{1 << i} * (COALESCE({t}, '') <> '')" for i, t in enumerate(HASH_TYPES))
    hashed = _hashed()
    size_cap = "" if max_bytes is None else " AND size <= :max_bytes"
    cur = _tuples(conn)
    cur.execute(
        f"""WITH cls AS (
                SELECT size FROM entries
                WHERE {_live(conn)} AND size >= :min_size{size_cap}
                GROUP BY size
                HAVING (COUNT(DISTINCT CASE WHEN {hashed} THEN {sig} END) >= 2
                        AND COUNT(DISTINCT CASE WHEN {hashed} THEN COALESCE(content_group, -id) END) >= 2)
                    OR (SUM(NOT {hashed}) >= 1 AND COUNT(*) >= 2
                        AND (COUNT(DISTINCT remote_id) >= 2 OR SUM(NOT {hashed}) >= 2)))
            SELECT e.size, e.id, e.name_norm, e.path, e.content_group, r.name, r.provider, e.remote_id,
                   {', '.join('e.' + t for t in HASH_TYPES)}
            FROM entries e JOIN remotes r ON r.id = e.remote_id
            WHERE e.size IN (SELECT size FROM cls) AND {_live(conn, 'e.')}
            ORDER BY e.size, e.id""",
        {"min_size": min_size, "max_bytes": max_bytes},
    )
    found: list[tuple[bool, int, int, BridgeCandidate]] = []
    for size, rows in groupby(cur, key=lambda r: r[0]):
        files = [_File(r[1], r[2], r[3], r[4], r[5], r[6], r[7], tuple(r[8:])) for r in rows]
        for cand in _class_candidates(size, files, bridged, linked):
            found.append((cand.reason != "name+size", -size * cand.unresolved_copies, cand.entry_id, cand))
    found.sort(key=lambda t: t[:3])

    out: list[BridgeCandidate] = []
    spent = 0
    for *_, cand in found:
        if len(out) >= limit:
            break
        if max_bytes is not None and spent + cand.size > max_bytes:
            continue
        spent += cand.size
        out.append(cand)
    return out


def _class_candidates(size: int, files: list[_File], bridged: set[tuple[str, str, int]],
                      linked: set[int]) -> list[BridgeCandidate]:
    """Candidates of one size class: at most one hashed-domain download plus one per unlinked bare file."""
    hashed: list[tuple[_File, int]] = []
    bare: list[_File] = []
    for f in files:
        mask = 0
        for i, t in enumerate(HASH_TYPES):
            if f.hashes[i]:
                mask |= 1 << i
                if (t, f.hashes[i], size) in bridged:
                    mask = _ALL_TYPES_MASK
        if f.id in linked:
            mask = _ALL_TYPES_MASK
        if mask:
            hashed.append((f, mask))
        else:
            bare.append(f)
    out = []
    cand = _hashed_candidate(size, hashed)
    if cand is not None:
        out.append(cand)
    out.extend(_bare_candidates(size, files, bare))
    return out


def _hashed_candidate(size: int, hashed: list[tuple[_File, int]]) -> BridgeCandidate | None:
    """The best download among files that carry (or are covered by) hashes, or None if all are comparable."""
    unit_mask: dict[int, int] = defaultdict(int)       # unit -> OR of its files' hash domains
    for f, mask in hashed:
        unit_mask[f.group if f.group is not None else -f.id] |= mask

    copies_by_mask: dict[int, int] = defaultdict(int)
    names_by_mask: dict[int, set[str]] = defaultdict(set)
    for f, _ in hashed:
        m = unit_mask[f.group if f.group is not None else -f.id]
        copies_by_mask[m] += 1
        names_by_mask[m].add(f.name)

    best = None
    for f, _ in hashed:
        mask = unit_mask[f.group if f.group is not None else -f.id]
        partners = [m for m in copies_by_mask if m & mask == 0]
        if not partners:
            continue
        unresolved = sum(copies_by_mask[m] for m in partners)
        name_match = any(f.name in names_by_mask[m] for m in partners)
        rank = (not name_match, -unresolved, _DOWNLOAD_ORDER.get(f.provider, len(_DOWNLOAD_ORDER)),
                -len(providers.get(f.provider).native_hashes), f.id)
        if best is None or rank < best[0]:
            best = (rank, f, unresolved, name_match)
    if best is None:
        return None
    _, f, unresolved, name_match = best
    return BridgeCandidate(f.id, f.remote, f.path, size, unresolved, "name+size" if name_match else "size")


def _bare_candidates(size: int, files: list[_File], bare: list[_File]) -> list[BridgeCandidate]:
    """One candidate per unlinked hashless file that has a counterpart to be compared with.

    A counterpart is any other file on a different remote, or any other
    hashless file. Counted with tallies so a big class stays linear.
    """
    if not bare:
        return []
    is_bare = {f.id for f in bare}
    total = len(files)
    n_remote: dict[int, int] = defaultdict(int)
    n_bare_remote: dict[int, int] = defaultdict(int)
    n_name: dict[str, int] = defaultdict(int)
    n_name_remote: dict[tuple[int, str], int] = defaultdict(int)
    n_name_bare_remote: dict[tuple[int, str], int] = defaultdict(int)
    for f in files:
        hashless = not any(f.hashes)
        n_remote[f.remote_id] += 1
        n_name[f.name] += 1
        n_name_remote[f.remote_id, f.name] += 1
        if hashless:
            n_bare_remote[f.remote_id] += 1
            n_name_bare_remote[f.remote_id, f.name] += 1
    out = []
    for f in bare:
        r = f.remote_id
        counterparts = total - n_remote[r] + n_bare_remote[r] - 1
        if counterparts <= 0:
            continue
        same_name = n_name[f.name] - n_name_remote[r, f.name] + n_name_bare_remote[r, f.name] - 1
        out.append(BridgeCandidate(f.id, f.remote, f.path, size, counterparts,
                                   "name+size" if same_name > 0 else "size"))
    return out


def _clean_bridge(hashes: Mapping[str, Any]) -> dict[str, Any]:
    """Validated, lowercased copy of a five-hash + size mapping (ValueError otherwise)."""
    clean: dict[str, Any] = {}
    for t in HASH_TYPES:
        value = hashes.get(t)
        if not isinstance(value, str) or not _HEX.fullmatch(value.lower()):
            raise ValueError(f"bridge needs a hex {t} value, got {value!r}")
        clean[t] = value.lower()
    size = hashes.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(f"bridge needs a non-negative integer size, got {size!r}")
    clean["size"] = size
    return clean


def record_bridge(conn: sqlite3.Connection, hashes: Mapping[str, Any], *, source_entry_id: int | None = None,
                  source_mtime: str | None = None) -> int:
    """Store the five hashes (+ size) of one downloaded content; returns the bridge id.

    Idempotent on sha256. A row with the same sha256 but different other values
    would link unrelated files, so it is refused instead of silently reused.
    `source_entry_id` + `source_mtime` name the file whose bytes were hashed;
    rebuild_groups links it only while its size and mtime still match. A bridge
    has one source slot, so a later copy takes it over only when the previous
    holder is stale or hashed (hashed files are linked by their hashes anyway).
    Run rebuild_groups afterwards for the bridge to take effect.
    """
    clean = _clean_bridge(hashes)
    conn.execute(
        "INSERT INTO hash_bridges(size, md5, sha1, sha256, dropbox, quickxor, source_entry_id, source_mtime, "
        "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(sha256) DO NOTHING",
        (clean["size"], clean["md5"], clean["sha1"], clean["sha256"], clean["dropbox"], clean["quickxor"],
         source_entry_id, source_mtime, db.utcnow()),
    )
    row = _rows(conn).execute(
        "SELECT id, size, source_entry_id, source_mtime, md5, sha1, sha256, dropbox, quickxor "
        "FROM hash_bridges WHERE sha256 = ?", (clean["sha256"],)
    ).fetchone()
    if any(row[t] != clean[t] for t in ("size", "md5", "sha1", "dropbox", "quickxor")):
        raise ValueError(f"hash_bridges row {row['id']} has this sha256 but different values")
    if source_entry_id is not None and (row["source_entry_id"], row["source_mtime"]) != (source_entry_id, source_mtime):
        holder = _valid_source(conn, row)
        holder_hashed = holder is not None and conn.execute(
            f"SELECT 1 FROM entries WHERE id = ? AND {_hashed()}", (holder,)).fetchone()
        if row["source_entry_id"] in (None, source_entry_id) or holder is None or holder_hashed:
            conn.execute("UPDATE hash_bridges SET source_entry_id = ?, source_mtime = ? WHERE id = ?",
                         (source_entry_id, source_mtime, row["id"]))
    return int(row["id"])


def bridge_entry(conn: sqlite3.Connection, rclone: Any, entry_id: int, tmpdir: str | Path) -> dict:
    """Download one file, hash it locally and record the result as a bridge.

    Read-only on the cloud. The temp file is always removed. Any hash the
    catalog already holds for the entry must match the downloaded bytes, else
    the file changed since the scan and nothing is recorded (ValueError).
    The result has `linked`: whether rebuild_groups will now tie this entry to
    the bridge (always for a hashed entry; for a hashless one only if the
    bridge's single source slot is theirs). Run rebuild_groups afterwards.
    """
    cur = _rows(conn)
    row = cur.execute(
        f"SELECT e.id, e.path, e.size, e.mtime, e.md5, e.sha1, e.sha256, e.dropbox, e.quickxor, r.read_remote "
        f"FROM entries e JOIN remotes r ON r.id = e.remote_id WHERE e.id = ? AND {_live(conn, 'e.')}", (entry_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"entry {entry_id} is not a live file")

    tmp = Path(tmpdir)
    tmp.mkdir(parents=True, exist_ok=True)
    local = tmp / f"bridge-{entry_id}-{uuid.uuid4().hex}.tmp"
    try:
        rclone.download(row["read_remote"], row["path"], str(local))
        computed = hashing.hash_file(local)
    finally:
        local.unlink(missing_ok=True)

    hashes = _clean_bridge(computed)
    if hashes["size"] != row["size"]:
        raise ValueError(f"entry {entry_id} changed since the scan: size {row['size']} -> {hashes['size']}")
    for t in HASH_TYPES:
        if row[t] and row[t].lower() != hashes[t]:
            raise ValueError(f"entry {entry_id} changed since the scan: {t} {row[t]} -> {hashes[t]}")
    bridge_id = record_bridge(conn, hashes, source_entry_id=entry_id, source_mtime=row["mtime"])
    bridge = cur.execute("SELECT * FROM hash_bridges WHERE id = ?", (bridge_id,)).fetchone()
    linked = any(row[t] for t in HASH_TYPES) or _valid_source(conn, bridge) == entry_id
    return {"bridge_id": bridge_id, "hashes": hashes, "entry_id": entry_id, "linked": linked}
