"""Asset delivery service: negotiation, cache, singleflight, conditionals."""
from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass

from app.auth import AccessController
from app.cache import AssetCache, CacheEntry, CacheKey
from app.compression import IDENTITY, compress, negotiate_encoding, supported_encodings
from app.config import DeliveryConfig
from app.store import AssetNotFound, AssetStore, RawAsset


class SourceError(Exception):
    """Origin load or compression failed. Distinct from a cache miss."""


@dataclass(frozen=True)
class DeliveryRequest:
    tenant: str
    path: str
    accept_encoding: str | None = None
    if_none_match: str | None = None
    token: str | None = None


@dataclass(frozen=True)
class DeliveryResponse:
    status: int  # 200 or 304
    body: bytes
    headers: dict[str, str]


class _Flight:
    """One in-flight load/compress shared by all concurrent waiters."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.entry: CacheEntry | None = None
        self.raw: RawAsset | None = None
        self.error: BaseException | None = None


class AssetDeliveryService:
    def __init__(
        self,
        store: AssetStore,
        cache: AssetCache,
        auth: AccessController,
        config: DeliveryConfig,
    ) -> None:
        self._store = store
        self._cache = cache
        self._auth = auth
        self._config = config
        self._supported = supported_encodings()
        # Policy hash: any rule/budget change yields new fingerprints,
        # so stale entries become unreachable.
        self._policy_hash = hashlib.sha256(repr(config).encode()).hexdigest()[:16]
        self._raw_flights: dict[tuple[str, str], _Flight] = {}
        self._variant_flights: dict[CacheKey, _Flight] = {}
        self._flights_lock = threading.Lock()
        self._fp_index: dict[tuple[str, str], set[str]] = {}
        self._index_lock = threading.Lock()
        self._compressor_pool = ThreadPoolExecutor(max_workers=4)

    # -- public API -----------------------------------------------------

    def get(self, request: DeliveryRequest) -> DeliveryResponse:
        # Authz first: denied requests never touch or populate the cache.
        self._auth.check(request.tenant, request.token)

        raw = self._load_raw(request.tenant, request.path)
        fingerprint = f"{raw.fingerprint}-{self._policy_hash}"
        rule = self._config.rule_for(raw.content_type)

        encoding = negotiate_encoding(request.accept_encoding, self._supported)
        if (
            not rule.enabled
            or len(raw.body) < rule.min_size_bytes
            or encoding == IDENTITY
        ):
            encoding = IDENTITY

        key = CacheKey(fingerprint=fingerprint, encoding=encoding)
        entry = self._cache.get(key)
        if entry is None:
            entry = self._build_variant(key, raw, rule)
            if entry.key.encoding != encoding:
                # Compression fell back to identity (time budget): look up /
                # serve the identity variant instead of the requested one.
                key = entry.key

        etag = entry.etag
        headers = {
            "Content-Type": entry.content_type,
            "ETag": etag,
            "Vary": "Accept-Encoding",
            "Cache-Control": "no-cache",
            "X-Asset-Version": entry.version,
        }
        if entry.key.encoding != IDENTITY:
            headers["Content-Encoding"] = entry.key.encoding

        if self._etag_matches(request.if_none_match, etag):
            return DeliveryResponse(status=304, body=b"", headers=headers)
        return DeliveryResponse(status=200, body=entry.body, headers=headers)

    def invalidate(self, tenant: str, path: str) -> int:
        """Explicitly drop every cached variant of one asset."""
        removed = 0
        with self._index_lock:
            fingerprints = self._fp_index.pop((tenant, path), set())
        for fp in fingerprints:
            removed += self._cache.invalidate_fingerprint(fp)
        try:
            current = self._store.load(tenant, path)
        except Exception:
            return removed
        removed += self._cache.invalidate_fingerprint(
            f"{current.fingerprint}-{self._policy_hash}"
        )
        return removed

    # -- internals ------------------------------------------------------

    def _load_raw(self, tenant: str, path: str) -> RawAsset:
        """Singleflight origin load: one disk read per asset in flight."""
        flight_key = (tenant, path)
        with self._flights_lock:
            flight = self._raw_flights.get(flight_key)
            if flight is None:
                flight = _Flight()
                self._raw_flights[flight_key] = flight
                leader = True
            else:
                leader = False
        if leader:
            try:
                flight.raw = self._store.load(tenant, path)
            except AssetNotFound as exc:
                flight.error = exc  # keep type so callers can map 404
            except BaseException as exc:  # noqa: BLE001 - propagate to waiters
                flight.error = SourceError(f"origin load failed: {exc}")
            finally:
                flight.done.set()
                with self._flights_lock:
                    self._raw_flights.pop(flight_key, None)
        else:
            flight.done.wait()
        if flight.error is not None:
            raise flight.error
        assert flight.raw is not None
        return flight.raw

    def _build_variant(self, key: CacheKey, raw: RawAsset, rule) -> CacheEntry:
        """Singleflight compression: one compress per cache key in flight."""
        with self._flights_lock:
            flight = self._variant_flights.get(key)
            if flight is None:
                flight = _Flight()
                self._variant_flights[key] = flight
                leader = True
            else:
                leader = False
        if leader:
            try:
                flight.entry = self._compress_variant(key, raw, rule)
            except BaseException as exc:  # noqa: BLE001
                flight.error = SourceError(f"compression failed: {exc}")
            finally:
                flight.done.set()
                with self._flights_lock:
                    self._variant_flights.pop(key, None)
        else:
            flight.done.wait()
        if flight.error is not None:
            raise flight.error
        assert flight.entry is not None
        return flight.entry

    def _compress_variant(self, key: CacheKey, raw: RawAsset, rule) -> CacheEntry:
        encoding = key.encoding
        if encoding != IDENTITY:
            budget_s = max(self._config.compress_time_budget_ms, 0.0) / 1000.0
            future = self._compressor_pool.submit(
                compress, raw.body, encoding, rule.level
            )
            try:
                payload = future.result(timeout=budget_s)
            except FutureTimeout:
                # Bound the wait: serve identity instead, cache nothing
                # compressed for this oversized/slow variant.
                future.cancel()
                encoding = IDENTITY
                key = CacheKey(fingerprint=key.fingerprint, encoding=IDENTITY)
                payload = compress(raw.body, IDENTITY, rule.level)
        else:
            payload = compress(raw.body, IDENTITY, rule.level)

        etag = f'"{key.fingerprint[:32]}-{encoding}"'
        entry = CacheEntry(
            key=key,
            body=payload.body,
            etag=etag,
            content_type=raw.content_type,
            version=raw.version,
        )
        # Only successful results reach the cache.
        self._cache.put(entry)
        with self._index_lock:
            self._fp_index.setdefault((raw.tenant, raw.path), set()).add(
                key.fingerprint
            )
        return entry

    @staticmethod
    def _etag_matches(if_none_match: str | None, etag: str) -> bool:
        if not if_none_match:
            return False
        for candidate in if_none_match.split(","):
            candidate = candidate.strip()
            if candidate == "*":
                return True
            if candidate.startswith("W/"):
                candidate = candidate[2:]
            if candidate == etag:
                return True
        return False
