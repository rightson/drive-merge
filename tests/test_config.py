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


# --- writing the file (docs/SETUP.md section B) ------------------------------------------------------

import os  # noqa: E402
import stat  # noqa: E402
import tomllib  # noqa: E402

from drivemerge import config as config_mod  # noqa: E402
from drivemerge.config import (  # noqa: E402
    ConfigError, RemoteConfig, add_remote_block, ensure_config, load_config, provider_of, set_remote_write,
)

HAND_WRITTEN = '''# my own notes, above everything
[database]
path = "/tmp/catalog.db"   # trailing comment

[safety]
execution_enabled = false  # keep it off

# the clouds
[[remotes]]
name = "dbx"           # first one
provider = "dropbox"
read = "dbx:"

[[remotes]]
# the second
name = "disk"
provider = "local"
read = "/mnt/disk"
write = "/mnt/disk"
extra_args = [
  "--one",
  "--two",
]

[api]
token = "keep-me"
'''


def _file(tmp_path, text=HAND_WRITTEN):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _tables(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))["remotes"]


def _leftovers(tmp_path):
    return [p.name for p in tmp_path.iterdir() if p.name != "config.toml"]


def test_add_remote_block_appends_and_keeps_everything_else_byte_for_byte(tmp_path):
    path = _file(tmp_path)
    add_remote_block(path, RemoteConfig("gd", "drive", "gd:"))
    text = path.read_text(encoding="utf-8")
    assert text.startswith(HAND_WRITTEN.rstrip("\n"))                 # comments, order and spacing are untouched
    assert text.endswith('[[remotes]]\nname = "gd"\nprovider = "drive"\nread = "gd:"\n'
                         'extra_args = ["--fast-list", "--drive-skip-shortcuts"]\n')
    assert [r["name"] for r in _tables(path)] == ["dbx", "disk", "gd"]
    cfg = load_config(path)
    assert cfg.api.token == "keep-me" and cfg.remote("gd").write is None
    assert cfg.remote("gd").extra_args == ("--fast-list", "--drive-skip-shortcuts")
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("provider, extra", [("dropbox", ()), ("onedrive", ()), ("iclouddrive", ()), ("other", ()),
                                             ("drive", ("--fast-list", "--drive-skip-shortcuts"))])
def test_provider_defaults_written_into_new_blocks(tmp_path, provider, extra):
    path = _file(tmp_path)
    add_remote_block(path, RemoteConfig("new", provider, "new:"))
    assert load_config(path).remote("new").extra_args == extra
    assert ("extra_args" in _tables(path)[-1]) == bool(extra)


def test_add_remote_block_writes_the_fields_it_was_given_and_quotes_them(tmp_path):
    path = _file(tmp_path)
    odd = 'we"ird\\name ☃'
    add_remote_block(path, RemoteConfig("x", "local", odd + ":", write=odd, hash_types=("md5",),
                                        extra_args=("--a", 'b"c'), case_insensitive=True))
    table = _tables(path)[-1]
    assert table == {"name": "x", "provider": "local", "read": odd + ":", "write": odd, "hash_types": ["md5"],
                     "extra_args": ["--a", 'b"c'], "case_insensitive": True}


def test_add_remote_block_to_an_empty_or_remote_less_file(tmp_path):
    path = _file(tmp_path, "")
    add_remote_block(path, RemoteConfig("a", "dropbox", "a:"))
    assert path.read_text() == '[[remotes]]\nname = "a"\nprovider = "dropbox"\nread = "a:"\n'
    path = _file(tmp_path, '[api]\ntoken = "t"')                    # no trailing newline
    add_remote_block(path, RemoteConfig("a", "dropbox", "a:"))
    assert load_config(path).api.token == "t" and [r.name for r in load_config(path).remotes] == ["a"]


def test_add_remote_block_refuses_a_duplicate_name_and_leaves_the_file_alone(tmp_path):
    path = _file(tmp_path)
    with pytest.raises(ConfigError) as ei:
        add_remote_block(path, RemoteConfig("dbx", "dropbox", "other:"))
    assert ei.value.code == "REMOTE_EXISTS" and ei.value.params == {"name": "dbx"}
    assert path.read_text(encoding="utf-8") == HAND_WRITTEN and _leftovers(tmp_path) == []


def test_the_file_mode_is_600_after_every_write(tmp_path):
    path = _file(tmp_path)
    path.chmod(0o644)
    add_remote_block(path, RemoteConfig("a", "dropbox", "a:"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    set_remote_write(path, "a", "a:")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_write_goes_through_a_symbolic_link(tmp_path):
    real = tmp_path / "real" / "config.toml"
    real.parent.mkdir()
    real.write_text(HAND_WRITTEN)
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    add_remote_block(link, RemoteConfig("a", "dropbox", "a:"))
    assert link.is_symlink() and "a:" in real.read_text()


def test_the_write_is_atomic_the_old_file_survives_a_failed_replace(tmp_path, monkeypatch):
    path = _file(tmp_path)

    def broken(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(ConfigError) as ei:
        add_remote_block(path, RemoteConfig("a", "dropbox", "a:"))
    assert ei.value.code == "CONFIG_WRITE"
    assert path.read_text(encoding="utf-8") == HAND_WRITTEN and _leftovers(tmp_path) == []   # temp file cleaned up


def test_the_new_text_is_written_to_a_temp_file_in_the_same_directory_and_fsynced(tmp_path, monkeypatch):
    path = _file(tmp_path)
    synced, replaced = [], []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((a, b)), real_replace(a, b))[1])
    add_remote_block(path, RemoteConfig("a", "dropbox", "a:"))
    assert synced and len(replaced) == 1
    src, dst = replaced[0]
    assert os.path.dirname(src) == str(tmp_path) and str(dst) == str(path)


def test_a_result_that_does_not_parse_leaves_the_file_untouched(tmp_path, monkeypatch):
    path = _file(tmp_path)
    monkeypatch.setattr(config_mod, "_toml_str", lambda value: value)            # emits an invalid block
    with pytest.raises(ConfigError) as ei:
        add_remote_block(path, RemoteConfig("a", "dropbox", "a:"))
    assert ei.value.code == "CONFIG_LAYOUT"
    assert path.read_text(encoding="utf-8") == HAND_WRITTEN and _leftovers(tmp_path) == []


def test_remotes_written_as_an_inline_array_cannot_take_a_block(tmp_path):
    text = 'remotes = [{name = "dbx", provider = "dropbox", read = "dbx:"}]\n'
    path = _file(tmp_path, text)
    with pytest.raises(ConfigError) as ei:
        add_remote_block(path, RemoteConfig("a", "dropbox", "a:"))
    assert ei.value.code == "CONFIG_LAYOUT"
    assert path.read_text() == text


def test_a_file_that_is_not_toml_is_not_touched(tmp_path):
    path = _file(tmp_path, "this is = = not toml\n")
    for call in (lambda: add_remote_block(path, RemoteConfig("a", "dropbox", "a:")),
                 lambda: set_remote_write(path, "a", "a:")):
        with pytest.raises(ConfigError) as ei:
            call()
        assert ei.value.code == "CONFIG_LAYOUT"
    assert path.read_text() == "this is = = not toml\n"


def test_a_missing_file_is_a_write_error(tmp_path):
    with pytest.raises(ConfigError) as ei:
        add_remote_block(tmp_path / "nope.toml", RemoteConfig("a", "dropbox", "a:"))
    assert ei.value.code == "CONFIG_WRITE"


def test_set_remote_write_replaces_the_line_in_place(tmp_path):
    path = _file(tmp_path)
    set_remote_write(path, "disk", "/mnt/other")
    expected = HAND_WRITTEN.replace('write = "/mnt/disk"', 'write = "/mnt/other"')
    assert path.read_text(encoding="utf-8") == expected                        # nothing else moved, comments kept


def test_set_remote_write_inserts_after_read_and_can_remove(tmp_path):
    path = _file(tmp_path)
    set_remote_write(path, "dbx", "dbx-rw:")
    text = path.read_text(encoding="utf-8")
    assert text == HAND_WRITTEN.replace('read = "dbx:"\n', 'read = "dbx:"\nwrite = "dbx-rw:"\n')
    assert load_config(path).remote("dbx").write == "dbx-rw:" and load_config(path).remote("disk").write == "/mnt/disk"
    set_remote_write(path, "dbx", None)
    assert path.read_text(encoding="utf-8") == HAND_WRITTEN
    set_remote_write(path, "disk", None)
    assert load_config(path).remote("disk").write is None and load_config(path).remote("disk").extra_args == (
        "--one", "--two")


def test_set_remote_write_only_touches_the_named_block(tmp_path):
    path = _file(tmp_path, HAND_WRITTEN.replace('name = "disk"', 'name = "disk2"') + "\n"
                 '[[remotes]]\nname = "disk"\nprovider = "local"\nread = "/x"\nwrite = "/y"\n')
    set_remote_write(path, "disk", "/z")
    cfg = load_config(path)
    assert cfg.remote("disk").write == "/z" and cfg.remote("disk2").write == "/mnt/disk"


def test_set_remote_write_unknown_remote(tmp_path):
    path = _file(tmp_path)
    with pytest.raises(ConfigError) as ei:
        set_remote_write(path, "nope", "x:")
    assert ei.value.code == "REMOTE_NOT_FOUND" and ei.value.params == {"name": "nope"}
    assert path.read_text(encoding="utf-8") == HAND_WRITTEN


def test_set_remote_write_refuses_inline_tables(tmp_path):
    text = 'remotes = [{name = "dbx", provider = "dropbox", read = "dbx:"}]\n'
    path = _file(tmp_path, text)
    with pytest.raises(ConfigError) as ei:
        set_remote_write(path, "dbx", "dbx:")
    assert ei.value.code == "CONFIG_LAYOUT"
    assert path.read_text() == text
    text = '[[remotes]]\nname = "a"\nread = "a:"\n[[remotes]]\nname = "b"\nread = "b:"\n\n' \
           '[extra]\nremotes = [ { name = "a" } ]\n'
    path = _file(tmp_path, text)
    set_remote_write(path, "b", "b:")                                       # still found line by line
    assert load_config(path).remote("b").write == "b:" and "extra" in path.read_text()


def test_set_remote_write_with_a_result_that_would_change_more_leaves_the_file(tmp_path, monkeypatch):
    path = _file(tmp_path)
    monkeypatch.setattr(config_mod, "_toml_str", lambda value: '"x"\nprovider = "drive"')   # smuggles a second key
    with pytest.raises(ConfigError) as ei:
        set_remote_write(path, "dbx", "dbx-rw:")
    assert ei.value.code == "CONFIG_LAYOUT"
    assert path.read_text(encoding="utf-8") == HAND_WRITTEN


def test_crlf_files_keep_their_line_endings(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(HAND_WRITTEN.replace("\n", "\r\n").encode())
    set_remote_write(path, "dbx", "dbx-rw:")
    add_remote_block(path, RemoteConfig("a", "dropbox", "a:"))
    raw = path.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert load_config(path).remote("dbx").write == "dbx-rw:"


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
    add_remote_block(path, RemoteConfig("gd", "drive", "gd:"))              # and it takes a block right away
    assert [r.name for r in load_config(path).remotes] == ["gd"]


def test_write_new_config_is_what_init_writes_and_has_no_sample_clouds(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.toml"
    config_mod.write_new_config(path)
    assert load_config(path).remotes == [] and "# [[remotes]]" in path.read_text()      # the examples stay comments


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
