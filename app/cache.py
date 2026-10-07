"""Content+encoding addressed cache with explicit invalidation."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheKey:
    """Cache address: tenant + content fingerprint + encoding.

    The fingerprint already changes whenever the origin content or the
    compression policy changes, so stale entries can never be hit after
    either changes.
    """

    tenant: str
    fingerprint: str
    encoding: str


@dataclass
class CacheEntry:
    """Cached representation of one (content, encoding) pair."""

    key: CacheKey
    body: bytes
    etag: str
    content_type: str
    version: int


class AssetCache:
    """Thread-safe bounded LRU cache keyed by CacheKey."""

    def __init__(self, max_entries: int = 1024) -> None:
        self._max_entries = max(1, max_entries)
        self._lock = threading.Lock()
        self._entries: OrderedDict[CacheKey, CacheEntry] = OrderedDict()

    def get(self, key: CacheKey) -> CacheEntry | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, entry: CacheEntry) -> None:
        with self._lock:
            self._entries[entry.key] = entry
            self._entries.move_to_end(entry.key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def invalidate_fingerprint(self, tenant: str, fingerprint: str) -> int:
        """Drop all encodings of one content fingerprint. Returns count removed."""
        with self._lock:
            doomed = [k for k in self._entries
                      if k.tenant == tenant and k.fingerprint == fingerprint]
            for k in doomed:
                del self._entries[k]
            return len(doomed)

    def invalidate_tenant(self, tenant: str) -> int:
        with self._lock:
            doomed = [k for k in self._entries if k.tenant == tenant]
            for k in doomed:
                del self._entries[k]
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
