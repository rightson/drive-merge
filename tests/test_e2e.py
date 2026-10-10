"""End-to-end: four local directories stand in for four clouds, each limited to
its provider's hash domain, and the full flow runs through real rclone:

scan -> identity -> bridge -> consolidate (copy only) -> approve -> execute ->
rescan -> cleanup (delete sources via quarantine) -> execute -> restore.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from drivemerge import db, plans
from drivemerge.config import parse_config
from drivemerge.service import Service

requires_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")

# name -> hash types the "provider" exposes (Drive, Dropbox, OneDrive, iCloud)
CLOUDS = {"gd": ["md5", "sha1", "sha256"], "dbx": ["dropbox"], "od": ["quickxor"], "ic": []}


def _write(root: Path, rel: str, data: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def world(tmp_path):
    roots = {name: tmp_path / name for name in CLOUDS}
    for r in roots.values():
        r.mkdir()
    rnd = __import__("random").Random(7)
    blob = lambda n: bytes(rnd.getrandbits(8) for _ in range(n))  # noqa: E731
    photo, doc, lonely, code = blob(5000), blob(3000), blob(1234), b"print('hi')\n"
    _write(roots["gd"], "Photos/IMG_20190304_101010.jpg", photo)
    _write(roots["gd"], "Old/IMG_20190304_101010.jpg", photo)                 # same-cloud duplicate
    _write(roots["dbx"], "Camera Uploads/IMG_20190304_101010.jpg", photo)     # cross-domain duplicate
    _write(roots["od"], "Pictures/IMG_20190304_101010 (1).jpg", photo)        # cross-domain, copy-style name
    _write(roots["ic"], "Docs/report.pdf", doc)                               # unhashed duplicate ...
    _write(roots["dbx"], "Work/report.pdf", doc)                              # ... of a Dropbox file
    _write(roots["ic"], "Notes/only-here.txt", lonely)                        # unique content, unhashed
    _write(roots["dbx"], "Code/myrepo/.git/HEAD", b"ref: refs/heads/main\n")  # an atomic unit
    _write(roots["dbx"], "Code/myrepo/main.py", code)
    cfg = parse_config({
        "database": {"path": str(tmp_path / "catalog.db")},
        "safety": {"execution_enabled": True},
        "remotes": [
            {"name": n, "provider": "local", "read": str(roots[n]), "write": str(roots[n]), "hash_types": h}
            for n, h in CLOUDS.items()
        ],
    })
    svc = Service(cfg)
    yield svc, roots, {"photo": photo, "doc": doc, "lonely": lonely, "code": code}
    svc.conn.close()


def _approve_and_run(svc: Service, plan_id: int) -> dict:
    plan = plans.get_plan(svc.conn, plan_id, with_ops=False)
    svc.approve(plan_id, plans.short_code(plan.digest), approved_by="test")
    return svc.execute(plan_id)


@requires_rclone
def test_full_flow(world):
    svc, roots, data = world
    for name in CLOUDS:
        assert svc.scan(name)["status"] == "complete"
    svc.rebuild_identity()

    # Before bridging, only same-domain duplicates are proven.
    exact = svc.duplicates()
    assert len(exact) == 1 and {c["remote"] for c in exact[0]["copies"]} == {"gd"}

    bridged = svc.bridge(max_bytes=10**7)
    assert bridged["bridged"] >= 2 and not bridged["failed"]
    exact = svc.duplicates()
    remotes_by_size = {g["size"]: set(g["remotes"]) for g in exact}
    assert remotes_by_size[len(data["photo"])] == {"gd", "dbx", "od"}
    assert remotes_by_size[len(data["doc"])] == {"ic", "dbx"}

    # Consolidate into gd, copy only.
    res = svc.propose_plan("consolidate", {"target": "gd", "target_root": "Archive"}, created_by="test")
    assert res["simulation"]["ok"], res["simulation"]["violations"]
    assert res["simulation"]["op_counts"].get("delete", 0) == 0
    before = {n: _files(r) for n, r in roots.items()}
    out = _approve_and_run(svc, res["plan_id"])
    assert out["status"] == "done", out
    for n in ("dbx", "od", "ic"):
        assert _files(roots[n]) == before[n], "sources must be untouched by a copy-only plan"
    gd_contents = set(_files(roots["gd"]).values())
    for content in data.values():
        assert content in gd_contents, "every distinct content must now exist on the target"
    # the git repo stayed together as a unit
    assert any(p.endswith("myrepo/.git/HEAD") for p in _files(roots["gd"]))

    # Rescan, then clean up the sources: deletes go to quarantine, with witnesses on gd.
    for name in CLOUDS:
        svc.scan(name)
    svc.rebuild_identity()
    svc.bridge(max_bytes=10**7)
    res = svc.propose_plan("consolidate", {"target": "gd", "target_root": "Archive", "delete_sources": True},
                           created_by="test")
    sim = res["simulation"]
    assert sim["ok"], sim["violations"]
    assert sim["contents_lost"] == 0
    assert sim["op_counts"].get("delete", 0) >= 5
    out = _approve_and_run(svc, res["plan_id"])
    assert out["status"] == "done", out
    for n in ("dbx", "od", "ic"):
        live = {k: v for k, v in _files(roots[n]).items() if not k.startswith(".drive-merge-quarantine")}
        assert live == {}, f"{n} should only hold quarantined files, has {sorted(live)}"
    # Restore brings every deleted file back to its original path.
    out = svc.restore(res["plan_id"])
    assert out["status"] in ("done", "restored"), out
    for n in ("dbx", "od", "ic"):
        restored = {k: v for k, v in _files(roots[n]).items() if not k.startswith(".drive-merge-quarantine")}
        assert restored == before[n]


@requires_rclone
def test_stale_catalog_blocks_execution(world):
    svc, roots, data = world
    for name in CLOUDS:
        svc.scan(name)
    svc.rebuild_identity()
    res = svc.propose_plan("dedupe", {"remote": "gd"}, created_by="test")
    assert res["simulation"]["ok"]
    plan = plans.get_plan(svc.conn, res["plan_id"], with_ops=False)
    svc.approve(res["plan_id"], plans.short_code(plan.digest), approved_by="test")
    svc.scan("gd")  # catalog changed after approval
    with pytest.raises(plans.StaleSimulation):
        svc.execute(res["plan_id"])
    assert sorted(_files(roots["gd"])) == ["Old/IMG_20190304_101010.jpg", "Photos/IMG_20190304_101010.jpg"]


@requires_rclone
def test_file_changed_after_scan_stops_execution(world):
    svc, roots, data = world
    for name in CLOUDS:
        svc.scan(name)
    svc.rebuild_identity()
    res = svc.propose_plan("dedupe", {"remote": "gd"}, created_by="test")
    assert res["simulation"]["ok"]
    # The file the plan wants to delete changes on the cloud after the scan.
    plan = plans.get_plan(svc.conn, res["plan_id"])
    victim = roots["gd"] / plan.ops[0].src_path
    victim.write_bytes(os.urandom(len(data["photo"])))
    out = _approve_and_run(svc, res["plan_id"])
    assert out["status"] == "failed"
    assert victim.exists(), "a file that changed since the scan must never be deleted"
    assert db.snapshot(svc.conn)  # catalog still readable
