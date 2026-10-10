"""Folder profiles, atomic-unit detection and rule-based target layout (the "blueprint").

Sorting purely by file type destroys context, so the layout works on *units*:
a folder that must stay together (a code repo, an app bundle, anything the
rules mark) moves as a whole and keeps its internal structure; only loose files
are sorted one by one. An LLM never sees every file: it reads `folder_profiles`
(compact aggregates) and writes TOML rules, which `plan_layout` then applies
deterministically.

Everything here is a pure function of the catalog: SQL reads only, nothing is
written and nothing is ever marked for deletion. Two files that map to the same
target are *not* resolved here; the plan builder knows file identity and does.
"""

from __future__ import annotations

import difflib
import fnmatch
import posixpath
import re
import sqlite3
import string
import tomllib
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from importlib import resources
from itertools import groupby
from typing import Callable

from . import db
from .categories import CATEGORIES, category_of

# --------------------------------------------------------------------------- rules

# Every variable a target / fallback template may use. See plan_layout for what
# each one means for loose files, unit rules and unit files under the fallback.
VARIABLES: tuple[str, ...] = (
    "name", "stem", "ext", "category", "year", "month", "day",
    "remote", "parent", "dir", "path", "unit", "relpath",
)
_DATE_VARS = frozenset({"year", "month", "day"})

DEFAULT_UNIT_MARKERS: tuple[str, ...] = (
    ".git", "package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "Gemfile", "composer.json", "*.xcodeproj", "*.sln", "*.csproj",
)
DEFAULT_BUNDLE_PATTERNS: tuple[str, ...] = (
    "*.pages", "*.key", "*.numbers", "*.app", "*.photoslibrary", "*.fcpbundle", "*.band",
    "*.rtfd", "*.logicx", "*.aplibrary", "*.imovielibrary",
)
DEFAULT_FALLBACK = "Unsorted/{remote}/{path}"

_TOP_KEYS = ("layout", "rule")
_LAYOUT_KEYS = ("unit_markers", "bundle_patterns", "fallback")
_RULE_KEYS = ("name", "target", "category", "ext", "path_glob", "name_regex",
              "min_size", "max_size", "remote", "unit")
_CATEGORY_NAMES = frozenset(CATEGORIES) | {"other"}


class RuleError(ValueError):
    """A rules file is invalid. The message is written for an LLM to read and fix the rules."""


@dataclass
class Rule:
    name: str
    target: str
    category: tuple[str, ...] = ()
    ext: tuple[str, ...] = ()
    path_glob: str | None = None
    name_regex: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    remote: tuple[str, ...] = ()
    unit: bool = False


@dataclass
class RuleSet:
    rules: list[Rule] = field(default_factory=list)
    unit_markers: tuple[str, ...] = DEFAULT_UNIT_MARKERS
    bundle_patterns: tuple[str, ...] = DEFAULT_BUNDLE_PATTERNS
    fallback: str = DEFAULT_FALLBACK


def _suggest(word: str, options) -> str:
    close = difflib.get_close_matches(word, list(options), n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _check_template(template: object, where: str) -> frozenset[str]:
    """Validate a path template and return the variables it uses."""
    if not isinstance(template, str) or not template.strip("/"):
        raise RuleError(f"{where}: must be a non-empty path template such as \"Photos/{{year}}/{{name}}\"; got {template!r}")
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise RuleError(f"{where}: bad template {template!r} ({exc}); write a literal brace as {{{{ or }}}}") from exc
    used: set[str] = set()
    for _literal, fname, spec, conv in parsed:
        if fname is None:
            continue
        if spec or conv:
            raise RuleError(f"{where}: in {template!r}, {{{fname}}} must be a bare variable "
                            "(format specs like :>5 and conversions like !r are not supported)")
        if fname not in VARIABLES:
            raise RuleError(f"{where}: unknown template variable {{{fname}}} in {template!r}"
                            f"{_suggest(fname, VARIABLES)}; valid variables: {', '.join(VARIABLES)}")
        used.add(fname)
    if any(seg in (".", "..") for seg in template.split("/")):
        raise RuleError(f"{where}: template {template!r} contains a '.' or '..' path segment; targets must stay inside the destination")
    return frozenset(used)


def _str_list(value: object, where: str, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise RuleError(f"{where}: {key} must be a list of non-empty strings, e.g. {key} = [\"a\", \"b\"]; got {value!r}")
    return tuple(value)


def _size(value: object, where: str, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuleError(f"{where}: {key} must be a non-negative integer number of bytes; got {value!r}")
    return value


def _parse_rule(index: int, raw: object, seen: set[str]) -> Rule:
    if not isinstance(raw, dict):
        raise RuleError(f"rule #{index}: each rule must be a [[rule]] table")
    label = f"rule #{index}" + (f" ({raw['name']!r})" if isinstance(raw.get("name"), str) else "")
    for key in raw:
        if key not in _RULE_KEYS:
            raise RuleError(f"{label}: unknown key {key!r}{_suggest(key, _RULE_KEYS)}; valid keys: {', '.join(_RULE_KEYS)}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuleError(f"{label}: 'name' is required and must be a non-empty string")
    if name == "fallback":
        raise RuleError(f"{label}: the name 'fallback' is reserved for files no rule matched")
    if name in seen:
        raise RuleError(f"{label}: duplicate rule name {name!r}; names must be unique")
    seen.add(name)
    if "target" not in raw:
        raise RuleError(f"{label}: 'target' is required, e.g. target = \"Photos/{{year}}/{{name}}\"")
    unit = raw.get("unit", False)
    if not isinstance(unit, bool):
        raise RuleError(f"{label}: unit must be true or false; got {unit!r}")
    used = _check_template(raw["target"], label)
    if unit and "relpath" not in used:
        raise RuleError(f"{label}: a unit rule's target must contain {{relpath}} so the files inside the unit "
                        f"keep their internal structure (e.g. \"Projects/{{unit}}/{{relpath}}\"); got {raw['target']!r}")
    if not unit and "unit" in used:
        raise RuleError(f"{label}: {{unit}} only exists for unit rules; add unit = true or drop it from the target")

    categories = _str_list(raw.get("category", []), label, "category")
    for c in categories:
        if c not in _CATEGORY_NAMES:
            raise RuleError(f"{label}: unknown category {c!r}{_suggest(c, _CATEGORY_NAMES)}; valid: {', '.join(sorted(_CATEGORY_NAMES))}")
    exts = tuple(e.lower().lstrip(".") for e in _str_list(raw.get("ext", []), label, "ext"))
    if not all(exts):
        raise RuleError(f"{label}: ext entries must be extensions such as \"jpg\" (lowercase, no dot)")
    glob = raw.get("path_glob")
    if glob is not None and (not isinstance(glob, str) or not glob):
        raise RuleError(f"{label}: path_glob must be a non-empty string such as \"*/Screenshots/*\"")
    regex = raw.get("name_regex")
    if regex is not None:
        if not isinstance(regex, str) or not regex:
            raise RuleError(f"{label}: name_regex must be a non-empty string")
        try:
            re.compile(regex)
        except re.error as exc:
            raise RuleError(f"{label}: name_regex {regex!r} is not a valid regular expression: {exc}") from exc
    lo = _size(raw["min_size"], label, "min_size") if "min_size" in raw else None
    hi = _size(raw["max_size"], label, "max_size") if "max_size" in raw else None
    if lo is not None and hi is not None and lo > hi:
        raise RuleError(f"{label}: min_size ({lo}) is larger than max_size ({hi}); nothing can match")
    return Rule(name=name, target=raw["target"], category=categories, ext=exts, path_glob=glob,
                name_regex=regex, min_size=lo, max_size=hi,
                remote=_str_list(raw.get("remote", []), label, "remote"), unit=unit)


def load_rules(text: str) -> RuleSet:
    """Parse and strictly validate a rules file; raises RuleError with a fixable message."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuleError(f"invalid TOML: {exc}") from exc
    for key in data:
        if key not in _TOP_KEYS:
            raise RuleError(f"unknown top-level key {key!r}{_suggest(key, _TOP_KEYS)}; "
                            "use [layout] for layout settings and one [[rule]] table per rule")
    layout = data.get("layout", {})
    if not isinstance(layout, dict):
        raise RuleError("'layout' must be a table: write [layout]")
    for key in layout:
        if key not in _LAYOUT_KEYS:
            raise RuleError(f"[layout]: unknown key {key!r}{_suggest(key, _LAYOUT_KEYS)}; valid keys: {', '.join(_LAYOUT_KEYS)}")
    markers = _str_list(layout["unit_markers"], "[layout]", "unit_markers") if "unit_markers" in layout else DEFAULT_UNIT_MARKERS
    if any("/" in m for m in markers):
        raise RuleError("[layout]: unit_markers match a child's name, so they cannot contain '/'")
    bundles = _str_list(layout["bundle_patterns"], "[layout]", "bundle_patterns") if "bundle_patterns" in layout else DEFAULT_BUNDLE_PATTERNS
    fallback = layout.get("fallback", DEFAULT_FALLBACK)
    _check_template(fallback, "[layout] fallback")

    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        raise RuleError("'rule' must be an array of tables: write [[rule]] once per rule")
    seen: set[str] = set()
    rules = [_parse_rule(i, raw, seen) for i, raw in enumerate(raw_rules, start=1)]
    return RuleSet(rules=rules, unit_markers=tuple(markers), bundle_patterns=tuple(bundles), fallback=fallback)


def default_rules() -> RuleSet:
    """The packaged default rules (a sensible layout for a personal archive)."""
    text = resources.files("drivemerge").joinpath("default_rules.toml").read_text(encoding="utf-8")
    return load_rules(text)


# --------------------------------------------------------------------------- dates and rendering

_SEP_DATE = re.compile(r"(?<!\d)(199\d|20\d\d)([-_.])(\d\d)\2(\d\d)(?!\d)")
_COMPACT_DATE = re.compile(r"(?<!\d)(199\d|20\d\d)(\d\d)(\d\d)(?!\d)")
_MTIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_UNKNOWN = ("unknown", "unknown", "unknown")


def _valid_ymd(y: str, m: str, d: str) -> bool:
    try:
        date(int(y), int(m), int(d))
    except ValueError:
        return False
    return 1990 <= int(y) <= 2099


def date_from_name(name: str) -> tuple[str, str, str] | None:
    """('2019','03','04') for names like IMG_20190304_123456.jpg or '2019-03-04 Trip.jpg'.

    A date inside a longer digit run (order numbers, unix timestamps) is not
    a date, and impossible calendar days (20191399) are rejected.
    """
    best: tuple[int, tuple[str, str, str]] | None = None
    for rx, groups in ((_SEP_DATE, (1, 3, 4)), (_COMPACT_DATE, (1, 2, 3))):
        for m in rx.finditer(name):
            ymd = (m.group(groups[0]), m.group(groups[1]), m.group(groups[2]))
            if _valid_ymd(*ymd):
                if best is None or m.start() < best[0]:
                    best = (m.start(), ymd)
                break
    return best[1] if best else None


def _mtime_date(mtime: str | None) -> tuple[str, str, str] | None:
    """Date part of an ISO mtime; epoch-zero style placeholder years count as unknown."""
    m = _MTIME.match(mtime) if mtime else None
    if m and _valid_ymd(*m.groups()):
        return m.group(1), m.group(2), m.group(3)
    return None


def _date_for(name: str, mtime: str | None) -> tuple[str, str, str]:
    return date_from_name(name) or _mtime_date(mtime) or _UNKNOWN


@lru_cache(maxsize=2048)
def _parse_segment(segment: str) -> tuple[tuple[str, str | None], ...]:
    try:
        parsed = list(string.Formatter().parse(segment))
    except ValueError as exc:
        raise RuleError(f"bad template segment {segment!r}: {exc}") from exc
    for _literal, fname, spec, conv in parsed:
        if fname is not None and (spec or conv):
            raise RuleError(f"template variable {{{fname}}} must be bare (no format spec or conversion)")
    return tuple((literal, fname) for literal, fname, _spec, _conv in parsed)


def render(template: str, variables: dict[str, str]) -> str:
    """Fill a template and sanitise it into a relative path.

    Each template segment is filled on its own: a literal '//' or leading/trailing
    '/' collapses, but a segment whose variables are all empty (e.g. {parent} at
    the root) becomes '_' so the target stays predictable. '.' and '..' segments
    are rejected. Names are never normalised, so NFD and CJK names pass through
    unchanged.
    """
    out: list[str] = []
    for segment in template.split("/"):
        if not segment:
            continue
        pieces: list[str] = []
        for literal, fname in _parse_segment(segment):
            pieces.append(literal)
            if fname is not None:
                if fname not in variables:
                    raise RuleError(f"unknown template variable {{{fname}}} in {template!r}; valid variables: {', '.join(VARIABLES)}")
                pieces.append(str(variables[fname]))
        parts = [p for p in "".join(pieces).split("/") if p] or ["_"]
        for part in parts:
            if part in (".", ".."):
                raise RuleError(f"template {template!r} rendered a {part!r} path segment, which is not allowed")
        out.extend(parts)
    return "/".join(out) or "_"


# --------------------------------------------------------------------------- matching helpers

def _norm(s: str) -> str:
    """NFC + casefold, like entries.name_norm: macOS/iCloud hand out NFD names."""
    return unicodedata.normalize("NFC", s).casefold()


def _ancestors(path: str):
    """Proper ancestor folders of a path, shortest first: 'a/b/c' -> 'a', 'a/b'."""
    pos = path.find("/")
    while pos != -1:
        yield path[:pos]
        pos = path.find("/", pos + 1)


def _under(prefix: str, *, include_self: bool = False, col: str = "path") -> tuple[str, tuple]:
    """SQL for 'inside folder prefix' without LIKE ('_' and '%' are legal in names).

    Paths compare bytewise, so everything starting with 'prefix/' lies in
    ['prefix/', 'prefix0') because '0' is the character after '/'.
    """
    if not prefix:
        return "1 = 1", ()
    inside = f"({col} >= ? AND {col} < ?)"
    args: tuple = (prefix + "/", prefix + "0")
    if include_self:
        return f"({col} = ? OR {inside})", (prefix, *args)
    return inside, args


def _remotes(conn: sqlite3.Connection, remote_ids: list[int] | None) -> list[tuple[int, str]]:
    rows = conn.execute("SELECT id, name FROM remotes ORDER BY id").fetchall()
    wanted = None if remote_ids is None else set(remote_ids)
    return [(r[0], r[1]) for r in rows if wanted is None or r[0] in wanted]


def _name_ext(name: str) -> str:
    """Extension of a folder name ('Foo.pages' -> 'pages'); '' for dotfiles and plain names."""
    stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and stem and ext else ""


def _stem(name: str, ext: str) -> str:
    if ext and name[-(len(ext) + 1):].lower() == "." + ext:
        return name[: -(len(ext) + 1)]
    return name


class _Patterns:
    """fnmatch patterns tested against a name; patterns containing '/' test the whole path.

    Matching is on NFC+casefolded text. Returns (index, pattern) of the first
    pattern in list order that matches, so reasons are deterministic.
    """

    def __init__(self, patterns: tuple[str, ...]):
        self.literals: dict[str, tuple[int, str]] = {}
        self.globs: list[tuple[int, str, re.Pattern]] = []
        self.path_globs: list[tuple[int, str, re.Pattern]] = []
        for i, p in enumerate(patterns):
            n = _norm(p)
            if "/" in n:
                self.path_globs.append((i, p, re.compile(fnmatch.translate(n))))
            elif any(ch in n for ch in "*?["):
                self.globs.append((i, p, re.compile(fnmatch.translate(n))))
            else:
                self.literals.setdefault(n, (i, p))

    def match_name(self, name_norm: str) -> tuple[int, str] | None:
        best = self.literals.get(name_norm)
        for i, p, rx in self.globs:
            if best is not None and best[0] < i:
                break
            if rx.match(name_norm):
                return (i, p) if best is None or i < best[0] else best
        return best

    def match_path(self, path_norm: str) -> tuple[int, str] | None:
        for i, p, rx in self.path_globs:
            if rx.match(path_norm):
                return i, p
        return None


# --------------------------------------------------------------------------- units

@dataclass
class Unit:
    remote_id: int
    path: str
    reason: str   # 'marker:.git' (a child matched) or 'bundle:*.pages' (own name matched)


def detect_units(conn: sqlite3.Connection, ruleset: RuleSet, *, remote_ids: list[int] | None = None) -> list[Unit]:
    """Folders that must move as a whole, outermost only, ordered by (remote, path).

    A folder is a unit when a live child matches one of `unit_markers` (any
    entry, file or folder) or when its own name matches one of `bundle_patterns`
    (folders only; a pattern containing '/' is matched against the whole path,
    which lets rules mark arbitrary folders such as 'Clients/*'). A remote's
    root is never a unit. One pass over the live rows of each remote.
    """
    markers = _Patterns(tuple(ruleset.unit_markers))
    bundles = _Patterns(tuple(ruleset.bundle_patterns))
    units: list[Unit] = []
    for rid, _name in _remotes(conn, remote_ids):
        cand: dict[str, tuple[tuple[int, int], str]] = {}

        def offer(path: str, rank: tuple[int, int], reason: str) -> None:
            if path not in cand or rank < cand[path][0]:
                cand[path] = (rank, reason)

        rows = conn.execute("SELECT path, parent, name_norm, is_dir FROM entries WHERE remote_id = ? AND present = 1", (rid,))
        for path, parent, name_norm, is_dir in rows:
            if parent:
                hit = markers.match_name(name_norm)
                if hit:
                    offer(parent, (1, hit[0]), f"marker:{hit[1]}")
            if is_dir:
                hit = bundles.match_name(name_norm)
                if bundles.path_globs:
                    by_path = bundles.match_path(_norm(path))
                    if by_path and (hit is None or by_path[0] < hit[0]):
                        hit = by_path
                if hit:
                    offer(path, (0, hit[0]), f"bundle:{hit[1]}")
        for path in sorted(cand):
            if not any(a in cand for a in _ancestors(path)):
                units.append(Unit(rid, path, cand[path][1]))
    return units


# --------------------------------------------------------------------------- folder profiles

@dataclass
class FolderProfile:
    remote: str
    path: str
    files: int
    dirs: int
    bytes: int
    categories: dict[str, int]
    top_exts: list[tuple[str, int]]
    mtime_min: str | None
    mtime_max: str | None
    sample_names: list[str]
    unit: str | None
    duplicate_files: int


def _spread(names: list[str], k: int = 5) -> list[str]:
    """Up to k names taken at the centres of k equal slices of the folder's path order."""
    n = len(names)
    m = min(k, n)
    picked = [names[(2 * i + 1) * n // (2 * m)] for i in range(m)]
    return list(dict.fromkeys(picked))


def _unit_reason(units: dict[str, str], path: str) -> str | None:
    if path in units:
        return units[path]
    for a in _ancestors(path):
        if a in units:
            return units[a]
    return None


def _profile_remote(conn: sqlite3.Connection, rid: int, rname: str, prefix: str, depth: int, ruleset: RuleSet) -> list[FolderProfile]:
    cut = len(prefix) + 1 if prefix else 0
    base = prefix + "/" if prefix else ""
    units = {u.path: u.reason for u in detect_units(conn, ruleset, remote_ids=[rid])}
    where, args = _under(prefix)
    file_where, _ = _under(prefix, col="e.path")

    # Sub-folder counts, plus folders that exist but hold no files.
    dir_counts: dict[str, int] = {}
    for (path,) in conn.execute(
        f"SELECT path FROM entries WHERE remote_id = ? AND is_dir = 1 AND present = 1 AND {where}", (rid, *args)
    ):
        parts = path[cut:].split("/", depth)
        if len(parts) >= depth:
            key = "/".join(parts[:depth])
            dir_counts[key] = dir_counts.get(key, 0) + (len(parts) > depth)

    rows = conn.execute(
        f"""SELECT e.path, e.name, e.ext, e.category, e.size, e.mtime,
                   CASE WHEN e.content_group IS NULL THEN 0 ELSE EXISTS (
                       SELECT 1 FROM entries d WHERE d.content_group = e.content_group
                          AND d.id <> e.id AND d.is_dir = 0 AND d.present = 1) END
            FROM entries e
            WHERE e.remote_id = ? AND e.is_dir = 0 AND e.present = 1 AND {file_where}
            ORDER BY e.path, e.id""",
        (rid, *args),
    )

    def folder_of(row) -> str | None:
        parts = row[0][cut:].split("/", depth)
        return "/".join(parts[:depth]) if len(parts) > depth else None

    # Rows are in path order, so all files of one folder are consecutive.
    keyed = ((k, r) for r in rows if (k := folder_of(r)) is not None)
    out: list[FolderProfile] = []
    seen: set[str] = set()
    for key, group in groupby(keyed, key=lambda t: t[0]):
        names: list[str] = []
        cats: Counter = Counter()
        exts: Counter = Counter()
        size = dups = 0
        lo = hi = None
        for _k, (_path, name, ext, cat, fsize, mtime, dup) in group:
            names.append(name)
            cats[cat] += 1
            if ext:
                exts[ext] += 1
            size += fsize or 0
            dups += dup
            if mtime:
                lo = mtime if lo is None or mtime < lo else lo
                hi = mtime if hi is None or mtime > hi else hi
        seen.add(key)
        path = base + key
        out.append(FolderProfile(
            remote=rname, path=path, files=len(names), dirs=dir_counts.get(key, 0), bytes=size,
            categories=dict(sorted(cats.items(), key=lambda kv: (-kv[1], kv[0]))[:6]),
            top_exts=sorted(exts.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
            mtime_min=lo, mtime_max=hi, sample_names=_spread(names),
            unit=_unit_reason(units, path), duplicate_files=dups,
        ))
    for key, n_dirs in dir_counts.items():
        if key not in seen:
            path = base + key
            out.append(FolderProfile(
                remote=rname, path=path, files=0, dirs=n_dirs, bytes=0, categories={}, top_exts=[],
                mtime_min=None, mtime_max=None, sample_names=[], unit=_unit_reason(units, path), duplicate_files=0,
            ))
    return out


def folder_profiles(conn: sqlite3.Connection, *, remote: str | None = None, prefix: str = "", depth: int = 1,
                    ruleset: RuleSet | None = None, limit: int = 200) -> list[FolderProfile]:
    """Compact aggregates of the folders exactly `depth` levels below `prefix`, biggest first.

    This is what an LLM reads instead of file lists. Each profile counts every
    live file below the folder, recursively. `top_exts` leaves out files without
    an extension (their category still shows in `categories`). Files that sit
    directly in `prefix` belong to no profiled folder.
    """
    if depth < 1:
        raise ValueError("depth must be at least 1")
    prefix = prefix.strip("/")
    ruleset = ruleset if ruleset is not None else default_rules()
    remotes = _remotes(conn, None) if remote is None else [(db.remote_id(conn, remote), remote)]
    out: list[FolderProfile] = []
    for rid, rname in remotes:
        out.extend(_profile_remote(conn, rid, rname, prefix, depth, ruleset))
    out.sort(key=lambda p: (-p.bytes, p.remote, p.path))
    return out[:limit]


# --------------------------------------------------------------------------- layout

@dataclass
class Placement:
    entry_id: int
    remote_id: int
    src_path: str
    target_path: str
    rule: str            # rule name, or 'fallback'
    unit: str | None     # source path of the unit folder the file travels with, else None


@dataclass
class _Compiled:
    rule: Rule
    categories: frozenset[str]
    exts: frozenset[str]
    remotes: frozenset[str]
    glob: re.Pattern | None
    regex: re.Pattern | None
    fields: frozenset[str]
    sized: bool


def _template_fields(template: str) -> frozenset[str]:
    return frozenset(f for seg in template.split("/") if seg for _lit, f in _parse_segment(seg) if f)


def _compile(ruleset: RuleSet) -> tuple[list[_Compiled], list[_Compiled]]:
    """(loose-file rules, unit rules), each in file order."""
    loose: list[_Compiled] = []
    unit: list[_Compiled] = []
    for r in ruleset.rules:
        try:
            regex = re.compile(r.name_regex) if r.name_regex else None
        except re.error as exc:
            raise RuleError(f"rule {r.name!r}: name_regex {r.name_regex!r} is not a valid regular expression: {exc}") from exc
        c = _Compiled(
            rule=r, categories=frozenset(r.category), exts=frozenset(e.lower().lstrip(".") for e in r.ext),
            remotes=frozenset(r.remote),
            glob=re.compile(fnmatch.translate(_norm(r.path_glob))) if r.path_glob else None,
            regex=regex, fields=_template_fields(r.target),
            sized=r.min_size is not None or r.max_size is not None,
        )
        (unit if r.unit else loose).append(c)
    return loose, unit


def _first_match(rules: list[_Compiled], remote: str, category: str, ext: str, name: str, path: str,
                 size: int | None | Callable[[], int | None]) -> _Compiled | None:
    """First rule whose keys all match; `size` may be a callable so it is only fetched when needed."""
    norm_path = None
    nfc_name = None
    for c in rules:
        if c.categories and category not in c.categories:
            continue
        if c.exts and ext not in c.exts:
            continue
        if c.remotes and remote not in c.remotes:
            continue
        if c.glob is not None:
            if norm_path is None:
                norm_path = _norm(path)
            if not c.glob.match(norm_path):
                continue
        if c.regex is not None:
            if nfc_name is None:
                nfc_name = unicodedata.normalize("NFC", name)
            if not c.regex.search(nfc_name):
                continue
        if c.sized:
            s = size() if callable(size) else size
            if s is None or (c.rule.min_size is not None and s < c.rule.min_size) \
                    or (c.rule.max_size is not None and s > c.rule.max_size):
                continue
        return c
    return None


@dataclass
class _UnitCtx:
    name: str
    comp: _Compiled | None          # the unit rule that claimed it, if any
    variables: dict[str, str]       # unit-level template variables (everything but {relpath})


def _unit_ctx(conn: sqlite3.Connection, rname: str, unit: Unit, unit_rules: list[_Compiled]) -> _UnitCtx:
    name = posixpath.basename(unit.path)
    ext = _name_ext(name)
    stats: list[tuple[int, str | None]] = []

    def get_stats() -> tuple[int, str | None]:
        if not stats:
            row = conn.execute(
                "SELECT COALESCE(SUM(size), 0), MAX(mtime) FROM entries "
                "WHERE remote_id = ? AND is_dir = 0 AND present = 1 AND path >= ? AND path < ?",
                (unit.remote_id, unit.path + "/", unit.path + "0"),
            ).fetchone()
            stats.append((int(row[0]), row[1]))
        return stats[0]

    comp = _first_match(unit_rules, rname, category_of(ext), ext, name, unit.path, lambda: get_stats()[0])
    if comp is None:
        return _UnitCtx(name, None, {})
    ymd = date_from_name(name)
    if ymd is None and comp.fields & _DATE_VARS:
        ymd = _mtime_date(get_stats()[1])
    ymd = ymd or _UNKNOWN
    parent = posixpath.dirname(unit.path)
    variables = {
        "name": name, "stem": _stem(name, ext), "ext": ext, "category": category_of(ext),
        "year": ymd[0], "month": ymd[1], "day": ymd[2], "remote": rname,
        "parent": parent.rpartition("/")[2], "dir": parent, "path": unit.path, "unit": name,
    }
    return _UnitCtx(name, comp, variables)


def _clean_root(target_root: str) -> str:
    parts = [p for p in target_root.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise RuleError(f"target_root {target_root!r} contains a '.' or '..' path segment")
    return "/".join(parts)


def plan_layout(conn: sqlite3.Connection, ruleset: RuleSet, *, remote_ids: list[int] | None = None, target_root: str = "",
                path_prefix: str | None = None) -> list[Placement]:
    """One Placement per live file (native docs included), ordered by (remote, path).

    * A file inside a unit goes with its unit. The first `unit = true` rule that
      matches the unit folder decides; its variables describe the *unit folder*
      (so {year} is the unit's date: from its name, else its newest file) and
      {relpath} is the file's path inside the unit. `ext`/`category` of a unit
      rule are those of the folder name ('Foo.pages' -> pages/document), and
      min_size/max_size compare the unit's total bytes. With no matching unit
      rule the unit's files take the fallback with their own variables, which
      keeps them together as long as it uses {path} or {relpath}.
    * A loose file takes the first non-unit rule that matches, else the fallback.
    * Collisions between targets are left to the plan builder.

    `path_prefix` limits the output to files at or under that folder in every
    selected remote; units are still detected on the whole remote.
    """
    root = _clean_root(target_root)
    loose_rules, unit_rules = _compile(ruleset)
    fallback_fields = _template_fields(ruleset.fallback)
    prefix = path_prefix.strip("/") if path_prefix else ""
    where, args = _under(prefix, include_self=True)
    out: list[Placement] = []
    for rid, rname in _remotes(conn, remote_ids):
        units = {u.path: u for u in detect_units(conn, ruleset, remote_ids=[rid])}
        ctxs: dict[str, _UnitCtx] = {}
        rows = conn.execute(
            "SELECT id, path, parent, name, ext, category, size, mtime FROM entries "
            f"WHERE remote_id = ? AND is_dir = 0 AND present = 1 AND {where} ORDER BY path, id",
            (rid, *args),
        )
        for eid, path, parent, name, ext, cat, size, mtime in rows:
            unit = None
            if units:
                p = parent
                while p:
                    unit = units.get(p)
                    if unit is not None:
                        break
                    p = p.rpartition("/")[0]
            rule_name = "fallback"
            template = ruleset.fallback
            variables = None
            fields = fallback_fields
            relpath = path
            if unit is not None:
                relpath = path[len(unit.path) + 1:]
                ctx = ctxs.get(unit.path)
                if ctx is None:
                    ctx = ctxs[unit.path] = _unit_ctx(conn, rname, unit, unit_rules)
                if ctx.comp is not None:
                    rule_name, template = ctx.comp.rule.name, ctx.comp.rule.target
                    variables = {**ctx.variables, "relpath": relpath}
            else:
                c = _first_match(loose_rules, rname, cat, ext, name, path, size)
                if c is not None:
                    rule_name, template, fields = c.rule.name, c.rule.target, c.fields
            if variables is None:
                variables = {
                    "name": name, "stem": _stem(name, ext), "ext": ext, "category": cat, "remote": rname,
                    "parent": parent.rpartition("/")[2], "dir": parent, "path": path,
                    "unit": posixpath.basename(unit.path) if unit else "", "relpath": relpath,
                }
                if fields & _DATE_VARS:
                    variables["year"], variables["month"], variables["day"] = _date_for(name, mtime)
            target = render(template, variables)
            out.append(Placement(eid, rid, path, f"{root}/{target}" if root else target, rule_name,
                                 unit.path if unit else None))
    return out
