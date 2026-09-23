"""Build the exact problem instance that is sent to the Lean checker.

This module is part of the trusted boundary: it turns wall-clock data (dates,
timezones, DST, recurring availability) into plain naturals. The JSON field
names match `lean/Nightingale/Types.lean` one-to-one.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import AvailabilityBlock, Nurse, Period, Shift, Unit

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def schedule_json(assignments) -> list[dict]:
    rows = [
        {"nurse": a["nurse"], "shift": a["shift"], "isCharge": bool(a["isCharge"])}
        for a in assignments
    ]
    return sorted(rows, key=lambda r: (r["shift"], r["nurse"]))


# ---------------------------------------------------------------------------
# Period / shift generation
# ---------------------------------------------------------------------------

def period_start_utc(period_start: date) -> datetime:
    tz = ZoneInfo(config.HOSPITAL_TZ)
    return datetime.combine(period_start, time(0, 0), tzinfo=tz)


def minutes_between(a: datetime, b: datetime) -> int:
    """Real elapsed minutes. Convert to UTC first: Python subtracts datetimes that
    share a tzinfo as wall-clock times, which would ignore DST transitions."""
    return int((b.astimezone(timezone.utc) - a.astimezone(timezone.utc)).total_seconds() // 60)


def shift_karma_cost(kind: str, d: date, holidays: set[str], scarcity: int) -> int:
    """How 'expensive' a shift is: undesirable timing plus hard-to-staff skills."""
    cost = 1
    if kind == "night":
        cost += 2
    if d.weekday() >= 5:
        cost += 2
    if d.weekday() == 4:
        cost += 1
    if d.isoformat() in holidays:
        cost += 3
    return cost + scarcity


def create_period(session: Session, name: str, start: date, num_days: int,
                  min_rest_minutes: int = 600, holidays: list[str] | None = None) -> Period:
    tz = ZoneInfo(config.HOSPITAL_TZ)
    period = Period(name=name, start_date=start, num_days=num_days,
                    min_rest_minutes=min_rest_minutes, holidays=holidays or [])
    session.add(period)
    session.flush()
    origin = period_start_utc(start)
    hol = set(period.holidays)
    units = session.scalars(select(Unit).order_by(Unit.id)).all()
    nurses = session.scalars(select(Nurse).where(Nurse.active, ~Nurse.is_agency)).all()
    for unit in units:
        unit_nurses = [n for n in nurses if n.unit_id == unit.id]
        for kind, tpl in sorted(unit.templates.items()):
            skill_min = {str(k): int(v) for k, v in tpl.get("skill_min", {}).items()}
            scarcity = 0
            for k, c in skill_min.items():
                supply = sum(1 for n in unit_nurses if int(k) in n.skills)
                scarcity = max(scarcity, -(-4 * c // max(supply, 1)))  # ceil(4c/supply)
            hh, mm = map(int, tpl["start"].split(":"))
            for d_idx in range(num_days):
                d = start + timedelta(days=d_idx)
                starts = datetime.combine(d, time(hh, mm), tzinfo=tz)
                # Wall-clock end: e.g. 19:00 -> 07:00 next day. Real duration is DST-aware.
                end_wall = datetime.combine(d, time(hh, mm)) + timedelta(hours=tpl["hours"])
                ends = end_wall.replace(tzinfo=tz)
                session.add(Shift(
                    period_id=period.id, unit_id=unit.id, date=d, day_index=d_idx, kind=kind,
                    starts_at=starts, ends_at=ends,
                    start_minute=minutes_between(origin, starts),
                    end_minute=minutes_between(origin, ends),
                    min_nurses=tpl["min"], max_nurses=tpl["max"], skill_min=skill_min,
                    needs_charge=tpl.get("charge", True),
                    min_charge_seniority=tpl.get("charge_seniority", 0),
                    karma_cost=shift_karma_cost(kind, d, hol, scarcity),
                ))
    session.flush()
    return period


# ---------------------------------------------------------------------------
# Preferences / availability -> (nurse, shift) pairs
# ---------------------------------------------------------------------------

def block_applies(block: AvailabilityBlock, shift: Shift) -> bool:
    if not (block.start_date <= shift.date <= block.end_date):
        return False
    return block.weekdays is None or shift.date.weekday() in block.weekdays


def preference_of(nurse: Nurse, shift: Shift) -> str:
    """'liked' | 'disliked' | 'neutral' — disliked wins over liked."""
    p = nurse.preferences or {}
    wd = shift.date.weekday()
    kind_pref = p.get("preferred_kind")
    if wd in p.get("disliked_weekdays", []) or (kind_pref and shift.kind != kind_pref):
        return "disliked"
    if wd in p.get("liked_weekdays", []) or (kind_pref and shift.kind == kind_pref):
        return "liked"
    return "neutral"


def build_instance(session: Session, period: Period) -> dict:
    nurses = session.scalars(select(Nurse).where(Nurse.active).order_by(Nurse.id)).all()
    shifts = session.scalars(
        select(Shift).where(Shift.period_id == period.id).order_by(Shift.id)).all()
    blocks = session.scalars(select(AvailabilityBlock)).all()
    blocks_by_nurse: dict[int, list[AvailabilityBlock]] = {}
    for b in blocks:
        blocks_by_nurse.setdefault(b.nurse_id, []).append(b)

    unavailable, likes, dislikes = [], [], []
    for n in nurses:
        for sh in shifts:
            if any(block_applies(b, sh) for b in blocks_by_nurse.get(n.id, [])):
                unavailable.append({"nurse": n.id, "shift": sh.id})
            if not n.is_agency and sh.unit_id == n.unit_id:
                pref = preference_of(n, sh)
                if pref == "liked":
                    likes.append({"nurse": n.id, "shift": sh.id})
                elif pref == "disliked":
                    dislikes.append({"nurse": n.id, "shift": sh.id})

    return {
        "nurses": [{
            "id": n.id, "unit": n.unit_id, "skills": sorted(n.skills),
            "seniority": n.seniority_years, "chargeQualified": n.charge_qualified,
            "maxMinutesPerWeek": n.max_minutes_per_week,
            "maxConsecutiveDays": n.max_consecutive_days,
        } for n in nurses],
        "shifts": [{
            "id": s.id, "unit": s.unit_id, "day": s.day_index,
            "start": s.start_minute, "stop": s.end_minute,
            "minNurses": s.min_nurses, "maxNurses": s.max_nurses,
            "skillMin": [{"skill": int(k), "count": v} for k, v in sorted(s.skill_min.items(), key=lambda kv: int(kv[0]))],
            "needsCharge": s.needs_charge, "minChargeSeniority": s.min_charge_seniority,
            "karmaCost": s.karma_cost,
        } for s in shifts],
        "unavailable": unavailable,
        "minRestMinutes": period.min_rest_minutes,
        "likes": likes,
        "dislikes": dislikes,
    }
