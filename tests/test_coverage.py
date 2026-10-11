"""Tests for drivemerge.coverage: intersections, `other` bucketing, only_here, unverified files, categories."""

from __future__ import annotations

import pytest

from drivemerge import db
from drivemerge.compare import CompareError
from drivemerge.coverage import coverage

QUARANTINE = db.DEFAULT_QUARANTINE_DIR


def group(conn, number, *ids):
    for i in ids:
        conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (number, i))


@pytest.fixture
def world(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    o = catalog.remote("onedrive", "onedrive", writable=False)
    a = [catalog.file(g, "a.jpg", size=100), catalog.file(d, "a.jpg", size=100), catalog.file(d, "a2.jpg", size=100),
         catalog.file(o, "a.jpg", size=100)]
    group(conn, 1, *a)                                          # on all three, 4 copies
    b = [catalog.file(g, "b.pdf", size=50), catalog.file(d, "b.pdf", size=50)]
    group(conn, 2, *b)                                          # gdrive + dropbox
    c = catalog.file(g, "c.txt", size=7)
    group(conn, 3, c)                                           # only gdrive
    catalog.file(d, "nohash.bin", size=30)                      # ungrouped: its own content, only dropbox
    catalog.file(g, "empty", size=0)                            # placeholder, not content
    catalog.file(g, "gone.txt", size=9, present=False)
    catalog.file(o, f"{QUARANTINE}/x/a.jpg", size=100)          # parked: not a copy
    return conn


def by_names(res):
    return {tuple(b["remotes"]): b for b in res["intersections"]}


def test_intersections_totals_and_remotes(world):
    res = coverage(world)
    bars = by_names(res)
    assert bars[("gdrive", "dropbox", "onedrive")] == {
        "remotes": ["gdrive", "dropbox", "onedrive"], "contents": 1, "bytes": 100, "copies": 4, "stored_bytes": 400}
    assert bars[("gdrive", "dropbox")] == {"remotes": ["gdrive", "dropbox"], "contents": 1, "bytes": 50,
                                           "copies": 2, "stored_bytes": 100}
    assert bars[("gdrive",)]["contents"] == 1 and bars[("dropbox",)]["bytes"] == 30
    assert ("onedrive",) not in bars
    assert [b["bytes"] for b in res["intersections"]] == [100, 50, 30, 7]          # bytes desc
    assert res["other"] is None
    assert res["totals"] == {"contents": 4, "bytes": 187, "stored_bytes": 400 + 100 + 7 + 30,
                             "redundant_bytes": 400 + 100 + 7 + 30 - 187, "copies": 8}
    names = [r["name"] for r in res["remotes"]]
    assert names == ["gdrive", "dropbox", "onedrive"]                              # configuration order


def test_per_remote_cards(world):
    g, d, o = coverage(world)["remotes"]
    assert (g["provider"], g["writable"]) == ("drive", True) and o["writable"] is False
    assert (g["files"], g["bytes"]) == (4, 100 + 50 + 7 + 0)            # live files, the empty one included
    assert (d["files"], d["bytes"]) == (4, 100 + 100 + 50 + 30)
    assert (o["files"], o["bytes"]) == (1, 100)                         # quarantined file excluded
    assert g["only_here"] == {"contents": 1, "bytes": 7}
    assert d["only_here"] == {"contents": 1, "bytes": 30}                # the ungrouped file counts as its own content
    assert o["only_here"] == {"contents": 0, "bytes": 0}
    assert (g["unverified_files"], d["unverified_files"], o["unverified_files"]) == (0, 1, 0)
    assert g["last_scan"]["status"] == "complete" and set(g["last_scan"]) == {"id", "status", "finished_at"}


def test_last_scan_is_the_last_completed_one(world):
    world.execute("INSERT INTO scans(remote_id, status, started_at) VALUES (1, 'failed', 'x')")
    done = world.execute("SELECT MAX(id) FROM scans WHERE remote_id = 1 AND status = 'complete'").fetchone()[0]
    assert coverage(world)["remotes"][0]["last_scan"]["id"] == done


def test_other_bucket_sums_the_combinations_beyond_max_bars(world):
    res = coverage(world, max_bars=2)
    assert [b["bytes"] for b in res["intersections"]] == [100, 50]
    assert res["other"] == {"combinations": 2, "contents": 2, "bytes": 37, "copies": 2, "stored_bytes": 37}
    one = coverage(world, max_bars=1)
    assert one["other"]["combinations"] == 3
    # bars + other always add up to the totals
    for r in (res, one):
        assert sum(b["contents"] for b in r["intersections"]) + r["other"]["contents"] == r["totals"]["contents"]


def test_ties_sort_by_contents_then_names(conn, catalog):
    g = catalog.remote("g", "drive")
    d = catalog.remote("d", "dropbox")
    catalog.file(g, "x", size=10)
    catalog.file(d, "y", size=10)
    catalog.file(d, "z", size=5)
    catalog.file(d, "w", size=5)
    res = coverage(conn)
    assert [b["remotes"] for b in res["intersections"]] == [["d"], ["g"]]    # 15 bytes vs 10


def test_category_filter_recomputes_everything(world):
    res = coverage(world, category="photo")
    assert res["totals"]["contents"] == 1 and res["totals"]["bytes"] == 100
    assert [b["remotes"] for b in res["intersections"]] == [["gdrive", "dropbox", "onedrive"]]
    g, d, o = res["remotes"]
    assert (g["files"], d["files"], o["files"]) == (1, 2, 1)
    assert d["unverified_files"] == 0 and g["only_here"]["contents"] == 0
    assert coverage(world, category="video")["totals"]["contents"] == 0


def test_empty_catalog(conn):
    res = coverage(conn)
    assert res["remotes"] == [] and res["intersections"] == [] and res["other"] is None
    assert res["totals"] == {"contents": 0, "bytes": 0, "stored_bytes": 0, "redundant_bytes": 0, "copies": 0}


@pytest.mark.parametrize("bad", [0, -1, "3", None])
def test_bad_max_bars(conn, bad):
    with pytest.raises(CompareError) as exc:
        coverage(conn, max_bars=bad)
    assert (exc.value.code, exc.value.params, exc.value.status) == ("BAD_QUERY", {"param": "max_bars"}, 400)


def test_coverage_does_not_write(world):
    before = world.total_changes
    coverage(world)
    assert world.total_changes == before
