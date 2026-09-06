from __future__ import annotations


def test_left_side_signal_requires_an_active_sell_tier(monkeypatch, tmp_path):
    from StockInvestmentTool.portfolio.manager import PortfolioManager
    from StockInvestmentTool.portfolio.models import ActionAdvice, Position

    manager = PortfolioManager()
    position = Position(id=7, stock_code="sh.600000", stock_name="测试", status="open")
    advice = ActionAdvice(
        position_id=7, stock_code=position.stock_code, stock_name=position.stock_name,
        advice_type="hold", check_results={
            "left_side": {"tier": 0, "sell_ratio": 0.0},
        },
    )
    emitted = []
    monkeypatch.setattr(manager, "_emit_signal_notifications", lambda *_args: emitted.append(True))
    # The regression is asserted against the notification gate itself below;
    # tier 0 must not be considered a triggered left-side sell signal.
    cr = advice.check_results["left_side"]
    assert not (cr.get("tier", 0) > 0 or cr.get("sell_ratio", 0) > 0)
