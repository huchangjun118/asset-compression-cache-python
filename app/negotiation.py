"""Accept-Encoding parsing and encoding selection."""

from __future__ import annotations


def parse_accept_encoding(header: str | None) -> list[tuple[str, float]]:
    """Parse an Accept-Encoding header into (encoding, qvalue) pairs.

    Returns pairs sorted by descending qvalue, preserving header order for
    ties. An absent/empty header yields an empty list (meaning: identity
    only, per RFC 9110 the server SHOULD send identity).
    """
    if not header or not header.strip():
        return []
    pairs: list[tuple[str, float]] = []
    for item in header.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(";")
        encoding = parts[0].strip().lower()
        q = 1.0
        for param in parts[1:]:
            param = param.strip()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
        if encoding:
            pairs.append((encoding, q))
    pairs.sort(key=lambda p: -p[1])
    return pairs


def select_encoding(header: str | None, supported: tuple[str, ...]) -> str | None:
    """Pick the best supported encoding, or None for identity.

    Rules:
    - No header / empty header -> identity (None).
    - Highest-q supported encoding wins.
    - ``*`` matches any supported encoding not explicitly listed.
    - An encoding with q=0 is explicitly refused and never selected.
    - If nothing supported is acceptable, fall back to identity (None).
    """
    pairs = parse_accept_encoding(header)
    if not pairs:
        return None
    qvalues: dict[str, float] = {}
    for encoding, q in pairs:
        # First occurrence wins after sorting (highest q first).
        qvalues.setdefault(encoding, q)
    best: str | None = None
    best_q = 0.0
    for enc in supported:
        q = qvalues.get(enc)
        if q is None:
            q = qvalues.get("*", 0.0)
        if q > best_q:
            best, best_q = enc, q
    return best
