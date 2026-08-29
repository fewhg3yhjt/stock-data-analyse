def test_management_db_path_uses_explicit_environment(monkeypatch, tmp_path):
    from ops.task_center import management_db_path
    target = tmp_path / "management.db"
    monkeypatch.setenv("MANAGEMENT_DB_PATH", str(target))
    assert management_db_path() == target
