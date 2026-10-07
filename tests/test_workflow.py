"""Workflow behaviour: the state machine, ordering, and rejection semantics."""
from conftest import decide, h, submit


def test_full_approval_path(client):
    site = submit(client)
    assert site["status"] == "PENDING_LEGAL" and site["version"] == 1

    r = decide(client, site["id"], "legal1")
    assert r.status_code == 200 and r.json()["status"] == "PENDING_COMPLIANCE"
    r = decide(client, site["id"], "compliance")
    assert r.json()["status"] == "PENDING_SITE_OPS"
    r = decide(client, site["id"], "site_ops")
    body = r.json()
    assert body["status"] == "APPROVED" and body["current_stage"] is None and body["completed_at"]
    assert body["version"] == 4

    detail = client.get(f"/sites/{site['id']}", headers=h("admin")).json()
    assert [a["stage"] for a in detail["approvals"]] == ["legal", "compliance", "site_ops"]


def test_rejection_halts_chain_immediately(client):
    """Old system: a Legal rejection still sent Compliance and Site Ops an approval request."""
    site = submit(client)
    r = decide(client, site["id"], "legal1", "reject", "Investigator conflict of interest not disclosed")
    assert r.json()["status"] == "REJECTED"

    # Compliance is never asked: the site is not visible to them and cannot be decided.
    assert decide(client, site["id"], "compliance").status_code == 404
    assert client.get("/my/tasks", headers=h("compliance")).json()["count"] == 0
    # Even an approver who can see it cannot act on a finalized site.
    assert decide(client, site["id"], "legal2").status_code == 409


def test_rejection_requires_reason(client):
    site = submit(client)
    r = decide(client, site["id"], "legal1", "reject")
    assert r.status_code == 422


def test_cannot_skip_or_reorder_stages(client):
    site = submit(client)
    # Compliance cannot approve before Legal has: the site isn't even visible to them yet.
    assert decide(client, site["id"], "compliance").status_code == 404
    decide(client, site["id"], "legal1")
    # Legal cannot decide the compliance stage.
    r = decide(client, site["id"], "legal1")
    assert r.status_code == 409 and "already" in r.json()["detail"]


def test_wrong_role_forbidden(client):
    site = submit(client)
    assert decide(client, site["id"], "coord_na").status_code in (403, 404)
    assert decide(client, site["id"], "leadership").status_code == 403  # read-only role
    assert decide(client, site["id"], "admin").status_code == 403  # no override: separation of duties


def test_optimistic_version_conflict(client):
    site = submit(client)
    r = decide(client, site["id"], "legal1", expected_version=99)
    assert r.status_code == 409 and r.json()["current_version"] == 1
    assert decide(client, site["id"], "legal1", expected_version=1).status_code == 200


def test_resubmission_allowed_after_rejection(client):
    site = submit(client, n=1)
    decide(client, site["id"], "legal1", "reject", "Missing GCP certificate")
    again = client.post("/sites", json={**__import__("conftest").site_payload(1), "idempotency_key": "idem-key-resubmit"},
                        headers=h("coord_na"))
    assert again.status_code == 201
