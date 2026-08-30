"""Prompt 模板填充 — 将分析数据填入四维一体决策框架模板"""

import logging
import re
from pathlib import Path
from typing import Optional

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_STOCK = "股票_分析prompt_V4.txt"
TEMPLATE_FUND = "基金_分析prompt_V4.txt"


class PromptBuilder:
    """从数据填充分析 Prompt 模板（逐行智能替换）"""

    def __init__(self, template_dir: Optional[Path] = None):
        self.template_dir = Path(template_dir) if template_dir else TEMPLATE_DIR
        self._ensure_templates()

    def _ensure_templates(self):
        self.template_dir.mkdir(parents=True, exist_ok=True)
        src_templates = Config.BASE_DIR / "prompt_templates"
        if src_templates.exists():
            for f in src_templates.iterdir():
                if f.suffix == ".txt":
                    dest = self.template_dir / f.name
                    if not dest.exists():
                        dest.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
                        logger.info("已复制模板: %s", f.name)

    def _load_template(self, template_type: str = "stock") -> str:
        filename = TEMPLATE_STOCK if template_type == "stock" else TEMPLATE_FUND
        path = self.template_dir / filename
        if not path.exists():
            logger.warning("模板文件不存在: %s", path)
            return self._fallback_template(template_type)
        return path.read_text(encoding="utf-8")

    def _fallback_template(self, template_type: str) -> str:
        if template_type == "stock":
            return "{stock_name} ({stock_code}) 个股分析\n当前价: {current_price}\nMA20: {ma20} | MA60: {ma60}\n趋势: {trend}\nPE(TTM): {pe_ttm}\n强支撑: {strong_support} | 弱支撑: {weak_support}"
        return "基金分析模板"

    # ── 核心：逐行填充 ──────────────────────────────

    @staticmethod
    def _replace_inline(line: str, value: str) -> str:
        """替换行内第一个占位符（— 或 ___）"""
        # 跳过已经替换过的行（已有数值的行）
        if re.search(r'[│|]\s*\d+\.?\d*\s*[│|]', line):
            return line

        # 替换 ___ 占位符（优先）
        if '___' in line:
            line = line.replace('___', str(value), 1)
        # 替换 — 占位符（只在无其他数值的单元格中替换）
        elif '—' in line:
            line = re.sub(r'(?<![✅⚠️❌⏳])—(?!—)', str(value), line, count=1)
        return line

    def build_stock_prompt(
        self,
        stock_name: str,
        stock_code: str,
        technical: dict,
        valuation: dict,
        cross_support: dict,
        strategy_plan: list[dict],
        pe_info: dict,
    ) -> str:
        """填充个股分析 Prompt"""
        template = self._load_template("stock")
        template = template.replace("长江电力", stock_name)

        # ── 构建标签→值映射（标签按长度降序，避免短标优先匹配） ──
        field_values: dict[str, str] = {}

        # 技术面
        if technical:
            for key, label in [
                ("current_price", "当前价"),
                ("ma20", "MA20"),
                ("ma60", "MA60"),
                ("ma120", "MA120"),
                ("recent_low", "近期低点"),
                ("year_high", "近12个月最高点"),
                ("year_low", "近12个月最低点"),
            ]:
                v = technical.get(key)
                if v is not None:
                    field_values[label] = f"{v:.2f}" if isinstance(v, float) else str(v)

            # 均线排列
            trend = technical.get("trend", "")
            if trend:
                field_values["均线排列"] = trend

        # PE 数据
        if pe_info:
            pe = pe_info.get("current_pe")
            if pe is not None:
                field_values["PE（TTM）"] = str(pe)
            pct = pe_info.get("pe_percentile")
            if pct is not None:
                field_values["PE历史分位（5年）"] = f"{pct}%"

        # 三重锚估值
        anchor = valuation.get("triple_anchor", {})
        if anchor and anchor.get("div_per_share"):
            dp = anchor.get("div_per_share")
            if dp:
                field_values["最近一年每股分红（TTM）"] = f"{dp}"  # 模板已有"元"
            yld = anchor.get("avg_5y_yield")
            if yld:
                field_values["股息率（TTM）"] = f"{yld}%"
                field_values["近5年平均股息率"] = f"{yld}%"
            for key, label in [
                ("anchor_price_1", "锚定价①（历史均值）"),
                ("anchor_price_2", "锚定价②（安全边际线3.4%）"),
                ("anchor_price_3", "锚定价③（极端低估线4.0%）"),
            ]:
                v = anchor.get(key)
                if v and v > 0:
                    field_values[label] = f"{v:.2f}"

        # 交叉支撑
        if cross_support:
            ss = cross_support.get("strong_support")
            if ss:
                field_values["综合强支撑"] = f"{ss:.2f}"
                field_values["股息率极端低估锚（4.0%）"] = f"{ss:.2f}"
            ws = cross_support.get("weak_support")
            if ws:
                field_values["综合弱支撑"] = f"{ws:.2f}"
            # 历史回调底线 = 近12月低点
            yl = technical.get("year_low")
            if yl:
                field_values["历史回调底线"] = f"{yl:.2f}"

        # 趋势判定
        if trend:
            field_values["趋势判定"] = trend

        # 按标签长度降序排列，但"均线排列"列为首位（避免被 MA20/MA60 短标签抢匹配）
        sorted_labels = sorted(field_values.keys(), key=lambda x: (x != "均线排列", len(x)), reverse=True)

        # ── 预处理1: 替换均线排列行（必须在主循环前，避免被 MA20/MA60 短标签拦截） ──
        trend_val = field_values.get("均线排列")
        if trend_val:
            template = re.sub(
                r'均线排列[：:]\s*MA5_{2,}.*$',
                f'均线排列： {trend_val}',
                template,
                flags=re.MULTILINE,
            )

        # ── 预处理2: 替换趋势判定行 ──
        trend_judge = field_values.get("趋势判定")
        if trend_judge:
            template = re.sub(
                r'趋势判定[：:]\s*多头\s*/\s*空头\s*/\s*震荡',
                f'趋势判定： {trend_judge}',
                template,
            )

        # ── 逐行处理 ──
        lines = template.split("\n")
        for i, line in enumerate(lines):
            for label in sorted_labels:
                if label in line and label not in ("均线排列", "趋势判定"):
                    if "___" in line or "—" in line:
                        lines[i] = self._replace_inline(line, field_values[label])
                    break

        template = "\n".join(lines)

        logger.info("Prompt 填充完成 (%d 个字段已替换)", len(field_values))
        return template

    def build_fund_prompt(
        self,
        fund_name: str,
        fund_code: str,
        technical: Optional[dict] = None,
        cross_support: Optional[dict] = None,
        **kwargs,
    ) -> str:
        """填充基金分析 Prompt（价格/技术面可填，基金特有字段留空）"""
        template = self._load_template("fund")
        template = template.replace("沪深300ETF华泰柏瑞", fund_name)

        # 填充技术面数据（与个股共享逻辑）
        tech = technical or {}
        cs = cross_support or {}
        replacements = {
            "当前净值/价格": str(tech.get("current_price", "N/A")),
            "MA60": str(tech.get("ma60", "N/A")),
            "MA20": str(tech.get("ma20", "N/A")),
            "近期低点": str(tech.get("recent_low", "N/A")),
            "近12个月最高点": str(tech.get("year_high", "N/A")),
            "近12个月最低点": str(tech.get("year_low", "N/A")),
            "综合强支撑": str(cs.get("strong_support", "N/A")),
            "综合弱支撑": str(cs.get("weak_support", "N/A")),
        }
        for label, value in replacements.items():
            if value and value != "N/A":
                template = template.replace(f"___{label}___", value)
                template = template.replace("—", value, 1) if label in template[:template.find("—")+1] and label in template else template

        return template

    def save_prompt_file(self, content: str, stock_code: str,
                         suffix: str = "prompt.md") -> str:
        safe_code = stock_code.replace(".", "_").replace("/", "_")
        filepath = Config.REPORT_DIR / f"{safe_code}_{suffix}"
        filepath.write_text(content, encoding="utf-8")
        logger.info("Prompt 已保存: %s", filepath)
        return str(filepath)
