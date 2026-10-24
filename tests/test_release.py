"""release_report / build_release: what can be freed on one cloud, and the evidence for it."""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from drivemerge import identity
from drivemerge.builders import BuildError
from drivemerge.coverage import coverage
from drivemerge.release import build_release, release_report
from drivemerge.simulate import simulate_ops

Q = ".drive-merge-quarantine"
NATIVE = "application/vnd.google-apps.document"


def sha(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


def md5(tag: str) -> str:
    return hashlib.md5(tag.encode()).hexdigest()


def dbx(tag: str) -> str:
    return hashlib.sha256(b"d" + tag.encode()).hexdigest()


@pytest.fixture
def w(catalog):
    """gdrive (sha256), dropbox (dropbox hash), onedrive (md5/quickxor only), icloud (nothing)."""
    c = catalog
    c.g = c.remote("gdrive", "drive")
    c.d = c.remote("dropbox", "dropbox")
    c.o = c.remote("onedrive", "onedrive")
    return c


def regroup(c):
    identity.rebuild_groups(c.conn)


def report(c, remote="dropbox", **kw):
    return release_report(c.conn, remote, **kw)


def test_proven_via_sha256(w):
    w.file(w.d, "a.bin", size=10, sha256=sha("a"))
    w.file(w.g, "x/a.bin", size=10, sha256=sha("a"))
    regroup(w)
    r = report(w)
    assert r["proven"]["files"] == 1 and r["proven"]["bytes"] == 10
    assert r["proven"]["by_remote"] == {"gdrive": 10}
    assert r["only_here"]["files"] == r["probable"]["files"] == r["unjudged"]["files"] == 0


def test_proven_via_dropbox_hash(w):
    w.file(w.g, "a.bin", size=10, dropbox=dbx("a"))
    w.file(w.d, "b.bin", size=10, dropbox=dbx("a"))
    regroup(w)
    assert report(w, "gdrive")["proven"]["files"] == 1


def test_proven_via_bridge(w):
    od = w.file(w.o, "r.bin", size=320, md5=md5("r"), quickxor="ab" * 10, mtime="2024-01-01T00:00:00Z")
    w.file(w.d, "r2.bin", size=320, dropbox=dbx("r"))
    regroup(w)
    r = report(w, "onedrive")
    assert r["proven"]["files"] == 0 and r["probable"]["files"] == 0 and r["only_here"]["files"] == 1
    identity.record_bridge(w.conn, {"md5": md5("r"), "sha1": hashlib.sha1(b"r").hexdigest(), "sha256": sha("r"),
                                    "dropbox": dbx("r"), "quickxor": "ab" * 10, "size": 320},
                           source_entry_id=od, source_mtime="2024-01-01T00:00:00Z")
    regroup(w)
    r = report(w, "onedrive")
    assert r["proven"]["files"] == 1 and r["proven"]["by_remote"] == {"dropbox": 320}
    # bridge goes stale when the file changes: back to unproven
    w.conn.execute("UPDATE entries SET mtime = '2025-01-01T00:00:00Z' WHERE id = ?", (od,))
    regroup(w)
    assert report(w, "onedrive")["proven"]["files"] == 0


def test_md5_equality_is_only_probable(w):
    w.file(w.o, "one.bin", size=5, md5=md5("m"))
    w.file(w.g, "different-name.bin", size=5, md5=md5("m"), sha256=sha("zzz"))
    regroup(w)
    r = report(w, "onedrive")
    assert (r["proven"]["files"], r["probable"]["files"], r["only_here"]["files"]) == (0, 1, 0)
    assert build_release(w.conn, remote="onedrive").ops == []


def test_quickxor_and_sha1_equality_are_probable(w):
    a = w.file(w.o, "q.bin", size=7, quickxor="cd" * 10)
    w.file(w.d, "zzz.bin", size=7, quickxor="cd" * 10)
    b = w.file(w.o, "s.bin", size=8, sha1="1" * 40)
    w.file(w.g, "yyy.bin", size=8, sha1="1" * 40)
    regroup(w)
    r = report(w, "onedrive")
    assert r["probable"]["files"] == 2 and sorted(r["probable"]["entry_ids"]) == sorted([a, b])
    assert r["proven"]["files"] == 0


def test_probable_by_name_and_size(w):
    w.file(w.o, "docs/Report.PDF", size=9)
    w.file(w.d, "elsewhere/report.pdf", size=9, dropbox=dbx("x"))
    w.file(w.o, "docs/other.pdf", size=9)
    w.file(w.d, "elsewhere/other.pdf", size=10, dropbox=dbx("y"))   # same name, other size
    regroup(w)
    r = report(w, "onedrive")
    assert r["probable"]["files"] == 1 and r["only_here"]["files"] == 1
    assert r["probable"]["entry_ids_total"] == 1


def test_contradicting_hash_is_not_probable(w):
    w.file(w.o, "a.bin", size=9, md5=md5("1"))
    w.file(w.d, "a.bin", size=9, md5=md5("2"))      # same name and size, different md5: different bytes
    w.file(w.o, "b.bin", size=9, md5=md5("3"))
    w.file(w.g, "b.bin", size=9, sha256=sha("b"), md5=md5("4"))
    regroup(w)
    r = report(w, "onedrive")
    assert r["probable"]["files"] == 0 and r["only_here"]["files"] == 2


def test_only_here_and_native_and_unknown_size_are_unjudged(w):
    w.file(w.d, "solo.bin", size=4, dropbox=dbx("solo"))
    w.file(w.d, "paper.doc", size=None, native_doc=True, mime=NATIVE)
    w.file(w.d, "weird", size=None)
    w.file(w.g, "paper.doc", size=None, native_doc=True, mime=NATIVE)
    w.file(w.g, "weird", size=None)
    regroup(w)
    r = report(w)
    assert r["only_here"] == {"files": 1, "bytes": 4}
    assert r["unjudged"]["files"] == 2 and r["proven"]["files"] == 0 and r["probable"]["files"] == 0
    b = build_release(w.conn, remote="dropbox")
    assert b.ops == [] and {s["code"] for s in b.skipped} == {"NATIVE_DOC", "SIZE_UNKNOWN"}


def test_witness_in_quarantine_or_on_same_remote_does_not_count(w):
    w.file(w.d, "a.bin", size=3, sha256=sha("a"))
    w.file(w.g, f"{Q}/a.bin", size=3, sha256=sha("a"))          # deleted, waiting for purge
    w.file(w.d, "copy/a.bin", size=3, sha256=sha("a"))          # same cloud
    w.file(w.d, "b.bin", size=3, sha256=sha("b"))
    w.file(w.g, f"{Q}x/b.bin", size=3, sha256=sha("b"))          # NOT quarantine (segment boundary): counts
    regroup(w)
    r = report(w)
    assert r["only_here"] == {"files": 2, "bytes": 6}
    assert r["proven"]["files"] == 1
    # the quarantined file of the cloud itself is not a live file to report
    w.file(w.d, f"{Q}/old.bin", size=100, sha256=sha("old"))
    regroup(w)
    assert report(w)["total"]["files"] == 3
    rep = simulate_ops(w.conn, build_release(w.conn, remote="dropbox").ops)
    assert rep.ok


def test_ambiguous_witness_path_is_unusable_and_not_only_here(w):
    w.file(w.d, "a.bin", size=3, sha256=sha("a"))
    w.file(w.g, "dup/a.bin", size=3, sha256=sha("a"), provider_id="1")
    w.file(w.g, "dup/a.bin", size=3, sha256=sha("zzz"), provider_id="2")   # two live entries at the path
    regroup(w)
    r = report(w)
    assert r["proven"]["files"] == 0 and r["only_here"]["files"] == 0 and r["elsewhere_unusable"]["files"] == 1
    assert build_release(w.conn, remote="dropbox").ops == []


def test_source_at_ambiguous_path_is_unjudged(w):
    w.file(w.g, "a.bin", size=3, sha256=sha("a"))
    w.file(w.d, "dup/a.bin", size=3, dropbox=dbx("a"), provider_id="1")
    w.file(w.d, "dup/a.bin", size=3, dropbox=dbx("a"), provider_id="2")
    regroup(w)
    r = report(w)
    assert r["unjudged"]["files"] == 2 and r["proven"]["files"] == 0
    assert {s["code"] for s in build_release(w.conn, remote="dropbox").skipped} == {"AMBIGUOUS_PATH"}


def test_keep_on_restricts_witnesses_and_orders_them(w):
    w.file(w.d, "a.bin", size=3, dropbox=dbx("a"), sha256=sha("a"))
    w.file(w.g, "a.bin", size=3, sha256=sha("a"))
    w.file(w.o, "a.bin", size=3, sha256=sha("a"))
    regroup(w)
    assert report(w, keep_on=["gdrive"])["proven"]["by_remote"] == {"gdrive": 3}
    assert report(w, keep_on=["onedrive", "gdrive"])["proven"]["by_remote"] == {"onedrive": 3}
    (op,) = build_release(w.conn, remote="dropbox", keep_on=["onedrive", "gdrive"]).ops
    assert op.witness_remote_id == w.o
    (op,) = build_release(w.conn, remote="dropbox", keep_on=["gdrive", "onedrive"]).ops
    assert op.witness_remote_id == w.g
    # copy only on a cloud outside keep_on: not proven, and not "only here" either
    w.file(w.d, "b.bin", size=4, sha256=sha("b"))
    w.file(w.o, "b.bin", size=4, sha256=sha("b"))
    regroup(w)
    r = report(w, keep_on=["gdrive"])
    assert r["proven"]["files"] == 1 and r["elsewhere_unusable"]["files"] == 1 and r["only_here"]["files"] == 0
    assert len(build_release(w.conn, remote="dropbox", keep_on=["gdrive"]).ops) == 1
    # probable candidates are restricted to keep_on too
    w.file(w.d, "c.bin", size=5)
    w.file(w.o, "c.bin", size=5)
    assert report(w, keep_on=["gdrive"])["probable"]["files"] == 0
    assert report(w, keep_on=["onedrive"])["probable"]["files"] == 1
    with pytest.raises(BuildError) as e:
        report(w, keep_on=["nope"])
    assert e.value.code == "UNKNOWN_REMOTE"


def test_path_prefix_is_by_whole_segments(w):
    for p in ("Photos/a.jpg", "Photos2/b.jpg", "Photos/sub/c.jpg", "Photosx.jpg", "photos/d.jpg"):
        w.file(w.d, p, size=1, sha256=sha(p))
        w.file(w.g, p, size=1, sha256=sha(p))
    regroup(w)
    assert report(w, path_prefix="Photos")["proven"]["files"] == 2
    assert report(w, path_prefix="/Photos/")["proven"]["files"] == 2
    assert report(w, path_prefix="Photos/sub")["proven"]["files"] == 1
    ops = build_release(w.conn, remote="dropbox", path_prefix="Photos").ops
    assert sorted(o.src_path for o in ops) == ["Photos/a.jpg", "Photos/sub/c.jpg"]


def test_duplicates_inside_the_cloud_count_per_file(w):
    w.file(w.d, "a/x.bin", size=10, sha256=sha("x"))
    w.file(w.d, "b/x.bin", size=10, sha256=sha("x"))
    w.file(w.d, "a/y.bin", size=20, sha256=sha("y"))
    w.file(w.d, "b/y.bin", size=20, sha256=sha("y"))
    w.file(w.g, "x.bin", size=10, sha256=sha("x"))
    regroup(w)
    r = report(w)
    assert r["proven"] == {"files": 2, "bytes": 20, "by_remote": {"gdrive": 20}}
    assert r["only_here"] == {"files": 2, "bytes": 40}      # y is not elsewhere: both files, not 20
    rep = simulate_ops(w.conn, build_release(w.conn, remote="dropbox").ops)
    assert rep.ok and rep.contents_lost == 0


def test_freshness(w):
    w.file(w.d, "a.bin", size=1, sha256=sha("a"))
    w.file(w.g, "a.bin", size=1, sha256=sha("a"))
    w.file(w.o, "u.bin", size=1, sha256=sha("u"))        # holds no witness: not listed
    regroup(w)
    now = datetime.now(timezone.utc)
    r = report(w, now=now)
    assert set(r["freshness"]) == {"dropbox", "gdrive"} and not r["stale"]
    old = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    w.conn.execute("UPDATE scans SET finished_at = ? WHERE remote_id = ?", (old, w.g))
    r = report(w)
    assert r["freshness"]["gdrive"]["stale"] and not r["freshness"]["dropbox"]["stale"] and r["stale"]
    assert not report(w, stale_after_hours=48)["freshness"]["gdrive"]["stale"]
    w.conn.execute("UPDATE scans SET status = 'failed' WHERE remote_id = ?", (w.d,))
    f = report(w)["freshness"]["dropbox"]
    assert f["synced_at"] is None and f["stale"]


def test_only_here_agrees_with_coverage(w):
    for i in range(6):
        w.file(w.d, f"only{i}.bin", size=10 + i, sha256=sha(f"o{i}"), dropbox=dbx(f"o{i}"))
    for i in range(4):
        w.file(w.d, f"both{i}.bin", size=100 + i, dropbox=dbx(f"b{i}"))
        w.file(w.g, f"both{i}.bin", size=100 + i, sha256=sha(f"b{i}"), dropbox=dbx(f"b{i}"))
    w.file(w.d, f"{Q}/ghost.bin", size=9999, dropbox=dbx("ghost"))
    w.file(w.g, f"{Q}/o0.bin", size=10, sha256=sha("o0"), dropbox=dbx("o0"))
    w.file(w.d, "native", size=None, native_doc=True, mime=NATIVE)
    regroup(w)
    r = report(w)
    cov = next(x for x in coverage(w.conn)["remotes"] if x["name"] == "dropbox")
    assert r["only_here"]["files"] == cov["only_here"]["contents"] == 6
    assert r["only_here"]["bytes"] == cov["only_here"]["bytes"]
    assert r["proven"]["files"] == 4


def test_build_release_plans_are_safe(w):
    rng = random.Random(1)
    for i in range(40):
        t = f"t{i}"
        place = rng.sample(["d", "g", "o"], rng.randint(1, 3))
        for p in place:
            r = {"d": w.d, "g": w.g, "o": w.o}[p]
            w.file(r, f"f{rng.randint(0, 3)}/{t}.bin", size=10 + i, sha256=sha(t), dropbox=dbx(t))
    regroup(w)
    for remote in ("dropbox", "gdrive", "onedrive"):
        for keep in (None, ["gdrive"], ["onedrive", "dropbox"]):
            if keep and remote in keep:
                continue
            res = build_release(w.conn, remote=remote, keep_on=keep)
            rep = simulate_ops(w.conn, res.ops)
            assert rep.ok, [(v.code, v.message) for v in rep.violations]
            assert rep.contents_lost == 0
            assert len(res.ops) == report(w, remote, keep_on=keep)["proven"]["files"]
            for op in res.ops:
                assert op.op == "delete" and op.witness_remote_id != op.src_remote_id
                assert op.reason.startswith(f"release {remote}: copy on ")
                if keep:
                    assert op.witness_remote_id in {w.conn.execute('SELECT id FROM remotes WHERE name=?', (k,)).fetchone()[0] for k in keep}


def test_two_clouds_can_both_be_released_only_if_each_keeps_a_copy(w):
    """Plans are per cloud: the first plan's quarantined file no longer vouches for the second."""
    w.file(w.d, "a.bin", size=3, sha256=sha("a"))
    w.file(w.g, "a.bin", size=3, sha256=sha("a"))
    regroup(w)
    assert len(build_release(w.conn, remote="dropbox").ops) == 1
    assert len(build_release(w.conn, remote="gdrive").ops) == 1      # each alone is safe; never both at once


def test_read_only_remote_is_refused(catalog):
    ro = catalog.remote("ro", "drive", writable=False)
    g = catalog.remote("gdrive", "drive")
    catalog.file(ro, "a.bin", size=3, sha256=sha("a"))
    catalog.file(g, "a.bin", size=3, sha256=sha("a"))
    regroup(catalog)
    assert release_report(catalog.conn, "ro")["proven"]["files"] == 1
    with pytest.raises(BuildError) as e:
        build_release(catalog.conn, remote="ro")
    assert e.value.code == "READ_ONLY"
    with pytest.raises(BuildError) as e:
        build_release(catalog.conn, remote="missing")
    assert e.value.code == "UNKNOWN_REMOTE"
    # a read-only remote can still be the witness
    assert len(build_release(catalog.conn, remote="gdrive").ops) == 1


def test_single_remote_and_empty(catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "a.bin", size=3, sha256=sha("a"))
    regroup(catalog)
    r = release_report(catalog.conn, "gdrive")
    assert r["only_here"]["files"] == 1 and r["proven"]["files"] == 0
    assert release_report(catalog.conn, "gdrive", path_prefix="nothing")["total"] == {"files": 0, "bytes": 0}


def test_performance_100k(catalog):
    c, conn = catalog, catalog.conn
    d = c.remote("dropbox", "dropbox")
    g = c.remote("gdrive", "drive")
    o = c.remote("onedrive", "onedrive")
    rows = []
    scan = c._scan
    rv = 1
    for i in range(100_000):
        h = sha(str(i % 60_000))
        kind = i % 5
        rid = (d, g, o)[i % 3]
        rows.append((rid, f"p:{i}", f"dir{i % 50}/f{i}.bin", f"dir{i % 50}", f"f{i}.bin", f"f{i}.bin", "bin", "other",
                     0, 100 + i % 60_000, "2024-01-01T00:00:00Z", None, None,
                     md5(str(i % 60_000)) if rid == o else None, None, h if rid == g else None,
                     dbx(str(i % 60_000)) if rid == d else None, None, 0, 1, scan[rid], scan[rid], rv + i))
    conn.executemany(
        "INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category, is_dir, size, mtime, "
        "mime, provider_id, md5, sha1, sha256, dropbox, quickxor, is_native_doc, present, first_seen_scan, "
        "last_seen_scan, row_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    identity.rebuild_groups(conn)
    t = time.time()
    r = release_report(conn, "dropbox", keep_on=["gdrive", "onedrive"])
    res = build_release(conn, remote="dropbox")
    assert time.time() - t < 5
    assert r["total"]["files"] > 30_000 and len(res.ops) == release_report(conn, "dropbox")["proven"]["files"]
