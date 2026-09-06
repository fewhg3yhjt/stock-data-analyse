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
    "return_1d", "return_3d", "return_5d", "return_10d", "return_20d",
    "market_return_1d", "market_return_3d", "market_return_5d", "market_return_10d",
    "relative_return_3d", "relative_return_5d", "relative_return_10d", "rs_5", "rs_5_change", "position_60", "amount_ratio", "amount_ratio_change",
    "ma5_slope", "ma5_slope_change", "rank_3d", "rank_5d", "rank_20d",
    "rank_1d", "rotation_rank", "rotation_rank_1d_ago", "rotation_rank_3d_ago", "rotation_rank_5d_ago", "rank_3d_change", "rank_5d_change", "strength_score", "strength_change", "rotation_score",
    "strength_level", "rotation_heat", "rotation_acceleration", "deterioration", "opportunity_score", "transition_type", "stage", "previous_stage", "stage_days", "transition", "reason", "advice",
]


def _config() -> dict:
    path = Path(__file__).resolve().parents[1] / "config" / "industry_rotation.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("industry_rotation", {})


def _pct_rank(series: pd.Series, ascending: bool = False) -> pd.Series:
    return series.rank(method="average", ascending=ascending, pct=True).mul(100)


def _cross_sectional_rank(frame: pd.DataFrame, field: str, *, ascending: bool = True) -> pd.Series:
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


def _v2_stage(row: pd.Series, previous_stage: str, history: pd.DataFrame) -> tuple[str, str, str]:
    """V2 data-driven stage detection; history explains transitions only."""
    def value(key, default=0.0):
        raw = row.get(key)
        return default if raw is None or pd.isna(raw) else float(raw)
    heat = value("rotation_heat")
    acceleration = value("rotation_acceleration")
    deterioration = value("deterioration")
    rank = value("rank_3d", 999)
    was_hot = bool(value("max_heat_10d") >= 75 or value("rank_best_10d", 999) <= 18 or value("max_relative_return_5d_pct_10d") >= 80)
    if was_hot and deterioration >= 70 and (value("rank_3d_change") < 0 or value("rs_5") < 0):
        return "FADING", "过去曾处于高热区域，当前排名或相对强度明显恶化", "注意风险，暂不参与"
    if heat >= 80 and (value("strength_level") >= 80 or rank <= 18 or value("position_60") >= .8):
        return "CLIMAX", "当前热度进入高位区域，边际改善开始需要谨慎观察", "谨慎追高"
    confirmation = value("rs_5") > 0 or value("outperform_days_3d") >= 2 or value("rank_3d_change") > 0
    if acceleration >= 70 and heat < 80 and rank <= 50 and value("rank_3d_change") > 0 and confirmation:
        return "STARTING", "改善速度明显且尚未过热，排名和短期相对表现同步改善", "重点关注，等待确认"
    improving = int(value("rank_3d_change") > 0) + int(value("rank_5d_change") > 0) + int(value("rs_5") >= 0) + int(40 <= rank <= 70)
    if acceleration >= 55 and acceleration < 70 and heat < 75 and improving >= 2:
        return "WARMING", "排名、相对强度或活跃度开始改善，但尚未确认启动", "提前观察"
    return "DORMANT", "当前没有形成明确的改善或退潮组合", "观望，等待确认"


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
        # Industry coverage can be WARNING while remaining a published,
        # date-valid input. Keep the quality in the output context instead of
        # blocking the V1 observation model entirely.
        industry = access.load_dataset("industry_daily", history_start, as_of, required_quality="WARNING",
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
        for n in (1, 3, 5, 10, 20):
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
        for n in (3, 5, 10):
            market[f"market_return_{n}d"] = (1 + market["market_return_1d"].fillna(0)).rolling(n).apply(lambda x: x.prod() - 1)
        boards = boards.merge(market[["market_return_1d", "market_return_3d", "market_return_5d", "market_return_10d"]], left_on="date", right_index=True, how="left")
        boards["relative_return_3d"] = boards["return_3d"] - boards["market_return_3d"]
        boards["relative_return_5d"] = boards["return_5d"] - boards["market_return_5d"]
        boards["relative_return_10d"] = boards["return_10d"] - boards["market_return_10d"]
        boards["rs_5"] = boards["relative_return_5d"]
        boards["rs_5_change"] = boards.groupby("industry_id")["rs_5"].diff()
        boards = boards.sort_values(["date", "industry_id"])
        for field, ascending in (("return_1d", False), ("return_3d", False), ("return_5d", False), ("return_20d", False)):
            boards[f"rank_{field.removeprefix('return_')}"] = boards.groupby("date")[field].rank(method="min", ascending=ascending)
        boards["rotation_rank"] = boards.groupby("date")["rs_5"].rank(method="min", ascending=False)
        for n in (1, 3, 5):
            boards[f"rotation_rank_{n}d_ago"] = boards.groupby("industry_id")["rotation_rank"].shift(n)
        boards["rank_3d_change"] = boards["rotation_rank_3d_ago"] - boards["rotation_rank"]
        boards["rank_5d_change"] = boards["rotation_rank_5d_ago"] - boards["rotation_rank"]
        boards["amount_ratio_change"] = boards.groupby("industry_id")["amount_ratio"].diff()
        boards["ma5_slope_change"] = boards.groupby("industry_id")["ma5_slope"].diff()

        boards["rank_1d_change"] = boards.groupby("industry_id")["rotation_rank"].diff().mul(-1)
        boards["rank_5d_change"] = boards.groupby("industry_id")["rank_5d"].diff().mul(-1)
        boards["outperform_1d"] = boards["return_1d"] > boards["market_return_1d"]
        boards["outperform_days_3d"] = boards.groupby("industry_id")["outperform_1d"].transform(lambda s: s.rolling(3, min_periods=3).sum())
        boards["outperform_days_5d"] = boards.groupby("industry_id")["outperform_1d"].transform(lambda s: s.rolling(5, min_periods=5).sum())
        # ``rank_5d`` is already a rank where 1 is best. Ascending percentile
        # therefore makes rank 1 the lowest percentile, which we invert to a
        # strength percentile near 100.
        boards["rank_strength_pct"] = 100 - _cross_sectional_rank(boards, "rotation_rank", ascending=True)
        boards["relative_return_5d_pct"] = _cross_sectional_rank(boards, "rs_5")
        boards["relative_return_10d"] = boards["return_10d"] - boards["market_return_10d"]
        boards["relative_return_10d_pct"] = _cross_sectional_rank(boards, "relative_return_10d")
        boards["relative_return_3d"] = boards["return_3d"] - boards["market_return_3d"]
        boards["relative_return_3d_pct"] = _cross_sectional_rank(boards, "relative_return_3d")
        boards["volume_pct"] = _cross_sectional_rank(boards, "amount_ratio")
        boards["persistence_pct"] = _cross_sectional_rank(boards, "outperform_days_5d")
        boards["strength_level"] = boards["rank_strength_pct"] * .45 + boards["relative_return_5d_pct"] * .30 + boards["relative_return_10d_pct"] * .25
        boards["rotation_heat"] = boards["rank_strength_pct"] * .35 + boards["relative_return_5d_pct"] * .30 + boards["position_60"].clip(0, 1).fillna(.5) * 100 * .20 + boards["volume_pct"] * .15
        boards["rotation_acceleration"] = (_cross_sectional_rank(boards, "rank_3d_change") * .35 + _cross_sectional_rank(boards, "rank_5d_change") * .25 + _cross_sectional_rank(boards, "rs_5_change") * .20 + boards["persistence_pct"] * .10 + boards["volume_pct"] * .10)
        boards["deterioration"] = (100 - _cross_sectional_rank(boards, "rank_3d_change")) * .45 + (100 - _cross_sectional_rank(boards, "rank_1d_change")) * .20 + (100 - _cross_sectional_rank(boards, "rs_5_change")) * .35
        boards["opportunity_score"] = ((100 - boards["rotation_heat"]) * .35 + boards["rotation_acceleration"] * .45 + boards["persistence_pct"] * .20).clip(0, 100)
        boards["max_heat_10d"] = boards.groupby("industry_id")["rotation_heat"].transform(lambda s: s.shift(1).rolling(10, min_periods=1).max())
        boards["rank_best_10d"] = boards.groupby("industry_id")["rank_5d"].transform(lambda s: s.shift(1).rolling(10, min_periods=1).min())
        boards["max_relative_return_5d_pct_10d"] = boards.groupby("industry_id")["relative_return_5d_pct"].transform(lambda s: s.shift(1).rolling(10, min_periods=1).max())
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
        boards = boards.sort_values(["industry_id", "date"])
        stages = []
        for _, row in boards.iterrows():
            history = boards[(boards["industry_id"] == row["industry_id"]) & (boards["date"] < row["date"])].tail(10)
            stage, reason, advice = _v2_stage(row, "", history)
            stages.append((stage, reason, advice))
        boards[["stage", "reason", "advice"]] = pd.DataFrame(stages, index=boards.index)
        boards["previous_stage"] = boards.groupby("industry_id")["stage"].shift(1).fillna("")
        stage_change = boards["stage"].ne(boards.groupby("industry_id")["stage"].shift())
        stage_group = stage_change.groupby(boards["industry_id"]).cumsum()
        boards["stage_days"] = boards.groupby(["industry_id", stage_group]).cumcount() + 1
        boards["transition"] = boards.apply(lambda row: f"{row['previous_stage']}->{row['stage']}" if row["previous_stage"] and row["previous_stage"] != row["stage"] else "", axis=1)
        boards["transition_type"] = boards.apply(lambda row: "JUMP" if row["previous_stage"] == "DORMANT" and row["stage"] in {"STARTING", "CLIMAX"} else "NORMAL", axis=1)
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
