"""Connections against the fake rclone in tests/fakes (a cloud is a local folder)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import stat
import sys
import time
import zipfile
from pathlib import Path

import pytest

from drivemerge import config as config_mod
from drivemerge import connections as C

FAKE = str(Path(__file__).parent / "fakes" / "rclone")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix fake rclone")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setenv("FAKE_RCLONE_ROOT_BASE", str(tmp_path / "clouds"))
    for var in ("FAKE_RCLONE_AUTH_MODE", "FAKE_RCLONE_TEST_FAIL", "FAKE_RCLONE_REAL", "FAKE_RCLONE_LOG",
                "FAKE_RCLONE_DRIVES", "FAKE_RCLONE_FAIL_CREATE", "FAKE_RCLONE_FAIL_CONTINUE",
                "FAKE_RCLONE_ECHO_ANSWER", "FAKE_RCLONE_ECHO_CREATE", "FAKE_RCLONE_HANG_LSJSON", "RCLONE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    yield
    for f in list(C._FLOWS.values()):
        C.cancel_flow(f.id)
    C._FLOWS.clear()
    C._RESERVED.clear()


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    main = d / "config.toml"
    main.write_text(f'[rclone]\nbinary = "{FAKE}"\n[api]\ntoken = "t"\n')
    return config_mod.load_config(main)


def _sign_in(flow, root=None, account=None):
    meta = {}
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
        meta["root"] = str(root)
    if account:
        meta["account"] = account
    state = flow.auth_url.split("state=")[1]
    d = Path(os.environ["FAKE_RCLONE_AUTH_DIR"])
    (d / state).write_text(json.dumps(meta))
    assert flow.wait(10)


def _connect(cfg, provider, tmp_path, *, name=None, files=None, account=None, **kw):
    root = tmp_path / "clouds" / (name or C.PROVIDER_SPECS[provider].default_name)
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in (files or {"a.txt": "hello"}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)
    if provider == "drive":
        kw.setdefault("client_id", "cid")
        kw.setdefault("client_secret", "csecret-xyz")
    flow = C.start_flow(cfg, provider, **kw)
    _sign_in(flow, root, account)
    return C.finish_connect(cfg, flow.id, name=name)


def _mode(p):
    return stat.S_IMODE(Path(p).stat().st_mode)


# ----------------------------------------------------------------------- basics

def test_rclone_status_with_the_fake_and_missing(cfg):
    st = C.rclone_status(cfg)
    assert st["found"] and st["ok"] and st["version"] == "v1.75.2" and st["managed"] is False
    cfg.rclone_binary = "/nonexistent/rclone-xyz"
    st = C.rclone_status(cfg)
    assert not st["found"] and not st["ok"]
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "dropbox")
    assert e.value.code == "NO_RCLONE"


def test_old_rclone_is_refused(cfg, tmp_path):
    old = tmp_path / "old-rclone"
    old.write_text("#!/bin/sh\necho 'rclone v1.60.1'\n")
    old.chmod(0o755)
    cfg.rclone_binary = str(old)
    st = C.rclone_status(cfg)
    assert st["found"] and not st["ok"]
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "dropbox")
    assert e.value.code == "RCLONE_TOO_OLD"


def test_provider_specs_match_the_contract():
    assert set(C.PROVIDER_SPECS) == {"drive", "onedrive", "dropbox", "iclouddrive"}
    d, o, b = (C.PROVIDER_SPECS[k] for k in ("drive", "onedrive", "dropbox"))
    assert d.needs_client and not o.needs_client and not b.needs_client
    assert d.read_options == {"scope": "drive.readonly"} and d.write_options == {"scope": "drive"}
    assert o.read_options["access_scopes"] == "Files.Read Files.Read.All Sites.Read.All offline_access"
    assert d.extra_args == ("--fast-list", "--drive-skip-shortcuts")


# --------------------------------------------------------------------- the happy path

def test_connect_dropbox_end_to_end(cfg, tmp_path):
    flow = C.start_flow(cfg, "dropbox")
    assert flow.status == "waiting" and flow.auth_url.startswith("http://127.0.0.1:53682/auth?state=")
    assert C.get_flow(flow.id) is flow
    with pytest.raises(C.ConnectError) as e:        # not finished yet
        C.finish_connect(cfg, flow.id)
    assert e.value.code == "FLOW_FAILED"
    root = tmp_path / "clouds" / "dropbox"
    root.mkdir(parents=True)
    (root / "x.txt").write_text("x")
    _sign_in(flow, root, "me@example.com")
    assert flow.status == "succeeded"
    conn = C.finish_connect(cfg, flow.id)
    assert conn["name"] == "dropbox" and conn["provider"] == "dropbox" and conn["read"] == "dropbox-ro:"
    assert conn["write"] is None and conn["writable"] is False and conn["account"] == "me@example.com"
    assert C.list_connections(cfg) == [conn]

    # merged into the config, read-only, with the provider's extra args
    merged = config_mod.load_config(cfg.config_path)
    r = merged.remote("dropbox")
    assert r.read == "dropbox-ro:" and r.write is None and r.connection
    assert merged.rclone_config == str(cfg.own_rclone_conf)

    res = C.test_connection(cfg, "dropbox")
    assert res["ok"] and res["account"] == "me@example.com" and res["error"] is None
    assert res["quota"] is None or set(res["quota"]) == {"used", "total", "free"}
    with pytest.raises(C.ConnectError) as e:
        C.test_connection(cfg, "nope")
    assert e.value.code == "NOT_FOUND"


def test_drive_needs_client_and_uses_the_read_scope(cfg, tmp_path, monkeypatch):
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "drive")
    assert e.value.code == "NEEDS_CLIENT"
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "drive", client_id="only-id")
    assert e.value.code == "NEEDS_CLIENT"
    conn = _connect(cfg, "drive", tmp_path)
    assert conn["name"] == "gdrive"
    started = next(Path(os.environ["FAKE_RCLONE_AUTH_DIR"]).glob("*.started"))
    meta = json.loads(started.read_text())
    assert meta["type"] == "drive" and meta["client_id"] == "cid"
    assert "--drive-scope=drive.readonly" in meta["flags"]
    assert "csecret-xyz" not in started.read_text()
    entry = C._read_entries(cfg)[0]
    assert entry["extra_args"] == ["--fast-list", "--drive-skip-shortcuts"]
    sec = C._conf_sections(cfg.own_rclone_conf)["gdrive-ro"]
    assert sec["scope"] == "drive.readonly" and sec["client_id"] == "cid" and sec["client_secret"] == "csecret-xyz"
    assert config_mod.load_config(cfg.config_path).remote("gdrive").extra_args == ("--fast-list", "--drive-skip-shortcuts")


def test_onedrive_uses_the_narrow_scopes_and_walks_the_questions(cfg, tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))
    _connect(cfg, "onedrive", tmp_path)
    sec = C._conf_sections(cfg.own_rclone_conf)["onedrive-ro"]
    assert sec["access_scopes"] == "Files.Read Files.Read.All Sites.Read.All offline_access"
    assert sec["drive_id"] == "fake-drive-id"              # the question loop reached the drive choice
    calls = [json.loads(ln)["argv"] for ln in log.read_text().splitlines()]
    assert any(a[-1] == "choose_type_done" or "choose_type_done" in a for a in calls)
    assert any(a[2:3] == ["authorize"] and "--onedrive-access-scopes=Files.Read Files.Read.All Sites.Read.All offline_access" in a
               for a in calls)


def test_default_names_count_up_and_explicit_names_are_checked(cfg, tmp_path):
    assert _connect(cfg, "dropbox", tmp_path)["name"] == "dropbox"
    assert _connect(cfg, "dropbox", tmp_path, name=None)["name"] == "dropbox-2"
    assert _connect(cfg, "dropbox", tmp_path, name=None)["name"] == "dropbox-3"
    for bad in ("Dropbox", "1abc", "a b", "", "x" * 40, "-a"):
        flow = C.start_flow(cfg, "dropbox")
        _sign_in(flow, tmp_path / "clouds" / "dropbox")
        if bad == "":
            continue                                       # empty means "default"
        with pytest.raises(C.ConnectError) as e:
            C.finish_connect(cfg, flow.id, name=bad)
        assert e.value.code == "BAD_NAME"
    flow = C.start_flow(cfg, "dropbox")
    _sign_in(flow, tmp_path / "clouds" / "dropbox")
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg, flow.id, name="dropbox-2")
    assert e.value.code == "NAME_TAKEN"
    assert "dropbox-9-ro" not in C._conf_sections(cfg.own_rclone_conf)


def test_name_clash_with_a_remote_of_the_main_config(cfg, tmp_path):
    cfg.config_path.write_text(cfg.config_path.read_text()
                               + f'[[remotes]]\nname = "dropbox"\nprovider = "local"\nread = "{tmp_path}"\n')
    cfg2 = config_mod.load_config(cfg.config_path)
    assert _connect(cfg2, "dropbox", tmp_path, name="photos")["name"] == "photos"
    cfg2 = config_mod.load_config(cfg.config_path)
    assert _connect(cfg2, "dropbox", tmp_path)["name"] == "dropbox-2"     # default skips the taken one
    flow = C.start_flow(cfg2, "dropbox")
    _sign_in(flow, tmp_path / "clouds" / "dropbox")
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg2, flow.id, name="dropbox")
    assert e.value.code == "NAME_TAKEN"


# ------------------------------------------------------------------- failing flows

def test_busy_slow_cancel_and_failure(cfg, tmp_path, monkeypatch):
    flow = C.start_flow(cfg, "dropbox")
    time.sleep(0.3)
    assert flow.status == "waiting"                        # a slow sign-in just waits
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "onedrive")
    assert e.value.code == "FLOW_BUSY"
    C.cancel_flow(flow.id)
    assert flow.wait(10) and flow.status == "cancelled" and flow._token is None
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg, flow.id)
    assert e.value.code == "FLOW_FAILED"
    again = C.start_flow(cfg, "dropbox")                   # the port is free again
    C.cancel_flow(again.id)
    C.cancel_flow(again.id)                                # idempotent
    with pytest.raises(C.ConnectError) as e:
        C.get_flow("nope")
    assert e.value.code == "NOT_FOUND"

    monkeypatch.setenv("FAKE_RCLONE_AUTH_MODE", "fail")
    flow = C.start_flow(cfg, "dropbox")
    _sign_in(flow)
    assert flow.status == "failed" and "access_denied" in flow.error
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg, flow.id)
    assert e.value.code == "FLOW_FAILED"
    assert C.list_connections(cfg) == []


def test_flow_times_out(cfg, monkeypatch):
    flow = C.start_flow(cfg, "dropbox", timeout=0.5)
    assert flow.wait(10) and flow.status == "timeout"
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg, flow.id)
    assert e.value.code == "FLOW_TIMEOUT"
    C.start_flow(cfg, "dropbox").id                        # not busy afterwards


@pytest.mark.parametrize("mode", ["nourl", "busy"])
def test_rclone_dying_before_the_link_is_a_failed_start(cfg, monkeypatch, mode):
    monkeypatch.setenv("FAKE_RCLONE_AUTH_MODE", mode)
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "dropbox")
    assert e.value.code == "FLOW_FAILED"
    monkeypatch.delenv("FAKE_RCLONE_AUTH_MODE")
    C.start_flow(cfg, "dropbox")                           # and it did not leave the lock held


def test_a_connection_that_does_not_work_is_rolled_back(cfg, tmp_path, monkeypatch):
    flow = C.start_flow(cfg, "dropbox")
    _sign_in(flow, tmp_path / "clouds" / "dropbox")
    monkeypatch.setenv("FAKE_RCLONE_TEST_FAIL", "1")
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg, flow.id)
    assert e.value.code == "TEST_FAILED" and "401" in str(e.value)
    assert "dropbox-ro" not in C._conf_sections(cfg.own_rclone_conf)
    assert C.list_connections(cfg) == [] and not cfg.connections_path.exists()


# ------------------------------------------------------------- writes and disconnect

def test_enable_writes_and_disconnect(cfg, tmp_path):
    _connect(cfg, "drive", tmp_path, account="a@b.c")
    flow = C.start_flow(cfg, "drive", purpose="write", name="gdrive")   # client creds come from the -ro remote
    _sign_in(flow, tmp_path / "clouds" / "gdrive")
    with pytest.raises(C.ConnectError):                                   # wrong purpose
        C.finish_connect(cfg, flow.id)
    conn = C.finish_enable_writes(cfg, flow.id, "gdrive")
    assert conn["writable"] and conn["write"] == "gdrive-rw:"
    secs = C._conf_sections(cfg.own_rclone_conf)
    assert secs["gdrive-rw"]["scope"] == "drive" and secs["gdrive-rw"]["client_id"] == "cid"
    assert secs["gdrive-ro"]["scope"] == "drive.readonly"
    assert config_mod.load_config(cfg.config_path).remote("gdrive").write == "gdrive-rw:"
    with pytest.raises(C.ConnectError) as e:
        C.finish_enable_writes(cfg, flow.id, "nope")
    assert e.value.code in ("NOT_FOUND", "FLOW_FAILED")

    C.disconnect(cfg, "gdrive")
    assert C.list_connections(cfg) == [] and not [s for s in C._conf_sections(cfg.own_rclone_conf)]
    with pytest.raises(C.ConnectError) as e:
        C.disconnect(cfg, "gdrive")
    assert e.value.code == "NOT_FOUND"
    assert "gdrive" not in {r.name for r in config_mod.load_config(cfg.config_path).remotes}


def test_disconnect_never_touches_a_remote_that_is_not_a_connection(cfg, tmp_path):
    cfg.config_path.write_text(cfg.config_path.read_text()
                               + f'[[remotes]]\nname = "mine"\nprovider = "local"\nread = "{tmp_path}"\n')
    with pytest.raises(C.ConnectError) as e:
        C.disconnect(config_mod.load_config(cfg.config_path), "mine")
    assert e.value.code == "NOT_FOUND"


# ------------------------------------------------------------ secrets and file modes

def test_tokens_never_leave_and_files_are_private(cfg, tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    (tmp_path / "clouds" / "gdrive").mkdir(parents=True)
    seen = []
    flow = C.start_flow(cfg, "drive", client_id="cid", client_secret="csecret-xyz")
    seen += [json.dumps(flow.public()), repr(flow), str(flow.__dict__.keys())]
    _sign_in(flow, tmp_path / "clouds" / "gdrive")
    token = json.loads(flow._token)["access_token"]
    refresh = json.loads(flow._token)["refresh_token"]
    seen += [json.dumps(flow.public()), repr(flow)]
    conn = C.finish_connect(cfg, flow.id)
    assert flow._token is None and flow._client_secret is None             # dropped once used
    seen += [json.dumps(conn), json.dumps(C.list_connections(cfg)), json.dumps(C.test_connection(cfg, "gdrive")),
             json.dumps(C.rclone_status(cfg)), repr(flow), json.dumps(flow.public())]
    flow2 = C.start_flow(cfg, "drive", purpose="write", name="gdrive")
    _sign_in(flow2, tmp_path / "clouds" / "gdrive")
    seen.append(json.dumps(C.finish_enable_writes(cfg, flow2.id, "gdrive")))

    conf_text = cfg.own_rclone_conf.read_text()
    assert token in conf_text                                              # it lives in rclone.conf only
    for path in (cfg.connections_path, cfg.config_path):
        seen.append(path.read_text())
    captured = capsys.readouterr()
    seen += [caplog.text, captured.out, captured.err]
    blob = "\n".join(seen)
    for secret in (token, refresh, "csecret-xyz"):
        assert secret not in blob
    # files: 600, and no temp files left behind
    for p in (cfg.own_rclone_conf, cfg.connections_path):
        assert _mode(p) == 0o600
    assert not [p for p in cfg.config_dir.iterdir() if p.name.startswith(".")]


def test_a_failed_flow_error_does_not_echo_the_client_secret(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_AUTH_MODE", "fail")
    flow = C.start_flow(cfg, "drive", client_id="cid", client_secret="csecret-xyz")
    _sign_in(flow)
    assert "csecret-xyz" not in json.dumps(flow.public())


# ------------------------------------------------------------------ merged config

def test_connections_toml_is_merged_and_clashes_are_errors(cfg, tmp_path):
    cfg.connections_path.write_text(
        '[[remotes]]\nname = "dropbox"\nprovider = "dropbox"\nread = "dropbox-ro:"\nconnection = true\n'
        'account = "x"\ncreated_at = "2026-01-01T00:00:00Z"\n')
    merged = config_mod.load_config(cfg.config_path)
    assert [r.name for r in merged.remotes] == ["dropbox"] and merged.remotes[0].connection
    cfg.config_path.write_text(cfg.config_path.read_text()
                               + f'[[remotes]]\nname = "dropbox"\nprovider = "local"\nread = "{tmp_path}"\n')
    with pytest.raises(ValueError) as e:
        config_mod.load_config(cfg.config_path)
    assert "config.toml" in str(e.value) and "connections.toml" in str(e.value) and "dropbox" in str(e.value)


def test_a_toml_written_by_us_round_trips_odd_values(cfg):
    entry = {"name": "dropbox", "provider": "dropbox", "read": "dropbox-ro:", "write": "dropbox-rw:",
             "extra_args": ["--a=b c", '--q="x"'], "account": 'O"Brien \\ ü@example.com', "created_at": "2026-01-01T00:00:00Z"}
    C._write_entries(cfg, [entry])
    got = C._read_entries(cfg)[0]
    assert got["account"] == entry["account"] and got["extra_args"] == entry["extra_args"]
    assert _mode(cfg.connections_path) == 0o600


# ---------------------------------------------------------------------- install

def _zip(exe_name="rclone", data=b"#!/bin/sh\necho hi\n"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"rclone-v9.9.9-linux-amd64/{exe_name}", data)
        z.writestr("rclone-v9.9.9-linux-amd64/README.txt", "x")
    return buf.getvalue()


def _fake_fetch(blob, *, good_sum=True, listed=True):
    tag = C._platform_tag()
    name = f"rclone-v9.9.9-{tag}.zip"
    digest = hashlib.sha256(blob).hexdigest() if good_sum else "0" * 64
    sums = (f"{digest}  {name}\n" if listed else "") + f"{'1' * 64}  other.zip\n"

    def fetch(url, progress=None, what=""):
        if url.endswith("version.txt"):
            return b"rclone v9.9.9\n"
        if url.endswith("SHA256SUMS"):
            return sums.encode()
        assert url.endswith(name)
        if progress:
            progress("download", len(blob), len(blob))
        return blob
    return fetch


def test_install_rclone_verifies_the_checksum(tmp_path):
    steps = []
    exe = "rclone.exe" if C._platform_tag().startswith("windows") else "rclone"
    blob = _zip(exe)
    dest = tmp_path / "bin"
    got = C.install_rclone(dest, fetch=_fake_fetch(blob), progress=lambda *a: steps.append(a))
    assert got == dest / exe and got.read_bytes().startswith(b"#!/bin/sh") and os.access(got, os.X_OK)
    assert steps

    dest2 = tmp_path / "bin2"
    with pytest.raises(C.ConnectError) as e:
        C.install_rclone(dest2, fetch=_fake_fetch(blob, good_sum=False))
    assert "checksum" in str(e.value) and not dest2.exists()
    with pytest.raises(C.ConnectError):
        C.install_rclone(dest2, fetch=_fake_fetch(blob, listed=False))
    assert not dest2.exists()


def test_a_managed_rclone_is_found_when_the_configured_one_is_not(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h2"))
    cfg.rclone_binary = "definitely-not-rclone-xyz"
    assert C.rclone_status(cfg)["found"] is False
    bindir = C.managed_bin_dir()
    bindir.mkdir(parents=True)
    exe = bindir / "rclone"
    exe.write_text(f"#!/bin/sh\nexec {FAKE} \"$@\"\n")
    exe.chmod(0o755)
    st = C.rclone_status(cfg)
    assert st["found"] and st["ok"] and st["managed"] and st["path"] == str(exe)


# ======================================================================================================
# docs/ONBOARDING.md section G: the question loop, iCloud, pasted tokens, import, coded Test results
# ======================================================================================================

import shutil  # noqa: E402

REAL_RCLONE = shutil.which("rclone")
needs_rclone = pytest.mark.skipif(REAL_RCLONE is None, reason="needs the real rclone on PATH")
PASSWORD = "pw-SECRET-4711"
CODE = "123456"


def _start_onedrive_question(cfg, tmp_path, monkeypatch, drives="2", name=None):
    """A OneDrive sign-in that ends in rclone asking which drive; returns (flow, question result)."""
    monkeypatch.setenv("FAKE_RCLONE_DRIVES", drives)
    (tmp_path / "clouds" / (name or "onedrive")).mkdir(parents=True, exist_ok=True)
    flow = C.start_flow(cfg, "onedrive", name=name)
    _sign_in(flow, tmp_path / "clouds" / (name or "onedrive"), "me@outlook.com")
    return flow, C.finish_connect(cfg, flow.id, name=name)


def _icloud(cfg, tmp_path, *, name=None, password=PASSWORD):
    (tmp_path / "clouds" / (name or "icloud")).mkdir(parents=True, exist_ok=True)
    return C.start_flow(cfg, "iclouddrive", name=name, apple_id="me@example.com", password=password)


def _sections(cfg):
    return C._conf_sections(cfg.own_rclone_conf)


def _nothing_left(cfg, *names):
    secs = _sections(cfg)
    assert not [n for n in secs if n.split("-")[0] in names or n in names], secs
    assert not cfg.connections_path.exists() or C._read_entries(cfg) == []


# ------------------------------------------------------------------ OneDrive: several drives

def test_onedrive_with_several_drives_asks_which_one_and_uses_the_answer(cfg, tmp_path, monkeypatch):
    flow, res = _start_onedrive_question(cfg, tmp_path, monkeypatch)
    assert set(res) == {"flow_id", "status", "question", "retry"}
    assert res["flow_id"] == flow.id and res["status"] == "question" and res["retry"] is False
    q = res["question"]
    assert q["name"] == "config_driveid" and q["exclusive"] is True and q["password"] is False
    assert [e["value"] for e in q["examples"]] == ["drive-personal", "drive-docs"] and q["default"] == ""
    assert flow.status == "question" and flow.public()["question"] == q and flow.name == "onedrive"
    assert "onedrive-ro" in _sections(cfg) and not cfg.connections_path.exists()      # not recorded before it is made
    conn = C.answer_flow(cfg, flow.id, "drive-docs")
    assert conn["name"] == "onedrive" and "flow_id" not in conn and conn["scoped"] is True
    assert _sections(cfg)["onedrive-ro"]["drive_id"] == "drive-docs"
    assert flow.status == "done" and flow.connection == conn and flow.question is None
    assert C.list_connections(cfg) == [conn]
    assert C.finish_connect(cfg, flow.id) == conn                                      # finishing twice is harmless


def test_onedrive_with_one_drive_asks_nothing(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_DRIVES", "1")
    conn = _connect(cfg, "onedrive", tmp_path)
    assert "flow_id" not in conn and _sections(cfg)["onedrive-ro"]["drive_id"] == "fake-drive-id"


def test_answers_are_validated(cfg, tmp_path, monkeypatch):
    flow, _ = _start_onedrive_question(cfg, tmp_path, monkeypatch)
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, flow.id, "x" * (C.MAX_ANSWER + 1))
    assert e.value.code == "BAD_REQUEST" and flow.status == "question"           # the question is still open
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, "nope", "x")
    assert e.value.code == "NOT_FOUND"
    C.answer_flow(cfg, flow.id, "x" * C.MAX_ANSWER)                                 # exactly the limit is fine
    with pytest.raises(C.ConnectError) as e:                                         # one answer per question
        C.answer_flow(cfg, flow.id, "x")
    assert e.value.code == "NOT_WAITING"
    other = C.start_flow(cfg, "dropbox")                                             # a flow that asked nothing
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, other.id, "x")
    assert e.value.code == "NOT_WAITING"


# ------------------------------------------------------------------ iCloud Drive

def test_icloud_needs_the_apple_id_and_password_up_front(cfg, tmp_path):
    for kw, missing in (({}, "apple_id"), ({"apple_id": "me@example.com"}, "password"), ({"password": "pw"}, "apple_id")):
        with pytest.raises(C.ConnectError) as e:
            C.start_flow(cfg, "iclouddrive", **kw)
        assert e.value.code == "MISSING_FIELD" and e.value.params == {"field": missing}
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "iclouddrive", apple_id="a@b.c", password="pw", client_id="x", client_secret="y")
    assert e.value.code == "BAD_REQUEST"
    for provider in ("dropbox", "onedrive", "drive"):
        kw = {"client_id": "c", "client_secret": "s"} if provider == "drive" else {}
        with pytest.raises(C.ConnectError) as e:
            C.start_flow(cfg, provider, apple_id="a@b.c", **kw)
        assert e.value.code == "BAD_REQUEST"
        with pytest.raises(C.ConnectError) as e:
            C.start_flow(cfg, provider, password="pw", **kw)
        assert e.value.code == "BAD_REQUEST"
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "onedrive", client_id="c", client_secret="s")             # only drive and dropbox take a client
    assert e.value.code == "BAD_REQUEST"
    assert not _sections(cfg) and not [f for f in C._FLOWS.values() if f.status in C._BUSY]


def test_icloud_asks_for_the_two_factor_code_then_connects_read_only(cfg, tmp_path, capsys, caplog, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))
    caplog.set_level(logging.DEBUG)
    flow = _icloud(cfg, tmp_path)
    assert flow.status == "question" and flow.name == "icloud" and flow.auth_url is None
    assert flow.question["name"] == "config_2fa" and flow.question["required"] is True and flow.retry is False
    stanza = _sections(cfg)["icloud-ro"]
    assert stanza["apple_id"] == "me@example.com" and PASSWORD not in stanza["password"]    # rclone obscured it
    assert not cfg.connections_path.exists()
    seen = [json.dumps(flow.public()), repr(flow)]

    wrong = C.answer_flow(cfg, flow.id, "000000")                                    # rejected: asked again, flagged
    assert wrong["status"] == "question" and wrong["retry"] is True and flow.retry is True
    assert "not accepted" not in json.dumps(wrong)                                    # rclone's own text is not forwarded
    seen.append(json.dumps(flow.public()))
    conn = C.answer_flow(cfg, flow.id, CODE)
    assert conn["name"] == "icloud" and conn["provider"] == "iclouddrive" and conn["read"] == "icloud-ro:"
    assert conn["write"] is None and conn["writable"] is False and conn["scoped"] is False
    assert conn["imported_from"] is None and C.list_connections(cfg) == [conn]
    assert "scoped = false" in cfg.connections_path.read_text()
    assert config_mod.load_config(cfg.config_path).remote("icloud").read == "icloud-ro:"
    out = capsys.readouterr()
    seen += [json.dumps(flow.public()), json.dumps(conn), out.out, out.err, caplog.text, log.read_text(),
             cfg.connections_path.read_text(), cfg.config_path.read_text(), repr(flow)]
    blob = "\n".join(seen)
    for secret in (PASSWORD, CODE, "000000"):
        assert secret not in blob
    assert flow._secrets == [] and flow._state is None


def test_icloud_names_count_up_and_a_name_can_be_asked_for(cfg, tmp_path):
    assert C.answer_flow(cfg, _icloud(cfg, tmp_path).id, CODE)["name"] == "icloud"
    (tmp_path / "clouds" / "icloud-2").mkdir(parents=True)
    assert C.answer_flow(cfg, _icloud(cfg, tmp_path).id, CODE)["name"] == "icloud-2"
    with pytest.raises(C.ConnectError) as e:
        _icloud(cfg, tmp_path, name="icloud")
    assert e.value.code == "NAME_TAKEN"
    with pytest.raises(C.ConnectError) as e:
        _icloud(cfg, tmp_path, name="Bad Name")
    assert e.value.code == "BAD_NAME"
    assert "icloud-3-ro" not in _sections(cfg) and not [f for f in C._FLOWS.values() if f.status in C._BUSY]


def test_icloud_rclone_failing_to_create_leaves_nothing(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL_CREATE", "1")
    with pytest.raises(C.ConnectError) as e:
        _icloud(cfg, tmp_path)
    assert e.value.code == "FLOW_FAILED" and "boom" in str(e.value) and PASSWORD not in str(e.value)
    _nothing_left(cfg, "icloud")
    monkeypatch.delenv("FAKE_RCLONE_FAIL_CREATE")
    assert _icloud(cfg, tmp_path).status == "question"                               # not busy, name free again


def test_a_careless_rclone_cannot_leak_the_password_or_the_code(cfg, tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FAKE_RCLONE_ECHO_CREATE", "1")
    with pytest.raises(C.ConnectError) as e:
        _icloud(cfg, tmp_path)
    flow = next(iter(C._FLOWS.values()))
    seen = [str(e.value), json.dumps(flow.public()), repr(flow)]
    assert e.value.code == "FLOW_FAILED" and "<hidden>" in str(e.value)               # the line arrived, masked
    _nothing_left(cfg, "icloud")
    monkeypatch.delenv("FAKE_RCLONE_ECHO_CREATE")

    flow = _icloud(cfg, tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_ECHO_ANSWER", "1")
    secret_code = "654321-SECRET"
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, flow.id, secret_code)
    seen += [str(e.value), json.dumps(flow.public()), repr(flow)]
    assert e.value.code == "FLOW_FAILED" and "<hidden>" in str(e.value)
    assert flow.status == "failed" and flow.code == "FLOW_FAILED" and flow.question is None and flow._secrets == []
    _nothing_left(cfg, "icloud")
    out = capsys.readouterr()
    blob = "\n".join(seen + [out.out, out.err, caplog.text])
    for secret in (PASSWORD, secret_code):
        assert secret not in blob


def test_rclone_failing_while_continuing_removes_what_it_wrote(cfg, tmp_path, monkeypatch):
    flow = _icloud(cfg, tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_FAIL_CONTINUE", "1")
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, flow.id, CODE)
    assert e.value.code == "FLOW_FAILED" and flow.status == "failed" and flow.code == "FLOW_FAILED"
    assert flow.public()["params"] == {} and "boom" in flow.public()["error"]
    _nothing_left(cfg, "icloud")
    monkeypatch.delenv("FAKE_RCLONE_FAIL_CONTINUE")
    # OAuth: the first rclone call after the token works, the next one fails
    monkeypatch.setenv("FAKE_RCLONE_FAIL_CREATE", "1")
    f2 = C.start_flow(cfg, "dropbox")
    _sign_in(f2, tmp_path / "clouds" / "dropbox")
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg, f2.id)
    assert e.value.code == "FLOW_FAILED" and f2.status == "failed"
    _nothing_left(cfg, "dropbox")


# ------------------------------------------------------------------ cancel, timeouts, names while a question waits

def test_cancel_while_a_question_waits_deletes_the_remote_and_frees_the_name(cfg, tmp_path, monkeypatch):
    flow, _ = _start_onedrive_question(cfg, tmp_path, monkeypatch)
    assert "onedrive-ro" in _sections(cfg)
    C.cancel_flow(flow.id)
    assert flow.status == "cancelled" and flow.question is None and flow.code is None
    _nothing_left(cfg, "onedrive")
    C.cancel_flow(flow.id)                                                            # idempotent
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, flow.id, "drive-docs")
    assert e.value.code == "NOT_WAITING"
    flow2, res = _start_onedrive_question(cfg, tmp_path, monkeypatch)                # the name and the lock are free
    assert res["status"] == "question" and flow2.name == "onedrive"


def test_cancel_works_on_a_signed_in_flow_and_a_cancelled_write_flow_changes_nothing(cfg, tmp_path):
    _connect(cfg, "dropbox", tmp_path, account="a@b.c")
    before = (cfg.own_rclone_conf.read_text(), cfg.connections_path.read_text())
    flow = C.start_flow(cfg, "dropbox", purpose="write", name="dropbox")
    _sign_in(flow, tmp_path / "clouds" / "dropbox")
    assert flow.status == "succeeded"
    C.cancel_flow(flow.id)
    assert flow.status == "cancelled" and flow._token is None
    with pytest.raises(C.ConnectError) as e:
        C.finish_enable_writes(cfg, flow.id, "dropbox")
    assert e.value.code == "FLOW_FAILED"
    assert (cfg.own_rclone_conf.read_text(), cfg.connections_path.read_text()) == before
    assert "dropbox-rw" not in _sections(cfg) and C.list_connections(cfg)[0]["write"] is None


def test_no_answer_in_time_times_out_deletes_the_remote_and_frees_the_name(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "QUESTION_TIMEOUT_S", 0.4)
    flow, _ = _start_onedrive_question(cfg, tmp_path, monkeypatch)
    deadline = time.monotonic() + 10
    while flow.status in ("question", "working") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert flow.status == "timeout" and flow.code == "FLOW_TIMEOUT" and flow.question is None
    assert flow.public()["code"] == "FLOW_TIMEOUT" and flow.public()["error"]
    _nothing_left(cfg, "onedrive")
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, flow.id, "drive-docs")
    assert e.value.code == "NOT_WAITING"
    monkeypatch.setattr(C, "QUESTION_TIMEOUT_S", 600.0)
    assert _start_onedrive_question(cfg, tmp_path, monkeypatch)[1]["status"] == "question"


def test_an_answered_question_does_not_time_out_later(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "QUESTION_TIMEOUT_S", 0.4)
    flow, _ = _start_onedrive_question(cfg, tmp_path, monkeypatch)
    conn = C.answer_flow(cfg, flow.id, "drive-docs")
    time.sleep(0.8)
    assert flow.status == "done" and C.list_connections(cfg) == [conn] and "onedrive-ro" in _sections(cfg)


def test_a_name_reserved_by_a_waiting_question_blocks_a_second_flow(cfg, tmp_path, monkeypatch):
    flow, _ = _start_onedrive_question(cfg, tmp_path, monkeypatch)
    paste = C.paste_token(cfg, "onedrive", json.dumps({"access_token": "a-b-c-1", "refresh_token": "r-1"}))
    with pytest.raises(C.ConnectError) as e:                                          # not busy (no process), but reserved
        C.finish_connect(cfg, paste.id, name="onedrive")
    assert e.value.code == "NAME_TAKEN" and paste.status == "succeeded"              # the paste can pick another name
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "dropbox")
    assert e.value.code == "FLOW_BUSY"                                                # a question is a flow in progress
    C.cancel_flow(flow.id)                                                            # the name is free again
    res = C.finish_connect(cfg, paste.id, name="onedrive")                            # asks the drive question itself
    assert res["status"] == "question" and paste.name == "onedrive"


def test_a_name_taken_meanwhile_is_refused_at_the_end_and_the_remote_goes(cfg, tmp_path, monkeypatch):
    flow, _ = _start_onedrive_question(cfg, tmp_path, monkeypatch)
    C._write_entries(cfg, [{"name": "onedrive", "provider": "dropbox", "read": "elsewhere-ro:", "write": None,
                            "extra_args": [], "account": None, "created_at": "2026-01-01T00:00:00Z"}])
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, flow.id, "drive-docs")
    assert e.value.code == "NAME_TAKEN" and flow.status == "failed" and flow.code == "NAME_TAKEN"
    assert "onedrive-ro" not in _sections(cfg)
    assert [c["read"] for c in C.list_connections(cfg)] == ["elsewhere-ro:"]          # the other entry is untouched


def test_a_connections_toml_write_error_deletes_the_new_remote(cfg, tmp_path, monkeypatch):
    flow = C.start_flow(cfg, "dropbox")
    _sign_in(flow, tmp_path / "clouds" / "dropbox")

    def broken(config, entries):
        raise OSError("disk on fire")

    real = C._write_entries
    monkeypatch.setattr(C, "_write_entries", broken)
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg, flow.id)
    assert e.value.code == "CONFIG_WRITE" and "disk on fire" not in str(e.value)
    assert flow.status == "failed" and flow.code == "CONFIG_WRITE"
    assert "dropbox-ro" not in _sections(cfg) and not cfg.connections_path.exists()    # main used to leave -ro behind
    monkeypatch.setattr(C, "_write_entries", real)
    # after a question, too
    flow, _ = _start_onedrive_question(cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(C, "_write_entries", broken)
    with pytest.raises(C.ConnectError) as e:
        C.answer_flow(cfg, flow.id, "drive-docs")
    assert e.value.code == "CONFIG_WRITE" and "onedrive-ro" not in _sections(cfg)


def test_a_write_flow_that_fails_to_record_removes_the_rw_remote(cfg, tmp_path, monkeypatch):
    _connect(cfg, "dropbox", tmp_path)
    flow = C.start_flow(cfg, "dropbox", purpose="write", name="dropbox")
    _sign_in(flow, tmp_path / "clouds" / "dropbox")
    before = cfg.connections_path.read_text()
    monkeypatch.setattr(C, "_write_entries", lambda config, entries: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(C.ConnectError) as e:
        C.finish_enable_writes(cfg, flow.id, "dropbox")
    assert e.value.code == "CONFIG_WRITE" and "dropbox-rw" not in _sections(cfg)
    assert cfg.connections_path.read_text() == before and "dropbox-ro" in _sections(cfg)


# ------------------------------------------------------------------ a token from another computer

def _token_block(root=None, **extra):
    token = {"access_token": "pasted-at-" + "a1b2c3d4", "token_type": "Bearer", "refresh_token": "pasted-rt-e5f6a7b8",
             "expiry": "2099-01-01T00:00:00Z", **extra}
    if root:
        token["fake_root"] = str(root)
    body = json.dumps(token, separators=(",", ":"))
    return token, body, f"Paste the following into your remote machine --->\n{body}\n<---End paste\n"


@pytest.mark.parametrize("form", ["block", "bare", "pretty", "padded"])
def test_a_pasted_token_connects_without_a_browser(cfg, tmp_path, caplog, capsys, form):
    caplog.set_level(logging.DEBUG)
    root = tmp_path / "clouds" / "dropbox"
    root.mkdir(parents=True)
    token, body, block = _token_block(root, fake_account="me@example.com")
    text = {"block": block, "bare": body, "pretty": json.dumps(token, indent=2), "padded": f"\n\n  {block}  \n"}[form]
    flow = C.paste_token(cfg, "dropbox", text)
    assert flow.status == "succeeded" and flow.auth_url is None and flow._proc is None
    assert flow.public()["status"] == "succeeded"
    conn = C.finish_connect(cfg, flow.id)
    assert conn["name"] == "dropbox" and conn["scoped"] is True and conn["account"] == "me@example.com"
    assert "pasted-rt-e5f6a7b8" in cfg.own_rclone_conf.read_text()                    # rclone keeps it, nobody else
    out = capsys.readouterr()
    blob = "\n".join([json.dumps(flow.public()), repr(flow), json.dumps(conn), json.dumps(C.list_connections(cfg)),
                      cfg.connections_path.read_text(), cfg.config_path.read_text(), out.out, out.err, caplog.text])
    for secret in ("pasted-at-a1b2c3d4", "pasted-rt-e5f6a7b8"):
        assert secret not in blob
    assert flow._token is None and flow._secrets == []


@pytest.mark.parametrize("junk", ["", "   ", "hello world", "{not json", "[1, 2]", '{"a": 1}', "{}",
                                  "--->\n<---End paste", "x" * (C.MAX_TOKEN + 1),
                                  json.dumps({"access_token": "t" * (C.MAX_TOKEN + 1)})])
def test_an_unreadable_token_is_refused_without_quoting_it(cfg, junk):
    with pytest.raises(C.ConnectError) as e:
        C.paste_token(cfg, "dropbox", junk)
    assert e.value.code == "TOKEN_UNREADABLE"
    if junk.strip():
        assert junk.strip()[:20] not in str(e.value)
    assert not _sections(cfg)


def test_a_pasted_drive_token_needs_the_client_and_never_the_secret_back(cfg, tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    _, body, block = _token_block(tmp_path / "clouds" / "gdrive")
    (tmp_path / "clouds" / "gdrive").mkdir(parents=True)
    for kw in ({}, {"client_id": "cid"}, {"client_secret": "only-secret"}):
        with pytest.raises(C.ConnectError) as e:
            C.paste_token(cfg, "drive", block, **kw)
        assert e.value.code == "NEEDS_CLIENT"
    flow = C.paste_token(cfg, "drive", block, client_id="cid", client_secret="csecret-paste-xyz")
    conn = C.finish_connect(cfg, flow.id)
    assert conn["name"] == "gdrive"
    sec = _sections(cfg)["gdrive-ro"]
    assert sec["scope"] == "drive.readonly" and sec["client_id"] == "cid" and sec["client_secret"] == "csecret-paste-xyz"
    out = capsys.readouterr()
    blob = "\n".join([json.dumps(flow.public()), json.dumps(conn), out.out, out.err, caplog.text,
                      cfg.connections_path.read_text()])
    assert "csecret-paste-xyz" not in blob and "pasted-rt" not in blob


def test_paste_is_only_for_providers_that_sign_in_with_a_token(cfg):
    _, _, block = _token_block()
    with pytest.raises(C.ConnectError) as e:
        C.paste_token(cfg, "iclouddrive", block)
    assert e.value.code == "BAD_REQUEST"
    with pytest.raises(C.ConnectError) as e:
        C.paste_token(cfg, "nope", block)
    assert e.value.code == "NOT_FOUND"
    with pytest.raises(C.ConnectError) as e:
        C.paste_token(cfg, "dropbox", block, purpose="sideways")
    assert e.value.code == "BAD_REQUEST"
    with pytest.raises(C.ConnectError) as e:
        C.paste_token(cfg, "dropbox", block, name="Bad Name")
    assert e.value.code == "BAD_NAME"


def test_a_pasted_token_can_enable_changes_too(cfg, tmp_path):
    _connect(cfg, "drive", tmp_path)
    (tmp_path / "clouds" / "gdrive").mkdir(parents=True, exist_ok=True)
    _, _, block = _token_block(tmp_path / "clouds" / "gdrive")
    flow = C.paste_token(cfg, "drive", block, purpose="write", name="gdrive")          # client comes from gdrive-ro
    conn = C.finish_enable_writes(cfg, flow.id, "gdrive")
    assert conn["writable"] and conn["write"] == "gdrive-rw:"
    assert _sections(cfg)["gdrive-rw"]["scope"] == "drive" and _sections(cfg)["gdrive-rw"]["client_id"] == "cid"


def test_authorize_command_is_the_exact_command_for_the_other_computer():
    assert C.authorize_command("dropbox", "read") == ["rclone", "authorize", "dropbox"]
    assert C.authorize_command("drive", "read") == ["rclone", "authorize", "drive", "--drive-scope=drive.readonly"]
    assert C.authorize_command("drive", "write", "cid-1") == ["rclone", "authorize", "drive", "cid-1", "CLIENT_SECRET",
                                                              "--drive-scope=drive"]
    assert C.authorize_command("onedrive", "read") == [
        "rclone", "authorize", "onedrive",
        "--onedrive-access-scopes=Files.Read Files.Read.All Sites.Read.All offline_access"]
    assert C.authorize_command("onedrive", "write") == ["rclone", "authorize", "onedrive"]
    for bad in (("iclouddrive", "read"), ("dropbox", "sideways"), ("nope", "read")):
        with pytest.raises(C.ConnectError):
            C.authorize_command(*bad)


# ------------------------------------------------------------------ only rclone's own helper URL is passed on

@pytest.mark.parametrize("line, url", [
    ("2026/01/01 NOTICE: Please go to the following link: http://127.0.0.1:53682/auth?state=9B338VBQ2e8ctJbajCeu4w",
     "http://127.0.0.1:53682/auth?state=9B338VBQ2e8ctJbajCeu4w"),
    ("If your browser doesn't open automatically go to the following link: http://127.0.0.1:53682/auth?state=a-b_c",
     "http://127.0.0.1:53682/auth?state=a-b_c"),
    ("go to http://127.0.0.1:53682/auth?state=abc@evil.example/x", "http://127.0.0.1:53682/auth?state=abc"),
])
def test_auth_url_pattern_takes_only_the_helper_url(line, url):
    assert C._URL_RE.search(line).group(0) == url


@pytest.mark.parametrize("line", [
    "NOTICE: Waiting for code...",
    "go to http://evil.example/auth?state=abc",
    "go to http://127.0.0.1:53683/auth?state=abc",
    "go to http://127.0.0.1:53682.evil.example/auth?state=abc",
    "go to https://127.0.0.1:53682/auth?state=abc",
    "go to http://localhost:53682/auth?state=abc",
    "http://127.0.0.1:53682/other?state=abc",
])
def test_auth_url_pattern_ignores_everything_else(line):
    assert C._URL_RE.search(line) is None


def test_a_sign_in_link_that_is_not_the_helper_is_never_stored(cfg, tmp_path):
    evil = tmp_path / "evil-rclone"
    evil.write_text("#!/usr/bin/env python3\nimport sys, time\n"
                    "if sys.argv[-1] == 'version' or 'version' in sys.argv[1:3]:\n    print('rclone v1.75.2'); sys.exit(0)\n"
                    "sys.stderr.write('NOTICE: go to http://evil.example/auth?state=abc\\n'); sys.stderr.flush()\n"
                    "time.sleep(30)\n")
    evil.chmod(0o755)
    cfg.rclone_binary = str(evil)
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "dropbox", url_wait=1.0)
    assert e.value.code == "FLOW_FAILED"
    assert all(f.auth_url is None for f in C._FLOWS.values())


# ------------------------------------------------------------------ importing remotes of the person's own rclone config

def _personal(tmp_path, monkeypatch, text, name="personal.conf"):
    path = tmp_path / "personal" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    return path


def _drop_root(tmp_path, name):
    root = tmp_path / "clouds" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "f.txt").write_text("x")
    return root


PERSONAL = ('# my remotes\n[work]\ntype = dropbox\ntoken = {{"access_token":"imp-at-1234","refresh_token":"imp-rt-5678",'
            '"expiry":"2099-01-01T00:00:00Z"}}\nfake_root = {root}\nfake_account = work@example.com\n\n'
            '[priv]\ntype = crypt\nremote = work:enc\npassword = OBSCURED-abc\n\n'
            '[bucket]\ntype = s3\nfake_root = {root2}\naccess_key_id = AKIA-IMPORT-KEY\n\n'
            '[icl]\ntype = iclouddrive\napple_id = me@example.com\npassword = OBSCURED-xyz\nfake_root = {root3}\n')


def _personal_text(tmp_path):
    return PERSONAL.format(root=_drop_root(tmp_path, "work"), root2=_drop_root(tmp_path, "bucket"),
                           root3=_drop_root(tmp_path, "icl"))


def test_importable_lists_the_remotes_of_the_persons_own_config(cfg, tmp_path, monkeypatch):
    path = _personal(tmp_path, monkeypatch, _personal_text(tmp_path))
    got = C.importable(cfg)
    assert got["config_path"] == str(path) and got["encrypted"] is False
    assert got["remotes"] == [
        {"name": "work", "type": "dropbox", "provider": "dropbox", "supported": True, "imported": False},
        {"name": "priv", "type": "crypt", "provider": "other", "supported": False, "imported": False},
        {"name": "bucket", "type": "s3", "provider": "other", "supported": True, "imported": False},
        {"name": "icl", "type": "iclouddrive", "provider": "iclouddrive", "supported": True, "imported": False}]
    assert "imp-at" not in json.dumps(got) and "AKIA" not in json.dumps(got)         # names and types only


@pytest.mark.parametrize("rtype", ["alias", "crypt", "union", "combine", "chunker", "hasher", "compress", "cache", "box"])
def test_remotes_that_wrap_others_or_rotate_tokens_are_not_supported(cfg, tmp_path, monkeypatch, rtype):
    _personal(tmp_path, monkeypatch, f"[x]\ntype = {rtype}\n")
    assert C.importable(cfg)["remotes"][0]["supported"] is False
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "x")
    assert e.value.code == "IMPORT_UNSUPPORTED" and e.value.params == {"type": rtype}
    assert not _sections(cfg) and not cfg.connections_path.exists()


def test_import_copies_the_remote_read_only_and_leaves_the_persons_config_alone(cfg, tmp_path, monkeypatch, capsys,
                                                                              caplog):
    caplog.set_level(logging.DEBUG)
    text = _personal_text(tmp_path)
    path = _personal(tmp_path, monkeypatch, text)
    before = path.read_bytes()
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))
    conn = C.import_remote(cfg, "work")
    assert conn["name"] == "work" and conn["provider"] == "dropbox" and conn["read"] == "work-ro:"
    assert conn["write"] is None and conn["writable"] is False
    assert conn["scoped"] is False and conn["imported_from"] == "work"
    assert conn["account"] == "work@example.com"
    entry = C._read_entries(cfg)[0]
    assert entry["scoped"] is False and entry["imported_from"] == "work" and "write" not in entry
    assert path.read_bytes() == before                                                 # byte for byte
    sec = _sections(cfg)["work-ro"]
    assert sec["type"] == "dropbox" and "imp-rt-5678" in sec["token"]                  # a copy, in drive-merge's own file
    assert config_mod.load_config(cfg.config_path).remote("work").read == "work-ro:"
    assert C.importable(cfg)["remotes"][0]["imported"] is True
    assert C.list_connections(cfg) == [conn]
    out = capsys.readouterr()
    blob = "\n".join([json.dumps(conn), out.out, out.err, caplog.text, log.read_text(), cfg.connections_path.read_text(),
                      cfg.config_path.read_text()])
    for secret in ("imp-at-1234", "imp-rt-5678"):
        assert secret not in blob
    calls = [json.loads(ln)["argv"] for ln in log.read_text().splitlines()]
    assert any("--no-obscure" in a and "create" in a for a in calls)                  # values are copied as stored


def test_import_keeps_values_as_stored_so_a_password_is_not_obscured_twice(cfg, tmp_path, monkeypatch):
    _personal(tmp_path, monkeypatch, _personal_text(tmp_path))
    flow_or_q = C.import_remote(cfg, "icl")
    assert flow_or_q["status"] == "question" and flow_or_q["question"]["name"] == "config_2fa"   # trust expired: ask
    assert _sections(cfg)["icl-ro"]["password"] == "OBSCURED-xyz"                       # stored as given
    conn = C.answer_flow(cfg, flow_or_q["flow_id"], CODE)
    assert conn["name"] == "icl" and conn["provider"] == "iclouddrive" and conn["scoped"] is False
    assert conn["imported_from"] == "icl"


def test_import_names_and_refusals(cfg, tmp_path, monkeypatch):
    _personal(tmp_path, monkeypatch, _personal_text(tmp_path) + "[My_Drive.2]\ntype = dropbox\nfake_root = "
              + str(_drop_root(tmp_path, "my")) + "\n")
    conn = C.import_remote(cfg, "My_Drive.2")                                          # lowercased into the name rule
    assert conn["name"] == "my-drive-2"
    with pytest.raises(C.ConnectError) as e:                                           # already imported
        C.import_remote(cfg, "My_Drive.2", name="another")
    assert e.value.code == "NAME_TAKEN"
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "ghost")
    assert e.value.code == "NOT_FOUND"
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "work", name="Bad Name")
    assert e.value.code == "BAD_NAME"
    C._write_entries(cfg, [*C._read_entries(cfg), {"name": "mine", "provider": "dropbox", "read": "m-ro:", "write": None,
                                                   "extra_args": [], "account": None, "created_at": "2026-01-01"}])
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "work", name="mine")
    assert e.value.code == "NAME_TAKEN"
    assert set(_sections(cfg)) == {"my-drive-2-ro"} and len(C._read_entries(cfg)) == 2


def test_name_taken_on_import_suggests_the_first_free_numbered_name(cfg, tmp_path, monkeypatch):
    _personal(tmp_path, monkeypatch, _personal_text(tmp_path))
    C.import_remote(cfg, "work")
    with pytest.raises(C.ConnectError) as e:                                           # bucket would be named "work" too
        C.import_remote(cfg, "bucket", name="work")
    assert e.value.code == "NAME_TAKEN" and e.value.params == {"suggested_name": "work-2"}
    C.import_remote(cfg, "bucket", name="work-2")
    with pytest.raises(C.ConnectError) as e:                                           # work-2 is taken now, and so is its rclone section
        C.import_remote(cfg, "icl", name="work")
    assert e.value.params == {"suggested_name": "work-3"}
    long = "a" * 31                                                                    # the suffix must still fit the 31-character rule
    assert C._suggest_locked(cfg, long) == "a" * 29 + "-2"
    assert C.NAME_RE.match(C._suggest_locked(cfg, long))
    with pytest.raises(C.ConnectError) as e:                                           # BAD_NAME has no suggestion
        C.import_remote(cfg, "icl", name="Bad Name")
    assert e.value.code == "BAD_NAME" and e.value.params == {}


def test_unknown_types_are_imported_as_provider_other(cfg, tmp_path, monkeypatch):
    _personal(tmp_path, monkeypatch, _personal_text(tmp_path))
    conn = C.import_remote(cfg, "bucket")
    assert conn["provider"] == "other" and conn["scoped"] is False and conn["read"] == "bucket-ro:"
    assert config_mod.load_config(cfg.config_path).remote("bucket").provider == "other"
    assert C.test_connection(cfg, "bucket")["ok"] is True


def test_an_encrypted_rclone_config_is_reported_and_refused(cfg, tmp_path, monkeypatch):
    _personal(tmp_path, monkeypatch, "# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\nabcdefgh\n")
    got = C.importable(cfg)
    assert got["encrypted"] is True and got["remotes"] == [] and got["config_path"]
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "anything")
    assert e.value.code == "IMPORT_ENCRYPTED"


def test_nothing_to_import_without_a_config_of_their_own_or_when_it_is_ours(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    assert C.importable(cfg) == {"config_path": None, "encrypted": False, "remotes": []}
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "x")
    assert e.value.code == "NOT_FOUND"
    _connect(cfg, "dropbox", tmp_path)
    monkeypatch.setenv("RCLONE_CONFIG", str(cfg.own_rclone_conf))                      # drive-merge's own file is ignored
    assert C.importable(cfg)["remotes"] == []
    link = tmp_path / "link.conf"
    link.symlink_to(cfg.own_rclone_conf)
    monkeypatch.setenv("RCLONE_CONFIG", str(link))
    assert C.importable(cfg)["config_path"] is None


def test_the_persons_config_is_found_through_rclone_config_file(cfg, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "rclone").mkdir(parents=True)
    (home / ".config" / "rclone" / "rclone.conf").write_text("[work]\ntype = dropbox\n")
    got = C.importable(cfg)                                                            # RCLONE_CONFIG unset
    assert got["config_path"] == str(home / ".config" / "rclone" / "rclone.conf")
    assert [r["name"] for r in got["remotes"]] == ["work"]


def test_a_failed_import_leaves_nothing_behind(cfg, tmp_path, monkeypatch):
    _personal(tmp_path, monkeypatch, _personal_text(tmp_path))
    monkeypatch.setenv("FAKE_RCLONE_FAIL_CREATE", "1")
    with pytest.raises(C.ConnectError):
        C.import_remote(cfg, "work")
    _nothing_left(cfg, "work")
    monkeypatch.delenv("FAKE_RCLONE_FAIL_CREATE")
    monkeypatch.setenv("FAKE_RCLONE_TEST_FAIL", "1")
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "work")
    assert e.value.code == "TEST_FAILED"
    _nothing_left(cfg, "work")
    monkeypatch.delenv("FAKE_RCLONE_TEST_FAIL")
    monkeypatch.setattr(C, "_write_entries", lambda c, e: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "work")
    assert e.value.code == "CONFIG_WRITE"
    assert "work-ro" not in _sections(cfg)
    assert not [f for f in C._FLOWS.values() if f.status in C._BUSY] and not C._RESERVED


def test_imported_secrets_are_scrubbed_from_everything_rclone_says(cfg, tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    _personal(tmp_path, monkeypatch, _personal_text(tmp_path))
    monkeypatch.setenv("FAKE_RCLONE_ECHO_CREATE", "1")
    with pytest.raises(C.ConnectError) as e:
        C.import_remote(cfg, "work")
    flow = next(iter(C._FLOWS.values()))
    out = capsys.readouterr()
    blob = "\n".join([str(e.value), json.dumps(flow.public()), out.out, out.err, caplog.text])
    assert "imp-rt-5678" not in blob and "imp-at-1234" not in blob and "work@example.com" not in blob
    _nothing_left(cfg, "work")


@needs_rclone
def test_real_rclone_imports_a_local_remote_and_classifies_a_missing_one(cfg, tmp_path, monkeypatch):
    cfg.rclone_binary = REAL_RCLONE
    data = tmp_path / "data"
    data.mkdir()
    (data / "f.txt").write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
    conf = tmp_path / "realhome" / ".config" / "rclone" / "rclone.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"[loc]\ntype = local\n\n[locked]\ntype = crypt\nremote = loc:\n")
    before = conf.read_bytes()
    got = C.importable(cfg)                                                            # `rclone config file`, for real
    assert got["config_path"] == str(conf)
    assert [(r["name"], r["supported"]) for r in got["remotes"]] == [("loc", True), ("locked", False)]
    conn = C.import_remote(cfg, "loc")
    assert conn["name"] == "loc" and conn["scoped"] is False and conn["provider"] == "local"
    assert conf.read_bytes() == before and "loc-ro" in _sections(cfg)
    res = C.test_connection(cfg, "loc")
    assert res["ok"] is True and res["code"] is None
    cfg.config_path.write_text(cfg.config_path.read_text()
                               + '[[remotes]]\nname = "ghost"\nprovider = "other"\nread = "ghost-remote:"\n')
    res = C.test_connection(config_mod.load_config(cfg.config_path), "ghost")
    assert res["ok"] is False and res["code"] == "NOT_FOUND" and res["params"] == {}


# ------------------------------------------------------------------ Test results are coded

def test_a_test_that_works_has_a_null_code(cfg, tmp_path):
    _connect(cfg, "dropbox", tmp_path, account="a@b.c")
    res = C.test_connection(cfg, "dropbox")
    assert res["ok"] is True and res["code"] is None and res["params"] == {} and res["error"] is None
    assert set(res) == {"ok", "account", "quota", "error", "code", "params"}


@pytest.mark.parametrize("kind, code, params", [
    ("auth", "AUTH_EXPIRED", {}), ("network", "NETWORK", {}), ("other", "RCLONE_ERROR", None)])
def test_a_failing_test_is_worded_from_a_code(cfg, tmp_path, kind, code, params):
    _connect(cfg, "dropbox", tmp_path)
    text = cfg.own_rclone_conf.read_text().replace("[dropbox-ro]\n", f"[dropbox-ro]\nfake_error = {kind}\n")
    cfg.own_rclone_conf.write_text(text)
    res = C.test_connection(cfg, "dropbox")
    assert res["ok"] is False and res["code"] == code and res["error"]
    if params is None:                                                                  # RCLONE_ERROR carries rclone's last line
        assert list(res["params"]) == ["detail"] and "something odd happened" in res["params"]["detail"]
    else:
        assert res["params"] == params
    assert res["account"] is None and res["quota"] is None


def test_test_of_a_remote_that_rclone_lost_is_not_found(cfg, tmp_path):
    _connect(cfg, "dropbox", tmp_path)
    cfg.own_rclone_conf.write_text("")
    res = C.test_connection(cfg, "dropbox")
    assert res["ok"] is False and res["code"] == "NOT_FOUND"


def test_a_remote_of_config_toml_can_be_tested_too(cfg, tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "f.txt").write_text("x")
    cfg.config_path.write_text(cfg.config_path.read_text()
                               + f'[[remotes]]\nname = "mine"\nprovider = "local"\nread = "{good}"\n'
                               + '[[remotes]]\nname = "lost"\nprovider = "other"\nread = "lost-remote:"\n')
    cfg2 = config_mod.load_config(cfg.config_path)
    assert cfg2.remote("mine").connection is False
    assert C.test_connection(cfg2, "mine")["ok"] is True
    res = C.test_connection(cfg2, "lost")
    assert res["ok"] is False and res["code"] == "NOT_FOUND"
    with pytest.raises(C.ConnectError) as e:
        C.test_connection(cfg2, "nobody")
    assert e.value.code == "NOT_FOUND"


def test_a_test_that_never_answers_ends_after_the_timeout_as_a_network_failure(cfg, tmp_path, monkeypatch):
    _connect(cfg, "dropbox", tmp_path)
    monkeypatch.setattr(C, "TEST_TIMEOUT_S", 0.5)
    monkeypatch.setenv("FAKE_RCLONE_HANG_LSJSON", "1")
    started = time.monotonic()
    res = C.test_connection(cfg, "dropbox")
    assert time.monotonic() - started < 10
    assert res["ok"] is False and res["code"] == "NETWORK" and "timed out" in res["error"]


def test_the_production_timeout_of_a_test_is_thirty_seconds():
    assert C.TEST_TIMEOUT_S == 30.0 and C.CREATE_TIMEOUT_S == 120 and C.QUESTION_TIMEOUT_S == 600.0


# ------------------------------------------------------------------ Enable changes without a second sign-in

def test_enable_changes_on_icloud_reuses_read(cfg, tmp_path):
    conn = C.answer_flow(cfg, _icloud(cfg, tmp_path).id, CODE)
    secs = dict(_sections(cfg))
    flow = C.start_flow(cfg, "iclouddrive", purpose="write", name="icloud")
    assert flow.status == "succeeded" and flow.auth_url is None and flow._proc is None
    out = C.finish_enable_writes(cfg, flow.id, "icloud")
    assert out["writable"] is True and out["write"] == out["read"] == "icloud-ro:" and out["scoped"] is False
    assert _sections(cfg) == secs                                                       # no second remote
    assert config_mod.load_config(cfg.config_path).remote("icloud").write == "icloud-ro:"
    assert flow.status == "done" and flow.connection == out
    C.disconnect(cfg, "icloud")
    assert _sections(cfg) == {} and C.list_connections(cfg) == []


def test_enable_changes_on_an_imported_remote_reuses_read(cfg, tmp_path, monkeypatch):
    _personal(tmp_path, monkeypatch, _personal_text(tmp_path))
    C.import_remote(cfg, "work")
    secs = dict(_sections(cfg))
    flow = C.start_flow(cfg, "dropbox", purpose="write", name="work")                   # even a Dropbox: it was imported
    assert flow.status == "succeeded" and flow._proc is None
    out = C.finish_enable_writes(cfg, flow.id, "work")
    assert out["write"] == "work-ro:" and _sections(cfg) == secs
    flow = C.start_flow(cfg, "other", purpose="write", name="work")                     # the provider key is not needed
    assert C.finish_enable_writes(cfg, flow.id, "work")["writable"] is True


def test_a_reuse_flow_for_the_wrong_name_or_an_unknown_one_changes_nothing(cfg, tmp_path):
    C.answer_flow(cfg, _icloud(cfg, tmp_path).id, CODE)
    flow = C.start_flow(cfg, "iclouddrive", purpose="write", name="icloud")
    with pytest.raises(C.ConnectError) as e:
        C.finish_enable_writes(cfg, flow.id, "ghost")
    assert e.value.code == "NOT_FOUND"
    assert C.list_connections(cfg)[0]["write"] is None
    C.cancel_flow(flow.id)
    assert flow.status == "cancelled" and C.list_connections(cfg)[0]["write"] is None
    with pytest.raises(C.ConnectError) as e:                                            # iCloud has no write sign-in at all
        C.start_flow(cfg, "iclouddrive", purpose="write")
    assert e.value.code in ("BAD_REQUEST", "MISSING_FIELD")


def test_disconnect_deletes_exactly_the_remotes_the_entry_names(cfg, tmp_path):
    _connect(cfg, "dropbox", tmp_path)
    text = cfg.own_rclone_conf.read_text() + "\n[dropbox-rw]\ntype = dropbox\n"       # a stray, not named by the entry
    cfg.own_rclone_conf.write_text(text)
    C.disconnect(cfg, "dropbox")
    assert set(_sections(cfg)) == {"dropbox-rw"}


# ------------------------------------------------------------------ the public view of a flow

def test_a_flow_view_has_the_documented_keys_and_no_secret(cfg, tmp_path, monkeypatch):
    flow = C.start_flow(cfg, "drive", client_id="cid", client_secret="csecret-view")
    assert set(flow.public()) == {"id", "provider", "purpose", "status", "auth_url", "error", "code", "params", "name",
                                  "question", "retry", "connection"}
    assert flow.public()["status"] == "waiting" and flow.public()["question"] is None and flow.public()["code"] is None
    monkeypatch.setenv("FAKE_RCLONE_AUTH_MODE", "fail")
    C.cancel_flow(flow.id)
    f2 = C.start_flow(cfg, "drive", client_id="cid", client_secret="csecret-view")
    _sign_in(f2)
    pub = f2.public()
    assert pub["status"] == "failed" and pub["code"] == "FLOW_FAILED" and pub["error"] and pub["params"] == {}
    assert "csecret-view" not in json.dumps(pub)


def test_flow_timeout_is_coded_too(cfg):
    flow = C.start_flow(cfg, "dropbox", timeout=0.4)
    assert flow.wait(10) and flow.public()["code"] == "FLOW_TIMEOUT" and flow.public()["status"] == "timeout"


def test_check_name_reports_what_a_flow_would_get(cfg, tmp_path):
    assert C.check_name(cfg, "dropbox", None) == "dropbox"
    assert C.check_name(cfg, "iclouddrive", None) == "icloud"
    _connect(cfg, "dropbox", tmp_path)
    assert C.check_name(cfg, "dropbox", None) == "dropbox-2"
    with pytest.raises(C.ConnectError) as e:
        C.check_name(cfg, "dropbox", "dropbox")
    assert e.value.code == "NAME_TAKEN"
    with pytest.raises(C.ConnectError) as e:
        C.check_name(cfg, "dropbox", "Nope!")
    assert e.value.code == "BAD_NAME"
    assert not C._RESERVED                                                              # checking reserves nothing


def test_a_name_asked_for_at_start_is_checked_at_once_and_used_at_finish(cfg, tmp_path):
    with pytest.raises(C.ConnectError) as e:
        C.start_flow(cfg, "dropbox", name="Bad Name")
    assert e.value.code == "BAD_NAME"
    flow = C.start_flow(cfg, "dropbox", name="photos")
    _sign_in(flow, tmp_path / "clouds" / "photos")
    assert C.finish_connect(cfg, flow.id)["name"] == "photos"


def test_a_bad_name_at_finish_keeps_the_signed_in_flow_for_another_try(cfg, tmp_path):
    flow = C.start_flow(cfg, "dropbox")
    _sign_in(flow, tmp_path / "clouds" / "x")
    with pytest.raises(C.ConnectError) as e:
        C.finish_connect(cfg, flow.id, name="Bad Name")
    assert e.value.code == "BAD_NAME" and flow.status == "succeeded"
    assert C.finish_connect(cfg, flow.id, name="x")["name"] == "x"
