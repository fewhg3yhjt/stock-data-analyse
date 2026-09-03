"""固定 GitHub 快照的同花顺行业成员导入。"""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_ths_industry_membership

COMMIT = "fad8b3374fc8605ac723f5626e600d554b0c222a"
CSV_URL = f"https://raw.githubusercontent.com/panghu11033/thsdk/{COMMIT}/data/industry_constituents.csv"
_BASELINE = load_dataset_config("ths_industry_membership")["quality"]["source_baseline"]
EXPECTED_INDUSTRIES = int(_BASELINE["industries"])
EXPECTED_ROWS = int(_BASELINE["rows"])


def build_ths_industry_candidate(warehouse, partition: str, raw_path: Path) -> dict:
    """Build the snapshot candidate for the long-form THS relation.

    This dataset is snapshot-partitioned; it must not go through the generic
    industry builder, whose monthly filtering and industry_daily path rules
    are intentionally specific to market series.
    """
    import hashlib

    config = load_dataset_config("ths_industry_membership")
    frame = pd.read_parquet(raw_path)
    fields = [field["name"] for field in config["fields"]]
    missing = sorted(set(fields) - set(frame.columns))
    if missing:
        raise ValueError(f"ths_industry_membership 缺少字段: {', '.join(missing)}")
    frame = frame[fields].copy()
    frame["snapshot_date"] = frame["snapshot_date"].astype(str).str[:10]
    if str(partition) != "" and not frame["snapshot_date"].eq(str(partition)).all():
        raise ValueError(f"快照分区不一致: 期望 {partition}")
    keys = config["dataset"]["primary_keys"]
    frame = frame.drop_duplicates(keys).sort_values(keys).reset_index(drop=True)
    path = warehouse.base_dir / "candidates" / "ths_industry_membership" / str(partition) / f"ths_industry_membership_{str(partition).replace('-', '')}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False, engine="pyarrow", compression="zstd")
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"version_id": f"ths_industry_membership_{str(partition).replace('-', '')}_{checksum[:12]}",
            "dataset_name": "ths_industry_membership", "partition": str(partition),
            "path": path, "row_count": len(frame),
            "symbol_count": int(frame["code"].nunique()), "checksum": checksum,
            "source_batches": [str(raw_path)]}


def canonical_code(value: str) -> str:
    value = str(value or "").strip().upper().replace(".", "")
    for prefix, market in (("USHA", "sh"), ("USZA", "sz"), ("USTM", "bj")):
        if value.startswith(prefix):
            return market + value[len(prefix):].zfill(6)
    if re.fullmatch(r"\d{6}", value):
        return ("sh" if value.startswith(("5", "6", "68", "9")) else "sz") + value
    return value.lower()


def _member(value: str) -> tuple[str, str]:
    """Extract a code and optional name from common THS member spellings."""
    raw = str(value or "").strip()
    market_match = re.search(r"(?i)(USHA|USZA|USTM)(\d{6})", raw)
    if market_match:
        code = canonical_code(market_match.group(0))
        name = (raw[:market_match.start()] + raw[market_match.end():]).strip(" -:：()（）[]【】")
        return code, name
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    if not match:
        return "", ""
    code = canonical_code(match.group(1))
    name = (raw[:match.start()] + raw[match.end():]).strip(" -:：()（）[]【】")
    return code, name


def _pick(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    for name in names:
        if name in frame.columns:
            return frame[name]
    return pd.Series("", index=frame.index)


def parse_ths_constituents(data, *, snapshot_date: str, source_commit: str = COMMIT,
                           source: str = "github", captured_at: str | None = None) -> pd.DataFrame:
    """Parse the compact CSV without imposing a particular header spelling."""
    if isinstance(data, (bytes, bytearray)):
        data = io.BytesIO(data)
    frame = data.copy() if isinstance(data, pd.DataFrame) else pd.read_csv(data)
    industry_ids = _pick(frame, ("industry_id", "industry_code", "行业代码", "板块代码", "code"))
    industry_names = _pick(frame, ("industry_name", "行业名称", "板块名称", "name", "板块"))
    members = _pick(frame, ("members", "constituents", "成分股", "成员", "股票列表"))
    member_codes = _pick(frame, ("stock_code", "symbol", "证券代码", "股票代码", "member_code"))
    member_names = _pick(frame, ("stock_name", "证券名称", "股票名称", "member_name"))
    # Accept both the compact source shape (one industry + a member string)
    # and an already-expanded source shape (one member per row).
    expanded = pd.DataFrame({"industry_id": industry_ids, "industry_name": industry_names,
                             "members": members, "member_code": member_codes,
                             "member_name": member_names})
    rows = []
    for item in expanded.itertuples(index=False):
        tokens = [item.member_code] if str(item.member_code).strip() not in ("", "nan", "None") else re.split(r"[,，;；\s]+", str(item.members))
        for token in tokens:
            code, parsed_name = _member(str(token).split(":", 1)[0].strip())
            if not code:
                continue
            rows.append({"snapshot_date": snapshot_date, "industry_id": item.industry_id,
                         "industry_name": item.industry_name, "code": code,
                         "stock_name": (item.member_name if str(item.member_name) not in ("", "nan", "None") else parsed_name or code),
                         "source_commit": source_commit, "source": source,
                         "captured_at": captured_at or datetime.now().isoformat(timespec="seconds")})
    result = pd.DataFrame(rows)
    if result.empty:
        return pd.DataFrame(columns=["snapshot_date", "industry_id", "industry_name", "code", "stock_name", "source_commit", "source", "captured_at"])
    result["industry_id"] = result["industry_id"].astype(str).str.replace(r"(?i)^URFI", "", regex=True).str.strip()
    result["industry_name"] = result["industry_name"].astype(str).str.strip()
    return result[list(field["name"] for field in load_dataset_config("ths_industry_membership")["fields"])] \
        .drop_duplicates(["snapshot_date", "industry_id", "code"]).reset_index(drop=True)


def import_ths_industry_membership(warehouse, *, snapshot_date: str, csv_path: Path | None = None,
                                   source_commit: str = COMMIT) -> dict:
    """Run Raw -> Candidate -> Quality -> Publish; no formal file is written directly."""
    payload = Path(csv_path).read_bytes() if csv_path else urlopen(
        f"https://raw.githubusercontent.com/panghu11033/thsdk/{source_commit}/data/industry_constituents.csv", timeout=15
    ).read()
    frame = parse_ths_constituents(payload, snapshot_date=snapshot_date, source_commit=source_commit)
    captured = capture_frames(warehouse, dataset_name="ths_industry_membership", source_name="github",
        frames=[frame], run_date=snapshot_date, trade_date_start=snapshot_date, trade_date_end=snapshot_date,
        expected_symbols=len(frame), success_symbols=len(frame), universe_id="ths_industry",
        request_context={"source_commit": source_commit, "url": CSV_URL}, schema_version="ths_industry_membership.v1")
    build = build_ths_industry_candidate(warehouse, snapshot_date, Path(captured["raw"]["path"]))
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[captured["batch_id"]], dataset_name="ths_industry_membership", schema_version="ths_industry_membership.v1")
    baseline = source_commit == COMMIT
    quality = check_ths_industry_membership(
        build["path"],
        expected_industries=EXPECTED_INDUSTRIES if baseline else None,
        expected_rows=EXPECTED_ROWS if baseline else None,
        expected_source_commit=COMMIT if baseline else None,
    )
    state.quality(version, status=quality["status"], checks=quality["checks"], publish_allowed=quality["publish_allowed"])
    published = Publisher(warehouse).publish(version) if quality["publish_allowed"] else None
    return {"raw_batch_id": captured["batch_id"], "version_id": version,
            "expected_industries": int(frame["industry_id"].nunique()),
            "rows": int(len(frame)), "quality": quality, "published": published}
