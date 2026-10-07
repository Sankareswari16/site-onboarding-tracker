"""
The shared-workbook race condition. These tests drive the service layer from
many threads, each with its own DB connection, at the same moment.
"""
import threading
import sqlite3

from app import db, service
from app.auth import authenticate
from app.errors import Conflict
from conftest import FakeClock, site_payload, KEYS


def _actor(db_path, who):
    c = db.connect(db_path)
    a = authenticate(c, KEYS[who])
    c.close()
    return a


def _run_threads(fn, n):
    results, barrier = [None] * n, threading.Barrier(n)

    def worker(i):
        barrier.wait()  # release all threads together to maximise contention
        try:
            results[i] = ("ok", fn(i))
        except Exception as exc:  # noqa: BLE001
            results[i] = ("err", exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def test_concurrent_decisions_exactly_one_wins(client, db_path, clock):
    from conftest import submit
    site = submit(client)
    legal1, legal2 = _actor(db_path, "legal1"), _actor(db_path, "legal2")

    def attempt(i):
        conn = db.connect(db_path)
        try:
            return service.decide(conn, legal1 if i % 2 else legal2, site["id"], "approve", None, None, clock())
        finally:
            conn.close()

    results = _run_threads(attempt, 8)
    winners = [r for r in results if r[0] == "ok"]
    losers = [r for r in results if r[0] == "err"]
    assert len(winners) == 1, results
    assert all(isinstance(e, Conflict) and "already approved" in e.message for _, e in losers)

    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM approvals WHERE site_id=?", (site["id"],)).fetchone()[0] == 1
    # Exactly one transition, so exactly one 'stage_approved' audit entry.
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE site_id=? AND event_type='stage_approved'",
                        (site["id"],)).fetchone()[0] == 1
    assert service.get_site(conn, legal1, site["id"])["version"] == 2
    conn.close()


def test_concurrent_identical_submissions_create_one_site(db_path, clock, client):
    coord = _actor(db_path, "coord_na")
    payload = site_payload(7)

    def attempt(i):
        conn = db.connect(db_path)
        try:
            return service.create_site(conn, coord, dict(payload), clock())
        finally:
            conn.close()

    results = _run_threads(attempt, 10)
    assert all(r[0] == "ok" for r in results), results
    created = [r for r in results if r[1][1] is True]
    assert len(created) == 1
    assert len({r[1][0]["id"] for r in results}) == 1
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 1
    conn.close()


def test_concurrent_distinct_submissions_all_land_with_valid_audit_chain(db_path, clock, client):
    coord = _actor(db_path, "coord_na")

    def attempt(i):
        conn = db.connect(db_path)
        try:
            return service.create_site(conn, coord, site_payload(100 + i), clock())
        finally:
            conn.close()

    results = _run_threads(attempt, 12)
    assert all(r[0] == "ok" for r in results), results
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 12
    # Interleaved writers must still produce one linear, verifiable chain.
    from app import audit
    chain = audit.verify_chain(conn)
    assert chain["ok"] and chain["entries_checked"] == 12
    conn.close()
