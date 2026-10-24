"""Browser tests for the Rules view (docs/RULES.md section G), against a mocked /v1 API.

Same approach as tests/test_ui_frontend.py and tests/test_ui_quarantine.py: the real static files served under the real
CSP, every /v1 call answered by a mock through page.route. `RMock` adds the routes of section E with the exact JSON shapes
of sections B to D and a small model behind them (a tiny rules parser, versions that change status, a fake catalog the
preview places files from), so that a flow can be followed from typing to the activation dialog.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect
from test_ui_frontend import MockApi, Ui, browser, site  # noqa: F401  (fixtures)

pytestmark = pytest.mark.ui

SHOTS = Path(os.environ.get("DRIVEMERGE_UI_SHOTS", "/tmp/claude-0/ui-shots"))
NOW = datetime.now(UTC)
ACTIVE_PATH = "/home/u/.config/drive-merge/rules.toml"


def iso(days_ago: float = 0.0) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


ACTIVE_TEXT = '''# Layout rules (test)
[layout]
fallback = "Unsorted/{remote}/{path}"

# Before "photos": a screenshot is a png, but it is not a photo.
[[rule]]
name = "screenshots"
category = ["photo"]
name_regex = '(?i)^screenshot'
target = "Screenshots/{year}/{name}"

[[rule]]
name = "photos"
category = ["photo"]
target = "Photos/{year}/{name}"

# Paper and scans.
[[rule]]
name = "documents"
category = ["document", "pdf"]
target = "Documents/{year}/{name}"
'''

RECEIPTS = '''[[rule]]
name = "receipts"
category = ["pdf"]
name_regex = '(?i)receipt'
target = "Receipts/{year}/{name}"

'''
# Claude's proposal: a `receipts` rule in front of `documents`, which would otherwise take every pdf.
PROPOSED_TEXT = ACTIVE_TEXT.replace('# Paper and scans.\n', RECEIPTS + '# Paper and scans.\n', 1)

CATEGORIES = {"jpg": "photo", "png": "photo", "pdf": "pdf", "txt": "document", "zip": "archive"}
KEYS = ("name", "unit", "category", "ext", "path_glob", "name_regex", "min_size", "max_size", "remote", "target")


def _catalog() -> list[dict]:
    files: list[dict] = []
    n = 100
    for i in range(10):
        files.append({"remote": "gdrive", "path": f"Photos/trip{i}.jpg", "year": "2021"})
    for i in range(4):
        files.append({"remote": "gdrive", "path": f"Grabs/screenshot {i}.png", "year": "2022"})
    for i in range(8):
        files.append({"remote": "gdrive", "path": f"Scans/receipt_{i}.pdf", "year": "2019"})
    for i in range(3):
        files.append({"remote": "gdrive", "path": f"Documents/2020/old{i}.pdf", "year": "2020"})
    for i in range(6):
        files.append({"remote": "gdrive", "path": f"Docs/notes{i}.txt", "year": "2023"})
    for i in range(3):
        files.append({"remote": "gdrive", "path": f"Misc/archive{i}.zip", "year": "2018"})
    for i in range(5):
        files.append({"remote": "dropbox", "path": f"Pictures/img{i}.jpg", "year": "2021"})
    for f in files:
        n += 1
        f["entry_id"] = n
        f["name"] = f["path"].rsplit("/", 1)[-1]
        f["ext"] = f["name"].rsplit(".", 1)[-1]
        f["category"] = CATEGORIES[f["ext"]]
        f["size"] = 1000 + n
    return files


# --------------------------------------------------------------------------- a tiny rules language

def parse_rules(text: str) -> dict:
    """Header lines, keys and the first error of every rule, as classify.check_rules reports them (only what the tests need)."""
    lines = text.split("\n")
    rules: list[dict] = []
    errors: list[dict] = []
    cur: dict | None = None
    fallback = None
    for no, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if re.fullmatch(r"\[\[\s*rule\s*\]\]", s):
            cur = {"header": no, "keys": {}, "key_line": {}}
            rules.append(cur)
            continue
        if re.fullmatch(r"\[\s*layout\s*\]", s):
            cur = None
            continue
        m = re.fullmatch(r"(\w+)\s*=\s*(.*)", s)
        if not m:
            errors.append({"code": "TOML_SYNTAX", "message": "invalid TOML", "params": {"line": no, "rule_index": None, "rule": None, "key": None, "column": 1}})
            continue
        key, value = m.group(1), m.group(2)
        if cur is None:
            if key == "fallback":
                fallback = json.loads(value)
            continue
        cur["keys"][key] = _value(value)
        cur["key_line"][key] = no
    # spans: a rule runs from the comment lines above its header to the line before the next span
    for i, r in enumerate(rules):
        c = r["header"]
        while c > 1 and lines[c - 2].strip().startswith("#"):
            c -= 1
        r["comment_line"] = c
    for i, r in enumerate(rules):
        r["end_line"] = rules[i + 1]["comment_line"] - 1 if i + 1 < len(rules) else len(lines)
        while r["end_line"] > r["header"] and not lines[r["end_line"] - 1].strip():
            r["end_line"] -= 1
    seen: set[str] = set()
    for i, r in enumerate(rules, 1):
        k = r["keys"]
        name = k.get("name")
        base = {"line": r["header"], "rule_index": i, "rule": name, "key": None}
        err = None
        if not name:
            err = ("NAME_REQUIRED", "rule needs a name", {})
        elif name in seen:
            err = ("NAME_DUPLICATE", "duplicate name", {"name": name, "key": "name", "line": r["key_line"]["name"]})
        elif "target" not in k:
            err = ("TARGET_REQUIRED", "rule needs a target", {})
        else:
            for cat in k.get("category", []):
                if cat not in {"photo", "video", "audio", "document", "pdf", "spreadsheet", "presentation", "archive", "code", "design", "other"}:
                    close = difflib.get_close_matches(cat, ["photo", "video", "audio", "document", "pdf", "spreadsheet"], 1)
                    err = ("UNKNOWN_CATEGORY", "unknown category", {"value": cat, "suggestion": close[0] if close else None, "key": "category", "line": r["key_line"]["category"]})
                    break
        if name:
            seen.add(name)
        if err:
            code, message, extra = err
            errors.append({"code": code, "message": message, "params": {**base, "suggestion": None, **extra}})
            r["bad"] = True
    errors.sort(key=lambda e: e["params"]["line"] or 0)
    warnings = []
    for i, r in enumerate(rules):
        for e in rules[:i]:
            plain = not any(x in e["keys"] for x in ("category", "ext", "path_glob", "name_regex", "min_size", "max_size", "remote"))
            if plain and bool(e["keys"].get("unit")) == bool(r["keys"].get("unit")):
                warnings.append({"code": "RULE_SHADOWED", "params": {"line": r["header"], "rule_index": i + 1, "rule": r["keys"].get("name"), "key": None, "by": e["keys"].get("name")}})
                break
    return {"lines": lines, "rules": rules, "errors": errors, "warnings": warnings, "fallback": fallback or "Unsorted/{remote}/{path}"}


def _value(v: str) -> Any:
    v = v.strip()
    if v in ("true", "false"):
        return v == "true"
    if v.startswith("'"):
        return v.strip("'")
    try:
        return json.loads(v)
    except ValueError:
        return v


def render(template: str, f: dict) -> str:
    vars_ = {"name": f["name"], "year": f["year"], "category": f["category"], "remote": f["remote"], "path": f["path"], "ext": f["ext"]}
    return re.sub(r"\{(\w+)\}", lambda m: vars_.get(m.group(1), m.group(0)), template)


def place(parsed: dict, f: dict) -> tuple[str, str]:
    for r in parsed["rules"]:
        k = r["keys"]
        if r.get("bad") or "target" not in k:
            continue
        if k.get("category") and f["category"] not in k["category"]:
            continue
        if k.get("ext") and f["ext"] not in k["ext"]:
            continue
        if k.get("name_regex") and not re.search(k["name_regex"].replace("(?i)", ""), f["name"], re.IGNORECASE):
            continue
        return k["name"], render(k["target"], f)
    return "fallback", render(parsed["fallback"], f)


def outline_of(parsed: dict) -> list[dict]:
    out = []
    for i, r in enumerate(parsed["rules"], 1):
        k = r["keys"]
        out.append({"index": i, "name": k.get("name") or "", "unit": bool(k.get("unit")), "line": r["header"], "end_line": r["end_line"],
                    "match": {"category": k.get("category", []), "ext": k.get("ext", []), "path_glob": k.get("path_glob"), "name_regex": k.get("name_regex"),
                              "min_size": k.get("min_size"), "max_size": k.get("max_size"), "remote": k.get("remote", [])},
                    "target": k.get("target", "")})
    return out


def diff_of(old: str, new: str) -> dict:
    a, b = old.split("\n"), new.split("\n")
    hunks, added, removed = [], 0, 0
    for group in difflib.SequenceMatcher(None, a, b).get_grouped_opcodes(3):
        lines = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [{"op": " ", "text": x} for x in a[i1:i2]]
                continue
            lines += [{"op": "-", "text": x} for x in a[i1:i2]]
            lines += [{"op": "+", "text": x} for x in b[j1:j2]]
            removed += i2 - i1
            added += j2 - j1
        hunks.append({"old_start": group[0][1] + 1, "new_start": group[0][3] + 1, "lines": lines})
    return {"hunks": hunks, "added": added, "removed": removed}


def changes_of(old: dict, new: dict) -> list[dict]:
    before = {r["keys"].get("name"): (i, r["keys"]) for i, r in enumerate(old["rules"])}
    out = []
    order_old = [n for n in before if n in {r["keys"].get("name") for r in new["rules"]}]
    order_new = [r["keys"].get("name") for r in new["rules"] if r["keys"].get("name") in before]
    for i, r in enumerate(new["rules"]):
        name = r["keys"].get("name")
        if name not in before:
            out.append({"name": name, "change": "added"})
        elif before[name][1] != r["keys"]:
            out.append({"name": name, "change": "changed"})
        elif order_old.index(name) != order_new.index(name):
            out.append({"name": name, "change": "moved"})
    out += [{"name": n, "change": "removed"} for n in before if n not in {r["keys"].get("name") for r in new["rules"]}]
    return out


class RMock(MockApi):
    """The workspace mock plus the routes of docs/RULES.md section E."""

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.versions: dict[int, dict] = {}
        self.next_version = 12
        self.active_text = ACTIVE_TEXT
        self.active_path: str | None = ACTIVE_PATH
        self.owned = True
        self.source = "file"
        self.files = _catalog()
        self.previews: dict[str, dict] = {}
        self.next_preview = 1
        self.sampled = False
        self.sample_scale = 1000
        self.supersede_next = False
        self.activate_error: tuple[int, str, dict] | None = None
        self.state_error: tuple[int, str] | None = None
        self.make_active(ACTIVE_TEXT, by="file", days=9)

    # ------------------------------------------------------------------ the model
    def _version(self, text: str, *, status: str, by: str, title: str | None = None, note: str | None = None, base: int | None = None,
                 days: float = 0.0) -> dict:
        vid = self.next_version
        self.next_version += 1
        ok = not parse_rules(text)["errors"]
        v = {"id": vid, "status": status, "text": text, "sha256": sha(text), "ok": ok, "created_by": by, "title": title, "note": note,
             "base_id": base, "revision": 0, "created_at": iso(days), "updated_at": iso(days), "activated_at": None, "activated_by": None}
        self.versions[vid] = v
        return v

    def make_active(self, text: str, *, by: str = "ui", days: float = 2) -> dict:
        for v in self.versions.values():
            if v["status"] == "active":
                v["status"] = "retired"
        v = self._version(text, status="active", by=by, days=days)
        v["activated_at"], v["activated_by"] = iso(days), by
        self.active_text = text
        return v

    def add_proposal(self, text: str = PROPOSED_TEXT, title: str = "Keep scanned receipts by year", note: str | None = "Receipts were mixed with other pdfs.",
                     by: str = "mcp") -> dict:
        return self._version(text, status="proposed", by=by, title=title, note=note, base=self.active()["id"], days=0.01)

    def add_draft(self, text: str = ACTIVE_TEXT, title: str | None = "photos by camera") -> dict:
        return self._version(text, status="draft", by="ui", title=title, base=self.active()["id"], days=0.002)

    def active(self) -> dict:
        return next(v for v in self.versions.values() if v["status"] == "active")

    def summary(self, v: dict) -> dict:
        parsed = parse_rules(v["text"])
        base = {k: v[k] for k in ("id", "status", "title", "created_by", "created_at", "updated_at", "activated_at", "activated_by", "sha256", "ok", "base_id", "revision")}
        if parsed["errors"]:                                  # an invalid text has no rules to count or compare
            return {**base, "rules": None, "changes": None}
        return {**base, "rules": len(parsed["rules"]), "changes": changes_of(parse_rules(self.active_text), parsed)}

    def full(self, v: dict) -> dict:
        """What GET, POST and PUT of a version answer (service.rule_version): the summary, the note, the text, the diff
        against the active rules and the check."""
        c = self.check(v["text"])
        return {**self.summary(v), "note": v["note"], "text": v["text"], "diff": c["diff"],
                "check": {k: c[k] for k in ("ok", "errors", "warnings", "outline", "layout")}}

    def check(self, text: str) -> dict:
        parsed = parse_rules(text)
        active = parse_rules(self.active_text)
        ok = not parsed["errors"]
        return {"ok": ok, "errors": parsed["errors"], "warnings": parsed["warnings"], "outline": outline_of(parsed) if ok or parsed["rules"] else [],
                "layout": {"unit_markers": [".git"], "bundle_patterns": ["*.pages"], "fallback": parsed["fallback"], "defaulted": ["unit_markers"]},
                "diff": diff_of(self.active_text, text), "changes": changes_of(active, parsed) if ok else None, "sha256": sha(text)}

    def state(self) -> dict:
        act = self.active()
        by_status = lambda s: sorted((self.summary(v) for v in self.versions.values() if v["status"] == s), key=lambda x: -x["id"])
        history = sorted((self.summary(v) for v in self.versions.values() if v["status"] in ("active", "retired")), key=lambda x: -x["id"])
        error = None
        if parse_rules(self.active_text)["errors"]:
            error = parse_rules(self.active_text)["errors"][0]
            error = {"code": error["code"], "params": error["params"]}
        checked = parse_rules(self.active_text)
        info = {"text": self.active_text, "sha256": sha(self.active_text), "source": self.source, "path": self.active_path if self.source == "file" else None,
                "owned": self.owned, "version_id": act["id"], "error": error, "write_path": self.active_path, "status": "active", "created_by": act["created_by"],
                "since": act["activated_at"], "by": act["activated_by"], "changed_outside": act["created_by"] == "file",
                "rules": None if error else len(checked["rules"]),
                "check": {"ok": error is None, "error_count": len(checked["errors"]), "warning_count": len(checked["warnings"]),
                          "errors": [{"code": e["code"], "message": e["message"], "params": e["params"]} for e in checked["errors"][:10]], "warnings": checked["warnings"]}}
        return {"active": info, "proposals": by_status("proposed"), "drafts": by_status("draft"), "history": history}

    # ------------------------------------------------------------------ the preview
    def preview(self, body: dict) -> tuple[int, dict]:
        if self.supersede_next:
            self.supersede_next = False
            return 409, {"error": "superseded", "code": "PREVIEW_SUPERSEDED"}
        if body.get("text") is not None and body.get("version_id") is not None:
            return 400, {"error": "both", "code": "RULES_BOTH"}
        text = body["text"] if body.get("text") is not None else self.versions[body["version_id"]]["text"]
        parsed, base = parse_rules(text), parse_rules(self.active_text)
        if parsed["errors"]:
            e = parsed["errors"][0]
            return 400, {"error": e["message"], "code": e["code"], "params": e["params"]}
        broken = bool(base["errors"])                       # an invalid active file: nothing to compare with, every active_* is null
        remotes = body.get("remotes") or [r["name"] for r in REMOTES_]
        prefix = (body.get("path_prefix") or "").strip("/")
        scoped = [f for f in self.files if f["remote"] in remotes and (not prefix or f["path"].startswith(prefix))]
        sampled = self.sampled and body.get("mode") != "exact"
        k = self.sample_scale if sampled else 1
        rows = []
        for f in scoped:
            rule, target = place(parsed, f)
            arule, atarget = (None, None) if broken else place(base, f)
            rows.append({"entry_id": f["entry_id"], "remote": f["remote"], "path": f["path"], "size": f["size"], "rule": rule, "target": target,
                         "unit": None, "active_rule": arule, "active_target": atarget, "in_place": target == f["path"]})
        names = [r["keys"]["name"] for r in parsed["rules"]]
        out_rules = []
        for n in names:
            files = sum(1 for r in rows if r["rule"] == n)
            active_names = [r["keys"]["name"] for r in base["rules"]]
            act = sum(1 for r in rows if r["active_rule"] == n) if n in active_names and not broken else None
            out_rules.append({"name": n, "unit": False, "files": files, "bytes": 0, "units": None, "active_files": act,
                              "estimate_files": files * k, "estimate_bytes": 0, "estimate_active_files": None if act is None else act * k})
        if not broken:                                       # rules the edit removed: what they took before
            for r in base["rules"]:
                n = r["keys"]["name"]
                if n not in names and not r.get("bad"):
                    act = sum(1 for x in rows if x["active_rule"] == n)
                    out_rules.append({"name": n, "unit": False, "files": 0, "bytes": 0, "units": None, "active_files": act, "estimate_files": 0,
                                      "estimate_bytes": 0, "estimate_active_files": act * k, "removed": True})
        fb = [r for r in rows if r["rule"] == "fallback"]
        afb = [r for r in rows if r["active_rule"] == "fallback"]
        changed = [] if broken else [r for r in rows if r["target"] != r["active_target"]]
        trans: dict[tuple, int] = {}
        for r in changed:
            trans[(r["active_rule"], r["rule"])] = trans.get((r["active_rule"], r["rule"]), 0) + 1
        pid = f"pv_{self.next_preview * 2654435761 % 4294967296:08x}"
        self.next_preview += 1
        self.previews[pid] = {"rows": rows, "names": names, "baseline": not broken}
        data = {"preview_id": pid, "rules_sha256": sha(text), "active_sha256": sha(self.active_text),
                "scope": {"remotes": remotes, "target_root": body.get("target_root") or "", "path_prefix": prefix or None},
                "files_in_scope": len(rows) * k, "sampled": sampled, "sample_files": len(rows), "sample_rate": 1 / k if sampled else 1.0, "elapsed_ms": 12,
                "rules": out_rules, "fallback": {"files": len(fb), "bytes": 0, "estimate_files": len(fb) * k, "active_files": None if broken else len(afb)},
                "in_place": sum(1 for r in rows if r["in_place"]), "estimate_in_place": sum(1 for r in rows if r["in_place"]) * k,
                "changed": None if broken else len(changed), "estimate_changed": None if broken else len(changed) * k, "shared_targets": 0,
                "transitions": [{"from": a, "to": b, "files": n, "estimate_files": n * k} for (a, b), n in sorted(trans.items(), key=lambda x: -x[1])],
                "page": self.page(pid, body.get("filter") or "changed", None, 0, min(int(body.get("limit") or 100), 500))}
        return 200, data

    def page(self, pid: str, flt: str, q: str | None, offset: int, limit: int) -> dict:
        rows = self.previews[pid]["rows"]
        if flt == "changed":
            rows = [r for r in rows if r["active_rule"] is not None and r["target"] != r["active_target"]] if self.previews[pid]["baseline"] else rows
        elif flt == "fallback":
            rows = [r for r in rows if r["rule"] == "fallback"]
        elif flt == "in_place":
            rows = [r for r in rows if r["in_place"]]
        elif flt.startswith("rule:"):
            rows = [r for r in rows if r["rule"] == flt[5:]]
        if q:
            rows = [r for r in rows if q.lower() in f"{r['remote']}:{r['path']}".lower()]
        return {"filter": flt, "q": q, "offset": offset, "limit": limit, "total": len(rows), "files": rows[offset: offset + limit]}

    def explain(self, parsed: dict, f: dict) -> dict:
        steps, done, hit, target = [], False, "fallback", render(parsed["fallback"], f)
        for r in parsed["rules"]:
            k = r["keys"]
            if done:
                steps.append({"rule": k["name"], "unit": False, "result": "not_tried", "failed_key": None, "wanted": None, "got": None})
                continue
            if k.get("category") and f["category"] not in k["category"]:
                steps.append({"rule": k["name"], "unit": False, "result": "skip", "failed_key": "category", "wanted": k["category"], "got": f["category"]})
                continue
            if k.get("name_regex") and not re.search(k["name_regex"].replace("(?i)", ""), f["name"], re.IGNORECASE):
                steps.append({"rule": k["name"], "unit": False, "result": "skip", "failed_key": "name_regex", "wanted": k["name_regex"], "got": f["name"]})
                continue
            steps.append({"rule": k["name"], "unit": False, "result": "match", "failed_key": None, "wanted": None, "got": None})
            done, hit, target = True, k["name"], render(k["target"], f)
        return {"remote": f["remote"], "path": f["path"], "unit": None, "unit_rule": None, "steps": steps, "rule": hit, "target": target, "in_place": target == f["path"]}

    # ------------------------------------------------------------------ transform
    def transform(self, text: str, op: dict) -> tuple[int, dict]:
        parsed = parse_rules(text)
        lines = parsed["lines"]
        if parsed["errors"]:
            return 400, {"error": "cannot", "code": "RULES_CANNOT_EDIT", "params": {"reason": "unparsed", "line": None, "rule_index": None, "rule": None, "key": None}}
        blocks = [lines[r["comment_line"] - 1: r["end_line"]] for r in parsed["rules"]]
        head = lines[: parsed["rules"][0]["comment_line"] - 1] if parsed["rules"] else lines
        tail = []
        if op["op"] == "move":
            if not 1 <= op["index"] <= len(blocks) or not 1 <= op["to"] <= len(blocks):
                return 400, {"error": "cannot", "code": "RULES_CANNOT_EDIT", "params": {"reason": "not_found", "line": None, "rule_index": None, "rule": None, "key": None}}
            b = blocks.pop(op["index"] - 1)
            blocks.insert(op["to"] - 1, b)
        elif op["op"] == "remove":
            blocks.pop(op["index"] - 1)
        else:
            rule = op["rule"]
            body = ["[[rule]]", f'name = {json.dumps(rule["name"])}']
            for key in ("unit", "category", "ext", "path_glob", "name_regex", "min_size", "max_size", "remote"):
                if key in rule:
                    body.append(f"{key} = {json.dumps(rule[key]) if not isinstance(rule[key], bool) else str(rule[key]).lower()}")
            body.append(f'target = {json.dumps(rule["target"])}')
            blocks.insert(op["after"], body + [""])
        sep = lambda b: b if not b or b[-1] == "" else b + [""]
        new = "\n".join(head + [x for b in blocks for x in sep(b)] + tail).rstrip("\n") + "\n"
        again = parse_rules(new)
        if again["errors"]:
            e = again["errors"][0]
            return 400, {"error": e["message"], "code": e["code"], "params": e["params"]}
        return 200, {"text": new, "check": self.check(new)}

    # ------------------------------------------------------------------ routes
    def dispatch(self, method: str, path: str, q: dict, body: Any, headers: dict) -> tuple[int, Any]:
        if path.startswith(("/v1/rules", "/v1/ui/rules")):
            routed = self.rules_routes(method, path, q, body, headers)
            if routed is not None:
                return routed
        return super().dispatch(method, path, q, body, headers)

    def rules_routes(self, method: str, path: str, q: dict, body: Any, headers: dict) -> tuple[int, Any] | None:
        gone = lambda v: (404, {"error": "no such version", "code": "RULES_VERSION_NOT_FOUND", "params": {"version": v}})
        if (method, path) == ("GET", "/v1/rules"):
            if self.state_error:
                return self.state_error[0], {"error": "x", "code": self.state_error[1]}
            return 200, self.state()
        if (method, path) == ("GET", "/v1/rules/default"):
            return 200, {"text": ACTIVE_TEXT.replace("(test)", "(defaults)"), "sha256": "d" * 64}
        if (method, path) == ("POST", "/v1/rules/check"):
            return 200, self.check(body["text"])
        if (method, path) == ("POST", "/v1/rules/transform"):
            return self.transform(body["text"], body["op"])
        if (method, path) == ("GET", "/v1/rules/versions"):
            rows = [self.summary(v) for v in sorted(self.versions.values(), key=lambda x: -x["id"]) if not q.get("status") or v["status"] == q["status"]]
            return 200, {"versions": rows[: int(q.get("limit", 50))]}
        if (method, path) == ("POST", "/v1/rules/versions"):
            v = self._version(body["text"], status="draft", by="ui", title=body.get("title"), note=body.get("note"), base=body.get("base_id"))
            return 201, self.full(v)
        m = re.fullmatch(r"/v1/rules/versions/(\d+)", path)
        if m:
            v = self.versions.get(int(m.group(1)))
            if v is None:
                return gone(int(m.group(1)))
            if method == "GET":
                return 200, self.full(v)
            if method == "PUT":
                if v["status"] != "draft":
                    return 409, {"error": "x", "code": "RULES_NOT_DRAFT", "params": {"version": v["id"], "version_status": v["status"]}}
                if body["revision"] != v["revision"]:
                    return 409, {"error": "x", "code": "RULES_DRAFT_CHANGED", "params": {"version": v["id"], "revision": v["revision"]}}
                v.update(text=body["text"], sha256=sha(body["text"]), ok=not parse_rules(body["text"])["errors"], revision=v["revision"] + 1, updated_at=iso(0))
                if "title" in body:
                    v["title"] = body["title"] or None
                return 200, self.full(v)
            if method == "DELETE":
                if v["status"] == "draft":
                    del self.versions[v["id"]]
                    return 200, {"ok": True, "version": v["id"], "status": "deleted"}
                v["status"] = "dismissed"
                return 200, {"ok": True, "version": v["id"], "status": "dismissed"}
        if (method, path) == ("POST", "/v1/rules/preview"):
            return self.preview(body)
        m = re.fullmatch(r"/v1/rules/previews/([^/]+)/files", path)
        if m and method == "GET":
            if m.group(1) not in self.previews:
                return 410, {"error": "gone", "code": "PREVIEW_EXPIRED", "params": {"preview_id": m.group(1)}}
            return 200, self.page(m.group(1), q.get("filter", "changed"), q.get("q") or None, int(q.get("offset", 0)), int(q.get("limit", 100)))
        if (method, path) == ("POST", "/v1/rules/explain"):
            text = body["text"] if body.get("text") is not None else self.versions[body["version_id"]]["text"]
            f = next((x for x in self.files if x["remote"] == body["remote"] and x["path"] == body["path"]), None)
            if f is None:
                return 404, {"error": "no such file", "code": "NOT_FOUND"}
            return 200, {"edited": self.explain(parse_rules(text), f), "active": self.explain(parse_rules(self.active_text), f)}
        m = re.fullmatch(r"/v1/ui/rules/versions/(\d+)/activate", path)
        if m and method == "POST":
            if headers.get("x-csrf-token") != self.CSRF:
                return 403, {"error": "bad csrf token", "code": "BAD_CSRF"}
            v = self.versions.get(int(m.group(1)))
            if v is None:
                return gone(int(m.group(1)))
            if not v["ok"]:
                return 400, {"error": "x", "code": "RULES_INVALID", "params": {"errors": [{"code": e["code"], "params": e["params"]} for e in parse_rules(v["text"])["errors"]]}}
            if self.activate_error:
                status, code, params = self.activate_error
                return status, {"error": "x", "code": code, "params": params}
            if body["base_sha256"] != sha(self.active_text):
                act = self.active()
                return 409, {"error": "x", "code": "RULES_ACTIVE_CHANGED", "params": {"active_sha256": sha(self.active_text), "active_version": act["id"]}}
            for other in self.versions.values():
                if other["status"] == "active":
                    other["status"] = "retired"
            v.update(status="active", activated_at=iso(0), activated_by="ui")
            self.active_text = v["text"]
            keys = ("text", "sha256", "source", "path", "owned", "version_id", "error")
            return 200, {"active": {k: self.state()["active"][k] for k in keys}}
        return None

    # ------------------------------------------------------------------ the estimate of a reorganize form
    def estimate(self, kind: str, params: dict) -> tuple[int, dict]:
        if kind != "reorganize":
            return super().estimate(kind, params)
        if params.get("remote") not in {r["name"] for r in self.remotes}:
            return 400, {"error": "unknown remote", "code": "UNKNOWN_REMOTE", "params": {"remote": params.get("remote")}}
        v = self.versions.get(params["rules_version"]) if params.get("rules_version") else None
        parsed = parse_rules(v["text"] if v else self.active_text)
        prefix = (params.get("path_prefix") or "").strip("/")
        sample, files, inplace = [], 0, 0
        for f in self.files:
            if f["remote"] != params["remote"] or (prefix and not f["path"].startswith(prefix)):
                continue
            files += 1
            _, target = place(parsed, f)
            if params.get("target_root"):
                target = f"{params['target_root'].strip('/')}/{target}"
            if target == f["path"]:
                inplace += 1
            else:
                sample.append({"op": "move", "src": {"remote": f["remote"], "path": f["path"]}, "dst": {"remote": f["remote"], "path": target}, "size": f["size"], "reason": ""})
        stats = {"files": files, "in_place": inplace, "moves": len(sample), "renamed_for_collision": 0, "skipped": 0}
        return 200, {"kind": "reorganize", "ops": len(sample), "stats": stats, "skipped": [], "sample": sample[:50], "nothing_to_do": not sample}


REMOTES_ = [{"name": "gdrive"}, {"name": "dropbox"}]


# --------------------------------------------------------------------------- fixtures and helpers

@pytest.fixture
def make_r(browser, site):  # noqa: F811
    contexts: list = []
    errors: list[str] = []

    def factory(*, locale: str = "en-US", color_scheme: str = "light", width: int = 1440, height: int = 900, **kw: Any) -> Ui:
        ctx = browser.new_context(viewport={"width": width, "height": height}, locale=locale, color_scheme=color_scheme)
        contexts.append(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None)
        mock = RMock(**kw)
        page.route("**/v1/**", mock.handle)
        return Ui(page, mock, site)

    yield factory
    for c in contexts:
        c.close()
    assert not errors, f"uncaught JavaScript errors: {errors}"


def start(ui: Ui, route: str = "#/rules") -> Ui:
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    expect(ui.tid("rail")).to_be_visible()
    ui.go(route)
    return ui


def calls(ui: Ui, method: str, pattern: str) -> list:
    return ui.mock.calls_to(method, pattern)


def editor(ui: Ui, draft: dict | None = None, **kw: Any) -> dict:
    d = draft or ui.mock.add_draft(**kw)
    start(ui, f"#/rules/edit/{d['id']}")
    expect(ui.tid("rules-text")).to_have_value(d["text"])
    return d


def type_at_end(ui: Ui, text: str) -> None:
    ui.tid("rules-text").focus()
    ui.page.keyboard.press("Control+End")
    ui.page.keyboard.type(text, delay=5)


def replace_text(ui: Ui, text: str) -> None:
    ui.tid("rules-text").fill(text)


def calls_since(ui: Ui, n: int, method: str, pattern: str) -> list:
    return calls(ui, method, pattern)[n:]


# --------------------------------------------------------------------------- rail, badge, shortcut

def test_rules_is_the_seventh_view_with_alt_7_and_a_badge_for_proposals(make_r):
    ui = make_r()
    ui.mock.add_proposal()
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    expect(ui.tid("rail-rules-badge")).to_have_attribute("data-count", "1")
    expect(ui.tid("rail-rules")).to_have_attribute("title", re.compile(r"\(Alt\+7\)"))
    ui.page.keyboard.press("Alt+7")
    expect(ui.tid("rail-rules")).to_have_attribute("aria-current", "page")
    expect(ui.tid("view-rules")).to_be_visible()
    assert ui.page.evaluate("location.hash") == "#/rules"
    ui.page.keyboard.press("Alt+4")
    expect(ui.tid("rail-workspace")).to_have_attribute("aria-current", "page")
    ui.page.keyboard.press("Alt+7")                                   # the view is remembered as it was left
    expect(ui.tid("view-rules")).to_be_visible()


def test_no_badge_without_proposals_and_an_older_server_does_not_break_the_page(make_r):
    ui = make_r()
    ui.mock.versions.clear()
    ui.mock.make_active(ACTIVE_TEXT)
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    expect(ui.tid("rail")).to_be_visible()
    expect(ui.tid("rail-rules-badge")).to_have_count(0)
    # a server that has no rules routes at all answers 404: the page stays quiet
    ui2 = make_r()
    ui2.mock.rules_routes = lambda *a, **k: None                      # type: ignore[method-assign]
    ui2.page.goto(f"{ui2.base}/ui/#token={MockApi.TOKEN}")
    expect(ui2.tid("rail")).to_be_visible()
    expect(ui2.tid("rail-rules-badge")).to_have_count(0)


# --------------------------------------------------------------------------- the overview

def test_the_overview_says_where_the_active_rules_come_from(make_r):
    ui = make_r()
    ui.mock.add_proposal()
    ui.mock.add_draft()
    start(ui)
    active = ui.tid("rules-active")
    expect(active).to_have_attribute("data-source", "file")
    expect(ui.tid("rules-active-source")).to_contain_text(ACTIVE_PATH)
    expect(ui.tid("rules-active-source")).to_contain_text("3 rules")
    expect(ui.tid("rules-active-source")).to_contain_text("active since 9 d ago")
    expect(ui.tid("rules-outside-change")).to_have_count(1)          # the file was found, not activated from the page
    pid = next(v for v in ui.mock.versions.values() if v["status"] == "proposed")["id"]
    row = ui.tid(f"rules-proposal-row-{pid}")
    expect(row).to_contain_text("Keep scanned receipts by year")
    expect(row).to_contain_text("Claude")
    expect(row).to_contain_text("1 added")
    expect(ui.tid("rules-draft-row-" + str(next(v for v in ui.mock.versions.values() if v["status"] == "draft")["id"]))).to_contain_text("photos by camera")
    expect(ui.tid("rules-history")).to_contain_text("v12")


def test_packaged_defaults_and_an_active_file_you_activated_yourself(make_r):
    ui = make_r()
    ui.mock.source, ui.mock.active_path = "default", None
    start(ui)
    expect(ui.tid("rules-active-source")).to_have_text("Packaged defaults · not yet customised")
    ui2 = make_r()
    ui2.mock.versions[12]["created_by"] = "ui"
    start(ui2)
    expect(ui2.tid("rules-outside-change")).to_have_count(0)
    expect(ui2.tid("rules-active")).to_have_attribute("data-owned", "true")


def test_an_active_file_that_does_not_parse_is_called_out_in_red_with_its_error_worded(make_r):
    ui = make_r()
    ui.mock.active_text = ACTIVE_TEXT.replace('category = ["photo"]\nname_regex', 'category = ["foto"]\nname_regex', 1)
    ui.mock.versions[12]["text"] = ui.mock.active_text
    start(ui)
    bad = ui.tid("rules-active-invalid")
    expect(bad).to_have_attribute("data-code", "UNKNOWN_CATEGORY")
    expect(bad).to_contain_text("every consolidate and reorganize plan fails")
    expect(bad).to_contain_text("Rule 1 (screenshots): Unknown category “foto”.")


def test_new_from_active_and_new_from_defaults_create_a_draft_and_open_it(make_r):
    ui = make_r()
    start(ui)
    ui.tid("rules-new-active").click()
    expect(ui.tid("view-rules-edit")).to_be_visible()
    body = calls(ui, "POST", r"/v1/rules/versions")[-1].body
    assert body["text"] == ACTIVE_TEXT and body["base_id"] == 12
    ui.go("#/rules")
    ui.tid("rules-new-default").click()
    expect(ui.tid("view-rules-edit")).to_be_visible()
    assert "(defaults)" in calls(ui, "POST", r"/v1/rules/versions")[-1].body["text"]


def test_a_draft_can_be_deleted_after_a_question(make_r):
    ui = make_r()
    d = ui.mock.add_draft()
    start(ui)
    ui.tid(f"rules-draft-delete-{d['id']}").click()
    ui.tid("dialog-ok").click()
    expect(ui.tid(f"rules-draft-row-{d['id']}")).to_have_count(0)
    assert calls(ui, "DELETE", rf"/v1/rules/versions/{d['id']}")


def test_use_in_consolidate_and_reorganize_from_the_active_rules(make_r):
    ui = make_r()
    start(ui)
    ui.tid("rules-use-consolidate").click()
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "consolidate")
    expect(ui.tid("rules-field")).to_have_attribute("data-version", "")
    expect(ui.tid("rules-field-text")).to_contain_text(f"Active rules ({ACTIVE_PATH})")
    ui.go("#/rules")
    ui.tid("rules-use-reorganize").click()
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "reorganize")


# --------------------------------------------------------------------------- the editor: typing, check, errors

def test_typing_is_checked_once_after_a_200_ms_pause_and_errors_show_in_the_gutter_and_the_list(make_r):
    ui = make_r()
    editor(ui)
    expect(ui.tid("rules-check-ok")).to_be_visible()
    before = len(calls(ui, "POST", r"/v1/rules/check"))
    type_at_end(ui, '\n[[rule]]\nname = "x"\ncategory = ["foto"]\ntarget = "X/{name}"\n')
    expect(ui.tid("rules-error-1")).to_be_visible()
    assert len(calls_since(ui, before, "POST", r"/v1/rules/check")) == 1             # one request for the whole burst
    err = ui.tid("rules-error-1")
    expect(err).to_have_attribute("data-code", "UNKNOWN_CATEGORY")
    expect(err).to_contain_text("Rule 4 (x): Unknown category “foto”.")
    assert ui.page.locator('[data-testid="rules-gutter"] [data-mark="error"]').count() == 1
    line = int(ui.page.locator('[data-testid="rules-gutter"] [data-mark="error"]').get_attribute("data-line"))
    assert ui.tid("rules-text").input_value().split("\n")[line - 1].startswith("category")
    ui.tid(f"rules-goto-{line}").click()
    caret = ui.page.evaluate("(() => { const a = document.querySelector('[data-testid=rules-text]'); return a.value.slice(0, a.selectionStart).split('\\n').length; })()")
    assert caret == line
    expect(ui.tid("rules-text")).to_be_focused()


def test_a_stale_check_answer_never_replaces_the_answer_for_the_text_on_screen(make_r):
    ui = make_r()
    editor(ui)
    expect(ui.tid("rules-check-ok")).to_be_visible()
    ui.page.evaluate("""() => {
        const real = window.fetch; let n = 0;
        window.fetch = (url, opts) => {
            if (!String(url).includes('/v1/rules/check')) return real(url, opts);
            n += 1; const mine = n;
            return real(url, opts).then((r) => new Promise((done) => setTimeout(() => done(r), mine === 1 ? 1200 : 0)));
        };
    }""")
    replace_text(ui, ACTIVE_TEXT.replace('["photo"]\ntarget', '["foto"]\ntarget', 1))      # its (bad) answer is held back
    ui.page.wait_for_timeout(500)
    replace_text(ui, ACTIVE_TEXT)                                                          # the text is good again; this answer is fast
    expect(ui.tid("rules-check-ok")).to_be_visible()
    ui.page.wait_for_timeout(1400)                                                         # the slow, bad answer arrived by now
    expect(ui.tid("rules-check-ok")).to_be_visible()
    expect(ui.tid("rules-error-1")).to_have_count(0)


def test_a_misspelled_category_gets_the_suggestion_and_both_languages_have_the_sentence(make_r):
    for locale, needle in (("en-US", "did you mean “photo”?"), ("zh-TW", "你是指「photo」嗎？")):
        ui = make_r(locale=locale)
        d = ui.mock.add_draft(ACTIVE_TEXT.replace('category = ["photo"]\ntarget', 'category = ["foto"]\ntarget', 1))
        editor(ui, d)
        expect(ui.tid("rules-error-1")).to_contain_text(needle, ignore_case=True)
        if locale == "zh-TW":
            expect(ui.tid("rules-error-1")).to_contain_text("第 2 條規則（photos）")


def test_several_errors_are_all_listed_not_only_the_first_and_warnings_are_amber(make_r):
    ui = make_r()
    text = ACTIVE_TEXT + '\n[[rule]]\nname = "every"\ntarget = "All/{name}"\n\n[[rule]]\nname = "late"\ncategory = ["pdf"]\ntarget = "Late/{name}"\n\n[[rule]]\ncategory = ["pdf"]\n'
    editor(ui, ui.mock.add_draft(text))
    expect(ui.tid("rules-error-1")).to_have_attribute("data-code", "NAME_REQUIRED")
    expect(ui.tid("rules-warning-1")).to_have_attribute("data-code", "RULE_SHADOWED")
    expect(ui.tid("rules-warning-1")).to_contain_text("never matches")
    expect(ui.page.locator('[data-testid="rules-gutter"] [data-mark="warning"]')).to_have_count(1)
    expect(ui.tid("rules-check-ok")).to_have_count(0)


def test_the_gutter_has_one_number_per_line_and_follows_the_scroll(make_r):
    ui = make_r()
    text = ACTIVE_TEXT + "\n".join(f"# filler {i}" for i in range(120)) + "\n"
    editor(ui, ui.mock.add_draft(text))
    nums = ui.page.locator('[data-testid="rules-gutter"] .rt-nums').inner_text().split("\n")
    assert nums[0] == "1" and nums[-1] == str(len(text.split("\n")))
    ui.page.evaluate("document.querySelector('[data-testid=rules-text]').scrollTop = 400")
    expect(ui.page.locator('[data-testid="rules-gutter"] .rt-inner')).to_have_attribute("style", re.compile(r"translateY\(-400px\)"))


# --------------------------------------------------------------------------- the outline

def test_the_outline_lists_the_rules_in_order_and_move_down_sends_the_transform_and_applies_the_text(make_r):
    ui = make_r()
    editor(ui)
    expect(ui.tid("rules-outline-item-1")).to_have_attribute("data-name", "screenshots")
    expect(ui.tid("rules-outline-item-3")).to_have_attribute("data-name", "documents")
    ui.tid("rules-outline-item-1").click()
    ui.tid("rules-move-down").click()
    expect(ui.tid("rules-outline-item-2")).to_have_attribute("data-name", "screenshots")
    body = calls(ui, "POST", r"/v1/rules/transform")[-1].body
    assert body["op"] == {"op": "move", "index": 1, "to": 2} and body["text"] == ACTIVE_TEXT
    value = ui.tid("rules-text").input_value()
    assert value.index('name = "photos"') < value.index('name = "screenshots"')
    assert '# Before "photos": a screenshot' in value                    # the comment travelled with its rule
    expect(ui.tid("rules-outline-item-2")).to_have_attribute("data-change", "moved")
    ui.tid("toast-action").click()                                       # Undo
    expect(ui.tid("rules-text")).to_have_value(ACTIVE_TEXT)


def test_remove_sends_the_index_and_keeps_every_other_line(make_r):
    ui = make_r()
    editor(ui)
    ui.tid("rules-outline-item-2").click()
    ui.tid("rules-remove").click()
    expect(ui.tid("rules-outline-item-3")).to_have_count(0)
    assert calls(ui, "POST", r"/v1/rules/transform")[-1].body["op"] == {"op": "remove", "index": 2}
    assert 'name = "photos"' not in ui.tid("rules-text").input_value()
    assert "# Paper and scans." in ui.tid("rules-text").input_value()
    expect(ui.tid("rules-save-state")).to_have_attribute("data-state", re.compile("dirty|saving|saved"))


def test_a_refused_transform_is_worded_and_leaves_the_text_alone(make_r):
    ui = make_r()
    editor(ui)
    ui.tid("rules-outline-item-1").click()
    ui.page.route("**/v1/rules/transform", lambda route: route.fulfill(status=400, content_type="application/json", body=json.dumps(
        {"error": "x", "code": "RULES_CANNOT_EDIT", "params": {"reason": "mismatch", "line": None, "rule_index": None, "rule": None, "key": None}})))
    ui.tid("rules-move-down").click()
    expect(ui.tid("toast").filter(has_text="could not be changed automatically")).to_be_visible()
    expect(ui.tid("toast")).to_contain_text("the result would not be what you asked for")
    expect(ui.tid("rules-text")).to_have_value(ACTIVE_TEXT)


def test_the_insert_form_renders_an_example_and_sends_the_rule_as_data(make_r):
    ui = make_r()
    editor(ui)
    ui.tid("rules-outline-item-2").click()
    ui.tid("rules-insert").click()
    expect(ui.tid("rules-insert-form")).to_be_visible()
    expect(ui.tid("rules-insert-after")).to_have_value("2")
    ui.tid("rules-insert-name").fill("receipts")
    ui.tid("rules-insert-cat-pdf").check()
    ui.tid("rules-insert-ext").fill(".PDF, tiff")
    ui.tid("rules-insert-regex").fill("(?i)receipt")
    ui.tid("rules-insert-min").fill("10K")
    ui.tid("rules-insert-target").fill("Receipts/")
    ui.tid("rules-insert-var-year").click()
    ui.tid("rules-insert-target").press_sequentially("/")
    ui.tid("rules-insert-var-name").click()
    expect(ui.tid("rules-insert-rendered")).to_have_text("Receipts/2019/IMG_20190304_1021.jpg")
    ui.tid("rules-insert-submit").click()
    expect(ui.tid("rules-insert-form")).to_have_count(0)
    body = calls(ui, "POST", r"/v1/rules/transform")[-1].body
    assert body["op"] == {"op": "insert", "after": 2, "rule": {
        "name": "receipts", "category": ["pdf"], "ext": ["pdf", "tiff"], "name_regex": "(?i)receipt", "min_size": 10240, "target": "Receipts/{year}/{name}"}}
    expect(ui.tid("rules-outline-item-3")).to_have_attribute("data-name", "receipts")
    expect(ui.tid("rules-outline-item-3")).to_have_attribute("data-change", "added")
    assert ui.tid("rules-text").input_value().count("[[rule]]") == 4


def test_the_insert_form_needs_a_name_and_a_target_and_a_valid_size(make_r):
    ui = make_r()
    editor(ui)
    ui.tid("rules-insert").click()
    expect(ui.tid("rules-insert-submit")).to_be_disabled()
    ui.tid("rules-insert-name").fill("x")
    ui.tid("rules-insert-target").fill("X/{name}")
    expect(ui.tid("rules-insert-submit")).to_be_enabled()
    ui.tid("rules-insert-min").fill("big")
    expect(ui.tid("rules-insert-submit")).to_be_disabled()


# --------------------------------------------------------------------------- drafts: autosave, reload, conflicts

def test_a_draft_autosaves_two_seconds_after_typing_stops_with_its_revision(make_r):
    ui = make_r()
    d = editor(ui)
    expect(ui.tid("rules-save-state")).to_have_attribute("data-state", "saved")
    type_at_end(ui, "# more\n")
    expect(ui.tid("rules-save-state")).to_have_attribute("data-state", "dirty")
    assert not calls(ui, "PUT", rf"/v1/rules/versions/{d['id']}")                   # not yet
    expect(ui.tid("rules-save-state")).to_have_attribute("data-state", "saved", timeout=6000)
    put = calls(ui, "PUT", rf"/v1/rules/versions/{d['id']}")
    assert len(put) == 1 and put[0].body["revision"] == 0 and put[0].body["text"].endswith("# more\n")
    type_at_end(ui, "# again\n")
    expect(ui.tid("rules-save-state")).to_have_attribute("data-state", "saved", timeout=6000)
    assert calls(ui, "PUT", rf"/v1/rules/versions/{d['id']}")[-1].body["revision"] == 1


def test_unsaved_text_survives_a_reload_through_session_storage(make_r):
    ui = make_r()
    d = editor(ui)
    type_at_end(ui, "# kept\n")
    ui.page.reload()                                                                # before the 2 s are over
    expect(ui.tid("rules-text")).to_have_value(d["text"] + "# kept\n")
    expect(ui.tid("rules-save-state")).to_have_attribute("data-state", re.compile("dirty|saving|saved"))


def test_a_draft_another_tab_changed_asks_to_reload_or_save_as_new(make_r):
    ui = make_r()
    d = editor(ui)
    ui.mock.versions[d["id"]]["revision"] = 5                                       # saved elsewhere meanwhile
    type_at_end(ui, "# mine\n")
    box = ui.tid("rules-draft-conflict")
    expect(box).to_be_visible(timeout=6000)
    expect(box).to_contain_text(f"Draft v{d['id']} was changed somewhere else")
    expect(ui.tid("rules-save-state")).to_have_attribute("data-state", "conflict")
    ui.tid("rules-conflict-new").click()
    body = calls(ui, "POST", r"/v1/rules/versions")[-1].body
    assert body["text"].endswith("# mine\n") and body["base_id"] == 12
    expect(ui.tid("view-rules-edit")).not_to_have_attribute("data-id", str(d["id"]))
    ui2 = make_r()
    d2 = editor(ui2)
    ui2.mock.versions[d2["id"]]["revision"] = 5
    ui2.mock.versions[d2["id"]]["text"] = ACTIVE_TEXT + "# theirs\n"
    type_at_end(ui2, "# mine\n")
    expect(ui2.tid("rules-draft-conflict")).to_be_visible(timeout=6000)
    ui2.tid("rules-conflict-reload").click()
    expect(ui2.tid("rules-text")).to_have_value(ACTIVE_TEXT + "# theirs\n")
    expect(ui2.tid("rules-draft-conflict")).to_have_count(0)


def test_a_version_that_is_not_a_draft_opens_its_review_instead_of_the_editor(make_r):
    ui = make_r()
    p = ui.mock.add_proposal()
    start(ui, f"#/rules/edit/{p['id']}")
    expect(ui.tid("view-rules-version")).to_be_visible()
    assert ui.page.evaluate("location.hash") == f"#/rules/v/{p['id']}"


# --------------------------------------------------------------------------- the live preview

def test_the_preview_runs_600_ms_after_the_last_keystroke_and_only_when_the_text_is_valid(make_r):
    ui = make_r()
    editor(ui)
    expect(ui.tid("rules-preview-changed")).to_be_visible()
    before = len(calls(ui, "POST", r"/v1/rules/preview"))
    type_at_end(ui, '\n[[rule]]\nname = "docs2"\ncategory = ["pdf"]\ntarget = "Papers/{name}"\n')
    expect(ui.tid("rules-rule-row-docs2")).to_be_visible()
    mine = calls_since(ui, before, "POST", r"/v1/rules/preview")
    assert len(mine) == 1 and mine[0].body["text"].endswith('Papers/{name}"\n') and mine[0].body["mode"] == "auto"
    assert mine[0].body["filter"] == "changed" and mine[0].body["limit"] == 100
    # a broken text is checked but not previewed
    n = len(calls(ui, "POST", r"/v1/rules/preview"))
    type_at_end(ui, '[[rule]]\ncategory = ["pdf"]\n')
    expect(ui.tid("rules-error-1")).to_be_visible()
    ui.page.wait_for_timeout(900)
    assert len(calls(ui, "POST", r"/v1/rules/preview")) == n
    expect(ui.tid("rules-preview-blocked")).to_be_visible()


def test_the_preview_shows_changes_against_the_active_rules_per_rule_and_the_biggest_moves(make_r):
    ui = make_r()
    editor(ui, ui.mock.add_draft(PROPOSED_TEXT))
    expect(ui.tid("rules-preview-exact")).to_be_visible()
    expect(ui.tid("rules-preview-sampled")).to_have_count(0)
    expect(ui.tid("rules-preview-changed")).to_have_attribute("data-value", "8")     # the 8 receipts
    expect(ui.tid("rules-rule-row-receipts")).to_contain_text("new")
    expect(ui.tid("rules-rule-row-receipts")).to_have_attribute("data-edited", "8")
    expect(ui.tid("rules-rule-row-documents")).to_have_attribute("data-active", "17")
    expect(ui.tid("rules-rule-row-documents")).to_have_attribute("data-edited", "9")
    expect(ui.tid("rules-transition")).to_have_count(1)
    expect(ui.tid("rules-transition")).to_contain_text("documents")
    expect(ui.tid("rules-transition")).to_contain_text("receipts")
    expect(ui.tid("rules-transition")).to_contain_text("8")
    expect(ui.tid("rules-preview-fallback")).to_have_attribute("data-value", "3")
    expect(ui.tid("rules-preview-inplace")).to_have_attribute("data-value", "3")
    expect(ui.tid("rules-outline-count-3")).to_have_text("8")                          # the outline carries the counts
    rows = ui.tid("rules-file-row")
    expect(rows).to_have_count(8)
    expect(rows.first).to_contain_text("Receipts/2019/receipt_0.pdf")
    expect(rows.first).to_contain_text("Documents/2019/receipt_0.pdf")
    expect(ui.tid("rules-rule-empty-receipts")).to_have_count(0)


def test_a_rule_with_no_match_is_marked(make_r):
    ui = make_r()
    editor(ui, ui.mock.add_draft(ACTIVE_TEXT + '\n[[rule]]\nname = "videos"\ncategory = ["video"]\ntarget = "Videos/{name}"\n'))
    expect(ui.tid("rules-rule-empty-videos")).to_be_visible()


def test_sampled_numbers_are_marked_with_an_approximation_sign_and_count_all_asks_for_the_exact_pass(make_r):
    ui = make_r()
    ui.mock.sampled = True
    editor(ui, ui.mock.add_draft(ACTIVE_TEXT.replace("Photos/", "Pictures/")))
    expect(ui.tid("rules-preview-sampled")).to_contain_text("Estimated from a sample of 39 of 39,000 files (0.1%)")
    expect(ui.tid("rules-preview-changed")).to_contain_text("≈ ")
    expect(ui.tid("rules-preview-changed")).to_have_attribute("data-value", "15000")
    expect(ui.tid("rules-rule-row-photos")).to_contain_text("≈ 15,000")
    expect(ui.tid("rules-outline-count-2")).to_have_text("≈ 15,000")
    ui.tid("rules-preview-exact-go").click()
    expect(ui.tid("rules-preview-exact")).to_be_visible()
    assert calls(ui, "POST", r"/v1/rules/preview")[-1].body["mode"] == "exact"
    expect(ui.tid("rules-preview-changed")).not_to_contain_text("≈")
    expect(ui.tid("rules-preview-changed")).to_have_attribute("data-value", "15")


def test_a_stale_preview_answer_is_dropped_and_a_superseded_one_is_ignored(make_r):
    ui = make_r()
    editor(ui, ui.mock.add_draft(ACTIVE_TEXT))
    expect(ui.tid("rules-preview-changed")).to_be_visible()
    ui.page.evaluate("""() => {
        const real = window.fetch; let n = 0;
        window.fetch = (url, opts) => {
            if (!String(url).includes('/v1/rules/preview')) return real(url, opts);
            n += 1; const mine = n;
            return real(url, opts).then((r) => new Promise((done) => setTimeout(() => done(r), mine === 1 ? 1500 : 0)));
        };
    }""")
    replace_text(ui, ACTIVE_TEXT.replace('target = "Photos/{year}/{name}"', 'target = "Slow/{name}"'))
    ui.page.wait_for_timeout(1000)                                                   # the first preview is out, held back
    replace_text(ui, ACTIVE_TEXT.replace('target = "Photos/{year}/{name}"', 'target = "Fast/{name}"'))
    expect(ui.page.locator('[data-testid="rules-file-row"] .rp-new').first).to_contain_text("Fast/", timeout=4000)
    ui.page.wait_for_timeout(1500)                                                   # the slow answer arrived meanwhile
    expect(ui.page.locator('[data-testid="rules-file-row"] .rp-new').first).to_contain_text("Fast/")
    assert not ui.page.locator('[data-testid="rules-file-row"] .rp-new', has_text="Slow/").count()
    # superseded by a newer request elsewhere: no error is shown
    ui.mock.supersede_next = True
    replace_text(ui, ACTIVE_TEXT.replace('target = "Photos/{year}/{name}"', 'target = "Third/{name}"'))
    ui.page.wait_for_timeout(1000)
    expect(ui.tid("rules-preview-error")).to_have_count(0)
    expect(ui.tid("rules-preview-working")).to_have_count(0)


def test_the_file_list_has_tabs_filters_and_pages_of_100_and_an_expired_preview_is_recomputed(make_r):
    ui = make_r()
    ui.mock.files = ui.mock.files + [{"remote": "gdrive", "path": f"Bulk/img{i:03d}.jpg", "year": "2024", "entry_id": 1000 + i, "name": f"img{i:03d}.jpg", "ext": "jpg",
                                      "category": "photo", "size": 10} for i in range(130)]
    editor(ui, ui.mock.add_draft(ACTIVE_TEXT.replace("Photos/", "Pics/")))
    expect(ui.tid("rules-files-total")).to_contain_text("100 of 145 files")
    expect(ui.tid("rules-file-row")).to_have_count(100)
    ui.tid("rules-files-more").click()
    expect(ui.tid("rules-file-row")).to_have_count(145)
    assert calls(ui, "GET", r"/v1/rules/previews/[^/]+/files")[-1].query["offset"] == "100"
    expect(ui.tid("rules-files-more")).to_have_count(0)
    ui.tid("rules-files-tab-fallback").click()
    expect(ui.tid("rules-file-row")).to_have_count(3)
    assert calls(ui, "GET", r"/v1/rules/previews/[^/]+/files")[-1].query["filter"] == "fallback"
    ui.tid("rules-files-tab-all").click()
    expect(ui.tid("rules-files-total")).to_contain_text("100 of 169 files")
    ui.tid("rules-rule-row-screenshots").locator("button").first.click()
    expect(ui.tid("rules-files-tab-rule:screenshots")).to_have_attribute("aria-selected", "true")
    expect(ui.tid("rules-file-row")).to_have_count(4)
    ui.tid("rules-files-q").fill("screenshot 2")
    expect(ui.tid("rules-file-row")).to_have_count(1)
    ui.tid("rules-files-q").fill("")
    ui.mock.previews.clear()                                                         # the server forgot the preview
    n = len(calls(ui, "POST", r"/v1/rules/preview"))
    ui.tid("rules-files-tab-fallback").click()
    expect(ui.tid("rules-file-row")).to_have_count(3)
    assert len(calls(ui, "POST", r"/v1/rules/preview")) == n + 1                     # PREVIEW_EXPIRED: computed again


def test_scope_clouds_and_folder_go_into_the_preview_request(make_r):
    ui = make_r()
    editor(ui)
    expect(ui.tid("rules-preview-changed")).to_be_visible()
    ui.tid("rules-scope-remote").select_option("dropbox")
    ui.tid("rules-scope-path").fill("Pictures")
    ui.tid("rules-scope-root").fill("Sorted")
    expect(ui.tid("rules-preview-scope-files")).to_contain_text("5", timeout=4000)
    body = calls(ui, "POST", r"/v1/rules/preview")[-1].body
    assert body["remotes"] == ["dropbox"] and body["path_prefix"] == "Pictures" and body["target_root"] == "Sorted"


def test_explain_shows_each_rule_in_order_with_the_first_key_that_failed(make_r):
    ui = make_r()
    editor(ui, ui.mock.add_draft(ACTIVE_TEXT))
    ui.tid("rules-explain-input").fill("gdrive:Scans/receipt_0.pdf")
    ui.tid("rules-explain-go").click()
    edited = ui.tid("rules-explain-edited")
    expect(edited).to_contain_text("Documents/2019/receipt_0.pdf")
    expect(edited.locator('[data-result="skip"]').first).to_contain_text("category is pdf, the rule wants photo")
    expect(edited.locator('[data-result="match"]')).to_contain_text("documents")
    body = calls(ui, "POST", r"/v1/rules/explain")[-1].body
    assert body["remote"] == "gdrive" and body["path"] == "Scans/receipt_0.pdf" and body["text"] == ACTIVE_TEXT
    ui.tid("rules-explain-input").fill("nonsense")
    ui.tid("rules-explain-go").click()
    expect(ui.tid("rules-explain-error")).to_contain_text("cloud:path")
    # from a file row of the list
    ui.tid("rules-files-tab-all").click()
    ui.tid("rules-file-explain").first.click()
    expect(ui.tid("rules-explain-edited")).to_be_visible()


# --------------------------------------------------------------------------- activation

def test_activating_shows_the_file_the_diff_and_the_count_and_sends_the_sha_it_showed(make_r):
    ui = make_r()
    d = ui.mock.add_draft(ACTIVE_TEXT.replace("Photos/", "Pictures/"))
    editor(ui, d)
    ui.tid("rules-activate").click()
    dialog = ui.tid("rules-activate-dialog")
    expect(dialog).to_be_visible()
    expect(ui.tid("rules-activate-file")).to_contain_text(f"This writes the exact text to {ACTIVE_PATH}.")
    diff = ui.tid("rules-activate-diff")
    expect(diff.locator('[data-op="-"]')).to_contain_text('target = "Photos/{year}/{name}"')
    expect(diff.locator('[data-op="+"]')).to_contain_text('target = "Pictures/{year}/{name}"')
    expect(ui.tid("rules-activate-count")).to_contain_text("15 files would get a different destination")
    assert calls(ui, "POST", r"/v1/rules/preview")[-1].body["mode"] == "exact"
    ui.tid("rules-activate-confirm").click()
    expect(dialog).to_have_count(0)
    body = calls(ui, "POST", rf"/v1/ui/rules/versions/{d['id']}/activate")[-1].body
    assert body == {"base_sha256": sha(ACTIVE_TEXT)}
    assert calls(ui, "POST", rf"/v1/ui/rules/versions/{d['id']}/activate")[-1].headers["x-csrf-token"] == MockApi.CSRF
    expect(ui.tid("toast").filter(has_text=f"Rules v{d['id']} are active now")).to_be_visible()
    expect(ui.tid("view-rules")).to_be_visible()
    expect(ui.tid("rules-active-source")).to_contain_text(f"v{d['id']}")
    assert ui.mock.active_text == d["text"]


def test_escape_closes_the_activation_dialog_even_when_focus_is_outside_it(make_r):
    ui = make_r()
    editor(ui, ui.mock.add_draft(ACTIVE_TEXT.replace("Photos/", "Pictures/")))
    ui.tid("rules-activate").click()
    dialog = ui.tid("rules-activate-dialog")
    expect(dialog).to_be_visible()
    ui.page.evaluate("document.activeElement.blur()")                                # focus on the page body
    assert ui.page.evaluate("document.activeElement === document.body")
    ui.page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert not calls(ui, "POST", r"/activate")
    ui.tid("rules-activate").click()                                                # and an Escape from inside still closes it once
    expect(dialog).to_be_visible()
    ui.page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    ui.tid("rules-activate").click()                                                # an Escape that belongs to an IME composition does not
    expect(dialog).to_be_visible()
    ui.page.evaluate("document.activeElement.blur()")
    ui.page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', isComposing: true, bubbles: true}))")
    expect(dialog).to_be_visible()


def test_activate_is_disabled_for_a_text_with_errors(make_r):
    ui = make_r()
    editor(ui, ui.mock.add_draft(ACTIVE_TEXT.replace('["photo"]\ntarget', '["foto"]\ntarget', 1)))
    expect(ui.tid("rules-error-1")).to_be_visible()
    expect(ui.tid("rules-activate")).to_be_disabled()


def test_when_the_file_changed_meanwhile_the_dialog_reloads_and_says_so(make_r):
    ui = make_r()
    d = ui.mock.add_draft(ACTIVE_TEXT.replace("Photos/", "Pictures/"))
    editor(ui, d)
    ui.tid("rules-activate").click()
    expect(ui.tid("rules-activate-confirm")).to_be_enabled()
    ui.mock.make_active(ACTIVE_TEXT + "# somebody else\n", by="cli", days=0)
    ui.tid("rules-activate-confirm").click()
    err = ui.tid("rules-activate-error")
    expect(err).to_have_attribute("data-code", "RULES_ACTIVE_CHANGED")
    expect(err).to_contain_text("The active rules changed after you opened this")
    expect(err).to_contain_text("The diff above is the new one")
    expect(ui.tid("rules-activate-diff").locator('[data-op="-"]').filter(has_text="somebody else")).to_be_visible()
    ui.tid("rules-activate-confirm").click()                                         # the new diff was reviewed: this one goes through
    expect(ui.tid("rules-activate-dialog")).to_have_count(0)
    assert calls(ui, "POST", rf"/v1/ui/rules/versions/{d['id']}/activate")[-1].body["base_sha256"] == sha(ACTIVE_TEXT + "# somebody else\n")


def test_an_unwritable_file_names_the_path_and_offers_the_text_and_the_command(make_r):
    ui = make_r()
    d = ui.mock.add_draft(ACTIVE_TEXT + "# x\n")
    ui.mock.activate_error = (409, "RULES_FILE_UNWRITABLE", {"path": ACTIVE_PATH})
    editor(ui, d)
    ui.tid("rules-activate").click()
    ui.tid("rules-activate-confirm").click()
    err = ui.tid("rules-activate-error")
    expect(err).to_contain_text(f"The rules file {ACTIVE_PATH} cannot be written")
    expect(err).to_contain_text(f"drivemerge rules activate {d['id']}")
    expect(ui.tid("rules-activate-copy-text")).to_be_visible()
    expect(ui.tid("rules-activate-dialog")).to_be_visible()


def test_restoring_an_older_version_goes_through_the_same_dialog(make_r):
    ui = make_r()
    old = ui.mock.make_active(ACTIVE_TEXT + "# old\n", days=5)
    ui.mock.make_active(ACTIVE_TEXT + "# newer\n", days=1)
    start(ui)
    ui.tid(f"rules-restore-{old['id']}").click()
    expect(ui.tid("rules-activate-dialog")).to_be_visible()
    expect(ui.tid("rules-activate-diff")).to_contain_text("# old")
    ui.tid("rules-activate-confirm").click()
    expect(ui.tid("rules-activate-dialog")).to_have_count(0)
    assert ui.mock.active_text.endswith("# old\n")
    expect(ui.tid(f"rules-history-row-{old['id']}")).to_have_attribute("data-status", "active")


# --------------------------------------------------------------------------- the read-only page

def test_without_approvals_everything_but_activation_works_and_the_command_replaces_the_button(make_r):
    ui = make_r(approvals_enabled=False)
    old = ui.mock.make_active(ACTIVE_TEXT + "# old\n", days=5)
    ui.mock.make_active(ACTIVE_TEXT + "# newer\n", days=1)
    d = ui.mock.add_draft()
    start(ui)
    expect(ui.tid(f"rules-restore-{old['id']}")).to_contain_text(f"drivemerge rules activate {old['id']}")
    expect(ui.tid(f"rules-restore-{old['id']}").locator("button")).to_have_count(1)
    ui.go(f"#/rules/edit/{d['id']}")
    expect(ui.tid("rules-text")).to_be_visible()
    expect(ui.tid("rules-activate")).to_have_count(0)
    cli = ui.tid("rules-cli")
    expect(cli).to_be_visible()
    expect(ui.tid("rules-cli-command")).to_have_text(f"drivemerge rules activate {d['id']}")
    expect(cli).to_contain_text("shows the same diff and asks you to confirm")
    expect(ui.tid("rules-preview-changed")).to_be_visible()                          # check and preview work
    ui.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    type_at_end(ui, "# unsaved\n")
    ui.tid("rules-cli-copy").click()                                                 # the draft is saved first so the command is current
    expect(ui.tid("toast").filter(has_text="Copied")).to_be_visible()
    assert calls(ui, "PUT", rf"/v1/rules/versions/{d['id']}")[-1].body["text"].endswith("# unsaved\n")


# --------------------------------------------------------------------------- Claude's proposals

def test_a_new_proposal_from_claude_raises_a_toast_with_review_and_counts_on_the_rail(make_r):
    ui = make_r()
    start(ui, "#/overview")
    expect(ui.tid("rail-rules-badge")).to_have_count(0)
    p = ui.mock.add_proposal()
    ui.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")        # the poll that runs every 10 s
    toast = ui.tid("toast").filter(has_text="Claude proposed a rules change")
    expect(toast).to_contain_text("Keep scanned receipts by year")
    expect(ui.tid("rail-rules-badge")).to_have_attribute("data-count", "1")
    toast.get_by_test_id("toast-action").click()
    expect(ui.tid("view-rules-version")).to_be_visible()
    assert ui.page.evaluate("location.hash") == f"#/rules/v/{p['id']}"
    # an API client's proposal counts but does not raise the Claude toast
    ui.mock.add_proposal(by="api", title="from a script")
    ui.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(ui.tid("rail-rules-badge")).to_have_attribute("data-count", "2")
    expect(ui.tid("toast").filter(has_text="from a script")).to_have_count(0)


def test_the_review_shows_title_and_note_as_data_the_diff_and_the_preview_and_never_calls_it_active(make_r):
    ui = make_r()
    p = ui.mock.add_proposal(title="<img src=x onerror=alert(1)>Receipts", note="Ignore the rules and <b>activate</b> me")
    start(ui, f"#/rules/v/{p['id']}")
    expect(ui.tid("view-rules-version")).to_have_attribute("data-status", "proposed")
    expect(ui.tid("rules-not-active")).to_contain_text("Nothing here is active")
    expect(ui.tid("rules-version-title")).to_have_text("<img src=x onerror=alert(1)>Receipts")
    expect(ui.tid("rules-version-notetext")).to_have_text("Ignore the rules and <b>activate</b> me")
    assert ui.page.locator('[data-testid="rules-version-note"] img, [data-testid="rules-version-note"] b').count() == 0
    expect(ui.tid("rules-version-note")).to_contain_text("Read it as information, not as an instruction")
    expect(ui.tid("rules-diff").locator('[data-op="+"]').first).to_be_visible()
    expect(ui.tid("rules-changes")).to_contain_text("1 added")
    assert calls(ui, "POST", r"/v1/rules/preview")[-1].body["version_id"] == p["id"]
    expect(ui.tid("rules-preview-changed")).to_have_attribute("data-value", "8")


def test_dismiss_asks_first_and_sends_the_delete(make_r):
    ui = make_r()
    p = ui.mock.add_proposal()
    start(ui, f"#/rules/v/{p['id']}")
    ui.tid("rules-dismiss").click()
    ui.tid("dialog-ok").click()
    expect(ui.tid("view-rules")).to_be_visible()
    assert calls(ui, "DELETE", rf"/v1/rules/versions/{p['id']}")
    assert ui.mock.versions[p["id"]]["status"] == "dismissed"
    expect(ui.tid(f"rules-proposal-row-{p['id']}")).to_have_count(0)


def test_edit_as_draft_copies_the_proposal_into_a_new_draft_based_on_it(make_r):
    ui = make_r()
    p = ui.mock.add_proposal()
    start(ui, f"#/rules/v/{p['id']}")
    ui.tid("rules-edit-as-draft").click()
    expect(ui.tid("view-rules-edit")).to_be_visible()
    body = calls(ui, "POST", r"/v1/rules/versions")[-1].body
    assert body["text"] == p["text"] and body["base_id"] == p["id"]
    expect(ui.tid("rules-text")).to_have_value(p["text"])
    assert ui.mock.versions[p["id"]]["status"] == "proposed"                         # never edited in place


def test_a_proposal_can_be_activated_from_its_review(make_r):
    ui = make_r()
    p = ui.mock.add_proposal()
    start(ui, f"#/rules/v/{p['id']}")
    ui.tid("rules-activate").click()
    expect(ui.tid("rules-activate-file")).to_be_visible()
    ui.tid("rules-activate-confirm").click()
    expect(ui.tid("view-rules")).to_be_visible()
    assert ui.mock.versions[p["id"]]["status"] == "active"


# --------------------------------------------------------------------------- from the rules into a plan

def test_use_in_plan_saves_the_draft_first_and_opens_the_forms_with_the_version_in_the_url(make_r):
    ui = make_r()
    d = editor(ui)
    type_at_end(ui, "# edit\n")
    ui.tid("rules-scope-root").fill("Sorted")
    ui.tid("rules-scope-path").fill("Scans")
    ui.tid("rules-use-plan").click()
    ui.tid("rules-use-consolidate").click()
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "consolidate")
    assert calls(ui, "PUT", rf"/v1/rules/versions/{d['id']}")[-1].body["text"].endswith("# edit\n")
    q = parse_qs(ui.page.evaluate("location.hash").partition("?")[2])
    assert q["rules"] == [str(d["id"])] and q["root"] == ["Sorted"] and q["path"] == ["Scans"]
    expect(ui.tid("rules-field")).to_have_attribute("data-version", str(d["id"]))
    expect(ui.tid("rules-field")).to_have_attribute("data-active", "false")
    expect(ui.tid("rules-field-text")).to_contain_text(f"Version {d['id']} (draft) — not active")
    expect(ui.tid("estimate-headline").or_(ui.tid("cons-stats"))).to_be_visible()
    assert calls(ui, "POST", r"/v1/plans/estimate")[-1].body["params"]["rules_version"] == d["id"]
    ui.tid("create-plan").click()
    expect(ui.tid("view-plan-review")).to_be_visible()
    assert calls(ui, "POST", r"/v1/plans")[-1].body["params"]["rules_version"] == d["id"]


def test_the_reorganize_form_estimates_and_creates_with_the_rules_version(make_r):
    ui = make_r()
    d = ui.mock.add_draft(ACTIVE_TEXT.replace("Photos/", "Pictures/"))
    start(ui, f"#/plans/new/reorganize?remote=gdrive&root=Sorted&path=Photos&rules={d['id']}")
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "reorganize")
    expect(ui.tid("reorg-remote")).to_have_value("gdrive")
    expect(ui.tid("reorg-root")).to_have_value("Sorted")
    expect(ui.tid("reorg-path")).to_have_value("Photos")
    expect(ui.tid("reorg-stats")).to_be_visible()
    est = calls(ui, "POST", r"/v1/plans/estimate")[-1].body
    assert est["kind"] == "reorganize" and est["params"] == {"remote": "gdrive", "target_root": "Sorted", "path_prefix": "Photos", "rules_version": d["id"]}
    expect(ui.tid("summary-moves")).to_contain_text("10")
    expect(ui.tid("placement").first).to_contain_text("trip0.jpg")
    ui.tid("create-plan").click()
    expect(ui.tid("view-plan-review")).to_be_visible()
    body = calls(ui, "POST", r"/v1/plans")[-1].body
    assert body["kind"] == "reorganize" and body["params"]["rules_version"] == d["id"] and body["params"]["remote"] == "gdrive"


def test_the_reorganize_form_keeps_its_state_in_the_url_only_lists_writable_clouds_and_has_a_button_on_the_list(make_r):
    ui = make_r()
    ui.mock.remotes[1]["writable"] = False
    start(ui, "#/plans")
    ui.tid("plans-new-reorganize").click()
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "reorganize")
    expect(ui.tid("reorg-remote").locator("option")).to_have_count(1)
    ui.tid("reorg-root").fill("Sorted")
    ui.page.wait_for_timeout(500)
    assert "root=Sorted" in ui.page.evaluate("location.hash")


def test_use_active_rules_clears_the_choice_and_edit_opens_the_editor_with_a_way_back(make_r):
    ui = make_r()
    d = ui.mock.add_draft()
    start(ui, f"#/plans/new/consolidate?target=gdrive&rules={d['id']}")
    expect(ui.tid("rules-field-text")).to_contain_text("draft")
    ui.tid("rules-field-edit").click()
    expect(ui.tid("view-rules-edit")).to_have_attribute("data-id", str(d["id"]))
    back = ui.tid("rules-back")
    expect(back).to_be_visible()
    back.click()
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "consolidate")
    assert f"rules={d['id']}" in ui.page.evaluate("location.hash") and "target=gdrive" in ui.page.evaluate("location.hash")
    ui.tid("rules-field-active").click()
    expect(ui.tid("rules-field")).to_have_attribute("data-version", "")
    ui.page.wait_for_timeout(500)
    assert "rules=" not in ui.page.evaluate("location.hash")
    ui.tid("rules-field-edit").click()                                               # from the active rules: a draft is made first
    expect(ui.tid("view-rules-edit")).to_be_visible()
    assert calls(ui, "POST", r"/v1/rules/versions")[-1].body["text"] == ACTIVE_TEXT


def test_the_plan_review_says_when_a_plan_was_built_with_rules_that_are_not_the_active_ones(make_r):
    ui = make_r()
    p = ui.mock.add_proposal()
    ref = {"source": "version", "version": p["id"], "sha256": p["sha256"], "path": None}
    pid = ui.mock.add_plan(kind="consolidate", ops=3, params={"target": "gdrive", "rules": p["text"], "rules_ref": ref})
    start(ui, f"#/plans/{pid}")
    box = ui.tid("plan-rules-box")
    expect(box).to_have_attribute("data-same", "false")
    expect(ui.tid("plan-rules-not-active")).to_contain_text(f"Built with proposed rules v{p['id']}, which are not the active rules.")
    expect(ui.tid("plan-rules-line")).to_contain_text(p["sha256"][:8])
    ui.tid("plan-rules-view").click()
    expect(ui.tid("plan-rules-text")).to_contain_text("receipts")
    ui.tid("plan-rules-edit").click()
    expect(ui.tid("view-rules-edit")).to_be_visible()
    assert calls(ui, "POST", r"/v1/rules/versions")[-1].body["text"] == p["text"]
    # a plan built with the active rules says so, one built with inline rules says that; an old plan has no box
    act = {"source": "file", "version": 12, "sha256": sha(ACTIVE_TEXT), "path": ACTIVE_PATH}
    same = ui.mock.add_plan(kind="reorganize", ops=2, params={"remote": "gdrive", "rules": ACTIVE_TEXT, "rules_ref": act})
    start(ui, f"#/plans/{same}")
    expect(ui.tid("plan-rules-box")).to_have_attribute("data-same", "true")
    expect(ui.tid("plan-rules-active")).to_be_visible()
    inline = ui.mock.add_plan(kind="consolidate", ops=2, params={"target": "gdrive", "rules": "x", "rules_ref": {"source": "inline", "version": None, "sha256": "e" * 64, "path": None}})
    start(ui, f"#/plans/{inline}")
    expect(ui.tid("plan-rules-not-active")).to_contain_text("rules passed along with the request")
    old = ui.mock.add_plan(kind="consolidate", ops=2, params={"target": "gdrive"})
    start(ui, f"#/plans/{old}")
    expect(ui.tid("plan-facts")).to_be_visible()
    expect(ui.tid("plan-rules-box")).to_have_count(0)


def test_explore_can_ask_where_the_rules_would_put_a_file(make_r):
    ui = make_r()
    start(ui, "#/explore?q=thesis")
    ui.tid("ex-expand").first.click()
    ui.tid("ex-rules").first.click()
    expect(ui.tid("view-rules-edit")).to_be_visible()
    d = calls(ui, "POST", r"/v1/rules/versions")[-1].body
    assert d["text"] == ACTIVE_TEXT
    assert "explain=" in ui.page.evaluate("location.hash")
    expect(ui.tid("rules-explain-input")).to_have_value(re.compile(r"^(gdrive|dropbox):"))
    # asking again reuses the draft that still equals the active rules
    n = len(calls(ui, "POST", r"/v1/rules/versions"))
    start(ui, "#/explore?q=thesis")
    ui.tid("ex-expand").first.click()
    ui.tid("ex-rules").first.click()
    expect(ui.tid("view-rules-edit")).to_be_visible()
    assert len(calls(ui, "POST", r"/v1/rules/versions")) == n


# --------------------------------------------------------------------------- narrow windows, both languages

def test_on_a_narrow_window_the_three_panes_become_tabs(make_r):
    ui = make_r(width=900)
    editor(ui)
    expect(ui.tid("rules-pane-tab-text")).to_be_visible()
    expect(ui.tid("rules-text")).to_be_visible()
    expect(ui.tid("rules-outline")).to_be_hidden()
    ui.tid("rules-pane-tab-outline").click()
    expect(ui.tid("rules-outline")).to_be_visible()
    expect(ui.tid("rules-text")).to_be_hidden()
    ui.tid("rules-pane-tab-preview").click()
    expect(ui.tid("rules-preview")).to_be_visible()
    wide = make_r(width=1440)
    editor(wide)
    expect(wide.tid("rules-pane-tab-text")).to_be_hidden()
    expect(wide.tid("rules-outline")).to_be_visible()
    expect(wide.tid("rules-text")).to_be_visible()
    expect(wide.tid("rules-preview")).to_be_visible()


def test_the_view_is_in_traditional_chinese_for_a_browser_that_asks_for_it(make_r):
    ui = make_r(locale="zh-TW")
    ui.mock.add_proposal()
    start(ui)
    expect(ui.tid("rules-active")).to_contain_text("使用中")
    expect(ui.tid("rules-active")).to_contain_text("3 條規則")
    expect(ui.tid("rules-proposals")).to_contain_text("等待審閱")
    expect(ui.tid("rules-proposals")).to_contain_text("新增 1 條")
    d = ui.mock.add_draft()
    ui.go(f"#/rules/edit/{d['id']}")
    expect(ui.tid("rules-save-state")).to_have_text("已儲存")
    expect(ui.tid("rules-check-ok")).to_contain_text("規則沒有錯誤（共 3 條）")
    expect(ui.tid("rules-preview-exact")).to_contain_text("精確數字")
    ui.tid("rules-activate").click()
    expect(ui.tid("rules-activate-file")).to_contain_text("這會把這份文字原封不動寫入")
    ui.tid("rules-activate-cancel").click()
    # the language button switches the sentences without a reload
    ui.tid("view-lang").click()
    expect(ui.tid("rules-save-state")).to_have_text("Saved")


# --------------------------------------------------------------------------- screenshots

SHOT_STATES = ("overview", "editor-errors", "editor-preview", "review", "readonly")


def _shoot(ui: Ui, name: str) -> None:
    ui.page.wait_for_timeout(450)
    ui.page.screenshot(path=str(SHOTS / f"rules-{name}.png"))


@pytest.mark.parametrize("lang, locale", [("en", "en-US"), ("zh", "zh-TW")])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 900])
def test_screenshots_of_the_rules_view(make_r, lang, locale, scheme, width):
    """rules-<state>-<lang>-<scheme>-<width>.png for a human to look at; not a visual assertion."""
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
    except OSError:
        pytest.skip("screenshot directory is not writable")
    tag = f"{lang}-{scheme}-{width}"
    ui = make_r(locale=locale, color_scheme=scheme, width=width, height=900 if width == 1440 else 820)
    p = ui.mock.add_proposal()
    ui.mock.make_active(ACTIVE_TEXT + "# older\n", days=20)
    ui.mock.make_active(ACTIVE_TEXT, by="ui", days=2)
    ui.mock.add_draft(title="photos by camera")
    start(ui)
    expect(ui.tid(f"rules-proposal-row-{p['id']}")).to_be_visible()
    _shoot(ui, f"overview-{tag}")
    broken = ui.mock.add_draft(ACTIVE_TEXT.replace('category = ["photo"]\ntarget', 'category = ["foto"]\ntarget', 1) + '\n[[rule]]\nname = "every"\ntarget = "All/{name}"\n\n[[rule]]\nname = "late"\ntarget = "Late/{name}"\n', title="broken")
    ui.go(f"#/rules/edit/{broken['id']}")
    expect(ui.tid("rules-error-1")).to_be_visible()
    _shoot(ui, f"editor-errors-{tag}")
    good = ui.mock.add_draft(PROPOSED_TEXT, title="receipts")
    ui.go(f"#/rules/edit/{good['id']}")
    if width < 1240:
        ui.tid("rules-pane-tab-outline").click()
    ui.tid("rules-outline-item-3").click()
    if width < 1240:
        ui.tid("rules-pane-tab-preview").click()
    expect(ui.tid("rules-rule-row-receipts")).to_be_visible()
    ui.tid("rules-explain-input").fill("gdrive:Scans/receipt_0.pdf")
    ui.tid("rules-explain-go").click()
    expect(ui.tid("rules-explain-edited")).to_be_visible()
    _shoot(ui, f"editor-preview-{tag}")
    ui.go(f"#/rules/v/{p['id']}")
    expect(ui.tid("rules-preview-changed")).to_be_visible()
    _shoot(ui, f"review-{tag}")
    ui.tid("rules-activate").click()
    expect(ui.tid("rules-activate-count")).to_contain_text(re.compile(r"\d"))
    _shoot(ui, f"activate-{tag}")
    ui.tid("rules-activate-cancel").click()
    ro = make_r(locale=locale, color_scheme=scheme, width=width, height=900 if width == 1440 else 820, approvals_enabled=False)
    d = ro.mock.add_draft(title="photos by camera")
    ro.mock.add_proposal()
    start(ro, f"#/rules/edit/{d['id']}")
    expect(ro.tid("rules-cli")).to_be_visible()
    expect(ro.tid("rules-preview-exact")).to_have_count(1)
    _shoot(ro, f"readonly-{tag}")
    assert (SHOTS / f"rules-readonly-{tag}.png").stat().st_size > 5_000


def test_the_fallback_is_worded_in_the_page_language_and_an_invalid_active_file_leaves_the_comparison_blank(make_r):
    ui = make_r(locale="zh-TW")
    editor(ui, ui.mock.add_draft(ACTIVE_TEXT))
    ui.tid("rules-files-tab-fallback").click()
    expect(ui.tid("rules-file-row").first.locator(".badge")).to_have_text("後備")   # the server's sentinel is never shown raw
    ui.mock.active_text = "[[rule]]\nname = 'x'\n"                                           # the active file stops parsing
    ui.tid("rules-text").fill(ACTIVE_TEXT + "# edit\n")
    expect(ui.tid("rules-preview-changed")).to_contain_text("–")
