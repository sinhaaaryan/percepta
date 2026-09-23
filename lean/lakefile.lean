import Lake
open Lake DSL

package nightingale where
  leanOptions := #[⟨`autoImplicit, false⟩]

@[default_target]
lean_lib Nightingale

@[default_target]
lean_exe «nightingale-check» where
  root := `Main
  supportInterpreter := true
