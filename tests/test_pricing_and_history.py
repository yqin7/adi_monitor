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
    # 离档位边界远一点，别靠浮点运气：成交价 ≈ 250 -> 18，≈ 350 -> 28，≈ 450 -> 38
    assert payout_cn(250 / 1.155, CFG, "Apparel")["operate_fee"] == 18
    assert payout_cn(350 / 1.155, CFG, "Apparel")["operate_fee"] == 28
    assert payout_cn(450 / 1.155, CFG, "Apparel")["operate_fee"] == 38
    assert payout_cn(250 / 1.155, CFG, "men-shoes")["operate_fee"] == 38


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


class _FakeColl:
    """只实现 _build_ops 用到的 find(filter, projection)。"""
    def __init__(self, docs):
        self.docs = docs

    def find(self, flt, proj):
        wanted = {(c["sku"], c["site"]) for c in flt["$or"]}
        for d in self.docs:
            if (d["sku"], d["site"]) in wanted:
                pl = d.get("price_list") or []
                yield {"sku": d["sku"], "site": d["site"], "price_list": pl[-1:]}


class _Op:
    def __init__(self, flt, update, upsert=False):
        self.flt, self.update = flt, update


def _dao_with(docs):
    dao = ProductDAO.__new__(ProductDAO)
    dao.collection = _FakeColl(docs)
    return dao


def test_build_ops_pushes_only_on_price_change_and_strips_insert_only_keys():
    dao = _dao_with([{"sku": "A", "site": "us", "price_list": [_e("old", 55)]}])
    now = datetime(2026, 9, 15)
    pending = [
        ("A", "us", {"sku": "A", "site": "us", "sale_price": 55, "created_at": "stale",
                     "price_list": ["stale"], "_id": "x", "first_seen_batch": "stale"}, _e("b1", 55)),
        ("B", "us", {"sku": "B", "site": "us", "sale_price": 10}, _e("b1", 10)),   # 库里没有 -> 新文档要 push
        ("C", "us", {"sku": "C", "site": "us"}, None),                             # 不记价格历史
    ]
    ops, keys, pushed = dao._build_ops(pending, now, "b1", _Op)
    assert keys == [("A", "us"), ("B", "us"), ("C", "us")] and pushed == 1
    assert "$push" not in ops[0].update                     # 价格没变
    assert "$push" in ops[1].update                         # 新商品
    assert "$push" not in ops[2].update
    for k in ("_id", "created_at", "first_seen_batch", "price_list"):
        assert k not in ops[0].update["$set"]
    assert ops[0].update["$setOnInsert"] == {"created_at": now, "first_seen_batch": "b1"}


def test_build_ops_same_product_twice_in_one_batch_compares_with_previous_push():
    dao = _dao_with([{"sku": "A", "site": "us", "price_list": [_e("old", 55)]}])
    pending = [("A", "us", {"sku": "A", "site": "us"}, _e("b1", 60)),
               ("A", "us", {"sku": "A", "site": "us"}, _e("b1", 60)),
               ("A", "us", {"sku": "A", "site": "us"}, _e("b1", 70))]
    _, _, pushed = dao._build_ops(pending, datetime(2026, 9, 15), "b1", _Op)
    assert pushed == 2


def test_is_delisted_uses_site_latest_scan_with_grace():
    from datetime import timedelta
    from app.dao.product_dao import is_delisted, DELISTED_AFTER
    latest = datetime(2026, 9, 15, 3, 13)
    assert is_delisted({"updated_at": datetime(2026, 9, 8, 2, 48)}, latest)          # 7 天没见到
    assert not is_delisted({"updated_at": latest - timedelta(hours=1)}, latest)      # 同一轮
    assert not is_delisted({"updated_at": latest - DELISTED_AFTER + timedelta(minutes=1)}, latest)
    assert not is_delisted({"updated_at": None}, latest) and not is_delisted({"updated_at": latest}, None)
    assert is_delisted({"updated_at": datetime(2026, 9, 8, tzinfo=timezone.utc)}, latest)  # aware 也行


def test_scrape_guard_blocks_without_env(monkeypatch):
    from app.core.guard import ScrapeNotAllowed, assert_scrape_allowed, scrape_allowed
    monkeypatch.delenv("SCRAPE_ALLOWED", raising=False)
    assert not scrape_allowed()
    import pytest
    with pytest.raises(ScrapeNotAllowed):
        assert_scrape_allowed("x")
    for bad in ("0", "false", ""):
        monkeypatch.setenv("SCRAPE_ALLOWED", bad)
        assert not scrape_allowed()
    monkeypatch.setenv("SCRAPE_ALLOWED", "1")
    assert_scrape_allowed("x")


def test_make_session_is_guarded(monkeypatch):
    from app.core.guard import ScrapeNotAllowed
    from app.services.full_scan_service import make_session
    monkeypatch.delenv("SCRAPE_ALLOWED", raising=False)
    import pytest
    with pytest.raises(ScrapeNotAllowed):
        make_session({})


def test_size_table_mode_detector():
    from app.core.sizing import detect_size_table_mode
    old = {f"S{i}": ["a", "b"] for i in range(300)}
    # 正常轮次：少数商品补货到 4 个码
    normal = {k: (["a", "b", "c", "d"] if i % 20 == 0 else v) for i, (k, v) in enumerate(old.items())}
    assert not detect_size_table_mode(normal, old)["is_table"]
    # 故障轮次：30% 商品从 2 个码变成 27 个
    table = {k: ([f"s{j}" for j in range(27)] if i % 3 == 0 else v) for i, (k, v) in enumerate(old.items())}
    r = detect_size_table_mode(table, old)
    assert r["is_table"] and r["ballooned"] == 100 and r["compared"] == 300
    # 样本太少不下结论
    assert not detect_size_table_mode({"A": [f"s{j}" for j in range(27)]}, {"A": ["a"]})["is_table"]
    # 旧数据为空的商品不参与比较（首轮 / 之前售罄）
    assert detect_size_table_mode({"A": list("abcdefghijkl")}, {"A": []})["compared"] == 0
    # 真实回放（2026-09-16 美国站）：故障轮 18.8% / 恢复轮 0.2%，阈值 8% 两边都有余量
    from app.core.sizing import BALLOON_FACTOR, BALLOON_MIN, BALLOON_SHARE
    assert (BALLOON_FACTOR, BALLOON_MIN, BALLOON_SHARE) == (2, 8, 0.08)
