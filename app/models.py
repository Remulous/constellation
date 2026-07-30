from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


person_tags = Table(
    "person_tags",
    Base.metadata,
    Column("person_id", ForeignKey("people.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Provider(str, enum.Enum):
    GOOGLE = "google_contacts"
    LINKEDIN = "linkedin"


class Person(Base):
    __tablename__ = "people"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    display_name: Mapped[str] = mapped_column(String(300), index=True)
    first_name: Mapped[str | None] = mapped_column(String(120))
    middle_name: Mapped[str | None] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120), index=True)
    preferred_name: Mapped[str | None] = mapped_column(String(120))
    primary_email: Mapped[str | None] = mapped_column(String(320), index=True)
    primary_phone: Mapped[str | None] = mapped_column(String(50), index=True)
    current_organization: Mapped[str | None] = mapped_column(String(250), index=True)
    current_title: Mapped[str | None] = mapped_column(String(250))
    location: Mapped[str | None] = mapped_column(String(300))
    relationship_status: Mapped[str] = mapped_column(String(50), default="active", index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    followup_interval_days: Mapped[int | None] = mapped_column(Integer)
    last_meaningful_interaction: Mapped[date | None] = mapped_column(Date)
    next_followup: Mapped[date | None] = mapped_column(Date, index=True)
    followup_override: Mapped[date | None] = mapped_column(Date)
    followup_snoozed_until: Mapped[date | None] = mapped_column(Date)
    cadence_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    obsidian_uri: Mapped[str | None] = mapped_column(Text)
    general_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    methods: Mapped[list["ContactMethod"]] = relationship(cascade="all, delete-orphan")
    identities: Mapped[list["ExternalIdentity"]] = relationship(cascade="all, delete-orphan")
    employments: Mapped[list["Employment"]] = relationship(cascade="all, delete-orphan")
    interactions: Mapped[list["Interaction"]] = relationship(cascade="all, delete-orphan")
    tags: Mapped[list["Tag"]] = relationship(secondary=person_tags, back_populates="people")

    @property
    def linkedin_url(self) -> str | None:
        for identity in self.identities:
            if identity.provider == Provider.LINKEDIN.value and identity.profile_url and identity.active:
                return identity.profile_url
        return None


class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("provider", "provider_record_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50), index=True)
    provider_record_id: Mapped[str | None] = mapped_column(String(500))
    profile_url: Mapped[str | None] = mapped_column(Text)
    source_created_date: Mapped[date | None] = mapped_column(Date)
    source_updated_date: Mapped[date | None] = mapped_column(Date)
    last_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    source_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    record_hash: Mapped[str] = mapped_column(String(64), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ContactMethod(Base):
    __tablename__ = "contact_methods"
    __table_args__ = (UniqueConstraint("person_id", "method_type", "normalized_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    method_type: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text, index=True)
    label: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[str | None] = mapped_column(String(50))
    primary: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Employment(Base):
    __tablename__ = "employments"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    organization: Mapped[str] = mapped_column(String(250), index=True)
    title: Mapped[str | None] = mapped_column(String(250))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    current: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str | None] = mapped_column(String(50))


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    interaction_type: Mapped[str] = mapped_column(String(40))
    interaction_date: Mapped[date] = mapped_column(Date, index=True)
    direction: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(50), default="manual")
    external_reference: Mapped[str | None] = mapped_column(Text)
    source_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("vetbiz_import_records.id"), index=True
    )
    source_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("vetbiz_import_candidates.id"), unique=True
    )
    source_excerpt: Mapped[str | None] = mapped_column(Text)
    meaningful: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    people: Mapped[list[Person]] = relationship(secondary=person_tags, back_populates="tags")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    created_contacts: Mapped[int] = mapped_column(Integer, default=0)
    updated_contacts: Mapped[int] = mapped_column(Integer, default=0)
    exact_matches: Mapped[int] = mapped_column(Integer, default=0)
    possible_matches: Mapped[int] = mapped_column(Integer, default=0)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="processing")
    error_log: Mapped[str | None] = mapped_column(Text)


class MergeCandidate(Base):
    __tablename__ = "merge_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_identity_id: Mapped[int] = mapped_column(ForeignKey("external_identities.id"), index=True)
    candidate_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    match_reason: Mapped[str] = mapped_column(Text)
    confidence_score: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MergeHistory(Base):
    __tablename__ = "merge_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    survivor_person_id: Mapped[str] = mapped_column(String(36), index=True)
    merged_person_id: Mapped[str] = mapped_column(String(36), index=True)
    candidate_id: Mapped[int | None] = mapped_column(Integer, index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    merged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SavedSegment(Base):
    __tablename__ = "saved_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VetBizImportRecord(Base):
    __tablename__ = "vetbiz_import_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    meeting_title: Mapped[str] = mapped_column(String(300))
    meeting_date: Mapped[date] = mapped_column(Date, index=True)
    source_type: Mapped[str] = mapped_column(
        String(50), default="vetbiz_reviewed_minutes", index=True
    )
    review_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    review_notes: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    import_status: Mapped[str] = mapped_column(
        String(30), default="pending_review", index=True
    )
    raw_text: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    revision_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("vetbiz_import_records.id"), index=True
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidates: Mapped[list["VetBizImportCandidate"]] = relationship(
        cascade="all, delete-orphan", back_populates="import_record"
    )


class VetBizImportCandidate(Base):
    __tablename__ = "vetbiz_import_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_record_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_records.id", ondelete="CASCADE"), index=True
    )
    candidate_type: Mapped[str] = mapped_column(String(40), index=True)
    extracted_data: Mapped[dict] = mapped_column(JSON, default=dict)
    source_excerpt: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    matched_entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    match_reason: Mapped[str | None] = mapped_column(Text)
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    committed_entity_type: Mapped[str | None] = mapped_column(String(40))
    committed_entity_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    import_record: Mapped["VetBizImportRecord"] = relationship(back_populates="candidates")


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(250), index=True)
    normalized_name: Mapped[str] = mapped_column(String(250), unique=True, index=True)
    website: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    source_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("vetbiz_import_records.id"), index=True
    )
    source_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("vetbiz_import_candidates.id"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RelationshipSignal(Base):
    __tablename__ = "relationship_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    signal_type: Mapped[str] = mapped_column(String(50), index=True)
    summary: Mapped[str] = mapped_column(Text)
    meeting_date: Mapped[date] = mapped_column(Date, index=True)
    source_import_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_records.id"), index=True
    )
    source_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_candidates.id"), unique=True
    )
    source_excerpt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    person_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    product: Mapped[str | None] = mapped_column(String(80), index=True)
    stage: Mapped[str] = mapped_column(String(50), default="identified", index=True)
    next_action: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    source_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("relationship_signals.id"), index=True
    )
    source_import_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_records.id"), index=True
    )
    source_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_candidates.id"), unique=True
    )
    source_excerpt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConnectionSuggestion(Base):
    __tablename__ = "connection_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_person_id: Mapped[str] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    target_person_id: Mapped[str] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(Text)
    supporting_signal_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="suggested", index=True)
    source_import_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_records.id"), index=True
    )
    source_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_candidates.id"), unique=True
    )
    source_excerpt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FollowUpSuggestion(Base):
    __tablename__ = "follow_up_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[str] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    summary: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(30), default="suggested", index=True)
    source_import_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_records.id"), index=True
    )
    source_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("vetbiz_import_candidates.id"), unique=True
    )
    source_excerpt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
