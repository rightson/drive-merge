"""Service facade for the visual workspace: drafts, previews, directory statistics and closing a finished draft.

Local directories stand in for clouds, as in tests/test_e2e.py, so scans, bridging and execution are real.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from drivemerge import builders, db, dirstats, plans, workspace
from drivemerge.config import parse_config
from drivemerge.models import Plan
from drivemerge.service import Service, ServiceError, draft_of

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


@requires_rclone
def test_running_a_plan_that_only_claims_a_draft_does_not_close_that_draft(world):
    """A proposer's params can say anything; only a plan made by workspace.preview is tied to a draft. Here a
    plan a client labelled with the human's open draft is approved and run: the draft stays open."""
    svc, roots = world
    _scan_all(svc)
    d = svc.create_draft("thesis")["id"]
    _move(svc, d, "dbx:School/Thesis", "gd:Research")                       # staged, never previewed
    version = svc.get_draft(d)["version"]

    for created_by, kind in (("api", "manual"), ("mcp", "manual"), ("api", "dedupe")):
        # Written straight to the store: propose_plan refuses these params, but rows from before that exist,
        # and the guard must not depend on how the plan got there.
        pid = plans.create_plan(svc.conn, "forged", kind, [builders.build_manual(svc.conn, [
            {"op": "copy", "src": "gd:Research/Thesis/notes.md", "dst": f"gd:Research/{created_by}-{kind}.md"},
        ]).ops[0]], created_by=created_by, params={"draft_id": d, "draft_version": version})
        assert svc.simulate(pid)["ok"]
        svc.approve(pid, plans.short_code(plans.get_plan(svc.conn, pid, with_ops=False).digest), approved_by="test")
        result = svc.execute(pid)
        assert result["status"] == "done" and "draft_applied" not in result, (created_by, kind, result)
        assert svc.get_draft(d)["status"] == "open"
    assert _live(roots["gd"])["Research/mcp-manual.md"] == b"notes\n"
    assert any(k.startswith("School/Thesis") for k in _live(roots["dbx"])), "the human's staged move never ran"


@requires_rclone
def test_only_the_drafts_own_preview_closes_it(world):
    """The positive control: an unrelated plan finishing does not touch the draft, its own preview does."""
    svc, _ = world
    _scan_all(svc)
    d = svc.create_draft("thesis")["id"]
    _move(svc, d, "dbx:School/Thesis", "gd:Research")
    pv = svc.preview_draft(d)
    other = svc.propose_plan("manual", {"ops": [{"op": "copy", "src": "gd:Research/Thesis/notes.md",
                                                 "dst": "gd:Research/notes-copy.md"}]}, created_by="api")
    for pid in (other["plan_id"], pv["plan_id"]):
        svc.approve(pid, plans.short_code(plans.get_plan(svc.conn, pid, with_ops=False).digest), approved_by="test")
    assert "draft_applied" not in svc.execute(other["plan_id"]) and svc.get_draft(d)["status"] == "open"
    assert svc.execute(pv["plan_id"])["draft_applied"] is True and svc.get_draft(d)["status"] == "applied"


@pytest.mark.parametrize("kind", ["dedupe", "consolidate", "reorganize", "manual"])
@pytest.mark.parametrize("claim", [{"draft_id": 1}, {"draft_version": 1}, {"draft_id": 1, "draft_version": 1}])
def test_propose_plan_refuses_params_that_claim_a_draft(conn, catalog, kind, claim):
    catalog.remote("gd", "drive")
    cfg = parse_config({"remotes": [{"name": "gd", "provider": "drive", "read": "gd:", "write": "gd:"}]})
    svc = Service(cfg, conn=conn)
    with pytest.raises(ServiceError, match="reserved for workspace previews"):
        svc.propose_plan(kind, {"ops": [], "target": "gd", "remote": "gd", **claim}, created_by="api")
    assert plans.list_plans(conn) == []


def test_draft_of_follows_only_plans_that_workspace_preview_made(conn):
    params = {"draft_id": 5, "draft_version": 2}

    def plan(kind, created_by, p=params):
        return Plan(id=1, title="t", kind=kind, status="draft", created_by=created_by, params=p, digest=None,
                    snapshot=None, created_at="")

    assert draft_of(plan("workspace", "workspace")) == 5
    assert draft_of(plan("workspace", "workspace", {})) is None
    for kind, created_by in (("manual", "api"), ("manual", "workspace"), ("workspace", "api"), ("dedupe", "mcp")):
        assert draft_of(plan(kind, created_by)) is None, (kind, created_by)


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


# --------------------------------------------------------------------------- the rules a plan was built with (docs/RULES.md D)

from drivemerge import classify, rulestore  # noqa: E402
from drivemerge import service as service_mod  # noqa: E402

_RULES = '[[rule]]\nname = "docs"\ncategory = ["pdf"]\ntarget = "Docs/{name}"\n'


def _rules_service(conn, catalog, tmp_path, *, rules_text=None):
    from drivemerge.config import Config, RemoteConfig
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("")
    if rules_text is not None:
        (cfg_dir / "rules.toml").write_text(rules_text)
    gd = catalog.remote("gd", "drive")
    db_ = catalog.remote("db", "dropbox")
    catalog.file(gd, "Scans/a.pdf", size=10, md5="aa")
    catalog.file(db_, "Misc/b.pdf", size=20, md5="bb")
    cfg = Config(config_path=cfg_dir / "config.toml", remotes=[RemoteConfig("gd", "drive", "gd:", "gd:"),
                                                              RemoteConfig("db", "dropbox", "db:", "db:")])
    return Service(cfg, conn=conn), cfg_dir


@pytest.mark.parametrize("kind, params", [("consolidate", {"target": "gd"}), ("reorganize", {"remote": "gd"})])
def test_rules_ref_is_reserved_like_a_draft_id(conn, catalog, tmp_path, kind, params):
    svc, _ = _rules_service(conn, catalog, tmp_path)
    assert "rules_ref" in service_mod.RESERVED_PARAMS
    with pytest.raises(ServiceError, match="rules_ref"):
        svc.propose_plan(kind, {**params, "rules_ref": {"source": "file"}}, created_by="api")
    with pytest.raises(ServiceError, match="rules_ref"):
        svc.estimate_plan(kind, {**params, "rules_ref": {}})
    assert plans.list_plans(conn) == []


def test_only_consolidate_and_reorganize_plans_record_rules(conn, catalog, tmp_path):
    svc, _ = _rules_service(conn, catalog, tmp_path, rules_text=_RULES)
    for kind, params in (("consolidate", {"target": "gd"}), ("reorganize", {"remote": "gd"})):
        res = svc.propose_plan(kind, params, created_by="t")
        assert res["plan_id"] is not None, (kind, res)
        p = plans.get_plan(conn, res["plan_id"]).params
        assert p["rules"] == _RULES
        assert p["rules_ref"] == {"source": "file", "version": rulestore.active(conn, svc.config).version_id,
                                  "sha256": rulestore.sha256_of(_RULES), "path": str(tmp_path / "cfg" / "rules.toml")}
    assert svc.propose_plan("dedupe", {}, created_by="t")["code"] == "NOTHING_TO_DO"
    manual = svc.propose_plan("manual", {"ops": [{"op": "copy", "src": "gd:Scans/a.pdf", "dst": "gd:Copy/a.pdf",
                                                 "reason": "x"}]}, created_by="t")
    assert not {"rules", "rules_ref"} & set(plans.get_plan(conn, manual["plan_id"]).params)


def test_the_builder_gets_the_resolved_rules_and_an_edit_is_observed_before_the_build(conn, catalog, tmp_path, monkeypatch):
    svc, cfg_dir = _rules_service(conn, catalog, tmp_path, rules_text=_RULES)
    seen = {}

    def spy(conn_, **kw):
        seen["rules"] = [r.name for r in kw["ruleset"].rules]
        seen["observed"] = conn_.execute("SELECT COUNT(*) FROM rule_versions WHERE created_by = 'file'").fetchone()[0]
        return builders.BuildResult(ops=[], skipped=[], stats={})

    monkeypatch.setattr(builders, "build_reorganize", spy)
    assert svc.propose_plan("reorganize", {"remote": "gd"}, created_by="t")["code"] == "NOTHING_TO_DO"
    assert seen == {"rules": ["docs"], "observed": 1}                         # the outside file was in the history first
    v = svc.save_rule_version(_RULES + '[[rule]]\nname = "more"\next = ["txt"]\ntarget = "T/{name}"\n', created_by="mcp")
    svc.propose_plan("reorganize", {"remote": "gd", "rules_version": v["id"]}, created_by="t")
    assert seen["rules"] == ["docs", "more"]
    svc.propose_plan("reorganize", {"remote": "gd", "rules": '[[rule]]\nname = "x"\ntarget = "X"\n'}, created_by="t")
    assert seen["rules"] == ["x"]


def test_estimate_plan_writes_nothing_and_does_not_observe(conn, catalog, tmp_path):
    svc, _ = _rules_service(conn, catalog, tmp_path, rules_text=_RULES)
    before = conn.total_changes
    est = svc.estimate_plan("consolidate", {"target": "gd"})
    assert conn.total_changes == before and est["rules_ref"]["source"] == "file" and est["rules_ref"]["version"] is None
    assert conn.execute("SELECT COUNT(*) FROM rule_versions").fetchone()[0] == 0


def test_plan_summaries_carry_rules_ref_but_not_the_rules_text(conn, catalog, tmp_path):
    svc, _ = _rules_service(conn, catalog, tmp_path, rules_text=_RULES)
    pid = svc.propose_plan("reorganize", {"remote": "gd"}, created_by="t")["plan_id"]
    (row,) = [r for r in svc.plan_summaries() if r["id"] == pid]
    assert "rules" not in row["params"] and row["params"]["rules_ref"]["source"] == "file"
    assert plans.get_plan(conn, pid).params["rules"] == _RULES               # the full plan still has the text
    assert svc.plan(pid)["params"]["rules"] == _RULES


def test_a_missing_configured_file_stops_rule_plans_but_not_the_others(conn, catalog, tmp_path):
    svc, _ = _rules_service(conn, catalog, tmp_path)
    svc.config.rules_path = str(tmp_path / "nowhere.toml")
    for kind, params in (("consolidate", {"target": "gd"}), ("reorganize", {"remote": "gd"})):
        with pytest.raises(rulestore.RulesError) as exc:
            svc.propose_plan(kind, params, created_by="t")
        assert exc.value.code == "RULES_FILE_MISSING"
    assert svc.propose_plan("dedupe", {}, created_by="t")["code"] == "NOTHING_TO_DO"
    assert svc.validate_rules(_RULES)["ok"] is True
    with pytest.raises(rulestore.RulesError):
        svc.profiles()
    assert classify.default_rules().rules                                       # the packaged rules are still there to ask for
