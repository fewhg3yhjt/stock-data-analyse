"""FR-2 策略编排器 API 测试。"""

from __future__ import annotations

import pytest


@pytest.fixture
def client():
    from StockInvestmentTool.web.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["admin"] = True
    return c


def _sample_model() -> dict:
    return {
        "name": "test_composer",
        "version": "1.0",
        "description": "composer 测试",
        "applicable_types": ["A", "B"],
        "buy_rules": [{
            "type": "support_level",
            "params": {
                "support_sources": ["MA60", "MIN(MA20,MA240)"],
                "buy_stages": [
                    {"label": "弱支撑", "position_index": 1, "ratio": 0.3},
                    {"label": "强支撑", "position_index": 0, "ratio": 0.4},
                    {"label": "极端", "use_special": "dividend_anchor_4pct", "ratio": 0.3},
                ],
            },
        }],
        "sell_rules": [{
            "type": "hard_stop",
            "params": {"stop_loss_by_type": {"A": 0.15, "B": 0.15, "C": 0.15, "D": 0.10}},
        }],
        "risk": {"drawdown_stop": 0.08, "min_profit_for_dd": 0.06,
                 "technical_stop_enabled": True, "stop_loss_by_type": {"A": 0.15}},
        "backtest": {"initial_cash": 100000, "optimize": True,
                     "grid_search": {"trail_thresholds": [0.03, 0.05],
                                     "buy_offsets": [-0.02, 0.0, 0.02]}},
    }


def test_api_indicators(client):
    r = client.get("/api/indicators")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "success"
    for g in ("base", "composite", "code"):
        assert g in d["groups"]
    assert d["count"] > 0


def test_api_rules_schema(client):
    r = client.get("/api/rules/schema")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "success"
    assert any(x["kind"] == "buy" for x in d["rules"])
    assert any(x["kind"] == "sell" for x in d["rules"])
    trend = next(x for x in d["rules"] if x["type"] == "trend_following")
    assert any(field["key"] == "conditions" for field in trend["schema"])


def test_compose_roundtrip(client):
    model = _sample_model()
    r = client.post("/api/schemes/compose", json=model)
    assert r.status_code == 200
    yaml_out = r.get_json()["yaml"]
    assert "test_composer" in yaml_out
    # 解析回模型
    r2 = client.post("/api/schemes/parse", json={"content": yaml_out})
    assert r2.status_code == 200
    m2 = r2.get_json()["model"]
    assert m2["name"] == "test_composer"
    assert m2["buy_rules"][0]["type"] == "support_level"


def test_compose_missing_name_raises(client):
    r = client.post("/api/schemes/compose", json={"description": "no name"})
    assert r.status_code == 400


def test_breakeven_threshold_must_keep_formula_denominator_positive(client):
    model = _sample_model()
    model["sell_rules"][0]["params"] = {
        "mode": "breakeven",
        "breakeven_activation_by_type": {"B": 1.0},
    }
    response = client.post("/api/schemes/compose", json=model)
    assert response.status_code == 400


def test_save_list_toggle_cleanup(client):
    model = _sample_model()
    yaml_out = client.post("/api/schemes/compose", json=model).get_json()["yaml"]
    # save
    r = client.post("/api/schemes/save", json={"name": "test_composer", "content": yaml_out})
    assert r.status_code == 200
    # list includes it
    r = client.get("/api/schemes/list")
    names = [s["name"] for s in r.get_json()["schemes"]]
    assert "test_composer" in names
    # toggle off
    r = client.post("/api/schemes/toggle", json={"name": "test_composer", "enabled": False})
    assert r.status_code == 200
    # 列表仍可见但标记停用（管理页语义）
    entries = client.get("/api/schemes/list").get_json()["schemes"]
    tc = next(s for s in entries if s["name"] == "test_composer")
    assert tc["enabled"] is False
    # versions recorded
    r = client.get("/api/schemes/versions?name=test_composer")
    assert len(r.get_json()["versions"]) >= 1
    # delete
    r = client.post("/api/schemes/delete", json={"name": "test_composer"})
    assert r.status_code == 200


def test_builtin_scheme_delete_protected(client):
    # 内置方案删除应报错（受保护）
    r = client.post("/api/schemes/delete", json={"name": "default_value"})
    assert r.status_code == 400


def test_builtin_scheme_toggle_and_default_protected(client):
    assert client.post("/api/schemes/toggle", json={"name": "default_value", "enabled": False}).status_code == 400
    assert client.post("/api/schemes/default", json={"name": "default_value"}).status_code == 400


def test_scheme_list_exposes_indicator_bindings(client):
    response = client.get("/api/schemes/list")
    assert response.status_code == 200
    scheme = next(item for item in response.get_json()["schemes"]
                  if item["name"] == "right_aggressive_growth")
    support = next(rule for rule in scheme["buy_rules"] if rule["type"] == "support_level")
    keys = {item["key"] for item in support["indicator_refs"]}
    assert {"ma60", "low_3m", "year_low", "dividend_anchor_4pct"} <= keys


def test_validate_run_returns_backtest_result(client):
    model = _sample_model()
    yaml_out = client.post("/api/schemes/compose", json=model).get_json()["yaml"]
    r = client.post("/api/schemes/validate-run", json={"content": yaml_out, "code": "sh.600900"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "success"
    assert isinstance(data["trades"], int)
    assert "total_return" in data
    assert "max_drawdown" in data
    assert data["equity_points"] > 0
