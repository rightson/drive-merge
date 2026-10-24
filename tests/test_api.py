"""REST API layer: auth, routing, parameter mapping and error mapping over a real Service.

Endpoints whose core modules are still being written are checked with a
recorder that replaces the Service method and returns a canned value.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from drivemerge import api, builders, plans
from drivemerge.config import ApiConfig, Config
from drivemerge.models import Op
from drivemerge.service import Service, ServiceError

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _record(monkeypatch, service, name, *, returns=None, raises=None, real=None):
    """Replace service.<name>: record (args, kwargs), then raise, call `real`, or return `returns`."""
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        if raises is not None:
            raise raises
        return real(*args, **kwargs) if real is not None else returns

    monkeypatch.setattr(service, name, fake)
    return calls


def _draft(conn, title="copy budget"):
    g = conn.execute("SELECT id FROM remotes WHERE name = ?", ("gdrive",)).fetchone()[0]
    d = conn.execute("SELECT id FROM remotes WHERE name = ?", ("dropbox",)).fetchone()[0]
    return plans.create_plan(conn, title, "manual", [
        Op("copy", g, "Docs/budget-2024.pdf", dst_remote_id=d, dst_path="Docs/budget-2024.pdf"),
        Op("copy", g, "Photos/2023/vacation.jpg", dst_remote_id=d, dst_path="Photos/2023/vacation.jpg"),
    ], created_by="test")


@pytest.fixture
def service(conn, catalog):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    catalog.file(g, "Photos/2023/vacation.jpg", size=2_000, md5="aa")
    catalog.file(g, "Docs/budget-2024.pdf", size=500, md5="bb")
    catalog.file(d, "Photos/vacation-copy.jpg", size=2_000, md5="aa")
    catalog.file(d, "Docs/notes.txt", size=10)
    return Service(Config(), conn=conn)


@pytest.fixture
def client(service):
    return TestClient(api.create_app(service, token=TOKEN))


# ----------------------------------------------------------------- auth

def test_healthz_needs_no_token(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def _route_paths(routes):
    """Every route under `routes`, descending into the routers FastAPI wraps when included."""
    for route in routes:
        if getattr(route, "path", None):
            yield route
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _route_paths(nested.routes)


def test_every_route_but_healthz_requires_the_token(service):
    client = TestClient(api.create_app(service, token=TOKEN))
    routes = list(_route_paths(client.app.routes))
    paths = {r.path for r in routes}
    assert {"/healthz", "/v1/stats", "/v1/plans", "/v1/plans/{plan_id}/cancel"} <= paths

    bad_headers = [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
    ]
    checked = 0
    for route in routes:
        if route.path == "/healthz":
            continue
        for method in route.methods:
            for headers in bad_headers:
                r = client.request(method, re.sub(r"\{[^}]+\}", "1", route.path), headers=headers, json={})
                assert r.status_code == 401, (method, route.path, headers)
                assert r.json() == {"error": "missing or invalid bearer token"}
                assert r.headers["www-authenticate"] == "Bearer"
                checked += 1
    assert checked >= 14 * len(bad_headers)


def test_correct_token_is_accepted_with_any_scheme_case(client):
    assert client.get("/v1/stats", headers=AUTH).status_code == 200
    assert client.get("/v1/stats", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_docs_and_schema_are_not_served(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_create_app_refuses_empty_token(service):
    for token in ("", "   "):
        with pytest.raises(ValueError, match="drivemerge init"):
            api.create_app(service, token=token)


def test_service_calls_are_serialized(client, monkeypatch, service):
    inflight = peak = 0
    guard = threading.Lock()

    def slow_stats():
        nonlocal inflight, peak
        with guard:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.02)
        with guard:
            inflight -= 1
        return {}

    monkeypatch.setattr(service, "stats", slow_stats)
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda _: client.get("/v1/stats", headers=AUTH).status_code, range(6)))
    assert codes == [200] * 6
    assert peak == 1


def test_no_approve_execute_restore_purge_or_scan_routes(service):
    app = api.create_app(service, token=TOKEN)
    assert isinstance(app, FastAPI)
    paths = [route.path for route in _route_paths(app.routes)]
    assert "/healthz" in paths and "/v1/plans" in paths  # the walk really reached the included router
    for path in paths:
        assert not any(word in path for word in ("approve", "execute", "restore", "purge", "scan")), path


# ----------------------------------------------------------------- reads

def test_stats_returns_the_service_result(client, service):
    r = client.get("/v1/stats", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == service.stats()
    assert r.json()["totals"]["files"] == 4


def test_search_passes_every_query_param_to_the_service(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "search", real=service.search)
    r = client.get("/v1/search", headers=AUTH, params={
        "q": "budget", "remote": "gdrive", "category": "document", "ext": "pdf",
        "min_size": 1, "max_size": 900, "modified_after": "2024-01-01T00:00:00.000Z",
        "modified_before": "2026-01-01T00:00:00.000Z", "path_prefix": "Docs",
        "include_dirs": "true", "include_missing": "true", "duplicates_only": "true",
        "limit": 5, "offset": 2,
    })
    assert r.status_code == 200
    assert isinstance(r.json()["results"], list)
    assert calls == [((), dict(
        q="budget", remote="gdrive", category="document", ext="pdf", min_size=1, max_size=900,
        modified_after="2024-01-01T00:00:00.000Z", modified_before="2026-01-01T00:00:00.000Z",
        path_prefix="Docs", include_dirs=True, include_missing=True, duplicates_only=True,
        limit=5, offset=2,
    ))]


def test_search_defaults_and_real_results(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "search", real=service.search)
    r = client.get("/v1/search?q=vacation", headers=AUTH)
    assert calls == [((), dict(
        q="vacation", remote=None, category=None, ext=None, min_size=None, max_size=None,
        modified_after=None, modified_before=None, path_prefix=None, include_dirs=False,
        include_missing=False, duplicates_only=False, limit=50, offset=0,
    ))]
    found = {(x["remote"], x["path"]) for x in r.json()["results"]}
    assert found == {("gdrive", "Photos/2023/vacation.jpg"), ("dropbox", "Photos/vacation-copy.jpg")}


def test_entry_lookup_and_404(client, service):
    entry = service.search(q="budget")[0]
    r = client.get(f"/v1/entries/{entry['id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["path"] == "Docs/budget-2024.pdf"
    r = client.get("/v1/entries/999999", headers=AUTH)
    assert r.status_code == 404
    assert r.json() == {"error": "entry 999999 not found"}


def test_locations_maps_to_envelope_and_404s_for_missing_entry(client, service, monkeypatch):
    entry_id = service.search(q="budget")[0]["id"]
    calls = _record(monkeypatch, service, "locations", returns=[{"remote": "gdrive", "path": "Docs/budget-2024.pdf"}])
    r = client.get(f"/v1/entries/{entry_id}/locations", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"locations": [{"remote": "gdrive", "path": "Docs/budget-2024.pdf"}]}
    assert calls == [((entry_id,), {})]
    assert client.get("/v1/entries/999999/locations", headers=AUTH).status_code == 404


def test_duplicates_maps_params(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "duplicates", returns=[{"key": "g:1"}])
    r = client.get("/v1/duplicates", headers=AUTH, params={
        "confidence": "probable", "remote": "gdrive", "cross_remote_only": "true", "min_size": 10,
        "path_prefix": "Photos", "limit": 3, "offset": 1,
    })
    assert r.json() == {"groups": [{"key": "g:1"}]}
    assert calls == [((), dict(confidence="probable", remote="gdrive", cross_remote_only=True,
                               min_size=10, path_prefix="Photos", limit=3, offset=1))]


def test_duplicates_defaults(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "duplicates", returns=[])
    client.get("/v1/duplicates", headers=AUTH)
    assert calls == [((), dict(confidence="exact", remote=None, cross_remote_only=False,
                               min_size=1, path_prefix=None, limit=100, offset=0))]


def test_duplicates_rejects_unknown_confidence(client):
    assert client.get("/v1/duplicates?confidence=maybe", headers=AUTH).status_code == 422


def test_unknown_remote_is_404_with_message(client, service, monkeypatch):
    _record(monkeypatch, service, "duplicates", raises=KeyError("unknown remote 'nope'"))
    r = client.get("/v1/duplicates?remote=nope", headers=AUTH)
    assert r.status_code == 404
    assert r.json() == {"error": "unknown remote 'nope'"}


def test_profiles_pass_only_the_params_given(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "profiles", returns=[{"folder": "Photos"}])
    r = client.get("/v1/profiles?remote=gdrive&prefix=Photos&depth=2&limit=10", headers=AUTH)
    assert r.json() == {"profiles": [{"folder": "Photos"}]}
    client.get("/v1/profiles", headers=AUTH)
    assert calls == [
        ((), dict(remote="gdrive", prefix="Photos", depth=2, limit=10)),
        ((), {}),
    ]


def test_change_feed_pages_through_every_row(client, service, conn):
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    seen, cursor = [], None
    while True:
        params = {"limit": 4, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/changes", params=params, headers=AUTH).json()
        if not page["rows"]:
            break
        seen += page["rows"]
        cursor = page["cursor"]
    assert len(seen) == total
    assert len({row["id"] for row in seen}) == total


def test_bad_change_cursor_is_400(client):
    r = client.get("/v1/changes?cursor=garbage", headers=AUTH)
    assert r.status_code == 400
    assert "invalid change cursor" in r.json()["error"]


# ----------------------------------------------------------------- rules, layout

def test_validate_rules_passes_the_body_through(client, service, monkeypatch):
    result = {"ok": True, "rules": ["photos"]}
    calls = _record(monkeypatch, service, "validate_rules", returns=result)
    r = client.post("/v1/rules/validate", headers=AUTH, json={"rules": "[[rule]]"})
    assert r.status_code == 200
    assert r.json() == result
    assert calls == [(("[[rule]]",), {})]


def test_rules_body_is_strict(client):
    assert client.post("/v1/rules/validate", headers=AUTH, json={}).status_code == 422
    assert client.post("/v1/rules/validate", headers=AUTH, json={"rules": "x", "extra": 1}).status_code == 422


def test_layout_preview_maps_rules_to_rules_text(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "preview_layout", returns={"total": 0, "by_rule": {}, "sample": []})
    r = client.post("/v1/layout/preview", headers=AUTH, json={
        "rules": "R", "remotes": ["gdrive"], "target_root": "Sorted", "path_prefix": "Photos", "limit": 5,
    })
    assert r.status_code == 200
    assert calls[-1] == ((), dict(rules_text="R", remotes=["gdrive"], target_root="Sorted",
                                  path_prefix="Photos", limit=5))
    client.post("/v1/layout/preview", headers=AUTH, json={})
    assert calls[-1] == ((), dict(rules_text=None, remotes=None, target_root="", path_prefix=None, limit=200))


# ----------------------------------------------------------------- plans

def test_plan_list_and_get_use_the_real_service(client, conn):
    pid = _draft(conn)
    listed = client.get("/v1/plans?status=draft", headers=AUTH).json()
    assert [p["id"] for p in listed["plans"]] == [pid]
    assert client.get("/v1/plans?status=done", headers=AUTH).json() == {"plans": []}

    got = client.get(f"/v1/plans/{pid}?ops_limit=1", headers=AUTH).json()
    assert got["status"] == "draft"
    assert got["created_by"] == "test"
    assert got["ops_total"] == 2
    assert len(got["ops"]) == 1
    assert got["simulation"] is None


def test_missing_plan_is_404(client):
    r = client.get("/v1/plans/9999", headers=AUTH)
    assert r.status_code == 404
    assert r.json() == {"error": "no plan 9999"}
    assert client.post("/v1/plans/9999/cancel", headers=AUTH).status_code == 404


def test_cancel_draft_plan(client, conn):
    pid = _draft(conn)
    assert client.post(f"/v1/plans/{pid}/cancel", headers=AUTH).json() == {"ok": True}
    assert plans.get_plan(conn, pid).status == "cancelled"


def test_cancel_finished_plan_is_400(client, conn):
    pid = _draft(conn)
    plans.set_status(conn, pid, "done")
    r = client.post(f"/v1/plans/{pid}/cancel", headers=AUTH)
    assert r.status_code == 400
    assert "cannot cancel" in r.json()["error"]


def test_simulate_returns_the_service_report_without_the_approval_secrets(client, service, conn, monkeypatch):
    pid = _draft(conn)
    report = {"ok": True, "violations": [], "approval_code": "0123abcd", "digest": "0123abcd" + "e" * 56,
              "remotes": [{"remote": "gdrive", "digest": "nested"}]}
    calls = _record(monkeypatch, service, "simulate", returns=report)
    r = client.post(f"/v1/plans/{pid}/simulate", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "violations": [], "remotes": [{"remote": "gdrive"}]}
    assert calls == [((pid,), {})]


# The first 8 characters of the digest ARE the approval code: only the human may read them, so no plan route
# hands them to a caller that holds just the bearer token (the workspace preview and MCP already withhold them).

SECRET_KEYS = {"digest", "approval_code", "snapshot"}


def _secret_keys_in(value) -> set[str]:
    if isinstance(value, dict):
        return (SECRET_KEYS & value.keys()).union(*(_secret_keys_in(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_secret_keys_in(v) for v in value))
    return set()


def test_scrub_secrets_drops_the_keys_at_any_depth():
    nested = {"a": [{"digest": "x" * 64, "keep": 1, "deep": {"approval_code": "01234567", "snapshot": {"1": 2}}}],
              "snapshot": {"1": 2}, "t": ({"approval_code": "z"},), "name": "digest"}
    assert api.scrub_secrets(nested) == {"a": [{"keep": 1, "deep": {}}], "t": [{}], "name": "digest"}


def test_no_plan_route_returns_the_digest_the_approval_code_or_the_snapshot(client, service, conn):
    pid = _draft(conn)
    report = service.simulate(pid)
    assert report["ok"] and report["approval_code"], "the fixture plan must pass, or there is no code to leak"
    digest = plans.get_plan(conn, pid, with_ops=False).digest
    code = plans.short_code(digest)
    new_ops = [{"op": "copy", "src": "gdrive:Docs/budget-2024.pdf", "dst": "dropbox:Docs/copy.pdf"}]

    responses = {
        "GET /v1/plans/{id}": client.get(f"/v1/plans/{pid}", headers=AUTH),
        "GET /v1/plans": client.get("/v1/plans", headers=AUTH),
        "POST /v1/plans/{id}/simulate": client.post(f"/v1/plans/{pid}/simulate", headers=AUTH),
        "POST /v1/plans": client.post("/v1/plans", headers=AUTH, json={"kind": "manual", "params": {"ops": new_ops}}),
    }
    for route, r in responses.items():
        assert r.status_code in (200, 201), (route, r.text)
        assert not _secret_keys_in(r.json()), route
        assert code not in r.text and digest not in r.text, route
    # the proposal was simulated too, so its response is where a code used to appear for a brand-new plan
    created = responses["POST /v1/plans"].json()
    assert created["plan_id"] and created["simulation"]["ok"] is True
    new_digest = plans.get_plan(conn, created["plan_id"], with_ops=False).digest
    assert new_digest and new_digest not in responses["POST /v1/plans"].text

    # what a caller needs is still there
    shown = responses["GET /v1/plans/{id}"].json()
    assert shown["status"] == "simulated" and shown["simulation"]["ok"] is True and shown["ops_total"] == 2
    assert [p["id"] for p in responses["GET /v1/plans"].json()["plans"]] == [pid]
    assert responses["POST /v1/plans/{id}/simulate"].json()["ok"] is True


def test_create_plan_returns_201_and_records_api_as_creator(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "propose_plan", returns={"plan_id": 7, "ops": 1})
    r = client.post("/v1/plans", headers=AUTH, json={
        "kind": "manual", "params": {"ops": [{"op": "copy"}]}, "title": "Copy budget",
    })
    assert r.status_code == 201
    assert r.json() == {"plan_id": 7, "ops": 1}
    assert calls == [(("manual", {"ops": [{"op": "copy"}]}), {"created_by": "api", "title": "Copy budget"})]


@pytest.mark.parametrize("claim", [{"draft_id": 1}, {"draft_version": 1}, {"draft_id": 1, "draft_version": 3}])
def test_create_plan_refuses_params_that_claim_a_workspace_draft(client, conn, claim):
    """`draft_id` makes the server treat a plan as that draft's preview (and close the draft when it runs):
    only workspace.preview may write it, so a proposal that carries it is refused and nothing is saved."""
    ops = [{"op": "copy", "src": "gdrive:Docs/budget-2024.pdf", "dst": "dropbox:Docs/copy.pdf"}]
    r = client.post("/v1/plans", headers=AUTH, json={"kind": "manual", "params": {"ops": ops, **claim}})
    assert r.status_code == 400 and "reserved for workspace previews" in r.json()["error"]
    assert plans.list_plans(conn) == []


def test_create_plan_cannot_set_its_own_creator(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "propose_plan", returns={})
    r = client.post("/v1/plans", headers=AUTH, json={"kind": "manual", "created_by": "human"})
    assert r.status_code == 422
    assert calls == []


def test_unknown_plan_kind_is_400_from_the_real_service(client):
    r = client.post("/v1/plans", headers=AUTH, json={"kind": "teleport"})
    assert r.status_code == 400
    assert "unknown plan kind" in r.json()["error"]


@pytest.mark.parametrize("exc", [
    plans.PlanError("bad plan"),
    ServiceError("bad request"),
    ValueError("bad value"),
])
def test_planning_errors_map_to_400(client, service, monkeypatch, exc):
    _record(monkeypatch, service, "propose_plan", raises=exc)
    r = client.post("/v1/plans", headers=AUTH, json={"kind": "manual"})
    assert r.status_code == 400
    assert r.json() == {"error": str(exc)}


def test_build_error_is_answered_coded(client, service, monkeypatch):
    _record(monkeypatch, service, "propose_plan", raises=builders.BuildError("bad ops"))
    r = client.post("/v1/plans", headers=AUTH, json={"kind": "manual"})
    assert (r.status_code, r.json()) == (400, {"error": "bad ops", "code": "BUILD_ERROR"})
    _record(monkeypatch, service, "propose_plan",
            raises=builders.BuildError("target 'x' is read-only", "READ_ONLY", remote="x"))
    r = client.post("/v1/plans", headers=AUTH, json={"kind": "manual"})
    assert r.json() == {"error": "target 'x' is read-only", "code": "READ_ONLY", "params": {"remote": "x"}}


def test_plan_error_from_simulate_is_400(client, service, conn, monkeypatch):
    pid = _draft(conn)
    _record(monkeypatch, service, "simulate", raises=plans.PlanError("plan is executing"))
    r = client.post(f"/v1/plans/{pid}/simulate", headers=AUTH)
    assert r.status_code == 400
    assert r.json() == {"error": "plan is executing"}


# ----------------------------------------------------------------- serve()

def _no_run(*args, **kwargs):
    raise AssertionError("uvicorn.run must not be called")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_serve_refuses_non_loopback_hosts(monkeypatch, host):
    monkeypatch.setattr(api.uvicorn, "run", _no_run)
    config = Config(db_path=":memory:", api=ApiConfig(host=host, port=8765, token=TOKEN))
    with pytest.raises(ValueError, match="loopback"):
        api.serve(config)


def test_serve_allows_non_loopback_only_with_allow_remote(monkeypatch):
    seen = {}
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    config = Config(db_path=":memory:", api=ApiConfig(host="0.0.0.0", port=9000, token=TOKEN))
    api.serve(config, allow_remote=True)
    assert isinstance(seen["app"], FastAPI)
    assert seen["host"] == "0.0.0.0"
    assert seen["port"] == 9000


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_serve_binds_loopback_by_default(monkeypatch, host):
    seen = {}
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    api.serve(Config(db_path=":memory:", api=ApiConfig(host=host, port=8765, token=TOKEN)))
    assert seen["host"] == host
    assert seen["port"] == 8765


def test_serve_refuses_empty_token_before_opening_the_catalog(monkeypatch):
    monkeypatch.setattr(api.uvicorn, "run", _no_run)
    with pytest.raises(ValueError, match="drivemerge init"):
        api.serve(Config(db_path=":memory:", api=ApiConfig(token="")))


# ----------------------------------------------------------------- setup and free space (bearer API)

def test_setup_is_readable_with_the_token_and_has_no_connection_routes(client, service):
    body = client.get("/v1/setup", headers=AUTH).json()
    assert set(body) == {"first_run", "config_path", "rclone", "connections", "providers", "needed", "manual_remotes"}
    assert body["connections"] == [] and {p["key"] for p in body["providers"]} == {
        "drive", "onedrive", "dropbox", "iclouddrive"}
    assert body["needed"] is True and body["manual_remotes"] == []
    assert all({"key", "label", "needs_client", "kind", "fields", "authorize"} == set(p) for p in body["providers"])
    paths = {r.path for r in _route_paths(client.app.routes)}
    assert not [p for p in paths if "connect" in p or "sync" in p or "rclone" in p]     # human channel only


def test_release_report_passes_its_parameters_and_adds_a_null_quota(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "release_report", returns={"proven": {"files": 1, "bytes": 5}})
    r = client.get("/v1/release/gdrive?path=Docs&keep_on=dropbox,onedrive", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"proven": {"files": 1, "bytes": 5}, "quota": None}      # gdrive is not a connection here
    assert calls == [(("gdrive",), {"path_prefix": "Docs", "keep_on": ["dropbox", "onedrive"]})]
    client.get("/v1/release/gdrive", headers=AUTH)
    assert calls[-1] == (("gdrive",), {"path_prefix": None, "keep_on": None})


def test_release_errors_carry_code_and_params(client, service, monkeypatch):
    err = builders.BuildError("gdrive has no write remote", "READ_ONLY", remote="gdrive")
    _record(monkeypatch, service, "release_report", raises=err)
    r = client.get("/v1/release/gdrive", headers=AUTH)
    assert r.status_code == 400 and r.json()["code"] == "READ_ONLY" and r.json()["params"] == {"remote": "gdrive"}


def test_connect_errors_are_always_coded(client, service, monkeypatch):
    from drivemerge import connections
    _record(monkeypatch, service, "setup_status", raises=connections.ConnectError("NO_RCLONE", "rclone is missing"))
    r = client.get("/v1/setup", headers=AUTH)
    assert r.status_code == 409 and r.json() == {"error": "rclone is missing", "code": "NO_RCLONE"}


@pytest.mark.parametrize("code, status, params", [
    ("MISSING_FIELD", 400, {"field": "password"}), ("BAD_REQUEST", 400, {}), ("TOKEN_UNREADABLE", 400, {}),
    ("NOT_WAITING", 409, {}), ("IMPORT_UNSUPPORTED", 400, {"type": "crypt"}), ("IMPORT_ENCRYPTED", 409, {}),
    ("CONFIG_WRITE", 409, {}), ("NOT_FOUND", 404, {}), ("NEEDS_CLIENT", 400, {})])
def test_connect_error_codes_have_their_status_and_params(client, service, monkeypatch, code, status, params):
    from drivemerge import connections
    _record(monkeypatch, service, "setup_status", raises=connections.ConnectError(code, "nope", **params))
    r = client.get("/v1/setup", headers=AUTH)
    assert r.status_code == status and r.json()["code"] == code
    assert r.json().get("params", {}) == params


def test_a_release_plan_can_be_proposed_over_the_api(client, service, monkeypatch):
    calls = _record(monkeypatch, service, "propose_plan", returns={"plan_id": 3, "ops": 2})
    r = client.post("/v1/plans", headers=AUTH, json={"kind": "release", "params": {"remote": "dropbox"}})
    assert r.status_code == 201 and calls[0][0] == ("release", {"remote": "dropbox"})
