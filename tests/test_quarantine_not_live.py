"""Files parked in the quarantine folder are deleted files: no module may count them as live content.

Without this, every cleaned-up copy shows up as a duplicate of the file that replaced it (CLI, API, folder
badges in the workspace) and the suggestion engine offers to merge folders out of quarantine.
"""

from __future__ import annotations

import hashlib

import pytest

from drivemerge import db, dirstats, identity, suggest, workspace
from drivemerge.config import Config, RemoteConfig, SafetyConfig, sync_remotes

Q = ".drive-merge-quarantine"


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


@pytest.fixture
def world(conn, catalog):
    """A live file and the quarantined copy of its old twin, plus a folder pair that quarantine mirrors."""
    g = catalog.remote("gd", "drive")
    d = catalog.remote("dbx", "dropbox")
    for name in ("one.docx", "two.docx", "three.docx"):
        catalog.file(g, f"Research/Thesis/{name}", size=10, md5=md5(name))
        catalog.file(d, f"{Q}/7/School/Thesis/{name}", size=10, md5=md5(name))
    catalog.file(d, "School/keep.txt", size=5, md5=md5("keep"))
    identity.rebuild_groups(conn)
    dirstats.refresh(conn)
    return conn, g, d


def test_outside_quarantine_matches_whole_path_segments_only(conn, catalog):
    g, other = catalog.remote("gd", "drive"), catalog.remote("od", "onedrive")
    for p in (f"{Q}/1/a.txt", f"{Q}x/a.txt", f"x/{Q}/a.txt"):
        catalog.file(g, p, size=1)
    catalog.file(other, Q, size=1)  # a path equal to the folder itself counts as inside
    outside = {r[0] for r in conn.execute(f"SELECT path FROM entries WHERE is_dir = 0 AND {db.outside_quarantine(conn)}")}
    assert outside == {f"{Q}x/a.txt", f"x/{Q}/a.txt"}
    inside = {r[0] for r in conn.execute(
        f"SELECT path FROM entries WHERE is_dir = 0 AND NOT ({db.outside_quarantine(conn)})")}
    assert inside == {Q, f"{Q}/1/a.txt"}


def test_the_folder_comes_from_the_config_and_empty_means_none(conn, catalog):
    g = catalog.remote("gd", "drive")
    catalog.file(g, "trash/a.txt", size=1)
    assert db.quarantine_dir(conn) == Q  # nothing recorded yet: the default
    cfg = Config(remotes=[RemoteConfig("gd", "drive", "gd:", "gd:")], safety=SafetyConfig(quarantine_dir="/trash/"))
    sync_remotes(conn, cfg)
    assert db.quarantine_dir(conn) == "trash"
    assert conn.execute(f"SELECT COUNT(*) FROM entries WHERE NOT ({db.outside_quarantine(conn)}) AND is_dir = 0"
                        ).fetchone()[0] == 1
    cfg.safety.quarantine_dir = ""
    sync_remotes(conn, cfg)
    assert db.outside_quarantine(conn) == "1"


def test_quote_in_the_folder_name_cannot_break_the_sql(conn, catalog):
    g = catalog.remote("gd", "drive")
    catalog.file(g, "it's/a.txt", size=1)
    cfg = Config(remotes=[RemoteConfig("gd", "drive", "gd:", "gd:")], safety=SafetyConfig(quarantine_dir="it's"))
    sync_remotes(conn, cfg)
    assert conn.execute(f"SELECT COUNT(*) FROM entries WHERE is_dir = 0 AND {db.outside_quarantine(conn)}"
                        ).fetchone()[0] == 0


def test_quarantined_copies_are_not_duplicates(world):
    conn, g, d = world
    assert identity.duplicate_groups(conn) == []
    assert identity.duplicate_groups(conn, confidence="probable") == []
    ids = [r[0] for r in conn.execute("SELECT id FROM entries WHERE is_dir = 0 AND path LIKE ?", (f"{Q}/%",))]
    assert ids and all(conn.execute("SELECT content_group FROM entries WHERE id = ?", (i,)).fetchone()[0] is None
                       for i in ids)
    live = conn.execute("SELECT id FROM entries WHERE path = 'Research/Thesis/one.docx'").fetchone()[0]
    assert [c.path for c in identity.locations(conn, live)] == ["Research/Thesis/one.docx"]
    assert not identity.same_content(conn, live, ids[0])


def test_a_group_forgets_a_file_that_moves_into_quarantine(world):
    conn, g, d = world
    e = conn.execute("SELECT id FROM entries WHERE remote_id = ? AND path = 'School/keep.txt'", (d,)).fetchone()[0]
    twin = conn.execute("INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category, "
                        "is_dir, size, md5, present, first_seen_scan, last_seen_scan, row_version) "
                        "SELECT remote_id, 'p:Docs/keep.txt', 'Docs/keep.txt', 'Docs', 'keep.txt', 'keep.txt', 'txt', "
                        "category, 0, size, md5, 1, first_seen_scan, last_seen_scan, row_version "
                        "FROM entries WHERE id = ? RETURNING id", (e,)).fetchone()[0]
    identity.rebuild_groups(conn)
    assert len(identity.duplicate_groups(conn)) == 1
    conn.execute("UPDATE entries SET path = ?, parent = ? WHERE id = ?", (f"{Q}/9/Docs/keep.txt", f"{Q}/9/Docs", twin))
    identity.rebuild_groups(conn)
    assert identity.duplicate_groups(conn) == []
    assert conn.execute("SELECT content_group FROM entries WHERE id = ?", (twin,)).fetchone()[0] is None


def test_folder_stats_do_not_flag_quarantined_copies(world):
    conn, g, d = world
    assert dirstats.get(conn, g, "Research/Thesis")["dup_files"] == 0
    assert dirstats.get(conn, d, "")["dup_files"] == 0
    assert dirstats.get(conn, d, Q)["files"] == 3  # they still occupy the cloud and are counted as files


def test_workspace_marks_no_duplicates_and_keeps_quarantine_listed(world):
    conn, g, d = world
    items = {i["name"]: i for i in workspace.list_dir(conn, "gd", "Research/Thesis")["items"]}
    assert not any(i["dup_files"] for i in items.values())
    roots = {r["remote"]: r for r in workspace.roots(conn)}
    assert roots["gd"]["dup_files"] == 0 and roots["dbx"]["dup_files"] == 0
    assert Q in {i["name"] for i in workspace.list_dir(conn, "dbx", "")["items"]}


def test_suggestions_ignore_the_quarantine(world):
    conn, g, d = world
    assert suggest.related_folders(conn, min_files=2) == []
    assert suggest.duplicate_folders(conn, min_files=2) == []
