"""Browser tests for the bulk actions of Explore (docs/BULK.md section G), against a mocked /v1 API.

Same approach as tests/test_ui_frontend.py and tests/test_ui_quarantine.py: the real static files, every /v1 call
answered by a mock through page.route. `BulkMock` adds the two routes of docs/BULK.md section F
(`POST /v1/explore/selection`, `POST /v1/drafts/<id>/gather`), the `draft` parameter of `GET /v1/explore` and the
`selection` parameter of dedupe plans, with the exact JSON shapes of the spec. It keeps a small model, so a flow can be
followed from the checkbox to the staged step and the plan review.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from test_ui_frontend import MockApi, Stage, Ui, _copy, _get, _join, _name, _parent, browser, site  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.ui

SHOTS = Path(os.environ.get("DRIVEMERGE_UI_SHOTS", "/tmp/claude-0/ui-shots"))
PICK_LIMIT = 500
SELECT_LIMIT = 5000
SKIP_CODES = ("ALREADY_THERE", "NAME_TAKEN", "NAME_CLASH", "NOT_ON_TARGET", "READ_ONLY", "NATIVE_DOC", "AMBIGUOUS_PATH", "GONE")

# The catalog of these tests: PDFs spread over two clouds, a few duplicates, and 60 small files to page through.
BULK_CATALOG = [
    {"key": "g:1", "name": "thesis_final.pdf", "size": 4_212_331, "category": "pdf", "ext": "pdf", "mtime": "2023-06-01T10:00:00Z",
     "verified": True, "copies": [("gdrive", "Research/Papers/thesis_final.pdf"), ("dropbox", "School/thesis_final.pdf")]},
    {"key": "g:2", "name": "survey.pdf", "size": 2_000_000, "category": "pdf", "ext": "pdf", "mtime": "2022-11-12T08:00:00Z",
     "verified": True, "copies": [("gdrive", "Downloads/survey.pdf"), ("gdrive", "Research/Papers/survey.pdf")]},
    {"key": "g:3", "name": "paper-a.pdf", "size": 1_000_000, "category": "pdf", "ext": "pdf", "mtime": "2021-08-01T09:00:00Z",
     "verified": True, "copies": [("gdrive", "Desktop/paper-a.pdf")]},
    {"key": "g:4", "name": "hw1.pdf", "size": 500_000, "category": "pdf", "ext": "pdf", "mtime": "2019-01-01T00:00:00Z",
     "verified": True, "copies": [("dropbox", "School/hw1.pdf")]},
    {"key": "g:5", "name": "trip.jpg", "size": 3_000_000, "category": "photo", "ext": "jpg", "mtime": "2024-02-01T12:00:00Z",
     "verified": True, "copies": [("gdrive", "Photos/trip.jpg")]},
]
FILLER = 60
DUP_GROUPS = [
    {"key": "g:2", "size": 2_000_000, "copies": [_copy(401, "gdrive", "Downloads/survey.pdf"), _copy(402, "gdrive", "Research/Papers/survey.pdf")]},
    {"key": "g:5", "size": 3_000_000, "copies": [_copy(403, "gdrive", "Photos/trip.jpg"), _copy(404, "gdrive", "Photos/trip (1).jpg")]},
]


def filler(i: int) -> dict:
    return {"key": f"g:{100 + i}", "name": f"filler-{i:03d}.txt", "size": 1_000 + i, "category": "document", "ext": "txt",
            "mtime": "2020-01-01T00:00:00Z", "verified": True, "copies": [("gdrive", f"Misc/filler-{i:03d}.txt")]}


def _put(tree: dict, remote: str, path: str, size: int) -> None:
    node = tree[remote]
    parts = path.split("/")
    for seg in parts[:-1]:
        node = node.setdefault(seg, {})
    node[parts[-1]] = size


class BulkMock(MockApi):
    """The workspace mock plus the routes and parameters of docs/BULK.md section F."""

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.catalog = copy.deepcopy(BULK_CATALOG) + [filler(i) for i in range(FILLER)]
        self.dup_groups = copy.deepcopy(DUP_GROUPS)
        self.fs = {"gdrive": {"Research": {"Papers": {}}}, "dropbox": {"School": {}}}
        self.native: set[str] = set()                 # contents (keys) that are native cloud documents
        self.ambiguous: set[str] = set()              # contents (keys) with a copy at an ambiguous Drive path
        self.canned_check: dict | None = None         # a canned dry-run answer
        self.selection_fail: tuple[int, str, dict] | None = None
        self.gather_fail: tuple[int, str, dict] | None = None
        self.sync_fs()

    def sync_fs(self) -> None:
        """Every copy of the catalog is a file of the namespace the draft replays on."""
        for c in self.catalog:
            for remote, path in c["copies"]:
                if _get(self.fs, remote, path) is None:
                    _put(self.fs, remote, path, c["size"])

    # ------------------------------------------------------------------ helpers
    def content(self, key: str) -> dict | None:
        return next((c for c in self.catalog if c["key"] == key), None)

    def after(self, remote: str, path: str) -> tuple[str, str]:
        """Where the open draft's active steps put a catalog location."""
        for a in self.draft["actions"]:
            if a["state"] != "active" or a["kind"] != "move":
                continue
            for it in a["items"]:
                if (it["remote"], it["path"]) == (remote, path):
                    remote, path = a["dst"]["remote"], _join(a["dst"]["path"], a.get("new_name") or _name(path))
        return remote, path

    # ------------------------------------------------------------------ explore with staged marks
    def explore(self, q: dict) -> tuple[int, dict]:
        status, data = super().explore(q)
        if status == 200 and q.get("draft"):
            for r in data["results"]:
                n = 0
                for c in r["copies"]:
                    where = self.after(c["remote"], c["path"])
                    moved = where != (c["remote"], c["path"])
                    c["staged"] = {"remote": where[0], "path": where[1]} if moved else None
                    n += moved
                r["staged"] = n
        return status, data

    # ------------------------------------------------------------------ the selection (docs/BULK.md section B)
    def resolve(self, sel: Any, *, with_keys: bool = False, draft: int | None = None) -> tuple[int, dict]:
        def bad(status: int, code: str, **params: Any) -> tuple[int, dict]:
            return status, {"error": code.lower().replace("_", " "), "code": code, "params": params}

        if self.selection_fail:
            status, code, params = self.selection_fail
            return bad(status, code, **params)
        if not isinstance(sel, dict) or ("query" in sel) == ("keys" in sel):
            return bad(400, "BAD_SELECTION", param="selection")
        if "keys" in sel:
            keys = sel["keys"]
            if len(keys) > PICK_LIMIT:
                return bad(400, "TOO_MANY_KEYS", param="keys", count=len(keys), limit=PICK_LIMIT)
            for k in keys:
                if not re.fullmatch(r"(g|e):[1-9][0-9]{0,17}", k):
                    return bad(400, "BAD_KEY", key=k)
            keys = list(dict.fromkeys(keys))
            mode = "keys"
        else:
            query = {k: (",".join(v) if isinstance(v, list) else str(v)) for k, v in sel["query"].items()}
            status, found = super().explore({**query, "limit": "100000", "offset": "0"})
            if status != 200:
                return status, found
            keys = [r["key"] for r in found["results"]]
            except_ = set(sel.get("except") or [])
            if len(except_) > PICK_LIMIT:
                return bad(400, "TOO_MANY_KEYS", param="except", count=len(except_), limit=PICK_LIMIT)
            keys = [k for k in keys if k not in except_]
            if "expect_total" in sel and sel["expect_total"] != len(keys):
                return bad(409, "SELECTION_CHANGED", expected=sel["expect_total"], total=len(keys))
            mode = "query"
        if len(keys) > SELECT_LIMIT:
            return bad(409, "SELECTION_TOO_LARGE", total=len(keys), limit=SELECT_LIMIT)
        contents = [c for c in map(self.content, keys) if c]
        gone = len(keys) - len(contents)
        if not contents:
            return bad(409, "SELECTION_EMPTY")
        names = [r["name"] for r in self.remotes]
        by_remote = []
        for n in names:
            mine = [c for c in contents if any(r == n for r, _ in c["copies"])]
            by_remote.append({"remote": n, "contents": len(mine), "copies": sum(r == n for c in mine for r, _ in c["copies"]),
                              "bytes": sum(c["size"] * sum(r == n for r, _ in c["copies"]) for c in mine)})
        out = {"mode": mode, "total": len(contents), "grouped": sum(c["key"].startswith("g:") for c in contents),
               "ungrouped": sum(c["key"].startswith("e:") for c in contents), "gone": gone,
               "copies": sum(len(c["copies"]) for c in contents), "bytes": sum(c["size"] for c in contents),
               "by_remote": by_remote,
               "redundant": {"copies": sum(len(c["copies"]) - 1 for c in contents),
                             "bytes": sum((len(c["copies"]) - 1) * c["size"] for c in contents)},
               "staged": None}
        if draft is not None:
            out["staged"] = {"copies": sum(self.after(r, p) != (r, p) for c in contents for r, p in c["copies"])}
        if with_keys:
            out["keys"] = sorted(c["key"] for c in contents)
        return 200, out

    # ------------------------------------------------------------------ gather (docs/BULK.md section D)
    def gather(self, draft_id: int, body: dict) -> tuple[int, dict]:
        d = self.draft
        if draft_id != d["id"]:
            return 404, {"error": "no such draft", "code": "NOT_FOUND"}
        if d["status"] != "open":
            return 409, {"error": "draft is closed", "code": "NOT_OPEN", "params": {"draft": d["id"], "status": d["status"]}}
        status, sel = self.resolve(body["selection"], with_keys=True)
        if status != 200:
            return status, sel
        if self.gather_fail:
            code_status, code, params = self.gather_fail
            return code_status, {"error": code, "code": code, "params": params}
        dst, cross = body["dst"], bool(body.get("cross_cloud"))
        if dst["remote"] not in self._remote_names():
            return 404, {"error": "no such remote", "code": "NOT_FOUND", "params": {"remote": dst["remote"]}}
        tree, _, _ = self.replay()
        node = _get(tree, dst["remote"], dst["path"])
        if not isinstance(node, dict):
            return 409, {"error": "not a folder", "code": "DST_NOT_DIR", "params": {"item": {"remote": dst["remote"], "path": dst["path"]}}}
        writable = {r["name"] for r in self.remotes if r.get("writable", True)}
        picked: list[dict] = []
        skipped: list[dict] = []
        taken: dict[str, dict] = {}

        def skip(c: dict, code: str, **params: Any) -> None:
            skipped.append({"key": c["key"], "name": c["name"], "code": code, "params": params})

        for key in sel["keys"]:
            c = self.content(key)
            if c is None:
                continue
            copies = [(r, p, self.after(r, p)) for r, p in c["copies"]]
            if any(w[0] == dst["remote"] and _parent(w[1]) == dst["path"] for _, _, w in copies):
                skip(c, "ALREADY_THERE", name=c["name"])
                continue
            cands = [x for x in copies if x[0] == dst["remote"]]
            if not cands:
                if not cross:
                    skip(c, "NOT_ON_TARGET", remote=dst["remote"])
                    continue
                if key in self.native:
                    skip(c, "NATIVE_DOC")
                    continue
                cands = copies
            ok = [x for x in cands if x[0] in writable]
            if not ok:
                skip(c, "READ_ONLY", remote=cands[0][0])
                continue
            if key in self.ambiguous:
                skip(c, "AMBIGUOUS_PATH", remote=ok[0][0], path=ok[0][2][1])
                continue
            remote, _, where = ok[0]
            name = _name(where[1])
            if name in node:
                skip(c, "NAME_TAKEN", name=name)
                continue
            if name.casefold() in taken:
                skip(c, "NAME_CLASH", name=name, **{"with": taken[name.casefold()]})
                continue
            source = {"remote": where[0], "path": where[1]}
            taken[name.casefold()] = source
            picked.append({"key": key, "from": source, "to": {"remote": dst["remote"], "path": _join(dst["path"], name)},
                           "size": c["size"], "cross_cloud": where[0] != dst["remote"]})
        by_code: dict[str, int] = {}
        for s in skipped:
            by_code[s["code"]] = by_code.get(s["code"], 0) + 1
        crossed = [p for p in picked if p["cross_cloud"]]
        result = {"contents": len(sel["keys"]), "files": len(picked), "bytes": sum(p["size"] for p in picked),
                  "cross_cloud": {"files": len(crossed), "bytes": sum(p["size"] for p in crossed)},
                  "picked": picked[:200], "skipped": {"by_code": by_code, "items": skipped[:200]},
                  "selection": {"total": sel["total"], "gone": sel["gone"]}}
        if self.canned_check is not None and body.get("dry_run"):
            result = self.canned_check
        if body.get("dry_run"):
            return 200, result
        if not picked:
            return 409, {"error": "nothing to gather", "code": "NOTHING_TO_GATHER",
                         "params": {"contents": len(sel["keys"]), "skipped": dict(by_code)}}
        try:
            act, warnings = self._commit({"kind": "move", "items": [p["from"] for p in picked], "dst": dst,
                                          "note": body.get("note") or "explore:gather"}, "ui")
        except Stage as e:
            return 409, {"error": str(e), "code": e.code, **({"params": e.params} if e.params else {})}
        return 201, {**result, "action": act, "draft_version": d["version"], "warnings": warnings}

    # ------------------------------------------------------------------ routing
    def dispatch(self, method: str, path: str, q: dict, body: Any, headers: dict) -> tuple[int, Any]:
        if (method, path) == ("POST", "/v1/explore/selection"):
            return self.resolve(body["selection"], with_keys=bool(body.get("with_keys")), draft=body.get("draft"))
        m = re.fullmatch(r"/v1/drafts/(\d+)/gather", path)
        if m and method == "POST":
            return self.gather(int(m.group(1)), body)
        return super().dispatch(method, path, q, body, headers)

    # ------------------------------------------------------------------ dedupe over a selection (docs/BULK.md section C)
    def estimate(self, kind: str, params: dict) -> tuple[int, dict]:
        sel = params.get("selection")
        if kind != "dedupe" or sel is None:
            return super().estimate(kind, params)
        status, res = self.resolve(sel, with_keys=True)
        if status != 200:
            return status, res
        groups = [k for k in res["keys"] if k.startswith("g:")]
        saved = self.dup_groups
        self.dup_groups = [g for g in saved if g["key"] in groups]
        try:
            status, est = super().estimate(kind, {k: v for k, v in params.items() if k != "selection"})
        finally:
            self.dup_groups = saved
        if status == 200:
            exact = [g for g in saved if g["key"] in groups]
            est["stats"]["selection"] = {"contents": res["total"], "grouped": len(exact), "ungrouped": res["total"] - len(exact), "gone": res["gone"]}
            self.last_groups = [g["key"] for g in exact]
        return status, est

    def plan_routes(self, method: str, path: str, q: dict, body: Any) -> tuple[int, Any] | None:
        out = super().plan_routes(method, path, q, body)
        if out and (method, path) == ("POST", "/v1/plans") and out[0] == 200 and out[1].get("plan_id") and "selection" in body["params"]:
            self.plans[out[1]["plan_id"]]["params"]["groups"] = list(self.last_groups)
        return out

    def summaries(self, q: dict) -> list[dict]:
        rows = super().summaries(q)
        for r in rows:
            if isinstance(r["params"].get("groups"), list):
                r["params"] = {**{k: v for k, v in r["params"].items() if k != "groups"}, "groups_count": len(r["params"]["groups"])}
        return rows


# --------------------------------------------------------------------------- fixtures and helpers

@pytest.fixture
def make_b(browser, site):
    contexts: list = []
    errors: list[str] = []

    def factory(*, locale: str = "en-US", color_scheme: str = "light", width: int = 1440, height: int = 900, **kw: Any) -> Ui:
        ctx = browser.new_context(viewport={"width": width, "height": height}, locale=locale, color_scheme=color_scheme)
        contexts.append(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None)
        mock = BulkMock(**kw)
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


def explore(ui: Ui, query: str = "") -> Ui:
    start(ui, "#/explore" + (f"?q={query}" if query else ""))
    expect(ui.tid("view-explore")).to_be_visible()
    expect(ui.tid("ex-count")).to_contain_text(re.compile(r"\d"))
    return ui


def pick(ui: Ui, key: str, on: bool = True) -> None:
    box = ui.tid(f"ex-select-{key}")
    box.check() if on else box.uncheck()


def last_selection(ui: Ui):
    return ui.mock.last("POST", "/v1/explore/selection")


def expect_selection(ui: Ui, selection: dict, timeout: float = 4.0) -> None:
    """The bar asks after a debounce: wait until the last /v1/explore/selection request carries `selection`."""
    end = time.time() + timeout
    seen = None
    while time.time() < end:
        calls = ui.mock.calls_to("POST", "/v1/explore/selection")
        seen = calls[-1].body["selection"] if calls else None
        if seen == selection:
            return
        ui.page.wait_for_timeout(40)
    raise AssertionError(f"the last selection request was {seen}, expected {selection}")


def hold_first(ui: Ui, route: str, ms: int) -> None:
    """The page's first request to `route` is answered `ms` late (the answer is real, only slow): a newer one overtakes it."""
    ui.page.evaluate("""([route, ms]) => {
        const real = window.fetch; let n = 0;
        window.fetch = (url, opts) => {
            if (!String(url).includes(route)) return real(url, opts);
            n += 1; const mine = n;
            return real(url, opts).then((r) => new Promise((done) => setTimeout(() => done(r), mine === 1 ? ms : 0)));
        };
    }""", [route, ms])


def wait_calls(ui: Ui, method: str, pattern: str, n: int, timeout: float = 4.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if len(ui.mock.calls_to(method, pattern)) >= n:
            return
        ui.page.wait_for_timeout(40)
    raise AssertionError(f"expected {n} {method} {pattern}, saw {len(ui.mock.calls_to(method, pattern))}")


def shot(ui: Ui, name: str) -> None:
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    ui.page.screenshot(path=str(SHOTS / f"bulk-{name}.png"))


# --------------------------------------------------------------------------- the selection model (selection.js)

def test_the_selection_model_in_isolation(make_b):
    ui = start(make_b(), "#/overview")
    out = ui.page.evaluate("""async () => {
        const m = await import('./static/selection.js');
        const s = new m.Selection('type:pdf', { ext: 'pdf', sort: 'size', limit: 50, on: ['gdrive'] });
        const log = {};
        log.empty = [s.empty, s.count, s.signature];
        s.toggle('g:2'); s.toggle('g:1');
        log.keys = s.body();
        log.url = s.toDedupeParams();
        s.selectAllMatching({ ext: 'pdf', on: ['gdrive'] }, 7, 'type:pdf');
        s.toggle('g:3', false);
        log.query = s.body();
        log.queryUrl = s.toDedupeParams();
        const back = m.fromDedupeParams(new URLSearchParams(log.queryUrl), ['gdrive', 'dropbox']);
        log.roundtrip = [back.mode, back.count, [...back.except], back.query, back.text];
        const hand = m.fromDedupeParams(new URLSearchParams({ selk: 'g:1,g:2,bogus', selq: 'type:pdf' }), []);
        log.hand = [hand.mode, [...hand.keys]];
        log.none = [m.fromDedupeParams(new URLSearchParams(''), []), m.fromDedupeParams(new URLSearchParams('seln=x'), [])];
        const lim = new m.Selection('', {});
        let refused = '';
        for (let i = 1; i <= 501; i += 1) { const r = lim.toggle('g:' + i); if (r) refused = r + '@' + i; }
        log.limit = [lim.keys.size, refused];
        const page = new m.Selection('', {});
        log.page = [page.selectPage(Array.from({ length: 501 }, (_, i) => 'g:' + (i + 1))), page.keys.size];
        const ex = new m.Selection('', {});
        ex.selectAllMatching({}, 900, '');
        for (let i = 1; i <= 501; i += 1) ex.toggle('g:' + i, false);
        log.except = [ex.except.size, ex.count];
        ex.useCurrent(950);
        log.current = [ex.seen, ex.body().expect_total];
        return log;
    }""")
    assert out["empty"] == [True, 0, "null"]
    assert out["keys"] == {"keys": ["g:1", "g:2"]}
    assert out["url"] == {"selq": "type:pdf", "selk": "g:1,g:2"}
    assert out["query"] == {"query": {"ext": "pdf", "on": ["gdrive"]}, "except": ["g:3"], "expect_total": 6}
    assert out["queryUrl"] == {"selq": "type:pdf", "selx": "g:3", "seln": "6"}
    assert out["roundtrip"] == ["query", 6, ["g:3"], {"ext": "pdf"}, "type:pdf"]
    assert out["hand"] == ["keys", ["g:1", "g:2"]]
    assert out["none"] == [None, None]
    assert out["limit"] == [PICK_LIMIT, "sel.limit@501"]
    assert out["page"] == ["sel.limit", 0]
    assert out["except"] == [PICK_LIMIT, 400]
    assert out["current"] == [1450, 950]


# --------------------------------------------------------------------------- Explore: checkboxes, page select, all matching

def test_a_result_is_picked_with_its_checkbox_and_the_bar_shows_the_servers_numbers(make_b):
    ui = explore(make_b(), "type:pdf")
    expect(ui.tid("ex-selection-bar")).to_have_count(0)
    pick(ui, "g:2")
    expect(ui.tid("ex-selection-bar")).to_be_visible()
    expect(ui.tid("ex-sel-count")).to_have_text("1 selected")
    expect(ui.tid("ex-sel-copies")).to_have_text("2 copies")
    expect(ui.tid("ex-sel-bytes")).to_have_text("1.9 MB")
    expect(ui.tid("ex-sel-remote-gdrive")).to_contain_text("gdrive 1")
    expect(ui.tid("ex-sel-remote-dropbox")).to_have_count(0)           # a cloud with none of them is not listed
    expect(ui.tid("ex-sel-redundant")).to_have_text("1 redundant copy")
    expect_selection(ui, {"keys": ["g:2"]})
    assert last_selection(ui).body["draft"] == 1
    pick(ui, "g:1")
    expect(ui.tid("ex-sel-count")).to_have_text("2 selected")
    expect(ui.tid("ex-sel-remote-dropbox")).to_contain_text("dropbox 1")
    expect_selection(ui, {"keys": ["g:1", "g:2"]})
    expect(ui.page.locator('[data-testid="ex-result"][data-key="g:1"]')).to_have_attribute("data-selected", "true")
    pick(ui, "g:1", False)
    pick(ui, "g:2", False)
    expect(ui.tid("ex-selection-bar")).to_have_count(0)
    # nothing was asked of the server when nothing is picked
    n = len(ui.mock.calls_to("POST", "/v1/explore/selection"))
    ui.page.wait_for_timeout(400)
    assert len(ui.mock.calls_to("POST", "/v1/explore/selection")) == n


def test_the_bar_is_debounced_and_a_stale_answer_never_replaces_a_newer_one(make_b):
    ui = explore(make_b(), "type:pdf")
    hold_first(ui, "/v1/explore/selection", 1000)
    pick(ui, "g:2")
    wait_calls(ui, "POST", "/v1/explore/selection", 1)       # the first request is out and held back ...
    pick(ui, "g:3")                                          # ... and a newer one overtakes it
    expect(ui.tid("ex-sel-copies")).to_have_text("3 copies")
    ui.page.wait_for_timeout(1300)                           # the old answer has arrived by now and must not replace it
    expect(ui.tid("ex-sel-copies")).to_have_text("3 copies")
    expect(ui.tid("ex-sel-count")).to_have_text("2 selected")
    # typing fast sends one request, not one per click
    n = len(ui.mock.calls_to("POST", "/v1/explore/selection"))
    for key in ("g:1", "g:4"):
        pick(ui, key)
    ui.page.wait_for_timeout(700)
    assert len(ui.mock.calls_to("POST", "/v1/explore/selection")) <= n + 2


def test_the_header_checkbox_picks_the_page_and_all_matching_follows_with_exclusions(make_b):
    ui = explore(make_b(), "filler")
    expect(ui.tid("ex-count")).to_have_text("50 of 60")
    ui.tid("ex-select-page").check()
    expect(ui.page.locator('[data-testid="ex-result"][data-selected="true"]')).to_have_count(50)
    expect(ui.tid("ex-sel-count")).to_have_text("50 selected")
    line = ui.tid("ex-select-all-line")
    expect(line).to_contain_text("All 50 on this page are selected.")
    ui.tid("ex-select-all-matching").click()
    expect(ui.tid("ex-select-all-line")).to_contain_text("All 60 matching are selected.")
    expect(ui.tid("ex-sel-count")).to_have_text("60 selected")
    expect_selection(ui, {"query": {"q": "filler"}, "expect_total": 60})
    pick(ui, "g:105", False)
    pick(ui, "g:107", False)
    expect(ui.tid("ex-sel-count")).to_have_text("58 selected")
    expect_selection(ui, {"query": {"q": "filler"}, "except": ["g:105", "g:107"], "expect_total": 58})
    expect(ui.tid("ex-select-all-line")).to_contain_text("(2 left out)")
    # the rows that load next show checked, apart from the ones left out
    ui.tid("ex-load-more").click()
    expect(ui.tid("ex-count")).to_have_text("60 of 60")
    expect(ui.page.locator('[data-testid="ex-result"][data-selected="true"]')).to_have_count(58)
    expect(ui.tid("ex-select-g:155")).to_be_checked()
    expect(ui.tid("ex-select-g:105")).not_to_be_checked()
    expect(ui.tid("ex-sel-count")).to_have_text("58 selected")
    # the header box takes the exclusions back, and unchecking it ends "all matching"
    ui.tid("ex-select-page").check()
    expect(ui.tid("ex-sel-count")).to_have_text("60 selected")
    ui.tid("ex-select-page").uncheck()
    expect(ui.tid("ex-selection-bar")).to_have_count(0)


def test_the_header_checkbox_is_partial_when_only_some_rows_are_picked(make_b):
    ui = explore(make_b(), "type:pdf")
    pick(ui, "g:3")
    assert ui.tid("ex-select-page").evaluate("e => e.indeterminate") is True
    assert ui.tid("ex-select-page").evaluate("e => e.checked") is False
    ui.tid("ex-select-page").check()
    assert ui.tid("ex-select-page").evaluate("e => e.indeterminate") is False
    # everything is loaded, yet "all matching" is still offered: it ties the action to the query and the total seen
    expect(ui.tid("ex-select-all-line")).to_contain_text("All 4 on this page are selected.")
    ui.tid("ex-select-all-matching").click()
    expect(ui.tid("ex-select-all-line")).to_contain_text("All 4 matching are selected.")
    expect_selection(ui, {"query": {"ext": "pdf"}, "expect_total": 4})


def test_clear_empties_the_selection_and_the_bar_goes(make_b):
    ui = explore(make_b(), "type:pdf")
    ui.tid("ex-select-page").check()
    expect(ui.tid("ex-sel-count")).to_have_text("4 selected")
    ui.tid("ex-sel-clear").click()
    expect(ui.tid("ex-selection-bar")).to_have_count(0)
    expect(ui.page.locator('[data-testid="ex-result"][data-selected="true"]')).to_have_count(0)


def test_a_selection_belongs_to_its_query_and_comes_back_with_it(make_b):
    ui = explore(make_b(), "type:pdf")
    pick(ui, "g:1")
    expect(ui.tid("ex-sel-count")).to_have_text("1 selected")
    expect(ui.tid("ex-sel-cleared")).to_have_count(0)
    ui.tid("explore-input").fill("type:jpg")
    ui.tid("explore-input").press("Enter")
    expect(ui.tid("ex-sel-cleared")).to_be_visible()
    expect(ui.tid("ex-sel-cleared")).to_contain_text("selection was cleared")
    expect(ui.tid("ex-selection-bar")).to_have_count(0)
    expect(ui.tid("ex-result")).to_have_count(1)
    expect(ui.tid("ex-select-g:5")).not_to_be_checked()
    ui.page.go_back()                      # the query comes back, so does what was picked in it
    expect(ui.tid("explore-input")).to_have_value("type:pdf")
    expect(ui.tid("ex-sel-count")).to_have_text("1 selected")
    expect(ui.tid("ex-select-g:1")).to_be_checked()
    expect(ui.tid("ex-sel-cleared")).to_have_count(0)
    # a different sort keeps it: only the order changes
    ui.tid("explore-sort").select_option("size")
    expect(ui.tid("ex-sel-count")).to_have_text("1 selected")


def test_the_fiftieth_after_five_hundred_is_refused_and_says_why(make_b):
    ui = make_b()
    ui.mock.catalog += [filler(i) for i in range(FILLER, 620)]
    ui.mock.sync_fs()
    explore(ui, "filler")
    for _ in range(9):
        ui.tid("ex-load-more").click()
        expect(ui.tid("ex-load-more")).to_be_enabled()
    expect(ui.tid("ex-count")).to_have_text("500 of 620")
    ui.tid("ex-select-page").check()
    expect(ui.tid("ex-sel-count")).to_have_text("500 selected")
    expect(ui.tid("ex-sel-limit")).to_have_count(0)
    ui.tid("ex-load-more").click()
    expect(ui.tid("ex-count")).to_have_text("550 of 620")
    pick_box = ui.tid("ex-select-g:600")
    pick_box.click()
    expect(ui.tid("ex-sel-limit")).to_contain_text("at most 500")
    expect(pick_box).not_to_be_checked()
    expect(ui.tid("ex-sel-count")).to_have_text("500 selected")
    ui.tid("ex-select-g:100").uncheck()     # one makes room
    expect(ui.tid("ex-sel-count")).to_have_text("499 selected")
    expect(ui.tid("ex-sel-limit")).to_have_count(0)
    pick(ui, "g:600")
    expect(ui.tid("ex-sel-count")).to_have_text("500 selected")
    # "all matching" is not bound by the hand-picked limit, only by 5,000
    ui.tid("ex-select-g:601").click()
    expect(ui.tid("ex-sel-limit")).to_be_visible()
    ui.tid("ex-limit-all-matching").click()
    expect(ui.tid("ex-sel-count")).to_have_text("620 selected")


def test_a_refusal_of_the_selection_is_worded_in_the_bar_and_blocks_the_actions(make_b):
    ui = explore(make_b(), "type:pdf")
    ui.mock.selection_fail = (409, "SELECTION_TOO_LARGE", {"total": 6200, "limit": 5000})
    pick(ui, "g:1")
    expect(ui.tid("ex-sel-error")).to_contain_text("The selection holds 6,200 contents, more than the 5,000 one action takes")
    expect(ui.tid("ex-sel-gather")).to_be_disabled()
    expect(ui.tid("ex-sel-dedupe")).to_be_disabled()
    expect(ui.tid("ex-sel-clear")).to_be_enabled()
    ui.mock.selection_fail = None
    pick(ui, "g:2")
    expect(ui.tid("ex-sel-error")).to_have_count(0)
    expect(ui.tid("ex-sel-gather")).to_be_enabled()


def test_the_bar_offers_the_current_count_when_a_rescan_changed_the_answer(make_b):
    ui = explore(make_b(), "filler")
    ui.tid("ex-select-page").check()
    ui.tid("ex-select-all-matching").click()
    expect(ui.tid("ex-sel-count")).to_have_text("60 selected")
    expect_selection(ui, {"query": {"q": "filler"}, "expect_total": 60})
    ui.mock.catalog.append(filler(60))                  # a rescan found one more file that matches
    ui.mock.sync_fs()
    pick(ui, "g:100", False)
    error = ui.tid("ex-sel-error")
    expect(error).to_contain_text("The results changed since you selected: it was 59 contents, now it is 60. Nothing was done.")
    expect(ui.tid("ex-sel-gather")).to_be_disabled()
    ui.tid("ex-sel-update").click()
    expect(error).to_have_count(0)
    expect(ui.tid("ex-sel-count")).to_have_text("60 selected")
    expect_selection(ui, {"query": {"q": "filler"}, "except": ["g:100"], "expect_total": 60})


# --------------------------------------------------------------------------- the gather dialog

def open_gather(ui: Ui, query: str = "type:pdf") -> None:
    explore(ui, query)
    ui.tid("ex-select-page").check()
    expect(ui.tid("ex-sel-gather")).to_be_enabled()
    ui.tid("ex-sel-gather").click()
    expect(ui.tid("gather-dialog")).to_be_visible()


def choose(ui: Ui, remote: str = "gdrive", path: str = "Research/Papers") -> None:
    ui.tid("gather-dst-remote").select_option(remote)
    if path:
        ui.tid("gather-dst-path").fill(path)


def dry_runs(ui: Ui) -> list:
    return [c for c in ui.mock.calls_to("POST", r"/v1/drafts/1/gather") if c.body.get("dry_run")]


ALL4 = {"keys": ["g:1", "g:2", "g:3", "g:4"]}


def test_the_dialog_asks_nothing_until_a_cloud_is_chosen_then_shows_the_dry_run(make_b):
    ui = make_b()
    open_gather(ui)
    expect(ui.tid("gather-dialog")).to_contain_text("Gather 4 contents into a folder")
    expect(ui.tid("gather-dialog")).to_contain_text("Choose the destination cloud first.")
    expect(ui.tid("gather-stage")).to_be_disabled()
    assert dry_runs(ui) == []
    choose(ui)
    expect(ui.tid("gather-check")).to_be_visible()
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "1")
    call = dry_runs(ui)[-1]
    assert call.body == {"selection": {**ALL4}, "dst": {"remote": "gdrive", "path": "Research/Papers"}, "cross_cloud": False, "dry_run": True}
    expect(ui.tid("gather-check")).to_contain_text("1 file (977 KB) moves into gdrive:Research/Papers")
    expect(ui.tid("gather-sample")).to_contain_text("gdrive:Desktop/paper-a.pdf")
    expect(ui.tid("gather-sample")).to_contain_text("gdrive:Research/Papers/paper-a.pdf")
    expect(ui.tid("gather-skipped")).to_contain_text("Skipped 3 contents")
    ui.tid("gather-skipped").locator("summary").click()
    expect(ui.tid("gather-skip-ALREADY_THERE")).to_contain_text("2 already in this folder")
    expect(ui.tid("gather-skip-ALREADY_THERE")).to_contain_text("thesis_final.pdf")
    expect(ui.tid("gather-skip-ALREADY_THERE")).to_contain_text("The folder already holds a copy (thesis_final.pdf).")
    expect(ui.tid("gather-skip-NOT_ON_TARGET")).to_contain_text("hw1.pdf")
    expect(ui.tid("gather-skip-NOT_ON_TARGET")).to_contain_text("No copy on gdrive. Tick the option above")
    expect(ui.tid("gather-cross-warning")).to_have_count(0)
    expect(ui.tid("gather-dialog")).to_contain_text('"Clean up duplicates…" can thin them later')
    expect(ui.tid("gather-stage")).to_be_enabled()
    shot(ui, "gather-en-light")


def test_the_cloud_list_offers_only_clouds_that_can_be_written(make_b):
    ui = make_b()
    ui.mock.remotes.append({"id": 3, "name": "archive", "provider": "drive", "writable": False, "case_insensitive": False})
    open_gather(ui)
    options = ui.tid("gather-dst-remote").locator("option").all_text_contents()
    assert options == ["Choose a cloud…", "gdrive", "dropbox"]


def test_dry_runs_wait_for_typing_to_stop_and_a_slow_answer_never_beats_a_newer_one(make_b):
    ui = make_b()
    open_gather(ui)
    ui.tid("gather-dst-remote").select_option("gdrive")
    wait_calls(ui, "POST", r"/v1/drafts/1/gather", 1)
    before = len(dry_runs(ui))
    ui.tid("gather-dst-path").press_sequentially("Research/Papers", delay=25)
    ui.page.wait_for_timeout(700)
    assert len(dry_runs(ui)) == before + 1            # one request for the whole word, not one per letter
    assert dry_runs(ui)[-1].body["dst"]["path"] == "Research/Papers"
    # the answer for an earlier destination arrives late and must be ignored
    hold_first(ui, "/v1/drafts/1/gather", 1100)
    ui.tid("gather-dst-path").fill("Desktop")
    wait_calls(ui, "POST", r"/v1/drafts/1/gather", before + 2)
    ui.tid("gather-dst-path").fill("Research/Papers")
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "1")
    ui.page.wait_for_timeout(1500)
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "1")
    expect(ui.tid("gather-sample")).to_contain_text("gdrive:Research/Papers/paper-a.pdf")


def test_the_cross_cloud_option_warns_and_brings_the_other_clouds_files(make_b):
    ui = make_b()
    open_gather(ui)
    choose(ui)
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "1")
    expect(ui.tid("gather-cross")).not_to_be_checked()
    ui.tid("gather-cross").check()
    expect(ui.tid("gather-cross-warning")).to_contain_text("copied through this computer and verified first")
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "2")
    assert dry_runs(ui)[-1].body["cross_cloud"] is True
    expect(ui.tid("gather-cross-count")).to_contain_text("1 of them (488 KB) cross clouds.")
    expect(ui.tid("gather-sample").locator('li[data-cross="true"]')).to_contain_text("dropbox:School/hw1.pdf")
    expect(ui.tid("gather-skip-NOT_ON_TARGET")).to_have_count(0)
    ui.tid("gather-cross").uncheck()
    expect(ui.tid("gather-cross-warning")).to_have_count(0)
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "1")


SKIP_ITEMS = [("ALREADY_THERE", {"name": "a.pdf"}), ("NAME_TAKEN", {"name": "b.pdf"}),
              ("NAME_CLASH", {"name": "c.pdf", "with": {"remote": "dropbox", "path": "x/c.pdf"}}),
              ("NOT_ON_TARGET", {"remote": "gdrive"}), ("READ_ONLY", {"remote": "archive"}), ("NATIVE_DOC", {}),
              ("AMBIGUOUS_PATH", {"remote": "gdrive", "path": "Docs/x"}), ("GONE", {})]


def only_skips(mock: BulkMock) -> None:
    """A dry run that moves nothing and skips one content for every skip code."""
    mock.canned_check = {
        "contents": 8, "files": 0, "bytes": 0, "cross_cloud": {"files": 0, "bytes": 0}, "picked": [],
        "skipped": {"by_code": {c: 1 for c, _ in SKIP_ITEMS},
                    "items": [{"key": f"g:{i + 1}", "name": None if c == "GONE" else f"{c.lower()}.pdf", "code": c, "params": p}
                              for i, (c, p) in enumerate(SKIP_ITEMS)]},
        "selection": {"total": 8, "gone": 0}}


def test_every_skip_code_is_worded_from_its_params(make_b):
    ui = make_b()
    only_skips(ui.mock)
    open_gather(ui)
    choose(ui)
    expect(ui.tid("gather-none")).to_have_text("No file can move into this folder.")
    expect(ui.tid("gather-stage")).to_be_disabled()
    ui.tid("gather-skipped").locator("summary").click()
    english = {
        "ALREADY_THERE": "The folder already holds a copy (a.pdf).",
        "NAME_TAKEN": 'The folder already has a different file named "b.pdf"; it is never overwritten or renamed.',
        "NAME_CLASH": 'Another selected content is also called "c.pdf", and the one from dropbox:x/c.pdf goes in first',
        "NOT_ON_TARGET": "No copy on gdrive. Tick the option above to bring it over from another cloud.",
        "READ_ONLY": "archive is read-only, so nothing can move out of it.",
        "NATIVE_DOC": "Native cloud documents (such as Google Docs) cannot move to another cloud.",
        "AMBIGUOUS_PATH": 'gdrive has several items at "Docs/x", so it is not clear which one to move.',
        "GONE": "It is no longer in the catalog since you selected it.",
    }
    expect(ui.tid("gather-skip-GONE")).to_contain_text("g:8")                    # a gone content has no name: its key stands in
    assert set(english) == set(SKIP_CODES)
    for code, text in english.items():
        expect(ui.tid(f"gather-skip-{code}")).to_contain_text(text)


def test_every_skip_code_is_worded_in_chinese_from_its_params(make_b):
    ui = make_b(locale="zh-TW")
    only_skips(ui.mock)
    open_gather(ui)
    choose(ui)
    expect(ui.tid("gather-none")).to_have_text("沒有任何檔案可以搬到這裡。")
    ui.tid("gather-skipped").locator("summary").click()
    for code, _ in SKIP_ITEMS:
        text = ui.tid(f"gather-skip-{code}").inner_text()
        assert re.search("[一-鿿]", text), (code, text)
        assert "{" not in text and "gather." not in text, (code, text)
    expect(ui.tid("gather-skip-NAME_CLASH")).to_contain_text("dropbox:x/c.pdf")
    expect(ui.tid("gather-skip-AMBIGUOUS_PATH")).to_contain_text("Docs/x")
    expect(ui.tid("gather-skip-READ_ONLY")).to_contain_text("archive 是唯讀的")
    shot(ui, "gather-skips-zh-light")


def test_a_refusal_of_the_dry_run_is_worded_in_the_dialog(make_b):
    ui = make_b()
    open_gather(ui)
    choose(ui, path="No/Such/Folder")
    expect(ui.tid("gather-error")).to_have_text("The destination is not a folder.")
    expect(ui.tid("gather-stage")).to_be_disabled()
    ui.mock.gather_fail = (409, "GATHER_TOO_MANY", {"files": 2600, "limit": 2000})
    ui.tid("gather-dst-path").fill("Research/Papers")
    expect(ui.tid("gather-error")).to_have_text("This would move 2,600 files, more than the 2,000 one gather takes. Narrow the selection.")
    expect(ui.tid("gather-error")).to_have_attribute("data-code", "GATHER_TOO_MANY")
    ui.mock.gather_fail = None
    ui.tid("gather-dst-path").fill("Research")
    expect(ui.tid("gather-error")).to_have_count(0)
    expect(ui.tid("gather-check")).to_be_visible()


def test_try_again_asks_the_dry_run_once_more_with_the_same_inputs_after_an_error(make_b):
    ui = make_b()
    open_gather(ui)
    ui.mock.gather_fail = (409, "GATHER_TOO_MANY", {"files": 2600, "limit": 2000})
    choose(ui)
    expect(ui.tid("gather-error")).to_have_attribute("data-code", "GATHER_TOO_MANY")
    expect(ui.tid("gather-retry")).to_have_text("Try again")
    wait_calls(ui, "POST", r"/v1/drafts/1/gather", 1)
    ui.tid("gather-retry").click()
    wait_calls(ui, "POST", r"/v1/drafts/1/gather", 2)
    expect(ui.tid("gather-error")).to_be_visible()          # still refused: still an error, still a way to retry
    ui.mock.gather_fail = None
    ui.tid("gather-retry").click()
    expect(ui.tid("gather-check")).to_be_visible()
    expect(ui.tid("gather-error")).to_have_count(0)
    expect(ui.tid("gather-retry")).to_have_count(0)
    expect(ui.tid("gather-stage")).to_be_enabled()
    runs = dry_runs(ui)
    assert len(runs) == 3
    assert runs[0].body == runs[1].body == runs[2].body     # nothing was changed between the asks
    assert ui.mock.draft["actions"] == []


def test_try_again_is_worded_in_chinese(make_b):
    ui = make_b(locale="zh-TW")
    open_gather(ui)
    ui.mock.gather_fail = (409, "GATHER_TOO_MANY", {"files": 2600, "limit": 2000})
    choose(ui)
    expect(ui.tid("gather-retry")).to_have_text("再試一次")


def test_new_folder_stages_a_mkdir_and_becomes_the_destination(make_b):
    ui = make_b()
    open_gather(ui)
    choose(ui, path="Research")
    expect(ui.tid("gather-check")).to_be_visible()
    ui.tid("gather-new-folder").click()
    ui.tid("dialog-input").fill("Gathered")
    ui.tid("dialog-ok").click()
    expect(ui.tid("gather-dst-path")).to_have_value("Research/Gathered")
    mk = ui.mock.last("POST", r"/v1/drafts/1/actions")
    assert mk.body == {"kind": "mkdir", "dst": {"remote": "gdrive", "path": "Research"}, "new_name": "Gathered"}
    wait_calls(ui, "POST", r"/v1/drafts/1/gather", 2)
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "3")        # the new folder is empty: nothing is "already there"
    assert dry_runs(ui)[-1].body["dst"] == {"remote": "gdrive", "path": "Research/Gathered"}
    expect(ui.tid("gather-new-folder")).to_be_enabled()


def test_staging_sends_the_same_request_without_dry_run_and_leads_to_the_workspace(make_b):
    ui = make_b()
    open_gather(ui)
    choose(ui)
    ui.tid("gather-cross").check()
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "2")
    ui.tid("gather-stage").click()
    expect(ui.tid("gather-dialog")).to_have_count(0)
    call = ui.mock.calls_to("POST", r"/v1/drafts/1/gather")[-1]
    assert call.body == {"selection": ALL4, "dst": {"remote": "gdrive", "path": "Research/Papers"}, "cross_cloud": True}
    action = ui.mock.draft["actions"][0]
    assert action["kind"] == "move" and action["note"] == "explore:gather" and action["created_by"] == "ui"
    assert sorted(i["path"] for i in action["items"]) == ["Desktop/paper-a.pdf", "School/hw1.pdf"]
    toast = ui.page.locator('[data-testid="toast"][data-kind="success"]')
    expect(toast).to_contain_text("Staged: 2 files will move into gdrive:Research/Papers.")
    expect(ui.page.locator('[data-testid="toast"][data-kind="warn"]')).to_contain_text("Cross-cloud move")
    toast.get_by_test_id("toast-action").click()
    expect(ui.tid("workspace-host")).to_be_visible()
    ui.tid("staged-toggle").click()
    expect(ui.tid("staged-item")).to_have_count(1)
    item = ui.tid("staged-item")
    expect(item.locator(".staged-title")).to_have_text("Gather 2 files into gdrive:Research/Papers")
    expect(item.locator(".staged-detail")).to_contain_text("paper-a.pdf")
    expect(item.locator(".staged-note")).to_have_count(0)                    # the note is not shown as free text
    shot(ui, "staged-step-en")


def test_the_staged_step_is_worded_in_chinese_too(make_b):
    ui = make_b(locale="zh-TW")
    open_gather(ui)
    choose(ui)
    ui.tid("gather-stage").click()
    expect(ui.tid("gather-dialog")).to_have_count(0)
    ui.go("#/workspace")
    ui.tid("staged-toggle").click()
    expect(ui.tid("staged-item").locator(".staged-title")).to_have_text("收集 1 個檔案 → gdrive:Research/Papers")


def test_a_gather_refused_while_staging_is_worded_in_the_dialog_and_nothing_is_staged(make_b):
    ui = make_b()
    open_gather(ui)
    choose(ui)
    expect(ui.tid("gather-stage")).to_be_enabled()
    ui.mock.gather_fail = None
    ui.mock.draft["status"] = "open"
    # the catalog changes between the check and the click: the server refuses and the dialog stays
    ui.mock.catalog[2]["copies"] = [("gdrive", "Research/Papers/paper-a.pdf")]
    ui.tid("gather-stage").click()
    expect(ui.tid("gather-error")).to_have_text("None of the 4 selected contents has a file that can move here, so no step was added.")
    expect(ui.tid("gather-dialog")).to_be_visible()
    assert ui.mock.draft["actions"] == []


def test_the_dialog_closes_with_cancel_and_with_escape(make_b):
    ui = make_b()
    open_gather(ui)
    ui.tid("gather-cancel").click()
    expect(ui.tid("gather-dialog")).to_have_count(0)
    ui.tid("ex-sel-gather").click()
    expect(ui.tid("gather-dialog")).to_be_visible()
    expect(ui.page.locator('[data-testid="gather-dialog"] :focus')).to_have_count(1)     # the keyboard is in the dialog
    ui.page.keyboard.press("Escape")
    expect(ui.tid("gather-dialog")).to_have_count(0)


# --------------------------------------------------------------------------- staged marks

def stage_paper_a(ui: Ui) -> None:
    open_gather(ui)
    choose(ui)
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "1")
    ui.tid("gather-stage").click()
    expect(ui.tid("gather-dialog")).to_have_count(0)


def test_a_copy_the_draft_moves_is_marked_where_it_is_going(make_b):
    ui = make_b()
    stage_paper_a(ui)
    expect(ui.tid("ex-staged-badge")).to_have_count(1)
    expect(ui.page.locator('[data-testid="ex-result"][data-key="g:3"] [data-testid="ex-staged-badge"]')).to_have_text("staged")
    expect(ui.tid("ex-sel-staged")).to_have_text("1 copy already staged")
    sent = ui.mock.calls_to("GET", "/v1/explore")
    assert all(c.query.get("draft") == "1" for c in sent)
    assert ui.mock.last("POST", "/v1/explore/selection").body["draft"] == 1
    ui.page.locator('[data-testid="ex-result"][data-key="g:3"] [data-testid="ex-expand"]').click()
    expect(ui.tid("ex-staged")).to_have_text("staged: gdrive:Research/Papers/paper-a.pdf")
    expect(ui.tid("ex-copy")).to_contain_text("gdrive:Desktop/paper-a.pdf")        # the catalog still says where it was seen
    shot(ui, "explore-staged-en-light")
    # undoing the step takes the marks away again
    ui.go("#/workspace")
    ui.tid("staged-toggle").click()
    ui.tid("drawer-undo").click()
    expect(ui.tid("staged-item")).to_have_count(0)
    ui.go("#/explore?q=type:pdf")
    expect(ui.tid("ex-count")).to_have_text("4 of 4")
    expect(ui.tid("ex-staged-badge")).to_have_count(0)


# --------------------------------------------------------------------------- Clean up duplicates

def to_dedupe(ui: Ui, query: str = "type:pdf") -> None:
    explore(ui, query)
    ui.tid("ex-select-page").check()
    expect(ui.tid("ex-sel-redundant")).to_be_visible()
    ui.tid("ex-sel-dedupe").click()
    expect(ui.tid("view-plan-new")).to_be_visible()


def test_clean_up_duplicates_opens_the_dedupe_form_with_the_selection_in_the_address(make_b):
    ui = make_b()
    explore(ui, "type:pdf")
    expect(ui.tid("ex-sel-dedupe")).to_have_count(0)
    pick(ui, "g:3")                                    # one content with one copy: nothing to clean up
    expect(ui.tid("ex-sel-copies")).to_have_text("1 copy")
    expect(ui.tid("ex-sel-dedupe")).to_be_disabled()
    assert "nothing to clean up" in ui.tid("ex-sel-dedupe").get_attribute("title")
    pick(ui, "g:2")
    expect(ui.tid("ex-sel-dedupe")).to_be_enabled()
    ui.tid("ex-sel-dedupe").click()
    expect(ui.tid("view-plan-new")).to_be_visible()
    expect(ui.tid("dd-selection")).to_be_visible()
    expect(ui.tid("dd-selection-count")).to_have_text("2 hand-picked contents from Explore")
    assert unquote(ui.page.evaluate("location.hash")) == "#/plans/new/dedupe?selq=type:pdf&selk=g:2,g:3"
    expect(ui.tid("estimate-headline")).to_be_visible()
    est = ui.mock.last("POST", "/v1/plans/estimate").body
    assert est["kind"] == "dedupe" and est["params"]["cross_remote"] is False
    assert est["params"]["selection"] == {"keys": ["g:2", "g:3"]}
    expect(ui.tid("estimate-headline")).to_contain_text("1 redundant copies in 1 groups")
    expect(ui.tid("dd-selection-stats")).to_contain_text("The selection holds 2 contents. 1 of them have no exact duplicate")
    expect(ui.tid("dd-group")).to_have_count(1)
    # a reload keeps the selection: it is in the address
    ui.page.reload()
    expect(ui.tid("dd-selection")).to_be_visible()
    expect(ui.tid("estimate-headline")).to_be_visible()
    assert ui.mock.last("POST", "/v1/plans/estimate").body["params"]["selection"] == {"keys": ["g:2", "g:3"]}
    shot(ui, "dedupe-selection-en-light")


def test_create_plan_sends_the_selection_and_the_review_words_it(make_b):
    ui = make_b()
    to_dedupe(ui)
    expect(ui.tid("estimate-headline")).to_be_visible()
    ui.tid("create-plan").click()
    expect(ui.tid("view-plan-review")).to_be_visible()
    body = ui.mock.last("POST", "/v1/plans").body
    assert body == {"kind": "dedupe", "params": {"cross_remote": False, "selection": {"keys": ["g:1", "g:2", "g:3", "g:4"]}}}
    expect(ui.tid("view-plan-review").locator(".view-title p")).to_have_text("Dedupe of an Explore selection (1 content)")
    pid = max(ui.mock.plans)
    assert ui.mock.plans[pid]["params"]["groups"] == ["g:2"]
    ui.go("#/plans")
    expect(ui.tid("plan-row").first.locator(".pl-sum")).to_have_text("Dedupe of an Explore selection (1 content)")


def test_the_selection_can_be_taken_off_or_left_for_explore_where_it_is_kept(make_b):
    ui = make_b()
    to_dedupe(ui)
    ui.tid("dd-selection-back").click()
    expect(ui.tid("view-explore")).to_be_visible()
    expect(ui.tid("explore-input")).to_have_value("type:pdf")
    expect(ui.tid("ex-sel-count")).to_have_text("4 selected")
    ui.tid("ex-sel-dedupe").click()
    ui.tid("dd-selection-remove").click()
    expect(ui.tid("dd-selection")).to_have_count(0)
    ui.page.wait_for_function("() => !location.hash.includes('sel')")
    for _ in range(100):
        if "selection" not in ui.mock.last("POST", "/v1/plans/estimate").body["params"]:
            break
        ui.page.wait_for_timeout(40)
    assert "selection" not in ui.mock.last("POST", "/v1/plans/estimate").body["params"]
    expect(ui.tid("estimate-headline")).to_be_visible()
    ui.page.wait_for_timeout(500)
    assert "sel" not in ui.page.evaluate("location.hash")


def test_all_matching_travels_as_a_query_and_a_changed_catalog_is_refused_with_a_way_out(make_b):
    ui = make_b()
    start(ui, "#/plans/new/dedupe?selq=type:pdf&selx=g:4&seln=3")
    expect(ui.tid("dd-selection-count")).to_have_text("3 contents from Explore")
    expect(ui.tid("dd-selection-query")).to_contain_text("type:pdf")
    expect(ui.tid("dd-selection-query")).to_contain_text("(1 left out)")
    expect(ui.tid("estimate-headline")).to_be_visible()
    assert ui.mock.last("POST", "/v1/plans/estimate").body["params"]["selection"] == {
        "query": {"ext": "pdf"}, "except": ["g:4"], "expect_total": 3}
    # a rescan found one more PDF: the same address now answers SELECTION_CHANGED
    ui.mock.catalog.append({**filler(900), "name": "late.pdf", "ext": "pdf", "category": "pdf"})
    ui.mock.sync_fs()
    ui.page.reload()
    changed = ui.tid("dd-selection-changed")
    expect(changed).to_contain_text("The results changed since you selected: it was 3 contents, now it is 4. Nothing was done.")
    expect(ui.tid("estimate-error")).to_have_count(0)
    expect(ui.tid("create-plan")).to_be_disabled()
    ui.tid("dd-selection-update").click()
    expect(changed).to_have_count(0)
    expect(ui.tid("estimate-headline")).to_be_visible()
    assert "seln=4" in unquote(ui.page.evaluate("location.hash"))
    assert ui.mock.last("POST", "/v1/plans/estimate").body["params"]["selection"]["expect_total"] == 4


def test_a_refusal_of_the_selection_in_the_form_is_worded(make_b):
    ui = make_b()
    start(ui, "#/plans/new/dedupe?selq=type:pdf&seln=4")
    expect(ui.tid("estimate-headline")).to_be_visible()
    ui.mock.selection_fail = (409, "SELECTION_TRUNCATED", {"scan_cap": 20000})
    ui.tid("dd-min").fill("1k")
    expect(ui.tid("estimate-error")).to_have_text("More than 20,000 files match, so they cannot all be selected. Narrow the query first.")
    ui.mock.selection_fail = None
    ui.tid("dd-min").fill("")
    expect(ui.tid("estimate-headline")).to_be_visible()


# --------------------------------------------------------------------------- read-only fallback and languages

def test_with_approvals_disabled_everything_works_and_the_review_shows_the_commands(make_b):
    ui = make_b(approvals_enabled=False)
    stage_paper_a(ui)
    expect(ui.page.locator('[data-testid="toast"][data-kind="success"]')).to_contain_text("Staged: 1 file will move into gdrive:Research/Papers.")
    assert ui.mock.draft["actions"][0]["note"] == "explore:gather"
    ui.go("#/explore?q=type:pdf")
    ui.tid("ex-select-page").check()
    ui.tid("ex-sel-dedupe").click()
    expect(ui.tid("dd-selection")).to_be_visible()
    expect(ui.tid("estimate-headline")).to_be_visible()
    ui.tid("create-plan").click()
    expect(ui.tid("view-plan-review")).to_be_visible()
    pid = max(ui.mock.plans)
    expect(ui.tid("approvals-banner")).to_be_visible()
    expect(ui.tid("cli-approve")).to_have_text(f"drivemerge plan approve {pid}")
    expect(ui.tid("cli-execute")).to_have_text(f"drivemerge plan execute {pid}")
    expect(ui.tid("approval-area")).to_have_count(0)


def test_the_bar_the_dialog_and_the_form_are_in_traditional_chinese(make_b):
    ui = make_b(locale="zh-TW")
    explore(ui, "type:pdf")
    ui.tid("ex-select-page").check()
    expect(ui.tid("ex-sel-count")).to_have_text("已選取 4 份內容")
    expect(ui.tid("ex-sel-copies")).to_have_text("6 個副本")
    expect(ui.tid("ex-sel-gather")).to_contain_text("收集到資料夾")
    expect(ui.tid("ex-sel-dedupe")).to_contain_text("清理重複")
    shot(ui, "bar-zh-light")
    ui.tid("ex-sel-gather").click()
    choose(ui)
    expect(ui.tid("gather-check")).to_contain_text("會搬進 gdrive:Research/Papers")
    ui.tid("gather-cross").check()
    expect(ui.tid("gather-cross-warning")).to_contain_text("跨雲搬移")
    shot(ui, "gather-zh-light")
    ui.tid("gather-cancel").click()
    ui.tid("ex-sel-dedupe").click()
    expect(ui.tid("dd-selection-count")).to_have_text("來自 Explore、手動挑選的 4 份內容")
    expect(ui.tid("dd-selection-back")).to_have_text("回到 Explore")
    expect(ui.tid("dd-selection-stats")).to_contain_text("所選範圍有 4 份內容")
    shot(ui, "dedupe-selection-zh-light")
    ui.tid("create-plan").click()
    expect(ui.tid("view-plan-review").locator(".view-title p")).to_have_text("清理 Explore 所選內容的重複副本（1 份內容）")
    expect(ui.page.locator("body")).not_to_contain_text("sel.")


@pytest.mark.parametrize("lang,locale", [("en", "en-US"), ("zh", "zh-TW")])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(1440, 900), (900, 700)])
def test_screenshots(make_b, lang, locale, scheme, width, height):
    ui = make_b(locale=locale, color_scheme=scheme, width=width, height=height)
    explore(ui, "type:pdf")
    ui.tid("ex-select-page").check()
    expect(ui.tid("ex-sel-bytes")).to_be_visible()
    ui.page.locator('[data-testid="ex-result"][data-key="g:2"] [data-testid="ex-expand"]').click()
    ui.page.wait_for_timeout(200)
    shot(ui, f"bar-{lang}-{scheme}-{width}")
    ui.tid("ex-sel-gather").click()
    choose(ui)
    ui.tid("gather-cross").check()
    expect(ui.tid("gather-check")).to_have_attribute("data-files", "2")
    ui.tid("gather-skipped").locator("summary").click()
    ui.page.wait_for_timeout(200)
    shot(ui, f"gather-{lang}-{scheme}-{width}")
    ui.tid("gather-cancel").click()
    ui.tid("ex-sel-dedupe").click()
    expect(ui.tid("estimate-headline")).to_be_visible()
    ui.page.wait_for_timeout(200)
    shot(ui, f"dedupe-{lang}-{scheme}-{width}")


def test_all_matching_with_exclusions_opens_the_form_as_a_query_in_the_address(make_b):
    ui = make_b()
    explore(ui, "type:pdf")
    ui.tid("ex-select-page").check()
    ui.tid("ex-select-all-matching").click()
    pick(ui, "g:4", False)
    expect(ui.tid("ex-sel-count")).to_have_text("3 selected")
    expect_selection(ui, {"query": {"ext": "pdf"}, "except": ["g:4"], "expect_total": 3})
    ui.tid("ex-sel-dedupe").click()
    expect(ui.tid("dd-selection")).to_be_visible()
    assert unquote(ui.page.evaluate("location.hash")) == "#/plans/new/dedupe?selq=type:pdf&selx=g:4&seln=3"
    expect(ui.tid("dd-selection")).to_have_attribute("data-mode", "query")
    expect(ui.tid("dd-selection-back")).to_be_visible()
    ui.tid("dd-selection-back").click()
    expect(ui.tid("ex-sel-count")).to_have_text("3 selected")                 # what was left out is still left out
    expect(ui.tid("ex-select-g:4")).not_to_be_checked()
