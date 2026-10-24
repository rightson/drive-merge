"""The Quarantine routes (docs/QUARANTINE.md section E) over HTTP, in both apps, against real local "clouds".

Plans are executed for real (rclone on local folders) so that what the routes list, restore and purge is exactly
what the executor parked. The human-channel rules are the same as for approve and execute: session, CSRF, origin,
approvals enabled, `execution_enabled`, the typed word. A bearer token and MCP never reach restore or purge.
"""

from __future__ import annotations

import json
import random
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from drivemerge import api as api_mod
from drivemerge import builders, db, executor, plans, ui_api
from drivemerge.config import parse_config
from drivemerge.models import SimulationReport
from drivemerge.scanner import scan_remote
from drivemerge.service import Service

API = "api-token"
UI = "ui-token-one-time"
AUTH = {"Authorization": f"Bearer {API}"}
QDIR = ".drive-merge-quarantine"

pytestmark = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")


def blob(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


# ----------------------------------------------------------------- the world

class World:
    """Two local folders standing in for two clouds, a catalog and a service over them."""

    def __init__(self, tmp_path):
        self.roots = {n: tmp_path / "clouds" / n for n in ("gd", "dbx")}
        for r in self.roots.values():
            r.mkdir(parents=True)
        self.config = parse_config({
            "database": {"path": str(tmp_path / "catalog.db")},
            "safety": {"execution_enabled": True},
            "api": {"token": API},
            "remotes": [{"name": "gd", "provider": "local", "read": str(self.roots["gd"]),
                         "write": str(self.roots["gd"]), "hash_types": ["md5", "sha1", "sha256"]},
                        {"name": "dbx", "provider": "local", "read": str(self.roots["dbx"]),
                         "write": str(self.roots["dbx"]), "hash_types": ["dropbox"]}],
        })
        self.service = Service(self.config)
        self.conn = self.service.conn

    def put(self, cloud: str, rel: str, data: bytes) -> None:
        p = self.roots[cloud] / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def exists(self, cloud: str, rel: str) -> bool:
        return (self.roots[cloud] / rel).exists()

    def scan(self) -> None:
        for r in self.config.remotes:
            assert scan_remote(self.conn, self.service.rclone, r).status == "complete"

    def rid(self, name: str) -> int:
        return db.remote_id(self.conn, name)

    def executed_plan(self, deletes, *, kind="dedupe", created_by="mcp") -> int:
        """Create, approve and really execute a plan of deletes: [(cloud, path, witness cloud, witness path)]."""
        ops = []
        for cloud, path, wcloud, wpath in deletes:
            row = self.conn.execute("SELECT * FROM entries WHERE remote_id = ? AND path = ? AND present = 1",
                                    (self.rid(cloud), path)).fetchone()
            ops.append(builders.op_from_entry(row, "delete", witness_remote_id=self.rid(wcloud), witness_path=wpath,
                                              reason="duplicate of the kept copy"))
        pid = plans.create_plan(self.conn, f"{kind} plan", kind, ops, created_by=created_by)
        snap = db.snapshot(self.conn)
        digest = plans.compute_digest(self.conn, pid, plans.load_ops(self.conn, pid), snap)
        plans.record_simulation(self.conn, pid, SimulationReport(plan_id=pid, digest=digest, ok=True), snap)
        plans.approve_plan(self.conn, pid, plans.short_code(digest), approved_by="test")
        self.digest = digest
        res = executor.execute_plan(self.conn, self.service.rclone, self.config, pid)
        assert res.status == "done", res
        return pid

    def two_deletes(self, tag: str = "a", seed: int = 1) -> int:
        """Two files deleted into quarantine, each with an identical live witness on the other cloud."""
        for n, name in enumerate(("one", "two")):
            data = blob(100 + n, seed + n)
            self.put("gd", f"docs/{tag}/{name}.bin", data)
            self.put("dbx", f"keep/{tag}/{name}.bin", data)
        self.scan()
        return self.executed_plan([("gd", f"docs/{tag}/{n}.bin", "dbx", f"keep/{tag}/{n}.bin") for n in ("one", "two")])

    def ops(self, pid: int) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT id FROM plan_ops WHERE plan_id = ? ORDER BY seq", (pid,))]

    def age(self, pid: int, days: int) -> None:
        when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("UPDATE plan_ops SET executed_at = ? WHERE plan_id = ?", (when, pid))


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.conn.close()


def make_ui(world, *, approvals=True):
    return ui_api.create_ui_app(world.service, config=world.config, api_token=API, ui_token=UI,
                                approvals_enabled=approvals)


def login(app):
    client = TestClient(app)
    r = client.post("/v1/ui/session", json={"token": UI})
    assert r.status_code == 200, r.text
    return client, {"X-CSRF-Token": r.json()["csrf"]}


@pytest.fixture
def app(world):
    return make_ui(world)


@pytest.fixture
def browser(app):
    return login(app)


@pytest.fixture
def bearer(app):
    return TestClient(app, headers=AUTH)


@pytest.fixture
def plain(world):
    """The bearer-only REST app (`drivemerge serve`)."""
    return TestClient(api_mod.create_app(world.service, token=API), headers=AUTH)


def wait(client, job_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running")


def run_job(client, headers, url, body):
    r = client.post(url, headers=headers, json=body)
    assert r.status_code == 202, r.text
    return wait(client, r.json()["job_id"])


def refused(client, headers, url, body, status, code):
    r = client.post(url, headers=headers, json=body)
    assert r.status_code == status and r.json()["code"] == code, (r.status_code, r.text)
    assert r.json()["error"]
    return r.json()


# ----------------------------------------------------------------- the read routes, in both apps

@pytest.mark.parametrize("which", ["ui-bearer", "ui-session", "plain"])
def test_the_read_routes_answer_in_both_apps(world, which, app, bearer, browser, plain):
    client = {"ui-bearer": bearer, "ui-session": browser[0], "plain": plain}[which]
    pid = world.two_deletes()
    report = client.get("/v1/quarantine").json()
    assert report["totals"]["files"] == 2 and [p["plan_id"] for p in report["plans"]] == [pid]
    assert [r["remote"] for r in report["remotes"]] == ["gd", "dbx"] and "quota" in report["remotes"][0]
    items = client.get(f"/v1/quarantine/plans/{pid}").json()
    assert items["total"] == 2 and {i["path"] for i in items["items"]} == {"docs/a/one.bin", "docs/a/two.bin"}
    assert all(i["catalog"]["witness_live"] is True for i in items["items"])
    assert client.get(f"/v1/quarantine/plans/{pid}?state=restored").json()["total"] == 0
    assert client.get(f"/v1/quarantine/plans/{pid}?remote=gd&path=docs&limit=1").json()["items"][0]["path"] \
        == "docs/a/one.bin"
    check = client.post(f"/v1/quarantine/plans/{pid}/check", json={"action": "restore"}).json()
    assert check["files"] == 2 and check["op_ids"] == world.ops(pid)
    check = client.post(f"/v1/quarantine/plans/{pid}/check", json={"action": "purge", "older_than_days": 30}).json()
    assert check["files"] == 0 and check["too_recent"]["files"] == 2


def test_reading_needs_credentials_and_changes_nothing(world, app, plain):
    pid = world.two_deletes()
    files = sorted(p.name for p in world.roots["gd"].rglob("*"))
    for url in ("/v1/quarantine", f"/v1/quarantine/plans/{pid}"):
        assert TestClient(app).get(url).status_code == 401
        assert TestClient(plain.app).get(url).status_code == 401
    assert TestClient(app).post(f"/v1/quarantine/plans/{pid}/check", json={"action": "restore"}).status_code == 401
    world.age(pid, 400)                                    # long past any retention: still nothing happens by itself
    before = world.conn.total_changes
    for _ in range(3):
        plain.get("/v1/quarantine")
        plain.get(f"/v1/quarantine/plans/{pid}")
        plain.post(f"/v1/quarantine/plans/{pid}/check", json={"action": "purge"})
    assert world.conn.total_changes == before
    assert not world.exists("gd", "docs/a/one.bin") and world.exists("gd", f"{QDIR}/{pid}/docs/a/one.bin")
    assert files == sorted(p.name for p in world.roots["gd"].rglob("*"))


def test_the_report_carries_each_clouds_cached_quota(world, bearer, plain, monkeypatch):
    world.two_deletes()
    quota = {"used": 10, "total": 100, "free": 90}
    monkeypatch.setattr(api_mod.QuotaCache, "get", lambda self, config, name: quota if name == "gd" else None)
    for client in (bearer, plain):
        rows = {r["remote"]: r for r in client.get("/v1/quarantine").json()["remotes"]}
        assert rows["gd"]["quota"] == quota and rows["dbx"]["quota"] is None


def test_a_remote_that_is_not_a_connection_has_no_quota(world, plain):
    world.two_deletes()
    assert all(r["quota"] is None for r in plain.get("/v1/quarantine").json()["remotes"])


@pytest.mark.parametrize("client_name", ["bearer", "plain"])
def test_quarantine_errors_are_coded_in_both_apps(world, client_name, bearer, plain):
    client = {"bearer": bearer, "plain": plain}[client_name]
    pid = world.two_deletes()
    r = client.get("/v1/quarantine/plans/999")
    assert r.status_code == 404 and r.json() == {"error": "no plan 999", "code": "NOT_FOUND", "params": {"plan": 999}}
    r = client.get(f"/v1/quarantine/plans/{pid}?state=nope")
    assert r.status_code == 400 and r.json()["code"] == "BAD_QUERY" and r.json()["params"] == {"param": "state"}
    assert client.get(f"/v1/quarantine/plans/{pid}?limit=501").json()["params"] == {"param": "limit"}
    r = client.post(f"/v1/quarantine/plans/{pid}/check", json={"action": "delete"})
    assert r.status_code == 400 and r.json()["params"] == {"param": "action"}
    r = client.post(f"/v1/quarantine/plans/{pid}/check", json={"action": "restore", "op_ids": [987654, 1234]})
    assert r.status_code == 400 and r.json()["code"] == "BAD_OPS"
    assert r.json()["params"] == {"plan": pid, "op_ids": [987654, 1234]}
    empty = plans.create_plan(world.conn, "nothing", "manual", [], created_by="test")
    plans.set_status(world.conn, empty, "done")
    r = client.post(f"/v1/quarantine/plans/{empty}/check", json={"action": "purge"})
    assert r.status_code == 409 and r.json() == {"error": f"plan {empty} has nothing in quarantine",
                                                 "code": "NOTHING_QUARANTINED", "params": {"plan": empty}}
    assert client.post(f"/v1/quarantine/plans/999/check", json={"action": "restore"}).status_code == 404
    assert client.post(f"/v1/quarantine/plans/{pid}/check",
                       json={"action": "restore", "op_ids": list(range(5001))}).status_code == 422
    assert client.post(f"/v1/quarantine/plans/{pid}/check", json={"action": "restore", "extra": 1}).status_code == 422


# ----------------------------------------------------------------- the human channel

HUMAN = [
    ("/v1/ui/plans/1/restore", {"confirm": "restore"}),
    ("/v1/ui/plans/1/purge", {"confirm": "purge"}),
    ("/v1/ui/quarantine/purge", {"confirm": "purge"}),
]


@pytest.mark.parametrize("url,body", HUMAN)
def test_a_bearer_token_and_strangers_never_reach_restore_or_purge(app, bearer, url, body):
    r = bearer.post(url, json=body)
    assert r.status_code == 403 and r.json()["code"] == "HUMAN_ONLY"
    assert TestClient(app).post(url, json=body).status_code == 401


@pytest.mark.parametrize("url,body", HUMAN)
def test_a_session_needs_csrf_and_the_right_origin(browser, url, body):
    client, headers = browser
    r = client.post(url, json=body)
    assert r.status_code == 403 and r.json()["code"] == "BAD_CSRF"
    r = client.post(url, json=body, headers={**headers, "Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "BAD_ORIGIN"


@pytest.mark.parametrize("url,body", HUMAN)
def test_without_approvals_restore_and_purge_are_off_and_name_the_terminal_command(world, url, body):
    client, headers = login(make_ui(world, approvals=False))
    r = client.post(url, json=body, headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "APPROVALS_DISABLED"
    command = {"restore": "drivemerge plan restore", "purge": "drivemerge plan purge"}[url.rsplit("/", 1)[1]] \
        if "quarantine" not in url else "drivemerge quarantine purge-due"
    assert command in r.json()["error"]
    # ... while the read-only listing and the check still work for the session
    assert client.get("/v1/quarantine").status_code == 200


def test_the_human_routes_are_only_in_the_browser_app(world, plain):
    for url, body in HUMAN:
        assert plain.post(url, json=body).status_code == 404
    paths = {getattr(r, "path", "") for r in api_mod.create_app(world.service, token=API).routes}
    assert not any("restore" in p or "purge" in p for p in paths)


# ----------------------------------------------------------------- the order of refusals

def test_the_refusals_come_in_the_documented_order(world, browser, app):
    client, headers = browser
    pid = world.two_deletes()
    url = f"/v1/ui/plans/{pid}/restore"
    # the typed word first
    for body in ({}, {"confirm": "Restore"}, {"confirm": "purge"}):
        refused(client, headers, url, body, 400, "BAD_REQUEST")
    world.config.safety.execution_enabled = False
    refused(client, headers, f"/v1/ui/plans/999/restore", {"confirm": "restore"}, 403, "EXECUTION_DISABLED")
    refused(client, headers, url, {"confirm": "restore"}, 403, "EXECUTION_DISABLED")
    refused(client, headers, url, {"confirm": "wrong"}, 400, "BAD_REQUEST")
    world.config.safety.execution_enabled = True
    refused(client, headers, "/v1/ui/plans/999/restore", {"confirm": "restore"}, 404, "NOT_FOUND")
    # a plan that has not run
    draft = plans.create_plan(world.conn, "d", "manual", [], created_by="test")
    r = refused(client, headers, f"/v1/ui/plans/{draft}/restore", {"confirm": "restore"}, 409, "CONFLICT")
    assert r["params"] == {"plan": draft, "plan_status": "draft"}
    refused(client, headers, f"/v1/ui/plans/{draft}/purge", {"confirm": "purge"}, 409, "CONFLICT")
    # a plan that is running
    plans.set_status(world.conn, draft, "executing")
    r = refused(client, headers, f"/v1/ui/plans/{draft}/restore", {"confirm": "restore"}, 409, "PLAN_EXECUTING")
    assert r["params"] == {"plan": draft}
    refused(client, headers, f"/v1/ui/plans/{draft}/purge", {"confirm": "purge"}, 409, "PLAN_EXECUTING")
    # nothing quarantined
    plans.set_status(world.conn, draft, "done")
    r = refused(client, headers, f"/v1/ui/plans/{draft}/restore", {"confirm": "restore"}, 409, "NOTHING_QUARANTINED")
    assert r["params"] == {"plan": draft}
    refused(client, headers, f"/v1/ui/plans/{draft}/purge", {"confirm": "purge", "op_ids": [1]}, 409,
            "NOTHING_QUARANTINED")
    # a selection that names things that are not held items of this plan
    other = world.two_deletes("b", seed=50)
    foreign = world.ops(other)[0]
    r = refused(client, headers, url, {"confirm": "restore", "op_ids": [foreign, world.ops(pid)[0]]}, 400, "BAD_OPS")
    assert r["params"] == {"plan": pid, "op_ids": [foreign]}
    refused(client, headers, f"/v1/ui/plans/{pid}/purge", {"confirm": "purge", "op_ids": [foreign]}, 400, "BAD_OPS")
    refused(client, headers, url, {"confirm": "restore", "op_ids": []}, 400, "BAD_OPS")
    assert client.post(url, headers=headers, json={"confirm": "restore", "op_ids": list(range(5001))}).status_code == 422
    # nothing above started a job or touched a cloud
    assert world.exists("gd", f"{QDIR}/{pid}/docs/a/one.bin") and not world.exists("gd", "docs/a/one.bin")


def test_a_job_that_is_running_refuses_the_next_one_but_only_after_the_other_checks(world, browser, monkeypatch):
    client, headers = browser
    pid = world.two_deletes()
    other = world.two_deletes("b", seed=50)
    started, release = threading.Event(), threading.Event()

    def slow_restore(self, plan_id, **kw):
        started.set()
        assert release.wait(10)
        return {"plan_id": plan_id, "status": "done", "done": 0, "failed": 0, "skipped": 0, "error": None,
                "failures": []}

    monkeypatch.setattr(Service, "restore", slow_restore)
    r = client.post(f"/v1/ui/plans/{pid}/restore", headers=headers, json={"confirm": "restore"})
    assert r.status_code == 202
    assert started.wait(10)
    busy = refused(client, headers, f"/v1/ui/plans/{other}/restore", {"confirm": "restore"}, 409, "JOB_BUSY")
    assert busy["params"]["kind"] == "restore"
    refused(client, headers, f"/v1/ui/plans/{other}/purge", {"confirm": "purge"}, 409, "JOB_BUSY")
    refused(client, headers, "/v1/ui/quarantine/purge", {"confirm": "purge"}, 409, "JOB_BUSY")
    # the synchronous checks still win over JOB_BUSY
    refused(client, headers, "/v1/ui/plans/999/restore", {"confirm": "restore"}, 404, "NOT_FOUND")
    refused(client, headers, f"/v1/ui/plans/{other}/restore", {"confirm": "restore", "op_ids": [1]}, 400, "BAD_OPS")
    release.set()
    assert wait(client, r.json()["job_id"])["status"] == "done"


# ----------------------------------------------------------------- restore

def test_restore_puts_everything_back_as_a_job_and_marks_the_catalog_behind(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    world.scan()                                   # the catalog is current: nothing is behind
    assert client.get("/v1/ui/config").json()["outdated"] == []
    job = run_job(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore"})
    assert job["kind"] == "restore" and job["status"] == "done" and job["error"] is None
    assert job["progress"] == {"plan_id": pid, "restored": 2, "failed": 0, "total": 2}
    assert job["result"]["status"] == "done" and job["result"]["done"] == 2 and job["result"]["failures"] == []
    assert world.exists("gd", "docs/a/one.bin") and world.exists("gd", "docs/a/two.bin")
    assert not world.exists("gd", f"{QDIR}/{pid}/docs/a/one.bin")
    assert [r[0] for r in world.conn.execute("SELECT status FROM plan_ops WHERE plan_id = ?", (pid,))] \
        == ["skipped", "skipped"]
    # "the catalog is behind" after a restore, until the cloud is scanned again
    assert client.get("/v1/ui/config").json()["outdated"] == ["gd"]
    world.scan()
    assert client.get("/v1/ui/config").json()["outdated"] == []
    items = client.get(f"/v1/quarantine/plans/{pid}?state=restored").json()
    assert items["total"] == 2 and all(i["restored_at"] for i in items["items"])
    assert client.get("/v1/quarantine").json()["totals"]["files"] == 0


def test_restore_of_chosen_items_only(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    first, second = world.ops(pid)
    job = run_job(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore", "op_ids": [second]})
    assert job["status"] == "done" and job["progress"]["total"] == 1 and job["result"]["done"] == 1
    assert world.exists("gd", "docs/a/two.bin") and not world.exists("gd", "docs/a/one.bin")
    assert world.exists("gd", f"{QDIR}/{pid}/docs/a/one.bin")
    # the restored one is no longer a held item, so it cannot be named again
    refused(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore", "op_ids": [second]}, 400, "BAD_OPS")


def test_restore_never_overwrites_and_a_failed_item_fails_the_job_with_its_code(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    world.put("gd", "docs/a/one.bin", b"somebody put a new file here")
    (world.roots["gd"] / QDIR / str(pid) / "docs/a/two.bin").unlink()
    first, second = world.ops(pid)
    job = run_job(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore"})
    assert job["status"] == "failed" and job["error"]
    assert job["progress"] == {"plan_id": pid, "restored": 0, "failed": 2, "total": 2}
    result = job["result"]
    assert (result["status"], result["done"], result["failed"]) == ("partial", 0, 2)
    by_code = {f["code"]: f for f in result["failures"]}
    assert by_code["ORIGINAL_TAKEN"] == {"op_id": first, "seq": 1, "remote": "gd", "path": "docs/a/one.bin",
                                          "code": "ORIGINAL_TAKEN", "params": {"remote": "gd", "path": "docs/a/one.bin"},
                                          "error": by_code["ORIGINAL_TAKEN"]["error"]}
    assert by_code["QUARANTINE_GONE"]["op_id"] == second and by_code["QUARANTINE_GONE"]["params"] == {
        "remote": "gd", "path": "docs/a/two.bin"}
    assert (world.roots["gd"] / "docs/a/one.bin").read_bytes() == b"somebody put a new file here"
    assert world.exists("gd", f"{QDIR}/{pid}/docs/a/one.bin"), "the quarantined copy stayed"


def test_restore_is_off_when_execution_is_disabled(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    world.config.safety.execution_enabled = False
    refused(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore"}, 403, "EXECUTION_DISABLED")
    refused(client, headers, f"/v1/ui/plans/{pid}/purge", {"confirm": "purge"}, 403, "EXECUTION_DISABLED")
    refused(client, headers, "/v1/ui/quarantine/purge", {"confirm": "purge"}, 403, "EXECUTION_DISABLED")
    assert world.exists("gd", f"{QDIR}/{pid}/docs/a/one.bin")


# ----------------------------------------------------------------- purge

def test_purge_of_chosen_items_re_proves_each_witness_and_marks_the_catalog_behind(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    world.scan()
    first, second = world.ops(pid)
    job = run_job(client, headers, f"/v1/ui/plans/{pid}/purge", {"confirm": "purge", "op_ids": [first]})
    assert job["kind"] == "purge" and job["status"] == "done"
    assert job["progress"] == {"plan_id": pid, "purged": 1, "failed": 0, "total": 1}
    assert job["result"]["done"] == 1 and job["result"]["failures"] == []
    assert not world.exists("gd", f"{QDIR}/{pid}/docs/a/one.bin")
    assert world.exists("gd", f"{QDIR}/{pid}/docs/a/two.bin"), "only the chosen item went"
    assert world.exists("dbx", "keep/a/one.bin"), "the witness is never touched"
    assert client.get("/v1/ui/config").json()["outdated"] == ["gd"]
    items = client.get(f"/v1/quarantine/plans/{pid}?state=purged").json()
    assert [i["op_id"] for i in items["items"]] == [first]
    # a purged item cannot be restored or named again
    refused(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore", "op_ids": [first]}, 400, "BAD_OPS")


def test_purge_keeps_an_item_whose_witness_is_gone_and_fails_the_job(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    (world.roots["dbx"] / "keep/a/one.bin").unlink()
    job = run_job(client, headers, f"/v1/ui/plans/{pid}/purge", {"confirm": "purge"})
    assert job["status"] == "failed" and "is kept" in job["error"]
    assert job["progress"]["purged"] == 1 and job["progress"]["failed"] == 1 and job["progress"]["total"] == 2
    [failure] = job["result"]["failures"]
    assert failure["code"] == "WITNESS_UNPROVEN" and failure["remote"] == "gd" and failure["path"] == "docs/a/one.bin"
    assert failure["params"] == {"remote": "gd", "path": "docs/a/one.bin", "witness": "dbx:keep/a/one.bin"}
    assert world.exists("gd", f"{QDIR}/{pid}/docs/a/one.bin"), "the quarantined copy may be the last one"
    assert not world.exists("gd", f"{QDIR}/{pid}/docs/a/two.bin")


def test_purge_by_age_leaves_what_is_too_recent(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    job = run_job(client, headers, f"/v1/ui/plans/{pid}/purge", {"confirm": "purge", "older_than_days": 30})
    assert job["status"] == "done" and job["result"]["done"] == 0 and job["result"]["skipped"] == 2
    assert job["progress"] == {"plan_id": pid, "purged": 0, "failed": 0, "total": 0}
    world.age(pid, 31)
    job = run_job(client, headers, f"/v1/ui/plans/{pid}/purge", {"confirm": "purge", "older_than_days": 30})
    assert job["result"]["done"] == 2


def test_a_plan_wide_purge_still_works_for_a_plan_that_parked_nothing(world, browser):
    client, headers = browser
    empty = plans.create_plan(world.conn, "release", "release", [], created_by="test")
    plans.set_status(world.conn, empty, "done")
    job = run_job(client, headers, f"/v1/ui/plans/{empty}/purge", {"confirm": "purge"})
    assert job["status"] == "done" and job["result"]["done"] == 0


# ----------------------------------------------------------------- purge everything past retention

def test_purge_due_walks_the_plans_and_one_failing_plan_does_not_stop_the_others(world, browser):
    client, headers = browser
    good = world.two_deletes("a", seed=1)
    bad = world.two_deletes("b", seed=10)
    young = world.two_deletes("c", seed=20)
    for pid in (good, bad):
        world.age(pid, 45)
    (world.roots["dbx"] / "keep/b/one.bin").unlink()
    job = run_job(client, headers, "/v1/ui/quarantine/purge", {"confirm": "purge"})
    assert job["kind"] == "purge" and job["status"] == "failed" and job["error"]
    assert set(job["progress"]) == {"plans_total", "plans_done", "purged", "failed"}
    assert job["progress"] == {"plans_total": 2, "plans_done": 2, "purged": 3, "failed": 1}
    result = job["result"]
    assert [p["plan_id"] for p in result["plans"]] == [good, bad], "oldest first"
    assert (result["done"], result["failed"], result["skipped"]) == (3, 1, 0)
    by_plan = {p["plan_id"]: p for p in result["plans"]}
    assert (by_plan[good]["status"], by_plan[good]["done"], by_plan[good]["failures"]) == ("done", 2, [])
    assert by_plan[bad]["status"] == "partial" and by_plan[bad]["failures"][0]["code"] == "WITNESS_UNPROVEN"
    assert not world.exists("gd", f"{QDIR}/{good}/docs/a/one.bin")
    assert world.exists("gd", f"{QDIR}/{bad}/docs/b/one.bin") and not world.exists("gd", f"{QDIR}/{bad}/docs/b/two.bin")
    assert world.exists("gd", f"{QDIR}/{young}/docs/c/one.bin"), "within the retention period: untouched"
    assert client.get("/v1/ui/config").json()["outdated"] == ["gd"]


def test_purge_due_can_be_limited_to_one_cloud(world, browser):
    client, headers = browser
    world.put("gd", "x/g.bin", blob(50, 70))
    world.put("dbx", "x/d.bin", blob(60, 71))
    world.put("dbx", "keep/g.bin", blob(50, 70))
    world.put("gd", "keep/d.bin", blob(60, 71))
    world.scan()
    pid = world.executed_plan([("gd", "x/g.bin", "dbx", "keep/g.bin"), ("dbx", "x/d.bin", "gd", "keep/d.bin")],
                              kind="release")
    world.age(pid, 60)
    job = run_job(client, headers, "/v1/ui/quarantine/purge", {"confirm": "purge", "remote": "dbx"})
    assert job["status"] == "done" and job["result"]["done"] == 1
    assert world.exists("gd", f"{QDIR}/{pid}/x/g.bin") and not world.exists("dbx", f"{QDIR}/{pid}/x/d.bin")
    assert client.post("/v1/ui/quarantine/purge", headers=headers,
                       json={"confirm": "purge", "remote": "nosuchcloud"}).status_code == 404
    refused(client, headers, "/v1/ui/quarantine/purge", {"confirm": "yes"}, 400, "BAD_REQUEST")
    assert client.post("/v1/ui/quarantine/purge", headers=headers,
                       json={"confirm": "purge", "unknown": 1}).status_code == 422


def test_purge_due_with_nothing_due_is_an_empty_job(world, browser):
    client, headers = browser
    world.two_deletes()
    job = run_job(client, headers, "/v1/ui/quarantine/purge", {"confirm": "purge"})
    assert job["status"] == "done" and job["result"]["plans"] == []
    assert job["progress"] == {"plans_total": 0, "plans_done": 0, "purged": 0, "failed": 0}


def test_files_nobody_accounts_for_are_listed_but_never_restored_or_purged(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    world.age(pid, 60)
    world.put("gd", f"{QDIR}/777/stranger.bin", b"not ours")
    world.put("gd", f"{QDIR}/{pid}/docs/a/never-parked.bin", b"not ours either")
    world.scan()
    unknown = client.get("/v1/quarantine").json()["unknown"]
    assert len(unknown) == 1 and unknown[0]["remote"] == "gd" and unknown[0]["files"] == 2
    run_job(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore"})
    assert world.exists("gd", f"{QDIR}/777/stranger.bin") and world.exists("gd", f"{QDIR}/{pid}/docs/a/never-parked.bin")
    other = world.two_deletes("z", seed=90)
    world.age(other, 60)
    run_job(client, headers, "/v1/ui/quarantine/purge", {"confirm": "purge"})
    assert world.exists("gd", f"{QDIR}/777/stranger.bin") and world.exists("gd", f"{QDIR}/{pid}/docs/a/never-parked.bin")
    assert not world.exists("gd", f"{QDIR}/{other}/docs/z/one.bin")
    # no op id can name them
    refused(client, headers, f"/v1/ui/plans/{pid}/purge", {"confirm": "purge", "op_ids": [10**6]}, 409,
            "NOTHING_QUARANTINED")


# ----------------------------------------------------------------- the catalog is behind

def test_outdated_counts_a_restore_and_a_purge_from_the_terminal_too(world, browser):
    """Read from the journal, so a run from the terminal shows the banner as well."""
    client, _ = browser
    pid = world.two_deletes()
    world.scan()
    assert client.get("/v1/ui/config").json()["outdated"] == []
    first, second = world.ops(pid)
    time.sleep(0.01)
    assert executor.restore_plan(world.conn, world.service.rclone, world.config, pid, op_ids=[first]).done == 1
    assert client.get("/v1/ui/config").json()["outdated"] == ["gd"]
    world.scan()
    assert client.get("/v1/ui/config").json()["outdated"] == []
    time.sleep(0.01)
    assert executor.purge_quarantine(world.conn, world.service.rclone, world.config, pid, older_than_days=0).done == 1
    assert client.get("/v1/ui/config").json()["outdated"] == ["gd"]
    world.scan()
    assert client.get("/v1/ui/config").json()["outdated"] == []


def test_a_restore_or_purge_that_failed_changes_nothing_and_does_not_mark_the_catalog_behind(world, browser):
    client, headers = browser
    pid = world.two_deletes()
    world.scan()
    world.put("gd", "docs/a/one.bin", b"taken")
    (world.roots["gd"] / QDIR / str(pid) / "docs/a/two.bin").unlink()
    time.sleep(0.01)
    run_job(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore"})
    assert client.get("/v1/ui/config").json()["outdated"] == []


# ----------------------------------------------------------------- secrets

def test_no_response_carries_a_digest_an_approval_code_or_a_snapshot(world, browser, bearer, plain):
    client, headers = browser
    pid = world.two_deletes()
    digest = plans.get_plan(world.conn, pid, with_ops=False).digest
    code = plans.short_code(digest)
    world.conn.execute("UPDATE plans SET params = ? WHERE id = ?",
                       (json.dumps({"remote": "gd", "digest": digest, "approval_code": code, "snapshot": {"1": 2}}), pid))
    texts = []
    for c in (client, bearer, plain):
        texts.append(c.get("/v1/quarantine").text)
        texts.append(c.get(f"/v1/quarantine/plans/{pid}?state=all").text)
        texts.append(c.post(f"/v1/quarantine/plans/{pid}/check", json={"action": "restore"}).text)
        texts.append(c.post(f"/v1/quarantine/plans/{pid}/check", json={"action": "purge"}).text)
    job = run_job(client, headers, f"/v1/ui/plans/{pid}/restore", {"confirm": "restore", "op_ids": world.ops(pid)[:1]})
    texts.append(json.dumps(job))
    texts.append(json.dumps(run_job(client, headers, f"/v1/ui/plans/{pid}/purge", {"confirm": "purge"})))
    blob_ = "\n".join(texts)
    assert digest not in blob_ and code not in blob_
    assert "approval_code" not in blob_ and "snapshot" not in blob_ and '"digest"' not in blob_
    assert "gd" in json.loads(texts[0])["plans"][0]["params"]["remote"]
