"""
Authentication and role-based visibility.

Demo authentication is a per-user API key (stored hashed). In production this
layer is replaced by SSO (OIDC against Azure AD / Okta); everything downstream
only depends on the resolved `actor` dict (id, email, role, region), so the
swap is local to this module and the API dependency.
"""
from __future__ import annotations

import hashlib
import sqlite3

from .errors import Unauthorized


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def authenticate(conn: sqlite3.Connection, api_key: str | None) -> dict:
    if not api_key:
        raise Unauthorized("missing X-API-Key header")
    row = conn.execute(
        "SELECT id, email, name, role, region FROM users WHERE api_key_hash = ? AND active = 1",
        (hash_key(api_key),),
    ).fetchone()
    if row is None:
        raise Unauthorized("invalid or inactive API key")
    return dict(row)


def visibility_clause(actor: dict) -> tuple[str, list]:
    """
    SQL predicate (over the `sites` table) describing which sites an actor may see.

    - admin / leadership: everything (leadership is read-only; see the service layer)
    - coordinator: sites in their own region
    - approver (legal/compliance/site_ops): sites currently waiting on their stage,
      plus sites where they already recorded a decision. Sites that have not yet
      reached their stage are not visible (least privilege).
    """
    role = actor["role"]
    if role in ("admin", "leadership"):
        return "1=1", []
    if role == "coordinator":
        return "region = ?", [actor["region"]]
    return (
        "(current_stage = ? OR id IN (SELECT site_id FROM approvals WHERE stage = ?))",
        [role, role],
    )
