# 部署指南

## 配置管理

所有环境使用统一的 `config.yaml` 文件。敏感信息（MongoDB URI）通过环境变量注入。

```yaml
# config.yaml
mongodb:
  uri: "${MONGODB_URI}"  # 从环境变量读取
```

### 本地运行

```bash
$env:MONGODB_URI="mongodb+srv://username:password@cluster.mongodb.net/..."
python fetch_all_skus_and_sizes.py --no-sizes
```

### 云服务器部署

设置环境变量后运行相同命令：
```bash
export MONGODB_URI="mongodb+srv://..."
python fetch_all_skus_and_sizes.py --no-sizes
```

## 云服务器部署

### 1. 环境变量配置

在云服务器上设置以下环境变量：

```bash
# MongoDB 连接
export MONGODB_URI="mongodb+srv://username:password@cluster.mongodb.net/?retryWrites=true&w=majority"

# 应用环境
export APP_ENV="prod"

# Python IO 编码（Windows 可选）
export PYTHONIOENCODING="utf-8"
```

### 2. Docker 部署示例

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
RUN playwright install chromium

# 设置环境变量
ENV APP_ENV=prod
ENV PYTHONIOENCODING=utf-8

CMD ["python", "fetch_all_skus_and_sizes.py", "--no-sizes"]
```

### 3. 定时任务配置

#### Linux Cron（每天凌晨2点运行）

```bash
0 2 * * * cd /app/adidas_monitor && /usr/bin/python3 fetch_all_skus_and_sizes.py --no-sizes >> /var/log/adidas_monitor.log 2>&1
```

#### Windows 任务计划程序

```
触发器：每天 02:00
操作：运行程序
程序：C:\Python310\python.exe
参数：C:\path\to\fetch_all_skus_and_sizes.py --no-sizes
工作目录：C:\path\to\adi_monitor
环境变量：APP_ENV=prod
```

### 4. 性能对比

| 参数 | local | prod |
|------|-------|------|
| PLP Workers | 8 | 12 |
| AVAIL Workers | 16 | 24 |
| 目标运行时间 | ~20分钟 | ~12分钟 |

**生产环境配置针对云服务器优化，更高并发但对目标服务器压力更大，建议监控并调整。**

## 故障排查

### MongoDB 连接失败

检查环境变量是否正确设置：
```bash
echo $MONGODB_URI
```

### 线程数过高导致超时

降低 `avail_workers` 和 `plp_workers`：
```yaml
scraper:
  plp_workers: 6
  avail_workers: 12
```

### 内存溢出

减少 `write_batch_size`：
```yaml
app:
  write_batch_size: 2000
```

## 监控建议

1. **日志收集**：使用 ELK Stack 或云服务商的日志服务
2. **性能监控**：监控爬虫运行时间和 MongoDB 写入速度
3. **告警设置**：
   - 爬虫运行超时告警
   - MongoDB 连接失败告警
   - 磁盘空间告警
