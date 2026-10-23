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
    for var in ("FAKE_RCLONE_AUTH_MODE", "FAKE_RCLONE_TEST_FAIL", "FAKE_RCLONE_REAL", "FAKE_RCLONE_LOG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    yield
    for f in list(C._FLOWS.values()):
        C.cancel_flow(f.id)
    C._FLOWS.clear()


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
    assert set(C.PROVIDER_SPECS) == {"drive", "onedrive", "dropbox"}
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
