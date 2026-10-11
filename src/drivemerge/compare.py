"""Compare two folders of the catalog: which files are the same, which differ, which moved, which exist on one side.

Read-only: nothing here touches a cloud or writes the catalog. The rule of evidence is the project's own:
two files are *identical* only when they share a content group (strong-hash proof), *different* only when a
known size or a hash of the same type disagrees, and otherwise *not proven* either way (docs/EXPLORE.md §A, §B).
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any, Iterable

from . import db, dirstats
from .search import HASH_KEYS

STATUSES = ("same", "unverified", "differs", "moved", "left_only", "right_only")
_CHUNK = 500        # SQL variables per IN (...) list
_EXAMPLES = 3

_COLS = ("e.id, e.path, e.name, e.size, e.mtime, e.category, e.content_group, "
         "e.md5, e.sha1, e.sha256, e.dropbox, e.quickxor")


class CompareError(ValueError):
    """A request the read-only views refuse.

    `code` is machine-readable (NOT_FOUND, QUARANTINE, NOT_A_FOLDER, COMPARE_NESTED, COMPARE_TOO_LARGE,
    BAD_QUERY) and `params` hold the facts for the page to word; str(e) is the English sentence.
    """

    def __init__(self, code: str, message: str, **params: Any):
        super().__init__(message)
        self.code = code
        self.params = params

    @property
    def status(self) -> int:
        """The HTTP status this refusal maps to."""
        return 404 if self.code == "NOT_FOUND" else 400 if self.code == "BAD_QUERY" else 409


class Remote:
    """One remote as the views need it, in configuration order."""
    __slots__ = ("id", "name", "provider", "writable", "ci")

    def __init__(self, id: int, name: str, provider: str, writable: bool, ci: bool):
        self.id, self.name, self.provider, self.writable, self.ci = id, name, provider, writable, ci


def load_remotes(conn: sqlite3.Connection) -> list[Remote]:
    """Every remote in configuration order (`remotes.id`)."""
    return [Remote(r[0], r[1], r[2], r[3] is not None, bool(r[4])) for r in conn.execute(
        "SELECT id, name, provider, write_remote, case_insensitive FROM remotes ORDER BY id")]


def remote_named(remotes: Iterable[Remote], name: str) -> Remote:
    """The remote called `name`, else NOT_FOUND."""
    for r in remotes:
        if r.name == name:
            return r
    raise CompareError("NOT_FOUND", f"unknown remote {name!r}", remote=str(name))


def chunks(items: list, size: int = _CHUNK) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --------------------------------------------------------------------------- evidence

def proven_identical(a: dict, b: dict) -> bool:
    """Both files carry the same non-null content group."""
    return a["content_group"] is not None and a["content_group"] == b["content_group"]


def difference(a: dict, b: dict) -> dict | None:
    """Evidence that two files differ, or None when nothing proves it.

    Unequal known sizes first, then the first hash type (in `HASH_KEYS` order) present on both and unequal.
    """
    if a["size"] is not None and b["size"] is not None and a["size"] != b["size"]:
        return {"kind": "size"}
    for key in HASH_KEYS:
        if a["hashes"].get(key) is not None and b["hashes"].get(key) is not None \
                and a["hashes"][key] != b["hashes"][key]:
            return {"kind": "hash", "hash": key}
    return None


# --------------------------------------------------------------------------- helpers

def _norm(path: str) -> str:
    return path.strip("/")


def _under(path: str, root: str) -> bool:
    """`path` is `root` or inside it ('' is every path)."""
    return not root or path == root or path.startswith(root + "/")


def _folded(path: str, ci: bool) -> str:
    return unicodedata.normalize("NFC", path).casefold() if ci else path


def _natural(text: str) -> tuple:
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", text.casefold()) if p)


def _file(row: sqlite3.Row) -> dict:
    return {
        "entry_id": row["id"], "path": row["path"], "name": row["name"], "size": row["size"],
        "mtime": row["mtime"], "category": row["category"], "content_group": row["content_group"],
        "hashes": {k: row[k] for k in HASH_KEYS if row[k] is not None},
    }


def _folder_exists(conn: sqlite3.Connection, remote_id: int, path: str) -> str | None:
    """The catalog's spelling of a live folder at `path` (NFC or NFD), or None."""
    if not path:
        return ""
    forms = list(dict.fromkeys([path, unicodedata.normalize("NFC", path), unicodedata.normalize("NFD", path)]))
    row = conn.execute(
        f"SELECT path FROM entries WHERE remote_id = ? AND is_dir = 1 AND present = 1 "
        f"AND path IN ({','.join('?' * len(forms))}) LIMIT 1", (remote_id, *forms)).fetchone()
    return row[0] if row else None


def _live_files(conn: sqlite3.Connection, remote_id: int, root: str) -> list[sqlite3.Row]:
    """Live files below `root`, by entry id: one indexed range query."""
    cond = db.outside_quarantine(conn, "e.")
    sql = f"SELECT {_COLS} FROM entries e WHERE e.remote_id = ? AND e.present = 1 AND e.is_dir = 0 AND {cond}"
    args: list[Any] = [remote_id]
    if root:
        sql += " AND e.path >= ? AND e.path < ?"
        args += [root + "/", root + "0"]            # '0' is the character after '/'
    cur = conn.execute(sql + " ORDER BY e.id", args)
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


def _count_live(conn: sqlite3.Connection, remote_id: int, root: str) -> int:
    cond = db.outside_quarantine(conn, "e.")
    sql = f"SELECT COUNT(*) FROM entries e WHERE e.remote_id = ? AND e.present = 1 AND e.is_dir = 0 AND {cond}"
    args: list[Any] = [remote_id]
    if root:
        sql += " AND e.path >= ? AND e.path < ?"
        args += [root + "/", root + "0"]
    return conn.execute(sql, args).fetchone()[0]


def _place(remote: Remote, path: str) -> dict:
    return {"remote": remote.name, "path": path}


# --------------------------------------------------------------------------- compare

def compare_folders(conn: sqlite3.Connection, left: tuple[str, str], right: tuple[str, str], *,
                    max_files: int = 20000) -> dict:
    """Align the live files of two folders and classify every file (docs/EXPLORE.md §B).

    `left` and `right` are `(remote_name, path)`. Refuses rather than truncates: unknown remote
    (NOT_FOUND), QUARANTINE, NOT_A_FOLDER, COMPARE_NESTED, COMPARE_TOO_LARGE, in that order.
    """
    remotes = load_remotes(conn)
    sides = [(remote_named(remotes, name), _norm(path)) for name, path in (left, right)]
    qdir = db.quarantine_dir(conn).strip("/")
    for rem, path in sides:
        if qdir and _under(_folded(path, rem.ci), _folded(qdir, rem.ci)):
            raise CompareError("QUARANTINE", f"{rem.name}:{path} is in the quarantine folder, which holds deleted "
                                             "files and is not compared", remote=rem.name, path=path)
    roots: list[str] = []
    for rem, path in sides:
        actual = _folder_exists(conn, rem.id, path)
        if actual is None:
            raise CompareError("NOT_A_FOLDER", f"{rem.name}:{path} is not a folder in the catalog",
                               remote=rem.name, path=path)
        roots.append(actual)
    (lrem, _), (rrem, _) = sides
    lroot, rroot = roots
    if lrem.id == rrem.id:
        lf, rf = _folded(lroot, lrem.ci), _folded(rroot, lrem.ci)
        if _under(lf, rf) or _under(rf, lf):
            raise CompareError("COMPARE_NESTED", "one folder is the other or lies inside it",
                               left=_place(lrem, lroot), right=_place(rrem, rroot))
    for side, rem, root in (("left", lrem, lroot), ("right", rrem, rroot)):
        stats = dirstats.get(conn, rem.id, root)
        # dir_stats can count quarantined files and lag behind a scan: it only lets a clearly small folder pass
        # unchecked and a clearly large one be confirmed by an exact count before anything is loaded.
        if stats is not None and stats["files"] > max_files:
            n = _count_live(conn, rem.id, root)
            if n > max_files:
                raise CompareError("COMPARE_TOO_LARGE", f"{rem.name}:{root} has {n} files, more than the limit "
                                                        f"of {max_files}", side=side, files=n, limit=max_files)

    ci = lrem.ci or rrem.ci
    lrows = _live_files(conn, lrem.id, lroot)
    rrows = _live_files(conn, rrem.id, rroot)
    for side, rows in (("left", lrows), ("right", rrows)):
        if len(rows) > max_files:
            raise CompareError("COMPARE_TOO_LARGE", f"{side} folder has {len(rows)} files, more than the limit "
                                                    f"of {max_files}", side=side, files=len(rows), limit=max_files)

    def rel_of(path: str, root: str) -> str:
        return path[len(root) + 1:] if root else path

    def key_of(rel: str) -> str:
        """Match key: NFC, and casefolded when either remote ignores case."""
        nfc = unicodedata.normalize("NFC", rel)
        return nfc.casefold() if ci else nfc

    lfiles = [(rel_of(r["path"], lroot), _file(r)) for r in lrows]
    rfiles = [(rel_of(r["path"], rroot), _file(r)) for r in rrows]

    # ---- path pass
    lby: dict[str, list[tuple[str, dict]]] = {}
    rby: dict[str, list[tuple[str, dict]]] = {}
    for rel, f in lfiles:
        lby.setdefault(key_of(rel), []).append((rel, f))
    for rel, f in rfiles:
        rby.setdefault(key_of(rel), []).append((rel, f))

    rows_out: list[dict] = []
    left_rest: list[tuple[str, dict]] = []
    right_rest: list[tuple[str, dict]] = []

    def pair_row(status: str, lrel: str, lf: dict, rrel: str, rf: dict, evidence: dict) -> dict:
        return {"rel": lrel, "status": status, "rel_right": rrel if status == "moved" else None,
                "left": lf, "right": rf, "evidence": evidence, "elsewhere": None, "unique": None}

    for k, ls in lby.items():
        rs = list(rby.pop(k, []))
        if not rs:
            left_rest += ls
            continue
        remaining_r = list(range(len(rs)))
        unpaired_l: list[tuple[str, dict]] = []
        matched: list[tuple[tuple[str, dict], tuple[str, dict]]] = []
        for lrel_f in ls:                                   # proven-identical pairs first
            hit = next((i for i in remaining_r if proven_identical(lrel_f[1], rs[i][1])), None)
            if hit is None:
                unpaired_l.append(lrel_f)
            else:
                remaining_r.remove(hit)
                matched.append((lrel_f, rs[hit]))
        for lrel_f in unpaired_l:                           # then the rest in entry-id order
            if remaining_r:
                matched.append((lrel_f, rs[remaining_r.pop(0)]))
            else:
                left_rest.append(lrel_f)
        right_rest += [rs[i] for i in remaining_r]
        for (lrel, lf), (rrel, rf) in matched:
            if proven_identical(lf, rf):
                rows_out.append(pair_row("same", lrel, lf, rrel, rf, {"kind": "group"}))
            else:
                diff = difference(lf, rf)
                rows_out.append(pair_row("differs", lrel, lf, rrel, rf, diff) if diff
                                else pair_row("unverified", lrel, lf, rrel, rf, {"kind": "none"}))
    for rs in rby.values():
        right_rest += rs

    # ---- content pass
    left_rest.sort(key=lambda t: (t[0], t[1]["entry_id"]))
    right_rest.sort(key=lambda t: (t[0], t[1]["entry_id"]))
    by_group: dict[int, list[tuple[str, dict]]] = {}
    for item in right_rest:
        g = item[1]["content_group"]
        if g is not None:
            by_group.setdefault(g, []).append(item)
    used_right: set[int] = set()
    singles: list[tuple[str, str, dict]] = []               # (side, rel, file)
    for lrel, lf in left_rest:
        cands = by_group.get(lf["content_group"]) if lf["content_group"] is not None else None
        if cands:
            rrel, rf = cands.pop(0)
            used_right.add(rf["entry_id"])
            rows_out.append(pair_row("moved", lrel, lf, rrel, rf, {"kind": "group"}))
        else:
            singles.append(("left", lrel, lf))
    singles += [("right", rrel, rf) for rrel, rf in right_rest if rf["entry_id"] not in used_right]

    # ---- one-side files: copies elsewhere (one group query per chunk, no per-row query)
    groups = sorted({f["content_group"] for _, _, f in singles
                     if f["content_group"] is not None and f["size"]})
    live_cond = db.outside_quarantine(conn, "e.")
    copies: dict[int, list[tuple[int, str, str]]] = {}      # group -> [(remote_id, path, remote name)]
    names = {r.id: r.name for r in remotes}
    for part in chunks(groups):
        for rid, path, g in conn.execute(
                f"SELECT e.remote_id, e.path, e.content_group FROM entries e WHERE e.present = 1 AND e.is_dir = 0 "
                f"AND {live_cond} AND e.content_group IN ({','.join('?' * len(part))}) ORDER BY e.remote_id, e.path, e.id",
                part):
            copies.setdefault(g, []).append((rid, path, names[rid]))
    lgroups = {f["content_group"] for _, f in lfiles if f["content_group"] is not None}
    rgroups = {f["content_group"] for _, f in rfiles if f["content_group"] is not None}
    for side, rel, f in singles:
        row = {"rel": rel, "status": f"{side}_only", "rel_right": None, "left": None, "right": None,
               "evidence": None, "elsewhere": None, "unique": None}
        row["left" if side == "left" else "right"] = f
        g = f["content_group"]
        if g is not None and f["size"]:
            outside = [c for c in copies.get(g, [])
                       if not (c[0] == lrem.id and _under(c[1], lroot)) and not (c[0] == rrem.id and _under(c[1], rroot))]
            row["elsewhere"] = {"count": len(outside),
                                "examples": [{"remote": c[2], "path": c[1]} for c in outside[:_EXAMPLES]]}
            other = rgroups if side == "left" else lgroups
            row["unique"] = not outside and g not in other
        rows_out.append(row)

    # ---- result
    rows_out.sort(key=lambda r: (_natural(r["rel"]), r["rel"], (r["left"] or r["right"])["entry_id"]))
    summary: dict[str, Any] = {s: {"files": 0, "bytes": 0} for s in STATUSES}
    for r in rows_out:
        side = r["right"] if r["status"] == "right_only" else r["left"]
        slot = summary[r["status"]]
        slot["files"] += 1
        slot["bytes"] += side["size"] or 0
    summary["unique"] = sum(1 for r in rows_out if r["unique"])
    good = summary["same"]["files"] + summary["moved"]["files"]
    total = len(rows_out)
    summary["similarity"] = (good / total) if total else 1.0
    return {
        "left": {"remote": lrem.name, "path": lroot, "files": len(lfiles),
                 "bytes": sum(f["size"] or 0 for _, f in lfiles)},
        "right": {"remote": rrem.name, "path": rroot, "files": len(rfiles),
                  "bytes": sum(f["size"] or 0 for _, f in rfiles)},
        "summary": summary,
        "rows": rows_out,
    }


__all__ = ["CompareError", "STATUSES", "compare_folders", "difference", "proven_identical"]
