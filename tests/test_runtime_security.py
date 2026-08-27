"""Runtime security configuration must fail closed in production mode."""

from __future__ import annotations

import pytest


def test_runtime_security_requires_credentials(monkeypatch):
    from StockInvestmentTool.web.app import _validate_runtime_security

    monkeypatch.delenv("STOCK_DEV_MODE", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        _validate_runtime_security()


def test_runtime_security_allows_explicit_dev_mode(monkeypatch):
    from StockInvestmentTool.web.app import _validate_runtime_security

    monkeypatch.setenv("STOCK_DEV_MODE", "1")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    _validate_runtime_security()


def test_runtime_security_allows_existing_weak_secret_with_warning(monkeypatch, caplog):
    from StockInvestmentTool.web.app import _validate_runtime_security

    monkeypatch.delenv("STOCK_DEV_MODE", raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "configured")
    monkeypatch.setenv("SECRET_KEY", "short")
    _validate_runtime_security()
    assert "长度不足" in caplog.text


def test_runtime_security_allows_temporary_auth_bypass(monkeypatch):
    from StockInvestmentTool.web.app import _auth_enabled, _validate_runtime_security

    monkeypatch.setenv("ADMIN_PASSWORD", "configured")
    monkeypatch.setenv("STOCK_DISABLE_AUTH", "1")
    monkeypatch.delenv("STOCK_DEV_MODE", raising=False)
    _validate_runtime_security()
    assert _auth_enabled() is False
