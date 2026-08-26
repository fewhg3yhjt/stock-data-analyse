"""分钟行情：解析、独立存储和天级数据隔离测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse.minute import (
    MINUTE_COLUMNS,
    MinuteStore,
    parse_tencent_minute,
)
from StockInvestmentTool.warehouse.storage import Warehouse


def _payload():
    return {
        "code": 0,
        "data": {
            "sh600900": {
                "data": {
                    "date": "20260826",
                    "data": [
                        "0930 28.20 100 1000.00",
                        "0931 28.19 160 1605.00",
                        "0932 28.24 240 2420.00",
                    ],
                }
            }
        },
    }


def test_parse_tencent_minute_converts_cumulative_values():
    frame = parse_tencent_minute(_payload(), "sh.600900")

    assert list(frame.columns) == MINUTE_COLUMNS
    assert frame["code"].tolist() == ["sh600900"] * 3
    assert frame["time"].tolist()[0] == "2026-08-26 09:30:00"
    assert frame["close"].tolist() == [28.20, 28.19, 28.24]
    assert frame["volume"].tolist() == [100.0, 60.0, 80.0]
    assert frame["amount"].tolist() == [1000.0, 605.0, 815.0]


def test_minute_store_is_separate_from_daily(tmp_path):
    warehouse = Warehouse(base_dir=Path(tmp_path))
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-08-26"]), "code": ["sh600900"],
        "open": [28.0], "high": [29.0], "low": [27.0], "close": [28.2],
        "volume": [100], "amount": [1000], "peTTM": [10], "pbMRQ": [2], "turn": [1],
    })
    warehouse.write_daily_partition("2026-08", daily)
    minute = parse_tencent_minute(_payload(), "sh600900")
    path = MinuteStore(Path(tmp_path)).write(minute)

    assert path == Path(tmp_path) / "minute" / "2026-08-26" / "minute.csv"
    assert path.exists()
    assert list((Path(tmp_path) / "daily").glob("*.parquet"))
    assert len(MinuteStore(Path(tmp_path)).read("2026-08-26", "sh600900")) == 3


def test_minute_store_deduplicates_same_minute(tmp_path):
    store = MinuteStore(Path(tmp_path))
    frame = parse_tencent_minute(_payload(), "sh600900")
    store.write(frame)
    store.write(frame)

    assert len(store.read("2026-08-26", "sh600900")) == 3
