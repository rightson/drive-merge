"""Backend of the Plans view (docs/PLANS.md): estimates, summaries, review, cancel, coded build errors and bridging.

Real service over a file-backed catalog (jobs open their own connection). Clouds are catalog rows only; the
bridge job runs against a fake download.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from drivemerge import api, builders, db, identity, plans, ui_api
from drivemerge.config import ApiConfig, Config, RemoteConfig, SafetyConfig
from drivemerge.models import Op
from drivemerge.service import Service, ServiceError

API = "api-token"
UI = "ui-token-one-time"
AUTH = {"Authorization": f"Bearer {API}"}
SECRETS = ("digest", "approval_code", "snapshot")


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "catalog.db")
    yield c
    c.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        db_path=str(tmp_path / "catalog.db"),
        remotes=[RemoteConfig("gdrive", "drive", "gdrive:", "gdrive:"),
                 RemoteConfig("dropbox", "dropbox", "dropbox:", "dropbox:")],
        safety=SafetyConfig(execution_enabled=True),
        api=ApiConfig(token=API),
    )


@pytest.fixture
def service(conn, catalog, config):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    # one content three times on gdrive, another twice, and singletons elsewhere
    for path in ("Docs/report.pdf", "Docs/Copy of report.pdf", "Old/report.pdf"):
        catalog.file(g, path, size=1000, sha256="aa")
    for path in ("Photos/a.jpg", "Photos/a (1).jpg"):
        catalog.file(g, path, size=50, sha256="bb")
    catalog.file(g, "Docs/only-here.txt", size=7, sha256="cc")
    catalog.file(d, "Docs/budget.pdf", size=500, dropbox="dd")
    identity.rebuild_groups(conn)
    return Service(config, conn=conn)


def _app(service, config, *, approvals=True):
    return ui_api.create_ui_app(service, config=config, api_token=API, ui_token=UI, approvals_enabled=approvals)


def _login(app):
    client = TestClient(app)
    r = client.post("/v1/ui/session", json={"token": UI})
    assert r.status_code == 200, r.text
    return client, {"X-CSRF-Token": r.json()["csrf"]}


@pytest.fixture
def app(service, config):
    return _app(service, config)


@pytest.fixture
def bearer(app):
    return TestClient(app, headers=AUTH)


@pytest.fixture
def browser(app):
    return _login(app)


@pytest.fixture
def plain(service):
    """The bearer-only REST app (`drivemerge serve`)."""
    return TestClient(api.create_app(service, token=API), headers=AUTH)


def _rid(conn, name):
    return conn.execute("SELECT id FROM remotes WHERE name = ?", (name,)).fetchone()[0]


def _dedupe(service, **params):
    res = service.propose_plan("dedupe", {"remote": "gdrive", **params}, created_by="test")
    assert res["plan_id"], res
    return res["plan_id"]


def _secret_keys(value):
    if isinstance(value, dict):
        return ({k for k in SECRETS if k in value}).union(*map(_secret_keys, value.values()))
    if isinstance(value, list):
        return set().union(*map(_secret_keys, value))
    return set()


# ----------------------------------------------------------------- builders through the service

def test_estimate_persists_nothing(service, conn):
    before = conn.total_changes
    est = service.estimate_plan("dedupe", {"remote": "gdrive"})
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plan_ops").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0] == 0
    est = service.estimate_plan("consolidate", {"target": "dropbox"})
    assert conn.total_changes == before and est["ops"] > 0


def test_dedupe_estimate_shape(service):
    est = service.estimate_plan("dedupe", {"remote": "gdrive"}, sample=2)
    assert est["kind"] == "dedupe" and est["ops"] == 3 and est["nothing_to_do"] is False
    assert est["stats"]["bytes_reclaimed"] == 2 * 1000 + 50 and est["stats"]["copies_considered"] == 5
    assert len(est["sample"]) == 2
    op = est["sample"][0]
    assert op["op"] == "delete" and op["src"]["remote"] == "gdrive" and op["witness"]["remote"] == "gdrive"
    assert "dst" not in op and op["size"] == 1000 and op["reason"].startswith("duplicate of gdrive:")
    assert est["skipped"] == [] and est["groups_total"] == 2
    big, small = est["groups"]
    assert big["size"] == 1000 and big["reclaim"] == 2000 and big["skipped"] is False
    assert big["key"].startswith("g:")
    assert [c["path"] for c in big["copies"] if c["keep"]] == ["Docs/report.pdf"]
    assert sum(c["remove"] for c in big["copies"]) == 2
    assert set(big["copies"][0]) == {"entry_id", "remote", "path", "mtime", "keep", "remove", "usable", "why_not"}
    assert small["reclaim"] == 50
    assert service.estimate_plan("dedupe", {"remote": "gdrive"}, sample=0)["sample"] == []


def test_dedupe_estimate_pages_its_groups_and_never_hands_the_paging_to_the_builder(service):
    first = service.estimate_plan("dedupe", {"remote": "gdrive", "group_limit": 1})
    second = service.estimate_plan("dedupe", {"remote": "gdrive", "group_offset": 1, "group_limit": 1})
    assert [len(first["groups"]), len(second["groups"])] == [1, 1]
    assert first["groups"][0]["key"] != second["groups"][0]["key"]
    assert (first["group_offset"], first["group_limit"], first["groups_total"]) == (0, 1, 2)
    assert first["ops"] == second["ops"] == 3                       # the ops are for the whole build
    assert service.estimate_plan("dedupe", {"remote": "gdrive", "group_offset": 5})["groups"] == []
    for bad in ({"group_offset": -1}, {"group_limit": 0}, {"group_limit": "x"}, {"group_offset": True}):
        with pytest.raises(ServiceError):
            service.estimate_plan("dedupe", {"remote": "gdrive", **bad})
    with pytest.raises(TypeError):                                  # a proposal does not take them
        service.propose_plan("dedupe", {"group_offset": 1}, created_by="t")


def test_estimate_shows_overrides_and_skips(service):
    est = service.estimate_plan("dedupe", {"remote": "gdrive"})
    big = est["groups"][0]
    victim = next(c for c in big["copies"] if c["remove"])
    est2 = service.estimate_plan("dedupe", {"remote": "gdrive", "keep": {big["key"]: victim["entry_id"]},
                                            "skip_groups": [est["groups"][1]["key"]]})
    assert est2["ops"] == 2 and est2["groups"][1]["skipped"] is True
    assert [c["entry_id"] for c in est2["groups"][0]["copies"] if c["keep"]] == [victim["entry_id"]]
    with pytest.raises(builders.BuildError) as ei:
        service.estimate_plan("dedupe", {"remote": "gdrive", "keep": {big["key"]: 10 ** 6}})
    assert ei.value.code == "BAD_KEEPER"


def test_estimate_refuses_what_a_proposal_refuses(service):
    with pytest.raises(ServiceError, match="unknown plan kind"):
        service.estimate_plan("teleport", {})
    with pytest.raises(ServiceError, match="reserved"):
        service.estimate_plan("dedupe", {"draft_id": 1})
    with pytest.raises(ServiceError):
        service.estimate_plan("dedupe", {}, sample=-1)


def test_consolidate_estimate_reports_the_numbers_the_page_shows(service):
    est = service.estimate_plan("consolidate", {"target": "dropbox"})
    st = est["stats"]
    assert st["to_copy"] == 3 and st["already_on_target"] == 1
    assert st["bytes_to_copy"] == st["bytes_cross_remote"] == 1000 + 50 + 7
    assert {"moves_on_target", "deletes", "skipped_by_reason"} <= set(st)
    assert all(o["op"] == "copy" and o["dst"]["remote"] == "dropbox" for o in est["sample"])
    assert "groups" not in est


def test_nothing_to_do_has_a_code_in_both_the_proposal_and_the_estimate(service, conn):
    res = service.propose_plan("dedupe", {"remote": "dropbox"}, created_by="test")
    assert res["plan_id"] is None and res["message"] == "nothing to do" and res["code"] == "NOTHING_TO_DO"
    assert service.estimate_plan("dedupe", {"remote": "dropbox"})["nothing_to_do"] is True
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    made = service.propose_plan("dedupe", {"remote": "gdrive"}, created_by="test")
    assert "code" not in made


def test_plan_summaries(service, conn, catalog):
    ok_id = _dedupe(service)
    bad_id = plans.create_plan(conn, "gone", "manual", [
        Op("copy", _rid(conn, "gdrive"), "Missing/file.txt", dst_remote_id=_rid(conn, "dropbox"),
           dst_path="Missing/file.txt")], created_by="mcp", params={"digest": "x" * 64, "note": "n"})
    service.simulate(bad_id)
    never = plans.create_plan(conn, "never run", "manual", [
        Op("copy", _rid(conn, "gdrive"), "Docs/only-here.txt", dst_remote_id=_rid(conn, "dropbox"),
           dst_path="Docs/only-here.txt")], created_by="api")
    rows = {r["id"]: r for r in service.plan_summaries()}
    assert list(rows) == [never, bad_id, ok_id]                               # newest first
    good, bad, none = rows[ok_id], rows[bad_id], rows[never]
    assert set(good) == {"id", "kind", "title", "status", "created_by", "created_at", "simulated_at", "approved_at",
                         "finished_at", "ops", "ok", "problems", "params", "draft_id", "outdated"}
    assert (good["ok"], good["problems"], good["ops"], good["status"], good["outdated"]) == (True, 0, 3, "simulated", False)
    assert (bad["ok"], bad["status"], bad["created_by"]) == (False, "draft", "mcp") and bad["problems"] >= 1
    assert "digest" not in bad["params"] and bad["params"] == {"note": "n"}
    assert (none["ok"], none["problems"], none["outdated"], none["ops"]) == (None, 0, False, 1)
    assert good["params"] == {"remote": "gdrive"} and good["draft_id"] is None
    assert [r["id"] for r in service.plan_summaries(status="simulated")] == [ok_id]
    assert len(service.plan_summaries(limit=1)) == 1
    # a newer complete scan outdates every plan that was simulated before it
    scan = conn.execute("INSERT INTO scans(remote_id, status, started_at, finished_at) VALUES (?, 'complete', ?, ?) "
                        "RETURNING id", (_rid(conn, "gdrive"), db.utcnow(), db.utcnow())).fetchone()[0]
    assert scan
    rows = {r["id"]: r for r in service.plan_summaries()}
    assert rows[ok_id]["outdated"] is True and rows[bad_id]["outdated"] is True and rows[never]["outdated"] is False
    service.simulate(ok_id)
    assert {r["id"]: r for r in service.plan_summaries()}[ok_id]["outdated"] is False
    plans.cancel_plan(conn, ok_id)
    assert {r["id"]: r for r in service.plan_summaries()}[ok_id]["outdated"] is False      # closed plans are history


def test_a_workspace_preview_links_its_draft_in_the_summary(service, conn):
    pid = plans.create_plan(conn, "preview", "workspace", [], created_by="workspace",
                            params={"draft_id": 4, "draft_version": 2})
    assert {r["id"]: r for r in service.plan_summaries()}[pid]["draft_id"] == 4


# ----------------------------------------------------------------- estimate and summaries over HTTP

@pytest.mark.parametrize("which", ["ui", "plain"])
def test_estimate_and_summaries_routes(which, bearer, plain, service, conn):
    client = bearer if which == "ui" else plain
    before = conn.total_changes
    r = client.post("/v1/plans/estimate", json={"kind": "dedupe", "params": {"remote": "gdrive", "group_limit": 1},
                                                "sample": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ops"] == 3 and len(body["sample"]) == 1 and len(body["groups"]) == 1
    assert not _secret_keys(body)
    assert conn.total_changes == before and conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert client.post("/v1/plans/estimate", json={"kind": "dedupe", "sample": -1}).status_code == 422
    assert client.post("/v1/plans/estimate", json={"kind": "dedupe", "extra": 1}).status_code == 422
    assert client.post("/v1/plans/estimate", json={"kind": "teleport"}).status_code == 400
    _dedupe(service)
    r = client.get("/v1/plans/summaries")
    assert r.status_code == 200 and [p["kind"] for p in r.json()["plans"]] == ["dedupe"]
    assert r.json()["plans"][0]["ok"] is True and not _secret_keys(r.json())
    assert client.get("/v1/plans/summaries", params={"status": "done"}).json() == {"plans": []}
    assert client.get("/v1/plans/summaries", params={"limit": 0}).status_code == 422
    assert client.get("/v1/plans/1").status_code == 200                # the {plan_id} route still works


@pytest.mark.parametrize("path,method", [("/v1/plans/estimate", "POST"), ("/v1/plans/summaries", "GET")])
def test_new_plan_routes_need_credentials_in_both_apps(path, method, app, service):
    for target in (TestClient(app), TestClient(api.create_app(service, token=API))):
        assert target.request(method, path, json={"kind": "dedupe"}).status_code == 401
        assert target.request(method, path, json={"kind": "dedupe"},
                              headers={"Authorization": "Bearer nope"}).status_code == 401


def test_a_plan_is_created_by_ui_for_a_session_and_api_for_the_token(browser, bearer, plain, service, conn):
    body = {"kind": "dedupe", "params": {"remote": "gdrive"}}
    session_made = browser[0].post("/v1/plans", json=body, headers=browser[1])
    bearer_made = bearer.post("/v1/plans", json=body)
    plain_made = plain.post("/v1/plans", json=body)
    assert [r.status_code for r in (session_made, bearer_made, plain_made)] == [201, 201, 201]
    made = [r.json()["plan_id"] for r in (session_made, bearer_made, plain_made)]
    assert [plans.get_plan(conn, p, with_ops=False).created_by for p in made] == ["ui", "api", "api"]
    assert not any(_secret_keys(r.json()) for r in (session_made, bearer_made, plain_made))
    rows = {r["id"]: r["created_by"] for r in bearer.get("/v1/plans/summaries").json()["plans"]}
    assert rows == dict(zip(made, ["ui", "api", "api"]))


# ----------------------------------------------------------------- coded build errors

@pytest.mark.parametrize("which", ["ui", "plain"])
def test_build_errors_are_answered_coded_in_both_apps(which, bearer, plain):
    client = bearer if which == "ui" else plain
    r = client.post("/v1/plans", json={"kind": "consolidate", "params": {"target": "nowhere"}})
    assert r.status_code == 400
    assert r.json() == {"error": "unknown remote 'nowhere'", "code": "UNKNOWN_REMOTE", "params": {"remote": "nowhere"}}
    r = client.post("/v1/plans/estimate", json={"kind": "manual", "params": {"ops": [{"op": "copy", "src": "bad"}]}})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (400, "BAD_LOCATION", {"location": "bad"})
    r = client.post("/v1/plans/estimate", json={"kind": "dedupe", "params": {"skip_groups": ["g:99999"]}})
    assert r.json() == {"error": "g:99999 is not a duplicate group of this plan", "code": "BAD_GROUP",
                        "params": {"group": "g:99999"}}


def test_a_read_only_target_is_coded(config, tmp_path, conn, catalog):
    catalog.remote("gdrive", "drive")
    catalog.remote("vault", "drive", writable=False)
    client = TestClient(_app(Service(config, conn=conn), config), headers=AUTH)
    r = client.post("/v1/plans/estimate", json={"kind": "consolidate", "params": {"target": "vault"}})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (400, "READ_ONLY", {"remote": "vault"})


# ----------------------------------------------------------------- review

def _review(client, pid, **kw):
    return client.post(f"/v1/ui/plans/{pid}/review", **kw)


def test_review_hands_the_code_only_to_an_approving_session(service, config, conn):
    pid = _dedupe(service)
    app = _app(service, config)
    session, headers = _login(app)
    r = _review(session, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    code = plans.short_code(plans.get_plan(conn, pid, with_ops=False).digest)
    assert body["approval_code"] == code
    assert body["simulation"]["ok"] is True and body["status"] == "simulated" and body["ops_total"] == 3
    assert body["stale_reason"] is None
    assert not (_secret_keys({k: v for k, v in body.items() if k != "approval_code"}))
    assert "digest" not in r.text.replace("approval_code", "")
    assert code not in json.dumps({k: v for k, v in body.items() if k != "approval_code"})
    # ... and the code it gave is the one the human channel accepts
    assert session.post(f"/v1/ui/plans/{pid}/approve", headers=headers, json={"code": code}).status_code == 200
    assert plans.get_plan(conn, pid, with_ops=False).status == "approved"


def test_review_never_gives_the_code_to_the_token_or_a_session_that_may_not_approve(service, config, conn):
    pid = _dedupe(service)
    code = plans.short_code(plans.get_plan(conn, pid, with_ops=False).digest)
    digest = plans.get_plan(conn, pid, with_ops=False).digest
    viewers = {
        "bearer": TestClient(_app(service, config), headers=AUTH),
        "session without approvals": _login(_app(service, config, approvals=False))[0],
        "bearer without approvals": TestClient(_app(service, config, approvals=False), headers=AUTH),
    }
    for name, client in viewers.items():
        r = _review(client, pid)
        assert r.status_code == 200, name
        assert "approval_code" not in r.json() and not _secret_keys(r.json()), name
        assert code not in r.text and digest not in r.text, name
        assert r.json()["simulation"]["ok"] is True and r.json()["ops"], name


def test_review_has_no_code_for_a_plan_that_does_not_pass(service, config, conn):
    pid = plans.create_plan(conn, "gone", "manual", [
        Op("copy", _rid(conn, "gdrive"), "Missing/file.txt", dst_remote_id=_rid(conn, "dropbox"),
           dst_path="Missing/file.txt")], created_by="mcp")
    session, _ = _login(_app(service, config))
    body = _review(session, pid).json()
    assert body["simulation"]["ok"] is False and body["status"] == "draft"
    assert "approval_code" not in body and body["simulation"]["violations"]


def test_review_has_no_code_for_a_stale_workspace_preview(service, config, conn):
    draft = conn.execute("INSERT INTO drafts(title, status, version, created_at, updated_at) "
                         "VALUES ('d', 'open', 2, ?, ?) RETURNING id", (db.utcnow(), db.utcnow())).fetchone()[0]
    pid = plans.create_plan(conn, "preview", "workspace", [
        Op("copy", _rid(conn, "gdrive"), "Docs/only-here.txt", dst_remote_id=_rid(conn, "dropbox"),
           dst_path="Docs/only-here.txt")], created_by="workspace",
        params={"draft_id": draft, "draft_version": 2})
    session, _ = _login(_app(service, config))
    fresh = _review(session, pid).json()
    assert fresh["approval_code"] and fresh["stale_reason"] is None
    conn.execute("UPDATE drafts SET version = 3 WHERE id = ?", (draft,))
    stale = _review(session, pid).json()
    assert "approval_code" not in stale and "changed after this preview" in stale["stale_reason"]
    assert stale["simulation"]["ok"] is True


def test_review_simulates_again_but_keeps_a_current_approval(service, config, conn):
    pid = _dedupe(service)
    session, headers = _login(_app(service, config))
    code = _review(session, pid).json()["approval_code"]
    session.post(f"/v1/ui/plans/{pid}/approve", headers=headers, json={"code": code})
    again = _review(session, pid)
    assert again.status_code == 200 and again.json()["status"] == "approved"
    assert "approval_code" not in again.json() and again.json()["simulation"]["ok"] is True
    # when the catalog moved on, the approval was void anyway: the plan is simulated again and needs a new code
    conn.execute("INSERT INTO scans(remote_id, status, started_at, finished_at) VALUES (?, 'complete', ?, ?)",
                 (_rid(conn, "gdrive"), db.utcnow(), db.utcnow()))
    moved = _review(session, pid).json()
    assert moved["status"] == "simulated" and moved["approval_code"]


@pytest.mark.parametrize("status", ["executing", "done", "failed", "cancelled"])
def test_review_and_cancel_refuse_a_plan_that_is_closed(status, service, config, conn):
    pid = _dedupe(service)
    plans.set_status(conn, pid, status)
    session, headers = _login(_app(service, config))
    for client in (session, TestClient(_app(service, config), headers=AUTH)):
        r = _review(client, pid)
        assert r.status_code == 409
        assert r.json()["code"] == "PLAN_CLOSED" and r.json()["params"] == {"plan": pid, "plan_status": status}
    if status != "cancelled":
        r = session.post(f"/v1/ui/plans/{pid}/cancel")
        assert r.status_code == 409 and r.json()["params"] == {"plan": pid, "plan_status": status}
        assert plans.get_plan(conn, pid, with_ops=False).status == status


def test_cancel(service, config, conn):
    pid = _dedupe(service)
    session, _ = _login(_app(service, config))
    r = session.post(f"/v1/ui/plans/{pid}/cancel")
    assert (r.status_code, r.json()) == (200, {"ok": True})
    assert plans.get_plan(conn, pid, with_ops=False).status == "cancelled"
    assert session.post("/v1/ui/plans/999/cancel").status_code == 404
    assert session.post("/v1/ui/plans/999/review").status_code == 404
    other = _dedupe(service)
    bearer = TestClient(_app(service, config), headers=AUTH)
    assert bearer.post(f"/v1/ui/plans/{other}/cancel").json() == {"ok": True}      # a normal route, like the REST one
    for path in ("review", "cancel"):
        assert TestClient(_app(service, config)).post(f"/v1/ui/plans/{other}/{path}").status_code == 401


def test_review_reports_the_plan_with_a_bounded_op_list(service, config):
    pid = _dedupe(service)
    session, _ = _login(_app(service, config))
    body = _review(session, pid, params={"ops_limit": 1}).json()
    assert len(body["ops"]) == 1 and body["ops_total"] == 3
    assert _review(session, pid, params={"ops_limit": -1}).status_code == 422


def test_no_new_route_leaks_a_secret_at_any_depth(service, config, conn):
    """Every response of the new routes, for every kind of caller, is free of digest, snapshot and approval_code
    (the single exception: `approval_code` in a session's own review of a passing plan)."""
    pid = _dedupe(service)
    digest = plans.get_plan(conn, pid, with_ops=False).digest
    app = _app(service, config)
    callers = {"bearer": TestClient(app, headers=AUTH), "approving": _login(app)[0],
               "reader": _login(_app(service, config, approvals=False))[0],
               "plain": TestClient(api.create_app(service, token=API), headers=AUTH)}
    for name, client in callers.items():
        responses = [client.post("/v1/plans/estimate", json={"kind": "dedupe", "params": {"remote": "gdrive"}}),
                     client.get("/v1/plans/summaries"), client.get("/v1/plans"), client.get(f"/v1/plans/{pid}"),
                     client.post("/v1/plans", json={"kind": "dedupe", "params": {"remote": "gdrive"}})]
        if name != "plain":
            responses.append(_review(client, pid))
        for r in responses:
            assert r.status_code in (200, 201), (name, r.request.url, r.text)
            body = r.json()
            if name == "approving" and r.request.url.path.endswith("/review"):
                body = {k: v for k, v in body.items() if k != "approval_code"}
            assert not _secret_keys(body), (name, r.request.url)
            assert digest not in r.text and plans.short_code(digest) not in json.dumps(body), (name, r.request.url)


# ----------------------------------------------------------------- bridging

class _Download:
    """Stands in for identity.bridge_entry: records the entry, can block, can fail."""

    def __init__(self, monkeypatch):
        self.entries, self.gate, self.fail = [], threading.Event(), set()
        self.gate.set()
        monkeypatch.setattr(identity, "bridge_entry", self)
        monkeypatch.setattr(identity, "rebuild_groups", lambda conn: {"groups": 1})

    def __call__(self, conn, rclone, entry_id, tmp):
        assert self.gate.wait(10)
        self.entries.append(entry_id)
        if entry_id in self.fail:
            raise RuntimeError("download failed")


@pytest.fixture
def candidates(monkeypatch):
    found = [identity.BridgeCandidate(i, "icloud", f"f{i}", 100 * i, 1, "size") for i in (1, 2, 3)]
    monkeypatch.setattr(identity, "bridge_candidates",
                        lambda conn, *, max_bytes=None, min_size=1, limit=100:
                        [c for c in found if max_bytes is None or c.size <= max_bytes][:limit])
    return found


def _wait(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("bridge job still running")


def test_bridge_candidates_route(bearer, browser, candidates):
    for client in (bearer, browser[0]):
        assert client.get("/v1/ui/bridge/candidates").json() == {"count": 3, "bytes": 600}
        assert client.get("/v1/ui/bridge/candidates", params={"max_bytes": 250}).json() == {"count": 2, "bytes": 300}
        assert client.get("/v1/ui/bridge/candidates", params={"limit": 1}).json() == {"count": 1, "bytes": 100}
    assert bearer.get("/v1/ui/bridge/candidates", params={"limit": 0}).status_code == 422
    assert bearer.get("/v1/ui/bridge/candidates", params={"max_bytes": -1}).status_code == 422
    assert TestClient(bearer.app).get("/v1/ui/bridge/candidates").status_code == 401


def test_bridge_is_a_human_channel_job(app, bearer, browser, service, candidates, monkeypatch):
    fake = _Download(monkeypatch)
    assert TestClient(app).post("/v1/ui/bridge", json={}).status_code == 401
    r = bearer.post("/v1/ui/bridge", json={})
    assert r.status_code == 403 and r.json()["code"] == "HUMAN_ONLY" and fake.entries == []
    client, headers = browser
    assert client.post("/v1/ui/bridge", json={}).json()["code"] == "BAD_CSRF"
    assert client.post("/v1/ui/bridge", headers=headers, json={"max_bytes": -1}).status_code == 422
    assert client.post("/v1/ui/bridge", headers=headers, json={"bogus": 1}).status_code == 422
    r = client.post("/v1/ui/bridge", headers=headers, json={"max_bytes": 250, "limit": 5})
    assert r.status_code == 202
    job = _wait(client, r.json()["job_id"])
    assert job["kind"] == "bridge" and job["status"] == "done" and job["error"] is None
    assert job["progress"] == {"bridged": 2, "failed": 0, "total": 2}
    assert job["result"] == {"bridged": 2, "failed": [], "groups": {"groups": 1}}
    assert fake.entries == [1, 2]


def test_bridge_without_approvals_points_at_the_cli(service, config, candidates, monkeypatch):
    fake = _Download(monkeypatch)
    client, headers = _login(_app(service, config, approvals=False))
    r = client.post("/v1/ui/bridge", headers=headers, json={})
    assert r.status_code == 403 and r.json()["code"] == "APPROVALS_DISABLED"
    assert "drivemerge bridge" in r.json()["error"] and fake.entries == []


def test_bridge_reports_progress_and_failures(browser, candidates, monkeypatch):
    fake = _Download(monkeypatch)
    fake.fail = {2}
    fake.gate.clear()
    client, headers = browser
    job_id = client.post("/v1/ui/bridge", headers=headers, json={}).json()["job_id"]
    running = client.get(f"/v1/jobs/{job_id}").json()
    assert running["status"] == "running" and running["kind"] == "bridge"
    assert running["progress"].keys() >= {"bridged", "failed", "total"}
    fake.gate.set()
    job = _wait(client, job_id)
    assert job["status"] == "done" and job["progress"] == {"bridged": 2, "failed": 1, "total": 3}
    assert job["result"]["bridged"] == 2 and job["result"]["failed"] == [{"entry_id": 2, "error": "download failed"}]


def test_a_second_bridge_or_rescan_while_one_runs_is_busy(browser, candidates, monkeypatch):
    fake = _Download(monkeypatch)
    fake.gate.clear()
    client, headers = browser
    first = client.post("/v1/ui/bridge", headers=headers, json={})
    assert first.status_code == 202
    for path, body in (("/v1/ui/bridge", {}), ("/v1/ui/rescan", {})):
        r = client.post(path, headers=headers, json=body)
        assert r.status_code == 409 and r.json()["code"] == "JOB_BUSY"
        assert r.json()["params"]["kind"] == "bridge" and r.json()["params"]["job"] == first.json()["job_id"]
    fake.gate.set()
    assert _wait(client, first.json()["job_id"])["status"] == "done"
