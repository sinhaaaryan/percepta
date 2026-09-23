import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEAN_DIR = Path(os.environ.get("NIGHTINGALE_LEAN_DIR", ROOT / "lean"))
LEAN_BIN_DIR = Path(os.environ.get("LEAN_BIN_DIR", "/opt/lean/bin"))
CHECKER_BIN = Path(os.environ.get("NIGHTINGALE_CHECKER", LEAN_DIR / ".lake/build/bin/nightingale-check"))
CERT_DIR = Path(os.environ.get("NIGHTINGALE_CERT_DIR", ROOT / "var/certs"))
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://nightingale:nightingale@localhost:5432/nightingale"
)
HOSPITAL_TZ = os.environ.get("HOSPITAL_TZ", "America/New_York")
FRONTEND_DIST = ROOT / "frontend/dist"
