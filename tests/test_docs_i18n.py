"""The documentation is bilingual and the two languages must not drift apart.

Policy: English is the canonical language. `X.md` is English and `X.zh-TW.md` is its
Traditional Chinese mirror, side by side in the same directory (repository root and
`docs/`). Directly under its H1 title every such file carries a language switch line:

    X.md         **English** | [繁體中文](X.zh-TW.md)
    X.zh-TW.md   [English](X.md) | **繁體中文**

A translation keeps the structure of the original: the same headings, the same tables,
the same decision ids (D1, D2, ...), the same violation / staging codes, the same code
blocks and links (pointing at the zh-TW twin when there is one). These tests check
exactly that, so a doc edit in one language that forgets the other fails here.

Stdlib only; no network, no database.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC_DIRS = [ROOT, ROOT / "docs"]
ZH_SUFFIX = ".zh-TW.md"


def _markdown_files() -> list[Path]:
    """Every Markdown file directly in the repository root and in docs/."""
    return sorted(p for d in DOC_DIRS if d.is_dir() for p in d.glob("*.md"))


ZH_FILES = [p for p in _markdown_files() if p.name.endswith(ZH_SUFFIX)]
EN_FILES = [p for p in _markdown_files() if not p.name.endswith(ZH_SUFFIX)]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def english_twin(zh: Path) -> Path:
    return zh.with_name(zh.name[: -len(ZH_SUFFIX)] + ".md")


def zh_twin(en: Path) -> Path:
    return en.with_name(en.stem + ZH_SUFFIX)


# ---------------------------------------------------------------- parsing

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(\S.*?)[ \t]*$")
_DECISION_ID = re.compile(r"(?<![A-Za-z0-9_])D\d+(?![A-Za-z0-9_])")
_CODE_WORD = re.compile(r"[A-Z][A-Z0-9_]{2,}")  # QUARANTINE, NAME_EXISTS, CONTENT_LOST
_CODE_INSIDE = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![A-Za-z0-9_])")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_LINK_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass
class Doc:
    path: Path
    lines: list[str]
    prose: list[tuple[int, str]] = field(default_factory=list)  # (1-based line no, text) outside fences
    blocks: list[str] = field(default_factory=list)  # fenced code blocks: info string + body

    @property
    def name(self) -> str:
        return rel(self.path)

    def prose_text(self) -> str:
        return "\n".join(text for _, text in self.prose)

    def headings(self) -> list[tuple[int, str]]:
        found = []
        for _, text in self.prose:
            m = _HEADING.match(text)
            if m:
                found.append((len(m.group(1)), m.group(2)))
        return found

    def code_spans(self) -> list[str]:
        return [m.group(2).strip() for m in _INLINE_CODE.finditer(self.prose_text())]

    def prose_without_code_spans(self) -> list[tuple[int, str]]:
        return [(n, _INLINE_CODE.sub("", text)) for n, text in self.prose]

    def tables(self) -> list[list[str]]:
        """Runs of consecutive prose lines that start with '|', one list of lines per table."""
        tables: list[list[str]] = []
        current: list[str] = []
        previous = 0
        for no, text in self.prose:
            if text.lstrip().startswith("|"):
                if current and no != previous + 1:  # a code fence (or anything else) sat in between
                    tables.append(current)
                    current = []
                current.append(text)
                previous = no
            elif current:
                tables.append(current)
                current = []
        if current:
            tables.append(current)
        return tables

    def relative_links(self) -> list[str]:
        """Link targets that point at a local file (no scheme, not a bare #anchor), fragment removed."""
        targets = []
        for _, text in self.prose_without_code_spans():
            found = [m.group(1) for m in _LINK.finditer(text)]
            definition = _LINK_DEFINITION.match(text)
            if definition:
                found.append(definition.group(1))
            for target in found:
                if _URL_SCHEME.match(target) or target.startswith("#"):
                    continue
                path = unquote(re.split(r"[#?]", target, maxsplit=1)[0])
                if path:
                    targets.append(path)
        return targets


def parse(path: Path) -> Doc:
    doc = Doc(path=path, lines=path.read_text(encoding="utf-8").splitlines())
    fence: tuple[str, int] | None = None
    info = ""
    body: list[str] = []
    opened_at = 0
    for no, line in enumerate(doc.lines, 1):
        if fence is None:
            m = _FENCE_OPEN.match(line)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = (m.group(1)[0], len(m.group(1)))
                info = m.group(2).strip()
                body = []
                opened_at = no
            else:
                doc.prose.append((no, line))
        else:
            closing = re.match(rf"^ {{0,3}}({re.escape(fence[0])}{{{fence[1]},}})[ \t]*$", line)
            if closing:
                doc.blocks.append(info + "\n" + "\n".join(body))
                fence = None
            else:
                body.append(line)
    assert fence is None, f"{rel(path)}: the code fence opened on line {opened_at} is never closed"
    return doc


def pair(zh_path: Path) -> tuple[Doc, Doc]:
    twin = english_twin(zh_path)
    assert twin.is_file(), f"{rel(zh_path)} has no English twin {rel(twin)}"
    return parse(twin), parse(zh_path)


def split_cells(row: str) -> list[str]:
    s = row.strip().removeprefix("|")
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return re.split(r"(?<!\\)\|", s)


def code_tokens(doc: Doc) -> Counter:
    """Backticked tokens that look like codes: NAME_EXISTS, CONTENT_LOST, QUARANTINE, AI_AGENT ..."""
    tokens: Counter = Counter()
    for span in doc.code_spans():
        if _CODE_WORD.fullmatch(span):
            tokens[span] += 1
        else:
            tokens.update(_CODE_INSIDE.findall(span))
    return tokens


def decision_ids(doc: Doc) -> Counter:
    return Counter(_DECISION_ID.findall(doc.prose_text()))


def counter_diff(en: Counter, zh: Counter) -> str:
    lines = []
    for key in sorted(set(en) | set(zh)):
        if en[key] != zh[key]:
            lines.append(f"  {key!r}: English {en[key]}x, zh-TW {zh[key]}x")
    return "\n".join(lines)


def switch_lines(doc_path: Path) -> tuple[str, str]:
    """(English-file switch line, zh-TW-file switch line) for the pair that `doc_path` belongs to."""
    stem = doc_path.name[: -len(ZH_SUFFIX)] if doc_path.name.endswith(ZH_SUFFIX) else doc_path.stem
    return (
        f"**English** | [繁體中文]({stem}{ZH_SUFFIX})",
        f"[English]({stem}.md) | **繁體中文**",
    )


PAIR_IDS = [rel(p) for p in ZH_FILES]


# ---------------------------------------------------------------- tests


def test_there_are_translations_to_check():
    assert ZH_FILES, "no *.zh-TW.md found in the repository root or docs/: the bilingual docs are gone"


@pytest.mark.parametrize("en", EN_FILES, ids=[rel(p) for p in EN_FILES])
def test_every_english_doc_has_a_zh_tw_twin(en: Path):
    twin = zh_twin(en)
    assert twin.is_file(), (
        f"{rel(en)} is English-only. The docs are bilingual: add {rel(twin)} (Traditional Chinese, "
        f"Taiwan usage) with the same structure."
    )


@pytest.mark.parametrize("zh", ZH_FILES, ids=PAIR_IDS)
def test_english_twin_exists(zh: Path):
    twin = english_twin(zh)
    assert twin.is_file(), f"{rel(zh)} has no English twin {rel(twin)} (English is the canonical language)"


@pytest.mark.parametrize("zh", ZH_FILES, ids=PAIR_IDS)
def test_h1_is_followed_by_the_language_switch_line(zh: Path):
    en_doc, zh_doc = pair(zh)
    en_switch, zh_switch = switch_lines(zh)
    for doc, expected in ((en_doc, en_switch), (zh_doc, zh_switch)):
        nonblank = [(no, text) for no, text in enumerate(doc.lines, 1) if text.strip()]
        assert len(nonblank) >= 2, f"{doc.name}: the file is empty"
        (h1_no, h1), (switch_no, switch) = nonblank[0], nonblank[1]
        assert re.match(r"^# \S", h1), f"{doc.name}:{h1_no}: the first line must be the H1 title, got {h1!r}"
        assert switch.strip() == expected, (
            f"{doc.name}:{switch_no}: the line directly under the H1 must be the language switch line\n"
            f"  expected: {expected}\n  found:    {switch.strip()}"
        )


@pytest.mark.parametrize("zh", ZH_FILES, ids=PAIR_IDS)
def test_heading_levels_match(zh: Path):
    en_doc, zh_doc = pair(zh)
    en_h, zh_h = en_doc.headings(), zh_doc.headings()
    if [level for level, _ in en_h] != [level for level, _ in zh_h]:
        def fmt(hs):
            return [f"{'#' * level} {text}" for level, text in hs]

        diff = "\n".join(difflib.unified_diff(fmt(en_h), fmt(zh_h), "English", "zh-TW", lineterm="", n=2))
        pytest.fail(
            f"{zh_doc.name}: the heading levels differ from {en_doc.name} "
            f"({len(en_h)} English headings, {len(zh_h)} zh-TW headings)\n{diff}"
        )


@pytest.mark.parametrize("zh", ZH_FILES, ids=PAIR_IDS)
def test_decision_ids_match(zh: Path):
    en_doc, zh_doc = pair(zh)
    en_ids, zh_ids = decision_ids(en_doc), decision_ids(zh_doc)
    assert en_ids == zh_ids, f"{zh_doc.name}: decision ids (D1, D2, ...) differ from {en_doc.name}\n" + counter_diff(
        en_ids, zh_ids
    )


@pytest.mark.parametrize("zh", ZH_FILES, ids=PAIR_IDS)
def test_violation_and_staging_codes_match(zh: Path):
    en_doc, zh_doc = pair(zh)
    en_codes, zh_codes = code_tokens(en_doc), code_tokens(zh_doc)
    assert en_codes == zh_codes, (
        f"{zh_doc.name}: backticked codes (CONTENT_LOST, NAME_EXISTS, ...) differ from {en_doc.name}\n"
        + counter_diff(en_codes, zh_codes)
    )


@pytest.mark.parametrize("zh", ZH_FILES, ids=PAIR_IDS)
def test_fenced_code_blocks_match(zh: Path):
    en_doc, zh_doc = pair(zh)
    en_blocks, zh_blocks = Counter(en_doc.blocks), Counter(zh_doc.blocks)
    if en_blocks != zh_blocks:
        only_en = list((en_blocks - zh_blocks).elements())
        only_zh = list((zh_blocks - en_blocks).elements())
        detail = []
        for i, block in enumerate(only_en):
            detail.append(f"--- only in {en_doc.name} (block {i + 1}):\n{block}")
        for i, block in enumerate(only_zh):
            detail.append(f"+++ only in {zh_doc.name} (block {i + 1}):\n{block}")
        if len(only_en) == len(only_zh):
            for a, b in zip(only_en, only_zh):
                detail.append("first difference:\n" + "\n".join(difflib.unified_diff(
                    a.splitlines(), b.splitlines(), "English", "zh-TW", lineterm="", n=1)))
        pytest.fail(
            f"{zh_doc.name}: fenced code blocks must be byte-identical to {en_doc.name} "
            f"({len(en_doc.blocks)} English, {len(zh_doc.blocks)} zh-TW)\n" + "\n".join(detail)
        )


@pytest.mark.parametrize("zh", ZH_FILES, ids=PAIR_IDS)
def test_tables_have_the_same_shape(zh: Path):
    en_doc, zh_doc = pair(zh)

    def shape(doc: Doc) -> list[tuple[int, int]]:
        shapes = []
        for table in doc.tables():
            widths = {len(split_cells(row)) for row in table}
            assert len(widths) == 1, (
                f"{doc.name}: a table starting with {table[0][:60]!r} has rows with different "
                f"cell counts {sorted(widths)} (an unescaped '|' inside a cell?)"
            )
            shapes.append((len(table), widths.pop()))
        return shapes

    en_shape, zh_shape = shape(en_doc), shape(zh_doc)
    assert en_shape == zh_shape, (
        f"{zh_doc.name}: tables differ from {en_doc.name}; (rows, columns) per table in document order\n"
        f"  English: {en_shape}\n  zh-TW:   {zh_shape}"
    )


@pytest.mark.parametrize("doc_path", EN_FILES + ZH_FILES, ids=[rel(p) for p in EN_FILES + ZH_FILES])
def test_relative_links_resolve(doc_path: Path):
    doc = parse(doc_path)
    broken = [t for t in doc.relative_links() if not (doc_path.parent / t).exists()]
    assert not broken, f"{doc.name}: links to files that do not exist: {sorted(set(broken))}"


@pytest.mark.parametrize("zh", ZH_FILES, ids=PAIR_IDS)
def test_zh_links_point_at_zh_twins(zh: Path):
    """The same links, except that a link to an English page that has a zh-TW twin must go to the twin.

    The language switch line is left out: it links to the other language by design.
    """
    en_doc, zh_doc = pair(zh)

    def mirrored(target: str) -> str:
        if target.endswith(".md") and not target.endswith(ZH_SUFFIX):
            twin = target[: -len(".md")] + ZH_SUFFIX
            if (en_doc.path.parent / twin).exists():
                return twin
        return target

    expected = Counter(mirrored(t) for t in en_doc.relative_links())
    found = Counter(zh_doc.relative_links())
    expected[zh.name] -= 1  # the English file's switch line links to its zh-TW twin ...
    found[english_twin(zh).name] -= 1  # ... and the zh-TW file's switch line to its English one
    expected, found = +expected, +found  # drop the zero (and negative) entries
    assert expected == found, (
        f"{zh_doc.name}: the relative links must mirror {en_doc.name}, pointing at *.zh-TW.md twins where they exist\n"
        + counter_diff(expected, found)
    )
