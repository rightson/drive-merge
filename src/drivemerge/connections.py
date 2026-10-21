"""Connecting clouds: drive-merge's own rclone config, `connections.toml`, and the sign-in flows.

Files, all mode 600, next to the main config (default `~/.config/drive-merge/`):
  rclone.conf         drive-merge's own rclone config (tokens live here and nowhere else)
  connections.toml    `[[remotes]]` entries written here; `config.load_config` merges them into the config

Nothing in this module logs or returns a token. A token exists in memory (an `AuthFlow`) between rclone's
`authorize` exiting and the `rclone config create` that stores it; it is dropped right after. rclone itself
gets it on its command line for that one call (rclone has no other way to take it).

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

from .config import Config, write_private
from .db import utcnow
from .rclone import Rclone, RcloneError

MIN_RCLONE = (1, 69)
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_URL_RE = re.compile(r"http://[\w.\-\[\]:]+/auth\?state=[\w\-%.~]+")
_TOKEN_BLOCK = re.compile(r"--->\s*(\{.*?\})\s*<---End paste", re.S)
DOWNLOAD_BASE = "https://downloads.rclone.org"
URL_WAIT_SECONDS = 15
MANAGED_BIN_DIR = Path("~/.local/share/drive-merge/bin")


class ConnectError(ValueError):
    """A connection step failed; `code` is one of NO_RCLONE, RCLONE_TOO_OLD, FLOW_BUSY, FLOW_FAILED,
    FLOW_TIMEOUT, NAME_TAKEN, BAD_NAME, NEEDS_CLIENT, TEST_FAILED, NOT_FOUND."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "drive": ProviderSpec(
        "drive", "drive", "Google Drive", True, {"scope": "drive.readonly"}, {"scope": "drive"},
        ("--fast-list", "--drive-skip-shortcuts"), "gdrive"),
    "onedrive": ProviderSpec(
        "onedrive", "onedrive", "OneDrive", False,
        {"access_scopes": "Files.Read Files.Read.All Sites.Read.All offline_access"}, {}, (), "onedrive"),
    "dropbox": ProviderSpec("dropbox", "dropbox", "Dropbox", False, {}, {}, (), "dropbox"),
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
            "created_at": e.get("created_at")}


def list_connections(config: Config) -> list[dict]:
    """Connections made by drive-merge: name, provider, account, read, write, writable, created_at. No tokens."""
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


def _run(config: Config, args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
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


def _create_remote(config: Config, remote: str, spec: ProviderSpec, token: str, options: dict[str, str]) -> None:
    """`rclone config create` and its non-interactive question loop, ending in a stored, complete remote."""
    kv = [f"token={token}", *[f"{k}={v}" for k, v in options.items()]]
    try:
        rc, out, err = _run(config, ["config", "create", remote, spec.rclone_type, *kv, "--non-interactive"])
        for _ in range(12):
            if rc != 0:
                raise ConnectError("FLOW_FAILED", f"rclone could not create the remote: {_scrub(_tail(err), token)}")
            try:
                state = json.loads(out) if out.strip() else {}
            except ValueError:
                raise ConnectError("FLOW_FAILED", "rclone gave an unreadable answer while creating the remote") from None
            if state.get("Error") and not state.get("Option"):
                raise ConnectError("FLOW_FAILED", f"rclone: {_scrub(str(state['Error']), token)[:400]}")
            if not state.get("State"):
                return
            opt = state.get("Option") or {}
            name = opt.get("Name", "")
            if name in ("config_refresh_token", "config_change_team_drive"):
                result = "false"
            elif name == "config_type":
                result = "onedrive"
            elif name == "config_drive_ok":
                result = "true"
            else:  # config_driveid and anything unknown: the default, else the first example
                result = str(opt.get("DefaultStr") or opt.get("Default") or "")
                if not result and opt.get("Examples"):
                    result = str(opt["Examples"][0].get("Value", ""))
            rc, out, err = _run(config, ["config", "create", remote, spec.rclone_type, "--non-interactive",
                                         "--continue", "--state", state["State"], "--result", result])
        raise ConnectError("FLOW_FAILED", "rclone kept asking questions while creating the remote")
    except BaseException:
        with contextlib.suppress(Exception):
            _delete_remote(config, remote)
        raise


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


def _test_remote(config: Config, remote: str) -> dict:
    rc = _rclone(config)
    try:
        it = rc.lsjson(remote, "", recursive=False)
        next(it, None)
        it.close()
    except RcloneError as e:
        return {"ok": False, "account": None, "quota": None, "error": _scrub(str(e))}
    quota = None
    with contextlib.suppress(RcloneError):
        about = rc.about(remote)
        if about:
            quota = {"used": about.get("used"), "total": about.get("total"), "free": about.get("free")}
    return {"ok": True, "account": _account(config, remote), "quota": quota, "error": None}


def _entry(config: Config, name: str) -> dict:
    for e in _read_entries(config):
        if e.get("name") == name:
            return e
    raise ConnectError("NOT_FOUND", f"no connection named {name!r}")


def test_connection(config: Config, name: str) -> dict:
    """List the top level and ask for the quota: {'ok', 'account', 'quota': {...} | None, 'error'}."""
    e = _entry(config, name)
    return _test_remote(config, e["read"])


test_connection.__test__ = False  # not a pytest test


# --------------------------------------------------------------------------- sign-in flows

class AuthFlow:
    """One rclone `authorize` subprocess. `public()` is what may leave this module; the token never does."""

    def __init__(self, provider: str, purpose: str):
        self.id = secrets.token_urlsafe(12)
        self.provider = provider
        self.purpose = purpose
        self.status = "waiting"           # waiting | succeeded | failed | cancelled | timeout
        self.auth_url: str | None = None
        self.error: str | None = None
        self._token: str | None = None
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._proc: subprocess.Popen | None = None
        self._url_or_done = threading.Event()
        self._finished = threading.Event()

    def public(self) -> dict:
        return {"id": self.id, "provider": self.provider, "purpose": self.purpose, "status": self.status,
                "auth_url": self.auth_url, "error": self.error}

    def __repr__(self) -> str:
        return f"AuthFlow(id={self.id!r}, provider={self.provider!r}, purpose={self.purpose!r}, status={self.status!r})"

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the flow is no longer waiting (for CLI and tests); False on timeout."""
        return self._finished.wait(timeout)


_FLOWS: dict[str, AuthFlow] = {}
_FLOWS_LOCK = threading.Lock()


def _forget_old_flows() -> None:
    done = [f for f in _FLOWS.values() if f.status != "waiting"]
    for f in done[:-20]:
        _FLOWS.pop(f.id, None)


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
        if flow.status == "cancelled":
            pass
        elif timed_out:
            flow.status, flow.error = "timeout", "the sign-in was not completed in time"
        elif proc.returncode == 0:
            token = _parse_token(out)
            if token:
                flow._token, flow.status = token, "succeeded"
            else:
                flow.status, flow.error = "failed", "rclone did not return a token"
        else:
            lines = [ln for ln in stderr_tail if ln.strip() and "NOTICE" not in ln]
            flow.status, flow.error = "failed", _scrub(_tail("\n".join(lines or list(stderr_tail))), flow._client_secret)
        if flow.status != "succeeded":
            flow._token = None
    flow._url_or_done.set()
    flow._finished.set()


def _parse_token(out: str) -> str | None:
    m = _TOKEN_BLOCK.search(out)
    candidates = [m.group(1)] if m else [ln for ln in out.splitlines() if ln.strip().startswith("{")][-1:]
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


def start_flow(config: Config, provider: str, *, purpose: str = "read", client_id: str | None = None,
               client_secret: str | None = None, timeout: float = 600, name: str | None = None,
               url_wait: float = URL_WAIT_SECONDS) -> AuthFlow:
    """Start `rclone authorize` and return once its sign-in URL is known. One flow at a time.

    For purpose 'write', `name` (an existing connection) supplies the Drive client id/secret when not given."""
    spec = _spec(provider)
    if purpose not in ("read", "write"):
        raise ConnectError("NOT_FOUND", f"unknown purpose {purpose!r}")
    binary = _require_rclone(config)
    if purpose == "write" and name and not (client_id and client_secret):
        sec = _conf_sections(_conf_path(config)).get(f"{name}-ro", {})
        client_id, client_secret = client_id or sec.get("client_id"), client_secret or sec.get("client_secret")
    if bool(client_id) != bool(client_secret):
        raise ConnectError("NEEDS_CLIENT", "give both the client id and the client secret")
    if spec.needs_client and not (client_id and client_secret):
        raise ConnectError("NEEDS_CLIENT", f"{spec.label} needs your own OAuth client id and secret")
    options = spec.read_options if purpose == "read" else spec.write_options
    argv = [binary, "--config", str(_conf_path(config)), "authorize", spec.rclone_type]
    if client_id and client_secret:
        argv += [client_id, client_secret]
    argv += [f"--{spec.rclone_type}-{k.replace('_', '-')}={v}" for k, v in options.items()]
    argv.append("--auth-no-open-browser")
    flow = AuthFlow(provider, purpose)
    flow._client_id, flow._client_secret = client_id, client_secret
    with _FLOWS_LOCK:
        if any(f.status == "waiting" for f in _FLOWS.values()):
            raise ConnectError("FLOW_BUSY", "another sign-in is already in progress; finish or cancel it first")
        _FLOWS[flow.id] = flow
        _forget_old_flows()
    _ensure_private_file(_conf_path(config))
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        with _FLOWS_LOCK:
            flow.status, flow.error = "failed", f"cannot run rclone: {e}"
            flow._finished.set()
        raise ConnectError("NO_RCLONE", flow.error) from None
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


def get_flow(flow_id: str) -> AuthFlow:
    with _FLOWS_LOCK:
        f = _FLOWS.get(flow_id)
    if f is None:
        raise ConnectError("NOT_FOUND", "unknown or expired sign-in")
    return f


def cancel_flow(flow_id: str) -> None:
    flow = get_flow(flow_id)
    with _FLOWS_LOCK:
        if flow.status != "waiting":
            return
        flow.status = "cancelled"
        flow._token = None
        proc = flow._proc
    if proc is not None and proc.poll() is None:
        proc.kill()
    flow._url_or_done.set()


# ---------------------------------------------------------------------- finishing a flow

def _taken(config: Config, name: str) -> bool:
    names = {r.name for r in config.remotes} | {e.get("name") for e in _read_entries(config)}
    secs = _conf_sections(_conf_path(config))
    return bool(names & {name} or {name, f"{name}-ro", f"{name}-rw"} & set(secs)
                or any(n in names for n in (f"{name}-ro", f"{name}-rw")))


def _pick_name(config: Config, spec: ProviderSpec, name: str | None) -> str:
    if name:
        if not NAME_RE.match(name):
            raise ConnectError("BAD_NAME", "a name starts with a lowercase letter and uses lowercase letters, "
                                           "digits and '-' (at most 31 characters)")
        if _taken(config, name):
            raise ConnectError("NAME_TAKEN", f"{name!r} is already in use")
        return name
    n = 1
    while True:
        cand = spec.default_name if n == 1 else f"{spec.default_name}-{n}"
        if not _taken(config, cand):
            return cand
        n += 1


def check_name(config: Config, provider: str, name: str | None) -> str:
    """The name a new connection would get (BAD_NAME / NAME_TAKEN when `name` is unusable); for early checks."""
    return _pick_name(config, _spec(provider), name)


def _flow_ready(flow_id: str, purpose: str) -> AuthFlow:
    flow = get_flow(flow_id)
    if flow.purpose != purpose:
        raise ConnectError("FLOW_FAILED", f"this sign-in is for purpose {flow.purpose!r}, not {purpose!r}")
    if flow.status == "waiting":
        raise ConnectError("FLOW_FAILED", "the sign-in is not finished yet")
    if flow.status != "succeeded" or not flow._token:
        raise ConnectError("FLOW_TIMEOUT" if flow.status == "timeout" else "FLOW_FAILED",
                           flow.error or f"the sign-in {flow.status}")
    return flow


def _drop_secrets(flow: AuthFlow) -> None:
    flow._token = flow._client_secret = None


def finish_connect(config: Config, flow_id: str, *, name: str | None = None) -> dict:
    """Create `<name>-ro` from a finished read flow, test it and record the connection. Returns the connection."""
    flow = _flow_ready(flow_id, "read")
    spec = _spec(flow.provider)
    with _FILE_LOCK:
        name = _pick_name(config, spec, name)
        remote = f"{name}-ro"
        options = dict(spec.read_options)
        if flow._client_id and flow._client_secret:
            options.update(client_id=flow._client_id, client_secret=flow._client_secret)
        _create_remote(config, remote, spec, flow._token or "", options)
        _drop_secrets(flow)
        try:
            res = _test_remote(config, f"{remote}:")
        except BaseException:
            _delete_remote(config, remote)
            raise
        if not res["ok"]:
            _delete_remote(config, remote)
            raise ConnectError("TEST_FAILED", f"the new connection does not work: {res['error']}")
        entry = {"name": name, "provider": spec.key, "read": f"{remote}:", "write": None,
                 "extra_args": list(spec.extra_args), "account": res["account"], "created_at": utcnow()}
        _write_entries(config, [*_read_entries(config), entry])
    return _public(entry)


def finish_enable_writes(config: Config, flow_id: str, name: str) -> dict:
    """Create `<name>-rw` from a finished write flow and set it as the connection's write remote."""
    flow = _flow_ready(flow_id, "write")
    with _FILE_LOCK:
        entry = _entry(config, name)
        if entry.get("provider") != flow.provider:
            raise ConnectError("FLOW_FAILED", f"this sign-in is for {flow.provider}, but {name!r} is {entry.get('provider')}")
        spec = _spec(flow.provider)
        remote = f"{name}-rw"
        options = dict(spec.write_options)
        sec = _conf_sections(_conf_path(config)).get(f"{name}-ro", {})
        cid, csec = flow._client_id or sec.get("client_id"), flow._client_secret or sec.get("client_secret")
        if cid and csec:
            options.update(client_id=cid, client_secret=csec)
        _delete_remote(config, remote)
        _create_remote(config, remote, spec, flow._token or "", options)
        _drop_secrets(flow)
        res = _test_remote(config, f"{remote}:")
        if not res["ok"]:
            _delete_remote(config, remote)
            raise ConnectError("TEST_FAILED", f"the write connection does not work: {res['error']}")
        entries = _read_entries(config)
        for e in entries:
            if e.get("name") == name:
                e["write"] = f"{remote}:"
                e["account"] = e.get("account") or res["account"]
                entry = e
        _write_entries(config, entries)
    return _public(entry)


def disconnect(config: Config, name: str) -> None:
    """Remove `<name>-ro`/`<name>-rw` from drive-merge's rclone.conf and the entry from connections.toml."""
    with _FILE_LOCK:
        _entry(config, name)
        for suffix in ("-ro", "-rw"):
            _delete_remote(config, name + suffix)
        _write_entries(config, [e for e in _read_entries(config) if e.get("name") != name])
