from pathlib import Path

from warehouse.coverage import CoverageStore


def test_coverage_store_tracks_latest_and_date_failures(tmp_path):
    store = CoverageStore(tmp_path / "management.db")
    store.record_success(
        dataset_name="stock_daily", source_name="tencent", entity_type="stock",
        entity_id="sh600000", data_dates=["2026-09-05", "2026-09-07"], batch_id="b1",
    )
    store.record_failure(
        dataset_name="stock_daily", source_name="tencent", entity_type="stock",
        entity_id="sh600001", data_date="2026-09-07", batch_id="b2", status="timeout",
        error_code="timeout",
    )

    assert store.latest_success_dates("stock_daily", "tencent", ["sh600000"], "stock") == {
        "sh600000": "2026-09-07",
    }
    with store._connect() as conn:
        row = conn.execute(
            "SELECT status,error_code FROM dataset_entity_date_status WHERE entity_id='sh600001'"
        ).fetchone()
    assert tuple(row) == ("timeout", "timeout")


def test_coverage_separates_stock_and_etf(tmp_path):
    store = CoverageStore(Path(tmp_path) / "management.db")
    store.record_success(dataset_name="stock_daily", source_name="tencent",
                         entity_type="stock", entity_id="sh600000",
                         data_dates=["2026-09-07"])
    store.record_success(dataset_name="stock_daily", source_name="tencent",
                         entity_type="etf", entity_id="sh510300",
                         data_dates=["2026-09-07"])

    assert store.latest_success_dates("stock_daily", "tencent", ["sh600000", "sh510300"], "stock") == {"sh600000": "2026-09-07"}
    assert store.latest_success_dates("stock_daily", "tencent", ["sh600000", "sh510300"], "etf") == {"sh510300": "2026-09-07"}
