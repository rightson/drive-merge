"""Selections of Explore results (docs/BULK.md §B): both modes, every validation code in order, the numbers, read-only."""

from __future__ import annotations

import pytest

from drivemerge import db
from drivemerge.compare import CompareError
from drivemerge.explore import (PICK_LIMIT, SELECT_LIMIT, SELECTION_CODES, SelectionError, explore, select)

QUARANTINE = db.DEFAULT_QUARANTINE_DIR


def group(conn, number, *ids):
    for i in ids:
        conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (number, i))


@pytest.fixture
def world(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    o = catalog.remote("onedrive", "onedrive")
    ids = {}
    ids["t1"] = catalog.file(g, "School/thesis_final.pdf", size=4000)
    ids["t2"] = catalog.file(d, "School/thesis_final.pdf", size=4000)
    ids["t3"] = catalog.file(g, "Downloads/thesis-v9.pdf", size=4000)
    ids["t4"] = catalog.file(o, f"{QUARANTINE}/thesis_final.pdf", size=4000)
    group(conn, 1, ids["t1"], ids["t2"], ids["t3"], ids["t4"])
    ids["p1"] = catalog.file(g, "Photos/beach.jpg", size=100)
    ids["p2"] = catalog.file(o, "Pics/beach.jpg", size=100)
    group(conn, 2, ids["p1"], ids["p2"])
    ids["solo"] = catalog.file(d, "Misc/notes.txt", size=10)
    ids["e1"] = catalog.file(g, "empty1.txt", size=0, md5="e")
    ids["e2"] = catalog.file(d, "empty2.txt", size=0, md5="e")
    group(conn, 3, ids["e1"], ids["e2"])
    conn.commit()
    return conn, ids


def err(sel, conn, **kw) -> CompareError:
    with pytest.raises(CompareError) as e:
        select(conn, sel, **kw)
    return e.value


# ------------------------------------------------------------------------------------------ both modes

def test_keys_mode_numbers(world):
    conn, ids = world
    out = select(conn, {"keys": ["g:1", "g:2", f"e:{ids['solo']}"]}, with_keys=True)
    assert out["mode"] == "keys" and out["total"] == 3 and out["grouped"] == 2 and out["ungrouped"] == 1
    assert out["gone"] == 0 and out["staged"] is None
    assert out["copies"] == 3 + 2 + 1                      # the quarantined copy is not counted
    assert out["bytes"] == 4000 + 100 + 10                 # each content once
    assert out["redundant"] == {"copies": 2 + 1, "bytes": 2 * 4000 + 100}
    assert out["by_remote"] == [
        {"remote": "gdrive", "contents": 2, "copies": 3, "bytes": 8100},
        {"remote": "dropbox", "contents": 2, "copies": 2, "bytes": 4010},
        {"remote": "onedrive", "contents": 1, "copies": 1, "bytes": 100}]
    assert out["keys"] == ["g:1", "g:2", f"e:{ids['solo']}"]


def test_every_remote_is_listed_even_with_zeros_and_keys_only_on_request(world):
    conn, _ = world
    out = select(conn, {"keys": ["g:1"]})
    assert "keys" not in out
    assert [r["remote"] for r in out["by_remote"]] == ["gdrive", "dropbox", "onedrive"]
    assert out["by_remote"][2] == {"remote": "onedrive", "contents": 0, "copies": 0, "bytes": 0}


def test_query_mode_matches_explore(world):
    conn, _ = world
    out = select(conn, {"query": {"q": "thesis"}}, with_keys=True)
    assert out["mode"] == "query" and out["total"] == 1 and out["keys"] == ["g:1"]
    assert out["copies"] == explore(conn, "thesis")["results"][0]["copies_count"] == 3
    out = select(conn, {"query": {"in": "dropbox", "copies_min": 2}}, with_keys=True)
    assert out["keys"] == ["g:1"]
    assert select(conn, {"query": {"only_on": ["dropbox"]}}, with_keys=True)["total"] == 2   # notes + empty2
    assert err({"query": {"not_on": ["gdrive"], "on": ["onedrive"]}}, conn).code == "SELECTION_EMPTY"


def test_a_query_matching_nothing_is_empty(world):
    conn, _ = world
    assert err({"query": {"q": "zzzzzz"}}, conn).code == "SELECTION_EMPTY"


def test_except_removes_keys(world):
    conn, _ = world
    # only g:1 is a pdf: excluding it leaves nothing
    assert err({"query": {"category": "pdf"}, "except": ["g:1"]}, conn).code == "SELECTION_EMPTY"
    out = select(conn, {"query": {"ext": "jpg"}, "except": ["g:99"]}, with_keys=True)
    assert out["keys"] == ["g:2"]                          # an unknown excluded key is harmless
    out = select(conn, {"query": {"q": "e"}, "except": ["g:1", "g:2"]}, with_keys=True)
    assert "g:1" not in out["keys"] and "g:2" not in out["keys"]


def test_gone_keys_are_counted_in_keys_mode(world):
    conn, ids = world
    out = select(conn, {"keys": ["g:1", "g:404", "e:4040", "g:1"]})
    assert out["total"] == 1 and out["gone"] == 2           # a duplicate key counts once
    conn.execute("UPDATE entries SET present = 0 WHERE id = ?", (ids["solo"],))
    assert select(conn, {"keys": ["g:1", f"e:{ids['solo']}"]})["gone"] == 1


def test_e_keys_for_empty_and_ungrouped_files(world):
    conn, ids = world
    out = select(conn, {"keys": [f"e:{ids['e1']}", f"e:{ids['e2']}", f"e:{ids['solo']}"]}, with_keys=True)
    assert out["total"] == 3 and out["ungrouped"] == 3 and out["copies"] == 3 and out["bytes"] == 10
    # an e: key for a file that explore keys as g:N is not that content
    assert err({"keys": [f"e:{ids['t1']}"]}, conn).code == "SELECTION_EMPTY"
    # empty files are their own contents in query mode as well
    out = select(conn, {"query": {"q": "empty"}}, with_keys=True)
    assert sorted(out["keys"]) == sorted([f"e:{ids['e1']}", f"e:{ids['e2']}"])


def test_quarantined_copies_are_never_counted(world):
    conn, ids = world
    one = select(conn, {"keys": ["g:1"]})
    assert one["copies"] == 3
    conn.execute("UPDATE entries SET path = ? WHERE id = ?", (f"{QUARANTINE}/x.pdf", ids["t3"]))
    assert select(conn, {"keys": ["g:1"]})["copies"] == 2
    # a content whose every copy is parked is gone
    for k in ("t1", "t2"):
        conn.execute("UPDATE entries SET path = ? WHERE id = ?", (f"{QUARANTINE}/{k}.pdf", ids[k]))
    assert err({"keys": ["g:1"]}, conn).code == "SELECTION_EMPTY"


def test_keys_come_in_natural_order(world):
    conn, ids = world
    out = select(conn, {"keys": [f"e:{ids['solo']}", "g:2", "g:1"]}, with_keys=True)
    assert out["keys"] == ["g:1", "g:2", f"e:{ids['solo']}"]


# ------------------------------------------------------------------------------------------ validation

@pytest.mark.parametrize("sel,param", [
    ({}, "query"), ({"query": {}, "keys": []}, "keys"), ({"keys": ["g:1"], "except": []}, "except"),
    ({"keys": ["g:1"], "expect_total": 1}, "expect_total"), ({"keys": ["g:1"], "bogus": 1}, "bogus"),
    ({"query": {"sort": "size"}}, "sort"), ({"query": {"limit": 5}}, "limit"), ({"query": {"offset": 1}}, "offset"),
    ({"query": {"nope": 1}}, "nope"), ({"query": "x"}, "query"), ({"keys": "g:1"}, "keys"),
    ({"keys": [1]}, "keys"), ({"query": {}, "except": "g:1"}, "except"), ({"query": {}, "except": [3]}, "except"),
    ({"query": {}, "expect_total": -1}, "expect_total"), ({"query": {}, "expect_total": True}, "expect_total"),
    ({"query": {}, "expect_total": "3"}, "expect_total"),
])
def test_bad_selection_names_the_field(world, sel, param):
    e = err(sel, world[0])
    assert e.code == "BAD_SELECTION" and e.params == {"param": param}
    assert e.status == 400


def test_a_selection_must_be_an_object(world):
    assert err([], world[0]).code == "BAD_SELECTION"


def test_too_many_keys(world):
    conn, _ = world
    e = err({"keys": [f"g:{i}" for i in range(1, PICK_LIMIT + 2)]}, conn)
    assert (e.code, e.params, e.status) == ("TOO_MANY_KEYS", {"param": "keys", "count": 501, "limit": 500}, 400)
    e = err({"query": {}, "except": [f"g:{i}" for i in range(1, PICK_LIMIT + 2)]}, conn)
    assert e.code == "TOO_MANY_KEYS" and e.params["param"] == "except"
    assert select(conn, {"keys": ["g:1"] * PICK_LIMIT})["total"] == 1          # exactly the limit is fine


@pytest.mark.parametrize("bad", ["x:1", "g:0", "g:01", "g:", "g:-1", "g:1 ", "g:1234567890123456789", "G:1", "1"])
def test_bad_key(world, bad):
    e = err({"keys": ["g:1", bad, "nope"]}, world[0])
    assert (e.code, e.params, e.status) == ("BAD_KEY", {"key": bad}, 400)      # the first malformed one
    assert err({"query": {}, "except": [bad]}, world[0]).code == "BAD_KEY"


def test_order_shape_then_size_then_keys_then_query(world):
    conn, _ = world
    too_many = [f"g:{i}" for i in range(1, PICK_LIMIT + 2)]
    assert err({"keys": too_many + ["junk"], "x": 1}, conn).code == "BAD_SELECTION"
    assert err({"keys": too_many + ["junk"]}, conn).code == "TOO_MANY_KEYS"
    assert err({"keys": ["junk"]}, conn).code == "BAD_KEY"
    assert err({"query": {"in": "nowhere"}, "except": ["junk"]}, conn).code == "BAD_KEY"
    assert err({"query": {"in": "nowhere"}}, conn).code == "NOT_FOUND"


def test_query_params_are_validated_like_explore(world):
    conn, _ = world
    e = err({"query": {"in": "nowhere"}}, conn)
    assert (e.code, e.params, e.status) == ("NOT_FOUND", {"remote": "nowhere"}, 404)
    assert err({"query": {"on": ["nowhere"]}}, conn).code == "NOT_FOUND"
    for bad in ({"min_size": -1}, {"copies_min": "x"}, {"copies_max": True}, {"q": 5}, {"on": "gdrive"},
                {"only_on": [1]}, {"modified_after": 3}):
        e = err({"query": bad}, conn)
        assert e.code == "BAD_QUERY" and e.params == {"param": next(iter(bad))} and e.status == 400


def test_the_codes_list():
    assert SELECTION_CODES == ("BAD_SELECTION", "BAD_KEY", "TOO_MANY_KEYS", "SELECTION_EMPTY", "SELECTION_TOO_LARGE",
                               "SELECTION_TRUNCATED", "SELECTION_CHANGED")
    assert issubclass(SelectionError, CompareError)


# ------------------------------------------------------------------------------------------ resolution

def test_expect_total_and_selection_changed_after_a_rescan(world, catalog):
    conn, _ = world
    sel = {"query": {"category": "pdf"}, "expect_total": 1}
    assert select(conn, sel)["total"] == 1
    catalog.file(1, "School/new.pdf", size=77)                    # a rescan finds one more matching file
    e = err(sel, conn)
    assert (e.code, e.params, e.status) == ("SELECTION_CHANGED", {"expected": 1, "total": 2}, 409)
    assert select(conn, {**sel, "expect_total": 2})["total"] == 2


def test_expect_total_counts_after_exclusions(world, catalog):
    conn, _ = world
    catalog.file(1, "School/new.pdf", size=77)
    assert select(conn, {"query": {"category": "pdf"}, "except": ["g:1"], "expect_total": 1})["total"] == 1
    assert err({"query": {"category": "pdf"}, "except": ["g:1"], "expect_total": 2}, conn).code == "SELECTION_CHANGED"


def test_truncated_scan(world):
    conn, _ = world
    e = err({"query": {"q": "e"}}, conn, scan_cap=2)
    assert (e.code, e.params, e.status) == ("SELECTION_TRUNCATED", {"scan_cap": 2}, 409)
    assert select(conn, {"query": {"q": "thesis"}}, scan_cap=3)["total"] == 1       # 3 hits fit exactly


def test_too_large(world):
    conn, ids = world
    e = err({"query": {}}, conn, limit=2)
    assert (e.code, e.params, e.status) == ("SELECTION_TOO_LARGE", {"total": 5, "limit": 2}, 409)
    assert err({"keys": ["g:1", "g:2", f"e:{ids['solo']}"]}, conn, limit=2).code == "SELECTION_TOO_LARGE"
    assert SELECT_LIMIT == 5000


def test_changed_wins_over_too_large_and_empty_comes_last(world):
    conn, _ = world
    assert err({"query": {}, "expect_total": 99}, conn, limit=2).code == "SELECTION_CHANGED"
    assert err({"keys": ["g:404"]}, conn).code == "SELECTION_EMPTY"


# ------------------------------------------------------------------------------------------ read-only, explore unchanged

def test_select_writes_nothing_and_uses_few_queries(world):
    conn, ids = world
    before = conn.total_changes
    select(conn, {"keys": ["g:1", "g:2", f"e:{ids['solo']}"]}, with_keys=True)
    select(conn, {"query": {"category": "pdf"}})
    assert conn.total_changes == before
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    select(conn, {"keys": [f"g:{i}" for i in range(1, 400)] + [f"e:{i}" for i in range(1, 100)]})
    conn.set_trace_callback(None)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "FROM entries" in s]
    assert len(selects) <= 4                                          # one per kind and chunk, never one per key


def test_explore_output_is_unchanged_with_and_without_a_locator(world):
    conn, _ = world
    plain = explore(conn, "thesis")
    same = explore(conn, "thesis", draft_locate=lambda locs: list(locs))
    for r in same["results"]:
        assert r.pop("staged") == 0
        for c in r["copies"]:
            assert c.pop("staged") is None
    assert same == plain
    assert "staged" not in plain["results"][0]


def test_explore_marks_the_copies_a_locator_moves(world):
    conn, ids = world
    row = conn.execute("SELECT remote_id, path FROM entries WHERE id = ?", (ids["t1"],)).fetchone()

    def locate(locs):
        return [(r, "Gathered/x.pdf") if (r, p) == (row[0], row[1]) else (r, p) for r, p in locs]

    r = explore(conn, "thesis", draft_locate=locate)["results"][0]
    assert r["staged"] == 1
    marks = {c["entry_id"]: c["staged"] for c in r["copies"]}
    assert marks[ids["t1"]] == {"remote": "gdrive", "path": "Gathered/x.pdf"}
    assert marks[ids["t2"]] is None and marks[ids["t3"]] is None
