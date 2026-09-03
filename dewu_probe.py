#!/usr/bin/env python3
"""得物开放平台接口路径探测器

沙箱网关对存在的路径返回 mock 数据，对不存在的路径返回
「请求接口不正确」，据此可以在没有文档的情况下定位接口路径。
（bidding/lowest_price 就是这样找到的。）

用法:
    export DEWU_SANDBOX_APP_KEY=xxx DEWU_SANDBOX_APP_SECRET=yyy
    python dewu_probe.py dop/api/v1/bidding/{list,detail,lowest_price}
    python dewu_probe.py --module bidding --actions list detail price
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

from dewu_client import DewuClient

MISS_MARKER = "请求接口不正确"


def probe_paths(client: DewuClient, paths: List[str],
                workers: int = 10) -> List[Tuple[str, str]]:
    """并发探测一批路径，返回命中的 (path, 响应片段)"""

    def probe(path: str):
        try:
            resp = client.call(path, {"page_no": 1, "page_size": 10})
            text = json.dumps(resp, ensure_ascii=False)
            return None if MISS_MARKER in text else (path, text[:200])
        except Exception as e:  # 网络抖动不算命中
            print(f"  ! {path}: {e}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        return [r for r in executor.map(probe, sorted(set(paths))) if r]


def main():
    parser = argparse.ArgumentParser(description="得物接口路径探测")
    parser.add_argument("paths", nargs="*", help="要探测的完整路径")
    parser.add_argument("--module", help="模块名，与 --actions 组合成 dop/api/v1/<module>/<action>")
    parser.add_argument("--actions", nargs="*", default=[], help="动作名列表")
    parser.add_argument("--workers", type=int, default=10, help="并发数")
    args = parser.parse_args()

    paths = list(args.paths)
    if args.module:
        paths += [f"dop/api/v1/{args.module}/{a}" for a in args.actions]
    if not paths:
        parser.error("至少要给一个路径，或用 --module/--actions 组合")

    client = DewuClient(
        os.getenv("DEWU_SANDBOX_APP_KEY", ""),
        os.getenv("DEWU_SANDBOX_APP_SECRET", ""),
        sandbox=True,
    )

    hits = probe_paths(client, paths, args.workers)
    for path, preview in hits:
        print(f"HIT  {path}\n     {preview}")
    print(f"\n{len(set(paths))} 个候选，{len(hits)} 个命中")


if __name__ == "__main__":
    main()
