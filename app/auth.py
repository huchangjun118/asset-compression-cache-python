"""Access checks for protected resources."""

from __future__ import annotations

import hmac
from dataclasses import dataclass


@dataclass(frozen=True)
class CallerContext:
    """Who is calling: tenant plus an optional token."""

    tenant: str
    token: str | None = None


class AccessDeniedError(PermissionError):
    pass


def authorize(caller: CallerContext, asset_tenant: str, protected: bool,
              valid_tokens: dict[str, str]) -> None:
    """Raise AccessDeniedError unless the caller may read the asset.

    Rules:
    - Cross-tenant reads are never allowed: a caller may only touch assets
      of its own tenant, so no request can ever read another tenant's
      cached content.
    - Unprotected assets of the caller's own tenant are always allowed.
    - Protected assets additionally require a valid token for that tenant.
    """
    if caller.tenant != asset_tenant:
        raise AccessDeniedError(
            f"tenant {caller.tenant!r} cannot read assets of tenant {asset_tenant!r}")
    if not protected:
        return
    expected = valid_tokens.get(asset_tenant)
    if expected is None or caller.token is None:
        raise AccessDeniedError("protected asset requires a valid token")
    if not hmac.compare_digest(caller.token, expected):
        raise AccessDeniedError("invalid token")
