"""Differential tests: the Lean checker (proven equivalent to the spec) and the
Python mirror must agree, and every class of mutation must be caught."""
import copy
import subprocess

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import config, lean, reference


def lean_rules(instance, schedule):
    r = lean.fast_check(instance, schedule)
    rules = {v["rule"] for v in r.violations}
    assert r.valid == (not rules)
    return r.valid, rules


def test_solver_output_is_valid(solved):
    _, inst, sched = solved
    valid, rules = lean_rules(inst, sched)
    assert valid, rules
    assert reference.violated_rules(inst, sched) == set()


def test_karma_matches_lean_reference(solved):
    _, inst, sched = solved
    assert lean.fast_check(inst, sched).karma == reference.karma_earned(inst, sched)


def _mutations(inst, sched):
    shifts = {s["id"]: s for s in inst["shifts"]}
    nurses = {n["id"]: n for n in inst["nurses"]}
    by_shift = {}
    for a in sched:
        by_shift.setdefault(a["shift"], []).append(a)

    def drop(pred):
        out = copy.deepcopy(sched)
        for i, a in enumerate(out):
            if pred(a):
                del out[i]
                return out
        raise AssertionError("no candidate")

    # coverage: empty an entire shift
    sid = inst["shifts"][0]["id"]
    yield "coverage", [a for a in sched if a["shift"] != sid]
    # charge: remove a charge flag
    out = copy.deepcopy(sched)
    next(a for a in out if a["isCharge"])["isCharge"] = False
    yield "charge", out
    # charge: ineligible nurse (not charge qualified) marked charge
    out = copy.deepcopy(sched)
    a = next(a for a in out if not a["isCharge"] and not nurses[a["nurse"]]["chargeQualified"])
    a["isCharge"] = True
    yield "charge", out
    # well_formed: duplicate assignment
    yield "well_formed", sched + [dict(sched[0])]
    # well_formed: unknown nurse
    yield "well_formed", sched + [{"nurse": 999999, "shift": sched[0]["shift"], "isCharge": False}]
    # availability: assign someone to a blocked shift
    p = inst["unavailable"][0]
    yield "availability", sched + [{"nurse": p["nurse"], "shift": p["shift"], "isCharge": False}]
    # rest: give a day-shift nurse the same day's night shift (0 min rest)
    for a in sched:
        s = shifts[a["shift"]]
        night = next((t for t in inst["shifts"] if t["unit"] == s["unit"] and t["start"] == s["stop"]), None)
        if night and not any(b["nurse"] == a["nurse"] and b["shift"] == night["id"] for b in sched):
            yield "rest", sched + [{"nurse": a["nurse"], "shift": night["id"], "isCharge": False}]
            break
    # weekly_hours: shrink a working nurse's cap
    inst2 = copy.deepcopy(inst)
    worker = sched[0]["nurse"]
    next(n for n in inst2["nurses"] if n["id"] == worker)["maxMinutesPerWeek"] = 60
    yield "weekly_hours", (inst2, sched)
    # max_consecutive: set a working nurse's limit to 0
    inst3 = copy.deepcopy(inst)
    next(n for n in inst3["nurses"] if n["id"] == worker)["maxConsecutiveDays"] = 0
    yield "max_consecutive", (inst3, sched)
    # skill_mix: demand more of a skill than anyone has
    inst4 = copy.deepcopy(inst)
    inst4["shifts"][0]["skillMin"] = [{"skill": 99, "count": 1}]
    yield "skill_mix", (inst4, sched)


def test_every_mutation_is_caught(solved):
    _, inst, sched = solved
    seen = set()
    for rule, m in _mutations(inst, sched):
        i2, s2 = m if isinstance(m, tuple) else (inst, m)
        valid, rules = lean_rules(i2, s2)
        assert not valid and rule in rules, (rule, rules)
        assert reference.violated_rules(i2, s2) == rules, rule
        seen.add(rule)
    assert seen == {"coverage", "charge", "well_formed", "availability", "rest", "weekly_hours",
                    "max_consecutive", "skill_mix"}


# --- random small instances -------------------------------------------------

@st.composite
def small_problem(draw):
    n_nurses = draw(st.integers(1, 5))
    n_shifts = draw(st.integers(1, 6))
    nurses = [{
        "id": i + 1, "unit": draw(st.integers(1, 2)), "skills": sorted(draw(st.sets(st.integers(1, 3), max_size=3))),
        "seniority": draw(st.integers(0, 6)), "chargeQualified": draw(st.booleans()),
        "maxMinutesPerWeek": draw(st.sampled_from([600, 1440, 2400])),
        "maxConsecutiveDays": draw(st.integers(0, 3)),
    } for i in range(n_nurses)]
    shifts = []
    for j in range(n_shifts):
        day = draw(st.integers(0, 9))
        start = day * 1440 + draw(st.sampled_from([420, 1140]))
        shifts.append({
            "id": 100 + j, "unit": draw(st.integers(1, 2)), "day": day, "start": start,
            "stop": start + draw(st.sampled_from([480, 720])),
            "minNurses": draw(st.integers(0, 2)), "maxNurses": draw(st.integers(1, 3)),
            "skillMin": [{"skill": draw(st.integers(1, 3)), "count": draw(st.integers(0, 1))}
                         for _ in range(draw(st.integers(0, 2)))],
            "needsCharge": draw(st.booleans()), "minChargeSeniority": draw(st.integers(0, 4)),
            "karmaCost": draw(st.integers(0, 6)),
        })
    pairs = st.fixed_dictionaries({"nurse": st.integers(1, n_nurses), "shift": st.integers(100, 99 + n_shifts)})
    inst = {"nurses": nurses, "shifts": shifts, "minRestMinutes": draw(st.sampled_from([0, 600])),
            "unavailable": draw(st.lists(pairs, max_size=3)), "likes": draw(st.lists(pairs, max_size=3)),
            "dislikes": draw(st.lists(pairs, max_size=3))}
    sched = draw(st.lists(st.fixed_dictionaries({
        "nurse": st.integers(1, n_nurses + 1),  # sometimes an unknown nurse
        "shift": st.integers(100, 99 + n_shifts), "isCharge": st.booleans()}), max_size=8))
    return inst, sched


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(small_problem())
def test_random_instances_lean_agrees_with_mirror(problem):
    inst, sched = problem
    valid, rules = lean_rules(inst, sched)
    assert reference.violated_rules(inst, sched) == rules
    if all(a["nurse"] <= len(inst["nurses"]) for a in sched):
        assert lean.fast_check(inst, sched).karma == reference.karma_earned(inst, sched)


def test_lean_proofs_have_no_sorry_and_standard_axioms(tmp_path):
    src = "\n".join(p.read_text() for p in (config.LEAN_DIR / "Nightingale").glob("*.lean"))
    assert "sorry" not in src and "admit" not in src
    f = tmp_path / "Audit.lean"
    f.write_text("import Nightingale\n#print axioms Nightingale.check_iff\n"
                 "#print axioms Nightingale.violations_nil_iff\n"
                 "#print axioms Nightingale.karmaEarned_append\n")
    out = subprocess.run(["lake", "env", "lean", str(f)], cwd=config.LEAN_DIR, capture_output=True,
                         text=True, env=lean._env(), timeout=300).stdout
    allowed = {"propext", "Quot.sound", "Classical.choice"}
    for line in out.splitlines():
        if "depends on axioms" in line:
            axioms = {a.strip() for a in line.split("[")[1].rstrip("]").split(",")}
            assert axioms <= allowed, line
        else:
            assert "does not depend on any axioms" in line or not line.strip(), line
