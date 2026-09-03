#!/usr/bin/env python3
"""得物开放平台 (open.dewu.com) 调用客户端

签名规则（实测确认）：
    sign = MD5("k1=v1&k2=v2&..." + app_secret).upper()
参数按 key 字典序排序，值为空的参数与 sign 自身不参与签名。
（算法已由沙箱网关回显的 signStr 实测校验）

用法:
    export DEWU_APP_KEY=xxx DEWU_APP_SECRET=yyy
    python dewu_client.py --path dop/api/v1/spu/price --params '{"spuId": 123}'
    python dewu_client.py --sandbox --path ... --params ...
"""

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict

import requests

PROD_GATEWAY = "https://openapi.dewu.com"
SANDBOX_GATEWAY = "https://openapi-sandbox.dewu.com"


def make_sign(params: Dict[str, Any], app_secret: str) -> str:
    """生成得物开放平台签名

    签名串 = 参数按 key 字典序排序后拼成 "k1=v1&k2=v2&..."，再直接拼接 app_secret，
    取 MD5 后转大写。sign 自身与空值参数不参与签名。
    （算法由网关返回的 signStr 回显实测确认）
    """
    parts = []
    for key in sorted(params):
        if key == "sign":
            continue
        value = params[key]
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{key}={value}")
    raw = "&".join(parts) + app_secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


class DewuClient:
    def __init__(self, app_key: str, app_secret: str, sandbox: bool = False,
                 timeout: int = 20):
        if not app_key or not app_secret:
            raise ValueError("缺少 app_key / app_secret")
        self.app_key = app_key
        self.app_secret = app_secret
        self.gateway = SANDBOX_GATEWAY if sandbox else PROD_GATEWAY
        self.timeout = timeout

    def call(self, path: str, biz_params: Dict[str, Any] = None) -> Dict[str, Any]:
        """调用一个开放平台接口

        Args:
            path: 接口路径，如 "dop/api/v1/spu/price"（以文档为准）
            biz_params: 业务参数
        """
        payload = dict(biz_params or {})
        payload["app_key"] = self.app_key
        payload["timestamp"] = int(time.time() * 1000)
        payload["sign"] = make_sign(payload, self.app_secret)

        url = f"{self.gateway}/{path.lstrip('/')}"
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        try:
            return resp.json()
        except ValueError:
            return {"http_status": resp.status_code, "text": resp.text[:2000]}


def main():
    parser = argparse.ArgumentParser(description="得物开放平台接口调用")
    parser.add_argument("--path", required=True, help="接口路径，以官方文档为准")
    parser.add_argument("--params", default="{}", help="业务参数 JSON 字符串")
    parser.add_argument("--sandbox", action="store_true", help="使用沙箱环境")
    args = parser.parse_args()

    app_key = os.getenv("DEWU_SANDBOX_APP_KEY" if args.sandbox else "DEWU_APP_KEY", "")
    app_secret = os.getenv("DEWU_SANDBOX_APP_SECRET" if args.sandbox else "DEWU_APP_SECRET", "")

    try:
        client = DewuClient(app_key, app_secret, sandbox=args.sandbox)
    except ValueError as e:
        print(f"错误: {e}，请先设置环境变量", file=sys.stderr)
        sys.exit(1)

    result = client.call(args.path, json.loads(args.params))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
