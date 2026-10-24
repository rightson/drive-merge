"""Browser tests for connecting, syncing and freeing space (docs/ONBOARDING.md sections 4-6, contract D and E).

Same approach as tests/test_ui_frontend.py: the real static files, a mocked /v1 API through page.route. `OnbMock`
adds the onboarding routes (setup, connect flows, sync job, release report, purge) to the workspace mock and checks
that every human-channel call carries the CSRF header.
"""

from __future__ import annotations

import copy
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from test_ui_frontend import REMOTES, MockApi, Ui, browser, site  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.ui

SHOTS = Path(os.environ.get("DRIVEMERGE_UI_SHOTS", "/tmp/claude-0/ui-shots"))
GB = 1024 ** 3


def _iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


PROVIDERS = [{"key": "drive", "label": "Google Drive", "needs_client": True},
             {"key": "onedrive", "label": "OneDrive", "needs_client": False},
             {"key": "dropbox", "label": "Dropbox", "needs_client": False}]
DEFAULT_NAME = {"drive": "gdrive", "onedrive": "onedrive", "dropbox": "dropbox"}
GOOD_ID = "1234567890-abcdefghijklmnop.apps.googleusercontent.com"
GOOD_SECRET = "GOCSPX-abcdefghijklmnopqrstuv"


def default_report(remote: str = "dropbox") -> dict:
    fresh = {"synced_at": _iso(timedelta(minutes=3)), "stale": False}
    return {
        "remote": remote, "path_prefix": "", "keep_on": None, "writable": True,
        "total": {"files": 27_770, "bytes": 96 * GB},
        "proven": {"files": 18_204, "bytes": 54_700_000_000, "by_remote": {"gdrive": 52_100_000_000, "onedrive": 2_600_000_000}},
        "probable": {"files": 411, "bytes": 3_200_000_000, "entry_ids": [11, 12, 13], "entry_ids_total": 411},
        "only_here": {"files": 9_120, "bytes": 37_900_000_000},
        "unjudged": {"files": 35, "bytes": 200_000_000},
        "elsewhere_unusable": {"files": 0, "bytes": 0},
        "freshness": {remote: dict(fresh), "gdrive": dict(fresh), "onedrive": dict(fresh)},
        "stale": False,
        "quota": {"used": 96 * GB, "total": 2000 * GB, "free": 1904 * GB},
    }


class OnbMock(MockApi):
    """The workspace mock plus the onboarding routes."""

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.remotes = []                 # first run: nothing is configured yet
        self.conns: list[dict] = []
        self.first_run = True
        self.rclone: dict = {"found": True, "path": "/usr/bin/rclone", "version": "1.75.0", "ok": True, "managed": False}
        self.rclone_after_install: dict = dict(self.rclone)
        self.flow_script = ["waiting", "succeeded"]     # the status each poll of a flow returns, the last one repeats
        self.flow_error = "oauth2: cannot fetch token: 400 Bad Request"
        self.start_error: tuple[int, str, str] | None = None     # (status, code, message) for connect/start
        self.finish_error: tuple[int, str, str] | None = None
        self.flows: dict[str, dict] = {}
        self.test_results: dict[str, dict] = {}
        self.sync_stage = 0               # 0 queued, 1 first cloud listing, 2 first done and second listing, 3 finished
        self.sync_fail: set[str] = set()
        self.scan_at: dict[str, str | None] = {}
        self.report_by_remote: dict[str, dict] = {}
        self.verify_polls = 0
        self.purge_polls = 0
        self.install_polls = 0
        self.next_flow = 1

    # ------------------------------------------------------------------ helpers for the tests
    def connect_clouds(self, *names: str, writable: tuple[str, ...] = ("gdrive", "dropbox")) -> None:
        """These clouds are connected (and configured) from the start."""
        self.first_run = False
        for i, n in enumerate(names):
            provider = {"gdrive": "drive", "dropbox": "dropbox", "onedrive": "onedrive"}.get(n, "dropbox")
            self._add_conn(n, provider, n in writable, id_=i + 1)

    def _add_conn(self, name: str, provider: str, writable: bool, id_: int | None = None) -> dict:
        conn = {"name": name, "provider": provider, "account": f"me@{name}.example", "read": True, "write": writable,
                "writable": writable, "created_at": "2025-01-01T00:00:00Z"}
        self.conns.append(conn)
        self.remotes.append({"id": id_ or len(self.remotes) + 1, "name": name, "provider": provider, "writable": writable,
                             "case_insensitive": False})
        return conn

    def report(self, remote: str = "dropbox") -> dict:
        return self.report_by_remote.setdefault(remote, default_report(remote))

    # ------------------------------------------------------------------ the routes
    def dispatch(self, method: str, path: str, q: dict, body: Any, headers: dict) -> tuple[int, Any]:
        if path.startswith("/v1/ui/") and method != "GET" and path != "/v1/ui/session" and headers.get("x-csrf-token") != self.CSRF:
            return 403, {"error": "bad csrf token", "code": "BAD_CSRF"}
        out = self.onboarding(method, path, q, body)
        return out if out is not None else super().dispatch(method, path, q, body, headers)

    def coverage(self, q: dict) -> dict:
        cov = super().coverage(q)
        for r in cov["remotes"]:
            if r["name"] in self.scan_at:
                at = self.scan_at[r["name"]]
                r["last_scan"] = {"id": 5, "status": "done", "finished_at": at} if at else None
        return cov

    def onboarding(self, method: str, path: str, q: dict, body: Any) -> tuple[int, Any] | None:
        body = body or {}
        if (method, path) == ("GET", "/v1/setup"):
            return 200, {"first_run": self.first_run and not self.conns, "config_path": "/home/u/.config/drive-merge/config.toml",
                         "rclone": self.rclone, "connections": copy.deepcopy(self.conns), "providers": PROVIDERS}
        if (method, path) == ("POST", "/v1/ui/connect/start"):
            if self.start_error:
                status, code, message = self.start_error
                return status, {"error": message, "code": code}
            fid = f"flow-{self.next_flow}"
            self.next_flow += 1
            self.flows[fid] = {"provider": body.get("provider"), "purpose": body.get("purpose", "read"), "polls": 0,
                               "cancelled": False, "name": body.get("name")}
            return 200, {"flow_id": fid, "auth_url": f"https://auth.example/{fid}"}
        m = re.fullmatch(r"/v1/ui/connect/flows/(flow-\d+)(/finish|/cancel)?", path)
        if m:
            fid, tail = m.group(1), m.group(2)
            flow = self.flows[fid]
            if tail == "/cancel":
                flow["cancelled"] = True
                return 200, {"ok": True}
            if tail == "/finish":
                if self.finish_error:
                    status, code, message = self.finish_error
                    self.finish_error = None if code in ("NAME_TAKEN", "BAD_NAME") else self.finish_error
                    return status, {"error": message, "code": code}
                if flow["purpose"] == "write":
                    conn = next(c for c in self.conns if c["name"] == body.get("name"))
                    conn.update(write=True, writable=True)
                    next(r for r in self.remotes if r["name"] == conn["name"])["writable"] = True
                    return 200, conn
                name = body.get("name") or DEFAULT_NAME[flow["provider"]]
                return 200, self._add_conn(name, flow["provider"], False)
            flow["polls"] += 1
            if flow["cancelled"]:
                status = "cancelled"
            else:
                status = self.flow_script[min(flow["polls"], len(self.flow_script)) - 1]
            return 200, {"flow_id": fid, "provider": flow["provider"], "purpose": flow["purpose"], "status": status,
                         "auth_url": f"https://auth.example/{fid}", "error": self.flow_error if status == "failed" else None}
        m = re.fullmatch(r"/v1/ui/connections/([a-z0-9-]+)(/test)?", path)
        if m:
            name = m.group(1)
            if method == "DELETE":
                self.conns = [c for c in self.conns if c["name"] != name]
                self.remotes = [r for r in self.remotes if r["name"] != name]
                return 200, {"ok": True}
            return 200, self.test_results.get(name) or {
                "ok": True, "account": f"me@{name}.example", "quota": {"used": 412 * GB, "total": 1000 * GB, "free": 588 * GB}, "error": None}
        if (method, path) == ("POST", "/v1/ui/rclone/install"):
            self.install_polls = 0
            return 202, {"job_id": "job-r1"}
        if path == "/v1/jobs/job-r1":
            self.install_polls += 1
            done = self.install_polls >= 2
            if done:
                self.rclone = dict(self.rclone_after_install)
            return 200, {"id": "job-r1", "kind": "rclone_install", "status": "done" if done else "running", "progress": {}, "result": {}, "error": None}
        if (method, path) == ("POST", "/v1/ui/sync"):
            return 202, {"job_id": "job-s1"}
        if path == "/v1/jobs/job-s1":
            return 200, self.sync_job()
        m = re.fullmatch(r"/v1/release/([a-z0-9-]+)", path)
        if m:
            return 200, copy.deepcopy(self.report(m.group(1)))
        if (method, path) == ("POST", "/v1/ui/bridge"):
            self.verify_polls = 0
            return 202, {"job_id": "job-v1"}
        if path == "/v1/jobs/job-v1":
            self.verify_polls += 1
            done = self.verify_polls >= 3
            if done:
                self.apply_verify()
            return 200, {"id": "job-v1", "kind": "bridge", "status": "done" if done else "running",
                         "progress": {"bridged": min(self.verify_polls, 3), "failed": 0, "total": 3},
                         "result": {"bridged": 3, "failed": [], "groups": {}} if done else None, "error": None}
        if (method, path) == ("POST", "/v1/plans") and body.get("kind") == "release":
            params = body.get("params") or {}
            pid = self.add_plan(kind="release", status="simulated", created_by="ui", ops=18_204, params=params,
                                title=f"free space on {params.get('remote')}")
            return 200, {"plan_id": pid, "ops": 18_204, "status": "simulated", "simulation": {"ok": True, "violations": []}}
        m = re.fullmatch(r"/v1/ui/plans/(\d+)/purge", path)
        if m:
            self.purge_polls = 0
            return 202, {"job_id": "job-p1"}
        if path == "/v1/jobs/job-p1":
            self.purge_polls += 1
            done = self.purge_polls >= 2
            return 200, {"id": "job-p1", "kind": "purge", "status": "done" if done else "running", "progress": {},
                         "result": {"plan_id": 1, "status": "done", "done": 4, "failed": 0, "skipped": 0} if done else None, "error": None}
        return None

    def sync_job(self) -> dict:
        names = [c["name"] for c in self.conns][:3]
        a, b, c = (names + [None, None, None])[:3]
        state = {n: {"state": "queued", "files": 0, "dirs": 0, "bytes": 0} for n in names}
        stage = self.sync_stage
        if stage >= 1 and a:
            state[a] = {"state": "listing", "files": 1_200, "dirs": 30, "bytes": 5 * GB}
        if stage >= 2 and a and b:
            state[a] = {"state": "done", "files": 18_204, "dirs": 400, "bytes": 52 * GB}
            state[b] = {"state": "listing", "files": 3_100, "dirs": 90, "bytes": 8 * GB}
        if stage >= 3:
            for n in names:
                state[n] = {"state": "failed" if n in self.sync_fail else "done", "files": 5_000, "dirs": 90, "bytes": 8 * GB}
            result = {"remotes": {n: {"status": "failed" if n in self.sync_fail else "complete", "files": 5_000, "bytes": 8 * GB, "seconds": 3.2,
                                      "error": "ConnectionError: token expired" if n in self.sync_fail else None} for n in names},
                      "seconds": 9.0, "identity": {}}
            self.apply_sync(names)
            bad = [n for n in names if n in self.sync_fail]
            return {"id": "job-s1", "kind": "sync", "status": "failed" if bad else "done", "progress": {"remotes": state}, "result": result,
                    "error": f"sync did not complete for: {', '.join(bad)}" if bad else None}
        return {"id": "job-s1", "kind": "sync", "status": "running", "progress": {"remotes": state, "remotes_total": len(names)},
                "result": None, "error": None}

    def apply_sync(self, names: list[str]) -> None:
        for n in names:
            if n not in self.sync_fail:
                self.scan_at[n] = _iso()
        for rep in self.report_by_remote.values():
            for n, f in rep["freshness"].items():
                if n in names and n not in self.sync_fail:
                    f.update(synced_at=_iso(), stale=False)
            rep["stale"] = any(f["stale"] for f in rep["freshness"].values())

    def apply_verify(self) -> None:
        for rep in self.report_by_remote.values():
            p = rep["probable"]
            if p["files"]:
                rep["proven"]["files"] += p["files"]
                rep["proven"]["bytes"] += p["bytes"]
                rep["proven"]["by_remote"]["gdrive"] += p["bytes"]
                p.update(files=0, bytes=0, entry_ids=[], entry_ids_total=0)


# --------------------------------------------------------------------------- fixtures and helpers

@pytest.fixture
def make_onb(browser, site):
    contexts: list = []
    errors: list[str] = []

    def factory(*, locale: str = "en-US", color_scheme: str = "light", width: int = 1440, height: int = 900, **kw: Any) -> Ui:
        ctx = browser.new_context(viewport={"width": width, "height": height}, locale=locale, color_scheme=color_scheme)
        contexts.append(ctx)
        ctx.route("https://auth.example/**", lambda r: r.fulfill(status=200, content_type="text/html", body="<p>sign in</p>"))
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None)
        mock = OnbMock(**kw)
        page.route("**/v1/**", mock.handle)
        return Ui(page, mock, site)

    yield factory
    for c in contexts:
        c.close()
    assert not errors, f"uncaught JavaScript errors: {errors}"


def start(ui: Ui, route: str | None = None) -> Ui:
    """Open the page with the session token; `route` is where to go once it has loaded (None: wherever it opens)."""
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
    ui.page.wait_for_timeout(250)
    ui.page.screenshot(path=str(SHOTS / f"onb-{name}.png"))


def calls(ui: Ui, method: str, pattern: str) -> list:
    return ui.mock.calls_to(method, pattern)


def last_body(ui: Ui, method: str, pattern: str) -> Any:
    return ui.mock.last(method, pattern).body


def connect_via_dialog(ui: Ui, provider: str) -> None:
    ui.tid(f"connect-{provider}").click()
    expect(ui.tid("connect-dialog")).to_be_visible()


# --------------------------------------------------------------------------- Welcome and the Clouds view

def test_a_first_run_opens_on_the_welcome_screen_with_clouds_first_in_the_rail(make_onb):
    ui = start(make_onb())
    order = ui.page.eval_on_selector_all('[data-testid="rail"] a', "els => els.map(e => e.dataset.testid)")
    assert order[:2] == ["rail-setup", "rail-clouds"]            # Setup (docs/SETUP.md) leads the rail, Clouds follows
    expect(ui.tid("rail-clouds")).to_have_attribute("aria-current", "page")
    expect(ui.tid("view-clouds")).to_be_visible()
    expect(ui.tid("welcome")).to_contain_text("Welcome to drive-merge")
    expect(ui.tid("welcome")).to_contain_text("connections are read-only")
    for key in ("drive", "onedrive", "dropbox"):
        expect(ui.tid(f"provider-{key}")).to_be_visible()
    expect(ui.tid("provider-drive")).to_contain_text("Needs your own Google client")
    expect(ui.tid("connect-drive")).to_have_text("Set up and connect")
    expect(ui.tid("rclone-chip")).to_contain_text("rclone 1.75.0")
    expect(ui.tid("conn-cards")).to_have_count(0)
    expect(ui.tid("welcome-sync")).to_have_count(0)
    assert ui.page.evaluate("location.hash") == "#/clouds"
    shot(ui, "welcome-en")


def test_the_welcome_screen_in_traditional_chinese(make_onb):
    ui = start(make_onb(locale="zh-TW"))
    expect(ui.tid("welcome")).to_contain_text("歡迎使用 drive-merge")
    expect(ui.tid("connect-drive")).to_have_text("設定並連接")
    expect(ui.tid("rail-clouds")).to_contain_text("雲端")
    shot(ui, "welcome-zh")


def test_with_a_connection_the_page_opens_on_the_workspace_as_before(make_onb):
    mock_ui = make_onb()
    mock_ui.mock.connect_clouds("gdrive", "dropbox")
    start(mock_ui)
    expect(mock_ui.tid("pane-left")).to_be_visible()
    assert mock_ui.page.evaluate("location.hash") == ""


def test_a_server_without_setup_leaves_the_page_as_it_was(make_onb):
    ui = make_onb()
    ui.mock.remotes = copy.deepcopy(REMOTES)
    dispatch = ui.mock.dispatch                        # an older server: it has never heard of /v1/setup
    ui.mock.dispatch = lambda m, p, q, b, h: (404, {"error": "not found"}) if p == "/v1/setup" else dispatch(m, p, q, b, h)
    start(ui)
    expect(ui.tid("pane-left")).to_be_visible()


def test_rclone_missing_offers_install_and_the_page_rechecks(make_onb):
    ui = make_onb()
    ui.mock.rclone = {"found": False, "path": None, "version": None, "ok": False, "managed": False}
    start(ui)
    expect(ui.tid("rclone-banner")).to_have_attribute("data-state", "missing")
    expect(ui.tid("rclone-chip")).to_have_count(0)
    expect(ui.tid("connect-onedrive")).to_be_disabled()
    ui.tid("rclone-install").click()
    expect(ui.tid("rclone-banner")).to_have_count(0, timeout=8000)
    assert calls(ui, "POST", r"/v1/ui/rclone/install")
    expect(ui.tid("rclone-chip")).to_be_visible()
    expect(ui.tid("connect-onedrive")).to_be_enabled()


def test_an_rclone_that_is_too_old_says_so(make_onb):
    ui = make_onb()
    ui.mock.rclone = {"found": True, "path": "/usr/bin/rclone", "version": "1.60.0", "ok": False, "managed": False}
    start(ui)
    expect(ui.tid("rclone-banner")).to_have_attribute("data-state", "old")
    expect(ui.tid("rclone-banner")).to_contain_text("1.60.0")
    expect(ui.tid("rclone-install")).to_be_visible()


# --------------------------------------------------------------------------- connect

def test_connecting_onedrive_opens_the_auth_page_polls_and_shows_the_card(make_onb):
    ui = start(make_onb())
    connect_via_dialog(ui, "onedrive")
    expect(ui.tid("onedrive-text")).to_contain_text("read-only")
    expect(ui.tid("google-guide")).to_have_count(0)
    with ui.page.expect_popup() as popup:
        ui.tid("connect-start").click()
    assert popup.value.url == "https://auth.example/flow-1"
    expect(ui.tid("connect-waiting")).to_be_visible()
    expect(ui.tid("auth-link")).to_have_attribute("href", "https://auth.example/flow-1")
    assert last_body(ui, "POST", r"/v1/ui/connect/start") == {"provider": "onedrive", "purpose": "read"}
    expect(ui.tid("connect-dialog")).to_have_count(0, timeout=8000)
    assert len(calls(ui, "GET", r"/v1/ui/connect/flows/flow-1")) >= 2                        # polled until it succeeded
    assert last_body(ui, "POST", r"/v1/ui/connect/flows/flow-1/finish") == {}
    card = ui.tid("conn-card-onedrive")
    expect(card).to_be_visible()
    expect(card).to_have_attribute("data-state", "ro")
    expect(ui.tid("conn-state-onedrive")).to_contain_text("Connected, read-only")
    expect(ui.tid("conn-account-onedrive")).to_have_text("me@onedrive.example")
    expect(ui.tid("conn-quota-onedrive")).to_contain_text("412 GB of 1000 GB used")
    expect(ui.tid("toast")).to_contain_text("Connected onedrive (me@onedrive.example)")
    expect(ui.tid("welcome")).to_have_count(0)                                              # something is connected now
    expect(ui.tid("welcome-sync")).to_be_visible()
    shot(ui, "connected-one")


def test_a_failed_sign_in_is_explained_by_its_code_and_can_be_retried(make_onb):
    ui = start(make_onb())
    ui.mock.flow_script = ["waiting", "failed"]
    connect_via_dialog(ui, "dropbox")
    ui.tid("connect-start").click()
    err = ui.tid("connect-error")
    expect(err).to_be_visible(timeout=8000)
    expect(err).to_have_attribute("data-code", "FLOW_FAILED")
    expect(err).to_contain_text("The sign-in did not complete.")
    err.get_by_text("Technical details").click()
    expect(err).to_contain_text("oauth2: cannot fetch token")            # the raw text is behind a disclosure
    expect(ui.tid("connect-start")).to_be_enabled()                      # back on the form
    ui.mock.flow_script = ["succeeded"]
    ui.tid("connect-start").click()
    expect(ui.tid("conn-card-dropbox")).to_be_visible(timeout=8000)


@pytest.mark.parametrize("code, status, text", [
    ("FLOW_BUSY", 409, "Another sign-in is still running"),
    ("NO_RCLONE", 409, "rclone is not installed"),
    ("RCLONE_TOO_OLD", 409, "too old"),
    ("NEEDS_CLIENT", 400, "needs your own client ID"),
])
def test_refusals_when_a_sign_in_starts_are_worded_by_the_page(make_onb, code, status, text):
    ui = start(make_onb())
    ui.mock.start_error = (status, code, "english message from the server")
    connect_via_dialog(ui, "onedrive")
    ui.tid("connect-start").click()
    expect(ui.tid("connect-error")).to_have_attribute("data-code", code)
    expect(ui.tid("connect-error")).to_contain_text(text)
    expect(ui.tid("connect-dialog")).to_be_visible()


def test_a_timeout_and_a_failed_test_are_translated(make_onb):
    ui = start(make_onb(locale="zh-TW"))
    ui.mock.flow_script = ["waiting", "timeout"]
    connect_via_dialog(ui, "dropbox")
    ui.tid("connect-start").click()
    expect(ui.tid("connect-error")).to_contain_text("登入等太久已被停止", timeout=8000)
    ui.mock.flow_script = ["succeeded"]
    ui.mock.finish_error = (409, "TEST_FAILED", "lsjson: 401 unauthorized")
    ui.tid("connect-start").click()
    expect(ui.tid("connect-error")).to_have_attribute("data-code", "TEST_FAILED", timeout=8000)
    expect(ui.tid("connect-error")).to_contain_text("測試列出檔案失敗")


def test_cancelling_a_sign_in_cancels_the_flow(make_onb):
    ui = start(make_onb())
    ui.mock.flow_script = ["waiting"]
    connect_via_dialog(ui, "onedrive")
    ui.tid("connect-start").click()
    expect(ui.tid("connect-waiting")).to_be_visible()
    ui.tid("connect-cancel").click()
    expect(ui.tid("connect-waiting")).to_have_count(0)
    expect(ui.tid("connect-start")).to_be_enabled()                      # back on the form, nothing connected
    assert ui.mock.flows["flow-1"]["cancelled"] is True
    assert last_body(ui, "POST", r"/v1/ui/connect/flows/flow-1/cancel") == {}
    expect(ui.tid("conn-card-onedrive")).to_have_count(0)
    # ... and so does closing the dialog while it waits.
    ui.tid("connect-start").click()
    expect(ui.tid("connect-waiting")).to_be_visible()
    ui.page.keyboard.press("Escape")
    expect(ui.tid("connect-dialog")).to_have_count(0)
    ui.page.wait_for_timeout(300)
    assert ui.mock.flows["flow-2"]["cancelled"] is True


def test_a_taken_name_asks_for_another_one_and_finishes_the_same_flow(make_onb):
    ui = start(make_onb())
    ui.mock.finish_error = (409, "NAME_TAKEN", "name taken")
    connect_via_dialog(ui, "dropbox")
    ui.tid("connect-start").click()
    expect(ui.tid("connect-rename")).to_be_visible(timeout=8000)
    expect(ui.tid("connect-error")).to_have_attribute("data-code", "NAME_TAKEN")
    ui.tid("conn-name").fill("Bad Name")
    expect(ui.tid("conn-name-error")).to_be_visible()
    expect(ui.tid("connect-finish")).to_be_disabled()
    ui.tid("conn-name").fill("dropbox-work")
    ui.tid("connect-finish").click()
    expect(ui.tid("conn-card-dropbox-work")).to_be_visible()
    assert last_body(ui, "POST", r"/v1/ui/connect/flows/flow-1/finish") == {"name": "dropbox-work"}


def test_the_google_guide_explains_every_step_and_validates_the_pasted_client(make_onb):
    ui = start(make_onb())
    connect_via_dialog(ui, "drive")
    guide = ui.tid("google-guide")
    expect(guide.locator("li")).to_have_count(5)
    hrefs = ui.page.eval_on_selector_all('[data-testid^="google-link-"]', "els => els.map(e => e.href)")
    assert len(hrefs) == 4 and all(h.startswith("https://console.cloud.google.com/") for h in hrefs)
    assert "projectcreate" in hrefs[0] and "drive.googleapis.com" in hrefs[1] and "consent" in hrefs[2] and "oauthclient" in hrefs[3]
    for link in ui.page.query_selector_all('[data-testid^="google-link-"]'):
        assert link.get_attribute("target") == "_blank" and "noopener" in (link.get_attribute("rel") or "")
    expect(ui.tid("google-step-3")).to_contain_text("External")
    warning = ui.tid("google-testing-warning")
    expect(warning).to_contain_text("In production")
    expect(warning).to_contain_text("7 days")
    shot(ui, "google-dialog-en")

    start_btn = ui.tid("connect-start")
    expect(start_btn).to_be_disabled()
    ui.tid("client-id").fill("not-an-id")
    ui.tid("client-secret").fill("short")
    ui.tid("google-confirm-prod").focus()                                # blur both fields
    expect(ui.tid("client-id-error")).to_contain_text("does not look like a Google client ID")
    expect(ui.tid("client-secret-error")).to_be_visible()
    ui.tid("client-id").fill(GOOD_SECRET)                                 # the secret pasted into the id box
    ui.tid("client-secret").focus()
    expect(ui.tid("client-id-error")).to_contain_text("looks like the client secret")
    ui.tid("client-secret").fill(GOOD_ID)
    ui.tid("google-confirm-prod").focus()
    expect(ui.tid("client-secret-error")).to_contain_text("This is the client ID")
    ui.tid("client-id").fill(GOOD_ID)
    ui.tid("client-secret").fill(GOOD_SECRET)
    expect(ui.tid("client-id-error")).to_have_count(0)
    expect(ui.tid("client-secret-error")).to_have_count(0)
    expect(start_btn).to_be_disabled()                                   # the consent screen must be in production
    ui.tid("google-confirm-prod").check()
    expect(start_btn).to_be_enabled()
    start_btn.click()
    expect(ui.tid("connect-waiting")).to_be_visible()
    assert last_body(ui, "POST", r"/v1/ui/connect/start") == {
        "provider": "drive", "purpose": "read", "client_id": GOOD_ID, "client_secret": GOOD_SECRET}
    expect(ui.tid("conn-card-gdrive")).to_be_visible(timeout=8000)


def test_google_errors_get_a_hint_for_the_usual_mistakes(make_onb):
    ui = start(make_onb())
    ui.mock.flow_script = ["failed"]
    ui.mock.flow_error = "Error 400: invalid_client (The OAuth client was not found)"
    connect_via_dialog(ui, "drive")
    ui.tid("client-id").fill(GOOD_ID)
    ui.tid("client-secret").fill(GOOD_SECRET)
    ui.tid("google-confirm-prod").check()
    ui.tid("connect-start").click()
    expect(ui.tid("connect-hint-badClient")).to_contain_text("Desktop app", timeout=8000)
    ui.mock.flow_error = "access_denied: app is in testing and not verified"
    ui.tid("connect-start").click()
    expect(ui.tid("connect-hint-testing")).to_be_visible(timeout=8000)


def test_the_google_dialog_in_traditional_chinese(make_onb):
    ui = start(make_onb(locale="zh-TW", height=1100))
    connect_via_dialog(ui, "drive")
    expect(ui.tid("google-testing-warning")).to_contain_text("7 天")
    expect(ui.tid("google-step-4")).to_contain_text("桌面應用程式")
    shot(ui, "google-dialog-zh")


def test_dropbox_has_a_one_button_flow_and_a_use_my_own_app_disclosure(make_onb):
    ui = start(make_onb())
    connect_via_dialog(ui, "dropbox")
    expect(ui.tid("dropbox-text")).to_contain_text("one click")
    expect(ui.tid("client-fields")).to_have_count(0)
    expect(ui.tid("connect-start")).to_be_enabled()
    ui.tid("dropbox-own-toggle").click()
    expect(ui.tid("dropbox-own")).to_contain_text("files.metadata.read files.content.read account_info.read")
    expect(ui.tid("client-fields")).to_be_visible()
    expect(ui.tid("connect-start")).to_be_enabled()                      # both empty: rclone's own app
    ui.tid("client-id").fill("abc123")
    expect(ui.tid("connect-start")).to_be_disabled()                     # one without the other
    ui.tid("client-secret").fill("secret-value-1")
    expect(ui.tid("connect-start")).to_be_enabled()
    ui.tid("connect-start").click()
    expect(ui.tid("connect-waiting")).to_be_visible()
    assert last_body(ui, "POST", r"/v1/ui/connect/start") == {
        "provider": "dropbox", "purpose": "read", "client_id": "abc123", "client_secret": "secret-value-1"}


# --------------------------------------------------------------------------- cards

def test_cards_show_state_account_quota_last_sync_and_the_actions(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox", "onedrive", writable=("gdrive",))
    ui.mock.conns[2]["state"] = "relogin"
    ui.mock.test_results["dropbox"] = {"ok": False, "account": None, "quota": None, "error": "lsjson: 401 unauthorized"}
    ui.mock.scan_at = {"gdrive": _iso(timedelta(minutes=3)), "dropbox": _iso(timedelta(days=3)), "onedrive": None}
    start(ui, "#/clouds")
    expect(ui.tid("conn-card-gdrive")).to_have_attribute("data-state", "rw")
    expect(ui.tid("conn-state-gdrive")).to_contain_text("Connected, changes allowed")
    expect(ui.tid("conn-quota-gdrive")).to_contain_text("412 GB of 1000 GB used")
    expect(ui.tid("conn-age-gdrive")).to_contain_text("Synced 3 min ago")
    expect(ui.tid("conn-age-gdrive")).to_have_attribute("data-stale", "false")
    expect(ui.tid("conn-allow-gdrive")).to_have_count(0)                  # changes are allowed already
    expect(ui.tid("conn-card-dropbox")).to_have_attribute("data-state", "error")
    expect(ui.tid("conn-error-dropbox")).to_contain_text("The connection test failed: lsjson: 401 unauthorized")
    expect(ui.tid("conn-age-dropbox")).to_have_attribute("data-stale", "true")
    expect(ui.tid("conn-age-dropbox")).to_contain_text("Synced 3 d ago")
    expect(ui.tid("conn-card-onedrive")).to_have_attribute("data-state", "relogin")
    expect(ui.tid("conn-state-onedrive")).to_contain_text("Needs sign-in again")
    expect(ui.tid("conn-age-onedrive")).to_contain_text("Never synced")
    for name in ("gdrive", "dropbox", "onedrive"):
        for action in ("test", "disconnect", "free"):
            expect(ui.tid(f"conn-{action}-{name}")).to_be_visible()
    assert len(calls(ui, "POST", r"/v1/ui/connections/[a-z-]+/test")) == 3      # tested once when the view opened
    before = len(calls(ui, "POST", r"/v1/ui/connections/gdrive/test"))
    ui.tid("conn-test-gdrive").click()
    expect(ui.tid("conn-quota-gdrive")).to_be_visible()
    assert len(calls(ui, "POST", r"/v1/ui/connections/gdrive/test")) == before + 1
    shot(ui, "connected-cards-en")


def test_the_cards_in_dark_mode_and_chinese(make_onb):
    ui = make_onb(locale="zh-TW", color_scheme="dark")
    ui.mock.connect_clouds("gdrive", "dropbox", "onedrive", writable=("gdrive",))
    ui.mock.scan_at = {"gdrive": _iso(timedelta(minutes=3)), "dropbox": _iso(timedelta(days=3)), "onedrive": None}
    start(ui, "#/clouds")
    expect(ui.tid("conn-state-gdrive")).to_contain_text("已連線（允許變更）")
    expect(ui.tid("conn-age-dropbox")).to_contain_text("天前同步")
    expect(ui.tid("conn-allow-onedrive")).to_contain_text("允許變更")
    shot(ui, "connected-cards-zh-dark")


def test_allow_changes_runs_a_second_sign_in_for_that_connection(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("dropbox", writable=())
    start(ui, "#/clouds")
    ui.tid("conn-allow-dropbox").click()
    expect(ui.tid("allow-text")).to_contain_text("permission to change files on dropbox")
    ui.tid("connect-start").click()
    expect(ui.tid("connect-dialog")).to_have_count(0, timeout=8000)
    assert last_body(ui, "POST", r"/v1/ui/connect/start") == {"provider": "dropbox", "purpose": "write", "name": "dropbox"}
    assert last_body(ui, "POST", r"/v1/ui/connect/flows/flow-1/finish") == {"name": "dropbox"}
    expect(ui.tid("conn-card-dropbox")).to_have_attribute("data-state", "rw")
    expect(ui.tid("conn-allow-dropbox")).to_have_count(0)


def test_disconnect_asks_first_and_removes_the_card(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    start(ui, "#/clouds")
    ui.tid("conn-disconnect-dropbox").click()
    expect(ui.tid("dialog")).to_contain_text("Disconnect dropbox?")
    ui.tid("dialog-cancel").click()
    assert not calls(ui, "DELETE", r"/v1/ui/connections/dropbox")
    ui.tid("conn-disconnect-dropbox").click()
    ui.tid("dialog-ok").click()
    expect(ui.tid("conn-card-dropbox")).to_have_count(0)
    assert calls(ui, "DELETE", r"/v1/ui/connections/dropbox")
    expect(ui.tid("conn-card-gdrive")).to_be_visible()


# --------------------------------------------------------------------------- sync

def test_sync_shows_a_progress_pill_and_a_popover_per_cloud(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox", "onedrive")
    ui.mock.sync_fail = {"onedrive"}
    start(ui, "#/overview")
    expect(ui.tid("sync-pill")).to_have_count(0)
    ui.tid("sync-button").click()
    expect(ui.tid("sync-button")).to_be_disabled()
    pill = ui.tid("sync-pill-text")
    expect(pill).to_have_text("Syncing 0/3 · 0 files")
    assert last_body(ui, "POST", r"/v1/ui/sync") == {}
    ui.mock.sync_stage = 1
    expect(pill).to_have_text("Syncing 0/3 · 1,200 files")
    ui.tid("sync-pill").click()
    expect(ui.tid("sync-popover")).to_be_visible()
    expect(ui.tid("sync-state-gdrive")).to_have_text("Listing")
    expect(ui.tid("sync-state-dropbox")).to_have_text("Waiting")
    expect(ui.tid("sync-files-gdrive")).to_contain_text("1,200 files")
    ui.mock.sync_stage = 2
    expect(pill).to_have_text("Syncing 1/3 · 21,304 files")
    expect(ui.tid("sync-state-gdrive")).to_have_text("Done")
    expect(ui.tid("sync-state-dropbox")).to_have_text("Listing")
    shot(ui, "sync-in-progress")
    ui.mock.sync_stage = 3
    expect(pill).to_have_text("Synced, 1 of 3 failed")
    expect(ui.tid("sync-state-onedrive")).to_have_text("Failed")
    expect(ui.tid("sync-error-onedrive")).to_contain_text("token expired")
    expect(ui.tid("sync-button")).to_be_enabled()
    expect(ui.tid("toast").first).to_contain_text("failed for: onedrive")
    shot(ui, "sync-done-with-failure")


def test_a_clean_sync_ends_with_a_summary_and_refreshes_the_overview(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    ui.mock.scan_at = {"gdrive": _iso(timedelta(days=3)), "dropbox": _iso(timedelta(days=3))}
    start(ui, "#/overview")
    expect(ui.tid("cloud-age-gdrive")).to_have_attribute("data-stale", "true")
    ui.mock.sync_stage = 3
    ui.tid("sync-button").click()
    expect(ui.tid("sync-pill-text")).to_have_text("Synced 2 · 10,000 files")
    expect(ui.tid("sync-pill")).to_have_attribute("data-phase", "done")
    expect(ui.tid("cloud-age-gdrive")).to_have_attribute("data-stale", "false")          # the cards were reloaded
    expect(ui.tid("cloud-age-gdrive")).to_contain_text("Synced just now")


def test_the_workspace_header_has_the_same_sync_button(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    start(ui, "#/workspace")
    expect(ui.tid("sync-button")).to_have_count(1)
    expect(ui.tid("sync-button")).to_be_visible()
    ui.go("#/plans")
    expect(ui.tid("sync-button")).to_have_count(1)                       # never two at once


def test_sync_all_and_continue_goes_to_the_overview(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    ui.mock.sync_stage = 3
    start(ui, "#/clouds")
    ui.tid("welcome-sync").click()
    expect(ui.tid("view-overview")).to_be_visible(timeout=8000)
    assert ui.page.evaluate("location.hash") == "#/overview"


def test_syncing_one_cloud_sends_only_that_cloud(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    ui.mock.sync_stage = 3
    start(ui, "#/clouds")
    ui.tid("conn-sync-dropbox").click()
    expect(ui.tid("sync-pill")).to_be_visible()
    assert last_body(ui, "POST", r"/v1/ui/sync") == {"remotes": ["dropbox"]}


# --------------------------------------------------------------------------- overview and plans links, data age

def test_overview_cards_carry_a_data_age_chip_and_a_link_to_free_space(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    ui.mock.scan_at = {"gdrive": _iso(timedelta(hours=2)), "dropbox": _iso(timedelta(hours=30))}
    start(ui, "#/overview")
    expect(ui.tid("cloud-age-gdrive")).to_have_attribute("data-stale", "false")
    expect(ui.tid("cloud-age-gdrive")).to_contain_text("Synced 2 h ago")
    expect(ui.tid("cloud-age-dropbox")).to_have_attribute("data-stale", "true")      # a warning after 24 hours
    ui.tid("cloud-free-dropbox").click()
    expect(ui.tid("view-free")).to_be_visible()
    assert ui.page.evaluate("location.hash") == "#/free/dropbox"


def test_plans_offers_new_free_space(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    start(ui, "#/plans")
    ui.tid("plans-new-free").click()
    expect(ui.tid("view-free")).to_be_visible()
    assert ui.page.evaluate("location.hash") == "#/free/gdrive"


# --------------------------------------------------------------------------- free space

def free_ui(make_onb, route: str = "#/free/dropbox", **kw: Any) -> Ui:
    ui = make_onb(**kw)
    ui.mock.connect_clouds("gdrive", "dropbox", "onedrive", writable=("gdrive", "dropbox", "onedrive"))
    return start(ui, route)


def test_the_free_space_view_shows_buckets_quota_and_the_projection(make_onb):
    ui = free_ui(make_onb)
    expect(ui.tid("view-free")).to_have_attribute("data-remote", "dropbox")
    proven = ui.tid("bucket-proven")
    expect(proven).to_have_attribute("data-files", "18204")
    expect(ui.tid("bucket-bytes-proven")).to_have_text("50.9 GB")
    expect(ui.tid("bucket-files-proven")).to_have_text("18,204 files")
    expect(ui.tid("proven-by-gdrive")).to_contain_text("48.5 GB")
    expect(ui.tid("proven-by-onedrive")).to_contain_text("2.4 GB")
    expect(ui.tid("bucket-probable")).to_contain_text("Probably elsewhere")
    expect(ui.tid("bucket-only_here")).to_contain_text("Only on dropbox")
    expect(ui.tid("bucket-unjudged")).to_contain_text("Cannot be judged")
    expect(ui.tid("bucket-elsewhere_unusable")).to_have_count(0)                  # empty: not shown
    expect(ui.tid("free-used")).to_have_text("Used 96.0 GB of 2.0 TB")
    expect(ui.tid("free-after")).to_contain_text("After purge: 45.1 GB")
    expect(ui.tid("free-quota-freed")).to_be_visible()
    expect(ui.tid("view-free")).to_contain_text("still count against the quota until it is purged")
    expect(ui.tid("free-create")).to_have_text("Create plan to free 50.9 GB")
    expect(ui.tid("free-stale")).to_have_count(0)
    expect(ui.tid("free-age-dropbox")).to_contain_text("Synced 3 min ago")
    shot(ui, "free-space-en")


def test_the_free_space_view_in_traditional_chinese_and_dark(make_onb):
    ui = free_ui(make_onb, locale="zh-TW", color_scheme="dark")
    expect(ui.tid("bucket-proven")).to_contain_text("已證實別處有副本")
    expect(ui.tid("free-after")).to_contain_text("清除後")
    expect(ui.tid("free-create")).to_contain_text("建立計畫以釋放")
    shot(ui, "free-space-zh-dark")


def test_the_report_is_asked_for_the_folder_and_the_keep_order_in_the_address(make_onb):
    ui = free_ui(make_onb, "#/free/dropbox?path=Photos/2019&keep=onedrive,gdrive")
    expect(ui.tid("bucket-proven")).to_be_visible()
    q = ui.mock.last("GET", r"/v1/release/dropbox").query
    assert q == {"path": "Photos/2019", "keep_on": "onedrive,gdrive"}
    expect(ui.tid("free-path")).to_have_value("Photos/2019")
    items = ui.page.eval_on_selector_all('[data-testid="free-keep"] li', "els => els.map(e => e.dataset.testid)")
    assert items == ["keep-onedrive", "keep-gdrive"]


def test_keep_copies_on_can_be_reordered_and_narrowed(make_onb):
    ui = free_ui(make_onb)
    order = lambda: ui.page.eval_on_selector_all('[data-testid="free-keep"] li', "els => els.map(e => e.dataset.testid)")  # noqa: E731
    expect(ui.tid("free-keep").locator("li")).to_have_count(2)
    assert order() == ["keep-gdrive", "keep-onedrive"]
    assert "keep_on" not in ui.mock.last("GET", r"/v1/release/dropbox").query            # the default asks for nothing special
    ui.tid("keep-down-gdrive").click()
    expect(ui.tid("free-keep").locator("li").first).to_have_attribute("data-testid", "keep-onedrive")
    assert ui.page.evaluate("location.hash") == "#/free/dropbox?keep=onedrive,gdrive"
    ui.page.wait_for_timeout(500)
    assert ui.mock.last("GET", r"/v1/release/dropbox").query["keep_on"] == "onedrive,gdrive"
    ui.tid("keep-check-gdrive").uncheck()
    assert ui.page.evaluate("location.hash") == "#/free/dropbox?keep=onedrive"
    expect(ui.tid("keep-check-onedrive")).to_be_disabled()                               # the last one cannot be turned off


def test_typing_a_folder_updates_the_address_and_asks_again(make_onb):
    ui = free_ui(make_onb)
    ui.tid("free-path").fill("/Backup/2020/")
    expect(ui.tid("free-path")).to_have_value("Backup/2020", timeout=5000)
    assert ui.mock.last("GET", r"/v1/release/dropbox").query["path"] == "Backup/2020"


def test_the_freshness_gate_asks_to_sync_first_and_create_syncs_before_building(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox", "onedrive", writable=("gdrive", "dropbox", "onedrive"))
    rep = ui.mock.report("dropbox")
    rep["freshness"]["gdrive"] = {"synced_at": _iso(timedelta(days=3)), "stale": True}
    rep["freshness"]["onedrive"] = {"synced_at": None, "stale": True}
    rep["stale"] = True
    ui.mock.sync_stage = 3
    start(ui, "#/free/dropbox")
    gate = ui.tid("free-stale")
    expect(gate).to_be_visible()
    expect(ui.tid("free-stale-gdrive")).to_contain_text("3 d ago")
    expect(ui.tid("free-stale-onedrive")).to_contain_text("Never synced")
    expect(ui.tid("free-age-gdrive")).to_have_attribute("data-stale", "true")
    expect(ui.tid("free-create")).to_contain_text("Sync first, then create plan")
    shot(ui, "free-space-stale")
    ui.tid("free-sync-first").click()
    assert last_body(ui, "POST", r"/v1/ui/sync") == {"remotes": ["gdrive", "onedrive"]}
    expect(gate).to_have_count(0, timeout=8000)                                           # the report was fetched again
    expect(ui.tid("free-create")).to_have_text("Create plan to free 50.9 GB")
    assert not calls(ui, "POST", r"/v1/plans")                                            # syncing alone builds nothing

    # the other way: stale data, one click on Create syncs, re-reads the report and then builds the plan
    rep["freshness"]["gdrive"] = {"synced_at": _iso(timedelta(days=3)), "stale": True}
    rep["stale"] = True
    ui.mock.sync_stage = 3
    ui.go("#/free/dropbox?path=x")
    expect(ui.tid("free-stale")).to_be_visible()
    ui.tid("free-create").click()
    expect(ui.tid("view-plan-review")).to_be_visible(timeout=10000)
    assert len(calls(ui, "POST", r"/v1/ui/sync")) == 2
    assert len(calls(ui, "POST", r"/v1/plans")) == 1


def test_verify_runs_the_bridge_restricted_to_the_probable_entries_and_updates_the_buckets(make_onb):
    ui = free_ui(make_onb)
    verify = ui.tid("free-verify")
    expect(verify).to_have_text("Verify (downloads 3.0 GB)")
    expect(ui.tid("verify-capped")).to_contain_text("first 3 of 411")
    verify.click()
    assert last_body(ui, "POST", r"/v1/ui/bridge") == {"entry_ids": [11, 12, 13]}
    expect(ui.tid("verify-progress")).to_have_attribute("data-phase", "running")
    expect(ui.tid("verify-progress")).to_contain_text("of 3")
    expect(ui.tid("verify-progress")).to_have_attribute("data-phase", "done", timeout=8000)
    expect(ui.tid("verify-progress")).to_contain_text("Verified: 3 checked, 0 failed")
    expect(ui.tid("bucket-bytes-proven")).to_have_text("53.9 GB")                          # the report was read again
    expect(ui.tid("bucket-probable")).to_have_attribute("data-files", "0")
    expect(ui.tid("free-verify")).to_have_count(0)


def test_copy_first_opens_new_consolidate_with_this_cloud_as_the_source(make_onb):
    ui = free_ui(make_onb)
    ui.tid("free-copy-first").click()
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "consolidate")
    q = parse_qs(urlparse(ui.page.evaluate("location.href")).fragment.partition("?")[2])
    assert q["src"] == ["dropbox"] and q["target"] == ["gdrive"]


def test_create_plan_proposes_a_release_plan_and_opens_its_review(make_onb):
    ui = free_ui(make_onb, "#/free/dropbox?path=Photos&keep=onedrive,gdrive")
    ui.tid("free-create").click()
    expect(ui.tid("view-plan-review")).to_be_visible(timeout=8000)
    assert last_body(ui, "POST", r"/v1/plans") == {
        "kind": "release", "params": {"remote": "dropbox", "path_prefix": "Photos", "keep_on": ["onedrive", "gdrive"]}}
    assert re.fullmatch(r"#/plans/\d+", ui.page.evaluate("location.hash"))
    expect(ui.tid("plan-kind")).to_have_text("free space")
    expect(ui.tid("view-plan-review")).to_contain_text("Free space on dropbox")


def test_a_read_only_cloud_cannot_be_freed_and_a_refusal_is_worded(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox", writable=("gdrive",))
    ui.mock.report("dropbox")["writable"] = False
    start(ui, "#/free/dropbox")
    expect(ui.tid("free-readonly")).to_contain_text("dropbox is read-only")
    expect(ui.tid("free-create")).to_be_disabled()
    ui.mock.report("dropbox")["writable"] = True
    ui.go("#/free/dropbox?path=again")
    expect(ui.tid("free-create")).to_be_enabled()


def test_a_release_refused_by_the_builder_is_shown(make_onb):
    ui = free_ui(make_onb)
    original = ui.mock.onboarding

    def refusing(method, path, q, body):
        if (method, path) == ("POST", "/v1/plans"):
            return 400, {"error": "dropbox has no write remote", "code": "READ_ONLY", "params": {"remote": "dropbox"}}
        return original(method, path, q, body)

    ui.mock.onboarding = refusing
    ui.tid("free-create").click()
    expect(ui.tid("free-create-error")).to_contain_text("dropbox cannot be written to")


def test_nothing_proven_means_nothing_to_create(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    ui.mock.report("dropbox")["proven"] = {"files": 0, "bytes": 0, "by_remote": {}}
    start(ui, "#/free/dropbox")
    expect(ui.tid("free-create")).to_be_disabled()


def test_a_missing_quota_is_said_and_the_estimate_remains(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    ui.mock.report("dropbox")["quota"] = None
    start(ui, "#/free/dropbox")
    expect(ui.tid("free-quota")).to_have_attribute("data-known", "false")
    expect(ui.tid("free-after")).to_contain_text("Would free 50.9 GB")


def test_a_finished_release_plan_is_pointed_out_on_the_free_space_view(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    pid = ui.mock.add_plan(kind="release", status="done", params={"remote": "dropbox"}, ops=4)
    start(ui, "#/free/dropbox")
    expect(ui.tid(f"free-pending-{pid}")).to_be_visible()
    expect(ui.tid("free-pending")).to_contain_text("in quarantine on dropbox")


# --------------------------------------------------------------------------- purge

def purge_ui(make_onb, **kw: Any) -> tuple[Ui, int]:
    ui = make_onb(**kw)
    ui.mock.connect_clouds("gdrive", "dropbox")
    pid = ui.mock.add_plan(kind="release", status="done", params={"remote": "gdrive"}, ops=4)
    return start(ui, f"#/plans/{pid}"), pid


def test_a_finished_release_plan_offers_purge_with_a_typed_confirmation(make_onb):
    ui, pid = purge_ui(make_onb)
    box = ui.tid("purge-box")
    expect(box).to_be_visible()
    expect(ui.tid("purge-text")).to_contain_text("3.8 MB in quarantine on gdrive")
    expect(ui.tid("purge-text")).to_contain_text("space comes back when it is purged")
    expect(box).to_contain_text("30 days")
    ui.tid("purge-now").click()
    expect(ui.tid("purge-dialog")).to_be_visible()
    confirm = ui.tid("purge-confirm")
    expect(confirm).to_be_disabled()
    ui.tid("purge-input").fill("purg")
    expect(confirm).to_be_disabled()
    ui.tid("purge-input").fill("PURGE")
    expect(confirm).to_be_disabled()                                      # exactly the word, in lower case
    assert not calls(ui, "POST", rf"/v1/ui/plans/{pid}/purge")
    shot(ui, "purge-dialog")
    ui.tid("purge-input").fill("purge")
    expect(confirm).to_be_enabled()
    confirm.click()
    expect(ui.tid("purge-done")).to_contain_text("space on gdrive is free again", timeout=8000)
    assert last_body(ui, "POST", rf"/v1/ui/plans/{pid}/purge") == {"confirm": "purge", "older_than_days": 0}
    expect(ui.tid("purge-now")).to_have_count(0)
    expect(ui.tid("purge-box")).to_contain_text("4 files deleted")


def test_cancelling_the_purge_dialog_sends_nothing_and_in_30_days_leaves_the_quarantine(make_onb):
    ui, pid = purge_ui(make_onb)
    ui.tid("purge-now").click()
    ui.tid("purge-cancel").click()
    expect(ui.tid("purge-dialog")).to_have_count(0)
    ui.tid("purge-later").click()
    expect(ui.tid("purge-later-note")).to_contain_text("Left in quarantine on gdrive")
    assert not calls(ui, "POST", rf"/v1/ui/plans/{pid}/purge")
    ui.tid("purge-undo-later").click()
    expect(ui.tid("purge-now")).to_be_visible()


def test_a_purge_that_fails_keeps_the_dialog_open_with_the_reason(make_onb):
    ui, pid = purge_ui(make_onb)
    original = ui.mock.onboarding

    def refusing(method, path, q, body):
        if path == f"/v1/ui/plans/{pid}/purge":
            return 409, {"error": "plan is executing", "code": "CONFLICT", "params": {"plan": pid, "plan_status": "executing"}}
        return original(method, path, q, body)

    ui.mock.onboarding = refusing
    ui.tid("purge-now").click()
    ui.tid("purge-input").fill("purge")
    ui.tid("purge-confirm").click()
    expect(ui.tid("purge-error")).to_be_visible()
    expect(ui.tid("purge-dialog")).to_be_visible()


def test_purge_is_not_offered_on_other_plans_or_before_the_run(make_onb):
    ui = make_onb()
    ui.mock.connect_clouds("gdrive", "dropbox")
    dedupe = ui.mock.add_plan(kind="dedupe", status="done", ops=2)
    pending = ui.mock.add_plan(kind="release", status="simulated", params={"remote": "gdrive"}, ops=2)
    start(ui, f"#/plans/{dedupe}")
    expect(ui.tid("plan-closed")).to_be_visible()
    expect(ui.tid("purge-box")).to_have_count(0)
    ui.go(f"#/plans/{pending}")
    expect(ui.tid("view-plan-review")).to_be_visible()
    expect(ui.tid("plan-facts")).to_be_visible()
    expect(ui.tid("purge-box")).to_have_count(0)


def test_the_purge_box_in_chinese(make_onb):
    ui, _ = purge_ui(make_onb, locale="zh-TW")
    expect(ui.tid("purge-text")).to_contain_text("清除之後空間才會回來")
    ui.tid("purge-now").click()
    expect(ui.tid("purge-dialog")).to_contain_text("無法復原")
    ui.tid("purge-input").fill("purge")
    expect(ui.tid("purge-confirm")).to_be_enabled()


# --------------------------------------------------------------------------- the page keeps its manners

def test_every_human_channel_call_carries_the_csrf_token_and_no_secret_is_kept(make_onb):
    ui = free_ui(make_onb)
    ui.tid("free-verify").click()
    expect(ui.tid("verify-progress")).to_have_attribute("data-phase", "done", timeout=8000)
    ui.go("#/clouds")
    expect(ui.tid("conn-card-gdrive")).to_be_visible()
    ui.tid("conn-disconnect-onedrive").click()
    ui.tid("dialog-ok").click()
    expect(ui.tid("conn-card-onedrive")).to_have_count(0)
    human = [c for c in ui.mock.calls if c.path.startswith("/v1/ui/") and c.method != "GET" and c.path != "/v1/ui/session"]
    assert human and all(c.headers.get("x-csrf-token") == MockApi.CSRF for c in human)
    storage = ui.page.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
    assert "GOCSPX" not in storage and "client_secret" not in storage


def test_the_google_secret_is_never_put_in_the_address_or_storage(make_onb):
    ui = start(make_onb())
    connect_via_dialog(ui, "drive")
    ui.tid("client-id").fill(GOOD_ID)
    ui.tid("client-secret").fill(GOOD_SECRET)
    ui.tid("google-confirm-prod").check()
    expect(ui.tid("client-secret")).to_have_attribute("type", "password")
    ui.tid("connect-start").click()
    expect(ui.tid("conn-card-gdrive")).to_be_visible(timeout=8000)
    assert GOOD_SECRET not in ui.page.evaluate("location.href")
    assert GOOD_SECRET not in ui.page.evaluate("JSON.stringify(Object.entries(localStorage))")
    assert GOOD_SECRET not in ui.page.content()


def test_the_view_works_on_a_phone_width(make_onb):
    ui = free_ui(make_onb, width=390, height=844)
    assert ui.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    ui.go("#/clouds")
    expect(ui.tid("conn-card-gdrive")).to_be_visible()
    assert ui.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    shot(ui, "clouds-phone")


def test_the_dictionaries_have_every_key_the_new_screens_use(make_onb):
    ui = start(make_onb())
    dicts = ui.page.evaluate("import('/ui/static/i18n.js').then(m => m.dictionaries)")
    web = Path(__file__).resolve().parent.parent / "src" / "drivemerge" / "web"
    used: set[str] = set()
    for name in ("onboarding.js", "sync.js", "free.js"):
        used |= set(re.findall(r"""\bt\(\s*['"]([A-Za-z0-9_.+]+)['"]""", (web / name).read_text(encoding="utf-8")))
    assert used and not used - set(dicts["en"]), sorted(used - set(dicts["en"]))
    assert not used - set(dicts["zh-TW"])
    # the keys built at run time: card states, sync states, hints, tiles
    dynamic = [f"conn.state.{s}" for s in ("signing", "ro", "rw", "relogin", "error")] + \
              [f"sync.state.{s}" for s in ("queued", "listing", "done", "failed")] + \
              [f"conn.hint.{h}" for h in ("badClient", "testing", "api")] + \
              [f"conn.tile.{p}" for p in ("drive", "onedrive", "dropbox", "other")] + \
              [f"err.{c}" for c in ("NO_RCLONE", "RCLONE_TOO_OLD", "FLOW_BUSY", "FLOW_FAILED", "FLOW_TIMEOUT", "NAME_TAKEN",
                                    "NEEDS_CLIENT", "TEST_FAILED")]
    for key in dynamic:
        assert key in dicts["en"] and key in dicts["zh-TW"], key
