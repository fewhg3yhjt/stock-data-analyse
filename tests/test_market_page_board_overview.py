from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_market_page_restores_board_trend_overview():
    template = (ROOT / "web/templates/market.html").read_text(encoding="utf-8")
    app = (ROOT / "web/app.py").read_text(encoding="utf-8")

    assert "板块行情概览" in template
    assert "board.change_5d_pct" in template
    assert "board.trend" in template
    assert "board_overview = svc.board_overview()" in app
    assert "board_overview=board_overview" in app
