from __future__ import annotations

import pandas as pd

from StockInvestmentTool.portfolio.dashboard import DashboardService
from StockInvestmentTool.warehouse.quality import check_ths_industry_membership
from StockInvestmentTool.market_discovery.service import stock_series


def _membership(tmp_path, rows=2, source_commit="fad8b3374fc8605ac723f5626e600d554b0c222a"):
    frame = pd.DataFrame({
        "snapshot_date": ["2026-09-03"] * rows,
        "industry_id": ["881101"] * rows,
        "industry_name": ["种植业与林业"] * rows,
        "code": [f"sh60090{i}" for i in range(rows)],
        "stock_name": ["甲", "乙"][:rows],
        "source_commit": [source_commit] * rows,
        "source": ["github"] * rows,
        "captured_at": ["2026-09-03T00:00:00"] * rows,
    })
    path = tmp_path / "ths.parquet"
    frame.to_parquet(path, index=False)
    return path


def test_ths_quality_rejects_truncated_fixed_commit(tmp_path):
    result = check_ths_industry_membership(
        _membership(tmp_path), expected_industries=90, expected_rows=5556,
        expected_source_commit="fad8b3374fc8605ac723f5626e600d554b0c222a",
    )
    assert result["status"] == "FAIL"
    assert result["checks"]["baseline_row_count_mismatch"] is True
    assert result["checks"]["baseline_industry_count_mismatch"] is True


def test_board_overview_uses_published_warning_dataset(monkeypatch):
    frame = pd.DataFrame({
        "industry_id": ["881101", "881101", "881101"],
        "industry_name": ["种植业与林业"] * 3,
        "trading_date": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03"]),
        "close": [10.0, 10.5, 11.0],
    })
    calls = {}

    class Result:
        data = frame

    class Access:
        def __init__(self, warehouse):
            pass

        def load_dataset(self, name, **kwargs):
            calls["name"] = name
            calls["quality"] = kwargs["required_quality"]
            return Result()

    monkeypatch.setattr("StockInvestmentTool.warehouse.datasets.DatasetAccess", Access)
    result = DashboardService(object()).board_overview()
    assert result[0]["name"] == "种植业与林业"
    assert calls == {"name": "industry_daily", "quality": "WARNING"}


def test_ths_rotation_uses_common_actual_date(monkeypatch):
    membership = pd.DataFrame({
        "snapshot_date": ["2026-09-03", "2026-09-03"],
        "industry_id": ["881101", "881101"], "industry_name": ["种植业与林业"] * 2,
        "code": ["sh600900", "sz000001"],
    })
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-01", "2026-09-02"]),
        "code": ["sh600900", "sh600900", "sh600900", "sz000001", "sz000001"],
        "close": [10, 11, 12, 20, 19], "amount": [1, 1, 1, 2, 2],
    })

    class Result:
        def __init__(self, data):
            self.data = data

    class Access:
        def __init__(self, warehouse):
            pass

        def load_dataset(self, name, **kwargs):
            return Result(membership if name == "ths_industry_membership" else daily)

    monkeypatch.setattr("StockInvestmentTool.warehouse.datasets.DatasetAccess", Access)
    result = DashboardService(object()).industry_rotation_overview(
        "2026-09-03", category="ths_industry", membership_as_of="2026-09-03"
    )
    item = result["items"][0]
    assert result["actual_data_as_of"] == "2026-09-03"
    assert item["valid_count"] == 1
    assert item["member_count"] == 2
    assert item["coverage"] == 0.5


def test_board_kline_route_rejects_dashboard_error(monkeypatch):
    from StockInvestmentTool.web.app import create_app

    class FakeDashboard:
        def __init__(self, manager):
            pass

        def board_index_kline(self, *args, **kwargs):
            return {"name": "测试板块", "dates": [], "close": [], "error": "Published 数据不可用"}

    monkeypatch.setenv("STOCK_DISABLE_AUTH", "1")
    monkeypatch.setattr("StockInvestmentTool.portfolio.dashboard.DashboardService", FakeDashboard)
    app = create_app()
    response = app.test_client().get(
        "/market/board_kline?category=ths_industry&sector_id=881101&name=x"
    )
    assert response.status_code == 422
    assert response.get_json()["status"] == "error"
    assert response.get_json()["error"] == "Published 数据不可用"


def test_csrc_board_kline_builds_equal_weight_member_index(monkeypatch):
    from StockInvestmentTool.portfolio.dashboard import DashboardService

    membership = pd.DataFrame({
        "snapshot_date": ["2026-09-02", "2026-09-02"],
        "code": ["sh600900", "sz000001"],
        "industry_code": ["E47", "E47"],
        "industry_name": ["房屋建筑业", "房屋建筑业"],
    })
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01", "2026-09-02"] * 2),
        "code": ["sh600900", "sh600900", "sz000001", "sz000001"],
        "close": [10, 11, 20, 19],
    })

    class Result:
        def __init__(self, data): self.data = data

    class Access:
        def __init__(self, warehouse): pass
        def load_dataset(self, name, **kwargs):
            return Result(membership if name == "industry_membership" else daily)

    monkeypatch.setattr("StockInvestmentTool.warehouse.datasets.DatasetAccess", Access)
    result = DashboardService(object()).board_index_kline("房屋建筑业", category="csrc", sector_id="E47", days=120)
    assert result["dates"] == ["2026-09-02"]
    assert result["series_type"] == "证监会行业成员等权指数"
    assert result["close"][-1] == 102.5


def test_stock_series_respects_as_of(tmp_path):
    from StockInvestmentTool.warehouse.storage import Warehouse

    warehouse = Warehouse(tmp_path / "warehouse")
    dates = pd.bdate_range("2026-09-01", periods=5)
    warehouse.write_daily_partition("2026-09", pd.DataFrame({
        "date": dates, "code": ["sh600900"] * 5,
        "open": [10, 11, 12, 13, 14], "high": [11, 12, 13, 14, 15],
        "low": [9, 10, 11, 12, 13], "close": [10, 11, 12, 13, 14],
        "volume": [1] * 5, "amount": [1] * 5,
    }))
    result = stock_series("sh600900", warehouse=warehouse, days=20,
                          as_of="2026-09-03", allow_legacy=True)
    assert result["dates"][-1] == "2026-09-03"
    assert len(result["dates"]) == 3
