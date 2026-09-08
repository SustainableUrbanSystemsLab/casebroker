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
import json
import os
import time
import pathlib
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, db, footprints, ids

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
    # Where this worker is actually running -- surfaced on the dashboard and in
    # the workers table so "what machine produced this case" is answerable
    # without grepping a SLURM log. Optional: an older worker build, or one run
    # by hand, simply reports unknown.
    host: str | None = None
    cluster: str | None = None


class FleetIn(BaseModel):
    cluster: str
    queued: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    detail: str | None = None


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



def _is_postgres_dsn(target: str) -> bool:
    return target.startswith(("postgres://", "postgresql://"))


def _split_tokens(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def _tokens_from_env(canonical: str, legacy: str) -> list[str]:
    """One token bucket, read from its canonical variable or the old spelling.

    The old names said what a token WAS (``CASEBROKER_TOKENS``) rather than what it
    GRANTS, so the only way to learn that the plain one is read+write -- and that the
    "readonly" one is a strictly smaller subset of it rather than an alternative to
    it -- was to read this file. ``*_WRITE_TOKENS`` / ``*_READ_TOKENS`` name the
    capability, and the legacy spellings keep working so no deployment breaks.

    Both spellings set to DIFFERENT values is refused rather than resolved by
    precedence. Two plausible intents exist there and silently honouring one leaves
    the operator believing a credential is live when it is not -- the failure mode
    is "a token you think you revoked still works", which must never be quiet.
    """
    new = _split_tokens(os.environ.get(canonical, ""))
    old = _split_tokens(os.environ.get(legacy, ""))
    if new and old and set(new) != set(old):
        raise RuntimeError(
            f"{canonical} and {legacy} are both set, to different values. "
            f"{legacy} is the deprecated spelling of {canonical}; set only one."
        )
    return new or old


def create_app(db_path: str | None = None, tokens: list[str] | None = None,
               readonly_tokens: list[str] | None = None) -> FastAPI:
    """Build an app bound to one database and token set(s).

    Two independent buckets, not one list with a flag on each entry: ``tokens``
    can do everything (what a worker carries), ``readonly_tokens`` can only see
    (what a shared dashboard link carries -- "send this to a friend" needs a
    credential that literally cannot lease/complete/fail/release a case or add
    new ones, not a UI that merely hides the buttons for it, since anyone can
    still curl the API directly with whatever token the link handed them).

    A factory rather than module globals, because module-global config makes the
    tests lie: each test would have to reload the module to rebind the database,
    and ``from casebroker import app`` returns the STALE module after a
    ``sys.modules`` pop (the parent package keeps its own attribute), so one
    test's connection silently serves the next test's requests. Found exactly
    that way -- a test that passed alone and failed in the suite.
    """
    from_env = db_path is None
    db_path = db_path or os.environ.get("CASEBROKER_DB", "casebroker.sqlite")
    # A deployment that resolves to SQLite is almost always a misconfiguration:
    # the container filesystem it lands on does not survive a redeploy, and the
    # campaign disappears with no error at any point -- the service comes back
    # up healthy and simply empty. Only warned when the path came from the
    # ENVIRONMENT, which is the deployment path; the test suite passes db_path
    # explicitly and stays quiet.
    if from_env and not _is_postgres_dsn(db_path):
        print(f"[warn] CASEBROKER_DB is a SQLite file ({db_path}). This does NOT "
              "survive a container redeploy. Point it at Postgres for anything "
              "that is not a laptop.", file=sys.stderr)

    if tokens is None:
        tokens = _tokens_from_env("CASEBROKER_WRITE_TOKENS", "CASEBROKER_TOKENS")
    if readonly_tokens is None:
        readonly_tokens = _tokens_from_env("CASEBROKER_READ_TOKENS",
                                           "CASEBROKER_READONLY_TOKENS")

    app = FastAPI(title="Wind v2 case broker", version=__version__)
    conn = db.connect(db_path)
    app.state.db_path = db_path

    def _supplied_token(request: Request) -> str:
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        return header[len(prefix):] if header.startswith(prefix) else ""

    def require_write_token(request: Request) -> None:
        # Neither bucket configured means auth is OFF entirely -- fine for a
        # laptop smoke test, never how this should face a network -- /healthz
        # reports which mode it is in so a misconfigured deployment is visible
        # rather than silent. Configuring ONLY readonly_tokens (no worker
        # tokens at all) is a valid, if unusual, deployment -- it must lock
        # writes out entirely rather than silently falling back to open,
        # which is why this checks `tokens` alone and never falls through to
        # readonly_tokens.
        if not tokens and not readonly_tokens:
            return
        supplied = _supplied_token(request)
        # compare_digest against each configured token: constant-time, and it
        # does not reveal which token matched.
        if not any(hmac.compare_digest(supplied, t) for t in tokens):
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    def require_read_token(request: Request) -> None:
        if not tokens and not readonly_tokens:
            return
        supplied = _supplied_token(request)
        if not any(hmac.compare_digest(supplied, t) for t in (*tokens, *readonly_tokens)):
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    WriteAuth = Depends(require_write_token)
    ReadAuth = Depends(require_read_token)

    # -- routes -------------------------------------------------------------------

    # /healthz is what the uptime badge and the deploy smoke test poll, and it
    # answered only "did FastAPI start?". That is a weaker claim than it looks:
    # the process starts fine against a database it cannot reach, so the badge
    # read "live" for a broker that could not have served a single case. When
    # this repo's own .env stopped authenticating against Supabase there was no
    # way to tell from outside whether production was in the same state.
    #
    # Cached, because anyone who can reach the service can poll this: an uncached
    # probe is a free way to make the broker open a connection per request, and
    # Supabase's pooler answers a flood of those by tripping its breaker --
    # turning the health check into the outage it exists to detect.
    _db_probe: dict[str, Any] = {"at": 0.0, "ok": None}

    def _db_ok() -> bool | None:
        now = time.monotonic()
        if _db_probe["ok"] is not None and now - _db_probe["at"] < 30.0:
            return _db_probe["ok"]
        try:
            conn.execute("select 1")
            ok: bool | None = True
        except Exception:                                    # noqa: BLE001
            # Deliberately not re-raised, and deliberately undetailed: this
            # endpoint is unauthenticated, so WHY a connection failed -- host,
            # role, TLS posture -- is not ours to publish. False is the whole
            # signal; the logs carry the rest.
            ok = False
        _db_probe["at"] = now
        _db_probe["ok"] = ok
        return ok

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        # `version` is safe to expose unauthenticated -- it is already in the
        # public OpenAPI document and in the repo -- and it is what lets the
        # deploy smoke test assert that the RUNNING service is the commit that
        # was just pushed, instead of trusting a deploy's own status field.
        return {"ok": True, "version": __version__,
                "auth": "token" if (tokens or readonly_tokens) else "OPEN",
                # COUNTS, never values: how many credentials of each capability
                # exist is what an operator needs to answer "did my rotation
                # actually land?", and it discloses nothing usable.
                "scopes": {"write": len(tokens), "read": len(readonly_tokens)},
                # kept for older dashboards that read this field by name
                "readonly_auth": bool(readonly_tokens),
                # Whether the broker can actually REACH its database, as opposed
                # to merely having started. `ok` stays True either way: the
                # process is up, and collapsing the two would leave the badge
                # unable to tell "service down" from "database down".
                "db_ok": _db_ok(),
                "db": _redact_db_target(db_path)}


    @app.get("/v1/share-token", dependencies=[WriteAuth])
    def share_token() -> dict[str, Any]:
        """The read-only token, for building a shareable link. WRITE auth required.

        This is not an escalation, which is the only reason it can exist: a write
        token already passes every read gate, so a caller who can call this can
        already do strictly more than the credential it returns. Handing them the
        lesser one discloses nothing they could not otherwise reach.

        It exists because the alternative was worse in practice -- the dashboard
        asked an operator to go and fetch CASEBROKER_READ_TOKENS out of the
        hosting provider's environment tab and paste it into a form, which is a
        procedure that ends with production credentials in clipboards and chat
        messages. One click that never shows the value is safer than a workflow
        that requires copying a secret by hand.

        404 rather than an empty string when no read token is configured: the
        dashboard must say "no read-only token is set on this broker" instead of
        silently offering a link that cannot authenticate.
        """
        if not readonly_tokens:
            raise HTTPException(404, "no read-only token is configured on this broker")
        return {"token": readonly_tokens[0]}

    @app.get("/v1/whoami")
    def whoami(request: Request) -> dict[str, Any]:
        """What can the presented token do? Deliberately unauthenticated.

        Holding a token string, the only previous way to discover its capability was
        to attempt a mutating call and see whether it 401'd -- which means the way to
        find out was to try to change production data. This answers it directly, and
        answers it the same way the real dependencies do (same constant-time compare,
        same buckets), so it cannot drift from what the gates actually enforce.

        200 in every case, including a bad token (`scope: "none"`): distinguishing
        "this credential is wrong" from "the broker is unreachable" is the entire
        point, and a 401 would conflate them. This reveals no more than any guarded
        endpoint already does -- the token is either in a bucket or it is not.
        """
        supplied = _supplied_token(request)
        if not tokens and not readonly_tokens:
            return {"scope": "write", "auth": "OPEN",
                    "detail": "no tokens configured; every caller has full access"}
        if any(hmac.compare_digest(supplied, t) for t in tokens):
            return {"scope": "write", "auth": "token"}
        if any(hmac.compare_digest(supplied, t) for t in readonly_tokens):
            return {"scope": "read", "auth": "token"}
        return {"scope": "none", "auth": "token"}

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        """A minimal ops UI: campaign status, workers, one-case lookup. Vanilla HTML/JS,
        no build step, no external requests other than to this broker's own API."""
        return FileResponse(_STATIC_DIR / "dashboard.html")


    @app.post("/v1/cases", dependencies=[WriteAuth])
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


    @app.post("/v1/lease", response_model=list[LeaseOut], dependencies=[WriteAuth])
    def lease(body: LeaseIn) -> list[LeaseOut]:
        """Claim the next case(s) to simulate. An empty list means the campaign is
        drained (or everything left is leased by someone else) -- the worker should
        back off and retry, not treat it as an error."""
        got = db.lease(conn, body.worker_id, count=body.count,
                       lease_seconds=body.lease_seconds, splits=body.splits,
                       host=body.host, cluster=body.cluster)
        return [LeaseOut(case_id=g.case_id, lease_id=g.lease_id, expires_at=g.expires_at,
                         attempt=g.attempt, spec=g.spec) for g in got]


    @app.post("/v1/heartbeat", dependencies=[WriteAuth])
    def heartbeat(body: HeartbeatIn) -> dict[str, bool]:
        ok = db.heartbeat(conn, body.lease_id, body.lease_seconds, body.detail)
        # 409, not 404: the lease existed, it is just no longer the worker's. The
        # worker must abandon the case rather than retry the call.
        if not ok:
            raise HTTPException(409, "lease expired or superseded; stop work on this case")
        return {"ok": True}


    @app.post("/v1/complete", dependencies=[WriteAuth])
    def complete(body: CompleteIn) -> dict[str, bool]:
        if not db.complete(conn, body.lease_id, body.result_uri, body.sha256,
                           body.bytes, body.metrics):
            raise HTTPException(409, "lease expired or superseded; result rejected")
        return {"ok": True}


    @app.post("/v1/fail", dependencies=[WriteAuth])
    def fail(body: FailIn) -> dict[str, bool]:
        if not db.fail(conn, body.lease_id, body.error, body.retryable):
            raise HTTPException(409, "lease expired or superseded")
        return {"ok": True}


    @app.post("/v1/release", dependencies=[WriteAuth])
    def release(body: ReleaseIn) -> dict[str, bool]:
        """Graceful preemption. Refunds the attempt, unlike fail()."""
        if not db.release(conn, body.lease_id, body.reason):
            raise HTTPException(409, "lease expired or superseded")
        return {"ok": True}


    @app.post("/v1/fleet", dependencies=[WriteAuth])
    def report_fleet(body: FleetIn) -> dict[str, str]:
        """Tell the broker what a scheduler is holding that has not arrived yet.

        The broker cannot see SLURM: a queued worker has never called it, so it
        does not exist here until its first lease. That makes "5,000 pending, 0
        leased" true and unhelpful -- it cannot distinguish "nothing is coming"
        from "twenty workers are third in the queue". Whoever can run squeue
        pushes that in here.

        Write-scoped because it is an assertion about the campaign that the
        dashboard will show as fact, not a read.
        """
        db.report_fleet(conn, body.cluster, body.queued, body.running, body.detail)
        return {"cluster": body.cluster}

    @app.get("/v1/status", dependencies=[ReadAuth])
    def status() -> dict[str, Any]:
        # `version` and `db` are carried here as well as on /healthz so a client
        # that can read status never needs a SECOND request to identify what it
        # is talking to. The dashboard previously took them from /healthz, which
        # meant that if that one path failed -- and it did, reproducibly, for a
        # browser behind a filter that objected to a response containing what
        # looks like a connection string -- the page connected successfully and
        # then could not say which broker or database it had connected TO.
        #
        # Costs nothing: both values are already in this process's memory, and
        # this endpoint is read-authenticated, so it discloses strictly less
        # widely than /healthz already does unauthenticated.
        return {**db.status(conn), "version": __version__,
                "db": _redact_db_target(db_path)}


    @app.get("/v1/cases/{case_id}/footprints", dependencies=[ReadAuth])
    def case_footprints(case_id: str, refresh: bool = False) -> dict[str, Any]:
        """Overture building footprints for this case, as GeoJSON.

        The dashboard cannot fetch these itself: Overture publishes GeoParquet on
        S3 and a Python client, with no REST API and no published tile endpoint,
        so a browser has nothing to call. The broker runs the query.

        It is deliberately the SAME release and the same bbox derivation the
        runner uses, so the picture is the geometry that gets meshed. Drawing
        OSM footprints or a map tile instead would be worse than drawing
        nothing: it would look like a check while disagreeing with the mesh, and
        it would disagree most exactly where checking matters -- the sites where
        Overture is empty but OSM is not.

        Cached after the first fetch; `refresh=true` forces a re-query.
        """
        row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such case")
        row = dict(row)
        if not refresh:
            hit = db.get_footprints(conn, case_id)
            if hit:
                return {**json.loads(hit["geojson"]), "cached": True,
                        "fetched_at": hit["fetched_at"]}
        spec = row.get("spec") or {}
        if isinstance(spec, str):
            spec = json.loads(spec)
        lat, lon = spec.get("lat"), spec.get("lon")
        if lat is None or lon is None:
            raise HTTPException(422, "case spec carries no lat/lon")
        try:
            # GBA is what the geometry builder now defaults to, so it is what
            # gets meshed, so it is what this must draw. Overture stays as the
            # fallback rather than being deleted: it is one HTTP dependency
            # against another, and an inspector that 502s is useless exactly
            # when someone is trying to find out why a case looks wrong.
            try:
                fc = footprints.fetch_gba(float(lat), float(lon))
            except Exception as gba_err:             # noqa: BLE001
                fc = footprints.fetch(float(lat), float(lon))
                fc["source"] = "overture"
                fc["fallback_from"] = f"gba unavailable: {str(gba_err)[:120]}"
            # Whether this site has real bare-earth terrain or will be meshed
            # flat. Cached with the footprints because it is the same question --
            # "what will this case actually be made of" -- and because finding
            # out after 66 core-hours is worse than finding out now.
            fc["terrain"] = footprints.terrain(float(lat), float(lon))
        except Exception as e:                       # noqa: BLE001
            # 502, not 500: the failure is upstream at the building-data source,
            # and saying so keeps it out of the broker's own error budget.
            raise HTTPException(502, f"building query failed: {e}") from e
        db.put_footprints(conn, case_id, json.dumps(fc), fc["n"])
        return {**fc, "cached": False}

    @app.get("/v1/cases/{case_id}", dependencies=[ReadAuth])
    def get_case(case_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such case")
        return dict(row)


    @app.get("/v1/cases", dependencies=[ReadAuth])
    def list_cases(state: str | None = None, split: str | None = None,
                   city_cluster: str | None = None, limit: int = 50,
                   offset: int = 0) -> dict[str, Any]:
        """A page of cases for the dashboard's case browser -- most recently
        touched first, optionally filtered by state/split/city. Distinct from
        ``GET /v1/cases/{case_id}`` (one case by id, used for a direct lookup)."""
        return db.list_cases(conn, state=state, split=split, city_cluster=city_cluster,
                             limit=limit, offset=offset)

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
