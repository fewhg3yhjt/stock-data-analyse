"""Scheduler lock helper behavior is documented by the init contract."""

from __future__ import annotations

import os

import pytest


@pytest.mark.skipif(os.name == "nt", reason="fcntl is Unix-only")
def test_scheduler_lock_path_is_under_data_dir(tmp_path, monkeypatch):
    from StockInvestmentTool.config import Config

    monkeypatch.setattr(Config, "DATA_DIR", tmp_path)
    path = Config.DATA_DIR / "scheduler.lock"
    path.touch()

    assert path.parent == tmp_path
