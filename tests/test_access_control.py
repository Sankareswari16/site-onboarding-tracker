"""Access control: the shared-link problem. No key, no data; each role sees only its slice."""
from conftest import decide, h, submit


def test_no_credentials_no_data(client):
    assert client.get("/sites").status_code == 401
    assert client.get("/sites", headers={"X-API-Key": "wrong"}).status_code == 401


def test_coordinator_sees_only_own_region(client):
    na = submit(client, 1, "coord_na", region="NA")
    eu = submit(client, 2, "coord_eu", region="EU")
    ids_na = {s["id"] for s in client.get("/sites", headers=h("coord_na")).json()["results"]}
    assert ids_na == {na["id"]}
    # Other region's site is indistinguishable from a nonexistent one.
    assert client.get(f"/sites/{eu['id']}", headers=h("coord_na")).status_code == 404


def test_approver_sees_only_sites_at_or_past_their_stage(client):
    site = submit(client)
    assert client.get(f"/sites/{site['id']}", headers=h("compliance")).status_code == 404
    assert client.get(f"/sites/{site['id']}", headers=h("legal1")).status_code == 200
    decide(client, site["id"], "legal1")
    # Compliance now sees it; Legal retains read access to what they decided.
    assert client.get(f"/sites/{site['id']}", headers=h("compliance")).status_code == 200
    assert client.get(f"/sites/{site['id']}", headers=h("legal2")).status_code == 200


def test_leadership_reads_everything_but_cannot_write(client):
    submit(client, 1, "coord_na", region="NA")
    submit(client, 2, "coord_eu", region="EU")
    assert client.get("/sites", headers=h("leadership")).json()["count"] == 2
    assert client.post("/admin/escalations/run", headers=h("leadership")).status_code == 403


def test_reports_and_admin_endpoints_are_role_gated(client):
    for who in ("coord_na", "legal1", "compliance", "site_ops"):
        assert client.get("/reports/summary", headers=h(who)).status_code == 403
        assert client.get("/audit/verify", headers=h(who)).status_code == 403
        assert client.post("/admin/outbox/flush", headers=h(who)).status_code == 403
    assert client.get("/reports/summary", headers=h("leadership")).status_code == 200


def test_task_queue_is_per_stage(client):
    a = submit(client, 1)
    submit(client, 2)
    assert client.get("/my/tasks", headers=h("legal1")).json()["count"] == 2
    assert client.get("/my/tasks", headers=h("compliance")).json()["count"] == 0
    decide(client, a["id"], "legal1")
    assert client.get("/my/tasks", headers=h("compliance")).json()["count"] == 1
