import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class JobStatus(str, enum.Enum):
    pending = "pending"
    confirming = "confirming"  # awaiting mapping confirmation
    processing = "processing"
    completed = "completed"
    failed = "failed"
    paused = "paused"


class ASINStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    needs_review = "needs_review"
    missing_data = "missing_data"
    failed = "failed"
    approved = "approved"


class GenerationMode(str, enum.Enum):
    vision = "vision"
    text_only = "text_only"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status = Column(Enum(JobStatus), default=JobStatus.pending, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Source files
    source_files = Column(JSON, default=list)  # list of uploaded file paths
    template_type = Column(String(100))  # amazon flat file category
    mapping_config = Column(String(200))  # path to column mapping JSON

    # Progress tracking
    total_asins = Column(Integer, default=0)
    completed_asins = Column(Integer, default=0)
    failed_asins = Column(Integer, default=0)
    missing_data_asins = Column(Integer, default=0)

    # Cost tracking
    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)
    estimated_cost_usd = Column(Float, default=0.0)

    # Relationships
    asin_records = relationship("ASINRecord", back_populates="job", cascade="all, delete-orphan")


class ASINRecord(Base):
    __tablename__ = "asin_records"
    __table_args__ = (
        UniqueConstraint("job_id", "asin", name="uq_job_asin"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False, index=True)
    asin = Column(String(20), nullable=False, index=True)
    parent_asin = Column(String(20), nullable=True)  # for parent/child variations
    variation_theme = Column(String(100), nullable=True)

    status = Column(Enum(ASINStatus), default=ASINStatus.queued, nullable=False, index=True)
    generation_mode = Column(
        Enum(GenerationMode), default=GenerationMode.text_only, nullable=False
    )

    # Data fields (JSON)
    source_data = Column(JSON, default=dict)  # raw fields from base spreadsheets
    vision_data = Column(JSON, default=dict)  # features extracted from product images
    generated_data = Column(JSON, default=dict)  # AI-generated listing fields
    image_paths = Column(JSON, default=list)  # paths to product images

    # Quality
    confidence_score = Column(Float, nullable=True)
    missing_fields = Column(JSON, default=list)

    # Error tracking
    error_log = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    # Token usage
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    job = relationship("Job", back_populates="asin_records")


class ReferenceProfile(Base):
    __tablename__ = "reference_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    source_filename = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Structured optimization patterns (concrete schema, not raw text)
    tone = Column(String(100))  # e.g., "professional", "conversational"
    bullet_style = Column(String(100))  # e.g., "question-answer", "feature-benefit"
    keyword_density = Column(String(50))  # e.g., "moderate", "high"
    title_pattern = Column(Text)  # e.g., "Brand + Product + Key Feature + Use Case"
    formatting_rules = Column(JSON, default=list)  # list of extracted rules
    example_content = Column(JSON, default=dict)  # sample optimized fields
