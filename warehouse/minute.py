"""分钟行情采集与独立存储。

分钟数据和 daily/indicators/factors/online 完全隔离：这里只写
``warehouse/minute/YYYY-MM-DD/minute.csv``，不修改天级分区。
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

MINUTE_COLUMNS = [
    "code", "trade_date", "time", "open", "high", "low", "close",
    "volume", "amount", "source", "fetched_at",
]


def normalize_minute_code(code: str) -> str:
    """Normalize a stock code to the Tencent/Sina ``sh600900`` form."""
    from StockInvestmentTool.datasource.fetcher import StockDataFetcher

    return StockDataFetcher.normalize_code(code).replace(".", "").lower()


def parse_tencent_minute(payload: dict, code: str,
                         fetched_at: Optional[datetime] = None) -> pd.DataFrame:
    """Parse Tencent's cumulative minute quote payload.

    Tencent returns ``HHMM price cumulative_volume cumulative_amount``.
    The stored volume and amount are converted to per-minute increments.
    The source does not provide minute OHLC, so open/high/low are left null
    rather than inventing values.
    """
    normalized = normalize_minute_code(code)
    data = (((payload or {}).get("data") or {}).get(normalized) or {}).get("data") or {}
    rows = data.get("data") if isinstance(data, dict) else data
    trade_date = data.get("date") if isinstance(data, dict) else None
    if not rows or not trade_date:
        return pd.DataFrame(columns=MINUTE_COLUMNS)

    day = datetime.strptime(str(trade_date), "%Y%m%d").strftime("%Y-%m-%d")
    fetched = (fetched_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    parsed = []
    previous_volume = None
    previous_amount = None
    for item in rows:
        parts = str(item).split()
        if len(parts) < 4:
            continue
        try:
            minute = parts[0]
            price = float(parts[1])
            cumulative_volume = float(parts[2])
            cumulative_amount = float(parts[3])
        except (TypeError, ValueError):
            continue
        volume = cumulative_volume - previous_volume if previous_volume is not None else cumulative_volume
        amount = cumulative_amount - previous_amount if previous_amount is not None else cumulative_amount
        previous_volume = cumulative_volume
        previous_amount = cumulative_amount
        parsed.append({
            "code": normalized,
            "trade_date": day,
            "time": f"{day} {minute[:2]}:{minute[2:4]}:00",
            "open": None,
            "high": None,
            "low": None,
            "close": price,
            "volume": volume,
            "amount": amount,
            "source": "tencent",
            "fetched_at": fetched,
        })
    return pd.DataFrame(parsed, columns=MINUTE_COLUMNS)


def fetch_tencent_minute(code: str, timeout: int = 15) -> pd.DataFrame:
    """Fetch today's 1-minute series for one stock from Tencent."""
    import requests

    normalized = normalize_minute_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={normalized}"
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, "0"):
        raise RuntimeError(f"Tencent minute API error: {payload.get('msg', payload.get('code'))}")
    return parse_tencent_minute(payload, normalized)


class MinuteStore:
    """Independent daily minute-file store."""

    def __init__(self, base_dir: Optional[Path] = None):
        from StockInvestmentTool.config import Config

        root = Path(base_dir) if base_dir else Config.DATA_DIR / "warehouse"
        self.minute_dir = root / "minute"
        self.minute_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, day: str) -> Path:
        return self.minute_dir / day / "minute.csv"

    def write(self, frame: pd.DataFrame, day: Optional[str] = None) -> Path:
        if frame is None or frame.empty:
            raise ValueError("分钟数据为空")
        out = frame.copy()
        for col in MINUTE_COLUMNS:
            if col not in out.columns:
                out[col] = None
        out = out[MINUTE_COLUMNS]
        out["code"] = out["code"].map(normalize_minute_code)
        out["trade_date"] = out["trade_date"].astype(str).str[:10]
        out["time"] = out["time"].astype(str)
        target_day = day or str(out["trade_date"].iloc[0])
        path = self._path(target_day)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)

        old = self.read(target_day)
        combined = pd.concat([old, out], ignore_index=True)
        combined = combined.drop_duplicates(["code", "trade_date", "time"], keep="last")
        combined = combined.sort_values(["code", "time"]).reset_index(drop=True)
        fd, temp_name = tempfile.mkstemp(prefix="minute_", suffix=".csv", dir=path.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            combined.to_csv(temp_path, index=False, encoding="utf-8-sig")
            os.replace(temp_path, path)
            # 容器可能以 root 运行，保证宿主机备份/巡检用户可读取分钟数据。
            os.chmod(path, 0o644)
        finally:
            temp_path.unlink(missing_ok=True)
        return path

    def read(self, day: str, code: Optional[str] = None) -> pd.DataFrame:
        path = self._path(day)
        if not path.exists():
            return pd.DataFrame(columns=MINUTE_COLUMNS)
        frame = pd.read_csv(path, encoding="utf-8-sig")
        if code:
            frame = frame[frame["code"] == normalize_minute_code(code)]
        return frame.sort_values(["code", "time"]).reset_index(drop=True)

    def days(self) -> list[str]:
        return sorted(p.name for p in self.minute_dir.iterdir() if p.is_dir())


def collect_minute_snapshot(codes: list[str]) -> dict:
    """Fetch and persist minute series for the requested watchpool codes."""
    store = MinuteStore()
    frames = []
    errors = {}
    for code in codes:
        try:
            frame = fetch_tencent_minute(code)
            if not frame.empty:
                frames.append(frame)
        except Exception as exc:
            errors[code] = str(exc)
            logger.warning("分钟数据采集失败 %s: %s", code, exc)
    if not frames:
        return {"ok": False, "codes": 0, "rows": 0, "errors": errors}
    merged = pd.concat(frames, ignore_index=True)
    path = store.write(merged)
    return {"ok": True, "path": str(path), "codes": merged["code"].nunique(),
            "rows": len(merged), "errors": errors}
