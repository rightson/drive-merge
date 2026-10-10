"""Workspace server: auth, the human channel (CSRF, origin, approvals gate), jobs, progress and static files.

Workspace and suggestion functions are replaced with recorders so these tests exercise routing, auth and
error mapping; the real executor is replaced too. Jobs open their own connection, so the catalog lives in a file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from drivemerge import db, executor, plans, suggest, ui_api, workspace
from drivemerge.config import ApiConfig, Config, RemoteConfig, SafetyConfig
from drivemerge.models import Op
from drivemerge.service import Service

API = "api-token"
UI = "ui-token-one-time"
AUTH = {"Authorization": f"Bearer {API}"}


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def conn(tmp_path):
    """File-backed catalog (overrides conftest's in-memory one): jobs connect to it by path."""
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
    )


@pytest.fixture
def service(conn, catalog, config):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    catalog.file(g, "Docs/budget-2024.pdf", size=500, md5="bb")
    catalog.file(d, "Docs/notes.txt", size=10)
    return Service(config, conn=conn)


def _app(service, config, *, approvals=True, api_token=API):
    return ui_api.create_ui_app(service, config=config, api_token=api_token, ui_token=UI,
                                approvals_enabled=approvals)


@pytest.fixture
def app(service, config):
    return _app(service, config)


@pytest.fixture
def anon(app):
    return TestClient(app)


@pytest.fixture
def bearer(app):
    return TestClient(app, headers=AUTH)


def _login(app) -> tuple[TestClient, dict]:
    """A client holding a session cookie, plus the headers a browser would send on the human channel."""
    client = TestClient(app)
    r = client.post("/v1/ui/session", json={"token": UI})
    assert r.status_code == 200, r.text
    return client, {"X-CSRF-Token": r.json()["csrf"]}


@pytest.fixture
def browser(app):
    return _login(app)


def _rid(conn, name):
    return conn.execute("SELECT id FROM remotes WHERE name = ?", (name,)).fetchone()[0]


def _simulated_plan(conn, service, **params):
    """A copy plan that passes simulation; returns (plan_id, approval code)."""
    pid = plans.create_plan(conn, "copy budget", "manual", [
        Op("copy", _rid(conn, "gdrive"), "Docs/budget-2024.pdf",
           dst_remote_id=_rid(conn, "dropbox"), dst_path="Docs/budget-2024.pdf"),
    ], created_by="test", params=params)
    report = service.simulate(pid)
    assert report["ok"], report
    return pid, report["approval_code"]


def _draft_row(conn, version=1, status="open"):
    return conn.execute(
        "INSERT INTO drafts(title, status, version, created_at, updated_at) VALUES ('d', ?, ?, ?, ?) RETURNING id",
        (status, version, db.utcnow(), db.utcnow())).fetchone()[0]


def _approved_plan(conn, service, **params):
    pid, code = _simulated_plan(conn, service, **params)
    plans.approve_plan(conn, pid, code, approved_by="test")
    return pid


def _record(monkeypatch, module, name, *, returns=None, raises=None):
    """Replace module.<name> with a recorder of (args, kwargs)."""
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(module, name, fake)
    return calls


def _wait(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running")


def _routes(routes):
    for route in routes:
        if getattr(route, "path", None):
            yield route
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _routes(nested.routes)


# ----------------------------------------------------------------- sessions

def test_session_exchange_sets_a_strict_http_only_cookie(anon):
    r = anon.post("/v1/ui/session", json={"token": UI})
    assert r.status_code == 200
    body = r.json()
    assert body["approvals_enabled"] is True and len(body["csrf"]) >= 32
    cookie = r.headers["set-cookie"].lower()
    assert "dm_session=" in cookie
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # plain http://127.0.0.1
    assert anon.get("/v1/ui/config").status_code == 200


def test_wrong_ui_token_is_refused_and_sets_no_cookie(anon):
    for token in ("", "nope", UI[:-1], UI + "x"):
        r = anon.post("/v1/ui/session", json={"token": token})
        assert r.status_code == 401, token
        assert "set-cookie" not in r.headers
    assert anon.post("/v1/ui/session", json={}).status_code == 422
    assert anon.get("/v1/ui/config").status_code == 401


def test_ui_token_can_be_reused_and_each_session_has_its_own_csrf(app):
    (a, ha), (b, hb) = _login(app), _login(app)
    assert ha != hb
    assert a.cookies.get("dm_session") != b.cookies.get("dm_session")
    # session A's csrf token does not work with session B's cookie
    r = b.post("/v1/ui/rescan", headers=ha, json={})
    assert r.status_code == 403 and r.json()["code"] == "BAD_CSRF"


def test_config_reports_remotes_and_hands_a_session_its_csrf_token(browser, bearer, service):
    client, headers = browser
    cfg = client.get("/v1/ui/config").json()
    assert cfg["csrf"] == headers["X-CSRF-Token"]
    assert cfg["approvals_enabled"] is True and cfg["execution_enabled"] is True
    assert {r["name"] for r in cfg["remotes"]} == {"gdrive", "dropbox"}
    gd = next(r for r in cfg["remotes"] if r["name"] == "gdrive")
    assert gd["provider"] == "drive" and gd["writable"] is True and gd["case_insensitive"] is False
    assert cfg["version"]
    assert bearer.get("/v1/ui/config").json()["csrf"] is None  # a bearer caller never learns a csrf token


def test_empty_api_token_disables_bearer_but_not_the_session(service, config):
    app = _app(service, config, api_token="")
    for header in ("Bearer ", "Bearer", "Bearer x"):
        assert TestClient(app).get("/v1/stats", headers={"Authorization": header}).status_code == 401
    client, _ = _login(app)
    assert client.get("/v1/stats").status_code == 200


def test_empty_ui_token_is_rejected(service, config):
    with pytest.raises(ValueError, match="UI token"):
        ui_api.create_ui_app(service, config=config, api_token=API, ui_token=" ", approvals_enabled=True)


# ----------------------------------------------------------------- normal routes: bearer or session

OPEN = {"/healthz", "/", "/ui/", "/ui/static/{path:path}", "/v1/ui/session"}


def test_every_route_but_the_open_ones_needs_credentials(app):
    client = TestClient(app)
    routes = [r for r in _routes(client.app.routes) if r.path not in OPEN]
    paths = {r.path for r in routes}
    assert {"/v1/stats", "/v1/plans", "/v1/tree", "/v1/drafts", "/v1/suggestions", "/v1/jobs/{job_id}",
            "/v1/plans/{plan_id}/progress", "/v1/ui/plans/{plan_id}/approve", "/v1/ui/rescan"} <= paths
    checked = 0
    for route in routes:
        for method in route.methods:
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {API}"}):
                url = route.path.replace("{plan_id}", "1").replace("{draft_id}", "1") \
                                .replace("{action_id}", "1").replace("{entry_id}", "1").replace("{job_id}", "x")
                r = client.request(method, url, headers=headers, json={})
                assert r.status_code == 401, (method, route.path, headers)
                assert r.json()["code"] == "UNAUTHENTICATED"
                checked += 1
    assert checked >= 25 * 3


def test_open_routes_need_no_credentials(anon):
    assert anon.get("/healthz").json() == {"ok": True}
    assert anon.get("/", follow_redirects=False).status_code in (302, 307)


def test_docs_and_schema_are_not_served(anon):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert anon.get(path).status_code == 404


def test_existing_rest_routes_work_with_bearer_and_with_a_session(bearer, browser, service):
    assert bearer.get("/v1/stats").status_code == 200
    client, _ = browser
    assert client.get("/v1/stats").json() == bearer.get("/v1/stats").json()
    assert client.get("/v1/search", params={"q": "budget"}).status_code == 200
    pid, _ = _simulated_plan(service.conn, service)
    assert client.get(f"/v1/plans/{pid}").json()["status"] == "simulated"
    assert bearer.get("/v1/plans/999").status_code == 404


def test_bearer_can_read_tree_and_drafts(bearer, monkeypatch):
    roots = _record(monkeypatch, workspace, "roots", returns=[{"name": "gdrive", "path": ""}])
    tree = _record(monkeypatch, workspace, "list_dir",
                   returns={"remote": "gdrive", "path": "Docs", "view": "after", "draft_version": 3,
                            "items": [], "ghosts": [], "total": 0})
    drafts = _record(monkeypatch, workspace, "list_drafts", returns=[{"id": 1, "title": "t"}])
    one = _record(monkeypatch, workspace, "get_draft", returns={"id": 1, "version": 3, "actions": []})

    assert bearer.get("/v1/tree/roots", params={"draft": 1}).json() == {"roots": [{"name": "gdrive", "path": ""}]}
    assert roots[0][1] == {"draft_id": 1}
    assert bearer.get("/v1/tree/roots").status_code == 200 and roots[1][1] == {"draft_id": None}

    r = bearer.get("/v1/tree", params={"remote": "gdrive", "path": "Docs", "draft": 1, "view": "before",
                                       "ghosts": 1, "limit": 20, "offset": 40})
    assert r.status_code == 200 and r.json()["draft_version"] == 3
    assert tree[0][0][1:] == ("gdrive", "Docs")
    assert tree[0][1] == {"draft_id": 1, "view": "before", "ghosts": True, "limit": 20, "offset": 40}
    assert bearer.get("/v1/tree").status_code == 422  # remote is required
    bearer.get("/v1/tree", params={"remote": "gdrive", "limit": 10**9})
    assert tree[-1][1]["limit"] <= 5000 and tree[-1][1]["draft_id"] is None and tree[-1][1]["view"] == "after"

    assert bearer.get("/v1/drafts").json() == {"drafts": [{"id": 1, "title": "t"}]}
    assert drafts[0][1] == {"status": "open"}
    bearer.get("/v1/drafts", params={"status": "all"})
    assert drafts[1][1] == {"status": None}
    assert bearer.get("/v1/drafts/1").json()["version"] == 3 and one[0][0][1] == 1


def test_draft_mutations_are_routed_and_attributed_to_the_caller(bearer, browser, monkeypatch):
    client, _ = browser
    create = _record(monkeypatch, workspace, "create_draft", returns={"id": 2, "title": "School"})
    add = _record(monkeypatch, workspace, "add_action",
                  returns={"action": {"id": 5}, "draft_version": 4, "warnings": [{"code": "MERGE", "message": "m"}]})
    undo = _record(monkeypatch, workspace, "undo", returns={"id": 2, "version": 5})
    redo = _record(monkeypatch, workspace, "redo", returns={"id": 2, "version": 6})
    remove = _record(monkeypatch, workspace, "remove_action", returns={"draft_version": 7, "removed": [5, 6]})
    discard = _record(monkeypatch, workspace, "discard_draft")

    r = bearer.post("/v1/drafts", json={"title": "School"})
    assert r.status_code == 201 and r.json()["id"] == 2 and create[0][0][1] == "School"
    assert bearer.post("/v1/drafts").status_code == 201 and create[1][0][1] == ""  # workspace picks the default title

    body = {"kind": "move", "items": [{"remote": "gdrive", "path": "Docs"}],
            "dst": {"remote": "dropbox", "path": ""}, "note": "why"}
    r = bearer.post("/v1/drafts/2/actions", json=body)
    assert r.status_code == 201 and r.json()["warnings"][0]["code"] == "MERGE"
    assert add[0][0][1:] == (2, body) and add[0][1] == {"created_by": "api"}
    assert client.post("/v1/drafts/2/actions", json=body).status_code == 201
    assert add[1][1] == {"created_by": "ui"}

    assert bearer.post("/v1/drafts/2/undo").json()["version"] == 5 and undo[0][0][1] == 2
    assert bearer.post("/v1/drafts/2/redo").json()["version"] == 6 and redo[0][0][1] == 2
    assert bearer.delete("/v1/drafts/2/actions/5").json() == {"draft_version": 7, "removed": [5, 6]}
    assert remove[0][0][1:] == (2, 5)
    assert bearer.delete("/v1/drafts/2").json() == {"ok": True} and discard[0][0][1] == 2


def test_workspace_and_value_errors_map_to_http_statuses(bearer, monkeypatch):
    body = {"kind": "move", "items": [], "dst": {"remote": "gdrive", "path": ""}}

    def failing(exc):
        monkeypatch.setattr(workspace, "add_action", lambda *a, **k: (_ for _ in ()).throw(exc))
        return bearer.post("/v1/drafts/1/actions", json=body)

    r = failing(workspace.WorkspaceError("DST_NOT_DIR", "gdrive:a.txt is not a folder"))
    assert r.status_code == 409 and r.json() == {"error": "gdrive:a.txt is not a folder", "code": "DST_NOT_DIR"}
    r = failing(workspace.WorkspaceError("NOT_FOUND", "no draft 1"))
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    r = failing(workspace.WorkspaceError("BAD_REQUEST", "a move needs items"))
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST"
    r = failing(workspace.WorkspaceError("NOT_OPEN", "draft 1 is applied"))
    assert r.status_code == 409 and r.json()["code"] == "NOT_OPEN"
    r = failing(ValueError("bad value"))
    assert r.status_code == 400 and r.json() == {"error": "bad value"}
    r = failing(KeyError("unknown remote 'x'"))
    assert r.status_code == 404 and r.json() == {"error": "unknown remote 'x'"}
    assert bearer.post("/v1/drafts/1/actions", content=b"[]",
                       headers={"Content-Type": "application/json"}).status_code == 422


def test_real_workspace_errors_reach_the_client(bearer, service):
    """No fakes: the genuine workspace module raises WorkspaceError and the app maps it."""
    assert bearer.get("/v1/drafts/99").status_code == 404
    draft = bearer.post("/v1/drafts", json={"title": "t"}).json()
    r = bearer.post(f"/v1/drafts/{draft['id']}/actions", json={
        "kind": "move", "items": [{"remote": "gdrive", "path": "Nope"}], "dst": {"remote": "dropbox", "path": ""}})
    assert r.status_code == 409 and r.json()["code"] == "SRC_NOT_FOUND"
    r = bearer.post(f"/v1/drafts/{draft['id']}/actions", json={
        "kind": "move", "items": [{"remote": "gdrive", "path": "Docs"}], "dst": {"remote": "dropbox", "path": ""}})
    assert r.status_code == 201
    assert bearer.get(f"/v1/drafts/{draft['id']}").json()["version"] == r.json()["draft_version"]


def test_suggestions_route_forwards_kind_remote_and_limit(bearer, monkeypatch):
    related = _record(monkeypatch, suggest, "related_folders", returns=[{"id": "related:1"}])
    dupes = _record(monkeypatch, suggest, "duplicate_folders", returns=[{"id": "duplicates:2"}])

    assert bearer.get("/v1/suggestions", params={"kind": "related", "remote": "gdrive", "limit": 5}).json() == {
        "suggestions": [{"id": "related:1"}]}
    assert related[0][1] == {"remote": "gdrive", "limit": 5} and not dupes
    assert bearer.get("/v1/suggestions", params={"kind": "duplicates"}).json() == {
        "suggestions": [{"id": "duplicates:2"}]}
    assert dupes[0][1] == {"remote": None, "limit": 20}
    both = bearer.get("/v1/suggestions").json()["suggestions"]
    assert [s["id"] for s in both] == ["related:1", "duplicates:2"]
    r = bearer.get("/v1/suggestions", params={"kind": "bogus"})
    assert r.status_code == 400 and "related" in r.json()["error"]
    monkeypatch.setattr(suggest, "related_folders", lambda *a, **k: (_ for _ in ()).throw(KeyError("unknown remote 'z'")))
    assert bearer.get("/v1/suggestions", params={"kind": "related", "remote": "z"}).status_code == 404


def test_cookie_requests_from_another_origin_are_refused(browser, bearer, monkeypatch):
    client, _ = browser
    _record(monkeypatch, workspace, "create_draft", returns={"id": 1})
    r = client.post("/v1/drafts", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "BAD_ORIGIN"
    assert client.post("/v1/drafts", json={}, headers={"Origin": "http://testserver"}).status_code == 201
    assert client.get("/v1/drafts", headers={"Origin": "http://evil.example"}).status_code == 200  # reads are harmless
    assert bearer.post("/v1/drafts", json={}, headers={"Origin": "http://evil.example"}).status_code == 201


# ----------------------------------------------------------------- preview

def _preview_result(ok=True, digest="abcdef0123456789"):
    return {"plan_id": 7, "draft_version": 2, "ok": ok,
            "simulation": {"ok": ok, "digest": digest}, "summary": {}, "changes": [], "conflicts": [],
            "empty_dirs_left": 0}


def test_preview_returns_the_approval_code_only_to_a_session_that_may_approve(service, config, monkeypatch):
    calls = _record(monkeypatch, workspace, "preview", returns=_preview_result())
    approving = _app(service, config, approvals=True)
    viewing = _app(service, config, approvals=False)

    session, _ = _login(approving)
    r = session.post("/v1/drafts/3/preview")
    assert r.status_code == 200 and r.json()["approval_code"] == "abcdef01"
    assert "digest" not in r.json()["simulation"]  # its first 8 characters are the code: never sent on
    assert calls[0][0][1] == 3
    policy = calls[0][1]["policy"]
    assert policy.min_copies == config.safety.min_copies and policy.quarantine_dir == config.safety.quarantine_dir

    bearer_view = TestClient(approving, headers=AUTH).post("/v1/drafts/3/preview").json()
    assert "approval_code" not in bearer_view and "digest" not in bearer_view["simulation"]
    assert "approval_code" not in _login(viewing)[0].post("/v1/drafts/3/preview").json()

    monkeypatch.setattr(workspace, "preview", lambda *a, **k: _preview_result(ok=False))
    assert "approval_code" not in session.post("/v1/drafts/3/preview").json()
    # a clean simulation with conflicts is not a clean draft
    monkeypatch.setattr(workspace, "preview", lambda *a, **k: {**_preview_result(), "ok": False})
    assert "approval_code" not in session.post("/v1/drafts/3/preview").json()
    # nothing to do: no plan, no code
    monkeypatch.setattr(workspace, "preview", lambda *a, **k: {"plan_id": None, "message": "nothing to do"})
    assert session.post("/v1/drafts/3/preview").json() == {"plan_id": None, "message": "nothing to do"}
    # a code smuggled in by the preview itself is never passed on to a bearer caller
    monkeypatch.setattr(workspace, "preview", lambda *a, **k: {**_preview_result(), "approval_code": "leak"})
    assert "approval_code" not in TestClient(approving, headers=AUTH).post("/v1/drafts/3/preview").json()


# ----------------------------------------------------------------- the human channel

HUMAN = [("POST", "/v1/ui/plans/1/approve", {"code": "abcdef01"}),
         ("POST", "/v1/ui/plans/1/execute", None),
         ("POST", "/v1/ui/rescan", {"remotes": ["gdrive"]})]


def test_bearer_token_cannot_approve_execute_or_rescan(bearer, anon, service):
    pid, code = _simulated_plan(service.conn, service)
    for method, url, body in HUMAN:
        r = bearer.request(method, url.replace("/1/", f"/{pid}/"), json=body)
        assert r.status_code == 403, url
        assert r.json()["code"] == "HUMAN_ONLY"
        assert anon.request(method, url, json=body).status_code == 401
    assert bearer.post(f"/v1/ui/plans/{pid}/approve", json={"code": code}).status_code == 403
    assert plans.get_plan(service.conn, pid, with_ops=False).status == "simulated"


def test_a_session_cookie_alone_is_not_enough(browser, service):
    client, headers = browser
    pid, code = _simulated_plan(service.conn, service)
    url = f"/v1/ui/plans/{pid}/approve"
    for bad in ({}, {"X-CSRF-Token": ""}, {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": headers["X-CSRF-Token"][:-1]}):
        r = client.post(url, json={"code": code}, headers=bad)
        assert r.status_code == 403 and r.json()["code"] == "BAD_CSRF", bad
    for method, u, body in HUMAN[1:]:
        assert client.request(method, u, json=body).status_code == 403
    assert plans.get_plan(service.conn, pid, with_ops=False).status == "simulated"
    assert client.post(url, json={"code": code}, headers=headers).status_code == 200


def test_csrf_header_without_the_cookie_is_not_enough(browser, anon, bearer):
    _, headers = browser
    assert anon.post("/v1/ui/rescan", headers=headers, json={}).status_code == 401
    assert bearer.post("/v1/ui/rescan", headers=headers, json={}).status_code == 403


def test_a_wrong_origin_is_refused_and_a_matching_or_absent_one_is_not(browser, service):
    client, headers = browser
    pid, code = _simulated_plan(service.conn, service)
    url = f"/v1/ui/plans/{pid}/approve"
    for origin in ("http://evil.example", "https://testserver", "http://testserver:8765", "null", "http://127.0.0.1:8765"):
        r = client.post(url, json={"code": code}, headers={**headers, "Origin": origin})
        assert r.status_code == 403 and r.json()["code"] == "BAD_ORIGIN", origin
    assert plans.get_plan(service.conn, pid, with_ops=False).status == "simulated"
    r = client.post(url, json={"code": code}, headers={**headers, "Origin": "http://testserver"})
    assert r.status_code == 200


def test_origin_is_compared_with_the_servers_own_scheme_and_host(app):
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    csrf = {"X-CSRF-Token": client.post("/v1/ui/session", json={"token": UI}).json()["csrf"]}
    assert client.post("/v1/ui/plans/999/approve", json={"code": "x"},
                       headers={**csrf, "Origin": "http://127.0.0.1:8765"}).status_code == 404  # got past the gate
    for origin in ("http://localhost:8765", "http://127.0.0.1:9999", "http://127.0.0.1"):
        assert client.post("/v1/ui/plans/999/approve", json={"code": "x"},
                           headers={**csrf, "Origin": origin}).status_code == 403, origin


def test_without_approvals_the_human_channel_points_at_the_cli(service, config):
    app = _app(service, config, approvals=False)
    client, headers = _login(app)
    assert client.post("/v1/ui/session", json={"token": UI}).json()["approvals_enabled"] is False
    assert client.get("/v1/ui/config").json()["approvals_enabled"] is False
    expected = {"approve": "drivemerge plan approve 12", "execute": "drivemerge plan execute 12"}
    for action, command in expected.items():
        r = client.post(f"/v1/ui/plans/12/{action}", headers=headers, json={"code": "abcdef01"})
        assert r.status_code == 403 and r.json()["code"] == "APPROVALS_DISABLED"
        assert command in r.json()["error"]
    r = client.post("/v1/ui/rescan", headers=headers, json={})
    assert r.status_code == 403 and "drivemerge scan" in r.json()["error"]


def test_approve_binds_the_plan_to_a_human(browser, service):
    client, headers = browser
    pid, code = _simulated_plan(service.conn, service)
    url = f"/v1/ui/plans/{pid}/approve"
    r = client.post(url, headers=headers, json={"code": "00000000"})
    assert r.status_code == 400 and "does not match" in r.json()["error"]
    assert plans.get_plan(service.conn, pid, with_ops=False).status == "simulated"

    assert client.post(url, headers=headers, json={"code": f"  {code.upper()} "}).json() == {"ok": True}
    plan = plans.get_plan(service.conn, pid, with_ops=False)
    assert plan.status == "approved" and plan.approved_by.startswith("ui:")
    assert client.post(url, headers=headers, json={"code": code}).status_code == 400  # already approved
    assert client.post("/v1/ui/plans/999/approve", headers=headers, json={"code": code}).status_code == 404
    assert client.post(url, headers=headers, json={"code": code, "x": 1}).status_code == 422


def test_approve_refuses_a_preview_of_a_draft_that_has_changed_since(browser, service):
    client, headers = browser
    conn = service.conn
    draft = _draft_row(conn, version=3)
    stale, stale_code = _simulated_plan(conn, service, draft_id=draft, draft_version=2)
    r = client.post(f"/v1/ui/plans/{stale}/approve", headers=headers, json={"code": stale_code})
    assert r.status_code == 409 and r.json()["code"] == "STALE_PREVIEW" and "preview" in r.json()["error"]
    assert plans.get_plan(conn, stale, with_ops=False).status == "simulated"

    fresh, fresh_code = _simulated_plan(conn, service, draft_id=draft, draft_version=3)
    assert client.post(f"/v1/ui/plans/{fresh}/approve", headers=headers, json={"code": fresh_code}).status_code == 200

    gone = _draft_row(conn, version=1, status="discarded")
    closed, closed_code = _simulated_plan(conn, service, draft_id=gone, draft_version=1)
    assert client.post(f"/v1/ui/plans/{closed}/approve", headers=headers,
                       json={"code": closed_code}).status_code == 409
    orphan, orphan_code = _simulated_plan(conn, service, draft_id=9999, draft_version=1)
    assert client.post(f"/v1/ui/plans/{orphan}/approve", headers=headers,
                       json={"code": orphan_code}).status_code == 409


def test_the_real_preview_goes_stale_when_the_draft_changes(browser, service):
    """No fakes: preview a real draft, change it, and the UI refuses to approve the old preview."""
    client, headers = browser
    d = client.post("/v1/drafts", json={"title": "docs"}).json()["id"]
    move = {"kind": "move", "items": [{"remote": "gdrive", "path": "Docs"}], "dst": {"remote": "dropbox", "path": ""}}
    assert client.post(f"/v1/drafts/{d}/actions", json=move).status_code == 201
    preview = client.post(f"/v1/drafts/{d}/preview").json()
    assert preview["plan_id"] and preview["simulation"]["ok"], preview
    assert "digest" not in preview["simulation"]  # the code is the first 8 characters of it: not for the page's JSON
    digest = plans.get_plan(service.conn, preview["plan_id"], with_ops=False).digest
    assert preview["approval_code"] == plans.short_code(digest)

    client.post(f"/v1/drafts/{d}/undo")
    r = client.post(f"/v1/ui/plans/{preview['plan_id']}/approve", headers=headers,
                    json={"code": preview["approval_code"]})
    assert r.status_code == 409 and r.json()["code"] == "STALE_PREVIEW"


# ----------------------------------------------------------------- execute and jobs

class _Executor:
    """Stands in for executor.execute_plan: records the call, can block, can fail."""

    def __init__(self, monkeypatch):
        self.calls, self.started, self.gate = [], threading.Event(), threading.Event()
        self.gate.set()
        self.result = lambda plan_id: executor.ExecResult(plan_id, "done", done=2)
        self.raises = None
        monkeypatch.setattr(executor, "execute_plan", self)

    def __call__(self, conn, rclone, config, plan_id, *, tmpdir=None, progress=None):
        self.calls.append((conn, rclone, config, plan_id, threading.current_thread()))
        self.started.set()
        if progress:
            progress({"done": 1, "total": 2, "note": object()})
        assert self.gate.wait(10), "test never released the executor"
        if self.raises:
            raise self.raises
        return self.result(plan_id)


def test_execute_runs_in_a_background_job_with_its_own_connection(browser, service, config, monkeypatch):
    client, headers = browser
    fake = _Executor(monkeypatch)
    pid = _approved_plan(service.conn, service)

    r = client.post(f"/v1/ui/plans/{pid}/execute", headers=headers)
    assert r.status_code == 202
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done" and job["kind"] == "execute" and job["error"] is None
    assert job["result"] == {"plan_id": pid, "status": "done", "done": 2, "failed": 0, "skipped": 0, "error": None}
    assert job["progress"]["plan_id"] == pid and job["progress"]["done"] == 1 and job["progress"]["reports"] == 1
    assert "note" not in job["progress"]  # only plain values are kept

    conn, rclone, cfg, plan_id, thread = fake.calls[0]
    assert plan_id == pid and cfg is config
    assert conn is not service.conn and thread is not threading.current_thread()
    assert rclone is not None
    with pytest.raises(sqlite3.ProgrammingError):  # the job closed its connection when done
        conn.execute("SELECT 1")
    assert client.get("/v1/jobs/nope").status_code == 404


def test_executor_progress_calls_become_named_numbers():
    """The real executor calls progress(done, total, op): the job keeps those as done, total and the current op."""
    jobs, job = ui_api._Jobs(), ui_api._Job("j1", "execute")
    ui_api._reports(jobs, job)(3, 10, Op("copy", 1, "a/x.txt", dst_remote_id=2, dst_path="b/x.txt", seq=4))
    assert job.progress == {"done": 3, "total": 10, "op": "copy", "seq": 4, "src_path": "a/x.txt",
                            "dst_path": "b/x.txt", "reports": 1}


def test_a_finished_draft_plan_marks_the_draft_applied(browser, service, monkeypatch):
    client, headers = browser
    fake = _Executor(monkeypatch)
    applied = _record(monkeypatch, workspace, "mark_applied")
    draft = _draft_row(service.conn, version=4)
    pid = _approved_plan(service.conn, service, draft_id=draft, draft_version=4)
    other = _approved_plan(service.conn, service)

    job = _wait(client, client.post(f"/v1/ui/plans/{pid}/execute", headers=headers).json()["job_id"])
    assert job["status"] == "done" and job["result"]["draft_applied"] is True
    assert len(applied) == 1 and applied[0][0][1] == draft

    job = _wait(client, client.post(f"/v1/ui/plans/{other}/execute", headers=headers).json()["job_id"])
    assert job["status"] == "done" and len(applied) == 1  # a plan without a draft touches no draft

    fake.result = lambda plan_id: executor.ExecResult(plan_id, "failed", done=1, failed=1, error="op 2 failed")
    job = _wait(client, client.post(f"/v1/ui/plans/{pid}/execute", headers=headers).json()["job_id"])
    assert job["status"] == "failed" and job["error"] == "op 2 failed"
    assert job["result"]["failed"] == 1 and len(applied) == 1  # a failed run leaves the draft open


def test_a_draft_that_cannot_be_closed_does_not_turn_a_finished_run_into_a_failure(browser, service, monkeypatch):
    client, headers = browser
    _Executor(monkeypatch)
    _record(monkeypatch, workspace, "mark_applied", raises=workspace.WorkspaceError("NOT_OPEN", "draft is applied"))
    draft = _draft_row(service.conn, version=1)
    pid = _approved_plan(service.conn, service, draft_id=draft, draft_version=1)
    job = _wait(client, client.post(f"/v1/ui/plans/{pid}/execute", headers=headers).json()["job_id"])
    assert job["status"] == "done"
    assert job["result"]["draft_applied"] is False and "applied" in job["result"]["draft_error"]


def test_an_executor_that_raises_fails_the_job_with_its_message(browser, service, monkeypatch):
    client, headers = browser
    fake = _Executor(monkeypatch)
    fake.raises = executor.ExecutionDisabled("execution is disabled")
    pid = _approved_plan(service.conn, service)
    job = _wait(client, client.post(f"/v1/ui/plans/{pid}/execute", headers=headers).json()["job_id"])
    assert job["status"] == "failed" and job["error"] == "execution is disabled" and job["result"] is None


def test_only_one_execute_job_runs_at_a_time(browser, service, monkeypatch):
    client, headers = browser
    fake = _Executor(monkeypatch)
    fake.gate.clear()
    first = _approved_plan(service.conn, service)
    second = _approved_plan(service.conn, service)

    job_id = client.post(f"/v1/ui/plans/{first}/execute", headers=headers).json()["job_id"]
    assert fake.started.wait(5)
    assert client.get(f"/v1/jobs/{job_id}").json()["status"] == "running"

    for plan in (first, second):
        r = client.post(f"/v1/ui/plans/{plan}/execute", headers=headers)
        assert r.status_code == 409 and r.json()["code"] == "JOB_BUSY", plan
    r = client.post("/v1/ui/rescan", headers=headers, json={})  # a scan would race the executor on the catalog
    assert r.status_code == 409 and r.json()["code"] == "JOB_BUSY"
    assert len(fake.calls) == 1

    fake.gate.set()
    assert _wait(client, job_id)["status"] == "done"
    again = client.post(f"/v1/ui/plans/{second}/execute", headers=headers)
    assert again.status_code == 202
    assert _wait(client, again.json()["job_id"])["status"] == "done" and len(fake.calls) == 2


def test_concurrent_execute_requests_start_exactly_one_job(browser, service, monkeypatch):
    client, headers = browser
    fake = _Executor(monkeypatch)
    fake.gate.clear()
    pids = [_approved_plan(service.conn, service) for _ in range(6)]
    codes = []

    def post(pid):
        codes.append(client.post(f"/v1/ui/plans/{pid}/execute", headers=headers).status_code)

    threads = [threading.Thread(target=post, args=(p,)) for p in pids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    fake.gate.set()
    assert sorted(codes) == [202] + [409] * 5
    deadline = time.monotonic() + 5
    while len(fake.calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(fake.calls) == 1


def test_execute_checks_the_plan_before_starting_a_job(browser, service, config, monkeypatch):
    client, headers = browser
    fake = _Executor(monkeypatch)
    simulated, _ = _simulated_plan(service.conn, service)
    r = client.post(f"/v1/ui/plans/{simulated}/execute", headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "NOT_APPROVED"
    assert client.post("/v1/ui/plans/999/execute", headers=headers).status_code == 404

    draft = _draft_row(service.conn, version=1)
    pid = _approved_plan(service.conn, service, draft_id=draft, draft_version=1)
    service.conn.execute("UPDATE drafts SET version = 2 WHERE id = ?", (draft,))  # edited after approval
    r = client.post(f"/v1/ui/plans/{pid}/execute", headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "STALE_PREVIEW"
    assert not fake.calls


def test_execute_needs_execution_enabled(service, config, monkeypatch):
    config.safety.execution_enabled = False
    fake = _Executor(monkeypatch)
    client, headers = _login(_app(service, config))
    assert client.get("/v1/ui/config").json()["execution_enabled"] is False
    pid = _approved_plan(service.conn, service)
    r = client.post(f"/v1/ui/plans/{pid}/execute", headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "EXECUTION_DISABLED" and "execution_enabled" in r.json()["error"]
    assert not fake.calls


# ----------------------------------------------------------------- rescan

class _ScanLog(list):
    """Calls made by the rescan job; `fail` names the remotes whose scan raises."""

    def __init__(self):
        super().__init__()
        self.fail: set[str] = set()


@pytest.fixture
def scans(monkeypatch):
    """Record Service.scan / rebuild_identity as the rescan job calls them (both refresh dir_stats themselves)."""
    log = _ScanLog()

    def scan(self, remote, *, root="", progress=None, force=False):
        log.append(("scan", remote, self.conn))
        if progress:
            progress(10, 2, 1234)
        if remote in log.fail:
            raise RuntimeError(f"{remote} is unreachable")
        return {"status": "complete", "files": 10, "dirs": 2, "bytes": 1234}

    monkeypatch.setattr(Service, "scan", scan)
    monkeypatch.setattr(Service, "rebuild_identity", lambda self: log.append(("identity", self.conn)) or {"groups": 1})
    return log


def test_rescan_scans_each_remote_then_rebuilds_identity(browser, service, scans):
    client, headers = browser
    r = client.post("/v1/ui/rescan", headers=headers, json={})
    assert r.status_code == 202
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done" and job["kind"] == "rescan" and job["error"] is None
    assert [e[:2] if e[0] == "scan" else e[:1] for e in scans] == [
        ("scan", "gdrive"), ("scan", "dropbox"), ("identity",)]
    conns = {id(e[-1]) for e in scans}
    assert len(conns) == 1 and id(service.conn) not in conns  # one private connection for the whole job
    assert job["result"]["identity"] == {"groups": 1}
    assert [(s["remote"], s["status"]) for s in job["result"]["scans"]] == [("gdrive", "complete"),
                                                                           ("dropbox", "complete")]
    assert job["progress"]["files"] == 10 and job["progress"]["remote"] == "dropbox"
    assert job["progress"]["remotes_total"] == 2


def test_rescan_can_be_limited_to_some_remotes(browser, scans):
    client, headers = browser
    job = _wait(client, client.post("/v1/ui/rescan", headers=headers,
                                    json={"remotes": ["dropbox", "dropbox"]}).json()["job_id"])
    assert job["status"] == "done"
    assert [e[1] for e in scans if e[0] == "scan"] == ["dropbox"]


def test_rescan_rejects_an_unknown_remote_before_starting(browser, scans):
    client, headers = browser
    r = client.post("/v1/ui/rescan", headers=headers, json={"remotes": ["gdrive", "icloud"]})
    assert r.status_code == 404 and "icloud" in r.json()["error"]
    assert client.post("/v1/ui/rescan", headers=headers, json={"remotes": "gdrive"}).status_code == 422
    assert not scans


def test_a_failed_scan_fails_the_job_but_the_other_remotes_still_finish(browser, scans):
    client, headers = browser
    scans.fail.add("gdrive")
    job = _wait(client, client.post("/v1/ui/rescan", headers=headers, json={}).json()["job_id"])
    assert job["status"] == "failed" and "gdrive" in job["error"]
    by_remote = {s["remote"]: s for s in job["result"]["scans"]}
    assert by_remote["gdrive"]["status"] == "failed" and "unreachable" in by_remote["gdrive"]["error"]
    assert by_remote["dropbox"]["status"] == "complete"
    assert [e[0] for e in scans] == ["scan", "scan", "identity"]


# ----------------------------------------------------------------- plan progress

def test_plan_progress_reports_op_counts_the_current_op_and_the_journal(bearer, service):
    conn = service.conn
    g, d = _rid(conn, "gdrive"), _rid(conn, "dropbox")
    pid = plans.create_plan(conn, "p", "manual", [
        Op("copy", g, f"Docs/f{i}.pdf", dst_remote_id=d, dst_path=f"Docs/f{i}.pdf") for i in range(1, 6)
    ] + [Op("delete", g, "Docs/old.pdf", witness_remote_id=d, witness_path="Docs/f1.pdf")], created_by="test")
    ops = {o.seq: o.id for o in plans.load_ops(conn, pid)}

    r = bearer.get(f"/v1/plans/{pid}/progress").json()
    assert r["status"] == "draft" and r["ops_total"] == 6 and r["pending"] == 6
    assert (r["done"], r["failed"], r["skipped"]) == (0, 0, 0)
    assert r["current"] is None and r["recent_journal"] == []

    plans.set_status(conn, pid, "executing")
    plans.set_op_status(conn, ops[1], "done")
    plans.set_op_status(conn, ops[2], "done")
    plans.set_op_status(conn, ops[3], "failed", "boom")
    plans.set_op_status(conn, ops[4], "skipped")
    for i, (action, op_seq) in enumerate([("approve", None), ("precheck", 1), ("copy", 1), ("verify", 1), ("copy", 2),
                                          ("error", 3)]):
        db.journal(conn, action, plan_id=pid, op_id=ops[op_seq] if op_seq else None,
                   detail=json.dumps({"n": i}) if i != 4 else "not json")
    db.journal(conn, "copy", plan_id=pid + 100, detail="{}")  # another plan's rows never show up

    r = bearer.get(f"/v1/plans/{pid}/progress").json()
    assert r["status"] == "executing" and r["ops_total"] == 6
    assert (r["done"], r["failed"], r["skipped"], r["pending"]) == (2, 1, 1, 2)
    assert r["current"] == {"seq": 5, "op": "copy", "src": {"remote": "gdrive", "path": "Docs/f5.pdf"},
                            "dst": {"remote": "dropbox", "path": "Docs/f5.pdf"}}
    journal = r["recent_journal"]
    assert [j["action"] for j in journal] == ["approve", "precheck", "copy", "verify", "copy", "error"]  # oldest first
    assert journal[1]["detail"] == {"n": 1} and journal[4]["detail"] == "not json"
    assert [j["op_seq"] for j in journal] == [None, 1, 1, 1, 2, 3]
    assert all(j["id"] and j["ts"] for j in journal)

    last_two = bearer.get(f"/v1/plans/{pid}/progress", params={"journal": 2}).json()["recent_journal"]
    assert [j["action"] for j in last_two] == ["copy", "error"]

    plans.set_op_status(conn, ops[5], "done")
    plans.set_op_status(conn, ops[6], "done")
    plans.set_status(conn, pid, "failed")
    r = bearer.get(f"/v1/plans/{pid}/progress").json()
    assert r["status"] == "failed" and r["current"] is None and r["pending"] == 0 and r["done"] == 4

    assert bearer.get("/v1/plans/999/progress").status_code == 404


# ----------------------------------------------------------------- static files

@pytest.fixture
def web(tmp_path, monkeypatch):
    """A throw-away web root next to a file that must never be served."""
    root = tmp_path / "site" / "web"
    (root / "vendor").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>drive-merge</title><div id=app></div>")
    (root / "app.js").write_text("export const hi = 1;")
    (root / "styles.css").write_text("body{margin:0}")
    (root / "vendor" / "lib.mjs").write_text("export default 2;")
    (root / ".hidden").write_text("hidden")
    (tmp_path / "site" / "secret.txt").write_text("TOP-SECRET")
    (tmp_path / "outside.txt").write_text("TOP-SECRET")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    monkeypatch.setattr(ui_api, "_web_root", lambda: root)
    return root


def test_the_index_and_assets_are_served_without_credentials(web, anon):
    r = anon.get("/ui/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<title>drive-merge</title>" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"

    js = anon.get("/ui/static/app.js")
    assert js.status_code == 200 and js.text == "export const hi = 1;"
    assert js.headers["content-type"].startswith("text/javascript")
    assert anon.get("/ui/static/styles.css").headers["content-type"].startswith("text/css")
    assert anon.get("/ui/static/vendor/lib.mjs").headers["content-type"].startswith("text/javascript")
    assert anon.get("/ui/static/index.html").status_code == 200
    assert anon.get("/ui/static/missing.js").status_code == 404
    assert anon.get("/ui/static/").status_code == 404 and anon.get("/ui/static/vendor").status_code == 404


def test_root_redirects_to_the_app(web, anon):
    r = anon.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"
    assert anon.get("/").text.startswith("<!doctype html>")
    r = anon.get("/ui", follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308) and r.headers["location"].endswith("/ui/")


TRAVERSAL = [
    "/ui/static/../secret.txt",
    "/ui/static/../../config.py",
    "/ui/static/%2e%2e/secret.txt",
    "/ui/static/%2e%2e/%2e%2e/config.py",
    "/ui/static/..%2fsecret.txt",
    "/ui/static/%2e%2e%2fsecret.txt",
    "/ui/static/vendor/..%2f..%2fsecret.txt",
    "/ui/static/vendor/../../secret.txt",
    "/ui/static/..%5csecret.txt",
    "/ui/static/..\\secret.txt",
    "/ui/static/%252e%252e/secret.txt",
    "/ui/static/....//secret.txt",
    "/ui/static/.%2e/secret.txt",
    "/ui/static//etc/passwd",
    "/ui/static/%2fetc%2fpasswd",
    "/ui/static/app.js%00.png",
    "/ui/static/vendor/%00/../../secret.txt",
    "/ui/static/C:/Windows/win.ini",
    "/ui/static/.hidden",
    "/ui/static/link.txt",  # a symlink out of the web root
]


@pytest.mark.parametrize("url", TRAVERSAL)
def test_path_traversal_is_rejected(web, anon, url):
    r = anon.get(url)
    assert r.status_code == 404, url
    assert "TOP-SECRET" not in r.text and "root:" not in r.text and "class Config" not in r.text


def test_traversal_to_a_real_source_file_is_rejected(anon):
    """With the real web root, ../config.py is a file that exists next to it."""
    root = ui_api._web_root()
    assert (root.parent / "config.py").is_file()
    for url in ("/ui/static/../config.py", "/ui/static/%2e%2e/config.py", "/ui/static/..%2fconfig.py",
                "/ui/static/%2e%2e%2fconfig.py", "/ui/static/vendor/..%2f..%2fconfig.py"):
        r = anon.get(url)
        assert r.status_code == 404 and "class Config" not in r.text, url


def test_the_packaged_web_directory_is_served(anon):
    root = ui_api._web_root()
    assert root.name == "web" and root.is_dir()
    r = anon.get("/ui/static/vendor/htm-preact-standalone.mjs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.content == (root / "vendor" / "htm-preact-standalone.mjs").read_bytes()
    if (root / "index.html").is_file():
        assert anon.get("/ui/").text == (root / "index.html").read_text(encoding="utf-8")
    else:
        assert anon.get("/ui/").status_code == 404


def test_an_in_memory_catalog_is_refused(service, config):
    """Jobs open their own connection: to ':memory:' that would be an empty catalog, not this one."""
    config.db_path = ":memory:"
    with pytest.raises(ValueError, match="file"):
        _app(service, config)


def test_a_missing_index_is_a_plain_404(tmp_path, monkeypatch, service, config):
    monkeypatch.setattr(ui_api, "_web_root", lambda: tmp_path / "nowhere")  # before the app captures it
    assert TestClient(_app(service, config)).get("/ui/").status_code == 404


# ----------------------------------------------------------------- launching

class _Stream:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def test_approvals_need_a_terminal_and_no_agent_marker():
    allowed = ui_api.approvals_allowed
    assert allowed(stdin=_Stream(True), stdout=_Stream(True), env={}) == (True, None)
    assert allowed(stdin=_Stream(True), stdout=_Stream(True), env={"PATH": "/bin"}) == (True, None)
    for env in ({"CLAUDECODE": "1"}, {"AI_AGENT": "x"}, {"CLAUDECODE": ""}):
        ok, why = allowed(stdin=_Stream(True), stdout=_Stream(True), env=env)
        assert not ok and next(iter(env)) in why
    for stdin, stdout in ((False, True), (True, False), (False, False)):
        ok, why = allowed(stdin=_Stream(stdin), stdout=_Stream(stdout), env={})
        assert not ok and "terminal" in why
    assert allowed(stdin=object(), stdout=_Stream(True), env={})[0] is False  # no isatty at all


@pytest.fixture
def launched(monkeypatch, tmp_path):
    """Intercept uvicorn and the browser; returns what run_ui handed to them."""
    seen = {"opened": []}
    monkeypatch.setattr(ui_api.uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    monkeypatch.setattr(ui_api, "_open_when_ready", lambda host, port, url: seen["opened"].append((host, port, url)))
    return seen


def _run_config(tmp_path, **kw):
    return Config(db_path=str(tmp_path / "run.db"), api=ApiConfig(host="127.0.0.1", port=8765, token=API), **kw)


def test_run_ui_prints_a_one_time_url_and_serves_the_app_on_loopback(launched, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ui_api, "approvals_allowed", lambda: (True, None))
    ui_api.run_ui(_run_config(tmp_path), open_browser=False)
    out = capsys.readouterr().out
    assert launched["host"] == "127.0.0.1" and launched["port"] == 8765
    assert "http://127.0.0.1:8765/ui/#token=" in out and "enabled" in out
    assert not launched["opened"]
    token = out.split("#token=")[1].split()[0]
    assert len(token) >= 24 and token != API

    client = TestClient(launched["app"])
    assert client.post("/v1/ui/session", json={"token": token}).json()["approvals_enabled"] is True
    assert client.get("/v1/stats", headers=AUTH).status_code == 200


def test_run_ui_generates_a_new_token_each_time(launched, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ui_api, "approvals_allowed", lambda: (True, None))
    tokens = []
    for _ in range(2):
        ui_api.run_ui(_run_config(tmp_path), open_browser=False)
        tokens.append(capsys.readouterr().out.split("#token=")[1].split()[0])
    assert tokens[0] != tokens[1]


def test_run_ui_says_why_approvals_are_off_and_the_app_enforces_it(launched, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ui_api, "approvals_allowed", lambda: (False, "CLAUDECODE is set"))
    ui_api.run_ui(_run_config(tmp_path), host="127.0.0.1", port=9001, open_browser=True)
    out = capsys.readouterr().out
    assert "DISABLED" in out and "CLAUDECODE is set" in out and "drivemerge plan approve" in out
    assert "Execution is disabled" in out  # the default config
    assert launched["port"] == 9001 and ":9001/ui/#token=" in out
    assert launched["opened"] == [("127.0.0.1", 9001, out.split("drive-merge workspace: ")[1].split()[0])]
    client = TestClient(launched["app"])
    token = out.split("#token=")[1].split()[0]
    assert client.post("/v1/ui/session", json={"token": token}).json()["approvals_enabled"] is False


def test_run_ui_decides_approvals_from_the_real_environment(launched, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    ui_api.run_ui(_run_config(tmp_path), open_browser=False)
    assert "DISABLED" in capsys.readouterr().out


def test_run_ui_refuses_non_loopback_hosts(launched, tmp_path):
    for host in ("0.0.0.0", "192.168.1.5", "example.com", "::"):
        with pytest.raises(ValueError, match="loopback"):
            ui_api.run_ui(_run_config(tmp_path), host=host, open_browser=False)
    assert "app" not in launched
    cfg = _run_config(tmp_path)
    cfg.api.host = "0.0.0.0"
    with pytest.raises(ValueError, match="loopback"):
        ui_api.run_ui(cfg, open_browser=False)


def test_run_ui_brackets_an_ipv6_host_in_the_url(launched, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ui_api, "approvals_allowed", lambda: (True, None))
    ui_api.run_ui(_run_config(tmp_path), host="::1", open_browser=False)
    assert "http://[::1]:8765/ui/#token=" in capsys.readouterr().out
    assert launched["host"] == "::1"
