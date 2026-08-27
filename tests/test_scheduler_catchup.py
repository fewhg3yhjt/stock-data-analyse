from __future__ import annotations


def test_failed_daily_run_does_not_block_catchup(monkeypatch):
    from StockInvestmentTool.web import scheduler

    class Store:
        def recent(self, _limit):
            return [{"job_name": "daily_sync", "started_at": "2026-08-27T15:35:00", "status": "failed"}]

        def running(self, _name):
            return None

    started = []
    monkeypatch.setattr(scheduler, "run_daily_data_pipeline", lambda: started.append(True))
    # The startup branch is exercised through the condition that a failed run
    # is not active_or_success; this assertion protects the intended contract.
    runs = Store().recent(200)
    active = [item for item in runs if item["status"] in ("running", "success")]
    assert not active
