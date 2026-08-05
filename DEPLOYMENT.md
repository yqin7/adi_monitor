# Adidas Monitor 定时扫描系统 - 部署指南

这是一个支持大规模 SKU 扫描和 Slack 通知的微服务系统。

## 系统架构

```
Cloud Scheduler (30分钟触发一次)
    ↓
Cloud Run / Lambda / VPS 上的 Python FastAPI 服务
    ↓
MongoDB Atlas (存储快照和通知记录)
    ↓
Slack Webhook (发送通知)
```

## 核心功能

- ✅ 并行扫描 10000-20000 个 SKU（配置可调整并发数）
- ✅ 自动变化检测和去重（2小时内不重复通知）
- ✅ Slack 实时通知（有新货时 @mention）
- ✅ 观察列表管理（REST API）
- ✅ 完整的通知历史记录

## 快速开始

### 1. Docker 部署

```bash
# 构建镜像
docker build -t adidas-monitor .

# 运行容器
docker run -p 8080:8080 \
  -e MONGODB_URI="mongodb+srv://..." \
  -e SLACK_WEBHOOK_URL="https://hooks.slack.com/..." \
  adidas-monitor
```

### 2. 通过 docker-compose 启动

```bash
docker-compose up -d
```

### 3. 访问 API

http://localhost:8080 - 查看所有可用接口

## 关键 API

### 添加观察项（要通知的货）

```bash
POST /watch
{
  "sku": "JR5408",
  "size": "US 10",
  "name": "Adidas Ultraboost",
  "color": "White/Black"
}
```

### 列出所有观察项

```bash
GET /watch
```

### 删除观察项

```bash
DELETE /watch/{item_id}
```

### 查看通知历史

```bash
GET /notifications
```

### 手动触发扫描

```bash
POST /scan
```

## 云部署方案

### Google Cloud Run（推荐 - 最简单）

```bash
gcloud run deploy adidas-monitor \
  --source . \
  --platform managed \
  --region asia-east1 \
  --memory 2Gi \
  --set-env-vars MONGODB_URI=<uri>,SLACK_WEBHOOK_URL=<url>

# 创建定时任务（每30分钟）
gcloud scheduler jobs create http scan-job \
  --schedule "*/30 * * * *" \
  --http-method POST \
  --uri https://<cloud-run-url>/scan
```

### AWS Lambda + EventBridge

```bash
# 打包
zip -r lambda.zip . -x "*.git*" ".env" "*.pyc"

# 上传到 Lambda，设置环境变量
# 创建 EventBridge 规则：rate(30 minutes)
```

### 自建 VPS（最便宜）

```bash
# 用 docker-compose 在 VPS 上运行
docker-compose up -d

# 配置 cron 定时任务
*/30 * * * * curl -X POST http://localhost:8080/scan
```

## 性能参数

扫描 20000 SKU：
- 耗时：10-15 分钟
- 并发线程数：24-28
- 内存占用：200-500 MB
- 通知延迟：1-2 秒
