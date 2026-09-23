"""Scheduling workflow: solve -> verify (Lean) -> edit -> publish (Lean certificate)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session

from . import lean, reference
from .instance import build_instance, schedule_json, sha256
from .models import Assignment, KarmaEntry, Nurse, Period, ScheduleVersion, Shift, Verification
from .solver import solve


class WorkflowError(Exception):
    def __init__(self, message: str, status: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail or {}


def karma_balances(session: Session) -> dict[int, int]:
    rows = session.execute(select(KarmaEntry.nurse_id, func.sum(KarmaEntry.delta))
                           .group_by(KarmaEntry.nurse_id)).all()
    return {nid: int(total) for nid, total in rows}


def nurse_meta(session: Session) -> dict[int, dict]:
    bal = karma_balances(session)
    return {n.id: {"is_agency": n.is_agency, "target_shifts_per_week": n.target_shifts_per_week,
                   "karma": bal.get(n.id, 0)}
            for n in session.scalars(select(Nurse).where(Nurse.active))}


def schedule_of(version: ScheduleVersion) -> list[dict]:
    return schedule_json({"nurse": a.nurse_id, "shift": a.shift_id, "isCharge": a.is_charge}
                         for a in version.assignments)


def latest_version(session: Session, period_id: int) -> ScheduleVersion | None:
    return session.scalars(select(ScheduleVersion).where(ScheduleVersion.period_id == period_id)
                           .order_by(ScheduleVersion.version_no.desc()).limit(1)).first()


def verify(session: Session, version: ScheduleVersion, mode: str = "fast") -> Verification:
    sched = schedule_of(version)
    inst = version.instance_json
    if mode == "fast":
        r = lean.fast_check(inst, sched)
    elif mode == "kernel":
        r = lean.kernel_certify(inst, sched, f"period{version.period_id}_v{version.version_no}",
                                version.instance_hash, version.schedule_hash)
    else:
        raise WorkflowError(f"unknown verification mode {mode}")
    row = Verification(
        mode=mode, valid=r.valid, violations=r.violations,
        karma=[{"nurse": k, "earned": v} for k, v in sorted(r.karma.items())],
        instance_hash=version.instance_hash, schedule_hash=version.schedule_hash,
        spec_hash=lean.spec_hash(), lean_version=lean.lean_version(), cert_sha256=r.cert_sha256,
        cert_path=r.cert_path, axioms=r.axioms, duration_ms=r.duration_ms,
    )
    version.verifications.append(row)
    session.flush()
    return row


def create_version(session: Session, period: Period, assignments: list[dict], source: str,
                   parent: ScheduleVersion | None = None, note: str = "",
                   solver_status: str | None = None, objective: float | None = None,
                   solve_seconds: float | None = None) -> ScheduleVersion:
    inst = build_instance(session, period)
    sched = schedule_json(assignments)
    last = latest_version(session, period.id)
    version = ScheduleVersion(
        period_id=period.id, parent_id=parent.id if parent else None,
        version_no=(last.version_no + 1) if last else 1, status="draft", source=source, note=note,
        instance_json=inst, instance_hash=sha256(inst), schedule_hash=sha256(sched),
        objective=objective, solver_status=solver_status, solve_seconds=solve_seconds,
    )
    session.add(version)
    session.flush()
    shifts = {s.id: s for s in session.scalars(select(Shift).where(Shift.period_id == period.id))}
    for a in sched:
        sh = shifts.get(a["shift"])
        if sh is None:
            raise WorkflowError(f"shift {a['shift']} is not in period {period.id}", 422)
        version.assignments.append(Assignment(
            nurse_id=a["nurse"], shift_id=a["shift"], is_charge=a["isCharge"],
            during=Range(sh.starts_at, sh.ends_at, bounds="[)")))
    session.flush()  # PostgreSQL exclusion constraint fires here on double-booking
    verify(session, version, "fast")
    return version


def run_solver(session: Session, period: Period, time_limit: float = 20.0) -> ScheduleVersion:
    inst = build_instance(session, period)
    prev = latest_version(session, period.id)
    result = solve(inst, nurse_meta(session), time_limit=time_limit,
                   hint=schedule_of(prev) if prev else None)
    if not result.assignments:
        raise WorkflowError(f"solver found no schedule (status {result.status})", 422)
    return create_version(session, period, result.assignments, "solver", parent=prev,
                          note=f"CP-SAT {result.status}", solver_status=result.status,
                          objective=result.objective, solve_seconds=round(result.seconds, 2))


def apply_edits(session: Session, parent: ScheduleVersion, ops: list[dict], note: str = "") -> ScheduleVersion:
    """ops: {"op": "assign"|"unassign"|"set_charge", "nurse": id, "shift": id, "isCharge": bool}"""
    current = {(a["nurse"], a["shift"]): a for a in schedule_of(parent)}
    for op in ops:
        key = (op["nurse"], op["shift"])
        kind = op["op"]
        if kind == "assign":
            current[key] = {"nurse": key[0], "shift": key[1], "isCharge": bool(op.get("isCharge", False))}
        elif kind == "unassign":
            current.pop(key, None)
        elif kind == "set_charge":
            if key not in current:
                raise WorkflowError(f"nurse {key[0]} is not on shift {key[1]}", 422)
            current[key]["isCharge"] = bool(op.get("isCharge", True))
        else:
            raise WorkflowError(f"unknown op {kind}", 422)
    period = session.get(Period, parent.period_id)
    return create_version(session, period, list(current.values()), "edit", parent=parent, note=note)


def publish(session: Session, version: ScheduleVersion) -> ScheduleVersion:
    if version.status != "draft":
        raise WorkflowError(f"version is {version.status}, only drafts can be published", 409)
    fast = [v for v in version.verifications if v.mode == "fast"]
    if not fast or not fast[-1].valid:
        raise WorkflowError("schedule is not valid; fix the violations first", 409,
                            {"violations": fast[-1].violations if fast else []})
    kernel = verify(session, version, "kernel")
    if not kernel.valid:
        raise WorkflowError("Lean certificate check failed", 409, {"violations": kernel.violations})

    # Karma: Lean's karma function is the reference; the Python mirror must agree.
    lean_karma = {row["nurse"]: row["earned"] for row in fast[-1].karma}
    mirror = reference.karma_earned(version.instance_json, schedule_of(version))
    if lean_karma != mirror:
        raise WorkflowError("karma mismatch between Lean reference and Python mirror", 500)

    for old in session.scalars(select(ScheduleVersion).where(
            ScheduleVersion.period_id == version.period_id, ScheduleVersion.status == "published")):
        old.status = "superseded"
        # Reverse the superseded version's karma so a period is only ever counted once.
        for e in session.scalars(select(KarmaEntry).where(KarmaEntry.version_id == old.id,
                                                          KarmaEntry.delta != 0)).all():
            if not e.reason.startswith("reversal"):
                session.add(KarmaEntry(nurse_id=e.nurse_id, version_id=version.id, delta=-e.delta,
                                       reason=f"reversal: v{old.version_no} superseded"))
    for nid, earned in sorted(lean_karma.items()):
        if earned:
            session.add(KarmaEntry(nurse_id=nid, version_id=version.id, delta=earned,
                                   reason=f"period {version.period_id} v{version.version_no}"))
    version.status = "published"
    version.published_at = datetime.now(timezone.utc)
    session.flush()  # DB trigger re-checks the certificate exists
    return version
