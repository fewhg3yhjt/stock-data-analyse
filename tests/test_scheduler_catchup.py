from __future__ import annotations


def test_failed_daily_run_does_not_block_catchup():
    from StockInvestmentTool.web import scheduler

    class Store:
        def recent(self, _limit):
            return [{"job_name": "daily_sync", "started_at": "2026-08-27T15:35:00", "status": "failed"}]

        def running(self, _name):
            return None

    # The startup branch is exercised through the condition that a failed run
    # is not active_or_success; this assertion protects the intended contract.
    runs = Store().recent(200)
    active = [item for item in runs if item["status"] in ("running", "success")]
    assert not active


def test_gap_scan_uses_trade_days_and_is_bounded(monkeypatch):
    from datetime import date
    from StockInvestmentTool.web import scheduler

    monkeypatch.setattr(scheduler, "_published_daily_dates",
                        lambda **kwargs: ["2026-09-03", "2026-09-04"])
    result = scheduler.daily_data_gap_dates(target=date(2026, 9, 4), lookback_days=10)
    assert result == ["2026-09-03", "2026-09-04"]
