"""Notification deduplication must commit only after successful delivery."""

from __future__ import annotations

from StockInvestmentTool.web import scheduler


def test_filter_does_not_persist_before_delivery(tmp_path, monkeypatch):
    state_path = tmp_path / "notify_state.json"
    monkeypatch.setattr(scheduler, "_NOTIFY_STATE", str(state_path))
    data = {
        "positions": [{"id": 1, "advice": {"is_actionable": True, "advice_type": "sell_all"}}],
        "summary": {},
    }
    dedup = scheduler._filter_unnotified(data)

    assert dedup["pending_keys"] == ["1:sell_all"]
    assert not state_path.exists()


def test_dedup_commits_after_success(tmp_path, monkeypatch):
    state_path = tmp_path / "notify_state.json"
    monkeypatch.setattr(scheduler, "_NOTIFY_STATE", str(state_path))
    dedup = {"state": {}, "pending_keys": ["1:sell_all"], "now": "2026-08-26T15:00:00"}

    scheduler._commit_notify_dedup(dedup)

    assert scheduler._load_notify_state()["1:sell_all"] == "2026-08-26T15:00:00"
