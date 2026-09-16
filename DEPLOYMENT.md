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

## GitHub Actions 自动部署（推荐）

发布流程：功能分支合并到 `main` → 本地拉取 `main` → 从 `main` 切 `release-x.y.z` 分支推上去 → 自动：跑测试 → 构建镜像推到 GHCR（`ghcr.io/yqin7/adi_monitor`，标签 `latest` / `release-x.y.z` / 短哈希）→ SSH 到服务器拉起。push 到 `main` 本身不部署。

```bash
git checkout main && git pull
git checkout -b release-1.0.0
git push -u origin release-1.0.0      # 触发部署
```
流程在 `.github/workflows/deploy.yml`，不依赖具体云厂商，一台装了 Docker 的机器即可。

### 一次性准备

1. **服务器**（阿里云 / 火山引擎 / AWS 都行）：固定公网 IP（得物 POIZON 有 IP 白名单，出口 IP 要加进去）；装 Docker 与 compose 插件；
   建目录 `/opt/adi_monitor`，放入本仓库的 `docker-compose.prod.yml` 和一份 `.env`（按 `.env.example` 填 `MONGODB_URI`、`DEWU_INTL_APP_KEY/SECRET`、Slack）。
   Atlas 的 Network Access 也要加这台机器的 IP。
2. **GitHub Secrets**（仓库 Settings → Secrets and variables → Actions）：

   | Secret | 内容 |
   |---|---|
   | `DEWU_MONITOR_TOKEN` | fine-grained PAT，仅授权 `yqin7/dewu-monitor` 的 Contents: Read（构建镜像时安装私有依赖） |
   | `DEPLOY_HOST` | 服务器 IP / 域名 |
   | `DEPLOY_USER` | SSH 用户，需在 docker 组 |
   | `DEPLOY_SSH_KEY` | 该用户的 SSH 私钥全文 |

   前三步没配 `DEPLOY_*` 时，测试和构建照常跑、只跳过部署，可以先把镜像流水线跑通再买机器。
3. GHCR 镜像默认私有，服务器拉取用的是工作流的 `GITHUB_TOKEN`，不用另配。

### 本地构建镜像

```bash
printf '%s' "$GITHUB_PAT" > /tmp/gh_token
DOCKER_BUILDKIT=1 docker build --secret id=gh_token,src=/tmp/gh_token -t adi_monitor .
```

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
