"""Out of the box, end to end (docs/ONBOARDING.md, contract F): real server, real browser, real catalog, a fake rclone.

No config exists at the start. The person opens the page, connects a Dropbox (one button) and a Google Drive (the
client id/secret guide), both read-only; syncs both in parallel; looks at Free space on Dropbox (proven / probable /
only here), verifies the probable files, allows changes on Dropbox (a second sign-in), creates the plan, reviews it,
types the approval code, runs it and purges. Then the disk is checked: the freed Dropbox files are gone for good (not
even in quarantine), the Drive copies are intact, files that exist only on Dropbox are untouched, and no token
string appears in any page or network response of the run.

The two "clouds" are local folders (tests/fakes/rclone passes everything but sign-in through to the real rclone);
the sign-ins are approved by writing the trigger file the fake waits for.

Screenshots of every step go to $DRIVEMERGE_UI_SHOTS (default /tmp/claude-0/ui-shots) as final-*.png.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import socket
import threading
import time
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")
import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from drivemerge import cli, db, ui_api  # noqa: E402
from drivemerge.config import load_config  # noqa: E402
from drivemerge.service import Service  # noqa: E402

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH"),
]

SHOTS = Path(os.environ.get("DRIVEMERGE_UI_SHOTS", "/tmp/claude-0/ui-shots"))
FAKES = Path(__file__).parent / "fakes"
UI_TOKEN = "onboarding-e2e-ui-token"
QUARANTINE = ".drive-merge-quarantine"
CLIENT_ID = "1234567890-abcdefghijklmnop.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-e2eSecretValue0123456789"
TOKEN_RE = re.compile(r"fake-(?:at|rt)-[0-9a-f]{16}")


def _blobs() -> dict[str, dict[str, bytes]]:
    """cloud -> relative path -> bytes.

    proven    on Dropbox, the same bytes on Drive under other folders (bridged before the story starts, the way
              an earlier `drivemerge bridge` would have)
    probable  same name and size on Drive, no common strong hash yet: "Verify" settles it. `scans/*` are identical
              copies (they become proven), `invoices/inv-0*.pdf` only look alike (they turn out to be only here)
    only here nothing like it on Drive
    """
    rnd = random.Random(7)

    def blob(n: int) -> bytes:
        return bytes(rnd.getrandbits(8) for _ in range(n))

    proven = {f"Archive/old-{i}.bin": blob(3000 + 100 * i) for i in range(3)}
    same = {f"scans/scan-{i}.jpg": blob(2000 + 50 * i) for i in range(2)}
    liar_dbx = {f"invoices/inv-0{i}.pdf": blob(1500 + 10 * i) for i in range(2)}
    liar_gd = {f"Finance/inv-0{i}.pdf": blob(1500 + 10 * i) for i in range(2)}  # same size, other bytes
    mine = {"Notes/diary.txt": blob(700), "Photos/me.jpg": blob(4100)}
    dbx = {**{k: v for k, v in proven.items()}, **same, **liar_dbx, **mine}
    gd = {f"Backup/{k}": v for k, v in proven.items()}
    gd.update({f"Pictures/{Path(k).name}": v for k, v in same.items()})
    gd.update(liar_gd)
    gd["Docs/report.docx"] = blob(900)
    return {"dropbox": dbx, "gdrive": gd}


def _live(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not str(p.relative_to(root)).startswith(QUARANTINE)}


class Onboarding:
    """The temp home, the clouds on disk, the server in a thread, and the sign-in approvals."""

    def __init__(self, tmp: Path, monkeypatch, files: dict[str, dict[str, bytes]] | None = None):
        self.tmp = tmp
        self.home = tmp / "home"
        self.config_path = self.home / ".config" / "drive-merge" / "config.toml"
        self.auth_dir = tmp / "auth"
        self.auth_dir.mkdir()
        self.files = files if files is not None else _blobs()
        self.roots = {n: tmp / "clouds" / n for n in self.files}
        for name, root in self.roots.items():
            root.mkdir(parents=True)
            for rel, data in self.files[name].items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(data)
        self.before = {n: _live(r) for n, r in self.roots.items()}
        monkeypatch.setenv("HOME", str(self.home))
        # The fake rclone behind a wrapper that makes a listing take a moment while $slow exists, so the page can
        # be seen mid-sync (two tiny folders would be listed before the first poll).
        real = shutil.which("rclone")
        assert real
        self.slow = tmp / "slow-listings"
        wrapper = tmp / "bin" / "rclone"
        wrapper.parent.mkdir()
        wrapper.write_text('#!/bin/sh\nfor a in "$@"; do\n'
                           f'  if [ "$a" = lsjson ] && [ -e "{self.slow}" ]; then sleep 8; fi\n'
                           'done\n'
                           f'exec "{FAKES / "rclone"}" "$@"\n')
        wrapper.chmod(0o755)
        monkeypatch.setenv("FAKE_RCLONE_REAL", real)
        monkeypatch.setenv("PATH", f"{wrapper.parent}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_RCLONE_AUTH_DIR", str(self.auth_dir))
        monkeypatch.delenv("RCLONE_CONFIG", raising=False)
        # `drivemerge ui` with no config runs init first; the test then switches execution on, as a person would
        # by editing the file, before the server loads it.
        self.home.mkdir()
        cli._init_config(self.config_path)
        text = self.config_path.read_text(encoding="utf-8")
        assert "execution_enabled = false" in text
        self.config_path.write_text(text.replace("execution_enabled = false", "execution_enabled = true"), encoding="utf-8")
        self.config = load_config(self.config_path)
        assert self.config.safety.execution_enabled and not self.config.remotes
        self.service = Service(self.config, config_path=self.config_path)
        app = ui_api.create_ui_app(self.service, config=self.config, api_token=self.config.api.token,
                                   ui_token=UI_TOKEN, approvals_enabled=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=self.server.run, daemon=True).start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started
        self.url = f"http://127.0.0.1:{port}"
        self._approved: set[str] = set()

    def stop(self) -> None:
        self.server.should_exit = True
        time.sleep(0.2)
        self.service.conn.close()

    def approve_signin(self, cloud: str, account: str, *, flavour: str | None = None) -> dict:
        """What the person does in the provider's tab: wait for the sign-in to start, then approve it."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for started in sorted(self.auth_dir.glob("*.started")):
                state = started.name[: -len(".started")]
                if state not in self._approved:
                    self._approved.add(state)
                    (self.auth_dir / state).write_text(json.dumps({"root": str(self.roots[cloud]), "account": account}))
                    return json.loads(started.read_text())
            time.sleep(0.05)
        raise AssertionError("rclone authorize never started")

    def tokens_on_disk(self) -> set[str]:
        conf = (self.config_path.parent / "rclone.conf").read_text(encoding="utf-8")
        return set(TOKEN_RE.findall(conf))


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _run_story(browser, world: Onboarding, locale: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale=locale)
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    errors: list[str] = []
    seen_bodies: list[tuple[str, str]] = []    # (url, text) of every response in the run
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    def keep(response) -> None:
        try:
            seen_bodies.append((response.url, response.text()))
        except Exception:      # redirects, streams, bodies already gone
            pass
    page.on("response", keep)
    pages_html: list[str] = []

    def tid(name: str):
        return page.get_by_test_id(name)

    def shot(name: str, *, toasts: bool = False) -> None:
        if not toasts:      # earlier toasts pile up because the test is fast; a person would have seen them leave
            for close in page.locator(".toast .icon-btn").all():
                close.click()
            expect(page.locator(".toast")).to_have_count(0)
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / f"final-{name}.png"))

    def snapshot() -> None:
        pages_html.append(page.content())

    # ---------------------------------------------------------------- 1. welcome
    page.goto(f"{world.url}/ui/#token={UI_TOKEN}")
    expect(tid("welcome")).to_be_visible()
    expect(tid("provider-tiles")).to_be_visible()
    for p in ("drive", "onedrive", "dropbox"):
        expect(tid(f"provider-{p}")).to_be_visible()
    expect(tid("rclone-chip")).to_be_visible()
    shot("1-welcome" if locale == "en-US" else "welcome-zh")
    snapshot()
    if locale != "en-US":
        # the Traditional Chinese Welcome screen is all this run needs
        assert not errors, errors
        ctx.close()
        return

    # ---------------------------------------------------------------- 2. connect a fake Dropbox: one button
    tid("connect-dropbox").click()
    expect(tid("connect-dialog")).to_be_visible()
    expect(tid("dropbox-text")).to_be_visible()
    tid("connect-start").click()
    expect(tid("connect-waiting")).to_be_visible()
    expect(tid("auth-link")).to_have_attribute("href", re.compile(r"^http://127\.0\.0\.1:53682/auth\?state="))
    started = world.approve_signin("dropbox", "me@dropbox.example")
    assert started["type"] == "dropbox"
    expect(tid("conn-card-dropbox")).to_be_visible(timeout=30_000)
    expect(tid("connect-dialog")).to_have_count(0)

    # ---------------------------------------------------------------- 3. connect a fake Google Drive: the guide
    tid("connect-drive").click()
    expect(tid("google-guide")).to_be_visible()
    shot("2-google-guide")
    tid("client-id").fill(CLIENT_ID)
    tid("client-secret").fill(CLIENT_SECRET)
    tid("google-confirm-prod").check()
    tid("connect-start").click()
    expect(tid("connect-waiting")).to_be_visible()
    started = world.approve_signin("gdrive", "me@gmail.example")
    assert started["type"] == "drive" and started["client_id"] == CLIENT_ID
    expect(tid("conn-card-gdrive")).to_be_visible(timeout=30_000)
    expect(tid("connect-dialog")).to_have_count(0)

    for name, account in (("dropbox", "me@dropbox.example"), ("gdrive", "me@gmail.example")):
        card = tid(f"conn-card-{name}")
        expect(card).to_have_attribute("data-state", "ro")
        expect(tid(f"conn-state-{name}")).to_contain_text("read-only")
        expect(tid(f"conn-account-{name}")).to_have_text(account)
        expect(tid(f"conn-quota-{name}")).to_contain_text(re.compile(r"\d"), timeout=30_000)
        expect(tid(f"conn-quota-{name}")).not_to_contain_text("unknown")
    shot("3-connected-cards")
    snapshot()
    assert len(world.tokens_on_disk()) >= 2      # the tokens are in drive-merge's own rclone.conf, nowhere else

    # ---------------------------------------------------------------- 4. sync both clouds
    world.slow.touch()
    tid("welcome-sync").click()
    pill = tid("sync-pill")
    expect(pill).to_be_visible()
    pill.click()
    expect(tid("sync-popover")).to_be_visible()
    expect(tid("sync-row-dropbox")).to_be_visible()
    expect(tid("sync-row-gdrive")).to_be_visible()
    expect(page.locator('[data-testid^="sync-row-"][data-state="listing"]').first).to_be_visible()   # both are being listed
    shot("4-syncing")
    world.slow.unlink()
    expect(page.get_by_test_id("view-overview")).to_be_visible(timeout=60_000)
    expect(tid("cloud-card-dropbox")).to_be_visible()
    expect(tid("cloud-card-gdrive")).to_be_visible()
    expect(tid("sync-button")).to_be_enabled(timeout=60_000)
    expect(tid("sync-pill-text")).to_contain_text("Synced")
    live_dbx = len(world.before["dropbox"])
    live_gd = len(world.before["gdrive"])
    expect(tid("cloud-card-dropbox").get_by_test_id("cloud-files")).to_have_text(str(live_dbx))
    expect(tid("cloud-card-gdrive").get_by_test_id("cloud-files")).to_have_text(str(live_gd))
    page.keyboard.press("Escape")
    shot("5-overview-after-sync")
    snapshot()

    # An earlier `drivemerge bridge` in a terminal proved the "Archive" files (Dropbox's hash vs Drive's sha256).
    terminal = Service(world.config, config_path=world.config_path)
    try:
        conn = terminal.conn
        ids = [r[0] for r in conn.execute(
            "SELECT e.id FROM entries e JOIN remotes r ON r.id = e.remote_id "
            "WHERE r.name IN ('dropbox', 'gdrive') AND e.present = 1 AND "
            "(e.path LIKE 'Archive/%' OR e.path LIKE 'Backup/Archive/%')")]
        assert len(ids) == 6
        result = terminal.bridge(entry_ids=ids)
        assert result["bridged"] == 6 and not result["failed"], result
    finally:
        terminal.conn.close()

    # ---------------------------------------------------------------- 5. free space on dropbox
    page.goto(f"{world.url}/ui/#/overview")
    tid("cloud-free-dropbox").click()
    expect(tid("view-free")).to_have_attribute("data-remote", "dropbox")
    expect(tid("free-buckets")).to_be_visible()

    def bucket(name: str) -> tuple[int, int]:
        el = tid(f"bucket-{name}")
        return int(el.get_attribute("data-files")), int(el.get_attribute("data-bytes"))

    expect(tid("bucket-proven")).to_have_attribute("data-files", "3")
    arch = sum(len(v) for k, v in world.files["dropbox"].items() if k.startswith("Archive/"))
    assert bucket("proven") == (3, arch)
    expect(tid("proven-by-gdrive")).to_be_visible()
    assert int(tid("proven-by-gdrive").get_attribute("data-bytes")) == arch
    same_bytes = sum(len(v) for k, v in world.files["dropbox"].items() if k.startswith(("scans/", "invoices/")))
    assert bucket("probable") == (4, same_bytes)
    mine_bytes = sum(len(v) for k, v in world.files["dropbox"].items() if k.startswith(("Notes/", "Photos/")))
    assert bucket("only_here") == (2, mine_bytes)
    expect(tid("free-quota")).to_be_visible()
    shot("6-free-space")
    tid("view-lang").click()
    expect(page.locator("html")).to_have_attribute("lang", "zh-Hant-TW")
    shot("free-space-zh")
    tid("view-lang").click()
    expect(page.locator("html")).to_have_attribute("lang", re.compile("^en"))
    snapshot()

    # ---------------------------------------------------------------- 6. verify: probable becomes proven or only here
    tid("free-verify").click()
    expect(tid("bucket-probable")).to_have_attribute("data-files", "0", timeout=60_000)
    scans = sum(len(v) for k, v in world.files["dropbox"].items() if k.startswith("scans/"))
    liars = sum(len(v) for k, v in world.files["dropbox"].items() if k.startswith("invoices/"))
    assert bucket("proven") == (5, arch + scans)
    assert bucket("only_here") == (4, mine_bytes + liars)
    shot("7-free-space-after-verify")
    snapshot()

    # ---------------------------------------------------------------- 7. allow changes on dropbox (a second sign-in)
    page.goto(f"{world.url}/ui/#/clouds")
    expect(tid("conn-card-dropbox")).to_have_attribute("data-state", "ro")
    tid("conn-allow-dropbox").click()
    expect(tid("allow-text")).to_be_visible()
    tid("connect-start").click()
    world.approve_signin("dropbox", "me@dropbox.example")
    expect(tid("conn-card-dropbox")).to_have_attribute("data-state", "rw", timeout=30_000)
    expect(tid("conn-card-gdrive")).to_have_attribute("data-state", "ro")

    # ---------------------------------------------------------------- 8. create the plan, review it, approve, run
    tid("conn-free-dropbox").click()
    expect(tid("free-create")).to_be_enabled()
    tid("free-create").click()
    expect(tid("view-plan-review")).to_be_visible()
    expect(tid("plan-status")).to_have_attribute("data-status", "simulated")
    expect(tid("approval-area")).to_be_visible()
    shot("8-plan-review")
    snapshot()
    code = tid("approval-code").inner_text().strip()
    tid("approval-input").fill(code)
    tid("approval-button").click()
    expect(tid("plan-approved")).to_be_visible()
    tid("plan-run").click()
    expect(tid("purge-box")).to_be_visible(timeout=90_000)
    expect(tid("purge-box")).to_have_attribute("data-phase", "idle")
    # quarantined, not yet purged: the bytes are still on Dropbox's disk, in quarantine
    quarantined = [p for p in world.roots["dropbox"].rglob("*") if p.is_file() and QUARANTINE in p.parts]
    assert len(quarantined) == 5

    # ---------------------------------------------------------------- 9. purge now (typed 'purge')
    tid("purge-now").click()
    expect(tid("purge-dialog")).to_be_visible()
    tid("purge-input").fill("purge")
    shot("9-purge-confirm")
    tid("purge-confirm").click()
    expect(tid("purge-done")).to_be_visible(timeout=60_000)
    shot("10-done")
    snapshot()

    # ---------------------------------------------------------------- the disk
    freed = {k for k in world.before["dropbox"] if k.startswith(("Archive/", "scans/"))}
    now_dbx = _live(world.roots["dropbox"])
    assert freed and not (freed & set(now_dbx))
    assert not [p for p in world.roots["dropbox"].rglob("*") if p.is_file() and QUARANTINE in p.parts], \
        "purged files must not linger in quarantine"
    for rel, data in world.before["dropbox"].items():        # everything that was not freed is untouched
        if rel not in freed:
            assert now_dbx[rel] == data, rel
    assert set(now_dbx) == set(world.before["dropbox"]) - freed
    assert _live(world.roots["gdrive"]) == world.before["gdrive"]      # the Drive copies are intact, byte for byte
    for rel in freed:      # each freed file still exists, byte for byte, on Drive
        copy = ("Backup/" + rel) if rel.startswith("Archive/") else "Pictures/" + Path(rel).name
        assert world.roots["gdrive"].joinpath(copy).read_bytes() == world.before["dropbox"][rel], rel
    for rel in world.before["dropbox"]:   # the look-alike invoices and the person's own files stay on Dropbox
        if rel.startswith(("invoices/", "Notes/", "Photos/")):
            assert world.roots["dropbox"].joinpath(rel).read_bytes() == world.before["dropbox"][rel]

    # ---------------------------------------------------------------- no secrets anywhere
    snapshot()
    secrets_ = world.tokens_on_disk() | {CLIENT_SECRET}
    assert len(secrets_) >= 3
    for html_ in pages_html:
        for s in secrets_:
            assert s not in html_, "a secret is in the page"
    for url, body in seen_bodies:
        for s in secrets_:
            assert s not in body, f"a secret is in the response of {url}"
    assert not errors, f"JavaScript or network errors: {errors}"
    ctx.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = Onboarding(tmp_path, monkeypatch)
    yield w
    w.stop()


def test_first_start_to_freed_space_with_no_config(browser, world):
    _run_story(browser, world, "en-US")
    conn = db.connect(world.config.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM plans WHERE kind = 'release' AND status = 'done'").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_welcome_screen_in_traditional_chinese(browser, world):
    _run_story(browser, world, "zh-TW")


# --------------------------------------------------------------------------- iCloud, import and a pasted token

APPLE_ID = "me@icloud.example"
APPLE_PASSWORD = "Hunter2-iCloud-Pw!"
GOOD_CODE = "123456"
WRONG_CODE = "000000"
PERSONAL_AT = "fake-at-aaaaaaaaaaaaaaaa"
PERSONAL_RT = "fake-rt-bbbbbbbbbbbbbbbb"
PASTED_AT = "fake-at-cccccccccccccccc"
PASTED_RT = "fake-rt-dddddddddddddddd"


@pytest.fixture
def world3(tmp_path, monkeypatch):
    """Three clouds nobody has connected yet: an iCloud (Apple ID + 2FA), a remote the person already has in their own
    rclone config ("work"), and a OneDrive reached with a token made on another computer."""
    files = {name: {f"{name}/file-{i}.bin": bytes([i]) * (500 + 100 * i) for i in range(3)} for name in ("icloud", "work", "onedrive")}
    monkeypatch.setenv("FAKE_RCLONE_ROOT_BASE", str(tmp_path / "clouds"))     # an iCloud has no sign-in file to say where it is
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(tmp_path / "rclone.log"))
    w = Onboarding(tmp_path, monkeypatch, files)
    personal = w.home / ".config" / "rclone" / "rclone.conf"
    personal.parent.mkdir(parents=True)
    personal.write_text(
        "[work]\ntype = dropbox\n"
        f'token = {{"access_token":"{PERSONAL_AT}","token_type":"bearer","refresh_token":"{PERSONAL_RT}","expiry":"2030-01-01T00:00:00Z"}}\n'
        f"fake_root = {w.roots['work']}\n\n"
        "[vault]\ntype = crypt\nremote = work:vault\npassword = OBSCURED-secret\n", encoding="utf-8")
    w.personal = personal
    w.personal_before = personal.read_bytes()
    w.config_before = w.config_path.read_text(encoding="utf-8")
    yield w
    w.stop()


def test_icloud_an_imported_remote_and_a_pasted_token_end_to_end(browser, world3):
    world = world3
    SHOTS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    errors: list[str] = []
    bodies: list[tuple[str, str]] = []
    requested: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("request", lambda r: requested.append(r.url))

    def keep(response) -> None:
        try:
            bodies.append((response.url, response.text()))
        except Exception:
            pass
    page.on("response", keep)
    pages_html: list[str] = []

    def tid(name: str):
        return page.get_by_test_id(name)

    def where_it_could_leak() -> str:
        """Everything on the page a secret could be left in: text, address, storage, cookies, titles."""
        storage = page.evaluate("() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage), document.cookie])")
        pages_html.append(page.content())
        return "\n".join([page.inner_text("body"), page.url, storage, page.title(), *requested])

    # ---------------------------------------------------------------- the page offers all four providers and the import list
    page.goto(f"{world.url}/ui/#token={UI_TOKEN}")
    expect(tid("welcome")).to_be_visible()
    expect(tid("provider-iclouddrive")).to_be_visible()
    expect(tid("import-list")).to_be_visible()
    expect(tid("import-work")).to_have_attribute("data-supported", "true")
    expect(tid("import-vault")).to_have_attribute("data-supported", "false")      # a crypt remote wraps another one
    expect(tid("import-add-vault")).to_be_disabled()
    page.screenshot(path=str(SHOTS / "final-icloud-0-welcome.png"))

    # ---------------------------------------------------------------- iCloud: Apple ID + password, then the 2FA question
    tid("connect-iclouddrive").click()
    expect(tid("connect-icloud")).to_be_visible()
    expect(tid("connect-start")).to_be_disabled()
    tid("connect-apple-id").fill(APPLE_ID)
    tid("connect-apple-password").fill(APPLE_PASSWORD)
    tid("connect-start").click()
    expect(tid("connect-question")).to_be_visible()
    expect(tid("connect-q-name")).to_have_text("config_2fa")
    expect(tid("connect-q-retry")).to_have_count(0)
    expect(tid("connect-apple-password")).to_have_count(0)
    leak = where_it_could_leak()
    assert APPLE_PASSWORD not in leak
    page.screenshot(path=str(SHOTS / "final-icloud-1-question.png"))
    tid("connect-q-input").fill(WRONG_CODE)
    tid("connect-q-continue").click()
    expect(tid("connect-q-retry")).to_be_visible()                  # rclone refused: the same question again, with the retry line
    expect(tid("connect-q-input")).to_have_value("")
    assert WRONG_CODE not in where_it_could_leak() and APPLE_PASSWORD not in where_it_could_leak()
    page.screenshot(path=str(SHOTS / "final-icloud-2-retry.png"))
    tid("connect-q-input").fill(GOOD_CODE)
    tid("connect-q-continue").click()
    expect(tid("conn-card-icloud")).to_be_visible(timeout=30_000)
    expect(tid("connect-dialog")).to_have_count(0)
    expect(tid("conn-card-icloud")).to_have_attribute("data-state", "ro")
    expect(tid("conn-unscoped-icloud")).to_be_visible()
    expect(tid("conn-quota-icloud")).not_to_contain_text("unknown", timeout=30_000)       # the automatic Test listed the fake folder
    leak = where_it_could_leak()
    assert APPLE_PASSWORD not in leak and GOOD_CODE not in leak and WRONG_CODE not in leak

    # ---------------------------------------------------------------- import the remote from the person's own rclone config
    tid("import-add-work").click()
    expect(tid("conn-card-work")).to_be_visible(timeout=30_000)
    expect(tid("conn-card-work")).to_have_attribute("data-state", "ro")
    expect(tid("conn-unscoped-work")).to_be_visible()
    expect(tid("import-add-work")).to_have_count(0)                      # imported: no second copy offered
    expect(tid("import-done-work")).to_be_visible()
    expect(tid("conn-quota-work")).not_to_contain_text("unknown", timeout=30_000)
    leak = where_it_could_leak()
    assert PERSONAL_AT not in leak and PERSONAL_RT not in leak

    # ---------------------------------------------------------------- OneDrive with a token made on another computer
    tid("connect-onedrive").click()
    tid("conn-paste-toggle").click()
    command = tid("conn-paste-command").inner_text()
    assert command.startswith("rclone authorize onedrive "), command
    assert "'--onedrive-access-scopes=Files.Read Files.Read.All Sites.Read.All offline_access'" in command      # quoted: the value has spaces
    assert "CLIENT_SECRET" not in command           # no client pair for OneDrive
    expect(tid("conn-paste-scope")).to_be_visible()
    expect(tid("conn-paste-submit")).to_be_disabled()
    block = (
        "2026/10/24 10:00:00 NOTICE: Make sure your Redirect URL is set to \"http://localhost:53682/\" in your custom config.\n"
        "Paste the following into your remote machine --->\n"
        + json.dumps({"access_token": PASTED_AT, "token_type": "bearer", "refresh_token": PASTED_RT,
                      "expiry": "2030-01-01T00:00:00Z", "fake_root": str(world.roots["onedrive"]), "fake_account": "me@outlook.example"})
        + "\n<---End paste\n")
    tid("conn-paste-token").fill(block)
    expect(tid("conn-paste-submit")).to_be_enabled()
    tid("conn-paste-submit").click()
    expect(tid("conn-card-onedrive")).to_be_visible(timeout=30_000)
    expect(tid("connect-dialog")).to_have_count(0)
    expect(tid("conn-card-onedrive")).to_have_attribute("data-state", "ro")
    expect(tid("conn-account-onedrive")).to_have_text("me@outlook.example", timeout=30_000)
    leak = where_it_could_leak()
    assert PASTED_AT not in leak and PASTED_RT not in leak
    page.screenshot(path=str(SHOTS / "final-icloud-3-three-cards.png"))

    # ---------------------------------------------------------------- sync all three clouds
    tid("welcome-sync").click()
    expect(page.get_by_test_id("view-overview")).to_be_visible(timeout=60_000)
    for name in ("icloud", "work", "onedrive"):
        expect(tid(f"cloud-card-{name}")).to_be_visible()
        expect(tid(f"cloud-card-{name}").get_by_test_id("cloud-files")).to_have_text("3")
    leak = where_it_could_leak()

    # ---------------------------------------------------------------- the files drive-merge wrote
    conns = tomllib.loads((world.config_path.parent / "connections.toml").read_text(encoding="utf-8"))
    entries = {e["name"]: e for e in conns["remotes"]}
    assert set(entries) == {"icloud", "work", "onedrive"}
    assert entries["icloud"]["provider"] == "iclouddrive" and entries["icloud"]["read"] == "icloud-ro:"
    assert entries["icloud"]["scoped"] is False and "write" not in entries["icloud"] and "imported_from" not in entries["icloud"]
    assert entries["work"]["provider"] == "dropbox" and entries["work"]["read"] == "work-ro:"
    assert entries["work"]["scoped"] is False and entries["work"]["imported_from"] == "work" and "write" not in entries["work"]
    assert entries["onedrive"]["provider"] == "onedrive" and entries["onedrive"]["read"] == "onedrive-ro:"
    assert entries["onedrive"].get("scoped", True) is True and "write" not in entries["onedrive"]
    conf_text = (world.config_path.parent / "rclone.conf").read_text(encoding="utf-8")
    sections = set(re.findall(r"^\[(.+)\]$", conf_text, re.M))
    assert sections == {"icloud-ro", "work-ro", "onedrive-ro"}, sections            # no half-made remote, no -rw, nothing from crypt
    assert APPLE_PASSWORD not in conf_text                                       # rclone stores it obscured
    assert PERSONAL_AT in conf_text and PASTED_AT in conf_text                    # the tokens live in drive-merge's own rclone.conf
    assert world.personal.read_bytes() == world.personal_before                   # the person's own config is never written
    assert APPLE_ID in conf_text
    toml_text = (world.config_path.parent / "connections.toml").read_text(encoding="utf-8")
    assert APPLE_PASSWORD not in toml_text and PERSONAL_AT not in toml_text and PASTED_AT not in toml_text and GOOD_CODE not in toml_text
    assert world.config_path.read_text(encoding="utf-8") == world.config_before      # config.toml is the person's file: untouched

    # ---------------------------------------------------------------- no secret anywhere
    secrets_ = {APPLE_PASSWORD, GOOD_CODE, WRONG_CODE, PERSONAL_AT, PERSONAL_RT, PASTED_AT, PASTED_RT}
    for s_ in secrets_ - {GOOD_CODE, WRONG_CODE}:
        assert s_ not in leak, "a secret is on the page, in the address or in storage"
    for html_ in pages_html:
        for s_ in secrets_ - {PASTED_AT, PASTED_RT, GOOD_CODE, WRONG_CODE}:      # (the pasted token sat in the textarea until sent)
            assert s_ not in html_, "a secret is in the page"
    for url, body in bodies:
        for s_ in secrets_ - {GOOD_CODE, WRONG_CODE}:
            assert s_ not in body, f"a secret is in the response of {url}"
    for url in requested:
        for s_ in secrets_:
            assert s_ not in url, "a secret is in a request address"
    log = Path(os.environ["FAKE_RCLONE_LOG"]).read_text(encoding="utf-8")
    for s_ in secrets_ - {GOOD_CODE, WRONG_CODE}:
        assert s_ not in log, "a secret reached the rclone call log"
    assert not errors, f"JavaScript or network errors: {errors}"
    ctx.close()
