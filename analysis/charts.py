"""图表生成 — K 线图 + 回测结果图"""

import logging
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # 非交互式后端，服务器环境兼容

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)

_MPF_STYLE = "charles"


def setup_cjk_font() -> Optional[str]:
    """配置 matplotlib 中文字体（幂等，可被其它图表模块复用）

    Returns:
        使用的中文字体名；未找到则返回 None
    """
    global _MPF_STYLE
    cjk = None
    for fname in ["Microsoft YaHei", "SimHei", "WenQuanYi Micro Hei",
                  "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC"]:
        try:
            fp = fm.findfont(fname, fallback_to_default=False)
            if fp and "DejaVu" not in fp:
                cjk = fname
                break
        except Exception:
            continue

    # 兜底：从项目 fonts/ 目录注册中文字体（代码挂载进容器，容器无系统字体时可用）
    if cjk is None:
        from StockInvestmentTool.config import Config
        font_dir = Config.BASE_DIR / "fonts"
        if font_dir.exists():
            for font_file in font_dir.glob("*.[tT][tT][cCfF]"):
                try:
                    fm.fontManager.addfont(str(font_file))
                    logger.info("已注册项目字体: %s", font_file.name)
                except Exception as e:
                    logger.debug("字体注册失败 %s: %s", font_file, e)
            # 注册后再找一次
            for fname in ["Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK"]:
                try:
                    fp = fm.findfont(fname, fallback_to_default=False)
                    if fp and "DejaVu" not in fp:
                        cjk = fname
                        break
                except Exception:
                    continue

    if cjk:
        plt.rcParams["font.sans-serif"] = [cjk, "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        _MPF_STYLE = mpf.make_mpf_style(
            base_mpf_style="charles",
            rc={"font.family": cjk, "axes.unicode_minus": False},
        )
        logger.info("使用中文字体: %s", cjk)
    else:
        logger.warning("未找到中文字体，图表中文可能显示为方框")
    return cjk


# 模块导入时立即配置字体
setup_cjk_font()


class ChartGenerator:
    """生成分析图表并保存为图片文件"""

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = Path(output_dir) if output_dir else Config.CHART_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _safe_name(self, stock_code: str, base: str) -> str:
        """生成包含股票代码的文件名，防止多股票冲突"""
        code = stock_code.replace(".", "_").replace("/", "_")
        return f"{code}_{base}"

    def plot_kline(self, df: pd.DataFrame, stock_name: str,
                   stock_code: str = "stock",
                   filename: str = "kline.png") -> str:
        """绘制日 K 线图（含均线 + 成交量）"""
        filename = self._safe_name(stock_code, Path(filename).stem) + ".png"
        df_plot = df.rename(columns={
            "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        })
        df_plot = df_plot.set_index("date")
        df_plot.index = pd.to_datetime(df_plot.index)

        avail_mav = [w for w in [5, 10, 20, 60] if f"ma_{w}" in df.columns]
        save_path = str(self.output_dir / filename)

        mpf.plot(
            df_plot,
            type="candle",
            mav=tuple(avail_mav) if avail_mav else None,
            volume=True,
            title=f"{stock_name} 日K线图",
            ylabel="价格 (元)",
            ylabel_lower="成交量 (手)",
            # K线图含成交量副图，用更大尺寸+高DPI让主图区够大
            figsize=(Config.CHART_FIGSIZE[0] + 2, Config.CHART_FIGSIZE[1] + 2),
            style=_MPF_STYLE,
            savefig=dict(fname=save_path, dpi=Config.CHART_DPI),
        )
        logger.info("K 线图已保存: %s", save_path)
        return save_path

    def plot_backtest(self, df: pd.DataFrame, trades: list[dict],
                      equity_curve: list[dict], stock_name: str,
                      stock_code: str = "stock",
                      trail_threshold: float = 0.05,
                      filename: str = "backtest.png") -> str:
        """绘制回测结果图"""
        filename = self._safe_name(stock_code, Path(filename).stem) + ".png"
        df_equity = pd.DataFrame(equity_curve)
        df_equity["date"] = pd.to_datetime(df_equity["date"])

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), sharex=True)

        # ── 上子图：价格 + 均线 + 交易标记 ──
        ax1.plot(df["date"], df["close"], label="收盘价", color="black", linewidth=1)
        if "ma_20" in df.columns:
            ax1.plot(df["date"], df["ma_20"], label="MA20", linestyle="--", alpha=0.5)
        if "ma_60" in df.columns:
            ax1.plot(df["date"], df["ma_60"], label="MA60", linestyle="--", alpha=0.5)
        if "ma_120" in df.columns:
            ax1.plot(df["date"], df["ma_120"], label="MA120", linestyle="--", alpha=0.5)

        buy_trades = [t for t in trades if "买入" in t.get("type", "")]
        sell_trades = [t for t in trades if "止盈" in t.get("type", "")
                       or "止损" in t.get("type", "")
                       or "平仓" in t.get("type", "")
                       or "回撤" in t.get("type", "")]

        if buy_trades:
            buy_dates = pd.to_datetime([t["date"] for t in buy_trades])
            buy_prices = [t["price"] for t in buy_trades]
            ax1.scatter(buy_dates, buy_prices, marker="^", color="green", s=120,
                       label="买入", zorder=5)
        if sell_trades:
            sell_dates = pd.to_datetime([t["date"] for t in sell_trades])
            sell_prices = [t["price"] for t in sell_trades]
            ax1.scatter(sell_dates, sell_prices, marker="v", color="red", s=120,
                       label="卖出", zorder=5)

        ax1.legend(loc="best")
        ax1.grid(True, alpha=0.3)
        ax1.set_title(f"{stock_name} 回测 — 右侧回撤阈值 {int(trail_threshold * 100)}%")
        ax1.set_ylabel("价格 (元)")

        # ── 下子图：权益曲线 ──
        ax2.plot(df_equity["date"], df_equity["total_asset"],
                label="策略资产", color="purple", linewidth=1.5)
        ax2.axhline(y=Config.INITIAL_CASH, color="gray", linestyle="--",
                   alpha=0.7, label="初始本金")

        # 买入持有对比
        hold_asset = Config.INITIAL_CASH * df["close"] / df["close"].iloc[0]
        ax2.plot(df["date"], hold_asset, label="买入持有", color="orange",
                linestyle="-.", alpha=0.7)

        ax2.legend(loc="best")
        ax2.grid(True, alpha=0.3)
        ax2.set_xlabel("日期")
        ax2.set_ylabel("资产 (元)")

        plt.tight_layout()
        save_path = str(self.output_dir / filename)
        plt.savefig(save_path, dpi=Config.CHART_DPI)
        plt.close(fig)
        logger.info("回测图已保存: %s", save_path)
        return save_path

    def plot_performance_summary(self, equity_curve: list[dict],
                                 stock_code: str = "stock",
                                 filename: str = "performance.png") -> str:
        """绘制绩效摘要图（权益 + 回撤）"""
        filename = self._safe_name(stock_code, Path(filename).stem) + ".png"
        df = pd.DataFrame(equity_curve)
        df["date"] = pd.to_datetime(df["date"])
        df["peak"] = df["total_asset"].cummax()
        df["drawdown"] = (df["total_asset"] - df["peak"]) / df["peak"] * 100

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

        ax1.fill_between(df["date"], df["total_asset"], alpha=0.3, color="green")
        ax1.plot(df["date"], df["total_asset"], color="green", linewidth=1)
        ax1.plot(df["date"], df["peak"], color="red", linestyle="--", alpha=0.5)
        ax1.set_ylabel("资产 (元)")
        ax1.grid(True, alpha=0.3)
        ax1.set_title("策略权益曲线")

        ax2.fill_between(df["date"], df["drawdown"], 0, alpha=0.3, color="red")
        ax2.plot(df["date"], df["drawdown"], color="red", linewidth=1)
        ax2.set_ylabel("回撤 (%)")
        ax2.set_xlabel("日期")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        save_path = str(self.output_dir / filename)
        plt.savefig(save_path, dpi=Config.CHART_DPI)
        plt.close(fig)
        logger.info("绩效图已保存: %s", save_path)
        return save_path
