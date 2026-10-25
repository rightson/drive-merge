"""Gather (docs/BULK.md §D): one copy per content into one folder, staged as one ordinary move action."""

from __future__ import annotations

import time

import pytest

from drivemerge import db, gather, workspace as ws
from drivemerge.workspace import WorkspaceError

QUARANTINE = db.DEFAULT_QUARANTINE_DIR


class W:
    """A catalog with gdrive (case-sensitive), dropbox (ignores case), onedrive and a read-only archive."""

    def __init__(self, conn, catalog):
        self.conn, self.cat = conn, catalog
        self.g = catalog.remote("gdrive", "drive")
        self.d = catalog.remote("dropbox", "dropbox")
        self.o = catalog.remote("onedrive", "onedrive")
        self.ro = catalog.remote("archive", "local", writable=False)
        self.draft = ws.create_draft(conn, "t")["id"]
        self._n = 0

    def content(self, *places, size: int = 100, **kw) -> str:
        """One content with a copy at each (remote id, path); returns its key."""
        self._n += 1
        ids = [self.cat.file(rid, path, size=size, **kw) for rid, path in places]
        for i in ids:
            self.conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (self._n, i))
        return f"g:{self._n}"

    def single(self, rid, path, **kw) -> str:
        eid = self.cat.file(rid, path, size=kw.pop("size", 50), **kw)
        return f"e:{eid}"

    def plan(self, keys, remote="gdrive", path="Dest", **kw):
        return gather.plan_gather(self.conn, self.draft, keys, {"remote": remote, "path": path}, **kw)

    def run(self, keys, remote="gdrive", path="Dest", **kw):
        return gather.gather(self.conn, self.draft, keys, {"remote": remote, "path": path}, created_by="test", **kw)


@pytest.fixture
def w(conn, catalog):
    world = W(conn, catalog)
    catalog.dir(world.g, "Dest")
    catalog.dir(world.d, "Dest")
    return world


def codes(plan):
    return {s["key"]: s["code"] for s in plan["skipped"]["items"]}


def moves(plan):
    return {p["key"]: (p["from"]["remote"], p["from"]["path"], p["to"]["path"], p["cross_cloud"])
            for p in plan["picked"]}


# --------------------------------------------------------------------------- the basic pick

def test_the_copy_on_the_destination_cloud_moves_flat(w):
    k = w.content((w.g, "Downloads/thesis-v9.pdf"), (w.d, "x/thesis-v9.pdf"), size=4100)
    out = w.plan([k])
    assert (out["contents"], out["files"], out["bytes"]) == (1, 1, 4100)
    assert out["cross_cloud"] == {"files": 0, "bytes": 0}
    assert out["picked"] == [{"key": k, "from": {"remote": "gdrive", "path": "Downloads/thesis-v9.pdf"},
                              "to": {"remote": "gdrive", "path": "Dest/thesis-v9.pdf"}, "size": 4100,
                              "cross_cloud": False}]
    assert out["skipped"] == {"by_code": {}, "items": []}
    assert out["items"] == [{"remote": "gdrive", "path": "Downloads/thesis-v9.pdf"}]


def test_plan_is_read_only(w):
    k = w.content((w.g, "a/f.txt"))
    before = w.conn.total_changes
    version = ws.get_draft(w.conn, w.draft)["version"]
    w.plan([k])
    assert w.conn.total_changes == before and ws.get_draft(w.conn, w.draft)["version"] == version


def test_keeper_policy_chooses_among_copies_on_the_destination(w):
    k = w.content((w.g, "deep/er/f.bin"), (w.g, "f (1).bin"), (w.g, "shallow.bin"), (w.d, "zz/f.bin"))
    assert moves(w.plan([k]))[k][:2] == ("gdrive", "shallow.bin")           # a copy-looking name loses, then depth
    k2 = w.content((w.g, "a/x/same.bin"), (w.g, "b/same.bin"), mtime="2020-01-01T00:00:00Z")
    assert moves(w.plan([k2]))[k2][1] == "b/same.bin"                        # shallower beats the rest


def test_prefer_remotes_decides_between_other_clouds(w):
    k = w.content((w.d, "z/f.bin"), (w.o, "z/f.bin"))
    assert moves(w.plan([k], cross_cloud=True))[k][0] == "onedrive"            # provider order: onedrive > dropbox
    assert moves(w.plan([k], cross_cloud=True, prefer_remotes=["dropbox"]))[k][0] == "dropbox"


def test_the_destination_cloud_wins_even_when_prefer_remotes_names_another(w):
    k = w.content((w.d, "z/f.bin"), (w.g, "q/q/f.bin"))
    assert moves(w.plan([k], cross_cloud=True, prefer_remotes=["dropbox"]))[k][0] == "gdrive"


def test_destination_may_be_the_remote_root(w):
    k = w.content((w.g, "a/f.txt"))
    out = w.plan([k], path="")
    assert out["picked"][0]["to"] == {"remote": "gdrive", "path": "f.txt"}


# --------------------------------------------------------------------------- skip codes, each at its step

def test_gone(w):
    out = w.plan(["g:9999", "e:8888"])
    assert out["skipped"]["by_code"] == {"GONE": 2} and out["files"] == 0
    assert out["skipped"]["items"][0] == {"key": "g:9999", "name": None, "code": "GONE", "params": {}}


def test_already_there_judges_the_after_view_parent(w):
    k = w.content((w.g, "Dest/thesis.pdf"), (w.d, "o/thesis.pdf"))
    out = w.plan([k])
    assert codes(out) == {k: "ALREADY_THERE"}
    assert out["skipped"]["items"][0]["params"] == {"name": "thesis.pdf"}
    # a copy on another cloud inside a folder of the same name is not "there"
    k2 = w.content((w.d, "Dest/other.pdf"), (w.g, "a/other.pdf"))
    assert moves(w.plan([k2]))[k2][1] == "a/other.pdf"
    # a copy "there" under another name still means the content is there
    k3 = w.content((w.g, "Dest/renamed.pdf"), (w.g, "a/orig.pdf"))
    assert codes(w.plan([k3])) == {k3: "ALREADY_THERE"}
    assert w.plan([k3])["skipped"]["items"][0]["params"] == {"name": "renamed.pdf"}


def test_already_there_comes_before_not_on_target(w):
    k = w.content((w.d, "Dest/a.txt"))                      # on dropbox, destination is on dropbox: already there
    assert codes(w.plan([k], remote="dropbox")) == {k: "ALREADY_THERE"}


def test_not_on_target_without_the_option_and_moved_with_it(w):
    k = w.content((w.d, "School/hw1.pdf"))
    out = w.plan([k])
    assert codes(out) == {k: "NOT_ON_TARGET"}
    assert out["skipped"]["items"][0]["params"] == {"remote": "gdrive"}
    out = w.plan([k], cross_cloud=True)
    assert moves(out)[k] == ("dropbox", "School/hw1.pdf", "Dest/hw1.pdf", True)
    assert out["cross_cloud"] == {"files": 1, "bytes": 100} and out["files"] == 1


def test_native_documents_never_cross_clouds(w):
    k = w.content((w.d, "docs/note"))
    w.conn.execute("UPDATE entries SET is_native_doc = 1 WHERE content_group = ?", (int(k[2:]),))
    out = w.plan([k], cross_cloud=True)
    assert codes(out) == {k: "NATIVE_DOC"} and out["skipped"]["items"][0]["params"] == {}
    # with a plain copy next to it, the plain copy goes
    k2 = w.content((w.d, "docs/b"), (w.o, "docs/b"))
    w.conn.execute("UPDATE entries SET is_native_doc = 1 WHERE remote_id = ? AND path = 'docs/b'", (w.o,))
    assert moves(w.plan([k2], cross_cloud=True))[k2][0] == "dropbox"


def test_read_only_clouds(w):
    k = w.content((w.ro, "old/f.txt"))
    out = w.plan([k], cross_cloud=True)
    assert codes(out) == {k: "READ_ONLY"} and out["skipped"]["items"][0]["params"] == {"remote": "archive"}
    # a read-only copy is not picked when a writable one exists
    k2 = w.content((w.ro, "old/g.txt"), (w.d, "x/g.txt"))
    assert moves(w.plan([k2], cross_cloud=True))[k2][0] == "dropbox"
    # a read-only destination takes nothing
    k3 = w.content((w.ro, "Dest2/h.txt"))
    w.cat.dir(w.ro, "Into")
    out = w.plan([k3, k2], remote="archive", path="Into", cross_cloud=True)
    assert out["files"] == 0 and set(codes(out).values()) == {"READ_ONLY"}


def test_ambiguous_drive_paths(w):
    a = w.cat.file(w.g, "twin/f.txt", size=5, provider_id="one")
    b = w.cat.file(w.g, "twin/f.txt", size=6, provider_id="two")          # two live entries at one path
    w.conn.execute("UPDATE entries SET content_group = 1 WHERE id = ?", (a,))
    out = w.plan(["g:1"])
    assert codes(out) == {"g:1": "AMBIGUOUS_PATH"}
    assert out["skipped"]["items"][0]["params"] == {"remote": "gdrive", "path": "twin/f.txt"}
    assert b
    k = w.content((w.g, "twin/f.txt"), (w.g, "ok/f.txt"))                    # the clear copy is used instead
    assert moves(w.plan([k]))[k][1] == "ok/f.txt"


def test_name_taken_by_a_file_or_folder_in_the_destination(w):
    w.cat.file(w.g, "Dest/report.pdf", size=1)
    w.cat.dir(w.g, "Dest/sub")
    k = w.content((w.g, "a/report.pdf"))
    k2 = w.content((w.g, "a/sub"))
    out = w.plan([k, k2])
    assert codes(out) == {k: "NAME_TAKEN", k2: "NAME_TAKEN"}
    assert out["skipped"]["items"][0]["params"] == {"name": "report.pdf"}


def test_name_clash_between_two_selected_contents(w):
    k1 = w.content((w.g, "a/notes.txt"), size=10)
    k2 = w.content((w.g, "b/notes.txt"), size=20)
    k3 = w.content((w.g, "c/notes.txt"), size=30)
    out = w.plan([k2, k3, k1])                                              # processed in natural key order
    assert [p["key"] for p in out["picked"]] == [k1]
    assert out["skipped"]["by_code"] == {"NAME_CLASH": 2}
    item = out["skipped"]["items"][0]
    assert item["key"] == k2 and item["params"] == {"name": "notes.txt", "with": {"remote": "gdrive",
                                                                                   "path": "a/notes.txt"}}


def test_names_are_compared_ignoring_case_on_a_case_insensitive_destination(w):
    k1 = w.content((w.d, "a/Notes.txt"), size=10)
    k2 = w.content((w.d, "b/notes.TXT"), size=20)
    k3 = w.content((w.d, "c/NOTES.txt"), size=30)
    w.cat.file(w.d, "Dest/other.txt", size=1)
    k4 = w.content((w.d, "d/OTHER.txt"), size=40)
    out = w.plan([k1, k2, k3, k4], remote="dropbox")
    assert [p["key"] for p in out["picked"]] == [k1]
    assert out["skipped"]["by_code"] == {"NAME_CLASH": 2, "NAME_TAKEN": 1}
    assert codes(out)[k4] == "NAME_TAKEN"
    assert w.plan([k1, k3], remote="dropbox")["skipped"]["by_code"] == {"NAME_CLASH": 1}
    # a case-sensitive destination keeps both spellings
    g1 = w.content((w.g, "a/Notes.txt"), size=10)
    g2 = w.content((w.g, "b/notes.txt"), size=20)
    assert w.plan([g1, g2])["files"] == 2


def test_already_there_ignores_case_on_a_case_insensitive_cloud(w):
    k = w.content((w.d, "dest/a.txt"))                                       # 'dest' vs the folder 'Dest'
    assert codes(w.plan([k], remote="dropbox", path="Dest")) == {k: "ALREADY_THERE"}
    kg = w.content((w.g, "dest/a.txt"))
    assert moves(w.plan([kg]))[kg][1] == "dest/a.txt"                         # case-sensitive: a different folder


# --------------------------------------------------------------------------- the draft's after-view

def test_a_copy_the_draft_already_moved_is_staged_from_where_the_draft_put_it(w):
    k = w.content((w.g, "a/f.txt"))
    ws.add_action(w.conn, w.draft, {"kind": "move", "items": [{"remote": "gdrive", "path": "a/f.txt"}],
                                    "dst": {"remote": "gdrive", "path": "Dest"}}, created_by="t")
    assert codes(w.plan([k])) == {k: "ALREADY_THERE"}                         # the draft already put it there
    k2 = w.content((w.g, "b/g.txt"))
    w.cat.dir(w.g, "Elsewhere")
    ws.add_action(w.conn, w.draft, {"kind": "move", "items": [{"remote": "gdrive", "path": "b/g.txt"}],
                                    "dst": {"remote": "gdrive", "path": "Elsewhere"}}, created_by="t")
    out = w.plan([k2])
    assert moves(out)[k2][:3] == ("gdrive", "Elsewhere/g.txt", "Dest/g.txt")
    assert out["items"] == [{"remote": "gdrive", "path": "Elsewhere/g.txt"}]
    w.run([k2])
    assert ws.get_draft(w.conn, w.draft)["summary"]["moved_files"] == 2


def test_a_folder_staged_by_mkdir_is_a_destination_and_names_there_are_taken(w):
    ws.add_action(w.conn, w.draft, {"kind": "mkdir", "dst": {"remote": "gdrive", "path": ""}, "new_name": "Fresh"},
                  created_by="t")
    k = w.content((w.g, "a/f.txt"))
    out = w.plan([k], path="Fresh")
    assert out["picked"][0]["to"]["path"] == "Fresh/f.txt"
    w.run([k], path="Fresh")
    k2 = w.content((w.g, "b/f.txt"), size=7)
    assert codes(w.plan([k2], path="Fresh")) == {k2: "NAME_TAKEN"}            # the earlier gather occupies the name


def test_a_copy_moved_out_of_the_destination_is_no_longer_there(w):
    k = w.content((w.g, "Dest/f.txt"), (w.g, "z/f.txt"))
    w.cat.dir(w.g, "Away")
    ws.add_action(w.conn, w.draft, {"kind": "move", "items": [{"remote": "gdrive", "path": "Dest/f.txt"}],
                                    "dst": {"remote": "gdrive", "path": "Away"}}, created_by="t")
    out = w.plan([k])
    assert [p["key"] for p in out["picked"]] == [k] and out["skipped"]["items"] == []


# --------------------------------------------------------------------------- destination and draft refusals

def refusal(fn, *a, **kw) -> WorkspaceError:
    with pytest.raises(WorkspaceError) as e:
        fn(*a, **kw)
    return e.value


def test_destination_refusals(w):
    k = w.content((w.g, "a/f.txt"))
    w.cat.file(w.g, "afile", size=1)
    e = refusal(w.plan, [k], remote="nowhere")
    assert (e.code, e.params) == ("NOT_FOUND", {"remote": "nowhere"})
    assert refusal(w.plan, [k], path="Missing").code == "DST_NOT_DIR"
    assert refusal(w.plan, [k], path="afile").code == "DST_NOT_DIR"
    assert refusal(w.plan, [k], path=QUARANTINE).code == "QUARANTINE"
    assert refusal(w.plan, [k], path=f"{QUARANTINE}/deeper").code == "QUARANTINE"
    assert refusal(gather.plan_gather, w.conn, w.draft, [k], "gdrive:Dest").code == "BAD_REQUEST"
    assert refusal(gather.plan_gather, w.conn, w.draft, ["bogus"], {"remote": "gdrive", "path": "Dest"}).code == \
        "BAD_REQUEST"


def test_the_draft_must_exist_and_be_open(w):
    k = w.content((w.g, "a/f.txt"))
    assert refusal(gather.plan_gather, w.conn, 99, [k], {"remote": "gdrive", "path": "Dest"}).code == "NOT_FOUND"
    ws.discard_draft(w.conn, w.draft)
    assert refusal(w.plan, [k]).code == "NOT_OPEN"
    assert refusal(w.run, [k]).code == "NOT_OPEN"


# --------------------------------------------------------------------------- staging

def test_gather_stages_one_move_action_with_every_item_and_one_undo_removes_it(w):
    keys = [w.content((w.g, f"src{i}/file{i}.txt"), size=10 + i) for i in range(5)]
    out = w.run(keys, note=None)
    assert out["files"] == 5 and "items" not in out and out["warnings"] == []
    d = ws.get_draft(w.conn, w.draft)
    assert len(d["actions"]) == 1
    action = d["actions"][0]
    assert action["kind"] == "move" and action["note"] == "explore:gather" and action["created_by"] == "test"
    assert action["dst"] == {"remote": "gdrive", "path": "Dest"} and len(action["items"]) == 5
    assert out["action"]["id"] == action["id"] and out["draft_version"] == d["version"]
    assert d["summary"]["moved_files"] == 5
    after = ws.undo(w.conn, w.draft)
    assert after["actions"][0]["state"] == "undone" and after["summary"]["moved_files"] == 0


def test_a_custom_note_and_the_cross_cloud_warning(w):
    k = w.content((w.d, "School/hw1.pdf"))
    out = w.run([k], cross_cloud=True, note="hand-in")
    assert out["action"]["note"] == "hand-in"
    assert [x["code"] for x in out["warnings"]] == ["CROSS_REMOTE"] and out["cross_cloud"]["files"] == 1


def test_at_most_one_copy_per_content_and_the_others_stay(w):
    k = w.content((w.g, "a/f.bin"), (w.g, "b/f.bin"), (w.g, "c/f.bin"), (w.d, "d/f.bin"))
    w.run([k])
    action = ws.get_draft(w.conn, w.draft)["actions"][0]
    assert len(action["items"]) == 1
    assert ws.get_draft(w.conn, w.draft)["summary"]["moved_files"] == 1


def test_nothing_to_gather(w):
    k = w.content((w.d, "x/f.txt"))
    e = refusal(w.run, [k, "g:424242"])
    assert e.code == "NOTHING_TO_GATHER"
    assert e.params == {"contents": 2, "skipped": {"NOT_ON_TARGET": 1, "GONE": 1}}
    assert ws.get_draft(w.conn, w.draft)["actions"] == []


def test_gather_too_many(w, monkeypatch):
    monkeypatch.setattr(gather, "GATHER_LIMIT", 3)
    keys = [w.content((w.g, f"s/f{i}.txt")) for i in range(4)]
    e = refusal(w.plan, keys)
    assert (e.code, e.params) == ("GATHER_TOO_MANY", {"files": 4, "limit": 3})
    assert refusal(w.run, keys).code == "GATHER_TOO_MANY"
    assert ws.get_draft(w.conn, w.draft)["actions"] == []
    assert w.plan(keys[:3])["files"] == 3
    assert gather.GATHER_LIMIT == 3 and gather.CODES == ("NOTHING_TO_GATHER", "GATHER_TOO_MANY")


def test_picked_and_skipped_items_are_capped_but_counts_are_exact(w):
    keys = [w.content((w.g, f"s/f{i}.txt")) for i in range(230)]
    skips = [w.single(w.d, f"t/g{i}.txt") for i in range(230)]
    out = w.plan(keys + skips)
    assert out["files"] == 230 and len(out["picked"]) == 200
    assert out["skipped"]["by_code"] == {"NOT_ON_TARGET": 230} and len(out["skipped"]["items"]) == 200
    assert len(out["items"]) == 230


def test_a_catalog_change_between_plan_and_stage_is_a_coded_refusal_not_a_partial_action(w, monkeypatch):
    k1 = w.content((w.g, "a/one.txt"))
    k2 = w.content((w.g, "a/two.txt"))
    real = gather.plan_gather

    def plan_then_rescan(*a, **kw):
        out = real(*a, **kw)
        w.conn.execute("UPDATE entries SET present = 0 WHERE path = 'a/two.txt'")        # the file vanishes
        return out

    monkeypatch.setattr(gather, "plan_gather", plan_then_rescan)
    e = refusal(w.run, [k1, k2])
    assert e.code == "SRC_NOT_FOUND"
    assert ws.get_draft(w.conn, w.draft)["actions"] == []


def test_the_order_of_a_gather_is_natural_key_order(w):
    a = w.content((w.g, "p/z.txt"))
    b = w.content((w.g, "p/a.txt"))
    out = w.plan([b, a, b])
    assert [p["key"] for p in out["picked"]] == [a, b] and out["contents"] == 2


def test_two_thousand_keys_plan_and_stage_quickly(conn, catalog):
    world = W(conn, catalog)
    catalog.dir(world.g, "Dest")
    rows = []
    for i in range(2000):
        rows.append((world.g, f"src/{i % 40}/file{i}.bin"))
    keys = []
    for n, (rid, path) in enumerate(rows, 1):
        eid = catalog.file(rid, path, size=10 + n)
        conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (n, eid))
        if n % 3 == 0:                                   # a second copy elsewhere for a third of them
            other = catalog.file(world.d, f"mirror/{n}/file{n}.bin", size=10 + n)
            conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (n, other))
        keys.append(f"g:{n}")
    start = time.perf_counter()
    plan = world.plan(keys)
    staged = world.run(keys)
    elapsed = time.perf_counter() - start
    assert plan["files"] == 2000 and staged["files"] == 2000
    assert len(ws.get_draft(conn, world.draft)["actions"][0]["items"]) == 2000
    assert elapsed < 5, elapsed


def test_gather_never_writes_the_catalog(w):
    k = w.content((w.g, "a/f.txt"))
    entries = w.conn.execute("SELECT id, path, present, row_version FROM entries ORDER BY id").fetchall()
    w.run([k])
    assert w.conn.execute("SELECT id, path, present, row_version FROM entries ORDER BY id").fetchall() == entries
