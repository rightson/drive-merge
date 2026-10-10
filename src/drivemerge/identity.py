"""Content identity: which catalog entries hold the same bytes, and how sure we are.

Exact identity is a union-find over live files. Two files are joined when they
share a hash value of any one type *and* the same size, or when one row of
hash_bridges (all five hashes of a single download) matches both. A bridge is
the only way two hash domains (Drive md5 / Dropbox / OneDrive QuickXor) ever
meet, so exactness is never inferred from names, sizes or mtimes.

A join is refused when the two sides already know different values of one hash
type (md5 equal but sha256 different, a chain that only contradicts itself
after several joins): those bytes are provably different, so the shared value
proves nothing. Refusals are counted in GroupStats.hash_conflicts.

`content_group` is the smallest entry id of the component and is set for every
live file that is identified (singletons included): it has at least one hash,
or it is the source of a bridge that still describes it. A file without hashes
(iCloud) is exact only through such a bridge (bridge_sources), i.e. only after
its own bytes were downloaded and hashed, and only while its size and mtime are
unchanged; otherwise it can be at most a "probable" duplicate.
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
    hash_conflicts: int = 0       # joins refused: the two sides hold different values of one hash type


# What a component knows about its bytes: one value per hash type (HASH_TYPES order), NULL or '' = unknown.
# Stored hashes are lowercase hex (schema.sql), so values compare as they are.
_Sig = tuple[str | None, ...]
_NODE = 1 << 62                 # union-find id of a bridge: above any entry id, so entries stay the roots


def _sig_cols(a: str = "", *, fold: bool = False) -> str:
    """SELECT list of an entry's five hashes; `fold` lower-cases them, for comparing with a bridge's."""
    return ", ".join(f"lower({a}{t})" if fold else f"{a}{t}" for t in HASH_TYPES)


def _merge(a: _Sig, b: _Sig) -> _Sig | None:
    """Combine what two components know, or None when they know different values of one type."""
    out = []
    for x, y in zip(a, b):
        if not x or x == y:
            out.append(y)
        elif not y:
            out.append(x)
        else:
            return None
    return tuple(out)


class _Components:
    """Union-find over ids that take part in at least one edge, refusing edges that would fuse different bytes.

    Every component carries the hash values it knows (`sig`, root -> _Sig). Two components that
    know different values of one hash type hold provably different content, so the edge between
    them is dropped and counted in `refused`: without this, shared values being transitive, one
    corrupt listing or crafted collision would fuse two real contents into one "exact" group.
    The root is always the smallest id of its component, which is exactly the value content_group
    must hold; ids that never appear are their own root.
    """

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}
        self.sig: dict[int, _Sig] = {}
        self.refused = 0

    def add(self, x: int, sig: _Sig) -> None:
        """Register `x` with the hashes it carries (no-op when it is already known)."""
        if x not in self.parent:
            self.parent[x] = x
            self.sig[x] = sig

    def find(self, x: int) -> int:
        parent = self.parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:                      # path compression
            parent[x], x = root, parent[x]
        return root

    def union(self, a: int, b: int) -> bool:
        """Join the components of two added ids; False (and counted) when their hashes contradict."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        merged = _merge(self.sig[ra], self.sig[rb])
        if merged is None:
            self.refused += 1
            return False
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.sig[ra] = merged
        del self.sig[rb]
        return True


def _union_by_hash(conn: sqlite3.Connection, comps: _Components) -> int:
    """Join consecutive rows with equal (hash, size); returns the size-conflict count.

    Sorting by (hash, size, id) turns the join into one linear scan per hash type,
    with no pairwise comparison (a covering-index pre-filter skips hashes that
    occur only once); the first row of a run anchors it, so which rows win a
    conflict does not depend on scan order. The same hash with another size is
    not an identity (corrupt listing or a crafted collision), so it is counted
    instead.
    """
    conflicts = 0
    for t in HASH_TYPES:
        cur = _tuples(conn)
        cur.execute(                                    # values that occur once cannot link anything
            f"SELECT id, size, {t}, {_sig_cols()} FROM entries "
            f"WHERE {t} IN (SELECT {t} FROM entries WHERE {t} IS NOT NULL GROUP BY {t} HAVING COUNT(*) > 1) "
            f"AND {t} <> '' AND {_live(conn)} ORDER BY {t}, size, id"
        )
        prev_hash = prev_size = first = first_sig = None
        flagged = False
        for row in cur:
            eid, size, h = row[0], row[1], row[2]
            if h != prev_hash:
                prev_hash, prev_size, first, first_sig, flagged = h, size, eid, row[3:], False
            elif size == prev_size:
                comps.add(first, first_sig)
                comps.add(eid, row[3:])
                comps.union(first, eid)
            else:
                if not flagged:
                    conflicts += 1
                    flagged = True
                prev_size, first, first_sig = size, eid, row[3:]
    return conflicts


class _Link(NamedTuple):
    """A bridge and one entry whose own bytes it describes (a bridge_sources row that still holds)."""
    bridge_id: int
    entry_id: int
    hashes: _Sig                # what the catalog stores for the entry


def _valid_links(conn: sqlite3.Connection, entry_id: int | None = None) -> tuple[list[_Link], int]:
    """The (bridge, source entry) links the catalog still vouches for, and how many entries were dropped as ambiguous.

    A bridge_sources row links its entry only while the entry is live with the
    size and mtime that were downloaded (any rescan that sees a change breaks
    the link) and no hash it stores contradicts the bridge. An entry that
    validly sources two bridges (same size and mtime, yet different bytes were
    hashed at different times) is described by neither: its link is dropped.
    """
    contradicts = " OR ".join(f"(COALESCE(e.{t}, '') <> '' AND lower(e.{t}) <> b.{t})" for t in HASH_TYPES)
    params: tuple = ()
    only = ""
    if entry_id is not None:
        only, params = " AND s.entry_id = ?", (entry_id,)
    cur = _tuples(conn)
    cur.execute(
        f"SELECT s.entry_id, s.bridge_id, {_sig_cols('e.', fold=True)} "
        f"FROM bridge_sources s "
        f"JOIN hash_bridges b ON b.id = s.bridge_id AND b.size = s.size "
        f"JOIN entries e ON e.id = s.entry_id AND e.size = s.size AND e.mtime = s.mtime "
        f"WHERE s.mtime IS NOT NULL AND {_live(conn, 'e.')} AND NOT ({contradicts}){only} "
        f"ORDER BY s.entry_id, s.bridge_id", params)
    links: list[_Link] = []
    ambiguous = 0
    for eid, rows in groupby(cur, key=lambda r: r[0]):
        rows = list(rows)
        if len(rows) > 1:
            ambiguous += 1
        else:
            links.append(_Link(rows[0][1], eid, rows[0][2:]))
    return links, ambiguous


def _bridge_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _rows(conn).execute(f"SELECT id, size, {', '.join(HASH_TYPES)} FROM hash_bridges ORDER BY id").fetchall()


def _union_by_bridges(conn: sqlite3.Connection, comps: _Components) -> int:
    """A bridge row carries every hash of one content, so it joins all entries matching any of them.

    Each bridge is a node of its own, carrying all five hashes: an entry whose
    component knows a different value of any type is refused, which is how a
    stored md5 that matches while the sha256 does not stays out. Its valid source
    entries join too: that is how a file with no stored hash (iCloud) becomes
    exact, even when it is the only member for now. `+size` keeps SQLite on the
    (selective) hash index instead of the size index. Returns the number of
    sources dropped as ambiguous (_valid_links).
    """
    links, ambiguous = _valid_links(conn)
    sources: dict[int, dict[int, _Sig]] = defaultdict(dict)
    for link in links:
        sources[link.bridge_id][link.entry_id] = link.hashes
    for b in _bridge_rows(conn):
        node = _NODE + b["id"]
        comps.add(node, tuple(b[t] for t in HASH_TYPES))
        members = dict(sources.get(b["id"], ()))
        for t in HASH_TYPES:
            for row in _tuples(conn).execute(
                f"SELECT id, {_sig_cols(fold=True)} FROM entries WHERE {t} = ? AND +size = ? AND {_live(conn)}",
                (b[t], b["size"])
            ):
                members[row[0]] = row[1:]
        for eid in sorted(members):
            comps.add(eid, members[eid])
            comps.union(node, eid)
    return ambiguous


def rebuild_groups(conn: sqlite3.Connection) -> GroupStats:
    """Recompute entries.content_group for the whole catalog in one transaction.

    Only rows whose value changes are written (with one shared row_version), so
    a rebuild on an unchanged catalog leaves the change feed untouched.
    """
    stats = GroupStats()
    comps = _Components()
    with db.transaction(conn):
        stats.size_conflicts = _union_by_hash(conn, comps)
        ambiguous = _union_by_bridges(conn, comps)
        stats.hash_conflicts = comps.refused + ambiguous

        parent = comps.parent
        find = comps.find
        ids, new_groups = array("q"), array("q")
        comp_count: dict[int, int] = defaultdict(int)
        comp_size: dict[int, int] = {}

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
    One query returns a whole page with the copies of all its groups.
    """
    if confidence not in ("exact", "probable"):
        raise ValueError(f"confidence must be 'exact' or 'probable', not {confidence!r}")
    having, params = _group_filters(conn, remote, cross_remote_only, path_prefix)
    params.update(min_size=min_size, limit=max(limit, 0), offset=max(offset, 0))
    live = _live(conn, "e.")
    if confidence == "exact":
        page = (f"SELECT e.content_group AS g, MIN(e.size) * (COUNT(*) - 1) AS red, 'g:' || e.content_group AS k "
                f"FROM entries e WHERE {live} AND e.content_group IS NOT NULL AND e.size >= :min_size "
                f"GROUP BY e.content_group HAVING COUNT(*) >= 2{having} "
                f"ORDER BY red DESC, k LIMIT :limit OFFSET :offset")
        join = "e.content_group = page.g"
    else:
        page = (f"SELECT e.size AS size, e.name_norm AS name_norm, e.size * (COUNT(*) - 1) AS red, "
                f"'p:' || e.size || ':' || e.name_norm AS k "
                f"FROM entries e WHERE {live} AND e.size >= :min_size "
                f"GROUP BY e.size, e.name_norm "
                f"HAVING COUNT(*) >= 2 "
                f"AND NOT (COUNT(e.content_group) = COUNT(*) AND MIN(e.content_group) = MAX(e.content_group)){having} "
                f"ORDER BY red DESC, k LIMIT :limit OFFSET :offset")
        join = "e.size = page.size AND e.name_norm = page.name_norm"
    cur = _tuples(conn)
    cur.execute(                                        # the page, then every live copy of its groups, in one go
        f"WITH page AS ({page}) SELECT page.k, e.id, e.remote_id, r.name, e.path, e.size, e.mtime "
        f"FROM page JOIN entries e ON {join} JOIN remotes r ON r.id = e.remote_id "
        f"WHERE {live} ORDER BY page.red DESC, page.k, r.name, e.path, e.id", params)
    return [_dup_group(key, confidence, [Copy(*r[1:]) for r in rows])
            for key, rows in groupby(cur.fetchall(), key=lambda r: r[0])]


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
      remote or another unhashed file, unless a bridge_sources row already
      links it. A bridge cannot prove it equal to anything unless its own bytes
      were hashed, so each needs its own download (even when another copy of
      the same bytes is linked); `unresolved_copies` counts the counterparts.
      Once linked it carries all five hashes, which is why it no longer takes
      part in the hashed-domain pairs.

    Candidates are ranked name+size before size-only, then by
    size * unresolved_copies, and filled greedily within `max_bytes`.
    """
    bridged = {(t, b[t], b["size"]) for b in _bridge_rows(conn) for t in HASH_TYPES}
    linked = {link.entry_id for link in _valid_links(conn)[0]}
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
    it is added to bridge_sources (any number of copies of the same bytes can
    be sources, a repeat for the same entry refreshes its row) and
    rebuild_groups links it only while its size and mtime still match.
    hash_bridges.source_* keeps the first source for readers that predate
    bridge_sources: a later copy takes that slot over only when the previous
    holder is stale or hashed (hashed files are linked by their hashes anyway).
    All or nothing. Run rebuild_groups afterwards for the bridge to take effect.
    """
    clean = _clean_bridge(hashes)
    with db.savepoint(conn, "record_bridge"):
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
        if source_entry_id is not None:
            conn.execute(
                "INSERT INTO bridge_sources(bridge_id, entry_id, size, mtime) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(bridge_id, entry_id) DO UPDATE SET size = excluded.size, mtime = excluded.mtime",
                (row["id"], source_entry_id, clean["size"], source_mtime))
            if (row["source_entry_id"], row["source_mtime"]) != (source_entry_id, source_mtime) \
                    and _slot_is_free(conn, row, source_entry_id):
                conn.execute("UPDATE hash_bridges SET source_entry_id = ?, source_mtime = ? WHERE id = ?",
                             (source_entry_id, source_mtime, row["id"]))
    return int(row["id"])


def _slot_is_free(conn: sqlite3.Connection, bridge: sqlite3.Row, taker: int) -> bool:
    """May `taker` have hash_bridges.source_*? Yes when empty, its own, stale, or held by a hashed file."""
    holder = bridge["source_entry_id"]
    if holder in (None, taker):
        return True
    if not any(link.bridge_id == bridge["id"] for link in _valid_links(conn, holder)[0]):
        return True
    return conn.execute(f"SELECT 1 FROM entries WHERE id = ? AND {_hashed()}", (holder,)).fetchone() is not None


def bridge_entry(conn: sqlite3.Connection, rclone: Any, entry_id: int, tmpdir: str | Path) -> dict:
    """Download one file, hash it locally and record the result as a bridge.

    Read-only on the cloud. The temp file is always removed. Any hash the
    catalog already holds for the entry must match the downloaded bytes, else
    the file changed since the scan and nothing is recorded (ValueError).
    The result has `linked`: whether rebuild_groups will now tie this entry to
    the bridge (always for a hashed entry; for a hashless one when it has an
    mtime and no other bridge claims the same size and mtime). Several
    hashless copies of the same bytes are each linked by their own download.
    Run rebuild_groups afterwards.
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
    linked = any(row[t] for t in HASH_TYPES) or \
        any(link.bridge_id == bridge_id for link in _valid_links(conn, entry_id)[0])
    return {"bridge_id": bridge_id, "hashes": hashes, "entry_id": entry_id, "linked": linked}
