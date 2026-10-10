-- drive-merge catalog schema (SQLite >= 3.34, FTS5 trigram tokenizer).
--
-- This file is the shared contract between modules. Conventions:
--   * Paths are relative to the remote root, '/'-separated, no leading or
--     trailing '/'. The remote root itself is ''.
--   * Timestamps are ISO-8601 UTC strings: 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z'.
--   * Hashes are lowercase hex, exactly as `rclone lsjson --hash` prints them.
--   * Nothing in this database is a file's bytes; it is metadata only.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Mirrors [[remotes]] from config.toml (config is the source of truth).
CREATE TABLE IF NOT EXISTS remotes (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,      -- logical name, e.g. 'gdrive'
    provider         TEXT NOT NULL,             -- key into providers.PROVIDERS
    read_remote      TEXT NOT NULL,             -- rclone path used for listing/reading, e.g. 'gdrive-ro:'
    write_remote     TEXT,                      -- rclone path used by the executor; NULL = read-only
    case_insensitive INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    remote_id   INTEGER NOT NULL REFERENCES remotes(id),
    root        TEXT NOT NULL DEFAULT '',       -- sub-path that was scanned ('' = whole remote)
    status      TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    files_seen  INTEGER NOT NULL DEFAULT 0,
    dirs_seen   INTEGER NOT NULL DEFAULT 0,
    bytes_seen  INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

-- One row per file or directory ever seen on a remote.
CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY,
    remote_id       INTEGER NOT NULL REFERENCES remotes(id),
    -- Stable identity within a remote: 'id:<provider id>' when the backend
    -- reports IDs (Drive allows several files with the same path!), else
    -- 'p:<path>'.
    entry_key       TEXT NOT NULL,
    path            TEXT NOT NULL,
    parent          TEXT NOT NULL,              -- dirname(path), '' for top level
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,              -- NFC + casefold, for matching (macOS/iCloud emit NFD)
    ext             TEXT NOT NULL DEFAULT '',   -- lowercase, without the dot
    category        TEXT NOT NULL DEFAULT 'other', -- categories.category_of(ext, mime)
    is_dir          INTEGER NOT NULL,
    size            INTEGER,                    -- NULL for dirs and unknown-size objects
    mtime           TEXT,
    mime            TEXT,
    provider_id     TEXT,
    md5             TEXT,
    sha1            TEXT,
    sha256          TEXT,
    dropbox         TEXT,
    quickxor        TEXT,
    is_native_doc   INTEGER NOT NULL DEFAULT 0, -- Google Docs/Sheets etc.: no bytes, no hash
    content_group   INTEGER,                    -- set by identity.rebuild_groups(); NULL = not hash-identified
    present         INTEGER NOT NULL DEFAULT 1, -- 0 = missing from the latest complete scan (tombstone)
    first_seen_scan INTEGER NOT NULL,
    last_seen_scan  INTEGER NOT NULL,
    row_version     INTEGER NOT NULL,           -- monotonic, drives the change feed
    UNIQUE (remote_id, entry_key)
);
CREATE INDEX IF NOT EXISTS entries_path     ON entries(remote_id, path);
CREATE INDEX IF NOT EXISTS entries_parent   ON entries(remote_id, parent);
CREATE INDEX IF NOT EXISTS entries_size     ON entries(size) WHERE is_dir = 0 AND present = 1;
CREATE INDEX IF NOT EXISTS entries_md5      ON entries(md5) WHERE md5 IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_sha1     ON entries(sha1) WHERE sha1 IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_sha256   ON entries(sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_dropbox  ON entries(dropbox) WHERE dropbox IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_quickxor ON entries(quickxor) WHERE quickxor IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_group    ON entries(content_group) WHERE content_group IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_namekey  ON entries(name_norm, size);
CREATE INDEX IF NOT EXISTS entries_rowver   ON entries(row_version);

-- Full-text search over names and paths. The trigram tokenizer gives
-- substring search that works for CJK file names (unicode61 does not
-- segment Chinese/Japanese). Queries shorter than 3 characters fall back to
-- LIKE in search.py. The name column indexes name_norm (NFC + casefold) so
-- NFD names written by macOS/iCloud match NFC queries.
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    name_norm, path,
    content = 'entries', content_rowid = 'id',
    tokenize = 'trigram'
);
CREATE TRIGGER IF NOT EXISTS entries_fts_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, name_norm, path) VALUES (new.id, new.name_norm, new.path);
END;
CREATE TRIGGER IF NOT EXISTS entries_fts_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, name_norm, path) VALUES ('delete', old.id, old.name_norm, old.path);
END;
CREATE TRIGGER IF NOT EXISTS entries_fts_au AFTER UPDATE OF name_norm, path ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, name_norm, path) VALUES ('delete', old.id, old.name_norm, old.path);
    INSERT INTO entries_fts(rowid, name_norm, path) VALUES (new.id, new.name_norm, new.path);
END;

-- Hashes computed locally from one download of the bytes. Each row links
-- hash domains that providers cannot link themselves (Drive md5 <-> Dropbox
-- content hash <-> OneDrive QuickXorHash), so one download can confirm
-- copies on every other cloud without downloading them.
CREATE TABLE IF NOT EXISTS hash_bridges (
    id              INTEGER PRIMARY KEY,
    size            INTEGER NOT NULL,
    md5             TEXT NOT NULL,
    sha1            TEXT NOT NULL,
    sha256          TEXT NOT NULL UNIQUE,
    dropbox         TEXT NOT NULL,
    quickxor        TEXT NOT NULL,
    -- The entry whose bytes were hashed. For an entry without native hashes
    -- (iCloud) this link is its only route to an exact identity, valid while
    -- its size and mtime still equal what was downloaded.
    source_entry_id INTEGER REFERENCES entries(id),
    source_mtime    TEXT,
    computed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,                 -- 'dedupe' | 'consolidate' | 'reorganize' | 'manual'
    status       TEXT NOT NULL CHECK (status IN
                   ('draft', 'simulated', 'approved', 'executing', 'done', 'failed', 'cancelled')),
    created_by   TEXT NOT NULL,                 -- 'cli' | 'api' | 'mcp' ...
    params       TEXT,                          -- JSON: builder parameters, for reproducibility
    digest       TEXT,                          -- set by the last simulation; approval binds to it
    snapshot     TEXT,                          -- JSON {remote_id: last complete scan id} at simulation
    created_at   TEXT NOT NULL,
    simulated_at TEXT,
    approved_at  TEXT,
    approved_by  TEXT,
    finished_at  TEXT
);

-- Ops are immutable once written: editing a plan means creating a new plan.
CREATE TABLE IF NOT EXISTS plan_ops (
    id                INTEGER PRIMARY KEY,
    plan_id           INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL,         -- execution order, 1-based
    op                TEXT NOT NULL CHECK (op IN ('copy', 'move', 'delete')),
    src_entry_id      INTEGER REFERENCES entries(id), -- NULL when src is a file created earlier in this plan
    src_remote_id     INTEGER NOT NULL REFERENCES remotes(id),
    src_path          TEXT NOT NULL,
    dst_remote_id     INTEGER REFERENCES remotes(id), -- copy/move only
    dst_path          TEXT,
    witness_remote_id INTEGER REFERENCES remotes(id), -- delete only: the surviving copy
    witness_path      TEXT,
    expect_size       INTEGER,                  -- precondition snapshot of src, checked live before executing
    expect_hash       TEXT,                     -- '<type>:<hex>' e.g. 'md5:9e10...' or NULL
    expect_mtime      TEXT,
    depends_on        INTEGER,                  -- seq of an op that must be 'done' first
    reason            TEXT,
    status            TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
    error             TEXT,
    executed_at       TEXT,
    UNIQUE (plan_id, seq)
);

CREATE TABLE IF NOT EXISTS simulations (
    id         INTEGER PRIMARY KEY,
    plan_id    INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    digest     TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    report     TEXT NOT NULL,                   -- JSON SimulationReport
    created_at TEXT NOT NULL
);

-- Append-only audit log of everything that touched a real remote or a plan's lifecycle.
CREATE TABLE IF NOT EXISTS journal (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    plan_id INTEGER,
    op_id   INTEGER,
    action  TEXT NOT NULL,                      -- 'approve' | 'precheck' | 'copy' | 'verify' | 'quarantine' | 'error' ...
    detail  TEXT                                -- JSON
);
