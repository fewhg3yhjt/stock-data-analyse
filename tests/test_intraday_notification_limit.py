from StockInvestmentTool.web import scheduler


def test_intraday_trigger_quota_limits_to_three(tmp_path, monkeypatch):
    monkeypatch.setattr("StockInvestmentTool.config.Config.DATA_DIR", tmp_path)
    monkeypatch.setenv("INTRADAY_NOTIFY_DAILY_LIMIT", "3")
    scheduler._NOTIFY_STATE = None
    rule = {"id": "ordinary", "name": "普通盘中提醒"}
    for expected in range(3):
        quota = scheduler._intraday_trigger_quota(rule)
        assert quota["allowed"] is True
        assert quota["count"] == expected
        scheduler._commit_intraday_trigger_quota(quota)
    assert scheduler._intraday_trigger_quota(rule)["allowed"] is False
