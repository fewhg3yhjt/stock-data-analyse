"""Cross-origin mutation protection tests."""

from __future__ import annotations


def test_cross_origin_mutation_is_rejected():
    from StockInvestmentTool.web.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    response = client.post(
        "/api/schemes/compose",
        json={"name": "csrf_test"},
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403


def test_same_origin_mutation_is_allowed_to_reach_handler():
    from StockInvestmentTool.web.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    response = client.post(
        "/api/schemes/compose",
        json={"name": "same_origin_test"},
        headers={"Origin": "https://localhost"},
        base_url="https://localhost",
    )

    assert response.status_code != 403
