"""Tests for drivemerge.compare: the evidence rules, alignment passes, refusals and their order."""

from __future__ import annotations

import time
import unicodedata

import pytest

from drivemerge import db, dirstats
from drivemerge.compare import CompareError, compare_folders

QUARANTINE = db.DEFAULT_QUARANTINE_DIR


def group(conn, number: int, *entry_ids: int) -> None:
    """Put the entries in one content group, as identity.rebuild_groups would after hash proof."""
    for i in entry_ids:
        conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (number, i))


@pytest.fixture
def two(conn, catalog):
    """gdrive:School and dropbox:School (1), plus a third remote for copies elsewhere."""
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    o = catalog.remote("onedrive", "onedrive")
    return g, d, o


def by_rel(result) -> dict[str, dict]:
    return {r["rel"]: r for r in result["rows"]}


def run(conn, l="School", r="School (1)", **kw):
    return compare_folders(conn, ("gdrive", l), ("dropbox", r), **kw)


# --------------------------------------------------------------------------- statuses

def test_every_status_and_its_evidence(conn, catalog, two):
    g, d, o = two
    a1 = catalog.file(g, "School/same.pdf", size=10, md5="aa")
    a2 = catalog.file(d, "School (1)/same.pdf", size=10, dropbox="xx")
    group(conn, 1, a1, a2)
    catalog.file(g, "School/size.txt", size=5)
    catalog.file(d, "School (1)/size.txt", size=6)
    catalog.file(g, "School/hash.bin", size=7, md5="m1", sha1="s")
    catalog.file(d, "School (1)/hash.bin", size=7, md5="m2", sha1="s")
    catalog.file(g, "School/unproven.doc", size=9, md5="a")                 # sizes equal, hash types differ
    catalog.file(d, "School (1)/unproven.doc", size=9, dropbox="b")
    m1 = catalog.file(g, "School/Old/moved.jpg", size=20, md5="mv")
    m2 = catalog.file(d, "School (1)/New/renamed.jpg", size=20, dropbox="mv2")
    group(conn, 2, m1, m2)
    catalog.file(g, "School/left.txt", size=1)
    catalog.file(d, "School (1)/right.txt", size=2)

    res = run(conn)
    rows = by_rel(res)
    assert {k: v["status"] for k, v in rows.items()} == {
        "same.pdf": "same", "size.txt": "differs", "hash.bin": "differs", "unproven.doc": "unverified",
        "Old/moved.jpg": "moved", "left.txt": "left_only", "right.txt": "right_only"}
    assert rows["same.pdf"]["evidence"] == {"kind": "group"}
    assert rows["size.txt"]["evidence"] == {"kind": "size"}
    assert rows["hash.bin"]["evidence"] == {"kind": "hash", "hash": "md5"}
    assert rows["unproven.doc"]["evidence"] == {"kind": "none"}
    assert rows["Old/moved.jpg"]["rel_right"] == "New/renamed.jpg"
    assert rows["same.pdf"]["rel_right"] is None
    assert rows["left.txt"]["right"] is None and rows["right.txt"]["left"] is None
    s = res["summary"]
    assert (s["same"]["files"], s["differs"]["files"], s["unverified"]["files"], s["moved"]["files"],
            s["left_only"]["files"], s["right_only"]["files"]) == (1, 2, 1, 1, 1, 1)
    # bytes: the left file for pairs and left-only rows, the right file for right-only rows
    assert s["differs"]["bytes"] == 5 + 7 and s["right_only"]["bytes"] == 2 and s["moved"]["bytes"] == 20
    assert s["similarity"] == pytest.approx(2 / 7)
    assert res["left"] == {"remote": "gdrive", "path": "School", "files": 6, "bytes": 10 + 5 + 7 + 9 + 20 + 1}
    assert res["right"]["files"] == 6
    left = rows["size.txt"]["left"]
    assert set(left) == {"entry_id", "path", "name", "size", "mtime", "category", "content_group", "hashes"}
    assert left["path"] == "School/size.txt" and left["name"] == "size.txt"


def test_equal_known_sizes_without_a_shared_hash_type_are_not_proven(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "School/a", size=3)
    catalog.file(d, "School (1)/a", size=3)
    row = run(conn)["rows"][0]
    assert row["status"] == "unverified"


def test_different_groups_alone_prove_nothing(conn, catalog, two):
    g, d, _ = two
    a = catalog.file(g, "School/a", size=3)
    b = catalog.file(d, "School (1)/a", size=3)
    group(conn, 1, a)
    group(conn, 2, b)
    assert run(conn)["rows"][0]["status"] == "unverified"


def test_unknown_size_never_proves_a_difference(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "School/Doc", size=None, native_doc=True)
    catalog.file(d, "School (1)/Doc", size=4)
    assert run(conn)["rows"][0]["status"] == "unverified"


def test_empty_files_are_compared_by_path(conn, catalog, two):
    g, d, _ = two
    e1 = catalog.file(g, "School/empty", size=0, md5="d41d")
    e2 = catalog.file(d, "School (1)/empty", size=0, md5="d41d")
    group(conn, 5, e1, e2)
    catalog.file(g, "School/e2", size=0)
    catalog.file(d, "School (1)/e2", size=0)
    rows = by_rel(run(conn))
    assert rows["empty"]["status"] == "same"
    assert rows["e2"]["status"] == "unverified"


# --------------------------------------------------------------------------- alignment

def test_drive_same_path_duplicates_pair_identical_ones_first(conn, catalog, two):
    g, d, _ = two
    g1 = catalog.file(g, "School/a.txt", size=1, provider_id="g1")
    g2 = catalog.file(g, "School/a.txt", size=2, provider_id="g2")
    d1 = catalog.file(d, "School (1)/a.txt", size=2, provider_id="d1")
    group(conn, 1, g2, d1)                    # the second Drive file is the identical one
    res = run(conn)
    by_status = {r["status"]: r for r in res["rows"]}
    assert len(res["rows"]) == 2
    assert by_status["same"]["left"]["entry_id"] == g2 and by_status["same"]["right"]["entry_id"] == d1
    assert by_status["left_only"]["left"]["entry_id"] == g1


def test_leftover_same_path_files_pair_in_entry_id_order(conn, catalog, two):
    g, d, _ = two
    g1 = catalog.file(g, "School/a.txt", size=1, provider_id="g1")
    g2 = catalog.file(g, "School/a.txt", size=2, provider_id="g2")
    d1 = catalog.file(d, "School (1)/a.txt", size=1, provider_id="d1")
    d2 = catalog.file(d, "School (1)/a.txt", size=2, provider_id="d2")
    pairs = {(r["left"]["entry_id"], r["right"]["entry_id"]): r["status"] for r in run(conn)["rows"]}
    assert pairs == {(g1, d1): "unverified", (g2, d2): "unverified"}


def test_content_pass_pairs_one_to_one_in_rel_order(conn, catalog, two):
    g, d, _ = two
    l1 = catalog.file(g, "School/x/a.bin", size=4)
    l2 = catalog.file(g, "School/y/a.bin", size=4)
    r1 = catalog.file(d, "School (1)/p/q.bin", size=4)
    group(conn, 1, l1, l2, r1)
    rows = run(conn)["rows"]
    assert [(r["rel"], r["status"], r["rel_right"]) for r in rows] == [
        ("x/a.bin", "moved", "p/q.bin"), ("y/a.bin", "left_only", None)]
    # the leftover has a copy in the other folder, so it is not unique
    assert rows[1]["unique"] is False


def test_case_insensitive_match_when_either_remote_ignores_case(conn, catalog, two):
    g, d, _ = two          # drive and dropbox are both case-insensitive providers
    catalog.file(g, "School/Notes.TXT", size=1)
    catalog.file(d, "School (1)/notes.txt", size=1)
    res = run(conn)
    assert [r["status"] for r in res["rows"]] == ["unverified"]
    assert res["rows"][0]["rel"] == "Notes.TXT"


def test_case_sensitive_pair_keeps_case_apart(conn, catalog):
    a = catalog.remote("a", "local")
    b = catalog.remote("b", "local")
    catalog.file(a, "F/Notes.txt", size=1)
    catalog.file(b, "F/notes.txt", size=1)
    res = compare_folders(conn, ("a", "F"), ("b", "F"))
    assert sorted(r["status"] for r in res["rows"]) == ["left_only", "right_only"]


def test_case_folding_applies_if_only_one_remote_is_insensitive(conn, catalog):
    a = catalog.remote("a", "local")
    b = catalog.remote("b", "dropbox")
    catalog.file(a, "F/Notes.txt", size=1)
    catalog.file(b, "F/notes.txt", size=1)
    res = compare_folders(conn, ("a", "F"), ("b", "F"))
    assert [r["status"] for r in res["rows"]] == ["unverified"]


def test_nfd_and_nfc_names_are_the_same_file(conn, catalog, two):
    g, d, _ = two
    nfd = unicodedata.normalize("NFD", "café.txt")
    nfc = unicodedata.normalize("NFC", "café.txt")
    catalog.file(g, f"School/{nfd}", size=1)
    catalog.file(d, f"School (1)/{nfc}", size=1)
    assert [r["status"] for r in run(conn)["rows"]] == ["unverified"]


def test_nfd_folder_name_resolves(conn, catalog, two):
    g, d, _ = two
    nfd = unicodedata.normalize("NFD", "café")
    catalog.file(g, f"{nfd}/a.txt", size=1)
    catalog.file(d, "x/a.txt", size=1)
    res = compare_folders(conn, ("gdrive", "café"), ("dropbox", "x"))
    assert res["left"]["path"] == nfd and len(res["rows"]) == 1


def test_root_comparison_and_slashes_are_stripped(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "a/b.txt", size=1)
    catalog.file(d, "a/b.txt", size=1)
    res = compare_folders(conn, ("gdrive", ""), ("dropbox", "/"))
    assert res["left"]["path"] == "" and res["rows"][0]["rel"] == "a/b.txt"
    res = compare_folders(conn, ("gdrive", "/a/"), ("dropbox", "a"))
    assert res["rows"][0]["rel"] == "b.txt"


def test_a_sibling_folder_with_the_same_prefix_is_not_inside(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "School/a", size=1)
    catalog.file(g, "School2/b", size=1)
    catalog.file(g, "School.old/c", size=1)
    catalog.file(d, "x/a", size=1)
    res = compare_folders(conn, ("gdrive", "School"), ("dropbox", "x"))
    assert res["left"]["files"] == 1


def test_rows_use_natural_case_insensitive_order(conn, catalog, two):
    g, d, _ = two
    for name in ("file10.txt", "File2.txt", "file1.txt", "B/x.txt", "a/x.txt"):
        catalog.file(g, f"School/{name}", size=1)
    catalog.dir(d, "School (1)")
    assert [r["rel"] for r in run(conn)["rows"]] == [
        "a/x.txt", "B/x.txt", "file1.txt", "File2.txt", "file10.txt"]


# --------------------------------------------------------------------------- elsewhere / unique

def test_elsewhere_and_unique(conn, catalog, two):
    g, d, o = two
    lone = catalog.file(g, "School/lone.bin", size=8, md5="1")
    spare = catalog.file(g, "School/spare.bin", size=9, md5="2")
    copies = [catalog.file(o, f"Bk/spare{i}.bin", size=9, md5="2") for i in range(4)]
    inner = catalog.file(g, "School/inside.bin", size=3, md5="3")      # its twin sits on the other side, elsewhere
    twin = catalog.file(d, "School (1)/sub/inside2.bin", size=3, md5="3")
    ungrouped = catalog.file(g, "School/nohash.bin", size=5)
    same_root = catalog.file(g, "School/dupe.bin", size=7, md5="4")     # a second copy inside the same folder
    same_root2 = catalog.file(g, "School/dupe2.bin", size=7, md5="4")
    group(conn, 1, lone)
    group(conn, 2, spare, *copies)
    group(conn, 3, inner, twin)
    group(conn, 4, same_root, same_root2)
    rows = by_rel(run(conn))
    assert rows["lone.bin"]["elsewhere"] == {"count": 0, "examples": []}
    assert rows["lone.bin"]["unique"] is True
    spare_row = rows["spare.bin"]
    assert spare_row["elsewhere"]["count"] == 4 and spare_row["unique"] is False
    assert spare_row["elsewhere"]["examples"] == [{"remote": "onedrive", "path": f"Bk/spare{i}.bin"} for i in range(3)]
    assert rows["nohash.bin"]["elsewhere"] is None and rows["nohash.bin"]["unique"] is None
    # the moved pair has no elsewhere/unique; the right-only row with a counterpart on the left isn't unique
    assert rows["inside.bin"]["status"] == "moved" and rows["inside.bin"]["unique"] is None
    # copies inside the same compared root are not "elsewhere"
    assert rows["dupe.bin"]["elsewhere"]["count"] == 0
    assert rows["dupe.bin"]["status"] == "left_only"
    assert run(conn)["summary"]["unique"] == 3          # lone, dupe, dupe2 (nothing outside either folder)


def test_a_right_only_file_is_not_unique_if_the_left_folder_holds_its_content(conn, catalog, two):
    g, d, _ = two
    a = catalog.file(g, "School/a.txt", size=4)
    b = catalog.file(d, "School (1)/a.txt", size=5)      # differs, paired by path
    c = catalog.file(d, "School (1)/c.txt", size=4)      # same content as a, but a already paired
    group(conn, 1, a, c)
    row = by_rel(run(conn))["c.txt"]
    assert row["status"] == "right_only"
    assert row["elsewhere"]["count"] == 0 and row["unique"] is False
    del b


def test_quarantined_files_are_neither_compared_nor_counted_as_copies(conn, catalog, two):
    g, d, o = two
    keep = catalog.file(g, "School/a.bin", size=3, md5="1")
    parked = catalog.file(o, f"{QUARANTINE}/2024/a.bin", size=3, md5="1")
    catalog.file(g, f"School/{QUARANTINE}/x.bin", size=3)       # a quarantine folder name deeper down is not special
    catalog.file(d, "School (1)/b", size=1)
    group(conn, 1, keep, parked)
    res = run(conn)
    assert by_rel(res)["a.bin"]["unique"] is True            # the only other copy is parked
    assert by_rel(res)["a.bin"]["elsewhere"]["count"] == 0
    # a compared folder is never the quarantine; and files directly below it do not show in a parent either
    catalog.file(g, f"{QUARANTINE}/old/z", size=1)
    res = compare_folders(conn, ("gdrive", ""), ("dropbox", ""))
    assert all(not r["left"]["path"].startswith(QUARANTINE) for r in res["rows"] if r["left"])


def test_tombstones_are_not_compared(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "School/gone", size=1, present=False)
    catalog.file(d, "School (1)/here", size=1)
    res = run(conn)
    assert [r["rel"] for r in res["rows"]] == ["here"] and res["left"]["files"] == 0


# --------------------------------------------------------------------------- similarity

def test_similarity_edge_cases(conn, catalog, two):
    g, d, _ = two
    catalog.dir(g, "Empty")
    catalog.dir(d, "Empty")
    res = compare_folders(conn, ("gdrive", "Empty"), ("dropbox", "Empty"))
    assert res["rows"] == [] and res["summary"]["similarity"] == 1.0
    assert res["left"]["files"] == 0 and res["summary"]["unique"] == 0
    catalog.file(g, "L/a", size=1)
    catalog.dir(d, "R")
    assert compare_folders(conn, ("gdrive", "L"), ("dropbox", "R"))["summary"]["similarity"] == 0.0


def test_a_pair_counts_once_in_similarity(conn, catalog, two):
    g, d, _ = two
    a = catalog.file(g, "School/a", size=1)
    b = catalog.file(d, "School (1)/a", size=1)
    group(conn, 1, a, b)
    catalog.file(g, "School/only", size=1)
    assert run(conn)["summary"]["similarity"] == 0.5


# --------------------------------------------------------------------------- refusals

def code_of(conn, *a, **kw) -> CompareError:
    with pytest.raises(CompareError) as exc:
        compare_folders(conn, *a, **kw)
    return exc.value


def test_unknown_remote_is_not_found(conn, catalog, two):
    e = code_of(conn, ("nope", ""), ("dropbox", ""))
    assert (e.code, e.status, e.params) == ("NOT_FOUND", 404, {"remote": "nope"})
    assert code_of(conn, ("gdrive", ""), ("nope", "")).code == "NOT_FOUND"


def test_not_a_folder(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "School/a.txt", size=1)
    e = code_of(conn, ("gdrive", "School/a.txt"), ("dropbox", ""))
    assert (e.code, e.status, e.params) == ("NOT_A_FOLDER", 409, {"remote": "gdrive", "path": "School/a.txt"})
    assert code_of(conn, ("gdrive", "School"), ("dropbox", "missing")).params["remote"] == "dropbox"
    catalog.dir(d, "Gone")
    conn.execute("UPDATE entries SET present = 0 WHERE path = 'Gone'")
    assert code_of(conn, ("gdrive", "School"), ("dropbox", "Gone")).code == "NOT_A_FOLDER"
    compare_folders(conn, ("gdrive", ""), ("dropbox", ""))          # the root is always a folder, even when empty


def test_quarantine_refusal_comes_before_the_folder_check(conn, catalog, two):
    g, d, _ = two
    e = code_of(conn, ("gdrive", QUARANTINE), ("dropbox", "missing"))
    assert (e.code, e.status, e.params) == ("QUARANTINE", 409, {"remote": "gdrive", "path": QUARANTINE})
    assert code_of(conn, ("gdrive", ""), ("dropbox", f"{QUARANTINE}/2024/x")).code == "QUARANTINE"
    catalog.dir(g, f"{QUARANTINE}-notes")                              # a similarly named folder is not quarantine
    compare_folders(conn, ("gdrive", f"{QUARANTINE}-notes"), ("dropbox", ""))


def test_nested_folders_on_one_remote(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "A/B/f", size=1)
    catalog.file(g, "AB/f", size=1)
    for l, r in (("A", "A"), ("A", "A/B"), ("A/B", "A"), ("", "A")):
        e = code_of(conn, ("gdrive", l), ("gdrive", r))
        assert e.code == "COMPARE_NESTED" and e.status == 409
        assert e.params == {"left": {"remote": "gdrive", "path": l}, "right": {"remote": "gdrive", "path": r}}
    compare_folders(conn, ("gdrive", "A"), ("gdrive", "AB"))           # siblings are fine
    compare_folders(conn, ("gdrive", "A"), ("dropbox", ""))            # another remote is fine


def test_case_sensitive_remote_keeps_folders_that_differ_in_case_apart(conn, catalog):
    s = catalog.remote("s", "local")
    catalog.file(s, "Docs/x/f", size=1)
    catalog.dir(s, "docs")
    res = compare_folders(conn, ("s", "Docs"), ("s", "docs"))          # not nested: different folders
    assert [r["status"] for r in res["rows"]] == ["left_only"]


def test_too_large_uses_dir_stats_then_confirms_with_a_count(conn, catalog, two):
    g, d, _ = two
    for i in range(6):
        catalog.file(g, f"School/f{i}", size=1)
    catalog.file(d, "School (1)/x", size=1)
    dirstats.refresh(conn)
    e = code_of(conn, ("gdrive", "School"), ("dropbox", "School (1)"), max_files=5)
    assert (e.code, e.status, e.params) == ("COMPARE_TOO_LARGE", 409, {"side": "left", "files": 6, "limit": 5})
    assert code_of(conn, ("dropbox", "School (1)"), ("gdrive", "School"), max_files=5).params["side"] == "right"
    assert len(compare_folders(conn, ("gdrive", "School"), ("dropbox", "School (1)"), max_files=6)["rows"]) == 7


def test_too_large_without_dir_stats_recounts_the_rows(conn, catalog, two):
    g, d, _ = two
    for i in range(4):
        catalog.file(d, f"School (1)/f{i}", size=1)
    catalog.dir(g, "School")
    e = code_of(conn, ("gdrive", "School"), ("dropbox", "School (1)"), max_files=3)
    assert e.code == "COMPARE_TOO_LARGE" and e.params == {"side": "right", "files": 4, "limit": 3}


def test_a_stale_dir_stats_that_counts_quarantine_does_not_refuse(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "School/a", size=1)
    catalog.file(g, f"{QUARANTINE}/x", size=1)
    dirstats.refresh(conn)
    assert dirstats.get(conn, g, "")["files"] == 2
    res = compare_folders(conn, ("gdrive", ""), ("dropbox", ""), max_files=1)
    assert res["left"]["files"] == 1


def test_order_of_refusals(conn, catalog, two):
    g, d, _ = two
    for i in range(3):
        catalog.file(g, f"A/f{i}", size=1)
    # QUARANTINE before NOT_A_FOLDER before NESTED before TOO_LARGE
    assert code_of(conn, ("gdrive", "nope"), ("gdrive", QUARANTINE)).code == "QUARANTINE"
    assert code_of(conn, ("gdrive", "nope"), ("gdrive", "nope2")).code == "NOT_A_FOLDER"
    assert code_of(conn, ("gdrive", "A"), ("gdrive", "A"), max_files=1).code == "COMPARE_NESTED"
    assert code_of(conn, ("zzz", "nope"), ("gdrive", QUARANTINE)).code == "NOT_FOUND"


# --------------------------------------------------------------------------- performance, purity

def test_twenty_thousand_files_per_side_in_well_under_a_second(conn, catalog, two):
    g, d, _ = two
    n = 20000
    scan = conn.execute("SELECT MAX(id) FROM scans").fetchone()[0]
    catalog.dir(g, "S")
    catalog.dir(d, "S")
    batch = [(rid, f"p:S/{i % 50}/f{i}", f"S/{i % 50}/f{i}", f"S/{i % 50}", f"f{i}", f"f{i}", 10 + i % 7, scan, scan,
              100 + i) for rid in (g, d) for i in range(n)]
    conn.executemany(
        "INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, is_dir, size, present, "
        "first_seen_scan, last_seen_scan, row_version) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?, ?)", batch)
    start = time.perf_counter()
    res = compare_folders(conn, ("gdrive", "S"), ("dropbox", "S"))
    assert time.perf_counter() - start < 2.0          # generous bound for CI; a laptop needs well under a second
    assert len(res["rows"]) == n and res["summary"]["unverified"]["files"] == n


def test_compare_does_not_write_the_catalog(conn, catalog, two):
    g, d, _ = two
    catalog.file(g, "School/a", size=1)
    catalog.dir(d, "School (1)")
    before = conn.total_changes
    run(conn)
    assert conn.total_changes == before
