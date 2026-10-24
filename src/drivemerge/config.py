"""config.toml loading and mirroring [[remotes]] into the catalog."""

from __future__ import annotations

import contextlib
import importlib.resources
import os
import re
import secrets
import sqlite3
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import providers
from .db import connect as db_connect
from .db import set_quarantine_dir, utcnow

DEFAULT_CONFIG_PATH = Path(os.environ.get("DRIVEMERGE_CONFIG", "~/.config/drive-merge/config.toml")).expanduser()
DEFAULT_DB_PATH = "~/.local/share/drive-merge/catalog.db"
RCLONE_CONF_NAME = "rclone.conf"            # drive-merge's own rclone config, next to the main config
CONNECTIONS_NAME = "connections.toml"       # [[remotes]] written by `drivemerge connect` / the page


@dataclass
class RemoteConfig:
    name: str
    provider: str
    read: str                               # rclone path for listing/reading, e.g. 'gdrive-ro:'
    write: str | None = None                # rclone path the executor may modify; None = read-only
    hash_types: tuple[str, ...] | None = None  # override provider.native_hashes when scanning
    extra_args: tuple[str, ...] = ()        # extra rclone flags for this remote
    case_insensitive: bool | None = None    # override provider.case_insensitive (a local disk can be either)
    connection: bool = False                # created by drivemerge connect (lives in connections.toml)

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
    config_path: Path | None = None         # the main config file this was loaded from

    @property
    def config_dir(self) -> Path:
        return (self.config_path.parent if self.config_path else DEFAULT_CONFIG_PATH.parent)

    @property
    def connections_path(self) -> Path:
        return self.config_dir / CONNECTIONS_NAME

    @property
    def own_rclone_conf(self) -> Path:
        return self.config_dir / RCLONE_CONF_NAME

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
            connection=bool(r.get("connection", False)),
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
    """The main config plus `connections.toml` next to it.

    A remote name defined in both files is an error naming both. `rclone_config` defaults to drive-merge's own
    `rclone.conf` next to the config file when the config does not set `[rclone] config`.
    """
    p = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    with open(p, "rb") as f:
        cfg = parse_config(tomllib.load(f))
    cfg.config_path = p
    if not cfg.rclone_config:
        cfg.rclone_config = str(cfg.own_rclone_conf)
    cpath = cfg.connections_path
    if cpath.exists():
        try:
            with open(cpath, "rb") as f:
                extra = parse_config({"remotes": tomllib.load(f).get("remotes", [])}).remotes
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"invalid {cpath}: {e}") from None
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"invalid {cpath}: {e.args[0] if isinstance(e, KeyError) else e}") from None
        have = {r.name for r in cfg.remotes}
        for r in extra:
            if r.name in have:
                raise ValueError(f"remote {r.name!r} is defined in both {p} and {cpath}; "
                                 f"rename or remove one of them")
            have.add(r.name)
            r.connection = True
            cfg.remotes.append(r)
    return cfg


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


# --------------------------------------------------------------------------- writing files
# drive-merge never edits the person's config.toml in place: a connection is written to connections.toml (which
# connections.py owns and rewrites whole). What is left here creates a fresh config and writes private files.

RCLONE_PROVIDERS = {"drive": "drive", "dropbox": "dropbox", "onedrive": "onedrive", "iclouddrive": "iclouddrive",
                    "local": "local"}


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


def _template() -> str:
    return importlib.resources.files("drivemerge").joinpath("config_template.toml").read_text(encoding="utf-8")


def write_new_config(path: str | Path) -> Path:
    """Write a fresh config (mode 600, a new API token) and create its catalog; replaces whatever is at `path`.

    The packaged template keeps its example [[remotes]] entries as comments, so the file starts with no clouds."""
    path = Path(path)
    text, count = re.subn(r'(?m)^token\s*=\s*"".*$', f'token = "{secrets.token_urlsafe(32)}"', _template(), count=1)
    if count != 1:
        raise ValueError("config template has no empty [api] token line")
    write_private(path, text)
    cfg = load_config(path)                            # make sure what we wrote parses
    db_connect(cfg.db_path).close()
    return path


def ensure_config(path: str | Path) -> bool:
    """Create the config at `path` when the file is missing (what `drivemerge init` does); True when it was
    created. An existing file is never touched."""
    path = Path(path)
    if path.exists():
        return False
    write_new_config(path)
    return True
