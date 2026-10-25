"""Plan builders: keeper policy, dedupe, consolidate and reorganize.

Every plan is run through the simulator, and an independent end-state model
(`after`) checks what the ops would leave behind, so the tests do not merely
trust the component that is supposed to catch the builders' mistakes.
"""

from __future__ import annotations

import itertools
import posixpath
import unicodedata
from collections import namedtuple

import pytest

from drivemerge import builders, identity
from drivemerge.builders import (
    BuildError, build_consolidate, build_dedupe, build_manual, build_reorganize, choose_keeper, looks_like_copy,
)
from drivemerge.simulate import SafetyPolicy, simulate_ops

HASH_OF = {"drive": "sha256", "dropbox": "dropbox", "onedrive": "quickxor"}
NATIVE_MIME = "application/vnd.google-apps.document"


class World:
    """Remotes plus files labelled by content: same label = same bytes, hashed in the provider's own domain."""

    def __init__(self, catalog):
        self.cat, self.conn = catalog, catalog.conn
        self.rid: dict[str, int] = {}
        self.provider: dict[str, str] = {}
        self.groups: dict[str, int] = {}

    def remote(self, name: str, provider: str, *, writable: bool = True) -> int:
        self.rid[name] = self.cat.remote(name, provider, writable=writable)
        self.provider[name] = provider
        return self.rid[name]

    def put(self, remote: str, path: str, content: str | None = None, *, size: int | None = None, **kw) -> int:
        """content=None: an unhashed file (iCloud style) with its own identity."""
        kw.setdefault("size", size if size is not None else 10 + len(content or path))
        hash_type = HASH_OF.get(self.provider[remote])
        if content is not None and hash_type:
            kw.setdefault(hash_type, f"{hash_type}-{content}")
        eid = self.cat.file(self.rid[remote], path, **kw)
        if content is not None:
            group = self.groups.setdefault(content, len(self.groups) + 1)
            self.conn.execute("UPDATE entries SET content_group = ? WHERE id = ?", (group, eid))
        return eid

    def names(self) -> dict[int, str]:
        return {rid: name for name, rid in self.rid.items()}

    def live(self) -> dict[tuple[str, str], str]:
        """{(remote, path): content key} of every live file, quarantine included."""
        names = self.names()
        return {(names[r], p): identity.content_key({"content_group": g, "id": i}) for i, r, p, g in self.conn.execute(
            "SELECT id, remote_id, path, content_group FROM entries WHERE present = 1 AND is_dir = 0")}

    def after(self, ops) -> dict[tuple[str, str], str]:
        """Live files once the ops have run, computed without the simulator. Nothing may be overwritten."""
        state = self.live()
        names = self.names()
        ci = {name: self.provider[name] in ("dropbox", "onedrive") for name in self.rid}
        taken = {(r, p.casefold() if ci[r] else p) for r, p in state}

        def slot(remote: str, path: str) -> tuple[str, str]:
            return remote, path.casefold() if ci[remote] else path

        for op in ops:
            src = (names[op.src_remote_id], op.src_path)
            if op.op == "delete":
                del state[src]
                taken.discard(slot(*src))
                continue
            dst = (names[op.dst_remote_id], op.dst_path)
            assert slot(*dst) not in taken, f"{op.op} {src} overwrites {dst}"
            if op.op == "copy":
                state[dst] = state[src]
            else:
                state[dst] = state.pop(src)
                taken.discard(slot(*src))
            taken.add(slot(*dst))
        return state

    def key(self, remote: str, path: str) -> str:
        return self.live()[(remote, path)]

    def apply(self, ops) -> None:
        """Leave the catalog as the executor would after the ops: moves rename the row, copies add one, deletes hide it."""
        labels = {group: label for label, group in self.groups.items()}
        names = self.names()
        for op in ops:
            row = self.conn.execute("SELECT * FROM entries WHERE id = ?", (op.src_entry_id,)).fetchone()
            if op.op == "delete":
                self.conn.execute("UPDATE entries SET present = 0 WHERE id = ?", (row["id"],))
            elif op.op == "move":
                base = posixpath.basename(op.dst_path)
                key = f"p:{op.dst_path}" if row["entry_key"].startswith("p:") else row["entry_key"]
                self.conn.execute(
                    "UPDATE entries SET path = ?, parent = ?, name = ?, name_norm = ?, entry_key = ? WHERE id = ?",
                    (op.dst_path, posixpath.dirname(op.dst_path), base,
                     unicodedata.normalize("NFC", base).casefold(), key, row["id"]))
            else:
                self.put(names[op.dst_remote_id], op.dst_path, labels.get(row["content_group"]),
                         size=row["size"], mtime=row["mtime"])
        self.conn.execute("DELETE FROM entries WHERE is_dir = 1")                   # folders follow the files
        folders = set()
        for rid, parent in self.conn.execute("SELECT remote_id, parent FROM entries WHERE is_dir = 0 AND present = 1"):
            parts = parent.split("/") if parent else []
            folders.update((rid, "/".join(parts[:i])) for i in range(1, len(parts) + 1))
        for rid, folder in sorted(folders):
            self.cat.dir(rid, folder)


@pytest.fixture
def w(catalog):
    world = World(catalog)
    world.remote("gdrive", "drive")
    world.remote("dropbox", "dropbox")
    world.remote("onedrive", "onedrive")
    world.remote("icloud", "iclouddrive")
    return world


def run(w, result, **policy):
    """Simulate a build result; it must pass and lose nothing."""
    rep = simulate_ops(w.conn, result.ops, policy=SafetyPolicy(**policy))
    assert rep.ok, [(v.code, v.seq, v.message) for v in rep.violations]
    assert rep.contents_lost == 0
    return rep


def kinds(result, kind):
    return [o for o in result.ops if o.op == kind]


def dsts(result, kind="copy"):
    return sorted(o.dst_path for o in kinds(result, kind))


def reasons(result):
    return sorted(s["reason"] for s in result.skipped)


# =============================================================================
# keeper policy
# =============================================================================

C = namedtuple("C", "entry_id remote_id remote path mtime")


def cp(entry_id, path, remote="a", rid=1, mtime="2024-01-01T00:00:00Z"):
    return C(entry_id, rid, remote, path, mtime)


def test_keeper_target_beats_everything():
    on_target = cp(9, "deep/er/Copy of x (1).txt", remote="t", rid=3, mtime="2030-01-01T00:00:00Z")
    others = [cp(1, "x.txt", remote="a"), cp(2, "x.txt", remote="p", rid=2)]
    assert choose_keeper([*others, on_target], prefer_remotes=["a"], target="t") is on_target
    assert choose_keeper([*others, on_target], target=3) is on_target          # by remote id too


def test_keeper_prefer_remotes_order():
    a, b, c = cp(1, "x (1).txt", "a", 1), cp(2, "x.txt", "b", 2), cp(3, "x.txt", "c", 3)
    assert choose_keeper([b, a, c], prefer_remotes=["a", "c"]) is a             # listed beats a clean path
    assert choose_keeper([a, b, c], prefer_remotes=["c", "a"]) is c             # earlier in the list wins
    assert choose_keeper([a, b, c], prefer_remotes=["zzz"]) is b                # unknown names are harmless
    assert choose_keeper([a, b, c], prefer_remotes=[2, 1]) is b                 # ids work as well


@pytest.mark.parametrize("name", [
    "Copy of report.pdf", "Copy (2) of report.pdf", "docs/Copy of report.pdf", "COPY OF report.pdf", "report (1).pdf", "report (2).pdf",
    "report (12).pdf", "report - Copy.pdf", "report - Copy (2).pdf", "report_copy.pdf", "report_copy2.pdf",
    "report (Bob's conflicted copy 2020-01-01).pdf", "報告 副本.docx", "報告的副本.docx",
    "レポートのコピー.docx", "报告 复制.docx", "Copy of Photos/IMG.jpg", "photos (1)/IMG.jpg", "a/report (1)",
])
def test_copy_style_names(name):
    assert looks_like_copy(name)


@pytest.mark.parametrize("name", [
    "report.pdf", "docs/report.pdf", "Report (2023).pdf", "copyright.txt", "Copying notes.md",
    "image_copyright.png", "recopy_notes.txt", "report (final).pdf", "v1.2/report.pdf",
])
def test_ordinary_names(name):
    assert not looks_like_copy(name)


def test_keeper_prefers_non_copy_path_then_shallower():
    clean_deep = cp(1, "a/b/c/x.txt")
    copy_shallow = cp(2, "x (1).txt")
    clean_mid = cp(3, "a/x.txt")
    assert choose_keeper([copy_shallow, clean_deep]) is clean_deep              # looks-like-copy loses first
    assert choose_keeper([clean_deep, copy_shallow, clean_mid]) is clean_mid    # then shallower wins


def test_keeper_mtime_then_id():
    old = cp(5, "p/x.txt", mtime="2020-01-01T00:00:00Z")
    new = cp(1, "q/x.txt", mtime="2024-01-01T00:00:00Z")
    unknown = cp(0, "r/x.txt", mtime=None)
    assert choose_keeper([new, old, unknown]) is old                            # older wins, unknown last
    assert choose_keeper([new, unknown]) is new
    frac = cp(7, "s/x.txt", mtime="2020-01-01T00:00:00.5Z")
    plain = cp(6, "t/x.txt", mtime="2020-01-01T00:00:00Z")
    assert choose_keeper([frac, plain]) is plain                                # 00.5 is later than 00
    a, b = cp(4, "p/x.txt"), cp(3, "q/x.txt")
    assert choose_keeper([a, b]) is b                                           # smaller entry id breaks the tie


def test_keeper_is_deterministic_under_any_order():
    pool = [cp(1, "a/x (1).txt", "p", 1), cp(2, "a/x.txt", "q", 2), cp(3, "x.txt", "q", 2, "2023-01-01T00:00:00Z"),
            cp(4, "x.txt", "r", 3, "2022-01-01T00:00:00Z"), cp(5, "x.txt", "r", 3, "2022-01-01T00:00:00Z")]
    winners = {choose_keeper(list(p), prefer_remotes=["q"]).entry_id for p in itertools.permutations(pool)}
    assert winners == {3}


PROVIDERS = {"gd": "drive", "od": "onedrive", "dbx": "dropbox", "loc": "local", "oth": "other", "ic": "iclouddrive"}


def provider_pool(**override):
    """One identical-looking copy per provider (same depth, mtime), entry ids in the *opposite* order of the policy."""
    names = list(PROVIDERS)
    return [cp(10 - i, "x.txt", name, rid=i + 1) for i, name in enumerate(names)], {**PROVIDERS, **override}


def test_keeper_provider_order_drive_onedrive_dropbox_local_other_icloud():
    pool, providers = provider_pool()
    left = list(pool)
    order = []
    while left:
        best = choose_keeper(left, providers=providers)
        order.append(best.remote)
        left.remove(best)
    assert order == ["gd", "od", "dbx", "loc", "oth", "ic"]
    assert builders.KEEPER_PROVIDER_ORDER == ("drive", "onedrive", "dropbox", "local", "other", "iclouddrive")


def test_keeper_provider_comes_after_target_prefer_and_copy_pattern_but_before_depth_mtime_id():
    gd, ic = cp(5, "a/b/c/x.txt", "gd", 1, "2030-01-01T00:00:00Z"), cp(1, "x.txt", "ic", 2, "2000-01-01T00:00:00Z")
    providers = {"gd": "drive", "ic": "iclouddrive"}
    assert choose_keeper([ic, gd], providers=providers) is gd                    # beats shallower, older, smaller id
    assert choose_keeper([ic, gd]) is ic                                         # without provider info, as before
    assert choose_keeper([gd, ic], providers=providers, target="ic") is ic       # the target comes first
    assert choose_keeper([gd, ic], providers=providers, prefer_remotes=["ic"]) is ic
    ic_clean, gd_copy = cp(1, "x.txt", "ic", 2), cp(2, "x (1).txt", "gd", 1)
    assert choose_keeper([gd_copy, ic_clean], providers=providers) is ic_clean   # a copy-style name loses first


def test_keeper_provider_lookup_by_name_id_or_own_field_and_unknown_is_other():
    a, b, c = cp(1, "x.txt", "ic", 7), cp(2, "x.txt", "oth", 8), cp(3, "x.txt", "dbx", 9)
    assert choose_keeper([a, b, c], providers={7: "iclouddrive", 8: "other", 9: "dropbox"}) is c   # by remote id
    assert choose_keeper([a, b], providers={"ic": "iclouddrive"}) is b           # "oth" is unknown: ranks as other
    assert choose_keeper([a, b], providers={"ic": "iclouddrive", "oth": "somethingnew"}) is b
    as_dicts = [{"id": 1, "remote_id": 1, "path": "x", "provider": "iclouddrive"},
                {"id": 2, "remote_id": 2, "path": "x", "provider": "drive"}]
    assert choose_keeper(as_dicts)["id"] == 2                                    # a copy's own provider field counts
    assert choose_keeper([cp(2, "x", "m", 1), cp(1, "x", "l", 2)], providers={"m": "memory", "l": "local"}).entry_id == 1


def test_keeper_with_providers_is_deterministic_under_any_order():
    pool, providers = provider_pool()
    winners = {choose_keeper(list(p), providers=providers).remote for p in itertools.permutations(pool)}
    assert winners == {"gd"}


def test_keeper_accepts_copies_rows_and_dicts(w):
    w.put("gdrive", "a/x.txt", "X", mtime="2020-01-01T00:00:00Z")
    w.put("gdrive", "x (1).txt", "X")
    w.put("dropbox", "x.txt", "X")
    group = identity.duplicate_groups(w.conn)[0]
    best = choose_keeper(group.copies, prefer_remotes=["dropbox"])
    assert (best.remote, best.path) == ("dropbox", "x.txt")
    rows = w.conn.execute("SELECT * FROM entries WHERE is_dir = 0 AND remote_id = ? ORDER BY id",
                          (w.rid["gdrive"],)).fetchall()
    assert choose_keeper(rows)["path"] == "a/x.txt"                             # the copy-style name loses
    as_dicts = [{"id": 2, "remote_id": 1, "path": "b.txt"}, {"id": 1, "remote_id": 1, "path": "a.txt"}]
    assert choose_keeper(as_dicts)["id"] == 1
    with pytest.raises(ValueError):
        choose_keeper([])


# =============================================================================
# dedupe
# =============================================================================

def _onedrive_pair(catalog):
    od = catalog.remote("od", "onedrive")
    qx = "ab" * 20
    a = catalog.file(od, "keep/report.bin", size=320, quickxor=qx)
    b = catalog.file(od, "other/report.bin", size=320, quickxor=qx)
    return a, b


def _hashes(tag: str, quickxor: str) -> dict:
    import hashlib
    raw = tag.encode()
    return {"md5": hashlib.md5(raw).hexdigest(), "sha1": hashlib.sha1(raw).hexdigest(),
            "sha256": hashlib.sha256(raw).hexdigest(), "dropbox": hashlib.sha256(b"d" + raw).hexdigest(),
            "quickxor": quickxor, "size": 320}


def test_dedupe_never_deletes_onedrive_files_on_a_shared_quickxor_alone(catalog):
    """Two files of one size and one QuickXorHash may still be different files (rows swapped, say): until
    each was read and hashed there is no proof, so there is nothing to delete."""
    a, b = _onedrive_pair(catalog)
    identity.rebuild_groups(catalog.conn)
    assert build_dedupe(catalog.conn).ops == []
    assert [g.confidence for g in identity.duplicate_groups(catalog.conn, confidence="probable")] == ["probable"]

    # Both were downloaded and they are different files with the same QuickXor: still nothing.
    identity.record_bridge(catalog.conn, _hashes("one", "ab" * 20), source_entry_id=a, source_mtime="2024-01-01T00:00:00Z")
    identity.record_bridge(catalog.conn, _hashes("two", "ab" * 20), source_entry_id=b, source_mtime="2024-01-01T00:00:00Z")
    identity.rebuild_groups(catalog.conn)
    assert build_dedupe(catalog.conn).ops == []


def test_dedupe_deletes_a_onedrive_duplicate_once_both_copies_were_hashed(catalog):
    a, b = _onedrive_pair(catalog)
    for eid in (a, b):
        identity.record_bridge(catalog.conn, _hashes("same", "ab" * 20), source_entry_id=eid,
                               source_mtime="2024-01-01T00:00:00Z")
    identity.rebuild_groups(catalog.conn)
    r = build_dedupe(catalog.conn)
    (op,) = r.ops
    assert (op.op, op.src_path, op.witness_path) == ("delete", "other/report.bin", "keep/report.bin")
    rep = simulate_ops(catalog.conn, r.ops)
    assert rep.ok, [(v.code, v.message) for v in rep.violations]


def test_dedupe_within_one_remote_keeps_one_and_leaves_other_remotes(w):
    w.put("gdrive", "Docs/report.pdf", "R")
    w.put("gdrive", "Docs/Copy of report.pdf", "R")
    w.put("gdrive", "Archive/2019/old/report.pdf", "R")
    w.put("gdrive", "Docs/unique.txt", "U")
    w.put("dropbox", "report.pdf", "R")                                         # another remote: not thinned
    r = build_dedupe(w.conn, remote="gdrive")
    assert sorted(o.src_path for o in r.ops) == ["Archive/2019/old/report.pdf", "Docs/Copy of report.pdf"]
    for o in r.ops:
        assert o.op == "delete"
        assert (o.witness_remote_id, o.witness_path) == (w.rid["gdrive"], "Docs/report.pdf")
        assert o.reason == "duplicate of gdrive:Docs/report.pdf"
        assert o.src_entry_id and o.expect_size and o.expect_hash.startswith("sha256:")
    rep = run(w, r)
    assert (rep.contents_before, rep.contents_after) == (2, 2)
    after = w.after(r.ops)
    assert ("dropbox", "report.pdf") in after and ("gdrive", "Docs/report.pdf") in after
    assert r.stats["deletes"] == 2 and r.stats["groups"] == 1 and r.stats["bytes_reclaimed"] == 2 * (10 + 1)
    assert r.skipped == []


def test_dedupe_without_remote_thins_every_remote_on_its_own(w):
    for name in ("gdrive", "dropbox", "onedrive"):
        w.put(name, "a/x.bin", "X")
        w.put(name, "b/x.bin", "X")
    r = build_dedupe(w.conn)
    assert len(r.ops) == 3
    assert {o.src_remote_id for o in r.ops} == {w.rid["gdrive"], w.rid["dropbox"], w.rid["onedrive"]}
    assert all(o.witness_remote_id == o.src_remote_id for o in r.ops)           # never across remotes
    run(w, r)
    after = w.after(r.ops)
    assert sum(1 for (rem, _), k in after.items() if k == w.key("gdrive", "a/x.bin")) == 3


def test_cross_remote_dedupe_keeps_one_overall_and_skips_read_only(w):
    w.remote("archive", "dropbox", writable=False)
    w.put("gdrive", "Photos/p.jpg", "P")
    w.put("dropbox", "Camera/p.jpg", "P")
    w.put("onedrive", "Pictures/p (1).jpg", "P")
    w.put("archive", "p.jpg", "P")                                              # read-only: kept, never deleted
    r = build_dedupe(w.conn, cross_remote=True, prefer_remotes=["gdrive"])
    assert sorted((w.names()[o.src_remote_id], o.src_path) for o in r.ops) == [
        ("dropbox", "Camera/p.jpg"), ("onedrive", "Pictures/p (1).jpg")]
    assert all((o.witness_remote_id, o.witness_path) == (w.rid["gdrive"], "Photos/p.jpg") for o in r.ops)
    assert r.skipped == [{"entry_id": w.cat.conn.execute(
        "SELECT id FROM entries WHERE path = 'p.jpg'").fetchone()[0], "location": "archive:p.jpg",
        "code": "READ_ONLY", "reason": "read-only remote"}]
    run(w, r)
    assert ("archive", "p.jpg") in w.after(r.ops) and ("gdrive", "Photos/p.jpg") in w.after(r.ops)


def test_cross_remote_keeper_may_live_on_a_read_only_remote(w):
    w.remote("archive", "dropbox", writable=False)
    w.put("archive", "p.jpg", "P")
    w.put("gdrive", "copy/p.jpg", "P")
    r = build_dedupe(w.conn, cross_remote=True, prefer_remotes=["archive"])
    assert [(o.src_path, o.witness_path) for o in r.ops] == [("copy/p.jpg", "p.jpg")]
    run(w, r)


def test_cross_remote_dedupe_does_not_keep_the_bridged_icloud_copy(w):
    w.put("icloud", "x.pdf", "X", mtime="2001-01-01T00:00:00Z")                 # shallowest, oldest, lowest id
    w.put("dropbox", "deep/er/x.pdf", "X")
    w.put("onedrive", "deeper/still/x.pdf", "X")
    r = build_dedupe(w.conn, cross_remote=True)
    assert sorted((w.names()[o.src_remote_id], o.witness_path) for o in r.ops) == [
        ("dropbox", "deeper/still/x.pdf"), ("icloud", "deeper/still/x.pdf")]     # the onedrive copy outranks dropbox
    assert all(o.witness_remote_id == w.rid["onedrive"] for o in r.ops)
    run(w, r)
    kept = build_dedupe(w.conn, cross_remote=True, prefer_remotes=["icloud"])    # the owner can still choose it
    assert {o.witness_remote_id for o in kept.ops} == {w.rid["icloud"]}


def test_cross_remote_dedupe_prefers_drive_then_onedrive_then_dropbox(w):
    for name in ("icloud", "dropbox", "onedrive", "gdrive"):
        w.put(name, f"{name}/x.bin", "X")
    r = build_dedupe(w.conn, cross_remote=True)
    assert {(o.witness_remote_id, o.witness_path) for o in r.ops} == {(w.rid["gdrive"], "gdrive/x.bin")}
    assert sorted(w.names()[o.src_remote_id] for o in r.ops) == ["dropbox", "icloud", "onedrive"]
    run(w, r)


def test_dedupe_read_only_remote_alone_yields_nothing(w):
    w.remote("archive", "dropbox", writable=False)
    w.put("archive", "a.txt", "X")
    w.put("archive", "b.txt", "X")
    r = build_dedupe(w.conn, remote="archive")
    assert r.ops == [] and reasons(r) == ["read-only remote"]


def test_dedupe_never_touches_ambiguous_drive_paths(w):
    # two different Drive files share the path 'a.txt'; one of them duplicates 'b/a.txt'
    w.put("gdrive", "a.txt", "X", provider_id="id1")
    w.put("gdrive", "a.txt", "Y", provider_id="id2")
    w.put("gdrive", "b/a.txt", "X")
    w.put("gdrive", "deep/er/a.txt", "X")
    r = build_dedupe(w.conn, remote="gdrive")
    assert [o.src_path for o in r.ops] == ["deep/er/a.txt"]                     # ambiguous: neither victim nor witness
    assert r.ops[0].witness_path == "b/a.txt"
    assert reasons(r) == ["ambiguous path"] and r.skipped[0]["location"] == "gdrive:a.txt"
    run(w, r)


def test_dedupe_all_copies_ambiguous_means_no_ops(w):
    w.put("gdrive", "a.txt", "X", provider_id="id1")
    w.put("gdrive", "a.txt", "X", provider_id="id2")                            # same path, same content
    r = build_dedupe(w.conn, remote="gdrive")
    assert r.ops == [] and reasons(r) == ["ambiguous path", "ambiguous path"]


def test_dedupe_keeper_is_never_an_ambiguous_path(w):
    w.put("gdrive", "a.txt", "X", provider_id="id1")                            # would be the best keeper ...
    w.put("gdrive", "a.txt", "Y", provider_id="id2")                            # ... but its path is ambiguous
    w.put("gdrive", "m/n/x.txt", "X")
    w.put("gdrive", "m/n/o/x.txt", "X")
    r = build_dedupe(w.conn, remote="gdrive")
    assert [(o.src_path, o.witness_path) for o in r.ops] == [("m/n/o/x.txt", "m/n/x.txt")]
    run(w, r)


def test_dedupe_path_prefix_limits_deletes_not_the_keeper(w):
    w.put("gdrive", "Keep/x.bin", "X")
    w.put("gdrive", "Scratch/a/x.bin", "X")
    w.put("gdrive", "Scratch/b/x.bin", "X")
    w.put("gdrive", "Other/x.bin", "X")
    r = build_dedupe(w.conn, remote="gdrive", path_prefix="Scratch")
    assert sorted(o.src_path for o in r.ops) == ["Scratch/a/x.bin", "Scratch/b/x.bin"]
    assert all(o.witness_path == "Keep/x.bin" for o in r.ops)                   # the keeper lies outside the prefix
    run(w, r)


def test_dedupe_min_size_native_docs_and_quarantine(w):
    w.put("gdrive", "tiny/a", "T", size=5)
    w.put("gdrive", "tiny/b", "T", size=5)
    w.put("gdrive", "doc1", None, native_doc=True, mime=NATIVE_MIME, size=None)
    w.put("gdrive", "doc2", None, native_doc=True, mime=NATIVE_MIME, size=None)
    w.put("gdrive", "Q/x", "Q")
    w.put("gdrive", ".drive-merge-quarantine/7/Q/x", "Q")                       # a quarantined copy is not a live copy
    assert build_dedupe(w.conn, remote="gdrive", min_size=10).ops == []
    assert len(build_dedupe(w.conn, remote="gdrive", min_size=1).ops) == 1      # only the tiny pair
    assert build_dedupe(w.conn, remote="gdrive", min_size=1).ops[0].src_path == "tiny/b"


def test_dedupe_ignores_probable_duplicates(w):
    w.put("gdrive", "x/report.pdf", "R")
    w.put("icloud", "y/report.pdf", None, size=10 + len("R"))                    # same size and name, no proof
    assert build_dedupe(w.conn, cross_remote=True).ops == []


def test_dedupe_pages_through_every_group_and_honours_limit_groups(w, monkeypatch):
    monkeypatch.setattr(builders, "_PAGE", 3)
    for i in range(10):
        w.put("gdrive", f"d{i}/f.bin", f"C{i}", size=100 + i)
        w.put("gdrive", f"e{i}/f.bin", f"C{i}", size=100 + i)
    r = build_dedupe(w.conn, remote="gdrive")
    assert len(r.ops) == 10 and r.stats["groups"] == 10
    assert len({o.src_entry_id for o in r.ops}) == 10
    limited = build_dedupe(w.conn, remote="gdrive", limit_groups=4)
    assert len(limited.ops) == 4
    assert [o.expect_size for o in limited.ops] == [109, 108, 107, 106]        # biggest savings first
    run(w, r)


def test_dedupe_errors(w):
    with pytest.raises(BuildError):
        build_dedupe(w.conn, remote="nope")
    with pytest.raises(BuildError):
        build_dedupe(w.conn, prefer_remotes=["nope"])


# ----------------------------------------------------------------------------- dedupe over `groups`

def _group_key(w, content):
    return f"g:{w.groups[content]}"


@pytest.fixture
def several(w):
    for i, tag in enumerate("ABCD"):
        w.put("gdrive", f"keep/{tag}.bin", tag, size=100 + i)
        w.put("gdrive", f"copies/{tag} (1).bin", tag, size=100 + i)
        w.put("dropbox", f"d/{tag}.bin", tag, size=100 + i)
    return w


def test_groups_restricts_to_exactly_those_groups(several):
    w = several
    full = build_dedupe(w.conn)
    some = build_dedupe(w.conn, groups=[_group_key(w, "A"), _group_key(w, "C")])
    assert some.stats["groups"] == 2 and some.stats["groups_examined"] == 2
    assert {o.src_path for o in some.ops} == {"copies/A (1).bin", "copies/C (1).bin"}
    mine = {x.src_path for x in some.ops}
    assert sorted((o.src_path, o.witness_path) for o in some.ops) == sorted(
        (o.src_path, o.witness_path) for o in full.ops if o.src_path in mine)       # the same choices, fewer groups
    run(w, some)


def test_groups_keeps_every_other_parameter(several):
    w = several
    keys = [_group_key(w, t) for t in "ABCD"]
    assert len(build_dedupe(w.conn, cross_remote=True, groups=keys).ops) == 8
    assert len(build_dedupe(w.conn, cross_remote=True, groups=keys[:1]).ops) == 2
    only = build_dedupe(w.conn, remote="dropbox", groups=keys)             # dropbox has one copy per group: no pool
    assert only.ops == []
    assert len(build_dedupe(w.conn, remote="gdrive", groups=keys, limit_groups=2).ops) == 2
    assert len(build_dedupe(w.conn, remote="gdrive", groups=keys, path_prefix="copies/A").ops) == 0
    assert {o.src_path for o in build_dedupe(w.conn, groups=keys, path_prefix="copies").ops} == {
        f"copies/{t} (1).bin" for t in "ABCD"}
    assert build_dedupe(w.conn, groups=keys, min_size=102).stats["groups_examined"] == 2
    forced = build_dedupe(w.conn, groups=keys[:1], keep={keys[0]: w.conn.execute(
        "SELECT id FROM entries WHERE path = 'copies/A (1).bin'").fetchone()[0]})
    assert [o.src_path for o in forced.ops] == ["keep/A.bin"]
    skipped = build_dedupe(w.conn, groups=keys, skip_groups=[keys[1]])
    assert skipped.stats["groups"] == 3


def test_groups_empty_list_builds_nothing_and_none_is_unchanged(several):
    w = several
    assert build_dedupe(w.conn, groups=[]).ops == []
    a, b = build_dedupe(w.conn), build_dedupe(w.conn, groups=None)
    assert (a.ops, a.skipped, a.stats) == (b.ops, b.skipped, b.stats)
    assert "groups_missing" not in a.stats


def test_groups_missing_counts_keys_that_are_no_group(several):
    w = several
    single = w.put("gdrive", "solo.bin", "S")                                # one copy: not a duplicate group
    r = build_dedupe(w.conn, groups=[_group_key(w, "A"), "g:424242", _group_key(w, "S"), "g:424243"])
    assert r.stats["groups_missing"] == 3 and r.stats["groups"] == 1
    assert build_dedupe(w.conn, groups=[_group_key(w, "A")]).stats["groups_missing"] == 0
    assert single


@pytest.mark.parametrize("bad", ["g:1", "x", ["e:5"], ["g:x"], ["g:"], [5], [None], "g:1,g:2", {"g:1": 1},
                                 [f"g:{i}" for i in range(1, 5002)]])
def test_groups_must_be_a_list_of_group_keys(w, bad):
    with pytest.raises(BuildError) as e:
        build_dedupe(w.conn, groups=bad)
    assert e.value.code == "BAD_PARAM" and e.value.params == {"param": "groups"}


def test_keep_and_skip_outside_groups_are_bad_group(several):
    w = several
    a, b = _group_key(w, "A"), _group_key(w, "B")
    with pytest.raises(BuildError) as e:
        build_dedupe(w.conn, groups=[a], skip_groups=[b])
    assert (e.value.code, e.value.params) == ("BAD_GROUP", {"group": b})
    with pytest.raises(BuildError) as e:
        build_dedupe(w.conn, groups=[a], keep={b: 1})
    assert e.value.code in ("BAD_GROUP", "BAD_KEEPER")
    with pytest.raises(BuildError) as e:
        build_dedupe(w.conn, groups=[a], skip_groups=["g:424242"])
    assert e.value.code == "BAD_GROUP"


# =============================================================================
# consolidate
# =============================================================================

def source_world(w):
    """gdrive is the target and already holds content A; the other clouds hold the rest."""
    w.put("gdrive", "Photos/IMG_20190304_101010.jpg", "A")
    w.put("dropbox", "Camera/IMG_20190304_101010.jpg", "A")
    w.put("dropbox", "Docs/report.pdf", "R", mtime="2020-05-05T00:00:00Z")
    w.put("dropbox", "Code/myrepo/.git/HEAD", "H")
    w.put("dropbox", "Code/myrepo/main.py", "M")
    w.put("onedrive", "Pics/IMG_20190304_101010 (1).jpg", "A")
    w.put("onedrive", "Notes/n.txt", "N", mtime="2021-02-02T00:00:00Z")
    w.put("icloud", "Misc/lonely.bin", None)


def test_consolidate_copy_only_puts_every_content_on_the_target(w):
    source_world(w)
    before = w.live()
    r = build_consolidate(w.conn, target="gdrive", target_root="Archive")
    assert {o.op for o in r.ops} == {"copy"}                                    # no deletes, no moves
    assert dsts(r) == [
        "Archive/Documents/2020/report.pdf", "Archive/Documents/2021/n.txt", "Archive/Projects/myrepo/.git/HEAD",
        "Archive/Projects/myrepo/main.py", "Archive/Unsorted/icloud/Misc/lonely.bin"]
    assert all(o.dst_remote_id == w.rid["gdrive"] for o in r.ops)
    by_dst = {o.dst_path: w.names()[o.src_remote_id] for o in r.ops}
    assert by_dst["Archive/Documents/2020/report.pdf"] == "dropbox"
    assert by_dst["Archive/Unsorted/icloud/Misc/lonely.bin"] == "icloud"
    assert r.stats == {"contents": 6, "already_on_target": 1, "to_copy": 5, "bytes_to_copy": sum(
        o.expect_size for o in r.ops), "bytes_cross_remote": sum(o.expect_size for o in r.ops), "moves": 0, "moves_on_target": 0, "deletes": 0,
        "skipped": 0, "skipped_by_reason": {}}
    rep = run(w, r)
    assert rep.op_counts == {"copy": 5, "move": 0, "delete": 0}
    after = w.after(r.ops)
    assert {k for k in before} <= set(after), "sources must be untouched"
    target_keys = {k for (rem, _), k in after.items() if rem == "gdrive"}
    assert {k for k in before.values()} <= target_keys, "every content must now be on the target"


def test_copy_source_prefers_hashed_copies_in_provider_order(w):
    w.put("icloud", "a/doc.pdf", None)                                          # unhashed: a poor source
    w.put("onedrive", "b/doc.pdf", "D")
    w.put("dropbox", "c/doc.pdf", "D")
    w.put("gdrive", "d/photo.jpg", "P")
    w.put("onedrive", "e/photo.jpg", "P")
    w.remote("target", "drive")
    r = build_consolidate(w.conn, target="target")
    src_of = {o.dst_path: w.names()[o.src_remote_id] for o in r.ops}
    assert src_of["Documents/2024/doc.pdf"] == "dropbox"                        # drive > dropbox > onedrive
    assert src_of["Photos/2024/2024-01/photo.jpg"] == "gdrive"
    assert "Documents/2024/doc (2).pdf" in src_of and src_of["Documents/2024/doc (2).pdf"] == "icloud"
    run(w, r)


def test_copy_source_prefers_a_copy_with_a_strong_hash_over_a_quickxor_only_one(w):
    """A OneDrive copy is no better than an unhashed one for the executor (it must read it); a local copy with
    sha256 is verified for free, though OneDrive ranks ahead of local by provider."""
    w.remote("local", "local")
    w.remote("target", "drive")
    w.put("onedrive", "b/doc.pdf", "D")
    w.put("local", "c/doc.pdf", "D", sha256="sha256-D")
    r = build_consolidate(w.conn, target="target")
    (op,) = r.ops
    assert w.names()[op.src_remote_id] == "local"
    run(w, r)


def test_consolidate_delete_sources_witnesses_and_dependencies(w):
    source_world(w)
    w.put("gdrive", "Old/IMG_20190304_101010.jpg", "A")                         # an extra copy on the target
    r = build_consolidate(w.conn, target="gdrive", target_root="Archive", delete_sources=True)
    rep = run(w, r)
    kinds_in_order = [o.op for o in r.ops]
    assert kinds_in_order == sorted(kinds_in_order, key=["move", "copy", "delete"].index)
    deletes = kinds(r, "delete")
    assert sorted((w.names()[o.src_remote_id], o.src_path) for o in deletes) == sorted([
        ("dropbox", "Camera/IMG_20190304_101010.jpg"), ("dropbox", "Docs/report.pdf"),
        ("dropbox", "Code/myrepo/.git/HEAD"), ("dropbox", "Code/myrepo/main.py"),
        ("onedrive", "Pics/IMG_20190304_101010 (1).jpg"), ("onedrive", "Notes/n.txt"), ("icloud", "Misc/lonely.bin"),
        ("gdrive", "Old/IMG_20190304_101010.jpg")])
    copies_by_dst = {o.dst_path: i for i, o in enumerate(r.ops, start=1) if o.op == "copy"}
    target_files = {p for (rem, p) in w.live() if rem == "gdrive"}
    for o in deletes:
        assert o.witness_remote_id == w.rid["gdrive"]
        if o.witness_path in copies_by_dst:
            assert o.depends_on == copies_by_dst[o.witness_path], o
            assert r.ops[o.depends_on - 1].op == "copy"
        else:
            assert o.witness_path in target_files and o.depends_on is None, o
    extra = next(o for o in deletes if o.src_path == "Old/IMG_20190304_101010.jpg")
    assert extra.witness_path == "Photos/IMG_20190304_101010.jpg"               # the kept target copy
    after = w.after(r.ops)
    assert {k for (rem, _), k in after.items() if rem != "gdrive"} == set(), "no source copy may remain"
    assert {k for k in w.live().values()} <= set(after.values()), "every content must survive"
    assert rep.contents_lost == 0 and r.stats["deletes"] == 8


def test_consolidate_leaves_content_already_on_target_alone(w):
    w.put("gdrive", "Mine/x.pdf", "X")
    w.put("gdrive", "Mine/only-here.pdf", "O")
    w.put("dropbox", "x.pdf", "X")
    r = build_consolidate(w.conn, target="gdrive")
    assert r.ops == [] and r.stats["to_copy"] == 0
    assert (r.stats["contents"], r.stats["already_on_target"]) == (2, 2)        # target-only content is counted too


def test_unhashed_icloud_file_with_probable_match_needs_bridging(w):
    w.put("gdrive", "Work/report.pdf", "R")
    w.put("icloud", "Docs/report.pdf", None, size=10 + len("R"))                 # same size and name, no hash
    r = build_consolidate(w.conn, target="gdrive")
    assert r.ops == []
    assert r.skipped == [{"entry_id": r.skipped[0]["entry_id"], "location": "icloud:Docs/report.pdf",
                          "code": "PROBABLE_ONLY", "reason": "needs bridging: run drivemerge bridge"}]
    assert r.stats["skipped"] == 1 and r.stats["to_copy"] == 0
    # include_probable trusts the name+size match: nothing is copied
    r = build_consolidate(w.conn, target="gdrive", include_probable=True)
    assert r.ops == [] and r.skipped == []
    assert (r.stats["contents"], r.stats["already_on_target"]) == (2, 2)


def test_include_probable_with_delete_needs_the_probable_delete_policy(w):
    w.put("gdrive", "Work/report.pdf", "R")
    w.put("icloud", "Docs/report.pdf", None, size=10 + len("R"))
    r = build_consolidate(w.conn, target="gdrive", include_probable=True, delete_sources=True)
    assert [(o.op, o.src_path, o.witness_path, o.depends_on) for o in r.ops] == [
        ("delete", "Docs/report.pdf", "Work/report.pdf", None)]
    default = simulate_ops(w.conn, r.ops)
    assert not default.ok and [v.code for v in default.violations] == ["WITNESS_MISMATCH"]
    lenient = simulate_ops(w.conn, r.ops, policy=SafetyPolicy(allow_probable_delete=True))
    assert lenient.ok and [v.code for v in lenient.warnings] == ["PROBABLE_ONLY"]


def test_unhashed_file_without_a_match_is_copied_and_can_be_deleted_afterwards(w):
    w.put("icloud", "Notes/only-here.txt", None)
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    assert [(o.op, o.dst_path or o.witness_path) for o in r.ops] == [
        ("copy", "Documents/2024/only-here.txt"), ("delete", "Documents/2024/only-here.txt")]
    assert r.ops[1].depends_on == 1
    run(w, r)


def test_collisions_get_numbered_names(w):
    w.put("gdrive", "Photos/2020/2020-01/IMG_20200101_000000.jpg", "W")         # different content on the target
    w.put("dropbox", "a/IMG_20200101_000000.jpg", "X")
    w.put("onedrive", "b/IMG_20200101_000000.jpg", "Y")
    w.put("onedrive", "c/IMG_20200101_000000.jpg", "Z")
    r = build_consolidate(w.conn, target="gdrive")
    assert dsts(r) == [
        "Photos/2020/2020-01/IMG_20200101_000000 (2).jpg", "Photos/2020/2020-01/IMG_20200101_000000 (3).jpg",
        "Photos/2020/2020-01/IMG_20200101_000000 (4).jpg"]
    assert len({o.dst_path for o in r.ops}) == 3
    run(w, r)


def test_running_out_of_numbered_names_skips_instead_of_overwriting(w, monkeypatch):
    monkeypatch.setattr(builders, "_MAX_SUFFIX", 3)
    w.put("gdrive", "Photos/2020/2020-01/IMG_20200101_000000.jpg", "W")
    for folder, content in (("a", "X"), ("b", "Y"), ("c", "Z")):
        w.put("dropbox", f"{folder}/IMG_20200101_000000.jpg", content)
    r = build_consolidate(w.conn, target="gdrive")
    assert dsts(r) == ["Photos/2020/2020-01/IMG_20200101_000000 (2).jpg",
                       "Photos/2020/2020-01/IMG_20200101_000000 (3).jpg"]
    assert len(r.skipped) == 1 and "numbered names are taken" in r.skipped[0]["reason"]
    run(w, r)


def test_collision_with_an_existing_numbered_name_and_identical_content(w):
    w.put("gdrive", "Photos/2020/2020-01/IMG_20200101_000000.jpg", "W")
    w.put("gdrive", "Photos/2020/2020-01/IMG_20200101_000000 (2).jpg", "X")     # X is already there, renamed
    w.put("dropbox", "a/IMG_20200101_000000.jpg", "X")
    w.put("dropbox", "b/IMG_20200101_000000.jpg", "Y")
    r = build_consolidate(w.conn, target="gdrive")
    assert dsts(r) == ["Photos/2020/2020-01/IMG_20200101_000000 (3).jpg"]       # X: nothing to do; Y: first free name
    run(w, r)


def test_case_insensitive_target_collides_on_folded_names(w):
    w.put("dropbox", "Photos/2020/2020-01/img_20200101_000000.JPG", "W")
    w.put("gdrive", "p/IMG_20200101_000000.jpg", "X")                           # same name modulo case, other content
    w.put("gdrive", "q/img_20200101_000000.jpg", "Y")                           # collides with X once folded
    r = build_consolidate(w.conn, target="dropbox")
    assert dsts(r) == ["Photos/2020/2020-01/IMG_20200101_000000 (2).jpg",
                       "Photos/2020/2020-01/img_20200101_000000 (3).jpg"]
    run(w, r)                                                                   # no CASE_COLLISION


def test_case_sensitive_target_keeps_names_that_differ_only_by_case(w):
    w.put("dropbox", "p/IMG_20200101_000000.jpg", "X")
    w.put("onedrive", "q/img_20200101_000000.jpg", "Y")
    w.remote("localdisk", "local")
    r = build_consolidate(w.conn, target="localdisk")
    assert dsts(r) == ["Photos/2020/2020-01/IMG_20200101_000000.jpg", "Photos/2020/2020-01/img_20200101_000000.jpg"]
    run(w, r)


def test_destination_under_a_file_is_not_used(w):
    w.put("gdrive", "Documents/2024", "F")                                      # a FILE named like a needed folder
    w.put("dropbox", "r.pdf", "R")
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    assert r.ops == [] and "parent folder is a file" in r.skipped[0]["reason"]  # and the source is not deleted either


def test_destination_named_like_an_existing_folder_is_renamed(w):
    w.cat.dir(w.rid["gdrive"], "Pics")
    w.put("dropbox", "Pics", "S", size=3)
    all_in_root = _rules('[[rule]]\nname = "all"\ntarget = "{name}"')
    r = build_consolidate(w.conn, target="gdrive", ruleset=all_in_root)
    assert dsts(r) == ["Pics (2)"]
    run(w, r)


def _rules(text):
    return builders.classify.load_rules(text)


def test_units_stay_together_and_keep_their_repeated_files(w):
    w.put("dropbox", "Code/repo/.git/HEAD", "H")
    w.put("dropbox", "Code/repo/main.py", "M")
    w.put("dropbox", "Code/repo/pkg/__init__.py", "E")                          # identical files inside one unit ...
    w.put("dropbox", "Code/repo/tests/__init__.py", "E")                        # ... must both arrive
    w.put("onedrive", "Downloads/main.py", "M")                                 # a loose twin of a unit file
    r = build_consolidate(w.conn, target="gdrive")
    assert dsts(r) == ["Projects/repo/.git/HEAD", "Projects/repo/main.py", "Projects/repo/pkg/__init__.py",
                       "Projects/repo/tests/__init__.py"]
    run(w, r)
    # with delete_sources every twin goes, each witnessed by a file in the unit
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    run(w, r)
    assert len(kinds(r, "copy")) == 4 and len(kinds(r, "delete")) == 5
    twin = next(o for o in kinds(r, "delete") if o.src_path == "Downloads/main.py")
    assert twin.witness_path == "Projects/repo/main.py"
    assert r.ops[twin.depends_on - 1].dst_path == "Projects/repo/main.py"
    assert not [k for (rem, p), k in w.after(r.ops).items() if rem != "gdrive"]


def test_unit_file_already_in_place_on_the_target_is_not_copied_again(w):
    w.put("gdrive", "Projects/repo/pkg/__init__.py", "E")
    w.put("gdrive", "Projects/repo/.git/HEAD", "H")
    w.put("dropbox", "Code/repo/.git/HEAD", "H")
    w.put("dropbox", "Code/repo/pkg/__init__.py", "E")
    w.put("dropbox", "Code/repo/tests/__init__.py", "E")
    r = build_consolidate(w.conn, target="gdrive")
    assert dsts(r) == ["Projects/repo/tests/__init__.py"]
    run(w, r)
    d = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    run(w, d)
    assert len(kinds(d, "delete")) == 3 and len(kinds(d, "copy")) == 1
    assert {o.depends_on for o in kinds(d, "delete") if o.src_path.endswith("tests/__init__.py")} == {1}


def test_native_docs_are_never_copied_or_deleted(w):
    w.remote("gdrive2", "drive")
    w.put("gdrive2", "Other/Notes", None, native_doc=True, mime=NATIVE_MIME, size=None)
    w.put("gdrive", "Stuff/Plan", None, native_doc=True, mime=NATIVE_MIME, size=None)
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    assert r.ops == [] and len(r.skipped) == 1 and r.skipped[0]["reason"].startswith("native doc")
    moved = build_consolidate(w.conn, target="gdrive", reorganize_target=True)
    assert [(o.op, o.src_path, o.dst_path) for o in moved.ops] == [("move", "Stuff/Plan", "Documents/2024/Plan")]
    run(w, moved)


def test_reorganize_target_moves_the_kept_target_copy_into_the_layout(w):
    w.put("gdrive", "Misc/IMG_20190304_101010.jpg", "A")
    w.put("dropbox", "Cam/IMG_20190304_101010.jpg", "A")
    r = build_consolidate(w.conn, target="gdrive", reorganize_target=True, delete_sources=True)
    assert [(o.op, o.src_path, o.dst_path or o.witness_path, o.depends_on) for o in r.ops] == [
        ("move", "Misc/IMG_20190304_101010.jpg", "Photos/2019/2019-03/IMG_20190304_101010.jpg", None),
        ("delete", "Cam/IMG_20190304_101010.jpg", "Photos/2019/2019-03/IMG_20190304_101010.jpg", 1)]
    run(w, r)
    # already in the layout: nothing moves
    w.conn.execute("UPDATE entries SET path = 'Photos/2019/2019-03/IMG_20190304_101010.jpg', "
                   "parent = 'Photos/2019/2019-03' WHERE path = 'Misc/IMG_20190304_101010.jpg'")
    assert build_consolidate(w.conn, target="gdrive", reorganize_target=True).ops == []


def test_reorganize_target_prefers_the_copy_already_in_the_layout(w):
    w.put("gdrive", "Misc/IMG_20190304_101010.jpg", "A")
    w.put("gdrive", "Photos/2019/2019-03/IMG_20190304_101010.jpg", "A")         # already where the layout wants it
    r = build_consolidate(w.conn, target="gdrive", reorganize_target=True, delete_sources=True)
    assert [(o.op, o.src_path, o.witness_path) for o in r.ops] == [
        ("delete", "Misc/IMG_20190304_101010.jpg", "Photos/2019/2019-03/IMG_20190304_101010.jpg")]
    run(w, r)


def test_reorganize_target_never_overwrites_a_name_taken_by_other_content(w):
    w.put("gdrive", "Misc/IMG_20190304_101010.jpg", "A")
    w.put("gdrive", "Photos/2019/2019-03/IMG_20190304_101010.jpg", "B")
    r = build_consolidate(w.conn, target="gdrive", reorganize_target=True)
    assert [(o.op, o.dst_path) for o in r.ops] == [("move", "Photos/2019/2019-03/IMG_20190304_101010 (2).jpg")]
    run(w, r)


def test_read_only_sources_are_copied_from_but_never_deleted(w):
    w.remote("archive", "dropbox", writable=False)
    w.put("archive", "old/r.pdf", "R")
    w.put("dropbox", "r.pdf", "R")
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    assert [(o.op, w.names()[o.src_remote_id]) for o in r.ops] == [
        ("copy", "dropbox"), ("delete", "dropbox")]
    assert r.skipped == [{"entry_id": r.skipped[0]["entry_id"], "location": "archive:old/r.pdf",
                          "code": "READ_ONLY", "reason": "read-only remote"}]
    run(w, r)


def test_ambiguous_source_paths_are_skipped(w):
    w.put("gdrive", "x/a.pdf", "A", provider_id="1")
    w.put("gdrive", "x/a.pdf", "B", provider_id="2")
    w.put("gdrive", "y/b.pdf", "C")
    w.remote("target", "dropbox")
    r = build_consolidate(w.conn, target="target")
    assert dsts(r) == ["Documents/2024/b.pdf"]
    assert reasons(r) == ["ambiguous path", "ambiguous path"]
    run(w, r)


def test_ambiguous_target_copy_cannot_witness_anything(w):
    w.put("gdrive", "x/a.pdf", "A", provider_id="1")
    w.put("gdrive", "x/a.pdf", "B", provider_id="2")
    w.put("dropbox", "a.pdf", "A")
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    assert [(o.op, o.dst_path or o.witness_path) for o in r.ops] == [
        ("copy", "Documents/2024/a.pdf"), ("delete", "Documents/2024/a.pdf")]
    run(w, r)


def test_path_prefix_limits_sources_but_target_copies_always_count(w):
    w.put("dropbox", "In/a.pdf", "A")
    w.put("dropbox", "Out/b.pdf", "B")
    w.put("dropbox", "In/c.pdf", "C")
    w.put("gdrive", "Elsewhere/c.pdf", "C")                                     # outside the prefix, still "on target"
    r = build_consolidate(w.conn, target="gdrive", path_prefix="In")
    assert dsts(r) == ["Documents/2024/a.pdf"]
    run(w, r)


def test_quarantined_files_are_not_live_copies(w):
    w.put("dropbox", "r.pdf", "R")
    w.put("dropbox", ".drive-merge-quarantine/3/old/z.pdf", "Z")
    w.put("gdrive", ".drive-merge-quarantine/3/old/r.pdf", "R")                 # not a live copy on the target
    r = build_consolidate(w.conn, target="gdrive")
    assert dsts(r) == ["Documents/2024/r.pdf"]
    run(w, r)


def test_consolidate_sources_option_and_prefer_remotes(w):
    w.put("dropbox", "a/x.pdf", "X")
    w.put("onedrive", "b/x (1).pdf", "X")
    w.put("onedrive", "c/y.pdf", "Y")
    r = build_consolidate(w.conn, target="gdrive", sources=["onedrive"])
    assert dsts(r) == ["Documents/2024/x (1).pdf", "Documents/2024/y.pdf"]
    r = build_consolidate(w.conn, target="gdrive", prefer_remotes=["onedrive"])
    assert "Documents/2024/x (1).pdf" in dsts(r)                                # the preferred remote's name is kept
    r = build_consolidate(w.conn, target="gdrive", prefer_remotes=["dropbox"])
    assert "Documents/2024/x.pdf" in dsts(r)


def test_consolidate_layout_follows_the_provider_preferred_keeper(w):
    w.put("icloud", "x.pdf", "X", mtime="2020-02-02T00:00:00Z")                  # the shallow bridged copy
    w.put("dropbox", "deep/er/report.pdf", "X", mtime="2020-02-02T00:00:00Z")
    r = build_consolidate(w.conn, target="gdrive")
    assert dsts(r) == ["Documents/2020/report.pdf"]                              # named after the dropbox copy
    run(w, r)
    r = build_consolidate(w.conn, target="gdrive", prefer_remotes=["icloud"])
    assert dsts(r) == ["Documents/2020/x.pdf"]


def test_consolidate_rejects_bad_arguments(w):
    w.remote("ro", "drive", writable=False)
    with pytest.raises(BuildError, match="read-only"):
        build_consolidate(w.conn, target="ro")
    with pytest.raises(BuildError):
        build_consolidate(w.conn, target="nope")
    with pytest.raises(BuildError):
        build_consolidate(w.conn, target="gdrive", sources=["nope"])
    with pytest.raises(BuildError):
        build_consolidate(w.conn, target="gdrive", prefer_remotes=["nope"])


def test_consolidate_is_deterministic(w):
    source_world(w)
    a = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    b = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    assert [o.canonical() for o in a.ops] == [o.canonical() for o in b.ops]


# =============================================================================
# reorganize
# =============================================================================

def test_reorganize_moves_files_into_the_layout(w):
    w.put("gdrive", "Inbox/IMG_20190304_101010.jpg", "A")
    w.put("gdrive", "Inbox/report.pdf", "R", mtime="2020-05-05T00:00:00Z")
    w.put("gdrive", "Photos/2021/2021-06/IMG_20210607_000000.jpg", "B")         # already in place
    w.put("gdrive", "Scratch/notes.bin", "N")                                   # fallback: Unsorted/gdrive/<path>
    w.put("dropbox", "Inbox/other.pdf", "O")                                    # another remote: untouched
    r = build_reorganize(w.conn, remote="gdrive", ruleset=builders.classify.default_rules())
    assert sorted((o.src_path, o.dst_path) for o in r.ops) == [
        ("Inbox/IMG_20190304_101010.jpg", "Photos/2019/2019-03/IMG_20190304_101010.jpg"),
        ("Inbox/report.pdf", "Documents/2020/report.pdf"),
        ("Scratch/notes.bin", "Unsorted/gdrive/Scratch/notes.bin")]
    assert all(o.op == "move" and o.dst_remote_id == o.src_remote_id == w.rid["gdrive"] for o in r.ops)
    assert r.stats == {"files": 4, "in_place": 1, "moves": 3, "renamed_for_collision": 0, "skipped": 0}
    run(w, r)


def test_reorganize_is_idempotent_once_applied(w):
    w.put("gdrive", "Photos/2019/2019-03/IMG_20190304_101010.jpg", "A")
    w.put("gdrive", "Documents/2020/report.pdf", "R", mtime="2020-05-05T00:00:00Z")
    r = build_reorganize(w.conn, remote="gdrive")
    assert r.ops == [] and r.stats["in_place"] == 2


def test_reorganize_collisions_get_numbered_names(w):
    w.put("gdrive", "a/IMG_20200101_000000.jpg", "X")
    w.put("gdrive", "b/IMG_20200101_000000.jpg", "Y")
    w.put("gdrive", "Photos/2020/2020-01/IMG_20200101_000000.jpg", "Z")         # occupies the target name
    r = build_reorganize(w.conn, remote="gdrive")
    assert sorted(o.dst_path for o in r.ops) == [
        "Photos/2020/2020-01/IMG_20200101_000000 (2).jpg", "Photos/2020/2020-01/IMG_20200101_000000 (3).jpg"]
    assert r.stats["renamed_for_collision"] == 2
    run(w, r)


def test_reorganize_never_moves_onto_identical_content(w):
    w.put("gdrive", "a/IMG_20200101_000000.jpg", "X")
    w.put("gdrive", "Photos/2020/2020-01/IMG_20200101_000000.jpg", "X")         # same bytes already at the target name
    r = build_reorganize(w.conn, remote="gdrive")
    assert [o.dst_path for o in r.ops] == ["Photos/2020/2020-01/IMG_20200101_000000 (2).jpg"]
    run(w, r)


def test_reorganize_does_not_depend_on_other_files_moving_away(w):
    # b/y.pdf wants the name that a/y.pdf holds; a/y.pdf itself moves, but later in the plan
    rules = _rules('[[rule]]\nname="by-dir"\ncategory=["pdf"]\ntarget="out/{parent}/{name}"')
    w.put("gdrive", "out/b/y.pdf", "A")
    w.put("gdrive", "b/y.pdf", "B")
    w.put("gdrive", "out/b/z.pdf", "C")
    r = build_reorganize(w.conn, remote="gdrive", ruleset=rules)
    assert [(o.src_path, o.dst_path) for o in r.ops] == [("b/y.pdf", "out/b/y (2).pdf")]
    run(w, r)


def test_reorganize_case_insensitive_remote(w):
    w.put("dropbox", "a/IMG_20200101_000000.jpg", "X")
    w.put("dropbox", "b/img_20200101_000000.jpg", "Y")
    r = build_reorganize(w.conn, remote="dropbox")
    assert sorted(o.dst_path for o in r.ops) == [
        "Photos/2020/2020-01/IMG_20200101_000000.jpg", "Photos/2020/2020-01/img_20200101_000000 (2).jpg"]
    run(w, r)
    # a case-only difference with the file itself is "in place", not a rename
    w.put("dropbox", "Photos/2021/2021-01/img_20210101_000000.JPG", "Z")
    r = build_reorganize(w.conn, remote="dropbox", path_prefix="Photos")
    assert r.ops == []


def test_reorganize_keeps_units_together(w):
    w.put("gdrive", "Code/repo/.git/HEAD", "H")
    w.put("gdrive", "Code/repo/src/main.py", "M")
    w.put("gdrive", "Code/repo/README.md", "D")
    w.put("gdrive", "Code/repo/docs/guide.md", "D")                             # identical files in a unit both move
    r = build_reorganize(w.conn, remote="gdrive")
    assert sorted(o.dst_path for o in r.ops) == [
        "Projects/repo/.git/HEAD", "Projects/repo/README.md", "Projects/repo/docs/guide.md",
        "Projects/repo/src/main.py"]
    run(w, r)


def test_reorganize_moves_native_docs_and_skips_ambiguous_paths(w):
    w.put("gdrive", "Stuff/Plan", None, native_doc=True, mime=NATIVE_MIME, size=None)
    w.put("gdrive", "x/dup.pdf", "A", provider_id="1")
    w.put("gdrive", "x/dup.pdf", "B", provider_id="2")
    r = build_reorganize(w.conn, remote="gdrive")
    assert [(o.src_path, o.dst_path) for o in r.ops] == [("Stuff/Plan", "Documents/2024/Plan")]
    assert reasons(r) == ["ambiguous path", "ambiguous path"]
    run(w, r)


def test_reorganize_target_root_prefix_and_quarantine(w):
    w.put("gdrive", "In/a.pdf", "A")
    w.put("gdrive", "Out/b.pdf", "B")
    w.put("gdrive", ".drive-merge-quarantine/4/In/c.pdf", "C")
    r = build_reorganize(w.conn, remote="gdrive", target_root="Sorted", path_prefix="In")
    assert [(o.src_path, o.dst_path) for o in r.ops] == [("In/a.pdf", "Sorted/Documents/2024/a.pdf")]
    run(w, r)


def test_reorganize_rejects_bad_arguments(w):
    w.remote("ro", "drive", writable=False)
    with pytest.raises(BuildError, match="read-only"):
        build_reorganize(w.conn, remote="ro")
    with pytest.raises(BuildError):
        build_reorganize(w.conn, remote="nope")


# =============================================================================
# helpers that already existed
# =============================================================================

def test_best_hash_puts_the_weak_types_last(w):
    local = w.remote("local", "local")
    row = lambda **hashes: {t: hashes.get(t) for t in identity.HASH_TYPES}  # noqa: E731
    assert builders.best_hash(row(md5="m", sha1="s", quickxor="q")) == "sha1:s"
    assert builders.best_hash(row(md5="m", dropbox="d")) == "dropbox:d"
    assert builders.best_hash(row(sha1="s", dropbox="d")) == "dropbox:d"          # sha1 is weak too: strong ones first
    assert builders.best_hash(row(md5="m", quickxor="q")) == "md5:m"
    assert builders.best_hash(row(quickxor="q")) == "quickxor:q"
    assert builders.best_hash(row(md5="m", sha1="s", sha256="h", dropbox="d", quickxor="q")) == "sha256:h"
    assert builders.best_hash(row()) is None and local


def test_best_hash_parse_location_and_op_from_entry(w):
    eid = w.put("gdrive", "a/b.txt", "B", sha256="ss")
    row = w.conn.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
    assert builders.best_hash(row) == "sha256:ss"                               # the strongest hash first
    assert builders.parse_location(w.conn, "gdrive:/a/b.txt/") == (w.rid["gdrive"], "a/b.txt")
    with pytest.raises(BuildError):
        builders.parse_location(w.conn, "no-colon")
    with pytest.raises(BuildError):
        builders.parse_location(w.conn, "nope:a")
    op = builders.op_from_entry(row, "move", dst_remote_id=row["remote_id"], dst_path="c.txt")
    assert (op.src_entry_id, op.expect_size, op.expect_hash) == (eid, row["size"], "sha256:ss")


def test_build_manual_expands_folders_and_flags_missing_sources(w):
    w.put("gdrive", "Old/a.txt", "A")
    w.put("gdrive", "Old/sub/b.txt", "B")
    r = build_manual(w.conn, [
        {"op": "move", "src": "gdrive:Old", "dst": "gdrive:New", "reason": "tidy"},
        {"op": "copy", "src": "gdrive:missing.txt", "dst": "dropbox:x.txt"}])
    assert [(o.op, o.src_path, o.dst_path) for o in r.ops] == [
        ("move", "Old/a.txt", "New/a.txt"), ("move", "Old/sub/b.txt", "New/sub/b.txt"),
        ("copy", "missing.txt", "x.txt")]
    assert not simulate_ops(w.conn, r.ops).ok                                   # a missing source is reported
    with pytest.raises(BuildError):
        build_manual(w.conn, [{"op": "rename", "src": "gdrive:a"}])


# =============================================================================
# repeated runs, JSON-style params, units on the target
# =============================================================================

def test_path_keeping_fallback_does_not_nest_on_a_second_run(w):
    w.put("gdrive", "Scratch/notes.bin", "N")
    first = build_reorganize(w.conn, remote="gdrive")
    assert [o.dst_path for o in first.ops] == ["Unsorted/gdrive/Scratch/notes.bin"]
    w.conn.execute("UPDATE entries SET path = ?, parent = ? WHERE name = 'notes.bin'",
                   ("Unsorted/gdrive/Scratch/notes.bin", "Unsorted/gdrive/Scratch"))
    assert build_reorganize(w.conn, remote="gdrive").ops == []
    rooted = build_reorganize(w.conn, remote="gdrive", target_root="Sorted")      # moves in under the root, once
    assert [o.dst_path for o in rooted.ops] == ["Sorted/Unsorted/gdrive/Scratch/notes.bin"]
    w.conn.execute("UPDATE entries SET path = ?, parent = ? WHERE name = 'notes.bin'",
                   ("Sorted/Unsorted/gdrive/Scratch/notes.bin", "Sorted/Unsorted/gdrive/Scratch"))
    assert build_reorganize(w.conn, remote="gdrive", target_root="Sorted").ops == []
    assert build_consolidate(w.conn, target="gdrive", target_root="Sorted", reorganize_target=True).ops == []


def test_files_below_the_root_are_laid_out_relative_to_it(w):
    w.put("gdrive", "Archive/Unsorted/gdrive/Scratch/placed.bin", "P")        # an earlier run's result
    w.put("gdrive", "Archive/Mine/own.bin", "O")                              # the user's own folder in the root
    w.put("gdrive", "Elsewhere/x.bin", "X")
    r = build_reorganize(w.conn, remote="gdrive", target_root="Archive")
    assert sorted((o.src_path, o.dst_path) for o in r.ops) == [
        ("Archive/Mine/own.bin", "Archive/Unsorted/gdrive/Mine/own.bin"),
        ("Elsewhere/x.bin", "Archive/Unsorted/gdrive/Elsewhere/x.bin")]
    assert r.stats["in_place"] == 1
    run(w, r)


def test_consolidated_copy_on_the_target_is_not_moved_by_a_second_run(w):
    w.put("dropbox", "Misc/w.bin", "W")
    first = build_consolidate(w.conn, target="gdrive", target_root="Archive", reorganize_target=True)
    assert dsts(first) == ["Archive/Unsorted/dropbox/Misc/w.bin"]               # the layout of the dropbox file
    w.apply(first.ops)
    again = build_consolidate(w.conn, target="gdrive", target_root="Archive", reorganize_target=True)
    assert again.ops == []                                                       # not Unsorted/gdrive/Unsorted/dropbox/...
    assert build_consolidate(w.conn, target="gdrive", target_root="Archive", reorganize_target=True,
                             delete_sources=True).ops[0].op == "delete"          # only the source copy goes


def test_a_source_folder_named_like_the_root_is_just_a_folder(w):
    w.put("dropbox", "Archive/Old/a.bin", "A")
    r = build_consolidate(w.conn, target="gdrive", target_root="Archive")
    assert dsts(r) == ["Archive/Unsorted/dropbox/Archive/Old/a.bin"]
    run(w, r)


def test_reorganize_target_moves_a_whole_unit_on_the_target(w):
    w.put("gdrive", "Code/repo/.git/HEAD", "H")
    w.put("gdrive", "Code/repo/a/__init__.py", "E")
    w.put("gdrive", "Code/repo/b/__init__.py", "E")
    r = build_consolidate(w.conn, target="gdrive", reorganize_target=True)
    assert sorted((o.op, o.dst_path) for o in r.ops) == [
        ("move", "Projects/repo/.git/HEAD"), ("move", "Projects/repo/a/__init__.py"),
        ("move", "Projects/repo/b/__init__.py")]
    run(w, r)


def test_delete_sources_keeps_repeated_files_inside_target_units_but_drops_loose_extras(w):
    w.put("gdrive", "Projects/repo/.git/HEAD", "H")
    w.put("gdrive", "Projects/repo/a/__init__.py", "E")
    w.put("gdrive", "Projects/repo/b/__init__.py", "E")
    assert build_consolidate(w.conn, target="gdrive", delete_sources=True).ops == []
    w.put("gdrive", "Stray/one.pdf", "S")
    w.put("gdrive", "Stray/two.pdf", "S")
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    assert [(o.op, o.src_path, o.witness_path) for o in r.ops] == [("delete", "Stray/two.pdf", "Stray/one.pdf")]
    run(w, r)


def test_json_style_params_are_tolerated(w):
    w.put("dropbox", "a.pdf", "A")
    w.put("dropbox", "b/a.pdf", "A")
    d = build_dedupe(w.conn, remote=None, min_size=None, prefer_remotes=None, path_prefix=None, limit_groups=None)
    assert len(d.ops) == 1
    c = build_consolidate(w.conn, target="gdrive", target_root=None, sources="dropbox", prefer_remotes=None,
                          path_prefix=None)
    assert dsts(c) == ["Documents/2024/a.pdf"]
    assert build_reorganize(w.conn, remote="dropbox", target_root=None).ops


# =============================================================================
# randomised: whatever the catalog looks like, the builders' plans simulate clean
# =============================================================================

_DIRS = ["", "a", "b/c", "Photos", "Docs", "Code/repo", "Code/repo/pkg", "Downloads", "Misc/old"]
_NAMES = ["x.jpg", "IMG_20200101_000000.jpg", "img_20200101_000000.JPG", "r.pdf", "r (1).pdf", "n.txt",
          "main.py", "__init__.py", "data.bin", "副本.docx"]
_REPO_ONLY = {"Code/repo": ["README.md", "main.py"], "Code/repo/pkg": ["__init__.py"]}


def _mtime(rnd) -> str:
    return f"20{rnd.randint(18, 23)}-0{rnd.randint(1, 9)}-15T00:00:00Z"


def random_world(w: World, rnd, *, bridged: bool = False) -> None:
    """A random catalog. With `bridged` the iCloud files are proven copies (hashed) instead of unhashed, every
    copy of a content has the same mtime (as it would after a copy: a layout dated by mtime must not depend on
    which copy it was read from), and gdrive, the consolidate target of the tests, holds each content once."""
    w.remote("ro", "dropbox", writable=False)
    remotes = list(w.rid)
    sizes = {f"C{i}": 20 + 7 * i for i in range(8)}
    used: set[tuple[str, str]] = set()
    on_gdrive: set[str] = set()

    def stamp(label: str) -> str:
        return f"20{10 + int(label[1:])}-0{1 + int(label[1:])}-15T00:00:00Z" if bridged else _mtime(rnd)

    if rnd.random() < 0.7:
        w.put("dropbox", "Code/repo/.git/HEAD", "C0", size=sizes["C0"], **({"mtime": stamp("C0")} if bridged else {}))
        used.add(("dropbox", "code/repo/.git/head"))
    for _ in range(rnd.randint(8, 40)):
        remote = rnd.choice(remotes)
        folder = rnd.choice(_DIRS)
        name = rnd.choice(_REPO_ONLY.get(folder, _NAMES))
        path = f"{folder}/{name}".lstrip("/")
        folded = (remote, path.casefold())
        if folded in used:
            continue
        used.add(folded)
        label = rnd.choice(list(sizes))
        if bridged and remote == "gdrive":
            if label in on_gdrive:
                continue
            on_gdrive.add(label)
        if remote == "icloud":
            w.put(remote, path, label if bridged else None, size=sizes[label], mtime=stamp(label))
        elif rnd.random() < 0.08 and remote == "gdrive":
            w.put(remote, path, label, size=sizes[label], provider_id=f"id{len(used)}")
            w.put(remote, path, rnd.choice(list(sizes)), size=999, provider_id=f"idb{len(used)}")  # an ambiguous path
        else:
            w.put(remote, path, label, size=sizes[label], mtime=stamp(label))


def assert_units_arrived(w, after, skipped, target, root) -> None:
    """Every source file inside a unit sits at its own layout position on the target (or its '(n)' variant)."""
    import re
    names = w.names()
    sources = [rid for rid, name in names.items() if name != target]
    wanted = [p for p in builders.classify.plan_layout(w.conn, builders.classify.default_rules(), remote_ids=sources,
                                                       target_root=root) if p.unit is not None]
    for p in wanted:
        remote = names[p.remote_id]
        if f"{remote}:{p.src_path}" in skipped:
            continue
        key = w.live()[(remote, p.src_path)]
        stem, ext = p.target_path.rsplit(".", 1) if "." in p.target_path.rsplit("/", 1)[-1] else (p.target_path, "")
        variant = re.compile(re.escape(stem) + r"( \(\d+\))?" + (r"\." + re.escape(ext) if ext else "") + "$")
        assert any(r == target and k == key and variant.match(path) for (r, path), k in after.items()), (
            f"{remote}:{p.src_path} is in unit {p.unit} but {p.target_path} is missing on {target}")


@pytest.mark.parametrize("seed", range(60))
def test_random_catalogs_produce_clean_plans(w, seed):
    import random
    rnd = random.Random(seed)
    random_world(w, rnd)
    before = w.live()
    survivors = set(before.values())
    for remote in ("gdrive", "dropbox", "onedrive", "icloud"):
        for cross in (False, True):
            r = build_dedupe(w.conn, remote=None if cross else remote, cross_remote=cross,
                             prefer_remotes=rnd.sample(["gdrive", "dropbox", "onedrive"], 2))
            run(w, r)
            assert survivors <= set(w.after(r.ops).values()), "dedupe lost a content"
        r = build_reorganize(w.conn, remote=remote)
        run(w, r)
        assert survivors <= set(w.after(r.ops).values())
    for target in ("gdrive", "dropbox"):
        flags = dict(delete_sources=rnd.random() < 0.6, reorganize_target=rnd.random() < 0.5,
                     target_root=rnd.choice(["", "Archive", "Archive/2024"]))
        r = build_consolidate(w.conn, target=target, **flags)
        run(w, r)
        after = w.after(r.ops)
        skipped = {s["location"] for s in r.skipped}
        on_target = {k for (rem, _), k in after.items() if rem == target}
        for (rem, path), key in before.items():
            if rem != target and f"{rem}:{path}" not in skipped:
                assert key in on_target, f"{rem}:{path} never reached {target}: {r.stats}"
            if flags["delete_sources"] and rem not in (target, "ro") and f"{rem}:{path}" not in skipped:
                assert (rem, path) not in after, f"{rem}:{path} survived delete_sources"
        assert survivors <= set(after.values())
        assert_units_arrived(w, after, skipped, target, flags["target_root"])


# =============================================================================
# idempotence: a plan, once applied, leaves nothing to do
# =============================================================================

_RULESETS = {
    "default": None,
    "keep-path": '',
    "dir-keeping": '[layout]\nfallback = "Misc/{parent}/{dir}/{name}"\n'
                   '[[rule]]\nname = "p"\ncategory = ["photo"]\ntarget = "Pics/{year}/{path}"\n'
                   '[[rule]]\nname = "u"\nunit = true\ntarget = "Units/{dir}/{unit}/{relpath}"\n',
    "remote-first": '[layout]\nfallback = "{remote}/{category}/{relpath}"\n',
}


def _ruleset(name):
    text = _RULESETS[name]
    return None if text is None else _rules(text)


def put_some_under_root(w, rnd, root: str) -> None:
    """Move a share of the files below the root, as if an earlier (partial) run or the user had put them there."""
    if not root:
        return
    chosen = {(rid, path) for rid, path, in w.conn.execute(
        "SELECT remote_id, path FROM entries WHERE is_dir = 0 AND present = 1") if rnd.random() < 0.4}
    for rid, path in sorted(chosen):
        new = f"{root}/{path}"
        w.conn.execute("UPDATE entries SET path = ?, parent = ?, entry_key = CASE WHEN entry_key LIKE 'p:%' "
                       "THEN ? ELSE entry_key END WHERE remote_id = ? AND path = ? AND is_dir = 0",
                       (new, posixpath.dirname(new), f"p:{new}", rid, path))
    w.apply([])


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("root", ["", "Sorted", "Archive/2024"])
@pytest.mark.parametrize("rules", list(_RULESETS))
def test_applied_reorganize_leaves_nothing_to_do(w, seed, root, rules):
    """Property: apply a reorganize plan's moves to the catalog, build again, and there are no ops."""
    import random
    rnd = random.Random(f"{seed}/{root}/{rules}")
    random_world(w, rnd)
    put_some_under_root(w, rnd, root)
    ruleset = _ruleset(rules)
    writable = [name for name in w.rid if name != "ro"]
    for remote in writable:
        r = build_reorganize(w.conn, remote=remote, ruleset=ruleset, target_root=root)
        run(w, r)
        w.apply(r.ops)
    for remote in writable:
        again = build_reorganize(w.conn, remote=remote, ruleset=ruleset, target_root=root)
        assert [(o.src_path, o.dst_path) for o in again.ops] == [], f"{remote} {rules} root={root!r} seed={seed}"


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("root", ["", "Archive"])
@pytest.mark.parametrize("flags", [
    dict(), dict(reorganize_target=True), dict(delete_sources=True), dict(reorganize_target=True, delete_sources=True)],
    ids=["copy", "reorganize-target", "delete-sources", "reorganize-and-delete"])
def test_applied_consolidate_leaves_nothing_to_do(w, seed, root, flags):
    """Property: run a consolidate, apply its moves, copies and deletes, build again: no ops (proven content only).

    The target holds one copy of each content (see random_world): which of several copies is the keeper depends
    on how deep each lies, so moving one into the layout can hand the role to another. Dedupe the target first.
    """
    import random
    rnd = random.Random(f"{seed}/{root}/{sorted(flags)}")
    random_world(w, rnd, bridged=True)
    put_some_under_root(w, rnd, root if flags.get("reorganize_target") else "")
    r = build_consolidate(w.conn, target="gdrive", target_root=root, **flags)
    run(w, r)
    w.apply(r.ops)
    again = build_consolidate(w.conn, target="gdrive", target_root=root, **flags)
    assert [(o.op, o.src_path, o.dst_path or o.witness_path) for o in again.ops] == [], f"seed={seed} {flags}"


# =============================================================================
# Plans view: keep / skip_groups overrides, coded BuildError, new stats and skip codes
# =============================================================================

def _same(a, b):
    return [(o.op, o.src_remote_id, o.src_path, o.dst_path, o.witness_path, o.reason) for o in a.ops] == \
           [(o.op, o.src_remote_id, o.src_path, o.dst_path, o.witness_path, o.reason) for o in b.ops]


def _group_key(w, label):
    return f"g:{w.groups[label]}"


def test_build_error_defaults_and_carries_code_and_params():
    e = BuildError("boom")
    assert (e.code, e.params, str(e)) == ("BUILD_ERROR", {}, "boom")
    e = BuildError("x", "READ_ONLY", remote="a")
    assert (e.code, e.params) == ("READ_ONLY", {"remote": "a"})
    assert isinstance(e, ValueError)


def test_every_raise_site_is_coded(w, catalog):
    def err(fn, *a, **kw):
        with pytest.raises(BuildError) as ei:
            fn(*a, **kw)
        return ei.value.code, ei.value.params

    ro = w.remote("ro", "drive", writable=False)
    assert err(build_manual, w.conn, [{"op": "copy", "src": "nowhere"}]) == ("BAD_LOCATION", {"location": "nowhere"})
    assert err(build_manual, w.conn, [{"op": "copy", "src": "zzz:a", "dst": "gdrive:b"}]) == (
        "UNKNOWN_REMOTE", {"remote": "zzz"})
    assert err(build_manual, w.conn, [{"op": "nope", "src": "gdrive:a"}]) == ("BAD_OP", {"index": 1})
    assert err(build_manual, w.conn, [{"op": "move"}]) == ("MISSING_SRC", {"index": 1})
    assert err(build_manual, w.conn, [{"op": "copy", "src": "gdrive:a"}]) == ("MISSING_DST", {"index": 1, "op": "copy"})
    assert err(build_dedupe, w.conn, remote="zzz") == ("UNKNOWN_REMOTE", {"remote": "zzz"})
    assert err(build_consolidate, w.conn, target="ro") == ("READ_ONLY", {"remote": "ro"})
    assert err(build_consolidate, w.conn, target="gdrive", sources=["zzz"]) == ("UNKNOWN_REMOTE", {"remote": "zzz"})
    assert err(build_reorganize, w.conn, remote="ro") == ("READ_ONLY", {"remote": "ro"})
    assert err(build_dedupe, w.conn, keep=[1]) == ("BAD_PARAM", {"param": "keep"})
    assert err(build_dedupe, w.conn, skip_groups="g:1") == ("BAD_PARAM", {"param": "skip_groups"})
    assert err(build_dedupe, w.conn, keep={"g:1": "x"})[0] == "BAD_KEEPER"
    assert err(build_dedupe, w.conn, skip_groups=["g:999"]) == ("BAD_GROUP", {"group": "g:999"})
    assert err(build_dedupe, w.conn, keep={"g:999": 1}) == ("BAD_GROUP", {"group": "g:999"})
    assert ro


def _three(w):
    a = w.put("gdrive", "Docs/report.pdf", "R")
    b = w.put("gdrive", "Docs/Copy of report.pdf", "R")
    c = w.put("gdrive", "Archive/old/report.pdf", "R")
    return a, b, c


def test_dedupe_without_overrides_is_unchanged(w):
    _three(w)
    w.put("dropbox", "r.pdf", "R")
    base = build_dedupe(w.conn)
    assert _same(base, build_dedupe(w.conn, keep={}, skip_groups=()))
    assert _same(base, build_dedupe(w.conn, keep=None, skip_groups=[]))
    assert _same(base, build_dedupe(w.conn, group_window=(0, 10)))


def test_keep_override_changes_the_keeper_of_that_group_only(w):
    a, b, c = _three(w)
    w.put("gdrive", "x/u.txt", "U")
    w.put("gdrive", "x/v.txt", "U")
    key = _group_key(w, "R")
    r = build_dedupe(w.conn, remote="gdrive", keep={key: c})
    assert sorted(o.src_path for o in r.ops if o.src_entry_id != c) == sorted(
        ["Docs/report.pdf", "Docs/Copy of report.pdf"] + ["x/v.txt"])
    assert all(o.witness_path == "Archive/old/report.pdf" for o in r.ops if "report" in o.src_path)
    assert any(o.src_path == "x/v.txt" for o in r.ops)                           # the other group follows the policy
    run(w, r)


def test_keep_applies_to_its_own_cloud_pool_in_per_cloud_mode(w):
    g1 = w.put("gdrive", "a/r.pdf", "R")
    g2 = w.put("gdrive", "b/r (1).pdf", "R")
    d1 = w.put("dropbox", "a/r.pdf", "R")
    d2 = w.put("dropbox", "b/r (1).pdf", "R")
    r = build_dedupe(w.conn, keep={_group_key(w, "R"): g2})
    got = {(w.names()[o.src_remote_id], o.src_path) for o in r.ops}
    assert got == {("gdrive", "a/r.pdf"), ("dropbox", "b/r (1).pdf")}           # gdrive overridden, dropbox by policy
    assert (g1, d1, d2) and run(w, r)


def test_keep_across_clouds_may_pick_any_cloud(w):
    w.put("gdrive", "r.pdf", "R")
    d = w.put("dropbox", "r.pdf", "R")
    r = build_dedupe(w.conn, cross_remote=True, keep={_group_key(w, "R"): d})
    assert [(w.names()[o.src_remote_id], o.src_path) for o in r.ops] == [("gdrive", "r.pdf")]
    assert r.ops[0].witness_remote_id == w.rid["dropbox"]


def test_bad_keeper_cases(w):
    w.put("gdrive", "a/r.pdf", "R")
    w.put("gdrive", "b/r (1).pdf", "R")
    d = w.put("dropbox", "r.pdf", "R")
    q = w.put("gdrive", ".drive-merge-quarantine/r.pdf", "R")
    nd = w.put("gdrive", "native.gdoc", "R", native_doc=True)
    w.put("gdrive", "amb.pdf", "R", provider_id="i1")
    amb = w.put("gdrive", "amb.pdf", "Z", provider_id="i2")
    other = w.put("gdrive", "o/other.pdf", "O1")
    w.put("gdrive", "o/other2.pdf", "O1")
    key = _group_key(w, "R")

    def bad(**kw):
        with pytest.raises(BuildError) as ei:
            build_dedupe(w.conn, **kw)
        assert ei.value.code == "BAD_KEEPER"
        return ei.value.params

    assert bad(remote="gdrive", keep={key: d}) == {"group": key, "entry_id": d}          # other cloud, per-cloud mode
    assert bad(keep={key: q}) == {"group": key, "entry_id": q}                            # quarantined
    assert bad(keep={key: nd}) == {"group": key, "entry_id": nd}                          # native doc
    assert bad(keep={key: other}) == {"group": key, "entry_id": other}                    # copy of another group
    assert bad(keep={key: 10 ** 6}) == {"group": key, "entry_id": 10 ** 6}                # not an entry at all
    assert amb
    # an ambiguous path: both Drive files at amb.pdf are in different groups; make one share the group
    w.put("gdrive", "amb.pdf", "R", provider_id="i3")
    amb_r = w.conn.execute("SELECT id FROM entries WHERE provider_id = 'i3'").fetchone()[0]
    assert bad(keep={key: amb_r}) == {"group": key, "entry_id": amb_r}
    # cross-cloud mode accepts the dropbox copy
    assert build_dedupe(w.conn, cross_remote=True, keep={key: d}).ops


def test_skip_groups_produce_no_ops_and_no_skips(w):
    _three(w)
    w.put("gdrive", "x/u.txt", "U")
    w.put("gdrive", "x/v.txt", "U")
    r = build_dedupe(w.conn, skip_groups=[_group_key(w, "R")])
    assert [o.src_path for o in r.ops] == ["x/v.txt"]
    assert r.stats["groups"] == 1 and r.stats["groups_examined"] == 2
    assert build_dedupe(w.conn, skip_groups=[_group_key(w, "R"), _group_key(w, "U")]).ops == []


def test_group_window_describes_groups_in_build_order(w):
    a, b, c = _three(w)
    w.put("gdrive", "x/u.txt", "U")
    w.put("gdrive", "x/v.txt", "U")
    w.put("gdrive", "custom-q/u.txt", "U")        # quarantined for this build only (the catalog's own dir differs)
    r = build_dedupe(w.conn, group_window=(0, 10), skip_groups=[_group_key(w, "U")], quarantine_dir="custom-q")
    assert [g["key"] for g in r.groups] == [_group_key(w, "R"), _group_key(w, "U")]   # biggest reclaimable first
    big, small = r.groups
    assert (big["skipped"], big["reclaim"]) == (False, 2 * (10 + 1))
    kept = [x for x in big["copies"] if x["keep"]]
    assert [x["path"] for x in kept] == ["Docs/report.pdf"]
    assert {x["entry_id"] for x in big["copies"] if x["remove"]} == {b, c}
    assert all(x["usable"] and x["why_not"] is None for x in big["copies"])
    assert small["skipped"] and small["reclaim"] == 0
    assert not any(x["keep"] or x["remove"] for x in small["copies"])
    why = {x["path"]: x["why_not"] for x in small["copies"]}
    assert why["custom-q/u.txt"] == "QUARANTINE" and why["x/u.txt"] is None
    assert build_dedupe(w.conn, group_window=(1, 1)).groups[0]["key"] == _group_key(w, "U")
    assert build_dedupe(w.conn, group_window=(5, 5)).groups == []


def test_dedupe_stats_report_copies_considered_and_skip_codes(w):
    w.put("gdrive", "a.txt", "X", provider_id="id1")
    w.put("gdrive", "a.txt", "Y", provider_id="id2")
    w.put("gdrive", "b/a.txt", "X")
    w.put("gdrive", "deep/er/a.txt", "X")
    w.put("gdrive", "custom-q/a.txt", "X")
    r = build_dedupe(w.conn, quarantine_dir="custom-q")
    assert r.stats["copies_considered"] == 3                                    # quarantine excluded
    assert r.stats["skipped_by_reason"] == {"AMBIGUOUS_PATH": 1}
    assert r.skipped[0]["code"] == "AMBIGUOUS_PATH" and r.skipped[0]["reason"] == "ambiguous path"


def test_every_skip_has_a_known_code(w):
    w.put("gdrive", "keep/r.pdf", "R")
    w.put("icloud", "Docs/r.pdf", None, size=10 + len("R"))                       # probable only
    w.put("gdrive", "n.gdoc", "N", native_doc=True)
    w.put("dropbox", "nosize", "Q", size=None)
    w.put("dropbox", "amb", "A", provider_id="p1")
    w.put("dropbox", "amb", "B", provider_id="p2")
    w.remote("archive", "local", writable=False)
    w.put("archive", "ro/r.pdf", "R")
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    codes = {s["code"] for s in r.skipped}
    assert codes <= set(builders.SKIP_CODES) and all(set(s) <= {"entry_id", "location", "code", "reason", "dest"}
                                                     for s in r.skipped)
    assert set(builders.NOT_KEEPABLE_CODES) <= set(builders.SKIP_CODES)
    assert {"PROBABLE_ONLY", "AMBIGUOUS_PATH", "READ_ONLY"} <= codes
    assert r.stats["skipped_by_reason"] == {c: sum(s["code"] == c for s in r.skipped) for c in codes}
    assert sum(r.stats["skipped_by_reason"].values()) == r.stats["skipped"]


def test_unplaceable_destinations_carry_layout_codes(w):
    w.put("gdrive", "Documents", "F")                                           # a FILE where a layout folder must go
    w.put("dropbox", "r.pdf", "R")
    r = build_consolidate(w.conn, target="gdrive")
    layout = [s for s in r.skipped if s["code"].startswith("LAYOUT_")]
    assert layout and layout[0]["code"] == "LAYOUT_BLOCKED" and layout[0]["dest"].startswith("gdrive:")


def test_consolidate_stats_for_the_estimate(w):
    w.put("gdrive", "Documents/2020/there.pdf", "T")
    w.put("dropbox", "Docs/there.pdf", "T")
    a = w.put("dropbox", "Docs/new.pdf", "N")
    w.put("onedrive", "Docs/other.pdf", "O")
    w.put("dropbox", "n.gdoc", "G", native_doc=True)
    r = build_consolidate(w.conn, target="gdrive")
    assert r.stats["already_on_target"] == 1 and r.stats["to_copy"] == 2
    assert r.stats["bytes_to_copy"] == r.stats["bytes_cross_remote"] == sum(o.expect_size for o in r.ops)
    assert r.stats["moves_on_target"] == 0 and r.stats["deletes"] == 0
    assert r.stats["skipped_by_reason"] == {"NATIVE_DOC": 1}
    assert a
    r = build_consolidate(w.conn, target="gdrive", delete_sources=True)
    assert r.stats["deletes"] == len(kinds(r, "delete")) > 0
    r = build_consolidate(w.conn, target="gdrive", reorganize_target=True, target_root="Z")
    assert r.stats["moves_on_target"] == r.stats["moves"] == len(kinds(r, "move")) > 0
