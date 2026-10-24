"""Per-cloud quota (used/total/free) as last seen by `connections.test_connection`.

One cache per running service: the page reads it for the Clouds cards and the Free space view, and the
simulator reads its fresh entries as the free space a plan may fill (Service.policy). Freshness is QUOTA_TTL
from the moment the quota was fetched; after that a quota is unknown to the simulator until the page (or a
connection test) fetches it again, and unknown means the quota is not checked.
"""

from __future__ import annotations

import threading
import time

from . import connections
from .config import Config

QUOTA_TTL = 600.0          # seconds a quota from test_connection is reused
QUOTA_FAIL_TTL = 60.0      # a failed lookup is not retried for this long


class QuotaCache:
    """Per-cloud used/total/free from `connections.test_connection`, kept for ten minutes so a page that shows
    several clouds does not call every provider on each view. None means unknown (not a connection, or it failed)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_name: dict[str, tuple[float, dict | None]] = {}

    def put(self, name: str, result: dict) -> None:
        quota = result.get("quota") if result.get("ok") else None
        with self._lock:
            self._by_name[name] = (time.monotonic() + (QUOTA_TTL if quota else QUOTA_FAIL_TTL), quota)

    def drop(self, name: str) -> None:
        with self._lock:
            self._by_name.pop(name, None)

    def peek(self, name: str) -> dict | None:
        """The cached quota while it is fresh, else None. Never looks anything up: safe under the catalog lock."""
        with self._lock:
            hit = self._by_name.get(name)
        return hit[1] if hit is not None and hit[0] > time.monotonic() else None

    def free_bytes(self, name: str) -> int | None:
        """Fresh known free bytes of a cloud; None when unknown, stale, or not a plain non-negative number."""
        free = (self.peek(name) or {}).get("free")
        return free if isinstance(free, int) and not isinstance(free, bool) and free >= 0 else None

    def get(self, config: Config, name: str) -> dict | None:
        """The cached quota; looks it up through test_connection when the cache is empty or old. Never raises."""
        hit = self.peek(name)
        if hit is not None:
            return hit
        with self._lock:
            failed = self._by_name.get(name)
        if failed is not None and failed[0] > time.monotonic():
            return None                                   # a failed lookup, not retried yet
        try:
            remote = config.remote(name)
        except KeyError:
            return None
        if not getattr(remote, "connection", False):
            return None
        try:
            result = connections.test_connection(config, name)
        except Exception:  # unknown quota is not an error for the report
            result = {"ok": False}
        self.put(name, result)
        return result.get("quota") if result.get("ok") else None
