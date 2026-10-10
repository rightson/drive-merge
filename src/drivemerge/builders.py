"""Plan builders: turn a goal (dedupe, consolidate, reorganize, a hand-written
op list) into an ordered list of Ops. Builders only read the catalog; whether a
plan is safe is decided by simulate.py, never here.

Builders still avoid proposing what the simulator or executor would refuse
(an occupied destination, an ambiguous Drive path, a read-only remote), because
a plan full of violations helps nobody; what they skip is reported in
BuildResult.skipped with a reason.
"""

from __future__ import annotations

import posixpath
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from . import classify, db, identity
from .models import Op

# Entries under this folder are quarantined deletes, not live copies (simulate.SafetyPolicy.quarantine_dir).
DEFAULT_QUARANTINE = ".drive-merge-quarantine"
_PAGE = 200            # duplicate groups fetched per query
_MAX_SUFFIX = 1000     # ' (2)' ... ' (1000)' before a destination is given up on

# Strongest first: the executor re-checks this hash live before touching the file. sha1, md5 and QuickXor
# come last because they are the weak ones (identity.WEAK_TYPES): fine to notice a change, no proof of identity.
_HASH_PREFERENCE = ("sha256", "dropbox", "sha1", "md5", "quickxor")


@dataclass
class BuildResult:
    ops: list[Op] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)   # {"entry_id"?, "location", "reason"}
    stats: dict = field(default_factory=dict)


class BuildError(ValueError):
    pass


def best_hash(row) -> str | None:
    """'<type>:<hex>' for the strongest hash a catalog row carries."""
    for t in _HASH_PREFERENCE:
        if row[t]:
            return f"{t}:{row[t]}"
    return None


def parse_location(conn: sqlite3.Connection, loc: str) -> tuple[int, str]:
    """'gdrive:Photos/a.jpg' -> (remote_id, 'Photos/a.jpg'). Remote names never contain ':'."""
    if ":" not in loc:
        raise BuildError(f"expected '<remote>:<path>', got {loc!r}")
    name, path = loc.split(":", 1)
    try:
        return db.remote_id(conn, name), path.strip("/")
    except KeyError as e:
        raise BuildError(str(e)) from None


def _live_files_at(conn: sqlite3.Connection, rid: int, path: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE remote_id = ? AND path = ? AND present = 1 AND is_dir = 0",
        (rid, path),
    ).fetchall()


def _live_files_under(conn: sqlite3.Connection, rid: int, prefix: str) -> list[sqlite3.Row]:
    if prefix == "":
        return conn.execute(
            "SELECT * FROM entries WHERE remote_id = ? AND present = 1 AND is_dir = 0 ORDER BY path", (rid,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM entries WHERE remote_id = ? AND present = 1 AND is_dir = 0 "
        "AND substr(path, 1, ?) = ? ORDER BY path",
        (rid, len(prefix) + 1, prefix + "/"),
    ).fetchall()


def op_from_entry(row, kind: str, **kw) -> Op:
    """An Op whose source is an existing catalog entry, with live preconditions filled in."""
    return Op(kind, row["remote_id"], row["path"], src_entry_id=row["id"], expect_size=row["size"],
              expect_hash=best_hash(row), expect_mtime=row["mtime"], **kw)


def build_manual(conn: sqlite3.Connection, specs: list[dict]) -> BuildResult:
    """Ops written by a person or an LLM, e.g.

        {"op": "move", "src": "gdrive:Old/Projects", "dst": "gdrive:Projects", "reason": "..."}
        {"op": "delete", "src": "dropbox:a.jpg", "witness": "gdrive:Photos/a.jpg"}

    A directory source expands to one op per live file under it, keeping
    relative paths (for delete, the witness must then be a directory too). A
    source that is not in the catalog still yields an op so the simulation
    reports it (SRC_MISSING / AMBIGUOUS_PATH) instead of it silently vanishing.
    """
    result = BuildResult()
    for i, spec in enumerate(specs, start=1):
        kind = spec.get("op")
        if kind not in ("copy", "move", "delete"):
            raise BuildError(f"op #{i}: 'op' must be copy, move or delete")
        if "src" not in spec:
            raise BuildError(f"op #{i}: missing 'src'")
        src_rid, src_path = parse_location(conn, spec["src"])
        dst = parse_location(conn, spec["dst"]) if spec.get("dst") else None
        wit = parse_location(conn, spec["witness"]) if spec.get("witness") else None
        if kind in ("copy", "move") and dst is None:
            raise BuildError(f"op #{i}: {kind} needs 'dst'")
        reason = spec.get("reason")

        def make(row, rel: str | None):
            extra: dict = {"reason": reason}
            if dst:
                extra.update(dst_remote_id=dst[0], dst_path=posixpath.join(dst[1], rel) if rel else dst[1])
            if wit:
                extra.update(witness_remote_id=wit[0],
                             witness_path=posixpath.join(wit[1], rel) if rel else wit[1])
            if row is None:
                return Op(kind, src_rid, src_path, **extra)
            return op_from_entry(row, kind, **extra)

        files = _live_files_at(conn, src_rid, src_path)
        if len(files) == 1:
            result.ops.append(make(files[0], None))
            continue
        if len(files) > 1:  # Drive duplicate paths: let the simulator flag it
            result.ops.append(make(None, None))
            continue
        under = _live_files_under(conn, src_rid, src_path)
        if not under:
            result.ops.append(make(None, None))
            continue
        cut = len(src_path) + 1 if src_path else 0
        for row in under:
            result.ops.append(make(row, row["path"][cut:]))
    result.stats = {"specs": len(specs), "ops": len(result.ops)}
    return result


# --------------------------------------------------------------------------- keeper policy

# Names a sync client or a person gives to a second copy. Matched against every path segment
# (the last one without its extension), NFC-normalised and case-insensitive.
_COPY_NAME = re.compile(
    r"^copy(?: \(\d+\))? of |\s\(\d{1,3}\)$| - copy(?: \(\d+\))?$|_copy\d*$|conflicted copy|副本|的副本|のコピー|复制",
    re.IGNORECASE,
)
_MISSING = object()

# Which provider's copy to keep when nothing above it decides, best first. A copy on a provider with native
# hashes and server-side moves is cheap to verify and to keep tidy; iCloud has neither (its content is only
# "proved" through a bridge) and is the last resort. Unlisted providers rank like "other".
KEEPER_PROVIDER_ORDER: tuple[str, ...] = ("drive", "onedrive", "dropbox", "local", "other", "iclouddrive")
_KEEPER_RANK = {**{p: i for i, p in enumerate(KEEPER_PROVIDER_ORDER)}, "memory": KEEPER_PROVIDER_ORDER.index("local")}


def looks_like_copy(path: str) -> bool:
    """True when a path reads like a duplicate: 'Copy of x', 'x (1)', 'x - Copy', 'x_copy', '副本', ..."""
    parts = path.split("/")
    for i, part in enumerate(parts):
        stem = posixpath.splitext(part)[0] if i == len(parts) - 1 else part
        if _COPY_NAME.search(unicodedata.normalize("NFC", stem)):
            return True
    return False


def _field(copy: Any, *names: str) -> Any:
    """First present attribute/key of a copy: identity.Copy, a catalog row, a dict or any lookalike."""
    for name in names:
        if isinstance(copy, Mapping):
            if name in copy:
                return copy[name]
        elif isinstance(copy, sqlite3.Row):
            if name in copy.keys():
                return copy[name]
        else:
            value = getattr(copy, name, _MISSING)
            if value is not _MISSING:
                return value
    return None


def _mtime_key(mtime: str | None) -> tuple[int, str, str]:
    """Orders ISO timestamps with or without fractional seconds; unknown times last."""
    if not mtime:
        return (1, "", "")
    base, _, frac = str(mtime).rstrip("Z").partition(".")
    return (0, base, frac.ljust(9, "0"))


def _provider_rank(copy: Any, providers: Mapping | None) -> int:
    """Position of the copy's provider in KEEPER_PROVIDER_ORDER; a copy of unknown provider ranks as "other"."""
    key = _field(copy, "provider")
    if key is None and providers:
        key = providers.get(_field(copy, "remote"), providers.get(_field(copy, "remote_id")))
    return _KEEPER_RANK.get(key, _KEEPER_RANK["other"])


def _keeper_key(copy: Any, prefer: Sequence, target: Any, providers: Mapping | None = None) -> tuple:
    rid, name = _field(copy, "remote_id"), _field(copy, "remote")
    on_target = target is not None and (name == target or rid == target)
    rank = next((i for i, p in enumerate(prefer) if p == name or p == rid), len(prefer))
    path = _field(copy, "path") or ""
    entry_id = _field(copy, "entry_id", "id")
    return (not on_target, rank, looks_like_copy(path), _provider_rank(copy, providers), path.count("/"),
            _mtime_key(_field(copy, "mtime")), entry_id if entry_id is not None else 1 << 62)


def choose_keeper(copies: Iterable, *, prefer_remotes: Sequence = (), target: Any = None,
                  providers: Mapping | None = None):
    """The copy to keep out of several identical ones. Deterministic; the first difference decides:

    on the target remote > earlier in prefer_remotes > a path that does not look like a copy
    > provider (drive > onedrive > dropbox > local > other > iclouddrive) > shallower path
    > older mtime > smaller entry id.

    `copies` are identity.Copy objects, catalog rows or dicts (remote name or id, path, mtime,
    entry id); `target` and `prefer_remotes` take remote names or ids. A copy's provider is its
    own `provider` field when it has one, else looked up in `providers` ({remote name or id:
    provider key}); without either it ranks as "other", so the criterion changes nothing.
    """
    pool = list(copies)
    if not pool:
        raise ValueError("choose_keeper needs at least one copy")
    prefer = tuple(prefer_remotes)
    return min(pool, key=lambda c: _keeper_key(c, prefer, target, providers))


# --------------------------------------------------------------------------- shared helpers

@dataclass(frozen=True)
class _Remote:
    id: int
    name: str
    provider: str
    writable: bool
    ci: bool


class _File(NamedTuple):
    """A live file as the builders need it: slim, so a million-file catalog stays affordable."""

    entry_id: int
    remote_id: int
    remote: str
    path: str
    name_norm: str
    size: int | None
    mtime: str | None
    group: int | None
    native: bool
    hashed: bool
    key: str            # identity.content_key

    @property
    def loc(self) -> str:
        return f"{self.remote}:{self.path}"


# "Hashed" = carries a hash that proves identity. A file with only md5/sha1/QuickXor (OneDrive) is no better off than
# a hashless one: the executor must read it unless a bridge made from its own bytes vouches for it.
_HASHED = " OR ".join(f"COALESCE(e.{t}, '') <> ''" for t in identity.STRONG_TYPES)


def _fold(s: str) -> str:
    """NFC + casefold: how case-insensitive remotes compare names."""
    return unicodedata.normalize("NFC", s).casefold()


def _remote_map(conn: sqlite3.Connection) -> dict[int, _Remote]:
    return {r[0]: _Remote(r[0], r[1], r[2], bool(r[3]), bool(r[4])) for r in conn.execute(
        "SELECT id, name, provider, write_remote, case_insensitive FROM remotes ORDER BY id")}


def _provider_map(remotes: Mapping[int, _Remote]) -> dict[str | int, str]:
    """{remote name or id: provider key}, for choose_keeper."""
    return {**{r.name: r.provider for r in remotes.values()}, **{r.id: r.provider for r in remotes.values()}}


def _named(remotes: Mapping[int, _Remote], name: str) -> _Remote:
    for r in remotes.values():
        if r.name == name:
            return r
    raise BuildError(f"unknown remote {name!r}")


def _names(remotes: Mapping[int, _Remote], names: Iterable[str] | str | None) -> list[_Remote]:
    """Remotes by name, in order, without repeats; plan params come from JSON, so None and one bare name work."""
    out: dict[int, _Remote] = {}
    for n in [names] if isinstance(names, str) else (names or ()):
        r = _named(remotes, n)
        out.setdefault(r.id, r)
    return list(out.values())


def _clean_prefix(prefix: str | None) -> str:
    return (prefix or "").strip("/")


def _in_prefix(path: str, prefix: str) -> bool:
    return not prefix or path == prefix or path.startswith(prefix + "/")


def _quarantine_test(quarantine_dir: str | None) -> Callable[[str], bool]:
    q = _fold((quarantine_dir or "").strip("/"))
    if not q:
        return lambda path: False
    return lambda path: (f := _fold(path)) == q or f.startswith(q + "/")


def _ancestors(path: str):
    """Proper ancestor folders of a path, shortest first: 'a/b/c' -> 'a', 'a/b'."""
    i = path.find("/")
    while i != -1:
        yield path[:i]
        i = path.find("/", i + 1)


def _suffixed(path: str, n: int) -> str:
    """'dir/a.txt' -> 'dir/a (2).txt'; n <= 1 leaves the path alone."""
    if n <= 1:
        return path
    folder, base = posixpath.split(path)
    stem, ext = posixpath.splitext(base)
    return posixpath.join(folder, f"{stem} ({n}){ext}")


def _load_files(conn: sqlite3.Connection, remote: _Remote, *, prefix: str,
                in_quarantine: Callable[[str], bool]) -> list[_File]:
    """Live files of a remote (native docs included), under `prefix`, ordered by (path, id)."""
    scope, args = ("", ()) if not prefix else (
        " AND (e.path = ? OR (e.path >= ? AND e.path < ?))", (prefix, prefix + "/", prefix + "0"))
    cur = conn.execute(
        "SELECT e.id, e.path, e.name_norm, e.size, e.mtime, e.content_group, e.is_native_doc, "
        f"({_HASHED}) FROM entries e WHERE e.remote_id = ? AND e.present = 1 AND e.is_dir = 0{scope} "
        "ORDER BY e.path, e.id", (remote.id, *args))
    return [
        _File(eid, remote.id, remote.name, path, name_norm, size, mtime, group, bool(native), bool(hashed),
              identity.content_key({"content_group": group, "id": eid}))
        for eid, path, name_norm, size, mtime, group, native, hashed in cur
        if not in_quarantine(path)
    ]


def _ambiguous_paths(conn: sqlite3.Connection, rid: int) -> set[str]:
    """Paths with several live entries (Drive allows them): rclone cannot address any one of them."""
    return {r[0] for r in conn.execute(
        "SELECT path FROM entries WHERE remote_id = ? AND present = 1 GROUP BY path HAVING COUNT(*) > 1", (rid,))}


def _rows_by_id(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
    ids = list(dict.fromkeys(ids))
    out: dict[int, sqlite3.Row] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for row in conn.execute(f"SELECT * FROM entries WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            out[row["id"]] = row
    return out


def _skip(result: BuildResult, f: _File, reason: str) -> None:
    result.skipped.append({"entry_id": f.entry_id, "location": f.loc, "reason": reason})


class _Namespace:
    """What one remote will hold once the ops built so far have run.

    Destinations are chosen against it so that no two ops land on one path and
    nothing different is overwritten. Case-insensitive remotes compare folded
    names, and a path counts as taken when it is a file, a folder, or when one
    of its parents is a file (a name cannot be both).
    """

    def __init__(self, case_insensitive: bool):
        self._ci = case_insensitive
        self._files: dict[str, list[str]] = {}    # folded path -> content keys of the files there
        self._dirs: set[str] = set()
        self._made: dict[str, Op] = {}            # folded path -> op that creates the file there

    def fold(self, path: str) -> str:
        return _fold(path) if self._ci else path

    def same_path(self, a: str, b: str) -> bool:
        return self.fold(a) == self.fold(b)

    def add_dir(self, path: str) -> None:
        fp = self.fold(path)
        self._dirs.add(fp)
        self._dirs.update(_ancestors(fp))

    def place(self, path: str, key: str, creator: Op | None = None) -> None:
        fp = self.fold(path)
        self._files.setdefault(fp, []).append(key)
        self._dirs.update(_ancestors(fp))
        if creator is not None:
            self._made[fp] = creator

    def vacate(self, path: str, key: str) -> None:
        fp = self.fold(path)
        keys = self._files.get(fp)
        if keys and key in keys:
            keys.remove(key)
            if not keys:
                del self._files[fp]
                self._made.pop(fp, None)

    def creator_of(self, path: str) -> Op | None:
        return self._made.get(self.fold(path))

    def state(self, path: str, key: str) -> str:
        """'free', 'same' (a file with this content is there), 'taken' or 'blocked' (a parent is a file)."""
        fp = self.fold(path)
        if any(a in self._files for a in _ancestors(fp)):
            return "blocked"
        if fp in self._dirs:
            return "taken"
        keys = self._files.get(fp)
        if not keys:
            return "free"
        return "same" if len(keys) == 1 and keys[0] == key else "taken"

    def resolve(self, path: str, key: str, *, own: str | None = None, same_ok: bool = False) -> tuple[str, str]:
        """First usable name among path, 'path (2)', 'path (3)', ... as (status, final path).

        status: 'free' (create it), 'same' (identical content already there; only with same_ok),
        'inplace' (the file being moved already sits at a candidate: `own` is its path),
        'blocked' (a parent folder is a file) or 'full' (every numbered name is taken).
        """
        own_fold = self.fold(own) if own is not None else None
        for n in range(1, _MAX_SUFFIX + 1):
            cand = _suffixed(path, n)
            if own_fold is not None and self.fold(cand) == own_fold:
                return "inplace", own  # type: ignore[return-value]
            st = self.state(cand, key)
            if st == "blocked":
                return "blocked", cand
            if st == "free" or (st == "same" and same_ok):
                return st, cand
        return "full", path


def _unplaceable(remote: str, dest: str, status: str) -> str:
    what = "a parent folder is a file there" if status == "blocked" else f"all {_MAX_SUFFIX} numbered names are taken"
    return f"cannot be placed at {remote}:{dest}: {what}"


def _namespace(conn: sqlite3.Connection, remote: _Remote, files: Iterable[_File]) -> _Namespace:
    ns = _Namespace(remote.ci)
    for (path,) in conn.execute("SELECT path FROM entries WHERE remote_id = ? AND present = 1 AND is_dir = 1",
                                (remote.id,)):
        ns.add_dir(path)
    for f in files:
        ns.place(f.path, f.key)
    return ns


# --------------------------------------------------------------------------- dedupe

def build_dedupe(conn: sqlite3.Connection, *, remote: str | None = None, cross_remote: bool = False,
                 path_prefix: str | None = None, min_size: int = 1, prefer_remotes: Sequence[str] = (),
                 limit_groups: int | None = None, quarantine_dir: str = DEFAULT_QUARANTINE) -> BuildResult:
    """Delete redundant exact copies, always keeping one per content (and per remote, unless cross_remote).

    Without cross_remote each remote is thinned on its own: the keeper (choose_keeper) stays and the
    other copies on that remote are deleted with the keeper as witness. With cross_remote one keeper
    stands for the whole group and every other copy, on any remote, goes. `remote` only selects
    groups that have a copy there (and, without cross_remote, limits the work to that remote);
    `path_prefix` limits what may be deleted, not which copy is kept. Copies on read-only remotes,
    at ambiguous Drive paths, native docs and quarantined files are never touched; the first two
    are reported in `skipped`. Only exact groups are used: a probable duplicate is never a reason to delete.
    `limit_groups` stops after that many groups produced a delete.
    """
    remotes = _remote_map(conn)
    only = _named(remotes, remote) if remote is not None else None
    prefer = [r.name for r in _names(remotes, prefer_remotes)]
    prefix = _clean_prefix(path_prefix)
    min_size = 1 if min_size is None else min_size
    in_quarantine = _quarantine_test(quarantine_dir)
    providers = _provider_map(remotes)
    result = BuildResult()
    ambiguous: dict[int, set[str]] = {}
    examined = made = reclaimed = 0
    offset = 0
    while limit_groups is None or made < limit_groups:
        groups = identity.duplicate_groups(
            conn, confidence="exact", remote=only.name if only else None, path_prefix=prefix or None,
            min_size=min_size, limit=_PAGE, offset=offset)
        if not groups:
            break
        offset += len(groups)
        rows = _rows_by_id(conn, (c.entry_id for g in groups for c in g.copies))
        for g in groups:
            if limit_groups is not None and made >= limit_groups:
                break
            examined += 1
            copies = [c for c in g.copies if not in_quarantine(c.path) and not rows[c.entry_id]["is_native_doc"]]
            if cross_remote:
                pools = [copies]
            else:
                by_remote: dict[int, list] = defaultdict(list)
                for c in copies:
                    if only is None or c.remote_id == only.id:
                        by_remote[c.remote_id].append(c)
                pools = [by_remote[rid] for rid in sorted(by_remote)]
            before = len(result.ops)
            for pool in pools:
                for victim, keeper in _dedupe_pool(conn, pool, prefer, providers, prefix, remotes, ambiguous, result):
                    row = rows[victim.entry_id]
                    result.ops.append(op_from_entry(
                        row, "delete", witness_remote_id=keeper.remote_id, witness_path=keeper.path,
                        reason=f"duplicate of {keeper.remote}:{keeper.path}"))
                    reclaimed += victim.size
            made += len(result.ops) > before
    result.stats = {"groups_examined": examined, "groups": made, "deletes": len(result.ops),
                    "bytes_reclaimed": reclaimed, "skipped": len(result.skipped)}
    return result


def _dedupe_pool(conn: sqlite3.Connection, pool: list[identity.Copy], prefer: list[str],
                 providers: Mapping[str | int, str], prefix: str, remotes: Mapping[int, _Remote],
                 ambiguous: dict[int, set[str]], result: BuildResult) -> list[tuple[identity.Copy, identity.Copy]]:
    """(victim, keeper) pairs for one set of identical copies; records what it will not touch."""
    if len(pool) < 2:
        return []
    amb: list[identity.Copy] = []
    usable: list[identity.Copy] = []
    for c in pool:
        if c.remote_id not in ambiguous:
            ambiguous[c.remote_id] = _ambiguous_paths(conn, c.remote_id)
        (amb if c.path in ambiguous[c.remote_id] else usable).append(c)
    for c in amb:
        if _in_prefix(c.path, prefix):
            result.skipped.append({"entry_id": c.entry_id, "location": f"{c.remote}:{c.path}",
                                   "reason": "ambiguous path"})
    if len(usable) < 2:
        return []
    keeper = choose_keeper(usable, prefer_remotes=prefer, providers=providers)
    pairs = []
    for c in usable:
        if c is keeper or not _in_prefix(c.path, prefix):
            continue
        if not remotes[c.remote_id].writable:
            result.skipped.append({"entry_id": c.entry_id, "location": f"{c.remote}:{c.path}",
                                   "reason": "read-only remote"})
            continue
        pairs.append((c, keeper))
    return pairs


# --------------------------------------------------------------------------- consolidate

_NEEDS_BRIDGING = "needs bridging: run drivemerge bridge"
# Which source to read the bytes from: hashed copies first (the executor can verify them), then by provider
# (cheapest to read; not the keeper order, which is about which copy to keep).
_PROVIDER_RANK = {"drive": 0, "dropbox": 1, "onedrive": 2, "local": 3, "memory": 4, "other": 5, "iclouddrive": 6}


class _Content:
    """All live copies of one content, as consolidate sees them."""

    __slots__ = ("key", "t", "s", "primary", "slot", "probable")

    def __init__(self, key: str):
        self.key = key
        self.t: list[_File] = []             # usable copies on the target
        self.s: list[_File] = []             # usable, in-scope copies on the source remotes
        self.primary: str | None = None      # target path that stands for the content once the plan has run
        self.slot: dict[int, str | None] = {}  # unit members: source entry id -> own target path (None = unplaced)
        self.probable = False                # accepted as already on the target by name and size (include_probable)


class _Consolidation:
    """State of one build_consolidate run.

    Loose files are placed once per content: the keeper's layout position. A file
    inside a unit (a repo, a bundle) is placed at its own layout position and
    is never merged with its twins, otherwise a repo's repeated files (empty
    __init__.py, LICENSE copies) would collapse into one and the unit would
    arrive incomplete. Its content must exist at that position on the target;
    a copy elsewhere does not count.
    """

    def __init__(self, conn: sqlite3.Connection, remotes: Mapping[int, _Remote], target: _Remote,
                 sources: list[_Remote], *, target_root: str, ruleset: classify.RuleSet,
                 delete_sources: bool, reorganize_target: bool, prefix: str, include_probable: bool,
                 prefer: list[str], in_quarantine: Callable[[str], bool]):
        self.conn, self.remotes, self.tgt = conn, remotes, target
        self.delete_sources, self.reorganize_target = delete_sources, reorganize_target
        self.include_probable, self.prefer, self.prefix = include_probable, prefer, prefix
        self.providers = _provider_map(remotes)
        self.result = BuildResult()
        self.moves: list[Op] = []
        self.copies: list[Op] = []
        self.deletes: list[Op] = []
        self.deps: list[tuple[Op, Op]] = []        # (delete, the move/copy that creates its witness)
        self.final: dict[int, str] = {}            # target entry id -> path after this plan's moves
        self._probable: dict[tuple, list[_File]] | None = None
        self._claimed: set[int] = set()            # target files already accepted as the probable twin of a source
        self._vouching: set[str] = set()           # folded target paths that vouch for a source and must stay
        self.idle = 0                              # contents only on the target that nothing was asked to do about

        self.tfiles = _load_files(conn, target, prefix="", in_quarantine=in_quarantine)
        self.ns = _namespace(conn, target, self.tfiles)
        self.t_amb = _ambiguous_paths(conn, target.id)
        self.sfiles: list[_File] = []
        self.s_amb: dict[int, set[str]] = {}
        for r in sources:
            self.sfiles.extend(_load_files(conn, r, prefix=prefix, in_quarantine=in_quarantine))
            self.s_amb[r.id] = _ambiguous_paths(conn, r.id)
        # The target takes part in the layout only when its own files may move or be judged against units.
        # target_root is a folder of the target: its files below it are laid out as they stand inside it,
        # while a source's folder of the same name is only a folder.
        ids = [r.id for r in sources] + ([target.id] if reorganize_target or delete_sources else [])
        self.pl = {p.entry_id: p for p in classify.plan_layout(
            conn, ruleset, remote_ids=ids, target_root=target_root, path_prefix=prefix or None,
            root_remote_ids=[target.id])}

    # ------------------------------------------------------------ small helpers

    def _skip(self, f: _File, reason: str) -> None:
        _skip(self.result, f, reason)

    def _row(self, f: _File) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM entries WHERE id = ?", (f.entry_id,)).fetchone()

    def _pinned(self, f: _File) -> bool:
        p = self.pl.get(f.entry_id)
        return p is not None and p.unit is not None

    def _movable(self, f: _File) -> bool:
        return f.entry_id in self.pl and _in_prefix(f.path, self.prefix)

    def _best_source(self, files: list[_File]) -> _File:
        return min(files, key=lambda f: (
            not f.hashed, _PROVIDER_RANK.get(self.remotes[f.remote_id].provider, 5),
            *_keeper_key(f, self.prefer, self.tgt.name, self.providers)))

    def _probable_hits(self, f: _File) -> list[_File]:
        """Target files with the same size and normalised name as `f`: maybe the same content."""
        if self._probable is None:
            idx: dict[tuple, list[_File]] = defaultdict(list)
            for t in self.tfiles:
                if not t.native and t.size is not None and t.path not in self.t_amb:
                    idx[(t.size, t.name_norm)].append(t)
            self._probable = idx
        return self._probable.get((f.size, f.name_norm), [])

    def _unplaced(self, f: _File, dest: str, status: str) -> None:
        self._skip(f, _unplaceable(self.tgt.name, dest, status))

    # ------------------------------------------------------------ op makers

    def _move(self, f: _File, dst: str, rule: str) -> Op:
        op = op_from_entry(self._row(f), "move", dst_remote_id=f.remote_id, dst_path=dst,
                           reason=f"consolidate: reorganize target ({rule})")
        self.moves.append(op)
        self.ns.vacate(f.path, f.key)
        self.ns.place(dst, f.key, creator=op)
        self.final[f.entry_id] = dst
        return op

    def _copy(self, src: _File, dst: str, rule: str) -> Op:
        op = op_from_entry(self._row(src), "copy", dst_remote_id=self.tgt.id, dst_path=dst,
                           reason=f"consolidate: {rule}")
        self.copies.append(op)
        self.ns.place(dst, src.key, creator=op)
        return op

    def _delete(self, f: _File, witness: str, why: str) -> None:
        op = op_from_entry(self._row(f), "delete", witness_remote_id=self.tgt.id, witness_path=witness,
                           reason=f"consolidate: {why} {self.tgt.name}:{witness}")
        self.deletes.append(op)
        creator = self.ns.creator_of(witness)
        if creator is not None:
            self.deps.append((op, creator))

    # ------------------------------------------------------------ contents

    def contents(self) -> list[_Content]:
        by_key: dict[str, _Content] = {}
        for f in self.sfiles:
            if f.native:
                self._skip(f, "native doc: no bytes to copy, and deleting it is unverifiable")
            elif f.path in self.s_amb[f.remote_id]:
                self._skip(f, "ambiguous path")
            elif f.size is None:
                self._skip(f, "size unknown")
            else:
                by_key.setdefault(f.key, _Content(f.key)).s.append(f)
        alone = self.reorganize_target or self.delete_sources   # target-only contents have work to do
        idle: set[str] = set()                                  # ... otherwise they are only counted
        for f in self.tfiles:
            if f.path in self.t_amb:
                continue
            c = by_key.get(f.key)
            if c is None and _in_prefix(f.path, self.prefix):
                if not alone:
                    idle.add(f.key)
                    continue
                c = by_key[f.key] = _Content(f.key)
            if c is not None:
                c.t.append(f)
        self.idle = len(idle)
        return sorted(by_key.values(), key=lambda c: min((f.remote, f.path, f.entry_id) for f in (*c.s, *c.t)))

    # ------------------------------------------------------------ pass 1: the target's own files

    def settle_target(self, c: _Content) -> None:
        """Decide which target copy stands for the content and, if asked, move target files into the layout."""
        if not c.t:
            return
        pool = [f for f in c.t if not self._pinned(f)] or c.t
        keeper = choose_keeper(pool, prefer_remotes=self.prefer, target=self.tgt.name, providers=self.providers)
        primary = keeper.path
        if self.reorganize_target:
            if self._movable(keeper) and not self._pinned(keeper):
                primary = self._relocate_keeper(keeper, pool)
            for f in c.t:
                if self._pinned(f) and self._movable(f):
                    self._relocate_unit_file(f)
        c.primary = self.final.get(keeper.entry_id, primary)

    def _relocate_keeper(self, keeper: _File, pool: list[_File]) -> str:
        place = self.pl[keeper.entry_id]
        for f in pool:                       # a copy already in the layout makes moving unnecessary
            if self.ns.same_path(f.path, place.target_path):
                return f.path
        status, final = self.ns.resolve(place.target_path, keeper.key, own=keeper.path, same_ok=True)
        if status == "free":
            self._move(keeper, final, place.rule)
        elif status in ("blocked", "full"):
            self._unplaced(keeper, place.target_path, status)
            return keeper.path
        return final

    def _relocate_unit_file(self, f: _File) -> None:
        place = self.pl[f.entry_id]
        if self.ns.same_path(f.path, place.target_path):
            return
        status, final = self.ns.resolve(place.target_path, f.key, own=f.path)
        if status == "free":
            self._move(f, final, place.rule)
        elif status in ("blocked", "full"):
            self._unplaced(f, place.target_path, status)

    # ------------------------------------------------------------ pass 2: bring the sources over

    def place_sources(self, c: _Content) -> None:
        first = c.s[0]
        if c.key.startswith("e:") and not c.t:
            hits = self._probable_hits(first)
            if hits:
                # One target file can stand in for only one unhashed file (two files cannot both equal it),
                # and the simulator accepts a probable witness only under the same normalised name, so a
                # twin that this plan renames can no longer vouch for the file.
                free = [t for t in hits if t.entry_id not in self._claimed]
                match = choose_keeper(free, prefer_remotes=self.prefer, target=self.tgt.name,
                                      providers=self.providers) if free else None
                where = self.final.get(match.entry_id, match.path) if match else None
                if match is None or not self.include_probable or _fold(posixpath.basename(where)) != first.name_norm:
                    self._skip(first, _NEEDS_BRIDGING)
                    c.s = []
                    return
                self._claimed.add(match.entry_id)
                self._vouching.add(self.ns.fold(where))
                c.primary = where
                c.slot = {f.entry_id: where for f in c.s}
                c.probable = True
                return
        units: dict[str, list[_File]] = defaultdict(list)
        for f in c.s:
            if self._pinned(f):
                units[self.ns.fold(self.pl[f.entry_id].target_path)].append(f)
        for group in units.values():
            place = self.pl[group[0].entry_id]
            status, final = self.ns.resolve(place.target_path, c.key, same_ok=True)
            if status in ("blocked", "full"):
                for f in group:
                    c.slot[f.entry_id] = None
                    self._unplaced(f, place.target_path, status)
                continue
            if status == "free":
                self._copy(self._best_source(c.s), final, place.rule)
            for f in group:
                c.slot[f.entry_id] = final
            if c.primary is None:
                c.primary = final
        loose = [f for f in c.s if not self._pinned(f)]
        if loose and c.primary is None:
            keeper = choose_keeper(loose, prefer_remotes=self.prefer, target=self.tgt.name, providers=self.providers)
            place = self.pl[keeper.entry_id]
            status, final = self.ns.resolve(place.target_path, c.key, same_ok=True)
            if status in ("blocked", "full"):
                for f in loose:
                    self._unplaced(f, place.target_path, status)
                return
            if status == "free":
                self._copy(self._best_source(c.s), final, place.rule)
            c.primary = final

    # ------------------------------------------------------------ pass 3: delete what is now redundant

    def delete_redundant(self, c: _Content) -> None:
        used = {self.ns.fold(p) for p in (*c.slot.values(), c.primary) if p}
        for f in c.s:
            loc = c.slot.get(f.entry_id) if self._pinned(f) else c.primary
            if loc is None:
                continue
            if not self.remotes[f.remote_id].writable:
                self._skip(f, "read-only remote")
                continue
            self._delete(f, loc, "copy kept at")
        if c.primary is None:
            return
        for f in c.t:                        # extra loose copies on the target; unit files are structure, not clutter
            if self._pinned(f) or not _in_prefix(f.path, self.prefix):
                continue
            here = self.ns.fold(self.final.get(f.entry_id, f.path))
            if here in used or here in self._vouching:
                continue
            self._delete(f, c.primary, "duplicate of")

    # ------------------------------------------------------------ run

    def run(self) -> BuildResult:
        contents = self.contents()
        for c in contents:
            self.settle_target(c)
        for c in contents:
            if c.s:
                self.place_sources(c)
        if self.delete_sources:
            for c in contents:
                self.delete_redundant(c)
        ops = self.moves + self.copies + self.deletes
        seq = {id(op): i for i, op in enumerate(ops, start=1)}
        for op, creator in self.deps:
            op.depends_on = seq[id(creator)]
        self.result.ops = ops
        self.result.stats = {
            "contents": len(contents) + self.idle,
            "already_on_target": self.idle + sum(1 for c in contents if c.t or c.probable),
            "to_copy": len(self.copies),
            "bytes_to_copy": sum(op.expect_size or 0 for op in self.copies), "moves": len(self.moves),
            "deletes": len(self.deletes), "skipped": len(self.result.skipped),
        }
        return self.result


def build_consolidate(conn: sqlite3.Connection, *, target: str, target_root: str = "",
                      ruleset: classify.RuleSet | None = None, sources: Sequence[str] | None = None,
                      delete_sources: bool = False, reorganize_target: bool = False,
                      path_prefix: str | None = None, include_probable: bool = False,
                      prefer_remotes: Sequence[str] = (), quarantine_dir: str = DEFAULT_QUARANTINE) -> BuildResult:
    """Gather the content of the source remotes onto `target` in the rule-based layout (DESIGN.md §8).

    Every distinct content ends on the target: a content already there is left alone (with
    reorganize_target its keeper moves to the layout position); otherwise the best source copy is
    copied to the keeper's layout position under `target_root`. Destinations are unique in the plan
    and never occupied by different content: a taken name becomes 'name (2).ext'. Content with
    no hash that merely looks like a target file (same size and name) is skipped until bridging
    proves it, unless include_probable. Native docs are never copied; on the target they may only move.
    With delete_sources every other copy on a writable remote is deleted, the target copy as witness
    (after the copy that creates it), and so are extra loose copies on the target. Order: moves,
    copies, deletes. `path_prefix` limits which files are acted on; what is already on the
    target is always taken into account. Copies on read-only remotes are never deleted.
    """
    remotes = _remote_map(conn)
    tgt = _named(remotes, target)
    if not tgt.writable:
        raise BuildError(f"target {target!r} is read-only (no write_remote configured)")
    srcs = [r for r in (_names(remotes, sources) if sources else remotes.values()) if r.id != tgt.id]
    prefer = [r.name for r in _names(remotes, prefer_remotes)]
    run = _Consolidation(
        conn, remotes, tgt, srcs, target_root=target_root or "", ruleset=ruleset or classify.default_rules(),
        delete_sources=delete_sources, reorganize_target=reorganize_target, prefix=_clean_prefix(path_prefix),
        include_probable=include_probable, prefer=prefer, in_quarantine=_quarantine_test(quarantine_dir))
    return run.run()


# --------------------------------------------------------------------------- reorganize

def build_reorganize(conn: sqlite3.Connection, *, remote: str, ruleset: classify.RuleSet | None = None,
                     target_root: str = "", path_prefix: str | None = None,
                     quarantine_dir: str = DEFAULT_QUARANTINE) -> BuildResult:
    """Move the live files of one remote to their rule-based layout position (same-remote moves only).

    Files already in place stay. A destination that is taken (by anything, including a file that
    will move away later in the plan) becomes 'name (2).ext', so no move ever needs an overwrite,
    which the executor refuses. Native docs move like any file; files at ambiguous Drive paths are
    skipped. `path_prefix` limits which files move.
    """
    remotes = _remote_map(conn)
    rem = _named(remotes, remote)
    if not rem.writable:
        raise BuildError(f"remote {remote!r} is read-only (no write_remote configured)")
    prefix = _clean_prefix(path_prefix)
    files = {f.entry_id: f for f in _load_files(conn, rem, prefix="", in_quarantine=_quarantine_test(quarantine_dir))}
    ns = _namespace(conn, rem, files.values())
    ambiguous = _ambiguous_paths(conn, rem.id)
    placements = classify.plan_layout(conn, ruleset or classify.default_rules(), remote_ids=[rem.id],
                                      target_root=target_root or "", path_prefix=prefix or None)
    result = BuildResult()
    examined = in_place = renamed = 0
    for p in placements:
        f = files.get(p.entry_id)
        if f is None:                       # quarantined
            continue
        examined += 1
        if f.path in ambiguous:
            _skip(result, f, "ambiguous path")
            continue
        if ns.same_path(f.path, p.target_path):
            in_place += 1
            continue
        status, final = ns.resolve(p.target_path, f.key, own=f.path)
        if status == "inplace":
            in_place += 1
        elif status in ("blocked", "full"):
            _skip(result, f, _unplaceable(rem.name, p.target_path, status))
        else:
            op = op_from_entry(conn.execute("SELECT * FROM entries WHERE id = ?", (f.entry_id,)).fetchone(), "move",
                               dst_remote_id=rem.id, dst_path=final, reason=f"reorganize ({p.rule})")
            result.ops.append(op)
            ns.vacate(f.path, f.key)
            ns.place(final, f.key, creator=op)
            renamed += final != p.target_path
    result.stats = {"files": examined, "in_place": in_place, "moves": len(result.ops),
                    "renamed_for_collision": renamed, "skipped": len(result.skipped)}
    return result
