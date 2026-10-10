"""Visual workspace engine: draft store, virtual namespace, plan building.

The last section is a differential test: ~200 random actions over a small
multi-remote tree, with every directory listing, every statistic, every staging
verdict and the net-effect ops compared with a naive model that moves each
catalog entry one by one.
"""

from __future__ import annotations

import posixpath
import random
import time
import unicodedata
from collections import Counter

import pytest

from drivemerge import db, dirstats, plans, simulate
from drivemerge import workspace as ws
from drivemerge.workspace import WorkspaceError

# --------------------------------------------------------------------- helpers


def put(cat, rid, path, *, group=None, size=1, **kw) -> int:
    """catalog.file plus an optional content_group (hash-proven identity)."""
    eid = cat.file(rid, path, size=size, **kw)
    if group is not None:
        cat.conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (group, eid))
    return eid


def refresh_stats(conn) -> None:
    """Brute-force dir_stats, so the additive after-view formula has an exact 'before'."""
    conn.execute("DELETE FROM dir_stats")
    ents = [dict(r) for r in conn.execute("SELECT * FROM entries WHERE present = 1")]
    copies = Counter(e["content_group"] for e in ents
                     if not e["is_dir"] and e["content_group"] is not None and (e["size"] or 0) > 0)
    dirs = {(e["remote_id"], e["path"]) for e in ents if e["is_dir"]}
    dirs |= {(r[0], "") for r in conn.execute("SELECT id FROM remotes")}
    for rid, d in dirs:
        under = [e for e in ents if e["remote_id"] == rid and (d == "" or e["path"].startswith(d + "/"))]
        files = [e for e in under if not e["is_dir"]]
        conn.execute(
            "INSERT INTO dir_stats(remote_id, path, files, dirs, bytes, dup_files) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, d, len(files), sum(1 for e in under if e["is_dir"]), sum(e["size"] or 0 for e in files),
             sum(1 for e in files if e["content_group"] is not None and (e["size"] or 0) > 0
                 and copies[e["content_group"]] >= 2)))


def L(spec: str) -> dict:
    """'gdrive:a/b' -> {'remote': 'gdrive', 'path': 'a/b'}"""
    remote, _, path = spec.partition(":")
    return {"remote": remote, "path": path}


def new_draft(conn, title="t") -> int:
    return ws.create_draft(conn, title)["id"]


def mv(conn, d, items, dst, **kw) -> dict:
    items = [items] if isinstance(items, str) else items
    return ws.add_action(conn, d, {"kind": "move", "items": [L(i) for i in items], "dst": L(dst), **kw},
                         created_by="test")


def mkdir(conn, d, dst, name) -> dict:
    return ws.add_action(conn, d, {"kind": "mkdir", "dst": L(dst), "new_name": name}, created_by="test")


def refuse(conn, d, code, items, dst, **kw) -> WorkspaceError:
    version = ws.get_draft(conn, d)["version"]
    with pytest.raises(WorkspaceError) as e:
        mv(conn, d, items, dst, **kw)
    assert e.value.code == code, str(e.value)
    assert ws.get_draft(conn, d)["version"] == version      # a refused action leaves no trace
    return e.value


def codes(result) -> list[str]:
    return [w["code"] for w in result["warnings"]]


def ls(conn, spec: str, d=None, **kw) -> dict[str, dict]:
    remote, _, path = spec.partition(":")
    out = ws.list_dir(conn, remote, path, draft_id=d, limit=100000, **kw)
    return {i["name"]: i for i in out["items"]}


def describe(conn, ops) -> list[tuple]:
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM remotes")}
    out = []
    for o in ops:
        src = f"{names[o.src_remote_id]}:{o.src_path}"
        if o.op == "delete":
            out.append(("delete", src, f"{names[o.witness_remote_id]}:{o.witness_path}"))
        else:
            out.append((o.op, src, f"{names[o.dst_remote_id]}:{o.dst_path}"))
    return out


def sim(conn, ops, **kw):
    return simulate.simulate_ops(conn, ops, policy=simulate.SafetyPolicy(**kw))


def vcodes(report) -> list[str]:
    return [v.code for v in report.violations]


@pytest.fixture
def world(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    x = catalog.remote("dropbox", "dropbox")      # case-insensitive
    return conn, catalog, g, x


# ----------------------------------------------------------------- draft store


def test_draft_lifecycle(conn):
    d = ws.create_draft(conn, "  my plan ")
    assert d["title"] == "my plan" and d["status"] == "open" and d["version"] == 0
    assert d["actions"] == [] and not d["undo_available"] and not d["redo_available"]
    assert d["summary"] == {"moved_files": 0, "moved_bytes": 0, "cross_remote_bytes": 0, "remotes_touched": [],
                            "stale_actions": 0}
    assert ws.create_draft(conn, "   ")["title"] == "Untitled"
    other = ws.create_draft(conn, "other")["id"]
    assert [x["id"] for x in ws.list_drafts(conn)] == [other, d["id"] + 1, d["id"]]      # newest first
    ws.discard_draft(conn, other)
    assert other not in [x["id"] for x in ws.list_drafts(conn)]
    assert [x["id"] for x in ws.list_drafts(conn, status="discarded")] == [other]
    assert other in [x["id"] for x in ws.list_drafts(conn, status=None)]
    ws.discard_draft(conn, other)                           # idempotent
    assert ws.get_draft(conn, other)["status"] == "discarded"
    with pytest.raises(WorkspaceError) as e:
        ws.get_draft(conn, 999)
    assert e.value.code == "NOT_FOUND"


def test_closed_drafts_refuse_edits(world):
    conn, cat, g, _ = world
    put(cat, g, "a/f.txt")
    cat.dir(g, "b")
    d = new_draft(conn)
    mv(conn, d, "gdrive:a", "gdrive:b")
    v = ws.get_draft(conn, d)["version"]
    ws.discard_draft(conn, d)
    assert ws.get_draft(conn, d)["version"] == v + 1        # a discard makes any preview stale
    for call in (lambda: mv(conn, d, "gdrive:b/a", "gdrive:"), lambda: ws.undo(conn, d),
                 lambda: ws.redo(conn, d), lambda: ws.remove_action(conn, d, 1), lambda: ws.preview(conn, d),
                 lambda: ws.mark_applied(conn, d)):
        with pytest.raises(WorkspaceError) as e:
            call()
        assert e.value.code == "NOT_OPEN"
    d2 = new_draft(conn)
    ws.mark_applied(conn, d2)
    assert ws.get_draft(conn, d2)["status"] == "applied"
    with pytest.raises(WorkspaceError):
        ws.discard_draft(conn, d2)


def test_unknown_remote_and_malformed_actions(world):
    conn, cat, g, _ = world
    put(cat, g, "a/f.txt")
    d = new_draft(conn)
    refuse(conn, d, "SRC_NOT_FOUND", "nowhere:a", "gdrive:")
    refuse(conn, d, "DST_NOT_DIR", "gdrive:a", "nowhere:")
    refuse(conn, d, "SRC_NOT_FOUND", "gdrive:", "gdrive:a")                 # a remote root cannot be moved
    for bad in ({}, {"kind": "copy"}, {"kind": "move", "items": [], "dst": L("gdrive:")},
                {"kind": "move", "dst": L("gdrive:")}, None):
        with pytest.raises(WorkspaceError) as e:
            ws.add_action(conn, d, bad, created_by="t")
        assert e.value.code in ("BAD_REQUEST",)
    with pytest.raises(WorkspaceError) as e:
        ws.add_action(conn, d, {"kind": "mkdir", "dst": L("gdrive:")}, created_by="t")
    assert e.value.code == "BAD_NAME"
    with pytest.raises(WorkspaceError) as e:
        ws.list_dir(conn, "nowhere", "")
    assert e.value.code == "NOT_FOUND"
    with pytest.raises(WorkspaceError) as e:
        ws.list_dir(conn, "gdrive", "", view="sideways")
    assert e.value.code == "BAD_REQUEST"
    with pytest.raises(WorkspaceError) as e:
        ws.list_dir(conn, "gdrive", "", draft_id=42)
    assert e.value.code == "NOT_FOUND"


# ------------------------------------------------------------------ composition


def test_drag_a_into_b_then_b_into_c(world):
    conn, cat, g, _ = world
    put(cat, g, "A/x.txt", size=3, md5="aa", group=1)
    put(cat, g, "B/y.txt", size=4, md5="bb", group=2)
    put(cat, g, "C/z.txt", size=5, md5="cc", group=3)
    refresh_stats(conn)
    d = new_draft(conn)
    assert mv(conn, d, "gdrive:A", "gdrive:B")["draft_version"] == 1
    assert mv(conn, d, "gdrive:B", "gdrive:C")["draft_version"] == 2

    assert list(ls(conn, "gdrive:", d)) == ["C"]
    top = ls(conn, "gdrive:C", d)
    assert list(top) == ["B", "z.txt"] and top["B"]["is_dir"] and top["B"]["moved_here"]
    assert not top["z.txt"]["moved_here"]
    assert list(ls(conn, "gdrive:C/B", d)) == ["A", "y.txt"]
    assert list(ls(conn, "gdrive:C/B/A", d)) == ["x.txt"]
    assert ls(conn, "gdrive:C/B/A", d)["x.txt"]["origins"] == [{"remote": "gdrive", "path": "A/x.txt"}]
    # the sources are gone from the after-view, but the before-view is untouched
    assert ls(conn, "gdrive:A", d) == {} and ls(conn, "gdrive:B", d) == {}
    assert list(ls(conn, "gdrive:", d, view="before")) == ["A", "B", "C"]
    assert list(ls(conn, "gdrive:", None)) == ["A", "B", "C"]
    # stats: 'before' comes from dir_stats, the after-view is additive
    c = ls(conn, "gdrive:", d)["C"]
    assert (c["files"], c["bytes"], c["dirs"]) == (3, 12, 2)
    assert ls(conn, "gdrive:C", d)["B"]["files"] == 2
    root = ws.roots(conn, draft_id=d)[0]
    assert (root["files"], root["dirs"], root["bytes"]) == (3, 3, 12)       # C, C/B, C/B/A
    assert ws.roots(conn)[0]["dirs"] == 3 and ws.roots(conn)[0]["files"] == 3

    # a source that was dragged away no longer exists to drag again
    refuse(conn, d, "SRC_NOT_FOUND", "gdrive:B/y.txt", "gdrive:C")
    refuse(conn, d, "SRC_NOT_FOUND", "gdrive:A", "gdrive:C")

    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "gdrive:A/x.txt", "gdrive:C/B/A/x.txt"),
                                   ("move", "gdrive:B/y.txt", "gdrive:C/B/y.txt")]
    assert [(c["from"]["path"], c["to"]["path"], c["files"], c["bytes"], c["kind"]) for c in info["changes"]] == [
        ("A", "C/B/A", 1, 3, "move"), ("B", "C/B", 1, 4, "move")]
    assert info["empty_dirs_left"] == 2 and info["dedupes"] == 0 and info["conflicts"] == []
    assert all(f"draft #{d}" in o.reason for o in ops)
    assert ws.get_draft(conn, d)["summary"] == {"moved_files": 2, "moved_bytes": 7, "cross_remote_bytes": 0,
                                                "remotes_touched": ["gdrive"], "stale_actions": 0}
    assert sim(conn, ops).ok


def test_ghosts_show_where_things_went(world):
    conn, cat, g, _ = world
    put(cat, g, "A/x.txt")
    put(cat, g, "A/y.txt")
    put(cat, g, "B/z.txt")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/x.txt", "gdrive:B")
    mv(conn, d, "gdrive:A/y.txt", "gdrive:A", new_name="renamed.txt")
    out = ws.list_dir(conn, "gdrive", "A", draft_id=d, ghosts=True)
    assert [i["name"] for i in out["items"]] == ["renamed.txt"]
    assert out["items"][0]["moved_here"] and out["items"][0]["origins"][0]["path"] == "A/y.txt"
    assert out["ghosts"] == [
        {"name": "x.txt", "path": "A/x.txt", "is_dir": False, "moved_to": {"remote": "gdrive", "path": "B/x.txt"}},
        {"name": "y.txt", "path": "A/y.txt", "is_dir": False,
         "moved_to": {"remote": "gdrive", "path": "A/renamed.txt"}}]
    assert ws.list_dir(conn, "gdrive", "A", draft_id=d)["ghosts"] == []
    assert ws.list_dir(conn, "gdrive", "A", draft_id=d, view="before", ghosts=True)["ghosts"] == []
    assert ws.list_dir(conn, "gdrive", "A", draft_id=d)["draft_version"] == 2
    assert ws.list_dir(conn, "gdrive", "A")["draft_version"] is None


def test_dragging_away_and_back_is_a_net_noop(world):
    conn, cat, g, x = world
    put(cat, g, "A/x.txt", size=3, group=1)
    put(cat, g, "A/sub/y.txt", size=4, group=2)
    put(cat, g, "B/z.txt", size=5, group=3)
    put(cat, x, "far/w.txt", size=6, group=4)
    refresh_stats(conn)
    before = {p: ls(conn, p) for p in ("gdrive:", "gdrive:A", "gdrive:B", "dropbox:")}
    d = new_draft(conn)
    mv(conn, d, "gdrive:A", "gdrive:B")                        # B/A
    mv(conn, d, "gdrive:B/A", "dropbox:far")                   # across clouds
    mv(conn, d, "dropbox:far/A", "gdrive:")                    # and home again
    mv(conn, d, "gdrive:B/z.txt", "gdrive:A", new_name="zz.txt")
    mv(conn, d, "gdrive:A/zz.txt", "gdrive:B", new_name="z.txt")
    ops, info = ws.build_ops(conn, d)
    assert ops == [] and info["changes"] == [] and info["empty_dirs_left"] == 0
    assert ws.preview(conn, d) == {"plan_id": None, "message": "nothing to do", "stale_actions": [],
                                   "empty_dirs_skipped": []}
    assert ws.get_draft(conn, d)["summary"]["moved_files"] == 0
    for p, items in before.items():
        after = ls(conn, p, d)
        assert list(after) == list(items)
        for name, it in after.items():
            assert (it["files"], it["dirs"], it["bytes"], it["dup_files"]) == \
                   (items[name]["files"], items[name]["dirs"], items[name]["bytes"], items[name]["dup_files"])
            assert not it["moved_here"] and it["origins"] == items[name]["origins"]
    assert [(r["files"], r["dirs"], r["bytes"]) for r in ws.roots(conn, draft_id=d)] == \
           [(r["files"], r["dirs"], r["bytes"]) for r in ws.roots(conn)]


def test_swapping_a_name_back_with_a_rename_is_a_noop_too(world):
    conn, cat, g, _ = world
    put(cat, g, "P/f.txt", group=1)
    cat.dir(g, "Q")
    d = new_draft(conn)
    mv(conn, d, "gdrive:P/f.txt", "gdrive:Q")
    mv(conn, d, "gdrive:Q/f.txt", "gdrive:Q", new_name="g.txt")
    mv(conn, d, "gdrive:Q/g.txt", "gdrive:P", new_name="f.txt")
    assert ws.build_ops(conn, d)[0] == []


def test_moving_folders_that_hold_no_files_is_reported_not_silently_dropped(world):
    """Ops are per file, so a folder without files never travels. The page shows it in its new home, so the
    preview has to say that nothing will happen to it."""
    conn, cat, g, _ = world
    for path in ("Projects", "Projects/2019", "Archive"):
        cat.dir(g, path)
    d = new_draft(conn)
    mv(conn, d, "gdrive:Projects/2019", "gdrive:Archive")
    assert list(ls(conn, "gdrive:Archive", d)) == ["2019"]                    # on screen it moved
    out = ws.preview(conn, d)
    assert out["plan_id"] is None and out["stale_actions"] == []
    assert out["empty_dirs_skipped"] == [{"from": {"remote": "gdrive", "path": "Projects/2019"},
                                          "to": {"remote": "gdrive", "path": "Archive/2019"}}]
    assert ws.build_ops(conn, d)[0] == []


def test_empty_subfolders_of_a_moved_folder_are_listed_once_at_their_top(world):
    conn, cat, g, x = world
    put(cat, g, "School/notes.txt", size=3, md5="aa", group=1)
    put(cat, g, "School/deep/er/f.txt", size=3, md5="bb", group=2)             # folders that hold a file travel
    for path in ("School/empty", "School/empty/inner", "School/also"):
        cat.dir(g, path)
    cat.dir(x, "Archive")
    d = new_draft(conn)
    mv(conn, d, "gdrive:School", "dropbox:Archive")
    out = ws.preview(conn, d)
    assert out["plan_id"] is not None and out["summary"]["ops"] == {"move": 0, "copy": 2, "delete": 2}
    assert [(e["from"]["path"], e["to"]["remote"], e["to"]["path"]) for e in out["empty_dirs_skipped"]] == [
        ("School/also", "dropbox", "Archive/School/also"), ("School/empty", "dropbox", "Archive/School/empty")]
    assert out["summary"]["empty_dirs_skipped"] == 2
    assert out["ok"] is True                                                    # harmless, but nobody is left guessing


def test_a_draft_that_moves_files_only_reports_no_skipped_folders(world):
    conn, cat, g, _ = world
    put(cat, g, "A/x/f.txt", size=3, md5="aa", group=1)
    cat.dir(g, "B")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A", "gdrive:B")
    out = ws.preview(conn, d)
    assert out["empty_dirs_skipped"] == [] and out["summary"]["empty_dirs_skipped"] == 0


def test_paths_match_by_whole_segments_only(world):
    conn, cat, g, _ = world
    put(cat, g, "a/b/f.txt", group=1)
    put(cat, g, "a/bc/g.txt", group=2)
    put(cat, g, "a/b.txt", group=3)
    put(cat, g, "D/m/h.txt", group=4)
    put(cat, g, "D/m2/i.txt", group=5)
    put(cat, g, "dest/b2/z.txt", group=6)
    d = new_draft(conn)
    mv(conn, d, "gdrive:a/b", "gdrive:dest")                  # 'a/bc' and 'a/b.txt' only look like 'a/b'
    assert describe(conn, ws.build_ops(conn, d)[0]) == [("move", "gdrive:a/b/f.txt", "gdrive:dest/b/f.txt")]
    assert list(ls(conn, "gdrive:a", d)) == ["bc", "b.txt"]
    assert not ls(conn, "gdrive:a", d)["bc"]["moved_here"] and ls(conn, "gdrive:a/bc", d)["g.txt"]["origins"]
    mv(conn, d, "gdrive:D/m", "gdrive:dest")
    assert list(ls(conn, "gdrive:D", d)) == ["m2"] and not ls(conn, "gdrive:D/m2", d)["i.txt"]["moved_here"]
    assert list(ls(conn, "gdrive:dest", d)) == ["b", "b2", "m"]
    assert list(ls(conn, "gdrive:dest/b2", d)) == ["z.txt"]
    mv(conn, d, "gdrive:a/b.txt", "gdrive:dest/b2")
    # a folder may go into a sibling whose name merely starts with its own name
    mv(conn, d, "gdrive:dest/b", "gdrive:dest/b2")
    assert sorted(ls(conn, "gdrive:dest/b2", d)) == ["b", "b.txt", "z.txt"]
    refuse(conn, d, "INTO_ITSELF", "gdrive:dest/b2", "gdrive:dest/b2/b")
    ops, info = ws.build_ops(conn, d)
    assert sorted(describe(conn, ops)) == [
        ("move", "gdrive:D/m/h.txt", "gdrive:dest/m/h.txt"), ("move", "gdrive:a/b.txt", "gdrive:dest/b2/b.txt"),
        ("move", "gdrive:a/b/f.txt", "gdrive:dest/b2/b/f.txt")]
    assert info["conflicts"] == [] and sim(conn, ops).ok


# ---------------------------------------------------------------------- merges


def merge_world(conn, cat, g):
    put(cat, g, "School/Thesis/a.txt", size=3, md5="aa", group=1)
    put(cat, g, "School/Thesis/b.txt", size=4, md5="bb", group=2)
    put(cat, g, "Research/Thesis/a.txt", size=3, md5="aa", group=1)
    put(cat, g, "Research/Thesis/c.txt", size=5, md5="cc", group=3)
    refresh_stats(conn)


def test_folder_merge_with_identical_files_dedupes_with_a_witness(world):
    conn, cat, g, _ = world
    merge_world(conn, cat, g)
    d = new_draft(conn)
    r = mv(conn, d, "gdrive:Research/Thesis", "gdrive:School")
    assert codes(r) == ["MERGE"]

    school = ls(conn, "gdrive:School", d)
    assert list(school) == ["Thesis"]
    t = school["Thesis"]
    assert t["merged"] and t["moved_here"] and not t["conflict"] and not t["virtual"]
    assert t["origins"] == [{"remote": "gdrive", "path": "Research/Thesis"},
                            {"remote": "gdrive", "path": "School/Thesis"}]
    # The identical file that the merge deletes arrives nowhere: 3 files, not 4, and the survivor has no twin left.
    assert (t["files"], t["bytes"], t["dup_files"]) == (3, 12, 0)
    assert ls(conn, "gdrive:Research", d) == {}

    kids = ls(conn, "gdrive:School/Thesis", d)                         # children from both origins
    assert list(kids) == ["a.txt", "b.txt", "c.txt"]
    assert kids["a.txt"]["dedupe"] and not kids["a.txt"]["conflict"] and kids["a.txt"]["moved_here"]
    assert [o["path"] for o in kids["a.txt"]["origins"]] == ["Research/Thesis/a.txt", "School/Thesis/a.txt"]
    assert not kids["b.txt"]["moved_here"] and kids["c.txt"]["moved_here"]
    assert kids["c.txt"]["origins"] == [{"remote": "gdrive", "path": "Research/Thesis/c.txt"}]

    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "gdrive:Research/Thesis/c.txt", "gdrive:School/Thesis/c.txt"),
                                   ("delete", "gdrive:Research/Thesis/a.txt", "gdrive:School/Thesis/a.txt")]
    assert info["dedupes"] == 1 and info["conflicts"] == []
    assert [(c["kind"], c["files"]) for c in info["changes"]] == [("merge", 2)]
    assert info["empty_dirs_left"] == 2                                # Research/Thesis and Research are left empty
    out = ws.preview(conn, d)
    assert out["simulation"]["ok"] and out["summary"]["dedupes"] == 1
    assert out["summary"]["ops"] == {"move": 1, "copy": 0, "delete": 1}
    assert plans.get_plan(conn, out["plan_id"]).status == "simulated"


def test_empty_files_are_not_duplicates_in_the_tree_or_in_the_folder_counts(world):
    """Every empty file shares one hash. Search, the duplicate list and dir_stats leave them out, so a folder
    badge must not count them either (a file row would say "duplicate" while its folder says none)."""
    conn, cat, g, x = world
    put(cat, g, "proj1/__init__.py", size=0, md5="e", group=1)
    put(cat, x, "proj2/__init__.py", size=0, md5="e", group=1)
    put(cat, g, "proj1/real.bin", size=5, md5="r", group=2)
    put(cat, x, "proj2/real.bin", size=5, md5="r", group=2)
    dirstats.refresh(conn)
    first = ls(conn, "gdrive:proj1")
    assert (first["__init__.py"]["dup_files"], first["real.bin"]["dup_files"]) == (0, 1)
    assert ls(conn, "gdrive:")["proj1"]["dup_files"] == 1 == dirstats.get(conn, g, "proj1")["dup_files"]
    d = new_draft(conn)
    mv(conn, d, "gdrive:proj1", "dropbox:")
    assert ls(conn, "dropbox:", d)["proj1"]["dup_files"] == 1                  # same answer after the move


def test_a_merge_that_deletes_an_empty_file_does_not_take_a_duplicate_off_its_folder(world):
    """The survivor of a deleted twin stops being a duplicate -- unless the twin is an empty file, which never was."""
    conn, cat, g, x = world
    put(cat, x, "a/empty.txt", size=0, md5="e", group=1)
    put(cat, x, "a/r1.bin", size=5, md5="r", group=2)
    put(cat, x, "a/r2.bin", size=5, md5="r", group=2)
    put(cat, g, "a/empty.txt", size=0, md5="e", group=1)
    dirstats.refresh(conn)
    assert dirstats.get(conn, x, "a")["dup_files"] == 2
    d = new_draft(conn)
    mv(conn, d, "gdrive:a", "dropbox:")                      # merges into dropbox:a; its empty.txt twin is deleted
    assert ws.preview(conn, d)["summary"]["dedupes"] == 1
    merged = ls(conn, "dropbox:", d)["a"]
    assert (merged["files"], merged["dup_files"]) == (3, 2)


def test_the_after_view_agrees_with_the_simulator_when_a_merge_drops_an_identical_file(world):
    """A cross-cloud merge: the identical draft.docx is deleted, not copied, so the folder and the clouds must not
    claim it. The numbers the tree shows are the numbers the preview's per-cloud table shows."""
    conn, cat, g, x = world
    put(cat, x, "School/NTU/Thesis/draft.docx", size=40, md5="dd", group=7)
    put(cat, x, "School/NTU/Thesis/refs.bib", size=9, md5="rr", group=8)
    put(cat, x, "School/keep.txt", size=2, md5="kk", group=10)
    put(cat, g, "Research/Thesis/draft.docx", size=40, md5="dd", group=7)
    put(cat, g, "Research/Thesis/notes.md", size=5, md5="nn", group=9)
    refresh_stats(conn)
    d = new_draft(conn)
    assert codes(mv(conn, d, "dropbox:School/NTU/Thesis", "gdrive:Research")) == ["MERGE", "CROSS_REMOTE"]

    thesis = ls(conn, "gdrive:Research", d)["Thesis"]
    assert (thesis["files"], thesis["bytes"]) == (3, 54)             # draft.docx once, refs.bib, notes.md
    assert list(ls(conn, "gdrive:Research/Thesis", d)) == ["draft.docx", "notes.md", "refs.bib"]
    assert (ls(conn, "dropbox:School", d)["NTU"]["files"], ls(conn, "dropbox:School", d)["NTU"]["bytes"]) == (0, 0)

    out = ws.preview(conn, d)
    assert out["simulation"]["ok"] and out["summary"]["dedupes"] == 1
    after = {r["remote"]: (r["files_after"], r["bytes_after"]) for r in out["simulation"]["remotes"]}
    roots = {r["remote"]: (r["files"], r["bytes"]) for r in ws.roots(conn, draft_id=d)}
    assert roots == after == {"gdrive": (3, 54), "dropbox": (1, 2)}
    assert (thesis["dup_files"], ls(conn, "gdrive:", d)["Research"]["dup_files"]) == (0, 0)
    assert {r["remote"]: r["dup_files"] for r in ws.roots(conn, draft_id=d)} == {"gdrive": 0, "dropbox": 0}


def test_two_identical_files_dragged_to_one_spot_count_once(world):
    """Nothing sits there yet: two moved folders of the same name merge, and the identical files share one spot."""
    conn, cat, g, _ = world
    put(cat, g, "x/T/f.txt", size=7, md5="ff", group=1)
    put(cat, g, "y/T/f.txt", size=7, md5="ff", group=1)
    put(cat, g, "z/other.txt", size=1, md5="oo", group=2)
    refresh_stats(conn)
    d = new_draft(conn)
    mv(conn, d, "gdrive:x/T", "gdrive:z")
    assert codes(mv(conn, d, "gdrive:y/T", "gdrive:z")) == ["MERGE"]
    assert ls(conn, "gdrive:z/T", d)["f.txt"]["dedupe"]
    z = ls(conn, "gdrive:", d)["z"]
    assert (z["files"], z["bytes"]) == (2, 8)                        # f.txt once, other.txt
    assert ls(conn, "gdrive:", d)["x"]["files"] == 0 and ls(conn, "gdrive:", d)["y"]["files"] == 0
    assert ws.roots(conn, draft_id=d)[0]["files"] == 2
    ops, info = ws.build_ops(conn, d)
    assert info["dedupes"] == 1 and sim(conn, ops).ok


def test_folder_merge_with_differing_files_is_a_conflict(world):
    conn, cat, g, _ = world
    merge_world(conn, cat, g)
    conn.execute("UPDATE entries SET content_group = 9, md5 = 'zz' WHERE path = 'Research/Thesis/a.txt'")
    d = new_draft(conn)
    mv(conn, d, "gdrive:Research/Thesis", "gdrive:School")
    kids = ls(conn, "gdrive:School/Thesis", d)
    assert kids["a.txt"]["conflict"] and not kids["a.txt"]["dedupe"]
    ops, info = ws.build_ops(conn, d)
    # the colliding move is still emitted so that the simulator reports it
    assert ("move", "gdrive:Research/Thesis/a.txt", "gdrive:School/Thesis/a.txt") in describe(conn, ops)
    assert [(c["code"], c["from"]["path"], c["to"]["path"], c["with"]["path"]) for c in info["conflicts"]] == [
        ("DIFFERENT_CONTENT", "Research/Thesis/a.txt", "School/Thesis/a.txt", "School/Thesis/a.txt")]
    out = ws.preview(conn, d)
    assert not out["simulation"]["ok"]
    assert "DST_EXISTS" in [v["code"] for v in out["simulation"]["violations"]]
    assert out["summary"]["conflicts"] == 1
    # resolving it by renaming the file before the merge makes the draft clean
    d2 = new_draft(conn)
    mv(conn, d2, "gdrive:Research/Thesis/a.txt", "gdrive:Research/Thesis", new_name="a2.txt")
    mv(conn, d2, "gdrive:Research/Thesis", "gdrive:School")
    ops, info = ws.build_ops(conn, d2)
    assert info["conflicts"] == [] and sim(conn, ops).ok
    assert ("move", "gdrive:Research/Thesis/a.txt", "gdrive:School/Thesis/a2.txt") in describe(conn, ops)


def test_unhashed_files_are_never_assumed_identical(world):
    conn, cat, g, _ = world
    cat.file(g, "S/Thesis/a.txt", size=3)
    cat.file(g, "R/Thesis/a.txt", size=3)                    # same size and name, no hashes: not provable
    d = new_draft(conn)
    mv(conn, d, "gdrive:R/Thesis", "gdrive:S")
    ops, info = ws.build_ops(conn, d)
    assert info["dedupes"] == 0 and [c["code"] for c in info["conflicts"]] == ["DIFFERENT_CONTENT"]
    assert "NAME_EXISTS" not in [c["code"] for c in info["conflicts"]]
    assert not ws.preview(conn, d)["simulation"]["ok"]


def test_merge_on_a_case_insensitive_remote(world):
    conn, cat, g, x = world
    put(cat, x, "School/Thesis/Report.pdf", size=3, md5="aa", group=1)
    put(cat, x, "Research/thesis/report.pdf", size=3, md5="aa", group=1)       # same content, other case
    put(cat, x, "Research/thesis/notes.txt", size=4, md5="bb", group=2)
    d = new_draft(conn)
    r = mv(conn, d, "dropbox:Research/thesis", "dropbox:School")
    assert codes(r) == ["CASE_COLLISION"]                    # exact names differ, so no MERGE; dropbox merges them
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "dropbox:Research/thesis/notes.txt", "dropbox:School/thesis/notes.txt"),
                                   ("delete", "dropbox:Research/thesis/report.pdf", "dropbox:School/Thesis/Report.pdf")]
    assert info["dedupes"] == 1 and info["conflicts"] == []
    assert sim(conn, ops).ok

    # different content under names that differ only by case: a conflict, and the simulator says why
    conn.execute("UPDATE entries SET content_group = 7, md5 = 'zz' WHERE path = 'Research/thesis/report.pdf'")
    ops, info = ws.build_ops(conn, d)
    assert [c["code"] for c in info["conflicts"]] == ["DIFFERENT_CONTENT"]
    assert info["conflicts"][0]["with"]["path"] == "School/Thesis/Report.pdf"
    report = sim(conn, ops)
    assert not report.ok and "CASE_COLLISION" in vcodes(report)


def test_case_only_rename_warns_but_is_allowed_when_alone(world):
    conn, cat, g, x = world
    put(cat, x, "d/readme.md", group=1)
    put(cat, x, "d/other.md", group=2)
    d = new_draft(conn)
    r = mv(conn, d, "dropbox:d/readme.md", "dropbox:d", new_name="README.md")
    assert codes(r) == ["CASE_ONLY_RENAME"]                  # the only "clash" is the file itself: no CASE_COLLISION
    r = mv(conn, d, "dropbox:d/other.md", "dropbox:d", new_name="README.MD")
    assert codes(r) == ["CASE_COLLISION"]
    mk = mkdir(conn, d, "dropbox:d", "Readme.md")
    assert codes(mk) == ["CASE_COLLISION"]


def test_case_only_renames_on_a_case_insensitive_remote_are_conflicts_not_clean_drafts(world):
    """The simulator allows a rename by move, but the executor refuses a move onto the file itself (the cloud
    finds the source under the new spelling), and moving file by file cannot change a folder's case either. So
    the preview must say so before anyone approves, instead of calling the draft clean and failing at run time."""
    conn, cat, g, x = world
    put(cat, x, "d/readme.md", size=3, dropbox="aa", group=1)
    put(cat, x, "Thesis/a.txt", size=3, dropbox="bb", group=2)
    put(cat, x, "Thesis/sub/b.txt", size=3, dropbox="cc", group=3)
    d = new_draft(conn)
    # staging allows it but says so at once (the toast), long before anyone opens the preview
    assert codes(mv(conn, d, "dropbox:d/readme.md", "dropbox:d", new_name="README.md")) == ["CASE_ONLY_RENAME"]
    assert codes(mv(conn, d, "dropbox:Thesis", "dropbox:", new_name="thesis")) == ["CASE_ONLY_RENAME"]
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "dropbox:Thesis/a.txt", "dropbox:thesis/a.txt"),
                                   ("move", "dropbox:Thesis/sub/b.txt", "dropbox:thesis/sub/b.txt"),
                                   ("move", "dropbox:d/readme.md", "dropbox:d/README.md")]
    # one conflict for the folder that was renamed (not one per file in it), one for the lone file
    assert [(c["code"], c["from"]["path"], c["to"]["path"]) for c in info["conflicts"]] == [
        ("CASE_ONLY_RENAME", "Thesis", "thesis"), ("CASE_ONLY_RENAME", "d/readme.md", "d/README.md")]
    assert sim(conn, ops).ok                                 # the simulator alone cannot see it
    out = ws.preview(conn, d)
    assert out["ok"] is False and out["summary"]["conflicts"] == 2
    assert [c["code"] for c in out["conflicts"]] == ["CASE_ONLY_RENAME"] * 2
    assert "CYCLE" not in [c["code"] for c in out["conflicts"]]     # landing on its own spot is not a wait


def test_a_respelled_name_is_the_same_name_on_a_remote_that_ignores_case_and_unicode_form(world):
    conn, cat, g, x = world
    nfd, nfc = unicodedata.normalize("NFD", "café.txt"), unicodedata.normalize("NFC", "café.txt")
    assert nfd != nfc
    put(cat, x, f"d/{nfd}", size=3, dropbox="aa", group=1)
    put(cat, x, "d/Notes.txt", size=3, dropbox="bb", group=2)
    put(cat, x, "d/Plain.txt", size=3, dropbox="cc", group=3)
    cat.dir(x, "other")
    d = new_draft(conn)
    assert codes(mv(conn, d, f"dropbox:d/{nfd}", "dropbox:d", new_name=nfc)) == ["CASE_ONLY_RENAME"]
    assert [(c["code"], c["to"]["path"]) for c in ws.build_ops(conn, d)[1]["conflicts"]] == [
        ("CASE_ONLY_RENAME", f"d/{nfc}")]
    ws.undo(conn, d)
    # what is not a respelling in place is fine: a new name, the same name in another folder, another case elsewhere
    assert codes(mv(conn, d, "dropbox:d/Notes.txt", "dropbox:d", new_name="Notes2.txt")) == []
    assert codes(mv(conn, d, "dropbox:d/Plain.txt", "dropbox:other", new_name="PLAIN.txt")) == []
    out = ws.preview(conn, d)
    assert out["conflicts"] == [] and out["ok"] is True


def test_the_same_case_only_rename_is_fine_on_a_case_sensitive_remote(world):
    conn, cat, g, x = world
    put(cat, g, "Thesis/a.txt", size=3, md5="aa", group=1)
    d = new_draft(conn)
    assert codes(mv(conn, d, "gdrive:Thesis", "gdrive:", new_name="thesis")) == []       # no warning either
    out = ws.preview(conn, d)
    assert out["conflicts"] == [] and out["ok"] is True
    assert describe(conn, plans.get_plan(conn, out["plan_id"]).ops) == [
        ("move", "gdrive:Thesis/a.txt", "gdrive:thesis/a.txt")]


def test_a_local_disk_declared_case_insensitive_is_treated_like_dropbox(conn, catalog):
    """`case_insensitive = true` on a local remote reaches staging, the preview and the plan, not just the executor."""
    disk = catalog.remote("disk", "local", case_insensitive=True)
    put(catalog, disk, "Photos/a.jpg", size=3, md5="aa", group=1)
    d = new_draft(conn)
    assert codes(mv(conn, d, "disk:Photos", "disk:", new_name="photos")) == ["CASE_ONLY_RENAME"]
    out = ws.preview(conn, d)
    assert out["ok"] is False and [c["code"] for c in out["conflicts"]] == ["CASE_ONLY_RENAME"]


def test_files_landing_in_one_action_dedupe_against_each_other(world):
    conn, cat, g, _ = world
    put(cat, g, "P/Thesis/f.txt", size=2, md5="aa", group=1)
    put(cat, g, "Q/Thesis/f.txt", size=2, md5="aa", group=1)
    put(cat, g, "Q/Thesis/h.txt", size=2, md5="hh", group=5)
    put(cat, g, "P/Thesis/h.txt", size=2, md5="h2", group=6)
    cat.dir(g, "R")
    d = new_draft(conn)
    r = mv(conn, d, ["gdrive:P/Thesis", "gdrive:Q/Thesis"], "gdrive:R")
    assert codes(r) == ["MERGE"]
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "gdrive:P/Thesis/f.txt", "gdrive:R/Thesis/f.txt"),
                                   ("move", "gdrive:P/Thesis/h.txt", "gdrive:R/Thesis/h.txt"),
                                   ("move", "gdrive:Q/Thesis/h.txt", "gdrive:R/Thesis/h.txt"),   # conflict: kept
                                   ("delete", "gdrive:Q/Thesis/f.txt", "gdrive:R/Thesis/f.txt")]
    assert ops[-1].depends_on == 1                          # the witness is created by op #1
    assert [(c["code"], c["from"]["path"]) for c in info["conflicts"]] == [("DIFFERENT_CONTENT", "Q/Thesis/h.txt")]
    assert "DST_EXISTS" in vcodes(sim(conn, ops))


def test_file_where_a_folder_needs_to_go(world):
    conn, cat, g, _ = world
    put(cat, g, "N/x", size=2, md5="nn", group=1)          # a file called x
    put(cat, g, "A/x/y.txt", size=2, md5="yy", group=2)    # a folder called x
    d = new_draft(conn)
    mv(conn, d, "gdrive:A", "gdrive:", new_name="N")           # A (with a folder x) becomes N, merging with N
    kids = ls(conn, "gdrive:N", d)
    assert kids["x"]["conflict"]                           # file and folder under one name
    ops, info = ws.build_ops(conn, d)
    assert [c["code"] for c in info["conflicts"]] == ["FILE_IN_WAY"]
    assert info["conflicts"][0]["with"]["path"] == "N/x"
    assert not sim(conn, ops).ok


def test_a_folder_that_moved_away_still_exists_for_the_file_that_takes_its_name(world):
    conn, cat, g, _ = world
    put(cat, g, "p/a/f.txt", group=1)
    put(cat, g, "r/g.txt", group=2)
    cat.dir(g, "q")
    d = new_draft(conn)
    mv(conn, d, "gdrive:p/a", "gdrive:q", new_name="a2")
    mv(conn, d, "gdrive:r/g.txt", "gdrive:p", new_name="a")      # the after-view says p/a is free
    ops, info = ws.build_ops(conn, d)
    assert [c["code"] for c in info["conflicts"]] == ["DIR_IN_WAY"]   # ...but the old folder is still there
    assert "DST_EXISTS" in vcodes(sim(conn, ops))


# ---------------------------------------------------------- staging validation


def test_staging_errors(world):
    conn, cat, g, x = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "A/sub/deep.txt", group=2)
    put(cat, g, "B/f.txt", group=3)
    put(cat, g, "B/A", group=4)                     # a *file* called A inside B
    put(cat, g, "B/dir/z.txt", group=5)
    put(cat, g, "C/dir", group=6)                   # a file called dir inside C
    d = new_draft(conn)

    refuse(conn, d, "SRC_NOT_FOUND", "gdrive:nothing", "gdrive:B")
    refuse(conn, d, "DST_NOT_DIR", "gdrive:A/f.txt", "gdrive:nothing")
    refuse(conn, d, "DST_NOT_DIR", "gdrive:A/f.txt", "gdrive:B/f.txt")         # a file is not a destination
    refuse(conn, d, "INTO_ITSELF", "gdrive:A", "gdrive:A")
    refuse(conn, d, "INTO_ITSELF", "gdrive:A", "gdrive:A/sub")
    refuse(conn, d, "INTO_ITSELF", ["gdrive:B", "gdrive:A"], "gdrive:A/sub")
    refuse(conn, d, "NAME_EXISTS", "gdrive:A/f.txt", "gdrive:B")                 # file onto file
    refuse(conn, d, "NAME_EXISTS", "gdrive:A", "gdrive:B")                       # folder onto a file of that name
    refuse(conn, d, "NAME_EXISTS", "gdrive:A/f.txt", "gdrive:B", new_name="dir")  # file onto a folder of that name
    refuse(conn, d, "NAME_EXISTS", "gdrive:B/dir", "gdrive:C")                   # folder onto a file
    refuse(conn, d, "NOOP", "gdrive:A/f.txt", "gdrive:A")
    refuse(conn, d, "NOOP", ["gdrive:A/f.txt", "gdrive:A/sub"], "gdrive:A")
    for bad in ("", "a/b", ".", "..", "x\x00y"):
        refuse(conn, d, "BAD_NAME", "gdrive:A/f.txt", "gdrive:B", new_name=bad)
    refuse(conn, d, "BAD_NAME", ["gdrive:A/f.txt", "gdrive:A/sub"], "gdrive:B", new_name="one")
    with pytest.raises(WorkspaceError) as e:
        mkdir(conn, d, "gdrive:A", "a/b")
    assert e.value.code == "BAD_NAME"
    with pytest.raises(WorkspaceError) as e:
        mkdir(conn, d, "gdrive:A", "sub")
    assert e.value.code == "NOOP"
    with pytest.raises(WorkspaceError) as e:
        mkdir(conn, d, "gdrive:A", "f.txt")
    assert e.value.code == "NAME_EXISTS"
    with pytest.raises(WorkspaceError) as e:
        mkdir(conn, d, "gdrive:A/f.txt", "inside-a-file")
    assert e.value.code == "DST_NOT_DIR"
    assert ws.get_draft(conn, d)["actions"] == []

    # rename resolves the clash; the file keeps its content under the new name
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B", new_name="f2.txt")
    assert describe(conn, ws.build_ops(conn, d)[0]) == [("move", "gdrive:A/f.txt", "gdrive:B/f2.txt")]
    # a leading or trailing slash is accepted
    mv(conn, d, "gdrive:A/sub/", "gdrive:/B/")
    assert "sub" in ls(conn, "gdrive:B", d)


def test_staging_is_checked_against_the_after_view_not_the_catalog(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "B/f.txt", group=2)
    cat.dir(g, "C")
    d = new_draft(conn)
    refuse(conn, d, "NAME_EXISTS", "gdrive:A/f.txt", "gdrive:B")
    mv(conn, d, "gdrive:B/f.txt", "gdrive:C")                  # B/f.txt leaves, so the name is free
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    refuse(conn, d, "NAME_EXISTS", "gdrive:C/f.txt", "gdrive:B")
    ops, info = ws.build_ops(conn, d)
    assert info["conflicts"] == []
    assert describe(conn, ops) == [("move", "gdrive:B/f.txt", "gdrive:C/f.txt"),      # B/f.txt must leave first
                                   ("move", "gdrive:A/f.txt", "gdrive:B/f.txt")]
    assert sim(conn, ops).ok


def test_nested_and_repeated_items_are_dropped(world):
    conn, cat, g, _ = world
    put(cat, g, "A/sub/f.txt", group=1)
    put(cat, g, "A/g.txt", group=2)
    cat.dir(g, "B")
    d = new_draft(conn)
    r = mv(conn, d, ["gdrive:A/sub/f.txt", "gdrive:A", "gdrive:A", "gdrive:A/g.txt"], "gdrive:B")
    assert r["action"]["items"] == [{"remote": "gdrive", "path": "A"}]
    assert codes(r) == []
    assert sorted(describe(conn, ws.build_ops(conn, d)[0])) == [("move", "gdrive:A/g.txt", "gdrive:B/A/g.txt"),
                                                                ("move", "gdrive:A/sub/f.txt", "gdrive:B/A/sub/f.txt")]
    d2 = new_draft(conn)
    mv(conn, d2, ["gdrive:A/sub/f.txt", "gdrive:A"], "gdrive:B", new_name="renamed")      # one item after pruning
    assert "renamed" in ls(conn, "gdrive:B", d2)


def test_warnings(world):
    conn, cat, g, x = world
    ro = cat.remote("archive", "other", writable=False)
    put(cat, g, "a/f.txt", group=1)
    put(cat, ro, "r/old.txt", group=2)
    cat.dir(x, "box")
    d = new_draft(conn)
    assert codes(mv(conn, d, "gdrive:a/f.txt", "dropbox:box")) == ["CROSS_REMOTE"]
    assert codes(mv(conn, d, "archive:r/old.txt", "archive:r", new_name="older.txt")) == ["READ_ONLY"]
    r = mv(conn, d, "dropbox:box/f.txt", "archive:")
    assert sorted(codes(r)) == ["CROSS_REMOTE", "READ_ONLY"]
    assert codes(mkdir(conn, d, "archive:", "new")) == ["READ_ONLY"]
    ops, _ = ws.build_ops(conn, d)
    assert "READ_ONLY_REMOTE" in vcodes(sim(conn, ops))
    assert not ws.preview(conn, d)["simulation"]["ok"]


def test_a_multi_item_drag_lists_the_destination_once_not_once_per_item(world, monkeypatch):
    """Listing a folder walks every sub-move, so doing it per dragged item made staging cubic on Dropbox/OneDrive
    (400 files into a folder of 400: ~16 s under the server lock, minutes for 1000) while Drive took milliseconds."""
    conn, cat, g, x = world
    n = 120
    for i in range(n):
        put(cat, x, f"src/new{i}.txt", size=1)
        put(cat, x, f"dst/old{i}.txt", size=1)
    listed = []
    original = ws._Space.children
    monkeypatch.setattr(ws._Space, "children", lambda self, r, d: listed.append((r, d)) or original(self, r, d))
    d = new_draft(conn)
    r = mv(conn, d, [f"dropbox:src/new{i}.txt" for i in range(n)], "dropbox:dst")
    assert codes(r) == [] and len(listed) == 1
    assert len(ls(conn, "dropbox:dst", d)) == 2 * n


def test_staging_hundreds_of_items_into_a_case_insensitive_folder_stays_fast(world):
    conn, cat, g, x = world
    n = 300
    for i in range(n):
        put(cat, x, f"src/new{i}.txt", size=1)
        put(cat, x, f"dst/old{i}.txt", size=1)
    d = new_draft(conn)
    started = time.perf_counter()
    mv(conn, d, [f"dropbox:src/new{i}.txt" for i in range(n)], "dropbox:dst")
    assert time.perf_counter() - started < 3.0               # ~0.05 s now; it took 7 s while it was cubic


def test_case_clashes_inside_one_multi_item_drag_are_still_found(world):
    conn, cat, g, x = world
    put(cat, x, "a/Report.pdf", group=1)
    put(cat, x, "b/report.PDF", group=2)
    put(cat, x, "c/Other.txt", group=3)
    put(cat, x, "dst/REPORT.pdf", group=4)
    put(cat, x, "e/readme.md", group=5)
    cat.dir(x, "empty")
    d = new_draft(conn)
    # against what is already there ...
    assert codes(mv(conn, d, "dropbox:a/Report.pdf", "dropbox:dst")) == ["CASE_COLLISION"]
    ws.undo(conn, d)
    # ... and against an item of the same drag that lands first
    r = mv(conn, d, ["dropbox:b/report.PDF", "dropbox:c/Other.txt", "dropbox:e/readme.md"], "dropbox:empty")
    assert codes(r) == []
    ws.undo(conn, d)
    put(cat, x, "f/README.MD", group=6)
    r = mv(conn, d, ["dropbox:e/readme.md", "dropbox:f/README.MD"], "dropbox:empty")
    assert codes(r) == ["CASE_COLLISION"]                    # the second one differs from the first only by case
    # a rename inside the folder is not a clash with its own old spelling
    ws.undo(conn, d)
    assert codes(mv(conn, d, "dropbox:dst/REPORT.pdf", "dropbox:dst", new_name="Report.pdf")) == ["CASE_ONLY_RENAME"]


# ------------------------------------------------------------------ quarantine

Q = ".drive-merge-quarantine"


def test_nothing_is_staged_out_of_into_or_around_the_quarantine_folder(world):
    conn, cat, g, x = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "A/g.txt", group=2)
    put(cat, g, f"{Q}/7/A/old.txt", group=3)
    put(cat, x, f"{Q}/7/B/old2.txt", group=4)
    cat.dir(g, "B")
    d = new_draft(conn)

    assert refuse(conn, d, "QUARANTINE", f"gdrive:{Q}/7/A/old.txt", "gdrive:B").params == {
        "item": L(f"gdrive:{Q}/7/A/old.txt")}                                           # out of it: a file
    refuse(conn, d, "QUARANTINE", f"gdrive:{Q}/7", "gdrive:B")                    # a folder inside
    refuse(conn, d, "QUARANTINE", f"gdrive:{Q}", "gdrive:B")                      # the folder itself
    refuse(conn, d, "QUARANTINE", f"gdrive:{Q}/7/A/old.txt", f"gdrive:{Q}/7/A", new_name="renamed.txt")
    refuse(conn, d, "QUARANTINE", ["gdrive:A/f.txt", f"gdrive:{Q}/7/A/old.txt"], "gdrive:B")   # one bad item spoils all
    refuse(conn, d, "QUARANTINE", "gdrive:A/f.txt", f"gdrive:{Q}")                # into it
    refuse(conn, d, "QUARANTINE", "gdrive:A", f"gdrive:{Q}/7/A")
    refuse(conn, d, "QUARANTINE", "gdrive:A/g.txt", "gdrive:", new_name=Q)        # renamed to its name: it would land there
    refuse(conn, d, "QUARANTINE", "gdrive:A/f.txt", f"dropbox:{Q}/7")             # another cloud's quarantine as well
    refuse(conn, d, "QUARANTINE", f"dropbox:{Q}/7/B/old2.txt", "gdrive:B")
    with pytest.raises(WorkspaceError) as e:
        mkdir(conn, d, "gdrive:", Q)
    assert e.value.code == "QUARANTINE"
    with pytest.raises(WorkspaceError) as e:
        mkdir(conn, d, f"gdrive:{Q}/7", "mine")
    assert e.value.code == "QUARANTINE"
    assert "restore" in str(e.value) and "command line" in str(e.value)
    assert ws.get_draft(conn, d)["actions"] == []

    # everything else still works, including names that merely start like the quarantine folder
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    mv(conn, d, "gdrive:A/g.txt", "gdrive:", new_name=Q + "-not")
    mkdir(conn, d, "gdrive:", Q[1:])
    assert [o.dst_path for o in ws.build_ops(conn, d)[0]] == ["B/f.txt", Q + "-not"]


def test_a_case_insensitive_remote_knows_the_quarantine_folder_by_any_casing(world):
    conn, cat, g, x = world
    put(cat, x, "A/f.txt", group=1)
    d = new_draft(conn)
    refuse(conn, d, "QUARANTINE", "dropbox:A/f.txt", "dropbox:" + Q.upper())
    refuse(conn, d, "QUARANTINE", "dropbox:A/f.txt", "dropbox:", new_name=Q.title())
    put(cat, g, "A/f.txt", group=2)                         # gdrive is case sensitive: another folder
    mv(conn, d, "gdrive:A/f.txt", "gdrive:", new_name=Q.upper())


def test_the_quarantine_folder_follows_the_configured_name_and_a_nested_one_pins_its_parents(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "keep/trash/3/x.txt", group=2)
    put(cat, g, f"{Q}/9/y.txt", group=3)
    cat.dir(g, "B")
    db.set_quarantine_dir(conn, "keep/trash")
    d = new_draft(conn)
    refuse(conn, d, "QUARANTINE", "gdrive:keep/trash/3/x.txt", "gdrive:B")
    refuse(conn, d, "QUARANTINE", "gdrive:A/f.txt", "gdrive:keep/trash")
    refuse(conn, d, "QUARANTINE", "gdrive:keep", "gdrive:B")                        # it would carry the quarantine along
    mv(conn, d, f"gdrive:{Q}/9/y.txt", "gdrive:B")                                  # not the quarantine folder any more
    mv(conn, d, "gdrive:A/f.txt", "gdrive:keep")
    db.set_quarantine_dir(conn, "")                                                  # no quarantine at all
    mv(conn, d, "gdrive:keep/trash/3/x.txt", "gdrive:B")


def test_listings_flag_the_quarantine_folder_and_everything_in_it(world):
    conn, cat, g, x = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, f"{Q}/7/A/old.txt", group=2)
    put(cat, x, "A/f.txt", group=3)
    refresh_stats(conn)
    d = new_draft(conn)
    for draft in (None, d):
        top = ls(conn, "gdrive:", draft)
        assert (top[Q]["quarantine"], top[Q]["quarantine_root"]) == (True, True)
        assert (top["A"]["quarantine"], top["A"]["quarantine_root"]) == (False, False)
        inside = ls(conn, f"gdrive:{Q}", draft)["7"]
        assert (inside["quarantine"], inside["quarantine_root"]) == (True, False)
        assert ls(conn, f"gdrive:{Q}/7/A", draft)["old.txt"]["quarantine"] is True
        assert ls(conn, "dropbox:", draft)["A"]["quarantine"] is False
    assert ls(conn, "gdrive:", d, view="before")[Q]["quarantine_root"] is True
    assert all(not n["quarantine"] and not n["quarantine_root"] for n in ws.roots(conn, draft_id=d))
    mv(conn, d, "gdrive:A", "dropbox:")
    assert not ls(conn, "dropbox:", d)["A"]["quarantine"]


# ----------------------------------------------------------------------- mkdir


def test_mkdir_move_in_then_move_the_new_folder_away(world):
    conn, cat, g, _ = world
    put(cat, g, "Old/a.txt", size=3, group=1)
    put(cat, g, "Old/b.txt", size=4, group=2)
    put(cat, g, "Dest/z.txt", size=5, group=3)
    refresh_stats(conn)
    d = new_draft(conn)
    mkdir(conn, d, "gdrive:", "Thesis")
    t = ls(conn, "gdrive:", d)["Thesis"]
    assert t["virtual"] and t["is_dir"] and t["entry_id"] is None and t["origins"] == []
    assert (t["files"], t["dirs"]) == (0, 0)
    mv(conn, d, ["gdrive:Old/a.txt", "gdrive:Old/b.txt"], "gdrive:Thesis")
    t = ls(conn, "gdrive:", d)["Thesis"]
    assert (t["files"], t["bytes"]) == (2, 7)
    mv(conn, d, "gdrive:Thesis", "gdrive:Dest")
    assert "Thesis" not in ls(conn, "gdrive:", d)
    inner = ls(conn, "gdrive:Dest/Thesis", d)
    assert list(inner) == ["a.txt", "b.txt"]
    dest = ls(conn, "gdrive:", d)["Dest"]
    assert (dest["files"], dest["dirs"], dest["bytes"]) == (3, 1, 12)
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "gdrive:Old/a.txt", "gdrive:Dest/Thesis/a.txt"),
                                   ("move", "gdrive:Old/b.txt", "gdrive:Dest/Thesis/b.txt")]
    assert sim(conn, ops).ok
    # an empty new folder produces no ops at all
    d2 = new_draft(conn)
    mkdir(conn, d2, "gdrive:Dest", "Empty")
    assert ws.build_ops(conn, d2)[0] == []
    assert ls(conn, "gdrive:Dest", d2)["Empty"]["virtual"]
    assert ws.roots(conn, draft_id=d2)[0]["dirs"] == ws.roots(conn)[0]["dirs"] + 1


def test_mkdir_on_a_name_that_was_moved_away_is_a_new_folder(world):
    conn, cat, g, _ = world
    put(cat, g, "a/f.txt", size=2, group=1)
    cat.dir(g, "elsewhere")
    d = new_draft(conn)
    mv(conn, d, "gdrive:a", "gdrive:elsewhere")
    mkdir(conn, d, "gdrive:", "a")                           # a brand new 'a', unrelated to the one that left
    assert ls(conn, "gdrive:a", d) == {}
    assert ls(conn, "gdrive:", d)["a"]["virtual"]
    assert list(ls(conn, "gdrive:elsewhere/a", d)) == ["f.txt"]
    mv(conn, d, "gdrive:elsewhere/a/f.txt", "gdrive:a")
    ops, _ = ws.build_ops(conn, d)
    assert ops == []                                         # f.txt went away and came back


def test_mkdir_follows_the_parent_that_moves_afterwards(world):
    conn, cat, g, _ = world
    put(cat, g, "p/f.txt", group=1)
    put(cat, g, "src/g.txt", group=2)
    cat.dir(g, "dest")
    d = new_draft(conn)
    mkdir(conn, d, "gdrive:p", "new")
    mv(conn, d, "gdrive:p", "gdrive:dest")
    assert ls(conn, "gdrive:dest/p", d)["new"]["virtual"]
    mv(conn, d, "gdrive:src/g.txt", "gdrive:dest/p/new")
    assert describe(conn, ws.build_ops(conn, d)[0]) == [("move", "gdrive:p/f.txt", "gdrive:dest/p/f.txt"),
                                                        ("move", "gdrive:src/g.txt", "gdrive:dest/p/new/g.txt")]


def test_moving_a_real_folder_onto_a_new_folder_merges(world):
    conn, cat, g, _ = world
    put(cat, g, "x/f.txt", group=1)
    d = new_draft(conn)
    mkdir(conn, d, "gdrive:", "y")
    r = mv(conn, d, "gdrive:x", "gdrive:", new_name="y")
    assert codes(r) == ["MERGE"]
    y = ls(conn, "gdrive:", d)["y"]
    assert y["merged"] and not y["virtual"]
    assert describe(conn, ws.build_ops(conn, d)[0]) == [("move", "gdrive:x/f.txt", "gdrive:y/f.txt")]


# ---------------------------------------------------------------- cross remote


def test_cross_remote_move_is_copy_then_delete_with_the_copy_as_witness(world):
    conn, cat, g, x = world
    put(cat, g, "Photos/b.jpg", size=20, md5="bb", group=2)
    put(cat, g, "Photos/a.jpg", size=10, md5="aa", group=1)
    put(cat, g, "Photos/sub/c.jpg", size=30, md5="cc", group=3)
    put(cat, x, "Pics/old.jpg", size=1, dropbox="dd", group=4)
    refresh_stats(conn)
    d = new_draft(conn)
    assert codes(mv(conn, d, "gdrive:Photos", "dropbox:Pics")) == ["CROSS_REMOTE"]
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [
        ("copy", "gdrive:Photos/a.jpg", "dropbox:Pics/Photos/a.jpg"),
        ("copy", "gdrive:Photos/b.jpg", "dropbox:Pics/Photos/b.jpg"),
        ("copy", "gdrive:Photos/sub/c.jpg", "dropbox:Pics/Photos/sub/c.jpg"),
        ("delete", "gdrive:Photos/a.jpg", "dropbox:Pics/Photos/a.jpg"),
        ("delete", "gdrive:Photos/b.jpg", "dropbox:Pics/Photos/b.jpg"),
        ("delete", "gdrive:Photos/sub/c.jpg", "dropbox:Pics/Photos/sub/c.jpg")]
    assert [o.depends_on for o in ops] == [None, None, None, 1, 2, 3]
    assert [(c["kind"], c["files"], c["bytes"]) for c in info["changes"]] == [("copy+delete", 3, 60)]
    assert ws.get_draft(conn, d)["summary"] == {"moved_files": 3, "moved_bytes": 60, "cross_remote_bytes": 60,
                                                "remotes_touched": ["dropbox", "gdrive"], "stale_actions": 0}
    out = ws.preview(conn, d)
    assert out["simulation"]["ok"] and out["simulation"]["bytes_cross_remote"] == 60
    assert out["simulation"]["op_counts"] == {"copy": 3, "move": 0, "delete": 3}
    roots = {r["remote"]: r for r in ws.roots(conn, draft_id=d)}
    assert (roots["gdrive"]["files"], roots["gdrive"]["bytes"]) == (0, 0)
    assert (roots["dropbox"]["files"], roots["dropbox"]["bytes"], roots["dropbox"]["dirs"]) == (4, 61, 3)
    assert ls(conn, "dropbox:Pics/Photos", d)["sub"]["files"] == 1


def test_cross_remote_conflict_keeps_the_copy_but_not_the_delete(world):
    conn, cat, g, x = world
    put(cat, g, "Photos/a.jpg", size=10, md5="aa", group=1)
    put(cat, x, "Pics/Photos/a.jpg", size=11, dropbox="zz", group=9)
    d = new_draft(conn)
    mv(conn, d, "gdrive:Photos", "dropbox:Pics")
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("copy", "gdrive:Photos/a.jpg", "dropbox:Pics/Photos/a.jpg")]
    assert [c["code"] for c in info["conflicts"]] == ["DIFFERENT_CONTENT"]
    assert "DST_EXISTS" in vcodes(sim(conn, ops))


def test_cross_remote_merge_with_identical_content_just_removes_the_source(world):
    conn, cat, g, x = world
    put(cat, g, "Photos/a.jpg", size=10, md5="aa", group=1)
    put(cat, x, "Pics/Photos/a.jpg", size=10, dropbox="aa-db", group=1)
    d = new_draft(conn)
    mv(conn, d, "gdrive:Photos", "dropbox:Pics")
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("delete", "gdrive:Photos/a.jpg", "dropbox:Pics/Photos/a.jpg")]
    assert info["dedupes"] == 1 and [c["kind"] for c in info["changes"]] == ["merge"]
    assert sim(conn, ops).ok


def test_a_merge_that_deletes_instead_of_copying_is_not_a_cross_cloud_transfer(world):
    """The approval card says how much goes through this machine: files the merge deletes (identical content is
    already there) are not copied, so they must not count, in the preview and in the staged list alike."""
    conn, cat, g, x = world
    big = 5 * 1024 ** 3
    put(cat, x, "Thesis/dup.bin", size=big, dropbox="d1", group=1)         # identical copy already on Drive
    put(cat, x, "Thesis/new.bin", size=10, dropbox="d2", group=2)
    put(cat, g, "Thesis/dup.bin", size=big, md5="m1", group=1)
    refresh_stats(conn)
    d = new_draft(conn)
    mv(conn, d, "dropbox:Thesis", "gdrive:")
    out = ws.preview(conn, d)
    assert out["summary"]["dedupes"] == 1 and out["simulation"]["op_counts"] == {"copy": 1, "move": 0, "delete": 2}
    assert out["simulation"]["bytes_cross_remote"] == 10
    assert out["summary"]["cross_remote_bytes"] == out["simulation"]["bytes_cross_remote"]
    assert ws.get_draft(conn, d)["summary"]["cross_remote_bytes"] == 10
    # both files still leave Dropbox, so the other totals are unchanged
    assert out["summary"]["moved_files"] == 2 and out["summary"]["moved_bytes"] == big + 10

    # only identical files: nothing is transferred at all
    ws.remove_action(conn, d, ws.get_draft(conn, d)["actions"][0]["id"])
    conn.execute("UPDATE entries SET present = 0 WHERE path = 'Thesis/new.bin'")
    mv(conn, d, "dropbox:Thesis", "gdrive:")
    out = ws.preview(conn, d)
    assert out["simulation"]["bytes_cross_remote"] == 0 and out["summary"]["cross_remote_bytes"] == 0
    assert ws.get_draft(conn, d)["summary"]["cross_remote_bytes"] == 0


def test_swapping_across_remotes_is_reported_not_attempted_blindly(world):
    conn, cat, g, x = world
    put(cat, g, "a/f.txt", size=10, md5="aa", group=1)
    put(cat, x, "b/g.txt", size=12, dropbox="bb", group=2)
    d = new_draft(conn)
    mv(conn, d, "gdrive:a/f.txt", "dropbox:b")
    mv(conn, d, "dropbox:b/g.txt", "gdrive:a", new_name="f.txt")
    ops, info = ws.build_ops(conn, d)
    # g.txt would land on f.txt's spot, but f.txt is only quarantined in the delete phase
    assert [c["code"] for c in info["conflicts"]] == ["OCCUPIED"]
    assert not sim(conn, ops).ok


def test_an_identical_swap_passes_the_simulator_but_the_draft_is_not_ok(world):
    conn, cat, g, x = world
    put(cat, g, "a/f.txt", size=10, md5="aa", dropbox="aa-db", group=1)
    put(cat, x, "b/f.txt", size=10, md5="aa", dropbox="aa-db", group=1)      # the very same content
    d = new_draft(conn)
    refuse(conn, d, "NAME_EXISTS", "gdrive:a/f.txt", "dropbox:b")
    mv(conn, d, "dropbox:b/f.txt", "dropbox:b", new_name="tmp.txt")          # a swap needs a temporary name
    mv(conn, d, "gdrive:a/f.txt", "dropbox:b")
    mv(conn, d, "dropbox:b/tmp.txt", "gdrive:a", new_name="f.txt")
    out = ws.preview(conn, d)
    # copying onto identical content looks like a harmless no-op to the simulator ...
    assert out["simulation"]["ok"]
    # ... but the plan would not come out as drawn, so the draft must not count as clean
    assert [c["code"] for c in out["conflicts"]] == ["OCCUPIED", "OCCUPIED"] and out["ok"] is False


def test_mixed_remotes_keep_all_moves_and_copies_before_deletes(world):
    conn, cat, g, x = world
    put(cat, g, "s/a.txt", size=1, md5="aa", group=1)
    put(cat, g, "s/b.txt", size=2, md5="bb", group=2)
    put(cat, x, "t/c.txt", size=3, dropbox="cc", group=3)
    cat.dir(g, "dest")
    cat.dir(x, "dest")
    d = new_draft(conn)
    mv(conn, d, "gdrive:s/a.txt", "dropbox:dest")
    mv(conn, d, "gdrive:s/b.txt", "gdrive:dest")
    mv(conn, d, "dropbox:t/c.txt", "gdrive:dest")
    ops, _ = ws.build_ops(conn, d)
    kinds = [o.op for o in ops]
    assert kinds == sorted(kinds, key=lambda k: k == "delete")
    assert sorted(kinds) == ["copy", "copy", "delete", "delete", "move"]
    for o in ops:
        if o.op == "delete":
            copy = ops[o.depends_on - 1]
            assert copy.op == "copy" and (copy.dst_remote_id, copy.dst_path) == (o.witness_remote_id, o.witness_path)
            assert (copy.src_remote_id, copy.src_path) == (o.src_remote_id, o.src_path)
    assert sim(conn, ops).ok


# ------------------------------------------------------------------- ordering


def test_a_chain_of_renames_is_ordered_so_each_spot_is_free(world):
    conn, cat, g, _ = world
    put(cat, g, "d/a", size=1, md5="aa", group=1)
    put(cat, g, "d/b", size=2, md5="bb", group=2)
    put(cat, g, "d/c", size=3, md5="cc", group=3)
    d = new_draft(conn)
    mv(conn, d, "gdrive:d/c", "gdrive:d", new_name="e")
    mv(conn, d, "gdrive:d/b", "gdrive:d", new_name="c")
    mv(conn, d, "gdrive:d/a", "gdrive:d", new_name="b")
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "gdrive:d/c", "gdrive:d/e"), ("move", "gdrive:d/b", "gdrive:d/c"),
                                   ("move", "gdrive:d/a", "gdrive:d/b")]
    assert info["conflicts"] == [] and sim(conn, ops).ok


def test_a_folder_may_take_the_name_of_a_file_that_leaves(world):
    conn, cat, g, _ = world
    put(cat, g, "p/a", size=1, md5="aa", group=1)               # a file called 'a'
    put(cat, g, "q/f.txt", size=2, md5="bb", group=2)
    cat.dir(g, "away")
    d = new_draft(conn)
    mv(conn, d, "gdrive:p/a", "gdrive:away")
    mv(conn, d, "gdrive:q", "gdrive:p", new_name="a")
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "gdrive:p/a", "gdrive:away/a"),
                                   ("move", "gdrive:q/f.txt", "gdrive:p/a/f.txt")]
    assert info["conflicts"] == [] and sim(conn, ops).ok


def test_a_swap_has_no_valid_order_and_is_flagged(world):
    conn, cat, g, _ = world
    put(cat, g, "d/a", size=1, md5="aa", group=1)
    put(cat, g, "d/b", size=2, md5="bb", group=2)
    d = new_draft(conn)
    mv(conn, d, "gdrive:d/a", "gdrive:d", new_name="tmp")
    mv(conn, d, "gdrive:d/b", "gdrive:d", new_name="a")
    mv(conn, d, "gdrive:d/tmp", "gdrive:d", new_name="b")
    ops, info = ws.build_ops(conn, d)
    assert len(ops) == 2 and sorted(c["code"] for c in info["conflicts"]) == ["CYCLE", "CYCLE"]
    assert not ws.preview(conn, d)["simulation"]["ok"]


def test_a_file_that_would_have_to_move_into_itself_is_a_cycle_with_an_explanation(world):
    """c (holding x.txt) is dragged into dropbox's c under the name x.txt and that c back to the root: the file
    c/x.txt has to end up at c/x.txt/x.txt, which can only happen after it has left, i.e. it waits on itself."""
    conn, cat, g, x = world
    put(cat, g, "c/x.txt", size=3, md5="aa", group=1)
    cat.dir(x, "c")
    d = new_draft(conn)
    mv(conn, d, "gdrive:c", "dropbox:c", new_name="x.txt")
    mv(conn, d, "dropbox:c", "gdrive:")
    ops, info = ws.build_ops(conn, d)
    assert describe(conn, ops) == [("move", "gdrive:c/x.txt", "gdrive:c/x.txt/x.txt")]
    assert [(c["code"], c["from"]["path"], c["to"]["path"]) for c in info["conflicts"]] == [
        ("CYCLE", "c/x.txt", "c/x.txt/x.txt")]
    assert not sim(conn, ops).ok                             # the simulator stops it as well; now the draft says why
    out = ws.preview(conn, d)
    assert out["ok"] is False and [c["code"] for c in out["conflicts"]] == ["CYCLE"]


# --------------------------------------------------------------- undo and redo


def test_undo_redo_remove_cascade(world):
    conn, cat, g, _ = world
    put(cat, g, "X/f.txt", group=1)
    put(cat, g, "Y/g.txt", group=2)
    put(cat, g, "Z/h.txt", group=3)
    cat.dir(g, "C")
    d = new_draft(conn)
    a1 = mkdir(conn, d, "gdrive:", "N")["action"]["id"]
    a2 = mv(conn, d, "gdrive:X", "gdrive:N")["action"]["id"]
    a3 = mv(conn, d, "gdrive:N", "gdrive:C")["action"]["id"]
    a4 = mv(conn, d, "gdrive:Z", "gdrive:C")["action"]["id"]
    st = ws.get_draft(conn, d)
    assert st["version"] == 4 and st["undo_available"] and not st["redo_available"]

    out = ws.remove_action(conn, d, a1)                      # N vanishes, so what moved into it and out of it must go
    assert out["removed"] == [a1, a2, a3] and out["draft_version"] == 5
    st = ws.get_draft(conn, d)
    assert [(a["id"], a["state"]) for a in st["actions"]] == [(a1, "undone"), (a2, "undone"), (a3, "undone"),
                                                              (a4, "active")]
    assert list(ls(conn, "gdrive:C", d)) == ["Z"] and "X" in ls(conn, "gdrive:", d)
    assert st["redo_available"] and st["version"] == 5

    st = ws.redo(conn, d)                                     # lowest undone first: the mkdir
    assert [a["state"] for a in st["actions"]] == ["active", "undone", "undone", "active"]
    assert "N" in ls(conn, "gdrive:", d)
    ws.redo(conn, d)
    st = ws.redo(conn, d)
    assert all(a["state"] == "active" for a in st["actions"]) and not st["redo_available"]
    with pytest.raises(WorkspaceError) as e:
        ws.redo(conn, d)
    assert e.value.code == "NOTHING_TO_REDO"
    assert list(ls(conn, "gdrive:C", d)) == ["N", "Z"]

    st = ws.undo(conn, d)
    assert st["actions"][-1]["state"] == "undone" and st["redo_available"]
    version = ws.get_draft(conn, d)["version"]
    # a new action drops the redo stack for good
    mv(conn, d, "gdrive:Y", "gdrive:C")
    st = ws.get_draft(conn, d)
    assert [a["seq"] for a in st["actions"]] == [1, 2, 3, 4] and not st["redo_available"]
    assert st["actions"][-1]["items"] == [{"remote": "gdrive", "path": "Y"}] and st["version"] == version + 1
    while ws.get_draft(conn, d)["undo_available"]:
        ws.undo(conn, d)
    with pytest.raises(WorkspaceError) as e:
        ws.undo(conn, d)
    assert e.value.code == "NOTHING_TO_UNDO"
    assert list(ls(conn, "gdrive:", d)) == ["C", "X", "Y", "Z"]


def test_removing_a_middle_action_keeps_independent_later_ones(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "B/g.txt", group=2)
    put(cat, g, "C/h.txt", group=3)
    cat.dir(g, "D")
    d = new_draft(conn)
    a1 = mv(conn, d, "gdrive:A", "gdrive:D")["action"]["id"]
    a2 = mv(conn, d, "gdrive:B", "gdrive:D")["action"]["id"]
    a3 = mv(conn, d, "gdrive:C", "gdrive:D")["action"]["id"]
    out = ws.remove_action(conn, d, a2)
    assert out["removed"] == [a2]
    assert [(a["id"], a["state"]) for a in ws.get_draft(conn, d)["actions"]] == [
        (a1, "active"), (a2, "undone"), (a3, "active")]
    assert list(ls(conn, "gdrive:D", d)) == ["A", "C"]
    # redo brings the removed one back in the middle and nothing else changes
    ws.redo(conn, d)
    assert list(ls(conn, "gdrive:D", d)) == ["A", "B", "C"]
    # removing an action that is already undone changes nothing but the version
    ws.undo(conn, d)
    version = ws.get_draft(conn, d)["version"]
    assert ws.remove_action(conn, d, a3) == {"draft_version": version + 1, "removed": [a3]}
    assert [(a["id"], a["state"]) for a in ws.get_draft(conn, d)["actions"]] == [
        (a1, "active"), (a2, "active"), (a3, "undone")]
    ws.undo(conn, d)
    ws.undo(conn, d)
    assert [a["state"] for a in ws.get_draft(conn, d)["actions"]] == ["undone"] * 3
    assert list(ls(conn, "gdrive:", d)) == ["A", "B", "C", "D"]
    with pytest.raises(WorkspaceError) as e:
        ws.remove_action(conn, d, 424242)
    assert e.value.code == "NOT_FOUND"
    other = new_draft(conn)
    with pytest.raises(WorkspaceError):
        ws.remove_action(conn, other, a1)                    # an action of another draft is not addressable


def test_removing_an_early_action_that_later_ones_depend_on_through_names(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "B/f.txt", group=2)
    cat.dir(g, "C")
    d = new_draft(conn)
    a1 = mv(conn, d, "gdrive:B/f.txt", "gdrive:C")["action"]["id"]       # frees the name f.txt in B
    a2 = mv(conn, d, "gdrive:A/f.txt", "gdrive:B")["action"]["id"]       # ... which A/f.txt then takes
    out = ws.remove_action(conn, d, a1)
    assert out["removed"] == [a1, a2]                        # without a1 the name is taken again: NAME_EXISTS
    assert ws.build_ops(conn, d)[0] == []


def test_redo_refuses_an_action_that_a_rescan_made_invalid(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "A/g.txt", group=2)
    cat.dir(g, "B")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    mv(conn, d, "gdrive:A/g.txt", "gdrive:B")
    ws.undo(conn, d)
    ws.undo(conn, d)
    put(cat, g, "B/f.txt", group=3)                          # a rescan finds a new B/f.txt in the meantime
    with pytest.raises(WorkspaceError) as e:
        ws.redo(conn, d)
    assert e.value.code == "NAME_EXISTS"
    st = ws.get_draft(conn, d)
    assert [a["state"] for a in st["actions"]] == ["undone", "undone"] and st["redo_available"]
    conn.execute("DELETE FROM entries WHERE path = 'B/f.txt'")
    ws.redo(conn, d)
    ws.redo(conn, d)
    assert len(ws.build_ops(conn, d)[0]) == 2


def test_redo_undoes_later_actions_that_a_rescan_made_invalid(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "A/g.txt", group=2)
    cat.dir(g, "B")
    d = new_draft(conn)
    a1 = mv(conn, d, "gdrive:A/f.txt", "gdrive:B")["action"]["id"]
    a2 = mv(conn, d, "gdrive:A/g.txt", "gdrive:B")["action"]["id"]
    ws.remove_action(conn, d, a1)                            # a2 does not depend on a1, so it stays
    assert [a["state"] for a in ws.get_draft(conn, d)["actions"]] == ["undone", "active"]
    conn.execute("UPDATE entries SET present = 0 WHERE path = 'A/g.txt'")      # a rescan: g.txt is gone
    st = ws.redo(conn, d)
    assert [(a["id"], a["state"]) for a in st["actions"]] == [(a1, "active"), (a2, "undone")]
    assert [(o.src_path, o.dst_path) for o in ws.build_ops(conn, d)[0]] == [("A/f.txt", "B/f.txt")]


# ---------------------------------------------------------------- stale actions


def stale_of(conn, d) -> dict[int, dict | None]:
    return {a["id"]: a["stale_reason"] for a in ws.get_draft(conn, d)["actions"] if a["stale"]}


def test_a_rescan_that_removes_a_source_makes_its_action_stale_not_silent(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", size=3, group=1)
    put(cat, g, "A/g.txt", size=4, group=2)
    cat.dir(g, "B")
    cat.dir(g, "C")
    d = new_draft(conn)
    a1 = mv(conn, d, "gdrive:A/f.txt", "gdrive:B")["action"]
    a2 = mv(conn, d, "gdrive:A/g.txt", "gdrive:C")["action"]
    assert not a1["stale"] and a1["stale_reason"] is None           # nothing is wrong at staging time
    assert stale_of(conn, d) == {} and ws.get_draft(conn, d)["summary"]["stale_actions"] == 0

    conn.execute("UPDATE entries SET present = 0 WHERE path = 'A/f.txt'")           # a rescan: f.txt is gone
    st = ws.get_draft(conn, d)
    assert [a["stale"] for a in st["actions"]] == [True, False]
    why = st["actions"][0]["stale_reason"]
    assert why["code"] == "SRC_NOT_FOUND" and why["items"] == [{"remote": "gdrive", "path": "A/f.txt"}]
    assert "gdrive:A/f.txt" in why["message"]
    assert st["actions"][1]["stale_reason"] is None
    assert st["summary"]["stale_actions"] == 1 and st["summary"]["moved_files"] == 1   # g.txt still counts

    out = ws.preview(conn, d)
    assert [(a["id"], a["seq"], a["stale"]) for a in out["stale_actions"]] == [(a1["id"], 1, True)]
    assert out["stale_actions"][0]["stale_reason"]["code"] == "SRC_NOT_FOUND"
    assert out["stale_actions"][0]["items"] == [{"remote": "gdrive", "path": "A/f.txt"}]       # self-contained
    assert out["summary"]["stale_actions"] == 1
    assert out["ok"] is True and out["summary"]["ops"] == {"move": 1, "copy": 0, "delete": 0}
    assert [(c["from"]["path"], c["to"]["path"]) for c in out["changes"]] == [("A/g.txt", "C/g.txt")]
    assert a2["id"] not in {a["id"] for a in out["stale_actions"]}

    conn.execute("UPDATE entries SET present = 1 WHERE path = 'A/f.txt'")           # the file is back: not stale
    assert stale_of(conn, d) == {}


def test_only_the_vanished_items_of_a_multi_item_action_are_named(world):
    conn, cat, g, _ = world
    for i, n in enumerate("abc"):
        put(cat, g, f"A/{n}.txt", group=i + 1)
    cat.dir(g, "B")
    d = new_draft(conn)
    mv(conn, d, ["gdrive:A/a.txt", "gdrive:A/b.txt", "gdrive:A/c.txt"], "gdrive:B")
    conn.execute("UPDATE entries SET present = 0 WHERE path IN ('A/a.txt', 'A/c.txt')")
    (why,) = stale_of(conn, d).values()
    assert why["items"] == [{"remote": "gdrive", "path": "A/a.txt"}, {"remote": "gdrive", "path": "A/c.txt"}]
    assert why["message"].count("gdrive:A/") == 2 and "b.txt" not in why["message"]
    assert [(o.src_path, o.dst_path) for o in ws.build_ops(conn, d)[0]] == [("A/b.txt", "B/b.txt")]


def test_an_action_that_built_on_a_stale_one_is_stale_too(world):
    conn, cat, g, _ = world
    put(cat, g, "X/z.txt", group=1)
    put(cat, g, "Keep/k.txt", group=2)
    cat.dir(g, "Y")
    cat.dir(g, "W")
    d = new_draft(conn)
    mv(conn, d, "gdrive:X", "gdrive:Y")
    mv(conn, d, "gdrive:Y/X/z.txt", "gdrive:W")           # lives in the folder the first step brought
    mv(conn, d, "gdrive:Keep/k.txt", "gdrive:W")
    conn.execute("UPDATE entries SET present = 0 WHERE path LIKE 'X%'")             # the whole folder vanished
    st = ws.get_draft(conn, d)
    assert [a["stale"] for a in st["actions"]] == [True, True, False]
    assert st["actions"][1]["stale_reason"]["items"] == [{"remote": "gdrive", "path": "Y/X/z.txt"}]
    assert [(o.src_path, o.dst_path) for o in ws.build_ops(conn, d)[0]] == [("Keep/k.txt", "W/k.txt")]


def test_a_destination_that_is_no_longer_a_folder_makes_move_and_mkdir_stale(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    cat.dir(g, "B")
    cat.dir(g, "C")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    mkdir(conn, d, "gdrive:C", "new")
    conn.execute("UPDATE entries SET present = 0 WHERE path IN ('B', 'C')")        # both folders were deleted in the cloud
    st = ws.get_draft(conn, d)
    assert [a["stale_reason"]["code"] for a in st["actions"]] == ["DST_NOT_DIR", "DST_NOT_DIR"]
    assert st["actions"][0]["stale_reason"]["items"] == [{"remote": "gdrive", "path": "B"}]
    assert st["summary"]["stale_actions"] == 2


def test_a_new_folder_made_in_the_draft_is_a_valid_destination_and_remote_roots_always_are(world):
    conn, cat, g, x = world
    put(cat, g, "A/f.txt", group=1)
    put(cat, g, "A/g.txt", group=2)
    d = new_draft(conn)
    mkdir(conn, d, "gdrive:", "Fresh")
    mv(conn, d, "gdrive:A/f.txt", "gdrive:Fresh")
    mv(conn, d, "gdrive:A/g.txt", "dropbox:")
    assert stale_of(conn, d) == {}


def test_undone_actions_are_never_stale_and_dropping_the_stale_one_clears_the_flag(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    cat.dir(g, "B")
    d = new_draft(conn)
    a1 = mv(conn, d, "gdrive:A/f.txt", "gdrive:B")["action"]["id"]
    conn.execute("UPDATE entries SET present = 0 WHERE path = 'A/f.txt'")
    assert list(stale_of(conn, d)) == [a1]
    ws.undo(conn, d)
    st = ws.get_draft(conn, d)
    assert [(a["state"], a["stale"]) for a in st["actions"]] == [("undone", False)]
    assert st["summary"]["stale_actions"] == 0


def test_a_draft_of_only_stale_steps_previews_as_nothing_to_do_but_says_why(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", group=1)
    cat.dir(g, "B")
    d = new_draft(conn)
    a1 = mv(conn, d, "gdrive:A/f.txt", "gdrive:B")["action"]["id"]
    assert ws.preview(conn, d)["stale_actions"] == []
    conn.execute("UPDATE entries SET present = 0 WHERE path = 'A/f.txt'")
    out = ws.preview(conn, d)
    assert out["plan_id"] is None and out["message"] == "nothing to do"
    assert [a["id"] for a in out["stale_actions"]] == [a1]


def test_stale_steps_do_not_block_a_clean_preview_of_the_rest(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", size=3, md5="aa", group=1)
    put(cat, g, "A/g.txt", size=4, md5="bb", group=2)
    cat.dir(g, "B")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    mv(conn, d, "gdrive:A/g.txt", "gdrive:B")
    conn.execute("UPDATE entries SET present = 0 WHERE path = 'A/f.txt'")
    out = ws.preview(conn, d)
    assert out["ok"] and out["simulation"]["ok"] and len(out["stale_actions"]) == 1
    assert not ws.is_stale_preview(conn, out["plan_id"])        # a different notion: the draft itself did not change


def test_staleness_is_cheap_enough_to_ask_on_every_poll(world):
    """get_draft runs after every gesture and every 3 seconds per open page, with up to hundreds of steps."""
    import time
    conn, cat, g, _ = world
    for i in range(60):
        put(cat, g, f"src/d{i}/f.txt", group=i + 1)
        cat.dir(g, f"dst/e{i}")
    d = new_draft(conn)
    for i in range(60):
        mv(conn, d, f"gdrive:src/d{i}/f.txt", f"gdrive:dst/e{i}")
        mv(conn, d, f"gdrive:src/d{i}", f"gdrive:dst/e{i}")
    t0 = time.perf_counter()
    st = ws.get_draft(conn, d)
    assert time.perf_counter() - t0 < 3.0 and len(st["actions"]) == 120
    assert st["summary"]["stale_actions"] == 0


# ------------------------------------------------------------- listing details


def test_listing_order_pagination_and_flags(world):
    conn, cat, g, _ = world
    for i, n in enumerate(("file10.txt", "file2.txt", "File1.txt", "b.txt"), start=1):
        put(cat, g, f"d/{n}", group=i)
    cat.dir(g, "d/Zdir")
    cat.dir(g, "d/adir")
    cat.dir(g, "d/dir10")
    cat.dir(g, "d/dir9")
    d = new_draft(conn)
    names = [i["name"] for i in ws.list_dir(conn, "gdrive", "d", draft_id=d)["items"]]
    assert names == ["adir", "dir9", "dir10", "Zdir", "b.txt", "File1.txt", "file2.txt", "file10.txt"]
    assert names == [i["name"] for i in ws.list_dir(conn, "gdrive", "d")["items"]]
    page = ws.list_dir(conn, "gdrive", "d", limit=3, offset=2)
    assert [i["name"] for i in page["items"]] == names[2:5] and page["total"] == 8
    assert ws.list_dir(conn, "gdrive", "d", limit=3, offset=100)["items"] == []
    item = ws.list_dir(conn, "gdrive", "d")["items"][-1]
    assert set(item) == {"name", "path", "remote", "is_dir", "size", "mtime", "category", "files", "dirs", "bytes",
                         "dup_files", "entry_id", "origins", "moved_here", "merged", "conflict", "dedupe",
                         "virtual", "quarantine", "quarantine_root"}
    assert item["entry_id"] is not None and item["remote"] == "gdrive" and item["path"] == "d/file10.txt"


def test_roots_have_the_documented_shape(world):
    conn, cat, g, x = world
    put(cat, g, "a/f.txt", size=5, group=1)
    put(cat, x, "b/f.txt", size=5, group=1)
    refresh_stats(conn)
    r = ws.roots(conn)
    assert [n["remote"] for n in r] == ["gdrive", "dropbox"]
    assert r[0]["path"] == "" and r[0]["is_dir"] and r[0]["provider"] == "drive" and r[1]["case_insensitive"]
    assert (r[0]["files"], r[0]["dirs"], r[0]["bytes"], r[0]["dup_files"]) == (1, 1, 5, 1)
    assert ws.roots(conn, draft_id=new_draft(conn)) == r


# ------------------------------------------------------------------- statistics


def test_after_view_stats_use_dir_stats_and_treat_missing_rows_as_zero(world):
    conn, cat, g, x = world
    put(cat, g, "A/f1", size=10, group=1)
    put(cat, g, "A/s/f2", size=20, group=2)
    put(cat, g, "B/f3", size=30, group=3)
    put(cat, x, "A/f4", size=40, group=1)
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/s", "gdrive:B")
    # without dir_stats rows everything is 'before = 0', the formula still adds what arrived
    b = ls(conn, "gdrive:", d)["B"]
    assert (b["files"], b["bytes"], b["dirs"]) == (1, 20, 1)
    assert (ls(conn, "gdrive:", d)["A"]["files"]) == 0                  # max(0, 0 - 1): never negative
    refresh_stats(conn)
    b = ls(conn, "gdrive:", d)["B"]
    assert (b["files"], b["bytes"], b["dirs"], b["dup_files"]) == (2, 50, 1, 0)
    a = ls(conn, "gdrive:", d)["A"]
    assert (a["files"], a["bytes"], a["dirs"], a["dup_files"]) == (1, 10, 0, 1)
    mv(conn, d, "gdrive:A", "dropbox:")                                  # f1 is a duplicate of dropbox:A/f4
    assert "A" not in ls(conn, "gdrive:", d)
    dbx = ws.roots(conn, draft_id=d)
    assert (dbx[1]["files"], dbx[1]["bytes"], dbx[1]["dup_files"]) == (2, 50, 2)
    assert (dbx[0]["files"], dbx[0]["bytes"], dbx[0]["dup_files"], dbx[0]["dirs"]) == (2, 50, 0, 2)


def test_a_dir_that_is_merged_counts_once(world):
    conn, cat, g, _ = world
    put(cat, g, "S/T/a", size=1, group=1)
    put(cat, g, "S/T/sub/x", size=1, group=2)
    put(cat, g, "R/T/b", size=1, group=3)
    put(cat, g, "R/T/sub/y", size=1, group=4)
    refresh_stats(conn)
    d = new_draft(conn)
    mv(conn, d, "gdrive:R/T", "gdrive:S")
    s = ls(conn, "gdrive:", d)["S"]
    assert (s["files"], s["dirs"]) == (4, 2)                           # S/T and S/T/sub, each once
    assert ws.roots(conn, draft_id=d)[0]["dirs"] == 4                  # S, S/T, S/T/sub, and the now empty R
    assert ls(conn, "gdrive:S/T", d)["sub"]["files"] == 2


def test_the_moved_set_maps_files_that_leave_a_moving_folder_individually(world):
    conn, cat, g, _ = world
    for i in range(60):
        put(cat, g, f"big/f{i}.txt", size=i + 1, group=100 + i)
    put(cat, g, "big/inner/f1.txt", size=500, group=999)           # same name as a moved file, elsewhere
    cat.dir(g, "out")
    cat.dir(g, "dest")
    d = new_draft(conn)
    mv(conn, d, "gdrive:big/f7.txt", "gdrive:out")
    mv(conn, d, "gdrive:big/f1.txt", "gdrive:out", new_name="one.txt")
    mv(conn, d, "gdrive:big", "gdrive:dest")
    mv(conn, d, "gdrive:dest/big/f3.txt", "gdrive:dest/big", new_name="three.txt")
    ops, info = ws.build_ops(conn, d)
    got = {(o.src_path): o.dst_path for o in ops}
    assert len(ops) == 61 and info["conflicts"] == []
    assert got["big/f7.txt"] == "out/f7.txt" and got["big/f1.txt"] == "out/one.txt"
    assert got["big/f3.txt"] == "dest/big/three.txt" and got["big/f9.txt"] == "dest/big/f9.txt"
    assert got["big/inner/f1.txt"] == "dest/big/inner/f1.txt"       # the namesake follows its folder
    assert sim(conn, ops).ok
    roots = {(c["from"]["path"], c["to"]["path"]) for c in info["changes"]}
    assert roots == {("big", "dest/big"), ("big/f7.txt", "out/f7.txt"), ("big/f1.txt", "out/one.txt"),
                     ("big/f3.txt", "dest/big/three.txt")}


def test_brute_force_stats_agree_with_dirstats_refresh(world):
    dirstats = pytest.importorskip("drivemerge.dirstats")
    conn, cat, g, x = world
    rng = random.Random(5)
    random_catalog(cat, rng, tame=False)
    refresh_stats(conn)
    mine = {tuple(r) for r in conn.execute("SELECT remote_id, path, files, dirs, bytes, dup_files FROM dir_stats")}
    dirstats.refresh(conn)
    theirs = {tuple(r) for r in conn.execute("SELECT remote_id, path, files, dirs, bytes, dup_files FROM dir_stats")}
    assert mine <= theirs and len(mine) > 10


# ------------------------------------------------------ preview, approval, stale


def test_preview_binds_the_plan_to_the_draft_version(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", size=3, md5="aa", group=1)
    cat.dir(g, "B")
    cat.dir(g, "C")
    d = new_draft(conn, "School stuff")
    mv(conn, d, "gdrive:A", "gdrive:B")
    out = ws.preview(conn, d)
    assert out["simulation"]["ok"] and out["draft_version"] == 1
    plan = plans.get_plan(conn, out["plan_id"])
    assert plan.kind == "workspace" and plan.title == "Workspace: School stuff" and plan.created_by == "workspace"
    assert plan.params == {"draft_id": d, "draft_version": 1} and plan.status == "simulated"
    dr = ws.get_draft(conn, d)
    assert (dr["preview_plan_id"], dr["preview_version"]) == (plan.id, 1)
    assert set(out) == {"plan_id", "draft_version", "ok", "simulation", "summary", "changes", "conflicts",
                        "empty_dirs_left", "empty_dirs_skipped", "stale_actions"}
    assert out["ok"] is True
    assert out["changes"] == [{"from": {"remote": "gdrive", "path": "A"}, "to": {"remote": "gdrive", "path": "B/A"},
                               "files": 1, "bytes": 3, "kind": "move", "is_dir": True}]
    assert out["empty_dirs_left"] == 1 and out["conflicts"] == []
    assert not ws.is_stale_preview(conn, plan.id)

    # approval binds to the simulation of exactly this plan
    plans.approve_plan(conn, plan.id, plans.short_code(plan.digest), approved_by="test")
    assert plans.get_plan(conn, plan.id, with_ops=False).status == "approved"

    mv(conn, d, "gdrive:B", "gdrive:C")                       # any change makes the preview stale
    assert ws.is_stale_preview(conn, plan.id)
    assert ws.get_draft(conn, d)["preview_version"] == 1 != ws.get_draft(conn, d)["version"]
    again = ws.preview(conn, d)
    assert again["plan_id"] != plan.id and not ws.is_stale_preview(conn, again["plan_id"])
    assert ws.is_stale_preview(conn, plan.id)
    # the old preview, approved or not, can no longer be run from anywhere
    assert plans.get_plan(conn, plan.id, with_ops=False).status == "cancelled"
    assert plans.get_plan(conn, again["plan_id"], with_ops=False).status == "simulated"
    ws.undo(conn, d)
    assert ws.is_stale_preview(conn, again["plan_id"])        # undo is a change as well
    ws.discard_draft(conn, d)
    assert ws.is_stale_preview(conn, again["plan_id"])
    other = plans.create_plan(conn, "x", "manual", [], created_by="t")
    assert not ws.is_stale_preview(conn, other)


def test_a_plan_that_only_claims_to_be_a_preview_is_not_judged_by_a_draft(world):
    """Only workspace.preview makes a plan of kind 'workspace', created by 'workspace'; a proposer's params
    cannot tie its plan to someone's open draft (the same rule as Service.execute)."""
    conn, cat, g, _ = world
    d = new_draft(conn)
    params = {"draft_id": d, "draft_version": 99}
    forged = plans.create_plan(conn, "x", "workspace", [], created_by="api", params=params)
    genuine = plans.create_plan(conn, "x", "workspace", [], created_by="workspace", params=params)
    assert not ws.is_stale_preview(conn, forged)
    assert ws.is_stale_preview(conn, genuine)                 # version 99 is not the draft's version 0


def test_previewing_a_draft_that_became_empty_retires_the_plan_of_the_moves_taken_back(world):
    """The CLI approves any simulated plan, so a plan for moves the draft no longer holds must not stay one."""
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", size=3, md5="aa", group=1)
    cat.dir(g, "B")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    first = ws.preview(conn, d)
    ws.undo(conn, d)
    out = ws.preview(conn, d)
    assert out["plan_id"] is None and out["message"] == "nothing to do"
    assert plans.get_plan(conn, first["plan_id"], with_ops=False).status == "cancelled"
    with pytest.raises(plans.PlanError):
        plans.approve_plan(conn, first["plan_id"], plans.short_code(first["simulation"]["digest"]), approved_by="cli")
    dr = ws.get_draft(conn, d)
    assert (dr["preview_plan_id"], dr["preview_version"]) == (None, None)       # nothing is "the current preview"
    assert ws.preview(conn, d)["plan_id"] is None                               # and asking again is harmless

    # an approved one is retired as well
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    second = ws.preview(conn, d)
    plans.approve_plan(conn, second["plan_id"], plans.short_code(second["simulation"]["digest"]), approved_by="cli")
    ws.undo(conn, d)
    ws.preview(conn, d)
    assert plans.get_plan(conn, second["plan_id"], with_ops=False).status == "cancelled"


def test_a_preview_whose_steps_all_went_stale_is_retired_too(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", size=3, md5="aa", group=1)
    cat.dir(g, "B")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    first = ws.preview(conn, d)
    conn.execute("UPDATE entries SET present = 0 WHERE path = 'A/f.txt'")        # a rescan: the file is gone
    out = ws.preview(conn, d)
    assert out["plan_id"] is None and len(out["stale_actions"]) == 1
    assert plans.get_plan(conn, first["plan_id"], with_ops=False).status == "cancelled"
    # the draft version did not move, so the pointer must not claim that a (dead) preview is current
    assert ws.get_draft(conn, d)["preview_plan_id"] is None


def test_a_plan_that_already_ran_is_history_and_stays(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", size=3, md5="aa", group=1)
    cat.dir(g, "B")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    first = ws.preview(conn, d)
    plans.set_status(conn, first["plan_id"], "done")
    ws.undo(conn, d)
    assert ws.preview(conn, d)["plan_id"] is None
    assert plans.get_plan(conn, first["plan_id"], with_ops=False).status == "done"
    assert ws.get_draft(conn, d)["preview_plan_id"] == first["plan_id"]


def test_discarding_a_draft_cancels_its_unrun_preview_plan(world):
    conn, cat, g, _ = world
    put(cat, g, "A/f.txt", size=3, md5="aa", group=1)
    cat.dir(g, "B")
    d = new_draft(conn)
    mv(conn, d, "gdrive:A/f.txt", "gdrive:B")
    pid = ws.preview(conn, d)["plan_id"]
    ws.discard_draft(conn, d)
    assert plans.get_plan(conn, pid, with_ops=False).status == "cancelled"
    ws.discard_draft(conn, d)                                                   # idempotent
    # one that ran is kept
    d2 = new_draft(conn)
    mv(conn, d2, "gdrive:A/f.txt", "gdrive:B", new_name="g.txt")
    ran = ws.preview(conn, d2)["plan_id"]
    plans.set_status(conn, ran, "done")
    ws.discard_draft(conn, d2)
    assert plans.get_plan(conn, ran, with_ops=False).status == "done"
    # a draft that was never previewed has nothing to cancel
    ws.discard_draft(conn, new_draft(conn))


def test_a_failed_preview_still_records_the_plan_but_never_becomes_approvable(world):
    conn, cat, g, _ = world
    put(cat, g, "S/T/a", size=1, md5="aa", group=1)
    put(cat, g, "R/T/a", size=1, md5="bb", group=2)
    d = new_draft(conn)
    mv(conn, d, "gdrive:R/T", "gdrive:S")
    out = ws.preview(conn, d)
    assert not out["simulation"]["ok"]
    plan = plans.get_plan(conn, out["plan_id"], with_ops=False)
    assert plan.status == "draft"
    with pytest.raises(plans.PlanError):
        plans.approve_plan(conn, plan.id, plans.short_code(plan.digest), approved_by="test")


def test_preview_honours_a_safety_policy(world):
    conn, cat, g, x = world
    put(cat, g, "A/f.txt", size=100, md5="aa", group=1)
    d = new_draft(conn)
    mv(conn, d, "gdrive:A", "dropbox:")
    out = ws.preview(conn, d, policy=simulate.SafetyPolicy(free_bytes={x: 10}))
    assert "QUOTA" in [v["code"] for v in out["simulation"]["violations"]]


# ----------------------------------------------------------- differential test

NAMES = ["a", "b", "c", "d", "e", "ab"]      # "a" and "ab": paths match by whole segments only
FILES = ["f1", "f2", "f3", "x.txt"]


def jn(a: str, b: str) -> str:
    return f"{a}/{b}" if a else b


def under(p: str, base: str) -> bool:
    return p == base or p.startswith(base + "/")


class Thing:
    """One catalog entry (or mkdir) tracked individually, the slow and obviously correct way."""

    def __init__(self, kind, origin, key=None, size=0, dup=False, ident=None):
        self.kind, self.origin, self.cur = kind, origin, origin
        self.key, self.size, self.dup, self.ident = key, size, dup, ident


def landed(files: list[Thing], things: list[Thing]) -> list[Thing]:
    """The files that still exist at their final spots once a merge has dropped identical arrivals.

    At each spot the files that never moved stay. A file that moved there is dropped when the content
    already there (staying files, or an identical file that moved there first, in (origin, id) order)
    is exactly its own; any other content already there makes it a conflict, so it stays. A catalog
    folder at the spot (even one that has been moved away: execution leaves it behind) blocks it, and
    nothing is dropped.
    """
    by_spot: dict[tuple, list[Thing]] = {}
    for t in files:
        by_spot.setdefault(t.cur, []).append(t)
    folders = {t.origin for t in things if t.kind == "dir"}
    out: list[Thing] = []
    for spot, ts in by_spot.items():
        stay = [t for t in ts if t.cur == t.origin]
        out += stay
        taken = None
        for t in sorted((t for t in ts if t.cur != t.origin), key=lambda t: (t.origin, t.ident)):
            there = {x.key for x in stay} | ({taken} if taken is not None else set())
            if spot not in folders and there == {t.key}:
                continue
            out.append(t)
            if not there:
                taken = t.key
    return out


class Model:
    def __init__(self, things, rid):
        self.things, self.rid = things, rid

    @classmethod
    def from_catalog(cls, conn, rid):
        rows = [dict(r) for r in conn.execute("SELECT * FROM entries WHERE present = 1")]
        copies = Counter(r["content_group"] for r in rows if not r["is_dir"] and r["content_group"] is not None)
        things = []
        for r in rows:
            loc = (r["remote_id"], r["path"])
            if r["is_dir"]:
                things.append(Thing("dir", loc))
            else:
                things.append(Thing("file", loc, key=r["content_group"], size=r["size"] or 0,
                                    dup=copies[r["content_group"]] >= 2, ident=r["id"]))
        return cls(things, rid)

    def copy(self) -> "Model":
        out = Model([], self.rid)
        for t in self.things:
            c = Thing(t.kind, t.origin, t.key, t.size, t.dup, t.ident)
            c.cur = t.cur
            out.things.append(c)
        return out

    def sub(self, src, dst) -> None:
        for t in self.things:
            if t.cur[0] == src[0] and under(t.cur[1], src[1]):
                t.cur = (dst[0], dst[1] + t.cur[1][len(src[1]):])

    def apply(self, act: dict) -> None:
        dst = (self.rid[act["dst"]["remote"]], act["dst"]["path"])
        if act["kind"] == "mkdir":
            self.things.append(Thing("virt", (dst[0], jn(dst[1], act["new_name"]))))
            return
        for it in act["items"]:
            src = (self.rid[it["remote"]], it["path"])
            tgt = (dst[0], jn(dst[1], act["new_name"] or posixpath.basename(it["path"])))
            if src != tgt:
                self.sub(src, tgt)

    def kinds(self, loc) -> set[str]:
        return {"file" if t.kind == "file" else "dir" for t in self.things if t.cur == loc}


def bad_name(n) -> bool:
    return not n or "/" in n or n in (".", "..") or "\x00" in n


def predict(model: Model, body: dict):
    """Expected (error code or None, warning codes) for a staging request, from docs/WORKSPACE.md section A."""
    rid = model.rid
    dst = (rid[body["dst"]["remote"]], body["dst"]["path"])
    nn = body.get("new_name")
    if body["kind"] == "mkdir":
        if bad_name(nn):
            return "BAD_NAME", set()
        if dst[1] and "dir" not in model.kinds(dst):
            return "DST_NOT_DIR", set()
        occ = model.kinds((dst[0], jn(dst[1], nn)))
        return ("NAME_EXISTS" if "file" in occ else "NOOP" if occ else None), set()
    raw = [(rid[i["remote"]], i["path"]) for i in body["items"]]
    if any(p == "" for _, p in raw):
        return "SRC_NOT_FOUND", set()
    uniq = list(dict.fromkeys(raw))
    items = [it for it in uniq if not any(o != it and o[0] == it[0] and under(it[1], o[1]) for o in uniq)]
    if nn is not None and (bad_name(nn) or len(items) != 1):
        return "BAD_NAME", set()
    kinds = [model.kinds(it) for it in items]
    if any(not k for k in kinds):
        return "SRC_NOT_FOUND", set()
    if dst[1] and "dir" not in model.kinds(dst):
        return "DST_NOT_DIR", set()
    for it, k in zip(items, kinds):
        if "dir" in k and it[0] == dst[0] and under(dst[1], it[1]):
            return "INTO_ITSELF", set()
    m = model.copy()
    warns, real = set(), 0
    for it, k in zip(items, kinds):
        tgt = (dst[0], jn(dst[1], nn or posixpath.basename(it[1])))
        if tgt == it:
            continue
        occ = m.kinds(tgt)
        if occ:
            if "dir" not in k or "file" in occ:
                return "NAME_EXISTS", set()
            warns.add("MERGE")
        m.sub(it, tgt)
        real += 1
    if not real:
        return "NOOP", set()
    if any(it[0] != dst[0] for it in items):
        warns.add("CROSS_REMOTE")
    return None, warns


def random_catalog(cat, rng, tame: bool):
    """A few folders and files on three remotes. Wild: contents are mostly unique, so merges collide.
    Tame: a file name always carries the same content, so merges dedupe instead."""
    rids = {name: cat.remote(name, prov) for name, prov in (("g", "drive"), ("d", "dropbox"), ("o", "onedrive"))}
    unique = 1000
    for name, rid in rids.items():
        dirs = [""]
        taken = set()
        for _ in range(rng.randint(4, 7)):
            parent = rng.choice(dirs)
            path = jn(parent, rng.choice(NAMES))
            if path.count("/") >= 3 or path in taken:
                continue
            taken.add(path)
            cat.dir(rid, path)
            dirs.append(path)
        for _ in range(rng.randint(6, 11)):
            path = jn(rng.choice(dirs), rng.choice(FILES))
            if path in taken:
                continue
            taken.add(path)
            c = FILES.index(path.rpartition("/")[2]) if tame else rng.randint(0, 6)
            if c < 4:       # contents shared across remotes, so merges can dedupe
                put(cat, rid, path, size=10 + c, md5=f"{c:032x}", group=c + 1)
            else:
                unique += 1
                put(cat, rid, path, size=20 + c, md5=f"{unique:032x}", group=unique)
    refresh_stats(cat.conn)
    return rids


def active_actions(conn, d) -> list[dict]:
    return [a for a in ws.get_draft(conn, d)["actions"] if a["state"] == "active"]


def model_of(conn, d, base: Model) -> Model:
    m = base.copy()
    for a in active_actions(conn, d):
        m.apply(a)
    return m


def check_views(conn, d, m: Model, names: dict[int, str]) -> None:
    # A copy that a merge deletes is gone for good: what is left of a content is what can still be a duplicate.
    copies_after = Counter(t.key for t in landed([t for t in m.things if t.kind == "file"], m.things))
    still_dup = lambda t: copies_after[t.key] >= 2  # noqa: E731
    by_parent: dict[tuple, dict[str, list[Thing]]] = {}
    for t in m.things:
        by_parent.setdefault((t.cur[0], posixpath.dirname(t.cur[1])), {}).setdefault(t.cur[1], []).append(t)
    dirs = {(rid, "") for rid in m.rid.values()} | {t.cur for t in m.things if t.kind != "file"}
    assert set(by_parent) <= dirs, "something sits in a folder that does not exist"
    for rid, dpath in sorted(dirs):
        got = ls(conn, f"{names[rid]}:{dpath}", d)
        exp = by_parent.get((rid, dpath), {})
        assert set(got) == {posixpath.basename(p) for p in exp}, (names[rid], dpath)
        for p, ts in exp.items():
            it = got[posixpath.basename(p)]
            files = [t for t in ts if t.kind == "file"]
            subdirs = [t for t in ts if t.kind != "file"]
            keys = {t.key for t in files}
            where = (names[rid], p)
            assert it["is_dir"] == bool(subdirs), where
            assert it["merged"] == (len({t.origin for t in subdirs}) >= 2), where
            assert it["conflict"] == (len(keys) > 1 or bool(files and subdirs)), where
            assert it["dedupe"] == (len(files) >= 2 and len(keys) == 1 and not subdirs), where
            assert it["virtual"] == all(t.kind == "virt" for t in ts), where
            real = sorted({(names[t.origin[0]], t.origin[1]) for t in ts if t.kind != "virt"})
            assert sorted((o["remote"], o["path"]) for o in it["origins"]) == real, where
            assert it["moved_here"] == any(t.origin != t.cur for t in ts if t.kind != "virt"), where
            if it["is_dir"]:
                under_ = [t for t in m.things if t.cur[0] == rid and t.cur[1].startswith(p + "/")]
                fs = landed([t for t in under_ if t.kind == "file"], m.things)
                assert (it["files"], it["dirs"], it["bytes"], it["dup_files"]) == (
                    len(fs), len({t.cur for t in under_ if t.kind != "file"}), sum(t.size for t in fs),
                    sum(t.dup for t in fs)), where
    for node in ws.roots(conn, draft_id=d):
        rid = m.rid[node["remote"]]
        mine = [t for t in m.things if t.cur[0] == rid]
        fs = landed([t for t in mine if t.kind == "file"], m.things)
        assert (node["files"], node["dirs"], node["bytes"], node["dup_files"]) == (
            len(fs), len({t.cur for t in mine if t.kind != "file"}), sum(t.size for t in fs),
            sum(still_dup(t) for t in fs))
    # ghosts: every original child of a folder that the draft moved away
    originals: dict[tuple, list[Thing]] = {}
    for t in m.things:
        if t.kind != "virt":
            originals.setdefault((t.origin[0], posixpath.dirname(t.origin[1])), []).append(t)
    for rid, dpath in sorted(originals):
        out = ws.list_dir(conn, names[rid], dpath, draft_id=d, ghosts=True, limit=10000)["ghosts"]
        exp = sorted((posixpath.basename(t.origin[1]), t.origin[1], t.kind == "dir", names[t.cur[0]], t.cur[1])
                     for t in originals[(rid, dpath)] if t.cur != t.origin)
        assert sorted((g["name"], g["path"], g["is_dir"], g["moved_to"]["remote"], g["moved_to"]["path"])
                      for g in out) == exp
        before = ws.list_dir(conn, names[rid], dpath, draft_id=d, view="before", limit=10000)["items"]
        assert sorted(i["path"] for i in before) == sorted(t.origin[1] for t in originals[(rid, dpath)])


def check_ops(conn, d, m: Model, names: dict[int, str], seen: Counter) -> None:
    """The net-effect ops, replayed naively, reproduce the model's final file layout, or are flagged."""
    ops, info = ws.build_ops(conn, d)
    files = [t for t in m.things if t.kind == "file"]
    dirs_at_origin = {t.origin for t in m.things if t.kind == "dir"}
    moving = {t.ident for t in files if t.cur != t.origin}
    want: dict[tuple, set] = {}
    for t in files:
        want.setdefault(t.cur, set()).add(t.key)
    expected_bad = set()
    for t in files:
        if t.ident not in moving:
            continue
        if len(want[t.cur]) > 1 or t.cur in dirs_at_origin:
            expected_bad.add(t.cur)
        parts = t.cur[1].split("/")
        for i in range(1, len(parts)):
            if (t.cur[0], "/".join(parts[:i])) in want:
                expected_bad.add(t.cur)
    flagged = {(c["to"]["remote"], c["to"]["path"]) for c in info["conflicts"]
               if c["code"] in ("DIFFERENT_CONTENT", "DIR_IN_WAY", "FILE_IN_WAY")}
    assert flagged == {(names[r], p) for r, p in expected_bad}
    report = sim(conn, ops)
    if expected_bad:                                         # different content, folders and files in the way
        assert not report.ok
        seen["bad"] += 1
        return
    if info["conflicts"]:       # CYCLE / OCCUPIED: the plan could not come out as drawn (the simulator may not mind)
        assert {c["code"] for c in info["conflicts"]} <= {"CYCLE", "OCCUPIED"}
        seen["structural"] += 1
        return
    assert report.ok, [(v.code, v.message) for v in report.violations]
    seen["ok" if ops else "empty"] += 1
    seen["dedupes"] += info["dedupes"]
    seen["cross"] += any(o.op == "copy" for o in ops)
    state = {(r["remote_id"], r["path"]): r["content_group"]
             for r in conn.execute("SELECT * FROM entries WHERE present = 1 AND is_dir = 0")}
    for o in ops:
        src = (o.src_remote_id, o.src_path)
        if o.op == "delete":
            assert state[(o.witness_remote_id, o.witness_path)] == state[src]
            del state[src]
        else:
            dst = (o.dst_remote_id, o.dst_path)
            assert dst not in state
            state[dst] = state[src]
            if o.op == "move":
                del state[src]
    assert state == {loc: next(iter(keys)) for loc, keys in want.items()}


def propose(rng, m: Model, names: dict[int, str]) -> dict:
    """A random staging request: mostly sensible, sometimes plainly wrong."""
    locs = sorted({t.cur for t in m.things})
    dirs = sorted({t.cur for t in m.things if t.kind != "file"} | {(rid, "") for rid in m.rid.values()})

    def spec(loc):
        return {"remote": names[loc[0]], "path": loc[1]}

    def pick_dst():
        r = rng.random()
        if r < 0.85 or not locs:
            return rng.choice(dirs)
        if r < 0.93:
            return rng.choice(locs)
        return (rng.choice(list(m.rid.values())), rng.choice(["nope", "a/zz"]))

    if rng.random() < 0.12:
        name = rng.choice(NAMES + ["n1", "n2"] + (["", "a/b", ".."] if rng.random() < 0.1 else []))
        return {"kind": "mkdir", "dst": spec(pick_dst()), "new_name": name}
    if rng.random() < 0.3:          # aim a folder at a place that already has a folder of that name: a merge
        names_at = {}
        for t in m.things:
            if t.kind != "file" and t.cur[1]:
                names_at.setdefault(posixpath.basename(t.cur[1]), []).append(t.cur)
        pairs = [(a, posixpath.dirname(b[1]), b[0]) for locs_ in names_at.values() for a in locs_ for b in locs_
                 if a != b and (a[0], posixpath.dirname(a[1])) != (b[0], posixpath.dirname(b[1]))]
        if pairs:
            src, parent, prid = rng.choice(pairs)
            return {"kind": "move", "items": [spec(src)], "dst": spec((prid, parent))}
    items = []
    for _ in range(rng.choice([1, 1, 1, 2, 3])):
        r = rng.random()
        if r < 0.9 and locs:
            loc = rng.choice(locs)
            if rng.random() < 0.15 and loc[1]:
                loc = (loc[0], posixpath.dirname(loc[1]))
            if loc[1]:
                items.append(loc)
        elif r < 0.97:
            items.append((rng.choice(list(m.rid.values())), "nothing/here"))
    if not items:
        items.append(rng.choice(locs))
    body = {"kind": "move", "items": [spec(i) for i in items], "dst": spec(pick_dst())}
    if rng.random() < 0.3:
        body["new_name"] = rng.choice(NAMES + FILES + (["", "x/y", "."] if rng.random() < 0.1 else []))
    return body


# 5004, 6016 and 6036 once produced a file that had to move into a folder of its own name: unflagged and unexecutable.
@pytest.mark.parametrize("seed, tame", [(20240601, False), (7, False), (424242, True), (11, True),
                                        (5004, False), (6016, True), (6036, True)])
def test_random_drafts_match_a_naive_model(conn, catalog, seed, tame):
    rng = random.Random(seed)
    rid = random_catalog(catalog, rng, tame)
    names = {v: k for k, v in rid.items()}
    base = Model.from_catalog(conn, rid)
    d = new_draft(conn)
    seen: Counter = Counter()
    accepted = 0
    steps = 0
    while accepted < 200 and steps < 1500:
        steps += 1
        roll = rng.random()
        if rng.random() < 0.02:                              # now and then start over: small drafts stay clean
            d = new_draft(conn)
        before = model_of(conn, d, base)
        if roll < 0.06 and ws.get_draft(conn, d)["undo_available"]:
            ws.undo(conn, d)
        elif roll < 0.20 and ws.get_draft(conn, d)["redo_available"]:
            expected = redo_expected(conn, d, base)
            if isinstance(expected, str):
                with pytest.raises(WorkspaceError) as e:
                    ws.redo(conn, d)
                assert e.value.code == expected
            else:
                ws.redo(conn, d)
                assert [a["id"] for a in active_actions(conn, d)] == expected
        elif roll < 0.24 and active_actions(conn, d):
            victim = rng.choice(ws.get_draft(conn, d)["actions"])
            expected = remove_expected(conn, d, base, victim["id"])
            out = ws.remove_action(conn, d, victim["id"])
            assert [a["id"] for a in active_actions(conn, d)] == expected
            assert out["removed"][0] == victim["id"]
        else:
            body = propose(rng, before, names)
            code, warns = predict(before, body)
            version = ws.get_draft(conn, d)["version"]
            if code:
                with pytest.raises(WorkspaceError) as e:
                    ws.add_action(conn, d, body, created_by="fuzz")
                assert e.value.code == code, (body, str(e.value))
                assert ws.get_draft(conn, d)["version"] == version
                continue
            out = ws.add_action(conn, d, body, created_by="fuzz")
            assert {w["code"] for w in out["warnings"]} & {"MERGE", "CROSS_REMOTE"} == warns, body
            assert out["draft_version"] == version + 1
            accepted += 1
        after = model_of(conn, d, base)
        assert not any(a["stale"] for a in ws.get_draft(conn, d)["actions"]), "an untouched catalog has no stale steps"
        if steps % 9 == 0 or accepted <= 10:                 # small drafts first: they are the ones that stay clean
            check_views(conn, d, after, names)
            check_ops(conn, d, after, names, seen)
    assert accepted >= 150
    check_views(conn, d, model_of(conn, d, base), names)
    check_ops(conn, d, model_of(conn, d, base), names, seen)
    assert seen["ok"] >= (8 if tame else 1), seen            # the clean path really is exercised
    assert seen["cross"] >= 1 or not tame, seen


def replay_ok(base: Model, actions: list[dict]) -> list[dict]:
    """Which of `actions`, staged in order on top of each other, validate (the cascade the engine must match)."""
    m = base.copy()
    kept = []
    for a in actions:
        body = {"kind": a["kind"], "dst": a["dst"], "new_name": a["new_name"], "items": a["items"]}
        if predict(m, body)[0] is None:
            m.apply(a)
            kept.append(a)
    return kept


def remove_expected(conn, d, base, victim_id) -> list[int]:
    acts = ws.get_draft(conn, d)["actions"]
    victim = next(a for a in acts if a["id"] == victim_id)
    if victim["state"] == "undone":
        return [a["id"] for a in acts if a["state"] == "active"]
    head = [a for a in acts if a["state"] == "active" and a["seq"] < victim["seq"]]
    tail = [a for a in acts if a["state"] == "active" and a["seq"] > victim["seq"]]
    m = base.copy()
    for a in head:
        m.apply(a)
    return [a["id"] for a in head] + [a["id"] for a in replay_ok(m, tail)]


def redo_expected(conn, d, base):
    acts = ws.get_draft(conn, d)["actions"]
    target = next(a for a in acts if a["state"] == "undone")
    head = [a for a in acts if a["state"] == "active" and a["seq"] < target["seq"]]
    tail = [a for a in acts if a["state"] == "active" and a["seq"] > target["seq"]]
    m = base.copy()
    for a in head:
        m.apply(a)
    body = {"kind": target["kind"], "dst": target["dst"], "new_name": target["new_name"], "items": target["items"]}
    code = predict(m, body)[0]
    if code:
        return code
    m.apply(target)
    return [a["id"] for a in head] + [target["id"]] + [a["id"] for a in replay_ok(m, tail)]


# ------------------------------------------------- messages a page can word itself


def test_warnings_carry_params_so_a_page_can_word_them(world):
    """Each warning is a code plus the facts of its English message (a place is {"remote", "path"})."""
    conn, cat, g, x = world
    ro = cat.remote("archive", "other", writable=False)
    put(cat, g, "a/f.txt", group=1)
    put(cat, g, "Research/Thesis/c.txt", group=3)
    put(cat, g, "School/Thesis/d.txt", group=4)
    put(cat, x, "d/readme.md", group=5)
    put(cat, x, "d/other.md", group=6)
    cat.dir(x, "box")
    d = new_draft(conn)
    by_code = {}
    for result in (mv(conn, d, "gdrive:Research/Thesis", "gdrive:School"),
                   mv(conn, d, "gdrive:a/f.txt", "dropbox:box"),
                   mv(conn, d, "dropbox:box/f.txt", "archive:"),
                   mv(conn, d, "dropbox:d/readme.md", "dropbox:d", new_name="README.md"),
                   mv(conn, d, "dropbox:d/other.md", "dropbox:d", new_name="README.MD")):
        by_code.update({w["code"]: w for w in result["warnings"]})
    assert set(by_code) == {"MERGE", "CROSS_REMOTE", "READ_ONLY", "CASE_ONLY_RENAME", "CASE_COLLISION"}
    for w in by_code.values():
        assert isinstance(w["message"], str) and w["message"]       # the English sentence stays for CLI/API/MCP
    assert by_code["MERGE"]["params"] == {"src": L("gdrive:Research/Thesis"), "into": L("gdrive:School/Thesis")}
    assert by_code["CROSS_REMOTE"]["params"] == {"n": 1}
    assert by_code["READ_ONLY"]["params"] == {"remote": "archive"}
    assert by_code["CASE_ONLY_RENAME"]["params"] == {"remote": "dropbox", "from": "readme.md", "to": "README.md"}
    assert by_code["CASE_COLLISION"]["params"] == {"remote": "dropbox", "name": "README.MD", "dst": L("dropbox:d")}
    assert mkdir(conn, d, "archive:", "new")["warnings"][0]["params"] == {"remote": "archive"}


def test_refusals_carry_params(world):
    conn, cat, g, x = world
    put(cat, g, "a/f.txt", group=1)
    put(cat, g, "b/f.txt", group=2)
    cat.dir(g, "c")
    d = new_draft(conn)
    assert refuse(conn, d, "SRC_NOT_FOUND", "gdrive:a/missing.txt", "gdrive:c").params == {"item": L("gdrive:a/missing.txt")}
    assert refuse(conn, d, "DST_NOT_DIR", "gdrive:a/f.txt", "gdrive:b/f.txt").params == {"dst": L("gdrive:b/f.txt")}
    assert refuse(conn, d, "INTO_ITSELF", "gdrive:a", "gdrive:a").params == {"item": L("gdrive:a")}
    assert refuse(conn, d, "NAME_EXISTS", "gdrive:a/f.txt", "gdrive:b").params == {"item": L("gdrive:b/f.txt")}
    assert refuse(conn, d, "BAD_NAME", "gdrive:a/f.txt", "gdrive:c", new_name="x/y").params == {"name": "x/y"}
    assert refuse(conn, d, "NOOP", "gdrive:a/f.txt", "gdrive:a").params == {"dst": L("gdrive:a")}
    with pytest.raises(WorkspaceError) as e:
        ws.get_draft(conn, 999)
    assert e.value.code == "NOT_FOUND" and e.value.params == {"draft": 999}
    ws.discard_draft(conn, d)
    with pytest.raises(WorkspaceError) as e:
        mv(conn, d, "gdrive:a/f.txt", "gdrive:c")
    assert e.value.code == "NOT_OPEN" and e.value.params == {"draft": d, "status": "discarded"}
    with pytest.raises(WorkspaceError) as e:
        ws.remove_action(conn, new_draft(conn), 7)
    assert e.value.code == "NOT_FOUND" and set(e.value.params) == {"draft", "action"}
    assert WorkspaceError("X", "message").params == {}
