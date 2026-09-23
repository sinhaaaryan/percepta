import Nightingale.Spec
/-!
# Executable checker

One Boolean function per rule. `Sound.lean` proves `check i s = true ↔ Valid i s`.
Pairwise rules (rest) only compare a nurse's own assignments, so the check is
roughly O(|schedule| · shifts-per-nurse) rather than O(|schedule|²).
-/
namespace Nightingale

def wellFormedB (i : Instance) (s : Schedule) : Bool :=
  decide (i.nurses.map (·.id)).Nodup &&
  decide (i.shifts.map (·.id)).Nodup &&
  i.shifts.all (fun sh => decide (sh.start < sh.stop)) &&
  s.all (fun a => i.nurses.any (·.id == a.nurse) && i.shifts.any (·.id == a.shift)) &&
  decide (s.map fun a => (a.nurse, a.shift)).Nodup

def coverageB (i : Instance) (s : Schedule) : Bool :=
  i.shifts.all fun sh =>
    let c := (staffOn s sh.id).length
    decide (sh.minNurses ≤ c) && decide (c ≤ sh.maxNurses)

def skillMixB (i : Instance) (s : Schedule) : Bool :=
  i.shifts.all fun sh => sh.skillMin.all fun req =>
    decide (req.count ≤ ((staffOn s sh.id).filter (fun a => hasSkill i a.nurse req.skill)).length)

def chargeEligibleB (i : Instance) (sh : Shift) (nid : Nat) : Bool :=
  match i.nurse? nid with
  | some n => n.unit == sh.unit && n.chargeQualified && decide (sh.minChargeSeniority ≤ n.seniority)
  | none => false

def chargeAssignmentOkB (i : Instance) (a : Assignment) : Bool :=
  match i.shift? a.shift with
  | some sh => sh.needsCharge && chargeEligibleB i sh a.nurse
  | none => false

def chargeB (i : Instance) (s : Schedule) : Bool :=
  i.shifts.all (fun sh => !sh.needsCharge || ((staffOn s sh.id).filter (·.isCharge)).length == 1) &&
  s.all (fun a => !a.isCharge || chargeAssignmentOkB i a)

def availableB (i : Instance) (s : Schedule) : Bool :=
  s.all fun a => !(i.unavailable.contains ⟨a.nurse, a.shift⟩)

def separatedB (i : Instance) (x y : Nat) : Bool :=
  match i.shift? x, i.shift? y with
  | some p, some q =>
    decide (p.stop + i.minRestMinutes ≤ q.start) || decide (q.stop + i.minRestMinutes ≤ p.start)
  | _, _ => false

def restB (i : Instance) (s : Schedule) : Bool :=
  s.all fun a => (s.filter (·.nurse == a.nurse)).all fun b =>
    b.shift == a.shift || separatedB i a.shift b.shift

def weeklyHoursB (i : Instance) (s : Schedule) : Bool :=
  i.nurses.all fun n => (List.range (horizonWeeks i)).all fun w =>
    decide (minutesInWeek i s n.id w ≤ n.maxMinutesPerWeek)

def maxConsecutiveB (i : Instance) (s : Schedule) : Bool :=
  i.nurses.all fun n =>
    let ds := daysWorked i s n.id
    ds.all fun d => (List.range (n.maxConsecutiveDays + 1)).any fun j => !(ds.contains (d + j))

def check (i : Instance) (s : Schedule) : Bool :=
  wellFormedB i s && coverageB i s && skillMixB i s && chargeB i s &&
  availableB i s && restB i s && weeklyHoursB i s && maxConsecutiveB i s

/-! ## Human-readable violation report (for the UI) -/

structure Violation where
  rule : String
  nurse : Option Nat := none
  shift : Option Nat := none
  detail : String
deriving Repr, Inhabited

/-- If a rule fails, report its specific violations (or a generic one if the
detailed scan found nothing to name, so the report is never silently empty). -/
def ruleReport (rule : String) (ok : Bool) (details : Unit → List Violation) : List Violation :=
  if ok then [] else
    match details () with
    | [] => [{ rule := rule, detail := "rule failed" }]
    | v :: vs => v :: vs

def viol (rule : String) (nurse shift : Option Nat) (detail : String) : Violation :=
  { rule, nurse, shift, detail }

def coverageDetails (i : Instance) (s : Schedule) : List Violation :=
  i.shifts.filterMap fun sh =>
    let c := (staffOn s sh.id).length
    if c < sh.minNurses then
      some (viol "coverage" none sh.id s!"understaffed: {c} < min {sh.minNurses}")
    else if sh.maxNurses < c then
      some (viol "coverage" none sh.id s!"overstaffed: {c} > max {sh.maxNurses}")
    else none

def skillDetails (i : Instance) (s : Schedule) : List Violation :=
  i.shifts.flatMap fun sh => sh.skillMin.filterMap fun req =>
    let c := ((staffOn s sh.id).filter (fun a => hasSkill i a.nurse req.skill)).length
    if c < req.count then
      some (viol "skill_mix" none sh.id s!"skill {req.skill}: {c} < required {req.count}")
    else none

def chargeDetails (i : Instance) (s : Schedule) : List Violation :=
  (i.shifts.filterMap fun sh =>
    let c := ((staffOn s sh.id).filter (·.isCharge)).length
    if sh.needsCharge && c != 1 then
      some (viol "charge" none sh.id s!"needs exactly 1 charge nurse, has {c}")
    else none) ++
  (s.filterMap fun a =>
    if a.isCharge && !chargeAssignmentOkB i a then
      some (viol "charge" a.nurse a.shift "nurse not eligible to be charge on this shift")
    else none)

def availabilityDetails (i : Instance) (s : Schedule) : List Violation :=
  s.filterMap fun a =>
    if i.unavailable.contains ⟨a.nurse, a.shift⟩ then
      some (viol "availability" a.nurse a.shift "nurse is unavailable for this shift")
    else none

def restDetails (i : Instance) (s : Schedule) : List Violation :=
  s.flatMap fun a => (s.filter (·.nurse == a.nurse)).filterMap fun b =>
    if a.shift < b.shift && !separatedB i a.shift b.shift then
      some (viol "rest" a.nurse a.shift
        s!"shifts {a.shift} and {b.shift} overlap or lack {i.minRestMinutes} min rest")
    else none

def hoursDetails (i : Instance) (s : Schedule) : List Violation :=
  i.nurses.flatMap fun n => (List.range (horizonWeeks i)).filterMap fun w =>
    let m := minutesInWeek i s n.id w
    if n.maxMinutesPerWeek < m then
      some (viol "weekly_hours" n.id none s!"week {w}: {m} min > max {n.maxMinutesPerWeek}")
    else none

def consecutiveDetails (i : Instance) (s : Schedule) : List Violation :=
  i.nurses.flatMap fun n =>
    let ds := daysWorked i s n.id
    (ds.eraseDups.filter fun d =>
      !((List.range (n.maxConsecutiveDays + 1)).any fun j => !(ds.contains (d + j)))).map fun d =>
      viol "max_consecutive" n.id none
        s!"works more than {n.maxConsecutiveDays} consecutive days starting day {d}"

def violations (i : Instance) (s : Schedule) : List Violation :=
  ruleReport "well_formed" (wellFormedB i s) (fun _ => []) ++
  ruleReport "coverage" (coverageB i s) (fun _ => coverageDetails i s) ++
  ruleReport "skill_mix" (skillMixB i s) (fun _ => skillDetails i s) ++
  ruleReport "charge" (chargeB i s) (fun _ => chargeDetails i s) ++
  ruleReport "availability" (availableB i s) (fun _ => availabilityDetails i s) ++
  ruleReport "rest" (restB i s) (fun _ => restDetails i s) ++
  ruleReport "weekly_hours" (weeklyHoursB i s) (fun _ => hoursDetails i s) ++
  ruleReport "max_consecutive" (maxConsecutiveB i s) (fun _ => consecutiveDetails i s)

end Nightingale
