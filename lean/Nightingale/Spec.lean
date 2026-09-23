import Nightingale.Types
/-!
# Specification: what a *valid* schedule is

This file is the contract. Every rule is stated in its most direct form
(quantifiers over lists), not for efficiency but for readability — it is
the file nurse leaders and reviewers should read. `Checker.lean` gives an
executable decision procedure and `Sound.lean` proves it agrees with `Valid`.
-/
namespace Nightingale

/-- Assignments on a given shift. -/
def staffOn (s : Schedule) (sid : Nat) : List Assignment := s.filter (·.shift == sid)

/-- Does nurse `nid` hold skill `k`? -/
def hasSkill (i : Instance) (nid k : Nat) : Bool :=
  match i.nurse? nid with
  | some n => n.skills.contains k
  | none => false

/-- R0. Well-formedness: unique ids, real shifts, every assignment refers to a
known nurse and shift, and nobody is assigned twice to the same shift. -/
def WellFormed (i : Instance) (s : Schedule) : Prop :=
  (i.nurses.map (·.id)).Nodup ∧
  (i.shifts.map (·.id)).Nodup ∧
  (∀ sh ∈ i.shifts, sh.start < sh.stop) ∧
  (∀ a ∈ s, (∃ n ∈ i.nurses, n.id = a.nurse) ∧ (∃ sh ∈ i.shifts, sh.id = a.shift)) ∧
  (s.map fun a => (a.nurse, a.shift)).Nodup

/-- R1. Coverage: every shift is staffed between its minimum and maximum. -/
def Coverage (i : Instance) (s : Schedule) : Prop :=
  ∀ sh ∈ i.shifts,
    sh.minNurses ≤ (staffOn s sh.id).length ∧ (staffOn s sh.id).length ≤ sh.maxNurses

/-- R2. Skill mix: every shift has at least the required number of nurses with each skill. -/
def SkillMix (i : Instance) (s : Schedule) : Prop :=
  ∀ sh ∈ i.shifts, ∀ req ∈ sh.skillMin,
    req.count ≤ ((staffOn s sh.id).filter (fun a => hasSkill i a.nurse req.skill)).length

/-- A nurse may be charge on a shift only if they belong to that shift's unit
("a unit that wanted only its own nurses serving as charge"), are charge-qualified,
and are senior enough. -/
def ChargeEligible (i : Instance) (sh : Shift) (nid : Nat) : Prop :=
  ∃ n, i.nurse? nid = some n ∧ n.unit = sh.unit ∧ n.chargeQualified = true ∧
    sh.minChargeSeniority ≤ n.seniority

/-- R3. Charge: shifts that need a charge nurse have exactly one; every charge
assignment is on a shift needing one and is held by an eligible nurse. -/
def ChargeRule (i : Instance) (s : Schedule) : Prop :=
  (∀ sh ∈ i.shifts, sh.needsCharge = true →
      ((staffOn s sh.id).filter (·.isCharge)).length = 1) ∧
  (∀ a ∈ s, a.isCharge = true →
      ∃ sh, i.shift? a.shift = some sh ∧ sh.needsCharge = true ∧ ChargeEligible i sh a.nurse)

/-- R4. Availability: nobody works a shift they are blocked from (PTO, standing commitments). -/
def Available (i : Instance) (s : Schedule) : Prop :=
  ∀ a ∈ s, (⟨a.nurse, a.shift⟩ : Pair) ∉ i.unavailable

/-- Two shifts are separated by at least the minimum rest, in either order. -/
def Separated (i : Instance) (x y : Nat) : Prop :=
  ∃ p q, i.shift? x = some p ∧ i.shift? y = some q ∧
    (p.stop + i.minRestMinutes ≤ q.start ∨ q.stop + i.minRestMinutes ≤ p.start)

/-- R5. Rest: any two different shifts worked by the same nurse are separated by
the minimum rest. This rules out double-booking and back-to-back shifts. -/
def Rest (i : Instance) (s : Schedule) : Prop :=
  ∀ a ∈ s, ∀ b ∈ s, a.nurse = b.nurse → a.shift ≠ b.shift → Separated i a.shift b.shift

/-- Minutes a nurse works in week `w`. -/
def minutesInWeek (i : Instance) (s : Schedule) (nid w : Nat) : Nat :=
  ((s.filter (·.nurse == nid)).map fun a =>
      match i.shift? a.shift with
      | some sh => if sh.week == w then sh.minutes else 0
      | none => 0).sum

/-- Number of weeks the period spans. -/
def horizonWeeks (i : Instance) : Nat := (i.shifts.map (·.week)).foldl max 0 + 1

/-- R6. Weekly hours cap per nurse. -/
def WeeklyHours (i : Instance) (s : Schedule) : Prop :=
  ∀ n ∈ i.nurses, ∀ w, w < horizonWeeks i → minutesInWeek i s n.id w ≤ n.maxMinutesPerWeek

/-- Days (by shift start day) a nurse works. -/
def daysWorked (i : Instance) (s : Schedule) (nid : Nat) : List Nat :=
  (s.filter (·.nurse == nid)).filterMap fun a => (i.shift? a.shift).map (·.day)

/-- R7. Max consecutive days: starting from any worked day, a day off occurs
within the nurse's limit — so no run of worked days is longer than the limit. -/
def MaxConsecutive (i : Instance) (s : Schedule) : Prop :=
  ∀ n ∈ i.nurses, ∀ d ∈ daysWorked i s n.id,
    ∃ j, j ≤ n.maxConsecutiveDays ∧ d + j ∉ daysWorked i s n.id

/-- A schedule is valid iff it satisfies every hard rule. -/
def Valid (i : Instance) (s : Schedule) : Prop :=
  WellFormed i s ∧ Coverage i s ∧ SkillMix i s ∧ ChargeRule i s ∧
  Available i s ∧ Rest i s ∧ WeeklyHours i s ∧ MaxConsecutive i s

end Nightingale
