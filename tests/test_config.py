"""Tests for Settings configuration and Heroku URL fix validator."""

import os

import pytest
from pydantic import SecretStr


def test_heroku_postgres_url_rewritten():
    """Heroku uses postgres:// but SQLAlchemy needs postgresql://."""
    from app.config import Settings

    s = Settings(
        database_url="postgres://user:pass@host:5432/db",
        _env_file=None,
    )
    assert s.database_url.startswith("postgresql://")
    assert "postgres://" not in s.database_url


def test_postgresql_url_unchanged():
    """A proper postgresql:// URL should not be modified."""
    from app.config import Settings

    s = Settings(
        database_url="postgresql://user:pass@host:5432/db",
        _env_file=None,
    )
    assert s.database_url == "postgresql://user:pass@host:5432/db"


def test_celery_urls_default_to_redis_url():
    """When redis_url is set but celery URLs are defaults, celery should follow redis."""
    from app.config import Settings

    s = Settings(
        redis_url="redis://custom-redis:6379/0",
        _env_file=None,
    )
    assert s.celery_broker_url == "redis://custom-redis:6379/0"
    assert s.celery_result_backend == "redis://custom-redis:6379/0"


def test_explicit_celery_urls_preserved():
    """Explicitly set celery URLs should not be overwritten."""
    from app.config import Settings

    s = Settings(
        redis_url="redis://custom-redis:6379/0",
        celery_broker_url="redis://broker:6379/0",
        celery_result_backend="redis://backend:6379/1",
        _env_file=None,
    )
    assert s.celery_broker_url == "redis://broker:6379/0"
    assert s.celery_result_backend == "redis://backend:6379/1"


def test_default_values():
    """Verify sane defaults for key settings."""
    import os
    from unittest.mock import patch

    from app.config import Settings

    # Temporarily clear env vars that override defaults
    env_overrides = {
        "ENVIRONMENT": "development",
        "AUTH_USERNAME": "admin",
        "AUTH_PASSWORD": "changeme",
        "DATABASE_URL": "postgresql://localhost:5432/rufus_generator",
        "ANTHROPIC_API_KEY": "",
    }
    with patch.dict(os.environ, env_overrides):
        s = Settings(_env_file=None)
    assert s.app_name == "Rufus Generator"
    assert s.environment == "development"
    assert s.debug is False
    assert s.anthropic_max_concurrent == 10
    assert s.max_images_per_asin == 10
    assert s.retry_max_attempts == 3


def test_auth_password_is_secret():
    """Auth password should be a SecretStr."""
    from app.config import Settings

    s = Settings(_env_file=None)
    assert isinstance(s.auth_password, SecretStr)
    # The secret value should not appear in repr
    assert "changeme" not in repr(s.auth_password)


def test_upload_limits():
    """Upload limits should be reasonable sizes."""
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.max_upload_size_bytes == 10 * 1024 * 1024 * 1024  # 10GB
    assert s.max_image_size_bytes == 10 * 1024 * 1024  # 10MB


def test_get_logger_returns_logger():
    """get_logger should return a structlog logger."""
    from app.config import get_logger

    log = get_logger("test")
    assert log is not None


def test_default_password_rejected_in_production(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("DYNO", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="AUTH_PASSWORD"):
        Settings(environment="production", _env_file=None)


def test_default_password_rejected_on_heroku(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DYNO", "web.1")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="AUTH_PASSWORD"):
        Settings(_env_file=None)


def test_real_password_accepted_on_heroku(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DYNO", "web.1")
    s = Settings(auth_password=SecretStr("a-real-password"), _env_file=None)
    assert s.auth_password.get_secret_value() == "a-real-password"


def test_default_password_allowed_in_development(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("DYNO", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    s = Settings(_env_file=None)
    assert s.auth_password.get_secret_value() == "changeme"
