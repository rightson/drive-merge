"""Plain data types shared across modules (no I/O here)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

OpKind = Literal["copy", "move", "delete"]


@dataclass
class Op:
    """One step of a plan.

    copy   src -> dst (src stays)
    move   src -> dst (src goes away); only within one remote in v1
    delete src goes to quarantine; witness names the copy that survives and
           justifies the delete — it must hold the same content and still be
           live after the whole plan has run.

    Sources and witnesses are addressed by (remote_id, path) so that an op can
    refer to a file created by an earlier op of the same plan. src_entry_id is
    set when the source is an existing catalog entry.
    """

    op: OpKind
    src_remote_id: int
    src_path: str
    src_entry_id: int | None = None
    dst_remote_id: int | None = None
    dst_path: str | None = None
    witness_remote_id: int | None = None
    witness_path: str | None = None
    expect_size: int | None = None
    expect_hash: str | None = None      # '<type>:<hex>'
    expect_mtime: str | None = None
    depends_on: int | None = None       # seq of an op that must be done first
    reason: str | None = None
    seq: int = 0                        # assigned by plans.create_plan (1-based)
    # Execution state (read back from the DB).
    id: int | None = None
    status: str = "pending"
    error: str | None = None

    def canonical(self) -> dict:
        """Fields that define what the op does; used for the plan digest."""
        return {
            "seq": self.seq, "op": self.op,
            "src": [self.src_remote_id, self.src_path, self.src_entry_id],
            "dst": [self.dst_remote_id, self.dst_path],
            "witness": [self.witness_remote_id, self.witness_path],
            "expect": [self.expect_size, self.expect_hash, self.expect_mtime],
            "depends_on": self.depends_on,
        }


@dataclass
class Plan:
    id: int
    title: str
    kind: str
    status: str
    created_by: str
    params: dict
    digest: str | None
    snapshot: dict | None
    created_at: str
    simulated_at: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    finished_at: str | None = None
    ops: list[Op] = field(default_factory=list)


@dataclass
class Violation:
    """A reason the plan must not run (or, as a warning, should be looked at).

    Codes used by the simulator:
      SRC_MISSING       source does not exist at that step
      DST_EXISTS        destination holds different content (never overwrite)
      CASE_COLLISION    destination collides case-insensitively on a case-insensitive remote
      AMBIGUOUS_PATH    several entries share the source path (Drive duplicates)
      NO_WITNESS        delete without a live witness at the end of the plan
      WITNESS_MISMATCH  witness content is not provably identical (exact) to the deleted file
      CONTENT_LOST      a content present before has no live copy after
      NATIVE_DOC        op touches a Google-native doc (no bytes to copy/verify)
      READ_ONLY_REMOTE  op writes to a remote without write_remote configured
      CROSS_REMOTE_MOVE move between remotes (use copy + delete)
      BAD_PATH          empty, absolute, or '..' path segment
      DEPENDENCY        depends_on names a missing or later op
      QUOTA             bytes added exceed the remote's known free space
      EXPECT_MISMATCH   the op's expect_size no longer matches the catalog (stale plan)
      NOOP              (warning) op changes nothing, e.g. copy onto identical content
      PROBABLE_ONLY     (warning) delete justified by name+size only; allowed only by policy

    `message` is an English sentence for people reading a terminal or a log. `params` holds the facts the
    sentence was made from, so that a client can word it in its own language: `variant` (which of the code's
    sentences this is, when it has several), `at` and other places as {"remote", "path"}, counts, sizes,
    `seq`s, and `why` / `problem` / `what` (a short kind, like "moved" or "directory", that has a sentence of its
    own). A report stored before `params` existed has an empty dict, and a page then shows `message` as it is.
    """

    code: str
    message: str
    seq: int | None = None
    params: dict = field(default_factory=dict)


def count_codes(items: list[Violation]) -> dict[str, int]:
    """How many violations (or warnings) carry each code, most frequent first."""
    counts: dict[str, int] = {}
    for v in items:
        counts[v.code] = counts.get(v.code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


@dataclass
class RemoteDelta:
    remote_id: int
    remote: str
    files_before: int = 0
    bytes_before: int = 0
    files_after: int = 0
    bytes_after: int = 0
    bytes_in: int = 0             # bytes written into this remote by copy/move
    files_quarantined: int = 0
    bytes_quarantined: int = 0


@dataclass
class SimulationReport:
    """What a simulation found. `violations` and `warnings` are capped (simulate.MAX_REPORTED) so a huge
    failing plan cannot produce an unbounded report; the *_total and *_by_code fields always count all of them."""

    plan_id: int | None
    digest: str
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)
    remotes: list[RemoteDelta] = field(default_factory=list)
    contents_before: int = 0          # distinct contents among live files touched by the plan's remotes
    contents_after: int = 0
    contents_lost: int = 0            # must be 0 for ok
    bytes_cross_remote: int = 0       # bytes that travel through this machine
    op_counts: dict[str, int] = field(default_factory=dict)
    sample: list[dict] = field(default_factory=list)  # first N human-readable changes
    violations_total: int = 0         # before the cap; ok is decided on the full count
    warnings_total: int = 0
    violations_by_code: dict[str, int] = field(default_factory=dict)
    warnings_by_code: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A report stored before the cap existed, or built by hand, carries the whole lists.
        if not self.violations_total:
            self.violations_total = len(self.violations)
        if not self.warnings_total:
            self.warnings_total = len(self.warnings)
        if not self.violations_by_code:
            self.violations_by_code = count_codes(self.violations)
        if not self.warnings_by_code:
            self.warnings_by_code = count_codes(self.warnings)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "SimulationReport":
        d = json.loads(text)
        d["violations"] = [Violation(**v) for v in d.get("violations", [])]
        d["warnings"] = [Violation(**v) for v in d.get("warnings", [])]
        d["remotes"] = [RemoteDelta(**r) for r in d.get("remotes", [])]
        return cls(**d)
