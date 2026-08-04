# Adidas Monitor

抓取 Adidas US 官网商品数据，并统一存入 MongoDB Atlas。项目不再把商品、价格或尺码数据保存为本地 JSON。

## 环境准备

- Python 3.10+
- `curl_cffi`
- `pymongo`
- `python-dotenv`
- Playwright Chromium（仅在 buildId 缓存缺失或失效时自动获取）

安装依赖：

```powershell
pip install curl_cffi pymongo python-dotenv playwright
playwright install chromium
```

在项目根目录创建 `.env`：

```env
MONGODB_URI=mongodb+srv://用户名:密码@集群地址/?retryWrites=true&w=majority
MONGODB_DATABASE=adidas_monitor
MONGODB_COLLECTION=products
```

`.env`、`.build_id_cache`、`__pycache__` 和 Python 字节码已加入 `.gitignore`。

## `fetch_all_skus_and_sizes.py` 主命令运行流程

直接运行：

```powershell
python fetch_all_skus_and_sizes.py
```

默认会依次：

1. 验证缓存中的 buildId；缺失或失效时自动获取新的 buildId。
2. 抓取 8 个内置分类的 SKU 和价格。
3. 按 SKU 去重并更新 MongoDB 的 `products`。
4. 将本次价格快照追加到 `price_history`，生成新的 `batch_id`。
5. 抓取尺码和库存；稳定尺码从 MongoDB 复用，低库存、新 SKU 和无尺码商品重新请求。
6. 将尺码结果更新回 `products`，不新增价格历史。

日常只更新 SKU 和价格，跳过较慢的尺码接口：

```powershell
python fetch_all_skus_and_sizes.py --no-sizes
```

只抓一个分类：

```powershell
python fetch_all_skus_and_sizes.py --category men-shoes --no-sizes
```
## 日常价格抓取

默认抓取全部固定分类的 SKU 和价格，直接写入 MongoDB：

```powershell
$env:PYTHONIOENCODING="utf-8"
python fetch_all_skus_and_sizes.py --no-sizes
```

这会：

1. 抓取各分类商品列表并按 SKU 去重。
2. 更新 `products` 中的最新商品和价格。
3. 向 `price_history` 追加本次价格快照。

只抓一个分类：

```powershell
python fetch_all_skus_and_sizes.py --category men-shoes --no-sizes
```

## 每日增量更新

价格和 SKU 的日常更新直接执行：

```powershell
python fetch_all_skus_and_sizes.py --no-sizes
```

脚本会把最新商品 upsert 到 MongoDB，并为本次价格抓取生成新的 `batch_id`。不使用上一轮本地文件，也不会生成本地 JSON。

如果需要更新部分重点商品的尺码：

```powershell
python fetch_sizes.py --sku JY8928 JR5408
```
## 重点商品尺码抓取

尺码接口较慢，日常不全站抓取。按 SKU 从 MongoDB 读取并更新：

```powershell
python fetch_sizes.py --sku JY8928 JR5408
```

只处理前几个 SKU：

```powershell
python fetch_sizes.py --sku JY8928 JR5408 --limit 1
```

强制刷新已有尺码：

```powershell
python fetch_sizes.py --sku JY8928 --force
```

全量抓取时如果不加 `--no-sizes`，脚本会从 MongoDB 读取上一轮尺码：稳定库存复用，低库存、新 SKU 和无尺码商品重新请求。

## MongoDB 数据结构

### `products`

每个 SKU 一条当前状态记录，保存最新商品信息、价格、折扣和可选的尺码库存。价格和商品字段会更新；尺码抓取只更新实际处理的 SKU。

### `price_history`

每次价格抓取追加一条记录，不覆盖旧价格。字段固定为：

```text
sku
batch_id
discount_pct
is_sold_out
name
observed_at
orig_price
sale_price
```

唯一约束为 `sku + batch_id`。批次号示例：`price_20260806_143000`。

## buildId

运行主脚本时会：

1. 验证命令行 buildId（如果传入）。
2. 验证 `.build_id_cache`。
3. 缓存缺失或失效时，自动打开 Adidas 页面探测新的 buildId。
4. 验证成功后写入缓存并继续抓取。

通常不需要手动运行：

```powershell
python fetch_build_id.py
```

## 单个商品即时查询

`adidas_monitor.py` 用于临时查询少量 SKU，不负责全站入库：

```powershell
python adidas_monitor.py JY8928
python adidas_monitor.py --watch JY8928 --interval 300
```

## 分类说明

当前内置分类为：

```text
men-clothing
women-clothing
kids-clothing
men-shoes
women-shoes
kids-shoes
accessories
sale
```

分类明细使用英文 slug。当前列表是固定的；如果 Adidas 官网新增商品分类，需要把新 slug 加入 `fetch_all_skus_and_sizes.py` 的 `CATEGORIES`，否则不会自动抓取。