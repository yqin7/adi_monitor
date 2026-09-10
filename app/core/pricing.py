"""比价定价模型：采购成本、得物到手价、净利/ROI。

全部为纯函数，不依赖网络，参数来自 config.yaml 的 pricing 段。

采购侧（Adidas 美国）:
    成本USD = 标价 × promo折扣 × (1 - 返现合计) × (1 + 销售税) + 运费
    注意：返利门户（Rakuten / RetailMeNot）互斥，配置里 cashback_portal 只填最高的一个；
          银行卡返现可与其叠加。

卖出侧（得物）:
    国内 —— 海外查得价含税差，需 × (1 + 电商税) 得到国内成交基数，再扣各项费用
    香港 —— 直接按港币售价扣费，操作服务费按 350 HKD 分档
"""
from __future__ import annotations

from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    "fx": {"usd_cny": 7.12, "usd_hkd": 7.80, "krw_cny": 0.0052},
    "purchase": {"sales_tax": 0.0, "promo_rate": 1.0,
                 "cashback_bank": 0.0, "cashback_portal": 0.0},
    "sell_cn": {"tax_rate": 0.155, "tech_fee_rate": 0.05, "transfer_fee_rate": 0.01,
                "after_sale_rate": 0.025, "operate_fee": 38, "postage_subsidy": 10},
    "sell_hk": {"tech_fee_rate": 0.05, "tech_fee_min": 18, "transfer_fee_rate": 0.01,
                "threshold": 350, "operate_fee_low": 44, "operate_fee_high": 62},
    "shipping": {"enabled": False, "usd_per_item": 0.0},
    "filter": {"min_monthly_sales": 20, "min_profit_cny": 50, "min_roi": 0.15},
}


def get_pricing_config(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """取 pricing 配置，缺失项回落到 DEFAULTS。"""
    raw = (config or {}).get("pricing") or {}
    merged: Dict[str, Any] = {}
    for section, defaults in DEFAULTS.items():
        merged[section] = {**defaults, **(raw.get(section) or {})}
    return merged


# ---------------------------------------------------------------- 采购成本

def purchase_cost_usd(list_price_usd: float, cfg: Dict[str, Any],
                      promo_rate: float | None = None,
                      promo_code: str | None = None) -> Dict[str, Any]:
    """Adidas 标价 -> 实际采购成本（美元），返回各环节明细。

    promo_rate: 该商品实际适用的促销折扣（来自商品 badge，如 "Extra 30% Off with
                CODE: EXTRA" -> 0.70）。传 None 时回落到配置里的全局 promo_rate。
                Adidas 的促销码是逐商品标注的，没有 badge 的商品用不了码。
    """
    p = cfg["purchase"]
    ship = cfg["shipping"]

    rate = promo_rate if promo_rate is not None else p["promo_rate"]
    after_promo = list_price_usd * rate
    cashback_rate = p["cashback_bank"] + p["cashback_portal"]
    cashback = after_promo * cashback_rate
    tax = after_promo * p["sales_tax"]
    shipping = ship["usd_per_item"] if ship.get("enabled") else 0.0
    total = after_promo - cashback + tax + shipping

    return {
        "list_price_usd": round(list_price_usd, 2),
        "promo_code": promo_code,
        "promo_rate": rate,
        "after_promo_usd": round(after_promo, 2),
        "cashback_usd": round(cashback, 2),
        "sales_tax_usd": round(tax, 2),
        "shipping_usd": round(shipping, 2),
        "cost_usd": round(total, 2),
    }


# ---------------------------------------------------------------- 得物到手价

def payout_cn(dewu_price_cny: float, cfg: Dict[str, Any]) -> Dict[str, float]:
    """得物国内到手价。dewu_price_cny 为海外接口查得的价格（不含电商税）。"""
    s = cfg["sell_cn"]
    # 国内成交基数 = 接口查得的香港报价 × (1 + tax_rate)。
    # 系数由实测标定：2026-09-10 拿 B75806(鞋20码) + IJ7058(服装7码) 共 27 个
    # 尺码，对照得物 App 买家端渠道最低价，倍数均值 1.1547、标准差 0.0048
    # （变异系数 0.42%），过原点最小二乘 1.1540。取 1.155 时平均偏差 1.7 元。
    # 原先的 0.09 系统性低估约 6%，27 件累计少算 701 元。
    base = dewu_price_cny * (1 + s["tax_rate"])
    tech = base * s["tech_fee_rate"]
    transfer = base * s["transfer_fee_rate"]
    after_sale = base * s["after_sale_rate"]
    fixed = s["operate_fee"] + s["postage_subsidy"]
    payout = base - tech - transfer - after_sale - fixed

    return {
        "quoted_price": round(dewu_price_cny, 2),
        "sale_base": round(base, 2),
        "tech_fee": round(tech, 2),
        "transfer_fee": round(transfer, 2),
        "after_sale_fee": round(after_sale, 2),
        "operate_fee": float(s["operate_fee"]),
        "postage_subsidy": float(s["postage_subsidy"]),
        "payout": round(payout, 2),
        "currency": "CNY",
    }


def payout_hk(dewu_price_cny: float, cfg: Dict[str, Any]) -> Dict[str, float]:
    """得物香港到手价，全程以人民币计。

    要点：接口查到的报价本身就是【香港报价、人民币结算】，
    但 sell_hk 里的固定费与分档阈值（技术费下限 18、操作费 44/62、门槛 350）
    都是【港币】口径 —— 必须先折成人民币再扣，否则等于按 1:1 当人民币扣，
    每单多扣三四十块。前端早就按 hkd2cny 折算了，服务端这里一直没跟上。
    """
    s = cfg["sell_hk"]
    fx = cfg.get("fx", {})
    usd_cny = fx.get("usd_cny") or 7.12
    usd_hkd = fx.get("usd_hkd") or 7.80
    hkd2cny = usd_cny / usd_hkd if usd_hkd else 0.857

    tech = max(dewu_price_cny * s["tech_fee_rate"], s["tech_fee_min"] * hkd2cny)
    threshold_cny = s["threshold"] * hkd2cny
    operate = (s["operate_fee_low"] if dewu_price_cny < threshold_cny
               else s["operate_fee_high"]) * hkd2cny
    transfer = dewu_price_cny * s["transfer_fee_rate"]
    payout = dewu_price_cny - tech - operate - transfer

    return {
        "quoted_price": round(dewu_price_cny, 2),
        "sale_base": round(dewu_price_cny, 2),
        "tech_fee": round(tech, 2),
        "operate_fee": round(operate, 2),
        "transfer_fee": round(transfer, 2),
        "payout": round(payout, 2),
        "hkd2cny": round(hkd2cny, 4),
        "currency": "CNY",
    }


# ---------------------------------------------------------------- 净利

def evaluate(list_price_usd: float, dewu_price: float, cfg: Dict[str, Any],
             market: str = "CN", promo_rate: float | None = None,
             promo_code: str | None = None) -> Dict[str, Any]:
    """单个尺码的完整测算。

    market: "CN" 国内卖 | "HK" 香港卖
    返回统一以该市场币种计价的成本、到手、净利、ROI。
    """
    cost = purchase_cost_usd(list_price_usd, cfg, promo_rate, promo_code)
    fx = cfg["fx"]

    if market.upper() == "HK":
        # payout_hk 现在返回人民币，成本也必须用人民币，否则收入/成本两套币种
        payout = payout_hk(dewu_price, cfg)
        cost_local = cost["cost_usd"] * fx["usd_cny"]
    else:
        payout = payout_cn(dewu_price, cfg)
        cost_local = cost["cost_usd"] * fx["usd_cny"]

    profit = payout["payout"] - cost_local
    roi = profit / cost_local if cost_local else 0.0

    return {
        "market": market.upper(),
        "currency": payout["currency"],
        "cost": {**cost, "cost_local": round(cost_local, 2)},
        "payout": payout,
        "profit": round(profit, 2),
        "roi": round(roi, 4),
    }


def passes_filter(result: Dict[str, Any], monthly_sales: int | None,
                  cfg: Dict[str, Any]) -> bool:
    """是否值得推荐：净利、ROI、流动性三道门槛。"""
    f = cfg["filter"]
    if result["profit"] < f["min_profit_cny"]:
        return False
    if result["roi"] < f["min_roi"]:
        return False
    if (monthly_sales or 0) < f["min_monthly_sales"]:
        return False
    return True
