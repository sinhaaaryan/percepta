"""Demo data modeled on the blog: ~90 nurses (Tupta's "90 employees") across three
units, a 4-week period that crosses the DST change, standing commitments, PTO,
preferences and carried-over karma."""
from __future__ import annotations

import random
from datetime import date

from sqlalchemy.orm import Session

from .db import SessionLocal, drop_all, engine, init_db
from .instance import create_period
from .models import AvailabilityBlock, KarmaEntry, Nurse, Skill, Unit

FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Drew",
         "Sam", "Reese", "Harper", "Rowan", "Emerson", "Finley", "Skyler", "Dakota", "Hayden", "Parker",
         "Kendall", "Logan", "Peyton", "Cameron", "Sage", "Blake", "Elliot", "Remy", "Marlowe", "Shay"]
LAST = ["Nguyen", "Patel", "Garcia", "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis",
        "Lopez", "Wilson", "Anderson", "Thomas", "Moore", "Martin", "Lee", "Thompson", "White", "Harris",
        "Clark", "Lewis", "Robinson", "Walker", "Young", "Allen", "King", "Wright", "Scott", "Hill"]

SKILLS = [("ACLS", "Advanced Cardiac Life Support"), ("CCRN", "Critical Care RN"),
          ("TELE", "Telemetry / cardiac monitoring"), ("PREC", "Preceptor")]

UNITS = [
    ("MS", "4 West Med-Surg", {
        "day": {"start": "07:00", "hours": 12, "min": 5, "max": 7, "skill_min": {"1": 2},
                "charge": True, "charge_seniority": 3},
        "night": {"start": "19:00", "hours": 12, "min": 4, "max": 6, "skill_min": {"1": 1},
                  "charge": True, "charge_seniority": 3}}),
    ("ICU", "Medical ICU", {
        "day": {"start": "07:00", "hours": 12, "min": 5, "max": 6, "skill_min": {"1": 5, "2": 3},
                "charge": True, "charge_seniority": 5},
        "night": {"start": "19:00", "hours": 12, "min": 4, "max": 5, "skill_min": {"1": 4, "2": 2},
                  "charge": True, "charge_seniority": 5}}),
    ("TELE", "Cardiac Telemetry", {
        "day": {"start": "07:00", "hours": 12, "min": 4, "max": 6, "skill_min": {"3": 2},
                "charge": True, "charge_seniority": 3},
        "night": {"start": "19:00", "hours": 12, "min": 4, "max": 5, "skill_min": {"3": 2},
                  "charge": True, "charge_seniority": 3}}),
]

PERIOD_START = date(2026, 10, 19)  # 4 weeks, crosses DST end on 2026-11-01


def seed(session: Session, nurses_per_unit: int = 30, agency_per_unit: int = 4, rng_seed: int = 42,
         period_days: int = 28) -> None:
    rng = random.Random(rng_seed)
    for code, name in SKILLS:
        session.add(Skill(code=code, name=name))
    units = []
    for code, name, tpl in UNITS:
        u = Unit(code=code, name=name, templates=tpl)
        session.add(u)
        units.append(u)
    session.flush()

    names = [f"{f} {l}" for f in FIRST for l in LAST]
    rng.shuffle(names)
    staff: list[Nurse] = []
    for u in units:
        for k in range(nurses_per_unit):
            seniority = rng.choice([0, 1, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20])
            skills = [1] if (u.code == "ICU" or rng.random() < 0.6) else []
            if u.code == "ICU" and rng.random() < 0.6:
                skills.append(2)
            if u.code == "TELE" and rng.random() < 0.55:
                skills.append(3)
            if seniority >= 5 and rng.random() < 0.5:
                skills.append(4)
            threshold = u.templates["day"]["charge_seniority"]
            charge = seniority >= threshold and (rng.random() < 0.65 or k < 3)
            kind = rng.choices(["day", "night", None], weights=[55, 25, 20])[0]
            liked = sorted(rng.sample(range(7), rng.choice([0, 2, 3])))
            disliked = sorted(set(rng.sample([4, 5, 6, 0], rng.choice([0, 1, 1, 2]))) - set(liked))
            part_time = rng.random() < 0.15
            n = Nurse(name=names.pop(), unit_id=u.id, skills=sorted(skills), seniority_years=seniority,
                      charge_qualified=charge, max_minutes_per_week=40 * 60, max_consecutive_days=3,
                      target_shifts_per_week=2 if part_time else 3,
                      preferences={"preferred_kind": kind, "liked_weekdays": liked,
                                   "disliked_weekdays": disliked})
            session.add(n)
            staff.append(n)
        for k in range(agency_per_unit):
            skills = {"MS": [1], "ICU": [1, 2], "TELE": [1, 3]}[u.code]
            session.add(Nurse(name=f"Agency {u.code} #{k + 1}", unit_id=u.id, skills=skills,
                              seniority_years=0, charge_qualified=False, max_minutes_per_week=40 * 60,
                              max_consecutive_days=3, target_shifts_per_week=0, is_agency=True,
                              preferences={}))
    session.flush()

    # The blog's example: can't work Mondays and Wednesdays for the next three weeks.
    commit_nurse = staff[1]
    session.add(AvailabilityBlock(nurse_id=commit_nurse.id, start_date=PERIOD_START,
                                  end_date=date(2026, 11, 8), weekdays=[0, 2],
                                  reason="Standing commitment: no Mon/Wed for 3 weeks"))
    # Some PTO.
    for n in rng.sample(staff, 12):
        start = PERIOD_START.toordinal() + rng.randrange(0, 22)
        session.add(AvailabilityBlock(nurse_id=n.id, start_date=date.fromordinal(start),
                                      end_date=date.fromordinal(start + rng.randrange(2, 6)),
                                      weekdays=None, reason="PTO"))
    # Karma carried over from previous periods.
    for n in staff:
        bal = rng.randrange(0, 80)
        if bal:
            session.add(KarmaEntry(nurse_id=n.id, version_id=None, delta=bal,
                                   reason="carried over from previous periods"))
    create_period(session, "Oct 19 – Nov 15, 2026", PERIOD_START, period_days,
                  min_rest_minutes=600, holidays=[])


def main() -> None:
    drop_all()
    init_db()
    with SessionLocal() as s:
        seed(s)
        s.commit()
    print("seeded", engine.url.render_as_string(hide_password=True))


if __name__ == "__main__":
    main()
