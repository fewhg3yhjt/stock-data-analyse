"""统一规则注册中心（FR-1.1）

设计意图（HLD §3.1 / ADR-2 / ADR-3 / ADR-4）：
  - 把 v4.5 与 V6.0 两代规则 type 全部纳入统一 `RuleRegistry`；
  - 「新增规则」= 注册一个 `RuleExecutor`，不修改既有策略执行逻辑；
  - 每个 executor 自带 `schema`（参数元数据），前端据此动态渲染表单；
  - 派发统一走 `registry.get(kind, type)`，消除 `find_buy_rule("support_level")`
    这类写死 type 名的散落判断。

扩展点：
  - `RuleRegistry.register(RuleExecutor)` —— 新增规则零侵入；
  - `RuleRegistry.schema(kind, type)` —— 前端动态表单数据源（FR-2.2）。

设计取舍：
  - 存量 v4.5 引擎（MultiBuyStrategy / TakeProfitOptimizer）与 V6.0 引擎
    （BacktestEngineV6）的**算法语义本 SRD 明确不改**（SRD §8-1），
    因此这里注册的 executor 是「适配器」：按 type 把 RuleContext + params
    转成原有函数/类所需的调用形式，保证回归通过。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from StockInvestmentTool.strategy.context import RuleContext

logger = logging.getLogger(__name__)

# executor kind 常量
KIND_BUY = "buy"
KIND_SELL = "sell"


@dataclass
class ParamField:
    """规则参数元数据（供前端动态渲染表单）。

    type:
      - number: 数字框
      - select: 下拉（options 提供可选项）
      - map:    键值表（如 stop_loss_by_type）
      - list:   可增删表格
      - text:   文本框（如指标表达式）
      - map_list: 结构化对象数组（如 support_sources 里的买点批次）
    """

    key: str
    label: str
    type: str = "number"
    default: Any = None
    options: list = field(default_factory=list)
    required: bool = False
    min: Optional[float] = None
    max: Optional[float] = None
    help: str = ""


@dataclass
class RuleExecutor:
    """一条规则的注册元数据 + 执行函数。

    kind: "buy" | "sell"
    type: 规则 type（如 support_level / hard_stop）
    fn:   callable(ctx: RuleContext, params: dict) -> RuleResult
    schema: 参数元数据列表
    description: 规则说明
    """

    kind: str
    type: str
    fn: Callable
    schema: list = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "type": self.type,
            "description": self.description,
            "schema": [
                {
                    "key": f.key,
                    "label": f.label,
                    "type": f.type,
                    "default": f.default,
                    "options": f.options,
                    "required": f.required,
                    "min": f.min,
                    "max": f.max,
                    "help": f.help,
                }
                for f in self.schema
            ],
        }


class RuleRegistry:
    """统一规则注册中心（type → executor 映射）。"""

    def __init__(self):
        self._executors: dict[tuple[str, str], RuleExecutor] = {}

    # ── 注册 ────────────────────────────────────────────

    def register(self, executor: RuleExecutor) -> None:
        if executor.kind not in (KIND_BUY, KIND_SELL):
            raise ValueError(f"非法规则 kind: {executor.kind}")
        self._executors[(executor.kind, executor.type)] = executor

    def register_many(self, executors: list[RuleExecutor]) -> None:
        for e in executors:
            self.register(e)

    # ── 查询 ────────────────────────────────────────────

    def get(self, kind: str, type: str) -> RuleExecutor:
        key = (kind, type)
        if key not in self._executors:
            raise KeyError(f"未注册规则: {kind}/{type}")
        return self._executors[key]

    def has(self, kind: str, type: str) -> bool:
        return (kind, type) in self._executors

    def types(self, kind: str) -> list[str]:
        return sorted(t for (k, t) in self._executors if k == kind)

    def schema(self, kind: str, type: str) -> list[dict]:
        return self.get(kind, type).to_dict()["schema"]

    def describe(self) -> list[dict]:
        """全部规则元数据（供 FR-2 引擎列出可配置规则）。"""
        return [e.to_dict() for e in self._executors.values()]

    # ── 派发 ────────────────────────────────────────────

    def dispatch(self, kind: str, type: str, ctx: RuleContext,
                 params: dict) -> Any:
        """按 type 派发执行，返回 RuleResult（或 executor 自定义返回）。"""
        executor = self.get(kind, type)
        return executor.fn(ctx, params or {})


# ═══════════════════════════════════════════════════════════════
# 全局单例（懒构建），供各处 `from ...rule_registry import rule_registry`
# ═══════════════════════════════════════════════════════════════

_registry: Optional[RuleRegistry] = None


def get_rule_registry() -> RuleRegistry:
    """获取全局 RuleRegistry（懒初始化，避免循环 import）。"""
    global _registry
    if _registry is None:
        _registry = RuleRegistry()
        from StockInvestmentTool.strategy.rule_builtin import build_builtin_executors
        _registry.register_many(build_builtin_executors())
    return _registry


def make_rule_registry() -> RuleRegistry:
    """构建一个新的 RuleRegistry（含内置规则），供单测隔离使用。"""
    reg = RuleRegistry()
    from StockInvestmentTool.strategy.rule_builtin import build_builtin_executors
    reg.register_many(build_builtin_executors())
    return reg


def dispatch_rule(kind: str, type: str, ctx: RuleContext, params: dict) -> Any:
    """便捷派发入口（用全局注册表）。"""
    return get_rule_registry().dispatch(kind, type, ctx, params)
