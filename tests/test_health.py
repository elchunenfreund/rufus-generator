"""Tests for /health endpoint and home route."""

import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.asyncio
async def test_health_check_returns_app_info(client):
    """Health endpoint should be unauthenticated and return app metadata.

    In test environment, DB/Redis/Celery are not available, so status will
    be 'degraded' (503). We still verify the response structure.
    """
    resp = await client.get("/health")
    # Accept either 200 (all healthy) or 503 (degraded in test env)
    assert resp.status_code in (200, 503)
    data = resp.json()
    assert data["app"] == "Rufus Generator"
    assert "version" in data
    assert "status" in data


@pytest.mark.asyncio
async def test_health_check_structure(client):
    """Health response should include all subsystem check keys."""
    resp = await client.get("/health")
    data = resp.json()
    assert "database" in data
    assert "redis" in data
    assert "celery_workers" in data
    assert "status" in data


@pytest.mark.asyncio
async def test_home_requires_auth(client):
    """GET / without credentials should return 401."""
    resp = await client.get("/")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_home_with_auth(client, auth_headers):
    """GET / with valid credentials should return 200 HTML."""
    resp = await client.get("/", headers=auth_headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
