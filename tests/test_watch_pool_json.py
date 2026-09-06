"""Watch-pool API JSON safety tests."""

from __future__ import annotations

import math

from StockInvestmentTool.web.app import _to_json_safe


def test_to_json_safe_converts_non_finite_floats_to_null():
    result = _to_json_safe({"nan": float("nan"), "inf": float("inf"), "ok": 1.5})
    assert result == {"nan": None, "inf": None, "ok": 1.5}
    assert not any(isinstance(value, float) and not math.isfinite(value) for value in result.values())
