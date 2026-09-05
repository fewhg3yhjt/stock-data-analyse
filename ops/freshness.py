"""Warehouse freshness checks used by the operational data center.

The checks inspect the files themselves instead of trusting only manifests.  All
functions accept an optional ``now``/``warehouse`` so tests can use an isolated
temporary warehouse without touching runtime data.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd


@dataclass
class DatasetStatus:
    dataset: str
    latest_value: Optional[str] = None
    latest_trade_date: Optional[str] = None
    status: str = "unknown"
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_error: Optional[str] = None
    rows: Optional[int] = None
    symbols: Optional[int] = None
    job_name: Optional[str] = None
    source: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _daily_close_time() -> time:
    """日线任务计划时间（DAILY_RUN_TIME，默认 15:35）作为交易日收盘判定时刻。"""
    spec = os.getenv("DAILY_RUN_TIME", "15:35")
    try:
        hour, minute = (int(part) for part in spec.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError):
        return time(15, 35)


def latest_expected_trade_day(now: Optional[datetime] = None) -> date:
    """Return the latest weekday expected to have daily data.

    Delegates to the unified trade-day service (weekend + configurable holidays).
    """
    from StockInvestmentTool.ops.trade_calendar import latest_closed_trade_day

    return latest_closed_trade_day(now, close_time=_daily_close_time())


def latest_completed_trade_day(now: Optional[datetime] = None) -> date:
    """Return the latest full daily bar expected by the warehouse pipeline.

    Delegates to the unified trade-day service. Intraday minute data is tracked
    separately and may use today's date.
    """
    from StockInvestmentTool.ops.trade_calendar import latest_closed_trade_day

    return latest_closed_trade_day(now, close_time=_daily_close_time())


def _date_value(value) -> Optional[date]:
    if value is None or pd.isna(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _latest_parquet(warehouse, kind: str, date_column: str = "date") -> tuple[Optional[str], int, int]:
    if kind == "daily":
        try:
            from StockInvestmentTool.warehouse.datasets import DatasetAccess
            result = DatasetAccess(warehouse).load_dataset(
                "stock_daily", required_quality="WARNING")
            frame = result.data
            if frame is not None and not frame.empty:
                values = pd.to_datetime(frame.get(date_column), errors="coerce").dropna()
                symbols = set(frame["code"].dropna().astype(str)) if "code" in frame else set()
                return (values.max().strftime("%Y-%m-%d") if not values.empty else None,
                        len(frame), len(symbols))
        except Exception:
            pass
    months = warehouse.available_months(kind)
    latest = None
    rows = 0
    symbols: set[str] = set()
    for month in months:
        frame = (warehouse.read_daily(month) if kind == "daily" else
                 warehouse.read_indicator(month))
        if frame is None or frame.empty:
            continue
        rows += len(frame)
        if "code" in frame.columns:
            symbols.update(frame["code"].dropna().astype(str))
        if date_column in frame.columns:
            values = pd.to_datetime(frame[date_column], errors="coerce").dropna()
            if not values.empty:
                candidate = values.max().date()
                latest = candidate if latest is None or candidate > latest else latest
    return (latest.isoformat() if latest else None, rows, len(symbols))


def _job_context(job_names: set[str], job_runs) -> tuple[Optional[dict], Optional[dict]]:
    matching = [item for item in job_runs if item.get("job_name") in job_names]
    if not matching:
        return None, None
    latest = matching[0]
    success = next((item for item in matching if item.get("status") == "success"), None)
    return latest, success


def _apply_job_state(item: DatasetStatus, latest_job: Optional[dict], success_job: Optional[dict]) -> None:
    if success_job:
        item.last_success_at = success_job.get("finished_at") or success_job.get("started_at")
    if latest_job and latest_job.get("status") in {"failed", "timeout"}:
        item.status = "failed"
        item.last_failure_at = latest_job.get("finished_at") or latest_job.get("started_at")
        item.last_error = latest_job.get("error") or (
            "最近一次任务超时" if latest_job.get("status") == "timeout" else "最近一次任务失败")


def _classify_daily_task(item: DatasetStatus, latest_job: Optional[dict], current: datetime) -> None:
    if latest_job:
        if latest_job.get("status") == "running":
            item.status = "running"
            item.last_error = latest_job.get("phase") or "日线任务执行中"
            return
        _apply_job_state(item, latest_job, latest_job if latest_job.get("status") == "success" else None)
        if latest_job.get("status") == "timeout":
            item.status = "failed"
            item.last_failure_at = latest_job.get("finished_at") or latest_job.get("started_at")
            item.last_error = latest_job.get("error") or "日线任务超时"
        return
    close = _daily_close_time()
    scheduled = current.replace(hour=close.hour, minute=close.minute, second=0, microsecond=0)
    if current < scheduled:
        item.status = "waiting_close"
        item.last_error = f"等待今日收盘及 {close.strftime('%H:%M')} 日线任务计划时间"
    else:
        item.status = "failed"
        item.last_error = f"今日日线任务未执行（计划时间 {close.strftime('%H:%M')}）"


def classify_freshness(dataset_latest: Optional[str], expected_trade_day: date) -> str:
    """Classify a daily-like date against an expected trade day."""
    latest = _date_value(dataset_latest)
    if latest is None:
        return "empty"
    expected = expected_trade_day
    if latest >= expected:
        return "healthy"
    lag = 0
    cursor = latest
    while cursor < expected:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            lag += 1
    return "stale" if lag == 1 else "critical"


def _minute_latest(warehouse) -> tuple[Optional[str], int, int]:
    latest = None
    rows = 0
    symbols: set[str] = set()
    store = warehouse.minute_store()
    for day in store.days():
        frame = store.read(day)
        if frame.empty:
            continue
        rows += len(frame)
        if "code" in frame.columns:
            symbols.update(frame["code"].dropna().astype(str))
        if "time" in frame.columns:
            values = pd.to_datetime(frame["time"], errors="coerce").dropna()
            if not values.empty:
                candidate = values.max().to_pydatetime()
                latest = candidate if latest is None or candidate > latest else latest
    return (latest.strftime("%Y-%m-%d %H:%M:%S") if latest else None, rows, len(symbols))


def _online_latest(warehouse) -> tuple[Optional[str], int, int]:
    latest = None
    rows = 0
    symbols: set[str] = set()
    for day_dir in warehouse.online_dir.iterdir():
        if not day_dir.is_dir():
            continue
        for path in day_dir.glob("snapshot_*.csv"):
            try:
                frame = pd.read_csv(path, encoding="utf-8-sig")
            except Exception:
                continue
            rows += len(frame)
            if "code" in frame.columns:
                symbols.update(frame["code"].dropna().astype(str))
            stamp = frame.get("snapshot_time", pd.Series(dtype=str))
            values = pd.to_datetime(stamp, errors="coerce").dropna()
            if not values.empty:
                candidate = values.max().to_pydatetime()
            else:
                try:
                    candidate = datetime.strptime(f"{day_dir.name} {path.stem[-6:]}", "%Y-%m-%d %H%M%S")
                except ValueError:
                    continue
            latest = candidate if latest is None or candidate > latest else latest
    return (latest.strftime("%Y-%m-%d %H:%M:%S") if latest else None, rows, len(symbols))


def _in_market_session(current: datetime) -> bool:
    if current.weekday() >= 5:
        return False
    clock = current.time()
    return time(9, 30) <= clock <= time(11, 30) or time(13) <= clock <= time(15)


def _intraday_status(latest: Optional[str], current: datetime, minutes: int) -> str:
    if not latest:
        return "empty"
    if not _in_market_session(current):
        return "healthy"
    stamp = pd.to_datetime(latest, errors="coerce")
    if pd.isna(stamp):
        return "unknown"
    return "critical" if current - stamp.to_pydatetime() > timedelta(minutes=minutes) else "healthy"


def dataset_statuses(*, warehouse=None, job_runs=None, now: Optional[datetime] = None) -> list[DatasetStatus]:
    from StockInvestmentTool.warehouse.storage import Warehouse
    from StockInvestmentTool.ops.job_runs import JobRunStore

    warehouse = warehouse or Warehouse()
    current = now or datetime.now()
    minute_configured = os.getenv("WAREHOUSE_MINUTE_SNAPSHOT") == "1"
    online_configured = os.getenv("WAREHOUSE_ONLINE_SNAPSHOT") == "1"
    expected = latest_completed_trade_day(current)
    if job_runs is not None:
        runs = job_runs
    else:
        # Do not let high-frequency records hide long-running data jobs.
        runs, _ = JobRunStore().query(
            limit=1000,
            job_names={
                "daily_sync", "daily_tasks", "rebuild_indicators",
                "minute_snapshot", "online_snapshot",
            },
        )
    result: list[DatasetStatus] = []

    specs = [
        ("daily", "daily", "daily_sync", "全市场日线 Parquet", "WAREHOUSE_DAILY_SYNC"),
        ("indicators", "indicator", "rebuild_indicators", "指标 Parquet", None),
    ]
    values: dict[str, DatasetStatus] = {}
    for name, kind, job_name, source, switch in specs:
        latest, rows, symbols = _latest_parquet(warehouse, kind)
        item = DatasetStatus(name, latest, latest, classify_freshness(latest, expected),
                             rows=rows, symbols=symbols, job_name=job_name, source=source)
        job_names = {job_name}
        if name == "daily":
            # Keep historical daily_tasks records meaningful while new runs
            # use the dedicated daily_sync entry.
            job_names.add("daily_tasks")
        latest_job, success_job = _job_context(job_names, runs)
        if name == "daily":
            _classify_daily_task(item, latest_job, current)
        else:
            _apply_job_state(item, latest_job, success_job)
            if not latest_job:
                item.status = "waiting_upstream"
                item.last_error = "等待上游任务完成"
        if switch and os.getenv(switch) != "1" and not latest_job:
            item.status = "disabled"
            item.last_error = f"{switch} 未开启"
        values[name] = item
        result.append(item)

    daily_date = _date_value(values["daily"].latest_value)
    for name in ("indicators",):
        item = values[name]
        item.latest_trade_date = item.latest_value
        if daily_date and _date_value(item.latest_value) and _date_value(item.latest_value) > daily_date:
            item.status = "failed"
            item.last_error = f"{name} 日期晚于 daily，可能存在分区不一致"

    latest, rows, symbols = _online_latest(warehouse)
    online = DatasetStatus("online", latest, latest[:10] if latest else None,
                           _intraday_status(latest, current, 10), rows=rows,
                           symbols=symbols, job_name="online_snapshot", source="在线快照 CSV")
    latest_job, success_job = _job_context({"online_snapshot"}, runs)
    _apply_job_state(online, latest_job, success_job)
    if minute_configured:
        online.status = "disabled"
        online.last_error = "分钟采集已启用，在线快照被抑制"
    result.append(online)

    latest, rows, symbols = _minute_latest(warehouse)
    minute = DatasetStatus("minute", latest, latest[:10] if latest else None,
                           _intraday_status(latest, current, 15), rows=rows,
                           symbols=symbols, job_name="minute_snapshot", source="腾讯分钟 CSV")
    latest_job, success_job = _job_context({"minute_snapshot"}, runs)
    _apply_job_state(minute, latest_job, success_job)
    if not minute_configured:
        minute.status = "disabled"
        minute.last_error = "WAREHOUSE_MINUTE_SNAPSHOT 未开启"
    result.append(minute)
    return result


def data_status(*, warehouse=None, job_runs=None, now: Optional[datetime] = None) -> dict:
    current = now or datetime.now()
    expected = latest_completed_trade_day(current)
    datasets = dataset_statuses(warehouse=warehouse, job_runs=job_runs, now=current)
    priority = {"failed": 4, "critical": 3, "stale": 2, "empty": 1, "disabled": 1, "unknown": 1, "healthy": 0}
    overall = max((item.status for item in datasets), key=lambda status: priority.get(status, 1), default="unknown")
    return {"status": "success", "overall_status": overall,
            "expected_trade_day": expected.isoformat(),
            "checked_at": current.isoformat(timespec="seconds"),
            "datasets": [item.to_dict() for item in datasets]}


def quick_daily_status(*, warehouse=None, now: Optional[datetime] = None) -> dict:
    """Read only the newest daily partition for workflow gating."""
    from StockInvestmentTool.warehouse.storage import Warehouse

    warehouse = warehouse or Warehouse()
    current = now or datetime.now()
    months = warehouse.available_months("daily")
    latest = None
    if months:
        frame = warehouse.read_daily(months[-1])
        if frame is not None and not frame.empty and "date" in frame.columns:
            values = pd.to_datetime(frame["date"], errors="coerce").dropna()
            if not values.empty:
                latest = values.max().date().isoformat()
    expected = latest_completed_trade_day(current)
    return {"dataset": "daily", "latest_value": latest,
            "expected_trade_day": expected.isoformat(),
            "status": classify_freshness(latest, expected)}
