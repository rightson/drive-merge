"""The `drivemerge` command line.

Every command goes through `service.Service` (plus config loading). Commands
that change a cloud (`plan approve|execute|restore|purge`) are human-only and
share one gate, `_gate`.

Exit codes: 0 ok, 1 error, 2 usage error or refused (not a human at a
terminal), 3 simulation has violations, 130 interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import getpass
import importlib.resources
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import db, executor, plans
from . import service as service_mod
from .rclone import RcloneError

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_VIOLATIONS, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
MAX_VIOLATIONS_SHOWN = 20
# Fields that let someone approve a plan; shown to a human by `plan approve`
# only, never in other output (human or --json).
_SECRET_KEYS = frozenset({"digest", "approval_code"})
_AGENT_ENV_VARS = ("CLAUDECODE", "AI_AGENT")


class GateRefused(Exception):
    """A human-only command was started somewhere a human is not at the keyboard."""


# --------------------------------------------------------------------- parsing helpers

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
_SIZE_UNITS = {"": 1, "B": 1}
for _i, _letter in enumerate("KMGTP", start=1):
    for _suffix in ("", "B", "IB"):
        _SIZE_UNITS[_letter + _suffix] = 1024**_i
del _i, _letter, _suffix
_DATE_RE = re.compile(
    r"^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?)?)?)?$"
)
_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def parse_size(text: str) -> int:
    """'5G' / '500M' / '1T' / '1024' / '1.5GiB' -> bytes (binary units)."""
    m = _SIZE_RE.match(str(text))
    mult = _SIZE_UNITS.get(m.group(2).upper()) if m else None
    if not m or mult is None:
        raise ValueError(f"invalid size {text!r}; use a byte count or a number with K, M, G, T (e.g. 500M, 5G)")
    return int(Decimal(m.group(1)) * mult)


def format_size(n: int | None) -> str:
    """Bytes -> '1.5 GiB'."""
    if n is None:
        return "-"
    value = float(n)
    for unit in _BINARY_UNITS:
        if abs(value) < 1024 or unit == _BINARY_UNITS[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"  # unreachable


def parse_date(text: str) -> str:
    """'2023' / '2023-06' / '2023-06-15' / ISO timestamp -> text comparable with catalog mtimes."""
    m = _DATE_RE.match(text.strip())
    if not m:
        raise ValueError(f"invalid date {text!r}; use YYYY, YYYY-MM, YYYY-MM-DD or an ISO timestamp")
    year, month, day = m.groups()
    try:
        if month:
            datetime.date(int(year), int(month), int(day or 1))
    except ValueError:
        raise ValueError(f"invalid date {text!r}") from None
    return text.strip().replace(" ", "T").removesuffix("Z")


def _arg_type(fn: Callable[[str], Any]) -> Callable[[str], Any]:
    """Make a ValueError-raising parser usable as an argparse `type`."""
    def convert(text: str) -> Any:
        try:
            return fn(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None
    convert.__name__ = fn.__name__
    return convert


size_arg = _arg_type(parse_size)
date_arg = _arg_type(parse_date)


# ------------------------------------------------------------------------ output helpers

def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items() if k not in _SECRET_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


def _dump(obj: Any) -> None:
    print(json.dumps(_redact(obj), indent=2, ensure_ascii=False, default=str))


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], align: str = "") -> list[str]:
    """Plain aligned columns; `align` has one of 'l'/'r' per column (default left)."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(str(h)), *(len(r[i]) for r in cells)) if cells else len(str(h))
              for i, h in enumerate(headers)]
    right = [i < len(align) and align[i] == "r" for i in range(len(headers))]

    def line(values: Sequence[str]) -> str:
        return "  ".join(v.rjust(w) if right[i] else v.ljust(w) for i, (v, w) in enumerate(zip(values, widths))).rstrip()

    return [line([str(h) for h in headers]), *(line(r) for r in cells)]


def _n(value: Any) -> str:
    return f"{int(value or 0):,}"


def _kv(d: dict, skip: Sequence[str] = ()) -> str:
    return "  ".join(f"{k}={v}" for k, v in d.items() if k not in skip and isinstance(v, (str, int, float, bool)))


def _message(e: BaseException) -> str:
    if isinstance(e, KeyError) and e.args and isinstance(e.args[0], str):
        return e.args[0]
    return str(e) or type(e).__name__


class _Progress:
    """Throttled progress on stderr: one rewritten line on a terminal, an occasional line otherwise."""

    def __init__(self, render: Callable[..., str]):
        self.render = render
        self._last = 0.0
        self._dirty = False

    def __call__(self, *values: Any) -> None:
        stream = sys.stderr
        tty = _is_tty(stream)
        now = time.monotonic()
        if now - self._last < (0.25 if tty else 5.0):
            return
        self._last = now
        text = self.render(*values)
        if tty:
            stream.write("\r\x1b[K" + text)
            self._dirty = True
        else:
            stream.write(text + "\n")
        stream.flush()

    def finish(self) -> None:
        if self._dirty:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()
            self._dirty = False


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


# -------------------------------------------------------------------------------- context

def _load_config(path: Path) -> config_mod.Config:
    """config.load_config with errors a person can act on."""
    try:
        return config_mod.load_config(path)
    except FileNotFoundError:
        raise FileNotFoundError(f"no config at {path}; run `drivemerge init` to create one") from None
    except KeyError as e:
        raise ValueError(f"invalid config {path}: missing key {e.args[0]!r}") from None
    except TypeError as e:
        raise ValueError(f"invalid config {path}: {e}") from None


class Ctx:
    """Parsed arguments plus lazily loaded config and service."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.json = bool(getattr(args, "json", False))
        raw = getattr(args, "config", None) or os.environ.get("DRIVEMERGE_CONFIG") or config_mod.DEFAULT_CONFIG_PATH
        self.config_path = Path(raw).expanduser()
        self._config: config_mod.Config | None = None
        self._service: service_mod.Service | None = None

    @property
    def config(self) -> config_mod.Config:
        if self._config is None:
            self._config = _load_config(self.config_path)
        return self._config

    @property
    def service(self) -> service_mod.Service:
        if self._service is None:
            self._service = service_mod.Service(self.config)
        return self._service


# ------------------------------------------------------------------------ report printing

def format_report(sim: dict | None, *, warnings_shown: int = 0, plan_id: int | str = "<id>") -> list[str]:
    """Human-readable lines for a simulation report (dict form)."""
    if not sim:
        return [f"Simulation: none yet (run `drivemerge plan simulate {plan_id}`)"]
    violations = sim.get("violations") or []
    warnings = sim.get("warnings") or []
    lines = ["Simulation: OK, no violations" if sim.get("ok")
             else f"Simulation: FAILED, {len(violations)} violation(s)"]
    for v in violations[:MAX_VIOLATIONS_SHOWN]:
        where = f"op {v['seq']}" if v.get("seq") else "plan"
        lines.append(f"  {v.get('code', '?'):<18} {where:<7} {v.get('message', '')}")
    if len(violations) > MAX_VIOLATIONS_SHOWN:
        lines.append(f"  ... and {len(violations) - MAX_VIOLATIONS_SHOWN} more")
    lines.append(f"Warnings: {len(warnings)}")
    for w in warnings[:warnings_shown]:
        where = f"op {w['seq']}" if w.get("seq") else "plan"
        lines.append(f"  {w.get('code', '?'):<18} {where:<7} {w.get('message', '')}")
    if warnings_shown and len(warnings) > warnings_shown:
        lines.append(f"  ... and {len(warnings) - warnings_shown} more")
    if any((sim.get("op_counts") or {}).values()):
        lines.append("Ops: " + ", ".join(f"{n} {k}" for k, n in sorted(sim["op_counts"].items()) if n))
    remotes = sim.get("remotes") or []
    if remotes:
        lines.append("Per remote (files / bytes, before -> after):")
        for r in remotes:
            lines.append(
                f"  {r.get('remote', '?')}: {_n(r.get('files_before'))} files / {format_size(r.get('bytes_before'))}"
                f" -> {_n(r.get('files_after'))} files / {format_size(r.get('bytes_after'))}")
    lines.append(f"Bytes quarantined: {format_size(sum(r.get('bytes_quarantined') or 0 for r in remotes))}")
    lines.append(f"Bytes copied across remotes: {format_size(sim.get('bytes_cross_remote'))}")
    lines.append(f"Contents: {_n(sim.get('contents_before'))} -> {_n(sim.get('contents_after'))}"
                 f", lost: {_n(sim.get('contents_lost'))}")
    return lines


def _short(value: Any) -> str:
    text = str(value)
    if len(text) <= 60:
        return text
    return f"<{len(value)} {'chars' if isinstance(value, str) else 'items'}>" if hasattr(value, "__len__") else text[:57] + "..."


def _loc(names: dict[int, str], rid: int | None, path: str | None) -> str:
    return f"{names.get(rid, f'#{rid}')}:{path}"


def _remote_names(ctx: Ctx, plan: dict) -> dict[int, str]:
    names = {r["remote_id"]: r["remote"] for r in (plan.get("simulation") or {}).get("remotes", [])
             if "remote_id" in r and "remote" in r}
    ids = {op.get(k) for op in plan.get("ops") or [] for k in ("src_remote_id", "dst_remote_id", "witness_remote_id")}
    if ids - {None} - set(names):
        with contextlib.suppress(Exception):  # names are cosmetic
            names.update({r["id"]: r["name"] for r in ctx.service.stats()["remotes"]})
    return names


def format_plan(plan: dict, names: dict[int, str], *, ops_shown: int = 20) -> list[str]:
    """Header, parameters, first ops and simulation report of a plan (dict from Service.plan)."""
    lines = [f"Plan {plan['id']}: {plan.get('title', '')}  [{plan.get('kind', '?')}]  status: {plan.get('status', '?')}"]
    when = [f"created {str(plan.get('created_at') or '')[:19]} by {plan.get('created_by')}"]
    if plan.get("approved_at"):
        when.append(f"approved {str(plan['approved_at'])[:19]} by {plan.get('approved_by')}")
    if plan.get("finished_at"):
        when.append(f"finished {str(plan['finished_at'])[:19]}")
    lines.append("  " + "; ".join(when))
    params = plan.get("params") or {}
    if params:
        lines.append("  parameters: " + "  ".join(f"{k}={_short(v)}" for k, v in params.items()))
    ops = plan.get("ops") or []
    total = plan.get("ops_total", len(ops))
    lines.append(f"Operations: {_n(total)}" + (f" (first {min(len(ops), ops_shown)} shown)" if total > ops_shown else ""))
    for op in ops[:ops_shown]:
        text = f"  {op.get('seq', 0):>4} {op.get('op', '?'):<6} {_loc(names, op.get('src_remote_id'), op.get('src_path'))}"
        if op.get("dst_path") is not None:
            text += f" -> {_loc(names, op.get('dst_remote_id'), op['dst_path'])}"
        if op.get("witness_path") is not None:
            text += f"  [witness {_loc(names, op.get('witness_remote_id'), op['witness_path'])}]"
        if op.get("status") not in (None, "pending"):
            text += f"  <{op['status']}{': ' + op['error'] if op.get('error') else ''}>"
        lines.append(text)
    lines.extend(format_report(plan.get("simulation"), warnings_shown=10, plan_id=plan["id"]))
    return lines


def _violations(sim: dict | None) -> bool:
    return bool(sim) and not sim.get("ok", True)


# ------------------------------------------------------------------------- the human gate

@dataclass
class Confirmation:
    """What a gated command asks the human: summary to show, prompt, and the exact text to accept.

    `expected=None` means the service validates the typed text (plan approval).
    """

    show: Callable[[], None]
    prompt: str
    expected: str | None = None


def _human_refusal(action: str) -> str | None:
    """Why `action` may not run here, or None when a human seems to be at a terminal.

    This is a speed bump, not a security boundary: any process running as the
    same user can allocate a pty, clear these variables or call the Python API
    directly. It stops an AI agent from approving a plan by accident or by
    prompt injection; the protection that matters is that approval needs a
    simulation-bound code and execution is off unless [safety] enables it.
    """
    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        return (f"refusing to {action}: this command needs an interactive terminal "
                "(stdin and stdout must both be a TTY); run it yourself in a terminal, not from a script or pipe")
    for var in _AGENT_ENV_VARS:
        if var in os.environ:
            return (f"refusing to {action}: {var} is set, so this looks like an AI agent session; "
                    "run this in your own terminal, not through an AI agent")
    return None


def _gate(action: str, prepare: Callable[[], Confirmation]) -> str:
    """The one human-only gate for approve, execute, restore and purge.

    Checks that a human is at a terminal, lets `prepare` run its own
    pre-checks and build the confirmation, shows the summary and reads the
    confirmation from the terminal (never argv, env or a pipe). Returns the text typed.
    """
    refusal = _human_refusal(action)
    if refusal:
        raise GateRefused(refusal)
    confirmation = prepare()
    confirmation.show()
    try:
        typed = input(confirmation.prompt).strip()
    except EOFError:
        raise ValueError("aborted: no confirmation entered") from None
    if confirmation.expected is not None and " ".join(typed.split()) != confirmation.expected:
        raise ValueError(f"aborted: confirmation did not match (expected {confirmation.expected!r})")
    return typed


def _approval_code(plan: dict) -> str:
    digest = plan.get("digest")
    if not digest:
        raise plans.PlanError(f"plan {plan['id']} has not been simulated; run `drivemerge plan simulate {plan['id']}`")
    return plans.short_code(digest)


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry, odd containers
        return "unknown"


def _exec_progress(*args: Any, **kwargs: Any) -> None:
    """Executor progress callback: one line per report on stderr."""
    def describe(a: Any) -> str:
        if dataclasses.is_dataclass(a) and not isinstance(a, type):
            a = dataclasses.asdict(a)
        if isinstance(a, dict):
            return " ".join(f"{k}={v}" for k, v in a.items() if isinstance(v, (str, int, float)) and v not in ("", None))
        return str(a)
    parts = [describe(a) for a in args] + [f"{k}={v}" for k, v in kwargs.items()]
    print("  " + " ".join(p for p in parts if p), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------- commands: setup

def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` readable by the owner only, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def cmd_init(ctx: Ctx) -> int:
    path = ctx.config_path
    if path.exists() and not ctx.args.force:
        raise ValueError(f"{path} already exists; use --force to overwrite it (this also replaces the API token)")
    template = importlib.resources.files("drivemerge").joinpath("config_template.toml").read_text(encoding="utf-8")
    token = secrets.token_urlsafe(32)
    text, count = re.subn(r'(?m)^token\s*=\s*"".*$', f'token = "{token}"', template, count=1)
    if count != 1:
        raise ValueError("config template has no empty [api] token line")
    _write_private(path, text)
    cfg = _load_config(path)  # make sure what we wrote parses
    db.connect(cfg.db_path).close()
    db_path = str(Path(cfg.db_path).expanduser())
    if ctx.json:
        _dump({"config": str(path), "database": db_path, "api_token": "generated"})
        return EXIT_OK
    print(f"Wrote {path} (mode 600, new API token) and created the catalog {db_path}")
    print()
    print("Next steps:")
    print("  1. Configure one rclone remote per cloud:   rclone config")
    print("     (use a read-only remote for scanning, e.g. Google Drive scope drive.readonly)")
    print(f"  2. Edit the [[remotes]] entries in {path} to match your rclone remotes")
    print("  3. Check them, then scan:                   drivemerge remotes && drivemerge scan --all")
    return EXIT_OK


def _last_scan(scan: dict | None) -> str:
    if not scan:
        return "never"
    return f"{str(scan.get('finished_at') or '')[:16].replace('T', ' ')} {scan.get('status', '')}".strip()


def cmd_remotes(ctx: Ctx) -> int:
    stats = {r["name"]: r for r in ctx.service.stats()["remotes"]}
    rows = []
    for r in ctx.config.remotes:
        s = stats.get(r.name, {})
        rows.append({"name": r.name, "provider": r.provider, "read": r.read, "write": r.write,
                     "read_only": r.write is None, "last_scan": s.get("last_scan"),
                     "files": s.get("files", 0), "bytes": s.get("bytes", 0)})
    if ctx.json:
        _dump(rows)
        return EXIT_OK
    if not rows:
        print(f"No remotes configured; add [[remotes]] entries to {ctx.config_path}")
        return EXIT_OK
    print("\n".join(_table(["NAME", "PROVIDER", "READ", "WRITE", "LAST SCAN"],
                           [[r["name"], r["provider"], r["read"], r["write"] or "read-only", _last_scan(r["last_scan"])]
                            for r in rows])))
    return EXIT_OK


def cmd_scan(ctx: Ctx) -> int:
    a = ctx.args
    if a.all == bool(a.remote):
        raise ValueError("give a REMOTE to scan, or --all")
    if a.all and a.root:
        raise ValueError("--root cannot be combined with --all")
    names = [r.name for r in ctx.config.remotes] if a.all else [a.remote]
    if not names:
        raise ValueError("no remotes configured")
    results, failed = [], False
    for name in names:
        progress = _Progress(lambda files, dirs, size, _n_=name: (
            f"scanning {_n_}: {_n(files)} files, {_n(dirs)} dirs, {format_size(size)}"))
        try:
            res = ctx.service.scan(name, root=a.root or "", progress=progress, force=a.force)
        except Exception as e:
            if not a.all:
                raise
            res = {"remote": name, "status": "failed", "error": _message(e)}
        finally:
            progress.finish()
        results.append(res)
        failed = failed or res.get("status") != "complete" or bool(res.get("error"))
        if not ctx.json:
            if res.get("status") == "complete":
                where = f"{name}:{res['root']}" if res.get("root") else name
                print(f"{where}: {_n(res['files'])} files, {_n(res['dirs'])} dirs, {format_size(res['bytes'])}"
                      f", {_n(res['tombstoned'])} no longer present")
            else:
                print(f"{name}: scan failed: {res.get('error')}", file=sys.stderr)
    if ctx.json:
        _dump(results if a.all else results[0])
    elif not failed:
        print("Next: drivemerge identity rebuild")
    return EXIT_ERROR if failed else EXIT_OK


def _identity_lines(stats: dict | None) -> list[str]:
    if not stats:
        return []
    lines = [f"{_n(stats.get('files_hashed'))} hashed files in {_n(stats.get('groups'))} content groups; "
             f"{_n(stats.get('duplicate_groups'))} duplicate groups "
             f"({_n(stats.get('redundant_files'))} redundant files, {format_size(stats.get('redundant_bytes'))})"]
    if stats.get("size_conflicts") or stats.get("hash_conflicts"):
        lines.append(f"conflicts: {_n(stats.get('size_conflicts'))} size, {_n(stats.get('hash_conflicts'))} hash")
    return lines


def cmd_identity_rebuild(ctx: Ctx) -> int:
    stats = ctx.service.rebuild_identity()
    if ctx.json:
        _dump(stats)
    else:
        print("Identity rebuilt: " + "\n".join(_identity_lines(stats)))
    return EXIT_OK


def cmd_bridge(ctx: Ctx) -> int:
    a = ctx.args
    progress = _Progress(lambda done, bad: f"bridging: {done} done, {bad} failed")
    try:
        res = ctx.service.bridge(max_bytes=a.max_bytes, limit=a.limit, progress=progress)
    finally:
        progress.finish()
    if ctx.json:
        _dump(res)
        return EXIT_OK
    print(f"Bridged {_n(res['bridged'])} file(s), {len(res['failed'])} failed")
    for f in res["failed"][:10]:
        print(f"  entry {f['entry_id']}: {f['error']}")
    if len(res["failed"]) > 10:
        print(f"  ... and {len(res['failed']) - 10} more")
    for line in _identity_lines(res.get("groups")):
        print(line)
    return EXIT_OK


# ---------------------------------------------------------------------------- commands: read

def cmd_search(ctx: Ctx) -> int:
    a = ctx.args
    rows = ctx.service.search(
        a.query, remote=a.remote, category=a.category, ext=a.ext, min_size=a.min_size, max_size=a.max_size,
        modified_after=a.after, modified_before=a.before, path_prefix=a.prefix, include_dirs=a.dirs,
        include_missing=a.missing, duplicates_only=a.dups, limit=a.limit, offset=a.offset)
    if ctx.json:
        _dump(rows)
        return EXIT_OK
    if not rows:
        print("No matches.")
        return EXIT_OK
    table = [[e["id"], e["remote"], e["category"] if not e["is_dir"] else "dir",
              format_size(e["size"]) if not e["is_dir"] else "-", (e["mtime"] or "")[:10],
              e["path"] + ("" if e["present"] else "  (gone)")] for e in rows]
    print("\n".join(_table(["ID", "REMOTE", "CATEGORY", "SIZE", "MODIFIED", "PATH"], table, "rllrll")))
    if len(rows) >= a.limit:
        print(f"-- {len(rows)} shown; use --offset {a.offset + len(rows)} for more")
    return EXIT_OK


def cmd_stats(ctx: Ctx) -> int:
    st = ctx.service.stats()
    if ctx.json:
        _dump(st)
        return EXIT_OK
    totals, dup = st["totals"], st["duplicates"]
    print(f"{_n(totals['files'])} files, {format_size(totals['bytes'])} in the catalog")
    print()
    print("\n".join(_table(
        ["REMOTE", "PROVIDER", "FILES", "DIRS", "SIZE", "LAST SCAN"],
        [[r["name"], r["provider"], _n(r["files"]), _n(r["dirs"]), format_size(r["bytes"]), _last_scan(r["last_scan"])]
         for r in st["remotes"]], "llrrrl")))
    print()
    print(f"Duplicates: {_n(dup['groups'])} groups, {_n(dup['redundant_files'])} redundant files, "
          f"{format_size(dup['redundant_bytes'])} reclaimable")
    if st["categories"]:
        print()
        print("\n".join(_table(["CATEGORY", "FILES", "SIZE"],
                               [[c["category"], _n(c["files"]), format_size(c["bytes"])] for c in st["categories"]], "lrr")))
    return EXIT_OK


def cmd_locations(ctx: Ctx) -> int:
    copies = ctx.service.locations(ctx.args.entry_id)
    if not copies:
        raise KeyError(f"no live copies of entry {ctx.args.entry_id} (unknown id, or the file is gone)")
    if ctx.json:
        _dump(copies)
        return EXIT_OK
    print(f"Entry {ctx.args.entry_id}: {len(copies)} live cop{'y' if len(copies) == 1 else 'ies'} of this content")
    print("\n".join(_table(["ENTRY", "LOCATION", "SIZE", "MODIFIED"],
                           [[c["entry_id"], f"{c['remote']}:{c['path']}", format_size(c["size"]), (c["mtime"] or "")[:10]]
                            for c in copies], "rlrl")))
    return EXIT_OK


def cmd_duplicates(ctx: Ctx) -> int:
    a = ctx.args
    groups = ctx.service.duplicates(
        confidence="probable" if a.probable else "exact", remote=a.remote, cross_remote_only=a.cross_remote,
        min_size=a.min_size, path_prefix=a.prefix, limit=a.limit, offset=a.offset)
    if ctx.json:
        _dump(groups)
        return EXIT_OK
    if not groups:
        print("No duplicate groups found." + ("" if a.probable else " (Try --probable for name+size candidates.)"))
        return EXIT_OK
    for g in groups:
        print(f"{g['key']}  {g['confidence']}  {len(g['copies'])} copies x {format_size(g['size'])}"
              f"  reclaimable {format_size(g['redundant_bytes'])}  on {', '.join(g['remotes'])}")
        for c in g["copies"]:
            print(f"    {c['remote']}:{c['path']}  (entry {c['entry_id']})")
    print(f"-- {len(groups)} group(s), {format_size(sum(g['redundant_bytes'] for g in groups))} reclaimable"
          + (f"; use --offset {a.offset + len(groups)} for more" if len(groups) >= a.limit else ""))
    return EXIT_OK


def cmd_profiles(ctx: Ctx) -> int:
    a = ctx.args
    kwargs = {k: v for k, v in (("remote", a.remote), ("prefix", a.prefix), ("depth", a.depth), ("limit", a.limit))
              if v is not None}
    found = ctx.service.profiles(**kwargs)
    if ctx.json:
        _dump(found)
        return EXIT_OK
    if not found:
        print("No folders found.")
        return EXIT_OK
    for p in found:
        flags = ([f"unit: {p['unit']}"] if p.get("unit") else []) + (
            [f"{_n(p['duplicate_files'])} duplicate files"] if p.get("duplicate_files") else [])
        print(f"{p['remote']}:{p['path'] or '/'}  {_n(p['files'])} files, {format_size(p['bytes'])}"
              + (f"  [{'; '.join(flags)}]" if flags else ""))
        cats = ", ".join(f"{k} {v}" for k, v in sorted((p.get("categories") or {}).items(), key=lambda kv: -kv[1]))
        exts = ", ".join(f"{e} {n}" for e, n in p.get("top_exts") or [])
        dates = f"{(p.get('mtime_min') or '')[:10]}..{(p.get('mtime_max') or '')[:10]}" if p.get("mtime_min") else ""
        for label, value in (("categories", cats), ("extensions", exts), ("modified", dates),
                             ("samples", ", ".join(p.get("sample_names") or []))):
            if value:
                print(f"    {label}: {value}")
    return EXIT_OK


def _read_text(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e.strerror or e}") from None


def cmd_rules_check(ctx: Ctx) -> int:
    res = ctx.service.validate_rules(_read_text(ctx.args.file))
    if ctx.json:
        _dump(res)
    elif res.get("ok"):
        rules = res.get("rules") or []
        print(f"OK: {len(rules)} rule(s)" + (": " + ", ".join(rules) if rules else ""))
    else:
        print(f"Invalid rules: {res.get('error')}", file=sys.stderr)
    return EXIT_OK if res.get("ok") else EXIT_ERROR


def cmd_rules_preview(ctx: Ctx) -> int:
    a = ctx.args
    res = ctx.service.preview_layout(
        rules_text=_read_text(a.rules) if a.rules else None, remotes=a.remote or None,
        target_root=a.target_root or "", path_prefix=a.prefix, limit=a.limit)
    if ctx.json:
        _dump(res)
        return EXIT_OK
    print(f"{_n(res['total'])} file(s) would be placed")
    if res.get("by_rule"):
        print("\n".join(_table(["RULE", "FILES"], [[k, _n(v)] for k, v in sorted(res["by_rule"].items(), key=lambda kv: -kv[1])], "lr")))
    sample = res.get("sample") or []
    if sample:
        names = {r["id"]: r["name"] for r in ctx.service.stats()["remotes"]}
        print()
        print(f"Sample (first {len(sample)}):")
        for p in sample:
            print(f"  [{p.get('rule')}] {_loc(names, p.get('remote_id'), p.get('src_path'))} -> {p.get('target_path')}")
    return EXIT_OK


# --------------------------------------------------------------------------- commands: plans

def _params(**kw: Any) -> dict:
    """Plan parameters: values that were not given (None, empty list) are left out."""
    return {k: v for k, v in kw.items() if v is not None and v != []}


def _propose(ctx: Ctx, kind: str, params: dict) -> int:
    res = ctx.service.propose_plan(kind, params, created_by="cli", title=ctx.args.title)
    sim = res.get("simulation")
    code = EXIT_VIOLATIONS if _violations(sim) else EXIT_OK
    if ctx.json:
        _dump(res)
        return code
    skipped = res.get("skipped") or []
    if res.get("plan_id") is None:
        print(f"Nothing to do: {res.get('message', 'the plan has no operations')}")
        if skipped:
            print(f"{len(skipped)} item(s) skipped")
        return code
    pid = res["plan_id"]
    print(f"Plan {pid} created ({kind}): {_n(res.get('ops'))} op(s), {_n(len(skipped))} skipped")
    for s in skipped[:5]:
        print(f"  skipped {s.get('location', '?')}: {s.get('reason', '')}")
    if len(skipped) > 5:
        print(f"  ... and {len(skipped) - 5} more skipped")
    if res.get("stats"):
        print("Builder stats: " + _kv(res["stats"]))
    print("\n".join(format_report(sim)))
    if sim and sim.get("ok"):
        print(f"Review with `drivemerge plan show {pid}`, then approve it in your own terminal: "
              f"drivemerge plan approve {pid}")
    elif sim:
        print(f"Plan {pid} was saved but cannot be approved. Fix the violations and propose a new plan, "
              f"or cancel it: drivemerge plan cancel {pid}")
    return code


def _rules_param(a: argparse.Namespace) -> str | None:
    return _read_text(a.rules) if getattr(a, "rules", None) else None


def cmd_plan_dedupe(ctx: Ctx) -> int:
    a = ctx.args
    return _propose(ctx, "dedupe", _params(
        remote=a.remote, cross_remote=a.cross_remote, path_prefix=a.prefix, min_size=a.min_size,
        prefer_remotes=a.prefer))


def cmd_plan_consolidate(ctx: Ctx) -> int:
    a = ctx.args
    return _propose(ctx, "consolidate", _params(
        target=a.target, target_root=a.root, sources=a.source, delete_sources=a.delete_sources,
        reorganize_target=a.reorganize_target, path_prefix=a.prefix, rules=_rules_param(a)))


def cmd_plan_reorganize(ctx: Ctx) -> int:
    a = ctx.args
    return _propose(ctx, "reorganize", _params(
        remote=a.remote, target_root=a.root, path_prefix=a.prefix, rules=_rules_param(a)))


def cmd_plan_manual(ctx: Ctx) -> int:
    try:
        specs = json.loads(_read_text(ctx.args.file))
    except json.JSONDecodeError as e:
        raise ValueError(f"{ctx.args.file}: invalid JSON: {e}") from None
    if isinstance(specs, dict) and "ops" in specs:
        specs = specs["ops"]
    if not isinstance(specs, list):
        raise ValueError(f"{ctx.args.file} must hold a JSON list of op specs")
    return _propose(ctx, "manual", {"ops": specs})


def cmd_plan_list(ctx: Ctx) -> int:
    found = ctx.service.plans(status=ctx.args.status)
    if ctx.json:
        _dump(found)
        return EXIT_OK
    if not found:
        print("No plans.")
        return EXIT_OK
    print("\n".join(_table(
        ["ID", "STATUS", "KIND", "CREATED", "BY", "TITLE"],
        [[p["id"], p["status"], p["kind"], str(p.get("created_at") or "")[:16].replace("T", " "),
          p.get("created_by") or "", p.get("title") or ""] for p in found], "rlllll")))
    return EXIT_OK


def cmd_plan_show(ctx: Ctx) -> int:
    plan = ctx.service.plan(ctx.args.id, ops_limit=ctx.args.ops)
    if ctx.json:
        _dump(plan)
        return EXIT_OK
    print("\n".join(format_plan(plan, _remote_names(ctx, plan), ops_shown=ctx.args.ops)))
    return EXIT_OK


def cmd_plan_simulate(ctx: Ctx) -> int:
    pid = ctx.args.id
    sim = ctx.service.simulate(pid)
    if ctx.json:
        _dump(sim)
    else:
        print(f"Plan {pid}")
        print("\n".join(format_report(sim, warnings_shown=10)))
        if sim.get("ok"):
            print(f"Next: drivemerge plan approve {pid}")
    return EXIT_VIOLATIONS if _violations(sim) else EXIT_OK


def cmd_plan_cancel(ctx: Ctx) -> int:
    ctx.service.cancel(ctx.args.id)
    if ctx.json:
        _dump({"plan_id": ctx.args.id, "status": "cancelled"})
    else:
        print(f"Plan {ctx.args.id} cancelled")
    return EXIT_OK


# ----------------------------------------------------------- commands: plans (human-only)

def _summary(ctx: Ctx, plan: dict) -> Callable[[], None]:
    return lambda: print("\n".join(format_plan(plan, _remote_names(ctx, plan), ops_shown=10)) + "\n")


def cmd_plan_approve(ctx: Ctx) -> int:
    pid = ctx.args.id

    def prepare() -> Confirmation:
        plan = ctx.service.plan(pid, ops_limit=10)
        code = _approval_code(plan)
        if plan.get("status") != "simulated":
            raise plans.PlanError(
                f"plan {pid} is {plan.get('status')}; only a simulated plan with no violations can be approved")
        return Confirmation(_summary(ctx, plan), f"Type the approval code {code} to approve plan {pid}: ")

    typed = _gate("approve a plan", prepare)
    ctx.service.approve(pid, typed, approved_by=f"cli:{_current_user()}")
    if ctx.json:
        _dump({"plan_id": pid, "status": "approved"})
    else:
        print(f"Plan {pid} approved. To run it: drivemerge plan execute {pid}")
    return EXIT_OK


def _result(ctx: Ctx, verb: str, pid: int, res: dict) -> int:
    failed = res.get("status") == "failed" or bool(res.get("error")) or bool(res.get("failed"))
    if ctx.json:
        _dump(res)
    else:
        print(f"Plan {pid} {verb}: {res.get('status')} - {_n(res.get('done'))} done, "
              f"{_n(res.get('failed'))} failed, {_n(res.get('skipped'))} skipped")
        if res.get("error"):
            print(f"error: {res['error']}", file=sys.stderr)
    return EXIT_ERROR if failed else EXIT_OK


def cmd_plan_execute(ctx: Ctx) -> int:
    pid = ctx.args.id

    def prepare() -> Confirmation:
        if not ctx.config.safety.execution_enabled:
            raise executor.ExecutionDisabled(
                "execution is disabled. To allow drive-merge to change your clouds, set "
                f"`execution_enabled = true` under [safety] in {ctx.config_path}, then run this again")
        plan = ctx.service.plan(pid, ops_limit=10)
        return Confirmation(_summary(ctx, plan), f'This will change your clouds. Type "execute {pid}" to run plan {pid}: ',
                            f"execute {pid}")

    _gate("execute a plan", prepare)
    return _result(ctx, "executed", pid, ctx.service.execute(pid, progress=_exec_progress))


def cmd_plan_restore(ctx: Ctx) -> int:
    pid = ctx.args.id

    def prepare() -> Confirmation:
        plan = ctx.service.plan(pid, ops_limit=10)
        return Confirmation(_summary(ctx, plan), f'Type "restore {pid}" to move plan {pid}\'s quarantined files back: ',
                            f"restore {pid}")

    _gate("restore a plan", prepare)
    return _result(ctx, "restored", pid, ctx.service.restore(pid))


def cmd_plan_purge(ctx: Ctx) -> int:
    pid, days = ctx.args.id, ctx.args.older_than_days

    def prepare() -> Confirmation:
        plan = ctx.service.plan(pid, ops_limit=10)
        return Confirmation(
            _summary(ctx, plan),
            f'Quarantined files of plan {pid} older than {days} day(s) will be deleted for good. '
            f'Type "purge {pid}" to continue: ', f"purge {pid}")

    _gate("purge a plan's quarantine", prepare)
    return _result(ctx, "purged", pid, ctx.service.purge(pid, older_than_days=days))


# ---------------------------------------------------------------------- commands: the rest

def cmd_changes(ctx: Ctx) -> int:
    res = ctx.service.changes(ctx.args.cursor, ctx.args.limit)
    if ctx.json:
        _dump(res)
        return EXIT_OK
    for e in res["rows"]:
        print(f"{e['row_version']}:{e['id']}  {e['remote']}:{e['path']}  "
              + ("dir" if e["is_dir"] else format_size(e["size"])) + ("" if e["present"] else "  (gone)"))
    print(f"-- {len(res['rows'])} change(s); next cursor: {res['cursor'] or '(none)'}")
    return EXIT_OK


def cmd_serve(ctx: Ctx) -> int:
    cfg = ctx.config
    if ctx.args.host:
        cfg.api.host = ctx.args.host
    if ctx.args.port is not None:
        cfg.api.port = ctx.args.port
    try:
        from . import api
    except ImportError as e:
        raise ValueError(f"the REST API needs its optional dependencies: pip install 'drive-merge[api]' ({e})") from None
    print(f"Serving on http://{cfg.api.host}:{cfg.api.port} (Ctrl-C to stop)", file=sys.stderr)
    api.serve(cfg)
    return EXIT_OK


def cmd_ui(ctx: Ctx) -> int:
    cfg = ctx.config
    try:
        from . import ui_api
    except ImportError as e:
        raise ValueError(f"the web UI needs its optional dependencies: pip install 'drive-merge[api]' ({e})") from None
    ui_api.run_ui(cfg, host=ctx.args.host, port=ctx.args.port, open_browser=not ctx.args.no_browser)
    return EXIT_OK


def cmd_mcp(ctx: Ctx) -> int:
    _ = ctx.config  # fail early, with the `drivemerge init` hint, if there is no config
    try:
        from . import mcp_server
    except ImportError as e:
        raise ValueError(f"the MCP server needs its optional dependencies: pip install 'drive-merge[mcp]' ({e})") from None
    return int(mcp_server.main(str(ctx.config_path)) or 0)


def _upstream(name: str, read: str) -> str:
    token = f"{name}={read}"
    return '"' + token.replace('"', '""') + '"' if re.search(r'[\s"]', token) else token


def cmd_rclone_combine(ctx: Ctx) -> int:
    name = ctx.args.name
    if not re.fullmatch(r"[A-Za-z0-9_.+@][A-Za-z0-9_.+@-]*", name):
        raise ValueError(f"invalid rclone remote name {name!r}")
    if not ctx.config.remotes:
        raise ValueError("no remotes configured")
    upstreams = " ".join(_upstream(r.name, r.read) for r in ctx.config.remotes)
    section = (
        "# combine (not union) keeps each cloud as its own top-level folder, so you can always tell which cloud "
        "holds a file; union merges by path and hides that.\n"
        f"[{name}]\ntype = combine\nupstreams = {upstreams}"
    )
    if ctx.json:
        _dump({"name": name, "section": section, "upstreams": {r.name: r.read for r in ctx.config.remotes}})
        return EXIT_OK
    print(section)
    print(f"Append this to your rclone config (`rclone config file` shows where), then try `rclone lsd {name}:`",
          file=sys.stderr)
    return EXIT_OK


# --------------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS,
                        help=f"config file (default: $DRIVEMERGE_CONFIG or {config_mod.DEFAULT_CONFIG_PATH})")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable JSON output")

    parser = argparse.ArgumentParser(
        prog="drivemerge", parents=[common],
        description="Catalog, deduplicate and consolidate files across clouds. "
                    "Nothing changes a cloud except an approved plan.",
        allow_abbrev=False)  # a prefix like --delete must never silently mean --delete-sources
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add(group: Any, name: str, handler: Callable[[Ctx], int], help: str) -> argparse.ArgumentParser:
        p = group.add_parser(name, help=help, description=help, parents=[common], allow_abbrev=False)
        p.set_defaults(func=handler)
        return p

    def group_of(group: Any, name: str, help: str, dest: str) -> Any:
        p = group.add_parser(name, help=help, description=help, parents=[common], allow_abbrev=False)
        return p.add_subparsers(dest=dest, metavar="SUBCOMMAND", required=True)

    p = add(sub, "init", cmd_init, "write a config file with a new API token and create the catalog")
    p.add_argument("--force", action="store_true", help="overwrite an existing config (replaces the token)")

    add(sub, "remotes", cmd_remotes, "list configured remotes")

    p = add(sub, "scan", cmd_scan, "list a remote into the catalog (read-only)")
    p.add_argument("remote", nargs="?", help="remote name from the config")
    p.add_argument("--all", action="store_true", help="scan every configured remote")
    p.add_argument("--root", metavar="PATH", help="only scan below this folder")
    p.add_argument("--force", action="store_true",
                   help="accept a listing that lost most previously seen entries (normally refused as suspicious)")

    ident = group_of(sub, "identity", "content identity", "identity_command")
    add(ident, "rebuild", cmd_identity_rebuild, "regroup identical content")

    p = add(sub, "bridge", cmd_bridge, "download and hash ambiguous files once, then regroup")
    p.add_argument("--max-bytes", type=size_arg, metavar="SIZE", help="download budget, e.g. 5G, 500M (default: unlimited)")
    p.add_argument("--limit", type=int, default=100, help="at most N files (default 100)")

    p = add(sub, "search", cmd_search, "search the catalog by name or path")
    p.add_argument("query", nargs="?", help="words that must appear in the name or path")
    p.add_argument("--remote", help="only this remote")
    p.add_argument("--category", help="e.g. image, document, video")
    p.add_argument("--ext", help="file extension, e.g. pdf")
    p.add_argument("--min-size", type=size_arg, metavar="SIZE")
    p.add_argument("--max-size", type=size_arg, metavar="SIZE")
    p.add_argument("--after", type=date_arg, metavar="DATE", help="modified on or after (YYYY[-MM[-DD]])")
    p.add_argument("--before", type=date_arg, metavar="DATE", help="modified before")
    p.add_argument("--prefix", metavar="P", help="only below this folder")
    p.add_argument("--dirs", action="store_true", help="include folders")
    p.add_argument("--missing", action="store_true", help="include files no longer present")
    p.add_argument("--dups", action="store_true", help="only files with a duplicate")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)

    add(sub, "stats", cmd_stats, "catalog totals per remote and category")

    p = add(sub, "locations", cmd_locations, "every live copy of an entry's content")
    p.add_argument("entry_id", type=int, metavar="ENTRY_ID")

    p = add(sub, "duplicates", cmd_duplicates, "list duplicate groups, most reclaimable space first")
    p.add_argument("--probable", action="store_true", help="name+size candidates instead of proven duplicates")
    p.add_argument("--remote", help="groups with a copy on this remote")
    p.add_argument("--cross-remote", action="store_true", help="only groups spanning several remotes")
    p.add_argument("--min-size", type=size_arg, metavar="SIZE", default=1)
    p.add_argument("--prefix", metavar="P")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)

    p = add(sub, "profiles", cmd_profiles, "folder profiles: what each folder holds")
    p.add_argument("--remote")
    p.add_argument("--prefix", metavar="P")
    p.add_argument("--depth", type=int, help="folder levels below the prefix")
    p.add_argument("--limit", type=int, default=50)

    rules = group_of(sub, "rules", "layout rules", "rules_command")
    p = add(rules, "check", cmd_rules_check, "validate a rules file")
    p.add_argument("file", metavar="FILE")
    p = add(rules, "preview", cmd_rules_preview, "show where rules would put files")
    p.add_argument("--rules", metavar="FILE", help="rules file (default: configured or built-in rules)")
    p.add_argument("--remote", action="append", metavar="R", help="source remote (repeatable; default all)")
    p.add_argument("--target-root", metavar="P", help="folder under which the layout is created")
    p.add_argument("--prefix", metavar="P", help="only files below this folder")
    p.add_argument("--limit", type=int, default=20, help="sample size (default 20)")

    plan = group_of(sub, "plan", "build, inspect and (by a human) approve plans", "plan_command")
    p = add(plan, "dedupe", cmd_plan_dedupe, "propose deleting proven duplicates (to quarantine)")
    p.add_argument("--remote", help="only duplicates with a copy here")
    p.add_argument("--cross-remote", action="store_true", help="also dedupe across remotes")
    p.add_argument("--prefix", metavar="P")
    p.add_argument("--min-size", type=size_arg, metavar="SIZE")
    p.add_argument("--prefer", action="append", metavar="R", help="keep copies on this remote first (repeatable)")
    p.add_argument("--title", metavar="T")

    p = add(plan, "consolidate", cmd_plan_consolidate, "propose gathering content into one remote")
    p.add_argument("--target", required=True, metavar="R", help="remote to gather into")
    p.add_argument("--root", metavar="P", help="folder in the target to gather under")
    p.add_argument("--source", action="append", metavar="R", help="only these source remotes (repeatable)")
    p.add_argument("--delete-sources", action="store_true", help="also delete source copies (to quarantine)")
    p.add_argument("--reorganize-target", action="store_true", help="also move target files into the layout")
    p.add_argument("--rules", metavar="FILE")
    p.add_argument("--prefix", metavar="P")
    p.add_argument("--title", metavar="T")

    p = add(plan, "reorganize", cmd_plan_reorganize, "propose moving files within a remote to the rules' layout")
    p.add_argument("--remote", required=True, metavar="R")
    p.add_argument("--root", metavar="P")
    p.add_argument("--rules", metavar="FILE")
    p.add_argument("--prefix", metavar="P")
    p.add_argument("--title", metavar="T")

    p = add(plan, "manual", cmd_plan_manual, "propose a hand-written list of operations")
    p.add_argument("file", metavar="FILE.json", help="JSON list of op specs")
    p.add_argument("--title", metavar="T")

    p = add(plan, "list", cmd_plan_list, "list plans")
    p.add_argument("--status", metavar="S")
    p = add(plan, "show", cmd_plan_show, "show a plan and its latest simulation")
    p.add_argument("id", type=int, metavar="ID")
    p.add_argument("--ops", type=int, default=20, metavar="N", help="operations to list (default 20)")
    p = add(plan, "simulate", cmd_plan_simulate, "re-run the simulation")
    p.add_argument("id", type=int, metavar="ID")
    p = add(plan, "cancel", cmd_plan_cancel, "cancel a plan that has not run")
    p.add_argument("id", type=int, metavar="ID")

    p = add(plan, "approve", cmd_plan_approve, "approve a simulated plan (human, interactive terminal only)")
    p.add_argument("id", type=int, metavar="ID")
    p = add(plan, "execute", cmd_plan_execute, "run an approved plan (human, interactive terminal only)")
    p.add_argument("id", type=int, metavar="ID")
    p = add(plan, "restore", cmd_plan_restore, "move a plan's quarantined files back (human, interactive terminal only)")
    p.add_argument("id", type=int, metavar="ID")
    p = add(plan, "purge", cmd_plan_purge, "permanently delete a plan's old quarantine (human, interactive terminal only)")
    p.add_argument("id", type=int, metavar="ID")
    p.add_argument("--older-than-days", type=int, default=30, metavar="N", help="default 30")

    p = add(sub, "changes", cmd_changes, "catalog change feed")
    p.add_argument("--cursor", metavar="C", help="'<row_version>:<id>' from a previous call")
    p.add_argument("--limit", type=int, default=100)

    p = add(sub, "serve", cmd_serve, "run the REST API (needs the 'api' extra)")
    p.add_argument("--host", metavar="H")
    p.add_argument("--port", type=int, metavar="P")

    p = add(sub, "ui", cmd_ui, "open the visual workspace in your browser (approvals only from your own terminal)")
    p.add_argument("--host", metavar="H")
    p.add_argument("--port", type=int, metavar="P")
    p.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")

    add(sub, "mcp", cmd_mcp, "run the MCP server on stdio (needs the 'mcp' extra)")

    p = add(sub, "rclone-combine", cmd_rclone_combine, "print an rclone `combine` remote over all configured remotes")
    p.add_argument("--name", default="all", help="name of the combine remote (default: all)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse: --help (0) or a usage error (2)
        return e.code if isinstance(e.code, int) else (EXIT_OK if e.code is None else EXIT_REFUSED)
    ctx = Ctx(args)

    def fail(message: str, code: int = EXIT_ERROR) -> int:
        if ctx.json:
            print(json.dumps({"error": message}, ensure_ascii=False))
        else:
            print(f"drivemerge: {message}", file=sys.stderr)
        return code

    try:
        return int(args.func(ctx) or 0)
    except GateRefused as e:
        return fail(str(e), EXIT_REFUSED)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except BrokenPipeError:  # e.g. `drivemerge search x | head`
        with contextlib.suppress(Exception):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except (ValueError, plans.PlanError, KeyError, OSError, executor.ExecutionError, RcloneError,
            sqlite3.Error) as e:
        return fail(_message(e))


if __name__ == "__main__":
    sys.exit(main())
