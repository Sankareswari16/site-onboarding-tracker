from __future__ import annotations

import time
from typing import Callable

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from . import audit, outbox, service
from .auth import authenticate
from .clock import utcnow
from .config import DEFAULT_DB_PATH
from .db import connect, init_db
from .errors import Conflict, DomainError, Forbidden, NotFound, Unauthorized
from .logging_utils import get_logger, log
from .schemas import DecisionIn, SiteCreate
from .seed import seed_demo_users

logger = get_logger("api")

_STATUS = {Unauthorized: 401, Forbidden: 403, NotFound: 404, Conflict: 409}


def create_app(db_path: str | None = None, clock: Callable = utcnow,
               notifier: outbox.Notifier | None = None, seed_demo: bool = True) -> FastAPI:
    db_path = db_path or DEFAULT_DB_PATH
    notifier = notifier or outbox.LogNotifier()
    init_db(db_path)
    if seed_demo:
        c = connect(db_path)
        seed_demo_users(c)
        c.close()

    app = FastAPI(title="Investigator Site Onboarding Tracker", version="1.0.0")

    # ------------------------------------------------------------ plumbing --
    def get_conn():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def get_actor(x_api_key: str | None = Header(default=None), conn=Depends(get_conn)) -> dict:
        return authenticate(conn, x_api_key)

    def require_roles(actor: dict, *roles: str) -> None:
        if actor["role"] not in roles:
            raise Forbidden(f"requires role: {', '.join(roles)}")

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError):
        status = next((s for cls, s in _STATUS.items() if isinstance(exc, cls)), 400)
        return JSONResponse(status_code=status, content={"detail": exc.message, **exc.extra})

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        log(logger, "api_request", method=request.method, path=request.url.path,
            status=response.status_code, duration_ms=round((time.perf_counter() - start) * 1000, 2))
        return response

    # --------------------------------------------------------------- routes --
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/sites", status_code=201)
    def submit_site(body: SiteCreate, actor=Depends(get_actor), conn=Depends(get_conn)):
        site, created, warnings = service.create_site(conn, actor, body.model_dump(), clock())
        # Replay of an already-processed submission returns 200 with the original record.
        return JSONResponse(status_code=201 if created else 200,
                            content={"site": site, "created": created, "warnings": warnings})

    @app.get("/sites")
    def list_sites(status: str | None = None, region: str | None = None,
                   limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                   actor=Depends(get_actor), conn=Depends(get_conn)):
        items = service.list_sites(conn, actor, status=status, region=region, limit=limit, offset=offset)
        return {"count": len(items), "results": items}

    @app.get("/sites/{site_id}")
    def get_site(site_id: str, actor=Depends(get_actor), conn=Depends(get_conn)):
        return service.get_site(conn, actor, site_id)

    @app.post("/sites/{site_id}/decisions")
    def decide(site_id: str, body: DecisionIn, actor=Depends(get_actor), conn=Depends(get_conn)):
        return service.decide(conn, actor, site_id, body.decision, body.comment, body.expected_version, clock())

    @app.get("/sites/{site_id}/audit")
    def site_audit(site_id: str, actor=Depends(get_actor), conn=Depends(get_conn)):
        service.get_site(conn, actor, site_id)  # enforces visibility
        return {"site_id": site_id, "events": audit.for_site(conn, site_id)}

    @app.get("/my/tasks")
    def my_tasks(actor=Depends(get_actor), conn=Depends(get_conn)):
        tasks = service.my_tasks(conn, actor, clock())
        return {"count": len(tasks), "tasks": tasks}

    @app.get("/reports/summary")
    def report_summary(actor=Depends(get_actor), conn=Depends(get_conn)):
        require_roles(actor, "leadership", "admin")
        return service.summary(conn, clock())

    @app.get("/audit/verify")
    def verify_audit(actor=Depends(get_actor), conn=Depends(get_conn)):
        require_roles(actor, "leadership", "admin")
        return audit.verify_chain(conn)

    @app.post("/admin/escalations/run")
    def run_escalations(actor=Depends(get_actor), conn=Depends(get_conn)):
        require_roles(actor, "admin")
        escalated = service.run_escalations(conn, clock())
        return {"escalated": len(escalated), "sites": escalated}

    @app.post("/admin/outbox/flush")
    def flush_outbox(actor=Depends(get_actor), conn=Depends(get_conn)):
        require_roles(actor, "admin")
        return outbox.flush(conn, notifier, clock())

    return app

