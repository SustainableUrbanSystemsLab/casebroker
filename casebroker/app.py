"""HTTP API for the E3D Simulation Broker.

Deliberately small. Everything transactional lives in :mod:`casebroker.db`; this
module is transport, auth and shape-checking only, so the storage engine can be
swapped for Postgres without touching the protocol the workers speak.

Three kinds of principal, and the differences are deliberate. A HUMAN logs in
with a password and gets an expiring server-side session; an ``admin`` may change
the campaign and manage identity, a ``viewer`` may only read it. A MACHINE cannot
type a password, so it carries a long-lived per-machine token, checked against
the database on every request so revoking one box takes effect on its next call
rather than at the next redeploy -- and a machine token deliberately cannot mint
more machine tokens. A SHARED ENVIRONMENT TOKEN is the older model and still
works: the live fleet runs on one, and an auth change that stranded workers
mid-lease would be worse than carrying both for a while.

With no environment tokens and no accounts, auth is off entirely -- fine for a
laptop smoke test, and ``/healthz`` says so rather than leaving it silent.
See ``docs/operations.md`` for the first-run sequence.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
import pathlib
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, auth, db, footprints, ids

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

class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)
    # Only consulted when the deployment sets CASEBROKER_SETUP_TOKEN. Accepted
    # in the body as well as a bearer header so the browser setup form can send
    # it without inventing a header.
    setup_token: str | None = Field(default=None, max_length=256)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class WorkerTokenIn(BaseModel):
    # The worker id the machine will run under, so the credential and the
    # dashboard row are the same thing.
    name: str = Field(min_length=1, max_length=64)


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)
    # Defaults to the LESSER privilege on purpose: an operator adding a
    # colleague to watch the campaign should have to ask for admin explicitly,
    # not discover afterwards that they handed over the keys.
    role: str = Field(default="viewer")


class PasswordIn(BaseModel):
    # Required when changing your OWN password, so that a borrowed session
    # cannot lock the real owner out. An admin resetting someone ELSE'S
    # password does not supply it -- the whole point of a reset is that the
    # current one is lost.
    current_password: str | None = Field(default=None, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class RoleIn(BaseModel):
    role: str = Field(min_length=1, max_length=32)


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
    # Cases this worker holds a local checkpoint for (see db.lease): claimed
    # first, and handed back without spending an attempt when still leased to
    # this same worker_id. Never honoured for a different worker -- the
    # checkpoint is on that machine's own disk.
    resume_case_ids: list[str] | None = Field(default=None, max_length=64)


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


def _may_lease_as(credential_name: str, worker_id: str) -> bool:
    """Whether a per-machine credential may claim work as ``worker_id``.

    Exact match is the workstation case. The prefix form is the CLUSTER case:
    the sbatch scripts run every SLURM task as ``phoenix-<job>-<task>``, ids
    the scheduler hands out, so one credential per task is impossible and an
    exact-match rule left clusters on the shared env token forever -- the
    un-revocable, un-attributed model per-machine credentials exist to
    replace. A credential named ``phoenix`` therefore covers everything under
    ``phoenix-``. Attribution holds at the only granularity a cluster credential
    can have: the lease records the per-task id, and the credential that
    produced it is its prefix.

    The dash is load-bearing: ``lab`` does not cover ``laboratory``.
    """
    return worker_id == credential_name or worker_id.startswith(credential_name + "-")


def create_app(db_path: str | None = None, tokens: list[str] | None = None,
               readonly_tokens: list[str] | None = None,
               setup_token: str | None = None) -> FastAPI:
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
    # Optional, and the answer to "who gets to be the admin of a broker that is
    # already on the internet?". /v1/auth/setup cannot require a session -- there
    # is nobody to authenticate as yet -- so on a public deployment the first
    # stranger to find it becomes the permanent sole admin. Setting this makes
    # setup require a secret the operator already holds. Unset keeps the open
    # first-run flow, which is right for a laptop or a broker behind a firewall.
    if setup_token is None:
        setup_token = os.environ.get("CASEBROKER_SETUP_TOKEN", "").strip() or None

    app = FastAPI(title="E3D Simulation Broker", version=__version__)
    conn = db.connect(db_path)
    app.state.db_path = db_path

    def _supplied_token(request: Request) -> str:
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        return header[len(prefix):] if header.startswith(prefix) else ""

    SESSION_COOKIE = "wsb_session"

    def _session_principal(request: Request):
        """The logged-in human behind this request, if any."""
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        return db.session_user(conn, auth.hash_token(raw))

    def _machine_principal(request: Request):
        """The machine behind this request's bearer token, if the token is a
        per-machine one issued from the admin UI.

        Checked against the DATABASE, not a cached environment list, which is
        what makes revocation take effect on the very next request instead of
        at the next redeploy.
        """
        supplied = _supplied_token(request)
        if not supplied:
            return None
        return db.worker_token_owner(conn, auth.hash_token(supplied))

    def _auth_is_open() -> bool:
        """No env tokens configured AND no accounts: every caller has full
        access. Fine for a laptop smoke test, never how this should face a
        network.

        Both halves matter, and the account half was missing from everything
        that reported this posture (`/healthz`, `/v1/whoami`) while being
        present in the gates themselves. A deployment secured entirely by
        accounts -- the from-scratch path -- therefore reported "OPEN" and
        handed `scope: write` to any string at all, which made `casebroker
        health` and `casebroker token check` report the exact opposite of the
        truth on a correctly secured broker.
        """
        return not tokens and not readonly_tokens and db.count_users(conn) == 0

    def _ct_eq(a: str, b: str) -> bool:
        """Constant-time equality that cannot raise.

        `hmac.compare_digest` refuses a non-ASCII str with TypeError, and the
        credential compared here is attacker-chosen: a bearer header (which the
        server decodes as latin-1, so any byte becomes a character) or a JSON
        body. A single `\xe9` in an Authorization header used to 500 /healthz --
        an endpoint that by design needs no credential to reach, and that the
        uptime badge polls. Encoding both sides first makes every input
        comparable without changing the timing property.
        """
        return hmac.compare_digest(a.encode("utf-8", "surrogatepass"),
                                   b.encode("utf-8", "surrogatepass"))

    def _env_token_ok(supplied: str, bucket) -> bool:
        # Constant-time against each configured token, and it does not reveal
        # which one matched.
        return any(_ct_eq(supplied, t) for t in bucket)

    def require_write_token(request: Request) -> None:
        """Three ways to be allowed to write, in the order they are cheapest.

        A logged-in human, a per-machine token from the database, or one of the
        shared environment tokens. The last is the OLD model and is kept working
        on purpose: the fleet is running on one right now, and an auth change
        that strands live workers mid-lease is a worse outcome than a
        transitional period with both.
        """
        # Neither bucket configured AND no accounts means auth is OFF entirely
        # -- fine for a laptop smoke test, never how this should face a network
        # -- /healthz reports which mode it is in so a misconfigured deployment
        # is visible rather than silent. Once an account exists the service is
        # no longer open, even with no env tokens set.
        if _auth_is_open():
            return
        user = _session_principal(request)
        if user and user["role"] == "admin":
            return
        if _machine_principal(request):
            return
        # Configuring ONLY readonly_tokens (no worker tokens at all) is a valid,
        # if unusual, deployment -- it must lock writes out entirely rather than
        # silently falling back to open, which is why this checks `tokens`
        # alone and never falls through to readonly_tokens.
        if _env_token_ok(_supplied_token(request), tokens):
            return
        if user:
            # Checked LAST, not on sight: a viewer's cookie rides along on every
            # request from that browser, and rejecting immediately would refuse
            # a request that also carried a perfectly good write credential.
            raise HTTPException(
                status_code=403,
                detail="this account is a viewer; it can read the campaign "
                       "but not change it")
        raise HTTPException(status_code=401, detail="log in, or send a valid bearer token")

    def require_read_token(request: Request) -> None:
        if _auth_is_open():
            return
        if _session_principal(request) or _machine_principal(request):
            return
        if _env_token_ok(_supplied_token(request), (*tokens, *readonly_tokens)):
            return
        raise HTTPException(status_code=401, detail="log in, or send a valid bearer token")

    def require_admin(request: Request):
        """For the endpoints that manage identity itself. A machine token is
        deliberately NOT enough here: a worker credential that could mint more
        worker credentials would defeat the point of issuing them per machine.
        """
        user = _session_principal(request)
        if not user:
            raise HTTPException(status_code=401, detail="admin session required")
        if user["role"] != "admin":
            # The `role` column existed from the start and nothing read it, so
            # every account was an admin whatever its row said. A viewer that
            # could mint machine credentials would make the role decorative.
            raise HTTPException(
                status_code=403,
                detail="this account is a viewer; managing accounts and machine "
                       "credentials needs an admin")
        return user

    def _is_authenticated(request: Request) -> bool:
        """Whether this caller is anybody at all -- without raising.

        For endpoints that must answer an anonymous caller, but can say more to
        an operator. `require_*` raise, which is wrong when the response has to
        succeed either way.
        """
        if _session_principal(request) or _machine_principal(request):
            return True
        return _env_token_ok(_supplied_token(request), (*tokens, *readonly_tokens))

    def require_session(request: Request):
        """Any logged-in human, viewer included. For the endpoints a viewer must
        reach on their own behalf -- changing their own password."""
        user = _session_principal(request)
        if not user:
            raise HTTPException(status_code=401, detail="log in first")
        return user

    WriteAuth = Depends(require_write_token)
    ReadAuth = Depends(require_read_token)
    AdminAuth = Depends(require_admin)
    SessionAuth = Depends(require_session)

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
    _db_probe: dict[str, Any] = {"at": 0.0, "ok": None, "accounts": None}

    def _db_state() -> tuple[bool | None, bool | None]:
        """(is the database reachable, does it hold any account) -- one cached
        probe for both.

        The account count belongs in HERE, not in the handler body. Every
        database touch on this endpoint has to fail SOFT: an unguarded count
        made /healthz answer 500 during an outage instead of `db_ok: false`,
        which is the single distinction the endpoint exists to draw. The
        Dockerfile HEALTHCHECK reads a 500 as unhealthy, so a database blip
        would have restart-looped a broker that was itself fine, and
        `casebroker health` would have reported "could not reach" -- a database
        outage misdiagnosed as an unreachable service, exactly the confusion
        `db_ok` was added to remove.

        Caching it matters for the same reason the reachability probe is cached:
        this endpoint is unauthenticated and the badge polls it, so an uncached
        count is a free way to make anyone open a connection per request.
        """
        now = time.monotonic()
        if _db_probe["ok"] is not None and now - _db_probe["at"] < 30.0:
            return _db_probe["ok"], _db_probe["accounts"]
        try:
            conn.execute("select 1")
            ok: bool | None = True
        except Exception:                                    # noqa: BLE001
            # Deliberately not re-raised, and deliberately undetailed: this
            # endpoint is unauthenticated, so WHY a connection failed -- host,
            # role, TLS posture -- is not ours to publish. False is the whole
            # signal; the logs carry the rest.
            ok = False
        accounts: bool | None = None
        if ok:
            try:
                # Separately guarded, and separate from `select 1` on purpose: a
                # database that predates the identity tables answers the former
                # and not the latter, and that is a schema problem, not an
                # unreachable database.
                accounts = db.count_users(conn) > 0
            except Exception:                                # noqa: BLE001
                accounts = None
        _db_probe.update({"at": now, "ok": ok, "accounts": accounts})
        return ok, accounts

    def _forget_db_probe() -> None:
        """Drop the cached posture after something that changes it.

        Without this, creating the first account leaves /healthz reporting
        "OPEN" for up to the cache window -- which is precisely the moment an
        operator runs `casebroker health` to confirm the opposite, because the
        first-run instructions tell them to.
        """
        _db_probe.update({"at": 0.0, "ok": None, "accounts": None})

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        # `version` is safe to expose unauthenticated -- it is already in the
        # public OpenAPI document and in the repo -- and it is what lets the
        # deploy smoke test assert that the RUNNING service is the commit that
        # was just pushed, instead of trusting a deploy's own status field.
        # "accounts" is a THIRD posture, and reporting it as OPEN was the bug
        # that made `casebroker health` exit non-zero -- its documented use as a
        # deploy gate -- against a broker that was properly locked down.
        db_ok, has_accounts = _db_state()
        if tokens or readonly_tokens:
            posture = "token"
        elif has_accounts:
            posture = "accounts"
        elif has_accounts is None:
            # The database is unreachable, so whether an account exists is not
            # knowable. Saying "OPEN" here would raise a false alarm about auth
            # during what is really a database outage -- and `db_ok` below
            # already reports that outage accurately.
            posture = "unknown"
        else:
            posture = "OPEN"
        return {"ok": True, "version": __version__,
                "auth": posture,
                # A boolean, never the count: /v1/auth/state already tells an
                # anonymous caller whether this broker has been set up, so this
                # discloses nothing new, and the exact number of operators is
                # not the internet's business.
                "accounts": has_accounts,
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
                "db_ok": db_ok,
                # The DSN summary is for OPERATORS, not for the internet.
                # Masking the password is necessary but not sufficient: what is
                # left still names the exact database instance, its host, port
                # and username, which is reconnaissance handed out for free on
                # an endpoint that by design needs no credential. `db_ok` above
                # is the part a health check actually needs, and it stays
                # public; the key stays present-but-null so a client reading it
                # by name does not break.
                "db": _redact_db_target(db_path) if _is_authenticated(request) else None}


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
        # Most specific principal first, so the answer names WHICH credential
        # was recognised rather than merely what it can do.
        user = _session_principal(request)
        if user:
            return {"scope": "read" if user["role"] != "admin" else "write",
                    "auth": "session", "user": user["username"],
                    "role": user["role"]}
        machine = _machine_principal(request)
        if machine:
            # Previously absent, and the reason the documented worker
            # onboarding aborted: setup_windows.ps1 runs `token check --expect
            # write` against this endpoint, and a dashboard-issued per-machine
            # token -- the credential the docs tell you to use -- came back
            # `scope: none`.
            return {"scope": "write", "auth": "machine", "machine": machine["name"]}
        supplied = _supplied_token(request)
        if _env_token_ok(supplied, tokens):
            return {"scope": "write", "auth": "token"}
        if _env_token_ok(supplied, readonly_tokens):
            return {"scope": "read", "auth": "token"}
        if _auth_is_open():
            return {"scope": "write", "auth": "OPEN",
                    "detail": "no tokens and no accounts; every caller has full access"}
        return {"scope": "none", "auth": "token"}

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        """A minimal ops UI: campaign status, workers, one-case lookup. Vanilla HTML/JS,
        no build step, no external requests other than to this broker's own API."""
        return FileResponse(_STATIC_DIR / "dashboard.html")


    # -- identity: who you are, and which machine that is -------------------

    @app.get("/v1/auth/state")
    def auth_state(request: Request) -> dict[str, Any]:
        """What the login UI needs before anything is entered.

        Unauthenticated on purpose -- it is what tells a first-time visitor
        whether to show the SETUP form or the LOGIN form, and it leaks nothing
        beyond "does this deployment have an account yet", which is already
        obvious from whether logging in is possible.
        """
        user = _session_principal(request)
        return {
            "needs_setup": db.count_users(conn) == 0,
            # So the setup form can ask for the bootstrap secret up front
            # instead of failing the submit. Whether one is REQUIRED is not
            # itself a secret; its value never leaves the server.
            "setup_token_required": bool(setup_token or tokens) and db.count_users(conn) == 0,
            "user": user["username"] if user else None,
            "role": user["role"] if user else None,
            # An env token still works; the UI says so, so the transition is
            # visible rather than a mystery when a pasted token keeps working.
            "env_tokens": bool(tokens or readonly_tokens),
        }

    @app.post("/v1/auth/setup")
    def auth_setup(body: SetupIn, request: Request, response: Response) -> dict[str, Any]:
        """Create the FIRST account. Open only while there are none.

        This is the one endpoint that cannot require authentication -- there is
        nobody to authenticate as yet -- so it closes permanently the moment it
        succeeds. A second caller gets 409, not another admin.
        """
        if db.count_users(conn) > 0:
            raise HTTPException(409, "already set up -- log in instead")
        # WHO may claim the first account.
        #
        # Nobody can be authenticated here -- there is no account yet -- so the
        # question is whether this deployment already holds a credential that
        # identifies its operator. If it does, setup must demand one.
        #
        # Without this, a broker running on a shared env token -- which is
        # EXACTLY what production looks like before anyone has set it up -- hands
        # its permanent admin account, and with it the power to mint machine
        # credentials, to the first stranger who finds the form. Read tokens are
        # deliberately not accepted: a credential that cannot change the campaign
        # must not be able to create the account that can.
        if setup_token:
            accepted = [setup_token]
            hint = "the setup token (CASEBROKER_SETUP_TOKEN)"
        elif tokens:
            accepted = list(tokens)
            hint = "one of its write tokens (CASEBROKER_WRITE_TOKENS)"
        else:
            accepted = []       # nothing configured: a laptop, or behind a firewall
            hint = ""
        if accepted:
            # EITHER carrier. Giving the header precedence would mean a stale
            # bearer token left in a client's config blocked a correct body value.
            offered = [t for t in (_supplied_token(request), body.setup_token or "") if t]
            if not any(_ct_eq(o, a) for o in offered for a in accepted):
                # 403 rather than 401: the caller is not expected to have a
                # session, so "authenticate yourself" would be misleading.
                raise HTTPException(
                    403, f"this broker requires {hint} to create its first account")
        if len(body.password) < 12:
            # Length over composition rules: this guards a service reachable
            # from the internet, and a short password is the only property that
            # reliably predicts a guessable one.
            raise HTTPException(400, "password must be at least 12 characters")
        # Explicitly admin: this is the account that has to be able to create
        # every other one, and UserIn defaults the other direction.
        user = db.create_user(conn, body.username,
                              auth.hash_password(body.password), role="admin")
        _forget_db_probe()          # this deployment is no longer "OPEN"
        _issue_session(request, response, user["id"])
        return {"username": user["username"], "role": user["role"]}

    def _is_https(request: Request) -> bool:
        """Whether this request arrived over TLS.

        Drives the cookie's Secure flag, and getting it wrong fails in a way
        that gives no clue: a Secure cookie sent over http is DISCARDED by the
        client silently, so login appears to succeed and the next request is
        anonymous. Setting it from a static config is the footgun -- production
        and localhost would need different values and nobody would remember.

        X-Forwarded-Proto first because Render (like every TLS-terminating
        proxy) speaks plain http to the app, so request.url.scheme alone would
        say "http" in production and drop the Secure flag exactly where it
        matters most.
        """
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        if forwarded:
            return forwarded == "https"
        return request.url.scheme == "https"

    def _issue_session(request: Request, response: Response, user_id: int) -> str:
        raw = auth.new_token()
        db.start_session(conn, user_id, auth.hash_token(raw), auth.session_expiry())
        response.set_cookie(
            SESSION_COOKIE, raw,
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,      # JavaScript must never be able to read it
            samesite="lax",     # a cross-site POST must not carry it
            secure=_is_https(request),
            path="/",
        )
        return raw

    # Failed logins, for the throttle below. In-process, so it resets on a
    # redeploy and is per worker process -- the honest scope for it. The goal is
    # to make ONLINE guessing impractical against a handful of lab accounts, not
    # to survive a distributed attack; scrypt already makes each attempt cost
    # ~100 ms, and this caps how many of those an attacker gets.
    _login_failures: dict[str, list[float]] = {}
    _login_lock = threading.Lock()
    LOGIN_FAIL_WINDOW = 300.0
    LOGIN_FAIL_LIMIT = 10
    # The username is attacker-chosen and need not exist, so without a cap an
    # anonymous caller can grow this map indefinitely. Well above any real
    # deployment's account count, and evicting the stalest entry is correct
    # behaviour rather than a mere safeguard: the stalest is also the one whose
    # window is most likely to have expired anyway.
    LOGIN_FAIL_MAX_KEYS = 4096

    def _throttle_key(request: Request, username: str) -> str:
        # The SOCKET address deliberately, not X-Forwarded-For -- unlike the
        # Secure-cookie decision, which reads X-Forwarded-Proto. A forwarded
        # header's leftmost value is supplied by the caller, so keying on it
        # would let an attacker rotate it and evade the throttle entirely,
        # which is worse than the cost of not using it: behind a
        # TLS-terminating proxy every caller shares one apparent address, so
        # one attacker can throttle the others for that username.
        client = request.client.host if request.client else "?"
        return f"{username}|{client}"

    @app.post("/v1/auth/login")
    def auth_login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
        key = _throttle_key(request, body.username)
        now = time.monotonic()
        # The slot is RESERVED before the password is checked, and released
        # again only on success. Counting a failure afterwards instead would
        # bound nothing under concurrency: verify_password is ~100 ms of scrypt,
        # so a whole wave of simultaneous attempts passes the check while the
        # count is still zero and only the NEXT wave sees the failures. Measured
        # at 15 attempts admitted against a limit of 10 before this.
        with _login_lock:
            recent = [t for t in _login_failures.get(key, [])
                      if now - t < LOGIN_FAIL_WINDOW]
            if len(recent) >= LOGIN_FAIL_LIMIT:
                _login_failures[key] = recent
                raise HTTPException(
                    429, "too many failed logins for this account from this "
                         "address; wait a few minutes")
            _login_failures[key] = recent + [now]
            if len(_login_failures) > LOGIN_FAIL_MAX_KEYS:
                stalest = min(_login_failures, key=lambda k: _login_failures[k][-1])
                _login_failures.pop(stalest, None)
        user = db.get_user(conn, body.username)
        # Verify even when the user does not exist, against a throwaway hash, so
        # a wrong USERNAME and a wrong PASSWORD take the same time. Otherwise the
        # response time enumerates accounts.
        stored = user["password_hash"] if user else auth.hash_password("decoy")
        if not auth.verify_password(body.password, stored) or not user:
            # The reservation above stands as the failure record.
            raise HTTPException(401, "wrong username or password")
        with _login_lock:
            _login_failures.pop(key, None)      # success releases the whole run
        # The one place a sweep costs nothing and cannot be forgotten: expiry is
        # already enforced at read time, so this only stops the table growing
        # without bound over a long-lived campaign.
        db.purge_expired_sessions(conn)
        _issue_session(request, response, user["id"])
        return {"username": user["username"], "role": user["role"]}

    @app.post("/v1/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict[str, Any]:
        raw = request.cookies.get(SESSION_COOKIE)
        if raw:
            db.end_session(conn, auth.hash_token(raw))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    # -- per-machine credentials -------------------------------------------

    @app.get("/v1/workers/tokens")
    def list_tokens(user=AdminAuth) -> dict[str, Any]:
        """Every machine credential, with when it was last used.

        `last_seen_at` is the question a shared secret could never answer: which
        box is this, and is it still alive?
        """
        return {"tokens": db.list_worker_tokens(conn)}

    @app.post("/v1/workers/tokens")
    def issue_token(body: WorkerTokenIn, user=AdminAuth) -> dict[str, Any]:
        """Mint a credential for one machine and return it ONCE.

        Only the hash is stored, so this response is the only time the token
        exists in readable form. That is deliberate: a credential the server can
        show you again is a credential an attacker can read out of the database.
        """
        raw = auth.new_token()
        try:
            db.create_worker_token(conn, body.name, auth.hash_token(raw),
                                   created_by=user["username"])
        except Exception:
            # UNIQUE(name): re-issuing for a machine that already has one would
            # silently strand whichever credential the box is actually using.
            raise HTTPException(409, f"a token for {body.name!r} already exists -- "
                                     "revoke it first if the machine needs a new one")
        return {"name": body.name, "token": raw,
                "hint": "copy it now -- only its hash is stored, so it cannot be shown again"}

    @app.delete("/v1/workers/tokens/{name}")
    def revoke_token(name: str, user=AdminAuth) -> dict[str, Any]:
        """Revoke one machine. Effective on its next request: the check is a row
        read, not a cached environment variable, so there is no redeploy."""
        if not db.revoke_worker_token(conn, name):
            raise HTTPException(404, f"no active token named {name!r}")
        return {"name": name, "revoked": True}

    # -- accounts ----------------------------------------------------------
    #
    # Until these existed there was exactly ONE way an account could come into
    # being -- the one-shot /v1/auth/setup -- and no way at all to add a second
    # operator, change a password, or recover from a forgotten one. A lab
    # broker with one permanent credential and no reset is a deployment one
    # departure away from being unmanageable.

    @app.get("/v1/users")
    def list_accounts(user=AdminAuth) -> dict[str, Any]:
        return {"users": db.list_users(conn)}

    @app.post("/v1/users")
    def create_account(body: UserIn, user=AdminAuth) -> dict[str, Any]:
        """Add an operator. Admin-only, and never a way to escalate: the caller
        is already an admin, so it grants nothing it does not itself hold."""
        if body.role not in db.ROLES:
            raise HTTPException(400, "role must be one of %s" % ", ".join(db.ROLES))
        try:
            created = db.create_user(conn, body.username,
                                     auth.hash_password(body.password), role=body.role)
        except Exception:
            # UNIQUE(username). Deliberately not "does this user exist?" as a
            # separate probe -- this endpoint is admin-only, so there is no
            # enumeration concern, but one code path is one thing to get wrong.
            raise HTTPException(409, f"an account named {body.username!r} already exists")
        _forget_db_probe()
        return {"username": created["username"], "role": created["role"]}

    @app.delete("/v1/users/{username}")
    def delete_account(username: str, user=AdminAuth) -> dict[str, Any]:
        try:
            removed = db.delete_user(conn, username)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if not removed:
            raise HTTPException(404, f"no account named {username!r}")
        _forget_db_probe()
        return {"username": username, "deleted": True}

    @app.post("/v1/users/{username}/role")
    def set_account_role(username: str, body: RoleIn, user=AdminAuth) -> dict[str, Any]:
        try:
            changed = db.set_role(conn, username, body.role)
        except ValueError as exc:
            # Covers both an unknown role and demoting the last admin, which is
            # the one change that can leave a deployment unmanageable.
            raise HTTPException(409, str(exc))
        if not changed:
            raise HTTPException(404, f"no account named {username!r}")
        return {"username": username, "role": body.role}

    @app.post("/v1/users/{username}/password")
    def change_password(username: str, body: PasswordIn, request: Request,
                        response: Response, caller=SessionAuth) -> dict[str, Any]:
        """Change your own password, or -- as an admin -- reset someone else's.

        Changing your OWN requires the current one even for an admin: a session
        cookie that leaked from a logged-in laptop should not be enough to lock
        the actual owner out of their account.
        """
        target = db.get_user(conn, username)
        if not target:
            raise HTTPException(404, f"no account named {username!r}")
        if caller["username"] == username:
            if not body.current_password or not auth.verify_password(
                    body.current_password, target["password_hash"]):
                raise HTTPException(403, "current password is wrong")
        elif caller["role"] != "admin":
            raise HTTPException(403, "only an admin can reset another account's password")
        db.set_password(conn, username, auth.hash_password(body.new_password))
        if caller["username"] == username:
            # set_password revoked every session this account held, this one
            # included. Re-issue so changing your own password does not log you
            # out of the tab you changed it in.
            _issue_session(request, response, caller["id"])
        return {"username": username, "password_changed": True,
                "sessions_revoked": True}


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


    @app.delete("/v1/cases", dependencies=[WriteAuth])
    def purge_cases(expect: int | None = None, recipe: str | None = None,
                    state: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        """Delete cases from the campaign, with their events and footprints.

        Exists so that "the 5,000 cases were built from a superseded recipe and
        have to go" does not become a psql session against production. It runs
        HERE because this is where the database credentials already are -- the
        alternative is distributing them to every operator's laptop.

        `dry_run` defaults to TRUE: the destructive form has to be asked for
        explicitly, so a half-remembered curl reports what it would have done
        instead of doing it. `expect` is the real interlock -- state the row
        count you believe you are deleting and a mismatch aborts untouched,
        which is what catches a filter that is subtly wrong rather than empty.

        Deleting a case does not delete the archive a worker already wrote;
        result_uri points at a file on the machine that produced it. The
        response says how many doomed rows carried one, so orphaned archives
        are a number you were told rather than one you discover later.
        """
        return db.purge_cases(conn, recipe=recipe, state=state,
                              expect=expect, dry_run=dry_run)


    @app.post("/v1/lease", response_model=list[LeaseOut], dependencies=[WriteAuth])
    def lease(body: LeaseIn, request: Request) -> list[LeaseOut]:
        """Claim the next case(s) to simulate. An empty list means the campaign is
        drained (or everything left is leased by someone else) -- the worker should
        back off and retry, not treat it as an error."""
        # A per-machine credential may only claim work AS its own machine.
        #
        # This is the only endpoint where identity is asserted: heartbeat,
        # complete, fail and release are all keyed by lease_id, and the lease
        # already records who holds it. So checking here covers the rest.
        #
        # The dashboard has always told operators the token name "must match the
        # machine's CASEBROKER_WORKER_ID" and nothing enforced it, which made
        # every row in the Machines list and the workers table a claim rather
        # than a fact -- exactly the attribution that issuing one credential per
        # box exists to provide. Env tokens are deliberately unaffected: they are
        # shared by design, so there is no machine identity to contradict.
        #
        # "Its own" includes every id UNDER its name (see _may_lease_as): a
        # cluster credential named `phoenix` covers the `phoenix-<job>-<task>`
        # ids its SLURM tasks actually run as.
        machine = _machine_principal(request)
        if machine and not _may_lease_as(machine["name"], body.worker_id):
            raise HTTPException(
                403, f"this credential belongs to {machine['name']!r}, so it "
                     f"cannot lease as {body.worker_id!r}. Use that machine's own "
                     "token, or set CASEBROKER_WORKER_ID to match it -- or to "
                     f"anything under it, such as {machine['name']}-<job>-<task>.")
        got = db.lease(conn, body.worker_id, count=body.count,
                       lease_seconds=body.lease_seconds, splits=body.splits,
                       host=body.host, cluster=body.cluster,
                       resume_case_ids=body.resume_case_ids)
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
        row = db.get_case(conn, case_id)
        if row is None:
            raise HTTPException(404, "no such case")
        return row


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
