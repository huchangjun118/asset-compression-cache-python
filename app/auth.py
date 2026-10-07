"""Tenant access validation for protected assets."""
from __future__ import annotations

import hmac


class AccessDenied(Exception):
    pass


class AccessController:
    """Validates that a bearer token may read a tenant's assets.

    tenant_tokens maps tenant -> set of accepted tokens. A tenant with an
    empty token set is public. Validation happens before any cache access,
    so denied requests never touch or populate the cache, and tokens are
    scoped per tenant so one tenant's token cannot read another's cache.
    """

    def __init__(self, tenant_tokens: dict[str, set[str]]) -> None:
        self._tenant_tokens = {t: set(tokens) for t, tokens in tenant_tokens.items()}

    def check(self, tenant: str, token: str | None) -> None:
        """Raise AccessDenied when the token may not read the tenant."""
        tokens = self._tenant_tokens.get(tenant)
        if tokens is None:
            raise AccessDenied(f"unknown tenant: {tenant}")
        if not tokens:
            return  # public tenant
        if token is None:
            raise AccessDenied("missing token")
        if not any(hmac.compare_digest(token, t) for t in tokens):
            raise AccessDenied("invalid token")
