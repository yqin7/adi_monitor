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
    "1SIZE": "OS", "ONESIZE": "OS", "OSFA": "OS", "FREE": "OS", "フリー": "OS", "F": "OS",
}


def _clean(raw: str) -> str:
    s = str(raw or "").strip().upper()
    for uni, ascii_ in _FRAC.items():
        s = s.replace(uni, ascii_)
    # 去掉版型前缀：A/ 亚洲版、J/ 日本版、E/ 欧版。
    # 只是同一档位的不同版型标注，档位本身在斜杠后面。
    s = re.sub(r"^[AJE]\s*/\s*", "", s)
    # 短裤的裤长后缀不是尺码档位，去掉：
    #   韩国 'A/M 5"' / "A/S 7''"、日本 'M-7inch(裤)'
    # 不去的话 normalize 直接返回 None，in_stock 会把「有货」误判成「断码」。
    s = re.sub(r"[\s\-]*\d+(?:\.\d+)?\s*(?:INCH(?:ES)?|\"|''|’’|”|″)\S*$",
               "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


# adidas 美码(男) -> 毫米。女码比男码大 1.5 档，故 "M 10 / W 11.5" 取男码即可。
US_M_TO_MM = {
    "4": 220, "4.5": 225, "5": 230, "5.5": 235, "6": 240, "6.5": 245,
    "7": 250, "7.5": 255, "8": 260, "8.5": 265, "9": 270, "9.5": 275,
    "10": 280, "10.5": 285, "11": 290, "11.5": 295, "12": 300, "12.5": 305,
    "13": 310, "13.5": 315, "14": 320, "15": 330, "16": 340,
}

# adidas 英码(UK,男) -> 毫米。UK = US − 0.5
UK_TO_MM = {
    "3": 220, "3.5": 225, "4": 230, "4.5": 235, "5": 240, "5.5": 245,
    "6": 250, "6.5": 255, "7": 260, "7.5": 265, "8": 270, "8.5": 275,
    "9": 280, "9.5": 285, "10": 290, "10.5": 295, "11": 300, "11.5": 305,
    "12": 310, "12.5": 315, "13": 320, "14": 330, "15": 340,
}

_JP_CM = re.compile(r"^([\d.]+)\s*CM$", re.I)          # 日本站 "27.5cm"
_UK_ONLY = re.compile(r"^UK\s*([\d.]+)$", re.I)
_US_COMBO = re.compile(r"^M\s*([\d.]+)\s*/\s*W\s*[\d.]+$", re.I)
_US_ONLY = re.compile(r"^(?:M|US)\s*([\d.]+)$", re.I)


def normalize(raw: str) -> tuple[str, str | int] | None:
    """任意平台的尺码字符串 -> 内部规范码。无法识别返回 None。"""
    s = _clean(raw)
    if not s or s in ("HIDDEN", "-"):
        return None

    # 0a) 日本站 "27.5cm" -> 毫米
    m = _JP_CM.match(s)
    if m:
        try:
            return ("shoe", int(round(float(m.group(1)) * 10)))
        except ValueError:
            return None

    # 0b) 英国站 "UK 9"（裸数字英码在下面按上下文处理）
    m = _UK_ONLY.match(s)
    if m:
        mm = UK_TO_MM.get(m.group(1))
        return ("shoe", mm) if mm else None

    # 0c) 美国站复合尺码 "M 10 / W 11" 或 "M 10"
    m = _US_COMBO.match(s) or _US_ONLY.match(s)
    if m:
        mm = US_M_TO_MM.get(m.group(1))
        return ("shoe", mm) if mm else None

    # 1) 纯数字：毫米（韩国站 200~360）或 EU 整码（35~50）
    if re.fullmatch(r"\d{1,3}", s):
        n = int(s)
        if 200 <= n <= 360:                 # 韩国站毫米
            return ("shoe", n)
        if 34 <= n <= 52:                   # EU 整码
            mm = EU_TO_MM.get(s)
            return ("shoe", mm) if mm else None
        if 1 <= n <= 15:                    # 英国站裸英码
            mm = UK_TO_MM.get(s)
            return ("shoe", mm) if mm else None
        return None

    # 2) EU 分数码：38 2/3
    if re.fullmatch(r"\d{2}\s+\d/\d", s):
        mm = EU_TO_MM.get(s)
        return ("shoe", mm) if mm else None

    # 3) US 码：US 9.5 / 9.5（服装不会出现小数，故小数视为 US 鞋码）
    m = re.fullmatch(r"(?:US\s*)?(\d{1,2}(?:\.5)?)", s)
    if m:
        v = m.group(1)
        if s.startswith("US"):
            mm = US_M_TO_MM.get(v)
            return ("shoe", mm) if mm else None
        if "." in v:                        # 英国站 "9.5"
            mm = UK_TO_MM.get(v)
            return ("shoe", mm) if mm else None

    # 3b) 英国童装年龄码 "7-8Y"、"3-4Y" —— 自成一套，只跟同类比
    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})\s*Y", s)
    if m:
        return ("kid_age", f"{int(m.group(1))}-{int(m.group(2))}Y")

    # 3c) 英国童鞋 "10K"/"13K"（K = kids），与成人英码不是同一把尺子
    m = re.fullmatch(r"(\d{1,2}(?:\.5)?)\s*K", s)
    if m:
        return ("kid_shoe", m.group(1))

    # 3d) 区间码（手套/袜子）"6.5 - 8"：取区间本身做标识，不硬套成单码
    m = re.fullmatch(r"(\d{1,2}(?:\.5)?)\s*-\s*(\d{1,2}(?:\.5)?)", s)
    if m:
        return ("range", f"{m.group(1)}-{m.group(2)}")

    # 4) 服装
    key = s.replace(" ", "")
    if key in _APPAREL_ALIAS:
        return ("apparel", _APPAREL_ALIAS[key])

    return None


def sizes_match(a: str, b: str, tolerance_mm: int = 6) -> bool:
    """两个尺码是否指同一档。

    鞋按毫米比，容差 ±6mm：EU 三分制取整有 2~3mm 误差，
    日码(cm)按鞋楦长标注、比 EU 对照毫米小 5mm 左右，都要能吸收。
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na[0] != nb[0]:              # 不同命名空间不可比（成人码 vs 童码等）
        return False
    if na[0] == "shoe":
        return abs(int(na[1]) - int(nb[1])) <= tolerance_mm
    # 其余（apparel / kid_age / kid_shoe / range）都是离散标签，同值即同档。
    # 用通用相等而非逐类枚举 —— 以后再加命名空间不必改这里。
    return na[1] == nb[1]


def in_stock(dewu_size: str, available: Iterable[str], tolerance_mm: int = 6) -> bool | None:
    """得物的某个尺码，在 Adidas 侧是否有货。

    available 为空/缺失时返回 None（未知），不要当成「无货」——
    美国站目前就拿不到尺码库存。
    """
    avail = [s for s in (available or []) if s and str(s).upper() != "HIDDEN"]
    if not avail:
        return None
    if any(sizes_match(dewu_size, a, tolerance_mm) for a in avail):
        return True
    # 没匹配上，先分清是「真断码」还是「我们看不懂这些尺码」：
    # 该商品的站点尺码一个都归一化不了时，说的是后者，应返回未知。
    if normalize(dewu_size) is None or not any(normalize(a) for a in avail):
        return None
    return False
