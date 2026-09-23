"""CP-SAT optimizer. UNTRUSTED: every schedule it produces is re-checked by Lean.

Hard constraints mirror `Valid` in lean/Nightingale/Spec.lean. Soft objective:
  * agency (and overtime) usage — the blog's main cost driver
  * nurses' target hours (don't leave staff idle while paying agency)
  * preferences, weighted by karma (nurses owed more get preferences first)
  * fairness of expensive shifts (nights / weekends / Fridays / holidays)
  * stable weekly patterns ("wanting to work the same days each week")
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .reference import separated

AGENCY_COST = 1000
UNDER_TARGET_COST = 60
DISLIKE_COST = 12
LIKE_REWARD = 4
FAIRNESS_COST = 25
PATTERN_COST = 3
EXPENSIVE_THRESHOLD = 4


@dataclass
class SolveResult:
    status: str
    assignments: list[dict]
    objective: float | None
    seconds: float


def solve(instance: dict, nurse_meta: dict[int, dict], time_limit: float = 20.0,
          workers: int = 8, hint: list[dict] | None = None) -> SolveResult:
    """nurse_meta[id] = {"is_agency": bool, "target_shifts_per_week": int, "karma": int}"""
    t0 = time.time()
    m = cp_model.CpModel()
    nurses = instance["nurses"]
    shifts = instance["shifts"]
    shift_by_id = {s["id"]: s for s in shifts}
    blocked = {(p["nurse"], p["shift"]) for p in instance["unavailable"]}
    likes = {(p["nurse"], p["shift"]) for p in instance["likes"]}
    dislikes = {(p["nurse"], p["shift"]) for p in instance["dislikes"]}
    rest = instance["minRestMinutes"]
    num_days = max((s["day"] for s in shifts), default=0) + 1
    weeks = num_days // 7 + (1 if num_days % 7 else 0)

    # Relative karma -> preference weight. Nurses owed more karma get their
    # preferences weighted more heavily.
    karmas = [nurse_meta.get(n["id"], {}).get("karma", 0) for n in nurses if not nurse_meta.get(n["id"], {}).get("is_agency")]
    mean_k = sum(karmas) / len(karmas) if karmas else 0

    def pref_weight(nid: int) -> int:
        rel = nurse_meta.get(nid, {}).get("karma", 0) - mean_k
        return max(1, min(6, 2 + round(rel / 15)))

    x: dict[tuple[int, int], cp_model.IntVar] = {}
    c: dict[tuple[int, int], cp_model.IntVar] = {}
    nurse_by_id = {n["id"]: n for n in nurses}
    for n in nurses:
        for s in shifts:
            # Nurses work their home unit only (agency nurses are attached to a unit).
            if n["unit"] != s["unit"] or (n["id"], s["id"]) in blocked:
                continue
            x[n["id"], s["id"]] = m.NewBoolVar(f"x_{n['id']}_{s['id']}")
            if (s["needsCharge"] and n["chargeQualified"]
                    and n["seniority"] >= s["minChargeSeniority"]):
                c[n["id"], s["id"]] = m.NewBoolVar(f"c_{n['id']}_{s['id']}")
                m.AddImplication(c[n["id"], s["id"]], x[n["id"], s["id"]])

    by_shift = defaultdict(list)
    by_nurse = defaultdict(list)
    for (nid, sid), v in x.items():
        by_shift[sid].append((nid, v))
        by_nurse[nid].append((sid, v))

    for s in shifts:
        staff = by_shift[s["id"]]
        total = sum(v for _, v in staff)
        m.Add(total >= s["minNurses"])
        m.Add(total <= s["maxNurses"])
        for req in s["skillMin"]:
            m.Add(sum(v for nid, v in staff if req["skill"] in nurse_by_id[nid]["skills"]) >= req["count"])
        if s["needsCharge"]:
            m.AddExactlyOne([c[nid, s["id"]] for nid, _ in staff if (nid, s["id"]) in c])

    # Rest / no overlap: pairwise conflicts between shifts in a unit.
    shifts_by_unit = defaultdict(list)
    for s in shifts:
        shifts_by_unit[s["unit"]].append(s)
    conflicts: dict[int, list[tuple[int, int]]] = {}
    for u, lst in shifts_by_unit.items():
        conflicts[u] = [(p["id"], q["id"]) for i, p in enumerate(lst) for q in lst[i + 1:]
                        if not separated(p, q, rest)]

    objective = []
    for n in nurses:
        nid = n["id"]
        meta = nurse_meta.get(nid, {})
        mine = dict(by_nurse[nid])
        if not mine:
            continue
        for a, b in conflicts.get(n["unit"], []):
            if a in mine and b in mine:
                m.Add(mine[a] + mine[b] <= 1)
        # Weekly minutes.
        for w in range(weeks):
            terms = [(shift_by_id[sid]["stop"] - shift_by_id[sid]["start"]) * v
                     for sid, v in mine.items() if shift_by_id[sid]["day"] // 7 == w]
            if terms:
                m.Add(sum(terms) <= n["maxMinutesPerWeek"])
        # Consecutive days.
        work = []
        for d in range(num_days):
            day_vars = [v for sid, v in mine.items() if shift_by_id[sid]["day"] == d]
            wd = m.NewBoolVar(f"w_{nid}_{d}")
            if day_vars:
                for v in day_vars:
                    m.AddImplication(v, wd)
                m.Add(wd <= sum(day_vars))
            else:
                m.Add(wd == 0)
            work.append(wd)
        k = n["maxConsecutiveDays"]
        for d in range(num_days - k):
            m.Add(sum(work[d:d + k + 1]) <= k)

        if meta.get("is_agency"):
            objective.extend(AGENCY_COST * v for v in mine.values())
            continue

        # Target hours per week (soft).
        target = meta.get("target_shifts_per_week", 3)
        for w in range(weeks):
            wk = [v for sid, v in mine.items() if shift_by_id[sid]["day"] // 7 == w]
            days_in_week = min(7, num_days - 7 * w)
            tgt = round(target * days_in_week / 7)
            under = m.NewIntVar(0, tgt, f"under_{nid}_{w}")
            m.Add(under >= tgt - sum(wk))
            objective.append(UNDER_TARGET_COST * under)

        # Preferences, karma-weighted.
        wgt = pref_weight(nid)
        for sid, v in mine.items():
            if (nid, sid) in dislikes:
                objective.append(DISLIKE_COST * wgt * shift_by_id[sid]["karmaCost"] * v)
            elif (nid, sid) in likes:
                objective.append(-LIKE_REWARD * wgt * v)

        # Stable weekly pattern: y[wd] = "usually works this weekday".
        for wd in range(7):
            days = [d for d in range(wd, num_days, 7)]
            if len(days) < 2:
                continue
            y = m.NewBoolVar(f"y_{nid}_{wd}")
            for d in days:
                dev = m.NewBoolVar(f"dev_{nid}_{d}")
                m.Add(work[d] - y <= dev)
                m.Add(y - work[d] <= dev)
                objective.append(PATTERN_COST * dev)

    # Fairness: minimize the max number of expensive shifts any staff nurse gets, per unit.
    for u, lst in shifts_by_unit.items():
        exp_ids = {s["id"] for s in lst if s["karmaCost"] >= EXPENSIVE_THRESHOLD}
        staff = [n["id"] for n in nurses if n["unit"] == u and not nurse_meta.get(n["id"], {}).get("is_agency")]
        if not exp_ids or not staff:
            continue
        mx = m.NewIntVar(0, len(exp_ids), f"maxexp_{u}")
        for nid in staff:
            m.Add(sum(v for sid, v in by_nurse[nid] if sid in exp_ids) <= mx)
        objective.append(FAIRNESS_COST * mx)

    m.Minimize(sum(objective))

    if hint:
        hinted = {(a["nurse"], a["shift"]): a for a in hint}
        for key, v in x.items():
            m.AddHint(v, 1 if key in hinted else 0)
        for key, v in c.items():
            m.AddHint(v, 1 if key in hinted and hinted[key]["isCharge"] else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = 7
    status = solver.Solve(m)
    name = solver.StatusName(status)
    out: list[dict] = []
    obj = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        obj = solver.ObjectiveValue()
        for (nid, sid), v in x.items():
            if solver.Value(v):
                out.append({"nurse": nid, "shift": sid,
                            "isCharge": bool((nid, sid) in c and solver.Value(c[nid, sid]))})
    return SolveResult(status=name, assignments=out, objective=obj, seconds=time.time() - t0)
