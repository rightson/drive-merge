"""Connecting clouds: drive-merge's own rclone config, `connections.toml`, and the sign-in flows.

Files, all mode 600, next to the main config (default `~/.config/drive-merge/`):
  rclone.conf         drive-merge's own rclone config (tokens live here and nowhere else)
  connections.toml    `[[remotes]]` entries written here; `config.load_config` merges them into the config

Nothing in this module logs or returns a token, a password or an answer. A token exists in memory (an `AuthFlow`)
between rclone's `authorize` exiting (or the person pasting one) and the `rclone config create` that stores it; it
is dropped right after. rclone itself gets it, a password and the answers to its questions on its command line for
one call each (rclone has no other way to take them; docs/ONBOARDING.md section 8). Every rclone stderr tail is
scrubbed of them (`_scrub`, `AuthFlow._secrets`) before it becomes an error or a message.

One question loop (`_advance`) serves every path that runs `rclone config create ... --non-interactive`: OAuth
connections (token from `authorize` or pasted), iCloud Drive (Apple ID and password, then a two-factor code) and
imports of remotes from the person's own rclone config. rclone's known questions are answered here; any other
goes to the page as a question (`status == "question"`).

Verified against rclone v1.75.2:
  * `authorize <type> [id secret] --auth-no-open-browser` prints NOTICE lines on stderr, among them
    `Please go to the following link: http://127.0.0.1:53682/auth?state=...`, waits for the browser callback,
    then prints `Paste the following into your remote machine --->`, the token JSON and `<---End paste` on stdout.
  * Backend options reach `authorize` as global flags: `--drive-scope=drive.readonly` and
    `--onedrive-access-scopes=...` change the scope in the sign-in URL.
  * `config create N T token=... k=v --non-interactive` writes the remote and answers with a JSON question
    (`State`, `Option`); it is continued with `--continue --state S --result R`. With a token already in the
    options the first question is "replace the token?" (answer false), then per backend: Drive asks for a Shared
    Drive (false), OneDrive for the connection type (`onedrive`), the drive id (the default) and a confirmation.
    The loop ends with `State == ""`; an `Error` with no `Option` is a failure.
  * `config userinfo remote: --json` works for Drive and fails for Dropbox/OneDrive (account stays None there).
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import tomllib
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config, load_config, provider_of, write_private
from .db import utcnow
from .rclone import Rclone, classify_failure

MIN_RCLONE = (1, 69)
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
# Where rclone's sign-in helper listens; only a URL there is ever stored in a flow or handed to a browser.
_URL_RE = re.compile(r"http://127\.0\.0\.1:53682/auth\?state=[A-Za-z0-9_.~%-]+")
_TOKEN_BLOCK = re.compile(r"--->\s*(\{.*?\})\s*<---End paste", re.S)
DOWNLOAD_BASE = "https://downloads.rclone.org"
URL_WAIT_SECONDS = 15
MANAGED_BIN_DIR = Path("~/.local/share/drive-merge/bin")
TEST_TIMEOUT_S = 30.0        # `lsjson` of a Test
CREATE_TIMEOUT_S = 120       # one `rclone config create` call
QUESTION_TIMEOUT_S = 600.0   # how long a question waits for its answer
MAX_ANSWER = 65536
MAX_TOKEN = 16384
MAX_ROUNDS = 30              # questions rclone may ask in one flow
# Remotes that wrap other remotes or rotate single-use tokens cannot be copied into drive-merge's rclone.conf.
UNSUPPORTED_IMPORT = frozenset({"alias", "crypt", "union", "combine", "chunker", "hasher", "compress", "cache", "box"})
_ENCRYPTED_HEADER = "# Encrypted rclone configuration File"


class ConnectError(ValueError):
    """A connection step failed. `code` is one of NO_RCLONE, RCLONE_TOO_OLD, FLOW_BUSY, FLOW_FAILED, FLOW_TIMEOUT,
    NAME_TAKEN, BAD_NAME, NEEDS_CLIENT, TEST_FAILED, NOT_FOUND, MISSING_FIELD {field}, NOT_WAITING,
    TOKEN_UNREADABLE, CONFIG_WRITE, IMPORT_UNSUPPORTED {type}, IMPORT_ENCRYPTED, BAD_REQUEST. `params` are the
    values the page needs to word the code (never a secret)."""

    def __init__(self, code: str, message: str, **params: str):
        super().__init__(message)
        self.code = code
        self.params = params


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    rclone_type: str
    label: str
    needs_client: bool
    read_options: dict[str, str]
    write_options: dict[str, str]
    extra_args: tuple[str, ...]
    default_name: str = ""
    kind: str = "authorize"          # 'authorize': rclone authorize + token; 'config': rclone config asks (iCloud)
    fields: tuple[str, ...] = ()     # what `start_flow` needs up front for kind 'config'
    scoped_write: bool = True        # False: the read sign-in can already write; Enable changes reuses `read`


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "drive": ProviderSpec(
        "drive", "drive", "Google Drive", True, {"scope": "drive.readonly"}, {"scope": "drive"},
        ("--fast-list", "--drive-skip-shortcuts"), "gdrive"),
    "onedrive": ProviderSpec(
        "onedrive", "onedrive", "OneDrive", False,
        {"access_scopes": "Files.Read Files.Read.All Sites.Read.All offline_access"}, {}, (), "onedrive"),
    "dropbox": ProviderSpec("dropbox", "dropbox", "Dropbox", False, {}, {}, (), "dropbox"),
    "iclouddrive": ProviderSpec("iclouddrive", "iclouddrive", "iCloud Drive", False, {}, {}, (), "icloud",
                                kind="config", fields=("apple_id", "password"), scoped_write=False),
}


# --------------------------------------------------------------------------- rclone binary

def managed_bin_dir() -> Path:
    return MANAGED_BIN_DIR.expanduser()


def _managed_binary() -> Path:
    return managed_bin_dir() / ("rclone.exe" if os.name == "nt" else "rclone")


def resolve_binary(config: Config) -> str | None:
    """The rclone to run: the configured one if it exists (path or on PATH), else the one drive-merge installed."""
    found = shutil.which(config.rclone_binary) if config.rclone_binary else None
    if found:
        return found
    managed = _managed_binary()
    return str(managed) if managed.is_file() else None


def make_rclone(config: Config) -> Rclone:
    """The Rclone every scan, sync and execution uses: the configured binary, else the one drive-merge installed."""
    return Rclone(resolve_binary(config) or config.rclone_binary, config=config.rclone_config,
                  extra_args=config.rclone_extra_args)


def _parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(g) for g in m.groups() if g is not None) if m else None


def rclone_status(config: Config) -> dict:
    """{'found', 'path', 'version', 'ok' (>= 1.69), 'managed' (installed by drive-merge)}."""
    path = resolve_binary(config)
    out = {"found": False, "path": path, "version": None, "ok": False, "managed": False}
    if not path:
        return out
    out["managed"] = Path(path).resolve() == _managed_binary().resolve() if _managed_binary().exists() else False
    try:
        p = subprocess.run([path, "version"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return out
    m = re.search(r"^rclone\s+(\S+)", p.stdout, re.M)
    if p.returncode != 0 or not m:
        return out
    out["found"] = True
    out["version"] = m.group(1)
    v = _parse_version(m.group(1))
    out["ok"] = bool(v and v[:2] >= MIN_RCLONE)
    return out


def _require_rclone(config: Config) -> str:
    st = rclone_status(config)
    if not st["found"]:
        raise ConnectError("NO_RCLONE", "rclone was not found; install it (https://rclone.org/install/) "
                                        "or let drive-merge install it")
    if not st["ok"]:
        raise ConnectError("RCLONE_TOO_OLD", f"rclone {st['version']} is too old; "
                                             f"{'.'.join(map(str, MIN_RCLONE))} or newer is needed")
    return st["path"]


def _fetch(url: str, progress: Callable[[str, int, int | None], None] | None = None, what: str = "") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "drive-merge"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https host
        total = int(r.headers.get("Content-Length") or 0) or None
        buf = io.BytesIO()
        while chunk := r.read(1 << 16):
            buf.write(chunk)
            if progress:
                progress(what, buf.tell(), total)
        return buf.getvalue()


def _platform_tag() -> str:
    system = {"linux": "linux", "darwin": "osx", "win32": "windows", "freebsd": "freebsd"}.get(sys.platform)
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "i386": "386",
            "i686": "386", "armv7l": "arm-v7", "armv6l": "arm-v6"}.get(platform.machine().lower())
    if not system or not arch:
        raise ConnectError("NO_RCLONE", f"no official rclone build for {sys.platform}/{platform.machine()}")
    return f"{system}-{arch}"


def install_rclone(dest_dir: Path, *, progress: Callable[[str, int, int | None], None] | None = None,
                   base_url: str = DOWNLOAD_BASE, fetch: Callable[..., bytes] | None = None) -> Path:
    """Download the official rclone zip for this platform, verify it against the published SHA256SUMS, and
    install the binary into `dest_dir`. A checksum mismatch raises ConnectError and installs nothing.
    `progress(phase, done_bytes, total_bytes_or_None)`; `fetch(url, progress, what)` is replaceable for tests."""
    get = fetch or _fetch
    dest_dir = Path(dest_dir).expanduser()
    tag = _platform_tag()
    try:
        version = get(f"{base_url}/version.txt", None, "version").decode().split()[-1].strip()
        name = f"rclone-{version}-{tag}.zip"
        sums = get(f"{base_url}/{version}/SHA256SUMS", None, "checksums").decode("utf-8", "replace")
        expected = None
        for line in sums.splitlines():
            m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S+)$", line.strip())
            if m and m.group(2) == name:
                expected = m.group(1).lower()
        if not expected:
            raise ConnectError("FLOW_FAILED", f"{name} is not listed in the published SHA256SUMS")
        blob = get(f"{base_url}/{version}/{name}", progress, "download")
    except (OSError, ValueError) as e:
        if isinstance(e, ConnectError):
            raise
        raise ConnectError("FLOW_FAILED", f"could not download rclone: {e}") from e
    if hashlib.sha256(blob).hexdigest() != expected:
        raise ConnectError("FLOW_FAILED", f"checksum mismatch for {name}; nothing was installed")
    exe = "rclone.exe" if tag.startswith("windows") else "rclone"
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            member = next((n for n in z.namelist() if n.rsplit("/", 1)[-1] == exe and not n.endswith("/")), None)
            if member is None:
                raise ConnectError("FLOW_FAILED", f"{name} contains no {exe}")
            data = z.read(member)
    except zipfile.BadZipFile as e:
        raise ConnectError("FLOW_FAILED", f"{name} is not a valid zip: {e}") from e
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / exe
    tmp = dest_dir / f".{exe}.{secrets.token_hex(4)}"
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    if progress:
        progress("done", len(data), len(data))
    return target


# ------------------------------------------------------------------ files: conf and toml

_FILE_LOCK = threading.RLock()


def _conf_path(config: Config) -> Path:
    return Path(config.rclone_config).expanduser() if config.rclone_config else config.own_rclone_conf


def _ensure_private_file(path: Path) -> None:
    """Create `path` (empty, mode 600) if missing, so rclone keeps the mode when it rewrites it."""
    if not path.exists():
        write_private(path, "")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _conf_sections(path: Path) -> dict[str, dict[str, str]]:
    """Section -> options of an rclone.conf, read directly (never through a subprocess that prints tokens)."""
    out: dict[str, dict[str, str]] = {}
    cur = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^\[(.+)\]\s*$", line)
        if m:
            cur = out.setdefault(m.group(1), {})
        elif cur is not None and "=" in line and not line.lstrip().startswith(("#", ";")):
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    return out


def _toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


def _entry_text(e: dict) -> str:
    lines = ["[[remotes]]"]
    for k in ("name", "provider", "read", "write"):
        if e.get(k) is not None:
            lines.append(f"{k} = {_toml_str(e[k])}")
    lines.append("extra_args = [" + ", ".join(_toml_str(a) for a in e.get("extra_args", ())) + "]")
    lines.append("connection = true")
    if e.get("scoped") is False:
        lines.append("scoped = false")
    if e.get("imported_from"):
        lines.append(f"imported_from = {_toml_str(e['imported_from'])}")
    if e.get("account"):
        lines.append(f"account = {_toml_str(e['account'])}")
    lines.append(f"created_at = {_toml_str(e.get('created_at') or utcnow())}")
    return "\n".join(lines)


def _read_entries(config: Config) -> list[dict]:
    p = config.connections_path
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            return [dict(r) for r in tomllib.load(f).get("remotes", [])]
    except tomllib.TOMLDecodeError as e:
        raise ConnectError("FLOW_FAILED", f"{p} is not valid TOML: {e}") from None


def _write_entries(config: Config, entries: list[dict]) -> None:
    header = ("# Written by drive-merge (`drivemerge connect`, the Clouds page). Tokens are not stored here;\n"
              "# they live in rclone.conf next to this file. Safe to edit by hand.\n\n")
    write_private(config.connections_path, header + "\n\n".join(_entry_text(e) for e in entries) + ("\n" if entries else ""))


def _public(e: dict) -> dict:
    return {"name": e["name"], "provider": e.get("provider"), "account": e.get("account"),
            "read": e.get("read"), "write": e.get("write"), "writable": bool(e.get("write")),
            "created_at": e.get("created_at"), "scoped": e.get("scoped") is not False,
            "imported_from": e.get("imported_from") or None}


def list_connections(config: Config) -> list[dict]:
    """Connections made by drive-merge: name, provider, account, read, write, writable, created_at, scoped,
    imported_from. No tokens."""
    return [_public(e) for e in _read_entries(config)]


# ------------------------------------------------------------------------- rclone calls

def _scrub(text: str, *secrets_: str | None) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "<hidden>")
    return re.sub(r'"(access_token|refresh_token|id_token)"\s*:\s*"[^"]*"', r'"\1":"<hidden>"', text)


def _tail(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:400] if lines else "no output"


def _run(config: Config, args: list[str], *, timeout: float = 120) -> tuple[int, str, str]:
    binary = _require_rclone(config)
    conf = _conf_path(config)
    _ensure_private_file(conf)
    try:
        p = subprocess.run([binary, "--config", str(conf), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConnectError("FLOW_TIMEOUT", f"rclone {args[0]} timed out") from None
    except OSError as e:
        raise ConnectError("NO_RCLONE", f"cannot run rclone: {e}") from None
    with contextlib.suppress(OSError):
        os.chmod(conf, 0o600)
    return p.returncode, p.stdout, p.stderr


def _delete_remote(config: Config, remote: str) -> None:
    if remote in _conf_sections(_conf_path(config)):
        _run(config, ["config", "delete", remote])
    # rclone could not be reached or failed: make sure the section is gone anyway
    if remote in _conf_sections(_conf_path(config)):
        raise ConnectError("FLOW_FAILED", f"could not remove rclone remote {remote!r}")


def _rclone(config: Config) -> Rclone:
    return Rclone(_require_rclone(config), config=str(_conf_path(config)), extra_args=config.rclone_extra_args)


_ACCOUNT_KEYS = ("email", "emailAddress", "mail", "userPrincipalName", "user", "displayName", "name")


def _account(config: Config, remote: str) -> str | None:
    """Best effort, from `rclone config userinfo`; None when the backend has none."""
    try:
        rc, out, _ = _run(config, ["config", "userinfo", remote, "--json"], timeout=30)
        if rc != 0:
            return None
        data = json.loads(out)
    except (ConnectError, ValueError):
        return None
    flat = dict(data) if isinstance(data, dict) else {}
    for v in list(flat.values()):
        if isinstance(v, dict):
            flat.update({k: x for k, x in v.items() if k not in flat})
    for k in _ACCOUNT_KEYS:
        if isinstance(flat.get(k), str) and flat[k]:
            return flat[k]
    return None


def _test_remote(config: Config, remote: str, *secrets_: str | None) -> dict:
    """List the top level (30 s) and ask for the quota. On failure `code` is rclone.classify_failure's."""
    try:
        rc, _out, err = _run(config, ["lsjson", "--max-depth", "1", remote], timeout=TEST_TIMEOUT_S)
    except ConnectError as e:
        if e.code != "FLOW_TIMEOUT":
            raise
        rc, err = -1, f"rclone lsjson timed out after {TEST_TIMEOUT_S:g} s"
    if rc != 0:
        tail = _scrub(_tail(err), *secrets_)
        code = classify_failure(rc, err)
        return {"ok": False, "account": None, "quota": None, "error": tail, "code": code,
                "params": {"detail": tail[:300]} if code == "RCLONE_ERROR" else {}}
    quota = None
    try:
        about = _rclone(config).about(remote)
    except Exception:  # noqa: BLE001 - a quota is a nicety; RcloneError, timeouts, odd output
        about = None
    if about:
        quota = {"used": about.get("used"), "total": about.get("total"), "free": about.get("free")}
    return {"ok": True, "account": _account(config, remote), "quota": quota, "error": None, "code": None,
            "params": {}}


def _entry(config: Config, name: str) -> dict:
    for e in _read_entries(config):
        if e.get("name") == name:
            return e
    raise ConnectError("NOT_FOUND", f"no connection named {name!r}")


def test_connection(config: Config, name: str) -> dict:
    """List the top level and ask for the quota. A connection, or a remote of config.toml (its `read` path).

    {'ok', 'account', 'quota': {...} | None, 'error', 'code', 'params'}; on failure `code` is AUTH_EXPIRED,
    NETWORK, NOT_FOUND or RCLONE_ERROR (params.detail), `error` the scrubbed last line of rclone's stderr."""
    entry = next((e for e in _read_entries(config) if e.get("name") == name), None)
    if entry is not None:
        remote = entry["read"]
    else:
        manual = next((r for r in config.remotes if r.name == name), None)
        if manual is None:
            raise ConnectError("NOT_FOUND", f"no connection or remote named {name!r}")
        remote = manual.read
    return _test_remote(config, remote)


test_connection.__test__ = False  # not a pytest test


# ------------------------------------------------------------------------ questions

def question_view(opt: dict) -> dict:
    """The page's view of one rclone question. Only what the form needs; nothing the person typed."""
    examples = opt.get("Examples") or []
    return {
        "name": str(opt.get("Name") or ""),
        "help": str(opt.get("Help") or "")[:4000],
        "default": opt.get("Default") if isinstance(opt.get("Default"), (str, int, float, bool)) else "",
        "examples": [{"value": str(e.get("Value", "")), "help": str(e.get("Help", ""))[:500]}
                     for e in examples[:200] if isinstance(e, dict)],
        "password": bool(opt.get("IsPassword")),
        "required": bool(opt.get("Required")),
        "exclusive": bool(opt.get("Exclusive")),
        "type": str(opt.get("Type") or "string"),
    }


def _auto_answer(opt: dict) -> str | None:
    """The answer drive-merge gives itself to a question rclone is known to ask; None for the person's questions."""
    name = opt.get("Name")
    if name in ("config_refresh_token", "config_change_team_drive"):
        return "false"
    if name == "config_type":
        return "onedrive"
    if name == "config_drive_ok":
        return "true"
    if name == "config_driveid":
        examples = [e for e in (opt.get("Examples") or []) if isinstance(e, dict)]
        if len(examples) > 1:
            return None
        result = str(opt.get("DefaultStr") or opt.get("Default") or "")
        if not result and examples:
            result = str(examples[0].get("Value", ""))
        return result
    return None


# --------------------------------------------------------------------------- sign-in flows

class AuthFlow:
    """One connection in progress: an rclone `authorize` subprocess, a pasted token, an iCloud sign-in or an
    import. `public()` is what may leave this module; no token, password, answer or secret ever does."""

    def __init__(self, provider: str, purpose: str):
        self.id = secrets.token_urlsafe(12)
        self.provider = provider
        self.purpose = purpose
        # waiting (rclone authorize runs) | succeeded (token in hand, finish next) | question | working
        # | done | failed | cancelled | timeout
        self.status = "waiting"
        self.auth_url: str | None = None
        self.error: str | None = None
        self.code: str | None = None
        self.params: dict = {}
        self.name: str | None = None            # reserved connection name once known
        self.question: dict | None = None       # question_view(...) while status == 'question'
        self.retry = False                      # rclone refused the previous answer (its text is never forwarded)
        self.connection: dict | None = None     # the public entry once status == 'done'
        self._token: str | None = None
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._secrets: list[str] = []           # every value to scrub from rclone's output
        self._state: str | None = None          # rclone's State while a question waits
        self._remote: str | None = None         # the rclone remote this flow created and must remove on failure
        self._rtype: str = provider
        self._config: Config | None = None
        self._wanted: str | None = None         # the name asked for at start (authorize flows pick it at finish)
        self._reuse_read = False                # Enable changes without a second sign-in
        self._extra: dict = {}                  # entry keys of an import (scoped, imported_from)
        self._timer: threading.Timer | None = None
        self._proc: subprocess.Popen | None = None
        self._url_or_done = threading.Event()
        self._finished = threading.Event()

    def public(self) -> dict:
        return {"id": self.id, "provider": self.provider, "purpose": self.purpose, "status": self.status,
                "auth_url": self.auth_url, "error": self.error, "code": self.code, "params": dict(self.params),
                "name": self.name, "question": dict(self.question) if self.question else None,
                "retry": self.retry, "connection": dict(self.connection) if self.connection else None}

    def __repr__(self) -> str:
        return f"AuthFlow(id={self.id!r}, provider={self.provider!r}, purpose={self.purpose!r}, status={self.status!r})"

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the flow is no longer waiting (for CLI and tests); False on timeout."""
        return self._finished.wait(timeout)


_FLOWS: dict[str, AuthFlow] = {}
_RESERVED: dict[str, str] = {}      # connection name -> id of the live flow that holds it
_FLOWS_LOCK = threading.Lock()
_BUSY = ("waiting", "question", "working")
_ENDED = ("done", "failed", "cancelled", "timeout")


def _forget_old_flows() -> None:
    old = [f for f in _FLOWS.values() if f.status not in _BUSY]
    for f in old[:-20]:
        _FLOWS.pop(f.id, None)


def _register(flow: AuthFlow, config: Config) -> None:
    flow._config = config
    with _FLOWS_LOCK:
        if any(f.status in _BUSY for f in _FLOWS.values()):
            raise ConnectError("FLOW_BUSY", "another sign-in is already in progress; finish or cancel it first")
        _FLOWS[flow.id] = flow
        _forget_old_flows()


def _release(flow: AuthFlow) -> None:
    with _FLOWS_LOCK:
        for n in [n for n, fid in _RESERVED.items() if fid == flow.id]:
            del _RESERVED[n]


def _clear_private(flow: AuthFlow) -> None:
    flow._token = flow._client_secret = flow._state = None
    flow._secrets.clear()
    if flow._timer is not None:
        flow._timer.cancel()
        flow._timer = None


def _end_locked(flow: AuthFlow, status: str, *, code: str | None = None, message: str | None = None,
                params: dict | None = None) -> None:
    flow.status, flow.code, flow.error, flow.params = status, code, message, dict(params or {})
    flow.question, flow.retry = None, False
    _clear_private(flow)
    flow._url_or_done.set()
    flow._finished.set()


def _fail(flow: AuthFlow, exc: BaseException) -> None:
    """The flow ended badly: remove the rclone remote it made, record a coded failure, release the name."""
    if isinstance(exc, ConnectError):
        code, message, params = exc.code, str(exc), exc.params
    else:
        code, message, params = "FLOW_FAILED", f"unexpected {type(exc).__name__}", {}
    remote, config = flow._remote, flow._config
    if remote and config is not None:
        with contextlib.suppress(Exception), _FILE_LOCK:
            _delete_remote(config, remote)
    with _FLOWS_LOCK:
        if flow.status not in _ENDED:
            _end_locked(flow, "failed", code=code, message=message, params=params)
    _release(flow)


def _expire(flow: AuthFlow) -> None:
    """Timer: nobody answered the question in time. Delete what the flow made and free its name; the flow only
    reads `timeout` once that is done (meanwhile it is `working`, so no answer can slip in)."""
    with _FLOWS_LOCK:
        if flow.status != "question":
            return
        flow.status, flow._state = "working", None
    remote, config = flow._remote, flow._config
    if remote and config is not None:
        with contextlib.suppress(Exception), _FILE_LOCK:
            _delete_remote(config, remote)
    with _FLOWS_LOCK:
        _end_locked(flow, "timeout", code="FLOW_TIMEOUT", message="the question was not answered in time")
    _release(flow)


def _arm(flow: AuthFlow) -> None:
    if flow._timer is not None:
        flow._timer.cancel()
    timer = threading.Timer(QUESTION_TIMEOUT_S, _expire, args=(flow,))
    timer.daemon = True
    flow._timer = timer
    timer.start()


def _watch(flow: AuthFlow, proc: subprocess.Popen, timeout: float) -> None:
    stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
    out_chunks: list[str] = []

    def read_err() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_tail.append(line.rstrip("\n"))
            if flow.auth_url is None:
                m = _URL_RE.search(line)
                if m:
                    flow.auth_url = m.group(0)
                    flow._url_or_done.set()

    def read_out() -> None:
        out_chunks.append(proc.stdout.read())  # type: ignore[union-attr]

    threads = [threading.Thread(target=read_err, daemon=True), threading.Thread(target=read_out, daemon=True)]
    for t in threads:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    for t in threads:
        t.join(5)
    out = "".join(out_chunks)
    with _FLOWS_LOCK:
        if flow.status != "waiting":
            pass                                     # cancelled meanwhile
        elif timed_out:
            _end_locked(flow, "timeout", code="FLOW_TIMEOUT", message="the sign-in was not completed in time")
        elif proc.returncode == 0:
            token = _parse_token(out)
            if token:
                flow._token, flow.status = token, "succeeded"
                flow._secrets.append(token)
            else:
                _end_locked(flow, "failed", code="FLOW_FAILED", message="rclone did not return a token")
        else:
            lines = [ln for ln in stderr_tail if ln.strip() and "NOTICE" not in ln]
            message = _scrub(_tail("\n".join(lines or list(stderr_tail))), *flow._secrets)
            _end_locked(flow, "failed", code="FLOW_FAILED", message=message)
    flow._url_or_done.set()
    flow._finished.set()


def _parse_token(out: str) -> str | None:
    m = _TOKEN_BLOCK.search(out)
    candidates = [m.group(1)] if m else [out.strip(), *[ln for ln in out.splitlines() if ln.strip().startswith("{")][-1:]]
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj:
            return json.dumps(obj, separators=(",", ":"))
    return None


def _spec(provider: str) -> ProviderSpec:
    try:
        return PROVIDER_SPECS[provider]
    except KeyError:
        raise ConnectError("NOT_FOUND", f"unknown provider {provider!r}; use one of "
                                        f"{', '.join(PROVIDER_SPECS)}") from None


def authorize_command(provider: str, purpose: str, client_id: str | None = None) -> list[str]:
    """The command to run on another computer that has a browser, as an argv list: `rclone authorize <type>`, the
    client pair when given (the secret is a placeholder the person types), and the purpose's scope flags."""
    spec = _spec(provider)
    if purpose not in ("read", "write"):
        raise ConnectError("BAD_REQUEST", f"unknown purpose {purpose!r}")
    if spec.kind != "authorize":
        raise ConnectError("BAD_REQUEST", f"{spec.label} is not connected with rclone authorize")
    argv = ["rclone", "authorize", spec.rclone_type]
    if client_id:
        argv += [client_id, "CLIENT_SECRET"]
    options = spec.read_options if purpose == "read" else spec.write_options
    argv += [f"--{spec.rclone_type}-{k.replace('_', '-')}={v}" for k, v in options.items()]
    return argv


def _check_fields(spec: ProviderSpec, purpose: str, client_id, client_secret, apple_id, password) -> None:
    if spec.kind == "config":
        for f in spec.fields:
            if not {"apple_id": apple_id, "password": password}.get(f):
                raise ConnectError("MISSING_FIELD", f"{spec.label} needs {f.replace('_', ' ')}", field=f)
    elif apple_id or password:
        raise ConnectError("BAD_REQUEST", "apple_id and password apply to iCloud Drive only")
    if (client_id or client_secret) and spec.key not in ("drive", "dropbox"):
        raise ConnectError("BAD_REQUEST", f"client_id and client_secret do not apply to {spec.label}")


def _client_for(config: Config, purpose: str, name: str | None, client_id, client_secret):
    """The Drive/Dropbox OAuth client: given, else (for a write sign-in) the one the connection's `-ro` uses."""
    if purpose == "write" and name and not (client_id and client_secret):
        sec = _conf_sections(_conf_path(config)).get(f"{name}-ro", {})
        client_id, client_secret = client_id or sec.get("client_id"), client_secret or sec.get("client_secret")
    return client_id or None, client_secret or None


def _write_reuses_read(config: Config, provider: str, name: str | None) -> bool:
    if not name:
        return False
    entry = next((e for e in _read_entries(config) if e.get("name") == name), None)
    if entry is not None and (entry.get("scoped") is False or entry.get("imported_from")):
        return True
    spec = PROVIDER_SPECS.get(provider)
    return bool(spec and not spec.scoped_write)


def _secret_list(*values: str | None) -> list[str]:
    return [v for v in values if v]


def start_flow(config: Config, provider: str, *, purpose: str = "read", name: str | None = None,
               client_id: str | None = None, client_secret: str | None = None, apple_id: str | None = None,
               password: str | None = None, timeout: float = 600, url_wait: float = URL_WAIT_SECONDS) -> AuthFlow:
    """Start a connection. One flow at a time (FLOW_BUSY).

    kind 'authorize' (Drive, OneDrive, Dropbox): run `rclone authorize` and return once its sign-in URL is known.
    kind 'config' (iCloud): create the remote with the Apple ID and password, and return the flow in `question`
    (the two-factor code) or `done`. `name` is the new connection's name (BAD_NAME / NAME_TAKEN at once) or, for
    purpose 'write', the connection to enable changes on. A write flow of a connection whose sign-in cannot be
    scoped (iCloud, an import) returns already `succeeded`, with no process: finishing it reuses `read`."""
    if purpose not in ("read", "write"):
        raise ConnectError("BAD_REQUEST", f"unknown purpose {purpose!r}")
    binary = _require_rclone(config)
    if purpose == "write" and _write_reuses_read(config, provider, name):
        flow = AuthFlow(provider, purpose)
        flow._reuse_read, flow.name, flow.status = True, name, "succeeded"
        _register(flow, config)
        return flow
    spec = _spec(provider)
    if spec.kind == "config" and purpose != "read":
        raise ConnectError("BAD_REQUEST", f"{spec.label} has no separate write sign-in; name the connection")
    _check_fields(spec, purpose, client_id, client_secret, apple_id, password)
    client_id, client_secret = _client_for(config, purpose, name, client_id, client_secret)
    if bool(client_id) != bool(client_secret):
        raise ConnectError("NEEDS_CLIENT", "give both the client id and the client secret")
    if spec.needs_client and not (client_id and client_secret):
        raise ConnectError("NEEDS_CLIENT", f"{spec.label} needs your own OAuth client id and secret")
    flow = AuthFlow(provider, purpose)
    flow._rtype = spec.rclone_type
    flow._client_id, flow._client_secret = client_id, client_secret
    flow._secrets = _secret_list(client_secret, apple_id, password)
    if spec.kind == "config":
        return _start_config_flow(config, flow, spec, name, apple_id or "", password or "")
    if purpose == "read" and name:
        _check_free(config, name)                       # fail before the person signs in, not after
        flow._wanted = name
    argv = [binary, "--config", str(_conf_path(config)), "authorize", spec.rclone_type]
    if client_id and client_secret:
        argv += [client_id, client_secret]
    options = spec.read_options if purpose == "read" else spec.write_options
    argv += [f"--{spec.rclone_type}-{k.replace('_', '-')}={v}" for k, v in options.items()]
    argv.append("--auth-no-open-browser")
    _register(flow, config)
    _ensure_private_file(_conf_path(config))
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        with _FLOWS_LOCK:
            _end_locked(flow, "failed", code="NO_RCLONE", message=f"cannot run rclone: {e}")
        raise ConnectError("NO_RCLONE", flow.error or "cannot run rclone") from None
    flow._proc = proc
    threading.Thread(target=_watch, args=(flow, proc, timeout), daemon=True, name=f"auth-{flow.id}").start()
    flow._url_or_done.wait(url_wait)
    if flow.auth_url is None:
        if flow.status == "waiting":
            cancel_flow(flow.id)
            raise ConnectError("FLOW_FAILED", "rclone did not show a sign-in link in time")
        flow._finished.wait(2)
        raise ConnectError("FLOW_FAILED", flow.error or "rclone stopped before showing a sign-in link")
    return flow


def _start_config_flow(config: Config, flow: AuthFlow, spec: ProviderSpec, name: str | None,
                       apple_id: str, password: str) -> AuthFlow:
    _register(flow, config)
    try:
        with _FILE_LOCK:
            flow.name = _claim_name(config, flow, spec, name, expect="waiting")
            flow._remote = f"{flow.name}-ro"
            out = _cfg(config, flow, ["config", "create", flow._remote, spec.rclone_type,
                                      f"apple_id={apple_id}", f"password={password}", "--non-interactive"])
            _forget_credentials(flow)
            _next(config, flow, out)
    except BaseException as e:
        _fail(flow, e)
        raise
    return flow


def _forget_credentials(flow: AuthFlow) -> None:
    """The token and client secret were handed to rclone; keep them only as strings to scrub."""
    flow._token = flow._client_secret = None


def paste_token(config: Config, provider: str, token: str, *, purpose: str = "read", name: str | None = None,
                client_id: str | None = None, client_secret: str | None = None) -> AuthFlow:
    """A token made with `rclone authorize` on another computer. Returns a flow already `succeeded` (no process);
    finish it as after a browser sign-in. The token is never quoted back, not even in an error."""
    if purpose not in ("read", "write"):
        raise ConnectError("BAD_REQUEST", f"unknown purpose {purpose!r}")
    spec = _spec(provider)
    if spec.kind != "authorize":
        raise ConnectError("BAD_REQUEST", f"{spec.label} is not connected with a pasted token")
    _check_fields(spec, purpose, client_id, client_secret, None, None)
    client_id, client_secret = _client_for(config, purpose, name, client_id, client_secret)
    if bool(client_id) != bool(client_secret):
        raise ConnectError("NEEDS_CLIENT", "give both the client id and the client secret")
    if spec.needs_client and not (client_id and client_secret):
        raise ConnectError("NEEDS_CLIENT", f"{spec.label} needs your own OAuth client id and secret "
                                           "(the token is tied to the client that made it)")
    parsed = _parse_token(token) if len(token) <= MAX_TOKEN else None
    obj = json.loads(parsed) if parsed else {}
    if not (obj.get("access_token") or obj.get("refresh_token")):
        raise ConnectError("TOKEN_UNREADABLE", "that does not look like the output of `rclone authorize`; paste the "
                                               "block between --->  and <---End paste, or the JSON in it")
    if purpose == "read" and name:
        _check_free(config, name)
    flow = AuthFlow(provider, purpose)
    flow._rtype = spec.rclone_type
    flow._config = config
    flow._token, flow.status = parsed, "succeeded"
    flow._client_id, flow._client_secret = client_id, client_secret
    flow._secrets = _secret_list(parsed, token.strip(), client_secret)
    flow._wanted = name if purpose == "read" else None
    if purpose == "write":
        flow.name = name
    with _FLOWS_LOCK:
        _FLOWS[flow.id] = flow
        _forget_old_flows()
    return flow


def get_flow(flow_id: str) -> AuthFlow:
    with _FLOWS_LOCK:
        f = _FLOWS.get(flow_id)
    if f is None:
        raise ConnectError("NOT_FOUND", "unknown or expired sign-in")
    return f


def cancel_flow(flow_id: str) -> None:
    """Stop a flow that is waiting for the browser, asking a question or holding a token. Deletes the rclone
    remote a question flow already made. Idempotent; a flow that is busy with an rclone call is left to end."""
    flow = get_flow(flow_id)
    with _FLOWS_LOCK:
        if flow.status not in ("waiting", "question", "succeeded"):
            return
        proc = flow._proc
        _end_locked(flow, "cancelled")
    if proc is not None and proc.poll() is None:
        proc.kill()
    remote, config = flow._remote, flow._config
    if remote and config is not None:
        with contextlib.suppress(Exception), _FILE_LOCK:
            _delete_remote(config, remote)
    _release(flow)


# ---------------------------------------------------------------------- names

def _all_names(config: Config) -> set[str]:
    names = {r.name for r in config.remotes} | {e.get("name") for e in _read_entries(config)}
    if config.config_path and Path(config.config_path).exists():
        try:
            names |= {r.name for r in load_config(config.config_path).remotes}
        except (OSError, ValueError):
            pass
    return names


def _taken_locked(config: Config, name: str, ignore: str | None = None) -> bool:
    names = _all_names(config)
    secs = set(_conf_sections(_conf_path(config)))
    reserved = {n for n, fid in _RESERVED.items() if fid != ignore}
    mine = {name, f"{name}-ro", f"{name}-rw"}
    return bool(name in names or name in reserved or mine & secs or {f"{name}-ro", f"{name}-rw"} & names)


def _pick_locked(config: Config, spec: ProviderSpec, name: str | None, ignore: str | None = None) -> str:
    if name:
        if not NAME_RE.match(name):
            raise ConnectError("BAD_NAME", "a name starts with a lowercase letter and uses lowercase letters, "
                                           "digits and '-' (at most 31 characters)")
        if _taken_locked(config, name, ignore):
            raise ConnectError("NAME_TAKEN", f"{name!r} is already in use")
        return name
    n = 1
    while True:
        cand = spec.default_name if n == 1 else f"{spec.default_name}-{n}"
        if not _taken_locked(config, cand, ignore):
            return cand
        n += 1


def _check_free(config: Config, name: str) -> None:
    with _FLOWS_LOCK:
        _pick_locked(config, PROVIDER_SPECS["dropbox"], name)


def check_name(config: Config, provider: str, name: str | None) -> str:
    """The name a new connection would get (BAD_NAME / NAME_TAKEN when `name` is unusable); for early checks."""
    spec = _spec(provider)
    with _FLOWS_LOCK:
        return _pick_locked(config, spec, name)


def _claim_name(config: Config, flow: AuthFlow, spec: ProviderSpec | None, name: str | None, *,
                expect: str) -> str:
    """Pick the flow's connection name and reserve it, atomically with the flow's change to `working`.
    BAD_NAME / NAME_TAKEN leave the flow as it was (a person can pick another name)."""
    with _FLOWS_LOCK:
        if flow.status != expect:
            raise ConnectError("FLOW_FAILED", f"the sign-in is {flow.status}, not {expect}")
        for n in [n for n, fid in _RESERVED.items() if fid == flow.id]:
            del _RESERVED[n]
        picked = _pick_locked(config, spec or PROVIDER_SPECS["dropbox"], name, flow.id)
        _RESERVED[picked] = flow.id
        flow.status = "working"
        return picked


# ------------------------------------------------------------------ the question loop

def _cfg(config: Config, flow: AuthFlow, args: list[str], *, timeout: float = CREATE_TIMEOUT_S) -> dict:
    """One `rclone config create` call and its JSON answer. Everything rclone says is scrubbed of the flow's
    secrets before it can reach an error."""
    rc, out, err = _run(config, args, timeout=timeout)
    if rc != 0:
        raise ConnectError("FLOW_FAILED", f"rclone could not create the remote: {_scrub(_tail(err), *flow._secrets)}")
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        raise ConnectError("FLOW_FAILED", "rclone gave an unreadable answer while creating the remote") from None
    if not isinstance(data, dict):
        raise ConnectError("FLOW_FAILED", "rclone gave an unexpected answer while creating the remote")
    return data


def question_result(flow: AuthFlow) -> dict:
    """`{"flow_id", "status": "question", "question", "retry"}`: what the page and the terminal get while a
    flow waits for the person. A connection never has a `flow_id` key, which is how callers tell them apart."""
    return {"flow_id": flow.id, "status": "question", "question": dict(flow.question or {}), "retry": flow.retry}


def flow_result(flow: AuthFlow) -> dict:
    """What a flow that has just been started (iCloud) or advanced came to: the connection, or the question."""
    if flow.status == "done" and flow.connection:
        return dict(flow.connection)
    if flow.status == "question":
        return question_result(flow)
    raise ConnectError("FLOW_FAILED", flow.error or f"the sign-in is {flow.status}")


def _advance(config: Config, flow: AuthFlow, out: dict) -> dict | None:
    """Walk rclone's questions from its last answer `out`. Known questions are answered here; any other sets
    the flow to `question` and returns the question result. None: rclone is done."""
    rtype = flow._rtype
    for _ in range(MAX_ROUNDS):
        error = str(out.get("Error") or "")
        opt = out.get("Option") or {}
        if error and not opt:
            raise ConnectError("FLOW_FAILED", f"rclone: {_scrub(error, *flow._secrets)[:400]}")
        if not out.get("State"):
            return None
        if not opt:
            raise ConnectError("FLOW_FAILED", "rclone asked for something it did not describe")
        answer = _auto_answer(opt)
        if answer is None:
            with _FLOWS_LOCK:
                flow.status, flow.question, flow.retry = "question", question_view(opt), bool(error)
                flow._state = str(out["State"])
            _arm(flow)
            return question_result(flow)
        if error:
            raise ConnectError("FLOW_FAILED", f"rclone: {_scrub(error, *flow._secrets)[:400]}")
        out = _cfg(config, flow, ["config", "create", flow._remote or "", rtype, "--non-interactive",
                                  "--continue", "--state", str(out["State"]), "--result", answer])
    raise ConnectError("FLOW_FAILED", "rclone kept asking questions while creating the remote")


def _next(config: Config, flow: AuthFlow, out: dict) -> dict:
    """After an rclone answer: another question, or the connection once rclone is done."""
    return _advance(config, flow, out) or _complete(config, flow)


def _complete(config: Config, flow: AuthFlow) -> dict:
    """rclone is done: test the remote and record the connection (or its write remote)."""
    remote = flow._remote or ""
    res = _test_remote(config, f"{remote}:", *flow._secrets)
    label = "the new connection" if flow.purpose == "read" else "the write connection"
    if not res["ok"]:
        raise ConnectError("TEST_FAILED", f"{label} does not work: {res['error']}")
    with _FILE_LOCK:
        entries = _read_entries(config)
        if flow.purpose == "read":
            if flow.name in _all_names(config):
                raise ConnectError("NAME_TAKEN", f"{flow.name!r} was taken meanwhile")
            spec = PROVIDER_SPECS.get(flow.provider)
            extra = {"scoped": False} if spec and not spec.scoped_write else {}
            entry = {"name": flow.name, "provider": flow.provider, "read": f"{remote}:", "write": None,
                     "extra_args": list(spec.extra_args) if spec else [], "account": res["account"],
                     "created_at": utcnow(), **extra, **flow._extra}
            entries.append(entry)
        else:
            entry = next((e for e in entries if e.get("name") == flow.name), None)
            if entry is None:
                raise ConnectError("NOT_FOUND", f"no connection named {flow.name!r}")
            entry["write"] = f"{remote}:"
            entry["account"] = entry.get("account") or res["account"]
        _store(config, entries)
    return _done(flow, entry)


def _store(config: Config, entries: list[dict]) -> None:
    try:
        _write_entries(config, entries)
    except ConnectError:
        raise
    except Exception:  # noqa: BLE001 - disk full, permissions, a read-only home ...
        raise ConnectError("CONFIG_WRITE", f"{config.connections_path.name} could not be written") from None


def _done(flow: AuthFlow, entry: dict) -> dict:
    public = _public(entry)
    with _FLOWS_LOCK:
        _end_locked(flow, "done")
        flow.connection = public
    _release(flow)
    return dict(public)


def answer_flow(config: Config, flow_id: str, result: str) -> dict:
    """The person's answer to the question a flow is waiting on. Returns the connection once rclone is done, or
    the next question (a wrong answer asks again with `retry`). The answer is passed to rclone for this one call
    and then dropped; it is scrubbed from everything rclone says back."""
    flow = get_flow(flow_id)
    if not isinstance(result, str) or len(result) > MAX_ANSWER:
        raise ConnectError("BAD_REQUEST", f"an answer is at most {MAX_ANSWER} characters")
    with _FLOWS_LOCK:
        if flow.status != "question" or flow._state is None:
            raise ConnectError("NOT_WAITING", "this connection is not waiting for an answer")
        state, password = flow._state, bool((flow.question or {}).get("password"))
        flow.status, flow.question, flow.retry, flow._state = "working", None, False, None
        if flow._timer is not None:
            flow._timer.cancel()
            flow._timer = None
        added = bool(result) and (password or len(result) >= 3)
        if added:
            flow._secrets.append(result)
    try:
        with _FILE_LOCK:
            out = _cfg(config, flow, ["config", "create", flow._remote or "", flow._rtype, "--non-interactive",
                                      "--continue", "--state", state, "--result", result])
            res = _next(config, flow, out)
    except BaseException as e:
        _fail(flow, e)
        raise
    finally:
        with _FLOWS_LOCK:
            if added and result in flow._secrets:
                flow._secrets.remove(result)
    return res


# ---------------------------------------------------------------------- finishing a flow

def _flow_ready(flow_id: str, purpose: str) -> AuthFlow:
    flow = get_flow(flow_id)
    if flow.purpose != purpose:
        raise ConnectError("FLOW_FAILED", f"this sign-in is for purpose {flow.purpose!r}, not {purpose!r}")
    if flow.status == "waiting":
        raise ConnectError("FLOW_FAILED", "the sign-in is not finished yet")
    if flow.status in ("question", "working"):
        raise ConnectError("FLOW_FAILED", f"the sign-in is {flow.status}")
    if flow.status in ("failed", "timeout") and flow.code in ("FLOW_TIMEOUT", "FLOW_FAILED", "NO_RCLONE"):
        raise ConnectError(flow.code, flow.error or f"the sign-in {flow.status}")
    if flow.status not in ("succeeded", "done"):
        raise ConnectError("FLOW_TIMEOUT" if flow.status == "timeout" else "FLOW_FAILED",
                           flow.error or f"the sign-in {flow.status}")
    if flow.status == "succeeded" and not (flow._token or flow._reuse_read):
        raise ConnectError("FLOW_FAILED", "the sign-in has no token any more; sign in again")
    return flow


def finish_connect(config: Config, flow_id: str, *, name: str | None = None) -> dict:
    """Create `<name>-ro` from a finished read flow, walk rclone's questions, test it and record the connection.
    Returns the connection, or a question result (`flow_id` in it) when rclone asks the person something."""
    flow = _flow_ready(flow_id, "read")
    if flow.status == "done" and flow.connection:
        return dict(flow.connection)
    spec = _spec(flow.provider)
    if spec.kind != "authorize":
        raise ConnectError("FLOW_FAILED", f"{spec.label} is connected with its own questions, not finish")
    with _FILE_LOCK:
        flow.name = _claim_name(config, flow, spec, name or flow._wanted, expect="succeeded")
        remote = flow._remote = f"{flow.name}-ro"
        options = dict(spec.read_options)
        if flow._client_id and flow._client_secret:
            options.update(client_id=flow._client_id, client_secret=flow._client_secret)
        try:
            kv = [f"token={flow._token}", *[f"{k}={v}" for k, v in options.items()]]
            out = _cfg(config, flow, ["config", "create", remote, spec.rclone_type, *kv, "--non-interactive"])
            _forget_credentials(flow)
            return _next(config, flow, out)
        except BaseException as e:
            _fail(flow, e)
            raise


def finish_enable_writes(config: Config, flow_id: str, name: str) -> dict:
    """Create `<name>-rw` from a finished write flow and set it as the connection's write remote. For a
    connection that cannot be scoped (iCloud, an import) `read` itself becomes the write remote.
    Returns the connection, or a question result."""
    flow = _flow_ready(flow_id, "write")
    if flow.status == "done" and flow.connection and flow.connection.get("name") == name:
        return dict(flow.connection)
    if flow.status == "done":
        raise ConnectError("FLOW_FAILED", f"this sign-in already enabled changes on {flow.name!r}, not {name!r}")
    with _FILE_LOCK:
        entries = _read_entries(config)
        entry = next((e for e in entries if e.get("name") == name), None)
        if entry is None:
            raise ConnectError("NOT_FOUND", f"no connection named {name!r}")
        if flow._reuse_read:
            with _FLOWS_LOCK:
                if flow.status != "succeeded":
                    raise ConnectError("FLOW_FAILED", f"the sign-in is {flow.status}")
                flow.status = "working"
            try:
                entry["write"] = entry["read"]
                _store(config, entries)
            except BaseException as e:
                _fail(flow, e)
                raise
            flow.name = name
            return _done(flow, entry)
        if entry.get("provider") != flow.provider:
            raise ConnectError("FLOW_FAILED", f"this sign-in is for {flow.provider}, but {name!r} is {entry.get('provider')}")
        spec = _spec(flow.provider)
        remote = f"{name}-rw"
        options = dict(spec.write_options)
        sec = _conf_sections(_conf_path(config)).get(f"{name}-ro", {})
        cid, csec = flow._client_id or sec.get("client_id"), flow._client_secret or sec.get("client_secret")
        if cid and csec:
            options.update(client_id=cid, client_secret=csec)
            flow._secrets.append(csec)
        with _FLOWS_LOCK:
            if flow.status != "succeeded":
                raise ConnectError("FLOW_FAILED", f"the sign-in is {flow.status}")
            flow.status = "working"
        flow.name = name
        try:
            _delete_remote(config, remote)
            flow._remote = remote
            kv = [f"token={flow._token}", *[f"{k}={v}" for k, v in options.items()]]
            out = _cfg(config, flow, ["config", "create", remote, spec.rclone_type, *kv, "--non-interactive"])
            _forget_credentials(flow)
            return _next(config, flow, out)
        except BaseException as e:
            _fail(flow, e)
            raise


def disconnect(config: Config, name: str) -> None:
    """Remove the rclone remotes the entry's `read` and `write` name (when they are sections of drive-merge's own
    rclone.conf) and the entry from connections.toml. The person's own rclone config is never opened."""
    with _FILE_LOCK:
        entry = _entry(config, name)
        wanted = []
        for path in (entry.get("read"), entry.get("write")):
            section = str(path).split(":", 1)[0] if path and ":" in str(path) else None
            if section and section not in wanted:
                wanted.append(section)
        for section in wanted:
            if section in _conf_sections(_conf_path(config)):
                _delete_remote(config, section)
        _write_entries(config, [e for e in _read_entries(config) if e.get("name") != name])


# ----------------------------------------------------------------------------- import

def _personal_conf(config: Config) -> Path | None:
    """The person's own rclone config file: RCLONE_CONFIG, else what `rclone config file` says without --config.
    None when there is none, or when it is drive-merge's own."""
    env = os.environ.get("RCLONE_CONFIG")
    path: Path | None = None
    if env:
        path = Path(env).expanduser()
    else:
        binary = resolve_binary(config)
        if binary:
            try:
                p = subprocess.run([binary, "config", "file"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", stdin=subprocess.DEVNULL, timeout=20)
            except (OSError, subprocess.SubprocessError):
                p = None
            lines = [ln.strip() for ln in (p.stdout if p and p.returncode == 0 else "").splitlines() if ln.strip()]
            if lines:
                path = Path(lines[-1]).expanduser()
    if path is None or not path.is_file():
        return None
    ours = {os.path.realpath(_conf_path(config)), os.path.realpath(config.own_rclone_conf)}
    return None if os.path.realpath(path) in ours else path


def _is_encrypted(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    return line.strip().startswith(_ENCRYPTED_HEADER)
    except OSError:
        pass
    return False


def importable(config: Config) -> dict:
    """The remotes of the person's own rclone config that could be copied into drive-merge. Section names and
    types are read straight from the file: no subprocess that prints tokens."""
    path = _personal_conf(config)
    if path is None:
        return {"config_path": None, "encrypted": False, "remotes": []}
    if _is_encrypted(path):
        return {"config_path": str(path), "encrypted": True, "remotes": []}
    done = {e.get("imported_from") for e in _read_entries(config) if e.get("imported_from")}
    remotes = []
    for section, opts in _conf_sections(path).items():
        rtype = opts.get("type")
        if not rtype:
            continue
        remotes.append({"name": section, "type": rtype, "provider": provider_of(rtype),
                        "supported": rtype not in UNSUPPORTED_IMPORT, "imported": section in done})
    return {"config_path": str(path), "encrypted": False, "remotes": remotes}


def _import_name(source: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", source.lower()).strip("-")
    if not NAME_RE.match(name):
        raise ConnectError("BAD_NAME", f"{source!r} cannot be turned into a cloud name; pass a name "
                                       "(lowercase letters, digits and '-', starting with a letter)")
    return name


def import_remote(config: Config, source: str, *, name: str | None = None) -> dict:
    """Copy remote `source` of the person's own rclone config into drive-merge's rclone.conf as `<name>-ro`.

    The copy keeps every value as stored (obscured passwords stay obscured once), is tested, and is recorded
    read-only with `scoped = false` and `imported_from`. The person's file is never written. Returns the
    connection, or a question result when rclone asks something."""
    _require_rclone(config)
    path = _personal_conf(config)
    if path is None:
        raise ConnectError("NOT_FOUND", "no rclone config of your own was found to import from")
    if _is_encrypted(path):
        raise ConnectError("IMPORT_ENCRYPTED", "your rclone config is encrypted; drive-merge cannot read it")
    section = _conf_sections(path).get(source)
    if section is None or not section.get("type"):
        raise ConnectError("NOT_FOUND", f"no remote named {source!r} in your rclone config")
    rtype = section["type"]
    if rtype in UNSUPPORTED_IMPORT:
        raise ConnectError("IMPORT_UNSUPPORTED", f"a {rtype} remote cannot be imported", type=rtype)
    if any(e.get("imported_from") == source for e in _read_entries(config)):
        raise ConnectError("NAME_TAKEN", f"{source!r} is already imported")
    wanted = name if name else _import_name(source)
    flow = AuthFlow(provider_of(rtype), "read")
    flow._rtype = rtype
    flow._extra = {"scoped": False, "imported_from": source}
    flow._secrets = [v for k, v in section.items() if k != "type" and len(v) >= 3]
    flow.status = "succeeded"
    spec = PROVIDER_SPECS.get(flow.provider)
    _register(flow, config)
    try:
        with _FILE_LOCK:
            flow.name = _claim_name(config, flow, spec, wanted, expect="succeeded")
            remote = flow._remote = f"{flow.name}-ro"
            kv = [f"{k}={v}" for k, v in section.items() if k != "type"]
            out = _cfg(config, flow, ["config", "create", remote, rtype, *kv, "--no-obscure", "--non-interactive"])
            return _next(config, flow, out)
    except BaseException as e:
        _fail(flow, e)
        raise
