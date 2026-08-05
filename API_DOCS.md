# Adidas Monitor Scanner — 接口文档

服务启动后会自动生成交互式文档，推荐直接用它们来调试（可以在页面里直接发请求）：

- **Swagger UI**：http://localhost:8080/docs
- **ReDoc**：http://localhost:8080/redoc

本文档是对同一套接口的静态说明，方便离线查阅。

Base URL（本地）：`http://localhost:8080`

---

## 目录

| 分组 | 说明 |
|------|------|
| [System](#system) | 健康检查、服务信息 |
| [Products](#products) | 单品实时查询 |
| [Scan](#scan) | 触发扫描（监控列表 / 全量 / 全量+尺码）、任务状态查询 |
| [Watch List](#watch-list) | 监控项（要通知的 SKU+尺码）增删改查 |
| [Notifications](#notifications) | 通知历史 |
| [Config](#config) | Slack 配置 |

---

## System

### `GET /health`

健康检查。

**响应 200**
```json
{
  "status": "ok",
  "timestamp": "2026-08-05T03:33:16.864252",
  "mongodb": true,
  "slack": false
}
```

### `GET /`

服务信息 + 所有接口路径总览（也会给出 `/docs`、`/redoc` 链接）。

---

## Products

### `GET /products/{sku}`

**实时**查询单个商品的价格和各尺码库存状态。直接请求 Adidas 官网 API，**不经过数据库缓存**，适合临时核对某个商品的当前状态。

**路径参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `sku` | string | 商品 SKU，如 `JR5408` |

**请求示例**
```bash
curl http://localhost:8080/products/JR5408
```

**响应 200**
```json
{
  "sku": "JR5408",
  "name": "Adidas Ultraboost 22",
  "color": "White/Black",
  "currency": "USD",
  "original_price": 180.0,
  "sale_price": 144.0,
  "overall_status": "IN_STOCK",
  "sizes": [
    {"sku": "JR5408.1", "size": "US 8", "status": "IN_STOCK", "qty": 12},
    {"sku": "JR5408.2", "size": "US 9", "status": "OUT_OF_STOCK", "qty": 0}
  ],
  "in_stock_sizes": [
    {"sku": "JR5408.1", "size": "US 8", "status": "IN_STOCK", "qty": 12}
  ],
  "checked_at": "2026-08-05 11:30:00"
}
```

**响应 404** — SKU 不存在或已下架
```json
{"detail": "查询 JR5408 失败: HTTP Error 404: "}
```

---

## Scan

三种扫描粒度，按场景选用：

| 接口 | 数据来源 | 速度 | 用途 |
|------|---------|------|------|
| `POST /scan` | 监控列表（或手动传入的 SKU） | 秒级~分钟级 | 只关心"我关注的货有没有货"，会触发 Slack 通知 |
| `POST /scan/full` | 全站分类页（PLP）发现的所有 SKU | 几分钟 | 只更新价格/上下架，不抓尺码 |
| `POST /scan/full-with-sizes` | 全站分类页 + 每个 SKU 的尺码库存 | 10-20 分钟（1-2万 SKU） | 完整扫描，对应定时任务每 30 分钟应执行的内容 |

三个接口都是**异步**的：请求会立刻返回一个 `job_id`，实际扫描在后台线程执行，用 `GET /scan/jobs/{job_id}` 轮询进度和结果。

### `POST /scan` — 扫描监控列表

检测监控列表中每个 SKU+尺码 的库存变化，命中时发送 Slack 通知。

**请求体**（可选，留空则自动使用当前监控列表里的全部 SKU）
```json
{
  "skus": ["JR5408", "JR5410"]
}
```

**请求示例**
```bash
# 扫描监控列表中的所有 SKU
curl -X POST http://localhost:8080/scan -H "Content-Type: application/json" -d '{}'

# 或指定 SKU
curl -X POST http://localhost:8080/scan \
  -H "Content-Type: application/json" \
  -d '{"skus": ["JR5408", "JR5410"]}'
```

**响应 200**
```json
{
  "job_id": "d1de410d-ea70-43ad-810b-2a4a58862d61",
  "status": "queued",
  "message": "扫描任务已排队（1 个 SKU），可通过 GET /scan/jobs/{job_id} 查询进度"
}
```

**响应 400** — 监控列表为空且未传 `skus`
```json
{"detail": "监控列表为空，请先通过 POST /watch 添加要监控的商品，或在请求体中提供 skus 列表"}
```

任务完成后 `GET /scan/jobs/{job_id}` 的 `result` 字段：
```json
{
  "total_skus": 1,
  "successful_scans": 0,
  "failed_count": 1,
  "success_rate": 0.0,
  "notifications_sent": 0,
  "duration_seconds": 3.9,
  "new_in_stock_items": []
}
```

### `POST /scan/full` — 立即触发全量扫描（仅价格）

爬取全站分类页（PLP）发现所有 SKU 和价格，写入 MongoDB（含价格历史）。**不抓尺码库存**，速度较快。

**请求体**（可选）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `category` | string \| null | `null` | 只扫描单个分类，留空 = 全部分类。可选值见下方 |
| `plp_workers` | int | `12` | 分类翻页并发线程数，1-32 |

可选分类：`men-clothing`、`women-clothing`、`kids-clothing`、`men-shoes`、`women-shoes`、`kids-shoes`、`accessories`、`sale`

**请求示例**
```bash
# 全站
curl -X POST http://localhost:8080/scan/full -H "Content-Type: application/json" -d '{}'

# 只扫一个分类（用于测试，更快）
curl -X POST http://localhost:8080/scan/full \
  -H "Content-Type: application/json" \
  -d '{"category": "accessories", "plp_workers": 8}'
```

**响应 200**
```json
{
  "job_id": "409e1d1c-4b67-409f-a58d-6f282714f483",
  "status": "queued",
  "message": "全量扫描（仅价格）任务已排队，可通过 GET /scan/jobs/409e1d1c-4b67-409f-a58d-6f282714f483 查询进度"
}
```

### `POST /scan/full-with-sizes` — 立即触发全量扫描（含尺码库存）

在全量扫描的基础上，为每个发现的 SKU 补充尺码库存（等价于 CLI 脚本 `fetch_all_skus_and_sizes.py` 不加 `--no-sizes` 参数的完整流程）。**这是定时任务每 30 分钟应该调用的接口。**

请求体、请求示例与 `/scan/full` 相同，路径不同：`POST /scan/full-with-sizes`。

10,000-20,000 个 SKU 预计耗时 10-20 分钟。

> **注意**：全量扫描第一步需要获取 Adidas 站点当前的 Next.js `buildId`（通过 Playwright 打开真实浏览器页面探测），这一步依赖出站网络能否正常加载 adidas.com 完整页面。如果部署环境网络受限或被目标站点的反爬策略拦截，这一步可能超时失败（任务状态会变为 `failed`，`error` 字段会说明原因）。首次探测成功后，buildId 会缓存到本地 `.build_id_cache` 文件，后续扫描会优先复用缓存，减少 Playwright 调用次数。

### `GET /scan/jobs/{job_id}` — 查询任务状态

```bash
curl http://localhost:8080/scan/jobs/409e1d1c-4b67-409f-a58d-6f282714f483
```

**响应 200**
```json
{
  "job_id": "409e1d1c-4b67-409f-a58d-6f282714f483",
  "type": "full_scan",
  "status": "running",
  "stage": "plp",
  "message": "开始抓取分类: accessories",
  "created_at": "2026-08-05T03:27:49.958194",
  "started_at": "2026-08-05T03:27:49.959254",
  "finished_at": null,
  "result": null,
  "error": null
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| `type` | `watch_scan` \| `full_scan` \| `full_scan_with_sizes` |
| `status` | `queued` → `running` → `success` \| `failed` |
| `stage` | 全量扫描的当前阶段：`build_id` / `plp` / `plp_retry` / `dedupe` / `mongo_price` / `sizes` / `mongo_sizes` / `done` |
| `result` | 成功后的汇总结果（见下方各接口示例），失败/进行中为 `null` |
| `error` | 失败原因，仅 `status=failed` 时有值 |

`full_scan` / `full_scan_with_sizes` 成功后的 `result` 示例：
```json
{
  "batch_id": "price_20260805_112749",
  "total_skus": 812,
  "categories": {"accessories": 812},
  "with_sale": 130,
  "sold_out": 45,
  "price_range": [8.0, 220.0],
  "include_sizes": false
}
```

**响应 404** — job_id 不存在（服务重启后内存中的任务记录会清空）
```json
{"detail": "任务不存在"}
```

### `GET /scan/jobs?limit=20` — 列出最近的任务

```bash
curl http://localhost:8080/scan/jobs
```

返回按创建时间倒序的任务数组，结构同上，默认最多 20 条。

> 任务状态保存在服务进程内存中，**服务重启后会清空**。如需持久化任务历史，需要额外接入 Redis/数据库，目前未实现（用量不大，暂不需要）。

---

## Watch List

管理"我关注哪些货、哪些尺码，一有货就通知我"的列表。

### `POST /watch` — 添加监控项

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sku` | string | 是 | 商品 SKU |
| `size` | string | 是 | 尺码，如 `US 10` |
| `name` | string | 否 | 商品名称，仅用于通知展示 |
| `color` | string | 否 | 配色，仅用于通知展示 |

```bash
curl -X POST http://localhost:8080/watch \
  -H "Content-Type: application/json" \
  -d '{"sku": "JR5408", "size": "US 10", "name": "Adidas Ultraboost 22", "color": "White/Black"}'
```

**响应 200**
```json
{
  "id": "58622061-d6b6-40ea-999d-9bbeebf9a376",
  "sku": "JR5408",
  "size": "US 10",
  "name": "Adidas Ultraboost 22",
  "color": "White/Black",
  "created_at": "2026-08-05T03:33:23.345367",
  "enabled": true
}
```

### `GET /watch?enabled_only=true` — 列出监控项

```bash
curl http://localhost:8080/watch
curl "http://localhost:8080/watch?enabled_only=false"   # 含已禁用的
```

**响应 200** — `WatchItemResponse` 数组，结构同上。

### `DELETE /watch/{item_id}` — 删除监控项

```bash
curl -X DELETE http://localhost:8080/watch/58622061-d6b6-40ea-999d-9bbeebf9a376
```

**响应 200** `{"message": "观察项已删除"}` ｜ **响应 404** `{"detail": "观察项不存在"}`

### `PATCH /watch/{item_id}/disable` / `PATCH /watch/{item_id}/enable`

暂停/恢复某个监控项，不删除历史数据。

```bash
curl -X PATCH http://localhost:8080/watch/{item_id}/disable
curl -X PATCH http://localhost:8080/watch/{item_id}/enable
```

---

## Notifications

### `GET /notifications?sku=JR5408&limit=100`

查询已发送的通知历史（用于核对是否漏发/重复发）。

| 查询参数 | 类型 | 说明 |
|---------|------|------|
| `sku` | string，可选 | 只看某个 SKU |
| `limit` | int，默认 100 | 最多返回条数 |

```bash
curl http://localhost:8080/notifications
curl "http://localhost:8080/notifications?sku=JR5408&limit=10"
```

**响应 200**
```json
[
  {
    "id": "b1e2...",
    "sku": "JR5408",
    "size": "US 10",
    "status": "IN_STOCK",
    "message": "Adidas Ultraboost 22 [JR5408] US 10 有货",
    "sent_at": "2026-08-05T10:35:00"
  }
]
```

同一个 SKU+尺码 **2 小时内**只会记录/通知一次（`config.yaml` 里 `notification.duplicate_notification_hours` 可调）。

---

## Config

### `GET /config/slack` — 查看 Slack 配置状态

```bash
curl http://localhost:8080/config/slack
```

```json
{"enabled": false, "webhook_configured": false, "user_mention_enabled": false}
```

### `POST /config/slack` — 更新 Slack Webhook 配置

运行时热更新（不用重启服务），也可以直接改 `.env` 里的 `SLACK_WEBHOOK_URL` / `SLACK_USER_ID` 后重启服务生效。

| 查询参数 | 类型 | 必填 | 说明 |
|---------|------|------|------|
| `webhook_url` | string | 是 | Slack Incoming Webhook URL |
| `user_id` | string | 否 | Slack 用户 ID，用于 @提醒 |

```bash
curl -X POST "http://localhost:8080/config/slack?webhook_url=https://hooks.slack.com/services/XXX/YYY/ZZZ&user_id=U123456"
```

---

## 常见状态码

| 状态码 | 含义 |
|--------|------|
| 200 | 成功 |
| 400 | 请求参数问题（如监控列表为空又没传 skus） |
| 404 | 资源不存在（SKU 未找到 / 监控项 ID 不存在 / job_id 不存在） |
| 500 | 服务内部错误（如数据库未连接） |

## 本地快速验证清单

```bash
# 1. 健康检查
curl http://localhost:8080/health

# 2. 实时查一个 SKU
curl http://localhost:8080/products/JR5408

# 3. 加一个监控项
curl -X POST http://localhost:8080/watch -H "Content-Type: application/json" \
  -d '{"sku":"JR5408","size":"US 10","name":"测试","color":"White"}'

# 4. 扫描监控列表
curl -X POST http://localhost:8080/scan -H "Content-Type: application/json" -d '{}'

# 5. 触发一次小范围全量扫描（用 accessories 分类测试更快）
curl -X POST http://localhost:8080/scan/full \
  -H "Content-Type: application/json" -d '{"category":"accessories"}'

# 6. 查任务进度（把上一步返回的 job_id 换进去）
curl http://localhost:8080/scan/jobs/<job_id>
```
