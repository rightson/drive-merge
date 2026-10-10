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
import drivemerge.workspace  # noqa: E402,F401  (the conflict codes it can raise are checked against the translations)
from test_i18n_contract import dictionaries as contract_dictionaries  # noqa: E402
from test_i18n_contract import simulator_messages  # noqa: E402

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
    """A refusal as workspace.WorkspaceError makes it: a code, the English message and the facts (`params`)."""

    def __init__(self, code: str, message: str, **params: Any):
        super().__init__(message)
        self.code = code
        self.params = params


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
QUARANTINE = ".drive-merge-quarantine"
# The six codes build_ops can give a conflict, with the English sentence it puts in `reason`.
ENGINE_CONFLICTS = [
    ("DIFFERENT_CONTENT", "a different file already has that name"),
    ("DIR_IN_WAY", "a folder with that name is in the way"),
    ("FILE_IN_WAY", "a file is where this needs a folder"),
    ("OCCUPIED", "the spot is still occupied until the delete phase"),
    ("CYCLE", "moves wait on each other (a swap needs a temporary name)"),
    ("CASE_ONLY_RENAME", "this remote ignores case, so a rename that changes only the case cannot be carried out "
                         "file by file"),
]
# What preview() reports for moved folders that hold no files (workspace._empty_dirs_skipped).
EMPTY_FOLDERS = [
    {"from": {"remote": "gdrive", "path": "研究/空的"}, "to": {"remote": "dropbox", "path": "學校/空的"}},
    {"from": {"remote": "gdrive", "path": "研究/待整理"}, "to": {"remote": "dropbox", "path": "學校/待整理"}},
]
CATEGORY = {"pdf": "pdf", "docx": "document", "txt": "document", "csv": "spreadsheet", "pptx": "presentation",
            "jpg": "photo", "zip": "archive"}


def _join(base: str, name: str) -> str:
    return f"{base}/{name}" if base else name


def _loc(remote: str, path: str) -> dict:
    """A place as the server sends it in a message's params."""
    return {"remote": remote, "path": path}


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

    def __init__(self, *, approvals_enabled: bool = True, execution_enabled: bool = True, quarantine: bool = False):
        self.approvals_enabled = approvals_enabled
        self.execution_enabled = execution_enabled
        self.fs = copy.deepcopy(SAMPLE)
        if quarantine:      # what an executed plan leaves behind on gdrive
            self.fs["gdrive"][QUARANTINE] = {"7": {"學校": {"old-report.pdf": 52_000}, "scan.zip": 1_000_000}}
        self.force_error: tuple[str, str] | None = None
        self.stale: dict[int, dict] = {}
        self.calls: list[Call] = []
        self.preview_mode = "ok"
        self.violations_extra: list[dict] = []
        self.outdated: list[str] = []          # what /v1/ui/config reports as changed since the last completed scan
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

    def vanish(self, remote: str, path: str) -> None:
        """A rescan finds that something is gone from the cloud."""
        del _get(self.fs, remote, _parent(path))[_name(path)]

    # ------------------------------------------------------------------ namespace
    def replay(self) -> tuple[dict, dict, list]:
        """Apply the active actions; one whose source or destination is gone is stale and changes nothing."""
        tree, flags, ghosts = copy.deepcopy(self.fs), {}, []
        self.moved = {"files": 0, "bytes": 0, "cross": 0, "remotes": set()}
        self.stale = {}
        for a in self.draft["actions"]:
            if a["state"] != "active":
                continue
            try:
                self._apply(tree, flags, ghosts, a)
            except Stage as e:
                if e.code not in ("SRC_NOT_FOUND", "DST_NOT_DIR"):
                    raise
                gone = ([it for it in a["items"] if not it["path"] or _get(tree, it["remote"], it["path"]) is None]
                        if e.code == "SRC_NOT_FOUND" else [a["dst"]])
                self.stale[a["id"]] = {"code": e.code, "message": str(e),
                                       "items": [{"remote": it["remote"], "path": it["path"]} for it in gone]}
        return tree, flags, ghosts

    def _apply(self, tree: dict, flags: dict, ghosts: list, a: dict) -> list[dict]:
        warnings: list[dict] = []
        for place in [it["path"] for it in a.get("items", [])] + [_join(a["dst"]["path"], a.get("new_name") or "")]:
            if place == QUARANTINE or place.startswith(QUARANTINE + "/"):
                raise Stage("QUARANTINE", "the quarantine folder holds deleted files", item=_loc(a["dst"]["remote"], place))
        self.moved["remotes"].add(a["dst"]["remote"])
        dst = a["dst"]
        dnode = _get(tree, dst["remote"], dst["path"])
        if not isinstance(dnode, dict):
            raise Stage("DST_NOT_DIR", f"{dst['remote']}:{dst['path']} is not a folder", dst=_loc(dst["remote"], dst["path"]))
        if a["kind"] == "mkdir":
            name = a["new_name"]
            if name in dnode:
                raise Stage("NOOP" if isinstance(dnode[name], dict) else "NAME_EXISTS", f"{name} exists",
                            item=_loc(dst["remote"], _join(dst["path"], name)))
            dnode[name] = {}
            flags[(dst["remote"], _join(dst["path"], name))] = {"virtual": True}
            return warnings
        for it in a["items"]:
            if not it["path"] or _get(tree, it["remote"], it["path"]) is None:
                raise Stage("SRC_NOT_FOUND", f"{it['remote']}:{it['path']} does not exist", item=_loc(it["remote"], it["path"]))
        moved = 0
        for it in a["items"]:
            node = _get(tree, it["remote"], it["path"])
            if isinstance(node, dict) and it["remote"] == dst["remote"] and (
                    dst["path"] == it["path"] or dst["path"].startswith(it["path"] + "/")):
                raise Stage("INTO_ITSELF", "cannot move a folder into itself", item=_loc(it["remote"], it["path"]))
            name = a.get("new_name") or _name(it["path"])
            target = _join(dst["path"], name)
            if (it["remote"], it["path"]) == (dst["remote"], target):
                continue
            existing = dnode.get(name)
            if existing is not None and not (isinstance(node, dict) and isinstance(existing, dict)):
                raise Stage("NAME_EXISTS", f"{dst['remote']}:{target} already exists", item=_loc(dst["remote"], target))
            files, _, size = _stats(node)
            self.moved["files"] += files
            self.moved["bytes"] += size
            self.moved["cross"] += size if it["remote"] != dst["remote"] else 0
            self.moved["remotes"].add(it["remote"])
            del _get(tree, it["remote"], _parent(it["path"]))[_name(it["path"])]
            if existing is not None:
                self._merge(existing, node)
                warnings.append({"code": "MERGE", "message": f"merges into {dst['remote']}:{target}",
                                 "params": {"src": _loc(it["remote"], it["path"]), "into": _loc(dst["remote"], target)}})
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
                    warnings.append({"code": "CROSS_REMOTE", "message": "bytes are copied through this machine",
                                     "params": {"n": sum(x["remote"] != dst["remote"] for x in a["items"])}})
            moved += 1
        if not moved:
            raise Stage("NOOP", "every item is already where it would be moved to", dst=_loc(dst["remote"], dst["path"]))
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
               "note": body.get("note"), "created_by": created_by, "state": "active", "created_at": "2024-03-01T10:00:00Z",
               "stale": False, "stale_reason": None}
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
        for a in d["actions"]:
            a["stale_reason"] = self.stale.get(a["id"])
            a["stale"] = a["stale_reason"] is not None
        d["summary"] = {"moved_files": m["files"], "moved_bytes": m["bytes"], "cross_remote_bytes": m["cross"],
                        "remotes_touched": sorted(m["remotes"]), "stale_actions": len(self.stale)}
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
                "conflict": False, "dedupe": False, "virtual": fl.get("virtual", False),
                "quarantine": path == QUARANTINE or path.startswith(QUARANTINE + "/"), "quarantine_root": path == QUARANTINE}

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
                        "virtual": False, "quarantine": False, "quarantine_root": False, "remote_id": r["id"], "provider": r["provider"], "writable": r["writable"],
                        "case_insensitive": r["case_insensitive"]})
        return out

    # ------------------------------------------------------------------ preview payloads
    def preview(self) -> dict:
        d = self._draft_json()
        active = [a for a in d["actions"] if a["state"] == "active"]
        stale = [a for a in active if a["stale"]]
        if self.preview_mode == "only-empty-folders" and active:
            return {"plan_id": None, "message": "nothing to do", "stale_actions": [],
                    "empty_dirs_skipped": [EMPTY_FOLDERS[0]]}
        if not active or len(stale) == len(active):
            return {"plan_id": None, "message": "nothing to do", "stale_actions": stale, "empty_dirs_skipped": []}
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
                        "dedupes": 1, "conflicts": 0, "ops": {"move": 0, "copy": 2, "delete": 2}, "roots": 1,
                        "stale_actions": len(stale)},
            "changes": [{"from": {"remote": "gdrive", "path": "研究/資料"}, "to": {"remote": "dropbox", "path": "學校/資料"},
                         "files": 2, "bytes": 123_000, "kind": "copy+delete", "is_dir": True}],
            "conflicts": [], "empty_dirs_left": 1, "stale_actions": stale, "empty_dirs_skipped": [],
            "simulation": {"plan_id": 7, "ok": True, "violations": [], "warnings": [], "remotes": remotes,
                           "contents_before": 9, "contents_after": 9, "contents_lost": 0,
                           "bytes_cross_remote": s["cross_remote_bytes"], "op_counts": {"copy": 2, "delete": 2}, "sample": []},
        }
        if self.preview_mode == "empty-folders":
            base["empty_dirs_skipped"] = EMPTY_FOLDERS
            base["summary"]["empty_dirs_skipped"] = len(EMPTY_FOLDERS)
        if self.preview_mode == "violations":
            base["ok"] = False
            base["simulation"]["ok"] = False
            base["simulation"]["violations"] = [
                {"code": "DST_EXISTS", "seq": 1, "message": "'dropbox:學校/report.pdf' holds different or unproven content; never overwritten",
                 "params": {"variant": "different", "at": _loc("dropbox", "學校/report.pdf")}},
                {"code": "CASE_COLLISION", "seq": 2, "message": "'dropbox:學校/Report.PDF' collides with 'dropbox:學校/report.pdf' (case-insensitive)",
                 "params": {"variant": "name", "at": _loc("dropbox", "學校/Report.PDF"), "other": _loc("dropbox", "學校/report.pdf")}},
            ]
            base["simulation"]["violations"] += self.violations_extra
            base["simulation"]["warnings"] = [
                {"code": "NOOP", "message": "move of 'gdrive:a' onto itself", "seq": 3,
                 "params": {"variant": "onto_itself", "at": _loc("gdrive", "a")}}]
        elif self.preview_mode == "conflicts":
            base["ok"] = False
            base["simulation"]["ok"] = False
            base["simulation"]["violations"] = [
                {"code": "DST_EXISTS", "seq": 1,
                 "message": "'dropbox:學校/資料/survey.csv' holds different or unproven content; never overwritten",
                 "params": {"variant": "different", "at": _loc("dropbox", "學校/資料/survey.csv")}}]
            base["summary"]["conflicts"] = len(ENGINE_CONFLICTS)
            base["conflicts"] = [
                {"code": code, "reason": reason, "from": {"remote": "gdrive", "path": f"研究/資料/survey-{i}.csv"},
                 "to": {"remote": "dropbox", "path": f"學校/資料/survey-{i}.csv"},
                 "with": None if code in ("OCCUPIED", "CYCLE", "CASE_ONLY_RENAME") else {"remote": "dropbox", "path": f"學校/資料/survey-{i}.csv"}}
                for i, (code, reason) in enumerate(ENGINE_CONFLICTS)]
        elif self.preview_mode in ("overlap", "overlap-legacy"):
            # One clash told by both the simulator and the workspace, on a read-only cloud, with the echoes it causes.
            # "overlap-legacy" is the same report as stored before `params` existed: the messages alone.
            spot = {"remote": "dropbox", "path": "學校/report.pdf"}
            base["ok"] = False
            base["simulation"]["ok"] = False
            read_only = {"at": _loc("dropbox", "")}
            missing = {"variant": "missing", "why": "absent"}
            base["simulation"]["violations"] = [
                {"code": "READ_ONLY_REMOTE", "message": "'dropbox:' has no write_remote configured", "seq": 1,
                 "params": read_only},
                {"code": "DST_EXISTS", "message": "'dropbox:學校/report.pdf' holds different or unproven content; never overwritten",
                 "seq": 1, "params": {"variant": "different", "at": spot}},
                {"code": "READ_ONLY_REMOTE", "message": "'dropbox:' has no write_remote configured", "seq": 2,
                 "params": read_only},
                {"code": "NO_WITNESS", "message": "no live file at the witness 'dropbox:學校/資料/x.csv': the catalog has no such file",
                 "seq": 3, "params": {**missing, "at": _loc("dropbox", "學校/資料/x.csv")}},
                {"code": "NO_WITNESS", "message": "no live file at the witness 'gdrive:a.txt': the catalog has no such file",
                 "seq": 4, "params": {**missing, "at": _loc("gdrive", "a.txt")}},
            ]
            if self.preview_mode == "overlap-legacy":
                for v in base["simulation"]["violations"]:
                    del v["params"]
            base["summary"]["conflicts"] = 1
            base["conflicts"] = [{"code": "DIFFERENT_CONTENT", "reason": "a different file already has that name",
                                  "from": {"remote": "gdrive", "path": "學校/report.pdf"}, "to": spot, "with": spot}]
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
                         "remotes": REMOTES, "version": "0.1.0", "csrf": self.CSRF, "outdated": self.outdated}
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
                if self.force_error:
                    code, message = self.force_error
                    self.force_error = None
                    return 409, {"error": message, "code": code}
                try:
                    act, warnings = self._commit(body, "ui")
                except Stage as e:
                    return 409, {"error": str(e), "code": e.code, **({"params": e.params} if e.params else {})}
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
        """What suggest.py sends: English `title` and `reason` for the CLI, and the same as a `code` and `params`."""
        if kind == "duplicates":
            return [{"id": "duplicates:aaa111", "kind": "duplicates", "score": 0.95,
                     "title": "These 2 folders hold almost the same content: 學校",
                     "reason": "Content similarity 95% (only content proven identical by hash counts; 1 shared). "
                               "Merging the others into \"gdrive:學校\" turns identical files into witnessed dedupes "
                               "and frees about 72.4 KB.",
                     "code": "DUPLICATES", "params": {"count": 2, "names": ["學校"], "kept": "gdrive:學校",
                                                      "similarity": 0.95, "shared": 1, "saved_bytes": 74_100},
                     "folders": [{"remote": "gdrive", "path": "學校", "files": 3, "bytes": 1_032_000},
                                 {"remote": "dropbox", "path": "學校", "files": 2, "bytes": 74_100}],
                     "terms": ["學校"],
                     "actions": [{"kind": "move", "items": [{"remote": "dropbox", "path": "學校"}],
                                  "dst": {"remote": "gdrive", "path": ""}, "note": "duplicates:aaa111"}]}]
        return [{"id": "related:bbb222", "kind": "related", "score": 0.61,
                 "title": "These 2 folders look related: 論文, 資料",
                 "reason": "their folder names share \"研究\"; 1 file inside has exactly the same content. "
                           "Suggested: tidy them under \"gdrive:研究/論文\".",
                 "code": "RELATED", "params": {"count": 2, "names": ["論文", "資料"], "anchor": "gdrive:研究/論文",
                                               "name_terms": ["研究"], "content_terms": [], "same": 1},
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

    def awaiting(self, method: str, pattern: str):
        """`with ui.awaiting("POST", r"/v1/drafts/1/actions"): <key or click>` returns once the page's request was answered.

        A key press or click returns before the page sends its request, and with the sync API the mock's route handler
        only runs inside a Playwright call, so asserting on ui.mock right after the action races the request.
        """
        return self.page.expect_response(
            lambda r: r.request.method == method and re.fullmatch(pattern, urlparse(r.url).path) is not None)

    def drag(self, src: tuple[str, str], dst: tuple[str, str]) -> None:
        self.settle()
        self.page.drag_and_drop(self.sel(*src), self.sel(*dst))

    def dragover_allowed(self, src: tuple[str, str], dst: tuple[str, str]) -> bool:
        """Would the browser offer a drop here? Fires dragstart on `src` and dragover on `dst` as the page sees them:
        a handler that calls preventDefault() on dragover is what makes an element accept a drop."""
        return self.page.evaluate("""([src, dst]) => {
            const dt = new DataTransfer();
            const fire = (el, type) => el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
            fire(document.querySelector(src), 'dragstart');
            const allowed = !fire(document.querySelector(dst), 'dragover');
            window.dispatchEvent(new Event('dragend'));
            return allowed;
        }""", [self.sel(*src), self.sel(*dst)])

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
                color_scheme: str = "light", quarantine: bool = False, locale: str = "zh-TW",
                languages: list[str] | None = None, block_storage: bool = False) -> Ui:
        """`locale` is the browser's language (the page starts in Chinese for the default zh-TW); `languages` replaces
        navigator.languages with a longer list; `block_storage` makes localStorage throw, as in a private window."""
        ctx = browser.new_context(viewport={"width": width, "height": height}, locale=locale, color_scheme=color_scheme)
        contexts.append(ctx)
        if languages is not None:
            ctx.add_init_script(
                f"Object.defineProperty(navigator, 'languages', {{get: () => {json.dumps(languages)}}});"
                f"Object.defineProperty(navigator, 'language', {{get: () => {json.dumps(languages[0] if languages else '')}}});")
        if block_storage:
            ctx.add_init_script("Object.defineProperty(window, 'localStorage', "
                                "{get() { throw new DOMException('blocked', 'SecurityError'); }});")
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None)
        mock = MockApi(approvals_enabled=approvals_enabled, execution_enabled=execution_enabled, quarantine=quarantine)
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
    with ui.awaiting("POST", r"/v1/drafts/1/actions"):
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
    with ui.awaiting("POST", r"/v1/drafts/1/actions"):
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
    with ui.awaiting("POST", r"/v1/drafts/1/actions"):
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
    with ui.awaiting("POST", r"/v1/drafts/1/actions"):
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

    with ui.awaiting("POST", r"/v1/drafts/1/actions"):
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


def test_the_detail_line_of_a_violation_is_worded_by_the_page_from_its_params(make_ui):
    """The engine's English sentence is for terminals; under each headline the page writes the same facts in its
    own language, from the violation's `params`, and keeps the code beside them."""
    ui = make_ui().open()
    ui.mock.preview_mode = "violations"
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("violation")).to_have_count(2)
    first, second = modal.get_by_test_id("violation").all()
    for item, code, said in ((first, "DST_EXISTS", "「dropbox:學校/report.pdf」已有內容不同或未能證明相同的檔案；絕不覆寫。"),
                             (second, "CASE_COLLISION", "「dropbox:學校/Report.PDF」與「dropbox:學校/report.pdf」衝突（不分大小寫）。")):
        raw = item.get_by_test_id("problem-raw")
        expect(raw).to_have_text(f"{code} {said}")
        assert "holds" not in raw.inner_text() and "collides" not in raw.inner_text()      # not the engine's English
    modal.locator("summary").click()
    expect(modal.get_by_test_id("warning").get_by_test_id("problem-raw")).to_have_text("NOOP 把「gdrive:a」搬到它自己身上。")
    ui.page.keyboard.press("Escape")

    ui.page.get_by_test_id("lang-toggle").click()
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("violation")).to_have_count(2)
    first, second = modal.get_by_test_id("violation").all()
    expect(first.get_by_test_id("problem-raw")).to_have_text(
        'DST_EXISTS "dropbox:學校/report.pdf" holds different or unproven content; it is never overwritten.')
    expect(second.get_by_test_id("problem-raw")).to_have_text(
        'CASE_COLLISION "dropbox:學校/Report.PDF" collides with "dropbox:學校/report.pdf" (case-insensitive).')
    modal.locator("summary").click()
    expect(modal.get_by_test_id("warning").get_by_test_id("problem-raw")).to_have_text('NOOP Move of "gdrive:a" onto itself.')


def test_a_violation_without_params_shows_the_engines_english_and_still_links_to_its_place(make_ui):
    """A report stored before `params` existed (or a newer server's variant this page has no sentence for) is shown
    as the engine wrote it; the reveal link then finds the place in the message, as it always did."""
    ui = make_ui().open()
    ui.mock.preview_mode = "overlap-legacy"
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    raw = modal.get_by_test_id("violation").nth(1).get_by_test_id("problem-raw")
    expect(raw).to_contain_text("no live file at the witness 'gdrive:a.txt'")
    expect(modal.get_by_test_id("violation").nth(1).get_by_test_id("reveal-link")).to_be_visible()


def test_a_violation_variant_the_page_has_no_sentence_for_falls_back_to_the_english_message(make_ui):
    ui = make_ui().open()
    ui.mock.preview_mode = "violations"
    ui.mock.violations_extra = [{"code": "DST_EXISTS", "seq": 9, "message": "an engine sentence from the future",
                                 "params": {"variant": "from_the_future", "at": _loc("dropbox", "學校/report.pdf")}}]
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("violation").last.get_by_test_id("problem-raw")).to_have_text(
        "DST_EXISTS an engine sentence from the future")


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


# --- conflicts, in the person's language

def _open_conflict_preview(ui: Ui):
    ui.mock.preview_mode = "conflicts"
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("conflict")).to_have_count(len(ENGINE_CONFLICTS))
    return modal


def test_every_conflict_code_is_explained_in_chinese_and_the_detail_line_is_secondary_monospace(make_ui):
    ui = make_ui().open()
    modal = _open_conflict_preview(ui)
    titles = {"DIFFERENT_CONTENT": "同名但內容不同", "DIR_IN_WAY": "同名的資料夾擋住", "FILE_IN_WAY": "同名的檔案擋住",
              "OCCUPIED": "位置要到最後才會空出來", "CYCLE": "搬移互相等待", "CASE_ONLY_RENAME": "只改大小寫的更名"}
    for code, reason in ENGINE_CONFLICTS:
        item = modal.locator(f'[data-testid="conflict"][data-code="{code}"]')
        expect(item).to_have_count(1)
        expect(item.locator(".problem-head strong")).to_contain_text(titles[code])
        text = item.locator(".problem-text").inner_text()
        assert re.search(r"[一-鿿]", text) and reason not in text, text          # an explanation, not the engine's sentence
        raw = item.get_by_test_id("problem-raw")
        expect(raw).to_contain_text(code)                                          # the raw code is kept, with the detail ...
        detail = raw.inner_text()
        assert re.search(r"[一-鿿]", detail) and reason not in detail, detail    # ... in Chinese, not the engine's English
        assert "mono" in raw.evaluate("e => getComputedStyle(e).fontFamily").lower()   # ... in monospace ...
        size = lambda sel: item.locator(sel).evaluate("e => parseFloat(getComputedStyle(e).fontSize)")  # noqa: E731
        assert size(".problem-detail") < size(".problem-head strong")              # ... and smaller than the headline
        head = item.locator(".problem-head").inner_text()
        assert code not in head and reason not in head                             # the headline itself has no English code
    expect(modal.get_by_test_id("conflict").first.get_by_test_id("conflict-path")).to_contain_text("survey-0.csv")
    ui.page.keyboard.press("Escape")

    # The same in English once asked for.
    ui.page.get_by_test_id("lang-toggle").click()
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.locator('[data-testid="conflict"][data-code="DIFFERENT_CONTENT"] .problem-head strong')).to_have_text(
        "Same name, different content")
    expect(modal.locator('[data-testid="conflict"][data-code="CYCLE"] .problem-text')).to_contain_text("temporary name")
    said = {"DIFFERENT_CONTENT": "different file already has that name", "DIR_IN_WAY": "folder with that name is in the way",
            "FILE_IN_WAY": "file is where this needs a folder", "OCCUPIED": "still occupied until the delete phase",
            "CYCLE": "wait on each other", "CASE_ONLY_RENAME": "ignores case"}
    for code, _ in ENGINE_CONFLICTS:                                   # the detail line says what the engine says, in English
        raw = modal.locator(f'[data-testid="conflict"][data-code="{code}"]').get_by_test_id("problem-raw")
        expect(raw).to_contain_text(code)
        expect(raw).to_contain_text(said[code])
        assert not re.search(r"[一-鿿]", raw.inner_text())


# --- steps that a rescan left with nothing to act on

def test_a_step_that_a_rescan_made_stale_is_struck_through_explained_and_named_in_the_preview(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.drag(("left", "gdrive:學校/課程"), ("right", "dropbox:Archive"))
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("2")
    expect(ui.page.get_by_test_id("staged-stale-count")).to_have_count(0)

    ui.mock.vanish("gdrive", "研究/資料")                      # a rescan (elsewhere) notices that the folder is gone
    pill = ui.page.get_by_test_id("staged-stale-count")
    expect(pill).to_have_text("1", timeout=8000)               # the page's poll notices on its own
    assert "不會被執行" in pill.get_attribute("title")
    expect(ui.page.get_by_test_id("toast").filter(has_text="已失效")).to_be_visible()

    ui.page.get_by_test_id("staged-toggle").click()
    items = ui.page.get_by_test_id("staged-item")
    expect(items).to_have_count(2)
    stale, fine = items.first, items.nth(1)
    expect(stale).to_have_attribute("data-stale", "true")
    expect(fine).to_have_attribute("data-stale", "false")
    why = stale.get_by_test_id("staged-stale")
    expect(why).to_contain_text("已失效")
    expect(why).to_contain_text("來源已不存在：gdrive:研究/資料")
    expect(why).to_contain_text("這一步不會產生任何動作")
    assert "line-through" in stale.locator(".staged-title").evaluate("e => getComputedStyle(e).textDecorationLine")
    assert "line-through" not in fine.locator(".staged-title").evaluate("e => getComputedStyle(e).textDecorationLine")
    expect(fine.get_by_test_id("staged-stale")).to_have_count(0)
    expect(ui.page.get_by_test_id("staged-stale-summary")).to_contain_text("1 個步驟已失效")
    expect(ui.node("right", "dropbox:學校/資料")).to_have_count(0)            # the stale step moved nothing
    expect(ui.node("right", "dropbox:Archive/課程")).to_be_visible()          # the other one did

    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    box = modal.get_by_test_id("stale-actions")
    expect(box).to_be_visible()
    expect(box).to_contain_text("有 1 個暫存的步驟已失效，不會被執行")
    expect(box.get_by_test_id("stale-action")).to_have_count(1)
    expect(box.get_by_test_id("stale-action")).to_contain_text("移動 1 項 → dropbox:學校")
    expect(box.get_by_test_id("stale-action")).to_contain_text("來源已不存在：gdrive:研究/資料")
    expect(modal.get_by_test_id("approval-button")).to_be_visible()           # the rest can still be approved
    ui.page.keyboard.press("Escape")

    stale.get_by_test_id("staged-remove").click()                              # tidy up: the warning goes away
    expect(ui.page.get_by_test_id("staged-stale-count")).to_have_count(0)
    expect(ui.page.get_by_test_id("staged-stale-summary")).to_have_count(0)


def test_a_draft_of_only_stale_steps_says_why_there_is_nothing_to_run(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.mock.vanish("gdrive", "研究/資料")
    expect(ui.page.get_by_test_id("staged-stale-count")).to_have_text("1", timeout=8000)
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("preview-empty")).to_contain_text("沒有任何可以執行的步驟")
    expect(modal.get_by_test_id("preview-empty")).not_to_contain_text("來回拖放")   # not the "dragged back" message
    expect(modal.get_by_test_id("stale-action")).to_have_count(1)
    expect(modal.get_by_test_id("approval-button")).to_have_count(0)
    ui.page.keyboard.press("Escape")
    ui.page.get_by_test_id("lang-toggle").click()
    ui.page.get_by_test_id("staged-toggle").click()
    expect(ui.page.get_by_test_id("staged-stale")).to_contain_text("The source no longer exists: gdrive:研究/資料")


# --- folders that hold no files

def test_moved_folders_without_files_are_listed_in_the_preview_not_dropped_silently(make_ui):
    """Ops are per file, so a folder with no file in it is never created at its new place: say so, in both languages."""
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.mock.preview_mode = "empty-folders"
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    box = modal.get_by_test_id("empty-dirs-skipped")
    expect(box).to_contain_text("2 個資料夾裡面沒有任何檔案")
    expect(box.get_by_test_id("empty-dir-skipped")).to_have_count(2)
    expect(box.get_by_test_id("empty-dir-skipped").first).to_contain_text("研究/空的")
    expect(box.get_by_test_id("empty-dir-skipped").first).to_contain_text("學校/空的")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")     # a note, not a problem
    ui.page.keyboard.press("Escape")
    ui.page.get_by_test_id("lang-toggle").click()
    ui.page.get_by_test_id("preview-button").click()
    box = ui.page.get_by_test_id("preview-modal").get_by_test_id("empty-dirs-skipped")
    expect(box).to_contain_text("2 folders hold no files, so moving them changes nothing in the cloud")
    ui.mock.preview_mode = "ok"


def test_a_draft_that_only_moves_folders_without_files_says_there_is_nothing_to_run(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.mock.preview_mode = "only-empty-folders"
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("preview-empty")).to_contain_text("只搬動了裡面沒有任何檔案的資料夾")
    expect(modal.get_by_test_id("preview-empty")).not_to_contain_text("來回拖放")      # not the "dragged back" message
    expect(modal.get_by_test_id("empty-dir-skipped")).to_have_count(1)
    expect(modal.get_by_test_id("approval-button")).to_have_count(0)
    ui.page.keyboard.press("Escape")
    ui.page.get_by_test_id("lang-toggle").click()
    ui.page.get_by_test_id("preview-button").click()
    expect(ui.page.get_by_test_id("preview-modal").get_by_test_id("empty-dirs-skipped")).to_contain_text(
        "1 folder holds no files, so moving it changes nothing in the cloud")


# --- the quarantine folder

def test_the_quarantine_folder_is_drawn_apart_and_is_neither_draggable_nor_a_drop_target(make_ui):
    ui = make_ui(quarantine=True).open()
    ui.expand_all()
    q = ui.node("left", f"gdrive:{QUARANTINE}")
    expect(q).to_be_visible()
    expect(q).to_have_attribute("data-quarantine", "root")
    expect(q.get_by_test_id("quarantine-label")).to_have_text("隔離區")
    expect(q).to_contain_text(QUARANTINE)                                     # the real folder name stays visible, small
    expect(q).to_have_attribute("draggable", "false")
    assert q.get_attribute("data-droptarget") is None
    tip = q.get_attribute("title")
    assert "drivemerge plan restore" in tip and "drivemerge plan purge" in tip and QUARANTINE in tip
    assert q.get_by_test_id("quarantine-label").get_attribute("title") == tip   # the label itself carries it too
    assert ui.node("left", "gdrive:研究").get_attribute("title") is None        # ordinary folders have no such tooltip
    posts = len(ui.mock.calls_to("POST", "/v1/drafts/1/actions"))

    ui.drag(("left", f"gdrive:{QUARANTINE}"), ("right", "dropbox:學校"))        # out of the tree
    ui.drag(("left", "gdrive:研究/資料"), ("left", f"gdrive:{QUARANTINE}"))    # into it
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/actions")) == posts

    # Hovering a dragged folder over it offers no drop; an ordinary folder is the control.
    assert ui.dragover_allowed(("left", "gdrive:研究/資料"), ("right", "dropbox:Archive"))
    assert not ui.dragover_allowed(("left", "gdrive:研究/資料"), ("left", f"gdrive:{QUARANTINE}"))
    assert not ui.dragover_allowed(("left", f"gdrive:{QUARANTINE}"), ("right", "dropbox:Archive"))
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/actions")) == posts

    # What is inside is locked as well, and says why.
    ui.expand("left", f"gdrive:{QUARANTINE}")
    ui.expand("left", f"gdrive:{QUARANTINE}/7")
    inner = ui.node("left", f"gdrive:{QUARANTINE}/7/學校")
    expect(inner).to_have_attribute("data-quarantine", "inside")
    expect(inner).to_have_attribute("draggable", "false")
    assert "drivemerge plan restore" in inner.get_attribute("title")
    expect(ui.node("left", f"gdrive:{QUARANTINE}/7/scan.zip")).to_have_attribute("draggable", "false")
    ui.drag(("left", f"gdrive:{QUARANTINE}/7/scan.zip"), ("right", "dropbox:學校"))
    ui.drag(("left", "gdrive:研究/資料"), ("left", f"gdrive:{QUARANTINE}/7/學校"))
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/actions")) == posts
    # Dropping on a breadcrumb that lies inside the quarantine folder is no way in either.
    inner.click()                       # the left pane's breadcrumbs now run through the quarantine folder
    crumb = ui.page.get_by_test_id(f"crumb-gdrive:{QUARANTINE}")
    expect(crumb).to_be_visible()
    assert not ui.dragover_allowed(("right", "dropbox:學校/作業"), ("left", f"gdrive:{QUARANTINE}/7/學校"))
    ui.page.locator(ui.sel("right", "dropbox:學校/作業")).drag_to(crumb)
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/actions")) == posts
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("0")


def test_the_keyboard_has_no_way_into_or_out_of_the_quarantine_folder_either(make_ui):
    ui = make_ui(quarantine=True).open()
    ui.expand_all()
    q = ui.node("left", f"gdrive:{QUARANTINE}")
    toasts = ui.page.get_by_test_id("toast")
    q.click()
    ui.page.keyboard.press("Control+x")                                       # nothing is cut
    ui.page.keyboard.press("F2")                                              # nothing is renamed
    q.get_by_test_id("quarantine-label").dblclick()
    expect(ui.page.get_by_test_id("rename-input")).to_have_count(0)
    expect(toasts.filter(has_text="已剪下")).to_have_count(0)

    ui.node("left", "gdrive:研究/資料").click()
    ui.page.keyboard.press("Control+x")
    expect(toasts.filter(has_text="已剪下 1 項")).to_be_visible()
    q.click()
    ui.page.keyboard.press("Control+v")                                       # no paste into it
    expect(toasts.filter(has_text="隔離區不能")).to_be_visible()
    ui.page.get_by_test_id("mkdir-left").click()                              # nor a new folder
    expect(ui.page.get_by_test_id("dialog")).to_have_count(0)
    expect(toasts.filter(has_text="隔離區不能")).to_have_count(1)             # the same notice, not a pile of them
    assert not ui.mock.calls_to("POST", "/v1/drafts/1/actions")

    # Cut with the quarantine row in the selection: it is left out, the rest goes.
    ui.node("left", "gdrive:研究/論文").click()
    q.click(modifiers=["Control"])
    ui.page.keyboard.press("Control+x")
    ui.node("right", "dropbox:學校").click()
    with ui.awaiting("POST", r"/v1/drafts/1/actions"):
        ui.page.keyboard.press("Control+v")
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body["items"] == [{"remote": "gdrive", "path": "研究/論文"}]


def test_a_refusal_from_the_server_about_the_quarantine_folder_is_explained(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.mock.force_error = ("QUARANTINE", "the quarantine folder holds deleted files")
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    toast = ui.page.get_by_test_id("toast").filter(has_text="隔離區")
    expect(toast).to_contain_text("drivemerge plan restore")
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("0")


# --- toasts

def _boxes(locator):
    return [b for b in (locator.nth(i).bounding_box() for i in range(locator.count())) if b]


def _fresh_page_on_a_jumpable_clock(ui: Ui) -> None:
    """Time runs normally but can be jumped (page.clock.fast_forward), so the 6 s of a toast need no real wait."""
    ui.page.clock.install()
    ui.page.reload()
    expect(ui.node("left", "gdrive:")).to_be_visible()
    ui.expand_all()


def test_toasts_stack_bottom_right_info_leaves_by_itself_and_errors_stay_until_closed(make_ui):
    ui = make_ui(width=1440, height=900).open()
    _fresh_page_on_a_jumpable_clock(ui)
    toasts = ui.page.get_by_test_id("toast")
    ui.node("left", "gdrive:研究/資料").click()
    ui.page.keyboard.press("Control+x")                                       # an info toast
    info = toasts.filter(has_text="已剪下")
    expect(info).to_be_visible()
    ui.page.keyboard.press("Control+v")                                       # pasting onto itself: an error toast
    error = toasts.filter(has_text="不能貼上")
    expect(error).to_be_visible()
    expect(info).to_have_attribute("data-kind", "info")
    expect(error).to_have_attribute("data-kind", "error")

    # Bottom right, one above the other, inside the window.
    first, second = _boxes(toasts)[:2]
    for b in (first, second):
        assert b["x"] + b["width"] > 1440 * 0.9 and b["x"] > 1440 * 0.5, b      # at the right edge, not in the middle
        assert b["x"] + b["width"] <= 1440 and b["y"] + b["height"] <= 900
    assert second["y"] >= first["y"] + first["height"] - 1                      # stacked, not overlapping
    assert second["y"] + second["height"] > 900 - 40                            # the stack sits at the bottom

    ui.page.clock.fast_forward(5000)
    expect(info).to_be_visible()                                                # not yet
    ui.page.clock.fast_forward(2000)
    expect(info).to_have_count(0)                                               # info leaves by itself ...
    ui.page.clock.fast_forward(60_000)
    expect(error).to_be_visible()                                               # ... an error does not
    error.get_by_test_id("toast-close").click()
    expect(error).to_have_count(0)


def test_a_warning_leaves_by_itself_too(make_ui):
    ui = make_ui().open()
    _fresh_page_on_a_jumpable_clock(ui)
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))          # CROSS_REMOTE: a warning
    warn = ui.page.get_by_test_id("toast").filter(has_text="跨雲")
    expect(warn).to_have_attribute("data-kind", "warn")
    ui.page.clock.fast_forward(5000)
    expect(warn).to_be_visible()
    ui.page.clock.fast_forward(2000)
    expect(warn).to_have_count(0)


def test_a_toast_waits_while_the_pointer_is_on_it(make_ui):
    ui = make_ui().open()
    _fresh_page_on_a_jumpable_clock(ui)
    ui.node("left", "gdrive:研究/資料").click()
    ui.page.keyboard.press("Control+x")
    info = ui.page.get_by_test_id("toast").filter(has_text="已剪下")
    expect(info).to_be_visible()
    info.hover()
    ui.page.wait_for_timeout(150)
    ui.page.clock.fast_forward(30_000)
    expect(info).to_be_visible()                                                # being read: it stays
    ui.page.mouse.move(300, 300)
    ui.page.wait_for_timeout(150)
    ui.page.clock.fast_forward(5000)
    expect(info).to_be_visible()
    ui.page.clock.fast_forward(2000)
    expect(info).to_have_count(0)

    # Keyboard focus on its button holds it just the same.
    ui.node("left", "gdrive:研究/論文").click()
    ui.page.keyboard.press("Control+x")
    again = ui.page.get_by_test_id("toast").filter(has_text="已剪下")
    expect(again).to_be_visible()
    again.get_by_test_id("toast-close").focus()
    ui.page.wait_for_timeout(150)
    ui.page.clock.fast_forward(30_000)
    expect(again).to_be_visible()
    ui.node("left", "gdrive:研究/論文").click()                                 # focus moves away
    ui.page.wait_for_timeout(150)
    ui.page.clock.fast_forward(7000)
    expect(again).to_have_count(0)


def test_a_repeated_message_replaces_its_earlier_copy(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.node("left", "gdrive:研究/資料").click()
    for _ in range(3):
        ui.page.keyboard.press("Control+x")
    expect(ui.page.get_by_test_id("toast").filter(has_text="已剪下")).to_have_count(1)


def _assert_apart(a: dict, b: dict) -> None:
    assert (a["x"] + a["width"] <= b["x"] + 0.5 or b["x"] + b["width"] <= a["x"] + 0.5
            or a["y"] + a["height"] <= b["y"] + 0.5 or b["y"] + b["height"] <= a["y"] + 0.5), (a, b)


@pytest.mark.parametrize("height", [900, 640])
def test_a_toast_never_covers_the_footer_of_the_preview_modal(make_ui, height):
    ui = make_ui(width=1280, height=height).open()
    _fresh_page_on_a_jumpable_clock(ui)
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("approval-button")).to_be_visible()
    before = modal.locator(".modal-foot").bounding_box()
    assert before["y"] + before["height"] <= height

    # Claude stages something meanwhile: the poll shows a toast over the open dialog.
    ui.mock.external_action(created_by="mcp", items=[{"remote": "gdrive", "path": "研究/論文"}],
                            dst={"remote": "dropbox", "path": "Archive"})
    ui.page.clock.fast_forward(3500)
    toast = ui.page.get_by_test_id("toast").filter(has_text="Claude")
    expect(toast).to_be_visible()
    ui.page.wait_for_timeout(400)                                                # the dialog settles into its smaller room
    foot = modal.locator(".modal-foot").bounding_box()
    t = toast.bounding_box()
    assert foot["y"] + foot["height"] <= height and foot["height"] > 20          # the footer is all there
    _assert_apart(foot, t)
    _assert_apart(foot, ui.page.get_by_test_id("toasts").bounding_box())
    expect(modal.get_by_test_id("preview-close")).to_be_visible()
    modal.get_by_test_id("preview-close").click()                                # and its buttons can be pressed
    expect(modal).to_have_count(0)


def test_a_toast_keeps_clear_of_the_open_drawers_buttons(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("staged-toggle").click()
    drawer = ui.page.get_by_test_id("staged-drawer")
    expect(drawer).to_be_visible()
    ui.node("left", "gdrive:研究/論文").click()
    ui.page.keyboard.press("Control+x")
    toast = ui.page.get_by_test_id("toast").filter(has_text="已剪下")
    expect(toast).to_be_visible()
    ui.page.wait_for_timeout(300)
    _assert_apart(drawer.locator(".drawer-foot").bounding_box(), toast.bounding_box())
    _assert_apart(drawer.bounding_box(), toast.bounding_box())
    ui.page.get_by_test_id("drawer-undo").click()                                # the buttons are reachable
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("0")


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


# --- the language follows the browser's order of preference: the first listed language we have wins, else English

ENGLISH_BROWSERS = [[], ["en-US"], ["en-US", "en"], ["fr-FR", "de"], ["ja-JP"], ["ko"], ["zha"], ["z"], ["xzh-TW"],
                    ["en-US", "zh-TW"], ["en", "zh-Hans"], ["fr", "EN-gb", "zh"]]     # English listed before any Chinese
CHINESE_BROWSERS = [["zh-TW"], ["zh-CN"], ["zh-Hant-HK", "en"], ["zh"], ["ZH-tw"], ["zh_TW"], ["fr", "zh-TW", "en"],
                    ["ja", "zh-TW", "en"], ["zha", "zh"]]       # the first supported entry decides; others are skipped


def _page_language(ui: Ui) -> dict:
    return ui.page.evaluate("""() => ({lang: document.documentElement.lang, title: document.title,
        toggle: document.querySelector('[data-testid="lang-toggle"]').textContent.trim(),
        preview: document.querySelector('[data-testid="preview-button"]').textContent.trim()})""")


@pytest.mark.parametrize("languages", ENGLISH_BROWSERS, ids=lambda x: ",".join(x) or "none")
def test_the_page_is_in_english_when_the_browser_prefers_english_or_lists_no_chinese(make_ui, languages):
    ui = make_ui(languages=languages, locale="en-US").open()
    assert _page_language(ui) == {"lang": "en", "title": "drive-merge workspace", "toggle": "中文", "preview": "Preview"}
    expect(ui.page.get_by_test_id("undo-button")).to_contain_text("Undo")
    assert not re.search(r"[㐀-鿿]", ui.page.get_by_test_id("pane-left").inner_text().replace("研究", "").replace("學校", ""))


@pytest.mark.parametrize("languages", CHINESE_BROWSERS, ids=lambda x: ",".join(x))
def test_the_page_is_in_traditional_chinese_when_the_browser_prefers_chinese_over_english(make_ui, languages):
    ui = make_ui(languages=languages, locale="en-US").open()
    assert _page_language(ui) == {"lang": "zh-Hant-TW", "title": "drive-merge 工作區", "toggle": "EN", "preview": "預覽"}


def test_a_browser_with_no_language_list_at_all_gets_english(make_ui):
    ui = make_ui(locale="en-US")
    ui.page.context.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => undefined});"
                                    "Object.defineProperty(navigator, 'language', {get: () => undefined});")
    assert _page_language(ui.open())["lang"] == "en"


def test_detect_lang_in_isolation(make_ui):
    ui = make_ui()
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    table = {"[]": "en", "undefined": "en", "null": "en", '"zh-TW"': "en", "[null,5]": "en", '["fr"]': "en",
             '["en"]': "en", '["en-US","zh-TW"]': "en", '["en","zh-Hans"]': "en", '["EN_gb","zh"]': "en",
             '["zh"]': "zh-TW", '["zh-CN","en"]': "zh-TW", '["ja","zh-TW","en"]': "zh-TW", '["ZH_tw"]': "zh-TW",
             '[" zh-TW "]': "zh-TW", '["fr","zh-Hant"]': "zh-TW", '[null,"zh"]': "zh-TW",
             '["zhx"]': "en", '["zha","zh-TW"]': "zh-TW", '["zha","en","zh"]': "en", '["ja","fr"]': "en"}
    for given, expected in table.items():
        assert ui.page.evaluate(f"import('/ui/static/i18n.js').then(m => m.detectLang({given}))") == expected, given


def test_the_language_the_person_picked_is_remembered_and_beats_the_browser(make_ui):
    english = make_ui(locale="en-US").open()
    english.page.get_by_test_id("lang-toggle").click()
    assert _page_language(english)["lang"] == "zh-Hant-TW"
    english.page.reload()
    expect(english.page.get_by_test_id("preview-button")).to_contain_text("預覽")      # remembered across a reload
    assert _page_language(english)["title"] == "drive-merge 工作區"
    assert english.page.evaluate("localStorage.getItem('dm-lang')") == "zh-TW"
    english.page.get_by_test_id("lang-toggle").click()
    english.page.reload()
    expect(english.page.get_by_test_id("preview-button")).to_contain_text("Preview")
    chinese = make_ui().open()
    chinese.page.get_by_test_id("lang-toggle").click()
    chinese.page.reload()
    expect(chinese.page.get_by_test_id("preview-button")).to_contain_text("Preview")      # English although the browser is zh-TW
    assert _page_language(chinese)["lang"] == "en"


def test_the_lang_query_beats_everything_and_an_unknown_value_is_ignored(make_ui):
    ui = make_ui(locale="en-US")
    ui.page.goto(f"{ui.base}/ui/?lang=zh-TW#token={MockApi.TOKEN}")
    expect(ui.page.get_by_test_id("preview-button")).to_contain_text("預覽")
    ui.page.goto(f"{ui.base}/ui/?lang=klingon#token={MockApi.TOKEN}")
    expect(ui.page.get_by_test_id("preview-button")).to_contain_text("Preview")


def test_without_usable_storage_the_page_still_picks_a_language_and_switches_it(make_ui):
    ui = make_ui(locale="en-US", block_storage=True).open()
    assert _page_language(ui)["lang"] == "en"
    ui.page.get_by_test_id("lang-toggle").click()                        # nothing to save to: it holds for this visit only
    expect(ui.page.get_by_test_id("preview-button")).to_contain_text("預覽")
    ui.page.get_by_test_id("lang-toggle").click()
    expect(ui.page.get_by_test_id("preview-button")).to_contain_text("Preview")
    chinese = make_ui(block_storage=True).open()
    assert _page_language(chinese)["lang"] == "zh-Hant-TW"                 # detection works with storage blocked too


def test_messages_the_server_words_follow_a_switch_of_language_without_a_reload(make_ui):
    """Warnings, refusals and violations arrive as a code plus params, so they read in the language of the moment."""
    ui = make_ui(locale="en-US").open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.mock.preview_mode = "violations"
    ui.page.get_by_test_id("preview-button").click()
    raw = ui.page.get_by_test_id("preview-modal").get_by_test_id("violation").first.get_by_test_id("problem-raw")
    expect(raw).to_contain_text("never overwritten")
    ui.page.keyboard.press("Escape")
    ui.page.get_by_test_id("lang-toggle").click()
    ui.page.get_by_test_id("preview-button").click()
    expect(ui.page.get_by_test_id("preview-modal").get_by_test_id("violation").first.get_by_test_id("problem-raw")
           ).to_contain_text("絕不覆寫")


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
    source = Path(drivemerge.workspace.__file__).read_text(encoding="utf-8")
    engine_warnings = set(re.findall(r'\bwarn\(\s*"([A-Z_]+)"', source))          # every staging warning the engine can give
    assert engine_warnings >= {"MERGE", "CROSS_REMOTE", "READ_ONLY", "CASE_COLLISION", "CASE_ONLY_RENAME"}, engine_warnings
    for code in engine_warnings:
        assert f"warn.{code}" in zh, code
    for code in ("SRC_NOT_FOUND DST_NOT_DIR INTO_ITSELF NAME_EXISTS NOOP BAD_NAME QUARANTINE").split():
        assert f"err.{code}" in zh, code
    # Every conflict code the engine can raise has a title and an explanation. The keys are built from the code
    # at run time, so the scan above cannot see them; the codes themselves are read from the engine's source.
    engine_codes = set(re.findall(r'\bconflict\([^,]+,\s*"([A-Z_]+)"', source))
    assert engine_codes == {c for c, _ in ENGINE_CONFLICTS}, engine_codes     # the mock lists what the engine really has
    for code in engine_codes:
        assert f"conflict.{code}.title" in zh and f"conflict.{code}.text" in zh, code
        assert re.search(r"[一-鿿]", dicts["zh-TW"][f"conflict.{code}.text"]), code
        # ... and the detail line under them, which used to be the engine's English sentence.
        assert re.search(r"[一-鿿]", dicts["zh-TW"][f"conflict.{code}.detail"]), code
        assert not re.search(r"[一-鿿]", dicts["en"][f"conflict.{code}.detail"]), code
    # ... and so does every reason a staged step can have gone stale for, and the quarantine folder.
    for code in ("SRC_NOT_FOUND", "DST_NOT_DIR"):
        assert f"stale.{code}" in zh, code
    for key in ("quarantine.label", "quarantine.tip", "quarantine.inside", "quarantine.cannotHere", "stale.badge"):
        assert key in zh, key
    # Every sentence of the simulator (code and variant), of a suggestion and of a refusal that the server can send as
    # a code plus params has its wording in both languages (tests/test_i18n_contract.py checks the same from the sources).
    for code, variant in simulator_messages():
        for lang in ("zh-TW", "en"):
            assert f"viol.{code}.detail" + (f".{variant}" if variant else "") in dicts[lang], (lang, code, variant)
    for code in ("RELATED", "DUPLICATES"):
        for part in ("title", "reason"):
            assert f"sug.{code}.{part}" in zh and f"sug.{code}.{part}" in en, (code, part)
    for code in ("BAD_REQUEST", "NOT_FOUND", "BAD_TOKEN", "JOB_BUSY", "STALE_PREVIEW", "NOT_APPROVED", "EXECUTION_DISABLED"):
        assert f"err.{code}" in zh and f"err.{code}" in en, code
    # The dictionaries the page really runs are the ones that test reads from the file.
    parsed = contract_dictionaries()
    assert set(parsed["zh-TW"]) == zh and set(parsed["en"]) == en


@pytest.mark.parametrize("lang, locale", [("en", "en-US"), ("zh-TW", "zh-TW")])
def test_screenshots_in_both_languages(make_ui, lang, locale):
    """i18n-<screen>-<lang>.png: the same screens from a browser that asks for English and one that asks for Chinese,
    for a human to look for untranslated or clipped text."""
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
    except OSError:
        pytest.skip("screenshot directory is not writable")
    ui = make_ui(quarantine=True, locale=locale).open()
    assert ui.page.evaluate("document.documentElement.lang") == ("en" if lang == "en" else "zh-Hant-TW")
    ui.expand_all()
    ui.expand("left", "gdrive:研究/論文")
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(ui.node("right", "dropbox:學校/資料")).to_be_visible()
    ui.node("right", "dropbox:學校").click()
    ui.page.get_by_test_id("mkdir-right").click()
    ui.page.get_by_test_id("dialog-input").fill("thesis" if lang == "en" else "碩士論文")
    ui.page.get_by_test_id("dialog-ok").click()
    ui.page.get_by_test_id("ghost-toggle-left").check()
    ui.page.get_by_test_id("staged-toggle").click()
    expect(ui.page.get_by_test_id("staged-item")).to_have_count(2)
    ui.page.wait_for_timeout(1800)
    ui.page.screenshot(path=str(SHOTS / f"i18n-main-{lang}.png"))

    ui.page.get_by_test_id("suggestions-toggle").click()
    expect(ui.page.get_by_test_id("suggestion-card")).to_be_visible()
    ui.page.screenshot(path=str(SHOTS / f"i18n-suggestions-{lang}.png"))

    ui.page.get_by_test_id("preview-button").click()
    expect(ui.page.get_by_test_id("approval-input")).to_be_visible()
    ui.page.screenshot(path=str(SHOTS / f"i18n-preview-{lang}.png"))
    ui.page.get_by_test_id("preview-close").click()

    ui.mock.preview_mode = "violations"
    ui.page.get_by_test_id("preview-button").click()
    expect(ui.page.get_by_test_id("violation").first).to_be_visible()
    ui.page.get_by_test_id("warning-list").locator("summary").click()
    ui.page.screenshot(path=str(SHOTS / f"i18n-violations-{lang}.png"))
    ui.page.get_by_test_id("preview-close").click()

    ui.mock.preview_mode = "conflicts"
    ui.page.get_by_test_id("preview-button").click()
    expect(ui.page.get_by_test_id("conflict").first).to_be_visible()
    ui.page.screenshot(path=str(SHOTS / f"i18n-conflicts-{lang}.png"))


def test_screenshots_for_a_visual_check(make_ui):
    """Writes PNGs of the main screen and the preview modal (light and dark) so a human can eyeball the layout."""
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
    except OSError:
        pytest.skip("screenshot directory is not writable")
    ui = make_ui(quarantine=True).open()
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

    # A rescan removed a source under the draft; the preview lists it; a toast arrives beside the open dialog.
    broken = make_ui(quarantine=True).open()
    broken.expand_all()
    broken.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    broken.drag(("left", "gdrive:學校/課程"), ("right", "dropbox:Archive"))
    broken.mock.vanish("gdrive", "研究/資料")
    expect(broken.page.get_by_test_id("staged-stale-count")).to_have_text("1", timeout=8000)
    broken.page.get_by_test_id("staged-toggle").click()
    expect(broken.page.get_by_test_id("staged-stale")).to_be_visible()
    broken.expand("left", f"gdrive:{QUARANTINE}")
    broken.page.wait_for_timeout(1800)
    broken.page.screenshot(path=str(SHOTS / "stale-draft.png"))
    broken.page.get_by_test_id("preview-button").click()
    expect(broken.page.get_by_test_id("stale-actions")).to_be_visible()
    broken.page.screenshot(path=str(SHOTS / "stale-preview.png"))
    broken.mock.external_action(created_by="mcp", items=[{"remote": "gdrive", "path": "研究/論文"}],
                                dst={"remote": "dropbox", "path": "Archive"})
    expect(broken.page.get_by_test_id("toast").filter(has_text="Claude")).to_be_visible(timeout=8000)
    broken.page.wait_for_timeout(500)
    broken.page.screenshot(path=str(SHOTS / "toast-over-preview.png"))
    broken.page.get_by_test_id("preview-close").click()
    broken.mock.preview_mode = "conflicts"
    broken.page.get_by_test_id("preview-button").click()
    expect(broken.page.get_by_test_id("conflict").first).to_be_visible()
    broken.page.screenshot(path=str(SHOTS / "preview-conflicts.png"))

    dark = make_ui(color_scheme="dark", width=1100, height=760, quarantine=True).open()
    dark.expand_all()
    dark.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(dark.node("right", "dropbox:學校/資料")).to_be_visible()
    dark.page.wait_for_timeout(1800)
    dark.page.screenshot(path=str(SHOTS / "main-dark-1100.png"))
    assert (SHOTS / "main.png").stat().st_size > 5_000


# --- follow-ups of the review (ux)

def test_a_suggestion_can_be_staged_again_after_undo(make_ui):
    """The card is "staged" while the draft holds its steps, not for as long as the drawer happens to stay open."""
    ui = make_ui().open()
    ui.page.get_by_test_id("suggestions-toggle").click()
    add = ui.page.get_by_test_id("suggestion-card").get_by_test_id("suggestion-add")
    expect(add).to_have_text("加入暫存")
    add.click()
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("1")
    expect(add).to_be_disabled()
    expect(add).to_have_text("已加入")
    ui.page.get_by_test_id("undo-button").click()
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("0")
    expect(add).to_be_enabled()
    expect(add).to_have_text("加入暫存")
    ui.page.get_by_test_id("redo-button").click()
    expect(add).to_be_disabled()
    ui.page.get_by_test_id("undo-button").click()
    add.click()
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("1")
    assert len(ui.mock.calls_to("POST", "/v1/drafts/1/actions")) == 2


def test_clicking_away_from_the_rename_box_keeps_the_typed_name(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    ui.node("left", "gdrive:研究/資料").click()
    ui.page.keyboard.press("F2")
    box = ui.page.get_by_test_id("rename-input")
    box.fill("調查資料")
    ui.node("left", "gdrive:Photos").click()  # not Enter: the person just clicks somewhere else
    expect(box).to_have_count(0)
    assert ui.mock.last("POST", "/v1/drafts/1/actions").body == {
        "kind": "move", "items": [{"remote": "gdrive", "path": "研究/資料"}],
        "dst": {"remote": "gdrive", "path": "研究"}, "new_name": "調查資料"}
    expect(ui.node("left", "gdrive:研究/調查資料")).to_be_visible()
    expect(ui.node("left", "gdrive:Photos")).to_have_attribute("aria-selected", "true")  # the click still did its own job


def test_leaving_the_rename_box_without_a_new_name_stages_nothing(make_ui):
    ui = make_ui().open()
    ui.expand_all()
    for how in ("escape", "unchanged", "empty"):
        ui.node("left", "gdrive:研究/資料").click()
        ui.page.keyboard.press("F2")
        box = ui.page.get_by_test_id("rename-input")
        if how == "escape":
            box.fill("not wanted")
            box.press("Escape")
        elif how == "unchanged":
            ui.node("left", "gdrive:Photos").click()
        else:
            box.fill("   ")
            ui.node("left", "gdrive:Photos").click()
        expect(box).to_have_count(0)
    assert not ui.mock.calls_to("POST", "/v1/drafts/1/actions")


def test_keyboard_focus_stays_in_the_tree_after_a_rename_or_a_cancelled_rename(make_ui):
    """The editor held the focus and the renamed row has a new key: without help the focus falls to <body> and the
    arrow keys, F2 and Ctrl+X need another mouse click."""
    ui = make_ui().open()
    ui.expand_all()
    ui.node("left", "gdrive:研究/資料").click()
    ui.page.keyboard.press("F2")
    ui.page.get_by_test_id("rename-input").press("Escape")
    expect(ui.node("left", "gdrive:研究/資料")).to_be_focused()
    ui.page.keyboard.press("F2")
    box = ui.page.get_by_test_id("rename-input")
    box.fill("調查資料")
    box.press("Enter")
    expect(ui.node("left", "gdrive:研究/調查資料")).to_be_visible()
    expect(ui.node("left", "gdrive:研究/調查資料")).to_be_focused()   # on the renamed row, not on <body>
    expect(ui.node("left", "gdrive:研究/調查資料")).to_have_attribute("aria-selected", "true")
    ui.page.keyboard.press("ArrowDown")                                 # and the keyboard works at once
    expect(ui.node("left", "gdrive:研究/論文")).to_be_focused()
    ui.page.keyboard.press("F2")
    expect(ui.page.get_by_test_id("rename-input")).to_be_visible()


def _english_suggestions(kind: str) -> list[dict]:
    """Suggestions with ASCII folder names, so that any Chinese in a card in the English UI would be the server's."""
    folders = [{"remote": "gdrive", "path": "Research/Thesis", "files": 4, "bytes": 2_000_000},
               {"remote": "dropbox", "path": "School/thesis-drafts", "files": 3, "bytes": 1_000_000}]
    if kind == "duplicates":
        return [{"id": "duplicates:aaa111", "kind": "duplicates", "score": 0.95, "folders": folders, "terms": ["thesis"],
                 "title": "These 2 folders hold almost the same content: Thesis, thesis-drafts",
                 "reason": "Content similarity 95% ...", "code": "DUPLICATES",
                 "params": {"count": 2, "names": ["Thesis", "thesis-drafts"], "kept": "gdrive:Research/Thesis",
                            "similarity": 0.95, "shared": 3, "saved_bytes": 1_048_576},
                 "actions": [{"kind": "move", "items": [{"remote": "dropbox", "path": "School/thesis-drafts"}],
                              "dst": {"remote": "gdrive", "path": "Research"}, "note": "duplicates:aaa111"}]}]
    return [{"id": "related:bbb222", "kind": "related", "score": 0.61, "folders": folders, "terms": ["thesis", "survey"],
             "title": "These 2 folders look related: Thesis, thesis-drafts", "reason": "their folder names share ...",
             "code": "RELATED", "params": {"count": 2, "names": ["Thesis", "thesis-drafts"], "anchor": "gdrive:Research/Thesis",
                                           "name_terms": ["thesis"], "content_terms": ["survey"], "same": 1},
             "actions": [{"kind": "move", "items": [{"remote": "dropbox", "path": "School/thesis-drafts"}],
                          "dst": {"remote": "gdrive", "path": "Research/Thesis"}, "note": "related:bbb222"}]}]


def test_suggestion_cards_are_worded_by_the_page_from_the_code_and_params(make_ui):
    """The server sends each suggestion's title and reason in English (for the CLI and MCP) and as a code plus
    params; the page words the card itself, in whichever language it is in."""
    ui = make_ui().open()
    ui.mock.suggestions = _english_suggestions
    cjk = re.compile(r"[㐀-鿿]")
    expect(ui.page.get_by_test_id("lang-toggle")).to_have_text("EN")                # Chinese first (the browser is zh-TW)
    ui.page.get_by_test_id("lang-toggle").click()
    ui.page.get_by_test_id("suggestions-toggle").click()
    card = ui.page.get_by_test_id("suggestion-card")
    expect(card).to_have_count(1)
    expect(card.locator(".sug-title")).to_have_text("These 2 folders look related: Thesis, thesis-drafts")
    expect(card.locator(".sug-reason")).to_have_text(
        'their folder names share "thesis"; file names inside often contain "survey"; 1 file inside has exactly the '
        'same content. Suggested: tidy them under "gdrive:Research/Thesis".')
    assert not cjk.search(card.inner_text()), card.inner_text()
    ui.page.get_by_test_id("sug-tab-duplicates").click()
    expect(card.locator(".sug-title")).to_have_text("These 2 folders hold almost the same content: Thesis, thesis-drafts")
    expect(card.locator(".sug-reason")).to_contain_text("Content similarity 95%")
    expect(card.locator(".sug-reason")).to_contain_text('into "gdrive:Research/Thesis"')
    expect(card.locator(".sug-reason")).to_contain_text("frees about 1.0 MB")
    assert not cjk.search(card.inner_text()), card.inner_text()
    ui.page.get_by_test_id("lang-toggle").click()  # and back to Chinese: the same facts, worded in Chinese
    expect(card.locator(".sug-title")).to_have_text("這 2 個資料夾內容幾乎相同：Thesis、thesis-drafts")
    expect(card.locator(".sug-reason")).to_contain_text("內容相同度 95%")
    ui.page.get_by_test_id("sug-tab-related").click()
    expect(card.locator(".sug-reason")).to_contain_text("資料夾名稱都有「thesis」；裡面的檔名常出現「survey」；其中有 1 份內容完全相同的檔案")


def test_a_suggestion_with_a_code_the_page_does_not_know_shows_the_servers_english(make_ui):
    """A newer server may send a kind of suggestion this page has no sentence for: its English text is shown as it
    is, never a blank card or a raw key."""
    ui = make_ui().open()
    ui.mock.suggestions = lambda kind: [{
        "id": "future:1", "kind": "related", "score": 0.5, "title": "These folders belong together", "reason": "Because.",
        "code": "FUTURE_KIND", "params": {"count": 2}, "terms": [], "actions": [],
        "folders": [{"remote": "gdrive", "path": "Research/Thesis", "files": 4, "bytes": 2_000_000},
                    {"remote": "dropbox", "path": "School/thesis-drafts", "files": 3, "bytes": 1_000_000}]}]
    ui.page.get_by_test_id("suggestions-toggle").click()
    card = ui.page.get_by_test_id("suggestion-card")
    expect(card.locator(".sug-title")).to_have_text("These folders belong together")
    expect(card.locator(".sug-reason")).to_have_text("Because.")


@pytest.mark.parametrize("mode", ["overlap", "overlap-legacy"])
def test_one_problem_is_counted_and_drawn_once_not_once_per_echo(make_ui, mode):
    """A clash both the simulator and the workspace report, a sentence repeated for every op, and the NO_WITNESS echo
    of a copy that a read-only cloud already prevents are one problem each. An unrelated NO_WITNESS stays. The
    places come from the violations' `params`, or from their English messages in a report stored without them."""
    ui = make_ui().open()
    ui.mock.preview_mode = mode
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "false")
    codes = [v.get_attribute("data-code") for v in modal.get_by_test_id("violation").all()]
    assert codes == ["READ_ONLY_REMOTE", "NO_WITNESS"], codes
    assert "gdrive:a.txt" in modal.get_by_test_id("violation").nth(1).inner_text()
    expect(modal.get_by_test_id("conflict")).to_have_count(1)
    expect(modal.get_by_test_id("sim-status")).to_contain_text("3 個問題")


def _run_to_done(ui: Ui) -> Any:
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    modal.get_by_test_id("approval-input").fill(MockApi.CODE)
    modal.get_by_test_id("approval-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", "done", timeout=10_000)
    return modal


def test_the_catalog_banner_outlives_the_finished_run_dialog_and_its_button_rescans(make_ui):
    ui = make_ui().open()
    expect(ui.page.get_by_test_id("catalog-banner")).to_have_count(0)
    modal = _run_to_done(ui)
    modal.get_by_test_id("preview-close").click()           # the obvious thing: no rescan
    banner = ui.page.get_by_test_id("catalog-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("重新掃描")
    expect(banner).to_contain_text("dropbox, gdrive")
    expect(ui.page.get_by_test_id("staged-count")).to_have_text("0")   # a fresh draft on the old catalog ...
    ui.page.wait_for_timeout(3500)                                     # ... and still the banner a poll later
    expect(banner).to_be_visible()
    banner.get_by_test_id("catalog-rescan").click()
    expect(banner).to_have_count(0)
    assert ui.mock.last("POST", "/v1/ui/rescan").body == {"remotes": ["dropbox", "gdrive"]}
    expect(ui.page.get_by_test_id("toast").filter(has_text="已重新掃描")).to_be_visible()


def test_a_rescan_from_the_finished_run_dialog_clears_the_banner_and_it_does_not_come_back(make_ui):
    ui = make_ui().open()
    modal = _run_to_done(ui)
    expect(ui.page.get_by_test_id("catalog-banner")).to_be_visible()   # behind the dialog already
    modal.get_by_test_id("rescan-button").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", "rescanned", timeout=10_000)
    expect(ui.page.get_by_test_id("catalog-banner")).to_have_count(0)
    modal.get_by_test_id("preview-close").click()
    ui.page.wait_for_timeout(3500)                                     # the poll finds the draft applied: nothing new
    expect(ui.page.get_by_test_id("catalog-banner")).to_have_count(0)
    assert len(ui.mock.calls_to("POST", "/v1/ui/rescan")) == 1


def test_a_preview_made_while_the_catalog_is_out_of_date_cannot_be_approved_until_a_rescan(make_ui):
    ui = make_ui().open()
    _run_to_done(ui).get_by_test_id("preview-close").click()
    ui.drag(("left", "gdrive:Photos"), ("right", "dropbox:Archive"))
    ui.page.get_by_test_id("preview-button").click()
    modal = ui.page.get_by_test_id("preview-modal")
    expect(modal.get_by_test_id("sim-status")).to_have_attribute("data-ok", "true")
    expect(modal.get_by_test_id("outdated-note")).to_contain_text("目錄資料已過期")
    expect(modal.get_by_test_id("approval-input")).to_have_count(0)
    expect(modal.get_by_test_id("approval-button")).to_have_count(0)
    modal.get_by_test_id("outdated-rescan").click()
    expect(modal.get_by_test_id("progress")).to_have_attribute("data-phase", "rescanned", timeout=10_000)
    expect(ui.page.get_by_test_id("catalog-banner")).to_have_count(0)
    modal.get_by_test_id("preview-close").click()
    ui.page.get_by_test_id("preview-button").click()                   # preview again: approval is offered
    expect(ui.page.get_by_test_id("approval-input")).to_be_visible()


def test_the_catalog_banner_can_be_dismissed(make_ui):
    ui = make_ui().open()
    _run_to_done(ui).get_by_test_id("preview-close").click()
    expect(ui.page.get_by_test_id("catalog-banner")).to_be_visible()
    ui.page.get_by_test_id("catalog-dismiss").click()                  # "I already rescanned in a terminal"
    expect(ui.page.get_by_test_id("catalog-banner")).to_have_count(0)
    assert not ui.mock.calls_to("POST", "/v1/ui/rescan")


def test_the_banner_comes_back_after_a_reload_while_the_server_says_the_catalog_is_behind(make_ui):
    """It used to live in page memory only: a reload (or a second tab) forgot that a run had changed the clouds."""
    ui = make_ui().open()
    ui.mock.outdated = ["dropbox"]
    ui.page.goto(f"{ui.base}/ui/")                                      # the cookie and the CSRF token carry over
    banner = ui.page.get_by_test_id("catalog-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("dropbox")
    expect(banner).not_to_contain_text("gdrive")
    banner.get_by_test_id("catalog-rescan").click()
    expect(banner).to_have_count(0)
    assert ui.mock.last("POST", "/v1/ui/rescan").body == {"remotes": ["dropbox"]}
    ui.mock.outdated = []                                               # the scan finished: the server stops saying so
    ui.page.goto(f"{ui.base}/ui/")
    expect(ui.page.get_by_test_id("pane-left")).to_be_visible()
    ui.page.wait_for_timeout(300)
    expect(ui.page.get_by_test_id("catalog-banner")).to_have_count(0)


def test_a_draft_applied_from_a_terminal_leaves_the_banner_with_the_command_to_run(make_ui):
    """Proposal-only page: the browser cannot rescan (that is the human channel), so the banner names the command."""
    ui = make_ui(approvals_enabled=False).open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    ui.mock.draft["status"] = "applied"                                 # `drivemerge plan execute` finished in a terminal
    banner = ui.page.get_by_test_id("catalog-banner")
    expect(banner).to_be_visible(timeout=8000)
    expect(banner).to_contain_text("drivemerge scan dropbox")
    expect(banner).to_contain_text("drivemerge scan gdrive")
    expect(banner.get_by_test_id("catalog-rescan")).to_have_count(0)


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
