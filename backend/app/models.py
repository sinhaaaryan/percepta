from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, Range
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Skill(Base):
    __tablename__ = "skill"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))


class Unit(Base):
    __tablename__ = "unit"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    # Staffing templates, e.g. {"day": {"min": 5, "max": 7, "skill_min": {"2": 3}, "charge_seniority": 3}, ...}
    templates: Mapped[dict] = mapped_column(JSONType, default=dict)


class Nurse(Base):
    __tablename__ = "nurse"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    unit_id: Mapped[int] = mapped_column(ForeignKey("unit.id"))
    skills: Mapped[list] = mapped_column(JSONType, default=list)  # skill ids
    seniority_years: Mapped[int] = mapped_column(Integer, default=0)
    charge_qualified: Mapped[bool] = mapped_column(Boolean, default=False)
    max_minutes_per_week: Mapped[int] = mapped_column(Integer, default=40 * 60)
    max_consecutive_days: Mapped[int] = mapped_column(Integer, default=3)
    target_shifts_per_week: Mapped[int] = mapped_column(Integer, default=3)
    is_agency: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # {"liked_weekdays": [0..6], "disliked_weekdays": [...], "preferred_kind": "day"|"night"|null}
    preferences: Mapped[dict] = mapped_column(JSONType, default=dict)
    unit: Mapped[Unit] = relationship()


class AvailabilityBlock(Base):
    """Hard block: PTO or a standing commitment (e.g. no Mon/Wed for 3 weeks)."""
    __tablename__ = "availability_block"
    id: Mapped[int] = mapped_column(primary_key=True)
    nurse_id: Mapped[int] = mapped_column(ForeignKey("nurse.id", ondelete="CASCADE"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)  # inclusive
    weekdays: Mapped[list | None] = mapped_column(JSONType, nullable=True)  # None = every day
    reason: Mapped[str] = mapped_column(String(200), default="")


class Period(Base):
    __tablename__ = "period"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    start_date: Mapped[date] = mapped_column(Date)
    num_days: Mapped[int] = mapped_column(Integer)
    min_rest_minutes: Mapped[int] = mapped_column(Integer, default=600)
    holidays: Mapped[list] = mapped_column(JSONType, default=list)  # ISO dates
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    shifts: Mapped[list["Shift"]] = relationship(back_populates="period", order_by="Shift.id")


class Shift(Base):
    __tablename__ = "shift"
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id", ondelete="CASCADE"))
    unit_id: Mapped[int] = mapped_column(ForeignKey("unit.id"))
    date: Mapped[date] = mapped_column(Date)
    day_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # day | night
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_minute: Mapped[int] = mapped_column(Integer)  # minutes since period start (DST-correct)
    end_minute: Mapped[int] = mapped_column(Integer)
    min_nurses: Mapped[int] = mapped_column(Integer)
    max_nurses: Mapped[int] = mapped_column(Integer)
    skill_min: Mapped[dict] = mapped_column(JSONType, default=dict)  # {skill_id: count}
    needs_charge: Mapped[bool] = mapped_column(Boolean, default=True)
    min_charge_seniority: Mapped[int] = mapped_column(Integer, default=0)
    karma_cost: Mapped[int] = mapped_column(Integer, default=1)
    period: Mapped[Period] = relationship(back_populates="shifts")


class ScheduleVersion(Base):
    __tablename__ = "schedule_version"
    __table_args__ = (UniqueConstraint("period_id", "version_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id", ondelete="CASCADE"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("schedule_version.id"), nullable=True)
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|published|superseded
    source: Mapped[str] = mapped_column(String(16))  # solver|edit
    note: Mapped[str] = mapped_column(Text, default="")
    instance_json: Mapped[dict] = mapped_column(JSONType)  # exact snapshot sent to Lean
    instance_hash: Mapped[str] = mapped_column(String(64))
    schedule_hash: Mapped[str] = mapped_column(String(64))
    objective: Mapped[float | None] = mapped_column(Float, nullable=True)
    solver_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    solve_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="version", order_by="Assignment.id", cascade="all, delete-orphan"
    )
    verifications: Mapped[list["Verification"]] = relationship(
        back_populates="version", order_by="Verification.id", cascade="all, delete-orphan"
    )


class Assignment(Base):
    __tablename__ = "assignment"
    __table_args__ = (UniqueConstraint("version_id", "nurse_id", "shift_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("schedule_version.id", ondelete="CASCADE"))
    nurse_id: Mapped[int] = mapped_column(ForeignKey("nurse.id"))
    shift_id: Mapped[int] = mapped_column(ForeignKey("shift.id"))
    is_charge: Mapped[bool] = mapped_column(Boolean, default=False)
    # Real time interval; PostgreSQL enforces no overlap per (version, nurse) via exclusion constraint.
    during: Mapped[Range] = mapped_column(TSTZRANGE)
    version: Mapped[ScheduleVersion] = relationship(back_populates="assignments")


class Verification(Base):
    __tablename__ = "verification"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("schedule_version.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(16))  # fast | kernel
    valid: Mapped[bool] = mapped_column(Boolean)
    violations: Mapped[list] = mapped_column(JSONType, default=list)
    karma: Mapped[list] = mapped_column(JSONType, default=list)
    instance_hash: Mapped[str] = mapped_column(String(64))
    schedule_hash: Mapped[str] = mapped_column(String(64))
    spec_hash: Mapped[str] = mapped_column(String(64))
    lean_version: Mapped[str] = mapped_column(String(200))
    cert_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cert_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    axioms: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[ScheduleVersion] = relationship(back_populates="verifications")


class KarmaEntry(Base):
    """Append-only karma ledger. Balance = SUM(delta) per nurse."""
    __tablename__ = "karma_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    nurse_id: Mapped[int] = mapped_column(ForeignKey("nurse.id"))
    version_id: Mapped[int | None] = mapped_column(ForeignKey("schedule_version.id"), nullable=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
