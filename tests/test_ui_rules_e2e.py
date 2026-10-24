"""The Rules view end to end: real rclone, real catalog over local directories, real server, real Chromium.

A person edits the layout rules in the browser: starts a draft from the active rules, makes an error and sees it at its
line, fixes it and sees the live per-rule counts, asks Explain about one file, activates (the rules file on disk then holds
exactly the draft and config.toml is untouched), has the file edited behind the page's back (activation is refused with
RULES_ACTIVE_CHANGED and the outside edit is shown), builds a reorganize plan with the draft's rules, and receives a
proposal from the API token, which is counted but never active. docs/RULES.md.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402
from test_ui_e2e import API_TOKEN, UI_TOKEN, Page, World, _api, browser, make_world  # noqa: E402,F401

pytestmark = pytest.mark.ui

CONFIG = "# the person's config, never edited by drive-merge\n[safety]\nexecution_enabled = true\n"


@pytest.fixture
def world(make_world, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_bytes(CONFIG.encode())
    real_serve = World.serve

    def serve(self, **kw):                                   # the config knows its file before the server starts
        self.config.config_path = cfg / "config.toml"
        return real_serve(self, **kw)

    monkeypatch.setattr(World, "serve", serve)
    w = make_world()
    w.cfg = cfg
    return w


def test_edit_preview_activate_outside_edit_plan_and_proposal(world, browser):
    cfg = world.cfg
    rules_file, config_file = cfg / "rules.toml", cfg / "config.toml"
    config_before = config_file.read_bytes()
    me = Page(browser, world.url)
    me.ctx.close()
    me.ctx = browser.new_context(viewport={"width": 1480, "height": 920}, locale="en-US")
    me.page = me.ctx.new_page()
    me.page.set_default_timeout(20_000)
    me.errors = []
    me.page.on("pageerror", lambda e: me.errors.append(str(e)))
    page = me.page
    tid = page.get_by_test_id
    try:
        page.goto(f"{world.url}/ui/#token={UI_TOKEN}")
        expect(tid("rail-rules")).to_be_visible()
        page.goto(f"{world.url}/ui/#/rules")
        expect(tid("view-rules")).to_be_visible()
        expect(tid("rules-active")).to_have_attribute("data-source", "default")
        assert not rules_file.exists()

        # 1. A draft from the active rules, with a new first rule that has a misspelled category.
        tid("rules-new-active").click()
        expect(tid("view-rules-edit")).to_be_visible()
        text = tid("rules-text").input_value()
        block = '[[rule]]\nname = "pdfs"\ncategory = ["foto"]\ntarget = "PdfBox/{name}"\n\n'
        assert text.count("[[rule]]") > 3
        i = text.index("[[rule]]")
        j = text.rfind("\n", 0, i) + 1                        # keep the comment lines above the first rule where they are
        draft = text[:j] + block + text[j:]
        tid("rules-text").fill(draft)
        err = tid("rules-error-1")
        expect(err).to_be_visible()
        expect(err).to_have_attribute("data-code", "UNKNOWN_CATEGORY")
        line = int(page.locator('[data-testid="rules-gutter"] [data-mark="error"]').first.get_attribute("data-line"))
        assert draft.split("\n")[line - 1].startswith("category")

        # 2. Fix it: the live preview shows the new rule's count.
        fixed = draft.replace('"foto"', '"pdf"')
        tid("rules-text").fill(fixed)
        expect(tid("rules-error-1")).to_have_count(0)
        row = tid("rules-rule-row-pdfs")
        expect(row).to_be_visible()
        expect(row).to_have_attribute("data-edited", "2")      # survey.pdf and hw1.pdf
        expect(tid("rules-preview-exact")).to_be_visible()
        before = {r.get_attribute("data-testid"): r.get_attribute("data-edited") for r in page.locator('[data-testid^="rules-rule-row-"]').all()}
        assert before["rules-rule-row-pdfs"] == "2"
        tid("rules-explain-input").fill("gd:Research/Papers/survey.pdf")
        tid("rules-explain-go").click()
        expect(tid("rules-explain-edited")).to_contain_text("PdfBox/survey.pdf")
        me.shot("rules-01-preview")

        # 3. Activate by a human action: the file holds exactly the draft, config.toml is untouched.
        expect(tid("rules-save-state")).to_contain_text("aved", timeout=10_000)
        tid("rules-activate").click()
        expect(tid("rules-activate-confirm")).to_be_enabled()
        tid("rules-activate-confirm").click()
        expect(tid("rules-activate-dialog")).to_have_count(0)
        assert rules_file.read_text(encoding="utf-8") == fixed
        assert config_file.read_bytes() == config_before
        assert sorted(p.name for p in cfg.iterdir()) == ["config.toml", "rules.toml"], "no temporary file left behind"
        expect(tid("rules-active")).to_have_attribute("data-source", "file")

        # 4. A second draft; the file is edited behind the page's back while the dialog is open.
        tid("rules-new-active").click()
        expect(tid("view-rules-edit")).to_be_visible()
        second = tid("rules-text").input_value() + "# second draft\n"
        tid("rules-text").fill(second)
        expect(tid("rules-save-state")).to_contain_text("aved", timeout=10_000)
        tid("rules-activate").click()
        expect(tid("rules-activate-confirm")).to_be_enabled()
        outside = fixed + "# edited in an editor\n"
        rules_file.write_text(outside, encoding="utf-8")
        tid("rules-activate-confirm").click()
        expect(tid("rules-activate-error")).to_have_attribute("data-code", "RULES_ACTIVE_CHANGED")
        assert rules_file.read_text(encoding="utf-8") == outside, "a refused activation leaves the file alone"
        tid("rules-activate-cancel").click()
        expect(tid("rules-activate-dialog")).to_have_count(0)

        # 5. The draft goes into a reorganize plan, which records the rules text and where it came from.
        tid("rules-use-plan").click()
        tid("rules-use-reorganize").click()
        expect(tid("view-plan-new")).to_have_attribute("data-kind", "reorganize")
        tid("reorg-remote").select_option("gd")
        expect(tid("reorg-stats")).to_be_visible()
        tid("create-plan").click()
        expect(tid("view-plan-review")).to_be_visible()
        expect(tid("plan-rules-box")).to_contain_text("not the active rules")
        status, listing = _api(world, "GET", "/v1/plans/summaries")
        plan_id = listing["plans"][0]["id"]
        status, plan = _api(world, "GET", f"/v1/plans/{plan_id}")
        assert status == 200 and plan["kind"] == "reorganize"
        assert plan["params"]["rules"] == second
        ref = plan["params"]["rules_ref"]
        assert ref["source"] == "version" and ref["version"] and len(ref["sha256"]) == 64
        _, version = _api(world, "GET", f"/v1/rules/versions/{ref['version']}")
        assert version["text"] == second and version["status"] == "draft"

        # 6. The outside edit is shown as such in the Rules view.
        page.goto(f"{world.url}/ui/#/rules")
        expect(tid("rules-outside-change")).to_be_visible()
        assert rules_file.read_text(encoding="utf-8") == outside

        # 7. A proposal from the API token is counted, listed and not active.
        status, prop = _api(world, "POST", "/v1/rules/versions", {"text": outside + "# proposed by a script\n", "title": "Script proposal"})
        assert status == 201 and prop["status"] == "proposed" and prop["created_by"] == "api"
        deadline = time.monotonic() + 20
        page.reload()
        expect(tid("rail-rules-badge")).to_have_attribute("data-count", "1", timeout=max(1000, int((deadline - time.monotonic()) * 1000)))
        expect(tid(f"rules-proposal-row-{prop['id']}")).to_be_visible()
        _, state = _api(world, "GET", "/v1/rules")
        assert state["active"]["version_id"] != prop["id"]
        assert state["active"]["text"] == outside
        assert rules_file.read_text(encoding="utf-8") == outside
        assert config_file.read_bytes() == config_before
        me.shot("rules-02-proposal")
        me.finish()
    finally:
        me.close()
