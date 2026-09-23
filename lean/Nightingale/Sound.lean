import Nightingale.Checker
/-!
# Correctness of the checker

`check_iff : check i s = true ↔ Valid i s` — the executable checker accepts
exactly the schedules the specification calls valid (sound *and* complete).
-/
namespace Nightingale

theorem wellFormedB_iff (i : Instance) (s : Schedule) :
    wellFormedB i s = true ↔ WellFormed i s := by
  simp [wellFormedB, WellFormed, List.all_eq_true, List.any_eq_true, and_assoc]

theorem coverageB_iff (i : Instance) (s : Schedule) :
    coverageB i s = true ↔ Coverage i s := by
  simp [coverageB, Coverage, List.all_eq_true]

theorem skillMixB_iff (i : Instance) (s : Schedule) :
    skillMixB i s = true ↔ SkillMix i s := by
  simp [skillMixB, SkillMix, List.all_eq_true]

theorem chargeEligibleB_iff (i : Instance) (sh : Shift) (nid : Nat) :
    chargeEligibleB i sh nid = true ↔ ChargeEligible i sh nid := by
  unfold chargeEligibleB ChargeEligible
  cases h : i.nurse? nid <;> simp [and_assoc]

theorem chargeAssignmentOkB_iff (i : Instance) (a : Assignment) :
    chargeAssignmentOkB i a = true ↔
      ∃ sh, i.shift? a.shift = some sh ∧ sh.needsCharge = true ∧ ChargeEligible i sh a.nurse := by
  unfold chargeAssignmentOkB
  cases h : i.shift? a.shift <;> simp [chargeEligibleB_iff]

theorem chargeB_iff (i : Instance) (s : Schedule) :
    chargeB i s = true ↔ ChargeRule i s := by
  simp only [chargeB, ChargeRule, Bool.and_eq_true, List.all_eq_true, Bool.or_eq_true,
    Bool.not_eq_true', beq_iff_eq, chargeAssignmentOkB_iff]
  constructor
  · rintro ⟨h1, h2⟩
    refine ⟨fun sh hs hc => ?_, fun a ha hc => ?_⟩
    · rcases h1 sh hs with h | h
      · simp [h] at hc
      · exact h
    · rcases h2 a ha with h | h
      · simp [h] at hc
      · exact h
  · rintro ⟨h1, h2⟩
    refine ⟨fun sh hs => ?_, fun a ha => ?_⟩
    · cases hc : sh.needsCharge
      · exact Or.inl rfl
      · exact Or.inr (h1 sh hs hc)
    · cases hc : a.isCharge
      · exact Or.inl rfl
      · exact Or.inr (h2 a ha hc)

theorem availableB_iff (i : Instance) (s : Schedule) :
    availableB i s = true ↔ Available i s := by
  simp [availableB, Available, List.all_eq_true]

theorem separatedB_iff (i : Instance) (x y : Nat) :
    separatedB i x y = true ↔ Separated i x y := by
  unfold separatedB Separated
  cases hx : i.shift? x <;> cases hy : i.shift? y <;> simp

theorem restB_iff (i : Instance) (s : Schedule) :
    restB i s = true ↔ Rest i s := by
  simp only [restB, Rest, List.all_eq_true, List.mem_filter, Bool.or_eq_true, beq_iff_eq,
    separatedB_iff, and_imp]
  constructor
  · intro h a ha b hb hn hne
    rcases h a ha b hb hn.symm with h' | h'
    · exact absurd h'.symm hne
    · exact h'
  · intro h a ha b hb hn
    by_cases hs : b.shift = a.shift
    · exact Or.inl hs
    · exact Or.inr (h a ha b hb hn.symm (Ne.symm hs))

theorem weeklyHoursB_iff (i : Instance) (s : Schedule) :
    weeklyHoursB i s = true ↔ WeeklyHours i s := by
  simp [weeklyHoursB, WeeklyHours, List.all_eq_true]

theorem maxConsecutiveB_iff (i : Instance) (s : Schedule) :
    maxConsecutiveB i s = true ↔ MaxConsecutive i s := by
  simp [maxConsecutiveB, MaxConsecutive, List.all_eq_true, List.any_eq_true, Nat.lt_succ_iff]

/-- **Main theorem.** The checker returns `true` exactly on valid schedules. -/
theorem check_iff (i : Instance) (s : Schedule) : check i s = true ↔ Valid i s := by
  simp only [check, Valid, Bool.and_eq_true, wellFormedB_iff, coverageB_iff, skillMixB_iff,
    chargeB_iff, availableB_iff, restB_iff, weeklyHoursB_iff, maxConsecutiveB_iff, and_assoc]

theorem check_sound {i : Instance} {s : Schedule} (h : check i s = true) : Valid i s :=
  (check_iff i s).mp h

theorem check_complete {i : Instance} {s : Schedule} (h : Valid i s) : check i s = true :=
  (check_iff i s).mpr h

instance (i : Instance) (s : Schedule) : Decidable (Valid i s) :=
  decidable_of_iff _ (check_iff i s)

/-- The violation report is empty exactly when the checker accepts, so the UI
can never show "no problems" for an invalid schedule (or vice versa). -/
theorem violations_nil_iff (i : Instance) (s : Schedule) :
    violations i s = [] ↔ check i s = true := by
  have hr : ∀ r ok (d : Unit → List Violation), ruleReport r ok d = [] ↔ ok = true := by
    intro r ok d
    unfold ruleReport
    cases ok <;> simp
    split <;> simp
  simp only [violations, List.append_eq_nil, hr, check, Bool.and_eq_true, and_assoc]

/-! ## Sanity theorems: the spec implies the safety properties we care about -/

/-- A valid schedule never double-books a nurse into overlapping shifts. -/
theorem valid_no_overlap {i : Instance} {s : Schedule} (h : Valid i s)
    {a b : Assignment} (ha : a ∈ s) (hb : b ∈ s) (hn : a.nurse = b.nurse)
    (hne : a.shift ≠ b.shift) :
    ∃ p q, i.shift? a.shift = some p ∧ i.shift? b.shift = some q ∧
      (p.stop ≤ q.start ∨ q.stop ≤ p.start) := by
  obtain ⟨p, q, hp, hq, hsep⟩ := h.2.2.2.2.2.1 a ha b hb hn hne
  refine ⟨p, q, hp, hq, ?_⟩
  omega

/-- Every shift that needs a charge nurse gets one from its own unit. -/
theorem valid_charge_from_own_unit {i : Instance} {s : Schedule} (h : Valid i s)
    {sh : Shift} (hs : sh ∈ i.shifts) (hc : sh.needsCharge = true) :
    ∃ a ∈ s, a.shift = sh.id ∧ a.isCharge = true := by
  obtain ⟨a, hl⟩ := List.length_eq_one.mp (h.2.2.2.1.1 sh hs hc)
  have hmem : a ∈ (staffOn s sh.id).filter (·.isCharge) := by simp [hl]
  simp only [staffOn, List.mem_filter, beq_iff_eq] at hmem
  exact ⟨a, hmem.1.1, hmem.1.2, hmem.2⟩

/-- In a valid schedule every shift is staffed at or above its minimum. -/
theorem valid_min_staffed {i : Instance} {s : Schedule} (h : Valid i s)
    {sh : Shift} (hs : sh ∈ i.shifts) : sh.minNurses ≤ (staffOn s sh.id).length :=
  (h.2.1 sh hs).1

end Nightingale
