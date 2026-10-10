"""Plan simulation: apply ops to a virtual overlay of the catalog and check invariants.

This is the only gate between a plan and a human approval, so every doubt
resolves to a violation. Nothing here writes to the database: the catalog is
read lazily, per lookup, and the plan's effects live in an in-memory overlay
(added virtual files, removed catalog entries, implied directories).

Content identity: a live entry's key is 'g:<content_group>' when hash-proven
and 'e:<entry id>' otherwise. The 'e:' key is unique on purpose, so an
unhashed file is never taken to equal another file; only a file created by
copy/move inherits its source's key.

Files under the quarantine directory are never "live content": a purge would
destroy them, so they must not serve as witnesses or count as surviving copies.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from . import db, plans
from .models import Op, RemoteDelta, SimulationReport, Violation

_NODE_COLS = "id, path, is_dir, size, name_norm, content_group, is_native_doc, md5, sha1, sha256, dropbox, quickxor"
_CHUNK = 500  # bound parameters per IN (...) query


@dataclass
class SafetyPolicy:
    allow_probable_delete: bool = False
    min_copies: int = 1
    free_bytes: dict[int, int] | None = None   # remote_id -> known free bytes; None = don't check quota
    sample_size: int = 50
    quarantine_dir: str = ".drive-merge-quarantine"  # entries under it are not live copies; '' disables


def _fold(s: str) -> str:
    """NFC + casefold: how case-insensitive remotes (and entries.name_norm) compare names."""
    return unicodedata.normalize("NFC", s).casefold()


def _chunks(items: Sequence, n: int = _CHUNK) -> Iterator[Sequence]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _path_problem(path: object) -> str | None:
    """Why a path cannot be handed to rclone safely, or None.

    '..' or an absolute path would let an op act outside the remote root; an
    empty segment means the path is not in the catalog's canonical form.
    """
    if not isinstance(path, str) or not path:
        return "is empty"
    if "\x00" in path:
        return "contains a NUL byte"
    if path.startswith("/"):
        return "is absolute"
    for seg in path.split("/"):
        if seg == "":
            return "has an empty segment"
        if seg in (".", ".."):
            return f"has a {seg!r} segment"
    return None


class _Node:
    """A live catalog entry or a virtual file created by the plan."""

    __slots__ = ("id", "path", "is_dir", "size", "name_norm", "key", "native", "hashes", "virtual")

    def __init__(self, id: int | None, path: str, is_dir: bool, size: int | None, name_norm: str,
                 key: str, native: bool, hashes: tuple, virtual: bool):
        self.id = id
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.name_norm = name_norm
        self.key = key
        self.native = native
        self.hashes = hashes
        self.virtual = virtual


def _node(r: Sequence) -> _Node:
    eid, path, is_dir, size, name_norm, group, native = tuple(r[:7])
    key = f"g:{group}" if group is not None else f"e:{eid}"
    return _Node(eid, path, bool(is_dir), size, name_norm, key, bool(native), tuple(r[7:12]), False)


def _compare(a: _Node, b: _Node) -> str:
    """'same' (hash-proven identical), 'different' (provably not), or 'unproven'.

    Differing sizes or a differing hash of one type prove a difference even
    when the content keys claim otherwise (a corrupt content_group must not
    be able to authorise a delete or an overwrite).
    """
    if a.native != b.native:
        return "different"
    if a.size is not None and b.size is not None and a.size != b.size:
        return "different"
    for x, y in zip(a.hashes, b.hashes, strict=True):
        if x is not None and y is not None and str(x).lower() != str(y).lower():
            return "different"
    return "same" if a.key == b.key else "unproven"


@dataclass
class _Remote:
    id: int
    name: str
    writable: bool
    ci: bool


@dataclass
class _Witness:
    seq: int
    remote_id: int
    path: str
    key: str        # content key the witness must still carry when the plan is over
    src_key: str    # content key it vouches for
    probable: bool


class _Engine:
    def __init__(self, conn: sqlite3.Connection, policy: SafetyPolicy):
        self.conn = conn
        self.policy = policy
        self.remotes: dict[int, _Remote] = {
            r[0]: _Remote(r[0], r[1], bool(r[2]), bool(r[3]))
            for r in conn.execute("SELECT id, name, write_remote, case_insensitive FROM remotes")
        }
        q = (policy.quarantine_dir or "").strip("/")
        self._fq = _fold(q)
        self._qsql = " AND NOT (path = ? OR substr(path, 1, ?) = ?)" if q else ""
        self._qargs: tuple = (q, len(q) + 1, q + "/") if q else ()
        self.seqs: set[int] = set()
        # Catalog reads are immutable during a run, so they are cached; the overlay is not.
        self._base_cache: dict[tuple[int, str], list[_Node]] = {}
        self._ci_cache: dict[tuple[int, str], list[tuple[str, _Node]]] = {}
        # Overlay.
        self.removed: set[int] = set()                       # catalog entry ids taken out of the live state
        self.vfiles: dict[tuple[int, str], _Node] = {}       # files created by the plan
        self.vdirs: set[tuple[int, str]] = set()             # directories implied by created files (never un-made)
        self.vfold: dict[tuple[int, str], set[str]] = {}     # case-insensitive remotes: folded path -> real paths
        self.gone: dict[tuple[int, str], int] = {}           # (remote, path) -> seq of the op that emptied it
        # Accounting.
        self.kdelta: dict[str, int] = {}                     # content key -> net live copies added/removed
        self.touched: dict[str, tuple[int, str]] = {}        # keys of applied deletes/moves -> (seq, source)
        self.witnesses: list[_Witness] = []
        self.probable_for: dict[tuple[int, str, str], str] = {}  # (remote, path, witness key) -> content it vouched for
        self.rstat: dict[int, list[int]] = {}                # remote -> [files, bytes, bytes_in, q_files, q_bytes]
        self.bytes_cross = 0
        self.quota_seq: dict[int, int] = {}
        self.violations: list[Violation] = []
        self.warnings: list[Violation] = []

    # ------------------------------------------------------------ catalog reads

    def _live_sql(self) -> str:
        return "present = 1 AND is_dir = 0" + self._qsql

    def _base(self, rid: int, path: str) -> list[_Node]:
        k = (rid, path)
        nodes = self._base_cache.get(k)
        if nodes is None:
            nodes = [_node(r) for r in self.conn.execute(
                f"SELECT {_NODE_COLS} FROM entries WHERE remote_id = ? AND path = ? AND present = 1", k)]
            self._base_cache[k] = nodes
        return nodes

    def files_at(self, rid: int, path: str) -> list[_Node]:
        out = [n for n in self._base(rid, path) if not n.is_dir and n.id not in self.removed]
        v = self.vfiles.get((rid, path))
        if v is not None:
            out.append(v)
        return out

    def has_dir(self, rid: int, path: str) -> bool:
        return (rid, path) in self.vdirs or any(n.is_dir for n in self._base(rid, path))

    def _lookup(self, rid: int, path: str) -> tuple[_Node | None, int]:
        """(the single live file at path or None, number of live entries there).

        More than one entry (Drive duplicates, or a file and a folder with one
        name) cannot be addressed unambiguously, so the node is withheld.
        """
        files = self.files_at(rid, path)
        n = len(files) + (1 if self.has_dir(rid, path) else 0)
        return (files[0] if n == 1 and files else None), n

    def _ci_nodes(self, rid: int, path: str) -> list[tuple[str, bool]]:
        """(real path, is_dir) of live things whose NFC+casefold path equals path's, exact match included.

        Found through name_norm (indexed), not by scanning the parent: a
        folder with 100k files would make every lookup linear.
        """
        fp = _fold(path)
        nn = _fold(path.rsplit("/", 1)[-1])
        rows = self._ci_cache.get((rid, nn))
        if rows is None:
            rows = [(_fold(n.path), n) for n in map(_node, self.conn.execute(
                f"SELECT {_NODE_COLS} FROM entries WHERE name_norm = ? AND +remote_id = ? AND present = 1", (nn, rid)))]
            self._ci_cache[(rid, nn)] = rows
        out = [(n.path, n.is_dir) for f, n in rows if f == fp and (n.is_dir or n.id not in self.removed)]
        out.extend((p, (rid, p) in self.vdirs) for p in self.vfold.get((rid, fp), ()))
        return out

    # ----------------------------------------------------------------- overlay

    def label(self, rid: int | None, path: str | None) -> str | None:
        """'remote:path' for the report's sample; None when the op has no such address."""
        if rid is None or path is None:
            return None
        r = self.remotes.get(rid)
        return f"{r.name if r else '#' + str(rid)}:{path}"

    def _w(self, rid: int | None, path: object) -> str:
        """Remote:path for messages. repr() because file names are untrusted text."""
        return repr(self.label(rid, path))

    def _add(self, rid: int, path: str, tpl: _Node, entry_id: int | None) -> None:
        """Create a virtual file that inherits tpl's identity (key, size, hashes) and its parents."""
        node = _Node(entry_id, path, False, tpl.size, _fold(path.rsplit("/", 1)[-1]),
                     tpl.key, tpl.native, tpl.hashes, True)
        self.vfiles[(rid, path)] = node
        ci = self.remotes[rid].ci
        if ci:
            self.vfold.setdefault((rid, _fold(path)), set()).add(path)
        i = path.rfind("/")
        while i > 0:
            anc = path[:i]
            if (rid, anc) in self.vdirs:
                break  # a registered directory has all its ancestors registered
            self.vdirs.add((rid, anc))
            if ci:
                self.vfold.setdefault((rid, _fold(anc)), set()).add(anc)
            i = path.rfind("/", 0, i)
        size = node.size or 0
        st = self._stat(rid)
        st[0] += 1
        st[1] += size
        self.kdelta[node.key] = self.kdelta.get(node.key, 0) + 1

    def _remove(self, node: _Node, rid: int, path: str, seq: int) -> None:
        if node.virtual:
            del self.vfiles[(rid, path)]
            if self.remotes[rid].ci:
                self.vfold.get((rid, _fold(path)), set()).discard(path)
        else:
            self.removed.add(node.id)  # type: ignore[arg-type]
        self.gone[(rid, path)] = seq
        st = self._stat(rid)
        st[0] -= 1
        st[1] -= node.size or 0
        self.kdelta[node.key] = self.kdelta.get(node.key, 0) - 1

    def _stat(self, rid: int) -> list[int]:
        st = self.rstat.get(rid)
        if st is None:
            st = self.rstat[rid] = [0, 0, 0, 0, 0]
        return st

    def _quarantine(self, rid: int, size: int) -> None:
        st = self._stat(rid)
        st[3] += 1
        st[4] += size

    def _touch(self, node: _Node, rid: int, path: str, seq: int) -> None:
        self.touched.setdefault(node.key, (seq, self._w(rid, path)))

    # --------------------------------------------------------------------- ops

    def _addr(self, rid: object, path: object, role: str, bad) -> bool:
        ok = True
        if rid not in self.remotes:
            bad("BAD_PATH", f"{role} remote id {rid!r} does not exist")
            ok = False
        problem = _path_problem(path)
        if problem:
            bad("BAD_PATH", f"{role} path {path!r} {problem}")
            ok = False
        elif self._fq and (_fold(path) == self._fq or _fold(path).startswith(self._fq + "/")):  # type: ignore[arg-type]
            bad("BAD_PATH", f"{role} path {path!r} is inside the quarantine area")
            ok = False
        return ok

    def _why_missing(self, rid: int, path: str) -> str:
        seq = self.gone.get((rid, path))
        if seq is not None:
            return f"it was already moved or deleted by op {seq}"
        if self.has_dir(rid, path):
            return "that path is a directory"
        if self.conn.execute("SELECT 1 FROM entries WHERE remote_id = ? AND path = ? AND present = 0 LIMIT 1",
                             (rid, path)).fetchone():
            return "the catalog marks it missing (tombstone)"
        return "the catalog has no such file"

    def run_op(self, op: Op, seq: int) -> None:
        viol: list[Violation] = []
        warn: list[Violation] = []

        def bad(code: str, msg: str) -> None:
            viol.append(Violation(code, msg, seq))

        kind = op.op
        if kind not in ("copy", "move", "delete"):
            bad("BAD_PATH", f"unknown op kind {kind!r}")
            self.violations.extend(viol)
            return
        two = kind != "delete"
        srid, spath, drid, dpath = op.src_remote_id, op.src_path, op.dst_remote_id, op.dst_path
        wrid, wpath = op.witness_remote_id, op.witness_path

        src_ok = self._addr(srid, spath, "source", bad)
        dst_ok = False
        if two:
            if drid is None or dpath is None:
                bad("BAD_PATH", f"{kind} needs a destination remote and path")
            else:
                dst_ok = self._addr(drid, dpath, "destination", bad)
        wit_given = not two and wrid is not None and wpath is not None
        wit_ok = wit_given and self._addr(wrid, wpath, "witness", bad)

        dep = op.depends_on
        if dep is not None and not (isinstance(dep, int) and 1 <= dep < seq and dep in self.seqs):
            bad("DEPENDENCY", f"depends_on {dep!r} is not an earlier op of this plan")

        written = ([drid] if two else []) + ([srid] if kind != "copy" else [])
        for rid in dict.fromkeys(written):  # once per remote, even for a move inside one
            if rid in self.remotes and not self.remotes[rid].writable:
                bad("READ_ONLY_REMOTE", f"{self._w(rid, '')} has no write_remote configured")
        if kind == "move" and drid is not None and drid != srid:
            bad("CROSS_REMOTE_MOVE", "a move cannot cross remotes; use copy + delete")

        if not src_ok:
            self.violations.extend(viol)
            return

        # Source.
        sw = self._w(srid, spath)
        src, n = self._lookup(srid, spath)
        if n > 1:
            bad("AMBIGUOUS_PATH", f"{n} live entries share {sw}; rclone cannot address one of them")
        elif src is None:
            bad("SRC_MISSING", f"no live file at {sw}: {self._why_missing(srid, spath)}")
        elif op.src_entry_id is not None and src.id != op.src_entry_id:
            what = "a file created earlier in this plan" if src.id is None else f"entry {src.id}"
            bad("SRC_MISSING", f"{sw} holds {what}, not the planned entry {op.src_entry_id}")
            src = None
        if src is not None:
            if op.expect_size is not None and src.size != op.expect_size:
                bad("EXPECT_MISMATCH", f"{sw} is {src.size} bytes at this step, the plan expected {op.expect_size}")
            if src.native and (kind == "delete" or (kind == "copy" and drid != srid)):
                bad("NATIVE_DOC", f"{sw} is a Google-native doc: no bytes to copy or to verify a delete against")

        action = "add"
        witness: _Witness | None = None
        if src is not None and two and dst_ok:
            action = self._check_dst(op, src, bad)
        elif src is not None and kind == "delete" and not src.native:
            witness = self._check_witness(op, seq, src, wit_given, wit_ok, bad, warn)

        if viol:
            self.violations.extend(viol)
            return
        if src is None:  # unreachable without a violation; refuse rather than apply blindly
            return
        self.warnings.extend(warn)
        self._apply(op, seq, src, action, witness)

    def _check_dst(self, op: Op, src: _Node, bad) -> str:
        """Returns 'add', or 'same' when the destination already holds identical content."""
        drid, dpath = op.dst_remote_id, op.dst_path
        assert drid is not None and dpath is not None
        dw = self._w(drid, dpath)
        parts = dpath.split("/")
        ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
        for anc in ancestors:
            if self.files_at(drid, anc):
                bad("DST_EXISTS", f"{self._w(drid, anc)} is a file, so {dw} cannot be created under it")
                break
        action = "add"
        if self.has_dir(drid, dpath):
            bad("DST_EXISTS", f"{dw} is a directory")
        else:
            existing = self.files_at(drid, dpath)
            if len(existing) > 1:
                bad("AMBIGUOUS_PATH", f"{len(existing)} live entries share the destination {dw}")
            elif existing:
                if _compare(src, existing[0]) == "same":
                    action = "same"
                else:
                    bad("DST_EXISTS", f"{dw} holds different or unproven content; never overwritten")
        if self.remotes[drid].ci:
            self._check_case(op, ancestors, bad)
        return action

    def _check_case(self, op: Op, ancestors: list[str], bad) -> None:
        """Case-insensitive remotes: two names differing only by case are one name."""
        drid, dpath = op.dst_remote_id, op.dst_path
        assert drid is not None and dpath is not None
        for anc in ancestors:
            for p, is_dir in self._ci_nodes(drid, anc):
                if p != anc and not is_dir:
                    bad("CASE_COLLISION", f"{self._w(drid, anc)} collides with the file {self._w(drid, p)} (case-insensitive)")
                    return
        for p, _ in self._ci_nodes(drid, dpath):
            if p == dpath:
                continue
            if op.op == "move" and drid == op.src_remote_id and p == op.src_path:
                continue  # a case-only rename: the "collision" is the file itself
            bad("CASE_COLLISION", f"{self._w(drid, dpath)} collides with {self._w(drid, p)} (case-insensitive)")
            return

    def _check_witness(self, op: Op, seq: int, src: _Node, given: bool, addr_ok: bool, bad, warn) -> _Witness | None:
        wrid, wpath = op.witness_remote_id, op.witness_path
        if not given:
            bad("NO_WITNESS", "a delete must name a witness: the identical copy that survives")
            return None
        if not addr_ok:
            return None  # BAD_PATH already recorded
        assert wrid is not None and wpath is not None
        ww = self._w(wrid, wpath)
        if wrid == op.src_remote_id and (
                wpath == op.src_path or (self.remotes[wrid].ci and _fold(wpath) == _fold(op.src_path))):
            bad("NO_WITNESS", f"the witness {ww} is the file being deleted")
            return None
        w, n = self._lookup(wrid, wpath)
        if n > 1:
            bad("AMBIGUOUS_PATH", f"{n} live entries share the witness path {ww}")
            return None
        if w is None:
            bad("NO_WITNESS", f"no live file at the witness {ww}: {self._why_missing(wrid, wpath)}")
            return None
        verdict = _compare(src, w)
        if verdict == "same":
            return _Witness(seq, wrid, wpath, w.key, src.key, False)
        if (verdict == "unproven" and self.policy.allow_probable_delete and not w.native
                and src.size is not None and w.size is not None and w.name_norm == src.name_norm):
            if self.probable_for.get((wrid, wpath, w.key), src.key) != src.key:
                bad("WITNESS_MISMATCH", f"{ww} already vouches, by name and size only, for a different file; "
                                        f"one probable match cannot cover two contents")
                return None
            warn.append(Violation(
                "PROBABLE_ONLY", f"{ww} matches by name and size only; content is not hash-proven", seq))
            return _Witness(seq, wrid, wpath, w.key, src.key, True)
        why = {"different": "its size or hash differs", "unproven": "its content is not provably identical"}[verdict]
        bad("WITNESS_MISMATCH", f"witness {ww} does not match the deleted file: {why}")
        return None

    def _apply(self, op: Op, seq: int, src: _Node, action: str, witness: _Witness | None) -> None:
        kind = op.op
        srid, spath = op.src_remote_id, op.src_path
        drid, dpath = op.dst_remote_id, op.dst_path
        size = src.size or 0
        if kind == "copy":
            assert drid is not None and dpath is not None
            if action == "same":
                self.warnings.append(Violation("NOOP", f"{self._w(drid, dpath)} already holds identical content", seq))
                return
            self._add(drid, dpath, src, None)
            st = self._stat(drid)
            st[2] += size
            free = (self.policy.free_bytes or {}).get(drid)
            if free is not None and st[2] > free:
                self.quota_seq.setdefault(drid, seq)
            if drid != srid:
                self.bytes_cross += size
        elif kind == "move":
            assert drid is not None and dpath is not None
            if drid == srid and dpath == spath:
                self.warnings.append(Violation("NOOP", f"move of {self._w(srid, spath)} onto itself", seq))
                return
            self._touch(src, srid, spath, seq)
            self._remove(src, srid, spath, seq)
            if action == "same":
                self.warnings.append(Violation(
                    "NOOP", f"{self._w(drid, dpath)} already holds identical content; "
                            f"the move only removes {self._w(srid, spath)}", seq))
                self._quarantine(srid, size)
                self.witnesses.append(_Witness(seq, drid, dpath, src.key, src.key, False))
            else:
                self._add(drid, dpath, src, src.id)
        else:
            assert witness is not None
            self._touch(src, srid, spath, seq)
            self._remove(src, srid, spath, seq)
            self._quarantine(srid, size)
            self.witnesses.append(witness)
            if witness.probable:
                self.probable_for[(witness.remote_id, witness.path, witness.key)] = src.key

    # ----------------------------------------------------------------- finish

    def _key_counts(self, keys: Sequence[str], rids: Sequence[int] | None) -> dict[str, int]:
        """Live copies per content key, catalog-wide or restricted to some remotes."""
        out: dict[str, int] = {}
        rfrag = "" if rids is None else f" AND remote_id IN ({','.join('?' * len(rids))})"
        rargs: tuple = () if rids is None else tuple(rids)
        groups = [k for k in keys if k.startswith("g:")]
        entries = [k for k in keys if k.startswith("e:")]
        for chunk in _chunks(groups):
            ph = ",".join("?" * len(chunk))
            for g, c in self.conn.execute(
                    f"SELECT content_group, COUNT(*) FROM entries WHERE content_group IN ({ph}) "
                    f"AND {self._live_sql()}{rfrag} GROUP BY content_group",
                    (*(int(k[2:]) for k in chunk), *self._qargs, *rargs)):
                out[f"g:{g}"] = c
        for chunk in _chunks(entries):
            ph = ",".join("?" * len(chunk))
            for (i,) in self.conn.execute(
                    f"SELECT id FROM entries WHERE id IN ({ph}) AND {self._live_sql()}{rfrag}",
                    (*(int(k[2:]) for k in chunk), *self._qargs, *rargs)):
                out[f"e:{i}"] = 1
        return out

    def finish(self, touched_remotes: list[int]) -> tuple[list[RemoteDelta], int, int, int]:
        """End-of-plan checks. Returns (remote deltas, contents before, after, lost)."""
        # 1. Every witness must still be live, with its content, once the whole plan has run.
        cover: set[tuple[str, int, str]] = set()
        for w in self.witnesses:
            cur = self.files_at(w.remote_id, w.path)
            if len(cur) != 1 or cur[0].key != w.key:
                self.violations.append(Violation(
                    "NO_WITNESS", f"witness {self._w(w.remote_id, w.path)} is no longer live with the same "
                                  f"content at the end of the plan", w.seq))
            elif w.probable:
                cover.add((w.src_key, w.remote_id, w.path))
        cover_n: dict[str, int] = {}
        for key, _, _ in cover:
            cover_n[key] = cover_n.get(key, 0) + 1

        # 2. Content that was touched must keep its copies. An accepted probable witness stands in
        #    for one copy of the content it vouched for.
        before_all = self._key_counts(list(self.touched), None)
        lost = 0
        for key, (seq, src_desc) in self.touched.items():
            before = before_all.get(key, 0)
            after = before + self.kdelta.get(key, 0) + cover_n.get(key, 0)
            need = max(1, min(self.policy.min_copies, before))
            if after <= 0:
                lost += 1
            if after < need:
                self.violations.append(Violation(
                    "CONTENT_LOST", f"{src_desc}: content would be left with {max(after, 0)} live "
                                    f"cop{'y' if after == 1 else 'ies'} (policy needs {need})", seq))

        # 3. Quota. Quarantine is a move inside the remote, so deletes never offset it.
        for rid, seq in self.quota_seq.items():
            st = self.rstat[rid]
            self.violations.append(Violation(
                "QUOTA", f"{self._w(rid, '')} would receive {st[2]} bytes but only "
                         f"{(self.policy.free_bytes or {})[rid]} are free", seq))

        # 4. Before/after numbers.
        deltas: list[RemoteDelta] = []
        for rid in touched_remotes:
            files, nbytes = self.conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM entries WHERE remote_id = ? AND {self._live_sql()}",
                (rid, *self._qargs)).fetchone()
            st = self._stat(rid)
            deltas.append(RemoteDelta(rid, self.remotes[rid].name, files, nbytes, files + st[0], nbytes + st[1],
                                      st[2], st[3], st[4]))
        return (deltas, *self._contents(touched_remotes), lost)

    def _contents(self, rids: list[int]) -> tuple[int, int]:
        """Distinct content keys among live files of the touched remotes, before and after."""
        if not rids:
            return 0, 0
        ph = ",".join("?" * len(rids))
        live = f"remote_id IN ({ph}) AND {self._live_sql()}"
        grouped = self.conn.execute(
            f"SELECT COUNT(DISTINCT content_group) FROM entries WHERE {live} AND content_group IS NOT NULL",
            (*rids, *self._qargs)).fetchone()[0]
        ungrouped = self.conn.execute(
            f"SELECT COUNT(*) FROM entries WHERE {live} AND content_group IS NULL", (*rids, *self._qargs)).fetchone()[0]
        before = grouped + ungrouped
        changed = [k for k, d in self.kdelta.items() if d]
        base = self._key_counts(changed, rids)
        after = before
        for k in changed:
            b = base.get(k, 0)
            a = b + self.kdelta[k]
            if b == 0 and a > 0:
                after += 1
            elif b > 0 and a <= 0:
                after -= 1
        return before, after


def _ordered(ops: Sequence[Op]) -> list[tuple[int, Op]]:
    """(seq, op) in execution order. Ops that were never numbered take their list position."""
    if all(not o.seq for o in ops):
        return list(enumerate(ops, start=1))
    return sorted(((o.seq or 0, o) for o in ops), key=lambda t: t[0])


def simulate_ops(conn: sqlite3.Connection, ops: list[Op], *, plan_id: int | None = None,
                 policy: SafetyPolicy | None = None, digest: str = "") -> SimulationReport:
    """Apply ops in seq order to a virtual copy of the catalog and report every violation.

    Read-only: it never writes to the database. An op with a violation is not
    applied, so later ops see the state as it would be if the problem were fixed.
    """
    policy = policy or SafetyPolicy()
    eng = _Engine(conn, policy)
    ordered = _ordered(ops)
    eng.seqs = {s for s, _ in ordered}
    touched = sorted({rid for _, o in ordered for rid in (o.src_remote_id, o.dst_remote_id, o.witness_remote_id)
                      if rid in eng.remotes})
    counts = {"copy": 0, "move": 0, "delete": 0}
    last = 0
    for seq, op in ordered:
        if op.op in counts:
            counts[op.op] += 1
        if seq < 1 or seq == last:
            eng.violations.append(Violation("DEPENDENCY", f"seq {seq} is not a unique positive position", seq))
        else:
            eng.run_op(op, seq)
        last = seq

    deltas, contents_before, contents_after, lost = eng.finish(touched)
    eng.violations.sort(key=lambda v: (v.seq is None, v.seq or 0))
    sample = [{
        "seq": seq, "op": op.op,
        "from": eng.label(op.src_remote_id, op.src_path),
        "to": eng.label(op.dst_remote_id, op.dst_path) if op.op != "delete" else None,
        "witness": eng.label(op.witness_remote_id, op.witness_path),
        "reason": op.reason,
    } for seq, op in ordered[:max(0, policy.sample_size)]]
    return SimulationReport(
        plan_id=plan_id, digest=digest, ok=not eng.violations,
        violations=eng.violations, warnings=eng.warnings, remotes=deltas,
        contents_before=contents_before, contents_after=contents_after, contents_lost=lost,
        bytes_cross_remote=eng.bytes_cross, op_counts=counts, sample=sample,
    )


def simulate_plan(conn: sqlite3.Connection, plan_id: int, *, policy: SafetyPolicy | None = None) -> SimulationReport:
    """Simulate a stored plan and record the result (moves it to 'simulated' or back to 'draft').

    The ops, the snapshot the digest binds to and the simulation all read one
    consistent catalog state, so an approval can never be bound to a snapshot
    newer than the data the simulation actually saw.
    """
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        ops = plans.load_ops(conn, plan_id)
        snapshot = db.snapshot(conn)
        digest = plans.compute_digest(plan_id, ops, snapshot)
        report = simulate_ops(conn, ops, plan_id=plan_id, policy=policy, digest=digest)
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    plans.record_simulation(conn, plan_id, report, snapshot)
    return report
