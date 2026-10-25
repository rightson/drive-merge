"""Tests for drivemerge.explore: grouping by content, renamed copies, presence filters, sorts, facets, truncation."""

from __future__ import annotations

import pytest

from drivemerge import db, search
from drivemerge.compare import CompareError
from drivemerge.explore import explore

QUARANTINE = db.DEFAULT_QUARANTINE_DIR


def group(conn, number, *ids):
    for i in ids:
        conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (number, i))


@pytest.fixture
def built(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    o = catalog.remote("onedrive", "onedrive")
    ids = {}
    ids["t1"] = catalog.file(g, "School/Thesis/thesis_final.pdf", size=4000, mtime="2023-06-01T00:00:00Z")
    ids["t2"] = catalog.file(d, "School (1)/thesis_final.pdf", size=4000, mtime="2023-05-28T00:00:00Z")
    ids["t3"] = catalog.file(d, "Backup/v9.pdf", size=4000, mtime="2023-07-01T00:00:00Z")      # renamed copy
    ids["t4"] = catalog.file(o, f"{QUARANTINE}/thesis_final.pdf", size=4000)                    # parked: not a copy
    group(conn, 1, ids["t1"], ids["t2"], ids["t3"], ids["t4"])
    ids["draft"] = catalog.file(g, "School/thesis_draft.docx", size=880, mtime="2022-11-12T00:00:00Z")
    ids["pic"] = catalog.file(g, "Photos/beach.jpg", size=100, mtime="2024-01-01T00:00:00Z")
    ids["pic2"] = catalog.file(o, "Pics/beach.jpg", size=100, mtime="2024-01-02T00:00:00Z")
    group(conn, 2, ids["pic"], ids["pic2"])
    ids["solo"] = catalog.file(d, "Misc/notes.txt", size=10)                                    # ungrouped
    ids["e1"] = catalog.file(g, "empty1.txt", size=0, md5="e")
    ids["e2"] = catalog.file(d, "empty2.txt", size=0, md5="e")
    group(conn, 3, ids["e1"], ids["e2"])
    catalog.file(g, "School/old.txt", size=1, present=False)
    return conn, ids


@pytest.fixture
def world(built):
    return built[0]


@pytest.fixture
def ids(built):
    return built[1]


def names(res):
    return [r["name"] for r in res["results"]]


def test_grouping_with_renamed_copies_marked_unmatched(world):
    res = explore(world, "thesis_final")
    assert res["total"] == 1 and not res["truncated"]
    r = res["results"][0]
    assert r["key"] == "g:1" and r["verified"] is True and r["size"] == 4000
    assert r["category"] == "pdf" and r["ext"] == "pdf"
    assert r["remotes"] == ["gdrive", "dropbox"] and r["copies_count"] == 3        # quarantine excluded
    assert [(c["remote"], c["path"], c["matched"]) for c in r["copies"]] == [
        ("gdrive", "School/Thesis/thesis_final.pdf", True),
        ("dropbox", "Backup/v9.pdf", False),                                       # the renamed copy is listed anyway
        ("dropbox", "School (1)/thesis_final.pdf", True)]
    assert set(r["copies"][0]) == {"entry_id", "remote", "path", "name", "mtime", "matched"}
    assert r["mtime"] == "2023-07-01T00:00:00Z"                                     # newest copy
    assert r["name"] == "thesis_final.pdf"


def test_a_query_matching_only_a_renamed_copy_still_shows_the_whole_content(world):
    r = explore(world, "v9.pdf")["results"][0]
    assert r["copies_count"] == 3 and r["name"] == "v9.pdf"
    assert sum(c["matched"] for c in r["copies"]) == 1


def test_ungrouped_file_stands_alone_and_is_not_verified(world, ids):
    r = explore(world, "notes.txt")["results"][0]
    assert r["key"] == f"e:{ids['solo']}" and r["verified"] is False and r["copies_count"] == 1


def test_empty_files_are_not_merged_into_one_content(world):
    res = explore(world, "empty")
    assert res["total"] == 2 and all(not r["verified"] and r["copies_count"] == 1 for r in res["results"])


def test_hits_are_the_same_files_search_finds_minus_quarantine(world):
    for q in ("thesis", "pdf", "a", "School", None):
        found = {e["id"] for e in search.search(world, q, limit=1000) if not e["path"].startswith(QUARANTINE)}
        got = {c["entry_id"] for r in explore(world, q, limit=200)["results"] for c in r["copies"] if c["matched"]}
        assert got == found, q


@pytest.mark.parametrize("kw", [
    {"category": "document"}, {"ext": ".PDF"}, {"min_size": 100}, {"max_size": 100}, {"min_size": 5, "max_size": 900},
    {"modified_after": "2023-06-01T00:00:00Z"}, {"modified_before": "2023-06-01T00:00:00Z"},
    {"path_prefix": "School"}, {"path_prefix": "/School (1)/"},
])
def test_attribute_filters_use_the_search_rules(world, kw):
    found = {e["id"] for e in search.search(world, None, limit=1000, **kw) if not e["path"].startswith(QUARANTINE)}
    got = {c["entry_id"] for r in explore(world, None, limit=200, **kw)["results"] for c in r["copies"] if c["matched"]}
    assert got == found and found


def test_in_remote_restricts_hits_not_copies(world):
    res = explore(world, "thesis_final", in_remote="dropbox")
    r = res["results"][0]
    assert r["copies_count"] == 3 and sum(c["matched"] for c in r["copies"]) == 1
    assert {c["remote"] for c in r["copies"] if c["matched"]} == {"dropbox"}
    assert explore(world, "beach", in_remote="gdrive")["results"][0]["remotes"] == ["gdrive", "onedrive"]


def test_presence_filters_judge_the_full_copy_set(world):
    def keys(**kw):
        return sorted(r["name"] for r in explore(world, None, limit=200, **kw)["results"])
    assert keys(on=["onedrive"]) == ["beach.jpg"]
    assert keys(on=["gdrive", "dropbox"]) == ["thesis_final.pdf"]
    assert keys(not_on=["gdrive"]) == ["empty2.txt", "notes.txt"]
    assert keys(not_on=["dropbox", "onedrive"]) == ["empty1.txt", "thesis_draft.docx"]
    assert keys(only_on=["gdrive", "onedrive"]) == ["beach.jpg"]
    assert keys(only_on=["onedrive"]) == []
    assert keys(only_on=["dropbox", "gdrive"]) == ["thesis_final.pdf"]            # order of names does not matter
    assert keys(on=["gdrive"], not_on=["gdrive"]) == []                           # contradictory: nothing
    # a name query narrows hits, but presence still looks at every copy
    assert [r["name"] for r in explore(world, "v9", only_on=["gdrive", "dropbox"])["results"]] == ["v9.pdf"]


def test_copies_filters(world):
    def keys(**kw):
        return sorted(r["name"] for r in explore(world, None, limit=200, **kw)["results"])
    assert keys(copies_min=3) == ["thesis_final.pdf"]
    assert keys(copies_max=1) == ["empty1.txt", "empty2.txt", "notes.txt", "thesis_draft.docx"]
    assert keys(copies_min=2, copies_max=2) == ["beach.jpg"]
    assert keys(copies_min=2, copies_max=1) == []


def test_sorts(world):
    def order(sort, q=None):
        return [r["name"] for r in explore(world, q, sort=sort, limit=200)["results"]]
    assert order("name")[:3] == ["beach.jpg", "empty1.txt", "empty2.txt"]
    assert order("size")[:3] == ["thesis_final.pdf", "thesis_draft.docx", "beach.jpg"]
    assert order("mtime") == ["beach.jpg", "empty1.txt", "empty2.txt", "notes.txt", "thesis_final.pdf",
                              "thesis_draft.docx"]            # 2024-01-02, three equal (by name), 2023-07, 2022-11
    assert order("copies")[:2] == ["thesis_final.pdf", "beach.jpg"]
    # ties by name
    assert order("copies")[2:] == sorted(order("copies")[2:], key=str.casefold)
    # relevance without a full-text term falls back to name
    assert order("relevance") == order("name")


def test_mtime_sort_is_newest_first(world):
    res = explore(world, None, sort="mtime", limit=200)["results"]
    times = [r["mtime"] for r in res]
    assert times == sorted(times, reverse=True)


def test_relevance_uses_bm25_of_the_best_matching_copy(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "alpha beta.txt", size=1)
    catalog.file(g, "alpha.txt", size=2)
    catalog.file(g, "x/alpha/alpha_alpha.txt", size=3)
    expected = [e["name"] for e in search.search(conn, "alpha")]
    assert [r["name"] for r in explore(conn, "alpha")["results"]] == expected


def test_facets_cover_all_filtered_groups_before_pagination(world):
    res = explore(world, None, limit=2)
    assert len(res["results"]) == 2 and res["total"] == 6
    f = res["facets"]
    assert f["remotes"] == [{"name": "gdrive", "contents": 4}, {"name": "dropbox", "contents": 3},
                            {"name": "onedrive", "contents": 1}]
    assert f["copies"] == {"1": 4, "2": 1, "3+": 1}
    assert {c["category"]: c["contents"] for c in f["categories"]} == {"pdf": 1, "document": 4, "photo": 1}
    assert [c["category"] for c in f["categories"]][0] == "document"                    # most contents first
    assert {e["ext"]: e["contents"] for e in f["exts"]} == {"pdf": 1, "docx": 1, "jpg": 1, "txt": 3}
    narrowed = explore(world, "thesis")["facets"]
    assert narrowed["copies"] == {"1": 1, "2": 0, "3+": 1}


def test_top_twelve_exts(conn, catalog):
    g = catalog.remote("g", "drive")
    for i in range(15):
        catalog.file(g, f"f.e{i:02}", size=1)
    assert len(explore(conn)["facets"]["exts"]) == 12


def test_pagination_and_limit_cap(world):
    first = explore(world, None, sort="name", limit=3)
    second = explore(world, None, sort="name", limit=3, offset=3)
    allr = explore(world, None, sort="name", limit=200)
    assert names(first) + names(second) == names(allr)[:6] and first["total"] == 6
    assert explore(world, None, limit=100000)["total"] == 6
    assert explore(world, None, offset=50)["results"] == []


def test_truncated_when_more_hits_than_scan_cap(world):
    res = explore(world, "txt", scan_cap=2)
    assert res["truncated"] is True and res["total"] <= 2
    assert explore(world, "txt", scan_cap=50)["truncated"] is False
    n = len(search.search(world, "txt", limit=1000))
    assert explore(world, "txt", scan_cap=n)["truncated"] is False
    assert explore(world, "txt", scan_cap=n - 1)["truncated"] is True


def test_unknown_remote_names_are_not_found(world):
    for kw in ({"in_remote": "nope"}, {"on": ["nope"]}, {"not_on": ["gdrive", "nope"]}, {"only_on": ["nope"]}):
        with pytest.raises(CompareError) as exc:
            explore(world, None, **kw)
        assert (exc.value.code, exc.value.status, exc.value.params) == ("NOT_FOUND", 404, {"remote": "nope"})


@pytest.mark.parametrize("kw,param", [
    ({"sort": "bogus"}, "sort"), ({"limit": 0}, "limit"), ({"offset": -1}, "offset"),
    ({"copies_min": -1}, "copies_min"), ({"copies_max": "x"}, "copies_max"), ({"min_size": -5}, "min_size"),
    ({"scan_cap": 0}, "scan_cap")])
def test_bad_parameters(world, kw, param):
    with pytest.raises(CompareError) as exc:
        explore(world, None, **kw)
    assert (exc.value.code, exc.value.status, exc.value.params) == ("BAD_QUERY", 400, {"param": param})


def test_cjk_and_nfd_queries(conn, catalog):
    import unicodedata
    g = catalog.remote("g", "drive")
    catalog.file(g, "工作/報告.docx", size=1)
    catalog.file(g, f"{unicodedata.normalize('NFD', 'café')}/menu.txt", size=2)
    assert names(explore(conn, "報告")) == ["報告.docx"]
    assert names(explore(conn, "café")) == ["menu.txt"]


def test_search_results_are_unchanged_by_the_shared_builder(world):
    # include_missing / include_dirs / duplicates_only still work through search.search
    assert any(not e["present"] for e in search.search(world, "old", include_missing=True))
    assert search.search(world, "old") == []
    assert {e["name"] for e in search.search(world, None, duplicates_only=True)} >= {"beach.jpg", "thesis_final.pdf"}
    # search itself still lists quarantined files (its callers filter); explore never does
    assert any(e["path"].startswith(QUARANTINE) for e in search.search(world, "thesis_final"))


def test_explore_does_not_write(world):
    before = world.total_changes
    explore(world, "thesis")
    assert world.total_changes == before


def test_draft_locate_marks_copies_and_leaves_everything_else_alone(world, ids):
    plain = explore(world, "thesis_final")
    seen = []

    def locate(locs):
        seen.append(list(locs))
        return [(r, "Gathered/" + p.rpartition("/")[2]) if r == 1 else (r, p) for r, p in locs]

    marked = explore(world, "thesis_final", draft_locate=locate)
    assert len(seen) == 1 and len(seen[0]) == plain["results"][0]["copies_count"]      # one call for the whole page
    r = marked["results"][0]
    assert r["staged"] == 1
    assert [c["staged"] for c in r["copies"]] == [
        {"remote": "gdrive", "path": "Gathered/thesis_final.pdf"}, None, None]
    for key in ("total", "truncated", "facets"):
        assert marked[key] == plain[key]
    assert explore(world, "zzzzzz", draft_locate=locate)["results"] == []
