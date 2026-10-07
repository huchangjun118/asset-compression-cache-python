# Asset Compression & Cache Delivery Layer

在返回图片、静态文本等资源之前，统一做**压缩协商**与**缓存复用**的交付层。
HTTP 入口在 `main.py`（FastAPI），核心逻辑在 `app/` 包内，可脱离 Web 层独立使用。

## 架构

```
请求 ──► AccessController (鉴权，失败即拒，不触碰缓存)
      ──► AssetStore (按生效版本回源读盘，singleflight 合并并发回源)
      ──► 编码协商 (Accept-Encoding → gzip/deflate/identity)
      ──► AssetCache (按 内容指纹+编码 寻址，singleflight 合并并发压缩)
      ──► 条件请求 (If-None-Match → 304) / 完整响应
```

## 缓存键（Cache Key）

```
CacheKey = (fingerprint, encoding)
fingerprint = sha256(version + content_type + body) + "-" + policy_hash
```

- **按内容与编码共同寻址**：同一份原内容的 gzip / identity 是两条独立缓存项。
- 内容变化、版本发布（`publish_version`）或压缩策略（阈值/级别/预算）变化都会改变
  fingerprint，旧条目**永远不会再被命中**。
- 显式清理：`POST /admin/invalidate/{tenant}/{path}` 或
  `service.invalidate(tenant, path)`，清除该资源所有编码变体。
- 缓存为 LRU，容量由 `DeliveryConfig.max_cache_entries` 控制。

## 协商语义（Content Negotiation）

- 解析 `Accept-Encoding`（含 q 值与 `*`）；q 相同按服务端偏好 `br > gzip > deflate`。
- 未声明、声明了不支持的编码、或目标编码 `q=0` 时，回退 **identity**（不压缩）。
- 响应头与实际字节严格一致：压缩时带 `Content-Encoding: gzip`，identity 时不带该头，
  绝不把压缩字节当作未压缩内容发出。
- 所有响应带 `Vary: Accept-Encoding`；**ETag 按编码区分**
  （`"<fingerprint前32位>-<encoding>"`），代理与客户端缓存不会把不同编码的响应串味。
- `If-None-Match` 匹配当前变体 ETag 时返回 `304`（空 body），否则返回完整内容。

## 并发与失败语义

- 同一资源缓存未命中时，并发请求通过 singleflight 合并：**一次回源 + 一次压缩**，
  其余请求等待并复用同一份结果。
- 回源或压缩失败**不写入缓存**；所有等待方拿到同一个可区分的异常
  （`AssetNotFound` → 404，`SourceError` → 502）。

## 配置项（DeliveryConfig）

| 配置 | 含义 | 默认 |
|---|---|---|
| `rules` | 按 content-type 前缀（最长前缀优先）的 `CompressionRule` | `{}` |
| `CompressionRule.min_size_bytes` | 小于该体积不压缩（收益低） | `0` |
| `CompressionRule.level` | 压缩级别（gzip/deflate 1-9） | `6` |
| `CompressionRule.enabled` | 为 False 的类型（如已压缩的图片）不压缩 | `True` |
| `default_rule` | 未匹配类型的兜底规则 | 默认规则 |
| `compress_time_budget_ms` | 单次压缩耗时上界；超时回退 identity 且不缓存压缩结果 | `200` |
| `max_cache_entries` | 缓存 LRU 容量 | `1024` |

## 版本与一致性

- 资源存放在 `<root>/<tenant>/<version>/<path>`；`publish_version` 原子切换生效版本。
- 单次请求钉住开始时的版本，一次响应不会混合新旧版本的字节。
- 鉴权按租户隔离 token：校验失败返回 403 且不占用/污染缓存，
  一个租户的 token 无法读取其他租户的缓存内容。

## 运行与测试

```bash
uv sync
uv run pytest            # 26 个测试：协商/条件请求/并发回源/失效/越权/HTTP 层
uv run uvicorn main:app  # 启动服务（ASSET_ROOT 环境变量指定资源根目录）
```
