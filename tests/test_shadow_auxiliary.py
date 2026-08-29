from scripts.run_shadow_auxiliary import _quality


def test_auxiliary_quality_uses_symbol_coverage():
    result = _quality("fundamentals", 10, 9, 10, "", "")
    assert result["coverage"] == 0.9
    assert result["status"] == "WARNING"
    assert result["publish_allowed"] is False
