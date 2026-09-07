"""尺码归一化与跨平台换算。

三方尺码口径不同，直接字符串比对必然失配：
    Adidas 韩国   鞋=毫米(JP/mm) 220~320   服装=XS/S/M/L/XL/2XL，含 A/ 亚洲版型前缀
    Adidas 美国   鞋=US 码                  服装=XS/S/M/L/XL
    得物          鞋=EU 码（带 ⅓ ⅔ 分数）   服装=XS/S/M/L/XXL

统一转成内部规范码后再比：
    鞋   -> ("shoe", 毫米整数)      毫米是 adidas 官方尺码表的主轴，EU/US 都能映射过去
    服装 -> ("apparel", 归一标签)   2XL/XXL 等写法统一

参考 adidas 官方尺码对照（EU ⅓/⅔ 制与 JP 毫米一一对应）。
"""
from __future__ import annotations

import re
from typing import Iterable

# ── adidas 官方 EU(mm) 对照表 ────────────────────────────────────────────────
# EU 码 -> 毫米。EU 用三分制，每档 5mm。
EU_TO_MM: dict[str, int] = {
    "35": 215, "35 1/3": 215, "35 1/2": 217, "35 2/3": 217,
    "36": 220, "36 1/3": 222, "36 2/3": 225,
    "37": 227, "37 1/3": 230, "37 2/3": 232,
    "38": 235, "38 1/3": 237, "38 2/3": 240,
    "39": 242, "39 1/3": 245, "39 2/3": 247,
    "40": 250, "40 1/3": 252, "40 2/3": 255,
    "41": 257, "41 1/3": 260, "41 2/3": 262,
    "42": 265, "42 1/3": 267, "42 2/3": 270,
    "43": 272, "43 1/3": 275, "43 2/3": 277,
    "44": 280, "44 1/3": 282, "44 2/3": 285,
    "45": 287, "45 1/3": 290, "45 2/3": 292,
    "46": 295, "46 1/3": 297, "46 2/3": 300,
    "47": 302, "47 1/3": 305, "47 2/3": 307,
    "48": 310, "48 1/3": 312, "48 2/3": 315,
    "49": 317, "49 1/3": 320, "49 2/3": 322,
    "50": 325, "50 1/3": 327, "50 2/3": 330,
}

# Unicode 分数 -> ASCII，得物返回的是 ⅓ ⅔ ½ 这类字符
_FRAC = {"⅓": " 1/3", "⅔": " 2/3", "½": " 1/2", "¼": " 1/4", "¾": " 3/4"}

# 服装尺码归一：把各种写法收敛到统一标签
_APPAREL_ALIAS = {
    "XXS": "XXS", "2XS": "XXS",
    "XS": "XS", "S": "S", "M": "M", "L": "L", "XL": "XL",
    "XXL": "2XL", "2XL": "2XL",
    "XXXL": "3XL", "3XL": "3XL",
    "XXXXL": "4XL", "4XL": "4XL",
    "XXXXXL": "5XL", "5XL": "5XL",
    # 均码
    "NS": "OS", "OS": "OS", "OSFM": "OS", "OSFW": "OS", "OSFL": "OS", "ONE SIZE": "OS",
}


def _clean(raw: str) -> str:
    s = str(raw or "").strip().upper()
    for uni, ascii_ in _FRAC.items():
        s = s.replace(uni, ascii_)
    # 去掉亚洲版型前缀 A/、韩版 K 前缀（KS/KM/KL）保留主体
    s = re.sub(r"^A\s*/\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize(raw: str) -> tuple[str, str | int] | None:
    """任意平台的尺码字符串 -> 内部规范码。无法识别返回 None。"""
    s = _clean(raw)
    if not s or s in ("HIDDEN", "-"):
        return None

    # 1) 纯数字：毫米（韩国站 200~360）或 EU 整码（35~50）
    if re.fullmatch(r"\d{2,3}", s):
        n = int(s)
        if 200 <= n <= 360:
            return ("shoe", n)
        if 34 <= n <= 52:
            mm = EU_TO_MM.get(s)
            return ("shoe", mm) if mm else None
        return None

    # 2) EU 分数码：38 2/3
    if re.fullmatch(r"\d{2}\s+\d/\d", s):
        mm = EU_TO_MM.get(s)
        return ("shoe", mm) if mm else None

    # 3) US 码：US 9.5 / 9.5（服装不会出现小数，故小数视为 US 鞋码）
    m = re.fullmatch(r"(?:US\s*)?(\d{1,2}(?:\.5)?)", s)
    if m and ("." in m.group(1) or s.startswith("US")):
        return ("shoe_us", float(m.group(1)))

    # 4) 服装
    key = s.replace(" ", "")
    if key in _APPAREL_ALIAS:
        return ("apparel", _APPAREL_ALIAS[key])

    return None


def sizes_match(a: str, b: str, tolerance_mm: int = 3) -> bool:
    """两个尺码是否指同一档。鞋按毫米比（允许 ±tolerance，吸收三分制取整误差）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na[0] == "shoe" and nb[0] == "shoe":
        return abs(int(na[1]) - int(nb[1])) <= tolerance_mm
    if na[0] == "apparel" and nb[0] == "apparel":
        return na[1] == nb[1]
    return False


def in_stock(dewu_size: str, available: Iterable[str], tolerance_mm: int = 3) -> bool | None:
    """得物的某个尺码，在 Adidas 侧是否有货。

    available 为空/缺失时返回 None（未知），不要当成「无货」——
    美国站目前就拿不到尺码库存。
    """
    avail = [s for s in (available or []) if s and str(s).upper() != "HIDDEN"]
    if not avail:
        return None
    return any(sizes_match(dewu_size, a, tolerance_mm) for a in avail)
