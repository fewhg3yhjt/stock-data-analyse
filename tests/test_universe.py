from pathlib import Path

from warehouse.universe import UniverseStore


def test_universe_authoritative_snapshot_and_history_fallback(tmp_path):
    store = UniverseStore(tmp_path / "management.db")
    items = [{"code": "sh600000", "type": "stock", "tradeStatus": "1"}]
    result = store.resolve(
        snapshot_date="2026-09-07", fetch_full=lambda _day: items,
        entity_types={"stock"}, fallback_to_catalog=[],
    )
    assert result["authoritative"] is True
    assert result["items"][0]["entity_id"] == "sh600000"
    assert store.active_codes(snapshot_date="2026-09-07", entity_types={"stock"}) == ["sh600000"]

    fallback = store.resolve(
        snapshot_date="2026-09-08", fetch_full=lambda _day: (_ for _ in ()).throw(RuntimeError("source down")),
        entity_types={"stock"}, fallback_to_catalog=[],
    )
    assert fallback["source"] == "historical_snapshot"
    assert fallback["authoritative"] is False


def test_universe_empty_authoritative_result_falls_back_without_retiring(tmp_path):
    store = UniverseStore(tmp_path / "management.db")
    catalog = [{"code": "sh600000", "type": "stock"}]
    result = store.resolve(
        snapshot_date="2026-09-08", fetch_full=lambda _day: [],
        entity_types={"stock"}, fallback_to_catalog=catalog,
    )
    assert result["source"] == "instrument_catalog"
    assert result["authoritative"] is False
    assert result["complete"] is False


def test_universe_snapshot_keeps_trade_status_and_catalog_entry(tmp_path):
    from StockInvestmentTool.warehouse.storage import Warehouse

    warehouse = Warehouse(tmp_path / "warehouse", meta_db_path=tmp_path / "management.db")
    store = UniverseStore(warehouse.meta_db_path)
    result = store.record_snapshot(
        "2026-09-09",
        [{"code": "sh600000", "type": "stock", "name": "浦发银行", "tradeStatus": "0"}],
        source="baostock", authoritative=True, complete=True,
    )
    warehouse.upsert_instruments([{
        "code": "sh600000", "type": "stock", "name": "浦发银行",
        "trade_status": "0", "universe_status": "suspended",
        "first_seen_date": "2026-09-09", "last_seen_date": "2026-09-09",
        "last_source": "baostock",
    }])

    assert result["authoritative"] is True
    instrument = warehouse.get_instrument("sh600000")
    assert instrument["trade_status"] == "0"
    assert instrument["universe_status"] == "suspended"
    assert store.active_codes(snapshot_date="2026-09-09", entity_types={"stock"}) == []


def test_authoritative_reconcile_marks_omitted_catalog_entity_candidate(tmp_path):
    from StockInvestmentTool.warehouse.storage import Warehouse

    db = Path(tmp_path) / "management.db"
    warehouse = Warehouse(Path(tmp_path) / "warehouse", meta_db_path=db)
    warehouse.upsert_instruments([
        {"code": "sh600000", "type": "stock", "universe_status": "active"},
        {"code": "sh600001", "type": "stock", "universe_status": "active"},
    ])
    store = UniverseStore(db)
    store.reconcile_authoritative_snapshot(
        "2026-09-10", [{"code": "sh600000", "type": "stock", "tradeStatus": "1"}],
    )

    assert warehouse.get_instrument("sh600001")["universe_status"] == "inactive_candidate"


def test_historical_fallback_does_not_retire_omitted_entity(tmp_path):
    from StockInvestmentTool.warehouse.storage import Warehouse

    db = Path(tmp_path) / "management.db"
    warehouse = Warehouse(Path(tmp_path) / "warehouse", meta_db_path=db)
    warehouse.upsert_instruments([
        {"code": "sh600000", "type": "stock", "universe_status": "active"},
        {"code": "sh600001", "type": "stock", "universe_status": "active"},
    ])
    store = UniverseStore(db)
    store.record_snapshot(
        "2026-09-09", [{"code": "sh600000", "type": "stock", "tradeStatus": "1"}],
        source="authoritative", authoritative=True, complete=True,
    )
    result = store.resolve(
        snapshot_date="2026-09-10",
        fetch_full=lambda _day: (_ for _ in ()).throw(RuntimeError("source down")),
        entity_types={"stock"}, fallback_to_catalog=[
            {"code": "sh600000", "type": "stock"}, {"code": "sh600001", "type": "stock"},
        ],
    )

    assert result["authoritative"] is False
    assert warehouse.get_instrument("sh600001")["universe_status"] == "active"
