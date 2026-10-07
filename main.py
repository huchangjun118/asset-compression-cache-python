import uvicorn

from app.cache import AssetCache
from app.config import CompressionRule, DeliveryConfig
from app.service import AssetDeliveryService
from app.store import Asset, AssetStore, make_fingerprint
from app.web import create_app

store = AssetStore()
config = DeliveryConfig(
    rules=(
        CompressionRule("image/", enabled=False),
        CompressionRule("text/", min_size_bytes=256, level=9),
    ),
)
cache = AssetCache(max_entries=config.cache_max_entries)
service = AssetDeliveryService(store, cache, config,
                               valid_tokens={"demo": "s3cret"})

body = ("Hello, world! " * 100).encode()
store.publish("demo", [
    Asset("demo", "/hello.txt", "text/plain", body,
          make_fingerprint("text/plain", body, 1), 1),
], version=1)

app = create_app(service)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
