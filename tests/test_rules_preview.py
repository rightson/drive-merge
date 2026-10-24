"""The rules preview (rulepreview.py, docs/RULES.md D and 7): exact vs sampled, counts and estimates, filters and
paging, the caches, cancellation, and that it writes nothing."""

from __future__ import annotations

import threading

import pytest

from drivemerge import classify, rulepreview
from drivemerge.config import Config
from drivemerge.rulestore import RulesError
from drivemerge.service import Service

ACTIVE = ('[[rule]]\nname = "photos"\ncategory = ["photo"]\ntarget = "Photos/{name}"\n'
          '[[rule]]\nname = "documents"\ncategory = ["pdf", "document"]\ntarget = "Documents/{name}"\n')
EDITED = ('[[rule]]\nname = "photos"\ncategory = ["photo"]\ntarget = "Photos/{name}"\n'
          '[[rule]]\nname = "receipts"\ncategory = ["pdf"]\nname_regex = "^r_"\ntarget = "Receipts/{name}"\n'
          '[[rule]]\nname = "documents"\ncategory = ["pdf", "document"]\ntarget = "Documents/{name}"\n')


@pytest.fixture
def service(conn, catalog, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_text('[database]\npath = "x.db"\n')
    (cfg / "rules.toml").write_text(ACTIVE)
    from drivemerge.config import load_config
    config = load_config(cfg / "config.toml")
    g = catalog.remote("gd", "drive")
    d = catalog.remote("db", "dropbox")
    for path, size in (("Photos/a.jpg", 10), ("Photos/b.jpg", 20), ("Scans/r_1.pdf", 100), ("Scans/s_2.pdf", 200),
                       ("Documents/s_3.pdf", 300), ("Misc/x.bin", 5)):
        catalog.file(g, path, size=size)
    catalog.file(d, "Photos/a.jpg", size=11)
    svc = Service(config, conn=conn)
    svc.cat = catalog
    svc.ids = {"gd": g, "db": d}
    return svc


def by_name(out):
    return {r["name"]: r for r in out["rules"]}


# --------------------------------------------------------------------------- the numbers, by hand

def test_a_preview_compares_the_edited_rules_with_the_active_ones(service):
    out = service.preview_rules(text=EDITED)
    assert out["preview_id"].startswith("pv_") and out["scope"] == {"remotes": ["gd", "db"], "target_root": "", "path_prefix": None}
    assert (out["files_in_scope"], out["sampled"], out["sample_files"], out["sample_rate"]) == (7, False, 7, 1.0)
    assert out["rules_sha256"] == rulepreview._sha(EDITED) and out["active_sha256"] == rulepreview._sha(ACTIVE)
    rules = by_name(out)
    assert list(rules) == ["photos", "receipts", "documents"]
    assert rules["photos"] == {"name": "photos", "unit": False, "files": 3, "bytes": 41, "units": None, "active_files": 3,
                               "estimate_files": 3, "estimate_bytes": 41, "estimate_active_files": 3}
    assert (rules["receipts"]["files"], rules["receipts"]["bytes"], rules["receipts"]["active_files"],
            rules["receipts"]["estimate_active_files"]) == (1, 100, None, None)
    assert (rules["documents"]["files"], rules["documents"]["active_files"], rules["documents"]["bytes"]) == (2, 3, 500)
    assert out["fallback"] == {"files": 1, "bytes": 5, "estimate_files": 1, "active_files": 1}
    assert out["in_place"] == 4 and out["changed"] == 1 and out["estimate_changed"] == 1
    assert out["shared_targets"] == 1                                             # gd and db both put a.jpg at Photos/a.jpg
    assert out["transitions"] == [{"from": "documents", "to": "receipts", "files": 1, "estimate_files": 1}]
    assert out["elapsed_ms"] >= 0
    (row,) = out["page"]["files"]
    assert out["page"]["filter"] == "changed" and out["page"]["total"] == 1
    assert row["remote"] == "gd" and row["path"] == "Scans/r_1.pdf" and row["size"] == 100
    assert (row["rule"], row["target"], row["active_rule"], row["active_target"], row["in_place"], row["unit"]) == (
        "receipts", "Receipts/r_1.pdf", "documents", "Documents/r_1.pdf", False, None)


def test_previewing_the_active_rules_changes_nothing(service):
    out = service.preview_rules()
    assert out["rules_sha256"] == out["active_sha256"] and out["changed"] == 0 and out["transitions"] == []
    assert out["page"]["files"] == [] and out["page"]["total"] == 0


def test_a_removed_rule_stays_in_the_table_with_what_it_took_before(service):
    only_photos = ACTIVE.split("[[rule]]")[1]
    out = service.preview_rules(text="[[rule]]" + only_photos)
    rules = by_name(out)
    assert rules["documents"]["removed"] is True and rules["documents"]["files"] == 0 and rules["documents"]["active_files"] == 3
    assert out["fallback"]["files"] == 4 and out["fallback"]["active_files"] == 1
    assert out["changed"] == 3                                                     # the three pdfs moved to the fallback
    assert {(t["from"], t["to"], t["files"]) for t in out["transitions"]} == {("documents", "fallback", 3)}


def test_units_are_counted_for_unit_rules(service):
    cat, g = service.cat, service.ids["gd"]
    cat.file(g, "Proj/.git/HEAD", size=1)
    cat.file(g, "Proj/src/a.py", size=2)
    cat.file(g, "Other/.git/HEAD", size=1)
    text = EDITED + '[[rule]]\nname = "projects"\nunit = true\ntarget = "Projects/{unit}/{relpath}"\n'
    out = service.preview_rules(text=text)
    projects = by_name(out)["projects"]
    assert projects["unit"] is True and projects["units"] == 2 and projects["files"] == 3
    assert out["page"]["total"] >= 3
    unit_rows = service.preview_page(out["preview_id"], filter="rule:projects")["files"]
    assert {r["unit"] for r in unit_rows} == {"Proj", "Other"} and unit_rows[0]["rule"] == "projects"


# --------------------------------------------------------------------------- filters and paging

def test_filters_and_paging(service):
    out = service.preview_rules(text=EDITED)
    pid = out["preview_id"]

    def page(**kw):
        return service.preview_page(pid, **kw)

    assert page(filter="all")["total"] == 7
    assert [f["path"] for f in page(filter="fallback")["files"]] == ["Misc/x.bin"]
    assert page(filter="in_place")["total"] == 4 and all(f["in_place"] for f in page(filter="in_place")["files"])
    assert [f["path"] for f in page(filter="rule:photos")["files"]] == ["Photos/a.jpg", "Photos/b.jpg", "Photos/a.jpg"]
    assert page(filter="rule:nope")["total"] == 0
    assert [f["remote"] + ":" + f["path"] for f in page(filter="all", q="DB:")["files"]] == ["db:Photos/a.jpg"]
    assert [f["path"] for f in page(filter="all", q="scans/")["files"]] == ["Scans/r_1.pdf", "Scans/s_2.pdf"]
    first = page(filter="all", limit=3)
    second = page(filter="all", limit=3, offset=3)
    last = page(filter="all", limit=3, offset=6)
    assert [len(p["files"]) for p in (first, second, last)] == [3, 3, 1] and first["total"] == 7
    assert [f["entry_id"] for f in first["files"] + second["files"] + last["files"]] == [
        f["entry_id"] for f in page(filter="all", limit=500)["files"]]
    assert (second["offset"], second["limit"], second["q"], second["filter"]) == (3, 3, None, "all")
    assert page(filter="all", limit=10_000)["limit"] == 500
    with pytest.raises(ValueError):
        page(filter="bogus")
    with pytest.raises(ValueError):
        page(filter="rule:")
    big = service.preview_rules(text=EDITED, filter="all", limit=2)
    assert len(big["page"]["files"]) == 2 and big["page"]["total"] == 7


def test_an_evicted_preview_is_expired(service):
    first = service.preview_rules(text=EDITED)["preview_id"]
    for i in range(rulepreview.PREVIEWS):
        service.preview_rules(text=EDITED + f'[[rule]]\nname = "x{i}"\next = ["zz{i}"]\ntarget = "Z{i}/{{name}}"\n')
    with pytest.raises(RulesError) as exc:
        service.preview_page(first)
    assert (exc.value.code, exc.value.status, exc.value.params) == ("PREVIEW_EXPIRED", 410, {"preview_id": first})
    with pytest.raises(RulesError):
        service.preview_page("pv_nothing")


def test_an_invalid_active_file_leaves_nothing_to_compare_with(service, tmp_path):
    (tmp_path / "cfg" / "rules.toml").write_text('[[rule]]\nname = "a"\n')
    out = service.preview_rules(text=EDITED)
    assert out["changed"] is None and out["estimate_changed"] is None and out["transitions"] == []
    assert out["fallback"]["active_files"] is None and all(r["active_files"] is None for r in out["rules"])
    page = service.preview_page(out["preview_id"], filter="changed")
    assert page["total"] == 7 and page["files"][0]["active_target"] is None            # no baseline: everything is new


# --------------------------------------------------------------------------- scope

def test_scope_remotes_prefix_and_target_root(service):
    out = service.preview_rules(text=EDITED, remotes=["db"])
    assert out["files_in_scope"] == 1 and out["scope"]["remotes"] == ["db"]
    out = service.preview_rules(text=EDITED, path_prefix="Scans", target_root="Archive", filter="all")
    assert out["files_in_scope"] == 2 and out["scope"]["path_prefix"] == "Scans" and out["scope"]["target_root"] == "Archive"
    assert sorted(f["target"] for f in out["page"]["files"]) == ["Archive/Documents/s_2.pdf", "Archive/Receipts/r_1.pdf"]
    with pytest.raises(KeyError):
        service.preview_rules(text=EDITED, remotes=["nope"])
    with pytest.raises(classify.RuleError) as exc:
        service.preview_rules(text=EDITED, target_root="a/../b")
    assert exc.value.code == "TARGET_ROOT_DOT"


def test_invalid_text_or_both_sources_are_refused(service):
    with pytest.raises(classify.RuleError) as exc:
        service.preview_rules(text='[[rule]]\nname = "a"\ntarget = "{yeer}"\n')
    assert exc.value.code == "UNKNOWN_VARIABLE"
    with pytest.raises(RulesError) as both:
        service.preview_rules(text=EDITED, version_id=1)
    assert both.value.code == "RULES_BOTH" and both.value.status == 400
    with pytest.raises(RulesError) as missing:
        service.preview_rules(version_id=99)
    assert missing.value.code == "RULES_VERSION_NOT_FOUND"
    with pytest.raises(ValueError):
        service.preview_rules(text=EDITED, mode="fast")


def test_a_version_can_be_previewed_by_id(service):
    v = service.save_rule_version(EDITED, created_by="mcp", title="t")
    out = service.preview_rules(version_id=v["id"])
    assert out["rules_sha256"] == v["sha256"] and out["changed"] == 1


# --------------------------------------------------------------------------- exact or sampled

@pytest.fixture
def many(conn, catalog, service, monkeypatch):
    monkeypatch.setattr(rulepreview, "EXACT_LIMIT", 1000)
    monkeypatch.setattr(rulepreview, "SAMPLE_SIZE", 300)
    g = service.ids["gd"]
    for i in range(1500):
        catalog.file(g, f"Bulk/{i % 9}/{'r_' if i % 5 == 0 else 's_'}{i:04d}.pdf", size=1)
    return service


def test_above_the_limit_a_deterministic_sample_is_placed_and_counts_are_scaled(many):
    out = many.preview_rules(text=EDITED, filter="all", limit=500)
    assert out["files_in_scope"] == 1507 and out["sampled"] is True
    assert 220 <= out["sample_files"] <= 380 and out["sample_rate"] == round(out["sample_files"] / 1507, 4)
    rules = by_name(out)
    assert rules["documents"]["files"] + rules["receipts"]["files"] + rules["photos"]["files"] + out["fallback"]["files"] == out["sample_files"]
    scale = 1507 / out["sample_files"]
    assert rules["receipts"]["estimate_files"] == round(rules["receipts"]["files"] * scale)
    est_total = sum(r["estimate_files"] for r in out["rules"]) + out["fallback"]["estimate_files"]
    assert abs(est_total - 1507) <= len(out["rules"]) + 1                               # rounding only
    assert out["estimate_changed"] == round(out["changed"] * scale) and out["estimate_changed"] > 1
    assert out["estimate_in_place"] == round(out["in_place"] * scale)
    ids = {f["entry_id"] for f in out["page"]["files"]}
    other = many.preview_rules(text=EDITED.replace("Receipts", "Rcpts"), filter="all", limit=500)
    assert {f["entry_id"] for f in other["page"]["files"]} == ids and other["preview_id"] != out["preview_id"]


def test_exact_mode_places_every_file_up_to_the_cap(many, monkeypatch):
    out = many.preview_rules(text=EDITED, mode="exact")
    assert out["sampled"] is False and out["sample_files"] == out["files_in_scope"] == 1507 and out["sample_rate"] == 1.0
    assert by_name(out)["receipts"]["files"] == by_name(out)["receipts"]["estimate_files"] > 300
    monkeypatch.setattr(rulepreview, "EXACT_MAX", 1000)
    with pytest.raises(RulesError) as exc:
        many.preview_rules(text=EDITED, mode="exact")
    assert (exc.value.code, exc.value.status, exc.value.params) == ("PREVIEW_TOO_LARGE", 409, {"files": 1507, "limit": 1000})


def test_a_small_scope_is_exact_even_in_auto_mode(many):
    out = many.preview_rules(text=EDITED, path_prefix="Scans")
    assert out["sampled"] is False and out["files_in_scope"] == 2


def test_the_threshold_keeps_about_the_sample_size():
    assert rulepreview._threshold(1_000_000) == round(rulepreview.SAMPLE_SIZE / 1_000_000 * classify.SAMPLE_MODULUS)
    assert rulepreview._threshold(10) == classify.SAMPLE_MODULUS


# --------------------------------------------------------------------------- caches

def test_the_same_preview_twice_is_served_from_the_cache(service):
    a = service.preview_rules(text=EDITED)
    b = service.preview_rules(text=EDITED, filter="all", limit=3)
    assert a["preview_id"] == b["preview_id"] and len(b["page"]["files"]) == 3 and b["page"]["filter"] == "all"
    assert service.previewer.stats["preview_hits"] == 1 and service.previewer.stats["preview_misses"] == 1


def test_units_are_not_recomputed_when_only_targets_change(service):
    stats = service.previewer.stats
    service.preview_rules(text=EDITED)
    misses = stats["units_misses"]
    assert misses == 2                                                   # one per remote; edited and active share the markers
    service.preview_rules(text=EDITED.replace("Receipts/", "Rcpts/"))
    service.preview_rules(text=EDITED.replace('"^r_"', '"^q_"'))
    assert stats["units_misses"] == misses and stats["units_hits"] >= 4


def test_units_are_recomputed_when_the_markers_or_the_catalog_change(service):
    stats = service.previewer.stats
    service.preview_rules(text=EDITED)
    base = stats["units_misses"]
    service.preview_rules(text='[layout]\nunit_markers = [".git"]\n' + EDITED)
    assert stats["units_misses"] == base + 2
    service.preview_rules(text='[layout]\nbundle_patterns = ["*.app"]\n' + EDITED)
    assert stats["units_misses"] == base + 4
    service.cat.file(service.ids["gd"], "New/f.jpg", size=1)                                   # the catalog changed
    service.preview_rules(text=EDITED)
    assert stats["units_misses"] == base + 6


def test_the_active_baseline_is_cached_across_keystrokes(service):
    stats = service.previewer.stats
    service.preview_rules(text=EDITED)
    assert stats["baseline_misses"] == 1
    service.preview_rules(text=EDITED.replace("Receipts/", "R/"))
    service.preview_rules(text=EDITED.replace("Receipts/", "S/"))
    assert stats["baseline_misses"] == 1 and stats["baseline_hits"] == 2
    service.preview_rules(text=EDITED, path_prefix="Scans")                                   # another scope: a new baseline
    assert stats["baseline_misses"] == 2


def test_a_rescan_invalidates_cached_previews(service):
    a = service.preview_rules(text=EDITED)
    conn = service.conn
    conn.execute("INSERT INTO scans(remote_id, status, started_at, finished_at) VALUES (?, 'complete', 'x', 'x')",
                 (service.ids["gd"],))
    b = service.preview_rules(text=EDITED)
    assert a["preview_id"] != b["preview_id"] and service.previewer.stats["preview_misses"] == 2


# --------------------------------------------------------------------------- read-only and cancellable

def test_a_preview_writes_nothing(service):
    before = service.conn.total_changes
    service.preview_rules(text=EDITED)
    service.preview_rules(text=EDITED, mode="exact", filter="all")
    service.explain_layout(text=EDITED, remote="gd", path="Scans/r_1.pdf")
    assert service.conn.total_changes == before and not service.conn.in_transaction
    assert service.conn.execute("SELECT COUNT(*) FROM rule_versions").fetchone()[0] == 0


def test_a_newer_preview_stops_the_running_one(many, monkeypatch):
    real = classify.plan_layout
    first_in = threading.Event()
    outcome: dict = {}

    def slow(conn, rs, **kw):
        if not first_in.is_set():
            first_in.set()
            deadline = threading.Event()
            while many.previewer._gen < 2 and not deadline.wait(0.01):          # wait until the second request arrives
                pass
        return real(conn, rs, **kw)

    monkeypatch.setattr(classify, "plan_layout", slow)

    def run_first():
        try:
            outcome["first"] = many.preview_rules(text=EDITED)
        except RulesError as exc:
            outcome["first"] = exc

    t = threading.Thread(target=run_first)
    t.start()
    assert first_in.wait(5)
    outcome["second"] = many.preview_rules(text=EDITED.replace("Receipts/", "R/"))
    t.join(10)
    assert isinstance(outcome["first"], RulesError) and outcome["first"].code == "PREVIEW_SUPERSEDED"
    assert outcome["first"].status == 409
    assert outcome["second"]["sampled"] is True and many.previewer._running is None
    assert not many.conn.in_transaction


def test_a_waiting_preview_that_was_overtaken_never_runs(many, monkeypatch):
    p = many.previewer
    with p._run:                                          # a preview is "running"
        results = {}

        def ask(name, text):
            try:
                results[name] = many.preview_rules(text=text)
            except RulesError as exc:
                results[name] = exc

        t1 = threading.Thread(target=ask, args=("old", EDITED))
        t1.start()
        while p._gen < 1:
            threading.Event().wait(0.005)
        t2 = threading.Thread(target=ask, args=("new", EDITED.replace("Receipts/", "R/")))
        t2.start()
        while p._gen < 2:
            threading.Event().wait(0.005)
    t1.join(10)
    t2.join(10)
    assert isinstance(results["old"], RulesError) and results["old"].code == "PREVIEW_SUPERSEDED"
    assert results["new"]["files_in_scope"] == 1507


def test_an_interrupted_statement_of_a_superseded_preview_is_a_superseded_answer(many, monkeypatch):
    import sqlite3
    p = many.previewer
    real = p._preview

    def interrupted(conn, mine, *a, **k):
        p._gen += 1                                         # a newer request arrived meanwhile
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(p, "_preview", interrupted)
    with pytest.raises(RulesError) as exc:
        many.preview_rules(text=EDITED)
    assert exc.value.code == "PREVIEW_SUPERSEDED"
    monkeypatch.setattr(p, "_preview", real)
    assert many.preview_rules(text=EDITED)["sampled"] is True


def test_explain_runs_under_the_edited_and_the_active_rules(service):
    out = service.explain_layout(text=EDITED, remote="gd", path="Scans/r_1.pdf")
    assert out["edited"]["rule"] == "receipts" and out["edited"]["target"] == "Receipts/r_1.pdf"
    assert out["active"]["rule"] == "documents" and out["active"]["target"] == "Documents/r_1.pdf"
    assert [s["rule"] for s in out["edited"]["steps"]] == ["photos", "receipts", "documents"]
    with pytest.raises(KeyError):
        service.explain_layout(text=EDITED, remote="gd", path="nope.txt")
    with pytest.raises(KeyError):
        service.explain_layout(text=EDITED, remote="nope", path="x")
