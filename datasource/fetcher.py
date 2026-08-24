"""数据获取层 — 对接 baostock 获取 A 股行情与基本面数据

线程安全说明:
    baostock 的 login/logout 是全局的，跨线程互相干扰。
    使用 _bs_lock + 引用计数确保多线程安全。
"""

import json
import logging
import signal
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import baostock as bs
import pandas as pd

from StockInvestmentTool.config import Config

logger = logging.getLogger(__name__)

# baostock 全局锁（线程安全）
# 注意: baostock 的 logout/login 交替会导致会话状态不稳定，
# 因此保持长连接，只在服务器关闭时统一登出。
_bs_lock = threading.Lock()
_bs_initialized = False
_last_activity = 0.0       # 最近一次成功查询时间戳（用于空闲主动重连）
_last_relogin = 0.0         # 最近一次强制重连时间戳（用于重连限流）
_last_login_attempt = 0.0   # 最近一次登录尝试时间戳（含失败，用于失败后退避）

# 空闲超时: 连接空闲超过该时长，下次查询前主动重连一次。
# baostock 服务端易在空闲后掐断连接，提前换连接可让查询"第一次就成功"。
_CONN_MAX_IDLE_SECONDS = 10 * 60
# 重连限流: 距上次重连不足该秒数则跳过重连，防止连接不稳时 logout/login
# 风暴触发 baostock 黑名单。
_RELOGIN_MIN_INTERVAL = 60

# 缓存覆盖容忍度(天): 请求区间与缓存首/末日相差少量天数时仍算"覆盖"，
# 避免 web 默认 1 年区间比缓存早/晚 1~2 天就白白触发一次连接。
_START_TOLERANCE_DAYS = 10   # 缓存起始允许比请求晚 ≤10 天
_END_TOLERANCE_DAYS = 3      # 缓存截止允许比请求早 ≤3 天

# 单次 baostock 查询看门狗超时（秒）：
# baostock 的 send_msg 内部 recv 无超时 + while True，连接断开时会永久卡死。
# 用 signal.alarm 中断（仅主线程可用，本工具同步调用均在主线程）。
# 超时后强制重连重试；失败重试仍超时才抛 ConnectionError。
_QUERY_WATCHDOG_SECONDS = 60

# ── baostock send_msg 死循环补丁 ──────────────────────────
# baostock 的 send_msg 内部 while True: recv(8192)，靠结尾分隔符退出。
# 连接被服务端关闭时 recv 立即返回空字节 b''（不阻塞、不触发 socket.timeout），
# 导致 while True 无限循环 + CPU 空转（全市场采集已实测多次卡死）。
# 这里 monkeypatch send_msg：检测到空字节立即抛 ConnectionError，
# 由上层 _bs_query 捕获 → 强制重连恢复。这是根治方案。
_SEND_MSG_ORIGINAL = None


def _install_send_msg_patch():
    """给 baostock 的 send_msg 打补丁：recv 返回空字节 → 抛连接异常。"""
    global _SEND_MSG_ORIGINAL
    try:
        import baostock.util.socketutil as _bs_sock
        import baostock.common.context as _context
        if _SEND_MSG_ORIGINAL is None:
            _SEND_MSG_ORIGINAL = _bs_sock.send_msg

        def _send_msg_patched(msg):
            orig = _SEND_MSG_ORIGINAL
            import socket as _socket
            try:
                sock = getattr(_context, "default_socket", None)
                if sock is not None:
                    # 非阻塞预检：探测 socket 是否已被服务端关闭（recv 返回空字节）
                    try:
                        peek = sock.recv(1, _socket.MSG_PEEK | _socket.MSG_DONTWAIT)
                        if peek == b"":
                            raise ConnectionError(
                                "baostock socket 已被服务端关闭（recv 返回空）")
                    except (BlockingIOError, _socket.timeout):
                        pass  # 无数据可读 = 正常等待
                return orig(msg)
            except ConnectionError:
                raise
            except Exception as e:
                logger.debug("send_msg 异常: %s", e)
                return None

        _bs_sock.send_msg = _send_msg_patched
        logger.info("已安装 baostock send_msg 死循环补丁")
    except Exception as e:
        logger.warning("安装 baostock send_msg 补丁失败: %s", e)


_install_send_msg_patch()

# ── V6.0 财务史：AkShare stock_financial_abstract 指标行 → 标准列名 ─────
# 实测该接口返回 70+ 指标 × 95 期季度史（近24年），扣非/商誉/营收史全覆盖。
ABSTRACT_METRIC_MAP = {
    "扣非净利润": "net_profit_ded",      # 类型决策树第①步
    "商誉": "goodwill",                  # 排雷（商誉/净资产）
    "营业总收入": "revenue",
    "营业总收入增长率": "revenue_yoy",   # 营收同比
    "归母净利润": "net_profit",
    "净利润": "net_profit_total",
    "应收账款周转率": "accounts_receivable",  # 排雷（应收/营收）
    "股东权益合计(净资产)": "net_assets",
    "净资产收益率(ROE)": "roe",
    "毛利率": "gross_margin",
    "资产负债率": "asset_liability_ratio",
    "经营现金流量净额": "operating_cashflow",
}


def _is_connection_error(error_code) -> bool:
    """判断 baostock 错误码是否属于网络/会话失效类（重连可恢复）。

    连接类错误（重连可恢复）: 10002xxx 网络错误族。
    会话失效: 10001001 用户未登录 —— 长连接被服务端断开后，查询会返回
    "未登录"而非连接错误码，必须重连恢复。
    业务/参数类错误（重连无效）: 10004006 参数错误等 10004xxx / 10005xxx。
    """
    if error_code is None:
        return False
    code = str(error_code)
    return code.startswith("10002") or code == "10001001"


def _alarm_handler(signum, frame):
    """signal.alarm 看门狗处理器：超时抛 TimeoutError 打断 baostock recv 死循环。"""
    raise TimeoutError(f"baostock 查询超时（> {_QUERY_WATCHDOG_SECONDS}s），视为连接异常")


def _is_main_thread() -> bool:
    """是否主线程（signal 相关仅主线程可用）。"""
    try:
        return threading.current_thread() is threading.main_thread()
    except Exception:
        return True


class StockDataFetcher:
    """A 股数据获取器（K 线 + 基本面 + 分红）

    线程安全: 多个 fetcher 实例共享一个 baostock 长连接。
    不重复 login/logout，避免会话状态不一致。
    """

    # 交易所前缀规则
    SH_PREFIXES = ("6", "9", "5")  # 5=ETF/LOF 沪市
    SZ_PREFIXES = ("0", "2", "3")

    # 全局初始化状态
    _global_initialized = False

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else Config.DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 懒登录: 缓存命中时完全不需要连接，登录推迟到首次实际查询(_bs_query)

    # ── 连接管理（全局一次，不复用登出）───────────────

    @staticmethod
    def _set_socket_timeout():
        """给 baostock 全局 socket 设超时，防 recv 永久阻塞。

        baostock 的 send_msg 内部 recv 无超时 + while True，连接断开时
        会永久卡死（全市场采集已实测）。设置 socket timeout 后，
        recv 超时会抛 socket.timeout，被 baostock 吞掉返回 None →
        查询走连接错误 → 触发 _force_relogin 重连。
        覆盖 login/logout/查询 所有走 send_msg 的调用。
        """
        try:
            from baostock.util import socketutil as _bs_sock
            sock = getattr(_bs_sock.context, "default_socket", None)
            if sock is not None:
                sock.settimeout(_QUERY_WATCHDOG_SECONDS)
        except Exception as e:
            logger.debug("设置 baostock socket 超时失败: %s", e)

    @classmethod
    def _ensure_login(cls):
        """全局确保 baostock 已登录；空闲过久则主动重连；失败后退避不风暴。"""
        global _bs_initialized, _last_activity, _last_login_attempt
        if _bs_initialized:
            # 空闲超时 → 提前换连接，避免"查询时才发现连接死了"
            if time.time() - _last_activity > _CONN_MAX_IDLE_SECONDS:
                cls._force_relogin()
            return
        with _bs_lock:
            if _bs_initialized:
                return
            # 登录失败退避: 距上次尝试不足60s，不重复尝试（黑名单/服务器挂时不风暴）
            if time.time() - _last_login_attempt < _RELOGIN_MIN_INTERVAL:
                raise ConnectionError("baostock 尚未登录（登录失败退避期内，稍后再试）")
            _last_login_attempt = time.time()
            lg = bs.login()
            if lg.error_code != "0":
                _bs_initialized = False
                raise ConnectionError(f"baostock 登录失败: {lg.error_msg}")
            _bs_initialized = True
            _last_activity = time.time()
            cls._set_socket_timeout()
            logger.info("baostock 登录成功（全局长连接）")

    @classmethod
    def _mark_activity(cls):
        """记录一次成功的连接活动（用于空闲判定）"""
        global _last_activity
        _last_activity = time.time()

    @classmethod
    def _force_relogin(cls):
        """强制重新登录：会话被服务端断开后的唯一恢复手段，带限流。

        baostock 长连接随时可能被服务端断开（WinError 10054 / 接收数据异常）。
        logout 会关闭旧 socket，login 会新建 socket + 会话。
        _RELOGIN_MIN_INTERVAL 秒内不重复重连，避免频繁 login 触发黑名单。
        """
        global _bs_initialized, _last_activity, _last_relogin
        now = time.time()
        with _bs_lock:
            if now - _last_relogin < _RELOGIN_MIN_INTERVAL:
                logger.warning("baostock 重连限流: 距上次重连不足%d秒，跳过重连",
                               _RELOGIN_MIN_INTERVAL)
                return
            _last_relogin = now
            try:
                bs.logout()
            except Exception:
                logger.debug("baostock logout 异常（忽略）", exc_info=True)
            lg = bs.login()
            if lg.error_code != "0":
                _bs_initialized = False
                raise ConnectionError(f"baostock 重新登录失败: {lg.error_msg}")
            _bs_initialized = True
            _last_activity = now
            cls._set_socket_timeout()
            logger.info("baostock 会话已重建")

    def _bs_query(self, query_fn, *args, timeout: Optional[float] = None, **kwargs):
        """执行 baostock 查询，带连接自愈 + 看门狗超时。

        背景1: 长连接被服务端断开后，baostock 的 send_msg 会返回 None，
        查询结果 error_code 变为连接类错误（如 BSERR_RECVSOCK_FAIL），导致
        get_kline 把有效股票误判为"未找到数据"。这里仅对连接类失败
        强制重连并重试一次；业务/参数类错误（如 10004006 年份类别不正确）
        不是连接问题，不重连，原样返回让调用方处理（通常表现为空数据）。

        背景2: baostock send_msg 内部 recv 无超时 + while True，连接断开时
        会永久阻塞。这里用 signal.alarm 作为看门狗，超时强制重连重试，
        避免进程卡死（全市场采集场景已实测卡住）。

        Returns
        -------
        rs: baostock 结果对象（error_code == "0"，或业务类错误码）

        Raises
        ------
        ConnectionError: 重连后仍失败（服务器确实不可用或查询超时）
        """
        for attempt in (1, 2):
            self._ensure_login()
            rs = None
            try:
                # 看门狗：超时抛 TimeoutError，打断 baostock 的 recv 死循环。
                # signal 仅主线程可用；APScheduler 后台线程里会抛
                # "signal only works in main thread"，此时退化为依赖 socket timeout。
                use_signal = _is_main_thread()
                query_timeout = timeout if timeout else _QUERY_WATCHDOG_SECONDS
                if use_signal:
                    signal.signal(signal.SIGALRM, _alarm_handler)
                    signal.alarm(int(query_timeout))
                # 自定义短超时：临时调整 socket timeout（如行业查询）
                _orig_sock_timeout = None
                if timeout:
                    try:
                        from baostock.util import socketutil as _sock
                        sock = getattr(_sock.context, "default_socket", None)
                        if sock is not None:
                            _orig_sock_timeout = sock.gettimeout()
                            sock.settimeout(timeout)
                    except Exception:
                        pass
                try:
                    rs = query_fn(*args, **kwargs)
                finally:
                    if use_signal:
                        signal.alarm(0)  # 取消闹钟
                    if _orig_sock_timeout is not None:
                        try:
                            from baostock.util import socketutil as _sock
                            sock = getattr(_sock.context, "default_socket", None)
                            if sock is not None:
                                sock.settimeout(_orig_sock_timeout)
                        except Exception:
                            pass
            except (OSError, ConnectionError, TimeoutError) as e:
                if attempt == 1:
                    logger.warning("baostock 连接异常(%s)，重连后重试", e)
                    self._force_relogin()
                    continue
                raise ConnectionError(f"baostock 查询异常: {e}") from e

            # query_fn 返回 None（baostock send_msg 吞掉异常）→ 按连接错误处理
            if rs is None:
                if attempt == 1:
                    logger.warning("baostock 查询返回 None，重连后重试")
                    self._force_relogin()
                    continue
                raise ConnectionError("baostock 查询返回 None（连接异常）")

            code = getattr(rs, "error_code", "0")
            if code == "0":
                self._mark_activity()
                return rs
            msg = getattr(rs, "error_msg", "") or "unknown error"
            if _is_connection_error(code):
                if attempt == 1:
                    logger.warning("baostock 连接错误(error_code=%s: %s)，重连后重试",
                                   code, msg)
                    self._force_relogin()
                    continue
                raise ConnectionError(f"baostock 连接失败: {msg}")
            # 业务/参数类错误：连接是通的，只是业务报错，记录活动并交调用方处理
            self._mark_activity()
            logger.warning("baostock 查询返回业务错误(error_code=%s: %s)，不重试",
                           code, msg)
            return rs
        raise ConnectionError("baostock 查询失败")  # 理论不可达

    @classmethod
    def shutdown(cls):
        """服务器关闭时统一登出"""
        global _bs_initialized
        with _bs_lock:
            if _bs_initialized:
                bs.logout()
                _bs_initialized = False
                logger.info("baostock 已登出")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass  # 不登出，保持全局连接

    # ── 代码标准化 ───────────────────────────────────

    @staticmethod
    def normalize_code(code: str) -> str:
        """将用户输入的代码转为 baostock 标准格式

        兼容多种写法:
        >>> StockDataFetcher.normalize_code("600900")
        'sh.600900'
        >>> StockDataFetcher.normalize_code("sz.000001")
        'sz.000001'
        >>> StockDataFetcher.normalize_code("sz000001")
        'sz.000001'
        >>> StockDataFetcher.normalize_code("SH600519")
        'sh.600519'
        """
        code = code.strip().lower()
        if "." in code:
            prefix, num = code.split(".", 1)
            if prefix in ("sh", "sz", "bj"):
                return f"{prefix}.{num}"
        if code[:2] in ("sh", "sz", "bj") and len(code) >= 8:
            return f"{code[:2]}.{code[2:]}"
        if code.startswith(StockDataFetcher.SH_PREFIXES):
            return f"sh.{code}"
        else:
            return f"sz.{code}"

    @staticmethod
    def detect_type(code: str) -> str:
        """检测代码类型: stock / etf / index

        精确规则（区分 sh/sz/bj 前缀）:
          - ETF: sh.5xxxxx / sh.51xxxx / sz.15xxxx / sz.16xxxx
          - 指数: sh.000xxx（上证指数系列）/ sh.9xxxxx / sz.399xxx（深证指数）
          - 其余为股票（注意 sz.000xxx 是深市主板股票，如平安银行 sz.000001）
        """
        raw = code.strip().lower()
        prefix = raw[:2]
        digits = raw
        for p in ("sh.", "sz.", "bj.", "sh", "sz", "bj"):
            if digits.startswith(p):
                digits = digits[len(p):]
                break
        if not digits.isdigit() or len(digits) < 6:
            return "stock"
        d6 = digits[:6]
        # ETF: 沪 5xxxxx, 深 15/16
        if prefix in ("sh", "bj") and d6.startswith(("5", "51", "56", "58")):
            return "etf"
        if prefix == "sz" and d6.startswith(("15", "16", "18")):
            return "etf"
        if d6.startswith(("159", "510", "511", "512", "513", "515", "516", "518")):
            return "etf"
        # 指数: sh.000xxx（上证指数）, sh.9xxxxx, sz.399xxx（深证）, 中证 000/932
        if prefix == "sh" and d6.startswith("000"):
            return "index"
        if prefix == "sz" and d6.startswith("399"):
            return "index"
        if prefix == "bj" and d6.startswith(("000", "932")):
            return "index"
        return "stock"

    # ── K 线数据 ─────────────────────────────────────

    @staticmethod
    def _normalize_kline_df(df: pd.DataFrame) -> pd.DataFrame:
        """规范化 K 线 DataFrame：数值列转换 + 日期排序去重。"""
        df = df.copy()
        numeric_cols = [c for c in df.columns if c != "date"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
        return df

    def _read_cached_kline(self, code: str) -> Optional[pd.DataFrame]:
        """读取缓存 K 线文件，返回 DataFrame 或 None。"""
        code = self.normalize_code(code)
        csv_path = self.data_dir / f"{code.replace('.', '_')}_kline.csv"
        if not csv_path.exists():
            return None
        try:
            df = pd.read_csv(csv_path, encoding="utf-8")
            return self._normalize_kline_df(df)
        except Exception:
            logger.debug("缓存 K 线读取失败: %s", csv_path, exc_info=True)
            return None

    def _load_cached_kline(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """缓存优先: 请求区间落在容忍度内则直接复用缓存，否则 None。

        覆盖判定（允许少量缺失，避免 1~2 天差异触发连接）:
          - 缓存起始不晚于请求起始 + _START_TOLERANCE_DAYS 天
          - 缓存截止不早于请求截止 - _END_TOLERANCE_DAYS 天
        """
        df = self._read_cached_kline(code)
        if df is None or len(df) == 0:
            return None
        first = df["date"].iloc[0]
        last = df["date"].iloc[-1]
        req_start = pd.Timestamp(start_date)
        req_end = pd.Timestamp(end_date)
        start_ok = (first - req_start).days <= _START_TOLERANCE_DAYS
        end_ok = last >= req_end - pd.Timedelta(days=_END_TOLERANCE_DAYS)
        if start_ok and end_ok:
            return df
        logger.debug("缓存未覆盖请求区间: 缓存 %s~%s, 请求 %s~%s（start_ok=%s end_ok=%s）",
                     first.date(), last.date(), req_start.date(), req_end.date(),
                     start_ok, end_ok)
        return None

    def _load_cached_kline_any(self, codes) -> Optional[pd.DataFrame]:
        """任意覆盖度的缓存（拉取失败降级兜底用），返回第一条命中的。"""
        for c in codes:
            df = self._read_cached_kline(c)
            if df is not None and len(df) > 0:
                return df
        return None

    def get_kline(
        self,
        code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        frequency: str = "d",
        adjustflag: str = "2",
        fields: Optional[str] = None,
    ) -> pd.DataFrame:
        """获取历史 K 线数据（缓存优先，避免反复连接 baostock）

        优先级:
          ① 缓存命中（完整覆盖请求区间）→ 直接用，0 次连接
          ② 拉取 baostock → 写缓存
          ③ 拉取失败（连接挂/黑名单）→ 降级用缓存（可能不覆盖区间），并警告

        自动纠错: 纯数字代码如 000425 会先按规则分配到 sh/sz，
        若查不到数据则自动尝试另一个交易所。

        Parameters
        ----------
        code : str
            股票代码，支持 600900 / sh.600900 / sz.000001 格式
        start_date, end_date : str, optional
            YYYY-MM-DD 格式。默认 end_date=昨天，start_date=1年前
        frequency : str
            d=日线, w=周, m=月
        adjustflag : str
            复权类型: 1=后复权, 2=前复权, 3=不复权
        fields : str, optional
            逗号分隔字段列表。默认含 peTTM/pbMRQ
        """
        # 记录用户原始输入，判断是否显式指定了前缀
        raw_input = code.strip()
        has_explicit_prefix = "." in raw_input

        normalized = self.normalize_code(code)

        if fields is None:
            fields = Config.BAOSTOCK_DEFAULT_FIELDS

        if end_date is None:
            # 默认拉到"今天"：盘后 baostock 有当天日K（技术指标反映今天收盘）；
            # 盘中 baostock 不返回未收盘当天 → 自然回退到最近收盘
            end_date = datetime.now().strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        # 尝试获取数据，必要时自动切换交易所
        candidates = [normalized]
        if not has_explicit_prefix:
            # 自动补一个反向前缀
            other = f"sh.{raw_input}" if normalized.startswith("sz.") else f"sz.{raw_input}"
            candidates.append(other)

        # ① 缓存优先: 完整覆盖请求区间 → 直接用，不连接
        for c in candidates:
            cached = self._load_cached_kline(c, start_date, end_date)
            if cached is not None:
                logger.info("K 线缓存命中: %s %s~%s (%d 条)", c, start_date, end_date, len(cached))
                return cached

        # ② 拉取 baostock
        try:
            for attempt_code in candidates:
                logger.info("获取 K 线: %s %s~%s", attempt_code, start_date, end_date)
                rs = self._bs_query(
                    bs.query_history_k_data_plus,
                    code=attempt_code,
                    fields=fields,
                    start_date=start_date,
                    end_date=end_date,
                    frequency=frequency,
                    adjustflag=adjustflag,
                )

                data_list = []
                while (rs.error_code == "0") & rs.next():
                    data_list.append(rs.get_row_data())

                if data_list:
                    # 成功！用这个结果
                    fields_list = self._parse_fields(rs)
                    df = pd.DataFrame(data_list, columns=fields_list)
                    df = self._normalize_kline_df(df)

                    csv_path = self.data_dir / f"{attempt_code.replace('.', '_')}_kline.csv"
                    df.to_csv(csv_path, index=False, encoding="utf-8")
                    logger.info("K 线数据已缓存: %s (%d 条)", csv_path, len(df))
                    return df

                logger.warning("未获取到数据: %s %s~%s", attempt_code, start_date, end_date)
        except Exception as e:
            # ③ 降级: 拉取失败(连接挂/黑名单)用缓存兜底，可能不覆盖请求区间
            stale = self._load_cached_kline_any(candidates)
            if stale is not None:
                logger.warning("baostock 拉取失败(%s)，降级使用缓存数据(%d 条，可能过期)",
                               e, len(stale))
                return stale
            raise

        raise ValueError(
            f"股票 {raw_input} 在深市和沪市均未找到数据，请检查代码是否正确。\n"
            f"提示: 6xx/9xx 开头为沪市(sh)，0xx/2xx/3xx 开头为深市(sz)"
        )

    # ── 基本面数据 ──────────────────────────────────

    @staticmethod
    def _parse_fields(rs) -> list:
        """安全解析 baostock 返回的 fields（3.x 版本返回 list，旧版返回 str）"""
        return rs.fields if isinstance(rs.fields, list) else rs.fields.split(",")

    def get_profit_data(self, code: str, year: int, quarter: int) -> dict:
        """获取盈利数据（连接/登录失败时降级返回空，不阻塞分析）

        Returns
        -------
        dict
            keys: pubDate, statDate, ROEVal, eps, netProfitMargin, grossProfitMargin, ...
        """
        code = self.normalize_code(code)
        try:
            rs = self._bs_query(bs.query_profit_data, code=code, year=year, quarter=quarter)
            result = []
            while (rs.error_code == "0") & rs.next():
                result.append(rs.get_row_data())
            if result:
                fields = self._parse_fields(rs)
                return dict(zip(fields, result[0]))
        except Exception as e:
            logger.warning("盈利数据获取失败(%s): %s，降级返回空", code, e)
        return {}

    def get_cash_flow_data(self, code: str, year: int, quarter: int) -> dict:
        """获取现金流数据（连接/登录失败时降级返回空，不阻塞分析）"""
        code = self.normalize_code(code)
        try:
            rs = self._bs_query(bs.query_cash_flow_data, code=code, year=year, quarter=quarter)
            result = []
            while (rs.error_code == "0") & rs.next():
                result.append(rs.get_row_data())
            if result:
                fields = self._parse_fields(rs)
                return dict(zip(fields, result[0]))
        except Exception as e:
            logger.warning("现金流数据获取失败(%s): %s，降级返回空", code, e)
        return {}

    @staticmethod
    def _div_cache_path(code: str, data_dir) -> Path:
        return data_dir / f"{code.replace('.', '_')}_dividends.json"

    def _load_div_cache(self, code: str) -> dict:
        """读取分红缓存 {year: [records]}（年度静态数据，缓存基本一次到位）。"""
        path = self._div_cache_path(code, self.data_dir)
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logger.debug("分红缓存读取失败: %s", path, exc_info=True)
        return {}

    def _save_div_cache(self, code: str, cache: dict):
        path = self._div_cache_path(code, self.data_dir)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception:
            logger.warning("分红缓存写入失败: %s", path)

    def get_dividend_data(self, code: str, year: int) -> list[dict]:
        """获取分红数据（按年缓存，避免反复连接）

        yearType 必须是字符串类别: "report"=预案公告年份 / "operate"=除权除息年份。
        传整数(1/2)会被服务器拒绝(10004006 年份类别不正确)。
        同时把新版本字段归一化为旧接口字段: dividSum(每股股利)、pubDate(公告日)，
        供 ValuationHelper.triple_anchor 计算股息率锚使用。

        Returns
        -------
        list[dict]
            分红记录，key 含 dividSum(每股股利)、pubDate(公告日)
        """
        code = self.normalize_code(code)
        cache = self._load_div_cache(code)
        key = str(year)
        if key in cache:
            return list(cache[key])

        results = []
        had_error = False
        for year_type in ("report", "operate"):
            try:
                rs = self._bs_query(bs.query_dividend_data, code=code, year=year, yearType=year_type)
                while (rs.error_code == "0") & rs.next():
                    fields = self._parse_fields(rs)
                    row = dict(zip(fields, rs.get_row_data()))
                    # 归一化字段名: 旧接口 dividSum/pubDate → 新版本字段
                    row.setdefault("dividSum", row.get("dividCashPsBeforeTax", ""))
                    row.setdefault("pubDate", row.get("dividPlanAnnounceDate", ""))
                    results.append(row)
            except Exception:
                had_error = True
                continue

        if had_error:
            # 连接/登录失败: 不写缓存，避免空结果挡住之后的正常拉取
            return results
        cache[key] = results
        self._save_div_cache(code, cache)
        return results

    def get_stock_basic(self, code: str) -> dict:
        """获取股票基本信息（连接/登录失败时降级返回空，不阻塞分析）"""
        code = self.normalize_code(code)
        try:
            rs = self._bs_query(bs.query_stock_basic, code=code)
            while (rs.error_code == "0") & rs.next():
                fields = self._parse_fields(rs)
                return dict(zip(fields, rs.get_row_data()))
        except Exception as e:
            logger.warning("股票基本信息获取失败(%s): %s，降级返回空", code, e)
        return {}

    # ── V6.0 财务史（AkShare 东财财务摘要）────────────

    @staticmethod
    def parse_abstract_to_history(raw: pd.DataFrame) -> pd.DataFrame:
        """把 AkShare `stock_financial_abstract` 原始宽表转成统一季度史。

        V6.0 六步法 ②③ 的主输入（一行一季，stat_date 升序）。

        Parameters
        ----------
        raw : pd.DataFrame
            akshare `stock_financial_abstract` 返回的宽表：
            列 = [选项, 指标, <各报告期如 20260331>…]，行 = 70+ 指标 × 报告期。

        Returns
        -------
        pd.DataFrame
            索引 stat_date（datetime，升序），列见 ABSTRACT_METRIC_MAP；
            数值单位：金额类为「元」，比率类为「百分数绝对值」
            （revenue_yoy 6.44 表示 +6.44%）。缺失指标列不出现。
        """
        # 报告期列：8 位数字且前 4 位像年份
        period_cols = [
            c for c in raw.columns
            if isinstance(c, str) and len(c) == 8 and c.isdigit()
            and 1990 <= int(c[:4]) <= 2100
        ]
        if not period_cols:
            raise ValueError("stock_financial_abstract 原始表无报告期列，结构可能改版")

        # 行：指标名 → 该指标在【最新报告期】的值
        metric_rows = raw[["指标"] + period_cols].copy()
        metric_rows["period"] = period_cols[0]
        # 指标名可能有重复（如「毛利率」出现在 常用指标/盈利能力），取第一个
        metric_rows = metric_rows.drop_duplicates(subset="指标", keep="first")

        # 转置：period → 行，指标 → 列
        long = metric_rows.melt(id_vars="指标", value_vars=period_cols,
                                var_name="stat_date", value_name="value")
        # 指标重复（跨分类同名）导致 melt 多行，pivot 前先按最新期去重
        long = long.drop_duplicates(subset=["指标", "stat_date"], keep="first")
        wide = long.pivot(index="stat_date", columns="指标", values="value")

        wide = wide.reset_index()
        wide["stat_date"] = pd.to_datetime(wide["stat_date"], format="%Y%m%d")
        # 数值列转 float（空值 → NaN）
        for c in wide.columns:
            if c != "stat_date":
                wide[c] = pd.to_numeric(wide[c], errors="coerce")
        wide = wide.sort_values("stat_date").reset_index(drop=True)

        # 映射为统一列名（只保留 V6.0 需要的）
        keep = {"stat_date": "stat_date"}
        for raw_name, std_name in ABSTRACT_METRIC_MAP.items():
            if raw_name in wide.columns:
                keep[raw_name] = std_name
        out = wide[[k for k in keep if k in wide.columns]].rename(columns=keep)
        return out

    def _fund_cache_path(self, code: str) -> Path:
        return self.data_dir / f"{code.replace('.', '_')}_fundamentals.json"

    def _load_fund_cache(self, code: str) -> Optional[dict]:
        path = self._fund_cache_path(code)
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logger.debug("财务史缓存读取失败: %s", path, exc_info=True)
        return None

    def _save_fund_cache(self, code: str, payload: dict):
        path = self._fund_cache_path(code)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception:
            logger.warning("财务史缓存写入失败: %s", path)

    def get_fundamental_history(self, code: str, years: int = 5,
                                use_cache: bool = True) -> pd.DataFrame:
        """V6.0 财务史主入口：AkShare `stock_financial_abstract`，缓存优先。

        - 数据：扣非净利润 / 商誉 / 营业总收入(含同比) / 归母净利润 /
          应收账款周转率 / 净资产 / ROE / 毛利率 / 负债率 / 经营现金流。
        - 缓存：JSON 存原始宽表（季度静态数据，基本一次到位）。
        - 降级：拉取失败且无缓存 → 返回空 DataFrame（不阻塞分析）。

        Parameters
        ----------
        years : int
            截取最近 N 年季度（默认 5，覆盖 3 年营收 CAGR 判定 + 5 年下滑判定）。
        """
        code = self.normalize_code(code)
        digits = code.split(".")[1]

        raw_payload = None
        if use_cache:
            raw_payload = self._load_fund_cache(code)
            if raw_payload is not None:
                logger.info("财务史缓存命中: %s (%d 期)", code, len(raw_payload))
                raw = pd.DataFrame(raw_payload)
                return self.parse_abstract_to_history(raw).tail(4 * years)

        try:
            import akshare as ak
            raw = ak.stock_financial_abstract(symbol=digits)
            if raw is None or raw.empty:
                logger.warning("财务史拉取为空: %s", code)
                return pd.DataFrame()
            self._save_fund_cache(code, raw.to_dict("records"))
        except Exception as e:
            logger.warning("财务史拉取失败(%s): %s，降级空表", code, e)
            if raw_payload is not None:
                return self.parse_abstract_to_history(pd.DataFrame(raw_payload)).tail(4 * years)
            return pd.DataFrame()

        return self.parse_abstract_to_history(raw).tail(4 * years)

    # ── V6.0 行业（baostock）────────────────────────

    def get_stock_industry(self, code: str) -> str:
        """获取所属证监会行业（V6.0 类型决策树：金融判定/强周期名单）。

        实测 OK：长江电力 → 'D44电力、热力生产和供应业'。
        失败/未取到 → 返回 ''（调用方按未知行业处理，不阻塞）。

        缓存: 行业是低频静态数据，首次拉取后存 JSON，后续直接读缓存，
        避免每次实时连 baostock（连接不稳定时会导致接口卡住）。
        """
        import json as _json
        from StockInvestmentTool.config import Config as _Config

        code = self.normalize_code(code)
        cache_file = _Config.DATA_DIR / "industry_cache.json"
        # ① 读缓存
        try:
            if cache_file.exists():
                cache = _json.loads(cache_file.read_text(encoding="utf-8"))
                if code in cache:
                    return cache.get(code, "")
        except Exception:
            cache = {}
        # ② 拉取 baostock（短超时 10s，连接不稳时快速失败，不卡页面）
        industry = ""
        try:
            rs = self._bs_query(bs.query_stock_industry, code=code, timeout=10)
            while (rs.error_code == "0") & rs.next():
                fields = self._parse_fields(rs)
                row = dict(zip(fields, rs.get_row_data()))
                industry = row.get("industry", "") or ""
                break
        except Exception as e:
            logger.warning("行业数据获取失败(%s): %s，降级空串", code, e)
            return ""
        # ③ 写缓存
        try:
            cache[code] = industry
            cache_file.write_text(_json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return industry

    # ── V6.0 业绩快照（AkShare 业绩报表，补毛利率）───

    @staticmethod
    def _latest_report_dates(count: int = 4, as_of=None) -> list[str]:
        """最近 count 个【已披露】季报的 YYYYMMDD 列表（按时间倒序）。

        用于 stock_yjbb_em 的 date 参数（必须传报告期末）。A 股年报/Q1/中报/
        Q3 末分别为 1231/0331/0630/0930。当前季度尚未披露，从最近已完成季报
        往回推：
          - 1~3 月   → 最近已完成 = 上年 Q4（1231）
          - 4~6 月   → 本年 Q1（0331）
          - 7~9 月   → 本年 Q2（0630）
          - 10~12 月 → 本年 Q3（0930）

        as_of 供测试注入日期，缺省用当前时间。
        """
        now = as_of or datetime.now()
        y, m = now.year, now.month
        quarter_end = ["0331", "0630", "0930", "1231"]  # 日历序: Q1..Q4
        # 最近已完成季度的索引（从当前季度退一格）
        q_idx = ((m - 1) // 3) - 1  # -1 → 上年 Q4；0 → 本年 Q1；1 → 本年 Q2；2 → 本年 Q3
        dates = []
        yy, qi = y, q_idx
        for _ in range(count):
            if qi < 0:
                qi = 3
                yy -= 1
            dates.append(f"{yy}{quarter_end[qi]}")
            qi -= 1
        return dates

    def get_annual_profit_snapshot(self, code: str) -> dict:
        """最新业绩报表快照（AkShare `stock_yjbb_em`，补销售毛利率）。

        stock_financial_abstract 没有「销售毛利率」指标，业绩报表有。
        返回含 每股收益/营收(含同比)/净利润(含同比)/销售毛利率/ROE/行业 的快照。

        拉取失败 → 返回 {}（调用方降级，不阻塞）。
        """
        digits = self.normalize_code(code).split(".")[1]
        try:
            import akshare as ak
            for date in self._latest_report_dates():
                df = ak.stock_yjbb_em(date=date)
                if df is None or df.empty:
                    continue
                row = df[df["股票代码"] == digits]
                if not row.empty:
                    r = row.iloc[0]
                    return {
                        "report_date": date,
                        "eps": r.get("每股收益"),
                        "revenue": r.get("营业总收入-营业总收入"),
                        "revenue_yoy": r.get("营业总收入-同比增长"),
                        "net_profit": r.get("净利润-净利润"),
                        "net_profit_yoy": r.get("净利润-同比增长"),
                        "gross_margin": r.get("销售毛利率"),
                        "roe": r.get("净资产收益率"),
                        "industry": r.get("所处行业"),
                    }
            logger.warning("业绩报表未找到 %s（最近 %d 期）", code, len(self._latest_report_dates()))
        except Exception as e:
            logger.warning("业绩快照获取失败(%s): %s，降级空 dict", code, e)
        return {}

    # ── 一站式获取 ──────────────────────────────────

    def fetch_all(self, code: str, years_back: int = 1) -> dict:
        """一次性获取分析所需的全部数据

        Returns
        -------
        dict
            - kline: DataFrame 日线
            - profit: dict 最新盈利数据
            - dividends: list[dict] 近5年分红
            - basic: dict 基本信息
        """
        end = datetime.now() - timedelta(days=1)
        start = end - timedelta(days=365 * years_back)

        kline = self.get_kline(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        basic = self.get_stock_basic(code)

        # 最新完整季度
        current_year = end.year
        last_q = ((end.month - 1) // 3)
        if last_q == 0:
            last_q = 4
            current_year -= 1

        profit = self.get_profit_data(code, current_year, last_q)

        # 近5年分红
        all_divs = []
        for y in range(current_year - 5, current_year + 1):
            all_divs.extend(self.get_dividend_data(code, y))

        return {
            "kline": kline,
            "profit": profit,
            "dividends": all_divs,
            "basic": basic,
        }
