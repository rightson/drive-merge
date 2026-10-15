"""The Setup screen's server side (docs/SETUP.md sections C and D): setup state, the human-only routes, the connect
job against a fake rclone, adopting, testing, enabling writing, and `drivemerge ui` creating a missing config.

The fake rclone (tests/fake_rclone.py) behaves like rclone v1.71.1's `config create/update --non-interactive`: it
writes the remote at once, prints the sign-in URL on stderr and blocks until the test plays the browser.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import threading
import time
import tomllib

import pytest
from fake_rclone import FakeRclone
from fastapi.testclient import TestClient

from drivemerge import cli, db, ui_api
from drivemerge import config as config_mod
from drivemerge.config import Config, RemoteConfig, ensure_config, load_config
from drivemerge.service import Service

UI = "ui-token-one-time"
API = "api-token"
AUTH = {"Authorization": f"Bearer {API}"}
SECRETS = ("PASSWORD-SECRET-1", "CLIENT-SECRET-2", "TOKEN-SECRET-3", "CODE-SECRET-4")


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def fake(tmp_path):
    return FakeRclone(tmp_path)


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "drivemerge" / "config.toml"
    assert ensure_config(path)
    return path


@pytest.fixture
def config(cfg_path, fake, tmp_path):
    cfg = load_config(cfg_path)
    cfg.db_path = str(tmp_path / "catalog.db")
    cfg.rclone_binary, cfg.rclone_config = fake.binary, fake.config
    return cfg


@pytest.fixture
def service(config, cfg_path):
    svc = Service(config, config_path=cfg_path)
    yield svc
    svc.conn.close()


def make_app(service, config, cfg_path, *, approvals=True, api_token=API, path=True):
    return ui_api.create_ui_app(service, config=config, api_token=api_token, ui_token=UI,
                                approvals_enabled=approvals, config_path=cfg_path if path else None)


@pytest.fixture
def app(service, config, cfg_path):
    return make_app(service, config, cfg_path)


def login(app):
    client = TestClient(app)
    r = client.post("/v1/ui/session", json={"token": UI})
    assert r.status_code == 200, r.text
    return client, {"X-CSRF-Token": r.json()["csrf"]}


@pytest.fixture
def browser(app):
    return login(app)


def job_view(client, job_id):
    return client.get(f"/v1/jobs/{job_id}").json()


def wait_job(client, job_id, *, stage=None, timeout=15.0, seen=None):
    """Poll until the job is no longer running, or (with `stage`) until it reaches that progress stage.
    Every view is appended to `seen`, so a test can check that no view ever held a secret."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = job_view(client, job_id)
        if seen is not None:
            seen.append(json.dumps(view))
        if stage is not None and view["progress"].get("stage") == stage:
            return view
        if stage is None and view["status"] != "running":
            return view
        if stage is not None and view["status"] != "running":
            raise AssertionError(f"job ended before stage {stage}: {view}")
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {stage or 'the end'}: {view}")


def start_connect(client, headers, provider="dropbox", name="dbx", **extra):
    r = client.post("/v1/ui/setup/connect", headers=headers, json={"provider": provider, "name": name, **extra})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def toml_remotes(path):
    return tomllib.loads(path.read_text(encoding="utf-8")).get("remotes", [])


def item(path, size=3, **kw):
    return {"Path": path, "Name": path.rsplit("/", 1)[-1], "Size": size, "IsDir": False,
            "ModTime": "2024-01-01T00:00:00Z", "MimeType": "text/plain", **kw}


# ----------------------------------------------------------------- Service.setup_state / reload_config

def test_setup_state_of_a_fresh_install(service, cfg_path, fake):
    state = service.setup_state()
    assert state == {
        "config_path": str(cfg_path),
        "rclone": {"found": True, "version": "v1.71.1", "ok": True, "binary": fake.binary},
        "remotes": [], "rclone_remotes": [], "scanned": False, "identity_built": False, "execution_enabled": False,
    }


def test_setup_state_when_rclone_is_missing_or_old(config, tmp_path, monkeypatch):
    config.rclone_binary = str(tmp_path / "no-such-rclone")
    state = Service(config).setup_state()
    assert state["rclone"] == {"found": False, "version": None, "ok": False, "binary": config.rclone_binary}
    assert state["rclone_remotes"] == []
    monkeypatch.setenv("FAKE_RCLONE_VERSION", "v1.60.0")
    config.rclone_binary = FakeRclone(tmp_path / "old").binary
    config.rclone_config = None
    os.environ["FAKE_RCLONE_CONFIG"] = str(tmp_path / "old" / "r.conf")
    state = Service(config).setup_state()
    assert state["rclone"]["found"] is True and state["rclone"]["ok"] is False and state["rclone"]["version"] == "v1.60.0"


def test_setup_state_lists_remotes_with_their_scans_and_marks_rclone_remotes_already_in_config(service, fake, catalog):
    fake.seed("gd-ro", "drive")
    fake.seed("work-od", "onedrive")
    fake.seed("s3box", "s3")
    service.reload_config(Config(
        db_path=service.config.db_path, rclone_binary=fake.binary, rclone_config=fake.config,
        remotes=[RemoteConfig("gdrive", "drive", "gd-ro:", "gd-rw:"), RemoteConfig("disk", "local", "/mnt/x")]))
    service.conn.execute("INSERT INTO scans(remote_id, status, started_at, finished_at) "
                         "SELECT id, 'complete', ?, ? FROM remotes WHERE name = 'gdrive'", (db.utcnow(), db.utcnow()))
    state = service.setup_state()
    by_name = {r["name"]: r for r in state["remotes"]}
    assert by_name["gdrive"]["read"] == "gd-ro:" and by_name["gdrive"]["write"] == "gd-rw:"
    assert by_name["gdrive"]["writable"] is True and by_name["disk"]["writable"] is False
    assert by_name["gdrive"]["last_scan"]["status"] == "complete" and by_name["disk"]["last_scan"] is None
    assert {"files", "bytes"} <= set(by_name["gdrive"])
    assert {r["name"]: (r["type"], r["provider"], r["in_config"]) for r in state["rclone_remotes"]} == {
        "gd-ro": ("drive", "drive", True), "work-od": ("onedrive", "onedrive", False), "s3box": ("s3", "other", False)}
    assert state["scanned"] is False                      # `disk` has never been scanned


def test_scanned_and_identity_built(service, fake):
    service.reload_config(Config(db_path=service.config.db_path, rclone_binary=fake.binary,
                                 rclone_config=fake.config, remotes=[RemoteConfig("a", "dropbox", "a:")]))
    fake.seed("a", "dropbox")
    fake.set_files("a", [item("x.txt")])
    assert service.setup_state()["scanned"] is False
    service.scan("a")
    state = service.setup_state()
    assert state["scanned"] is True and state["identity_built"] is False
    service.rebuild_identity()
    assert service.setup_state()["identity_built"] is True
    time.sleep(0.01)
    service.scan("a")                                     # a newer scan than the last identity rebuild
    assert service.setup_state()["identity_built"] is False


def test_the_rclone_listing_is_cached_for_ten_seconds_and_can_be_forgotten(service, fake, monkeypatch):
    def listings():
        return sum(1 for c in fake.calls() if "listremotes" in c)

    service.setup_state()
    service.setup_state()
    assert listings() == 1
    fake.seed("late", "dropbox")
    assert service.setup_state()["rclone_remotes"] == []
    service.forget_rclone()
    assert [r["name"] for r in service.setup_state()["rclone_remotes"]] == ["late"] and listings() == 2
    monkeypatch.setattr("drivemerge.service.SETUP_CACHE_S", 0.0)
    service.setup_state()
    assert listings() == 3


def test_reload_config_syncs_remotes_and_rebuilds_rclone_only_when_its_settings_changed(service, fake, tmp_path):
    first = service.rclone
    new = Config(db_path=service.config.db_path, rclone_binary=fake.binary, rclone_config=fake.config,
                 remotes=[RemoteConfig("added", "dropbox", "added:")])
    service.reload_config(new)
    assert service.config is new and service.rclone is first
    assert service.conn.execute("SELECT provider, read_remote, write_remote FROM remotes WHERE name = 'added'"
                                ).fetchone()[:] == ("dropbox", "added:", None)
    other = Config(db_path=service.config.db_path, rclone_binary="/elsewhere/rclone", rclone_config=fake.config)
    service.reload_config(other)
    assert service.rclone is not first and service.rclone.binary == "/elsewhere/rclone"


# ----------------------------------------------------------------- GET /v1/ui/setup, channels

def test_setup_route_adds_needed(browser):
    client, _ = browser
    body = client.get("/v1/ui/setup").json()
    assert body["needed"] is True and body["remotes"] == [] and body["rclone"]["found"] is True


def test_setup_get_and_test_use_the_normal_channel(app, fake, config):
    fake.seed("dbx", "dropbox")
    assert TestClient(app).get("/v1/ui/setup").status_code == 401
    bearer = TestClient(app, headers=AUTH)
    assert bearer.get("/v1/ui/setup").status_code == 200
    assert bearer.post("/v1/ui/setup/test", json={"name": "nope"}).status_code == 404


def test_needed_is_false_once_every_cloud_is_scanned(browser, fake):
    client, headers = browser
    fake.seed("dbx", "dropbox")
    fake.set_files("dbx", [item("a.txt")])
    assert client.post("/v1/ui/setup/adopt", headers=headers, json={"name": "dbx"}).status_code == 200
    assert client.get("/v1/ui/setup").json()["needed"] is True                       # configured, never scanned
    job = client.post("/v1/ui/rescan", headers=headers, json={}).json()["job_id"]
    assert wait_job(client, job)["status"] == "done"
    state = client.get("/v1/ui/setup").json()
    assert state["needed"] is False and state["scanned"] is True and state["identity_built"] is True
    assert state["remotes"][0]["files"] == 1


HUMAN_ROUTES = [
    ("POST", "/v1/ui/setup/connect", {"provider": "dropbox", "name": "dbx"}),
    ("POST", "/v1/ui/setup/connect/abc/answer", {"result": "x"}),
    ("POST", "/v1/ui/setup/connect/abc/cancel", None),
    ("POST", "/v1/ui/setup/adopt", {"name": "dbx"}),
    ("POST", "/v1/ui/setup/enable-write", {"name": "dbx"}),
]


@pytest.mark.parametrize("method, path, body", HUMAN_ROUTES)
def test_the_human_routes_refuse_every_other_channel(app, service, config, cfg_path, fake, method, path, body):
    fake.seed("dbx", "dropbox")
    before = cfg_path.read_text()
    anon, bearer = TestClient(app), TestClient(app, headers=AUTH)
    client, headers = login(app)
    send = lambda c, **kw: c.request(method, path, json=body, **kw)
    assert send(anon).status_code == 401
    r = send(bearer, headers={"X-CSRF-Token": "x"})
    assert r.status_code == 403 and r.json()["code"] == "HUMAN_ONLY"
    r = send(client)                                                           # session, no CSRF header
    assert r.status_code == 403 and r.json()["code"] == "BAD_CSRF"
    r = send(client, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403 and r.json()["code"] == "BAD_CSRF"
    r = send(client, headers={**headers, "Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "BAD_ORIGIN"
    assert cfg_path.read_text() == before and fake.sections() == {"dbx": {"type": "dropbox"}}
    assert not any(c[:2] == ["config", "create"] or "create" in c for c in fake.calls() if "config" in c)


@pytest.mark.parametrize("method, path, body", HUMAN_ROUTES)
def test_a_server_started_by_an_agent_has_no_setup_writes(service, config, cfg_path, method, path, body):
    app = make_app(service, config, cfg_path, approvals=False)
    client, headers = login(app)
    r = client.request(method, path, json=body, headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "APPROVALS_DISABLED"
    assert "rclone config" in r.json()["error"] and "scan --all" in r.json()["error"]
    assert client.get("/v1/ui/setup").status_code == 200                       # reading stays possible


@pytest.mark.parametrize("path, body", [
    ("/v1/ui/setup/connect", {"provider": "dropbox", "name": "dbx"}),
    ("/v1/ui/setup/adopt", {"name": "dbx"}),
    ("/v1/ui/setup/enable-write", {"name": "dbx"}),
])
def test_without_a_config_path_the_write_routes_say_so(service, config, cfg_path, path, body):
    client, headers = login(make_app(service, config, cfg_path, path=False))
    r = client.post(path, headers=headers, json=body)
    assert r.status_code == 409 and r.json()["code"] == "NO_CONFIG_PATH"


# ----------------------------------------------------------------- connect: refusals

@pytest.mark.parametrize("name", ["", " ", "-x", "_x", "a b", "a/b", "a:b", "x" * 33, "ü", "a.b", "..", "a\n"])
def test_connect_refuses_bad_names(browser, fake, name):
    client, headers = browser
    r = client.post("/v1/ui/setup/connect", headers=headers, json={"provider": "dropbox", "name": name})
    assert r.status_code == 400 and r.json()["code"] == "BAD_NAME", name
    assert fake.calls() == [] or not any("create" in c for c in fake.calls())


@pytest.mark.parametrize("name", ["a", "A1", "my-cloud_2", "x" * 32])
def test_connect_accepts_good_names(browser, fake, name):
    client, headers = browser
    job = start_connect(client, headers, name=name)
    client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers)
    assert wait_job(client, job)["code"] == "CANCELLED"


def test_connect_refuses_a_name_rclone_or_drive_merge_already_has(browser, fake, config, service, cfg_path):
    client, headers = browser
    fake.seed("taken", "dropbox")
    r = client.post("/v1/ui/setup/connect", headers=headers, json={"provider": "dropbox", "name": "taken"})
    assert r.status_code == 409 and r.json()["code"] == "REMOTE_EXISTS" and r.json()["params"] == {"name": "taken"}
    assert not any("create" in c for c in fake.calls())
    # a server whose config already has a cloud called "mine" that reads through the rclone remote "viarclone"
    config_mod.add_remote_block(cfg_path, RemoteConfig("mine", "other", "viarclone:"))
    fresh = load_config(cfg_path)
    fresh.db_path, fresh.rclone_binary, fresh.rclone_config = config.db_path, fake.binary, fake.config
    client, headers = login(make_app(Service(fresh, config_path=cfg_path), fresh, cfg_path))
    for name in ("mine", "viarclone"):                # a drive-merge name, and an rclone name used in a read path
        r = client.post("/v1/ui/setup/connect", headers=headers, json={"provider": "dropbox", "name": name})
        assert r.status_code == 409 and r.json()["code"] == "REMOTE_EXISTS", name
    assert not any("create" in c for c in fake.calls())


def test_connect_without_rclone(service, config, cfg_path, tmp_path):
    config = dataclasses.replace(config, rclone_binary=str(tmp_path / "no-such-rclone"))
    service.reload_config(config)
    client, headers = login(make_app(service, config, cfg_path))
    r = client.post("/v1/ui/setup/connect", headers=headers, json={"provider": "dropbox", "name": "dbx"})
    assert r.status_code == 409 and r.json()["code"] == "RCLONE_MISSING"
    assert r.json()["params"] == {"binary": config.rclone_binary}


def test_one_connect_at_a_time(browser):
    client, headers = browser
    first = start_connect(client, headers, name="one")
    r = client.post("/v1/ui/setup/connect", headers=headers, json={"provider": "dropbox", "name": "two"})
    assert r.status_code == 409 and r.json()["code"] == "JOB_BUSY" and r.json()["params"]["job"] == first
    r = client.post("/v1/ui/rescan", headers=headers, json={})
    assert r.status_code in (400, 409)
    client.post(f"/v1/ui/setup/connect/{first}/cancel", headers=headers)
    wait_job(client, first)


def test_connect_validates_its_body(browser, fake):
    client, headers = browser
    post = lambda **b: client.post("/v1/ui/setup/connect", headers=headers, json=b)
    assert post(provider="box", name="x").status_code == 422
    assert post(provider="dropbox").status_code == 422
    assert post(provider="dropbox", name="x", surprise=1).status_code == 422
    assert post(provider="dropbox", name="x", client_id="abc").json()["code"] == "BAD_REQUEST"
    assert post(provider="dropbox", name="x", password="p").json()["code"] == "BAD_REQUEST"
    r = post(provider="iclouddrive", name="ic", apple_id="me@example.com")
    assert r.status_code == 400 and r.json()["code"] == "MISSING_FIELD" and r.json()["params"] == {"field": "password"}
    r = post(provider="iclouddrive", name="ic", password="p")
    assert r.json()["code"] == "MISSING_FIELD" and r.json()["params"] == {"field": "apple_id"}
    assert not any("create" in c for c in fake.calls())


# ----------------------------------------------------------------- connect: the job

def test_dropbox_connect_end_to_end(browser, fake, cfg_path, config, service):
    client, headers = browser
    job = start_connect(client, headers)
    view = wait_job(client, job, stage="signin")
    assert view["kind"] == "connect" and view["status"] == "running"
    assert view["progress"] == {"stage": "signin", "name": "dbx", "provider": "dropbox", "mode": "new",
                                "auth_url": "http://127.0.0.1:53682/auth?state=FAKESTATE123"}
    assert fake.sections()["dbx"]["type"] == "dropbox"                         # rclone already holds the half-made remote
    assert toml_remotes(cfg_path) == []                                        # drive-merge has not recorded it yet
    fake.finish_browser()
    done = wait_job(client, job)
    assert done["status"] == "done" and done["error"] is None and "code" not in done
    remote = done["result"]["remote"]
    assert remote["name"] == "dbx" and remote["provider"] == "dropbox" and remote["read"] == "dbx:"
    assert remote["write"] is None and remote["writable"] is False and remote["last_scan"] is None
    assert done["progress"]["stage"] == "done"
    assert toml_remotes(cfg_path) == [{"name": "dbx", "provider": "dropbox", "read": "dbx:"}]    # read-only
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
    # the sign-in questions went to rclone in the order rclone asked them, config_is_local answered by the server
    updates = [c for c in fake.calls() if "update" in c]
    assert len(updates) == 1 and updates[0][updates[0].index("--result") + 1] == "true"
    # reload_config: the running server and the catalog know the new cloud at once
    assert [r["name"] for r in client.get("/v1/ui/config").json()["remotes"]] == ["dbx"]
    assert [r.name for r in service.config.remotes] == ["dbx"]
    state = client.get("/v1/ui/setup").json()
    assert state["needed"] is True and state["rclone_remotes"][0] == {
        "name": "dbx", "type": "dropbox", "provider": "dropbox", "in_config": True}
    # ... and it is scannable at once (the first scan of a cloud that was just connected)
    fake.set_files("dbx", [item("a.txt"), item("d/b.txt")])
    scan = client.post("/v1/ui/rescan", headers=headers, json={"remotes": ["dbx"]}).json()["job_id"]
    result = wait_job(client, scan)
    assert result["status"] == "done", result
    state = client.get("/v1/ui/setup").json()
    assert state["remotes"][0]["files"] == 2 and state["scanned"] and state["needed"] is False


def test_drive_connect_is_read_only_scoped_and_keeps_the_client_secret_out_of_everything(browser, fake, cfg_path, capsys):
    client, headers = browser
    seen = []
    job = start_connect(client, headers, provider="drive", name="gdrive", client_id="my-id.apps.example",
                        client_secret=SECRETS[1])
    wait_job(client, job, stage="signin", seen=seen)
    stanza = fake.sections()["gdrive"]
    assert stanza["scope"] == "drive.readonly" and stanza["client_id"] == "my-id.apps.example"
    create = next(c for c in fake.calls() if "create" in c)
    assert create[create.index("create"):] == ["create", "gdrive", "drive", "scope=drive.readonly",
                                              "client_id=my-id.apps.example", f"client_secret={SECRETS[1]}",
                                              "--non-interactive"]
    fake.finish_browser()
    done = wait_job(client, job, seen=seen)
    assert done["status"] == "done"
    block = toml_remotes(cfg_path)[0]
    assert block == {"name": "gdrive", "provider": "drive", "read": "gdrive:",
                     "extra_args": ["--fast-list", "--drive-skip-shortcuts"]}
    assert "write" not in block
    out = capsys.readouterr()
    for text in (*seen, json.dumps(done), cfg_path.read_text(), out.out, out.err):
        assert SECRETS[1] not in text


def test_drive_connect_without_a_client_id_passes_only_the_scope(browser, fake):
    client, headers = browser
    job = start_connect(client, headers, provider="drive", name="gdrive")
    wait_job(client, job, stage="signin")
    create = next(c for c in fake.calls() if "create" in c)
    assert create[create.index("create"):] == ["create", "gdrive", "drive", "scope=drive.readonly", "--non-interactive"]
    client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers)
    wait_job(client, job)


def test_onedrive_asks_which_drive_after_sign_in(browser, fake, cfg_path):
    client, headers = browser
    job = start_connect(client, headers, provider="onedrive", name="od")
    wait_job(client, job, stage="signin")
    r = client.post(f"/v1/ui/setup/connect/{job}/answer", headers=headers, json={"result": "x"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_WAITING" and r.json()["params"] == {"job": job}
    fake.finish_browser()
    view = wait_job(client, job, stage="question")
    assert view["status"] == "running" and view["result"] is None
    assert view["progress"]["question"] == {
        "name": "config_driveid", "help": "Select which drive to use.", "default": "",
        "examples": [{"value": "drive-personal", "help": "OneDrive (personal)"},
                     {"value": "drive-docs", "help": "Documents (business)"}],
        "password": False, "required": True, "exclusive": True, "type": "string"}
    assert set(view["progress"]) == {"stage", "name", "provider", "mode", "question"}      # no stale auth_url
    assert toml_remotes(cfg_path) == []
    r = client.post(f"/v1/ui/setup/connect/{job}/answer", headers=headers, json={"result": "drive-docs"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    done = wait_job(client, job)
    assert done["status"] == "done" and done["result"]["remote"]["provider"] == "onedrive"
    assert fake.sections()["od"]["drive_id"] == "drive-docs"
    again = client.post(f"/v1/ui/setup/connect/{job}/answer", headers=headers, json={"result": "x"})
    assert again.status_code == 409 and again.json()["code"] == "NOT_WAITING"          # one answer per question


def test_an_answer_is_validated(browser):
    client, headers = browser
    job = start_connect(client, headers)
    wait_job(client, job, stage="signin")
    post = lambda body, j=job: client.post(f"/v1/ui/setup/connect/{j}/answer", headers=headers, json=body)
    assert post({}).status_code == 422 and post({"result": 5}).status_code == 422
    assert post({"result": "x", "extra": 1}).status_code == 422
    assert post({"result": "x" * 70000}).status_code == 422
    assert post({"result": "x"}, "nojob").status_code == 404
    client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers)
    wait_job(client, job)
    rescan = client.post("/v1/ui/rescan", headers=headers, json={}).json()
    if "job_id" in rescan:
        assert post({"result": "x"}, rescan["job_id"]).status_code == 404            # not a connect job
        wait_job(client, rescan["job_id"])


def test_authorising_on_another_machine_asks_for_a_token_and_never_shows_it_back(browser, fake, cfg_path, capsys, caplog):
    client, headers = browser
    seen = []
    job = start_connect(client, headers, remote_auth=True)
    view = wait_job(client, job, stage="question", seen=seen)
    assert view["progress"]["question"]["name"] == "config_token" and view["progress"]["question"]["exclusive"] is False
    assert view["progress"]["question"]["required"] is True and "authorize" in view["progress"]["question"]["help"]
    assert [c[c.index("--result") + 1] for c in fake.calls() if "update" in c] == ["false"]    # no local browser
    answer = client.post(f"/v1/ui/setup/connect/{job}/answer", headers=headers, json={"result": SECRETS[2]})
    assert answer.status_code == 200 and SECRETS[2] not in answer.text
    done = wait_job(client, job, seen=seen)
    assert done["status"] == "done"
    out = capsys.readouterr()
    for text in (*seen, json.dumps(done), cfg_path.read_text(), out.out, out.err, caplog.text):
        assert SECRETS[2] not in text
    assert fake.sections()["dbx"]["token"] == SECRETS[2]                       # rclone keeps credentials, not drive-merge


def test_icloud_needs_the_apple_id_and_password_up_front_and_then_the_two_factor_code(browser, fake, cfg_path, capsys, caplog):
    client, headers = browser
    seen = []
    job = start_connect(client, headers, provider="iclouddrive", name="icloud", apple_id="me@example.com",
                        password=SECRETS[0])
    view = wait_job(client, job, stage="question", seen=seen)
    assert SECRETS[0] not in json.dumps(view)
    assert view["progress"]["question"]["name"] == "config_2fa" and "retry" not in view["progress"]["question"]
    stanza = fake.sections()["icloud"]
    assert stanza["apple_id"] == "me@example.com" and SECRETS[0] not in stanza["password"]          # rclone obscures it
    r = client.post(f"/v1/ui/setup/connect/{job}/answer", headers=headers, json={"result": "000000"})
    assert r.status_code == 200
    view = wait_job(client, job, stage="question", seen=seen)                  # rejected: asked again, flagged
    deadline = time.monotonic() + 5
    while "retry" not in view["progress"]["question"] and time.monotonic() < deadline:
        view = job_view(client, job)
    assert view["progress"]["question"]["retry"] is True
    assert "not accepted" not in json.dumps(view)                               # rclone's own text is not forwarded
    client.post(f"/v1/ui/setup/connect/{job}/answer", headers=headers, json={"result": "123456"})
    done = wait_job(client, job, seen=seen)
    assert done["status"] == "done" and done["result"]["remote"]["provider"] == "iclouddrive"
    out = capsys.readouterr()
    for text in (*seen, json.dumps(done), cfg_path.read_text(), out.out, out.err, caplog.text):
        assert SECRETS[0] not in text
    assert toml_remotes(cfg_path) == [{"name": "icloud", "provider": "iclouddrive", "read": "icloud:"}]


def test_only_helper_urls_are_forwarded_as_the_sign_in_link(browser, fake, monkeypatch):
    """A line that carries some other URL (a hostile or buggy rclone) never becomes `auth_url`."""
    script = fake.binary
    text = open(script).read().replace("http://127.0.0.1:53682/auth?state=FAKESTATE123",
                                       "http://evil.example/auth?state=FAKESTATE123")
    open(script, "w").write(text)
    client, headers = browser
    job = start_connect(client, headers)
    time.sleep(0.5)
    view = job_view(client, job)
    assert "auth_url" not in view["progress"] and view["progress"]["stage"] == "working"
    client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers)
    wait_job(client, job)


# ----------------------------------------------------------------- connect: cancel, timeouts and failures delete the half-made remote

def assert_nothing_left(fake, cfg_path, name="dbx"):
    assert name not in fake.sections()
    assert toml_remotes(cfg_path) == []


def test_cancel_while_waiting_for_the_browser_stops_rclone_and_deletes_the_remote(browser, fake, cfg_path):
    client, headers = browser
    job = start_connect(client, headers)
    wait_job(client, job, stage="signin")
    assert "dbx" in fake.sections()
    assert client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers).json() == {"ok": True}
    view = wait_job(client, job)
    assert view["status"] == "failed" and view["code"] == "CANCELLED" and view["params"] == {"name": "dbx"}
    assert view["error"] and view["progress"]["stage"] == "failed" and view["result"] is None
    assert_nothing_left(fake, cfg_path)
    assert client.get("/v1/ui/setup").json()["rclone_remotes"] == []           # the cached listing was dropped
    # the next sign-in can start straight away (the first rclone is dead, the name is free)
    again = start_connect(client, headers)
    client.post(f"/v1/ui/setup/connect/{again}/cancel", headers=headers)
    assert wait_job(client, again)["code"] == "CANCELLED"


def test_cancel_while_a_question_is_waiting(browser, fake, cfg_path):
    client, headers = browser
    job = start_connect(client, headers, provider="onedrive", name="od")
    wait_job(client, job, stage="signin")
    fake.finish_browser()
    wait_job(client, job, stage="question")
    client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers)
    view = wait_job(client, job)
    assert view["code"] == "CANCELLED"
    assert_nothing_left(fake, cfg_path, "od")


def test_cancel_is_idempotent_and_needs_a_known_connect_job(browser, fake, cfg_path):
    client, headers = browser
    job = start_connect(client, headers)
    client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers)
    wait_job(client, job)
    assert client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers).json() == {"ok": True}
    assert client.post("/v1/ui/setup/connect/nojob/cancel", headers=headers).status_code == 404


def test_no_sign_in_in_time_fails_with_a_timeout_and_cleans_up(browser, fake, cfg_path, monkeypatch):
    monkeypatch.setattr(ui_api, "SIGNIN_TIMEOUT_S", 0.5)
    client, headers = browser
    job = start_connect(client, headers)
    view = wait_job(client, job)
    assert view["status"] == "failed" and view["code"] == "SIGNIN_TIMEOUT" and view["params"] == {"name": "dbx"}
    assert_nothing_left(fake, cfg_path)


def test_no_answer_in_time_fails_with_a_timeout_and_cleans_up(browser, fake, cfg_path, monkeypatch):
    monkeypatch.setattr(ui_api, "ANSWER_TIMEOUT_S", 0.5)
    client, headers = browser
    job = start_connect(client, headers, provider="onedrive", name="od")
    wait_job(client, job, stage="signin")
    fake.finish_browser()
    view = wait_job(client, job)
    assert view["code"] == "SIGNIN_TIMEOUT"
    assert_nothing_left(fake, cfg_path, "od")


def test_the_person_refusing_access_fails_the_job_and_cleans_up(browser, fake, cfg_path):
    client, headers = browser
    job = start_connect(client, headers)
    wait_job(client, job, stage="signin")
    fake.deny_browser()
    view = wait_job(client, job)
    assert view["status"] == "failed" and view["code"] == "RCLONE_ERROR"
    assert "access_denied" in view["params"]["detail"] and view["params"]["name"] == "dbx"
    assert_nothing_left(fake, cfg_path)


def test_rclone_failing_to_create_still_removes_what_it_wrote(browser, fake, cfg_path):
    fake.flag("fail_create")
    client, headers = browser
    view = wait_job(client, start_connect(client, headers))
    assert view["code"] == "RCLONE_ERROR" and "boom" in view["params"]["detail"]
    assert_nothing_left(fake, cfg_path)


def test_rclone_failing_while_continuing(browser, fake, cfg_path):
    fake.flag("fail_continue")                     # `config create` works; the first `config update` blows up
    client, headers = browser
    fake.seed("keep", "dropbox")
    view = wait_job(client, start_connect(client, headers))
    assert view["status"] == "failed" and view["code"] == "RCLONE_ERROR" and "exploded" in view["params"]["detail"]
    assert "dbx" not in fake.sections() and "keep" in fake.sections()          # only the failed job's remote went
    assert toml_remotes(cfg_path) == []


def test_an_rclone_that_echoes_the_answer_cannot_leak_it(browser, fake, cfg_path, capsys, caplog):
    client, headers = browser
    seen = []
    job = start_connect(client, headers, remote_auth=True)
    wait_job(client, job, stage="question", seen=seen)
    fake.flag("echo_answer")
    client.post(f"/v1/ui/setup/connect/{job}/answer", headers=headers, json={"result": SECRETS[3] + "-token"})
    view = wait_job(client, job, seen=seen)
    assert view["code"] == "RCLONE_ERROR"
    out = capsys.readouterr()
    for text in (*seen, json.dumps(view), out.out, out.err, caplog.text):
        assert SECRETS[3] not in text
    assert "***" in view["params"]["detail"]
    assert_nothing_left(fake, cfg_path)


def test_a_config_write_error_deletes_the_remote_and_leaves_the_file(browser, fake, cfg_path, monkeypatch):
    before = cfg_path.read_text()
    client, headers = browser
    job = start_connect(client, headers)
    wait_job(client, job, stage="signin")

    def broken(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", broken)
    fake.finish_browser()
    view = wait_job(client, job)
    assert view["status"] == "failed" and view["code"] == "CONFIG_WRITE" and view["params"] == {"name": "dbx"}
    assert cfg_path.read_text() == before and "dbx" not in fake.sections()


def test_a_layout_that_cannot_take_a_block_fails_the_job_and_deletes_the_remote(browser, fake, cfg_path):
    client, headers = browser
    job = start_connect(client, headers)
    wait_job(client, job, stage="signin")
    cfg_path.write_text(cfg_path.read_text() + '\nremotes = [{name = "x", provider = "other", read = "x:"}]\n')
    changed = cfg_path.read_text()
    fake.finish_browser()
    view = wait_job(client, job)
    assert view["code"] == "CONFIG_LAYOUT" and view["params"] == {"name": "dbx"}
    assert cfg_path.read_text() == changed and "dbx" not in fake.sections()


def test_a_name_that_appeared_in_the_config_meanwhile_is_refused_at_the_end(browser, fake, cfg_path):
    client, headers = browser
    job = start_connect(client, headers)
    wait_job(client, job, stage="signin")
    config_mod.add_remote_block(cfg_path, RemoteConfig("dbx", "other", "somewhere:"))      # someone edited the file
    fake.finish_browser()
    view = wait_job(client, job)
    assert view["code"] == "REMOTE_EXISTS" and view["params"] == {"name": "dbx"}
    assert "dbx" not in fake.sections()
    assert [r["read"] for r in toml_remotes(cfg_path)] == ["somewhere:"]


# ----------------------------------------------------------------- adopt

def test_adopt_adds_a_read_only_block_for_an_existing_rclone_remote(browser, fake, cfg_path, service):
    client, headers = browser
    fake.seed("work-od", "onedrive", token="KEEP")
    r = client.post("/v1/ui/setup/adopt", headers=headers, json={"name": "work-od"})
    assert r.status_code == 200
    remote = r.json()["remote"]
    assert remote["name"] == "work-od" and remote["provider"] == "onedrive" and remote["read"] == "work-od:"
    assert remote["write"] is None and remote["writable"] is False
    assert toml_remotes(cfg_path) == [{"name": "work-od", "provider": "onedrive", "read": "work-od:"}]
    assert [r.name for r in service.config.remotes] == ["work-od"]
    assert fake.sections()["work-od"] == {"type": "onedrive", "token": "KEEP"}           # rclone's own config untouched
    assert next(r for r in client.get("/v1/ui/setup").json()["rclone_remotes"])["in_config"] is True


def test_adopt_refusals(browser, fake, cfg_path):
    client, headers = browser
    fake.seed("have", "dropbox")
    post = lambda name: client.post("/v1/ui/setup/adopt", headers=headers, json={"name": name})
    assert post("have").status_code == 200
    r = post("have")
    assert r.status_code == 409 and r.json()["code"] == "REMOTE_EXISTS" and r.json()["params"] == {"name": "have"}
    r = post("ghost")
    assert r.status_code == 404 and r.json()["code"] == "REMOTE_NOT_FOUND" and r.json()["params"] == {"name": "ghost"}
    for bad in ("", "-x", "a b", "a:b", "../x"):
        assert post(bad).json()["code"] == "BAD_NAME"
    assert [r["name"] for r in toml_remotes(cfg_path)] == ["have"]


def test_adopt_unknown_types_become_provider_other(browser, fake):
    client, headers = browser
    fake.seed("bucket", "s3")
    assert client.post("/v1/ui/setup/adopt", headers=headers, json={"name": "bucket"}).json()["remote"]["provider"] == "other"


# ----------------------------------------------------------------- test

@pytest.fixture
def adopted(browser, fake):
    client, headers = browser
    fake.seed("gd", "drive")
    assert client.post("/v1/ui/setup/adopt", headers=headers, json={"name": "gd"}).status_code == 200
    return client


def test_test_ok(adopted):
    assert adopted.post("/v1/ui/setup/test", json={"name": "gd"}).json() == {"ok": True}


@pytest.mark.parametrize("kind, code, params", [
    ("auth", "AUTH_EXPIRED", {}), ("network", "NETWORK", {}), ("other", "RCLONE_ERROR", {"detail": "CRITICAL: something odd happened"}),
])
def test_test_words_the_failure_from_a_code(adopted, fake, kind, code, params):
    fake.fail_lsjson("gd", kind)
    body = adopted.post("/v1/ui/setup/test", json={"name": "gd"}).json()
    assert body["ok"] is False and body["code"] == code
    assert body["params"] == params


def test_test_not_found_when_rclone_lost_the_remote(adopted, fake):
    fake.seed("gd", "drive")
    path = open(fake.config, "w")
    path.close()                                      # the rclone config no longer has [gd]
    body = adopted.post("/v1/ui/setup/test", json={"name": "gd"}).json()
    assert body["ok"] is False and body["code"] == "NOT_FOUND"


def test_test_only_knows_configured_clouds(adopted, fake):
    fake.seed("not-in-drive-merge", "drive")
    r = adopted.post("/v1/ui/setup/test", json={"name": "not-in-drive-merge"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert adopted.post("/v1/ui/setup/test", json={}).status_code == 422
    assert not any("not-in-drive-merge:" in " ".join(c) for c in fake.calls())


# ----------------------------------------------------------------- enable writing

def test_enable_write_for_dropbox_records_the_same_remote(browser, fake, cfg_path, service):
    client, headers = browser
    fake.seed("dbx", "dropbox")
    client.post("/v1/ui/setup/adopt", headers=headers, json={"name": "dbx"})
    r = client.post("/v1/ui/setup/enable-write", headers=headers, json={"name": "dbx"})
    assert r.status_code == 200
    assert r.json()["remote"]["write"] == "dbx:" and r.json()["remote"]["writable"] is True
    assert toml_remotes(cfg_path) == [{"name": "dbx", "provider": "dropbox", "read": "dbx:", "write": "dbx:"}]
    assert service.config.remote("dbx").write == "dbx:"
    assert client.get("/v1/ui/config").json()["remotes"][0]["writable"] is True
    assert not any("create" in c for c in fake.calls() if "config" in c)                # no new sign-in
    again = client.post("/v1/ui/setup/enable-write", headers=headers, json={"name": "dbx"})
    assert again.status_code == 200 and again.json()["remote"]["write"] == "dbx:"


def test_enable_write_for_drive_signs_in_again_with_full_scope(browser, fake, cfg_path, service):
    client, headers = browser
    job = start_connect(client, headers, provider="drive", name="gdrive")
    wait_job(client, job, stage="signin")
    fake.finish_browser()
    assert wait_job(client, job)["status"] == "done"
    fake.ctl.joinpath("browser_done").unlink()
    r = client.post("/v1/ui/setup/enable-write", headers=headers,
                    json={"name": "gdrive", "client_id": "my-id", "client_secret": SECRETS[1]})
    assert r.status_code == 202
    rw = r.json()["job_id"]
    view = wait_job(client, rw, stage="signin")
    assert view["kind"] == "connect" and view["progress"]["name"] == "gdrive-rw" and view["progress"]["mode"] == "write"
    assert fake.sections()["gdrive-rw"]["scope"] == "drive" and fake.sections()["gdrive"]["scope"] == "drive.readonly"
    assert toml_remotes(cfg_path)[0].get("write") is None                      # nothing is writable until sign-in ends
    fake.finish_browser()
    done = wait_job(client, rw)
    assert done["status"] == "done" and done["result"]["remote"]["name"] == "gdrive"
    assert done["result"]["remote"]["write"] == "gdrive-rw:" and done["result"]["remote"]["read"] == "gdrive:"
    assert [r["name"] for r in toml_remotes(cfg_path)] == ["gdrive"]           # one block, `write` set; no second cloud
    assert toml_remotes(cfg_path)[0]["write"] == "gdrive-rw:"
    assert service.config.remote("gdrive").write == "gdrive-rw:"
    assert SECRETS[1] not in json.dumps(done) and SECRETS[1] not in cfg_path.read_text()


def test_cancelled_enable_write_removes_the_rw_remote_and_changes_nothing(browser, fake, cfg_path):
    client, headers = browser
    fake.seed("gdrive", "drive", scope="drive.readonly")
    client.post("/v1/ui/setup/adopt", headers=headers, json={"name": "gdrive"})
    before = cfg_path.read_text()
    job = client.post("/v1/ui/setup/enable-write", headers=headers, json={"name": "gdrive"}).json()["job_id"]
    wait_job(client, job, stage="signin")
    client.post(f"/v1/ui/setup/connect/{job}/cancel", headers=headers)
    view = wait_job(client, job)
    assert view["code"] == "CANCELLED" and view["params"] == {"name": "gdrive-rw"}
    assert "gdrive-rw" not in fake.sections() and "gdrive" in fake.sections()
    assert cfg_path.read_text() == before


def test_enable_write_refusals(browser, fake, cfg_path):
    client, headers = browser
    post = lambda **b: client.post("/v1/ui/setup/enable-write", headers=headers, json=b)
    r = post(name="nope")
    assert r.status_code == 404 and r.json()["code"] == "REMOTE_NOT_FOUND" and r.json()["params"] == {"name": "nope"}
    fake.seed("gdrive", "drive")
    fake.seed("gdrive-rw", "drive")
    client.post("/v1/ui/setup/adopt", headers=headers, json={"name": "gdrive"})
    r = post(name="gdrive")
    assert r.status_code == 409 and r.json()["code"] == "REMOTE_EXISTS" and r.json()["params"] == {"name": "gdrive-rw"}
    assert post(name="gdrive", surprise=1).status_code == 422


def test_enable_write_name_too_long_for_the_rw_suffix(browser, fake):
    client, headers = browser
    name = "g" * 31
    fake.seed(name, "drive")
    client.post("/v1/ui/setup/adopt", headers=headers, json={"name": name})
    r = client.post("/v1/ui/setup/enable-write", headers=headers, json={"name": name})
    assert r.status_code == 400 and r.json()["code"] == "BAD_NAME"


# ----------------------------------------------------------------- rescan of clouds that were never scanned

def test_rescan_works_for_a_never_scanned_remote_and_marks_the_first_scan_done(browser, fake):
    client, headers = browser
    for name in ("one", "two"):
        fake.seed(name, "dropbox")
        fake.set_files(name, [item("same.txt", dropbox="h1"), item(f"only-{name}.txt")])
        client.post("/v1/ui/setup/adopt", headers=headers, json={"name": name})
    assert client.get("/v1/ui/setup").json()["scanned"] is False
    job = client.post("/v1/ui/rescan", headers=headers, json={}).json()["job_id"]
    done = wait_job(client, job)
    assert done["status"] == "done", done
    assert {s["remote"] for s in done["result"]["scans"]} == {"one", "two"}
    state = client.get("/v1/ui/setup").json()
    assert state["scanned"] and state["identity_built"] and {r["name"]: r["files"] for r in state["remotes"]} == {
        "one": 2, "two": 2}
    assert all(r["last_scan"]["status"] == "complete" for r in state["remotes"])


# ----------------------------------------------------------------- `drivemerge ui` and `init`

def test_ui_creates_a_missing_config_and_hands_its_path_to_the_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "new" / "config.toml"
    seen = {}
    monkeypatch.setattr(ui_api, "run_ui", lambda cfg, **kw: seen.update(cfg=cfg, **kw))
    assert cli.main(["--config", str(path), "ui", "--no-browser"]) == 0
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "Created" in capsys.readouterr().out
    assert seen["config_path"] == path and seen["open_browser"] is False
    assert seen["cfg"].remotes == [] and len(seen["cfg"].api.token) >= 32
    first = path.read_text()
    seen.clear()
    assert cli.main(["--config", str(path), "ui", "--no-browser"]) == 0
    assert path.read_text() == first and "Created" not in capsys.readouterr().out         # an existing file is left alone
    assert seen["config_path"] == path


def test_other_commands_still_hint_at_init_when_the_config_is_missing(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "none.toml"), "remotes"]) == 1
    assert "drivemerge init" in capsys.readouterr().err


def test_run_ui_passes_the_config_path_to_the_app(config, cfg_path, monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(ui_api.uvicorn, "run", lambda app, **kw: captured.update(app=app))
    monkeypatch.setattr(ui_api, "approvals_allowed", lambda: (True, None))
    ui_api.run_ui(config, host="127.0.0.1", port=0, open_browser=False, config_path=cfg_path)
    token = capsys.readouterr().out.split("#token=")[1].split()[0]
    client = TestClient(captured["app"])
    assert client.get("/v1/ui/setup").status_code == 401                      # locked until a session is opened
    csrf = client.post("/v1/ui/session", json={"token": token}).json()["csrf"]
    assert client.get("/v1/ui/setup").json()["config_path"] == str(cfg_path)
    r = client.post("/v1/ui/setup/adopt", headers={"X-CSRF-Token": csrf}, json={"name": "x"})
    assert r.json()["code"] != "NO_CONFIG_PATH"                                # the write routes know where to write


# ----------------------------------------------------------------- the whole first run, in one go

def test_first_run_from_nothing_to_a_scanned_catalog(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.toml"
    ensure_config(path)
    cfg = load_config(path)
    cfg.db_path, cfg.rclone_binary, cfg.rclone_config = str(tmp_path / "c.db"), fake.binary, fake.config
    svc = Service(cfg, config_path=path)
    client, headers = login(make_app(svc, cfg, path))
    assert client.get("/v1/ui/setup").json()["needed"] is True
    fake.seed("old-box", "dropbox")
    fake.set_files("old-box", [item("a.txt", dropbox="aa")])
    fake.set_files("dbx", [item("a.txt", dropbox="aa"), item("b.txt")])
    client.post("/v1/ui/setup/adopt", headers=headers, json={"name": "old-box"})
    job = start_connect(client, headers)
    wait_job(client, job, stage="signin")
    fake.finish_browser()
    assert wait_job(client, job)["status"] == "done"
    scan = client.post("/v1/ui/rescan", headers=headers, json={}).json()["job_id"]
    assert wait_job(client, scan)["status"] == "done"
    state = client.get("/v1/ui/setup").json()
    assert state["needed"] is False and [r["name"] for r in state["remotes"]] == ["old-box", "dbx"]
    assert all(r["writable"] is False for r in state["remotes"])                          # read-only until enabled
    assert [r.name for r in load_config(path).remotes] == ["old-box", "dbx"]
    svc.conn.close()
