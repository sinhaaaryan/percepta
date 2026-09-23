import Nightingale.Types
/-!
# Karma: fairness over time

"The more people dislike a shift, or the tighter its constraints … the more
'expensive' it is. A nurse picks up more positive karma for the future if they
are assigned an expensive shift that they did not prefer." — Percepta blog

Karma earned for one assignment = preference weight × shift cost, where the
weight is 0 (liked), 1 (neutral) or 2 (disliked). Karma never affects validity;
it steers the optimizer (high-karma nurses get their preferences honored first).
This file is the reference implementation the Python engine is tested against.
-/
namespace Nightingale

inductive Pref where
  | liked | neutral | disliked
deriving Repr, DecidableEq

def Pref.weight : Pref → Nat
  | .liked => 0
  | .neutral => 1
  | .disliked => 2

def karmaDelta (p : Pref) (cost : Nat) : Nat := p.weight * cost

def prefOf (i : Instance) (a : Assignment) : Pref :=
  if i.dislikes.contains ⟨a.nurse, a.shift⟩ then .disliked
  else if i.likes.contains ⟨a.nurse, a.shift⟩ then .liked
  else .neutral

def assignmentKarma (i : Instance) (a : Assignment) : Nat :=
  match i.shift? a.shift with
  | some sh => karmaDelta (prefOf i a) sh.karmaCost
  | none => 0

/-- Karma a nurse earns from a schedule. -/
def karmaEarned (i : Instance) (s : Schedule) (nid : Nat) : Nat :=
  ((s.filter (·.nurse == nid)).map (assignmentKarma i)).sum

/-! ## Properties -/

/-- Working a shift you asked for earns nothing. -/
theorem karmaDelta_liked (c : Nat) : karmaDelta .liked c = 0 := by
  simp [karmaDelta, Pref.weight]

/-- For the same shift, disliking it earns at least as much as being neutral,
which earns at least as much as liking it. -/
theorem karmaDelta_pref_mono (c : Nat) :
    karmaDelta .liked c ≤ karmaDelta .neutral c ∧ karmaDelta .neutral c ≤ karmaDelta .disliked c := by
  simp only [karmaDelta, Pref.weight]; omega

/-- More expensive shifts earn at least as much karma. -/
theorem karmaDelta_cost_mono (p : Pref) {c c' : Nat} (h : c ≤ c') :
    karmaDelta p c ≤ karmaDelta p c' :=
  Nat.mul_le_mul_left _ h

private theorem sum_append' (a b : List Nat) : (a ++ b).sum = a.sum + b.sum := by
  induction a with
  | nil => simp
  | cons x xs ih => simp [List.sum_cons, ih, Nat.add_assoc]

/-- Karma is additive across schedules, so a running ledger is well-defined. -/
theorem karmaEarned_append (i : Instance) (s₁ s₂ : Schedule) (nid : Nat) :
    karmaEarned i (s₁ ++ s₂) nid = karmaEarned i s₁ nid + karmaEarned i s₂ nid := by
  simp [karmaEarned, List.filter_append, sum_append']

/-- Taking on an extra shift never lowers your karma. -/
theorem karmaEarned_cons_mono (i : Instance) (a : Assignment) (s : Schedule) (nid : Nat) :
    karmaEarned i s nid ≤ karmaEarned i (a :: s) nid := by
  have := karmaEarned_append i [a] s nid
  simp only [List.singleton_append] at this
  omega

/-- Someone else's assignment never changes your karma. -/
theorem karmaEarned_other (i : Instance) (a : Assignment) (s : Schedule) (nid : Nat)
    (h : a.nurse ≠ nid) : karmaEarned i (a :: s) nid = karmaEarned i s nid := by
  simp [karmaEarned, List.filter_cons, h]

end Nightingale
