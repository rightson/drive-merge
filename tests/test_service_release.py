from __future__ import annotations

import sys
import types

import pytest

from drivemerge import builders, config as cfgmod, db
from drivemerge.service import Service, ServiceError


@pytest.fixture
def svc(tmp_path):
    cfg = cfgmod.parse_config({"database": {"path": ":memory:"},
                               "remotes": [{"name": "a", "provider": "local", "read": str(tmp_path / "a"),
                                            "write": str(tmp_path / "a")}]})
    s = Service(cfg, conn=db.connect(":memory:"))
    yield s
    s.conn.close()


def fake_release(monkeypatch, ops=()):
    calls = {}
    mod = types.ModuleType("drivemerge.release")

    def release_report(conn, remote, **kw):
        calls["report"] = (remote, kw)
        return {"remote": remote, "proven": {"files": 0, "bytes": 0}}

    def build_release(conn, **kw):
        calls["build"] = kw
        return builders.BuildResult(ops=list(ops), skipped=[], stats={"x": 1}) if \
            "ops" in builders.BuildResult.__dataclass_fields__ else builders.BuildResult()

    mod.release_report, mod.build_release = release_report, build_release
    monkeypatch.setitem(sys.modules, "drivemerge.release", mod)
    monkeypatch.setattr("drivemerge.release", mod, raising=False)
    return calls


def test_release_report_passes_arguments(svc, monkeypatch):
    calls = fake_release(monkeypatch)
    out = svc.release_report("a", "Photos", ["b"])
    assert out["remote"] == "a"
    assert calls["report"] == ("a", {"path_prefix": "Photos", "keep_on": ["b"]})
    svc.release_report("a")
    assert calls["report"][1] == {"path_prefix": None, "keep_on": None}


def test_propose_release_dispatches_with_quarantine_dir(svc, monkeypatch):
    calls = fake_release(monkeypatch)
    out = svc.propose_plan("release", {"remote": "a", "keep_on": ["b"]}, created_by="test")
    assert out["code"] == "NOTHING_TO_DO"
    assert calls["build"] == {"quarantine_dir": svc.config.safety.quarantine_dir, "remote": "a", "keep_on": ["b"]}
    assert "release" in __import__("drivemerge.service", fromlist=["x"]).PLAN_KINDS


def test_existing_kinds_still_work_and_unknown_refused(svc):
    assert svc.propose_plan("manual", {"ops": []}, created_by="t")["code"] == "NOTHING_TO_DO"
    with pytest.raises(ServiceError):
        svc.propose_plan("bogus", {}, created_by="t")


def test_bridge_entry_ids_restricted(svc, monkeypatch):
    seen = []
    monkeypatch.setattr("drivemerge.identity.bridge_entry",
                        lambda conn, rc, eid, tmp: seen.append(eid) or (_ for _ in ()).throw(ValueError("x"))
                        if eid == 3 else seen.append(eid))
    monkeypatch.setattr("drivemerge.identity.bridge_candidates",
                        lambda *a, **k: pytest.fail("candidates must not be consulted"))
    progress = []
    res = svc.bridge(entry_ids=[1, 2, 2, 3], progress=lambda d, f: progress.append((d, f)))
    assert seen == [1, 2, 3]
    assert res["bridged"] == 2 and [f["entry_id"] for f in res["failed"]] == [3]
    assert progress[-1] == (2, 1)
    assert svc.bridge(entry_ids=[])["bridged"] == 0


def test_reload_config_picks_up_new_remote(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(f'[database]\npath = ":memory:"\n[[remotes]]\nname="a"\nprovider="local"\nread="{tmp_path}"\n')
    cfg = cfgmod.load_config(path)
    s = Service(cfg, conn=db.connect(":memory:"), config_path=path)
    old_rclone = s.rclone
    path.write_text(path.read_text() + f'[[remotes]]\nname="b"\nprovider="local"\nread="{tmp_path}"\n'
                    f'[rclone]\nconfig="{tmp_path}/rc.conf"\n')
    s.reload_config()
    assert [r.name for r in cfg.remotes] == ["a", "b"]  # same object, updated in place
    assert s.conn.execute("SELECT COUNT(*) FROM remotes").fetchone()[0] == 2
    assert s.rclone is not old_rclone
    s.conn.close()


def test_connection_facade_delegates(svc, monkeypatch):
    log = []
    flow = types.SimpleNamespace(id="f1", provider="dropbox", purpose="read", status="waiting",
                                 auth_url="http://x", error=None)
    wflow = types.SimpleNamespace(id="f2", provider="dropbox", purpose="write", status="succeeded",
                                  auth_url=None, error=None)
    mod = types.ModuleType("drivemerge.connections")
    mod.PROVIDER_SPECS = {"dropbox": types.SimpleNamespace(label="Dropbox", needs_client=False)}
    mod.rclone_status = lambda c: {"found": True, "ok": True}
    mod.list_connections = lambda c: [{"name": "db", "provider": "dropbox"}]
    mod.start_flow = lambda c, p, **kw: log.append(("start", p, kw)) or flow
    mod.get_flow = lambda i: wflow if i == "f2" else flow
    mod.cancel_flow = lambda i: log.append(("cancel", i))
    mod.finish_connect = lambda c, i, name=None: log.append(("finish", i, name)) or {"name": name or "dropbox"}
    mod.finish_enable_writes = lambda c, i, n: log.append(("writes", i, n)) or {"name": n}
    mod.test_connection = lambda c, n: {"ok": True}
    mod.disconnect = lambda c, n: log.append(("disconnect", n))
    monkeypatch.setitem(sys.modules, "drivemerge.connections", mod)
    monkeypatch.setattr("drivemerge.connections", mod, raising=False)
    monkeypatch.setattr(svc, "reload_config", lambda: log.append("reload"))

    st = svc.setup_status()
    assert st["providers"] == [{"key": "dropbox", "label": "Dropbox", "needs_client": False}]
    assert st["rclone"]["ok"] and st["connections"][0]["name"] == "db" and st["first_run"] is False
    assert svc.start_connect("dropbox")["flow_id"] == "f1"
    assert svc.connect_flow("f1")["auth_url"] == "http://x"
    assert svc.finish_connect("f1", name="mine") == {"name": "mine"}
    assert svc.enable_writes("db")["flow_id"] == "f1" and log[-1][2]["purpose"] == "write"
    assert svc.finish_connect("f2", name="db") == {"name": "db"} and ("writes", "f2", "db") in log
    with pytest.raises(ServiceError):
        svc.finish_connect("f2")
    with pytest.raises(ServiceError):
        svc.enable_writes("nope")
    svc.cancel_connect("f1")
    svc.disconnect("db")
    assert svc.test_connection("db") == {"ok": True}
    assert log.count("reload") == 3
