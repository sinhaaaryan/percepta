import Nightingale
import Nightingale.Json
import Nightingale.Karma
import Nightingale.Sound
/-!
`nightingale-check`: reads `{"instance": …, "schedule": […]}` on stdin and
prints `{"valid", "violations", "karma", "assignments"}` as JSON.
The `valid` field is `Nightingale.check`, proven equivalent to `Valid` in `Sound.lean`.
-/
open Nightingale Lean

partial def readAll (h : IO.FS.Stream) (acc : String := "") : IO String := do
  let line ← h.getLine
  if line.isEmpty then return acc else readAll h (acc ++ line)

def main : IO UInt32 := do
  let input ← readAll (← IO.getStdin)
  match Json.parse input >>= fromJson? (α := CheckRequest) with
  | .error e =>
    IO.eprintln s!"invalid input: {e}"
    return 2
  | .ok req =>
    let i := req.instance
    let s := req.schedule
    let resp : CheckResponse := {
      valid := check i s
      violations := violations i s
      karma := i.nurses.map fun n => { nurse := n.id, earned := karmaEarned i s n.id }
      assignments := s.length }
    IO.println (toJson resp).compress
    return 0
