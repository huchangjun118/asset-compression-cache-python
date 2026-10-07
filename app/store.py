"""Versioned origin asset store with atomic snapshot reads."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    """One immutable asset snapshot.

    The fingerprint covers content + content type + the compression policy
    salt, so any change to content or policy yields a new cache address.
    """

    tenant: str
    path: str
    content_type: str
    body: bytes
    fingerprint: str
    version: int
    protected: bool = False


def make_fingerprint(content_type: str, body: bytes, version: int,
                     policy_salt: str = "") -> str:
    """Content fingerprint: strong hash over type, body, version and policy."""
    h = hashlib.sha256()
    h.update(content_type.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(version).encode("ascii"))
    h.update(b"\x00")
    h.update(policy_salt.encode("utf-8"))
    h.update(b"\x00")
    h.update(body)
    return h.hexdigest()


class AssetNotFoundError(KeyError):
    pass


class AssetStore:
    """Origin store. publish() swaps a whole version atomically.

    Readers always see exactly one fully-published version: get() resolves
    the live version and reads the asset from that same snapshot under one
    lock hold, so a concurrent publish can never produce a field-level mix
    of old and new content.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # tenant -> version -> {path: Asset}
        self._versions: dict[str, dict[int, dict[str, Asset]]] = {}
        self._live: dict[str, int] = {}

    def publish(self, tenant: str, assets: list[Asset], version: int) -> None:
        """Atomically make a full asset set the live version for a tenant."""
        snapshot = {a.path: a for a in assets}
        for a in assets:
            if a.version != version or a.tenant != tenant:
                raise ValueError("asset does not belong to this tenant/version")
        with self._lock:
            self._versions.setdefault(tenant, {})[version] = snapshot
            self._live[tenant] = version
            # Keep only the live version to bound memory; cached entries
            # are fingerprint-addressed so they stay valid independently.
            for v in list(self._versions[tenant]):
                if v != version:
                    del self._versions[tenant][v]

    def get(self, tenant: str, path: str) -> Asset:
        """Read one asset from the currently live version (atomic)."""
        with self._lock:
            version = self._live.get(tenant)
            if version is None:
                raise AssetNotFoundError(f"tenant {tenant!r} has no live version")
            snapshot = self._versions[tenant][version]
            try:
                return snapshot[path]
            except KeyError:
                raise AssetNotFoundError(f"{tenant}/{path} not found") from None

    def current_version(self, tenant: str) -> int:
        with self._lock:
            return self._live.get(tenant, -1)
