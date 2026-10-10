"""REST API over the Service facade.

Catalog reads, plan proposals and simulation only. Approve, execute, restore,
purge and scan are not routes: approval stays behind the CLI's TTY gate.
"""

from __future__ import annotations

import hmac
import re
import threading
from typing import Any, Literal

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, builders, plans
from .config import Config
from .service import Service, ServiceError

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_NO_PLAN = re.compile(r"no plan \d+")


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


def _check_token(token: str) -> None:
    if not token or not token.strip():
        raise ValueError("the API token is empty; run `drivemerge init` to generate one")


def _given(**kwargs: Any) -> dict[str, Any]:
    """Only the arguments the caller supplied, so the service's own defaults apply."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _message(exc: Exception) -> str:
    return str(exc.args[0]) if len(exc.args) == 1 else str(exc)


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def bearer_ok(authorization: str | None, expected: bytes | None) -> bool:
    """True when `Authorization: Bearer <token>` carries the expected token (None disables bearer auth)."""
    scheme, _, presented = (authorization or "").partition(" ")
    return (expected is not None and scheme.lower() == "bearer"
            and hmac.compare_digest(presented.encode(), expected))


def install_error_handlers(app: FastAPI) -> None:
    """Map service/plan errors to JSON `{"error": ...}` responses; shared with the UI app."""
    def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail), exc.headers)

    def bad_request(_: Request, exc: Exception) -> JSONResponse:
        return _error(400, _message(exc))

    def not_found(_: Request, exc: KeyError) -> JSONResponse:
        return _error(404, _message(exc))

    def plan_error(_: Request, exc: plans.PlanError) -> JSONResponse:
        return _error(404 if _NO_PLAN.fullmatch(str(exc)) else 400, str(exc))

    app.add_exception_handler(StarletteHTTPException, http_error)
    for exc_type in (ValueError, ServiceError, builders.BuildError):  # the last two subclass ValueError
        app.add_exception_handler(exc_type, bad_request)
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


def build_router(service: Service, lock: threading.Lock, *, dependencies: list[Any]) -> APIRouter:
    """The /v1 catalog and plan routes. `dependencies` is the auth the including app requires;
    `lock` serialises use of the service's single SQLite connection."""
    api = APIRouter(dependencies=dependencies)

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
            return {"plans": service.plans(status=status, limit=limit)}

    @api.post("/v1/plans", status_code=201)
    def create_plan(body: PlanBody) -> dict:
        with lock:
            return service.propose_plan(body.kind, body.params, created_by="api", title=body.title)

    @api.get("/v1/plans/{plan_id}")
    def get_plan(plan_id: int, ops_limit: int = 200) -> dict:
        with lock:
            return service.plan(plan_id, ops_limit=ops_limit)

    @api.post("/v1/plans/{plan_id}/simulate")
    def simulate_plan(plan_id: int) -> dict:
        with lock:
            return service.simulate(plan_id)

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
    app = create_app(Service(config), token=config.api.token)
    uvicorn.run(app, host=host, port=config.api.port)


__all__ = ["create_app", "build_router", "install_error_handlers", "bearer_ok", "serve", "LOOPBACK_HOSTS"]
