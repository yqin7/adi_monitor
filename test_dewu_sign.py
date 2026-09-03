"""得物开放平台签名算法回归测试

期望值来自沙箱网关返回的 signStr 回显，逐字节比对确认。
运行: python test_dewu_sign.py
"""

from dewu_client import _java_url_encode, make_sign


def test_java_url_encode():
    """编码规则需与服务端 Java URLEncoder 一致"""
    cases = {
        "https://a/b": "https%3A%2F%2Fa%2Fb",
        "a b": "a+b",          # 空格是 + 不是 %20
        "a+b": "a%2Bb",
        "~": "%7E",            # Java 编码 ~，Python 默认不编码
        "*": "*",              # Java 不编码 *，Python 默认会编码
        "中文": "%E4%B8%AD%E6%96%87",
    }
    for raw, expected in cases.items():
        actual = _java_url_encode(raw)
        assert actual == expected, f"{raw!r}: 期望 {expected!r}，实际 {actual!r}"


def test_make_sign():
    """签名串 = 排序后的 k=urlencode(v) 用 & 连接，末尾拼 app_secret，MD5 转大写"""
    params = {
        "app_key": "testkey",
        "code": "a b/c+d~*",
        "spuId": 123,
        "timestamp": 1700000000000,
        "empty": "",        # 空值不参与
        "sign": "ignored",  # sign 自身不参与
    }
    assert make_sign(params, "testsecret") == "D9DA9C3B86B0FFFFB0AB538EDC7D23A6"


if __name__ == "__main__":
    test_java_url_encode()
    test_make_sign()
    print("签名算法测试通过")
