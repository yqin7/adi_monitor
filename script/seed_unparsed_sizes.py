"""把现存的「解析不了的尺码写法」登记为已知（一次性脚本）。

不做这一步的话，告警上线第一轮会一口气报出 1,300 多种存量写法 ——
噪音这么大，这个功能第一天就会被关掉。存量先压成 known，
之后只有【真正新冒出来的】写法才会打扰人。

    python script/seed_unparsed_sizes.py --dry-run
    python script/seed_unparsed_sizes.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dao.mongo_client import MongoConnection        # noqa: E402
from app.dao.unparsed_size_dao import UnparsedSizeDAO   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = MongoConnection.from_environment(required=True).db
    dao = UnparsedSizeDAO(db)
    dao.ensure_indexes()

    from app.core.sizing import normalize

    # ── 官网侧，按站分别登记 ──────────────────────────────────────────
    total_new = 0
    for site in ("us", "kr", "jp", "gb", "ca"):
        pairs = [(z, d["sku"])
                 for d in db["products"].find({"site": site},
                                              {"sku": 1, "available_sizes": 1})
                 for z in (d.get("available_sizes") or [])]
        distinct = {z for z, _ in pairs
                    if z and str(z).strip().upper() != "HIDDEN" and normalize(z) is None}
        print(f"  {site}: 扫描 {len(pairs)} 个尺码，{len(distinct)} 种解析不了")
        total_new += len(distinct)
        if not args.dry_run and pairs:
            dao.record("site", site, pairs, seed_as_known=True)

    # ── 得物侧 ────────────────────────────────────────────────────────
    pairs = [(z.get("size"), q["sku"])
             for q in db["dewu_quotes"].find({}, {"sku": 1, "sizes.size": 1})
             for z in (q.get("sizes") or []) if z.get("size")]
    distinct = {z for z, _ in pairs if normalize(z) is None}
    print(f"  dewu: 扫描 {len(pairs)} 个尺码，{len(distinct)} 种解析不了")
    total_new += len(distinct)
    if not args.dry_run and pairs:
        dao.record("dewu", None, pairs, seed_as_known=True)

    print(f"\n合计 {total_new} 种写法")
    if args.dry_run:
        print("dry-run，未写库")
        return 0

    print("汇总:", dao.summary())
    left = db["unparsed_sizes"].count_documents({"status": "new"})
    print(f"状态为 new（会触发告警）的还有: {left}  <- 应为 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
