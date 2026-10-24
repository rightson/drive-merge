"""Server for the visual workspace: the REST API plus a browser-only human channel.

Two kinds of caller reach this app, and they are not equal:

* normal routes (catalog, drafts, trees, suggestions, previews) accept the API
  bearer token or the UI session cookie, so scripts and Claude can stage and
  preview changes alongside the person using the browser;
* the human channel (approve, execute, sync, bridge, purge, connect, disconnect, install rclone) accepts only the session cookie
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
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, connections, plans
from .api import (
    LOOPBACK_HOSTS, QuotaCache, bearer_ok, build_router, install_error_handlers, scrub_secrets,
)
from .config import (
    Config, ConfigError, RemoteConfig, add_remote_block, load_config, provider_of, set_remote_write,
)
from .models import Op, Plan
from .rclone import AUTH_URL, Rclone, RcloneCancelled, RcloneError, RcloneTimeout, classify_failure
from .service import Service, draft_of

SESSION_COOKIE = "dm_session"
MAX_SESSIONS = 32          # token holders only, but a loop must not grow memory without bound
KEEP_JOBS = 50
SIGNIN_TIMEOUT_S = 600.0   # rclone waits this long for the browser, and a question waits this long for its answer
ANSWER_TIMEOUT_S = 600.0
CREATE_TIMEOUT_S = 120.0
NEW_REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
ADOPT_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+@-]{0,63}")
MAX_ANSWER = 65536
# What each cloud needs to start signing in: rclone type, and the option values this screen sets itself.
CONNECT_TYPES = {"drive": "drive", "dropbox": "dropbox", "onedrive": "onedrive", "iclouddrive": "iclouddrive"}
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
    """A refusal with a machine-readable code; rendered as {"error", "code"} (and "params" when it has any).

    `message` is English, for terminals and API clients; the page words the code itself, with `params`.
    """

    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None, **params: Any):
        super().__init__(message)
        self.status, self.code, self.headers, self.params = status, code, headers, params


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionBody(_Body):
    token: str


class DraftBody(_Body):
    title: str = ""


class ApproveBody(_Body):
    code: str


class SyncBody(_Body):
    remotes: list[str] | None = None


class ConnectBody(_Body):
    provider: Literal["drive", "dropbox", "onedrive", "iclouddrive"]
    name: str = Field(max_length=200)
    client_id: str | None = Field(default=None, max_length=512)
    client_secret: str | None = Field(default=None, max_length=512)
    apple_id: str | None = Field(default=None, max_length=512)       # iCloud only: rclone needs it to start
    password: str | None = Field(default=None, max_length=512)       # iCloud only; forwarded, never kept
    remote_auth: bool = False        # sign in on another machine: rclone asks for a token made by `rclone authorize`


class AnswerBody(_Body):
    result: str = Field(max_length=MAX_ANSWER)


class NameBody(_Body):
    name: str = Field(max_length=200)


class EnableWriteBody(_Body):
    name: str = Field(max_length=200)
    client_id: str | None = Field(default=None, max_length=512)
    client_secret: str | None = Field(default=None, max_length=512)


RescanBody = SyncBody  # the old name of the sync body (POST /v1/ui/rescan is an alias of /v1/ui/sync)


class BridgeBody(_Body):
    max_bytes: int | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=100000)
    entry_ids: list[int] | None = Field(default=None, max_length=5000)  # verify exactly these files


class ConnectStartBody(_Body):
    provider: str | None = None
    purpose: str = "read"
    name: str | None = None          # purpose 'write': the connection to enable changes on
    client_id: str | None = None
    client_secret: str | None = None


class ConnectFinishBody(_Body):
    name: str | None = None


class PurgeBody(_Body):
    confirm: str = ""
    older_than_days: int = Field(default=0, ge=0, le=36500)


def _refusal(status: int, payload: dict, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=headers)


def _coded(message: str, code: str, params: dict | None = None) -> dict:
    """An error body: the English `error` for people at a terminal, and `code` + `params` for the page to word."""
    body: dict = {"error": message, "code": code}
    if params:
        body["params"] = params
    return body


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


class CodedError(str):
    """A job failure the page words from `code` and `params`; as text it is the English message."""

    def __new__(cls, code: str, message: str, **params: Any):
        self = super().__new__(cls, message)
        self.code, self.params = code, params
        return self


@dataclass
class _Job:
    id: str
    kind: str
    status: str = "running"
    progress: dict = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    code: str | None = None
    params: dict = field(default_factory=dict)


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
                                               "wait for it to finish", job=running.id, kind=running.kind)
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
            job.result, job.error = result, (str(error) if error else error)
            job.code, job.params = getattr(error, "code", None), dict(getattr(error, "params", {}))
            job.status = "failed" if error else "done"

    def update(self, job: _Job, **progress: Any) -> None:
        with self._lock:
            job.progress.update(progress)

    def reset(self, job: _Job, **progress: Any) -> None:
        """Replace the whole progress (a stage change must not keep the last stage's keys)."""
        with self._lock:
            job.progress = dict(progress)

    def get(self, job_id: str) -> _Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"no job {job_id}")
            return job

    def view(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"no job {job_id}")
            view = {"id": job.id, "kind": job.kind, "status": job.status, "progress": dict(job.progress),
                    "result": job.result, "error": job.error}
            if job.code:
                view.update(code=job.code, params=dict(job.params))
            return view


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
# Connecting a cloud: rclone's non-interactive configuration, driven in a thread
# --------------------------------------------------------------------------- #

class _Connect:
    """What the answer and cancel routes need to reach a running connect job. The answer is handed over once and
    never copied anywhere else: it is not part of the job view, the progress or any error."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._answer: str | None = None
        self.waiting = False

    def ask(self) -> None:
        with self._lock:
            self._answer = None
            self._arrived.clear()
            self.waiting = True

    def give(self, answer: str) -> bool:
        with self._lock:
            if not self.waiting:
                return False
            self.waiting = False
            self._answer = answer
            self._arrived.set()
            return True

    def wait(self, timeout: float) -> str:
        """The answer, once it arrives. RcloneCancelled when cancelled; RcloneTimeout when none comes in `timeout`."""
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancel.is_set():
                    raise RcloneCancelled("cancelled while waiting for an answer")
                if self._arrived.wait(0.05):
                    with self._lock:
                        answer, self._answer = self._answer or "", None
                    return answer
                if time.monotonic() > deadline:
                    raise RcloneTimeout("no answer in time")
        finally:
            with self._lock:
                self.waiting = False


def _question(opt: dict) -> dict:
    """The page's view of one rclone question. Only what the form needs; nothing the person typed."""
    examples = opt.get("Examples") or []
    return {
        "name": str(opt.get("Name") or ""),
        "help": str(opt.get("Help") or "")[:4000],
        "default": opt.get("Default") if isinstance(opt.get("Default"), (str, int, float, bool)) else "",
        "examples": [{"value": str(e.get("Value", "")), "help": str(e.get("Help", ""))[:500]}
                     for e in examples[:200] if isinstance(e, dict)],
        "password": bool(opt.get("IsPassword")),
        "required": bool(opt.get("Required")),
        "exclusive": bool(opt.get("Exclusive")),
        "type": str(opt.get("Type") or "string"),
    }


def _connect_failure(exc: BaseException, name: str) -> CodedError:
    """The coded failure for whatever stopped a connect job. Never built from a message that could hold an answer
    (only the stderr tail rclone printed, from which the secrets were already removed)."""
    if isinstance(exc, RcloneCancelled):
        return CodedError("CANCELLED", "the sign-in was cancelled", name=name)
    if isinstance(exc, RcloneTimeout):
        return CodedError("SIGNIN_TIMEOUT", "no sign-in in 10 minutes", name=name)
    if isinstance(exc, ConfigError):
        return CodedError(exc.code, str(exc), **exc.params)
    if isinstance(exc, RcloneError):
        tail = [ln.strip() for ln in exc.stderr.splitlines() if ln.strip()]
        detail = (tail[-1] if tail else "no output")[:300]
        return CodedError("RCLONE_ERROR", f"rclone failed: {detail}", name=name, detail=detail)
    if isinstance(exc, OSError):
        return CodedError("CONFIG_WRITE", "the config file could not be written", name=name)
    return CodedError("RCLONE_ERROR", f"unexpected {type(exc).__name__}", name=name, detail=type(exc).__name__)


def _remote_view(service: Service, name: str) -> dict:
    return next(r for r in service.setup_state()["remotes"] if r["name"] == name)


# --------------------------------------------------------------------------- #
# Helpers shared by routes
# --------------------------------------------------------------------------- #

def _outdated_remotes(conn) -> list[str]:
    """Names of the clouds a plan operation changed after the catalog last finished scanning them.

    The executor never touches the catalog, so after a run the tree describes the cloud as it was; the page
    says so until a scan completes. Read from the journal of executed operations (done, or failed part way)
    rather than from page memory, so a reload, another tab or a run from the terminal show it as well.
    """
    return [r[0] for r in conn.execute(
        """SELECT r.name FROM remotes r WHERE EXISTS (
               SELECT 1 FROM (SELECT src_remote_id AS rid, executed_at AS t FROM plan_ops
                              WHERE status IN ('done', 'failed') AND executed_at IS NOT NULL
                              UNION ALL
                              SELECT dst_remote_id, executed_at FROM plan_ops
                              WHERE status IN ('done', 'failed') AND dst_remote_id IS NOT NULL
                                AND executed_at IS NOT NULL) x
               WHERE x.rid = r.id AND x.t > COALESCE((SELECT MAX(s.finished_at) FROM scans s
                                                      WHERE s.remote_id = r.id AND s.status = 'complete'), ''))
           ORDER BY r.id""")]


def _stale_reason(conn, plan: Plan) -> str | None:
    """Why a draft's plan no longer matches the draft; None for other plans and current previews."""
    draft_id = draft_of(plan)
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
    elif path.endswith("/bridge"):
        cli = "`drivemerge bridge`"
    elif "/setup/" in path:
        return ("connecting, adopting and enabling clouds from the browser is off: `drivemerge ui` must be started "
                "from an interactive terminal, outside an AI agent. In your own terminal run `rclone config`, add a "
                "[[remotes]] entry to config.toml, then run `drivemerge scan --all`")
    elif path.endswith("/purge"):
        cli = f"`drivemerge plan purge {plan_id}`"
    elif "/connect" in path or "/connections" in path:
        cli = "`drivemerge connect` / `drivemerge disconnect` / `drivemerge connections`"
    elif "/rclone/" in path:
        cli = "`drivemerge connect` (it tells you how to install rclone)"
    else:
        cli = "`drivemerge scan --all`"
    return ("approving, executing, syncing, bridging, purging and connecting clouds from the browser is off: "
            "`drivemerge ui` must be started from "
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
                  approvals_enabled: bool, config_path: Path | None = None) -> FastAPI:
    """FastAPI app: REST routes, workspace routes, the human channel and the single-page app.

    `ui_token` is exchanged once per browser for a session cookie. `api_token` may be empty, which
    leaves the browser session as the only way in. `approvals_enabled` is decided by whoever
    started the process (see `approvals_allowed`); when false the human channel answers 403. `config_path` is the
    file the Setup screen may write; without it those routes answer 409 NO_CONFIG_PATH.
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
    connects: dict[str, _Connect] = {}
    web = _web_root()
    if config_path is not None and not service.config_path:
        service.config_path = str(config_path)

    quotas = QuotaCache()

    def job_service() -> Service:
        """A service for a job thread: its own catalog connection and rclone, the same config object and file,
        so reload_config in a job reads the right file and clouds connected meanwhile are seen."""
        return Service(service.config, config_path=service.config_path)

    app = FastAPI(title="drive-merge workspace", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app, coded=True)

    def ui_error(_: Request, exc: UiError) -> JSONResponse:
        return _refusal(exc.status, _coded(str(exc), exc.code, exc.params), exc.headers)

    def workspace_error(_: Request, exc: Exception) -> JSONResponse:
        code = getattr(exc, "code", "CONFLICT")
        status = 404 if code == "NOT_FOUND" else 400 if code == "BAD_REQUEST" else 409
        return _refusal(status, _coded(str(exc), code, getattr(exc, "params", None)))

    def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return _refusal(422, _coded(problems, "BAD_REQUEST"))

    def config_error(_: Request, exc: ConfigError) -> JSONResponse:
        status = {"REMOTE_EXISTS": 409, "REMOTE_NOT_FOUND": 404, "CONFIG_LAYOUT": 409}.get(exc.code, 500)
        return _refusal(status, _coded(str(exc), exc.code, exc.params))

    app.add_exception_handler(UiError, ui_error)
    app.add_exception_handler(ConfigError, config_error)
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
            outdated = _outdated_remotes(service.conn)
            live = {r.name for r in service.config.remotes}
        return {
            "approvals_enabled": approvals_enabled,
            "execution_enabled": service.config.safety.execution_enabled,
            "remotes": [{"id": r[0], "name": r[1], "provider": r[2], "writable": r[3] is not None,
                         "case_insensitive": bool(r[4]),
                         # False for a cloud that was disconnected: the catalog keeps its files as last seen.
                         "connected": r[1] in live} for r in rows],
            "version": __version__,
            # Lets a reloaded page recover its CSRF token without the one-time URL token; only a session gets it.
            "csrf": auth.session.csrf if auth.session else None,
            # Clouds a run changed since their last completed scan: the catalog (and so the tree) is behind them.
            "outdated": outdated,
        }

    @router.get("/v1/ui/setup")
    def setup() -> dict:
        service.rclone_facts()               # runs rclone outside the lock when its answer is not cached
        with lock:
            state = service.setup_state()
        state["needed"] = not state["remotes"] or not state["scanned"]
        return state

    @router.post("/v1/ui/setup/test")
    def setup_test(body: NameBody) -> dict:
        """List the top of one configured cloud and say what went wrong, worded from a code."""
        with lock:
            remote = next((r for r in config.remotes if r.name == body.name), None)
        if remote is None:
            raise UiError(404, "NOT_FOUND", f"no remote {body.name!r} in drive-merge")
        failure = service.rclone.test_remote(remote.read)
        if failure is None:
            return {"ok": True}
        code = classify_failure(failure.returncode, failure.stderr)
        params = {}
        if code == "RCLONE_ERROR":
            tail = [ln.strip() for ln in failure.stderr.splitlines() if ln.strip()]
            params["detail"] = (tail[-1] if tail else str(failure))[:300]
        return {"ok": False, "code": code, "params": params}

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
                                "detail": scrub_secrets(_json_detail(r[5]))} for r in reversed(rows)],
        }

    @router.post("/v1/ui/plans/{plan_id}/review")
    def review_plan(plan_id: int, request: Request, ops_limit: int = Query(default=200, ge=0, le=1000),
                    auth: _Auth = Depends(require_auth)) -> dict:
        """Simulate the plan against the catalog as it is now and return it for a person to read.

        The approval code is added for a browser session of a server that may approve, when the simulation is ok
        and the plan is not a stale workspace preview; no other caller ever gets it, or the digest, at any depth.
        A plan that is approved and still current is not simulated again (that would withdraw the approval)."""
        with lock:
            plan = plans.get_plan(service.conn, plan_id, with_ops=False)
            if plan.status not in ("draft", "simulated", "approved"):
                raise UiError(409, "PLAN_CLOSED", f"plan {plan_id} is {plan.status} and can no longer be reviewed",
                              plan=plan_id, plan_status=plan.status)
            if plan.status == "approved" and plans.current_digest(service.conn, plan_id) == plan.digest:
                report = None
            else:
                report = service.simulate(plan_id)
            data = service.plan(plan_id, ops_limit=ops_limit)
            stale = _stale_reason(service.conn, plans.get_plan(service.conn, plan_id, with_ops=False))
        sim = data.get("simulation")
        code = None
        if report is not None:
            code = report.get("approval_code")
            sim = report
        out = scrub_secrets(data)
        out["simulation"] = scrub_secrets(sim)
        out["stale_reason"] = stale
        if (auth.session is not None and approvals_enabled and code and not stale and report is not None
                and report.get("ok") and data["status"] == "simulated"):
            out["approval_code"] = code
        return out

    @router.post("/v1/ui/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: int) -> dict:
        with lock:
            plan = plans.get_plan(service.conn, plan_id, with_ops=False)
            if plan.status in ("executing", "done", "failed"):
                raise UiError(409, "PLAN_CLOSED", f"plan {plan_id} is {plan.status} and can no longer be cancelled",
                              plan=plan_id, plan_status=plan.status)
            service.cancel(plan_id)
        return {"ok": True}

    @router.get("/v1/ui/bridge/candidates")
    def bridge_candidates(max_bytes: int | None = Query(default=None, ge=0),
                          limit: int = Query(default=100, ge=1, le=100000)) -> dict:
        with lock:
            return service.bridge_candidates(max_bytes=max_bytes, limit=limit)

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
                raise UiError(409, "STALE_PREVIEW", f"{stale}; preview the draft again before approving", plan=plan_id)
            plans.approve_plan(service.conn, plan_id, body.code.strip().lower(),
                               approved_by=f"ui:{_current_user()}")
        return {"ok": True}

    @human.post("/v1/ui/plans/{plan_id}/execute", status_code=202)
    def execute(plan_id: int) -> dict:
        busy = jobs.busy()
        if busy is not None:
            raise UiError(409, "JOB_BUSY", f"a {busy.kind} job ({busy.id}) is still running; wait for it to finish",
                          job=busy.id, kind=busy.kind)
        if not service.config.safety.execution_enabled:
            raise UiError(403, "EXECUTION_DISABLED", "execution is disabled: set `execution_enabled = true` under "
                                                     "[safety] in config.toml, then restart `drivemerge ui`")
        with lock:
            plan = plans.get_plan(service.conn, plan_id, with_ops=False)
            if plan.status != "approved":
                raise UiError(409, "NOT_APPROVED", f"plan {plan_id} is {plan.status}; "
                                                   "only an approved plan can be executed",
                              plan=plan_id, plan_status=plan.status)
            stale = _stale_reason(service.conn, plan)
            if stale:
                raise UiError(409, "STALE_PREVIEW", f"{stale}; preview and approve it again before executing",
                              plan=plan_id)

        def work(job: _Job) -> tuple[Any, str | None]:
            svc = job_service()  # own connection and rclone: this thread never touches the request connection
            try:
                result = svc.execute(plan_id, progress=_reports(jobs, job))  # also closes a finished draft
                failed = result.get("status") == "failed" or result.get("error")
                return result, (str(result.get("error") or f"plan {plan_id} failed") if failed else None)
            finally:
                svc.conn.close()

        return {"job_id": jobs.start("execute", work, plan_id=plan_id).id}

    @human.post("/v1/ui/bridge", status_code=202)
    def bridge(body: BridgeBody = BridgeBody()) -> dict:
        """Download and hash the files that settle the most doubt about duplicates (reads clouds, writes the
        catalog). Progress is {bridged, failed, total}; the result is Service.bridge's."""
        def work(job: _Job) -> tuple[Any, str | None]:
            svc = job_service()
            try:
                result = svc.bridge(
                    max_bytes=body.max_bytes, limit=body.limit, entry_ids=body.entry_ids,
                    started=lambda total: jobs.update(job, total=total),
                    progress=lambda done, failed: jobs.update(job, bridged=done, failed=failed))
                return result, None
            finally:
                svc.conn.close()

        return {"job_id": jobs.start("bridge", work, bridged=0, failed=0, total=None).id}

    # ---- setup: connect, adopt, enable writing (all human-only: they grant access to a cloud)

    def need_path() -> Path:
        if config_path is None:
            raise UiError(409, "NO_CONFIG_PATH", "the server was started without a config file path, so it cannot "
                                                 "add clouds; edit config.toml by hand")
        return Path(config_path)

    def swap_config(path: Path, name: str) -> None:
        """Adopt the remotes now in the config file (and nothing else of it) for this server and later jobs."""
        nonlocal config
        try:
            fresh = load_config(path)
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ConfigError("CONFIG_LAYOUT", f"{path} could not be read back ({type(e).__name__})",
                              name=name) from e
        new = replace(config, remotes=fresh.remotes)
        with lock:
            service.reload_config(new)
            config = new

    def remote_view(name: str) -> dict:
        service.rclone_facts()               # rclone runs outside the lock when its answer is not cached
        with lock:
            return _remote_view(service, name)

    def check_new_name(name: str, *, others: tuple[str, ...] = ()) -> None:
        if not NEW_REMOTE_NAME.fullmatch(name):
            raise UiError(400, "BAD_NAME", "a cloud name is letters, digits, - and _ (at most 32), starting with a "
                                           "letter or digit")
        taken = {r.name for r in config.remotes}
        taken |= {p.split(":", 1)[0] for r in config.remotes for p in (r.read, r.write) if p and ":" in p}
        try:
            taken |= {r["name"] for r in service.rclone.list_remotes()}
        except RcloneError:
            pass
        for n in (name, *others):
            if n in taken:
                raise UiError(409, "REMOTE_EXISTS", f"{n!r} already exists in rclone or in drive-merge", name=n)

    def start_connect(*, name: str, provider: str, options: dict[str, str], remote_auth: bool,
                      mode: str, base: str | None = None) -> str:
        """Start the connect job: create the rclone remote and walk rclone's questions. `mode` is 'new' (add a
        read-only remote when done) or 'write' (record `name` as the write path of the existing remote `base`)."""
        path = need_path()
        ctl = _Connect()
        rc: Rclone = service.rclone
        rtype = CONNECT_TYPES[provider]

        def work(job: _Job) -> tuple[Any, str | None]:
            connects[job.id] = ctl
            state: dict[str, Any] = {"auth_url": None}

            def stage(stage_name: str, **extra: Any) -> None:
                jobs.reset(job, stage=stage_name, name=name, provider=provider, mode=mode, **extra)

            def on_stderr(line: str) -> None:
                found = AUTH_URL.search(line)       # only rclone's own helper on 127.0.0.1:53682 is passed on
                if found:
                    state["auth_url"] = found.group(0)
                    stage("signin", auth_url=state["auth_url"])

            try:
                stage("starting")
                out = rc.config_create(name, rtype, options, on_stderr=on_stderr, cancel=ctl.cancel,
                                       timeout=CREATE_TIMEOUT_S)
                while out.get("State"):
                    opt = out.get("Option") or {}
                    if not opt:
                        raise RcloneError("rclone gave a state without a question", 0, str(out.get("Error") or ""))
                    if opt.get("Name") == "config_is_local" and not state.get("local_asked"):
                        state["local_asked"] = True
                        answer = "false" if remote_auth else "true"
                    else:
                        question = _question(opt)
                        if out.get("Error"):
                            question["retry"] = True      # rclone refused the last answer; its text is not passed on
                        stage("question", question=question)
                        ctl.ask()
                        answer = ctl.wait(ANSWER_TIMEOUT_S)
                    stage("working")
                    out = rc.config_continue(name, out["State"], answer, on_stderr=on_stderr, cancel=ctl.cancel,
                                             timeout=SIGNIN_TIMEOUT_S)
                    del answer
                if out.get("Error"):
                    raise RcloneError("rclone reported an error", 0, str(out["Error"]))
                stage("saving")
                if mode == "new":
                    add_remote_block(path, RemoteConfig(name=name, provider=provider_of(rtype), read=f"{name}:"))
                    swap_config(path, name)
                    shown = name
                else:
                    set_remote_write(path, base, f"{name}:")
                    swap_config(path, base)
                    shown = base
                service.forget_rclone()
                jobs.reset(job, stage="done", name=name, provider=provider, mode=mode)
                return {"remote": remote_view(shown)}, None
            except Exception as e:
                failure = _connect_failure(e, name)
                try:
                    rc.config_delete(name)         # the half-made remote (rclone writes it at the first step)
                except Exception:
                    pass
                service.forget_rclone()
                jobs.reset(job, stage="failed", name=name, provider=provider, mode=mode)
                return None, failure
            finally:
                connects.pop(job.id, None)

        return jobs.start("connect", work, stage="starting", name=name, provider=provider, mode=mode).id

    @human.post("/v1/ui/setup/connect", status_code=202)
    def setup_connect(body: ConnectBody) -> dict:
        need_path()
        check_new_name(body.name)
        if service.rclone.version_info() is None:
            raise UiError(409, "RCLONE_MISSING", f"rclone cannot be run (binary: {config.rclone_binary!r}); "
                                                 "install it or set [rclone] binary in config.toml",
                          binary=config.rclone_binary)
        options: dict[str, str] = {}
        if body.provider == "drive":
            options["scope"] = "drive.readonly"
            options.update({k: v for k, v in (("client_id", body.client_id), ("client_secret", body.client_secret)) if v})
        elif body.client_id or body.client_secret:
            raise UiError(400, "BAD_REQUEST", "client_id and client_secret apply to Google Drive only")
        if body.provider == "iclouddrive":
            for key in ("apple_id", "password"):
                if not getattr(body, key):
                    raise UiError(400, "MISSING_FIELD", f"iCloud Drive needs {key}", field=key)
            options.update(apple_id=body.apple_id, password=body.password)
        elif body.apple_id or body.password:
            raise UiError(400, "BAD_REQUEST", "apple_id and password apply to iCloud Drive only")
        return {"job_id": start_connect(name=body.name, provider=body.provider, options=options,
                                        remote_auth=body.remote_auth, mode="new")}

    def connect_job(job_id: str) -> _Job:
        job = jobs.get(job_id)           # KeyError -> 404 NOT_FOUND
        if job.kind != "connect":
            raise UiError(404, "NOT_FOUND", f"no connect job {job_id}")
        return job

    @human.post("/v1/ui/setup/connect/{job_id}/answer")
    def setup_answer(job_id: str, body: AnswerBody) -> dict:
        connect_job(job_id)
        ctl = connects.get(job_id)
        if ctl is None or not ctl.give(body.result):
            raise UiError(409, "NOT_WAITING", f"connect job {job_id} is not waiting for an answer", job=job_id)
        return {"ok": True}

    @human.post("/v1/ui/setup/connect/{job_id}/cancel")
    def setup_cancel(job_id: str) -> dict:
        connect_job(job_id)
        ctl = connects.get(job_id)
        if ctl is not None:
            ctl.cancel.set()
        return {"ok": True}

    @human.post("/v1/ui/setup/adopt")
    def setup_adopt(body: NameBody) -> dict:
        path = need_path()
        if not ADOPT_NAME.fullmatch(body.name):
            raise UiError(400, "BAD_NAME", "that name cannot be used as a cloud name")
        with lock:
            names = {r.name for r in config.remotes}
        if body.name in names:
            raise UiError(409, "REMOTE_EXISTS", f"{body.name!r} is already in drive-merge", name=body.name)
        listed = {r["name"]: r for r in service.rclone.list_remotes()}
        if body.name not in listed:
            raise UiError(404, "REMOTE_NOT_FOUND", f"rclone has no remote {body.name!r}", name=body.name)
        add_remote_block(path, RemoteConfig(name=body.name, provider=provider_of(listed[body.name]["type"]),
                                            read=f"{body.name}:"))
        swap_config(path, body.name)
        service.forget_rclone()
        return {"remote": remote_view(body.name)}

    @human.post("/v1/ui/setup/enable-write")
    def setup_enable_write(body: EnableWriteBody, response: Response) -> dict:
        path = need_path()
        with lock:
            remote = next((r for r in config.remotes if r.name == body.name), None)
        if remote is None:
            raise UiError(404, "REMOTE_NOT_FOUND", f"no remote {body.name!r} in drive-merge", name=body.name)
        if remote.write is not None:
            return {"remote": remote_view(remote.name)}
        if remote.provider != "drive":
            set_remote_write(path, remote.name, remote.read)    # their tokens are not scope-limited
            swap_config(path, remote.name)
            return {"remote": remote_view(remote.name)}
        rw = f"{remote.name}-rw"
        check_new_name(rw)
        if service.rclone.version_info() is None:
            raise UiError(409, "RCLONE_MISSING", f"rclone cannot be run (binary: {config.rclone_binary!r}); "
                                                 "install it or set [rclone] binary in config.toml",
                          binary=config.rclone_binary)
        options = {"scope": "drive"}
        options.update({k: v for k, v in (("client_id", body.client_id), ("client_secret", body.client_secret)) if v})
        response.status_code = 202
        return {"job_id": start_connect(name=rw, provider="drive", options=options, remote_auth=False,
                                        mode="write", base=remote.name)}

    def start_sync(body: SyncBody) -> dict:
        known = [r.name for r in service.config.remotes]
        names = list(dict.fromkeys(body.remotes or known))
        if not names:
            raise UiError(400, "BAD_REQUEST", "no clouds are connected yet; connect one first")
        for name in names:
            service.config.remote(name)  # KeyError -> 404 before any job starts

        def work(job: _Job) -> tuple[Any, str | None]:
            state = {n: {"state": "queued", "files": 0, "dirs": 0, "bytes": 0} for n in names}
            guard = threading.Lock()

            def progress(name: str, st: str, files: int, dirs: int, size: int) -> None:
                with guard:  # replace, never mutate: the job view serialises the previous dict concurrently
                    state[name] = {"state": st, "files": files, "dirs": dirs, "bytes": size}
                    snapshot = dict(state)
                jobs.update(job, remotes=snapshot, remote=name,   # remote/remote_index: what the Setup page's bar reads
                            remote_index=min(len(names), sum(1 for v in snapshot.values()
                                                             if v["state"] in ("done", "failed")) + 1),
                            remotes_done=sum(1 for v in snapshot.values() if v["state"] in ("done", "failed")),
                            files=sum(v["files"] for v in snapshot.values()),
                            bytes=sum(v["bytes"] for v in snapshot.values()))

            svc = job_service()
            try:
                result = svc.sync(names, progress=progress)
                result["scans"] = [      # one entry per cloud, as the Setup page reads them
                    {"remote": n, **(r.get("scan") or {"status": r["status"], "error": r.get("error")})}
                    for n, r in result["remotes"].items()]
                bad = [n for n, r in result["remotes"].items() if r["status"] != "complete"]
                return result, f"sync did not complete for: {', '.join(bad)}" if bad else None
            finally:
                svc.conn.close()

        initial = {n: {"state": "queued", "files": 0, "dirs": 0, "bytes": 0} for n in names}
        return {"job_id": jobs.start("sync", work, remotes=initial, remotes_total=len(names), remotes_done=0,
                                     files=0, bytes=0).id}

    @human.post("/v1/ui/sync", status_code=202)
    def sync(body: SyncBody = SyncBody()) -> dict:
        """List the clouds (default all) in parallel into the catalog. Progress is per remote:
        {remotes: {name: {state, files, dirs, bytes}}, remotes_total, remotes_done, files, bytes}."""
        return start_sync(body)

    @human.post("/v1/ui/rescan", status_code=202)
    def rescan(body: SyncBody = SyncBody()) -> dict:
        return start_sync(body)

    @human.post("/v1/ui/plans/{plan_id}/purge", status_code=202)
    def purge(plan_id: int, body: PurgeBody) -> dict:
        """Permanently delete this plan's quarantined files (each re-proven against its witness first)."""
        if body.confirm != "purge":
            raise UiError(400, "BAD_REQUEST", 'type "purge" to confirm deleting the quarantined files for good')
        if not service.config.safety.execution_enabled:
            raise UiError(403, "EXECUTION_DISABLED", "execution is disabled: set `execution_enabled = true` under "
                                                     "[safety] in config.toml, then restart `drivemerge ui`")
        with lock:
            plan = plans.get_plan(service.conn, plan_id, with_ops=False)
            if plan.status not in ("done", "failed"):
                raise UiError(409, "CONFLICT", f"plan {plan_id} is {plan.status}; only a plan that has run "
                                                    "has anything in quarantine", plan=plan_id, plan_status=plan.status)

        def work(job: _Job) -> tuple[Any, str | None]:
            svc = job_service()
            try:
                result = svc.purge(plan_id, older_than_days=body.older_than_days)
                return result, (str(result.get("error")) if result.get("failed") else None)
            finally:
                svc.conn.close()

        return {"job_id": jobs.start("purge", work, plan_id=plan_id).id}

    # ---- connections (human channel; slow rclone calls run outside the catalog lock)

    @human.post("/v1/ui/connect/start")
    def connect_start(body: ConnectStartBody) -> dict:
        provider = body.provider
        if body.purpose == "write":
            found = next((c for c in connections.list_connections(service.config) if c["name"] == body.name), None)
            if found is None:
                raise connections.ConnectError("NOT_FOUND", f"no connection named {body.name!r}")
            provider = found["provider"]
        if not provider:
            raise UiError(400, "BAD_REQUEST", "provider is required")
        if body.purpose == "read":
            connections.check_name(service.config, provider, None)  # a free default name exists
        flow = connections.start_flow(service.config, provider, purpose=body.purpose, name=body.name,
                                      client_id=body.client_id or None, client_secret=body.client_secret or None)
        return {"flow_id": flow.id, "auth_url": flow.auth_url}

    def flow_view(flow: connections.AuthFlow) -> dict:
        return {"flow_id": flow.id, "provider": flow.provider, "purpose": flow.purpose, "status": flow.status,
                "auth_url": flow.auth_url, "error": flow.error}

    @human.get("/v1/ui/connect/flows/{flow_id}")
    def connect_flow(flow_id: str) -> dict:
        return flow_view(connections.get_flow(flow_id))

    @human.post("/v1/ui/connect/flows/{flow_id}/finish")
    def connect_finish(flow_id: str, body: ConnectFinishBody = ConnectFinishBody()) -> dict:
        flow = connections.get_flow(flow_id)
        if flow.purpose == "write":
            if not body.name:
                raise UiError(400, "BAD_NAME", "name is required to enable changes on a connection")
            result = connections.finish_enable_writes(service.config, flow_id, body.name)
        else:
            result = connections.finish_connect(service.config, flow_id, name=body.name or None)
        quotas.drop(result["name"])
        with lock:
            service.reload_config()  # the running server sees the new cloud at once
        return result

    @human.post("/v1/ui/connect/flows/{flow_id}/cancel")
    def connect_cancel(flow_id: str) -> dict:
        connections.cancel_flow(flow_id)
        return {"ok": True}

    @human.post("/v1/ui/connections/{name}/test")
    def connection_test(name: str) -> dict:
        result = connections.test_connection(service.config, name)
        quotas.put(name, result)
        return result

    @human.delete("/v1/ui/connections/{name}")
    def connection_delete(name: str) -> dict:
        connections.disconnect(service.config, name)
        quotas.drop(name)
        with lock:
            service.reload_config()
        return {"ok": True}

    @human.post("/v1/ui/rclone/install", status_code=202)
    def rclone_install() -> dict:
        def work(job: _Job) -> tuple[Any, str | None]:
            def progress(phase: str, done: int, total: int | None) -> None:
                jobs.update(job, phase=phase, done=done, total=total)

            path = connections.install_rclone(connections.managed_bin_dir(), progress=progress)
            with lock:
                service.reload_config()  # rebuild Rclone around the new binary
            return {"path": str(path), "rclone": connections.rclone_status(service.config)}, None

        return {"job_id": jobs.start("rclone_install", work, phase="starting", done=0, total=None).id}

    app.include_router(build_router(service, lock, dependencies=normal, quotas=quotas,
                                    created_by=lambda request: "ui" if session_of(request) is not None else "api"))
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


def run_ui(config: Config, *, host: str | None = None, port: int | None = None, open_browser: bool = True,
           config_path: Path | None = None) -> None:
    """Serve the workspace on loopback and print the one-time URL that opens a browser session."""
    host = host or config.api.host
    port = config.api.port if port is None else port
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing to bind {host!r}: the workspace is loopback-only")
    approvals, why_not = approvals_allowed()
    ui_token = secrets.token_urlsafe(24)
    app = create_ui_app(Service(config, config_path=config_path or config.config_path), config=config,
                        api_token=config.api.token, ui_token=ui_token, approvals_enabled=approvals,
                        config_path=config_path or config.config_path)
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
