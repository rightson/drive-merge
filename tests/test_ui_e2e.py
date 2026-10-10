"""The visual workspace end to end: real rclone, real catalog, real server, real browser.

Three local directories stand in for three clouds (like tests/test_e2e.py) and hold some scattered school and
research material. A person opens the workspace in headless Chromium, looks at two places at once, drags
folders to better levels (one of them to a wrong place and back), undoes and redoes, previews, types the
approval code, waits for the execution and rescans. Then the files on disk are checked: what moved, what went
to quarantine, and that nothing was overwritten.

Screenshots of every step go to $DRIVEMERGE_UI_SHOTS (default /tmp/claude-0/ui-shots) as e2e-*.png.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")
import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from drivemerge import db, plans, ui_api  # noqa: E402
from drivemerge.config import parse_config  # noqa: E402
from drivemerge.service import Service  # noqa: E402

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH"),
]

SHOTS = Path(os.environ.get("DRIVEMERGE_UI_SHOTS", "/tmp/claude-0/ui-shots"))
UI_TOKEN = "e2e-ui-token"
API_TOKEN = "e2e-api-token"
QUARANTINE = ".drive-merge-quarantine"

# Each "cloud" only exposes its provider's native hash, so the same content looks different on each of them
# until it has been bridged.
CLOUDS = {"gd": ["md5", "sha1", "sha256"], "dbx": ["dropbox"], "od": ["quickxor"]}


def _blobs() -> dict[str, dict[str, bytes]]:
    """The scattered material: cloud -> relative path -> bytes. Deterministic, and every content is distinct
    except Thesis/draft.docx, which is the same file on Dropbox and on Drive."""
    rnd = random.Random(2024)

    def blob(n: int) -> bytes:
        return bytes(rnd.getrandbits(8) for _ in range(n))

    draft = blob(4000)
    return {
        "dbx": {
            "School/NTU/Thesis/draft.docx": draft,
            "School/NTU/Thesis/refs.bib": blob(900),
            "School/NTU/Thesis/chapter1.docx": blob(2200),
            "School/NTU/Thesis/chapter2.docx": blob(2300),
            "School/NTU/Courses/CS101/notes.txt": blob(300),
            "School/NTU/Courses/CS101/hw1.pdf": blob(2000),
            "Photos/IMG_2024.jpg": blob(3000),
        },
        "gd": {
            "Research/thesis-drafts/draft.docx": draft,
            "Research/thesis-drafts/notes.md": blob(500),
            "Research/thesis-drafts/chapter1-outline.md": blob(400),
            "Research/thesis-drafts/advisor-meeting-notes.md": blob(450),
            "Research/Papers/survey.pdf": blob(5000),
            "Old/論文資料/data.csv": blob(1500),
            "Old/論文資料/survey-results.csv": blob(1600),
            "Old/論文資料/thesis-notes.txt": blob(120),
            "Photos/trip.jpg": blob(2500),
        },
        "od": {
            "Documents/resume.docx": blob(700),
            "Documents/Uni/slides-week1.pptx": blob(1800),
        },
    }


def _live(root: Path) -> dict[str, bytes]:
    """Every file under `root` that is not in quarantine."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not str(p.relative_to(root)).startswith(QUARANTINE)}


class World:
    """The three clouds on disk, the service that catalogs them and the web server on top."""

    def __init__(self, tmp: Path, files: dict[str, dict[str, bytes]]):
        self.files = files
        self.roots = {name: tmp / name for name in CLOUDS}
        for name, root in self.roots.items():
            for rel, data in files.get(name, {}).items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(data)
            root.mkdir(exist_ok=True)
        self.db_path = tmp / "catalog.db"
        self.config = parse_config({
            "database": {"path": str(self.db_path)},
            "safety": {"execution_enabled": True},
            "remotes": [{"name": n, "provider": "local", "read": str(self.roots[n]), "write": str(self.roots[n]),
                         "hash_types": h} for n, h in CLOUDS.items()],
        })
        self.service = Service(self.config)
        for name in CLOUDS:
            assert self.service.scan(name)["status"] == "complete"
        self.service.rebuild_identity()
        bridged = self.service.bridge(max_bytes=10**7)  # proves Dropbox's draft.docx equals Drive's
        assert not bridged["failed"], bridged
        self.service.refresh_dir_stats()
        self.url = ""
        self._server: uvicorn.Server | None = None

    def serve(self, *, approvals: bool = True) -> None:
        app = ui_api.create_ui_app(self.service, config=self.config, api_token=API_TOKEN, ui_token=UI_TOKEN,
                                   approvals_enabled=approvals)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=self._server.run, daemon=True).start()
        deadline = time.monotonic() + 15
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self._server.started, "the web server did not start"
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            time.sleep(0.2)
        self.service.conn.close()

    def catalog(self):
        """A second connection for assertions; the server keeps its own."""
        return db.connect(self.db_path)


@pytest.fixture
def make_world(tmp_path):
    made: list[World] = []

    def factory(extra: dict[str, dict[str, bytes]] | None = None, *, approvals: bool = True) -> World:
        files = _blobs()
        for cloud, more in (extra or {}).items():
            files[cloud].update(more)
        world = World(tmp_path, files)
        world.serve(approvals=approvals)
        made.append(world)
        return world

    yield factory
    for w in made:
        w.stop()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


class Page:
    """The few gestures the story needs, spelled the way a person would do them."""

    def __init__(self, browser, base: str):
        self.ctx = browser.new_context(viewport={"width": 1480, "height": 920}, locale="zh-TW")
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(20_000)
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        self.base = base
        SHOTS.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.ctx.close()

    def open(self) -> None:
        self.page.goto(f"{self.base}/ui/#token={UI_TOKEN}")
        expect(self.page.get_by_test_id("pane-left")).to_be_visible()
        expect(self.node("left", "dbx:")).to_be_visible()
        assert "token" not in self.page.url, "the one-time token must leave the address bar"

    def shot(self, name: str) -> None:
        self.page.wait_for_timeout(400)  # let the arrival flash and any toast animation settle
        self.page.screenshot(path=str(SHOTS / f"e2e-{name}.png"))

    def pane(self, side: str):
        return self.page.locator(f'[data-testid="pane-{side}"]')

    def node(self, side: str, key: str):
        return self.pane(side).locator(f'[data-testid="node-{key}"]')

    def expand(self, side: str, key: str) -> None:
        self.pane(side).locator(f'[data-testid="toggle-{key}"]').click()

    def settle(self) -> None:
        """Drag only into a quiet page: a layout change while the mouse is down makes Chromium drop the drag."""
        expect(self.page.locator(".tree .row-msg").filter(has_text="載入中")).to_have_count(0)

    def drag(self, src: tuple[str, str], dst: tuple[str, str]) -> None:
        self.settle()
        self.page.drag_and_drop(f'[data-testid="pane-{src[0]}"] [data-testid="node-{src[1]}"]',
                                f'[data-testid="pane-{dst[0]}"] [data-testid="node-{dst[1]}"]')

    def staged(self, n: int) -> None:
        expect(self.page.get_by_test_id("staged-count")).to_have_text(str(n))

    def finish(self) -> None:
        assert not self.errors, f"JavaScript or network errors in the page: {self.errors}"


@pytest.fixture
def person(browser):
    people: list[Page] = []

    def factory(world: World) -> Page:
        p = Page(browser, world.url)
        people.append(p)
        return p

    yield factory
    for p in people:
        p.close()


# --------------------------------------------------------------------------- the story

def test_reorganise_school_and_research_folders_from_two_panes(make_world, person):
    world = make_world()
    before = {n: _live(r) for n, r in world.roots.items()}
    me = person(world)
    page = me.page
    me.open()
    me.shot("01-start")

    # Left pane: every cloud, opened down to the school thesis folder on Dropbox.
    for key in ("dbx:", "dbx:School", "dbx:School/NTU"):
        me.expand("left", key)
    expect(me.node("left", "dbx:School/NTU/Thesis")).to_be_visible()
    expect(me.node("left", "dbx:School/NTU/Thesis")).to_contain_text("4 個檔案")
    # Right pane: Google Drive alone, opened at two different levels.
    page.get_by_test_id("root-select-right").select_option("gd")
    expect(me.node("right", "gd:Research")).to_be_visible()
    me.expand("right", "gd:Research")
    me.expand("right", "gd:Old")
    expect(me.node("right", "gd:Research/thesis-drafts")).to_be_visible()
    expect(me.node("right", "gd:Old/論文資料")).to_be_visible()
    me.shot("02-two-panes-expanded")

    # 1. The school thesis goes into Research (across clouds, so a copy through this machine).
    me.drag(("left", "dbx:School/NTU/Thesis"), ("right", "gd:Research"))
    me.staged(1)
    expect(me.node("right", "gd:Research/Thesis")).to_be_visible()
    expect(me.node("right", "gd:Research/Thesis")).to_contain_text("移入")
    expect(me.node("left", "dbx:School/NTU/Thesis")).to_have_count(0)  # the "after" view: it has left
    expect(page.get_by_test_id("toasts")).to_contain_text("跨雲搬移")  # CROSS_REMOTE warning: bytes travel through here
    me.shot("03-thesis-dragged")

    # 2. The old Chinese-named folder, from another level of the same pane, goes there too.
    me.drag(("right", "gd:Old/論文資料"), ("right", "gd:Research"))
    me.staged(2)
    expect(me.node("right", "gd:Research/論文資料")).to_be_visible()
    expect(me.node("right", "gd:Old/論文資料")).to_have_count(0)
    me.shot("04-second-folder")

    # 3. A wrong place, then back: Photos into Old and out to the top again. The net effect is nothing.
    me.drag(("right", "gd:Photos"), ("right", "gd:Old"))
    me.staged(3)
    expect(me.node("right", "gd:Old/Photos")).to_be_visible()
    me.drag(("right", "gd:Old/Photos"), ("right", "gd:"))
    me.staged(4)
    expect(me.node("right", "gd:Photos")).to_be_visible()
    expect(me.node("right", "gd:Old/Photos")).to_have_count(0)

    # 4. Undo the way back, then redo it.
    page.get_by_test_id("undo-button").click()
    me.staged(3)
    expect(me.node("right", "gd:Old/Photos")).to_be_visible()
    page.get_by_test_id("redo-button").click()
    me.staged(4)
    expect(me.node("right", "gd:Photos")).to_be_visible()
    expect(page.get_by_test_id("redo-button")).to_be_disabled()
    me.shot("05-wrong-place-and-back")

    # 5. Rename the Drive folder to the name of the school one: the two now merge.
    row = me.node("right", "gd:Research/thesis-drafts")
    row.click()
    row.press("F2")
    page.get_by_test_id("rename-input").fill("Thesis")
    page.get_by_test_id("rename-input").press("Enter")
    me.staged(5)
    expect(me.node("right", "gd:Research/thesis-drafts")).to_have_count(0)
    merged = me.node("right", "gd:Research/Thesis")
    expect(merged).to_have_count(1)
    expect(merged.locator('[data-badge="merged"]')).to_be_visible()
    me.expand("right", "gd:Research/Thesis")
    expect(me.node("right", "gd:Research/Thesis/refs.bib")).to_be_visible()  # arrived from Dropbox
    expect(me.node("right", "gd:Research/Thesis/notes.md")).to_be_visible()  # was already on Drive
    same = me.node("right", "gd:Research/Thesis/draft.docx")
    expect(same).to_have_count(1)  # two files of the same content share one place in the merged folder
    expect(same.locator('[data-badge="dedupe"]')).to_be_visible()
    # The "after" numbers are what the clouds will really hold: draft.docx is not counted twice.
    expect(merged).to_contain_text("7 個檔案")
    expect(me.node("right", "gd:")).to_contain_text("12 個檔案")
    expect(me.node("left", "dbx:")).to_contain_text("3 個檔案")
    me.shot("06-merged-with-dedupe")

    # 6. The staged list shows all five gestures; the suggestions drawer offers related folders.
    page.get_by_test_id("staged-toggle").click()
    expect(page.get_by_test_id("staged-item")).to_have_count(5)
    me.shot("07-staged-list")
    page.get_by_test_id("suggestions-toggle").click()
    card = page.get_by_test_id("suggestion-card").first
    expect(card).to_be_visible()
    expect(card).to_contain_text(re.compile("thesis", re.I))
    me.shot("08-suggestions")
    page.get_by_test_id("suggestions-toggle").click()  # closes the drawer again

    # 7. The "before" view still shows the world as it is; "after" is the plan.
    page.get_by_test_id("view-before-left").click()
    expect(me.node("left", "dbx:School/NTU/Thesis")).to_be_visible()
    expect(me.node("left", "dbx:")).to_contain_text("7 個檔案")  # the cloud as it is today, totals included
    expect(me.node("left", "gd:")).to_contain_text("9 個檔案")
    me.shot("09-before-view")
    page.get_by_test_id("view-after-left").click()
    expect(me.node("left", "dbx:School/NTU/Thesis")).to_have_count(0)
    expect(me.node("left", "dbx:")).to_contain_text("3 個檔案")

    # 8. Nothing has touched a cloud so far.
    assert {n: _live(r) for n, r in world.roots.items()} == before
    assert not any((r / QUARANTINE).exists() for r in world.roots.values())

    # 9. Preview: the simulator's verdict, per cloud, with the identical draft.docx collapsed.
    page.get_by_test_id("preview-button").click()
    modal = page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")
    expect(modal.get_by_test_id("summary-dedupes")).to_contain_text("1")
    expect(modal.get_by_test_id("summary-conflicts")).to_contain_text("0")
    expect(modal.get_by_test_id("cloud-table")).to_be_visible()
    expect(modal.get_by_test_id("cloud-row-gd")).to_be_visible()
    expect(modal.get_by_test_id("cloud-row-dbx")).to_be_visible()
    expect(modal.get_by_test_id("cloud-row-od")).to_have_count(0)  # untouched clouds are not listed
    changes = modal.get_by_test_id("change")
    expect(changes).to_have_count(3)  # the Photos round trip left no trace
    listed = modal.get_by_test_id("change-list").inner_text()
    assert "Old/論文資料" in listed and "School/NTU/Thesis" in listed and "Photos" not in listed
    plan_id = int(re.search(r"\d+", modal.get_by_test_id("plan-id").inner_text()).group())
    me.shot("10-preview")

    # The preview is a real plan: dbx's draft.docx is a delete witnessed by the Drive copy, nothing else is.
    conn = world.catalog()
    try:
        names = dict(conn.execute("SELECT id, name FROM remotes").fetchall())
        ops = plans.load_ops(conn, plan_id)
        dedupe = [o for o in ops if o.op == "delete" and o.src_path.endswith("Thesis/draft.docx")]
        assert len(dedupe) == 1
        assert (names[dedupe[0].src_remote_id], dedupe[0].src_path) == ("dbx", "School/NTU/Thesis/draft.docx")
        assert (names[dedupe[0].witness_remote_id], dedupe[0].witness_path) == ("gd", "Research/Thesis/draft.docx")
        assert not any(o.op == "copy" and o.src_path.endswith("draft.docx") for o in ops), "identical: no copy"
        assert not any("Photos" in o.src_path for o in ops)
    finally:
        conn.close()

    # 10. Approve: the code must be typed, and typing something else does nothing.
    code = modal.get_by_test_id("approval-code").inner_text().strip()
    assert re.fullmatch(r"[0-9a-f]{8}", code)
    approve = modal.get_by_test_id("approval-button")
    expect(approve).to_be_disabled()
    modal.get_by_test_id("approval-input").fill("deadbeef" if code != "deadbeef" else "00000000")
    expect(approve).to_be_disabled()
    modal.get_by_test_id("approval-input").fill(code)
    expect(approve).to_be_enabled()
    approve.click()
    progress = modal.get_by_test_id("progress")
    expect(progress).to_be_visible()
    me.shot("11-executing")
    expect(progress).to_have_attribute("data-phase", re.compile("^(done|failed)$"), timeout=120_000)
    me.shot("12-executed")
    assert progress.get_attribute("data-phase") == "done", progress.inner_text()
    expect(modal.get_by_test_id("progress-failures")).to_have_count(0)

    # 11. Rescan what changed, then close.
    modal.get_by_test_id("rescan-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", "rescanned", timeout=120_000)
    me.shot("13-rescanned")
    modal.get_by_test_id("preview-close").click()
    expect(page.get_by_test_id("preview-modal")).to_have_count(0)

    # --- the clouds, on disk -------------------------------------------------------------------------------
    after = {n: _live(r) for n, r in world.roots.items()}
    orig = world.files

    # Drive: the two Drive folders moved (the renamed one merged with the Dropbox thesis), the rest is as it was,
    # and the Dropbox thesis files arrived beside them (draft.docx did not: its twin was already there).
    renamed = {"Research/thesis-drafts/": "Research/Thesis/", "Old/論文資料/": "Research/論文資料/"}
    expected_gd = {}
    for rel, data in orig["gd"].items():
        old_dir = next((d for d in renamed if rel.startswith(d)), None)
        expected_gd[renamed[old_dir] + rel[len(old_dir):] if old_dir else rel] = data
    for rel, data in orig["dbx"].items():
        if rel.startswith("School/NTU/Thesis/"):
            expected_gd.setdefault("Research/Thesis/" + rel[len("School/NTU/Thesis/"):], data)
    assert after["gd"] == expected_gd
    assert (world.roots["gd"] / "Research/Thesis/refs.bib").read_bytes() == orig["dbx"]["School/NTU/Thesis/refs.bib"]
    assert (world.roots["gd"] / "Research/論文資料/data.csv").read_bytes() == orig["gd"]["Old/論文資料/data.csv"]
    assert (world.roots["gd"] / "Research/Thesis/draft.docx").read_bytes() == orig["gd"]["Research/thesis-drafts/draft.docx"]
    assert (world.roots["gd"] / "Photos/trip.jpg").read_bytes() == orig["gd"]["Photos/trip.jpg"]

    # Dropbox keeps only what was not moved; the thesis files wait in quarantine, byte for byte.
    assert after["dbx"] == {rel: data for rel, data in orig["dbx"].items() if not rel.startswith("School/NTU/Thesis/")}
    quarantined = {str(p.relative_to(world.roots["dbx"] / QUARANTINE / str(plan_id))): p.read_bytes()
                   for p in (world.roots["dbx"] / QUARANTINE / str(plan_id)).rglob("*") if p.is_file()}
    assert quarantined == {rel: data for rel, data in orig["dbx"].items() if rel.startswith("School/NTU/Thesis/")}
    assert after["od"] == orig["od"], "a cloud the draft never mentioned is untouched"
    # Nothing was overwritten or lost: every original content exists somewhere, live or quarantined.
    digests = lambda files: {hashlib.sha256(d).hexdigest() for d in files.values()}  # noqa: E731
    still_there = digests(after["gd"]) | digests(after["dbx"]) | digests(after["od"]) | digests(quarantined)
    assert digests({**orig["gd"], **orig["dbx"], **orig["od"]}) <= still_there

    # The catalog agrees: the plan finished, the draft is closed, and a rescan saw the new layout.
    conn = world.catalog()
    try:
        assert conn.execute("SELECT COUNT(*) FROM entries WHERE present = 1 AND content_group IS NOT NULL "
                            "AND path LIKE ?", (f"{QUARANTINE}/%",)).fetchone()[0] == 0, "quarantine is not live"
        status = conn.execute("SELECT status FROM plans WHERE id = ?", (plan_id,)).fetchone()[0]
        assert status == "done"
        draft_id = plans.get_plan(conn, plan_id, with_ops=False).params["draft_id"]
        assert conn.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,)).fetchone()[0] == "applied"
        bad = conn.execute("SELECT seq, op, status, error FROM plan_ops WHERE plan_id = ? AND status NOT IN ('done', 'skipped')",
                           (plan_id,)).fetchall()
        assert not bad, [tuple(r) for r in bad]
        live = {r[0] for r in conn.execute(
            "SELECT e.path FROM entries e JOIN remotes r ON r.id = e.remote_id "
            "WHERE r.name = 'gd' AND e.present = 1 AND e.is_dir = 0")}
        assert "Research/Thesis/refs.bib" in live and "Research/論文資料/data.csv" in live
        assert "Old/論文資料/data.csv" not in live
    finally:
        conn.close()

    # Nothing is offered out of quarantine: a deleted file is not a folder to tidy, nor a copy.
    request = urllib.request.Request(f"{world.url}/v1/suggestions", headers={"Authorization": f"Bearer {API_TOKEN}"})
    offered = json.load(urllib.request.urlopen(request))["suggestions"]
    assert not [f for sug in offered for f in sug["folders"] if f["path"].startswith(QUARANTINE)], offered

    # --- the panes, after the rescan -----------------------------------------------------------------------
    me.staged(0)  # a fresh draft: the old one is applied
    expect(me.node("right", "gd:Research/Thesis")).to_be_visible()
    expect(me.node("right", "gd:Research/Thesis")).to_contain_text("7 個檔案")
    expect(me.node("right", "gd:Research/Thesis")).not_to_contain_text("重複")  # the quarantined twins are not duplicates
    expect(me.node("right", "gd:Research/Thesis/refs.bib")).to_be_visible()
    expect(me.node("right", "gd:Research/Thesis/refs.bib").locator('[data-badge="dup"]')).to_have_count(0)
    expect(me.node("left", "dbx:")).not_to_contain_text("重複")
    expect(me.node("left", f"dbx:{QUARANTINE}")).to_contain_text("4 個檔案")  # still there, still counted as files
    expect(me.node("right", "gd:Research/論文資料")).to_be_visible()
    expect(me.node("right", "gd:Old/論文資料")).to_have_count(1)  # the emptied folder stays behind (v1 limitation)
    expect(me.node("right", "gd:Old/論文資料")).to_contain_text("空")
    expect(me.node("right", "gd:Research/thesis-drafts")).to_have_count(1)
    expect(me.node("right", "gd:Research/thesis-drafts")).to_contain_text("空")
    expect(me.node("left", "dbx:School/NTU/Thesis")).to_contain_text("空")
    page.get_by_test_id("view-before-right").click()  # "before" now equals "after": the catalog caught up
    expect(me.node("right", "gd:Research/Thesis/refs.bib")).to_be_visible()
    page.get_by_test_id("view-after-right").click()
    expect(me.node("left", "dbx:")).to_contain_text(re.compile("個檔案"))
    me.shot("14-after-rescan")
    me.finish()


def test_a_merge_with_different_content_is_a_conflict_that_cannot_be_approved(make_world, person):
    """Same name, different bytes: the preview names the problem, offers no approval, and nothing moves."""
    clash = b"this is NOT the bibliography that is on Dropbox"
    world = make_world(extra={"gd": {"Research/Thesis/refs.bib": clash}})
    before = {n: _live(r) for n, r in world.roots.items()}
    me = person(world)
    page = me.page
    me.open()
    for key in ("dbx:", "dbx:School", "dbx:School/NTU"):
        me.expand("left", key)
    page.get_by_test_id("root-select-right").select_option("gd")
    me.expand("right", "gd:Research")
    expect(me.node("right", "gd:Research/Thesis")).to_be_visible()

    me.drag(("left", "dbx:School/NTU/Thesis"), ("right", "gd:Research"))  # same name as a folder that is there
    me.staged(1)
    merged = me.node("right", "gd:Research/Thesis")
    expect(merged.locator('[data-badge="merged"]')).to_be_visible()
    me.expand("right", "gd:Research/Thesis")
    clashing = me.node("right", "gd:Research/Thesis/refs.bib")
    expect(clashing).to_have_count(1)
    expect(clashing.locator('[data-badge="conflict"]')).to_be_visible()
    me.shot("20-conflict-in-tree")

    page.get_by_test_id("preview-button").click()
    modal = page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "false")
    expect(modal.get_by_test_id("conflict").first).to_contain_text("refs.bib")
    expect(modal.get_by_test_id("summary-conflicts")).to_contain_text("1")
    expect(modal.get_by_test_id("approval-code")).to_have_count(0)
    expect(modal.get_by_test_id("approval-button")).to_have_count(0)
    me.shot("21-conflict-preview")
    modal.get_by_test_id("preview-close").click()

    assert {n: _live(r) for n, r in world.roots.items()} == before
    assert not any((r / QUARANTINE).exists() for r in world.roots.values())
    conn = world.catalog()
    try:  # whatever the page offered, the plan cannot be approved: the simulator refused it
        plan_id = conn.execute("SELECT MAX(id) FROM plans").fetchone()[0]
        assert conn.execute("SELECT status FROM plans WHERE id = ?", (plan_id,)).fetchone()[0] == "draft"
    finally:
        conn.close()
    me.finish()


def test_without_browser_approval_the_terminal_finishes_the_job_and_the_page_follows(make_world, person):
    """`drivemerge ui` outside a terminal can stage and preview but not approve. Approving and executing in a
    terminal (here: a second Service on the same catalog, as the CLI does) closes the draft, and the open page
    notices on its own."""
    world = make_world(approvals=False)
    me = person(world)
    page = me.page
    me.open()
    expect(page.get_by_test_id("approvals-banner")).to_be_visible()
    for key in ("dbx:", "dbx:School", "dbx:School/NTU"):
        me.expand("left", key)
    page.get_by_test_id("root-select-right").select_option("gd")
    expect(me.node("right", "gd:Research")).to_be_visible()
    me.drag(("left", "dbx:School/NTU/Thesis"), ("right", "gd:Research"))
    me.staged(1)

    page.get_by_test_id("preview-button").click()
    modal = page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")
    expect(modal.get_by_test_id("approval-button")).to_have_count(0)
    expect(modal.get_by_test_id("approval-code")).to_have_count(0)
    plan_id = int(re.search(r"\d+", modal.get_by_test_id("plan-id").inner_text()).group())
    expect(modal.get_by_test_id("cli-approve")).to_have_text(f"drivemerge plan approve {plan_id}")
    expect(modal.get_by_test_id("cli-execute")).to_have_text(f"drivemerge plan execute {plan_id}")
    me.shot("30-cli-commands")

    # Neither the browser session without approvals nor the API token reaches the human channel.
    session = page.context.request.post(f"{world.url}/v1/ui/plans/{plan_id}/approve", data={"code": "00000000"})
    assert session.status == 403 and session.json()["code"] == "APPROVALS_DISABLED"
    asked = urllib.request.Request(f"{world.url}/v1/ui/plans/{plan_id}/approve", data=b'{"code": "00000000"}',
                                   headers={"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(asked)
    assert refused.value.code == 403 and json.load(refused.value)["code"] == "HUMAN_ONLY"
    modal.get_by_test_id("preview-close").click()

    # In the person's own terminal:
    terminal = Service(world.config)
    try:
        plan = plans.get_plan(terminal.conn, plan_id, with_ops=False)
        terminal.approve(plan_id, plans.short_code(plan.digest), approved_by="terminal")
        outcome = terminal.execute(plan_id)
    finally:
        terminal.conn.close()
    assert outcome["status"] == "done" and outcome["draft_applied"] is True

    # The page was open the whole time; its polling sees that the draft was applied and starts a fresh one.
    expect(page.get_by_test_id("staged-count")).to_have_text("0", timeout=15_000)
    expect(page.get_by_test_id("draft-select")).to_contain_text("#2")
    me.shot("31-draft-applied-elsewhere")
    assert (world.roots["gd"] / "Research/Thesis/refs.bib").read_bytes() == world.files["dbx"]["School/NTU/Thesis/refs.bib"]
    quarantine = world.roots["dbx"] / QUARANTINE / str(plan_id) / "School/NTU/Thesis"
    assert sorted(p.name for p in quarantine.iterdir()) == ["chapter1.docx", "chapter2.docx", "draft.docx", "refs.bib"]
    me.finish()


def test_a_duplicate_folder_suggestion_stages_in_one_click_and_dedupes_with_witnesses(make_world, person):
    """Dropbox holds a backup copy of a Drive folder. One click on the suggestion stages the merge; every file
    becomes a delete witnessed by the Drive original, so Drive itself is not touched at all."""
    backup = {f"Backup/thesis drafts (copy)/{rel.split('/', 2)[2]}": data
              for rel, data in _blobs()["gd"].items() if rel.startswith("Research/thesis-drafts/")}
    world = make_world(extra={"dbx": backup})
    gd_before = _live(world.roots["gd"])
    me = person(world)
    page = me.page
    me.open()
    page.get_by_test_id("suggestions-toggle").click()
    page.get_by_test_id("sug-tab-duplicates").click()
    card = page.get_by_test_id("suggestion-card").first
    expect(card).to_be_visible()
    expect(card).to_contain_text("thesis drafts (copy)")
    me.shot("40-duplicate-suggestion")
    card.get_by_test_id("suggestion-add").click()
    me.staged(1)
    expect(card.get_by_test_id("suggestion-add")).to_be_disabled()  # added: not offered twice
    page.get_by_test_id("suggestions-toggle").click()

    page.get_by_test_id("preview-button").click()
    modal = page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")
    expect(modal.get_by_test_id("summary-dedupes")).to_contain_text("4")
    expect(modal.get_by_test_id("cloud-row-gd").locator(".delta")).to_have_count(0)  # Drive gains and loses nothing
    expect(modal.get_by_test_id("cloud-row-dbx").locator(".delta")).to_contain_text("4")
    me.shot("41-duplicate-preview")
    modal.get_by_test_id("approval-input").fill(modal.get_by_test_id("approval-code").inner_text().strip())
    modal.get_by_test_id("approval-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", re.compile("^(done|failed)$"),
                                                              timeout=120_000)
    assert modal.get_by_test_id("progress").get_attribute("data-phase") == "done"

    assert _live(world.roots["gd"]) == gd_before, "the Drive folder is the witness: it must not change"
    assert not any(k.startswith("Backup/thesis drafts (copy)/") for k in _live(world.roots["dbx"]))
    kept = world.roots["dbx"].rglob(f"{QUARANTINE}/*/Backup/thesis drafts (copy)/*")
    assert sorted(p.name for p in kept) == sorted(rel.rsplit("/", 1)[1] for rel in backup)
    me.finish()


def test_the_ui_command_serves_the_catalog_that_the_cli_built(tmp_path, browser):
    """`drivemerge scan`, `identity rebuild` and `ui` as a person runs them: folder counts and duplicate hints are
    there without any extra step, the one-time URL opens a session, and without a terminal there is no approval."""
    for cloud in ("gd", "dbx"):
        (tmp_path / cloud / "Docs").mkdir(parents=True)
        (tmp_path / cloud / "Docs" / "a.txt").write_bytes(b"same on both clouds")
    (tmp_path / "gd" / "Docs" / "only-here.txt").write_bytes(b"gd only")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\npath = "{tmp_path / "catalog.db"}"\n' + "".join(
        f'[[remotes]]\nname = "{n}"\nprovider = "local"\nread = "{tmp_path / n}"\nwrite = "{tmp_path / n}"\n'
        'hash_types = ["md5"]\n' for n in ("gd", "dbx")))
    cli = [sys.executable, "-c", "import sys; from drivemerge.cli import main; sys.exit(main(sys.argv[1:]))"]
    for args in (["scan", "--all"], ["identity", "rebuild"]):
        done = subprocess.run([*cli, *args, "--config", str(cfg)], capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen([*cli, "ui", "--no-browser", "--port", str(port), "--config", str(cfg)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    person_ = None
    try:
        first = proc.stdout.readline()
        match = re.search(r"http://127\.0\.0\.1:%d/ui/#token=\S+" % port, first)
        assert match, first
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        person_ = Page(browser, f"http://127.0.0.1:{port}")
        page = person_.page
        page.goto(match.group())
        expect(person_.node("left", "gd:")).to_contain_text("2 個檔案")
        expect(person_.node("left", "dbx:")).to_contain_text("1 個檔案")
        expect(person_.node("left", "gd:")).to_contain_text("重複 1")           # a.txt exists on both clouds
        person_.expand("left", "gd:")
        person_.expand("left", "gd:Docs")
        expect(person_.node("left", "gd:Docs/a.txt").locator('[data-badge="dup"]')).to_be_visible()
        expect(person_.node("left", "gd:Docs/only-here.txt").locator('[data-badge="dup"]')).to_have_count(0)
        expect(page.get_by_test_id("approvals-banner")).to_be_visible()      # started without a terminal
        assert "token" not in page.url
        person_.finish()
    finally:
        if person_ is not None:
            person_.close()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_file_that_changed_behind_the_scenes_stops_the_run_and_a_retry_after_a_rescan_finishes_it(make_world, person):
    """Fail closed: the executor looks at every source right before it acts. When refs.bib is edited on Dropbox after
    the scan, the run stops there, nothing is deleted or overwritten, the page says so and offers a rescan; the
    draft is still open, and after the rescan the same drag previews, approves and completes."""
    world = make_world()
    orig = world.files
    me = person(world)
    page = me.page
    me.open()
    for key in ("dbx:", "dbx:School", "dbx:School/NTU"):
        me.expand("left", key)
    page.get_by_test_id("root-select-right").select_option("gd")
    expect(me.node("right", "gd:Research")).to_be_visible()
    me.drag(("left", "dbx:School/NTU/Thesis"), ("right", "gd:Research"))
    me.staged(1)

    edited = b"E" * len(orig["dbx"]["School/NTU/Thesis/refs.bib"])      # same size, other bytes: only a hash notices
    (world.roots["dbx"] / "School/NTU/Thesis/refs.bib").write_bytes(edited)
    dbx_now = _live(world.roots["dbx"])
    gd_before = _live(world.roots["gd"])

    page.get_by_test_id("preview-button").click()
    modal = page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")  # the catalog does not know yet
    modal.get_by_test_id("approval-input").fill(modal.get_by_test_id("approval-code").inner_text().strip())
    modal.get_by_test_id("approval-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", re.compile("^(done|failed)$"),
                                                              timeout=120_000)
    assert modal.get_by_test_id("progress").get_attribute("data-phase") == "failed"
    expect(modal.get_by_test_id("progress-failures")).to_be_visible()
    expect(modal.get_by_test_id("failure-help")).to_be_visible()
    me.shot("50-run-stopped")

    assert _live(world.roots["dbx"]) == dbx_now, "no source was deleted or changed by the stopped run"
    assert not (world.roots["dbx"] / QUARANTINE).exists()
    gd_now = _live(world.roots["gd"])
    assert {k: v for k, v in gd_now.items() if k in gd_before} == gd_before, "nothing on Drive was overwritten"
    assert "Research/Thesis/refs.bib" not in gd_now, "the edited file was not copied"
    conn = world.catalog()
    try:
        failed_plan = conn.execute("SELECT id, status FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        assert failed_plan["status"] == "failed"
        failed_id = failed_plan["id"]
    finally:
        conn.close()

    # Rescan, and the same draft is still there to preview again.
    modal.get_by_test_id("rescan-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", "rescanned", timeout=120_000)
    modal.get_by_test_id("preview-close").click()
    me.staged(1)
    page.get_by_test_id("preview-button").click()
    modal = page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")
    expect(modal.get_by_test_id("plan-id")).not_to_have_text(f"計畫 #{failed_id}")
    me.shot("51-preview-after-rescan")
    modal.get_by_test_id("approval-input").fill(modal.get_by_test_id("approval-code").inner_text().strip())
    modal.get_by_test_id("approval-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", re.compile("^(done|failed)$"),
                                                              timeout=120_000)
    assert modal.get_by_test_id("progress").get_attribute("data-phase") == "done", modal.inner_text()

    assert (world.roots["gd"] / "Research/Thesis/refs.bib").read_bytes() == edited, "the file as it is now"
    assert not [k for k in _live(world.roots["dbx"]) if k.startswith("School/NTU/Thesis/")]
    for name in ("chapter1.docx", "chapter2.docx"):
        assert (world.roots["gd"] / "Research/Thesis" / name).read_bytes() == orig["dbx"][f"School/NTU/Thesis/{name}"]
    assert {k: v for k, v in _live(world.roots["gd"]).items() if k in gd_before} == gd_before
    me.finish()
