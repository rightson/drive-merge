"""The server speaks in codes and params; the page words them (src/drivemerge/web/i18n.js).

Every human-readable message the server hands the web page is an English `message` (for the CLI, the API and MCP
clients) plus a `code` and `params`. These tests read the server's sources and the page's dictionaries side by side,
so a new violation variant, warning, refusal or suggestion without a sentence in both languages fails here, and so
does a sentence that uses a parameter the server does not send. No browser is needed (tests/test_ui_frontend.py also
runs the dictionaries in Chromium).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import drivemerge
from drivemerge import builders, simulate

SRC = Path(drivemerge.__file__).parent
CJK = re.compile("[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
ENTRY = re.compile(r"^  '([^']+)': (.*),\s*$")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Placeholders the page itself fills in, whatever the server sent.
PAGE_PARAMS = {"warn": {"dst"}}


def dictionaries() -> dict[str, dict[str, str]]:
    """{'zh-TW': {key: raw value}, 'en': {...}} read from i18n.js; a repeated key in one dictionary is an error."""
    text = (SRC / "web" / "i18n.js").read_text(encoding="utf-8")
    zh_start, en_start, end = text.index("const zh = {"), text.index("const en = {"), text.index("const DICTS")
    out = {}
    for lang, block in (("zh-TW", text[zh_start:en_start]), ("en", text[en_start:end])):
        entries: dict[str, str] = {}
        for line in block.splitlines():
            m = ENTRY.match(line)
            if m:
                assert m.group(1) not in entries, f"{lang}: {m.group(1)} is defined twice"
                entries[m.group(1)] = m.group(2)
        out[lang] = entries
    return out


DICTS = dictionaries()


def keys_of(*patterns: str) -> set[str]:
    return {k for k in DICTS["en"] if any(re.fullmatch(p, k) for p in patterns)}


def need(key: str) -> None:
    for lang, d in DICTS.items():
        assert key in d, f"{lang} has no {key}"


def calls(path: str, *names: str):
    """Every call to a function called one of `names` (a bare name or an attribute) in a source file."""
    tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id in names) or (isinstance(f, ast.Attribute) and f.attr in names):
                yield node


def const(node: ast.AST | None):
    return node.value if isinstance(node, ast.Constant) else None


def keyword_names(call: ast.Call) -> set[str]:
    """Names of the keyword arguments, including those given as **{"from": ...}."""
    out = set()
    for kw in call.keywords:
        if kw.arg is not None:
            out.add(kw.arg)
        elif isinstance(kw.value, ast.Dict):
            out |= {const(k) for k in kw.value.keys if const(k) is not None}
    return out


def placeholders(raw: str) -> set[str]:
    return set(PLACEHOLDER.findall(raw))


# --------------------------------------------------------------------------- the dictionaries

def test_both_languages_have_the_same_keys_and_no_placeholder_goes_missing():
    zh, en = DICTS["zh-TW"], DICTS["en"]
    assert set(zh) == set(en), sorted(set(zh) ^ set(en))
    for key in en:
        assert placeholders(zh[key]) == placeholders(en[key]), f"{key}: {placeholders(zh[key])} vs {placeholders(en[key])}"


def test_chinese_strings_are_chinese_and_english_strings_are_not():
    zh, en = DICTS["zh-TW"], DICTS["en"]
    loose = {"app.language", "common.listSep", "sug.q", "sug.sep", "sug.joinReasons", "rules.problemLine"}      # punctuation or both languages
    for key, raw in en.items():
        if key in loose:
            continue
        assert not CJK.search(raw), f"en {key} contains Chinese"
    for key, raw in zh.items():
        if key in loose or key.startswith(("lang.", "app.version")):
            continue
        if re.fullmatch(r"[A-Za-z0-9 ._:/{}+()'\"<>=\[\]-]*", raw.strip("'")):
            continue                                         # a code, a command or a unit: the same in both languages
        assert CJK.search(raw), f"zh-TW {key} has no Chinese: {raw}"


# --------------------------------------------------------------------------- simulator violations and warnings

def simulator_messages() -> set[tuple[str, str | None]]:
    """(code, variant) of every violation or warning simulate.py can raise, from its source."""
    found: set[tuple[str, str | None]] = set()
    for code, variant, _ in simulator_calls():
        found.add((code, variant))
    return found


def simulator_calls():
    """(code, variant, param names) of each bad(...) and Violation(...) call with a literal code."""
    for call in calls("simulate.py", "bad"):
        if const(call.args[0]) is not None:
            variant = next((const(k.value) for k in call.keywords if k.arg == "variant"), None)
            yield const(call.args[0]), variant, keyword_names(call) - {"variant"}
    for call in calls("simulate.py", "Violation"):
        if const(call.args[0]) is None:                  # the one inside bad(), which passes its own arguments on
            continue
        params = call.args[3] if len(call.args) > 3 else None
        variant, names = None, set()
        if isinstance(params, ast.Dict):
            variant = next((const(v) for k, v in zip(params.keys, params.values) if const(k) == "variant"), None)
            names = {const(k) for k in params.keys} - {None, "variant"}
        yield const(call.args[0]), variant, names


def test_every_simulator_message_has_a_sentence_in_both_languages():
    messages = simulator_messages()
    assert len(messages) >= 30, messages                   # the scan found the calls, not nothing
    for code, variant in messages:
        need(f"viol.{code}.title")
        need(f"viol.{code}.detail" + (f".{variant}" if variant else ""))
    # ... and nothing is left over: a sentence the server never sends is a sentence nobody maintains.
    sentences = {k for k in keys_of(r"viol\.[A-Z_]+\.detail(\.\w+)?")}
    assert sentences == {f"viol.{c}.detail" + (f".{v}" if v else "") for c, v in messages}


def test_the_kinds_a_simulator_message_names_have_sentences():
    for why in (*simulate._MISSING_WHY, "different", "unproven"):
        need(f"viol.why.{why}")
    for problem in simulate._PATH_PROBLEMS:
        need(f"viol.problem.{problem}")
    for what in ("created", "entry"):
        need(f"viol.what.{what}")
    for role in ("source", "destination", "witness"):
        need(f"viol.role.{role}")
    kinds = keys_of(r"viol\.(why|what|problem|role)\.\w+")
    assert len(kinds) == len(simulate._MISSING_WHY) + 2 + len(simulate._PATH_PROBLEMS) + 2 + 3


def test_simulator_sentences_use_only_parameters_the_simulator_sends():
    """A placeholder the server never fills would show up as '{name}' on the page."""
    sent: dict[tuple[str, str | None], set[str]] = {}
    for code, variant, names in simulator_calls():
        sent.setdefault((code, variant), set()).update(names)
    # Each violation also has `seq` (its own field, not a param); the page passes it along with the params.
    extra = {"seq"}
    kind_params = {"why": {"by"}, "problem": {"segment"}, "what": {"entry"}}      # the sentences of a kind use these
    for (code, variant), names in sent.items():
        key = f"viol.{code}.detail" + (f".{variant}" if variant else "")
        usable = names | extra | {p for n in names for p in kind_params.get(n, ())}
        for lang, d in DICTS.items():
            assert placeholders(d[key]) <= usable, f"{lang} {key}: {placeholders(d[key]) - usable} is never sent"


# --------------------------------------------------------------------------- workspace conflicts, warnings, refusals

def test_every_conflict_the_engine_can_raise_has_a_title_text_and_detail():
    codes = {const(c.args[1]) for c in calls("workspace.py", "conflict")}
    assert codes == {"DIFFERENT_CONTENT", "DIR_IN_WAY", "FILE_IN_WAY", "OCCUPIED", "CYCLE", "CASE_ONLY_RENAME"}, codes
    for code in codes:
        for part in ("title", "text", "detail"):
            need(f"conflict.{code}.{part}")
    assert keys_of(r"conflict\.[A-Z_]+\.detail") == {f"conflict.{c}.detail" for c in codes}


def test_every_staging_warning_has_a_sentence_that_uses_only_what_it_sends():
    sent: dict[str, set[str]] = {}
    for call in calls("workspace.py", "warn"):
        sent.setdefault(const(call.args[0]), set()).update(keyword_names(call))
    assert set(sent) >= {"MERGE", "CROSS_REMOTE", "READ_ONLY", "CASE_COLLISION", "CASE_ONLY_RENAME"}, sent
    for code, names in sent.items():
        need(f"warn.{code}")
        for lang, d in DICTS.items():
            assert placeholders(d[f"warn.{code}"]) <= names | PAGE_PARAMS["warn"], (lang, code)
    assert keys_of(r"warn\.[A-Z_]+") == {f"warn.{c}" for c in sent}


def test_every_refusal_has_a_sentence_that_uses_only_what_every_site_sends():
    """err.<CODE> is shown for a WorkspaceError or UiError code; a parameter is usable only if all sites send it."""
    sites: dict[str, list[set[str]]] = {}
    for call in calls("workspace.py", "WorkspaceError"):
        code = const(call.args[0])
        if code is None:                               # the code is a variable: _parse_action's unknown-remote refusal
            for dynamic in ("SRC_NOT_FOUND", "DST_NOT_DIR"):
                sites.setdefault(dynamic, []).append(keyword_names(call))
        else:
            sites.setdefault(code, []).append(keyword_names(call))
    for call in calls("ui_api.py", "UiError"):
        sites.setdefault(const(call.args[1]), []).append(keyword_names(call) - {"headers"})
    for code in ("NOT_FOUND", "BAD_REQUEST", "UNAUTHENTICATED"):   # also given to every error the shared handlers map
        sites.setdefault(code, []).append(set())
    assert len(sites) >= 20, sorted(sites)
    for code, per_site in sites.items():
        need(f"err.{code}")
        usable = set.intersection(*per_site)
        for lang, d in DICTS.items():
            assert placeholders(d[f"err.{code}"]) <= usable, f"{lang} err.{code}: {placeholders(d[f'err.{code}']) - usable}"


def test_every_code_the_explore_compare_and_coverage_views_can_raise_has_a_sentence():
    """CompareError(code, message, **params) is what compare.py, explore.py, coverage.py and the service raise
    (docs/EXPLORE.md section A); the page words each code from its params in both languages."""
    sites: dict[str, list[set[str]]] = {}
    for path in ("compare.py", "explore.py", "coverage.py", "service.py"):
        for call in calls(path, "CompareError"):
            sites.setdefault(const(call.args[0]), []).append(keyword_names(call))
    assert set(sites) == {"NOT_FOUND", "QUARANTINE", "NOT_A_FOLDER", "COMPARE_NESTED", "COMPARE_TOO_LARGE",
                          "BAD_QUERY"}, sorted(sites)
    for code, per_site in sites.items():
        need(f"err.{code}")
        usable = set.intersection(*per_site)
        for lang, d in DICTS.items():
            assert placeholders(d[f"err.{code}"]) <= usable, f"{lang} err.{code}: {placeholders(d[f'err.{code}']) - usable}"


def test_the_jobs_and_staged_step_codes_have_sentences():
    for code in ("SRC_NOT_FOUND", "DST_NOT_DIR"):
        need(f"stale.{code}")
    codes = {const(v) for n in ast.walk(ast.parse((SRC / "workspace.py").read_text(encoding="utf-8")))
             if isinstance(n, ast.Dict) for k, v in zip(n.keys, n.values) if const(k) == "code" and const(v)}
    assert {"SRC_NOT_FOUND", "DST_NOT_DIR"} <= codes


# --------------------------------------------------------------------------- bulk actions from Explore (docs/BULK.md)

def test_every_selection_code_has_a_sentence_that_uses_only_what_every_site_sends():
    """A SelectionError is answered {error, code, params}; the page words err.<CODE> in both languages (docs/BULK.md section B)."""
    from drivemerge import explore
    sites: dict[str, list[set[str]]] = {}
    for call in calls("explore.py", "SelectionError"):
        sites.setdefault(const(call.args[0]), []).append(keyword_names(call))
    assert set(sites) - {"BAD_QUERY"} == set(explore.SELECTION_CODES), sorted(sites)
    for code in explore.SELECTION_CODES:
        need(f"err.{code}")
        usable = set.intersection(*sites[code])
        for lang, d in DICTS.items():
            assert placeholders(d[f"err.{code}"]) <= usable, f"{lang} err.{code}: {placeholders(d[f'err.{code}']) - usable}"
    # the parameters the spec names, which the sentences may rely on
    assert set.intersection(*sites["TOO_MANY_KEYS"]) >= {"param", "count", "limit"}
    assert set.intersection(*sites["SELECTION_CHANGED"]) >= {"expected", "total"}
    assert set.intersection(*sites["SELECTION_TOO_LARGE"]) >= {"total", "limit"}
    assert set.intersection(*sites["SELECTION_TRUNCATED"]) >= {"scan_cap"}
    assert set.intersection(*sites["BAD_KEY"]) >= {"key"}


def test_every_gather_refusal_has_a_sentence_that_uses_only_what_every_site_sends():
    """gather.py raises WorkspaceError (409) with CODES, besides the draft and destination refusals the workspace has."""
    from drivemerge import gather
    sites: dict[str, list[set[str]]] = {}
    for call in calls("gather.py", "WorkspaceError"):
        sites.setdefault(const(call.args[0]), []).append(keyword_names(call))
    assert set(gather.CODES) <= set(sites), sorted(sites)
    for code in sites:
        need(f"err.{code}")
        usable = set.intersection(*sites[code])
        for lang, d in DICTS.items():
            assert placeholders(d[f"err.{code}"]) <= usable, f"{lang} err.{code}: {placeholders(d[f'err.{code}']) - usable}"
    assert set.intersection(*sites["GATHER_TOO_MANY"]) >= {"files", "limit"}
    assert set.intersection(*sites["NOTHING_TO_GATHER"]) >= {"contents"}


def test_every_gather_skip_code_has_a_sentence_and_a_heading_in_both_languages_that_use_only_its_params():
    """`skipped.items[].params` of a gather (docs/BULK.md section D): the dialog words gather.skip.<CODE> from them, and
    gather.skipHead.<CODE> (a heading over the contents skipped for that code) from the count the page makes."""
    from drivemerge import gather
    emitted: dict[str, list[set[str]]] = {}
    for call in calls("gather.py", "skip"):
        emitted.setdefault(const(call.args[2]), []).append(keyword_names(call))
    assert set(emitted) == set(gather.SKIP_CODES), sorted(emitted)
    spec = {"ALREADY_THERE": {"name"}, "NAME_TAKEN": {"name"}, "NAME_CLASH": {"name", "with"}, "NOT_ON_TARGET": {"remote"},
            "READ_ONLY": {"remote"}, "NATIVE_DOC": set(), "AMBIGUOUS_PATH": {"remote", "path"}, "GONE": set()}
    for code in gather.SKIP_CODES:
        need(f"gather.skip.{code}")
        need(f"gather.skipHead.{code}")
        usable = set.intersection(*emitted[code])
        assert usable == spec[code], (code, usable)
        for lang, d in DICTS.items():
            assert placeholders(d[f"gather.skip.{code}"]) <= usable, f"{lang} gather.skip.{code}"
            assert placeholders(d[f"gather.skipHead.{code}"]) <= {"n"}, f"{lang} gather.skipHead.{code}"
    assert keys_of(r"gather\.skip\.[A-Z_]+") == {f"gather.skip.{c}" for c in gather.SKIP_CODES}
    assert keys_of(r"gather\.skipHead\.[A-Z_]+") == {f"gather.skipHead.{c}" for c in gather.SKIP_CODES}


def test_the_bulk_wording_of_a_gather_step_and_a_selection_plan_is_in_both_languages():
    for key in ("act.gather", "plans.sum.dedupeSelection", "gather.title", "gather.staged", "sel.count", "sel.limit",
                "sel.cleared", "sel.useCurrent"):
        need(key)
    for lang, d in DICTS.items():
        assert placeholders(d["act.gather"]) == {"n", "dst"}, lang           # what describeAction fills in
        assert placeholders(d["plans.sum.dedupeSelection"]) == {"n"}, lang


# --------------------------------------------------------------------------- the Plans view: build errors, skips

def build_error_sites() -> dict[str, list[set[str]]]:
    """{code: [parameter names of each raise site]} of every `BuildError(message, code, **params)` in builders.py."""
    sites: dict[str, list[set[str]]] = {}
    for call in calls("builders.py", "BuildError"):
        code = const(call.args[1]) if len(call.args) > 1 else next(
            (const(k.value) for k in call.keywords if k.arg == "code"), None)
        assert code is not None, f"builders.py:{call.lineno}: a BuildError without a literal code"
        sites.setdefault(code, []).append(keyword_names(call) - {"code"})
    return sites


def test_every_build_error_code_has_a_sentence_that_uses_only_what_every_site_sends():
    """A BuildError is answered as 400 {error, code, params}; the page words build.<CODE> from the params."""
    sites = build_error_sites()
    assert set(sites) == {"BAD_LOCATION", "UNKNOWN_REMOTE", "BAD_OP", "MISSING_SRC", "MISSING_DST", "READ_ONLY",
                          "BAD_PARAM", "BAD_KEEPER", "BAD_GROUP"}, sorted(sites)
    sites["BUILD_ERROR"] = [set()]                         # the default code of a BuildError raised without one
    for code, per_site in sites.items():
        need(f"build.{code}")
        usable = set.intersection(*per_site)
        for lang, d in DICTS.items():
            assert placeholders(d[f"build.{code}"]) <= usable, f"{lang} build.{code}: {placeholders(d[f'build.{code}']) - usable}"
    assert keys_of(r"build\.[A-Z_]+") == {f"build.{c}" for c in sites}


def test_every_skip_code_has_a_sentence():
    """A skipped file and a copy a dedupe group cannot keep (`why_not`) are worded from skip.<CODE>. The sentence
    sits next to the file's location, so it takes no parameter, except the destination of a layout skip."""
    emitted = set()
    tree = ast.parse((SRC / "builders.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            emitted |= {const(v) for k, v in zip(node.keys, node.values) if const(k) == "code" and const(v)}
        if isinstance(node, ast.Call) and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) in (
                "_skip", "_skip_unplaceable"):
            emitted |= {const(a) for a in node.args if isinstance(const(a), str) and const(a).isupper()}
    emitted |= {"LAYOUT_BLOCKED", "LAYOUT_TAKEN"}          # _skip_unplaceable picks one of them
    assert emitted <= set(builders.SKIP_CODES), emitted - set(builders.SKIP_CODES)
    assert set(builders.NOT_KEEPABLE_CODES) <= set(builders.SKIP_CODES)
    assert set(builders.SKIP_CODES) == emitted | set(builders.NOT_KEEPABLE_CODES)
    for code in builders.SKIP_CODES:
        need(f"skip.{code}")
        usable = {"dest"} if code.startswith("LAYOUT_") else set()
        for lang, d in DICTS.items():
            assert placeholders(d[f"skip.{code}"]) <= usable, f"{lang} skip.{code}"
    assert keys_of(r"skip\.[A-Z_]+") == {f"skip.{c}" for c in builders.SKIP_CODES}


def test_the_plan_codes_the_server_sends_have_sentences():
    for key in ("err.PLAN_CLOSED", "plans.NOTHING_TO_DO"):
        need(key)
    for lang, d in DICTS.items():
        assert placeholders(d["err.PLAN_CLOSED"]) <= {"plan", "plan_status"}, lang
        assert not placeholders(d["plans.NOTHING_TO_DO"]), lang
    assert '"NOTHING_TO_DO"' in (SRC / "service.py").read_text(encoding="utf-8")
    sites = [keyword_names(c) for c in calls("ui_api.py", "UiError") if const(c.args[1]) == "PLAN_CLOSED"]
    assert len(sites) == 2 and all({"plan", "plan_status"} <= s for s in sites)


# --------------------------------------------------------------------------- connecting a cloud (docs/ONBOARDING.md G)

CONNECT_CODES = {"NO_RCLONE", "RCLONE_TOO_OLD", "FLOW_BUSY", "FLOW_FAILED", "FLOW_TIMEOUT", "NAME_TAKEN", "BAD_NAME",
                 "NEEDS_CLIENT", "TEST_FAILED", "NOT_FOUND", "MISSING_FIELD", "NOT_WAITING", "TOKEN_UNREADABLE",
                 "CONFIG_WRITE", "IMPORT_UNSUPPORTED", "IMPORT_ENCRYPTED", "BAD_REQUEST",
                 "AUTH_EXPIRED", "NETWORK", "RCLONE_ERROR"}      # the last three come from classify_failure


def connect_code_sites() -> dict[str, list[set[str]]]:
    """{code: [parameter names of each site]} for every `ConnectError(code, message, **params)` in connections.py,
    ui_api.py and cli.py (a dynamic code, such as a flow's own, is one of the others), plus the codes
    `rclone.classify_failure` gives a failed Test (RCLONE_ERROR sends `detail`)."""
    sites: dict[str, list[set[str]]] = {}
    for path in ("connections.py", "ui_api.py", "cli.py"):
        for call in calls(path, "ConnectError"):
            code = const(call.args[0]) if call.args else None
            if isinstance(code, str):
                sites.setdefault(code, []).append(keyword_names(call))
    fn = next(n for n in ast.walk(ast.parse((SRC / "rclone.py").read_text(encoding="utf-8")))
              if isinstance(n, ast.FunctionDef) and n.name == "classify_failure")
    for node in ast.walk(fn):
        if isinstance(const(node), str) and re.fullmatch(r"[A-Z_]+", const(node)):
            sites.setdefault(const(node), []).append({"detail"} if const(node) == "RCLONE_ERROR" else set())
    return sites


def test_every_connect_code_has_a_sentence_in_both_languages_that_uses_only_what_every_site_sends():
    sites = connect_code_sites()
    assert CONNECT_CODES <= set(sites), sorted(CONNECT_CODES - set(sites))
    for code in sorted(CONNECT_CODES):
        need(f"err.{code}")
        usable = set.intersection(*sites[code])
        for lang, d in DICTS.items():
            assert placeholders(d[f"err.{code}"]) <= usable, (
                f"{lang} err.{code}: {placeholders(d[f'err.{code}']) - usable} is not sent by every site")


def test_the_codes_that_need_a_parameter_send_it_at_every_site():
    sites = connect_code_sites()
    assert sites["MISSING_FIELD"] and all("field" in s for s in sites["MISSING_FIELD"])
    assert sites["IMPORT_UNSUPPORTED"] and all("type" in s for s in sites["IMPORT_UNSUPPORTED"])
    assert sites["RCLONE_ERROR"] and all("detail" in s for s in sites["RCLONE_ERROR"])


def test_the_codes_of_the_removed_setup_screen_are_gone_from_the_page():
    for gone in ("REMOTE_EXISTS", "REMOTE_NOT_FOUND", "CONFIG_LAYOUT", "RCLONE_MISSING", "NO_CONFIG_PATH",
                 "SIGNIN_TIMEOUT", "CANCELLED"):
        assert f"err.{gone}" not in DICTS["en"] and f"err.{gone}" not in DICTS["zh-TW"], gone


# --------------------------------------------------------------------------- the Quarantine view (docs/QUARANTINE.md)

def quarantine_error_sites() -> dict[str, list[set[str]]]:
    """{code: [parameter names of each site]} of every `QuarantineError(code, message, **params)` in quarantine.py,
    including the `_bad_query(param, message)` helper, which raises BAD_QUERY {param}."""
    sites: dict[str, list[set[str]]] = {}
    for call in calls("quarantine.py", "QuarantineError"):
        code = const(call.args[0]) if call.args else None
        if isinstance(code, str):
            sites.setdefault(code, []).append(keyword_names(call))
    if any(True for _ in calls("quarantine.py", "_bad_query")):
        sites.setdefault("BAD_QUERY", []).append({"param"})
    return sites


def op_failed_sites() -> dict[str, list[set[str]]]:
    """{code: [parameter names of each site]} of every `_OpFailed(message, code, **params)` in executor.py with a
    literal code; the others are OP_FAILED with no params."""
    sites: dict[str, list[set[str]]] = {"OP_FAILED": [set()]}
    for call in calls("executor.py", "_OpFailed"):
        code = next((const(k.value) for k in call.keywords if k.arg == "code"), None)
        if code is None and len(call.args) > 1:
            code = const(call.args[1])
        if isinstance(code, str):
            sites.setdefault(code, []).append(keyword_names(call) - {"code"})
    return sites


def test_every_quarantine_error_code_has_a_sentence_that_uses_only_what_every_site_sends():
    """QuarantineError is answered as {error, code, params} (404 NOT_FOUND, 400 BAD_OPS / BAD_QUERY, else 409); the page
    words err.<CODE> from the params."""
    from drivemerge import quarantine

    sites = quarantine_error_sites()
    assert set(sites) == set(quarantine.CODES) == {"NOT_FOUND", "NOTHING_QUARANTINED", "BAD_OPS", "BAD_QUERY"}, sorted(sites)
    spec = {"NOT_FOUND": {"plan"}, "NOTHING_QUARANTINED": {"plan"}, "BAD_OPS": {"plan", "op_ids"}, "BAD_QUERY": {"param"}}
    for code, per_site in sites.items():
        need(f"err.{code}")
        assert all(site == spec[code] for site in per_site), (code, per_site)      # the server sends exactly what the spec says
        usable = set.intersection(*per_site)
        for lang, d in DICTS.items():
            assert placeholders(d[f"err.{code}"]) <= usable, f"{lang} err.{code}: {placeholders(d[f'err.{code}']) - usable}"


def test_every_restore_and_purge_failure_code_has_a_sentence_in_both_languages_that_uses_only_its_params():
    """`ExecResult.failures` carries {code, params}; the page words each as qr.fail.<CODE> and shows the executor's English
    `error` only for OP_FAILED and for a code it does not know."""
    sites = op_failed_sites()
    assert set(sites) == {"ORIGINAL_TAKEN", "QUARANTINE_GONE", "READ_ONLY", "BAD_PATH", "VERIFY_FAILED", "NO_WITNESS_RECORD",
                          "WITNESS_UNPROVEN", "OP_FAILED"}, sorted(sites)
    spec = {"ORIGINAL_TAKEN": {"remote", "path"}, "QUARANTINE_GONE": {"remote", "path"}, "READ_ONLY": {"remote"}, "BAD_PATH": {"path"},
            "VERIFY_FAILED": {"remote", "path"}, "NO_WITNESS_RECORD": {"remote", "path"},
            "WITNESS_UNPROVEN": {"remote", "path", "witness"}, "OP_FAILED": set()}
    for code, per_site in sites.items():
        need(f"qr.fail.{code}")
        assert all(site == spec[code] for site in per_site), (code, per_site)
        usable = set.intersection(*per_site)
        for lang, d in DICTS.items():
            assert placeholders(d[f"qr.fail.{code}"]) <= usable, f"{lang} qr.fail.{code}: {placeholders(d[f'qr.fail.{code}']) - usable}"
    assert keys_of(r"qr\.fail\.[A-Z_]+") == {f"qr.fail.{c}" for c in sites}          # no sentence for a code nobody raises


def test_a_plan_that_is_executing_is_refused_with_a_coded_sentence():
    sites = [keyword_names(c) - {"code"} for c in calls("executor.py", "ExecutionError")
             if any(k.arg == "code" and const(k.value) == "PLAN_EXECUTING" for k in c.keywords)]
    assert sites and all(site == {"plan"} for site in sites), sites
    ui = [keyword_names(c) for c in calls("ui_api.py", "UiError") if const(c.args[1]) == "PLAN_EXECUTING"]
    assert ui and all("plan" in site for site in ui), ui
    need("err.PLAN_EXECUTING")
    for lang, d in DICTS.items():
        assert placeholders(d["err.PLAN_EXECUTING"]) <= {"plan"}, lang


def test_the_quarantine_view_has_its_state_words_and_the_sentences_that_replace_the_terminal_ones():
    for state in ("held", "restored", "purged", "all"):
        need(f"qr.state.{state}")
    for key in ("nav.quarantine", "nav.quarantineTip", "quarantine.tipPage", "quarantine.insidePage", "err.QUARANTINE_PAGE",
                "prog.failedHelpPage"):
        need(key)
    for lang, d in DICTS.items():
        for page_key, terminal_key in (("quarantine.tipPage", "quarantine.tip"), ("quarantine.insidePage", "quarantine.inside"),
                                       ("err.QUARANTINE_PAGE", "err.QUARANTINE"), ("prog.failedHelpPage", "prog.failedHelp")):
            assert "drivemerge plan" not in d[page_key], (lang, page_key)           # the page version never sends anyone to a terminal
            assert "drivemerge plan" in d[terminal_key], (lang, terminal_key)       # the terminal wording stays for the read-only page
        assert placeholders(d["quarantine.tipPage"]) == placeholders(d["quarantine.tip"]) == {"dir"}, lang
        assert placeholders(d["prog.failedHelpPage"]) <= {"id"}, lang


# --------------------------------------------------------------------------- suggestions

def test_every_suggestion_code_has_a_title_and_a_reason():
    codes = set()
    for node in ast.walk(ast.parse((SRC / "suggest.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Dict):
            codes |= {const(v) for k, v in zip(node.keys, node.values) if const(k) == "code"}
    assert codes == {"RELATED", "DUPLICATES"}, codes
    for code in codes:
        need(f"sug.{code}.title")
        need(f"sug.{code}.reason")
    for key in ("sug.RELATED.why.names", "sug.RELATED.why.files", "sug.RELATED.why.same", "sug.RELATED.why.alike"):
        need(key)


# --------------------------------------------------------------------------- the server writes English

def test_server_messages_are_english_only():
    """Chinese in the server's sources belongs to matching (stop words, duplicate markers), never to a sentence."""
    allowed = {"builders.py", "suggest.py"}
    sentences = {"_related_text", "_duplicates_text", "_related_suggestion", "_duplicate_suggestion"}
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value):
                    assert path.name in allowed, f"{path.name}:{node.lineno} has Chinese text in {fn.name}"
                    assert fn.name not in sentences, f"{path.name}:{node.lineno}: {fn.name} words a suggestion in Chinese"


@pytest.mark.parametrize("name", ["index.html"])
def test_the_page_starts_in_english(name):
    html = (SRC / "web" / name).read_text(encoding="utf-8")
    assert '<html lang="en">' in html and "<title>drive-merge workspace</title>" in html
    assert html.index("The drive-merge workspace needs JavaScript.") < html.index("drive-merge 工作區")


# --------------------------------------------------------------------------- the Rules view (docs/RULES.md)

# The params each code carries besides the four every RuleError has (docs/RULES.md section B and C).
RULE_COMMON = {"line", "rule_index", "rule", "key"}
RULE_PARAMS = {
    "TOML_SYNTAX": {"column"}, "UNKNOWN_TOP_KEY": {"suggestion"}, "LAYOUT_NOT_TABLE": set(), "UNKNOWN_LAYOUT_KEY": {"suggestion"},
    "MARKER_HAS_SLASH": {"value"}, "RULES_NOT_ARRAY": set(), "RULE_NOT_TABLE": set(), "UNKNOWN_RULE_KEY": {"suggestion"},
    "NAME_REQUIRED": set(), "NAME_RESERVED": set(), "NAME_DUPLICATE": {"name"}, "TARGET_REQUIRED": set(), "BAD_UNIT_FLAG": set(),
    "TEMPLATE_EMPTY": {"where"}, "TEMPLATE_SYNTAX": {"where"}, "TEMPLATE_FORMAT_SPEC": {"where", "variable"},
    "UNKNOWN_VARIABLE": {"where", "variable", "suggestion"}, "TEMPLATE_DOT_SEGMENT": {"where"}, "UNIT_NEEDS_RELPATH": set(),
    "UNIT_VAR_NOT_UNIT": set(), "BAD_STRING_LIST": set(), "UNKNOWN_CATEGORY": {"value", "suggestion"}, "BAD_EXT": set(),
    "BAD_GLOB": set(), "BAD_REGEX": {"value"}, "BAD_SIZE": {"value"}, "SIZE_RANGE_EMPTY": {"min", "max"},
    "RULES_TOO_LARGE": {"limit"}, "RULES_CANNOT_EDIT": {"reason"}, "TARGET_ROOT_DOT": set(),
}
RULE_WARN_PARAMS = {"RULE_SHADOWED": {"rule", "by"}, "FALLBACK_LOSES_STRUCTURE": set(), "RULE_REMOTE_UNKNOWN": {"rule", "value"}}
RULESTORE_PARAMS = {
    "RULES_VERSION_NOT_FOUND": {"version"}, "RULES_NOT_DRAFT": {"version", "version_status"}, "RULES_DRAFT_CHANGED": {"version", "revision"},
    "RULES_INVALID": {"errors"}, "RULES_ACTIVE_CHANGED": {"active_sha256", "active_version"}, "RULES_FILE_UNWRITABLE": {"path"},
    "RULES_FILE_MISSING": {"path"}, "RULES_FILE_UNREADABLE": {"path"}, "RULES_NO_FILE": set(), "RULES_NOT_ACTIVATABLE": {"version", "version_status"},
    "RULES_BOTH": set(), "DRAFT_UI_ONLY": {"version"}, "PREVIEW_EXPIRED": {"preview_id"}, "PREVIEW_SUPERSEDED": set(),
    "PREVIEW_TOO_LARGE": {"files", "limit"},
}
# Params whose value the page does not write into a sentence as it is (a list of errors the page lists itself, hashes, ids).
NOT_WORDED = {"errors", "active_sha256", "preview_id"}


def rule_error_sites() -> dict[str, list[set[str]]]:
    """{code: [own keyword params of each RuleError(...) site with a literal code]} in classify.py."""
    sites: dict[str, list[set[str]]] = {}
    for call in calls("classify.py", "RuleError"):
        code = const(call.args[0]) if call.args else None
        if isinstance(code, str):
            sites.setdefault(code, []).append(keyword_names(call) - RULE_COMMON - {"message"})
    return sites


def test_the_rule_code_lists_are_the_ones_the_spec_names():
    from drivemerge import classify, rulestore

    assert set(classify.RULE_CODES) == set(RULE_PARAMS), sorted(set(classify.RULE_CODES) ^ set(RULE_PARAMS))
    assert set(classify.RULE_WARN_CODES) == set(RULE_WARN_PARAMS), sorted(set(classify.RULE_WARN_CODES) ^ set(RULE_WARN_PARAMS))
    assert set(rulestore.CODES) == set(RULESTORE_PARAMS), sorted(set(rulestore.CODES) ^ set(RULESTORE_PARAMS))


def test_every_rule_error_code_has_a_sentence_in_both_languages_that_uses_only_the_params_the_server_sends():
    from drivemerge import classify

    sites = rule_error_sites()
    for code in classify.RULE_CODES:
        need(f"rulecheck.{code}")
        own = RULE_PARAMS[code]
        # the server really sends nothing beyond what the spec says (a site may send fewer: those words are not used)
        for site in sites.get(code, []):
            assert site <= own, f"{code}: a site sends {site - own} that the spec does not list"
        for lang, d in DICTS.items():
            assert placeholders(d[f"rulecheck.{code}"]) <= RULE_COMMON | own, (
                f"{lang} rulecheck.{code}: {placeholders(d[f'rulecheck.{code}']) - RULE_COMMON - own}")
        if "suggestion" in own:                       # "did you mean ...?" follows the sentence when the server has a suggestion
            need(f"rulecheck.{code}.hint")
            for lang, d in DICTS.items():
                assert placeholders(d[f"rulecheck.{code}.hint"]) == {"suggestion"}, (lang, code)
    assert keys_of(r"rulecheck\.[A-Z_]+") == {f"rulecheck.{c}" for c in classify.RULE_CODES}       # no sentence for a code nobody raises
    # a TOML error without a column and the three reasons of an edit that could not be made
    need("rulecheck.TOML_SYNTAX.nocol")
    for reason in ("unparsed", "not_found", "mismatch"):
        need(f"rulecheck.RULES_CANNOT_EDIT.reason.{reason}")
    for lang, d in DICTS.items():
        assert not placeholders(d["rulecheck.TOML_SYNTAX.nocol"]), lang
        assert placeholders(d["rulecheck.RULES_CANNOT_EDIT"]) == {"reason"}, lang
        assert {f"rulecheck.RULES_CANNOT_EDIT.reason.{r}" for r in ("unparsed", "not_found", "mismatch")} <= set(d)
    # the words for `where` of the template codes
    need("rules.tpl.target")
    need("rules.tpl.fallback")


def test_every_rule_warning_code_has_a_sentence_that_uses_only_its_params():
    from drivemerge import classify

    for code in classify.RULE_WARN_CODES:
        need(f"rulewarn.{code}")
        for lang, d in DICTS.items():
            assert placeholders(d[f"rulewarn.{code}"]) <= RULE_COMMON | RULE_WARN_PARAMS[code], (
                f"{lang} rulewarn.{code}: {placeholders(d[f'rulewarn.{code}']) - RULE_COMMON - RULE_WARN_PARAMS[code]}")
    assert keys_of(r"rulewarn\.[A-Z_]+") == {f"rulewarn.{c}" for c in classify.RULE_WARN_CODES}
    # RULE_SHADOWED names the earlier rule, which is the one thing the reader needs
    for d in DICTS.values():
        assert "{by}" in d["rulewarn.RULE_SHADOWED"]
        assert "{value}" in d["rulewarn.RULE_REMOTE_UNKNOWN"]


def test_every_rules_refusal_has_a_sentence_in_both_languages_that_uses_only_its_params():
    from drivemerge import rulestore

    for code in rulestore.CODES:
        need(f"err.{code}")
        own = RULESTORE_PARAMS[code]
        for lang, d in DICTS.items():
            used = placeholders(d[f"err.{code}"])
            assert used <= own, f"{lang} err.{code}: {used - own}"
            assert not used & NOT_WORDED, f"{lang} err.{code} writes {used & NOT_WORDED} into the sentence"
    # a refusal that says what state a version is in has the state as a word
    for status in ("draft", "proposed", "active", "retired", "dismissed"):
        need(f"rstatus.{status}")


def test_the_rules_view_has_its_words_for_sources_statuses_authors_and_changes_in_both_languages():
    for key in ("nav.rules", "nav.rulesTip", "rules.railTip", "plans.newReorganize", "plans.sum.reorganize", "reorg.title", "reorg.hint",
                "plan.rules.title", "plan.rules.proposed", "plan.rules.inline", "plan.rules.version", "plan.rules.replaced", "ex.rules"):
        need(key)
    for source in ("file", "default", "version", "inline"):
        need(f"rsrc.{source}")
    for by in ("ui", "cli", "file", "mcp", "api", "default"):
        need(f"rby.{by}")
    for change in ("added", "removed", "changed", "moved"):
        need(f"rchange.{change}")
        for lang, d in DICTS.items():
            assert placeholders(d[f"rchange.{change}"]) == {"n"}, (lang, change)
    for flt in ("changed", "fallback", "all"):
        need(f"rules.files.{flt}")
    for part in ("outline", "text", "preview"):
        need(f"rules.pane.{part}")
    # no sentence of the Rules view tells anyone to use only the terminal for what the page now does
    for lang, d in DICTS.items():
        assert "cons.rulesHelp" not in d, lang
    for lang, d in DICTS.items():
        assert "drivemerge rules activate" not in d["rules.activated"], lang
