#!/usr/bin/env python3
"""得物商品查询与比价

以 adidas 货号为入口打通得物侧数据，接口与字段均为生产环境实测确认：

    POST /dop/api/v1/spu/batch_article_number   货号 -> spu + sku 列表
         入参 article_numbers（数组），返回 auth_price（官方指导价，单位分）
    POST /dop/api/v1/bidding/query_expect_income 给定出价算到手价
         入参 sku_id + bidding_price（分）+ bidding_type
    POST /dop/api/v1/nps/bidding_types           查询该商品支持的出价类型
         入参 spu_id + brand_id + category_id

关于「市场最低价」：/dop/api/v1/bidding/lowest_price 在网关上存在，
但不属于任何公开权限包（恒返回 5013 无权调用），官方 API 目录里也查无此项。
开放平台对 ISV 开放的价格维度是商家自身出价，不含其他商家的挂单价。

价格单位统一为「分」，129900 表示 ¥1299.00。

用法:
    export DEWU_APP_KEY=xxx DEWU_APP_SECRET=yyy
    python dewu_price.py JR5408
    python dewu_price.py JR5408 --income 129900
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

from dewu_client import DewuClient

ARTICLE_NUMBER_PATH = "dop/api/v1/spu/batch_article_number"
EXPECT_INCOME_PATH = "dop/api/v1/bidding/query_expect_income"
BIDDING_TYPES_PATH = "dop/api/v1/nps/bidding_types"

# 单次货号查询上限，超出自动分批
BATCH_SIZE = 20


def fen_to_yuan(fen: Optional[int]) -> Optional[float]:
    """分转元"""
    return None if fen is None else round(fen / 100, 2)


class DewuProductQuery:
    """货号 -> 商品 -> 价格"""

    def __init__(self, client: DewuClient, access_token: str):
        self.client = client
        self.access_token = access_token

    def _call(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.client.call(path, params, access_token=self.access_token)
        if resp.get("code") != 200:
            raise RuntimeError(json.dumps(resp, ensure_ascii=False))
        return resp

    def lookup_article_numbers(self, article_numbers: List[str]) -> List[Dict[str, Any]]:
        """按货号批量查商品

        Args:
            article_numbers: adidas 货号列表，如 ["JR5408"]

        Returns:
            每个商品含 spu_id、title、brand_name、auth_price(元) 与 skus
        """
        products = []
        for i in range(0, len(article_numbers), BATCH_SIZE):
            batch = article_numbers[i:i + BATCH_SIZE]
            resp = self._call(ARTICLE_NUMBER_PATH, {"article_numbers": batch})
            for item in resp.get("data") or []:
                products.append({
                    "article_number": item.get("article_number"),
                    "spu_id": item.get("spu_id"),
                    "title": item.get("title"),
                    "brand_id": item.get("brand_id"),
                    "brand_name": item.get("brand_name"),
                    "category_id": item.get("category_id"),
                    "category_name": item.get("category_name"),
                    "auth_price_fen": item.get("auth_price"),
                    "auth_price": fen_to_yuan(item.get("auth_price")),
                    "skus": [
                        {
                            "sku_id": s.get("sku_id"),
                            "properties": _parse_properties(s.get("properties")),
                            "status": s.get("status"),
                        }
                        for s in item.get("skus") or []
                    ],
                })
        return products

    def bidding_types(self, spu_id: int, brand_id: int,
                      category_id: int) -> List[Dict[str, Any]]:
        """查询该商品支持的出价类型，如 [{bidding_type:0, desc:"现货"}]"""
        resp = self._call(BIDDING_TYPES_PATH, {
            "spu_id": spu_id, "brand_id": brand_id, "category_id": category_id,
        })
        return [
            {"bidding_type": t.get("bidding_type"), "desc": t.get("bidding_type_desc")}
            for t in resp.get("data") or []
        ]

    def expect_income(self, sku_id: int, bidding_price_fen: int,
                      bidding_type: int = 0) -> Dict[str, Any]:
        """按给定出价算到手价与各项手续费

        Args:
            sku_id: 得物 SKU ID
            bidding_price_fen: 出价，单位分
            bidding_type: 出价类型，0 为现货

        Returns:
            各项费用与到手价，同时给出元为单位的换算
        """
        resp = self._call(EXPECT_INCOME_PATH, {
            "sku_id": sku_id,
            "bidding_price": bidding_price_fen,
            "bidding_type": bidding_type,
        })
        data = resp.get("data") or {}
        fees = {k: v for k, v in data.items() if isinstance(v, int)}
        return {
            "sku_id": sku_id,
            "bidding_type": bidding_type,
            "fen": fees,
            "yuan": {k: fen_to_yuan(v) for k, v in fees.items()},
        }


def _parse_properties(raw: Optional[str]) -> Dict[str, str]:
    """sku 的 properties 是个 JSON 字符串，解析成字典"""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def build_query(sandbox: bool = False) -> DewuProductQuery:
    """从环境变量与本地 token 缓存构造查询器"""
    from dewu_oauth import DewuOAuth

    prefix = "DEWU_SANDBOX_" if sandbox else "DEWU_"
    client = DewuClient(
        os.getenv(f"{prefix}APP_KEY", ""),
        os.getenv(f"{prefix}APP_SECRET", ""),
        sandbox=sandbox,
    )
    token = DewuOAuth(client).get_access_token()
    if not token:
        raise RuntimeError("本地没有可用的 access_token，请先完成 OAuth 授权")
    return DewuProductQuery(client, token)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="得物商品查询（按货号）")
    parser.add_argument("article_numbers", nargs="+", help="一个或多个货号，如 JR5408")
    parser.add_argument("--sandbox", action="store_true", help="使用沙箱环境")
    parser.add_argument("--income", type=int, metavar="FEN",
                        help="按该出价（分）计算每个 SKU 的到手价")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    try:
        query = build_query(args.sandbox)
        products = query.lookup_article_numbers(args.article_numbers)
    except (ValueError, RuntimeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(products, ensure_ascii=False, indent=2))
        return

    for p in products:
        print(f"\n{p['article_number']}  ->  spu {p['spu_id']}")
        print(f"  {p['title']}")
        print(f"  品牌: {p['brand_name']}   类目: {p['category_name']}")
        print(f"  指导价: ¥{p['auth_price']}")
        print(f"  尺码数: {len(p['skus'])}")
        for s in p["skus"][:5]:
            size = s["properties"].get("尺码", "")
            print(f"    sku {s['sku_id']}  {size}")
        if len(p["skus"]) > 5:
            print(f"    ... 另有 {len(p['skus']) - 5} 个")

        if args.income and p["skus"]:
            sku_id = p["skus"][0]["sku_id"]
            income = query.expect_income(sku_id, args.income)
            print(f"  按 ¥{fen_to_yuan(args.income)} 出价（sku {sku_id}）:")
            for k, v in income["yuan"].items():
                print(f"    {k:<26} ¥{v}")


if __name__ == "__main__":
    main()
