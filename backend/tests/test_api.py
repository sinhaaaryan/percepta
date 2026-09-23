"""End-to-end workflow through the HTTP API against PostgreSQL."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app


@pytest.fixture(scope="module")
def client(seeded_db):
    with TestClient(app) as c:
        yield c


def test_dst_night_shift_is_13_hours(client):
    p = client.get("/api/periods/1").json()
    long = [s for s in p["shifts"] if s["minutes"] != 720]
    assert long and all(s["minutes"] == 780 and s["date"] == "2026-10-31" for s in long)


def test_full_workflow(client, solved):
    vid = solved[0]
    v = client.get(f"/api/versions/{vid}").json()
    assert v["valid"] is True and v["violation_count"] == 0

    # Manager edit that breaks the charge rule -> new draft, Lean flags it.
    charge = next(a for a in v["assignments"] if a["isCharge"])
    bad = client.post(f"/api/versions/{vid}/edits", json={"ops": [
        {"op": "set_charge", "nurse": charge["nurse"], "shift": charge["shift"], "isCharge": False}]}).json()
    assert bad["valid"] is False
    assert any(x["rule"] == "charge" and x["shift"] == charge["shift"]
               for x in bad["verifications"][-1]["violations"])

    # Publishing an invalid draft is refused.
    r = client.post(f"/api/versions/{bad['id']}/publish")
    assert r.status_code == 409 and r.json()["violations"]

    # Double-booking is also blocked by the PostgreSQL exclusion constraint.
    meta = client.get("/api/periods/1").json()
    shifts = {s["id"]: s for s in meta["shifts"]}
    a = v["assignments"][0]
    # Same hours, different unit (float assignment) -> overlapping interval.
    overlapping = next(s for s in meta["shifts"] if s["id"] != a["shift"] and s["starts_at"] < shifts[a["shift"]]["ends_at"]
                       and shifts[a["shift"]]["starts_at"] < s["ends_at"])
    r = client.post(f"/api/versions/{vid}/edits", json={"ops": [
        {"op": "assign", "nurse": a["nurse"], "shift": overlapping["id"]}]})
    assert r.status_code == 409 and "double-booked" in r.json()["detail"]

    # Valid version publishes with a Lean certificate and updates karma.
    before = client.get("/api/karma").json()["balances"]
    pub = client.post(f"/api/versions/{vid}/publish")
    assert pub.status_code == 200, pub.text
    pub = pub.json()
    assert pub["status"] == "published" and pub["certified"]
    kernel = [x for x in pub["verifications"] if x["mode"] == "kernel"][-1]
    assert kernel["valid"] and kernel["cert_sha256"]
    cert = client.get(f"/api/versions/{vid}/certificate").text
    assert "theorem schedule_valid : Valid inst sched" in cert
    after = client.get("/api/karma").json()["balances"]
    earned = {row["nurse"]: row["earned"] for row in pub["karma_earned"]}
    for nid, e in earned.items():
        assert after.get(str(nid), 0) - before.get(str(nid), 0) == e

    # Published versions are immutable at the DB level.
    with pytest.raises(Exception, match="immutable"):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM assignment WHERE version_id = :v"), {"v": vid})


def test_db_refuses_publish_without_certificate(client, solved):
    r = client.post("/api/periods/1/solve", json={"time_limit": 5}).json()
    with pytest.raises(Exception, match="no valid Lean certificate"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE schedule_version SET status='published' WHERE id=:v"), {"v": r["id"]})


def test_availability_change_is_respected(client, solved):
    v = client.get(f"/api/versions/{solved[0]}").json()
    a = next(x for x in v["assignments"] if not x["isCharge"])
    shift = next(s for s in client.get("/api/periods/1").json()["shifts"] if s["id"] == a["shift"])
    r = client.post(f"/api/nurses/{a['nurse']}/availability",
                    json={"start_date": shift["date"], "end_date": shift["date"], "reason": "sick"})
    assert r.status_code == 200
    # Re-verifying the old assignment set against fresh data flags it...
    stale = client.post(f"/api/versions/{v['id']}/edits", json={"ops": []}).json()
    assert any(x["rule"] == "availability" for x in stale["verifications"][-1]["violations"])
    # ...and a re-solve routes around it.
    fresh = client.post("/api/periods/1/solve", json={"time_limit": 10}).json()
    assert fresh["valid"]
    assert not any(x["nurse"] == a["nurse"] and x["shift"] == a["shift"] for x in fresh["assignments"])
