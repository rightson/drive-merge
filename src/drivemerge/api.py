"""REST API over the Service facade.

Catalog reads, plan proposals and simulation only. Approve, execute, restore,
purge and scan are not routes: approval stays behind the CLI's TTY gate.
"""

from __future__ import annotations

import hmac
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, builders, connections, plans
from .compare import CompareError
from .config import Config
from .service import Service, ServiceError, scrub_secrets

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_NO_PLAN = re.compile(r"no plan \d+")
QUOTA_TTL = 600.0          # seconds a quota from test_connection is reused
QUOTA_FAIL_TTL = 60.0      # a failed lookup is not retried for this long
_CONNECT_STATUS = {"NOT_FOUND": 404, "BAD_NAME": 400, "NEEDS_CLIENT": 400}


class QuotaCache:
    """Per-cloud used/total/free from `connections.test_connection`, kept for ten minutes so a page that shows
    several clouds does not call every provider on each view. None means unknown (not a connection, or it failed)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_name: dict[str, tuple[float, dict | None]] = {}

    def put(self, name: str, result: dict) -> None:
        quota = result.get("quota") if result.get("ok") else None
        with self._lock:
            self._by_name[name] = (time.monotonic() + (QUOTA_TTL if quota else QUOTA_FAIL_TTL), quota)

    def drop(self, name: str) -> None:
        with self._lock:
            self._by_name.pop(name, None)

    def get(self, config: Config, name: str) -> dict | None:
        """The cached quota; looks it up through test_connection when the cache is empty or old. Never raises."""
        with self._lock:
            hit = self._by_name.get(name)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        try:
            remote = config.remote(name)
        except KeyError:
            return None
        if not getattr(remote, "connection", False):
            return None
        try:
            result = connections.test_connection(config, name)
        except Exception:  # unknown quota is not an error for the report
            result = {"ok": False}
        self.put(name, result)
        return result.get("quota") if result.get("ok") else None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RulesBody(_Body):
    rules: str


class LayoutBody(_Body):
    rules: str | None = None
    remotes: list[str] | None = None
    target_root: str = ""
    path_prefix: str | None = None
    limit: int = 200


class PlanBody(_Body):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None


class EstimateBody(_Body):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    sample: int = Field(default=50, ge=0, le=500)


def _check_token(token: str) -> None:
    if not token or not token.strip():
        raise ValueError("the API token is empty; run `drivemerge init` to generate one")


def _given(**kwargs: Any) -> dict[str, Any]:
    """Only the arguments the caller supplied, so the service's own defaults apply."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _csv(value: str | None) -> list[str]:
    """A comma-separated query parameter as a list (empty items dropped)."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _message(exc: Exception) -> str:
    return str(exc.args[0]) if len(exc.args) == 1 else str(exc)


def _error(status: int, message: str, headers: dict[str, str] | None = None, code: str | None = None,
           params: dict | None = None) -> JSONResponse:
    body = {"error": message} if code is None else {"error": message, "code": code}
    if params:
        body["params"] = params
    return JSONResponse(body, status_code=status, headers=headers)


def bearer_ok(authorization: str | None, expected: bytes | None) -> bool:
    """True when `Authorization: Bearer <token>` carries the expected token (None disables bearer auth)."""
    scheme, _, presented = (authorization or "").partition(" ")
    return (expected is not None and scheme.lower() == "bearer"
            and hmac.compare_digest(presented.encode(), expected))


def install_error_handlers(app: FastAPI, *, coded: bool = False) -> None:
    """Map service/plan errors to JSON `{"error": ...}` responses; shared with the UI app.

    With `coded` (the UI app) every error also carries a `code`, so that the page can word it in the
    person's language instead of showing the English `error`.
    """
    def code(name: str) -> str | None:
        return name if coded else None

    def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        name = {401: "UNAUTHENTICATED", 404: "NOT_FOUND"}.get(exc.status_code, "BAD_REQUEST")
        return _error(exc.status_code, str(exc.detail), exc.headers, code(name))

    def bad_request(_: Request, exc: Exception) -> JSONResponse:
        return _error(400, _message(exc), code=code("BAD_REQUEST"))

    def not_found(_: Request, exc: KeyError) -> JSONResponse:
        return _error(404, _message(exc), code=code("NOT_FOUND"))

    def build_error(_: Request, exc: builders.BuildError) -> JSONResponse:
        # Always coded, like compare errors: the facts are the same for a script and for the page.
        return _error(400, _message(exc), code=exc.code, params=exc.params)

    def plan_error(_: Request, exc: plans.PlanError) -> JSONResponse:
        missing = _NO_PLAN.fullmatch(str(exc))
        return _error(404 if missing else 400, str(exc), code=code("NOT_FOUND" if missing else "BAD_REQUEST"))

    def connect_error(_: Request, exc: connections.ConnectError) -> JSONResponse:
        # Always coded: the facts of a failed sign-in are the same for a script and for the page. Messages come
        # from connections.py, which never carries a token.
        status = _CONNECT_STATUS.get(exc.code, 409)
        return _error(status, _message(exc), code=exc.code, params=getattr(exc, "params", None))

    def compare_error(_: Request, exc: CompareError) -> JSONResponse:
        # Always coded: compare, coverage and explore refusals are the same facts for a script and for the page.
        return _error(exc.status, str(exc), code=exc.code, params=exc.params)

    app.add_exception_handler(StarletteHTTPException, http_error)
    for exc_type in (ValueError, ServiceError):  # ServiceError subclasses ValueError
        app.add_exception_handler(exc_type, bad_request)
    app.add_exception_handler(builders.BuildError, build_error)
    app.add_exception_handler(CompareError, compare_error)
    app.add_exception_handler(connections.ConnectError, connect_error)
    app.add_exception_handler(plans.PlanError, plan_error)
    app.add_exception_handler(KeyError, not_found)


def create_app(service: Service, *, token: str) -> FastAPI:
    """FastAPI app over `service`. Every route except GET /healthz needs the bearer token."""
    _check_token(token)
    expected = token.encode()

    # Docs and schema would be unauthenticated routes, so they are not served.
    app = FastAPI(title="drive-merge", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not bearer_ok(authorization, expected):
            raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    # One shared SQLite connection: one service call at a time.
    app.include_router(build_router(service, threading.Lock(), dependencies=[Depends(require_token)]))
    return app


def build_router(service: Service, lock: threading.Lock, *, dependencies: list[Any],
                 created_by: Callable[[Request], str] | None = None,
                 quotas: QuotaCache | None = None) -> APIRouter:
    """The /v1 catalog and plan routes. `dependencies` is the auth the including app requires;
    `lock` serialises use of the service's single SQLite connection. Plan responses leave here scrubbed of
    the digest, the approval code and the snapshot: every caller of these routes is a bearer client or a
    page, and the code reaches a human only through the workspace preview, the plan review or the CLI.
    `created_by(request)` names who proposes a plan (default "api"; the UI app says "ui" for a browser session)."""
    api = APIRouter(dependencies=dependencies)
    quotas = quotas if quotas is not None else QuotaCache()

    @api.get("/v1/setup")
    def setup() -> dict:
        """First-run state, rclone status and the connections (names, accounts, access; never a token)."""
        with lock:
            return service.setup_status()

    @api.get("/v1/release/{remote}")
    def release(remote: str, path: str | None = None, keep_on: str | None = None) -> dict:
        """How much of `remote` provably exists on the other clouds, plus the cloud's cached quota (or null)."""
        with lock:
            report = service.release_report(remote, path_prefix=path or None, keep_on=_csv(keep_on) or None)
            config = service.config
        return {**report, "quota": quotas.get(config, remote)}

    @api.get("/v1/stats")
    def stats() -> dict:
        with lock:
            return service.stats()

    @api.get("/v1/search")
    def search_entries(q: str | None = None, remote: str | None = None, category: str | None = None,
                       ext: str | None = None, min_size: int | None = None, max_size: int | None = None,
                       modified_after: str | None = None, modified_before: str | None = None,
                       path_prefix: str | None = None, include_dirs: bool = False,
                       include_missing: bool = False, duplicates_only: bool = False,
                       limit: int = 50, offset: int = 0) -> dict:
        with lock:
            results = service.search(
                q=q, remote=remote, category=category, ext=ext, min_size=min_size, max_size=max_size,
                modified_after=modified_after, modified_before=modified_before, path_prefix=path_prefix,
                include_dirs=include_dirs, include_missing=include_missing,
                duplicates_only=duplicates_only, limit=limit, offset=offset)
        return {"results": results}

    @api.get("/v1/compare")
    def compare_folders(left_remote: str, left_path: str, right_remote: str, right_path: str,
                        max_files: int = 20000) -> dict:
        with lock:
            return service.compare((left_remote, left_path), (right_remote, right_path), max_files=max_files)

    @api.get("/v1/coverage")
    def coverage(category: str | None = None, max_bars: int = 24) -> dict:
        with lock:
            return service.coverage(category, max_bars=max_bars)

    @api.get("/v1/explore")
    def explore(q: str | None = None, in_: str | None = Query(default=None, alias="in"),
                category: str | None = None, ext: str | None = None, min_size: int | None = None,
                max_size: int | None = None, modified_after: str | None = None,
                modified_before: str | None = None, path_prefix: str | None = None,
                on: str | None = None, not_on: str | None = None, only_on: str | None = None,
                copies_min: int | None = None, copies_max: int | None = None, sort: str = "relevance",
                limit: int = 50, offset: int = 0) -> dict:
        with lock:
            return service.explore(
                q, in_remote=in_ or None, category=category or None, ext=ext or None, min_size=min_size,
                max_size=max_size, modified_after=modified_after or None, modified_before=modified_before or None,
                path_prefix=path_prefix or None, on=_csv(on), not_on=_csv(not_on),
                only_on=_csv(only_on) if only_on else None, copies_min=copies_min, copies_max=copies_max,
                sort=sort, limit=limit, offset=offset)

    @api.get("/v1/entries/{entry_id}")
    def get_entry(entry_id: int) -> dict:
        with lock:
            entry = service.entry(entry_id)
        if entry is None:
            raise HTTPException(404, f"entry {entry_id} not found")
        return entry

    @api.get("/v1/entries/{entry_id}/locations")
    def get_locations(entry_id: int) -> dict:
        with lock:
            if service.entry(entry_id) is None:
                raise HTTPException(404, f"entry {entry_id} not found")
            return {"locations": service.locations(entry_id)}

    @api.get("/v1/duplicates")
    def duplicates(confidence: Literal["exact", "probable"] = "exact", remote: str | None = None,
                   cross_remote_only: bool = False, min_size: int = 1, path_prefix: str | None = None,
                   limit: int = 100, offset: int = 0) -> dict:
        with lock:
            groups = service.duplicates(confidence=confidence, remote=remote,
                                        cross_remote_only=cross_remote_only, min_size=min_size,
                                        path_prefix=path_prefix, limit=limit, offset=offset)
        return {"groups": groups}

    @api.get("/v1/profiles")
    def profiles(remote: str | None = None, prefix: str | None = None, depth: int | None = None,
                 limit: int | None = None) -> dict:
        with lock:
            found = service.profiles(**_given(remote=remote, prefix=prefix, depth=depth, limit=limit))
        return {"profiles": found}

    @api.get("/v1/changes")
    def changes(cursor: str | None = None, limit: int = 1000) -> dict:
        with lock:
            return service.changes(cursor, limit=limit)

    @api.post("/v1/rules/validate")
    def validate_rules(body: RulesBody) -> dict:
        with lock:
            return service.validate_rules(body.rules)

    @api.post("/v1/layout/preview")
    def preview_layout(body: LayoutBody) -> dict:
        with lock:
            return service.preview_layout(rules_text=body.rules, remotes=body.remotes,
                                          target_root=body.target_root, path_prefix=body.path_prefix,
                                          limit=body.limit)

    @api.get("/v1/plans")
    def list_plans(status: str | None = None, limit: int = 50) -> dict:
        with lock:
            return {"plans": scrub_secrets(service.plans(status=status, limit=limit))}

    @api.post("/v1/plans/estimate")
    def estimate_plan(body: EstimateBody) -> dict:
        with lock:
            return scrub_secrets(service.estimate_plan(body.kind, body.params, sample=body.sample))

    @api.get("/v1/plans/summaries")
    def plan_summaries(status: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> dict:
        with lock:
            return {"plans": scrub_secrets(service.plan_summaries(status=status or None, limit=limit))}

    @api.post("/v1/plans", status_code=201)
    def create_plan(body: PlanBody, request: Request) -> dict:
        with lock:
            return scrub_secrets(service.propose_plan(body.kind, body.params, title=body.title,
                                                      created_by=created_by(request) if created_by else "api"))

    @api.get("/v1/plans/{plan_id}")
    def get_plan(plan_id: int, ops_limit: int = 200) -> dict:
        with lock:
            return scrub_secrets(service.plan(plan_id, ops_limit=ops_limit))

    @api.post("/v1/plans/{plan_id}/simulate")
    def simulate_plan(plan_id: int) -> dict:
        with lock:
            return scrub_secrets(service.simulate(plan_id))

    @api.post("/v1/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: int) -> dict:
        with lock:
            service.cancel(plan_id)
        return {"ok": True}

    return api


def serve(config: Config, *, allow_remote: bool = False) -> None:
    """Run the API with uvicorn on config.api. A non-loopback host needs allow_remote=True."""
    host = config.api.host
    if host not in LOOPBACK_HOSTS and not allow_remote:
        raise ValueError(f"refusing to bind {host!r}: the API is loopback-only unless allow_remote=True")
    _check_token(config.api.token)
    app = create_app(Service(config, config_path=config.config_path), token=config.api.token)
    uvicorn.run(app, host=host, port=config.api.port)


__all__ = ["create_app", "build_router", "QuotaCache", "install_error_handlers", "bearer_ok", "scrub_secrets", "serve",
           "LOOPBACK_HOSTS"]
