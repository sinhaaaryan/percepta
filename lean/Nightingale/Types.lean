/-!
# Core data model

All times are minutes since the start of the scheduling period (`Nat`).
Wall-clock / timezone / DST handling happens in Python before data reaches
Lean; that conversion is part of the trusted boundary.
-/
namespace Nightingale

structure SkillReq where
  skill : Nat
  count : Nat
deriving Repr, DecidableEq, Inhabited

structure Nurse where
  id : Nat
  unit : Nat
  skills : List Nat
  seniority : Nat
  chargeQualified : Bool
  maxMinutesPerWeek : Nat
  maxConsecutiveDays : Nat
deriving Repr, DecidableEq, Inhabited

structure Shift where
  id : Nat
  unit : Nat
  /-- Day index within the period (day 0 = first day). -/
  day : Nat
  start : Nat
  stop : Nat
  minNurses : Nat
  maxNurses : Nat
  skillMin : List SkillReq
  needsCharge : Bool
  minChargeSeniority : Nat
  /-- How "expensive" the shift is (undesirability + scarcity); used only for karma. -/
  karmaCost : Nat
deriving Repr, DecidableEq, Inhabited

/-- A (nurse, shift) pair, used for availability blocks and preferences. -/
structure Pair where
  nurse : Nat
  shift : Nat
deriving Repr, DecidableEq, Inhabited

structure Instance where
  nurses : List Nurse
  shifts : List Shift
  /-- Hard blocks: PTO, standing commitments ("can't work Mon/Wed for 3 weeks"). -/
  unavailable : List Pair
  minRestMinutes : Nat
  /-- Soft preferences (only used by karma, never by validity). -/
  likes : List Pair
  dislikes : List Pair
deriving Repr, Inhabited

structure Assignment where
  nurse : Nat
  shift : Nat
  isCharge : Bool
deriving Repr, DecidableEq, Inhabited

abbrev Schedule := List Assignment

namespace Instance
def nurse? (i : Instance) (id : Nat) : Option Nurse := i.nurses.find? (·.id == id)
def shift? (i : Instance) (id : Nat) : Option Shift := i.shifts.find? (·.id == id)
end Instance

def Shift.minutes (sh : Shift) : Nat := sh.stop - sh.start
def Shift.week (sh : Shift) : Nat := sh.day / 7

end Nightingale
