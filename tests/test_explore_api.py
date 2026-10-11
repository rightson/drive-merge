"""HTTP routes of Overview, Explore and Compare through both apps: the REST app (bearer) and the UI app
(bearer and browser session), parameter mapping and the coded errors."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from drivemerge import api, db, ui_api
from drivemerge.config import Config, RemoteConfig
from drivemerge.service import Service

API = "api-token"
UI = "ui-token-one-time"
AUTH = {"Authorization": f"Bearer {API}"}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "catalog.db")
    yield c
    c.close()


@pytest.fixture
def service(conn, catalog, tmp_path):
    g = catalog.remote("gdrive", "drive")
    d = catalog.remote("dropbox", "dropbox")
    a = catalog.file(g, "School/a.pdf", size=10, md5="aa")
    b = catalog.file(d, "School (1)/a.pdf", size=10, dropbox="bb")
    conn.execute("UPDATE entries SET content_group = 1 WHERE id IN (?, ?)", (a, b))
    catalog.file(g, "School/only.txt", size=3)
    catalog.file(d, "School (1)/diff.txt", size=4)
    catalog.file(g, "School/diff.txt", size=5)
    config = Config(db_path=str(tmp_path / "catalog.db"),
                    remotes=[RemoteConfig("gdrive", "drive", "gdrive:", "gdrive:"),
                             RemoteConfig("dropbox", "dropbox", "dropbox:", "dropbox:")])
    return Service(config, conn=conn)


@pytest.fixture(params=["rest", "ui-bearer", "ui-session"])
def client(request, service):
    if request.param == "rest":
        return TestClient(api.create_app(service, token=API), headers=AUTH)
    app = ui_api.create_ui_app(service, config=service.config, api_token=API, ui_token=UI, approvals_enabled=False)
    c = TestClient(app, headers=AUTH if request.param == "ui-bearer" else None)
    if request.param == "ui-session":
        assert c.post("/v1/ui/session", json={"token": UI}).status_code == 200
    return c


@pytest.fixture(params=["rest", "ui"])
def coded(request, service):
    """Both apps; the REST app's refusals carry codes too."""
    if request.param == "rest":
        return TestClient(api.create_app(service, token=API), headers=AUTH)
    app = ui_api.create_ui_app(service, config=service.config, api_token=API, ui_token=UI, approvals_enabled=False)
    return TestClient(app, headers=AUTH)


CMP = {"left_remote": "gdrive", "left_path": "School", "right_remote": "dropbox", "right_path": "School (1)"}


# --------------------------------------------------------------------------- auth

@pytest.mark.parametrize("path,params", [("/v1/compare", CMP), ("/v1/coverage", {}), ("/v1/explore", {})])
def test_routes_need_credentials(service, path, params):
    for app in (api.create_app(service, token=API),
                ui_api.create_ui_app(service, config=service.config, api_token=API, ui_token=UI,
                                     approvals_enabled=False)):
        c = TestClient(app)
        assert c.get(path, params=params).status_code == 401
        assert c.get(path, params=params, headers={"Authorization": "Bearer wrong"}).status_code == 401


# --------------------------------------------------------------------------- compare

def test_compare(client):
    r = client.get("/v1/compare", params=CMP)
    assert r.status_code == 200
    body = r.json()
    assert {row["rel"]: row["status"] for row in body["rows"]} == {
        "a.pdf": "same", "only.txt": "left_only", "diff.txt": "differs"}
    assert body["left"]["remote"] == "gdrive" and body["summary"]["same"]["files"] == 1


def test_compare_errors_are_coded_in_both_apps(coded):
    def get(**over):
        return coded.get("/v1/compare", params={**CMP, **over})

    r = get(left_remote="nope")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND" and r.json()["params"] == {"remote": "nope"}
    assert isinstance(r.json()["error"], str)
    r = get(left_path="School/a.pdf")
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (
        409, "NOT_A_FOLDER", {"remote": "gdrive", "path": "School/a.pdf"})
    r = get(right_remote="gdrive", right_path="School")
    assert (r.status_code, r.json()["code"]) == (409, "COMPARE_NESTED")
    assert r.json()["params"] == {"left": {"remote": "gdrive", "path": "School"},
                                  "right": {"remote": "gdrive", "path": "School"}}
    r = get(max_files=1)
    assert (r.status_code, r.json()["code"], r.json()["params"]["limit"]) == (409, "COMPARE_TOO_LARGE", 1)
    assert r.json()["params"]["side"] in ("left", "right")
    r = get(left_path=db.DEFAULT_QUARANTINE_DIR)
    assert (r.status_code, r.json()["code"]) == (409, "QUARANTINE")
    for bad in (0, 100001):
        r = get(max_files=bad)
        assert (r.status_code, r.json()["code"], r.json()["params"]) == (400, "BAD_QUERY", {"param": "max_files"})


def test_compare_needs_all_four_parameters(coded):
    assert coded.get("/v1/compare", params={"left_remote": "gdrive"}).status_code in (400, 422)


# --------------------------------------------------------------------------- coverage

def test_coverage(client):
    r = client.get("/v1/coverage")
    assert r.status_code == 200
    body = r.json()
    assert [x["name"] for x in body["remotes"]] == ["gdrive", "dropbox"]
    assert body["intersections"][0]["remotes"] == ["gdrive", "dropbox"]
    assert client.get("/v1/coverage", params={"category": "photo"}).json()["totals"]["contents"] == 0
    assert client.get("/v1/coverage", params={"max_bars": 1}).json()["other"]["combinations"] == 2


def test_coverage_bad_max_bars(coded):
    for bad in (0, 65):
        r = coded.get("/v1/coverage", params={"max_bars": bad})
        assert (r.status_code, r.json()["code"], r.json()["params"]) == (400, "BAD_QUERY", {"param": "max_bars"})


# --------------------------------------------------------------------------- explore

def test_explore_maps_every_parameter(client):
    r = client.get("/v1/explore", params={"q": "a.pdf", "in": "gdrive", "on": "gdrive,dropbox", "not_on": "",
                                          "copies_min": 2, "copies_max": 2, "ext": "pdf", "min_size": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["results"][0]["copies_count"] == 2
    assert [(c["remote"], c["matched"]) for c in body["results"][0]["copies"]] == [("gdrive", True), ("dropbox", False)]
    assert set(body) == {"total", "truncated", "facets", "results"}


def test_explore_comma_lists_and_only_on(client):
    def names(**p):
        return sorted(x["name"] for x in client.get("/v1/explore", params=p).json()["results"])
    assert names(only_on="gdrive") == ["diff.txt", "only.txt"]
    assert names(only_on="gdrive,dropbox") == ["a.pdf"]
    assert names(not_on="dropbox") == ["diff.txt", "only.txt"]
    assert names(sort="name", limit=1) == ["a.pdf"]
    assert client.get("/v1/explore", params={"q": "txt", "limit": 1, "offset": 1}).json()["total"] == 3


def test_explore_errors_are_coded_in_both_apps(coded):
    r = coded.get("/v1/explore", params={"on": "gdrive,nope"})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (404, "NOT_FOUND", {"remote": "nope"})
    r = coded.get("/v1/explore", params={"in": "nope"})
    assert r.status_code == 404
    r = coded.get("/v1/explore", params={"sort": "bogus"})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (400, "BAD_QUERY", {"param": "sort"})
    r = coded.get("/v1/explore", params={"copies_min": -3})
    assert (r.status_code, r.json()["code"], r.json()["params"]) == (400, "BAD_QUERY", {"param": "copies_min"})


def test_routes_are_read_only(client, conn):
    before = conn.total_changes
    client.get("/v1/compare", params=CMP)
    client.get("/v1/coverage")
    client.get("/v1/explore", params={"q": "pdf"})
    assert conn.total_changes == before
