"""Daily notification pipeline builds one aggregated digest."""

from __future__ import annotations

from StockInvestmentTool.notifier.core import MessageAggregator, NotificationFragment


def test_daily_digest_topics_are_aggregated_once():
    aggregator = MessageAggregator()
    aggregator.add(NotificationFragment("price", "价格", ["A 上涨"]))
    aggregator.add(NotificationFragment("fundflow", "资金流", ["行业流入"]))
    aggregator.add(NotificationFragment("summary", "汇总", ["市场正常"]))
    aggregator.add(NotificationFragment("orders", "持仓", ["A 减仓"]))

    digest = aggregator.digest(meta={"subject": "盘后汇总"})

    assert digest is not None
    assert len(digest.sections) == 4
    assert digest.to_text().count("A 上涨") == 1
