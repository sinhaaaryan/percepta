# Verified Nurse Scheduling Backend — Design Plan

Context: Percepta's *Building the AI-native hospital* describes **Nightingale**, an agentic
nurse scheduling system built with Summa Health. This plan designs a backend where
**any schedule, whether from a solver, an LLM agent, or a manager's edit, is published only
after a Lean 4 checker proves it satisfies the hard rules.**

Core principle: **the AI proposes, Lean checks.** The optimizer and agents aren't trusted.
Only the small, proven checker is.

---

## 1. Architecture

```
            ┌──────────────┐   requests (PTO, swaps, call-outs, census changes)
 Managers / │  FastAPI     │◄──────────────────────────────────────────────
 Nurses /   │  (Python)    │
 Agent      └──────┬───────┘
                   │
      ┌────────────┼─────────────────────────┐
      ▼            ▼                         ▼
 ┌─────────┐  ┌──────────────┐        ┌──────────────┐
 │Postgres │  │ Solver       │        │ Agent layer  │  (LLM: interprets requests,
 │(source  │  │ OR-Tools     │        │ tool-calling │   proposes diffs, explains
 │of truth)│  │ CP-SAT       │        │              │   rejections)
 └────┬────┘  └──────┬───────┘        └──────┬───────┘
      │              └──── candidate schedule ─┘
      │                         │  (canonical JSON + SHA-256)
      │                         ▼
      │              ┌──────────────────────┐
      │              │ Lean 4 checker binary │  check : Instance → Schedule → Result
      │              │ (proven sound AND     │  theorem: check = ok ↔ Valid
      │              │  complete vs. spec)   │
      │              └──────────┬───────────┘
      │                         │ ok / violations[]
      └──── verification_run ◄──┘   publish only if ok, tied to the content hash
```

Why a **verified checker** instead of generating a Lean proof for each schedule:
- Proofs are written once, for the checker (`check_sound`, `check_complete`). After that,
  running the compiled checker on a schedule gives a result that is correct by construction.
- It's fast (milliseconds for a 6-week, 200-nurse unit). Kernel-checking `decide` on each
  instance doesn't scale.
- Completeness means every rejection points to a real rule violation, so the agent
  gets a concrete list of violations to fix.
- Optional **audit mode**: for a published schedule, emit a `.lean` file with
  `example : Valid inst sched := by decide` (or `native_decide`) so a third party can
  re-verify with the kernel alone.

**Trusted computing base** (state this openly in the interview): the Lean kernel, the Lean
compiler, the JSON decoder, and the spec itself. We shrink it by (a) keeping `Spec.lean` short
and readable by a clinical ops person, (b) proving a round-trip lemma for the decoder where
practical, and (c) cross-checking with an independent Python validator in CI.

---

## 2. Domain model and rules

**Entities:** Unit (ICU, Med-Surg, ED…), Nurse (role: RN/LPN/CNA, FTE, home unit,
float-eligible units), Certification (ACLS, PALS, chemo, charge-qualified), ShiftTemplate
(Day 07–19, Night 19–07, 4-hr, 8-hr), ShiftInstance (unit + date + template → an absolute
time interval), Demand (per shift instance: required count per role/skill, derived from
census plus a ratio table), Availability (PTO, unavailability, preferences), RuleSet
(versioned parameters).

**Hard constraints (proven in Lean).** Each is a `Prop` in `Spec.lean`:

| # | Rule | Parameter example |
|---|------|------------------|
| H1 | Coverage: each shift gets ≥ the required count per role | from census/ratio table |
| H2 | Skill mix: ≥ 1 charge-qualified RN per shift; required certs present (e.g. ICU needs ACLS) | per unit |
| H3 | Qualification: a nurse works only on units they're oriented to or float-eligible for | — |
| H4 | No overlap: no nurse has two assignments whose intervals intersect | — |
| H5 | Minimum rest between consecutive shifts | ≥ 10 h |
| H6 | Max hours per rolling 7-day window / pay period | 60 h, contract-specific |
| H7 | Max consecutive shifts (nights handled separately) | ≤ 4 (≤ 3 nights) |
| H8 | Approved PTO / hard unavailability respected | — |
| H9 | Max shift length including extensions | ≤ 12 h (16 h emergency flag) |

**Soft constraints (optimized in CP-SAT, *reported* but not proven valid).** Preferences,
fair distribution of weekends/nights/holidays, overtime cost, continuity of care, and
minimal changes from the published schedule during re-optimization. Optional stretch goal:
prove a *reported metric* is computed correctly (e.g. "overtime hours = X").

Time is `Nat` minutes since the epoch of the scheduling period, so all interval math is
decidable integer arithmetic. No floats or time zones in Lean. Python normalizes DST/UTC
before export.

---

## 3. Lean 4 project (`lean/`)

```
lean/
  lakefile.lean
  NurseSched/
    Types.lean       -- Nurse, Shift, Assignment, Instance, Schedule (structures)
    Spec.lean        -- Valid : Instance → Schedule → Prop  (H1..H9, human-readable)
    Checker.lean     -- executable check returning List Violation
    Soundness.lean   -- theorem check_sound / check_complete
    Json.lean        -- FromJson instances (Lean.Json)
  Main.lean          -- CLI: reads instance+schedule JSON on stdin, prints result JSON
  Audit.lean         -- emits per-schedule `by decide` certificate file
```

Sketch:

```lean
structure Assignment where
  nurse : NurseId
  shift : ShiftId
deriving DecidableEq, Repr

def NoOverlap (I : Instance) (S : Schedule) : Prop :=
  ∀ a ∈ S.assignments, ∀ b ∈ S.assignments,
    a ≠ b → a.nurse = b.nurse → Disjoint (I.interval a.shift) (I.interval b.shift)

def MinRest (I : Instance) (S : Schedule) : Prop :=
  ∀ a ∈ S.assignments, ∀ b ∈ S.assignments,
    a.nurse = b.nurse → (I.interval a.shift).stop ≤ (I.interval b.shift).start →
    (I.interval b.shift).start - (I.interval a.shift).stop ≥ I.rules.minRestMin

def Valid (I : Instance) (S : Schedule) : Prop :=
  Coverage I S ∧ SkillMix I S ∧ Qualified I S ∧ NoOverlap I S ∧ MinRest I S ∧
  MaxHoursWindow I S ∧ MaxConsecutive I S ∧ RespectsPTO I S ∧ MaxShiftLen I S

def check (I : Instance) (S : Schedule) : List Violation := ...

theorem check_sound    : check I S = [] → Valid I S
theorem check_complete : Valid I S → check I S = []
```

Proof strategy: write each rule's checker as a `List.all`/`List.any` fold, prove a lemma
for each rule (`checkNoOverlap_iff : checkNoOverlap I S = true ↔ NoOverlap I S`), then
combine. Sort assignments per nurse by start time so the rest/consecutive checks are
linear-time, and prove the sort doesn't affect validity. Use Mathlib only if needed
(`List.Sorted`, `Finset`); plain Lean core keeps build times low.

The checker binary is built in CI and tagged with its git SHA. The SHA is stored with every
verification run, so a published schedule records which proven checker approved it.

---

## 4. Database: PostgreSQL

Postgres fits because we need transactional integrity, range types, and exclusion
constraints (a second, independent line of defense for H4), plus JSONB for rule parameters
and violation payloads.

```sql
units(id, name, min_charge_rn int, ...)
nurses(id, name, role, fte, home_unit_id, active)
certifications(id, code)                     nurse_certs(nurse_id, cert_id, expires_on)
nurse_unit_eligibility(nurse_id, unit_id, kind)   -- home | oriented | float
shift_templates(id, unit_id, name, start_local, duration_min)
shift_instances(id, unit_id, template_id, period tstzrange)
demand(shift_instance_id, role, skill, required int, source)  -- census-driven
availability(id, nurse_id, period tstzrange, kind)  -- pto_approved | unavailable | pref_off
rulesets(id, version, params jsonb, effective_from)

schedules(id, unit_id, horizon daterange, ruleset_id, parent_id,
          status  -- draft | verifying | verified | published | superseded
          content_hash bytea, created_by)             -- solver | agent | user:<id>
assignments(schedule_id, nurse_id, shift_instance_id, period tstzrange,
  EXCLUDE USING gist (schedule_id WITH =, nurse_id WITH =, period WITH &&))

verification_runs(id, schedule_id, content_hash, checker_sha, result, violations jsonb,
                  duration_ms, created_at)
change_requests(id, nurse_id, kind, payload jsonb, status, proposed_schedule_id)
audit_log(...)                                 -- append-only
```

Invariants enforced in the DB:
- A trigger blocks `status = 'published'` unless there's a `verification_runs` row with
  `result = 'ok'` whose `content_hash` matches the schedule's current hash.
- Published schedules are immutable. Changes create a child schedule (`parent_id`), which
  gets its own verification.

---

## 5. Python service (`backend/`)

Stack: **FastAPI**, **SQLAlchemy 2 + Alembic**, **Pydantic v2**, **OR-Tools CP-SAT**,
**Hypothesis** for property tests, **arq/Celery + Redis** for solve/verify jobs.

```
backend/app/
  api/          schedules.py, requests.py, nurses.py, verification.py
  domain/       models.py (Pydantic), rules.py (RuleSet params)
  export/       lean_export.py  -- DB → canonical JSON (sorted keys, int minutes) + hash
  solver/       cpsat.py        -- hard rules as constraints, soft rules as weighted objective
  verify/       lean_runner.py  -- subprocess to checker binary, timeout, parse violations
  agent/        tools.py        -- LLM tools: get_schedule, propose_swap, find_coverage,
                                   run_verify, explain_violation
  db/           models.py, migrations/
```

Key endpoints:
- `POST /schedules:generate {unit, horizon}` → solve, then verify, then return `verified` or violations
- `POST /schedules/{id}/verify` → run the Lean checker, store `verification_run`
- `POST /schedules/{id}/publish` → allowed only if verified (the DB also enforces this)
- `POST /requests` (swap, PTO, call-out) → the agent proposes a minimal diff (CP-SAT warm-started,
  minimizing changes), and Lean verifies it before the result is offered to a manager or nurse
- `GET /schedules/{id}/certificate` → checker SHA, hash, and optional audit `.lean` file

**Agent loop for a call-out** (the Nightingale-style flow):
1. A nurse calls out → the agent pins the known shifts, frees the gap, and asks CP-SAT for the
   k best minimal-change fills.
2. Each candidate goes to Lean. Rejected candidates return structured violations
   ("Nurse 42: rest 8h < 10h between 19:00 and 03:00"), which the agent can explain or
   route around.
3. The first verified candidate goes to the charge nurse for one-tap approval, then publish.

---

## 6. Testing and assurance

- **Lean:** the proofs themselves, plus `#eval` unit tests on small fixtures.
- **Differential testing:** an independent Python validator vs. the Lean checker on
  Hypothesis-generated random schedules. Any disagreement fails the build.
- **Mutation tests:** take verified schedules, apply violating edits (drop the charge nurse,
  shrink rest), and assert the checker rejects each one with the right rule tag.
- **Solver soundness monitoring:** CP-SAT output should always pass Lean. A rejection points
  to a solver modeling bug and gets alerted on.
- **CI:** `lake build` + proofs, `pytest`, and Alembic migration checks against a Postgres
  service container.

---

## 7. Milestones

1. **Week 1:** Lean `Types`/`Spec` for H1, H3, H4, H5; checker + soundness proofs; JSON CLI.
2. **Week 2:** Postgres schema + Alembic; exporter with canonical hashing; `/verify` endpoint.
3. **Week 3:** CP-SAT generator for the same rules; the generate → verify → publish pipeline.
4. **Week 4:** Remaining rules H2, H6–H9 in Lean + proofs; completeness proofs; violation messages.
5. **Week 5:** Change requests + agent tools (swap/call-out) with minimal-change re-solve.
6. **Week 6:** Audit certificates, differential/mutation testing, seed data for a demo unit.

---

## 8. Open questions for the team

- Which rules are *legal/contractual* (must be proven) vs. *policy* (can bend under a
  documented override)? Supporting "verified with override X signed by Y" needs a
  `Valid` parameterized by waived rules.
- What's the source of census/acuity for demand, and how often does it change during a shift?
- Is a verified checker enough, or do auditors want kernel-checked certificates for each
  schedule?
- Union rules that differ by nurse (seniority bidding, self-scheduling windows) need a
  per-nurse rule set in the spec.
