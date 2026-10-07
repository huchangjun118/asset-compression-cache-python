"""Encoding negotiation and compression primitives."""
from __future__ import annotations

import gzip
import zlib
from dataclasses import dataclass

try:  # optional
    import brotli  # type: ignore
except ImportError:  # pragma: no cover
    brotli = None

IDENTITY = "identity"

_CODECS: dict[str, object] = {"gzip": gzip, "deflate": zlib}
if brotli is not None:  # pragma: no cover
    _CODECS["br"] = brotli

# Server preference order when the client has no explicit preference.
_PREFERENCE = ["br", "gzip", "deflate"]


def supported_encodings() -> list[str]:
    return [e for e in _PREFERENCE if e in _CODECS]


@dataclass(frozen=True)
class CompressedPayload:
    encoding: str
    body: bytes


def _parse_accept_encoding(header: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        token, _, params = part.partition(";")
        token = token.strip().lower()
        q = 1.0
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
        weights[token] = q
    return weights


def negotiate_encoding(accept_encoding: str | None, supported: list[str]) -> str:
    """Pick the best encoding the client declared; fall back to identity.

    A token with q=0 means "not acceptable". Unknown / undeclared
    encodings are never chosen. No header means identity.
    """
    if not accept_encoding or not accept_encoding.strip():
        return IDENTITY
    weights = _parse_accept_encoding(accept_encoding)
    best, best_q = IDENTITY, 0.0
    identity_q = weights.get(IDENTITY, 1.0)
    if identity_q <= 0.0 and not any(
        weights.get(enc, weights.get("*", 0.0)) > 0 for enc in supported
    ):
        return IDENTITY
    # Equal q-values break ties by server preference order.
    ordered = sorted(
        supported,
        key=lambda e: _PREFERENCE.index(e) if e in _PREFERENCE else len(_PREFERENCE),
    )
    for enc in ordered:
        q = weights.get(enc, weights.get("*", 0.0))
        if q > best_q or (q == best_q and q > 0 and best == IDENTITY):
            best, best_q = enc, q
    return best


def compress(body: bytes, encoding: str, level: int) -> CompressedPayload:
    """Compress body with encoding; identity returns the body unchanged."""
    if encoding == IDENTITY:
        return CompressedPayload(encoding=IDENTITY, body=body)
    if encoding == "gzip":
        return CompressedPayload(encoding, gzip.compress(body, compresslevel=level))
    if encoding == "deflate":
        return CompressedPayload(encoding, zlib.compress(body, level))
    if encoding == "br":  # pragma: no cover
        assert brotli is not None
        return CompressedPayload(encoding, brotli.compress(body, quality=min(level, 11)))
    raise ValueError(f"unsupported encoding: {encoding}")
