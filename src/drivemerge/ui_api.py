"""Server for the visual workspace: the REST API plus a browser-only human channel.

Two kinds of caller reach this app, and they are not equal:

* normal routes (catalog, drafts, trees, suggestions, previews) accept the API
  bearer token or the UI session cookie, so scripts and Claude can stage and
  preview changes alongside the person using the browser;
* the human channel (approve, execute, rescan) accepts only the session cookie
  plus a CSRF header, a same-origin check, and a server that was started from an
  interactive terminal. A bearer token never reaches it: whatever an LLM can do
  here ends in a proposal that a person still has to approve.

Workspace and suggestion modules are imported when a request needs them.
"""

from __future__ import annotations

import getpass
import hmac
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, Body, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from . import __version__, plans
from .api import LOOPBACK_HOSTS, bearer_ok, build_router, install_error_handlers
from .config import Config
from .models import Op, Plan
from .service import Service

SESSION_COOKIE = "dm_session"
MAX_SESSIONS = 32          # token holders only, but a loop must not grow memory without bound
KEEP_JOBS = 50
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_TEXT = "; charset=utf-8"
_MEDIA = {".html": "text/html" + _TEXT, ".js": "text/javascript" + _TEXT, ".mjs": "text/javascript" + _TEXT,
          ".css": "text/css" + _TEXT, ".json": "application/json", ".svg": "image/svg+xml",
          ".map": "application/json", ".woff2": "font/woff2"}
# The page and its modules come from this origin only: file names are attacker-controlled text, so
# nothing inline may run and nothing may be sent elsewhere.
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'")


class UiError(Exception):
    """A refusal with a machine-readable code; rendered as {"error", "code"}."""

    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.headers = status, code, headers


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionBody(_Body):
    token: str


class DraftBody(_Body):
    title: str = ""


class ApproveBody(_Body):
    code: str


class RescanBody(_Body):
    remotes: list[str] | None = None


def _refusal(status: int, payload: dict, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=headers)


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry, odd containers
        return "unknown"


def _describe(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


# --------------------------------------------------------------------------- #
# Sessions and jobs
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Session:
    id: str
    csrf: str


@dataclass(frozen=True)
class _Auth:
    """Who made a request: the browser session (`session` set) or the API bearer token."""
    kind: str
    session: _Session | None = None


class _Sessions:
    """In-memory sessions; they die with the process, like the one-time UI token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, _Session] = {}

    def create(self) -> _Session:
        session = _Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        with self._lock:
            while len(self._by_id) >= MAX_SESSIONS:
                del self._by_id[next(iter(self._by_id))]
            self._by_id[session.id] = session
        return session

    def get(self, session_id: str | None) -> _Session | None:
        if not session_id:
            return None
        with self._lock:
            return self._by_id.get(session_id)


@dataclass
class _Job:
    id: str
    kind: str
    status: str = "running"
    progress: dict = field(default_factory=dict)
    result: Any = None
    error: str | None = None


class _Jobs:
    """Background jobs. One runs at a time: an execution and a rescan both write the catalog."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}

    def busy(self) -> _Job | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.status == "running"), None)

    def start(self, kind: str, work: Callable[[_Job], tuple[Any, str | None]], **progress: Any) -> _Job:
        """Run `work(job)` in a thread; it returns (result, error). Raises UiError(409) while another job runs."""
        with self._lock:
            running = next((j for j in self._jobs.values() if j.status == "running"), None)
            if running is not None:
                raise UiError(409, "JOB_BUSY", f"a {running.kind} job ({running.id}) is still running; "
                                               "wait for it to finish")
            job = _Job(secrets.token_hex(6), kind, progress=dict(progress))
            self._jobs[job.id] = job
            for old in [j.id for j in self._jobs.values() if j.status != "running"][:-KEEP_JOBS]:
                del self._jobs[old]
        threading.Thread(target=self._run, args=(job, work), name=f"drivemerge-{kind}-{job.id}",
                         daemon=True).start()
        return job

    def _run(self, job: _Job, work: Callable[[_Job], tuple[Any, str | None]]) -> None:
        try:
            result, error = work(job)
        except Exception as e:  # a job must always end in done or failed
            result, error = None, _describe(e)
        with self._lock:
            job.result, job.error = result, error
            job.status = "failed" if error else "done"

    def update(self, job: _Job, **progress: Any) -> None:
        with self._lock:
            job.progress.update(progress)

    def view(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"no job {job_id}")
            return {"id": job.id, "kind": job.kind, "status": job.status, "progress": dict(job.progress),
                    "result": job.result, "error": job.error}


def _reports(jobs: _Jobs, job: _Job) -> Callable[..., None]:
    """Executor progress callback: `progress(done, total, op)`. Any other scalars it is given are kept too;
    the authoritative progress is still GET /v1/plans/{id}/progress."""
    count = 0

    def report(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        count += 1
        flat: dict[str, Any] = {}
        words = []
        if len(args) >= 2 and all(type(a) is int for a in args[:2]):
            flat.update(done=args[0], total=args[1])
            args = args[2:]
        for a in (*args, kwargs):
            if isinstance(a, Op):
                flat.update(op=a.op, seq=a.seq, src_path=a.src_path, dst_path=a.dst_path)
                continue
            if is_dataclass(a) and not isinstance(a, type):
                a = asdict(a)
            if isinstance(a, dict):
                flat.update({str(k): v for k, v in a.items() if v is None or isinstance(v, (str, int, float, bool))})
            elif a is not None:
                words.append(str(a))
        if words:
            flat["detail"] = " ".join(words)
        jobs.update(job, **flat, reports=count)

    return report


# --------------------------------------------------------------------------- #
# Helpers shared by routes
# --------------------------------------------------------------------------- #

def _stale_reason(conn, plan: Plan) -> str | None:
    """Why a draft's plan no longer matches the draft; None for other plans and current previews."""
    draft_id = plan.params.get("draft_id")
    if draft_id is None:
        return None
    row = conn.execute("SELECT status, version FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        return f"draft {draft_id} no longer exists"
    if row[0] != "open":
        return f"draft {draft_id} is {row[0]}"
    if row[1] != plan.params.get("draft_version"):
        return f"draft {draft_id} changed after this preview was made"
    return None


def _own_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}".lower()


def _foreign_origin(request: Request) -> bool:
    """True when the browser says the request comes from some other origin. No Origin header: not browser-made."""
    origin = request.headers.get("origin")
    return origin is not None and origin.lower() != _own_origin(request)


def _disabled_message(request: Request) -> str:
    path = request.url.path
    plan_id = request.path_params.get("plan_id", "N")
    if path.endswith("/approve"):
        cli = f"`drivemerge plan approve {plan_id}`"
    elif path.endswith("/execute"):
        cli = f"`drivemerge plan execute {plan_id}`"
    else:
        cli = "`drivemerge scan --all`"
    return ("approving, executing and rescanning from the browser is off: `drivemerge ui` must be started from "
            "an interactive terminal, outside an AI agent. In your own terminal run "
            f"{cli} (plans are approved with `drivemerge plan approve ID` and run with `drivemerge plan execute ID`)")


def _json_detail(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _resolve_static(root: Path, rel: str) -> Path | None:
    """The file under `root` for a URL path, or None. Anything that could leave `root` is refused."""
    if not rel or "\x00" in rel or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or ":" in p for p in parts):
        return None
    base = root.resolve()
    target = (base / rel).resolve()          # follows symlinks, so a link out of `root` fails the next check
    if base not in target.parents or not target.is_file():
        return None
    return target


def _web_root() -> Path:
    return Path(str(resources.files("drivemerge").joinpath("web")))


def _static_response(root: Path, rel: str) -> FileResponse:
    path = _resolve_static(root, rel)
    if path is None:
        raise UiError(404, "NOT_FOUND", "not found")
    media = _MEDIA.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
    if path.suffix.lower() == ".html":
        headers["Content-Security-Policy"] = _CSP
    return FileResponse(path, media_type=media, headers=headers)


# --------------------------------------------------------------------------- #
# The app
# --------------------------------------------------------------------------- #

def create_ui_app(service: Service, *, config: Config, api_token: str, ui_token: str,
                  approvals_enabled: bool) -> FastAPI:
    """FastAPI app: REST routes, workspace routes, the human channel and the single-page app.

    `ui_token` is exchanged once per browser for a session cookie. `api_token` may be empty, which
    leaves the browser session as the only way in. `approvals_enabled` is decided by whoever
    started the process (see `approvals_allowed`); when false the human channel answers 403.
    """
    from . import workspace

    if not ui_token or not ui_token.strip():
        raise ValueError("the UI token is empty")
    if config.db_path in ("", ":memory:"):
        # Executions and rescans run on their own connection; to an in-memory catalog that would be a new, empty one.
        raise ValueError("the workspace needs a catalog stored in a file ([database] path)")
    ui_expected = ui_token.encode()
    bearer = api_token.encode() if api_token and api_token.strip() else None
    lock = threading.Lock()  # one shared SQLite connection: one service call at a time
    sessions = _Sessions()
    jobs = _Jobs()
    web = _web_root()

    app = FastAPI(title="drive-merge workspace", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    def ui_error(_: Request, exc: UiError) -> JSONResponse:
        return _refusal(exc.status, {"error": str(exc), "code": exc.code}, exc.headers)

    def workspace_error(_: Request, exc: Exception) -> JSONResponse:
        code = getattr(exc, "code", "CONFLICT")
        status = 404 if code == "NOT_FOUND" else 400 if code == "BAD_REQUEST" else 409
        return _refusal(status, {"error": str(exc), "code": code})

    def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return _refusal(422, {"error": problems})

    app.add_exception_handler(UiError, ui_error)
    app.add_exception_handler(workspace.WorkspaceError, workspace_error)
    app.add_exception_handler(RequestValidationError, invalid_request)

    # ---- authentication

    def session_of(request: Request) -> _Session | None:
        return sessions.get(request.cookies.get(SESSION_COOKIE))

    def require_auth(request: Request) -> _Auth:
        session = session_of(request)
        if session is not None:
            # SameSite=Strict keeps other sites out; this also stops a page on another local port.
            if request.method not in _SAFE_METHODS and _foreign_origin(request):
                raise UiError(403, "BAD_ORIGIN", "request origin does not match this server")
            return _Auth("session", session)
        if bearer_ok(request.headers.get("authorization"), bearer):
            return _Auth("bearer")
        raise UiError(401, "UNAUTHENTICATED", "missing or invalid credentials",
                      headers={"WWW-Authenticate": "Bearer"})

    def require_human(request: Request) -> _Session:
        session = session_of(request)
        if session is None:
            if bearer_ok(request.headers.get("authorization"), bearer):
                raise UiError(403, "HUMAN_ONLY", "approving, executing and rescanning are only possible from the "
                                                 "browser session opened by `drivemerge ui`, not with the API token")
            raise UiError(401, "UNAUTHENTICATED", "missing or invalid credentials",
                          headers={"WWW-Authenticate": "Bearer"})
        if not approvals_enabled:
            raise UiError(403, "APPROVALS_DISABLED", _disabled_message(request))
        sent = request.headers.get("x-csrf-token")
        if not sent or not hmac.compare_digest(sent.encode(), session.csrf.encode()):
            raise UiError(403, "BAD_CSRF", "missing or invalid X-CSRF-Token header")
        if _foreign_origin(request):
            raise UiError(403, "BAD_ORIGIN", "request origin does not match this server")
        return session

    normal = [Depends(require_auth)]
    router = APIRouter(dependencies=normal)
    human = APIRouter(dependencies=[Depends(require_human)])

    # ---- open routes

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/ui/session")
    def open_session(body: SessionBody, response: Response) -> dict:
        if not hmac.compare_digest(body.token.encode(), ui_expected):
            raise UiError(401, "BAD_TOKEN", "invalid UI token; open the URL printed by `drivemerge ui`")
        session = sessions.create()
        response.set_cookie(SESSION_COOKIE, session.id, httponly=True, samesite="strict", path="/", secure=False)
        return {"csrf": session.csrf, "approvals_enabled": approvals_enabled}

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/ui/")

    @app.get("/ui/", include_in_schema=False)
    def index() -> FileResponse:
        return _static_response(web, "index.html")

    @app.get("/ui/static/{path:path}", include_in_schema=False)
    def asset(path: str) -> FileResponse:
        return _static_response(web, path)

    # ---- configuration and catalog

    @router.get("/v1/ui/config")
    def ui_config(auth: _Auth = Depends(require_auth)) -> dict:
        with lock:
            rows = service.conn.execute(
                "SELECT id, name, provider, write_remote, case_insensitive FROM remotes ORDER BY id").fetchall()
        return {
            "approvals_enabled": approvals_enabled,
            "execution_enabled": config.safety.execution_enabled,
            "remotes": [{"id": r[0], "name": r[1], "provider": r[2], "writable": r[3] is not None,
                         "case_insensitive": bool(r[4])} for r in rows],
            "version": __version__,
            # Lets a reloaded page recover its CSRF token without the one-time URL token; only a session gets it.
            "csrf": auth.session.csrf if auth.session else None,
        }

    @router.get("/v1/tree/roots")
    def tree_roots(draft: int | None = None) -> dict:
        with lock:
            return {"roots": workspace.roots(service.conn, draft_id=draft)}

    @router.get("/v1/tree")
    def tree(remote: str, path: str = "", draft: int | None = None, view: str = "after",
             ghosts: bool = False, limit: int = 500, offset: int = 0) -> dict:
        with lock:
            return workspace.list_dir(service.conn, remote, path, draft_id=draft, view=view, ghosts=ghosts,
                                      limit=min(limit, 5000), offset=offset)

    # ---- drafts

    @router.get("/v1/drafts")
    def list_drafts(status: str = "open") -> dict:
        with lock:
            return {"drafts": workspace.list_drafts(service.conn, status=None if status == "all" else status)}

    @router.post("/v1/drafts", status_code=201)
    def create_draft(body: DraftBody = DraftBody()) -> dict:
        with lock:
            return workspace.create_draft(service.conn, body.title)

    @router.get("/v1/drafts/{draft_id}")
    def get_draft(draft_id: int) -> dict:
        with lock:
            return workspace.get_draft(service.conn, draft_id)

    @router.delete("/v1/drafts/{draft_id}")
    def discard_draft(draft_id: int) -> dict:
        with lock:
            workspace.discard_draft(service.conn, draft_id)
        return {"ok": True}

    @router.post("/v1/drafts/{draft_id}/actions", status_code=201)
    def add_action(draft_id: int, action: dict[str, Any] = Body(...), auth: _Auth = Depends(require_auth)) -> dict:
        with lock:
            return workspace.add_action(service.conn, draft_id, action,
                                        created_by="ui" if auth.session else "api")

    @router.post("/v1/drafts/{draft_id}/undo")
    def undo(draft_id: int) -> dict:
        with lock:
            return workspace.undo(service.conn, draft_id)

    @router.post("/v1/drafts/{draft_id}/redo")
    def redo(draft_id: int) -> dict:
        with lock:
            return workspace.redo(service.conn, draft_id)

    @router.delete("/v1/drafts/{draft_id}/actions/{action_id}")
    def remove_action(draft_id: int, action_id: int) -> dict:
        with lock:
            return workspace.remove_action(service.conn, draft_id, action_id)

    @router.post("/v1/drafts/{draft_id}/preview")
    def preview(draft_id: int, auth: _Auth = Depends(require_auth)) -> dict:
        with lock:
            result = service.preview_draft(draft_id)
        result = {k: v for k, v in result.items() if k != "approval_code"}
        sim = result.get("simulation")
        if isinstance(sim, dict):
            # The first 8 characters of the digest are what a person types to approve; nobody else gets
            # the digest, and only a browser session that may approve gets the code.
            digest = sim.get("digest")
            result["simulation"] = {k: v for k, v in sim.items() if k != "digest"}
            if (auth.session is not None and approvals_enabled and sim.get("ok") and result.get("ok", True)
                    and digest):
                result["approval_code"] = plans.short_code(digest)
        return result

    @router.get("/v1/suggestions")
    def suggestions(kind: str | None = None, remote: str | None = None, limit: int = 20) -> dict:
        if kind not in (None, "related", "duplicates"):
            raise ValueError("kind must be 'related' or 'duplicates'")
        from . import suggest
        limit = max(0, min(limit, 200))
        found: list[dict] = []
        with lock:
            if kind in (None, "related"):
                found += suggest.related_folders(service.conn, remote=remote, limit=limit)
            if kind in (None, "duplicates"):
                found += suggest.duplicate_folders(service.conn, remote=remote, limit=limit)
        return {"suggestions": found}

    # ---- plans and jobs

    @router.get("/v1/plans/{plan_id}/progress")
    def plan_progress(plan_id: int, journal: int = 20) -> dict:
        with lock:
            conn = service.conn
            plan = plans.get_plan(conn, plan_id, with_ops=False)
            counts = {r[0]: r[1] for r in conn.execute(
                "SELECT status, COUNT(*) FROM plan_ops WHERE plan_id = ? GROUP BY status", (plan_id,))}
            current = None
            if plan.status == "executing":
                row = conn.execute(
                    "SELECT o.seq, o.op, sr.name, o.src_path, dr.name, o.dst_path FROM plan_ops o "
                    "JOIN remotes sr ON sr.id = o.src_remote_id LEFT JOIN remotes dr ON dr.id = o.dst_remote_id "
                    "WHERE o.plan_id = ? AND o.status = 'pending' ORDER BY o.seq LIMIT 1", (plan_id,)).fetchone()
                if row is not None:
                    current = {"seq": row[0], "op": row[1], "src": {"remote": row[2], "path": row[3]},
                               "dst": {"remote": row[4], "path": row[5]} if row[4] is not None else None}
            rows = conn.execute(
                "SELECT j.id, j.ts, j.action, j.op_id, o.seq, j.detail FROM journal j "
                "LEFT JOIN plan_ops o ON o.id = j.op_id WHERE j.plan_id = ? ORDER BY j.id DESC LIMIT ?",
                (plan_id, max(0, min(journal, 200)))).fetchall()
        return {
            "status": plan.status,
            "ops_total": sum(counts.values()),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "pending": counts.get("pending", 0),
            "current": current,
            "recent_journal": [{"id": r[0], "ts": r[1], "action": r[2], "op_id": r[3], "op_seq": r[4],
                                "detail": _json_detail(r[5])} for r in reversed(rows)],
        }

    @router.get("/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        return jobs.view(job_id)

    # ---- human channel

    @human.post("/v1/ui/plans/{plan_id}/approve")
    def approve(plan_id: int, body: ApproveBody) -> dict:
        with lock:
            plan = plans.get_plan(service.conn, plan_id, with_ops=False)
            stale = _stale_reason(service.conn, plan)
            if stale:
                raise UiError(409, "STALE_PREVIEW", f"{stale}; preview the draft again before approving")
            plans.approve_plan(service.conn, plan_id, body.code.strip().lower(),
                               approved_by=f"ui:{_current_user()}")
        return {"ok": True}

    @human.post("/v1/ui/plans/{plan_id}/execute", status_code=202)
    def execute(plan_id: int) -> dict:
        busy = jobs.busy()
        if busy is not None:
            raise UiError(409, "JOB_BUSY", f"a {busy.kind} job ({busy.id}) is still running; wait for it to finish")
        if not config.safety.execution_enabled:
            raise UiError(403, "EXECUTION_DISABLED", "execution is disabled: set `execution_enabled = true` under "
                                                     "[safety] in config.toml, then restart `drivemerge ui`")
        with lock:
            plan = plans.get_plan(service.conn, plan_id, with_ops=False)
            if plan.status != "approved":
                raise UiError(409, "NOT_APPROVED", f"plan {plan_id} is {plan.status}; "
                                                   "only an approved plan can be executed")
            stale = _stale_reason(service.conn, plan)
            if stale:
                raise UiError(409, "STALE_PREVIEW", f"{stale}; preview and approve it again before executing")

        def work(job: _Job) -> tuple[Any, str | None]:
            svc = Service(config)  # own connection and rclone: this thread never touches the request connection
            try:
                result = svc.execute(plan_id, progress=_reports(jobs, job))  # also closes a finished draft
                failed = result.get("status") == "failed" or result.get("error")
                return result, (str(result.get("error") or f"plan {plan_id} failed") if failed else None)
            finally:
                svc.conn.close()

        return {"job_id": jobs.start("execute", work, plan_id=plan_id).id}

    @human.post("/v1/ui/rescan", status_code=202)
    def rescan(body: RescanBody = RescanBody()) -> dict:
        names = list(dict.fromkeys(body.remotes or [r.name for r in config.remotes]))
        if not names:
            raise ValueError("no remotes configured")
        for name in names:
            config.remote(name)  # KeyError -> 404 before any job starts

        def work(job: _Job) -> tuple[Any, str | None]:
            svc = Service(config)
            try:
                scans = []
                for i, name in enumerate(names, start=1):
                    jobs.update(job, remote=name, remote_index=i, files=0, dirs=0, bytes=0)
                    try:
                        res = svc.scan(name, progress=lambda files, dirs, size: jobs.update(
                            job, files=files, dirs=dirs, bytes=size))
                    except Exception as e:  # keep going: the other clouds still deserve a fresh catalog
                        res = {"status": "failed", "error": _describe(e)}
                    res.setdefault("remote", name)
                    scans.append(res)
                jobs.update(job, stage="identity")
                identity = svc.rebuild_identity()  # also refreshes the folder statistics
                bad = [s["remote"] for s in scans if s.get("status") != "complete" or s.get("error")]
                return ({"scans": scans, "identity": identity},
                        f"scan did not complete for: {', '.join(bad)}" if bad else None)
            finally:
                svc.conn.close()

        return {"job_id": jobs.start("rescan", work, remotes=names, remotes_total=len(names)).id}

    app.include_router(build_router(service, lock, dependencies=normal))
    app.include_router(router)
    app.include_router(human)
    return app


# --------------------------------------------------------------------------- #
# Launching
# --------------------------------------------------------------------------- #

def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def approvals_allowed(*, stdin: Any = None, stdout: Any = None, env: Any = None) -> tuple[bool, str | None]:
    """Whether this process may offer browser approvals, and why not.

    A person at a terminal can; an agent cannot, since it has no terminal and its harness sets
    CLAUDECODE or AI_AGENT. Without approvals the workspace is still useful for proposing changes.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    env = os.environ if env is None else env
    reasons = []
    if not (_isatty(stdin) and _isatty(stdout)):
        reasons.append("stdin and stdout are not both an interactive terminal")
    for name in ("CLAUDECODE", "AI_AGENT"):
        if name in env:
            reasons.append(f"{name} is set, so an AI agent seems to have started this")
    return not reasons, "; ".join(reasons) or None


def _open_when_ready(host: str, port: int, url: str, timeout: float = 15.0) -> None:
    """Open the browser once the server accepts connections, so the first request does not fail."""
    def run() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                socket.create_connection((host, port), timeout=0.3).close()
                break
            except OSError:
                time.sleep(0.1)
        try:
            webbrowser.open(url)
        except Exception:  # no browser here; the URL is printed anyway
            pass

    threading.Thread(target=run, name="drivemerge-open-browser", daemon=True).start()


def run_ui(config: Config, *, host: str | None = None, port: int | None = None, open_browser: bool = True) -> None:
    """Serve the workspace on loopback and print the one-time URL that opens a browser session."""
    host = host or config.api.host
    port = config.api.port if port is None else port
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing to bind {host!r}: the workspace is loopback-only")
    approvals, why_not = approvals_allowed()
    ui_token = secrets.token_urlsafe(24)
    app = create_ui_app(Service(config), config=config, api_token=config.api.token, ui_token=ui_token,
                        approvals_enabled=approvals)
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    url = f"http://{netloc}/ui/#token={ui_token}"
    print(f"drive-merge workspace: {url}", flush=True)
    if approvals:
        print("Approving and executing from the browser: enabled", flush=True)
    else:
        print(f"Approving and executing from the browser: DISABLED ({why_not}).\n"
              "You can still stage and preview changes; approve them with `drivemerge plan approve ID` and "
              "run them with `drivemerge plan execute ID` in your own terminal.", flush=True)
    if not config.safety.execution_enabled:
        print("Execution is disabled ([safety] execution_enabled = false): plans can be previewed and "
              "approved but not run.", flush=True)
    if open_browser:
        _open_when_ready(host, port, url)
    # proxy_headers off: nothing sits in front of this server, so X-Forwarded-* must not change the scheme.
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=False)


__all__ = ["create_ui_app", "run_ui", "approvals_allowed", "SESSION_COOKIE"]
