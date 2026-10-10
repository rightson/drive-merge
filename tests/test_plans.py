import json
import os
import socket
import subprocess
import sys

import pytest

from drivemerge import db, plans
from drivemerge.models import Op, SimulationReport, Violation


def _sim(conn, plan_id, ok=True):
    snap = db.snapshot(conn)
    digest = plans.compute_digest(plan_id, plans.load_ops(conn, plan_id), snap)
    plans.record_simulation(conn, plan_id, SimulationReport(plan_id=plan_id, digest=digest, ok=ok), snap)
    return digest


def test_create_and_load(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "a/x.txt", size=3, md5="aa")
    pid = plans.create_plan(conn, "t", "manual", [
        Op("copy", g, "a/x.txt", dst_remote_id=g, dst_path="b/x.txt"),
        Op("delete", g, "a/x.txt", witness_remote_id=g, witness_path="b/x.txt", depends_on=1),
    ], created_by="test")
    plan = plans.get_plan(conn, pid)
    assert plan.status == "draft"
    assert [o.seq for o in plan.ops] == [1, 2]
    assert plan.ops[1].depends_on == 1


def test_approval_requires_passing_simulation_and_code(conn, catalog):
    g = catalog.remote("g", "drive")
    pid = plans.create_plan(conn, "t", "manual", [Op("copy", g, "x", dst_remote_id=g, dst_path="y")], created_by="t")
    with pytest.raises(plans.PlanError):
        plans.approve_plan(conn, pid, "whatever", approved_by="human")
    _sim(conn, pid, ok=False)
    with pytest.raises(plans.PlanError):
        plans.approve_plan(conn, pid, "x", approved_by="human")
    digest = _sim(conn, pid, ok=True)
    with pytest.raises(plans.PlanError):
        plans.approve_plan(conn, pid, "deadbeef", approved_by="human")
    plans.approve_plan(conn, pid, plans.short_code(digest), approved_by="human")
    assert plans.get_plan(conn, pid).status == "approved"


def test_rescan_invalidates_approval(conn, catalog):
    g = catalog.remote("g", "drive")
    pid = plans.create_plan(conn, "t", "manual", [Op("copy", g, "x", dst_remote_id=g, dst_path="y")], created_by="t")
    digest = _sim(conn, pid)
    catalog.remote("h", "dropbox")  # a new complete scan changes the snapshot
    with pytest.raises(plans.StaleSimulation):
        plans.approve_plan(conn, pid, plans.short_code(digest), approved_by="human")


def test_resimulation_drops_approval(conn, catalog):
    g = catalog.remote("g", "drive")
    pid = plans.create_plan(conn, "t", "manual", [Op("copy", g, "x", dst_remote_id=g, dst_path="y")], created_by="t")
    digest = _sim(conn, pid)
    plans.approve_plan(conn, pid, digest, approved_by="human")
    _sim(conn, pid)
    assert plans.get_plan(conn, pid).status == "simulated"


# ------------------------------------------------------------------ the execution lock

def _lock(conn, plan_id):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"executing:{plan_id}",)).fetchone()
    return None if row is None else json.loads(row[0])


def _set_lock(conn, plan_id, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                 (f"executing:{plan_id}", value if isinstance(value, str) else json.dumps(value)))


def _approved_plan(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "a.txt", size=3, md5="aa")
    pid = plans.create_plan(conn, "t", "manual", [Op("copy", g, "a.txt", dst_remote_id=g, dst_path="b.txt")],
                            created_by="test")
    plans.approve_plan(conn, pid, plans.short_code(_sim(conn, pid)), approved_by="test")
    return pid


def _dead_pid() -> int:
    """The pid of a process that has ended and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_claiming_a_plan_records_who_runs_it(conn, catalog):
    pid = _approved_plan(conn, catalog)
    assert plans.claim_for_execution(conn, pid)
    assert plans.get_plan(conn, pid, with_ops=False).status == "executing"
    lock = _lock(conn, pid)
    assert lock["pid"] == os.getpid() and lock["host"] == socket.gethostname()
    assert lock["started_at"].endswith("Z") and lock["started_at"] <= db.utcnow()
    assert not plans.claim_for_execution(conn, pid), "a plan is claimed once"
    plans.release_execution_lock(conn, pid)
    plans.release_execution_lock(conn, pid)  # releasing twice is harmless
    assert _lock(conn, pid) is None


@pytest.mark.parametrize("status", ["draft", "simulated", "executing", "done", "cancelled"])
def test_only_an_approved_or_failed_plan_can_be_claimed(conn, catalog, status):
    pid = _approved_plan(conn, catalog)
    plans.set_status(conn, pid, status)
    assert not plans.claim_for_execution(conn, pid)
    assert plans.get_plan(conn, pid, with_ops=False).status == status and _lock(conn, pid) is None


def test_claiming_replaces_a_lock_left_behind(conn, catalog):
    pid = _approved_plan(conn, catalog)
    _set_lock(conn, pid, {"pid": _dead_pid(), "host": "old", "started_at": "2000-01-01T00:00:00.000000Z"})
    assert plans.claim_for_execution(conn, pid)
    assert _lock(conn, pid)["pid"] == os.getpid()


def _stuck(conn, catalog, lock=None):
    pid = _approved_plan(conn, catalog)
    plans.claim_for_execution(conn, pid)
    plans.release_execution_lock(conn, pid)
    if lock is not None:
        _set_lock(conn, pid, lock)
    return pid


def _holder(pid, host=None):
    return {"pid": pid, "host": host or socket.gethostname(), "started_at": "2024-05-01T10:00:00.000000Z"}


def test_reset_frees_a_plan_whose_executor_died(conn, catalog):
    pid = _stuck(conn, catalog, _holder(_dead_pid()))
    plans.reset_stuck(conn, pid)
    plan = plans.get_plan(conn, pid, with_ops=False)
    assert plan.status == "failed" and plan.finished_at is not None
    assert _lock(conn, pid) is None
    [(action, detail)] = conn.execute("SELECT action, detail FROM journal WHERE plan_id = ?", (pid,)).fetchall()[-1:]
    assert action == "reset" and json.loads(detail)["lock"]["host"] == socket.gethostname()
    with pytest.raises(plans.PlanError):  # it is not executing any more
        plans.reset_stuck(conn, pid)


@pytest.mark.parametrize("alive", [False, True])
def test_the_pid_alone_decides(conn, catalog, alive):
    """host and start time are for the human reading the refusal; a lock needs nothing else to be understood."""
    pid = _stuck(conn, catalog, {"pid": os.getpid() if alive else _dead_pid()})
    if alive:
        with pytest.raises(plans.PlanError, match="being executed"):
            plans.reset_stuck(conn, pid)
    else:
        plans.reset_stuck(conn, pid)
        assert plans.get_plan(conn, pid, with_ops=False).status == "failed"


def test_reset_frees_a_plan_that_has_no_lock(conn, catalog):
    """A crash between the claim and the lock cannot happen (one transaction), but older runs left no lock."""
    pid = _stuck(conn, catalog)
    plans.reset_stuck(conn, pid)
    assert plans.get_plan(conn, pid, with_ops=False).status == "failed"


def test_reset_refuses_while_the_executor_is_alive(conn, catalog):
    pid = _stuck(conn, catalog, _holder(os.getpid()))
    with pytest.raises(plans.PlanError, match=rf"being executed by pid {os.getpid()}"):
        plans.reset_stuck(conn, pid)
    assert plans.get_plan(conn, pid, with_ops=False).status == "executing"
    assert _lock(conn, pid)["pid"] == os.getpid(), "a refused reset changes nothing"


def test_reset_refuses_a_process_it_may_not_signal(conn, catalog):
    """pid 1 is alive; for a non-root user kill(1, 0) fails with PermissionError, which must not read as 'dead'."""
    pid = _stuck(conn, catalog, _holder(1))
    with pytest.raises(plans.PlanError, match="being executed"):
        plans.reset_stuck(conn, pid)


def test_the_host_name_in_a_lock_does_not_decide(conn, catalog):
    """Host names change (a laptop on another network), so the pid is looked up here whatever the lock says."""
    alive = _stuck(conn, catalog, _holder(os.getpid(), host="some-other-name"))
    with pytest.raises(plans.PlanError, match="some-other-name"):
        plans.reset_stuck(conn, alive)
    assert plans.get_plan(conn, alive, with_ops=False).status == "executing"
    g = db.remote_id(conn, "g")
    gone = plans.create_plan(conn, "t2", "manual", [Op("copy", g, "a.txt", dst_remote_id=g, dst_path="d.txt")],
                             created_by="test")
    plans.approve_plan(conn, gone, plans.short_code(_sim(conn, gone)), approved_by="test")
    plans.claim_for_execution(conn, gone)
    _set_lock(conn, gone, _holder(_dead_pid(), host="some-other-name"))
    plans.reset_stuck(conn, gone)
    assert plans.get_plan(conn, gone, with_ops=False).status == "failed"


@pytest.mark.parametrize("lock", [
    "not json", "[]", "null", '{"host": "x"}', {"pid": "12", "host": "x"}, {"pid": 0, "host": "x"},
    {"pid": -1, "host": "x"}, {"pid": True, "host": "x"}, {"pid": None},
], ids=repr)
def test_reset_refuses_a_lock_it_cannot_read(conn, catalog, lock):
    """Signalling pid 0 or -1 would reach whole process groups, and a guess about a stranger's lock is not safe."""
    pid = _stuck(conn, catalog, lock)
    with pytest.raises(plans.PlanError, match="unreadable"):
        plans.reset_stuck(conn, pid)
    assert plans.get_plan(conn, pid, with_ops=False).status == "executing"


@pytest.mark.parametrize("status", ["draft", "simulated", "approved", "failed", "done", "cancelled"])
def test_reset_only_touches_an_executing_plan(conn, catalog, status):
    pid = _approved_plan(conn, catalog)
    plans.set_status(conn, pid, status)
    with pytest.raises(plans.PlanError, match=status):
        plans.reset_stuck(conn, pid)
    assert plans.get_plan(conn, pid, with_ops=False).status == status


def test_reset_of_an_unknown_plan(conn):
    with pytest.raises(plans.PlanNotFound):
        plans.reset_stuck(conn, 99)


def test_a_lock_is_per_plan(conn, catalog):
    first = _stuck(conn, catalog, _holder(os.getpid()))
    g = db.remote_id(conn, "g")
    second = plans.create_plan(conn, "t2", "manual", [Op("copy", g, "a.txt", dst_remote_id=g, dst_path="c.txt")],
                               created_by="test")
    plans.approve_plan(conn, second, plans.short_code(_sim(conn, second)), approved_by="test")
    plans.claim_for_execution(conn, second)
    plans.release_execution_lock(conn, second)
    plans.reset_stuck(conn, second)                      # the live lock of the first plan does not matter
    assert plans.get_plan(conn, second, with_ops=False).status == "failed"
    assert plans.get_plan(conn, first, with_ops=False).status == "executing"


# ---------------------------------------------------------------- stale digests

def test_a_stale_approved_plan_is_told_to_simulate_again(conn, catalog):
    pid = _approved_plan(conn, catalog)
    plans.require_current(conn, pid)
    catalog.remote("h", "dropbox")
    with pytest.raises(plans.StaleSimulation, match="simulate again"):
        plans.require_current(conn, pid)


def test_a_stale_failed_plan_is_told_to_propose_a_new_one(conn, catalog):
    """A failed plan cannot be simulated again, so 'simulate again' would send the user in a circle."""
    pid = _approved_plan(conn, catalog)
    plans.set_status(conn, pid, "failed")
    plans.require_current(conn, pid)
    catalog.remote("h", "dropbox")
    with pytest.raises(plans.StaleSimulation, match="propose a new plan") as e:
        plans.require_current(conn, pid)
    assert "simulate again" not in str(e.value)


# ------------------------------------------------------------ the capped report

def _violations(n, code="SRC_MISSING"):
    return [Violation(code, f"m{i}", i) for i in range(1, n + 1)]


def test_a_report_built_by_hand_counts_what_it_lists():
    rep = SimulationReport(plan_id=1, digest="d", ok=False, violations=_violations(3) + _violations(1, "DST_EXISTS"),
                           warnings=_violations(2, "NOOP"))
    assert (rep.violations_total, rep.warnings_total) == (4, 2)
    assert rep.violations_by_code == {"SRC_MISSING": 3, "DST_EXISTS": 1} and rep.warnings_by_code == {"NOOP": 2}


def test_a_capped_report_keeps_its_counts_through_json():
    rep = SimulationReport(plan_id=1, digest="d", ok=False, violations=_violations(5), violations_total=1500,
                           violations_by_code={"SRC_MISSING": 1400, "DST_EXISTS": 100}, warnings_total=7)
    back = SimulationReport.from_json(rep.to_json())
    assert back == rep
    assert (back.violations_total, back.warnings_total, len(back.violations)) == (1500, 7, 5)
    assert back.violations_by_code == {"DST_EXISTS": 100, "SRC_MISSING": 1400}


def test_reports_stored_before_the_cap_existed_still_load():
    old = json.dumps({
        "plan_id": 3, "digest": "abc", "ok": False,
        "violations": [{"code": "SRC_MISSING", "message": "gone", "seq": 1},
                       {"code": "SRC_MISSING", "message": "x", "seq": 2}],
        "warnings": [{"code": "NOOP", "message": "same", "seq": 3}],
        "remotes": [{"remote_id": 1, "remote": "g", "files_before": 2, "bytes_before": 5, "files_after": 1,
                     "bytes_after": 3, "bytes_in": 0, "files_quarantined": 1, "bytes_quarantined": 2}],
        "contents_before": 2, "contents_after": 1, "contents_lost": 0, "bytes_cross_remote": 0,
        "op_counts": {"copy": 1}, "sample": [],
    })
    rep = SimulationReport.from_json(old)
    assert (rep.violations_total, rep.warnings_total) == (2, 1)
    assert rep.violations_by_code == {"SRC_MISSING": 2} and rep.warnings_by_code == {"NOOP": 1}
    assert rep.remotes[0].remote == "g" and rep.violations[1].message == "x"


def test_stored_simulations_come_back_with_their_totals(conn, catalog):
    g = catalog.remote("g", "drive")
    pid = plans.create_plan(conn, "t", "manual", [Op("copy", g, "x", dst_remote_id=g, dst_path="y")], created_by="t")
    snap = db.snapshot(conn)
    digest = plans.compute_digest(pid, plans.load_ops(conn, pid), snap)
    rep = SimulationReport(plan_id=pid, digest=digest, ok=False, violations=_violations(2), violations_total=9,
                           violations_by_code={"SRC_MISSING": 9})
    plans.record_simulation(conn, pid, rep, snap)
    got = plans.latest_simulation(conn, pid)
    assert got.violations_total == 9 and got.violations_by_code == {"SRC_MISSING": 9} and len(got.violations) == 2
