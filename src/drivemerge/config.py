"""config.toml loading and mirroring [[remotes]] into the catalog."""

from __future__ import annotations

import contextlib
import importlib.resources
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import providers
from .db import connect as db_connect
from .db import set_quarantine_dir, utcnow

DEFAULT_CONFIG_PATH = Path(os.environ.get("DRIVEMERGE_CONFIG", "~/.config/drive-merge/config.toml")).expanduser()
DEFAULT_DB_PATH = "~/.local/share/drive-merge/catalog.db"


@dataclass
class RemoteConfig:
    name: str
    provider: str
    read: str                               # rclone path for listing/reading, e.g. 'gdrive-ro:'
    write: str | None = None                # rclone path the executor may modify; None = read-only
    hash_types: tuple[str, ...] | None = None  # override provider.native_hashes when scanning
    extra_args: tuple[str, ...] = ()        # extra rclone flags for this remote
    case_insensitive: bool | None = None    # override provider.case_insensitive (a local disk can be either)

    @property
    def scan_hash_types(self) -> tuple[str, ...]:
        if self.hash_types is not None:
            return self.hash_types
        return providers.get(self.provider).native_hashes

    @property
    def folds_case(self) -> bool:
        """Does this remote treat `Docs` and `docs` as one name? The config wins over the provider's default."""
        if self.case_insensitive is not None:
            return self.case_insensitive
        return providers.get(self.provider).case_insensitive


@dataclass
class SafetyConfig:
    execution_enabled: bool = False         # master switch; nothing is ever written while False
    allow_probable_delete: bool = False     # never delete on a name+size match alone
    quarantine_dir: str = ".drive-merge-quarantine"
    min_copies: int = 1


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""


@dataclass
class Config:
    db_path: str = DEFAULT_DB_PATH
    rclone_binary: str = "rclone"
    rclone_config: str | None = None
    rclone_extra_args: tuple[str, ...] = ()
    rules_path: str | None = None
    remotes: list[RemoteConfig] = field(default_factory=list)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    api: ApiConfig = field(default_factory=ApiConfig)

    def remote(self, name: str) -> RemoteConfig:
        for r in self.remotes:
            if r.name == name:
                return r
        raise KeyError(f"unknown remote {name!r}")


def _optional_bool(remote: dict, key: str) -> bool | None:
    """`key` of a [[remotes]] entry as a bool, None when absent; a string like "false" would be truthy, so refuse it."""
    if key not in remote:
        return None
    if not isinstance(remote[key], bool):
        raise ValueError(f"remote {remote.get('name')!r}: {key} must be true or false, not {remote[key]!r}")
    return remote[key]


def parse_config(data: dict) -> Config:
    db = data.get("database", {})
    rc = data.get("rclone", {})
    remotes = []
    for r in data.get("remotes", []):
        if r.get("provider", "other") not in providers.PROVIDERS:
            raise ValueError(f"remote {r.get('name')!r}: unknown provider {r.get('provider')!r}")
        # rclone does not expand '~'; do it for local paths.
        expand = (lambda p: os.path.expanduser(p) if p else p) if r.get("provider") == "local" else (lambda p: p)
        remotes.append(RemoteConfig(
            name=r["name"],
            provider=r.get("provider", "other"),
            read=expand(r["read"]),
            write=expand(r.get("write")),
            hash_types=tuple(r["hash_types"]) if "hash_types" in r else None,
            extra_args=tuple(r.get("extra_args", ())),
            case_insensitive=_optional_bool(r, "case_insensitive"),
        ))
    names = [r.name for r in remotes]
    if len(names) != len(set(names)):
        raise ValueError("duplicate remote names in config")
    return Config(
        db_path=db.get("path", DEFAULT_DB_PATH),
        rclone_binary=rc.get("binary", "rclone"),
        rclone_config=rc.get("config") or None,
        rclone_extra_args=tuple(rc.get("extra_args", ())),
        rules_path=data.get("rules", {}).get("path"),
        remotes=remotes,
        safety=SafetyConfig(**data.get("safety", {})),
        api=ApiConfig(**data.get("api", {})),
    )


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    with open(p, "rb") as f:
        return parse_config(tomllib.load(f))


def sync_remotes(conn: sqlite3.Connection, config: Config) -> None:
    """Upsert config remotes into the `remotes` table (rows are never deleted:
    entries reference them), and record the quarantine folder every module must treat as not live."""
    set_quarantine_dir(conn, config.safety.quarantine_dir)
    for r in config.remotes:
        conn.execute(
            """INSERT INTO remotes(name, provider, read_remote, write_remote, case_insensitive, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET provider = excluded.provider,
                 read_remote = excluded.read_remote, write_remote = excluded.write_remote,
                 case_insensitive = excluded.case_insensitive""",
            (r.name, r.provider, r.read, r.write, int(r.folds_case), utcnow()),
        )


# --------------------------------------------------------------------------- writing the file
# The setup screen appends a [[remotes]] block and edits one `write` line; everything else in the file, comments
# included, stays as it is. Every write re-parses the result and leaves the file untouched when anything is off.

class ConfigError(ValueError):
    """A refusal to edit the config file, with a machine-readable `code` and `params` for the page to word."""

    def __init__(self, code: str, message: str, **params: str):
        super().__init__(message)
        self.code, self.params = code, params


RCLONE_PROVIDERS = {"drive": "drive", "dropbox": "dropbox", "onedrive": "onedrive", "iclouddrive": "iclouddrive",
                    "local": "local"}
PROVIDER_EXTRA_ARGS = {"drive": ("--fast-list", "--drive-skip-shortcuts")}
_write_lock = threading.Lock()
_HEADER = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\"' -]+?)\s*\]\]?\s*(?:#.*)?$")
_NAME_LINE = re.compile(r"^\s*name\s*=\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*')\s*(?:#.*)?$")


def provider_of(rclone_type: str) -> str:
    """drive-merge's provider key for an rclone backend type; anything it has no profile for is `other`."""
    return RCLONE_PROVIDERS.get(rclone_type, "other")


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` readable by the owner only, atomically (temp file beside it, fsync, rename).
    A symbolic link is written through, so it keeps pointing at the real file."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)      # a JSON string is a valid TOML basic string


def _toml_list(values) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _read_text(path: Path, name: str) -> tuple[str, dict]:
    try:
        text = Path(path).read_bytes().decode("utf-8")        # keeps \r\n as it is
        return text, tomllib.loads(text)
    except OSError as e:
        raise ConfigError("CONFIG_WRITE", f"cannot read {path}: {e}", name=name) from e
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError("CONFIG_LAYOUT", f"{path} is not valid TOML ({e}); fix it by hand first", name=name) from e


def _commit(path: Path, name: str, new_text: str, expected: list[dict]) -> None:
    """Write `new_text` after checking that it parses and that [[remotes]] is exactly `expected` (raw tables)."""
    try:
        data = tomllib.loads(new_text)
        parse_config(data)
    except (tomllib.TOMLDecodeError, ValueError, KeyError, TypeError) as e:
        raise ConfigError("CONFIG_LAYOUT", f"the edited config would not parse ({e}); {path} was not changed",
                          name=name) from e
    if data.get("remotes") != expected:
        raise ConfigError("CONFIG_LAYOUT", f"the edit would change more than the remote {name!r}; "
                                           f"{path} was not changed", name=name)
    try:
        write_private(path, new_text)
    except OSError as e:
        raise ConfigError("CONFIG_WRITE", f"cannot write {path}: {e}", name=name) from e


def add_remote_block(path: str | Path, remote: RemoteConfig) -> None:
    """Append a `[[remotes]]` block for `remote` (extra_args default to the provider's when none are given).

    REMOTE_EXISTS when the name is taken; CONFIG_LAYOUT when the file cannot take the block; the file is untouched
    on any error."""
    path = Path(path)
    with _write_lock:
        text, data = _read_text(path, remote.name)
        existing = data.get("remotes", [])
        if not isinstance(existing, list) or any(not isinstance(r, dict) for r in existing):
            raise ConfigError("CONFIG_LAYOUT", "[[remotes]] in the config is not a list of tables", name=remote.name)
        if any(r.get("name") == remote.name for r in existing):
            raise ConfigError("REMOTE_EXISTS", f"remote {remote.name!r} is already in {path}", name=remote.name)
        extra = remote.extra_args or PROVIDER_EXTRA_ARGS.get(remote.provider, ())
        table = {"name": remote.name, "provider": remote.provider, "read": remote.read}
        lines = ["[[remotes]]", f"name = {_toml_str(remote.name)}", f"provider = {_toml_str(remote.provider)}",
                 f"read = {_toml_str(remote.read)}"]
        if remote.write:
            table["write"] = remote.write
            lines.append(f"write = {_toml_str(remote.write)}")
        if remote.hash_types is not None:
            table["hash_types"] = list(remote.hash_types)
            lines.append(f"hash_types = {_toml_list(remote.hash_types)}")
        if extra:
            table["extra_args"] = list(extra)
            lines.append(f"extra_args = {_toml_list(extra)}")
        if remote.case_insensitive is not None:
            table["case_insensitive"] = remote.case_insensitive
            lines.append(f"case_insensitive = {'true' if remote.case_insensitive else 'false'}")
        nl = "\r\n" if "\r\n" in text else "\n"
        body = text.rstrip("\r\n")
        new_text = (body + nl + nl if body else "") + nl.join(lines) + nl
        _commit(path, remote.name, new_text, [*existing, table])


def set_remote_write(path: str | Path, name: str, write: str | None) -> None:
    """Set (or, with None, remove) the `write` key of the `[[remotes]]` block named `name`, in place.

    REMOTE_NOT_FOUND when no such remote; CONFIG_LAYOUT when its block cannot be located line by line (remotes
    written as inline tables, say); the file is untouched on any error."""
    path = Path(path)
    with _write_lock:
        text, data = _read_text(path, name)
        existing = data.get("remotes", [])
        if not isinstance(existing, list) or not any(isinstance(r, dict) and r.get("name") == name for r in existing):
            raise ConfigError("REMOTE_NOT_FOUND", f"remote {name!r} is not in {path}", name=name)
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()
        starts = [i for i, ln in enumerate(lines) if re.fullmatch(r"\s*\[\[\s*remotes\s*\]\]\s*(?:#.*)?", ln)]
        found = []
        for i in starts:
            end = next((j for j in range(i + 1, len(lines)) if _HEADER.match(lines[j])), len(lines))
            for j in range(i + 1, end):
                m = _NAME_LINE.match(lines[j])
                if m:
                    try:
                        if tomllib.loads(f"n = {m.group(1)}")["n"] == name:
                            found.append((i, end))
                    except tomllib.TOMLDecodeError:
                        pass
                    break
        if len(found) != 1:
            raise ConfigError("CONFIG_LAYOUT", f"cannot find the [[remotes]] block of {name!r} in {path} "
                                               "to edit it in place; edit the file by hand", name=name)
        start, end = found[0]
        write_at = next((j for j in range(start + 1, end) if re.match(r"\s*write\s*=", lines[j])), None)
        if write is None:
            if write_at is not None:
                del lines[write_at]
        elif write_at is not None:
            lines[write_at] = f"write = {_toml_str(write)}"
        else:
            after = next((j for j in range(start + 1, end) if re.match(r"\s*read\s*=", lines[j])), None)
            if after is None:
                after = next(j for j in range(start + 1, end) if _NAME_LINE.match(lines[j]))
            lines.insert(after + 1, f"write = {_toml_str(write)}")
        expected = []
        for r in existing:
            if r.get("name") == name:
                r = {k: v for k, v in r.items() if k != "write"}
                if write is not None:
                    r["write"] = write
            expected.append(r)
        new_text = nl.join(lines) + (nl if text.endswith(("\n", "\r")) else "")
        _commit(path, name, new_text, expected)


def _template() -> str:
    return importlib.resources.files("drivemerge").joinpath("config_template.toml").read_text(encoding="utf-8")


def write_new_config(path: str | Path, *, sample_remotes: bool = True) -> Path:
    """Write a fresh config (mode 600, a new API token) and create its catalog; replaces whatever is at `path`.

    With `sample_remotes` False the example [[remotes]] entries stay as comments, so the file starts with no clouds."""
    path = Path(path)
    text, count = re.subn(r'(?m)^token\s*=\s*"".*$', f'token = "{secrets.token_urlsafe(32)}"', _template(), count=1)
    if count != 1:
        raise ValueError("config template has no empty [api] token line")
    if not sample_remotes:
        head, sep, tail = text.partition("[[remotes]]")
        tail = "\n".join(ln if not ln.strip() or ln.lstrip().startswith("#") else "# " + ln
                         for ln in (sep + tail).splitlines())
        text = head + tail + "\n"
    write_private(path, text)
    cfg = load_config(path)                            # make sure what we wrote parses
    db_connect(cfg.db_path).close()
    return path


def ensure_config(path: str | Path) -> bool:
    """Create the config at `path` when the file is missing (what `drivemerge init` does, without the sample
    clouds); True when it was created. An existing file is never touched."""
    path = Path(path)
    if path.exists():
        return False
    write_new_config(path, sample_remotes=False)
    return True
