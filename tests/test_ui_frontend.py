"""Browser tests for the visual workspace (src/drivemerge/web), in headless Chromium.

The page is served by a tiny static server that maps /ui/ and /ui/static/ like the real backend; every
/v1 call is answered by `MockApi` through Playwright's page.route, following docs/WORKSPACE.md section D.
So these tests pin the front end's side of the HTTP contract (request bodies, headers, flows) and nothing
about the Python backend.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

import drivemerge  # noqa: E402

pytestmark = pytest.mark.ui

WEB = Path(drivemerge.__file__).parent / "web"
SHOTS = Path(os.environ.get("DRIVEMERGE_UI_SHOTS", "/tmp/claude-0/ui-shots"))
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css"}
# The same policy the real server (ui_api.py) sends with index.html: the page must work under it.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


# --------------------------------------------------------------------------- static site

class _Static(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = urlparse(self.path).path
        if path in ("/ui", "/ui/"):
            rel = "index.html"
        elif path.startswith("/ui/static/"):
            rel = path[len("/ui/static/"):]
        else:
            return self._send(404, b"not found", "text/plain")
        target = (WEB / rel).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        return self._send(200, target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream"))

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # the browser gave up on a request (page navigated away)
            pass

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Static)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


# --------------------------------------------------------------------------- mock backend

class Call(NamedTuple):
    method: str
    path: str
    query: dict
    body: Any
    headers: dict


class Stage(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


SAMPLE = {
    "gdrive": {
        "研究": {
            "論文": {"thesis.pdf": 2_400_000, "draft.docx": 48_000},
            "資料": {"survey.csv": 120_000, "notes.txt": 3_000},
        },
        "學校": {"課程": {"syllabus.pdf": 80_000, "week1.pptx": 900_000}, "report.pdf": 52_000},
        "Photos": {"trip.jpg": 3_000_000},
    },
    "dropbox": {
        "學校": {"作業": {"hw1.docx": 22_000}, "report.pdf": 52_100},
        "Archive": {"old.zip": 9_000_000},
    },
}
REMOTES = [
    {"id": 1, "name": "gdrive", "provider": "drive", "writable": True, "case_insensitive": False},
    {"id": 2, "name": "dropbox", "provider": "dropbox", "writable": True, "case_insensitive": True},
]
CATEGORY = {"pdf": "pdf", "docx": "document", "txt": "document", "csv": "spreadsheet", "pptx": "presentation",
            "jpg": "photo", "zip": "archive"}


def _join(base: str, name: str) -> str:
    return f"{base}/{name}" if base else name


def _parent(path: str) -> str:
    return path.rpartition("/")[0]


def _name(path: str) -> str:
    return path.rpartition("/")[2]


def _get(tree: dict, remote: str, path: str):
    node: Any = tree[remote]
    for seg in filter(None, path.split("/")):
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def _stats(node: Any) -> tuple[int, int, int]:
    """(files, dirs, bytes) below a node."""
    if not isinstance(node, dict):
        return 1, 0, node
    files = dirs = size = 0
    for child in node.values():
        f, d, b = _stats(child)
        files, dirs, size = files + f, dirs + d + (1 if isinstance(child, dict) else 0), size + b
    return files, dirs, size


class MockApi:
    """Just enough of the backend: a namespace that replays the draft's actions, plus the human channel."""

    TOKEN = "TESTTOKEN"
    CSRF = "csrf-test-token"
    CODE = "a1b2c3d4"

    def __init__(self, *, approvals_enabled: bool = True, execution_enabled: bool = True):
        self.approvals_enabled = approvals_enabled
        self.execution_enabled = execution_enabled
        self.fs = copy.deepcopy(SAMPLE)
        self.calls: list[Call] = []
        self.preview_mode = "ok"
        self.job_polls = 0
        self.session_open = False
        self.next_draft_id = 1
        self.next_action_id = 1
        self.draft = self._new_draft("整理 測試")

    # ------------------------------------------------------------------ bookkeeping
    def _new_draft(self, title: str) -> dict:
        d = {"id": self.next_draft_id, "title": title, "status": "open", "version": 1, "preview_plan_id": None,
             "preview_version": None, "actions": []}
        self.next_draft_id += 1
        return d

    def calls_to(self, method: str, pattern: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and re.fullmatch(pattern, c.path)]

    def last(self, method: str, pattern: str) -> Call:
        found = self.calls_to(method, pattern)
        assert found, f"no {method} {pattern} call; saw {[(c.method, c.path) for c in self.calls]}"
        return found[-1]

    def external_action(self, *, created_by: str, items: list, dst: dict) -> None:
        """Something other than this page (Claude via MCP) stages a move."""
        self._commit({"kind": "move", "items": items, "dst": dst, "note": "Claude: group the thesis files"}, created_by)

    # ------------------------------------------------------------------ namespace
    def replay(self) -> tuple[dict, dict, list]:
        tree, flags, ghosts = copy.deepcopy(self.fs), {}, []
        self.moved = {"files": 0, "bytes": 0, "cross": 0, "remotes": set()}
        for a in self.draft["actions"]:
            if a["state"] == "active":
                self._apply(tree, flags, ghosts, a)
        return tree, flags, ghosts

    def _apply(self, tree: dict, flags: dict, ghosts: list, a: dict) -> list[dict]:
        warnings: list[dict] = []
        self.moved["remotes"].add(a["dst"]["remote"])
        dst = a["dst"]
        dnode = _get(tree, dst["remote"], dst["path"])
        if not isinstance(dnode, dict):
            raise Stage("DST_NOT_DIR", f"{dst['remote']}:{dst['path']} is not a folder")
        if a["kind"] == "mkdir":
            name = a["new_name"]
            if name in dnode:
                raise Stage("NOOP" if isinstance(dnode[name], dict) else "NAME_EXISTS", f"{name} exists")
            dnode[name] = {}
            flags[(dst["remote"], _join(dst["path"], name))] = {"virtual": True}
            return warnings
        for it in a["items"]:
            if not it["path"] or _get(tree, it["remote"], it["path"]) is None:
                raise Stage("SRC_NOT_FOUND", f"{it['remote']}:{it['path']} does not exist")
        moved = 0
        for it in a["items"]:
            node = _get(tree, it["remote"], it["path"])
            if isinstance(node, dict) and it["remote"] == dst["remote"] and (
                    dst["path"] == it["path"] or dst["path"].startswith(it["path"] + "/")):
                raise Stage("INTO_ITSELF", "cannot move a folder into itself")
            name = a.get("new_name") or _name(it["path"])
            target = _join(dst["path"], name)
            if (it["remote"], it["path"]) == (dst["remote"], target):
                continue
            existing = dnode.get(name)
            if existing is not None and not (isinstance(node, dict) and isinstance(existing, dict)):
                raise Stage("NAME_EXISTS", f"{dst['remote']}:{target} already exists")
            files, _, size = _stats(node)
            self.moved["files"] += files
            self.moved["bytes"] += size
            self.moved["cross"] += size if it["remote"] != dst["remote"] else 0
            self.moved["remotes"].add(it["remote"])
            del _get(tree, it["remote"], _parent(it["path"]))[_name(it["path"])]
            if existing is not None:
                self._merge(existing, node)
                warnings.append({"code": "MERGE", "message": f"merges into {dst['remote']}:{target}"})
            else:
                dnode[name] = node
            for key in [k for k in flags if k[0] == it["remote"] and (k[1] == it["path"] or k[1].startswith(it["path"] + "/"))]:
                flags[(dst["remote"], target + key[1][len(it["path"]):])] = flags.pop(key)
            flags[(dst["remote"], target)] = {"moved": True, "origins": [{"remote": it["remote"], "path": it["path"]}],
                                              "merged": existing is not None}
            ghosts.append({"remote": it["remote"], "parent": _parent(it["path"]), "name": _name(it["path"]),
                           "is_dir": isinstance(node, dict), "moved_to": {"remote": dst["remote"], "path": target}})
            if it["remote"] != dst["remote"]:
                if not any(w["code"] == "CROSS_REMOTE" for w in warnings):
                    warnings.append({"code": "CROSS_REMOTE", "message": "bytes are copied through this machine"})
            moved += 1
        if not moved:
            raise Stage("NOOP", "every item is already where it would be moved to")
        return warnings

    def _merge(self, into: dict, other: dict) -> None:
        for k, v in other.items():
            if k in into and isinstance(into[k], dict) and isinstance(v, dict):
                self._merge(into[k], v)
            else:
                into[k] = v

    def _commit(self, body: dict, created_by: str) -> tuple[dict, list]:
        tree, flags, ghosts = self.replay()
        warnings = self._apply(tree, flags, ghosts, body)  # raises Stage when the action is not valid
        self.draft["actions"] = [a for a in self.draft["actions"] if a["state"] == "active"]
        act = {"id": self.next_action_id, "seq": len(self.draft["actions"]) + 1, "kind": body["kind"],
               "items": body.get("items", []), "dst": body["dst"], "new_name": body.get("new_name"),
               "note": body.get("note"), "created_by": created_by, "state": "active", "created_at": "2024-03-01T10:00:00Z"}
        self.next_action_id += 1
        self.draft["actions"].append(act)
        self.draft["version"] += 1
        return act, warnings

    def _draft_json(self) -> dict:
        d = copy.deepcopy(self.draft)
        d["undo_available"] = any(a["state"] == "active" for a in d["actions"])
        d["redo_available"] = any(a["state"] == "undone" for a in d["actions"])
        self.replay()
        m = self.moved
        d["summary"] = {"moved_files": m["files"], "moved_bytes": m["bytes"], "cross_remote_bytes": m["cross"],
                        "remotes_touched": sorted(m["remotes"])}
        return d

    def _item(self, remote: str, parent: str, name: str, node: Any, flags: dict) -> dict:
        path = _join(parent, name)
        is_dir = isinstance(node, dict)
        files, dirs, size = _stats(node) if is_dir else (1, 0, node)
        fl = flags.get((remote, path), {})
        return {"name": name, "path": path, "remote": remote, "is_dir": is_dir, "size": None if is_dir else node,
                "mtime": "2024-03-01T10:00:00Z", "category": "other" if is_dir else CATEGORY.get(name.rpartition(".")[2], "other"),
                "files": files, "dirs": dirs, "bytes": size, "dup_files": int(name == "report.pdf"),
                "entry_id": None if is_dir else abs(hash((remote, path))) % 100000,
                "origins": fl.get("origins", []), "moved_here": fl.get("moved", False), "merged": fl.get("merged", False),
                "conflict": False, "dedupe": False, "virtual": fl.get("virtual", False)}

    def list_dir(self, remote: str, path: str, view: str, ghosts: bool) -> dict:
        if view == "before":
            tree, flags, gl = copy.deepcopy(self.fs), {}, []
        else:
            tree, flags, gl = self.replay()
        node = _get(tree, remote, path)
        items = []
        if isinstance(node, dict):
            names = sorted(node, key=lambda n: (not isinstance(node[n], dict), n.lower()))
            items = [self._item(remote, path, n, node[n], flags) for n in names]
        shown = []
        if ghosts and view == "after":
            for g in gl:
                if g["remote"] == remote and g["parent"] == path and not (isinstance(node, dict) and g["name"] in node):
                    shown.append({"name": g["name"], "path": _join(path, g["name"]), "is_dir": g["is_dir"], "moved_to": g["moved_to"]})
        return {"remote": remote, "path": path, "view": view, "draft_version": self.draft["version"],
                "items": items, "ghosts": shown, "total": len(items)}

    def roots(self) -> list[dict]:
        tree, flags, _ = self.replay()
        out = []
        for r in REMOTES:
            files, dirs, size = _stats(tree[r["name"]])
            out.append({"name": r["name"], "path": "", "remote": r["name"], "is_dir": True, "size": None, "mtime": None,
                        "category": "other", "files": files, "dirs": dirs, "bytes": size, "dup_files": 0, "entry_id": None,
                        "origins": [], "moved_here": False, "merged": False, "conflict": False, "dedupe": False,
                        "virtual": False, "remote_id": r["id"], "provider": r["provider"], "writable": r["writable"],
                        "case_insensitive": r["case_insensitive"]})
        return out

    # ------------------------------------------------------------------ preview payloads
    def preview(self) -> dict:
        d = self._draft_json()
        if not d["actions"] or not any(a["state"] == "active" for a in d["actions"]):
            return {"plan_id": None, "message": "nothing to do"}
        s = d["summary"]
        remotes = [
            {"remote_id": 1, "remote": "gdrive", "files_before": 6, "bytes_before": 6_603_000, "files_after": 4,
             "bytes_after": 6_480_000, "bytes_in": 0, "files_quarantined": 0, "bytes_quarantined": 0},
            {"remote_id": 2, "remote": "dropbox", "files_before": 3, "bytes_before": 9_074_100, "files_after": 5,
             "bytes_after": 9_197_100, "bytes_in": 123_000, "files_quarantined": 0, "bytes_quarantined": 0},
        ]
        base = {
            "plan_id": 7, "draft_version": d["version"],
            "summary": {**{k: s[k] for k in ("moved_files", "moved_bytes", "cross_remote_bytes", "remotes_touched")},
                        "dedupes": 1, "conflicts": 0, "ops": {"move": 0, "copy": 2, "delete": 2}, "roots": 1},
            "changes": [{"from": {"remote": "gdrive", "path": "研究/資料"}, "to": {"remote": "dropbox", "path": "學校/資料"},
                         "files": 2, "bytes": 123_000, "kind": "copy+delete", "is_dir": True}],
            "conflicts": [], "empty_dirs_left": 1,
            "simulation": {"plan_id": 7, "ok": True, "violations": [], "warnings": [], "remotes": remotes,
                           "contents_before": 9, "contents_after": 9, "contents_lost": 0,
                           "bytes_cross_remote": s["cross_remote_bytes"], "op_counts": {"copy": 2, "delete": 2}, "sample": []},
        }
        if self.preview_mode == "violations":
            base["ok"] = False
            base["simulation"]["ok"] = False
            base["simulation"]["violations"] = [
                {"code": "DST_EXISTS", "message": "'dropbox:學校/report.pdf' already holds different content", "seq": 1},
                {"code": "CASE_COLLISION", "message": "'dropbox:學校/Report.PDF' differs only by case", "seq": 2},
            ]
            base["simulation"]["warnings"] = [{"code": "NOOP", "message": "move of 'gdrive:a' onto itself", "seq": 3}]
        else:
            base["ok"] = True
            if self.approvals_enabled:
                base["approval_code"] = self.CODE
        return base

    # ------------------------------------------------------------------ HTTP
    def handle(self, route: Any, request: Any) -> None:
        url = urlparse(request.url)
        query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        body = json.loads(request.post_data) if request.post_data else None
        self.calls.append(Call(request.method, url.path, query, body, dict(request.headers)))
        status, payload = self.dispatch(request.method, url.path, query, body, request.headers)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    def dispatch(self, method: str, path: str, q: dict, body: Any, headers: dict) -> tuple[int, Any]:
        d = self.draft
        if (method, path) == ("POST", "/v1/ui/session"):
            if (body or {}).get("token") != self.TOKEN:
                return 401, {"error": "bad token"}
            self.session_open = True
            return 200, {"csrf": self.CSRF, "approvals_enabled": self.approvals_enabled}
        if (method, path) == ("GET", "/v1/ui/config"):
            if not self.session_open:
                return 401, {"error": "missing or invalid credentials", "code": "UNAUTHENTICATED"}
            return 200, {"approvals_enabled": self.approvals_enabled, "execution_enabled": self.execution_enabled,
                         "remotes": REMOTES, "version": "0.1.0", "csrf": self.CSRF}
        if (method, path) == ("GET", "/v1/tree/roots"):
            return 200, {"roots": self.roots()}
        if (method, path) == ("GET", "/v1/tree"):
            return 200, self.list_dir(q["remote"], q.get("path", ""), q.get("view", "after"), q.get("ghosts") == "1")
        if (method, path) == ("GET", "/v1/drafts"):
            return 200, {"drafts": [{"id": d["id"], "title": d["title"], "status": d["status"], "version": d["version"],
                                     "action_count": len([a for a in d["actions"] if a["state"] == "active"])}]
                         if d["status"] == "open" else []}
        if (method, path) == ("POST", "/v1/drafts"):
            self.draft = self._new_draft((body or {}).get("title") or "Untitled")
            return 201, self._draft_json()
        m = re.fullmatch(r"/v1/drafts/(\d+)(/.*)?", path)
        if m:
            rest = m.group(2) or ""
            if int(m.group(1)) != d["id"]:
                return 404, {"error": "no such draft", "code": "NOT_FOUND"}
            if method == "GET" and rest == "":
                return 200, self._draft_json()
            if method == "DELETE" and rest == "":
                d["status"] = "discarded"
                return 200, {"ok": True}
            if d["status"] != "open":
                return 409, {"error": "draft is closed", "code": "NOT_OPEN"}
            if method == "POST" and rest == "/actions":
                try:
                    act, warnings = self._commit(body, "ui")
                except Stage as e:
                    return 409, {"error": str(e), "code": e.code}
                return 201, {"action": act, "draft_version": d["version"], "warnings": warnings}
            if method == "POST" and rest in ("/undo", "/redo"):
                undo = rest == "/undo"
                pool = [a for a in d["actions"] if a["state"] == ("active" if undo else "undone")]
                if not pool:
                    return 409, {"error": "nothing", "code": "NOTHING_TO_UNDO" if undo else "NOTHING_TO_REDO"}
                target = pool[-1] if undo else pool[0]
                target["state"] = "undone" if undo else "active"
                d["version"] += 1
                return 200, self._draft_json()
            m2 = re.fullmatch(r"/actions/(\d+)", rest)
            if method == "DELETE" and m2:
                for a in d["actions"]:
                    if a["id"] == int(m2.group(1)):
                        a["state"] = "undone"
                d["version"] += 1
                return 200, {"draft_version": d["version"], "removed": [int(m2.group(1))]}
            if method == "POST" and rest == "/preview":
                return 200, self.preview()
        if (method, path) == ("GET", "/v1/search"):
            needle = q.get("q", "").lower()
            hits = []
            for r, tree in self.fs.items():
                if q.get("remote") and q["remote"] != r:
                    continue
                self._walk(r, "", tree, needle, hits)
            return 200, {"results": hits[: int(q.get("limit", 20))]}
        if (method, path) == ("GET", "/v1/suggestions"):
            return 200, {"suggestions": self.suggestions(q.get("kind", "related"))}
        m = re.fullmatch(r"/v1/ui/plans/(\d+)/(approve|execute)", path)
        if m and method == "POST":
            if headers.get("x-csrf-token") != self.CSRF:
                return 403, {"error": "bad csrf token"}
            if m.group(2) == "approve":
                if (body or {}).get("code") != self.CODE:
                    return 403, {"error": "the code does not match"}
                return 200, {"ok": True}
            self.job_polls = 0
            return 202, {"job_id": "job-1"}
        if (method, path) == ("POST", "/v1/ui/rescan"):
            return 202, {"job_id": "job-2"}
        m = re.fullmatch(r"/v1/jobs/(job-\d)", path)
        if m:
            if m.group(1) == "job-2":
                return 200, {"id": "job-2", "kind": "rescan", "status": "done", "progress": {}, "result": {}, "error": None}
            self.job_polls += 1
            done = self.job_polls >= 3
            if done:
                self.draft["status"] = "applied"
            return 200, {"id": "job-1", "kind": "execute", "status": "done" if done else "running", "progress": {},
                         "result": {"status": "executed"} if done else None, "error": None}
        if re.fullmatch(r"/v1/plans/\d+/progress", path):
            n = min(4, self.job_polls * 2)
            return 200, {"status": "executed" if n == 4 else "running", "ops_total": 4, "done": n, "failed": 0, "skipped": 0,
                         "current": None if n == 4 else {"op": "copy", "src": {"remote": "gdrive", "path": "研究/資料/survey.csv"},
                                                         "dst": {"remote": "dropbox", "path": "學校/資料/survey.csv"}},
                         "recent_journal": []}
        return 404, {"error": f"unmocked {method} {path}"}

    def _walk(self, remote: str, base: str, node: dict, needle: str, hits: list) -> None:
        for name, child in node.items():
            path = _join(base, name)
            if needle in name.lower():
                hits.append({"id": len(hits) + 1, "remote": remote, "path": path, "name": name, "is_dir": isinstance(child, dict),
                             "size": None if isinstance(child, dict) else child,
                             "category": "other" if isinstance(child, dict) else CATEGORY.get(name.rpartition(".")[2], "other")})
            if isinstance(child, dict):
                self._walk(remote, path, child, needle, hits)

    def suggestions(self, kind: str) -> list[dict]:
        if kind == "duplicates":
            return [{"id": "duplicates:aaa111", "kind": "duplicates", "score": 0.95, "title": "這 2 個資料夾內容幾乎一樣：學校、學校",
                     "reason": "內容相同度 95%（只計算已用雜湊證明相同的內容，共 1 份相同）。",
                     "folders": [{"remote": "gdrive", "path": "學校", "files": 3, "bytes": 1_032_000},
                                 {"remote": "dropbox", "path": "學校", "files": 2, "bytes": 74_100}],
                     "terms": ["學校"],
                     "actions": [{"kind": "move", "items": [{"remote": "dropbox", "path": "學校"}],
                                  "dst": {"remote": "gdrive", "path": ""}, "note": "duplicates:aaa111"}]}]
        return [{"id": "related:bbb222", "kind": "related", "score": 0.61, "title": "這 2 個資料夾看起來相關：論文、資料",
                 "reason": "資料夾名稱都有「研究」；其中有 1 份內容完全相同的檔案。建議整理到「gdrive:研究/論文」底下。",
                 "folders": [{"remote": "gdrive", "path": "研究/論文", "files": 2, "bytes": 2_448_000},
                             {"remote": "gdrive", "path": "研究/資料", "files": 2, "bytes": 123_000}],
                 "terms": ["研究", "survey"],
                 "actions": [{"kind": "move", "items": [{"remote": "gdrive", "path": "研究/資料"}],
                              "dst": {"remote": "gdrive", "path": "研究/論文"}, "note": "related:bbb222"}]}]


# --------------------------------------------------------------------------- page helpers

class Ui:
    def __init__(self, page: Any, mock: MockApi, base: str):
        self.page, self.mock, self.base = page, mock, base

    def open(self, token: str | None = MockApi.TOKEN) -> "Ui":
        self.page.goto(f"{self.base}/ui/" + (f"#token={token}" if token else ""))
        expect(self.page.get_by_test_id("pane-left")).to_be_visible()
        expect(self.node("left", "gdrive:")).to_be_visible()
        return self

    def pane(self, side: str):
        return self.page.locator(f'[data-testid="pane-{side}"]')

    def node(self, side: str, key: str):
        return self.pane(side).locator(f'[data-testid="node-{key}"]')

    def sel(self, side: str, key: str) -> str:
        return f'[data-testid="pane-{side}"] [data-testid="node-{key}"]'

    def expand(self, side: str, key: str) -> None:
        self.pane(side).locator(f'[data-testid="toggle-{key}"]').click()

    def settle(self) -> None:
        """No tree is still loading. A layout change while the mouse is down makes Chromium swallow the drag start
        (Playwright decides within one timer tick whether `dragstart` happened), so drag only into a quiet page."""
        expect(self.page.locator(".tree .row-msg").filter(has_text="載入中")).to_have_count(0)

    def drag(self, src: tuple[str, str], dst: tuple[str, str]) -> None:
        self.settle()
        self.page.drag_and_drop(self.sel(*src), self.sel(*dst))

    def expand_all(self) -> None:
        """Left: gdrive > 研究 > (論文, 資料), 學校. Right: dropbox > 學校."""
        for key in ("gdrive:", "gdrive:研究", "gdrive:學校"):
            self.expand("left", key)
        for key in ("dropbox:", "dropbox:學校"):
            self.expand("right", key)
        expect(self.node("left", "gdrive:研究/資料")).to_be_visible()
        expect(self.node("right", "dropbox:學校/report.pdf")).to_be_visible()


@pytest.fixture
def make_ui(browser, site):
    contexts: list = []
    errors: list[str] = []

    def factory(*, approvals_enabled: bool = True, execution_enabled: bool = True, width: int = 1440, height: int = 900,
                color_scheme: str = "light") -> Ui:
        ctx = browser.new_context(viewport={"width": width, "height": height}, locale="zh-TW", color_scheme=color_scheme)
        contexts.append(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None)
        mock = MockApi(approvals_enabled=approvals_enabled, execution_enabled=execution_enabled)
        page.route("**/v1/**", mock.handle)
        return Ui(page, mock, site)

    yield factory
    for c in contexts:
        c.close()
    assert not errors, f"uncaught JavaScript errors: {errors}"


# --------------------------------------------------------------------------- tests

def test_login_exchange_and_token_removed_from_url(make_ui):
    ui = make_ui().open()
    session = ui.mock.last("POST", "/v1/ui/session")
    assert session.body == {"token": MockApi.TOKEN}
    assert "token" not in ui.page.url and ui.page.evaluate("location.hash") == ""
    assert ui.mock.calls_to("GET", "/v1/ui/config")
    assert ui.mock.calls_to("GET", "/v1/drafts/1") and not ui.mock.calls_to("POST", "/v1/drafts")  # resumes the open draft
    expect(ui.page.get_by_test_id("approvals-banner")).to_have_count(0)


def test_a_reload_without_the_token_keeps_the_session_and_recovers_the_csrf_token(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.goto(f"{ui.base}/ui/")  # the one-time token is gone from the URL; the cookie (mock: session_open) remains
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("1")
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    modal.get_by_test_id("approval-input").fill(MockApi.CODE)
    modal.get_by_test_id("approval-button").click()
    expect(modal.get_by_test_id("progress")).to_be_visible()
    assert ui.mock.last("POST", r"/v1/ui/plans/7/approve").headers["x-csrf-token"] == MockApi.CSRF


def test_without_a_session_the_page_says_how_to_sign_in(make_ui):
    ui = make_ui()
    ui.page.goto(f"{ui.base}/ui/")  # no token, no cookie
    expect(ui.page.get_by_test_id("fatal")).to_contain_text("drivemerge ui")


def test_a_first_visit_creates_a_draft(make_ui):
    ui = make_ui()
    ui.mock.draft["status"] = "discarded"  # no open draft exists yet
    ui.open()
    assert ui.mock.last("POST", "/v1/drafts").body["title"].startswith("整理")
    expect(ui.page.get_by_test_id("draft-select")).to_contain_text("#2")


def test_bad_token_shows_a_plain_message(make_ui):
    ui = make_ui()
    ui.page.goto(f"{ui.base}/ui/#token=WRONG")
    expect(ui.page.get_by_test_id("fatal")).to_contain_text("drivemerge ui")
    assert "WRONG" not in ui.page.url


def test_both_panes_render_roots_and_lazy_children(make_ui):
    ui = make_ui().open()
    for side in ("left", "right"):
        expect(ui.node(side, "gdrive:")).to_contain_text("gdrive")
        expect(ui.node(side, "dropbox:")).to_be_visible()
    assert not [c for c in ui.mock.calls_to("GET", "/v1/tree")], "children must not load before a node is expanded"

    ui.expand("left", "gdrive:")
    expect(ui.node("left", "gdrive:研究")).to_be_visible()
    expect(ui.node("left", "gdrive:Photos")).to_be_visible()
    tree_calls = ui.mock.calls_to("GET", "/v1/tree")
    assert [c.query["remote"] for c in tree_calls] == ["gdrive"]
    assert tree_calls[0].query["view"] == "after" and tree_calls[0].query["draft"] == "1"
    expect(ui.node("right", "gdrive:研究")).to_have_count(0)  # the other pane is independent

    ui.expand("left", "gdrive:研究")
    ui.expand("left", "gdrive:學校")
    names = ui.pane("left").locator('[role="treeitem"] .name').all_inner_texts()
    assert names.index("課程") < names.index("report.pdf")  # folders come first
    assert names.index("論文") < names.index("資料")
    # Counts and sizes are shown.
    expect(ui.node("left", "gdrive:研究")).to_contain_text("4 個檔案")
    ui.expand("left", "gdrive:研究/論文")
    expect(ui.node("left", "gdrive:研究/論文/thesis.pdf")).to_contain_text("2.3 MB")
    expect(ui.pane("left").get_by_role("tree")).to_be_visible()
    assert ui.node("left", "gdrive:研究/論文").get_attribute("aria-level") == "3"  # cloud > 研究 > 論文


def test_drag_folder_to_another_level_in_the_other_pane_sends_exact_post(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))

    post = ui.mock.last("POST", "/v1/drafts/1/actions")
    assert post.body == {"kind": "move", "items": [{"remote": "gdrive", "path": "研究/資料"}],
                         "dst": {"remote": "dropbox", "path": "學校"}}
    # Cross-cloud moves warn, and both panes refresh while keeping what was expanded.
    expect(ui.page.get_by_test_id("toast").first).to_contain_text("跨雲")
    expect(ui.node("right", "dropbox:學校/資料")).to_be_visible()
    expect(ui.node("left", "gdrive:研究/資料")).to_have_count(0)
    expect(ui.node("left", "gdrive:研究")).to_have_attribute("aria-expanded", "true")
    expect(ui.node("right", "dropbox:學校/資料")).to_have_attribute("data-kind", "dir")
    expect(ui.node("right", "dropbox:學校/資料").locator('[data-badge="moved"]')).to_be_visible()
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("1")
    expect(ui.page.get_by_test_id("undo-button")).to_be_enabled()


def test_drag_onto_a_root_node_and_between_levels_of_the_same_pane(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    # Right pane shows the gdrive root too: drop a deep folder on the root node of the other cloud.
    ui.expand("right", "gdrive:")
    expect(ui.node("right", "gdrive:Photos")).to_be_visible()
    ui.drag(("left", "gdrive:學校/課程"), ("right", "gdrive:"))
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body == {
        "kind": "move", "items": [{"remote": "gdrive", "path": "學校/課程"}], "dst": {"remote": "gdrive", "path": ""}}
    expect(ui.node("left", "gdrive:課程")).to_be_visible()
    # Same pane, folder to a deeper level.
    ui.drag(("left", "gdrive:課程"), ("left", "gdrive:研究"))
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body["dst"] == {"remote": "gdrive", "path": "研究"}
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("2")


def test_multi_selection_drag_sends_every_item(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.node("left", "gdrive:研究/論文").click()
    ui.node("left", "gdrive:研究/資料").click(modifiers=["Control"])
    expect(ui.pane("left").locator('[aria-selected="true"]')).to_have_count(2)
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body["items"] == [
        {"remote": "gdrive", "path": "研究/論文"}, {"remote": "gdrive", "path": "研究/資料"}]


def test_keyboard_navigation_follows_the_tree_pattern(make_ui):
    ui = make_ui().open()
    root = ui.node("left", "gdrive:")
    root.focus()
    assert root.get_attribute("aria-expanded") == "false"
    ui.page.keyboard.press("ArrowRight")  # expand
    expect(root).to_have_attribute("aria-expanded", "true")
    expect(ui.node("left", "gdrive:Photos")).to_be_visible()  # children load lazily
    ui.page.keyboard.press("ArrowDown")  # first child (folders sort first: Photos < 學校 < 研究)
    expect(ui.node("left", "gdrive:Photos")).to_have_attribute("aria-selected", "true")
    expect(ui.node("left", "gdrive:Photos")).to_be_focused()
    ui.page.keyboard.press("Shift+ArrowDown")
    expect(ui.pane("left").locator('[aria-selected="true"]')).to_have_count(2)
    ui.page.keyboard.press("ArrowLeft")  # a closed folder: go to its parent
    expect(root).to_be_focused()
    ui.page.keyboard.press("ArrowLeft")  # an open folder: close it
    expect(root).to_have_attribute("aria-expanded", "false")
    ui.page.keyboard.press("Enter")  # Enter toggles
    expect(root).to_have_attribute("aria-expanded", "true")


def test_cut_and_paste_is_a_keyboard_way_to_move(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.node("left", "gdrive:研究/資料").click()
    ui.page.keyboard.press("Control+x")
    expect(ui.page.get_by_test_id("toast").first).to_contain_text("已剪下 1 項")
    ui.node("right", "dropbox:學校").click()
    ui.page.keyboard.press("Control+v")
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body == {
        "kind": "move", "items": [{"remote": "gdrive", "path": "研究/資料"}], "dst": {"remote": "dropbox", "path": "學校"}}
    expect(ui.node("right", "dropbox:學校/資料")).to_be_visible()


def test_the_splitter_resizes_the_panes_from_the_keyboard(make_ui):
    ui = make_ui().open()
    bar = ui.page.get_by_test_id("splitter")
    assert bar.get_attribute("aria-valuenow") == "50"
    before = ui.pane("left").bounding_box()["width"]
    bar.focus()
    ui.page.keyboard.press("ArrowRight")
    ui.page.keyboard.press("ArrowRight")
    assert int(bar.get_attribute("aria-valuenow")) > 50
    assert ui.pane("left").bounding_box()["width"] > before
    box = bar.bounding_box()
    ui.page.mouse.move(box["x"] + 5, box["y"] + 100)
    ui.page.mouse.down()
    ui.page.mouse.move(300, box["y"] + 100, steps=4)
    ui.page.mouse.up()
    assert int(bar.get_attribute("aria-valuenow")) < 50


def test_dropping_a_folder_into_itself_is_not_offered(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.expand("left", "gdrive:研究/論文")
    ui.drag(("left", "gdrive:研究"), ("left", "gdrive:研究/論文"))
    assert not ui.mock.calls_to("POST", "/v1/drafts/1/actions")


def test_name_clash_offers_keep_both(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:學校/report.pdf"), ("right", "dropbox:學校"))
    toast = ui.page.get_by_test_id("toast").first
    expect(toast).to_contain_text("同名")
    toast.get_by_test_id("toast-action").click()
    post = ui.mock.last("POST", "/v1/drafts/1/actions")
    assert post.body == {"kind": "move", "items": [{"remote": "gdrive", "path": "學校/report.pdf"}],
                         "dst": {"remote": "dropbox", "path": "學校"}, "new_name": "report (2).pdf"}
    expect(ui.node("right", "dropbox:學校/report (2).pdf")).to_be_visible()


def test_rename_with_f2_and_double_click(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.node("left", "gdrive:研究/資料").click()
    ui.page.keyboard.press("F2")
    box = ui.page.get_by_test_id("rename-input")
    expect(box).to_be_focused()
    box.fill("調查資料")
    box.press("Enter")
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body == {
        "kind": "move", "items": [{"remote": "gdrive", "path": "研究/資料"}],
        "dst": {"remote": "gdrive", "path": "研究"}, "new_name": "調查資料"}
    expect(ui.node("left", "gdrive:研究/調查資料")).to_be_visible()
    ui.node("left", "gdrive:研究/調查資料").locator(".name").dblclick()
    expect(ui.page.get_by_test_id("rename-input")).to_be_visible()
    ui.page.keyboard.press("Escape")
    expect(ui.page.get_by_test_id("rename-input")).to_have_count(0)


def test_new_folder_stages_a_mkdir(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.node("right", "dropbox:學校").click()
    ui.page.get_by_test_id("mkdir-right").click()
    ui.page.get_by_test_id("dialog-input").fill("碩士論文")
    ui.page.get_by_test_id("dialog-ok").click()
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body == {
        "kind": "mkdir", "dst": {"remote": "dropbox", "path": "學校"}, "new_name": "碩士論文"}
    new = ui.node("right", "dropbox:學校/碩士論文")
    expect(new).to_be_visible()
    expect(new.locator('[data-badge="virtual"]')).to_be_visible()


def test_undo_redo_buttons_and_keyboard_shortcuts(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    undo, redo = ui.page.get_by_test_id("undo-button"), ui.page.get_by_test_id("redo-button")
    expect(undo).to_be_disabled()
    expect(redo).to_be_disabled()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(undo).to_be_enabled()

    undo.click()
    expect(ui.node("left", "gdrive:研究/資料")).to_be_visible()
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/undo")) == 1
    expect(redo).to_be_enabled()
    redo.click()
    expect(ui.node("left", "gdrive:研究/資料")).to_have_count(0)
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/redo")) == 1

    ui.page.locator("body").click(position={"x": 5, "y": 5})
    ui.page.keyboard.press("Control+z")
    expect(ui.node("left", "gdrive:研究/資料")).to_be_visible()
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/undo")) == 2
    ui.page.keyboard.press("Control+Shift+z")
    expect(ui.node("left", "gdrive:研究/資料")).to_have_count(0)
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/redo")) == 2


def test_view_toggle_and_ghosts(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(ui.node("right", "dropbox:學校/資料")).to_be_visible()

    ui.page.get_by_test_id("ghost-toggle-left").check()
    ghost = ui.pane("left").get_by_test_id("ghost-gdrive:研究/資料")
    expect(ghost).to_contain_text("→ dropbox:學校/資料")
    assert any(c.query.get("ghosts") == "1" for c in ui.mock.calls_to("GET", "/v1/tree"))

    ui.page.get_by_test_id("view-before-left").click()
    expect(ui.node("left", "gdrive:研究/資料")).to_be_visible()
    expect(ui.page.get_by_test_id("before-note-left")).to_be_visible()
    assert ui.mock.calls_to("GET", "/v1/tree")[-1].query["view"] == "before"
    ui.drag(("left", "gdrive:研究/論文"), ("right", "dropbox:Archive"))  # read-only view: nothing is staged
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/actions")) == 1


def test_search_reveals_and_selects_the_result(make_ui):
    ui = make_ui().open()
    ui.page.get_by_test_id("search-left").fill("survey")
    hit = ui.page.get_by_test_id("search-result-gdrive:研究/資料/survey.csv")
    expect(hit).to_be_visible()
    q = ui.mock.last("GET", "/v1/search").query
    assert q["q"] == "survey" and q["include_dirs"] == "true"
    hit.click()
    row = ui.node("left", "gdrive:研究/資料/survey.csv")
    expect(row).to_be_visible()
    expect(row).to_have_attribute("aria-selected", "true")
    expect(ui.node("left", "gdrive:研究")).to_have_attribute("aria-expanded", "true")
    expect(ui.page.get_by_test_id("crumbs-left")).to_contain_text("survey.csv")


def test_staged_drawer_lists_actions_and_removes_one(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("staged-toggle").click()
    item = ui.page.get_by_test_id("staged-list").get_by_test_id("staged-item")
    expect(item).to_have_count(1)
    expect(item).to_contain_text("移動 1 項 → dropbox:學校")
    expect(item).to_contain_text("資料")
    item.get_by_test_id("staged-remove").click()
    expect(ui.page.get_by_test_id("staged-item")).to_have_count(0)
    assert ui.mock.last("DELETE", r"/v1/drafts/1/actions/\d+")
    expect(ui.node("left", "gdrive:研究/資料")).to_be_visible()


def test_clear_discards_the_draft_after_confirmation(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("staged-toggle").click()
    ui.page.get_by_test_id("staged-clear").click()
    ui.page.get_by_test_id("dialog-cancel").click()
    assert not ui.mock.calls_to("DELETE", "/v1/drafts/1")
    ui.page.get_by_test_id("staged-clear").click()
    ui.page.get_by_test_id("dialog-ok").click()
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("0")
    assert ui.mock.calls_to("DELETE", "/v1/drafts/1")
    assert len(ui.mock.calls_to("POST", "/v1/drafts")) == 1  # the replacement; the first draft was already there
    expect(ui.node("left", "gdrive:研究/資料")).to_be_visible()


def test_suggestions_reveal_and_stage(make_ui):
    ui = make_ui().open()
    ui.page.get_by_test_id("suggestions-toggle").click()
    card = ui.page.get_by_test_id("suggestion-card")
    expect(card).to_have_count(1)
    expect(card).to_contain_text("gdrive:研究/論文")
    expect(card).to_contain_text("研究")
    assert ui.mock.last("GET", "/v1/suggestions").query["kind"] == "related"

    card.get_by_test_id("suggestion-show-left").click()
    expect(ui.node("left", "gdrive:研究/論文")).to_have_attribute("aria-selected", "true")
    expect(ui.node("right", "gdrive:研究/資料")).to_have_attribute("aria-selected", "true")

    card.get_by_test_id("suggestion-add").click()
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body == {
        "kind": "move", "items": [{"remote": "gdrive", "path": "研究/資料"}],
        "dst": {"remote": "gdrive", "path": "研究/論文"}, "note": "related:bbb222"}
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("1")
    expect(card.get_by_test_id("suggestion-add")).to_be_disabled()

    ui.page.get_by_test_id("sug-tab-duplicates").click()
    expect(ui.page.get_by_test_id("suggestion-card")).to_contain_text("95%")
    assert ui.mock.last("GET", "/v1/suggestions").query["kind"] == "duplicates"


def test_changes_from_claude_show_up_with_a_toast(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.mock.external_action(created_by="mcp", items=[{"remote": "gdrive", "path": "研究/資料"}],
                            dst={"remote": "gdrive", "path": "研究/論文"})
    toast = ui.page.get_by_test_id("toast").filter(has_text="已有新的暫存變更（由 Claude 提出）")
    expect(toast).to_be_visible(timeout=8000)
    expect(ui.node("left", "gdrive:研究/資料")).to_have_count(0)  # moved into 論文 by "Claude"
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("1")
    ui.page.get_by_test_id("staged-toggle").click()
    expect(ui.page.get_by_test_id("claude-badge")).to_be_visible()


def test_preview_with_violations_explains_them_and_offers_no_approval(make_ui):
    ui = make_ui().open()
    ui.mock.preview_mode = "violations"
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal).to_be_visible()
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "false")
    expect(modal.get_by_test_id("sim-status")).to_contain_text("2 個問題")
    violations = modal.get_by_test_id("violation")
    expect(violations).to_have_count(2)
    expect(violations.first).to_have_attribute("data-code", "DST_EXISTS")
    expect(violations.first).to_contain_text("絕不覆寫")
    expect(violations.nth(1)).to_contain_text("不分大小寫")
    expect(modal.get_by_test_id("warning-list")).to_be_visible()
    for card in ("files", "bytes", "cross", "dedupes", "conflicts", "empty"):
        expect(modal.get_by_test_id(f"summary-{card}")).to_be_visible()
    expect(modal.get_by_test_id("cloud-table")).to_contain_text("dropbox")
    expect(modal.get_by_test_id("change-list")).to_contain_text("gdrive:研究/資料")
    expect(modal.get_by_test_id("approval-input")).to_have_count(0)
    expect(modal.get_by_test_id("approval-button")).to_have_count(0)
    # The reveal link jumps to the place named in the message and closes the modal.
    violations.first.get_by_test_id("reveal-link").click()
    expect(modal).to_have_count(0)
    expect(ui.node("right", "dropbox:學校/report.pdf")).to_have_attribute("aria-selected", "true")


def test_approval_button_stays_disabled_until_the_code_is_typed_then_runs_and_rescans(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")
    expect(modal.get_by_test_id("approval-code")).to_have_text(MockApi.CODE)
    button, field = modal.get_by_test_id("approval-button"), modal.get_by_test_id("approval-input")
    expect(button).to_have_text(re.compile("核准並執行"))
    expect(button).to_be_disabled()
    field.fill("a1b2c3d")
    expect(button).to_be_disabled()
    field.fill("zzzzzzzz")
    expect(button).to_be_disabled()
    field.fill(MockApi.CODE)
    expect(button).to_be_enabled()
    assert not ui.mock.calls_to("POST", r"/v1/ui/plans/7/approve")

    button.click()
    expect(modal.get_by_test_id("progress")).to_be_visible()
    approve = ui.mock.last("POST", r"/v1/ui/plans/7/approve")
    assert approve.body == {"code": MockApi.CODE}
    assert approve.headers["x-csrf-token"] == MockApi.CSRF
    assert ui.mock.last("POST", r"/v1/ui/plans/7/execute").headers["x-csrf-token"] == MockApi.CSRF
    expect(modal.get_by_test_id("progress-bar")).to_have_attribute("role", "progressbar")
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", "done", timeout=10_000)
    expect(modal.get_by_test_id("progress-bar")).to_have_attribute("aria-valuenow", "100")
    assert ui.mock.calls_to("GET", "/v1/jobs/job-1") and ui.mock.calls_to("GET", r"/v1/plans/7/progress")

    modal.get_by_test_id("rescan-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", "rescanned", timeout=10_000)
    assert ui.mock.last("POST", "/v1/ui/rescan").body == {"remotes": ["dropbox", "gdrive"]}
    modal.get_by_test_id("preview-close").click()
    expect(modal).to_have_count(0)
    # The applied draft is replaced by a fresh one.
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("0")
    assert len(ui.mock.calls_to("POST", "/v1/drafts")) == 1


def test_stale_preview_cannot_be_approved(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    modal.get_by_test_id("approval-input").fill(MockApi.CODE)
    expect(modal.get_by_test_id("approval-button")).to_be_enabled()
    ui.mock.external_action(created_by="mcp", items=[{"remote": "gdrive", "path": "研究/論文"}],
                            dst={"remote": "dropbox", "path": "Archive"})
    expect(modal.get_by_test_id("stale-banner")).to_be_visible(timeout=8000)
    expect(modal.get_by_test_id("approval-button")).to_be_disabled()


def test_approvals_disabled_shows_banner_and_cli_commands(make_ui):
    ui = make_ui(approvals_enabled=False).open()
    banner = ui.page.get_by_test_id("approvals-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("drivemerge plan approve")
    expect(banner).to_contain_text("drivemerge plan execute")
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("cli-approve")).to_have_text("drivemerge plan approve 7")
    expect(modal.get_by_test_id("cli-execute")).to_have_text("drivemerge plan execute 7")
    expect(modal.get_by_test_id("approval-button")).to_have_count(0)
    expect(banner).to_be_visible()  # persistent


def test_execution_disabled_explains_how_to_enable_it(make_ui):
    ui = make_ui(execution_enabled=False).open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("execution-disabled")).to_contain_text("execution_enabled = true")
    expect(modal.get_by_test_id("approval-button")).to_have_count(0)


def test_nothing_to_do_after_dragging_back(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("undo-button").click()
    expect(ui.page.get_by_test_id("preview-button")).to_be_disabled()


def test_english_fallback_and_language_switch(make_ui):
    ui = make_ui().open()
    expect(ui.page.get_by_test_id("preview-button")).to_contain_text("預覽")
    ui.page.get_by_test_id("lang-toggle").click()
    expect(ui.page.get_by_test_id("preview-button")).to_contain_text("Preview")
    expect(ui.page.get_by_test_id("undo-button")).to_contain_text("Undo")
    ui.expand("left", "gdrive:")
    expect(ui.node("left", "gdrive:Photos")).to_contain_text("1 file")
    expect(ui.node("left", "gdrive:Photos")).not_to_contain_text("1 files")
    expect(ui.node("left", "gdrive:研究")).to_contain_text("4 files")


def test_every_translation_key_exists_in_both_languages(make_ui):
    ui = make_ui()
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    dicts = ui.page.evaluate("import('/ui/static/i18n.js').then(m => m.dictionaries)")
    zh, en = set(dicts["zh-TW"]), set(dicts["en"])
    assert zh == en, f"differences: {sorted(zh ^ en)}"
    used: set[str] = set()
    for f in WEB.glob("*.js"):
        if f.name != "i18n.js":
            used |= set(re.findall(r"""\bt\(\s*['"]([A-Za-z0-9_.+]+)['"]""", f.read_text(encoding="utf-8")))
    assert not used - zh, f"missing translations: {sorted(used - zh)}"
    codes = ("SRC_MISSING DST_EXISTS CASE_COLLISION AMBIGUOUS_PATH NO_WITNESS WITNESS_MISMATCH CONTENT_LOST NATIVE_DOC "
             "READ_ONLY_REMOTE CROSS_REMOTE_MOVE BAD_PATH DEPENDENCY QUOTA EXPECT_MISMATCH NOOP PROBABLE_ONLY").split()
    for code in codes:
        assert f"viol.{code}.title" in zh and f"viol.{code}.text" in zh, code
    for code in ("MERGE", "CROSS_REMOTE", "READ_ONLY", "CASE_COLLISION"):
        assert f"warn.{code}" in zh, code
    for code in ("SRC_NOT_FOUND DST_NOT_DIR INTO_ITSELF NAME_EXISTS NOOP BAD_NAME").split():
        assert f"err.{code}" in zh, code


def test_screenshots_for_a_visual_check(make_ui):
    """Writes PNGs of the main screen and the preview modal (light and dark) so a human can eyeball the layout."""
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
    except OSError:
        pytest.skip("screenshot directory is not writable")
    ui = make_ui().open()
    ui.expand_all()
    ui.expand("left", "gdrive:研究/論文")
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(ui.node("right", "dropbox:學校/資料")).to_be_visible()
    ui.node("right", "dropbox:學校").click()
    ui.page.get_by_test_id("mkdir-right").click()
    ui.page.get_by_test_id("dialog-input").fill("碩士論文")
    ui.page.get_by_test_id("dialog-ok").click()
    ui.page.get_by_test_id("ghost-toggle-left").check()
    ui.page.get_by_test_id("staged-toggle").click()
    expect(ui.page.get_by_test_id("staged-item")).to_have_count(2)
    ui.page.wait_for_timeout(1800)
    ui.page.screenshot(path=str(SHOTS / "main.png"))

    ui.page.get_by_test_id("suggestions-toggle").click()
    expect(ui.page.get_by_test_id("suggestion-card")).to_be_visible()
    ui.page.screenshot(path=str(SHOTS / "suggestions.png"))

    ui.page.get_by_test_id("preview-button").click()
    expect(ui.page.get_by_test_id("approval-input")).to_be_visible()
    ui.page.get_by_test_id("approval-input").fill("a1b2")
    ui.page.screenshot(path=str(SHOTS / "preview.png"))

    ui.page.get_by_test_id("preview-close").click()
    ui.mock.preview_mode = "violations"
    ui.page.get_by_test_id("preview-button").click()
    expect(ui.page.get_by_test_id("violation").first).to_be_visible()
    ui.page.screenshot(path=str(SHOTS / "preview-violations.png"))
    ui.page.get_by_test_id("preview-close").click()

    ui.mock.preview_mode = "ok"
    ui.page.get_by_test_id("preview-button").click()
    ui.page.get_by_test_id("approval-input").fill(MockApi.CODE)
    ui.page.get_by_test_id("approval-button").click()
    expect(ui.page.get_by_test_id("progress-current")).to_be_visible()
    ui.page.screenshot(path=str(SHOTS / "executing.png"))
    expect(ui.page.get_by_test_id("progress")).to_have_attribute("data-phase", "done", timeout=10_000)
    ui.page.screenshot(path=str(SHOTS / "done.png"))

    dark = make_ui(color_scheme="dark", width=1100, height=760).open()
    dark.expand_all()
    dark.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(dark.node("right", "dropbox:學校/資料")).to_be_visible()
    dark.page.wait_for_timeout(1800)
    dark.page.screenshot(path=str(SHOTS / "main-dark-1100.png"))
    assert (SHOTS / "main.png").stat().st_size > 5_000


# --------------------------------------------------------------------------- the real backend

@pytest.fixture
def real_server(tmp_path):
    """drivemerge.ui_api over a small file-backed catalog, served by uvicorn on a free port."""
    pytest.importorskip("uvicorn")
    ui_api = pytest.importorskip("drivemerge.ui_api")
    import socket
    import time

    import uvicorn
    from conftest import Catalog

    from drivemerge import db, dirstats, identity
    from drivemerge.config import Config, RemoteConfig, SafetyConfig
    from drivemerge.service import Service

    conn = db.connect(tmp_path / "catalog.db")
    cat = Catalog(conn)
    g, d = cat.remote("gdrive", "drive"), cat.remote("dropbox", "dropbox")
    cat.file(g, "研究/論文/thesis.pdf", size=2_400_000, md5="m1")
    cat.file(g, "研究/資料/survey.csv", size=120_000, md5="m2")
    cat.file(g, "研究/資料/notes.txt", size=3_000, md5="m3")
    cat.file(d, "學校/作業/hw1.docx", size=22_000, dropbox="d1")
    conn.commit()
    identity.rebuild_groups(conn)
    dirstats.refresh(conn)
    conn.commit()
    config = Config(db_path=str(tmp_path / "catalog.db"),
                    remotes=[RemoteConfig("gdrive", "drive", "gdrive:", "gdrive:"),
                             RemoteConfig("dropbox", "dropbox", "dropbox:", "dropbox:")],
                    safety=SafetyConfig(execution_enabled=True))
    app = ui_api.create_ui_app(Service(config, conn=conn), config=config, api_token="api-token", ui_token="real-token",
                               approvals_enabled=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    conn.close()


def test_the_page_works_against_the_real_backend(browser, real_server):
    """Everything above runs on canned JSON; this one checks the same flow against the real routes and shapes."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-TW")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None)
    try:
        page.goto(f"{real_server}/ui/#token=real-token")
        ui = Ui(page, None, real_server)  # type: ignore[arg-type]
        expect(ui.node("left", "gdrive:")).to_be_visible()
        assert "token" not in page.url
        for key in ("gdrive:", "gdrive:研究"):
            ui.expand("left", key)
        for key in ("dropbox:", "dropbox:學校"):
            ui.expand("right", key)
        expect(ui.node("left", "gdrive:研究/資料")).to_contain_text("2 個檔案")
        expect(ui.node("right", "dropbox:學校/作業")).to_be_visible()

        ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
        expect(ui.node("right", "dropbox:學校/資料")).to_be_visible()
        expect(ui.node("left", "gdrive:研究/資料")).to_have_count(0)
        expect(page.get_by_test_id("staged-count")).to_have_text("1")

        page.get_by_test_id("preview-button").click()
        modal = page.get_by_test_id("preview-modal")
        expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")
        expect(modal.get_by_test_id("change-list")).to_contain_text("gdrive:研究/資料")
        code = modal.get_by_test_id("approval-code").inner_text()
        assert re.fullmatch(r"[0-9a-f]{8}", code)
        expect(modal.get_by_test_id("approval-button")).to_be_disabled()
        modal.get_by_test_id("approval-input").fill(code)
        expect(modal.get_by_test_id("approval-button")).to_be_enabled()  # not clicked: that would start a real execution
        modal.get_by_test_id("preview-close").click()

        page.get_by_test_id("undo-button").click()
        expect(page.get_by_test_id("staged-count")).to_have_text("0")
        expect(ui.node("left", "gdrive:研究/資料")).to_be_visible()

        page.goto(f"{real_server}/ui/")  # a reload: the session cookie and the CSRF token from /v1/ui/config
        expect(page.get_by_test_id("pane-left")).to_be_visible()
        expect(page.get_by_test_id("fatal")).to_have_count(0)
    finally:
        ctx.close()
    assert not errors, errors
