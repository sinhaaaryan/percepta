"""Pure-Python mirror of the Lean spec and karma function.

NOT trusted — Lean is the judge. This exists for differential testing (the
Python mirror, the Lean checker and the solver must agree) and to give the
solver cheap helpers such as `separated`.
"""
from __future__ import annotations

from collections import defaultdict


def index(instance: dict):
    nurses = {n["id"]: n for n in instance["nurses"]}
    shifts = {s["id"]: s for s in instance["shifts"]}
    return nurses, shifts


def separated(p: dict, q: dict, rest: int) -> bool:
    return p["stop"] + rest <= q["start"] or q["stop"] + rest <= p["start"]


def violated_rules(instance: dict, schedule: list[dict]) -> set[str]:
    nurses, shifts = index(instance)
    bad: set[str] = set()
    ids_n = [n["id"] for n in instance["nurses"]]
    ids_s = [s["id"] for s in instance["shifts"]]
    pairs = [(a["nurse"], a["shift"]) for a in schedule]
    if (len(set(ids_n)) != len(ids_n) or len(set(ids_s)) != len(ids_s)
            or any(s["start"] >= s["stop"] for s in instance["shifts"])
            or any(a["nurse"] not in nurses or a["shift"] not in shifts for a in schedule)
            or len(set(pairs)) != len(pairs)):
        bad.add("well_formed")

    on_shift = defaultdict(list)
    for a in schedule:
        on_shift[a["shift"]].append(a)
    for s in instance["shifts"]:
        staff = on_shift[s["id"]]
        if not (s["minNurses"] <= len(staff) <= s["maxNurses"]):
            bad.add("coverage")
        for req in s["skillMin"]:
            have = sum(1 for a in staff if a["nurse"] in nurses and req["skill"] in nurses[a["nurse"]]["skills"])
            if have < req["count"]:
                bad.add("skill_mix")
        if s["needsCharge"] and sum(1 for a in staff if a["isCharge"]) != 1:
            bad.add("charge")
    for a in schedule:
        if a["isCharge"]:
            s, n = shifts.get(a["shift"]), nurses.get(a["nurse"])
            if not (s and s["needsCharge"] and n and n["unit"] == s["unit"] and n["chargeQualified"]
                    and s["minChargeSeniority"] <= n["seniority"]):
                bad.add("charge")

    blocked = {(p["nurse"], p["shift"]) for p in instance["unavailable"]}
    if any((a["nurse"], a["shift"]) in blocked for a in schedule):
        bad.add("availability")

    by_nurse = defaultdict(list)
    for a in schedule:
        by_nurse[a["nurse"]].append(a)
    rest = instance["minRestMinutes"]
    for mine in by_nurse.values():
        for a in mine:
            for b in mine:
                if a["shift"] != b["shift"]:
                    p, q = shifts.get(a["shift"]), shifts.get(b["shift"])
                    if not (p and q and separated(p, q, rest)):
                        bad.add("rest")

    weeks = max((s["day"] // 7 for s in instance["shifts"]), default=0) + 1
    for n in instance["nurses"]:
        mine = [shifts[a["shift"]] for a in by_nurse[n["id"]] if a["shift"] in shifts]
        for w in range(weeks):
            if sum(s["stop"] - s["start"] for s in mine if s["day"] // 7 == w) > n["maxMinutesPerWeek"]:
                bad.add("weekly_hours")
        days = {s["day"] for s in mine}
        for d in days:
            if all(d + j in days for j in range(n["maxConsecutiveDays"] + 1)):
                bad.add("max_consecutive")
    return bad


def karma_earned(instance: dict, schedule: list[dict]) -> dict[int, int]:
    _, shifts = index(instance)
    likes = {(p["nurse"], p["shift"]) for p in instance["likes"]}
    dislikes = {(p["nurse"], p["shift"]) for p in instance["dislikes"]}
    out = {n["id"]: 0 for n in instance["nurses"]}
    for a in schedule:
        s = shifts.get(a["shift"])
        if s is None or a["nurse"] not in out:
            continue
        key = (a["nurse"], a["shift"])
        weight = 2 if key in dislikes else 0 if key in likes else 1
        out[a["nurse"]] += weight * s["karmaCost"]
    return out
