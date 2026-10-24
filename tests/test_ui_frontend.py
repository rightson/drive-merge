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
from urllib.parse import parse_qs, unquote, urlparse

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


# The catalog as the Explore and Overview views see it: one entry per content, with every live copy.
CATALOG = [
    {"key": "g:1", "name": "thesis_final.pdf", "size": 4_212_331, "category": "pdf", "ext": "pdf", "mtime": "2023-06-01T10:00:00Z",
     "verified": True, "copies": [("gdrive", "研究/論文/thesis_final.pdf"), ("dropbox", "學校/thesis_final.pdf"),
                                  ("dropbox", "Archive/thesis-v9.pdf")]},
    {"key": "g:2", "name": "survey.csv", "size": 120_000, "category": "spreadsheet", "ext": "csv", "mtime": "2022-11-12T08:00:00Z",
     "verified": True, "copies": [("gdrive", "研究/資料/survey.csv")]},
    {"key": "g:3", "name": "trip.jpg", "size": 3_000_000, "category": "photo", "ext": "jpg", "mtime": "2021-08-01T09:00:00Z",
     "verified": True, "copies": [("gdrive", "Photos/trip.jpg")]},
    {"key": "g:4", "name": "old.zip", "size": 9_000_000, "category": "archive", "ext": "zip", "mtime": "2019-01-01T00:00:00Z",
     "verified": True, "copies": [("dropbox", "Archive/old.zip")]},
    {"key": "g:5", "name": "report.pdf", "size": 52_000, "category": "pdf", "ext": "pdf", "mtime": "2024-02-01T12:00:00Z",
     "verified": True, "copies": [("gdrive", "學校/report.pdf"), ("dropbox", "學校/report.pdf")]},
    {"key": "e:9", "name": "notes.txt", "size": 3_000, "category": "document", "ext": "txt", "mtime": "2024-03-01T10:00:00Z",
     "verified": False, "copies": [("gdrive", "研究/資料/notes.txt")]},
]


# Duplicate groups as Service.estimate_plan sees them: every live copy with the reason it could not be kept, if any.
def _copy(entry_id: int, remote: str, path: str, why_not: str | None = None) -> dict:
    return {"entry_id": entry_id, "remote": remote, "path": path, "mtime": "2023-06-01T10:00:00Z", "why_not": why_not}


DUP_GROUPS = [
    {"key": "g:11", "size": 4_100_000, "copies": [_copy(201, "gdrive", "Photos/2019/IMG_0412.jpg"),
                                                   _copy(202, "gdrive", "Backup/IMG_0412 (1).jpg"),
                                                   _copy(203, "dropbox", "Photos/IMG_0412.jpg")]},
    {"key": "g:12", "size": 52_000, "copies": [_copy(204, "gdrive", "學校/report.pdf"),
                                                _copy(205, "gdrive", "Backup/report.pdf", "AMBIGUOUS_PATH"),
                                                _copy(206, "gdrive", "Old/report.pdf")]},
    {"key": "g:13", "size": 9_000_000, "copies": [_copy(207, "dropbox", "Archive/old.zip"), _copy(208, "dropbox", "Archive/old (copy).zip"),
                                                   _copy(209, "dropbox", f"{QUARANTINE}/3/old.zip", "QUARANTINE")]},
]


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
        self.catalog = copy.deepcopy(CATALOG)
        self.coverage_override: dict | None = None   # a canned /v1/coverage answer
        self.compare_limit: int | None = None        # files per side above which /v1/compare refuses
        self.unproven: set[str] = set()              # relative paths /v1/compare reports as `unverified`
        self.unique_names: set[str] = {"figure3.png"}  # one-side files whose content exists nowhere else
        self.explore_fail: tuple[str, dict] | None = None  # (code, params): /v1/explore refuses with a 400
        self.session_open = False
        self.remotes = copy.deepcopy(REMOTES)       # what /v1/ui/config reports (a test may add a read-only cloud)
        self.plans: dict[int, dict] = {}            # the Plans view: summaries plus each plan's ops and simulation
        self.next_plan_id = 20
        self.dup_groups = copy.deepcopy(DUP_GROUPS)
        self.estimate_delay: Any = None             # (n) -> seconds a canned estimate answer is held back (see delay_estimate)
        self.bridge_candidates = {"count": 3, "bytes": 2_400_000}
        self.bridge_polls = 0
        self.apply_draft_on_done = True
        self.setup: dict | None = None              # what /v1/ui/setup reports; None = a server that does not know the route
        self.connect_view: dict | None = None       # the connect job as GET /v1/jobs/setup-1 shows it
        self.connect_next: list[dict] = []          # the views that follow, one per answer
        self.connect_refusal: tuple[int, str, dict] | None = None   # status, code, params of POST /v1/ui/setup/connect
        self.test_results: dict[str, dict] = {}     # POST /v1/ui/setup/test, by remote name (default {"ok": True})
        self.enable_response: tuple[int, dict] = (200, {"remote": {"name": "dropbox", "write": "dropbox:"}})
        self.scan_script: list[tuple[dict, Any]] = []   # (job view, effect run when it is served) for the scan job
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

    # ------------------------------------------------------------------ Setup (docs/SETUP.md section D)
    def make_setup(self, *, clouds: list[tuple[str, str]] | None = None, scanned: bool = True, rclone_found: bool = True,
                   loose: list[tuple[str, str]] | None = None, execution_enabled: bool = False, version: str = "v1.71.1") -> dict:
        """Report this state from /v1/ui/setup: `clouds` are (name, provider) pairs, `loose` rclone remotes not yet added."""
        remotes = []
        for name, provider in clouds or []:
            remotes.append({"name": name, "provider": provider, "read": f"{name}:", "write": None, "writable": False,
                            "last_scan": {"id": 3, "status": "complete", "finished_at": "2024-03-01T10:00:00Z"} if scanned else None,
                            "files": 41203 if scanned else 0, "bytes": 12_345_678_901 if scanned else 0})
        self.setup = {
            "config_path": "/home/me/.config/drive-merge/config.toml",
            "rclone": {"found": rclone_found, "version": version if rclone_found else None, "ok": rclone_found, "binary": "rclone"},
            "remotes": remotes,
            "rclone_remotes": [{"name": r["name"], "type": r["provider"], "provider": r["provider"], "in_config": True} for r in remotes]
            + [{"name": n, "type": ty, "provider": ty, "in_config": False} for n, ty in loose or []],
            "scanned": bool(remotes) and scanned, "identity_built": bool(remotes) and scanned, "execution_enabled": execution_enabled,
        }
        return self.setup

    def setup_json(self) -> dict:
        s = self.setup or {}
        return {**s, "needed": not s.get("remotes") or not s.get("scanned")}

    def setup_routes(self, method: str, path: str, body: Any, headers: dict) -> tuple[int, Any] | None:
        if self.setup is None:
            return None
        if (method, path) == ("GET", "/v1/ui/setup"):
            return 200, self.setup_json()
        if (method, path) == ("POST", "/v1/ui/setup/test"):
            return 200, self.test_results.get((body or {}).get("name"), {"ok": True})
        human = re.fullmatch(r"/v1/ui/setup/(connect|adopt|enable-write|connect/[\w-]+/answer|connect/[\w-]+/cancel)", path)
        if method != "POST" or not human:
            return None
        if headers.get("x-csrf-token") != self.CSRF:
            return 403, {"error": "bad csrf token", "code": "BAD_CSRF"}
        if path == "/v1/ui/setup/connect":
            if self.connect_refusal:
                status, code, params = self.connect_refusal
                return status, {"error": code, "code": code, "params": params}
            return 202, {"job_id": "setup-1"}
        if path == "/v1/ui/setup/adopt":
            name = (body or {}).get("name")
            loose = next((r for r in self.setup["rclone_remotes"] if r["name"] == name and not r["in_config"]), None)
            if loose is None:
                return 404, {"error": "no such remote", "code": "REMOTE_NOT_FOUND", "params": {"name": name}}
            loose["in_config"] = True
            remote = {"name": name, "provider": loose["provider"], "read": f"{name}:", "write": None, "writable": False,
                      "last_scan": None, "files": 0, "bytes": 0}
            self.setup["remotes"].append(remote)
            self.setup["scanned"] = False
            return 200, {"remote": remote}
        if path == "/v1/ui/setup/enable-write":
            return self.enable_response
        if path.endswith("/answer"):
            if not self.connect_view or self.connect_view["progress"].get("stage") != "question":
                return 409, {"error": "not waiting", "code": "NOT_WAITING"}
            self.connect_view = self.connect_next.pop(0) if self.connect_next else self._connect_done()
            return 200, {"ok": True}
        self.connect_view = {"id": "setup-1", "kind": "connect", "status": "failed", "progress": {}, "result": None,
                             "error": "cancelled", "code": "CANCELLED", "params": {"name": "x"}}
        return 200, {"ok": True}

    def _connect_done(self) -> dict:
        return {"id": "setup-1", "kind": "connect", "status": "done", "progress": {}, "error": None,
                "result": {"remote": {"name": "dropbox", "provider": "dropbox", "read": "dropbox:", "write": None}}}

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
                         "remotes": self.remotes, "version": "0.1.0", "csrf": self.CSRF, "outdated": self.outdated}
        if (method, path) == ("GET", "/v1/setup"):       # docs/ONBOARDING.md section D (tests/test_ui_onboarding.py mocks it in depth)
            return 200, {"first_run": False, "config_path": "/home/u/.config/drive-merge/config.toml",
                         "rclone": {"found": True, "path": "/usr/bin/rclone", "version": "1.75.0", "ok": True, "managed": False},
                         "connections": [{"name": r["name"], "provider": r["provider"], "account": f"me@{r['name']}.example",
                                          "read": True, "write": r["writable"], "writable": r["writable"], "created_at": "2024-01-01T00:00:00Z"}
                                         for r in self.remotes],
                         "providers": [{"key": "drive", "label": "Google Drive", "needs_client": True},
                                       {"key": "onedrive", "label": "OneDrive", "needs_client": False},
                                       {"key": "dropbox", "label": "Dropbox", "needs_client": False}]}
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
        if (method, path) == ("GET", "/v1/coverage"):
            return 200, self.coverage(q)
        if (method, path) == ("GET", "/v1/explore"):
            return self.explore(q)
        if (method, path) == ("GET", "/v1/compare"):
            return self.compare(q)
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
            self.apply_draft_on_done = m.group(1) == "7"       # a plan of the Plans view does not close the draft
            if m.group(1) != "7" and int(m.group(1)) in self.plans:
                self.plans[int(m.group(1))]["status"] = "approved"
            return 202, {"job_id": "job-1"}
        setup = self.setup_routes(method, path, body, headers)
        if setup is not None:
            return setup
        if (method, path) == ("POST", "/v1/ui/rescan"):
            return 202, {"job_id": "scan-1" if self.scan_script else "job-2"}
        if (method, path) == ("GET", "/v1/jobs/setup-1") and self.connect_view is not None:
            return 200, self.connect_view
        if (method, path) == ("GET", "/v1/jobs/scan-1") and self.scan_script:
            view, effect = self.scan_script[0] if len(self.scan_script) == 1 else self.scan_script.pop(0)
            if effect:
                effect()
            return 200, view
        m = re.fullmatch(r"/v1/jobs/(job-\d)", path)
        if m:
            if m.group(1) == "job-3":
                self.bridge_polls += 1
                done = self.bridge_polls >= 3
                return 200, {"id": "job-3", "kind": "bridge", "status": "done" if done else "running",
                             "progress": {"bridged": min(3, self.bridge_polls), "failed": 0, "total": 3},
                             "result": {"bridged": 3, "failed": [], "groups": {}} if done else None, "error": None}
            if m.group(1) == "job-2":
                return 200, {"id": "job-2", "kind": "rescan", "status": "done", "progress": {}, "result": {}, "error": None}
            self.job_polls += 1
            done = self.job_polls >= 3
            if done and self.apply_draft_on_done:
                self.draft["status"] = "applied"
            return 200, {"id": "job-1", "kind": "execute", "status": "done" if done else "running", "progress": {},
                         "result": {"status": "executed"} if done else None, "error": None}
        if re.fullmatch(r"/v1/plans/\d+/progress", path):
            n = min(4, self.job_polls * 2)
            return 200, {"status": "executed" if n == 4 else "running", "ops_total": 4, "done": n, "failed": 0, "skipped": 0,
                         "current": None if n == 4 else {"op": "copy", "src": {"remote": "gdrive", "path": "研究/資料/survey.csv"},
                                                         "dst": {"remote": "dropbox", "path": "學校/資料/survey.csv"}},
                         "recent_journal": []}
        planned = self.plan_routes(method, path, q, body)
        if planned is not None:
            return planned
        return 404, {"error": f"unmocked {method} {path}"}


    # ------------------------------------------------------------------ Plans (docs/PLANS.md section C)
    def add_plan(self, *, kind: str = "dedupe", status: str = "simulated", created_by: str = "api", ok: bool | None = True,
                 problems: int = 0, ops: int = 4, params: dict | None = None, title: str | None = None, outdated: bool = False,
                 violations: list | None = None, draft_id: int | None = None, op_rows: list | None = None) -> int:
        pid = self.next_plan_id
        self.next_plan_id += 1
        rows = op_rows if op_rows is not None else [
            {"seq": i + 1, "op": "delete", "src_remote_id": 1, "src_path": f"Backup/copy {i}.jpg", "src_entry_id": 300 + i,
             "witness_remote_id": 1, "witness_path": f"Photos/copy {i}.jpg", "expect_size": 1_000_000, "reason": "duplicate of gdrive:Photos"}
            for i in range(ops)]
        self.plans[pid] = {
            "id": pid, "kind": kind, "title": title or f"{kind} test", "status": status, "created_by": created_by,
            "created_at": "2024-03-01T10:00:00Z", "simulated_at": "2024-03-01T10:00:05Z" if ok is not None else None,
            "approved_at": None, "finished_at": "2024-03-01T11:00:00Z" if status in ("done", "failed", "cancelled") else None,
            "ops": ops, "ok": ok, "problems": problems, "params": params or {}, "draft_id": draft_id, "outdated": outdated,
            "_ops": rows, "_violations": violations or []}
        return pid

    def summaries(self, q: dict) -> list[dict]:
        rows = [{k: v for k, v in p.items() if not k.startswith("_")} for p in sorted(self.plans.values(), key=lambda p: -p["id"])]
        return [r for r in rows if not q.get("status") or r["status"] == q["status"]][: int(q.get("limit", 100))]

    def simulation(self, p: dict) -> dict:
        violations = p["_violations"]
        deletes = sum(1 for o in p["_ops"] if o["op"] == "delete")
        return {"plan_id": p["id"], "ok": not violations and p["ok"] is not False, "violations": violations, "warnings": [],
                "violations_total": len(violations),
                "remotes": [{"remote_id": 1, "remote": "gdrive", "files_before": 6 + deletes, "bytes_before": 6_603_000 + deletes * 1_000_000, "files_after": 6,
                             "bytes_after": 6_603_000, "bytes_in": 0, "files_quarantined": deletes,
                             "bytes_quarantined": deletes * 1_000_000}],
                "contents_before": 9, "contents_after": 9, "contents_lost": 0, "bytes_cross_remote": p.get("_cross", 0),
                "op_counts": {k: sum(1 for o in p["_ops"] if o["op"] == k) for k in ("copy", "move", "delete") if any(o["op"] == k for o in p["_ops"])},
                "sample": []}

    def plan_json(self, p: dict, limit: int = 200) -> dict:
        d = {k: v for k, v in p.items() if not k.startswith("_") and k not in ("ok", "problems", "outdated", "ops")}
        d["ops_total"] = len(p["_ops"])
        d["ops"] = p["_ops"][:limit]
        d["simulation"] = self.simulation(p) if p["ok"] is not None else None
        return d

    def plan_routes(self, method: str, path: str, q: dict, body: Any) -> tuple[int, Any] | None:
        if (method, path) == ("GET", "/v1/plans/summaries"):
            return 200, {"plans": self.summaries(q)}
        if (method, path) == ("POST", "/v1/plans/estimate"):
            return self.estimate(body["kind"], body.get("params", {}))
        if (method, path) == ("POST", "/v1/plans"):
            status, est = self.estimate(body["kind"], body.get("params", {}))
            if status != 200:
                return status, est
            if not est["ops"]:
                return 200, {"plan_id": None, "ops": 0, "message": "nothing to do", "code": "NOTHING_TO_DO", "skipped": [], "stats": est["stats"]}
            params = {k: v for k, v in body["params"].items() if k not in ("group_offset", "group_limit")}
            pid = self.add_plan(kind=body["kind"], created_by="ui", ops=est["ops"], params=params, status="simulated")
            return 200, {"plan_id": pid, "ops": est["ops"], "status": "simulated", "simulation": {"ok": True, "violations": []}}
        m = re.fullmatch(r"/v1/plans/(\d+)", path)
        if m and method == "GET":
            p = self.plans.get(int(m.group(1)))
            return (200, self.plan_json(p)) if p else (404, {"error": "no plan", "code": "NOT_FOUND"})
        m = re.fullmatch(r"/v1/ui/plans/(\d+)/(review|cancel)", path)
        if m and method == "POST":
            p = self.plans.get(int(m.group(1)))
            if p is None:
                return 404, {"error": "no plan", "code": "NOT_FOUND"}
            if m.group(2) == "review":
                if p["status"] not in ("draft", "simulated", "approved"):
                    return 409, {"error": "closed", "code": "PLAN_CLOSED", "params": {"plan": p["id"], "plan_status": p["status"]}}
                p["ok"] = not p["_violations"]
                p["problems"] = len(p["_violations"])
                d = self.plan_json(p)
                if self.approvals_enabled and d["simulation"]["ok"]:
                    d["approval_code"] = self.CODE
                return 200, d
            if p["status"] in ("done", "executing"):
                return 409, {"error": "closed", "code": "PLAN_CLOSED", "params": {"plan": p["id"], "plan_status": p["status"]}}
            p["status"] = "cancelled"
            return 200, {"ok": True}
        if (method, path) == ("GET", "/v1/ui/bridge/candidates"):
            return 200, self.bridge_candidates
        if (method, path) == ("POST", "/v1/ui/bridge"):
            self.bridge_polls = 0
            return 202, {"job_id": "job-3"}
        return None

    def estimate(self, kind: str, params: dict) -> tuple[int, dict]:
        if self.estimate_delay is not None:
            self.estimate_delay(params)
        order = self._remote_names()
        for key in ("remote", "target"):
            name = params.get(key)
            if name and name not in [r["name"] for r in self.remotes]:
                return 400, {"error": f"unknown remote {name!r}", "code": "UNKNOWN_REMOTE", "params": {"remote": name}}
        writable = {r["name"] for r in self.remotes if r.get("writable", True)}
        return self._dedupe(params) if kind == "dedupe" else self._consolidate(params, order, writable)

    def _dedupe(self, q: dict) -> tuple[int, dict]:
        prefix = (q.get("path_prefix") or "").strip("/")
        keep, skip = q.get("keep") or {}, set(q.get("skip_groups") or [])
        across = bool(q.get("cross_remote"))
        prefer = q.get("prefer_remotes") or []
        shown, ops, reclaimed, skipped, by_reason = [], 0, 0, [], {}
        keys = {g["key"] for g in self.dup_groups}
        for key in skip:
            if key not in keys:
                return 400, {"error": f"{key} is not a duplicate group", "code": "BAD_GROUP", "params": {"group": key}}
        for g in self.dup_groups:
            if q.get("remote") and not any(c["remote"] == q["remote"] for c in g["copies"]):
                continue
            if g["size"] < (q.get("min_size") or 1):
                continue
            pools: dict[str, list[dict]] = {}
            for c in g["copies"]:
                if q.get("remote") and not across and c["remote"] != q["remote"]:
                    continue
                pools.setdefault("*" if across else c["remote"], []).append(c)
            keepers: set[int] = set()
            removed: set[int] = set()
            for pool in pools.values():
                usable = [c for c in pool if c["why_not"] is None]
                forced = keep.get(g["key"])
                if forced is not None and forced in [c["entry_id"] for c in g["copies"]]:
                    if forced not in [c["entry_id"] for c in usable]:
                        if any(c["entry_id"] == forced for c in pool):
                            return 400, {"error": "cannot be kept", "code": "BAD_KEEPER", "params": {"group": g["key"], "entry_id": forced}}
                        forced_here = None
                    else:
                        forced_here = forced
                elif forced is not None:
                    return 400, {"error": "cannot be kept", "code": "BAD_KEEPER", "params": {"group": g["key"], "entry_id": forced}}
                else:
                    forced_here = None
                if len(usable) < 2:
                    keepers.update(c["entry_id"] for c in usable)
                    continue
                best = next((c for c in usable if c["entry_id"] == forced_here), None) or sorted(
                    usable, key=lambda c: (prefer.index(c["remote"]) if c["remote"] in prefer else 99, c["entry_id"]))[0]
                keepers.add(best["entry_id"])
                for c in usable:
                    if c is not best and (not prefix or c["path"].startswith(prefix + "/")):
                        removed.add(c["entry_id"])
            for c in g["copies"]:
                if c["why_not"] in ("AMBIGUOUS_PATH",):
                    skipped.append({"entry_id": c["entry_id"], "location": f"{c['remote']}:{c['path']}", "code": c["why_not"], "reason": "ambiguous path"})
                    by_reason[c["why_not"]] = by_reason.get(c["why_not"], 0) + 1
            if not removed and g["key"] not in skip:
                continue
            is_skipped = g["key"] in skip
            reclaim = 0 if is_skipped else g["size"] * len(removed)
            shown.append({"key": g["key"], "size": g["size"], "skipped": is_skipped, "reclaim": reclaim,
                          "copies": [{"entry_id": c["entry_id"], "remote": c["remote"], "path": c["path"], "mtime": c["mtime"],
                                      "keep": (not is_skipped) and c["entry_id"] in keepers, "remove": (not is_skipped) and c["entry_id"] in removed,
                                      "usable": c["why_not"] is None, "why_not": c["why_not"]} for c in g["copies"]]})
            if not is_skipped:
                ops += len(removed)
                reclaimed += reclaim
        offset, limit = int(q.get("group_offset", 0)), int(q.get("group_limit", 50))
        stats = {"groups_examined": len(shown), "groups": sum(1 for g in shown if not g["skipped"]), "deletes": ops,
                 "bytes_reclaimed": reclaimed, "skipped": len(skipped), "copies_considered": sum(len(g["copies"]) for g in shown),
                 "skipped_by_reason": by_reason}
        return 200, {"kind": "dedupe", "ops": ops, "stats": stats, "skipped": skipped, "sample": [], "nothing_to_do": ops == 0,
                     "groups": shown[offset:offset + limit]}

    def _consolidate(self, q: dict, order: list[str], writable: set[str]) -> tuple[int, dict]:
        target = q["target"]
        if target not in writable:
            return 400, {"error": f"target {target!r} is read-only", "code": "READ_ONLY", "params": {"remote": target}}
        sources = q.get("sources") or [r for r in order if r != target]
        root = (q.get("target_root") or "").strip("/")
        already = to_copy = bytes_copy = cross = deletes = 0
        sample, skipped, by_reason = [], [], {}
        for c in self.catalog:
            where = {r for r, _ in c["copies"]}
            if target in where:
                already += 1
            elif where & set(sources):
                if not c["verified"]:
                    skipped.append({"entry_id": 1, "location": f"{c['copies'][0][0]}:{c['copies'][0][1]}", "code": "PROBABLE_ONLY", "reason": "x"})
                    by_reason["PROBABLE_ONLY"] = by_reason.get("PROBABLE_ONLY", 0) + 1
                    continue
                to_copy += 1
                bytes_copy += c["size"]
                src_remote, src_path = c["copies"][0]
                cross += c["size"]
                folder = {"pdf": "Documents", "photo": "Photos", "spreadsheet": "Documents", "archive": "Archives"}.get(c["category"], "Other")
                sample.append({"op": "copy", "src": {"remote": src_remote, "path": src_path},
                               "dst": {"remote": target, "path": "/".join(filter(None, [root, folder, c["name"]]))}, "size": c["size"], "reason": ""})
            if q.get("delete_sources"):
                deletes += sum(1 for r, _ in c["copies"] if r in sources)
        stats = {"contents": already + to_copy, "already_on_target": already, "to_copy": to_copy, "bytes_to_copy": bytes_copy,
                 "bytes_cross_remote": cross, "moves": 0, "moves_on_target": 0, "deletes": deletes, "skipped": len(skipped),
                 "skipped_by_reason": by_reason}
        ops = to_copy + deletes
        return 200, {"kind": "consolidate", "ops": ops, "stats": stats, "skipped": skipped, "sample": sample[:50], "nothing_to_do": ops == 0}

    # ------------------------------------------------------------------ Overview, Explore and Compare (docs/EXPLORE.md)
    @staticmethod
    def _remote_names() -> list[str]:
        return [r["name"] for r in REMOTES]

    def coverage(self, q: dict) -> dict:
        if self.coverage_override is not None:
            return self.coverage_override
        order = self._remote_names()
        items = [c for c in self.catalog if c["size"] > 0 and (not q.get("category") or c["category"] == q["category"])]
        sets: dict[tuple, dict] = {}
        for c in items:
            names = tuple(sorted({r for r, _ in c["copies"]}, key=order.index))
            b = sets.setdefault(names, {"remotes": list(names), "contents": 0, "bytes": 0, "copies": 0, "stored_bytes": 0})
            b["contents"] += 1
            b["bytes"] += c["size"]
            b["copies"] += len(c["copies"])
            b["stored_bytes"] += c["size"] * len(c["copies"])
        bars = sorted(sets.values(), key=lambda b: (-b["bytes"], -b["contents"], b["remotes"]))
        remotes = []
        for r in REMOTES:
            mine = [c for c in items if any(x == r["name"] for x, _ in c["copies"])]
            only = [c for c in mine if {x for x, _ in c["copies"]} == {r["name"]}]
            remotes.append({
                "name": r["name"], "provider": r["provider"], "writable": r["writable"],
                "files": sum(1 for c in mine for x, _ in c["copies"] if x == r["name"]),
                "bytes": sum(c["size"] for c in mine for x, _ in c["copies"] if x == r["name"]),
                "only_here": {"contents": len(only), "bytes": sum(c["size"] for c in only)},
                "unverified_files": sum(1 for c in mine if not c["verified"]),
                "last_scan": {"id": 3, "status": "done", "finished_at": "2024-03-01T10:00:00Z"}})
        total = sum(c["size"] for c in items)
        stored = sum(c["size"] * len(c["copies"]) for c in items)
        return {"remotes": remotes,
                "totals": {"contents": len(items), "bytes": total, "stored_bytes": stored, "redundant_bytes": stored - total,
                           "copies": sum(len(c["copies"]) for c in items)},
                "intersections": bars, "other": None}

    def explore(self, q: dict) -> tuple[int, dict]:
        order = self._remote_names()
        if self.explore_fail:
            code, params = self.explore_fail
            return 400, {"error": "the parameter is not valid", "code": code, "params": params}

        def lst(key: str) -> list[str]:
            return [x for x in q.get(key, "").split(",") if x]

        for key in ("in", "on", "not_on", "only_on"):
            for name in ([q[key]] if key == "in" and key in q else lst(key)):
                if name not in order:
                    return 404, {"error": f"no remote {name}", "code": "NOT_FOUND", "params": {"remote": name}}
        if q.get("min_size") and not q["min_size"].isdigit():
            return 400, {"error": "bad min_size", "code": "BAD_QUERY", "params": {"param": "min_size"}}
        words = q.get("q", "").lower().split()
        on, not_on, only_on = lst("on"), lst("not_on"), lst("only_on")
        found = []
        for c in self.catalog:
            remotes = {r for r, _ in c["copies"]}
            hits = []
            for r, path in c["copies"]:
                text = (path + " " + path.rpartition("/")[2]).lower()
                if not all(w in text for w in words):
                    continue
                if q.get("in") and r != q["in"]:
                    continue
                if q.get("path_prefix") and not (path.startswith(q["path_prefix"] + "/") or path == q["path_prefix"]):
                    continue
                hits.append((r, path))
            if not hits:
                continue
            if q.get("category") and c["category"] != q["category"]:
                continue
            if q.get("ext") and c["ext"] != q["ext"]:
                continue
            if q.get("min_size") and c["size"] < int(q["min_size"]):
                continue
            if q.get("max_size") and c["size"] > int(q["max_size"]):
                continue
            if q.get("modified_after") and c["mtime"] < q["modified_after"]:
                continue
            if q.get("modified_before") and c["mtime"] >= q["modified_before"]:
                continue
            if any(r not in remotes for r in on) or any(r in remotes for r in not_on):
                continue
            if only_on and remotes != set(only_on):
                continue
            n = len(c["copies"])
            if q.get("copies_min") and n < int(q["copies_min"]):
                continue
            if q.get("copies_max") and n > int(q["copies_max"]):
                continue
            found.append((c, set(hits)))
        sort = q.get("sort", "relevance")
        keyf = {"size": lambda t: (-t[0]["size"], t[0]["name"]), "mtime": lambda t: (t[0]["mtime"], t[0]["name"]),
                "copies": lambda t: (-len(t[0]["copies"]), t[0]["name"])}.get(sort, lambda t: (t[0]["name"], t[0]["key"]))
        found.sort(key=keyf, reverse=sort == "mtime")
        limit, offset = int(q.get("limit", 50)), int(q.get("offset", 0))
        cats: dict[str, int] = {}
        exts: dict[str, int] = {}
        per_remote: dict[str, int] = {}
        copies = {"1": 0, "2": 0, "3+": 0}
        for c, _ in found:
            cats[c["category"]] = cats.get(c["category"], 0) + 1
            exts[c["ext"]] = exts.get(c["ext"], 0) + 1
            for r in {r for r, _ in c["copies"]}:
                per_remote[r] = per_remote.get(r, 0) + 1
            copies["1" if len(c["copies"]) == 1 else "2" if len(c["copies"]) == 2 else "3+"] += 1
        results = [{"key": c["key"], "name": c["name"], "size": c["size"], "category": c["category"], "ext": c["ext"],
                    "mtime": c["mtime"], "verified": c["verified"], "remotes": sorted({r for r, _ in c["copies"]}, key=order.index),
                    "copies_count": len(c["copies"]),
                    "copies": [{"entry_id": 100 + i, "remote": r, "path": p, "name": p.rpartition("/")[2], "mtime": c["mtime"],
                                "matched": (r, p) in hits} for i, (r, p) in enumerate(c["copies"])]}
                   for c, hits in found[offset:offset + limit]]
        return 200, {"total": len(found), "truncated": False, "results": results,
                     "facets": {"remotes": [{"name": n, "contents": per_remote[n]} for n in order if n in per_remote],
                                "copies": {k: v for k, v in copies.items() if v},
                                "categories": [{"category": k, "contents": v} for k, v in sorted(cats.items())],
                                "exts": [{"ext": k, "contents": v} for k, v in sorted(exts.items())][:12]}}

    def compare(self, q: dict) -> tuple[int, dict]:
        sides = {}
        for side in ("left", "right"):
            remote, path = q[f"{side}_remote"], q[f"{side}_path"].strip("/")
            if remote not in self._remote_names():
                return 404, {"error": f"no remote {remote}", "code": "NOT_FOUND", "params": {"remote": remote}}
            node = _get(self.fs, remote, path)
            if path == QUARANTINE or path.startswith(QUARANTINE + "/"):
                return 409, {"error": "quarantine", "code": "QUARANTINE", "params": {"item": _loc(remote, path)}}
            if not isinstance(node, dict):
                return 409, {"error": "not a folder", "code": "NOT_A_FOLDER", "params": {"remote": remote, "path": path}}
            sides[side] = (remote, path, node)
        (lr, lp, ln), (rr, rp, rn) = sides["left"], sides["right"]
        if lr == rr and (lp == rp or lp.startswith(rp + "/") or rp.startswith(lp + "/") or lp == "" or rp == ""):
            return 409, {"error": "nested", "code": "COMPARE_NESTED", "params": {"left": _loc(lr, lp), "right": _loc(rr, rp)}}

        def flat(node: dict, base: str = "") -> dict[str, int]:
            out: dict[str, int] = {}
            for name, child in node.items():
                rel = _join(base, name)
                out.update(flat(child, rel) if isinstance(child, dict) else {rel: child})
            return out

        lf, rf = flat(ln), flat(rn)
        for side, files, limit_side in (("left", lf, self.compare_limit), ("right", rf, self.compare_limit)):
            if limit_side is not None and len(files) > limit_side:
                return 409, {"error": "too large", "code": "COMPARE_TOO_LARGE",
                             "params": {"side": side, "files": len(files), "limit": limit_side}}

        def entry(remote: str, root: str, rel: str, size: int, n: int) -> dict:
            name = rel.rpartition("/")[2]
            return {"entry_id": n, "path": _join(root, rel), "name": name, "size": size, "mtime": "2024-03-01T10:00:00Z",
                    "category": CATEGORY.get(name.rpartition(".")[2], "other"), "content_group": n, "hashes": {"md5": "0" * 32}}

        rows: list[dict] = []
        eid = [0]

        def mk(rel: str, status: str, left_rel: str | None, right_rel: str | None, **extra: Any) -> None:
            eid[0] += 2
            left = entry(lr, lp, left_rel, lf[left_rel], eid[0]) if left_rel is not None else None
            right = entry(rr, rp, right_rel, rf[right_rel], eid[0] + 1) if right_rel is not None else None
            rows.append({"rel": rel, "status": status, "rel_right": None, "left": left, "right": right, "evidence": None,
                         "elsewhere": None, "unique": None, **extra})

        for rel in sorted(set(lf) & set(rf)):
            if rel in self.unproven:
                mk(rel, "unverified", rel, rel, evidence={"kind": "none"})
            elif lf[rel] == rf[rel]:
                mk(rel, "same", rel, rel, evidence={"kind": "group"})
            else:
                mk(rel, "differs", rel, rel, evidence={"kind": "size"})
        left_rest = sorted(set(lf) - set(rf))
        right_rest = sorted(set(rf) - set(lf))
        for rel in list(left_rest):
            twin = next((o for o in right_rest if o.rpartition("/")[2] == rel.rpartition("/")[2] and rf[o] == lf[rel]), None)
            if twin:
                mk(rel, "moved", rel, twin, rel_right=twin, evidence={"kind": "group"})
                left_rest.remove(rel)
                right_rest.remove(twin)
        for rel in left_rest:
            name = rel.rpartition("/")[2]
            uniq = name in self.unique_names
            mk(rel, "left_only", rel, None, unique=uniq,
               elsewhere={"count": 0 if uniq else 2, "examples": [] if uniq else [_loc("onedrive", "Backup/" + name)]})
        for rel in right_rest:
            mk(rel, "right_only", None, rel, unique=False, elsewhere={"count": 1, "examples": [_loc("gdrive", "Backup/" + rel)]})
        rows.sort(key=lambda r: r["rel"].lower())

        def stat(status: str) -> dict:
            mine = [r for r in rows if r["status"] == status]
            return {"files": len(mine), "bytes": sum((r["right"] if status == "right_only" else r["left"])["size"] for r in mine)}

        summary = {s: stat(s) for s in ("same", "unverified", "differs", "moved", "left_only", "right_only")}
        total = sum(v["files"] for v in summary.values())
        summary["unique"] = sum(1 for r in rows if r["unique"])
        summary["similarity"] = (summary["same"]["files"] + summary["moved"]["files"]) / total if total else 1.0
        return 200, {"left": {"remote": lr, "path": lp, "files": len(lf), "bytes": sum(lf.values())},
                     "right": {"remote": rr, "path": rp, "files": len(rf), "bytes": sum(rf.values())},
                     "summary": summary, "rows": rows}

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

    def go(self, route: str) -> None:
        """Change the route the way a link or the address bar does."""
        self.page.evaluate("(h) => { window.location.hash = h; }", route)

    def tid(self, testid: str):
        return self.page.get_by_test_id(testid)

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
    # Closing the finished run swaps the applied draft for a fresh one and reloads both trees; a drag that starts
    # meanwhile is swallowed (the layout moves under the mouse), so wait until the page is on the new draft.
    expect(ui.page.get_by_test_id("draft-select")).to_have_value("2")
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


# =========================================================================== Overview, Explore and Compare
# (docs/EXPLORE.md): the rail, the router, the three read-only views and their links into the workspace.

def _rich_pair(mock: MockApi) -> None:
    """gdrive:學校 against dropbox:學校 with every status: same, unverified, differs, moved, left only (a unique one), right only."""
    mock.fs["dropbox"]["學校"]["課程"] = {"syllabus.pdf": 80_000}
    mock.fs["dropbox"]["學校"]["舊檔"] = {"week1.pptx": 900_000}
    mock.fs["dropbox"]["學校"]["notes.md"] = 4_000
    mock.fs["gdrive"]["學校"]["notes.md"] = 4_000
    mock.fs["gdrive"]["學校"]["課程"]["figure3.png"] = 20_000
    mock.unproven = {"notes.md"}


PAIR = "#/compare?l=gdrive:學校&r=dropbox:學校"


def _wait_explore(ui: Ui, n: int = 1):
    """Wait until the page has sent `n` more /v1/explore requests (typing is debounced) and return the last one."""
    before = len(ui.mock.calls_to("GET", "/v1/explore"))
    ui.page.wait_for_function("() => true")
    for _ in range(100):
        if len(ui.mock.calls_to("GET", "/v1/explore")) >= before + n:
            break
        ui.page.wait_for_timeout(40)
    return ui.mock.last("GET", "/v1/explore")


def test_the_rail_switches_views_and_marks_the_current_one(make_ui):
    ui = make_ui(locale="en-US").open()
    expect(ui.tid("rail")).to_be_visible()
    expect(ui.tid("rail")).to_have_attribute("aria-label", "Views")
    expect(ui.tid("rail-workspace")).to_have_attribute("aria-current", "page")     # no route: the workspace, as always
    assert ui.page.evaluate("location.hash") == ""
    for view in ("overview", "explore", "compare", "workspace"):
        ui.tid(f"rail-{view}").click()
        expect(ui.tid(f"rail-{view}")).to_have_attribute("aria-current", "page")
        expect(ui.tid("rail").locator('[aria-current="page"]')).to_have_count(1)
        assert ui.page.evaluate("location.hash") == f"#/{view}"
        if view == "workspace":
            expect(ui.tid("workspace-host")).to_be_visible()
            expect(ui.tid("pane-left")).to_be_visible()
        else:
            expect(ui.tid(f"view-{view}")).to_be_visible()
            expect(ui.tid("workspace-host")).to_be_hidden()
            expect(ui.tid("workspace-host")).to_have_attribute("hidden", "")


def test_routes_come_from_the_hash_and_back_returns(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/overview")
    expect(ui.tid("view-overview")).to_be_visible()
    ui.go("#/explore?q=thesis")
    expect(ui.tid("explore-input")).to_have_value("thesis")
    ui.page.go_back()
    expect(ui.tid("view-overview")).to_be_visible()
    expect(ui.tid("rail-overview")).to_have_attribute("aria-current", "page")
    ui.page.go_forward()
    expect(ui.tid("explore-input")).to_have_value("thesis")
    ui.page.reload()                                  # the route survives a reload (the session cookie is still good)
    expect(ui.tid("view-explore")).to_be_visible()
    expect(ui.tid("explore-input")).to_have_value("thesis")
    ui.go("#/compare?l=gdrive:學校&r=dropbox:學校")
    expect(ui.tid("rail-compare")).to_have_attribute("aria-current", "page")
    expect(ui.tid("cmp-summary")).to_be_visible()
    assert [c.query["left_remote"] for c in ui.mock.calls_to("GET", "/v1/compare")] == ["gdrive"]


def test_the_workspace_stays_mounted_and_keeps_its_state_while_another_view_shows(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(ui.tid("staged-count")).to_have_text("1")
    expect(ui.node("right", "dropbox:學校/資料")).to_be_visible()
    reads = len(ui.mock.calls_to("GET", "/v1/tree/roots"))
    ui.tid("rail-overview").click()
    expect(ui.tid("pane-left")).to_be_hidden()
    ui.tid("rail-explore").click()
    ui.tid("rail-workspace").click()
    expect(ui.tid("pane-left")).to_be_visible()
    expect(ui.tid("staged-count")).to_have_text("1")                       # the draft, the open folders: all still there
    expect(ui.node("right", "dropbox:學校/資料")).to_be_visible()
    expect(ui.node("left", "gdrive:學校")).to_be_visible()
    assert len(ui.mock.calls_to("GET", "/v1/tree/roots")) == reads            # nothing was reloaded by switching
    assert not ui.mock.calls_to("POST", "/v1/drafts")


def test_alt_digits_switch_views_and_the_slash_key_jumps_to_the_explore_box(make_ui):
    ui = make_ui(locale="en-US").open()
    for digit, view in ((2, "explore"), (1, "overview"), (3, "compare"), (4, "workspace")):
        ui.page.keyboard.press(f"Alt+{digit}")
        expect(ui.tid(f"rail-{view}")).to_have_attribute("aria-current", "page")
    ui.page.keyboard.press("/")                                           # nothing is being typed into: the box gets the keyboard
    expect(ui.tid("rail-explore")).to_have_attribute("aria-current", "page")
    expect(ui.tid("explore-input")).to_be_focused()
    ui.page.keyboard.type("a/b")                                          # inside a text field "/" is just a character
    expect(ui.tid("explore-input")).to_have_value("a/b")
    ui.page.keyboard.press("Escape")
    expect(ui.tid("explore-input")).to_have_value("")


def test_ctrl_z_does_not_undo_the_draft_while_another_view_is_on_top(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.expand_all()
    ui.drag(("left", "gdrive:研究/資料"), ("right", "dropbox:學校"))
    expect(ui.tid("staged-count")).to_have_text("1")
    ui.tid("rail-overview").click()
    ui.page.keyboard.press("Control+z")
    ui.tid("rail-workspace").click()
    expect(ui.tid("staged-count")).to_have_text("1")
    assert not ui.mock.calls_to("POST", r"/v1/drafts/1/undo")


# --------------------------------------------------------------------------- the query language

QUERY_NAMES = ["gdrive", "dropbox", "onedrive"]


def _parse(ui: Ui, texts: list[str]) -> list[dict]:
    return ui.page.evaluate("""async ([texts, names]) => {
        const q = await import('/ui/static/query.js');
        return texts.map((text) => {
            const a = q.parseQuery(text, names);
            const b = q.parseQuery(q.formatQuery(a.params), names);
            return { text, params: a.params, again: b.params, formatted: q.formatQuery(a.params), errors: a.errors.map((e) => e.code),
                     chips: a.chips.map((c) => [c.key, c.value, c.error && c.error.code]) };
        });
    }""", [texts, QUERY_NAMES])


def test_parse_query_reads_every_token_of_the_table(make_ui):
    ui = make_ui(locale="en-US").open()
    cases = {
        'thesis "final draft" in:gdrive': {"q": "thesis final draft", "in": "gdrive"},
        "on:gdrive on:dropbox not:onedrive": {"on": ["gdrive", "dropbox"], "not_on": ["onedrive"]},
        "only:onedrive,gdrive": {"only_on": ["gdrive", "onedrive"]},
        "copies:1": {"copies_min": 1, "copies_max": 1},
        "copies:>2": {"copies_min": 3},
        "copies:2..4": {"copies_min": 2, "copies_max": 4},
        "type:PDF": {"ext": "pdf"},
        "cat:photo": {"category": "photo"},
        "size:>10MB": {"min_size": 10 * 1024 ** 2 + 1},
        "size:<1k": {"max_size": 1023},
        "size:1M..1G": {"min_size": 1024 ** 2, "max_size": 1024 ** 3},
        "size:2.5KB": {"min_size": 2560, "max_size": 2560},
        "modified:2023": {"modified_after": "2023-01-01", "modified_before": "2024-01-01"},
        "modified:2023-06": {"modified_after": "2023-06-01", "modified_before": "2023-07-01"},
        "modified:2023-12-31": {"modified_after": "2023-12-31", "modified_before": "2024-01-01"},
        "modified:>2024-01-01": {"modified_after": "2024-01-02"},
        "modified:<2020": {"modified_before": "2020-01-01"},
        "modified:2021..2022": {"modified_after": "2021-01-01", "modified_before": "2023-01-01"},
        "path:School/Thesis": {"path_prefix": "School/Thesis"},
        "報告 type:docx 學校": {"q": "報告 學校", "ext": "docx"},
        "": {},
    }
    got = _parse(ui, list(cases))
    for row in got:
        assert row["params"] == cases[row["text"]], row
        assert row["errors"] == [], row
        assert row["again"] == row["params"], f"round trip of {row['text']!r} through {row['formatted']!r}: {row}"


def test_format_query_round_trips_every_parameter_it_can_hold(make_ui):
    ui = make_ui(locale="en-US").open()
    params = ui.page.evaluate("""async (names) => {
        const q = await import('/ui/static/query.js');
        const all = { q: 'thesis draft', in: 'dropbox', on: ['gdrive', 'dropbox'], not_on: ['onedrive'], only_on: ['gdrive', 'dropbox'],
            copies_min: 2, copies_max: 5, ext: 'pdf', category: 'photo', min_size: 1048576, max_size: 1572864,
            modified_after: '2021-01-01', modified_before: '2023-01-01', path_prefix: 'School/My Thesis' };
        const half = { copies_min: 3, min_size: 10485761, modified_after: '2024-01-02' };
        const other = { copies_max: 1, max_size: 1023, modified_before: '2020-03-01' };
        const spans = { modified_after: '2023-06-01', modified_before: '2023-07-01' };
        return [all, half, other, spans, {}].map((p) => ({ p, text: q.formatQuery(p), back: q.parseQuery(q.formatQuery(p), names).params }));
    }""", QUERY_NAMES)
    for row in params:
        assert row["back"] == row["p"], row
    assert params[0]["text"].startswith('thesis draft in:dropbox on:gdrive,dropbox not:onedrive only:gdrive,dropbox copies:2..5 type:pdf')
    assert 'path:"School/My Thesis"' in params[0]["text"]
    assert "size:1M..1536K" in params[0]["text"] and "modified:2021-01-01..2022-12-31" in params[0]["text"]
    assert "modified:2023-06" in params[3]["text"]


def test_malformed_tokens_stay_visible_as_red_chips_and_are_not_sent(make_ui):
    ui = make_ui(locale="en-US").open()
    got = _parse(ui, ["thesis foo:bar in:nowhere size:big copies:x modified:2023-13 type: on:gdrive,nowhere"])[0]
    assert got["params"] == {"q": "thesis"}
    assert got["errors"] == ["unknownKey", "unknownCloud", "badValue", "badValue", "badValue", "empty", "unknownCloud"]
    ui.go("#/explore")
    ui.tid("explore-input").fill("thesis foo:bar size:big type:pdf")
    sent = _wait_explore(ui, 2).query
    assert sent.get("q") == "thesis" and sent.get("ext") == "pdf" and "min_size" not in sent and "foo" not in sent
    chips = ui.tid("query-chip")
    expect(chips).to_have_count(3)
    bad = ui.page.locator('[data-testid="query-chip"][data-error]')
    expect(bad).to_have_count(2)
    expect(bad.first).to_have_attribute("data-error", "unknownKey")
    expect(bad.first).to_have_attribute("title", 'Unknown filter "foo:"')
    expect(ui.tid("query-errors")).to_contain_text("2 filters were not understood")
    bad.first.get_by_test_id("query-chip-remove").click()                       # removing a chip edits the text
    expect(ui.tid("explore-input")).to_have_value("thesis size:big type:pdf")


def test_the_help_button_shows_the_query_syntax(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore")
    ui.tid("explore-help").click()
    dialog = ui.tid("query-help")
    expect(dialog).to_be_visible()
    for token in ("in:gdrive", "only:gdrive,dropbox", "copies:2..4", "size:1M..1G", "path:School/Thesis"):
        expect(dialog).to_contain_text(token)
    ui.page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(ui.tid("explore-help")).to_be_focused()


# --------------------------------------------------------------------------- Explore

def test_explore_groups_results_by_content_with_a_presence_cell_per_cloud(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore?q=final")
    row = ui.page.locator('[data-testid="ex-result"][data-key="g:1"]')
    expect(row).to_be_visible()
    expect(ui.tid("ex-count")).to_have_text("1 of 1")
    gd, db, = row.locator('[data-cloud="gdrive"]'), row.locator('[data-cloud="dropbox"]')
    expect(gd).to_have_attribute("aria-label", "on gdrive, 1 copy")
    expect(db).to_have_attribute("aria-label", "on dropbox, 2 copies")
    expect(db).to_contain_text("2")                                         # the count is drawn when a cloud holds more than one
    expect(gd).not_to_have_class(re.compile("unproven"))
    # the request carries exactly the words
    sent = ui.mock.last("GET", "/v1/explore")
    assert sent.query["q"] == "final" and sent.query["limit"] == "50" and "sort" not in sent.query
    # a copy whose name does not match is still listed under its content, marked
    row.get_by_test_id("ex-expand").click()
    copies = row.get_by_test_id("ex-copy")
    expect(copies).to_have_count(3)
    odd = row.locator('[data-testid="ex-copy"][data-matched="false"]')
    expect(odd).to_have_count(1)
    expect(odd).to_contain_text("name does not match")
    expect(odd).to_contain_text("dropbox:Archive/thesis-v9.pdf")
    expect(row.get_by_test_id("ex-expand")).to_have_attribute("aria-expanded", "true")


def test_a_content_without_a_hash_group_is_drawn_dashed_with_its_caveat(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore?q=notes")
    row = ui.page.locator('[data-testid="ex-result"][data-key="e:9"]')
    cell = row.locator('[data-cloud="gdrive"]')
    expect(cell).to_have_class(re.compile("unproven"))
    expect(cell).to_have_attribute("title", re.compile("identity not proven: copies elsewhere may exist under a different hash"))
    expect(cell).to_have_attribute("aria-label", "on gdrive, 1 copy")
    expect(row.locator('[data-cloud="dropbox"]')).to_have_attribute("aria-label", "not on dropbox")


def test_facets_edit_the_query_text_and_the_request(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore")
    expect(ui.tid("ex-count")).to_have_text("6 of 6")
    expect(ui.tid("facet-on-gdrive")).to_be_visible()
    ui.tid("facet-on-gdrive").check()
    expect(ui.tid("explore-input")).to_have_value("on:gdrive")
    expect(ui.tid("ex-count")).to_have_text("5 of 5")
    assert ui.mock.last("GET", "/v1/explore").query["on"] == "gdrive"
    ui.tid("facet-copies-1").check()
    expect(ui.tid("explore-input")).to_have_value("on:gdrive copies:1")
    expect(ui.tid("ex-count")).to_have_text("3 of 3")
    q = ui.mock.last("GET", "/v1/explore").query
    assert (q["copies_min"], q["copies_max"]) == ("1", "1")
    ui.tid("facet-cat-spreadsheet").check()
    expect(ui.tid("explore-input")).to_have_value("on:gdrive copies:1 cat:spreadsheet")
    expect(ui.tid("ex-count")).to_have_text("1 of 1")
    expect(ui.tid("facet-cat-spreadsheet")).to_be_checked()
    ui.tid("facet-on-gdrive").uncheck()                                       # the same box takes its token out again
    expect(ui.tid("explore-input")).to_have_value("copies:1 cat:spreadsheet")
    ui.tid("query-chip-remove").first.click()
    expect(ui.tid("explore-input")).to_have_value("cat:spreadsheet")
    assert unquote(ui.page.evaluate("location.hash")) == "#/explore?q=cat:spreadsheet"


def test_typing_updates_the_address_and_back_undoes_a_facet(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore")
    ui.tid("explore-input").fill("survey type:csv")
    ui.page.wait_for_function("() => location.hash.includes('survey')")
    assert unquote(ui.page.evaluate("location.hash")) == "#/explore?q=survey type:csv"
    expect(ui.tid("ex-count")).to_have_text("1 of 1")
    ui.tid("explore-clear").click()
    expect(ui.tid("explore-input")).to_have_value("")
    expect(ui.tid("ex-count")).to_have_text("6 of 6")
    ui.page.go_back()                                                         # the clear was a history entry of its own
    expect(ui.tid("explore-input")).to_have_value("survey type:csv")
    expect(ui.tid("ex-count")).to_have_text("1 of 1")


def test_explore_sorts_and_loads_more_in_pages_of_fifty(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.mock.catalog += [{"key": f"g:{100 + i}", "name": f"bulk{i:03}.txt", "size": 100 + i, "category": "document", "ext": "txt",
                         "mtime": "2020-01-01T00:00:00Z", "verified": True, "copies": [("gdrive", f"Bulk/bulk{i:03}.txt")]}
                        for i in range(60)]
    ui.go("#/explore")
    expect(ui.tid("ex-count")).to_have_text("50 of 66")
    expect(ui.tid("ex-result")).to_have_count(50)
    ui.tid("ex-load-more").click()
    expect(ui.tid("ex-count")).to_have_text("66 of 66")
    expect(ui.tid("ex-result")).to_have_count(66)
    assert [c.query["offset"] for c in ui.mock.calls_to("GET", "/v1/explore")][-2:] == ["0", "50"]
    expect(ui.tid("ex-load-more")).to_have_count(0)
    ui.tid("explore-sort").select_option("size")
    expect(ui.tid("ex-result").first).to_have_attribute("data-key", "g:4")
    assert ui.mock.last("GET", "/v1/explore").query["sort"] == "size"
    assert "sort=size" in ui.page.evaluate("location.hash")


def test_a_slow_answer_to_an_earlier_query_is_ignored(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore")
    expect(ui.tid("ex-count")).to_have_text("6 of 6")
    ui.page.evaluate("""() => {
        const real = window.fetch; let n = 0;
        window.fetch = (url, opts) => {
            if (!String(url).includes('/v1/explore')) return real(url, opts);
            n += 1; const mine = n;
            return real(url, opts).then((r) => new Promise((done) => setTimeout(() => done(r), mine === 1 ? 900 : 0)));
        };
    }""")
    ui.tid("explore-input").fill("survey")
    ui.page.wait_for_timeout(380)                       # the first request is out (and held back) ...
    ui.tid("explore-input").fill("trip")                # ... and a newer one is sent after it
    expect(ui.tid("ex-result")).to_have_count(1)
    expect(ui.tid("ex-result")).to_have_attribute("data-key", "g:3")
    ui.page.wait_for_timeout(1100)                      # the old answer has arrived by now and must not replace the new one
    expect(ui.tid("ex-result")).to_have_attribute("data-key", "g:3")


def test_explore_shows_a_refusal_in_the_users_language_and_none_when_nothing_matches(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore?q=zzzz")
    expect(ui.tid("ex-none")).to_have_text("Nothing matches this query.")
    ui.mock.explore_fail = ("BAD_QUERY", {"param": "min_size"})
    ui.tid("explore-input").fill("size:>1k")
    expect(ui.tid("ex-error")).to_have_text('The search parameter "min_size" is not valid.')
    ui.tid("view-lang").click()
    expect(ui.tid("ex-error")).to_have_text("搜尋參數「min_size」不正確。")


def test_reveal_opens_the_workspace_with_the_item_selected_in_the_left_pane(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore?q=survey")
    ui.tid("ex-expand").click()
    ui.tid("ex-reveal").click()
    expect(ui.tid("workspace-host")).to_be_visible()
    assert ui.page.evaluate("location.hash") == "#/workspace"
    target = ui.node("left", "gdrive:研究/資料/survey.csv")
    expect(target).to_be_visible()
    expect(target).to_have_attribute("aria-selected", "true")
    expect(ui.tid("rail-workspace")).to_have_attribute("aria-current", "page")


def test_a_result_with_copies_in_two_folders_offers_to_compare_their_folders(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore?q=report")
    ui.tid("ex-expand").click()
    ui.tid("ex-compare-folders").click()
    expect(ui.tid("view-compare")).to_be_visible()
    assert unquote(ui.page.evaluate("location.hash")) == "#/compare?l=gdrive:學校&r=dropbox:學校"
    expect(ui.tid("cmp-summary")).to_be_visible()
    expect(ui.tid("cmp-left-path")).to_have_value("學校")


# --------------------------------------------------------------------------- Overview

def test_overview_shows_the_cloud_cards_the_totals_and_the_undercount_warning(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/overview")
    gd, db = ui.tid("cloud-card-gdrive"), ui.tid("cloud-card-dropbox")
    expect(gd).to_contain_text("Last scan 2024-03-01 10:00")
    expect(gd.get_by_test_id("cloud-files")).to_have_text("5")
    expect(ui.tid("cloud-only-gdrive")).to_contain_text("3")                    # survey, trip, notes exist on no other cloud
    expect(ui.tid("cloud-only-dropbox")).to_contain_text("1")
    expect(ui.tid("cloud-unverified-gdrive")).to_contain_text("1")
    expect(ui.tid("cloud-undercount-gdrive")).to_contain_text("drivemerge bridge")
    expect(ui.tid("cloud-undercount-dropbox")).to_have_count(0)                # nothing to warn about on dropbox
    expect(db).to_contain_text("dropbox")
    totals = ui.tid("ov-totals")
    expect(totals).to_contain_text("Unique content")
    expect(totals).to_contain_text("Redundant")
    assert ui.mock.last("GET", "/v1/coverage").query == {"max_bars": "24"}


def test_a_read_only_cloud_is_badged(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.mock.coverage_override = {
        "remotes": [{"name": "gdrive", "provider": "drive", "writable": True, "files": 1, "bytes": 5, "only_here": {"contents": 0, "bytes": 0},
                     "unverified_files": 0, "last_scan": None},
                    {"name": "dropbox", "provider": "dropbox", "writable": False, "files": 1, "bytes": 5, "only_here": {"contents": 0, "bytes": 0},
                     "unverified_files": 0, "last_scan": None}],
        "totals": {"contents": 1, "bytes": 5, "stored_bytes": 10, "redundant_bytes": 5, "copies": 2},
        "intersections": [{"remotes": ["gdrive", "dropbox"], "contents": 1, "bytes": 5, "copies": 2, "stored_bytes": 10}], "other": None}
    ui.go("#/overview")
    expect(ui.tid("cloud-card-dropbox")).to_contain_text("read-only")
    expect(ui.tid("cloud-card-gdrive")).not_to_contain_text("read-only")
    expect(ui.tid("cloud-card-gdrive")).to_contain_text("Not scanned yet")
    expect(ui.tid("cloud-only-gdrive")).to_be_disabled()


def test_the_intersection_chart_has_a_bar_per_combination_and_toggles_its_measure(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/overview")
    bars = ui.tid("chart-bar")
    expect(bars).to_have_count(3)
    order = [b.get_attribute("data-remotes") for b in bars.all()]
    assert order == ["gdrive", "gdrive,dropbox", "dropbox"]                    # by contents: 3, 2, 1
    assert bars.first.get_attribute("data-contents") == "3"
    expect(bars.nth(1)).to_have_attribute("role", "button")
    expect(bars.nth(1)).to_have_attribute("tabindex", "0")
    expect(bars.nth(1)).to_have_attribute("aria-label", re.compile(r"gdrive \+ dropbox: 2 contents, 4\.1 MB, 5 copies"))
    bars.nth(1).hover()
    expect(ui.tid("chart-readout")).to_contain_text("gdrive + dropbox: 2 contents")
    ui.tid("measure-bytes").click()
    assert [b.get_attribute("data-remotes") for b in bars.all()] == ["dropbox", "gdrive,dropbox", "gdrive"]    # by bytes: 9.0, 4.3, 3.1 MB
    expect(ui.tid("measure-bytes")).to_have_attribute("aria-pressed", "true")
    ui.tid("ov-category").select_option("pdf")
    expect(bars).to_have_count(1)
    assert ui.mock.last("GET", "/v1/coverage").query["category"] == "pdf"
    expect(bars.first).to_have_attribute("data-remotes", "gdrive,dropbox")


def test_a_bar_opens_explore_filtered_to_exactly_that_set_of_clouds(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/overview")
    ui.page.locator('[data-testid="chart-bar"][data-remotes="dropbox"]').click()
    expect(ui.tid("view-explore")).to_be_visible()
    expect(ui.tid("explore-input")).to_have_value("only:dropbox")
    assert unquote(ui.page.evaluate("location.hash")) == "#/explore?q=only:dropbox"
    assert _wait_explore(ui).query["only_on"] == "dropbox"
    expect(ui.tid("ex-count")).to_have_text("1 of 1")
    ui.page.go_back()
    expect(ui.tid("view-overview")).to_be_visible()
    # the keyboard does the same: Enter on a focused bar
    ui.page.locator('[data-testid="chart-bar"][data-remotes="gdrive,dropbox"]').focus()
    ui.page.keyboard.press("Enter")
    expect(ui.tid("explore-input")).to_have_value("only:gdrive,dropbox")
    assert _wait_explore(ui).query["only_on"] == "gdrive,dropbox"
    expect(ui.tid("ex-count")).to_have_text("2 of 2")


def test_a_cloud_cards_only_here_number_opens_explore_and_other_combinations_are_not_a_link(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/overview")
    ui.tid("cloud-only-gdrive").click()
    expect(ui.tid("explore-input")).to_have_value("only:gdrive")
    base = ui.mock.coverage({})
    base["other"] = {"combinations": 5, "contents": 7, "bytes": 1234, "copies": 9, "stored_bytes": 2000}
    ui.mock.coverage_override = base
    ui.go("#/overview")
    other = ui.tid("chart-other")
    expect(other).to_have_count(1)
    expect(other).to_have_attribute("aria-label", re.compile(r"Other combinations \(5\): 7 contents"))
    assert other.get_attribute("role") == "img" and other.get_attribute("tabindex") is None
    other.click(force=True)
    expect(ui.tid("view-overview")).to_be_visible()


def test_similar_folders_are_listed_with_a_compare_button(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/overview")
    rows = ui.tid("ov-similar")
    expect(rows).to_have_count(2)
    dup = ui.page.locator('[data-testid="ov-similar"][data-kind="duplicates"]')
    expect(dup).to_contain_text("95%")
    expect(dup).to_contain_text("gdrive:學校")
    dup.get_by_test_id("ov-compare").click()
    assert unquote(ui.page.evaluate("location.hash")) == "#/compare?l=gdrive:學校&r=dropbox:學校"
    expect(ui.tid("cmp-summary")).to_be_visible()


# --------------------------------------------------------------------------- Compare

def test_compare_counts_every_status_and_draws_the_tree_with_a_row_per_file(make_ui):
    ui = make_ui(locale="en-US")
    _rich_pair(ui.mock)
    ui.open().go(PAIR)
    summary = ui.tid("cmp-summary")
    expect(summary).to_be_visible()
    for status in ("same", "unverified", "differs", "moved", "left_only", "right_only"):
        expect(ui.tid(f"cmp-count-{status}")).to_have_attribute("data-count", "1")
    expect(ui.tid("cmp-count-unique")).to_contain_text("1 file exists nowhere else")
    expect(ui.tid("cmp-similarity")).to_contain_text("33%")
    sent = ui.mock.last("GET", "/v1/compare").query
    assert sent == {"left_remote": "gdrive", "left_path": "學校", "right_remote": "dropbox", "right_path": "學校"}
    tree = ui.tid("cmp-tree")
    expect(tree).to_have_attribute("role", "tree")
    status = {r.get_attribute("data-testid"): r.get_attribute("data-status") for r in tree.locator('[role="treeitem"][data-status]').all()}
    assert status == {"cmp-file-課程/figure3.png": "left_only", "cmp-file-課程/week1.pptx": "moved", "cmp-file-notes.md": "unverified",
                      "cmp-file-report.pdf": "differs", "cmp-file-作業/hw1.docx": "right_only"}        # `same` is hidden by default
    expect(ui.tid("cmp-file-課程/syllabus.pdf")).to_have_count(0)
    moved = ui.tid("cmp-file-課程/week1.pptx")
    expect(moved).to_contain_text("舊檔/week1.pptx")                                                      # where it is on the other side
    expect(moved).to_contain_text("Moved")
    uniq = ui.tid("cmp-file-課程/figure3.png")
    expect(uniq).to_contain_text("Only left")
    expect(uniq).to_contain_text("unique")
    expect(ui.tid("cmp-file-作業/hw1.docx")).to_contain_text("⧉ 1")                                         # one copy elsewhere
    expect(ui.tid("cmp-file-notes.md")).to_contain_text("Probably same")
    folder = ui.tid("cmp-dir-課程")
    expect(folder).to_have_attribute("aria-expanded", "true")
    expect(folder.locator(".stackbar")).to_have_attribute("aria-label", re.compile("Same 1, Moved 1, Only left 1"))
    expect(folder.locator(".cmp-diff-count")).to_have_text("2")                                           # files that are not the same


def test_show_same_and_the_status_filters_narrow_the_tree_but_not_the_counts(make_ui):
    ui = make_ui(locale="en-US")
    _rich_pair(ui.mock)
    ui.open().go(PAIR)
    expect(ui.tid("cmp-file-report.pdf")).to_be_visible()
    ui.tid("cmp-show-same").check()
    expect(ui.tid("cmp-file-課程/syllabus.pdf")).to_have_attribute("data-status", "same")
    ui.tid("cmp-filter-differs").click()                                    # a filter shows only what it names
    expect(ui.page.locator('[role="treeitem"][data-status]')).to_have_count(1)
    expect(ui.tid("cmp-file-report.pdf")).to_be_visible()
    expect(ui.tid("cmp-filter-differs")).to_have_attribute("aria-pressed", "true")
    ui.tid("cmp-filter-moved").click()
    expect(ui.page.locator('[role="treeitem"][data-status]')).to_have_count(2)
    expect(ui.tid("cmp-dir-課程")).to_be_visible()                          # its folder is opened to show the moved file
    expect(ui.tid("cmp-file-課程/week1.pptx")).to_be_visible()
    for status in ("same", "differs", "left_only"):                          # the header always counts the whole comparison
        expect(ui.tid(f"cmp-count-{status}")).to_have_attribute("data-count", "1")
    ui.tid("cmp-filter-differs").click()
    ui.tid("cmp-filter-moved").click()
    ui.tid("cmp-show-same").uncheck()
    expect(ui.tid("cmp-file-課程/syllabus.pdf")).to_have_count(0)


def test_the_tree_is_a_keyboard_tree_and_enter_opens_the_detail(make_ui):
    ui = make_ui(locale="en-US")
    _rich_pair(ui.mock)
    ui.open().go(PAIR)
    folder = ui.tid("cmp-dir-課程")
    folder.focus()
    ui.page.keyboard.press("ArrowLeft")                                      # collapse
    expect(folder).to_have_attribute("aria-expanded", "false")
    expect(ui.tid("cmp-file-課程/week1.pptx")).to_have_count(0)
    ui.page.keyboard.press("ArrowRight")                                     # expand
    expect(folder).to_have_attribute("aria-expanded", "true")
    ui.page.keyboard.press("ArrowRight")                                     # on an open folder: into its first child
    expect(ui.tid("cmp-file-課程/figure3.png")).to_be_focused()
    ui.page.keyboard.press("ArrowDown")
    expect(ui.tid("cmp-file-課程/week1.pptx")).to_be_focused()
    ui.page.keyboard.press("ArrowLeft")                                      # on a file: back to its folder
    expect(folder).to_be_focused()
    ui.page.keyboard.press("ArrowDown")
    ui.page.keyboard.press("ArrowDown")
    ui.page.keyboard.press("Enter")
    detail = ui.tid("cmp-detail")
    expect(detail).to_have_attribute("data-kind", "file")
    expect(detail).to_contain_text("week1.pptx")
    expect(ui.tid("cmp-file-課程/week1.pptx")).to_have_attribute("aria-selected", "true")


def test_the_detail_panel_shows_both_sides_the_evidence_and_where_else_the_content_lives(make_ui):
    ui = make_ui(locale="en-US")
    _rich_pair(ui.mock)
    ui.open().go(PAIR)
    ui.tid("cmp-file-report.pdf").click()
    detail = ui.tid("cmp-detail")
    expect(detail).to_contain_text("gdrive:學校/report.pdf")
    expect(detail).to_contain_text("dropbox:學校/report.pdf")
    expect(detail).to_contain_text("52,000 B")
    expect(detail).to_contain_text("52,100 B")
    expect(ui.tid("cmp-evidence")).to_have_text("Evidence: The sizes differ.")
    ui.tid("cmp-file-notes.md").click()
    expect(ui.tid("cmp-evidence")).to_contain_text("No comparable hash")
    ui.tid("cmp-file-課程/week1.pptx").click()
    expect(ui.tid("cmp-moved-to")).to_contain_text("課程/week1.pptx")
    expect(ui.tid("cmp-moved-to")).to_contain_text("舊檔/week1.pptx")
    expect(ui.tid("cmp-evidence")).to_contain_text("same content group")
    ui.tid("cmp-file-課程/figure3.png").click()                              # the only copy anywhere: a warning
    expect(ui.tid("cmp-unique")).to_contain_text("The only copy on any cloud")
    ui.tid("cmp-file-作業/hw1.docx").click()
    expect(ui.tid("cmp-elsewhere")).to_contain_text("1 other copy outside these folders")
    expect(ui.tid("cmp-elsewhere")).to_contain_text("gdrive:Backup/作業/hw1.docx")
    ui.tid("cmp-file-report.pdf").click()
    ui.tid("cmp-reveal-right").click()                                       # Reveal leads into the workspace, left pane
    expect(ui.tid("workspace-host")).to_be_visible()
    expect(ui.node("left", "dropbox:學校/report.pdf")).to_have_attribute("aria-selected", "true")


def test_merge_left_into_right_stages_one_move_of_the_whole_folder(make_ui):
    ui = make_ui(locale="en-US")
    _rich_pair(ui.mock)
    ui.open().go(PAIR)
    ui.tid("cmp-merge-lr").click()
    post = ui.mock.last("POST", r"/v1/drafts/1/actions")
    assert post.body == {"kind": "move", "items": [{"remote": "gdrive", "path": "學校"}], "dst": {"remote": "dropbox", "path": ""},
                         "new_name": "學校"}
    toast = ui.page.locator('[data-testid="toast"][data-kind="success"]')
    expect(toast).to_contain_text("Staged: merge gdrive:學校 into dropbox:學校")
    expect(ui.page.locator('[data-testid="toast"][data-kind="warn"]').first).to_be_visible()    # the staging warnings (a merge, a copy between clouds)
    toast.get_by_test_id("toast-action").click()                              # "Open workspace"
    expect(ui.tid("workspace-host")).to_be_visible()
    expect(ui.tid("staged-count")).to_have_text("1")
    ui.go(PAIR)
    expect(ui.tid("cmp-draft-note")).to_contain_text("as last scanned")          # the comparison reads the catalog, not the draft


def test_merge_right_into_left_and_the_parent_of_a_nested_folder(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/compare?l=gdrive:研究/論文&r=dropbox:學校")
    ui.tid("cmp-merge-rl").click()
    assert ui.mock.last("POST", r"/v1/drafts/1/actions").body == {
        "kind": "move", "items": [{"remote": "dropbox", "path": "學校"}], "dst": {"remote": "gdrive", "path": "研究"}, "new_name": "論文"}
    ui.go("#/compare?l=dropbox:學校&r=gdrive:研究/論文")
    ui.tid("cmp-merge-lr").click()
    expect(ui.page.locator('[data-testid="toast"][data-kind="success"]').last).to_contain_text("Staged")
    assert ui.mock.last("POST", r"/v1/drafts/1/actions").body["dst"] == {"remote": "gdrive", "path": "研究"}


def test_merging_a_whole_cloud_is_not_offered(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/compare?l=gdrive:Photos&r=dropbox:")
    expect(ui.tid("cmp-summary")).to_be_visible()
    expect(ui.tid("cmp-merge-lr")).to_be_disabled()
    expect(ui.tid("cmp-merge-lr")).to_have_attribute("title", re.compile("choose a folder on each side"))
    assert not ui.mock.calls_to("POST", r"/v1/drafts/1/actions")


def test_move_to_the_other_side_stages_one_move_into_the_same_relative_folder(make_ui):
    ui = make_ui(locale="en-US")
    _rich_pair(ui.mock)
    ui.open().go(PAIR)
    ui.tid("cmp-file-課程/figure3.png").click()
    ui.tid("cmp-move").click()
    post = ui.mock.last("POST", r"/v1/drafts/1/actions")
    assert post.body == {"kind": "move", "items": [{"remote": "gdrive", "path": "學校/課程/figure3.png"}],
                         "dst": {"remote": "dropbox", "path": "學校/課程"}}
    expect(ui.page.locator('[data-testid="toast"][data-kind="success"]')).to_contain_text("Staged: move gdrive:學校/課程/figure3.png")


def test_move_to_the_other_side_explains_when_the_folder_is_missing_there(make_ui):
    ui = make_ui(locale="en-US")
    _rich_pair(ui.mock)
    ui.mock.fs["gdrive"]["學校"]["新資料夾"] = {"x.txt": 5}
    ui.open().go(PAIR)
    ui.tid("cmp-file-新資料夾/x.txt").click()
    before = len(ui.mock.calls_to("POST", r"/v1/drafts/1/actions"))
    ui.tid("cmp-move").click()
    toast = ui.page.locator('[data-testid="toast"][data-kind="error"]')
    expect(toast).to_contain_text("The folder dropbox:學校/新資料夾 does not exist on the other side")
    assert len(ui.mock.calls_to("POST", r"/v1/drafts/1/actions")) == before
    # a whole folder that exists on one side only moves as one step
    ui.tid("cmp-dir-作業").click()
    expect(ui.tid("cmp-detail")).to_have_attribute("data-kind", "dir")
    ui.tid("cmp-move").click()
    assert ui.mock.last("POST", r"/v1/drafts/1/actions").body == {
        "kind": "move", "items": [{"remote": "dropbox", "path": "學校/作業"}], "dst": {"remote": "gdrive", "path": "學校"}}


def test_a_refusal_is_worded_by_the_page_from_the_code_and_params(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/compare?l=gdrive:研究/論文/thesis.pdf&r=dropbox:學校")
    err = ui.tid("cmp-error")
    expect(err).to_have_text('"研究/論文/thesis.pdf" is not a folder on gdrive.')
    expect(err).to_have_attribute("data-code", "NOT_A_FOLDER")
    ui.go("#/compare?l=gdrive:研究&r=gdrive:研究/論文")
    expect(err).to_contain_text("One folder contains the other (gdrive:研究 and gdrive:研究/論文)")
    ui.mock.compare_limit = 1
    ui.go("#/compare?l=gdrive:研究/資料&r=dropbox:學校")
    expect(err).to_have_text("The left folder holds 2 files, more than the limit of 1. Choose a smaller folder.")
    ui.mock.compare_limit = None
    ui.go("#/compare?l=gdrive:.drive-merge-quarantine&r=dropbox:學校")
    expect(err).to_have_attribute("data-code", "QUARANTINE")
    ui.tid("view-lang").click()                                              # the same refusals, in Chinese, without a reload
    ui.mock.compare_limit = 1
    ui.go("#/compare?l=gdrive:研究/資料&r=dropbox:學校")
    expect(err).to_have_text("左側資料夾有 2 個檔案，超過 1 個的上限。請選擇較小的資料夾。")
    ui.go("#/compare?l=gdrive:研究/論文/thesis.pdf&r=dropbox:學校")
    expect(err).to_have_text("gdrive 上的「研究/論文/thesis.pdf」不是資料夾。")


def test_the_pickers_complete_folder_names_and_compare_runs_on_enter(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/compare?l=gdrive:學校")
    expect(ui.tid("cmp-right-remote")).to_be_focused()                         # one side set: the picker for the other is next
    expect(ui.tid("cmp-pick-right")).to_be_visible()
    ui.tid("cmp-right-remote").select_option("dropbox")
    path = ui.tid("cmp-right-path")
    path.click()
    path.type("學")
    options = ui.tid("cmp-right-option")
    expect(options).to_have_count(1)
    expect(options).to_contain_text("學校")
    assert ui.mock.last("GET", "/v1/tree").query["remote"] == "dropbox"
    path.press("ArrowDown")
    path.press("Enter")                                                       # picks the folder
    expect(path).to_have_value("學校")
    assert not ui.mock.calls_to("GET", "/v1/compare")
    path.press("Enter")                                                       # a second Enter runs the comparison
    expect(ui.tid("cmp-summary")).to_be_visible()
    assert unquote(ui.page.evaluate("location.hash")) == "#/compare?l=gdrive:學校&r=dropbox:學校"
    with ui.awaiting("GET", "/v1/compare"):
        ui.tid("cmp-swap").click()                                            # swapping flips the pair and compares again
    assert unquote(ui.page.evaluate("location.hash")) == "#/compare?l=dropbox:學校&r=gdrive:學校"
    expect(ui.tid("cmp-left-remote")).to_have_value("dropbox")
    assert ui.mock.last("GET", "/v1/compare").query["left_remote"] == "dropbox"


def test_compare_with_in_the_folder_row_menu_opens_compare_with_the_picker_on_the_right(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.expand("left", "gdrive:")
    ui.node("left", "gdrive:學校").click(button="right")
    expect(ui.tid("row-menu")).to_be_visible()
    ui.tid("menu-compare").click()
    expect(ui.tid("view-compare")).to_be_visible()
    assert unquote(ui.page.evaluate("location.hash")) == "#/compare?l=gdrive:學校"
    expect(ui.tid("cmp-left-remote")).to_have_value("gdrive")
    expect(ui.tid("cmp-left-path")).to_have_value("學校")
    expect(ui.tid("cmp-right-remote")).to_be_focused()
    # the button on the row and the keyboard open the same menu; a file has none
    ui.tid("rail-workspace").click()
    ui.node("left", "gdrive:學校").hover()
    ui.tid("row-menu-gdrive:學校").click()
    expect(ui.tid("menu-compare")).to_be_focused()
    ui.page.keyboard.press("Escape")
    expect(ui.tid("row-menu")).to_have_count(0)
    ui.expand("left", "gdrive:學校")
    ui.node("left", "gdrive:學校/report.pdf").click()
    ui.page.keyboard.press("Shift+F10")
    expect(ui.tid("row-menu")).to_have_count(0)
    ui.node("left", "gdrive:學校").click()
    ui.page.keyboard.press("ContextMenu")
    expect(ui.tid("menu-compare")).to_be_focused()


def test_a_suggestion_card_has_a_compare_button(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.tid("suggestions-toggle").click()
    card = ui.tid("suggestion-card").first
    card.get_by_test_id("suggestion-compare").click()
    expect(ui.tid("view-compare")).to_be_visible()
    assert unquote(ui.page.evaluate("location.hash")) == "#/compare?l=gdrive:研究/論文&r=gdrive:研究/資料"
    expect(ui.tid("cmp-summary")).to_be_visible()


# --------------------------------------------------------------------------- both languages, colours, looks

def test_the_new_views_speak_both_languages_and_a_switch_needs_no_reload(make_ui):
    ui = make_ui(locale="zh-TW")
    _rich_pair(ui.mock)
    ui.open().go(PAIR)
    expect(ui.tid("rail-explore")).to_contain_text("探索")
    expect(ui.tid("cmp-count-left_only")).to_contain_text("僅左側")
    expect(ui.tid("cmp-file-課程/week1.pptx")).to_contain_text("搬動過")
    expect(ui.tid("cmp-merge-lr")).to_contain_text("左側併入右側")
    ui.tid("cmp-file-report.pdf").click()
    expect(ui.tid("cmp-evidence")).to_contain_text("大小不同")
    ui.tid("view-lang").click()
    expect(ui.tid("rail-explore")).to_contain_text("Explore")
    expect(ui.tid("cmp-count-left_only")).to_contain_text("Only left")
    expect(ui.tid("cmp-evidence")).to_contain_text("The sizes differ")
    ui.go("#/overview")
    expect(ui.tid("cloud-only-gdrive")).to_contain_text("Only here")
    ui.tid("view-lang").click()
    expect(ui.tid("cloud-only-gdrive")).to_contain_text("僅存在這裡")
    ui.go("#/explore?q=final")
    expect(ui.page.locator('[data-testid="ex-result"] [data-cloud="dropbox"]').first).to_have_attribute("aria-label", "在 dropbox，2 份")
    assert ui.page.evaluate("document.documentElement.lang") == "zh-Hant-TW"


def _luminance(color: str) -> float:
    m = re.fullmatch(r"#([0-9a-f]{6})", color.strip().lower())
    assert m, color
    channels = [int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_cloud_colour_has_at_least_three_to_one_against_the_surface(make_ui, scheme):
    ui = make_ui(locale="en-US", color_scheme=scheme).open()
    colors = ui.page.evaluate("""() => {
        const cs = getComputedStyle(document.documentElement);
        return { surface: cs.getPropertyValue('--surface'), surface2: cs.getPropertyValue('--surface-2'),
                 clouds: [0, 1, 2, 3, 4, 5, 6, 7].map((i) => cs.getPropertyValue('--cloud-' + i)) };
    }""")
    assert len(set(c.strip().lower() for c in colors["clouds"])) == 8                      # eight distinct hues
    for surface in (colors["surface"], colors["surface2"]):
        sl = _luminance(surface)
        for color in colors["clouds"]:
            cl = _luminance(color)
            ratio = (max(sl, cl) + 0.05) / (min(sl, cl) + 0.05)
            assert ratio >= 3.0, f"{scheme}: {color} on {surface} is only {ratio:.2f}:1"


def test_a_cloud_keeps_its_colour_in_every_view(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/explore?q=final")
    cell = ui.page.locator('[data-testid="ex-result"] [data-cloud="dropbox"] .presence-dot').first
    expect(cell).to_be_visible()
    explore_colour = cell.evaluate("el => getComputedStyle(el).backgroundColor")
    ui.go("#/overview")
    dot = ui.tid("cloud-card-dropbox").locator(".cloud-dot")
    assert dot.evaluate("el => getComputedStyle(el).backgroundColor") == explore_colour
    gd = ui.tid("cloud-card-gdrive").locator(".cloud-dot").evaluate("el => getComputedStyle(el).backgroundColor")
    assert gd != explore_colour


def test_screenshots_of_the_new_views(make_ui):
    """overview/explore/compare in light and dark, English and Chinese, at 1440 and at the narrowest width (900)."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    for scheme, locale, width in (("light", "en-US", 1440), ("dark", "en-US", 1440), ("light", "zh-TW", 1440), ("dark", "zh-TW", 900)):
        ui = make_ui(color_scheme=scheme, locale=locale, width=width, height=860)
        _rich_pair(ui.mock)
        ui.mock.catalog.append({"key": "g:6", "name": "thesis-notes.docx", "size": 700_000, "category": "document", "ext": "docx",
                                "mtime": "2023-02-01T10:00:00Z", "verified": True,
                                "copies": [("dropbox", "學校/thesis-notes.docx"), ("gdrive", "研究/論文/thesis-notes.docx")]})
        ui.open()
        tag = f"{scheme}-{locale}-{width}"
        ui.go("#/overview")
        expect(ui.tid("chart-bar").first).to_be_visible()
        ui.tid("chart-bar").nth(1).hover()
        ui.page.screenshot(path=str(SHOTS / f"overview-{tag}.png"))
        ui.go("#/explore?q=thesis cat:pdf not:onedrive")
        expect(ui.tid("ex-result").first).to_be_visible()
        ui.tid("ex-expand").first.click()
        ui.page.screenshot(path=str(SHOTS / f"explore-{tag}.png"))
        ui.go(PAIR)
        expect(ui.tid("cmp-tree")).to_be_visible()
        ui.tid("cmp-file-report.pdf").click()
        ui.page.screenshot(path=str(SHOTS / f"compare-{tag}.png"))
    assert (SHOTS / "compare-light-en-US-1440.png").stat().st_size > 5_000


def test_the_keys_the_new_views_build_at_run_time_exist_in_both_languages_and_use_what_the_server_sends(make_ui):
    """`t(`cmp.status.${s}`)` and friends cannot be found by scanning the sources, so they are listed here."""
    ui = make_ui(locale="en-US")
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    dicts = ui.page.evaluate("import('/ui/static/i18n.js').then(m => m.dictionaries)")
    statuses = ["same", "unverified", "differs", "moved", "left_only", "right_only"]
    keys = [f"nav.{v}" for v in ("overview", "explore", "compare", "workspace")] + [f"nav.{v}Tip" for v in ("overview", "explore", "compare", "workspace")]
    keys += [f"cmp.status.{s}" for s in statuses] + [f"cmp.statusTip.{s}" for s in statuses]
    keys += [f"cmp.evidence.{k}" for k in ("group", "size", "hash", "none")] + ["cmp.side.left", "cmp.side.right"]
    keys += [f"ex.sort.{s}" for s in ("relevance", "size", "mtime", "name", "copies")]
    keys += [f"q.help.{k}" for k in "word in on not only copies type cat size modified path".split()]
    keys += [f"q.err.{c}" for c in ("unknownKey", "empty", "unknownCloud", "repeated", "badValue", "summary")]
    keys += [f"q.cat.{c}" for c in "photo video audio document pdf spreadsheet presentation archive code design other".split()]
    keys += ["ex.expand", "ex.collapse", "sug.compare", "menu.compareWith", "cloud.column"]
    for lang in ("zh-TW", "en"):
        for key in keys:
            assert key in dicts[lang], (lang, key)
    # the refusals of the new views: the page words each from exactly the params the spec lists
    sent = {"NOT_A_FOLDER": {"remote", "path"}, "COMPARE_NESTED": {"left", "right"}, "COMPARE_TOO_LARGE": {"side", "files", "limit"},
            "BAD_QUERY": {"param"}}
    for code, params in sent.items():
        for lang in ("zh-TW", "en"):
            used = set(re.findall(r"\{(\w+)\}", dicts[lang][f"err.{code}"]))
            assert used and used <= params, (lang, code, used)
        assert re.search(r"[一-鿿]", dicts["zh-TW"][f"err.{code}"]) and not re.search(r"[一-鿿]", dicts["en"][f"err.{code}"])


def test_a_folder_of_identical_files_is_hidden_and_when_shown_it_is_closed(make_ui):
    ui = make_ui(locale="en-US")
    _rich_pair(ui.mock)
    ui.mock.fs["gdrive"]["學校"]["相同"] = {"a.txt": 10, "b.txt": 20}
    ui.mock.fs["dropbox"]["學校"]["相同"] = {"a.txt": 10, "b.txt": 20}
    ui.open().go(PAIR)
    expect(ui.tid("cmp-count-same")).to_have_attribute("data-count", "3")
    expect(ui.tid("cmp-dir-相同")).to_have_count(0)                         # nothing to see in it: hidden by default
    ui.tid("cmp-show-same").check()
    folder = ui.tid("cmp-dir-相同")
    expect(folder).to_have_attribute("aria-expanded", "false")             # and closed when it is shown
    expect(ui.tid("cmp-file-相同/a.txt")).to_have_count(0)
    folder.click()
    expect(ui.tid("cmp-file-相同/a.txt")).to_have_attribute("data-status", "same")
    expect(folder.locator(".cmp-diff-count")).to_have_text("0")


# =========================================================================== Plans (docs/PLANS.md)
# The list, the review, the two forms and the bridge dialog, against the mocked routes of section C.

def _ids(ui: Ui, testid: str = "plan-row") -> list[str]:
    return ui.page.eval_on_selector_all(f'[data-testid="{testid}"]', "els => els.map(e => e.dataset.id)")


def _query(ui: Ui) -> dict:
    h = ui.page.evaluate("location.hash")
    return dict(parse_qs(h.partition("?")[2]))


def test_plans_sits_between_compare_and_the_workspace_and_alt_5_opens_it(make_ui):
    ui = make_ui(locale="en-US").open()
    order = ui.page.eval_on_selector_all('[data-testid="rail"] a', "els => els.map(e => e.dataset.testid)")
    assert order == ["rail-setup", "rail-clouds", "rail-overview", "rail-explore", "rail-compare", "rail-plans",
                     "rail-workspace"]
    ui.page.keyboard.press("Alt+5")
    expect(ui.tid("rail-plans")).to_have_attribute("aria-current", "page")
    expect(ui.tid("view-plans")).to_be_visible()
    assert ui.page.evaluate("location.hash") == "#/plans"
    ui.page.keyboard.press("Alt+4")                                  # Alt+1...4 keep their meaning
    expect(ui.tid("rail-workspace")).to_have_attribute("aria-current", "page")
    expect(ui.tid("rail-plans")).to_have_attribute("title", re.compile(r"\(Alt\+5\)"))


def test_the_list_puts_plans_waiting_for_review_first_and_counts_them_on_the_rail(make_ui):
    ui = make_ui(locale="en-US")
    a = ui.mock.add_plan(kind="dedupe", status="simulated", created_by="api", ops=2310, params={"cross_remote": False})
    b = ui.mock.add_plan(kind="consolidate", status="draft", created_by="mcp", ok=False, problems=2, outdated=True,
                         params={"target": "gdrive", "target_root": "Archive", "sources": ["dropbox"]})
    done = ui.mock.add_plan(kind="dedupe", status="done", created_by="cli", params={"cross_remote": True, "remote": "gdrive"})
    ui.mock.add_plan(kind="dedupe", status="cancelled")
    preview = ui.mock.add_plan(kind="workspace", status="simulated", created_by="workspace", draft_id=1)
    ui.open()
    expect(ui.tid("rail-plans-badge")).to_have_attribute("data-count", "2")        # the workspace preview is not a proposal
    ui.tid("rail-plans").click()
    waiting = ui.tid("plans-waiting")
    expect(waiting.get_by_test_id("plan-row")).to_have_count(2)
    expect(ui.tid("plans-waiting-count")).to_have_text("2")
    first = waiting.get_by_test_id("plan-row").first
    expect(first).to_have_attribute("data-id", str(b))                              # newest first
    expect(first).to_contain_text("Gather onto gdrive:Archive (copy only) · from dropbox")
    expect(first.get_by_test_id("claude-badge")).to_be_visible()
    expect(first).to_contain_text("2 problems")
    expect(first.get_by_test_id("plan-outdated")).to_be_visible()
    second = waiting.get_by_test_id("plan-row").nth(1)
    expect(second).to_contain_text("Remove redundant copies (within each cloud) · all clouds")
    expect(second).to_contain_text("2,310 ops")
    expect(second).to_contain_text("ok")
    expect(ui.tid("plans-tab-done")).to_have_attribute("data-count", "1")
    expect(ui.tid("plans-tab-cancelled")).to_have_attribute("data-count", "1")
    ui.tid("plans-tab-done").click()
    expect(ui.tid("plans-tabpanel").get_by_test_id("plan-row")).to_have_attribute("data-id", str(done))
    expect(ui.tid("plans-tabpanel")).to_contain_text("across clouds")
    expect(ui.tid("plans-workspace")).to_contain_text("Workspace previews (1)")
    assert str(preview) not in _ids(ui)[:2]
    waiting.get_by_test_id("plan-open").nth(1).click()
    expect(ui.tid("view-plan-review")).to_have_attribute("data-id", str(a))


def test_the_review_shows_the_shared_report_the_ops_and_a_filter(make_ui):
    ui = make_ui(locale="en-US")
    bad = [{"code": "DST_EXISTS", "seq": 1, "message": "'dropbox:學校/report.pdf' holds different or unproven content; never overwritten",
            "params": {"variant": "different", "at": _loc("dropbox", "學校/report.pdf")}}]
    pid = ui.mock.add_plan(kind="dedupe", status="draft", ok=False, problems=1, ops=120, violations=bad)
    ui.open().go(f"#/plans/{pid}")
    expect(ui.tid("sim-status")).to_have_attribute("data-ok", "false")
    expect(ui.tid("violation")).to_have_attribute("data-code", "DST_EXISTS")
    expect(ui.tid("violation")).to_contain_text("never")                          # the page's own sentence, plus the code pill
    expect(ui.tid("cloud-row-gdrive")).to_be_visible()
    expect(ui.tid("summary-ops")).to_contain_text("120")
    expect(ui.tid("plan-cannot-approve")).to_be_visible()
    expect(ui.tid("approval-area")).to_have_count(0)
    assert ui.mock.last("POST", rf"/v1/ui/plans/{pid}/review")
    groups = ui.tid("ops-groups")
    expect(groups).to_contain_text("Quarantine")
    expect(groups).to_contain_text("120")
    expect(ui.tid("op-row")).to_have_count(50)
    expect(ui.tid("op-witness").first).to_contain_text("witness: gdrive:Photos/copy 0.jpg")
    ui.tid("ops-more").click()
    expect(ui.tid("op-row")).to_have_count(100)
    ui.tid("ops-filter").fill("copy 7")
    expect(ui.tid("op-row")).to_have_count(11)                                    # copy 7, copy 70 ... copy 79
    ui.tid("reveal-link").click()
    expect(ui.tid("rail-workspace")).to_have_attribute("aria-current", "page")     # the reveal goes to the workspace


def test_approving_with_the_typed_code_then_running_shows_progress_and_the_catalog_banner(make_ui):
    ui = make_ui(locale="en-US")
    pid = ui.mock.add_plan(kind="dedupe", status="simulated", ops=4)
    ui.open().go(f"#/plans/{pid}")
    expect(ui.tid("approval-code")).to_have_text(MockApi.CODE)                     # only the review response carries it
    expect(ui.tid("approval-button")).to_be_disabled()
    ui.tid("approval-input").fill("zzzz")
    expect(ui.tid("approval-button")).to_be_disabled()
    ui.tid("approval-input").fill(MockApi.CODE)
    ui.tid("approval-button").click()
    expect(ui.tid("plan-approved")).to_be_visible()
    call = ui.mock.last("POST", rf"/v1/ui/plans/{pid}/approve")
    assert call.body == {"code": MockApi.CODE} and call.headers["x-csrf-token"] == MockApi.CSRF
    assert not ui.mock.calls_to("POST", rf"/v1/ui/plans/{pid}/execute")            # approving does not run it
    ui.tid("plan-run").click()
    expect(ui.tid("progress-current")).to_be_visible()
    expect(ui.tid("progress")).to_have_attribute("data-phase", "done", timeout=10_000)
    assert ui.mock.calls_to("POST", rf"/v1/ui/plans/{pid}/execute")
    assert ui.mock.draft["status"] == "open"                                       # a plan of this view does not close the draft
    expect(ui.tid("catalog-banner")).to_be_visible()
    ui.tid("rescan-button").click()
    expect(ui.tid("progress")).to_have_attribute("data-phase", "rescanned", timeout=10_000)
    ui.tid("plan-back").click()
    expect(ui.tid("view-plans")).to_be_visible()


def test_without_approvals_the_review_shows_the_terminal_commands_and_no_form(make_ui):
    ui = make_ui(locale="en-US", approvals_enabled=False)
    pid = ui.mock.add_plan()
    ui.open().go(f"#/plans/{pid}")
    expect(ui.tid("approvals-banner")).to_be_visible()
    expect(ui.tid("cli-approve")).to_have_text(f"drivemerge plan approve {pid}")
    expect(ui.tid("cli-execute")).to_have_text(f"drivemerge plan execute {pid}")
    expect(ui.tid("approval-area")).to_have_count(0)
    expect(ui.tid("plan-run")).to_have_count(0)
    assert "approval_code" not in json.dumps(ui.mock.plan_json(ui.mock.plans[pid]))


def test_cancelling_a_plan_asks_first_and_moves_it_to_the_cancelled_tab(make_ui):
    ui = make_ui(locale="en-US")
    pid = ui.mock.add_plan()
    ui.open().go(f"#/plans/{pid}")
    ui.tid("plan-cancel").click()
    ui.tid("dialog-cancel").click()
    assert not ui.mock.calls_to("POST", rf"/v1/ui/plans/{pid}/cancel")
    ui.tid("plan-cancel").click()
    ui.tid("dialog-ok").click()
    expect(ui.tid("view-plans")).to_be_visible()
    assert ui.mock.plans[pid]["status"] == "cancelled"
    expect(ui.tid("plans-tab-cancelled")).to_have_attribute("data-count", "1", timeout=12_000)


def test_a_closed_plan_opens_read_only_with_its_state_in_words(make_ui):
    ui = make_ui(locale="en-US")
    pid = ui.mock.add_plan(status="done")
    ui.open().go(f"#/plans/{pid}")
    expect(ui.tid("plan-closed")).to_contain_text(f"Plan #{pid} is Done")
    expect(ui.tid("op-row").first).to_be_visible()
    expect(ui.tid("approval-area")).to_have_count(0)
    expect(ui.tid("plan-cancel")).to_have_count(0)
    ui.go("#/plans/9999")
    expect(ui.tid("plan-error")).to_contain_text("There is no plan #9999")


# --- dedupe

def test_the_dedupe_form_lives_in_the_address_bar_and_the_estimate_follows_it(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/dedupe?remote=gdrive&across=1&path=Backup&min=1M&prefer=dropbox,gdrive")
    expect(ui.tid("dd-remote")).to_have_value("gdrive")
    expect(ui.tid("dd-mode-across")).to_be_checked()
    expect(ui.tid("dd-across-warning")).to_be_visible()
    expect(ui.tid("dd-path")).to_have_value("Backup")
    expect(ui.tid("dd-min")).to_have_value("1M")
    expect(ui.tid("prefer-list").locator("li").first).to_have_attribute("data-cloud", "dropbox")
    expect(ui.tid("estimate-headline").or_(ui.tid("estimate-nothing"))).to_be_visible()
    body = ui.mock.last("POST", "/v1/plans/estimate").body
    assert body["kind"] == "dedupe"
    assert body["params"] == {"cross_remote": True, "remote": "gdrive", "path_prefix": "Backup", "min_size": 1_048_576,
                              "prefer_remotes": ["dropbox", "gdrive"], "group_offset": 0, "group_limit": 50}
    ui.tid("dd-mode-each").check()                                                  # an edit is written back to the address
    ui.page.wait_for_function("() => !location.hash.includes('across')")
    assert _query(ui)["remote"] == ["gdrive"]
    ui.tid("dd-min").fill("big")
    expect(ui.tid("dd-min-error")).to_be_visible()
    ui.go("#/plans/new/dedupe")                                                     # a link resets the form
    expect(ui.tid("dd-path")).to_have_value("")


def test_typing_is_debounced_into_one_estimate_and_a_stale_answer_is_dropped(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/dedupe")
    expect(ui.tid("dd-group").first).to_be_visible()
    before = len(ui.mock.calls_to("POST", "/v1/plans/estimate"))
    ui.tid("dd-path").press_sequentially("Backup", delay=40)
    expect(ui.tid("estimating")).to_have_count(0)
    assert len(ui.mock.calls_to("POST", "/v1/plans/estimate")) - before == 1
    # hold the answer of the next request back, then send a newer one: the old answer must not replace the new one
    ui.page.evaluate("""() => {
        const real = window.fetch; let n = 0;
        window.fetch = (url, opts) => {
            if (!String(url).includes('/v1/plans/estimate')) return real(url, opts);
            n += 1; const mine = n;
            return real(url, opts).then((r) => new Promise((done) => setTimeout(() => done(r), mine === 1 ? 1200 : 0)));
        };
    }""")
    ui.tid("dd-path").fill("Old")                                                   # slow answer: only the Old/ report.pdf group
    ui.page.wait_for_timeout(600)
    ui.tid("dd-path").fill("Photos")                                                # newer: only the gdrive Photos group
    expect(ui.tid("dd-group")).to_have_count(0)                                     # nothing to remove under Photos (single copy)
    ui.page.wait_for_timeout(1300)
    expect(ui.tid("dd-group")).to_have_count(0)                                     # the slow Old/ answer arrived and was ignored


def test_the_estimate_lists_groups_with_the_policys_keeper_and_why_a_copy_cannot_stay(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/dedupe")
    expect(ui.tid("estimate-headline")).to_contain_text("3 redundant copies in 3 groups")
    g11 = ui.page.locator('[data-testid="dd-group"][data-key="g:11"]')
    expect(g11.locator('[data-testid="dd-copy"][data-entry="201"]')).to_have_attribute("data-role", "keep")
    expect(g11.locator('[data-testid="dd-copy"][data-entry="202"]')).to_have_attribute("data-role", "remove")
    expect(g11.locator('[data-testid="dd-copy"][data-entry="203"]')).to_have_attribute("data-role", "keep")   # its own copy on dropbox
    why = ui.page.locator('[data-testid="dd-group"][data-key="g:12"] [data-testid="dd-why"]')
    expect(why).to_have_attribute("data-code", "AMBIGUOUS_PATH")
    expect(why).to_have_text("several files share this path")
    expect(ui.page.locator('[data-testid="dd-group"][data-key="g:13"] [data-testid="dd-why"]')).to_contain_text("quarantine")
    expect(ui.tid("est-skipped")).to_contain_text("several files share this path")
    ui.page.locator('[data-testid="dd-group"][data-key="g:12"] [data-testid="dd-copy"][data-entry="205"] button').click(force=True)
    expect(ui.tid("dd-overrides")).to_have_count(0)                                 # a copy that cannot stay cannot be picked
    ui.tid("dd-mode-across").check()
    expect(g11.locator('[data-testid="dd-copy"][data-role="remove"]')).to_have_count(2)


def test_a_keeper_override_and_a_skip_are_part_of_the_create_request(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/dedupe")
    g11 = ui.page.locator('[data-testid="dd-group"][data-key="g:11"]')
    g11.locator('[data-testid="dd-copy"][data-entry="202"] button').click()
    expect(g11.locator('[data-testid="dd-copy"][data-entry="202"]')).to_have_attribute("data-role", "keep")
    expect(g11.locator('[data-testid="dd-copy"][data-entry="201"]')).to_have_attribute("data-role", "remove")
    expect(g11.get_by_test_id("dd-overridden")).to_be_visible()
    ui.page.locator('[data-testid="dd-group"][data-key="g:12"]').get_by_test_id("dd-skip").click()
    expect(ui.page.locator('[data-testid="dd-group"][data-key="g:12"]')).to_have_attribute("data-skipped", "true")
    expect(ui.tid("dd-overrides")).to_contain_text("keepers changed: 1 · groups skipped: 1")
    ui.page.wait_for_function("() => location.hash.includes('skip=')")
    q = _query(ui)
    assert q["skip"] == ["g:12"] and q["keep"] == ["g:11=202"]
    expect(ui.tid("estimate-headline")).to_contain_text("2 redundant copies in 2 groups")
    ui.page.reload()                                                                # Back / reload keep the choices
    expect(ui.page.locator('[data-testid="dd-group"][data-key="g:11"] [data-testid="dd-copy"][data-entry="202"]')).to_have_attribute("data-role", "keep")
    with ui.awaiting("POST", "/v1/plans"):
        ui.tid("create-plan").click()
    body = ui.mock.last("POST", "/v1/plans").body
    assert body == {"kind": "dedupe", "params": {"cross_remote": False, "keep": {"g:11": 202}, "skip_groups": ["g:12"]}}
    expect(ui.tid("view-plan-review")).to_be_visible()


def test_a_stale_override_is_reported_in_words_and_can_be_reset(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/dedupe?keep=g:11%3D999")
    expect(ui.tid("estimate-error")).to_contain_text("Entry 999 cannot be kept for group g:11")
    ui.tid("estimate-error").get_by_role("button", name="Reset").click()
    expect(ui.tid("estimate-headline")).to_be_visible()


def test_the_groups_page_with_load_more(make_ui):
    ui = make_ui(locale="en-US")
    for i in range(60):
        ui.mock.dup_groups.append({"key": f"g:{100 + i}", "size": 1_000 + i,
                                   "copies": [_copy(1000 + 2 * i, "gdrive", f"Bulk/a{i}.bin"), _copy(1001 + 2 * i, "gdrive", f"Bulk/b{i}.bin")]})
    ui.open().go("#/plans/new/dedupe")
    expect(ui.tid("dd-group")).to_have_count(50)
    ui.tid("dd-more").click()
    expect(ui.tid("dd-group")).to_have_count(63)
    assert ui.mock.last("POST", "/v1/plans/estimate").body["params"]["group_offset"] == 50
    expect(ui.tid("dd-more")).to_have_count(0)


def test_nothing_to_do_is_said_instead_of_offering_an_empty_plan(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/dedupe?path=Nowhere")
    expect(ui.tid("estimate-nothing")).to_have_text("Nothing to do: with these settings no operation is needed.")
    expect(ui.tid("create-plan")).to_be_disabled()


def test_a_refused_build_is_worded_by_the_page_from_its_code(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/dedupe?remote=nowhere")
    expect(ui.tid("estimate-error")).to_have_text("There is no cloud called nowhere.")
    ui.go("#/plans/new/dedupe")
    expect(ui.tid("estimate-headline")).to_be_visible()


# --- consolidate

def _with_read_only_cloud(ui: Ui) -> None:
    ui.mock.remotes.append({"id": 3, "name": "onedrive", "provider": "onedrive", "writable": False, "case_insensitive": True})


def test_consolidate_offers_only_writable_targets_and_defaults_to_copying_only(make_ui):
    ui = make_ui(locale="en-US")
    _with_read_only_cloud(ui)
    ui.open().go("#/plans/new/consolidate")
    options = ui.tid("cons-target").locator("option")
    expect(options).to_have_count(2)
    expect(options.nth(0)).to_have_text("gdrive")
    expect(ui.tid("cons-source-dropbox")).to_be_checked()
    expect(ui.tid("cons-source-onedrive")).to_be_checked()
    expect(ui.tid("cons-delete")).not_to_be_checked()
    expect(ui.tid("cons-stats")).to_contain_text("To copy")
    expect(ui.tid("summary-copy")).to_contain_text("1")
    expect(ui.tid("placement-folder")).to_have_attribute("data-dir", "gdrive:Archives")
    expect(ui.tid("placements")).to_contain_text("Archive/old.zip")
    ui.tid("cons-root").fill("Merged")
    expect(ui.tid("placement-folder")).to_have_attribute("data-dir", "gdrive:Merged/Archives")
    with ui.awaiting("POST", "/v1/plans"):
        ui.tid("create-plan").click()
    assert ui.mock.last("POST", "/v1/plans").body == {"kind": "consolidate", "params": {
        "target": "gdrive", "target_root": "Merged", "reorganize_target": False, "include_probable": False, "delete_sources": False}}
    expect(ui.tid("view-plan-review")).to_be_visible()


def test_cleaning_up_sources_sits_behind_a_section_that_warns_while_content_is_not_on_the_target(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/consolidate")
    expect(ui.tid("cons-delete")).to_be_hidden()                                    # closed by default
    ui.tid("cons-cleanup").locator("summary").click()
    expect(ui.tid("cons-missing")).to_have_attribute("data-contents", "1")           # old.zip exists on dropbox only
    expect(ui.tid("cons-missing")).to_contain_text("are still not on gdrive")
    expect(ui.tid("cons-delete-warning")).to_have_count(0)
    ui.tid("cons-delete").check()
    expect(ui.tid("cons-delete-warning")).to_contain_text("copy them and remove their sources in one go")
    ui.page.wait_for_function("() => location.hash.includes('del=1')")
    ui.page.wait_for_timeout(900)                                                   # the estimate follows after its debounce
    assert ui.mock.last("POST", "/v1/plans/estimate").body["params"]["delete_sources"] is True
    ui.go("#/plans/new/consolidate?target=dropbox&src=gdrive")                       # everything gdrive holds is not on dropbox
    ui.tid("cons-cleanup").locator("summary").click()
    expect(ui.tid("cons-missing")).to_have_attribute("data-contents", "3")


def test_probable_matches_are_explained_when_turned_on(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/plans/new/consolidate")
    expect(ui.tid("cons-probable-warning")).to_have_count(0)
    ui.tid("cons-probable").check()
    expect(ui.tid("cons-probable-warning")).to_contain_text("without proof")


def test_a_read_only_target_in_the_address_falls_back_to_a_writable_one(make_ui):
    ui = make_ui(locale="en-US")
    _with_read_only_cloud(ui)
    ui.open().go("#/plans/new/consolidate?target=onedrive")
    expect(ui.tid("cons-target")).to_have_value("gdrive")
    expect(ui.tid("cons-stats")).to_be_visible()
    assert ui.mock.last("POST", "/v1/plans/estimate").body["params"]["target"] == "gdrive"


# --- polling, the toast, the bridge, the links

def test_a_plan_claude_proposes_while_the_page_is_open_raises_the_toast_and_the_badge(make_ui):
    ui = make_ui(locale="en-US")
    ui.mock.add_plan(created_by="api")
    ui.open()
    ui.page.clock.install()
    ui.page.reload()
    expect(ui.tid("rail-plans-badge")).to_have_attribute("data-count", "1")
    assert not ui.tid("toast").count()                                              # what was there when the page opened is not news
    pid = ui.mock.add_plan(created_by="mcp", kind="consolidate", params={"target": "gdrive"}, title="Gather onto gdrive")
    ui.page.clock.fast_forward(10_500)
    toast = ui.tid("toast").filter(has_text=f"Claude proposed plan #{pid}")
    expect(toast).to_be_visible()
    expect(ui.tid("rail-plans-badge")).to_have_attribute("data-count", "2")
    toast.get_by_test_id("toast-action").click()
    expect(ui.tid("view-plan-review")).to_have_attribute("data-id", str(pid))


def test_a_hidden_page_does_not_poll_the_plan_list(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.page.clock.install()
    ui.page.reload()
    expect(ui.tid("rail")).to_be_visible()
    n = len(ui.mock.calls_to("GET", "/v1/plans/summaries"))
    ui.page.evaluate("""() => { Object.defineProperty(document, 'hidden', { get: () => true, configurable: true }); }""")
    ui.page.clock.fast_forward(35_000)
    assert len(ui.mock.calls_to("GET", "/v1/plans/summaries")) == n


def test_the_bridge_dialog_counts_candidates_runs_the_job_and_refreshes_the_overview(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/overview")
    expect(ui.tid("cloud-undercount-gdrive")).to_be_visible()
    reads = len(ui.mock.calls_to("GET", "/v1/coverage"))
    ui.tid("bridge-open-gdrive").click()
    expect(ui.tid("bridge-candidates")).to_contain_text("Candidates: 3 files")
    assert ui.mock.last("GET", "/v1/ui/bridge/candidates").query == {"max_bytes": str(5 * 1024 ** 3), "limit": "500"}
    ui.tid("bridge-limit").fill("abc")
    expect(ui.tid("bridge-start")).to_be_disabled()
    ui.tid("bridge-limit").fill("20")
    expect(ui.tid("bridge-start")).to_be_enabled()
    ui.tid("bridge-start").click()
    expect(ui.tid("bridge-nums")).to_contain_text("Done: 3 bridged, 0 failed", timeout=10_000)
    assert ui.mock.last("POST", "/v1/ui/bridge").body == {"max_bytes": 5 * 1024 ** 3, "limit": 20}
    assert len(ui.mock.calls_to("GET", "/v1/coverage")) > reads
    ui.tid("bridge-close").click()
    expect(ui.tid("bridge-dialog")).to_have_count(0)


def test_the_bridge_button_needs_approvals(make_ui):
    ui = make_ui(locale="en-US", approvals_enabled=False).open()
    ui.go("#/overview")
    expect(ui.tid("cloud-undercount-gdrive")).to_be_visible()
    expect(ui.tid("bridge-open-gdrive")).to_have_count(0)


def test_the_overview_links_into_both_flows(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.go("#/overview")
    ui.tid("ov-dedupe").click()
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "dedupe")
    ui.page.go_back()
    ui.tid("cloud-consolidate-dropbox").click()
    expect(ui.tid("view-plan-new")).to_have_attribute("data-kind", "consolidate")
    expect(ui.tid("cons-target")).to_have_value("gdrive")                            # the first writable other cloud
    expect(ui.tid("cons-source-dropbox")).to_be_checked()
    ui.page.go_back()
    ui.tid("cloud-consolidate-gdrive").click()
    expect(ui.tid("cons-target")).to_have_value("dropbox")


def test_explore_offers_a_dedupe_scoped_to_a_cloud_and_folder_with_several_copies(make_ui):
    ui = make_ui(locale="en-US")
    ui.mock.catalog.append({"key": "g:7", "name": "dup.bin", "size": 5_000, "category": "other", "ext": "bin", "mtime": "2023-02-01T10:00:00Z",
                            "verified": True, "copies": [("gdrive", "Archive/a/dup.bin"), ("gdrive", "Archive/a/b/dup.bin"), ("dropbox", "x/dup.bin")]})
    ui.open().go("#/explore?q=dup.bin")
    ui.tid("ex-expand").first.click()
    expect(ui.tid("ex-dedupe-dropbox")).to_have_count(0)                              # only one copy there
    ui.tid("ex-dedupe-gdrive").click()
    assert _query(ui) == {"remote": ["gdrive"], "path": ["Archive/a"]}
    expect(ui.tid("dd-remote")).to_have_value("gdrive")
    expect(ui.tid("dd-path")).to_have_value("Archive/a")


def test_the_plans_view_speaks_both_languages_and_switches_without_a_reload(make_ui):
    ui = make_ui(locale="zh-TW")
    pid = ui.mock.add_plan(kind="consolidate", created_by="mcp", params={"target": "gdrive"})
    ui.open().go("#/plans")
    expect(ui.tid("rail-plans")).to_contain_text("計畫")
    expect(ui.tid("plans-waiting")).to_contain_text("等待審閱")
    expect(ui.tid("plans-waiting")).to_contain_text("整併到 gdrive:（只複製）")
    ui.go(f"#/plans/{pid}")
    expect(ui.tid("approval-button")).to_contain_text("核准")
    expect(ui.tid("plan-resimulate")).to_contain_text("重新模擬")
    ui.tid("view-lang").click()
    expect(ui.tid("approval-button")).to_contain_text("Approve")
    ui.go("#/plans/new/dedupe")
    expect(ui.tid("dd-group").first).to_be_visible()
    expect(ui.tid("estimate-headline")).to_contain_text("redundant copies")
    ui.tid("view-lang").click()
    expect(ui.tid("estimate-headline")).to_contain_text("多餘副本")
    expect(ui.page.locator('[data-testid="dd-group"][data-key="g:12"] [data-testid="dd-why"]')).to_have_text("有多個檔案共用同一個路徑")
    ui.go("#/plans/new/consolidate")
    expect(ui.tid("cons-stats")).to_contain_text("要複製")


def test_the_keys_the_plans_view_builds_at_run_time_exist_in_both_languages(make_ui):
    ui = make_ui(locale="en-US")
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    dicts = ui.page.evaluate("import('/ui/static/i18n.js').then(m => m.dictionaries)")
    keys = [f"pstatus.{s}" for s in "draft simulated approved executing done failed cancelled".split()]
    keys += [f"kind.{k}" for k in "dedupe consolidate reorganize manual workspace".split()]
    keys += [f"plans.op.{o}" for o in ("copy", "move", "delete")] + [f"plans.by.{b}" for b in "ui api cli workspace".split()]
    keys += [f"build.{c}" for c in "BAD_LOCATION UNKNOWN_REMOTE READ_ONLY BAD_OP MISSING_SRC MISSING_DST BAD_PARAM BAD_KEEPER BAD_GROUP BUILD_ERROR".split()]
    keys += [f"skip.{c}" for c in "READ_ONLY AMBIGUOUS_PATH NATIVE_DOC SIZE_UNKNOWN PROBABLE_ONLY LAYOUT_BLOCKED LAYOUT_TAKEN QUARANTINE OTHER_CLOUD".split()]
    keys += ["err.PLAN_CLOSED", "plans.NOTHING_TO_DO", "ago.now", "ago.min", "ago.hour", "ago.day"]
    for lang in ("zh-TW", "en"):
        for key in keys:
            assert key in dicts[lang], (lang, key)
    used = set()
    for f in WEB.glob("*.js"):
        if f.name != "i18n.js":
            used |= set(re.findall(r"""['"`]((?:plans|dedupe|cons|bridge|skip|build)\.[A-Za-z_.]+)['"`]""", f.read_text(encoding="utf-8")))
    assert not {k for k in used if not k.endswith(".")} - set(dicts["en"]), sorted({k for k in used if not k.endswith(".")} - set(dicts["en"]))


@pytest.mark.parametrize("scheme, locale, width", [("light", "en-US", 1440), ("dark", "en-US", 1440), ("light", "zh-TW", 1440), ("dark", "zh-TW", 900)])
def test_screenshots_of_the_plans_view(make_ui, scheme, locale, width):
    SHOTS.mkdir(parents=True, exist_ok=True)
    ui = make_ui(color_scheme=scheme, locale=locale, width=width, height=860)
    _with_read_only_cloud(ui)
    bad = [{"code": "DST_EXISTS", "seq": 1, "message": "x", "params": {"variant": "different", "at": _loc("dropbox", "學校/report.pdf")}}]
    ui.mock.add_plan(kind="consolidate", status="simulated", created_by="mcp", ops=8120, params={"target": "gdrive", "target_root": "Archive", "sources": ["dropbox"]})
    ui.mock.add_plan(kind="dedupe", status="draft", created_by="ui", ops=2310, ok=False, problems=2, outdated=True)
    ui.mock.add_plan(kind="dedupe", status="simulated", created_by="cli", ops=40, params={"remote": "gdrive", "path_prefix": "Photos"})
    ui.mock.add_plan(kind="dedupe", status="done", created_by="cli")
    review = ui.mock.add_plan(kind="dedupe", status="simulated", ops=120, violations=bad)
    ok = ui.mock.add_plan(kind="dedupe", status="simulated", ops=60)
    ui.open()
    tag = f"{scheme}-{locale}-{width}"
    ui.go("#/plans")
    expect(ui.tid("plan-row").first).to_be_visible()
    ui.page.screenshot(path=str(SHOTS / f"plans-list-{tag}.png"))
    ui.go(f"#/plans/{review}")
    expect(ui.tid("violation")).to_be_visible()
    ui.page.screenshot(path=str(SHOTS / f"plans-review-bad-{tag}.png"))
    ui.go(f"#/plans/{ok}")
    expect(ui.tid("approval-input")).to_be_visible()
    ui.tid("approval-input").fill(MockApi.CODE)
    ui.page.screenshot(path=str(SHOTS / f"plans-review-ok-{tag}.png"))
    ui.go("#/plans/new/dedupe?across=1")
    expect(ui.tid("dd-group").first).to_be_visible()
    ui.page.locator('[data-testid="dd-group"][data-key="g:11"] [data-testid="dd-copy"][data-entry="202"] button').click()
    ui.page.wait_for_timeout(700)
    ui.page.screenshot(path=str(SHOTS / f"plans-dedupe-{tag}.png"))
    ui.go("#/plans/new/consolidate")
    expect(ui.tid("placements")).to_be_visible()
    ui.tid("cons-cleanup").locator("summary").click()
    ui.tid("cons-delete").check()
    ui.page.wait_for_timeout(700)
    ui.page.screenshot(path=str(SHOTS / f"plans-consolidate-{tag}.png"))
    ui.go("#/overview")
    ui.tid("bridge-open-gdrive").click()
    expect(ui.tid("bridge-candidates")).to_contain_text("3")
    ui.page.screenshot(path=str(SHOTS / f"plans-bridge-{tag}.png"))
    assert (SHOTS / f"plans-list-{tag}.png").stat().st_size > 5_000


# =========================================================================== Setup (docs/SETUP.md)

def _job(progress: dict, status: str = "running", **extra: Any) -> dict:
    return {"id": "setup-1", "kind": "connect", "status": status, "progress": progress, "result": None, "error": None, **extra}


SIGNIN = _job({"stage": "signin", "auth_url": "http://127.0.0.1:53682/auth?state=abc"})


def _question(**kw: Any) -> dict:
    q = {"name": "config_driveid", "help": "Select a drive.\nSecond line", "default": "", "examples": [], "password": False,
         "required": False, "exclusive": False, "type": "string"}
    return {"stage": "question", "question": {**q, **kw}}


def _setup_page(make_ui, *, clouds=(), scanned=False, via_route=False, **kw) -> Ui:
    opts = {k: kw.pop(k) for k in ("locale", "approvals_enabled", "width", "height", "color_scheme") if k in kw}
    ui = make_ui(**{"locale": "en-US", **opts})
    ui.mock.make_setup(clouds=list(clouds), scanned=scanned, **kw)
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    if via_route:
        expect(ui.tid("pane-left")).to_be_visible()
        ui.go("#/setup")
    expect(ui.tid("view-setup")).to_be_visible()
    return ui


def test_a_server_that_does_not_know_setup_lands_on_the_workspace_and_the_rail_has_setup_first(make_ui):
    ui = make_ui(locale="en-US").open()                       # /v1/ui/setup is not mocked: 404
    assert ui.page.evaluate("location.hash") == ""
    order = ui.page.eval_on_selector_all('[data-testid="rail"] a', "els => els.map(e => e.dataset.testid)")
    assert order[:2] == ["rail-setup", "rail-clouds"] and len(order) == 7
    expect(ui.tid("rail-setup")).to_have_attribute("title", re.compile(r"\(Alt\+0\)"))
    ui.page.keyboard.press("Alt+0")
    expect(ui.tid("view-setup")).to_be_visible()
    ui.page.keyboard.press("Alt+1")
    expect(ui.tid("rail-overview")).to_have_attribute("aria-current", "page")


def test_setup_without_needed_changes_nothing_and_needed_redirects_only_a_page_without_a_route(make_ui):
    ui = make_ui(locale="en-US")
    ui.mock.make_setup(clouds=[("gdrive", "drive")], scanned=True)           # needed: false
    ui.open()
    assert ui.page.evaluate("location.hash") == ""
    ui = make_ui(locale="en-US")
    ui.mock.make_setup()                                                      # no remote: needed
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    expect(ui.tid("view-setup")).to_be_visible()
    assert ui.page.evaluate("location.hash") == "#/setup"
    ui = make_ui(locale="en-US")
    ui.mock.make_setup()
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    expect(ui.tid("view-setup")).to_be_visible()
    ui = make_ui(locale="en-US")
    ui.mock.make_setup()
    ui.page.goto(f"{ui.base}/ui/?x=1")                                        # a route wins over the redirect
    ui.page.goto(f"{ui.base}/ui/#/overview")
    ui.mock.session_open = True
    ui.page.reload()
    expect(ui.tid("view-overview")).to_be_visible()


def test_the_four_steps_show_their_state(make_ui):
    ui = _setup_page(make_ui)
    for step, state in (("rclone", "done"), ("clouds", "todo"), ("scan", "todo"), ("ready", "todo")):
        expect(ui.tid(f"setup-step-{step}")).to_have_attribute("data-state", state)
    expect(ui.tid("setup-state-clouds")).to_have_text("Not yet")
    expect(ui.tid("setup-open-overview")).to_be_disabled()
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive"), ("dropbox", "dropbox")], scanned=False)
    expect(ui.tid("setup-step-clouds")).to_have_attribute("data-state", "done")
    expect(ui.tid("setup-step-scan")).to_have_attribute("data-state", "attention")
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive")], scanned=True, via_route=True)
    for step in ("rclone", "clouds", "scan", "ready"):
        expect(ui.tid(f"setup-step-{step}")).to_have_attribute("data-state", "done")
    expect(ui.tid("setup-files-gdrive")).to_have_text("41,203 files")
    expect(ui.tid("setup-execution")).to_contain_text("switched off")
    ui.tid("setup-open-overview").click()
    expect(ui.tid("view-overview")).to_be_visible()


def test_a_missing_rclone_needs_attention_and_blocks_connecting(make_ui):
    ui = _setup_page(make_ui, rclone_found=False)
    expect(ui.tid("setup-step-rclone")).to_have_attribute("data-state", "attention")
    expect(ui.tid("setup-rclone-text")).to_contain_text('"rclone"')
    expect(ui.tid("setup-rclone-install")).to_have_attribute("rel", "noopener")
    expect(ui.tid("setup-connect")).to_be_disabled()


def test_connecting_a_cloud_signs_in_through_a_link_answers_a_question_and_lists_the_cloud(make_ui):
    ui = _setup_page(make_ui)
    ui.mock.connect_view = SIGNIN
    ui.tid("setup-connect").click()
    expect(ui.tid("connect-dialog")).to_be_visible()
    ui.tid("connect-name").fill("bad name!")
    expect(ui.tid("connect-start")).to_be_disabled()
    expect(ui.tid("connect-name-help")).to_contain_text("Invalid name")
    ui.tid("connect-provider-dropbox").check()
    ui.tid("connect-name").fill("dbx")
    ui.tid("connect-start").click()
    assert ui.mock.last("POST", "/v1/ui/setup/connect").body == {"provider": "dropbox", "name": "dbx"}
    assert ui.mock.last("POST", "/v1/ui/setup/connect").headers["x-csrf-token"] == MockApi.CSRF
    link = ui.tid("connect-signin-link")
    expect(link).to_have_attribute("href", "http://127.0.0.1:53682/auth?state=abc")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener")
    expect(link).to_have_text("Open the sign-in page")
    link.evaluate("e => e.addEventListener('click', ev => ev.preventDefault())")
    link.click()
    expect(link).to_have_text("Open it again")
    ui.mock.connect_view = _job(_question(name="config_driveid", exclusive=True, default="a1", help="Which drive?",
                                          examples=[{"value": "a1", "help": "OneDrive (personal)"}, {"value": "b2", "help": "Documents"}]))
    expect(ui.tid("connect-question")).to_be_visible()
    expect(ui.tid("connect-q-help")).to_contain_text("Which drive?")
    expect(ui.tid("connect-q-help")).to_contain_text("From rclone")
    expect(ui.tid("connect-q-option-0")).to_be_checked()
    ui.tid("connect-q-option-1").check()
    ui.mock.make_setup(clouds=[("dbx", "dropbox")], scanned=False)
    ui.mock.connect_next = [_job({}, status="done", result={"remote": {"name": "dbx", "provider": "dropbox", "write": None}})]
    ui.tid("connect-q-continue").click()
    assert ui.mock.last("POST", "/v1/ui/setup/connect/setup-1/answer").body == {"result": "b2"}
    expect(ui.tid("connect-done")).to_contain_text("dbx is connected (read-only)")
    expect(ui.tid("setup-cloud-dbx")).to_contain_text("read-only")
    ui.tid("connect-close").click()
    expect(ui.tid("connect-dialog")).to_have_count(0)


def test_a_sign_in_link_that_is_not_rclones_local_helper_is_never_shown(make_ui):
    ui = _setup_page(make_ui)
    ui.mock.connect_view = _job({"stage": "signin", "auth_url": "http://evil.example/auth?state=x"})
    ui.tid("setup-connect").click()
    ui.tid("connect-start").click()
    expect(ui.tid("connect-working")).to_be_visible()
    expect(ui.tid("connect-signin-link")).to_have_count(0)


def test_every_kind_of_question_has_its_input(make_ui):
    ui = _setup_page(make_ui)
    ui.mock.connect_view = SIGNIN
    ui.tid("setup-connect").click()
    ui.tid("connect-start").click()
    expect(ui.tid("connect-signin")).to_be_visible()

    def ask(**kw):
        ui.mock.connect_view = _job(_question(**kw))
        expect(ui.tid("connect-question")).to_have_attribute("data-name", kw.get("name", "config_driveid"))

    ask(name="use_local", type="bool", default="true", exclusive=True, examples=[{"value": "true", "help": "Yes"}, {"value": "false", "help": "No"}])
    ui.mock.connect_next = [_job(_question(name="folder", default="root", required=True))]
    ui.tid("connect-q-no").click()
    assert ui.mock.last("POST", "/v1/ui/setup/connect/setup-1/answer").body == {"result": "false"}
    expect(ui.tid("connect-q-input")).to_have_value("root")
    ui.tid("connect-q-input").fill("")
    expect(ui.tid("connect-q-continue")).to_be_disabled()                 # required
    ui.tid("connect-q-input").fill("Archive")
    ui.mock.connect_next = [_job(_question(name="region", examples=[{"value": "eu", "help": "Europe"}, {"value": "us", "help": "US"}]))]
    ui.page.keyboard.press("Enter")                                       # Enter submits; nothing navigates
    assert ui.mock.last("POST", "/v1/ui/setup/connect/setup-1/answer").body == {"result": "Archive"}
    expect(ui.tid("connect-q-select")).to_be_visible()
    ui.tid("connect-q-select").select_option(index=2)
    expect(ui.tid("connect-q-input")).to_have_value("us")
    expect(ui.tid("connect-q-continue")).to_be_enabled()                  # optional answers may be empty too
    assert "?" not in ui.page.url


def test_a_password_never_reaches_the_url_storage_or_a_toast(make_ui):
    secret = "s3cret-Pa55-xyz"
    ui = _setup_page(make_ui)
    ui.mock.connect_view = SIGNIN
    ui.tid("setup-connect").click()
    ui.tid("connect-provider-iclouddrive").check()
    ui.tid("connect-apple-id").fill("me@example.com")
    ui.tid("connect-apple-password").fill("icloud-pw")
    ui.tid("connect-start").click()
    ui.mock.connect_view = _job(_question(name="password", password=True, required=True, help="Password."))
    field = ui.tid("connect-q-input")
    expect(field).to_have_attribute("type", "password")
    field.fill(secret)
    ui.mock.connect_next = [_job(_question(name="2fa_code", required=True, help="Enter the code."))]
    before = ui.page.url
    ui.tid("connect-q-continue").click()
    assert ui.mock.last("POST", "/v1/ui/setup/connect/setup-1/answer").body == {"result": secret}
    expect(ui.tid("connect-q-name")).to_have_text("2fa_code")
    assert ui.page.url == before and secret not in ui.page.url
    storage = ui.page.evaluate("JSON.stringify([localStorage, sessionStorage])")
    assert secret not in storage
    assert secret not in ui.tid("toasts").inner_text() and secret not in ui.page.locator("body").inner_text()
    for call in ui.mock.calls:
        assert secret not in call.path and secret not in json.dumps(call.query)
        if call.method == "GET":
            assert call.body is None


def test_cancelling_stops_the_sign_in_and_adds_nothing(make_ui):
    ui = _setup_page(make_ui)
    ui.mock.connect_view = SIGNIN
    ui.tid("setup-connect").click()
    ui.tid("connect-start").click()
    expect(ui.tid("connect-signin")).to_be_visible()
    ui.page.keyboard.press("Escape")                                       # a running sign-in is not dismissed by accident
    expect(ui.tid("connect-dialog")).to_be_visible()
    ui.tid("connect-cancel").click()
    assert ui.mock.calls_to("POST", "/v1/ui/setup/connect/setup-1/cancel")
    expect(ui.tid("connect-dialog")).to_have_count(0)
    expect(ui.tid("toast").first).to_contain_text("Nothing was added")


@pytest.mark.parametrize("code, params, text", [
    ("SIGNIN_TIMEOUT", {"name": "x"}, "within 10 minutes"),
    ("CONFIG_WRITE", {"name": "x"}, "could not write its config file"),
    ("RCLONE_ERROR", {"name": "x", "detail": "Failed to configure token: oauth2: cannot fetch"}, "rclone reported an error"),
])
def test_a_failed_connect_is_worded_from_its_code(make_ui, code, params, text):
    ui = _setup_page(make_ui)
    ui.mock.connect_view = SIGNIN
    ui.tid("setup-connect").click()
    ui.tid("connect-start").click()
    ui.mock.connect_view = _job({}, status="failed", error="English message", code=code, params=params)
    expect(ui.tid("connect-failed-text")).to_contain_text(text)
    expect(ui.tid("connect-failed-text")).to_have_attribute("data-code", code)
    if "detail" in params:
        expect(ui.tid("connect-failed-detail")).to_contain_text("oauth2")
    ui.tid("connect-retry").click()
    expect(ui.tid("connect-name")).to_be_visible()


def test_a_refused_connect_shows_the_reason_in_the_dialog(make_ui):
    ui = _setup_page(make_ui)
    ui.mock.connect_refusal = (409, "REMOTE_EXISTS", {"name": "gdrive"})
    ui.tid("setup-connect").click()
    ui.tid("connect-start").click()
    expect(ui.tid("connect-error")).to_contain_text('"gdrive" already exists')


def test_rclone_remotes_not_yet_in_drive_merge_can_be_adopted(make_ui):
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive")], scanned=True, loose=[("work-od", "onedrive")], via_route=True)
    expect(ui.tid("setup-adopt-work-od")).to_contain_text("Found in rclone: work-od (onedrive)")
    ui.tid("setup-adopt-add-work-od").click()
    assert ui.mock.last("POST", "/v1/ui/setup/adopt").body == {"name": "work-od"}
    expect(ui.tid("setup-cloud-work-od")).to_be_visible()
    expect(ui.tid("setup-adopt-work-od")).to_have_count(0)
    expect(ui.tid("setup-step-scan")).to_have_attribute("data-state", "attention")


def test_testing_a_cloud_words_the_result_from_its_code(make_ui):
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive"), ("dropbox", "dropbox")], scanned=True, via_route=True)
    ui.tid("setup-test-gdrive").click()
    expect(ui.tid("setup-test-result-gdrive")).to_contain_text("Works")
    assert ui.mock.last("POST", "/v1/ui/setup/test").body == {"name": "gdrive"}
    for code, text in (("AUTH_EXPIRED", "expired or was revoked"), ("NETWORK", "could not reach the cloud"), ("NOT_FOUND", "Not found"),
                       ("RCLONE_ERROR", "rclone reported an error")):
        ui.mock.test_results["dropbox"] = {"ok": False, "code": code, "params": {}}
        ui.tid("setup-test-dropbox").click()
        expect(ui.tid("setup-test-result-dropbox")).to_contain_text(text)
        expect(ui.tid("setup-test-result-dropbox")).to_have_attribute("data-ok", "false")
    ui.mock.test_results["dropbox"] = {"ok": False, "code": "AUTH_EXPIRED", "params": {}}
    ui.tid("setup-test-dropbox").click()
    expect(ui.tid("setup-test-result-dropbox")).to_contain_text("rclone config reconnect dropbox:")


def test_the_first_scan_shows_live_counts_per_cloud_and_ends_with_four_green_steps(make_ui):
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive"), ("dropbox", "dropbox")], scanned=False)
    mock = ui.mock

    def running(remote: str, i: int, files: int) -> dict:
        return {"id": "scan-1", "kind": "rescan", "status": "running", "result": None, "error": None,
                "progress": {"remotes": ["gdrive", "dropbox"], "remotes_total": 2, "remote": remote, "remote_index": i,
                             "files": files, "dirs": 0, "bytes": 0}}

    def scanned(*names: str):
        def effect():
            for r in mock.setup["remotes"]:
                if r["name"] in names:
                    r.update(last_scan={"id": 3, "status": "complete", "finished_at": "2024-03-01T10:00:00Z"}, files=41203, bytes=5_000_000)
            mock.setup["scanned"] = len(names) == 2
            mock.setup["identity_built"] = len(names) == 2
        return effect

    mock.scan_script = [(running("gdrive", 1, 1200), None)]
    ui.tid("setup-scan-all").click()
    assert mock.last("POST", "/v1/ui/rescan").body == {"remotes": ["gdrive", "dropbox"]}
    expect(ui.tid("setup-files-gdrive")).to_have_text("1,200 files")
    expect(ui.tid("setup-scan-dropbox")).to_have_attribute("data-kind", "waiting")
    expect(ui.tid("setup-scan-all")).to_be_disabled()
    mock.scan_script = [(running("dropbox", 2, 300), scanned("gdrive"))]
    expect(ui.tid("setup-files-gdrive")).to_have_text("41,203 files")
    expect(ui.tid("setup-files-dropbox")).to_have_text("300 files")
    expect(ui.tid("setup-scan-text")).to_contain_text("Scanning dropbox (2/2)")
    mock.scan_script = [({"id": "scan-1", "kind": "rescan", "status": "running", "result": None, "error": None,
                          "progress": {"stage": "identity", "remotes": ["gdrive", "dropbox"], "remotes_total": 2}}, scanned("gdrive", "dropbox"))]
    expect(ui.tid("setup-scan-text")).to_contain_text("content identities")
    mock.scan_script = [({"id": "scan-1", "kind": "rescan", "status": "done", "progress": {}, "error": None,
                          "result": {"scans": [], "identity": {}}}, scanned("gdrive", "dropbox"))]
    expect(ui.tid("setup-step-scan")).to_have_attribute("data-state", "done")
    expect(ui.tid("setup-step-ready")).to_have_attribute("data-state", "done")
    expect(ui.tid("setup-open-overview")).to_be_enabled()
    expect(ui.tid("toast").first).to_contain_text("Scan finished")


def test_a_failed_scan_says_so(make_ui):
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive")], scanned=False)
    ui.mock.scan_script = [({"id": "scan-1", "kind": "rescan", "status": "failed", "progress": {}, "result": None,
                             "error": "scan did not complete for: gdrive"}, None)]
    ui.tid("setup-scan-one-gdrive").click()
    assert ui.mock.last("POST", "/v1/ui/rescan").body == {"remotes": ["gdrive"]}
    expect(ui.tid("setup-scan-failed")).to_contain_text("did not finish")
    expect(ui.tid("setup-step-scan")).to_have_attribute("data-state", "attention")


def test_enabling_changes_on_drive_goes_through_a_second_sign_in_and_on_dropbox_is_immediate(make_ui):
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive"), ("dropbox", "dropbox")], scanned=True, via_route=True)
    ui.mock.enable_response = (202, {"job_id": "setup-1"})
    ui.mock.connect_view = SIGNIN
    ui.tid("setup-enable-gdrive").click()
    expect(ui.tid("dialog")).to_contain_text("gdrive-rw")
    ui.tid("dialog-ok").click()
    assert ui.mock.last("POST", "/v1/ui/setup/enable-write").body == {"name": "gdrive"}
    expect(ui.tid("connect-signin-link")).to_be_visible()
    ui.mock.connect_view = _job({}, status="done", result={"remote": {"name": "gdrive", "write": "gdrive-rw:"}})
    expect(ui.tid("connect-done")).to_contain_text("a plan you approve can now change")
    expect(ui.tid("connect-scan")).to_have_count(0)
    ui.tid("connect-close").click()
    expect(ui.tid("connect-dialog")).to_have_count(0)
    ui.mock.enable_response = (200, {"remote": {"name": "dropbox", "write": "dropbox:"}})
    ui.tid("setup-enable-dropbox").click()
    ui.tid("dialog-ok").click()
    assert ui.mock.last("POST", "/v1/ui/setup/enable-write").body == {"name": "dropbox"}
    expect(ui.tid("toast").first).to_contain_text("dropbox can now be changed")


def test_without_approvals_the_screen_is_read_only_and_shows_commands_and_a_remotes_block(make_ui):
    ui = _setup_page(make_ui, approvals_enabled=False, clouds=[("gdrive", "drive")], scanned=True, loose=[("work-od", "onedrive")], via_route=True)
    expect(ui.tid("setup-readonly")).to_be_visible()
    expect(ui.tid("setup-cmd-rclone")).to_have_text("rclone config")
    block = ui.tid("setup-cmd-remotes").inner_text()
    assert '[[remotes]]' in block and 'name = "work-od"' in block and 'provider = "onedrive"' in block and 'read = "work-od:"' in block
    expect(ui.tid("setup-cmd-scan")).to_contain_text("drivemerge scan --all")
    for testid in ("setup-connect", "setup-scan-all", "setup-scan-one-gdrive", "setup-enable-gdrive", "setup-adopt-add-work-od"):
        expect(ui.tid(testid)).to_have_count(0)
    expect(ui.tid("setup-test-gdrive")).to_be_enabled()                     # testing only reads
    assert not [c for c in ui.mock.calls if c.method == "POST" and "/setup/" in c.path]


def test_setup_speaks_both_languages_and_switches_without_a_reload(make_ui):
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive")], scanned=True, locale="zh-TW", via_route=True)
    expect(ui.tid("setup-state-rclone")).to_have_text("已完成")
    expect(ui.tid("setup-connect")).to_have_text(re.compile("連接雲端"))
    ui.tid("view-lang").click()
    expect(ui.tid("setup-state-rclone")).to_have_text("Done")
    expect(ui.tid("setup-connect")).to_have_text(re.compile("Connect a cloud"))
    ui.tid("setup-connect").click()
    expect(ui.tid("connect-start")).to_have_text("Sign in")


def test_the_overview_empty_state_links_to_setup(make_ui):
    ui = make_ui(locale="en-US").open()
    ui.mock.coverage_override = {"remotes": [], "totals": {"contents": 0, "bytes": 0, "stored_bytes": 0, "redundant_bytes": 0, "copies": 0},
                                 "intersections": [], "other": None}
    ui.go("#/overview")
    ui.tid("ov-setup-link").click()
    expect(ui.tid("view-setup")).to_be_visible()


def test_the_keys_setup_builds_at_run_time_exist_in_both_languages(make_ui):
    ui = make_ui(locale="en-US")
    ui.page.goto(f"{ui.base}/ui/#token={MockApi.TOKEN}")
    dicts = ui.page.evaluate("import('/ui/static/i18n.js').then(m => m.dictionaries)")
    keys = [f"setup.provider.{p}" for p in ("drive", "dropbox", "onedrive", "iclouddrive", "local", "other")]
    keys += ["connect.field.apple_id", "connect.field.password", "setup.enable.textDrive", "setup.enable.textOther", "prog.stage.identity", "prog.stage.dir_stats"]
    sent = {"BAD_NAME": set(), "REMOTE_EXISTS": {"name"}, "REMOTE_NOT_FOUND": {"name"}, "CONFIG_LAYOUT": set(), "RCLONE_MISSING": {"binary"},
            "NOT_WAITING": set(), "NO_CONFIG_PATH": set(), "SIGNIN_TIMEOUT": set(), "CANCELLED": set(), "RCLONE_ERROR": set(),
            "CONFIG_WRITE": set(), "AUTH_EXPIRED": set(), "NOT_FOUND": set(), "NETWORK": set(), "MISSING_FIELD": {"field"}}
    for lang in ("zh-TW", "en"):
        for key in keys:
            assert key in dicts[lang], (lang, key)
        for code, params in sent.items():
            raw = dicts[lang][f"err.{code}"]
            assert set(re.findall(r"\{(\w+)\}", raw)) <= params, (lang, code)
    for code in sent:
        if code not in ("BAD_NAME", "NOT_FOUND"):
            assert re.search(r"[一-鿿]", dicts["zh-TW"][f"err.{code}"]) and not re.search(r"[一-鿿]", dicts["en"][f"err.{code}"]), code


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("locale", ["en-US", "zh-TW"])
@pytest.mark.parametrize("width", [1440, 900])
def test_screenshots_of_the_setup_screen(make_ui, scheme, locale, width):
    SHOTS.mkdir(parents=True, exist_ok=True)
    tag = f"{scheme}-{locale}-{width}"

    ui = _setup_page(make_ui, locale=locale, color_scheme=scheme, width=width, loose=[("work-od", "onedrive")])
    page = ui.page

    def snap(name: str) -> None:
        page.screenshot(path=str(SHOTS / f"setup-{name}-{tag}.png"))
        assert (SHOTS / f"setup-{name}-{tag}.png").stat().st_size > 5_000

    snap("fresh")
    ui.mock.connect_view = SIGNIN
    ui.tid("setup-connect").click()
    snap("connect")
    ui.tid("connect-start").click()
    expect(ui.tid("connect-signin")).to_be_visible()
    snap("signin")
    ui.mock.connect_view = _job(_question(
        name="config_driveid", exclusive=True, default="a1", help="Select a drive to use.\nThe personal drive is the default.",
        examples=[{"value": "a1", "help": "OneDrive (personal)"}, {"value": "b2", "help": "Documents (business)"}]))
    expect(ui.tid("connect-question")).to_be_visible()
    snap("question")
    ui.mock.connect_view = _job({}, status="done", result={"remote": {"name": "gdrive", "write": None}})
    expect(ui.tid("connect-done")).to_be_visible()
    ui.mock.make_setup(clouds=[("gdrive", "drive"), ("dropbox", "dropbox")], scanned=False)
    ui.tid("connect-close").click()
    ui.tid("setup-refresh").click()
    expect(ui.tid("setup-cloud-gdrive")).to_be_visible()
    ui.mock.scan_script = [({"id": "scan-1", "kind": "rescan", "status": "running", "result": None, "error": None,
                             "progress": {"remotes": ["gdrive", "dropbox"], "remotes_total": 2, "remote": "dropbox", "remote_index": 2,
                                          "files": 12004, "dirs": 0, "bytes": 0}}, None)]
    ui.tid("setup-scan-all").click()
    expect(ui.tid("setup-files-dropbox")).to_have_text(re.compile("12,004"))
    snap("scanning")
    ui.mock.make_setup(clouds=[("gdrive", "drive"), ("dropbox", "dropbox")], scanned=True)
    ui.mock.scan_script = [({"id": "scan-1", "kind": "rescan", "status": "done", "progress": {}, "error": None, "result": {}}, None)]
    expect(ui.tid("setup-step-ready")).to_have_attribute("data-state", "done")
    snap("done")
    ui2 = _setup_page(make_ui, locale=locale, color_scheme=scheme, width=width, approvals_enabled=False, loose=[("work-od", "onedrive")])
    ui2.page.screenshot(path=str(SHOTS / f"setup-readonly-{tag}.png"))


def test_icloud_asks_for_the_apple_id_and_password_up_front_and_keeps_them_out_of_the_url_storage_and_toasts(make_ui):
    password = "apple-Pa55-word"
    ui = _setup_page(make_ui)
    ui.mock.connect_view = _job({"stage": "starting", "name": "icloud", "provider": "iclouddrive", "mode": "new"})
    ui.tid("setup-connect").click()
    ui.tid("connect-provider-iclouddrive").check()
    expect(ui.tid("connect-apple-password")).to_have_attribute("type", "password")
    expect(ui.tid("connect-start")).to_be_disabled()                       # both fields are required
    ui.tid("connect-apple-id").fill("me@example.com")
    ui.tid("connect-apple-password").fill(password)
    before = ui.page.url
    ui.tid("connect-start").click()
    body = ui.mock.last("POST", "/v1/ui/setup/connect").body
    assert body == {"provider": "iclouddrive", "name": "icloud", "apple_id": "me@example.com", "password": password}
    ui.mock.connect_view = _job({**_question(name="config_2fa", required=True, help="2FA code"), "name": "icloud",
                                 "provider": "iclouddrive", "mode": "new", "retry": True})
    expect(ui.tid("connect-q-retry")).to_contain_text("did not accept")
    assert ui.page.url == before and password not in ui.page.url
    assert password not in ui.page.evaluate("JSON.stringify([localStorage, sessionStorage])")
    assert password not in ui.page.locator("body").inner_text()
    assert all(password not in c.path and password not in json.dumps(c.query) for c in ui.mock.calls)


def test_remote_auth_is_offered_as_an_option_and_sent_only_when_ticked(make_ui):
    ui = _setup_page(make_ui)
    ui.mock.connect_view = SIGNIN
    ui.tid("setup-connect").click()
    ui.tid("connect-remote-auth").check()
    ui.tid("connect-start").click()
    assert ui.mock.last("POST", "/v1/ui/setup/connect").body == {"provider": "drive", "name": "gdrive", "remote_auth": True}


def test_a_busy_server_and_a_missing_field_are_worded_on_the_setup_screen(make_ui):
    ui = _setup_page(make_ui, clouds=[("gdrive", "drive")], scanned=False)
    ui.mock.connect_refusal = (400, "MISSING_FIELD", {"field": "apple_id"})
    ui.tid("setup-connect").click()
    ui.tid("connect-start").click()
    expect(ui.tid("connect-error")).to_contain_text('"Apple ID" is required')
    ui.mock.connect_refusal = (409, "JOB_BUSY", {})
    ui.tid("connect-start").click()
    expect(ui.tid("connect-error")).to_contain_text("sign-in that is still open")
