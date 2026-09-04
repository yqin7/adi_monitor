"""单品价格 & 尺码库存查询服务（直接请求 Adidas 官网，不经过数据库缓存）"""
import sys
from datetime import datetime

try:
    from curl_cffi import requests
except ImportError:
    print("请先安装依赖: pip install curl_cffi")
    sys.exit(1)

from app.core.config import load_config, get_requests_proxies

IMPERSONATE = "chrome131"

PRODUCT_URL = "https://www.adidas.com/api/products/{sku}?sitePath=us"
AVAIL_URL = "https://www.adidas.com/api/products/{sku}/availability?sitePath=us"

try:
    PROXIES = get_requests_proxies(load_config())
except Exception:
    PROXIES = None


def _get(url: str) -> dict:
    resp = requests.get(url, impersonate=IMPERSONATE, timeout=15, proxies=PROXIES)
    resp.raise_for_status()
    return resp.json()


def check_sku(sku: str) -> dict:
    """整合产品信息 + 库存，返回结构化结果"""
    sku = sku.upper().strip()

    detail = _get(PRODUCT_URL.format(sku=sku))
    avail = _get(AVAIL_URL.format(sku=sku))

    pi = detail.get("pricing_information", {})
    sale_price = pi.get("sale_price") or pi.get("currentPrice")
    original_price = pi.get("standard_price")
    if sale_price is None:
        for p in detail.get("price_information", []):
            if p["type"] == "sale":
                sale_price = p["value"]
            elif p["type"] == "original":
                original_price = original_price or p["value"]

    currency = detail.get("pricing_information", {}).get("currency", "USD")
    if currency == "USD" and not pi.get("currency"):
        currency = "USD"

    sizes = []
    for v in avail.get("variation_list", []):
        sizes.append({
            "sku":    v["sku"],
            "size":   v["size"],
            "status": v["availability_status"],
            "qty":    v.get("availability", 0),
        })

    in_stock = [s for s in sizes if s["status"] == "IN_STOCK"]

    color = detail.get("attribute_list", {}).get("color", "")
    if not color:
        desc = detail.get("product_description", {})
        color = desc.get("color", "")

    return {
        "sku":            sku,
        "name":           detail.get("name", ""),
        "color":          color,
        "currency":       currency,
        "original_price": original_price,
        "sale_price":     sale_price,
        "overall_status": avail.get("availability_status", "UNKNOWN"),
        "sizes":          sizes,
        "in_stock_sizes": in_stock,
        "checked_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
