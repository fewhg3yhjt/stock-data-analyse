from scripts.run_production_validation import _quality


def test_production_validation_quality_gate():
    result = _quality("valuation_daily", 20, 2, 2, "2025-01-01", "2025-12-31")
    assert result["status"] == "PASS"
    assert result["publish_allowed"] is True
