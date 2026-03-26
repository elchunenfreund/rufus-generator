"""Tests for HTTP Basic authentication."""

import base64

import pytest


@pytest.mark.asyncio
async def test_valid_credentials_accepted(client, auth_headers):
    """Correct username+password should grant access."""
    resp = await client.get("/", headers=auth_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_wrong_password_rejected(client):
    """Wrong password should return 401."""
    creds = base64.b64encode(b"testuser:wrongpassword").decode()
    resp = await client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_username_rejected(client):
    """Wrong username should return 401."""
    creds = base64.b64encode(b"baduser:testpass").decode()
    resp = await client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_missing_auth_returns_401(client):
    """No Authorization header should return 401."""
    resp = await client.get("/")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_returns_username():
    """verify_credentials should return the username on success."""
    from unittest.mock import MagicMock

    from fastapi.security import HTTPBasicCredentials

    from app.auth import verify_credentials

    creds = MagicMock(spec=HTTPBasicCredentials)
    creds.username = "testuser"
    creds.password = "testpass"

    result = verify_credentials(creds)
    assert result == "testuser"


@pytest.mark.asyncio
async def test_auth_raises_on_bad_creds():
    """verify_credentials should raise HTTPException on bad creds."""
    from unittest.mock import MagicMock

    from fastapi import HTTPException
    from fastapi.security import HTTPBasicCredentials

    from app.auth import verify_credentials

    creds = MagicMock(spec=HTTPBasicCredentials)
    creds.username = "wrong"
    creds.password = "wrong"

    with pytest.raises(HTTPException) as exc_info:
        verify_credentials(creds)
    assert exc_info.value.status_code == 401
