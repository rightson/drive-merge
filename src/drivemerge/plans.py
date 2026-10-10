"""Plan persistence and lifecycle: draft -> simulated -> approved -> executing -> done|failed.

Rules enforced here (independently of any UI):
  * ops are immutable once written; a changed plan is a new plan
  * approval needs an ok simulation whose digest still matches the plan's
    ops and the *current* catalog snapshot (a rescan invalidates approval)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from . import db
from .models import Op, Plan, SimulationReport

_OP_COLUMNS = (
    "op", "src_entry_id", "src_remote_id", "src_path", "dst_remote_id", "dst_path",
    "witness_remote_id", "witness_path", "expect_size", "expect_hash", "expect_mtime",
    "depends_on", "reason",
)


class PlanError(Exception):
    pass


class PlanNotFound(PlanError):
    pass


class StaleSimulation(PlanError):
    """The catalog or the plan changed since it was simulated."""


def compute_digest(plan_id: int, ops: list[Op], snapshot: dict) -> str:
    payload = json.dumps(
        {"plan": plan_id, "ops": [o.canonical() for o in ops], "snapshot": snapshot},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def short_code(digest: str) -> str:
    """What a human types to approve: first 8 hex chars of the digest."""
    return digest[:8]


def create_plan(conn: sqlite3.Connection, title: str, kind: str, ops: list[Op], *,
                created_by: str, params: dict | None = None) -> int:
    """Persist a draft plan. Assigns seq 1..n in list order (overwriting op.seq),
    so builders should set depends_on using the final list positions (index+1)."""
    with db.transaction(conn):
        plan_id = conn.execute(
            "INSERT INTO plans(title, kind, status, created_by, params, created_at) "
            "VALUES (?, ?, 'draft', ?, ?, ?) RETURNING id",
            (title, kind, created_by, json.dumps(params or {}, ensure_ascii=False), db.utcnow()),
        ).fetchone()[0]
        for i, op in enumerate(ops, start=1):
            op.seq = i
            conn.execute(
                f"INSERT INTO plan_ops(plan_id, seq, {', '.join(_OP_COLUMNS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(_OP_COLUMNS))})",
                (plan_id, i, *(getattr(op, c) for c in _OP_COLUMNS)),
            )
    return int(plan_id)


def _row_to_op(r: sqlite3.Row) -> Op:
    return Op(**{c: r[c] for c in _OP_COLUMNS}, seq=r["seq"], id=r["id"], status=r["status"], error=r["error"])


def load_ops(conn: sqlite3.Connection, plan_id: int) -> list[Op]:
    rows = conn.execute("SELECT * FROM plan_ops WHERE plan_id = ? ORDER BY seq", (plan_id,)).fetchall()
    return [_row_to_op(r) for r in rows]


def get_plan(conn: sqlite3.Connection, plan_id: int, *, with_ops: bool = True) -> Plan:
    r = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if r is None:
        raise PlanNotFound(f"no plan {plan_id}")
    return Plan(
        id=r["id"], title=r["title"], kind=r["kind"], status=r["status"], created_by=r["created_by"],
        params=json.loads(r["params"] or "{}"), digest=r["digest"],
        snapshot=json.loads(r["snapshot"]) if r["snapshot"] else None,
        created_at=r["created_at"], simulated_at=r["simulated_at"], approved_at=r["approved_at"],
        approved_by=r["approved_by"], finished_at=r["finished_at"],
        ops=load_ops(conn, plan_id) if with_ops else [],
    )


def list_plans(conn: sqlite3.Connection, *, status: str | None = None, limit: int = 50) -> list[Plan]:
    q = "SELECT id FROM plans" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT ?"
    args = (status, limit) if status else (limit,)
    return [get_plan(conn, r[0], with_ops=False) for r in conn.execute(q, args).fetchall()]


def record_simulation(conn: sqlite3.Connection, plan_id: int, report: SimulationReport, snapshot: dict) -> None:
    """Store a simulation result. Called by simulate.simulate_plan.

    A plan that was approved and is re-simulated drops back to 'simulated'
    (or 'draft' if the new simulation fails): approval never survives a
    re-simulation.
    """
    plan = get_plan(conn, plan_id, with_ops=False)
    if plan.status not in ("draft", "simulated", "approved"):
        raise PlanError(f"plan {plan_id} is {plan.status}; cannot re-simulate")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO simulations(plan_id, digest, ok, report, created_at) VALUES (?, ?, ?, ?, ?)",
            (plan_id, report.digest, int(report.ok), report.to_json(), db.utcnow()),
        )
        conn.execute(
            "UPDATE plans SET status = ?, digest = ?, snapshot = ?, simulated_at = ?, "
            "approved_at = NULL, approved_by = NULL WHERE id = ?",
            ("simulated" if report.ok else "draft", report.digest, json.dumps(snapshot), db.utcnow(), plan_id),
        )


def latest_simulation(conn: sqlite3.Connection, plan_id: int) -> SimulationReport | None:
    r = conn.execute(
        "SELECT report FROM simulations WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (plan_id,)
    ).fetchone()
    return SimulationReport.from_json(r[0]) if r else None


def current_digest(conn: sqlite3.Connection, plan_id: int) -> str:
    """Digest of the plan's ops against the catalog as it is *now*."""
    return compute_digest(plan_id, load_ops(conn, plan_id), db.snapshot(conn))


def approve_plan(conn: sqlite3.Connection, plan_id: int, code: str, *, approved_by: str) -> None:
    """Approve a simulated plan. `code` must be short_code(digest) or the full digest.

    Callers must make sure a human is behind `approved_by`; the MCP server
    never exposes this function.
    """
    plan = get_plan(conn, plan_id, with_ops=False)
    if plan.status != "simulated":
        raise PlanError(f"plan {plan_id} is {plan.status}; only a simulated plan with no violations can be approved")
    sim = latest_simulation(conn, plan_id)
    if sim is None or not sim.ok or sim.digest != plan.digest:
        raise PlanError(f"plan {plan_id} has no passing simulation")
    if current_digest(conn, plan_id) != plan.digest:
        raise StaleSimulation(f"catalog changed since plan {plan_id} was simulated; simulate again")
    if code not in (plan.digest, short_code(plan.digest)):
        raise PlanError("confirmation code does not match the simulated plan")
    with db.transaction(conn):
        conn.execute(
            "UPDATE plans SET status = 'approved', approved_at = ?, approved_by = ? WHERE id = ?",
            (db.utcnow(), approved_by, plan_id),
        )
        db.journal(conn, "approve", plan_id=plan_id, detail=json.dumps({"by": approved_by, "digest": plan.digest}))


def set_status(conn: sqlite3.Connection, plan_id: int, status: str) -> None:
    finished = db.utcnow() if status in ("done", "failed", "cancelled") else None
    conn.execute(
        "UPDATE plans SET status = ?, finished_at = COALESCE(?, finished_at) WHERE id = ?",
        (status, finished, plan_id),
    )


def cancel_plan(conn: sqlite3.Connection, plan_id: int) -> None:
    plan = get_plan(conn, plan_id, with_ops=False)
    if plan.status in ("executing", "done"):
        raise PlanError(f"plan {plan_id} is {plan.status}; cannot cancel")
    set_status(conn, plan_id, "cancelled")


def set_op_status(conn: sqlite3.Connection, op_id: int, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE plan_ops SET status = ?, error = ?, executed_at = ? WHERE id = ?",
        (status, error, db.utcnow(), op_id),
    )
