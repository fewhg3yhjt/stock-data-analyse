"""Published-only industry rotation features and its reproducible decision service."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_industry_features_daily
from StockInvestmentTool.warehouse.storage import Warehouse, _atomic_parquet_write


FEATURE_COLUMNS = [
    "date", "industry_code", "industry_name", "industry_classification", "member_count", "valid_count",
    "up_count", "down_count", "up_ratio", "return_1d", "return_3d", "return_5d", "return_10d", "return_20d",
    "amount", "amount_ma5", "amount_ma20", "amount_ratio", "rank_1d", "rank_5d", "rank_20d",
    "leader_code", "leader_return", "leader_amount", "industry_score", "industry_state", "state_reason",
]


def _config() -> dict:
    path = Path(__file__).resolve().parents[1] / "config" / "industry_rotation.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("industry_rotation", {})


def _returns(group: pd.DataFrame, days: int) -> pd.Series:
    return group.groupby("code", sort=False)["close"].pct_change(days)


class IndustryFeaturesBuilder:
    """Build monthly features from the three published input datasets only."""

    def __init__(self, warehouse: Warehouse | None = None, allow_legacy: bool = False):
        self.warehouse = warehouse or Warehouse()
        self.allow_legacy = allow_legacy

    def build(self, *, start_date: str, end_date: str, as_of: str | None = None,
              partition_versions: dict | None = None) -> dict:
        as_of = min(as_of or end_date, end_date)
        access = DatasetAccess(self.warehouse)
        history_start = (pd.Timestamp(start_date) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
        input_versions = partition_versions or {}
        # stock_daily/indicators may be formally published with WARNING under
        # the existing daily-data policy; WARNING is consumable, FAIL is not.
        daily = access.load_dataset("stock_daily", history_start, as_of, required_quality="WARNING", allow_legacy=self.allow_legacy,
                                    partition_versions=input_versions.get("stock_daily"))
        membership = access.load_dataset("industry_membership", end_date=as_of, required_quality="PASS", allow_legacy=False,
                                         partition_versions=input_versions.get("industry_membership"))
        for name, result in (("stock_daily", daily), ("industry_membership", membership)):
            if not self.allow_legacy and (result.context.get("fallback_used") or result.context.get("source") != "published_dataset"):
                raise DatasetAccessError(f"{name} 不是 Published 数据，禁止生成行业轮动特征")
        if daily.data.empty or membership.data.empty:
            raise DatasetAccessError("行业轮动要求 Published stock_daily 和 industry_membership 均有数据")
        d = daily.data.copy()
        m = membership.data.copy()
        for frame, col in ((d, "date"), (m, "snapshot_date")):
            frame[col] = pd.to_datetime(frame[col]).dt.normalize()
            frame["code"] = frame["code"].astype(str).str.lower().str.replace(".", "", regex=False) if "code" in frame else frame.get("code")
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d["amount"] = pd.to_numeric(d.get("amount", 0), errors="coerce").fillna(0)
        pct = pd.to_numeric(d.get("pct_chg", pd.Series(index=d.index)), errors="coerce")
        d["pct_chg"] = pct.fillna(d.groupby("code")["close"].pct_change())
        d = d.sort_values(["code", "date"])
        for n in (1, 3, 5, 10, 20):
            d[f"ret_{n}d"] = d.groupby("code", sort=False)["close"].pct_change(n)
        # A snapshot is valid from its date until the next snapshot. This avoids
        # using a future membership snapshot for a historical as_of decision.
        classification = str(_config().get("classification", "csrc"))
        m = m[m["industry_classification"].astype(str).isin({classification, "证监会行业分类"})]
        m = m.sort_values("snapshot_date").drop_duplicates(["snapshot_date", "code", "industry_classification"])
        membership_available_from = (
            str(m["snapshot_date"].min())[:10] if not m.empty else None
        )
        stock_latest = str(d["date"].max())[:10] if not d.empty else None
        if membership_available_from and stock_latest and pd.Timestamp(membership_available_from) > pd.Timestamp(stock_latest):
            return {
                "status": "no_data", "rows": 0, "months": 0, "output_versions": {},
                "input_versions": {name: result.context.get("partition_versions", {})
                                    for name, result in (("industry_membership", membership), ("stock_daily", daily), ("indicators", indicators))},
                "membership_available_from": membership_available_from,
                "actual_data_as_of": None,
                "gap_reason": "membership 首次快照晚于 stock_daily 最新交易日，历史区间没有可用行业归属",
            }
        dates = pd.DataFrame({"date": sorted(d[(d["date"] >= pd.Timestamp(start_date))]["date"].unique())})
        rows = []
        for date in dates["date"]:
            snap = m[m["snapshot_date"] <= date]
            if snap.empty:
                continue
            snap = snap.loc[snap.groupby("code")["snapshot_date"].idxmax()]
            day = d[d["date"] == date].merge(snap[["code", "industry_code", "industry_name", "industry_classification"]], on="code", how="inner")
            if day.empty:
                continue
            for keys, grp in day.groupby(["industry_code", "industry_classification"], sort=False):
                code, classification = keys
                # BaoStock may return two display-name variants for one
                # industry code across source updates. The source code is the
                # identity; choose the most frequent source name for display
                # while aggregating all members into one industry row.
                name = grp["industry_name"].astype(str).value_counts().index[0]
                valid = grp[grp["close"].gt(0)].copy()
                returns = {n: float(valid[f"ret_{n}d"].mean()) if valid[f"ret_{n}d"].notna().any() else None for n in (1, 3, 5, 10, 20)}
                amount = float(valid["amount"].sum())
                # The rolling amount values are computed on the already grouped
                # daily aggregate below, so no cross-industry lookups are needed.
                up = int((valid["pct_chg"] > 0).sum())
                down = int((valid["pct_chg"] < 0).sum())
                leader = valid.sort_values(["pct_chg", "amount", "code"], ascending=[False, False, True]).iloc[0] if len(valid) else None
                row = {"date": date, "industry_code": str(code), "industry_name": str(name), "industry_classification": str(classification),
                       "member_count": int(len(grp)), "valid_count": int(len(valid)), "up_count": up, "down_count": down,
                       "up_ratio": up / len(valid) if len(valid) else None, **{f"return_{n}d": returns[n] for n in (1, 3, 5, 10, 20)},
                       "amount": amount, "leader_code": str(leader["code"]) if leader is not None else "",
                       "leader_return": float(leader["pct_chg"]) if leader is not None and pd.notna(leader["pct_chg"]) else None,
                       "leader_amount": float(leader["amount"]) if leader is not None else None}
                rows.append(row)
        out = pd.DataFrame(rows)
        if out.empty:
            return {
                "status": "no_data", "rows": 0, "months": 0, "output_versions": {},
                "input_versions": {name: result.context.get("partition_versions", {})
                                    for name, result in (("industry_membership", membership), ("stock_daily", daily), ("indicators", indicators))},
                "membership_available_from": membership_available_from,
                "actual_data_as_of": None,
                "gap_reason": "行业成员快照与股票交易日没有重叠，未使用未来快照补齐历史数据",
            }
        out = out.sort_values(["industry_code", "date"])
        for col, window in (("amount_ma5", 5), ("amount_ma20", 20)):
            out[col] = out.groupby("industry_code")["amount"].transform(lambda s: s.rolling(window, min_periods=1).mean())
        out["amount_ratio"] = (out["amount"] / out["amount_ma20"].replace(0, pd.NA)).clip(0, 3)
        for n in (1, 5, 20):
            out[f"rank_{n}d"] = out.groupby("date")[f"return_{n}d"].rank(method="min", ascending=False).astype("Int64")
        cfg = _config(); weights = cfg.get("weights", {})
        # Each component is bounded to [0, 1]. Returns are converted to a
        # cross-sectional percentile, while breadth/liquidity are already
        # bounded or clipped. This makes configured thresholds interpretable.
        for key in ("return_1d", "return_5d", "return_20d"):
            out[f"score_{key}"] = out.groupby("date")[key].rank(pct=True).fillna(0.5)
        out["score_up_ratio"] = out["up_ratio"].clip(0, 1).fillna(0)
        out["score_amount_ratio"] = (out["amount_ratio"] / 3).clip(0, 1).fillna(0)
        score_fields = {"return_1d": "score_return_1d", "return_5d": "score_return_5d", "return_20d": "score_return_20d", "up_ratio": "score_up_ratio", "amount_ratio": "score_amount_ratio"}
        out["industry_score"] = sum(float(weights.get(k, 0)) * out[v] for k, v in score_fields.items())
        strong = float(cfg.get("state_thresholds", {}).get("strong", .70)); weak = float(cfg.get("state_thresholds", {}).get("weak", .35))
        out["industry_state"] = out["industry_score"].map(lambda x: "strong" if x >= strong else "weak" if x < weak else "neutral")
        out["state_reason"] = out.apply(lambda r: f"score={r['industry_score']:.4f}; valid={int(r['valid_count'])}; up_ratio={r['up_ratio'] if pd.notna(r['up_ratio']) else 'NA'}", axis=1)
        out.loc[out["valid_count"] < int(cfg.get("minimum_valid_count", 3)), "industry_state"] = "insufficient_data"
        out = out[FEATURE_COLUMNS]
        output_dir = self.warehouse.base_dir / "candidates" / "industry_features_daily"; output_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for month, frame in out[out["date"] <= pd.Timestamp(as_of)].groupby(out["date"].dt.strftime("%Y-%m")):
            path = output_dir / f"{month}.parquet"; _atomic_parquet_write(frame, path); paths[month] = path
        actual_input_versions = {}
        for name, result in (("industry_membership", membership), ("stock_daily", daily)):
            actual_input_versions[name] = result.context.get("partition_versions", {})
        state = PipelineState(self.warehouse.meta_db_path)
        versions = state.record_output_versions(dataset_name="industry_features_daily", paths=paths, input_dataset="stock_daily", input_versions=actual_input_versions, builder_version="industry_features_builder.v1", schema_version="industry_features_daily.v1")
        quality = {}
        for month, version in versions.items():
            result = check_industry_features_daily(
                paths[month], expected_industries=int(m["industry_code"].nunique()),
                expected_as_of=as_of if month == max(paths) else None)
            quality[month] = result; state.quality(version, status=result["status"], checks=result["checks"], publish_allowed=result["publish_allowed"])
            if result["publish_allowed"]: Publisher(self.warehouse).publish(version)
        actual_data_as_of = str(out["date"].max())[:10]
        return {"status": "success", "rows": len(out), "months": len(paths), "output_versions": versions,
                "input_versions": actual_input_versions, "quality": quality,
                "membership_available_from": membership_available_from,
                "actual_data_as_of": actual_data_as_of, "gap_reason": None}


def build_industry_features(warehouse: Warehouse, *, start_date: str, end_date: str,
                            as_of: str | None = None, **kwargs) -> dict:
    """Small functional entry point used by jobs and integrations."""
    return IndustryFeaturesBuilder(warehouse).build(
        start_date=start_date, end_date=end_date, as_of=as_of, **kwargs)


class IndustryRotationService:
    """As-of industry decisions and stock screens, with Screen* persistence left to caller."""

    def __init__(self, warehouse: Warehouse | None = None, config: dict | None = None):
        self.warehouse = warehouse or Warehouse(); self.config = config or _config()
        self.last_context: dict = {}

    def features(self, as_of: str) -> pd.DataFrame:
        result = DatasetAccess(self.warehouse).load_dataset("industry_features_daily", end_date=as_of, required_quality="PASS", allow_legacy=False)
        frame = result.data.copy()
        if frame.empty:
            self.last_context = {"requested_as_of": as_of, "actual_data_as_of": None, "status": "no_data",
                                 "reason": "没有不晚于请求 as_of 的行业特征"}
            return frame
        dates = pd.to_datetime(frame["date"], errors="coerce")
        eligible = frame[dates <= pd.Timestamp(as_of)]
        actual = str(pd.to_datetime(eligible["date"]).max())[:10] if not eligible.empty else None
        if actual is None:
            self.last_context = {"requested_as_of": as_of, "actual_data_as_of": None, "status": "no_data",
                                 "reason": "没有不晚于请求 as_of 的行业特征"}
            return eligible
        selected = eligible[pd.to_datetime(eligible["date"]).dt.strftime("%Y-%m-%d") == actual].copy()
        self.last_context = {"requested_as_of": as_of, "actual_data_as_of": actual,
                             "status": "success", "reason": None,
                             "data_context": result.context}
        selected.attrs.update(self.last_context)
        return selected

    def decide(self, as_of: str) -> list[dict]:
        frame = self.features(as_of)
        return frame.sort_values(["industry_score", "industry_code"], ascending=[False, True]).to_dict("records")

    def screen_stocks(self, as_of: str, industry_code: str, *, definition=None) -> tuple[list, dict]:
        from StockInvestmentTool.biz.data_access import load_market_data
        from StockInvestmentTool.biz.screen import ScreenDefinition, ScreenExecutor
        membership = DatasetAccess(self.warehouse).load_dataset("industry_membership", end_date=as_of, required_quality="PASS", allow_legacy=False).data
        classification = str(self.config.get("classification", "csrc"))
        membership = membership[(membership["industry_code"].astype(str) == str(industry_code)) &
                                (membership["industry_classification"].astype(str) == classification)]
        symbols = membership["code"].astype(str).tolist()
        data = load_market_data(self.warehouse, start_date=(pd.Timestamp(as_of) - pd.Timedelta(days=365)).strftime("%Y-%m-%d"), end_date=as_of, symbols=symbols, required_quality="PASS")
        definition = definition or ScreenDefinition(screen_id="industry_rotation_stock", name="行业轮动行业内选股", condition_spec=self.config["stock_screen"]["condition_spec"], sort_spec=self.config["stock_screen"]["sort_spec"], display_fields=self.config["stock_screen"]["display_fields"])
        return ScreenExecutor(definition, data.data).execute(as_of)
