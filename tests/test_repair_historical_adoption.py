def test_repair_module_is_available():
    from scripts.repair_historical_adoption import repair
    assert callable(repair)
