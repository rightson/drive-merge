"""Plan builders: turn a goal (dedupe, consolidate, reorganize, a hand-written
op list) into an ordered list of Ops. Builders only read the catalog; whether a
plan is safe is decided by simulate.py, never here.
"""

from __future__ import annotations

import posixpath
import sqlite3
from dataclasses import dataclass, field

from . import db
from .models import Op

# Strongest first: the executor re-checks this hash live before touching the file.
_HASH_PREFERENCE = ("sha256", "md5", "sha1", "dropbox", "quickxor")


@dataclass
class BuildResult:
    ops: list[Op] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)   # {"entry_id"?, "location", "reason"}
    stats: dict = field(default_factory=dict)


class BuildError(ValueError):
    pass


def best_hash(row) -> str | None:
    """'<type>:<hex>' for the strongest hash a catalog row carries."""
    for t in _HASH_PREFERENCE:
        if row[t]:
            return f"{t}:{row[t]}"
    return None


def parse_location(conn: sqlite3.Connection, loc: str) -> tuple[int, str]:
    """'gdrive:Photos/a.jpg' -> (remote_id, 'Photos/a.jpg'). Remote names never contain ':'."""
    if ":" not in loc:
        raise BuildError(f"expected '<remote>:<path>', got {loc!r}")
    name, path = loc.split(":", 1)
    try:
        return db.remote_id(conn, name), path.strip("/")
    except KeyError as e:
        raise BuildError(str(e)) from None


def _live_files_at(conn: sqlite3.Connection, rid: int, path: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE remote_id = ? AND path = ? AND present = 1 AND is_dir = 0",
        (rid, path),
    ).fetchall()


def _live_files_under(conn: sqlite3.Connection, rid: int, prefix: str) -> list[sqlite3.Row]:
    if prefix == "":
        return conn.execute(
            "SELECT * FROM entries WHERE remote_id = ? AND present = 1 AND is_dir = 0 ORDER BY path", (rid,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM entries WHERE remote_id = ? AND present = 1 AND is_dir = 0 "
        "AND substr(path, 1, ?) = ? ORDER BY path",
        (rid, len(prefix) + 1, prefix + "/"),
    ).fetchall()


def op_from_entry(row, kind: str, **kw) -> Op:
    """An Op whose source is an existing catalog entry, with live preconditions filled in."""
    return Op(kind, row["remote_id"], row["path"], src_entry_id=row["id"], expect_size=row["size"],
              expect_hash=best_hash(row), expect_mtime=row["mtime"], **kw)


def build_manual(conn: sqlite3.Connection, specs: list[dict]) -> BuildResult:
    """Ops written by a person or an LLM, e.g.

        {"op": "move", "src": "gdrive:Old/Projects", "dst": "gdrive:Projects", "reason": "..."}
        {"op": "delete", "src": "dropbox:a.jpg", "witness": "gdrive:Photos/a.jpg"}

    A directory source expands to one op per live file under it, keeping
    relative paths (for delete, the witness must then be a directory too). A
    source that is not in the catalog still yields an op so the simulation
    reports it (SRC_MISSING / AMBIGUOUS_PATH) instead of it silently vanishing.
    """
    result = BuildResult()
    for i, spec in enumerate(specs, start=1):
        kind = spec.get("op")
        if kind not in ("copy", "move", "delete"):
            raise BuildError(f"op #{i}: 'op' must be copy, move or delete")
        if "src" not in spec:
            raise BuildError(f"op #{i}: missing 'src'")
        src_rid, src_path = parse_location(conn, spec["src"])
        dst = parse_location(conn, spec["dst"]) if spec.get("dst") else None
        wit = parse_location(conn, spec["witness"]) if spec.get("witness") else None
        if kind in ("copy", "move") and dst is None:
            raise BuildError(f"op #{i}: {kind} needs 'dst'")
        reason = spec.get("reason")

        def make(row, rel: str | None):
            extra: dict = {"reason": reason}
            if dst:
                extra.update(dst_remote_id=dst[0], dst_path=posixpath.join(dst[1], rel) if rel else dst[1])
            if wit:
                extra.update(witness_remote_id=wit[0],
                             witness_path=posixpath.join(wit[1], rel) if rel else wit[1])
            if row is None:
                return Op(kind, src_rid, src_path, **extra)
            return op_from_entry(row, kind, **extra)

        files = _live_files_at(conn, src_rid, src_path)
        if len(files) == 1:
            result.ops.append(make(files[0], None))
            continue
        if len(files) > 1:  # Drive duplicate paths: let the simulator flag it
            result.ops.append(make(None, None))
            continue
        under = _live_files_under(conn, src_rid, src_path)
        if not under:
            result.ops.append(make(None, None))
            continue
        cut = len(src_path) + 1 if src_path else 0
        for row in under:
            result.ops.append(make(row, row["path"][cut:]))
    result.stats = {"specs": len(specs), "ops": len(result.ops)}
    return result


# The builders below are implemented in wave 2; see docs/DESIGN.md §5, §6, §8.

def build_dedupe(conn: sqlite3.Connection, **params) -> BuildResult:
    raise NotImplementedError


def build_consolidate(conn: sqlite3.Connection, **params) -> BuildResult:
    raise NotImplementedError


def build_reorganize(conn: sqlite3.Connection, **params) -> BuildResult:
    raise NotImplementedError
