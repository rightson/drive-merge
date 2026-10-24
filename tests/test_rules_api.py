"""The Rules routes (docs/RULES.md E) through both apps: the bearer-only API and the workspace server.

Auth (a bearer token proposes, a browser session drafts), the human-channel activation route and its refusal order,
coded errors, `rules_version` in plans, the rules recorded on a plan, previews outside the lock, scrubbing and
`/v1/ui/config`."""

from __future__ import annotations

import json
import os
import threading

import pytest
from conftest import Catalog
from fastapi.testclient import TestClient

from drivemerge import api, classify, db, plans, rulestore, ui_api
from drivemerge.config import ApiConfig, Config, RemoteConfig
from drivemerge.service import Service

API = "api-token"
UI = "ui-token-one-time"
AUTH = {"Authorization": f"Bearer {API}"}

ACTIVE = ('[[rule]]\nname = "photos"\ncategory = ["photo"]\ntarget = "Photos/{name}"\n'
          '[[rule]]\nname = "documents"\ncategory = ["pdf", "document"]\ntarget = "Documents/{name}"\n')
EDITED = ('[[rule]]\nname = "photos"\ncategory = ["photo"]\ntarget = "Photos/{name}"\n'
          '[[rule]]\nname = "receipts"\ncategory = ["pdf"]\nname_regex = "^r_"\ntarget = "Receipts/{name}"\n'
          '[[rule]]\nname = "documents"\ncategory = ["pdf", "document"]\ntarget = "Documents/{name}"\n')
BAD = '[[rule]]\nname = "a"\ntarget = "{yeer}"\n'


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "catalog.db")
    yield c
    c.close()


@pytest.fixture
def home(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "config.toml").write_text("# the person's config\n[api]\ntoken = 't'\n")
    return d


@pytest.fixture
def config(tmp_path, home):
    return Config(db_path=str(tmp_path / "catalog.db"), config_path=home / "config.toml",
                  remotes=[RemoteConfig("gd", "drive", "gd:", "gd:"), RemoteConfig("db", "dropbox", "db:", "db:")],
                  api=ApiConfig(token=API))


@pytest.fixture
def service(conn, catalog, config):
    g = catalog.remote("gd", "drive")
    d = catalog.remote("db", "dropbox")
    catalog.file(g, "Scans/r_1.pdf", size=100, md5="aa")
    catalog.file(g, "Scans/s_2.pdf", size=200, md5="bb")
    catalog.file(g, "Photos/a.jpg", size=10, md5="cc")
    catalog.file(d, "Photos/a.jpg", size=10, md5="cc")
    catalog.file(d, "Docs/n.txt", size=1, md5="dd")
    svc = Service(config, conn=conn)
    svc.ids = {"gd": g, "db": d}
    svc.cat = catalog
    return svc


def _ui(service, config, *, approvals=True):
    return ui_api.create_ui_app(service, config=config, api_token=API, ui_token=UI, approvals_enabled=approvals)


def _login(app):
    client = TestClient(app)
    r = client.post("/v1/ui/session", json={"token": UI})
    assert r.status_code == 200, r.text
    return client, {"X-CSRF-Token": r.json()["csrf"]}


@pytest.fixture
def ui(service, config):
    return _ui(service, config)


@pytest.fixture
def browser(ui):
    return _login(ui)[0]


@pytest.fixture
def human(ui):
    return _login(ui)


@pytest.fixture
def bearer(ui):
    return TestClient(ui, headers=AUTH)


@pytest.fixture
def plain(service, config):
    """The bearer-only API (`drivemerge serve`)."""
    return TestClient(api.create_app(service, token=API), headers=AUTH)


@pytest.fixture(params=["plain", "browser", "bearer"])
def any_client(request):
    """Every way in through the shared router: the REST API, a browser session, the bearer token on the workspace server."""
    return request.getfixturevalue(request.param)


def _write_active(home, text=ACTIVE):
    (home / "rules.toml").write_text(text)


def _body(r):
    assert r.headers["content-type"].startswith("application/json"), r.text
    return r.json()


def _err(r, status, code):
    assert r.status_code == status, r.text
    body = _body(r)
    assert body["code"] == code and isinstance(body["error"], str), body
    return body.get("params", {})


# ----------------------------------------------------------------- state, default, check, transform

def test_state_lists_active_rules_proposals_drafts_and_history(any_client, home):
    _write_active(home)
    st = _body(any_client.get("/v1/rules"))
    active = st["active"]
    assert active["source"] == "file" and active["path"] == str(home / "rules.toml") and active["owned"] is True
    assert active["text"] == ACTIVE and active["sha256"] == rulestore.sha256_of(ACTIVE) and active["error"] is None
    assert active["write_path"] == str(home / "rules.toml") and active["rules"] == 2
    assert active["created_by"] == "file" and active["changed_outside"] is True and active["by"] == "file"
    assert active["check"]["ok"] is True and active["check"]["error_count"] == 0
    assert (st["proposals"], st["drafts"]) == ([], [])
    (h,) = st["history"]
    assert h["id"] == active["version_id"] and h["status"] == "active"
    assert set(h) == {"id", "status", "title", "created_by", "created_at", "updated_at", "activated_at", "activated_by",
                      "sha256", "ok", "base_id", "revision", "rules", "changes"}
    assert (h["rules"], h["changes"]) == (2, [])


def test_state_without_a_file_shows_the_packaged_defaults(any_client):
    active = _body(any_client.get("/v1/rules"))["active"]
    assert (active["source"], active["path"], active["created_by"], active["changed_outside"]) == ("default", None, "default", False)
    assert active["text"] == classify.default_rules_text() and active["owned"] is True and active["version_id"]


def test_state_reports_an_invalid_active_file_and_a_missing_configured_one(any_client, home, service):
    _write_active(home, BAD)
    active = _body(any_client.get("/v1/rules"))["active"]
    assert active["error"]["code"] == "UNKNOWN_VARIABLE" and active["check"]["ok"] is False and active["rules"] is None
    assert active["check"]["errors"][0]["params"]["line"] == 3
    (home / "rules.toml").unlink()
    service.config.rules_path = str(home / "gone.toml")
    active = _body(any_client.get("/v1/rules"))["active"]
    assert active["error"] == {"code": "RULES_FILE_MISSING", "params": {"path": str(home / "gone.toml")}}


def test_default_rules_route(any_client):
    d = _body(any_client.get("/v1/rules/default"))
    assert d["text"] == classify.default_rules_text() and d["sha256"] == rulestore.sha256_of(d["text"])


def test_check_returns_coded_errors_warnings_outline_diff_and_changes(any_client, home):
    _write_active(home)
    out = _body(any_client.post("/v1/rules/check", json={"text": EDITED}))
    assert out["ok"] is True and out["errors"] == [] and out["sha256"] == rulestore.sha256_of(EDITED)
    assert [o["name"] for o in out["outline"]] == ["photos", "receipts", "documents"]
    assert out["changes"] == [{"name": "receipts", "change": "added"}]
    assert out["diff"]["added"] == 5 and out["diff"]["removed"] == 0 and out["layout"]["defaulted"]
    bad = _body(any_client.post("/v1/rules/check", json={"text": BAD + '[[rule]]\nname = "b"\nbogus = 1\ntarget = "x"\n'}))
    assert bad["ok"] is False and bad["changes"] is None
    assert [(e["code"], e["params"]["line"], e["params"]["rule_index"]) for e in bad["errors"]] == [
        ("UNKNOWN_VARIABLE", 3, 1), ("UNKNOWN_RULE_KEY", 6, 2)]
    assert all(set(e) == {"code", "message", "params"} for e in bad["errors"])


def test_check_warns_about_a_remote_that_is_not_a_cloud_of_this_catalog(any_client):
    text = '[[rule]]\nname = "x"\nremote = ["gd", "nowhere"]\ntarget = "X/{name}"\n'
    out = _body(any_client.post("/v1/rules/check", json={"text": text}))
    (w,) = out["warnings"]
    assert w["code"] == "RULE_REMOTE_UNKNOWN" and w["params"] == {
        "line": 1, "rule_index": 1, "rule": "x", "key": "remote", "value": "nowhere"}


def test_check_against_a_version_and_the_size_limit(plain):
    v = _body(plain.post("/v1/rules/versions", json={"text": ACTIVE}))
    out = _body(plain.post("/v1/rules/check", json={"text": EDITED, "against": v["id"]}))
    assert out["changes"] == [{"name": "receipts", "change": "added"}]
    _err(plain.post("/v1/rules/check", json={"text": EDITED, "against": 999}), 404, "RULES_VERSION_NOT_FOUND")
    big = _body(plain.post("/v1/rules/check", json={"text": "#" * (300 * 1024)}))
    assert big["ok"] is False and big["errors"][0]["code"] == "RULES_TOO_LARGE" and big["errors"][0]["params"]["limit"] == 262144


def test_bodies_forbid_extra_keys(plain):
    assert plain.post("/v1/rules/check", json={"text": "", "extra": 1}).status_code == 422
    assert plain.post("/v1/rules/versions", json={"text": "", "bogus": 1}).status_code == 422
    assert plain.post("/v1/rules/preview", json={"mode": "fast"}).status_code == 422
    assert plain.post("/v1/rules/preview", json={"limit": 501}).status_code == 422


def test_transform_moves_removes_and_inserts_and_refuses_with_a_reason(any_client):
    out = _body(any_client.post("/v1/rules/transform", json={"text": EDITED, "op": {"op": "move", "index": 2, "to": 1}}))
    assert [o["name"] for o in out["check"]["outline"]] == ["receipts", "photos", "documents"]
    assert out["text"].startswith("[[rule]]\nname = \"receipts\"")
    out = _body(any_client.post("/v1/rules/transform", json={"text": EDITED, "op": {"op": "remove", "index": 1}}))
    assert "photos" not in out["text"] and out["check"]["ok"] is True
    out = _body(any_client.post("/v1/rules/transform", json={
        "text": EDITED, "op": {"op": "insert", "after": 0, "rule": {"name": "z", "target": "Z/{name}"}}}))
    assert out["check"]["outline"][0]["name"] == "z"
    params = _err(any_client.post("/v1/rules/transform", json={"text": EDITED, "op": {"op": "remove", "index": 9}}),
                  400, "RULES_CANNOT_EDIT")
    assert params["reason"] == "not_found" and set(params) >= {"line", "rule_index", "rule", "key"}
    params = _err(any_client.post("/v1/rules/transform", json={"text": "x = [", "op": {"op": "remove", "index": 1}}),
                  400, "RULES_CANNOT_EDIT")
    assert params["reason"] == "unparsed"
    params = _err(any_client.post("/v1/rules/transform", json={
        "text": EDITED, "op": {"op": "insert", "after": 1, "rule": {"name": "photos", "target": "x"}}}), 400, "NAME_DUPLICATE")
    assert params["name"] == "photos"


# ----------------------------------------------------------------- versions: who may create, edit, delete

def test_the_bearer_token_makes_proposals_and_a_browser_session_makes_drafts(plain, browser, bearer):
    for client in (plain, bearer):
        v = _body(client.post("/v1/rules/versions", json={"text": EDITED, "title": "Receipts", "note": "why"}))
        assert (v["status"], v["created_by"], v["title"], v["note"], v["ok"]) == ("proposed", "api", "Receipts", "why", True)
    r = browser.post("/v1/rules/versions", json={"text": BAD, "title": "wip"})
    assert r.status_code == 201
    d = r.json()
    assert (d["status"], d["created_by"], d["ok"], d["revision"]) == ("draft", "ui", False, 0)
    assert d["check"]["ok"] is False and d["text"] == BAD and d["note"] is None and "diff" in d and "changes" in d


def test_a_proposal_with_errors_is_refused_with_its_codes(plain, bearer):
    for client in (plain, bearer):
        params = _err(client.post("/v1/rules/versions", json={"text": BAD, "title": "x"}), 400, "RULES_INVALID")
        (first,) = params["errors"]
        assert first["code"] == "UNKNOWN_VARIABLE" and first["params"]["variable"] == "yeer" and set(first) == {"code", "params"}


def test_a_draft_may_have_errors(browser):
    assert browser.post("/v1/rules/versions", json={"text": BAD}).status_code == 201


def test_field_limits(plain):
    assert plain.post("/v1/rules/versions", json={"text": EDITED, "title": "x" * 201}).status_code == 422
    assert plain.post("/v1/rules/versions", json={"text": EDITED, "note": "x" * 2001}).status_code == 422
    assert plain.post("/v1/rules/versions", json={"text": EDITED, "title": "x" * 200, "note": "n" * 2000}).status_code == 201
    r = plain.post("/v1/rules/versions", json={"text": "#" * (300 * 1024)})
    _err(r, 400, "RULES_TOO_LARGE")
    _err(plain.post("/v1/rules/versions", json={"text": EDITED, "base_id": 77}), 404, "RULES_VERSION_NOT_FOUND")


def test_listing_and_reading_versions(plain, browser, home):
    _write_active(home)
    p = plain.post("/v1/rules/versions", json={"text": EDITED, "title": "P"}).json()
    d = browser.post("/v1/rules/versions", json={"text": ACTIVE}).json()
    listing = _body(plain.get("/v1/rules/versions"))["versions"]
    assert {v["id"] for v in listing} == {p["id"], d["id"]}
    assert [v["id"] for v in _body(plain.get("/v1/rules/versions", params={"status": "proposed"}))["versions"]] == [p["id"]]
    assert len(plain.get("/v1/rules/versions", params={"limit": 1}).json()["versions"]) == 1
    assert plain.get("/v1/rules/versions", params={"limit": 201}).status_code == 422
    assert plain.get("/v1/rules/versions", params={"status": "weird"}).status_code == 400
    one = _body(plain.get(f"/v1/rules/versions/{p['id']}"))
    assert one["text"] == EDITED and one["note"] is None and one["changes"] == [{"name": "receipts", "change": "added"}]
    assert one["diff"]["added"] == 5 and one["check"]["ok"] is True and one["rules"] == 3
    _err(plain.get("/v1/rules/versions/999"), 404, "RULES_VERSION_NOT_FOUND")


def test_only_the_browser_session_edits_drafts(plain, browser, bearer):
    d = browser.post("/v1/rules/versions", json={"text": ACTIVE}).json()
    url = f"/v1/rules/versions/{d['id']}"
    for client in (plain, bearer):
        params = _err(client.put(url, json={"text": EDITED, "revision": 0}), 403, "DRAFT_UI_ONLY")
        assert params == {"version": d["id"]}
    saved = _body(browser.put(url, json={"text": EDITED, "revision": 0, "title": "T", "note": "N"}))
    assert (saved["revision"], saved["text"], saved["title"], saved["note"]) == (1, EDITED, "T", "N")
    params = _err(browser.put(url, json={"text": ACTIVE, "revision": 0}), 409, "RULES_DRAFT_CHANGED")
    assert params == {"version": d["id"], "revision": 1}
    assert browser.get(url).json()["text"] == EDITED
    p = plain.post("/v1/rules/versions", json={"text": EDITED}).json()
    params = _err(browser.put(f"/v1/rules/versions/{p['id']}", json={"text": ACTIVE, "revision": 0}), 409, "RULES_NOT_DRAFT")
    assert params == {"version": p["id"], "version_status": "proposed"}
    _err(browser.put("/v1/rules/versions/999", json={"text": ACTIVE, "revision": 0}), 404, "RULES_VERSION_NOT_FOUND")


def test_a_deleted_drafts_id_is_not_reused_and_a_stale_put_gets_not_found(browser):
    browser.post("/v1/rules/versions", json={"text": ACTIVE})
    old = browser.post("/v1/rules/versions", json={"text": ACTIVE}).json()
    assert browser.delete(f"/v1/rules/versions/{old['id']}").json() == {"ok": True}
    new = browser.post("/v1/rules/versions", json={"text": EDITED}).json()
    assert new["id"] != old["id"]
    _err(browser.put(f"/v1/rules/versions/{old['id']}", json={"text": ACTIVE, "revision": 0}), 404, "RULES_VERSION_NOT_FOUND")
    assert browser.get(f"/v1/rules/versions/{new['id']}").json()["text"] == EDITED


def test_deleting_drafts_and_dismissing_proposals(plain, browser, bearer, service):
    d = browser.post("/v1/rules/versions", json={"text": ACTIVE}).json()
    for client in (plain, bearer):
        _err(client.delete(f"/v1/rules/versions/{d['id']}"), 403, "DRAFT_UI_ONLY")
    assert browser.delete(f"/v1/rules/versions/{d['id']}").json() == {"ok": True}
    _err(browser.get(f"/v1/rules/versions/{d['id']}"), 404, "RULES_VERSION_NOT_FOUND")
    # a proposal made through the API may be dismissed by the API ...
    p = bearer.post("/v1/rules/versions", json={"text": EDITED}).json()
    assert bearer.delete(f"/v1/rules/versions/{p['id']}").json() == {"ok": True}
    assert rulestore.get_version(service.conn, p["id"])["status"] == "dismissed"
    # ... but Claude's (created_by mcp) only by the person
    claude = service.save_rule_version(EDITED, created_by="mcp", title="from Claude")
    for client in (plain, bearer):
        _err(client.delete(f"/v1/rules/versions/{claude['id']}"), 403, "DRAFT_UI_ONLY")
    assert browser.delete(f"/v1/rules/versions/{claude['id']}").status_code == 200
    assert rulestore.get_version(service.conn, claude["id"])["status"] == "dismissed"
    _err(browser.delete("/v1/rules/versions/999"), 404, "RULES_VERSION_NOT_FOUND")


def test_nothing_can_be_done_without_credentials(ui):
    anon = TestClient(ui)
    for method, url in (("get", "/v1/rules"), ("post", "/v1/rules/check"), ("post", "/v1/rules/versions"),
                        ("get", "/v1/rules/versions"), ("post", "/v1/rules/preview"), ("post", "/v1/rules/explain"),
                        ("get", "/v1/rules/previews/pv_x/files"), ("delete", "/v1/rules/versions/1"),
                        ("put", "/v1/rules/versions/1"), ("post", "/v1/rules/transform"), ("get", "/v1/rules/default")):
        assert getattr(anon, method)(url).status_code == 401, (method, url)


# ----------------------------------------------------------------- previews and explain

def test_preview_and_its_pages_through_the_routes(any_client, home):
    _write_active(home)
    out = _body(any_client.post("/v1/rules/preview", json={"text": EDITED}))
    assert out["changed"] == 1 and out["page"]["files"][0]["path"] == "Scans/r_1.pdf" and out["sampled"] is False
    pid = out["preview_id"]
    page = _body(any_client.get(f"/v1/rules/previews/{pid}/files", params={"filter": "all", "limit": 2, "offset": 1}))
    assert page["total"] == 5 and len(page["files"]) == 2 and page["offset"] == 1
    page = _body(any_client.get(f"/v1/rules/previews/{pid}/files", params={"filter": "rule:photos", "q": "db:"}))
    assert [f["remote"] for f in page["files"]] == ["db"]
    _err(any_client.get("/v1/rules/previews/pv_nope/files"), 410, "PREVIEW_EXPIRED")
    assert any_client.get(f"/v1/rules/previews/{pid}/files", params={"limit": 501}).status_code == 422
    assert any_client.get(f"/v1/rules/previews/{pid}/files", params={"filter": "zzz"}).status_code == 400
    shown = _body(any_client.post("/v1/rules/preview", json={"filter": "all", "limit": 2, "remotes": ["gd"]}))
    assert len(shown["page"]["files"]) == 2 and shown["files_in_scope"] == 3 and shown["changed"] == 0


def test_preview_refusals_are_coded(any_client, home):
    _write_active(home)
    _err(any_client.post("/v1/rules/preview", json={"text": BAD}), 400, "UNKNOWN_VARIABLE")
    _err(any_client.post("/v1/rules/preview", json={"text": EDITED, "version_id": 1}), 400, "RULES_BOTH")
    _err(any_client.post("/v1/rules/preview", json={"version_id": 999}), 404, "RULES_VERSION_NOT_FOUND")
    _err(any_client.post("/v1/rules/preview", json={"text": EDITED, "target_root": "a/../b"}), 400, "TARGET_ROOT_DOT")
    assert any_client.post("/v1/rules/preview", json={"text": EDITED, "remotes": ["nope"]}).status_code == 404


def test_preview_by_version_id(plain, home):
    _write_active(home)
    v = plain.post("/v1/rules/versions", json={"text": EDITED}).json()
    out = _body(plain.post("/v1/rules/preview", json={"version_id": v["id"]}))
    assert out["rules_sha256"] == v["sha256"] and out["changed"] == 1


def test_previews_run_on_their_own_connection_outside_the_lock(service, ui, monkeypatch):
    seen = []
    real = service.preview_rules

    def spy(**kw):
        seen.append(kw.get("conn"))
        return real(**kw)

    monkeypatch.setattr(service, "preview_rules", spy)
    client = TestClient(ui, headers=AUTH)
    assert client.post("/v1/rules/preview", json={"text": EDITED}).status_code == 200
    assert client.post("/v1/rules/preview", json={"text": EDITED + "\n"}).status_code == 200
    conn = seen[0]
    assert conn is not None and conn is not service.conn and seen[1] is conn                      # opened lazily, reused
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(Exception):
        conn.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")


def test_an_in_memory_catalog_previews_on_the_service_connection(monkeypatch):
    mem = db.connect(":memory:")
    cat = Catalog(mem)
    g = cat.remote("gd", "drive")
    cat.file(g, "Photos/a.jpg", size=1)
    svc = Service(Config(), conn=mem)
    seen = []
    real = svc.preview_rules
    monkeypatch.setattr(svc, "preview_rules", lambda **kw: seen.append(kw.get("conn")) or real(**kw))
    client = TestClient(api.create_app(svc, token=API), headers=AUTH)
    out = client.post("/v1/rules/preview", json={"text": EDITED}).json()
    assert seen == [None] and out["files_in_scope"] == 1


def test_two_concurrent_previews_the_older_answers_superseded(service, ui, monkeypatch):
    real = classify.plan_layout
    gate = threading.Event()
    first_in = threading.Event()

    def slow(conn, rs, **kw):
        if not first_in.is_set():
            first_in.set()
            gate.wait(5)
        return real(conn, rs, **kw)

    monkeypatch.setattr(classify, "plan_layout", slow)
    g = service.ids["gd"]
    for i in range(1100):                  # enough rows for the cancel flag to be looked at (every 1,000 rows)
        service.cat.file(g, f"Bulk/f{i:05d}.pdf", size=1)
    results = {}

    def go(name, text):
        results[name] = TestClient(ui, headers=AUTH).post("/v1/rules/preview", json={"text": text})

    t1 = threading.Thread(target=go, args=("first", EDITED))
    t1.start()
    assert first_in.wait(5)
    t2 = threading.Thread(target=go, args=("second", EDITED.replace("Receipts/", "R/")))
    t2.start()
    waiter = threading.Event()
    while service.previewer._gen < 2 and not waiter.wait(0.01):
        pass
    gate.set()
    t1.join(15)
    t2.join(15)
    r1, r2 = results["first"], results["second"]
    assert r1.status_code == 409 and r1.json()["code"] == "PREVIEW_SUPERSEDED", r1.text
    assert r2.status_code == 200 and r2.json()["files_in_scope"] == 1105


def test_explain(any_client, home):
    _write_active(home)
    out = _body(any_client.post("/v1/rules/explain", json={"text": EDITED, "remote": "gd", "path": "Scans/r_1.pdf"}))
    assert out["edited"]["rule"] == "receipts" and out["active"]["rule"] == "documents"
    assert out["edited"]["steps"][1]["result"] == "match"
    out = _body(any_client.post("/v1/rules/explain", json={"remote": "gd", "path": "Photos/a.jpg", "target_root": "A"}))
    assert out["edited"]["target"] == "A/Photos/a.jpg" == out["active"]["target"]
    assert any_client.post("/v1/rules/explain", json={"remote": "gd", "path": "nope"}).status_code == 404
    _err(any_client.post("/v1/rules/explain", json={"text": EDITED, "version_id": 1, "remote": "gd", "path": "x"}),
         400, "RULES_BOTH")


# ----------------------------------------------------------------- activation: the human channel

def _proposal(service, text=EDITED):
    return service.save_rule_version(text, created_by="mcp", title="from Claude")


def _base(service):
    return rulestore.active(service.conn, service.config).sha256


def test_activation_is_a_human_channel_route_only(plain, service, human, bearer, ui, home):
    v = _proposal(service)
    url = f"/v1/ui/rules/versions/{v['id']}/activate"
    body = {"base_sha256": _base(service)}
    assert plain.post(url, json=body).status_code in (404, 405)             # not on the bearer-only API at all
    assert TestClient(ui).post(url, json=body).status_code == 401
    _err(bearer.post(url, json=body), 403, "HUMAN_ONLY")
    client, csrf = human
    _err(client.post(url, json=body), 403, "BAD_CSRF")
    _err(client.post(url, json=body, headers={"X-CSRF-Token": "wrong"}), 403, "BAD_CSRF")
    _err(client.post(url, json=body, headers={**csrf, "Origin": "http://evil.example"}), 403, "BAD_ORIGIN")
    assert not (home / "rules.toml").exists()
    assert rulestore.get_version(service.conn, v["id"])["status"] == "proposed"


def test_activation_needs_approvals_enabled(service, config):
    app = _ui(service, config, approvals=False)
    client, csrf = _login(app)
    v = _proposal(service)
    r = client.post(f"/v1/ui/rules/versions/{v['id']}/activate", json={"base_sha256": _base(service)}, headers=csrf)
    _err(r, 403, "APPROVALS_DISABLED")
    assert f"rules activate {v['id']}" in r.json()["error"]
    assert client.get("/v1/rules").status_code == 200                  # drafts, checks and previews still work
    assert client.post("/v1/rules/versions", json={"text": EDITED}).status_code == 201


def _journal(service, action):
    row = service.conn.execute("SELECT detail FROM journal WHERE action = ? ORDER BY id DESC", (action,)).fetchone()
    return json.loads(row[0])


def test_activation_writes_the_file_and_the_history_follows(service, human, home):
    client, csrf = human
    _write_active(home)
    before = (home / "config.toml").read_bytes()
    base = client.get("/v1/rules").json()["active"]["sha256"]
    v = _proposal(service)
    r = client.post(f"/v1/ui/rules/versions/{v['id']}/activate", json={"base_sha256": base}, headers=csrf)
    assert r.status_code == 200, r.text
    active = r.json()["active"]
    assert active["version_id"] == v["id"] and active["sha256"] == v["sha256"] and active["path"] == str(home / "rules.toml")
    assert (home / "rules.toml").read_text() == EDITED and (home / "config.toml").read_bytes() == before
    after = client.get("/v1/rules").json()
    assert after["active"]["version_id"] == v["id"] and after["active"]["by"].startswith("ui:")
    assert after["active"]["changed_outside"] is False
    assert after["proposals"] == [] and [h["status"] for h in after["history"]] == ["active", "retired"]
    # the plan forms and the waiting plans see the new rules at once
    assert service.ruleset().rules[1].name == "receipts"
    ui_config = client.get("/v1/ui/config").json()["rules"]
    assert ui_config == {"source": "file", "path": str(home / "rules.toml"), "owned": True, "active_version": v["id"], "proposals": 0}
    assert _journal(service, "rules_activate")["by"].startswith("ui:")


def test_activation_refusals_in_order(service, config, human, home):
    client, csrf = human
    _write_active(home)
    base = {"base_sha256": _base(service)}

    def post(vid, body=base):
        return client.post(f"/v1/ui/rules/versions/{vid}/activate", json=body, headers=csrf)

    _err(post(999), 404, "RULES_VERSION_NOT_FOUND")
    gone = _proposal(service)
    client.delete(f"/v1/rules/versions/{gone['id']}")
    params = _err(post(gone["id"]), 409, "RULES_NOT_ACTIVATABLE")
    assert params == {"version": gone["id"], "version_status": "dismissed"}
    draft = client.post("/v1/rules/versions", json={"text": BAD}).json()
    params = _err(post(draft["id"]), 400, "RULES_INVALID")
    assert params["errors"][0]["code"] == "UNKNOWN_VARIABLE"
    good = _proposal(service)
    params = _err(post(good["id"], {"base_sha256": "0" * 64}), 409, "RULES_ACTIVE_CHANGED")
    assert params == {"active_sha256": rulestore.sha256_of(ACTIVE),
                      "active_version": rulestore.active(service.conn, config).version_id}
    assert client.post(f"/v1/ui/rules/versions/{good['id']}/activate", json={}, headers=csrf).status_code == 422
    assert client.post(f"/v1/ui/rules/versions/{good['id']}/activate", json={"base_sha256": "x", "extra": 1},
                       headers=csrf).status_code == 422
    # no config file: nowhere to write
    nofile = Service(Config(db_path=config.db_path, api=ApiConfig(token=API)), conn=service.conn)
    c2, csrf2 = _login(_ui(nofile, nofile.config))
    r = c2.post(f"/v1/ui/rules/versions/{good['id']}/activate", json=base, headers=csrf2)
    _err(r, 409, "RULES_NO_FILE")


def test_a_file_changed_after_the_review_is_refused_and_shown(service, human, home):
    client, csrf = human
    _write_active(home)
    base = client.get("/v1/rules").json()["active"]["sha256"]          # what the dialog showed
    v = _proposal(service)
    (home / "rules.toml").write_text(ACTIVE + "# edited in an editor meanwhile\n")
    r = client.post(f"/v1/ui/rules/versions/{v['id']}/activate", json={"base_sha256": base}, headers=csrf)
    params = _err(r, 409, "RULES_ACTIVE_CHANGED")
    assert params["active_sha256"] == rulestore.sha256_of(ACTIVE + "# edited in an editor meanwhile\n")
    assert (home / "rules.toml").read_text().endswith("meanwhile\n")
    reloaded = client.get("/v1/rules").json()["active"]
    assert reloaded["sha256"] == params["active_sha256"] and reloaded["version_id"] == params["active_version"]


def test_an_unwritable_file_is_reported_with_its_path(service, human, home, monkeypatch):
    client, csrf = human
    _write_active(home)
    base = client.get("/v1/rules").json()["active"]["sha256"]
    v = _proposal(service)

    def boom(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", boom)
    r = client.post(f"/v1/ui/rules/versions/{v['id']}/activate", json={"base_sha256": base}, headers=csrf)
    assert _err(r, 409, "RULES_FILE_UNWRITABLE") == {"path": str(home / "rules.toml")}
    assert (home / "rules.toml").read_text() == ACTIVE


# ----------------------------------------------------------------- plans record their rules

def _consolidate(**extra):
    return {"kind": "consolidate", "params": {"target": "db", "sources": ["gd"], **extra}}


def test_a_plan_built_with_the_active_file_records_the_text_and_where_it_came_from(plain, service, home):
    _write_active(home)
    r = plain.post("/v1/plans", json=_consolidate())
    assert r.status_code == 201, r.text
    plan = plans.get_plan(service.conn, r.json()["plan_id"])
    assert plan.params["rules"] == ACTIVE
    version = rulestore.active(service.conn, service.config).version_id
    assert plan.params["rules_ref"] == {"source": "file", "version": version, "sha256": rulestore.sha256_of(ACTIVE),
                                        "path": str(home / "rules.toml")}
    assert rulestore.get_version(service.conn, version)["created_by"] == "file"        # observed when the plan was proposed
    _write_active(home, EDITED)                                                         # the file changes later ...
    assert plans.get_plan(service.conn, plan.id).params["rules"] == ACTIVE              # ... the plan still says what built it


def test_a_plan_built_with_the_defaults_records_them(plain, service):
    r = plain.post("/v1/plans", json={"kind": "reorganize", "params": {"remote": "gd"}})
    assert r.status_code == 201, r.text
    plan = plans.get_plan(service.conn, r.json()["plan_id"])
    default = classify.default_rules_text()
    assert plan.params["rules"] == default
    assert plan.params["rules_ref"] == {"source": "default", "version": rulestore.active(service.conn, service.config).version_id,
                                        "sha256": rulestore.sha256_of(default), "path": None}


def test_a_plan_built_with_inline_rules_or_a_version(plain, service, home):
    _write_active(home)
    r = plain.post("/v1/plans", json=_consolidate(rules=EDITED))
    plan = plans.get_plan(service.conn, r.json()["plan_id"])
    assert plan.params["rules"] == EDITED
    assert plan.params["rules_ref"] == {"source": "inline", "version": None, "sha256": rulestore.sha256_of(EDITED), "path": None}
    v = _proposal(service)
    r = plain.post("/v1/plans", json=_consolidate(rules_version=v["id"]))
    plan = plans.get_plan(service.conn, r.json()["plan_id"])
    assert plan.params["rules"] == EDITED and "rules_version" not in plan.params
    assert plan.params["rules_ref"] == {"source": "version", "version": v["id"], "sha256": v["sha256"], "path": None}
    assert rulestore.get_version(service.conn, v["id"])["status"] == "proposed"        # using a proposal activates nothing
    assert (home / "rules.toml").read_text() == ACTIVE
    r = plain.post("/v1/plans", json={"kind": "reorganize", "params": {"remote": "gd", "rules_version": v["id"]}})
    assert plans.get_plan(service.conn, r.json()["plan_id"]).params["rules_ref"]["source"] == "version"


def test_estimate_accepts_rules_version_returns_rules_ref_and_writes_nothing(plain, service, home):
    _write_active(home)
    v = _proposal(service)
    before = service.conn.total_changes
    est = _body(plain.post("/v1/plans/estimate", json=_consolidate(rules_version=v["id"])))
    assert est["rules_ref"] == {"source": "version", "version": v["id"], "sha256": v["sha256"], "path": None}
    est = _body(plain.post("/v1/plans/estimate", json=_consolidate()))
    assert est["rules_ref"]["source"] == "file" and est["rules_ref"]["sha256"] == rulestore.sha256_of(ACTIVE)
    assert "rules_ref" not in _body(plain.post("/v1/plans/estimate", json={"kind": "dedupe", "params": {}}))
    assert service.conn.total_changes == before                                          # not even the outside-edit record


def test_rules_ref_cannot_be_claimed_and_rules_and_version_do_not_mix(plain, service):
    claim = {"source": "file", "version": 1, "sha256": "x", "path": None}
    for kind, params in (("consolidate", {"target": "db", "rules_ref": claim}),
                         ("reorganize", {"remote": "gd", "rules_ref": claim}), ("dedupe", {"rules_ref": claim})):
        for url in ("/v1/plans", "/v1/plans/estimate"):
            r = plain.post(url, json={"kind": kind, "params": params})
            assert r.status_code == 400 and "rules_ref" in r.json()["error"], (kind, url, r.text)
    v = _proposal(service)
    for url in ("/v1/plans", "/v1/plans/estimate"):
        _err(plain.post(url, json=_consolidate(rules=EDITED, rules_version=v["id"])), 400, "RULES_BOTH")
        _err(plain.post(url, json=_consolidate(rules_version=999)), 404, "RULES_VERSION_NOT_FOUND")
    assert not service.conn.execute("SELECT 1 FROM plans").fetchone()


def test_a_missing_configured_rules_file_stops_plans_with_a_coded_error(plain, service, home):
    service.config.rules_path = str(home / "gone.toml")
    r = plain.post("/v1/plans", json=_consolidate())
    assert _err(r, 409, "RULES_FILE_MISSING") == {"path": str(home / "gone.toml")}
    _err(plain.post("/v1/plans/estimate", json={"kind": "reorganize", "params": {"remote": "gd"}}), 409, "RULES_FILE_MISSING")
    assert plain.post("/v1/plans", json={"kind": "dedupe", "params": {}}).status_code in (200, 201)


def test_an_invalid_active_file_stops_plans_with_its_rule_error(plain, service, home):
    _write_active(home, BAD)
    _err(plain.post("/v1/plans", json=_consolidate()), 400, "UNKNOWN_VARIABLE")


# ----------------------------------------------------------------- scrubbing and /v1/ui/config

def test_no_new_route_leaks_an_approval_secret(any_client, service, home):
    _write_active(home)
    v = any_client.post("/v1/rules/versions", json={"text": EDITED}).json()
    preview = any_client.post("/v1/rules/preview", json={"text": EDITED, "filter": "all"})
    pid = preview.json()["preview_id"]
    created = any_client.post("/v1/plans", json=_consolidate(rules_version=v["id"]))
    assert created.status_code == 201
    outputs = [any_client.get("/v1/rules").text, any_client.get("/v1/rules/versions").text,
               any_client.get(f"/v1/rules/versions/{v['id']}").text,
               any_client.post("/v1/rules/check", json={"text": EDITED}).text, preview.text,
               any_client.post("/v1/rules/explain", json={"remote": "gd", "path": "Scans/r_1.pdf"}).text,
               any_client.get(f"/v1/rules/previews/{pid}/files", params={"filter": "all"}).text, created.text]
    digest = service.conn.execute("SELECT digest FROM plans WHERE digest IS NOT NULL").fetchone()
    assert digest
    for text in outputs:
        for word in ("approval_code", '"digest"', '"snapshot"'):
            assert word not in text, word
        assert digest[0] not in text


def test_ui_config_carries_the_rules_summary(bearer, browser, service, home):
    cfg = bearer.get("/v1/ui/config").json()["rules"]
    assert cfg == {"source": "default", "path": None, "owned": True, "active_version": None, "proposals": 0}
    _write_active(home)
    service.save_rule_version(EDITED, created_by="mcp", title="p")
    cfg = browser.get("/v1/ui/config").json()["rules"]
    assert cfg["source"] == "file" and cfg["path"] == str(home / "rules.toml") and cfg["proposals"] == 1
    assert cfg["active_version"] is None                                  # a read does not record the outside edit
    browser.get("/v1/rules")
    assert browser.get("/v1/ui/config").json()["rules"]["active_version"] is not None


def test_mcp_proposals_show_up_as_proposed_with_their_changes_and_note(service, bearer):
    service.save_rule_version(EDITED, created_by="mcp", title="from Claude", note="because")
    st = bearer.get("/v1/rules").json()
    (p,) = st["proposals"]
    assert p["created_by"] == "mcp" and p["title"] == "from Claude" and p["status"] == "proposed"
    assert bearer.get("/v1/rules/versions", params={"status": "proposed"}).json()["versions"][0]["id"] == p["id"]
    assert bearer.get(f"/v1/rules/versions/{p['id']}").json()["note"] == "because"
