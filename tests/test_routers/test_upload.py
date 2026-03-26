"""Tests for the upload router."""

import uuid
from unittest.mock import patch, MagicMock

import pytest

from app.models import Job, JobStatus


@pytest.mark.asyncio
async def test_upload_page_requires_auth(client):
    resp = await client.get("/upload/")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_upload_page_renders(client, auth_headers):
    resp = await client.get("/upload/", headers=auth_headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_confirm_job_not_found(client, auth_headers):
    fake_id = str(uuid.uuid4())
    resp = await client.post(f"/upload/confirm/{fake_id}", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_confirm_job_wrong_status(client, auth_headers, db_session, sample_job):
    """Confirming a job that is not in 'confirming' status should return 400."""
    # sample_job is 'pending'
    resp = await client.post(
        f"/upload/confirm/{sample_job.id}", headers=auth_headers
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_confirm_job_starts_processing(client, auth_headers, db_session, sample_job):
    """Confirming a 'confirming' job should set status to pending and enqueue."""
    sample_job.status = JobStatus.confirming
    db_session.commit()

    with patch("app.tasks.process_asin.enqueue_job") as mock_enqueue:
        mock_enqueue.delay = MagicMock()
        resp = await client.post(
            f"/upload/confirm/{sample_job.id}",
            headers=auth_headers,
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "/processing/" in resp.headers["location"]
    mock_enqueue.delay.assert_called_once_with(str(sample_job.id))

    db_session.refresh(sample_job)
    assert sample_job.status == JobStatus.pending
