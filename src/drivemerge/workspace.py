"""Visual workspace: staged drafts over a virtual namespace, and their conversion to plans.

A draft is an ordered list of actions (move / mkdir) on the tree the user *sees*,
never on a cloud. Every action expands into sub-moves (moved root -> new
location); applying them in order gives the forward map `fwd`, which sends a
catalog location to where it ends up. Everything the UI shows and every op a plan
contains is derived from that one function, so "drag A into B, then B into C"
and "drag away and back" need no special cases: only the net effect survives.

Two ideas keep it fast and exact:
  * `rev` walks the actions backwards and returns a *superset* of catalog
    locations that may land at a place (a set, because folder merges have
    several). Candidates are always re-checked with `fwd`, so a loose superset
    costs time, never correctness.
  * Sub-move sources are the only places where an entry can leave its parent, so
    an entry whose name is not the name of any source simply follows its parent
    directory; only the others are mapped individually.

Staging errors are `WorkspaceError`; `code` is one of SRC_NOT_FOUND, DST_NOT_DIR,
INTO_ITSELF, NAME_EXISTS, NOOP, BAD_NAME (docs/WORKSPACE.md section A) or, for
requests that are not about the namespace, NOT_FOUND, NOT_OPEN, BAD_REQUEST,
NOTHING_TO_UNDO, NOTHING_TO_REDO.

Nothing here writes to a cloud or touches the catalog; `preview` only creates a
plan and has the simulator judge it, like every other plan source.
"""

from __future__ import annotations

import heapq
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Iterator, NamedTuple

from . import builders, db, identity, plans, simulate
from .models import Op

Loc = tuple[int, str]               # (remote_id, path); '' is a remote root
Sub = tuple[int, str, int, str]     # (src remote, src path, dst remote, dst path) of a moved root

_COLS = "e.id, e.remote_id, e.path, e.name, e.is_dir, e.size, e.mtime, e.category, e.content_group"
# Files whose content has another live copy anywhere (dir_stats.dup_files counts the same thing).
_DUP = ("CASE WHEN e.is_dir = 0 AND e.content_group IS NOT NULL THEN (SELECT COUNT(*) FROM entries d "
        "WHERE d.content_group = e.content_group AND d.present = 1 AND d.is_dir = 0) ELSE 0 END")
_CHUNK = 500
_GHOST_LIMIT = 2000


class WorkspaceError(ValueError):
    """A request the workspace refuses. `code` is machine-readable; str(e) is for people."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _q(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> sqlite3.Cursor:
    """Execute with sqlite3.Row results whatever the connection's row_factory is."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(sql, args)


def _join(base: str, name: str) -> str:
    return f"{base}/{name}" if base else name


def _parent(path: str) -> str:
    return path.rpartition("/")[0]


def _name(path: str) -> str:
    return path.rpartition("/")[2]


def _under(path: str, base: str) -> bool:
    """path is base or inside it, by whole path segments ('a/b' is not under 'a/bc')."""
    return path == base or path.startswith(base + "/")


def _ancestors(path: str) -> list[str]:
    """Strict ancestors, deepest first, always ending with '' (the root)."""
    out = []
    while path:
        path = path.rpartition("/")[0]
        out.append(path)
    return out


def _fold(s: str) -> str:
    """NFC + casefold: how case-insensitive remotes compare names."""
    return unicodedata.normalize("NFC", s).casefold()


def _natural_key(name: str) -> list:
    parts = re.split(r"([0-9]+)", name)
    return [int(t) if i % 2 else t.casefold() for i, t in enumerate(parts)]


def _clean(path: Any) -> str:
    return str(path or "").strip("/")


def _chunks(items: list, n: int = _CHUNK) -> Iterator[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not name or "/" in name or name in (".", "..") or "\x00" in name:
        raise WorkspaceError("BAD_NAME", f"{name!r} is not a valid file or folder name")
    return name


class _Remote(NamedTuple):
    id: int
    name: str
    provider: str
    writable: bool
    ci: bool


def _remotes(conn: sqlite3.Connection) -> dict[int, _Remote]:
    return {r["id"]: _Remote(r["id"], r["name"], r["provider"], r["write_remote"] is not None,
                             bool(r["case_insensitive"]))
            for r in _q(conn, "SELECT id, name, provider, write_remote, case_insensitive FROM remotes ORDER BY id")}


def _remote_by_name(rem: dict[int, _Remote], name: Any) -> _Remote | None:
    return next((r for r in rem.values() if r.name == name), None)


# --------------------------------------------------------------------------- #
# Catalog reads (memoised for one operation)
# --------------------------------------------------------------------------- #

class _Cat:
    """Catalog lookups for one operation; the catalog does not change while a view is computed."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._at: dict[Loc, list[sqlite3.Row]] = {}
        self._kids: dict[Loc, list[sqlite3.Row]] = {}
        self._dirs: dict[Loc, bool] = {}

    def at(self, rid: int, path: str) -> list[sqlite3.Row]:
        k = (rid, path)
        if k not in self._at:
            self._at[k] = _q(self.conn, f"SELECT {_COLS} FROM entries e WHERE e.remote_id = ? AND e.path = ? "
                                        f"AND e.present = 1", k).fetchall()
        return self._at[k]

    def children(self, rid: int, parent: str) -> list[sqlite3.Row]:
        k = (rid, parent)
        if k not in self._kids:
            self._kids[k] = _q(self.conn, f"SELECT {_COLS} FROM entries e WHERE e.remote_id = ? AND e.parent = ? "
                                          f"AND e.present = 1", k).fetchall()
        return self._kids[k]

    def named(self, rid: int, name_norm: str) -> list[sqlite3.Row]:
        return _q(self.conn, f"SELECT {_COLS} FROM entries e WHERE e.remote_id = ? AND e.name_norm = ? "
                             f"AND e.present = 1", (rid, name_norm)).fetchall()

    def kinds(self, rid: int, path: str) -> set[str]:
        return {"dir" if r["is_dir"] else "file" for r in self.at(rid, path)}

    def dir_exists(self, rid: int, path: str) -> bool:
        k = (rid, path)
        if k not in self._dirs:
            self._dirs[k] = any(r["is_dir"] for r in self.at(rid, path))
        return self._dirs[k]

    def subtree(self, rid: int, path: str) -> Iterator[sqlite3.Row]:
        """Live entries at or under path, with a 'gcount' column (live files sharing the content group)."""
        # '0' is the character after '/', so [path + '/', path + '0') is exactly the subtree and
        # stays on the (remote_id, path) index.
        return iter(_q(self.conn, f"SELECT {_COLS}, {_DUP} AS gcount FROM entries e WHERE e.remote_id = ? "
                                  f"AND e.present = 1 AND (e.path = ? OR (e.path >= ? AND e.path < ?)) "
                                  f"ORDER BY e.path", (rid, path, path + "/", path + "0")).fetchall())


def _dups(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    """Ids among `ids` whose content has another live copy."""
    out: set[int] = set()
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        out.update(r["id"] for r in _q(conn, f"SELECT e.id, {_DUP} AS g FROM entries e WHERE e.id IN ({ph})",
                                       tuple(chunk)) if r["g"] >= 2)
    return out


def _before_stats(conn: sqlite3.Connection, rid: int, paths: list[str]) -> dict[str, dict]:
    """dir_stats rows by path; a directory without a row counts as empty."""
    out: dict[str, dict] = {}
    for chunk in _chunks(paths):
        ph = ",".join("?" * len(chunk))
        for r in _q(conn, f"SELECT path, files, dirs, bytes, dup_files FROM dir_stats "
                          f"WHERE remote_id = ? AND path IN ({ph})", (rid, *chunk)):
            out[r["path"]] = {"files": r["files"], "dirs": r["dirs"], "bytes": r["bytes"],
                              "dup_files": r["dup_files"]}
    return out


_ZERO = {"files": 0, "dirs": 0, "bytes": 0, "dup_files": 0}


# --------------------------------------------------------------------------- #
# Actions and the virtual namespace
# --------------------------------------------------------------------------- #

@dataclass
class _Act:
    id: int
    seq: int
    kind: str                   # 'move' | 'mkdir'
    items: list[Loc]
    dst: Loc
    new_name: str | None
    note: str | None
    created_by: str
    state: str
    created_at: str


def _act(row: sqlite3.Row) -> _Act:
    items = [(i["remote_id"], i["path"]) for i in json.loads(row["items"])]
    return _Act(row["id"], row["seq"], row["kind"], items, (row["dst_remote_id"], row["dst_path"]),
                row["new_name"], row["note"], row["created_by"], row["state"], row["created_at"])


def _expand(items: list[Loc], dst: Loc, new_name: str | None) -> list[Sub]:
    """Sub-moves of a move action; items that already sit at their target are dropped."""
    out: list[Sub] = []
    for rs, ps in items:
        sub = (rs, ps, dst[0], _join(dst[1], new_name or _name(ps)))
        if sub[:2] != sub[2:]:
            out.append(sub)
    return out


def _prune(items: list[Loc]) -> list[Loc]:
    """Drop duplicates and items nested in another item: the ancestor carries them."""
    uniq = list(dict.fromkeys(items))
    have = set(uniq)
    return [(r, p) for r, p in uniq if not any((r, a) in have for a in _ancestors(p))]


class _Origin(NamedTuple):
    """Something that lands at a final path: a catalog entry, or a directory made by mkdir."""
    kind: str                       # 'file' | 'dir' | 'virtual'
    row: sqlite3.Row | None
    loc: Loc                        # where it lives before the draft (a mkdir's creation spot)

    @property
    def key(self) -> tuple:
        return ("v", self.loc) if self.row is None else ("e", self.row["id"])


def _origin(row: sqlite3.Row) -> _Origin:
    return _Origin("dir" if row["is_dir"] else "file", row, (row["remote_id"], row["path"]))


class _Moved(NamedTuple):
    """A live catalog entry that ends up somewhere else."""
    id: int
    rid: int
    path: str
    is_dir: bool
    size: int
    dup: bool
    frid: int
    fpath: str
    aseq: int                       # seq of the last action that moved it
    root: Loc                       # top of the moved subtree it travels with
    key: str = ""                   # content identity (identity.content_key)


class _Space:
    """The namespace after a list of actions: forward map, reverse candidates, listings.

    Mutable on purpose: validation appends one action at a time and rolls back
    on failure. Nothing derived is cached, so appending is always safe.
    """

    def __init__(self, cat: _Cat):
        self.cat = cat
        # (rs, ps, rd, pd, action seq, ps + '/', pd + '/')
        self.moves: list[tuple[int, str, int, str, int, str, str]] = []
        self.mkdirs: list[tuple[int, int, str, int]] = []      # (moves applied before it, remote, path, action seq)

    # ------------------------------------------------------------- building

    def apply(self, act: _Act) -> None:
        if act.kind == "mkdir":
            self.mkdirs.append((len(self.moves), act.dst[0], _join(act.dst[1], act.new_name or ""), act.seq))
        else:
            for rs, ps, rd, pd in _expand(act.items, act.dst, act.new_name):
                self.moves.append((rs, ps, rd, pd, act.seq, ps + "/", pd + "/"))

    def push(self, sub: Sub, seq: int) -> None:
        self.moves.append((*sub, seq, sub[1] + "/", sub[3] + "/"))

    def mark(self) -> tuple[int, int]:
        return len(self.moves), len(self.mkdirs)

    def rollback(self, mark: tuple[int, int]) -> None:
        del self.moves[mark[0]:]
        del self.mkdirs[mark[1]:]

    def src_names(self) -> set[str]:
        return {_name(m[1]) for m in self.moves}

    # ------------------------------------------------------------ maps

    def _fwd(self, r: int, p: str, start: int = 0) -> tuple[int, str, int]:
        """Where location (r, p) ends up, and the index of the last sub-move that touched it (-1: none)."""
        last = -1
        mv = self.moves
        for i in range(start, len(mv)):
            rs, ps, rd, pd, _, spfx, _ = mv[i]
            if r == rs and (p == ps or p.startswith(spfx)):
                r, p, last = rd, pd + p[len(ps):], i
        return r, p, last

    def fwd(self, r: int, p: str, start: int = 0) -> Loc:
        return self._fwd(r, p, start)[:2]

    def rev(self, r: int, p: str) -> set[Loc]:
        """Catalog locations whose content may land at (r, p): a superset, one per merged origin."""
        cands: set[Loc] = {(r, p)}
        for rs, ps, rd, pd, _, spfx, dpfx in reversed(self.moves):
            new: set[Loc] = set()
            for cr, cp in cands:
                if cr == rd and (cp == pd or cp.startswith(dpfx)):
                    new.add((rs, ps + cp[len(pd):]))
                if not (cr == rs and (cp == ps or cp.startswith(spfx))):
                    new.add((cr, cp))
            cands = new
        return cands

    # ------------------------------------------------------------ lookups

    def origins_at(self, r: int, p: str) -> list[_Origin]:
        """Everything whose final location is exactly (r, p)."""
        out = []
        for cr, cp in sorted(self.rev(r, p)):
            if self.fwd(cr, cp) == (r, p):
                out.extend(_origin(row) for row in self.cat.at(cr, cp))
        for pos, mr, mp, _ in self.mkdirs:
            if self.fwd(mr, mp, pos) == (r, p):
                out.append(_Origin("virtual", None, (mr, mp)))
        return out

    def kinds_at(self, r: int, p: str) -> set[str]:
        if p == "":
            return {"dir"}
        return {"file" if o.kind == "file" else "dir" for o in self.origins_at(r, p)}

    def children(self, r: int, d: str) -> dict[str, dict[tuple, _Origin]]:
        """After-view children of directory (r, d): final path -> its origins (several = merged)."""
        names = self.src_names()
        groups: dict[str, dict[tuple, _Origin]] = {}

        def add(z: str, o: _Origin) -> None:
            groups.setdefault(z, {})[o.key] = o

        # Entries that travel with a directory whose content may land in d ...
        for cr, cp in sorted(self.rev(r, d)):
            fr, fp, _ = self._fwd(cr, cp)
            for row in self.cat.children(cr, cp):
                n = row["name"]
                zr, zp = self.fwd(cr, row["path"]) if n in names else (fr, _join(fp, n))
                if zr == r and _parent(zp) == d:
                    add(zp, _origin(row))
        # ... plus moved roots and new folders, which may come from anywhere.
        finals: set[str] = set()
        for i, m in enumerate(self.moves):
            zr, zp = self.fwd(m[2], m[3], i + 1)
            if zr == r and _parent(zp) == d:
                finals.add(zp)
        for pos, mr, mp, _ in self.mkdirs:
            zr, zp = self.fwd(mr, mp, pos)
            if zr == r and _parent(zp) == d:
                finals.add(zp)
        for z in finals:
            for o in self.origins_at(r, z):
                add(z, o)
        return groups

    def ghosts(self, r: int, d: str) -> list[sqlite3.Row]:
        """Catalog children of (r, d) that the draft moved or renamed away."""
        names = self.src_names()
        fr, fp, _ = self._fwd(r, d)
        out = []
        for row in self.cat.children(r, d):
            n = row["name"]
            z = self.fwd(r, row["path"]) if n in names else (fr, _join(fp, n))
            if z != (r, row["path"]):
                out.append(row)
        return out

    # ------------------------------------------------------------ moved set

    def moved(self) -> list[_Moved]:
        """M: every live catalog entry whose final location differs from where it is now.

        An entry can only leave its parent by being a sub-move source itself, so
        enumerating the subtrees under all (pruned) source paths covers every
        moved entry, and each distinct parent directory is mapped once.
        """
        if not self.moves:
            return []
        names = self.src_names()
        roots: list[Loc] = []
        seen: set[Loc] = set()
        for r, p in sorted({(m[0], m[1]) for m in self.moves}):
            if not any((r, a) in seen for a in _ancestors(p)):
                seen.add((r, p))
                roots.append((r, p))

        cache: dict[Loc, tuple[int, str, int]] = {}

        def full(loc: Loc) -> tuple[int, str, int]:
            v = cache.get(loc)
            if v is None:
                v = cache[loc] = self._fwd(*loc)
            return v

        top: dict[Loc, Loc] = {}

        def root_of(loc: Loc) -> Loc:
            """The top location of the moved subtree that carries directory `loc`."""
            chain = []
            while loc not in top:
                rid, p = loc
                fl, fp = full(loc), full((rid, _parent(p)))
                if p and fl[:2] == (fp[0], _join(fp[1], _name(p))):
                    chain.append(loc)
                    loc = (rid, _parent(p))
                else:
                    top[loc] = loc
            for c in chain:
                top[c] = top[loc]
            return top[loc]

        out: list[_Moved] = []
        for r, root in roots:
            for row in self.cat.subtree(r, root):
                rid, p = row["remote_id"], row["path"]
                par = (rid, _parent(p))
                fpar = full(par)
                if _name(p) in names:
                    f = self._fwd(rid, p)
                else:
                    f = (fpar[0], _join(fpar[1], _name(p)), fpar[2])
                if f[:2] == (rid, p):
                    continue
                follows = f[:2] == (fpar[0], _join(fpar[1], _name(p)))
                out.append(_Moved(row["id"], rid, p, bool(row["is_dir"]), row["size"] or 0, row["gcount"] >= 2,
                                  f[0], f[1], self.moves[f[2]][4], root_of(par) if follows else (rid, p),
                                  identity.content_key(row)))
        return out


def _occupants(cat: _Cat, rem: dict[int, _Remote], frid: int, fpath: str) -> list[sqlite3.Row]:
    """Live catalog entries sitting at a final path (compared as the remote does: exact, or NFC+casefold)."""
    if rem[frid].ci:
        return [o for o in cat.named(frid, _fold(_name(fpath))) if _fold(o["path"]) == _fold(fpath)]
    return cat.at(frid, fpath)


def _dedupe_victims(space: _Space, rem: dict[int, _Remote], moved: list[_Moved]) -> dict[int, Loc]:
    """Moved files whose final spot already holds identical content.

    The plan does not copy these over the occupant: it deletes them with the occupant as witness
    (build_ops). They leave their old folder but add nothing to the folder they were dragged into, so
    the after-view counts must skip them or a merged folder would claim files it does not list.
    Mirrors build_ops' rule: a spot taken by an identical unmoved file, or by an identical file moved
    there first, makes the later one a victim; a different file there is a conflict, not a dedupe.
    Returns {file id: the spot where its twin stays}.
    """
    files = sorted((m for m in moved if not m.is_dir), key=lambda m: (m.rid, m.path, m.id))
    if not files:
        return {}
    cat = space.cat
    moving = {m.id for m in files}
    probe: dict[Loc, bool] = {}     # moved root -> does something already sit where it lands?

    def landing_is_occupied(m: _Moved) -> bool:
        """Cheap gate: a file travelling with a folder that lands on a spot nothing occupies cannot collide."""
        if m.root == (m.rid, m.path):
            return True
        v = probe.get(m.root)
        if v is None:
            fr, fp = space.fwd(*m.root)
            v = probe[m.root] = any(o["is_dir"] for o in _occupants(cat, rem, fr, fp))
        return v

    victims: dict[int, Loc] = {}
    taken: dict[tuple[int, str], str] = {}
    for m in files:
        ci = rem[m.frid].ci
        slot = (m.frid, _fold(m.fpath) if ci else m.fpath)
        peers: list[sqlite3.Row] = []
        blocked = False
        if landing_is_occupied(m):
            occ = [o for o in _occupants(cat, rem, m.frid, m.fpath) if o["is_dir"] or o["id"] not in moving]
            blocked = any(o["is_dir"] for o in occ)
            peers = [o for o in occ if not o["is_dir"]]
        if blocked:
            continue
        keys = {identity.content_key(o) for o in peers}
        if slot in taken:
            keys.add(taken[slot])
        if keys == {m.key}:
            victims[m.id] = (m.frid, m.fpath)
        elif not keys:
            taken[slot] = m.key
    return victims


# --------------------------------------------------------------------------- #
# Additive after-view statistics
# --------------------------------------------------------------------------- #

class _Delta:
    """Σ over the moved set, indexed by directory, for after(F) = before(F) − out(F) + in(F).

    out(F): moved files whose origin is under F and whose final location is not;
    in(F): the converse, except files that a merge deletes as identical to what is already
    there (dedupe victims), which arrive nowhere. Only the ancestors that differ between
    origin and final can change, so the shared ones are skipped. Directory counts work on
    distinct locations, because merged folders are one folder.
    """

    def __init__(self, space: _Space, moved: list[_Moved], victims: dict[int, Loc] | None = None):
        if victims is None:
            victims = _dedupe_victims(space, _remotes(space.cat.conn), moved)
        self.out: dict[Loc, list[int]] = {}
        self.inn: dict[Loc, list[int]] = {}
        self.dup_adj: dict[Loc, int] = {}
        self.dgain: dict[Loc, int] = {}
        self.dlose: dict[Loc, int] = {}
        dold: set[Loc] = set()
        dnew: set[Loc] = set()
        for m in moved:
            if m.is_dir:
                dold.add((m.rid, m.path))
                dnew.add((m.frid, m.fpath))
                continue
            a0, a1 = _ancestors(m.path), _ancestors(m.fpath)
            if m.id in victims:
                a1 = []         # it is deleted: every folder that held it loses it, even one that also holds its twin
            elif m.rid == m.frid:
                while a0 and a1 and a0[-1] == a1[-1]:
                    a0.pop()
                    a1.pop()
            for table, rid, anc in ((self.out, m.rid, a0), (self.inn, m.frid, a1)):
                for a in anc:
                    t = table.setdefault((rid, a), [0, 0, 0])
                    t[0] += 1
                    t[1] += m.size
                    t[2] += m.dup
        self._survivors_stop_being_duplicates(space.cat.conn, moved, victims)
        for pos, mr, mp, _ in space.mkdirs:
            dnew.add(space.fwd(mr, mp, pos))
        # A location stays a directory when something still lands on it; a new one counts once.
        for loc in dold - dnew:
            for a in _ancestors(loc[1]):
                self.dlose[(loc[0], a)] = self.dlose.get((loc[0], a), 0) + 1
        for loc in dnew - dold:
            if not space.cat.dir_exists(*loc):
                for a in _ancestors(loc[1]):
                    self.dgain[(loc[0], a)] = self.dgain.get((loc[0], a), 0) + 1

    def _survivors_stop_being_duplicates(self, conn: sqlite3.Connection, moved: list[_Moved],
                                         victims: dict[int, Loc]) -> None:
        """A merge that deletes copies can leave one file with no other copy: it is no longer a duplicate.

        The copies of a content group that survive are N (live files now) minus its victims; at one,
        the survivor sits where the victims landed and every folder around it stops counting it.
        """
        by_group: dict[str, list[Loc]] = {}
        for m in moved:
            if m.id in victims and m.key.startswith("g:"):
                by_group.setdefault(m.key, []).append(victims[m.id])
        for key, spots in by_group.items():
            n = conn.execute("SELECT COUNT(*) FROM entries WHERE content_group = ? AND present = 1 AND is_dir = 0",
                             (int(key[2:]),)).fetchone()[0]
            if n - len(spots) <= 1:
                rid, path = spots[0]
                for a in _ancestors(path):
                    self.dup_adj[(rid, a)] = self.dup_adj.get((rid, a), 0) - 1

    def apply(self, before: dict, rid: int, path: str) -> dict:
        o = self.out.get((rid, path), (0, 0, 0))
        i = self.inn.get((rid, path), (0, 0, 0))
        return {
            "files": max(0, before["files"] - o[0] + i[0]),
            "dirs": max(0, before["dirs"] - self.dlose.get((rid, path), 0) + self.dgain.get((rid, path), 0)),
            "bytes": max(0, before["bytes"] - o[1] + i[1]),
            "dup_files": max(0, before["dup_files"] - o[2] + i[2] + self.dup_adj.get((rid, path), 0)),
        }


def _summary(moved: list[_Moved], rem: dict[int, _Remote]) -> dict:
    files = [m for m in moved if not m.is_dir]
    touched = {m.rid for m in files} | {m.frid for m in files}
    return {
        "moved_files": len(files),
        "moved_bytes": sum(m.size for m in files),
        "cross_remote_bytes": sum(m.size for m in files if m.rid != m.frid),
        "remotes_touched": sorted(rem[i].name for i in touched if i in rem),
    }


# --------------------------------------------------------------------------- #
# Staging validation
# --------------------------------------------------------------------------- #

def _need_dir(space: _Space, loc: Loc) -> None:
    if loc[1] and "dir" not in space.kinds_at(*loc):
        raise WorkspaceError("DST_NOT_DIR", f"destination {loc[1]!r} is not a folder")


def _fmt(rem: dict[int, _Remote], loc: Loc) -> str:
    return f"{rem[loc[0]].name}:{loc[1]}"


def _stage(space: _Space, rem: dict[int, _Remote], act: _Act) -> list[dict]:
    """Validate `act` against the after-view of `space` and append it. Returns the warnings.

    On WorkspaceError the space is left as it was.
    """
    mark = space.mark()
    try:
        return _stage_checked(space, rem, act)
    except WorkspaceError:
        space.rollback(mark)
        raise


def _case_clash(space: _Space, rem: dict[int, _Remote], dst: Loc, name: str, target: str, own: str | None) -> bool:
    """A differently-cased sibling with the same folded name already sits in the destination."""
    if not rem[dst[0]].ci:
        return False
    folded = _fold(name)
    return any(_fold(_name(z)) == folded and z != target and z != own
               for z in space.children(dst[0], dst[1]))


def _stage_checked(space: _Space, rem: dict[int, _Remote], act: _Act) -> list[dict]:
    warns: list[dict] = []

    def warn(code: str, message: str) -> None:
        warns.append({"code": code, "message": message})

    dst = act.dst
    if act.kind == "mkdir":
        name = _check_name(act.new_name)
        _need_dir(space, dst)
        loc = (dst[0], _join(dst[1], name))
        kinds = space.kinds_at(*loc)
        if "file" in kinds:
            raise WorkspaceError("NAME_EXISTS", f"{_fmt(rem, loc)!r} is already a file")
        if kinds:
            raise WorkspaceError("NOOP", f"folder {_fmt(rem, loc)!r} already exists")
        if _case_clash(space, rem, dst, name, loc[1], None):
            warn("CASE_COLLISION", f"{rem[dst[0]].name} ignores case: a folder named like {name!r} already exists")
        space.apply(act)
        if not rem[dst[0]].writable:
            warn("READ_ONLY", f"{rem[dst[0]].name} has no write access; the plan will not pass simulation")
        return warns

    if act.new_name is not None:
        _check_name(act.new_name)
        if len(act.items) != 1:
            raise WorkspaceError("BAD_NAME", "new_name renames exactly one item")
    if not act.items:
        raise WorkspaceError("BAD_REQUEST", "a move needs at least one item")
    kinds = []
    for rs, ps in act.items:
        k = space.kinds_at(rs, ps) if ps else set()
        if not k:
            raise WorkspaceError("SRC_NOT_FOUND", f"{_fmt(rem, (rs, ps))!r} does not exist"
                                 + (" (a remote root cannot be moved)" if not ps else ""))
        kinds.append(k)
    _need_dir(space, dst)
    for (rs, ps), k in zip(act.items, kinds):
        if "dir" in k and rs == dst[0] and _under(dst[1], ps):
            raise WorkspaceError("INTO_ITSELF", f"cannot move {_fmt(rem, (rs, ps))!r} into itself")

    real = 0
    remotes_used = {dst[0]}
    for (rs, ps), k in zip(act.items, kinds):
        name = act.new_name or _name(ps)
        target = _join(dst[1], name)
        if (rs, ps) == (dst[0], target):
            continue
        occupied = space.kinds_at(dst[0], target)
        if occupied:
            if "dir" not in k or "file" in occupied:
                raise WorkspaceError("NAME_EXISTS", f"{_fmt(rem, (dst[0], target))!r} already exists")
            warn("MERGE", f"folder {_fmt(rem, (rs, ps))!r} merges into the existing {_fmt(rem, (dst[0], target))!r}")
        if _case_clash(space, rem, dst, name, target, ps if rs == dst[0] else None):
            warn("CASE_COLLISION", f"{rem[dst[0]].name} ignores case: {name!r} differs only by case from "
                                   f"an existing name in {_fmt(rem, dst)!r}")
        space.push((rs, ps, dst[0], target), act.seq)
        remotes_used.add(rs)
        real += 1
    if not real:
        raise WorkspaceError("NOOP", "every item is already where it would be moved to")

    for rid in sorted(remotes_used):
        if not rem[rid].writable:
            warn("READ_ONLY", f"{rem[rid].name} has no write access; the plan will not pass simulation")
    others = [rs for rs, _ in act.items if rs != dst[0]]
    if others:
        warn("CROSS_REMOTE", f"{len(others)} item(s) move between remotes: their bytes are copied through "
                             f"this machine, then the originals are quarantined")
    return warns


# --------------------------------------------------------------------------- #
# Draft store
# --------------------------------------------------------------------------- #

def _draft_row(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row:
    row = _q(conn, "SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise WorkspaceError("NOT_FOUND", f"no draft {draft_id}")
    return row


def _open_draft(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row:
    row = _draft_row(conn, draft_id)
    if row["status"] != "open":
        raise WorkspaceError("NOT_OPEN", f"draft {draft_id} is {row['status']}")
    return row


def _acts(conn: sqlite3.Connection, draft_id: int, state: str | None = None) -> list[_Act]:
    sql = "SELECT * FROM draft_actions WHERE draft_id = ?" + (" AND state = ?" if state else "") + " ORDER BY seq"
    return [_act(r) for r in _q(conn, sql, (draft_id, state) if state else (draft_id,))]


def _bump(conn: sqlite3.Connection, draft_id: int) -> int:
    return _q(conn, "UPDATE drafts SET version = version + 1, updated_at = ? WHERE id = ? RETURNING version",
              (db.utcnow(), draft_id)).fetchone()[0]


def _space_for(cat: _Cat, acts: list[_Act]) -> _Space:
    space = _Space(cat)
    for a in acts:
        if a.state == "active":
            space.apply(a)
    return space


def _action_dict(a: _Act, rem: dict[int, _Remote]) -> dict:
    return {
        "id": a.id, "seq": a.seq, "kind": a.kind,
        "items": [{"remote": rem[r].name, "path": p} for r, p in a.items],
        "dst": {"remote": rem[a.dst[0]].name, "path": a.dst[1]},
        "new_name": a.new_name, "note": a.note, "created_by": a.created_by,
        "state": a.state, "created_at": a.created_at,
    }


def create_draft(conn: sqlite3.Connection, title: str) -> dict:
    now = db.utcnow()
    draft_id = _q(conn, "INSERT INTO drafts(title, status, created_at, updated_at) VALUES (?, 'open', ?, ?) "
                        "RETURNING id", ((title or "").strip() or "Untitled", now, now)).fetchone()[0]
    return get_draft(conn, draft_id)


def list_drafts(conn: sqlite3.Connection, *, status: str | None = "open") -> list[dict]:
    sql = ("SELECT d.*, (SELECT COUNT(*) FROM draft_actions a WHERE a.draft_id = d.id AND a.state = 'active') AS n "
           "FROM drafts d" + (" WHERE d.status = ?" if status else "") + " ORDER BY d.id DESC")
    return [{"id": r["id"], "title": r["title"], "status": r["status"], "version": r["version"],
             "preview_plan_id": r["preview_plan_id"], "preview_version": r["preview_version"],
             "created_at": r["created_at"], "updated_at": r["updated_at"], "action_count": r["n"]}
            for r in _q(conn, sql, (status,) if status else ())]


def get_draft(conn: sqlite3.Connection, draft_id: int) -> dict:
    d = _draft_row(conn, draft_id)
    rem = _remotes(conn)
    acts = _acts(conn, draft_id)
    space = _space_for(_Cat(conn), acts)
    return {
        "id": d["id"], "title": d["title"], "status": d["status"], "version": d["version"],
        "preview_plan_id": d["preview_plan_id"], "preview_version": d["preview_version"],
        "actions": [_action_dict(a, rem) for a in acts],
        "undo_available": any(a.state == "active" for a in acts),
        "redo_available": any(a.state == "undone" for a in acts),
        "summary": _summary(space.moved(), rem),
    }


def discard_draft(conn: sqlite3.Connection, draft_id: int) -> None:
    """Close a draft without applying it. The version bump makes any preview of it stale."""
    d = _draft_row(conn, draft_id)
    if d["status"] == "discarded":
        return
    if d["status"] != "open":
        raise WorkspaceError("NOT_OPEN", f"draft {draft_id} is {d['status']}")
    with db.transaction(conn):
        conn.execute("UPDATE drafts SET status = 'discarded' WHERE id = ?", (draft_id,))
        _bump(conn, draft_id)


def mark_applied(conn: sqlite3.Connection, draft_id: int) -> None:
    """Record that the draft's plan was executed; the draft becomes read-only."""
    _open_draft(conn, draft_id)
    conn.execute("UPDATE drafts SET status = 'applied', updated_at = ? WHERE id = ?", (db.utcnow(), draft_id))


def _parse_action(action: Any, rem: dict[int, _Remote]) -> _Act:
    """HTTP-style body (remote names) -> _Act (remote ids), items pruned of nested duplicates."""
    if not isinstance(action, dict) or action.get("kind") not in ("move", "mkdir"):
        raise WorkspaceError("BAD_REQUEST", "action.kind must be 'move' or 'mkdir'")
    kind = action["kind"]

    def loc(spec: Any, code: str) -> Loc:
        r = _remote_by_name(rem, spec.get("remote")) if isinstance(spec, dict) else None
        if r is None:
            raise WorkspaceError(code, f"unknown remote in {spec!r}")
        return r.id, _clean(spec.get("path"))

    items: list[Loc] = []
    if kind == "move":
        raw = action.get("items")
        if not isinstance(raw, list) or not raw:
            raise WorkspaceError("BAD_REQUEST", "a move needs a non-empty items list")
        items = [loc(i, "SRC_NOT_FOUND") for i in raw]
        if any(p == "" for _, p in items):
            raise WorkspaceError("SRC_NOT_FOUND", "a remote root cannot be moved")
        items = _prune(items)
    note = action.get("note")
    new_name = action.get("new_name")
    if kind == "mkdir" and new_name is None:
        raise WorkspaceError("BAD_NAME", "mkdir needs new_name")
    return _Act(0, 0, kind, items, loc(action.get("dst"), "DST_NOT_DIR"), new_name,
                None if note is None else str(note), "", "active", "")


def add_action(conn: sqlite3.Connection, draft_id: int, action: dict, *, created_by: str) -> dict:
    """Stage one gesture. Validation runs against the current after-view; staging drops the redo stack."""
    rem = _remotes(conn)
    act = _parse_action(action, rem)
    with db.transaction(conn):
        _open_draft(conn, draft_id)
        space = _space_for(_Cat(conn), _acts(conn, draft_id))
        conn.execute("DELETE FROM draft_actions WHERE draft_id = ? AND state = 'undone'", (draft_id,))
        act.seq = _q(conn, "SELECT COALESCE(MAX(seq), 0) + 1 FROM draft_actions WHERE draft_id = ?",
                     (draft_id,)).fetchone()[0]
        warnings = _stage(space, rem, act)
        act.created_by, act.created_at = created_by, db.utcnow()
        act.id = conn.execute(
            "INSERT INTO draft_actions(draft_id, seq, kind, items, dst_remote_id, dst_path, new_name, note, "
            "created_by, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?) RETURNING id",
            (draft_id, act.seq, act.kind, json.dumps([{"remote_id": r, "path": p} for r, p in act.items]),
             act.dst[0], act.dst[1], act.new_name, act.note, created_by, act.created_at)).fetchone()[0]
        version = _bump(conn, draft_id)
    return {"action": _action_dict(act, rem), "draft_version": version, "warnings": warnings}


def undo(conn: sqlite3.Connection, draft_id: int) -> dict:
    with db.transaction(conn):
        _open_draft(conn, draft_id)
        top = _q(conn, "SELECT id FROM draft_actions WHERE draft_id = ? AND state = 'active' "
                       "ORDER BY seq DESC LIMIT 1", (draft_id,)).fetchone()
        if top is None:
            raise WorkspaceError("NOTHING_TO_UNDO", "nothing to undo")
        conn.execute("UPDATE draft_actions SET state = 'undone' WHERE id = ?", (top["id"],))
        _bump(conn, draft_id)
    return get_draft(conn, draft_id)


def _revalidate(conn: sqlite3.Connection, space: _Space, rem: dict[int, _Remote],
                later: list[_Act]) -> list[int]:
    """Re-stage `later` actions on top of `space` in order; mark the ones that no longer validate undone."""
    dropped = []
    for a in later:
        try:
            _stage(space, rem, a)
        except WorkspaceError:
            conn.execute("UPDATE draft_actions SET state = 'undone' WHERE id = ?", (a.id,))
            dropped.append(a.id)
    return dropped


def redo(conn: sqlite3.Connection, draft_id: int) -> dict:
    """Re-activate the lowest-seq undone action, re-validating it and everything after it."""
    rem = _remotes(conn)
    with db.transaction(conn):
        _open_draft(conn, draft_id)
        acts = _acts(conn, draft_id)
        target = next((a for a in acts if a.state == "undone"), None)
        if target is None:
            raise WorkspaceError("NOTHING_TO_REDO", "nothing to redo")
        space = _space_for(_Cat(conn), [a for a in acts if a.seq < target.seq])
        _stage(space, rem, target)          # raises if the catalog or earlier edits no longer allow it
        conn.execute("UPDATE draft_actions SET state = 'active' WHERE id = ?", (target.id,))
        _revalidate(conn, space, rem, [a for a in acts if a.seq > target.seq and a.state == "active"])
        _bump(conn, draft_id)
    return get_draft(conn, draft_id)


def remove_action(conn: sqlite3.Connection, draft_id: int, action_id: int) -> dict:
    """Take one action out of the draft; later actions that depended on it are undone too."""
    rem = _remotes(conn)
    with db.transaction(conn):
        _open_draft(conn, draft_id)
        acts = _acts(conn, draft_id)
        target = next((a for a in acts if a.id == action_id), None)
        if target is None:
            raise WorkspaceError("NOT_FOUND", f"draft {draft_id} has no action {action_id}")
        removed = [target.id]
        if target.state == "active":
            conn.execute("UPDATE draft_actions SET state = 'undone' WHERE id = ?", (target.id,))
            space = _space_for(_Cat(conn), [a for a in acts if a.seq < target.seq])
            removed += _revalidate(conn, space, rem,
                                   [a for a in acts if a.seq > target.seq and a.state == "active"])
        version = _bump(conn, draft_id)
    return {"draft_version": version, "removed": removed}


# --------------------------------------------------------------------------- #
# Listings
# --------------------------------------------------------------------------- #

def _rid(rem: dict[int, _Remote], name: str) -> int:
    r = _remote_by_name(rem, name)
    if r is None:
        raise WorkspaceError("NOT_FOUND", f"unknown remote {name!r}")
    return r.id


def _node(r: _Remote, path: str) -> dict:
    return {"name": _name(path) or r.name, "path": path, "remote": r.name, "is_dir": True, "size": None,
            "mtime": None, "category": "other", "files": 0, "dirs": 0, "bytes": 0, "dup_files": 0,
            "entry_id": None, "origins": [], "moved_here": False, "merged": False, "conflict": False,
            "dedupe": False, "virtual": False}


def _group_item(r: _Remote, rem: dict[int, _Remote], z: str, origins: list[_Origin]) -> dict:
    """One row of a listing from everything that lands at final path z."""
    dirs = [o for o in origins if o.kind != "file"]
    files = [o for o in origins if o.kind == "file"]
    keys = {identity.content_key(o.row) for o in files}
    real = sorted({o.loc for o in origins if o.row is not None})
    item = _node(r, z)
    item["is_dir"] = bool(dirs)
    item["origins"] = [{"remote": rem[lr].name, "path": lp} for lr, lp in real]
    item["moved_here"] = any(loc != (r.id, z) for loc in real)
    item["merged"] = len({o.loc for o in dirs}) >= 2
    item["conflict"] = len(keys) > 1 or bool(dirs and files)
    item["dedupe"] = len(files) >= 2 and len(keys) == 1 and not dirs
    item["virtual"] = bool(origins) and all(o.row is None for o in origins)
    if not dirs:
        lead = min(files, key=lambda o: (o.loc != (r.id, z), o.loc))
        item.update(size=lead.row["size"], mtime=lead.row["mtime"], category=lead.row["category"],
                    entry_id=lead.row["id"])
    else:
        lead_dirs = [o for o in dirs if o.row is not None]
        if lead_dirs:
            item["mtime"] = min(lead_dirs, key=lambda o: o.loc).row["mtime"]
    return item


def _finish_items(conn: sqlite3.Connection, r: _Remote, items: list[dict], space: _Space | None) -> None:
    """Fill files/dirs/bytes/dup_files for one page of items; the after-view delta is only built if a folder shows."""
    dirs = [i["path"] for i in items if i["is_dir"]]
    before = _before_stats(conn, r.id, dirs)
    delta = _Delta(space, space.moved()) if dirs and space is not None and (space.moves or space.mkdirs) else None
    dup = _dups(conn, [i["entry_id"] for i in items if i["entry_id"] is not None])
    for i in items:
        if i["is_dir"]:
            b = before.get(i["path"], _ZERO)
            i.update(delta.apply(b, r.id, i["path"]) if delta else b)
        else:
            i.update(files=1, dirs=0, bytes=i["size"] or 0, dup_files=int(i["entry_id"] in dup))


def _sort(items: list[dict]) -> None:
    items.sort(key=lambda i: (not i["is_dir"], _natural_key(i["name"]), i["name"]))


def list_dir(conn: sqlite3.Connection, remote: str, path: str, *, draft_id: int | None = None,
             view: str = "after", ghosts: bool = False, limit: int = 500, offset: int = 0) -> dict:
    """Children of one directory, in the after-view of a draft (default) or as the catalog has them."""
    if view not in ("after", "before"):
        raise WorkspaceError("BAD_REQUEST", "view must be 'after' or 'before'")
    rem = _remotes(conn)
    rid = _rid(rem, remote)
    r = rem[rid]
    path = _clean(path)
    version = _draft_row(conn, draft_id)["version"] if draft_id is not None else None
    limit, offset = max(0, int(limit)), max(0, int(offset))
    cat = _Cat(conn)
    ghost_rows: list[dict] = []
    space: _Space | None = None

    if draft_id is None or view == "before":
        items = [_group_item(r, rem, row["path"], [_origin(row)]) for row in cat.children(rid, path)]
    else:
        space = _space_for(cat, _acts(conn, draft_id))
        items = [_group_item(r, rem, z, sorted(g.values(), key=lambda o: o.loc))
                 for z, g in space.children(rid, path).items()]
        if ghosts:
            for row in space.ghosts(rid, path)[:_GHOST_LIMIT]:
                fr, fp = space.fwd(rid, row["path"])
                ghost_rows.append({"name": row["name"], "path": row["path"], "is_dir": bool(row["is_dir"]),
                                   "moved_to": {"remote": rem[fr].name, "path": fp}})
            ghost_rows.sort(key=lambda g: (not g["is_dir"], _natural_key(g["name"]), g["name"]))
    _sort(items)
    page = items[offset:offset + limit]
    _finish_items(conn, r, page, space)
    return {"remote": remote, "path": path, "view": view, "draft_version": version,
            "items": page, "ghosts": ghost_rows, "total": len(items)}


def roots(conn: sqlite3.Connection, *, draft_id: int | None = None) -> list[dict]:
    """One node per remote with its stats (after-view when a draft is given)."""
    rem = _remotes(conn)
    delta = None
    if draft_id is not None:
        _draft_row(conn, draft_id)
        space = _space_for(_Cat(conn), _acts(conn, draft_id))
        if space.moves or space.mkdirs:
            delta = _Delta(space, space.moved())
    out = []
    for r in rem.values():
        node = _node(r, "")
        b = _before_stats(conn, r.id, [""]).get("", _ZERO)
        node.update(delta.apply(b, r.id, "") if delta else b)
        node.update(remote_id=r.id, provider=r.provider, writable=r.writable, case_insensitive=r.ci)
        out.append(node)
    return out


# --------------------------------------------------------------------------- #
# Draft -> plan
# --------------------------------------------------------------------------- #

@dataclass
class _Prim:
    """A move or copy op under construction (the file's net effect)."""
    m: _Moved
    row: sqlite3.Row
    kind: str                       # 'move' | 'copy'
    pos: int = -1                   # final index in the op list


def _fetch_rows(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    out: dict[int, sqlite3.Row] = {}
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        out.update((r["id"], r) for r in _q(conn, f"SELECT * FROM entries WHERE id IN ({ph})", tuple(chunk)))
    return out


def build_ops(conn: sqlite3.Connection, draft_id: int) -> tuple[list[Op], dict]:
    """Net effect of the active actions as plan ops, plus what the preview should show.

    A file whose final spot holds identical content is not copied over it: it is
    deleted with that occupant as witness (dedupe on merge). Different content
    is a conflict; the op is still emitted so the simulator reports DST_EXISTS.
    """
    d = _draft_row(conn, draft_id)
    version = d["version"]
    rem = _remotes(conn)
    cat = _Cat(conn)
    space = _space_for(cat, _acts(conn, draft_id))
    moved = space.moved()
    files = sorted((m for m in moved if not m.is_dir), key=lambda m: (m.rid, m.path, m.id))
    rows = _fetch_rows(conn, [m.id for m in files])
    moving = {m.id for m in files}

    def nk(rid: int, path: str) -> str:
        return _fold(path) if rem[rid].ci else path

    def occupants(frid: int, fpath: str) -> list[sqlite3.Row]:
        return _occupants(cat, rem, frid, fpath)

    prims: list[_Prim] = []
    dedupes: list[tuple[_Moved, sqlite3.Row, Loc, _Prim | None]] = []   # file, row, witness, witness op
    conflicts: list[dict] = []
    deduped: set[int] = set()
    taken: dict[tuple[int, str], _Prim] = {}

    def conflict(m: _Moved, code: str, reason: str, other: Loc | None) -> None:
        conflicts.append({"code": code, "reason": reason, "from": {"remote": rem[m.rid].name, "path": m.path},
                          "to": {"remote": rem[m.frid].name, "path": m.fpath},
                          "with": None if other is None else {"remote": rem[other[0]].name, "path": other[1]}})

    landing = {(m.frid, nk(m.frid, m.fpath)) for m in files}

    def file_in_the_way(m: _Moved) -> Loc | None:
        """A file (in the catalog and staying, or landing there too) sits where this file needs a folder."""
        for a in _ancestors(m.fpath)[:-1]:
            if (m.frid, nk(m.frid, a)) in landing:
                return m.frid, a
            for o in occupants(m.frid, a):
                if not o["is_dir"] and o["id"] not in moving:
                    return m.frid, o["path"]
        return None

    for m in files:
        row = rows[m.id]
        key = identity.content_key(row)
        slot = (m.frid, nk(m.frid, m.fpath))
        occ = [o for o in occupants(m.frid, m.fpath) if o["is_dir"] or o["id"] not in moving]
        early = taken.get(slot)
        blocking_dir = next((o for o in occ if o["is_dir"]), None)
        peers = [o for o in occ if not o["is_dir"]]
        clean = False
        if blocking_dir is not None:
            conflict(m, "DIR_IN_WAY", "a folder with that name is in the way", (m.frid, blocking_dir["path"]))
        elif peers or early:
            other_keys = {identity.content_key(o) for o in peers}
            if early:
                other_keys.add(identity.content_key(early.row))
            if other_keys == {key}:
                witness = (m.frid, peers[0]["path"]) if peers else (early.m.frid, early.m.fpath)
                dedupes.append((m, row, witness, None if peers else early))
                deduped.add(m.id)
                continue
            clash = (m.frid, peers[0]["path"]) if peers else (early.m.frid, early.m.fpath)
            conflict(m, "DIFFERENT_CONTENT", "a different file already has that name", clash)
        elif (hole := file_in_the_way(m)) is not None:
            conflict(m, "FILE_IN_WAY", "a file is where this needs a folder", hole)
        else:
            clean = True
        prim = _Prim(m, row, "move" if m.rid == m.frid else "copy")
        prims.append(prim)
        if clean:
            taken[slot] = prim

    # Order moves/copies so a file lands only after whatever sits on its spot has left. A copy (and a
    # file deleted by dedupe) leaves only in the delete phase, so landing on its spot cannot work.
    origin_of: dict[tuple[int, str], list[tuple[str, int]]] = {}
    for i, p in enumerate(prims):
        origin_of.setdefault((p.m.rid, nk(p.m.rid, p.m.path)), []).append((p.kind, i))
    for m, _, _, _ in dedupes:
        origin_of.setdefault((m.rid, nk(m.rid, m.path)), []).append(("dedupe", -1))
    succ: list[list[int]] = [[] for _ in prims]
    indeg = [0] * len(prims)
    blocked: set[int] = set()
    for i, p in enumerate(prims):
        spots = [p.m.fpath] + [a for a in _ancestors(p.m.fpath) if a]
        for sp in spots:
            for kind, j in origin_of.get((p.m.frid, nk(p.m.frid, sp)), ()):
                if j == i:
                    continue
                if kind == "move":
                    succ[j].append(i)
                    indeg[i] += 1
                else:
                    blocked.add(i)
    for i in sorted(blocked):
        m = prims[i].m
        conflict(m, "OCCUPIED", "the spot is still occupied until the delete phase", None)
    heap = [i for i, n in enumerate(indeg) if n == 0]
    heapq.heapify(heap)
    order: list[int] = []
    while heap:
        i = heapq.heappop(heap)
        order.append(i)
        for k in succ[i]:
            indeg[k] -= 1
            if indeg[k] == 0:
                heapq.heappush(heap, k)
    stuck = [i for i, n in enumerate(indeg) if n > 0]
    for i in stuck:
        conflict(prims[i].m, "CYCLE", "moves wait on each other (a swap needs a temporary name)", None)
    order += stuck
    ordered = [prims[i] for i in order]
    for pos, p in enumerate(ordered):
        p.pos = pos

    def reason(m: _Moved, extra: str = "") -> str:
        return (f"workspace draft #{draft_id} action #{m.aseq}: {rem[m.rid].name}:{m.path} -> "
                f"{rem[m.frid].name}:{m.fpath}{extra}")

    ops: list[Op] = []
    bad = {(c["from"]["remote"], c["from"]["path"]) for c in conflicts}
    for p in ordered:
        m = p.m
        ops.append(builders.op_from_entry(p.row, p.kind, dst_remote_id=m.frid, dst_path=m.fpath, reason=reason(m)))
    deletes: list[tuple[tuple[int, str, int], Op]] = []
    for p in ordered:
        if p.kind == "copy" and (rem[p.m.rid].name, p.m.path) not in bad:
            deletes.append(((p.m.rid, p.m.path, p.m.id), builders.op_from_entry(
                p.row, "delete", witness_remote_id=p.m.frid, witness_path=p.m.fpath, depends_on=p.pos + 1,
                reason=reason(p.m, " (verified copy first)"))))
    for m, row, witness, wop in dedupes:
        deletes.append(((m.rid, m.path, m.id), builders.op_from_entry(
            row, "delete", witness_remote_id=witness[0], witness_path=witness[1],
            depends_on=None if wop is None else wop.pos + 1,
            reason=reason(m, " (identical file already there)"))))
    deletes.sort(key=lambda t: t[0])
    ops.extend(op for _, op in deletes)

    info = {
        "draft_version": version,
        "changes": _changes(space, rem, moved, deduped),
        "conflicts": conflicts,
        "empty_dirs_left": _empty_dirs(conn, space, moved),
        "dedupes": len(dedupes),
        "summary": _summary(moved, rem),
    }
    return ops, info


def _changes(space: _Space, rem: dict[int, _Remote], moved: list[_Moved], deduped: set[int]) -> list[dict]:
    """One row per moved root: a folder (or lone file) and everything that travels with it."""
    tops: dict[Loc, dict] = {}
    for m in moved:
        t = tops.setdefault(m.root, {"files": 0, "bytes": 0, "is_dir": False, "deduped": 0})
        # Anything that is not its own root travels with a folder, so the root is a folder.
        t["is_dir"] = t["is_dir"] or m.is_dir or (m.rid, m.path) != m.root
        if not m.is_dir:
            t["files"] += 1
            t["bytes"] += m.size
            t["deduped"] += m.id in deduped
    out = []
    for (rid, path), t in sorted(tops.items()):
        fr, fp = space.fwd(rid, path)
        if t["is_dir"] and sum(o.kind != "file" for o in space.origins_at(fr, fp)) >= 2:
            kind = "merge"
        elif not t["is_dir"] and t["deduped"]:
            kind = "dedupe"
        elif fr != rid:
            kind = "copy+delete"
        else:
            kind = "move"
        out.append({"from": {"remote": rem[rid].name, "path": path}, "to": {"remote": rem[fr].name, "path": fp},
                    "files": t["files"], "bytes": t["bytes"], "kind": kind, "is_dir": t["is_dir"]})
    return out


def _empty_dirs(conn: sqlite3.Connection, space: _Space, moved: list[_Moved]) -> int:
    """Catalog folders that the file-by-file execution leaves with no files in them (they are not deleted)."""
    delta = _Delta(space, moved)
    count = 0
    for (rid, path), (n_out, _, _) in delta.out.items():
        if not path or not n_out:
            continue
        before = conn.execute("SELECT COUNT(*) FROM entries WHERE remote_id = ? AND present = 1 AND is_dir = 0 "
                              "AND path >= ? AND path < ?", (rid, path + "/", path + "0")).fetchone()[0]
        if before and before - n_out + delta.inn.get((rid, path), (0, 0, 0))[0] == 0:
            count += 1
    return count


def preview(conn: sqlite3.Connection, draft_id: int, *, policy: simulate.SafetyPolicy | None = None) -> dict:
    """Turn the draft into a 'workspace' plan, simulate it, and bind the result to the draft version."""
    title = _open_draft(conn, draft_id)["title"]
    ops, info = build_ops(conn, draft_id)
    if not ops:
        return {"plan_id": None, "message": "nothing to do"}
    version = info["draft_version"]
    old = _q(conn, "SELECT preview_plan_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()[0]
    plan_id = plans.create_plan(conn, f"Workspace: {title}", "workspace", ops, created_by="workspace",
                                params={"draft_id": draft_id, "draft_version": version})
    report = simulate.simulate_plan(conn, plan_id, policy=policy)
    conn.execute("UPDATE drafts SET preview_plan_id = ?, preview_version = ?, updated_at = ? WHERE id = ?",
                 (plan_id, version, db.utcnow(), draft_id))
    if old is not None and plans.get_plan(conn, old, with_ops=False).status in ("draft", "simulated", "approved"):
        # The previous preview describes an older draft: retire it, so previewing again never leaves a stale
        # plan that someone could still approve from the CLI. A plan that ran (or failed) is history: keep it.
        plans.cancel_plan(conn, old)
    counts = {k: sum(o.op == k for o in ops) for k in ("move", "copy", "delete")}
    return {
        "plan_id": plan_id,
        "draft_version": version,
        # The simulator cannot see every way a draft fails to come out as drawn (a swap of identical files
        # looks like harmless no-ops to it), so only a passing simulation *and* no conflicts is a clean draft.
        "ok": report.ok and not info["conflicts"],
        "simulation": asdict(report),
        "summary": {**info["summary"], "dedupes": info["dedupes"], "conflicts": len(info["conflicts"]),
                    "ops": counts, "roots": len(info["changes"])},
        "changes": info["changes"],
        "conflicts": info["conflicts"],
        "empty_dirs_left": info["empty_dirs_left"],
    }


def is_stale_preview(conn: sqlite3.Connection, plan_id: int) -> bool:
    """True for a workspace plan whose draft changed (or closed) since the preview; False for other plans."""
    plan = plans.get_plan(conn, plan_id, with_ops=False)
    if plan.kind != "workspace":
        return False
    row = _q(conn, "SELECT status, version FROM drafts WHERE id = ?", (plan.params.get("draft_id"),)).fetchone()
    return row is None or row["status"] != "open" or row["version"] != plan.params.get("draft_version")


__all__ = [
    "WorkspaceError", "create_draft", "list_drafts", "get_draft", "discard_draft", "add_action", "undo", "redo",
    "remove_action", "list_dir", "roots", "build_ops", "preview", "mark_applied", "is_stale_preview",
]
