"""HTTP routes of bulk actions from Explore (docs/BULK.md §F) through both apps: the REST app (bearer) and the UI app
(bearer and browser session): shapes, coded errors with their status, the gather refusal order, `created_by`, scrubbing."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from drivemerge import api, db, gather, ui_api
from drivemerge import explore as explore_mod
from drivemerge.config import Config, RemoteConfig
from drivemerge.service import Service

API = "api-token"
UI = "ui-token-one-time"
AUTH = {"Authorization": f"Bearer {API}"}
SECRETS = ("approval_code", "digest", "snapshot")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "catalog.db")
    yield c
    c.close()


@pytest.fixture
def service(conn, catalog, tmp_path):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    for n, (tag, size) in enumerate((("thesis_final", 4000), ("thesis-v9", 4100), ("paper-a", 300)), 1):
        ids = [catalog.file(g, f"Downloads/{tag}.pdf", size=size, sha256=f"s{n}"),
               catalog.file(g, f"Old/{tag} (1).pdf", size=size, sha256=f"s{n}")]
        if n == 1:
            ids.append(catalog.file(d, f"School/{tag}.pdf", size=size, dropbox=f"d{n}"))
        for i in ids:
            conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (n, i))
    catalog.file(d, "School/hw1.pdf", size=60, dropbox="hw")
    catalog.dir(g, "Dest")
    config = Config(db_path=str(tmp_path / "catalog.db"),
                    remotes=[RemoteConfig("gdrive", "drive", "gdrive:", "gdrive:"),
                             RemoteConfig("dropbox", "dropbox", "dropbox:", "dropbox:")])
    return Service(config, conn=conn)


def ui_app(service):
    return ui_api.create_ui_app(service, config=service.config, api_token=API, ui_token=UI, approvals_enabled=False)


@pytest.fixture(params=["rest", "ui-bearer", "ui-session"])
def client(request, service):
    """Every app that mounts the shared router."""
    if request.param == "rest":
        return TestClient(api.create_app(service, token=API), headers=AUTH)
    c = TestClient(ui_app(service), headers=AUTH if request.param == "ui-bearer" else None)
    if request.param == "ui-session":
        assert c.post("/v1/ui/session", json={"token": UI}).status_code == 200
    return c


@pytest.fixture(params=["ui-bearer", "ui-session"])
def ui(request, service):
    """The apps that mount the draft routes."""
    c = TestClient(ui_app(service), headers=AUTH if request.param == "ui-bearer" else None)
    if request.param == "ui-session":
        assert c.post("/v1/ui/session", json={"token": UI}).status_code == 200
    c.who = "api" if request.param == "ui-bearer" else "ui"
    return c


def no_secrets(body) -> None:
    text = json.dumps(body)
    for word in SECRETS:
        assert word not in text, word


def new_draft(service) -> int:
    return service.create_draft("t")["id"]


DST = {"remote": "gdrive", "path": "Dest"}


# --------------------------------------------------------------------------- auth

def test_the_routes_need_credentials(service):
    for app in (api.create_app(service, token=API), ui_app(service)):
        c = TestClient(app)
        assert c.post("/v1/explore/selection", json={"selection": {"keys": ["g:1"]}}).status_code == 401
        assert c.get("/v1/explore", params={"draft": 1}).status_code == 401
    c = TestClient(ui_app(service))
    body = {"selection": {"keys": ["g:1"]}, "dst": DST}
    assert c.post("/v1/drafts/1/gather", json=body).status_code == 401
    assert c.post("/v1/drafts/1/gather", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_the_rest_app_has_no_gather_route(service):
    c = TestClient(api.create_app(service, token=API), headers=AUTH)
    assert c.post("/v1/drafts/1/gather", json={"selection": {"keys": ["g:1"]}, "dst": DST}).status_code == 404


# --------------------------------------------------------------------------- GET /v1/explore?draft=

def test_explore_without_a_draft_is_as_before(client):
    body = client.get("/v1/explore", params={"q": "thesis"}).json()
    assert "staged" not in body["results"][0] and "staged" not in body["results"][0]["copies"][0]


def test_explore_with_a_draft_marks_the_copies_it_moves(client, service):
    d = new_draft(service)
    service.stage_action(d, {"kind": "move", "items": [{"remote": "gdrive", "path": "Downloads/thesis_final.pdf"}],
                             "dst": DST}, created_by="test")
    body = client.get("/v1/explore", params={"q": "thesis_final", "draft": d}).json()
    r = body["results"][0]
    assert r["staged"] == 1
    marks = {c["path"]: c["staged"] for c in r["copies"]}
    assert marks["Downloads/thesis_final.pdf"] == {"remote": "gdrive", "path": "Dest/thesis_final.pdf"}
    assert marks["Old/thesis_final (1).pdf"] is None and marks["School/thesis_final.pdf"] is None
    service.discard_draft(d)
    assert "staged" not in client.get("/v1/explore", params={"q": "thesis_final", "draft": d}).json()["results"][0]


def test_explore_with_an_unknown_draft_is_a_coded_404(client):
    r = client.get("/v1/explore", params={"q": "x", "draft": 999})
    assert r.status_code == 404
    assert r.json() == {"error": "no draft 999", "code": "NOT_FOUND", "params": {"draft": 999}}


# --------------------------------------------------------------------------- POST /v1/explore/selection

def test_selection_numbers(client):
    r = client.post("/v1/explore/selection", json={"selection": {"query": {"q": "thesis"}}})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "query" and body["total"] == 2 and body["copies"] == 5
    assert body["bytes"] == 8100 and body["redundant"] == {"copies": 3, "bytes": 4000 * 2 + 4100}
    assert body["staged"] is None and "keys" not in body
    assert [r["remote"] for r in body["by_remote"]] == ["gdrive", "dropbox"]
    no_secrets(body)


def test_selection_with_keys_and_a_draft(client, service):
    d = new_draft(service)
    service.stage_action(d, {"kind": "move", "items": [{"remote": "gdrive", "path": "Old/paper-a (1).pdf"}],
                             "dst": DST}, created_by="test")
    r = client.post("/v1/explore/selection",
                    json={"selection": {"keys": ["g:3", "g:1", "g:999"]}, "draft": d, "with_keys": True})
    body = r.json()
    assert r.status_code == 200 and body["keys"] == ["g:1", "g:3"] and body["gone"] == 1
    assert body["staged"] == {"copies": 1}


def test_selection_with_an_unknown_draft_is_not_found(client):
    r = client.post("/v1/explore/selection", json={"selection": {"keys": ["g:1"]}, "draft": 999})
    assert (r.status_code, r.json()["code"]) == (404, "NOT_FOUND")


@pytest.mark.parametrize("selection,status,code,params", [
    ({"keys": ["g:1"], "query": {}}, 400, "BAD_SELECTION", {"param": "keys"}),
    ({"keys": "g:1"}, 400, "BAD_SELECTION", {"param": "keys"}),
    ({"keys": [f"g:{i}" for i in range(1, 502)]}, 400, "TOO_MANY_KEYS", {"param": "keys", "count": 501, "limit": 500}),
    ({"keys": ["g:1", "nope"]}, 400, "BAD_KEY", {"key": "nope"}),
    ({"query": {"in": "nowhere"}}, 404, "NOT_FOUND", {"remote": "nowhere"}),
    ({"query": {"min_size": -4}}, 400, "BAD_QUERY", {"param": "min_size"}),
    ({"query": {"q": "zzzzzz"}}, 409, "SELECTION_EMPTY", None),
    ({"keys": ["g:404"]}, 409, "SELECTION_EMPTY", None),
    ({"query": {"q": "thesis"}, "expect_total": 9}, 409, "SELECTION_CHANGED", {"expected": 9, "total": 2}),
])
def test_selection_errors_are_coded_in_both_apps(client, selection, status, code, params):
    r = client.post("/v1/explore/selection", json={"selection": selection})
    assert r.status_code == status, r.text
    body = r.json()
    assert body["code"] == code and isinstance(body["error"], str)
    assert body.get("params") == params


def test_selection_too_large_and_truncated(client, monkeypatch):
    monkeypatch.setattr(explore_mod, "SELECT_LIMIT", 1)
    monkeypatch.setattr(explore_mod.select, "__kwdefaults__", {**explore_mod.select.__kwdefaults__, "limit": 1})
    r = client.post("/v1/explore/selection", json={"selection": {"query": {"q": "pdf"}}})
    assert (r.status_code, r.json()["code"]) == (409, "SELECTION_TOO_LARGE")
    assert r.json()["params"]["limit"] == 1
    monkeypatch.setattr(explore_mod.select, "__kwdefaults__", {**explore_mod.select.__kwdefaults__, "limit": 5000,
                                                                "scan_cap": 2})
    r = client.post("/v1/explore/selection", json={"selection": {"query": {"q": "pdf"}}})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (409, "SELECTION_TRUNCATED", {"scan_cap": 2})


def test_selection_body_shape(client):
    assert client.post("/v1/explore/selection", json={}).status_code in (400, 422)
    assert client.post("/v1/explore/selection", json={"selection": {"keys": ["g:1"]}, "bogus": 1}).status_code in (400, 422)


# --------------------------------------------------------------------------- plans over a selection

def test_estimate_and_create_a_dedupe_over_a_selection(client, service):
    body = {"kind": "dedupe", "params": {"selection": {"keys": ["g:1", "g:2"]}, "remote": "gdrive"}}
    est = client.post("/v1/plans/estimate", json=body).json()
    assert est["stats"]["selection"] == {"contents": 2, "grouped": 2, "ungrouped": 0, "gone": 0}
    assert est["ops"] == 2 and est["groups_total"] == 2
    no_secrets(est)
    r = client.post("/v1/plans", json=body)
    assert r.status_code == 201
    made = r.json()
    assert made["stats"]["selection"]["contents"] == 2
    no_secrets(made)
    plan = client.get(f"/v1/plans/{made['plan_id']}").json()
    assert plan["params"]["selection"] == {"keys": ["g:1", "g:2"]} and plan["params"]["groups"] == ["g:1", "g:2"]
    no_secrets(plan)
    listed = {p["id"]: p for p in client.get("/v1/plans/summaries").json()["plans"]}[made["plan_id"]]
    assert listed["params"]["groups_count"] == 2 and "groups" not in listed["params"]
    no_secrets(listed)


def test_plan_errors_over_a_selection_are_coded(client):
    r = client.post("/v1/plans/estimate", json={"kind": "dedupe", "params": {
        "selection": {"query": {"q": "thesis"}, "expect_total": 7}}})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (409, "SELECTION_CHANGED",
                                                                     {"expected": 7, "total": 2})
    r = client.post("/v1/plans", json={"kind": "dedupe", "params": {"selection": {"keys": ["g:1"]},
                                                                     "groups": ["g:1"]}})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (400, "BAD_PARAM", {"param": "selection"})
    r = client.post("/v1/plans/estimate", json={"kind": "dedupe", "params": {"selection": {"keys": ["x"]}}})
    assert (r.status_code, r.json()["code"]) == (400, "BAD_KEY")


# --------------------------------------------------------------------------- POST /v1/drafts/{id}/gather

def test_gather_dry_run_is_200_and_stages_nothing(ui, service):
    d = new_draft(service)
    before = service.get_draft(d)["version"]
    r = ui.post(f"/v1/drafts/{d}/gather", json={"selection": {"keys": ["g:1", "g:2"]}, "dst": DST, "dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["files"] == 2 and body["selection"] == {"total": 2, "gone": 0}
    assert {"contents", "files", "bytes", "cross_cloud", "picked", "skipped", "selection"} == set(body)
    assert body["picked"][0]["to"]["path"].startswith("Dest/")
    no_secrets(body)
    assert service.get_draft(d)["version"] == before and service.get_draft(d)["actions"] == []


def test_gather_stages_one_action_and_names_who(ui, service):
    d = new_draft(service)
    r = ui.post(f"/v1/drafts/{d}/gather",
                json={"selection": {"query": {"q": "thesis"}, "expect_total": 2}, "dst": DST, "note": "my note"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["files"] == 2 and body["action"]["created_by"] == ui.who and body["action"]["note"] == "my note"
    assert body["draft_version"] == service.get_draft(d)["version"] and body["warnings"] == []
    assert "items" not in body
    no_secrets(body)
    actions = service.get_draft(d)["actions"]
    assert len(actions) == 1 and len(actions[0]["items"]) == 2 and actions[0]["kind"] == "move"


def test_gather_cross_cloud_and_prefer_remotes(ui, service):
    d = new_draft(service)
    hw = service.select({"query": {"q": "hw1"}}, with_keys=True)["keys"]
    body = {"selection": {"keys": ["g:1", *hw]}, "dst": DST, "dry_run": True}
    out = ui.post(f"/v1/drafts/{d}/gather", json=body).json()
    assert out["skipped"]["by_code"] == {"NOT_ON_TARGET": 1}
    out = ui.post(f"/v1/drafts/{d}/gather", json={**body, "cross_cloud": True, "prefer_remotes": ["dropbox"]}).json()
    assert out["cross_cloud"]["files"] == 1 and out["files"] == 2
    r = ui.post(f"/v1/drafts/{d}/gather", json={**body, "dry_run": False, "cross_cloud": True})
    assert r.status_code == 201 and [w["code"] for w in r.json()["warnings"]] == ["CROSS_REMOTE"]


def test_gather_works_without_the_human_channel(service):
    """Approvals are off and no CSRF token is sent: a gather is a proposal, not an approval."""
    c = TestClient(ui_app(service), headers=AUTH)
    d = new_draft(service)
    assert c.post(f"/v1/drafts/{d}/gather", json={"selection": {"keys": ["g:3"]}, "dst": DST}).status_code == 201


@pytest.mark.parametrize("body,status,code", [
    ({}, 422, "BAD_REQUEST"),
    ({"selection": {"keys": ["g:1"]}}, 422, "BAD_REQUEST"),
    ({"selection": {"keys": ["g:1"]}, "dst": DST, "extra": 1}, 422, "BAD_REQUEST"),
    ({"selection": {"keys": ["g:1"]}, "dst": DST, "cross_cloud": "maybe"}, 422, "BAD_REQUEST"),
    ({"selection": {"keys": ["g:1"]}, "dst": "gdrive:Dest"}, 422, "BAD_REQUEST"),
])
def test_gather_body_shape_comes_first(ui, service, body, status, code):
    r = ui.post(f"/v1/drafts/{new_draft(service)}/gather", json=body)
    assert (r.status_code, r.json()["code"]) == (status, code)


def test_gather_refusal_order(ui, service):
    """body shape, draft, selection, destination, GATHER_TOO_MANY, NOTHING_TO_GATHER."""
    def post(draft, selection, dst, **kw):
        r = ui.post(f"/v1/drafts/{draft}/gather", json={"selection": selection, "dst": dst, **kw})
        return r.status_code, r.json()["code"]

    d = new_draft(service)
    bad_sel, bad_dst = {"keys": ["nope"]}, {"remote": "nowhere", "path": ""}
    assert post(999, bad_sel, bad_dst) == (404, "NOT_FOUND")                       # draft before selection
    closed = new_draft(service)
    service.discard_draft(closed)
    assert post(closed, bad_sel, bad_dst) == (409, "NOT_OPEN")
    assert post(d, bad_sel, bad_dst) == (400, "BAD_KEY")                           # selection before destination
    assert post(d, {"keys": ["g:404"]}, bad_dst) == (409, "SELECTION_EMPTY")
    assert post(d, {"keys": ["g:1"]}, bad_dst) == (404, "NOT_FOUND")
    assert post(d, {"keys": ["g:1"]}, {"remote": "gdrive", "path": ".drive-merge-quarantine"}) == (409, "QUARANTINE")
    assert post(d, {"keys": ["g:1"]}, {"remote": "gdrive", "path": "Missing"}) == (409, "DST_NOT_DIR")
    hw = service.select({"query": {"q": "hw1"}}, with_keys=True)["keys"]
    assert post(d, {"keys": hw}, DST) == (409, "NOTHING_TO_GATHER")                # nothing on gdrive, no option
    ok = ui.post(f"/v1/drafts/{d}/gather", json={"selection": {"keys": ["g:1"]}, "dst": DST, "dry_run": True})
    assert ok.status_code == 200


def test_gather_too_many_comes_before_nothing_to_gather(ui, service, monkeypatch):
    monkeypatch.setattr(gather, "GATHER_LIMIT", 1)
    d = new_draft(service)
    r = ui.post(f"/v1/drafts/{d}/gather", json={"selection": {"keys": ["g:1", "g:2"]}, "dst": DST})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (409, "GATHER_TOO_MANY",
                                                                     {"files": 2, "limit": 1})
    assert service.get_draft(d)["actions"] == []


def test_nothing_to_gather_carries_its_facts(ui, service):
    d = new_draft(service)
    hw = service.select({"query": {"q": "hw1"}}, with_keys=True)["keys"]
    r = ui.post(f"/v1/drafts/{d}/gather", json={"selection": {"keys": hw}, "dst": DST})
    assert (r.status_code, r.json()["code"]) == (409, "NOTHING_TO_GATHER")
    assert r.json()["params"] == {"contents": 1, "skipped": {"NOT_ON_TARGET": 1}}


def test_a_changed_selection_is_refused_not_acted_on(ui, service, catalog):
    d = new_draft(service)
    sel = {"query": {"q": "pdf"}, "expect_total": 4}
    assert ui.post(f"/v1/drafts/{d}/gather", json={"selection": sel, "dst": DST, "dry_run": True}).status_code == 200
    catalog.file(service.conn.execute("SELECT id FROM remotes WHERE name='gdrive'").fetchone()[0],
                 "Downloads/new.pdf", size=9)
    r = ui.post(f"/v1/drafts/{d}/gather", json={"selection": sel, "dst": DST})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (409, "SELECTION_CHANGED",
                                                                     {"expected": 4, "total": 5})
    assert service.get_draft(d)["actions"] == []
