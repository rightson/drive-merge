"""The onboarding routes (docs/ONBOARDING.md section D) end to end over HTTP, against the fake rclone in tests/fakes
(a cloud is a local folder): connect, sync, free space, disconnect, the human-channel rules, and the promise that
no token ever appears in a response."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from drivemerge import config as config_mod
from drivemerge import connections, plans, ui_api
from drivemerge.models import Op
from drivemerge.service import Service

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix fake rclone")

FAKE = str(Path(__file__).parent / "fakes" / "rclone")
API = "api-token"
UI = "ui-token-one-time"
AUTH = {"Authorization": f"Bearer {API}"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setenv("FAKE_RCLONE_ROOT_BASE", str(tmp_path / "clouds"))
    for var in ("FAKE_RCLONE_AUTH_MODE", "FAKE_RCLONE_TEST_FAIL", "FAKE_RCLONE_REAL", "FAKE_RCLONE_LOG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(connections, "MANAGED_BIN_DIR", tmp_path / "managed-bin")
    yield
    for f in list(connections._FLOWS.values()):
        connections.cancel_flow(f.id)
    connections._FLOWS.clear()


@pytest.fixture
def config(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    main = d / "config.toml"
    main.write_text(f'[rclone]\nbinary = "{FAKE}"\n[database]\npath = "{tmp_path / "catalog.db"}"\n'
                    '[safety]\nexecution_enabled = true\n[api]\ntoken = "' + API + '"\n')
    return config_mod.load_config(main)


@pytest.fixture
def service(config):
    svc = Service(config)       # config_path comes from the loaded config
    yield svc
    svc.conn.close()


def _app(service, *, approvals=True):
    return ui_api.create_ui_app(service, config=service.config, api_token=API, ui_token=UI,
                                approvals_enabled=approvals)


def _login(app):
    client = TestClient(app)
    r = client.post("/v1/ui/session", json={"token": UI})
    assert r.status_code == 200
    return client, {"X-CSRF-Token": r.json()["csrf"]}


@pytest.fixture
def app(service):
    return _app(service)


@pytest.fixture
def browser(app):
    return _login(app)


@pytest.fixture
def bearer(app):
    return TestClient(app, headers=AUTH)


class Recorder:
    """Every response body the page or a script could have seen, to scan for secrets afterwards."""

    def __init__(self, client: TestClient, headers: dict):
        self.client, self.headers, self.bodies = client, headers, []

    def call(self, method: str, url: str, **kw):
        r = self.client.request(method, url, headers=self.headers, **kw)
        self.bodies.append(f"{method} {url} -> {r.status_code} {r.text}")
        return r


def _wait(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


def _sign_in(flow_auth_url: str, root: Path, account: str | None = None):
    root.mkdir(parents=True, exist_ok=True)
    meta = {"root": str(root)}
    if account:
        meta["account"] = account
    state = flow_auth_url.split("state=")[1]
    Path(os.environ["FAKE_RCLONE_AUTH_DIR"], state).write_text(json.dumps(meta))


def _flow_done(client, flow_id, headers, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get(f"/v1/ui/connect/flows/{flow_id}", headers=headers).json()
        if st["status"] != "waiting":
            return st
        time.sleep(0.05)
    raise AssertionError("sign-in still waiting")


def _connect(rec: Recorder, provider: str, root: Path, *, name: str | None = None, **start):
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": provider, **start})
    assert r.status_code == 200, r.text
    flow_id, url = r.json()["flow_id"], r.json()["auth_url"]
    _sign_in(url, root, f"{provider}@example.com")
    assert _flow_done(rec.client, flow_id, rec.headers)["status"] == "succeeded"
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/finish", json={"name": name} if name else {})
    assert r.status_code == 200, r.text
    return flow_id, r.json()


# ------------------------------------------------------------------ the full flow, and no token anywhere

def test_full_connect_sync_release_disconnect_flow_never_exposes_a_token(browser, bearer, service, tmp_path):
    client, headers = browser
    rec = Recorder(client, headers)
    brec = Recorder(bearer, {})
    secret = "csecret-never-shown"

    setup = rec.call("GET", "/v1/setup").json()
    assert setup["first_run"] is True and setup["connections"] == [] and setup["rclone"]["ok"] is True
    assert {p["key"]: p["needs_client"] for p in setup["providers"]} == {
        "drive": True, "onedrive": False, "dropbox": False}
    assert brec.call("GET", "/v1/setup").json()["first_run"] is True          # bearer may read it too

    # Dropbox: a click, a sign-in, done. Its content is the same as the Drive's, so there is something to free.
    shared = {"a.txt": "same bytes", "only-dbx.txt": "dropbox only"}  # 10 bytes shared, 12 only on dropbox
    for rel, data in shared.items():
        (tmp_path / "clouds" / "dropbox").mkdir(parents=True, exist_ok=True)
        (tmp_path / "clouds" / "dropbox" / rel).write_text(data)
    (tmp_path / "clouds" / "gdrive").mkdir(parents=True)
    (tmp_path / "clouds" / "gdrive" / "a.txt").write_text("same bytes")
    tokens = []

    def remember_tokens(flow_id):
        flow = connections.get_flow(flow_id)
        data = json.loads(flow._token)
        tokens.extend([data["access_token"], data["refresh_token"]])

    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "dropbox"})
    flow_id, url = r.json()["flow_id"], r.json()["auth_url"]
    assert url.startswith("http://127.0.0.1:53682/auth?state=") and set(r.json()) == {"flow_id", "auth_url"}
    assert rec.call("GET", f"/v1/ui/connect/flows/{flow_id}").json()["status"] == "waiting"
    _sign_in(url, tmp_path / "clouds" / "dropbox", "me@example.com")
    assert _flow_done(client, flow_id, headers)["status"] == "succeeded"
    remember_tokens(flow_id)
    conn = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/finish", json={}).json()
    assert conn["name"] == "dropbox" and conn["writable"] is False and conn["account"] == "me@example.com"

    # Drive needs its own client; the secret goes in and never comes out.
    r = rec.call("POST", "/v1/ui/connect/start",
                 json={"provider": "drive", "client_id": "cid", "client_secret": secret})
    flow_id, url = r.json()["flow_id"], r.json()["auth_url"]
    _sign_in(url, tmp_path / "clouds" / "gdrive", "me@gmail.com")
    assert _flow_done(client, flow_id, headers)["status"] == "succeeded"
    remember_tokens(flow_id)
    conn = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/finish", json={"name": "gdrive"}).json()
    assert conn["name"] == "gdrive"

    # The running server sees both clouds without a restart.
    names = [r.name for r in service.config.remotes]
    assert names == ["dropbox", "gdrive"]
    ui_cfg = rec.call("GET", "/v1/ui/config").json()
    assert {r["name"]: r["connected"] for r in ui_cfg["remotes"]} == {"dropbox": True, "gdrive": True}
    setup = brec.call("GET", "/v1/setup").json()
    assert setup["first_run"] is False and [c["name"] for c in setup["connections"]] == ["dropbox", "gdrive"]

    test = rec.call("POST", "/v1/ui/connections/dropbox/test").json()
    assert test["ok"] is True and test["account"] == "me@example.com"

    # Sync everything (a job of kind sync, progress per remote), then the free-space report.
    job_id = rec.call("POST", "/v1/ui/sync", json={}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["kind"] == "sync" and job["status"] == "done", job
    assert set(job["progress"]["remotes"]) == {"dropbox", "gdrive"}
    assert job["result"]["remotes"]["dropbox"]["files"] == 2
    report = brec.call("GET", "/v1/release/dropbox").json()
    assert report["proven"]["files"] == 0 and report["probable"]["files"] == 1       # same name and size, no shared hash
    assert report["only_here"]["files"] == 1 and "quota" in report
    # Verify: bridge exactly the probable files (a job), and they move to proven.
    job = _wait(client, rec.call("POST", "/v1/ui/bridge", json={"entry_ids": report["probable"]["entry_ids"]}
                                 ).json()["job_id"])
    assert job["status"] == "done" and job["result"]["bridged"] == 1, job
    report = brec.call("GET", "/v1/release/dropbox").json()
    assert report["proven"]["files"] == 1 and report["proven"]["by_remote"] == {"gdrive": 10}
    assert report["only_here"]["files"] == 1
    assert brec.call("GET", "/v1/release/dropbox?keep_on=gdrive").json()["proven"]["files"] == 1
    assert brec.call("GET", "/v1/release/ghost").status_code in (400, 404)

    # Allow changes: the second, write-scope sign-in.
    r = rec.call("POST", "/v1/ui/connect/start", json={"purpose": "write", "name": "dropbox"})
    flow_id, url = r.json()["flow_id"], r.json()["auth_url"]
    _sign_in(url, tmp_path / "clouds" / "dropbox")
    assert _flow_done(client, flow_id, headers)["status"] == "succeeded"
    remember_tokens(flow_id)
    conn = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/finish", json={"name": "dropbox"}).json()
    assert conn["writable"] is True
    assert service.config.remote("dropbox").write == "dropbox-rw:"

    # Disconnect: gone from the config at once, still known to the catalog but marked as not connected.
    assert rec.call("DELETE", "/v1/ui/connections/dropbox").json() == {"ok": True}
    assert [r.name for r in service.config.remotes] == ["gdrive"]
    ui_cfg = rec.call("GET", "/v1/ui/config").json()
    assert {r["name"]: r["connected"] for r in ui_cfg["remotes"]} == {"dropbox": False, "gdrive": True}
    assert [c["name"] for c in brec.call("GET", "/v1/setup").json()["connections"]] == ["gdrive"]

    assert len(tokens) == 6 and all(len(t) > 10 for t in tokens)
    conf_text = service.config.own_rclone_conf.read_text()
    assert any(t in conf_text for t in tokens)                  # the tokens do live in rclone.conf
    blob = "\n".join(rec.bodies + brec.bodies)
    for secret_text in (*tokens, secret, "fake-at-", "fake-rt-"):
        assert secret_text not in blob, secret_text[:12]
    assert "refresh_token" not in blob and "access_token" not in blob


# ------------------------------------------------------------------ channels

HUMAN_ROUTES = [
    ("POST", "/v1/ui/connect/start", {"provider": "dropbox"}),
    ("GET", "/v1/ui/connect/flows/abc", None),
    ("POST", "/v1/ui/connect/flows/abc/finish", {}),
    ("POST", "/v1/ui/connect/flows/abc/cancel", None),
    ("POST", "/v1/ui/connections/dropbox/test", None),
    ("DELETE", "/v1/ui/connections/dropbox", None),
    ("POST", "/v1/ui/rclone/install", None),
    ("POST", "/v1/ui/sync", {}),
    ("POST", "/v1/ui/rescan", {}),
    ("POST", "/v1/ui/plans/1/purge", {"confirm": "purge"}),
]


@pytest.mark.parametrize("method,url,body", HUMAN_ROUTES)
def test_the_bearer_token_and_strangers_never_reach_the_human_routes(bearer, app, method, url, body):
    r = bearer.request(method, url, json=body)
    assert r.status_code == 403 and r.json()["code"] == "HUMAN_ONLY"
    assert TestClient(app).request(method, url, json=body).status_code == 401


@pytest.mark.parametrize("method,url,body", HUMAN_ROUTES)
def test_a_session_without_csrf_or_from_a_foreign_origin_is_refused(browser, method, url, body):
    client, headers = browser
    r = client.request(method, url, json=body)
    assert r.status_code == 403 and r.json()["code"] == "BAD_CSRF"
    r = client.request(method, url, json=body, headers={**headers, "Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "BAD_ORIGIN"


@pytest.mark.parametrize("method,url,body", HUMAN_ROUTES)
def test_without_approvals_the_human_routes_are_off(service, method, url, body):
    client, headers = _login(_app(service, approvals=False))
    r = client.request(method, url, json=body, headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "APPROVALS_DISABLED"


def test_setup_and_release_are_readable_with_either_credential_but_not_anonymously(browser, bearer, app, service,
                                                                                     catalog_with_two_remotes):
    client, _ = browser
    for url in ("/v1/setup", "/v1/release/gdrive"):
        assert bearer.get(url).status_code == 200
        assert client.get(url).status_code == 200
        assert TestClient(app).get(url).status_code == 401


@pytest.fixture
def conn(service):
    return service.conn


@pytest.fixture
def catalog_with_two_remotes(catalog):
    cat = catalog
    g = cat.remote("gdrive", "drive")
    d = cat.remote("dropbox", "dropbox")
    cat.file(g, "a.txt", size=10, md5="aa")
    cat.file(d, "b.txt", size=10, md5="aa")
    return cat


# ------------------------------------------------------------------ errors carry code and params

def test_connect_errors_are_coded(browser, tmp_path):
    client, headers = browser
    rec = Recorder(client, headers)
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "drive"})
    assert r.status_code == 400 and r.json()["code"] == "NEEDS_CLIENT"
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "nope"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    r = rec.call("POST", "/v1/ui/connect/start", json={"purpose": "write", "name": "ghost"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "dropbox"})
    first = r.json()["flow_id"]
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "onedrive"})
    assert r.status_code == 409 and r.json()["code"] == "FLOW_BUSY"
    r = rec.call("POST", f"/v1/ui/connect/flows/{first}/finish", json={})
    assert r.status_code == 409 and r.json()["code"] == "FLOW_FAILED"           # not signed in yet
    assert rec.call("POST", f"/v1/ui/connect/flows/{first}/cancel").json() == {"ok": True}
    assert rec.call("GET", f"/v1/ui/connect/flows/{first}").json()["status"] == "cancelled"
    assert rec.call("GET", "/v1/ui/connect/flows/unknown").status_code == 404
    assert rec.call("POST", "/v1/ui/connections/ghost/test").json()["code"] == "NOT_FOUND"
    assert rec.call("DELETE", "/v1/ui/connections/ghost").status_code == 404

    _connect(rec, "dropbox", tmp_path / "clouds" / "dropbox")
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "dropbox"})
    flow = r.json()["flow_id"]
    _sign_in(r.json()["auth_url"], tmp_path / "clouds" / "dropbox")
    _flow_done(client, flow, headers)
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow}/finish", json={"name": "dropbox"})
    assert r.status_code == 409 and r.json()["code"] == "NAME_TAKEN"
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow}/finish", json={"name": "Bad Name"})
    assert r.status_code == 400 and r.json()["code"] == "BAD_NAME"
    # a write sign-in needs the connection's name
    rec.call("POST", f"/v1/ui/connect/flows/{flow}/cancel")
    r = rec.call("POST", "/v1/ui/connect/start", json={"purpose": "write", "name": "dropbox"})
    wflow = r.json()["flow_id"]
    _sign_in(r.json()["auth_url"], tmp_path / "clouds" / "dropbox")
    _flow_done(client, wflow, headers)
    r = rec.call("POST", f"/v1/ui/connect/flows/{wflow}/finish", json={})
    assert r.status_code == 400 and r.json()["code"] == "BAD_NAME"


def test_a_connection_that_does_not_work_is_refused_with_its_reason(browser, tmp_path, monkeypatch):
    client, headers = browser
    rec = Recorder(client, headers)
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "dropbox"})
    flow = r.json()["flow_id"]
    _sign_in(r.json()["auth_url"], tmp_path / "clouds" / "dropbox")
    _flow_done(client, flow, headers)
    monkeypatch.setenv("FAKE_RCLONE_TEST_FAIL", "1")
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow}/finish", json={})
    assert r.status_code == 409 and r.json()["code"] == "TEST_FAILED"
    assert rec.call("GET", "/v1/setup").json()["connections"] == []


def test_sync_before_any_connection_is_a_coded_error(browser):
    client, headers = browser
    r = client.post("/v1/ui/sync", headers=headers, json={})
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST"


# ------------------------------------------------------------------ jobs: purge, install, bridge

def _done_release_plan(service):
    conn = service.conn
    g = conn.execute("SELECT id FROM remotes WHERE name = 'gdrive'").fetchone()[0]
    d = conn.execute("SELECT id FROM remotes WHERE name = 'dropbox'").fetchone()[0]
    pid = plans.create_plan(conn, "p", "release", [
        Op("delete", g, "a.txt", witness_remote_id=d, witness_path="b.txt")], created_by="test")
    plans.set_status(conn, pid, "done")
    return pid


def test_purge_needs_the_typed_word_and_runs_as_a_job(browser, service, catalog_with_two_remotes, monkeypatch):
    client, headers = browser
    pid = _done_release_plan(service)
    calls = []

    def purge(self, plan_id, *, older_than_days=30):
        calls.append((plan_id, older_than_days, self.conn is not service.conn))
        return {"plan_id": plan_id, "status": "done", "done": 1, "failed": 0, "skipped": 0, "error": None}

    monkeypatch.setattr(Service, "purge", purge)
    url = f"/v1/ui/plans/{pid}/purge"
    for body in ({}, {"confirm": "Purge"}, {"confirm": "yes"}):
        r = client.post(url, headers=headers, json=body)
        assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST", body
    assert not calls
    r = client.post(url, headers=headers, json={"confirm": "purge"})
    assert r.status_code == 202
    job = _wait(client, r.json()["job_id"])
    assert job["kind"] == "purge" and job["status"] == "done" and job["result"]["done"] == 1
    assert calls == [(pid, 0, True)]                          # purge now by default, on the job's own connection
    r = client.post(url, headers=headers, json={"confirm": "purge", "older_than_days": 30})
    _wait(client, r.json()["job_id"])
    assert calls[-1][:2] == (pid, 30)


def test_purge_refuses_a_plan_that_has_not_run_and_a_missing_one(browser, service, catalog_with_two_remotes):
    client, headers = browser
    pid = _done_release_plan(service)
    plans.set_status(service.conn, pid, "draft")
    r = client.post(f"/v1/ui/plans/{pid}/purge", headers=headers, json={"confirm": "purge"})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert client.post("/v1/ui/plans/999/purge", headers=headers, json={"confirm": "purge"}).status_code == 404


def test_purge_failures_fail_the_job(browser, service, catalog_with_two_remotes, monkeypatch):
    client, headers = browser
    pid = _done_release_plan(service)
    monkeypatch.setattr(Service, "purge", lambda self, plan_id, *, older_than_days=30: {
        "status": "partial", "done": 0, "failed": 1, "skipped": 0, "error": "witness gone"})
    job = _wait(client, client.post(f"/v1/ui/plans/{pid}/purge", headers=headers,
                                    json={"confirm": "purge"}).json()["job_id"])
    assert job["status"] == "failed" and "witness gone" in job["error"]


def test_purge_is_off_when_execution_is_disabled(browser, service, config, catalog_with_two_remotes):
    client, headers = browser
    pid = _done_release_plan(service)
    config.safety.execution_enabled = False
    r = client.post(f"/v1/ui/plans/{pid}/purge", headers=headers, json={"confirm": "purge"})
    assert r.status_code == 403 and r.json()["code"] == "EXECUTION_DISABLED"


def test_bridge_can_be_restricted_to_given_entries(browser, monkeypatch):
    client, headers = browser
    seen = {}

    def bridge(self, **kw):
        seen.update(kw)
        return {"bridged": 2, "failed": [], "groups": None}

    monkeypatch.setattr(Service, "bridge", bridge)
    job = _wait(client, client.post("/v1/ui/bridge", headers=headers, json={"entry_ids": [3, 4]}).json()["job_id"])
    assert job["status"] == "done" and seen["entry_ids"] == [3, 4]
    assert client.post("/v1/ui/bridge", headers=headers, json={"entry_ids": ["x"]}).status_code == 422


def test_rclone_install_is_a_job_and_the_server_then_uses_the_new_binary(browser, service, tmp_path, monkeypatch):
    client, headers = browser
    path = service.config.config_path
    path.write_text(path.read_text().replace(FAKE, str(tmp_path / "no-such-rclone")))
    service.reload_config()
    managed = tmp_path / "managed-bin"

    def install(dest, *, progress=None, **kw):
        assert Path(dest) == managed
        if progress:
            progress("download", 5, 10)
        managed.mkdir(parents=True, exist_ok=True)
        (managed / "rclone").write_text(Path(FAKE).read_text())
        (managed / "rclone").chmod(0o755)
        return managed / "rclone"

    monkeypatch.setattr(connections, "install_rclone", install)
    assert client.get("/v1/setup").json()["rclone"]["found"] is False
    job = _wait(client, client.post("/v1/ui/rclone/install", headers=headers).json()["job_id"])
    assert job["kind"] == "rclone_install" and job["status"] == "done", job
    assert job["result"]["rclone"]["found"] and job["result"]["rclone"]["managed"]
    assert service.rclone.binary == str(managed / "rclone")
    assert client.get("/v1/setup").json()["rclone"]["managed"] is True


def test_a_failed_install_is_a_failed_job(browser, monkeypatch):
    client, headers = browser

    def install(dest, *, progress=None, **kw):
        raise connections.ConnectError("FLOW_FAILED", "checksum mismatch")

    monkeypatch.setattr(connections, "install_rclone", install)
    job = _wait(client, client.post("/v1/ui/rclone/install", headers=headers).json()["job_id"])
    assert job["status"] == "failed" and "checksum mismatch" in job["error"]


# ------------------------------------------------------------------ config path, binary, quota cache

def test_service_and_jobs_use_the_config_path_and_a_managed_rclone(config, tmp_path):
    managed = tmp_path / "managed-bin"
    managed.mkdir()
    (managed / "rclone").write_text("#!/bin/sh\necho 'rclone v1.75.2'\n")
    (managed / "rclone").chmod(0o755)
    config.rclone_binary = str(tmp_path / "missing-rclone")
    svc = Service(config)
    try:
        assert svc.config_path == config.config_path
        assert svc.rclone.binary == str(managed / "rclone")
        assert Service(config, config_path=config.config_path).config_path == config.config_path
    finally:
        svc.conn.close()


def test_quota_in_the_release_report_is_cached_for_ten_minutes(browser, bearer, service, tmp_path, monkeypatch):
    client, headers = browser
    _connect(Recorder(client, headers), "dropbox", tmp_path / "clouds" / "dropbox")
    calls = []
    real = connections.test_connection

    def counting(config, name):
        calls.append(name)
        return {**real(config, name), "quota": {"used": 5, "total": 50, "free": 45}}

    monkeypatch.setattr(connections, "test_connection", counting)
    first = bearer.get("/v1/release/dropbox").json()
    second = bearer.get("/v1/release/dropbox").json()
    assert first["quota"] == second["quota"] == {"used": 5, "total": 50, "free": 45}
    assert calls == ["dropbox"]
    # the page's Test button refreshes the cache
    client.post("/v1/ui/connections/dropbox/test", headers=headers)
    assert calls == ["dropbox", "dropbox"]
    bearer.get("/v1/release/dropbox")
    assert calls == ["dropbox", "dropbox"]


def test_quota_is_null_when_unknown(bearer, service, catalog_with_two_remotes, monkeypatch):
    # a cloud from config.toml (not a connection) has no quota, and a failing lookup is not an error
    service.config.remotes.extend([config_mod.RemoteConfig("gdrive", "drive", "gdrive:", "gdrive:")])
    assert bearer.get("/v1/release/gdrive").json()["quota"] is None
    service.config.remote("gdrive").connection = True

    def boom(config, name):
        raise connections.ConnectError("TEST_FAILED", "expired")

    monkeypatch.setattr(connections, "test_connection", boom)
    assert bearer.get("/v1/release/gdrive").json()["quota"] is None
