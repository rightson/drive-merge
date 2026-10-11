from __future__ import annotations

import json
import os
import shutil
import time
import unicodedata

import pytest

from drivemerge import config, db
from drivemerge.config import RemoteConfig
from drivemerge.rclone import Rclone, RcloneError
from drivemerge.scanner import ScanResult, normalize_item, parse_mtime, scan_remote

needs_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")

MD5_EMPTY = "d41d8cd98f00b204e9800998ecf8427e"
MD5_HELLO = "b1946ac92492d2347c6235b4d2611184"  # md5("hello\n")


# --- normalize_item ----------------------------------------------------------------------------

def file_item(path="a/b.txt", **kw) -> dict:
    return {"Path": path, "Name": path.rsplit("/", 1)[-1], "Size": 12, "MimeType": "text/plain",
            "ModTime": "2024-03-05T10:20:30.123456789Z", "IsDir": False, **kw}


def test_normalize_plain_file():
    row = normalize_item(file_item("docs/Report.PDF", MimeType="application/pdf",
                                   Hashes={"md5": "ABCDEF", "sha1": "ff00"}))
    assert row == {
        "entry_key": "p:docs/Report.PDF", "path": "docs/Report.PDF", "parent": "docs",
        "name": "Report.PDF", "name_norm": "report.pdf", "ext": "pdf", "category": "pdf",
        "is_dir": 0, "size": 12, "mtime": "2024-03-05T10:20:30.123456Z", "mime": "application/pdf",
        "provider_id": None, "md5": "abcdef", "sha1": "ff00", "sha256": None, "dropbox": None,
        "quickxor": None, "is_native_doc": 0,
    }


def test_normalize_top_level_parent_is_empty():
    assert normalize_item(file_item("x.txt"))["parent"] == ""


def test_normalize_directory():
    row = normalize_item({"Path": "photos/v1.2", "Name": "v1.2", "Size": 4096, "IsDir": True,
                          "MimeType": "inode/directory", "ModTime": "2024-01-01T00:00:00Z"})
    assert (row["is_dir"], row["size"], row["ext"], row["category"], row["is_native_doc"]) == (1, None, "", "other", 0)
    assert row["parent"] == "photos" and row["name"] == "v1.2"


@pytest.mark.parametrize("name, ext", [
    (".bashrc", ""),
    (".hidden.txt", "txt"),
    ("archive.tar.GZ", "gz"),
    ("noext", ""),
    ("trailing.", ""),
    ("photo.JPEG", "jpeg"),
    ("a.b.c", "c"),
])
def test_normalize_extension(name, ext):
    assert normalize_item(file_item(name))["ext"] == ext


def test_normalize_category_falls_back_to_mime():
    assert normalize_item(file_item("noext", MimeType="image/heic"))["category"] == "photo"
    assert normalize_item(file_item("x.mp3", MimeType="application/octet-stream"))["category"] == "audio"
    assert normalize_item(file_item("blob", MimeType="application/octet-stream"))["category"] == "other"


def test_normalize_drive_native_doc_with_unknown_size():
    row = normalize_item({"Path": "Notes", "Name": "Notes", "Size": -1, "IsDir": False, "ID": "1AbC",
                          "MimeType": "application/vnd.google-apps.document",
                          "ModTime": "2020-02-02T02:02:02.5Z"})
    assert row["size"] is None and row["is_native_doc"] == 1
    assert row["category"] == "document"
    assert row["entry_key"] == "id:1AbC" and row["provider_id"] == "1AbC"
    assert row["md5"] is None


def test_normalize_native_doc_by_mime_even_with_size():
    row = normalize_item(file_item("Sheet", Size=0, MimeType="application/vnd.google-apps.spreadsheet"))
    assert row["size"] == 0 and row["is_native_doc"] == 1


def test_normalize_unknown_size_without_mime_is_still_native():
    row = normalize_item(file_item("odd", Size=-1, MimeType=""))
    assert row["size"] is None and row["is_native_doc"] == 1 and row["mime"] is None


def test_normalize_zero_size_regular_file_is_not_native():
    row = normalize_item(file_item("empty.txt", Size=0))
    assert row["size"] == 0 and row["is_native_doc"] == 0


def test_normalize_nfd_name_is_matched_via_nfc_casefold():
    nfd = unicodedata.normalize("NFD", "Café.TXT")
    assert nfd != "Café.TXT"
    row = normalize_item(file_item(f"dir/{nfd}"))
    assert row["name"] == nfd and row["path"] == f"dir/{nfd}"  # stored verbatim for rclone ops
    assert row["name_norm"] == unicodedata.normalize("NFC", "café.txt")
    assert row["name_norm"] == normalize_item(file_item("dir/Café.TXT"))["name_norm"]


def test_normalize_casefold_handles_sharp_s():
    assert normalize_item(file_item("Straße.txt"))["name_norm"] == "strasse.txt"


def test_normalize_missing_hashes_and_junk():
    assert all(normalize_item(file_item())[h] is None for h in ("md5", "sha1", "sha256", "dropbox", "quickxor"))
    assert normalize_item(file_item(Hashes=None))["md5"] is None
    row = normalize_item(file_item(Hashes={"md5": "", "sha256": "AA", "whirlpool": "zz", "DROPBOX": "Bb", "quickxor": "Cc="}))
    assert (row["md5"], row["sha256"], row["dropbox"], row["quickxor"]) == (None, "aa", "bb", "cc=")
    assert "whirlpool" not in row


def test_normalize_identity_key():
    assert normalize_item(file_item("a/b.txt", ID="X1"))["entry_key"] == "id:X1"
    assert normalize_item(file_item("a/b.txt", ID=""))["entry_key"] == "p:a/b.txt"
    assert normalize_item(file_item("a/b.txt"))["provider_id"] is None


@pytest.mark.parametrize("root, expected_path, expected_parent", [
    ("", "x/y.txt", "x"),
    ("sub", "sub/x/y.txt", "sub/x"),
    ("a/b", "a/b/x/y.txt", "a/b/x"),
    ("/a/b/", "a/b/x/y.txt", "a/b/x"),
])
def test_normalize_root_prefix(root, expected_path, expected_parent):
    row = normalize_item(file_item("x/y.txt"), root)
    assert (row["path"], row["parent"], row["entry_key"]) == (expected_path, expected_parent, f"p:{expected_path}")


def test_normalize_root_prefix_top_level_file():
    row = normalize_item(file_item("y.txt"), "sub")
    assert (row["path"], row["parent"]) == ("sub/y.txt", "sub")


@pytest.mark.parametrize("raw, expected", [
    ("2017-05-31T16:15:57.034468261+01:00", "2017-05-31T15:15:57.034468Z"),
    ("2017-05-31T16:15:57.034468261Z", "2017-05-31T16:15:57.034468Z"),
    ("2017-05-31T16:15:57Z", "2017-05-31T16:15:57.000000Z"),
    ("2017-05-31T16:15:57.5Z", "2017-05-31T16:15:57.500000Z"),
    ("2017-05-31T16:15:57.999999999Z", "2017-05-31T16:15:57.999999Z"),  # truncated, never rounded up
    ("2017-05-31T16:15:57.123-05:30", "2017-05-31T21:45:57.123000Z"),
    ("2017-12-31T23:30:00+00:30", "2017-12-31T23:00:00.000000Z"),
    ("2018-01-01T00:15:00+01:00", "2017-12-31T23:15:00.000000Z"),   # crosses the year boundary
    ("2017-12-31T22:00:00-03:00", "2018-01-01T01:00:00.000000Z"),
    ("2017-05-31T16:15:57+0100", "2017-05-31T15:15:57.000000Z"),
    ("2017-05-31T16:15:57.25z", "2017-05-31T16:15:57.250000Z"),
])
def test_parse_mtime(raw, expected):
    assert parse_mtime(raw) == expected
    assert normalize_item(file_item(ModTime=raw))["mtime"] == expected


@pytest.mark.parametrize("raw", [None, "", "yesterday", "0001-01-01T00:00:00Z", "0001-01-01T00:00:00+05:00", "2017-13-31T00:00:00Z"])
def test_parse_mtime_unknown_is_none(raw):
    assert parse_mtime(raw) is None


def test_normalize_missing_modtime_key():
    item = file_item()
    del item["ModTime"]
    assert normalize_item(item)["mtime"] is None


# --- helpers for integration tests ------------------------------------------------------------

class FakeRunner:
    def __init__(self, out: str = "[\n]\n", rc: int = 0, err: str = ""):
        self.out, self.rc, self.err = out, rc, err
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        return self.rc, self.out, self.err


def listing(*items: dict) -> str:
    return "[\n" + ",\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n]\n"


def gdrive_file(path, id_, size=10, md5="aa", mtime="2024-01-01T00:00:00.000Z", **kw):
    return {"Path": path, "Name": path.rsplit("/", 1)[-1], "Size": size, "ID": id_, "IsDir": False,
            "MimeType": "application/octet-stream", "ModTime": mtime, "Hashes": {"md5": md5}, **kw}


def rows(conn, where="1=1", args=()):
    return {r["path"]: r for r in conn.execute(f"SELECT * FROM entries WHERE {where} ORDER BY path", args)}


def keyed(conn):
    return {r["entry_key"]: r for r in conn.execute("SELECT * FROM entries")}


def max_rv(conn) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key = 'row_version'").fetchone()[0])


@pytest.fixture
def remote(conn):
    r = RemoteConfig(name="gd", provider="drive", read="gd:", hash_types=("md5",))
    config.sync_remotes(conn, config.Config(remotes=[r]))
    return r


# --- scan_remote with a fake runner (Drive-like: IDs, duplicate paths, native docs) ----------

def test_scan_passes_root_hashes_and_extra_args_to_rclone(conn):
    r = RemoteConfig(name="d", provider="drive", read="gd:Backup", extra_args=("--drive-skip-shortcuts",))
    config.sync_remotes(conn, config.Config(remotes=[r]))
    fake = FakeRunner()
    scan_remote(conn, Rclone(runner=fake), r, root="/photos/2020/")
    assert fake.calls == [["rclone", "lsjson", "--recursive", "--hash", "--hash-type", "md5",
                           "--hash-type", "sha1", "--hash-type", "sha256",
                           "--drive-skip-shortcuts", "gd:Backup/photos/2020"]]


def test_scan_result_and_scans_row(conn, remote):
    out = listing(
        {"Path": "d", "Name": "d", "Size": -1, "IsDir": True, "ID": "D1", "MimeType": "application/vnd.google-apps.folder"},
        gdrive_file("d/a.bin", "F1", size=10),
        gdrive_file("d/b.bin", "F2", size=5),
        {"Path": "Doc", "Name": "Doc", "Size": -1, "IsDir": False, "ID": "G1",
         "MimeType": "application/vnd.google-apps.document", "ModTime": "2024-01-01T00:00:00Z"},
    )
    res = scan_remote(conn, Rclone(runner=FakeRunner(out)), remote)
    assert isinstance(res, ScanResult)
    assert (res.remote, res.root, res.status, res.files, res.dirs, res.bytes, res.tombstoned, res.error) == \
        ("gd", "", "complete", 3, 1, 15, 0, None)
    s = conn.execute("SELECT * FROM scans WHERE id = ?", (res.scan_id,)).fetchone()
    assert (s["status"], s["files_seen"], s["dirs_seen"], s["bytes_seen"], s["error"]) == ("complete", 3, 1, 15, None)
    assert s["finished_at"] and s["started_at"] and s["remote_id"] == db.remote_id(conn, "gd")
    e = keyed(conn)
    assert e["id:G1"]["is_native_doc"] == 1 and e["id:G1"]["size"] is None
    assert e["id:D1"]["is_dir"] == 1 and e["id:F1"]["first_seen_scan"] == e["id:F1"]["last_seen_scan"] == res.scan_id


def test_duplicate_paths_are_distinct_rows_keyed_by_id(conn, remote):
    out = listing(gdrive_file("dup.txt", "A", md5="11"), gdrive_file("dup.txt", "B", md5="22"))
    res = scan_remote(conn, Rclone(runner=FakeRunner(out)), remote)
    assert res.files == 2
    e = keyed(conn)
    assert {k: v["md5"] for k, v in e.items()} == {"id:A": "11", "id:B": "22"}
    # deleting one duplicate tombstones exactly that one
    res2 = scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("dup.txt", "B", md5="22")))), remote)
    e = keyed(conn)
    assert res2.tombstoned == 1 and e["id:A"]["present"] == 0 and e["id:B"]["present"] == 1


def test_rename_keeps_row_and_bumps_version_but_keeps_content_group(conn, remote):
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("old.txt", "A")))), remote)
    conn.execute("UPDATE entries SET content_group = 7")
    before = keyed(conn)["id:A"]
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("moved/new.txt", "A")))), remote)
    after = keyed(conn)["id:A"]
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    assert (after["path"], after["parent"], after["name"]) == ("moved/new.txt", "moved", "new.txt")
    assert after["row_version"] > before["row_version"]
    assert after["content_group"] == 7  # same bytes, identity still valid
    assert after["id"] == before["id"] and after["first_seen_scan"] == before["first_seen_scan"]


def test_unchanged_rescan_keeps_row_versions_and_content_group(conn, remote):
    out = listing(gdrive_file("a.txt", "A"), gdrive_file("b/c.txt", "C", md5="cc"))
    scan_remote(conn, Rclone(runner=FakeRunner(out)), remote)
    conn.execute("UPDATE entries SET content_group = 3")
    versions = {k: v["row_version"] for k, v in keyed(conn).items()}
    mark = max_rv(conn)
    res = scan_remote(conn, Rclone(runner=FakeRunner(out)), remote)
    after = keyed(conn)
    assert {k: v["row_version"] for k, v in after.items()} == versions
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE row_version > ?", (mark,)).fetchone()[0] == 0
    assert all(v["last_seen_scan"] == res.scan_id and v["content_group"] == 3 for v in after.values())


def test_size_or_hash_change_clears_content_group_mtime_change_does_not(conn, remote):
    out = listing(gdrive_file("size.bin", "S"), gdrive_file("hash.bin", "H"), gdrive_file("time.bin", "T"),
                  gdrive_file("same.bin", "N"))
    scan_remote(conn, Rclone(runner=FakeRunner(out)), remote)
    conn.execute("UPDATE entries SET content_group = 5")
    before = keyed(conn)
    out2 = listing(gdrive_file("size.bin", "S", size=11), gdrive_file("hash.bin", "H", md5="bb"),
                   gdrive_file("time.bin", "T", mtime="2025-05-05T05:05:05.000Z"), gdrive_file("same.bin", "N"))
    scan_remote(conn, Rclone(runner=FakeRunner(out2)), remote)
    after = keyed(conn)
    for key in ("id:S", "id:H"):
        assert after[key]["content_group"] is None and after[key]["row_version"] > before[key]["row_version"]
    assert after["id:T"]["content_group"] == 5 and after["id:T"]["row_version"] > before["id:T"]["row_version"]
    assert after["id:T"]["mtime"] == "2025-05-05T05:05:05.000000Z"
    assert after["id:N"]["content_group"] == 5 and after["id:N"]["row_version"] == before["id:N"]["row_version"]


def test_hash_appearing_or_disappearing_counts_as_change(conn, remote):
    no_hash = gdrive_file("a", "A")
    del no_hash["Hashes"]
    scan_remote(conn, Rclone(runner=FakeRunner(listing(no_hash))), remote)
    conn.execute("UPDATE entries SET content_group = 1")
    v0 = keyed(conn)["id:A"]["row_version"]
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("a", "A")))), remote)
    e = keyed(conn)["id:A"]
    assert e["md5"] == "aa" and e["row_version"] > v0 and e["content_group"] is None


def test_tombstone_then_revival_bumps_version(conn, remote):
    a, b = gdrive_file("a", "A"), gdrive_file("b", "B")
    scan_remote(conn, Rclone(runner=FakeRunner(listing(a, b))), remote)
    scan_remote(conn, Rclone(runner=FakeRunner(listing(a))), remote)
    gone = keyed(conn)["id:B"]
    assert gone["present"] == 0
    mark = max_rv(conn)
    scan_remote(conn, Rclone(runner=FakeRunner(listing(a, b))), remote)
    back = keyed(conn)["id:B"]
    assert back["present"] == 1 and back["row_version"] > gone["row_version"] and back["row_version"] > mark
    assert keyed(conn)["id:A"]["row_version"] < back["row_version"]


def test_tombstoning_bumps_row_version_and_leaves_content_group(conn, remote):
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("a", "A"), gdrive_file("b", "B")))), remote)
    conn.execute("UPDATE entries SET content_group = 9")
    before = keyed(conn)["id:B"]["row_version"]
    res = scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("a", "A")))), remote)
    gone = keyed(conn)["id:B"]
    assert res.tombstoned == 1 and gone["present"] == 0 and gone["row_version"] > before
    assert gone["content_group"] == 9 and gone["last_seen_scan"] < res.scan_id


def test_each_batch_gets_one_row_version_and_progress_is_called_per_batch(conn, remote):
    items = [gdrive_file(f"f{i}.bin", f"ID{i}", size=i + 1) for i in range(7)]
    calls = []
    res = scan_remote(conn, Rclone(runner=FakeRunner(listing(*items))), remote, batch_size=3,
                      progress=lambda f, d, b: calls.append((f, d, b)))
    versions = [r["row_version"] for r in conn.execute("SELECT row_version FROM entries ORDER BY id")]
    assert versions == [versions[0]] * 3 + [versions[3]] * 3 + [versions[6]]
    assert len(set(versions)) == 3
    assert calls == [(3, 0, 6), (6, 0, 21), (7, 0, 28)]
    assert (res.files, res.bytes) == (7, 28)


def test_empty_listing_is_a_valid_complete_scan(conn, remote):
    calls = []
    res = scan_remote(conn, Rclone(runner=FakeRunner("[\n]\n")), remote, progress=lambda *a: calls.append(a))
    assert (res.status, res.files, res.dirs, res.bytes, res.tombstoned) == ("complete", 0, 0, 0, 0)
    assert calls == []


def test_unknown_remote_raises_before_creating_a_scan(conn):
    ghost = RemoteConfig(name="ghost", provider="drive", read="ghost:")
    with pytest.raises(KeyError):
        scan_remote(conn, Rclone(runner=FakeRunner()), ghost)
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0


# --- failure handling -------------------------------------------------------------------------

def test_failed_scan_marks_failed_and_tombstones_nothing(conn, remote):
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("a", "A"), gdrive_file("b", "B")))), remote)
    before = {k: (v["present"], v["row_version"], v["last_seen_scan"]) for k, v in keyed(conn).items()}
    bad = Rclone(runner=FakeRunner("[\n", rc=3, err="ERROR : directory not found"))
    with pytest.raises(RcloneError):
        scan_remote(conn, bad, remote)
    s = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    assert s["status"] == "failed" and "directory not found" in s["error"] and s["finished_at"]
    assert {k: (v["present"], v["row_version"], v["last_seen_scan"]) for k, v in keyed(conn).items()} == before
    assert db.snapshot(conn) == {str(db.remote_id(conn, "gd")): 1}  # failed scan is not a snapshot


def test_scan_failing_midway_keeps_partial_upserts_but_no_tombstones(conn, remote):
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("a", "A"), gdrive_file("b", "B")))), remote)
    partial = FakeRunner("[\n" + json.dumps(gdrive_file("a", "A", md5="new")) + ",\n", rc=1, err="ERROR : boom")
    with pytest.raises(RcloneError, match="boom"):
        scan_remote(conn, Rclone(runner=partial), remote, batch_size=1)
    e = keyed(conn)
    assert e["id:A"]["md5"] == "new" and e["id:B"]["present"] == 1
    s = conn.execute("SELECT status FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    assert s["status"] == "failed"
    assert not conn.in_transaction


def test_exception_in_progress_callback_fails_the_scan(conn, remote):
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("a", "A")))), remote)

    def boom(*_):
        raise RuntimeError("callback exploded")

    with pytest.raises(RuntimeError, match="callback exploded"):
        scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("z", "Z")))), remote, progress=boom)
    assert keyed(conn)["id:A"]["present"] == 1
    s = conn.execute("SELECT status, error FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    assert s["status"] == "failed" and "callback exploded" in s["error"]


# --- real rclone on local directories ---------------------------------------------------------

@pytest.fixture
def local_remote(conn, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    r = RemoteConfig(name="loc", provider="local", read=str(root), hash_types=("md5",))
    config.sync_remotes(conn, config.Config(remotes=[r]))
    return r, root


def build_tree(root):
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "photos").mkdir()
    (root / "empty_dir").mkdir()
    (root / "a.txt").write_text("hello\n")
    (root / "docs" / "copy-of-a.txt").write_text("hello\n")          # duplicate content elsewhere
    (root / "docs" / "b.txt").write_text("bee\n")
    (root / "docs" / "deep" / "测试文件.md").write_text("中文内容")
    (root / "photos" / "zero.jpg").write_bytes(b"")
    nfd = unicodedata.normalize("NFD", "Café.TXT")
    (root / "photos" / nfd).write_text("nfd")
    return nfd


@needs_rclone
def test_real_scan_builds_expected_rows(conn, local_remote):
    r, root = local_remote
    nfd = build_tree(root)
    if nfd not in os.listdir(root / "photos"):
        pytest.skip("filesystem normalizes unicode names")
    res = scan_remote(conn, Rclone(), r)

    assert (res.status, res.files, res.dirs, res.tombstoned, res.error) == ("complete", 6, 4, 0, None)
    assert res.bytes == 6 + 6 + 4 + len("中文内容".encode()) + 0 + 3
    e = rows(conn)
    assert set(e) == {"a.txt", "docs", "docs/b.txt", "docs/copy-of-a.txt", "docs/deep", "docs/deep/测试文件.md",
                      "empty_dir", "photos", f"photos/{nfd}", "photos/zero.jpg"}

    a, dup = e["a.txt"], e["docs/copy-of-a.txt"]
    assert a["md5"] == dup["md5"] == MD5_HELLO and a["size"] == 6
    assert (a["parent"], a["ext"], a["category"], a["is_dir"], a["present"]) == ("", "txt", "document", 0, 1)
    assert a["entry_key"] == "p:a.txt" and a["provider_id"] is None and a["content_group"] is None
    assert a["mtime"].endswith("Z") and len(a["mtime"]) == len("2024-03-05T10:20:30.123456Z")

    zero = e["photos/zero.jpg"]
    assert (zero["size"], zero["md5"], zero["category"], zero["ext"]) == (0, MD5_EMPTY, "photo", "jpg")

    zh = e["docs/deep/测试文件.md"]
    assert (zh["name"], zh["parent"], zh["ext"], zh["is_native_doc"]) == ("测试文件.md", "docs/deep", "md", 0)

    n = e[f"photos/{nfd}"]
    assert n["name"] == nfd and n["name_norm"] == unicodedata.normalize("NFC", "Café.TXT").casefold()

    d = e["docs"]
    assert (d["is_dir"], d["size"], d["md5"], d["category"], d["ext"]) == (1, None, None, "other", "")
    assert e["empty_dir"]["is_dir"] == 1 and e["empty_dir"]["parent"] == ""

    s = conn.execute("SELECT * FROM scans").fetchone()
    assert (s["status"], s["files_seen"], s["dirs_seen"], s["bytes_seen"], s["root"]) == ("complete", 6, 4, res.bytes, "")
    assert all(row["first_seen_scan"] == row["last_seen_scan"] == res.scan_id for row in e.values())


@needs_rclone
def test_real_rescan_unchanged_yields_empty_change_feed(conn, local_remote):
    r, root = local_remote
    build_tree(root)
    scan_remote(conn, Rclone(), r)
    versions = {k: v["row_version"] for k, v in keyed(conn).items()}
    mark = max_rv(conn)
    res = scan_remote(conn, Rclone(), r)
    after = keyed(conn)
    assert {k: v["row_version"] for k, v in after.items()} == versions
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE row_version > ?", (mark,)).fetchone()[0] == 0
    assert res.tombstoned == 0 and all(v["last_seen_scan"] == res.scan_id for v in after.values())


@needs_rclone
def test_real_rescan_after_delete_modify_and_add(conn, local_remote):
    r, root = local_remote
    build_tree(root)
    scan_remote(conn, Rclone(), r)
    conn.execute("UPDATE entries SET content_group = 42")
    before = rows(conn)
    mark = max_rv(conn)

    (root / "docs" / "b.txt").unlink()                                   # deleted
    target = root / "a.txt"                                              # modified, same size AND same mtime
    st = target.stat()
    target.write_text("jello\n")
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    (root / "docs" / "deep" / "新文件.md").write_text("new")             # added

    res = scan_remote(conn, Rclone(), r)
    after = rows(conn)

    assert res.tombstoned == 1
    assert after["docs/b.txt"]["present"] == 0 and after["docs/b.txt"]["row_version"] > mark
    assert after["a.txt"]["md5"] != MD5_HELLO and after["a.txt"]["size"] == 6
    assert after["a.txt"]["mtime"] == before["a.txt"]["mtime"]
    assert after["a.txt"]["row_version"] > mark and after["a.txt"]["content_group"] is None
    assert after["docs/deep/新文件.md"]["present"] == 1 and after["docs/deep/新文件.md"]["row_version"] > mark

    untouched = ["docs/copy-of-a.txt", "docs/deep/测试文件.md", "photos/zero.jpg", "empty_dir", "photos"]
    for p in untouched:
        assert after[p]["row_version"] == before[p]["row_version"], p
        assert after[p]["content_group"] == 42, p
        assert after[p]["present"] == 1 and after[p]["last_seen_scan"] == res.scan_id, p
    assert after["docs/copy-of-a.txt"]["md5"] == MD5_HELLO  # the duplicate is unaffected

    # the deleted file reappears -> revived, not duplicated
    (root / "docs" / "b.txt").write_text("bee\n")
    count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    mark2 = max_rv(conn)
    scan_remote(conn, Rclone(), r)
    back = rows(conn)["docs/b.txt"]
    assert back["present"] == 1 and back["row_version"] > mark2 and back["md5"] == before["docs/b.txt"]["md5"]
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == count


@needs_rclone
def test_real_size_change_clears_content_group(conn, local_remote):
    r, root = local_remote
    build_tree(root)
    scan_remote(conn, Rclone(), r)
    conn.execute("UPDATE entries SET content_group = 1")
    (root / "docs" / "b.txt").write_text("much longer content\n")
    scan_remote(conn, Rclone(), r)
    e = rows(conn)
    assert e["docs/b.txt"]["size"] == 20 and e["docs/b.txt"]["content_group"] is None
    assert e["a.txt"]["content_group"] == 1


@needs_rclone
def test_real_mtime_only_change_bumps_version_but_keeps_content_group(conn, local_remote):
    r, root = local_remote
    build_tree(root)
    scan_remote(conn, Rclone(), r)
    conn.execute("UPDATE entries SET content_group = 1")
    before = rows(conn)["a.txt"]
    os.utime(root / "a.txt", ns=(1_500_000_000_123_456_789, 1_500_000_000_123_456_789))
    scan_remote(conn, Rclone(), r)
    after = rows(conn)["a.txt"]
    assert after["mtime"] == "2017-07-14T02:40:00.123456Z"
    assert after["row_version"] > before["row_version"] and after["content_group"] == 1


@needs_rclone
def test_real_subroot_scan_only_tombstones_inside_subroot(conn, local_remote):
    r, root = local_remote
    build_tree(root)
    scan_remote(conn, Rclone(), r)
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    before = rows(conn)
    (root / "docs" / "b.txt").unlink()
    (root / "photos" / "zero.jpg").unlink()      # outside the sub-root: must stay present
    (root / "a.txt").unlink()                    # outside as well

    res = scan_remote(conn, Rclone(), r, root="docs/")
    after = rows(conn)

    assert res.root == "docs" and res.tombstoned == 1
    assert (res.files, res.dirs) == (2, 1)       # copy-of-a.txt + 测试文件.md, and deep/
    assert after["docs/b.txt"]["present"] == 0
    for p in ("photos/zero.jpg", "a.txt", "docs", "photos", "empty_dir"):
        assert after[p]["present"] == 1 and after[p]["row_version"] == before[p]["row_version"], p
        assert after[p]["last_seen_scan"] == before[p]["last_seen_scan"], p
    assert after["docs/copy-of-a.txt"]["last_seen_scan"] == res.scan_id
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == total  # same keys, no duplicates
    assert conn.execute("SELECT root FROM scans WHERE id = ?", (res.scan_id,)).fetchone()[0] == "docs"

    # a later full scan then tombstones the rest
    res2 = scan_remote(conn, Rclone(), r)
    assert res2.tombstoned == 2
    assert rows(conn)["a.txt"]["present"] == 0 and rows(conn)["photos/zero.jpg"]["present"] == 0


@needs_rclone
def test_real_subroot_boundary_with_wildcards_and_similar_names(conn, local_remote):
    r, root = local_remote
    names = ["a", "ab", "a.b", "a-b", "a0", "a_b", "a%", "aXb"]
    for n in names:
        (root / n).mkdir()
        (root / n / "f.txt").write_text(n)
    scan_remote(conn, Rclone(), r)
    for n in names:
        (root / n / "f.txt").unlink()
    res = scan_remote(conn, Rclone(), r, root="a")
    assert res.tombstoned == 1
    gone = {p for p, row in rows(conn).items() if row["present"] == 0}
    assert gone == {"a/f.txt"}
    scan_remote(conn, Rclone(), r, root="a_b")
    assert {p for p, row in rows(conn).items() if row["present"] == 0} == {"a/f.txt", "a_b/f.txt"}
    scan_remote(conn, Rclone(), r, root="a%")
    assert {p for p, row in rows(conn).items() if row["present"] == 0} == {"a/f.txt", "a_b/f.txt", "a%/f.txt"}


@needs_rclone
def test_real_missing_root_fails_and_tombstones_nothing(conn, local_remote):
    r, root = local_remote
    build_tree(root)
    scan_remote(conn, Rclone(), r)
    before = {k: (v["present"], v["row_version"]) for k, v in keyed(conn).items()}
    with pytest.raises(RcloneError) as ei:
        scan_remote(conn, Rclone(), r, root="no/such/dir")
    assert ei.value.returncode == 3
    s = conn.execute("SELECT status, error, root FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    assert s["status"] == "failed" and s["root"] == "no/such/dir" and s["error"]
    assert {k: (v["present"], v["row_version"]) for k, v in keyed(conn).items()} == before


@needs_rclone
def test_real_vanished_remote_directory_fails_instead_of_tombstoning_everything(conn, local_remote, tmp_path):
    r, root = local_remote
    build_tree(root)
    scan_remote(conn, Rclone(), r)
    shutil.rmtree(root)  # e.g. an unmounted disk
    with pytest.raises(RcloneError):
        scan_remote(conn, Rclone(), r)
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE present = 0").fetchone()[0] == 0


@needs_rclone
def test_real_search_index_stays_consistent_across_rescans(conn, local_remote):
    r, root = local_remote
    build_tree(root)
    scan_remote(conn, Rclone(), r)

    def hits(q):
        return [row[0] for row in conn.execute(
            "SELECT e.path FROM entries_fts f JOIN entries e ON e.id = f.rowid WHERE entries_fts MATCH ?", (f'"{q}"',))]

    assert hits("测试文") == ["docs/deep/测试文件.md"]
    scan_remote(conn, Rclone(), r)  # unchanged: still exactly one hit
    assert hits("测试文") == ["docs/deep/测试文件.md"]
    (root / "docs" / "deep" / "测试文件.md").rename(root / "docs" / "deep" / "重命名.md")
    scan_remote(conn, Rclone(), r)
    assert hits("重命名") == ["docs/deep/重命名.md"]
    assert [p for p in hits("测试文") if rows(conn)[p]["present"] == 1] == []


@needs_rclone
def test_real_batching_with_small_batches_matches_single_batch(conn, local_remote):
    r, root = local_remote
    build_tree(root)
    res = scan_remote(conn, Rclone(), r, batch_size=2)
    assert (res.files, res.dirs) == (6, 4)
    versions = {row["row_version"] for row in conn.execute("SELECT row_version FROM entries")}
    assert len(versions) == 5  # ceil(10 / 2) batches, one version each


# --- tombstone guard and interrupted scans (added by the lead) ----------------

def _fake_rclone(items):
    import json as _json

    def runner(args):
        return 0, "[\n" + ",\n".join(_json.dumps(i) for i in items) + "\n]\n", ""
    from drivemerge.rclone import Rclone
    return Rclone(runner=runner)


def _items(n):
    return [{"Path": f"f{i}.txt", "Name": f"f{i}.txt", "Size": 1, "ModTime": "2024-01-01T00:00:00Z",
             "IsDir": False} for i in range(n)]


def test_mass_disappearance_is_refused_without_force(conn):
    from drivemerge.config import Config, RemoteConfig, sync_remotes
    from drivemerge.scanner import SuspiciousScan, scan_remote
    rc = RemoteConfig(name="x", provider="other", read="x:")
    sync_remotes(conn, Config(remotes=[rc]))
    scan_remote(conn, _fake_rclone(_items(100)), rc)
    with pytest.raises(SuspiciousScan):
        scan_remote(conn, _fake_rclone(_items(10)), rc)
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE present = 1").fetchone()[0] == 100
    assert conn.execute("SELECT status FROM scans ORDER BY id DESC LIMIT 1").fetchone()[0] == "failed"
    res = scan_remote(conn, _fake_rclone(_items(10)), rc, force=True)
    assert res.tombstoned == 90


def test_small_trees_and_modest_deletions_are_not_blocked(conn):
    from drivemerge.config import Config, RemoteConfig, sync_remotes
    from drivemerge.scanner import scan_remote
    rc = RemoteConfig(name="x", provider="other", read="x:")
    sync_remotes(conn, Config(remotes=[rc]))
    scan_remote(conn, _fake_rclone(_items(100)), rc)
    assert scan_remote(conn, _fake_rclone(_items(60)), rc).tombstoned == 40
    scan_remote(conn, _fake_rclone(_items(0)), RemoteConfig(name="x", provider="other", read="x:"), force=True)
    sync_remotes(conn, Config(remotes=[RemoteConfig(name="y", provider="other", read="y:")]))
    ry = RemoteConfig(name="y", provider="other", read="y:")
    scan_remote(conn, _fake_rclone(_items(20)), ry)
    assert scan_remote(conn, _fake_rclone(_items(15)), ry).tombstoned == 5    # a share of a small tree: fine
    scan_remote(conn, _fake_rclone(_items(3)), ry)
    assert scan_remote(conn, _fake_rclone([]), ry).tombstoned == 3            # a handful of files really can all go


def test_stale_running_scan_is_marked_failed(conn):
    from drivemerge import db as _db
    from drivemerge.config import Config, RemoteConfig, sync_remotes
    from drivemerge.scanner import scan_remote
    rc = RemoteConfig(name="x", provider="other", read="x:")
    sync_remotes(conn, Config(remotes=[rc]))
    conn.execute("INSERT INTO scans(remote_id, status, started_at) VALUES (?, 'running', ?)",
                 (_db.remote_id(conn, "x"), _db.utcnow()))
    scan_remote(conn, _fake_rclone(_items(1)), rc)
    statuses = [r[0] for r in conn.execute("SELECT status FROM scans ORDER BY id")]
    assert statuses == ["failed", "complete"]


# --- review fixes: keys shared inside one listing -----------------------------------------------

def plain_item(path, size=10, **kw) -> dict:
    """A backend without provider IDs (rclone local): rows are keyed by path."""
    return {"Path": path, "Name": path.rsplit("/", 1)[-1], "Size": size, "IsDir": False,
            "ModTime": "2024-01-01T00:00:00Z", **kw}


@pytest.fixture
def plain(conn):
    r = RemoteConfig(name="x", provider="other", read="x:")
    config.sync_remotes(conn, config.Config(remotes=[r]))
    return r


def test_items_sharing_a_key_each_get_a_row_and_rescans_do_not_churn(conn, plain):
    # rclone lists `a\xff.txt` and `a\xfe.txt` (and more) under the one spelling `a<U+FFFD>.txt`
    name = "a�.txt"
    out = listing(plain_item(name, 5), plain_item(name, 7), plain_item(name, 9), plain_item("ok.txt", 1))
    res = scan_remote(conn, Rclone(runner=FakeRunner(out)), plain, batch_size=2)      # the batch cuts between them
    assert (res.files, res.ambiguous) == (4, 2)
    e = keyed(conn)
    assert sorted(e) == [f"p:{name}", f"p:{name}#2", f"p:{name}#3", "p:ok.txt"]
    assert [e[k]["size"] for k in (f"p:{name}", f"p:{name}#2", f"p:{name}#3")] == [5, 7, 9]
    assert len({e[k]["path"] for k in e if k != "p:ok.txt"}) == 1                    # same path: ambiguous, not hidden
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE is_dir = 0 AND present = 1").fetchone()[0] == res.files

    conn.execute("UPDATE entries SET content_group = 3")
    versions = {k: v["row_version"] for k, v in e.items()}
    mark = max_rv(conn)
    again = scan_remote(conn, Rclone(runner=FakeRunner(out)), plain, batch_size=2)
    after = keyed(conn)
    assert again.ambiguous == 2 and again.tombstoned == 0
    assert {k: v["row_version"] for k, v in after.items()} == versions
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE row_version > ?", (mark,)).fetchone()[0] == 0
    assert all(v["content_group"] == 3 and v["last_seen_scan"] == again.scan_id for v in after.values())

    gone = scan_remote(conn, Rclone(runner=FakeRunner(listing(plain_item(name, 5), plain_item(name, 7),
                                                               plain_item("ok.txt", 1)))), plain)
    assert gone.tombstoned == 1 and keyed(conn)[f"p:{name}#3"]["present"] == 0
    assert keyed(conn)[f"p:{name}#2"]["present"] == 1


def test_a_repeated_provider_id_is_kept_as_a_second_row(conn, remote):
    out = listing(gdrive_file("s.txt", "A", md5="11"), gdrive_file("s.txt", "A", md5="22"))
    res = scan_remote(conn, Rclone(runner=FakeRunner(out)), remote)
    assert (res.files, res.ambiguous) == (2, 1)
    assert {k: v["md5"] for k, v in keyed(conn).items()} == {"id:A": "11", "id:A#2": "22"}


@needs_rclone
def test_real_names_that_differ_only_in_invalid_utf8_are_not_collapsed(conn, local_remote):
    r, root = local_remote
    for name, body in ((b"a\xff.txt", b"first"), (b"a\xfe.txt", b"second!"), (b"ok.txt", b"fine")):
        try:
            with open(os.path.join(os.fsencode(str(root)), name), "wb") as f:
                f.write(body)
        except OSError:
            pytest.skip("filesystem refuses non-UTF-8 names")
    res = scan_remote(conn, Rclone(), r)
    assert (res.files, res.ambiguous) == (3, 1)
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE is_dir = 0 AND present = 1").fetchone()[0] == 3
    mark = max_rv(conn)
    scan_remote(conn, Rclone(), r)
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE row_version > ?", (mark,)).fetchone()[0] == 0


# --- review fixes: sub-root scans store the folders above the root ------------------------------

def dir_item(path, id_=None) -> dict:
    return {"Path": path, "Name": path.rsplit("/", 1)[-1], "Size": -1, "IsDir": True,
            "ModTime": "2024-01-01T00:00:00Z", **({"ID": id_} if id_ else {})}


def test_subroot_scan_stores_the_root_and_its_parents(conn, remote):
    out = listing(gdrive_file("x.jpg", "F1"))
    res = scan_remote(conn, Rclone(runner=FakeRunner(out)), remote, root="Photos/2020")
    e = rows(conn)
    assert sorted(e) == ["Photos", "Photos/2020", "Photos/2020/x.jpg"]
    assert (e["Photos"]["is_dir"], e["Photos"]["parent"], e["Photos"]["entry_key"]) == (1, "", "p:Photos")
    assert (e["Photos/2020"]["parent"], e["Photos/2020"]["name"]) == ("Photos", "2020")
    assert all(e[p]["last_seen_scan"] == res.scan_id for p in e)
    assert (res.files, res.dirs) == (1, 0)                       # the summary still counts what rclone listed

    versions = {k: v["row_version"] for k, v in keyed(conn).items()}
    scan_remote(conn, Rclone(runner=FakeRunner(out)), remote, root="Photos/2020")
    assert {k: v["row_version"] for k, v in keyed(conn).items()} == versions    # an unchanged rescan adds nothing


def test_subroot_scan_of_an_empty_folder_still_stores_its_parents(conn, remote):
    scan_remote(conn, Rclone(runner=FakeRunner("[\n]\n")), remote, root="a/b")
    assert sorted(rows(conn)) == ["a", "a/b"]


def test_subroot_scan_does_not_duplicate_folders_the_catalog_has(conn, remote):
    full = listing(dir_item("Photos", "D1"), dir_item("Photos/2020", "D2"), gdrive_file("Photos/2020/x.jpg", "F1"))
    scan_remote(conn, Rclone(runner=FakeRunner(full)), remote)
    before = keyed(conn)
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("x.jpg", "F1")))), remote, root="Photos/2020")
    after = keyed(conn)
    assert sorted(after) == ["id:D1", "id:D2", "id:F1"]
    assert {k: v["row_version"] for k, v in after.items()} == {k: v["row_version"] for k, v in before.items()}


def test_a_full_scan_replaces_the_stored_parents_with_the_listed_ones(conn, remote):
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("x.jpg", "F1")))), remote, root="Photos/2020")
    full = listing(dir_item("Photos", "D1"), dir_item("Photos/2020", "D2"), gdrive_file("Photos/2020/x.jpg", "F1"))
    res = scan_remote(conn, Rclone(runner=FakeRunner(full)), remote)
    assert res.tombstoned == 2
    live = [(r["path"], r["entry_key"]) for r in conn.execute("SELECT * FROM entries WHERE present = 1 ORDER BY path")]
    assert live == [("Photos", "id:D1"), ("Photos/2020", "id:D2"), ("Photos/2020/x.jpg", "id:F1")]


def test_a_failed_subroot_scan_stores_no_parents(conn, remote):
    bad = Rclone(runner=FakeRunner("[\n]\n", rc=3, err="ERROR : directory not found"))
    with pytest.raises(RcloneError):
        scan_remote(conn, bad, remote, root="Photos/2020")
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


@needs_rclone
def test_real_subroot_only_catalog_can_be_browsed_from_the_root(conn, local_remote):
    from drivemerge import dirstats, workspace
    r, root = local_remote
    (root / "Photos" / "2020").mkdir(parents=True)
    (root / "Photos" / "2020" / "x.jpg").write_text("x")
    scan_remote(conn, Rclone(), r, root="Photos/2020")
    dirstats.refresh(conn)
    assert [i["name"] for i in workspace.list_dir(conn, "loc", "")["items"]] == ["Photos"]
    sub = workspace.list_dir(conn, "loc", "Photos/2020")["items"]
    assert [i["name"] for i in sub] == ["x.jpg"]
    photos = workspace.list_dir(conn, "loc", "")["items"][0]
    assert (photos["files"], photos["dirs"]) == (1, 1)


# --- review fixes: the root typed in another case on a case-insensitive remote -----------------

@pytest.fixture
def dropbox(conn):
    r = RemoteConfig(name="db", provider="dropbox", read="db:")
    config.sync_remotes(conn, config.Config(remotes=[r]))
    return r


def dropbox_file(path, id_):
    return {"Path": path, "Name": path.rsplit("/", 1)[-1], "Size": 10, "ID": id_, "IsDir": False,
            "ModTime": "2024-01-01T00:00:00Z", "Hashes": {"dropbox": "ab" * 32}}


def test_root_typed_in_another_case_keeps_the_catalogs_spelling(conn, dropbox):
    from drivemerge import workspace
    full = listing(dir_item("Photos", "id:d1"), dir_item("Photos/2020", "id:d2"), dropbox_file("Photos/2020/a.jpg", "id:f1"))
    scan_remote(conn, Rclone(runner=FakeRunner(full)), dropbox)
    before = keyed(conn)
    fake = FakeRunner(listing(dropbox_file("a.jpg", "id:f1")))          # what Dropbox lists for `db:photos/2020`
    res = scan_remote(conn, Rclone(runner=fake), dropbox, root="photos/2020")
    assert res.root == "Photos/2020" and fake.calls[0][-1] == "db:Photos/2020"
    assert conn.execute("SELECT root FROM scans WHERE id = ?", (res.scan_id,)).fetchone()[0] == "Photos/2020"
    after = keyed(conn)
    assert {k: v["path"] for k, v in after.items()} == {k: v["path"] for k, v in before.items()}
    assert {k: v["row_version"] for k, v in after.items()} == {k: v["row_version"] for k, v in before.items()}
    assert res.tombstoned == 0 and sorted(rows(conn)) == ["Photos", "Photos/2020", "Photos/2020/a.jpg"]
    assert [i["name"] for i in workspace.list_dir(conn, "db", "Photos/2020", view="before")["items"]] == ["a.jpg"]


def test_a_missing_file_below_a_retyped_root_is_still_tombstoned(conn, dropbox):
    full = listing(dir_item("Photos", "id:d1"), dropbox_file("Photos/a.jpg", "id:f1"), dropbox_file("Photos/b.jpg", "id:f2"))
    scan_remote(conn, Rclone(runner=FakeRunner(full)), dropbox)
    res = scan_remote(conn, Rclone(runner=FakeRunner(listing(dropbox_file("a.jpg", "id:f1")))), dropbox, root="PHOTOS")
    assert res.tombstoned == 1 and rows(conn)["Photos/b.jpg"]["present"] == 0


def test_root_spelling_is_kept_where_the_catalog_knows_no_folder(conn, dropbox):
    scan_remote(conn, Rclone(runner=FakeRunner(listing(dir_item("Photos", "id:d1")))), dropbox)
    scan_remote(conn, Rclone(runner=FakeRunner(listing(dropbox_file("x.jpg", "id:f9")))), dropbox, root="photos/NewDir")
    assert sorted(rows(conn)) == ["Photos", "Photos/NewDir", "Photos/NewDir/x.jpg"]


def test_a_case_sensitive_remote_takes_the_root_literally(conn, remote):
    scan_remote(conn, Rclone(runner=FakeRunner(listing(dir_item("Photos", "D1")))), remote)
    scan_remote(conn, Rclone(runner=FakeRunner(listing(gdrive_file("x.jpg", "F1")))), remote, root="photos")
    assert sorted(rows(conn)) == ["Photos", "photos", "photos/x.jpg"]


# --- review fixes: an emptied small remote, overlapping scans, partial scans --------------------

@needs_rclone
def test_real_emptied_small_remote_is_refused_without_force(conn, local_remote):
    r, root = local_remote
    for i in range(30):
        (root / f"photo{i}.jpg").write_bytes(b"x" * (i + 1))
    scan_remote(conn, Rclone(), r)
    for f in root.iterdir():                                  # the drive is unmounted: an empty mount point
        f.unlink()
    from drivemerge.scanner import SuspiciousScan
    with pytest.raises(SuspiciousScan, match="30 of 30"):
        scan_remote(conn, Rclone(), r)
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE present = 0").fetchone()[0] == 0
    assert scan_remote(conn, Rclone(), r, force=True).tombstoned == 30


def test_a_listing_missing_part_of_a_small_tree_is_not_refused(conn, plain):
    scan_remote(conn, _fake_rclone(_items(10)), plain)
    assert scan_remote(conn, _fake_rclone(_items(1)), plain).tombstoned == 9


class Racing(Rclone):
    """Lists `items`; after the first one, runs `during()` (a newer scan of the same remote)."""

    def __init__(self, items, during):
        super().__init__()
        self.items, self.during = items, during

    def lsjson(self, remote, path="", **kw):
        for i, it in enumerate(self.items):
            yield dict(it)
            if i == 0:
                self.during()


def test_a_scan_that_a_newer_one_replaced_stops_before_it_can_tombstone(conn, plain):
    from drivemerge.scanner import ScanSuperseded
    items = [plain_item(f"f{i}.txt", i + 1) for i in range(3)]
    scan_remote(conn, Rclone(runner=FakeRunner(listing(*items))), plain)            # scan 1: the catalog knows f0..f2
    newer = []
    old = Racing(items, lambda: newer.append(scan_remote(conn, Rclone(runner=FakeRunner(listing(*items))), plain)))
    with pytest.raises(ScanSuperseded):
        scan_remote(conn, old, plain, batch_size=2)                                 # scan 2, replaced by scan 3
    assert newer and newer[0].scan_id == 3 and newer[0].tombstoned == 0
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE present = 1").fetchone()[0] == 3
    assert [r[0] for r in conn.execute("SELECT status FROM scans ORDER BY id")] == ["complete", "failed", "complete"]
    assert all(r["last_seen_scan"] == 3 for r in conn.execute("SELECT * FROM entries"))   # nothing moved back to scan 2
    assert db.snapshot(conn) == {str(db.remote_id(conn, "x")): 3}


def test_overlapping_scans_in_two_connections_do_not_tombstone_live_files(tmp_path):
    import threading
    from drivemerge.scanner import ScanSuperseded

    path = tmp_path / "c.db"
    r = RemoteConfig(name="x", provider="other", read="x:")
    c0 = db.connect(path)
    config.sync_remotes(c0, config.Config(remotes=[r]))
    items = [plain_item(f"f{i}.txt", i + 1) for i in range(3)]
    scan_remote(c0, Rclone(runner=FakeRunner(listing(*items))), r)

    class Gated(Rclone):
        def __init__(self, wait=None):
            super().__init__()
            self.wait = wait

        def lsjson(self, remote, path="", **kw):
            if self.wait is not None:
                assert self.wait.wait(10)
            yield from (dict(i) for i in items)

    b_flushed, a_done = threading.Event(), threading.Event()
    outcome: dict[str, BaseException | None] = {}

    def run(name, rclone, **kw):
        try:
            scan_remote(db.connect(path), rclone, r, **kw)
            outcome[name] = None
        except BaseException as e:
            outcome[name] = e

    def b_progress(*_):                                      # scan 3 pauses right after writing every row
        b_flushed.set()
        assert a_done.wait(10)

    ta = threading.Thread(target=run, args=("old", Gated(b_flushed)))
    ta.start()
    deadline = time.monotonic() + 10
    while not c0.execute("SELECT 1 FROM scans WHERE id = 2").fetchone():
        assert time.monotonic() < deadline, "the first scan never started"
        time.sleep(0.001)
    tb = threading.Thread(target=run, args=("new", Gated()), kwargs={"progress": b_progress})
    tb.start()
    ta.join(15)
    a_done.set()
    tb.join(15)
    assert isinstance(outcome["old"], ScanSuperseded) and outcome["new"] is None
    assert c0.execute("SELECT COUNT(*) FROM entries WHERE present = 0").fetchone()[0] == 0


def test_a_failed_scan_leaves_dir_stats_matching_the_rows_it_committed(conn, plain):
    from drivemerge import dirstats
    rid = db.remote_id(conn, "x")
    scan_remote(conn, Rclone(runner=FakeRunner(listing(dir_item("Docs"), plain_item("Docs/a.txt", 4)))), plain)
    dirstats.refresh(conn)
    assert dirstats.get(conn, rid, "Docs")["files"] == 1

    items = [plain_item(f"Docs/n{i}.txt", i + 1) for i in range(3)]
    partial = FakeRunner(listing(*items)[:-3] + ",\n", rc=1, err="ERROR : boom")      # dies after the listing began
    with pytest.raises(RcloneError):
        scan_remote(conn, Rclone(runner=partial), plain, batch_size=2)
    live = conn.execute("SELECT COUNT(*), SUM(size) FROM entries WHERE present = 1 AND is_dir = 0").fetchone()
    docs = dirstats.get(conn, rid, "Docs")
    assert live[0] > 1 and (docs["files"], docs["bytes"]) == tuple(live)


def test_suspicious_scan_also_leaves_dir_stats_matching(conn, plain):
    from drivemerge import dirstats
    from drivemerge.scanner import SuspiciousScan
    rid = db.remote_id(conn, "x")
    scan_remote(conn, _fake_rclone(_items(100)), plain)
    dirstats.refresh(conn)
    changed = [dict(i, Size=2) for i in _items(10)]                        # rewritten rows are committed before the guard
    with pytest.raises(SuspiciousScan):
        scan_remote(conn, _fake_rclone(changed), plain)
    assert dirstats.get(conn, rid, "")["bytes"] == conn.execute("SELECT SUM(size) FROM entries").fetchone()[0] == 110


def test_a_scan_that_wrote_nothing_does_not_touch_dir_stats(conn, plain, monkeypatch):
    from drivemerge import dirstats
    calls = []
    monkeypatch.setattr(dirstats, "refresh", lambda *a, **k: calls.append(1))
    with pytest.raises(RcloneError):
        scan_remote(conn, Rclone(runner=FakeRunner("[\n", rc=3, err="ERROR : directory not found")), plain)
    assert calls == []
