"""V6.0 宏观数据层 — AkShare 宏观数据源（国债/M2/沪深300 PE）

服务 V6.0「宏观刹车片」：
  - 股债收益差 → 总仓位上限（1/PE − 10Y国债收益率）
  - M2 同比 / 利率 → 调节系数
  - 沪深300 PE → 市场温度辅助

数据源（已实测确认字段，2026-08-10）：
  - bond_zh_us_rate        → 中国国债收益率10年（单位 %）
  - macro_china_money_supply → 货币和准货币(M2)-同比增长（单位 %，升序）
  - stock_index_pe_lg      → 滚动市盈率（沪深300 PE-TTM）

缓存策略：低频数据存 JSON（日/月粒度），拉取失败降级用缓存快照。
同一数据只有一条写入路径（fetch 写入），消费端只读，保证一致性。
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)


@dataclass
class MacroSnapshot:
    """V6.0 宏观快照（全部为百分数绝对值，None 表示未取到）"""
    bond_yield_10y: Optional[float]   # 中国 10Y 国债收益率 %
    m2_growth: Optional[float]        # M2 同比 %
    csi300_pe: Optional[float]        # 沪深300 滚动 PE（TTM）
    updated_at: Optional[str]         # 快照日期 YYYY-MM-DD

    @property
    def erp(self) -> Optional[float]:
        """股债收益差 = 1/PE − 10Y国债收益率（小数点形式）

        V6.0 刹车片输入：erp 高 → 股票有吸引力 → 总仓位上限高。
        任一缺失返回 None。
        """
        if not self.csi300_pe or not self.bond_yield_10y or self.csi300_pe <= 0:
            return None
        return round(1.0 / self.csi300_pe - self.bond_yield_10y / 100.0, 6)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["erp"] = self.erp
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MacroSnapshot":
        """从 dict 重建（容错缺失字段）"""
        return cls(**{k: d.get(k) for k in
                      ("bond_yield_10y", "m2_growth", "csi300_pe", "updated_at")})


class MacroFetcher:
    """V6.0 宏观数据获取器（AkShare，缓存优先 + 降级）"""

    _CACHE_FILE = "macro_snapshot.json"

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else Config.DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = self.data_dir / self._CACHE_FILE

    # ── 缓存 ─────────────────────────────────────────

    def _load_cache(self) -> Optional[MacroSnapshot]:
        if not self._cache_path.exists():
            return None
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                d = json.load(f)
            return MacroSnapshot(**{k: d.get(k) for k in
                                    ("bond_yield_10y", "m2_growth", "csi300_pe", "updated_at")})
        except Exception:
            logger.debug("宏观缓存读取失败: %s", self._cache_path, exc_info=True)
            return None

    def _save_cache(self, snap: MacroSnapshot):
        try:
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(snap.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning("宏观缓存写入失败: %s", self._cache_path)

    # ── 单一指标抓取 ────────────────────────────────

    def _fetch_bond_yield(self) -> Optional[float]:
        """中国 10Y 国债收益率（%）"""
        try:
            import akshare as ak
            df = ak.bond_zh_us_rate(start_date="20250101")
            df = df.dropna(subset=["中国国债收益率10年"])
            return float(df.iloc[-1]["中国国债收益率10年"])
        except Exception as e:
            logger.warning("10Y 国债收益率拉取失败: %s", e)
            return None

    def _fetch_m2_growth(self) -> Optional[float]:
        """M2 同比（%）— 接口返回【倒序】（最新在前），取第一行"""
        try:
            import akshare as ak
            df = ak.macro_china_money_supply()
            df = df.dropna(subset=["货币和准货币(M2)-同比增长"])
            return float(df.iloc[0]["货币和准货币(M2)-同比增长"])
        except Exception as e:
            logger.warning("M2 拉取失败: %s", e)
            return None

    def _fetch_csi300_pe(self) -> Optional[float]:
        """沪深300 滚动市盈率 PE-TTM"""
        try:
            import akshare as ak
            df = ak.stock_index_pe_lg(symbol="沪深300")
            df = df.dropna(subset=["滚动市盈率"])
            return float(df.iloc[-1]["滚动市盈率"])
        except Exception as e:
            logger.warning("沪深300 PE 拉取失败: %s", e)
            return None

    # ── 主入口 ──────────────────────────────────────

    def get_snapshot(self, use_cache: bool = True) -> MacroSnapshot:
        """获取宏观快照（缓存优先，拉取失败降级缓存）。

        拉取成功后用最新值覆盖写缓存；全部失败且有缓存则返回缓存快照。
        全部失败且无缓存 → 各字段 None 的空快照（不抛错，不阻塞分析）。
        """
        cached = self._load_cache() if use_cache else None

        bond = self._fetch_bond_yield()
        m2 = self._fetch_m2_growth()
        pe = self._fetch_csi300_pe()

        if bond is None and m2 is None and pe is None and cached is not None:
            logger.warning("宏观拉取全部失败，降级使用缓存快照: %s", self._cache_path)
            return cached

        snap = MacroSnapshot(
            bond_yield_10y=bond,
            m2_growth=m2,
            csi300_pe=pe,
            updated_at=datetime.now().strftime("%Y-%m-%d"),
        )
        # 部分失败时用缓存补缺
        if cached is not None:
            snap.bond_yield_10y = snap.bond_yield_10y if snap.bond_yield_10y is not None else cached.bond_yield_10y
            snap.m2_growth = snap.m2_growth if snap.m2_growth is not None else cached.m2_growth
            snap.csi300_pe = snap.csi300_pe if snap.csi300_pe is not None else cached.csi300_pe

        self._save_cache(snap)
        return snap
