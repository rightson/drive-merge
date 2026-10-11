"""Tests for drivemerge.search: matching, filters, lookups, change feed and stats."""

from __future__ import annotations

import unicodedata

import pytest

from drivemerge import db, search


def _paths(rows):
    return [r["path"] for r in rows]


def _raw_insert(conn, rid, path, row_version, *, size=1):
    """Insert a live file with an explicit row_version (shares a batch stamp)."""
    scan = conn.execute("SELECT MAX(id) FROM scans WHERE remote_id = ?", (rid,)).fetchone()[0]
    name = path.rsplit("/", 1)[-1]
    return conn.execute(
        """INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category,
             is_dir, size, present, first_seen_scan, last_seen_scan, row_version)
           VALUES (?, ?, ?, ?, ?, ?, '', 'other', 0, ?, 1, ?, ?, ?) RETURNING id""",
        (rid, f"p:{path}", path, path.rpartition("/")[0], name, name.casefold(), size,
         scan, scan, row_version),
    ).fetchone()[0]


def test_substring_match_on_name_and_path(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "Photos/holiday_2024.jpg")
    catalog.file(g, "Docs/tax.pdf")
    assert _paths(search.search(conn, "liday_20")) == ["Photos/holiday_2024.jpg"]
    assert _paths(search.search(conn, "hotos")) == ["Photos/holiday_2024.jpg"]


def test_ascii_is_case_insensitive(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "Photos/IMG_0001.JPG")
    catalog.file(g, "Notes/Ab.txt")
    assert _paths(search.search(conn, "img_0001")) == ["Photos/IMG_0001.JPG"]
    assert _paths(search.search(conn, "AB")) == ["Notes/Ab.txt"]


def test_terms_are_anded_and_match_name_or_path(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "Trips/Italy/beach_sunset.jpg")
    catalog.file(g, "Trips/Italy/museum.jpg")
    catalog.file(g, "Trips/Spain/beach_day.jpg")
    assert _paths(search.search(conn, "beach sunset")) == ["Trips/Italy/beach_sunset.jpg"]
    assert _paths(search.search(conn, "ITALY beach")) == ["Trips/Italy/beach_sunset.jpg"]


def test_cjk_short_term_uses_like_and_long_term_uses_fts(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "工作/報告.docx")
    catalog.file(g, "工作/會議.docx")
    catalog.file(g, "工作/報告草稿.docx")
    catalog.file(g, "財務/年度財務報告.pdf")
    assert sorted(_paths(search.search(conn, "報告"))) == [
        "工作/報告.docx", "工作/報告草稿.docx", "財務/年度財務報告.pdf"]
    assert _paths(search.search(conn, "報告草")) == ["工作/報告草稿.docx"]
    assert _paths(search.search(conn, "財務報")) == ["財務/年度財務報告.pdf"]
    assert len(search.search(conn, "工作")) == 3


def test_short_term_matches_nfd_names(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "menu/Café.txt")
    assert _paths(search.search(conn, "é")) == ["menu/Café.txt"]


def test_wildcards_and_quotes_are_literal(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "p/50%off.txt")
    catalog.file(g, "p/50x off.txt")
    catalog.file(g, "p/a_b.txt")
    catalog.file(g, "p/axb.txt")
    catalog.file(g, 'p/say "hi".txt')
    catalog.file(g, "p/sayhi.txt")
    assert _paths(search.search(conn, "%")) == ["p/50%off.txt"]
    assert _paths(search.search(conn, "_")) == ["p/a_b.txt"]
    assert _paths(search.search(conn, "50%")) == ["p/50%off.txt"]
    assert _paths(search.search(conn, "a_b")) == ["p/a_b.txt"]
    assert _paths(search.search(conn, '"')) == ['p/say "hi".txt']
    assert _paths(search.search(conn, '"hi"')) == ['p/say "hi".txt']
    assert _paths(search.search(conn, 'say "h')) == ['p/say "hi".txt']


def test_fts_terms_rank_by_bm25_not_path(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "a/aaa/spreadsheet_budget.txt")
    catalog.file(g, "a/zzz/budget_budget_budget_budget.txt")
    assert _paths(search.search(conn, "budget"))[0] == "a/zzz/budget_budget_budget_budget.txt"


def test_remote_and_category_filters(conn, catalog):
    a = catalog.remote("a", "drive")
    b = catalog.remote("b", "drive")
    catalog.file(a, "x.jpg")
    catalog.file(b, "x.jpg")
    catalog.file(b, "y.pdf")
    assert [(r["remote"], r["path"]) for r in search.search(conn, remote="b")] == [
        ("b", "x.jpg"), ("b", "y.pdf")]
    assert [(r["remote"], r["path"]) for r in search.search(conn, category="photo")] == [
        ("a", "x.jpg"), ("b", "x.jpg")]
    assert search.search(conn, remote="nope") == []


def test_ext_filter_accepts_dot_and_any_case(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "a.JPG")
    catalog.file(g, "b.png")
    assert _paths(search.search(conn, ext="jpg")) == ["a.JPG"]
    assert _paths(search.search(conn, ext=".JPG")) == ["a.JPG"]


def test_size_range_is_files_only(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "small.txt", size=5)
    catalog.file(g, "mid.txt", size=50)
    catalog.file(g, "big.txt", size=500)
    catalog.dir(g, "Folder")
    assert _paths(search.search(conn, min_size=10, max_size=100)) == ["mid.txt"]
    assert _paths(search.search(conn, min_size=10, include_dirs=True)) == ["big.txt", "mid.txt"]


def test_mtime_range_is_half_open_on_dates(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "old.txt", mtime="2023-06-01T10:00:00Z")
    catalog.file(g, "edge.txt", mtime="2024-01-01T00:00:00Z")
    catalog.file(g, "new.txt", mtime="2025-03-01T00:00:00Z")
    assert _paths(search.search(conn, modified_after="2024-01-01")) == ["edge.txt", "new.txt"]
    assert _paths(search.search(conn, modified_before="2024-01-01")) == ["old.txt"]
    assert _paths(search.search(conn, modified_after="2024-01-01",
                                modified_before="2025-01-01")) == ["edge.txt"]


def test_path_prefix_is_segment_aware(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.dir(g, "Photos")
    catalog.file(g, "Photos/a.jpg")
    catalog.file(g, "Photos2/b.jpg")
    catalog.file(g, "Photos.jpg")
    assert _paths(search.search(conn, path_prefix="Photos")) == ["Photos/a.jpg"]
    assert _paths(search.search(conn, path_prefix="Photos/", include_dirs=True)) == [
        "Photos", "Photos/a.jpg"]


def test_dirs_and_tombstones_hidden_by_default(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.dir(g, "Docs")
    catalog.file(g, "Docs/live.txt")
    catalog.file(g, "Docs/gone.txt", present=False)
    assert _paths(search.search(conn, "Docs")) == ["Docs/live.txt"]
    assert _paths(search.search(conn, "Docs", include_missing=True)) == [
        "Docs/gone.txt", "Docs/live.txt"]
    assert _paths(search.search(conn, "Docs", include_dirs=True)) == ["Docs", "Docs/live.txt"]
    everything = search.search(conn, "Docs", include_dirs=True, include_missing=True)
    assert _paths(everything) == ["Docs", "Docs/gone.txt", "Docs/live.txt"]
    assert [r["present"] for r in everything] == [True, False, True]


def test_duplicates_only_counts_live_copies(conn, catalog):
    a = catalog.remote("a", "drive")
    b = catalog.remote("b", "local")
    x1 = catalog.file(a, "one.bin", size=10, md5="aa")
    x2 = catalog.file(b, "copy/one.bin", size=10, md5="aa")
    solo = catalog.file(a, "solo.bin", size=3, md5="bb")
    gone = catalog.file(a, "old/one.bin", size=10, md5="aa", present=False)
    for eid, group in [(x1, 1), (x2, 1), (solo, 2), (gone, 1)]:
        conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (group, eid))
    assert [r["id"] for r in search.search(conn, duplicates_only=True)] == [x1, x2]
    both = search.search(conn, duplicates_only=True, include_missing=True)
    assert {r["id"] for r in both} == {x1, x2, gone}


def test_pagination(conn, catalog):
    g = catalog.remote("g", "drive")
    for i in range(5):
        catalog.file(g, f"f{i}.txt")
    pages = [_paths(search.search(conn, limit=2, offset=o)) for o in (0, 2, 4)]
    assert sum(pages, []) == [f"f{i}.txt" for i in range(5)]
    assert len(search.search(conn, limit=0)) == 1
    assert search.search(conn, limit=5000) == search.search(conn, limit=50)
    assert search.search(conn, offset=-3, limit=2) == search.search(conn, limit=2)


def test_fts_follows_name_and_path_updates(conn, catalog):
    g = catalog.remote("g", "drive")
    eid = catalog.file(g, "docs/quarterly_report.txt")
    assert _paths(search.search(conn, "quarterly")) == ["docs/quarterly_report.txt"]
    conn.execute(
        "UPDATE entries SET name = ?, name_norm = ?, path = ?, parent = ? WHERE id = ?",
        ("annual_summary.txt", "annual_summary.txt", "archive/annual_summary.txt", "archive", eid),
    )
    assert search.search(conn, "quarterly") == []
    assert search.search(conn, "docs") == []
    assert _paths(search.search(conn, "annual")) == ["archive/annual_summary.txt"]
    assert _paths(search.search(conn, "archive")) == ["archive/annual_summary.txt"]


def test_entry_shape_and_lookup(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    eid = catalog.file(g, "Docs/a.pdf", size=42, md5="aa", sha256="bb", mime="application/pdf")
    e = search.get_entry(conn, eid)
    assert e["remote"] == "gdrive"
    assert (e["path"], e["name"], e["ext"], e["category"]) == ("Docs/a.pdf", "a.pdf", "pdf", "pdf")
    assert e["is_dir"] is False and e["present"] is True and e["is_native_doc"] is False
    assert e["size"] == 42
    assert e["hashes"] == {"md5": "aa", "sha256": "bb"}
    assert search.get_entry(conn, 9999) is None


def test_entries_at_returns_all_live_rows_at_path(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    first = catalog.file(g, "Notes/todo.txt", provider_id="id1")
    second = catalog.file(g, "Notes/todo.txt", provider_id="id2")
    catalog.file(g, "Notes/todo.txt", provider_id="id3", present=False)
    assert [e["id"] for e in search.entries_at(conn, g, "Notes/todo.txt")] == [first, second]
    assert [e["id"] for e in search.entries_at(conn, g, "/Notes/todo.txt/")] == [first, second]
    assert search.entries_at(conn, g, "Notes/nope.txt") == []


def test_changes_pages_through_a_shared_row_version(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "a.txt")
    rv = db.next_row_version(conn)
    batch = [_raw_insert(conn, g, f"batch/f{i}.txt", rv) for i in range(5)]
    catalog.file(g, "z.txt")

    seen, cursor = [], None
    while True:
        rows, nxt = search.changes(conn, cursor, limit=2)
        if not rows:
            assert nxt == cursor
            break
        assert nxt == f"{rows[-1]['row_version']}:{rows[-1]['id']}"
        seen += rows
        cursor = nxt

    all_ids = [r["id"] for r in conn.execute("SELECT id FROM entries ORDER BY row_version, id")]
    assert [r["id"] for r in seen] == all_ids
    assert [r["id"] for r in seen if r["row_version"] == rv] == batch

    rows, _ = search.changes(conn, f"{rv}:{batch[1]}")
    assert [r["id"] for r in rows][:3] == batch[2:]

    tomb = db.next_row_version(conn)
    conn.execute("UPDATE entries SET present = 0, row_version = ? WHERE id = ?", (tomb, batch[2]))
    rows, nxt = search.changes(conn, cursor)
    assert [(r["id"], r["present"]) for r in rows] == [(batch[2], False)]
    assert nxt == f"{tomb}:{batch[2]}"


def test_changes_from_beginning_and_bad_cursor(conn, catalog):
    assert search.changes(conn) == ([], "")
    g = catalog.remote("g", "drive")
    catalog.file(g, "a.txt")
    rows, nxt = search.changes(conn, None, limit=0)
    assert len(rows) == 1
    assert nxt == f"{rows[0]['row_version']}:{rows[0]['id']}"
    assert search.changes(conn, "") == (rows, nxt)
    assert search.changes(conn, nxt) == ([], nxt)
    with pytest.raises(ValueError):
        search.changes(conn, "nonsense")


def test_stats_numbers(conn, catalog):
    a = catalog.remote("a", "drive")
    b = catalog.remote("b", "local")
    c = catalog.remote("c", "drive")
    catalog.dir(a, "Photos")
    p1 = catalog.file(a, "Photos/x.jpg", size=10, md5="aa")
    p2 = catalog.file(a, "Photos/y.jpg", size=10, md5="aa")
    catalog.file(a, "notes.txt", size=5)
    gone = catalog.file(a, "old.jpg", size=1000, present=False)
    q1 = catalog.file(b, "copy/x.jpg", size=10, md5="aa")
    d1 = catalog.file(b, "d1.bin", size=7)
    d2 = catalog.file(b, "d2.bin", size=7)
    for eid, group in [(p1, 1), (p2, 1), (q1, 1), (gone, 1), (d1, 2), (d2, 2)]:
        conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (group, eid))
    failed = conn.execute(
        "INSERT INTO scans(remote_id, status, started_at, finished_at, error) "
        "VALUES (?, 'failed', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z', 'boom') RETURNING id",
        (a,),
    ).fetchone()[0]

    s = search.stats(conn)
    by_name = {r["name"]: r for r in s["remotes"]}
    assert [r["name"] for r in s["remotes"]] == ["a", "b", "c"]
    assert (by_name["a"]["files"], by_name["a"]["dirs"], by_name["a"]["bytes"]) == (3, 1, 25)
    assert (by_name["b"]["files"], by_name["b"]["dirs"], by_name["b"]["bytes"]) == (3, 1, 24)
    assert (by_name["c"]["files"], by_name["c"]["dirs"], by_name["c"]["bytes"]) == (0, 0, 0)
    assert by_name["a"]["provider"] == "drive"
    assert by_name["a"]["last_scan"] == {"id": failed, "status": "failed",
                                         "finished_at": "2026-01-01T00:05:00Z"}
    assert by_name["c"]["last_scan"]["status"] == "complete"
    assert s["totals"] == {"files": 6, "bytes": 49}
    assert s["duplicates"] == {"groups": 2, "redundant_files": 3, "redundant_bytes": 27}
    assert [(c["category"], c["files"], c["bytes"]) for c in s["categories"]] == [
        ("photo", 3, 30), ("other", 2, 14), ("document", 1, 5)]


def test_nfd_name_matches_nfc_query_via_fts(conn, catalog):
    import unicodedata
    g = catalog.remote("icloud", "iclouddrive")
    nfd = unicodedata.normalize("NFD", "Café menu.pdf")
    catalog.file(g, f"Docs/{nfd}", size=5)
    rows = search.search(conn, "café")
    assert [r["name"] for r in rows] == [nfd]


# --- review fixes: NFD folders, NUL, empty files ------------------------------------------------

FOLDER_NFC = "Résumés"
FOLDER_NFD = unicodedata.normalize("NFD", FOLDER_NFC)


@pytest.fixture
def nfd_catalog(conn, catalog):
    """An iCloud folder stored the way macOS emits it (NFD) next to an ASCII twin."""
    g = catalog.remote("icloud", "iclouddrive")
    catalog.file(g, f"{FOLDER_NFD}/cv.txt")
    catalog.file(g, f"{FOLDER_NFD}/Sub/note.txt")
    catalog.file(g, "Photos/cv.txt")
    catalog.file(g, f"{unicodedata.normalize('NFD', 'éé')}/z.txt")
    return conn


def test_ascii_folder_name_finds_its_files(nfd_catalog):
    assert _paths(search.search(nfd_catalog, "Photos")) == ["Photos/cv.txt"]


@pytest.mark.parametrize("query", [FOLDER_NFC, FOLDER_NFD, FOLDER_NFC.upper(), "résumé", "sumés"])
def test_folder_name_in_either_unicode_form_finds_files_below_an_nfd_folder(nfd_catalog, query):
    assert sorted(_paths(search.search(nfd_catalog, query))) == sorted(
        [f"{FOLDER_NFD}/Sub/note.txt", f"{FOLDER_NFD}/cv.txt"])


def test_folder_name_term_still_ands_with_other_terms(nfd_catalog):
    assert _paths(search.search(nfd_catalog, f"{FOLDER_NFC} cv")) == [f"{FOLDER_NFD}/cv.txt"]
    assert _paths(search.search(nfd_catalog, f"{FOLDER_NFC} Photos")) == []


def test_short_folder_name_term_uses_both_forms_too(nfd_catalog):
    assert _paths(search.search(nfd_catalog, "éé")) == [f"{unicodedata.normalize('NFD', 'éé')}/z.txt"]
    assert _paths(search.search(nfd_catalog, "é"))          # 1 character: LIKE on the NFD path


@pytest.mark.parametrize("prefix", [FOLDER_NFC, FOLDER_NFD, f"/{FOLDER_NFC}/"])
def test_path_prefix_in_either_unicode_form_matches_an_nfd_folder(nfd_catalog, prefix):
    assert sorted(_paths(search.search(nfd_catalog, "txt", path_prefix=prefix))) == sorted(
        [f"{FOLDER_NFD}/Sub/note.txt", f"{FOLDER_NFD}/cv.txt"])
    assert _paths(search.search(nfd_catalog, "txt", path_prefix=prefix.rstrip("/") + "/Sub")) == [f"{FOLDER_NFD}/Sub/note.txt"]


def test_path_prefix_stays_segment_aware_for_nfd_folders(nfd_catalog, catalog):
    g = nfd_catalog.execute("SELECT id FROM remotes").fetchone()[0]
    catalog.file(g, f"{FOLDER_NFD}x/other.txt")
    assert _paths(search.search(nfd_catalog, "other", path_prefix=FOLDER_NFC)) == []


def test_nul_in_a_term_cannot_break_the_query(conn, catalog):
    assert search.search(conn, "abc\x00def") == []
    g = catalog.remote("g", "drive")
    catalog.file(g, "docs/abc-def.txt")
    assert _paths(search.search(conn, "abc\x00def")) == ["docs/abc-def.txt"]         # NUL separates terms
    assert _paths(search.search(conn, "ab\x00")) == ["docs/abc-def.txt"]
    assert len(search.search(conn, "\x00")) == 1                                       # no terms left: no filter


def test_empty_files_are_not_duplicates(conn, catalog):
    g = catalog.remote("g", "drive")
    e1 = catalog.file(g, "proj1/__init__.py", size=0, md5="d41d8cd98f00b204e9800998ecf8427e")
    e2 = catalog.file(g, "proj2/__init__.py", size=0, md5="d41d8cd98f00b204e9800998ecf8427e")
    r1 = catalog.file(g, "a/real.bin", size=5, md5="aa")
    r2 = catalog.file(g, "b/real.bin", size=5, md5="aa")
    for eid, group in [(e1, 1), (e2, 1), (r1, 2), (r2, 2)]:
        conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (group, eid))
    assert _paths(search.search(conn, "py", duplicates_only=True)) == []
    assert sorted(_paths(search.search(conn, "real", duplicates_only=True))) == ["a/real.bin", "b/real.bin"]
    assert search.stats(conn)["duplicates"] == {"groups": 1, "redundant_files": 1, "redundant_bytes": 5}
