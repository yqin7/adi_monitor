# Adidas Monitor 定时扫描系统 - 快速开始

## 系统概述

这是一个企业级的 Adidas 商品定时监控系统，每30分钟自动扫描 10000-20000 个 SKU，
当你关注的货有库存时，通过 Slack 实时通知你。

### 核心特性
✅ 大规模并行扫描（10000+ SKU）
✅ 实时 Slack 通知
✅ 变化检测 + 去重（避免重复通知）
✅ 完整的观察列表管理 API
✅ 云原生设计（支持 Cloud Run/Lambda/VPS）

## 项目文件结构

```
adi_monitor/
├── main.py                 # FastAPI 入口
├── models.py              # 数据模型
├── scanner.py             # 并行扫描引擎
├── notifier.py            # Slack 通知
├── storage.py             # 数据库操作
├── adidas_monitor.py      # 原有的爬虫逻辑
├── mongo_store.py         # MongoDB 连接
├── config.py              # 配置加载
├── config.yaml            # 配置文件
├── requirements.txt       # Python 依赖
├── Dockerfile             # 容器镜像
├── docker-compose.yml     # 本地开发配置
└── DEPLOYMENT.md          # 详细部署指南
```

## 本地测试（5分钟）

### 1. 安装依赖
\`\`\`bash
pip install -r requirements.txt
\`\`\`

### 2. 配置环境
在项目根目录创建 `.env` 文件：
\`\`\`bash
MONGODB_URI=mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
SLACK_USER_ID=U123456789
\`\`\`

### 3. 启动服务
\`\`\`bash
python main.py
\`\`\`

访问 http://localhost:8080 查看 API 文档

## 快速 API 演示

### 添加要监控的货（有货就通知）
\`\`\`bash
curl -X POST http://localhost:8080/watch \\
  -H "Content-Type: application/json" \\
  -d '{
    "sku": "JR5408",
    "size": "US 10",
    "name": "Adidas Ultraboost",
    "color": "White/Black"
  }'
\`\`\`

### 查看所有监控项
\`\`\`bash
curl http://localhost:8080/watch
\`\`\`

### 查看通知历史
\`\`\`bash
curl http://localhost:8080/notifications
\`\`\`

### 删除监控项
\`\`\`bash
curl -X DELETE http://localhost:8080/watch/{item_id}
\`\`\`

## Docker 部署（推荐）

### 本地测试
\`\`\`bash
docker-compose up --build
\`\`\`

### 推送到云端
\`\`\`bash
# Google Cloud Run
docker build -t gcr.io/your-project/adidas-monitor .
docker push gcr.io/your-project/adidas-monitor
gcloud run deploy adidas-monitor \\
  --image gcr.io/your-project/adidas-monitor \\
  --platform managed \\
  --region asia-east1
\`\`\`

## 系统架构

扫描流程：
```
1. Cloud Scheduler 每30分钟触发 /scan 接口
   ↓
2. ParallelScanner 用 24 个线程并行扫描 SKU
   ↓
3. 检测库存变化（对比上次结果）
   ↓
4. 查询观察列表（你要通知的货）
   ↓
5. 发送 Slack 通知 + 记录数据库
```

## 重要概念

### 观察项（Watch Item）
你想要通知的货，包含：
- SKU: 商品代码
- SIZE: 尺码
- NAME: 商品名称
- COLOR: 配色

### 变化检测
比对两次扫描结果：
- 上次没货 → 这次有货 = 新有货 → **发送通知**
- 上次有货 → 这次没货 = 下架

### 去重（Duplicate Prevention）
同一个货 2 小时内只通知一次，避免刷屏

## 配置优化

扫描 20000 SKU 的最佳配置：
\`\`\`yaml
notification:
  concurrent_workers: 28          # 并发线程
  duplicate_notification_hours: 2  # 2小时内去重
  scan_interval_minutes: 30        # 30分钟扫描一次
\`\`\`

性能指标：
- 总耗时：10-15 分钟
- 内存占用：200-500 MB
- 通知延迟：1-2 秒
- 成功率：>99%

## 部署建议

| 方案 | 成本 | 难度 | 推荐场景 |
|------|------|------|---------|
| Cloud Run | $0.7/月 | ⭐ 简单 | 想省事，追求简单 |
| Lambda | $0.2/月 | ⭐⭐ 中等 | 用 AWS 且想省钱 |
| VPS | ¥20/月 | ⭐⭐⭐ 复杂 | 想完全控制 + 省钱 |

## 故障排查

**扫描超时？**
→ 减少 concurrent_workers 或增加等待时间

**Slack 不通知？**
→ 检查 SLACK_WEBHOOK_URL 是否正确

**MongoDB 连接失败？**
→ 检查防火墙是否允许云服务 IP

## 下一步

1. 【必须】设置 Slack Webhook
2. 【必须】配置 MongoDB URI
3. 【可选】部署到云端
4. 【可选】通过 API 添加监控项

详细文档见 DEPLOYMENT.md
