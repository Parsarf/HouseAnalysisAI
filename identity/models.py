from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


def now():
    return datetime.now(UTC)


class MergeReportMove(Base):
    # Provenance for reports re-parented by merge(); unmerge() restores any move
    # whose restored_at is still NULL. Additive to db/schema.sql — a migration
    # in the 140-159 range (WP-3 ownership) should create this table.
    __tablename__ = "identity_merge_report_moves"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_property_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("properties.id"), index=True)
    target_property_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("properties.id"))
    report_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("reports.id"), index=True)
    moved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
