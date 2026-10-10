"""Executor tests against REAL rclone on local directories.

Four local directories stand in for the four clouds by restricting the hash
types each one reports (gd: md5/sha1/sha256, dbx: Dropbox hash, od: QuickXor,
ic: none), plus a read-only remote. Plans are forced to 'approved' with a
fabricated passing simulation unless a test says otherwise: the executor must
hold even when the simulator and the catalog were wrong or stale.

The recurring assertion is that a refused op leaves every cloud exactly as it
was: full listings (files, bytes, directories) are compared before and after.
"""

from __future__ import annotations

import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from drivemerge import builders, db, executor, hashing, identity, plans, simulate
from drivemerge.config import parse_config, sync_remotes
from drivemerge.executor import ExecResult, ExecutionDisabled, ExecutionError
from drivemerge.models import Op, SimulationReport
from drivemerge.rclone import Rclone, RcloneError
from drivemerge.scanner import scan_remote

QDIR = ".drive-merge-quarantine"
CLOUDS = {"gd": ["md5", "sha1", "sha256"], "dbx": ["dropbox"], "od": ["quickxor"], "ic": [], "ro": ["md5"],
          "ci": ["dropbox"]}
READ_ONLY = {"ro"}
PROVIDER = {"ci": "dropbox"}  # a case-insensitive cloud (the directory underneath is still case-sensitive)


def blob(n: int, seed: int = 0) -> bytes:
    return random.Random(seed).randbytes(n)


class Spy(Rclone):
    """Real rclone that remembers every mutating call, can make downloads or stats fail, and can
    run a hook just before a copy or move to play a concurrent writer."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.download_error: Exception | None = None
        self.stat_error: Exception | None = None
        self.before: dict[str, object] = {}

    def stat(self, remote, path, *, hash_types=()):
        if self.stat_error is not None:
            raise self.stat_error
        return super().stat(remote, path, hash_types=hash_types)

    def copyto(self, src_remote, src_path, dst_remote, dst_path):
        self.calls.append(("copyto", src_path, dst_path))
        if "copyto" in self.before:
            self.before["copyto"](src_path, dst_path)
        super().copyto(src_remote, src_path, dst_remote, dst_path)

    def moveto(self, src_remote, src_path, dst_remote, dst_path):
        self.calls.append(("moveto", src_path, dst_path))
        if "moveto" in self.before:
            self.before["moveto"](src_path, dst_path)
        super().moveto(src_remote, src_path, dst_remote, dst_path)

    def deletefile(self, remote, path):
        self.calls.append(("deletefile", path))
        super().deletefile(remote, path)

    def download(self, remote, path, local_path):
        self.calls.append(("download", path))
        if self.download_error is not None:
            raise self.download_error
        super().download(remote, path, local_path)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c[0] == kind)

    def paths(self, kind: str) -> list[tuple]:
        return [c[1:] for c in self.calls if c[0] == kind]

    def mutations(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("copyto", "moveto", "deletefile")]


@dataclass
class Env:
    tmp: Path
    roots: dict[str, Path]
    config: object
    conn: object
    spy: Spy

    # ------------------------------------------------------------ the clouds

    def put(self, remote: str, rel: str, data: bytes | str) -> Path:
        """Write a file; rewriting one also moves its mtime, as a real edit would."""
        p = self.roots[remote] / rel
        old = p.stat().st_mtime_ns if p.exists() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        if old is not None:
            os.utime(p, ns=(old + 10**10, old + 10**10))
        return p

    def read(self, remote: str, rel: str) -> bytes:
        return (self.roots[remote] / rel).read_bytes()

    def exists(self, remote: str, rel: str) -> bool:
        return (self.roots[remote] / rel).exists()

    def tree(self) -> dict[tuple[str, str], bytes | None]:
        """Every file (bytes) and directory (None) on every cloud."""
        out: dict[tuple[str, str], bytes | None] = {}
        for name, root in self.roots.items():
            for p in sorted(root.rglob("*")):
                out[(name, str(p.relative_to(root)))] = p.read_bytes() if p.is_file() else None
        return out

    def files(self, *, quarantine: bool = False) -> dict[tuple[str, str], bytes]:
        return {k: v for k, v in self.tree().items()
                if v is not None and k[1].startswith(QDIR) == quarantine}

    # ------------------------------------------------------------ the catalog

    def rid(self, remote: str) -> int:
        return db.remote_id(self.conn, remote)

    def scan(self, *remotes: str) -> None:
        for name in remotes or CLOUDS:
            assert scan_remote(self.conn, self.spy, self.config.remote(name)).status == "complete"

    def entry(self, remote: str, path: str):
        return self.conn.execute(
            "SELECT * FROM entries WHERE remote_id = ? AND path = ? AND present = 1 AND is_dir = 0",
            (self.rid(remote), path)).fetchone()

    # ------------------------------------------------------------------ ops

    def _op(self, kind: str, remote: str, path: str, *, dst=None, witness=None, **kw) -> Op:
        extra = dict(kw)
        if dst:
            extra.update(dst_remote_id=self.rid(dst[0]), dst_path=dst[1])
        if witness:
            extra.update(witness_remote_id=self.rid(witness[0]), witness_path=witness[1])
        row = self.entry(remote, path)
        if row is None:
            return Op(kind, self.rid(remote), path, **extra)
        return builders.op_from_entry(row, kind, **extra)

    def copy(self, remote, path, dst_remote, dst_path, **kw) -> Op:
        return self._op("copy", remote, path, dst=(dst_remote, dst_path), **kw)

    def move(self, remote, path, dst_remote, dst_path, **kw) -> Op:
        return self._op("move", remote, path, dst=(dst_remote, dst_path), **kw)

    def delete(self, remote, path, witness=None, **kw) -> Op:
        return self._op("delete", remote, path, witness=witness, **kw)

    def approve(self, pid: int) -> None:
        snap = db.snapshot(self.conn)
        digest = plans.compute_digest(pid, plans.load_ops(self.conn, pid), snap)
        plans.record_simulation(self.conn, pid, SimulationReport(plan_id=pid, digest=digest, ok=True), snap)
        plans.approve_plan(self.conn, pid, plans.short_code(digest), approved_by="test")

    def plan(self, ops: list[Op], *, approve: bool = True) -> int:
        pid = plans.create_plan(self.conn, "t", "manual", ops, created_by="test")
        if approve:
            self.approve(pid)
        return pid

    def run(self, pid: int, **kw) -> ExecResult:
        return executor.execute_plan(self.conn, self.spy, self.config, pid, **kw)

    def restore(self, pid: int) -> ExecResult:
        return executor.restore_plan(self.conn, self.spy, self.config, pid)

    def purge(self, pid: int, **kw) -> ExecResult:
        return executor.purge_quarantine(self.conn, self.spy, self.config, pid, **kw)

    def plan_status(self, pid: int) -> str:
        return plans.get_plan(self.conn, pid, with_ops=False).status

    def op_states(self, pid: int) -> list[tuple[str, str | None]]:
        return [(o.status, o.error) for o in plans.load_ops(self.conn, pid)]

    def journal(self, pid: int, action: str | None = None) -> list[dict]:
        q = "SELECT action, op_id, detail FROM journal WHERE plan_id = ?" + (" AND action = ?" if action else "")
        rows = self.conn.execute(q + " ORDER BY id", (pid, action) if action else (pid,)).fetchall()
        return [{"action": r[0], "op_id": r[1], "detail": json.loads(r[2]) if r[2] else None} for r in rows]

    def qpath(self, pid: int, path: str) -> str:
        return f"{QDIR}/{pid}/{path}"

    def bridges(self) -> list:
        return self.conn.execute("SELECT * FROM hash_bridges ORDER BY id").fetchall()


@pytest.fixture
def env(tmp_path):
    if shutil.which("rclone") is None:
        pytest.skip("rclone not on PATH")
    roots = {n: tmp_path / "clouds" / n for n in CLOUDS}
    for r in roots.values():
        r.mkdir(parents=True)
    config = parse_config({
        "safety": {"execution_enabled": True},
        "remotes": [
            {"name": n, "provider": PROVIDER.get(n, "local"), "read": str(roots[n]), "hash_types": h,
             "write": None if n in READ_ONLY else str(roots[n])}
            for n, h in CLOUDS.items()
        ],
    })
    conn = db.connect(":memory:")
    sync_remotes(conn, config)
    yield Env(tmp_path, roots, config, conn, Spy())
    conn.close()


def assert_refused(env: Env, pid: int, before: dict, *, mutations: bool = False) -> ExecResult:
    """Run the plan; it must fail, leave every cloud as it was and (normally) not even try to write."""
    res = env.run(pid)
    assert res.status == "failed" and res.failed == 1 and res.error, res
    assert env.tree() == before
    if not mutations:
        assert env.spy.mutations() == []
    assert env.plan_status(pid) == "failed"
    return res


# --------------------------------------------------------------------------- #
# Pure pieces
# --------------------------------------------------------------------------- #

def test_quarantine_path_layout():
    cfg = parse_config({})
    assert executor.quarantine_path(cfg, 7, "a/b.txt") == ".drive-merge-quarantine/7/a/b.txt"
    cfg = parse_config({"safety": {"quarantine_dir": "Trash/dm/"}})
    assert executor.quarantine_path(cfg, 3, "x") == "Trash/dm/3/x"


@pytest.mark.parametrize("bad", ["", "/", "..", "a/../b", "/abs"])
def test_unsafe_quarantine_dir_is_refused(bad):
    with pytest.raises(ExecutionError):
        executor.quarantine_path(parse_config({"safety": {"quarantine_dir": bad}}), 1, "x")


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return 0, "", ""


def test_deletefile_builds_one_file_command():
    r = FakeRunner()
    Rclone(runner=r).deletefile("gd:", ".q/1/a.txt")
    Rclone(runner=r).deletefile("-odd", "x.txt")  # an operand that looks like a flag gets '--'
    assert r.calls == [["rclone", "deletefile", "gd:.q/1/a.txt"],
                       ["rclone", "deletefile", "--", "-odd/x.txt"]]


@pytest.mark.parametrize("bad", ["", "/a", "a/../b", "..", "a//b", "./a", "a\x00b"])
def test_deletefile_rejects_paths_that_could_leave_the_remote(bad):
    r = FakeRunner()
    with pytest.raises(ValueError):
        Rclone(runner=r).deletefile("gd:", bad)
    assert r.calls == []


def test_deletefile_failure_raises():
    with pytest.raises(RcloneError, match="deletefile failed"):
        Rclone(runner=lambda argv: (1, "", "ERROR : boom")).deletefile("gd:", "a")


def test_real_deletefile_removes_exactly_one_file(tmp_path):
    if shutil.which("rclone") is None:
        pytest.skip("rclone not on PATH")
    root = tmp_path / "r"
    (root / "d").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    (root / "d" / "c.txt").write_text("c")
    rc = Rclone()
    rc.deletefile(str(root), "a.txt")
    assert not (root / "a.txt").exists() and (root / "b.txt").exists() and (root / "d" / "c.txt").exists()
    with pytest.raises(RcloneError):  # a directory is refused, with its content
        rc.deletefile(str(root), "d")
    with pytest.raises(RcloneError):  # a missing file is an error, not a silent success
        rc.deletefile(str(root), "nope.txt")
    assert sorted(p.name for p in root.rglob("*")) == ["b.txt", "c.txt", "d"]


# --------------------------------------------------------------------------- #
# Gates before anything happens
# --------------------------------------------------------------------------- #

def test_disabled_execution_refuses_everything_before_touching_anything(env):
    env.put("gd", "a.bin", blob(50))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    env.config.safety.execution_enabled = False
    before = env.tree()
    for call in (lambda: env.run(pid), lambda: env.restore(pid), lambda: env.purge(pid)):
        with pytest.raises(ExecutionDisabled):
            call()
    assert env.spy.calls == [] and env.tree() == before
    assert env.plan_status(pid) == "approved"
    assert [j["action"] for j in env.journal(pid)] == ["approve"]


@pytest.mark.parametrize("status", ["draft", "cancelled", "done", "executing"])
def test_only_an_approved_or_failed_plan_runs(env, status):
    env.put("gd", "a.bin", blob(50))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")], approve=False)
    plans.set_status(env.conn, pid, status)
    with pytest.raises(ExecutionError):
        env.run(pid)
    assert env.spy.calls == [] and not env.exists("gd", "b.bin")


def test_a_simulated_but_unapproved_plan_does_not_run(env):
    env.put("gd", "a.bin", blob(50))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")], approve=False)
    snap = db.snapshot(env.conn)
    digest = plans.compute_digest(pid, plans.load_ops(env.conn, pid), snap)
    plans.record_simulation(env.conn, pid, SimulationReport(plan_id=pid, digest=digest, ok=True), snap)
    with pytest.raises(ExecutionError):
        env.run(pid)
    assert not env.exists("gd", "b.bin")


def test_unknown_plan_raises(env):
    with pytest.raises(plans.PlanNotFound):
        env.run(999)


def test_a_rescan_after_approval_blocks_execution(env):
    env.put("gd", "a.bin", blob(50))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    env.scan("gd")  # a newer complete scan changes the catalog snapshot
    before = env.tree()
    with pytest.raises(plans.StaleSimulation):
        env.run(pid)
    assert env.plan_status(pid) == "approved" and env.spy.calls == [] and env.tree() == before


def test_ops_edited_after_approval_block_execution(env):
    env.put("gd", "a.bin", blob(50))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    env.conn.execute("UPDATE plan_ops SET dst_path = 'elsewhere.bin' WHERE plan_id = ?", (pid,))
    with pytest.raises(plans.StaleSimulation):
        env.run(pid)
    assert env.spy.calls == [] and not env.exists("gd", "elsewhere.bin")


def test_a_plan_cannot_be_started_twice(env):
    env.put("gd", "a.bin", blob(50))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    assert env.run(pid).status == "done"
    with pytest.raises(ExecutionError):
        env.run(pid)  # now 'done'
    assert env.spy.count("copyto") == 1


# --------------------------------------------------------------------------- #
# copy
# --------------------------------------------------------------------------- #

def test_copy_within_one_remote_is_verified_without_downloading(env):
    env.put("gd", "a/x.bin", blob(300))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a/x.bin", "gd", "b/deep/y.bin")])
    res = env.run(pid)
    assert res == ExecResult(pid, "done", done=1)
    assert env.read("gd", "b/deep/y.bin") == blob(300) and env.read("gd", "a/x.bin") == blob(300)
    assert env.spy.count("download") == 0 and env.spy.count("copyto") == 1
    assert env.plan_status(pid) == "done" and env.op_states(pid) == [("done", None)]
    actions = [j["action"] for j in env.journal(pid)]
    assert actions[0] == "approve" and actions[-1] == "execute_done"
    assert {"execute", "precheck", "intent", "copy", "verify"} <= set(actions)
    assert all(j["detail"] is None or isinstance(j["detail"], dict) for j in env.journal(pid))


def test_cross_domain_copy_is_verified_by_download_and_records_a_bridge(env):
    data = blob(5000, 1)
    env.put("gd", "Photos/p.jpg", data)
    env.scan("gd")
    pid = env.plan([env.copy("gd", "Photos/p.jpg", "dbx", "Backup/p.jpg")])
    assert env.run(pid).status == "done"
    assert env.read("dbx", "Backup/p.jpg") == data
    # md5/sha1/sha256 vs the Dropbox hash share nothing: one download proves it.
    assert env.spy.paths("download") == [("Backup/p.jpg",)]
    [bridge] = env.bridges()
    full = hashing.hash_file(env.roots["dbx"] / "Backup/p.jpg")
    assert bridge["size"] == len(data)
    assert {t: bridge[t] for t in hashing.HASH_TYPES} == {t: full[t] for t in hashing.HASH_TYPES}
    # ... and the bridge does its job: after scanning the destination the two files are one exact group.
    env.scan("dbx")
    identity.rebuild_groups(env.conn)
    assert identity.same_content(env.conn, env.entry("gd", "Photos/p.jpg")["id"],
                                 env.entry("dbx", "Backup/p.jpg")["id"])


def test_the_bridge_from_a_verified_copy_proves_the_follow_up_delete_without_another_download(env):
    data = blob(4000, 2)
    env.put("gd", "p.jpg", data)
    env.scan("gd")
    assert env.run(env.plan([env.copy("gd", "p.jpg", "dbx", "p.jpg")])).status == "done"
    assert env.spy.count("download") == 1
    pid = env.plan([env.delete("gd", "p.jpg", witness=("dbx", "p.jpg"))])
    res = env.run(pid)
    assert res.status == "done", res
    assert env.spy.count("download") == 1, "the recorded bridge must make a second download unnecessary"
    assert env.read("gd", env.qpath(pid, "p.jpg")) == data and not env.exists("gd", "p.jpg")
    assert env.read("dbx", "p.jpg") == data


def test_a_file_without_native_hashes_is_downloaded_and_bridged_to_its_catalog_entry(env):
    data = blob(1500, 3)
    env.put("ic", "Notes/n.txt", data)
    env.scan("ic")
    entry = env.entry("ic", "Notes/n.txt")
    pid = env.plan([env.copy("ic", "Notes/n.txt", "gd", "Archive/n.txt")])
    assert env.run(pid).status == "done"
    assert env.spy.paths("download") == [("Notes/n.txt",)], "only the hashless side needs downloading"
    [bridge] = env.bridges()
    assert (bridge["source_entry_id"], bridge["source_mtime"]) == (entry["id"], entry["mtime"])
    # The iCloud-style source is now exact via its bridge: deleting it needs no further download.
    pid = env.plan([env.delete("ic", "Notes/n.txt", witness=("gd", "Archive/n.txt"))])
    assert env.run(pid).status == "done"
    assert env.spy.count("download") == 1
    assert env.read("ic", env.qpath(pid, "Notes/n.txt")) == data


def test_copy_onto_identical_content_is_skipped_and_counts_as_satisfied(env):
    data = blob(200, 4)
    env.put("gd", "a.bin", data)
    env.put("gd", "b.bin", data)
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    res = env.run(pid)
    assert res == ExecResult(pid, "done", done=0, skipped=1)
    assert env.spy.count("copyto") == 0 and env.op_states(pid) == [("skipped", None)]


def test_a_skipped_copy_still_lets_its_dependent_delete_run(env):
    data = blob(200, 5)
    env.put("gd", "a.bin", data)
    env.put("dbx", "a.bin", data)  # the copy's destination already holds the content
    env.scan("gd", "dbx")
    pid = env.plan([env.copy("gd", "a.bin", "dbx", "a.bin"),
                    env.delete("gd", "a.bin", witness=("dbx", "a.bin"), depends_on=1)])
    res = env.run(pid)
    assert res == ExecResult(pid, "done", done=1, skipped=1)
    assert env.read("gd", env.qpath(pid, "a.bin")) == data


def test_copy_never_overwrites_a_different_file_even_of_equal_size(env):
    env.put("gd", "a.bin", blob(100, 1))
    env.put("gd", "b.bin", blob(100, 2))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    res = assert_refused(env, pid, env.tree())
    assert "already exists" in res.error and env.read("gd", "b.bin") == blob(100, 2)


@pytest.mark.parametrize("dst_remote", ["dbx", "ic"])
def test_copy_onto_a_different_file_in_another_hash_domain_is_refused_after_a_download(env, dst_remote):
    env.put("gd", "a.bin", blob(100, 1))
    env.put(dst_remote, "b.bin", blob(100, 2))  # same size, other bytes
    env.scan("gd", dst_remote)
    pid = env.plan([env.copy("gd", "a.bin", dst_remote, "b.bin")])
    assert_refused(env, pid, env.tree())
    assert env.spy.count("download") == 1 and env.read(dst_remote, "b.bin") == blob(100, 2)


def test_copy_onto_identical_content_in_a_hashless_cloud_is_proven_by_download(env):
    env.put("gd", "a.bin", blob(100, 1))
    env.put("ic", "b.bin", blob(100, 1))
    env.scan("gd", "ic")
    pid = env.plan([env.copy("gd", "a.bin", "ic", "b.bin")])
    assert env.run(pid) == ExecResult(pid, "done", skipped=1)
    assert env.spy.count("copyto") == 0 and env.spy.count("download") == 1


def test_copy_onto_a_directory_is_refused(env):
    env.put("gd", "a.bin", blob(10))
    env.put("gd", "dir/inner.bin", blob(10))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "dir")])
    res = assert_refused(env, pid, env.tree())
    assert "directory" in res.error


def test_copy_from_a_read_only_remote_is_fine_into_a_writable_one(env):
    env.put("ro", "a.bin", blob(64))
    env.scan("ro")
    pid = env.plan([env.copy("ro", "a.bin", "gd", "a.bin")])
    assert env.run(pid).status == "done" and env.read("gd", "a.bin") == blob(64)


# --------------------------------------------------------------------------- #
# move
# --------------------------------------------------------------------------- #

def test_move_within_one_remote(env):
    env.put("gd", "a/x.bin", blob(120))
    env.scan("gd")
    pid = env.plan([env.move("gd", "a/x.bin", "gd", "b/z.bin")])
    assert env.run(pid) == ExecResult(pid, "done", done=1)
    assert env.read("gd", "b/z.bin") == blob(120) and not env.exists("gd", "a/x.bin")
    assert env.spy.count("download") == 0 and env.spy.count("moveto") == 1 and env.spy.count("copyto") == 0


def test_move_onto_identical_content_quarantines_the_source_and_overwrites_nothing(env):
    """What the simulator models: the destination already holds the content, so the move only removes the source."""
    data = blob(90)
    env.put("gd", "a.bin", data)
    env.put("gd", "b.bin", data)
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", "b.bin")])
    assert env.run(pid) == ExecResult(pid, "done", done=1)
    q = env.qpath(pid, "a.bin")
    assert env.read("gd", "b.bin") == data and not env.exists("gd", "a.bin") and env.read("gd", q) == data
    assert env.spy.paths("moveto") == [("a.bin", q)], "the only write parks the source; nothing goes onto b.bin"
    assert env.spy.count("copyto") == 0 and env.spy.count("deletefile") == 0 and env.spy.count("download") == 0
    assert env.plan_status(pid) == "done" and env.op_states(pid) == [("done", None)]
    [entry] = env.journal(pid, "quarantine")
    assert entry["detail"] == {"remote": "gd", "from": "a.bin", "to": q}
    [intent] = [j for j in env.journal(pid, "intent")]
    assert intent["detail"]["kind"] == "quarantine" and "identical" in intent["detail"]["why"]
    assert [j["detail"]["verdict"] for j in env.journal(pid, "verify")] == ["same"]


def test_move_onto_a_different_file_of_the_same_size_is_refused(env):
    env.put("gd", "a.bin", blob(90, 1))
    env.put("gd", "b.bin", blob(90, 2))
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", "b.bin")])
    res = assert_refused(env, pid, env.tree())
    assert "already exists" in res.error and "not provably identical" in res.error
    assert env.read("gd", "b.bin") == blob(90, 2) and env.read("gd", "a.bin") == blob(90, 1)


def test_move_onto_a_file_of_another_size_is_refused_without_reading_either(env):
    env.put("gd", "a.bin", blob(90, 1))
    env.put("gd", "b.bin", blob(91, 1))
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", "b.bin")])
    res = assert_refused(env, pid, env.tree())
    assert "already exists" in res.error and env.spy.count("download") == 0


def test_move_onto_identical_content_in_a_hashless_cloud_is_proven_by_reading_both(env):
    data = blob(70, 3)
    env.put("ic", "a.bin", data)
    env.put("ic", "b.bin", data)
    env.scan("ic")
    pid = env.plan([env.move("ic", "a.bin", "ic", "b.bin")])
    assert env.run(pid) == ExecResult(pid, "done", done=1)
    assert env.spy.count("download") == 2 and env.read("ic", env.qpath(pid, "a.bin")) == data


def test_move_onto_other_bytes_in_a_hashless_cloud_is_refused_after_reading_both(env):
    env.put("ic", "a.bin", blob(70, 3))
    env.put("ic", "b.bin", blob(70, 4))
    env.scan("ic")
    pid = env.plan([env.move("ic", "a.bin", "ic", "b.bin")])
    assert_refused(env, pid, env.tree())
    assert env.spy.count("download") == 2


def test_move_onto_itself_changes_nothing(env):
    env.put("gd", "a.bin", blob(40))
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", "a.bin")])
    before = env.tree()
    assert env.run(pid) == ExecResult(pid, "done", skipped=1)
    assert env.tree() == before and env.spy.mutations() == [] and env.exists("gd", "a.bin")


def test_move_onto_a_name_that_differs_only_in_case_is_refused(env):
    """On a real case-insensitive cloud that name is the source itself: quarantining it would lose the file."""
    data = blob(40)
    env.put("ci", "a.txt", data)
    env.put("ci", "A.TXT", data)  # these are two files on the test directory, one file on Dropbox
    env.scan("ci")
    pid = env.plan([env.move("ci", "a.txt", "ci", "A.TXT")])
    res = assert_refused(env, pid, env.tree())
    assert "only in case" in res.error


def test_move_onto_identical_content_inside_the_quarantine_is_refused(env):
    """A file in quarantine is not a live copy; moving the source away because of it would lose the content."""
    data = blob(40)
    env.put("gd", "a.bin", data)
    env.put("gd", f"{QDIR}/9/a.bin", data)
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", f"{QDIR}/9/a.bin")])
    res = assert_refused(env, pid, env.tree())
    assert "quarantine directory" in res.error and env.read("gd", "a.bin") == data


def test_move_onto_a_directory_is_refused(env):
    env.put("gd", "a.bin", blob(10))
    env.put("gd", "dir/inner.bin", blob(10))
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", "dir")])
    res = assert_refused(env, pid, env.tree())
    assert "directory" in res.error


def test_move_onto_identical_content_stops_when_the_quarantine_slot_is_taken(env):
    data = blob(40)
    env.put("gd", "a.bin", data)
    env.put("gd", "b.bin", data)
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", "b.bin")])
    env.put("gd", env.qpath(pid, "a.bin"), b"someone else's file")
    res = assert_refused(env, pid, env.tree())
    assert "occupied" in res.error


def test_a_move_onto_identical_content_satisfies_its_dependents(env):
    data = blob(40, 1)
    env.put("gd", "a.bin", data)
    env.put("gd", "b.bin", data)
    env.put("gd", "c.bin", data)
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", "b.bin"),
                    env.delete("gd", "c.bin", witness=("gd", "b.bin"), depends_on=1)])
    assert env.run(pid) == ExecResult(pid, "done", done=2)
    assert env.read("gd", env.qpath(pid, "c.bin")) == data and env.read("gd", "b.bin") == data


def _move_onto_identical_plan(env):
    """a.bin quarantined because b.bin has its content; p.bin moved to q.bin for real."""
    data = blob(60, 7)
    env.put("gd", "a.bin", data)
    env.put("gd", "b.bin", data)
    env.put("gd", "p.bin", blob(50, 8))
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "gd", "b.bin"), env.move("gd", "p.bin", "gd", "q.bin")])
    assert env.run(pid) == ExecResult(pid, "done", done=2)
    return pid, data


def test_restore_brings_back_a_source_that_a_move_quarantined_and_leaves_real_moves_alone(env):
    pid, data = _move_onto_identical_plan(env)
    res = env.restore(pid)
    assert res == ExecResult(pid, "done", done=1)
    assert env.read("gd", "a.bin") == data and env.read("gd", "b.bin") == data
    assert env.exists("gd", "q.bin") and not env.exists("gd", "p.bin"), "a plain move is not undone"
    assert env.files(quarantine=True) == {}
    assert [s for s, _ in env.op_states(pid)] == ["skipped", "done"]
    assert env.restore(pid) == ExecResult(pid, "done"), "a second restore has nothing left"


def test_purge_removes_the_source_a_move_quarantined_and_only_that(env):
    pid, data = _move_onto_identical_plan(env)
    before = env.files()
    env.spy.calls.clear()
    assert env.purge(pid, older_than_days=0) == ExecResult(pid, "done", done=1)
    assert env.spy.paths("deletefile") == [(env.qpath(pid, "a.bin"),)]
    assert env.files() == before and env.files(quarantine=True) == {}


def test_purge_keeps_the_source_a_move_quarantined_when_its_destination_is_gone(env):
    pid, data = _move_onto_identical_plan(env)
    env.roots["gd"].joinpath("b.bin").unlink()
    res = env.purge(pid, older_than_days=0)
    assert (res.status, res.done, res.failed) == ("partial", 0, 1) and "b.bin" in res.error
    assert env.read("gd", env.qpath(pid, "a.bin")) == data and env.spy.count("deletefile") == 0


def test_move_between_remotes_is_refused(env):
    env.put("gd", "a.bin", blob(90))
    env.scan("gd")
    pid = env.plan([env.move("gd", "a.bin", "dbx", "a.bin")])
    res = assert_refused(env, pid, env.tree())
    assert "within one remote" in res.error


def test_move_inside_a_hashless_cloud_is_verified_by_reading_the_bytes_before_and_after(env):
    env.put("ic", "a/n.txt", blob(77, 9))
    env.scan("ic")
    pid = env.plan([env.move("ic", "a/n.txt", "ic", "b/n.txt")])
    assert env.run(pid).status == "done"
    assert env.spy.paths("download") == [("a/n.txt",), ("b/n.txt",)]
    assert env.read("ic", "b/n.txt") == blob(77, 9) and not env.exists("ic", "a/n.txt")


# --------------------------------------------------------------------------- #
# delete = quarantine
# --------------------------------------------------------------------------- #

def test_delete_moves_the_source_into_the_quarantine_of_its_own_cloud(env):
    data = blob(400, 6)
    env.put("gd", "a/x.bin", data)
    env.put("gd", "keep/x.bin", data)
    env.scan("gd")
    pid = env.plan([env.delete("gd", "a/x.bin", witness=("gd", "keep/x.bin"))])
    res = env.run(pid)
    assert res == ExecResult(pid, "done", done=1)
    assert not env.exists("gd", "a/x.bin") and env.read("gd", "keep/x.bin") == data
    q = f"{QDIR}/{pid}/a/x.bin"
    assert env.read("gd", q) == data
    assert env.spy.count("deletefile") == 0, "execute never deletes bytes"
    assert env.spy.count("download") == 0
    [entry] = env.journal(pid, "quarantine")
    assert entry["detail"] == {"remote": "gd", "from": "a/x.bin", "to": q}


def test_delete_with_a_witness_on_a_hashless_cloud_is_proven_by_download(env):
    data = blob(250, 7)
    env.put("gd", "a.bin", data)
    env.put("ic", "w.bin", data)
    env.scan("gd", "ic")
    pid = env.plan([env.delete("gd", "a.bin", witness=("ic", "w.bin"))])
    assert env.run(pid).status == "done"
    assert env.spy.paths("download") == [("w.bin",)]
    assert env.read("gd", env.qpath(pid, "a.bin")) == data and env.read("ic", "w.bin") == data


def _witness_missing(env):
    return ("gd", "w/missing.bin")


def _witness_other_bytes_same_domain(env):
    env.put("gd", "w/x.bin", blob(100, 99))
    return ("gd", "w/x.bin")


def _witness_other_bytes_other_domain(env):
    env.put("dbx", "w/x.bin", blob(100, 99))
    return ("dbx", "w/x.bin")


def _witness_other_bytes_hashless(env):
    env.put("ic", "w/x.bin", blob(100, 99))
    return ("ic", "w/x.bin")


def _witness_shorter(env):
    env.put("gd", "w/x.bin", blob(99, 1))
    return ("gd", "w/x.bin")


def _witness_is_the_source(env):
    return ("gd", "a/x.bin")


def _witness_in_quarantine(env):
    env.put("gd", f"{QDIR}/9/x.bin", blob(100, 1))  # identical, but a purge would remove it
    return ("gd", f"{QDIR}/9/x.bin")


def _witness_is_a_directory(env):
    env.put("gd", "w/x.bin/inner", blob(100, 1))
    return ("gd", "w/x.bin")


def _no_witness(env):
    return None


@pytest.mark.parametrize("make_witness", [
    _witness_missing, _witness_other_bytes_same_domain, _witness_other_bytes_other_domain,
    _witness_other_bytes_hashless, _witness_shorter, _witness_is_the_source, _witness_in_quarantine,
    _witness_is_a_directory, _no_witness,
], ids=lambda f: f.__name__.lstrip("_"))
def test_delete_without_a_provably_identical_surviving_witness_is_refused(env, make_witness):
    env.put("gd", "a/x.bin", blob(100, 1))
    witness = make_witness(env)
    env.scan("gd", "dbx", "ic")
    pid = env.plan([env.delete("gd", "a/x.bin", witness=witness)])
    assert_refused(env, pid, env.tree())
    assert env.read("gd", "a/x.bin") == blob(100, 1)


def test_a_witness_that_cannot_be_read_back_is_not_a_proof(env):
    env.put("gd", "a.bin", blob(100, 1))
    env.put("dbx", "w.bin", blob(100, 1))  # identical, but no shared hash and no bridge: needs a download
    env.scan("gd", "dbx")
    env.spy.download_error = RcloneError("network down")
    pid = env.plan([env.delete("gd", "a.bin", witness=("dbx", "w.bin"))])
    res = assert_refused(env, pid, env.tree())
    assert "cannot download" in res.error


def test_delete_stops_when_the_quarantine_slot_is_taken(env):
    data = blob(100, 1)
    env.put("gd", "a.bin", data)
    env.put("gd", "w.bin", data)
    env.scan("gd")
    pid = env.plan([env.delete("gd", "a.bin", witness=("gd", "w.bin"))])
    env.put("gd", env.qpath(pid, "a.bin"), b"someone else's file")
    res = assert_refused(env, pid, env.tree())
    assert "occupied" in res.error


# --------------------------------------------------------------------------- #
# The source changed after the plan was made
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind", ["copy", "move", "delete"])
@pytest.mark.parametrize("change", ["size", "same-size content"])
def test_a_source_that_changed_since_the_scan_stops_the_plan(env, kind, change):
    old = blob(200, 1)
    env.put("gd", "a/x.bin", old)
    env.put("gd", "w/x.bin", old)  # the witness holds the content the plan was made for
    env.scan("gd")
    op = {"copy": lambda: env.copy("gd", "a/x.bin", "gd", "b/x.bin"),
          "move": lambda: env.move("gd", "a/x.bin", "gd", "b/x.bin"),
          "delete": lambda: env.delete("gd", "a/x.bin", witness=("gd", "w/x.bin"))}[kind]()
    env.put("gd", "a/x.bin", blob(250, 1) if change == "size" else blob(200, 2))
    pid = env.plan([op])
    res = assert_refused(env, pid, env.tree())
    assert "changed since the plan" in res.error
    assert env.op_states(pid)[0][0] == "failed"


@pytest.mark.parametrize("kind", ["copy", "move", "delete"])
def test_a_source_that_vanished_stops_the_plan(env, kind):
    data = blob(60)
    env.put("gd", "a.bin", data)
    env.put("gd", "w.bin", data)
    env.scan("gd")
    op = {"copy": lambda: env.copy("gd", "a.bin", "gd", "b.bin"),
          "move": lambda: env.move("gd", "a.bin", "gd", "b.bin"),
          "delete": lambda: env.delete("gd", "a.bin", witness=("gd", "w.bin"))}[kind]()
    (env.roots["gd"] / "a.bin").unlink()
    pid = env.plan([op])
    res = assert_refused(env, pid, env.tree())
    assert "no longer exists" in res.error


def test_a_source_that_became_a_directory_stops_the_plan(env):
    env.put("gd", "a.bin", blob(60))
    env.scan("gd")
    op = env.copy("gd", "a.bin", "gd", "b.bin")
    (env.roots["gd"] / "a.bin").unlink()
    env.put("gd", "a.bin/inner", blob(5))
    assert_refused(env, env.plan([op]), env.tree())


def test_a_hashless_source_edited_in_place_fails_the_content_proof(env):
    """iCloud has no hash to catch a same-size edit; comparing the real bytes does."""
    old = blob(120, 1)
    env.put("ic", "n.txt", old)
    env.put("gd", "w.txt", old)
    env.scan("ic", "gd")
    op = env.delete("ic", "n.txt", witness=("gd", "w.txt"))
    env.put("ic", "n.txt", blob(120, 2))
    res = assert_refused(env, env.plan([op]), env.tree())
    assert "not provably identical" in res.error and env.spy.count("download") == 1


def test_a_stale_bridge_does_not_vouch_for_an_edited_hashless_source(env):
    old = blob(120, 1)
    env.put("ic", "n.txt", old)
    env.put("gd", "w.txt", old)
    env.scan("ic", "gd")
    identity.bridge_entry(env.conn, Rclone(), env.entry("ic", "n.txt")["id"], env.tmp / "bridge-tmp")
    op = env.delete("ic", "n.txt", witness=("gd", "w.txt"))
    env.put("ic", "n.txt", blob(120, 2))  # same size, new mtime: the bridge no longer describes it
    res = assert_refused(env, env.plan([op]), env.tree())
    assert "not provably identical" in res.error and env.spy.count("download") == 1


def test_an_unchanged_bridged_hashless_source_is_deleted_without_any_download(env):
    data = blob(120, 1)
    env.put("ic", "n.txt", data)
    env.put("gd", "w.txt", data)
    env.scan("ic", "gd")
    identity.bridge_entry(env.conn, Rclone(), env.entry("ic", "n.txt")["id"], env.tmp / "bridge-tmp")
    pid = env.plan([env.delete("ic", "n.txt", witness=("gd", "w.txt"))])
    assert env.run(pid).status == "done"
    assert env.spy.count("download") == 0
    assert env.read("ic", env.qpath(pid, "n.txt")) == data


@pytest.mark.parametrize("kind", ["copy", "move", "delete"])
def test_a_hashless_source_that_changed_size_stops_the_plan(env, kind):
    old = blob(100, 1)
    env.put("ic", "n.txt", old)
    env.put("gd", "w.txt", old)
    env.scan("ic", "gd")
    op = {"copy": lambda: env.copy("ic", "n.txt", "ic", "m.txt"),
          "move": lambda: env.move("ic", "n.txt", "ic", "m.txt"),
          "delete": lambda: env.delete("ic", "n.txt", witness=("gd", "w.txt"))}[kind]()
    env.put("ic", "n.txt", blob(130, 1))
    res = assert_refused(env, env.plan([op]), env.tree())
    assert "changed since the plan" in res.error and env.spy.count("download") == 0


def test_a_poisoned_bridge_cannot_vouch_for_a_different_file(env):
    """A bridge that shares one hash with the source but contradicts another is not about that file."""
    a, z = blob(100, 1), blob(100, 2)
    env.put("gd", "a.bin", a)
    env.put("dbx", "w.bin", z)  # the 'witness' holds other bytes of the same size
    env.scan("gd", "dbx")
    full = lambda data: hashing.hash_stream(io.BytesIO(data))  # noqa: E731
    identity.record_bridge(env.conn, {**full(z), "md5": full(a)["md5"]})  # md5 of a.bin, everything else z's
    pid = env.plan([env.delete("gd", "a.bin", witness=("dbx", "w.bin"))])
    assert_refused(env, pid, env.tree())


def test_a_witness_that_is_the_same_file_on_a_case_insensitive_cloud_is_refused(env):
    data = blob(50)
    env.put("ci", "A/File.txt", data)
    env.put("ci", "a/file.txt", data)  # two files here, but one file to the cloud
    env.scan("ci")
    pid = env.plan([env.delete("ci", "A/File.txt", witness=("ci", "a/file.txt"))])
    res = assert_refused(env, pid, env.tree())
    assert "witness is the file being deleted" in res.error


def test_a_failing_stat_stops_the_plan_before_anything_is_written(env):
    env.put("gd", "a.bin", blob(30))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    env.spy.stat_error = RcloneError("rclone lsjson --stat failed (exit 1): token expired")
    res = assert_refused(env, pid, env.tree())
    assert "cannot stat" in res.error


class _StatOnly(Spy):
    """Answers stat with a canned object (e.g. a Google-native document: no size, no bytes)."""

    def __init__(self, answer: dict) -> None:
        super().__init__()
        self.answer = answer

    def stat(self, remote, path, *, hash_types=()):
        return self.answer


def test_an_object_without_a_known_size_is_never_touched(env):
    env.put("gd", "doc.gdoc", b"{}")
    env.scan("gd")
    pid = env.plan([env.copy("gd", "doc.gdoc", "dbx", "doc.gdoc")])
    env.spy = _StatOnly({"Path": "doc.gdoc", "Name": "doc.gdoc", "Size": -1, "IsDir": False})
    res = assert_refused(env, pid, env.tree())
    assert "no known size" in res.error


def test_a_plan_without_ops_just_completes(env):
    pid = env.plan([])
    assert env.run(pid) == ExecResult(pid, "done") and env.plan_status(pid) == "done"


# A concurrent writer takes the destination between our check and rclone's move/copy. rclone's
# --ignore-existing then does nothing and exits 0: only the post-checks can notice.

def test_a_destination_that_appears_during_a_move_is_noticed_and_nothing_is_lost(env):
    data = blob(60)
    env.put("gd", "a.bin", data)
    env.scan("gd")
    env.spy.before["moveto"] = lambda src, dst: env.put("gd", dst, data)  # even identical bytes
    pid = env.plan([env.move("gd", "a.bin", "gd", "b.bin")])
    res = assert_refused(env, pid, {**env.tree(), ("gd", "b.bin"): data}, mutations=True)
    assert "still there" in res.error and env.read("gd", "a.bin") == data


def test_a_destination_that_appears_during_a_copy_is_never_overwritten(env):
    env.put("gd", "a.bin", blob(60, 1))
    env.scan("gd")
    env.spy.before["copyto"] = lambda src, dst: env.put("gd", dst, blob(60, 2))
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    res = assert_refused(env, pid, {**env.tree(), ("gd", "b.bin"): blob(60, 2)}, mutations=True)
    assert "not provably identical" in res.error and env.read("gd", "b.bin") == blob(60, 2)


def test_a_quarantine_slot_taken_during_the_move_is_noticed(env):
    data = blob(60)
    env.put("gd", "a.bin", data)
    env.put("gd", "w.bin", data)
    env.scan("gd")
    pid = env.plan([env.delete("gd", "a.bin", witness=("gd", "w.bin"))])
    env.spy.before["moveto"] = lambda src, dst: env.put("gd", dst, data)  # same size and bytes
    res = assert_refused(env, pid, {**env.tree(), ("gd", env.qpath(pid, "a.bin")): data,
                                    ("gd", f"{QDIR}"): None, ("gd", f"{QDIR}/{pid}"): None}, mutations=True)
    assert "still there" in res.error and env.read("gd", "a.bin") == data


# --------------------------------------------------------------------------- #
# Read-only remotes, unsafe paths, unknown remotes, dependencies
# --------------------------------------------------------------------------- #

def test_nothing_is_written_to_a_read_only_remote(env):
    data = blob(80)
    env.put("gd", "a.bin", data)
    env.put("ro", "r.bin", data)
    env.put("ro", "w.bin", data)
    env.scan("gd", "ro")
    for op in (env.copy("gd", "a.bin", "ro", "new.bin"),
               env.move("ro", "r.bin", "ro", "moved.bin"),
               env.delete("ro", "r.bin", witness=("gd", "a.bin"))):
        res = assert_refused(env, env.plan([op]), env.tree())
        assert "read-only" in res.error


@pytest.mark.parametrize("field,value", [
    ("dst_path", "../outside.bin"), ("dst_path", "a//b.bin"), ("dst_path", "./x.bin"), ("dst_path", ""),
    ("src_path", "../decoy.bin"), ("witness_path", "../decoy.bin"),
])
def test_paths_that_could_leave_the_remote_are_refused_before_rclone_is_called(env, field, value):
    data = blob(40)
    env.put("gd", "a.bin", data)
    env.put("gd", "w.bin", data)
    (env.roots["gd"].parent / "decoy.bin").write_bytes(data)  # sits just outside the gd root
    env.scan("gd")
    before = env.tree()
    outside = {p.name for p in env.roots["gd"].parent.iterdir()}
    op = env.copy("gd", "a.bin", "gd", "b.bin") if field == "dst_path" else \
        env.delete("gd", "a.bin", witness=("gd", "w.bin"))
    setattr(op, field, value)
    pid = env.plan([op])
    res = assert_refused(env, pid, before)
    assert "path" in res.error
    assert env.spy.calls == [], "no rclone call at all for an unsafe path"
    assert (env.roots["gd"].parent / "decoy.bin").read_bytes() == data
    assert {p.name for p in env.roots["gd"].parent.iterdir()} == outside


def test_a_remote_missing_from_the_configuration_is_refused(env):
    env.conn.execute("INSERT INTO remotes(name, provider, read_remote, created_at) VALUES ('ghost', 'local', '/x', 'now')")
    env.put("gd", "a.bin", blob(10))
    env.scan("gd")
    ghost = db.remote_id(env.conn, "ghost")
    pid = env.plan([Op("copy", ghost, "a.bin", dst_remote_id=env.rid("gd"), dst_path="b.bin")])
    res = assert_refused(env, pid, env.tree())
    assert "not in the configuration" in res.error


def test_dependencies_must_point_to_an_earlier_finished_op(env):
    data = blob(30)
    env.put("gd", "a.bin", data)
    env.put("gd", "w.bin", data)
    env.scan("gd")
    for depends_on in (2, 1, 9):  # a later op, itself, a missing op
        pid = env.plan([env.delete("gd", "a.bin", witness=("gd", "w.bin"), depends_on=depends_on),
                        env.copy("gd", "w.bin", "gd", "c.bin")])
        res = assert_refused(env, pid, env.tree())
        assert "depends on op" in res.error


def test_a_delete_does_not_run_after_its_copy_failed(env):
    data = blob(30)
    env.put("gd", "a.bin", data)
    env.put("dbx", "taken.bin", blob(30, 5))  # blocks the copy: different content already there
    env.scan("gd", "dbx")
    pid = env.plan([env.copy("gd", "a.bin", "dbx", "taken.bin"),
                    env.delete("gd", "a.bin", witness=("dbx", "taken.bin"), depends_on=1)])
    res = env.run(pid)
    assert res.status == "failed" and res.done == 0
    assert env.op_states(pid)[1] == ("pending", None)
    assert env.read("gd", "a.bin") == data and env.spy.mutations() == []


# --------------------------------------------------------------------------- #
# Stopping, progress, resuming
# --------------------------------------------------------------------------- #

def test_the_first_failure_stops_the_plan_and_is_journaled(env):
    for name in ("a", "b", "c"):
        env.put("gd", f"{name}.bin", blob(20, ord(name)))
    env.scan("gd")
    ops = [env.copy("gd", "a.bin", "gd", "a2.bin"), env.copy("gd", "b.bin", "gd", "b2.bin"),
           env.copy("gd", "c.bin", "gd", "c2.bin")]
    env.put("gd", "b.bin", blob(25, 1))  # b changed
    pid = env.plan(ops)
    res = env.run(pid)
    assert (res.status, res.done, res.failed, res.skipped) == ("failed", 1, 1, 0)
    assert res.error.startswith("op 2 (copy gd:b.bin)")
    assert [s for s, _ in env.op_states(pid)] == ["done", "failed", "pending"]
    assert env.exists("gd", "a2.bin") and not env.exists("gd", "b2.bin") and not env.exists("gd", "c2.bin")
    [err] = env.journal(pid, "error")
    assert "changed since the plan" in err["detail"]["error"] and err["op_id"] is not None
    plan = plans.get_plan(env.conn, pid, with_ops=False)
    assert plan.status == "failed" and plan.finished_at


def test_progress_is_reported_after_every_op(env):
    for name in ("a", "b"):
        env.put("gd", f"{name}.bin", blob(20, ord(name)))
    env.scan("gd")
    seen = []
    pid = env.plan([env.copy("gd", "a.bin", "gd", "a2.bin"), env.copy("gd", "b.bin", "gd", "b2.bin")])
    env.run(pid, progress=lambda done, total, op: seen.append((done, total, op.seq, op.status)))
    assert seen == [(1, 2, 1, "done"), (2, 2, 2, "done")]


def test_progress_reports_the_failing_op_too(env):
    env.put("gd", "a.bin", blob(20))
    env.scan("gd")
    seen = []
    pid = env.plan([env.copy("gd", "a.bin", "gd", "a2.bin"), env.copy("gd", "ghost.bin", "gd", "g2.bin")])
    env.run(pid, progress=lambda done, total, op: seen.append((done, total, op.seq, op.status)))
    assert seen == [(1, 2, 1, "done"), (1, 2, 2, "failed")]


def test_a_crashing_progress_callback_never_leaves_the_plan_executing(env):
    env.put("gd", "a.bin", blob(20))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "a2.bin"), env.copy("gd", "a.bin", "gd", "a3.bin")])

    def boom(done, total, op):
        raise RuntimeError("ui died")

    with pytest.raises(RuntimeError):
        env.run(pid, progress=boom)
    assert env.plan_status(pid) == "failed"
    assert env.run(pid).status == "done"  # and it resumes where it stopped
    assert env.exists("gd", "a3.bin") and env.spy.count("copyto") == 2


def test_a_failed_plan_resumes_without_repeating_finished_ops(env):
    for name in ("a", "b", "c"):
        env.put("gd", f"{name}.bin", blob(20, ord(name)))
    env.scan("gd")
    ops = [env.copy("gd", f"{n}.bin", "gd", f"{n}2.bin") for n in "abc"]
    env.put("gd", "b.bin", blob(25, 1))  # b changed after the scan
    pid = env.plan(ops)
    assert env.run(pid).status == "failed"
    assert env.spy.paths("copyto") == [("a.bin", "a2.bin")]

    env.put("gd", "b.bin", blob(20, ord("b")))  # put back the content the plan was made for
    env.spy.calls.clear()
    seen = []
    res = env.run(pid, progress=lambda d, t, op: seen.append((d, t, op.seq)))
    assert res == ExecResult(pid, "done", done=3)
    assert env.spy.paths("copyto") == [("b.bin", "b2.bin"), ("c.bin", "c2.bin")]
    assert seen == [(2, 3, 2), (3, 3, 3)]
    assert [s for s in env.op_states(pid)] == [("done", None)] * 3, "the old failure message is cleared"
    assert env.plan_status(pid) == "done"


def test_a_failed_plan_still_needs_a_current_digest_to_resume(env):
    env.put("gd", "a.bin", blob(20))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "ghost.bin", "gd", "g.bin")])
    assert env.run(pid).status == "failed"
    env.scan("gd")
    with pytest.raises(plans.StaleSimulation, match="propose a new plan"):  # simulating a failed plan is impossible
        env.run(pid)
    assert env.plan_status(pid) == "failed" and env.spy.mutations() == []


# --------------------------------------------------------------------------- #
# Temp files
# --------------------------------------------------------------------------- #

def test_downloaded_scratch_files_are_always_removed(env, tmp_path):
    env.put("gd", "a.bin", blob(100, 1))
    env.put("gd", "b.bin", blob(100, 3))
    env.put("dbx", "taken.bin", blob(100, 2))
    env.scan("gd", "dbx")
    work = tmp_path / "work"
    # one op that downloads and succeeds, one that downloads and then fails verification
    pid = env.plan([env.copy("gd", "a.bin", "dbx", "new.bin"), env.copy("gd", "b.bin", "dbx", "taken.bin")])
    assert env.run(pid, tmpdir=work).status == "failed"
    assert work.is_dir() and list(work.iterdir()) == [], "the caller's directory is kept, but emptied"
    assert env.spy.count("download") == 2


def test_the_default_scratch_directory_is_removed(env, tmp_path, monkeypatch):
    scratch = tmp_path / "sys-tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    env.put("gd", "a.bin", blob(100, 1))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "dbx", "a.bin")])
    assert env.run(pid).status == "done" and env.spy.count("download") == 1
    assert list(scratch.iterdir()) == []


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #

def _three_deletes(env):
    """Three quarantined files on three clouds, each with a live witness on gd."""
    files = {("gd", "a/one.bin"): blob(100, 1), ("dbx", "b/two.bin"): blob(110, 2), ("ic", "c/three.bin"): blob(120, 3)}
    for (remote, path), data in files.items():
        env.put(remote, path, data)
        env.put("gd", f"keep/{path.rsplit('/', 1)[1]}", data)
    env.scan("gd", "dbx", "ic")
    pid = env.plan([env.delete(r, p, witness=("gd", f"keep/{p.rsplit('/', 1)[1]}")) for (r, p) in files])
    return pid, files


def test_restore_puts_every_quarantined_file_back(env):
    pid, files = _three_deletes(env)
    before = env.files()
    assert env.run(pid).status == "done"
    assert all(not env.exists(r, p) for (r, p) in files)
    env.spy.calls.clear()
    res = env.restore(pid)
    assert res == ExecResult(pid, "done", done=3)
    assert env.files() == before and env.files(quarantine=True) == {}
    assert [s for s, _ in env.op_states(pid)] == ["skipped"] * 3
    assert [j["detail"]["status"] for j in env.journal(pid, "restore")] == ["restored"] * 3
    # newest delete first
    assert [j["detail"]["to"] for j in env.journal(pid, "restore")] == ["c/three.bin", "b/two.bin", "a/one.bin"]
    assert env.spy.count("deletefile") == 0


def test_restore_twice_is_a_no_op(env):
    pid, _ = _three_deletes(env)
    env.run(pid)
    env.restore(pid)
    env.spy.calls.clear()
    before = env.tree()
    assert env.restore(pid) == ExecResult(pid, "done")
    assert env.spy.calls == [] and env.tree() == before


def test_restore_never_overwrites_a_path_that_is_taken_again(env):
    pid, files = _three_deletes(env)
    env.run(pid)
    env.put("dbx", "b/two.bin", b"a new file lives here now")
    res = env.restore(pid)
    assert (res.status, res.done, res.failed) == ("partial", 2, 1)
    assert "occupied" in res.error and "op 2" in res.error
    assert env.read("dbx", "b/two.bin") == b"a new file lives here now"
    assert env.read("dbx", env.qpath(pid, "b/two.bin")) == files[("dbx", "b/two.bin")]
    assert env.read("gd", "a/one.bin") == files[("gd", "a/one.bin")]
    assert env.read("ic", "c/three.bin") == files[("ic", "c/three.bin")]
    assert [s for s, _ in env.op_states(pid)] == ["skipped", "done", "skipped"]
    # free the path and the leftover can be restored later
    (env.roots["dbx"] / "b/two.bin").unlink()
    assert env.restore(pid) == ExecResult(pid, "done", done=1)
    assert env.read("dbx", "b/two.bin") == files[("dbx", "b/two.bin")]


def test_restore_reports_a_quarantined_file_that_is_gone(env):
    pid, files = _three_deletes(env)
    env.run(pid)
    (env.roots["ic"] / env.qpath(pid, "c/three.bin")).unlink()
    res = env.restore(pid)
    assert (res.status, res.done, res.failed) == ("partial", 2, 1) and "gone" in res.error


def test_restore_of_a_plan_that_deleted_nothing_does_nothing(env):
    env.put("gd", "a.bin", blob(10))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    env.run(pid)
    env.spy.calls.clear()
    assert env.restore(pid) == ExecResult(pid, "done")
    assert env.spy.calls == [] and env.exists("gd", "b.bin")


def test_restore_refuses_a_plan_that_is_still_executing(env):
    env.put("gd", "a.bin", blob(10))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    plans.set_status(env.conn, pid, "executing")
    with pytest.raises(ExecutionError):
        env.restore(pid)


def test_a_restored_delete_no_longer_satisfies_a_dependency(env):
    data = blob(50)
    env.put("gd", "a.bin", data)
    env.put("gd", "w.bin", data)
    env.scan("gd")
    pid = env.plan([env.delete("gd", "a.bin", witness=("gd", "w.bin")),
                    env.copy("gd", "w.bin", "gd", "z.bin", depends_on=1)])
    env.conn.execute("UPDATE plan_ops SET status = 'skipped', error = 'restored from quarantine' "
                     "WHERE plan_id = ? AND seq = 1", (pid,))
    plans.set_status(env.conn, pid, "failed")
    res = env.run(pid)
    assert res.status == "failed" and "depends on op 1, which is skipped" in res.error
    assert not env.exists("gd", "z.bin")


# --------------------------------------------------------------------------- #
# Purge
# --------------------------------------------------------------------------- #

def _age(env, pid: int, seq: int, days: int) -> None:
    env.conn.execute(
        "UPDATE plan_ops SET executed_at = strftime('%Y-%m-%dT%H:%M:%f000Z', 'now', ?) WHERE plan_id = ? AND seq = ?",
        (f"-{days} days", pid, seq))


def test_purge_removes_only_old_enough_quarantined_files(env):
    pid, files = _three_deletes(env)
    env.run(pid)
    _age(env, pid, 1, 40)
    _age(env, pid, 2, 29)
    before = env.tree()
    env.spy.calls.clear()
    res = env.purge(pid)
    assert res == ExecResult(pid, "done", done=1, skipped=2)
    assert env.spy.paths("deletefile") == [(env.qpath(pid, "a/one.bin"),)]
    after = env.tree()
    assert {k for k in before if k not in after} == {("gd", env.qpath(pid, "a/one.bin"))}
    assert set(after) <= set(before) and all(after[k] == before[k] for k in after)

    # the file is gone for good, a second purge leaves the rest alone, and 0 days takes everything left
    env.spy.calls.clear()
    assert env.purge(pid) == ExecResult(pid, "done", done=0, skipped=3)
    assert env.spy.count("deletefile") == 0
    res = env.purge(pid, older_than_days=0)
    assert res == ExecResult(pid, "done", done=2, skipped=1)
    assert env.files(quarantine=True) == {}
    # witnesses and every other live file are still there
    assert all(env.read("gd", f"keep/{p.rsplit('/', 1)[1]}") == d for (_, p), d in files.items())


def test_purge_only_ever_deletes_below_the_plans_quarantine_directory(env):
    pid, files = _three_deletes(env)
    env.run(pid)
    before = env.files()
    env.purge(pid, older_than_days=0)
    prefix = f"{QDIR}/{pid}/"
    assert env.spy.paths("deletefile") and all(p[0].startswith(prefix) for p in env.spy.paths("deletefile"))
    assert env.files() == before, "live files are untouched; only quarantined ones went away"


def test_purge_ignores_restored_deletes(env):
    pid, files = _three_deletes(env)
    env.run(pid)
    env.put("dbx", "b/two.bin", b"taken")  # keeps op 2 in quarantine, the others are restored
    env.restore(pid)
    env.spy.calls.clear()
    res = env.purge(pid, older_than_days=0)
    assert res == ExecResult(pid, "done", done=1)
    assert env.spy.paths("deletefile") == [(env.qpath(pid, "b/two.bin"),)]
    assert env.read("gd", "a/one.bin") == files[("gd", "a/one.bin")] and env.exists("ic", "c/three.bin")


def test_purge_refuses_a_path_that_would_leave_the_quarantine(env):
    """Forged delete rows (the DB is not trusted either): '..' must never reach rclone deletefile."""
    victim = env.roots["gd"].parent / "victim.txt"
    victim.write_text("outside the remote")
    inside = env.put("gd", "live/precious.txt", "live data")
    pid = env.plan([Op("delete", env.rid("gd"), "x.txt")], approve=False)
    plans.set_status(env.conn, pid, "done")
    for seq, bad in enumerate(["../victim.txt", "../../clouds/gd/live/precious.txt", "a/../../victim.txt",
                               "a//b.txt", "/victim.txt", ""], start=1):
        if seq > 1:
            env.conn.execute(
                "INSERT INTO plan_ops(plan_id, seq, op, src_remote_id, src_path, status, executed_at) "
                "VALUES (?, ?, 'delete', ?, ?, 'done', '2000-01-01T00:00:00.000000Z')", (pid, seq, env.rid("gd"), bad))
        else:
            env.conn.execute("UPDATE plan_ops SET src_path = ?, status = 'done', executed_at = "
                             "'2000-01-01T00:00:00.000000Z' WHERE plan_id = ?", (bad, pid))
    before = env.tree()
    res = env.purge(pid, older_than_days=0)
    assert (res.status, res.done, res.failed) == ("partial", 0, 6)
    assert env.spy.calls == [] and env.tree() == before
    assert victim.read_text() == "outside the remote" and inside.read_text() == "live data"
    assert len(env.journal(pid, "purge")) == 6


def test_purge_leaves_a_read_only_remote_alone(env):
    pid = env.plan([Op("delete", env.rid("ro"), "x.bin")], approve=False)
    env.put("ro", env.qpath(pid, "x.bin"), blob(10))
    env.conn.execute("UPDATE plan_ops SET status = 'done', executed_at = '2000-01-01T00:00:00.000000Z' "
                     "WHERE plan_id = ?", (pid,))
    before = env.tree()
    res = env.purge(pid, older_than_days=0)
    assert res.status == "partial" and "read-only" in res.error
    assert env.tree() == before and env.spy.calls == []


def test_purge_rejects_a_negative_age_and_a_running_plan(env):
    env.put("gd", "a.bin", blob(10))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    with pytest.raises(ExecutionError):
        env.purge(pid, older_than_days=-1)
    plans.set_status(env.conn, pid, "executing")
    with pytest.raises(ExecutionError):
        env.purge(pid)


def test_purge_of_a_plan_that_deleted_nothing_does_nothing(env):
    env.put("gd", "a.bin", blob(10))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "b.bin")])
    env.run(pid)
    env.spy.calls.clear()
    assert env.purge(pid, older_than_days=0) == ExecResult(pid, "done")
    assert env.spy.calls == []


def _quarantined_pair(env, *, source="gd", witness="gd", seed=21):
    """One delete executed: its file waits in quarantine, `witness` holds the identical live copy."""
    data = blob(130, seed)
    env.put(source, "docs/a.bin", data)
    env.put(witness, "keep/a.bin", data)
    env.scan(source, witness)
    pid = env.plan([env.delete(source, "docs/a.bin", witness=(witness, "keep/a.bin"))])
    assert env.run(pid).status == "done"
    env.spy.calls.clear()
    return pid, data


def _kept(env, pid, source="gd", data=None):
    """The purge failed for the file, said so in the journal, and left the quarantined copy where it was."""
    res = env.purge(pid, older_than_days=0)
    assert (res.status, res.done, res.failed) == ("partial", 0, 1) and "is kept" in res.error, res
    assert env.spy.count("deletefile") == 0
    if data is not None:
        assert env.read(source, env.qpath(pid, "docs/a.bin")) == data
    [entry] = env.journal(pid, "purge")
    assert entry["detail"]["status"] == "failed" and "is kept" in entry["detail"]["error"]
    return res


def test_purge_checks_the_witness_and_then_deletes(env):
    pid, data = _quarantined_pair(env)
    assert env.purge(pid, older_than_days=0) == ExecResult(pid, "done", done=1)
    assert env.spy.paths("deletefile") == [(env.qpath(pid, "docs/a.bin"),)]
    assert env.read("gd", "keep/a.bin") == data and env.files(quarantine=True) == {}
    assert [j["detail"]["what"] for j in env.journal(pid, "verify")][-1] == "witness re-check before purge"


def test_purge_keeps_a_file_whose_witness_has_been_deleted(env):
    """The quarantined copy may now be the only one left."""
    pid, data = _quarantined_pair(env)
    env.roots["gd"].joinpath("keep/a.bin").unlink()
    res = _kept(env, pid, data=data)
    assert "does not exist" in res.error


def test_purge_keeps_a_file_whose_witness_was_moved_away(env):
    pid, data = _quarantined_pair(env)
    shutil.move(env.roots["gd"] / "keep/a.bin", env.roots["gd"] / "elsewhere.bin")
    _kept(env, pid, data=data)


def test_purge_keeps_a_file_whose_witness_now_holds_other_bytes(env):
    pid, data = _quarantined_pair(env)
    env.put("gd", "keep/a.bin", blob(len(data), 99))  # same size, same name, other content
    res = _kept(env, pid, data=data)
    assert "not provably identical" in res.error


def test_purge_keeps_a_file_whose_witness_changed_size(env):
    pid, data = _quarantined_pair(env)
    env.put("gd", "keep/a.bin", data + b"!")
    _kept(env, pid, data=data)


def test_purge_keeps_a_file_whose_witness_became_a_directory(env):
    pid, data = _quarantined_pair(env)
    (env.roots["gd"] / "keep/a.bin").unlink()
    env.put("gd", "keep/a.bin/inner", data)
    res = _kept(env, pid, data=data)
    assert "directory" in res.error


def test_purge_keeps_a_file_when_the_witness_cannot_be_read(env):
    pid, data = _quarantined_pair(env, source="gd", witness="dbx")
    env.conn.execute("DELETE FROM hash_bridges")       # no bridge: proving the pair needs a download
    env.spy.download_error = RcloneError("network down")
    res = _kept(env, pid, data=data)
    assert "cannot download" in res.error


def test_purge_takes_only_what_still_has_a_witness(env):
    pid, files = _three_deletes(env)
    env.run(pid)
    env.roots["gd"].joinpath("keep/two.bin").unlink()       # the witness of the dbx file
    res = env.purge(pid, older_than_days=0)
    assert (res.status, res.done, res.failed) == ("partial", 2, 1) and "two.bin" in res.error
    assert env.read("dbx", env.qpath(pid, "b/two.bin")) == files[("dbx", "b/two.bin")], "that one stays"
    assert not env.exists("gd", env.qpath(pid, "a/one.bin")) and not env.exists("ic", env.qpath(pid, "c/three.bin"))
    assert [j["detail"]["status"] for j in env.journal(pid, "purge") if j["action"] == "purge"].count("failed") == 1


def test_purge_proves_a_witness_in_another_hash_domain_from_the_recorded_bridge(env):
    pid, data = _quarantined_pair(env, source="gd", witness="dbx")
    assert env.bridges(), "the delete had to download once to prove its witness"
    assert env.purge(pid, older_than_days=0) == ExecResult(pid, "done", done=1)
    assert env.spy.count("download") == 0


def test_purge_downloads_when_no_bridge_links_the_two_hash_domains(env):
    pid, data = _quarantined_pair(env, source="gd", witness="dbx")
    env.conn.execute("DELETE FROM hash_bridges")
    assert env.purge(pid, older_than_days=0) == ExecResult(pid, "done", done=1)
    assert env.spy.count("download") == 1, "one read of the witness proves it against the quarantined copy's hashes"
    assert env.files(quarantine=True) == {} and env.read("dbx", "keep/a.bin") == data


def test_purge_reads_a_hashless_witness(env):
    pid, data = _quarantined_pair(env, source="gd", witness="ic")
    env.conn.execute("DELETE FROM hash_bridges")
    assert env.purge(pid, older_than_days=0).status == "done"
    assert env.spy.paths("download") == [("keep/a.bin",)]


def test_purge_leaves_scratch_files_behind_nowhere(env, tmp_path, monkeypatch):
    pid, data = _quarantined_pair(env, source="gd", witness="dbx")
    env.conn.execute("DELETE FROM hash_bridges")
    scratch = tmp_path / "purge-scratch"
    assert env.purge(pid, older_than_days=0, tmpdir=scratch).status == "done"
    assert list(scratch.iterdir()) == []


def test_purge_refuses_a_witness_that_would_not_survive_a_purge(env):
    """Forged rows (the database is not trusted): a witness inside the quarantine is no live copy."""
    pid, data = _quarantined_pair(env)
    env.put("gd", f"{QDIR}/9/x.bin", data)
    env.conn.execute("UPDATE plan_ops SET witness_path = ? WHERE plan_id = ?", (f"{QDIR}/9/x.bin", pid))
    res = _kept(env, pid, data=data)
    assert "quarantine directory" in res.error


@pytest.mark.parametrize("witness_path", ["../victim.txt", "a//b", "/abs", ""])
def test_purge_refuses_a_witness_path_that_could_leave_the_remote(env, witness_path):
    pid, data = _quarantined_pair(env)
    env.conn.execute("UPDATE plan_ops SET witness_path = ? WHERE plan_id = ?", (witness_path, pid))
    _kept(env, pid, data=data)


def test_purge_refuses_a_delete_that_names_no_witness(env):
    pid, data = _quarantined_pair(env)
    env.conn.execute("UPDATE plan_ops SET witness_remote_id = NULL, witness_path = NULL WHERE plan_id = ?", (pid,))
    res = _kept(env, pid, data=data)
    assert "no surviving copy" in res.error


def test_purge_does_not_look_for_a_witness_when_the_file_is_already_gone(env):
    pid, data = _quarantined_pair(env)
    env.roots["gd"].joinpath(env.qpath(pid, "docs/a.bin")).unlink()
    env.roots["gd"].joinpath("keep/a.bin").unlink()
    assert env.purge(pid, older_than_days=0) == ExecResult(pid, "done", skipped=1)


# --------------------------------------------------------------------------- #
# Who is executing
# --------------------------------------------------------------------------- #

def _lock_row(env, pid):
    row = env.conn.execute("SELECT value FROM meta WHERE key = ?", (f"executing:{pid}",)).fetchone()
    return None if row is None else json.loads(row[0])


def test_execution_holds_a_lock_with_pid_host_and_start_time_and_releases_it(env):
    env.put("gd", "a.bin", blob(20))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "a2.bin"), env.copy("gd", "a.bin", "gd", "a3.bin")])
    assert _lock_row(env, pid) is None
    seen = []
    env.run(pid, progress=lambda done, total, op: seen.append((env.plan_status(pid), _lock_row(env, pid))))
    assert [status for status, _ in seen] == ["executing", "executing"]
    lock = seen[0][1]
    assert lock["pid"] == os.getpid() and lock["host"] == socket.gethostname() and lock["started_at"].endswith("Z")
    assert env.plan_status(pid) == "done" and _lock_row(env, pid) is None


def test_the_lock_is_released_when_an_op_fails(env):
    env.put("gd", "a.bin", blob(20))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "ghost.bin", "gd", "g.bin")])
    assert env.run(pid).status == "failed" and _lock_row(env, pid) is None


def test_the_lock_is_released_when_the_run_is_interrupted(env):
    env.put("gd", "a.bin", blob(20))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "a2.bin"), env.copy("gd", "a.bin", "gd", "a3.bin")])

    def interrupt(done, total, op):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        env.run(pid, progress=interrupt)
    assert env.plan_status(pid) == "failed" and _lock_row(env, pid) is None


def test_a_plan_is_not_left_executing_when_the_run_cannot_even_start(env, monkeypatch):
    env.put("gd", "a.bin", blob(20))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "a2.bin")])

    def no_space(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(executor.tempfile, "TemporaryDirectory", no_space)
    with pytest.raises(OSError):
        env.run(pid)
    assert env.plan_status(pid) == "failed" and _lock_row(env, pid) is None
    monkeypatch.undo()
    assert env.run(pid).status == "done"


def test_a_stale_lock_does_not_stop_a_failed_plan_from_running_again(env):
    env.put("gd", "a.bin", blob(20))
    env.scan("gd")
    pid = env.plan([env.copy("gd", "a.bin", "gd", "a2.bin")])
    plans.set_status(env.conn, pid, "failed")
    env.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (
        f"executing:{pid}", json.dumps({"pid": 1, "host": "gone", "started_at": "2001-01-01T00:00:00.000000Z"})))
    assert env.run(pid).status == "done" and _lock_row(env, pid) is None


def test_a_plan_left_executing_by_a_dead_process_is_reset_and_resumes_where_it_stopped(env):
    for name in ("a", "b", "c"):
        env.put("gd", f"{name}.bin", blob(30, ord(name)))
    env.scan("gd")
    pid = env.plan([env.copy("gd", f"{n}.bin", "gd", f"{n}2.bin") for n in "abc"])
    assert env.run(pid).status == "done"
    # as if the process had been killed during op 3: executing, op 3 unfinished, a lock nobody holds
    (env.roots["gd"] / "c2.bin").unlink()
    env.conn.execute("UPDATE plan_ops SET status = 'pending', error = NULL, executed_at = NULL "
                     "WHERE plan_id = ? AND seq = 3", (pid,))
    plans.set_status(env.conn, pid, "executing")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    env.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (
        f"executing:{pid}", json.dumps({"pid": child.pid, "host": socket.gethostname(),
                                         "started_at": "2024-05-01T10:00:00.000000Z"})))
    with pytest.raises(ExecutionError):
        env.run(pid)                       # still 'executing': it must be reset first
    plans.reset_stuck(env.conn, pid)
    env.spy.calls.clear()
    assert env.run(pid) == ExecResult(pid, "done", done=3)
    assert env.spy.count("copyto") == 1 and env.read("gd", "c2.bin") == blob(30, ord("c"))
    assert _lock_row(env, pid) is None


# --------------------------------------------------------------------------- #
# Whole-flow invariants
# --------------------------------------------------------------------------- #

def _mixed_plan(env):
    """copy+delete across domains, a hashless source, a rename and a same-cloud dedupe."""
    P, N, X, D = blob(900, 1), blob(500, 2), blob(300, 3), blob(200, 4)
    env.put("dbx", "Photos/p.jpg", P)
    env.put("ic", "Notes/n.txt", N)
    env.put("gd", "docs/x.txt", X)
    env.put("gd", "a/dup.txt", D)
    env.put("gd", "a/keep.txt", D)
    env.put("od", "untouched/u.bin", blob(64, 5))
    env.scan("gd", "dbx", "ic", "od")
    pid = env.plan([
        env.copy("dbx", "Photos/p.jpg", "gd", "Archive/p.jpg"),
        env.delete("dbx", "Photos/p.jpg", witness=("gd", "Archive/p.jpg"), depends_on=1),
        env.copy("ic", "Notes/n.txt", "gd", "Archive/n.txt"),
        env.delete("ic", "Notes/n.txt", witness=("gd", "Archive/n.txt"), depends_on=3),
        env.move("gd", "docs/x.txt", "gd", "Archive/x.txt"),
        env.delete("gd", "a/dup.txt", witness=("gd", "a/keep.txt")),
    ])
    return pid, {"P": P, "N": N, "X": X, "D": D}


def test_a_mixed_plan_changes_exactly_what_it_says_and_nothing_else(env):
    pid, d = _mixed_plan(env)
    before = env.tree()
    res = env.run(pid)
    assert res == ExecResult(pid, "done", done=6)
    expected = {k: v for k, v in before.items() if v is not None}
    expected[("gd", "Archive/p.jpg")] = d["P"]
    expected[("gd", "Archive/n.txt")] = d["N"]
    expected[("gd", "Archive/x.txt")] = d["X"]
    del expected[("dbx", "Photos/p.jpg")], expected[("ic", "Notes/n.txt")]
    del expected[("gd", "docs/x.txt")], expected[("gd", "a/dup.txt")]
    expected[("dbx", env.qpath(pid, "Photos/p.jpg"))] = d["P"]
    expected[("ic", env.qpath(pid, "Notes/n.txt"))] = d["N"]
    expected[("gd", env.qpath(pid, "a/dup.txt"))] = d["D"]
    after = env.tree()
    assert {k: v for k, v in after.items() if v is not None} == expected
    dirs = lambda tree: {k for k, v in tree.items() if v is None}  # noqa: E731
    assert dirs(before) <= dirs(after), "no directory was removed either"
    assert env.spy.count("deletefile") == 0
    assert [s for s, _ in env.op_states(pid)] == ["done"] * 6


def test_restore_after_a_mixed_plan_brings_every_original_back(env):
    pid, d = _mixed_plan(env)
    before = env.files()
    env.run(pid)
    assert env.restore(pid) == ExecResult(pid, "done", done=3)
    # The three deleted files are back; the two copies and the rename the plan also made stay as they are.
    expected = dict(before)
    del expected[("gd", "docs/x.txt")]
    expected[("gd", "Archive/p.jpg")] = d["P"]
    expected[("gd", "Archive/n.txt")] = d["N"]
    expected[("gd", "Archive/x.txt")] = d["X"]
    assert env.files() == expected
    assert env.files(quarantine=True) == {}


def test_purge_after_a_mixed_plan_removes_nothing_but_quarantined_files(env):
    pid, d = _mixed_plan(env)
    env.run(pid)
    live_and_quarantine = env.tree()
    assert env.purge(pid) == ExecResult(pid, "done", skipped=3), "nothing is 30 days old yet"
    assert env.tree() == live_and_quarantine
    assert env.purge(pid, older_than_days=0).done == 3
    after = env.tree()
    removed = {k for k in live_and_quarantine if k not in after}
    assert removed == {("dbx", env.qpath(pid, "Photos/p.jpg")), ("ic", env.qpath(pid, "Notes/n.txt")),
                       ("gd", env.qpath(pid, "a/dup.txt"))}
    assert all(after[k] == live_and_quarantine[k] for k in after)


# --------------------------------------------------------------------------- #
# Together with the real simulator
# --------------------------------------------------------------------------- #

def test_a_plan_that_passes_the_real_simulator_executes(env):
    data = blob(2048, 8)
    env.put("gd", "Photos/p.jpg", data)
    env.scan("gd", "dbx")
    pid = env.plan([env.copy("gd", "Photos/p.jpg", "dbx", "Photos/p.jpg"),
                    env.delete("gd", "Photos/p.jpg", witness=("dbx", "Photos/p.jpg"), depends_on=1)],
                   approve=False)
    report = simulate.simulate_plan(env.conn, pid, policy=simulate.SafetyPolicy(quarantine_dir=QDIR))
    assert report.ok, report.violations
    plans.approve_plan(env.conn, pid, plans.short_code(report.digest), approved_by="test")
    res = env.run(pid)
    assert res == ExecResult(pid, "done", done=2)
    assert env.read("dbx", "Photos/p.jpg") == data and env.read("gd", env.qpath(pid, "Photos/p.jpg")) == data
