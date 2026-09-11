"""尺码归一化的回归测试。

存在意义：sizing 的规则是靠一条条撞出来的 —— 韩国的裤长后缀、日本的 J/ 版型
前缀、英国的童装年龄码，每一条都对应过一次「库存列在骗人」。改正则很容易顺手
打破已支持的格式，所以每修一类就在这里钉一颗钉子。

新增格式的流程：
    1. GET /lookup/sizes/unparsed 看哪种写法冒头了
    2. 在 app/core/sizing.py 加规则
    3. 回到这里补用例，跑 pytest tests/test_sizing.py
"""
from __future__ import annotations

import pytest

from app.core.sizing import in_stock, normalize, sizes_match


# ── 鞋码：各国口径都要收敛到毫米 ──────────────────────────────────────
@pytest.mark.parametrize("raw,expect", [
    ("27.5cm", ("shoe", 275)),          # 日本站厘米
    ("250", ("shoe", 250)),             # 韩国站毫米
    ("42", ("shoe", 265)),              # EU 整码
    ("38 2/3", ("shoe", 240)),          # EU 三分制
    ("UK 9", ("shoe", 280)),            # 英国站
    ("US 9.5", ("shoe", 275)),          # 美码显式
    ("M 10 / W 11", ("shoe", 280)),     # 美国站男/女复合码
])
def test_shoe(raw, expect):
    assert normalize(raw) == expect


# ── 服装：版型前缀与裤长后缀都只是修饰，档位在主体 ────────────────────
@pytest.mark.parametrize("raw,expect", [
    ("M", ("apparel", "M")),
    ("2XL", ("apparel", "2XL")),
    ("XXL", ("apparel", "2XL")),        # 别名收敛
    ("A/M", ("apparel", "M")),          # 亚洲版型前缀
    ("J/M", ("apparel", "M")),          # 日本版型前缀
    ('A/M 5"', ("apparel", "M")),       # 韩国短裤裤长后缀
    ("A/S 7''", ("apparel", "S")),
    ("M-7inch", ("apparel", "M")),      # 日本短裤裤长后缀
    ("1 Size", ("apparel", "OS")),      # 均码
    ("フリー", ("apparel", "OS")),
])
def test_apparel(raw, expect):
    assert normalize(raw) == expect


# ── 童码与区间码：各自独立命名空间，不能和成人码混比 ──────────────────
@pytest.mark.parametrize("raw,expect", [
    ("7-8Y", ("kid_age", "7-8Y")),
    ("11-12Y", ("kid_age", "11-12Y")),
    ("10K", ("kid_shoe", "10")),
    ("13.5K", ("kid_shoe", "13.5")),
    ("6.5 - 8", ("range", "6.5-8")),
])
def test_kid_and_range(raw, expect):
    assert normalize(raw) == expect


def test_unknown_returns_none():
    for raw in ("", "HIDDEN", "-", "??", "A130"):
        assert normalize(raw) is None


# ── 跨站匹配 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b,expect", [
    ("A/M", "J/M", True),               # 得物 vs 日本站
    ("A/M", 'A/M 5"', True),            # 得物 vs 韩国站短裤
    ("A/XL", "M-7inch", False),
    ("A/M", "J/L", False),
    ("27.5cm", "US 9.5", True),         # 日码 vs 美码，容差内
    ("42", "36", False),
    ("7-8Y", "7-8Y", True),
    ("7-8Y", "9-10Y", False),
    ("10K", "10", False),               # 童鞋码不等于成人码
    ("1 Size", "フリー", True),
])
def test_cross_site_match(a, b, expect):
    assert sizes_match(a, b) is expect


# ── in_stock 三态：True / False / None ────────────────────────────────
def test_in_stock_true():
    assert in_stock("A/M", ['A/M 5"', 'A/L 5"']) is True


def test_in_stock_false_is_real_stockout():
    """能看懂尺码、确实没这一档 -> False。"""
    assert in_stock("A/3XL", ['A/M 5"', 'A/L 5"']) is False


def test_in_stock_none_when_sizes_unreadable():
    """看不懂站点尺码时必须返回 None（未知）而不是 False（断码）。

    这条最关键：把「不知道」说成「没货」会让用户直接错过真实机会。
    """
    assert in_stock("A/M", ["A130", "??"]) is None


def test_in_stock_none_when_no_data():
    assert in_stock("A/M", []) is None
    assert in_stock("A/M", ["HIDDEN"]) is None
