"""指标中心：表达式求值、预览 API 与页面入口测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from StockInvestmentTool.indicators.engine import IndicatorRegistry


@pytest.fixture
def sample_kline():
    dates = pd.bdate_range("2025-01-02", periods=300)
    close = pd.Series(range(20, 320), dtype=float)
    return pd.DataFrame({
        "date": dates, "open": close - 0.2, "high": close + 0.5,
        "low": close - 0.5, "close": close, "volume": 1000.0,
    })


@pytest.fixture
def client():
    from StockInvestmentTool.web.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    return client


def test_ad_hoc_expression_uses_configured_indicators(sample_kline):
    series = IndicatorRegistry().evaluate_expression(sample_kline, "0.95*MIN(MA20,MA240)")

    assert len(series) == len(sample_kline)
    assert series.iloc[-1] == pytest.approx(0.95 * 199.5)


def test_indicator_preview_api(client, sample_kline, monkeypatch):
    from StockInvestmentTool.datasource.base import WarehouseSource

    monkeypatch.setattr(WarehouseSource, "fetch_daily_series", lambda *_args, **_kwargs: sample_kline)
    response = client.post("/api/indicators/preview", json={
        "code": "sh.600900", "expr": "0.95*MA20",
    })

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert data["latest"] == pytest.approx(0.95 * 309.5)
    assert len(data["series"]) == 180


def test_indicator_preview_rejects_unknown_name(client, sample_kline, monkeypatch):
    from StockInvestmentTool.datasource.base import WarehouseSource

    monkeypatch.setattr(WarehouseSource, "fetch_daily_series", lambda *_args, **_kwargs: sample_kline)
    response = client.post("/api/indicators/preview", json={
        "code": "sh.600900", "expr": "UNKNOWN_INDICATOR",
    })

    assert response.status_code == 400
    assert "UNKNOWN_INDICATOR" in response.get_json()["error"]


def test_indicator_center_page(client):
    response = client.get("/indicator-center")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "指标中心" in html
    assert "/api/indicators/preview" in html
    assert 'href="/indicator-center" class="active"' in html


def test_indicator_api_includes_definition_metadata(client):
    data = client.get("/api/indicators").get_json()
    ma20 = next(item for item in data["groups"]["base"] if item["name"] == "MA20")
    assert ma20["meaning"]
    assert ma20["calculation"]
    assert ma20["data_requirements"]


def test_custom_indicator_store_crud_isolated(tmp_path, monkeypatch):
    from StockInvestmentTool.indicators import store

    monkeypatch.setattr(store, "_root", lambda: tmp_path / "custom")
    saved = store.save_indicator("MyMA17", "base", "MA(close,17)", "自定义 17 日均线")
    assert saved["name"] == "MyMA17"
    assert store.list_indicators()[0]["editable"] is True
    assert store.set_enabled("MyMA17", False)["enabled"] is False
    assert store.list_indicators()[0]["enabled"] is False
    assert store.delete_indicator("MyMA17") is True
    assert store.list_indicators() == []


def test_custom_indicator_cannot_override_builtin(tmp_path, monkeypatch):
    from StockInvestmentTool.indicators import store

    monkeypatch.setattr(store, "_root", lambda: tmp_path / "custom")
    with pytest.raises(ValueError, match="内置指标"):
        store.save_indicator("MA20", "base", "MA(close,20)")


def test_indicator_crud_api(client, tmp_path, monkeypatch):
    from StockInvestmentTool.indicators import store

    monkeypatch.setattr(store, "_root", lambda: tmp_path / "custom")
    response = client.post("/api/indicators/save", json={
        "name": "ApiMA17", "kind": "base", "expr": "MA(close,17)",
        "description": "API 指标",
    })
    assert response.status_code == 200
    assert response.get_json()["indicator"]["editable"] is True

    response = client.post("/api/indicators/toggle", json={
        "name": "ApiMA17", "enabled": False,
    })
    assert response.status_code == 200
    assert response.get_json()["enabled"] is False

    response = client.post("/api/indicators/delete", json={"name": "ApiMA17"})
    assert response.status_code == 200
    assert response.get_json()["deleted"] is True
