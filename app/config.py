"""Compression / cache configuration."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CompressionRule:
    """Per resource-type compression policy.

    min_size_bytes: bodies smaller than this are served uncompressed.
    level: codec level (gzip/deflate 1-9, brotli 0-11).
    enabled: when False the type is never compressed.
    """

    min_size_bytes: int = 0
    level: int = 6
    enabled: bool = True


@dataclass(frozen=True)
class DeliveryConfig:
    """Top-level delivery configuration.

    rules: mapping of content-type prefix (e.g. "text/", "image/svg")
           to a CompressionRule; longest prefix wins.
    compress_time_budget_ms: upper bound for a single compression call;
           exceeding it falls back to identity for that response.
    max_cache_entries: LRU bound of the asset cache.
    """

    rules: dict[str, CompressionRule] = field(default_factory=dict)
    default_rule: CompressionRule = field(default_factory=CompressionRule)
    compress_time_budget_ms: float = 200.0
    max_cache_entries: int = 1024

    def rule_for(self, content_type: str) -> CompressionRule:
        best_prefix = ""
        best_rule = self.default_rule
        for prefix, rule in self.rules.items():
            if content_type.startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix = prefix
                best_rule = rule
        return best_rule
