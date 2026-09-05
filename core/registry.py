"""方案注册中心 — 发现、加载、缓存、查询策略方案

扫描 schemes/ 目录（内置 + 用户自定义）中的 YAML 文件，
加载为 SchemeConfig 并提供查询接口。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from StockInvestmentTool.core.scheme import (
    SchemeConfig,
    load_scheme_from_yaml,
)

logger = logging.getLogger(__name__)


class SchemeNotFoundError(KeyError):
    """方案不存在"""


class SchemeValidationError(ValueError):
    """方案校验失败"""


@dataclass
class SchemeSummary:
    """方案摘要（用于列表展示）"""
    name: str
    version: str
    description: str
    applicable_types: list[str]
    source: str  # 来源文件路径


class SchemeRegistry:
    """方案注册中心

    Args:
        schemes_dir: 方案目录，默认项目根目录下的 schemes/
        custom_dir:   用户自定义方案目录（可选，默认 schemes/custom）
    """

    def __init__(
        self,
        schemes_dir: Optional[Path] = None,
        custom_dir: Optional[Path] = None,
    ):
        self._schemes_dir = Path(schemes_dir) if schemes_dir else self._default_schemes_dir()
        self._custom_dir = Path(custom_dir) if custom_dir else self._schemes_dir / "custom"
        self._custom_dir.mkdir(parents=True, exist_ok=True)
        self._schemes: dict[str, SchemeConfig] = {}
        self._sources: dict[str, Path] = {}
        self._load_all()

    # ── 目录定位 ──────────────────────────────────────

    @staticmethod
    def _default_schemes_dir() -> Path:
        """定位项目根目录的 schemes/（兼容从子目录运行）"""
        here = Path(__file__).resolve().parent  # core/
        for p in [here, here.parent, here.parent.parent]:
            candidate = p / "schemes"
            if candidate.is_dir():
                return candidate
        # 兜底：创建默认位置
        default = here.parent / "schemes"
        default.mkdir(parents=True, exist_ok=True)
        return default

    # ── 加载 ──────────────────────────────────────────

    def _load_all(self):
        """扫描目录加载全部方案"""
        self._schemes.clear()
        self._sources.clear()

        # 内置 + 用户自定义（自定义优先级更高，同名覆盖）
        scan_dirs = [self._schemes_dir, self._custom_dir]
        seen: dict[str, Path] = {}
        for d in scan_dirs:
            if not d.exists():
                continue
            for yaml_file in sorted(d.glob("*.yaml")):
                # 跳过 _开头的草稿文件 + 非策略方案文件（如 indicators.yaml）
                if yaml_file.stem.startswith("_") or yaml_file.stem == "indicators":
                    continue
                seen[yaml_file.stem] = yaml_file

        for name, path in seen.items():
            try:
                scheme = load_scheme_from_yaml(path)
                self._schemes[scheme.name] = scheme
                self._sources[scheme.name] = path
                logger.debug("加载方案: %s (%s)", scheme.name, path)
            except Exception as e:
                logger.error("加载方案失败 %s: %s", path, e)

    def reload(self):
        """重新扫描目录（配置热更新）"""
        self._load_all()

    # ── 启停 / 默认（FR-2.4，配合 scheme_store 状态）─────────

    def enabled(self) -> list[SchemeConfig]:
        """返回启用的方案（过滤停用）。"""
        from StockInvestmentTool.core import scheme_store
        return [s for s in self._schemes.values() if scheme_store.is_enabled(s.name)]

    # ── 查询 ──────────────────────────────────────────

    def list(self) -> list[SchemeSummary]:
        """列出所有方案摘要（含启停/默认状态）。"""
        from StockInvestmentTool.core import scheme_store
        return [
            SchemeSummary(
                name=s.name,
                version=s.version,
                description=s.description,
                applicable_types=list(s.applicable_types),
                source=str(self._sources.get(s.name, "")),
            )
            for s in self._schemes.values()
            if scheme_store.is_enabled(s.name)
        ]

    def list_all(self) -> list[SchemeSummary]:
        """列出全部已加载方案，包含已停用方案。"""
        return [
            SchemeSummary(
                name=s.name,
                version=s.version,
                description=s.description,
                applicable_types=list(s.applicable_types),
                source=str(self._sources.get(s.name, "")),
            )
            for s in self._schemes.values()
        ]

    def get(self, name: str) -> SchemeConfig:
        """按名称获取方案配置

        Raises:
            SchemeNotFoundError: 方案不存在
        """
        scheme = self._schemes.get(name)
        if scheme is None:
            raise SchemeNotFoundError(
                f"方案 '{name}' 不存在。可用方案: {', '.join(self._schemes) or '(无)'}"
            )
        return scheme

    def has(self, name: str) -> bool:
        return name in self._schemes

    def get_default(self, stock_type: str = "B") -> SchemeConfig:
        """获取某股票类型对应的默认方案

        优先匹配名称含 stock_type 的方案，其次回退用户标记的默认方案，
        否则返回 default_value。
        """
        from StockInvestmentTool.core import scheme_store
        # 用户显式标记的默认
        for s in self._schemes.values():
            if scheme_store.is_default(s.name) and scheme_store.is_enabled(s.name):
                return s
        for s in self._schemes.values():
            if s.name == f"default_{stock_type.lower()}":
                return s
        return self.get("default_value")

    # ── 自定义方案注册 ─────────────────────────────────

    def register_custom(self, yaml_path: Path | str) -> SchemeConfig:
        """注册一个自定义方案文件

        将文件复制到 custom/ 目录后加载。复制而非引用，保证 custom/ 目录
        自包含、可整体迁移。
        """
        src = Path(yaml_path)
        if not src.exists():
            raise FileNotFoundError(f"方案文件不存在: {src}")

        # 先试加载验证
        try:
            scheme = load_scheme_from_yaml(src)
        except Exception as e:
            raise SchemeValidationError(f"方案文件无效: {e}") from e

        dest = self._custom_dir / f"{scheme.name}.yaml"
        # 内容相同则跳过写入，避免无谓的 mtime 变化
        if not dest.exists() or dest.read_text(encoding="utf-8") != src.read_text(encoding="utf-8"):
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        self._schemes[scheme.name] = scheme
        self._sources[scheme.name] = dest
        logger.info("注册自定义方案: %s (%s)", scheme.name, dest)
        return scheme
