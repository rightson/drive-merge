"""One facade over the core modules, shared by the CLI, the REST API and the MCP server.

Everything returns JSON-serialisable values. Methods are grouped by trust level:

* read        — catalog queries; safe for anyone with access to the service
* maintain    — scans and hash bridging; read-only on the clouds
* propose     — build and simulate plans; writes only to the local catalog
* human-only  — approve / execute / restore / purge. The MCP server and the
                REST API never call these; the CLI gates them behind a TTY.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
from pathlib import Path

from . import builders, classify, db, executor, identity, plans, search, simulate
from .config import Config, sync_remotes
from .rclone import Rclone
from .scanner import scan_remote

PLAN_KINDS = ("dedupe", "consolidate", "reorganize", "manual")


def _plain(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [_plain(o) for o in obj]
    return obj


class ServiceError(ValueError):
    pass


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
        return _plain(scan_remote(self.conn, self.rclone, self.config.remote(remote), root=root,
                                  progress=progress, force=force))

    def rebuild_identity(self) -> dict:
        return _plain(identity.rebuild_groups(self.conn))

    def bridge(self, *, max_bytes: int | None = None, limit: int = 100, progress=None) -> dict:
        """Download each candidate once (read remote only), hash it, record the bridge, regroup."""
        done, failed = [], []
        with tempfile.TemporaryDirectory(prefix="drivemerge-bridge-") as tmp:
            for cand in identity.bridge_candidates(self.conn, max_bytes=max_bytes, limit=limit):
                try:
                    identity.bridge_entry(self.conn, self.rclone, cand.entry_id, tmp)
                    done.append(cand.entry_id)
                except Exception as e:  # one bad file must not stop the batch
                    failed.append({"entry_id": cand.entry_id, "error": str(e)})
                if progress:
                    progress(len(done), len(failed))
        stats = identity.rebuild_groups(self.conn) if done else None
        return {"bridged": len(done), "failed": failed, "groups": _plain(stats)}

    # ------------------------------------------------------------- propose

    def propose_plan(self, kind: str, params: dict, *, created_by: str, title: str | None = None) -> dict:
        """Build a plan, persist it as a draft and simulate it. Never touches a cloud."""
        if kind not in PLAN_KINDS:
            raise ServiceError(f"unknown plan kind {kind!r}; expected one of {PLAN_KINDS}")
        params = dict(params)
        rules_text = params.pop("rules", None)
        if kind == "dedupe":
            result = builders.build_dedupe(self.conn, **params)
        elif kind == "consolidate":
            result = builders.build_consolidate(self.conn, ruleset=self.ruleset(rules_text), **params)
        elif kind == "reorganize":
            result = builders.build_reorganize(self.conn, ruleset=self.ruleset(rules_text), **params)
        else:
            result = builders.build_manual(self.conn, params.get("ops", []))
        if not result.ops:
            return {"plan_id": None, "ops": 0, "skipped": result.skipped[:200], "stats": result.stats,
                    "message": "nothing to do"}
        if rules_text is not None:
            params["rules"] = rules_text
        plan_id = plans.create_plan(self.conn, title or f"{kind} plan", kind, result.ops,
                                    created_by=created_by, params=params)
        report = self.simulate(plan_id)
        return {"plan_id": plan_id, "ops": len(result.ops), "skipped": result.skipped[:200],
                "stats": result.stats, "simulation": report}

    def simulate(self, plan_id: int) -> dict:
        policy = simulate.SafetyPolicy(
            allow_probable_delete=self.config.safety.allow_probable_delete,
            min_copies=self.config.safety.min_copies,
        )
        report = simulate.simulate_plan(self.conn, plan_id, policy=policy)
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

    def cancel(self, plan_id: int) -> None:
        plans.cancel_plan(self.conn, plan_id)

    # ---------------------------------------------------------- human-only

    def approve(self, plan_id: int, code: str, *, approved_by: str) -> None:
        plans.approve_plan(self.conn, plan_id, code, approved_by=approved_by)

    def execute(self, plan_id: int, *, progress=None) -> dict:
        return _plain(executor.execute_plan(self.conn, self.rclone, self.config, plan_id, progress=progress))

    def restore(self, plan_id: int) -> dict:
        return _plain(executor.restore_plan(self.conn, self.rclone, self.config, plan_id))

    def purge(self, plan_id: int, *, older_than_days: int = 30) -> dict:
        return _plain(executor.purge_quarantine(self.conn, self.rclone, self.config, plan_id,
                                                older_than_days=older_than_days))


__all__ = ["Service", "ServiceError", "PLAN_KINDS"]
