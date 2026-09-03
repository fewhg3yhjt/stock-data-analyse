from __future__ import annotations

from StockInvestmentTool.portfolio.dashboard import DashboardService


def test_industry_rotation_overview_maps_published_features(monkeypatch):
    class FakeRotation:
        def __init__(self):
            self.last_context = {"status": "success", "actual_data_as_of": "2026-09-02", "reason": None}

        def decide(self, as_of):
            assert as_of == "2026-09-03"
            return [{"industry_code": "I64", "industry_name": "互联网", "industry_state": "strong",
                     "industry_score": 0.81234, "return_1d": 0.01, "return_5d": -0.02,
                     "return_20d": 0.15, "up_ratio": 0.6, "amount_ratio": 1.2,
                     "rank_1d": 2, "rank_5d": 1, "rank_20d": 3,
                     "leader_code": "sh600000", "state_reason": "score=0.8123"}]

    monkeypatch.setattr("StockInvestmentTool.warehouse.industry_features.IndustryRotationService", FakeRotation)
    result = DashboardService(object()).industry_rotation_overview("2026-09-03")
    assert result["actual_data_as_of"] == "2026-09-02"
    assert result["items"][0]["status_label"] == "强势"
    assert result["items"][0]["leader"] == "sh600000"
    assert result["items"][0]["return_5d"] == -0.02
