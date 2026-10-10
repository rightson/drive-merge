"""Opening a catalog made by an older release.

CREATE TABLE IF NOT EXISTS never adds a column to an existing table and never
replaces a trigger, so db.migrate has to. The old database here is built from
the first schema ever committed (tests/data/schema_cfb9079.sql, verbatim
`git show cfb9079:src/drivemerge/schema.sql`) and opened with the current code.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from drivemerge import db, identity, search

OLD_SCHEMA = Path(__file__).parent / "data" / "schema_cfb9079.sql"
T0 = "2024-03-03T00:00:00Z"
NFD_NAME = unicodedata.normalize("NFD", "Café Résumé.PDF")       # what macOS / iCloud report


# --------------------------------------------------------------------------- helpers

def _sha(tag: str, salt: str = "") -> str:
    return hashlib.sha256((salt + tag).encode()).hexdigest()


def _add_entry(conn: sqlite3.Connection, rid: int, scan: int, path: str, *, size: int = 10, name: str | None = None,
               **hashes) -> int:
    """One live file in a database of any schema version (the columns entries always had)."""
    name = name or path.rsplit("/", 1)[-1]
    return conn.execute(
        """INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category, is_dir, size,
             mtime, md5, sha1, sha256, dropbox, quickxor, is_native_doc, present, first_seen_scan,
             last_seen_scan, row_version)
           VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,0,1,?,?,1) RETURNING id""",
        (rid, f"p:{path}", path, path.rpartition("/")[0], name, unicodedata.normalize("NFC", name).casefold(),
         name.rpartition(".")[2].lower(), "other", size, T0, hashes.get("md5"), hashes.get("sha1"),
         hashes.get("sha256"), hashes.get("dropbox"), hashes.get("quickxor"), scan, scan),
    ).fetchone()[0]


def _populate(conn: sqlite3.Connection, *, source_mtime_column: bool) -> dict[str, int]:
    """A small catalog: a Drive copy, an iCloud copy (NFD name) bridged from its own bytes, a plan and a journal."""
    rid = {}
    scan = {}
    for name, provider in (("gd", "drive"), ("ic", "iclouddrive")):
        rid[name] = conn.execute(
            "INSERT INTO remotes(name, provider, read_remote, write_remote, case_insensitive, created_at) "
            "VALUES (?,?,?,?,0,?) RETURNING id", (name, provider, f"{name}:", f"{name}:", T0)).fetchone()[0]
        scan[name] = conn.execute(
            "INSERT INTO scans(remote_id, status, started_at, finished_at) VALUES (?, 'complete', ?, ?) RETURNING id",
            (rid[name], T0, T0)).fetchone()[0]
    h = {"md5": hashlib.md5(b"A").hexdigest(), "sha1": hashlib.sha1(b"A").hexdigest(), "sha256": _sha("A"),
         "dropbox": _sha("A", "dbx"), "quickxor": hashlib.sha1(b"qxA").hexdigest()}
    ids = {
        "drive": _add_entry(conn, rid["gd"], scan["gd"], "Docs/Report.pdf", md5=h["md5"], sha1=h["sha1"],
                            sha256=h["sha256"]),
        "icloud": _add_entry(conn, rid["ic"], scan["ic"], f"Docs/{NFD_NAME}", name=NFD_NAME),
        "other": _add_entry(conn, rid["gd"], scan["gd"], "Docs/Other.txt", size=3, md5="ff"),
    }
    cols = "size, md5, sha1, sha256, dropbox, quickxor, source_entry_id, "
    marks = "?, ?, ?, ?, ?, ?, ?, "
    values = [10, h["md5"], h["sha1"], h["sha256"], h["dropbox"], h["quickxor"], ids["icloud"]]
    if source_mtime_column:
        cols, marks, values = cols + "source_mtime, ", marks + "?, ", values + [T0]
    conn.execute(f"INSERT INTO hash_bridges({cols}computed_at) VALUES ({marks}?)", (*values, T0))
    plan = conn.execute("INSERT INTO plans(title, kind, status, created_by, created_at) "
                        "VALUES ('keep me', 'dedupe', 'draft', 'cli', ?) RETURNING id", (T0,)).fetchone()[0]
    conn.execute("INSERT INTO plan_ops(plan_id, seq, op, src_remote_id, src_path) VALUES (?, 1, 'delete', ?, 'x')",
                 (plan, rid["gd"]))
    conn.execute("INSERT INTO journal(ts, plan_id, action) VALUES (?, ?, 'approve')", (T0, plan))
    return ids


def make_v1_catalog(path: Path) -> dict[str, int]:
    """A catalog exactly as the first release left it: no source_mtime, FTS on `name`, no workspace tables."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(OLD_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1'), ('row_version', '7')")
    ids = _populate(conn, source_mtime_column=False)
    conn.close()
    return ids


def make_previous_catalog(path: Path) -> dict[str, int]:
    """The release just before bridge_sources: everything current except that table; still schema_version 1."""
    conn = db.connect(path)
    conn.execute("DROP TABLE bridge_sources")
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    ids = _populate(conn, source_mtime_column=True)
    conn.close()
    return ids


def shape(conn: sqlite3.Connection) -> dict:
    """Columns of every table (by name: ALTER appends) and the SQL of every index and trigger, whitespace aside."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    out = {t: {r[1]: tuple(r)[2:] for r in conn.execute(f"PRAGMA table_info({t})")} for t in tables}
    for r in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE type IN ('index', 'trigger') ORDER BY name"):
        out[f"{r[0]}:{r[1]}"] = re.sub(r"\s+", " ", r[2] or "")
    return out


def check_fts(conn: sqlite3.Connection) -> None:
    """The full-text index agrees with the entries table (raises sqlite3.DatabaseError otherwise)."""
    conn.execute("INSERT INTO entries_fts(entries_fts, rank) VALUES ('integrity-check', 1)")


def meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


@pytest.fixture
def fresh_shape():
    conn = db.connect(":memory:")
    try:
        return shape(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------- from the first release

def test_fixture_is_really_the_old_schema(tmp_path):
    path = tmp_path / "old.db"
    make_v1_catalog(path)
    conn = sqlite3.connect(path)
    assert "source_mtime" not in [r[1] for r in conn.execute("PRAGMA table_info(hash_bridges)")]
    assert [r[1] for r in conn.execute("PRAGMA table_info(entries_fts)")] == ["name", "path"]
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name IN ('drafts', 'dir_stats', 'bridge_sources')").fetchall()


def test_first_release_catalog_gets_the_current_schema(tmp_path, fresh_shape):
    path = tmp_path / "old.db"
    make_v1_catalog(path)
    conn = db.connect(path)
    assert shape(conn) == fresh_shape                       # columns, indexes, triggers, new tables: all of it
    assert meta(conn, "schema_version") == str(db.SCHEMA_VERSION) == "2"
    assert meta(conn, "fts_rebuild") is None
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_first_release_data_survives(tmp_path):
    path = tmp_path / "old.db"
    ids = make_v1_catalog(path)
    conn = db.connect(path)
    count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert (count("entries"), count("remotes"), count("scans"), count("hash_bridges")) == (3, 2, 2, 1)
    assert (count("plans"), count("plan_ops"), count("journal")) == (1, 1, 1)
    assert conn.execute("SELECT title FROM plans").fetchone()[0] == "keep me"
    assert meta(conn, "row_version") == "7"                  # the change feed's counter is not reset
    assert conn.execute("SELECT path FROM entries WHERE id = ?", (ids["drive"],)).fetchone()[0] == "Docs/Report.pdf"


def test_first_release_full_text_index_is_rebuilt_on_name_norm(tmp_path):
    path = tmp_path / "old.db"
    ids = make_v1_catalog(path)
    conn = db.connect(path)
    check_fts(conn)
    # the NFD name now matches an NFC, lower-case query (the old `name` index could not)
    assert [r["id"] for r in search.search(conn, "résumé")] == [ids["icloud"]]
    assert [r["id"] for r in search.search(conn, "report")] == [ids["drive"]]

    new = _add_entry(conn, 1, 1, "Docs/Quarterly.xlsx")                      # the recreated triggers keep it current
    assert [r["id"] for r in search.search(conn, "quarterly")] == [new]
    conn.execute("UPDATE entries SET name_norm = 'annual.xlsx', path = 'Docs/Annual.xlsx' WHERE id = ?", (new,))
    assert search.search(conn, "quarterly") == []
    assert [r["id"] for r in search.search(conn, "annual")] == [new]
    conn.execute("DELETE FROM entries WHERE id = ?", (new,))
    assert search.search(conn, "annual") == []
    check_fts(conn)


def test_first_release_bridge_source_is_kept_but_never_valid_without_an_mtime(tmp_path):
    path = tmp_path / "old.db"
    ids = make_v1_catalog(path)
    conn = db.connect(path)
    (src,) = conn.execute("SELECT entry_id, size, mtime FROM bridge_sources").fetchall()
    assert tuple(src) == (ids["icloud"], 10, None)           # the old schema never recorded when it was hashed
    stats = identity.rebuild_groups(conn)
    assert conn.execute("SELECT content_group FROM entries WHERE id = ?", (ids["icloud"],)).fetchone()[0] is None
    assert stats.hash_conflicts == 0
    assert [c.entry_id for c in identity.bridge_candidates(conn)] == [ids["icloud"]]    # offered for a fresh download


def test_first_release_catalog_works_end_to_end(tmp_path):
    """Re-bridging after the upgrade links the hashless copy through the new table."""
    path = tmp_path / "old.db"
    ids = make_v1_catalog(path)
    conn = db.connect(path)
    h = {"md5": hashlib.md5(b"A").hexdigest(), "sha1": hashlib.sha1(b"A").hexdigest(), "sha256": _sha("A"),
         "dropbox": _sha("A", "dbx"), "quickxor": hashlib.sha1(b"qxA").hexdigest(), "size": 10}
    bid = identity.record_bridge(conn, h, source_entry_id=ids["icloud"], source_mtime=T0)
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 1       # same bridge, now with an mtime
    assert conn.execute("SELECT mtime FROM bridge_sources WHERE bridge_id = ?", (bid,)).fetchone()[0] == T0
    identity.rebuild_groups(conn)
    g = {r["id"]: r["content_group"] for r in conn.execute("SELECT id, content_group FROM entries")}
    assert g[ids["icloud"]] == g[ids["drive"]] == ids["drive"]


# --------------------------------------------------------------------------- from the release before bridge_sources

def test_previous_release_catalog_gains_bridge_sources_filled_from_its_slot(tmp_path, fresh_shape):
    path = tmp_path / "prev.db"
    ids = make_previous_catalog(path)
    conn = db.connect(path)
    assert shape(conn) == fresh_shape
    assert meta(conn, "schema_version") == "2"
    assert meta(conn, "fts_rebuild") is None                  # its full-text index was already right: not touched
    (src,) = conn.execute("SELECT entry_id, size, mtime FROM bridge_sources").fetchall()
    assert tuple(src) == (ids["icloud"], 10, T0)
    identity.rebuild_groups(conn)
    g = {r["id"]: r["content_group"] for r in conn.execute("SELECT id, content_group FROM entries")}
    assert g[ids["icloud"]] == g[ids["drive"]] == ids["drive"]       # the old link still counts
    assert identity.bridge_candidates(conn) == []


# --------------------------------------------------------------------------- behaviour of migrate itself

def test_new_catalogs_record_the_current_version(tmp_path):
    for target in (":memory:", tmp_path / "new.db"):
        conn = db.connect(target)
        assert meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
        assert meta(conn, "row_version") == "0"
        assert meta(conn, "fts_rebuild") is None
        conn.close()


def test_reconnecting_changes_nothing(tmp_path):
    path = tmp_path / "old.db"
    make_v1_catalog(path)
    db.connect(path).close()
    conn = db.connect(path)
    before = (conn.execute("PRAGMA schema_version").fetchone()[0], shape(conn), meta(conn, "schema_version"))
    conn.close()
    conn = db.connect(path)
    assert (conn.execute("PRAGMA schema_version").fetchone()[0], shape(conn), meta(conn, "schema_version")) == before


def test_a_catalog_from_a_newer_release_is_refused(tmp_path):
    path = tmp_path / "new.db"
    db.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="newer"):
        db.connect(path)


def test_an_interrupted_upgrade_is_finished_by_the_next_connection(tmp_path, monkeypatch, fresh_shape):
    path = tmp_path / "old.db"
    ids = make_v1_catalog(path)

    def crash(conn):
        raise RuntimeError("power cut")

    with monkeypatch.context() as m:
        m.setattr(db, "_upgrade_after", crash)
        with pytest.raises(RuntimeError, match="power cut"):
            db.connect(path)
    raw = sqlite3.connect(path)                                # in between: version not bumped, refill still owed
    assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "1"
    assert raw.execute("SELECT value FROM meta WHERE key = 'fts_rebuild'").fetchone()[0] == "1"
    raw.close()

    conn = db.connect(path)
    assert meta(conn, "schema_version") == "2" and meta(conn, "fts_rebuild") is None
    assert shape(conn) == fresh_shape
    check_fts(conn)
    assert [r["id"] for r in search.search(conn, "résumé")] == [ids["icloud"]]


def test_a_failure_while_altering_rolls_the_whole_step_back(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    make_v1_catalog(path)
    real = db._columns

    def flaky(conn, table):
        if table == "entries_fts":                         # reached after the ALTER of hash_bridges succeeded
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, table)

    with monkeypatch.context() as m:
        m.setattr(db, "_columns", flaky)
        with pytest.raises(sqlite3.OperationalError):
            db.connect(path)
    raw = sqlite3.connect(path)
    assert "source_mtime" not in [r[1] for r in raw.execute("PRAGMA table_info(hash_bridges)")]
    assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "1"
    raw.close()
    assert meta(db.connect(path), "schema_version") == "2"       # and a later attempt simply works


# --------------------------------------------------------------------------- savepoint

def test_savepoint_rolls_back_only_its_own_block():
    conn = db.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    with db.transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(ValueError):
            with db.savepoint(conn):
                conn.execute("INSERT INTO t VALUES (2)")
                raise ValueError("no")
        with db.savepoint(conn):
            conn.execute("INSERT INTO t VALUES (3)")
    assert [r[0] for r in conn.execute("SELECT x FROM t ORDER BY x")] == [1, 3]
    assert not conn.in_transaction


def test_savepoint_outside_a_transaction_is_atomic_and_commits():
    conn = db.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    with db.savepoint(conn):
        conn.execute("INSERT INTO t VALUES (1)")
    assert not conn.in_transaction
    with pytest.raises(ValueError):
        with db.savepoint(conn):
            conn.execute("INSERT INTO t VALUES (2)")
            raise ValueError("no")
    assert [r[0] for r in conn.execute("SELECT x FROM t")] == [1] and not conn.in_transaction
