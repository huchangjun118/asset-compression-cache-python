"""Delivery service: negotiation, single-flight fetch+compress, caching."""

from __future__ import annotations

import gzip
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass

from .auth import AccessDeniedError, CallerContext, authorize
from .cache import AssetCache, CacheEntry, CacheKey
from .config import DeliveryConfig
from .negotiation import select_encoding
from .store import Asset, AssetStore, make_fingerprint

__all__ = [
    "AccessDeniedError",
    "AssetDeliveryService",
    "CompressionTimeoutError",
    "DeliveryResult",
    "OriginFetchError",
]


class OriginFetchError(RuntimeError):
    """Raised to waiters when the single-flight leader failed."""


class CompressionTimeoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    """What the caller gets back."""

    status: int  # 200 or 304
    body: bytes
    encoding: str  # "identity" when uncompressed
    etag: str
    content_type: str
    headers: dict[str, str]


class _Flight:
    """Shared state for one in-flight load; waiters read the result here."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.entry: CacheEntry | None = None
        self.error: BaseException | None = None


def _compress_body(body: bytes, encoding: str, level: int) -> bytes:
    if encoding == "gzip":
        return gzip.compress(body, compresslevel=level)
    if encoding == "deflate":
        return zlib.compress(body, level)
    raise ValueError(f"unsupported encoding {encoding!r}")


def _etag_for(fingerprint: str, encoding: str) -> str:
    # Per-encoding tag: gzip and identity variants of the same content
    # carry different ETags so proxy/client caches never mix them up.
    return f'"{fingerprint[:16]}+{encoding}"'


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = [c.strip() for c in if_none_match.split(",")]
    return "*" in candidates or etag in candidates


class AssetDeliveryService:
    """Coordinates store, cache, compression and conditional requests."""

    WAIT_TIMEOUT_SECONDS = 30.0

    def __init__(self, store: AssetStore, cache: AssetCache,
                 config: DeliveryConfig,
                 valid_tokens: dict[str, str] | None = None) -> None:
        self._store = store
        self._cache = cache
        self._config = config
        self._valid_tokens = valid_tokens or {}
        self._inflight: dict[tuple[str, str, str], _Flight] = {}
        self._inflight_lock = threading.Lock()
        self._known: dict[tuple[str, str], set[str]] = {}
        self._known_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4,
                                            thread_name_prefix="compress")

    # -- public API ------------------------------------------------------

    def get_asset(self, caller: CallerContext, path: str,
                  accept_encoding: str | None = None,
                  if_none_match: str | None = None) -> DeliveryResult:
        # 1. Atomic snapshot read of the live version.
        asset = self._store.get(caller.tenant, path)
        # 2. Authorize BEFORE any cache access: denied requests must not
        #    populate, read or otherwise touch the cache.
        authorize(caller, asset.tenant, asset.protected, self._valid_tokens)
        # 3. Negotiate encoding and apply the compression policy.
        encoding = self._negotiate(asset, accept_encoding)
        # 4. Cache addressed by (tenant, effective fingerprint, encoding).
        #    The effective fingerprint mixes the content fingerprint with
        #    the compression-policy salt, so policy changes make old
        #    entries unreachable.
        fingerprint = make_fingerprint(
            asset.content_type, asset.body, asset.version,
            self._config.policy_salt())
        key = CacheKey(caller.tenant, fingerprint, encoding)
        entry = self._cache.get(key)
        if entry is None:
            entry = self._load_single_flight(key, asset, encoding)
        # 5. Conditional request handling.
        return self._respond(entry, if_none_match)

    def invalidate(self, tenant: str, path: str) -> int:
        """Explicitly drop cached representations of one asset."""
        with self._known_lock:
            fingerprints = self._known.pop((tenant, path), set())
        removed = 0
        for fp in fingerprints:
            removed += self._cache.invalidate_fingerprint(tenant, fp)
        return removed

    # -- internals -------------------------------------------------------

    def _negotiate(self, asset: Asset, accept_encoding: str | None) -> str:
        encoding = select_encoding(accept_encoding, self._config.supported_encodings)
        if encoding is None:
            return "identity"
        should, _level = self._config.compression_params(
            asset.content_type, len(asset.body))
        if not should:
            return "identity"
        return encoding

    def _load_single_flight(self, key: CacheKey, asset: Asset,
                            encoding: str) -> CacheEntry:
        flight_key = (key.tenant, key.fingerprint, key.encoding)
        with self._inflight_lock:
            flight = self._inflight.get(flight_key)
            if flight is None:
                flight = _Flight()
                self._inflight[flight_key] = flight
                leader = True
            else:
                leader = False
        if leader:
            try:
                flight.entry = self._fetch_and_compress(key, asset, encoding)
            except BaseException as exc:  # noqa: BLE001 - propagated to waiters
                flight.error = exc
                raise
            finally:
                with self._inflight_lock:
                    self._inflight.pop(flight_key, None)
                flight.event.set()
            return flight.entry
        # Follower: wait for the leader's single fetch+compress.
        if not flight.event.wait(self.WAIT_TIMEOUT_SECONDS):
            raise OriginFetchError("timed out waiting for in-flight load")
        if flight.error is not None:
            raise OriginFetchError("concurrent load failed") from flight.error
        if flight.entry is None:  # pragma: no cover - defensive
            raise OriginFetchError("in-flight load returned no result")
        return flight.entry

    def _fetch_and_compress(self, key: CacheKey, asset: Asset,
                            encoding: str) -> CacheEntry:
        """Runs only on the single-flight leader. Failures are never cached."""
        if encoding == "identity":
            body = asset.body
        else:
            _should, level = self._config.compression_params(
                asset.content_type, len(asset.body))
            future = self._executor.submit(_compress_body, asset.body,
                                           encoding, level)
            try:
                body = future.result(timeout=self._config.compress_timeout_seconds)
            except FutureTimeout:
                future.cancel()
                raise CompressionTimeoutError(
                    f"compression of {asset.path} exceeded "
                    f"{self._config.compress_timeout_seconds}s") from None
        entry = CacheEntry(
            key=key,
            body=body,
            etag=_etag_for(key.fingerprint, encoding),
            content_type=asset.content_type,
            version=asset.version,
        )
        self._cache.put(entry)
        with self._known_lock:
            self._known.setdefault((key.tenant, asset.path), set()).add(
                key.fingerprint)
        return entry

    def _respond(self, entry: CacheEntry,
                 if_none_match: str | None) -> DeliveryResult:
        headers = {
            "Content-Type": entry.content_type,
            "ETag": entry.etag,
            "Vary": "Accept-Encoding",
            "X-Asset-Version": str(entry.version),
        }
        if entry.key.encoding != "identity":
            headers["Content-Encoding"] = entry.key.encoding
        if _etag_matches(if_none_match, entry.etag):
            return DeliveryResult(status=304, body=b"",
                                  encoding=entry.key.encoding,
                                  etag=entry.etag,
                                  content_type=entry.content_type,
                                  headers=headers)
        return DeliveryResult(status=200, body=entry.body,
                              encoding=entry.key.encoding,
                              etag=entry.etag,
                              content_type=entry.content_type,
                              headers=headers)
