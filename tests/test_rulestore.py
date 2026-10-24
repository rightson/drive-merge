"""The rules' history and the active file (rulestore.py, docs/RULES.md C): resolution, observing outside edits,
versions, drafts, proposals, activation with compare-and-swap, undo, diff and rule changes."""

from __future__ import annotations

import json
import os
import stat
import sqlite3

import pytest

from drivemerge import classify, db, rulestore
from drivemerge.config import Config, load_config
from drivemerge.rulestore import RulesError

A = '[[rule]]\nname = "photos"\ncategory = ["photo"]\ntarget = "Photos/{year}/{name}"\n'
B = A + '[[rule]]\nname = "docs"\ncategory = ["pdf"]\ntarget = "Docs/{name}"\n'
C = '[[rule]]\nname = "docs"\ncategory = ["pdf"]\ntarget = "Docs/{name}"\n' + A
CONFIG_TOML = '[database]\npath = "{db}"\n[api]\ntoken = "t"\n'


@pytest.fixture
def home(tmp_path):
    """A config directory with a real config.toml, and a config loaded from it (rules next to it are drive-merge's own)."""
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "config.toml").write_text(CONFIG_TOML.format(db=tmp_path / "c.db"))
    return d


@pytest.fixture
def config(home):
    return load_config(home / "config.toml")


def _config_with_path(home, path):
    (home / "config.toml").write_text(CONFIG_TOML.format(db=home / "c.db") + f'[rules]\npath = "{path}"\n')
    return load_config(home / "config.toml")


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM rule_versions ORDER BY id")]


def sha(text):
    return rulestore.sha256_of(text)


# --------------------------------------------------------------------------- which file is the active one

def test_the_rules_file_is_the_configured_path_else_rules_toml_next_to_the_config(tmp_path, home):
    configured = _config_with_path(home, "~/x/layout.toml")
    assert configured.rules_file == __import__("pathlib").Path("~/x/layout.toml").expanduser()
    assert configured.rules_owned is False
    owned = load_config(_write_plain(home))
    assert owned.rules_file == home / "rules.toml" and owned.rules_owned is True
    assert Config().rules_file is None and Config().rules_owned is True


def _write_plain(home):
    (home / "config.toml").write_text(CONFIG_TOML.format(db=home / "c.db"))
    return home / "config.toml"


def test_no_file_means_the_packaged_defaults(conn, config):
    a = rulestore.active(conn, config)
    assert (a.source, a.path, a.owned, a.error) == ("default", None, True, None)
    assert a.text == classify.default_rules_text() and a.sha256 == sha(a.text) and a.version_id is None


def test_an_owned_rules_toml_next_to_the_config_is_picked_up_without_a_rules_path(conn, config, home):
    (home / "rules.toml").write_text(A)
    a = rulestore.active(conn, config)
    assert (a.source, a.path, a.owned, a.text) == ("file", str(home / "rules.toml"), True, A)
    assert [r.name for r in classify.load_rules(a.text).rules] == ["photos"]


def test_a_configured_path_wins_over_the_owned_file(conn, home):
    (home / "rules.toml").write_text(B)
    mine = home / "mine.toml"
    mine.write_text(A)
    a = rulestore.active(conn, _config_with_path(home, mine))
    assert a.path == str(mine) and a.text == A and a.owned is False


def test_a_configured_path_whose_file_is_missing_is_an_error_not_the_defaults(conn, home):
    config = _config_with_path(home, home / "gone.toml")
    a = rulestore.active(conn, config)
    assert a.error == {"code": "RULES_FILE_MISSING", "params": {"path": str(home / "gone.toml")}}
    assert a.source == "file" and a.text == ""
    assert rulestore.observe(conn, config).error["code"] == "RULES_FILE_MISSING"
    assert _rows(conn) == []                                           # nothing to observe


def test_an_unreadable_or_unparsable_file_is_reported_in_error(conn, home):
    bad = home / "bad.toml"
    bad.write_bytes(b"\xff\xfe not utf8")
    assert rulestore.active(conn, _config_with_path(home, bad)).error["code"] == "RULES_FILE_UNREADABLE"
    bad.write_text("[[rule]]\nname = 'a'\n")
    err = rulestore.active(conn, _config_with_path(home, bad)).error
    assert err["code"] == "TARGET_REQUIRED" and err["params"]["rule"] == "a"


def test_a_config_built_in_memory_never_reads_a_file_in_the_home_directory(conn, tmp_path, monkeypatch):
    fake_home = tmp_path / "fakehome"
    (fake_home / ".config" / "drive-merge").mkdir(parents=True)
    (fake_home / ".config" / "drive-merge" / "rules.toml").write_text(A)
    monkeypatch.setenv("HOME", str(fake_home))
    a = rulestore.active(conn, Config())
    assert a.source == "default" and a.text == classify.default_rules_text()
    with pytest.raises(RulesError) as exc:
        rulestore.activate(conn, Config(), rulestore.create_version(conn, A, created_by="mcp")["id"],
                           base_sha256=a.sha256, activated_by="t")
    assert exc.value.code == "RULES_NO_FILE" and exc.value.status == 409


# --------------------------------------------------------------------------- observe

def test_observing_the_defaults_records_a_default_version_once(conn, config):
    a = rulestore.observe(conn, config)
    (row,) = _rows(conn)
    assert (row["status"], row["created_by"], row["sha256"]) == ("active", "default", a.sha256) and a.version_id == row["id"]
    rulestore.observe(conn, config)
    assert len(_rows(conn)) == 1


def test_an_outside_edit_is_recorded_once_and_the_old_text_is_retired(conn, config, home):
    first = rulestore.observe(conn, config)
    (home / "rules.toml").write_text(A)
    a = rulestore.observe(conn, config)
    again = rulestore.observe(conn, config)
    assert a.version_id == again.version_id != first.version_id
    rows = _rows(conn)
    assert [(r["status"], r["created_by"]) for r in rows] == [("retired", "default"), ("active", "file")]
    assert rows[1]["text"] == A and rows[1]["sha256"] == sha(A) and rows[1]["ok"] == 1 and rows[1]["activated_by"] == "file"
    (j,) = [json.loads(r["detail"]) for r in conn.execute("SELECT detail FROM journal WHERE action = 'rules_observe'")
            if json.loads(r["detail"])["sha256"] == sha(A)]
    assert j["by"] == "file" and j["path"] == str(home / "rules.toml")


def test_active_never_writes(conn, config, home):
    (home / "rules.toml").write_text(A)
    before = conn.total_changes
    rulestore.active(conn, config)
    assert conn.total_changes == before and _rows(conn) == []


def test_an_outside_edit_back_to_a_known_text_reuses_that_version(conn, config, home):
    (home / "rules.toml").write_text(A)
    one = rulestore.observe(conn, config)
    (home / "rules.toml").write_text(B)
    two = rulestore.observe(conn, config)
    (home / "rules.toml").write_text(A)
    back = rulestore.observe(conn, config)
    assert back.version_id == one.version_id != two.version_id
    assert [r["status"] for r in _rows(conn)] == ["active", "retired"] and len(_rows(conn)) == 2


def test_an_outside_edit_that_matches_a_proposal_makes_that_proposal_active(conn, config, home):
    prop = rulestore.create_version(conn, B, created_by="mcp", title="t")
    (home / "rules.toml").write_text(B)
    a = rulestore.observe(conn, config)
    assert a.version_id == prop["id"]
    assert rulestore.get_version(conn, prop["id"])["status"] == "active"


def test_an_outside_edit_matching_a_draft_or_a_dismissed_text_gets_its_own_version(conn, config, home):
    draft = rulestore.create_version(conn, A, created_by="ui")
    (home / "rules.toml").write_text(A)
    a = rulestore.observe(conn, config)
    assert a.version_id != draft["id"] and rulestore.get_version(conn, draft["id"])["status"] == "draft"


def test_an_invalid_file_is_still_recorded_with_ok_false(conn, config, home):
    (home / "rules.toml").write_text("[[rule]]\nname = 'a'\n")
    a = rulestore.observe(conn, config)
    row = rulestore.get_version(conn, a.version_id)
    assert row["ok"] is False and row["created_by"] == "file" and a.error["code"] == "TARGET_REQUIRED"


# --------------------------------------------------------------------------- versions

def test_a_draft_may_have_errors_and_a_proposal_may_not(conn):
    d = rulestore.create_version(conn, "[[rule]]\nname = 'a'\n", created_by="ui", title="wip", note="n")
    assert (d["status"], d["ok"], d["revision"], d["created_by"], d["title"], d["note"]) == ("draft", False, 0, "ui", "wip", "n")
    for who in ("mcp", "api"):
        p = rulestore.create_version(conn, A, created_by=who, title="x")
        assert (p["status"], p["ok"], p["created_by"]) == ("proposed", True, who)
    with pytest.raises(RulesError) as exc:
        rulestore.create_version(conn, "[[rule]]\nname = 'a'\ntarget = '{yeer}'\n", created_by="mcp")
    assert exc.value.code == "RULES_INVALID" and exc.value.status == 400
    (first,) = exc.value.params["errors"]
    assert set(first) == {"code", "params"} and first["code"] == "UNKNOWN_VARIABLE"
    assert len(_rows(conn)) == 3                                                   # nothing stored for the invalid one


def test_invalid_errors_are_capped_at_ten(conn):
    text = "".join('[[rule]]\nname = "r%d"\n' % i for i in range(30))
    with pytest.raises(RulesError) as exc:
        rulestore.create_version(conn, text, created_by="api")
    assert len(exc.value.params["errors"]) == 10


def test_text_limit_and_field_checks(conn):
    with pytest.raises(classify.RuleError) as exc:
        rulestore.create_version(conn, "#" * (257 * 1024), created_by="ui")
    assert exc.value.code == "RULES_TOO_LARGE"
    with pytest.raises(ValueError):
        rulestore.create_version(conn, A, created_by="robot")
    with pytest.raises(ValueError):
        rulestore.create_version(conn, A, created_by="ui", title="x" * 201)
    with pytest.raises(ValueError):
        rulestore.create_version(conn, A, created_by="ui", note="x" * 2001)
    with pytest.raises(RulesError) as nf:
        rulestore.create_version(conn, A, created_by="ui", base_id=99)
    assert nf.value.code == "RULES_VERSION_NOT_FOUND" and nf.value.status == 404 and nf.value.params == {"version": 99}


def test_drafts_bump_their_revision_and_refuse_a_stale_save(conn):
    d = rulestore.create_version(conn, A, created_by="ui")
    d1 = rulestore.update_draft(conn, d["id"], B, revision=0, title="T")
    assert (d1["revision"], d1["text"], d1["sha256"], d1["title"]) == (1, B, sha(B), "T")
    assert rulestore.update_draft(conn, d["id"], B, revision=1)["title"] == "T"        # None keeps the title
    with pytest.raises(RulesError) as exc:
        rulestore.update_draft(conn, d["id"], A, revision=0)
    assert exc.value.code == "RULES_DRAFT_CHANGED" and exc.value.status == 409
    assert exc.value.params == {"version": d["id"], "revision": 2}
    bad = rulestore.update_draft(conn, d["id"], "[[rule]]\n", revision=2)
    assert bad["ok"] is False


def test_only_a_draft_can_be_edited(conn):
    p = rulestore.create_version(conn, A, created_by="mcp")
    with pytest.raises(RulesError) as exc:
        rulestore.update_draft(conn, p["id"], B, revision=0)
    assert exc.value.code == "RULES_NOT_DRAFT" and exc.value.params == {"version": p["id"], "version_status": "proposed"}
    with pytest.raises(RulesError) as nf:
        rulestore.update_draft(conn, 404, B, revision=0)
    assert nf.value.code == "RULES_VERSION_NOT_FOUND"


def test_discard_deletes_a_draft_and_dismisses_a_proposal(conn):
    base = rulestore.create_version(conn, A, created_by="ui")
    child = rulestore.create_version(conn, B, created_by="ui", base_id=base["id"])
    rulestore.discard(conn, base["id"], by="ui:me")
    assert [r["id"] for r in _rows(conn)] == [child["id"]] and rulestore.get_version(conn, child["id"])["base_id"] is None
    p = rulestore.create_version(conn, A, created_by="mcp")
    assert rulestore.discard(conn, p["id"], by="ui:me")["status"] == "dismissed"
    assert rulestore.discard(conn, p["id"], by="ui:me")["status"] == "dismissed"          # idempotent
    assert rulestore.get_version(conn, p["id"])["status"] == "dismissed" and rulestore.count_proposals(conn) == 0
    assert any(json.loads(r[0])["version"] == p["id"] for r in conn.execute("SELECT detail FROM journal WHERE action = 'rules_dismiss'"))


def test_active_and_retired_versions_cannot_be_discarded(conn, config):
    a = rulestore.observe(conn, config)
    with pytest.raises(RulesError) as exc:
        rulestore.discard(conn, a.version_id, by="ui")
    assert exc.value.code == "RULES_NOT_DRAFT" and exc.value.params["version_status"] == "active"


def test_the_database_allows_one_active_row(conn, config):
    rulestore.observe(conn, config)
    now = db.utcnow()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO rule_versions(status, text, sha256, ok, created_by, created_at, updated_at) "
                     "VALUES ('active', 'x', 'y', 1, 'ui', ?, ?)", (now, now))


def test_list_versions_filters_and_orders_newest_first(conn):
    ids = [rulestore.create_version(conn, A, created_by="ui")["id"] for _ in range(3)]
    rulestore.update_draft(conn, ids[0], B, revision=0)
    rulestore.create_version(conn, B, created_by="mcp")
    assert [v["id"] for v in rulestore.list_versions(conn, status="draft")] == [ids[0], ids[2], ids[1]]
    assert [v["status"] for v in rulestore.list_versions(conn, status="proposed")] == ["proposed"]
    assert len(rulestore.list_versions(conn, limit=2)) == 2
    with pytest.raises(ValueError):
        rulestore.list_versions(conn, status="bogus")


# --------------------------------------------------------------------------- activation

def test_activation_writes_the_exact_text_and_swaps_the_history(conn, config, home):
    (home / "rules.toml").write_text(A)
    before = rulestore.observe(conn, config)
    config_bytes = (home / "config.toml").read_bytes()
    v = rulestore.create_version(conn, C.replace("\n", "\r\n"), created_by="mcp", title="crlf")
    after = rulestore.activate(conn, config, v["id"], base_sha256=before.sha256, activated_by="ui:me")
    assert (home / "rules.toml").read_bytes() == C.replace("\n", "\r\n").encode()          # exact bytes, no translation
    assert after.version_id == v["id"] and after.sha256 == v["sha256"] and after.source == "file" and after.error is None
    old, new = rulestore.get_version(conn, before.version_id), rulestore.get_version(conn, v["id"])
    assert (old["status"], new["status"], new["activated_by"]) == ("retired", "active", "ui:me") and new["activated_at"]
    (row,) = [r for r in conn.execute("SELECT detail FROM journal WHERE action = 'rules_activate'")]
    assert json.loads(row[0]) == {"version": v["id"], "sha256": v["sha256"], "path": str(home / "rules.toml"), "by": "ui:me"}
    assert (home / "config.toml").read_bytes() == config_bytes                              # never edited
    assert not [p for p in home.iterdir() if p.name.startswith(".rules.toml.")]             # no temp file left


def test_a_stale_base_is_refused_with_the_current_state(conn, config, home):
    (home / "rules.toml").write_text(A)
    seen = rulestore.observe(conn, config)
    (home / "rules.toml").write_text(B)                                  # someone edits the file after the review
    v = rulestore.create_version(conn, C, created_by="api")
    with pytest.raises(RulesError) as exc:
        rulestore.activate(conn, config, v["id"], base_sha256=seen.sha256, activated_by="ui")
    assert exc.value.code == "RULES_ACTIVE_CHANGED" and exc.value.status == 409
    assert exc.value.params["active_sha256"] == sha(B)
    assert exc.value.params["active_version"] == rulestore.active(conn, config).version_id      # the outside edit was observed
    assert (home / "rules.toml").read_text() == B and rulestore.get_version(conn, v["id"])["status"] == "proposed"


def test_refusals_come_in_the_documented_order(conn, config, home):
    cur = rulestore.observe(conn, config)
    with pytest.raises(RulesError) as e1:
        rulestore.activate(conn, config, 99, base_sha256="x", activated_by="u")
    assert e1.value.code == "RULES_VERSION_NOT_FOUND"
    dismissed = rulestore.create_version(conn, A, created_by="mcp")
    rulestore.discard(conn, dismissed["id"], by="u")
    with pytest.raises(RulesError) as e2:
        rulestore.activate(conn, config, dismissed["id"], base_sha256="x", activated_by="u")
    assert e2.value.code == "RULES_NOT_ACTIVATABLE" and e2.value.params["version_status"] == "dismissed"
    bad = rulestore.create_version(conn, "[[rule]]\nname = 'a'\n", created_by="ui")
    with pytest.raises(RulesError) as e3:
        rulestore.activate(conn, config, bad["id"], base_sha256="x", activated_by="u")      # before the base is compared
    assert e3.value.code == "RULES_INVALID" and e3.value.params["errors"][0]["code"] == "TARGET_REQUIRED"
    good = rulestore.create_version(conn, A, created_by="ui")
    with pytest.raises(RulesError) as e4:
        rulestore.activate(conn, config, good["id"], base_sha256="stale", activated_by="u")
    assert e4.value.code == "RULES_ACTIVE_CHANGED" and e4.value.params["active_sha256"] == cur.sha256


def test_a_new_owned_file_is_created_private_with_its_directory(conn, tmp_path):
    cfg_dir = tmp_path / "fresh" / "cfg"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(CONFIG_TOML.format(db=tmp_path / "c.db"))
    config = load_config(cfg_dir / "config.toml")
    a = rulestore.observe(conn, config)
    v = rulestore.create_version(conn, A, created_by="mcp")
    rulestore.activate(conn, config, v["id"], base_sha256=a.sha256, activated_by="u")
    assert stat.S_IMODE((cfg_dir / "rules.toml").stat().st_mode) == 0o600
    # the owned file's parent directory is created when it is missing
    gone = tmp_path / "later"
    cfg2 = load_config(_cfg(gone, tmp_path))
    rulestore.activate(conn, cfg2, v["id"], base_sha256=rulestore.active(conn, cfg2).sha256, activated_by="u")
    assert (gone / "rules.toml").read_text() == A


def _cfg(d, tmp_path):
    d.mkdir()
    (d / "config.toml").write_text(CONFIG_TOML.format(db=tmp_path / "x.db"))
    path = d / "config.toml"
    for f in d.glob("rules.toml"):
        f.unlink()
    return path


def test_an_existing_file_keeps_its_mode_and_a_symlink_is_written_through(conn, config, home, tmp_path):
    real = tmp_path / "dotfiles" / "layout.toml"
    real.parent.mkdir()
    real.write_text(A)
    real.chmod(0o640)
    (home / "rules.toml").symlink_to(real)
    a = rulestore.observe(conn, config)
    v = rulestore.create_version(conn, B, created_by="mcp")
    rulestore.activate(conn, config, v["id"], base_sha256=a.sha256, activated_by="u")
    assert (home / "rules.toml").is_symlink() and os.path.realpath(home / "rules.toml") == str(real)
    assert real.read_text() == B and stat.S_IMODE(real.stat().st_mode) == 0o640


def test_a_configured_file_is_written_where_it_is_but_its_directory_is_not_created(conn, home, tmp_path):
    mine = tmp_path / "elsewhere" / "layout.toml"
    config = _config_with_path(home, mine)
    v = rulestore.create_version(conn, A, created_by="mcp")
    with pytest.raises(RulesError) as exc:                       # missing configured file: base is the empty text
        rulestore.activate(conn, config, v["id"], base_sha256=rulestore.active(conn, config).sha256, activated_by="u")
    assert exc.value.code == "RULES_FILE_UNWRITABLE" and exc.value.params == {"path": str(mine)}
    mine.parent.mkdir()
    rulestore.activate(conn, config, v["id"], base_sha256=rulestore.active(conn, config).sha256, activated_by="u")
    assert mine.read_text() == A and rulestore.active(conn, config).error is None      # activating repairs the missing file


def test_an_unwritable_file_is_reported_and_changes_nothing(conn, config, home, monkeypatch):
    (home / "rules.toml").write_text(A)
    a = rulestore.observe(conn, config)
    v = rulestore.create_version(conn, B, created_by="mcp")

    def boom(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RulesError) as exc:
        rulestore.activate(conn, config, v["id"], base_sha256=a.sha256, activated_by="u")
    assert exc.value.code == "RULES_FILE_UNWRITABLE" and exc.value.params == {"path": str(home / "rules.toml")}
    assert (home / "rules.toml").read_text() == A and rulestore.get_version(conn, v["id"])["status"] == "proposed"
    assert rulestore.get_version(conn, a.version_id)["status"] == "active"
    assert not [p for p in home.iterdir() if p.name.startswith(".rules.toml.")]
    assert not conn.execute("SELECT 1 FROM journal WHERE action = 'rules_activate'").fetchone()


def test_undo_is_activating_the_previous_version(conn, config, home):
    (home / "rules.toml").write_text(A)
    one = rulestore.observe(conn, config)
    v = rulestore.create_version(conn, B, created_by="mcp")
    two = rulestore.activate(conn, config, v["id"], base_sha256=one.sha256, activated_by="u")
    back = rulestore.activate(conn, config, one.version_id, base_sha256=two.sha256, activated_by="u")
    assert (home / "rules.toml").read_text() == A and back.version_id == one.version_id
    assert [r["status"] for r in _rows(conn)] == ["active", "retired"]
    assert rulestore.get_version(conn, one.version_id)["activated_by"] == "u"


def test_activating_the_packaged_defaults_is_activating_a_version_made_from_them(conn, config, home):
    (home / "rules.toml").write_text(A)
    cur = rulestore.observe(conn, config)
    default = rulestore.create_version(conn, classify.default_rules_text(), created_by="default")
    assert default["status"] == "proposed" and default["created_by"] == "default"
    rulestore.activate(conn, config, default["id"], base_sha256=cur.sha256, activated_by="u")
    assert (home / "rules.toml").read_text() == classify.default_rules_text()


def test_activating_a_draft_makes_it_immutable(conn, config, home):
    a = rulestore.observe(conn, config)
    d = rulestore.create_version(conn, A, created_by="ui")
    rulestore.activate(conn, config, d["id"], base_sha256=a.sha256, activated_by="u")
    with pytest.raises(RulesError) as exc:
        rulestore.update_draft(conn, d["id"], B, revision=0)
    assert exc.value.code == "RULES_NOT_DRAFT" and exc.value.params["version_status"] == "active"


# --------------------------------------------------------------------------- diff and rule_changes

def test_diff_has_three_lines_of_context_and_counts():
    old = "".join(f"line {i}\n" for i in range(1, 21))
    new = old.replace("line 10\n", "LINE 10\n").replace("line 18\n", "")
    d = rulestore.diff(old, new)
    assert (d["added"], d["removed"]) == (1, 2)
    assert [h["old_start"] for h in d["hunks"]] == [7, 15] and [h["new_start"] for h in d["hunks"]] == [7, 15]
    first = d["hunks"][0]["lines"]
    assert first == [{"op": " ", "text": f"line {i}"} for i in (7, 8, 9)] + [
        {"op": "-", "text": "line 10"}, {"op": "+", "text": "LINE 10"}] + [{"op": " ", "text": f"line {i}"} for i in (11, 12, 13)]
    assert rulestore.diff(old, old) == {"hunks": [], "added": 0, "removed": 0}
    assert rulestore.diff("", "a\n") == {"hunks": [{"old_start": 1, "new_start": 1, "lines": [{"op": "+", "text": "a"}]}],
                                         "added": 1, "removed": 0}


def test_rule_changes_by_name():
    load = classify.load_rules
    old = load(B + '[[rule]]\nname = "gone"\ntarget = "G"\n')
    new = load(C.replace('"Docs/{name}"', '"Documents/{name}"') + '[[rule]]\nname = "fresh"\ntarget = "F"\n')
    assert rulestore.rule_changes(old, new) == [
        {"name": "docs", "change": "changed"}, {"name": "fresh", "change": "added"}, {"name": "gone", "change": "removed"}]
    assert rulestore.rule_changes(load(B), load(C)) == [{"name": "docs", "change": "moved"}]     # swapped: the later one moved
    assert rulestore.rule_changes(load(B), load(B)) == []
    three = load(A + '[[rule]]\nname = "x"\ntarget = "X"\n[[rule]]\nname = "y"\ntarget = "Y"\n[[rule]]\nname = "z"\ntarget = "Z"\n')
    moved = load('[[rule]]\nname = "z"\ntarget = "Z"\n' + A + '[[rule]]\nname = "x"\ntarget = "X"\n[[rule]]\nname = "y"\ntarget = "Y"\n')
    assert rulestore.rule_changes(three, moved) == [{"name": "z", "change": "moved"}]
