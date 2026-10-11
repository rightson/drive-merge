"""One facade over the core modules, shared by the CLI, the REST API and the MCP server.

Everything returns JSON-serialisable values. Methods are grouped by trust level:

* read        — catalog queries, compare/coverage/explore and workspace trees; safe for anyone with access to the service
* maintain    — scans, hash bridging and directory statistics; read-only on the clouds
* propose     — build and simulate plans, stage workspace drafts; writes only to the local catalog
* human-only  — approve / execute / restore / purge. The MCP server and the
                REST API never call these; the CLI gates them behind a TTY and
                the browser workspace behind its own session (ui_api).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
from pathlib import Path

from . import (
    builders, classify, compare, coverage, db, dirstats, executor, explore, identity, plans, search, simulate,
    suggest, workspace,
)
from .config import Config, sync_remotes
from .models import Plan
from .rclone import Rclone
from .scanner import scan_remote

PLAN_KINDS = ("dedupe", "consolidate", "reorganize", "manual")
# Written only by workspace.preview; a proposer's params must never be able to claim a draft.
RESERVED_PARAMS = ("draft_id", "draft_version")
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
    def __init__(self, config: Config, conn: sqlite3.Connection | None = None, rclone: Rclone | None = None):
        self.config = config
        self.conn = conn if conn is not None else db.connect(config.db_path)
        sync_remotes(self.conn, config)
        self.rclone = rclone or Rclone(
            config.rclone_binary, config=config.rclone_config, extra_args=config.rclone_extra_args
        )

    # ---------------------------------------------------------------- read

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

    def ruleset(self, rules_text: str | None = None) -> classify.RuleSet:
        if rules_text is not None:
            return classify.load_rules(rules_text)
        if self.config.rules_path:
            return classify.load_rules(Path(self.config.rules_path).expanduser().read_text(encoding="utf-8"))
        return classify.default_rules()

    def validate_rules(self, rules_text: str) -> dict:
        try:
            rs = classify.load_rules(rules_text)
        except classify.RuleError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "rules": [r.name for r in rs.rules]}

    def preview_layout(self, *, rules_text: str | None = None, remotes: list[str] | None = None,
                       target_root: str = "", path_prefix: str | None = None, limit: int = 200) -> dict:
        rs = self.ruleset(rules_text)
        ids = [db.remote_id(self.conn, r) for r in remotes] if remotes else None
        placements = classify.plan_layout(self.conn, rs, remote_ids=ids, target_root=target_root,
                                          path_prefix=path_prefix)
        by_rule: dict[str, int] = {}
        for p in placements:
            by_rule[p.rule] = by_rule.get(p.rule, 0) + 1
        return {"total": len(placements), "by_rule": by_rule, "sample": _plain(placements[:limit])}

    # ------------------------------------------------------------ maintain

    def scan(self, remote: str, *, root: str = "", progress=None, force: bool = False) -> dict:
        res = _plain(scan_remote(self.conn, self.rclone, self.config.remote(remote), root=root,
                                 progress=progress, force=force))
        # Folder counts in the workspace come from dir_stats; keep them in step with the catalog.
        self.refresh_dir_stats([remote])
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

    def bridge(self, *, max_bytes: int | None = None, limit: int = 100, progress=None, started=None) -> dict:
        """Download each candidate once (read remote only), hash it, record the bridge, regroup.
        `progress(done, failed)` runs after each file; `started(total)` once, with the number of candidates."""
        done, failed = [], []
        with tempfile.TemporaryDirectory(prefix="drivemerge-bridge-") as tmp:
            candidates = identity.bridge_candidates(self.conn, max_bytes=max_bytes, limit=limit)
            if started:
                started(len(candidates))
            for cand in candidates:
                try:
                    identity.bridge_entry(self.conn, self.rclone, cand.entry_id, tmp)
                    done.append(cand.entry_id)
                except Exception as e:  # one bad file must not stop the batch
                    failed.append({"entry_id": cand.entry_id, "error": str(e)})
                if progress:
                    progress(len(done), len(failed))
        stats = identity.rebuild_groups(self.conn) if done else None
        if done:
            self.refresh_dir_stats()
        return {"bridged": len(done), "failed": failed, "groups": _plain(stats)}

    # ------------------------------------------------------------- propose

    def _build(self, kind: str, params: dict) -> tuple[builders.BuildResult, dict, str | None]:
        """Run the builder of `kind`: (result, params without `rules`, the rules text or None). Persists nothing."""
        if kind not in PLAN_KINDS:
            raise ServiceError(f"unknown plan kind {kind!r}; expected one of {PLAN_KINDS}")
        claimed = [k for k in RESERVED_PARAMS if k in params]
        if claimed:
            raise ServiceError(f"params {', '.join(claimed)} are reserved for workspace previews; "
                               "stage moves into a draft and preview it instead")
        params = dict(params)
        rules_text = params.pop("rules", None)
        # Quarantined files are deleted content, not live copies; builders must know where they are.
        quarantine = {"quarantine_dir": self.config.safety.quarantine_dir}
        if kind == "dedupe":
            result = builders.build_dedupe(self.conn, **{**quarantine, **params})
        elif kind == "consolidate":
            result = builders.build_consolidate(self.conn, ruleset=self.ruleset(rules_text),
                                                **{**quarantine, **params})
        elif kind == "reorganize":
            result = builders.build_reorganize(self.conn, ruleset=self.ruleset(rules_text),
                                               **{**quarantine, **params})
        else:
            result = builders.build_manual(self.conn, params.get("ops", []))
        return result, params, rules_text

    def propose_plan(self, kind: str, params: dict, *, created_by: str, title: str | None = None) -> dict:
        """Build a plan, persist it as a draft and simulate it. Never touches a cloud."""
        result, params, rules_text = self._build(kind, params)
        if not result.ops:
            return {"plan_id": None, "ops": 0, "skipped": result.skipped[:SAMPLE_SKIPPED], "stats": result.stats,
                    "message": "nothing to do", "code": "NOTHING_TO_DO"}
        if rules_text is not None:
            params["rules"] = rules_text
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
        result, params, _ = self._build(kind, params)
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
        scrubbed `params`, the `draft_id` it previews and `outdated`: the catalog was scanned again since that
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
                "params": scrub_secrets(p.params), "draft_id": draft_of(p),
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

    def approve(self, plan_id: int, code: str, *, approved_by: str) -> None:
        plans.approve_plan(self.conn, plan_id, code, approved_by=approved_by)

    def execute(self, plan_id: int, *, progress=None) -> dict:
        """Run an approved plan. A finished workspace plan also closes its draft, so the browser,
        the CLI and a later rescan all agree that the staged moves have happened."""
        result = _plain(executor.execute_plan(self.conn, self.rclone, self.config, plan_id, progress=progress))
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

    def restore(self, plan_id: int) -> dict:
        return _plain(executor.restore_plan(self.conn, self.rclone, self.config, plan_id))

    def purge(self, plan_id: int, *, older_than_days: int = 30) -> dict:
        return _plain(executor.purge_quarantine(self.conn, self.rclone, self.config, plan_id,
                                                older_than_days=older_than_days))


__all__ = ["Service", "ServiceError", "PLAN_KINDS", "RESERVED_PARAMS", "draft_of", "scrub_secrets"]
