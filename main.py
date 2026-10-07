"""HTTP wiring for the asset delivery service."""
from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, Request, Response

from app.auth import AccessController, AccessDenied
from app.cache import AssetCache
from app.config import CompressionRule, DeliveryConfig
from app.service import AssetDeliveryService, DeliveryRequest, SourceError
from app.store import AssetNotFound, AssetStore

ASSET_ROOT = os.environ.get("ASSET_ROOT", "assets")

config = DeliveryConfig(
    rules={
        "text/": CompressionRule(min_size_bytes=256, level=6),
        "image/svg": CompressionRule(min_size_bytes=256, level=6),
        "image/": CompressionRule(enabled=False),  # already-compressed formats
    },
    default_rule=CompressionRule(min_size_bytes=1024, level=6),
    compress_time_budget_ms=200.0,
    max_cache_entries=1024,
)

service = AssetDeliveryService(
    store=AssetStore(ASSET_ROOT),
    cache=AssetCache(max_entries=config.max_cache_entries),
    auth=AccessController(
        {"public": set(), "protected": {"protected-secret-token"}}
    ),
    config=config,
)

app = FastAPI()


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/assets/{tenant}/{asset_path:path}")
def get_asset(tenant: str, asset_path: str, request: Request, response: Response):
    try:
        result = service.get(
            DeliveryRequest(
                tenant=tenant,
                path=asset_path,
                accept_encoding=request.headers.get("accept-encoding"),
                if_none_match=request.headers.get("if-none-match"),
                token=request.headers.get("x-asset-token"),
            )
        )
    except AccessDenied as exc:
        return Response(status_code=403, content=str(exc))
    except AssetNotFound as exc:
        return Response(status_code=404, content=str(exc))
    except SourceError as exc:
        return Response(status_code=502, content=str(exc))
    for name, value in result.headers.items():
        response.headers[name] = value
    return Response(
        status_code=result.status,
        content=result.body,
        headers=dict(result.headers),
    )


@app.post("/admin/invalidate/{tenant}/{asset_path:path}")
def invalidate_asset(tenant: str, asset_path: str):
    removed = service.invalidate(tenant, asset_path)
    return {"removed": removed}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
