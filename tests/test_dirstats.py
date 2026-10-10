"""Tests for drivemerge.dirstats: recursive aggregates, tombstones, duplicates and refresh scope."""

from __future__ import annotations

import time
from collections import Counter

from drivemerge import db, dirstats


def stats(conn, rid, path):
    """(files, dirs, bytes, dup_files) for one directory, or None if it has no row."""
    row = dirstats.get(conn, rid, path)
    return None if row is None else (row["files"], row["dirs"], row["bytes"], row["dup_files"])


def rows(conn, rid):
    return [tuple(r) for r in conn.execute("SELECT * FROM dir_stats WHERE remote_id = ? ORDER BY path", (rid,))]


def set_groups(conn, groups: dict[int, int]):
    conn.executemany("UPDATE entries SET content_group = ? WHERE id = ?", [(g, eid) for eid, g in groups.items()])


def test_nested_tree_counts_and_mtime_max(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "a/x.txt", size=3, mtime="2024-01-05T00:00:00.000000Z")
    catalog.file(g, "a/b/y.txt", size=5, mtime="2024-02-01T00:00:00.000000Z")
    catalog.file(g, "a/b/c/z.txt", size=7, mtime="2023-12-31T23:59:59.000000Z")
    dirstats.refresh(conn)

    assert stats(conn, g, "a/b/c") == (1, 0, 7, 0)
    assert stats(conn, g, "a/b") == (2, 1, 12, 0)
    assert stats(conn, g, "a") == (3, 2, 15, 0)
    assert dirstats.get(conn, g, "a")["mtime_max"] == "2024-02-01T00:00:00.000000Z"
    assert dirstats.get(conn, g, "a/b/c")["mtime_max"] == "2023-12-31T23:59:59.000000Z"


def test_root_row_aggregates_the_whole_remote(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "a/x.txt", size=3)
    catalog.file(g, "top.txt", size=4)
    catalog.file(g, "a/b/y.txt", size=5)
    dirstats.refresh(conn)

    assert stats(conn, g, "") == (3, 2, 12, 0)


def test_empty_directories_get_zero_rows(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.dir(g, "empty")
    catalog.dir(g, "a")
    catalog.dir(g, "a/nothing")
    dirstats.refresh(conn)

    assert stats(conn, g, "empty") == (0, 0, 0, 0)
    assert dirstats.get(conn, g, "empty")["mtime_max"] is None
    assert stats(conn, g, "a") == (0, 1, 0, 0)
    assert stats(conn, g, "") == (0, 3, 0, 0)


def test_ancestors_of_a_file_get_rows_without_directory_entries(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog._insert(g, "x/y/f.txt", is_dir=False, size=4)  # a partial scan can leave no dir entries
    dirstats.refresh(conn)

    assert stats(conn, g, "x/y") == (1, 0, 4, 0)
    assert stats(conn, g, "x") == (1, 0, 4, 0)
    assert stats(conn, g, "") == (1, 0, 4, 0)


def test_tombstoned_entries_are_excluded(conn, catalog):
    g = catalog.remote("g", "drive")
    catalog.file(g, "a/keep.txt", size=3)
    catalog.file(g, "a/gone.txt", size=100, present=False)
    catalog._insert(g, "a/old", is_dir=True, present=False)
    catalog.file(g, "a/old/x.txt", size=50, present=False)
    dirstats.refresh(conn)

    assert stats(conn, g, "a") == (1, 0, 3, 0)
    assert stats(conn, g, "") == (1, 1, 3, 0)
    assert stats(conn, g, "a/old") is None


def test_dup_files_count_copies_across_remotes(conn, catalog):
    g = catalog.remote("g", "drive")
    d = catalog.remote("d", "dropbox")
    x = catalog.file(g, "docs/a.pdf", size=10)
    y = catalog.file(d, "backup/a copy.pdf", size=10)
    solo = catalog.file(g, "docs/solo.txt", size=4)
    set_groups(conn, {x: 1, y: 1, solo: 2})
    dirstats.refresh(conn)

    assert stats(conn, g, "docs") == (2, 0, 14, 1)
    assert stats(conn, d, "backup") == (1, 0, 10, 1)
    assert stats(conn, g, "") == (2, 1, 14, 1)
    assert stats(conn, d, "") == (1, 1, 10, 1)


def test_tombstoned_copy_does_not_make_a_duplicate(conn, catalog):
    g = catalog.remote("g", "drive")
    live = catalog.file(g, "a/w.txt", size=5)
    gone = catalog.file(g, "b/w.txt", size=5, present=False)
    set_groups(conn, {live: 3, gone: 3})
    dirstats.refresh(conn)

    assert stats(conn, g, "a") == (1, 0, 5, 0)
    assert stats(conn, g, "b") == (0, 0, 0, 0)


def test_refresh_of_one_remote_leaves_others_untouched(conn, catalog):
    g = catalog.remote("g", "drive")
    d = catalog.remote("d", "dropbox")
    catalog.file(g, "a/1.txt", size=1)
    catalog.file(d, "b/2.txt", size=2)
    dirstats.refresh(conn)
    d_before = rows(conn, d)

    catalog.file(g, "a/new.txt", size=10)
    catalog.file(d, "b/late.txt", size=20)
    result = dirstats.refresh(conn, remote_ids=[g])

    assert result["remotes"] == 1
    assert stats(conn, g, "a") == (2, 0, 11, 0)
    assert rows(conn, d) == d_before
    assert stats(conn, d, "b") == (1, 0, 2, 0)


def test_refresh_of_one_remote_sees_copies_on_other_remotes(conn, catalog):
    g = catalog.remote("g", "drive")
    d = catalog.remote("d", "dropbox")
    x = catalog.file(g, "a/x.bin", size=9)
    dirstats.refresh(conn)
    assert stats(conn, g, "a") == (1, 0, 9, 0)

    y = catalog.file(d, "c/x.bin", size=9)
    set_groups(conn, {x: 7, y: 7})
    dirstats.refresh(conn, remote_ids=[g])

    assert stats(conn, g, "a") == (1, 0, 9, 1)


def test_refresh_replaces_rows_of_removed_directories(conn, catalog):
    g = catalog.remote("g", "drive")
    empty = catalog.dir(g, "old")
    dirstats.refresh(conn)
    assert dirstats.get(conn, g, "old") is not None

    conn.execute("UPDATE entries SET present = 0 WHERE id = ?", (empty,))
    dirstats.refresh(conn)
    assert dirstats.get(conn, g, "old") is None


def test_refresh_defaults_to_all_remotes_and_reports(conn, catalog):
    g = catalog.remote("g", "drive")
    d = catalog.remote("d", "dropbox")
    catalog.file(g, "a/x.txt", size=1)
    catalog.dir(d, "empty")
    result = dirstats.refresh(conn)

    assert set(result) == {"remotes", "dirs", "seconds"}
    assert result["remotes"] == 2
    assert result["dirs"] == 4  # g: root, a; d: root, empty
    assert stats(conn, d, "") == (0, 1, 0, 0)

    assert dirstats.refresh(conn, remote_ids=[])["remotes"] == 0
    assert len(rows(conn, g)) == 2


def test_refresh_200k_files_in_seconds(conn, catalog):
    g = catalog.remote("g", "drive")
    scan = conn.execute("SELECT id FROM scans WHERE remote_id = ?", (g,)).fetchone()[0]
    rv = db.next_row_version(conn)

    def row(path, is_dir, size=None, mtime=None, group=None):
        parent, _, name = path.rpartition("/")
        return (g, f"p:{path}", path, parent, name, name.casefold(), "", "other",
                int(is_dir), size, mtime, 1, scan, scan, rv, group)

    insert = ("INSERT INTO entries(remote_id, entry_key, path, parent, name, name_norm, ext, category, "
              "is_dir, size, mtime, present, first_seen_scan, last_seen_scan, row_version, content_group) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    dir_rows = []
    for t in range(40):
        dir_rows.append(row(f"t{t:02d}", True))
        dir_rows.extend(row(f"t{t:02d}/s{s:02d}", True) for s in range(25))
    conn.executemany(insert, dir_rows)

    n = 200_000
    sizes, mtimes, groups = [], [], []
    file_rows = []
    for i in range(n):
        size = i % 1000 + 1
        mtime = f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}T00:00:00.000000Z"
        group = i // 2 if i % 3 else None
        sizes.append(size)
        mtimes.append(mtime)
        groups.append(group)
        file_rows.append(row(f"t{i % 40:02d}/s{(i // 40) % 25:02d}/f{i:06d}.bin", False, size, mtime, group))
    conn.executemany(insert, file_rows)

    counts = Counter(g_ for g_ in groups if g_ is not None)
    dup = [1 if g_ is not None and counts[g_] >= 2 else 0 for g_ in groups]

    started = time.perf_counter()
    result = dirstats.refresh(conn)
    elapsed = time.perf_counter() - started

    assert result["remotes"] == 1
    assert result["dirs"] == 1 + 40 + 1000
    assert stats(conn, g, "") == (n, 1040, sum(sizes), sum(dup))
    assert dirstats.get(conn, g, "")["mtime_max"] == max(mtimes)
    top = 7
    idx = [i for i in range(n) if i % 40 == top]
    assert stats(conn, g, f"t{top:02d}") == (
        len(idx), 25, sum(sizes[i] for i in idx), sum(dup[i] for i in idx))
    leaf = [i for i in idx if (i // 40) % 25 == 3]
    assert stats(conn, g, f"t{top:02d}/s03") == (
        len(leaf), 0, sum(sizes[i] for i in leaf), sum(dup[i] for i in leaf))
    assert elapsed < 5, f"refresh took {elapsed:.2f}s"
