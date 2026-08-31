# -*- coding: utf-8 -*-

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_DB_PATH", str(tmp_path / "business.db"))
    monkeypatch.setenv("STOCK_DISABLE_AUTH", "1")
    from StockInvestmentTool.web.app import create_app
    return create_app().test_client()


def test_research_detail_page_has_structured_sections(client):
    response = client.get("/research/detail?symbol=sh600908")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for text in ("个股研究", "技术面", "市场面", "估值面", "基本面", "策略决策", "研究证据"):
        assert text in body


def test_research_detail_page_supports_run_context(client):
    response = client.get("/research/detail?run_id=rr_123&symbol=sh600908")
    assert response.status_code == 200
    assert "RESEARCH_PREFILL_RUN_ID" in response.get_data(as_text=True)
