"""[[remotes]] entries reach the catalog's `remotes` table, case sensitivity included."""

from __future__ import annotations

import sys

import pytest

from drivemerge import db, providers
from drivemerge.config import parse_config, sync_remotes


def _remote(name: str, provider: str, **extra) -> dict:
    return {"name": name, "provider": provider, "read": f"{name}:", **extra}


def _flags(conn) -> dict[str, int]:
    return {r["name"]: r["case_insensitive"] for r in conn.execute("SELECT name, case_insensitive FROM remotes")}


def test_a_remote_without_the_key_follows_its_provider():
    conn = db.connect(":memory:")
    sync_remotes(conn, parse_config({"remotes": [
        _remote("gd", "drive"), _remote("dbx", "dropbox"), _remote("od", "onedrive"), _remote("ic", "iclouddrive")]}))
    assert _flags(conn) == {"gd": 0, "dbx": 1, "od": 1, "ic": 0}


def test_a_local_disk_is_case_insensitive_only_where_the_operating_system_is():
    """APFS and NTFS fold case, ext4 does not; the executor's guard cannot be the only thing that knows."""
    assert providers.LOCAL_CASE_INSENSITIVE == (sys.platform in ("darwin", "win32"))
    assert providers.get("local").case_insensitive == providers.LOCAL_CASE_INSENSITIVE
    conn = db.connect(":memory:")
    sync_remotes(conn, parse_config({"remotes": [_remote("disk", "local")]}))
    assert _flags(conn) == {"disk": int(providers.LOCAL_CASE_INSENSITIVE)}


def test_the_key_overrides_the_provider_either_way():
    conn = db.connect(":memory:")
    cfg = parse_config({"remotes": [
        _remote("exfat", "local", case_insensitive=True), _remote("apfs-cs", "local", case_insensitive=False),
        _remote("dbx", "dropbox", case_insensitive=False)]})
    assert [r.case_insensitive for r in cfg.remotes] == [True, False, False]
    sync_remotes(conn, cfg)
    assert _flags(conn) == {"exfat": 1, "apfs-cs": 0, "dbx": 0}


def test_changing_the_key_updates_the_catalog_on_the_next_sync():
    conn = db.connect(":memory:")
    sync_remotes(conn, parse_config({"remotes": [_remote("disk", "local", case_insensitive=False)]}))
    assert _flags(conn) == {"disk": 0}
    sync_remotes(conn, parse_config({"remotes": [_remote("disk", "local", case_insensitive=True)]}))
    assert _flags(conn) == {"disk": 1}


@pytest.mark.parametrize("value", ["false", "no", 0, 1, ""])
def test_the_key_must_be_a_real_boolean(value):
    """The string "false" is truthy in Python: accepting it would silently flip the meaning."""
    with pytest.raises(ValueError, match="case_insensitive"):
        parse_config({"remotes": [_remote("disk", "local", case_insensitive=value)]})


# --- quarantine retention (docs/QUARANTINE.md section D) --------------------------------------------------

def test_quarantine_retention_defaults_to_thirty_days():
    assert parse_config({}).safety.quarantine_retention_days == 30
    assert parse_config({"safety": {"execution_enabled": True}}).safety.quarantine_retention_days == 30


def test_quarantine_retention_is_read_from_the_safety_table():
    assert parse_config({"safety": {"quarantine_retention_days": 7}}).safety.quarantine_retention_days == 7


@pytest.mark.parametrize("value", [0, -1, "30", 1.5, True, None])
def test_quarantine_retention_must_be_a_whole_number_of_days_of_at_least_one(value):
    with pytest.raises(ValueError, match="quarantine_retention_days"):
        parse_config({"safety": {"quarantine_retention_days": value}})


# --- writing the file (docs/ONBOARDING.md section G4.6) -----------------------------------------------
# drive-merge never edits a human's config.toml in place any more: connections.toml is rewritten whole. What stays
# is the private, atomic writer, a fresh config, and the rclone type -> provider map.

import os  # noqa: E402
import stat  # noqa: E402

from drivemerge import config as config_mod  # noqa: E402
from drivemerge.config import ensure_config, load_config, provider_of, write_private  # noqa: E402


def _leftovers(tmp_path, keep="config.toml"):
    return [p.name for p in tmp_path.iterdir() if p.name != keep]


def test_write_private_creates_and_replaces_with_mode_600(tmp_path):
    path = tmp_path / "sub" / "dir" / "x.toml"
    write_private(path, "a = 1\n")
    assert path.read_text() == "a = 1\n" and stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    write_private(path, "a = 2\n")
    assert path.read_text() == "a = 2\n" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _leftovers(path.parent, "x.toml") == []


def test_write_private_keeps_line_endings_as_given(tmp_path):
    path = tmp_path / "x.toml"
    write_private(path, "a = 1\r\nb = 2\r\n")
    assert path.read_bytes() == b"a = 1\r\nb = 2\r\n"


def test_a_write_goes_through_a_symbolic_link(tmp_path):
    real = tmp_path / "real" / "config.toml"
    real.parent.mkdir()
    real.write_text("old = 1\n")
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    write_private(link, "new = 1\n")
    assert link.is_symlink() and real.read_text() == "new = 1\n"


def test_the_write_is_atomic_the_old_file_survives_a_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("old = 1\n")

    def broken(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        write_private(path, "new = 1\n")
    assert path.read_text() == "old = 1\n" and _leftovers(tmp_path) == []          # temp file cleaned up


def test_the_new_text_is_written_to_a_temp_file_in_the_same_directory_and_fsynced(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("old = 1\n")
    synced, replaced = [], []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((a, b)), real_replace(a, b))[1])
    write_private(path, "new = 1\n")
    assert synced and len(replaced) == 1
    src, dst = replaced[0]
    assert os.path.dirname(src) == str(tmp_path) and str(dst) == str(path)


@pytest.mark.parametrize("rtype, provider", [("drive", "drive"), ("dropbox", "dropbox"), ("onedrive", "onedrive"),
                                             ("iclouddrive", "iclouddrive"), ("local", "local"), ("s3", "other"),
                                             ("crypt", "other"), ("", "other")])
def test_rclone_types_map_to_provider_keys(rtype, provider):
    assert provider_of(rtype) == provider


def test_ensure_config_creates_a_private_config_with_a_token_and_no_clouds(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "conf" / "config.toml"
    assert ensure_config(path) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cfg = load_config(path)
    assert len(cfg.api.token) >= 32 and cfg.remotes == []
    assert "# [[remotes]]" in path.read_text()                              # the examples stay as comments
    assert (tmp_path / "home/.local/share/drive-merge/catalog.db").exists()
    first = path.read_text()
    assert ensure_config(path) is False and path.read_text() == first       # an existing file is never touched


def test_write_new_config_is_what_init_writes_and_has_no_sample_clouds(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.toml"
    config_mod.write_new_config(path)
    assert load_config(path).remotes == [] and "# [[remotes]]" in path.read_text()      # the examples stay comments


def test_the_editing_helpers_of_the_setup_screen_are_gone():
    for gone in ("ConfigError", "add_remote_block", "set_remote_write", "PROVIDER_EXTRA_ARGS"):
        assert not hasattr(config_mod, gone), gone


# ----------------------------------------------------------- connections.toml and rclone.conf

def test_load_config_defaults_rclone_conf_next_to_the_config_and_merges_connections(tmp_path):
    from drivemerge.config import load_config
    main = tmp_path / "c" / "config.toml"
    main.parent.mkdir()
    main.write_text('[[remotes]]\nname = "mine"\nprovider = "local"\nread = "/x"\n')
    (main.parent / "connections.toml").write_text(
        '[[remotes]]\nname = "dropbox"\nprovider = "dropbox"\nread = "dropbox-ro:"\nconnection = true\n'
        'account = "a@b.c"\ncreated_at = "2026-01-01T00:00:00Z"\n')
    cfg = load_config(main)
    assert cfg.rclone_config == str(main.parent / "rclone.conf")
    assert [r.name for r in cfg.remotes] == ["mine", "dropbox"]            # main first, then connections
    assert [r.connection for r in cfg.remotes] == [False, True]
    assert cfg.config_path == main and cfg.connections_path == main.parent / "connections.toml"


def test_an_explicit_rclone_config_is_kept(tmp_path):
    from drivemerge.config import load_config
    main = tmp_path / "config.toml"
    main.write_text('[rclone]\nconfig = "/home/me/.config/rclone/rclone.conf"\n')
    assert load_config(main).rclone_config == "/home/me/.config/rclone/rclone.conf"
    assert parse_config({}).rclone_config is None                          # parse_config alone knows no directory


def test_duplicate_between_the_two_files_names_both(tmp_path):
    from drivemerge.config import load_config
    main = tmp_path / "config.toml"
    main.write_text('[[remotes]]\nname = "dbx"\nprovider = "dropbox"\nread = "dbx:"\n')
    (tmp_path / "connections.toml").write_text('[[remotes]]\nname = "dbx"\nprovider = "dropbox"\nread = "dbx-ro:"\n')
    with pytest.raises(ValueError, match=r"'dbx'.*config\.toml.*connections\.toml"):
        load_config(main)


def test_a_broken_connections_toml_is_a_clear_error(tmp_path):
    from drivemerge.config import load_config
    main = tmp_path / "config.toml"
    main.write_text("")
    (tmp_path / "connections.toml").write_text("[[remotes]\n")
    with pytest.raises(ValueError, match="connections.toml"):
        load_config(main)
    (tmp_path / "connections.toml").write_text('[[remotes]]\nname = "x"\nprovider = "bogus"\nread = "x:"\n')
    with pytest.raises(ValueError, match="unknown provider"):
        load_config(main)


def test_write_private_is_atomic_and_mode_600(tmp_path):
    import stat
    from drivemerge.config import write_private
    p = tmp_path / "sub" / "f"
    write_private(p, "a")
    p.chmod(0o644)
    write_private(p, "b")
    assert p.read_text() == "b" and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [x.name for x in p.parent.iterdir()] == ["f"]


# --------------------------------------------------------------------------- the rules file (docs/RULES.md A)

from pathlib import Path  # noqa: E402


def _config_file(tmp_path, extra=""):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(f'[database]\npath = "{tmp_path / "c.db"}"\n{extra}')
    return d / "config.toml"


def test_rules_file_is_the_configured_path_else_rules_toml_next_to_the_config(tmp_path):
    plain = load_config(_config_file(tmp_path))
    assert plain.rules_path is None and plain.rules_owned is True
    assert plain.rules_file == tmp_path / "cfg" / "rules.toml"
    configured = load_config(_config_file(tmp_path, '[rules]\npath = "~/mine/layout.toml"\n'))
    assert configured.rules_owned is False
    assert configured.rules_file == Path("~/mine/layout.toml").expanduser()
    absolute = load_config(_config_file(tmp_path, f'[rules]\npath = "{tmp_path}/x.toml"\n'))
    assert absolute.rules_file == tmp_path / "x.toml"


def test_a_config_built_in_memory_has_no_rules_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert parse_config({}).rules_file is None and parse_config({}).rules_owned is True
    assert parse_config({"rules": {"path": "/x/y.toml"}}).rules_file == Path("/x/y.toml")


def test_existing_users_without_a_rules_path_keep_the_packaged_defaults(tmp_path):
    from drivemerge import classify
    from drivemerge.service import Service
    cfg = load_config(_config_file(tmp_path))
    svc = Service(cfg, config_path=cfg.config_path)
    assert svc.ruleset().rules == classify.default_rules().rules
    assert svc.rules_state()["active"]["source"] == "default"
    assert not (tmp_path / "cfg" / "rules.toml").exists()                 # nothing is written just by looking


def test_an_owned_rules_toml_is_picked_up_without_a_rules_path(tmp_path):
    from drivemerge.service import Service
    cfg = load_config(_config_file(tmp_path))
    (tmp_path / "cfg" / "rules.toml").write_text('[[rule]]\nname = "only"\ntarget = "X/{name}"\n')
    svc = Service(cfg, config_path=cfg.config_path)
    assert [r.name for r in svc.ruleset().rules] == ["only"]
    assert svc.rules_state()["active"]["path"] == str(tmp_path / "cfg" / "rules.toml")


def test_a_configured_rules_path_whose_file_is_missing_is_an_error_not_the_defaults(tmp_path):
    from drivemerge.rulestore import RulesError
    from drivemerge.service import Service
    cfg = load_config(_config_file(tmp_path, f'[rules]\npath = "{tmp_path}/nope.toml"\n'))
    svc = Service(cfg, config_path=cfg.config_path)
    with pytest.raises(RulesError) as exc:
        svc.ruleset()
    assert exc.value.code == "RULES_FILE_MISSING" and exc.value.params == {"path": f"{tmp_path}/nope.toml"}
    assert exc.value.status == 409
    (tmp_path / "nope.toml").write_text('[[rule]]\nname = "here"\ntarget = "X"\n')
    assert [r.name for r in svc.ruleset().rules] == ["here"]


def test_the_configured_file_wins_over_an_owned_one_and_the_owned_one_is_never_written_by_loading(tmp_path):
    from drivemerge.service import Service
    cfg = load_config(_config_file(tmp_path, f'[rules]\npath = "{tmp_path}/mine.toml"\n'))
    (tmp_path / "cfg" / "rules.toml").write_text('[[rule]]\nname = "owned"\ntarget = "O"\n')
    (tmp_path / "mine.toml").write_text('[[rule]]\nname = "mine"\ntarget = "M"\n')
    assert [r.name for r in Service(cfg, config_path=cfg.config_path).ruleset().rules] == ["mine"]
    before = (tmp_path / "cfg" / "config.toml").read_bytes()
    Service(cfg, config_path=cfg.config_path).rules_state()
    assert (tmp_path / "cfg" / "config.toml").read_bytes() == before
