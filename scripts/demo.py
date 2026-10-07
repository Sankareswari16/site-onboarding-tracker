"""
End-to-end walkthrough against an in-memory-style temp database, printing what
each step does. Run: python scripts/demo.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PYTHONWARNINGS", "ignore")

import logging  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

logging.getLogger("api").disabled = True
logging.getLogger("service").disabled = True
logging.getLogger("outbox").disabled = True

path = os.path.join(tempfile.mkdtemp(), "demo.db")
c = TestClient(create_app(db_path=path))
H = lambda k: {"X-API-Key": k}  # noqa: E731


def step(msg):
    print(f"\n=== {msg}")


step("1. Coordinator submits a site (idempotent: a double-click is harmless)")
body = {"idempotency_key": "demo-key-0001", "site_name": "Riverside Clinical Research", "investigator_name": "Dr. Alex Morgan",
        "region": "NA", "country": "US", "contact_email": "pi@riverside.example.org"}
r1 = c.post("/sites", json=body, headers=H("demo-coord-na"))
r2 = c.post("/sites", json=body, headers=H("demo-coord-na"))
sid = r1.json()["site"]["id"]
print("first submit :", r1.status_code, "created =", r1.json()["created"])
print("replayed     :", r2.status_code, "created =", r2.json()["created"], "(same site id)", r2.json()["site"]["id"] == sid)

step("2. A second, brand-new submission of the SAME site is blocked as a duplicate")
r = c.post("/sites", json={**body, "idempotency_key": "demo-key-0002"}, headers=H("demo-coord-na"))
print(r.status_code, r.json()["detail"])

step("3. Access control: a European coordinator cannot see the NA site")
print("EU coordinator GET /sites/<id> ->", c.get(f"/sites/{sid}", headers=H("demo-coord-eu")).status_code)
print("no API key                      ->", c.get("/sites").status_code)

step("4. Compliance cannot jump the queue; Legal approves; chain advances")
print("compliance tries early ->", c.post(f"/sites/{sid}/decisions", json={"decision": "approve"}, headers=H("demo-compliance-1")).status_code)
r = c.post(f"/sites/{sid}/decisions", json={"decision": "approve", "comment": "Contract reviewed"}, headers=H("demo-legal-1"))
print("legal approves ->", r.json()["status"])
r = c.post(f"/sites/{sid}/decisions", json={"decision": "reject", "comment": "GCP certificate expired"}, headers=H("demo-compliance-1"))
print("compliance rejects ->", r.json()["status"], "(Site Ops is never asked)")
print("site ops queue:", c.get("/my/tasks", headers=H("demo-siteops-1")).json()["count"], "tasks")

step("5. Who did what, when (hash-chained audit trail)")
for e in c.get(f"/sites/{sid}/audit", headers=H("demo-admin-1")).json()["events"]:
    print(f"  {e['occurred_at']}  {e['event_type']:<16} by {e['actor_email']}")
print("chain verification:", c.get("/audit/verify", headers=H("demo-admin-1")).json())

step("6. Live leadership report (no monthly snapshot)")
print(json.dumps(c.get("/reports/summary", headers=H("demo-leadership-1")).json(), indent=2))
