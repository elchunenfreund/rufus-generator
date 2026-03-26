"""Shared fixtures for the test suite."""

import os
import sys
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import StaticPool, create_engine, event
from sqlalchemy.orm import sessionmaker

# Set env vars BEFORE any app imports so Settings picks them up.
# Use a postgresql:// URL so the module-level create_engine in database.py
# doesn't choke on SQLite-incompatible kwargs.  We never actually connect
# through that engine — every test gets an in-memory SQLite engine via
# the db_engine fixture.
os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test_rufus"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["AUTH_USERNAME"] = "testuser"
os.environ["AUTH_PASSWORD"] = "testpass"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-fake-key"
os.environ["ENVIRONMENT"] = "test"

# ---------------------------------------------------------------------------
# SQLite compatibility: PostgreSQL UUID type -> CHAR(32) in SQLite
# Must be registered BEFORE metadata is created.
# ---------------------------------------------------------------------------
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy import String, TypeDecorator

import sqlalchemy

_original_create_engine = sqlalchemy.create_engine


class SQLiteUUID(TypeDecorator):
    """Platform-independent UUID type: uses CHAR(32) on SQLite."""
    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if isinstance(value, uuid.UUID):
                return value.hex
            return uuid.UUID(str(value)).hex
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return uuid.UUID(value)
        return value


# Monkey-patch the PG UUID so it works on SQLite
PG_UUID.__init_orig__ = PG_UUID.__init__


class _PatchedUUID(PG_UUID):
    """Wrapper that returns SQLiteUUID adapter when dialect is SQLite."""
    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(SQLiteUUID())
        return super().load_dialect_impl(dialect)

    def bind_processor(self, dialect):
        if dialect.name == "sqlite":
            return SQLiteUUID().bind_processor(dialect)
        return super().bind_processor(dialect)

    def result_processor(self, dialect, coltype):
        if dialect.name == "sqlite":
            return SQLiteUUID().result_processor(dialect, coltype)
        return super().result_processor(dialect, coltype)


# Replace in the dialects module so model definitions pick it up
import sqlalchemy.dialects.postgresql as pg_mod
pg_mod.UUID = _PatchedUUID  # type: ignore[attr-defined]

# Also patch the specific import path used by app.models
import sqlalchemy.dialects.postgresql.types as pg_types_mod
if hasattr(pg_types_mod, "UUID"):
    pg_types_mod.UUID = _PatchedUUID  # type: ignore[attr-defined]


# Patch the module-level engine creation in app.database so it doesn't try
# to connect to a real PostgreSQL instance on import.
_test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


def _patched_create_engine(url, **kwargs):
    """Intercept module-level create_engine and return our test engine."""
    if "test_rufus" in str(url) or "postgresql" in str(url):
        return _test_engine
    return _original_create_engine(url, **kwargs)


sqlalchemy.create_engine = _patched_create_engine

# Now safe to import app modules
from app.database import Base, get_db  # noqa: E402
from app.models import (  # noqa: E402
    ASINRecord,
    ASINStatus,
    GenerationMode,
    Job,
    JobStatus,
    ReferenceProfile,
)

# Restore original create_engine for test fixtures
sqlalchemy.create_engine = _original_create_engine


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_engine():
    """Create an in-memory SQLite engine for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Provide a transactional DB session that rolls back after each test."""
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    session = TestSession()
    yield session
    session.rollback()
    session.close()


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(db_session):
    """HTTPX-based async test client with DB override and auth helper."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_headers():
    """HTTP Basic auth headers for the default test user."""
    import base64
    creds = base64.b64encode(b"testuser:testpass").decode()
    return {"Authorization": f"Basic {creds}"}


# ---------------------------------------------------------------------------
# Model factory helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_job(db_session):
    """Insert and return a sample Job."""
    job = Job(
        id=uuid.uuid4(),
        status=JobStatus.pending,
        total_asins=3,
        source_files=["/tmp/test.xlsx"],
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


@pytest.fixture()
def sample_asin_record(db_session, sample_job):
    """Insert and return a sample ASINRecord linked to sample_job."""
    record = ASINRecord(
        id=uuid.uuid4(),
        job_id=sample_job.id,
        asin="B0TESTTEST",
        source_data={"brand": "TestBrand", "product_name": "Widget"},
        generation_mode=GenerationMode.text_only,
        status=ASINStatus.queued,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


@pytest.fixture()
def completed_asin_record(db_session, sample_job):
    """An ASIN record that has been fully generated."""
    record = ASINRecord(
        id=uuid.uuid4(),
        job_id=sample_job.id,
        asin="B0COMPLETE",
        source_data={"brand": "TestBrand", "product_name": "Completed Widget"},
        generated_data={
            "title": "TestBrand Widget - Premium Quality",
            "bullet_point_1": "Durable construction",
            "bullet_point_2": "Easy to use",
            "bullet_point_3": "Lightweight design",
            "bullet_point_4": "Versatile functionality",
            "bullet_point_5": "Great value",
            "description": "A premium widget for all your needs.",
            "search_terms": "premium widget durable lightweight",
            "image_alt_text": "TestBrand widget in silver finish",
            "color": "Silver",
            "size": "Medium",
            "material": "Aluminum",
            "intended_use": "General purpose",
            "age_range_description": "Adult",
            "target_audience": "Home users",
            "item_form": "Solid",
            "special_features": "Corrosion resistant",
            "included_components": "1x Widget, 1x Manual",
            "country_of_origin": "USA",
        },
        generation_mode=GenerationMode.text_only,
        status=ASINStatus.completed,
        confidence_score=0.92,
        missing_fields=[],
        input_tokens=1500,
        output_tokens=800,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


@pytest.fixture()
def sample_reference_profile(db_session):
    """Insert and return a sample ReferenceProfile."""
    profile = ReferenceProfile(
        id=uuid.uuid4(),
        name="Test Competitor Profile",
        source_filename="/tmp/competitor.pdf",
        tone="professional",
        bullet_style="feature-benefit",
        keyword_density="moderate",
        title_pattern="Brand + Product + Feature + Use Case",
        formatting_rules=["Capitalize first word", "Max 500 chars per bullet"],
        example_content={"title_example": "BrandX Widget Pro - Heavy Duty"},
    )
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile
