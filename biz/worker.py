# -*- coding: utf-8 -*-
"""业务平面 Worker 入口。

独立于数据任务运行。默认单次执行，持续模式需显式传入 poll_interval。
启动时先回收业务库中的 stale JobRun，再领取 requested JobRun。
"""

from __future__ import annotations

import argparse
import time

from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.task_registry import register_business_tasks
from StockInvestmentTool.biz.tasks import BusinessTaskService


def run_once() -> dict | None:
    service = BusinessTaskService(BusinessRepository())
    register_business_tasks(service)
    recovered = service.recover_stale_runs()
    run = service.run_next()
    return {"recovered": recovered, "run": run.__dict__ if run else None}


def run_loop(poll_interval: float) -> None:
    while True:
        run_once()
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
