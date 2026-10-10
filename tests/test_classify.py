from __future__ import annotations

import unicodedata

import pytest

from drivemerge.classify import (
    DEFAULT_BUNDLE_PATTERNS, DEFAULT_FALLBACK, DEFAULT_UNIT_MARKERS, RuleError, date_from_name,
    default_rules, detect_units, folder_profiles, load_rules, plan_layout, render,
)

PHOTO_RULE = '[[rule]]\nname = "photos"\ncategory = ["photo"]\ntarget = "Photos/{year}/{name}"\n'
PROJECT_RULE = '[[rule]]\nname = "projects"\nunit = true\ntarget = "Projects/{unit}/{relpath}"\n'


def _placed(conn, ruleset, **kw):
    """{src_path: Placement} for single-remote tests."""
    return {p.src_path: p for p in plan_layout(conn, ruleset, **kw)}


# --------------------------------------------------------------------------- rule validation

@pytest.mark.parametrize("text, needles", [
    ('[[rule]]\nname = "a"\ntarget = "x"\ncatgory = ["photo"]\n', ["unknown key 'catgory'", "did you mean 'category'"]),
    ('[[rule]]\nname = "a"\ntarget = "X/{yeer}/{name}"\n', ["unknown template variable {yeer}", "did you mean 'year'", "valid variables"]),
    ('[[rule]]\nname = "a"\ntarget = "X/{}/{name}"\n', ["unknown template variable"]),
    ('[[rule]]\nname = "a"\ntarget = "X/{name.upper}"\n', ["unknown template variable"]),
    ('[[rule]]\nname = "a"\ntarget = "X/{year:>5}"\n', ["bare variable"]),
    ('[[rule]]\nname = "a"\ntarget = "X/{name!r}"\n', ["bare variable"]),
    ('[[rule]]\nname = "a"\ntarget = "X/{name"\n', ["bad template"]),
    ('[[rule]]\nname = "a"\ntarget = "X"\nname_regex = "("\n', ["name_regex", "not a valid regular expression"]),
    ('[[rule]]\nname = "a"\ntarget = "../escape/{name}"\n', ["'..'"]),
    ('[[rule]]\nname = "a"\ntarget = "Photos/./{name}"\n', ["'.'"]),
    ('[[rule]]\nname = "a"\ntarget = ""\n', ["non-empty"]),
    ('[[rule]]\nname = "a"\n', ["'target' is required"]),
    ('[[rule]]\ntarget = "x"\n', ["'name' is required"]),
    ('[[rule]]\nname = "a"\ntarget = "x"\n[[rule]]\nname = "a"\ntarget = "y"\n', ["duplicate rule name 'a'"]),
    ('[[rule]]\nname = "fallback"\ntarget = "x"\n', ["reserved"]),
    ('[[rule]]\nname = "a"\ntarget = "x"\ncategory = "photo"\n', ["category must be a list"]),
    ('[[rule]]\nname = "a"\ntarget = "x"\ncategory = ["photos"]\n', ["unknown category 'photos'", "did you mean 'photo'"]),
    ('[[rule]]\nname = "a"\ntarget = "x"\nmin_size = -1\n', ["non-negative integer"]),
    ('[[rule]]\nname = "a"\ntarget = "x"\nmax_size = "10MB"\n', ["non-negative integer"]),
    ('[[rule]]\nname = "a"\ntarget = "x"\nmin_size = 10\nmax_size = 5\n', ["min_size (10) is larger than max_size (5)"]),
    ('[[rule]]\nname = "a"\ntarget = "x"\nunit = "yes"\n', ["unit must be true or false"]),
    ('[[rule]]\nname = "a"\nunit = true\ntarget = "Projects/{unit}"\n', ["{relpath}"]),
    ('[[rule]]\nname = "a"\ntarget = "Projects/{unit}/{name}"\n', ["{unit} only exists for unit rules"]),
    ('[[rules]]\nname = "a"\ntarget = "x"\n', ["unknown top-level key 'rules'", "did you mean 'rule'"]),
    ('rule = {name = "a", target = "x"}\n', ["array of tables"]),
    ('[layout]\nunit_marker = [".git"]\n', ["unknown key 'unit_marker'", "did you mean 'unit_markers'"]),
    ('[layout]\nunit_markers = [".git", "a/b"]\n', ["cannot contain '/'"]),
    ('[layout]\nbundle_patterns = ".app"\n', ["bundle_patterns must be a list"]),
    ('[layout]\nfallback = "Unsorted/{folder}"\n', ["[layout] fallback", "unknown template variable {folder}"]),
    ('[layout]\nfallback = "../x/{path}"\n', ["'..'"]),
    ("this is = not toml [", ["invalid TOML"]),
])
def test_invalid_rules_explain_themselves(text, needles):
    with pytest.raises(RuleError) as exc:
        load_rules(text)
    for needle in needles:
        assert needle in str(exc.value)


def test_error_names_the_rule_by_position_and_name():
    text = PHOTO_RULE + '[[rule]]\nname = "second"\ntarget = "x"\nbogus = 1\n'
    with pytest.raises(RuleError, match=r"rule #2 \('second'\): unknown key 'bogus'"):
        load_rules(text)


def test_rule_error_is_a_value_error():
    assert issubclass(RuleError, ValueError)


def test_minimal_rules_get_layout_defaults():
    rs = load_rules(PHOTO_RULE)
    assert [r.name for r in rs.rules] == ["photos"]
    assert rs.unit_markers == DEFAULT_UNIT_MARKERS
    assert rs.bundle_patterns == DEFAULT_BUNDLE_PATTERNS
    assert rs.fallback == DEFAULT_FALLBACK
    assert load_rules("").rules == []


def test_explicit_empty_layout_lists_turn_units_off():
    rs = load_rules('[layout]\nunit_markers = []\nbundle_patterns = []\n')
    assert rs.unit_markers == () and rs.bundle_patterns == ()


def test_rule_fields_are_normalised():
    rs = load_rules('[[rule]]\nname = "a"\ntarget = "x/{name}"\next = [".JPG", "png"]\ncategory = ["photo"]\n'
                    'remote = ["gdrive"]\nmin_size = 1\nmax_size = 9\npath_glob = "*/x/*"\nname_regex = "^a"\n')
    r = rs.rules[0]
    assert r.ext == ("jpg", "png") and r.category == ("photo",) and r.remote == ("gdrive",)
    assert (r.min_size, r.max_size, r.path_glob, r.name_regex, r.unit) == (1, 9, "*/x/*", "^a", False)


# --------------------------------------------------------------------------- render

def test_render_substitutes_and_sanitises():
    assert render("Photos/{year}/{name}", {"year": "2019", "name": "a.jpg"}) == "Photos/2019/a.jpg"
    assert render("/Photos//{year}/", {"year": "2019"}) == "Photos/2019"
    assert render("Misc/{dir}/{name}", {"dir": "a/b", "name": "x"}) == "Misc/a/b/x"
    assert render("{{literal}}/{name}", {"name": "x"}) == "{literal}/x"


def test_render_empty_variable_segment_becomes_underscore():
    assert render("Music/{parent}/{name}", {"parent": "", "name": "a.mp3"}) == "Music/_/a.mp3"
    assert render("A/{ext}", {"ext": ""}) == "A/_"
    assert render("{parent}-x/{name}", {"parent": "", "name": "n"}) == "-x/n"
    assert render("/", {}) == "_"


def test_render_rejects_dot_segments_from_values():
    with pytest.raises(RuleError):
        render("X/{dir}/{name}", {"dir": "a/../b", "name": "n"})
    with pytest.raises(RuleError):
        render("X/{name}", {"name": ".."})


def test_render_unknown_variable_raises():
    with pytest.raises(RuleError, match="unknown template variable"):
        render("X/{nope}", {"name": "n"})


# --------------------------------------------------------------------------- dates

@pytest.mark.parametrize("name, expected", [
    ("IMG_20190304_123456.jpg", ("2019", "03", "04")),
    ("IMG-20190304-WA0001.jpg", ("2019", "03", "04")),
    ("Screenshot 2019-03-04 at 10.00.00.png", ("2019", "03", "04")),
    ("Screen Shot 2019-03-04 at 10.00.00 AM.png", ("2019", "03", "04")),
    ("2019-03-04 Trip.jpg", ("2019", "03", "04")),
    ("20190304.jpg", ("2019", "03", "04")),
    ("PXL_20190304_123456789.jpg", ("2019", "03", "04")),
    ("scan 2019_03_04.pdf", ("2019", "03", "04")),
    ("notes.2019.03.04.txt", ("2019", "03", "04")),
    ("2000-01-01.txt", ("2000", "01", "01")),
    ("19900101.txt", ("1990", "01", "01")),
    ("20200229.txt", ("2020", "02", "29")),
    ("20191399 then 2018-07-09 trip.jpg", ("2018", "07", "09")),   # first date is impossible, second is fine
    ("2018-07-09 and 2017-01-02.jpg", ("2018", "07", "09")),
])
def test_date_from_name_positive(name, expected):
    assert date_from_name(name) == expected


@pytest.mark.parametrize("name", [
    "invoice 12345678901.pdf",        # inside a longer digit run
    "x1920190304.jpg",                # glued to other digits on the left
    "120190304.jpg",
    "201903041.jpg",
    "20191399.jpg",                   # month 13
    "20190230.jpg",                   # no 30 February
    "20190100.jpg",                   # day 0
    "18990304.jpg",                   # before 1990
    "21000304.jpg",                   # after 2099
    "2019-3-4.jpg",
    "2019-03_04.jpg",                 # mixed separators
    "1551686400000.jpg",              # unix milliseconds
    "IMG_1234.JPG",
    "",
])
def test_date_from_name_negative(name):
    assert date_from_name(name) is None


def test_year_from_name_beats_mtime_and_mtime_beats_unknown(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "a/IMG_20190304_1.jpg", mtime="2021-12-31T10:00:00Z")
    catalog.file(g, "a/plain.jpg", mtime="2018-07-09T10:00:00.123456Z")
    no_mtime = catalog.file(g, "a/none.jpg")
    epoch = catalog.file(g, "a/epoch.jpg", mtime="1970-01-01T00:00:00Z")
    conn.execute("UPDATE entries SET mtime = NULL WHERE id = ?", (no_mtime,))
    rs = load_rules('[[rule]]\nname = "p"\ntarget = "{year}-{month}-{day}/{name}"\n')
    got = {p.src_path: p.target_path for p in plan_layout(conn, rs)}
    assert got["a/IMG_20190304_1.jpg"] == "2019-03-04/IMG_20190304_1.jpg"
    assert got["a/plain.jpg"] == "2018-07-09/plain.jpg"
    assert got["a/none.jpg"] == "unknown-unknown-unknown/none.jpg"
    assert got["a/epoch.jpg"] == "unknown-unknown-unknown/epoch.jpg"


# --------------------------------------------------------------------------- matching loose files

def test_first_matching_rule_wins(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "a/x.jpg")
    catalog.file(g, "a/y.txt")
    rs = load_rules(
        '[[rule]]\nname = "first"\next = ["jpg"]\ntarget = "First/{name}"\n'
        '[[rule]]\nname = "second"\ncategory = ["photo"]\ntarget = "Second/{name}"\n'
        '[[rule]]\nname = "catchall"\ntarget = "Rest/{name}"\n')
    got = _placed(conn, rs)
    assert (got["a/x.jpg"].rule, got["a/x.jpg"].target_path) == ("first", "First/x.jpg")
    assert (got["a/y.txt"].rule, got["a/y.txt"].target_path) == ("catchall", "Rest/y.txt")


def _matches(conn, rule_body: str) -> set[str]:
    """Paths (by name) that the single rule 'r' claims; the others take the fallback."""
    rs = load_rules(f'[[rule]]\nname = "r"\ntarget = "R/{{name}}"\n{rule_body}')
    return {p.src_path for p in plan_layout(conn, rs) if p.rule == "r"}


@pytest.fixture
def mixed(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    catalog.file(g, "Pics/a.jpg", size=10)
    catalog.file(g, "Pics/Screenshots/shot.png", size=100)
    catalog.file(g, "Pics/IMG_1.JPG", size=1000)
    catalog.file(g, "Docs/report.pdf", size=5000)
    catalog.file(g, "Docs/my_IMG_1.txt", size=1)
    catalog.file(g, "Notes/Plan", size=None, native_doc=True, mime="application/vnd.google-apps.document")
    catalog.file(d, "Pics/b.jpg", size=20)
    return conn


def test_match_category(mixed):
    assert _matches(mixed, 'category = ["photo"]') == {
        "Pics/a.jpg", "Pics/Screenshots/shot.png", "Pics/IMG_1.JPG", "Pics/b.jpg"}
    assert _matches(mixed, 'category = ["document", "pdf"]') == {"Docs/report.pdf", "Docs/my_IMG_1.txt", "Notes/Plan"}


def test_match_ext_is_case_insensitive_on_the_rule_side(mixed):
    assert _matches(mixed, 'ext = ["jpg"]') == {"Pics/a.jpg", "Pics/IMG_1.JPG", "Pics/b.jpg"}
    assert _matches(mixed, 'ext = [".JPG"]') == {"Pics/a.jpg", "Pics/IMG_1.JPG", "Pics/b.jpg"}


def test_match_path_glob_is_case_insensitive_and_crosses_slashes(mixed):
    assert _matches(mixed, 'path_glob = "*/screenshots/*"') == {"Pics/Screenshots/shot.png"}
    assert _matches(mixed, 'path_glob = "pics/*"') == {
        "Pics/a.jpg", "Pics/Screenshots/shot.png", "Pics/IMG_1.JPG", "Pics/b.jpg"}
    assert _matches(mixed, 'path_glob = "docs/*.pdf"') == {"Docs/report.pdf"}


def test_match_name_regex_is_a_search(mixed):
    assert _matches(mixed, 'name_regex = "IMG_1"') == {"Pics/IMG_1.JPG", "Docs/my_IMG_1.txt"}
    assert _matches(mixed, 'name_regex = "^IMG_1"') == {"Pics/IMG_1.JPG"}
    assert _matches(mixed, 'name_regex = "(?i)^img_1"') == {"Pics/IMG_1.JPG"}


def test_match_sizes_and_unknown_size(mixed):
    assert _matches(mixed, "min_size = 1000") == {"Pics/IMG_1.JPG", "Docs/report.pdf"}
    assert _matches(mixed, "max_size = 20") == {"Pics/a.jpg", "Docs/my_IMG_1.txt", "Pics/b.jpg"}
    assert _matches(mixed, "min_size = 10\nmax_size = 100") == {"Pics/a.jpg", "Pics/Screenshots/shot.png", "Pics/b.jpg"}
    assert "Notes/Plan" not in _matches(mixed, "min_size = 0")   # native docs have no size to compare


def test_match_remote(mixed):
    assert _matches(mixed, 'remote = ["dropbox"]') == {"Pics/b.jpg"}
    assert "Pics/b.jpg" not in _matches(mixed, 'remote = ["gdrive", "onedrive"]')


def test_match_keys_are_anded(mixed):
    assert _matches(mixed, 'category = ["photo"]\nremote = ["gdrive"]\nmin_size = 100') == {
        "Pics/Screenshots/shot.png", "Pics/IMG_1.JPG"}


def test_native_docs_are_placed(mixed):
    got = _placed(mixed, default_rules())
    assert got["Notes/Plan"].rule == "documents"


def test_nfc_rule_text_matches_nfd_names(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    nfd = unicodedata.normalize("NFD", "Café")
    catalog.file(g, f"{nfd}/menu.txt")
    catalog.file(g, f"Other/{nfd}-notes.txt")
    assert nfd != "Café"
    assert _matches(conn, 'path_glob = "café/*"') == {f"{nfd}/menu.txt"}
    assert _matches(conn, 'name_regex = "^Café-"') == {f"Other/{nfd}-notes.txt"}


# --------------------------------------------------------------------------- units

def test_detect_git_repo_and_ignore_everything_else(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "Code/app/.git/HEAD")
    catalog.file(g, "Code/app/src/main.py")
    catalog.file(g, "Code/loose.py")
    catalog.file(g, "Photos/a.jpg")
    units = detect_units(conn, default_rules())
    assert [(u.remote_id, u.path, u.reason) for u in units] == [(g, "Code/app", "marker:.git")]


def test_detect_bundle_by_own_name_case_insensitively(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "Docs/Foo.pages/Index/Document.iwa")
    catalog.file(g, "Docs/BAR.KEY/Data/x.png")
    catalog.file(g, "Docs/old.pages")                 # a plain file named like a bundle is not a unit
    units = {u.path: u.reason for u in detect_units(conn, default_rules())}
    assert units == {"Docs/Foo.pages": "bundle:*.pages", "Docs/BAR.KEY": "bundle:*.key"}


def test_nested_unit_is_not_reported_separately(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "Code/mono/.git/HEAD")
    catalog.file(g, "Code/mono/web/package.json")
    catalog.file(g, "Code/mono/web/node_modules/left-pad/package.json")
    catalog.file(g, "Code/mono/Slides.key/Data/a.png")
    catalog.file(g, "Code/other/package.json")
    assert [u.path for u in detect_units(conn, default_rules())] == ["Code/mono", "Code/other"]


def test_marker_glob_xcodeproj(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "iOS/MyApp/MyApp.xcodeproj/project.pbxproj")
    catalog.file(g, "iOS/MyApp/Sources/a.swift")
    catalog.file(g, "net/Thing/Thing.sln")
    units = {u.path: u.reason for u in detect_units(conn, default_rules())}
    assert units == {"iOS/MyApp": "marker:*.xcodeproj", "net/Thing": "marker:*.sln"}


def test_bundle_wins_over_marker_inside_the_same_folder(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "Apps/Tool.app/package.json")
    assert [(u.path, u.reason) for u in detect_units(conn, default_rules())] == [("Apps/Tool.app", "bundle:*.app")]


def test_remote_root_is_never_a_unit(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "package.json")
    catalog.file(g, "a.txt")
    assert detect_units(conn, default_rules()) == []


def test_tombstoned_rows_are_ignored(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "Gone/package.json", present=False)
    catalog.file(g, "Alive/package.json")
    assert [u.path for u in detect_units(conn, default_rules())] == ["Alive"]


def test_custom_markers_and_path_patterns(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "Clients/Acme/contract.pdf")
    catalog.file(g, "Clients/Acme/logo.png")
    catalog.file(g, "Clients/Zed/brief.docx")
    catalog.file(g, "Taxes 2019/receipt.jpg")
    catalog.file(g, "Misc/.unit")
    rs = load_rules('[layout]\nunit_markers = [".unit"]\nbundle_patterns = ["Clients/*", "Taxes *"]\n')
    got = {u.path: u.reason for u in detect_units(conn, rs)}
    assert got == {"Clients/Acme": "bundle:Clients/*", "Clients/Zed": "bundle:Clients/*",
                   "Taxes 2019": "bundle:Taxes *", "Misc": "marker:.unit"}


def test_detect_units_per_remote_and_filter(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    catalog.file(g, "p/.git/HEAD")
    catalog.file(d, "q/go.mod")
    rs = default_rules()
    assert [(u.remote_id, u.path) for u in detect_units(conn, rs)] == [(g, "p"), (d, "q")]
    assert [(u.remote_id, u.path) for u in detect_units(conn, rs, remote_ids=[d])] == [(d, "q")]
    assert detect_units(conn, rs, remote_ids=[]) == []


def test_detect_units_reads_each_remote_once(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    for i in range(30):
        catalog.file(g, f"p{i}/.git/HEAD")
        catalog.file(d, f"q{i}/package.json")
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    assert len(detect_units(conn, default_rules())) == 60
    conn.set_trace_callback(None)
    assert sum("FROM entries" in s for s in seen) == 2


# --------------------------------------------------------------------------- layout

@pytest.fixture
def archive(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "Code/proj/.git/HEAD", size=10, mtime="2017-01-01T00:00:00Z")
    catalog.file(g, "Code/proj/src/a.py", size=20, mtime="2018-06-01T00:00:00Z")
    catalog.file(g, "Code/proj/docs/diagram.png", size=30, mtime="2016-01-01T00:00:00Z")
    catalog.file(g, "Pics/IMG_20190304_101010.jpg", size=40)
    catalog.file(g, "Misc/deep/tree/readme.xyz", size=50)
    return g


def test_unit_files_keep_relpath_under_the_unit_rule(conn, archive):
    got = _placed(conn, load_rules(PHOTO_RULE + PROJECT_RULE))
    assert got["Code/proj/src/a.py"].target_path == "Projects/proj/src/a.py"
    assert got["Code/proj/.git/HEAD"].target_path == "Projects/proj/.git/HEAD"
    # a png inside a repo is not a photo: the repo travels as a whole
    assert got["Code/proj/docs/diagram.png"].target_path == "Projects/proj/docs/diagram.png"
    assert got["Code/proj/docs/diagram.png"].rule == "projects"
    assert got["Code/proj/docs/diagram.png"].unit == "Code/proj"
    assert got["Pics/IMG_20190304_101010.jpg"].unit is None


def test_unit_rule_does_not_apply_to_loose_files_and_vice_versa(conn, archive):
    rs = load_rules('[[rule]]\nname = "everything"\ntarget = "All/{name}"\n' + PROJECT_RULE)
    got = _placed(conn, rs)
    assert got["Pics/IMG_20190304_101010.jpg"].rule == "everything"
    assert got["Code/proj/src/a.py"].rule == "projects"


def test_fallback_keeps_original_structure(conn, archive):
    got = _placed(conn, load_rules(PHOTO_RULE + PROJECT_RULE))
    p = got["Misc/deep/tree/readme.xyz"]
    assert (p.rule, p.target_path, p.unit) == ("fallback", "Unsorted/gdrive/Misc/deep/tree/readme.xyz", None)


def test_unit_without_a_unit_rule_stays_together_via_fallback(conn, archive):
    got = _placed(conn, load_rules(PHOTO_RULE))
    p = got["Code/proj/src/a.py"]
    assert (p.rule, p.target_path, p.unit) == ("fallback", "Unsorted/gdrive/Code/proj/src/a.py", "Code/proj")


def test_custom_fallback_can_use_unit_variables(conn, archive):
    rs = load_rules('[layout]\nfallback = "Rest/{unit}/{relpath}"\n')
    got = _placed(conn, rs)
    assert got["Code/proj/src/a.py"].target_path == "Rest/proj/src/a.py"
    assert got["Misc/deep/tree/readme.xyz"].target_path == "Rest/_/Misc/deep/tree/readme.xyz"


@pytest.mark.parametrize("rule, expected", [
    ('name_regex = "^proj$"', "Match"),
    ('name_regex = "^other$"', "Projects"),
    ('path_glob = "code/*"', "Match"),
    ('path_glob = "pics/*"', "Projects"),
    ('remote = ["gdrive"]', "Match"),
    ('remote = ["dropbox"]', "Projects"),
    ("min_size = 60", "Match"),       # 10 + 20 + 30 bytes in the unit
    ("min_size = 61", "Projects"),
    ("max_size = 60", "Match"),
    ("max_size = 59", "Projects"),
])
def test_unit_rule_match_keys_apply_to_the_unit_folder(conn, archive, rule, expected):
    rs = load_rules(f'[[rule]]\nname = "m"\nunit = true\n{rule}\ntarget = "Match/{{unit}}/{{relpath}}"\n' + PROJECT_RULE)
    got = _placed(conn, rs)
    assert got["Code/proj/src/a.py"].target_path.split("/")[0] == expected


def test_unit_rule_ext_and_category_come_from_the_folder_name(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "Docs/Foo.pages/Index/doc.iwa")
    catalog.file(g, "Docs/Deck.key/Data/s.png")
    catalog.file(g, "Code/p/.git/HEAD")
    rs = load_rules('[[rule]]\nname = "iwork"\nunit = true\next = ["pages"]\ntarget = "Pages/{unit}/{relpath}"\n'
                    '[[rule]]\nname = "slides"\nunit = true\ncategory = ["presentation"]\ntarget = "Slides/{unit}/{relpath}"\n'
                    + PROJECT_RULE)
    got = _placed(conn, rs)
    assert got["Docs/Foo.pages/Index/doc.iwa"].target_path == "Pages/Foo.pages/Index/doc.iwa"
    assert got["Docs/Deck.key/Data/s.png"].target_path == "Slides/Deck.key/Data/s.png"
    assert got["Code/p/.git/HEAD"].target_path == "Projects/p/.git/HEAD"


def test_unit_dates_come_from_the_unit_not_from_each_file(conn, archive):
    rs = load_rules('[[rule]]\nname = "p"\nunit = true\ntarget = "Projects/{year}/{unit}/{relpath}"\n')
    got = _placed(conn, rs)
    assert {p.target_path.split("/")[1] for p in got.values() if p.unit} == {"2018"}   # newest file wins


def test_unit_date_prefers_the_folder_name(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "D/2015-01-02 Notes.pages/Index/x.iwa", mtime="2021-01-01T00:00:00Z")
    rs = load_rules('[[rule]]\nname = "p"\nunit = true\ntarget = "{year}/{month}/{unit}/{relpath}"\n')
    assert _placed(conn, rs)["D/2015-01-02 Notes.pages/Index/x.iwa"].target_path == \
        "2015/01/2015-01-02 Notes.pages/Index/x.iwa"


def test_template_variables_for_loose_files(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    catalog.file(g, "A/B/Report.v2.tar.gz", mtime="2019-03-04T10:00:00Z")
    catalog.file(g, "top.txt", mtime="2019-03-04T10:00:00Z")
    rs = load_rules('[[rule]]\nname = "all"\ntarget = "{category}/{ext}/{stem}/{name}/{parent}/{dir}/{path}/{remote}/{relpath}/{day}"\n')
    got = _placed(conn, rs)
    assert got["A/B/Report.v2.tar.gz"].target_path == \
        "archive/gz/Report.v2.tar/Report.v2.tar.gz/B/A/B/A/B/Report.v2.tar.gz/gdrive/A/B/Report.v2.tar.gz/04"
    assert got["top.txt"].target_path == "document/txt/top/top.txt/_/_/top.txt/gdrive/top.txt/04"


def test_target_root_and_path_prefix(conn, archive):
    rs = load_rules(PHOTO_RULE + PROJECT_RULE)
    all_ = plan_layout(conn, rs, target_root="/Merged/")
    assert all(p.target_path.startswith("Merged/") for p in all_)
    assert plan_layout(conn, rs)[0].target_path == "Code/proj/.git/HEAD".replace("Code/proj", "Projects/proj")
    sub = _placed(conn, rs, path_prefix="Code/proj/src")
    assert list(sub) == ["Code/proj/src/a.py"]
    # the unit is detected on the whole remote, so relpath is still relative to the unit root
    assert sub["Code/proj/src/a.py"].target_path == "Projects/proj/src/a.py"
    with pytest.raises(RuleError):
        plan_layout(conn, rs, target_root="../up")


def test_path_prefix_is_not_a_like_pattern(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    for p in ("A_B/f1.txt", "AxB/f2.txt", "A%B/f3.txt", "A_B2/f4.txt", "A_B.txt", "A_B/sub/f5.txt"):
        catalog.file(g, p)
    got = sorted(p.src_path for p in plan_layout(conn, load_rules(""), path_prefix="A_B"))
    assert got == ["A_B/f1.txt", "A_B/sub/f5.txt"]
    only_file = [p.src_path for p in plan_layout(conn, load_rules(""), path_prefix="A_B.txt")]
    assert only_file == ["A_B.txt"]


def test_one_placement_per_live_file_and_deterministic_order(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    catalog.file(d, "z/2.txt")
    catalog.file(g, "b/9.txt")
    catalog.file(g, "a/1.txt")
    catalog.file(g, "a/gone.txt", present=False)
    catalog.file(g, "a/Foo.pages/x")
    rs = default_rules()
    first = plan_layout(conn, rs)
    assert first == plan_layout(conn, rs)
    assert [(p.remote_id, p.src_path) for p in first] == [
        (g, "a/1.txt"), (g, "a/Foo.pages/x"), (g, "b/9.txt"), (d, "z/2.txt")]
    live_files = conn.execute("SELECT COUNT(*) FROM entries WHERE is_dir = 0 AND present = 1").fetchone()[0]
    assert len(first) == live_files == 4
    assert {p.entry_id for p in first} == {r[0] for r in conn.execute("SELECT id FROM entries WHERE is_dir = 0 AND present = 1")}
    assert [p.src_path for p in plan_layout(conn, rs, remote_ids=[d])] == ["z/2.txt"]


def test_drive_duplicate_paths_get_one_placement_each(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    a = catalog.file(g, "a/same.txt", provider_id="1")
    b = catalog.file(g, "a/same.txt", provider_id="2")
    placed = plan_layout(conn, load_rules(""))
    assert [p.entry_id for p in placed] == [a, b]
    assert placed[0].target_path == placed[1].target_path     # collisions are the plan builder's job


def test_planning_never_writes(conn, archive):
    before = conn.execute("SELECT COUNT(*), SUM(row_version) FROM entries").fetchone()[:]
    changes = conn.total_changes
    plan_layout(conn, default_rules())
    folder_profiles(conn)
    assert conn.total_changes == changes
    assert conn.execute("SELECT COUNT(*), SUM(row_version) FROM entries").fetchone()[:] == before


def test_chinese_and_nfd_names_survive_rendering(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    nfd_dir = unicodedata.normalize("NFD", "Résumé Café")
    nfd_name = unicodedata.normalize("NFD", "Éclair.pdf")
    catalog.file(g, "照片/旅行/IMG_20190304_101010.jpg")
    catalog.file(g, "音乐/周杰伦·范特西/01 开不了口.mp3")
    catalog.file(g, f"{nfd_dir}/{nfd_name}", mtime="2020-05-06T00:00:00Z")
    catalog.file(g, "项目/代码/.git/HEAD")
    catalog.file(g, "项目/代码/说明 文档.md")
    got = _placed(conn, default_rules())
    assert got["照片/旅行/IMG_20190304_101010.jpg"].target_path == "Photos/2019/2019-03/IMG_20190304_101010.jpg"
    assert got["音乐/周杰伦·范特西/01 开不了口.mp3"].target_path == "Music/周杰伦·范特西/01 开不了口.mp3"
    pdf = got[f"{nfd_dir}/{nfd_name}"].target_path
    assert pdf == f"Documents/2020/{nfd_name}"
    assert unicodedata.is_normalized("NFD", pdf.split("/")[-1])
    assert got["项目/代码/说明 文档.md"].target_path == "Projects/代码/说明 文档.md"
    assert got["项目/代码/.git/HEAD"].unit == "项目/代码"


# --------------------------------------------------------------------------- default rules

def test_default_rules_load_and_match_the_module_constants():
    rs = default_rules()
    assert rs.unit_markers == DEFAULT_UNIT_MARKERS
    assert rs.bundle_patterns == DEFAULT_BUNDLE_PATTERNS
    assert rs.fallback == DEFAULT_FALLBACK
    names = [r.name for r in rs.rules]
    assert len(names) == len(set(names)) > 5
    assert names.index("screenshots") < names.index("photos")


def test_default_rules_lay_out_a_mixed_catalog_sensibly(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    catalog.file(g, "Camera/IMG_20190304_101010.jpg")
    catalog.file(g, "Camera/DSC0001.JPG", mtime="2015-08-09T00:00:00Z")
    catalog.file(g, "Desktop/Screenshot 2020-01-02 at 10.00.00.png")
    catalog.file(g, "Trips/clip.mov", mtime="2018-02-03T00:00:00Z")
    catalog.file(g, "Music/Pink Floyd/The Wall/01 In The Flesh.mp3")
    catalog.file(g, "Taxes/2019 return.pdf", mtime="2020-03-01T00:00:00Z")
    catalog.file(g, "Taxes/budget.xlsx", mtime="2021-04-01T00:00:00Z")
    catalog.file(g, "Notes/Plan", size=None, native_doc=True, mime="application/vnd.google-apps.document",
                 mtime="2022-02-02T00:00:00Z")
    catalog.file(g, "Work/site/package.json")
    catalog.file(g, "Work/site/img/logo.png")
    catalog.file(g, "Work/site/README.md")
    catalog.file(g, "Docs/Memo.pages/Index/Document.iwa", mtime="2019-09-09T00:00:00Z")
    catalog.file(g, "Docs/Memo.pages/Preview.jpg", mtime="2019-09-10T00:00:00Z")
    catalog.file(g, "Misc/script.sh")
    catalog.file(g, "Misc/blob.bin")
    catalog.file(d, "Backups/old.zip", mtime="2014-01-01T00:00:00Z")
    got = _placed(conn, default_rules())
    expected = {
        "Camera/IMG_20190304_101010.jpg": "Photos/2019/2019-03/IMG_20190304_101010.jpg",
        "Camera/DSC0001.JPG": "Photos/2015/2015-08/DSC0001.JPG",
        "Desktop/Screenshot 2020-01-02 at 10.00.00.png": "Screenshots/2020/Screenshot 2020-01-02 at 10.00.00.png",
        "Trips/clip.mov": "Videos/2018/clip.mov",
        "Music/Pink Floyd/The Wall/01 In The Flesh.mp3": "Music/The Wall/01 In The Flesh.mp3",
        "Taxes/2019 return.pdf": "Documents/2020/2019 return.pdf",
        "Taxes/budget.xlsx": "Documents/Spreadsheets/2021/budget.xlsx",
        "Notes/Plan": "Documents/2022/Plan",
        "Work/site/package.json": "Projects/site/package.json",
        "Work/site/img/logo.png": "Projects/site/img/logo.png",
        "Work/site/README.md": "Projects/site/README.md",
        "Docs/Memo.pages/Index/Document.iwa": "Documents/2019/Memo.pages/Index/Document.iwa",
        "Docs/Memo.pages/Preview.jpg": "Documents/2019/Memo.pages/Preview.jpg",
        "Misc/script.sh": "Unsorted/gdrive/Misc/script.sh",
        "Misc/blob.bin": "Unsorted/gdrive/Misc/blob.bin",
        "Backups/old.zip": "Archives/2014/old.zip",
    }
    assert {k: v.target_path for k, v in got.items()} == expected
    assert got["Work/site/img/logo.png"].unit == "Work/site"
    assert got["Misc/script.sh"].rule == "fallback"
    assert got["Backups/old.zip"].rule == "archives"


# --------------------------------------------------------------------------- folder profiles

@pytest.fixture
def tree(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    ids = {}
    ids["a"] = catalog.file(g, "Photos/2019/a.jpg", size=100, md5="aa", mtime="2019-03-04T10:00:00Z")
    ids["b"] = catalog.file(g, "Photos/2019/b.jpg", size=200, mtime="2019-04-01T10:00:00Z")
    ids["c"] = catalog.file(g, "Photos/2019/sub/c.png", size=50, mtime="2019-05-05T10:00:00Z")
    ids["d"] = catalog.file(g, "Photos/2020/d.jpg", size=300, mtime="2020-01-01T10:00:00Z")
    ids["x"] = catalog.file(g, "Docs/x.pdf", size=1000, mtime="2018-01-01T10:00:00Z")
    ids["y"] = catalog.file(g, "Docs/y.docx", size=10, mtime="2018-02-01T10:00:00Z")
    ids["z"] = catalog.file(g, "Docs/noext", size=5, mtime="2018-03-01T10:00:00Z")
    ids["root"] = catalog.file(g, "root.txt", size=1)
    ids["head"] = catalog.file(g, "Code/app/.git/HEAD", size=1, mtime="2017-01-01T10:00:00Z")
    ids["main"] = catalog.file(g, "Code/app/src/main.py", size=20, mtime="2017-02-01T10:00:00Z")
    catalog.dir(g, "Empty")
    ids["dbx"] = catalog.file(d, "Photos/2019/a-copy.jpg", size=100, md5="aa", mtime="2019-03-04T10:00:00Z")
    ids["gone"] = catalog.file(g, "Photos/2019/gone.jpg", size=999, present=False)
    return ids


def _by_path(profiles):
    return {(p.remote, p.path): p for p in profiles}


def test_profiles_depth_one_aggregate_everything_below(conn, tree):
    profiles = folder_profiles(conn, remote="gdrive")
    assert [p.path for p in profiles] == ["Docs", "Photos", "Code", "Empty"]   # bytes desc
    docs, photos, code, empty = profiles
    assert (docs.remote, docs.files, docs.dirs, docs.bytes) == ("gdrive", 3, 0, 1015)
    assert docs.categories == {"pdf": 1, "document": 1, "other": 1}
    assert docs.top_exts == [("docx", 1), ("pdf", 1)]        # extension-less files are left out
    assert (docs.mtime_min, docs.mtime_max) == ("2018-01-01T10:00:00Z", "2018-03-01T10:00:00Z")
    assert (photos.files, photos.dirs, photos.bytes) == (4, 3, 650)   # 2019, 2019/sub, 2020 ; tombstone ignored
    assert photos.categories == {"photo": 4}
    assert photos.top_exts == [("jpg", 3), ("png", 1)]
    assert (photos.mtime_min, photos.mtime_max) == ("2019-03-04T10:00:00Z", "2020-01-01T10:00:00Z")
    assert (code.files, code.dirs, code.bytes) == (2, 3, 21)           # .git, src, app
    assert (empty.files, empty.dirs, empty.bytes, empty.categories, empty.sample_names) == (0, 0, 0, {}, [])
    assert "root.txt" not in [n for p in profiles for n in p.sample_names]


def test_profiles_depth_two_and_prefix(conn, tree):
    two = folder_profiles(conn, remote="gdrive", depth=2)
    assert [(p.path, p.files, p.dirs, p.bytes) for p in two] == [
        ("Photos/2019", 3, 1, 350), ("Photos/2020", 1, 0, 300), ("Code/app", 2, 2, 21)]
    under = folder_profiles(conn, remote="gdrive", prefix="Photos")
    assert [(p.path, p.bytes) for p in under] == [("Photos/2019", 350), ("Photos/2020", 300)]
    assert [p.path for p in folder_profiles(conn, remote="gdrive", prefix="/Photos/2019/")] == ["Photos/2019/sub"]
    assert folder_profiles(conn, remote="gdrive", prefix="Photos/2019", depth=2) == []
    assert folder_profiles(conn, remote="gdrive", prefix="Nope") == []


def test_profiles_prefix_is_not_a_like_pattern(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    for p in ("A_B/s1/f", "AxB/s2/f", "A%B/s3/f", "A_B2/s4/f", "A_B.txt"):
        catalog.file(g, p)
    assert [p.path for p in folder_profiles(conn, prefix="A_B")] == ["A_B/s1"]
    assert [p.path for p in folder_profiles(conn, prefix="A%B")] == ["A%B/s3"]


def test_profiles_span_remotes_unless_filtered(conn, tree):
    both = folder_profiles(conn)
    assert {(p.remote, p.path) for p in both} >= {("gdrive", "Photos"), ("dropbox", "Photos")}
    only = folder_profiles(conn, remote="dropbox")
    assert [(p.remote, p.path, p.files) for p in only] == [("dropbox", "Photos", 1)]
    with pytest.raises(KeyError):
        folder_profiles(conn, remote="nope")


def test_profiles_limit_keeps_the_biggest(conn, tree):
    assert [p.path for p in folder_profiles(conn, remote="gdrive", limit=2)] == ["Docs", "Photos"]
    assert folder_profiles(conn, limit=0) == []


def test_profiles_reject_bad_depth(conn, tree):
    with pytest.raises(ValueError):
        folder_profiles(conn, depth=0)


def test_profile_duplicate_files_use_content_groups_across_remotes(conn, tree):
    conn.execute("UPDATE entries SET content_group = 7 WHERE id IN (?, ?)", (tree["a"], tree["dbx"]))
    conn.execute("UPDATE entries SET content_group = 8 WHERE id IN (?, ?)", (tree["d"], tree["gone"]))   # only copy is a tombstone
    conn.execute("UPDATE entries SET content_group = 9 WHERE id = ?", (tree["x"],))                       # sole member
    dup = {(p.remote, p.path): p.duplicate_files for p in folder_profiles(conn)}
    assert dup[("gdrive", "Photos")] == 1
    assert dup[("dropbox", "Photos")] == 1
    assert dup[("gdrive", "Docs")] == 0
    # a copy in the same folder counts for both files
    conn.execute("UPDATE entries SET content_group = 7 WHERE id = ?", (tree["b"],))
    assert _by_path(folder_profiles(conn))[("gdrive", "Photos")].duplicate_files == 2


def test_profile_unit_reason_covers_the_unit_and_everything_inside(conn, tree):
    got = {p.path: p.unit for p in folder_profiles(conn, remote="gdrive", prefix="Code", depth=1)}
    assert got == {"Code/app": "marker:.git"}
    inside = {p.path: p.unit for p in folder_profiles(conn, remote="gdrive", prefix="Code/app")}
    assert inside == {"Code/app/.git": "marker:.git", "Code/app/src": "marker:.git"}
    assert {p.path: p.unit for p in folder_profiles(conn, remote="gdrive")}["Photos"] is None
    no_units = load_rules('[layout]\nunit_markers = []\nbundle_patterns = []\n')
    assert folder_profiles(conn, remote="gdrive", prefix="Code", ruleset=no_units)[0].unit is None


def test_profile_sample_names_are_spread_and_deterministic(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    for i in range(100):
        catalog.file(g, f"Big/sub{i % 4}/f{i:03d}.txt")
    catalog.file(g, "Small/only.txt")
    catalog.file(g, "Small/two.txt")
    profiles = _by_path(folder_profiles(conn))
    big = profiles[("gdrive", "Big")]
    # path order is sub0/*, sub1/*, ... (25 files each); the samples sit at the centres of 5 equal slices
    assert big.sample_names == ["f040.txt", "f021.txt", "f002.txt", "f082.txt", "f063.txt"]
    assert big.sample_names == _by_path(folder_profiles(conn))[("gdrive", "Big")].sample_names
    assert profiles[("gdrive", "Small")].sample_names == ["only.txt", "two.txt"]
    assert big.categories == {"document": 100}


def test_profile_top_lists_are_capped_and_ordered(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    cats = {"jpg": 7, "mp4": 6, "mp3": 5, "pdf": 4, "xlsx": 3, "pptx": 2, "zip": 1, "py": 1}
    for ext, n in cats.items():
        for i in range(n):
            catalog.file(g, f"Mix/{ext}{i}.{ext}")
    mix = folder_profiles(conn)[0]
    assert list(mix.categories) == ["photo", "video", "audio", "pdf", "spreadsheet", "presentation"]   # top 6, by count
    assert mix.top_exts == [("jpg", 7), ("mp4", 6), ("mp3", 5), ("pdf", 4), ("xlsx", 3)]
    assert mix.files == 29
