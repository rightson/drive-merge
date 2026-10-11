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
