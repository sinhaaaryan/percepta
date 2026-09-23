"""Nightingale-style verified nurse scheduling API."""
from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from . import config, lean, services
from .db import SessionLocal, init_db
from .instance import create_period
from .models import (AvailabilityBlock, KarmaEntry, Nurse, Period, ScheduleVersion, Shift, Skill,
                     Unit)

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Nightingale (verified)", version="0.1.0", lifespan=lifespan)


def get_db():
    with SessionLocal() as s:
        yield s


@app.exception_handler(services.WorkflowError)
def _workflow_error(_, exc: services.WorkflowError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), **exc.detail})


def _commit(db: Session) -> None:
    try:
        db.commit()
    except (IntegrityError, DBAPIError) as e:
        db.rollback()
        msg = str(e.orig) if getattr(e, "orig", None) else str(e)
        if "assignment_no_overlap" in msg:
            msg = "nurse would be double-booked (database exclusion constraint)"
        raise HTTPException(409, msg.splitlines()[0])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PeriodIn(BaseModel):
    name: str
    start_date: date
    num_days: int = Field(28, ge=1, le=84)
    min_rest_minutes: int = 600
    holidays: list[date] = []


class SolveIn(BaseModel):
    time_limit: float = Field(20, gt=0, le=300)


class EditOp(BaseModel):
    op: str  # assign | unassign | set_charge
    nurse: int
    shift: int
    isCharge: bool = False


class EditsIn(BaseModel):
    ops: list[EditOp]
    note: str = ""


class AvailabilityIn(BaseModel):
    start_date: date
    end_date: date
    weekdays: list[int] | None = None
    reason: str = ""


class PreferencesIn(BaseModel):
    preferred_kind: str | None = None
    liked_weekdays: list[int] = []
    disliked_weekdays: list[int] = []


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def nurse_out(n: Nurse, balance: int = 0) -> dict:
    return {"id": n.id, "name": n.name, "unit_id": n.unit_id, "skills": n.skills,
            "seniority_years": n.seniority_years, "charge_qualified": n.charge_qualified,
            "max_minutes_per_week": n.max_minutes_per_week,
            "max_consecutive_days": n.max_consecutive_days,
            "target_shifts_per_week": n.target_shifts_per_week, "is_agency": n.is_agency,
            "preferences": n.preferences, "karma": balance}


def shift_out(s: Shift) -> dict:
    return {"id": s.id, "unit_id": s.unit_id, "date": s.date.isoformat(), "day_index": s.day_index,
            "kind": s.kind, "starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat(),
            "minutes": s.end_minute - s.start_minute, "min_nurses": s.min_nurses,
            "max_nurses": s.max_nurses, "skill_min": s.skill_min, "needs_charge": s.needs_charge,
            "min_charge_seniority": s.min_charge_seniority, "karma_cost": s.karma_cost}


def verification_out(v) -> dict:
    return {"id": v.id, "mode": v.mode, "valid": v.valid, "violations": v.violations,
            "duration_ms": v.duration_ms, "spec_hash": v.spec_hash, "lean_version": v.lean_version,
            "cert_sha256": v.cert_sha256, "axioms": v.axioms, "instance_hash": v.instance_hash,
            "schedule_hash": v.schedule_hash, "created_at": v.created_at.isoformat()}


def version_summary(v: ScheduleVersion) -> dict:
    fast = [x for x in v.verifications if x.mode == "fast"]
    kernel = [x for x in v.verifications if x.mode == "kernel"]
    return {"id": v.id, "period_id": v.period_id, "version_no": v.version_no, "status": v.status,
            "source": v.source, "note": v.note, "parent_id": v.parent_id,
            "created_at": v.created_at.isoformat(),
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "valid": fast[-1].valid if fast else None,
            "certified": bool(kernel and kernel[-1].valid),
            "violation_count": len(fast[-1].violations) if fast else None,
            "objective": v.objective, "solver_status": v.solver_status,
            "solve_seconds": v.solve_seconds}


def version_detail(db: Session, v: ScheduleVersion) -> dict:
    sched = services.schedule_of(v)
    inst = v.instance_json
    agency = {n.id for n in db.scalars(select(Nurse).where(Nurse.is_agency))}
    likes = {(p["nurse"], p["shift"]) for p in inst["likes"]}
    dislikes = {(p["nurse"], p["shift"]) for p in inst["dislikes"]}
    prefs = Counter("liked" if (a["nurse"], a["shift"]) in likes else
                    "disliked" if (a["nurse"], a["shift"]) in dislikes else "neutral"
                    for a in sched if a["nurse"] not in agency)
    fast = [x for x in v.verifications if x.mode == "fast"]
    return {
        **version_summary(v),
        "instance_hash": v.instance_hash, "schedule_hash": v.schedule_hash,
        "assignments": sched,
        "verifications": [verification_out(x) for x in v.verifications],
        "karma_earned": fast[-1].karma if fast else [],
        "unavailable": inst["unavailable"], "likes": inst["likes"], "dislikes": inst["dislikes"],
        "stats": {"assignments": len(sched),
                  "agency_shifts": sum(1 for a in sched if a["nurse"] in agency),
                  "preferences": dict(prefs)},
    }


def get_or_404(db: Session, model, id_: int):
    obj = db.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {id_} not found")
    return obj


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "lean": lean.lean_version(), "spec_hash": lean.spec_hash(),
            "checker": config.CHECKER_BIN.exists()}


@app.get("/api/spec", response_class=PlainTextResponse)
def spec():
    return (config.LEAN_DIR / "Nightingale/Spec.lean").read_text()


@app.get("/api/meta")
def meta(db: Session = Depends(get_db)):
    bal = services.karma_balances(db)
    return {
        "units": [{"id": u.id, "code": u.code, "name": u.name, "templates": u.templates}
                  for u in db.scalars(select(Unit).order_by(Unit.id))],
        "skills": [{"id": s.id, "code": s.code, "name": s.name}
                   for s in db.scalars(select(Skill).order_by(Skill.id))],
        "nurses": [nurse_out(n, bal.get(n.id, 0))
                   for n in db.scalars(select(Nurse).where(Nurse.active).order_by(Nurse.unit_id, Nurse.is_agency, Nurse.name))],
        "periods": [{"id": p.id, "name": p.name, "start_date": p.start_date.isoformat(),
                     "num_days": p.num_days} for p in db.scalars(select(Period).order_by(Period.id))],
    }


@app.post("/api/periods")
def new_period(body: PeriodIn, db: Session = Depends(get_db)):
    p = create_period(db, body.name, body.start_date, body.num_days, body.min_rest_minutes,
                      [d.isoformat() for d in body.holidays])
    _commit(db)
    return {"id": p.id}


@app.get("/api/periods/{period_id}")
def period_detail(period_id: int, db: Session = Depends(get_db)):
    p = get_or_404(db, Period, period_id)
    versions = db.scalars(select(ScheduleVersion).where(ScheduleVersion.period_id == p.id)
                          .order_by(ScheduleVersion.version_no.desc())).all()
    return {"id": p.id, "name": p.name, "start_date": p.start_date.isoformat(),
            "num_days": p.num_days, "min_rest_minutes": p.min_rest_minutes,
            "shifts": [shift_out(s) for s in p.shifts],
            "versions": [version_summary(v) for v in versions]}


@app.post("/api/periods/{period_id}/solve")
def solve_period(period_id: int, body: SolveIn = SolveIn(), db: Session = Depends(get_db)):
    p = get_or_404(db, Period, period_id)
    v = services.run_solver(db, p, body.time_limit)
    _commit(db)
    return version_detail(db, v)


@app.get("/api/versions/{version_id}")
def get_version(version_id: int, db: Session = Depends(get_db)):
    return version_detail(db, get_or_404(db, ScheduleVersion, version_id))


@app.post("/api/versions/{version_id}/edits")
def edit_version(version_id: int, body: EditsIn, db: Session = Depends(get_db)):
    parent = get_or_404(db, ScheduleVersion, version_id)
    try:
        v = services.apply_edits(db, parent, [op.model_dump() for op in body.ops], body.note)
    except (IntegrityError, DBAPIError):
        db.rollback()
        raise HTTPException(409, "nurse would be double-booked (database exclusion constraint)")
    _commit(db)
    return version_detail(db, v)


@app.post("/api/versions/{version_id}/verify")
def verify_version(version_id: int, mode: str = "fast", db: Session = Depends(get_db)):
    v = get_or_404(db, ScheduleVersion, version_id)
    r = services.verify(db, v, mode)
    _commit(db)
    return verification_out(r)


@app.post("/api/versions/{version_id}/publish")
def publish_version(version_id: int, db: Session = Depends(get_db)):
    v = get_or_404(db, ScheduleVersion, version_id)
    try:
        services.publish(db, v)
    except services.WorkflowError:
        # Keep the failed certificate attempt for the audit trail.
        _commit(db)
        raise
    _commit(db)
    return version_detail(db, v)


@app.get("/api/versions/{version_id}/certificate", response_class=PlainTextResponse)
def certificate(version_id: int, db: Session = Depends(get_db)):
    v = get_or_404(db, ScheduleVersion, version_id)
    kernel = [x for x in v.verifications if x.mode == "kernel" and x.cert_path]
    if not kernel or not Path(kernel[-1].cert_path).exists():
        raise HTTPException(404, "no certificate for this version; publish it first")
    return Path(kernel[-1].cert_path).read_text()


@app.get("/api/karma")
def karma(db: Session = Depends(get_db)):
    bal = services.karma_balances(db)
    ledger = db.scalars(select(KarmaEntry).order_by(KarmaEntry.id.desc()).limit(200)).all()
    return {"balances": bal,
            "recent": [{"nurse_id": e.nurse_id, "version_id": e.version_id, "delta": e.delta,
                        "reason": e.reason, "created_at": e.created_at.isoformat()} for e in ledger]}


@app.post("/api/nurses/{nurse_id}/availability")
def add_availability(nurse_id: int, body: AvailabilityIn, db: Session = Depends(get_db)):
    get_or_404(db, Nurse, nurse_id)
    b = AvailabilityBlock(nurse_id=nurse_id, **body.model_dump())
    db.add(b)
    _commit(db)
    return {"id": b.id}


@app.get("/api/nurses/{nurse_id}/availability")
def list_availability(nurse_id: int, db: Session = Depends(get_db)):
    return [{"id": b.id, "start_date": b.start_date.isoformat(), "end_date": b.end_date.isoformat(),
             "weekdays": b.weekdays, "reason": b.reason}
            for b in db.scalars(select(AvailabilityBlock).where(AvailabilityBlock.nurse_id == nurse_id))]


@app.put("/api/nurses/{nurse_id}/preferences")
def set_preferences(nurse_id: int, body: PreferencesIn, db: Session = Depends(get_db)):
    n = get_or_404(db, Nurse, nurse_id)
    n.preferences = body.model_dump()
    _commit(db)
    return nurse_out(n)


# Serve the built React app, if present.
if config.FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        return FileResponse(config.FRONTEND_DIST / "index.html")
