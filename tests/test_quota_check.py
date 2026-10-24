"""The simulator's QUOTA check fed from the quota cache the page fills (Service.policy), and the re-check of
free space before approval and execution (Service.require_room)."""

from __future__ import annotations

import pytest

from drivemerge import plans, quota
from drivemerge.config import Config
from drivemerge.models import Op
from drivemerge.service import Service


@pytest.fixture
def service(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.remote("dropbox", "dropbox")
    catalog.file(g, "Photos/vacation.jpg", size=2_000, md5="aa")
    catalog.file(g, "Docs/budget.pdf", size=500, md5="bb")
    return Service(Config(), conn=conn)


def _rid(conn, name):
    return conn.execute("SELECT id FROM remotes WHERE name = ?", (name,)).fetchone()[0]


def _plan(conn):
    """Copies 2 500 bytes into dropbox."""
    g, d = _rid(conn, "gdrive"), _rid(conn, "dropbox")
    return plans.create_plan(conn, "copy to dropbox", "manual", [
        Op("copy", g, "Photos/vacation.jpg", dst_remote_id=d, dst_path="Photos/vacation.jpg"),
        Op("copy", g, "Docs/budget.pdf", dst_remote_id=d, dst_path="Docs/budget.pdf"),
    ], created_by="test")


def _free(service, name, free):
    service.quotas.put(name, {"ok": True, "quota": {"used": 1, "total": 1 + free, "free": free}})


def _codes(report):
    return [v["code"] for v in report["violations"]]


def test_a_plan_over_the_cached_free_space_gets_a_quota_violation(service, conn):
    _free(service, "dropbox", 2_499)
    report = service.simulate(_plan(conn))
    assert not report["ok"] and _codes(report) == ["QUOTA"]
    assert report["violations"][0]["params"]["bytes"] == 2_500
    assert report["violations"][0]["params"]["free"] == 2_499
    assert report["approval_code"] is None


def test_a_plan_within_the_cached_free_space_passes(service, conn):
    _free(service, "dropbox", 2_500)
    report = service.simulate(_plan(conn))
    assert report["ok"] and _codes(report) == []


@pytest.mark.parametrize("cache", ["nothing", "failed lookup", "no free figure", "not a number"])
def test_unknown_free_space_is_not_checked(service, conn, cache):
    if cache == "failed lookup":
        service.quotas.put("dropbox", {"ok": False})
    elif cache == "no free figure":
        service.quotas.put("dropbox", {"ok": True, "quota": {"used": 5, "total": None, "free": None}})
    elif cache == "not a number":
        service.quotas.put("dropbox", {"ok": True, "quota": {"free": "12"}})
    assert service.policy().free_bytes is None
    report = service.simulate(_plan(conn))
    assert report["ok"] and _codes(report) == []


def test_a_stale_quota_is_not_checked(service, conn, monkeypatch):
    _free(service, "dropbox", 0)
    now = quota.time.monotonic()
    monkeypatch.setattr(quota.time, "monotonic", lambda: now + quota.QUOTA_TTL + 1)
    assert service.policy().free_bytes is None
    assert service.simulate(_plan(conn))["ok"]


def test_policy_maps_free_space_by_remote_id(service, conn):
    _free(service, "dropbox", 7)
    assert service.policy().free_bytes == {_rid(conn, "dropbox"): 7}


def test_free_space_is_not_in_the_digest(service, conn):
    pid = _plan(conn)
    _free(service, "dropbox", 10_000)
    first = service.simulate(pid)["approval_code"]
    _free(service, "dropbox", 50_000)
    assert service.simulate(pid)["approval_code"] == first


def test_approval_refuses_when_free_space_shrank_since_the_simulation(service, conn):
    pid = _plan(conn)
    _free(service, "dropbox", 10_000)
    code = service.simulate(pid)["approval_code"]
    _free(service, "dropbox", 1_000)
    with pytest.raises(plans.StaleSimulation, match="dropbox now has 1000 bytes free"):
        service.approve(pid, code, approved_by="test")
    assert plans.get_plan(conn, pid, with_ops=False).status == "simulated"
    # the way on: simulate again, which now reports the shortfall
    assert _codes(service.simulate(pid)) == ["QUOTA"]


def test_execution_refuses_when_free_space_shrank_after_approval(service, conn, monkeypatch):
    pid = _plan(conn)
    _free(service, "dropbox", 10_000)
    service.approve(pid, service.simulate(pid)["approval_code"], approved_by="test")
    _free(service, "dropbox", 2_499)
    ran = []
    monkeypatch.setattr("drivemerge.executor.execute_plan", lambda *a, **k: ran.append(a))
    with pytest.raises(plans.StaleSimulation):
        service.execute(pid)
    assert ran == [] and plans.get_plan(conn, pid, with_ops=False).status == "approved"


def test_unknown_free_space_at_approval_does_not_refuse(service, conn):
    pid = _plan(conn)
    _free(service, "dropbox", 10_000)
    code = service.simulate(pid)["approval_code"]
    service.quotas.drop("dropbox")
    service.approve(pid, code, approved_by="test")
    assert plans.get_plan(conn, pid, with_ops=False).status == "approved"


def test_a_failed_plan_is_not_rechecked(service, conn):
    pid = _plan(conn)
    _free(service, "dropbox", 10_000)
    service.approve(pid, service.simulate(pid)["approval_code"], approved_by="test")
    conn.execute("UPDATE plans SET status = 'failed' WHERE id = ?", (pid,))
    _free(service, "dropbox", 0)          # its done ops used the space it counted
    service.require_room(pid)
