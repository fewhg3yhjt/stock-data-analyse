"""Durable notification outbox tests."""

from __future__ import annotations

from StockInvestmentTool.notifier.outbox import NotificationOutbox


def test_outbox_survives_reopen_and_marks_sent(tmp_path):
    path = tmp_path / "outbox.db"
    first = NotificationOutbox(path)
    item_id = first.enqueue("feishu", {"sections": [], "meta": {}})

    second = NotificationOutbox(path)
    due = second.claim_due()
    assert due[0]["id"] == item_id
    assert second.pending_count() == 0
    assert second.counts().get("processing", 0) == 1
    second.mark_sent(item_id, "default")
    assert second.pending_count() == 0


def test_outbox_failed_delivery_is_retryable(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    item_id = outbox.enqueue("feishu", {"sections": [], "meta": {}})
    item = outbox.claim_due()[0]
    outbox.mark_failed(item_id, item["attempts"], "provider unavailable", "default")

    reopened = NotificationOutbox(tmp_path / "outbox.db")
    assert reopened.pending_count() == 1


def test_outbox_moves_repeated_failures_to_dead_letter(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    item_id = outbox.enqueue("feishu", {"sections": [], "meta": {}})

    for attempts in range(5):
        outbox.mark_failed(item_id, attempts, "provider unavailable")

    assert outbox.counts()["dead"] == 1
    assert outbox.pending_count() == 0


def test_outbox_claim_is_atomic_no_double_claim(tmp_path):
    """两个 worker 领取同一批：只有一个拿到（工作项 1/2）。"""
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    outbox.enqueue("feishu", {"sections": [], "meta": {}})

    first = outbox.claim_due(worker_id="worker-a")
    second = outbox.claim_due(worker_id="worker-b")
    assert len(first) == 1
    assert len(second) == 0
    assert outbox.counts().get("processing", 0) == 1


def test_outbox_lease_expiry_allows_reclaim(tmp_path):
    """租约过期后其他 worker 可重新领取。"""
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    item_id = outbox.enqueue("feishu", {"sections": [], "meta": {}})
    outbox.claim_due(worker_id="worker-a", lease_seconds=1)

    import time
    time.sleep(1.1)
    second = outbox.claim_due(worker_id="worker-b")
    assert len(second) == 1
    assert second[0]["id"] == item_id


def test_outbox_renew_and_release(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.db")
    item_id = outbox.enqueue("feishu", {"sections": [], "meta": {}})
    outbox.claim_due(worker_id="worker-a")

    assert outbox.renew_lease(item_id, "worker-a") is True
    assert outbox.renew_lease(item_id, "worker-b") is False
    assert outbox.release_lease(item_id, "worker-b") is False
    assert outbox.release_lease(item_id, "worker-a") is True
    assert outbox.pending_count() == 1
