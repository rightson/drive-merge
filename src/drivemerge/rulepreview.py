"""Live before/after preview of layout rules over the real catalog (docs/RULES.md sections D and 7).

A preview places the files of a scope under the edited rules and under the active rules and compares the two.
It is the expensive half of the Rules view, so it is:

* exact up to EXACT_LIMIT files in scope; above that a deterministic hash sample of SAMPLE_SIZE files (the same
  files on every keystroke, so counts do not flicker) whose counts are scaled to `estimate_*`;
* cached: unit detection per remote (it depends only on `unit_markers` and `bundle_patterns`), the active rules'
  placements, and the last PREVIEWS finished previews with their per-file rows, which `preview_page` serves;
* cancellable: one preview runs at a time; a newer request interrupts the running one (`Connection.interrupt()`
  and a flag `plan_layout` asks every 1,000 rows), which then answers PREVIEW_SUPERSEDED;
* read-only: it runs on whatever connection the caller hands it (the app's dedicated reader) inside one read
  transaction, and writes nothing.

Every cache key includes `db.snapshot` and the catalog's row version, so a scan or a change invalidates it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from . import classify, db
from .rulestore import RulesError

EXACT_LIMIT = 100_000          # files in scope up to which a preview is exact
SAMPLE_SIZE = 20_000           # files a sampled preview places
EXACT_MAX = 2_000_000          # files "exact" mode accepts
PREVIEWS = 8                   # finished previews kept for paging
MAX_CACHED_ROWS = 400_000      # per-file rows kept across those previews
PAGE_DEFAULT = 100
PAGE_MAX = 500
TOP_TRANSITIONS = 10
MODES = ("auto", "exact")


@dataclass
class _Preview:
    summary: dict
    rows: list[tuple]                      # (entry_id, remote, path, size, rule, target, unit, active_rule, active_target)
    has_baseline: bool


@dataclass
class _Scope:
    ids: list[int]
    names: list[str]
    target_root: str
    path_prefix: str | None

    def key(self) -> tuple:
        return (tuple(self.ids), self.target_root, self.path_prefix or "")


@dataclass
class Previewer:
    """The caches and the one-at-a-time machinery of one server. Thread-safe."""
    _cache: threading.Lock = field(default_factory=threading.Lock)
    _run: threading.Lock = field(default_factory=threading.Lock)
    _gen: int = 0
    _running: sqlite3.Connection | None = None
    _units: OrderedDict = field(default_factory=OrderedDict)
    _baselines: OrderedDict = field(default_factory=OrderedDict)
    _previews: OrderedDict = field(default_factory=OrderedDict)
    stats: Counter = field(default_factory=Counter)      # cache hits and misses, for tests

    # ------------------------------------------------------------------ public

    def preview(self, conn: sqlite3.Connection, *, edited_text: str, active_text: str | None,
                remotes: list[str] | None, target_root: str, path_prefix: str | None, mode: str,
                limit: int, filter: str, q: str | None = None) -> dict:
        """Place the scope under `edited_text` (and `active_text`, when it is valid rules) and compare.

        Raises the first RuleError of invalid rules, KeyError for an unknown remote, RulesError PREVIEW_TOO_LARGE
        (mode exact above EXACT_MAX files) and PREVIEW_SUPERSEDED (a newer preview was asked for meanwhile)."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        limit = max(1, min(int(limit), PAGE_MAX))
        _check_filter(filter)
        edited = classify.load_rules(edited_text)
        baseline_rules = None
        if active_text is not None:
            try:
                baseline_rules = classify.load_rules(active_text)
            except classify.RuleError:
                baseline_rules = None            # an invalid active file: nothing to compare with
        with self._cache:
            self._gen += 1
            mine = self._gen
            running = self._running
        if running is not None:
            try:
                running.interrupt()
            except sqlite3.Error:
                pass
        with self._run:
            if self._gen != mine:
                raise _superseded()
            for attempt in (1, 2):
                started = conn.in_transaction
                try:
                    if not started:
                        conn.execute("BEGIN")
                    with self._cache:
                        self._running = conn
                    return self._preview(conn, mine, edited_text, edited, active_text, baseline_rules, remotes,
                                         target_root, path_prefix, mode, limit, filter, q)
                except sqlite3.OperationalError as exc:
                    if "interrupt" not in str(exc).lower():
                        raise
                    if self._gen != mine:
                        raise _superseded() from None
                    if attempt == 2:
                        raise
                except classify.LayoutCancelled:
                    raise _superseded() from None
                finally:
                    with self._cache:
                        self._running = None
                    if not started:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass

    def page(self, preview_id: str, *, filter: str = "changed", q: str | None = None, offset: int = 0,
             limit: int = PAGE_DEFAULT) -> dict:
        """One page of a finished preview's files; RulesError PREVIEW_EXPIRED when it was evicted."""
        _check_filter(filter)
        with self._cache:
            cached = self._previews.get(preview_id)
            if cached is not None:
                self._previews.move_to_end(preview_id)
        if cached is None:
            raise RulesError(410, "PREVIEW_EXPIRED", "this preview is no longer kept; run it again", preview_id=preview_id)
        return _page(cached, filter, q, offset, limit)

    # ------------------------------------------------------------------ the work

    def _preview(self, conn, mine, edited_text, edited, active_text, baseline_rules, remotes, target_root,
                 path_prefix, mode, limit, filter, q) -> dict:
        t0 = time.monotonic()

        def cancelled() -> bool:
            return self._gen != mine

        scope = _scope(conn, remotes, target_root, path_prefix)
        state = _state(conn)
        files = _count_files(conn, scope)
        if mode == "exact" and files > EXACT_MAX:
            raise RulesError(409, "PREVIEW_TOO_LARGE", f"{files} files are in scope; an exact preview takes at most {EXACT_MAX}",
                             files=files, limit=EXACT_MAX)
        sampled = files > EXACT_LIMIT and mode == "auto"
        threshold = _threshold(files) if sampled else None
        rules_sha = _sha(edited_text)
        active_sha = _sha(active_text) if active_text is not None else None
        key = (rules_sha, active_sha if baseline_rules is not None else None, scope.key(), threshold, state)
        preview_id = "pv_" + hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:8]
        with self._cache:
            hit = self._previews.get(preview_id)
            if hit is not None:
                self._previews.move_to_end(preview_id)
                self.stats["preview_hits"] += 1
        if hit is not None:
            out = dict(hit.summary)
            out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
            out["page"] = _page(hit, filter, q, 0, limit)
            return out
        self.stats["preview_misses"] += 1

        placed = self._place(conn, edited, scope, threshold, state, cancelled)
        if baseline_rules is None:
            base = None
        elif active_sha == rules_sha:
            base = placed
        else:
            base = self._baseline(conn, active_sha, baseline_rules, scope, threshold, state, cancelled)
        sizes = _sizes(conn, scope, threshold, cancelled)
        summary, rows = _compare(preview_id, scope, edited, baseline_rules, placed, base, sizes, files, sampled,
                                 rules_sha, active_sha)
        summary["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        entry = _Preview(summary, rows, base is not None)
        with self._cache:
            self._previews[preview_id] = entry
            self._previews.move_to_end(preview_id)
            while len(self._previews) > PREVIEWS or (len(self._previews) > 1 and
                                                     sum(len(p.rows) for p in self._previews.values()) > MAX_CACHED_ROWS):
                self._previews.popitem(last=False)
        out = dict(summary)
        out["page"] = _page(entry, filter, q, 0, limit)
        return out

    def _unit_lists(self, conn, ruleset, scope, state, cancelled) -> dict[int, list]:
        """Units per selected remote, from the cache when the markers, bundle patterns and catalog are the same."""
        out = {}
        for rid in scope.ids:
            key = (rid, tuple(ruleset.unit_markers), tuple(ruleset.bundle_patterns), state)
            with self._cache:
                found = self._units.get(key)
                if found is not None:
                    self._units.move_to_end(key)
                    self.stats["units_hits"] += 1
            if found is None:
                if cancelled():
                    raise classify.LayoutCancelled()
                self.stats["units_misses"] += 1
                found = classify.detect_units(conn, ruleset, remote_ids=[rid])
                with self._cache:
                    self._units[key] = found
                    while len(self._units) > 64:
                        self._units.popitem(last=False)
            out[rid] = found
        return out

    def _place(self, conn, ruleset, scope, threshold, state, cancelled) -> list[classify.Placement]:
        return classify.plan_layout(
            conn, ruleset, remote_ids=scope.ids, target_root=scope.target_root, path_prefix=scope.path_prefix,
            sample_threshold=threshold, units=self._unit_lists(conn, ruleset, scope, state, cancelled), cancel=cancelled)

    def _baseline(self, conn, active_sha, ruleset, scope, threshold, state, cancelled) -> list[classify.Placement]:
        key = (active_sha, scope.key(), threshold, state)
        with self._cache:
            found = self._baselines.get(key)
            if found is not None:
                self._baselines.move_to_end(key)
                self.stats["baseline_hits"] += 1
        if found is None:
            self.stats["baseline_misses"] += 1
            found = self._place(conn, ruleset, scope, threshold, state, cancelled)
            with self._cache:
                self._baselines[key] = found
                while len(self._baselines) > 2 or (len(self._baselines) > 1 and sum(len(v) for v in self._baselines.values()) > MAX_CACHED_ROWS):
                    self._baselines.popitem(last=False)
        return found


# --------------------------------------------------------------------------- helpers

def _superseded() -> RulesError:
    return RulesError(409, "PREVIEW_SUPERSEDED", "a newer preview was asked for; this one was stopped")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_filter(filter: str) -> None:
    if filter in ("all", "changed", "fallback", "in_place") or (filter.startswith("rule:") and len(filter) > 5):
        return
    raise ValueError("filter must be all, changed, fallback, in_place or rule:<name>")


def _state(conn: sqlite3.Connection) -> str:
    """Identifies the catalog's state: the latest complete scans and the row version."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'row_version'").fetchone()
    return json.dumps([db.snapshot(conn), row[0] if row else None], sort_keys=True)


def _scope(conn: sqlite3.Connection, remotes: list[str] | None, target_root: str, path_prefix: str | None) -> _Scope:
    rows = conn.execute("SELECT id, name FROM remotes ORDER BY id").fetchall()
    if remotes:
        by_name = {r[1]: r[0] for r in rows}
        for name in remotes:
            if name not in by_name:
                raise KeyError(f"unknown remote {name!r}")
        picked = [(by_name[n], n) for n in dict.fromkeys(remotes)]
    else:
        picked = [(r[0], r[1]) for r in rows]
    classify._clean_root(target_root or "")          # a '.' or '..' segment is refused before any work
    return _Scope([i for i, _ in picked], [n for _, n in picked], (target_root or "").strip("/"),
                  path_prefix.strip("/") if path_prefix else None)


def _count_files(conn: sqlite3.Connection, scope: _Scope) -> int:
    where, args = classify._under(scope.path_prefix or "", include_self=True)
    total = 0
    for rid in scope.ids:
        total += conn.execute(
            f"SELECT COUNT(*) FROM entries WHERE remote_id = ? AND is_dir = 0 AND present = 1 AND {where}",
            (rid, *args)).fetchone()[0]
    return total


def _threshold(files: int) -> int:
    """The hash threshold that keeps about SAMPLE_SIZE of `files` rows."""
    return max(1, min(classify.SAMPLE_MODULUS, round(SAMPLE_SIZE / files * classify.SAMPLE_MODULUS)))


def _sizes(conn: sqlite3.Connection, scope: _Scope, threshold: int | None, cancelled) -> dict[int, int]:
    where, args = classify._under(scope.path_prefix or "", include_self=True)
    if threshold is not None:
        where += f" AND ((id * {classify.SAMPLE_MULTIPLIER}) % {classify.SAMPLE_MODULUS}) < {int(threshold)}"
    out: dict[int, int] = {}
    for rid in scope.ids:
        for n, (eid, size) in enumerate(conn.execute(
                f"SELECT id, size FROM entries WHERE remote_id = ? AND is_dir = 0 AND present = 1 AND {where}",
                (rid, *args))):
            if n % classify.CANCEL_EVERY == 0 and cancelled():
                raise classify.LayoutCancelled()
            out[eid] = size or 0
    return out


def _compare(preview_id, scope, edited, baseline_rules, placed, base, sizes, files, sampled, rules_sha, active_sha):
    by_id = {p.entry_id: p for p in base} if base is not None else {}
    names = dict(zip(scope.ids, scope.names))
    edited_rules = [(r.name, r.unit) for r in edited.rules]
    active_rules = [(r.name, r.unit) for r in baseline_rules.rules] if baseline_rules is not None else []
    per = {name: {"files": 0, "bytes": 0, "units": set()} for name, _u in edited_rules}
    per["fallback"] = {"files": 0, "bytes": 0, "units": set()}
    per_active = {name: 0 for name, _u in active_rules}
    per_active["fallback"] = 0
    transitions: Counter = Counter()
    targets: Counter = Counter()
    in_place = changed = 0
    rows: list[tuple] = []
    for p in placed:
        size = sizes.get(p.entry_id, 0)
        slot = per.setdefault(p.rule, {"files": 0, "bytes": 0, "units": set()})
        slot["files"] += 1
        slot["bytes"] += size
        if p.unit is not None:
            slot["units"].add((p.remote_id, p.unit))
        targets[p.target_path] += 1
        if p.target_path == p.src_path:
            in_place += 1
        a = by_id.get(p.entry_id)
        if a is not None:
            per_active[a.rule] = per_active.get(a.rule, 0) + 1
            if a.target_path != p.target_path:
                changed += 1
                transitions[(a.rule, p.rule)] += 1
        rows.append((p.entry_id, names.get(p.remote_id, str(p.remote_id)), p.src_path, size, p.rule, p.target_path,
                     p.unit, a.rule if a else None, a.target_path if a else None))
    n = len(placed)
    scale = files / n if sampled and n else 1.0

    def est(count: int) -> int:
        return round(count * scale) if sampled else count

    active_names = {name for name, _u in active_rules}
    rules = []
    for name, unit in edited_rules:
        slot = per[name]
        before = per_active[name] if base is not None and name in active_names else None
        rules.append({"name": name, "unit": unit, "files": slot["files"], "bytes": slot["bytes"],
                      "units": len(slot["units"]) if unit else None, "active_files": before,
                      "estimate_files": est(slot["files"]), "estimate_bytes": est(slot["bytes"]),
                      "estimate_active_files": est(before) if before is not None else None})
    edited_names = {name for name, _u in edited_rules}
    for name, unit in active_rules:                  # rules the edit removed: what they took before
        if name not in edited_names and base is not None:
            rules.append({"name": name, "unit": unit, "files": 0, "bytes": 0, "units": 0 if unit else None,
                          "active_files": per_active.get(name, 0), "estimate_files": 0, "estimate_bytes": 0,
                          "estimate_active_files": est(per_active.get(name, 0)), "removed": True})
    fb = per["fallback"]
    summary = {
        "preview_id": preview_id, "rules_sha256": rules_sha, "active_sha256": active_sha,
        "scope": {"remotes": scope.names, "target_root": scope.target_root, "path_prefix": scope.path_prefix},
        "files_in_scope": files, "sampled": sampled, "sample_files": n,
        "sample_rate": round(n / files, 4) if sampled and files else 1.0, "elapsed_ms": 0,
        "rules": rules,
        "fallback": {"files": fb["files"], "bytes": fb["bytes"], "estimate_files": est(fb["files"]),
                     "active_files": per_active["fallback"] if base is not None else None},
        "in_place": in_place, "estimate_in_place": est(in_place),
        "changed": changed if base is not None else None,
        "estimate_changed": est(changed) if base is not None else None,
        "shared_targets": sum(1 for c in targets.values() if c > 1),
        "transitions": [{"from": frm, "to": to, "files": c, "estimate_files": est(c)}
                        for (frm, to), c in sorted(transitions.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_TRANSITIONS]],
    }
    return summary, rows


def _page(preview: _Preview, filter: str, q: str | None, offset: int, limit: int) -> dict:
    limit = max(1, min(int(limit), PAGE_MAX))
    offset = max(0, int(offset))
    needle = q.lower() if q else None
    if filter == "all" or (filter == "changed" and not preview.has_baseline):
        pick = None
    elif filter == "changed":
        pick = lambda r: r[7] is not None and r[8] != r[5]          # noqa: E731
    elif filter == "fallback":
        pick = lambda r: r[4] == "fallback"                          # noqa: E731
    elif filter == "in_place":
        pick = lambda r: r[5] == r[2]                                # noqa: E731
    else:
        wanted = filter[5:]
        pick = lambda r: r[4] == wanted                              # noqa: E731
    total, files = 0, []
    for r in preview.rows:
        if pick is not None and not pick(r):
            continue
        if needle is not None and needle not in f"{r[1]}:{r[2]}".lower():
            continue
        if offset <= total < offset + limit:
            files.append({"entry_id": r[0], "remote": r[1], "path": r[2], "size": r[3], "rule": r[4], "target": r[5],
                          "unit": r[6], "active_rule": r[7], "active_target": r[8], "in_place": r[5] == r[2]})
        total += 1
    return {"filter": filter, "q": q, "offset": offset, "limit": limit, "total": total, "files": files}
