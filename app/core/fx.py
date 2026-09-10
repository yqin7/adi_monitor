"""汇率获取：实时优先，配置兜底。

拆出来是因为原先有两处各写各的：
  * /arbitrage/fx 接口实时查 open.er-api / frankfurter，返回全套币种；
  * pricing 的配置兜底只有 usd_cny / usd_hkd / krw_cny。
日元、英镑、加元在兜底路径上直接缺失，前端拿不到就回落成 7.12 —— 日元成本
会虚高约 148 倍。现在两边共用这里，兜底也补齐全部站点币种。
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# 站点 -> 本币
SITE_CURRENCY = {"us": "USD", "kr": "KRW", "jp": "JPY", "gb": "GBP", "ca": "CAD"}

# 兜底汇率（1 美元 = ? 本币）。仅在两个实时源都失败时使用，
# 数量级对就行 —— 真要精确必须靠实时源。
FALLBACK_USD = {"CNY": 7.12, "HKD": 7.80, "KRW": 1380.0,
                "JPY": 148.0, "GBP": 0.79, "CAD": 1.37, "EUR": 0.92}

_CACHE: dict[str, Any] = {}
_TTL = 900          # 15 分钟：汇率日内波动对利润的影响远小于这个精度


def _fetch() -> dict[str, float] | None:
    import requests

    for name, url, pick in (
        ("open.er-api", "https://open.er-api.com/v6/latest/USD",
         lambda d: (d["rates"], d.get("time_last_update_utc", ""))),
        ("frankfurter",
         "https://api.frankfurter.app/latest?from=USD&to=CNY,HKD,KRW,JPY,GBP,CAD,EUR",
         lambda d: (d["rates"], d.get("date", ""))),
    ):
        try:
            d = requests.get(url, timeout=12).json()
            rates, ts = pick(d)
            if "CNY" not in rates:
                continue
            return {"_source": name, "_updated": ts,
                    **{k: float(v) for k, v in rates.items() if isinstance(v, (int, float))}}
        except Exception as exc:
            log.warning("汇率源 %s 失败: %s", name, exc)
    return None


def get_rates(force: bool = False) -> dict[str, Any]:
    """返回 {'USD_CNY':7.1, 'JPY_CNY':0.048, ..., 'live':bool, 'source':str}。

    键统一为 <本币>_CNY 与 usd_<小写币种>，两种写法前端都在用。
    """
    now = time.time()
    if not force and _CACHE and now - _CACHE.get("_at", 0) < _TTL:
        return _CACHE["data"]

    raw = _fetch()
    live = raw is not None
    usd = {k: v for k, v in (raw or {}).items() if not k.startswith("_")} or dict(FALLBACK_USD)

    cny = usd.get("CNY") or FALLBACK_USD["CNY"]
    out: dict[str, Any] = {
        "live": live,
        "source": (raw or {}).get("_source", "fallback"),
        "updated": (raw or {}).get("_updated", ""),
        "usd_cny": round(cny, 6),
        "usd_hkd": round(usd.get("HKD") or FALLBACK_USD["HKD"], 6),
    }
    # 各站本币 -> 人民币；以及 usd_<币种> 便于反查
    for cur in ("USD", "KRW", "JPY", "GBP", "CAD", "EUR", "HKD"):
        rate = usd.get(cur) if cur != "USD" else 1.0
        if not rate:
            rate = FALLBACK_USD.get(cur)
        if not rate:
            continue
        out[f"{cur.lower()}_cny"] = round(cny / rate, 8)
        out[f"usd_{cur.lower()}"] = round(rate, 6)
    out["usd_cny_rate"] = out["usd_cny"]

    _CACHE.update(_at=now, data=out)
    return out


def to_usd(amount: float, currency: str, rates: dict[str, Any] | None = None) -> float:
    """把站点本币金额折成美元。定价链路全程以美元为成本基准。"""
    if amount is None:
        return 0.0
    cur = (currency or "USD").upper()
    if cur == "USD":
        return float(amount)
    r = (rates or get_rates()).get(f"usd_{cur.lower()}")
    if not r:
        r = FALLBACK_USD.get(cur)
    return float(amount) / r if r else float(amount)
