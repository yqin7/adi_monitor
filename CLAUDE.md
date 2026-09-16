# adi_monitor — 项目规则

## 抓取只在云服务器上执行（硬性规则）

**本地绝不执行任何抓取，一个货号也不行。** 包括但不限于：

- 调本地服务的 `/scan`、`/scan/full*`、`/jobs/run/*`、`/arbitrage/refresh`、`/arbitrage/compute` 里带抓取的任务
- 运行 `script/fetch_all_skus_and_sizes.py`、`script/fetch_sizes.py`、`script/adidas_monitor.py`、`script/fetch_build_id.py`
- 直接 import 并调用 `full_scan_service` / `adidas_intl_service` / `us_sizes_service` / `arbitrage_service.refresh_quotes`
- 用 curl / curl_cffi / requests 直接请求 `adidas.*` 或 `open.poizon.com`（"测一下代理通不通"也不行）
- 本地起服务时开启 `SCHEDULER_ENABLED`

原因：抓取必须走固定的美国住宅代理和已加白名单的服务器 IP；本地跑会用本机 IP 触发 Adidas 风控、绕过得物 IP 白名单，并且和云上服务写同一个 Atlas 库互相覆盖。

需要触发或验证抓取时，在云服务器上执行（细节见本地文件 `docs/ops.local.md`，不在仓库里）：

```bash
ssh admin@<服务器IP> 'curl -s -X POST "localhost:8080/jobs/run/adidas?sites=us"'
ssh admin@<服务器IP> 'docker logs adi_monitor --since 10m 2>&1 | grep -vE "GET /(jobs|health)" | tail -50'
```

本地允许做的：读库分析（只读）、跑 `pytest`、起本地服务看页面（页面本身不抓取）、改代码。

## 发版流程

功能分支 → 合并到 `main` → 本地 `git pull` → 从 `main` 切 `release-x.y.z` 并推送 → GitHub Actions 自动测试、构建、部署。
`main` 不触发部署；**不要直接在 `release-*` 分支上提交**；日常改动放独立分支（如 `cloud-deploy`）。

## 配置

- `config.yaml` 打进镜像，只放不含秘密的业务参数（费率、并发数），秘密一律写 `${ENV_VAR}` 占位符
- 凭证、代理、调度开关放服务器 `/opt/adi_monitor/.env`，改完 `docker compose up -d` 即生效，不需要发版
- `docs/*.local.md` 是本地运维笔记，已 gitignore
