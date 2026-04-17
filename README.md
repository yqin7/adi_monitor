# Adidas Monitor

用于抓取 Adidas US 官网当前商品的 SKU、价格、折扣、分类，以及尺码库存。

当前项目保留 4 个正式脚本：

- `fetch_all_skus_and_sizes.py`
- `fetch_sizes.py`
- `adidas_monitor.py`
- `fetch_build_id.py`

## 环境依赖

- Python 3.10+
- `curl_cffi`

安装依赖：

```bash
pip install curl_cffi
```

## 目录说明

- `result/`
  存放抓取结果 JSON
- `.build_id_cache`
  缓存最近可用的 Adidas Next.js `buildId`

## 脚本总览

### `fetch_all_skus_and_sizes.py`

主脚本。先从 Adidas PLP 接口抓全站商品列表，再按 SKU 抓尺码和库存。

适用场景：

- 第一次全量抓取
- 定时跑整站价格 + 尺码库存
- 结合 `--prev` 做增量刷新

默认抓取分类：

- `men-clothing`
- `women-clothing`
- `kids-clothing`
- `men-shoes`
- `women-shoes`
- `kids-shoes`
- `accessories`
- `sale`

默认输出：

- `result/adidas_prices_YYYYMMDD_HHMM.json`

### `fetch_sizes.py`

独立尺码刷新脚本。读取已有 JSON，只补充或刷新 `sizes`，不重新抓 PLP 商品列表。

当前正式方案会复用 `adidas_monitor.py` 的单 SKU 查询逻辑，并以多线程方式抓取尺码。

适用场景：

- 已经有一份商品列表 JSON
- 只想补尺码库存
- 想调不同并发 / 速率反复测试尺码接口

### `adidas_monitor.py`

单 SKU 查询/监控脚本。适合手动查某几个商品。

适用场景：

- 调试某个 SKU
- 人工验证价格和库存
- 持续 watch 某几个目标商品

### `fetch_build_id.py`

独立获取 Adidas 最新 `buildId` 的脚本。

适用场景：

- `fetch_all_skus_and_sizes.py` 提示当前 `buildId` 无效
- 想提前刷新 `.build_id_cache`
- 想手动查看当前站点最新 `buildId`

## `fetch_all_skus_and_sizes.py` 用法

### 基本命令

```bash
python fetch_all_skus_and_sizes.py
```

只抓商品价格，不抓尺码：

```bash
python fetch_all_skus_and_sizes.py --no-sizes
```

只抓单个分类：

```bash
python fetch_all_skus_and_sizes.py --category men-shoes
python fetch_all_skus_and_sizes.py --category accessories
python fetch_all_skus_and_sizes.py --category sale
```

先刷新 `buildId` 缓存：

```bash
python fetch_build_id.py
```

手动指定 `buildId`：

```bash
python fetch_all_skus_and_sizes.py --build-id your_build_id
```

指定输出文件：

```bash
python fetch_all_skus_and_sizes.py --out result/all_products.json
```

使用上一轮结果做增量复用：

```bash
python fetch_all_skus_and_sizes.py --prev result/adidas_prices_20260416_1408.json
```

### 参数说明

- `--build-id`
  手动传入 Adidas Next.js `buildId`。不传时只读取 `.build_id_cache`。
- `--category`
  只抓一个分类 slug。
- `--out`
  输出文件路径。默认自动生成带时间戳文件名。
- `--no-sizes`
  跳过尺码/库存抓取，只保存商品列表和价格。
- `--prev`
  传入上一轮结果文件，启用智能增量复用。

### 输出字段

主脚本输出的每个商品大致包含：

- `sku`
- `name`
- `subtitle`
- `category`
- `url`
- `sale_price`
- `orig_price`
- `discount_pct`
- `is_sold_out`
- `colour_variations`
- `rating`
- `rating_count`
- `sizes`
- `scraped_at`

### 抓取逻辑

1. 先获取或复用 `buildId`
2. 通过 `/plp-app/_next/data/...` 拉各分类商品列表
3. 合并去重 SKU
4. 先保存一版不含尺码的价格快照
5. 再调用 `/api/products/{sku}/availability` 补 `sizes`

### 404 / 429 行为

- `404`
  视为商品下架、卖完或不可用，直接跳过，不再重试。
- `429`
  触发全局冷却，并按指数退避重试。

## `fetch_sizes.py` 用法

### 基本命令

读取已有结果并补尺码：

```bash
python fetch_sizes.py result/adidas_prices_20260416_1408.json
```

输出到新文件：

```bash
python fetch_sizes.py result/adidas_prices_20260416_1408.json --out result/with_sizes.json
```

强制重拉全部 SKU：

```bash
python fetch_sizes.py result/adidas_prices_20260416_1408.json --force
```

只处理前 200 个 SKU：

```bash
python fetch_sizes.py result/adidas_prices_20260416_1408.json --limit 200
```

自定义并发和速率：

```bash
python fetch_sizes.py result/adidas_prices_20260416_1408.json --workers 32 --retries 4 --min-sleep 0.6 --max-sleep 1.0
```*** End Patch```}ிjson to=functions.ApplyPatch अंत  әһвал  assistant to=functions.ApplyPatch +#+#+#+#+#+commentary ैम*** Begin Patch

### 参数说明

- `input`
  输入 JSON 文件路径。
- `--out`
  输出文件路径；不传则覆盖原文件。
- `--force`
  强制重拉全部 SKU，忽略已有 `sizes` 和 `is_sold_out`。
- `--limit`
  只处理前 N 个 SKU，便于测试。
- `--workers`
  并发线程数。
- `--retries`
  单 SKU 最大重试次数。
- `--min-sleep`
  每个线程在成功请求后的最小随机等待秒数。
- `--max-sleep`
  每个线程在成功请求后的最大随机等待秒数。

### 默认行为

不加 `--force` 时：

- 已售罄商品会跳过
- 已有有效 `sizes` 的商品会复用
- 低库存 / `LOW_STOCK` 商品会重新拉取

## `adidas_monitor.py` 用法

### 基本命令

查一个 SKU：

```bash
python adidas_monitor.py JR5408
```

查多个 SKU：

```bash
python adidas_monitor.py JR5408 JR5410
```

JSON 输出：

```bash
python adidas_monitor.py IJ7058 --json
```

持续监控：

```bash
python adidas_monitor.py --watch IJ7058
python adidas_monitor.py --watch IJ7058 JR5408 --interval 60
```

### 参数说明

- `skus`
  一个或多个 SKU。不传时默认 `JR5408`。
- `--watch`
  持续监控模式。
- `--interval`
  监控刷新间隔，单位秒，默认 `300`。
- `--json`
  以 JSON 格式输出。

## 推荐工作流

### 方案 1：全量抓取

```bash
python fetch_all_skus_and_sizes.py
```

### 方案 2：先抓价格，后补尺码

```bash
python fetch_all_skus_and_sizes.py --no-sizes
python fetch_sizes.py result/adidas_prices_20260416_1408.json
```

### 方案 3：每日增量更新

```bash
python fetch_all_skus_and_sizes.py --prev result/adidas_prices_20260416_1408.json
```

### 方案 4：手动验证单个商品

```bash
python adidas_monitor.py IJ7058
```

## 常见问题

### 1. 为什么会有 `404`

通常表示：

- SKU 已下架
- 商品不存在
- 商品暂时不可用

当前逻辑中 `404` 不会重试。

### 2. 为什么会有 `429`

表示触发 Adidas 接口限流。可以尝试：

- 降低 `--workers`
- 降低 `--rate`
- 分批次跑

### 3. `buildId` 失效怎么办

先运行：

```bash
python fetch_build_id.py
```

或者手动传入新的：

```bash
python fetch_all_skus_and_sizes.py --build-id <id>
```

## `fetch_build_id.py` 用法

### 基本命令

```bash
python fetch_build_id.py
```

可见浏览器模式：

```bash
python fetch_build_id.py --headed
```

指定页面 URL：

```bash
python fetch_build_id.py --url https://www.adidas.com/us/accessories
```

### 参数说明

- `--url`
  自定义要尝试的页面 URL，可多次传入。
- `--timeout`
  页面加载超时，单位毫秒。
- `--headed`
  使用可见浏览器打开页面。
- `--no-cache`
  只输出结果，不写入 `.build_id_cache`。

### 4. 输出文件会不会被覆盖

- `fetch_all_skus_and_sizes.py`
  默认不会，文件名带时间戳
- `fetch_sizes.py`
  默认会覆盖输入文件，除非你传 `--out`

## 备注

- 当前目标站点是 Adidas US
- 价格单位默认按接口返回，通常是 `USD`
- 项目里 `result/adidas_prices_20260416_1408.json` 是现有样例数据
