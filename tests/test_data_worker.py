from pathlib import Path

from ops.data_worker import DataWorker
from ops.task_center import TaskCenter


def _request(tmp_path: Path, task_key="stock_daily_build"):
    db = tmp_path / "management.db"
    center = TaskCenter(db, db)
    center.sync_definitions()
    request_id = center.create_request(
        task_key, "manual", period_start="2026-09-04", period_end="2026-09-04",
        symbols=["sh600000"], requested_by="test",
    )
    return db, center, request_id


def test_data_worker_claim_is_atomic_and_allowlisted(tmp_path):
    db, center, request_id = _request(tmp_path)
    first = DataWorker(db, task_keys={"stock_daily_build"})
    second = DataWorker(db, task_keys={"stock_daily_build"})

    claimed = first.claim_next()
    assert claimed["request_id"] == request_id
    assert second.claim_next() is None
    assert center.request(request_id)["status"] == "running"


def test_data_worker_records_heartbeat_and_timeout_result(tmp_path, monkeypatch):
    db, center, request_id = _request(tmp_path)
    worker = DataWorker(db, task_keys={"stock_daily_build"}, task_timeout=7)
    seen = {}

    def fake_execute(db_path, task_key, payload, request_id=None):
        seen.update({"db": db_path, "task": task_key, "timeout": payload["task_timeout"], "request": request_id})
        center.update_request(request_id, "timeout")
        return {"run_id": 9, "request_id": request_id, "status": "timeout", "result": {"reason": "deadline"}}

    monkeypatch.setattr("ops.data_worker.execute_task", fake_execute)
    result = worker.process_once()

    assert result["status"] == "timeout"
    assert seen == {"db": db, "task": "stock_daily_build", "timeout": 7, "request": request_id}
    assert center.request(request_id)["status"] == "timeout"
    with worker.center._connect() as conn:
        row = conn.execute("SELECT status,current_request_id FROM data_worker_heartbeats WHERE worker_id=?", (worker.worker_id,)).fetchone()
    assert row["status"] == "idle"
    assert row["current_request_id"] == ""


def test_data_worker_failure_closes_claimed_request(tmp_path, monkeypatch):
    db, center, request_id = _request(tmp_path)
    worker = DataWorker(db, task_keys={"stock_daily_build"})

    def fail(*_args, **_kwargs):
        raise RuntimeError("worker failure")

    monkeypatch.setattr("ops.data_worker.execute_task", fail)
    result = worker.process_once()

    assert result["status"] == "failed"
    assert center.request(request_id)["status"] == "failed"


def test_data_worker_does_not_claim_non_data_task(tmp_path):
    db = tmp_path / "management.db"
    center = TaskCenter(db, db)
    center.sync_definitions()
    request_id = center.create_request(
        "stock_daily_build", "manual", period_start="2026-09-04", period_end="2026-09-04",
        symbols=["sh600000"], requested_by="test",
    )
    with center._connect() as conn:
        conn.execute("UPDATE task_definitions SET task_type='BUSINESS_TASK' WHERE task_key='stock_daily_build'")
    worker = DataWorker(db, task_keys={"stock_daily_build"})

    assert worker.claim_next() is None
    assert center.request(request_id)["status"] == "requested"
