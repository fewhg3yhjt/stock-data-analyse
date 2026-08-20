"""全局配置模块 — 集中管理所有可调参数"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件（从项目根目录向上查找）
_env_loaded = False


def _ensure_env():
    global _env_loaded
    if not _env_loaded:
        # 从当前文件向上找 .env
        start = Path(__file__).resolve().parent.parent
        for p in [start, start.parent, Path.cwd()]:
            env_file = p / ".env"
            if env_file.exists():
                load_dotenv(env_file)
                break
        _env_loaded = True


class Config:
    """所有配置项集中管理，不在此处的请通过环境变量注入"""

    _ensure_env()

    # ── 文件路径 ─────────────────────────────────────
    # BASE_DIR 指向 StockInvestmentTool 包本身；运行产物统一放包内 output/ 下，
    # 避免落到仓库根目录。
    BASE_DIR = Path(__file__).resolve().parent
    OUTPUT_DIR = BASE_DIR / "output"
    DATA_DIR = OUTPUT_DIR / "data"
    REPORT_DIR = OUTPUT_DIR / "reports"
    CHART_DIR = OUTPUT_DIR / "charts"

    # 自动创建目录
    for _d in [OUTPUT_DIR, DATA_DIR, REPORT_DIR, CHART_DIR]:
        _d.mkdir(parents=True, exist_ok=True)

    # ── Baostock 数据源 ──────────────────────────────
    BAOSTOCK_DEFAULT_ADJUST = "2"  # 前复权
    BAOSTOCK_DEFAULT_FREQ = "d"  # 日线
    BAOSTOCK_DEFAULT_FIELDS = "date,open,high,low,close,volume,amount,peTTM,pbMRQ"

    # ── 默认策略方案 ──────────────────────────────────
    DEFAULT_SCHEME = "default_value"

    # ── 策略兜底默认参数（LEGACY）──────────────────────
    # 规范来源: schemes/*.yaml 方案文件。此处仅作为"未指定方案时"的兜底，
    # 以及向后兼容旧调用（MultiBuyStrategy() / TakeProfitOptimizer() 无 scheme 参数）。
    # 修改策略请编辑 schemes/default_value.yaml，勿改这里。
    INITIAL_CASH = 100_000
    BUY_RATIOS = [0.3, 0.4, 0.3]
    SUPPORT_MA = ["ma_20", "ma_60", "ma_120"]
    STOP_LOSS_RATE = 0.15  # 硬止损扣减率（止损价 = 均价 × (1-0.15)）
    DRAWDOWN_STOP = 0.08
    MIN_PROFIT_FOR_DD = 0.06

    # 买入阈值偏移量搜索范围（默认方案 grid_search 的兜底）
    MA_OFFSETS = [-0.02, 0.0, 0.02]

    # ── DeepSeek LLM ─────────────────────────────────
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    DEEPSEEK_TIMEOUT = 120

    # ── 输出选项 ─────────────────────────────────────
    REPORT_ENCODING = "utf-8"
    CHART_DPI = 150
    CHART_FIGSIZE = (14, 8)
