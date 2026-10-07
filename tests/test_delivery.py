"""End-to-end tests for the asset delivery layer."""
from __future__ import annotations

import gzip
import threading
import time

import pytest

from app.auth import AccessController, AccessDenied
from app.cache import AssetCache
from app.compression import compress, negotiate_encoding
from app.config import CompressionRule, DeliveryConfig
from app.service import AssetDeliveryService, DeliveryRequest, SourceError
from app.store import AssetNotFound, AssetStore

BODY = ("lorem ipsum dolor sit amet " * 200).encode()
BODY_V2 = ("VERSION TWO " * 300).encode()


@pytest.fixture()
def env(tmp_path):
    v1 = tmp_path / "t1" / "v1"
    v1.mkdir(parents=True)
    (v1 / "big.txt").write_bytes(BODY)
    (v1 / "tiny.txt").write_bytes(b"hi")
    (v1 / "logo.png").write_bytes(BODY)
    prot = tmp_path / "prot" / "v1"
    prot.mkdir(parents=True)
    (prot / "secret.txt").write_bytes(BODY)
    store = AssetStore(str(tmp_path))
    cache = AssetCache(max_entries=64)
    auth = AccessController({"t1": set(), "prot": {"tok-prot"}})
    config = DeliveryConfig(
        rules={
            "text/": CompressionRule(min_size_bytes=64, level=6),
            "image/": CompressionRule(enabled=False),
        },
        default_rule=CompressionRule(min_size_bytes=1024),
        compress_time_budget_ms=500.0,
    )
    svc = AssetDeliveryService(store, cache, auth, config)
    return svc, store, cache, tmp_path


def req(path="big.txt", tenant="t1", **kw):
    return DeliveryRequest(tenant=tenant, path=path, **kw)


# -- 1. encoding negotiation --------------------------------------------


def test_gzip_chosen_and_header_matches_bytes(env):
    svc, *_ = env
    r = svc.get(req(accept_encoding="gzip"))
    assert r.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(r.body) == BODY


def test_no_accept_encoding_falls_back_to_identity(env):
    svc, *_ = env
    r = svc.get(req())
    assert "Content-Encoding" not in r.headers
    assert r.body == BODY


def test_unsupported_encoding_falls_back_to_identity(env):
    svc, *_ = env
    r = svc.get(req(accept_encoding="zstd, compress"))
    assert "Content-Encoding" not in r.headers
    assert r.body == BODY


def test_q_zero_gzip_not_chosen(env):
    svc, *_ = env
    r = svc.get(req(accept_encoding="gzip;q=0"))
    assert "Content-Encoding" not in r.headers


def test_negotiate_encoding_unit():
    assert negotiate_encoding(None, ["gzip"]) == "identity"
    assert negotiate_encoding("gzip, deflate", ["deflate", "gzip"]) == "gzip"
    assert negotiate_encoding("*;q=0.5", ["gzip"]) == "gzip"


def test_small_body_not_compressed(env):
    svc, *_ = env
    r = svc.get(req(path="tiny.txt", accept_encoding="gzip"))
    assert "Content-Encoding" not in r.headers
    assert r.body == b"hi"


def test_disabled_type_not_compressed(env):
    svc, *_ = env
    r = svc.get(req(path="logo.png", accept_encoding="gzip"))
    assert "Content-Encoding" not in r.headers
    assert r.body == BODY


# -- 2. cache addressing by content+encoding -----------------------------


def test_each_encoding_cached_separately(env):
    svc, _, cache, _ = env
    svc.get(req(accept_encoding="gzip"))
    svc.get(req())
    assert len(cache) == 2
    r_gz = svc.get(req(accept_encoding="gzip"))
    r_id = svc.get(req())
    assert gzip.decompress(r_gz.body) == r_id.body == BODY


def test_content_change_never_hits_old_entry(env):
    svc, store, cache, tmp = env
    r1 = svc.get(req(accept_encoding="gzip"))
    (tmp / "t1" / "v1" / "big.txt").write_bytes(BODY_V2)
    r2 = svc.get(req(accept_encoding="gzip"))
    assert gzip.decompress(r2.body) == BODY_V2
    assert r2.headers["ETag"] != r1.headers["ETag"]


def test_explicit_invalidation(env):
    svc, _, cache, _ = env
    svc.get(req(accept_encoding="gzip"))
    svc.get(req())
    removed = svc.invalidate("t1", "big.txt")
    assert removed == 2
    assert len(cache) == 0


# -- 3. conditional requests ---------------------------------------------


def test_if_none_match_returns_304(env):
    svc, *_ = env
    r = svc.get(req(accept_encoding="gzip"))
    r2 = svc.get(req(accept_encoding="gzip", if_none_match=r.headers["ETag"]))
    assert r2.status == 304 and r2.body == b""
    assert r2.headers["ETag"] == r.headers["ETag"]


def test_if_none_match_mismatch_returns_full_body(env):
    svc, *_ = env
    r = svc.get(req(accept_encoding="gzip", if_none_match='"stale"'))
    assert r.status == 200
    assert gzip.decompress(r.body) == BODY


def test_etags_differ_per_encoding_and_vary_header_set(env):
    svc, *_ = env
    r_gz = svc.get(req(accept_encoding="gzip"))
    r_id = svc.get(req())
    assert r_gz.headers["ETag"] != r_id.headers["ETag"]
    assert r_gz.headers["Vary"] == "Accept-Encoding"
    # A gzip etag must not validate the identity variant (proxy cache safety).
    r_cross = svc.get(req(if_none_match=r_gz.headers["ETag"]))
    assert r_cross.status == 200


# -- 4. concurrent singleflight ------------------------------------------


def test_concurrent_miss_triggers_single_origin_load(env):
    svc, store, _, _ = env
    loads = []
    orig_load = store.load

    def counting_load(tenant, path):
        loads.append((tenant, path))
        time.sleep(0.05)  # widen the race window
        return orig_load(tenant, path)

    store.load = counting_load  # type: ignore[method-assign]
    results, errors = [], []

    def worker():
        try:
            results.append(svc.get(req(accept_encoding="gzip")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 16
    assert len(loads) == 1
    assert all(gzip.decompress(r.body) == BODY for r in results)


def test_origin_failure_not_cached_and_waiters_see_error(env):
    svc, store, cache, _ = env
    orig_load = store.load
    calls = []

    def failing_load(tenant, path):
        calls.append(1)
        time.sleep(0.05)
        raise AssetNotFound("gone")

    store.load = failing_load  # type: ignore[method-assign]
    errors = []

    def worker():
        try:
            svc.get(req(accept_encoding="gzip"))
        except AssetNotFound as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 8  # every waiter gets a distinguishable failure
    assert len(cache) == 0   # nothing poisoned the cache
    store.load = orig_load  # type: ignore[method-assign]
    assert svc.get(req(accept_encoding="gzip")).status == 200


def test_compression_failure_not_cached(env):
    svc, _, cache, _ = env
    import app.service as svc_mod

    orig = svc_mod.compress

    def boom(body, encoding, level):
        if encoding != "identity":
            raise RuntimeError("codec exploded")
        return orig(body, encoding, level)

    svc_mod.compress = boom
    try:
        with pytest.raises(SourceError):
            svc.get(req(accept_encoding="gzip"))
    finally:
        svc_mod.compress = orig
    assert len(cache) == 0
    assert svc.get(req(accept_encoding="gzip")).status == 200


# -- 5. compression budget ------------------------------------------------


def test_slow_compression_falls_back_to_identity(tmp_path):
    v1 = tmp_path / "t1" / "v1"
    v1.mkdir(parents=True)
    (v1 / "big.txt").write_bytes(BODY)
    svc = AssetDeliveryService(
        AssetStore(str(tmp_path)),
        AssetCache(),
        AccessController({"t1": set()}),
        DeliveryConfig(
            rules={"text/": CompressionRule(min_size_bytes=64)},
            compress_time_budget_ms=1.0,  # impossibly tight
        ),
    )
    import app.service as svc_mod

    orig = svc_mod.compress

    def slow(body, encoding, level):
        if encoding != "identity":
            time.sleep(0.3)
        return orig(body, encoding, level)

    svc_mod.compress = slow
    try:
        start = time.monotonic()
        r = svc.get(req(accept_encoding="gzip"))
        elapsed = time.monotonic() - start
    finally:
        svc_mod.compress = orig
    assert "Content-Encoding" not in r.headers  # fell back, bytes are identity
    assert r.body == BODY
    assert elapsed < 0.3  # wait was bounded


# -- 6. version publish ----------------------------------------------------


def test_version_publish_switches_content_atomically(env):
    svc, store, _, tmp = env
    r1 = svc.get(req())
    assert r1.body == BODY
    v2 = tmp / "t1" / "v2"
    v2.mkdir(parents=True)
    (v2 / "big.txt").write_bytes(BODY_V2)
    store.publish_version("v2")
    r2 = svc.get(req())
    assert r2.body == BODY_V2
    assert r2.headers["X-Asset-Version"] == "v2"
    assert r2.headers["ETag"] != r1.headers["ETag"]


# -- 7. access control ------------------------------------------------------


def test_protected_tenant_requires_token(env):
    svc, *_ = env
    with pytest.raises(AccessDenied):
        svc.get(req(tenant="prot", path="secret.txt"))
    with pytest.raises(AccessDenied):
        svc.get(req(tenant="prot", path="secret.txt", token="wrong"))
    r = svc.get(req(tenant="prot", path="secret.txt", token="tok-prot"))
    assert r.body == BODY


def test_denied_request_does_not_pollute_cache(env):
    svc, _, cache, _ = env
    with pytest.raises(AccessDenied):
        svc.get(req(tenant="prot", path="secret.txt", accept_encoding="gzip"))
    assert len(cache) == 0


def test_cross_tenant_token_rejected(env):
    svc, *_ = env
    # t1 is public; its "token" must not open prot.
    with pytest.raises(AccessDenied):
        svc.get(req(tenant="prot", path="secret.txt", token="tok-other"))
