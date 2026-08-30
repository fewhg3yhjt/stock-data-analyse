from pathlib import Path

import pandas as pd


def test_derived_tasks_accept_explicit_month_scope():
    from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
    import inspect

    assert "months" in inspect.signature(IndicatorsBuilder.build_all).parameters
