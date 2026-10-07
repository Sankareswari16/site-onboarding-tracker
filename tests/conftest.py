import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import create_app  # noqa: E402
from app import db  # noqa: E402


class FakeClock:
    """Controllable clock so SLA/escalation behaviour is testable without sleeping."""

    def __init__(self):
        self.now = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


KEYS = {
    "coord_na": "demo-coord-na", "coord_eu": "demo-coord-eu",
    "legal1": "demo-legal-1", "legal2": "demo-legal-2",
    "compliance": "demo-compliance-1", "site_ops": "demo-siteops-1",
    "leadership": "demo-leadership-1", "admin": "demo-admin-1",
}


def h(who):
    return {"X-API-Key": KEYS[who]}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def client(db_path, clock):
    app = create_app(db_path=db_path, clock=clock)
    return TestClient(app)


def site_payload(n=1, region="NA", **over):
    base = {
        "idempotency_key": f"idem-key-{n:04d}",
        "site_name": f"Riverside Clinical Research {n}",
        "investigator_name": f"Dr. Alex Morgan {n}",
        "region": region,
        "country": "US" if region == "NA" else "DE",
        "contact_email": f"site{n}@example.org",
    }
    base.update(over)
    return base


def submit(client, n=1, who="coord_na", **over):
    r = client.post("/sites", json=site_payload(n, **over), headers=h(who))
    assert r.status_code in (200, 201), r.text
    return r.json()["site"]


def decide(client, site_id, who, decision="approve", comment=None, **extra):
    body = {"decision": decision, "comment": comment, **extra}
    return client.post(f"/sites/{site_id}/decisions", json=body, headers=h(who))
