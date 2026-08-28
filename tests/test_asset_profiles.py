import pytest

from StockInvestmentTool.warehouse.asset_profiles import AssetProfileError, applicability, select_symbols


def test_asset_profiles_select_mixed_security_types():
    symbols, counts = select_symbols(
        ["sh600000", "sh510300", "sh000001"], asset_types=["stock", "etf"],
    )
    assert symbols == ["sh600000", "sh510300"]
    assert counts == {"stock": 1, "etf": 1}
    assert applicability("etf", "metrics", "roe") == "not_applicable"
    assert applicability("stock", "metrics", "roe") == "optional"


def test_asset_profiles_reject_unknown_types():
    with pytest.raises(AssetProfileError):
        select_symbols(["sh600000"], asset_types=["crypto"])


def test_metric_applicability_distinguishes_etf_non_applicable_data(tmp_path):
    from ops.task_center import TaskCenter
    center = TaskCenter(tmp_path / "metrics.db")
    assert center.metric_applicability("roe", ["stock", "etf"]) == {
        "stock": "optional", "etf": "not_applicable",
    }
