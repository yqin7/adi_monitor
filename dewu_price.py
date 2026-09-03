#!/usr/bin/env python3
"""得物商品价格查询

接口: GET /dop/api/v1/bidding/lowest_price（查询 SKU 最低出价）
    入参: sku_id（必填）
    返回: {"sku_id": ..., "items": [{"lowest_price": 129900, "bidding_type": 1}, ...]}

价格单位是「分」，129900 表示 ¥1299.00。
路径与出入参由沙箱环境实测确认。

用法:
    export DEWU_APP_KEY=xxx DEWU_APP_SECRET=yyy
    python dewu_price.py 2132132
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

from dewu_client import DewuClient

LOWEST_PRICE_PATH = "dop/api/v1/bidding/lowest_price"

# 出价类型，含义以官方文档为准
BIDDING_TYPE_NAMES = {
    0: "普通",
    1: "现货",
    2: "预售",
}


def fen_to_yuan(fen: int) -> float:
    """分转元"""
    return round(fen / 100, 2)


class DewuPriceQuery:
    """封装最低价查询"""

    def __init__(self, client: DewuClient, access_token: str = None):
        self.client = client
        self.access_token = access_token

    def get_lowest_price(self, sku_id: int) -> Dict[str, Any]:
        """查询单个 SKU 的最低出价

        Args:
            sku_id: 得物 SKU ID

        Returns:
            {"sku_id": ..., "prices": [{"bidding_type": 1, "type_name": "现货",
                                        "lowest_price_fen": 129900,
                                        "lowest_price": 1299.0}]}

        Raises:
            RuntimeError: 网关返回非 200
        """
        resp = self.client.call(
            LOWEST_PRICE_PATH,
            {"sku_id": sku_id},
            access_token=self.access_token,
            method="GET",
        )

        if resp.get("code") != 200:
            raise RuntimeError(
                f"查询 sku_id={sku_id} 失败: {json.dumps(resp, ensure_ascii=False)}"
            )

        data = resp.get("data") or {}
        prices = []
        for item in data.get("items", []):
            fen = item.get("lowest_price")
            bidding_type = item.get("bidding_type")
            prices.append({
                "bidding_type": bidding_type,
                "type_name": BIDDING_TYPE_NAMES.get(bidding_type, f"类型{bidding_type}"),
                "lowest_price_fen": fen,
                "lowest_price": fen_to_yuan(fen) if fen is not None else None,
            })

        return {"sku_id": data.get("sku_id", sku_id), "prices": prices}

    def get_lowest_price_of_type(self, sku_id: int,
                                 bidding_type: int) -> Optional[float]:
        """取指定出价类型的最低价（元），没有该类型时返回 None"""
        for p in self.get_lowest_price(sku_id)["prices"]:
            if p["bidding_type"] == bidding_type:
                return p["lowest_price"]
        return None

    def batch_lowest_price(self, sku_ids: List[int]) -> Dict[int, Any]:
        """逐个查询多个 SKU

        接口本身不支持批量，失败的 SKU 记录错误信息而不中断整批。
        """
        results = {}
        for sku_id in sku_ids:
            try:
                results[sku_id] = self.get_lowest_price(sku_id)
            except RuntimeError as e:
                results[sku_id] = {"sku_id": sku_id, "error": str(e)}
        return results


def _build_query(sandbox: bool, use_auth: bool) -> DewuPriceQuery:
    prefix = "DEWU_SANDBOX_" if sandbox else "DEWU_"
    client = DewuClient(
        os.getenv(f"{prefix}APP_KEY", ""),
        os.getenv(f"{prefix}APP_SECRET", ""),
        sandbox=sandbox,
    )
    token = None
    if use_auth:
        from dewu_oauth import DewuOAuth

        token = DewuOAuth(client).get_access_token()
    return DewuPriceQuery(client, access_token=token)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="得物 SKU 最低价查询")
    parser.add_argument("sku_ids", nargs="+", type=int, help="一个或多个 SKU ID")
    parser.add_argument("--sandbox", action="store_true", help="使用沙箱环境")
    parser.add_argument("--auth", action="store_true", help="附带本地缓存的 access_token")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    try:
        query = _build_query(args.sandbox, args.auth)
    except ValueError as e:
        print(f"错误: {e}，请先设置环境变量", file=sys.stderr)
        sys.exit(1)

    results = query.batch_lowest_price(args.sku_ids)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    for sku_id, r in results.items():
        if "error" in r:
            print(f"[{sku_id}] {r['error']}")
            continue
        print(f"\nSKU {r['sku_id']}")
        for p in r["prices"]:
            print(f"  {p['type_name']:<6} ¥{p['lowest_price']}")


if __name__ == "__main__":
    main()
