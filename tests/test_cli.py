"""Command-line interface: argument mapping, output, exit codes and the human-only gate.

Most tests call `cli.main(argv)` against a temp config with `local` remotes and a
real `Service`. Methods whose core modules may not be finished (builders, executor)
are replaced with recorders, so these tests check what the CLI sends and prints.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import Catalog

from drivemerge import cli, config as config_mod, db, executor, plans
from drivemerge.rclone import RcloneError
from drivemerge.service import Service

AGENT_VARS = ("CLAUDECODE", "AI_AGENT", "DRIVEMERGE_CONFIG")


# ---------------------------------------------------------------------------- fixtures

@dataclass
class Env:
    tmp: Path
    config: Path             # execution disabled
    config_enabled: Path     # same catalog, [safety] execution_enabled = true
    db_path: Path


def _config_text(tmp: Path, execution_enabled: bool) -> str:
    return f"""
[database]
path = "{tmp / 'catalog.db'}"

[safety]
execution_enabled = {str(execution_enabled).lower()}

[api]
token = "test-token"

[[remotes]]
name = "alpha"
provider = "local"
read = "{tmp / 'a'}"
write = "{tmp / 'a'}"

[[remotes]]
name = "beta"
provider = "local"
read = "{tmp / 'b'}"
"""


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch):
    """Tests run inside AI agent sessions too; the gate must not see those variables."""
    for var in AGENT_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
    paths = []
    for name, enabled in (("config.toml", False), ("config-enabled.toml", True)):
        p = tmp_path / name
        p.write_text(_config_text(tmp_path, enabled))
        paths.append(p)
    return Env(tmp_path, paths[0], paths[1], tmp_path / "catalog.db")


@pytest.fixture
def run(env, capsys):
    def _run(*argv, config: Path | None = None):
        rc = cli.main(["--config", str(config or env.config), *argv])
        out, err = capsys.readouterr()
        return rc, out, err
    return _run


@pytest.fixture
def jrun(run):
    def _jrun(*argv, **kw):
        rc, out, err = run("--json", *argv, **kw)
        return rc, json.loads(out)
    return _jrun


@pytest.fixture
def catalog_db(env):
    """alpha: Docs/report.pdf, Photos/img1.jpg; beta: Backup/report copy.pdf (same content as the first)."""
    conn = db.connect(env.db_path)
    cat = Catalog(conn)
    a, b = cat.remote("alpha"), cat.remote("beta", writable=False)
    cat.file(a, "Docs/report.pdf", size=100, sha256="aa")
    cat.file(a, "Photos/img1.jpg", size=2048, sha256="bb")
    cat.file(b, "Backup/report copy.pdf", size=100, sha256="aa")
    conn.close()
    return env


@pytest.fixture
def trun(run, monkeypatch):
    """`run` as if stdin and stdout were a terminal (patched per call, so capture can't undo it)."""
    def _trun(*argv, **kw):
        with monkeypatch.context() as m:
            m.setattr(sys.stdin, "isatty", lambda: True, raising=False)
            m.setattr(sys.stdout, "isatty", lambda: True, raising=False)
            return run(*argv, **kw)
    return _trun


@pytest.fixture
def no_input(monkeypatch):
    def refuse(prompt=""):
        raise AssertionError(f"must not prompt: {prompt!r}")
    monkeypatch.setattr("builtins.input", refuse)


def record(monkeypatch, name, returns=None, raises=None):
    """Replace Service.<name>; the returned list collects (args, kwargs) of each call."""
    calls = []

    def fake(self, *args, **kwargs):
        calls.append((args, kwargs))
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(Service, name, fake)
    return calls


def typed(monkeypatch, *answers):
    """Make input() answer from `answers` (a callable gets the prompt); returns the prompts seen."""
    prompts, queue = [], list(answers)

    def fake_input(prompt=""):
        prompts.append(prompt)
        a = queue.pop(0)
        return a(prompt) if callable(a) else a
    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def sim_report(ok=True, violations=(), **extra):
    return {
        "plan_id": 7, "digest": "a" * 64, "approval_code": "aaaaaaaa", "ok": ok,
        "violations": list(violations), "warnings": [{"code": "NOOP", "seq": 1, "message": "same"}],
        "remotes": [{"remote_id": 1, "remote": "alpha", "files_before": 3, "bytes_before": 3 * 2**20,
                     "files_after": 4, "bytes_after": 5 * 2**30, "bytes_in": 1, "files_quarantined": 1,
                     "bytes_quarantined": 2048}],
        "contents_before": 3, "contents_after": 3, "contents_lost": 0, "bytes_cross_remote": 1536,
        "op_counts": {"copy": 2, "delete": 1}, "sample": [], **extra,
    }


def proposed(**kw):
    return {"plan_id": 7, "ops": 3, "skipped": [{"location": "alpha:x", "reason": "native doc"}],
            "stats": {"files": 9}, "simulation": sim_report(**kw)}


@pytest.fixture
def planned(env, catalog_db, run):
    """Plan 1 (simulated, ok): copy alpha:Docs/report.pdf to alpha:Archive/report.pdf."""
    spec = env.tmp / "ops.json"
    spec.write_text(json.dumps([{"op": "copy", "src": "alpha:Docs/report.pdf", "dst": "alpha:Archive/report.pdf"}]))
    rc, out, err = run("plan", "manual", str(spec), "--title", "archive report")
    assert rc == 0, out + err
    return 1


# --------------------------------------------------------------------------------- init

def test_init_writes_private_config_with_token_and_catalog(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    path = tmp_path / "conf" / "config.toml"
    assert cli.main(["--config", str(path), "init"]) == 0
    out = capsys.readouterr().out

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    token = tomllib.loads(path.read_text())["api"]["token"]
    assert len(token) >= 32
    assert config_mod.load_config(path).api.token == token
    assert token not in out                       # never echoed
    assert "rclone config" in out and "[[remotes]]" in out
    assert (home / ".local/share/drive-merge/catalog.db").exists()


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.toml"
    assert cli.main(["--config", str(path), "init"]) == 0
    first = path.read_text()
    capsys.readouterr()

    assert cli.main(["--config", str(path), "init"]) == 1
    assert "--force" in capsys.readouterr().err
    assert path.read_text() == first

    path.chmod(0o644)
    assert cli.main(["--config", str(path), "init", "--force"]) == 0
    assert path.read_text() != first              # new token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_init_json_reports_paths_not_the_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.toml"
    assert cli.main(["--config", str(path), "--json", "init"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["config"] == str(path)
    assert tomllib.loads(path.read_text())["api"]["token"] not in out


# ------------------------------------------------------------------------------- sizes

@pytest.mark.parametrize("text,expected", [
    ("1024", 1024), ("0", 0), ("5G", 5 * 2**30), ("500M", 500 * 2**20), ("1T", 2**40), ("2k", 2048),
    ("1.5K", 1536), ("2GiB", 2 * 2**30), ("10MB", 10 * 2**20), ("10 m", 10 * 2**20), ("7B", 7),
])
def test_parse_size(text, expected):
    assert cli.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "G", "abc", "5X", "-5", "1e3", "5 GB GB"])
def test_parse_size_rejects_garbage(text):
    with pytest.raises(ValueError):
        cli.parse_size(text)


def test_format_size():
    assert cli.format_size(None) == "-"
    assert cli.format_size(0) == "0 B"
    assert cli.format_size(1023) == "1023 B"
    assert cli.format_size(1536) == "1.5 KiB"
    assert cli.format_size(5 * 2**20) == "5.0 MiB"
    assert cli.format_size(3 * 2**30) == "3.0 GiB"
    assert cli.format_size(2**40) == "1.0 TiB"


def test_bridge_maps_size_and_limit(run, monkeypatch):
    calls = record(monkeypatch, "bridge", {"bridged": 2, "failed": [{"entry_id": 9, "error": "boom"}], "groups": None})
    rc, out, err = run("bridge", "--max-bytes", "5G", "--limit", "7")
    assert rc == 0
    assert calls[0][1]["max_bytes"] == 5 * 2**30 and calls[0][1]["limit"] == 7 and callable(calls[0][1]["progress"])
    assert "Bridged 2" in out and "entry 9: boom" in out


def test_bad_size_is_a_usage_error(run):
    rc, out, err = run("bridge", "--max-bytes", "lots")
    assert rc == 2 and "invalid size" in err


# ----------------------------------------------------------------------- read commands

def test_search_json_and_filters(catalog_db, jrun):
    rc, rows = jrun("search", "report")
    assert rc == 0
    assert sorted(r["path"] for r in rows) == ["Backup/report copy.pdf", "Docs/report.pdf"]
    assert {r["remote"] for r in rows} == {"alpha", "beta"}

    _, rows = jrun("search", "--remote", "beta")
    assert [r["path"] for r in rows] == ["Backup/report copy.pdf"]
    _, rows = jrun("search", "--ext", "jpg", "--min-size", "1K")
    assert [r["path"] for r in rows] == ["Photos/img1.jpg"]
    _, rows = jrun("search", "--dirs", "--prefix", "Docs")
    assert {r["path"] for r in rows} == {"Docs", "Docs/report.pdf"}


def test_search_maps_every_flag(env, monkeypatch, run):
    calls = record(monkeypatch, "search", [])
    rc, out, _ = run("search", "tax 2023", "--remote", "alpha", "--category", "document", "--ext", "pdf",
                     "--min-size", "1M", "--max-size", "2G", "--after", "2023-01-01", "--before", "2024",
                     "--prefix", "Docs", "--dirs", "--missing", "--dups", "--limit", "5", "--offset", "10")
    assert rc == 0 and "No matches" in out
    args, kw = calls[0]
    assert args == ("tax 2023",)
    assert kw == dict(remote="alpha", category="document", ext="pdf", min_size=2**20, max_size=2 * 2**30,
                      modified_after="2023-01-01", modified_before="2024", path_prefix="Docs",
                      include_dirs=True, include_missing=True, duplicates_only=True, limit=5, offset=10)


def test_search_rejects_a_bad_date(run):
    rc, _, err = run("search", "--after", "2023-13-45")
    assert rc == 2 and "invalid date" in err


def test_search_human_output(catalog_db, run):
    rc, out, _ = run("search", "img")
    assert rc == 0
    assert "Photos/img1.jpg" in out and "2.0 KiB" in out and "alpha" in out


def test_stats_json_and_human(catalog_db, jrun, run):
    rc, st = jrun("stats")
    assert rc == 0
    assert st["totals"] == {"files": 3, "bytes": 2248}
    assert {r["name"]: r["files"] for r in st["remotes"]} == {"alpha": 2, "beta": 1}

    rc, out, _ = run("stats")
    assert rc == 0 and "3 files" in out and "alpha" in out and "beta" in out


def test_json_flag_works_after_the_subcommand(catalog_db, run):
    rc, out, _ = run("stats", "--json")
    assert rc == 0 and json.loads(out)["totals"]["files"] == 3


def test_remotes_lists_read_only_and_last_scan(catalog_db, jrun, run):
    rc, rows = jrun("remotes")
    by = {r["name"]: r for r in rows}
    assert by["alpha"]["read_only"] is False and by["beta"]["read_only"] is True
    assert by["alpha"]["last_scan"]["status"] == "complete"
    rc, out, _ = run("remotes")
    beta_line = next(line for line in out.splitlines() if line.startswith("beta"))
    assert "read-only" in beta_line and "complete" in beta_line


def test_identity_duplicates_and_locations(catalog_db, jrun, run):
    rc, out, _ = run("identity", "rebuild")
    assert rc == 0 and "duplicate groups" in out
    rc, groups = jrun("duplicates", "--cross-remote")
    assert rc == 0 and len(groups) == 1
    assert groups[0]["confidence"] == "exact"
    assert sorted(c["remote"] for c in groups[0]["copies"]) == ["alpha", "beta"]
    entry_id = groups[0]["copies"][0]["entry_id"]

    rc, copies = jrun("locations", str(entry_id))
    assert rc == 0 and len(copies) == 2
    rc, out, _ = run("locations", str(entry_id))
    assert "alpha:Docs/report.pdf" in out and "beta:Backup/report copy.pdf" in out

    rc, out, err = run("locations", "9999")
    assert rc == 1 and "no live copies" in err


def test_duplicates_maps_flags(run, monkeypatch):
    calls = record(monkeypatch, "duplicates", [])
    assert run("duplicates", "--probable", "--remote", "alpha", "--cross-remote", "--min-size", "1M",
               "--prefix", "Docs", "--limit", "5", "--offset", "2")[0] == 0
    assert calls[0][1] == dict(confidence="probable", remote="alpha", cross_remote_only=True, min_size=2**20,
                               path_prefix="Docs", limit=5, offset=2)
    run("duplicates")
    assert calls[1][1]["confidence"] == "exact" and calls[1][1]["cross_remote_only"] is False


def test_profiles_maps_flags_and_prints(run, monkeypatch):
    found = [{"remote": "alpha", "path": "Photos", "files": 12, "dirs": 1, "bytes": 3 * 2**20,
              "categories": {"image": 12}, "top_exts": [["jpg", 12]], "mtime_min": "2020-01-01T00:00:00Z",
              "mtime_max": "2021-01-01T00:00:00Z", "sample_names": ["a.jpg"], "unit": None, "duplicate_files": 2}]
    calls = record(monkeypatch, "profiles", found)
    rc, out, _ = run("profiles", "--remote", "alpha", "--prefix", "Photos", "--depth", "2", "--limit", "5")
    assert rc == 0
    assert calls[0][1] == dict(remote="alpha", prefix="Photos", depth=2, limit=5)
    assert "alpha:Photos" in out and "12 files" in out and "jpg 12" in out and "2 duplicate files" in out


def test_changes_feed(catalog_db, jrun, run):
    rc, res = jrun("changes", "--limit", "2")
    assert rc == 0 and len(res["rows"]) == 2 and res["cursor"]
    rc, rest = jrun("changes", "--cursor", res["cursor"])
    _, everything = jrun("changes", "--limit", "100")
    assert [r["id"] for r in res["rows"] + rest["rows"]] == [r["id"] for r in everything["rows"]]
    assert len(rest["rows"]) == len(everything["rows"]) - 2
    rc, out, _ = run("changes")
    assert "next cursor" in out


def test_rules_check_and_preview(env, run, monkeypatch):
    good = env.tmp / "rules.toml"
    good.write_text('[layout]\nfallback = "Unsorted/{remote}/{path}"\n\n'
                    '[[rule]]\nname = "photos"\ncategory = ["photo"]\ntarget = "Photos/{year}/{name}"\n')
    bad = env.tmp / "bad.toml"
    bad.write_text("[[rule]\nname =")
    rc, out, _ = run("rules", "check", str(good))
    assert rc == 0 and out.startswith("OK")
    rc, out, err = run("rules", "check", str(bad))
    assert rc == 1 and "Invalid rules" in err
    rc, out, err = run("rules", "check", str(env.tmp / "missing.toml"))
    assert rc == 1 and "cannot read" in err

    calls = record(monkeypatch, "preview_layout", {"total": 1, "by_rule": {"photos": 1}, "sample": [
        {"entry_id": 1, "remote_id": 1, "src_path": "a.jpg", "target_path": "Photos/2020/a.jpg", "rule": "photos", "unit": None}]})
    rc, out, _ = run("rules", "preview", "--rules", str(good), "--remote", "alpha", "--remote", "beta",
                     "--target-root", "Archive", "--prefix", "Pics", "--limit", "3")
    assert rc == 0
    assert calls[0][1] == dict(rules_text=good.read_text(), remotes=["alpha", "beta"], target_root="Archive",
                               path_prefix="Pics", limit=3)
    assert "photos" in out and "Photos/2020/a.jpg" in out


# ------------------------------------------------------------------------------- scan

def test_scan_one_and_all(run, monkeypatch):
    done = lambda name: {"scan_id": 1, "remote": name, "root": "", "status": "complete", "files": 3,
                         "dirs": 1, "bytes": 2048, "tombstoned": 0, "error": None}
    calls = record(monkeypatch, "scan", None)

    def fake(self, remote, *, root="", progress=None, force=False):
        calls.append(((remote,), {"root": root, "progress": progress}))
        return done(remote)
    monkeypatch.setattr(Service, "scan", fake)

    rc, out, _ = run("scan", "alpha", "--root", "Docs")
    assert rc == 0 and "alpha: 3 files, 1 dirs, 2.0 KiB" in out
    assert calls[-1][0] == ("alpha",) and calls[-1][1]["root"] == "Docs" and callable(calls[-1][1]["progress"])

    calls.clear()
    rc, out, _ = run("scan", "--all")
    assert rc == 0 and [c[0][0] for c in calls] == ["alpha", "beta"]


def test_scan_says_how_many_items_share_a_name_with_another_file(run, monkeypatch):
    """Names that differ only in invalid UTF-8 (or two Drive files at one path) are kept as separate, flagged rows."""
    def fake(self, remote, *, root="", progress=None, force=False):
        return {"scan_id": 1, "remote": remote, "root": "", "status": "complete", "files": 3, "dirs": 1,
                "bytes": 2048, "tombstoned": 0, "error": None, "ambiguous": 2}
    monkeypatch.setattr(Service, "scan", fake)
    rc, out, _ = run("scan", "alpha")
    assert rc == 0 and "alpha: 3 files, 1 dirs, 2.0 KiB, 0 no longer present, 2 share a name with another file" in out
    assert "AMBIGUOUS_PATH" in out
    rc, out, _ = run("--json", "scan", "alpha")
    assert json.loads(out)["ambiguous"] == 2


def test_scan_all_continues_after_a_failure(run, monkeypatch):
    def fake(self, remote, *, root="", progress=None, force=False):
        if remote == "alpha":
            raise RcloneError("rclone lsjson failed (exit 1): boom")
        return {"scan_id": 2, "remote": remote, "root": "", "status": "complete", "files": 1, "dirs": 0,
                "bytes": 1, "tombstoned": 0, "error": None}
    monkeypatch.setattr(Service, "scan", fake)
    rc, out, err = run("scan", "--all")
    assert rc == 1
    assert "alpha: scan failed" in err and "boom" in err
    assert "beta: 1 files" in out


@pytest.mark.parametrize("argv", [["scan"], ["scan", "alpha", "--all"], ["scan", "--all", "--root", "x"]])
def test_scan_argument_errors(run, argv):
    rc, _, err = run(*argv)
    assert rc == 1 and err.startswith("drivemerge:")


def test_scan_unknown_remote(run):
    rc, _, err = run("scan", "nope")
    assert rc == 1 and "unknown remote 'nope'" in err


# ----------------------------------------------------------------------- plan creation

def test_plan_consolidate_maps_flags_and_rules_text(env, run, monkeypatch):
    rules = env.tmp / "rules.toml"
    rules.write_text('[layout]\nfallback = "Unsorted/{path}"\n')
    calls = record(monkeypatch, "propose_plan", proposed())
    rc, out, _ = run("plan", "consolidate", "--target", "alpha", "--root", "Archive", "--source", "beta",
                     "--delete-sources", "--reorganize-target", "--rules", str(rules), "--prefix", "Docs",
                     "--title", "Gather")
    assert rc == 0
    args, kw = calls[0]
    assert args[0] == "consolidate" and kw == {"created_by": "cli", "title": "Gather"}
    assert args[1] == {"target": "alpha", "target_root": "Archive", "sources": ["beta"], "delete_sources": True,
                       "reorganize_target": True, "path_prefix": "Docs", "rules": rules.read_text()}

    run("plan", "consolidate", "--target", "alpha")
    assert calls[1][0][1] == {"target": "alpha", "delete_sources": False, "reorganize_target": False}
    assert calls[1][1]["title"] is None


def test_plan_consolidate_requires_target(run):
    rc, _, err = run("plan", "consolidate")
    assert rc == 2 and "--target" in err


def test_plan_dedupe_reorganize_and_manual_mapping(env, run, monkeypatch):
    calls = record(monkeypatch, "propose_plan", proposed())
    run("plan", "dedupe", "--remote", "alpha", "--cross-remote", "--prefix", "Docs", "--min-size", "1M",
        "--prefer", "alpha", "--prefer", "beta", "--title", "Dedupe")
    assert calls[0][0] == ("dedupe", {"remote": "alpha", "cross_remote": True, "path_prefix": "Docs",
                                      "min_size": 2**20, "prefer_remotes": ["alpha", "beta"]})
    run("plan", "dedupe")
    assert calls[1][0][1] == {"cross_remote": False}

    run("plan", "reorganize", "--remote", "alpha", "--root", "Sorted", "--prefix", "Misc")
    assert calls[2][0] == ("reorganize", {"remote": "alpha", "target_root": "Sorted", "path_prefix": "Misc"})

    ops = [{"op": "delete", "src": "beta:a", "witness": "alpha:a"}]
    f = env.tmp / "ops.json"
    f.write_text(json.dumps(ops))
    run("plan", "manual", str(f), "--title", "mine")
    assert calls[3][0] == ("manual", {"ops": ops}) and calls[3][1]["title"] == "mine"

    f.write_text(json.dumps({"ops": ops}))
    run("plan", "manual", str(f))
    assert calls[4][0][1] == {"ops": ops}

    f.write_text("{not json")
    rc, _, err = run("plan", "manual", str(f))
    assert rc == 1 and "invalid JSON" in err
    f.write_text('"just a string"')
    assert run("plan", "manual", str(f))[0] == 1


def test_plan_creation_output_has_report_and_next_step(run, monkeypatch):
    record(monkeypatch, "propose_plan", proposed())
    rc, out, _ = run("plan", "dedupe")
    assert rc == 0
    assert "Plan 7 created" in out and "3 op(s)" in out and "1 skipped" in out
    assert "Simulation: OK" in out and "Warnings: 1" in out
    assert "alpha: 3 files / 3.0 MiB -> 4 files / 5.0 GiB" in out
    assert "Bytes quarantined: 2.0 KiB" in out and "Bytes copied across remotes: 1.5 KiB" in out
    assert "lost: 0" in out
    assert "drivemerge plan approve 7" in out
    assert "aaaaaaaa" not in out                    # the approval code is shown by `plan approve` only


def test_plan_creation_json_never_contains_approval_code(run, monkeypatch):
    record(monkeypatch, "propose_plan", proposed())
    rc, out, _ = run("--json", "plan", "dedupe")
    data = json.loads(out)
    assert rc == 0 and data["plan_id"] == 7 and data["simulation"]["ok"] is True
    assert "aaaa" not in out and "approval_code" not in out and "digest" not in out


def test_violations_give_exit_code_3(run, monkeypatch):
    violations = [{"code": "CONTENT_LOST", "seq": 2, "message": "last copy of b.txt"},
                  *({"code": "NO_WITNESS", "seq": i, "message": f"op {i} lacks a witness"} for i in range(3, 28))]
    record(monkeypatch, "propose_plan", proposed(ok=False, violations=violations))
    rc, out, _ = run("plan", "dedupe")
    assert rc == cli.EXIT_VIOLATIONS == 3
    assert "FAILED, 26 violation(s)" in out
    assert "CONTENT_LOST" in out and "op 2" in out and "last copy of b.txt" in out
    assert "... and 6 more" in out                       # 20 shown
    assert "drivemerge plan approve" not in out

    rc, out, _ = run("--json", "plan", "dedupe")
    assert rc == 3 and json.loads(out)["simulation"]["ok"] is False


def test_a_capped_report_says_how_much_it_leaves_out(run, monkeypatch):
    """The simulator lists its first 1000 findings and counts all of them; the summary must not pretend otherwise."""
    listed = [{"code": "SRC_MISSING", "seq": i, "message": f"op {i} lacks a source"} for i in range(1, 1001)]
    extra = dict(violations_total=5000, violations_by_code={"DST_EXISTS": 600, "SRC_MISSING": 4399, "NO_WITNESS": 1},
                 warnings_total=2500)
    record(monkeypatch, "propose_plan", proposed(ok=False, violations=listed, **extra))
    rc, out, _ = run("plan", "dedupe")
    assert rc == 3
    assert "FAILED, 5,000 violation(s)" in out and "... and 4,980 more" in out
    assert "by code: SRC_MISSING 4,399, DST_EXISTS 600, NO_WITNESS 1" in out
    assert "Warnings: 2,500" in out
    lines = cli.format_report({**proposed(ok=False, violations=listed[:3], **extra)["simulation"]})
    assert "... and 4,997 more" in "\n".join(lines), "counts what is not listed, not what is not shown"


def test_a_report_without_totals_still_prints(run):
    lines = cli.format_report({"ok": False, "violations": [{"code": "BAD_PATH", "seq": 1, "message": "m"}] * 3})
    assert lines[0] == "Simulation: FAILED, 3 violation(s)" and "by code" not in "\n".join(lines)


def test_nothing_to_do_is_not_an_error(run, monkeypatch):
    record(monkeypatch, "propose_plan", {"plan_id": None, "ops": 0, "skipped": [], "stats": {}, "message": "nothing to do"})
    rc, out, _ = run("plan", "dedupe")
    assert rc == 0 and "Nothing to do" in out


def test_plan_simulate_exit_code(run, monkeypatch):
    calls = record(monkeypatch, "simulate", sim_report())
    rc, out, _ = run("plan", "simulate", "7")
    assert rc == 0 and calls[0][0] == (7,) and "Simulation: OK" in out and "aaaaaaaa" not in out
    record(monkeypatch, "simulate", sim_report(ok=False, violations=[{"code": "SRC_MISSING", "seq": 1, "message": "gone"}]))
    rc, out, _ = run("plan", "simulate", "7")
    assert rc == 3 and "SRC_MISSING" in out


def test_json_output_hides_everything_that_lets_someone_approve(planned, run, env):
    """digest, approval code and the snapshot it binds to: the MCP server and the REST routes drop them too."""
    conn = db.connect(env.db_path)
    try:
        plan = plans.get_plan(conn, planned, with_ops=False)
    finally:
        conn.close()
    assert plan.digest and plan.snapshot, "the fixture plan must carry both"
    for argv in (("plan", "show", "1"), ("plan", "list"), ("plan", "simulate", "1")):
        _, out, _ = run("--json", *argv)
        assert plan.digest not in out and plans.short_code(plan.digest) not in out, argv
        for key in ('"digest"', '"approval_code"', '"snapshot"'):
            assert key not in out, (argv, key)


def test_plan_list_show_cancel(planned, run, jrun):
    rc, out, _ = run("plan", "list")
    assert rc == 0 and "archive report" in out and "simulated" in out
    rc, plans_ = jrun("plan", "list", "--status", "draft")
    assert rc == 0 and plans_ == []

    rc, out, _ = run("plan", "show", "1", "--ops", "5")
    assert rc == 0
    assert "Plan 1: archive report" in out
    assert "alpha:Docs/report.pdf -> alpha:Archive/report.pdf" in out
    assert "Simulation: OK" in out

    rc, plan = jrun("plan", "show", "1")
    assert plan["status"] == "simulated" and plan["ops_total"] == 1 and plan["simulation"]["ok"] is True
    assert "digest" not in json.dumps(plan) and "approval_code" not in json.dumps(plan)

    rc, out, _ = run("plan", "cancel", "1")
    assert rc == 0 and "cancelled" in out
    assert jrun("plan", "show", "1")[1]["status"] == "cancelled"


# -------------------------------------------------------------------- the human gate

GATED = [
    ("approve", ["plan", "approve", "1"]),
    ("execute", ["plan", "execute", "1"]),
    ("restore", ["plan", "restore", "1"]),
    ("purge", ["plan", "purge", "1"]),
]


def _spy_gated(monkeypatch):
    return {name: record(monkeypatch, name, {"plan_id": 1, "status": "done"})
            for name in ("approve", "execute", "restore", "purge")}


@pytest.mark.parametrize("name,argv", GATED)
def test_gate_refuses_without_a_tty(name, argv, run, monkeypatch, no_input):
    spies = _spy_gated(monkeypatch)
    rc, out, err = run(*argv, config=None)
    assert rc == 2 and "interactive terminal" in err
    assert not any(spies.values())


@pytest.mark.parametrize("name,argv", GATED)
def test_gate_refuses_when_only_one_side_is_a_tty(name, argv, run, monkeypatch, no_input):
    spies = _spy_gated(monkeypatch)
    for stdin_tty, stdout_tty in ((True, False), (False, True)):
        with monkeypatch.context() as m:
            m.setattr(sys.stdin, "isatty", lambda v=stdin_tty: v, raising=False)
            m.setattr(sys.stdout, "isatty", lambda v=stdout_tty: v, raising=False)
            rc, _, err = run(*argv)
        assert rc == 2 and "interactive terminal" in err
    assert not any(spies.values())


@pytest.mark.parametrize("var", ["CLAUDECODE", "AI_AGENT"])
@pytest.mark.parametrize("name,argv", GATED)
def test_gate_refuses_inside_an_ai_agent(name, argv, var, run, monkeypatch, trun, no_input):
    spies = _spy_gated(monkeypatch)
    monkeypatch.setenv(var, "1")
    rc, out, err = trun(*argv, config=None)
    assert rc == 2 and "AI agent" in err and var in err and "own terminal" in err
    assert not any(spies.values())


def test_gate_refusal_in_json_mode(run, no_input):
    rc, out, _ = run("--json", "plan", "approve", "1")
    assert rc == 2 and "interactive terminal" in json.loads(out)["error"]


def test_gate_comes_before_any_catalog_access(env, capsys, no_input):
    """A refused approve must not even touch the config or database."""
    rc = cli.main(["--config", str(env.tmp / "does-not-exist.toml"), "plan", "approve", "1"])
    assert rc == 2 and "interactive terminal" in capsys.readouterr().err
    assert not env.db_path.exists()


def test_approve_with_the_right_code(planned, run, jrun, trun, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "tester")
    prompts = typed(monkeypatch, lambda prompt: re.search(r"approval code (\w{8}) to approve plan 1: $", prompt).group(1))
    rc, out, err = trun("plan", "approve", "1")
    assert rc == 0, out + err
    assert re.fullmatch(r"Type the approval code [0-9a-f]{8} to approve plan 1: ", prompts[0])
    assert "Plan 1: archive report" in out and "Simulation: OK" in out     # summary shown before the prompt
    _, plan = jrun("plan", "show", "1")
    assert plan["status"] == "approved" and plan["approved_by"] == "cli:tester"


def test_approve_passes_the_typed_text_to_the_service(planned, run, trun, monkeypatch):
    calls = record(monkeypatch, "approve")
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "tester")
    typed(monkeypatch, lambda p: "  " + re.search(r"code (\w{8})", p).group(1) + " ")
    assert trun("plan", "approve", "1")[0] == 0
    (args, kw) = calls[0]
    assert args[0] == 1 and re.fullmatch(r"[0-9a-f]{8}", args[1]) and kw == {"approved_by": "cli:tester"}


def test_approve_with_a_wrong_code_fails_and_changes_nothing(planned, run, jrun, trun, monkeypatch):
    typed(monkeypatch, "deadbeef")
    rc, out, err = trun("plan", "approve", "1")
    assert rc == 1 and "does not match" in err
    assert jrun("plan", "show", "1")[1]["status"] == "simulated"


def test_approve_wrong_code_json_mode(planned, run, trun, monkeypatch):
    typed(monkeypatch, "deadbeef")
    rc, out, _ = trun("--json", "plan", "approve", "1")
    assert rc == 1 and "does not match" in json.loads(out.splitlines()[-1])["error"]


def test_approve_stops_before_prompting_when_not_approvable(planned, run, trun, no_input):
    assert trun("plan", "cancel", "1")[0] == 0
    rc, _, err = trun("plan", "approve", "1")
    assert rc == 1 and "cancelled" in err
    rc, _, err = trun("plan", "approve", "99")
    assert rc == 1 and "no plan 99" in err


def test_ctrl_d_at_the_prompt_aborts(planned, run, trun, monkeypatch):
    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    rc, _, err = trun("plan", "approve", "1")
    assert rc == 1 and "aborted" in err


def test_execute_refuses_when_execution_is_disabled(planned, run, trun, no_input, monkeypatch):
    calls = record(monkeypatch, "execute")
    rc, out, err = trun("plan", "execute", "1")
    assert rc == 1
    assert "execution_enabled = true" in err and "[safety]" in err and "config.toml" in err
    assert not calls


def test_execute_with_the_enabled_config(env, planned, run, trun, monkeypatch):
    calls = record(monkeypatch, "execute", {"plan_id": 1, "status": "done", "done": 4, "failed": 0, "skipped": 1, "error": None})
    prompts = typed(monkeypatch, "yes", "execute  1")

    rc, out, err = trun("plan", "execute", "1", config=env.config_enabled)
    assert rc == 1 and "did not match" in err and not calls           # wrong text: nothing runs
    assert '"execute 1"' in prompts[0]

    rc, out, err = trun("plan", "execute", "1", config=env.config_enabled)
    assert rc == 0, err
    assert calls[0][0] == (1,) and callable(calls[0][1]["progress"])
    assert "done" in out and "4 done" in out and "1 skipped" in out


def test_execute_failure_exit_code(env, planned, run, trun, monkeypatch):
    record(monkeypatch, "execute", {"plan_id": 1, "status": "failed", "done": 1, "failed": 1, "skipped": 0, "error": "op 2 failed"})
    typed(monkeypatch, "execute 1")
    rc, out, err = trun("plan", "execute", "1", config=env.config_enabled)
    assert rc == 1 and "op 2 failed" in err


def test_execute_disabled_error_from_the_executor_is_reported(env, planned, run, trun, monkeypatch):
    record(monkeypatch, "execute", raises=executor.ExecutionDisabled("disabled by policy"))
    typed(monkeypatch, "execute 1")
    rc, _, err = trun("plan", "execute", "1", config=env.config_enabled)
    assert rc == 1 and "disabled by policy" in err


def _approve_and_run_into_the_ground(env, status="failed"):
    """Plan 1 approved, then left in `status` (as a run that stopped would leave it)."""
    conn = db.connect(env.db_path)
    try:
        plan = plans.get_plan(conn, 1)
        plans.approve_plan(conn, 1, plans.short_code(plan.digest), approved_by="test")
        plans.set_status(conn, 1, status)
    finally:
        conn.close()


def _rescan_alpha(env):
    """A newer complete scan: what a rescan after a half-done run looks like to the plan's digest."""
    conn = db.connect(env.db_path)
    try:
        conn.execute("INSERT INTO scans(remote_id, status, started_at, finished_at) "
                     "VALUES (?, 'complete', ?, ?)", (db.remote_id(conn, "alpha"), db.utcnow(), db.utcnow()))
    finally:
        conn.close()


def test_a_failed_plan_whose_catalog_changed_is_told_to_get_a_new_plan_before_any_prompt(
        env, planned, run, trun, no_input, monkeypatch):
    calls = record(monkeypatch, "execute", {"plan_id": 1, "status": "done"})
    _approve_and_run_into_the_ground(env)
    _rescan_alpha(env)
    rc, out, err = trun("plan", "execute", "1", config=env.config_enabled)
    assert rc == 1 and "propose a new plan" in err and "cannot be simulated" in err
    assert "simulate again" not in err
    assert not calls and "Type" not in out, "refused before asking the human to confirm anything"
    rc, out, _ = trun("--json", "plan", "execute", "1", config=env.config_enabled)
    assert rc == 1 and "propose a new plan" in json.loads(out)["error"]


def test_an_approved_plan_whose_catalog_changed_is_told_to_simulate_again(env, planned, trun, no_input, monkeypatch):
    calls = record(monkeypatch, "execute", {"plan_id": 1, "status": "done"})
    _approve_and_run_into_the_ground(env, status="approved")
    _rescan_alpha(env)
    rc, _, err = trun("plan", "execute", "1", config=env.config_enabled)
    assert rc == 1 and "simulate again" in err and "new plan" not in err and not calls


def test_the_executors_own_stale_error_reaches_the_user_the_same_way(env, planned, trun, monkeypatch):
    """If the catalog changes while the human reads the summary, the executor's refusal says the same thing."""
    record(monkeypatch, "execute", raises=plans.StaleSimulation("plan 1 failed ...: propose a new plan"))
    typed(monkeypatch, "execute 1")
    rc, _, err = trun("plan", "execute", "1", config=env.config_enabled)
    assert rc == 1 and "propose a new plan" in err


def _executing(env, lock):
    """Plan 1 as a crashed run leaves it: 'executing', with `lock` (a dict, or None for no lock row)."""
    conn = db.connect(env.db_path)
    try:
        plans.set_status(conn, 1, "executing")
        if lock is not None:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('executing:1', ?)", (json.dumps(lock),))
    finally:
        conn.close()


def _holder(pid):
    return {"pid": pid, "host": socket.gethostname(), "started_at": "2024-05-01T10:00:00.000000Z"}


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_plan_reset_frees_a_plan_left_executing(env, planned, run, jrun):
    _executing(env, _holder(_dead_pid()))
    rc, out, err = run("plan", "reset", "1")          # no terminal, no prompt: it never touches a cloud
    assert rc == 0, err
    assert "Plan 1 reset to failed" in out and "drivemerge plan execute 1" in out
    assert jrun("plan", "show", "1")[1]["status"] == "failed"
    conn = db.connect(env.db_path)
    assert conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'executing:1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal WHERE plan_id = 1 AND action = 'reset'").fetchone()[0] == 1
    conn.close()


def test_plan_reset_json(env, planned, run):
    _executing(env, None)
    rc, out, _ = run("--json", "plan", "reset", "1")
    assert rc == 0 and json.loads(out) == {"plan_id": 1, "status": "failed"}


def test_plan_reset_refuses_while_the_executor_is_alive(env, planned, run, jrun):
    _executing(env, _holder(os.getpid()))
    rc, out, err = run("plan", "reset", "1")
    assert rc == 1 and f"pid {os.getpid()}" in err and "being executed" in err
    assert jrun("plan", "show", "1")[1]["status"] == "executing"
    rc, out, _ = run("--json", "plan", "reset", "1")
    assert rc == 1 and "being executed" in json.loads(out)["error"]


def test_plan_reset_only_applies_to_an_executing_plan(planned, run):
    rc, _, err = run("plan", "reset", "1")
    assert rc == 1 and "simulated" in err and "executing" in err
    rc, _, err = run("plan", "reset", "99")
    assert rc == 1 and "no plan 99" in err


def test_plan_reset_is_not_human_gated_and_calls_no_cloud_method(env, planned, run, monkeypatch):
    """It is allowed from scripts and from an AI agent session: it only repairs a status in the catalog."""
    spies = _spy_gated(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    _executing(env, None)
    rc, out, err = run("plan", "reset", "1")
    assert rc == 0, err
    assert not any(spies.values())


def test_plan_reset_needs_an_id(run):
    assert run("plan", "reset")[0] == 2


def test_restore_and_purge_need_their_own_confirmation(planned, run, trun, monkeypatch):
    restore = record(monkeypatch, "restore", {"plan_id": 1, "status": "restored", "done": 2, "failed": 0, "skipped": 0, "error": None})
    purge = record(monkeypatch, "purge", {"plan_id": 1, "status": "purged", "done": 2, "failed": 0, "skipped": 0, "error": None})
    prompts = typed(monkeypatch, "restore 2", "restore 1", "execute 1", "purge 1", "purge 1")

    assert trun("plan", "restore", "1")[0] == 1 and not restore        # wrong id
    rc, out, _ = trun("plan", "restore", "1")
    assert rc == 0 and restore[0][0] == (1,) and "restored" in out
    assert 'Type "restore 1"' in prompts[1]

    assert trun("plan", "purge", "1")[0] == 1 and not purge            # "execute 1" is not "purge 1"
    rc, out, _ = trun("plan", "purge", "1", "--older-than-days", "7")
    assert rc == 0 and purge[0] == ((1,), {"older_than_days": 7})
    assert "7 day" in prompts[3] and 'Type "purge 1"' in prompts[3]


def test_confirmation_is_never_read_from_argv_or_env(planned, run, trun, no_input, monkeypatch):
    spies = _spy_gated(monkeypatch)
    monkeypatch.setenv("DRIVEMERGE_CONFIRM", "execute 1")
    rc, _, err = trun("plan", "execute", "1", "--yes")
    assert rc == 2 and "unrecognized arguments" in err
    assert not any(spies.values())


# ------------------------------------------------------------------- errors and misc

def test_missing_config_hints_at_init(tmp_path, capsys):
    rc = cli.main(["--config", str(tmp_path / "nope.toml"), "stats"])
    assert rc == 1 and "drivemerge init" in capsys.readouterr().err
    rc = cli.main(["--config", str(tmp_path / "nope.toml"), "--json", "stats"])
    out = capsys.readouterr().out
    assert rc == 1 and "drivemerge init" in json.loads(out)["error"]


def test_config_path_comes_from_the_environment(env, monkeypatch, capsys):
    monkeypatch.setenv("DRIVEMERGE_CONFIG", str(env.config))
    assert cli.main(["remotes", "--json"]) == 0
    assert {r["name"] for r in json.loads(capsys.readouterr().out)} == {"alpha", "beta"}


def test_invalid_config_is_a_one_line_error(env, capsys):
    env.config.write_text("[safety]\nbogus = 1\n")
    assert cli.main(["--config", str(env.config), "stats"]) == 1
    assert capsys.readouterr().err.startswith("drivemerge: invalid config")
    env.config.write_text("[[remotes]]\nprovider = 'local'\nread = 'x'\n")
    assert cli.main(["--config", str(env.config), "stats"]) == 1
    assert "missing key 'name'" in capsys.readouterr().err


def test_core_errors_become_one_line_messages(run, monkeypatch):
    for exc, text in [(ValueError("bad value"), "bad value"), (plans.PlanError("no such plan"), "no such plan"),
                      (KeyError("unknown remote 'x'"), "unknown remote 'x'"),
                      (RcloneError("rclone lsjson failed (exit 3): directory not found"), "rclone lsjson failed (exit 3): directory not found"),
                      (executor.ExecutionDisabled("execution is off"), "execution is off"),
                      (FileNotFoundError("gone.toml"), "gone.toml")]:
        record(monkeypatch, "stats", raises=exc)
        rc, out, err = run("stats")
        assert rc == 1 and out == "" and err.strip() == f"drivemerge: {text}", (exc, err)


def test_usage_errors_exit_2(run, capsys):
    assert cli.main([]) == 2
    assert run("plan")[0] == 2
    assert run("no-such-command")[0] == 2
    assert cli.main(["--help"]) == 0


# ------------------------------------------------------------------- rclone-combine

def test_rclone_combine_output(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
[database]
path = "{tmp_path / 'c.db'}"
[[remotes]]
name = "gdrive"
provider = "drive"
read = "gdrive-ro:"
write = "gdrive:"
[[remotes]]
name = "dropbox"
provider = "dropbox"
read = "dropbox:"
[[remotes]]
name = "my box"
provider = "local"
read = "/data/with space"
""")
    assert cli.main(["--config", str(cfg), "rclone-combine"]) == 0
    out = capsys.readouterr().out
    assert "[all]\ntype = combine\n" in out
    assert 'upstreams = gdrive=gdrive-ro: dropbox=dropbox: "my box=/data/with space"' in out
    assert "union" in out.splitlines()[0] and out.startswith("#")      # one-line explanation, comment form

    assert cli.main(["--config", str(cfg), "rclone-combine", "--name", "everything"]) == 0
    assert "[everything]" in capsys.readouterr().out
    assert cli.main(["--config", str(cfg), "rclone-combine", "--name", "bad name]"]) == 1
    capsys.readouterr()

    assert cli.main(["--config", str(cfg), "--json", "rclone-combine"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["upstreams"]["gdrive"] == "gdrive-ro:" and data["name"] == "all"


def test_rclone_combine_without_remotes(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[database]\npath = "{tmp_path / "c.db"}"\n')
    assert cli.main(["--config", str(cfg), "rclone-combine"]) == 1
    assert "no remotes" in capsys.readouterr().err


# --------------------------------------------------------------------- serve and mcp

def test_serve_overrides_host_and_port(env, run, monkeypatch):
    pytest.importorskip("fastapi")
    from drivemerge import api
    seen = []
    monkeypatch.setattr(api, "serve", lambda cfg: seen.append((cfg.api.host, cfg.api.port, cfg.api.token)))
    assert run("serve", "--host", "127.0.0.1", "--port", "9999")[0] == 0
    assert seen == [("127.0.0.1", 9999, "test-token")]
    assert run("serve")[0] == 0 and seen[-1][1] == 8765


def test_serve_without_fastapi_is_a_friendly_error(run, monkeypatch):
    import drivemerge
    monkeypatch.delattr(drivemerge, "api", raising=False)
    monkeypatch.setitem(sys.modules, "drivemerge.api", None)
    rc, out, err = run("serve")
    assert rc == 1 and "drive-merge[api]" in err


def test_mcp_passes_the_config_path(env, run, monkeypatch):
    pytest.importorskip("mcp")
    from drivemerge import mcp_server
    seen = []
    monkeypatch.setattr(mcp_server, "main", lambda path: seen.append(path))
    assert run("mcp")[0] == 0
    assert seen == [str(env.config)]


def test_mcp_without_the_sdk_is_a_friendly_error(run, monkeypatch):
    import drivemerge
    monkeypatch.delattr(drivemerge, "mcp_server", raising=False)
    monkeypatch.setitem(sys.modules, "drivemerge.mcp_server", None)
    rc, out, err = run("mcp")
    assert rc == 1 and "drive-merge[mcp]" in err
