"""Security boundary regressions for configuration and login redirects."""

from __future__ import annotations

import pytest

from StockInvestmentTool.core import scheme_store


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", "bad/name", ""])
def test_scheme_names_reject_path_components(name):
    with pytest.raises(ValueError):
        scheme_store._safe_name(name)


def test_scheme_name_accepts_safe_name():
    assert scheme_store._safe_name("my_growth-v2") == "my_growth-v2"
