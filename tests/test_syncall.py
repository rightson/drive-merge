from __future__ import annotations

import shutil
import threading

import pytest

from drivemerge import config as cfgmod, db, syncall
from drivemerge.service import Service

needs_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")


def make_config(tmp_path, dirs: dict[str, object]):
    return cfgmod.parse_config({
        "database": {"path": str(tmp_path / "cat.db")},
        "remotes": [{"name": n, "provider": "local", "read": str(p), "write": str(p)} for n, p in dirs.items()],
    })


def fill(root, n, shared=200):
    root.mkdir(parents=True)
    for i in range(n):
        d = root / f"d{i % 20}"
        d.mkdir(exist_ok=True)
        # the first `shared` files have identical content on every remote
        (d / f"f{i}.txt").write_text(f"shared-{i}" if i < shared else f"{root.name}-{i}")


@needs_rclone
def test_three_remotes_parallel_one_missing(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    fill(a, 3000)
    fill(b, 2500)
    cfg = make_config(tmp_path, {"a": a, "b": b, "c": tmp_path / "missing"})
    events, lock = [], threading.Lock()

    def progress(name, state, files, dirs, size):
        with lock:
            events.append((name, state, files))

    res = syncall.sync_all(cfg, progress=progress, max_parallel=3)
    r = res["remotes"]
    assert r["a"]["status"] == "complete" and r["a"]["files"] == 3000 and r["a"]["error"] is None
    assert r["b"]["status"] == "complete" and r["b"]["files"] == 2500
    assert r["c"]["status"] == "failed" and r["c"]["error"]
    assert res["identity"]["duplicate_groups"] >= 200
    states = {(n, s) for n, s, _ in events}
    assert {("a", "queued"), ("a", "listing"), ("a", "done"), ("c", "failed"), ("b", "done")} <= states
    assert max(f for n, s, f in events if n == "a") == 3000

    conn = db.connect(cfg.db_path)
    try:
        n_a = conn.execute("SELECT COUNT(*) FROM entries e JOIN remotes r ON r.id=e.remote_id "
                           "WHERE r.name='a' AND e.is_dir=0 AND e.present=1").fetchone()[0]
        assert n_a == 3000
        assert conn.execute("SELECT COUNT(*) FROM dir_stats").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM scans WHERE status='running'").fetchone()[0] == 0
        failed = conn.execute("SELECT status FROM scans s JOIN remotes r ON r.id=s.remote_id "
                              "WHERE r.name='c'").fetchall()
        assert [x[0] for x in failed] == ["failed"]
    finally:
        conn.close()

    # data age: only complete scans count
    svc = Service(cfg)
    try:
        assert svc.data_age("a")["finished_at"] and svc.data_age("a")["age_seconds"] < 120
        assert svc.data_age("c")["finished_at"] is None
    finally:
        svc.conn.close()


@needs_rclone
def test_subset_and_unknown_remote(tmp_path):
    a = tmp_path / "a"
    fill(a, 10, shared=0)
    cfg = make_config(tmp_path, {"a": a, "b": tmp_path / "missing"})
    res = syncall.sync_all(cfg, remotes=["a"])
    assert list(res["remotes"]) == ["a"] and res["remotes"]["a"]["files"] == 10
    with pytest.raises(KeyError):
        syncall.sync_all(cfg, remotes=["nope"])


def test_no_remotes_and_all_failed_skip_identity(tmp_path):
    cfg = make_config(tmp_path, {})
    assert syncall.sync_all(cfg)["remotes"] == {}
    if shutil.which("rclone"):
        cfg = make_config(tmp_path, {"x": tmp_path / "gone"})
        res = syncall.sync_all(cfg)
        assert res["remotes"]["x"]["status"] == "failed" and res["identity"] is None


@needs_rclone
def test_broken_progress_callback_does_not_fail_sync(tmp_path):
    a = tmp_path / "a"
    fill(a, 5, shared=0)
    cfg = make_config(tmp_path, {"a": a})

    def boom(*args):
        raise RuntimeError("observer bug")

    assert syncall.sync_all(cfg, progress=boom)["remotes"]["a"]["status"] == "complete"
