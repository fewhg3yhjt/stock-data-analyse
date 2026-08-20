"""策略方案数据模型 — YAML 配置 → SchemeConfig 对象

策略方案将原本硬编码在代码里的买点/卖点/风控参数全部外置为 YAML 文件。
代码只提供原子执行能力，配置决定策略行为。

YAML 结构:
    name, version, description, applicable_types
    buy_rules:  list[BuyRuleConfig]   买点规则（可叠加多个）
    sell_rules: list[SellRuleConfig]  卖点规则（可叠加多个）
    risk:       RiskConfig            风控参数
    backtest:   BacktestConfig        回测参数
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

@dataclass
class BuyRuleConfig:
    """买点规则配置

    type 决定执行类，params 决定行为细节。例如:
      type="support_level"
        params:
          support_sources: ["dividend_anchor", "ma_60", "low_3m", "year_low"]
          sort_direction: "asc"
          buy_stages: [{label, position_index, ratio}, ...]
    """
    type: str
    params: dict = field(default_factory=dict)


@dataclass
class SellRuleConfig:
    """卖点规则配置

    type 决定执行类，params 决定行为细节。例如:
      type="left_side_fixed"
        params:
          reference_price: "year_high"
          zones: [{name, range, sell_ratio}, ...]
          half_profit_threshold: 0.10
    """
    type: str
    params: dict = field(default_factory=dict)


@dataclass
class RiskConfig:
    """风控参数

    stop_loss_by_type 用"扣减率"表示: 止损价 = 均价 × (1 - 扣减率)
      {A: 0.15, B: 0.15, C: 0.15, D: 0.10}
    """
    stop_loss_by_type: dict = field(default_factory=dict)  # {A: 0.15, ...}
    volume_surge_threshold: float = 1.8
    technical_stop_enabled: bool = True
    drawdown_stop: float = 0.08
    min_profit_for_dd: float = 0.06


@dataclass
class GridSearchConfig:
    """网格搜索参数"""
    trail_thresholds: list[float] = field(
        default_factory=lambda: [0.03, 0.04, 0.05, 0.06, 0.08, 0.10]
    )
    buy_offsets: list[float] = field(
        default_factory=lambda: [-0.02, 0.0, 0.02]
    )


@dataclass
class BacktestConfig:
    """回测参数"""
    initial_cash: float = 100_000
    optimize: bool = True
    grid_search: Optional[GridSearchConfig] = None


@dataclass
class SchemeConfig:
    """完整策略方案 — 从 YAML 加载后的运行时对象

    提供了 from_yaml 和 to_dict 两个方向，便于：
      - 加载: YAML → SchemeConfig
      - 快照: SchemeConfig → dict (JSON 可序列化，供 Phase 3 持仓记录)
    """
    name: str = ""
    version: str = "1.0"
    description: str = ""
    applicable_types: list[str] = field(default_factory=lambda: ["A", "B", "C", "D"])

    # 策略说明书（纯文档段，供人阅读/展示，不参与执行逻辑）
    strategy_spec: dict = field(default_factory=dict)

    buy_rules: list[BuyRuleConfig] = field(default_factory=list)
    sell_rules: list[SellRuleConfig] = field(default_factory=list)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    # ── 工具方法 ──────────────────────────────────────

    def to_dict(self) -> dict:
        """转回 dict（用于快照/JSON 序列化）"""
        return asdict(self)

    def find_buy_rule(self, rule_type: str) -> Optional[BuyRuleConfig]:
        """按 type 查找买点规则"""
        for r in self.buy_rules:
            if r.type == rule_type:
                return r
        return None

    def find_sell_rule(self, rule_type: str) -> Optional[SellRuleConfig]:
        """按 type 查找卖点规则"""
        for r in self.sell_rules:
            if r.type == rule_type:
                return r
        return None


# ═══════════════════════════════════════════════════════════════
# YAML 加载与校验
# ═══════════════════════════════════════════════════════════════

def load_scheme_from_dict(data: dict) -> SchemeConfig:
    """从 dict 构建 SchemeConfig，带缺省值填充与基础校验

    Raises:
        ValueError: 必填字段缺失或结构错误
    """
    if not isinstance(data, dict):
        raise ValueError("方案配置必须是 YAML 映射（顶层为键值对）")

    name = data.get("name") or data.get("id")
    if not name:
        raise ValueError("方案缺少必填字段: name")

    # buy_rules
    buy_rules = []
    for item in data.get("buy_rules", []):
        if not isinstance(item, dict) or not item.get("type"):
            raise ValueError(f"方案 '{name}' 的 buy_rules 项缺少 type")
        buy_rules.append(BuyRuleConfig(
            type=str(item["type"]),
            params=item.get("params", {}) or {},
        ))

    # sell_rules
    sell_rules = []
    for item in data.get("sell_rules", []):
        if not isinstance(item, dict) or not item.get("type"):
            raise ValueError(f"方案 '{name}' 的 sell_rules 项缺少 type")
        sell_rules.append(SellRuleConfig(
            type=str(item["type"]),
            params=item.get("params", {}) or {},
        ))

    # risk
    risk_data = data.get("risk", {}) or {}
    risk = RiskConfig(
        stop_loss_by_type=risk_data.get("stop_loss_by_type", {}),
        volume_surge_threshold=float(risk_data.get("volume_surge_threshold", 1.8)),
        technical_stop_enabled=bool(risk_data.get("technical_stop_enabled", True)),
        drawdown_stop=float(risk_data.get("drawdown_stop", 0.08)),
        min_profit_for_dd=float(risk_data.get("min_profit_for_dd", 0.06)),
    )

    # backtest
    bt_data = data.get("backtest", {}) or {}
    gs_data = bt_data.get("grid_search") or {}
    grid_search = None
    if gs_data:
        grid_search = GridSearchConfig(
            trail_thresholds=[float(x) for x in gs_data.get("trail_thresholds", [])],
            buy_offsets=[float(x) for x in gs_data.get("buy_offsets", [])],
        )
    backtest = BacktestConfig(
        initial_cash=float(bt_data.get("initial_cash", 100_000)),
        optimize=bool(bt_data.get("optimize", True)),
        grid_search=grid_search,
    )

    return SchemeConfig(
        name=name,
        version=str(data.get("version", "1.0")),
        description=data.get("description", ""),
        applicable_types=list(data.get("applicable_types", ["A", "B", "C", "D"])),
        strategy_spec=data.get("strategy_spec") or {},
        buy_rules=buy_rules,
        sell_rules=sell_rules,
        risk=risk,
        backtest=backtest,
    )


def load_scheme_from_yaml(path: Path | str) -> SchemeConfig:
    """从 YAML 文件加载方案配置"""
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return load_scheme_from_dict(data)
