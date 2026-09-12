import pandas as pd

from StockInvestmentTool.ops import trade_calendar
from StockInvestmentTool.warehouse.raw_repair import prepare_stock_daily_raw_range


def test_prepare_raw_range_skips_weekends_and_writes_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_calendar, "HOLIDAYS", set())
    result = prepare_stock_daily_raw_range(
        tmp_path, "2026-09-04", "2026-09-07", ["sh600000", "sz000001"]
    )
    assert [item["date"] for item in result["dates"]] == [
        "2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07"
    ]
    assert result["dates"][1]["trading"] is False
    assert not (tmp_path / "2026/09/05").exists()
    pending = pd.read_csv(tmp_path / "2026/09/04/pending_codes.csv")
    assert pending["code"].tolist() == ["sh600000", "sz000001"]


def test_prepare_raw_range_excludes_formal_and_tmp_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_calendar, "HOLIDAYS", set())
    directory = tmp_path / "2026/09/07"
    (directory / "_tmp").mkdir(parents=True)
    frame = pd.DataFrame({"date": ["2026-09-07"], "code": ["sh600000"]})
    frame.to_parquet(directory / "formal.parquet", index=False)
    frame.assign(code="sz000001").to_parquet(directory / "_tmp/tmp.parquet", index=False)
    prepare_stock_daily_raw_range(
        tmp_path, "2026-09-07", "2026-09-07", ["sh600000", "sz000001", "bj830001"]
    )
    pending = pd.read_csv(directory / "pending_codes.csv")
    assert pending["code"].tolist() == ["bj830001"]
