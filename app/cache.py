"""Content+encoding addressed cache."""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheKey:
    """Cache is addressed by content fingerprint AND content encoding.

    The fingerprint already covers content bytes + compression policy
    version, so any content or policy change yields new keys and old
    entries become unreachable.
    """

    fingerprint: str
    encoding: str


@dataclass
class CacheEntry:
    key: CacheKey
    body: bytes
    etag: str
    content_type: str
    version: str


class AssetCache:
    """Thread-safe LRU cache over CacheKey."""

    def __init__(self, max_entries: int = 1024) -> None:
        self._max = max(1, max_entries)
        self._entries: OrderedDict[CacheKey, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

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
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def invalidate_fingerprint(self, fingerprint: str) -> int:
        """Drop every encoding variant of one content fingerprint."""
        with self._lock:
            doomed = [k for k in self._entries if k.fingerprint == fingerprint]
            for k in doomed:
                del self._entries[k]
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
