from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from datetime import timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from common.db import Base


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def now():
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str | None] = mapped_column(String(255))
    tag: Mapped[str | None] = mapped_column(String(255))
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="created")
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Property(Base):
    __tablename__ = "properties"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    apn: Mapped[str | None] = mapped_column(String(120))
    apn_key: Mapped[str | None] = mapped_column(String(120), index=True)
    address_line1: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(2))
    zip5: Mapped[str | None] = mapped_column(String(5), index=True)
    address_key: Mapped[str | None] = mapped_column(String(255), index=True)
    pipeline_status: Mapped[str] = mapped_column(String(30), default="new")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    next_action: Mapped[str | None] = mapped_column(String(255))
    next_action_date: Mapped[date | None] = mapped_column(Date)
    gut_rating: Mapped[int | None] = mapped_column(Integer)
    is_watchlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    merged_into_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    batch_id: Mapped[UUID | None] = mapped_column(ForeignKey("batches.id"))
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    report_type: Mapped[str | None] = mapped_column(String(60))
    vendor: Mapped[str | None] = mapped_column(String(120))
    generated_date: Mapped[date | None] = mapped_column(Date)
    file_path: Mapped[str] = mapped_column(Text)
    ocr_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    is_scanned: Mapped[bool] = mapped_column(Boolean, default=False)
    ocr_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[UUID | None] = mapped_column(ForeignKey("reports.id"))
    status: Mapped[str] = mapped_column(String(30), default="uploaded")
    classification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ExtractionUnit(Base):
    __tablename__ = "extraction_units"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    report_id: Mapped[UUID] = mapped_column(ForeignKey("reports.id"))
    unit_type: Mapped[str] = mapped_column(String(60))
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    text_path: Mapped[str | None] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(80))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
