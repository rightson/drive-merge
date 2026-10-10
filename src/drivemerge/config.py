"""config.toml loading and mirroring [[remotes]] into the catalog."""

from __future__ import annotations

import os
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import providers
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
