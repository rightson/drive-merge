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
from drivemerge import simulate

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
    loose = {"app.language", "common.listSep", "sug.q", "sug.sep", "sug.joinReasons"}      # punctuation or both languages
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
