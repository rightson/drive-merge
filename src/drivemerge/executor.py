"""Execute approved plans against the real clouds; restore and purge quarantined deletes.

The executor is the last line of defence and trusts neither the simulator nor
the catalog: every op re-reads the live state of the clouds, and any doubt
fails the op and stops the plan (fail-closed). Nothing is ever overwritten.
execute_plan never removes bytes: a "delete" is a move into
<quarantine_dir>/<plan id>/ on the same cloud, and only purge_quarantine
(a separate, human-approved command) deletes, one file at a time and only
below that directory. A move onto a destination that already holds identical
content is a delete in disguise (the simulator models it so): the source is
quarantined, the destination is its witness, and nothing is overwritten.

"Provably identical" never rests on size alone, nor on a weak hash. Two files
are proven equal by a collision-resistant hash type (identity.STRONG_TYPES:
sha256, the Dropbox content hash) both clouds report, or by a
hash_bridges row linking their hash domains, or by downloading what is needed
and hashing it locally. md5, sha1 and QuickXorHash (OneDrive) only prove a
difference: a file with nothing stronger, like one with no hash at all
(iCloud), must be downloaded unless a bridge made from its own bytes vouches
for it. A download that supplied the missing link is recorded as a bridge so
later ops need none.

A path is never trusted to name two different objects just because it is
spelled differently: a local disk can be case-insensitive (APFS, NTFS) without
the config saying so, and a rename to another case, or a witness spelled in
another case, would then be the file itself (see _Run.may_be_same_object).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, hashing, identity, plans
from .config import Config, RemoteConfig
from .models import Op
from .rclone import Rclone, RcloneError
from .scanner import parse_mtime

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"   # db.utcnow(): fixed width, so strings compare chronologically


class ExecutionError(Exception):
    """Execution cannot start. `code` / `params` are set for the refusals a page words itself (PLAN_EXECUTING)."""

    code: str | None = None
    params: dict = {}

    def __init__(self, *args: object, code: str | None = None, **params: object):
        super().__init__(*args)
        if code is not None:
            self.code = code
        self.params = params


def _plan_executing(plan_id: int) -> ExecutionError:
    return ExecutionError(f"plan {plan_id} is executing; wait for it to finish", code="PLAN_EXECUTING", plan=plan_id)


class ExecutionDisabled(ExecutionError):
    pass


@dataclass
class ExecResult:
    plan_id: int
    status: str
    done: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None
    # restore and purge only: one entry per item that stayed, at most MAX_FAILURES,
    # {op_id, seq, remote, path (the original path), code, params, error}
    failures: list[dict] = field(default_factory=list)


MAX_FAILURES = 200


class _OpFailed(Exception):
    """An op cannot run or did not verify; the message is stored as plan_ops.error.

    `code` and `params` let a page word the failure in its own language (restore and purge failures are
    reported with them): ORIGINAL_TAKEN, QUARANTINE_GONE, READ_ONLY, BAD_PATH, VERIFY_FAILED,
    NO_WITNESS_RECORD, WITNESS_UNPROVEN; anything else is OP_FAILED with no params.
    """

    def __init__(self, message: str = "", code: str = "OP_FAILED", **params: object):
        super().__init__(message)
        self.code = code
        self.params = params


# --------------------------------------------------------------------------- #
# Paths and quarantine
# --------------------------------------------------------------------------- #

def _path_problem(path: object) -> str | None:
    """Why a path must never reach rclone, or None. Mirrors the simulator's BAD_PATH."""
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


def _quarantine_root(config: Config) -> str:
    root = (config.safety.quarantine_dir or "").rstrip("/")
    if _path_problem(root):
        raise ExecutionError(f"unsafe [safety] quarantine_dir {config.safety.quarantine_dir!r}")
    return root


def quarantine_path(config: Config, plan_id: int, path: str) -> str:
    """Where a deleted file waits: <quarantine_dir>/<plan id>/<original path>, on its own cloud."""
    return f"{_quarantine_root(config)}/{plan_id}/{path}"


def _inside_quarantine(config: Config, plan_id: int, qpath: str) -> bool:
    """True only for a normalised path strictly below <quarantine_dir>/<plan id>/ (the purge guard)."""
    prefix = f"{_quarantine_root(config)}/{plan_id}/"
    return qpath.startswith(prefix) and _path_problem(qpath[len(prefix):]) is None


def _fold(s: str, case_insensitive: bool) -> str:
    s = unicodedata.normalize("NFC", s)
    return s.casefold() if case_insensitive else s


def _require_enabled(config: Config) -> None:
    if not config.safety.execution_enabled:
        raise ExecutionDisabled("execution is disabled: set [safety] execution_enabled = true in the config")


# --------------------------------------------------------------------------- #
# What we know about one live file
# --------------------------------------------------------------------------- #

@dataclass
class _Side:
    rid: int
    name: str
    rc: RemoteConfig
    path: str
    size: int
    mtime: str | None                 # canonical (scanner.parse_mtime), for bridge source links
    native: dict[str, str]            # strong hashes the cloud reports for the file right now
    entry_id: int | None              # live catalog entry at this path, if exactly one
    known: dict[str, str] | None = None   # native + bridge-derived + download-derived; None = not computed yet

    @property
    def loc(self) -> str:
        return f"{self.name}:{self.path}"

    def signature(self) -> tuple:
        return self.size, self.mtime, tuple(sorted(self.native.items()))


@dataclass
class _Proof:
    verdict: str    # 'same' | 'different' | 'unproven'
    how: str        # which evidence decided it
    detail: str = ""


def _compare(a: dict[str, str], b: dict[str, str], *, weak_ok: bool = False) -> str | None:
    """'same' / 'different' over the hash types both sides know, None when that settles nothing.

    Any disagreement beats any agreement: a side whose hashes contradict each
    other is not "mostly the same". Disagreement on any shared type, md5, sha1
    and QuickXor included, proves a difference. Agreement proves identity only
    through a collision-resistant type; agreeing md5/sha1/QuickXor is exactly what
    two different files can do, so alone it is None. `weak_ok` accepts it, for
    the one caller whose two sides are the same object (a rename).
    """
    common = a.keys() & b.keys()
    if any(a[t] != b[t] for t in common):
        return "different"
    if common.intersection(identity.STRONG_TYPES) or (weak_ok and common):
        return "same"
    return None


def _has_strong(hashes: dict[str, str]) -> bool:
    """Can these hashes, compared with a file's, prove it identical (not only different)?"""
    return any(t in hashes for t in identity.STRONG_TYPES)


def _native_hashes(raw: dict) -> dict[str, str]:
    out = {}
    for key, val in (raw.get("Hashes") or {}).items():
        key = str(key).lower()
        if key in hashing.HASH_TYPES and isinstance(val, str) and val:
            out[key] = val.lower()
    return out


# --------------------------------------------------------------------------- #
# One execution context
# --------------------------------------------------------------------------- #

class _Run:
    def __init__(self, conn: sqlite3.Connection, rclone: Rclone, config: Config, plan_id: int,
                 tmp: Path | None = None):
        self.conn = conn
        self.rclone = rclone
        self.config = config
        self.plan_id = plan_id
        self.tmp = tmp
        self._remotes: dict[int, tuple[str, RemoteConfig, bool]] = {}

    # ----------------------------------------------------------- bookkeeping

    def log(self, action: str, op: Op | None = None, **detail) -> None:
        db.journal(self.conn, action, plan_id=self.plan_id, op_id=op.id if op else None,
                   detail=json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str))

    def remote(self, rid: int) -> tuple[str, RemoteConfig, bool]:
        """(name, config, case_insensitive) for a catalog remote id."""
        if rid not in self._remotes:
            row = self.conn.execute("SELECT name, case_insensitive FROM remotes WHERE id = ?", (rid,)).fetchone()
            if row is None:
                raise _OpFailed(f"remote id {rid} is not in the catalog")
            try:
                rc = self.config.remote(row[0])
            except KeyError:
                raise _OpFailed(f"remote {row[0]!r} is not in the configuration") from None
            self._remotes[rid] = (row[0], rc, bool(row[1]))
        return self._remotes[rid]

    def name(self, rid: int | None) -> str:
        if rid is None:
            return "?"
        try:
            return self.remote(rid)[0]
        except _OpFailed:
            return f"#{rid}"

    def writable(self, rid: int, what: str) -> str:
        """The rclone path the executor may modify on this remote, or fail the op."""
        name, rc, _ = self.remote(rid)
        if not rc.write:
            raise _OpFailed(f"{what} remote {name!r} is read-only (no write remote configured)",
                            "READ_ONLY", remote=name)
        return rc.write

    # ------------------------------------------------------------- live state

    def _raw(self, rid: int, path: str) -> dict | None:
        name, rc, _ = self.remote(rid)
        try:
            return self.rclone.stat(rc.read, path, hash_types=rc.scan_hash_types)
        except RcloneError as e:
            raise _OpFailed(f"cannot stat {name}:{path}: {e}") from e

    def _entry_at(self, rid: int, path: str, hint: int | None = None) -> int | None:
        """The live catalog file at this location (hint verified, never trusted), if unambiguous."""
        rows = self.conn.execute(
            "SELECT id FROM entries WHERE remote_id = ? AND path = ? AND present = 1 AND is_dir = 0", (rid, path)
        ).fetchall()
        ids = [r[0] for r in rows]
        if hint is not None and hint in ids:
            return hint
        return ids[0] if len(ids) == 1 else None

    def probe(self, role: str, rid: int, path: str, *, entry_hint: int | None = None) -> _Side | None:
        """Stat a file live. None = nothing there; a directory or a size-less object fails the op."""
        raw = self._raw(rid, path)
        if raw is None:
            return None
        name, rc, _ = self.remote(rid)
        if raw.get("IsDir"):
            raise _OpFailed(f"{role} {name}:{path} is a directory, not a file")
        size = raw.get("Size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise _OpFailed(f"{role} {name}:{path} has no known size (native document?)")
        return _Side(rid, name, rc, path, size, parse_mtime(raw.get("ModTime")), _native_hashes(raw),
                     self._entry_at(rid, path, entry_hint))

    # ------------------------------------------------------- content identity

    def _bridge_hashes(self, side: _Side) -> dict[str, str] | None:
        """All five hashes of the one bridge that describes this file now, or None.

        A bridge describes the file when it has the same size and shares a
        collision-resistant hash value the cloud reports for it, or when it was
        computed from this very catalog entry and the file's size and mtime are
        still what was hashed. A shared md5, sha1 or QuickXor value is not enough: it
        would let any other file with that weak hash borrow someone else's
        bridge. A bridge that contradicts any hash the cloud reports (weak ones
        included, they are how an in-place edit is noticed) is ignored, and so
        is an ambiguous match: both fall through to a download.
        """
        cur = self.conn.cursor()
        cur.row_factory = sqlite3.Row
        rows = []
        strong = [t for t in identity.STRONG_TYPES if t in side.native]
        if strong:
            clause = " OR ".join(f"{t} = ?" for t in strong)
            rows += cur.execute(f"SELECT * FROM hash_bridges WHERE size = ? AND ({clause})",
                                (side.size, *(side.native[t] for t in strong))).fetchall()
        if side.entry_id is not None and side.mtime is not None:
            rows += cur.execute("SELECT b.* FROM bridge_sources s JOIN hash_bridges b ON b.id = s.bridge_id "
                                "WHERE s.entry_id = ? AND s.size = ? AND s.mtime = ? AND b.size = s.size",
                                (side.entry_id, side.size, side.mtime)).fetchall()
        usable = {r["sha256"]: r for r in rows if all(r[t] == v for t, v in side.native.items())}
        if len(usable) != 1:
            return None
        row = next(iter(usable.values()))
        return {t: row[t] for t in hashing.HASH_TYPES}

    def _know(self, side: _Side) -> dict[str, str]:
        if side.known is None:
            side.known = dict(side.native)
            bridge = self._bridge_hashes(side)
            if bridge:
                side.known.update(bridge)
        return side.known

    def _fingerprint(self, side: _Side, op: Op) -> None:
        """Download the file, hash it locally and keep all five digests as what we know about it.

        The bytes must agree with everything already known (size and native
        hashes) and the file must not have changed while it was being read.
        """
        known = self._know(side)
        if self.tmp is None:
            raise _OpFailed(f"{side.loc} would have to be downloaded to be verified, but there is no scratch directory")
        local = self.tmp / f"verify-{uuid.uuid4().hex}.tmp"
        try:
            try:
                self.rclone.download(side.rc.read, side.path, str(local))
            except RcloneError as e:
                raise _OpFailed(f"cannot download {side.loc} to verify it: {e}") from e
            computed = hashing.hash_file(local)
        finally:
            local.unlink(missing_ok=True)
        if computed["size"] != side.size:
            raise _OpFailed(f"{side.loc} changed while being verified: {side.size} -> {computed['size']} bytes")
        for t, v in known.items():
            if computed[t] != v:
                raise _OpFailed(f"{side.loc} changed while being verified: {t} {v} -> {computed[t]}")
        again = self._raw(side.rid, side.path)
        if again is None or (again.get("Size"), parse_mtime(again.get("ModTime")), tuple(sorted(
                _native_hashes(again).items()))) != side.signature():
            raise _OpFailed(f"{side.loc} changed while being verified")
        linked = side.entry_id is not None and side.mtime is not None
        try:
            bridge_id = identity.record_bridge(self.conn, computed,
                                               source_entry_id=side.entry_id if linked else None,
                                               source_mtime=side.mtime if linked else None)
        except ValueError as e:
            raise _OpFailed(f"cannot record the hashes of {side.loc}: {e}") from e
        known.update({t: computed[t] for t in hashing.HASH_TYPES})
        self.log("hash", op, location=side.loc, size=side.size, bridge_id=bridge_id, sha256=computed["sha256"])

    def prove(self, a: _Side, b: _Side, op: Op) -> _Proof:
        """Are these two files provably the same bytes? Never decided by size or a weak hash alone."""
        if a.size != b.size:
            return _Proof("different", "size", f"{a.loc} is {a.size} bytes, {b.loc} is {b.size}")
        verdict = _compare(a.native, b.native)
        if verdict:
            return _Proof(verdict, "native hash")
        verdict = _compare(self._know(a), self._know(b))
        if verdict:
            return _Proof(verdict, "hash bridge")
        # Nothing strong links the two: read each side that has no strong hash (md5, sha1 and QuickXor agreeing
        # proves nothing), else the second (the copy / witness) so that the sides share a type.
        for side in [s for s in (a, b) if not _has_strong(s.known)] or [b]:
            self._fingerprint(side, op)
        verdict = _compare(a.known, b.known)
        if verdict is None:
            return _Proof("unproven", "download", "no hash type in common even after download")
        return _Proof(verdict, "download")

    def require_same(self, op: Op, a: _Side, b: _Side, what: str) -> _Proof:
        proof = self.prove(a, b, op)
        self.log("verify", op, what=what, a=a.loc, b=b.loc, verdict=proof.verdict, how=proof.how, detail=proof.detail)
        if proof.verdict != "same":
            reason = proof.detail or ("content differs" if proof.verdict == "different" else "cannot be proven")
            raise _OpFailed(f"{what}: {a.loc} and {b.loc} are not provably identical ({reason}; via {proof.how})")
        return proof

    @staticmethod
    def same_object(held: _Side, now: _Side, what: str) -> None:
        """Cheap check for a rename inside one cloud: same size and no contradicting native hash."""
        if held.size != now.size:
            raise _OpFailed(f"{what}: size changed {held.size} -> {now.size}")
        if _compare(held.native, now.native) == "different":
            raise _OpFailed(f"{what}: hashes changed")

    def verify_renamed(self, op: Op, src: _Side, out: _Side, what: str) -> None:
        """After a rename the cloud did itself, is the object that now sits at the new path the one that was moved?

        Telling an object from itself needs no collision resistance (that is only needed to tell two
        objects apart, see prove): the same size and the same md5/sha1/QuickXor values as before will do, and
        a OneDrive library can be reorganised without reading a byte. A cloud that reports nothing
        comparable is proven the long way (the source was read before it moved).
        """
        weak = _compare(src.native, out.native, weak_ok=True)
        if src.size == out.size and weak == "same" and _compare(src.native, out.native) is None:
            self.log("verify", op, what=what, a=src.loc, b=out.loc, verdict="same", how="native hash (same object)")
            return
        self.require_same(op, src, out, what)

    def may_be_same_object(self, rid: int, a: str, b: str) -> bool:
        """Could paths `a` and `b` on one remote be a single object spelled two ways?

        Not when they differ by more than case and Unicode form. When they do not, a remote flagged
        case-insensitive says yes. Any other cannot be believed to be case-sensitive (provider 'local'
        is never flagged, yet APFS and NTFS folders are not), so the first folder where the two
        spellings part is listed: a case-sensitive disk shows both names, a case-insensitive one only
        the name the object was created with. A failing listing fails the op.
        """
        if a == b:
            return True
        if _fold(a, True) != _fold(b, True):
            return False
        _, rc, ci = self.remote(rid)
        if ci:
            return True
        sa, sb = a.split("/"), b.split("/")
        part = next((i for i, (x, y) in enumerate(zip(sa, sb)) if x != y), None)
        if part is None or len(sa) != len(sb):
            return True
        try:
            names = {item.get("Name") for item in self.rclone.lsjson(rc.read, "/".join(sa[:part]), recursive=False)}
        except RcloneError as e:
            raise _OpFailed(f"cannot list {self.name(rid)}:{'/'.join(sa[:part])} to tell {a!r} from {b!r}: {e}") from e
        return not (sa[part] in names and sb[part] in names)

    # ------------------------------------------------------------------- ops

    def execute(self, op: Op, ops_by_seq: dict[int, Op]) -> str:
        """Run one op; returns 'done' or 'skipped'. Raises _OpFailed (or anything) when it must stop."""
        for label, path in (("source", op.src_path), ("destination", op.dst_path), ("witness", op.witness_path)):
            if path is not None and (problem := _path_problem(path)):
                raise _OpFailed(f"{label} path {path!r} {problem}")
        self._require_dependency(op, ops_by_seq)
        src = self._precheck(op)
        if op.op == "copy":
            return self._copy(op, src)
        if op.op == "move":
            return self._move(op, src)
        if op.op == "delete":
            return self._delete(op, src)
        raise _OpFailed(f"unknown op kind {op.op!r}")

    @staticmethod
    def _require_dependency(op: Op, ops_by_seq: dict[int, Op]) -> None:
        if op.depends_on is None:
            return
        dep = ops_by_seq.get(op.depends_on)
        if dep is None or op.depends_on >= op.seq:
            raise _OpFailed(f"depends on op {op.depends_on}, which is not an earlier op of this plan")
        # A copy skipped because the destination already held identical content has met its purpose;
        # a restored delete (also 'skipped') has not.
        if not (dep.status == "done" or (dep.status == "skipped" and dep.op == "copy")):
            raise _OpFailed(f"depends on op {op.depends_on}, which is {dep.status}")

    def _precheck(self, op: Op) -> _Side:
        src = self.probe("source", op.src_remote_id, op.src_path, entry_hint=op.src_entry_id)
        if src is None:
            raise _OpFailed(f"source {self.name(op.src_remote_id)}:{op.src_path} no longer exists")
        if op.expect_size is not None and src.size != op.expect_size:
            raise _OpFailed(f"source {src.loc} changed since the plan was made: "
                            f"{op.expect_size} -> {src.size} bytes")
        if op.expect_hash:
            kind, _, want = op.expect_hash.partition(":")
            kind, want = kind.lower(), want.lower()
            if kind in src.native and src.native[kind] != want:
                raise _OpFailed(f"source {src.loc} changed since the plan was made: "
                                f"{kind} {want} -> {src.native[kind]}")
        self.log("precheck", op, source=src.loc, size=src.size, hashes=src.native)
        return src

    def _dst(self, op: Op) -> tuple[int, str]:
        if op.dst_remote_id is None or op.dst_path is None:
            raise _OpFailed(f"{op.op} has no destination")
        return op.dst_remote_id, op.dst_path

    def _copy(self, op: Op, src: _Side) -> str:
        dst_rid, dst_path = self._dst(op)
        write = self.writable(dst_rid, "destination")
        existing = self.probe("destination", dst_rid, dst_path)
        if existing is not None:
            proof = self.prove(src, existing, op)
            self.log("verify", op, what="existing destination", a=src.loc, b=existing.loc,
                     verdict=proof.verdict, how=proof.how, detail=proof.detail)
            if proof.verdict == "same":
                return "skipped"
            raise _OpFailed(f"destination {existing.loc} already exists and is not provably identical "
                            f"to the source ({proof.verdict}); it was left as it is")
        self.log("intent", op, kind="copy", source=src.loc, destination=f"{self.name(dst_rid)}:{dst_path}")
        self.rclone.copyto(src.rc.read, src.path, write, dst_path)
        out = self.probe("destination", dst_rid, dst_path)
        if out is None:
            raise _OpFailed(f"destination {self.name(dst_rid)}:{dst_path} is missing after the copy")
        self.log("copy", op, source=src.loc, destination=out.loc, bytes=out.size)
        self.require_same(op, src, out, "copy verification")
        return "done"

    def _move(self, op: Op, src: _Side) -> str:
        dst_rid, dst_path = self._dst(op)
        if dst_rid != op.src_remote_id:
            raise _OpFailed("a move must stay within one remote (use copy + delete across remotes)")
        write = self.writable(dst_rid, "move")
        existing = self.probe("destination", dst_rid, dst_path)
        if existing is not None:
            return self._move_onto_existing(op, src, existing, write)
        # Once moved, the source is gone: its hashes must be known now so the destination can be checked against them.
        if not self._know(src):
            self._fingerprint(src, op)
        self.log("intent", op, kind="move", source=src.loc, destination=f"{src.name}:{dst_path}")
        self.rclone.moveto(write, src.path, write, dst_path)
        out = self.probe("destination", dst_rid, dst_path)
        if out is None:
            raise _OpFailed(f"destination {src.name}:{dst_path} is missing after the move")
        self.log("move", op, source=src.loc, destination=out.loc, bytes=out.size)
        self.verify_renamed(op, src, out, "move verification")
        if self.probe("source", src.rid, src.path) is not None:
            raise _OpFailed(f"source {src.loc} is still there after the move")
        return "done"

    def _move_onto_existing(self, op: Op, src: _Side, existing: _Side, write: str) -> str:
        """The destination is taken. If it provably holds the same bytes the move has nothing left to do
        but remove the source, which goes to quarantine like a deleted duplicate; otherwise nothing changes."""
        if existing.path == src.path:
            self.log("verify", op, what="move onto itself", a=src.loc, b=existing.loc, verdict="same", how="same path")
            return "skipped"
        if self.may_be_same_object(src.rid, existing.path, src.path):
            raise _OpFailed(f"destination {existing.loc} is the source {src.loc} under a name that differs "
                            "only in case or Unicode form; nothing was changed")
        self._require_live("destination", existing.rid, existing.path)
        proof = self.prove(src, existing, op)
        self.log("verify", op, what="existing destination", a=src.loc, b=existing.loc,
                 verdict=proof.verdict, how=proof.how, detail=proof.detail)
        if proof.verdict != "same":
            raise _OpFailed(f"destination {existing.loc} already exists and is not provably identical "
                            f"to the source ({proof.verdict}); nothing was overwritten")
        self._quarantine(op, src, write, why="the destination already holds identical content")
        return "done"

    def _require_live(self, role: str, rid: int, path: str) -> None:
        """Fail unless this location can count as a live copy, i.e. is not inside the quarantine directory."""
        root = _fold(_quarantine_root(self.config), True)     # like the simulator: any case, on any disk
        folded = _fold(path, True)
        if folded == root or folded.startswith(root + "/"):
            raise _OpFailed(f"{role} {self.name(rid)}:{path} is inside the quarantine directory, "
                            "which is not a live copy")

    def _witness(self, rid: int, path: str, victim: _Side) -> _Side:
        """Probe the surviving copy that vouches for `victim`: another file, outside quarantine, still there."""
        if rid == victim.rid and self.may_be_same_object(rid, path, victim.path):
            raise _OpFailed(f"the witness is the file being deleted ({victim.loc})")
        self._require_live("witness", rid, path)
        wit = self.probe("witness", rid, path)
        if wit is None:
            raise _OpFailed(f"witness {self.name(rid)}:{path} does not exist")
        return wit

    def _quarantine(self, op: Op, src: _Side, write: str, **why) -> None:
        """Move the source into this plan's quarantine on its own cloud and check the result."""
        qpath = quarantine_path(self.config, self.plan_id, src.path)
        if not _inside_quarantine(self.config, self.plan_id, qpath):
            raise _OpFailed(f"refusing quarantine path {qpath!r}")
        if self.probe("quarantine target", src.rid, qpath) is not None:
            raise _OpFailed(f"quarantine target {src.name}:{qpath} is already occupied")
        self.log("intent", op, kind="quarantine", remote=src.name, **{"from": src.path, "to": qpath}, **why)
        self.rclone.moveto(write, src.path, write, qpath)
        held = self.probe("quarantined copy", src.rid, qpath)
        if held is None:
            raise _OpFailed(f"quarantined copy {src.name}:{qpath} is missing after the move")
        self.same_object(src, held, "quarantined copy")
        if self.probe("source", src.rid, src.path) is not None:
            raise _OpFailed(f"source {src.loc} is still there after the move to quarantine")
        self.log("quarantine", op, remote=src.name, **{"from": src.path, "to": qpath})

    def _delete(self, op: Op, src: _Side) -> str:
        write = self.writable(src.rid, "delete")
        if op.witness_remote_id is None or not op.witness_path:
            raise _OpFailed("delete without a witness")
        wit = self._witness(op.witness_remote_id, op.witness_path, src)
        self.require_same(op, src, wit, "witness check")
        self._quarantine(op, src, write)
        return "done"

    # --------------------------------------------------------- restore / purge

    def restore(self, op: Op) -> None:
        rid = op.src_remote_id
        name = self.name(rid)
        if problem := _path_problem(op.src_path):
            raise _OpFailed(f"original path {op.src_path!r} {problem}", "BAD_PATH", path=op.src_path)
        qpath = quarantine_path(self.config, self.plan_id, op.src_path)
        if not _inside_quarantine(self.config, self.plan_id, qpath):
            raise _OpFailed(f"refusing quarantine path {qpath!r}", "BAD_PATH", path=qpath)
        write = self.writable(rid, "restore")
        held = self.probe("quarantined copy", rid, qpath)
        if held is None:
            raise _OpFailed(f"quarantined copy {name}:{qpath} is gone (purged or moved)",
                            "QUARANTINE_GONE", remote=name, path=op.src_path)
        taken = self.probe("original location", rid, op.src_path)
        if taken is not None:
            raise _OpFailed(f"original path {taken.loc} is occupied; nothing was overwritten",
                            "ORIGINAL_TAKEN", remote=name, path=op.src_path)
        self.log("intent", op, kind="restore", remote=name, **{"from": qpath, "to": op.src_path})
        self.rclone.moveto(write, qpath, write, op.src_path)
        back = self.probe("restored file", rid, op.src_path)
        if back is None:
            raise _OpFailed(f"{name}:{op.src_path} is missing after the restore",
                            "VERIFY_FAILED", remote=name, path=op.src_path)
        try:
            self.same_object(held, back, "restored file")
        except _OpFailed as e:
            raise _OpFailed(str(e), "VERIFY_FAILED", remote=name, path=op.src_path) from e
        if self.probe("quarantined copy", rid, qpath) is not None:
            raise _OpFailed(f"{name}:{qpath} is still in quarantine after the restore",
                            "VERIFY_FAILED", remote=name, path=op.src_path)
        self.log("restore", op, status="restored", remote=name, **{"from": qpath, "to": op.src_path})

    def purge(self, op: Op) -> bool:
        """Delete one quarantined file. False when it was already gone."""
        rid = op.src_remote_id
        name = self.name(rid)
        if problem := _path_problem(op.src_path):
            raise _OpFailed(f"original path {op.src_path!r} {problem}", "BAD_PATH", path=op.src_path)
        qpath = quarantine_path(self.config, self.plan_id, op.src_path)
        if not _inside_quarantine(self.config, self.plan_id, qpath):
            raise _OpFailed(f"refusing to purge {name}:{qpath}: not inside {_quarantine_root(self.config)}"
                            f"/{self.plan_id}/", "BAD_PATH", path=qpath)
        write = self.writable(rid, "purge")
        held = self.probe("quarantined copy", rid, qpath)
        if held is None:
            return False
        # The quarantined copy may by now be the only one: whatever vouched for it must still be there.
        if op.op == "delete":
            wit_rid, wit_path = op.witness_remote_id, op.witness_path
        else:  # a move that met identical content: the destination vouches
            wit_rid, wit_path = op.dst_remote_id, op.dst_path
        if wit_rid is None or not wit_path:
            raise _OpFailed(f"{name}:{qpath} is kept: the op names no surviving copy",
                            "NO_WITNESS_RECORD", remote=name, path=op.src_path)
        if problem := _path_problem(wit_path):
            raise _OpFailed(f"{name}:{qpath} is kept: witness path {wit_path!r} {problem}", "BAD_PATH", path=wit_path)
        try:
            self.require_same(op, held, self._witness(wit_rid, wit_path, held), "witness re-check before purge")
        except _OpFailed as e:
            raise _OpFailed(f"{name}:{qpath} is kept: {e}", "WITNESS_UNPROVEN", remote=name, path=op.src_path,
                            witness=f"{self.name(wit_rid)}:{wit_path}") from e
        self.log("intent", op, kind="purge", remote=name, path=qpath)
        self.rclone.deletefile(write, qpath)
        if self.probe("quarantined copy", rid, qpath) is not None:
            raise _OpFailed(f"{name}:{qpath} is still there after the purge",
                            "VERIFY_FAILED", remote=name, path=op.src_path)
        self.log("purge", op, status="purged", remote=name, path=qpath)
        return True


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _describe(run: _Run, op: Op) -> str:
    return f"op {op.seq} ({op.op} {run.name(op.src_remote_id)}:{op.src_path})"


def _failure_text(exc: Exception) -> str:
    if isinstance(exc, _OpFailed):
        return str(exc)
    if isinstance(exc, RcloneError):
        return f"rclone failed: {exc}"
    return f"unexpected {type(exc).__name__}: {exc}"


def execute_plan(conn: sqlite3.Connection, rclone: Rclone, config: Config, plan_id: int, *,
                 tmpdir: str | Path | None = None,
                 progress: Callable[[int, int, Op], None] | None = None) -> ExecResult:
    """Run an approved plan op by op, stopping at the first failure.

    A 'failed' plan resumes: ops already done (or skipped) are not repeated.
    `progress(done, total, op)` is called after every op executed in this run;
    `done` counts every finished op of the plan, including earlier runs.
    """
    _require_enabled(config)
    plan = plans.get_plan(conn, plan_id)
    if plan.status not in ("approved", "failed"):
        raise ExecutionError(f"plan {plan_id} is {plan.status}; only an approved plan can be executed")
    if not plan.digest:
        raise ExecutionError(f"plan {plan_id} has no simulation digest")
    plans.require_current(conn, plan_id)
    if not plans.claim_for_execution(conn, plan_id):
        raise ExecutionError(f"plan {plan_id} is already being executed")
    try:
        return _run_claimed(conn, rclone, config, plan, tmpdir, progress)
    finally:
        _unclaim(conn, plan_id)


def _unclaim(conn: sqlite3.Connection, plan_id: int) -> None:
    """End of a run: the plan is never left 'executing' by this process, and its lock goes away."""
    with contextlib.suppress(sqlite3.Error):
        if plans.get_plan(conn, plan_id, with_ops=False).status == "executing":
            plans.set_status(conn, plan_id, "failed")
    with contextlib.suppress(sqlite3.Error):
        plans.release_execution_lock(conn, plan_id)


def _run_claimed(conn: sqlite3.Connection, rclone: Rclone, config: Config, plan: plans.Plan,
                 tmpdir: str | Path | None, progress: Callable[[int, int, Op], None] | None) -> ExecResult:
    """Execute a plan this process has claimed ('executing' + lock); always leaves it done or failed."""
    plan_id = plan.id
    ops = plan.ops
    by_seq = {o.seq: o for o in ops}
    total = len(ops)
    counts = {"done": sum(o.status == "done" for o in ops), "skipped": sum(o.status == "skipped" for o in ops)}

    own_tmp = tempfile.TemporaryDirectory(prefix="drivemerge-exec-") if tmpdir is None else None
    tmp = Path(own_tmp.name if own_tmp else tmpdir)
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        run = _Run(conn, rclone, config, plan_id, tmp)
        run.log("execute", None, ops=total, already_finished=counts["done"] + counts["skipped"], digest=plan.digest)
        try:
            for op in ops:
                if op.status in ("done", "skipped"):
                    continue
                failure = None
                try:
                    outcome = run.execute(op, by_seq)
                except Exception as e:  # any surprise stops the plan: fail-closed
                    failure = _failure_text(e)
                if failure is not None:
                    plans.set_op_status(conn, op.id, "failed", failure)
                    op.status, op.error = "failed", failure
                    run.log("error", op, error=failure)
                    plans.set_status(conn, plan_id, "failed")
                    if progress:
                        progress(counts["done"] + counts["skipped"], total, op)
                    return ExecResult(plan_id, "failed", counts["done"], 1, counts["skipped"],
                                      f"{_describe(run, op)}: {failure}")
                plans.set_op_status(conn, op.id, outcome)
                op.status, op.error = outcome, None
                counts[outcome] += 1
                if progress:
                    progress(counts["done"] + counts["skipped"], total, op)
            plans.set_status(conn, plan_id, "done")
            run.log("execute_done", None, done=counts["done"], skipped=counts["skipped"])
            return ExecResult(plan_id, "done", counts["done"], 0, counts["skipped"])
        except BaseException as e:
            # Interrupted (Ctrl-C) or a failing progress callback: never leave the plan 'executing'.
            plans.set_status(conn, plan_id, "failed")
            run.log("error", None, error=f"execution aborted: {type(e).__name__}: {e}")
            raise
    finally:
        if own_tmp:
            own_tmp.cleanup()


def _quarantined_ops(conn: sqlite3.Connection, plan: plans.Plan) -> list[Op]:
    """Finished ops of the plan whose source sits in its quarantine: deletes, and moves that met identical content."""
    parked = {r[0] for r in conn.execute(
        "SELECT DISTINCT op_id FROM journal WHERE plan_id = ? AND action = 'quarantine' AND op_id IS NOT NULL",
        (plan.id,))}
    return [o for o in plan.ops if o.status == "done" and (o.op == "delete" or (o.op == "move" and o.id in parked))]


def _record_failure(result: ExecResult, run: _Run, op: Op, exc: Exception) -> str:
    """Count a failed restore / purge item; keeps at most MAX_FAILURES details. Returns the English text."""
    text = _failure_text(exc)
    result.failed += 1
    result.error = result.error or f"{_describe(run, op)}: {text}"
    if len(result.failures) < MAX_FAILURES:
        coded = isinstance(exc, _OpFailed)
        result.failures.append({
            "op_id": op.id, "seq": op.seq, "remote": run.name(op.src_remote_id), "path": op.src_path,
            "code": exc.code if coded else "OP_FAILED", "params": dict(exc.params) if coded else {},
            "error": text})
    return text


def _selected(ops: list[Op], op_ids: Collection[int] | None) -> list[Op]:
    """The quarantined ops a caller asked for; an id that is not one of them is ignored, never acted on."""
    if op_ids is None:
        return ops
    wanted = set(op_ids)
    return [o for o in ops if o.id in wanted]


def restore_plan(conn: sqlite3.Connection, rclone: Rclone, config: Config, plan_id: int, *,
                 op_ids: Collection[int] | None = None,
                 progress: Callable[[int, int, int], None] | None = None) -> ExecResult:
    """Move every quarantined file of this plan back to its original path (newest delete first).

    An original path that is taken again is a failure for that file only: it
    stays in quarantine and the rest are still restored. A restored op
    is marked 'skipped' so a second restore, a purge or a resume ignores it.
    `op_ids` restricts the run to those quarantined ops (anything else is ignored);
    `progress(done, failed, total)` is called after each item.
    """
    _require_enabled(config)
    plan = plans.get_plan(conn, plan_id)
    if plan.status == "executing":
        raise _plan_executing(plan_id)
    result = ExecResult(plan_id, "done")
    run = _Run(conn, rclone, config, plan_id)
    todo = sorted(_selected(_quarantined_ops(conn, plan), op_ids), key=lambda o: o.seq, reverse=True)
    for op in todo:
        failure = None
        try:
            run.restore(op)
        except Exception as e:
            failure = e
        if failure is None:
            plans.set_op_status(conn, op.id, "skipped", "restored from quarantine")
            result.done += 1
        else:
            run.log("restore", op, status="failed", error=_record_failure(result, run, op, failure))
        if progress:
            progress(result.done, result.failed, len(todo))
    result.status = "done" if result.failed == 0 else "partial"
    return result


def purge_quarantine(conn: sqlite3.Connection, rclone: Rclone, config: Config, plan_id: int, *,
                     older_than_days: int = 30, tmpdir: str | Path | None = None,
                     op_ids: Collection[int] | None = None,
                     progress: Callable[[int, int, int], None] | None = None) -> ExecResult:
    """Permanently delete quarantined files of this plan that were quarantined long enough ago.

    Only files of ops that quarantined their source ('done' deletes, and moves onto identical
    content) before the cutoff, and only paths below <quarantine_dir>/<plan id>/. `skipped` counts
    files left alone (too recent, or already gone). A file is purged only if what vouched for it
    (the delete's witness, the move's destination) still exists live and is proven identical now,
    since the quarantined copy may have become the last one; otherwise it fails and stays.
    `op_ids` restricts the run to those quarantined ops (anything else is ignored); `progress(done, failed,
    total)` is called after each item old enough to be attempted, `done` counting those purged or already gone.
    """
    _require_enabled(config)
    if older_than_days < 0:
        raise ExecutionError("older_than_days must not be negative")
    plan = plans.get_plan(conn, plan_id)
    if plan.status == "executing":
        raise _plan_executing(plan_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime(_TS_FORMAT)
    executed = {r[0]: r[1] for r in conn.execute(
        "SELECT id, executed_at FROM plan_ops WHERE plan_id = ? AND status = 'done'", (plan_id,))}
    result = ExecResult(plan_id, "done")
    chosen = _selected(_quarantined_ops(conn, plan), op_ids)
    todo = []
    for op in chosen:
        if executed[op.id] is None or executed[op.id] >= cutoff:
            result.skipped += 1
        else:
            todo.append(op)
    with contextlib.ExitStack() as stack:
        # Proving a witness identical may mean downloading something, and that needs scratch space.
        tmp = Path(tmpdir) if tmpdir is not None else Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix="drivemerge-purge-")))
        tmp.mkdir(parents=True, exist_ok=True)
        run = _Run(conn, rclone, config, plan_id, tmp)
        settled = 0
        for op in todo:
            failure = None
            try:
                purged = run.purge(op)
            except Exception as e:
                failure = e
            if failure is not None:
                run.log("purge", op, status="failed", error=_record_failure(result, run, op, failure))
            else:
                settled += 1
                if purged:
                    result.done += 1
                else:
                    result.skipped += 1
            if progress:
                progress(settled, result.failed, len(todo))
    result.status = "done" if result.failed == 0 else "partial"
    return result
