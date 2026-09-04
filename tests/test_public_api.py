"""Contract tests for the unauthenticated public market-data API."""

import pandas as pd
import pytest

from StockInvestmentTool.warehouse.datasets import DatasetResult


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))
    monkeypatch.setenv("STOCK_DISABLE_AUTH", "1")
    from StockInvestmentTool.web.app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    return app


def test_public_routes_are_registered_and_allow_cross_origin(app):
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/public/reports" in routes
    assert "/api/public/stock-sectors" in routes
    assert "/public-api" in routes
    assert "/public-api/health" in routes
    assert "/public-api/stock/daily" in routes
    response = app.test_client().get("/api/public/openapi.json")
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.get_json()["openapi"] == "3.0.3"


def test_public_api_index_and_docs_are_available_without_login(app):
    response = app.test_client().get("/public-api")
    assert response.status_code == 200
    assert response.is_json
    assert response.get_json()["service"] == "stock-public-api"
    response = app.test_client().get("/public-api/docs")
    assert response.status_code == 200
    assert "在线构造并查询" in response.get_data(as_text=True)


def test_public_api_health_is_direct_json(app):
    response = app.test_client().get("/public-api/health")
    assert response.status_code == 200
    assert response.content_type.startswith("application/json")
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.get_json() == {"status": "ok", "service": "stock-public-api"}


def test_daily_report_requires_explicit_dates(app):
    response = app.test_client().get("/api/public/stock-daily")
    assert response.status_code == 400
    assert "start 和 end" in response.get_json()["error"]


def test_stock_daily_returns_published_records_with_pagination(app, monkeypatch):
    data = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01", "2026-09-02"]),
        "code": ["sh600000", "sh600000"], "close": [10.1, 10.2],
        "volume": [100, 200],
    })
    monkeypatch.setattr(
        "StockInvestmentTool.web.public_api.DatasetAccess.load_dataset",
        lambda *args, **kwargs: DatasetResult(data, {"data_as_of": "2026-09-02", "quality_status": "PASS", "source": "published_dataset", "partition_versions": {"2026-09": "v1"}}),
    )
    response = app.test_client().get(
        "/api/public/stock-daily?symbol=sh.600000&start=2026-09-01&end=2026-09-02&page_size=1"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["report"] == "stock_daily"
    assert payload["total"] == 2
    assert payload["has_next"] is True
    assert payload["data"][0]["date"].startswith("2026-09-01")
    assert payload["meta"]["quality_status"] == "PASS"


def test_compat_stock_daily_accepts_six_digit_and_exchange_codes(app, monkeypatch):
    data = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01", "2026-09-02"]),
        "code": ["sz000400", "sz000400"], "open": [21.0, 21.1],
        "high": [21.5, 21.6], "low": [20.8, 20.9], "close": [21.3, 21.4],
        "volume": [100, 200], "amount": [2100.0, 4300.0], "turn": [1.1, 1.2],
    })
    monkeypatch.setattr(
        "StockInvestmentTool.web.public_api.DatasetAccess.load_dataset",
        lambda *args, **kwargs: DatasetResult(data, {"data_as_of": "2026-09-02", "quality_status": "PASS", "source": "published_dataset", "partition_versions": {"2026-09": "v1"}}),
    )
    monkeypatch.setattr(
        "StockInvestmentTool.web.public_api.Warehouse.get_instrument",
        lambda *args, **kwargs: {"name": "许继电气"},
    )
    response = app.test_client().get(
        "/public-api/stock/daily?code=000400.SZ&start=2026-09-01&end=2026-09-02"
    )
    assert response.status_code == 200
    assert response.content_type.startswith("application/json")
    payload = response.get_json()
    assert payload["code"] == "000400"
    assert payload["name"] == "许继电气"
    assert payload["adjust"] == "qfq"
    assert payload["count"] == 2
    assert payload["data"][0]["turnover_rate"] == 1.1


def test_compat_stock_daily_rejects_unpublished_adjustment(app):
    response = app.test_client().get(
        "/public-api/stock/daily?code=000400&start=2026-09-01&end=2026-09-02&adjust=hfq"
    )
    assert response.status_code == 422


def test_stock_sector_relationships_keep_classification_and_source(app, monkeypatch):
    frame = pd.DataFrame({
        "snapshot_date": ["2026-09-03"], "code": ["sh600000"],
        "industry_code": ["I64"], "industry_name": ["互联网和相关服务"],
        "industry_classification": ["csrc"], "source": ["baostock"],
        "captured_at": ["2026-09-03T12:00:00"],
    })
    class Access:
        def __init__(self, warehouse):
            pass
        def get_current_version(self, dataset_name):
            return {"2026-09-03": {"version_id": "v1"}} if dataset_name == "industry_membership" else {}
        def load_dataset(self, dataset_name, **kwargs):
            if dataset_name == "industry_membership":
                return DatasetResult(frame, {"data_as_of": "2026-09-03", "partition_versions": {"2026-09-03": "v1"}})
            raise __import__("StockInvestmentTool.warehouse.datasets", fromlist=["DatasetAccessError"]).DatasetAccessError("missing")
    monkeypatch.setattr("StockInvestmentTool.web.public_api.DatasetAccess", Access)
    response = app.test_client().get("/api/public/stock-sectors?symbol=sh600000&as_of=2026-09-03")
    assert response.status_code == 200
    row = response.get_json()["data"][0]
    assert row["sector_id"] == "I64"
    assert row["classification"] == "csrc"
    assert row["source"] == "baostock"


def test_stock_sector_relationships_support_ths_membership_without_classification_column(app, monkeypatch):
    frame = pd.DataFrame({
        "snapshot_date": ["2026-09-03"], "industry_id": ["881101"],
        "industry_name": ["种植业与林业"], "code": ["sz000998"],
        "stock_name": ["隆平高科"], "source": ["github"],
        "captured_at": ["2026-09-03T12:00:00"],
    })
    class Access:
        def __init__(self, warehouse):
            pass
        def get_current_version(self, dataset_name):
            return {"2026-09-03": {"version_id": "v1"}} if dataset_name == "ths_industry_membership" else {}
        def load_dataset(self, dataset_name, **kwargs):
            if dataset_name == "ths_industry_membership":
                return DatasetResult(frame, {"data_as_of": "2026-09-03", "partition_versions": {"2026-09-03": "v1"}})
            raise __import__("StockInvestmentTool.warehouse.datasets", fromlist=["DatasetAccessError"]).DatasetAccessError("missing")
    monkeypatch.setattr("StockInvestmentTool.web.public_api.DatasetAccess", Access)
    response = app.test_client().get("/api/public/stock-sectors?symbol=sz000998")
    assert response.status_code == 200
    row = response.get_json()["data"][0]
    assert row["sector_id"] == "881101"
    assert row["classification"] == "ths_industry"
