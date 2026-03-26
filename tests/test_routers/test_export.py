"""Tests for the export router."""

import uuid

import pytest

from app.models import ASINRecord, ASINStatus, Job


@pytest.mark.asyncio
async def test_export_index_redirects(client, auth_headers, sample_job):
    resp = await client.get("/export/", headers=auth_headers, follow_redirects=False)
    assert resp.status_code == 302


@pytest.mark.asyncio
async def test_export_page_renders(client, auth_headers, completed_asin_record):
    job_id = str(completed_asin_record.job_id)
    resp = await client.get(f"/export/{job_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_export_page_404(client, auth_headers):
    resp = await client.get(f"/export/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sheets_export_no_records(client, auth_headers, sample_job):
    """Exporting a job with no approved/completed ASINs should return 400."""
    resp = await client.post(
        f"/export/{sample_job.id}/sheets", headers=auth_headers
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sheets_export_no_config_returns_500(
    client, auth_headers, db_session, completed_asin_record
):
    """Without Google service account config, export should fail gracefully."""
    job_id = str(completed_asin_record.job_id)

    resp = await client.post(
        f"/export/{job_id}/sheets?include_completed=true", headers=auth_headers
    )
    # Should get 500 because no Google service account is configured
    assert resp.status_code == 500
    assert "Export failed" in resp.json()["detail"]
