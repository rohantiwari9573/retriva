"""Tests for app.main._check_production_config - the startup guard added in
Phase 7 that refuses to boot with an insecure production configuration
(cookies over plain HTTP, a wildcard CORS origin combined with credentials)."""

import pytest

from app.core.config import settings
from app.main import _check_production_config


def test_noop_in_development(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["*"])
    _check_production_config()  # must not raise


def test_production_requires_cookie_secure(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://app.example.com"])
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        _check_production_config()


def test_production_rejects_wildcard_cors(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["*"])
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        _check_production_config()


def test_production_passes_with_safe_config(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://app.example.com"])
    _check_production_config()  # must not raise
