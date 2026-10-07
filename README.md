# asset-compression-cache-python

资源交付层：在把图片、静态文本等资源返回给调用方之前，统一做压缩协商与缓存复用。

## 架构

```
HTTP (app/web.py, FastAPI)
  └─ AssetDeliveryService (app/service.py)   协商 / 单飞回源+压缩 / 条件请求
       ├─ AssetStore   (app/store.py)        版本化源站，原子发布
       ├─ AssetCache   (app/cache.py)        按 (租户, 指纹, 编码) 寻址的 LRU 缓存
       ├─ negotiation  (app/negotiation.py)  Accept-Encoding 解析与选择
       ├─ auth         (app/auth.py)         租户隔离与受保护资源校验
       └─ config       (app/config.py)       压缩策略与阈值
```

## 缓存键

缓存条目按 **`CacheKey(tenant, fingerprint, encoding)`** 寻址：

- `tenant`：调用方租户。越权请求在触碰缓存前即被拒绝，且键内含租户，
  任何请求都读不到其他租户的缓存内容。
- `fingerprint`：对 `content_type + body + version + policy_salt` 的 SHA-256。
  内容变化、版本升级、或压缩策略（规则/级别/阈值/支持的编码）变化都会得到
  新指纹，旧条目因此不可达，不会再被命中。
- `encoding`：`gzip` / `deflate` / `identity`。同一份内容的不同编码各自缓存。

显式清理：

- `AssetDeliveryService.invalidate(tenant, path)`：删除某资源所有已知指纹的所有编码条目；
- `AssetCache.invalidate_fingerprint(tenant, fingerprint)` / `invalidate_tenant(tenant)` / `clear()`。

## 协商语义

- 未声明 `Accept-Encoding`、或声明的编码均不支持（如只有 `br`）→ 回退 `identity`，
  响应不带 `Content-Encoding`，正文为未压缩字节。
- 按 q 值选择服务器支持的最高优先级编码；`q=0` 表示显式拒绝；`*` 匹配未列出的编码。
- 压缩策略可再否决：类型被禁用（如 `image/`）或体积低于阈值时，即使客户端接受
  压缩也返回 `identity`。
- 响应的 `Content-Encoding` 始终与实际字节一致；所有响应带 `Vary: Accept-Encoding`。

## 条件请求与校验

- ETag 形如 `"<指纹前16位>+<编码>"`，**按编码区分**：同一内容的 gzip 与 identity
  变体 ETag 不同，代理与客户端缓存不会串味。
- `If-None-Match` 匹配（含 `*`）→ `304`，空正文，仍带 `ETag` / `Content-Encoding` / `Vary`；
  不匹配 → `200` 完整内容。

## 并发与失败语义

- 缓存未命中时，同一 `(租户, 指纹, 编码)` 的并发请求进入单飞（single-flight）：
  只有 leader 执行一次回源读取与一次压缩，follower 等待并复用同一结果。
- 回源或压缩失败：错误**不写入缓存**；leader 拿到原始异常，follower 拿到
  `OriginFetchError`（可区分）；后续请求会干净地重试。
- 单次压缩在独立线程池执行并受 `compress_timeout_seconds` 上界约束，超时抛
  `CompressionTimeoutError`，不写入缓存，个别大资源不会拖垮整体响应。

## 版本与失效

- `AssetStore.publish(tenant, assets, version)` 原子切换整个版本；`get()` 在同一把锁内
  解析活版本并读取，发布期间读到的内容必然来自同一个已生效版本，不会字段级混合。
- 新版本内容指纹不同，旧缓存条目自然不可达；也可显式 `invalidate()` 立即清理。

## 访问控制

- 跨租户读取一律拒绝（`AccessDeniedError` → HTTP 403/404）。
- 受保护资源（`Asset.protected=True`）需要该租户的有效 token。
- 校验发生在任何缓存访问之前：被拒绝的请求不会占用或污染缓存。

## 配置项（DeliveryConfig）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `rules` | `()` | `CompressionRule(content_type_prefix, min_size_bytes, level, enabled)`，按前缀首个命中生效 |
| `default_level` | 6 | 无匹配规则时的压缩级别 |
| `default_min_size_bytes` | 1024 | 无匹配规则时的最小压缩体积 |
| `compress_timeout_seconds` | 2.0 | 单次压缩耗时上界 |
| `cache_max_entries` | 1024 | LRU 缓存容量 |
| `supported_encodings` | `("gzip", "deflate")` | 服务端支持的编码 |

`DeliveryConfig.policy_salt()` 是以上策略的摘要，混入缓存指纹：策略一变，旧条目即不可达。

## HTTP 接口

- `GET /assets/{path}`：请求头 `X-Tenant`、`X-Token`（受保护资源）、
  `Accept-Encoding`、`If-None-Match`；响应带 `Content-Encoding` / `ETag` / `Vary`。
- `DELETE /admin/cache/{tenant}/{path}`：显式失效某资源的全部缓存表示。

## 测试

```bash
uv run pytest
```

覆盖：编码协商（含回退与头部一致性）、条件请求（304/200、按编码区分的 ETag）、
并发单飞回源（仅一次压缩、失败不污染缓存、等待方可区分失败）、压缩超时上界、
版本失效与原子发布、显式失效、策略变更失效、越权与缓存租户隔离。
