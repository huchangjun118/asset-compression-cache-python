"""End-to-end tests for the asset delivery layer."""

from __future__ import annotations

import gzip
import threading
import time
import zlib

import pytest

import app.service as svc_mod
from app.auth import AccessDeniedError, CallerContext
from app.cache import AssetCache, CacheKey
from app.config import CompressionRule, DeliveryConfig
from app.service import (AssetDeliveryService, CompressionTimeoutError,
                         OriginFetchError)
from app.store import Asset, AssetStore, make_fingerprint

TOKENS = {"t1": "tok-1", "t2": "tok-2"}


def make_asset(tenant: str, path: str, content_type: str, body: bytes,
               version: int, protected: bool = False,
               salt: str = "") -> Asset:
    return Asset(tenant, path, content_type, body,
                 make_fingerprint(content_type, body, version, salt),
                 version, protected)


@pytest.fixture
def store() -> AssetStore:
    return AssetStore()


@pytest.fixture
def service(store: AssetStore) -> AssetDeliveryService:
    config = DeliveryConfig(
        rules=(
            CompressionRule("image/", enabled=False),
            CompressionRule("text/", min_size_bytes=64, level=9),
        ),
        default_min_size_bytes=128,
        compress_timeout_seconds=5.0,
    )
    return AssetDeliveryService(store, AssetCache(), config,
                                valid_tokens=TOKENS)


def publish(store: AssetStore, tenant: str, path: str, content_type: str,
            body: bytes, version: int, protected: bool = False) -> None:
    store.publish(tenant, [make_asset(tenant, path, content_type, body,
                                      version, protected)], version)


CALLER1 = CallerContext("t1", "tok-1")
BIG_TEXT = b"the quick brown fox jumps over the lazy dog. " * 100


# ---------------------------------------------------------------- negotiation

class TestNegotiation:
    def test_no_accept_encoding_returns_identity(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt")
        assert r.status == 200
        assert r.encoding == "identity"
        assert r.body == BIG_TEXT
        assert "Content-Encoding" not in r.headers

    def test_gzip_selected_and_bytes_match_header(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        assert r.encoding == "gzip"
        assert r.headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(r.body) == BIG_TEXT

    def test_deflate_selected(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt", accept_encoding="deflate")
        assert r.encoding == "deflate"
        assert zlib.decompress(r.body) == BIG_TEXT

    def test_unsupported_encoding_falls_back_to_identity(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt", accept_encoding="br, zstd")
        assert r.encoding == "identity"
        assert r.body == BIG_TEXT

    def test_qvalue_preference(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt",
                              accept_encoding="gzip;q=0.1, deflate;q=0.9")
        assert r.encoding == "deflate"

    def test_refused_encoding_not_used(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip;q=0")
        assert r.encoding == "identity"

    def test_small_body_not_compressed(self, store, service):
        publish(store, "t1", "/tiny.txt", "text/plain", b"hi", 1)
        r = service.get_asset(CALLER1, "/tiny.txt", accept_encoding="gzip")
        assert r.encoding == "identity"

    def test_disabled_type_not_compressed(self, store, service):
        publish(store, "t1", "/p.png", "image/png", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/p.png", accept_encoding="gzip")
        assert r.encoding == "identity"

    def test_vary_header_always_present(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        for ae in (None, "gzip", "br"):
            r = service.get_asset(CALLER1, "/a.txt", accept_encoding=ae)
            assert r.headers["Vary"] == "Accept-Encoding"


# ------------------------------------------------------- conditional requests

class TestConditional:
    def test_if_none_match_returns_304(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        r2 = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip",
                               if_none_match=r.etag)
        assert r2.status == 304
        assert r2.body == b""
        assert r2.etag == r.etag
        assert r2.headers["Content-Encoding"] == "gzip"

    def test_mismatched_etag_returns_full_body(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip",
                              if_none_match='"stale"')
        assert r.status == 200
        assert gzip.decompress(r.body) == BIG_TEXT

    def test_etags_differ_per_encoding(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        g = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        i = service.get_asset(CALLER1, "/a.txt")
        assert g.etag != i.etag
        # A gzip ETag must not validate against the identity variant.
        r = service.get_asset(CALLER1, "/a.txt", if_none_match=g.etag)
        assert r.status == 200

    def test_star_matches(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        r = service.get_asset(CALLER1, "/a.txt", if_none_match="*")
        assert r.status == 304


# ------------------------------------------------------- concurrent single-flight

class TestSingleFlight:
    def test_concurrent_miss_compresses_once(self, store, service,
                                             monkeypatch):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        calls = 0
        calls_lock = threading.Lock()
        start = threading.Barrier(8)  # align request start, not compression
        real = svc_mod._compress_body

        def counting_compress(body, encoding, level):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.2)  # widen the race window for followers to pile up
            return real(body, encoding, level)

        monkeypatch.setattr(svc_mod, "_compress_body", counting_compress)

        results: list = []
        errors: list[BaseException] = []

        def worker():
            try:
                start.wait(timeout=10)
                results.append(service.get_asset(CALLER1, "/a.txt",
                                                 accept_encoding="gzip"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors
        assert len(results) == 8
        assert calls == 1  # exactly one compression for 8 concurrent misses
        assert {r.etag for r in results} == {results[0].etag}
        for r in results:
            assert gzip.decompress(r.body) == BIG_TEXT

    def test_leader_failure_not_cached_and_waiters_fail(self, store, service,
                                                        monkeypatch):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        started = threading.Event()
        release = threading.Event()
        real = svc_mod._compress_body

        def flaky(body, encoding, level):
            started.set()
            release.wait(timeout=10)
            raise RuntimeError("codec exploded")

        monkeypatch.setattr(svc_mod, "_compress_body", flaky)

        leader_errors: list[BaseException] = []
        waiter_errors: list[BaseException] = []

        def leader():
            try:
                service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
            except BaseException as exc:  # noqa: BLE001
                leader_errors.append(exc)

        def waiter():
            try:
                service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
            except BaseException as exc:  # noqa: BLE001
                waiter_errors.append(exc)

        lead = threading.Thread(target=leader)
        lead.start()
        started.wait(timeout=5)  # leader is now inside compression
        w = threading.Thread(target=waiter)
        w.start()
        time.sleep(0.1)  # let the waiter park on the in-flight load
        release.set()
        lead.join(timeout=10)
        w.join(timeout=10)

        assert len(leader_errors) == 1
        assert isinstance(leader_errors[0], RuntimeError)
        assert len(waiter_errors) == 1
        assert isinstance(waiter_errors[0], OriginFetchError)
        # Failure must not be cached: next request retries cleanly.
        monkeypatch.setattr(svc_mod, "_compress_body", real)
        r = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        assert r.status == 200 and gzip.decompress(r.body) == BIG_TEXT

    def test_compression_timeout(self, store, monkeypatch):
        config = DeliveryConfig(compress_timeout_seconds=0.1)
        service = AssetDeliveryService(store, AssetCache(), config,
                                       valid_tokens=TOKENS)
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)

        def slow(body, encoding, level):
            time.sleep(5)
            return body

        monkeypatch.setattr(svc_mod, "_compress_body", slow)
        with pytest.raises(CompressionTimeoutError):
            service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        # Timed-out result must not be cached.
        assert len(service._cache) == 0


# ------------------------------------------------------------- invalidation

class TestInvalidation:
    def test_new_version_serves_new_content(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        old = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        new_body = b"brand new content. " * 200
        publish(store, "t1", "/a.txt", "text/plain", new_body, 2)
        new = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        assert new.etag != old.etag
        assert gzip.decompress(new.body) == new_body
        # Old validator must not match the new representation.
        r = service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip",
                              if_none_match=old.etag)
        assert r.status == 200

    def test_explicit_invalidate_forces_recompress(self, store, service,
                                                   monkeypatch):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        assert len(service._cache) == 1
        removed = service.invalidate("t1", "/a.txt")
        assert removed == 1
        assert len(service._cache) == 0
        calls = 0
        real = svc_mod._compress_body

        def counting(body, encoding, level):
            nonlocal calls
            calls += 1
            return real(body, encoding, level)

        monkeypatch.setattr(svc_mod, "_compress_body", counting)
        service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        assert calls == 1

    def test_policy_change_makes_old_entries_unreachable(self, store):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        shared_cache = AssetCache()
        cfg_a = DeliveryConfig(default_level=6)
        cfg_b = DeliveryConfig(default_level=1)
        assert cfg_a.policy_salt() != cfg_b.policy_salt()
        svc_a = AssetDeliveryService(store, shared_cache, cfg_a,
                                     valid_tokens=TOKENS)
        svc_b = AssetDeliveryService(store, shared_cache, cfg_b,
                                     valid_tokens=TOKENS)
        ra = svc_a.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        assert len(shared_cache) == 1
        rb = svc_b.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        # Policy change -> new cache address: old entry not hit, new one added.
        assert len(shared_cache) == 2
        assert ra.etag != rb.etag

    def test_atomic_version_no_field_mixing(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        new_body = b"v2 " * 500
        stop = threading.Event()
        mixes: list[str] = []

        def reader():
            while not stop.is_set():
                r = service.get_asset(CALLER1, "/a.txt")
                body = r.body
                if body not in (BIG_TEXT, new_body):
                    mixes.append(repr(body[:40]))

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        for v in range(2, 12):
            publish(store, "t1", "/a.txt", "text/plain",
                    BIG_TEXT if v % 2 == 0 else new_body, v)
        stop.set()
        for t in readers:
            t.join(timeout=10)
        assert mixes == []


# ------------------------------------------------------------------ security

class TestAuthorization:
    def test_cross_tenant_cannot_read(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        with pytest.raises(Exception):
            service.get_asset(CallerContext("t2", "tok-2"), "/a.txt")

    def test_protected_requires_token(self, store, service):
        publish(store, "t1", "/s.txt", "text/plain", BIG_TEXT, 1,
                protected=True)
        with pytest.raises(AccessDeniedError):
            service.get_asset(CallerContext("t1"), "/s.txt")
        with pytest.raises(AccessDeniedError):
            service.get_asset(CallerContext("t1", "wrong"), "/s.txt")
        r = service.get_asset(CALLER1, "/s.txt", accept_encoding="gzip")
        assert r.status == 200

    def test_denied_request_does_not_touch_cache(self, store, service):
        publish(store, "t1", "/s.txt", "text/plain", BIG_TEXT, 1,
                protected=True)
        with pytest.raises(AccessDeniedError):
            service.get_asset(CallerContext("t1"), "/s.txt",
                              accept_encoding="gzip")
        assert len(service._cache) == 0

    def test_cache_is_tenant_isolated(self, store, service):
        publish(store, "t1", "/a.txt", "text/plain", BIG_TEXT, 1)
        publish(store, "t2", "/a.txt", "text/plain", BIG_TEXT, 1)
        service.get_asset(CALLER1, "/a.txt", accept_encoding="gzip")
        # t1's entry is keyed under tenant t1 only.
        for key in service._cache._entries:
            assert key.tenant == "t1"
        r2 = service.get_asset(CallerContext("t2", "tok-2"), "/a.txt",
                               accept_encoding="gzip")
        assert r2.status == 200
        tenants = {k.tenant for k in service._cache._entries}
        assert tenants == {"t1", "t2"}
