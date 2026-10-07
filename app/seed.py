"""
Demo users. The API keys below are DEMO CREDENTIALS for local runs and tests
only; production identity comes from SSO, not static keys.
"""
from __future__ import annotations

import sqlite3

from .auth import hash_key

DEMO_USERS = [
    # id, email, name, role, region, api_key
    ("u-coord-na", "coord.na@example.org", "Nora (Coordinator NA)", "coordinator", "NA", "demo-coord-na"),
    ("u-coord-eu", "coord.eu@example.org", "Emil (Coordinator EU)", "coordinator", "EU", "demo-coord-eu"),
    ("u-legal-1", "legal1@example.org", "Lena (Legal)", "legal", None, "demo-legal-1"),
    ("u-legal-2", "legal2@example.org", "Liam (Legal)", "legal", None, "demo-legal-2"),
    ("u-comp-1", "compliance1@example.org", "Cara (Compliance)", "compliance", None, "demo-compliance-1"),
    ("u-ops-1", "siteops1@example.org", "Sam (Site Ops)", "site_ops", None, "demo-siteops-1"),
    ("u-lead-1", "leadership1@example.org", "Priya (Leadership)", "leadership", None, "demo-leadership-1"),
    ("u-admin-1", "admin1@example.org", "Ada (Admin)", "admin", None, "demo-admin-1"),
]


def seed_demo_users(conn: sqlite3.Connection) -> None:
    for uid, email, name, role, region, key in DEMO_USERS:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, name, role, region, api_key_hash) VALUES (?,?,?,?,?,?)",
            (uid, email, name, role, region, hash_key(key)),
        )
