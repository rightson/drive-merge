"""SQLite connection, migrations and small shared helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def utcnow() -> str:
    """Current time as the catalog's canonical ISO-8601 UTC string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and migrate) the catalog. Use ':memory:' in tests."""
    path = str(path)
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        path = str(Path(path).expanduser())
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    sql = resources.files("drivemerge").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    conn.execute("INSERT INTO meta(key, value) VALUES ('row_version', '0') ON CONFLICT(key) DO NOTHING")


def next_row_version(conn: sqlite3.Connection) -> int:
    """Allocate a monotonic row_version for the change feed.

    Call once per batch (inside the batch's transaction) and stamp every row
    written in that batch with the returned value.
    """
    row = conn.execute(
        "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'row_version' RETURNING value"
    ).fetchone()
    return int(row[0])


class transaction:
    """`with transaction(conn):` — BEGIN IMMEDIATE / COMMIT / ROLLBACK.

    Connections are opened in autocommit mode (isolation_level=None), so
    multi-statement writes must be wrapped explicitly. Nesting is not
    supported; inner code should not open its own transaction.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")


def remote_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM remotes WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError(f"unknown remote {name!r}")
    return int(row[0])


def snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    """{remote_id(str): id of the latest complete scan} — identifies catalog state.

    A plan's approval is bound to this snapshot; any newer complete scan
    invalidates it and forces a re-simulation.
    """
    rows = conn.execute(
        "SELECT remote_id, MAX(id) FROM scans WHERE status = 'complete' GROUP BY remote_id ORDER BY remote_id"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def journal(conn: sqlite3.Connection, action: str, *, plan_id: int | None = None,
            op_id: int | None = None, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO journal(ts, plan_id, op_id, action, detail) VALUES (?, ?, ?, ?, ?)",
        (utcnow(), plan_id, op_id, action, detail),
    )
