"""Tests for drivemerge.suggest: related-folder clusters and duplicate-folder merges.

Content identity in these catalogs comes from a shared md5 on every remote (as if
the hash domains had been bridged), then identity.rebuild_groups() fills
content_group exactly like a real scan would.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time

import pytest

from drivemerge import db, identity, suggest


def populate(catalog, rid, folder, files):
    """Add files to `folder`. A name alone is unique content; (name, content) pairs share bytes by content."""
    for item in files:
        name, content = item if isinstance(item, tuple) else (item, f"{rid}:{folder}/{item}")
        catalog.file(rid, f"{folder}/{name}", size=100 + len(content), md5=hashlib.md5(content.encode()).hexdigest())


def shared(prefix, names):
    """(name, content) pairs whose bytes are identical wherever the same prefix+name is used."""
    return [(n, f"{prefix}:{n}") for n in names]


def locs(sug):
    return {(f["remote"], f["path"]) for f in sug["folders"]}


def all_locs(sugs):
    return {loc for s in sugs for loc in locs(s)}


THESIS = ["thesis_chapter1.docx", "thesis_chapter2.docx", "thesis_chapter3.docx",
          "thesis_abstract.docx", "thesis_references.bib"]


@pytest.fixture
def scattered(conn, catalog):
    """School and research material spread over three clouds, plus noise that must stay untouched."""
    dbx = catalog.remote("Dropbox", "dropbox")
    gd = catalog.remote("GDrive", "drive")
    od = catalog.remote("OneDrive", "onedrive")
    # The thesis: one folder on Dropbox, a drafts folder, and two folders that only hold copies of its files.
    populate(catalog, dbx, "School/NTU/Thesis", shared("thesis", THESIS))
    populate(catalog, gd, "Research/thesis-drafts",
             shared("thesis", THESIS[:2]) + ["thesis_draft_v1.docx", "thesis_draft_v2.docx", "notes.txt"])
    populate(catalog, gd, "Old/論文資料", shared("thesis", THESIS[:4]) + ["論文_口試簡報.pptx"])
    populate(catalog, od, "Documents/研究計畫", shared("thesis", THESIS[:4]) + ["research_proposal.docx"])
    populate(catalog, dbx, "School/研究計畫備份", ["research_proposal.docx", "研究計畫書_v2.docx", "literature_review.docx"])
    # A second, separate topic: course notes in two places.
    populate(catalog, dbx, "School/Linear Algebra", ["week1_vectors.pdf", "week2_matrices.pdf", "week3_eigen.pdf"])
    populate(catalog, gd, "Courses/linear-algebra notes", ["lecture_rank.pdf", "lecture_basis.pdf", "lecture_det.pdf",
                                                           "midterm.pdf"])
    # Noise: camera folders, downloads, generic names with unrelated contents.
    for rid, tag in ((dbx, "d"), (gd, "g")):
        populate(catalog, rid, "Photos/2019", [f"IMG_{tag}{i:04d}.jpg" for i in range(6)])
        populate(catalog, rid, "Downloads", [f"{tag}-{w}.pdf" for w in ("setup", "invoice", "menu", "ticket")])
    populate(catalog, gd, "Documents", ["lease.pdf", "tax.pdf", "cv.docx"])
    populate(catalog, od, "Documents", ["budget.xlsx", "passport.jpg", "insurance.pdf"])
    populate(catalog, gd, "新增資料夾", ["a1.txt", "a2.txt", "a3.txt"])
    populate(catalog, dbx, "新增資料夾", ["b1.txt", "b2.txt", "b3.txt"])
    populate(catalog, od, "New folder", ["c1.txt", "c2.txt", "c3.txt"])
    populate(catalog, gd, "Misc", ["x1.bin", "x2.bin", "x3.bin"])
    populate(catalog, dbx, "Misc", ["y1.bin", "y2.bin", "y3.bin"])
    identity.rebuild_groups(conn)
    return conn


NOISE = [("Dropbox", "Photos/2019"), ("GDrive", "Photos/2019"), ("Dropbox", "Downloads"), ("GDrive", "Downloads"),
         ("GDrive", "Documents"), ("OneDrive", "Documents"), ("GDrive", "新增資料夾"), ("Dropbox", "新增資料夾"),
         ("OneDrive", "New folder"), ("GDrive", "Misc"), ("Dropbox", "Misc")]


# --------------------------------------------------------------------------- tokens

def test_tokens_split_words_camel_case_and_digits():
    assert list(suggest._tokens("Thesis_Draft-v2")) == ["thesis", "draft"]
    assert list(suggest._tokens("MyThesisDraft")) == ["thesis", "draft"]       # 'my' is generic
    assert list(suggest._tokens("HTMLParser2go")) == ["html", "parser", "go"]
    assert list(suggest._tokens("thesis2019")) == ["thesis", "2019"]


def test_tokens_numbers_dropped_years_weak():
    toks = suggest._tokens("Report 2019 v3 0042")
    assert toks == {"report": 1.0, "2019": suggest.YEAR_WEIGHT}
    assert suggest._tokens("1850 12345 99") == {}
    assert "2019" not in suggest._tokens("Report 2019", years=False)


def test_tokens_fold_plurals_and_accents():
    assert suggest._tokens("Papers") == suggest._tokens("paper")
    assert suggest._tokens("Notes") == suggest._tokens("note")
    assert list(suggest._tokens("Résumé")) == ["resume"]
    assert list(suggest._tokens("Café")) == ["cafe"]                       # NFD in, NFC out
    assert list(suggest._tokens("thesis")) == ["thesis"]                         # 'is' endings are not plurals


def test_tokens_cjk_bigrams_and_single_characters():
    assert list(suggest._tokens("研究計畫")) == ["研究", "究計", "計畫"]
    assert list(suggest._tokens("論文")) == ["論文"]
    assert list(suggest._tokens("書")) == ["書"]                                  # a run of length 1 keeps its character
    assert list(suggest._tokens("Thesis論文draft")) == ["thesis", "論文", "draft"]


def test_tokens_generic_names_vanish():
    for name in ("New folder", "Untitled", "Documents", "Misc", "Backup", "Desktop", "Downloads", "Photos",
                 "Images", "Pictures", "Data", "Other", "Files", "新增資料夾", "未命名", "文件", "資料", "其他",
                 "備份", "下載", "桌面", "照片", "图片", "新建文件夹", "未命名資料夾"):
        assert suggest._tokens(name) == {}, name
    # A generic suffix is removed before the rest is cut into bigrams, so no stray 料夾 bigram survives.
    assert list(suggest._tokens("論文資料")) == ["論文"]
    assert list(suggest._tokens("論文資料夾")) == ["論文"]
    assert list(suggest._tokens("研究計畫備份")) == ["研究", "究計", "計畫"]


def test_name_tokens_only_years_carry_no_evidence():
    assert suggest._name_tokens("2019") == {}
    assert suggest._name_tokens("Photos 2019") == {}
    assert set(suggest._name_tokens("Thesis 2019")) == {"thesis", "2019"}


def test_file_tokens_skip_extension_hidden_files_and_years():
    assert suggest._file_tokens("thesis_chapter1.docx") == ("thesis", "chapter")
    assert suggest._file_tokens(".DS_Store") == ()
    assert suggest._file_tokens("report 2019.pdf") == ("report",)
    assert suggest._file_tokens("論文_第一章.docx") == ("論文", "第一", "一章")
    assert suggest._file_tokens("IMG_0042.jpg") == ()                            # generic camera name


# --------------------------------------------------------------------------- related folders

def test_scattered_thesis_and_research_folders_cluster_and_noise_does_not(scattered):
    sugs = suggest.related_folders(scattered)
    clustered = all_locs(sugs)
    assert len(sugs) == 2                                                       # thesis/research, and the course notes
    assert {("Dropbox", "School/Linear Algebra"), ("GDrive", "Courses/linear-algebra notes")} in [locs(s) for s in sugs]
    for must in [("Dropbox", "School/NTU/Thesis"), ("GDrive", "Research/thesis-drafts"),
                 ("GDrive", "Old/論文資料"), ("OneDrive", "Documents/研究計畫")]:
        assert must in clustered
    # All four are in one suggestion: name words link Thesis and thesis-drafts, copies link the other two.
    main = next(s for s in sugs if ("Dropbox", "School/NTU/Thesis") in locs(s))
    assert {("GDrive", "Research/thesis-drafts"), ("GDrive", "Old/論文資料"),
            ("OneDrive", "Documents/研究計畫")} <= locs(main)
    assert ("Dropbox", "School/研究計畫備份") in clustered                        # same CJK name bigrams as 研究計畫
    assert not clustered & set(NOISE)
    assert all(s["kind"] == "related" for s in sugs)
    # Only the folders themselves are suggested, never the generic parents that merely wrap them
    # ('Old' holds nothing but 論文資料; 'School' holds Thesis, Linear Algebra and 研究計畫備份).
    assert clustered == {
        ("Dropbox", "School/NTU/Thesis"), ("GDrive", "Research/thesis-drafts"), ("GDrive", "Old/論文資料"),
        ("OneDrive", "Documents/研究計畫"), ("Dropbox", "School/研究計畫備份"),
        ("Dropbox", "School/Linear Algebra"), ("GDrive", "Courses/linear-algebra notes"),
    }


def test_suggestion_shape_and_actions_are_stageable(scattered):
    sugs = suggest.related_folders(scattered)
    for s in sugs:
        assert s["id"].startswith("related:") and len(s["id"]) == len("related:") + 12
        assert 0.25 <= s["score"] <= 1.0
        assert s["title"].startswith(f"這 {len(s['folders'])} 個資料夾看起來相關：")
        assert isinstance(s["reason"], str) and s["reason"]
        assert 2 <= len(s["folders"]) <= suggest.MAX_CLUSTER
        for f in s["folders"]:
            assert set(f) == {"remote", "path", "files", "bytes"}
            assert f["files"] >= 3 and f["bytes"] > 0
        anchor, rest = s["folders"][0], s["folders"][1:]
        assert [a["items"] for a in s["actions"]] == [[{"remote": f["remote"], "path": f["path"]}] for f in rest]
        for a in s["actions"]:
            assert a["kind"] == "move" and a["dst"] == {"remote": anchor["remote"], "path": anchor["path"]}
            assert "new_name" not in a                      # the folder keeps its name
            assert a["note"] == s["id"]
        json.dumps(s)                                       # plain JSON all the way down
    main = next(s for s in sugs if any(f["path"] == "School/NTU/Thesis" for f in s["folders"]))
    assert {"thesis", "研究計畫", "chapter"} <= set(main["terms"])               # bigrams are shown as the word
    assert main["title"].startswith("這 5 個資料夾看起來相關：") and main["title"].endswith("…")
    assert "「thesis」" in main["reason"] and "「研究計畫」" in main["reason"] and "4 份內容完全相同" in main["reason"]


def test_anchor_has_most_files_and_ties_go_by_path(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "drive")
    populate(catalog, a, "x/Climate Study", ["a1.csv", "a2.csv", "a3.csv"])
    populate(catalog, b, "z/Climate Study", ["b1.csv", "b2.csv", "b3.csv", "b4.csv", "b5.csv"])
    populate(catalog, a, "m/Climate Study", ["c1.csv", "c2.csv", "c3.csv", "c4.csv", "c5.csv"])
    identity.rebuild_groups(conn)
    (s,) = suggest.related_folders(conn)
    # 5 files each in m/ (A) and z/ (B): path order puts m/Climate Study first.
    assert [(f["remote"], f["path"]) for f in s["folders"]] == [
        ("A", "m/Climate Study"), ("A", "x/Climate Study"), ("B", "z/Climate Study")]
    assert [(x["items"][0]["remote"], x["items"][0]["path"]) for x in s["actions"]] == [
        ("A", "x/Climate Study"), ("B", "z/Climate Study")]
    assert {(x["dst"]["remote"], x["dst"]["path"]) for x in s["actions"]} == {("A", "m/Climate Study")}


def test_generic_names_do_not_cluster(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    c = catalog.remote("C", "onedrive")
    generic = ["Documents", "Misc", "New folder", "Untitled folder", "Backup", "Desktop", "Downloads", "Photos",
               "新增資料夾", "未命名資料夾", "文件", "其他", "備份", "桌面", "照片", "新建文件夾"]
    for i, name in enumerate(generic):
        for j, rid in enumerate((a, b, c)):
            populate(catalog, rid, f"p{i}/{name}", [f"u{i}{j}{k}.dat" for k in range(4)])
    identity.rebuild_groups(conn)
    assert suggest.related_folders(conn) == []


def test_photo_folders_and_year_names_do_not_cluster(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    populate(catalog, a, "Photos/2019", [f"IMG_{i:04d}.jpg" for i in range(8)])
    populate(catalog, b, "Camera Uploads/2019", [f"IMG_{i:04d}.jpg" for i in range(8)])
    populate(catalog, a, "Photos/2020", [f"DSC_{i:04d}.jpg" for i in range(8)])
    populate(catalog, b, "Backup/2020", [f"DSC_{i:04d}.jpg" for i in range(8)])
    identity.rebuild_groups(conn)
    assert suggest.related_folders(conn) == []


def test_same_parent_and_nested_pairs_are_skipped(conn, catalog):
    a = catalog.remote("A", "drive")
    populate(catalog, a, "School/Thesis", [f"t{i}.txt" for i in range(4)])
    populate(catalog, a, "School/Thesis Final", [f"u{i}.txt" for i in range(4)])        # sibling
    populate(catalog, a, "School/Thesis/thesis figures", [f"f{i}.png" for i in range(4)])  # nested
    identity.rebuild_groups(conn)
    assert suggest.related_folders(conn) == []
    # A third, unrelated place makes the two siblings meet; the nested folder never joins its own ancestor.
    b = catalog.remote("B", "dropbox")
    populate(catalog, b, "Archive/Thesis", [f"v{i}.txt" for i in range(4)])
    identity.rebuild_groups(conn)
    for s in suggest.related_folders(conn):
        paths = [f["path"] for f in s["folders"]]
        assert not any(p != q and (p.startswith(q + "/")) for p in paths for q in paths)


def test_folders_inside_an_atomic_unit_are_not_suggested_but_the_unit_root_is(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    populate(catalog, a, "Code/thesis-tools", ["package.json", "index.js", "util.js"])        # unit root
    populate(catalog, a, "Code/thesis-tools/thesis-data", ["d1.csv", "d2.csv", "d3.csv"])     # inside the unit
    populate(catalog, a, "Code/thesis-tools/thesis-data/raw", ["r1.csv", "r2.csv", "r3.csv"])
    populate(catalog, b, "School/Thesis", [f"s{i}.docx" for i in range(4)])
    populate(catalog, b, "Misc/thesis data", [f"m{i}.csv" for i in range(4)])
    identity.rebuild_groups(conn)
    sugs = suggest.related_folders(conn)
    found = all_locs(sugs)
    assert ("A", "Code/thesis-tools") in found                                  # the root may move as a whole
    assert not any(p.startswith("Code/thesis-tools/") for _, p in found)       # its insides may not
    assert {("B", "School/Thesis"), ("B", "Misc/thesis data")} <= found


def test_clusters_are_capped_and_the_rest_cluster_again(conn, catalog):
    rids = [catalog.remote(n, "drive") for n in ("A", "B", "C")]
    for i in range(12):
        populate(catalog, rids[i % 3], f"p{i}/Climate Study", [f"s{i}-{k}.csv" for k in range(3 + i % 2)])
    identity.rebuild_groups(conn)
    sugs = suggest.related_folders(conn, limit=50)
    assert sorted(len(s["folders"]) for s in sugs) == [4, 8]
    assert len(all_locs(sugs)) == 12                                            # nothing lost, nothing twice
    assert sum(len(s["folders"]) for s in sugs) == 12


def test_min_files_and_limit(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    for name in ("Climate Study", "Opera Notes", "Garden Plan"):
        populate(catalog, a, f"x/{name}", [f"a{k}.txt" for k in range(4)])
        populate(catalog, b, f"y/{name}", [f"b{k}.txt" for k in range(4)])
    populate(catalog, a, "x/Tiny Study", ["t1.txt", "t2.txt"])
    populate(catalog, b, "y/Tiny Study", ["t3.txt", "t4.txt"])
    identity.rebuild_groups(conn)
    assert len(suggest.related_folders(conn)) == 3                              # Tiny Study has < 3 files
    assert len(suggest.related_folders(conn, min_files=2)) == 4
    assert len(suggest.related_folders(conn, limit=2)) == 2
    assert suggest.related_folders(conn, limit=0) == []
    assert suggest.related_folders(conn, min_files=5) == []


def test_remote_filter_keeps_suggestions_that_involve_the_remote(scattered):
    everything = suggest.related_folders(scattered)
    assert len(everything) == 2
    on_od = suggest.related_folders(scattered, remote="OneDrive")
    assert len(on_od) == 1 and any(f["remote"] == "OneDrive" for f in on_od[0]["folders"])
    assert on_od[0]["id"] in {s["id"] for s in everything}
    # Cross-remote candidates are still found: the OneDrive suggestion also lists folders of other remotes.
    assert {f["remote"] for f in on_od[0]["folders"]} == {"OneDrive", "Dropbox", "GDrive"}
    assert suggest.related_folders(scattered, remote="GDrive") == everything          # both topics touch GDrive
    with pytest.raises(KeyError):
        suggest.related_folders(scattered, remote="nope")


def test_ranking_follows_score_times_log_of_total_files(scattered):
    sugs = suggest.related_folders(scattered)
    keys = [s["score"] * math.log(sum(f["files"] for f in s["folders"])) for s in sugs]
    assert len(sugs) >= 2 and keys == sorted(keys, reverse=True)


def test_file_name_vocabulary_alone_is_not_evidence(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    same_words = ["invoice_acme.pdf", "invoice_globex.pdf", "invoice_initech.pdf"]
    populate(catalog, a, "Stuff2/xq", same_words)
    populate(catalog, b, "Papers/zz", same_words)             # identical vocabulary, different bytes
    identity.rebuild_groups(conn)
    assert suggest.related_folders(conn) == []
    # One shared word in the folder names is enough to make the same vocabulary count.
    populate(catalog, a, "Stuff2/Invoices Acme", same_words)
    populate(catalog, b, "Papers/Invoices Archive", same_words)
    identity.rebuild_groups(conn)
    (s,) = suggest.related_folders(conn)
    assert locs(s) == {("A", "Stuff2/Invoices Acme"), ("B", "Papers/Invoices Archive")}
    assert "invoice" in s["terms"]


def test_shared_content_pulls_differently_named_folders_together(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    pool = shared("pool", ["alpha_report.pdf", "beta_report.pdf", "gamma_report.pdf", "delta_report.pdf"])
    populate(catalog, a, "Work/quarterly", pool + ["epsilon_report.pdf"])
    populate(catalog, b, "Misc2/stuffy", pool + ["zeta_report.pdf"])
    identity.rebuild_groups(conn)
    (s,) = suggest.related_folders(conn)
    assert locs(s) == {("A", "Work/quarterly"), ("B", "Misc2/stuffy")}
    assert "4 份內容完全相同" in s["reason"]


def test_empty_and_tiny_catalogs(conn, catalog):
    assert suggest.related_folders(conn) == []
    assert suggest.duplicate_folders(conn) == []
    a = catalog.remote("A", "drive")
    populate(catalog, a, "Only/Thesis", ["a.txt", "b.txt", "c.txt"])
    assert suggest.related_folders(conn) == []
    assert suggest.duplicate_folders(conn) == []


def test_names_still_cluster_before_identity_is_built(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    populate(catalog, a, "x/Climate Study", ["a1.csv", "a2.csv", "a3.csv"])
    populate(catalog, b, "y/Climate Study", ["b1.csv", "b2.csv", "b3.csv"])
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE content_group IS NOT NULL").fetchone()[0] == 0
    assert len(suggest.related_folders(conn)) == 1
    assert suggest.duplicate_folders(conn) == []                                # nothing is proven identical yet


def test_pair_budget_shrinks_the_common_word_cap_deterministically():
    postings = {f"w{i}": list(range(i * 50, i * 50 + 50)) for i in range(40)}      # 40 words in 50 folders each
    loose = suggest._pairs_within_budget(postings, 60, 10**9, 4000)
    tight = suggest._pairs_within_budget(postings, 60, 10_000, 4000)
    assert len(loose) == 40 * (50 * 49 // 2)
    assert tight == set()                                                       # 50 > the shrunk cap: too common to use
    small = {f"w{i}": [i, i + 1, i + 2] for i in range(5)}
    assert len(suggest._pairs_within_budget(small, 60, 10_000, 100)) == len({
        a * 100 + b for lst in small.values() for a in lst for b in lst if a < b})


def test_cjk_bigrams_read_as_words():
    assert suggest._words(["研究", "究計", "計畫", "thesis"]) == ["研究計畫", "thesis"]
    assert suggest._words(["計畫", "研究"]) == ["計畫", "研究"]                    # no overlap, nothing to join
    assert suggest._words(["thesis", "draft"]) == ["thesis", "draft"]


def test_tombstoned_entries_do_not_count(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    populate(catalog, a, "x/Climate Study", ["a1.csv", "a2.csv", "a3.csv"])
    populate(catalog, b, "y/Climate Study", ["b1.csv", "b2.csv", "b3.csv"])
    identity.rebuild_groups(conn)
    assert len(suggest.related_folders(conn)) == 1
    conn.execute("UPDATE entries SET present = 0 WHERE path = 'y/Climate Study/b1.csv'")
    assert suggest.related_folders(conn) == []                                  # only 2 live files left there


# --------------------------------------------------------------------------- determinism and ids

def test_ids_are_stable_and_depend_on_kind_and_locations(scattered, catalog):
    first = suggest.related_folders(scattered)
    assert first == suggest.related_folders(scattered)
    assert len({s["id"] for s in first}) == len(first)
    main = next(s for s in first if any(f["path"] == "School/NTU/Thesis" for f in s["folders"]))
    locations = sorted(f"{f['remote']}:{f['path']}" for f in main["folders"])
    digest = hashlib.sha256("\0".join(["related", *locations]).encode()).hexdigest()[:12]
    assert main["id"] == f"related:{digest}"                                   # kind + sorted remote-name:path
    # An unrelated folder elsewhere does not change the ids of clusters that did not change.
    gd = scattered.execute("SELECT id FROM remotes WHERE name = 'GDrive'").fetchone()[0]
    populate(catalog, gd, "Receipts/2020 taxes", ["r1.pdf", "r2.pdf", "r3.pdf"])
    assert {s["id"] for s in suggest.related_folders(scattered)} == {s["id"] for s in first}


def test_output_is_identical_across_processes_with_different_hash_seeds(scattered, tmp_path):
    path = tmp_path / "catalog.db"
    scattered.execute("VACUUM INTO ?", (str(path),))
    code = (
        "import json,sys\n"
        "from drivemerge import db, suggest\n"
        "c = db.connect(sys.argv[1])\n"
        "print(json.dumps([suggest.related_folders(c), suggest.duplicate_folders(c, threshold=0.5)], sort_keys=True))\n"
    )
    outs = []
    for seed in ("1", "2", "31337"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run([sys.executable, "-c", code, str(path)], env=env, capture_output=True, text=True, check=True)
        outs.append(done.stdout)
    assert outs[0] == outs[1] == outs[2]
    assert json.loads(outs[0])[0]                                               # and it is not vacuously empty


# --------------------------------------------------------------------------- duplicate folders

def tree_files(prefix, n, *, tag):
    return shared(tag, [f"{prefix}{i}.docx" for i in range(n)])


def test_copied_tree_is_reported_once_at_its_highest_level(conn, catalog):
    dbx = catalog.remote("Dropbox", "dropbox")
    gd = catalog.remote("GDrive", "drive")
    for rid, base in ((dbx, "School"), (gd, "Archive/School")):
        populate(catalog, rid, f"{base}/Thesis", tree_files("ch", 4, tag="th"))
        populate(catalog, rid, f"{base}/Thesis/figs", shared("fig", ["f1.png", "f2.png", "f3.png"]))
        populate(catalog, rid, f"{base}/Research", shared("rs", ["r1.pdf", "r2.pdf", "r3.pdf"]))
    identity.rebuild_groups(conn)
    (s,) = suggest.duplicate_folders(conn)
    assert locs(s) == {("Dropbox", "School"), ("GDrive", "Archive/School")}
    assert s["kind"] == "duplicates" and s["id"].startswith("duplicates:")
    assert s["score"] == 1.0
    # Equal sizes: the folder on the remote listed first (Dropbox) stays, the other is merged into its parent.
    assert [f["path"] for f in s["folders"]] == ["School", "Archive/School"]
    assert s["actions"] == [{
        "kind": "move",
        "items": [{"remote": "GDrive", "path": "Archive/School"}],
        "dst": {"remote": "Dropbox", "path": ""},
        "note": s["id"],
    }]
    assert s["title"].startswith("這 2 個資料夾內容幾乎相同：")
    assert "100%" in s["reason"]


def test_deep_copies_are_found_beyond_the_four_level_window(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    deep = "x/l1/l2/l3/l4/l5"
    populate(catalog, a, deep, shared("deep", ["f1.txt", "f2.txt", "f3.txt"]))
    populate(catalog, b, "y/" + deep, shared("deep", ["f1.txt", "f2.txt", "f3.txt"]))
    populate(catalog, a, "x/side", shared("side", ["s1.txt", "s2.txt", "s3.txt"]))
    populate(catalog, b, "y/x/side", shared("side", ["s1.txt", "s2.txt", "s3.txt"]))
    identity.rebuild_groups(conn)
    (s,) = suggest.duplicate_folders(conn)
    assert locs(s) == {("A", "x"), ("B", "y/x")}                                # the top of the copied tree


def test_jaccard_threshold_and_min_files(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    base = shared("p", [f"f{i}.txt" for i in range(20)])
    populate(catalog, a, "close/orig", base)
    populate(catalog, b, "close/copy", base[:19] + [("extra.txt", "only-here")])   # 19 shared of 21 -> 0.905
    populate(catalog, a, "far/orig", shared("q", [f"g{i}.txt" for i in range(20)]))
    populate(catalog, b, "far/copy", shared("q", [f"g{i}.txt" for i in range(18)]) +
             [("e1.txt", "u1"), ("e2.txt", "u2")])                                  # 18 of 22 -> 0.818
    populate(catalog, a, "small/orig", shared("s", ["a.txt", "b.txt"]))
    populate(catalog, b, "small/copy", shared("s", ["a.txt", "b.txt"]))
    identity.rebuild_groups(conn)
    found = suggest.duplicate_folders(conn)
    # The folders have different names, so merging their (equally named) parents would not merge them:
    # the pair itself is what gets reported.
    assert [locs(s) for s in found] == [{("A", "close/orig"), ("B", "close/copy")}]
    assert 0.9 <= found[0]["score"] < 0.92
    strict = suggest.duplicate_folders(conn, threshold=0.95)
    assert strict == []
    loose = suggest.duplicate_folders(conn, threshold=0.8)
    assert {frozenset(locs(s)) for s in loose} == {frozenset({("A", "close/orig"), ("B", "close/copy")}),
                                                   frozenset({("A", "far/orig"), ("B", "far/copy")})}
    tiny = suggest.duplicate_folders(conn, min_files=2)
    assert frozenset({("A", "small/orig"), ("B", "small/copy")}) in {frozenset(locs(s)) for s in tiny}


def test_files_without_proof_count_against_a_pair(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    for rid, folder in ((a, "x/orig"), (b, "y/copy")):
        populate(catalog, rid, folder, shared("s", ["a.txt", "b.txt", "c.txt"]))
        for k in range(3):                                      # same names, but no hash: nothing proves them equal
            catalog.file(rid, f"{folder}/n{k}.txt", size=7)
    identity.rebuild_groups(conn)
    assert suggest.duplicate_folders(conn) == []                # 3 proven of 6 contents each -> 0.33


def test_ancestor_descendant_pairs_are_dropped(conn, catalog):
    a = catalog.remote("A", "drive")
    populate(catalog, a, "Docs", shared("d", ["a1.txt", "a2.txt", "a3.txt", "a4.txt"]))
    populate(catalog, a, "Docs/copy", shared("d", ["a1.txt", "a2.txt", "a3.txt", "a4.txt"]))
    identity.rebuild_groups(conn)
    assert suggest.duplicate_folders(conn) == []


def test_smaller_folder_merges_into_the_bigger_ones_parent_under_its_name(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    populate(catalog, a, "Projects/Report", shared("r", [f"r{i}.txt" for i in range(20)]))
    populate(catalog, b, "Old/Report-2019", shared("r", [f"r{i}.txt" for i in range(19)]))
    identity.rebuild_groups(conn)
    (s,) = suggest.duplicate_folders(conn)
    assert [f["path"] for f in s["folders"]] == ["Projects/Report", "Old/Report-2019"]
    assert s["actions"] == [{
        "kind": "move",
        "items": [{"remote": "B", "path": "Old/Report-2019"}],
        "dst": {"remote": "A", "path": "Projects"},
        "new_name": "Report",                                   # same name as the kept folder => a merge
        "note": s["id"],
    }]
    assert "Report" in s["terms"] or "report" in s["terms"]


def test_equal_sizes_keep_the_first_listed_remote(conn, catalog):
    first = catalog.remote("First", "drive")
    second = catalog.remote("Second", "dropbox")
    populate(catalog, second, "A/Thesis", shared("t", ["a.txt", "b.txt", "c.txt"]))     # path order would pick this
    populate(catalog, first, "Z/Thesis", shared("t", ["a.txt", "b.txt", "c.txt"]))
    populate(catalog, second, "A/other", ["o1.txt", "o2.txt", "o3.txt"])                # parents differ, so the
    populate(catalog, first, "Z/other", ["p1.txt", "p2.txt", "p3.txt"])                 # folders are reported themselves
    identity.rebuild_groups(conn)
    (s,) = suggest.duplicate_folders(conn)
    assert s["folders"][0]["remote"] == "First"
    assert s["actions"][0]["items"] == [{"remote": "Second", "path": "A/Thesis"}]


def test_three_copies_form_one_suggestion_with_one_target(conn, catalog):
    rids = [catalog.remote(n, "drive") for n in ("A", "B", "C")]
    for i, rid in enumerate(rids):
        populate(catalog, rid, f"copy{i}/Thesis", shared("t", [f"c{k}.txt" for k in range(4)]))
    identity.rebuild_groups(conn)
    (s,) = suggest.duplicate_folders(conn)
    assert len(s["folders"]) == 3 and len(s["actions"]) == 2
    target = s["folders"][0]
    assert {(a["dst"]["remote"], a["dst"]["path"]) for a in s["actions"]} == {(target["remote"], target["path"].rpartition("/")[0])}


def test_duplicates_inside_atomic_units_are_not_suggested(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    for kind, marker in (("app", ["package.json"]), ("lib", [])):      # 'app' is an atomic unit, 'lib' is not
        populate(catalog, a, f"Code/{kind}", marker + [f"a{k}.txt" for k in range(3)])
        populate(catalog, b, f"Backup/{kind}", marker + [f"b{k}.txt" for k in range(3)])
        populate(catalog, a, f"Code/{kind}/src", shared(kind, [f"s{k}.js" for k in range(4)]))
        populate(catalog, b, f"Backup/{kind}/src", shared(kind, [f"s{k}.js" for k in range(4)]))
    identity.rebuild_groups(conn)
    sugs = suggest.duplicate_folders(conn, threshold=0.5)
    assert [locs(s) for s in sugs] == [{("A", "Code/lib/src"), ("B", "Backup/lib/src")}]


def test_sibling_copies_merge_by_renaming_onto_the_kept_folder(conn, catalog):
    a = catalog.remote("A", "drive")
    populate(catalog, a, "School/Thesis", shared("t", [f"c{k}.txt" for k in range(20)]))
    populate(catalog, a, "School/Thesis (1)", shared("t", [f"c{k}.txt" for k in range(19)]))
    populate(catalog, a, "School/Notes", ["n1.txt", "n2.txt", "n3.txt"])
    identity.rebuild_groups(conn)
    (s,) = suggest.duplicate_folders(conn)
    assert s["actions"] == [{
        "kind": "move",
        "items": [{"remote": "A", "path": "School/Thesis (1)"}],
        "dst": {"remote": "A", "path": "School"},
        "new_name": "Thesis",
        "note": s["id"],
    }]


def test_duplicates_remote_filter_and_limit_and_order(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    c = catalog.remote("C", "onedrive")
    big = [(f"b{i}.bin", f"big{i}") for i in range(4)]
    small = [(f"s{i}.bin", f"small{i}") for i in range(4)]
    populate(catalog, a, "x/Big", big)
    populate(catalog, b, "y/Big", big)
    populate(catalog, a, "x/Small", small)
    populate(catalog, c, "z/Small", small)
    # Contents hashed the same must have the same size: make "big" files bigger by their content.
    conn.execute("UPDATE entries SET size = 5000 WHERE name LIKE 'b%.bin'")
    identity.rebuild_groups(conn)
    sugs = suggest.duplicate_folders(conn)
    assert [locs(s) for s in sugs] == [{("A", "x/Big"), ("B", "y/Big")}, {("A", "x/Small"), ("C", "z/Small")}]
    assert len(suggest.duplicate_folders(conn, limit=1)) == 1
    only_c = suggest.duplicate_folders(conn, remote="C")
    assert [locs(s) for s in only_c] == [{("A", "x/Small"), ("C", "z/Small")}]
    with pytest.raises(KeyError):
        suggest.duplicate_folders(conn, remote="nope")


def test_duplicate_ids_are_stable(conn, catalog):
    a = catalog.remote("A", "drive")
    b = catalog.remote("B", "dropbox")
    populate(catalog, a, "x/Thesis", shared("t", ["a.txt", "b.txt", "c.txt"]))
    populate(catalog, b, "y/Thesis", shared("t", ["a.txt", "b.txt", "c.txt"]))
    populate(catalog, a, "x/o", ["o1.txt", "o2.txt", "o3.txt"])
    populate(catalog, b, "y/p", ["p1.txt", "p2.txt", "p3.txt"])
    identity.rebuild_groups(conn)
    first = suggest.duplicate_folders(conn)
    assert first == suggest.duplicate_folders(conn)
    assert first[0]["id"] != suggest.related_folders(conn)[0]["id"]         # kind is part of the hash


# --------------------------------------------------------------------------- scale

def _bulk_catalog(conn, leaves: int, seed: int = 7) -> tuple[int, int]:
    """~`leaves` leaf folders of 4 files each over two remotes, sharing a vocabulary (a dense worst case)."""
    rnd = random.Random(seed)
    words = ["".join(rnd.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rnd.randint(4, 8))) for _ in range(1500)]
    rids = []
    for name, prov in (("A", "drive"), ("B", "dropbox")):
        rid = conn.execute(
            "INSERT INTO remotes(name, provider, read_remote, write_remote, case_insensitive, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?) RETURNING id", (name, prov, f"{name}:", f"{name}:", db.utcnow())).fetchone()[0]
        scan = conn.execute("INSERT INTO scans(remote_id, status, started_at, finished_at) "
                            "VALUES (?, 'complete', ?, ?) RETURNING id", (rid, db.utcnow(), db.utcnow())).fetchone()[0]
        rids.append((rid, scan))
    conn.execute("DROP TRIGGER entries_fts_ai")                   # full-text indexing is irrelevant here and slow
    rows, dirs, version = [], set(), 0

    def add(rid, scan, path, is_dir, size=None, md5=None):
        nonlocal version
        version += 1
        parent, _, name = path.rpartition("/")
        rows.append((rid, f"p:{path}", path, parent, name, name.casefold(), "", "other", int(is_dir), size,
                     "2024-01-01T00:00:00Z", md5, scan, scan, version))

    def ensure(rid, scan, folder):
        parts = folder.split("/")
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            if (rid, p) not in dirs:
                dirs.add((rid, p))
                add(rid, scan, p, True)

    made = []
    for i in range(leaves):
        rid, scan = rids[i % 2]
        folder = f"{words[rnd.randrange(60)]}/{words[60 + rnd.randrange(60)]}/{rnd.choice(words)} {rnd.choice(words)}-{i}"
        ensure(rid, scan, folder)
        files = [(f"{rnd.choice(words)}_{rnd.choice(words)}.pdf", f"h{i}-{k}") for k in range(4)]
        for fname, h in files:
            add(rid, scan, f"{folder}/{fname}", False, 100, h)
        made.append(files)
        if i > 10 and rnd.random() < 0.1:                      # a copy of an earlier folder on the other remote
            rid2, scan2 = rids[(i + 1) % 2]
            copy = f"{rnd.choice(words)}/{rnd.choice(words)}/copy{i}"
            ensure(rid2, scan2, copy)
            for fname, h in made[rnd.randrange(len(made) - 1)]:
                add(rid2, scan2, f"{copy}/{fname}", False, 100, h)
    conn.executemany(
        "INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category, is_dir, size, "
        "mtime, md5, first_seen_scan, last_seen_scan, row_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    identity.rebuild_groups(conn)
    return len(dirs), len(rows)


def test_twenty_thousand_folders_in_seconds(conn):
    n_dirs, n_rows = _bulk_catalog(conn, 17000)
    assert n_dirs >= 20000
    started = time.perf_counter()
    related = suggest.related_folders(conn)
    related_seconds = time.perf_counter() - started
    started = time.perf_counter()
    dups = suggest.duplicate_folders(conn)
    dup_seconds = time.perf_counter() - started
    assert related_seconds < 10 and dup_seconds < 10, (related_seconds, dup_seconds, n_dirs, n_rows)
    assert len(related) == 20 and len(dups) == 20
    assert all(2 <= len(s["folders"]) <= suggest.MAX_CLUSTER for s in related + dups)
