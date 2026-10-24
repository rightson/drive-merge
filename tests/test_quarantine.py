"""The read-only Quarantine module (docs/QUARANTINE.md section B): states, retention, the report, one plan's
items with what the catalog knows about them, and the check before a restore or purge.

Plans, ops and journal rows are built by hand, as the executor leaves them, so that every state is exercised
without a cloud. Nothing in the module may write the catalog: the last test pins `total_changes`.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timedelta, timezone

import pytest

from drivemerge import db, quarantine
from drivemerge.quarantine import QuarantineError

QDIR = ".drive-merge-quarantine"
NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


def ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class World:
    """A catalog with two clouds and helpers to leave plans behind the way the executor would."""

    def __init__(self, conn, catalog):
        self.conn, self.cat = conn, catalog
        self.gd = catalog.remote("gdrive", "drive")
        self.dbx = catalog.remote("dropbox", "dropbox")        # case-insensitive
        self.od = catalog.remote("onedrive", "onedrive")

    def plan(self, kind="dedupe", status="done", created_by="mcp", finished_days_ago=1.0, title=None,
             params=None) -> int:
        return self.conn.execute(
            "INSERT INTO plans(title, kind, status, created_by, params, digest, snapshot, created_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, 'abcdef0123456789', '{\"1\": 1}', ?, ?) RETURNING id",
            (title or f"{kind} plan", kind, status, created_by, json.dumps(params or {}), ts(30),
             ts(finished_days_ago))).fetchone()[0]

    def op(self, pid, seq, kind, rid, path, *, dst=None, witness=None, size=100, status="done", days_ago=5.0,
           reason="duplicate of the kept copy", expect_hash=None, journal=True) -> int:
        """An op as the executor leaves it. A done delete or move gets its `quarantine` journal row."""
        extra = {}
        if dst:
            extra.update(dst_remote_id=dst[0], dst_path=dst[1])
        if witness:
            extra.update(witness_remote_id=witness[0], witness_path=witness[1])
        op_id = self.conn.execute(
            "INSERT INTO plan_ops(plan_id, seq, op, src_remote_id, src_path, dst_remote_id, dst_path, "
            "witness_remote_id, witness_path, expect_size, expect_hash, reason, status, executed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (pid, seq, kind, rid, path, extra.get("dst_remote_id"), extra.get("dst_path"),
             extra.get("witness_remote_id"), extra.get("witness_path"), size, expect_hash, reason, status,
             None if days_ago is None else ts(days_ago))).fetchone()[0]
        if journal and status == "done" and kind in ("delete", "move") and (kind == "delete" or dst):
            self.journal("quarantine", pid, op_id, days_ago, remote="x")
        return op_id

    def journal(self, action, pid, op_id, days_ago, **detail) -> None:
        self.conn.execute("INSERT INTO journal(ts, plan_id, op_id, action, detail) VALUES (?,?,?,?,?)",
                          (ts(days_ago), pid, op_id, action, json.dumps(detail)))

    def restored(self, pid, op_id, days_ago=1.0) -> None:
        self.conn.execute("UPDATE plan_ops SET status = 'skipped', error = 'restored from quarantine', "
                          "executed_at = ? WHERE id = ?", (ts(days_ago), op_id))
        self.journal("restore", pid, op_id, days_ago, status="restored")

    def purged(self, pid, op_id, days_ago=1.0) -> None:
        self.journal("purge", pid, op_id, days_ago, status="purged")

    def live(self, rid, path, **kw) -> int:
        return self.cat.file(rid, path, **kw)

    def qlive(self, rid, pid, path, **kw) -> int:
        return self.cat.file(rid, f"{QDIR}/{pid}/{path}", **kw)

    def report(self, **kw) -> dict:
        return quarantine.quarantine_report(self.conn, quarantine_dir=QDIR, now=NOW, **kw)

    def items(self, pid, **kw) -> dict:
        return quarantine.plan_items(self.conn, pid, quarantine_dir=QDIR, now=NOW, **kw)

    def check(self, pid, action, **kw) -> dict:
        return quarantine.check(self.conn, pid, action, quarantine_dir=QDIR, now=NOW, **kw)


@pytest.fixture
def w(conn, catalog):
    return World(conn, catalog)


def err(code, fn, *a, **kw) -> QuarantineError:
    with pytest.raises(QuarantineError) as caught:
        fn(*a, **kw)
    assert caught.value.code == code and code in quarantine.CODES
    return caught.value


def states(items: dict) -> dict[str, str]:
    return {i["path"]: i["state"] for i in items["items"]}


# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #

def test_the_code_list_is_the_contract():
    assert quarantine.CODES == ("NOT_FOUND", "NOTHING_QUARANTINED", "BAD_OPS", "BAD_QUERY")
    assert issubclass(QuarantineError, ValueError)


def test_each_state_comes_from_the_op_and_the_journal(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "held.bin", witness=(w.gd, "keep/held.bin"))
    restored = w.op(pid, 2, "delete", w.gd, "restored.bin", witness=(w.gd, "keep/r.bin"))
    w.restored(pid, restored)
    purged = w.op(pid, 3, "delete", w.gd, "purged.bin", witness=(w.gd, "keep/p.bin"))
    w.purged(pid, purged)
    got = w.items(pid, state="all")
    assert states(got) == {"held.bin": "held", "restored.bin": "restored", "purged.bin": "purged"}
    assert got["total"] == 3
    by_path = {i["path"]: i for i in got["items"]}
    assert by_path["restored.bin"]["restored_at"] and by_path["restored.bin"]["purged_at"] is None
    assert by_path["purged.bin"]["purged_at"] and by_path["purged.bin"]["restored_at"] is None
    assert by_path["held.bin"]["restored_at"] is None and by_path["held.bin"]["purged_at"] is None
    assert [i["path"] for i in w.items(pid)["items"]] == ["held.bin"], "held is the default"
    assert [i["path"] for i in w.items(pid, state="restored")["items"]] == ["restored.bin"]
    assert [i["path"] for i in w.items(pid, state="purged")["items"]] == ["purged.bin"]


def test_a_restore_that_failed_or_a_purge_that_failed_leaves_the_item_held(w):
    pid = w.plan()
    a = w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a.bin"))
    b = w.op(pid, 2, "delete", w.gd, "b.bin", witness=(w.gd, "k/b.bin"))
    w.journal("restore", pid, a, 1, status="failed", error="occupied")
    w.journal("purge", pid, b, 1, status="failed", error="witness gone")
    assert states(w.items(pid, state="all")) == {"a.bin": "held", "b.bin": "held"}


def test_a_move_counts_only_when_it_met_identical_content(w):
    pid = w.plan(kind="consolidate")
    w.op(pid, 1, "move", w.gd, "a/renamed-away.bin", dst=(w.gd, "b/renamed-away.bin"), journal=False)  # a real move
    w.op(pid, 2, "move", w.gd, "a/dup.bin", dst=(w.gd, "b/dup.bin"))                    # parked: has the row
    got = w.items(pid, state="all")
    assert states(got) == {"a/dup.bin": "held"}
    [item] = got["items"]
    assert item["op"] == "move" and item["witness"] == {"remote": "gdrive", "path": "b/dup.bin"}


def test_a_move_restored_after_it_was_parked_is_restored(w):
    pid = w.plan(kind="consolidate")
    op = w.op(pid, 1, "move", w.gd, "a/dup.bin", dst=(w.gd, "b/dup.bin"))
    w.restored(pid, op)
    assert states(w.items(pid, state="all")) == {"a/dup.bin": "restored"}


def test_ops_that_parked_nothing_are_not_items(w):
    pid = w.plan()
    w.op(pid, 1, "copy", w.gd, "c.bin", dst=(w.dbx, "c.bin"))
    w.op(pid, 2, "delete", w.gd, "pending.bin", witness=(w.gd, "k"), status="pending", days_ago=None)
    w.op(pid, 3, "delete", w.gd, "failed.bin", witness=(w.gd, "k"), status="failed")
    w.op(pid, 4, "delete", w.gd, "skipped.bin", witness=(w.gd, "k"), status="skipped")
    assert w.items(pid, state="all")["items"] == []
    assert w.report()["plans"] == []


def test_a_failed_plan_lists_the_items_that_ran_before_it_stopped(w):
    pid = w.plan(status="failed")
    w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a.bin"))
    w.op(pid, 2, "delete", w.gd, "b.bin", witness=(w.gd, "k/b.bin"), status="failed")
    assert states(w.items(pid)) == {"a.bin": "held"}
    assert [p["plan_id"] for p in w.report()["plans"]] == [pid]


@pytest.mark.parametrize("status", ["draft", "simulated", "approved", "executing", "cancelled"])
def test_plans_that_have_not_finished_running_hold_nothing(w, status):
    pid = w.plan(status=status)
    w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a.bin"))
    assert w.items(pid, state="all")["total"] == 0
    assert w.report()["plans"] == []
    err("NOTHING_QUARANTINED", w.check, pid, "restore")


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #

def test_an_item_is_due_when_quarantine_time_plus_retention_has_passed(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "old.bin", witness=(w.gd, "k/o"), days_ago=40)
    w.op(pid, 2, "delete", w.gd, "edge.bin", witness=(w.gd, "k/e"), days_ago=30)
    w.op(pid, 3, "delete", w.gd, "new.bin", witness=(w.gd, "k/n"), days_ago=26)
    w.op(pid, 4, "delete", w.gd, "undated.bin", witness=(w.gd, "k/u"), days_ago=None, journal=False)
    by_path = {i["path"]: i for i in w.items(pid)["items"]}
    assert [by_path[p]["due"] for p in ("old.bin", "edge.bin", "new.bin")] == [True, True, False]
    assert by_path["new.bin"]["quarantined_at"] == ts(26)
    assert by_path["new.bin"]["due_at"] == ts(26 - 30), "30 days after it was quarantined"
    assert by_path["undated.bin"]["due"] is False and by_path["undated.bin"]["due_at"] is None
    short = {i["path"]: i["due"] for i in w.items(pid, retention_days=7)["items"]}
    assert short["new.bin"] is True


def test_retention_changes_what_the_report_calls_due(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"), days_ago=10, size=10)
    assert w.report()["totals"]["due_files"] == 0
    rep = w.report(retention_days=7)
    assert rep["retention_days"] == 7 and rep["totals"] == {"files": 1, "bytes": 10, "due_files": 1, "due_bytes": 10}


def test_a_restored_item_keeps_the_time_it_was_quarantined(w):
    """Restoring marks the op skipped with a new executed_at; the quarantine journal row still says when."""
    pid = w.plan()
    op = w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"), days_ago=20)
    w.restored(pid, op, days_ago=1)
    [item] = w.items(pid, state="restored")["items"]
    assert item["quarantined_at"] == ts(20) and item["restored_at"] == ts(1)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

def test_the_report_totals_clouds_and_plans(w):
    p1 = w.plan(kind="dedupe", created_by="ui", title="dedupe each", params={"digest": "x", "keep": "gdrive"})
    w.op(p1, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"), days_ago=40, size=1000)
    w.op(p1, 2, "delete", w.gd, "b.bin", witness=(w.gd, "k/b"), days_ago=2, size=500)
    p2 = w.plan(kind="release", created_by="mcp")
    w.op(p2, 1, "delete", w.dbx, "c.bin", witness=(w.gd, "k/c"), days_ago=35, size=7)
    w.op(p2, 2, "delete", w.gd, "d.bin", witness=(w.dbx, "k/d"), days_ago=1, size=3)
    rep = w.report()
    assert rep["quarantine_dir"] == QDIR and rep["retention_days"] == 30 and rep["now"] == "2026-10-24T09:00:00Z"
    assert rep["totals"] == {"files": 4, "bytes": 1510, "due_files": 2, "due_bytes": 1007}
    # every cloud of the catalog, in order, zeros included
    assert [r["remote"] for r in rep["remotes"]] == ["gdrive", "dropbox", "onedrive"]
    gd, dbx, od = rep["remotes"]
    assert (gd["files"], gd["bytes"], gd["due_files"], gd["due_bytes"], gd["plans"]) == (3, 1503, 1, 1000, 2)
    assert (dbx["files"], dbx["bytes"], dbx["due_files"], dbx["due_bytes"], dbx["plans"]) == (1, 7, 1, 7, 1)
    assert (od["files"], od["bytes"], od["plans"]) == (0, 0, 0)
    assert set(gd["last_scan"]) == {"id", "finished_at"} and gd["last_scan"]["finished_at"]
    by_id = {p["plan_id"]: p for p in rep["plans"]}
    a = by_id[p1]
    assert (a["title"], a["kind"], a["status"], a["created_by"]) == ("dedupe each", "dedupe", "done", "ui")
    assert a["held"] == {"files": 2, "bytes": 1500} and a["due"] == {"files": 1, "bytes": 1000}
    assert a["remotes"] == ["gdrive"] and (a["restored"], a["purged"]) == (0, 0)
    assert a["oldest_at"] == ts(40) and a["next_due_at"] == ts(2 - 30)
    assert by_id[p2]["remotes"] == ["gdrive", "dropbox"] and by_id[p2]["due"]["files"] == 1


def test_the_report_has_no_approval_secret(w):
    pid = w.plan(params={"digest": "d1", "approval_code": "c1", "snapshot": {"1": 2}, "remote": "gdrive"})
    w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"))
    text = json.dumps(w.report())
    assert "abcdef0123456789" not in text and "d1" not in text and "c1" not in text and "snapshot" not in text
    assert w.report()["plans"][0]["params"] == {"remote": "gdrive"}


def test_plans_are_ordered_by_their_oldest_held_item_then_settled_ones_by_finish_time(w):
    newer = w.plan(title="newer")
    w.op(newer, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"), days_ago=3)
    older = w.plan(title="older")
    w.op(older, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"), days_ago=20)
    gone_early = w.plan(title="gone early", finished_days_ago=50)
    w.purged(gone_early, w.op(gone_early, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"), days_ago=49))
    gone_late = w.plan(title="gone late", finished_days_ago=10)
    w.restored(gone_late, w.op(gone_late, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"), days_ago=9))
    plans = w.report()["plans"]
    assert [p["title"] for p in plans] == ["older", "newer", "gone late", "gone early"]
    settled = plans[2]
    assert settled["held"] == {"files": 0, "bytes": 0} and settled["restored"] == 1
    assert settled["oldest_at"] is None and settled["next_due_at"] is None
    assert plans[3]["purged"] == 1
    # settled plans add nothing to the clouds
    assert w.report()["totals"]["files"] == 2


def test_a_plan_with_some_items_settled_stays_in_the_list_with_its_counts(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "held.bin", witness=(w.gd, "k/h"), size=5)
    w.restored(pid, w.op(pid, 2, "delete", w.gd, "back.bin", witness=(w.gd, "k/b"), size=6))
    w.purged(pid, w.op(pid, 3, "delete", w.gd, "gone.bin", witness=(w.gd, "k/g"), size=8))
    [p] = w.report()["plans"]
    assert p["held"] == {"files": 1, "bytes": 5} and (p["restored"], p["purged"]) == (1, 1)


def test_the_report_of_an_empty_catalog(w):
    rep = w.report()
    assert rep["totals"] == {"files": 0, "bytes": 0, "due_files": 0, "due_bytes": 0}
    assert rep["plans"] == [] and rep["unknown"] == []
    assert all(r["files"] == 0 for r in rep["remotes"])


def test_the_last_complete_scan_of_a_cloud_is_reported(w):
    w.conn.execute("UPDATE scans SET started_at = ?, finished_at = ?", (ts(6), ts(5)))
    later = w.conn.execute(
        "INSERT INTO scans(remote_id, status, started_at, finished_at) VALUES (?, 'complete', ?, ?) RETURNING id",
        (w.gd, ts(0.5), ts(0.4))).fetchone()[0]
    w.conn.execute("INSERT INTO scans(remote_id, status, started_at, finished_at) VALUES (?, 'failed', ?, ?)",
                   (w.gd, ts(0.2), ts(0.1)))
    gd = w.report()["remotes"][0]
    assert gd["last_scan"] == {"id": later, "finished_at": ts(0.4)}


# --------------------------------------------------------------------------- #
# Unknown items
# --------------------------------------------------------------------------- #

def test_files_nothing_accounts_for_are_unknown_and_never_items(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "docs/a.bin", witness=(w.gd, "k/a"))
    w.qlive(w.gd, pid, "docs/a.bin", size=100)                      # accounted for: the held item
    w.qlive(w.gd, 999, "stranger.bin", size=40)                     # a plan id this catalog does not know
    w.qlive(w.gd, pid, "docs/never-quarantined.bin", size=30)       # a path the plan never quarantined
    w.live(w.gd, f"{QDIR}/by-hand.bin", size=20)                    # someone put it there
    w.qlive(w.dbx, pid, "docs/a.bin", size=5)                       # the right path, the wrong cloud
    w.live(w.gd, "docs/a.bin", size=100)                            # a live file elsewhere: not in quarantine
    [gd_unknown, dbx_unknown] = w.report()["unknown"]
    assert gd_unknown["remote"] == "gdrive" and gd_unknown["files"] == 3 and gd_unknown["bytes"] == 90
    assert sorted(gd_unknown["examples"]) == gd_unknown["examples"] and len(gd_unknown["examples"]) == 3
    assert f"{QDIR}/by-hand.bin" in gd_unknown["examples"]
    assert dbx_unknown == {"remote": "dropbox", "files": 1, "bytes": 5, "examples": [f"{QDIR}/{pid}/docs/a.bin"]}
    assert w.items(pid, state="all")["total"] == 1
    # an unknown file can neither be restored nor purged: it is no item
    err("BAD_OPS", w.check, pid, "purge", op_ids=[10**6])


def test_unknown_examples_stop_at_three(w):
    for n in range(7):
        w.live(w.od, f"{QDIR}/odd/{n}.bin", size=1)
    [u] = w.report()["unknown"]
    assert (u["remote"], u["files"], u["bytes"]) == ("onedrive", 7, 7) and len(u["examples"]) == 3


def test_a_restored_then_reparked_file_is_unknown(w):
    pid = w.plan()
    op = w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"))
    w.restored(pid, op)
    w.qlive(w.gd, pid, "a.bin", size=3)               # the catalog still has (or again has) the file in quarantine
    [u] = w.report()["unknown"]
    assert u["files"] == 1 and u["examples"] == [f"{QDIR}/{pid}/a.bin"]


def test_a_purged_item_still_in_the_catalog_is_unknown_too(w):
    """The catalog lags behind the cloud: until a rescan, the purged file is a live file nobody accounts for."""
    pid = w.plan()
    op = w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k/a"))
    w.purged(pid, op)
    w.qlive(w.gd, pid, "a.bin", size=3)
    assert w.report()["unknown"][0]["files"] == 1


def test_missing_and_directory_entries_are_not_unknown(w):
    w.qlive(w.gd, 7, "gone.bin", size=3)
    w.conn.execute("UPDATE entries SET present = 0 WHERE path LIKE '%gone.bin'")
    w.cat.dir(w.gd, f"{QDIR}/empty")
    w.live(w.gd, f"{QDIR}x/sibling.bin", size=1)       # a folder that merely starts with the same letters
    assert w.report()["unknown"] == []


def test_unknown_matching_folds_case_on_a_case_insensitive_cloud_only(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.dbx, "Docs/A.bin", witness=(w.gd, "k/a"))
    w.op(pid, 2, "delete", w.gd, "Docs/A.bin", witness=(w.gd, "k/b"))
    w.qlive(w.dbx, pid, "docs/a.BIN", size=1)           # dropbox folds case: this IS the first item
    w.qlive(w.gd, pid, "docs/a.BIN", size=1)            # gdrive does not: this is somebody else's file
    [u] = w.report()["unknown"]
    assert u["remote"] == "gdrive" and u["examples"] == [f"{QDIR}/{pid}/docs/a.BIN"]


def test_unknown_matching_ignores_unicode_form(w):
    pid = w.plan()
    nfc, nfd = unicodedata.normalize("NFC", "café.bin"), unicodedata.normalize("NFD", "café.bin")
    w.op(pid, 1, "delete", w.gd, nfc, witness=(w.gd, "k/a"))
    w.qlive(w.gd, pid, nfd, size=1)                     # the catalog spells it the way macOS does
    assert w.report()["unknown"] == []


# --------------------------------------------------------------------------- #
# plan_items
# --------------------------------------------------------------------------- #

def test_an_item_carries_everything_the_page_needs(w):
    pid = w.plan(kind="dedupe", created_by="mcp", title="dedupe each")
    op = w.op(pid, 4, "delete", w.gd, "Photos/2019/IMG_0412 (1).jpg", witness=(w.gd, "Photos/2019/IMG_0412.jpg"),
              size=4300000, reason="duplicate of the kept copy", days_ago=4)
    got = w.items(pid)
    assert got["plan"] == {"id": pid, "title": "dedupe each", "kind": "dedupe", "status": "done",
                           "created_by": "mcp", "finished_at": ts(1)}
    assert (got["total"], got["bytes"], got["offset"], got["limit"]) == (1, 4300000, 0, 200)
    [item] = got["items"]
    assert item == {
        "op_id": op, "seq": 4, "op": "delete", "remote": "gdrive", "path": "Photos/2019/IMG_0412 (1).jpg",
        "quarantine_path": f"{QDIR}/{pid}/Photos/2019/IMG_0412 (1).jpg", "size": 4300000,
        "reason": "duplicate of the kept copy", "witness": {"remote": "gdrive", "path": "Photos/2019/IMG_0412.jpg"},
        "state": "held", "quarantined_at": ts(4), "due_at": ts(4 - 30), "due": False,
        "restored_at": None, "purged_at": None,
        "catalog": {"seen": False, "original_taken": False, "witness_live": False, "witness_same": None},
    }


def test_an_item_that_names_no_witness_has_none_and_a_null_witness_flag(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "a.bin")
    [item] = w.items(pid)["items"]
    assert item["witness"] is None and item["catalog"]["witness_live"] is None
    assert item["catalog"]["witness_same"] is None


def test_items_sort_by_cloud_then_naturally_and_ignoring_case(w):
    pid = w.plan()
    paths = {w.gd: ["img10.jpg", "img2.jpg", "B/x.jpg", "a/y.jpg", "A/z.jpg", "a b.jpg"], w.dbx: ["z.jpg", "a.jpg"]}
    seq = 0
    for rid in (w.dbx, w.gd):
        for p in paths[rid]:
            seq += 1
            w.op(pid, seq, "delete", rid, p, witness=(w.od, "k"))
    got = w.items(pid)["items"]
    assert [(i["remote"], i["path"]) for i in got] == [
        ("gdrive", "a/y.jpg"), ("gdrive", "A/z.jpg"), ("gdrive", "a b.jpg"), ("gdrive", "B/x.jpg"),
        ("gdrive", "img2.jpg"), ("gdrive", "img10.jpg"), ("dropbox", "a.jpg"), ("dropbox", "z.jpg")]


def test_items_page_with_offset_and_limit(w):
    pid = w.plan()
    for n in range(1, 8):
        w.op(pid, n, "delete", w.gd, f"f{n:02d}.bin", witness=(w.gd, "k"), size=n)
    first = w.items(pid, limit=3)
    assert [i["path"] for i in first["items"]] == ["f01.bin", "f02.bin", "f03.bin"]
    assert (first["total"], first["bytes"]) == (7, 28), "totals cover the whole filter, not the page"
    second = w.items(pid, offset=3, limit=3)
    assert [i["path"] for i in second["items"]] == ["f04.bin", "f05.bin", "f06.bin"]
    assert [i["path"] for i in w.items(pid, offset=6, limit=3)["items"]] == ["f07.bin"]
    assert w.items(pid, offset=50)["items"] == []


def test_items_filter_on_cloud_and_a_whole_segment_prefix(w):
    pid = w.plan()
    for n, (rid, path) in enumerate([(w.gd, "Photos/2019/a.jpg"), (w.gd, "Photos/2019/sub/b.jpg"),
                                     (w.gd, "Photos/20190/c.jpg"), (w.gd, "Photos/2019"),
                                     (w.gd, "Other/d.jpg"), (w.dbx, "Photos/2019/e.jpg")], start=1):
        w.op(pid, n, "delete", rid, path, witness=(w.od, "k"))
    only = lambda **kw: sorted((i["remote"], i["path"]) for i in w.items(pid, **kw)["items"])   # noqa: E731
    assert only(path_prefix="Photos/2019") == [
        ("dropbox", "Photos/2019/e.jpg"), ("gdrive", "Photos/2019"), ("gdrive", "Photos/2019/a.jpg"),
        ("gdrive", "Photos/2019/sub/b.jpg")], "the 20190 folder is not inside 2019"
    assert only(path_prefix="/Photos/2019/") == only(path_prefix="Photos/2019")
    assert only(remote="dropbox") == [("dropbox", "Photos/2019/e.jpg")]
    assert only(remote="gdrive", path_prefix="Photos/2019/sub") == [("gdrive", "Photos/2019/sub/b.jpg")]
    assert w.items(pid, remote="gdrive", path_prefix="Photos/2019")["total"] == 3
    assert w.items(pid, path_prefix="")["total"] == 6


def test_catalog_flags_say_what_the_last_scan_saw(w):
    pid = w.plan()
    body = {"md5": "aa" * 16}
    w.op(pid, 1, "delete", w.gd, "ok.bin", witness=(w.gd, "keep/ok.bin"))
    w.qlive(w.gd, pid, "ok.bin", size=9, **body)
    w.live(w.gd, "keep/ok.bin", size=9, **body)
    w.op(pid, 2, "delete", w.gd, "taken.bin", witness=(w.gd, "keep/taken.bin"))
    w.qlive(w.gd, pid, "taken.bin", size=9)
    w.live(w.gd, "taken.bin", size=1)                                   # the original path is occupied
    w.live(w.gd, "keep/taken.bin", size=9)
    w.op(pid, 3, "delete", w.gd, "unseen.bin", witness=(w.gd, "keep/unseen.bin"))   # not in the catalog
    w.op(pid, 4, "delete", w.gd, "nowit.bin", witness=(w.gd, "keep/gone.bin"))
    w.qlive(w.gd, pid, "nowit.bin", size=9)
    w.live(w.gd, "keep/gone.bin", size=9)
    w.conn.execute("UPDATE entries SET present = 0 WHERE path = 'keep/gone.bin'")
    flags = {i["path"]: i["catalog"] for i in w.items(pid)["items"]}
    assert flags["ok.bin"] == {"seen": True, "original_taken": False, "witness_live": True, "witness_same": None}
    assert flags["taken.bin"]["original_taken"] is True and flags["taken.bin"]["seen"] is True
    assert flags["unseen.bin"] == {"seen": False, "original_taken": False, "witness_live": False,
                                   "witness_same": None}
    assert flags["nowit.bin"]["witness_live"] is False, "a tombstone is not a live witness"


def test_witness_same_follows_the_content_group_or_a_strong_hash(w):
    pid = w.plan()
    cases = {"grouped": (True, "group"), "strong": (True, "strong"), "other": (False, "other-strong"),
             "groups-differ": (False, "groups-differ"), "unknown": (None, "unknown")}
    seq = 0
    for name in cases:
        seq += 1
        w.op(pid, seq, "delete", w.gd, f"{name}.bin", witness=(w.dbx, f"keep/{name}.bin"))
        q = w.qlive(w.gd, pid, f"{name}.bin", size=9)
        k = w.live(w.dbx, f"keep/{name}.bin", size=9)
        if name == "grouped":
            w.conn.execute("UPDATE entries SET content_group = 5 WHERE id IN (?, ?)", (q, k))
        elif name == "strong":
            w.conn.execute("UPDATE entries SET sha256 = 'ab' WHERE id IN (?, ?)", (q, k))
        elif name == "other":
            w.conn.execute("UPDATE entries SET sha256 = 'ab' WHERE id = ?", (q,))
            w.conn.execute("UPDATE entries SET sha256 = 'cd' WHERE id = ?", (k,))
        elif name == "groups-differ":
            w.conn.execute("UPDATE entries SET content_group = 1 WHERE id = ?", (q,))
            w.conn.execute("UPDATE entries SET content_group = 2 WHERE id = ?", (k,))
        elif name == "unknown":
            w.conn.execute("UPDATE entries SET md5 = 'ee' WHERE id IN (?, ?)", (q, k))   # a weak hash proves nothing
    got = {i["path"]: i["catalog"]["witness_same"] for i in w.items(pid)["items"]}
    assert got == {f"{n}.bin": want for n, (want, _) in cases.items()}


def test_witness_same_falls_back_to_the_hash_the_op_recorded_when_the_quarantined_file_is_unseen(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "same.bin", witness=(w.dbx, "keep/same.bin"), expect_hash="sha256:ABCD")
    w.op(pid, 2, "delete", w.gd, "diff.bin", witness=(w.dbx, "keep/diff.bin"), expect_hash="sha256:abcd")
    w.op(pid, 3, "delete", w.gd, "weak.bin", witness=(w.dbx, "keep/weak.bin"), expect_hash="md5:abcd")
    w.live(w.dbx, "keep/same.bin", size=9, sha256="abcd")
    w.live(w.dbx, "keep/diff.bin", size=9, sha256="ffff")
    w.live(w.dbx, "keep/weak.bin", size=9, md5="abcd")
    got = {i["path"]: i["catalog"]["witness_same"] for i in w.items(pid)["items"]}
    assert got == {"same.bin": True, "diff.bin": False, "weak.bin": None}


def test_a_moves_witness_is_its_destination(w):
    pid = w.plan(kind="consolidate")
    w.op(pid, 1, "move", w.gd, "a/dup.bin", dst=(w.gd, "b/dup.bin"))
    w.live(w.gd, "b/dup.bin", size=3)
    [item] = w.items(pid)["items"]
    assert item["witness"] == {"remote": "gdrive", "path": "b/dup.bin"} and item["catalog"]["witness_live"] is True


def test_catalog_flags_fold_case_on_case_insensitive_clouds_and_unicode_form_everywhere(w):
    pid = w.plan()
    nfc, nfd = unicodedata.normalize("NFC", "café.bin"), unicodedata.normalize("NFD", "café.bin")
    w.op(pid, 1, "delete", w.dbx, "Docs/Report.PDF", witness=(w.gd, "keep/r.pdf"))
    w.qlive(w.dbx, pid, "docs/report.pdf", size=1)       # dropbox: the catalog spells it in other case
    w.live(w.dbx, "DOCS/REPORT.pdf", size=1)             # ... and the original path is taken in yet another
    w.op(pid, 2, "delete", w.gd, "Docs/Report.PDF", witness=(w.gd, "keep/r.pdf"))
    w.qlive(w.gd, pid, "docs/report.pdf", size=1)        # gdrive is case-sensitive: that is another file
    w.live(w.gd, "DOCS/REPORT.pdf", size=1)
    w.op(pid, 3, "delete", w.gd, nfc, witness=(w.gd, "keep/r.pdf"))
    w.qlive(w.gd, pid, nfd, size=1)
    w.live(w.gd, nfd, size=1)
    w.op(pid, 4, "delete", w.gd, nfd, witness=(w.gd, "keep/r.pdf"))     # spelled as the catalog spells it
    w.op(pid, 5, "delete", w.gd, "caf.bin", witness=(w.gd, "keep/r.pdf"))
    w.live(w.gd, f"{QDIR}/{pid}/{nfc}x", size=1)         # an unrelated name must not match
    flags = {(i["remote"], i["path"]): i["catalog"] for i in w.items(pid)["items"]}
    assert flags[("dropbox", "Docs/Report.PDF")]["seen"] is True
    assert flags[("dropbox", "Docs/Report.PDF")]["original_taken"] is True
    assert flags[("gdrive", "Docs/Report.PDF")]["seen"] is False
    assert flags[("gdrive", "Docs/Report.PDF")]["original_taken"] is False
    assert flags[("gdrive", nfc)]["seen"] is True and flags[("gdrive", nfc)]["original_taken"] is True
    assert flags[("gdrive", nfd)]["original_taken"] is True and flags[("gdrive", nfd)]["seen"] is True
    assert flags[("gdrive", "caf.bin")]["seen"] is False and flags[("gdrive", "caf.bin")]["original_taken"] is False


def test_the_catalog_is_looked_up_in_batches_not_once_per_item(w):
    pid = w.plan()
    n = 700
    for k in range(n):
        w.op(pid, k + 1, "delete", w.gd, f"d/{k:04d}.bin", witness=(w.gd, f"keep/{k:04d}.bin"))
    statements = []
    w.conn.set_trace_callback(statements.append)
    got = w.items(pid, limit=500)
    w.conn.set_trace_callback(None)
    assert len(got["items"]) == 500
    assert sum("FROM entries" in s for s in statements) <= 8, "batched look-ups, never one query per item"


@pytest.mark.parametrize("bad", [{"state": "everything"}, {"limit": 0}, {"limit": 501}, {"offset": -1},
                                 {"remote": "nosuchcloud"}])
def test_bad_queries_name_the_parameter(w, bad):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k"))
    e = err("BAD_QUERY", w.items, pid, **bad)
    assert e.params == {"param": next(iter(bad))}


def test_an_unknown_plan_is_not_found(w):
    for call in (lambda: w.items(404), lambda: w.check(404, "restore"), lambda: w.check(404, "purge")):
        e = err("NOT_FOUND", call)
        assert e.params == {"plan": 404}


def test_a_plan_with_nothing_quarantined_lists_nothing(w):
    pid = w.plan()
    w.op(pid, 1, "copy", w.gd, "a.bin", dst=(w.dbx, "a.bin"))
    got = w.items(pid, state="all")
    assert got["total"] == 0 and got["items"] == [] and got["plan"]["id"] == pid


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def _checkable(w):
    """Four held items: one occupied original, one unseen copy, one with a copy on another cloud, one plain."""
    pid = w.plan(kind="consolidate")
    ids = {}
    for seq, (name, size) in enumerate([("taken", 10), ("unseen", 20), ("moved", 30), ("plain", 40)], start=1):
        ids[name] = w.op(pid, seq, "delete", w.gd, f"d/{name}.bin", witness=(w.dbx, f"d/{name}.bin"), size=size)
        if name != "unseen":
            w.qlive(w.gd, pid, f"d/{name}.bin", size=size)
    w.live(w.gd, "d/taken.bin", size=1)
    w.op(pid, 5, "copy", w.gd, "d/moved.bin", dst=(w.dbx, "d/moved.bin"))
    w.op(pid, 6, "copy", w.gd, "d/plain.bin", dst=(w.gd, "elsewhere/plain.bin"))           # same cloud: not "another"
    w.op(pid, 7, "copy", w.gd, "d/unseen.bin", dst=(w.dbx, "d/unseen.bin"), status="failed")  # never happened
    return pid, ids


def test_restore_check_counts_what_will_stay_and_what_will_exist_twice(w):
    pid, ids = _checkable(w)
    got = w.check(pid, "restore")
    assert got == {"action": "restore", "plan_id": pid, "files": 4, "bytes": 100,
                   "original_taken": {"files": 1, "bytes": 10}, "not_seen": {"files": 1, "bytes": 20},
                   "cross_remote_copies": 1, "remotes": ["gdrive"], "op_ids": sorted(ids.values())}


def test_restore_check_of_a_selection_counts_only_the_selection(w):
    pid, ids = _checkable(w)
    got = w.check(pid, "restore", op_ids=[ids["moved"], ids["plain"], ids["moved"]])
    assert (got["files"], got["bytes"], got["cross_remote_copies"]) == (2, 70, 1)
    assert got["original_taken"] == {"files": 0, "bytes": 0} and got["not_seen"] == {"files": 0, "bytes": 0}
    assert got["op_ids"] == [ids["moved"], ids["plain"]]


def test_restore_check_names_every_cloud_it_touches_in_catalog_order(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.dbx, "a.bin", witness=(w.gd, "k"))
    w.op(pid, 2, "delete", w.gd, "b.bin", witness=(w.gd, "k"))
    assert w.check(pid, "restore")["remotes"] == ["gdrive", "dropbox"]


def test_a_restored_or_purged_item_is_not_part_of_a_check(w):
    pid, ids = _checkable(w)
    w.restored(pid, ids["plain"])
    w.purged(pid, ids["moved"])
    assert w.check(pid, "restore")["files"] == 2
    e = err("BAD_OPS", w.check, pid, "restore", op_ids=[ids["plain"], ids["moved"], ids["taken"]])
    assert e.params == {"plan": pid, "op_ids": [ids["plain"], ids["moved"]]}


def _purgeable(w):
    """Six held items of different age and witness situation."""
    pid = w.plan()
    spec = [  # name, days ago, witness situation
        ("fine", 40, "grouped"), ("old-gone", 40, "gone"), ("old-unproven", 40, "unproven"),
        ("old-unprovable", 40, "differs"), ("recent", 3, "grouped"), ("young", 20, "grouped")]
    ids = {}
    for seq, (name, days, situation) in enumerate(spec, start=1):
        ids[name] = w.op(pid, seq, "delete", w.gd, f"d/{name}.bin", witness=(w.dbx, f"k/{name}.bin"), days_ago=days,
                         size=100 * seq)
        q = w.qlive(w.gd, pid, f"d/{name}.bin", size=100 * seq)
        if situation == "gone":
            continue
        k = w.live(w.dbx, f"k/{name}.bin", size=100 * seq)
        if situation == "grouped":
            w.conn.execute("UPDATE entries SET content_group = 9 WHERE id IN (?, ?)", (q, k))
        elif situation == "differs":
            w.conn.execute("UPDATE entries SET sha256 = 'aa' WHERE id = ?", (q,))
            w.conn.execute("UPDATE entries SET sha256 = 'bb' WHERE id = ?", (k,))
    return pid, ids


def test_purge_check_separates_what_will_be_attempted_from_what_stays(w):
    pid, ids = _purgeable(w)
    got = w.check(pid, "purge", older_than_days=30)
    assert got["action"] == "purge" and got["plan_id"] == pid
    # attempted: the four 40-day-old items (100 + 200 + 300 + 400 bytes)
    assert (got["files"], got["bytes"]) == (4, 1000)
    assert got["too_recent"] == {"files": 2, "bytes": 1100}
    assert got["witness_gone"] == {"files": 1, "bytes": 200}
    assert got["witness_unproven"] == {"files": 2, "bytes": 700, "download_bytes": 300}, \
        "only the one the catalog knows nothing about may need a download; a proven difference needs none"
    assert got["recent"] == {"files": 0, "bytes": 0}
    assert got["by_remote"] == [{"remote": "gdrive", "files": 4, "bytes": 1000}]
    assert got["op_ids"] == [ids["fine"], ids["old-gone"], ids["old-unproven"], ids["old-unprovable"]]


def test_purge_check_with_zero_days_attempts_everything_and_warns_about_recent_items(w):
    pid, ids = _purgeable(w)
    got = w.check(pid, "purge", older_than_days=0)
    assert (got["files"], got["bytes"]) == (6, 2100) and got["too_recent"] == {"files": 0, "bytes": 0}
    assert got["recent"] == {"files": 1, "bytes": 500}, "quarantined less than 7 days ago"
    assert sorted(got["op_ids"]) == sorted(ids.values())


def test_purge_check_of_a_selection_and_of_other_clouds(w):
    pid = w.plan()
    a = w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.od, "k/a"), days_ago=40, size=5)
    b = w.op(pid, 2, "delete", w.dbx, "b.bin", witness=(w.od, "k/b"), days_ago=40, size=6)
    w.op(pid, 3, "delete", w.dbx, "c.bin", witness=(w.od, "k/c"), days_ago=40, size=7)
    got = w.check(pid, "purge", op_ids=[b, a], older_than_days=30)
    assert (got["files"], got["bytes"]) == (2, 11)
    assert got["by_remote"] == [{"remote": "gdrive", "files": 1, "bytes": 5}, {"remote": "dropbox", "files": 1,
                                                                                "bytes": 6}]
    assert got["op_ids"] == [b, a] or sorted(got["op_ids"]) == sorted([a, b])


def test_an_undated_item_is_never_old_enough_to_purge(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "a.bin", witness=(w.gd, "k"), days_ago=None, journal=False, size=5)
    got = w.check(pid, "purge", older_than_days=0)
    assert got["files"] == 0 and got["too_recent"] == {"files": 1, "bytes": 5}


def test_a_purge_check_counts_an_op_with_no_witness_record_as_a_witness_that_cannot_vouch(w):
    pid = w.plan()
    w.op(pid, 1, "delete", w.gd, "a.bin", days_ago=40, size=5)
    assert w.check(pid, "purge", older_than_days=30)["witness_gone"] == {"files": 1, "bytes": 5}


def test_check_errors(w):
    pid, ids = _checkable(w)
    empty = w.plan()
    w.op(empty, 1, "copy", w.gd, "a.bin", dst=(w.dbx, "a.bin"))
    assert err("NOTHING_QUARANTINED", w.check, empty, "restore").params == {"plan": empty}
    assert err("NOTHING_QUARANTINED", w.check, empty, "purge", op_ids=[1]).params == {"plan": empty}, \
        "nothing held wins over a bad selection"
    other = w.plan()
    foreign = w.op(other, 1, "delete", w.gd, "x.bin", witness=(w.gd, "k"))
    bad = [10**6, foreign, ids["plain"]]
    e = err("BAD_OPS", w.check, pid, "restore", op_ids=bad)
    assert e.params == {"plan": pid, "op_ids": [10**6, foreign]}
    assert err("BAD_OPS", w.check, pid, "purge", op_ids=[]).params["op_ids"] == []
    many = list(range(10**6, 10**6 + 50))
    assert err("BAD_OPS", w.check, pid, "restore", op_ids=many).params["op_ids"] == many[:20]
    assert err("BAD_OPS", w.check, pid, "restore", op_ids=[True]).params["op_ids"] == [True]
    assert err("BAD_QUERY", w.check, pid, "delete").params == {"param": "action"}
    assert err("BAD_QUERY", w.check, pid, "purge", older_than_days=-1).params == {"param": "older_than_days"}
    for e in (err("BAD_OPS", w.check, pid, "restore", op_ids=bad), err("BAD_QUERY", w.check, pid, "x")):
        assert isinstance(e, ValueError) and str(e)


def test_op_ids_none_means_every_held_item(w):
    pid, ids = _checkable(w)
    assert w.check(pid, "restore")["op_ids"] == w.check(pid, "restore", op_ids=None)["op_ids"]


# --------------------------------------------------------------------------- #
# Read-only
# --------------------------------------------------------------------------- #

def test_no_function_of_the_module_writes_the_catalog(w):
    pid, ids = _checkable(w)
    other = w.plan()
    w.restored(other, w.op(other, 1, "delete", w.gd, "r.bin", witness=(w.gd, "k")))
    w.live(w.gd, f"{QDIR}/77/odd.bin", size=1)
    before = w.conn.total_changes
    w.report()
    w.items(pid, state="all")
    w.items(pid, remote="gdrive", path_prefix="d")
    w.check(pid, "restore")
    w.check(pid, "restore", op_ids=list(ids.values())[:2])
    w.check(pid, "purge", older_than_days=0)
    for bad in (lambda: w.check(pid, "oops"), lambda: w.check(9999, "purge"), lambda: w.items(pid, state="x")):
        with pytest.raises(QuarantineError):
            bad()
    assert w.conn.total_changes == before
