"""Service facade for the visual workspace: drafts, previews, directory statistics and closing a finished draft.

Local directories stand in for clouds, as in tests/test_e2e.py, so scans, bridging and execution are real.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from drivemerge import builders, db, dirstats, plans, workspace
from drivemerge.config import parse_config
from drivemerge.service import Service, ServiceError

requires_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")

CLOUDS = {"gd": ["md5", "sha1", "sha256"], "dbx": ["dropbox"]}


def _write(root: Path, rel: str, data: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _live(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and not str(p.relative_to(root)).startswith(".drive-merge-quarantine")}


@pytest.fixture
def world(tmp_path):
    roots = {n: tmp_path / n for n in CLOUDS}
    for r in roots.values():
        r.mkdir()
    _write(roots["dbx"], "School/Thesis/draft.docx", b"the same bytes " * 40)
    _write(roots["dbx"], "School/Thesis/refs.bib", b"@book{x}\n" * 9)
    _write(roots["gd"], "Research/Thesis/draft.docx", b"the same bytes " * 40)
    _write(roots["gd"], "Research/Thesis/notes.md", b"notes\n")
    cfg = parse_config({
        "database": {"path": str(tmp_path / "catalog.db")},
        "safety": {"execution_enabled": True},
        "remotes": [{"name": n, "provider": "local", "read": str(roots[n]), "write": str(roots[n]),
                     "hash_types": h} for n, h in CLOUDS.items()],
    })
    svc = Service(cfg)
    yield svc, roots
    svc.conn.close()


def _scan_all(svc: Service) -> None:
    for name in CLOUDS:
        assert svc.scan(name)["status"] == "complete"
    svc.rebuild_identity()
    assert not svc.bridge(max_bytes=10**6)["failed"]


def _move(svc: Service, draft: int, src: str, dst: str, **kw) -> dict:
    (sr, _, sp), (dr, _, dp) = src.partition(":"), dst.partition(":")
    body = {"kind": "move", "items": [{"remote": sr, "path": sp}], "dst": {"remote": dr, "path": dp}, **kw}
    return svc.stage_action(draft, body, created_by="test")


@requires_rclone
def test_scans_identity_and_bridging_keep_directory_statistics_current(world):
    svc, _ = world
    gd = db.remote_id(svc.conn, "gd")
    dbx = db.remote_id(svc.conn, "dbx")
    assert dirstats.get(svc.conn, gd, "") is None

    svc.scan("gd")
    assert dirstats.get(svc.conn, gd, "Research/Thesis")["files"] == 2
    assert dirstats.get(svc.conn, dbx, "") is None, "only the scanned remote is refreshed"

    svc.scan("dbx")
    svc.rebuild_identity()
    # md5 on one side, Dropbox's hash on the other: not provably the same yet
    assert dirstats.get(svc.conn, gd, "Research/Thesis")["dup_files"] == 0
    assert svc.bridge(max_bytes=10**6)["bridged"] >= 1
    assert dirstats.get(svc.conn, gd, "Research/Thesis")["dup_files"] == 1
    assert dirstats.get(svc.conn, dbx, "School/Thesis")["dup_files"] == 1
    assert svc.refresh_dir_stats(["gd"])["remotes"] == 1
    with pytest.raises(KeyError):
        svc.refresh_dir_stats(["nope"])


@requires_rclone
def test_the_draft_facade_stages_undoes_previews_and_lists(world):
    svc, _ = world
    _scan_all(svc)
    d = svc.create_draft("thesis")["id"]
    assert [x["id"] for x in svc.list_drafts()] == [d]
    staged = _move(svc, d, "dbx:School/Thesis", "gd:Research")
    assert [w["code"] for w in staged["warnings"]] == ["MERGE", "CROSS_REMOTE"]
    assert [r["remote"] for r in svc.tree_roots(draft_id=d)] == ["gd", "dbx"]
    kids = svc.list_dir("gd", "Research/Thesis", draft_id=d)["items"]
    assert [k["name"] for k in kids] == ["draft.docx", "notes.md", "refs.bib"]
    assert [k["dedupe"] for k in kids] == [True, False, False]

    assert svc.undo_action(d)["undo_available"] is False
    assert svc.redo_action(d)["redo_available"] is False
    removed = svc.remove_action(d, svc.get_draft(d)["actions"][0]["id"])
    assert removed["removed"]
    _move(svc, d, "dbx:School/Thesis", "gd:Research")

    pv = svc.preview_draft(d)
    assert pv["ok"] and pv["summary"]["dedupes"] == 1
    assert svc.plan(pv["plan_id"])["status"] == "simulated"
    assert [s["kind"] for s in svc.suggestions("related")] == []
    with pytest.raises(ServiceError):
        svc.suggestions("everything")
    svc.discard_draft(d)
    assert svc.list_drafts() == []


@requires_rclone
def test_executing_a_workspace_plan_closes_its_draft(world):
    svc, roots = world
    _scan_all(svc)
    d = svc.create_draft("thesis")["id"]
    _move(svc, d, "dbx:School/Thesis", "gd:Research")
    pv = svc.preview_draft(d)
    assert pv["ok"], pv["simulation"]["violations"]
    plan = plans.get_plan(svc.conn, pv["plan_id"], with_ops=False)
    svc.approve(pv["plan_id"], plans.short_code(plan.digest), approved_by="test")

    result = svc.execute(pv["plan_id"])
    assert result["status"] == "done" and result["draft_applied"] is True
    assert svc.get_draft(d)["status"] == "applied"
    assert _live(roots["gd"])["Research/Thesis/refs.bib"] == b"@book{x}\n" * 9
    assert not [k for k in _live(roots["dbx"]) if k.startswith("School/Thesis")]


@requires_rclone
def test_a_draft_that_changed_after_its_preview_stays_open_when_that_plan_runs(world):
    """Approved from the CLI, say: the run is legitimate, but it does not cover the newer drag."""
    svc, roots = world
    _scan_all(svc)
    d = svc.create_draft("thesis")["id"]
    _move(svc, d, "dbx:School/Thesis", "gd:Research")
    pv = svc.preview_draft(d)
    plan = plans.get_plan(svc.conn, pv["plan_id"], with_ops=False)
    svc.approve(pv["plan_id"], plans.short_code(plan.digest), approved_by="test")
    _move(svc, d, "gd:Research/Thesis/notes.md", "gd:")                 # a later change

    result = svc.execute(pv["plan_id"])
    assert result["status"] == "done"
    assert result["draft_applied"] is False and "changed after" in result["draft_error"]
    assert svc.get_draft(d)["status"] == "open"
    assert _live(roots["gd"])["Research/Thesis/refs.bib"]


@requires_rclone
def test_a_plan_without_a_draft_touches_no_draft(world):
    svc, roots = world
    _write(roots["gd"], "Research/Thesis/Copy of draft.docx", b"the same bytes " * 40)   # a twin on the same cloud
    _scan_all(svc)
    d = svc.create_draft("untouched")["id"]
    res = svc.propose_plan("dedupe", {"remote": "gd"}, created_by="test")
    assert res["plan_id"], res
    plan = plans.get_plan(svc.conn, res["plan_id"], with_ops=False)
    svc.approve(res["plan_id"], plans.short_code(plan.digest), approved_by="test")
    out = svc.execute(res["plan_id"])
    assert "draft_applied" not in out and svc.get_draft(d)["status"] == "open"


def test_builders_are_told_where_quarantine_is(conn, catalog, monkeypatch):
    seen: dict[str, str] = {}

    def spy(name):
        def fake(conn, **kw):
            seen[name] = kw["quarantine_dir"]
            return builders.BuildResult(ops=[], skipped=[], stats={})
        return fake

    cfg = parse_config({"remotes": [{"name": "gd", "provider": "drive", "read": "gd:", "write": "gd:"}],
                        "safety": {"quarantine_dir": "trash"}})
    catalog.remote("gd", "drive")
    svc = Service(cfg, conn=conn)
    for kind in ("dedupe", "consolidate", "reorganize"):
        monkeypatch.setattr(builders, f"build_{kind}", spy(kind))
    svc.propose_plan("dedupe", {}, created_by="t")
    svc.propose_plan("consolidate", {"target": "gd"}, created_by="t")
    svc.propose_plan("reorganize", {"remote": "gd"}, created_by="t")
    assert seen == {"dedupe": "trash", "consolidate": "trash", "reorganize": "trash"}
    assert db.quarantine_dir(conn) == "trash"
    assert svc.policy().quarantine_dir == "trash"


def test_workspace_module_is_the_one_the_facade_calls(conn, catalog):
    catalog.remote("gd", "drive")
    cfg = parse_config({"remotes": [{"name": "gd", "provider": "drive", "read": "gd:", "write": "gd:"}]})
    svc = Service(cfg, conn=conn)
    assert svc.create_draft("x")["status"] == "open"
    assert workspace.list_drafts(conn)[0]["title"] == "x"
