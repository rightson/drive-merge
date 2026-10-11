"""Content identity: these tests back delete-safety decisions, so every
'not grouped' case matters as much as every 'grouped' one."""

from __future__ import annotations

import hashlib
import random
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from drivemerge import db, hashing, identity
from drivemerge.identity import (
    bridge_candidates, bridge_entry, content_key, duplicate_groups, locations,
    rebuild_groups, record_bridge, same_content,
)
from drivemerge.providers import HASH_TYPES


# --------------------------------------------------------------------------- helpers

def H(tag: str, size: int = 10) -> dict:
    """Five deterministic hashes (+ size) for the imaginary content `tag`."""
    b = tag.encode()
    return {
        "md5": hashlib.md5(b).hexdigest(), "sha1": hashlib.sha1(b).hexdigest(),
        "sha256": hashlib.sha256(b).hexdigest(),
        "dropbox": hashlib.sha256(b"dbx" + b).hexdigest(), "quickxor": hashlib.sha1(b"qx" + b).hexdigest(),
        "size": size,
    }


def drive(cat, rid, path, tag, size=10, **kw):
    h = H(tag)
    return cat.file(rid, path, size=size, md5=h["md5"], sha1=h["sha1"], sha256=h["sha256"], **kw)


def dropbox(cat, rid, path, tag, size=10, **kw):
    return cat.file(rid, path, size=size, dropbox=H(tag)["dropbox"], **kw)


def onedrive(cat, rid, path, tag, size=10, **kw):
    return cat.file(rid, path, size=size, quickxor=H(tag)["quickxor"], **kw)


def icloud(cat, rid, path, size=10, **kw):
    return cat.file(rid, path, size=size, **kw)


def groups(conn) -> dict[int, int | None]:
    return {r["id"]: r["content_group"] for r in conn.execute("SELECT id, content_group FROM entries")}


def versions(conn) -> dict[int, int]:
    return {r["id"]: r["row_version"] for r in conn.execute("SELECT id, row_version FROM entries")}


def meta_version(conn) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key = 'row_version'").fetchone()[0])


def row(conn, entry_id):
    return conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


@pytest.fixture
def clouds(catalog):
    """One remote per provider, as the real setup has."""
    return {
        "d1": catalog.remote("d1", "drive"), "d2": catalog.remote("d2", "drive"),
        "db": catalog.remote("db", "dropbox"), "od": catalog.remote("od", "onedrive"),
        "ic": catalog.remote("ic", "iclouddrive"),
    }


# --------------------------------------------------------------------------- rebuild_groups

def test_same_md5_same_size_on_two_drives_is_one_group(conn, catalog, clouds):
    a = drive(catalog, clouds["d1"], "x/a.jpg", "A")
    b = drive(catalog, clouds["d2"], "y/b.jpg", "A")
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == g[b] == min(a, b)
    assert stats.duplicate_groups == 1 and stats.redundant_files == 1


def test_same_hash_different_size_is_not_grouped(conn, catalog, clouds):
    a = catalog.file(clouds["d1"], "a.bin", size=10, sha256="aa")
    b = catalog.file(clouds["d2"], "b.bin", size=11, sha256="aa")
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == a and g[b] == b          # each hash-identified, but alone
    assert stats.duplicate_groups == 0
    assert stats.size_conflicts == 1


def test_size_conflict_is_counted_once_per_hash_value(conn, catalog, clouds):
    for i, size in enumerate((10, 11, 12, 12)):
        catalog.file(clouds["d1"], f"f{i}.bin", size=size, sha256="aa")
    stats = rebuild_groups(conn)
    assert stats.size_conflicts == 1
    assert stats.duplicate_groups == 1       # the two size-12 files still match each other


def test_the_hash_types_are_split_into_strong_and_weak():
    assert identity.STRONG_TYPES == ("sha256", "dropbox")
    assert identity.WEAK_TYPES == ("md5", "sha1", "quickxor")
    assert set(identity.STRONG_TYPES) | set(identity.WEAK_TYPES) == set(HASH_TYPES)


@pytest.mark.parametrize("hash_type", identity.STRONG_TYPES)
def test_every_strong_hash_type_links_entries(conn, catalog, hash_type):
    r = catalog.remote("loc", "local")
    a = catalog.file(r, "a", size=5, **{hash_type: "ab12"})
    b = catalog.file(r, "b", size=5, **{hash_type: "ab12"})
    c = catalog.file(r, "c", size=5, **{hash_type: "ff00"})
    rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == g[b] == a
    assert g[c] == c


@pytest.mark.parametrize("hash_type", identity.WEAK_TYPES)
def test_a_weak_hash_never_links_entries_and_never_identifies_one(conn, catalog, hash_type):
    """md5 and QuickXorHash agreeing is what two different files can do (QuickXor: any two same-size files whose
    160-byte rows are a permutation of each other): the entries stay unidentified until their bytes are hashed."""
    r = catalog.remote("loc", "local")
    a = catalog.file(r, "x/a.bin", size=5, **{hash_type: "ab12"})
    b = catalog.file(r, "y/a.bin", size=5, **{hash_type: "ab12"})
    stats = rebuild_groups(conn)
    assert groups(conn)[a] is None and groups(conn)[b] is None
    assert (stats.files_hashed, stats.groups, stats.duplicate_groups) == (0, 0, 0)
    assert duplicate_groups(conn) == [] and not same_content(conn, a, b)
    (probable,) = duplicate_groups(conn, confidence="probable")          # a candidate, never a proof
    assert sorted(c.entry_id for c in probable.copies) == [a, b]


def test_transitive_chain_through_mixed_hash_types(conn, catalog, clouds):
    """Drive --sha256-- local --dropbox-- Dropbox: no direct shared hash between the ends."""
    loc = catalog.remote("loc", "local")
    d = catalog.file(clouds["d1"], "d", size=7, sha256="m1")
    mid = catalog.file(loc, "mid", size=7, sha256="m1", dropbox="dd")
    b = catalog.file(clouds["db"], "b", size=7, dropbox="dd")
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[mid] == g[b] == min(d, mid, b)


def test_content_group_is_smallest_id_and_singletons_are_grouped(conn, catalog, clouds):
    first = drive(catalog, clouds["d1"], "a", "A")
    lone = drive(catalog, clouds["d1"], "lone", "L", size=3)
    third = drive(catalog, clouds["d2"], "c", "A")
    rebuild_groups(conn)
    g = groups(conn)
    assert g[first] == g[third] == first
    assert g[lone] == lone


def test_unhashed_file_has_null_group_and_is_never_exact(conn, catalog, clouds):
    ic = icloud(catalog, clouds["ic"], "Photos/a.jpg")
    ic2 = icloud(catalog, clouds["ic"], "Photos/b.jpg")
    d = drive(catalog, clouds["d1"], "Photos/a.jpg", "A")
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[ic] is None and g[ic2] is None
    assert g[d] == d
    assert stats.files_hashed == 1
    assert duplicate_groups(conn) == []


def test_empty_string_hashes_are_not_hashes(conn, catalog, clouds):
    a = catalog.file(clouds["d1"], "a", size=4, md5="", sha1="")
    b = catalog.file(clouds["d2"], "b", size=4, md5="", sha1="")
    rebuild_groups(conn)
    g = groups(conn)
    assert g[a] is None and g[b] is None


def test_non_live_rows_are_never_grouped(conn, catalog, clouds):
    live = drive(catalog, clouds["d1"], "live", "A")
    tomb = drive(catalog, clouds["d2"], "tomb", "A", present=False)
    doc = drive(catalog, clouds["d2"], "doc", "A", native_doc=True)
    nosize = catalog._insert(clouds["d2"], "nosize", is_dir=False, size=None, md5=H("A")["md5"])
    folder = catalog.dir(clouds["d2"], "folder")
    conn.execute("UPDATE entries SET md5 = ? WHERE id = ?", (H("A")["md5"], folder))   # a dir that somehow carries a hash
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[live] == live
    assert g[tomb] is None and g[doc] is None and g[nosize] is None and g[folder] is None
    assert stats.files_hashed == 1 and stats.duplicate_groups == 0


def test_tombstoning_clears_group_and_regroups_survivors(conn, catalog, clouds):
    a = drive(catalog, clouds["d1"], "a", "A")
    b = drive(catalog, clouds["d1"], "b", "A")
    c = drive(catalog, clouds["d2"], "c", "A")
    rebuild_groups(conn)
    assert groups(conn)[c] == a
    conn.execute("UPDATE entries SET present = 0 WHERE id = ?", (a,))
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[a] is None
    assert g[b] == g[c] == b
    assert stats.changed == 3


def test_hash_change_splits_group(conn, catalog, clouds):
    a = drive(catalog, clouds["d1"], "a", "A")
    b = drive(catalog, clouds["d2"], "b", "A")
    rebuild_groups(conn)
    conn.execute("UPDATE entries SET md5 = ?, sha1 = ?, sha256 = ? WHERE id = ?",
                 (*(H("B")[t] for t in ("md5", "sha1", "sha256")), b))
    rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == a and g[b] == b


def test_empty_catalog(conn):
    assert rebuild_groups(conn) == identity.GroupStats()


def test_stats(conn, catalog, clouds):
    for i in range(3):                                   # 3 copies of A (size 10)
        drive(catalog, clouds["d1"], f"a{i}", "A", size=10)
    for i in range(2):                                   # 2 copies of B (size 100)
        drive(catalog, clouds["d2"], f"b{i}", "B", size=100)
    drive(catalog, clouds["d1"], "single", "C", size=5)
    icloud(catalog, clouds["ic"], "nohash")
    stats = rebuild_groups(conn)
    assert (stats.files_hashed, stats.groups, stats.duplicate_groups) == (6, 3, 2)
    assert stats.redundant_files == 3
    assert stats.redundant_bytes == 2 * 10 + 1 * 100
    assert stats.changed == 6


# -- bridges

def test_drive_and_dropbox_are_unlinked_until_bridged(conn, catalog, clouds):
    d = drive(catalog, clouds["d1"], "doc.pdf", "A", size=500)
    b = dropbox(catalog, clouds["db"], "doc.pdf", "A", size=500)
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] != g[b]                                   # same name, same size, still not proof

    record_bridge(conn, H("A", 500))
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[b] == d
    assert stats.duplicate_groups == 1 and stats.redundant_files == 1 and stats.redundant_bytes == 500


def test_a_onedrive_file_joins_a_bridge_only_through_its_own_download(conn, catalog, clouds):
    """The bridge of another file shares the OneDrive file's QuickXor; that is not its bytes."""
    d = drive(catalog, clouds["d1"], "doc.pdf", "A", size=500)
    b = dropbox(catalog, clouds["db"], "doc.pdf", "A", size=500)
    o = onedrive(catalog, clouds["od"], "doc.pdf", "A", size=500, mtime=T0)
    record_bridge(conn, H("A", 500))
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[b] == d and g[o] is None
    assert not same_content(conn, d, o)

    record_bridge(conn, H("A", 500), source_entry_id=o, source_mtime=T0)        # o's own bytes were hashed
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[b] == g[o] == d
    assert stats.duplicate_groups == 1 and stats.redundant_files == 2 and stats.redundant_bytes == 1000


def test_a_onedrive_file_with_other_bytes_and_the_same_quickxor_stays_apart(conn, catalog, clouds):
    """The crafted collision: same size, same QuickXor, other sha256. Its own download says so."""
    d = drive(catalog, clouds["d1"], "doc.pdf", "A", size=500)
    o = catalog.file(clouds["od"], "doc.pdf", size=500, mtime=T0, quickxor=H("A")["quickxor"])
    mine = H("B", 500) | {"quickxor": H("A")["quickxor"]}
    record_bridge(conn, H("A", 500))
    record_bridge(conn, mine, source_entry_id=o, source_mtime=T0)
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == d and g[o] == o and not same_content(conn, d, o)
    assert duplicate_groups(conn) == []


def test_bridge_joins_only_matching_content(conn, catalog, clouds):
    d = drive(catalog, clouds["d1"], "a", "A")
    b_same = dropbox(catalog, clouds["db"], "a", "A")
    b_other = dropbox(catalog, clouds["db"], "other", "B")      # same size, different content
    record_bridge(conn, H("A"))
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[b_same]
    assert g[b_other] == b_other


def test_bridge_requires_same_size(conn, catalog, clouds):
    d = drive(catalog, clouds["d1"], "a", "A", size=10)
    b = dropbox(catalog, clouds["db"], "a", "A", size=11)       # claims the bridge's hash but another size
    record_bridge(conn, H("A", 10))
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == d and g[b] == b


def test_bridge_ignores_non_live_entries(conn, catalog, clouds):
    d = drive(catalog, clouds["d1"], "a", "A")
    gone = dropbox(catalog, clouds["db"], "a", "A", present=False)
    record_bridge(conn, H("A"))
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == d and g[gone] is None


def test_bridge_with_one_match_changes_nothing(conn, catalog, clouds):
    d = drive(catalog, clouds["d1"], "a", "A")
    record_bridge(conn, H("A"))
    stats = rebuild_groups(conn)
    assert groups(conn)[d] == d and stats.duplicate_groups == 0


def test_native_and_bridge_edges_combine(conn, catalog, clouds):
    """Dropbox copy joins the Drive copy by bridge; a second Dropbox copy joins by its own hash."""
    d = drive(catalog, clouds["d1"], "a", "A")
    b1 = dropbox(catalog, clouds["db"], "a", "A")
    b2 = dropbox(catalog, clouds["db"], "elsewhere/a", "A")
    record_bridge(conn, H("A"))
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[b1] == g[b2] == d


def test_two_bridges_for_different_contents_stay_apart(conn, catalog, clouds):
    da, ba = drive(catalog, clouds["d1"], "a", "A"), dropbox(catalog, clouds["db"], "a", "A")
    dz, bz = drive(catalog, clouds["d1"], "z", "Z"), dropbox(catalog, clouds["db"], "z", "Z")
    record_bridge(conn, H("A"))
    record_bridge(conn, H("Z"))
    rebuild_groups(conn)
    g = groups(conn)
    assert g[da] == g[ba] == da and g[dz] == g[bz] == dz and g[da] != g[dz]


def test_conflicting_hash_values_are_not_joined_and_are_counted(conn, catalog, clouds):
    """Same Dropbox hash+size but different sha256 (a crafted collision?): the shared hash proves nothing, so no group."""
    a = catalog.file(clouds["d1"], "a", size=9, dropbox="dd", sha256="s1")
    b = catalog.file(clouds["d2"], "b", size=9, dropbox="dd", sha256="s2")
    stats = rebuild_groups(conn)
    assert groups(conn)[a] == a and groups(conn)[b] == b
    assert stats.hash_conflicts == 1 and stats.duplicate_groups == 0
    assert not same_content(conn, a, b)
    assert duplicate_groups(conn) == []


@pytest.mark.parametrize("shared", identity.WEAK_TYPES)
def test_a_shared_weak_hash_is_neither_a_join_nor_a_conflict(conn, catalog, clouds, shared):
    """md5, sha1 or QuickXor equal, another weak type different: nobody tried to join them, so nothing is refused."""
    other = next(t for t in identity.WEAK_TYPES if t != shared)
    a = catalog.file(clouds["d1"], "a", size=9, **{shared: "aa", other: "11"})
    b = catalog.file(clouds["d2"], "b", size=9, **{shared: "aa", other: "22"})
    stats = rebuild_groups(conn)
    assert groups(conn)[a] is None and groups(conn)[b] is None           # no strong hash: not identified at all
    assert stats.hash_conflicts == 0 and stats.duplicate_groups == 0 and not same_content(conn, a, b)


def test_sha1_is_no_proof_of_identity(conn, catalog, clouds):
    """sha1 has practical collision attacks: the same sha1 and size is a lead, never a join (Drive also has sha256)."""
    a = catalog.file(clouds["d1"], "x.bin", size=5, sha1="aa", sha256="s1")
    b = catalog.file(clouds["d2"], "x.bin", size=5, sha1="aa")                 # same sha1, no sha256 to compare
    c = catalog.file(clouds["d2"], "y.bin", size=5, sha1="aa", sha256="s1")    # sha256 is what joins a and c
    d = catalog.file(clouds["d1"], "z.bin", size=5, sha1="bb")
    e = catalog.file(clouds["d2"], "z.bin", size=5, sha1="bb")
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == g[c] == a and g[b] is None and g[d] is None and g[e] is None
    assert stats.hash_conflicts == 0 and stats.duplicate_groups == 1
    assert not same_content(conn, a, b) and not same_content(conn, d, e)
    assert [grp.key for grp in duplicate_groups(conn)] == [f"g:{a}"]
    probable = {cp.entry_id for grp in duplicate_groups(conn, confidence="probable") for cp in grp.copies}
    assert {d, e} <= probable                                                # still a lead for a bridge download


def test_consistent_groups_report_no_conflict(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a", "A")
    drive(catalog, clouds["d2"], "b", "A")
    assert rebuild_groups(conn).hash_conflicts == 0


# -- conflict-aware union: a join between components that disagree on a hash type is refused

@pytest.mark.parametrize("shared", ["md5", "sha1"])
def test_md5_or_sha1_equal_but_sha256_different_are_different_groups(conn, catalog, clouds, shared):
    """The delete-safety case: a shared md5 or sha1 must not make one file the witness for the other."""
    a = catalog.file(clouds["d1"], "a/report.pdf", size=50, sha256="s1", **{shared: "aa"})
    b = catalog.file(clouds["d2"], "b/report.pdf", size=50, sha256="s2", **{shared: "aa"})
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == a and g[b] == b
    assert stats.hash_conflicts == 0 and stats.duplicate_groups == 0 and stats.redundant_files == 0
    assert not same_content(conn, a, b)
    assert [c.entry_id for c in locations(conn, a)] == [a]
    assert duplicate_groups(conn) == []
    assert duplicate_groups(conn, confidence="probable") == []       # sha256 proves they differ: not even a candidate


def test_every_refused_edge_is_counted(conn, catalog, clouds):
    """sha256 and the Dropbox hash both link the pair and both are refused; sha1 is what tells them apart."""
    catalog.file(clouds["d1"], "a", size=5, sha256="s", dropbox="dd", sha1="11")
    catalog.file(clouds["d2"], "b", size=5, sha256="s", dropbox="dd", sha1="22")
    assert rebuild_groups(conn).hash_conflicts == 2


@pytest.mark.parametrize("hash_type", HASH_TYPES)
def test_a_contradiction_in_any_hash_type_blocks_the_join(conn, catalog, hash_type):
    r = catalog.remote("loc", "local")
    other = next(t for t in identity.STRONG_TYPES if t != hash_type)        # the type that links the pair
    a = catalog.file(r, "a", size=5, **{other: "same", hash_type: "x1"})
    b = catalog.file(r, "b", size=5, **{other: "same", hash_type: "x2"})
    stats = rebuild_groups(conn)
    assert groups(conn)[a] == a and groups(conn)[b] == b and stats.hash_conflicts == 1


def test_a_hash_the_other_side_does_not_know_is_not_a_contradiction(conn, catalog, clouds):
    """Drive (sha1, sha256) vs a copy with only sha256: nothing disagrees, so they are one content."""
    a = catalog.file(clouds["d1"], "a", size=5, sha1="aa", sha256="s1")
    b = catalog.file(clouds["d2"], "b", size=5, sha256="s1")
    c = catalog.file(clouds["d2"], "c", size=5, sha1="aa", sha256="s1")
    stats = rebuild_groups(conn)
    assert groups(conn)[a] == groups(conn)[b] == groups(conn)[c] == a and stats.hash_conflicts == 0


def test_contradiction_only_appears_after_several_joins(conn, catalog, clouds):
    """a~b by sha256, b~c by dropbox, but a and c disagree on sha1: no linked pair is contradictory
    on its own (b says nothing about sha1), only the component that grew out of a~b is."""
    loc = catalog.remote("loc", "local")
    a = catalog.file(loc, "a", size=7, sha256="x", sha1="m1")
    b = catalog.file(loc, "b", size=7, sha256="x", dropbox="d")
    c = catalog.file(loc, "c", size=7, dropbox="d", sha1="m2")
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == g[b] == a and g[c] == c
    assert stats.hash_conflicts == 1 and stats.duplicate_groups == 1


def test_chain_through_all_strong_hash_types_is_cut_at_its_last_link(conn, catalog):
    """Two joins build one component; the fourth file shares only the Dropbox hash but contradicts its sha1.
    The weak hashes ride along in the chain and change nothing."""
    loc = catalog.remote("loc", "local")
    chain = [
        catalog.file(loc, "f0", size=3, sha256="t", sha1="s", md5="m"),
        catalog.file(loc, "f1", size=3, sha256="t", sha1="s", dropbox="d", md5="m"),
        catalog.file(loc, "f2", size=3, dropbox="d", quickxor="q"),
    ]
    odd = catalog.file(loc, "f3", size=3, dropbox="d", sha1="OTHER", quickxor="q")
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert {g[e] for e in chain} == {chain[0]}
    assert g[odd] == odd
    assert (stats.hash_conflicts, stats.duplicate_groups, stats.redundant_files) == (1, 1, 2)


def test_conflicting_run_does_not_depend_on_row_order_within_a_value(conn, catalog):
    """Three files share a sha256; the lowest id anchors the run, the one that contradicts it is left out."""
    loc = catalog.remote("loc", "local")
    a = catalog.file(loc, "a", size=3, sha256="m", sha1="1")
    b = catalog.file(loc, "b", size=3, sha256="m", sha1="2")
    c = catalog.file(loc, "c", size=3, sha256="m")
    rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == g[c] == a and g[b] == b


def test_bridge_does_not_pull_in_a_file_that_contradicts_it(conn, catalog, clouds):
    """A Drive file with the bridge's sha256 but another sha1 stays out; the Dropbox copy still joins."""
    good = drive(catalog, clouds["d1"], "g", "A")
    fake = catalog.file(clouds["d2"], "f", size=10, sha256=H("A")["sha256"], sha1=H("FAKE")["sha1"])
    dbx = dropbox(catalog, clouds["db"], "g", "A")
    record_bridge(conn, H("A"))
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[good] == g[dbx] == good and g[fake] == fake
    assert stats.hash_conflicts == 2          # sha256 edge good~fake was refused directly, then the bridge's edge to fake
    assert not same_content(conn, fake, dbx)


def test_bridge_that_contradicts_a_native_group_is_refused_whole(conn, catalog, clouds):
    """Two Drive copies agree with each other; a bridge with their sha256 but a different sha1 describes other bytes."""
    a = catalog.file(clouds["d1"], "a", size=10, sha256=H("A")["sha256"], sha1="aa")
    b = catalog.file(clouds["d2"], "b", size=10, sha256=H("A")["sha256"], sha1="aa")
    o = dropbox(catalog, clouds["db"], "o", "A")
    record_bridge(conn, H("A"))                                   # its sha1 is not "aa"
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[a] == g[b] == a and g[o] == o
    assert stats.hash_conflicts >= 1


def test_hashless_source_cannot_be_forced_into_a_contradicting_component(conn, catalog, clouds):
    i = icloud(catalog, clouds["ic"], "a", mtime=T0)
    d = drive(catalog, clouds["d1"], "a", "A", mtime=T0)
    record_bridge(conn, H("A"), source_entry_id=i, source_mtime=T0)
    stats = rebuild_groups(conn)
    assert groups(conn)[i] == groups(conn)[d] == min(i, d) and stats.hash_conflicts == 0


def _naive_components(conn, edges_by_value=True):
    """What an unconditional union-find over the strong hashes would produce (independent reimplementation).

    Weak hashes (md5, QuickXor) are no edges; they are only passengers of the entries."""
    live = [r for r in conn.execute(
        "SELECT * FROM entries WHERE present = 1 AND is_dir = 0 AND is_native_doc = 0 AND size IS NOT NULL")]
    parent = {r["id"]: r["id"] for r in live}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    seen: dict = {}
    for r in live:
        for t in identity.STRONG_TYPES:
            if r[t]:
                union(r["id"], seen.setdefault((t, r[t], r["size"]), r["id"]))
    for b in conn.execute("SELECT * FROM hash_bridges"):
        members = [r["id"] for r in live
                   if r["size"] == b["size"] and any(r[t] == b[t] for t in identity.STRONG_TYPES)]
        members += [x["entry_id"] for x in conn.execute(
            "SELECT s.entry_id FROM bridge_sources s JOIN entries e ON e.id = s.entry_id "
            "WHERE s.bridge_id = ? AND e.size = s.size AND e.mtime = s.mtime AND e.present = 1", (b["id"],))]
        for m in members[1:]:
            union(members[0], m)
    return {r["id"]: find(r["id"]) for r in live}


@pytest.mark.parametrize("seed", range(60))
def test_random_catalogs_never_hold_two_values_of_a_hash_type_in_one_group(conn, catalog, seed):
    """Adversarial fuzz: tiny hash alphabets force collisions, partial hashes and contradicting bridges."""
    rng = random.Random(seed)
    remote = catalog.remote("loc", "local")
    vals = {t: [f"{t[:2]}{i}" for i in range(3)] for t in HASH_TYPES}
    for i in range(rng.randint(8, 40)):
        hashes = {t: rng.choice(vals[t]) for t in HASH_TYPES if rng.random() < 0.45}
        catalog.file(remote, f"f{i}", size=rng.choice((4, 5)), mtime=T0, **hashes)
    used = set()
    for _ in range(rng.randint(0, 6)):
        sig = {t: rng.choice(vals[t]) for t in HASH_TYPES}
        if sig["sha256"] in used:
            continue
        used.add(sig["sha256"])
        bid = conn.execute(
            "INSERT INTO hash_bridges(size, md5, sha1, sha256, dropbox, quickxor, computed_at) "
            "VALUES (?,?,?,?,?,?,?) RETURNING id",
            (rng.choice((4, 5)), *(sig[t] for t in HASH_TYPES), db.utcnow())).fetchone()[0]
        size = conn.execute("SELECT size FROM hash_bridges WHERE id = ?", (bid,)).fetchone()[0]
        same_size = [r[0] for r in conn.execute("SELECT id FROM entries WHERE is_dir = 0 AND size = ?", (size,))]
        for eid in rng.sample(same_size, min(2, len(same_size))):
            if rng.random() < 0.5:
                conn.execute("INSERT OR IGNORE INTO bridge_sources VALUES (?,?,?,?)", (bid, eid, size, T0))

    stats = rebuild_groups(conn)

    members: dict[int, list] = defaultdict(list)
    for r in conn.execute("SELECT * FROM entries WHERE content_group IS NOT NULL"):
        members[r["content_group"]].append(r)
    for g, rows in members.items():
        assert g == min(r["id"] for r in rows)
        assert len({r["size"] for r in rows}) == 1
        for t in HASH_TYPES:
            assert len({r[t] for r in rows if r[t]}) <= 1, (seed, g, t)
    coarse = _naive_components(conn)
    for rows in members.values():
        assert len({coarse[r["id"]] for r in rows}) == 1             # groups only ever split the old components
    assert rebuild_groups(conn).changed == 0
    assert stats.hash_conflicts == rebuild_groups(conn).hash_conflicts           # deterministic


@pytest.mark.parametrize("seed", range(30))
def test_consistent_random_catalogs_group_exactly_as_before(conn, catalog, seed):
    """No contradictions to refuse: every real content comes out as the full component, nothing is lost."""
    rng = random.Random(1000 + seed)
    remote = catalog.remote("loc", "local")
    contents = {c: H(f"content-{seed}-{c}", size=3 + c % 3) for c in range(rng.randint(2, 8))}
    for i in range(rng.randint(10, 40)):
        c = rng.randrange(len(contents))
        shown = {t: contents[c][t] for t in HASH_TYPES if rng.random() < 0.5}
        catalog.file(remote, f"f{i}", size=contents[c]["size"], mtime=T0, **shown)
    for c in contents:
        if rng.random() < 0.5:
            record_bridge(conn, contents[c])
    stats = rebuild_groups(conn)
    assert stats.hash_conflicts == 0
    want = _naive_components(conn)
    got = groups(conn)
    ids = [i for i in want if got[i] is not None]
    for x in ids:
        for y in ids:
            assert (got[x] == got[y]) == (want[x] == want[y]), (seed, x, y)


# -- change feed

def test_second_rebuild_changes_nothing_and_keeps_row_versions(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a", "A")
    drive(catalog, clouds["d2"], "a", "A")
    icloud(catalog, clouds["ic"], "a")
    catalog.file(clouds["d1"], "gone", size=1, md5="zz", present=False)
    first = rebuild_groups(conn)
    assert first.changed == 2
    before, meta_before = versions(conn), meta_version(conn)
    second = rebuild_groups(conn)
    assert second.changed == 0
    assert versions(conn) == before
    assert meta_version(conn) == meta_before
    assert (second.files_hashed, second.groups, second.duplicate_groups) == \
           (first.files_hashed, first.groups, first.duplicate_groups)


def test_changed_rows_share_one_new_row_version(conn, catalog, clouds):
    a = drive(catalog, clouds["d1"], "a", "A")
    b = drive(catalog, clouds["d2"], "b", "B")
    rebuild_groups(conn)
    before, meta_before = versions(conn), meta_version(conn)
    c = drive(catalog, clouds["d2"], "c", "A")          # joins a's group; b untouched
    meta_mid = meta_version(conn)                        # catalog.file bumped it for the insert
    stats = rebuild_groups(conn)
    after = versions(conn)
    assert stats.changed == 1
    assert after[a] == before[a] and after[b] == before[b]
    assert after[c] > meta_before and after[c] == meta_version(conn) == meta_mid + 1


def test_failed_rebuild_rolls_back(conn, catalog, clouds, monkeypatch):
    drive(catalog, clouds["d1"], "a", "A")
    drive(catalog, clouds["d2"], "b", "A")

    def boom(_conn):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "next_row_version", boom)
    with pytest.raises(RuntimeError):
        rebuild_groups(conn)
    assert not conn.in_transaction
    assert all(v is None for k, v in groups(conn).items() if row(conn, k)["is_dir"] == 0)


def test_rebuild_works_with_default_tuple_row_factory(catalog, conn, clouds):
    drive(catalog, clouds["d1"], "a", "A")
    drive(catalog, clouds["d2"], "b", "A")
    record_bridge(conn, H("A"))
    conn.row_factory = None
    assert rebuild_groups(conn).duplicate_groups == 1
    assert duplicate_groups(conn)[0].redundant_bytes == 10


# --------------------------------------------------------------------------- content_key / locations / same_content

def test_content_key(conn, catalog, clouds):
    a = drive(catalog, clouds["d1"], "a", "A")
    i = icloud(catalog, clouds["ic"], "a")
    rebuild_groups(conn)
    assert content_key(row(conn, a)) == f"g:{a}"
    assert content_key(row(conn, i)) == f"e:{i}"
    assert content_key({"id": 7, "content_group": None}) == "e:7"
    assert content_key({"id": 7, "content_group": 3}) == "g:3"


def test_locations_lists_every_live_copy_including_itself(conn, catalog, clouds):
    a = drive(catalog, clouds["d2"], "b/a", "A")
    b = drive(catalog, clouds["d1"], "z/a", "A")
    c = drive(catalog, clouds["d1"], "a/a", "A")
    dead = drive(catalog, clouds["d1"], "dead", "A", present=False)
    rebuild_groups(conn)
    locs = locations(conn, b)
    assert [(l.remote, l.path) for l in locs] == [("d1", "a/a"), ("d1", "z/a"), ("d2", "b/a")]
    assert {l.entry_id for l in locs} == {a, b, c} and dead not in {l.entry_id for l in locs}
    assert locs[0].size == 10 and locs[0].remote_id == clouds["d1"] and locs[0].mtime


def test_locations_of_ungrouped_and_non_live_entries(conn, catalog, clouds):
    i = icloud(catalog, clouds["ic"], "a")
    dead = drive(catalog, clouds["d1"], "dead", "A", present=False)
    folder = catalog.dir(clouds["d1"], "folder")
    rebuild_groups(conn)
    assert [l.entry_id for l in locations(conn, i)] == [i]
    assert locations(conn, dead) == [] and locations(conn, folder) == [] and locations(conn, 99999) == []


def test_same_content_is_exact_only(conn, catalog, clouds):
    a = drive(catalog, clouds["d1"], "a", "A")
    b = drive(catalog, clouds["d2"], "b", "A")
    other = drive(catalog, clouds["d2"], "c", "B")
    dbx = dropbox(catalog, clouds["db"], "a", "A")           # same name+size, no proof
    i1, i2 = icloud(catalog, clouds["ic"], "x/a"), icloud(catalog, clouds["ic"], "y/a")
    dead = drive(catalog, clouds["d1"], "dead", "A", present=False)
    rebuild_groups(conn)
    assert same_content(conn, a, b) and same_content(conn, b, a)
    assert not same_content(conn, a, other)
    assert not same_content(conn, a, dbx)
    assert not same_content(conn, i1, i2)                    # two unhashed files are never exact
    assert not same_content(conn, a, dead)
    record_bridge(conn, H("A"))
    rebuild_groups(conn)
    assert same_content(conn, a, dbx)


def test_same_content_with_itself(conn, catalog, clouds):
    i = icloud(catalog, clouds["ic"], "a")
    dead = drive(catalog, clouds["d1"], "dead", "A", present=False)
    rebuild_groups(conn)
    assert same_content(conn, i, i)
    assert not same_content(conn, dead, dead) and not same_content(conn, 12345, 12345)


# --------------------------------------------------------------------------- duplicate_groups (exact)

def test_exact_group_shape(conn, catalog, clouds):
    a = drive(catalog, clouds["d2"], "b.txt", "A", size=40, mtime="2023-05-05T00:00:00Z")
    b = drive(catalog, clouds["d1"], "z.txt", "A", size=40)
    c = drive(catalog, clouds["d1"], "a.txt", "A", size=40)
    rebuild_groups(conn)
    (grp,) = duplicate_groups(conn)
    assert grp.key == f"g:{a}" and grp.confidence == "exact" and grp.size == 40
    assert [(c_.remote, c_.path) for c_ in grp.copies] == [("d1", "a.txt"), ("d1", "z.txt"), ("d2", "b.txt")]
    assert grp.remotes == ["d1", "d2"]
    assert grp.redundant_bytes == 80
    assert next(c_ for c_ in grp.copies if c_.entry_id == a).mtime == "2023-05-05T00:00:00Z"
    assert {c_.entry_id for c_ in grp.copies} == {a, b, c}


def test_singletons_and_dead_copies_are_not_duplicate_groups(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a", "A")
    drive(catalog, clouds["d2"], "a", "A", present=False)
    drive(catalog, clouds["d2"], "doc", "A", native_doc=True)
    rebuild_groups(conn)
    assert duplicate_groups(conn) == []


def _three_groups(catalog, clouds):
    """sizes 100 (x2) -> 100 redundant, 30 (x4) -> 90, 50 (x2) -> 50, 10 (x2) -> 10, 0 (x3) -> 0."""
    ids = {}
    for tag, size, n in (("A", 100, 2), ("B", 30, 4), ("C", 50, 2), ("D", 10, 2), ("E", 0, 3)):
        ids[tag] = [drive(catalog, clouds["d1" if i % 2 == 0 else "d2"], f"{tag}/{i}", tag, size=size)
                    for i in range(n)]
    return ids


def test_ordering_by_redundant_bytes_then_key(conn, catalog, clouds):
    ids = _three_groups(catalog, clouds)
    rebuild_groups(conn)
    got = duplicate_groups(conn)
    assert [g.key for g in got] == [f"g:{ids[t][0]}" for t in "ABCD"]      # E (size 0) excluded by min_size=1
    assert [g.redundant_bytes for g in got] == [100, 90, 50, 10]


def test_ties_order_by_key_text(conn, catalog, clouds):
    keys = []
    for tag in "PQR":
        keys.append(drive(catalog, clouds["d1"], f"{tag}/1", tag, size=20))
        drive(catalog, clouds["d2"], f"{tag}/2", tag, size=20)
    rebuild_groups(conn)
    got = [g.key for g in duplicate_groups(conn)]
    assert got == sorted(got) and len(got) == 3


def test_min_size(conn, catalog, clouds):
    ids = _three_groups(catalog, clouds)
    rebuild_groups(conn)
    with_empty = duplicate_groups(conn, min_size=0)
    assert {g.key for g in with_empty} == {f"g:{ids[t][0]}" for t in "ABCDE"}
    assert [g.size for g in duplicate_groups(conn, min_size=50)] == [100, 50]


def test_remote_filter_keeps_group_and_lists_all_copies(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "only-d1-1", "X")
    drive(catalog, clouds["d1"], "only-d1-2", "X")
    drive(catalog, clouds["d1"], "shared", "Y")
    drive(catalog, clouds["d2"], "shared", "Y")
    rebuild_groups(conn)
    on_d2 = duplicate_groups(conn, remote="d2")
    assert len(on_d2) == 1 and on_d2[0].remotes == ["d1", "d2"] and len(on_d2[0].copies) == 2
    assert len(duplicate_groups(conn, remote="d1")) == 2
    assert duplicate_groups(conn, remote="db") == []
    with pytest.raises(KeyError):
        duplicate_groups(conn, remote="nope")


def test_cross_remote_only(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "same-remote-1", "X")
    drive(catalog, clouds["d1"], "same-remote-2", "X")
    drive(catalog, clouds["d1"], "cross", "Y")
    drive(catalog, clouds["d2"], "cross", "Y")
    rebuild_groups(conn)
    assert len(duplicate_groups(conn)) == 2
    (only,) = duplicate_groups(conn, cross_remote_only=True)
    assert only.remotes == ["d1", "d2"]


def test_path_prefix_matches_whole_segments_literally(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "Photos/2023/a.jpg", "A")
    drive(catalog, clouds["d2"], "backup/a.jpg", "A")
    drive(catalog, clouds["d1"], "Photos2/b.jpg", "B")
    drive(catalog, clouds["d2"], "misc/b.jpg", "B")
    drive(catalog, clouds["d1"], "100%/c.jpg", "C")
    drive(catalog, clouds["d2"], "misc/c.jpg", "C")
    drive(catalog, clouds["d1"], "a_b/d.jpg", "D")
    drive(catalog, clouds["d2"], "misc/d.jpg", "D")
    drive(catalog, clouds["d1"], "axb/e.jpg", "E")
    drive(catalog, clouds["d2"], "axb2/e.jpg", "E")
    rebuild_groups(conn)

    def sizes_of(prefix):
        return sorted(c.path for g in duplicate_groups(conn, path_prefix=prefix) for c in g.copies)

    assert sizes_of("Photos") == ["Photos/2023/a.jpg", "backup/a.jpg"]     # not Photos2
    assert sizes_of("Photos/") == sizes_of("/Photos") == sizes_of("Photos")
    assert sizes_of("Photos/2023") == ["Photos/2023/a.jpg", "backup/a.jpg"]
    assert sizes_of("Photos/2023/a.jpg") == ["Photos/2023/a.jpg", "backup/a.jpg"]   # exact path counts
    assert sizes_of("Phot") == []                                           # no partial segments
    assert sizes_of("100%") == ["100%/c.jpg", "misc/c.jpg"]
    assert sizes_of("%") == [] and sizes_of("100_") == []
    assert sizes_of("a_b") == ["a_b/d.jpg", "misc/d.jpg"]                   # '_' is not a wildcard
    assert sizes_of("") == sizes_of(None) == sizes_of("/") != []


def test_filters_combine(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "keep/a", "A", size=50)
    drive(catalog, clouds["d2"], "x/a", "A", size=50)
    drive(catalog, clouds["d1"], "keep/b", "B", size=50)
    drive(catalog, clouds["d1"], "keep/b2", "B", size=50)
    drive(catalog, clouds["d1"], "keep/c", "C", size=5)
    drive(catalog, clouds["d2"], "x/c", "C", size=5)
    rebuild_groups(conn)
    got = duplicate_groups(conn, remote="d2", cross_remote_only=True, path_prefix="keep", min_size=10)
    assert [g.size for g in got] == [50] and got[0].remotes == ["d1", "d2"]


def test_pagination_is_stable_and_complete(conn, catalog, clouds):
    for i in range(7):
        drive(catalog, clouds["d1"], f"g{i}/1", f"T{i}", size=10 * (i + 1))
        drive(catalog, clouds["d2"], f"g{i}/2", f"T{i}", size=10 * (i + 1))
    rebuild_groups(conn)
    everything = duplicate_groups(conn, limit=100)
    assert len(everything) == 7
    paged = []
    for off in range(0, 8, 3):
        paged += duplicate_groups(conn, limit=3, offset=off)
    assert [g.key for g in paged] == [g.key for g in everything]
    assert [g.redundant_bytes for g in everything] == sorted((g.redundant_bytes for g in everything), reverse=True)
    assert duplicate_groups(conn, limit=3, offset=7) == []
    assert duplicate_groups(conn, limit=0) == []


def _bulk_pairs(conn, catalog, clouds, n, *, hashed=True):
    """n two-copy groups inserted in bulk (sizes 1..n so every group has its own rank)."""
    rv = db.next_row_version(conn)
    rows = []
    for g in range(n):
        h = H(f"bulk-{g}")
        for k, remote in enumerate(("d1", "d2")):
            rows.append((clouds[remote], f"p:g{g}/{k}", f"g{g}/{k}", f"g{g}", "f.bin", "f.bin", "bin", "other", 0, g + 1,
                         T0, h["md5"] if hashed else None, h["sha1"] if hashed else None,
                         h["sha256"] if hashed else None, 0, 1, catalog._scan[clouds[remote]],
                         catalog._scan[clouds[remote]], rv))
    conn.executemany(
        """INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category, is_dir, size,
             mtime, md5, sha1, sha256, is_native_doc, present, first_seen_scan, last_seen_scan, row_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)


@pytest.mark.parametrize("confidence", ["exact", "probable"])
def test_a_page_of_groups_is_fetched_with_a_single_query(conn, catalog, clouds, confidence):
    """One query for the whole page, however many groups it holds (it used to be one per group)."""
    _bulk_pairs(conn, catalog, clouds, 1500, hashed=confidence == "exact")
    rebuild_groups(conn)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        got = duplicate_groups(conn, confidence=confidence, limit=1500)
        few = duplicate_groups(conn, confidence=confidence, limit=5)
    finally:
        conn.set_trace_callback(None)
    assert len(got) == 1500 and len(few) == 5
    scans = [q for q in statements if "FROM entries" in q]
    assert len(scans) == 2                                # one per call, not one per group
    assert [g.redundant_bytes for g in got] == sorted((g.redundant_bytes for g in got), reverse=True)
    assert [g.key for g in few] == [g.key for g in got[:5]]
    assert all(len(g.copies) == 2 and g.remotes == ["d1", "d2"] for g in got)
    assert [c.path for c in got[0].copies] == ["g1499/0", "g1499/1"]


def test_pages_cover_every_group_exactly_once_across_offsets(conn, catalog, clouds):
    _bulk_pairs(conn, catalog, clouds, 130)
    rebuild_groups(conn)
    keys = []
    for off in range(0, 140, 25):
        keys += [g.key for g in duplicate_groups(conn, limit=25, offset=off)]
    assert len(keys) == len(set(keys)) == 130
    assert keys == [g.key for g in duplicate_groups(conn, limit=500)]


def test_invalid_confidence(conn):
    with pytest.raises(ValueError):
        duplicate_groups(conn, confidence="sure")


# --------------------------------------------------------------------------- duplicate_groups (probable)

def test_icloud_file_is_probable_with_same_name_and_size_drive_file(conn, catalog, clouds):
    d = drive(catalog, clouds["d1"], "Photos/IMG_1.jpg", "A", size=1234)
    i = icloud(catalog, clouds["ic"], "Camera/IMG_1.jpg", size=1234)
    rebuild_groups(conn)
    assert duplicate_groups(conn, confidence="exact") == []
    (grp,) = duplicate_groups(conn, confidence="probable")
    assert grp.key == "p:1234:img_1.jpg" and grp.confidence == "probable"
    assert {c.entry_id for c in grp.copies} == {d, i}
    assert grp.remotes == ["d1", "ic"] and grp.redundant_bytes == 1234
    assert groups(conn)[i] is None


def test_probable_needs_same_size_and_name(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a/IMG_1.jpg", "A", size=100)
    icloud(catalog, clouds["ic"], "IMG_1.jpg", size=101)               # other size
    icloud(catalog, clouds["ic"], "IMG_2.jpg", size=100)               # other name
    rebuild_groups(conn)
    assert duplicate_groups(conn, confidence="probable") == []


def test_probable_matches_normalized_names(conn, catalog, clouds):
    nfd = unicodedata.normalize("NFD", "Résumé.PDF")
    nfc = unicodedata.normalize("NFC", "résumé.pdf")
    drive(catalog, clouds["d1"], f"docs/{nfc}", "A", size=77)
    icloud(catalog, clouds["ic"], f"Documents/{nfd}", size=77)
    rebuild_groups(conn)
    assert len(duplicate_groups(conn, confidence="probable")) == 1


def test_exact_group_is_not_repeated_as_probable(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a/x.bin", "A", size=9)
    drive(catalog, clouds["d2"], "b/x.bin", "A", size=9)
    rebuild_groups(conn)
    assert len(duplicate_groups(conn, confidence="exact")) == 1
    assert duplicate_groups(conn, confidence="probable") == []


def test_unhashed_copy_of_an_exact_group_makes_it_probable_too(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a/x.bin", "A", size=9)
    drive(catalog, clouds["d2"], "b/x.bin", "A", size=9)
    icloud(catalog, clouds["ic"], "x.bin", size=9)
    rebuild_groups(conn)
    (prob,) = duplicate_groups(conn, confidence="probable")
    assert len(prob.copies) == 3
    assert len(duplicate_groups(conn, confidence="exact")[0].copies) == 2


def test_cross_cloud_same_name_size_is_probable_until_bridged(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a/x.bin", "A", size=9)
    dropbox(catalog, clouds["db"], "b/x.bin", "A", size=9)
    rebuild_groups(conn)
    assert len(duplicate_groups(conn, confidence="probable")) == 1
    assert duplicate_groups(conn, confidence="exact") == []
    record_bridge(conn, H("A", 9))
    rebuild_groups(conn)
    assert duplicate_groups(conn, confidence="probable") == []
    assert len(duplicate_groups(conn, confidence="exact")) == 1


def test_two_unhashed_copies_are_probable(conn, catalog, clouds):
    icloud(catalog, clouds["ic"], "a/x.bin", size=9)
    icloud(catalog, clouds["ic"], "b/x.bin", size=9)
    rebuild_groups(conn)
    (prob,) = duplicate_groups(conn, confidence="probable")
    assert prob.redundant_bytes == 9


def test_probable_ignores_non_live_and_directories(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a/x.bin", "A", size=9)
    icloud(catalog, clouds["ic"], "x.bin", size=9, present=False)
    icloud(catalog, clouds["ic"], "y/x.bin", size=9, native_doc=True)
    rebuild_groups(conn)
    assert duplicate_groups(conn, confidence="probable") == []


def test_probable_filters_ordering_and_pagination(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "keep/big.bin", "A", size=900)
    icloud(catalog, clouds["ic"], "big.bin", size=900)
    drive(catalog, clouds["d1"], "keep/small.bin", "B", size=5)
    icloud(catalog, clouds["ic"], "small.bin", size=5)
    icloud(catalog, clouds["ic"], "x/same.bin", size=300)
    icloud(catalog, clouds["ic"], "y/same.bin", size=300)
    rebuild_groups(conn)
    assert [g.size for g in duplicate_groups(conn, confidence="probable")] == [900, 300, 5]
    assert [g.size for g in duplicate_groups(conn, confidence="probable", min_size=100)] == [900, 300]
    assert [g.size for g in duplicate_groups(conn, confidence="probable", cross_remote_only=True)] == [900, 5]
    assert [g.size for g in duplicate_groups(conn, confidence="probable", remote="d1")] == [900, 5]
    assert [g.size for g in duplicate_groups(conn, confidence="probable", path_prefix="keep")] == [900, 5]
    assert [g.size for g in duplicate_groups(conn, confidence="probable", limit=1, offset=1)] == [300]


def test_same_name_and_size_with_different_hashes_is_not_a_probable_duplicate(conn, catalog, clouds):
    """Probable means "no hash proof", not "proof of the opposite": md5 aa vs bb cannot be the same bytes."""
    catalog.file(clouds["d1"], "a/report.pdf", size=100, md5="aa")
    catalog.file(clouds["d2"], "b/report.pdf", size=100, md5="bb")
    rebuild_groups(conn)
    assert duplicate_groups(conn, confidence="exact") == []
    assert duplicate_groups(conn, confidence="probable") == []


def test_a_weak_hash_that_differs_also_rules_a_pair_out(conn, catalog, clouds):
    """QuickXor proves a difference too, even though it never proves a match."""
    onedrive(catalog, clouds["od"], "a/x.bin", "A", size=9)
    onedrive(catalog, clouds["od"], "b/x.bin", "B", size=9)
    rebuild_groups(conn)
    assert duplicate_groups(conn, confidence="probable") == []


def test_a_weak_hash_that_agrees_leaves_a_pair_probable(conn, catalog, clouds):
    onedrive(catalog, clouds["od"], "a/x.bin", "A", size=9)
    onedrive(catalog, clouds["od"], "b/x.bin", "A", size=9)
    rebuild_groups(conn)
    assert duplicate_groups(conn, confidence="exact") == []
    (prob,) = duplicate_groups(conn, confidence="probable")
    assert prob.redundant_bytes == 9


def test_an_unknown_hash_is_not_a_difference(conn, catalog, clouds):
    """Drive reports md5/sha1/sha256, Dropbox only its own hash: no type in common, so nothing is ruled out."""
    drive(catalog, clouds["d1"], "a/x.bin", "A", size=9)
    dropbox(catalog, clouds["db"], "b/x.bin", "B", size=9)
    rebuild_groups(conn)
    assert len(duplicate_groups(conn, confidence="probable")) == 1


def test_a_group_stays_probable_while_one_pair_is_open(conn, catalog, clouds):
    """Two Drive copies prove each other different, but the unhashed third is unsettled against both."""
    catalog.file(clouds["d1"], "a/x.bin", size=9, md5="aa")
    catalog.file(clouds["d2"], "b/x.bin", size=9, md5="bb")
    icloud(catalog, clouds["ic"], "x.bin", size=9)
    rebuild_groups(conn)
    (prob,) = duplicate_groups(conn, confidence="probable")
    assert len(prob.copies) == 3


def test_copies_that_all_prove_each_other_different_are_not_probable(conn, catalog, clouds):
    for rid, md5 in (("d1", "aa"), ("d2", "bb")):
        catalog.file(clouds[rid], f"{rid}/x.bin", size=9, md5=md5)
    catalog.file(clouds["d1"], "z/x.bin", size=9, md5="cc")
    rebuild_groups(conn)
    assert duplicate_groups(conn, confidence="probable") == []


# --------------------------------------------------------------------------- record_bridge

def test_record_bridge_inserts_and_is_idempotent_on_sha256(conn):
    first = record_bridge(conn, H("A", 42))
    again = record_bridge(conn, H("A", 42))
    assert first == again
    (n,) = conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()
    assert n == 1
    r = conn.execute("SELECT * FROM hash_bridges WHERE id = ?", (first,)).fetchone()
    assert r["size"] == 42 and r["md5"] == H("A")["md5"] and r["computed_at"].endswith("Z")
    assert r["source_entry_id"] is None
    assert record_bridge(conn, H("B", 42)) != first


def _source(conn, bid):
    r = conn.execute("SELECT source_entry_id, source_mtime FROM hash_bridges WHERE id = ?", (bid,)).fetchone()
    return r["source_entry_id"], r["source_mtime"]


def test_record_bridge_stores_source_entry_and_mtime(conn, catalog, clouds):
    a = icloud(catalog, clouds["ic"], "a", mtime="2024-02-02T00:00:00Z")
    bid = record_bridge(conn, H("A"), source_entry_id=a, source_mtime="2024-02-02T00:00:00Z")
    assert _source(conn, bid) == (a, "2024-02-02T00:00:00Z")
    bare = record_bridge(conn, H("B"))
    assert _source(conn, bare) == (None, None)


def test_bridge_has_one_source_slot_held_by_the_first_valid_hashless_copy(conn, catalog, clouds):
    t = "2024-02-02T00:00:00Z"
    a = icloud(catalog, clouds["ic"], "a/x", mtime=t)
    b = icloud(catalog, clouds["ic"], "b/x", mtime=t)
    bid = record_bridge(conn, H("A"), source_entry_id=a, source_mtime=t)
    assert record_bridge(conn, H("A"), source_entry_id=b, source_mtime=t) == bid
    assert _source(conn, bid) == (a, t)                          # a is still valid: not displaced
    conn.execute("UPDATE entries SET mtime = '2025-01-01T00:00:00Z' WHERE id = ?", (a,))   # a was rescanned
    record_bridge(conn, H("A"), source_entry_id=b, source_mtime=t)
    assert _source(conn, bid) == (b, t)                          # stale holder replaced


def test_hashed_holder_does_not_block_a_hashless_source(conn, catalog, clouds):
    t = "2024-02-02T00:00:00Z"
    d = drive(catalog, clouds["d1"], "a", "A", mtime=t)
    i = icloud(catalog, clouds["ic"], "a", mtime=t)
    bid = record_bridge(conn, H("A"), source_entry_id=d, source_mtime=t)
    record_bridge(conn, H("A"), source_entry_id=i, source_mtime=t)
    assert _source(conn, bid) == (i, t)
    record_bridge(conn, H("A"), source_entry_id=d, source_mtime=t)       # and a hashed one never takes it back
    assert _source(conn, bid) == (i, t)


def test_a_weak_only_holder_keeps_the_slot_like_a_hashless_one(conn, catalog, clouds):
    """A OneDrive file is identified by its link only, so it is not displaced the way a strongly hashed holder is."""
    t = "2024-02-02T00:00:00Z"
    o = onedrive(catalog, clouds["od"], "a/x", "A", mtime=t)
    i = icloud(catalog, clouds["ic"], "b/x", mtime=t)
    bid = record_bridge(conn, H("A"), source_entry_id=o, source_mtime=t)
    record_bridge(conn, H("A"), source_entry_id=i, source_mtime=t)
    assert _source(conn, bid) == (o, t)


def test_record_bridge_unknown_source_entry_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        record_bridge(conn, H("A"), source_entry_id=424242)


@pytest.mark.parametrize("missing", HASH_TYPES + ("size",))
def test_record_bridge_requires_every_field(conn, missing):
    h = H("A")
    del h[missing]
    with pytest.raises(ValueError):
        record_bridge(conn, h)
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 0


@pytest.mark.parametrize("bad", ["", "xyz", "12 34", None, 123, "0x12"])
def test_record_bridge_rejects_non_hex(conn, bad):
    h = H("A")
    h["quickxor"] = bad
    with pytest.raises(ValueError):
        record_bridge(conn, h)


@pytest.mark.parametrize("bad", [-1, "10", None, 1.5, True])
def test_record_bridge_rejects_bad_size(conn, bad):
    h = H("A")
    h["size"] = bad
    with pytest.raises(ValueError):
        record_bridge(conn, h)


def test_record_bridge_lowercases_and_ignores_extra_keys(conn, catalog, clouds):
    d = drive(catalog, clouds["d1"], "a", "A")
    b = dropbox(catalog, clouds["db"], "a", "A")
    shouting = {k: (v.upper() if isinstance(v, str) else v) for k, v in H("A").items()} | {"note": "x"}
    bid = record_bridge(conn, shouting)
    assert conn.execute("SELECT md5 FROM hash_bridges WHERE id = ?", (bid,)).fetchone()[0] == H("A")["md5"]
    rebuild_groups(conn)
    assert groups(conn)[d] == groups(conn)[b]


def test_record_bridge_refuses_inconsistent_duplicate(conn):
    record_bridge(conn, H("A"))
    lie = H("A") | {"md5": H("B")["md5"]}
    with pytest.raises(ValueError):
        record_bridge(conn, lie)
    lie = H("A") | {"size": 11}
    with pytest.raises(ValueError):
        record_bridge(conn, lie)
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 1


# --------------------------------------------------------------------------- bridge_candidates

def test_candidate_for_cross_cloud_same_name_and_size(conn, catalog, clouds):
    d = drive(catalog, clouds["d1"], "a/report.pdf", "A", size=500)
    dropbox(catalog, clouds["db"], "b/report.pdf", "A", size=500)
    rebuild_groups(conn)
    (c,) = bridge_candidates(conn)
    assert (c.entry_id, c.remote, c.path, c.size) == (d, "d1", "a/report.pdf", 500)
    assert c.unresolved_copies == 1 and c.reason == "name+size"


def test_candidate_with_different_names_is_size_only(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a/report.pdf", "A", size=500)
    dropbox(catalog, clouds["db"], "b/final-v2.pdf", "A", size=500)
    rebuild_groups(conn)
    (c,) = bridge_candidates(conn)
    assert c.reason == "size" and c.unresolved_copies == 1


def test_no_candidate_when_nothing_is_ambiguous(conn, catalog, clouds):
    # same domain, different content: provably different
    drive(catalog, clouds["d1"], "a", "A", size=10)
    drive(catalog, clouds["d2"], "b", "B", size=10)
    # same domain, same content: already one group
    drive(catalog, clouds["d1"], "c", "C", size=20)
    drive(catalog, clouds["d2"], "c", "C", size=20)
    # overlapping domains (both carry md5): comparable
    r = catalog.remote("mem", "memory")
    catalog.file(r, "m", size=30, md5=H("M")["md5"])
    drive(catalog, clouds["d1"], "m2", "N", size=30)
    # different sizes
    drive(catalog, clouds["d1"], "s1", "S", size=40)
    dropbox(catalog, clouds["db"], "s1", "S", size=41)
    rebuild_groups(conn)
    assert bridge_candidates(conn) == []


def test_non_live_files_do_not_make_candidates(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a", "A")
    dropbox(catalog, clouds["db"], "a", "A", present=False)
    onedrive(catalog, clouds["od"], "a", "A", native_doc=True)
    rebuild_groups(conn)
    assert bridge_candidates(conn) == []


def test_download_preference_follows_provider_order(conn, catalog, clouds):
    loc = catalog.remote("loc", "local")
    # onedrive vs dropbox: QuickXor proves nothing, so it is the OneDrive file itself that has to be read
    od = onedrive(catalog, clouds["od"], "x", "A", size=10)
    dropbox(catalog, clouds["db"], "x", "A", size=10)
    # a partial local entry (only the dropbox hash) vs drive: local wins
    drive(catalog, clouds["d1"], "y", "B", size=20)
    loc_e = catalog.file(loc, "y", size=20, dropbox=H("B")["dropbox"], quickxor=H("B")["quickxor"])
    # drive vs dropbox: drive wins
    d = drive(catalog, clouds["d1"], "z", "C", size=30)
    dropbox(catalog, clouds["db"], "z", "C", size=30)
    rebuild_groups(conn)
    by_size = {c.size: c for c in bridge_candidates(conn)}
    assert by_size[10].entry_id == od
    assert by_size[20].entry_id == loc_e
    assert by_size[30].entry_id == d


def test_download_from_the_smaller_side_to_settle_more_copies(conn, catalog, clouds):
    """Two identical Drive copies and one Dropbox copy: hashing the Dropbox one settles both Drive copies."""
    drive(catalog, clouds["d1"], "a/x.bin", "A", size=10)
    drive(catalog, clouds["d2"], "b/x.bin", "A", size=10)
    dbx = dropbox(catalog, clouds["db"], "c/x.bin", "A", size=10)
    rebuild_groups(conn)
    (c,) = bridge_candidates(conn)
    assert c.entry_id == dbx and c.unresolved_copies == 2


def test_ranking_name_matches_first_then_saving(conn, catalog, clouds):
    # size-only candidate with the biggest potential saving
    drive(catalog, clouds["d1"], "big-a.bin", "BIG", size=1_000_000)
    dropbox(catalog, clouds["db"], "big-b.bin", "BIG", size=1_000_000)
    # two name+size candidates: saving 200 and 3000
    drive(catalog, clouds["d1"], "m.bin", "M", size=100)
    dropbox(catalog, clouds["db"], "m.bin", "M", size=100)
    dropbox(catalog, clouds["db"], "copy/m.bin", "M", size=100)
    drive(catalog, clouds["d1"], "n.bin", "N", size=3000)
    dropbox(catalog, clouds["db"], "n.bin", "N", size=3000)
    rebuild_groups(conn)
    got = bridge_candidates(conn)
    assert [(c.size, c.reason) for c in got] == [(3000, "name+size"), (100, "name+size"), (1_000_000, "size")]
    assert got[1].unresolved_copies == 2
    assert [c.unresolved_copies * c.size for c in got[:2]] == [3000, 200]


def test_max_bytes_budget(conn, catalog, clouds):
    for tag, size in (("A", 500), ("B", 300), ("C", 200), ("D", 5000)):
        drive(catalog, clouds["d1"], f"{tag}.bin", tag, size=size)
        dropbox(catalog, clouds["db"], f"{tag}.bin", tag, size=size)
    rebuild_groups(conn)
    assert [c.size for c in bridge_candidates(conn)] == [5000, 500, 300, 200]
    assert [c.size for c in bridge_candidates(conn, max_bytes=800)] == [500, 300]       # 5000 never fits
    assert [c.size for c in bridge_candidates(conn, max_bytes=700)] == [500, 200]       # skips 300, still fills
    assert [c.size for c in bridge_candidates(conn, max_bytes=499)] == [300]
    assert bridge_candidates(conn, max_bytes=100) == []
    total = sum(c.size for c in bridge_candidates(conn, max_bytes=1000))
    assert total <= 1000


def test_limit_and_min_size(conn, catalog, clouds):
    for tag, size in (("A", 500), ("B", 300), ("C", 200)):
        drive(catalog, clouds["d1"], f"{tag}.bin", tag, size=size)
        dropbox(catalog, clouds["db"], f"{tag}.bin", tag, size=size)
    drive(catalog, clouds["d1"], "empty", "Z", size=0)
    dropbox(catalog, clouds["db"], "empty", "Z", size=0)
    rebuild_groups(conn)
    assert [c.size for c in bridge_candidates(conn, limit=2)] == [500, 300]
    assert [c.size for c in bridge_candidates(conn)] == [500, 300, 200]            # size 0 skipped by default
    assert 0 in [c.size for c in bridge_candidates(conn, min_size=0)]
    assert [c.size for c in bridge_candidates(conn, min_size=300)] == [500, 300]
    assert bridge_candidates(conn, limit=0) == []


def test_bridged_class_stops_being_a_candidate(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a", "A", size=10)
    dropbox(catalog, clouds["db"], "a", "A", size=10)
    drive(catalog, clouds["d2"], "b", "B", size=11)
    dropbox(catalog, clouds["db"], "b", "B", size=11)
    rebuild_groups(conn)
    assert sorted(c.size for c in bridge_candidates(conn)) == [10, 11]
    record_bridge(conn, H("A", 10))
    assert [c.size for c in bridge_candidates(conn)] == [11]       # even before the regroup: the work is done
    rebuild_groups(conn)
    assert [c.size for c in bridge_candidates(conn)] == [11]


def test_a_bridge_does_not_close_the_class_of_a_onedrive_file(conn, catalog, clouds):
    """The bridge made from the Drive copy has the OneDrive file's QuickXor, which is not its bytes: only its own
    download settles it, so it is the candidate; the Drive/Dropbox pair, bridged, is not."""
    d = drive(catalog, clouds["d1"], "a", "A", size=10)
    dropbox(catalog, clouds["db"], "a", "A", size=10)
    o = onedrive(catalog, clouds["od"], "a", "A", size=10, mtime=T0)
    rebuild_groups(conn)
    assert sorted(c.entry_id for c in bridge_candidates(conn)) == [d, o]    # the hashed-domain pair, and the OneDrive file
    record_bridge(conn, H("A", 10))
    rebuild_groups(conn)
    (c,) = bridge_candidates(conn)
    assert c.entry_id == o and c.unresolved_copies == 2 and c.reason == "name+size"
    record_bridge(conn, H("A", 10), source_entry_id=o, source_mtime=T0)
    rebuild_groups(conn)
    assert bridge_candidates(conn) == []


def test_bridge_that_proves_difference_also_closes_the_class(conn, catalog, clouds):
    """Drive and Dropbox match, the OneDrive file has other bytes: its QuickXor differs from the bridge's, so
    nothing is left to download."""
    d = drive(catalog, clouds["d1"], "a", "A", size=10)
    b = dropbox(catalog, clouds["db"], "a", "A", size=10)
    o = onedrive(catalog, clouds["od"], "a", "OTHER", size=10)
    record_bridge(conn, H("A"))
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[b] and g[o] is None
    assert bridge_candidates(conn) == []


def test_a_quickxor_that_differs_closes_a_onedrive_pair_without_a_download(conn, catalog, clouds):
    """Same size, different QuickXor: provably different files, no candidate. Equal QuickXor proves nothing: both are."""
    a = onedrive(catalog, clouds["od"], "x/a.bin", "A", size=10)
    b = onedrive(catalog, clouds["od"], "y/b.bin", "B", size=10)
    c = onedrive(catalog, clouds["od"], "x/c.bin", "C", size=20)
    c2 = catalog.file(clouds["od"], "y/c.bin", size=20, quickxor=H("C")["quickxor"])       # same QuickXor as c
    rebuild_groups(conn)
    assert sorted(x.entry_id for x in bridge_candidates(conn)) == [c, c2]
    assert a not in [x.entry_id for x in bridge_candidates(conn)] and b not in [x.entry_id for x in bridge_candidates(conn)]


def test_a_big_class_is_counted_without_dropping_counterparts(conn, catalog, clouds, monkeypatch):
    """The tally path of a big class cannot pair files up, so it keeps counterparts the pairwise path would drop."""
    onedrive(catalog, clouds["od"], "x/a.bin", "A", size=10)
    onedrive(catalog, clouds["od"], "y/b.bin", "B", size=10)
    rebuild_groups(conn)
    assert bridge_candidates(conn) == []
    monkeypatch.setattr(identity, "_PAIRWISE_LIMIT", 0)
    assert len(bridge_candidates(conn)) == 2


def test_bridge_for_one_content_leaves_other_contents_of_the_size_open(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a", "A", size=10)
    dropbox(catalog, clouds["db"], "a", "A", size=10)
    d2 = drive(catalog, clouds["d1"], "b", "B", size=10)
    b2 = dropbox(catalog, clouds["db"], "b", "B", size=10)
    record_bridge(conn, H("A"))
    rebuild_groups(conn)
    (c,) = bridge_candidates(conn)
    assert c.entry_id in (d2, b2)           # content B is still unlinked across clouds


def test_candidates_ignore_hash_free_directory_rows(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a/b/c.bin", "A", size=10)
    dropbox(catalog, clouds["db"], "x/y/c.bin", "A", size=10)
    rebuild_groups(conn)
    assert len(bridge_candidates(conn)) == 1


# --------------------------------------------------------------------------- bridge_entry

class FakeRclone:
    """download() writes canned bytes to the destination, like rclone copyto would."""

    def __init__(self, files: dict[tuple[str, str], bytes], *, fail_after_write: bool = False):
        self.files = files
        self.calls: list[tuple[str, str, str]] = []
        self.fail_after_write = fail_after_write

    def download(self, remote, path, local_path):
        self.calls.append((remote, path, local_path))
        Path(local_path).write_bytes(self.files[(remote, path)])
        if self.fail_after_write:
            raise RuntimeError("connection reset")


def fake_hashes(data: bytes) -> dict:
    return {
        "md5": hashlib.md5(data).hexdigest(), "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "dropbox": hashlib.sha256(b"dbx" + data).hexdigest(), "quickxor": hashlib.sha1(b"qx" + data).hexdigest(),
        "size": len(data),
    }


@pytest.fixture
def fake_hash(monkeypatch):
    seen: list[Path] = []

    def hash_file(path):
        seen.append(Path(path))
        assert Path(path).exists()
        return fake_hashes(Path(path).read_bytes())

    monkeypatch.setattr(hashing, "hash_file", hash_file, raising=False)
    return seen


def test_bridge_entry_records_bridge_and_cleans_up(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"hello bridge"
    fh = fake_hashes(data)
    d = catalog.file(clouds["d1"], "docs/a.txt", size=len(data), md5=fh["md5"], sha256=fh["sha256"])
    b = catalog.file(clouds["db"], "other/a.txt", size=len(data), dropbox=fh["dropbox"])
    o = catalog.file(clouds["od"], "o/a.txt", size=len(data), quickxor=fh["quickxor"])
    rc = FakeRclone({("d1:", "docs/a.txt"): data})
    tmpdir = tmp_path / "scratch"                       # does not exist yet

    out = bridge_entry(conn, rc, d, tmpdir)

    assert out["entry_id"] == d and out["hashes"] == fh
    assert rc.calls[0][:2] == ("d1:", "docs/a.txt")      # read_remote of the entry's remote
    assert fake_hash and fake_hash[0].parent == tmpdir
    assert list(tmpdir.iterdir()) == []                  # temp file deleted
    r = conn.execute("SELECT * FROM hash_bridges WHERE id = ?", (out["bridge_id"],)).fetchone()
    assert r["source_entry_id"] == d and r["sha256"] == fh["sha256"] and r["size"] == len(data)

    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[b] == d and g[o] is None            # the OneDrive file shares a QuickXor, not the bytes

    out = bridge_entry(conn, FakeRclone({("od:", "o/a.txt"): data}), o, tmp_path)
    assert out["linked"] is True and out["bridge_id"] == conn.execute(
        "SELECT id FROM hash_bridges WHERE sha256 = ?", (fh["sha256"],)).fetchone()[0]
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == g[b] == g[o] == d


def test_bridge_entry_twice_returns_the_same_bridge(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"again"
    fh = fake_hashes(data)
    d = catalog.file(clouds["d1"], "a", size=len(data), md5=fh["md5"])
    rc = FakeRclone({("d1:", "a"): data})
    assert bridge_entry(conn, rc, d, tmp_path)["bridge_id"] == bridge_entry(conn, rc, d, tmp_path)["bridge_id"]
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 1


def test_bridge_entry_native_hash_mismatch_raises_and_records_nothing(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"new content!"
    fh = fake_hashes(data)
    # catalog says md5 of something else, same size: the file changed after the scan
    d = catalog.file(clouds["d1"], "a", size=len(data), md5=hashlib.md5(b"old content!").hexdigest())
    rc = FakeRclone({("d1:", "a"): data})
    with pytest.raises(ValueError, match="changed since the scan"):
        bridge_entry(conn, rc, d, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 0
    assert list(tmp_path.iterdir()) == []
    assert fh["md5"] != row(conn, d)["md5"]


@pytest.mark.parametrize("hash_type", ["sha1", "sha256", "dropbox", "quickxor"])
def test_bridge_entry_checks_every_native_hash(conn, catalog, clouds, fake_hash, tmp_path, hash_type):
    data = b"payload"
    fh = fake_hashes(data)
    native = {t: fh[t] for t in ("md5", "sha1", "sha256", "dropbox", "quickxor")}
    native[hash_type] = "0" * len(native[hash_type])
    loc = catalog.remote("loc", "local")
    e = catalog.file(loc, "a", size=len(data), **native)
    with pytest.raises(ValueError):
        bridge_entry(conn, FakeRclone({("loc:", "a"): data}), e, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 0


def test_bridge_entry_native_hash_comparison_ignores_case(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"payload"
    fh = fake_hashes(data)
    d = catalog.file(clouds["d1"], "a", size=len(data), md5=fh["md5"].upper())
    assert bridge_entry(conn, FakeRclone({("d1:", "a"): data}), d, tmp_path)["bridge_id"]


def test_upper_case_native_hash_is_no_contradiction_for_its_own_bridge(conn, catalog, clouds, fake_hash, tmp_path):
    """A source whose stored hash differs from the bridge only in case is still the same bytes, not a conflict."""
    data = b"payload"
    fh = fake_hashes(data)
    d = catalog.file(clouds["d1"], "a", size=len(data), mtime=T0, md5=fh["md5"].upper(), sha1=fh["sha1"].upper())
    b = catalog.file(clouds["db"], "a", size=len(data), dropbox=fh["dropbox"])
    assert bridge_entry(conn, FakeRclone({("d1:", "a"): data}), d, tmp_path)["linked"]
    stats = rebuild_groups(conn)
    assert groups(conn)[d] == groups(conn)[b] == d and stats.hash_conflicts == 0


def test_bridge_entry_size_mismatch_raises(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"twelve bytes"
    fh = fake_hashes(data)
    d = catalog.file(clouds["d1"], "a", size=len(data) + 5, md5=fh["md5"])
    with pytest.raises(ValueError, match="size"):
        bridge_entry(conn, FakeRclone({("d1:", "a"): data}), d, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 0
    assert list(tmp_path.iterdir()) == []


def test_bridge_entry_without_native_hashes(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"icloud bytes"
    i = catalog.file(clouds["ic"], "a", size=len(data))
    out = bridge_entry(conn, FakeRclone({("ic:", "a"): data}), i, tmp_path)
    assert out["hashes"] == fake_hashes(data)
    assert list(tmp_path.iterdir()) == []


def test_bridge_entry_cleans_up_when_download_fails(conn, catalog, clouds, fake_hash, tmp_path):
    d = catalog.file(clouds["d1"], "a", size=3, md5="aa")
    rc = FakeRclone({("d1:", "a"): b"abc"}, fail_after_write=True)
    with pytest.raises(RuntimeError, match="connection reset"):
        bridge_entry(conn, rc, d, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert not fake_hash                                # never reached hashing
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 0


def test_bridge_entry_cleans_up_when_hashing_fails(conn, catalog, clouds, tmp_path, monkeypatch):
    d = catalog.file(clouds["d1"], "a", size=3, md5="aa")

    def explode(path):
        raise OSError("read error")

    monkeypatch.setattr(hashing, "hash_file", explode, raising=False)
    with pytest.raises(OSError):
        bridge_entry(conn, FakeRclone({("d1:", "a"): b"abc"}), d, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["missing", "tombstone", "directory", "native_doc", "unknown_size"])
def test_bridge_entry_refuses_non_live_entries_without_downloading(conn, catalog, clouds, fake_hash, tmp_path, kind):
    r = clouds["d1"]
    target = {
        "missing": 987654,
        "tombstone": catalog.file(r, "t", size=3, md5="aa", present=False),
        "directory": catalog.dir(r, "dir"),
        "native_doc": catalog.file(r, "doc", size=3, native_doc=True),
        "unknown_size": catalog._insert(r, "u", is_dir=False, size=None),
    }[kind]
    rc = FakeRclone({})
    with pytest.raises(ValueError):
        bridge_entry(conn, rc, target, tmp_path)
    assert rc.calls == []


def test_bridge_entry_rejects_malformed_hasher_output(conn, catalog, clouds, tmp_path, monkeypatch):
    d = catalog.file(clouds["d1"], "a", size=3)
    monkeypatch.setattr(hashing, "hash_file", lambda p: {"md5": "aa", "size": 3}, raising=False)
    with pytest.raises(ValueError):
        bridge_entry(conn, FakeRclone({("d1:", "a"): b"abc"}), d, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_end_to_end_candidates_then_bridge_then_regroup(conn, catalog, clouds, fake_hash, tmp_path):
    """The loop the service runs: pick candidates, hash each once, rebuild, nothing left to do."""
    files = {}
    for n, name in enumerate(("a", "b", "c")):
        data = f"content of {name}".encode() * (n + 1)
        fh = fake_hashes(data)
        files[("d1:", f"{name}.bin")] = data
        files[("od:", f"copy/{name}.bin")] = data
        catalog.file(clouds["d1"], f"{name}.bin", size=len(data), md5=fh["md5"], sha1=fh["sha1"], sha256=fh["sha256"])
        catalog.file(clouds["db"], f"copy/{name}.bin", size=len(data), dropbox=fh["dropbox"])
        catalog.file(clouds["od"], f"copy/{name}.bin", size=len(data), mtime=T0, quickxor=fh["quickxor"])
    rebuild_groups(conn)
    assert duplicate_groups(conn) == [] and len(duplicate_groups(conn, confidence="probable")) == 3
    rc = FakeRclone(files)
    cands = bridge_candidates(conn)
    assert len(cands) == 6                              # per content: the Drive/Dropbox pair, and the OneDrive file
    for cand in cands:
        bridge_entry(conn, rc, cand.entry_id, tmp_path)
    stats = rebuild_groups(conn)
    assert stats.duplicate_groups == 3 and stats.redundant_files == 6
    assert bridge_candidates(conn) == []
    assert duplicate_groups(conn, confidence="probable") == []
    assert len(rc.calls) == 6                           # one download per hash domain, and one per OneDrive copy


# --------------------------------------------------------------------------- hashless files via their own bridge

T0 = "2024-03-03T00:00:00Z"


def test_icloud_file_becomes_exact_through_its_own_download(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"vacation photo bytes"
    fh = fake_hashes(data)
    d = catalog.file(clouds["d1"], "Photos/a.jpg", size=len(data), md5=fh["md5"], sha1=fh["sha1"], sha256=fh["sha256"])
    i = catalog.file(clouds["ic"], "Camera/a.jpg", size=len(data), mtime=T0)
    rebuild_groups(conn)
    assert groups(conn)[i] is None and duplicate_groups(conn) == []

    out = bridge_entry(conn, FakeRclone({("ic:", "Camera/a.jpg"): data}), i, tmp_path)
    assert out["linked"] is True
    assert _source(conn, out["bridge_id"]) == (i, T0)
    stats = rebuild_groups(conn)

    assert groups(conn)[i] == groups(conn)[d] == d
    assert stats.duplicate_groups == 1 and stats.redundant_files == 1 and stats.redundant_bytes == len(data)
    assert same_content(conn, i, d)
    assert {c.entry_id for c in locations(conn, i)} == {i, d}
    (grp,) = duplicate_groups(conn)
    assert grp.remotes == ["d1", "ic"]
    assert duplicate_groups(conn, confidence="probable") == []

    # the file is rescanned with a new mtime: the link no longer vouches for its bytes
    conn.execute("UPDATE entries SET mtime = '2025-05-05T00:00:00Z' WHERE id = ?", (i,))
    stats = rebuild_groups(conn)
    assert groups(conn)[i] is None and groups(conn)[d] == d
    assert stats.changed == 1 and duplicate_groups(conn) == []
    assert not same_content(conn, i, d)
    assert len(duplicate_groups(conn, confidence="probable")) == 1       # falls back to a candidate


def test_source_link_is_dropped_when_size_changes_or_entry_dies(conn, catalog, clouds):
    i = icloud(catalog, clouds["ic"], "a", size=10, mtime=T0)
    d = drive(catalog, clouds["d1"], "a", "A", size=10)
    record_bridge(conn, H("A"), source_entry_id=i, source_mtime=T0)
    rebuild_groups(conn)
    assert groups(conn)[i] == groups(conn)[d] == min(i, d)
    conn.execute("UPDATE entries SET size = 11 WHERE id = ?", (i,))
    rebuild_groups(conn)
    assert groups(conn)[i] is None
    conn.execute("UPDATE entries SET size = 10 WHERE id = ?", (i,))
    rebuild_groups(conn)
    assert groups(conn)[i] == groups(conn)[d]                    # restored state, restored link
    conn.execute("UPDATE entries SET present = 0 WHERE id = ?", (i,))
    rebuild_groups(conn)
    assert groups(conn)[i] is None


def test_source_link_needs_a_recorded_mtime(conn, catalog, clouds):
    i = icloud(catalog, clouds["ic"], "a", mtime=T0)
    drive(catalog, clouds["d1"], "a", "A")
    record_bridge(conn, H("A"), source_entry_id=i)               # no source_mtime: cannot be validated
    rebuild_groups(conn)
    assert groups(conn)[i] is None


def test_source_with_a_contradicting_native_hash_is_not_linked(conn, catalog, clouds):
    """A hashed source whose stored hash disagrees with the bridge must not be pulled into its group."""
    d = drive(catalog, clouds["d1"], "a", "A", mtime=T0)
    liar = catalog.file(clouds["d2"], "b", size=10, mtime=T0, sha256=H("OTHER")["sha256"])
    sha1_liar = catalog.file(clouds["d2"], "e", size=10, mtime=T0, sha1=H("OTHER")["sha1"])
    weak_liar = catalog.file(clouds["od"], "c", size=10, mtime=T0, quickxor=H("OTHER")["quickxor"])
    record_bridge(conn, H("A"), source_entry_id=liar, source_mtime=T0)
    record_bridge(conn, H("A"), source_entry_id=sha1_liar, source_mtime=T0)
    record_bridge(conn, H("A"), source_entry_id=weak_liar, source_mtime=T0)
    rebuild_groups(conn)
    g = groups(conn)
    assert g[d] == d and g[liar] == liar
    assert g[sha1_liar] is None and g[weak_liar] is None                # not even a weak hash may disagree


def test_lone_linked_source_is_a_singleton_group(conn, catalog, clouds):
    i = icloud(catalog, clouds["ic"], "a", mtime=T0)
    other = icloud(catalog, clouds["ic"], "b", mtime=T0)
    record_bridge(conn, H("A"), source_entry_id=i, source_mtime=T0)
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[i] == i and g[other] is None
    assert (stats.files_hashed, stats.groups, stats.duplicate_groups) == (1, 1, 0)
    assert duplicate_groups(conn) == []


def test_two_hashless_copies_with_the_same_bytes_are_both_linked(conn, catalog, clouds, fake_hash, tmp_path):
    """bridge_sources holds one row per downloaded copy, so a bridge is not limited to one hashless source."""
    data = b"same bytes twice"
    a = catalog.file(clouds["ic"], "a/x.bin", size=len(data), mtime=T0)
    b = catalog.file(clouds["ic"], "b/x.bin", size=len(data), mtime=T0)
    rc = FakeRclone({("ic:", "a/x.bin"): data, ("ic:", "b/x.bin"): data})
    first = bridge_entry(conn, rc, a, tmp_path)
    second = bridge_entry(conn, rc, b, tmp_path)
    assert first["bridge_id"] == second["bridge_id"]
    assert first["linked"] is True and second["linked"] is True
    stats = rebuild_groups(conn)
    assert groups(conn)[a] == groups(conn)[b] == a
    assert (stats.duplicate_groups, stats.redundant_files, stats.redundant_bytes) == (1, 1, len(data))
    assert same_content(conn, a, b)
    (grp,) = duplicate_groups(conn)
    assert [c.entry_id for c in grp.copies] == [a, b]
    assert duplicate_groups(conn, confidence="probable") == []


def test_second_hashless_copy_stops_being_a_candidate_once_downloaded(conn, catalog, clouds, fake_hash, tmp_path):
    """It used to reappear forever: the bridge had one source slot and the first copy held it."""
    data = b"candidates until each is linked"
    a = catalog.file(clouds["ic"], "a/x.bin", size=len(data), mtime=T0)
    b = catalog.file(clouds["ic"], "b/x.bin", size=len(data), mtime=T0)
    rc = FakeRclone({("ic:", "a/x.bin"): data, ("ic:", "b/x.bin"): data})
    assert sorted(c.entry_id for c in bridge_candidates(conn)) == [a, b]
    bridge_entry(conn, rc, a, tmp_path)
    rebuild_groups(conn)
    assert [c.entry_id for c in bridge_candidates(conn)] == [b]      # its own bytes are still unproven
    bridge_entry(conn, rc, b, tmp_path)
    rebuild_groups(conn)
    assert bridge_candidates(conn) == []


def test_every_linked_copy_joins_the_cross_cloud_group(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"three hashless copies and a drive copy"
    fh = fake_hashes(data)
    d = catalog.file(clouds["d1"], "z.bin", size=len(data), md5=fh["md5"], sha1=fh["sha1"], sha256=fh["sha256"])
    copies = [catalog.file(clouds["ic"], f"{n}/x.bin", size=len(data), mtime=T0) for n in "abc"]
    rc = FakeRclone({("ic:", f"{n}/x.bin"): data for n in "abc"})
    for e in copies:
        assert bridge_entry(conn, rc, e, tmp_path)["linked"]
    stats = rebuild_groups(conn)
    assert {groups(conn)[e] for e in copies + [d]} == {d}
    assert (stats.duplicate_groups, stats.redundant_files) == (1, 3)


def test_each_source_is_validated_on_its_own(conn, catalog, clouds, fake_hash, tmp_path):
    """One copy being rescanned breaks only its own link."""
    data = b"validity is per source"
    a = catalog.file(clouds["ic"], "a/x.bin", size=len(data), mtime=T0)
    b = catalog.file(clouds["ic"], "b/x.bin", size=len(data), mtime=T0)
    rc = FakeRclone({("ic:", "a/x.bin"): data, ("ic:", "b/x.bin"): data})
    bridge_entry(conn, rc, a, tmp_path)
    bridge_entry(conn, rc, b, tmp_path)
    rebuild_groups(conn)
    assert groups(conn)[a] == groups(conn)[b] == a
    conn.execute("UPDATE entries SET mtime = '2025-05-05T00:00:00Z' WHERE id = ?", (a,))       # a changed
    rebuild_groups(conn)
    assert groups(conn)[a] is None and groups(conn)[b] == b
    assert [c.entry_id for c in bridge_candidates(conn)] == [a]
    conn.execute("UPDATE entries SET present = 0 WHERE id = ?", (b,))                           # b is gone
    rebuild_groups(conn)
    assert groups(conn)[b] is None


def test_record_bridge_adds_a_source_row_per_entry_and_refreshes_repeats(conn, catalog, clouds):
    a = icloud(catalog, clouds["ic"], "a", mtime=T0)
    b = icloud(catalog, clouds["ic"], "b", mtime=T0)
    bid = record_bridge(conn, H("A"), source_entry_id=a, source_mtime=T0)
    assert record_bridge(conn, H("A"), source_entry_id=b, source_mtime=T0) == bid

    def sources():
        return [tuple(r) for r in conn.execute(
            "SELECT bridge_id, entry_id, size, mtime FROM bridge_sources ORDER BY entry_id")]

    assert sources() == [(bid, a, 10, T0), (bid, b, 10, T0)]
    record_bridge(conn, H("A"), source_entry_id=a, source_mtime="2025-01-01T00:00:00Z")      # a downloaded again
    assert sources()[0] == (bid, a, 10, "2025-01-01T00:00:00Z") and len(sources()) == 2
    record_bridge(conn, H("A"))                                                               # no source: no row
    assert len(sources()) == 2
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 1


def test_record_bridge_keeps_the_first_source_in_the_legacy_columns(conn, catalog, clouds):
    """hash_bridges.source_* still names a source for readers that predate bridge_sources (the executor)."""
    a = icloud(catalog, clouds["ic"], "a", mtime=T0)
    b = icloud(catalog, clouds["ic"], "b", mtime=T0)
    bid = record_bridge(conn, H("A"), source_entry_id=a, source_mtime=T0)
    record_bridge(conn, H("A"), source_entry_id=b, source_mtime=T0)
    assert _source(conn, bid) == (a, T0)
    record_bridge(conn, H("A"), source_entry_id=a, source_mtime="2025-01-01T00:00:00Z")
    assert _source(conn, bid) == (a, "2025-01-01T00:00:00Z")         # its own refresh is mirrored


def test_record_bridge_with_an_unknown_source_leaves_nothing_behind(conn):
    with pytest.raises(sqlite3.IntegrityError):
        record_bridge(conn, H("A"), source_entry_id=424242, source_mtime=T0)
    assert conn.execute("SELECT COUNT(*) FROM hash_bridges").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM bridge_sources").fetchone()[0] == 0
    assert not conn.in_transaction


def test_record_bridge_works_inside_a_transaction_and_rolls_back_with_it(conn, catalog, clouds):
    a = icloud(catalog, clouds["ic"], "a", mtime=T0)
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            record_bridge(conn, H("A"), source_entry_id=a, source_mtime=T0)
            raise RuntimeError("abort")
    assert conn.execute("SELECT COUNT(*) FROM bridge_sources").fetchone()[0] == 0
    with db.transaction(conn):
        record_bridge(conn, H("A"), source_entry_id=a, source_mtime=T0)
    assert conn.execute("SELECT COUNT(*) FROM bridge_sources").fetchone()[0] == 1


def test_an_entry_sourcing_two_different_bridges_is_linked_to_neither(conn, catalog, clouds, fake_hash, tmp_path):
    """Same size and mtime but different bytes were hashed at different times: which one is true is unknowable."""
    i = icloud(catalog, clouds["ic"], "a", mtime=T0)
    d = drive(catalog, clouds["d1"], "a", "A")
    o = drive(catalog, clouds["d2"], "b", "B")
    record_bridge(conn, H("A"), source_entry_id=i, source_mtime=T0)
    record_bridge(conn, H("B"), source_entry_id=i, source_mtime=T0)
    stats = rebuild_groups(conn)
    g = groups(conn)
    assert g[i] is None and g[d] == d and g[o] == o
    assert stats.hash_conflicts == 1
    assert [c.entry_id for c in bridge_candidates(conn)] == [i]      # needs a fresh look, not trusted as resolved


def test_ambiguous_source_is_reported_unlinked_by_bridge_entry(conn, catalog, clouds, fake_hash, tmp_path):
    i = catalog.file(clouds["ic"], "a", size=3, mtime=T0)
    record_bridge(conn, H("OLD", 3), source_entry_id=i, source_mtime=T0)        # stale record of other bytes
    out = bridge_entry(conn, FakeRclone({("ic:", "a"): b"new"}), i, tmp_path)
    assert out["linked"] is False


def test_hashed_source_contradicting_the_bridge_is_not_linked_even_with_many_sources(conn, catalog, clouds):
    good = icloud(catalog, clouds["ic"], "a", mtime=T0)
    liar = catalog.file(clouds["d2"], "b", size=10, mtime=T0, sha256=H("OTHER")["sha256"])
    bid = record_bridge(conn, H("A"), source_entry_id=good, source_mtime=T0)
    record_bridge(conn, H("A"), source_entry_id=liar, source_mtime=T0)
    rebuild_groups(conn)
    g = groups(conn)
    assert g[good] == good and g[liar] == liar
    assert conn.execute("SELECT COUNT(*) FROM bridge_sources WHERE bridge_id = ?", (bid,)).fetchone()[0] == 2


def test_bridge_entry_reports_linked_for_hashed_entries(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"payload"
    fh = fake_hashes(data)
    d = catalog.file(clouds["d1"], "a", size=len(data), md5=fh["md5"])
    assert bridge_entry(conn, FakeRclone({("d1:", "a"): data}), d, tmp_path)["linked"] is True


def test_bridge_entry_reports_a_weak_only_entry_linked_only_through_its_source_row(conn, catalog, clouds, fake_hash,
                                                                                  tmp_path):
    data = b"onedrive bytes"
    fh = fake_hashes(data)
    o = catalog.file(clouds["od"], "a", size=len(data), mtime=T0, quickxor=fh["quickxor"])
    nomtime = catalog.file(clouds["od"], "b", size=len(data), quickxor=fh["quickxor"])
    conn.execute("UPDATE entries SET mtime = NULL WHERE id = ?", (nomtime,))
    rc = FakeRclone({("od:", "a"): data, ("od:", "b"): data})
    assert bridge_entry(conn, rc, o, tmp_path)["linked"] is True
    assert bridge_entry(conn, rc, nomtime, tmp_path)["linked"] is False       # its QuickXor is no substitute
    stats = rebuild_groups(conn)
    assert groups(conn)[o] == o and groups(conn)[nomtime] is None and stats.files_hashed == 1


def test_bridge_entry_hashless_without_mtime_is_not_linked(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"no mtime"
    i = catalog.file(clouds["ic"], "a", size=len(data))
    conn.execute("UPDATE entries SET mtime = NULL WHERE id = ?", (i,))
    out = bridge_entry(conn, FakeRclone({("ic:", "a"): data}), i, tmp_path)
    assert out["linked"] is False and _source(conn, out["bridge_id"]) == (i, None)


def test_two_onedrive_copies_become_exact_only_when_both_were_downloaded(conn, catalog, clouds, fake_hash, tmp_path):
    """The dedupe case: equal QuickXor and size is a reason to look, two downloads are the proof."""
    data = b"the same onedrive bytes"
    qx = fake_hashes(data)["quickxor"]
    a = catalog.file(clouds["od"], "a/x.bin", size=len(data), mtime=T0, quickxor=qx)
    b = catalog.file(clouds["od"], "b/x.bin", size=len(data), mtime=T0, quickxor=qx)
    rc = FakeRclone({("od:", "a/x.bin"): data, ("od:", "b/x.bin"): data})
    rebuild_groups(conn)
    assert duplicate_groups(conn) == [] and sorted(c.entry_id for c in bridge_candidates(conn)) == [a, b]
    bridge_entry(conn, rc, a, tmp_path)
    rebuild_groups(conn)
    assert duplicate_groups(conn) == [] and not same_content(conn, a, b)      # one proven file proves nothing about b
    assert [c.entry_id for c in bridge_candidates(conn)] == [b]
    bridge_entry(conn, rc, b, tmp_path)
    rebuild_groups(conn)
    (grp,) = duplicate_groups(conn)
    assert [c.entry_id for c in grp.copies] == [a, b] and same_content(conn, a, b)
    assert bridge_candidates(conn) == []


def test_a_rebuild_unlearns_groups_that_rested_on_a_weak_hash(conn, catalog, clouds):
    """A catalog grouped by an older version (md5 / QuickXor as proof) is cleaned by the next rebuild."""
    a = onedrive(catalog, clouds["od"], "a", "A")
    b = onedrive(catalog, clouds["od"], "b", "A")
    c = catalog.file(clouds["d1"], "c", size=10, md5=H("A")["md5"])
    conn.executemany("UPDATE entries SET content_group = ? WHERE id = ?", [(a, a), (a, b), (c, c)])
    stats = rebuild_groups(conn)
    assert set(groups(conn).values()) == {None} and stats.changed == 3
    assert duplicate_groups(conn) == []


@pytest.mark.parametrize("seed", range(8))
def test_big_and_small_classes_count_the_same_when_no_hash_can_tell_files_apart(conn, catalog, clouds, monkeypatch,
                                                                              seed):
    """The tally path (big classes) and the pairwise path agree wherever the pairwise path has nothing to drop."""
    rng = random.Random(seed)
    for n in range(rng.randint(3, 12)):
        kind = rng.choice("dbi")
        if kind == "d":
            drive(catalog, clouds[rng.choice(("d1", "d2"))], f"{n}/{rng.choice('ab')}.bin", rng.choice("AB"), size=10)
        elif kind == "b":
            dropbox(catalog, clouds["db"], f"{n}/{rng.choice('ab')}.bin", rng.choice("AB"), size=10)
        else:
            icloud(catalog, clouds["ic"], f"{n}/{rng.choice('ab')}.bin", size=10, mtime=T0)
    rebuild_groups(conn)
    pairwise = bridge_candidates(conn, limit=1000)
    monkeypatch.setattr(identity, "_PAIRWISE_LIMIT", 0)
    assert bridge_candidates(conn, limit=1000) == pairwise


def test_rebuild_with_source_links_is_idempotent(conn, catalog, clouds):
    i = icloud(catalog, clouds["ic"], "a", mtime=T0)
    drive(catalog, clouds["d1"], "a", "A")
    record_bridge(conn, H("A"), source_entry_id=i, source_mtime=T0)
    assert rebuild_groups(conn).changed == 2
    before, meta_before = versions(conn), meta_version(conn)
    assert rebuild_groups(conn).changed == 0
    assert versions(conn) == before and meta_version(conn) == meta_before


def test_linked_hashless_file_joins_a_cross_cloud_group(conn, catalog, clouds):
    """iCloud copy bridged; its all-five bridge also pulls in the Drive and Dropbox copies, through their strong
    hashes. The OneDrive copy only shares the QuickXor, so it waits for its own download."""
    i = icloud(catalog, clouds["ic"], "a.bin", mtime=T0)
    d = drive(catalog, clouds["d1"], "a.bin", "A")
    b = dropbox(catalog, clouds["db"], "a.bin", "A")
    o = onedrive(catalog, clouds["od"], "a.bin", "A", mtime=T0)
    record_bridge(conn, H("A"), source_entry_id=i, source_mtime=T0)
    rebuild_groups(conn)
    g = groups(conn)
    assert g[i] == g[d] == g[b] == min(i, d, b) and g[o] is None
    record_bridge(conn, H("A"), source_entry_id=o, source_mtime=T0)
    rebuild_groups(conn)
    g = groups(conn)
    assert g[i] == g[d] == g[b] == g[o] == min(i, d, b, o)


# -- bridge_candidates with hashless files

def test_hashless_file_with_a_counterpart_is_its_own_candidate(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "Photos/IMG_1.jpg", "A", size=500)
    i = icloud(catalog, clouds["ic"], "Camera/IMG_1.jpg", size=500)
    rebuild_groups(conn)
    (c,) = bridge_candidates(conn)                      # the Drive copy alone cannot prove anything about it
    assert (c.entry_id, c.remote, c.path, c.size) == (i, "ic", "Camera/IMG_1.jpg", 500)
    assert c.unresolved_copies == 1 and c.reason == "name+size"


def test_hashless_candidate_without_a_counterpart_is_skipped(conn, catalog, clouds):
    icloud(catalog, clouds["ic"], "alone.bin", size=500)
    drive(catalog, clouds["d1"], "other-size.bin", "A", size=501)
    rebuild_groups(conn)
    assert bridge_candidates(conn) == []


def test_a_hashless_file_with_an_unknown_mtime_is_never_offered(conn, catalog, clouds, fake_hash, tmp_path):
    """A bridge is tied to a file by size and mtime; with no mtime the download could never link it, so it
    would be offered (and paid for out of --max-bytes) on every bridge run."""
    data = b"no usable modification time"
    fh = fake_hashes(data)
    catalog.file(clouds["d1"], "a.txt", size=len(data), md5=fh["md5"], sha256=fh["sha256"])
    i = catalog.file(clouds["ic"], "a.txt", size=len(data))
    other = catalog.file(clouds["ic"], "b/a.txt", size=len(data), mtime=T0)
    conn.execute("UPDATE entries SET mtime = NULL WHERE id = ?", (i,))
    rebuild_groups(conn)
    assert [c.entry_id for c in bridge_candidates(conn)] == [other]           # not `i`, and not a counterpart of `other`
    assert [c.unresolved_copies for c in bridge_candidates(conn)] == [1]       # the Drive copy only
    rc = FakeRclone({("ic:", "a.txt"): data, ("ic:", "b/a.txt"): data})
    for _ in range(3):                                                         # what Service.bridge does, repeatedly
        for cand in bridge_candidates(conn):
            bridge_entry(conn, rc, cand.entry_id, tmp_path)
        rebuild_groups(conn)
    assert [path for _, path, _ in rc.calls] == ["b/a.txt"]                   # one download; `i` is never fetched
    conn.execute("UPDATE entries SET mtime = NULL WHERE id = ?", (other,))
    rebuild_groups(conn)
    assert bridge_candidates(conn) == []


def test_every_hashless_copy_is_a_candidate_even_on_one_remote(conn, catalog, clouds):
    a = icloud(catalog, clouds["ic"], "a/x.bin", size=70)
    b = icloud(catalog, clouds["ic"], "b/x.bin", size=70)
    rebuild_groups(conn)
    got = bridge_candidates(conn)
    assert sorted(c.entry_id for c in got) == [a, b]
    assert all(c.unresolved_copies == 1 and c.reason == "name+size" for c in got)


def test_hashless_candidate_counts_all_counterparts(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "a.bin", "A", size=10)
    dropbox(catalog, clouds["db"], "a.bin", "A", size=10)
    i = icloud(catalog, clouds["ic"], "a.bin", size=10)
    icloud(catalog, clouds["ic"], "copy/other-name.bin", size=10)
    rebuild_groups(conn)
    got = {c.entry_id: c for c in bridge_candidates(conn)}
    assert got[i].unresolved_copies == 3                         # Drive, Dropbox and the other iCloud file
    assert got[i].reason == "name+size"
    hashed = [c for c in got.values() if c.remote in ("d1", "db")]
    assert len(hashed) == 1 and hashed[0].unresolved_copies == 1     # the Drive/Dropbox pair; iCloud not counted


def test_hashless_candidates_rank_name_matches_first(conn, catalog, clouds):
    drive(catalog, clouds["d1"], "big-a.bin", "A", size=9000)
    big = icloud(catalog, clouds["ic"], "big-b.bin", size=9000)             # biggest saving, name differs
    drive(catalog, clouds["d1"], "small.bin", "B", size=10)
    small = icloud(catalog, clouds["ic"], "small.bin", size=10)
    rebuild_groups(conn)
    got = bridge_candidates(conn)
    assert [(c.entry_id, c.reason) for c in got] == [(small, "name+size"), (big, "size")]
    assert [c.entry_id for c in bridge_candidates(conn, max_bytes=100)] == [small]


def test_linked_hashless_file_is_no_longer_a_candidate(conn, catalog, clouds, fake_hash, tmp_path):
    data = b"a linked hashless file"
    fh = fake_hashes(data)
    catalog.file(clouds["d1"], "a.bin", size=len(data), md5=fh["md5"], sha256=fh["sha256"])
    i = catalog.file(clouds["ic"], "a.bin", size=len(data), mtime=T0)
    rebuild_groups(conn)
    assert [c.entry_id for c in bridge_candidates(conn)] == [i]
    bridge_entry(conn, FakeRclone({("ic:", "a.bin"): data}), i, tmp_path)
    assert bridge_candidates(conn) == []                         # linked, even before the regroup
    rebuild_groups(conn)
    assert bridge_candidates(conn) == []
    conn.execute("UPDATE entries SET mtime = '2025-01-01T00:00:00Z' WHERE id = ?", (i,))
    rebuild_groups(conn)
    assert [c.entry_id for c in bridge_candidates(conn)] == [i]  # link broke: it needs a fresh download


def test_hashless_bridge_covers_the_hashed_domains_of_its_class(conn, catalog, clouds, fake_hash, tmp_path):
    """Dropbox-only counterpart: the iCloud file's own bridge (all five hashes) settles it, no extra download."""
    data = b"cross-cloud bytes"
    fh = fake_hashes(data)
    i = catalog.file(clouds["ic"], "a.bin", size=len(data), mtime=T0)
    b = catalog.file(clouds["db"], "a.bin", size=len(data), dropbox=fh["dropbox"])
    assert [c.entry_id for c in bridge_candidates(conn)] == [i]
    bridge_entry(conn, FakeRclone({("ic:", "a.bin"): data}), i, tmp_path)
    assert bridge_candidates(conn) == []
    rebuild_groups(conn)
    assert groups(conn)[i] == groups(conn)[b] == min(i, b)
    assert bridge_candidates(conn) == []

    # same size, different bytes on Dropbox: still settled (provably different), nothing more to download
    other = catalog.file(clouds["db"], "z.bin", size=len(data), dropbox=H("ZZZ")["dropbox"])
    rebuild_groups(conn)
    assert groups(conn)[other] == other and bridge_candidates(conn) == []


def test_hashless_bridge_does_not_double_count_other_pairs(conn, catalog, clouds, fake_hash, tmp_path):
    """Drive vs Dropbox is a separate ambiguity; the iCloud bridge neither hides nor inflates it."""
    data = b"three way class"
    fh = fake_hashes(data)
    i = catalog.file(clouds["ic"], "a.bin", size=len(data), mtime=T0)
    catalog.file(clouds["d1"], "elsewhere.bin", size=len(data), sha256=H("D-ONLY")["sha256"])
    catalog.file(clouds["db"], "other.bin", size=len(data), dropbox=H("B-ONLY")["dropbox"])
    rebuild_groups(conn)
    before = {c.entry_id: c for c in bridge_candidates(conn)}
    assert i in before and len(before) == 2 and before[i].unresolved_copies == 2

    bridge_entry(conn, FakeRclone({("ic:", "a.bin"): data}), i, tmp_path)
    rebuild_groups(conn)
    (left,) = bridge_candidates(conn)
    assert left.entry_id != i and left.unresolved_copies == 1 and left.reason == "size"
    assert fh["size"] == len(data)


def test_end_to_end_with_icloud(conn, catalog, clouds, fake_hash, tmp_path):
    """Service loop with a hashless copy: the iCloud copy and one hashed copy each cost one download."""
    data = b"end to end with icloud"
    fh = fake_hashes(data)
    catalog.file(clouds["d1"], "a.bin", size=len(data), md5=fh["md5"], sha1=fh["sha1"], sha256=fh["sha256"])
    catalog.file(clouds["db"], "a.bin", size=len(data), dropbox=fh["dropbox"])
    catalog.file(clouds["ic"], "a.bin", size=len(data), mtime=T0)
    rc = FakeRclone({("d1:", "a.bin"): data, ("ic:", "a.bin"): data})
    rebuild_groups(conn)
    cands = bridge_candidates(conn)
    assert len(cands) == 2                                       # one hashed-domain download + the iCloud file
    for cand in cands:
        assert bridge_entry(conn, rc, cand.entry_id, tmp_path)["linked"]
    stats = rebuild_groups(conn)
    assert stats.duplicate_groups == 1 and stats.redundant_files == 2
    assert bridge_candidates(conn) == []
    assert len(duplicate_groups(conn)[0].copies) == 3


# --------------------------------------------------------------------------- scale

def test_50k_entry_rebuild_is_fast_and_correct(conn, catalog):
    remotes = {n: catalog.remote(n, p) for n, p in
               (("d1", "drive"), ("d2", "drive"), ("db", "dropbox"), ("od", "onedrive"), ("ic", "iclouddrive"))}
    scan = {n: catalog._scan[r] for n, r in remotes.items()}
    rv = db.next_row_version(conn)
    N = 20_000

    def h(c):
        b = str(c).encode()
        return (hashlib.md5(b).hexdigest(), hashlib.sha1(b).hexdigest(), hashlib.sha256(b).hexdigest(),
                hashlib.sha256(b"d" + b).hexdigest(), hashlib.sha1(b"q" + b).hexdigest())

    rows, klass = [], []      # klass[i]: what identity the i-th inserted row should end up with

    def add(remote, c, path, *, md5=None, sha1=None, sha256=None, dropbox=None, quickxor=None,
            present=1, ident=None):
        size = 1000 + c % 5000                      # ~4 distinct contents share each size
        name = f"f{c}.bin"
        rows.append((remotes[remote], f"p:{path}", path, path.rsplit("/", 1)[0], name, name, "bin", "other", 0,
                     size, "2024-01-01T00:00:00Z", None, None, md5, sha1, sha256, dropbox, quickxor, 0, present,
                     scan[remote], scan[remote], rv))
        klass.append(ident)

    bridged = []
    for c in range(N):
        md5, sha1, sha256, dbx, qx = h(c)
        add("d1", c, f"d1/{c}/f", md5=md5, sha1=sha1, sha256=sha256, ident=("c", c))
        if c % 2 == 0:
            add("d2", c, f"d2/{c}/f", md5=md5, sha1=sha1, sha256=sha256, ident=("c", c))
        if c % 3 == 0:
            add("db", c, f"db/{c}/f", dropbox=dbx, ident=("c", c) if c % 6 == 0 else ("db", c))
        if c % 5 == 0:
            add("od", c, f"od/{c}/f", quickxor=qx)       # QuickXor identifies nothing: never grouped, bridge or not
        if c % 4 == 0:
            add("ic", c, f"ic/{c}/f")
        if c % 5 == 1:
            add("d2", c, f"gone/{c}/f", md5=md5, present=0)
        if c % 6 == 0:
            bridged.append((1000 + c % 5000, md5, sha1, sha256, dbx, qx))

    conn.executemany(
        """INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category, is_dir, size,
             mtime, mime, provider_id, md5, sha1, sha256, dropbox, quickxor, is_native_doc, present,
             first_seen_scan, last_seen_scan, row_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.executemany(
        "INSERT INTO hash_bridges(size, md5, sha1, sha256, dropbox, quickxor, computed_at) "
        "VALUES (?,?,?,?,?,?,'2024-01-01T00:00:00Z')", bridged)
    ids = [r[0] for r in conn.execute("SELECT id FROM entries ORDER BY id")]
    assert len(ids) == len(rows) >= 45_000

    t0 = time.perf_counter()
    stats = rebuild_groups(conn)
    elapsed = time.perf_counter() - t0
    assert elapsed < 8, f"rebuild took {elapsed:.1f}s"

    first_of: dict = {}
    for eid, k, rowdata in zip(ids, klass, rows):
        if k is not None and rowdata[19]:
            first_of.setdefault(k, eid)
    got = groups(conn)
    for eid, k, rowdata in zip(ids, klass, rows):
        want = first_of[k] if (k is not None and rowdata[19]) else None
        assert got[eid] == want, (eid, k)

    assert stats.size_conflicts == 0 and stats.hash_conflicts == 0
    live_members = Counter(k for k, r in zip(klass, rows) if k is not None and r[19])
    assert stats.duplicate_groups == sum(1 for n in live_members.values() if n > 1)
    assert stats.redundant_files == sum(n - 1 for n in live_members.values() if n > 1)

    t0 = time.perf_counter()
    again = rebuild_groups(conn)
    assert again.changed == 0
    assert time.perf_counter() - t0 < 8
    t0 = time.perf_counter()
    assert duplicate_groups(conn, limit=50)
    assert duplicate_groups(conn, confidence="probable", limit=50)
    assert bridge_candidates(conn, limit=50)
    assert time.perf_counter() - t0 < 8
