"""一键任务入口。

    python tasks.py all           全流程：五国 Adidas → 去重 → 得物报价 → 算利润
    python tasks.py adidas        只跑 Adidas（五国 SKU + 尺码），不碰得物
    python tasks.py dewu          只跑得物报价（跳过已知得物没有的货号）
    python tasks.py dewu-retry    重查历史未命中的货号（低频，捡漏新上架）
    python tasks.py compute       只重算利润（不发任何请求）
    python tasks.py status        看各集合当前数量

常用参数
    --sites us,kr,jp,gb,ca   指定站点（默认全部）
    --limit N                本轮最多处理多少货号（调试用）
    --market CN|HK           利润按哪个市场算，默认 CN
    --no-filter              算利润时不套用净利/ROI/月销门槛

设计要点
    · 货号跨站去重：得物报价按货号存，与站点无关。五国共 47k 个 SKU，
      去重后 32k，省掉 31% 的查询。
    · 未命中记忆：得物查不到的货号写入 match_status(found=false)，
      后续 dewu 命令自动跳过；只有 dewu-retry 会按冷却期重试它们。
    · 目录缓存：货号→globalSkuId 的映射存 dewu_sku_map，30 天内复用，
      省掉得物接口 140 的调用（那个接口没法批量，是最大开销）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

ALL_SITES = ["us", "kr", "jp", "gb", "ca"]


def _log(msg: str) -> None:
    print(msg, flush=True)


# ── Adidas ────────────────────────────────────────────────────────────────────

def run_adidas(sites: list[str]) -> dict:
    """抓取指定站点的 SKU 与尺码库存。"""
    from app.services import adidas_intl_service, us_sizes_service
    from app.services.full_scan_service import run_full_scan

    out: dict[str, dict] = {}
    for site in sites:
        t0 = time.time()
        if site == "us":
            # 美国站的 SKU 与尺码来自两个不同接口，需分两步
            _log(f"\n=== 🇺🇸 美国站：抓取 SKU ===")
            r = run_full_scan(include_sizes=False, progress_cb=lambda m: _log(f"  {m}"))
            _log(f"=== 🇺🇸 美国站：补全尺码 ===")
            z = us_sizes_service.run(progress=_log)
            out["us"] = {**(r or {}), "sizes": z.get("skus")}
        else:
            _log(f"\n=== {site.upper()} 站抓取 ===")
            out[site] = adidas_intl_service.run_site_scan(site, progress=_log)
        _log(f"  用时 {time.time() - t0:.0f}s")
    return out


# ── 得物 ──────────────────────────────────────────────────────────────────────

def run_dewu(limit: int | None = None, include_missed: bool = False) -> dict:
    """拉取得物报价。默认跳过 match_status 里已标记为『得物没有』的货号。"""
    from app.services.arbitrage_service import refresh_quotes
    return refresh_quotes(limit=limit, only_discounted=False,
                          include_missed=include_missed, progress=_log)


def run_compute(market: str = "CN", apply_filter: bool = True) -> dict:
    from app.services.arbitrage_service import compute
    return compute(market=market, apply_filter=apply_filter, progress=_log)


# ── 状态 ──────────────────────────────────────────────────────────────────────

def show_status() -> None:
    from app.dao.mongo_client import MongoConnection
    db = MongoConnection.from_environment(required=True).db
    prod, match = db["products"], db["match_status"]

    NAME = {"us": "🇺🇸 美国", "kr": "🇰🇷 韩国", "jp": "🇯🇵 日本",
            "gb": "🇬🇧 英国", "ca": "🇨🇦 加拿大"}
    _log(f"{'站点':<10}{'SKU':<9}{'打折':<8}{'促销码':<9}{'有尺码'}")
    _log("-" * 46)
    for s in ALL_SITES:
        n = prod.count_documents({"site": s})
        if not n:
            continue
        _log(f"{NAME[s]:<10}{n:<9}"
             f"{prod.count_documents({'site': s, 'sale_price': {'$ne': None}}):<8}"
             f"{prod.count_documents({'site': s, 'promo_rate': {'$ne': None}}):<9}"
             f"{prod.count_documents({'site': s, 'available_sizes': {'$exists': True, '$ne': []}})}")

    uniq = len({d["sku"] for d in prod.find({}, {"sku": 1})})
    tried = match.count_documents({})
    hit = match.count_documents({"found": True})
    _log("-" * 46)
    _log(f"去重后唯一货号   {uniq}")
    _log(f"得物已查         {tried}   命中 {hit} / 未命中 {tried - hit}")
    _log(f"待查货号         {uniq - tried}")
    _log(f"dewu_quotes      {db['dewu_quotes'].count_documents({})}")
    _log(f"dewu_sku_map     {db['dewu_sku_map'].count_documents({})}  (目录缓存)")
    _log(f"arbitrage        {db['arbitrage'].count_documents({})}  (套利机会)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Adidas × 得物 比价任务",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("command",
                    choices=["all", "adidas", "dewu", "dewu-retry", "compute", "status"])
    ap.add_argument("--sites", default=",".join(ALL_SITES),
                    help="站点，逗号分隔，默认 us,kr,jp,gb,ca")
    ap.add_argument("--limit", type=int, default=None, help="本轮最多处理多少货号")
    ap.add_argument("--market", default="CN", choices=["CN", "HK"])
    ap.add_argument("--no-filter", action="store_true", help="算利润时不套门槛")
    args = ap.parse_args(argv[1:])

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    sites = [s.strip() for s in args.sites.split(",") if s.strip() in ALL_SITES]
    t0 = time.time()

    if args.command == "status":
        show_status()

    elif args.command == "adidas":
        _log(f"抓取站点: {sites}")
        _log(f"\n完成: {run_adidas(sites)}")

    elif args.command == "dewu":
        _log(f"\n结果: {run_dewu(limit=args.limit)}")
        _log(f"\n{run_compute(args.market, not args.no_filter)}")

    elif args.command == "dewu-retry":
        _log("重查历史未命中的货号（按 30 天冷却期）")
        _log(f"\n结果: {run_dewu(limit=args.limit, include_missed=True)}")
        _log(f"\n{run_compute(args.market, not args.no_filter)}")

    elif args.command == "compute":
        _log(f"{run_compute(args.market, not args.no_filter)}")

    elif args.command == "all":
        _log(f"全流程，站点: {sites}")
        run_adidas(sites)
        _log("\n=== 得物报价（自动跨站去重、跳过已知未命中）===")
        _log(f"{run_dewu(limit=args.limit)}")
        _log(f"\n{run_compute(args.market, not args.no_filter)}")

    _log(f"\n总用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
