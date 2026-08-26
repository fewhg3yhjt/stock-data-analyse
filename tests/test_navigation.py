"""核心页面公共导航回归测试。"""

from __future__ import annotations

import pytest


@pytest.fixture
def client():
    from StockInvestmentTool.web.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    return client


@pytest.mark.parametrize(
    ("path", "active"),
    [
        ("/", "analyze"),
        ("/market", "market"),
        ("/dashboard/observe", "observe"),
        ("/watchlist", "watchlist"),
        ("/simulation", "simulation"),
        ("/strategy", "strategy"),
        ("/strategy-composer", "composer"),
        ("/notify-center", "notify"),
        ("/dashboard/warroom", "warroom"),
        ("/dashboard/review", "review"),
        ("/settings", "settings"),
    ],
)
def test_core_page_renders_shared_navigation(client, path, active):
    response = client.get(path)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'class="nav"' in html
    assert 'href="/"' in html
    assert 'href="/dashboard/warroom"' in html
    assert f'<a href="{_href_for_active(active)}" class="active">' in html


def _href_for_active(active: str) -> str:
    return {
        "analyze": "/",
        "market": "/market",
        "observe": "/dashboard/observe",
        "watchlist": "/watchlist",
        "simulation": "/simulation",
        "strategy": "/strategy",
        "composer": "/strategy-composer",
        "notify": "/notify-center",
        "warroom": "/dashboard/warroom",
        "review": "/dashboard/review",
        "settings": "/settings",
    }[active]
