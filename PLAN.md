# Verified Nurse Scheduling Backend: Plan

A backend that works like Percepta's *Nightingale*. It produces monthly nurse schedules, lets managers revise them, and **refuses to publish any schedule until a Lean 4 checker has verified it against a formal specification**.

## 1. What the blog asks for

Requirements taken from *Building the AI-native hospital* (Percepta, Jun 2026):

| Blog detail | Constraint type |
|---|---|
| Every floor needs "the right mix of expertise and seniority at all times" | **Hard**: coverage and skill-mix minimums per unit and shift |
| "A unit that wanted only its own nurses serving as charge" | **Hard**: each shift has exactly one charge nurse, who is senior and belongs to the home unit |
| "A nurse who couldn't work Mondays and Wednesdays for the next three weeks" | **Hard**: availability blocks and PTO |
| "Back to back shifts" | **Hard**: minimum rest between shifts, no day shift after a night shift |
| "Multiple Fridays… for months", holidays | **Soft + fairness**: spread undesirable shifts evenly over time |
| "Wanting to work the same days each week" | **Soft**: high-dimensional preferences |
| Running **karma** balance: expensive shifts (disliked or hard to fill) earn karma when assigned against a nurse's preference | **Fairness ledger** carried across periods |
| Managers "easily view and revise entries"; the central staffing unit fills gaps day-of | Edit and swap APIs that **re-verify** after every change |

Core idea: **the optimizer is untrusted and Lean is the judge.** Proving an optimizer correct is hard. Proving a checker correct is easy. We only need a proof that the checker accepts exactly the schedules the spec calls valid (a "certifying algorithm" design).

## 2. Architecture

```
            ┌───────────── FastAPI (Python) ─────────────┐
 Manager UI │  /instances /solve /schedules /edits /publish │
   ───────► │                                              │
            │  Solver (OR-Tools CP-SAT) ── untrusted ──┐   │
            │  Karma engine (objective weights)        │   │
            │                                          ▼   │
            │  Canonical JSON exporter ──► Lean checker ◄──┼── NightingaleSpec (Lean 4 / Lake)
            │                               (compiled exe) │     • Spec.lean   : `Valid inst sched : Prop`
            │  ◄── verdict + violations + cert hash ───────┤     • Checker.lean: `check : … → Bool`
            └──────────────┬───────────────────────────────┘     • Sound.lean  : check = true ↔ Valid
                           ▼
                 PostgreSQL (source of truth, append-only versions,
                 exclusion constraints as defense-in-depth)
```

### Stack
- **Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic.**
- **OR-Tools CP-SAT** for optimization. It is standard for nurse rostering, handles hard constraints plus a weighted soft objective, and supports time limits and warm starts, which matter for re-solving after edits.
- **PostgreSQL 16.** Reasons: `tstzrange` plus `EXCLUDE USING gist` prevents a nurse from being double-booked even at the DB layer; JSONB stores the frozen instance snapshots; transactional append-only version history supports auditability, which a hospital needs. SQLite is fine for local tests but lacks exclusion constraints.
- **Lean 4 + Lake**, with no Mathlib dependency for the core (keeps builds fast). `lean4-json` / `Lean.Data.Json` handles input.
- Optional: Redis + RQ/Celery for long solves. Start with FastAPI `BackgroundTasks`.

## 3. Lean verification design

### 3.1 Data model (Lean)
Time is discretized **before** it reaches Lean. Python turns wall-clock times into minute offsets from the start of the period (`Nat`), so DST and timezones stay in Python. That conversion is part of the trusted boundary and gets its own tests.

```lean
structure Nurse where
  id : Nat; unit : Nat; skills : List Skill; seniorityYears : Nat
  chargeQualified : Bool; maxMinutesPerWeek : Nat; maxConsecutiveDays : Nat
structure Shift where
  id : Nat; unit : Nat; day : Nat; start : Nat; stop : Nat   -- minutes
  minNurses : Nat; maxNurses : Nat
  skillMin : List (Skill × Nat)       -- e.g. (ICU, 2)
  needsCharge : Bool; minChargeSeniority : Nat
structure Instance where
  nurses : List Nurse; shifts : List Shift
  unavailable : List (Nat × Nat)      -- (nurseId, shiftId) blocked (PTO, standing commitments)
  minRestMinutes : Nat                -- e.g. 600
structure Assignment where nurse : Nat; shift : Nat; isCharge : Bool
abbrev Schedule := List Assignment
```

### 3.2 Specification (`Spec.lean`): human-readable `Prop`s
Each rule is written in its most obvious form, even if inefficient. This is the file a nurse leader or reviewer reads.

```lean
def Covered (i : Instance) (s : Schedule) : Prop :=
  ∀ sh ∈ i.shifts, sh.minNurses ≤ (staffOn s sh).length ∧ (staffOn s sh).length ≤ sh.maxNurses
def SkillMix      : ∀ sh ∈ i.shifts, ∀ (k, n) ∈ sh.skillMin, n ≤ countWithSkill i s sh k
def ChargeRule    : ∀ sh, sh.needsCharge → ∃! a ∈ staffOn s sh, a.isCharge ∧
                     (nurse a).unit = sh.unit ∧ (nurse a).chargeQualified ∧ seniority ≥ min
def Available     : ∀ a ∈ s, (a.nurse, a.shift) ∉ i.unavailable
def NoOverlapRest : ∀ a b ∈ s, a ≠ b → a.nurse = b.nurse →
                     gap (shift a) (shift b) ≥ i.minRestMinutes     -- covers double-booking + back-to-back
def WeeklyHours   : ∀ n w, minutesInWeek s n w ≤ n.maxMinutesPerWeek
def MaxConsecutive: ∀ n, longestRun (daysWorked s n) ≤ n.maxConsecutiveDays
def WellFormed    : every id referenced exists, no duplicate (nurse, shift) pairs
def Valid i s := WellFormed i s ∧ Covered i s ∧ SkillMix i s ∧ ChargeRule i s ∧ Available i s ∧
                 NoOverlapRest i s ∧ WeeklyHours i s ∧ MaxConsecutive i s
```

### 3.3 Checker + soundness (`Checker.lean`, `Sound.lean`)
- `check : Instance → Schedule → Bool` is an efficient implementation: sort by (nurse, start) so rest checks are O(n log n), and use HashMaps for counts.
- The key theorems:
  ```lean
  theorem check_sound    : check i s = true → Valid i s
  theorem check_complete : Valid i s → check i s = true   -- so rejections are never spurious
  ```
- `checkExplain` returns a list of `Violation` (rule, nurse, shift, detail) for the UI. We prove that it returns `[]` iff `check = true`, so explanations can't disagree with the verdict.

### 3.4 Two verification modes
1. **Fast path (every edit):** `lake build` produces a native `nightingale-check` executable. Python pipes canonical JSON over stdin and gets `{valid, violations[]}` back in milliseconds. Trusted base: Lean compiler, JSON parser, runtime.
2. **Certificate path (on publish):** Python emits `Cert_<hash>.lean` containing the instance and schedule as literals plus
   `theorem cert : Valid inst sched := check_sound (by decide)`. Lean's kernel then checks it (use `native_decide` only if `decide` is too slow at 90 nurses × ~120 shifts; record which one was used). The `.lean` file, its SHA-256, and the Lean toolchain version are stored with the published version. Any auditor can re-run it independently of our backend.

### 3.5 Fairness / karma in Lean (stretch, but a strong interview point)
- Karma update is a pure function `karmaNext : Ledger → Instance → Schedule → Ledger`, specified in Lean and **used as the reference implementation**. Python's karma engine is differential-tested against it.
- Properties to prove:
  - **Monotonicity:** assigning a nurse a disliked shift never lowers their karma.
  - **No gain from preferred work:** a preferred shift never raises karma.
  - **Bounded drift** (optional): the ledger stays within ±B if the scheduler always picks from the lowest-karma eligible nurses.
- Hard fairness caps, such as "≤ 2 Fridays per 4 weeks" and "≤ 1 major holiday per year per nurse", can be encoded as hard rules in `Valid` when a unit opts in. Otherwise they stay soft in the objective.

We **don't** verify optimality. The claim is "every published schedule is safe and lawful", not "it is the best one". Optionally, CP-SAT's reported bound can be logged as an optimality gap for transparency.

## 4. Python backend

### 4.1 Modules
```
backend/
  app/main.py              FastAPI app
  app/api/                 instances.py, schedules.py, edits.py, nurses.py, karma.py
  app/domain/models.py     Pydantic domain types (mirror Lean structures 1:1)
  app/db/                  SQLAlchemy models, Alembic migrations
  app/solver/cpsat.py      model builder: hard constraints + weighted soft objective
  app/solver/karma.py      shift cost = dislike_rate·α + scarcity·β; karma-weighted objective
  app/verify/export.py     canonical JSON + Lean certificate emitter (deterministic ordering)
  app/verify/lean.py       subprocess runner, timeouts, result parsing
  app/services/publish.py  state machine: DRAFT → VERIFIED → PUBLISHED (→ SUPERSEDED)
lean/NightingaleSpec/      Spec.lean, Checker.lean, Sound.lean, Karma.lean, Main.lean (exe)
tests/                     pytest + Hypothesis
```

### 4.2 Solver objective (CP-SAT)
Variables: `x[n, s] ∈ {0,1}` and `c[n, s] ∈ {0,1}` (charge). Hard constraints mirror `Valid`, but the solver is untrusted, so any mismatch is caught by Lean. Minimized soft terms:
- preference violations (weighted per nurse by current **karma**: lower karma means that nurse's preferences are honored first),
- expensive-shift load imbalance (Fridays, weekends, nights, holidays) over a rolling window,
- pattern stability ("same days each week"): penalize deviation from each nurse's preferred weekday set,
- agency / overtime usage (the blog's main cost driver). Uncovered demand is modeled as explicit agency slack variables with a high cost, so the solver always returns something and the gap is visible.

### 4.3 API (main endpoints)
| Method | Path | Purpose |
|---|---|---|
| POST | `/periods` | create a scheduling period (e.g. 4–6 weeks) for units |
| PUT | `/nurses/{id}/availability`, `/preferences` | standing commitments, PTO, likes/dislikes |
| POST | `/periods/{id}/solve` | async solve → draft version + Lean verdict |
| GET | `/schedules/{version}` | schedule + violations + karma deltas |
| PATCH | `/schedules/{version}/assignments` | manager edit → new draft version, re-verified immediately |
| POST | `/schedules/{version}/swap` | day-of swap (central staffing) → verified before commit |
| POST | `/schedules/{version}/publish` | **fails 409 unless the certificate checks**; freezes the version and commits karma |
| GET | `/nurses/{id}/karma` | ledger history with reasons |

### 4.4 Database schema (PostgreSQL)
- `unit`, `nurse`, `skill`, `nurse_skill`, `shift_template`
- `period` (unit set, date range, rule parameters as JSONB)
- `shift` (period_id, unit_id, `during tstzrange`, requirements JSONB)
- `availability_block` (nurse_id, `during tstzrange`, reason, recurrence)
- `preference` (nurse_id, kind, weight, payload JSONB)
- `schedule_version` (id, period_id, parent_id, status, created_by, source=`solver|manual|swap`, `instance_snapshot` JSONB, `instance_hash`, `schedule_hash`)
- `assignment` (version_id, nurse_id, shift_id, is_charge, `during tstzrange`), with
  `EXCLUDE USING gist (version_id WITH =, nurse_id WITH =, during WITH &&)`
- `verification` (version_id, mode `fast|kernel`, verdict, violations JSONB, lean_toolchain, spec_git_sha, cert_sha256, cert blob / object-store key)
- `karma_ledger` (append-only: nurse_id, version_id, shift_id, delta, reason), where balance = SUM
- `audit_event` (who changed what, when)

Invariants enforced in code and DB: published versions are immutable; `publish` requires a `verification` row with `mode='kernel' AND verdict=true` whose hashes match the version. That is checked in the same transaction, with a DB trigger as backstop.

## 5. Testing strategy
- **Lean:** `lake build` must pass with no `sorry`; CI greps for `sorry` / `admit` / unexpected `axiom`s.
- **Differential:** Hypothesis generates random instances and schedules (valid and deliberately mutated). The Python mirror validator, Lean `check`, and CP-SAT feasibility must all agree. Every mutation class (drop the charge nurse, overlap, PTO hit, etc.) must be rejected with the right `Violation`.
- **Golden instances:** a realistic 90-nurse / 3-unit / 6-week scenario from the blog (Tupta's "90 employees") with a performance budget: solve under 60s, fast check under 100ms, kernel cert under ~30s.
- **Round-trip:** export → Lean parse → re-serialize produces byte-identical canonical JSON (guards the trusted boundary).
- API tests use testcontainers-postgres.

## 6. Milestones
1. **M0 (repo skeleton):** Lake project, FastAPI app, docker-compose (Postgres + app), CI (lake build, pytest, ruff, mypy).
2. **M1 (spec + checker):** `Spec.lean` for coverage, availability, overlap/rest, and charge rule; `check` + `check_sound`; JSON CLI.
3. **M2 (Python core):** domain models, DB schema + migrations, exporter, Lean runner, `/solve` with a CP-SAT baseline, publish gate.
4. **M3 (full rules):** skill mix, weekly hours, consecutive days, completeness proof, `checkExplain` with violations in the API.
5. **M4 (edits & day-of):** versioned edits, swaps, incremental re-verification, warm-started re-solve.
6. **M5 (fairness):** karma engine + Lean `Karma.lean` properties, fairness caps, reporting.
7. **M6 (certificates):** kernel-checked certificate on publish, stored with hashes; `verify-cert` CLI for auditors.

## 7. Trust base & risks
- **Trusted:** the Lean kernel (certificate mode), plus the Lean compiler (fast mode, or if `native_decide` is used), the JSON parser, Python's time discretization, and **the spec itself**. The spec is the real risk: a wrong rule is verified faithfully. Mitigation: keep `Spec.lean` short and readable, and review it with nurse managers the way the blog describes on-site sessions.
- **Scale:** `decide` on large literals can be slow. Fall back to `native_decide` or split the certificate per unit/week (rules are mostly local; cross-week rules are checked on overlapping windows).
- **Infeasibility:** real instances are often infeasible (short staffing). Agency-slack variables make the gap explicit instead of letting the solver fail. Lean still validates everything assigned, and the coverage rule is checked against *staff + declared agency slots*.

## 8. Questions to decide before building
1. Rule set: are rest time, max hours, and charge-seniority thresholds per-unit config or hospital-wide? (Plan: per-period JSONB parameters that flow into the Lean `Instance`.)
2. Is kernel certification on every publish acceptable latency-wise, or only nightly / on audit?
3. Should fairness caps be hard (verified) or soft (optimized)? Plan: configurable per unit.
4. Scope for the interview: I'd suggest M0–M3 plus a small M5 demo (karma monotonicity proof). That shows end-to-end "solve → Lean-verified → publish" plus the fairness research angle from the blog.
