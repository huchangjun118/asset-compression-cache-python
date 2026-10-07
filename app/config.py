"""Configuration for compression policy and cache behaviour."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CompressionRule:
    """Compression settings for one resource type.

    - content_type_prefix: matched against the start of the asset's
      Content-Type (e.g. "text/" or "image/svg").
    - min_size_bytes: bodies smaller than this are not compressed
      (compression gains would not pay for the CPU and the overhead).
    - level: codec level (1..9 for gzip/deflate).
    - enabled: False disables compression for this type entirely.
    """

    content_type_prefix: str
    min_size_bytes: int = 0
    level: int = 6
    enabled: bool = True


@dataclass(frozen=True)
class DeliveryConfig:
    """Top-level delivery configuration."""

    rules: tuple[CompressionRule, ...] = field(default_factory=tuple)
    default_level: int = 6
    default_min_size_bytes: int = 1024
    compress_timeout_seconds: float = 2.0
    cache_max_entries: int = 1024
    supported_encodings: tuple[str, ...] = ("gzip", "deflate")

    def rule_for(self, content_type: str) -> CompressionRule | None:
        """Return the first rule matching the content type, if any."""
        ct = (content_type or "").lower()
        for rule in self.rules:
            if ct.startswith(rule.content_type_prefix.lower()):
                return rule
        return None

    def compression_params(self, content_type: str, size: int) -> tuple[bool, int]:
        """Decide (should_compress, level) for a body of the given type/size."""
        rule = self.rule_for(content_type)
        if rule is not None:
            if not rule.enabled or size < rule.min_size_bytes:
                return False, rule.level
            return True, rule.level
        if size < self.default_min_size_bytes:
            return False, self.default_level
        return True, self.default_level

    def policy_salt(self) -> str:
        """Stable digest of everything that affects compression output.

        Any change to rules, levels, thresholds or supported encodings
        changes this salt. It is mixed into cache fingerprints so entries
        produced under an old policy become unreachable.
        """
        h = hashlib.sha256()
        for rule in self.rules:
            h.update(repr(rule).encode("utf-8"))
            h.update(b"\x01")
        h.update(repr((self.default_level, self.default_min_size_bytes,
                       self.supported_encodings)).encode("utf-8"))
        return h.hexdigest()[:16]
