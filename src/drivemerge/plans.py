"""Plan persistence and lifecycle: draft -> simulated -> approved -> executing -> done|failed.

Rules enforced here (independently of any UI):
  * ops are immutable once written; a changed plan is a new plan
  * approval needs an ok simulation whose digest still matches the plan's
    ops and the *current* catalog snapshot (a rescan invalidates approval)
  * a plan is 'executing' only while an executor holds its lock (a meta row
    naming the process); a plan whose process died is freed by reset_stuck
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
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


def compute_digest(conn: sqlite3.Connection, plan_id: int, ops: list[Op], snapshot: dict) -> str:
    """HMAC-SHA256 of the plan id, its ops and the catalog snapshot, keyed with this catalog's secret.

    Keyed so that the 8-character approval code cannot be worked out from what a client may read.
    """
    payload = json.dumps(
        {"plan": plan_id, "ops": [o.canonical() for o in ops], "snapshot": snapshot},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hmac.new(db.digest_secret(conn), payload.encode(), hashlib.sha256).hexdigest()


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
    return compute_digest(conn, plan_id, load_ops(conn, plan_id), db.snapshot(conn))


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


def require_current(conn: sqlite3.Connection, plan_id: int) -> None:
    """Raise StaleSimulation unless the plan's digest still matches its ops and the catalog as it is now.

    A failed plan can be neither simulated nor approved again, so for it a changed catalog (a rescan, or
    the plan's own half-done work) leaves only a new plan; for any other plan, simulating again is the way on.
    """
    plan = get_plan(conn, plan_id, with_ops=False)
    if current_digest(conn, plan_id) == plan.digest:
        return
    if plan.status == "failed":
        raise StaleSimulation(
            f"plan {plan_id} failed and the catalog has changed since it was simulated, and a failed plan cannot be "
            f"simulated or approved again: propose a new plan from the current catalog")
    raise StaleSimulation(f"plan {plan_id} or the catalog changed since it was simulated; simulate again")


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


# --------------------------------------------------------------------------- #
# Who is executing: the lock behind 'executing'
# --------------------------------------------------------------------------- #

def _lock_key(plan_id: int) -> str:
    return f"executing:{plan_id}"


def claim_for_execution(conn: sqlite3.Connection, plan_id: int) -> bool:
    """Move an approved or failed plan to 'executing' and record this process as its executor, in one step.

    False when the plan was not claimable (another executor got there first, or it is in another state).
    The lock is a meta row 'executing:<plan id>' holding pid, host and start time; release_execution_lock
    removes it, and reset_stuck reads it to tell a running executor from a dead one.
    """
    lock = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started_at": db.utcnow()})
    with db.transaction(conn):
        claimed = conn.execute(
            "UPDATE plans SET status = 'executing' WHERE id = ? AND status IN ('approved', 'failed')", (plan_id,)
        ).rowcount
        if claimed == 1:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_lock_key(plan_id), lock))
    return claimed == 1


def release_execution_lock(conn: sqlite3.Connection, plan_id: int) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (_lock_key(plan_id),))


def _pid_alive(pid: int) -> bool:
    """Is this process id in use on this machine? Anything uncertain counts as alive.

    A wrong 'dead' would let a second executor start on a plan that is still running.
    """
    if os.name != "posix":  # os.kill(pid, 0) would terminate the process on Windows
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # it exists, it just is not ours
        return True
    return True


def reset_stuck(conn: sqlite3.Connection, plan_id: int) -> None:
    """Move a plan that a crashed process left 'executing' to 'failed', so it can be executed again.

    Refused while an executor may still hold the plan: its lock names a pid that is alive on this machine, or
    cannot be read. A plan without any lock is not held. The lock's host is only reported: host names change
    (a laptop on another network), so the pid is always looked up here. Nothing is touched in a cloud: ops that
    did not finish stay pending and are checked against the live clouds again when the plan runs.
    """
    with db.transaction(conn):
        plan = get_plan(conn, plan_id, with_ops=False)
        if plan.status != "executing":
            raise PlanError(f"plan {plan_id} is {plan.status}; only a plan left 'executing' can be reset")
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (_lock_key(plan_id),)).fetchone()
        lock = None
        if row is not None:
            try:
                lock = json.loads(row[0])
            except ValueError:
                lock = None
            pid = lock.get("pid") if isinstance(lock, dict) else None
            if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
                raise PlanError(f"plan {plan_id} has an unreadable execution lock ({row[0]!r}); "
                                f"make sure no drivemerge process is running it, then clear the meta row "
                                f"'{_lock_key(plan_id)}'")
            if _pid_alive(pid):
                raise PlanError(f"plan {plan_id} is being executed by pid {pid} on {lock.get('host', 'this host')} "
                                f"(since {lock.get('started_at', 'unknown')}); wait for it to finish or stop it first")
        conn.execute("DELETE FROM meta WHERE key = ?", (_lock_key(plan_id),))
        set_status(conn, plan_id, "failed")
        db.journal(conn, "reset", plan_id=plan_id, detail=json.dumps({"lock": lock}, sort_keys=True))
