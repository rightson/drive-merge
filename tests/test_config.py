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
