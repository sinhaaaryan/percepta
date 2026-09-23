# Nightingale, verified with Lean 4

A nurse scheduling backend modeled on Percepta's *Nightingale* from the public blog post ([Building the AI-native hospital](https://www.percepta.ai/blog/building-the-ai-native-hospital)). **No schedule can be published until Lean has proven it valid.**

- **Optimizer (untrusted):** OR-Tools CP-SAT balances coverage, skill mix, seniority, preferences, a karma-based fairness score, stable weekly patterns, and agency cost.
- **Judge (Lean 4):** `Spec.lean` states the hard rules. A checker is **proven** to accept exactly the schedules the spec allows (`check_iff`). Every draft is checked, and publishing requires a Lean certificate stored with the schedule.
- **PostgreSQL** keeps versioned history and adds database-level safeguards:
  - an exclusion constraint stops a nurse being double-booked;
  - published versions can't be changed;
  - a trigger refuses `status = 'published'` unless a valid certificate exists for that exact instance and schedule hash.
- **React UI:** a manager grid for generating, editing, seeing violations, and publishing.

See [PLAN.md](PLAN.md) for the original design.

## How it fits together

```
React grid ──► FastAPI ──► CP-SAT solver (untrusted)
                  │
                  ├──► nightingale-check (compiled Lean; check_iff proven)   ← every draft / edit
                  ├──► Lean certificate: theorem schedule_valid : Valid inst sched  ← on publish
                  └──► PostgreSQL (versions, verifications, karma ledger, guards)
```

| Lean file | Contents |
|---|---|
| `Types.lean` | Nurses, shifts, instance, assignments. Time is plain minutes; Python handles time zones and DST first. |
| `Spec.lean` | `Valid` combines 8 rules: well-formed, coverage, skill mix, charge (own unit, qualified, senior enough, exactly one), availability, rest (includes no double-booking), weekly hours, max consecutive days. |
| `Checker.lean` | The executable `check` plus a readable violation report. |
| `Sound.lean` | `check_iff : check i s = true ↔ Valid i s`, `violations_nil_iff` (report is empty ⇔ valid), and safety corollaries (`valid_no_overlap`, `valid_charge_from_own_unit`, `valid_min_staffed`). |
| `Karma.lean` | The blog's karma: disliked ×2, neutral ×1, liked ×0, times the shift's cost. Proven properties: liked shifts earn 0, preference/cost monotonicity, additivity (a running ledger is well-defined), taking a shift never lowers your karma, and other people's shifts don't change yours. |

All proofs build with no `sorry`. They use only the standard axioms (`propext`, `Quot.sound`); a test checks this.

## Measured results (this repo, demo data)

Demo data: 90 staff nurses plus 12 agency nurses, 3 units, 28 days (Oct 19 – Nov 15, 2026, which crosses the DST change), 168 shifts, availability blocks including the blog's "can't work Mon/Wed for 3 weeks".

| Step | Result |
|---|---|
| CP-SAT solve | **OPTIMAL in ~7 s**, 962 assignments, 0 agency shifts |
| Lean fast check (compiled checker) | **valid, ~160–220 ms** |
| Lean certificate on publish | **~17 s**; axioms `propext, Lean.ofReduceBool, Quot.sound` |
| `pytest` (9 tests) | **all pass (~45 s)** |
| Headless-browser run of the UI | generate → break charge rule → Lean flags it, Publish disabled → fix → publish → view certificate: **works, no page errors** |

The tests cover:
- **Differential testing:** 300 random instances (Hypothesis), where the Lean checker and an independent Python mirror must agree rule-for-rule, plus the karma output.
- **Mutations:** every rule class (coverage, charge ×2, duplicate, unknown nurse, availability, rest, hours, consecutive days, skill mix) must be rejected with the right rule.
- **Proof audit:** no `sorry`, only standard axioms.
- **DST:** the Oct 31 night shift is 13 hours.
- **API workflow:**
  - an invalid draft can't be published (409);
  - double-booking hits the Postgres exclusion constraint (409);
  - publishing writes a certificate and karma ledger entries that match Lean's numbers;
  - published rows can't be changed;
  - the DB refuses to mark a version published without a certificate;
  - after new unavailability, the old schedule is flagged and a re-solve avoids it.

### Trust base, stated honestly
- **Fast check:** trusts the Lean compiler, the JSON parser, and Python's conversion of dates and times into minutes.
- **Certificates:** use `native_decide`, which runs the proven checker as compiled code. The trusted piece is therefore the Lean compiler (`Lean.ofReduceBool`), and that axiom is recorded with each certificate.
- **Pure-kernel proofs:** a certificate checked by the kernel alone (`decide +kernel`) works on small instances, but on the full 90-nurse month it **ran out of memory at ~14 GB**. Getting pure-kernel checking at this scale would need sharding (per unit or week) plus a composition theorem. That's future work.
- **The spec is the real risk.** A wrong rule gets verified faithfully. That's why `Spec.lean` is short and readable, and the UI links to it.
- **Not verified:** optimality. The claim is that every published schedule obeys the rules, not that it's the best one.

## Run it

Prereqs: Lean 4.15.0, Python 3.11+, Node 18+, PostgreSQL 14+ (with `btree_gist`, which ships in contrib).

```bash
# Lean: elan works too; this is the manual install
curl -L -o lean.tar.zst https://github.com/leanprover/lean4/releases/download/v4.15.0/lean-4.15.0-linux.tar.zst
# extract to /opt/lean (or set LEAN_BIN_DIR=/path/to/lean/bin)

# Postgres: `docker compose up -d db`, or a local server with:
#   user/password nightingale, databases nightingale and nightingale_test
export DATABASE_URL=postgresql+psycopg://nightingale:nightingale@localhost:5432/nightingale

make setup    # lake build (proofs + checker), python venv, npm build
make seed     # demo data
make run      # http://localhost:8000  (API + UI)
make test
```

UI development: `make run` in one terminal and `make dev-ui` in another (Vite on :5173 proxies `/api`).

### API

| Method | Path | |
|---|---|---|
| GET | `/api/meta` | units, skills, nurses (with karma), periods |
| POST | `/api/periods` | create a period; shifts are generated from unit templates |
| GET | `/api/periods/{id}` | shifts + versions |
| POST | `/api/periods/{id}/solve` | CP-SAT → new draft, Lean-checked |
| GET | `/api/versions/{id}` | assignments, violations, karma, audit hashes |
| POST | `/api/versions/{id}/edits` | `{ops: [{op: assign\|unassign\|set_charge, nurse, shift, isCharge}]}` → new draft, re-checked |
| POST | `/api/versions/{id}/verify?mode=fast\|kernel` | re-run verification |
| POST | `/api/versions/{id}/publish` | 409 unless valid **and** the Lean certificate checks; commits karma |
| GET | `/api/versions/{id}/certificate` | the `.lean` certificate (re-check with `cd lean && lake env lean <file>`) |
| GET | `/api/karma` | balances + ledger |
| POST/GET | `/api/nurses/{id}/availability` | PTO / standing commitments |
| PUT | `/api/nurses/{id}/preferences` | liked/disliked weekdays, preferred day/night |
| GET | `/api/spec` | the Lean spec text |

## Layout

```
lean/       Lake project: Nightingale/{Types,Spec,Checker,Sound,Karma,Json}.lean, Main.lean (CLI)
backend/    FastAPI app (app/), tests/
  app/instance.py   DB → Lean instance (time zones/DST → minutes); karma cost per shift
  app/solver.py     CP-SAT model
  app/lean.py       checker bridge + certificate generation
  app/services.py   solve / edit / verify / publish workflow
  app/reference.py  Python mirror of the spec (for differential tests only)
  app/seed.py       demo data
frontend/   React (Vite) manager UI
```
