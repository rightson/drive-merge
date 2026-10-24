"""One facade over the core modules, shared by the CLI, the REST API and the MCP server.

Everything returns JSON-serialisable values. Methods are grouped by trust level:

* read        — catalog queries, compare/coverage/explore and workspace trees; safe for anyone with access to the service
* maintain    — scans, syncs, hash bridging and directory statistics; read-only on the clouds
* propose     — build and simulate plans, stage workspace drafts; writes only to the local catalog
* human-only  — approve / execute / restore / purge, and the connection facade (sign-in, writes, disconnect).
                The MCP server and the REST API never call these; the CLI gates them behind a TTY and
                the browser workspace behind its own session (ui_api).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import sqlite3
import tempfile
from pathlib import Path

from . import (
    builders, classify, compare, coverage, db, dirstats, executor, explore, identity, plans, quarantine,
    rulepreview, rulestore, search, simulate, suggest, workspace,
)
from .config import DEFAULT_CONFIG_PATH, Config, load_config, sync_remotes
from .models import Plan
from .connections import make_rclone
from .rclone import Rclone
from .scanner import scan_remote

PLAN_KINDS = ("dedupe", "consolidate", "reorganize", "manual", "release")
# draft_id and draft_version are written only by workspace.preview, rules_ref only by propose_plan (the rules a plan
# was built with): a proposer's params must never be able to claim either.
RESERVED_PARAMS = ("draft_id", "draft_version", "rules_ref")
RULES_KINDS = ("consolidate", "reorganize")
MAX_COMPARE_FILES = 100000
MAX_COVERAGE_BARS = 64
MAX_GROUP_PAGE = 200       # dedupe groups an estimate lists per page
SAMPLE_SKIPPED = 200       # skipped files a build result hands back
# The first 8 characters of a digest ARE the approval code, and `snapshot` is what the digest binds to.
SECRET_KEYS = frozenset({"digest", "approval_code", "snapshot"})


def scrub_secrets(obj):
    """`obj` without approval secrets at any depth, so a caller holding only the API token never learns the
    code a person is meant to read off their own screen."""
    if isinstance(obj, dict):
        return {k: scrub_secrets(v) for k, v in obj.items() if k not in SECRET_KEYS}
    if isinstance(obj, (list, tuple)):
        return [scrub_secrets(v) for v in obj]
    return obj


def _plain(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [_plain(o) for o in obj]
    return obj


class ServiceError(ValueError):
    pass


def draft_of(plan: Plan) -> int | None:
    """The draft this plan is the preview of, or None.

    Only `workspace.preview` makes a plan of kind 'workspace', created by 'workspace'. A `draft_id` in the
    params of any other plan is a claim made by whoever proposed it, so it links nothing: otherwise running
    that plan would close someone else's draft.
    """
    if plan.kind != "workspace" or plan.created_by != "workspace":
        return None
    return plan.params.get("draft_id")


class Service:
    def __init__(self, config: Config, conn: sqlite3.Connection | None = None, rclone: Rclone | None = None,
                 config_path: str | Path | None = None):
        self.config = config
        self.config_path = config_path if config_path is not None else config.config_path
        self.conn = conn if conn is not None else db.connect(config.db_path)
        sync_remotes(self.conn, config)
        self.rclone = rclone or make_rclone(config)
        self._connections_seen = self._connections_mtime()
        self.previewer = rulepreview.Previewer()     # the rules preview's caches and its one-at-a-time machinery
        # Progress callback a background job hangs on its own service: restore and purge report to it when
        # their caller passes none (so the job can call purge(plan_id, older_than_days=...) as a person would).
        self.default_progress = None

    def _connections_mtime(self) -> int | None:
        """mtime of connections.toml next to the config file; None without a config file or without the file."""
        if not self.config_path:
            return None
        try:
            return (Path(self.config_path).expanduser().parent / "connections.toml").stat().st_mtime_ns
        except OSError:
            return None

    def reload_config(self) -> Config:
        """Re-read the merged config (config.toml plus connections.toml) so connections added while the service
        runs become visible: the remotes table is re-synced and the Rclone rebuilt (its config file may have
        changed). `self.config` is updated in place, so every holder of it sees the new remotes. A config file
        that does not exist (tests, a service built from an in-memory Config) leaves everything as it is."""
        path = Path(self.config_path).expanduser() if self.config_path else DEFAULT_CONFIG_PATH
        if path.exists():
            fresh = load_config(path)
            for f in dataclasses.fields(Config):
                setattr(self.config, f.name, getattr(fresh, f.name))
        sync_remotes(self.conn, self.config)
        self.rclone = make_rclone(self.config)
        self._connections_seen = self._connections_mtime()
        return self.config

    # ---------------------------------------------------------------- read

    def data_age(self, remote: str) -> dict:
        """When `remote` was last listed completely: {remote, finished_at, age_seconds} (None when never)."""
        row = self.conn.execute(
            "SELECT MAX(s.finished_at) FROM scans s JOIN remotes r ON r.id = s.remote_id "
            "WHERE r.name = ? AND s.status = 'complete'", (remote,)).fetchone()
        finished = row[0] if row else None
        age = None
        if finished:
            try:
                then = datetime.fromisoformat(finished.replace("Z", "+00:00"))
                if then.tzinfo is None:
                    then = then.replace(tzinfo=timezone.utc)
                age = max(0.0, (datetime.now(timezone.utc) - then).total_seconds())
            except ValueError:
                pass
        return {"remote": remote, "finished_at": finished, "age_seconds": age}

    def data_ages(self) -> dict[str, dict]:
        return {r.name: self.data_age(r.name) for r in self.config.remotes}

    def release_report(self, remote: str, path_prefix: str | None = None, keep_on: list[str] | None = None) -> dict:
        """How much of `remote` provably exists on the other clouds (release.py). Read-only."""
        from . import release
        return release.release_report(self.conn, remote, path_prefix=path_prefix or None, keep_on=keep_on or None)

    def stats(self) -> dict:
        return search.stats(self.conn)

    def search(self, q: str | None = None, **filters) -> list[dict]:
        return search.search(self.conn, q, **filters)

    def entry(self, entry_id: int) -> dict | None:
        return search.get_entry(self.conn, entry_id)

    def locations(self, entry_id: int) -> list[dict]:
        return _plain(identity.locations(self.conn, entry_id))

    def duplicates(self, **kw) -> list[dict]:
        return _plain(identity.duplicate_groups(self.conn, **kw))

    def profiles(self, *, rules_text: str | None = None, **kw) -> list[dict]:
        return _plain(classify.folder_profiles(self.conn, ruleset=self.ruleset(rules_text), **kw))

    def changes(self, cursor: str | None = None, limit: int = 1000) -> dict:
        rows, nxt = search.changes(self.conn, cursor, limit)
        return {"rows": rows, "cursor": nxt}

    def compare(self, left: tuple[str, str], right: tuple[str, str], *, max_files: int = 20000) -> dict:
        """Align two folders `(remote, path)` and classify every file (compare.py). Read-only."""
        if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= MAX_COMPARE_FILES:
            raise compare.CompareError("BAD_QUERY", f"max_files must be between 1 and {MAX_COMPARE_FILES}",
                                       param="max_files")
        return compare.compare_folders(self.conn, tuple(left), tuple(right), max_files=max_files)

    def coverage(self, category: str | None = None, *, max_bars: int = 24) -> dict:
        """How the clouds overlap (coverage.py). `max_bars` is 1 to 64."""
        if isinstance(max_bars, bool) or not isinstance(max_bars, int) or not 1 <= max_bars <= MAX_COVERAGE_BARS:
            raise compare.CompareError("BAD_QUERY", f"max_bars must be between 1 and {MAX_COVERAGE_BARS}",
                                       param="max_bars")
        return coverage.coverage(self.conn, category=category or None, max_bars=max_bars)

    def explore(self, q: str | None = None, **params) -> dict:
        """Search results grouped by content with one presence column per cloud (explore.py)."""
        return explore.explore(self.conn, q, **params)

    def ruleset(self, rules_text: str | None = None, *, rules_version: int | None = None) -> classify.RuleSet:
        """The rules to use: version `rules_version` (any status), else `rules_text`, else the active rules
        (rulestore.active: the rules file, else the packaged defaults). Both given is RULES_BOTH. Invalid rules
        raise their RuleError, as ever; a configured file that is missing or unreadable raises RulesError."""
        return self._resolve_rules(rules_text, rules_version)[0]

    def _resolve_rules(self, rules_text: str | None, rules_version: int | None) -> tuple[classify.RuleSet, str, dict]:
        """(rules, the exact text, rules ref) for the same inputs as `ruleset`."""
        if rules_text is not None and rules_version is not None:
            raise rulestore.RulesError(400, "RULES_BOTH", "pass either rules or rules_version, not both")
        if rules_version is not None:
            row = rulestore.get_version(self.conn, rules_version)
            ref = {"source": "version", "version": row["id"], "sha256": row["sha256"], "path": None}
            return classify.load_rules(row["text"]), row["text"], ref
        if rules_text is not None:
            ref = {"source": "inline", "version": None, "sha256": rulestore.sha256_of(rules_text), "path": None}
            return classify.load_rules(rules_text), rules_text, ref
        a = rulestore.active(self.conn, self.config)
        if a.error is not None and a.error["code"] in ("RULES_FILE_MISSING", "RULES_FILE_UNREADABLE"):
            path = a.error["params"]["path"]
            what = "does not exist" if a.error["code"] == "RULES_FILE_MISSING" else "cannot be read"
            raise rulestore.RulesError(409, a.error["code"], f"the rules file {path} {what}; "
                                       "fix [rules] path in config.toml or restore the file", path=path)
        rs = classify.load_rules(a.text)
        return rs, a.text, {"source": a.source, "version": a.version_id, "sha256": a.sha256, "path": a.path}

    def validate_rules(self, rules_text: str) -> dict:
        """{"ok": true, "rules": [names], "errors": [], "warnings": [...]} or {"ok": false, "error": first message,
        "code", "params", "errors": [every error as {code, message, params}], "warnings"}."""
        check = classify.check_rules(rules_text)
        warnings = self._warnings(check)
        if check.ok:
            return {"ok": True, "rules": [r.name for r in check.ruleset.rules], "errors": [], "warnings": warnings}
        first = check.errors[0]
        return {"ok": False, "error": str(first), "code": first.code, "params": dict(first.params),
                "errors": [classify.error_dict(e) for e in check.errors], "warnings": warnings}

    def preview_layout(self, *, rules_text: str | None = None, remotes: list[str] | None = None,
                       target_root: str = "", path_prefix: str | None = None, limit: int = 200,
                       rules_version: int | None = None) -> dict:
        rs = self.ruleset(rules_text, rules_version=rules_version)
        ids = [db.remote_id(self.conn, r) for r in remotes] if remotes else None
        placements = classify.plan_layout(self.conn, rs, remote_ids=ids, target_root=target_root,
                                          path_prefix=path_prefix)
        by_rule: dict[str, int] = {}
        for p in placements:
            by_rule[p.rule] = by_rule.get(p.rule, 0) + 1
        return {"total": len(placements), "by_rule": by_rule, "sample": _plain(placements[:limit])}

    # ---------------------------------------------------------- layout rules (docs/RULES.md)
    # Reads and proposals only: a draft, a proposal, a check, a preview. Making rules active is human-only
    # (`activate_rules` below).

    def _rules_of(self, row_or_text: str, *, ok: bool = True) -> classify.RuleSet | None:
        try:
            return classify.load_rules(row_or_text) if ok else None
        except classify.RuleError:
            return None

    def _warnings(self, check: classify.RulesCheck) -> list[dict]:
        """The check's warnings plus RULE_REMOTE_UNKNOWN for a `remote` that is not a cloud of this catalog."""
        known = {r[0] for r in self.conn.execute("SELECT name FROM remotes")}
        out = list(check.warnings)
        for item in check.outline:
            for value in item["match"]["remote"]:
                if value not in known:
                    out.append({"code": "RULE_REMOTE_UNKNOWN",
                                "message": f"rule {item['name']!r}: remote {value!r} is not a cloud of this catalog",
                                "params": {"line": item["line"], "rule_index": item["index"], "rule": item["name"],
                                           "key": "remote", "value": classify._cap(value)}})
        out.sort(key=lambda w: (w["params"]["line"] is None, w["params"]["line"] or 0))
        return out

    def check_rules(self, text: str, against: int | None = None) -> dict:
        """Validate rules text without touching the catalog's files: every error and warning with codes and line
        numbers, the outline, the resolved layout, and the text diff and rule changes against version `against`
        (default the rules in force)."""
        check = classify.check_rules(text)
        if against is not None:
            base_text = rulestore.get_version(self.conn, against)["text"]
        else:
            base_text = rulestore.active(self.conn, self.config).text
        base = self._rules_of(base_text)
        return {"ok": check.ok, "errors": [classify.error_dict(e) for e in check.errors],
                "warnings": self._warnings(check), "outline": check.outline, "layout": check.layout,
                "diff": rulestore.diff(base_text, text),
                "changes": rulestore.rule_changes(base, check.ruleset) if base and check.ruleset else None,
                "sha256": rulestore.sha256_of(text)}

    def transform_rules(self, text: str, op: dict) -> dict:
        """Move, remove or insert a whole rule in `text` (classify.transform_rules); {"text", "check"}."""
        new = classify.transform_rules(text, op)
        return {"text": new, "check": self.check_rules(new)}

    def rules_default(self) -> dict:
        text = classify.default_rules_text()
        return {"text": text, "sha256": rulestore.sha256_of(text)}

    def _summary(self, row: dict, active_rs: classify.RuleSet | None) -> dict:
        rs = self._rules_of(row["text"], ok=row["ok"])
        out = {k: row[k] for k in ("id", "status", "title", "created_by", "created_at", "updated_at", "activated_at",
                                   "activated_by", "sha256", "ok", "base_id", "revision")}
        out["rules"] = len(rs.rules) if rs else None
        out["changes"] = rulestore.rule_changes(active_rs, rs) if rs and active_rs else None
        return out

    def rule_versions(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """Summaries of the rule versions, newest first: {id, status, title, created_by, created_at, updated_at,
        activated_at, activated_by, sha256, ok, base_id, revision, rules (count), changes (against the active rules)}."""
        active_rs = self._rules_of(rulestore.active(self.conn, self.config).text)
        return [self._summary(row, active_rs) for row in rulestore.list_versions(self.conn, status=status, limit=limit)]

    def rule_version(self, version_id: int) -> dict:
        """One version: its summary, `note`, `text`, `check` (errors, warnings, outline, layout) and `diff` against
        the rules in force."""
        row = rulestore.get_version(self.conn, version_id)
        active = rulestore.active(self.conn, self.config)
        out = self._summary(row, self._rules_of(active.text))
        check = self.check_rules(row["text"])
        out.update(note=row["note"], text=row["text"], diff=check["diff"],
                   check={k: check[k] for k in ("ok", "errors", "warnings", "outline", "layout")})
        return out

    def rules_state(self) -> dict:
        """What the Rules view shows: the active rules (the file is observed first, so an edit made outside
        drive-merge becomes a version by `file`), the proposals waiting, the drafts and the history."""
        active = rulestore.observe(self.conn, self.config)
        check = classify.check_rules(active.text)
        row = rulestore.get_version(self.conn, active.version_id) if active.version_id is not None else None
        active_rs = check.ruleset
        info = active.as_dict()
        info.update(
            write_path=str(self.config.rules_file) if self.config.rules_file else None,
            status=row["status"] if row else None, created_by=row["created_by"] if row else None,
            since=row["activated_at"] if row else None, by=row["activated_by"] if row else None,
            changed_outside=bool(row and row["status"] == "active" and row["created_by"] == "file"),
            rules=len(check.ruleset.rules) if check.ruleset else None,
            check={"ok": check.ok, "error_count": len(check.errors), "warning_count": len(check.warnings),
                   "errors": [classify.error_dict(e) for e in check.errors[:10]], "warnings": self._warnings(check)})
        history = [r for r in rulestore.list_versions(self.conn, limit=200) if r["status"] in ("active", "retired")]
        return {"active": info,
                "proposals": [self._summary(r, active_rs) for r in rulestore.list_versions(self.conn, status="proposed")],
                "drafts": [self._summary(r, active_rs) for r in rulestore.list_versions(self.conn, status="draft")],
                "history": [self._summary(r, active_rs) for r in history[:50]]}

    def rules_brief(self) -> dict:
        """The one-line state /v1/ui/config carries: {source, path, owned, active_version, proposals}. Reads only."""
        a = rulestore.active(self.conn, self.config)
        return {"source": a.source, "path": a.path, "owned": a.owned, "active_version": a.version_id,
                "proposals": rulestore.count_proposals(self.conn)}

    def rules_overview(self) -> dict:
        """The rules in force and the proposals waiting, for a reader that must not write (MCP `get_rules`):
        {source, path, version, sha256, text, error, proposals: [{id, title, created_by, created_at}]}. Unlike
        `rules_state` it does not record an outside edit of the file."""
        a = rulestore.active(self.conn, self.config)
        return {"source": a.source, "path": a.path, "version": a.version_id, "sha256": a.sha256, "text": a.text,
                "error": a.error,
                "proposals": [{k: r[k] for k in ("id", "title", "created_by", "created_at")}
                              for r in rulestore.list_versions(self.conn, status="proposed", limit=50)]}

    def save_rule_version(self, text: str, *, created_by: str, title: str | None = None, note: str | None = None,
                          base_id: int | None = None) -> dict:
        """Store a draft (created_by "ui") or a proposal (anyone else; it must be valid). Never active by itself."""
        row = rulestore.create_version(self.conn, text, created_by=created_by, title=title, note=note, base_id=base_id)
        return self.rule_version(row["id"])

    def update_rule_draft(self, version_id: int, text: str, *, revision: int, title: str | None = None,
                          note: str | None = None) -> dict:
        rulestore.update_draft(self.conn, version_id, text, revision=revision, title=title, note=note)
        return self.rule_version(version_id)

    def discard_rule_version(self, version_id: int, *, by: str) -> dict:
        """Delete a draft or dismiss a proposal. Returns {"ok": true, "version", "status"}."""
        row = rulestore.discard(self.conn, version_id, by=by)
        return {"ok": True, "version": version_id, "status": "deleted" if row["status"] == "draft" else row["status"]}

    def _rules_text_for(self, text: str | None, version_id: int | None, conn: sqlite3.Connection) -> str:
        if text is not None and version_id is not None:
            raise rulestore.RulesError(400, "RULES_BOTH", "pass either text or version_id, not both")
        if version_id is not None:
            return rulestore.get_version(conn, version_id)["text"]
        if text is not None:
            return text
        return rulestore.active(conn, self.config).text

    def preview_rules(self, *, text: str | None = None, version_id: int | None = None, remotes: list[str] | None = None,
                      target_root: str = "", path_prefix: str | None = None, mode: str = "auto", limit: int = 100,
                      filter: str = "changed", conn: sqlite3.Connection | None = None) -> dict:
        """Where the files of a scope would go under the given rules (default the active rules) and what changes
        against the active rules; see rulepreview. `conn` is a read-only connection to run on (default the
        service's own). Writes nothing."""
        conn = conn or self.conn
        edited = self._rules_text_for(text, version_id, conn)
        active = rulestore.active(conn, self.config)
        return self.previewer.preview(conn, edited_text=edited, active_text=None if active.error and active.error["code"] in (
            "RULES_FILE_MISSING", "RULES_FILE_UNREADABLE") else active.text, remotes=remotes or None,
            target_root=target_root, path_prefix=path_prefix or None, mode=mode, limit=limit, filter=filter)

    def preview_page(self, preview_id: str, *, filter: str = "changed", q: str | None = None, offset: int = 0,
                     limit: int = 100) -> dict:
        return self.previewer.page(preview_id, filter=filter, q=q, offset=offset, limit=limit)

    def explain_layout(self, *, remote: str, path: str, text: str | None = None, version_id: int | None = None,
                       target_root: str = "", conn: sqlite3.Connection | None = None) -> dict:
        """Why a file lands where it does under the given rules and under the active rules: {"edited", "active"}."""
        conn = conn or self.conn
        edited = classify.load_rules(self._rules_text_for(text, version_id, conn))
        active = self._rules_of(rulestore.active(conn, self.config).text)
        rid = db.remote_id(conn, remote)
        return {"edited": classify.explain(conn, edited, rid, path, target_root=target_root),
                "active": classify.explain(conn, active, rid, path, target_root=target_root) if active else None}

    # ------------------------------------------------------------ maintain

    def scan(self, remote: str, *, root: str = "", progress=None, force: bool = False) -> dict:
        res = _plain(scan_remote(self.conn, self.rclone, self.config.remote(remote), root=root,
                                 progress=progress, force=force))
        # Folder counts in the workspace come from dir_stats; keep them in step with the catalog.
        self.refresh_dir_stats([remote])
        return res

    def sync(self, remotes: list[str] | None = None, progress=None) -> dict:
        """List the remotes (default all) in parallel, then rebuild identity and folder stats once (syncall.py).
        Uses its own catalog connections; this service's connection sees the result afterwards."""
        from . import syncall
        res = syncall.sync_all(self.config, remotes=remotes, progress=progress)
        res["data_age"] = {n: self.data_age(n) for n in res["remotes"]}
        return res

    def rebuild_identity(self) -> dict:
        res = _plain(identity.rebuild_groups(self.conn))
        self.refresh_dir_stats()  # dup_files depends on the content groups just rebuilt
        return res

    def refresh_dir_stats(self, remotes: list[str] | None = None) -> dict:
        ids = [db.remote_id(self.conn, r) for r in remotes] if remotes else None
        return dirstats.refresh(self.conn, remote_ids=ids)

    def bridge_candidates(self, *, max_bytes: int | None = None, limit: int = 100) -> dict:
        """How many files `bridge` would download with these limits, and their bytes. Read-only."""
        found = identity.bridge_candidates(self.conn, max_bytes=max_bytes, limit=limit)
        return {"count": len(found), "bytes": sum(c.size for c in found)}

    def bridge(self, *, max_bytes: int | None = None, limit: int = 100, progress=None, started=None,
               entry_ids: list[int] | None = None) -> dict:
        """Download each candidate once (read remote only), hash it, record the bridge, regroup.
        `progress(done, failed)` runs after each file; `started(total)` once, with the number of candidates.
        With `entry_ids`, exactly those entries are downloaded (each once; `max_bytes` and `limit` do not apply)
        instead of the automatic candidates; one that is not a live file is reported in `failed`."""
        done, failed = [], []
        with tempfile.TemporaryDirectory(prefix="drivemerge-bridge-") as tmp:
            if entry_ids is not None:
                ids = list(dict.fromkeys(int(i) for i in entry_ids))
            else:
                ids = [c.entry_id for c in identity.bridge_candidates(self.conn, max_bytes=max_bytes, limit=limit)]
            if started:
                started(len(ids))
            for entry_id in ids:
                try:
                    identity.bridge_entry(self.conn, self.rclone, entry_id, tmp)
                    done.append(entry_id)
                except Exception as e:  # one bad file must not stop the batch
                    failed.append({"entry_id": entry_id, "error": str(e)})
                if progress:
                    progress(len(done), len(failed))
        stats = identity.rebuild_groups(self.conn) if done else None
        if done:
            self.refresh_dir_stats()
        return {"bridged": len(done), "failed": failed, "groups": _plain(stats)}

    # ------------------------------------------------------------- propose

    def _build(self, kind: str, params: dict) -> tuple[builders.BuildResult, dict, str | None, dict | None]:
        """Run the builder of `kind`: (result, params without `rules`/`rules_version`, the rules text or None, the
        rules ref or None). For consolidate and reorganize the text and the ref are always those of the rules used.
        Persists nothing."""
        if kind not in PLAN_KINDS:
            raise ServiceError(f"unknown plan kind {kind!r}; expected one of {PLAN_KINDS}")
        claimed = [k for k in RESERVED_PARAMS if k in params]
        if claimed:
            if "rules_ref" in claimed:
                raise ServiceError("params rules_ref is reserved: the service records which rules built a plan; "
                                   "pass rules or rules_version to choose them")
            raise ServiceError(f"params {', '.join(claimed)} are reserved for workspace previews; "
                               "stage moves into a draft and preview it instead")
        params = dict(params)
        rules_text = params.pop("rules", None)
        rules_version = params.pop("rules_version", None) if kind in RULES_KINDS else None
        ref = None
        # Quarantined files are deleted content, not live copies; builders must know where they are.
        quarantine = {"quarantine_dir": self.config.safety.quarantine_dir}
        if kind in RULES_KINDS:
            ruleset, rules_text, ref = self._resolve_rules(rules_text, rules_version)
        if kind == "dedupe":
            result = builders.build_dedupe(self.conn, **{**quarantine, **params})
        elif kind == "consolidate":
            result = builders.build_consolidate(self.conn, ruleset=ruleset, **{**quarantine, **params})
        elif kind == "reorganize":
            result = builders.build_reorganize(self.conn, ruleset=ruleset, **{**quarantine, **params})
        elif kind == "release":
            from . import release
            result = release.build_release(self.conn, **{**quarantine, **params})
        else:
            result = builders.build_manual(self.conn, params.get("ops", []))
        return result, params, rules_text, ref

    def propose_plan(self, kind: str, params: dict, *, created_by: str, title: str | None = None) -> dict:
        """Build a plan, persist it as a draft and simulate it. Never touches a cloud.

        Consolidate and reorganize plans record the exact rules text they were built with (`params.rules`) and where
        it came from (`params.rules_ref`: source file, default, version or inline, version id, SHA-256, path), so
        a plan stays reproducible after the rules file changes. The active file is observed first, so an edit made
        outside drive-merge is in the history before a plan is built with it."""
        if kind in RULES_KINDS:
            rulestore.observe(self.conn, self.config)
        result, params, rules_text, ref = self._build(kind, params)
        if not result.ops:
            return {"plan_id": None, "ops": 0, "skipped": result.skipped[:SAMPLE_SKIPPED], "stats": result.stats,
                    "message": "nothing to do", "code": "NOTHING_TO_DO"}
        if rules_text is not None:
            params["rules"] = rules_text
        if ref is not None:
            params["rules_ref"] = ref
        plan_id = plans.create_plan(self.conn, title or f"{kind} plan", kind, result.ops,
                                    created_by=created_by, params=params)
        report = self.simulate(plan_id)
        return {"plan_id": plan_id, "ops": len(result.ops), "skipped": result.skipped[:SAMPLE_SKIPPED],
                "stats": result.stats, "simulation": report}

    def estimate_plan(self, kind: str, params: dict, *, sample: int = 50) -> dict:
        """What `propose_plan` would build, without persisting anything (no plan, no simulation, no catalog write).

        `params` are handled exactly as in propose_plan. For dedupe, `group_offset` / `group_limit` (default 0 / 50)
        page the `groups` list and are not passed to the builder. Each group is {key, size, reclaim, skipped,
        copies: [{entry_id, remote, path, mtime, keep, remove, usable, why_not}]}, in the builder's order.
        """
        params = dict(params)
        window = None
        if kind == "dedupe":
            offset, limit = (params.pop("group_offset", 0), params.pop("group_limit", 50))
            for name, value, low in (("group_offset", offset, 0), ("group_limit", limit, 1)):
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= 10 ** 9:
                    raise ServiceError(f"{name} must be an integer of at least {low}")
            window = (offset, min(limit, MAX_GROUP_PAGE))
            params["group_window"] = window
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
            raise ServiceError("sample must be a non-negative integer")
        result, params, _, ref = self._build(kind, params)
        names = {r[0]: r[1] for r in self.conn.execute("SELECT id, name FROM remotes")}

        def place(remote_id: int | None, path: str | None) -> dict | None:
            return None if remote_id is None else {"remote": names.get(remote_id, str(remote_id)), "path": path}

        shown = []
        for op in result.ops[:sample]:
            item = {"op": op.op, "src": place(op.src_remote_id, op.src_path), "size": op.expect_size,
                    "reason": op.reason}
            if op.dst_remote_id is not None:
                item["dst"] = place(op.dst_remote_id, op.dst_path)
            if op.witness_remote_id is not None:
                item["witness"] = place(op.witness_remote_id, op.witness_path)
            shown.append(item)
        out = {"kind": kind, "ops": len(result.ops), "stats": result.stats,
               "skipped": result.skipped[:SAMPLE_SKIPPED], "skipped_total": len(result.skipped), "sample": shown,
               "nothing_to_do": not result.ops}
        if kind == "dedupe":
            out.update(groups=result.groups, group_offset=window[0], group_limit=window[1],
                       groups_total=result.stats["groups_examined"])
        if ref is not None:
            out["rules_ref"] = ref
        return out

    def policy(self) -> simulate.SafetyPolicy:
        """The simulator's safety policy as configured; one place so every caller simulates alike."""
        s = self.config.safety
        return simulate.SafetyPolicy(allow_probable_delete=s.allow_probable_delete, min_copies=s.min_copies,
                                     quarantine_dir=s.quarantine_dir)

    def simulate(self, plan_id: int) -> dict:
        report = simulate.simulate_plan(self.conn, plan_id, policy=self.policy())
        d = dataclasses.asdict(report)
        d["approval_code"] = plans.short_code(report.digest) if report.ok else None
        return d

    def plan(self, plan_id: int, *, ops_limit: int = 200) -> dict:
        p = plans.get_plan(self.conn, plan_id)
        d = dataclasses.asdict(p)
        d["ops_total"] = len(p.ops)
        d["ops"] = d["ops"][:ops_limit]
        sim = plans.latest_simulation(self.conn, plan_id)
        d["simulation"] = dataclasses.asdict(sim) if sim else None
        return d

    def plans(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        return _plain(plans.list_plans(self.conn, status=status, limit=limit))

    def plan_summaries(self, status: str | None = None, limit: int = 100) -> list[dict]:
        """One row per plan for a list view, newest first: the plan's header, its op count, whether its last
        simulation passed (`ok`, None when it never ran) with the number of violations (`problems`), its
        scrubbed `params` (without the `rules` text), the `draft_id` it previews and `outdated`: the catalog was scanned again since that
        simulation, so it has to be simulated again before it can be approved."""
        rows = plans.list_plans(self.conn, status=status or None, limit=limit)
        counts = {r[0]: r[1] for r in self.conn.execute(
            f"SELECT plan_id, COUNT(*) FROM plan_ops WHERE plan_id IN ({','.join('?' * len(rows))}) GROUP BY plan_id",
            [p.id for p in rows])} if rows else {}
        current = db.snapshot(self.conn)
        out = []
        for p in rows:
            sim = plans.latest_simulation(self.conn, p.id)
            out.append({
                "id": p.id, "kind": p.kind, "title": p.title, "status": p.status, "created_by": p.created_by,
                "created_at": p.created_at, "simulated_at": p.simulated_at, "approved_at": p.approved_at,
                "finished_at": p.finished_at, "ops": counts.get(p.id, 0),
                "ok": None if sim is None else bool(sim.ok),
                "problems": 0 if sim is None else sim.violations_total,
                # The rules text can be tens of KB per plan; a list carries `rules_ref` (where it came from) and the
                # full plan (`plan`, the review) carries the text.
                "params": scrub_secrets({k: v for k, v in p.params.items() if k != "rules"}), "draft_id": draft_of(p),
                "outdated": p.status in ("draft", "simulated", "approved") and p.snapshot is not None
                            and p.snapshot != current,
            })
        return out

    def cancel(self, plan_id: int) -> None:
        plans.cancel_plan(self.conn, plan_id)

    # ----------------------------------------------------- workspace drafts
    # A draft is a list of staged moves over a virtual namespace; staging never touches a cloud.
    # `preview_draft` turns it into an ordinary plan and simulates it, so approval and execution
    # stay in the human-only section below.

    def tree_roots(self, *, draft_id: int | None = None) -> list[dict]:
        return workspace.roots(self.conn, draft_id=draft_id)

    def list_dir(self, remote: str, path: str = "", **kw) -> dict:
        return workspace.list_dir(self.conn, remote, path, **kw)

    def create_draft(self, title: str) -> dict:
        return workspace.create_draft(self.conn, title)

    def list_drafts(self, *, status: str | None = "open") -> list[dict]:
        return workspace.list_drafts(self.conn, status=status)

    def get_draft(self, draft_id: int) -> dict:
        return workspace.get_draft(self.conn, draft_id)

    def discard_draft(self, draft_id: int) -> None:
        workspace.discard_draft(self.conn, draft_id)

    def stage_action(self, draft_id: int, action: dict, *, created_by: str) -> dict:
        return workspace.add_action(self.conn, draft_id, action, created_by=created_by)

    def undo_action(self, draft_id: int) -> dict:
        return workspace.undo(self.conn, draft_id)

    def redo_action(self, draft_id: int) -> dict:
        return workspace.redo(self.conn, draft_id)

    def remove_action(self, draft_id: int, action_id: int) -> dict:
        return workspace.remove_action(self.conn, draft_id, action_id)

    def preview_draft(self, draft_id: int) -> dict:
        """Build, persist and simulate the draft's plan. The result carries the simulation's digest:
        callers that talk to anyone but the human at the browser must drop it (and `approval_code`)."""
        return workspace.preview(self.conn, draft_id, policy=self.policy())

    def suggestions(self, kind: str | None = None, *, remote: str | None = None, limit: int = 20) -> list[dict]:
        """Related and duplicate folder suggestions, ready to stage as draft actions."""
        if kind not in (None, "related", "duplicates"):
            raise ServiceError("kind must be 'related' or 'duplicates'")
        found: list[dict] = []
        if kind in (None, "related"):
            found += suggest.related_folders(self.conn, remote=remote, limit=limit)
        if kind in (None, "duplicates"):
            found += suggest.duplicate_folders(self.conn, remote=remote, limit=limit)
        return found

    # ---------------------------------------------------------- human-only

    def activate_rules(self, version_id: int, *, base_sha256: str, activated_by: str) -> dict:
        """Make a version's text the active rules file (rulestore.activate). Human-only like approve: it changes
        what every later plan built with "the active rules" does, so only the browser's human channel and the
        CLI's terminal gate call it; the REST API and MCP never do."""
        return rulestore.activate(self.conn, self.config, version_id, base_sha256=base_sha256,
                                  activated_by=activated_by).as_dict()

    def approve(self, plan_id: int, code: str, *, approved_by: str) -> None:
        plans.approve_plan(self.conn, plan_id, code, approved_by=approved_by)

    def execute(self, plan_id: int, *, progress=None) -> dict:
        """Run an approved plan. A finished workspace plan also closes its draft, so the browser,
        the CLI and a later rescan all agree that the staged moves have happened."""
        result = _plain(executor.execute_plan(self.conn, self.rclone, self.config, plan_id, progress=progress))
        result.pop("failures", None)        # only a restore or a purge lists the items that stayed
        plan = plans.get_plan(self.conn, plan_id, with_ops=False)
        draft_id = draft_of(plan)
        if draft_id is not None and result.get("status") == "done":
            result.update(self._close_draft(draft_id, plan.params.get("draft_version")))
        return result

    def _close_draft(self, draft_id: int, previewed_version: int | None) -> dict:
        """Mark the draft behind a finished plan applied. Never fails the run: the clouds are already changed."""
        try:
            row = self.conn.execute("SELECT version FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if row is not None and row[0] != previewed_version:
                return {"draft_applied": False, "draft_error": f"draft {draft_id} changed after this preview"}
            workspace.mark_applied(self.conn, draft_id)
            return {"draft_applied": True}
        except Exception as e:  # WorkspaceError (not open any more), sqlite errors, ...
            return {"draft_applied": False, "draft_error": str(e) or type(e).__name__}

    # Connections. Human-only like approve: a sign-in uses the person's tokens and enabling writes widens what a
    # plan may change. The REST API token and MCP must never reach these; ui_api exposes them on the human
    # (session-cookie) channel only. Tokens never pass through here: connections.py returns names and quota only.

    def setup_status(self) -> dict:
        """First-run state, rclone, the connections, the providers (with what each needs and the command for
        another computer), `needed` (nothing configured, or a configured cloud with no complete scan) and the
        `manual_remotes` of config.toml. A connections.toml changed since it was loaded (`drivemerge connect`
        while the server runs) is reloaded first."""
        from . import connections
        if self._connections_mtime() != self._connections_seen:
            try:
                self.reload_config()
            except (OSError, ValueError):
                self._connections_seen = self._connections_mtime()   # unreadable now: keep what is loaded
        scanned = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT r.name FROM scans s JOIN remotes r ON r.id = s.remote_id WHERE s.status = 'complete'")}
        providers = []
        for key, p in connections.PROVIDER_SPECS.items():
            cmds = ({"read": connections.authorize_command(key, "read"), "write": connections.authorize_command(key, "write")}
                    if p.kind == "authorize" else {"read": [], "write": []})
            providers.append({"key": key, "label": p.label, "needs_client": p.needs_client, "kind": p.kind,
                              "fields": list(p.fields), "authorize": cmds})
        return {"first_run": not self.config.remotes, "config_path": str(self.config_path or DEFAULT_CONFIG_PATH),
                "rclone": connections.rclone_status(self.config),
                "connections": connections.list_connections(self.config),
                "providers": providers,
                "needed": not self.config.remotes or any(r.name not in scanned for r in self.config.remotes),
                "manual_remotes": [{"name": r.name, "provider": r.provider, "writable": r.write is not None}
                                   for r in self.config.remotes if not r.connection]}

    def list_connections(self) -> list[dict]:
        from . import connections
        return connections.list_connections(self.config)

    @staticmethod
    def _flow(flow) -> dict:
        return {"flow_id": flow.id, "provider": flow.provider, "purpose": flow.purpose, "status": flow.status,
                "auth_url": flow.auth_url, "error": flow.error}

    def start_connect(self, provider: str, *, purpose: str = "read", client_id: str | None = None,
                      client_secret: str | None = None) -> dict:
        from . import connections
        return self._flow(connections.start_flow(self.config, provider, purpose=purpose,
                                                 client_id=client_id, client_secret=client_secret))

    def connect_flow(self, flow_id: str) -> dict:
        from . import connections
        return self._flow(connections.get_flow(flow_id))

    def finish_connect(self, flow_id: str, *, name: str | None = None) -> dict:
        """Complete a finished sign-in: a `read` flow creates the connection (`name` optional), a `write` flow
        enables changes on connection `name` (required). The config is reloaded so it is usable at once."""
        from . import connections
        flow = connections.get_flow(flow_id)
        if flow.purpose == "write":
            if not name:
                raise ServiceError("name is required to enable changes on a connection")
            res = connections.finish_enable_writes(self.config, flow_id, name)
        else:
            res = connections.finish_connect(self.config, flow_id, name=name)
        if "flow_id" not in res:        # a question result means the connection is not made yet
            self.reload_config()
        return res

    def cancel_connect(self, flow_id: str) -> None:
        from . import connections
        connections.cancel_flow(flow_id)

    def test_connection(self, name: str) -> dict:
        from . import connections
        return connections.test_connection(self.config, name)

    def disconnect(self, name: str) -> None:
        from . import connections
        connections.disconnect(self.config, name)
        self.reload_config()

    def enable_writes(self, name: str, *, client_id: str | None = None, client_secret: str | None = None) -> dict:
        """Start the second, write-scope sign-in for connection `name`; finish it with finish_connect(flow, name=name)."""
        from . import connections
        provider = next((c["provider"] for c in connections.list_connections(self.config) if c["name"] == name), None)
        if provider is None:
            raise ServiceError(f"unknown connection {name!r}")
        return self._flow(connections.start_flow(self.config, provider, purpose="write", name=name,
                                                 client_id=client_id, client_secret=client_secret))

    def restore(self, plan_id: int, *, op_ids: list[int] | None = None, progress=None) -> dict:
        """Put this plan's quarantined files (or only `op_ids`) back where they were. `progress(done, failed, total)`."""
        return _plain(executor.restore_plan(self.conn, self.rclone, self.config, plan_id,
                                            op_ids=op_ids, progress=progress or self.default_progress))

    def purge(self, plan_id: int, *, older_than_days: int = 30, op_ids: list[int] | None = None,
              progress=None) -> dict:
        """Delete this plan's quarantined files (or only `op_ids`) that are old enough, after proving each witness
        live. `progress(settled, failed, total)`."""
        return _plain(executor.purge_quarantine(self.conn, self.rclone, self.config, plan_id,
                                                older_than_days=older_than_days, op_ids=op_ids,
                                                progress=progress or self.default_progress))

    def purge_due(self, remote: str | None = None, progress=None) -> dict:
        """Purge, plan after plan (oldest first), every item whose retention period has passed (on `remote` when
        given). Nothing else calls this: retention never purges by itself. A plan that fails does not stop the others.
        `progress(plans_total=, plans_done=, purged=, failed=)` is called at the start and after every item."""
        executor._require_enabled(self.config)
        retention = self.config.safety.quarantine_retention_days
        wanted = quarantine.due_items(self.conn, quarantine_dir=self.config.safety.quarantine_dir,
                                      retention_days=retention, remote=remote)
        out: dict = {"plans": [], "done": 0, "failed": 0, "skipped": 0}
        total = len(wanted)

        def report(plans_done: int, purged: int, failed: int) -> None:
            if progress:
                progress(plans_total=total, plans_done=plans_done, purged=purged, failed=failed)

        report(0, 0, 0)
        for n, (plan_id, op_ids) in enumerate(wanted.items()):
            try:
                result = self.purge(
                    plan_id, older_than_days=retention, op_ids=op_ids if remote else None,
                    progress=lambda settled, failed, _total, n=n: report(n, out["done"] + settled, out["failed"] + failed))
                entry = {"plan_id": plan_id, "status": result["status"], "done": result["done"],
                         "failed": result["failed"], "skipped": result["skipped"],
                         "failures": result["failures"], "error": result["error"]}
            except Exception as e:      # this plan could not be purged at all; the others still can
                entry = {"plan_id": plan_id, "status": "failed", "done": 0, "failed": 0, "skipped": 0,
                         "failures": [], "error": str(e) or type(e).__name__}
            out["plans"].append(entry)
            for key in ("done", "failed", "skipped"):
                out[key] += entry[key]
            report(n + 1, out["done"], out["failed"])
        out["error"] = next((p["error"] for p in out["plans"] if p["error"]), None)
        return out

    # ---------------------------------------------------------- quarantine (read-only)

    def quarantine_report(self) -> dict:
        """Held files per cloud and plan, what is due, and the files nothing accounts for (quarantine.py)."""
        return quarantine.quarantine_report(
            self.conn, quarantine_dir=self.config.safety.quarantine_dir,
            retention_days=self.config.safety.quarantine_retention_days)

    def quarantine_items(self, plan_id: int, **kw) -> dict:
        return quarantine.plan_items(
            self.conn, plan_id, quarantine_dir=self.config.safety.quarantine_dir,
            retention_days=self.config.safety.quarantine_retention_days, **kw)

    def quarantine_check(self, plan_id: int, action: str, **kw) -> dict:
        return quarantine.check(
            self.conn, plan_id, action, quarantine_dir=self.config.safety.quarantine_dir,
            retention_days=self.config.safety.quarantine_retention_days, **kw)


__all__ = ["Service", "ServiceError", "PLAN_KINDS", "RESERVED_PARAMS", "RULES_KINDS", "draft_of", "scrub_secrets"]
