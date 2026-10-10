import pytest

from drivemerge import plans
from drivemerge.models import Op, SimulationReport


def _sim(conn, plan_id, ok=True):
    from drivemerge import db
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
