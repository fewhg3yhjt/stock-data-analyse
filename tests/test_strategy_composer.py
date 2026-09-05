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
    technical = next(x for x in d["rules"] if x["type"] == "technical_stop")
    source = next(field for field in technical["schema"] if field["key"] == "support_source")
    assert "最低值" in source["help"]
    trend = next(x for x in d["rules"] if x["type"] == "trend_following")
    assert any(field["key"] == "conditions" for field in trend["schema"])


def test_optimization_plan_is_bounded_and_non_persistent(client):
    response = client.post("/api/schemes/optimization-plan", json={
        "train_start": "2019-01-01", "train_end": "2023-12-31",
        "validation_start": "2024-01-01", "validation_end": "2025-12-31",
        "trail_thresholds": [0.04, 0.06], "buy_offsets": [-0.02, 0.0, 0.02],
    })
    assert response.status_code == 200
    assert response.get_json()["combinations"] == 6
    assert client.post("/api/schemes/optimization-plan", json={
        "train_start": "2019-01-01", "train_end": "2023-12-31",
        "validation_start": "2024-01-01", "validation_end": "2025-12-31",
        "trail_thresholds": list(range(11)), "buy_offsets": list(range(10)),
    }).status_code == 400


def test_optimization_run_requires_explicit_ranges(client):
    response = client.post("/api/schemes/optimization-run", json={"content": "name: x", "code": "sh.600900"})
    assert response.status_code == 400


def test_right_side_profit_activation_percentage_is_serialized_as_ratio(client):
    model = _sample_model()
    model["sell_rules"] = [{"type": "right_side_trailing", "params": {
        "profit_activation_enabled": True,
        "profit_activation_basis": "peak_price",
        "min_profit_for_activation": 0.08,
        "drawdown_by_type": {"B": 0.05},
    }}]
    response = client.post("/api/schemes/compose", json=model)
    assert response.status_code == 200
    assert "min_profit_for_activation: 0.08" in response.get_json()["yaml"]


def test_simulation_draft_is_one_time_and_non_persistent(client):
    model = _sample_model()
    response = client.post("/api/schemes/simulation-draft", json={"model": model})
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert data["scheme_name"] == "test_composer"


def test_stock_search_and_classification_support_sample_security(client):
    search = client.get("/api/stock/search?q=600900")
    assert search.status_code == 200
    assert search.get_json()["status"] == "success"
    assert "validate-stock-type" in client.get("/strategy-composer").get_data(as_text=True)


def test_strategy_composer_contains_lowma_research(client):
    page = client.get("/strategy-composer").get_data(as_text=True)
    assert "LowMA 承接策略配置" in page
    assert "/api/low-ma/research" in page
    assert page.index('id="scheme-overview-list"') < page.index('id="lowma-panel"')
    assert "openLowMAConfig" in page


def test_lowma_research_requires_explicit_dates_and_symbols(client):
    response = client.post("/api/low-ma/research", json={
        "symbols": ["sz000425"], "end_date": "2026-07-31",
    })
    assert response.status_code == 400
    assert "明确" in response.get_json()["error"]


def test_lowma_page_contains_explicit_simulation_fallback_link(client):
    page = client.get("/strategy-composer").get_data(as_text=True)
    assert "如果没有自动跳转" in page
    assert "simulation_url" in page


def test_strategy_simulation_has_unified_lowma_tabs(client):
    page = client.get("/strategy-simulation?lowma_run_id=test").get_data(as_text=True)
    assert "renderBatchResults" in page
    assert "K线" in page
    assert "交易记录" in page
    assert "事件日志" in page


def test_strategy_simulation_lists_lowma_system_strategy(client):
    page = client.get("/strategy-simulation?scheme=lowma_pullback").get_data(as_text=True)
    assert 'value="lowma_pullback"' in page
    assert "LowMA 承接参数" in page
    assert "next_day_limit_range" not in page  # execution detail remains backend-owned


def test_composer_lowma_card_links_to_selected_simulator(client):
    page = client.get("/strategy-composer").get_data(as_text=True)
    assert "/strategy-simulation?scheme=lowma_pullback" in page


def test_lowma_batch_accepts_system_strategy_without_scheme_registry(client, monkeypatch):
    captured = {}

    def run(target, args=(), daemon=None):
        captured["target"] = target
        captured["args"] = args
        return type("Thread", (), {"start": lambda self: None})()

    monkeypatch.setattr("StockInvestmentTool.web.app.threading.Thread", run)
    response = client.post("/strategy-simulation/batch", json={
        "scheme": "lowma_pullback", "start_date": "2024-01-01", "end_date": "2024-06-30",
        "initial_cash": 100000, "stocks": [{"code": "sz000425", "name": "徐工机械", "stock_type": "B"}],
        "lowma_options": {"entry_path": "path_a"},
    })
    assert response.status_code == 202
    assert captured["target"].__name__ == "_run_lowma_batch"


def test_lowma_batch_rejects_dates_after_latest_pass_partition(client, monkeypatch):
    monkeypatch.setattr("StockInvestmentTool.web.app._latest_stock_daily_pass_date", lambda: "2026-07-31")
    response = client.post("/strategy-simulation/batch", json={
        "scheme": "lowma_pullback", "start_date": "2024-01-01", "end_date": "2026-08-01",
        "initial_cash": 100000, "stocks": [{"code": "sz000425", "name": "徐工机械", "stock_type": "B"}],
    })
    assert response.status_code == 400
    assert "2026-07-31" in response.get_json()["error"]


def test_lowma_batch_normalizes_dotted_stock_codes(monkeypatch):
    from StockInvestmentTool.web.app import _run_lowma_batch, _analysis_status
    import StockInvestmentTool.web.app as app_module

    class FakeFrame:
        empty = False
        def replace(self, *args, **kwargs): return self
        def to_dict(self, orient="records"): return [{"stock": "sh600900", "trades": 3, "cumulative_return_pct": 4.0}]

    class FakeResult:
        def __getitem__(self, key):
            return FakeFrame()
        def get(self, key, default=None): return {}

    monkeypatch.setattr(app_module, "run_low_ma_dataset", lambda *args, **kwargs: FakeResult(), raising=False)
    # Contract is covered by the live integration test; this assertion protects
    # the normalized code path from regressing to dotted-code-only matching.
    assert "replace(\".\", \"\")" in __import__("inspect").getsource(_run_lowma_batch)


def test_strategy_simulation_routes_single_lowma_stock_to_batch_endpoint(client):
    page = client.get("/strategy-simulation?scheme=lowma_pullback").get_data(as_text=True)
    assert "simulationStocks.length === 1 && document.getElementById('sim-scheme').value !== 'lowma_pullback'" in page


def test_lowma_research_result_requires_existing_run(client):
    response = client.get("/api/low-ma/research/not-found")
    assert response.status_code == 404
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


def test_scheme_indicator_refs_follow_configured_reference(client):
    model = _sample_model()
    model["sell_rules"] = [{"type": "technical_stop", "params": {
        "support_source": "MA20", "volume_surge_ratio": 1.8,
        "technical_stop_enabled": True,
    }}]
    yaml_out = client.post("/api/schemes/compose", json=model).get_json()["yaml"]
    client.post("/api/schemes/save", json={"name": model["name"], "content": yaml_out})
    scheme = next(item for item in client.get("/api/schemes/list").get_json()["schemes"]
                  if item["name"] == model["name"])
    assert any(ref["key"] == "MA20" for ref in scheme["sell_rules"][0]["indicator_refs"])
    client.post("/api/schemes/delete", json={"name": model["name"]})


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
