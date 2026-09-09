"""Build deterministic stock_daily candidates from immutable Raw Batches."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config


class DailyBuilder:
    def __init__(self, warehouse, config_path: Path | str | None = None):
        self.warehouse = warehouse
        self.config = load_dataset_config("stock_daily", config_path)
        self._sources = {item["name"]: item for item in self.config["sources"]}

    def select_raw_batches(self, partition: str, sources: Optional[list[str]] = None) -> list[tuple[str, Path, str]]:
        """Select completed, existing source batches overlapping a month."""
        month_start = pd.Timestamp(f"{partition}-01")
        month_end = month_start + pd.offsets.MonthEnd(1)
        with self.warehouse._conn() as conn:
            rows = conn.execute(
                "SELECT batch_id, source_name, raw_path, trade_date_start, trade_date_end "
                "FROM source_batches WHERE dataset_name='stock_daily' "
                "AND status IN ('success','partial_success') AND raw_path IS NOT NULL "
                "ORDER BY finished_at DESC, batch_id DESC"
            ).fetchall()
        selected = []
        for batch_id, source, raw_path, date_start, date_end in rows:
            if sources and source not in sources:
                continue
            path = Path(raw_path)
            if source not in self._sources or not path.exists():
                continue
            batch_start = pd.Timestamp(date_start) if date_start else month_start
            batch_end = pd.Timestamp(date_end) if date_end else month_end
            if batch_start <= month_end and batch_end >= month_start:
                selected.append((source, path, batch_id))
        if not selected:
            raise ValueError(f"没有找到 {partition} 可用的 stock_daily Raw Batch")
        return selected

    def select_effective_raw(self, partition: str, sources: Optional[list[str]] = None) -> list[tuple[str, Path, str]]:
        """Select date-level Current Raw files within a monthly partition."""
        selected = []
        month_start = pd.Timestamp(f"{partition}-01")
        month_end = month_start + pd.offsets.MonthEnd(1)
        for source in sources or list(self._sources):
            root = self.warehouse.raw.raw_dir / "effective" / source / "stock_daily"
            if not root.exists():
                continue
            for path in sorted(root.glob(f"{month_start:%Y}/{month_start:%m}/*.parquet")):
                data_date = pd.Timestamp(f"{path.parent.parent.name}-{path.parent.name}-{path.stem}")
                if month_start <= data_date <= month_end:
                    selected.append((source, path, f"effective:{source}:stock_daily:{path.stem}"))
        return selected

    def _normalize(self, frame: pd.DataFrame, source: str, *, units: dict | None = None) -> pd.DataFrame:
        if source not in self._sources:
            raise ValueError(f"未注册的数据源: {source}")
        source_config = self._sources[source]
        mapping = source_config.get("field_mapping", {})
        factors = self._unit_conversion_factors(source)
        out = frame.rename(columns={raw: standard for standard, raw in mapping.items()}).copy()
        required = {"date", "code"}
        if not required.issubset(out.columns):
            raise ValueError(f"{source} Raw 缺少主键字段: {sorted(required - set(out.columns))}")
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["code"] = out["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        units = units or {}
        row_volume_units = out.get("raw_volume_unit")
        row_amount_units = out.get("raw_amount_unit")
        if source == "tencent" and row_volume_units is None:
            # Batch-level Tencent units may describe mixed historical data and
            # must not override row-level/type-aware interpretation.
            row_volume_units, row_amount_units = self._infer_tencent_units(
                out, source_config.get("unit_rules", [])
            )
        if row_volume_units is not None:
            out["raw_volume_unit"] = row_volume_units
        volume_unit = units.get("volume")
        amount_unit = units.get("amount")
        volume_factors = factors.get("volume", {})
        amount_factors = factors.get("amount", {})
        if row_volume_units is not None:
            invalid = ~row_volume_units.astype(str).map(
                lambda unit: f"{unit}_to_share" in volume_factors
            )
            if invalid.any():
                raise ValueError(f"{source} Raw Batch 存在未知 volume 单位行数: {int(invalid.sum())}")
            volume_unit = None
        if row_amount_units is not None:
            invalid = ~row_amount_units.astype(str).map(
                lambda unit: f"{unit}_to_yuan" in amount_factors
            )
            if invalid.any():
                raise ValueError(f"{source} Raw Batch 存在未知 amount 单位行数: {int(invalid.sum())}")
            amount_unit = None
        if row_volume_units is None and f"{volume_unit}_to_share" not in volume_factors:
            raise ValueError(f"{source} Raw Batch volume 单位未识别: {volume_unit!r}")
        if row_amount_units is None and f"{amount_unit}_to_yuan" not in amount_factors:
            raise ValueError(f"{source} Raw Batch 单位未识别: volume={volume_unit!r}, amount={amount_unit!r}")
        if "volume" in out:
            values = pd.to_numeric(out["volume"], errors="coerce")
            if row_volume_units is None:
                factor = factors.get("volume", {}).get(f"{volume_unit}_to_share")
                if factor is None:
                    raise ValueError(f"{source} Raw Batch 未配置 volume 转换: {volume_unit!r}")
                out["volume"] = values * factor
            else:
                factors_by_unit = row_volume_units.astype(str).map(
                    lambda unit: factors.get("volume", {}).get(f"{unit}_to_share")
                )
                if factors_by_unit.isna().any():
                    raise ValueError(f"{source} Raw Batch 未配置 volume 转换")
                out["volume"] = values * factors_by_unit
        if "amount" in out:
            values = pd.to_numeric(out["amount"], errors="coerce")
            if row_amount_units is None:
                factor = factors.get("amount", {}).get(f"{amount_unit}_to_yuan")
                if factor is None:
                    raise ValueError(f"{source} Raw Batch 未配置 amount 转换: {amount_unit!r}")
                out["amount"] = values * factor
            else:
                factors_by_unit = row_amount_units.astype(str).map(
                    lambda unit: factors.get("amount", {}).get(f"{unit}_to_yuan")
                )
                if factors_by_unit.isna().any():
                    raise ValueError(f"{source} Raw Batch 未配置 amount 转换")
                out["amount"] = values * factors_by_unit
        for field in self.config["fields"]:
            if field["name"] not in out:
                out[field["name"]] = pd.NA
        return out[[field["name"] for field in self.config["fields"]]]

    def _infer_tencent_units(self, frame: pd.DataFrame, rules: list[dict]) -> tuple[pd.Series, pd.Series]:
        """Infer legacy Tencent units from configured rules and value consistency."""
        codes = frame["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        volume = pd.to_numeric(frame.get("volume"), errors="coerce")
        amount = pd.to_numeric(frame.get("amount"), errors="coerce")
        close = pd.to_numeric(frame.get("close"), errors="coerce")
        conversion = self._unit_conversion_factors("tencent")
        candidates = {}
        for rule in rules:
            volume_unit, amount_unit = rule["volume"], rule["amount"]
            key = f"{volume_unit}/{amount_unit}"
            amount_factor = conversion.get("amount", {}).get(f"{amount_unit}_to_yuan")
            volume_factor = conversion.get("volume", {}).get(f"{volume_unit}_to_share")
            if amount_factor is None or volume_factor is None:
                raise ValueError(f"tencent unit_rules 引用了未配置的单位: {key}")
            candidates[key] = amount * amount_factor / (volume * volume_factor * close)
        if not candidates:
            raise ValueError("tencent Raw 缺少 unit_rules 配置")
        default = next((rule for rule in rules if rule.get("default")), rules[-1])
        preferred = pd.Series(
            f"{default['volume']}/{default['amount']}", index=frame.index
        )
        for rule in rules:
            if rule.get("default") or rule.get("inference_only"):
                continue
            prefixes = tuple(str(prefix).lower() for prefix in rule.get("code_prefixes", []))
            if prefixes:
                preferred.loc[codes.str.startswith(prefixes)] = f"{rule['volume']}/{rule['amount']}"
        result = preferred.copy()
        candidate_names = list(candidates)
        candidate_values = pd.DataFrame(candidates, index=frame.index)
        valid = candidate_values.notna() & candidate_values.ge(0.2) & candidate_values.le(5.0)
        distances = candidate_values.sub(1.0).abs().where(valid)
        preferred_matrix = pd.DataFrame(
            {name: preferred.eq(name) for name in candidate_names}, index=frame.index
        )
        scores = distances + (~preferred_matrix).astype(float) * 1e-12
        nearest = scores.fillna(float("inf")).idxmin(axis=1)
        nearest.loc[distances.isna().all(axis=1)] = pd.NA
        result.loc[nearest.notna()] = nearest.loc[nearest.notna()]
        volume_units = result.str.split("/").str[0]
        amount_units = result.str.split("/").str[1]
        return volume_units, amount_units

    def _unit_conversion_factors(self, source: str) -> dict[str, dict[str, float]]:
        conversions = self._sources[source].get("unit_conversions", {})
        return {
            field: {str(key): float(value) for key, value in (values or {}).items()}
            for field, values in conversions.items()
        }

    def _normalize_current(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        for field in self.config["fields"]:
            if field["name"] not in out:
                out[field["name"]] = pd.NA
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["code"] = out["code"].astype(str).str.lower().str.replace(".", "", regex=False)
        return out[[field["name"] for field in self.config["fields"]]]

    def _read_raw_partition(self, path: Path, source: str, partition: str, *, units: dict) -> pd.DataFrame:
        """Read only one month from a raw batch without materializing the batch.

        Raw batches may contain several years of data.  Reading the complete
        parquet file for every monthly build can exceed the production
        container memory limit, so process bounded Arrow batches instead.
        """
        import pyarrow.parquet as pq

        month_start = pd.Timestamp(f"{partition}-01")
        month_end = month_start + pd.offsets.MonthEnd(1)
        chunks = []
        parquet = pq.ParquetFile(path)
        for record_batch in parquet.iter_batches(batch_size=50_000):
            frame = self._normalize(record_batch.to_pandas(), source, units=units)
            dates = pd.to_datetime(frame["date"], errors="coerce")
            frame = frame[(dates >= month_start) & (dates <= month_end)]
            if not frame.empty:
                chunks.append(frame)
        if not chunks:
            return pd.DataFrame(columns=[field["name"] for field in self.config["fields"]])
        return pd.concat(chunks, ignore_index=True)

    def build_partition(self, partition: str, raw_batches: Optional[Iterable[tuple]] = None,
                        *, include_current: bool = True) -> dict:
        selected_batches = list(raw_batches) if raw_batches is not None else self.select_raw_batches(partition)
        effective = self.select_effective_raw(partition)
        if effective:
            selected_batches = effective + selected_batches
        frames = []
        source_frames: dict[str, pd.DataFrame] = {}
        batch_ids = []
        with self.warehouse._conn() as conn:
            batch_contexts = {
                row[0]: json.loads(row[1] or "{}")
                for row in conn.execute("SELECT batch_id, request_context FROM source_batches").fetchall()
            }
            path_batch_ids = {
                str(row[1]): row[0]
                for row in conn.execute("SELECT batch_id, raw_path FROM source_batches WHERE raw_path IS NOT NULL").fetchall()
            }
        for item in selected_batches:
            source, path = item[:2]
            batch_id = item[2] if len(item) > 2 else path_batch_ids.get(str(path), "")
            batch_ids.append(batch_id or str(path))
            context = batch_contexts.get(batch_id, {})
            units = context.get("units")
            if units is None:
                # The legacy Tencent adapter infers units per row in _normalize:
                # normal securities are hand/wan_yuan and sh68* are
                # share/wan_yuan. Keep the inferred rule in the build only.
                if source == "tencent":
                    units = {}
                # Other sources may use an explicit source-contract default.
                else:
                    fallback = self._sources.get(source, {}).get("raw_units")
                    if fallback is None:
                        raise ValueError(f"{source} Raw Batch {batch_id or path} 缺少单位元数据")
                    units = {**fallback, "resolution": "source_config_default"}
            elif source == "tencent":
                # Historical Batch-level declarations can be wrong for mixed
                # stock/ETF/STAR data; _normalize resolves each row instead.
                units = {}
            frame = self._read_raw_partition(path, source, partition, units=units)
            if frame is not None and not frame.empty:
                previous = source_frames.get(source)
                source_frames[source] = (pd.concat([previous, frame], ignore_index=True)
                                         if previous is not None and not previous.empty else frame)
                frame["_source"] = (
                    f"effective:{source}" if str(batch_id).startswith("effective:") else source
                )
                frames.append(frame)
        if include_current:
            current = self.warehouse.read_daily(partition)
            if current is not None and not current.empty:
                current_frame = self._normalize_current(current)
                current_frame["_source"] = "legacy_daily"
                frames.append(current_frame)
        if not frames:
            raise ValueError("没有可用于构建的 Raw Batch")
        return self._finalize_partition(
            partition, frames, source_frames, batch_ids, include_current=include_current,
        )

    def build_partitions(self, partitions: Iterable[str], raw_batches: Optional[Iterable[tuple]] = None,
                         *, include_current: bool = True) -> dict[str, dict]:
        """Build several monthly partitions in one bounded Raw scan.

        A large historical Raw batch commonly spans many months.  Scanning it
        once per output month multiplies I/O and unit inference cost, so route
        each normalized Arrow chunk to its target month before finalizing each
        partition independently.
        """
        partitions = sorted(set(partitions))
        if not partitions:
            return {}
        selected_batches = self.select_raw_batches(partitions[0]) if raw_batches is None else list(raw_batches)
        if raw_batches is None:
            selected_batches = []
            for partition in partitions:
                for item in self.select_raw_batches(partition):
                    if item not in selected_batches:
                        selected_batches.append(item)
        targets = set(partitions)
        staging_root = self.warehouse.base_dir / "candidates" / "stock_daily" / f".build-{uuid.uuid4().hex}"
        staging_root.mkdir(parents=True, exist_ok=True)
        writers = {}
        selected_sources = {item[0] for item in selected_batches}
        track_source_frames = len(selected_sources) > 1
        source_frames_by_month: dict[str, dict[str, list[pd.DataFrame]]] = {
            partition: {} for partition in partitions
        }
        batch_ids = []
        with self.warehouse._conn() as conn:
            batch_contexts = {
                row[0]: json.loads(row[1] or "{}")
                for row in conn.execute("SELECT batch_id, request_context FROM source_batches").fetchall()
            }
            path_batch_ids = {
                str(row[1]): row[0]
                for row in conn.execute(
                    "SELECT batch_id, raw_path FROM source_batches WHERE raw_path IS NOT NULL"
                ).fetchall()
            }
        import pyarrow.parquet as pq
        for item in selected_batches:
            source, path = item[:2]
            batch_id = item[2] if len(item) > 2 else path_batch_ids.get(str(path), "")
            batch_ids.append(batch_id or str(path))
            context = batch_contexts.get(batch_id, {})
            units = context.get("units")
            if source == "tencent":
                units = {}
            elif units is None:
                fallback = self._sources.get(source, {}).get("raw_units")
                if fallback is None:
                    raise ValueError(f"{source} Raw Batch {batch_id or path} 缺少单位元数据")
                units = {**fallback, "resolution": "source_config_default"}
            parquet = pq.ParquetFile(path)
            for record_batch in parquet.iter_batches(batch_size=50_000):
                frame = self._normalize(record_batch.to_pandas(), source, units=units)
                months = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m")
                for partition in sorted(set(months.dropna()) & targets):
                    part = frame[months == partition]
                    if part.empty:
                        continue
                    part = part.copy()
                    part["_source"] = (
                        f"effective:{source}" if str(batch_id).startswith("effective:") else source
                    )
                    path = staging_root / f"{partition}.parquet"
                    import pyarrow as pa
                    import pyarrow.parquet as pq
                    table = pa.Table.from_pandas(part, preserve_index=False)
                    writer = writers.get(partition)
                    if writer is None:
                        writer = pq.ParquetWriter(path, table.schema, compression="zstd")
                        writers[partition] = writer
                    writer.write_table(table)
                    if track_source_frames:
                        source_frames_by_month[partition].setdefault(source, []).append(part)
        for writer in writers.values():
            writer.close()
        results = {}
        try:
            import pyarrow.parquet as pq
            for partition in partitions:
                staged = staging_root / f"{partition}.parquet"
                frames = []
                if staged.exists():
                    parquet = pq.ParquetFile(staged)
                    frames = [batch.to_pandas() for batch in parquet.iter_batches(batch_size=50_000)]
                if include_current:
                    current = self.warehouse.read_daily(partition)
                    if current is not None and not current.empty:
                        current_frame = self._normalize_current(current)
                        current_frame["_source"] = "legacy_daily"
                        frames.append(current_frame)
                if not frames:
                    raise ValueError(f"{partition} 没有可用于构建的 Raw Batch")
                source_frames = {
                    source: pd.concat(chunks, ignore_index=True)
                    for source, chunks in source_frames_by_month[partition].items()
                }
                results[partition] = self._finalize_partition(
                    partition, frames, source_frames, batch_ids,
                    include_current=include_current,
                )
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
        return results

    def _finalize_partition(self, partition: str, frames: list[pd.DataFrame],
                            source_frames: dict[str, pd.DataFrame], batch_ids: list[str],
                            *, include_current: bool) -> dict:
        combined = pd.concat(frames, ignore_index=True)
        combined["date"] = pd.to_datetime(combined["date"], errors="coerce")
        priority = {name: item["priority"] for name, item in self._sources.items()}
        priority.update({f"effective:{name}": -1 for name in self._sources})
        priority["legacy_daily"] = 999
        combined["_priority"] = combined["_source"].map(priority).fillna(999)
        combined = combined.sort_values(["date", "code", "_priority"], kind="stable")
        result = combined.drop_duplicates(["date", "code"], keep="first")
        result = result.drop(columns=["_priority", "_source"], errors="ignore")
        result = result.drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)
        result = result[result["date"].dt.strftime("%Y-%m") == partition].reset_index(drop=True)

        canonical = result.to_json(date_format="iso", orient="records")
        content_fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        version_id = f"stock_daily_{partition.replace('-', '')}_{content_fingerprint[:12]}"
        candidate_root = self.warehouse.base_dir / "candidates" / "stock_daily" / partition
        candidate_root.mkdir(parents=True, exist_ok=True)
        path = candidate_root / f"{version_id}.parquet"
        if not path.exists():
            result.to_parquet(path, index=False, engine="pyarrow", compression="zstd")
        return {
            "version_id": version_id, "partition": partition, "path": path,
            "row_count": len(result), "symbol_count": int(result["code"].nunique()),
            "min_date": str(result["date"].min())[:10] if not result.empty else None,
            "max_date": str(result["date"].max())[:10] if not result.empty else None,
            "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_conflicts": self._conflicts(source_frames),
            "source_batches": batch_ids, "input_fingerprint": content_fingerprint,
            "diff_report": self._diff_current(partition, result) if include_current else None,
        }

    def _diff_current(self, partition: str, candidate: pd.DataFrame) -> dict:
        current = self.warehouse.read_daily(partition)
        if current is None or current.empty:
            return {"current_rows": 0, "candidate_rows": len(candidate), "added_keys": len(candidate),
                    "removed_keys": 0, "field_differences": 0}
        left = self._normalize_current(current).set_index(["date", "code"])
        right = candidate.set_index(["date", "code"])
        common = left.index.intersection(right.index)
        fields = [field for field in ("open", "high", "low", "close", "volume", "amount") if field in left and field in right]
        differences = 0
        for field in fields:
            a = pd.to_numeric(left.loc[common, field], errors="coerce")
            b = pd.to_numeric(right.loc[common, field], errors="coerce")
            differences += int((a.sub(b).abs().fillna(0) > 1e-9).sum())
        return {"current_rows": len(left), "candidate_rows": len(right),
                "added_keys": len(right.index.difference(left.index)),
                "removed_keys": len(left.index.difference(right.index)),
                "field_differences": differences, "common_keys": len(common), "fields": fields}

    @staticmethod
    def _conflicts(source_frames: dict[str, pd.DataFrame]) -> list[dict]:
        names = list(source_frames)
        conflicts = []
        for index, left_name in enumerate(names):
            for right_name in names[index + 1:]:
                left = source_frames[left_name]
                right = source_frames[right_name]
                fields = [field for field in ("open", "high", "low", "close", "volume", "amount") if field in left and field in right]
                if not fields:
                    continue
                joined = left.set_index(["date", "code"]).join(
                    right.set_index(["date", "code"]), lsuffix="_left", rsuffix="_right", how="inner"
                )
                for key, row in joined.iterrows():
                    for field in fields:
                        a, b = row.get(f"{field}_left"), row.get(f"{field}_right")
                        if pd.notna(a) and pd.notna(b) and float(a) != float(b):
                            conflicts.append({"left_source": left_name, "right_source": right_name,
                                              "date": str(key[0])[:10], "code": key[1], "field": field})
        return conflicts
