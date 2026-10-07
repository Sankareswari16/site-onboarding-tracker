"""Intake: idempotency, duplicate prevention, validation, scoping."""
from conftest import h, site_payload, submit


def test_replay_returns_original_not_duplicate(client):
    first = client.post("/sites", json=site_payload(1), headers=h("coord_na"))
    second = client.post("/sites", json=site_payload(1), headers=h("coord_na"))
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["created"] is False
    assert first.json()["site"]["id"] == second.json()["site"]["id"]
    assert client.get("/sites", headers=h("admin")).json()["count"] == 1


def test_idempotency_key_reuse_with_different_payload_rejected(client):
    client.post("/sites", json=site_payload(1), headers=h("coord_na"))
    r = client.post("/sites", json=site_payload(2, idempotency_key="idem-key-0001"), headers=h("coord_na"))
    assert r.status_code == 409


def test_same_site_with_new_key_is_blocked_as_duplicate(client):
    """A user resubmitting with a fresh key (or a typo-free retry) must not start a second approval chain."""
    first = submit(client, 1)
    r = client.post("/sites", json=site_payload(1, idempotency_key="a-brand-new-key"), headers=h("coord_na"))
    assert r.status_code == 409 and r.json()["existing_site_id"] == first["id"]


def test_case_and_punctuation_variants_are_the_same_site(client):
    submit(client, 1)
    variant = site_payload(1, idempotency_key="variant-key-1",
                           site_name="RIVERSIDE  clinical research, 1", investigator_name="dr alex morgan 1")
    assert client.post("/sites", json=variant, headers=h("coord_na")).status_code == 409


def test_possible_duplicate_warning_for_different_investigator(client):
    submit(client, 1)
    other = site_payload(1, idempotency_key="warn-key-0001", investigator_name="Dr. Someone Else")
    r = client.post("/sites", json=other, headers=h("coord_na"))
    assert r.status_code == 201
    assert r.json()["warnings"][0]["type"] == "possible_duplicate_site_name"


def test_validation_errors_are_explicit(client):
    bad = site_payload(1, region="MARS", contact_email="not-an-email", country="USA")
    r = client.post("/sites", json=bad, headers=h("coord_na"))
    assert r.status_code == 422
    fields = {e["loc"][-1] for e in r.json()["detail"]}
    assert {"region", "contact_email", "country"} <= fields


def test_coordinator_cannot_submit_for_other_region(client):
    r = client.post("/sites", json=site_payload(1, region="EU"), headers=h("coord_na"))
    assert r.status_code == 403


def test_approver_cannot_submit(client):
    assert client.post("/sites", json=site_payload(1), headers=h("legal1")).status_code == 403
