"""Execute approved plans against the real clouds.

STUB — implemented in wave 2. Contract:

    class ExecutionError(Exception)
    class ExecutionDisabled(ExecutionError)      # [safety] execution_enabled = false

    @dataclass
    class ExecResult: plan_id: int; status: str; done: int; failed: int; skipped: int; error: str | None

    def quarantine_path(config, plan_id, path) -> str
    def execute_plan(conn, rclone, config, plan_id, *, tmpdir=None, progress=None) -> ExecResult
    def restore_plan(conn, rclone, config, plan_id) -> ExecResult
    def purge_quarantine(conn, rclone, config, plan_id, *, older_than_days=30) -> ExecResult
"""

from __future__ import annotations

from dataclasses import dataclass


class ExecutionError(Exception):
    pass


class ExecutionDisabled(ExecutionError):
    pass


@dataclass
class ExecResult:
    plan_id: int
    status: str
    done: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None


def quarantine_path(config, plan_id: int, path: str) -> str:
    raise NotImplementedError


def execute_plan(conn, rclone, config, plan_id: int, *, tmpdir=None, progress=None) -> ExecResult:
    raise NotImplementedError


def restore_plan(conn, rclone, config, plan_id: int) -> ExecResult:
    raise NotImplementedError


def purge_quarantine(conn, rclone, config, plan_id: int, *, older_than_days: int = 30) -> ExecResult:
    raise NotImplementedError
