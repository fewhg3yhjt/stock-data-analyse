# -*- coding: utf-8 -*-
"""数据源适配器 — 股票池/实时快照/增强字段/同花顺行业汇总

统一列约定:
    - 快照(股票池): code(带 sh/sz/bj 前缀) / name / price / change_pct /
                     volume(股) / amount(元)
    - 增强(腾讯/东财): code / pe_ttm / pb / total_mcap(亿) / float_mcap(亿) /
                        turnover(%) / vol_ratio / limit_up / limit_down

源可靠性（实测结论）:
    - 新浪 stock_zh_a_spot: 一次调用全市场 ~5500 只，稳定
    - 东财 stock_zh_a_spot_em: 字段全，但大陆住宅宽带 IP 会被东财连接级
      间歇风控（RemoteDisconnected/HTTP 000），用作备胎
    - 腾讯 qt.gtimg.cn 批量报价: 不封 IP，字段全，一次 URL 可带 ~60 只
    - 同花顺: q.10jqka 全市场列表有 403 反爬，只有「行业热度汇总」可用
"""

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── 新浪（主源）──────────────────────────────────────────

SINA_SPOT_COLUMNS = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "change_pct",
    "成交量": "volume",
    "成交额": "amount",
}


def sina_spot() -> pd.DataFrame:
    """新浪全市场实时快照（一次调用，含北交所，带 sh/sz/bj 前缀）。"""
    import akshare as ak

    raw = ak.stock_zh_a_spot()
    df = raw[list(SINA_SPOT_COLUMNS)].rename(columns=SINA_SPOT_COLUMNS)
    return df


# ── 东财（备胎：快照/增强）───────────────────────────────

EM_SPOT_COLUMNS = {
    "代码": "code",             # 6 位纯数字
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "change_pct",
    "成交量": "volume",
    "成交额": "amount",
    "市盈率-动态": "pe_ttm",
    "市净率": "pb",
    "总市值": "total_mcap",     # 元 → 转亿
    "流通市值": "float_mcap",   # 元 → 转亿
    "换手率": "turnover",
    "量比": "vol_ratio",
    "涨速": None,
    "最高": "high",
    "最低": "low",
}


def em_spot() -> pd.DataFrame:
    """东财全市场实时快照（字段全；可能被连接级风控，需调用方捕获）。"""
    import akshare as ak

    raw = ak.stock_zh_a_spot_em()
    keep = [k for k in EM_SPOT_COLUMNS if k in raw.columns and EM_SPOT_COLUMNS[k]]
    df = raw[keep].rename(columns={k: EM_SPOT_COLUMNS[k] for k in keep})
    df["code"] = df["code"].astype(str).str.zfill(6).map(_with_exchange)
    for col in ("total_mcap", "float_mcap"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce") / 1e8  # 元 → 亿
    return df


# ── 腾讯批量报价（增强，不封 IP）─────────────────────────

TENCENT_URL = "https://qt.gtimg.cn/q="


def tencent_quotes(codes: list[str], batch: int = 60) -> pd.DataFrame:
    """腾讯批量报价: 一次请求一批代码，返回 PE/PB/市值/换手率等增强字段。

    不封 IP，是初筛增强字段的主源。返回含快照缺失行的 NaN 标记。
    """
    import requests

    rows: list[dict] = []
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        url = TENCENT_URL + ",".join(chunk)
        resp = requests.get(url, timeout=15)
        resp.encoding = "gbk"
        for line in resp.text.splitlines():
            parsed = _parse_tencent_line(line)
            if parsed:
                rows.append(parsed)
        logger.debug("腾讯报价批次 %d 条: %d 行", len(chunk), len(rows))
    return pd.DataFrame(rows)


def _parse_tencent_line(line: str) -> Optional[dict]:
    """解析单行 v_sh600900="1~贵州茅台~600519~..."。字段序号为实测校准值。"""
    if "=" not in line:
        return None
    key, _, val = line.partition("=")
    sym = key.replace("v_", "").replace('"', "").strip()
    val = val.strip().strip('";')
    parts = val.split("~")

    def f(i: int, default=None):
        try:
            v = parts[i]
        except IndexError:
            return default
        if v in ("", "-"):
            return default
        try:
            return float(v)
        except ValueError:
            return default

    def s(i: int, default=None):
        try:
            v = parts[i]
        except IndexError:
            return default
        return v if v not in ("", "-") else default

    quote = {
        "code": sym,
        "name": s(1),
        "price": f(3),
        "prev_close": f(4),
        "open": f(5),
        "high": f(33),
        "low": f(34),
        "amount_wan": f(37),     # 成交额(万元)
        "turnover": f(38),       # 换手率 %
        "pe_ttm": f(39),         # PE(TTM)
        "amplitude": f(43),      # 振幅 %
        "float_mcap": f(44),     # 流通市值(亿)
        "total_mcap": f(45),     # 总市值(亿)
        "pb": f(46),             # 市净率
        "limit_up": f(47),       # 涨停价
        "limit_down": f(48),     # 跌停价
        "vol_ratio": f(49),      # 量比
        "change_pct": f(32),     # 涨跌幅 %
    }
    # 僵尸报价检测（借鉴 a-stock-data）：腾讯对「已迁码的北交所老号段 / 长期停牌股」
    # 仍返回 HTTP 200 + 定格在最后交易日的报价（成交量 0、最新价==昨收），不报任何错。
    # 直接用会算错估值，标记出来供调用方处理。
    price, prev_close, amount_wan = quote["price"], quote["prev_close"], quote["amount_wan"]
    is_stale = (amount_wan == 0 and price == prev_close and price is not None and price > 0)
    quote["is_stale"] = is_stale
    if is_stale:
        if sym[2:4] in ("43", "83", "87"):
            quote["stale_reason"] = "北交所老号段，多数已迁至 920xxx，请按名称反查现行代码"
        else:
            quote["stale_reason"] = "成交量为 0（停牌 / 未开盘 / 废码），报价非当日真实成交"
    else:
        quote["stale_reason"] = ""
    return quote


# ── 同花顺（仅行业热度汇总可用）─────────────────────────


def ths_industry_summary() -> pd.DataFrame:
    """同花顺行业热度汇总（90 行业: 涨跌幅/净流入/领涨股/涨跌家数）。"""
    import akshare as ak

    return ak.stock_board_industry_summary_ths()


# ── 股票池主入口 ─────────────────────────────────────────

def _with_exchange(digits: str) -> str:
    from StockInvestmentTool.screener.board import normalize
    return normalize(digits)


def fetch_universe(source: str, fallback: Optional[str] = None) -> tuple[pd.DataFrame, str]:
    """获取全市场股票池（标准快照列）。

    Returns
    -------
    (df, used_source): df 为标准快照 DataFrame；used_source 为实际命中的源。
    """
    for name in (source, fallback):
        if not name or name == "none":
            continue
        try:
            if name == "sina":
                df = sina_spot()
            elif name == "em":
                df = em_spot()
            elif name == "tencent":
                # 腾讯只做增强，股票池需要代码全集，回退用新浪列表
                continue
            elif name == "baostock":
                df = _baostock_pool()
            else:
                logger.warning("未知股票池源: %s", name)
                continue
            if df is not None and not df.empty:
                df = _standardize_snapshot(df)
                logger.info("股票池命中源 %s: %d 只", name, len(df))
                return df, name
        except Exception as e:
            logger.warning("股票池源 %s 失败(%s: %s)，尝试备胎", name, type(e).__name__, e)
    raise RuntimeError("股票池全部数据源失败（新浪/东财/baostock 均不可用）")


def _baostock_pool() -> pd.DataFrame:
    """baostock 全市场股票列表（type=1 股票），拼装标准快照列（价格为空）。"""
    import baostock as bs
    from StockInvestmentTool.data.fetcher import StockDataFetcher

    fetcher = StockDataFetcher()
    from datetime import datetime, timedelta
    day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    rs = fetcher._bs_query(bs.query_all_stock, day=day)
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(dict(zip(rs.fields, rs.get_row_data())))
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[df["type"] == "1"][["code"]].rename(columns={"code": "code"})
    df["code"] = df["code"].str.replace(".", "", regex=False)
    df["name"] = ""
    df["price"] = None
    df["change_pct"] = None
    df["volume"] = None
    df["amount"] = None
    return df


def _standardize_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    """统一快照列名与类型（code/name/price/change_pct/volume/amount）。"""
    cols = {c: c for c in ("code", "name", "price", "change_pct", "volume", "amount") if c in df.columns}
    df = df[list(cols)].rename(columns=cols)
    for c in ("price", "change_pct", "volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["code"] = df["code"].astype(str).str.lower().str.replace(".", "", regex=False)
    # 裸 6 位代码补前缀（baostock 源）
    if df["code"].str.len().min() == 6:
        df["code"] = df["code"].map(_with_exchange)
    df["name"] = df["name"].astype(str)
    return df
