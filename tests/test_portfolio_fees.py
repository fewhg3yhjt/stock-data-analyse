from portfolio.fees import calculate_trade_fees


def test_stock_buy_uses_minimum_commission_and_transfer_fee():
    fees = calculate_trade_fees(1566, stock_type="B", direction="buy")
    assert fees.commission == 1.00
    assert fees.transfer == 0.02
    assert fees.stamp_tax == 0.00
    assert fees.total == 1.02


def test_stock_sell_includes_stamp_tax():
    fees = calculate_trade_fees(1610, stock_type="B", direction="sell")
    assert fees.commission == 1.00
    assert fees.transfer == 0.02
    assert fees.stamp_tax == 0.81
    assert fees.total == 1.83


def test_etf_uses_lower_minimum_commission_without_stock_tax():
    fees = calculate_trade_fees(922.6, stock_type="E", direction="buy")
    assert fees.commission == 0.50
    assert fees.transfer == 0.00
    assert fees.stamp_tax == 0.00
    assert fees.total == 0.50
