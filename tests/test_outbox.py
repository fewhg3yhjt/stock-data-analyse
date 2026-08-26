"""Durable notification outbox tests."""

from __future__ import annotations

from StockInvestmentTool.notifier.outbox import NotificationOutbox


def test_outbox_survives_reopen_and_marks_sent(tmp_path):
    path = tmp_path / "outbox.db"
    first = NotificationOutbox(path)
    item_id = first.enqueue("feishu", {"sections": [], "meta": {}})

    second = NotificationOutbox(path)
    due = second.due()
    assert due[0]["id"] == item_id
    assert second.pending_count() == 1
    second.mark_sent(item_id)
    assert second.pending_count() == 0


def test_outbox_failed_delivery_is_retryable(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    item_id = outbox.enqueue("feishu", {"sections": [], "meta": {}})
    item = outbox.due()[0]
    outbox.mark_failed(item_id, item["attempts"], "provider unavailable")

    reopened = NotificationOutbox(tmp_path / "outbox.db")
    assert reopened.pending_count() == 1


def test_outbox_moves_repeated_failures_to_dead_letter(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    item_id = outbox.enqueue("feishu", {"sections": [], "meta": {}})

    for attempts in range(5):
        outbox.mark_failed(item_id, attempts, "provider unavailable")

    assert outbox.counts()["dead"] == 1
    assert outbox.pending_count() == 0
