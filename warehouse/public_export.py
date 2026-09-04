"""Static JSON exports generated only from published daily datasets."""

from __future__ import annotations

import json
import os
import hashlib
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError


DEFAULT_PUBLIC_DAILY_CODES = (
    "000400",  # 许继电气
    "000425",  # 徐工机械
    "000725",  # 京东方A
    "603019",  # 中科曙光
    "300750",  # 宁德时代
    "601318",  # 中国平安
    "600519",  # 贵州茅台
    "600036",  # 招商银行
    "002594",  # 比亚迪
    "600031",  # 三一重工
    "601899",  # 紫金矿业
    "600941",  # 中国移动
)


def _canonical_code(value: str) -> tuple[str, str]:
    raw = str(value or "").strip().lower().replace(".", "").replace("_", "").replace("-", "")
    suffix = ""
    if raw.endswith(("sz", "sh", "bj")):
        suffix, raw = raw[-2:], raw[:-2]
    elif raw.startswith(("sz", "sh", "bj")):
        suffix, raw = raw[:2], raw[2:]
    if len(raw) != 6 or not raw.isdigit():
        raise ValueError(f"股票代码必须为 6 位数字: {value}")
    market = "sh" if raw.startswith(("60", "68", "69")) else "sz" if raw.startswith(("00", "20", "30")) else "bj" if raw.startswith(("4", "8")) else ""
    if not market or suffix and suffix != market:
        raise ValueError(f"无法识别股票代码市场: {value}")
    return raw, f"{market}{raw}"


def configured_public_daily_codes() -> tuple[str, ...]:
    configured = os.getenv("PUBLIC_DAILY_EXPORT_CODES", "")
    return tuple(item.strip() for item in configured.split(",") if item.strip()) or DEFAULT_PUBLIC_DAILY_CODES


def _json_value(value):
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return _json_value(value.item())
    return value


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_public_daily(warehouse, codes: tuple[str, ...] | list[str] | None = None) -> dict:
    """Atomically export complete, published qfq history for each configured stock."""
    requested = codes or configured_public_daily_codes()
    normalized = [_canonical_code(code) for code in requested]
    access = DatasetAccess(warehouse)
    versions = access.get_current_version("stock_daily")
    if not versions:
        raise DatasetAccessError("stock_daily 没有 Published Dataset")
    fields = ("date", "code", "open", "high", "low", "close", "volume", "amount", "pre_close", "turn")
    symbols = [internal_code for _, internal_code in normalized]
    frames = {internal_code: [] for internal_code in symbols}
    partition_versions = {}
    qualities = []
    # Read one published partition at a time and push the symbol predicate down
    # to PyArrow. A static export must not load the full market history into RAM.
    for partition, current in sorted(versions.items()):
        version, quality = access._version_context(current["version_id"])
        path = Path(version.get("published_path") or "")
        if version.get("publish_status") != "published" or not path.exists():
            raise DatasetAccessError(f"stock_daily/{partition} Published 版本不可读")
        if not access._quality_allowed(quality, "WARNING"):
            raise DatasetAccessError(f"stock_daily/{partition} Published 版本质量不满足要求")
        if _checksum(path) != version.get("checksum"):
            raise DatasetAccessError(f"stock_daily/{partition} Published 版本 checksum 不匹配")
        import pyarrow.parquet as pq
        available = [field for field in fields if field in pq.ParquetFile(path).schema.names]
        partition_frame = pd.read_parquet(path, columns=available, filters=[("code", "in", symbols)])
        for internal_code, group in partition_frame.groupby("code"):
            frames[str(internal_code)].append(group)
        partition_versions[partition] = version["version_id"]
        qualities.append(quality["status"])
    destination = warehouse.base_dir.parent.parent / "public-data"
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for public_code, internal_code in normalized:
        rows = pd.concat(frames[internal_code], ignore_index=True) if frames[internal_code] else pd.DataFrame(columns=fields)
        rows = rows.sort_values("date")
        records = []
        for item in rows[[field for field in fields if field in rows]].to_dict(orient="records"):
            record = {key: _json_value(value) for key, value in item.items()}
            record["date"] = str(record["date"])[:10]
            if "turn" in record:
                record["turnover_rate"] = record.pop("turn")
            records.append(record)
        instrument = warehouse.get_instrument(internal_code) or {}
        payload = {
            "status": "ok",
            "code": public_code,
            "name": instrument.get("name") or "",
            "adjust": "qfq",
            "count": len(records),
            "start_date": records[0]["date"] if records else None,
            "end_date": records[-1]["date"] if records else None,
            "data": records,
            "meta": {
                "data_as_of": records[-1]["date"] if records else None,
                "quality_status": "WARNING" if "WARNING" in qualities else "PASS",
                "source": "published_dataset",
                "partition_versions": partition_versions,
            },
        }
        target = destination / f"{public_code}.json"
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, target)
        written.append({"code": public_code, "path": str(target), "count": len(records)})
    return {"directory": str(destination), "exports": written}
