"""YAML-driven security type profiles and applicability rules."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from StockInvestmentTool.datasource.fetcher import StockDataFetcher


PROFILE_DIR = Path(__file__).resolve().parents[1] / "config" / "asset_profiles"
APPLICABILITY = {"required", "optional", "not_applicable"}


class AssetProfileError(ValueError):
    pass


def load_asset_profile(asset_type: str) -> dict:
    path = PROFILE_DIR / f"{asset_type}.yaml"
    if not path.exists():
        raise AssetProfileError(f"证券类型配置不存在: {asset_type}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profile = data.get("asset_type") or {}
    if profile.get("key") != asset_type:
        raise AssetProfileError(f"证券类型配置 key 不匹配: {path}")
    for section in ("datasets", "metrics"):
        for key, state in (data.get(section) or {}).items():
            if state not in APPLICABILITY:
                raise AssetProfileError(f"{path}: {section}.{key} 状态无效: {state}")
    return data


def asset_type_for(code: str, known_types: dict[str, str] | None = None) -> str:
    normalized = str(code or "").strip().lower().replace(".", "")
    if known_types and normalized in known_types:
        return known_types[normalized]
    return StockDataFetcher.detect_type(normalized)


def select_symbols(symbols: Iterable[str], *, asset_types: Iterable[str] | None = None,
                   known_types: dict[str, str] | None = None) -> tuple[list[str], dict[str, int]]:
    allowed = set(asset_types or ("stock", "etf", "index"))
    unknown = allowed - {"stock", "etf", "index"}
    if unknown:
        raise AssetProfileError(f"未知证券类型: {sorted(unknown)}")
    selected = []
    counts = {key: 0 for key in allowed}
    for code in symbols:
        kind = asset_type_for(code, known_types)
        if kind in allowed:
            selected.append(str(code))
            counts[kind] += 1
    return selected, counts


def applicability(asset_type: str, section: str, key: str) -> str:
    if section not in {"datasets", "metrics"}:
        raise AssetProfileError(f"未知适用性配置段: {section}")
    return (load_asset_profile(asset_type).get(section) or {}).get(key, "optional")
