"""Bulk actions from Explore, end to end: real rclone over local directories, a real catalog, the real server and a
real browser (docs/BULK.md section H).

The scenario of the spec: three PDFs across the clouds are selected in Explore, gathered into one folder of Google
Drive (one of them from Dropbox, through the cross-cloud option), previewed, approved by typing the code and run; then
the duplicates of exactly one selected content are cleaned up through a dedupe plan limited to that selection; and a file
that appears on disk after a rescan makes the old "all matching" address answer SELECTION_CHANGED, worded on the page.

The page runs in Traditional Chinese (the default of `Page`), so the sentences asserted here are the zh-TW ones.
Screenshots go to $DRIVEMERGE_UI_SHOTS as bulk-e2e-*.png.
"""

from __future__ import annotations

import random
import re
import shutil
import urllib.parse

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")
from playwright.sync_api import expect  # noqa: E402

from drivemerge import plans  # noqa: E402
from test_ui_e2e import (  # noqa: E402,F401  (fixtures)
    QUARANTINE, World, _blobs, _live, _rescan_from_a_terminal, browser, make_world, person,
)

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH"),
]

PAPER_A = bytes(random.Random(7).getrandbits(8) for _ in range(2048))


def kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def hash_of(page) -> str:
    return urllib.parse.unquote(page.evaluate("location.hash"))


def approve_and_run(page, *, workspace_modal: bool) -> None:
    """Type the approval code and wait for the run to end (the workspace preview dialog and the plan review share the fields)."""
    area = page.get_by_test_id("preview-modal") if workspace_modal else page
    code = area.get_by_test_id("approval-code").inner_text().strip()
    assert re.fullmatch(r"[0-9a-f]{8}", code)
    area.get_by_test_id("approval-input").fill(code)
    area.get_by_test_id("approval-button").click()
    if not workspace_modal:
        expect(page.get_by_test_id("plan-approved")).to_be_visible()
        page.get_by_test_id("plan-run").click()
    progress = page.get_by_test_id("progress")
    expect(progress).to_have_attribute("data-phase", re.compile("^(done|failed)$"), timeout=120_000)
    assert progress.get_attribute("data-phase") == "done", progress.inner_text()


def test_select_pdfs_in_explore_gather_them_then_clean_up_the_duplicates_of_the_selection(make_world, person):
    survey = _blobs()["gd"]["Research/Papers/survey.pdf"]
    world: World = make_world(extra={"gd": {"Downloads/survey.pdf": survey, "Desktop/paper-a.pdf": PAPER_A}})
    before = {n: _live(r) for n, r in world.roots.items()}
    hw1 = before["dbx"]["School/NTU/Courses/CS101/hw1.pdf"]
    me = person(world)
    page = me.page
    tid = page.get_by_test_id
    me.open()

    # ---------------------------------------------------------------- 1. select everything the query finds
    page.evaluate("() => { location.hash = '#/explore?q=type:pdf'; }")
    expect(tid("ex-count")).to_have_text("顯示 3 / 3 筆")
    tid("ex-select-page").check()
    tid("ex-select-all-matching").click()
    expect(tid("ex-select-all-line")).to_contain_text("全部符合的 3 筆都已選取")
    expect(tid("ex-sel-count")).to_have_text("已選取 3 份內容")
    expect(tid("ex-sel-copies")).to_have_text("4 個副本")
    assert tid("ex-sel-bytes").inner_text() == kb(len(survey) + len(PAPER_A) + len(hw1))
    expect(tid("ex-sel-remote-gd")).to_contain_text("gd 2")             # survey.pdf (twice on Drive, one content) and paper-a.pdf
    expect(tid("ex-sel-remote-dbx")).to_contain_text("dbx 1")
    expect(tid("ex-sel-remote-od")).to_have_count(0)
    expect(tid("ex-sel-redundant")).to_contain_text("1 個是多餘的副本")
    me.shot("bulk-e2e-1-selected")

    # ---------------------------------------------------------------- 2. gather into gd:Research/Papers
    tid("ex-sel-gather").click()
    tid("gather-dst-remote").select_option("gd")
    tid("gather-dst-path").fill("Research/Papers")
    check = tid("gather-check")
    expect(check).to_have_attribute("data-files", "1")                      # paper-a.pdf only
    expect(tid("gather-sample")).to_contain_text("gd:Desktop/paper-a.pdf")
    expect(tid("gather-sample")).to_contain_text("gd:Research/Papers/paper-a.pdf")
    tid("gather-skipped").locator("summary").click()
    expect(tid("gather-skip-ALREADY_THERE")).to_contain_text("survey.pdf")
    expect(tid("gather-skip-NOT_ON_TARGET")).to_contain_text("hw1.pdf")
    expect(tid("gather-skip-NOT_ON_TARGET")).to_contain_text("gd 上沒有副本")
    me.shot("bulk-e2e-2-dry-run")
    tid("gather-cross").check()
    expect(tid("gather-cross-warning")).to_be_visible()
    expect(check).to_have_attribute("data-files", "2")                      # hw1.pdf comes over from Dropbox
    expect(tid("gather-sample").locator('li[data-cross="true"]')).to_contain_text("dbx:School/NTU/Courses/CS101/hw1.pdf")
    expect(tid("gather-skip-NOT_ON_TARGET")).to_have_count(0)
    me.shot("bulk-e2e-3-cross-cloud")
    tid("gather-stage").click()
    expect(tid("gather-dialog")).to_have_count(0)
    toast = page.locator('[data-testid="toast"][data-kind="success"]')
    expect(toast).to_contain_text("2 個檔案會搬進 gd:Research/Papers")
    toast.get_by_test_id("toast-action").click()
    expect(tid("workspace-host")).to_be_visible()
    tid("staged-toggle").click()
    expect(tid("staged-item")).to_have_count(1)
    expect(tid("staged-item").locator(".staged-title")).to_have_text("收集 2 個檔案 → gd:Research/Papers")
    me.shot("bulk-e2e-4-staged-step")

    # back in Explore the two copies that the draft moves carry their marks, the selection still stands
    tid("rail-explore").click()
    expect(tid("ex-staged-badge")).to_have_count(2)
    expect(tid("ex-sel-staged")).to_contain_text("2 個副本已暫存")
    page.locator('[data-testid="ex-result"]', has_text="paper-a.pdf").get_by_test_id("ex-expand").click()
    expect(tid("ex-staged")).to_contain_text("gd:Research/Papers/paper-a.pdf")
    me.shot("bulk-e2e-5-staged-marks")
    assert {n: _live(r) for n, r in world.roots.items()} == before, "staging touches no cloud"

    # ---------------------------------------------------------------- 3. preview, approve with the typed code, run, rescan
    tid("rail-workspace").click()
    tid("preview-button").click()
    modal = tid("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")
    me.shot("bulk-e2e-6-preview")
    approve_and_run(page, workspace_modal=True)
    modal.get_by_test_id("rescan-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", "rescanned", timeout=120_000)
    plan_id = int(re.search(r"\d+", modal.get_by_test_id("plan-id").inner_text()).group())
    modal.get_by_test_id("preview-close").click()

    after = {n: _live(r) for n, r in world.roots.items()}
    assert (world.roots["gd"] / "Research/Papers/paper-a.pdf").read_bytes() == PAPER_A
    assert (world.roots["gd"] / "Research/Papers/hw1.pdf").read_bytes() == hw1
    held = world.roots["dbx"] / QUARANTINE / str(plan_id) / "School/NTU/Courses/CS101/hw1.pdf"
    assert held.read_bytes() == hw1, "the Dropbox original waits in quarantine"
    expected_gd = {k: v for k, v in before["gd"].items() if k != "Desktop/paper-a.pdf"}
    expected_gd.update({"Research/Papers/paper-a.pdf": PAPER_A, "Research/Papers/hw1.pdf": hw1})
    assert after["gd"] == expected_gd
    assert after["dbx"] == {k: v for k, v in before["dbx"].items() if k != "School/NTU/Courses/CS101/hw1.pdf"}
    assert after["od"] == before["od"], "nothing else changed"

    # ---------------------------------------------------------------- 4. clean up the duplicates of one selected content
    page.evaluate("() => { location.hash = '#/explore?q=type:pdf copies:>=2'; }")
    expect(tid("ex-count")).to_have_text("顯示 1 / 1 筆")
    expect(tid("ex-result")).to_contain_text("survey.pdf")
    expect(tid("ex-selection-bar")).to_have_count(0)                      # another query: its own (empty) selection
    tid("ex-select-page").check()
    tid("ex-select-all-matching").click()
    expect(tid("ex-sel-count")).to_have_text("已選取 1 份內容")
    expect(tid("ex-sel-copies")).to_have_text("2 個副本")
    tid("ex-sel-dedupe").click()
    expect(tid("view-plan-new")).to_be_visible()
    expect(tid("dd-selection")).to_be_visible()
    expect(tid("dd-selection-count")).to_have_text("來自 Explore 的 1 份內容")
    expect(tid("dd-selection-query")).to_contain_text("type:pdf copies:>=2")
    expect(tid("estimate-headline")).to_contain_text("1 組內共有 1 個多餘副本")
    expect(tid("dd-group")).to_have_count(1)
    form_hash = hash_of(page)
    assert form_hash.startswith("#/plans/new/dedupe?selq=type:pdf copies:>=2") and "seln=1" in form_hash, form_hash
    me.shot("bulk-e2e-7-dedupe-selection")
    # The default keeper is the shallower path (Downloads/survey.pdf); the person keeps the one in Research/Papers instead.
    copy = tid("dd-copy").filter(has_text="gd:Research/Papers/survey.pdf")
    copy.get_by_test_id("dd-pick").click()
    expect(tid("dd-overridden")).to_be_visible()
    expect(copy).to_have_attribute("data-role", "keep")
    expect(tid("estimate-headline")).to_contain_text("1 組內共有 1 個多餘副本")
    tid("create-plan").click()
    expect(tid("view-plan-review")).to_be_visible()
    expect(tid("view-plan-review").locator(".view-title p")).to_have_text("清理 Explore 所選內容的重複副本（1 份內容）")
    dedupe_id = int(tid("view-plan-review").get_attribute("data-id"))
    me.shot("bulk-e2e-8-review")
    approve_and_run(page, workspace_modal=False)

    assert (world.roots["gd"] / "Research/Papers/survey.pdf").read_bytes() == survey, "one copy always stays"
    assert not (world.roots["gd"] / "Downloads/survey.pdf").exists()
    assert (world.roots["gd"] / QUARANTINE / str(dedupe_id) / "Downloads/survey.pdf").read_bytes() == survey
    conn = world.catalog()
    try:
        group = conn.execute(
            "SELECT e.content_group FROM entries e JOIN remotes r ON r.id = e.remote_id "
            "WHERE r.name = 'gd' AND e.path = 'Research/Papers/survey.pdf' AND e.present = 1").fetchone()[0]
        params = plans.get_plan(conn, dedupe_id, with_ops=False).params
        assert params["selection"]["query"]["ext"] == "pdf" and params["selection"]["query"]["copies_min"] == 2
        assert params["selection"]["expect_total"] == 1
        assert params["groups"] == [f"g:{group}"]
    finally:
        conn.close()

    # ---------------------------------------------------------------- 5. the catalog changes under the old address
    (world.roots["gd"] / "Misc").mkdir(exist_ok=True)
    (world.roots["gd"] / "Misc/survey-again.pdf").write_bytes(survey)
    (world.roots["gd"] / "Misc/paper-a-again.pdf").write_bytes(PAPER_A)
    _rescan_from_a_terminal(world, "gd")
    page.evaluate("() => { location.hash = '#/overview'; }")
    page.evaluate("(h) => { location.hash = h; }", form_hash)
    changed = tid("dd-selection-changed")
    expect(changed).to_be_visible()
    expect(changed).to_contain_text("結果在你選取之後改變了：當時是 1 份內容，現在是 2 份。什麼都沒有動。")
    expect(tid("create-plan")).to_be_disabled()
    me.shot("bulk-e2e-9-selection-changed")
    tid("dd-selection-update").click()
    expect(changed).to_have_count(0)
    expect(tid("estimate-headline")).to_contain_text("2 組內共有 2 個多餘副本")
    assert "seln=2" in hash_of(page)
    expect(tid("create-plan")).to_be_enabled()
    me.shot("bulk-e2e-10-use-current")
    # the one refusal the story provokes (a 409 for the changed selection) is the only thing the browser may log
    stray = [e for e in me.errors if "status of 409" not in e]
    assert not stray, f"JavaScript or network errors in the page: {stray}"
