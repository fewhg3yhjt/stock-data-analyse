from __future__ import annotations

import pandas as pd
import pytest

from StockInvestmentTool.warehouse.datasets import DatasetAccessError
from StockInvestmentTool.warehouse.industry_rotation import IndustryRotationBuilder


def test_rotation_builder_blocks_mismatched_published_dates(monkeypatch):
    class Result:
        def __init__(self, data, as_of):
            self.data = data
            self.context = {"source": "published_dataset", "fallback_used": False,
                            "partition_versions": {}, "data_as_of": as_of}

    class Access:
        def __init__(self, warehouse): pass
        def load_dataset(self, name, *args, **kwargs):
            frame = pd.DataFrame({"trading_date": ["2026-09-07"], "industry_id": ["881101"], "industry_name": ["种植业与林业"], "close": [100.0], "amount": [1.0]}) if name == "industry_daily" else pd.DataFrame({"date": ["2026-09-04"], "code": ["sh600000"], "close": [10.0]})
            return Result(frame, "2026-09-07" if name == "industry_daily" else "2026-09-04")

    monkeypatch.setattr("StockInvestmentTool.warehouse.industry_rotation.DatasetAccess", Access)
    with pytest.raises(DatasetAccessError, match="输入日期未对齐"):
        IndustryRotationBuilder(object()).build(start_date="2026-09-07", end_date="2026-09-07", as_of="2026-09-07")


def test_rotation_input_barrier_keeps_industry_publish_independent(monkeypatch):
    from StockInvestmentTool.web import scheduler

    states = {"industry_daily": True, "stock_daily": False}
    monkeypatch.setattr(scheduler, "_dataset_released", lambda name, target: states[name])

    assert scheduler._dataset_released("industry_daily", "2026-09-07") is True
    assert scheduler._rotation_inputs_aligned("2026-09-07") is False
