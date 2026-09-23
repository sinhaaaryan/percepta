"""Bridge to the Lean checker.

* fast_check     — runs the compiled `nightingale-check` binary (proven-correct checker).
* kernel_certify — writes a standalone Lean file stating `Valid inst sched` for the
                   concrete data and has Lean check the proof. The file is kept as
                   an auditable certificate.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .instance import canonical_json

SPEC_FILES = ["Nightingale/Types.lean", "Nightingale/Spec.lean", "Nightingale/Checker.lean",
              "Nightingale/Sound.lean", "Nightingale/Karma.lean"]


class LeanError(RuntimeError):
    pass


@dataclass
class CheckResult:
    valid: bool
    violations: list[dict]
    karma: dict[int, int]
    duration_ms: int
    mode: str
    cert_path: str | None = None
    cert_sha256: str | None = None
    axioms: list[str] | None = None
    extra: dict = field(default_factory=dict)


def _env() -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{config.LEAN_BIN_DIR}:{env.get('PATH', '')}"
    return env


@functools.cache
def lean_version() -> str:
    out = subprocess.run(["lean", "--version"], capture_output=True, text=True, env=_env())
    return out.stdout.strip()


@functools.cache
def spec_hash() -> str:
    h = hashlib.sha256()
    for f in SPEC_FILES:
        h.update(f.encode())
        h.update((config.LEAN_DIR / f).read_bytes())
    return h.hexdigest()


def fast_check(instance: dict, schedule: list[dict], timeout: float = 60) -> CheckResult:
    if not config.CHECKER_BIN.exists():
        raise LeanError(f"checker binary not found at {config.CHECKER_BIN}; run `lake build` in lean/")
    payload = canonical_json({"instance": instance, "schedule": schedule})
    t0 = time.time()
    proc = subprocess.run([str(config.CHECKER_BIN)], input=payload, capture_output=True,
                          text=True, timeout=timeout)
    ms = int((time.time() - t0) * 1000)
    if proc.returncode != 0:
        raise LeanError(f"checker failed ({proc.returncode}): {proc.stderr.strip()}")
    out = json.loads(proc.stdout)
    return CheckResult(
        valid=out["valid"],
        violations=[{k: v.get(k) for k in ("rule", "nurse", "shift", "detail")} for v in out["violations"]],
        karma={row["nurse"]: row["earned"] for row in out["karma"]},
        duration_ms=ms, mode="fast",
    )


# ---------------------------------------------------------------------------
# Kernel-checked certificates
# ---------------------------------------------------------------------------

def _b(v: bool) -> str:
    return "true" if v else "false"


def _nats(xs) -> str:
    return "[" + ", ".join(str(int(x)) for x in xs) + "]"


def _reqs(reqs) -> str:
    return "[" + ", ".join("⟨%d, %d⟩" % (r["skill"], r["count"]) for r in reqs) + "]"


def to_lean_literal(instance: dict, schedule: list[dict]) -> str:
    nurses = ",\n    ".join(
        f"⟨{n['id']}, {n['unit']}, {_nats(n['skills'])}, {n['seniority']}, {_b(n['chargeQualified'])}, "
        f"{n['maxMinutesPerWeek']}, {n['maxConsecutiveDays']}⟩" for n in instance["nurses"])
    shifts = ",\n    ".join(
        f"⟨{s['id']}, {s['unit']}, {s['day']}, {s['start']}, {s['stop']}, {s['minNurses']}, "
        f"{s['maxNurses']}, {_reqs(s['skillMin'])}, "
        f"{_b(s['needsCharge'])}, {s['minChargeSeniority']}, {s['karmaCost']}⟩" for s in instance["shifts"])

    def pairs(ps):
        return "[" + ", ".join(f"⟨{p['nurse']}, {p['shift']}⟩" for p in ps) + "]"

    sched = ",\n    ".join(f"⟨{a['nurse']}, {a['shift']}, {_b(a['isCharge'])}⟩" for a in schedule)
    return (
        "def inst : Instance where\n"
        f"  nurses := [\n    {nurses}]\n"
        f"  shifts := [\n    {shifts}]\n"
        f"  unavailable := {pairs(instance['unavailable'])}\n"
        f"  minRestMinutes := {instance['minRestMinutes']}\n"
        f"  likes := {pairs(instance['likes'])}\n"
        f"  dislikes := {pairs(instance['dislikes'])}\n\n"
        f"def sched : Schedule := [\n    {sched}]\n"
    )


def certificate_source(instance: dict, schedule: list[dict], name: str, instance_hash: str,
                       schedule_hash: str) -> str:
    return (
        f"/-\n  Nightingale schedule certificate: {name}\n"
        f"  instance sha256: {instance_hash}\n  schedule sha256: {schedule_hash}\n"
        f"  spec sha256:     {spec_hash()}\n"
        "  Re-check with:  cd lean && lake env lean <this file>\n-/\n"
        "import Nightingale\nopen Nightingale\n\nset_option maxRecDepth 100000\n\n"
        + to_lean_literal(instance, schedule)
        + "\n/-- The published schedule satisfies every hard rule in `Spec.lean`. -/\n"
        "theorem schedule_valid : Valid inst sched :=\n"
        "  check_sound (by native_decide)\n\n"
        "#print axioms schedule_valid\n"
    )


def kernel_certify(instance: dict, schedule: list[dict], name: str, instance_hash: str,
                   schedule_hash: str, timeout: float = 600) -> CheckResult:
    """Produce and check a Lean proof of `Valid inst sched`.

    `native_decide` evaluates the (proven-correct) checker with compiled code;
    the resulting proof depends on the `Lean.ofReduceBool` axiom, which we
    record so auditors can see exactly what is trusted.
    """
    config.CERT_DIR.mkdir(parents=True, exist_ok=True)
    src = certificate_source(instance, schedule, name, instance_hash, schedule_hash)
    digest = hashlib.sha256(src.encode()).hexdigest()
    path = Path(config.CERT_DIR) / f"{name}.lean"
    path.write_text(src)
    t0 = time.time()
    proc = subprocess.run(["lake", "env", "lean", str(path.resolve())], cwd=config.LEAN_DIR,
                          capture_output=True, text=True, timeout=timeout, env=_env())
    ms = int((time.time() - t0) * 1000)
    out = proc.stdout + proc.stderr
    ok = proc.returncode == 0 and "error" not in out
    axioms: list[str] = []
    m = re.search(r"depends on axioms: \[(.*?)\]", out, re.S)
    if m:
        axioms = [a.strip() for a in m.group(1).split(",") if a.strip()]
    return CheckResult(valid=ok, violations=[] if ok else [{"rule": "certificate", "nurse": None,
                       "shift": None, "detail": out.strip()[:2000]}],
                       karma={}, duration_ms=ms, mode="kernel", cert_path=str(path),
                       cert_sha256=digest, axioms=axioms, extra={"output": out})
