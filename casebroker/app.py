"""HTTP API for the case broker.

Deliberately small. Everything transactional lives in :mod:`casebroker.db`; this
module is transport, auth and shape-checking only, so the storage engine can be
swapped for Postgres without touching the protocol the workers speak.

Auth is a shared bearer token. That is proportionate: the service hands out CFD
case specs and accepts result pointers, so the worst a leaked token buys an
attacker is the ability to waste our compute or poison result rows -- bad, but
not a reason to run an identity provider for a research campaign. Rotate by
changing ``CASEBROKER_TOKENS`` and restarting.
"""

from __future__ import annotations

import hmac
import os
import pathlib
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db, ids

MAX_LEASE_SECONDS = 24 * 3600

# The dashboard shell (static/dashboard.html) carries no secrets -- it prompts the
# viewer for a bearer token client-side and calls the JSON API with it, exactly like
# any other API client. So it is served with no Auth dependency; the DATA it displays
# is still gated by the same token check as every other endpoint.
_STATIC_DIR = pathlib.Path(__file__).parent / "static"


def _redact_db_target(db_path: str) -> str:
    """A safe-to-display summary of what CASEBROKER_DB points at.

    For SQLite this is just a local file path -- not a secret. For a Postgres
    DSN it is the connection string with the PASSWORD masked (host, port,
    username and database name stay visible; they are diagnostically useful and
    none of them is the credential). /healthz is deliberately unauthenticated so
    infrastructure health checks work with no token, which is exactly why
    nothing bearing a credential may ever appear in what it returns.

    Found the hard way: an earlier version returned db_path verbatim, so hitting
    /healthz against a real Postgres deployment printed the live database
    password in plain text to whoever (or whatever terminal, log, or transcript)
    made the request -- with no auth required to trigger it.
    """
    if not db_path.startswith(("postgres://", "postgresql://")):
        return db_path
    parts = urlsplit(db_path)
    netloc = parts.netloc
    if parts.password:
        netloc = netloc.replace(f":{parts.password}@", ":***@")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


# -- payloads -----------------------------------------------------------------

class CaseIn(BaseModel):
    lat: float
    lon: float
    recipe: str
    city_cluster: str
    lcz: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    max_attempts: int = 3


class LeaseIn(BaseModel):
    worker_id: str
    count: int = Field(default=1, ge=1, le=64)
    lease_seconds: int = Field(default=3600, ge=60, le=MAX_LEASE_SECONDS)
    splits: list[str] | None = None


class LeaseOut(BaseModel):
    case_id: str
    lease_id: str
    expires_at: int
    attempt: int
    spec: dict[str, Any]


class HeartbeatIn(BaseModel):
    lease_id: str
    lease_seconds: int = Field(default=3600, ge=60, le=MAX_LEASE_SECONDS)
    detail: str | None = None


class CompleteIn(BaseModel):
    lease_id: str
    result_uri: str
    sha256: str | None = None
    bytes: int | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class FailIn(BaseModel):
    lease_id: str
    error: str
    retryable: bool = True


class ReleaseIn(BaseModel):
    lease_id: str
    reason: str = "released"



def create_app(db_path: str | None = None, tokens: list[str] | None = None) -> FastAPI:
    """Build an app bound to one database and token set.

    A factory rather than module globals, because module-global config makes the
    tests lie: each test would have to reload the module to rebind the database,
    and ``from casebroker import app`` returns the STALE module after a
    ``sys.modules`` pop (the parent package keeps its own attribute), so one
    test's connection silently serves the next test's requests. Found exactly
    that way -- a test that passed alone and failed in the suite.
    """
    db_path = db_path or os.environ.get("CASEBROKER_DB", "casebroker.sqlite")
    if tokens is None:
        tokens = [t.strip() for t in
                  os.environ.get("CASEBROKER_TOKENS", "").split(",") if t.strip()]

    app = FastAPI(title="Wind v2 case broker", version="0.1.0")
    conn = db.connect(db_path)
    app.state.db_path = db_path

    def require_token(request: Request) -> None:
        # No tokens configured means auth is OFF. Fine for a laptop smoke test,
        # never how this should face a network -- /healthz reports which mode it
        # is in so a misconfigured deployment is visible rather than silent.
        if not tokens:
            return
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        supplied = header[len(prefix):] if header.startswith(prefix) else ""
        # compare_digest against each configured token: constant-time, and it
        # does not reveal which token matched.
        if not any(hmac.compare_digest(supplied, t) for t in tokens):
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    Auth = Depends(require_token)

    # -- routes -------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "auth": "token" if tokens else "OPEN",
                "db": _redact_db_target(db_path)}


    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        """A minimal ops UI: campaign status, workers, one-case lookup. Vanilla HTML/JS,
        no build step, no external requests other than to this broker's own API."""
        return FileResponse(_STATIC_DIR / "dashboard.html")


    @app.post("/v1/cases", dependencies=[Auth])
    def add_cases(cases: list[CaseIn]) -> dict[str, int]:
        """Append cases to the campaign. Safe to re-run: existing ids are skipped,
        so growing 5k -> 15k is 'post the new list' and nothing else."""
        if len(cases) > 5000:
            raise HTTPException(413, "post at most 5000 cases per request")
        rows = []
        for c in cases:
            rows.append({
                "case_id": ids.case_id(c.lat, c.lon, c.recipe),
                "spec": {**c.spec, "lat": c.lat, "lon": c.lon, "recipe": c.recipe},
                "recipe": c.recipe,
                "city_cluster": c.city_cluster,
                "lcz": c.lcz,
                "split": ids.split_for(c.city_cluster),
                "priority": c.priority,
                "max_attempts": c.max_attempts,
            })
        return db.add_cases(conn, rows)


    @app.post("/v1/lease", response_model=list[LeaseOut], dependencies=[Auth])
    def lease(body: LeaseIn) -> list[LeaseOut]:
        """Claim the next case(s) to simulate. An empty list means the campaign is
        drained (or everything left is leased by someone else) -- the worker should
        back off and retry, not treat it as an error."""
        got = db.lease(conn, body.worker_id, count=body.count,
                       lease_seconds=body.lease_seconds, splits=body.splits)
        return [LeaseOut(case_id=g.case_id, lease_id=g.lease_id, expires_at=g.expires_at,
                         attempt=g.attempt, spec=g.spec) for g in got]


    @app.post("/v1/heartbeat", dependencies=[Auth])
    def heartbeat(body: HeartbeatIn) -> dict[str, bool]:
        ok = db.heartbeat(conn, body.lease_id, body.lease_seconds, body.detail)
        # 409, not 404: the lease existed, it is just no longer the worker's. The
        # worker must abandon the case rather than retry the call.
        if not ok:
            raise HTTPException(409, "lease expired or superseded; stop work on this case")
        return {"ok": True}


    @app.post("/v1/complete", dependencies=[Auth])
    def complete(body: CompleteIn) -> dict[str, bool]:
        if not db.complete(conn, body.lease_id, body.result_uri, body.sha256,
                           body.bytes, body.metrics):
            raise HTTPException(409, "lease expired or superseded; result rejected")
        return {"ok": True}


    @app.post("/v1/fail", dependencies=[Auth])
    def fail(body: FailIn) -> dict[str, bool]:
        if not db.fail(conn, body.lease_id, body.error, body.retryable):
            raise HTTPException(409, "lease expired or superseded")
        return {"ok": True}


    @app.post("/v1/release", dependencies=[Auth])
    def release(body: ReleaseIn) -> dict[str, bool]:
        """Graceful preemption. Refunds the attempt, unlike fail()."""
        if not db.release(conn, body.lease_id, body.reason):
            raise HTTPException(409, "lease expired or superseded")
        return {"ok": True}


    @app.get("/v1/status", dependencies=[Auth])
    def status() -> dict[str, Any]:
        return db.status(conn)


    @app.get("/v1/cases/{case_id}", dependencies=[Auth])
    def get_case(case_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such case")
        return dict(row)

    return app


# Lazy default instance for "uvicorn casebroker.app:app", configured from the
# environment. Built on first attribute access rather than at import, so merely
# importing this module (as the tests do) never creates a stray database file in
# the working directory.
def __getattr__(name: str):
    if name == "app":
        global _default_app
        try:
            return _default_app
        except NameError:
            _default_app = create_app()
            return _default_app
    raise AttributeError(name)
