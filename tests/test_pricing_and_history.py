"""操作费分档、价格变化点折叠、迁移合并 —— 纯函数，不连库。"""
from datetime import datetime, timezone

from app.core.pricing import DEFAULTS, is_apparel, operate_fee_cn, payout_cn
from app.dao.product_dao import ProductDAO
from script.compact_price_list import compact
from script.migrate_price_history_to_list import merge

CFG = {k: dict(v) for k, v in DEFAULTS.items()}


def test_is_apparel_accepts_dewu_and_adidas_categories():
    assert is_apparel("Apparel") and is_apparel("Women's Apparel")
    assert is_apparel("men-clothing") and is_apparel("kids-clothing")
    assert not is_apparel("Shoes") and not is_apparel("men-shoes")
    assert not is_apparel("Fashion Accessories, Eyewear & Apparel Accessories")
    assert not is_apparel(None) and not is_apparel("")


def test_operate_fee_tiers_on_sale_base():
    assert operate_fee_cn(400, CFG, "Apparel") == 38
    assert operate_fee_cn(399.99, CFG, "Apparel") == 28
    assert operate_fee_cn(300, CFG, "Apparel") == 28
    assert operate_fee_cn(299.99, CFG, "Apparel") == 18
    assert operate_fee_cn(100, CFG, "Shoes") == 38
    assert operate_fee_cn(100, CFG, None) == 38


def test_payout_cn_uses_tiered_fee_for_apparel_only():
    quoted = 299 / 1.155                      # 成交价 299 -> 服装 18，其他 38
    assert payout_cn(quoted, CFG, "Apparel")["operate_fee"] == 18
    assert payout_cn(quoted, CFG, "men-shoes")["operate_fee"] == 38


def _e(batch, sale, orig=110, sold=False):
    return {"batch_id": batch, "observed_at": datetime(2026, 9, 1), "sale_price": sale,
            "orig_price": orig, "discount_pct": None, "is_sold_out": sold}


def test_compact_keeps_first_of_each_price_run():
    pl = [_e("a", 55), _e("b", 55), _e("c", 77), _e("d", 77), _e("e", 66), _e("f", 66, sold=True)]
    assert [x["batch_id"] for x in compact(pl)] == ["a", "c", "e", "f"]
    assert compact([]) == []
    assert compact(compact(pl)) == compact(pl)


def test_price_key_ignores_batch_and_time():
    assert ProductDAO._price_key(_e("x", 55)) == ProductDAO._price_key(_e("y", 55))
    assert ProductDAO._price_key(_e("x", 55)) != ProductDAO._price_key(_e("x", 56))
    assert ProductDAO._price_key(None) == (None, None, False)


def test_merge_dedupes_by_batch_and_sorts_mixed_timestamps():
    naive = {"batch_id": "old", "observed_at": datetime(2026, 8, 4), "sale_price": 1}
    aware = {"batch_id": "new", "observed_at": datetime(2026, 9, 12, tzinfo=timezone.utc), "sale_price": 2}
    nodate = {"batch_id": None, "observed_at": None, "sale_price": 3}
    out = merge([naive, aware], [naive, nodate])
    assert [x["batch_id"] for x in out] == [None, "old", "new"]
    assert merge(out, [naive]) == out
