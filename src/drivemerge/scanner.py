"""Ingest `rclone lsjson -R` listings into the catalog (incremental, streaming)."""

from __future__ import annotations

import posixpath
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db, dirstats, providers
from .categories import category_of
from .config import RemoteConfig
from .rclone import Rclone

NATIVE_DOC_MIME_PREFIX = "application/vnd.google-apps."

_MTIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$",
    re.IGNORECASE,
)

# Columns whose change is observable to the change feed (bumps row_version).
_OBSERVED = ("path", "parent", "name", "name_norm", "ext", "category", "is_dir", "size", "mtime",
             "mime", "provider_id", "md5", "sha1", "sha256", "dropbox", "quickxor", "is_native_doc")
# A change to any of these invalidates content_group (identity must be recomputed).
_CONTENT = ("size", "md5", "sha1", "sha256", "dropbox", "quickxor")

_COLS = ", ".join(_OBSERVED)
_VALUES = ", ".join(f":{c}" for c in _OBSERVED)

# Unchanged rows only get last_seen_scan touched. This statement deliberately
# does not name path/name, so the FTS update trigger (UPDATE OF name, path)
# never fires on a re-scan of an unchanged drive. A row this scan already wrote
# (last_seen_scan = :scan) is never matched: its key then belongs to an earlier
# item of the same listing (see _store).
_TOUCH_SQL = (
    "UPDATE entries SET last_seen_scan = :scan "
    "WHERE remote_id = :remote_id AND entry_key = :entry_key AND present = 1 AND last_seen_scan <> :scan AND "
    + " AND ".join(f"{c} IS :{c}" for c in _OBSERVED)
)

# Reached only for new or changed rows (or revived tombstones), which all bump row_version.
_UPSERT_SQL = (
    f"INSERT INTO entries(remote_id, entry_key, {_COLS}, content_group, present, "
    "first_seen_scan, last_seen_scan, row_version) "
    f"VALUES (:remote_id, :entry_key, {_VALUES}, NULL, 1, :scan, :scan, :rv) "
    "ON CONFLICT(remote_id, entry_key) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _OBSERVED)
    + ", content_group = CASE WHEN "
    + " OR ".join(f"entries.{c} IS NOT excluded.{c}" for c in _CONTENT)
    + " THEN NULL ELSE entries.content_group END"
    + ", present = 1, last_seen_scan = excluded.last_seen_scan, row_version = excluded.row_version"
    " WHERE entries.last_seen_scan <> excluded.last_seen_scan"
)


@dataclass
class ScanResult:
    scan_id: int
    remote: str
    root: str
    status: str
    files: int
    dirs: int
    bytes: int
    tombstoned: int
    error: str | None
    ambiguous: int = 0      # items that shared their key with an earlier item of the scan (see _store)


def parse_mtime(value: str | None) -> str | None:
    """rclone timestamp -> canonical UTC 'YYYY-MM-DDTHH:MM:SS.ffffffZ', or None.

    Parsed by hand because rclone emits nanoseconds (9 digits), which
    datetime.fromisoformat rejects. Fractions are truncated, not rounded.
    Year-1 times (Go's zero time, "unknown") and unparseable values give None.
    """
    if not value:
        return None
    m = _MTIME_RE.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, frac, tz = m.groups()
    try:
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
        if tz and tz.upper() != "Z":
            digits = tz[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            dt -= offset if tz[0] == "+" else -offset
    except (ValueError, OverflowError):
        return None
    if dt.year <= 1:
        return None
    micro = int((frac or "")[:6].ljust(6, "0"))
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{micro:06d}Z"


def normalize_item(item: dict, root: str = "") -> dict:
    """One lsjson object -> `entries` column values (everything the scanner owns)."""
    root = root.strip("/")
    rel = item["Path"]
    path = f"{root}/{rel}" if root else rel
    name = item.get("Name") or posixpath.basename(path)
    is_dir = bool(item.get("IsDir"))
    mime = item.get("MimeType") or None

    ext = ""
    if not is_dir:
        dot = name.rfind(".")
        if dot > 0:  # dot == 0 is a dotfile such as '.bashrc': no extension
            ext = name[dot + 1:].lower()

    raw_size = item.get("Size")
    size = None
    if not is_dir and isinstance(raw_size, int) and raw_size >= 0:
        size = raw_size
    unknown_size = not is_dir and isinstance(raw_size, int) and raw_size < 0
    is_native = not is_dir and (unknown_size or (mime or "").startswith(NATIVE_DOC_MIME_PREFIX))

    provider_id = item.get("ID")
    provider_id = str(provider_id) if provider_id not in (None, "") else None

    hashes = {t: None for t in providers.HASH_TYPES}
    for key, val in (item.get("Hashes") or {}).items():
        key = key.lower()
        if key in hashes and val:
            hashes[key] = str(val).lower()

    return {
        "entry_key": f"id:{provider_id}" if provider_id else f"p:{path}",
        "path": path,
        "parent": posixpath.dirname(path),
        "name": name,
        "name_norm": unicodedata.normalize("NFC", name).casefold(),
        "ext": ext,
        "category": "other" if is_dir else category_of(ext, mime),
        "is_dir": int(is_dir),
        "size": size,
        "mtime": parse_mtime(item.get("ModTime")),
        "mime": mime,
        "provider_id": provider_id,
        "is_native_doc": int(is_native),
        **hashes,
    }


def _store(conn: sqlite3.Connection, row: dict) -> bool:
    """Touch an unchanged row or upsert a new/changed one; False when this scan already wrote its key.

    Two items of one listing can share a key: rclone's local backend lists names that differ only in
    invalid UTF-8 bytes under the same U+FFFD spelling, and a backend may repeat a provider ID. Writing
    both onto one row would hide a file and rewrite the row on every scan, so the caller re-keys.
    """
    if conn.execute(_TOUCH_SQL, row).rowcount:
        return True
    return conn.execute(_UPSERT_SQL, row).rowcount > 0


def _flush(conn: sqlite3.Connection, rows: list[dict], remote_id: int, scan_id: int) -> int:
    """Upsert one batch under a single row_version so the change feed sees it atomically.

    An item whose key an earlier item of this scan already holds is stored as '<key>#2', '#3', ...
    (same path, so the simulator flags it AMBIGUOUS_PATH like a Drive duplicate); the keys are stable
    while the listing order is, so an unchanged rescan still writes nothing. Returns how many items
    needed such a key.
    """
    ambiguous = 0
    with db.transaction(conn):
        _require_running(conn, scan_id)
        rv = db.next_row_version(conn)
        for row in rows:
            row.update(remote_id=remote_id, scan=scan_id, rv=rv)
            base, n = row["entry_key"], 1
            while not _store(conn, row):
                n += 1
                row["entry_key"] = f"{base}#{n}"
            ambiguous += n > 1
    return ambiguous


def _require_running(conn: sqlite3.Connection, scan_id: int) -> None:
    """Stop a scan that a newer scan of the same remote has marked failed.

    Its later writes would stamp rows with an older scan id than the newer scan's and revive what the
    newer listing found gone, so it must not write or complete. Checked inside each writing transaction.
    """
    row = conn.execute("SELECT status FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if row is None or row[0] != "running":
        raise ScanSuperseded(f"scan {scan_id} was superseded by a newer scan of this remote; nothing more was written")


def _ensure_ancestors(conn: sqlite3.Connection, remote_id: int, scan_id: int, root: str) -> None:
    """Give a sub-root scan the directory rows rclone never lists: the root and every folder above it.

    Without them the workspace, which walks the tree by `parent`, cannot get from the remote's root to
    the scanned files. A folder the catalog already has (any key) is left alone; the rest are keyed
    'p:<path>' and are replaced by the real rows when a scan of a parent lists them.
    """
    rv = None
    parts = root.split("/")
    for i in range(1, len(parts) + 1):
        path = "/".join(parts[:i])
        if conn.execute("SELECT 1 FROM entries WHERE remote_id = ? AND path = ? AND is_dir = 1 AND present = 1",
                        (remote_id, path)).fetchone():
            continue
        rv = db.next_row_version(conn) if rv is None else rv
        row = normalize_item({"Path": path, "Name": parts[i - 1], "IsDir": True})
        row.update(remote_id=remote_id, scan=scan_id, rv=rv)
        _store(conn, row)


def _canonical_root(conn: sqlite3.Connection, remote_id: int, root: str) -> str:
    """`root` spelled as the catalog already knows its folders.

    Dropbox and OneDrive resolve `photos` to the folder `Photos` but list its children relative to it,
    so prefixing the typed spelling would rename every path below it. Names are matched NFC+casefold;
    from the first folder the catalog does not know (or knows twice) the typed spelling is kept.
    """
    parts = root.split("/")
    spelled: list[str] = []
    for seg in parts:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM entries WHERE remote_id = ? AND parent = ? AND is_dir = 1 AND present = 1 "
            "AND name_norm = ?",
            (remote_id, "/".join(spelled), unicodedata.normalize("NFC", seg).casefold()))}
        if len(names) != 1:
            break
        spelled.append(names.pop())
    return "/".join(spelled + parts[len(spelled):])


def _tombstone(conn: sqlite3.Connection, remote_id: int, scan_id: int, root: str) -> int:
    """Mark rows this scan should have seen but did not. Called inside the completing transaction.

    Range comparison on path (not LIKE) because paths may contain % and _;
    'root/' <= p < 'root0' is exactly the descendants of root ('0' follows '/').
    The root directory itself is never listed by rclone, so it is left alone.
    """
    rv = db.next_row_version(conn)
    sql = "UPDATE entries SET present = 0, row_version = ? WHERE remote_id = ? AND present = 1 AND last_seen_scan < ?"
    args: list = [rv, remote_id, scan_id]
    if root:
        sql += " AND path >= ? AND path < ?"
        args += [root + "/", root + "0"]
    return conn.execute(sql, args).rowcount


class SuspiciousScan(Exception):
    """A listing that succeeded but lost most of what was there before.

    Expired credentials, a half-mounted folder or a provider hiccup can make
    rclone exit 0 with an (almost) empty listing. Tombstoning on that would
    make the catalog claim the files are gone; refuse unless forced.
    """


class ScanSuperseded(Exception):
    """A newer scan of the same remote started and marked this one failed; it stops writing."""


# Refuse to tombstone more than this share of a sizeable subtree in one scan. Small subtrees are
# judged differently: the share means little there, but a listing that holds nothing at all of one
# with a handful of entries is the signature of an unmounted or emptied source (a user tidying up
# deletes a few files, not everything at once), so it is refused too.
_TOMBSTONE_GUARD_RATIO = 0.5
_TOMBSTONE_GUARD_MIN_ROWS = 50
_TOMBSTONE_GUARD_EMPTY_MIN_ROWS = 5


def _check_tombstone_guard(conn: sqlite3.Connection, remote_id: int, scan_id: int, root: str) -> None:
    where = "remote_id = ? AND present = 1"
    args: list = [remote_id]
    if root:
        where += " AND path >= ? AND path < ?"
        args += [root + "/", root + "0"]
    live, missing = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(last_seen_scan < ?), 0) FROM entries WHERE {where}",
        [scan_id, *args],
    ).fetchone()
    wiped = live >= _TOMBSTONE_GUARD_EMPTY_MIN_ROWS and missing == live
    if wiped or (live >= _TOMBSTONE_GUARD_MIN_ROWS and missing > live * _TOMBSTONE_GUARD_RATIO):
        raise SuspiciousScan(
            f"listing is missing {missing} of {live} previously seen entries; nothing was marked deleted. "
            "If they really are gone, scan again with force=True (CLI: --force)."
        )


def _mark_failed(conn: sqlite3.Connection, scan_id: int, exc: BaseException) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute(
            "UPDATE scans SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
            (db.utcnow(), f"{type(exc).__name__}: {exc}"[:4000], scan_id),
        )
    except sqlite3.Error:
        pass  # never mask the original exception


def _refresh_stats(conn: sqlite3.Connection, remote_id: int) -> None:
    """Best effort: bring dir_stats in step with rows a failed scan had already committed."""
    try:
        dirstats.refresh(conn, remote_ids=[remote_id])
    except sqlite3.Error:
        pass  # never mask the scan's own error


def scan_remote(conn: sqlite3.Connection, rclone: Rclone, remote: RemoteConfig, *, root: str = "",
                batch_size: int = 2000, progress: Callable[[int, int, int], None] | None = None,
                force: bool = False) -> ScanResult:
    """Scan `remote` (optionally only below `root`) into the catalog.

    The remote row must already exist (config.sync_remotes). Tombstoning only
    happens after the listing finished without error, so a failed or
    interrupted scan can never make files look deleted; a listing that lost
    most of a subtree raises SuspiciousScan unless `force`. One scan of a
    remote runs at a time: starting another marks a running one failed, and
    that one raises ScanSuperseded at its next write. A sub-root scan also
    stores the folders above the root, and a failed scan leaves dir_stats
    matching the rows it did commit.
    """
    remote_id = db.remote_id(conn, remote.name)
    root = root.strip("/")
    if root and conn.execute("SELECT case_insensitive FROM remotes WHERE id = ?", (remote_id,)).fetchone()[0]:
        root = _canonical_root(conn, remote_id, root)
    batch_size = max(1, batch_size)
    with db.transaction(conn):
        # A 'running' row left by a process that died mid-scan would otherwise stay running forever;
        # one that is alive finds out at its next write (_require_running).
        conn.execute(
            "UPDATE scans SET status = 'failed', finished_at = ?, error = 'interrupted before completion' "
            "WHERE remote_id = ? AND status = 'running'",
            (db.utcnow(), remote_id),
        )
        scan_id = conn.execute(
            "INSERT INTO scans(remote_id, root, status, started_at) VALUES (?, ?, 'running', ?) RETURNING id",
            (remote_id, root, db.utcnow()),
        ).fetchone()[0]

    files = dirs = size_total = ambiguous = 0
    written = False
    stream = None
    try:
        stream = iter(rclone.lsjson(remote.read, root, recursive=True,
                                    hash_types=remote.scan_hash_types, extra_args=remote.extra_args))
        batch: list[dict] = []
        for item in stream:
            row = normalize_item(item, root)
            if row["is_dir"]:
                dirs += 1
            else:
                files += 1
                size_total += row["size"] or 0
            batch.append(row)
            if len(batch) >= batch_size:
                ambiguous += _flush(conn, batch, remote_id, scan_id)
                written = True
                batch = []
                if progress:
                    progress(files, dirs, size_total)
        if batch:
            ambiguous += _flush(conn, batch, remote_id, scan_id)
            written = True
            if progress:
                progress(files, dirs, size_total)

        with db.transaction(conn):
            _require_running(conn, scan_id)
            if not force:
                _check_tombstone_guard(conn, remote_id, scan_id, root)
            tombstoned = _tombstone(conn, remote_id, scan_id, root)
            if root:
                _ensure_ancestors(conn, remote_id, scan_id, root)
            conn.execute(
                "UPDATE scans SET status = 'complete', finished_at = ?, files_seen = ?, dirs_seen = ?, "
                "bytes_seen = ? WHERE id = ?",
                (db.utcnow(), files, dirs, size_total, scan_id),
            )
    except BaseException as exc:
        _mark_failed(conn, scan_id, exc)
        if written and isinstance(exc, Exception):
            _refresh_stats(conn, remote_id)
        raise
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    return ScanResult(scan_id, remote.name, root, "complete", files, dirs, size_total, tombstoned, None, ambiguous)
