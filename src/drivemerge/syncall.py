"""Sync every remote into the catalog in parallel (ONBOARDING.md section B).

Each remote is listed by its own thread with its own catalog connection (SQLite serialises the writes; the
connection's busy_timeout absorbs the contention). One failing remote never stops the others: its old rows
stay. Identity and folder statistics are rebuilt once, on a fresh connection, after every listing finished.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Callable

from . import db, dirstats, identity
from .config import Config, sync_remotes
from .rclone import Rclone
from .scanner import scan_remote

Progress = Callable[[str, str, int, int, int], None]


def _describe(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:2000]


def sync_all(config: Config, *, remotes: list[str] | None = None, progress: Progress | None = None,
             max_parallel: int = 4) -> dict:
    """List `remotes` (default: all configured) in parallel and rebuild identity once.

    `progress(name, state, files, dirs, bytes)` runs per batch; state is queued, listing, done or failed.
    Returns {'remotes': {name: {status, files, bytes, seconds, error}}, 'seconds', 'identity'}.
    """
    names = list(dict.fromkeys(remotes if remotes is not None else [r.name for r in config.remotes]))
    configs = {n: config.remote(n) for n in names}  # KeyError for an unknown name, before any work
    started = time.monotonic()
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def emit(name: str, state: str, files: int = 0, dirs: int = 0, size: int = 0) -> None:
        if progress is None:
            return
        with lock:  # callbacks may update shared job state; keep them one at a time
            try:
                progress(name, state, files, dirs, size)
            except Exception:
                pass  # a broken observer must not fail a sync

    # Make sure every remote row exists before the threads race to use it.
    if names:
        setup = db.connect(config.db_path)
        try:
            sync_remotes(setup, config)
        finally:
            setup.close()

    def one(name: str) -> None:
        t0 = time.monotonic()
        counts = [0, 0, 0]
        conn = None
        try:
            conn = db.connect(config.db_path)
            rclone = Rclone(config.rclone_binary, config=config.rclone_config,
                            extra_args=config.rclone_extra_args)
            emit(name, "listing")

            def on_batch(files: int, dirs: int, size: int) -> None:
                counts[:] = [files, dirs, size]
                emit(name, "listing", files, dirs, size)

            res = scan_remote(conn, rclone, configs[name], progress=on_batch)
            results[name] = {"status": res.status, "files": res.files, "bytes": res.bytes,
                             "dirs": res.dirs, "seconds": round(time.monotonic() - t0, 3), "error": None,
                             "scan": asdict(res)}
            emit(name, "done", res.files, res.dirs, res.bytes)
        except BaseException as e:  # noqa: BLE001 - isolated per remote
            if not isinstance(e, Exception):
                raise
            results[name] = {"status": "failed", "files": counts[0], "bytes": counts[2], "dirs": counts[1],
                             "seconds": round(time.monotonic() - t0, 3), "error": _describe(e)}
            emit(name, "failed", *counts)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    for n in names:
        emit(n, "queued")
    if names:
        with ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(names))),
                                thread_name_prefix="drivemerge-sync") as pool:
            for f in [pool.submit(one, n) for n in names]:
                f.result()

    ident = None
    if any(r["status"] == "complete" for r in results.values()):
        conn = db.connect(config.db_path)
        try:
            ident = asdict(identity.rebuild_groups(conn))
            dirstats.refresh(conn)
        finally:
            conn.close()
    return {"remotes": {n: results[n] for n in names}, "seconds": round(time.monotonic() - started, 3),
            "identity": ident}
