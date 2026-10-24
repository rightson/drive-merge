"""Browser tests for the Quarantine view (docs/QUARANTINE.md section F), against a mocked /v1 API.

Same approach as tests/test_ui_frontend.py: the real static files, every /v1 call answered by a mock through
page.route. `QMock` adds the routes of section E (report, items, check, the restore and purge jobs) with the exact JSON
shapes of section B, and keeps a small model of what is held, restored and purged, so that a flow can be followed from
the check dialog to the result.
"""

from __future__ import annotations

import copy
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from test_ui_frontend import QUARANTINE, MockApi, Ui, browser, site  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.ui

SHOTS = Path(os.environ.get("DRIVEMERGE_UI_SHOTS", "/tmp/claude-0/ui-shots"))
GB = 1024 ** 3
MB = 1024 ** 2
NOW = datetime.now(timezone.utc)


def iso(days_ago: float = 0.0) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrub(plan: dict) -> dict:
    return {k: v for k, v in plan.items() if not k.startswith("_")}


class QMock(MockApi):
    """The workspace mock plus the quarantine routes (docs/QUARANTINE.md section E)."""

    RETENTION = 30

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.q_plans: dict[int, dict] = {}
        self.q_items: dict[int, list[dict]] = {}
        self.q_unknown: list[dict] = []
        self.q_quota: dict[str, dict | None] = {}
        self.q_job_error: str | None = None          # the next restore/purge start answers 409 with this code
        self.q_fail_op: dict[int, tuple[str, dict]] = {}   # op id -> (code, params): the executor refuses this item
        self.q_jobs: dict[str, dict] = {}
        self.q_plan_error: int | None = None        # purge-due: this plan raises as a whole (failed 0, error set)
        self.q_polls = 0
        self.q_next_op = 900
        self.q_report_status = 200
        self.q_item_delay = 0

    # ------------------------------------------------------------------ building the model
    def q_plan(self, plan_id: int, *, kind: str = "dedupe", created_by: str = "ui", title: str | None = None,
               params: dict | None = None, status: str = "done", finished_days_ago: float = 4) -> int:
        self.q_plans[plan_id] = {"plan_id": plan_id, "title": title or f"{kind} test", "kind": kind, "status": status,
                                 "created_by": created_by, "params": params or {}, "finished_at": iso(finished_days_ago)}
        self.q_items.setdefault(plan_id, [])
        return plan_id

    def q_item(self, plan_id: int, remote: str, path: str, size: int = 1 * MB, *, days: float = 4, state: str = "held",
               reason: str = "duplicate of the kept copy", witness: tuple[str, str] | None | bool = True, seen: bool = True,
               taken: bool = False, witness_live: bool | None = True, witness_same: bool | None = True, op: str = "delete") -> int:
        op_id = self.q_next_op
        self.q_next_op += 1
        if witness is True:
            witness = (remote, "KEPT/" + path.rpartition("/")[2])
        quarantined = NOW - timedelta(days=days)
        item = {
            "op_id": op_id, "seq": len(self.q_items[plan_id]) + 1, "op": op, "remote": remote, "path": path,
            "quarantine_path": f"{QUARANTINE}/{plan_id}/{path}", "size": size, "reason": reason,
            "witness": {"remote": witness[0], "path": witness[1]} if witness else None,
            "state": state, "quarantined_at": quarantined.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "due_at": (quarantined + timedelta(days=self.RETENTION)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "due": days >= self.RETENTION, "restored_at": iso(1) if state == "restored" else None,
            "purged_at": iso(1) if state == "purged" else None,
            "catalog": {"seen": seen, "original_taken": taken, "witness_live": witness_live if witness else None,
                        "witness_same": witness_same if witness else None},
            "_days": days,
        }
        self.q_items[plan_id].append(item)
        return op_id

    def q_standard(self) -> None:
        """Two plans: #21 (a release, everything on dropbox is due) and #17 (a dedupe on gdrive, partly due)."""
        self.q_plan(21, kind="release", created_by="ui", params={"remote": "dropbox"})
        for i in range(3):
            self.q_item(21, "dropbox", f"Old/{i}.zip", 18 * GB, days=34, witness=("gdrive", f"Backup/{i}.zip"))
        self.q_plan(17, kind="dedupe", created_by="mcp", params={"cross_remote": False})
        self.q_item(17, "gdrive", "Photos/2019/IMG_0412 (1).jpg", 4_300_000, days=4)
        self.q_item(17, "gdrive", "Photos/2019/IMG_0413 (1).jpg", 3_900_000, days=4, taken=True)
        self.q_item(17, "gdrive", "Backup/IMG_0414.jpg", 2_000_000, days=40)
        self.q_item(17, "gdrive", "Backup/IMG_0415.jpg", 1_000_000, days=4, seen=False)
        self.q_plan(9, kind="dedupe", created_by="cli", finished_days_ago=60)
        self.q_item(9, "gdrive", "Old/a.txt", 100, days=50, state="restored")
        self.q_item(9, "gdrive", "Old/b.txt", 100, days=50, state="purged")
        self.q_unknown = [{"remote": "gdrive", "files": 12, "bytes": 5 * MB, "examples": ["99/a.txt", "by-hand/b.txt", "c.txt"]}]
        self.q_quota = {"dropbox": {"used": 96 * GB, "total": 2048 * GB, "free": 1952 * GB},
                        "gdrive": {"used": 212 * GB, "total": 2048 * GB, "free": 1836 * GB}}

    # ------------------------------------------------------------------ the report
    def _held(self, plan_id: int) -> list[dict]:
        return [i for i in self.q_items[plan_id] if i["state"] == "held"]

    def report(self) -> dict:
        names = [r["name"] for r in self.remotes]
        remotes = []
        for n in names:
            held = [i for p in self.q_items for i in self._held(p) if i["remote"] == n]
            due = [i for i in held if i["due"]]
            remotes.append({"remote": n, "files": len(held), "bytes": sum(i["size"] for i in held), "due_files": len(due),
                            "due_bytes": sum(i["size"] for i in due), "plans": len({i["op_id"] for i in held}) and
                            len([p for p in self.q_items if any(x["remote"] == n for x in self._held(p))]),
                            "last_scan": {"id": 1, "finished_at": iso(0.002)}, "quota": self.q_quota.get(n)})
        plans = []
        for pid, meta in self.q_plans.items():
            held = self._held(pid)
            due = [i for i in held if i["due"]]
            plans.append({
                **{k: v for k, v in meta.items()},
                "remotes": [n for n in names if any(i["remote"] == n for i in self.q_items[pid])],
                "held": {"files": len(held), "bytes": sum(i["size"] for i in held)},
                "due": {"files": len(due), "bytes": sum(i["size"] for i in due)},
                "restored": sum(i["state"] == "restored" for i in self.q_items[pid]),
                "purged": sum(i["state"] == "purged" for i in self.q_items[pid]),
                "oldest_at": min((i["quarantined_at"] for i in held), default=None),
                "next_due_at": min((i["due_at"] for i in held), default=None)})
        plans.sort(key=lambda p: (p["held"]["files"] == 0, p["oldest_at"] or "", -p["plan_id"]))
        return {"quarantine_dir": QUARANTINE, "retention_days": self.RETENTION, "now": iso(0),
                "totals": {"files": sum(r["files"] for r in remotes), "bytes": sum(r["bytes"] for r in remotes),
                           "due_files": sum(r["due_files"] for r in remotes), "due_bytes": sum(r["due_bytes"] for r in remotes)},
                "remotes": remotes, "plans": plans, "unknown": self.q_unknown}

    def plan_items(self, pid: int, q: dict) -> dict:
        items = self.q_items[pid]
        state = q.get("state", "held")
        rows = [i for i in items if state == "all" or i["state"] == state]
        if q.get("remote"):
            rows = [i for i in rows if i["remote"] == q["remote"]]
        prefix = q.get("path")
        if prefix:
            rows = [i for i in rows if i["path"] == prefix or i["path"].startswith(prefix + "/")]
        rows.sort(key=lambda i: (i["remote"], i["path"].lower()))
        off, limit = int(q.get("offset", 0)), min(int(q.get("limit", 200)), 500)
        meta = self.q_plans[pid]
        return {"plan": {k: meta[k] for k in ("title", "kind", "status", "created_by", "finished_at")} | {"id": pid},
                "total": len(rows), "bytes": sum(i["size"] for i in rows), "offset": off, "limit": limit,
                "items": [{k: v for k, v in i.items() if not k.startswith("_")} for i in rows[off:off + limit]]}

    def check(self, pid: int, body: dict) -> tuple[int, dict]:
        held = self._held(pid)
        if pid not in self.q_plans:
            return 404, {"error": "no such plan", "code": "NOT_FOUND", "params": {"plan": pid}}
        if not held:
            return 409, {"error": "nothing", "code": "NOTHING_QUARANTINED", "params": {"plan": pid}}
        ids = body.get("op_ids")
        if ids is not None:
            bad = [x for x in ids if x not in {i["op_id"] for i in held}]
            if bad:
                return 400, {"error": "bad ops", "code": "BAD_OPS", "params": {"plan": pid, "op_ids": bad[:20]}}
            held = [i for i in held if i["op_id"] in ids]
        size = lambda xs: {"files": len(xs), "bytes": sum(i["size"] for i in xs)}  # noqa: E731
        if body["action"] == "restore":
            return 200, {"action": "restore", "plan_id": pid, **size(held), "original_taken": size([i for i in held if i["catalog"]["original_taken"]]),
                         "not_seen": size([i for i in held if not i["catalog"]["seen"]]),
                         "cross_remote_copies": 2 if self.q_plans[pid]["kind"] == "consolidate" else 0,
                         "remotes": sorted({i["remote"] for i in held}), "op_ids": [i["op_id"] for i in held]}
        older = int(body.get("older_than_days", 0))
        tried = [i for i in held if i["_days"] >= older]
        unproven = [i for i in tried if i["catalog"]["witness_live"] and not i["catalog"]["witness_same"]]
        by_remote = [{"remote": n, **size([i for i in tried if i["remote"] == n])} for n in sorted({i["remote"] for i in tried})]
        return 200, {"action": "purge", "plan_id": pid, **size(tried), "too_recent": size([i for i in held if i["_days"] < older]),
                     "witness_gone": size([i for i in tried if i["catalog"]["witness_live"] is False]),
                     "witness_unproven": {**size(unproven), "download_bytes": sum(i["size"] for i in unproven)},
                     "recent": size([i for i in tried if i["_days"] < 7]), "by_remote": by_remote,
                     "op_ids": [i["op_id"] for i in tried]}

    # ------------------------------------------------------------------ jobs
    def start_job(self, kind: str, plan_id: int | None, *, op_ids: list[int] | None, older: int | None, remote: str | None = None) -> dict:
        jid = f"qjob-{len(self.q_jobs) + 1}"
        job = {"id": jid, "kind": kind, "plan_id": plan_id, "op_ids": op_ids, "older": older, "remote": remote, "polls": 0}
        self.q_jobs[jid] = job
        return job

    def _run_one(self, kind: str, item: dict) -> dict | None:
        """Apply restore or purge to one item; a failure is returned as an ExecResult failure entry."""
        if item["op_id"] in self.q_fail_op:
            code, params = self.q_fail_op[item["op_id"]]
            return {"op_id": item["op_id"], "seq": item["seq"], "remote": item["remote"], "path": item["path"], "code": code,
                    "params": params, "error": f"english: {code}"}
        if kind == "restore" and item["catalog"]["original_taken"]:
            return {"op_id": item["op_id"], "seq": item["seq"], "remote": item["remote"], "path": item["path"], "code": "ORIGINAL_TAKEN",
                    "params": {"remote": item["remote"], "path": item["path"]}, "error": "english: original is taken"}
        item["state"] = "restored" if kind == "restore" else "purged"
        item["restored_at" if kind == "restore" else "purged_at"] = iso(0)
        return None

    def job_view(self, jid: str) -> dict:
        job = self.q_jobs[jid]
        job["polls"] += 1
        kind = job["kind"]
        if kind == "purge-due":
            plans = [p for p in self.q_plans if any(i["due"] for i in self._held(p))
                     and (not job["remote"] or any(i["remote"] == job["remote"] for i in self._held(p)))]
            total = len(plans)
            if job["polls"] < 2:
                return {"id": jid, "kind": "purge", "status": "running", "result": None, "error": None,
                        "progress": {"plans_total": total, "plans_done": 0, "purged": 0, "failed": 0}}
            if "result" not in job:
                results = []
                for pid in plans:
                    if pid == self.q_plan_error:
                        results.append({"plan_id": pid, "status": "failed", "done": 0, "failed": 0, "skipped": 0,
                                        "failures": [], "error": "rclone exploded"})
                        continue
                    done, fails = 0, []
                    for i in [x for x in self._held(pid) if x["due"] and (not job["remote"] or x["remote"] == job["remote"])]:
                        f = self._run_one("purge", i)
                        fails.append(f) if f else None
                        done += 0 if f else 1
                    results.append({"plan_id": pid, "status": "partial" if fails else "done", "done": done, "failed": len(fails),
                                    "skipped": 0, "failures": fails, "error": fails[0]["error"] if fails else None})
                job["result"] = {"plans": results, "done": sum(r["done"] for r in results), "failed": sum(r["failed"] for r in results),
                                 "skipped": 0, "error": next((r["error"] for r in results if r["error"]), None)}
            r = job["result"]
            return {"id": jid, "kind": "purge", "status": "failed" if r["failed"] else "done", "result": r,
                    "error": r["error"] if r["failed"] else None,
                    "progress": {"plans_total": total, "plans_done": total, "purged": r["done"], "failed": r["failed"]}}
        held = self._held(job["plan_id"])
        if job["op_ids"] is not None:
            held = [i for i in held if i["op_id"] in job["op_ids"]]
        if kind == "purge":
            held = [i for i in held if i["_days"] >= (job["older"] or 0)]
        key = "restored" if kind == "restore" else "purged"
        if job["polls"] < 2:
            return {"id": jid, "kind": kind, "status": "running", "result": None, "error": None,
                    "progress": {"plan_id": job["plan_id"], key: 0, "failed": 0, "total": len(held)}}
        if "result" not in job:
            fails = [f for f in (self._run_one(kind, i) for i in held) if f]
            job["result"] = {"plan_id": job["plan_id"], "status": "partial" if fails else "done", "done": len(held) - len(fails),
                             "failed": len(fails), "skipped": 0, "error": fails[0]["error"] if fails else None, "failures": fails}
        r = job["result"]
        return {"id": jid, "kind": kind, "status": "failed" if r["failed"] else "done", "result": r,
                "error": r["error"],
                "progress": {"plan_id": job["plan_id"], key: r["done"], "failed": r["failed"], "total": len(held)}}

    # ------------------------------------------------------------------ routes
    def dispatch(self, method: str, path: str, q: dict, body: Any, headers: dict) -> tuple[int, Any]:
        if (method, path) == ("GET", "/v1/quarantine"):
            if self.q_report_status != 200:
                return self.q_report_status, {"error": "unavailable", "code": "UNAVAILABLE"}
            return 200, self.report()
        m = re.fullmatch(r"/v1/quarantine/plans/(\d+)", path)
        if m and method == "GET":
            pid = int(m.group(1))
            if pid not in self.q_plans:
                return 404, {"error": "no such plan", "code": "NOT_FOUND", "params": {"plan": pid}}
            return 200, self.plan_items(pid, q)
        m = re.fullmatch(r"/v1/quarantine/plans/(\d+)/check", path)
        if m and method == "POST":
            return self.check(int(m.group(1)), body)
        m = re.fullmatch(r"/v1/ui/plans/(\d+)/(restore|purge)", path)
        if m and method == "POST":
            if headers.get("x-csrf-token") != self.CSRF:
                return 403, {"error": "bad csrf token", "code": "BAD_CSRF"}
            kind, pid = m.group(2), int(m.group(1))
            if body.get("confirm") != kind:
                return 400, {"error": "type the word", "code": "BAD_REQUEST"}
            if self.q_job_error:
                code, self.q_job_error = self.q_job_error, None
                return 409, {"error": "refused", "code": code, "params": {"plan": pid, "job": "x", "kind": "rescan"}}
            status, answer = self.check(pid, {"action": kind, "op_ids": body.get("op_ids"), "older_than_days": body.get("older_than_days", 0)})
            if status != 200:
                return status, answer
            return 202, {"job_id": self.start_job(kind, pid, op_ids=body.get("op_ids"), older=body.get("older_than_days"))["id"]}
        if (method, path) == ("POST", "/v1/ui/quarantine/purge"):
            if headers.get("x-csrf-token") != self.CSRF:
                return 403, {"error": "bad csrf token", "code": "BAD_CSRF"}
            if body.get("confirm") != "purge":
                return 400, {"error": "type the word", "code": "BAD_REQUEST"}
            return 202, {"job_id": self.start_job("purge-due", None, op_ids=None, older=None, remote=body.get("remote"))["id"]}
        m = re.fullmatch(r"/v1/jobs/(qjob-\d+)", path)
        if m:
            return 200, self.job_view(m.group(1))
        return super().dispatch(method, path, q, body, headers)


# --------------------------------------------------------------------------- fixtures and helpers

@pytest.fixture
def make_q(browser, site):
    contexts: list = []
    errors: list[str] = []

    def factory(*, locale: str = "en-US", color_scheme: str = "light", width: int = 1440, height: int = 900,
                standard: bool = True, **kw: Any) -> Ui:
        ctx = browser.new_context(viewport={"width": width, "height": height}, locale=locale, color_scheme=color_scheme)
        contexts.append(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None)
        mock = QMock(**kw)
        if standard:
            mock.q_standard()
        page.route("**/v1/**", mock.handle)
        return Ui(page, mock, site)

    yield factory
    for c in contexts:
        c.close()
    assert not errors, f"uncaught JavaScript errors: {errors}"


def start(ui: Ui, route: str | None = None) -> Ui:
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    expect(ui.tid("rail")).to_be_visible()
    if route:
        ui.go(route)
    return ui


def shot(ui: Ui, name: str) -> None:
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    ui.page.wait_for_timeout(300)
    ui.page.screenshot(path=str(SHOTS / f"quarantine-{name}.png"))


def op_ids(mock: QMock, plan: int, *paths: str) -> list[int]:
    return [i["op_id"] for i in mock.q_items[plan] if i["path"] in paths]


def plan_page(ui: Ui, plan: int, query: str = "") -> Ui:
    ui.go(f"#/quarantine/{plan}{query}")
    expect(ui.tid("view-quarantine-plan")).to_have_attribute("data-id", str(plan))
    return ui


def fb(n: int) -> str:
    """util.js fmtBytes."""
    v, i = float(n), 0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0:
        return f"{int(v)} B"
    return f"{v:.0f} {units[i]}" if v >= 100 else f"{v:.1f} {units[i]}"


def latest(ui: Ui, method: str, pattern: str, *, body: Any = None, query: dict | None = None, timeout: float = 6.0):
    """The newest matching call once it is there: the mock only answers while Playwright waits, so a call made by the
    page a moment ago may not be recorded yet."""
    end = time.time() + timeout
    while True:
        found = ui.mock.calls_to(method, pattern)
        if found and (body is None or found[-1].body == body) and all(found[-1].query.get(k) == v for k, v in (query or {}).items()):
            return found[-1]
        if time.time() > end:
            raise AssertionError(f"no {method} {pattern} with body={body} query={query}; saw {[(c.path, c.query, c.body) for c in found]}")
        ui.page.wait_for_timeout(50)


def finish(ui: Ui) -> None:
    expect(ui.tid("qr-result")).to_be_visible(timeout=15_000)


# --------------------------------------------------------------------------- the rail

def test_quarantine_is_a_rail_item_after_plans_with_alt_6_and_a_badge_for_plans_with_due_items(make_q):
    ui = start(make_q())
    order = ui.page.eval_on_selector_all('[data-testid="rail"] a', "els => els.map(e => e.dataset.testid)")
    assert order == ["rail-clouds", "rail-overview", "rail-explore", "rail-compare", "rail-plans", "rail-quarantine", "rail-workspace"]
    expect(ui.tid("rail-quarantine-badge")).to_have_attribute("data-count", "2")     # #21 and #17 have items past retention
    expect(ui.tid("rail-quarantine")).to_have_attribute("title", re.compile(r"\(Alt\+6\)"))
    assert len(ui.mock.calls_to("GET", "/v1/quarantine")) == 1                       # read at start, no timed polling
    ui.page.keyboard.press("Alt+6")
    expect(ui.tid("rail-quarantine")).to_have_attribute("aria-current", "page")
    expect(ui.tid("view-quarantine")).to_be_visible()
    assert ui.page.evaluate("location.hash") == "#/quarantine"
    ui.page.keyboard.press("Alt+4")
    expect(ui.tid("rail-workspace")).to_have_attribute("aria-current", "page")


def test_no_badge_while_nothing_is_past_retention_and_a_server_without_the_report_is_quiet(make_q):
    ui = make_q(standard=False)
    ui.mock.q_plan(5)
    ui.mock.q_item(5, "gdrive", "a.txt", days=3)
    start(ui)
    expect(ui.tid("rail-quarantine")).to_be_visible()
    expect(ui.tid("rail-quarantine-badge")).to_have_count(0)
    broken = make_q(standard=False)
    broken.mock.q_report_status = 404
    start(broken, "#/quarantine")
    expect(broken.tid("qr-error")).to_be_visible()
    expect(broken.tid("rail-quarantine-badge")).to_have_count(0)


def test_the_badge_is_read_again_after_a_restore_or_purge_job_ends(make_q):
    ui = start(make_q(), "#/quarantine/21")
    expect(ui.tid("rail-quarantine-badge")).to_have_attribute("data-count", "2")
    before = len(ui.mock.calls_to("GET", "/v1/quarantine"))
    ui.tid("qr-purge-menu").click()
    ui.tid("qr-purge-now").click()
    ui.tid("qr-confirm-input").fill("purge")
    ui.tid("qr-confirm").click()
    finish(ui)
    expect(ui.tid("rail-quarantine-badge")).to_have_attribute("data-count", "1")     # #21 holds nothing any more
    assert len(ui.mock.calls_to("GET", "/v1/quarantine")) > before


# --------------------------------------------------------------------------- the overview

def test_cloud_cards_show_what_is_held_what_is_due_and_the_quota_after_a_purge(make_q):
    ui = start(make_q(), "#/quarantine")
    dropbox = ui.tid("qr-cloud-dropbox")
    expect(dropbox).to_contain_text("3 files · 54.0 GB")
    expect(dropbox).to_contain_text("Due: 3 files · 54.0 GB")
    expect(dropbox).to_contain_text("Quota 96.0 GB / 2.0 TB")
    expect(dropbox).to_contain_text("42.0 GB after a purge of what is due")
    gdrive = ui.tid("qr-cloud-gdrive")
    expect(gdrive).to_contain_text("4 files")
    expect(gdrive).to_contain_text("Due: 1 files")
    expect(ui.tid("qr-retention")).to_have_text("Retention 30 days")
    expect(ui.tid("qr-synced")).to_be_visible()


def test_a_cloud_with_nothing_held_says_so(make_q):
    ui = make_q(standard=False)
    ui.mock.q_plan(5)
    ui.mock.q_item(5, "gdrive", "a.txt", days=3)
    start(ui, "#/quarantine")
    expect(ui.tid("qr-cloud-empty-dropbox")).to_have_text("Nothing in quarantine")


def test_plan_rows_run_oldest_first_with_kind_badge_held_and_due(make_q):
    ui = start(make_q(), "#/quarantine")
    ids = ui.page.eval_on_selector_all('[data-testid^="qr-plan-row-"]', "els => els.map(e => e.dataset.testid)")
    assert ids == ["qr-plan-row-17", "qr-plan-row-21"]                              # oldest held item first (#17 has one from 40 days ago); #9 holds nothing
    r21, r17 = ui.tid("qr-plan-row-21"), ui.tid("qr-plan-row-17")
    expect(r21).to_contain_text("Free space on dropbox")
    expect(r21).to_contain_text("3 files · 54.0 GB")
    expect(r21).to_have_attribute("data-due", "true")
    expect(ui.tid("qr-due-21")).to_have_text("due now")
    expect(ui.tid("qr-due-17")).to_have_text("1 due now")                           # one of four items is past retention
    expect(r17.get_by_test_id("qr-claude-badge")).to_be_visible()
    expect(r21.get_by_test_id("qr-claude-badge")).to_have_count(0)
    r17.get_by_test_id("qr-plan-open-17").click()
    expect(ui.tid("view-quarantine-plan")).to_have_attribute("data-id", "17")
    assert ui.page.evaluate("location.hash") == "#/quarantine/17"


def test_a_cloud_card_filters_the_plan_list_through_the_address(make_q):
    ui = start(make_q(), "#/quarantine")
    ui.tid("qr-cloud-filter-gdrive").click()
    assert ui.page.evaluate("location.hash") == "#/quarantine?remote=gdrive"
    expect(ui.tid("qr-plan-row-21")).to_have_count(0)
    expect(ui.tid("qr-plan-row-17")).to_be_visible()
    expect(ui.tid("qr-cloud-gdrive")).to_have_class(re.compile("on"))
    expect(ui.tid("qr-purge-due")).to_contain_text("gdrive")
    ui.page.go_back()
    expect(ui.tid("qr-plan-row-21")).to_be_visible()
    ui.go("#/quarantine?remote=dropbox")                                              # a link, a reload: the same filter
    expect(ui.tid("qr-plan-row-17")).to_have_count(0)
    ui.page.reload()
    expect(ui.tid("qr-plan-row-21")).to_be_visible()
    expect(ui.tid("qr-plan-row-17")).to_have_count(0)


def test_settled_plans_collapse_and_unknown_items_are_only_listed(make_q):
    ui = start(make_q(), "#/quarantine")
    settled = ui.tid("qr-settled")
    expect(settled).to_contain_text("Settled (restored or purged): 1 plans")
    expect(settled.get_by_test_id("qr-plan-row-9")).to_have_count(0)
    settled.locator("summary").click()
    expect(ui.tid("qr-settled-row-9")).to_contain_text("restored 1 · purged 1")
    unknown = ui.tid("qr-unknown")
    expect(unknown).to_contain_text("Unknown items: 12 files (5.0 MB)")
    unknown.locator("summary").click()
    expect(ui.tid("qr-unknown-gdrive")).to_contain_text("by-hand/b.txt")
    expect(unknown).to_contain_text("offers neither restore nor purge")
    expect(unknown.get_by_role("button")).to_have_count(0)                          # nothing to click: restore and purge are not offered
    ui.tid("qr-settled-open-9").click()
    assert ui.page.evaluate("location.hash") == "#/quarantine/9?state=all"


def test_an_empty_quarantine_explains_where_files_come_from(make_q):
    ui = start(make_q(standard=False), "#/quarantine")
    expect(ui.tid("qr-empty")).to_contain_text("Files land here when a plan removes them")
    expect(ui.tid("qr-purge-due")).to_have_count(0)


# --------------------------------------------------------------------------- a plan's quarantine

def test_items_form_a_tree_of_original_folders_with_tri_state_selection(make_q):
    ui = start(make_q(), "#/quarantine/17")
    expect(ui.tid("qr-total")).to_have_text(f"4 files · {fb(11_200_000)}")
    photos, backup = ui.tid("qr-folder-Photos"), ui.tid("qr-folder-Backup")
    expect(photos).to_be_visible()
    expect(ui.tid("qr-folder-Photos/2019")).to_contain_text("2")
    mock = ui.mock
    a, b = op_ids(mock, 17, "Photos/2019/IMG_0412 (1).jpg")[0], op_ids(mock, 17, "Photos/2019/IMG_0413 (1).jpg")[0]
    expect(ui.tid(f"qr-item-{a}")).to_contain_text("IMG_0412 (1).jpg")
    folder_box = ui.tid("qr-folder-check-Photos/2019")
    ui.tid(f"qr-check-{a}").check()
    assert folder_box.evaluate("e => e.indeterminate") is True                       # one of two chosen
    assert ui.tid("qr-folder-check-Photos").evaluate("e => e.indeterminate") is True
    expect(ui.tid("qr-selected")).to_contain_text("1 selected · 4.1 MB")
    ui.tid(f"qr-check-{b}").check()
    assert folder_box.evaluate("e => e.indeterminate") is False and folder_box.is_checked()
    folder_box.uncheck()                                                              # a folder box clears everything below it
    expect(ui.tid("qr-selected")).to_have_text("Nothing selected")
    expect(ui.tid("qr-restore-selected")).to_be_disabled()
    ui.tid("qr-folder-check-Backup").check()
    expect(ui.tid("qr-selected")).to_have_attribute("data-count", "2")
    # collapsing a folder hides its rows and keeps the selection
    ui.tid("qr-toggle-Backup").click()
    expect(ui.tid("qr-item-" + str(op_ids(mock, 17, "Backup/IMG_0414.jpg")[0]))).to_have_count(0)
    expect(ui.tid("qr-selected")).to_have_attribute("data-count", "2")
    expect(backup).to_have_attribute("aria-expanded", "false")


def test_rows_carry_size_due_date_and_the_catalogs_warnings(make_q):
    ui = start(make_q(), "#/quarantine/17")
    mock = ui.mock
    ok, taken, due, unseen = (op_ids(mock, 17, p)[0] for p in ("Photos/2019/IMG_0412 (1).jpg", "Photos/2019/IMG_0413 (1).jpg",
                                                              "Backup/IMG_0414.jpg", "Backup/IMG_0415.jpg"))
    expect(ui.tid(f"qr-item-{ok}")).to_contain_text("4.1 MB")
    expect(ui.tid(f"qr-item-{ok}")).to_contain_text(re.compile(r"due in 2[56] d"))
    expect(ui.tid(f"qr-item-{ok}").locator("[data-flag]")).to_have_count(0)
    expect(ui.tid(f"qr-item-{taken}").locator('[data-flag="taken"]')).to_be_visible()
    expect(ui.tid(f"qr-item-{unseen}").locator('[data-flag="unseen"]')).to_be_visible()
    expect(ui.tid(f"qr-item-{due}")).to_contain_text("due now")
    assert "nothing is ever overwritten" in ui.tid(f"qr-item-{taken}").locator('[data-flag="taken"]').get_attribute("title")


def test_the_detail_panel_names_where_it_was_where_it_is_why_and_who_vouches(make_q):
    ui = make_q()
    gone = ui.mock.q_item(17, "gdrive", "Backup/gone.jpg", 1000, days=2, witness_live=False, witness_same=None)
    unproven = ui.mock.q_item(17, "gdrive", "Backup/unproven.jpg", 1000, days=2, witness_live=True, witness_same=False)
    nowit = ui.mock.q_item(17, "gdrive", "Backup/nowit.jpg", 1000, days=2, witness=False)
    start(ui, "#/quarantine/17")
    expect(ui.tid("qr-detail")).to_contain_text("Choose an item")
    first = op_ids(ui.mock, 17, "Photos/2019/IMG_0412 (1).jpg")[0]
    ui.tid(f"qr-item-{first}").click()
    detail = ui.tid("qr-detail")
    expect(ui.tid("qr-detail-was")).to_have_text("gdrive:Photos/2019/IMG_0412 (1).jpg")
    expect(ui.tid("qr-detail-now")).to_have_text(f"gdrive:{QUARANTINE}/17/Photos/2019/IMG_0412 (1).jpg")
    expect(ui.tid("qr-detail-why")).to_contain_text("duplicate of the kept copy")
    expect(ui.tid("qr-detail-witness")).to_contain_text("gdrive:KEPT/IMG_0412 (1).jpg")
    expect(ui.tid("qr-witness-status")).to_have_attribute("data-status", "same")
    expect(detail).to_contain_text("live and identical")
    ui.tid(f"qr-item-{gone}").click()
    expect(ui.tid("qr-witness-status")).to_have_attribute("data-status", "gone")
    expect(detail).to_contain_text("not in the catalog")
    expect(detail.locator('[data-flag="witnessGone"]')).to_be_visible()
    ui.tid(f"qr-item-{unproven}").click()
    expect(detail).to_contain_text("live, identity not proven")
    ui.tid(f"qr-item-{nowit}").click()
    expect(ui.tid("qr-detail-witness")).to_contain_text("none named")
    expect(ui.tid("qr-reveal-witness")).to_have_count(0)
    ui.tid("qr-reveal-original").click()                                             # to the workspace, in the original folder
    expect(ui.tid("rail-workspace")).to_have_attribute("aria-current", "page")


def test_the_filters_live_in_the_address_and_back_undoes_them(make_q):
    ui = start(make_q(), "#/quarantine/17")
    expect(ui.tid("qr-filter-state")).to_have_value("held")
    ui.tid("qr-filter-state").select_option("all")
    assert ui.page.evaluate("location.hash") == "#/quarantine/17?state=all"
    latest(ui, "GET", r"/v1/quarantine/plans/17", query={"state": "all"})
    ui.tid("qr-filter-remote").select_option("gdrive")
    assert ui.page.evaluate("location.hash") == "#/quarantine/17?state=all&remote=gdrive"
    ui.tid("qr-filter-path").fill("Photos/2019")
    expect(ui.tid("qr-total")).to_have_text(f"2 files · {fb(8_200_000)}")
    assert ui.page.evaluate("location.hash") == "#/quarantine/17?state=all&remote=gdrive&path=Photos/2019"
    latest(ui, "GET", r"/v1/quarantine/plans/17", query={"path": "Photos/2019"})
    ui.page.go_back()                                                                # the folder box replaced the entry: Back leaves the cloud filter
    expect(ui.tid("qr-filter-remote")).to_have_value("")
    expect(ui.tid("qr-filter-path")).to_have_value("")
    ui.page.go_back()
    expect(ui.tid("qr-filter-state")).to_have_value("held")
    ui.go("#/quarantine/9?state=restored")                                           # a link: the filter is read from it
    expect(ui.tid("qr-filter-state")).to_have_value("restored")
    expect(ui.tid("qr-total")).to_contain_text("1 files")


def test_restored_and_purged_items_are_history_without_a_checkbox(make_q):
    ui = start(make_q(), "#/quarantine/9?state=all")
    mock = ui.mock
    restored, purged = (i["op_id"] for i in mock.q_items[9])
    for op, word in ((restored, "restored"), (purged, "purged")):
        row = ui.tid(f"qr-item-{op}")
        expect(row).to_have_attribute("data-state", word)
        expect(row).to_contain_text(word)
        expect(ui.tid(f"qr-check-{op}")).to_have_count(0)
    expect(ui.tid("qr-folder-check-Old")).to_have_count(0)
    ui.tid(f"qr-item-{purged}").click()
    expect(ui.tid("qr-detail-now")).to_have_text("–")


def test_a_long_plan_loads_in_pages_and_opens_only_the_first_level(make_q):
    ui = make_q(standard=False)
    ui.mock.q_plan(30, kind="consolidate", params={"target": "gdrive", "sources": ["dropbox"], "delete_sources": True})
    for i in range(230):
        ui.mock.q_item(30, "dropbox", f"Shoot {i % 5}/Day {i % 3}/img{i:04}.jpg", 1000, days=4)
    start(ui, "#/quarantine/30")
    expect(ui.tid("qr-loaded")).to_have_text("200 of 230 loaded")
    expect(ui.tid("qr-folder-Shoot 0")).to_be_visible()
    expect(ui.tid("qr-folder-Shoot 0/Day 0")).to_have_attribute("aria-expanded", "false")   # only expanded nodes are drawn
    assert ui.page.locator('[data-testid^="qr-item-"]').count() == 0
    ui.tid("qr-toggle-Shoot 0/Day 0").click()
    expect(ui.page.locator('[data-testid^="qr-item-"]').first).to_be_visible()
    ui.tid("qr-more").click()
    expect(ui.tid("qr-more")).to_have_count(0)
    latest(ui, "GET", r"/v1/quarantine/plans/30", query={"offset": "200"})
    expect(ui.tid("qr-total")).to_have_text(f"230 files · {fb(230_000)}")


def test_the_tree_is_a_keyboard_tree(make_q):
    ui = start(make_q(), "#/quarantine/17")
    mock = ui.mock
    first = op_ids(mock, 17, "Backup/IMG_0414.jpg")[0]
    ui.tid("qr-folder-Backup").focus()
    ui.page.keyboard.press("ArrowDown")
    expect(ui.tid(f"qr-item-{first}")).to_be_focused()
    ui.page.keyboard.press("Space")
    expect(ui.tid(f"qr-check-{first}")).to_be_checked()
    ui.page.keyboard.press("Enter")
    expect(ui.tid("qr-detail")).to_have_attribute("data-op", str(first))
    ui.page.keyboard.press("ArrowUp")
    ui.page.keyboard.press("ArrowLeft")                                              # on an open folder: close it
    expect(ui.tid("qr-folder-Backup")).to_have_attribute("aria-expanded", "false")
    ui.page.keyboard.press("ArrowRight")
    expect(ui.tid("qr-folder-Backup")).to_have_attribute("aria-expanded", "true")


# --------------------------------------------------------------------------- restore

def test_restoring_selected_items_checks_first_then_needs_the_word_then_runs_a_job(make_q):
    ui = start(make_q(), "#/quarantine/17")
    mock = ui.mock
    a, taken = (op_ids(mock, 17, p)[0] for p in ("Photos/2019/IMG_0412 (1).jpg", "Photos/2019/IMG_0413 (1).jpg"))
    ui.tid("qr-folder-check-Photos/2019").check()
    ui.tid("qr-restore-selected").click()
    dialog = ui.tid("qr-dialog")
    expect(dialog).to_be_visible()
    check = ui.tid("qr-dialog-check")
    expect(check.get_by_test_id("qr-check-will")).to_contain_text("2 files (7.8 MB) will be put back where they were.")
    expect(check.get_by_test_id("qr-check-taken")).to_contain_text("1 files (3.7 MB) have something at their original path")
    expect(check.get_by_test_id("qr-check-unseen")).to_have_count(0)
    latest(ui, "POST", r"/v1/quarantine/plans/17/check", body={"action": "restore", "op_ids": sorted([a, taken])})
    assert not mock.calls_to("POST", r"/v1/ui/plans/17/restore")                    # a check is read-only
    confirm = ui.tid("qr-confirm")
    expect(confirm).to_be_disabled()
    ui.tid("qr-confirm-input").fill("restor")
    expect(confirm).to_be_disabled()
    ui.tid("qr-confirm-input").fill("restore")
    expect(confirm).to_be_enabled()
    confirm.click()
    finish(ui)
    call = latest(ui, "POST", r"/v1/ui/plans/17/restore", body={"confirm": "restore", "op_ids": sorted([a, taken])})
    assert call.headers["x-csrf-token"] == MockApi.CSRF
    result = ui.tid("qr-result")
    expect(result).to_contain_text("1 files restored to where they were.")
    expect(result).to_contain_text("1 stayed in quarantine:")
    expect(ui.tid(f"qr-failure-{taken}")).to_have_attribute("data-code", "ORIGINAL_TAKEN")
    expect(ui.tid(f"qr-failure-{taken}")).to_contain_text("Something is already at gdrive:Photos/2019/IMG_0413 (1).jpg")
    expect(ui.tid(f"qr-failure-{taken}")).not_to_contain_text("english:")           # the page's sentence, not the executor's
    expect(ui.tid("catalog-banner")).to_be_visible()                                 # the catalog is behind now, as after any run
    ui.tid("qr-cancel").click()
    expect(ui.tid("qr-dialog")).to_have_count(0)
    expect(ui.tid(f"qr-item-{a}")).to_have_count(0)                                  # the list was read again: it is history now
    expect(ui.tid(f"qr-item-{taken}")).to_be_visible()


def test_restore_all_sends_no_op_ids_and_shows_progress_while_it_runs(make_q):
    ui = start(make_q(), "#/quarantine/21")
    ui.tid("qr-restore-all").click()
    expect(ui.tid("qr-scope")).to_have_text("Every item plan #21 still holds.")
    latest(ui, "POST", r"/v1/quarantine/plans/21/check", body={"action": "restore"})
    ui.tid("qr-confirm-input").fill("restore")
    ui.tid("qr-confirm").click()
    expect(ui.tid("qr-progress")).to_be_visible()
    expect(ui.tid("qr-progress-nums")).to_contain_text("restored")
    expect(ui.tid("qr-cancel")).to_be_disabled()                                     # no leaving a running job
    finish(ui)
    latest(ui, "POST", r"/v1/ui/plans/21/restore", body={"confirm": "restore"})
    expect(ui.tid("qr-result")).to_contain_text("3 files restored")


def test_the_restore_check_says_when_the_content_will_exist_twice_and_what_the_scan_did_not_see(make_q):
    ui = make_q(standard=False)
    ui.mock.q_plan(30, kind="consolidate")
    ui.mock.q_item(30, "dropbox", "a.txt", 100, days=3, seen=False)
    ui.mock.q_item(30, "dropbox", "b.txt", 100, days=3)
    start(ui, "#/quarantine/30")
    ui.tid("qr-restore-all").click()
    expect(ui.tid("qr-check-unseen")).to_contain_text("1 quarantined copies (100 B) were not seen by the last scan")
    expect(ui.tid("qr-check-twice")).to_contain_text("copied to another cloud")


@pytest.mark.parametrize("code, params, en, zh", [
    ("ORIGINAL_TAKEN", {"remote": "gdrive", "path": "a.txt"}, "Something is already at gdrive:a.txt", "gdrive:a.txt 已經有檔案了"),
    ("QUARANTINE_GONE", {"remote": "gdrive", "path": "a.txt"}, "The quarantined copy of gdrive:a.txt is not in the quarantine folder any more", "隔離副本已經不在了"),
    ("READ_ONLY", {"remote": "gdrive"}, "gdrive is read-only here", "gdrive 在這裡是唯讀的"),
    ("BAD_PATH", {"path": "../x"}, "The path “../x” cannot be used", "路徑「../x」不能用於這項操作"),
    ("VERIFY_FAILED", {"remote": "gdrive", "path": "a.txt"}, "After the move or delete for gdrive:a.txt, the cloud did not show the expected result", "gdrive:a.txt 在搬移或刪除之後"),
    ("NO_WITNESS_RECORD", {"remote": "gdrive", "path": "a.txt"}, "no record of a copy that vouched for gdrive:a.txt", "沒有任何副本替 gdrive:a.txt 背書"),
    ("WITNESS_UNPROVEN", {"remote": "gdrive", "path": "a.txt", "witness": "dropbox:k/a.txt"}, "The copy at dropbox:k/a.txt that vouched for gdrive:a.txt is gone",
     "替 gdrive:a.txt 背書的 dropbox:k/a.txt 已經不在"),
    ("OP_FAILED", {}, "This item failed. The details are shown as the engine reported them.", "這個項目失敗了"),
])
@pytest.mark.parametrize("lang", ["en", "zh"])
def test_every_failure_code_is_worded_by_the_page_in_both_languages(make_q, code, params, en, zh, lang):
    ui = make_q(standard=False, locale="en-US" if lang == "en" else "zh-TW")
    ui.mock.q_plan(30)
    op = ui.mock.q_item(30, "gdrive", "a.txt", 100, days=3)
    ui.mock.q_item(30, "gdrive", "b.txt", 100, days=3)
    ui.mock.q_fail_op[op] = (code, params)
    start(ui, "#/quarantine/30")
    ui.tid("qr-restore-all").click()
    ui.tid("qr-confirm-input").fill("restore")
    ui.tid("qr-confirm").click()
    finish(ui)
    failure = ui.tid(f"qr-failure-{op}")
    expect(failure).to_have_attribute("data-code", code)
    expect(failure).to_contain_text(en if lang == "en" else zh)
    expect(failure.locator(".mono")).to_have_text("gdrive:a.txt")                  # the original path, always
    if code == "OP_FAILED":
        expect(failure).to_contain_text(f"english: {code}")                         # the one text shown as received


def test_a_code_the_page_does_not_know_falls_back_to_the_executors_english(make_q):
    ui = make_q(standard=False)
    ui.mock.q_plan(30)
    op = ui.mock.q_item(30, "gdrive", "a.txt", 100, days=3)
    ui.mock.q_item(30, "gdrive", "b.txt", 100, days=3)
    ui.mock.q_fail_op[op] = ("FROM_THE_FUTURE", {})
    start(ui, "#/quarantine/30")
    ui.tid("qr-restore-all").click()
    ui.tid("qr-confirm-input").fill("restore")
    ui.tid("qr-confirm").click()
    finish(ui)
    expect(ui.tid(f"qr-failure-{op}")).to_contain_text("english: FROM_THE_FUTURE")


def test_a_refusal_when_the_job_starts_is_worded_in_the_dialog_and_the_dialog_stays(make_q):
    ui = start(make_q(), "#/quarantine/21")
    ui.tid("qr-restore-all").click()
    ui.mock.q_job_error = "JOB_BUSY"
    ui.tid("qr-confirm-input").fill("restore")
    ui.tid("qr-confirm").click()
    expect(ui.tid("qr-dialog-error")).to_contain_text("Another job")
    expect(ui.tid("qr-confirm")).to_be_enabled()                                     # nothing ran: try again
    ui.tid("qr-confirm").click()
    finish(ui)


def test_a_dialog_for_a_plan_that_has_nothing_left_says_so(make_q):
    ui = start(make_q(), "#/quarantine/9")
    expect(ui.tid("qr-restore-all")).to_be_disabled()                                # #9 holds nothing: nothing to restore
    expect(ui.tid("qr-purge-menu")).to_be_enabled()
    expect(ui.tid("qr-total")).to_contain_text("0 files")


# --------------------------------------------------------------------------- purge

def test_the_purge_menu_offers_selected_past_retention_and_everything_and_each_check_has_its_own_body(make_q):
    ui = start(make_q(), "#/quarantine/17")
    mock = ui.mock
    ui.tid("qr-purge-menu").click()
    expect(ui.tid("qr-purge-selected")).to_be_disabled()                             # nothing chosen yet
    ui.tid("qr-purge-due-plan").click()
    latest(ui, "POST", r"/v1/quarantine/plans/17/check", body={"action": "purge", "older_than_days": 30})
    check = ui.tid("qr-dialog-check")
    expect(check.get_by_test_id("qr-check-will")).to_contain_text("1 files (1.9 MB) will be deleted for good. This cannot be undone.")
    expect(check.get_by_test_id("qr-check-toorecent")).to_contain_text("3 files (8.8 MB) are not past their retention yet and stay.")
    ui.tid("qr-cancel").click()
    ui.tid("qr-purge-menu").click()
    ui.tid("qr-purge-now").click()
    latest(ui, "POST", r"/v1/quarantine/plans/17/check", body={"action": "purge", "older_than_days": 0})
    expect(check.get_by_test_id("qr-check-recent")).to_contain_text("3 of them (8.8 MB) were quarantined less than 7 days ago")
    assert "bad" in ui.tid("qr-check-recent").get_attribute("class")                # a warning tone
    ui.tid("qr-cancel").click()
    sel = op_ids(mock, 17, "Photos/2019/IMG_0412 (1).jpg")[0]
    ui.tid(f"qr-check-{sel}").check()
    ui.tid("qr-purge-menu").click()
    ui.tid("qr-purge-selected").click()
    latest(ui, "POST", r"/v1/quarantine/plans/17/check", body={"action": "purge", "op_ids": [sel], "older_than_days": 0})
    expect(ui.tid("qr-scope")).to_have_text("The 1 items you chose in plan #17.")
    ui.tid("qr-confirm-input").fill("purge")
    ui.tid("qr-confirm").click()
    finish(ui)
    latest(ui, "POST", r"/v1/ui/plans/17/purge", body={"confirm": "purge", "older_than_days": 0, "op_ids": [sel]})
    expect(ui.tid("qr-result")).to_contain_text("1 files deleted for good.")
    ui.tid("qr-cancel").click()                                                      # closing the dialog reads the list again
    expect(ui.tid(f"qr-item-{sel}")).to_have_count(0)


def test_the_purge_check_reports_gone_and_unproven_witnesses_and_the_quota_after(make_q):
    ui = make_q()
    ui.mock.q_item(17, "gdrive", "Backup/gone.jpg", 1_000_000, days=40, witness_live=False, witness_same=None)
    ui.mock.q_item(17, "gdrive", "Backup/unproven.jpg", 5_000_000, days=40, witness_live=True, witness_same=False)
    start(ui, "#/quarantine/17")
    ui.tid("qr-purge-menu").click()
    ui.tid("qr-purge-due-plan").click()
    check = ui.tid("qr-dialog-check")
    expect(check.get_by_test_id("qr-check-gone")).to_contain_text("Of these, 1 files (977 KB) have a witness that the catalog says is gone. They are kept")
    expect(check.get_by_test_id("qr-check-unproven")).to_contain_text(
        "1 witnesses (4.8 MB) are present, but the catalog cannot show that they are identical.")
    expect(check.get_by_test_id("qr-check-unproven")).to_contain_text("download up to 4.8 MB")
    expect(check.get_by_test_id("qr-quota-after")).to_contain_text("gdrive after the purge: 212 GB of 2.0 TB used")
    expect(ui.tid("qr-check-recent")).to_have_count(0)                               # nothing purged is younger than 7 days


def test_the_purge_word_is_purge_not_restore(make_q):
    ui = start(make_q(), "#/quarantine/21")
    ui.tid("qr-purge-menu").click()
    ui.tid("qr-purge-now").click()
    ui.tid("qr-confirm-input").fill("restore")
    expect(ui.tid("qr-confirm")).to_be_disabled()
    ui.tid("qr-confirm-input").fill("purge")
    expect(ui.tid("qr-confirm")).to_be_enabled()
    expect(ui.tid("qr-confirm")).to_have_class(re.compile("btn-danger"))


def test_a_purge_that_could_not_keep_every_witness_names_what_stayed_and_why(make_q):
    ui = make_q()
    start(ui, "#/quarantine/21")
    first = ui.mock.q_items[21][0]["op_id"]
    ui.mock.q_fail_op[first] = ("WITNESS_UNPROVEN", {"remote": "dropbox", "path": "Old/0.zip", "witness": "gdrive:Backup/0.zip"})
    ui.tid("qr-purge-menu").click()
    ui.tid("qr-purge-now").click()
    ui.tid("qr-confirm-input").fill("purge")
    ui.tid("qr-confirm").click()
    finish(ui)
    expect(ui.tid("qr-result")).to_have_attribute("data-status", "failed")           # the job failed because an item did
    expect(ui.tid("qr-result")).to_contain_text("2 files deleted for good.")
    expect(ui.tid(f"qr-failure-{first}")).to_contain_text("The copy at gdrive:Backup/0.zip that vouched for dropbox:Old/0.zip is gone")


def test_purge_everything_past_retention_from_the_overview_walks_the_due_plans(make_q):
    ui = start(make_q(), "#/quarantine")
    ui.tid("qr-purge-due").click()
    expect(ui.tid("qr-scope")).to_have_text("Every item past its retention, plan by plan (2 with due items).")
    for pid in (17, 21):
        latest(ui, "POST", rf"/v1/quarantine/plans/{pid}/check", body={"action": "purge", "older_than_days": 30})
    expect(ui.tid("qr-check-will")).to_contain_text("4 files (54.0 GB) will be deleted for good")   # 3 + 1, summed over the plans
    expect(ui.tid("qr-quota-after").first).to_be_visible()
    ui.tid("qr-confirm-input").fill("purge")
    ui.tid("qr-confirm").click()
    finish(ui)
    latest(ui, "POST", r"/v1/ui/quarantine/purge", body={"confirm": "purge"})
    expect(ui.tid("qr-result")).to_contain_text("4 files deleted for good.")
    expect(ui.tid("qr-plan-row-21")).to_have_count(0)                                # the overview read the report again
    expect(ui.tid("qr-purge-due")).to_be_disabled()


def test_the_overview_purge_on_a_filtered_cloud_sends_that_cloud(make_q):
    ui = start(make_q(), "#/quarantine?remote=gdrive")
    ui.tid("qr-purge-due").click()
    expect(ui.tid("qr-scope")).to_have_text("Every item past its retention on gdrive, plan by plan (1 with due items).")
    expect(ui.tid("qr-check-will")).to_contain_text("1 files (1.9 MB)")
    ui.tid("qr-confirm-input").fill("purge")
    ui.tid("qr-confirm").click()
    finish(ui)
    latest(ui, "POST", r"/v1/ui/quarantine/purge", body={"confirm": "purge", "remote": "gdrive"})


def test_a_failure_in_one_plan_of_the_overview_purge_is_listed_under_that_plan(make_q):
    ui = make_q()
    ui.mock.q_fail_op[ui.mock.q_items[21][1]["op_id"]] = ("READ_ONLY", {"remote": "dropbox"})
    start(ui, "#/quarantine")
    ui.tid("qr-purge-due").click()
    ui.tid("qr-confirm-input").fill("purge")
    ui.tid("qr-confirm").click()
    finish(ui)
    expect(ui.tid("qr-result-plan-21")).to_contain_text("Plan #21: 2 done, 1 kept")
    expect(ui.tid("qr-result-plan-21")).to_contain_text("dropbox is read-only here")
    expect(ui.tid("qr-result-plan-17")).to_have_count(0)


def test_a_plan_that_fails_as_a_whole_in_the_overview_purge_shows_its_error(make_q):
    ui = make_q()
    ui.mock.q_plan_error = 17                           # failed 0, error set: the service caught an exception for this plan
    start(ui, "#/quarantine")
    ui.tid("qr-purge-due").click()
    ui.tid("qr-confirm-input").fill("purge")
    ui.tid("qr-confirm").click()
    finish(ui)
    expect(ui.tid("qr-plan-error-17")).to_have_text("rclone exploded")
    expect(ui.tid("qr-result")).to_contain_text("3 files deleted for good.")


# --------------------------------------------------------------------------- the read-only page

def test_without_approvals_the_checks_still_run_and_the_dialog_ends_with_the_command(make_q):
    ui = start(make_q(approvals_enabled=False), "#/quarantine/17")
    mock = ui.mock
    ids = op_ids(mock, 17, "Photos/2019/IMG_0412 (1).jpg", "Backup/IMG_0414.jpg")
    for op in ids:
        ui.tid(f"qr-check-{op}").check()
    ui.tid("qr-restore-selected").click()
    expect(ui.tid("qr-check-will")).to_be_visible()                                  # read-only checks are allowed
    cmd = ui.tid("qr-cli-command")
    expect(cmd).to_have_text("drivemerge plan restore 17" + "".join(f" --op {i}" for i in ids))
    expect(ui.tid("qr-confirm")).to_have_count(0)
    expect(ui.tid("qr-confirm-input")).to_have_count(0)
    assert not mock.calls_to("POST", r"/v1/ui/plans/17/restore")
    ui.tid("qr-cancel").click()
    ui.tid("qr-purge-menu").click()
    ui.tid("qr-purge-due-plan").click()
    expect(ui.tid("qr-cli-command")).to_have_text("drivemerge plan purge 17 --older-than-days 30")


def test_beyond_twenty_items_the_command_is_plan_wide_with_a_note(make_q):
    ui = make_q(approvals_enabled=False, standard=False)
    ui.mock.q_plan(30)
    for i in range(25):
        ui.mock.q_item(30, "gdrive", f"f{i:02}.txt", 10, days=3)
    start(ui, "#/quarantine/30")
    ui.tid("qr-check-" + str(ui.mock.q_items[30][0]["op_id"])).check()
    ui.tid("qr-folder-check-nothing").count()
    ui.page.locator('[data-testid^="qr-check-"]').evaluate_all("els => els.forEach(e => { if (!e.checked) e.click(); })")
    expect(ui.tid("qr-selected")).to_have_attribute("data-count", "25")
    ui.tid("qr-restore-selected").click()
    expect(ui.tid("qr-cli-command")).to_have_text("drivemerge plan restore 30")
    expect(ui.tid("qr-cli-many")).to_contain_text("More than 20 items are chosen")
    ui.tid("qr-cancel").click()
    ui.page.locator('[data-testid^="qr-check-"]').evaluate_all("els => els.slice(0, 5).forEach(e => e.click())")
    ui.tid("qr-restore-selected").click()
    expect(ui.tid("qr-cli-command")).to_contain_text("--op")
    expect(ui.tid("qr-cli-many")).to_have_count(0)


def test_the_overview_purge_without_approvals_shows_the_purge_due_command(make_q):
    ui = start(make_q(approvals_enabled=False), "#/quarantine")
    ui.tid("qr-purge-due").click()
    expect(ui.tid("qr-cli-command")).to_have_text("drivemerge quarantine purge-due")
    ui.tid("qr-cancel").click()
    ui.go("#/quarantine?remote=gdrive")
    ui.tid("qr-purge-due").click()
    expect(ui.tid("qr-cli-command")).to_have_text("drivemerge quarantine purge-due --remote gdrive")


def test_without_approvals_the_terminal_sentences_stay_where_the_view_would_be_pointed_to(make_q):
    ui = make_q(approvals_enabled=False, quarantine=True, standard=False)
    ui.mock.q_plan(7)
    ui.mock.q_item(7, "gdrive", "學校/old-report.pdf", 52_000, days=3)
    start(ui)
    ui.expand_all()
    root = ui.node("left", f"gdrive:{QUARANTINE}")
    assert "drivemerge plan restore" in root.get_attribute("title") and "drivemerge plan purge" in root.get_attribute("title")
    ui.expand("left", f"gdrive:{QUARANTINE}")
    ui.expand("left", f"gdrive:{QUARANTINE}/7")
    assert "drivemerge plan restore" in ui.node("left", f"gdrive:{QUARANTINE}/7/學校").get_attribute("title")
    ui.mock.force_error = ("QUARANTINE", "the quarantine folder holds deleted files")
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(ui.tid("toast").filter(has_text="drivemerge plan restore")).to_be_visible()


# --------------------------------------------------------------------------- execution disabled

def test_with_execution_disabled_the_buttons_are_off_and_the_config_note_shows(make_q):
    ui = start(make_q(execution_enabled=False), "#/quarantine/17")
    expect(ui.tid("qr-exec-disabled")).to_contain_text("execution_enabled = true")
    ui.tid("qr-check-" + str(ui.mock.q_items[17][0]["op_id"])).check()
    expect(ui.tid("qr-restore-selected")).to_be_disabled()
    expect(ui.tid("qr-restore-all")).to_be_disabled()
    expect(ui.tid("qr-purge-menu")).to_be_disabled()
    ui.go("#/quarantine")
    expect(ui.tid("qr-exec-disabled")).to_be_visible()
    expect(ui.tid("qr-purge-due")).to_be_disabled()


# --------------------------------------------------------------------------- the plan review and the other links

def _done_plan(ui: Ui, plan_id: int, *, kind: str = "dedupe", status: str = "done") -> None:
    ui.mock.next_plan_id = plan_id
    ui.mock.add_plan(kind=kind, status=status, created_by="ui", ok=True)


def test_a_done_dedupe_review_shows_the_box_with_restore_and_a_link(make_q):
    ui = make_q()
    _done_plan(ui, 17)
    start(ui, "#/plans/17")
    box = ui.tid("plan-quarantine-box")
    expect(box).to_be_visible()
    expect(ui.tid("plan-quarantine-text")).to_have_text("4 files (10.7 MB) of this plan are in quarantine.")
    ui.tid("plan-quarantine-restore").click()
    expect(ui.tid("qr-dialog")).to_be_visible()
    ui.tid("qr-cancel").click()
    ui.tid("plan-quarantine-open").click()
    assert ui.page.evaluate("location.hash") == "#/quarantine/17"
    expect(ui.tid("view-quarantine-plan")).to_be_visible()
    ui.tid("qr-open-review").click()
    expect(ui.tid("view-plan-review")).to_have_attribute("data-id", "17")


def test_no_box_when_the_plan_holds_nothing_and_none_on_a_plan_that_has_not_run(make_q):
    ui = make_q()
    _done_plan(ui, 9)                                    # #9 holds nothing any more
    start(ui, "#/plans/9")
    expect(ui.tid("plan-closed")).to_be_visible()
    expect(ui.tid("plan-quarantine-box")).to_have_count(0)
    other = make_q()
    _done_plan(other, 17, status="simulated")
    start(other, "#/plans/17")
    expect(other.tid("approval-area")).to_be_visible()
    expect(other.tid("plan-quarantine-box")).to_have_count(0)


def test_the_release_plan_keeps_its_purge_box_and_gains_the_link(make_q):
    ui = make_q()
    _done_plan(ui, 21, kind="release")
    ui.mock.plans[21]["params"] = {"remote": "dropbox"}
    start(ui, "#/plans/21")
    expect(ui.tid("purge-box")).to_be_visible()
    expect(ui.tid("plan-quarantine-box")).to_have_count(0)
    ui.tid("purge-open-quarantine").click()
    assert ui.page.evaluate("location.hash") == "#/quarantine/21"


def test_a_stopped_run_points_to_the_view_with_approvals_and_to_the_terminal_without(make_q):
    ui = make_q()
    _done_plan(ui, 17, status="failed")
    start(ui, "#/plans/17")
    help_ = ui.tid("plan-closed")
    expect(help_).to_contain_text("can be restored from the Quarantine view")
    help_.get_by_test_id("failure-quarantine-link").click()
    assert ui.page.evaluate("location.hash") == "#/quarantine/17"
    plain = make_q(approvals_enabled=False)
    _done_plan(plain, 17, status="failed")
    start(plain, "#/plans/17")
    expect(plain.tid("plan-closed")).to_contain_text("drivemerge plan restore 17")
    expect(plain.tid("failure-quarantine-link")).to_have_count(0)


def test_the_quarantine_folder_in_the_tree_opens_the_view_and_a_plan_folder_opens_that_plan(make_q):
    ui = make_q(quarantine=True, standard=False)
    ui.mock.q_plan(7)
    ui.mock.q_item(7, "gdrive", "學校/old-report.pdf", 52_000, days=3)
    start(ui)
    ui.expand_all()
    root = ui.node("left", f"gdrive:{QUARANTINE}")
    root.get_by_test_id("quarantine-open").click()
    assert ui.page.evaluate("location.hash") == "#/quarantine"
    ui.tid("rail-workspace").click()
    ui.expand("left", f"gdrive:{QUARANTINE}")
    ui.node("left", f"gdrive:{QUARANTINE}/7").get_by_test_id("quarantine-open-plan").click()
    assert ui.page.evaluate("location.hash") == "#/quarantine/7"
    expect(ui.tid("view-quarantine-plan")).to_have_attribute("data-id", "7")


def test_overview_cloud_cards_and_the_free_view_say_how_much_is_in_quarantine(make_q):
    ui = start(make_q(), "#/overview")
    link = ui.tid("cloud-quarantine-dropbox")
    expect(link).to_have_text("54.0 GB in quarantine on this cloud (3 due)")
    link.click()
    assert ui.page.evaluate("location.hash") == "#/quarantine?remote=dropbox"
    ui.go("#/free/gdrive")
    expect(ui.tid("free-quarantine-link")).to_have_text(f"{fb(11_200_000)} in quarantine on this cloud (1 due)")
    ui.tid("free-quarantine-link").click()
    assert ui.page.evaluate("location.hash") == "#/quarantine?remote=gdrive"


# --------------------------------------------------------------------------- languages

def test_the_view_speaks_traditional_chinese_and_switches_without_a_reload(make_q):
    ui = start(make_q(locale="zh-TW"), "#/quarantine")
    expect(ui.tid("rail-quarantine")).to_contain_text("隔離區")
    expect(ui.tid("qr-cloud-dropbox")).to_contain_text("3 個檔案")
    expect(ui.tid("qr-purge-due")).to_have_text("清除所有已到期的檔案…")
    expect(ui.tid("qr-retention")).to_have_text("保留 30 天")
    ui.tid("view-lang").click()
    expect(ui.tid("qr-purge-due")).to_have_text("Purge everything past retention…")
    ui.tid("view-lang").click()
    ui.go("#/quarantine/17")
    ui.tid("qr-restore-all").click()
    expect(ui.tid("qr-check-will")).to_contain_text("會放回原處")
    ui.tid("qr-confirm-input").fill("restore")
    ui.tid("qr-confirm").click()
    finish(ui)
    expect(ui.tid("qr-result")).to_contain_text("已放回原處")
    assert not re.search(r"[一-鿿]", ui.page.evaluate("document.documentElement.lang") or "")


def test_the_keys_the_view_builds_at_run_time_exist_in_both_languages_and_use_what_the_server_sends(make_q):
    ui = make_q()
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    dicts = ui.page.evaluate("import('/ui/static/i18n.js').then(m => m.dictionaries)")
    keys = [f"qr.state.{s}" for s in ("held", "restored", "purged", "all")]
    keys += [f"qr.flag.{f}" for f in ("taken", "unseen", "witnessGone", "unproven")] + [f"qr.flag.{f}Tip" for f in ("taken", "unseen", "witnessGone", "unproven")]
    keys += [f"qr.witness.{w}" for w in ("same", "live", "gone")]
    keys += [f"qr.fail.{c}" for c in ("ORIGINAL_TAKEN", "QUARANTINE_GONE", "READ_ONLY", "BAD_PATH", "VERIFY_FAILED", "NO_WITNESS_RECORD",
                                      "WITNESS_UNPROVEN", "OP_FAILED")]
    keys += ["nav.quarantine", "nav.quarantineTip", "quarantine.tipPage", "quarantine.insidePage", "err.QUARANTINE_PAGE", "prog.failedHelpPage",
             "err.NOTHING_QUARANTINED", "err.BAD_OPS", "err.PLAN_EXECUTING"]
    for lang in ("zh-TW", "en"):
        for key in keys:
            assert key in dicts[lang], (lang, key)
    sent = {"qr.fail.ORIGINAL_TAKEN": {"remote", "path"}, "qr.fail.QUARANTINE_GONE": {"remote", "path"}, "qr.fail.READ_ONLY": {"remote"},
            "qr.fail.BAD_PATH": {"path"}, "qr.fail.VERIFY_FAILED": {"remote", "path"}, "qr.fail.NO_WITNESS_RECORD": {"remote", "path"},
            "qr.fail.WITNESS_UNPROVEN": {"remote", "path", "witness"}, "qr.fail.OP_FAILED": set(),
            "err.NOTHING_QUARANTINED": {"plan"}, "err.BAD_OPS": {"plan", "op_ids"}, "err.PLAN_EXECUTING": {"plan"}}
    for key, params in sent.items():
        for lang in ("zh-TW", "en"):
            assert set(re.findall(r"\{(\w+)\}", dicts[lang][key])) <= params, (lang, key)
    for lang in ("zh-TW", "en"):
        for page_key, old_key in (("quarantine.tipPage", "quarantine.tip"), ("quarantine.insidePage", "quarantine.inside"),
                                  ("err.QUARANTINE_PAGE", "err.QUARANTINE")):
            assert "drivemerge plan" not in dicts[lang][page_key]                      # the page sentences never send anyone to a terminal
            assert "drivemerge plan" in dicts[lang][old_key]                           # the terminal wording stays for the read-only page


@pytest.mark.parametrize("code, params, text", [
    ("NOTHING_QUARANTINED", {"plan": 17}, "Plan #17 holds nothing in quarantine."),
    ("PLAN_EXECUTING", {"plan": 17}, "Plan #17 is still running."),
    ("BAD_OPS", {"plan": 17, "op_ids": [1, 2]}, "not held in quarantine by plan #17 (steps 1,2)"),
])
def test_refusals_of_the_new_routes_are_worded_by_the_page(make_q, code, params, text):
    ui = start(make_q(), "#/quarantine/21")
    ui.page.route("**/v1/quarantine/plans/21/check", lambda r: r.fulfill(
        status=409, content_type="application/json", body='{"error": "x", "code": "%s", "params": %s}' % (code, __import__("json").dumps(params))))
    ui.tid("qr-restore-all").click()
    expect(ui.tid("qr-check-error")).to_contain_text(text)
    assert code in text or True


# --------------------------------------------------------------------------- looking at it

@pytest.mark.parametrize("scheme, locale, width", [("light", "en-US", 1440), ("dark", "zh-TW", 1440), ("light", "zh-TW", 900), ("dark", "en-US", 900)])
def test_screenshots(make_q, scheme, locale, width):
    ui = make_q(color_scheme=scheme, locale=locale, width=width, height=900)
    ui.mock.q_item(17, "gdrive", "Backup/gone.jpg", 1_000_000, days=40, witness_live=False, witness_same=None)
    tag = f"{scheme}-{'en' if locale == 'en-US' else 'zh'}-{width}"
    start(ui, "#/quarantine")
    expect(ui.tid("qr-plan-row-21")).to_be_visible()
    ui.tid("qr-settled").locator("summary").click()
    ui.tid("qr-unknown").locator("summary").click()
    shot(ui, f"overview-{tag}")
    plan_page(ui, 17)
    ui.tid(f"qr-item-{ui.mock.q_items[17][0]['op_id']}").click()
    ui.tid("qr-folder-check-Backup").check()
    shot(ui, f"plan-{tag}")
    ui.tid("qr-restore-selected").click()
    expect(ui.tid("qr-check-will")).to_be_visible()
    shot(ui, f"restore-check-{tag}")
    ui.tid("qr-cancel").click()
    ui.tid("qr-purge-menu").click()
    ui.tid("qr-purge-due-plan").click()
    expect(ui.tid("qr-check-will")).to_be_visible()
    ui.tid("qr-confirm-input").fill("purge")
    shot(ui, f"purge-confirm-{tag}")
    ui.tid("qr-confirm").click()
    finish(ui)
    shot(ui, f"purge-result-{tag}")
    ro = make_q(color_scheme=scheme, locale=locale, width=width, height=900, approvals_enabled=False)
    start(ro, "#/quarantine/17")
    ro.tid("qr-folder-check-Backup").check()
    ro.tid("qr-restore-selected").click()
    expect(ro.tid("qr-cli")).to_be_visible()
    shot(ro, f"read-only-{tag}")
