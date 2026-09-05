from __future__ import annotations

import pandas as pd

from StockInvestmentTool.portfolio.dashboard import DashboardService


def test_official_rotation_workbench_builds_summary_and_watchlists(monkeypatch):
    dates = pd.to_datetime(["2026-09-03", "2026-09-04"])
    frame = pd.DataFrame([
        {"date": dates[0], "industry_id": "881121", "industry_name": "半导体", "classification": "ths_industry", "stage": "STARTING", "previous_stage": "DORMANT", "stage_days": 1, "transition": "DORMANT->STARTING", "return_1d": .01, "return_5d": .05, "return_20d": .1, "rs_5": .02, "position_60": .5, "amount_ratio": 1.2, "rank_3d": 3, "rank_5d": 2, "rank_20d": 4, "rank_3d_change": 5, "strength_score": 70, "rotation_score": 90, "strength_change": 4, "reason": "排名改善", "advice": "重点关注"},
        {"date": dates[1], "industry_id": "881121", "industry_name": "半导体", "classification": "ths_industry", "stage": "RISING", "previous_stage": "STARTING", "stage_days": 1, "transition": "STARTING->RISING", "return_1d": .02, "return_5d": .06, "return_20d": .12, "rs_5": .03, "position_60": .6, "amount_ratio": 1.3, "rank_3d": 1, "rank_5d": 1, "rank_20d": 2, "rank_3d_change": 2, "strength_score": 82, "rotation_score": 75, "strength_change": 6, "reason": "强度提升", "advice": "持有观察"},
        {"date": dates[1], "industry_id": "881122", "industry_name": "煤炭", "classification": "ths_industry", "stage": "FADING", "previous_stage": "RISING", "stage_days": 2, "transition": "RISING->FADING", "return_1d": -.01, "return_5d": -.03, "return_20d": .01, "rs_5": -.02, "position_60": .4, "amount_ratio": .8, "rank_3d": 8, "rank_5d": 7, "rank_20d": 6, "rank_3d_change": -4, "strength_score": 40, "rotation_score": 20, "strength_change": -8, "reason": "轮动走弱", "advice": "观望"},
    ])

    class Result:
        data = frame
        context = {"source": "published_dataset", "fallback_used": False, "partition_versions": {}}

    class Access:
        def __init__(self, warehouse): pass
        def load_dataset(self, *args, **kwargs): return Result()

    monkeypatch.setattr("StockInvestmentTool.warehouse.datasets.DatasetAccess", Access)
    result = DashboardService(object()).official_rotation_workbench("2026-09-04")

    assert result["status"] == "success"
    assert result["actual_data_as_of"] == "2026-09-04"
    assert result["summary"]["mainline"] == 1
    assert result["summary"]["fading"] == 1
    assert result["watchlists"]["mainline"][0]["industry_name"] == "半导体"


def test_official_rotation_workbench_reads_latest_available_partition(monkeypatch):
    frame = pd.DataFrame([{
        "date": pd.Timestamp("2026-09-04"), "industry_id": "881121",
        "industry_name": "半导体", "classification": "ths_industry", "stage": "STARTING",
        "previous_stage": "DORMANT", "stage_days": 1, "transition": "DORMANT->STARTING",
        "return_1d": .01, "return_5d": .05, "return_20d": .1, "rs_5": .02,
        "position_60": .5, "amount_ratio": 1.2, "rank_3d": 3, "rank_5d": 2,
        "rank_20d": 4, "rank_3d_change": 5, "strength_score": 70,
        "rotation_score": 90, "strength_change": 4, "reason": "排名改善", "advice": "重点关注",
    }])

    class Result:
        data = frame
        context = {"source": "published_dataset", "fallback_used": False, "partition_versions": {}}

    class Access:
        def __init__(self, warehouse): pass
        def load_dataset(self, *args, **kwargs):
            assert kwargs.get("end_date") == "2026-09-04"
            assert kwargs.get("start_date") is None
            return Result()

    monkeypatch.setattr("StockInvestmentTool.warehouse.datasets.DatasetAccess", Access)
    result = DashboardService(object()).official_rotation_workbench("2026-09-04")
    assert result["status"] == "success"
    assert result["actual_data_as_of"] == "2026-09-04"
