"""FastAPI surface for the asset delivery service."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response

from .auth import AccessDeniedError, CallerContext
from .service import (AssetDeliveryService, CompressionTimeoutError,
                      OriginFetchError)
from .store import AssetNotFoundError


def create_app(service: AssetDeliveryService) -> FastAPI:
    app = FastAPI(title="asset-delivery")

    @app.get("/assets/{path:path}")
    def get_asset(path: str, request: Request) -> Response:
        caller = CallerContext(
            tenant=request.headers.get("x-tenant", "public"),
            token=request.headers.get("x-token"),
        )
        try:
            result = service.get_asset(
                caller,
                "/" + path,
                accept_encoding=request.headers.get("accept-encoding"),
                if_none_match=request.headers.get("if-none-match"),
            )
        except AssetNotFoundError:
            return Response(status_code=404)
        except AccessDeniedError as exc:
            return Response(status_code=403, content=str(exc))
        except CompressionTimeoutError as exc:
            return Response(status_code=504, content=str(exc))
        except OriginFetchError as exc:
            return Response(status_code=502, content=str(exc))
        return Response(
            status_code=result.status,
            content=result.body,
            headers=result.headers,
        )

    @app.delete("/admin/cache/{tenant}/{path:path}")
    def invalidate(tenant: str, path: str) -> dict[str, int]:
        return {"removed": service.invalidate(tenant, "/" + path)}

    return app
