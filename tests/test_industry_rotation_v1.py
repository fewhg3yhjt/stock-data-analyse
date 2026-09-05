from __future__ import annotations

import pandas as pd

from StockInvestmentTool.warehouse.industry_rotation import IndustryRotationBuilder
from StockInvestmentTool.warehouse.quality import check_industry_rotation_daily


def test_rotation_quality_requires_stage_and_transition_fields(tmp_path):
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-04"]), "industry_id": ["881121"],
        "industry_name": ["半导体"], "classification": ["ths_industry"],
        "strength_score": [68.3], "rotation_score": [91.3],
        "rank_3d": [12], "rank_5d": [5], "rank_20d": [8], "rank_3d_change": [18],
        "stage": ["STARTING"], "previous_stage": ["DORMANT"], "stage_days": [1],
        "transition": ["DORMANT->STARTING"], "reason": ["排名改善"], "advice": ["重点关注"],
    })
    path = tmp_path / "rotation.parquet"
    frame.to_parquet(path, index=False)
    result = check_industry_rotation_daily(path, expected_as_of="2026-09-04")
    assert result["status"] == "PASS"


def test_rotation_builder_does_not_read_money_flow(monkeypatch, tmp_path):
    from StockInvestmentTool.warehouse import industry_rotation
    from StockInvestmentTool.warehouse.storage import Warehouse

    calls = []

    class Result:
        def __init__(self, data, name):
            self.data = data
            self.context = {"source": "published_dataset", "fallback_used": False,
                            "partition_versions": {}, "dataset": name}

    class Access:
        def __init__(self, warehouse): pass
        def load_dataset(self, name, *args, **kwargs):
            calls.append(name)
            if name == "industry_daily":
                dates = pd.date_range("2026-08-01", "2026-09-04", freq="B")
                return Result(pd.DataFrame({"trading_date": dates.tolist() * 2,
                    "industry_id": ["881121"] * len(dates) + ["881122"] * len(dates),
                    "industry_name": ["半导体"] * len(dates) + ["通信设备"] * len(dates),
                    "close": list(range(100, 100 + len(dates))) + list(range(90, 90 + len(dates))),
                    "amount": [100.] * (len(dates) * 2)}), name)
            dates = pd.date_range("2026-08-01", "2026-09-04", freq="B")
            return Result(pd.DataFrame({"date": dates.tolist() * 2,
                "code": ["sh600000"] * len(dates) + ["sh600001"] * len(dates),
                "close": [100.] * (len(dates) * 2)}), name)

    monkeypatch.setattr(industry_rotation, "DatasetAccess", Access)
    result = IndustryRotationBuilder(Warehouse(tmp_path / "warehouse")).build(
        start_date="2026-09-01", end_date="2026-09-04", as_of="2026-09-04")
    assert result["status"] == "success"
    assert calls == ["industry_daily", "stock_daily"]


def test_rotation_builder_tracks_stage_days_per_industry(monkeypatch, tmp_path):
    from StockInvestmentTool.warehouse import industry_rotation
    from StockInvestmentTool.warehouse.storage import Warehouse

    class Result:
        def __init__(self, data, name):
            self.data = data
            self.context = {"source": "published_dataset", "fallback_used": False,
                            "partition_versions": {}, "dataset": name}

    class Access:
        def __init__(self, warehouse): pass
        def load_dataset(self, name, *args, **kwargs):
            dates = pd.date_range("2026-06-01", "2026-09-04", freq="B")
            if name == "industry_daily":
                return Result(pd.DataFrame({"trading_date": dates.tolist() * 2,
                    "industry_id": ["881121"] * len(dates) + ["881122"] * len(dates),
                    "industry_name": ["半导体"] * len(dates) + ["通信设备"] * len(dates),
                    "close": list(range(100, 100 + len(dates))) + list(range(90, 90 + len(dates))),
                    "amount": [100.] * (len(dates) * 2)}), name)
            return Result(pd.DataFrame({"date": dates.tolist() * 2,
                "code": ["sh600000"] * len(dates) + ["sh600001"] * len(dates),
                "close": [100.] * (len(dates) * 2)}), name)

    monkeypatch.setattr(industry_rotation, "DatasetAccess", Access)
    result = IndustryRotationBuilder(Warehouse(tmp_path / "warehouse")).build(
        start_date="2026-09-01", end_date="2026-09-04", as_of="2026-09-04")
    assert result["status"] == "success"
    frame = pd.read_parquet(tmp_path / "warehouse" / "candidates" / "industry_rotation_daily" / "2026-09.parquet")
    for _, group in frame.groupby("industry_id"):
        assert group["stage_days"].iloc[0] >= 1
