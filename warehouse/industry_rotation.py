"""Published-only V1 industry rotation snapshots.

The V1 model intentionally uses price, relative strength, liquidity and
cross-sectional movement only. Intraday money flow is a separate observation
and must not mutate an official close-based rotation stage.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_industry_rotation_daily
from StockInvestmentTool.warehouse.storage import Warehouse, _atomic_parquet_write


ROTATION_COLUMNS = [
    "date", "industry_id", "industry_name", "classification", "close",
    "return_1d", "return_3d", "return_5d", "return_20d", "market_return_5d",
    "rs_5", "rs_5_change", "position_60", "amount_ratio", "amount_ratio_change",
    "ma5_slope", "ma5_slope_change", "rank_3d", "rank_5d", "rank_20d",
    "rank_3d_change", "strength_score", "strength_change", "rotation_score",
    "stage", "previous_stage", "stage_days", "transition", "reason", "advice",
]


def _config() -> dict:
    path = Path(__file__).resolve().parents[1] / "config" / "industry_rotation.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("industry_rotation", {})


def _pct_rank(series: pd.Series, ascending: bool = False) -> pd.Series:
    return series.rank(method="average", ascending=ascending, pct=True).mul(100)


def _cross_sectional_rank(frame: pd.DataFrame, field: str, *, ascending: bool = False) -> pd.Series:
    """Rank a metric within each close date, never across future dates."""
    return frame.groupby("date")[field].transform(
        lambda values: _pct_rank(values, ascending=ascending)
    )


def _number(value, digits: int = 6):
    return None if value is None or pd.isna(value) else round(float(value), digits)


def _stage(row: pd.Series, cfg: dict) -> tuple[str, str, str]:
    strength = float(row["strength_score"] or 0)
    rotation = float(row["rotation_score"] or 0)
    position = float(row["position_60"] or 0)
    return_5d = float(row["return_5d"] or 0)
    rank_change = float(row["rank_3d_change"] or 0)
    thresholds = cfg.get("stage_thresholds", {})
    if strength >= thresholds.get("climax_strength", 85) and position >= thresholds.get("climax_position", .8) and rotation < thresholds.get("climax_rotation", 45):
        return "CLIMAX", "强度和位置较高，但轮动改善不足，进入高位风险区", "谨慎，避免追高"
    if strength >= thresholds.get("rising_strength", 70) and rotation >= thresholds.get("rising_rotation", 55):
        return "RISING", "强度较高且轮动指标持续改善", "持有观察，避免追高"
    if rotation >= thresholds.get("starting_rotation", 60) and rank_change >= thresholds.get("starting_rank_change", 3):
        return "STARTING", "排名改善、相对强度和动量同步抬升", "重点关注，等待确认"
    if strength <= thresholds.get("fading_strength", 35) and rotation <= thresholds.get("fading_rotation", 40) and return_5d < 0:
        return "FADING", "强度、轮动和近期收益同步走弱", "观望，注意风险"
    if strength <= thresholds.get("dormant_strength", 45) and rotation >= thresholds.get("dormant_rotation", 55):
        return "DORMANT", "当前强度偏低，但排名和轮动指标开始改善", "低位观察"
    if strength <= thresholds.get("cold_strength", 25) and position <= thresholds.get("cold_position", .2):
        return "COLD", "强度和位置均处于低位，尚未出现明确改善", "暂不参与，等待止跌"
    return "DORMANT", "价格强度或轮动方向尚未形成一致信号", "观望，等待确认"


class IndustryRotationBuilder:
    """Build official close-based same-classification rotation snapshots."""

    def __init__(self, warehouse: Warehouse | None = None, allow_legacy: bool = False):
        self.warehouse = warehouse or Warehouse()
        self.allow_legacy = allow_legacy

    def build(self, *, start_date: str, end_date: str, as_of: str | None = None,
              partition_versions: dict | None = None) -> dict:
        as_of = min(as_of or end_date, end_date)
        history_start = (pd.Timestamp(start_date) - pd.Timedelta(days=100)).strftime("%Y-%m-%d")
        access = DatasetAccess(self.warehouse)
        versions = partition_versions or {}
        industry = access.load_dataset("industry_daily", history_start, as_of, required_quality="PASS",
                                       partition_versions=versions.get("industry_daily"))
        daily = access.load_dataset("stock_daily", history_start, as_of, required_quality="WARNING",
                                    allow_legacy=self.allow_legacy, partition_versions=versions.get("stock_daily"))
        if industry.data.empty or daily.data.empty:
            raise DatasetAccessError("行业轮动 V1 要求 Published industry_daily 和 stock_daily")
        for name, result in (("industry_daily", industry), ("stock_daily", daily)):
            if result.context.get("fallback_used") or result.context.get("source") != "published_dataset":
                raise DatasetAccessError(f"{name} 不是 Published 数据，禁止生成正式轮动状态")

        boards = industry.data.copy()
        boards["date"] = pd.to_datetime(boards["trading_date"], errors="coerce").dt.normalize()
        boards["industry_id"] = boards["industry_id"].astype(str)
        boards["industry_name"] = boards["industry_name"].astype(str)
        boards["close"] = pd.to_numeric(boards["close"], errors="coerce")
        boards["amount"] = pd.to_numeric(boards["amount"], errors="coerce").fillna(0)
        boards = boards.dropna(subset=["date", "close"]).drop_duplicates(["date", "industry_id"])
        boards = boards.sort_values(["industry_id", "date"])
        for n in (1, 3, 5, 20):
            boards[f"return_{n}d"] = boards.groupby("industry_id")["close"].pct_change(n)
        boards["ma5"] = boards.groupby("industry_id")["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
        boards["ma5_slope"] = boards.groupby("industry_id")["ma5"].pct_change()
        boards["amount_ma20"] = boards.groupby("industry_id")["amount"].transform(lambda s: s.rolling(20, min_periods=5).mean())
        boards["amount_ratio"] = boards["amount"] / boards["amount_ma20"].replace(0, pd.NA)
        boards["position_60"] = boards.groupby("industry_id")["close"].transform(
            lambda s: (s - s.rolling(60, min_periods=20).min()) /
            (s.rolling(60, min_periods=20).max() - s.rolling(60, min_periods=20).min()).replace(0, pd.NA)
        )

        stocks = daily.data.copy()
        stocks["date"] = pd.to_datetime(stocks["date"], errors="coerce").dt.normalize()
        stocks["code"] = stocks["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        stocks["close"] = pd.to_numeric(stocks["close"], errors="coerce")
        stocks = stocks.dropna(subset=["date", "close"]).sort_values(["code", "date"])
        stock_returns = stocks.assign(ret=stocks.groupby("code")["close"].pct_change())
        market = stock_returns.groupby("date")["ret"].mean().rename("market_return_1d").to_frame()
        market["market_return_5d"] = (1 + market["market_return_1d"].fillna(0)).rolling(5).apply(lambda x: x.prod() - 1)
        boards = boards.merge(market[["market_return_5d"]], left_on="date", right_index=True, how="left")
        boards["rs_5"] = boards["return_5d"] - boards["market_return_5d"]
        boards["rs_5_change"] = boards.groupby("industry_id")["rs_5"].diff()
        boards = boards.sort_values(["date", "industry_id"])
        for field, ascending in (("return_3d", False), ("return_5d", False), ("return_20d", False)):
            boards[f"rank_{field.removeprefix('return_')}"] = boards.groupby("date")[field].rank(method="min", ascending=ascending)
        boards["rank_3d_change"] = boards.groupby("industry_id")["rank_3d"].diff().mul(-1)
        boards["amount_ratio_change"] = boards.groupby("industry_id")["amount_ratio"].diff()
        boards["ma5_slope_change"] = boards.groupby("industry_id")["ma5_slope"].diff()

        boards["strength_score"] = (
            _cross_sectional_rank(boards, "return_5d") * .35 +
            _cross_sectional_rank(boards, "return_20d") * .25 +
            _cross_sectional_rank(boards, "rs_5") * .25 +
            boards["position_60"].clip(0, 1).fillna(.5) * 100 * .15
        )
        boards["strength_change"] = boards.groupby("industry_id")["strength_score"].diff()
        boards["rotation_score"] = (
            _cross_sectional_rank(boards, "rank_3d_change") * .35 +
            _cross_sectional_rank(boards, "rs_5_change") * .25 +
            _cross_sectional_rank(boards, "strength_change") * .20 +
            _cross_sectional_rank(boards, "ma5_slope_change") * .10 +
            _cross_sectional_rank(boards, "amount_ratio_change") * .10
        )
        boards = boards[boards["date"] <= pd.Timestamp(as_of)].copy()
        cfg = _config()
        boards[["stage", "reason", "advice"]] = boards.apply(lambda row: pd.Series(_stage(row, cfg)), axis=1)
        boards["previous_stage"] = boards.groupby("industry_id")["stage"].shift(1).fillna("")
        stage_change = boards["stage"].ne(boards.groupby("industry_id")["stage"].shift())
        stage_group = stage_change.groupby(boards["industry_id"]).cumsum()
        boards["stage_days"] = boards.groupby(["industry_id", stage_group]).cumcount() + 1
        boards["transition"] = boards.apply(lambda row: f"{row['previous_stage']}->{row['stage']}" if row["previous_stage"] and row["previous_stage"] != row["stage"] else "", axis=1)
        boards["classification"] = str(cfg.get("official_classification", "ths_industry"))
        out = boards.rename(columns={"industry_id": "industry_id"})[ROTATION_COLUMNS].copy()
        out = out[out["date"] >= pd.Timestamp(start_date)].sort_values(["date", "industry_id"])
        output_dir = self.warehouse.base_dir / "candidates" / "industry_rotation_daily"
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for month, frame in out.groupby(out["date"].dt.strftime("%Y-%m")):
            path = output_dir / f"{month}.parquet"
            _atomic_parquet_write(frame, path)
            paths[month] = path
        input_versions = {
            "industry_daily": industry.context.get("partition_versions", {}),
            "stock_daily": daily.context.get("partition_versions", {}),
        }
        state = PipelineState(self.warehouse.meta_db_path)
        output_versions = state.record_output_versions(
            dataset_name="industry_rotation_daily", paths=paths,
            input_dataset="industry_daily", input_versions=input_versions,
            builder_version="industry_rotation_builder.v1", schema_version="industry_rotation_daily.v1",
        )
        quality = {}
        for month, version in output_versions.items():
            report = check_industry_rotation_daily(paths[month], expected_as_of=as_of)
            quality[month] = report
            state.quality(version, status=report["status"], checks=report["checks"], publish_allowed=report["publish_allowed"])
            if report["publish_allowed"]:
                Publisher(self.warehouse).publish(version)
        return {"status": "success", "rows": len(out), "months": len(paths), "actual_data_as_of": str(out["date"].max())[:10],
                "output_versions": output_versions, "quality": quality, "input_versions": input_versions}
