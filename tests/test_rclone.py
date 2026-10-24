from __future__ import annotations

import json
import pickle
import shutil
import stat as statmod
import sys
import threading
import time

import pytest

from drivemerge.rclone import Rclone, RcloneError

needs_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")


class FakeRunner:
    """Records argv and replays canned (rc, stdout, stderr)."""

    def __init__(self, rc: int = 0, out: str = "", err: str = ""):
        self.rc, self.out, self.err = rc, out, err
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        return self.rc, self.out, self.err


def item(path: str, **kw) -> dict:
    return {"Path": path, "Name": path.rsplit("/", 1)[-1], "Size": 1, "IsDir": False, **kw}


def listing(*items: dict, trailing_comma: bool = False) -> str:
    lines = [json.dumps(i, ensure_ascii=False) for i in items]
    body = ",\n".join(lines) + ("," if trailing_comma and lines else "")
    return "[\n" + body + ("\n" if body else "") + "]\n"


# --- join ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("remote, path, expected", [
    ("gd:", "a/b", "gd:a/b"),
    ("gd:root", "a", "gd:root/a"),
    ("gd:root/", "a", "gd:root/a"),
    ("/tmp/x", "a", "/tmp/x/a"),
    ("/tmp/x/", "a", "/tmp/x/a"),
    (":memory:", "a/b", ":memory:a/b"),
    ("gd:", "", "gd:"),
    ("gd:root", "", "gd:root"),
    ("/tmp/x", "", "/tmp/x"),
    ("gd:", "/a/b/", "gd:a/b"),
])
def test_join(remote, path, expected):
    assert Rclone.join(remote, path) == expected


# --- argv construction -------------------------------------------------------------------------

def test_lsjson_default_argv():
    r = FakeRunner(out=listing())
    assert list(Rclone(runner=r).lsjson("gd:", "a/b")) == []
    assert r.calls == [["rclone", "lsjson", "--recursive", "gd:a/b"]]


def test_lsjson_non_recursive_has_no_recursive_flag():
    r = FakeRunner(out=listing())
    list(Rclone(runner=r).lsjson("gd:", recursive=False))
    assert r.calls[0] == ["rclone", "lsjson", "gd:"]


def test_global_flags_come_first_then_hash_then_call_extras():
    r = FakeRunner(out=listing())
    rc = Rclone("/opt/rclone", config="/etc/rc.conf", extra_args=["--fast-list", "--tpslimit", "5"], runner=r)
    list(rc.lsjson("gd:", "x", hash_types=("md5", "sha1"), extra_args=("--drive-skip-shortcuts",)))
    assert r.calls[0] == [
        "/opt/rclone", "--config", "/etc/rc.conf", "--fast-list", "--tpslimit", "5",
        "lsjson", "--recursive", "--hash", "--hash-type", "md5", "--hash-type", "sha1",
        "--drive-skip-shortcuts", "gd:x",
    ]


def test_no_hash_flags_without_hash_types():
    r = FakeRunner(out=listing())
    list(Rclone(runner=r).lsjson("gd:"))
    assert "--hash" not in r.calls[0] and "--hash-type" not in r.calls[0]


def test_operand_starting_with_dash_is_separated():
    r = FakeRunner(out=listing())
    list(Rclone(runner=r).lsjson("-weird"))
    assert r.calls[0][-2:] == ["--", "-weird"]


def test_copyto_and_moveto_never_overwrite():
    r = FakeRunner()
    rc = Rclone(config="c.conf", runner=r)
    rc.copyto("a:", "x/f.txt", "b:", "y/f.txt")
    rc.moveto("a:", "x/f.txt", "b:root", "y/f.txt")
    assert r.calls[0] == ["rclone", "--config", "c.conf", "copyto", "--ignore-existing", "a:x/f.txt", "b:y/f.txt"]
    assert r.calls[1] == ["rclone", "--config", "c.conf", "moveto", "--ignore-existing", "a:x/f.txt", "b:root/y/f.txt"]


def test_no_destructive_subcommands_or_flags_are_ever_built(tmp_path):
    r = FakeRunner(out='{"total": 1, "used": 1, "free": 0}')
    rc = Rclone(runner=r)
    list(rc.lsjson("a:", hash_types=("md5",)))
    rc.stat("a:", "f")
    rc.copyto("a:", "f", "b:", "f")
    rc.moveto("a:", "f", "b:", "f")
    rc.download("a:", "f", str(tmp_path / "f"))
    rc.about("a:")
    assert len(r.calls) == 6
    for argv in r.calls:
        assert argv[1] not in {"sync", "delete", "deletefile", "purge", "bisync", "rmdirs"}
        assert not any(a.startswith("--delete") for a in argv)


def test_download_builds_absolute_local_path(tmp_path):
    r = FakeRunner()
    Rclone(runner=r).download("gd:", "a/b.bin", str(tmp_path / "sub" / "b.bin"))
    assert r.calls[0] == ["rclone", "copyto", "gd:a/b.bin", str(tmp_path / "sub" / "b.bin")]
    assert (tmp_path / "sub").is_dir()  # parent created


def test_download_relative_path_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = FakeRunner()
    Rclone(runner=r).download("gd:", "a", "x:y")
    assert r.calls[0][-1] == str(tmp_path / "x:y")  # not mistaken for remote 'x:'


# --- lsjson parsing ----------------------------------------------------------------------------

def test_lsjson_parses_lines_with_and_without_trailing_comma():
    items = [item("a.txt"), item("d/b.txt", Hashes={"md5": "ab"}), item("测试.txt")]
    out = listing(*items)
    assert out.splitlines()[1].endswith(",") and not out.splitlines()[-2].endswith(",")
    assert list(Rclone(runner=FakeRunner(out=out)).lsjson("gd:")) == items


def test_lsjson_only_splits_on_newline():
    items = [item("we\u0085ird\u2028name\x0b.txt")]
    out = "[\n" + json.dumps(items[0], ensure_ascii=False) + "\n]\n"
    assert list(Rclone(runner=FakeRunner(out=out)).lsjson("gd:")) == items


def test_lsjson_last_line_with_comma_is_tolerated():
    items = [item("a"), item("b")]
    assert list(Rclone(runner=FakeRunner(out=listing(*items, trailing_comma=True))).lsjson("gd:")) == items


def test_lsjson_empty_listing():
    assert list(Rclone(runner=FakeRunner(out="[\n]\n")).lsjson("gd:")) == []
    assert list(Rclone(runner=FakeRunner(out="")).lsjson("gd:")) == []


def test_lsjson_is_lazy_generator():
    r = FakeRunner(out=listing(item("a")))
    gen = Rclone(runner=r).lsjson("gd:")
    assert r.calls == []  # nothing runs until iterated
    assert next(gen)["Path"] == "a"


def test_lsjson_failure_raises_after_items():
    r = FakeRunner(rc=3, out="[\n" + json.dumps(item("a")) + ",\n", err="NOTICE: x\nERROR : directory not found\n")
    gen = Rclone(runner=r).lsjson("gd:", "missing")
    assert next(gen)["Path"] == "a"
    with pytest.raises(RcloneError) as ei:
        next(gen)
    e = ei.value
    assert e.returncode == 3
    assert "directory not found" in e.stderr and "directory not found" in str(e)
    assert e.args == r.calls[0] and isinstance(e.args, list)


def test_lsjson_failure_with_empty_output_raises():
    with pytest.raises(RcloneError) as ei:
        list(Rclone(runner=FakeRunner(rc=1, err="boom")).lsjson("nosuch:"))
    assert ei.value.returncode == 1


def test_lsjson_garbage_line_raises():
    with pytest.raises(RcloneError, match="unparseable"):
        list(Rclone(runner=FakeRunner(out="[\n{not json},\n]\n")).lsjson("gd:"))


# --- stat --------------------------------------------------------------------------------------

def test_stat_parses_object_and_builds_argv():
    obj = item("a.txt", Hashes={"md5": "ab"})
    r = FakeRunner(out=json.dumps(obj, indent="\t"))
    assert Rclone(runner=r).stat("gd:", "a.txt", hash_types=("md5",)) == obj
    assert r.calls[0] == ["rclone", "lsjson", "--stat", "--hash", "--hash-type", "md5", "gd:a.txt"]


@pytest.mark.parametrize("rc", [3, 4])
def test_stat_not_found_is_none(rc):
    assert Rclone(runner=FakeRunner(rc=rc, err="directory not found")).stat("gd:", "nope") is None


def test_stat_other_failure_raises():
    with pytest.raises(RcloneError) as ei:
        Rclone(runner=FakeRunner(rc=1, err="Failed to create file system")).stat("nosuch:", "x")
    assert ei.value.returncode == 1


# --- copy / move errors ------------------------------------------------------------------------

def test_copyto_failure_raises():
    with pytest.raises(RcloneError, match="copyto failed"):
        Rclone(runner=FakeRunner(rc=1, err="ERROR : quota")).copyto("a:", "x", "b:", "y")


def test_moveto_failure_raises():
    with pytest.raises(RcloneError, match="moveto failed"):
        Rclone(runner=FakeRunner(rc=5, err="temporary")).moveto("a:", "x", "b:", "y")


# --- about / version ---------------------------------------------------------------------------

def test_about_parses_ints_and_missing_values():
    r = FakeRunner(out='{"total": 100, "used": 40, "free": 60, "trashed": 1}')
    assert Rclone(runner=r).about("gd:") == {"total": 100, "used": 40, "free": 60}
    assert r.calls[0] == ["rclone", "about", "--json", "gd:"]
    assert Rclone(runner=FakeRunner(out='{"used": 5}')).about("gd:") == {"total": None, "used": 5, "free": None}


def test_about_unsupported_is_none_other_errors_raise():
    unsupported = FakeRunner(rc=1, err="NOTICE: Failed to about: Memory root '' doesn't support about")
    assert Rclone(runner=unsupported).about(":memory:") is None
    with pytest.raises(RcloneError):
        Rclone(runner=FakeRunner(rc=1, err="Failed to about: 401 unauthorized")).about("gd:")


def test_version_parses_first_line():
    out = "rclone v1.75.2\n- os/version: ubuntu 24.04 (64 bit)\n"
    assert Rclone(runner=FakeRunner(out=out)).version() == "v1.75.2"
    with pytest.raises(RcloneError):
        Rclone(runner=FakeRunner(out="garbage")).version()


# --- RcloneError --------------------------------------------------------------------------------

def test_rclone_error_attributes_and_pickle():
    e = RcloneError("msg", 3, "err text", ["rclone", "lsjson", "x:"])
    assert (e.returncode, e.stderr, e.args) == (3, "err text", ["rclone", "lsjson", "x:"])
    assert str(e) == "msg"
    e2 = pickle.loads(pickle.dumps(e))
    assert (str(e2), e2.returncode, e2.stderr, e2.args) == ("msg", 3, "err text", ["rclone", "lsjson", "x:"])


def test_missing_binary_is_rclone_error(tmp_path):
    rc = Rclone(str(tmp_path / "no-such-rclone"))
    with pytest.raises(RcloneError, match="cannot run"):
        list(rc.lsjson("x:"))
    with pytest.raises(RcloneError, match="cannot run"):
        rc.version()


# --- subprocess plumbing with a fake binary (no real rclone needed) ---------------------------

def fake_binary(tmp_path, body: str) -> str:
    path = tmp_path / "fake-rclone"
    path.write_text(f"#!{sys.executable}\nimport sys, time, json\n{body}\n")
    path.chmod(path.stat().st_mode | statmod.S_IXUSR)
    return str(path)


def run_with_timeout(fn, seconds: float = 30.0):
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "timed out: probable pipe deadlock"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_heavy_stderr_does_not_deadlock(tmp_path):
    binary = fake_binary(tmp_path, """
sys.stderr.write("x" * 100 + "\\n" * 1 )
for _ in range(20000):
    sys.stderr.write("noise noise noise noise noise noise noise noise noise noise\\n")
sys.stderr.flush()
print("[")
print(json.dumps({"Path": "a", "Name": "a", "Size": 1, "IsDir": False}) + ",")
print(json.dumps({"Path": "b", "Name": "b", "Size": 2, "IsDir": False}))
print("]")
""")
    got = run_with_timeout(lambda: list(Rclone(binary).lsjson("x:")))
    assert [i["Path"] for i in got] == ["a", "b"]


def test_nonzero_exit_raises_after_stream_with_stderr_tail(tmp_path):
    binary = fake_binary(tmp_path, """
print("[")
print(json.dumps({"Path": "a", "Name": "a", "Size": 1, "IsDir": False}) + ",")
sys.stdout.flush()
sys.stderr.write("first\\nERROR : listing exploded\\n")
sys.exit(7)
""")
    gen = Rclone(binary, config="cfg").lsjson("x:", "sub")
    assert next(gen)["Path"] == "a"
    with pytest.raises(RcloneError) as ei:
        next(gen)
    assert ei.value.returncode == 7
    assert "listing exploded" in ei.value.stderr and "listing exploded" in str(ei.value)
    assert ei.value.args == [binary, "--config", "cfg", "lsjson", "--recursive", "x:sub"]


def test_closing_generator_early_kills_process(tmp_path, monkeypatch):
    binary = fake_binary(tmp_path, """
print("[")
print(json.dumps({"Path": "a", "Name": "a", "Size": 1, "IsDir": False}) + ",")
sys.stdout.flush()
time.sleep(600)
""")
    import subprocess
    procs = []
    real_popen = subprocess.Popen

    def spy(*a, **kw):
        p = real_popen(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", spy)
    gen = Rclone(binary).lsjson("x:")
    assert next(gen)["Path"] == "a"
    start = time.monotonic()
    run_with_timeout(gen.close, 20)
    assert time.monotonic() - start < 15
    assert procs and procs[0].poll() is not None


# --- integration against real rclone on local directories -------------------------------------

@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "a.txt").write_text("hello\n")
    (root / "docs" / "b.txt").write_text("hi\n")
    (root / "docs" / "deep" / "测试 文件.md").write_text("中文")
    (root / "zero.bin").write_bytes(b"")
    return root


MD5_HELLO = "b1946ac92492d2347c6235b4d2611184"  # md5("hello\n")


@needs_rclone
def test_real_lsjson_recursive_with_hashes(tree):
    items = {i["Path"]: i for i in Rclone().lsjson(str(tree), hash_types=("md5", "sha1"))}
    assert set(items) == {"a.txt", "docs", "docs/b.txt", "docs/deep", "docs/deep/测试 文件.md", "empty", "zero.bin"}
    assert items["a.txt"]["Size"] == 6 and items["a.txt"]["Hashes"]["md5"] == MD5_HELLO
    assert set(items["a.txt"]["Hashes"]) == {"md5", "sha1"}
    assert items["docs"]["IsDir"] is True and items["zero.bin"]["Size"] == 0


@needs_rclone
def test_real_lsjson_without_hash_types_has_no_hashes(tree):
    assert all("Hashes" not in i for i in Rclone().lsjson(str(tree)))


@needs_rclone
def test_real_lsjson_non_recursive_and_subpath(tree):
    rc = Rclone()
    assert {i["Path"] for i in rc.lsjson(str(tree), recursive=False)} == {"a.txt", "docs", "empty", "zero.bin"}
    assert {i["Path"] for i in rc.lsjson(str(tree), "docs")} == {"b.txt", "deep", "deep/测试 文件.md"}
    assert list(rc.lsjson(str(tree), "empty")) == []


@needs_rclone
def test_real_lsjson_missing_root_raises_not_empty(tree):
    with pytest.raises(RcloneError) as ei:
        list(Rclone().lsjson(str(tree), "does/not/exist"))
    assert ei.value.returncode == 3


@needs_rclone
def test_real_lsjson_unknown_hash_type_raises(tree):
    with pytest.raises(RcloneError) as ei:
        list(Rclone().lsjson(str(tree), hash_types=("bogus",)))
    assert ei.value.returncode != 0 and "bogus" in ei.value.stderr


@needs_rclone
def test_real_lsjson_streams_many_entries(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(500):
        (root / f"f{i:04d}.txt").write_text("x")
    gen = Rclone().lsjson(str(root))
    first = next(gen)
    assert first["Path"].startswith("f")
    assert 1 + sum(1 for _ in gen) == 500


@needs_rclone
def test_real_path_starting_with_dash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "-weird").mkdir()
    (tmp_path / "-weird" / "f.txt").write_text("x")
    assert [i["Path"] for i in Rclone().lsjson("-weird")] == ["f.txt"]


@needs_rclone
def test_real_config_flag_is_applied(tree, tmp_path):
    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[alias1]\ntype = alias\nremote = {tree}\n")
    paths = {i["Path"] for i in Rclone(config=str(conf)).lsjson("alias1:", recursive=False)}
    assert paths == {"a.txt", "docs", "empty", "zero.bin"}
    with pytest.raises(RcloneError):  # without the config the remote is unknown
        list(Rclone(config=str(tmp_path / "nonexistent.conf")).lsjson("alias1:"))


@needs_rclone
def test_real_stat_file_dir_and_missing(tree):
    rc = Rclone()
    f = rc.stat(str(tree), "a.txt", hash_types=("md5",))
    assert f["IsDir"] is False and f["Size"] == 6 and f["Hashes"] == {"md5": MD5_HELLO}
    d = rc.stat(str(tree), "docs")
    assert d["IsDir"] is True
    assert rc.stat(str(tree), "nope.txt") is None
    assert rc.stat(str(tree), "no/such/dir/nope.txt") is None


@needs_rclone
def test_real_stat_invalid_remote_raises():
    with pytest.raises(RcloneError):
        Rclone().stat("definitely-not-a-remote:", "x")


@needs_rclone
def test_real_copyto_creates_destination_with_parents(tree, tmp_path):
    dst = tmp_path / "dst"
    Rclone().copyto(str(tree), "a.txt", str(dst), "new/dir/a.txt")
    assert (dst / "new" / "dir" / "a.txt").read_text() == "hello\n"
    assert (tree / "a.txt").exists()


@needs_rclone
def test_real_copyto_does_not_overwrite_existing(tree, tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "a.txt").write_text("PRECIOUS different content")
    Rclone().copyto(str(tree), "a.txt", str(dst), "a.txt")  # exits 0, silently skipped
    assert (dst / "a.txt").read_text() == "PRECIOUS different content"
    assert (tree / "a.txt").read_text() == "hello\n"


@needs_rclone
def test_real_copyto_missing_source_raises(tree, tmp_path):
    with pytest.raises(RcloneError):
        Rclone().copyto(str(tree), "ghost.txt", str(tmp_path / "dst"), "ghost.txt")
    assert not (tmp_path / "dst" / "ghost.txt").exists()


@needs_rclone
def test_real_moveto_moves_file(tree, tmp_path):
    dst = tmp_path / "dst"
    Rclone().moveto(str(tree), "docs/b.txt", str(dst), "moved/b.txt")
    assert (dst / "moved" / "b.txt").read_text() == "hi\n"
    assert not (tree / "docs" / "b.txt").exists()


@needs_rclone
def test_real_moveto_does_not_overwrite_and_keeps_source(tree, tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "b.txt").write_text("PRECIOUS")
    Rclone().moveto(str(tree), "docs/b.txt", str(dst), "b.txt")
    assert (dst / "b.txt").read_text() == "PRECIOUS"
    assert (tree / "docs" / "b.txt").read_text() == "hi\n"  # source untouched, caller must notice


@needs_rclone
def test_real_move_then_stat_verifies(tree, tmp_path):
    rc = Rclone()
    dst = str(tmp_path / "dst")
    rc.copyto(str(tree), "a.txt", dst, "a.txt")
    assert rc.stat(dst, "a.txt", hash_types=("md5",))["Hashes"]["md5"] == MD5_HELLO


@needs_rclone
def test_real_download_overwrites_local_scratch_file(tree, tmp_path):
    target = tmp_path / "scratch" / "out.bin"
    rc = Rclone()
    rc.download(str(tree), "a.txt", str(target))
    assert target.read_text() == "hello\n"
    target.write_text("stale")
    rc.download(str(tree), "a.txt", str(target))
    assert target.read_text() == "hello\n"


@needs_rclone
def test_real_about_and_version(tree):
    rc = Rclone()
    info = rc.about(str(tree))
    assert set(info) == {"total", "used", "free"} and all(isinstance(v, int) for v in info.values())
    assert rc.about(":memory:") is None
    assert rc.version().startswith("v1.")


# --- classify_failure: how a failed rclone call is worded (docs/ONBOARDING.md section G4.3) -------------

from drivemerge.rclone import classify_failure  # noqa: E402


@pytest.mark.parametrize("rc, stderr, code", [
    (1, 'CRITICAL: Failed to create file system for "x:": didn\'t find section in config file ("x")', "NOT_FOUND"),
    (3, "ERROR : error listing: directory not found", "NOT_FOUND"),
    (4, "something else entirely", "NOT_FOUND"),
    (1, "couldn't fetch token - maybe it has expired?: oauth2: token expired and refresh token is not set",
     "AUTH_EXPIRED"),
    (1, "Error: invalid_grant: Token has been expired or revoked.", "AUTH_EXPIRED"),
    (1, "googleapi: Error 401: Invalid Credentials", "AUTH_EXPIRED"),
    (1, 'Get "https://www.googleapis.com": dial tcp: lookup www.googleapis.com: no such host', "NETWORK"),
    (1, "read tcp 10.0.0.2:5555->1.2.3.4:443: i/o timeout", "NETWORK"),
    (1, "context deadline exceeded", "NETWORK"),
    (1, "rclone lsjson timed out after 30 s", "NETWORK"),
    (1, "something odd", "RCLONE_ERROR"),
    (2, "", "RCLONE_ERROR"),
])
def test_classify_failure(rc, stderr, code):
    assert classify_failure(rc, stderr) == code
