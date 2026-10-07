"""Origin asset store with versioning."""
from __future__ import annotations

import hashlib
import mimetypes
import threading
from dataclasses import dataclass
from pathlib import Path


class AssetNotFound(Exception):
    pass


@dataclass(frozen=True)
class RawAsset:
    tenant: str
    path: str
    body: bytes
    content_type: str
    fingerprint: str
    version: str


class AssetStore:
    """Reads assets from <root>/<tenant>/<version>/<path>.

    The active version pointer is swapped atomically on publish; a single
    load() pins the version it started with, so one response never mixes
    bytes from two versions.
    """

    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()
        self._version = "v1"

    def load(self, tenant: str, path: str) -> RawAsset:
        version = self.active_version()
        base = (self._root / tenant / version).resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise AssetNotFound(f"{tenant}/{path}@{version}")
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        fingerprint = hashlib.sha256(
            version.encode() + b"\0" + content_type.encode() + b"\0" + body
        ).hexdigest()
        return RawAsset(
            tenant=tenant,
            path=path,
            body=body,
            content_type=content_type,
            fingerprint=fingerprint,
            version=version,
        )

    def publish_version(self, version: str) -> None:
        with self._lock:
            self._version = version

    def active_version(self) -> str:
        with self._lock:
            return self._version
