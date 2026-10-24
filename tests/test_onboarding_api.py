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
    for var in ("FAKE_RCLONE_AUTH_MODE", "FAKE_RCLONE_TEST_FAIL", "FAKE_RCLONE_REAL", "FAKE_RCLONE_LOG",
                "FAKE_RCLONE_DRIVES", "FAKE_RCLONE_FAIL_CREATE", "FAKE_RCLONE_FAIL_CONTINUE",
                "FAKE_RCLONE_ECHO_ANSWER", "FAKE_RCLONE_ECHO_CREATE", "FAKE_RCLONE_HANG_LSJSON", "RCLONE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(connections, "MANAGED_BIN_DIR", tmp_path / "managed-bin")
    yield
    for f in list(connections._FLOWS.values()):
        connections.cancel_flow(f.id)
    connections._FLOWS.clear()
    connections._RESERVED.clear()


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
        "drive": True, "onedrive": False, "dropbox": False, "iclouddrive": False}
    assert setup["needed"] is True and setup["manual_remotes"] == []
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
    assert url.startswith("http://127.0.0.1:53682/auth?state=") and set(r.json()) == {"flow_id", "auth_url", "status"}
    assert r.json()["status"] == "waiting"
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
    ("POST", "/v1/ui/connect/flows/abc/answer", {"result": "x"}),
    ("POST", "/v1/ui/connect/flows/abc/cancel", None),
    ("POST", "/v1/ui/connect/paste", {"provider": "dropbox", "token": "x"}),
    ("POST", "/v1/ui/connect/import", {"source": "x"}),
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


def test_setup_importable_and_release_are_readable_with_either_credential_but_not_anonymously(
        browser, bearer, app, service, catalog_with_two_remotes):
    client, _ = browser
    for url in ("/v1/setup", "/v1/release/gdrive", "/v1/ui/connect/importable"):
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


# ======================================================================================================
# docs/ONBOARDING.md section G: questions, iCloud, pasted tokens, import, coded Test, `needed`
# ======================================================================================================

import logging  # noqa: E402

PASSWORD = "pw-SECRET-4711"
CODE = "123456"


def _clouds(tmp_path, name, files=None):
    root = tmp_path / "clouds" / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in (files or {"a.txt": "hello"}).items():
        (root / rel).write_text(data)
    return root


def _icloud_start(rec, tmp_path, *, name=None, password=PASSWORD, **extra):
    _clouds(tmp_path, name or "icloud")
    return rec.call("POST", "/v1/ui/connect/start", json={
        "provider": "iclouddrive", "apple_id": "me@example.com", "password": password,
        **({"name": name} if name else {}), **extra})


def _pasted(root: Path, **extra):
    token = {"access_token": "pasted-at-a1b2c3d4", "token_type": "Bearer", "refresh_token": "pasted-rt-e5f6a7b8",
             "expiry": "2099-01-01T00:00:00Z", "fake_root": str(root), **extra}
    return token, "Paste the following into your remote machine --->\n" + json.dumps(token) + "\n<---End paste\n"


# ---- GET /v1/setup: needed, manual_remotes, providers

def test_setup_reports_the_providers_and_the_command_for_another_computer(bearer):
    setup = bearer.get("/v1/setup").json()
    by = {p["key"]: p for p in setup["providers"]}
    assert by["iclouddrive"] == {"key": "iclouddrive", "label": "iCloud Drive", "needs_client": False, "kind": "config",
                                 "fields": ["apple_id", "password"], "authorize": {"read": [], "write": []}}
    assert by["drive"]["kind"] == "authorize" and by["drive"]["fields"] == []
    assert by["drive"]["authorize"] == {"read": ["rclone", "authorize", "drive", "--drive-scope=drive.readonly"],
                                        "write": ["rclone", "authorize", "drive", "--drive-scope=drive"]}
    assert by["onedrive"]["authorize"]["read"][-1].startswith("--onedrive-access-scopes=Files.Read")
    assert by["dropbox"]["authorize"] == {"read": ["rclone", "authorize", "dropbox"],
                                          "write": ["rclone", "authorize", "dropbox"]}


def test_needed_is_true_with_nothing_and_with_a_never_scanned_cloud_and_false_once_scanned(browser, bearer, tmp_path):
    client, headers = browser
    rec = Recorder(client, headers)
    assert bearer.get("/v1/setup").json()["needed"] is True                              # nothing configured
    _connect(rec, "dropbox", tmp_path / "clouds" / "dropbox")
    setup = bearer.get("/v1/setup").json()
    assert setup["needed"] is True and setup["first_run"] is False                        # configured, never scanned
    job = _wait(client, rec.call("POST", "/v1/ui/sync", json={}).json()["job_id"])
    assert job["status"] == "done", job
    assert bearer.get("/v1/setup").json()["needed"] is False                              # every cloud scanned
    _connect(rec, "dropbox", tmp_path / "clouds" / "dropbox-2", name="second")
    assert bearer.get("/v1/setup").json()["needed"] is True                               # a new, never-scanned one


def test_sync_of_a_never_scanned_connection_has_no_setup_page_extras(browser, tmp_path):
    client, headers = browser
    rec = Recorder(client, headers)
    _clouds(tmp_path, "dropbox", {"a.txt": "one", "b.txt": "two"})
    _connect(rec, "dropbox", tmp_path / "clouds" / "dropbox")
    job_id = rec.call("POST", "/v1/ui/rescan", json={}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "done" and job["result"]["remotes"]["dropbox"]["files"] == 2
    assert "remote" not in job["progress"] and "remote_index" not in job["progress"]       # PR #5's extras are gone
    assert "scans" not in job["result"]
    assert job["progress"]["remotes_total"] == 1 and job["progress"]["remotes_done"] == 1


def test_a_cloud_of_config_toml_is_a_manual_remote_and_can_be_tested_and_synced(browser, bearer, service, tmp_path):
    client, headers = browser
    disk = _clouds(tmp_path, "disk", {"f.txt": "x"})
    path = service.config.config_path
    path.write_text(path.read_text() + f'[[remotes]]\nname = "disk"\nprovider = "local"\nread = "{disk}"\n'
                    f'[[remotes]]\nname = "w"\nprovider = "local"\nread = "{disk}"\nwrite = "{disk}"\n')
    service.reload_config()
    setup = bearer.get("/v1/setup").json()
    assert setup["manual_remotes"] == [{"name": "disk", "provider": "local", "writable": False},
                                       {"name": "w", "provider": "local", "writable": True}]
    assert setup["connections"] == [] and setup["needed"] is True and setup["first_run"] is False
    r = client.post("/v1/ui/connections/disk/test", headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["code"] is None
    r = client.post("/v1/ui/connections/nobody/test", headers=headers)
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert _wait(client, client.post("/v1/ui/sync", headers=headers, json={"remotes": ["disk"]}).json()["job_id"]
                 )["status"] == "done"
    assert client.delete("/v1/ui/connections/disk", headers=headers).status_code == 404   # not a connection


def test_a_connection_made_by_the_terminal_while_the_server_runs_is_seen_on_the_next_setup(browser, bearer, service,
                                                                                         config, tmp_path):
    cli_config = config_mod.load_config(config.config_path)            # `drivemerge connect` runs in its own process
    flow = connections.start_flow(cli_config, "dropbox")
    _sign_in(flow.auth_url, tmp_path / "clouds" / "dropbox", "term@example.com")
    assert flow.wait(10)
    connections.finish_connect(cli_config, flow.id)
    assert [r.name for r in service.config.remotes] == []                # the server has not looked yet
    setup = bearer.get("/v1/setup").json()
    assert [c["name"] for c in setup["connections"]] == ["dropbox"] and setup["first_run"] is False
    assert [r.name for r in service.config.remotes] == ["dropbox"]       # reloaded, rclone and catalog remotes too
    assert service.conn.execute("SELECT 1 FROM remotes WHERE name = 'dropbox'").fetchone()
    connections.disconnect(cli_config, "dropbox")
    assert bearer.get("/v1/setup").json()["connections"] == []


# ---- iCloud Drive over HTTP

def test_icloud_start_is_a_202_question_then_the_answer_is_a_200_connection(browser, bearer, service, tmp_path, caplog,
                                                                           capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))
    client, headers = browser
    rec, brec = Recorder(client, headers), Recorder(bearer, {})
    r = _icloud_start(rec, tmp_path)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"flow_id", "status", "question", "retry", "auth_url"} and body["auth_url"] is None
    assert body["status"] == "question" and body["retry"] is False and body["question"]["name"] == "config_2fa"
    flow_id = body["flow_id"]
    view = rec.call("GET", f"/v1/ui/connect/flows/{flow_id}").json()
    assert view["status"] == "question" and view["question"] == body["question"] and view["name"] == "icloud"
    assert set(view) == {"id", "provider", "purpose", "status", "auth_url", "error", "code", "params", "name",
                         "question", "retry", "connection"}
    assert brec.call("GET", "/v1/setup").json()["connections"] == []                      # nothing recorded yet

    wrong = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": "000000"})
    assert wrong.status_code == 202 and wrong.json()["retry"] is True and wrong.json()["flow_id"] == flow_id
    assert "not accepted" not in wrong.text
    ok = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": CODE})
    assert ok.status_code == 200 and "flow_id" not in ok.json()
    conn = ok.json()
    assert conn["name"] == "icloud" and conn["provider"] == "iclouddrive" and conn["scoped"] is False
    assert conn["writable"] is False and conn["imported_from"] is None
    assert rec.call("GET", f"/v1/ui/connect/flows/{flow_id}").json()["status"] == "done"
    assert [r.name for r in service.config.remotes] == ["icloud"]                          # visible without a restart
    again = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": CODE})
    assert again.status_code == 409 and again.json()["code"] == "NOT_WAITING"
    assert brec.call("GET", "/v1/setup").json()["connections"][0]["scoped"] is False

    out = capsys.readouterr()
    blob = "\n".join(rec.bodies + brec.bodies + [out.out, out.err, caplog.text, log.read_text()])
    blob += (tmp_path / "conf" / "connections.toml").read_text() + (tmp_path / "conf" / "config.toml").read_text()
    for secret in (PASSWORD, CODE, "000000"):
        assert secret not in blob


def test_icloud_body_validation(browser, tmp_path):
    client, headers = browser
    rec = Recorder(client, headers)
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "iclouddrive", "apple_id": "me@example.com"})
    assert r.status_code == 400 and r.json()["code"] == "MISSING_FIELD" and r.json()["params"] == {"field": "password"}
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "iclouddrive", "password": "pw"})
    assert r.status_code == 400 and r.json()["params"] == {"field": "apple_id"}
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "iclouddrive", "apple_id": "a@b.c", "password": "pw",
                                                       "client_id": "x", "client_secret": "y"})
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST"
    for provider in ("dropbox", "onedrive"):
        for extra in ({"apple_id": "a@b.c"}, {"password": "pw"}):
            r = rec.call("POST", "/v1/ui/connect/start", json={"provider": provider, **extra})
            assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST", (provider, extra)
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "onedrive", "client_id": "c", "client_secret": "s"})
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST"
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "iclouddrive", "apple_id": "a@b.c", "password": "p",
                                                       "extra": 1})
    assert r.status_code == 422
    assert rec.call("GET", "/v1/setup").json()["connections"] == []
    assert not [f for f in connections._FLOWS.values() if f.status in connections._BUSY]


def test_icloud_enable_changes_reuses_read(browser, tmp_path):
    client, headers = browser
    rec = Recorder(client, headers)
    flow_id = _icloud_start(rec, tmp_path).json()["flow_id"]
    rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": CODE})
    r = rec.call("POST", "/v1/ui/connect/start", json={"purpose": "write", "name": "icloud"})
    assert r.status_code == 200 and r.json()["auth_url"] is None and r.json()["status"] == "succeeded"
    flow = r.json()["flow_id"]
    sections = connections._conf_sections(tmp_path / "conf" / "rclone.conf")
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow}/finish", json={"name": "icloud"})
    assert r.status_code == 200 and r.json()["write"] == r.json()["read"] == "icloud-ro:" and r.json()["writable"]
    assert connections._conf_sections(tmp_path / "conf" / "rclone.conf") == sections
    assert rec.call("DELETE", "/v1/ui/connections/icloud").json() == {"ok": True}


# ---- OneDrive with several drives

def test_finish_answers_202_with_the_drive_question_and_the_answer_makes_the_connection(browser, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_DRIVES", "2")
    client, headers = browser
    rec = Recorder(client, headers)
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "onedrive"})
    flow_id = r.json()["flow_id"]
    _sign_in(r.json()["auth_url"], _clouds(tmp_path, "onedrive"), "me@outlook.com")
    assert _flow_done(client, flow_id, headers)["status"] == "succeeded"
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/finish", json={})
    assert r.status_code == 202
    q = r.json()
    assert q["status"] == "question" and q["flow_id"] == flow_id and q["retry"] is False
    assert q["question"]["name"] == "config_driveid" and [e["value"] for e in q["question"]["examples"]] == [
        "drive-personal", "drive-docs"]
    assert rec.call("GET", "/v1/setup").json()["connections"] == []
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": "drive-docs"})
    assert r.status_code == 200 and r.json()["name"] == "onedrive"
    assert connections._conf_sections(tmp_path / "conf" / "rclone.conf")["onedrive-ro"]["drive_id"] == "drive-docs"


def test_cancel_during_a_question_removes_the_remote(browser, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_DRIVES", "2")
    client, headers = browser
    rec = Recorder(client, headers)
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "onedrive"})
    flow_id = r.json()["flow_id"]
    _sign_in(r.json()["auth_url"], _clouds(tmp_path, "onedrive"))
    _flow_done(client, flow_id, headers)
    assert rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/finish", json={}).status_code == 202
    assert "onedrive-ro" in connections._conf_sections(tmp_path / "conf" / "rclone.conf")
    assert rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/cancel").json() == {"ok": True}
    assert rec.call("GET", f"/v1/ui/connect/flows/{flow_id}").json()["status"] == "cancelled"
    assert connections._conf_sections(tmp_path / "conf" / "rclone.conf") == {}
    assert not (tmp_path / "conf" / "connections.toml").exists()
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": "drive-docs"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_WAITING"


def test_an_answer_is_validated(browser):
    client, headers = browser
    rec = Recorder(client, headers)
    url = "/v1/ui/connect/flows/nope/answer"
    assert rec.call("POST", url, json={}).status_code == 422
    assert rec.call("POST", url, json={"result": 5}).status_code == 422
    assert rec.call("POST", url, json={"result": "x", "extra": 1}).status_code == 422
    assert rec.call("POST", url, json={"result": "x"}).status_code == 404
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "dropbox"})
    flow_id = r.json()["flow_id"]
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": "x"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_WAITING"                  # no question was asked
    rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/cancel")


# ---- a token from another computer

def test_a_pasted_token_makes_a_flow_that_finishes_like_a_browser_sign_in(browser, bearer, tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    client, headers = browser
    rec, brec = Recorder(client, headers), Recorder(bearer, {})
    _, block = _pasted(_clouds(tmp_path, "dropbox"), fake_account="me@example.com")
    r = rec.call("POST", "/v1/ui/connect/paste", json={"provider": "dropbox", "token": block})
    assert r.status_code == 200 and set(r.json()) == {"flow_id", "status"} and r.json()["status"] == "succeeded"
    flow_id = r.json()["flow_id"]
    view = rec.call("GET", f"/v1/ui/connect/flows/{flow_id}").json()
    assert view["status"] == "succeeded" and view["auth_url"] is None
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/finish", json={})
    assert r.status_code == 200 and r.json()["name"] == "dropbox" and r.json()["account"] == "me@example.com"
    assert [c["name"] for c in brec.call("GET", "/v1/setup").json()["connections"]] == ["dropbox"]
    out = capsys.readouterr()
    blob = "\n".join(rec.bodies + brec.bodies + [out.out, out.err, caplog.text,
                                                 (tmp_path / "conf" / "connections.toml").read_text()])
    assert "pasted-at-a1b2c3d4" not in blob and "pasted-rt-e5f6a7b8" not in blob and "access_token" not in blob


def test_paste_errors_are_coded_and_never_quote_the_token(browser, tmp_path):
    client, headers = browser
    rec = Recorder(client, headers)
    r = rec.call("POST", "/v1/ui/connect/paste", json={"provider": "dropbox", "token": "SECRET-GIBBERISH-NOT-A-TOKEN"})
    assert r.status_code == 400 and r.json()["code"] == "TOKEN_UNREADABLE" and "GIBBERISH" not in r.text
    _, block = _pasted(tmp_path)
    r = rec.call("POST", "/v1/ui/connect/paste", json={"provider": "drive", "token": block})
    assert r.status_code == 400 and r.json()["code"] == "NEEDS_CLIENT" and "pasted-rt" not in r.text
    r = rec.call("POST", "/v1/ui/connect/paste", json={"provider": "nope", "token": block})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    r = rec.call("POST", "/v1/ui/connect/paste", json={"provider": "iclouddrive", "token": block})
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST"
    r = rec.call("POST", "/v1/ui/connect/paste", json={"provider": "dropbox"})
    assert r.status_code == 422
    r = rec.call("POST", "/v1/ui/connect/paste", json={"provider": "dropbox", "token": block, "x": 1})
    assert r.status_code == 422 and "pasted-rt" not in r.text
    r = rec.call("POST", "/v1/ui/connect/paste", json={"purpose": "write", "name": "ghost", "provider": "dropbox",
                                                       "token": block})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert all("pasted-rt" not in b and "pasted-at" not in b for b in rec.bodies)


# ---- import

def _personal(tmp_path, monkeypatch):
    path = tmp_path / "personal.conf"
    path.write_text(
        f'[work]\ntype = dropbox\ntoken = {{"access_token":"imp-at-1234","refresh_token":"imp-rt-5678",'
        f'"expiry":"2099-01-01T00:00:00Z"}}\nfake_root = {_clouds(tmp_path, "work")}\n\n'
        f'[priv]\ntype = crypt\nremote = work:enc\n\n[box1]\ntype = box\n\n'
        f'[spare]\ntype = dropbox\nfake_root = {_clouds(tmp_path, "spare")}\n')
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    return path


def test_importable_and_import_over_http(browser, bearer, service, tmp_path, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    path = _personal(tmp_path, monkeypatch)
    before = path.read_bytes()
    client, headers = browser
    rec, brec = Recorder(client, headers), Recorder(bearer, {})
    got = brec.call("GET", "/v1/ui/connect/importable").json()                             # readable with the API token
    assert got["encrypted"] is False and got["config_path"] == str(path)
    assert {r["name"]: (r["supported"], r["imported"]) for r in got["remotes"]} == {
        "work": (True, False), "priv": (False, False), "box1": (False, False), "spare": (True, False)}
    r = rec.call("POST", "/v1/ui/connect/import", json={"source": "work"})
    assert r.status_code == 200
    conn = r.json()
    assert conn["name"] == "work" and conn["scoped"] is False and conn["imported_from"] == "work"
    assert conn["writable"] is False and conn["read"] == "work-ro:"
    assert path.read_bytes() == before
    assert [r.name for r in service.config.remotes] == ["work"]
    assert {r["name"]: r["imported"] for r in client.get("/v1/ui/connect/importable").json()["remotes"]}["work"] is True
    r = rec.call("POST", "/v1/ui/connect/import", json={"source": "work"})
    assert r.status_code == 409 and r.json()["code"] == "NAME_TAKEN"
    r = rec.call("POST", "/v1/ui/connect/import", json={"source": "spare", "name": "work"})
    assert r.status_code == 409 and r.json()["code"] == "NAME_TAKEN" and r.json()["params"] == {"suggested_name": "work-2"}
    r = rec.call("POST", "/v1/ui/connect/import", json={"source": "priv"})
    assert r.status_code == 400 and r.json()["code"] == "IMPORT_UNSUPPORTED" and r.json()["params"] == {"type": "crypt"}
    r = rec.call("POST", "/v1/ui/connect/import", json={"source": "ghost"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    r = rec.call("POST", "/v1/ui/connect/import", json={"source": "box1"})
    assert r.status_code == 400 and r.json()["code"] == "IMPORT_UNSUPPORTED" and r.json()["params"] == {"type": "box"}
    r = rec.call("POST", "/v1/ui/connect/import", json={"source": "spare", "name": "Bad Name"})
    assert r.status_code == 400 and r.json()["code"] == "BAD_NAME"
    r = rec.call("POST", "/v1/ui/connect/import", json={"name": "x"})
    assert r.status_code == 422
    out = capsys.readouterr()
    blob = "\n".join(rec.bodies + brec.bodies + [out.out, out.err, caplog.text])
    assert "imp-at-1234" not in blob and "imp-rt-5678" not in blob


def test_an_encrypted_personal_config_is_reported_and_refused(browser, tmp_path, monkeypatch):
    path = tmp_path / "enc.conf"
    path.write_text("# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\nabcdefgh\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    client, headers = browser
    got = client.get("/v1/ui/connect/importable").json()
    assert got["encrypted"] is True and got["remotes"] == []
    r = client.post("/v1/ui/connect/import", headers=headers, json={"source": "x"})
    assert r.status_code == 409 and r.json()["code"] == "IMPORT_ENCRYPTED"


def test_import_can_ask_a_question_too(browser, tmp_path, monkeypatch):
    path = tmp_path / "personal.conf"
    path.write_text(f"[icl]\ntype = iclouddrive\napple_id = me@example.com\npassword = OBSCURED-xyz\n"
                    f"fake_root = {_clouds(tmp_path, 'icl')}\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    client, headers = browser
    r = client.post("/v1/ui/connect/import", headers=headers, json={"source": "icl"})
    assert r.status_code == 202 and r.json()["question"]["name"] == "config_2fa" and "OBSCURED-xyz" not in r.text
    r2 = client.post(f"/v1/ui/connect/flows/{r.json()['flow_id']}/answer", headers=headers, json={"result": CODE})
    assert r2.status_code == 200 and r2.json()["imported_from"] == "icl" and r2.json()["scoped"] is False


# ---- Test results are coded

def test_connection_test_is_worded_from_a_code(browser, tmp_path):
    client, headers = browser
    rec = Recorder(client, headers)
    _connect(rec, "dropbox", tmp_path / "clouds" / "dropbox")
    ok = rec.call("POST", "/v1/ui/connections/dropbox/test").json()
    assert ok["ok"] is True and ok["code"] is None and ok["params"] == {}
    conf = tmp_path / "conf" / "rclone.conf"
    conf.write_text(conf.read_text().replace("[dropbox-ro]\n", "[dropbox-ro]\nfake_error = auth\n"))
    bad = rec.call("POST", "/v1/ui/connections/dropbox/test").json()
    assert bad["ok"] is False and bad["code"] == "AUTH_EXPIRED" and bad["error"]
    conf.write_text(conf.read_text().replace("fake_error = auth", "fake_error = network"))
    assert rec.call("POST", "/v1/ui/connections/dropbox/test").json()["code"] == "NETWORK"
    conf.write_text(conf.read_text().replace("fake_error = network", "fake_error = other"))
    other = rec.call("POST", "/v1/ui/connections/dropbox/test").json()
    assert other["code"] == "RCLONE_ERROR" and "something odd happened" in other["params"]["detail"]


# ---- the whole thing never shows a secret, and failed flows leave nothing

def test_failed_and_cancelled_flows_leave_no_remote_and_no_entry(browser, tmp_path, monkeypatch):
    client, headers = browser
    rec = Recorder(client, headers)
    conf, toml = tmp_path / "conf" / "rclone.conf", tmp_path / "conf" / "connections.toml"

    monkeypatch.setenv("FAKE_RCLONE_FAIL_CREATE", "1")                           # iCloud: rclone fails on create
    r = _icloud_start(rec, tmp_path)
    assert r.status_code == 409 and r.json()["code"] == "FLOW_FAILED" and PASSWORD not in r.text
    monkeypatch.delenv("FAKE_RCLONE_FAIL_CREATE")
    assert connections._conf_sections(conf) == {} and not toml.exists()

    flow_id = _icloud_start(rec, tmp_path).json()["flow_id"]                     # a question, then rclone blows up
    monkeypatch.setenv("FAKE_RCLONE_ECHO_ANSWER", "1")
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": "654321-SECRET"})
    assert r.status_code == 409 and r.json()["code"] == "FLOW_FAILED" and "654321-SECRET" not in r.text
    view = rec.call("GET", f"/v1/ui/connect/flows/{flow_id}").json()
    assert view["status"] == "failed" and view["code"] == "FLOW_FAILED" and view["question"] is None
    monkeypatch.delenv("FAKE_RCLONE_ECHO_ANSWER")
    assert connections._conf_sections(conf) == {} and not toml.exists()

    flow_id = _icloud_start(rec, tmp_path).json()["flow_id"]                     # cancelled while asking
    assert rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/cancel").json() == {"ok": True}
    assert connections._conf_sections(conf) == {} and not toml.exists()
    assert not any(PASSWORD in b or "654321-SECRET" in b for b in rec.bodies)


def test_a_connections_toml_write_failure_over_http_is_coded_and_leaves_nothing(browser, tmp_path, monkeypatch):
    client, headers = browser
    rec = Recorder(client, headers)
    r = rec.call("POST", "/v1/ui/connect/start", json={"provider": "dropbox"})
    flow_id = r.json()["flow_id"]
    _sign_in(r.json()["auth_url"], _clouds(tmp_path, "dropbox"))
    _flow_done(client, flow_id, headers)
    monkeypatch.setattr(connections, "_write_entries", lambda c, e: (_ for _ in ()).throw(OSError("disk on fire")))
    r = rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/finish", json={})
    assert r.status_code == 409 and r.json()["code"] == "CONFIG_WRITE" and "disk on fire" not in r.text
    assert connections._conf_sections(tmp_path / "conf" / "rclone.conf") == {}


def test_nothing_secret_is_ever_logged_by_the_server(browser, tmp_path, caplog, capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))
    client, headers = browser
    rec = Recorder(client, headers)
    flow_id = _icloud_start(rec, tmp_path).json()["flow_id"]
    rec.call("POST", f"/v1/ui/connect/flows/{flow_id}/answer", json={"result": CODE})
    _, block = _pasted(_clouds(tmp_path, "dropbox"))
    pid = rec.call("POST", "/v1/ui/connect/paste", json={"provider": "dropbox", "token": block}).json()["flow_id"]
    rec.call("POST", f"/v1/ui/connect/flows/{pid}/finish", json={})
    out = capsys.readouterr()
    blob = "\n".join([out.out, out.err, caplog.text, log.read_text()])
    for secret in (PASSWORD, CODE, "pasted-at-a1b2c3d4", "pasted-rt-e5f6a7b8"):
        assert secret not in blob
