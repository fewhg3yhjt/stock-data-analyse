# -*- coding: utf-8 -*-
"""业务平面 Worker 入口。

独立于数据任务运行。默认单次执行，持续模式需显式传入 poll_interval。
启动时先回收业务库中的 stale JobRun，再领取 requested JobRun。
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time

from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.task_registry import register_business_tasks
from StockInvestmentTool.biz.tasks import BusinessTaskService
from StockInvestmentTool.biz.models import now_utc


def _worker_id() -> str:
    return os.getenv("BUSINESS_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def _heartbeat(service: BusinessTaskService) -> None:
    service.repo.db.upsert("business_worker_heartbeats", {
        "worker_id": _worker_id(), "heartbeat_at": now_utc(),
        "process_id": os.getpid(), "host": socket.gethostname(),
    }, "worker_id")


def _heartbeat_loop(stop: threading.Event, interval: float) -> None:
    service = BusinessTaskService(BusinessRepository())
    while not stop.wait(interval):
        try:
            _heartbeat(service)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("业务 Worker 心跳写入失败")


def run_once() -> dict | None:
    service = BusinessTaskService(BusinessRepository())
    register_business_tasks(service)
    _heartbeat(service)
    recovered = service.recover_stale_runs()
    run = service.run_next()
    _heartbeat(service)
    return {"recovered": recovered, "run": run.__dict__ if run else None}


def run_loop(poll_interval: float) -> None:
    stop_heartbeat = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(stop_heartbeat, max(1.0, float(os.getenv("BUSINESS_WORKER_HEARTBEAT_INTERVAL", "10")))),
        daemon=True,
    )
    heartbeat_thread.start()
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("业务 Worker 轮询失败")
        time.sleep(poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="StockInvestmentTool business worker")
    parser.add_argument("--poll-interval", type=float, default=0,
                        help="持续轮询间隔秒数；0 表示只执行一次")
    args = parser.parse_args()
    if args.poll_interval > 0:
        run_loop(args.poll_interval)
    else:
        print(run_once())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
