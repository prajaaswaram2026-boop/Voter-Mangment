import uuid
from datetime import datetime

from sqlalchemy import (Boolean, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, DateTime)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="admin")  # admin | operator


class District(Base):
    __tablename__ = "districts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class Assembly(Base):
    __tablename__ = "assemblies"
    __table_args__ = (UniqueConstraint("district_id", "number", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("districts.id"))
    number: Mapped[str] = mapped_column(String(10), default="")
    name: Mapped[str] = mapped_column(String(120))


class Division(Base):
    __tablename__ = "divisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    assembly_id: Mapped[int | None] = mapped_column(ForeignKey("assemblies.id"), nullable=True)
    ac_number: Mapped[str] = mapped_column(String(10), default="")
    ac_name: Mapped[str] = mapped_column(String(120), default="")
    district: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")


class PollingStation(Base):
    __tablename__ = "polling_stations"
    __table_args__ = (UniqueConstraint("division_id", "part_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    division_id: Mapped[int] = mapped_column(ForeignKey("divisions.id"))
    part_number: Mapped[str] = mapped_column(String(10))
    number: Mapped[str] = mapped_column(String(40), default="")
    name: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    locality: Mapped[str] = mapped_column(String(120), default="")
    mandal: Mapped[str] = mapped_column(String(120), default="")
    map_url: Mapped[str] = mapped_column(String(300), default="")


class Upload(Base):
    __tablename__ = "electoral_roll_uploads"
    id: Mapped[int] = mapped_column(primary_key=True)
    division_id: Mapped[int] = mapped_column(ForeignKey("divisions.id"))
    station_id: Mapped[int | None] = mapped_column(ForeignKey("polling_stations.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(400))
    roll_year: Mapped[int] = mapped_column(Integer)
    revision_type: Mapped[str] = mapped_column(String(30), default="main")  # main|supplement|deletions
    part_number: Mapped[str] = mapped_column(String(10), default="")
    station_name: Mapped[str] = mapped_column(String(200), default="")
    station_number: Mapped[str] = mapped_column(String(20), default="")
    qualifying_date: Mapped[str] = mapped_column(String(30), default="")
    publication_date: Mapped[str] = mapped_column(String(30), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # Uploaded | Queued | Processing | Validation Required | Completed | Failed
    status: Mapped[str] = mapped_column(String(30), default="Uploaded")
    error: Mapped[str] = mapped_column(Text, default="")
    pages: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Voter(Base):
    __tablename__ = "voters"
    __table_args__ = (
        # same voter may exist in different roll uploads (revisions), never twice in one
        UniqueConstraint("upload_id", "serial_number", "epic_number", name="uq_voter_upload"),
        Index("ix_voter_epic", "epic_number"),
        Index("ix_voter_name", "voter_name"),
        Index("ix_voter_rel", "relative_name"),
        Index("ix_voter_div", "division_id"),
        Index("ix_voter_part", "part_number"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    epic_number: Mapped[str] = mapped_column(String(20), default="")
    serial_number: Mapped[int] = mapped_column(Integer, default=0)
    voter_name: Mapped[str] = mapped_column(String(200), default="")
    relative_name: Mapped[str] = mapped_column(String(200), default="")
    relationship_type: Mapped[str] = mapped_column(String(20), default="")
    house_number: Mapped[str] = mapped_column(String(40), default="")
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gender: Mapped[str] = mapped_column(String(20), default="")
    division_id: Mapped[int] = mapped_column(ForeignKey("divisions.id"))
    upload_id: Mapped[int] = mapped_column(ForeignKey("electoral_roll_uploads.id"))
    part_number: Mapped[str] = mapped_column(String(10), default="")
    section: Mapped[str] = mapped_column(String(200), default="")
    pdf_page_number: Mapped[int] = mapped_column(Integer, default=0)
    record_status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|published
    ocr_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    flags: Mapped[str] = mapped_column(String(120), default="")  # e.g. missing_epic,duplicate
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExtractionLog(Base):
    __tablename__ = "ocr_extraction_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("electoral_roll_uploads.id"))
    page: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20))
    records: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")


class SearchLog(Base):
    """No names, EPICs or full mobile numbers are stored here."""
    __tablename__ = "voter_search_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    search_type: Mapped[str] = mapped_column(String(10))
    outcome: Mapped[str] = mapped_column(String(20))
    session_id: Mapped[str] = mapped_column(String(40), default="")


class Consent(Base):
    __tablename__ = "consent_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(60))
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MobileVerification(Base):
    """Only successfully verified numbers are stored (encrypted). Not linked to any voter."""
    __tablename__ = "mobile_verification_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40))
    mobile_masked: Mapped[str] = mapped_column(String(20))
    mobile_enc: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Volunteer(Base):
    __tablename__ = "volunteers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    mobile: Mapped[str] = mapped_column(String(15), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class VolunteerBooth(Base):
    __tablename__ = "volunteer_booths"
    __table_args__ = (UniqueConstraint("volunteer_id", "station_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("polling_stations.id"))


class VoterMark(Base):
    """Volunteer outreach on one elector: contact mobile and mood. Visible to admin and volunteers."""
    __tablename__ = "voter_marks"
    id: Mapped[int] = mapped_column(primary_key=True)
    voter_id: Mapped[str] = mapped_column(String(36), unique=True)
    mobile: Mapped[str] = mapped_column(String(15), default="")
    mood: Mapped[str] = mapped_column(String(20), default="")
    updated_by: Mapped[str] = mapped_column(String(80), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MobileClient(Base):
    """A phone installation. The admin chooses which division that phone receives."""
    __tablename__ = "mobile_clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(40), unique=True)
    division_id: Mapped[int | None] = mapped_column(ForeignKey("divisions.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "admin_audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    username: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default="")
