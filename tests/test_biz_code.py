# -*- coding: utf-8 -*-
"""biz 包单元测试：canonical code 规范。"""

import pytest

from StockInvestmentTool.biz.code import InvalidCodeError, is_canonical, normalize


class TestCanonicalCode:
    def test_normalize_canonical_keeps(self):
        assert normalize("sh600908") == "sh600908"
        assert normalize("sz000823") == "sz000823"
        assert normalize("bj920982") == "bj920982"

    def test_normalize_uppercase(self):
        assert normalize("SH600908") == "sh600908"
        assert normalize("SZ000001") == "sz000001"

    def test_normalize_raw6(self):
        assert normalize("600908") == "sh600908"
        assert normalize("000823") == "sz000823"
        assert normalize("301667") == "sz301667"
        assert normalize("510300") == "sh510300"
        assert normalize("159919") == "sz159919"
        assert normalize("920982") == "bj920982"

    def test_normalize_baostock_prefix_dot(self):
        assert normalize("sh.600908") == "sh600908"
        assert normalize("sz.000001") == "sz000001"

    def test_normalize_suffix_dot(self):
        assert normalize("600908.SH") == "sh600908"
        assert normalize("000300.SH") == "sh000300"
        assert normalize("000001.SZ") == "sz000001"
        assert normalize("920982.BJ") == "bj920982"

    def test_normalize_whitespace(self):
        assert normalize(" sh600908 ") == "sh600908"

    def test_invalid_codes(self):
        with pytest.raises(InvalidCodeError):
            normalize("")
        with pytest.raises(InvalidCodeError):
            normalize("12345")
        with pytest.raises(InvalidCodeError):
            normalize("SH60090")  # 5 位数字
        with pytest.raises(InvalidCodeError):
            normalize("xx600908")
        with pytest.raises(InvalidCodeError):
            normalize("sh.60090")

    def test_is_canonical(self):
        assert is_canonical("sh600908")
        assert not is_canonical("600908")
        assert not is_canonical("SH600908")

    def test_normalize_none(self):
        with pytest.raises(InvalidCodeError):
            normalize(None)