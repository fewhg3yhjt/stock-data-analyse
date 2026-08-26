# -*- coding: utf-8 -*-
"""全量数据仓库 — Parquet 月分区存储层 + SQLite 元数据清单

设计约束（2C2G / 40GB VPS）:
  - 原始全量数据用 Parquet 按月分区落地（列存压缩，磁盘友好、支持按需读取）
  - 元数据（标的清单/数据清单 manifest）落 SQLite，轻量且可查询
  - 查询/计算用 DuckDB 按需读 Parquet（嵌入式、按查询起停、不常驻内存）
  - 任何「全量载入内存」的用法都要避免 —— 一律按月度分块处理

布局:
  output/data/warehouse/
  ├── daily/YYYY-MM.parquet      # 全市场日线（每行 = 一标的一日）
  ├── factors/YYYY-MM.parquet    # 全市场因子宽表
  ├── online/YYYY-MM-DD/         # 观察池盘中快照（按日）
  └── meta.db                    # SQLite: instruments / daily_manifest / factor_manifest
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 日线标准列（baostock query_history_k_data_plus 常用字段）
DAILY_COLUMNS = [
    "date", "code", "open", "high", "low", "close",
    "volume", "amount", "peTTM", "pbMRQ", "turn", "tradestatus",
]

# 因子宽表前缀列（后续按需叠加）
FACTOR_COLUMNS_PREFIX = [
    "date", "code", "close", "volume", "amount",
]


def _ym_str(dt) -> str:
    """YYYY-MM"""
    if isinstance(dt, str):
        dt = datetime.strptime(dt[:10], "%Y-%m-%d")
    return dt.strftime("%Y-%m")


class Warehouse:
    """全量数据仓库入口：路径、分区读写、元数据清单。"""

    def __init__(self, base_dir: Optional[Path] = None):
        from StockInvestmentTool.config import Config

        self.base_dir = Path(base_dir) if base_dir else Config.DATA_DIR / "warehouse"
        self.daily_dir = self.base_dir / "daily"
        self.factor_dir = self.base_dir / "factors"
        self.indicator_dir = self.base_dir / "indicators"
        self.fundamental_dir = self.base_dir / "fundamentals"
        self.online_dir = self.base_dir / "online"
        self.minute_dir = self.base_dir / "minute"
        self.meta_db_path = self.base_dir / "meta.db"
        for d in (self.daily_dir, self.factor_dir, self.indicator_dir,
                  self.fundamental_dir, self.online_dir, self.minute_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._init_meta()

    @property
    def raw(self):
        """贴源层（各接口原始数据独立存放）"""
        from StockInvestmentTool.warehouse.raw import RawStore
        return RawStore(self.base_dir)

    # ── 元数据 ─────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.meta_db_path)

    def _init_meta(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS instruments (
                    code TEXT PRIMARY KEY,        -- sh600900
                    name TEXT,
                    type TEXT,                    -- stock/etf/index
                    board TEXT,
                    listed_date TEXT,
                    industry TEXT DEFAULT '',     -- 证监会行业（如 I64互联网和相关服务）
                    updated_at TEXT
                )
            """)
            # 迁移：老库加 industry 列
            cols = {r[1] for r in c.execute("PRAGMA table_info(instruments)").fetchall()}
            if "industry" not in cols:
                c.execute("ALTER TABLE instruments ADD COLUMN industry TEXT DEFAULT ''")
            c.execute("""
                CREATE TABLE IF NOT EXISTS daily_manifest (
                    month TEXT PRIMARY KEY,       -- YYYY-MM
                    rows INTEGER,
                    symbols INTEGER,
                    last_date TEXT,
                    updated_at TEXT
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS factor_manifest (
                    month TEXT PRIMARY KEY,
                    rows INTEGER,
                    symbols INTEGER,
                    factor_list TEXT,
                    updated_at TEXT
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS fundamental_manifest (
                    code TEXT PRIMARY KEY,        -- sh600900
                    rows INTEGER,
                    last_period TEXT,             -- 最新报告期
                    updated_at TEXT
                )
            """)

    def upsert_instruments(self, rows: Iterable[dict]):
        """批量写入/更新标的清单。rows: [{code,name,type,board,listed_date,industry}]"""
        rows = list(rows)
        if not rows:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as c:
            c.executemany(
                """INSERT INTO instruments(code,name,type,board,listed_date,industry,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name, type=excluded.type, board=excluded.board,
                     listed_date=excluded.listed_date, industry=excluded.industry,
                     updated_at=excluded.updated_at""",
                [(r.get("code"), r.get("name"), r.get("type"), r.get("board"),
                  r.get("listed_date"), r.get("industry", ""), now) for r in rows],
            )

    def update_industry(self, code: str, industry: str):
        """更新单只标的行业（低频静态，采集后写入）。"""
        with self._conn() as c:
            c.execute(
                "UPDATE instruments SET industry=?, updated_at=? WHERE code=?",
                (industry, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), code),
            )

    def get_industry(self, code: str) -> str:
        """查询标的行业（meta.db）。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT industry FROM instruments WHERE code=?", (code,)
            ).fetchone()
        return (row[0] or "") if row else ""

    def all_codes(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute("SELECT code FROM instruments").fetchall()
        return [r[0] for r in rows]

    def instrument_types(self) -> dict[str, str]:
        with self._conn() as c:
            return {r[0]: r[1] for r in c.execute("SELECT code,type FROM instruments")}

    # ── 日线分区读写 ────────────────────────────────────

    def daily_partition(self, month: str) -> Path:
        """某月日线分区文件路径"""
        return self.daily_dir / f"{month}.parquet"

    def _read_partition(self, directory: Path, month: str) -> "Optional[pd.DataFrame]":
        path = directory / f"{month}.parquet"
        if not path.exists():
            return None
        try:
            import pandas as pd
            return pd.read_parquet(path)
        except ImportError:
            logger.error("缺少 pyarrow，无法读取 parquet（pip install pyarrow）")
            return None
        except Exception as e:
            logger.warning("读取分区 %s 失败: %s", path, e)
            return None

    def read_daily(self, month: str):
        """读取某月日线分区（返回 DataFrame 或 None）"""
        return self._read_partition(self.daily_dir, month)

    def read_factor(self, month: str):
        """读取某月因子分区（返回 DataFrame 或 None）"""
        return self._read_partition(self.factor_dir, month)

    def write_factor_partition(self, month: str, df, factor_list: Optional[list] = None) -> int:
        """把某月因子分区整体覆写。返回写入行数。"""
        path = self.factor_dir / f"{month}.parquet"
        try:
            df.to_parquet(path, index=False, engine="pyarrow",
                           compression="zstd" if _has("pyarrow") else "snappy")
        except Exception as e:
            logger.error("写因子分区 %s 失败: %s", path, e)
            raise
        symbols = int(df["code"].nunique()) if "code" in df.columns else 0
        with self._conn() as c:
            c.execute(
                """INSERT INTO factor_manifest(month,rows,symbols,factor_list,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(month) DO UPDATE SET
                     rows=excluded.rows, symbols=excluded.symbols,
                     factor_list=excluded.factor_list, updated_at=excluded.updated_at""",
                (month, int(len(df)), symbols,
                 ",".join(factor_list) if factor_list else "",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        logger.info("因子分区已写入: %s (%d 行 / %d 标的)", path.name, len(df), symbols)
        return len(df)

    def write_indicator_partition(self, month: str, df) -> int:
        """把某月指标分区整体覆写。返回写入行数。"""
        path = self.indicator_dir / f"{month}.parquet"
        try:
            df.to_parquet(path, index=False, engine="pyarrow",
                           compression="zstd" if _has("pyarrow") else "snappy")
        except Exception as e:
            logger.error("写指标分区 %s 失败: %s", path, e)
            raise
        symbols = int(df["code"].nunique()) if "code" in df.columns else 0
        logger.info("指标分区已写入: %s (%d 行 / %d 标的)", path.name, len(df), symbols)
        return len(df)

    def read_indicator(self, month: str):
        """读取某月指标分区"""
        return self._read_partition(self.indicator_dir, month)

    def read_indicator_code(self, code: str, days: int = 750) -> Optional[pd.DataFrame]:
        """通过 DuckDB 只读取一个标的的指标分区数据。"""
        import duckdb

        files = [str(self.indicator_dir / f"{m}.parquet")
                 for m in self.available_months("indicator")]
        files = [p for p in files if Path(p).exists()]
        if not files:
            return None
        normalized = str(code).lower().replace(".", "")
        if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", normalized):
            raise ValueError("股票代码格式无效")
        days = max(1, min(int(days), 5000))
        escaped = "[" + ",".join("'" + p.replace("'", "''") + "'" for p in files) + "]"
        conn = duckdb.connect()
        try:
            frame = conn.execute(
                f"SELECT * FROM read_parquet({escaped}) WHERE code=? ORDER BY date DESC LIMIT ?",
                [normalized, days],
            ).df()
            return frame.sort_values("date").reset_index(drop=True)
        finally:
            conn.close()

    # ── 基本面层（fundamentals）───────────────────────

    def fundamental_path(self, code: str) -> Path:
        """单只标的基本面分区文件（按 code 存，季度报告数据）。"""
        return self.fundamental_dir / f"{code}.parquet"

    def write_fundamentals(self, code: str, df) -> int:
        """写入单只标的基本面历史（按 code 覆盖写）。返回行数。"""
        path = self.fundamental_path(code)
        try:
            df.to_parquet(path, index=False, engine="pyarrow",
                          compression="zstd" if _has("pyarrow") else "snappy")
        except Exception as e:
            logger.error("写基本面 %s 失败: %s", code, e)
            raise
        # 更新 manifest
        last_period = str(df["stat_date"].max())[:10] if "stat_date" in df.columns else ""
        with self._conn() as c:
            c.execute(
                """INSERT INTO fundamental_manifest(code,rows,last_period,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                     rows=excluded.rows, last_period=excluded.last_period,
                     updated_at=excluded.updated_at""",
                (code, int(len(df)), last_period,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        logger.info("基本面已写入: %s (%d 行)", code, len(df))
        return len(df)

    def read_fundamentals(self, code: str):
        """读取单只标的基本面历史（DataFrame 或 None）。"""
        path = self.fundamental_path(code)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            if "stat_date" in df.columns:
                df["stat_date"] = pd.to_datetime(df["stat_date"])
            return df
        except Exception as e:
            logger.warning("基本面读取失败 %s: %s", path, e)
            return None

    def has_fundamentals(self, code: str) -> bool:
        return self.fundamental_path(code).exists()

    def write_daily_partition(self, month: str, df) -> int:
        """把某月日线分区整体覆写。df 需含 code/date 等列。返回写入行数。"""
        path = self.daily_partition(month)
        try:
            df.to_parquet(path, index=False, engine="pyarrow",
                           compression="zstd" if _has("pyarrow") else "snappy")
        except Exception as e:
            logger.error("写日线分区 %s 失败: %s", path, e)
            raise
        # 更新 manifest
        symbols = int(df["code"].nunique()) if "code" in df.columns else 0
        last_date = str(df["date"].max())[:10] if "date" in df.columns else ""
        with self._conn() as c:
            c.execute(
                """INSERT INTO daily_manifest(month,rows,symbols,last_date,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(month) DO UPDATE SET
                     rows=excluded.rows, symbols=excluded.symbols,
                     last_date=excluded.last_date, updated_at=excluded.updated_at""",
                (month, int(len(df)), symbols, last_date,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        logger.info("日线分区已写入: %s (%d 行 / %d 标的)", path.name, len(df), symbols)
        return len(df)

    def available_months(self, kind: str = "daily") -> list[str]:
        """已落盘的分区月份列表（升序）"""
        if kind == "factor":
            directory = self.factor_dir
        elif kind == "indicator":
            directory = self.indicator_dir
        else:
            directory = self.daily_dir
        months = sorted(p.stem for p in directory.glob("*.parquet"))
        return months

    def reset(self, kinds: Optional[list[str]] = None) -> dict:
        """清空指定分区的数据与 manifest（破坏性操作，供 CLI reset 使用）。

        Args:
            kinds: ["daily","factor","online"] 之一或多个；None=全部

        Returns:
            dict: 各类型清理的文件数
        """
        kinds = kinds or ["daily", "factor", "online"]
        result = {}
        table_map = {"daily": "daily_manifest", "factor": "factor_manifest"}
        dir_map = {"daily": self.daily_dir, "factor": self.factor_dir}
        for k in kinds:
            removed = 0
            if k in dir_map:
                for p in dir_map[k].glob("*.parquet"):
                    p.unlink()
                    removed += 1
                with self._conn() as c:
                    c.execute(f"DELETE FROM {table_map[k]}")
            elif k == "online":
                for d in self.online_dir.iterdir():
                    if d.is_dir():
                        for p in d.glob("*.csv"):
                            p.unlink()
                            removed += 1
                        d.rmdir()
            result[k] = removed
        logger.warning("仓库数据已重置: %s", result)
        return result

    # ── 在线快照 ───────────────────────────────────────

    def write_online_snapshot(self, day: str, df) -> Path:
        """写观察池盘中快照（按日目录，文件名带时间戳）。返回文件路径。"""
        day_dir = self.online_dir / day
        day_dir.mkdir(parents=True, exist_ok=True)
        import time
        path = day_dir / f"snapshot_{time.strftime('%H%M%S')}.csv"
        try:
            df.to_csv(path, index=False, encoding="utf-8-sig")
        except Exception as e:
            logger.error("写在线快照失败: %s", e)
            raise
        logger.info("在线快照已写入: %s (%d 行)", path, len(df))
        return path

    def online_snapshots(self, day: str) -> list[Path]:
        day_dir = self.online_dir / day
        return sorted(day_dir.glob("snapshot_*.csv")) if day_dir.exists() else []

    # 分钟数据由 warehouse.minute.MinuteStore 管理，单独分区，避免和 daily
    # 的全量天级数据生命周期混在一起。
    def minute_store(self):
        from StockInvestmentTool.warehouse.minute import MinuteStore
        return MinuteStore(self.base_dir)


def _has(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False
