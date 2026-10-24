"""SQLite connection, migrations and small shared helpers."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 3          # 2: hash_bridges.source_mtime, FTS on name_norm, bridge_sources; 3: rule_versions
# What may join two files into one content_group (identity.py). 1: any shared hash; 2: only a strong one
# (sha1, sha256, the Dropbox hash), because md5 and QuickXor agree for different bytes too; 3: sha1 is no longer
# strong (collisions can be crafted), so only sha256 and the Dropbox hash are. A catalog whose
# meta.identity_rules is lower holds groups the current rules would not build and is regrouped once.
IDENTITY_RULES = 3
DEFAULT_QUARANTINE_DIR = ".drive-merge-quarantine"


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


# Columns added after the first release. CREATE TABLE IF NOT EXISTS (schema.sql) never touches an
# existing table, so a catalog from an older release gets them here.
_ADDED_COLUMNS = (("hash_bridges", "source_mtime", "TEXT"),)


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema, or bring a catalog from an older release up to date (idempotent).

    schema.sql only creates what is missing, so an older catalog is first stripped of what it
    cannot add (see _upgrade_before) and afterwards given the data the new objects need.
    meta.schema_version is written last: a crash in between just repeats the steps.
    """
    sql = resources.files("drivemerge").joinpath("schema.sql").read_text(encoding="utf-8")
    current = _schema_version(conn)
    if current is not None and current > SCHEMA_VERSION:
        raise RuntimeError(f"catalog has schema version {current}, newer than this drive-merge ({SCHEMA_VERSION})")
    upgrading = current is not None and current < SCHEMA_VERSION
    if upgrading:
        _upgrade_before(conn)
    conn.executescript(sql)
    if upgrading:
        _upgrade_after(conn)
    elif current is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO NOTHING",
                     (str(SCHEMA_VERSION),))
    conn.execute("INSERT INTO meta(key, value) VALUES ('row_version', '0') ON CONFLICT(key) DO NOTHING")
    digest_secret(conn)
    _regroup_if_stale(conn)


def _schema_version(conn: sqlite3.Connection) -> int | None:
    """The version a catalog records, None for a file that has none yet."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:               # no meta table: a new file
        return None
    return None if row is None else int(row[0])


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Column names of a table or virtual table ([] when it does not exist)."""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _upgrade_before(conn: sqlite3.Connection) -> None:
    """Add missing columns and drop the full-text index when it still covers `name` instead of `name_norm`.

    The index and its triggers are recreated from schema.sql, then refilled by _upgrade_after;
    the meta flag is written in the same transaction as the drop so that refill cannot be lost.
    Everything is re-checked inside the transaction, so two processes upgrading at once cannot both ALTER.
    """
    with transaction(conn):
        for table, column, decl in _ADDED_COLUMNS:
            cols = _columns(conn, table)
            if cols and column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        fts = _columns(conn, "entries_fts")
        if fts and "name_norm" not in fts:
            for trigger in ("entries_fts_ai", "entries_fts_ad", "entries_fts_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute("DROP TABLE entries_fts")
            conn.execute("INSERT INTO meta(key, value) VALUES ('fts_rebuild', '1') "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value")


def _upgrade_after(conn: sqlite3.Connection) -> None:
    """Fill what schema.sql just created from the old data, then record the new version.

    Version 3 only adds the rule_versions table, which schema.sql creates empty: from 2 there is nothing to fill.
    """
    if (_schema_version(conn) or 0) < 2:              # still the old version: it is written last
        _fill_version_2(conn)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))


def _fill_version_2(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        if conn.execute("SELECT 1 FROM meta WHERE key = 'fts_rebuild'").fetchone():
            conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")
            conn.execute("DELETE FROM meta WHERE key = 'fts_rebuild'")
        conn.execute(                                  # the one source slot of version 1 becomes a row
            "INSERT OR IGNORE INTO bridge_sources(bridge_id, entry_id, size, mtime) "
            "SELECT id, source_entry_id, size, source_mtime FROM hash_bridges WHERE source_entry_id IS NOT NULL")
    # dir_stats only exists since version 2 and a scan is what fills it; without this every folder of an
    # upgraded catalog would read as empty in the workspace until the next scan. Late import: dirstats needs db.
    from . import dirstats
    dirstats.refresh(conn)


def digest_secret(conn: sqlite3.Connection) -> bytes:
    """The key plan digests are made with: random, per catalog, kept in `meta` and never sent anywhere.

    The first 8 characters of a digest are what a person types to approve a plan. Without a key they
    would be a plain hash of public data (the ops and the scan ids anyone with the API token can read),
    so a client that was never shown the code could compute it. `migrate` creates it, so the first
    simulation never has to write it inside a read transaction it will roll back.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'digest_secret'").fetchone()
    if row is None:                     # read first: this is called inside a read transaction that must not write
        conn.execute("INSERT INTO meta(key, value) VALUES ('digest_secret', ?) ON CONFLICT(key) DO NOTHING",
                     (secrets.token_hex(32),))
        row = conn.execute("SELECT value FROM meta WHERE key = 'digest_secret'").fetchone()
    return bytes.fromhex(row[0])


def _regroup_if_stale(conn: sqlite3.Connection) -> None:
    """Rebuild content groups made under older identity rules, once, then record that they follow IDENTITY_RULES.

    Without it a catalog grouped when md5, sha1 or QuickXor could join files keeps those groups (and the duplicate
    lists, dedupe plans and workspace badges built on them) until someone happens to run `identity rebuild`.
    The flag is written last, so an interrupted rebuild is simply repeated by the next connection; an empty
    catalog (a new file) has nothing to rebuild and only gets the flag. Late imports: both modules need db.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'identity_rules'").fetchone()
    if row is not None and int(row[0]) >= IDENTITY_RULES:
        return
    if conn.execute("SELECT 1 FROM entries WHERE content_group IS NOT NULL LIMIT 1").fetchone():
        from . import dirstats, identity
        identity.rebuild_groups(conn)
        dirstats.refresh(conn)          # dup_files counts content groups
    conn.execute("INSERT INTO meta(key, value) VALUES ('identity_rules', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(IDENTITY_RULES),))


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


class savepoint:
    """`with savepoint(conn):` — all-or-nothing writes that also work inside a `transaction` (or alone).

    Rolls back to the start of the block when it raises; the exception propagates.
    """

    def __init__(self, conn: sqlite3.Connection, name: str = "dm_savepoint"):
        self.conn = conn
        self.name = name

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute(f"SAVEPOINT {self.name}")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.conn.execute(f"ROLLBACK TO {self.name}")
        self.conn.execute(f"RELEASE {self.name}")


def remote_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM remotes WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError(f"unknown remote {name!r}")
    return int(row[0])


def quarantine_dir(conn: sqlite3.Connection) -> str:
    """Where the executor parks deleted files, as the last config sync recorded it ('' = no quarantine).

    The catalog scans quarantined files like any other, so every module that talks about "live"
    content needs to know the folder; storing it next to the data keeps all of them in agreement.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'quarantine_dir'").fetchone()
    return DEFAULT_QUARANTINE_DIR if row is None else row[0]


def set_quarantine_dir(conn: sqlite3.Connection, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('quarantine_dir', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (value.strip("/"),),
    )


def outside_quarantine(conn: sqlite3.Connection, alias: str = "") -> str:
    """SQL condition: the entry is not inside the quarantine folder (always true when there is none).

    Compared by whole path segments and literally, case-sensitively like the executor writes it.
    """
    q = quarantine_dir(conn).strip("/")
    if not q:
        return "1"
    lit = q.replace("'", "''")
    return f"NOT ({alias}path = '{lit}' OR substr({alias}path, 1, {len(q) + 1}) = '{lit}/')"


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
