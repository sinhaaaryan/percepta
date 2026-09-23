import Lean.Data.Json
import Nightingale.Checker
/-! JSON (de)serialization for the CLI. Field names match the Python exporter. -/
namespace Nightingale
open Lean

deriving instance FromJson, ToJson for SkillReq
deriving instance FromJson, ToJson for Nurse
deriving instance FromJson, ToJson for Shift
deriving instance FromJson, ToJson for Pair
deriving instance FromJson, ToJson for Instance
deriving instance FromJson, ToJson for Assignment
deriving instance ToJson for Violation

structure CheckRequest where
  «instance» : Instance
  schedule : Schedule
deriving FromJson

structure KarmaRow where
  nurse : Nat
  earned : Nat
deriving ToJson

structure CheckResponse where
  valid : Bool
  violations : List Violation
  karma : List KarmaRow
  assignments : Nat
deriving ToJson

end Nightingale
