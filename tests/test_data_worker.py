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


def test_data_worker_queues_next_daily_stage_only_after_success(tmp_path):
    db, center, request_id = _request(tmp_path, task_key="stock_daily_capture")
    worker = DataWorker(db, task_keys={"stock_daily_build"})
    request = center.request(request_id)
    result = worker.enqueue_downstream(request, {"status": "success", "result": {"source_batch_id": "batch-1"}})

    assert result[0]["task_key"] == "stock_daily_build"
    child = center.request(result[0]["request_id"])
    assert child["request_payload"]["input_batch_id"] == "batch-1"


def test_data_worker_does_not_queue_after_failed_stage(tmp_path):
    db, center, request_id = _request(tmp_path, task_key="stock_daily_capture")
    worker = DataWorker(db, task_keys={"stock_daily_build"})
    request = center.request(request_id)

    assert worker.enqueue_downstream(request, {"status": "failed"}) == []


def test_data_worker_default_allowlist_covers_daily_chain():
    from ops.data_worker import DEFAULT_DATA_TASKS

    assert DEFAULT_DATA_TASKS[:5] == (
        "stock_daily_capture", "stock_daily_build", "stock_daily_quality",
        "stock_daily_publish", "indicators_build",
    )


def test_data_worker_batches_stock_and_etf_separately(tmp_path):
    db = tmp_path / "management.db"
    center = TaskCenter(db, db)
    center.sync_definitions()
    from StockInvestmentTool.warehouse.storage import Warehouse
    warehouse = Warehouse(base_dir=tmp_path / "warehouse", meta_db_path=db)
    warehouse.upsert_instruments([
        {"code": "sh600000", "type": "stock"},
        {"code": "sh600001", "type": "stock"},
        {"code": "sh510300", "type": "etf"},
    ])
    worker = DataWorker(db, task_keys={"stock_daily_capture"}, batch_size=1)

    groups = worker._batch_symbols(["sh600000", "sh600001", "sh510300"])
    assert groups == [
        ("stock", ["sh600000"]), ("stock", ["sh600001"]),
        ("etf", ["sh510300"]),
    ]


def test_data_worker_resume_uses_collector_skip_for_completed_symbols(tmp_path, monkeypatch):
    db = tmp_path / "management.db"
    center = TaskCenter(db, db)
    center.sync_definitions()
    from StockInvestmentTool.warehouse.storage import Warehouse
    warehouse = Warehouse(base_dir=tmp_path / "warehouse", meta_db_path=db)
    warehouse.upsert_instruments([
        {"code": "sh600000", "type": "stock"},
        {"code": "sh600001", "type": "stock"},
    ])
    worker = DataWorker(db, task_keys={"stock_daily_capture"}, batch_size=2)
    request_id = center.create_request(
        "stock_daily_capture", "retry", period_start="2026-09-07", period_end="2026-09-07",
        symbols=["sh600000", "sh600001"], requested_by="test",
    )
    called = []

    def fake_execute(_db, _task, payload, request_id=None):
        called.append((payload["symbols"], payload["batch_index"], request_id))
        return {"status": "success", "result": {"source_batch_id": f"batch-{payload['batch_index']}"}}

    monkeypatch.setattr("ops.data_worker.execute_task", fake_execute)
    result = worker.process_once()

    assert result["status"] == "success"
    assert called == [(["sh600000", "sh600001"], 1, called[0][2])]


def test_data_worker_persists_type_specific_child_requests(tmp_path, monkeypatch):
    db = tmp_path / "management.db"
    center = TaskCenter(db, db)
    center.sync_definitions()
    from StockInvestmentTool.warehouse.storage import Warehouse
    warehouse = Warehouse(base_dir=tmp_path / "warehouse", meta_db_path=db)
    warehouse.upsert_instruments([
        {"code": "sh600000", "type": "stock"},
        {"code": "sh510300", "type": "etf"},
    ])
    worker = DataWorker(db, task_keys={"stock_daily_capture"}, batch_size=50)
    request_id = center.create_request(
        "stock_daily_capture", "retry", period_start="2026-09-07", period_end="2026-09-07",
        symbols=["sh600000", "sh510300"], requested_by="test",
    )

    monkeypatch.setattr(
        "ops.data_worker.execute_task",
        lambda _db, _task, payload, request_id=None: {
            "status": "success", "request_id": request_id,
            "result": {"source_batch_id": f"batch-{payload['asset_types'][0]}"},
        },
    )
    result = worker.process_once()

    assert result["status"] == "success"
    children = center._connect().execute(
        "SELECT task_key,symbols,request_payload FROM task_execution_requests "
        "WHERE request_id<>? AND task_key='stock_daily_capture' ORDER BY created_at", (request_id,)
    ).fetchall()
    assert len(children) == 2
    assert [__import__("json").loads(row[1]) for row in children] == [["sh600000"], ["sh510300"]]
    payloads = [__import__("json").loads(row[2]) for row in children]
    assert [payload["asset_types"] for payload in payloads] == [["stock"], ["etf"]]
