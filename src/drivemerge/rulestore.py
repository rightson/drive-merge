"""The layout rules' history: the active file, versions, drafts and proposals (docs/RULES.md section C).

The *active rules* are the text of one file (`Config.rules_file`); when it does not exist the packaged defaults
are active. The catalog's `rule_versions` table keeps every text that was ever active, the person's drafts and
proposals from Claude or any API client. Nothing here edits `config.toml`, and nothing but `activate` writes the
rules file. `activate` is human-only: the browser's human channel and the terminal's TTY gate call it, no
bearer-token route and no MCP tool does.

Observing: the file is read on every use, so an edit made in an editor takes effect at once. `observe` (called by
`Service.rules_state` and when a plan is proposed, never by a check, a preview or an estimate) records a file text
the history does not know as a version made by `file`, so the history is complete and the change is shown.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import stat
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import classify, db
from .config import Config

CODES: tuple[str, ...] = (
    "RULES_VERSION_NOT_FOUND", "RULES_NOT_DRAFT", "RULES_DRAFT_CHANGED", "RULES_INVALID", "RULES_ACTIVE_CHANGED",
    "RULES_FILE_UNWRITABLE", "RULES_FILE_MISSING", "RULES_FILE_UNREADABLE", "RULES_NO_FILE", "RULES_NOT_ACTIVATABLE",
    "RULES_BOTH", "DRAFT_UI_ONLY", "PREVIEW_EXPIRED", "PREVIEW_SUPERSEDED", "PREVIEW_TOO_LARGE",
)
MAX_TEXT = classify.MAX_RULES_BYTES
MAX_TITLE = 200
MAX_NOTE = 2000
CREATORS = ("ui", "mcp", "api", "cli", "file", "default")
STATUSES = ("draft", "proposed", "active", "retired", "dismissed")
INVALID_SHOWN = 10


class RulesError(Exception):
    """A refusal with a code and params; `status` is the HTTP status the routes answer with. The message is
    English, for terminals and API clients; the page words the code itself."""

    def __init__(self, status: int, code: str, message: str, **params):
        super().__init__(message)
        self.status, self.code, self.params = status, code, params


@dataclass
class ActiveRules:
    text: str
    sha256: str
    source: str                   # "file" | "default"
    path: str | None              # the file read (None for the packaged defaults)
    owned: bool                   # drive-merge's own rules.toml (no `[rules] path`)
    version_id: int | None        # the history row holding this text, if any
    error: dict | None            # {"code", "params"}: the file is missing, unreadable or does not parse

    def as_dict(self) -> dict:
        return asdict(self)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return db.utcnow()


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    names = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = dict(zip(names, tuple(row)))
        if "ok" in d:
            d["ok"] = bool(d["ok"])
        out.append(d)
    return out


_COLUMNS = ("id, status, text, sha256, ok, created_by, title, note, base_id, revision, created_at, updated_at, "
            "activated_at, activated_by")


def _check_text(text: object) -> str:
    if not isinstance(text, str):
        raise ValueError("the rules text must be a string")
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise classify.RuleError("RULES_TOO_LARGE", f"the rules text is larger than {MAX_TEXT // 1024} KB", limit=MAX_TEXT)
    return text


# --------------------------------------------------------------------------- the active rules

def _error_of(exc: classify.RuleError) -> dict:
    return {"code": exc.code, "params": dict(exc.params)}


def active(conn: sqlite3.Connection, config: Config) -> ActiveRules:
    """The rules in force: the active file's text, else the packaged defaults. Reads, never writes.

    A configured `[rules] path` whose file is missing is an error (RULES_FILE_MISSING), not the defaults; a file
    that cannot be read is RULES_FILE_UNREADABLE; one that does not parse carries its first RuleError. In those
    cases `error` is set and `text` is what could be read (empty when nothing could).
    """
    file = config.rules_file
    owned = config.rules_owned
    text: str | None = None
    sha: str | None = None
    error: dict | None = None
    source, path = "default", None
    if file is not None:
        unreadable = {"code": "RULES_FILE_UNREADABLE", "params": {"path": str(file)}}
        try:
            raw = file.read_bytes()
        except FileNotFoundError:
            if not owned:
                source, path, text = "file", str(file), ""
                error = {"code": "RULES_FILE_MISSING", "params": {"path": str(file)}}
        except (OSError, ValueError):
            source, path, text, error = "file", str(file), "", unreadable
        else:
            source, path = "file", str(file)
            try:
                text, sha = raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()
            except UnicodeDecodeError:
                text, error = "", unreadable
    if text is None:
        text = classify.default_rules_text()
    if sha is None:
        sha = sha256_of(text)
    if error is None:
        try:
            classify.load_rules(text)
        except classify.RuleError as exc:
            error = _error_of(exc)
    row = conn.execute("SELECT id FROM rule_versions WHERE sha256 = ? "
                       "ORDER BY (status = 'active') DESC, id DESC LIMIT 1", (sha,)).fetchone()
    return ActiveRules(text=text, sha256=sha, source=source, path=path, owned=owned,
                       version_id=row[0] if row else None, error=error)


def _active_row(conn: sqlite3.Connection) -> dict | None:
    rows = _rows(conn.execute(f"SELECT {_COLUMNS} FROM rule_versions WHERE status = 'active'"))
    return rows[0] if rows else None


def observe(conn: sqlite3.Connection, config: Config) -> ActiveRules:
    """Read the active file; when its text is not the `active` row's, retire that row and record the text as the
    new active version by `file` (or `default` for the packaged defaults), reusing a retired or proposed version
    with the same SHA-256. A missing or unreadable file changes nothing."""
    a = active(conn, config)
    if a.error is not None and a.error["code"] in ("RULES_FILE_MISSING", "RULES_FILE_UNREADABLE"):
        return a
    row = _active_row(conn)
    if row is not None and row["sha256"] == a.sha256:
        return a
    who = "file" if a.source == "file" else "default"
    with db.transaction(conn):
        row = _active_row(conn)                      # re-read under the write lock: another process may have adopted it
        if row is None or row["sha256"] != a.sha256:
            now = _now()
            if row is not None:
                conn.execute("UPDATE rule_versions SET status = 'retired', updated_at = ? WHERE id = ?", (now, row["id"]))
            same = conn.execute("SELECT id FROM rule_versions WHERE sha256 = ? AND status IN ('retired', 'proposed') "
                                "ORDER BY id DESC LIMIT 1", (a.sha256,)).fetchone()
            if same is not None:
                version = same[0]
                conn.execute("UPDATE rule_versions SET status = 'active', activated_at = ?, activated_by = ?, "
                             "updated_at = ? WHERE id = ?", (now, who, now, version))
            else:
                version = conn.execute(
                    "INSERT INTO rule_versions(status, text, sha256, ok, created_by, revision, created_at, updated_at, "
                    "activated_at, activated_by) VALUES ('active', ?, ?, ?, ?, 0, ?, ?, ?, ?) RETURNING id",
                    (a.text, a.sha256, int(classify.check_rules(a.text).ok), who, now, now, now, who)).fetchone()[0]
            db.journal(conn, "rules_observe", detail=json.dumps(
                {"version": version, "sha256": a.sha256, "path": a.path, "by": who}, sort_keys=True))
    return active(conn, config)


# --------------------------------------------------------------------------- versions

def get_version(conn: sqlite3.Connection, version_id: int) -> dict:
    rows = _rows(conn.execute(f"SELECT {_COLUMNS} FROM rule_versions WHERE id = ?", (version_id,)))
    if not rows:
        raise RulesError(404, "RULES_VERSION_NOT_FOUND", f"there is no rules version {version_id}", version=version_id)
    return rows[0]


def list_versions(conn: sqlite3.Connection, *, status: str | None = None, limit: int = 50) -> list[dict]:
    """Versions with their text, newest first (by last change)."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    limit = max(1, min(int(limit), 200))
    where, args = ("WHERE status = ?", (status,)) if status else ("", ())
    return _rows(conn.execute(
        f"SELECT {_COLUMNS} FROM rule_versions {where} ORDER BY updated_at DESC, id DESC LIMIT ?", (*args, limit)))


def count_proposals(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM rule_versions WHERE status = 'proposed'").fetchone()[0]


def _invalid(check: classify.RulesCheck) -> RulesError:
    shown = [{"code": e.code, "params": dict(e.params)} for e in check.errors[:INVALID_SHOWN]]
    first = str(check.errors[0]) if check.errors else "invalid rules"
    return RulesError(400, "RULES_INVALID", f"the rules do not pass validation: {first}", errors=shown)


def create_version(conn: sqlite3.Connection, text: str, *, created_by: str, title: str | None = None,
                   note: str | None = None, base_id: int | None = None) -> dict:
    """A draft (`created_by` ui; it may hold errors) or a proposal (anyone else; it must pass check_rules)."""
    text = _check_text(text)
    if created_by not in CREATORS:
        raise ValueError(f"created_by must be one of {', '.join(CREATORS)}")
    if title is not None and len(title) > MAX_TITLE or note is not None and len(note) > MAX_NOTE:
        raise ValueError(f"title is at most {MAX_TITLE} characters and note at most {MAX_NOTE}")
    check = classify.check_rules(text)
    draft = created_by == "ui"
    if not draft and not check.ok:
        raise _invalid(check)
    if base_id is not None:
        get_version(conn, base_id)
    now = _now()
    new_id = conn.execute(
        "INSERT INTO rule_versions(status, text, sha256, ok, created_by, title, note, base_id, revision, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?) RETURNING id",
        ("draft" if draft else "proposed", text, sha256_of(text), int(check.ok), created_by, title, note, base_id,
         now, now)).fetchone()[0]
    return get_version(conn, new_id)


def update_draft(conn: sqlite3.Connection, version_id: int, text: str, *, revision: int, title: str | None = None,
                 note: str | None = None) -> dict:
    """Save a draft. `revision` must be the one the caller read, else RULES_DRAFT_CHANGED (another tab saved)."""
    text = _check_text(text)
    if title is not None and len(title) > MAX_TITLE or note is not None and len(note) > MAX_NOTE:
        raise ValueError(f"title is at most {MAX_TITLE} characters and note at most {MAX_NOTE}")
    check = classify.check_rules(text)
    with db.transaction(conn):
        row = get_version(conn, version_id)
        if row["status"] != "draft":
            raise RulesError(409, "RULES_NOT_DRAFT", f"rules version {version_id} is {row['status']}, not a draft",
                             version=version_id, version_status=row["status"])
        if row["revision"] != revision:
            raise RulesError(409, "RULES_DRAFT_CHANGED", f"draft {version_id} was saved again (revision {row['revision']})",
                             version=version_id, revision=row["revision"])
        conn.execute(
            "UPDATE rule_versions SET text = ?, sha256 = ?, ok = ?, title = ?, note = ?, revision = revision + 1, "
            "updated_at = ? WHERE id = ?",
            (text, sha256_of(text), int(check.ok), row["title"] if title is None else title,
             row["note"] if note is None else note, _now(), version_id))
    return get_version(conn, version_id)


def discard(conn: sqlite3.Connection, version_id: int, *, by: str) -> dict:
    """Delete a draft; dismiss a proposal (it stays in the history). Anything else is refused."""
    with db.transaction(conn):
        row = get_version(conn, version_id)
        if row["status"] == "dismissed":
            return row
        if row["status"] == "draft":
            conn.execute("UPDATE rule_versions SET base_id = NULL WHERE base_id = ?", (version_id,))
            conn.execute("DELETE FROM rule_versions WHERE id = ?", (version_id,))
            return row
        if row["status"] != "proposed":
            raise RulesError(409, "RULES_NOT_DRAFT", f"rules version {version_id} is {row['status']} and cannot be discarded",
                             version=version_id, version_status=row["status"])
        conn.execute("UPDATE rule_versions SET status = 'dismissed', updated_at = ? WHERE id = ?", (_now(), version_id))
        db.journal(conn, "rules_dismiss", detail=json.dumps({"version": version_id, "by": by}, sort_keys=True))
    return get_version(conn, version_id)


# --------------------------------------------------------------------------- activating

def _write_atomic(path: Path, text: str, *, create_parent: bool) -> None:
    """Write `text` exactly to `path`: a temp file beside it, fsync, rename. A symbolic link is written through, the
    existing file's mode is kept, a new file is 0600. Anything that fails is RULES_FILE_UNWRITABLE."""
    real = Path(os.path.realpath(path))
    try:
        if create_parent:
            real.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(real.stat().st_mode) if real.exists() else 0o600
        fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, real)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            dir_fd = os.open(real.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        raise RulesError(409, "RULES_FILE_UNWRITABLE", f"cannot write {path}: {exc.strerror or exc}", path=str(path)) from exc


def require_activatable(conn: sqlite3.Connection, config: Config, version_id: int) -> dict:
    """The version, if it may be activated; else the refusal `activate` gives, in its order: RULES_VERSION_NOT_FOUND,
    RULES_NOT_ACTIVATABLE (dismissed), RULES_INVALID, RULES_NO_FILE (a config built in memory)."""
    row = get_version(conn, version_id)
    if row["status"] not in ("draft", "proposed", "active", "retired"):
        raise RulesError(409, "RULES_NOT_ACTIVATABLE", f"rules version {version_id} is {row['status']} and cannot be activated",
                         version=version_id, version_status=row["status"])
    check = classify.check_rules(row["text"])
    if not row["ok"] or not check.ok:
        raise _invalid(check)
    if config.rules_file is None:
        raise RulesError(409, "RULES_NO_FILE", "there is no config file, so there is nowhere to write the rules")
    return row


def activate(conn: sqlite3.Connection, config: Config, version_id: int, *, base_sha256: str,
             activated_by: str) -> ActiveRules:
    """Make a version's exact text the active file, if the file still holds the text the person reviewed against.

    Refusals in this order: those of `require_activatable`, RULES_ACTIVE_CHANGED (the file's SHA-256 is not
    `base_sha256`), RULES_FILE_UNWRITABLE. The previous text is already in the history (it was observed first), so
    undo is activating the previous version.
    """
    row = require_activatable(conn, config, version_id)
    file = config.rules_file
    current = observe(conn, config)
    if current.sha256 != base_sha256:
        raise RulesError(409, "RULES_ACTIVE_CHANGED", "the active rules changed since you reviewed them",
                         active_sha256=current.sha256, active_version=current.version_id)
    _write_atomic(file, row["text"], create_parent=config.rules_owned)
    now = _now()
    with db.transaction(conn):
        conn.execute("UPDATE rule_versions SET status = 'retired', updated_at = ? WHERE status = 'active' AND id <> ?",
                     (now, version_id))
        conn.execute("UPDATE rule_versions SET status = 'active', activated_at = ?, activated_by = ?, updated_at = ? "
                     "WHERE id = ?", (now, activated_by, now, version_id))
        db.journal(conn, "rules_activate", detail=json.dumps(
            {"version": version_id, "sha256": row["sha256"], "path": str(file), "by": activated_by}, sort_keys=True))
    return active(conn, config)


# --------------------------------------------------------------------------- comparing

def diff(old_text: str, new_text: str) -> dict:
    """A line diff with 3 lines of context: {"hunks": [{"old_start", "new_start", "lines": [{"op", "text"}]}],
    "added", "removed"}. `op` is " " (context), "+" or "-"."""
    old, new = old_text.split("\n"), new_text.split("\n")
    for lines in (old, new):
        if lines and lines[-1] == "":              # the final newline is not a line
            lines.pop()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    hunks, added, removed = [], 0, 0
    for group in matcher.get_grouped_opcodes(3):
        lines = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines.extend({"op": " ", "text": t} for t in old[i1:i2])
                continue
            if tag in ("replace", "delete"):
                lines.extend({"op": "-", "text": t} for t in old[i1:i2])
                removed += i2 - i1
            if tag in ("replace", "insert"):
                lines.extend({"op": "+", "text": t} for t in new[j1:j2])
                added += j2 - j1
        hunks.append({"old_start": group[0][1] + 1, "new_start": group[0][3] + 1, "lines": lines})
    return {"hunks": hunks, "added": added, "removed": removed}


def rule_changes(old: classify.RuleSet, new: classify.RuleSet) -> list[dict]:
    """What changed from `old` to `new`, by rule name: added, removed, changed (the rule's own keys) or moved
    (same rule, different place among the rules both have). New rules' order first, then the removed ones."""
    before = {r.name: r for r in old.rules}
    after = {r.name: r for r in new.rules}
    common_old = [r.name for r in old.rules if r.name in after]
    common_new = [r.name for r in new.rules if r.name in before]
    stay: set[str] = set()
    for block in difflib.SequenceMatcher(None, common_old, common_new, autojunk=False).get_matching_blocks():
        stay.update(common_old[block.a:block.a + block.size])
    out = []
    for r in new.rules:
        if r.name not in before:
            out.append({"name": r.name, "change": "added"})
        elif before[r.name] != r:
            out.append({"name": r.name, "change": "changed"})
        elif r.name not in stay:
            out.append({"name": r.name, "change": "moved"})
    out.extend({"name": r.name, "change": "removed"} for r in old.rules if r.name not in after)
    return out
