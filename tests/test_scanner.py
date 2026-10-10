from __future__ import annotations

import json
import os
import shutil
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
