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
import contextlib
import os
import secrets
import threading
import time
import pathlib
import sys
import weakref
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, auth, dataset, db, footprints, ids, notify, places

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


class PairStartIn(BaseModel):
    # Arrives from a machine nobody has authenticated yet, and ends up in an
    # admin's browser -- so it is a strict charset, not "any 64 characters" like
    # the admin-typed WorkerTokenIn below. It is also the worker id the machine
    # will lease under, and ids are hostnames and cluster prefixes, never prose.
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    # SHA-256 of a token the NODE generated. The raw token never comes here.
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    host: str | None = Field(default=None, max_length=128)
    platform: str | None = Field(default=None, max_length=64)


class PairPollIn(BaseModel):
    user_code: str = Field(min_length=4, max_length=16)


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
    # WHICH CODE is asking. All optional and additive: a worker built before
    # them still leases exactly as it did. `build` is version+commit -- the
    # product version is the same for every push, so it cannot tell two nodes
    # apart -- and `recipes` are the exact recipes this build can produce.
    build: str | None = Field(default=None, max_length=96)
    version: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=32)
    recipes: list[str] | None = Field(default=None, max_length=64)


class ReleaseIn_(BaseModel):
    """One published build for one platform. Named with a trailing underscore
    because ReleaseIn is already the body of POST /v1/release (giving a case
    back), which is a different meaning of the same word."""
    build: str = Field(min_length=3, max_length=96)
    platform: str = Field(min_length=3, max_length=32)
    file: str = Field(min_length=1, max_length=200)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    notes: str | None = Field(default=None, max_length=500)


class TargetIn(BaseModel):
    build: str | None = Field(default=None, max_length=96)
    apply: str | None = Field(default=None, max_length=16)


class PolicyIn(BaseModel):
    require_build: bool | None = None
    blocked_builds: list[str] | None = Field(default=None, max_length=64)


class DrainIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


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


class NotifyIn(BaseModel):
    """Settings -> Notifications. Every field optional: only the ones sent change."""
    url: str | None = None
    token: str | None = None
    events: list[str] | None = None
    public_url: str | None = None


class HeartbeatIn(BaseModel):
    lease_id: str
    lease_seconds: int = Field(default=3600, ge=60, le=MAX_LEASE_SECONDS)
    detail: str | None = None


class CompleteIn(BaseModel):
    lease_id: str
    # Optional, and additive on purpose: a worker built before this field still
    # completes normally. It scopes the "was this already written?" check that
    # makes a lost response safe to retry -- without it that check matches any
    # done case carrying the same result_uri, which is only unique if the runner
    # made it so.
    case_id: str | None = None
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


class TelemetryIn(BaseModel):
    """One kind of structured telemetry for the case a lease holds; see
    db.post_telemetry. `case_id` is required, unlike CompleteIn's: telemetry is
    new, so there is no older node to stay compatible with, and it is what stops
    one case's report landing on another."""
    lease_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    data: dict[str, Any]



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


# How many sync request handlers may run at once.
#
# FastAPI runs every sync endpoint in anyio's threadpool, which defaults to 40.
# That number is sized for a machine, not for a 512 MB instance whose resident
# floor is already ~280 MB once DuckDB and GDAL load -- and it buys nothing here,
# because db._LOCK serialises the database work those handlers exist to do. What
# it does buy is 40 simultaneous request bodies, 40 stack frames deep in scrypt
# or a GeoJSON parse, and a queue that grows until the platform kills the
# process. Lower is not slower for this workload; it is the same throughput with
# a bound on the worst case.
REQUEST_CONCURRENCY = int(os.environ.get("CASEBROKER_REQUEST_CONCURRENCY", "12"))


def _apply_thread_limit() -> None:
    """Bound the running event loop's threadpool. Never fatal.

    anyio's limiter is per event loop, which is why this is a function called at
    startup rather than a value set at import: at import time there is no loop to
    set it on.
    """
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = (
            REQUEST_CONCURRENCY)
    except Exception as e:                               # noqa: BLE001
        print(f"[warn] could not bound the threadpool: {e}", file=sys.stderr)


# How many password verifications may run at once, process-wide.
#
# scrypt at RFC 7914's interactive parameters is ~16 MB per call, and that cost
# is the point -- it is what makes a stolen hash expensive. But uvicorn's default
# threadpool is 40 wide and /v1/auth/login needs no credential, so nothing
# stopped 40 simultaneous attempts asking for ~640 MB on a 512 MB instance that
# already sits at ~280 MB once DuckDB and GDAL are resident. Four at a time caps
# it near 64 MB; the cost is that a burst of logins queues rather than the
# platform killing the process mid-lease.
PASSWORD_CONCURRENCY = int(os.environ.get("CASEBROKER_PASSWORD_CONCURRENCY", "4"))
_password_slots = threading.Semaphore(PASSWORD_CONCURRENCY)


def _hash_password(password: str) -> str:
    """scrypt, under the concurrency bound. Every caller in this module goes through it."""
    with _password_slots:
        return auth.hash_password(password)


def _verify_password(password: str, stored: str) -> bool:
    """scrypt, under the concurrency bound."""
    with _password_slots:
        return auth.verify_password(password, stored)


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


def _if_none_match(header: str | None, etag: str) -> bool:
    """Whether an `If-None-Match` header matches, by RFC 7232's WEAK comparison.

    The weak part is not pedantry. The deployed broker sits behind Cloudflare,
    which gzips the dashboard and, doing so, rewrites the strong ETag this app
    sends into a weak one -- the browser then sends back `W/"33c77-18d6ee..."`
    for an entity tagged `"33c77-18d6ee..."`. An exact string comparison says no
    to every one of those, which is a 304 path that can never fire in production
    while passing every test written against the app directly. Measured after
    deploying exactly that: HTTP 200, 212,087 bytes, for a request carrying the
    server's own tag back.

    A weak validator is the right comparison for a cache revalidation anyway:
    it asks "is this the same representation", which is what a browser reusing
    a cached page needs, not "is this byte-for-byte the same entity".
    """
    if not header:
        return False
    for raw in header.split(","):
        candidate = raw.strip()
        if candidate == "*":
            return True
        if candidate.startswith(("W/", "w/")):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


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

    # Applied on startup rather than in the Dockerfile's CMD so it holds however
    # the app is started -- uvicorn, gunicorn, or a test client -- instead of
    # only in the one invocation someone remembered to pass a flag to.
    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI):
        _apply_thread_limit()
        # The first dataset aggregate after a start is the expensive one (every
        # site's country is looked up once, then remembered), so it is started
        # now, in the background, rather than by whoever first opens a case.
        try:
            dataset_cache.peek()
        except Exception as e:                           # noqa: BLE001
            print(f"[warn] could not start the dataset warm-up: {e}", file=sys.stderr)
        # Push notifications (casebroker/notify.py). The poller always runs; it
        # sends only when an ntfy topic is configured (Settings, else env), and
        # re-reads that every poll. Started here, not at import, so a test client
        # that never enters the lifespan never starts a thread.
        notifier = notify.from_env(conn)
        notifier.start()
        try:
            yield
        finally:
            notifier.stop()

    app = FastAPI(title="E3D Simulation Broker", version=__version__,
                  lifespan=_lifespan)
    conn = db.connect(db_path)
    app.state.db_path = db_path
    # Per app, not per module, for the reason this is a factory at all: a
    # module-level cache would serve one test's campaign to the next test.
    # Exposed on app.state so a test can drive its clock.
    dataset_cache = dataset.DatasetCache(lambda: dataset.stream_rows(conn))
    app.state.dataset = dataset_cache

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
        if _may_write_as(request, ("admin", "operator")):
            return
        user = _session_principal(request)
        if user:
            # Checked LAST, not on sight: a viewer's cookie rides along on every
            # request from that browser, and rejecting immediately would refuse
            # a request that also carried a perfectly good write credential.
            raise HTTPException(
                status_code=403,
                detail=f"this account is a {user['role']}; it can read the "
                       "campaign but not change it")
        raise HTTPException(status_code=401, detail="log in, or send a valid bearer token")

    def require_purge(request: Request) -> None:
        """For DELETE /v1/cases, which retires a whole campaign.

        Identical to the write gate except that an `operator` session is not
        enough: adding, leasing and completing cases is the daily work, while
        deleting them and their events and footprints is the one campaign
        operation with nothing behind it. An admin, or a bearer write token,
        still passes -- a machine token could always call this, and narrowing
        that here would strand the documented `curl` in operations.md without
        making anything safer, since the token holder can simply use it.
        """
        if _may_write_as(request, ("admin",)):
            return
        user = _session_principal(request)
        if user:
            raise HTTPException(
                status_code=403,
                detail=f"this account is a {user['role']}; purging a campaign "
                       "needs an admin")
        raise HTTPException(status_code=401, detail="log in, or send a valid bearer token")

    def _may_write_as(request: Request, roles: tuple[str, ...]) -> bool:
        """Shared by the write and purge gates, which differ ONLY in which
        logged-in roles they accept.

        The order matters and is the same in both: a session of a sufficient
        role, then a machine token, then an environment write token. A session
        whose role is too weak falls THROUGH to the token checks rather than
        refusing on sight, so a browser that is logged in as a viewer and also
        carrying a real write token is not turned away by the cookie.
        """
        if _auth_is_open():
            return True
        user = _session_principal(request)
        if user and user["role"] in roles:
            return True
        if _machine_principal(request):
            return True
        # Configuring ONLY readonly_tokens (no worker tokens at all) is a valid,
        # if unusual, deployment -- it must lock writes out entirely rather than
        # silently falling back to open, which is why this checks `tokens`
        # alone and never falls through to readonly_tokens.
        return _env_token_ok(_supplied_token(request), tokens)

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
            # every account was an admin whatever its row said. An operator that
            # could mint machine credentials or create accounts would make the
            # role decorative -- and a credential it issued would outlive the
            # account that issued it.
            raise HTTPException(
                status_code=403,
                detail=f"this account is a {user['role']}; managing accounts and "
                       "machine credentials needs an admin")
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
    PurgeAuth = Depends(require_purge)
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
            db.ping(conn)
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
        # Every database touch on this endpoint has to fail SOFT -- the same rule
        # _db_state() above states, and the same bug one line further out.
        # _is_authenticated resolves a session cookie through db.session_user and
        # ANY bearer token through db.worker_token_owner (the env token included,
        # because revocation is checked against the database), so during an
        # outage a CREDENTIALLED /healthz raised where an anonymous one answered
        # db_ok: false. The dashboard's session cookie is scoped to "/", which
        # made the operator signed in to diagnose the outage the one caller who
        # could not see it: the wizard said "Waiting for the broker to answer
        # /healthz." instead of "cannot reach its database". A container
        # HEALTHCHECK carrying a credential would have restart-looped a broker
        # that was itself fine, which is what db_ok exists to prevent.
        try:
            authed = _is_authenticated(request)
        except Exception:                                    # noqa: BLE001
            # Failing CLOSED: the only field this gates is the DSN summary, so a
            # credential that cannot be resolved must hide it, never expose it.
            authed = False
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
                "db": _redact_db_target(db_path) if authed else None}


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
    def dashboard(request: Request) -> Response:
        """A minimal ops UI: campaign status, workers, one-case lookup. Vanilla HTML/JS,
        no build step, no external requests other than to this broker's own API.

        The conditional request is answered HERE because `FileResponse` does not:
        it computes an ETag and sends it, and then ignores the `If-None-Match`
        the browser sends back, so every load re-downloaded the whole page.
        Measured against the deployed broker: a request carrying the exact ETag
        the server had just issued came back `200` with all 203,794 bytes. With
        this, the same request is a `304` with no body.

        `no-cache` rather than a max-age, deliberately: the page must never be
        served stale from a cache after a deploy -- it is the thing that talks to
        this broker's API. `no-cache` means "revalidate every time", which is the
        round trip above, and the 304 makes that round trip carry no payload."""
        path = _STATIC_DIR / "dashboard.html"
        stat = path.stat()
        # Strong enough for a file served off disk, and cheap: size plus mtime in
        # nanoseconds changes on every deploy that changes the file.
        etag = f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if _if_none_match(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return FileResponse(path, headers=headers)


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
            # So the dashboard's role pickers offer exactly what this broker
            # accepts. Restating the list in the page would let the two drift,
            # and the drift shows up as a 400 at the moment someone is trying to
            # add a colleague. The names are public -- they are in the docs and
            # in every auth response -- so this leaks nothing.
            "roles": list(db.ROLES),
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
                              _hash_password(body.password), role="admin")
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
    # Higher than the per-account limit because one address is legitimately many
    # people behind a TLS-terminating proxy or a university NAT, but still a hard
    # ceiling on how much scrypt an anonymous caller can buy.
    LOGIN_FAIL_ADDR_LIMIT = 25
    # The username is attacker-chosen and need not exist, so without a cap an
    # anonymous caller can grow this map indefinitely. Well above any real
    # deployment's account count, and evicting the stalest entry is correct
    # behaviour rather than a mere safeguard: the stalest is also the one whose
    # window is most likely to have expired anyway.
    LOGIN_FAIL_MAX_KEYS = 4096

    def _client_addr(request: Request) -> str:
        return request.client.host if request.client else "?"

    def _throttle_key(request: Request, username: str) -> str:
        # The SOCKET address deliberately, not X-Forwarded-For -- unlike the
        # Secure-cookie decision, which reads X-Forwarded-Proto. A forwarded
        # header's leftmost value is supplied by the caller, so keying on it
        # would let an attacker rotate it and evade the throttle entirely,
        # which is worse than the cost of not using it: behind a
        # TLS-terminating proxy every caller shares one apparent address, so
        # one attacker can throttle the others for that username.
        return f"{username}|{_client_addr(request)}"

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
        # TWO buckets, and the address one is the load-bearing half. Keying only
        # on `username|address` bounded nothing an attacker cares about: the
        # username is attacker-chosen and need not exist, so every attempt with a
        # fresh username opened a fresh bucket and the limit never applied. Each
        # of those attempts is a full scrypt -- ~100 ms and ~16 MB by design --
        # from an endpoint that needs no credential to reach.
        addr_key = f"|addr|{_client_addr(request)}"
        with _login_lock:
            for k, limit in ((key, LOGIN_FAIL_LIMIT),
                             (addr_key, LOGIN_FAIL_ADDR_LIMIT)):
                recent = [t for t in _login_failures.get(k, [])
                          if now - t < LOGIN_FAIL_WINDOW]
                if len(recent) >= limit:
                    _login_failures[k] = recent
                    raise HTTPException(
                        429, "too many failed logins from this address; "
                             "wait a few minutes")
                _login_failures[k] = recent + [now]
            if len(_login_failures) > LOGIN_FAIL_MAX_KEYS:
                stalest = min(_login_failures, key=lambda k: _login_failures[k][-1])
                _login_failures.pop(stalest, None)
        user = db.get_user(conn, body.username)
        # Verify even when the user does not exist, against a decoy hash, so a
        # wrong USERNAME and a wrong PASSWORD take the same time. Otherwise the
        # response time enumerates accounts. The decoy is precomputed at import
        # (auth.DECOY_HASH) rather than built here: building it is itself a full
        # scrypt, which made a miss cost exactly twice a hit.
        stored = user["password_hash"] if user else auth.DECOY_HASH
        # Bounded, because scrypt is ~16 MB a call and the threadpool is 40 wide:
        # unbounded, 40 simultaneous logins ask for ~640 MB on a 512 MB instance
        # that already sits at ~280 MB once the geo libraries are resident. The
        # queue costs a slow login under load; the alternative is the platform
        # killing the process, which is what actually happened to this service.
        ok = _verify_password(body.password, stored)
        if not ok or not user:
            # The reservation above stands as the failure record.
            raise HTTPException(401, "wrong username or password")
        with _login_lock:
            _login_failures.pop(key, None)      # success releases the whole run
            _login_failures.pop(addr_key, None)
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

    # -- pairing: a machine asks, an admin approves in the browser ---------------
    #
    # What `E3D --setup-simulation-node` talks to. The old enrolment needed an
    # admin to type their PASSWORD on every simulation node, which is the wrong
    # place for it: those are shared cluster logins and lab boxes. Here the node
    # shows a short code and opens the dashboard; whoever is already signed in as
    # an admin confirms the code matches and clicks Approve. Nothing secret is
    # ever typed on the node.
    #
    # The node generates its own token and sends only the hash (see the pairings
    # table in db.py), so approval is a row insert and there is no moment at
    # which the broker holds a readable credential.
    _PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/I
    _pair_starts: dict[str, list[float]] = {}
    PAIR_START_WINDOW = 600.0
    PAIR_START_LIMIT = 10

    def _norm_code(code: str) -> str:
        return "".join(ch for ch in code.upper() if ch.isalnum())

    def _show_code(code: str) -> str:
        return code[:4] + "-" + code[4:]

    @app.post("/v1/pair/start")
    def pair_start(body: PairStartIn, request: Request) -> dict[str, Any]:
        """Ask to join. Unauthenticated by necessity -- the machine has nothing to
        authenticate with yet -- so it is throttled per address and the queue is
        bounded, and the only thing it can cause is a card in an admin's browser.
        """
        addr = _client_addr(request)
        now_m = time.monotonic()
        with _login_lock:
            recent = [t for t in _pair_starts.get(addr, []) if now_m - t < PAIR_START_WINDOW]
            if len(recent) >= PAIR_START_LIMIT:
                _pair_starts[addr] = recent
                raise HTTPException(429, "too many pairing requests from this address; "
                                         "wait a few minutes")
            _pair_starts[addr] = recent + [now_m]
            if len(_pair_starts) > 4096:
                _pair_starts.pop(min(_pair_starts, key=lambda k: _pair_starts[k][-1]), None)

        last: Exception | None = None
        for _ in range(5):                  # a user_code collision is a retry, not an error
            code = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(8))
            try:
                made = db.create_pairing(conn, code, body.name, body.token_hash,
                                         host=body.host, platform=body.platform,
                                         requested_ip=addr)
                break
            except ValueError as e:
                reason = str(e)
                if reason == "name-in-use":
                    raise HTTPException(
                        409, f"{body.name!r} already has a live credential. Revoke it in "
                             "the dashboard (Settings > Machines) first, or pick another "
                             "name with --name.") from e
                if reason == "token-in-use":
                    raise HTTPException(409, "that token is already registered; "
                                             "generate a new one") from e
                raise HTTPException(429, "too many machines are waiting for approval; "
                                         "ask an admin to clear the queue") from e
            except Exception as e:          # noqa: BLE001 -- UNIQUE(user_code)
                last = e
        else:
            raise HTTPException(503, "could not allocate a pairing code") from last

        scheme = "https" if _is_https(request) else "http"
        host = request.headers.get("host") or request.url.netloc
        return {"user_code": _show_code(code),
                "verification_url": f"{scheme}://{host}/?pair={_show_code(code)}",
                "expires_in": made["expires_at"] - int(time.time()),
                "interval": 3}

    @app.post("/v1/pair/poll")
    def pair_poll(body: PairPollIn, request: Request) -> dict[str, Any]:
        """Has it been approved yet? Proven by presenting the token itself: only
        the machine that started this pairing can produce a token with this hash.
        An unknown code and a wrong token get the same 404, so the endpoint cannot
        be used to find out which codes are live.
        """
        supplied = _supplied_token(request)
        row = db.get_pairing(conn, _norm_code(body.user_code))
        if (not supplied or row is None
                or not _ct_eq(auth.hash_token(supplied), row["token_hash"])):
            raise HTTPException(404, "no such pairing")
        out: dict[str, Any] = {"status": row["status"], "name": row["name"]}
        if row["status"] == "pending":
            out["expires_in"] = max(0, row["expires_at"] - int(time.time()))
        return out

    @app.get("/v1/pair/pending")
    def pair_pending(user=AdminAuth) -> dict[str, Any]:
        rows = db.list_pending_pairings(conn)
        for r in rows:
            r["user_code"] = _show_code(r["user_code"])
        return {"pending": rows}

    def _resolve(user_code: str, approve: bool, user) -> dict[str, Any]:
        status = db.resolve_pairing(conn, _norm_code(user_code), approve, user["username"])
        if status == "missing":
            raise HTTPException(404, "no such pairing request")
        if status == "expired":
            raise HTTPException(410, "that request expired; run the setup on the machine again")
        if status == "conflict":
            raise HTTPException(409, "that name or token was claimed by another credential "
                                     "in the meantime; run the setup on the machine again")
        return {"status": status}

    @app.post("/v1/pair/{user_code}/approve")
    def pair_approve(user_code: str, user=AdminAuth) -> dict[str, Any]:
        return _resolve(user_code, True, user)

    @app.post("/v1/pair/{user_code}/deny")
    def pair_deny(user_code: str, user=AdminAuth) -> dict[str, Any]:
        return _resolve(user_code, False, user)

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
            # UNIQUE(name), and only for a LIVE credential: re-issuing over one
            # would silently strand whichever token the box is actually using.
            # A revoked one is reclaimed instead (see db.create_worker_token),
            # so "revoke it first" is now advice that works rather than the
            # dead end it used to be.
            raise HTTPException(409, f"{body.name!r} already has a live credential. "
                                     "Revoke it first, then issue a new one -- that "
                                     "reclaims the name.")
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
        """Add an account. Admin-only, and never a way to escalate: the caller
        is already an admin, so it grants nothing it does not itself hold.

        Defaults to `viewer` rather than `operator`: the least privilege that is
        still useful is the right default for the endpoint a script calls, and
        the caller has to say `operator` or `admin` on purpose.
        """
        if body.role not in db.ROLES:
            raise HTTPException(400, "role must be one of %s" % ", ".join(db.ROLES))
        try:
            created = db.create_user(conn, body.username,
                                     _hash_password(body.password), role=body.role)
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
            if not body.current_password or not _verify_password(
                    body.current_password, target["password_hash"]):
                raise HTTPException(403, "current password is wrong")
        elif caller["role"] != "admin":
            raise HTTPException(403, "only an admin can reset another account's password")
        db.set_password(conn, username, _hash_password(body.new_password))
        if caller["username"] == username:
            # set_password revoked every session this account held, this one
            # included. Re-issue so changing your own password does not log you
            # out of the tab you changed it in.
            _issue_session(request, response, caller["id"])
        return {"username": username, "password_changed": True,
                "sessions_revoked": True}


    @app.post("/v1/cases", dependencies=[WriteAuth])
    def add_cases(cases: list[CaseIn]) -> dict[str, Any]:
        """Append cases to the campaign. Safe to re-run: existing ids are skipped,
        so growing 5k -> 15k is 'post the new list' and nothing else.

        **Sites that are not on land are dropped here**, and reported back rather
        than refused. The sampler's LCZ raster reads snow, ice and open water as
        built classes, and its purity test cannot catch that -- a uniformly
        misread ice sheet is 100% "pure" -- so the draw has produced sites in
        Antarctica and in the open ocean. Its polar gate handles the poles by
        latitude, which by construction cannot catch 7.5N 37.5W in the middle of
        the Atlantic.

        Dropped rather than rejecting the batch, because a 5,000-case draw with
        twenty bad sites in it should still land the other 4,980, and the caller
        is told exactly what went and why. The check is coarse on purpose; see
        :func:`footprints.on_land`.
        """
        if len(cases) > 5000:
            raise HTTPException(413, "post at most 5000 cases per request")
        rows, rejected = [], []
        for c in cases:
            if not footprints.on_land(c.lat, c.lon):
                rejected.append({"lat": c.lat, "lon": c.lon,
                                 "city_cluster": c.city_cluster, "lcz": c.lcz,
                                 "tile": footprints.gba_tile_for(c.lat, c.lon)
                                 if -90.0 <= c.lat <= 90.0 and -180.0 <= c.lon <= 180.0
                                 else None})
                continue
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
        out: dict[str, Any] = dict(db.add_cases(conn, rows))
        # Always present, so a caller can read it without a version check, and
        # a draw that produced none can say so rather than staying silent.
        out["rejected_not_on_land"] = len(rejected)
        if rejected:
            # A sample, not the lot: twenty bad sites are a bug in the draw and
            # five of them show it, while 5,000 would be the response body.
            out["rejected_examples"] = rejected[:5]
        return out


    @app.post("/v1/cases/land-audit", dependencies=[WriteAuth])
    def land_audit(dry_run: bool = True, limit: int = 50) -> dict[str, Any]:
        """Find cases already in the campaign that are not on land, and park them.

        The gate on `POST /v1/cases` protects only what was added after it
        existed. The published campaign predates it -- AGENTS.md has carried
        "production holds the ungated draw, including sites in Antarctica and one
        in the open Pacific" as a known problem -- and each of those is 66
        core-hours aimed at an empty flat plane.

        They are quarantined, not deleted: nothing leases a quarantined case, the
        row and its event trail stay auditable, and the decision is reversible.
        Deleting them would also quietly shrink the campaign's own record of what
        its sampler produced, which is the thing worth keeping.

        `dry_run` defaults to TRUE, as it does for purge. Finding out how bad it
        is must not be the same keystroke as changing production.
        """
        return db.quarantine_not_on_land(conn, footprints.on_land,
                                         dry_run=dry_run, limit=limit)

    @app.post("/v1/cases/reopen", dependencies=[WriteAuth])
    def reopen_cases(error_contains: str | None = None, dry_run: bool = True,
                     limit: int = 50,
                     case_id: list[str] | None = Query(None)) -> dict[str, Any]:
        """Put quarantined cases back in the pool, attempts refunded.

        Quarantine means "this site is broken everywhere". It also collects cases
        that merely met three machines that could not run anything: a stopped
        container daemon charged an attempt per lease, and three of those
        quarantine a site with nothing wrong with it. `runner/run_case.sh` has
        carried the warning since the first ICE run -- a wrongly fatal error
        "silently removes a site from the campaign with no way back short of
        editing the database" -- and this is that way back.

        `error_contains` matches the case's LAST failure text, which is what makes
        this "undo what that one broken node did" rather than "reopen everything".

        `dry_run` defaults to TRUE, as it does for the land audit and for purge.

        `limit` bounds how many cases are REOPENED, not merely how many are shown
        back. The reply carries `matched` and `capped` so a larger backlog is
        visible; call again to take the next batch.

        `case_id` (repeatable) names cases outright. The database layer always
        took a list; the endpoint never passed one through, so an operator looking
        at ONE wrongly quarantined case had only a substring match over everybody's
        errors to reach it with. Combines with `error_contains` as an AND.
        """
        return db.reopen_cases(conn, error_contains=error_contains, case_ids=case_id,
                               dry_run=dry_run, limit=limit)

    @app.delete("/v1/cases", dependencies=[PurgeAuth])
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
        # 426, not 403: the credential is fine and the BUILD is not. A node that
        # sees it knows to wait for its update rather than to re-pair.
        refused = db.lease_refusal(conn, body.build)
        if refused:
            raise HTTPException(426, refused)
        got = db.lease(conn, body.worker_id, count=body.count,
                       lease_seconds=body.lease_seconds, splits=body.splits,
                       host=body.host, cluster=body.cluster,
                       resume_case_ids=body.resume_case_ids,
                       build=body.build, version=body.version,
                       platform=body.platform, recipes=body.recipes)
        return [LeaseOut(case_id=g.case_id, lease_id=g.lease_id, expires_at=g.expires_at,
                         attempt=g.attempt, spec=g.spec) for g in got]


    # -- node releases -----------------------------------------------------
    #
    # The broker says WHICH build a node should run and what its file must hash
    # to. It never serves the file: nodes take it from their own release share
    # (Syncthing, next to the folder their archives already travel through) and
    # verify it against the hash given here, over the one channel that is
    # already authenticated per machine.

    @app.get("/v1/node/release", dependencies=[WriteAuth])
    def node_release(request: Request, worker_id: str, platform: str | None = None,
                     build: str | None = None, failed_build: str | None = None,
                     failed_reason: str | None = None) -> dict[str, Any]:
        """What this node should be running. Asked before every lease, and
        during a solve so an update does not have to wait for the case.

        `failed_build` is a node saying it TRIED a build, could not start it and
        rolled back: the one thing an unattended update must never hide."""
        machine = _machine_principal(request)
        if machine and not _may_lease_as(machine["name"], worker_id):
            raise HTTPException(403, f"this credential belongs to {machine['name']!r}")
        return db.node_release(conn, worker_id, platform, build,
                               failed_build=(failed_build or "")[:96] or None,
                               failed_reason=failed_reason)

    @app.get("/v1/releases", dependencies=[ReadAuth])
    def releases() -> dict[str, Any]:
        return db.list_releases(conn)

    @app.post("/v1/releases")
    def register_release(body: ReleaseIn_, user=AdminAuth) -> dict[str, Any]:
        """Record a published build. Admin only, like everything below: pointing
        a fleet at a build is remote code execution by design, so who may do it
        is the whole security model."""
        db.register_release(conn, body.build, body.platform, body.file, body.sha256,
                            body.notes, by=user["username"])
        return {"build": body.build, "platform": body.platform}

    @app.delete("/v1/releases/{build}")
    def delete_release(build: str, user=AdminAuth) -> dict[str, Any]:
        current = db.list_releases(conn)
        if current["target_build"] == build:
            raise HTTPException(409, f"{build} is the fleet's target; move the target first")
        return {"removed": db.delete_release(conn, build, by=user["username"])}

    @app.put("/v1/releases/target")
    def set_release_target(body: TargetIn, user=AdminAuth) -> dict[str, Any]:
        """Point the whole fleet at a build (or, with build=null, at nothing).

        Refused for a build with no published file: every node would learn it
        should move and none of them could."""
        if body.apply is not None:
            if body.apply not in db.APPLY_MODES:
                raise HTTPException(422, f"apply must be one of {', '.join(db.APPLY_MODES)}")
            db.set_setting(conn, "target_apply", body.apply, by=user["username"])
        if "build" in body.model_fields_set:
            if body.build and not any(r["build"] == body.build for r in db.list_releases(conn)["releases"]):
                raise HTTPException(409, f"{body.build} has no published file; register it first")
            db.set_setting(conn, "target_build", body.build, by=user["username"])
        return db.list_releases(conn)

    # -- push notifications (ntfy) ----------------------------------------
    # Admin only: the topic URL is a secret (on ntfy.sh, anyone who knows it can
    # read every notice), and pointing it elsewhere redirects the fleet's news.
    def _notify_view() -> dict[str, Any]:
        cfg = notify.resolve(db.get_settings(conn))
        return {"enabled": bool(cfg["url"]), "url": notify.mask(cfg["url"]),
                "token_set": bool(cfg["token"]), "events": cfg["events"],
                "all_events": list(notify.ALL_EVENTS), "public_url": cfg["public_url"],
                "source": cfg["source"]}

    @app.get("/v1/notify")
    def get_notify(user=AdminAuth) -> dict[str, Any]:
        return _notify_view()

    @app.put("/v1/notify")
    def set_notify(body: NotifyIn, user=AdminAuth) -> dict[str, Any]:
        """Only the fields SENT change. An empty string clears that field in the
        settings table, which hands it back to its environment variable."""
        by = user["username"]
        current = db.get_settings(conn)
        sent = {f: (getattr(body, f) or "").strip() or None
                for f in ("url", "token", "public_url") if f in body.model_fields_set}
        if sent.get("url"):
            token_after = sent["token"] if "token" in sent else current.get(notify.KEYS["token"])
            problem = notify.check_url(sent["url"], from_dashboard=True, with_token=bool(token_after))
            if problem:
                raise HTTPException(422, f"url {problem}")
        if sent.get("public_url"):
            problem = notify.check_url(sent["public_url"], from_dashboard=False)
            if problem:
                raise HTTPException(422, f"public_url {problem}")
        # A token belongs to the topic it was issued for. Pointing the topic at a
        # different host keeps no token that was not re-entered along with it,
        # or the old secret would be sent to wherever the new URL points.
        old_url = current.get(notify.KEYS["url"])
        if ("url" in sent and "token" not in sent and current.get(notify.KEYS["token"])
                and notify._origin(sent["url"]) != notify._origin(old_url)):
            sent["token"] = None
        for field, value in sent.items():
            db.set_setting(conn, notify.KEYS[field], value, by=by)
        if body.events is not None:
            bad = [e for e in body.events if e not in notify.ALL_EVENTS]
            if bad:
                raise HTTPException(422, f"unknown event(s): {', '.join(bad)}")
            db.set_setting(conn, notify.KEYS["events"], json.dumps(sorted(set(body.events))), by=by)
        return _notify_view()

    @app.post("/v1/notify/test")
    def test_notify(user=AdminAuth) -> dict[str, Any]:
        return notify.send_test(db.get_settings(conn))

    @app.put("/v1/releases/policy")
    def set_release_policy(body: PolicyIn, user=AdminAuth) -> dict[str, Any]:
        if body.require_build is not None:
            db.set_setting(conn, "require_build", "1" if body.require_build else "0", by=user["username"])
        if body.blocked_builds is not None:
            db.set_setting(conn, "blocked_builds", json.dumps(sorted(set(body.blocked_builds))),
                           by=user["username"])
        return db.list_releases(conn)

    @app.put("/v1/workers/{worker_id}/target")
    def set_worker_target(worker_id: str, body: TargetIn, user=AdminAuth) -> dict[str, Any]:
        """The canary: move ONE worker to a build, ahead of the fleet."""
        if body.build and not any(r["build"] == body.build for r in db.list_releases(conn)["releases"]):
            raise HTTPException(409, f"{body.build} has no published file; register it first")
        if not db.set_worker_target(conn, worker_id, body.build, by=user["username"]):
            raise HTTPException(404, f"no worker named {worker_id!r}")
        return {"worker_id": worker_id, "target_build": body.build}

    @app.post("/v1/workers/{worker_id}/drain")
    def drain_worker(worker_id: str, body: DrainIn, user=AdminAuth) -> dict[str, Any]:
        if not db.set_worker_drain(conn, worker_id, True, body.reason, by=user["username"]):
            raise HTTPException(404, f"no worker named {worker_id!r}")
        return {"worker_id": worker_id, "drain": True}

    @app.post("/v1/workers/{worker_id}/undrain")
    def undrain_worker(worker_id: str, user=AdminAuth) -> dict[str, Any]:
        if not db.set_worker_drain(conn, worker_id, False, by=user["username"]):
            raise HTTPException(404, f"no worker named {worker_id!r}")
        return {"worker_id": worker_id, "drain": False}

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
                           body.bytes, body.metrics, case_id=body.case_id):
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


    @app.post("/v1/telemetry", dependencies=[WriteAuth])
    def telemetry(body: TelemetryIn, request: Request) -> dict[str, bool]:
        """Structured telemetry from the node: the latest `data` for one `kind`
        of one case, replacing that kind only. See db.post_telemetry.

        Never 404 from here, whatever is wrong: a node reads 404 as "this broker
        predates telemetry" and stops sending it for the rest of its life, so a
        stale lease or an unknown case is 409 -- stop for THIS case -- exactly
        as a heartbeat would answer it."""
        # Checked before db._LOCK is taken: a body that is too large or too
        # deep is refused without holding up anyone's heartbeat.
        outcome, _ = db.prepare_telemetry(body.kind, body.data)
        if outcome == "ok":
            # A per-machine credential may report only on a lease held under
            # its own name, as /v1/lease lets it claim only under its own name.
            # lease_id is no secret -- the case list shows it to any reader --
            # so without this, one machine's credential could post invented
            # mesh numbers into another machine's case, stamped with the OTHER
            # machine's name, and into the dataset's statistics. 409, never
            # 403 or 404: to the node it means "stop for this case", which is
            # the only right reaction to a lease it does not hold.
            machine = _machine_principal(request)
            worker_ok = None
            if machine:
                name = machine["name"]
                worker_ok = lambda worker: bool(worker) and _may_lease_as(name, worker)  # noqa: E731
            outcome = db.post_telemetry(conn, body.lease_id, body.case_id, body.kind,
                                        body.data, worker_ok=worker_ok)
        if outcome == "ok":
            return {"ok": True}
        if outcome == "gone":
            raise HTTPException(409, "lease expired or superseded, or not this case's, or "
                                     "not this credential's; stop sending telemetry for "
                                     "this case")
        if outcome == "too_many_kinds":
            raise HTTPException(413, f"a case holds at most {db.TELEMETRY_MAX_KINDS} "
                                     "telemetry kinds")
        if outcome == "too_large":
            raise HTTPException(413, f"telemetry data is limited to "
                                     f"{db.TELEMETRY_MAX_BYTES} bytes of JSON per post "
                                     "(compact, non-ASCII escaped as \\uXXXX)")
        if outcome == "too_deep":
            raise HTTPException(422, f"telemetry data may nest objects and arrays at most "
                                     f"{db.TELEMETRY_MAX_DEPTH} levels deep")
        raise HTTPException(422, "kind must match ^[a-z][a-z0-9_]{0,31}$ and data must be an object")


    @app.get("/v1/dataset", dependencies=[ReadAuth])
    def dataset_stats() -> dict[str, Any]:
        """The campaign as a dataset: counts by state, split, LCZ, recipe and
        country, and per metric of casebroker.dataset.METRICS its distribution
        over every case and per LCZ -- all on one set of histogram edges per
        metric, so the reference sets can be drawn on one axis. Computed from
        every case and cached for 60 s (casebroker/dataset.py)."""
        try:
            return dataset_cache.get().public
        except dataset.Unavailable as exc:
            # The last computation failed and there is no earlier one to serve.
            # Remembered for the TTL, so this is one campaign read per minute,
            # not one per request, until whatever broke it is fixed.
            raise HTTPException(503, str(exc),
                                headers={"Retry-After": str(int(dataset.TTL_SECONDS))}) from exc


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

    @app.get("/v1/storage", dependencies=[ReadAuth])
    def storage() -> dict[str, Any]:
        """How much space the database uses, per table, against the plan's limit
        when one is known -- what the dashboard's Storage page shows.

        The limit is not something the database can report about itself.
        ``CASEBROKER_DB_QUOTA_MB`` sets it; unset, a Supabase DSN is assumed to be
        on the free plan's 500 MB and the answer SAYS it is an assumption, and
        anything else reports no limit rather than invent one."""
        out = db.storage(conn)
        raw = os.environ.get("CASEBROKER_DB_QUOTA_MB", "").strip()
        if raw:
            try:
                out["quota_bytes"] = int(float(raw) * 1024 * 1024)
                out["quota_source"] = "CASEBROKER_DB_QUOTA_MB"
            except ValueError:
                out["quota_bytes"], out["quota_source"] = None, f"CASEBROKER_DB_QUOTA_MB is not a number: {raw!r}"
        elif _is_postgres_dsn(db_path) and "supabase" in db_path:
            out["quota_bytes"] = 500 * 1024 * 1024
            out["quota_source"] = "assumed: Supabase free plan (set CASEBROKER_DB_QUOTA_MB to override)"
        else:
            out["quota_bytes"], out["quota_source"] = None, None
        return out

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


    # One building query per case at a time. The dashboard rebuilds its geometry
    # panel on every refresh and a second viewer can open the same case, and each
    # used to start its own read of the same remote bytes while the first was
    # still under way. Now the rest wait for it, then answer from the cache it
    # wrote. Weak values: a case's lock lives only while a request holds it.
    footprints_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
    footprints_locks_guard = threading.Lock()

    def _drawn_from(mesh: str) -> str:
        """The source a case whose mesh is ``mesh`` is actually drawn from.

        The mesh's own source, except that Overture is an optional extra: it
        costs a second interpreter loading pyarrow inside a memory-capped web
        process, so it runs only where CASEBROKER_OVERTURE_FALLBACK says it may.
        A case meshed from Overture on an instance that will not read Overture is
        drawn from GBA and labelled -- a substitution that says so, rather than a
        silent one.

        One function because the cache compares against it and the query follows
        it. If those two ever disagreed, a case would miss its own cache and
        re-query the atlas on every inspector open.
        """
        if mesh == footprints.OVERTURE and not footprints.OVERTURE_FALLBACK:
            return footprints.GBA
        return mesh

    def _building_query(lat: float, lon: float,
                        source: str) -> tuple[dict[str, Any], bool]:
        """The three layers for one site, the buildings from the source this
        case's mesh was built from.

        Returns the payload and whether any of it is a fact about TODAY rather
        than about the site, which is what decides whether it may be cached.

        The layers are fetched INDEPENDENTLY. They used to share one try/except
        that raised 502 on any failure, so a site the building atlas simply does
        not cover took the terrain and the canopy down with it and the panel
        showed an error instead of the answer -- when "no buildings, no land, no
        trees" was itself the answer, and the most useful one this endpoint can
        give: that case is in the ocean.

        The layers run in sequence, not side by side. Together they are the
        slower arm plus the other two rather than the slowest alone, which is
        real -- GBA measured 2.1-8.7 s and GEDTM30 0.05-10.6 s on 2026-09-15 --
        but the ceilings around them are per call and not per process, so two
        layers in flight are two budgets live at once inside one capped web
        process. That is the pressure the preview slot exists to hold down.
        """
        transient = False
        empty = {"type": "FeatureCollection", "features": [], "n": 0,
                 "release": "GBA.LoD1", "source": footprints.GBA,
                 "height_kind": "predicted", "centre": [lat, lon],
                 "half_m": footprints.HALF_M}

        # Looked up on each call rather than bound once, so a test can replace
        # them.
        def gba() -> dict[str, Any]:
            return {**footprints.fetch_gba(lat, lon), "source": footprints.GBA}

        def overture() -> dict[str, Any]:
            return {**footprints.fetch(lat, lon), "source": footprints.OVERTURE}

        def attempt(fn):
            """``(payload, error)``, where a missing tile is a payload.

            GBA publishes 922 tiles of a possible 2,592 and the rest are ocean
            and ice, so an empty answer there is a fact about the site, not a
            failure, and is cached like any other.
            """
            try:
                return fn(), None
            except footprints.TileNotPublished as gap:
                return {**empty, "tile_published": False, "tile": str(gap)}, None
            except Exception as err:                     # noqa: BLE001
                return None, err

        # The mesh's own source first, because that is what this must draw. The
        # other stays as a fallback rather than nothing: it is one HTTP
        # dependency against another, and an inspector that 502s is useless
        # exactly when someone is trying to find out why a case looks wrong.
        # `fallback_from` then says the picture is not the mesh's, and why.
        drawn = _drawn_from(source)
        first, other = ((overture, gba) if drawn == footprints.OVERTURE
                        else (gba, overture if footprints.OVERTURE_FALLBACK
                              else None))
        fc, err = attempt(first)
        if fc is not None and drawn != source:
            fc["fallback_from"] = (
                "overture not enabled here: install casebroker[overture] and "
                "set CASEBROKER_OVERTURE_FALLBACK to draw this case from the "
                "source it was meshed from")
        elif fc is None and other is not None:
            fc, later = attempt(other)
            if fc is None:
                err = later
            else:
                fc["fallback_from"] = f"{drawn} unavailable: {str(err)[:120]}"
        if fc is None:
            # A reachability failure, unlike a missing tile, says nothing about
            # the site -- so it is reported and NOT cached, and the other two
            # layers still get drawn.
            transient = True
            fc = {**empty, "buildings_error": str(err)[:200]}

        # What the site is made of BESIDES buildings. Cached with the footprints
        # because they answer the same question -- "what will this case actually
        # be" -- and because finding out after 66 core-hours is worse than
        # finding out now. Neither raises: a dead raster host is a fact about
        # today, not about the site.
        fc["terrain"] = footprints.terrain(lat, lon)
        fc["canopy"] = footprints.canopy(lat, lon)
        # "unavailable" and "unknown" are facts about TODAY -- a raster host that
        # did not answer, a library that is not there -- and caching them freezes
        # one bad moment into "this site has no terrain and no trees" for the
        # life of the case. Only the buildings failure used to set `transient`,
        # so a canopy read that timed out once was served as a treeless site
        # forever. "flat" and "none" are facts about the SITE and stay cacheable.
        for layer in (fc["terrain"], fc["canopy"]):
            if layer.get("source") in ("unavailable", "unknown"):
                transient = True
        return fc, transient

    @app.get("/v1/cases/{case_id}/footprints", dependencies=[ReadAuth])
    def case_footprints(case_id: str, refresh: bool = False) -> dict[str, Any]:
        """Everything this case will be meshed from: buildings, terrain, trees.

        GeoJSON footprints with predicted heights, plus a GEDTM30 relief grid
        and a Meta/WRI canopy-height grid over the mesh domain. The dashboard
        cannot fetch any of it itself -- all three are GeoParquet or
        Cloud-Optimized GeoTIFF on object storage, with no REST API and no tile
        endpoint a browser could call -- so the broker runs the queries.

        The buildings come from the source THIS case's mesh was built from,
        which :func:`footprints.mesh_source` decides: what the case's run
        reported, Overture for a case that finished before the builder could
        mesh GBA, and GBA otherwise. The response carries it as `mesh_source`,
        with `mesh_source_basis` saying how it is known; `source` is what
        actually answered, and `fallback_from` says why the two differ when
        they do.

        It is deliberately the SAME sources and the same bbox derivation the
        runner uses, so the picture is the geometry that gets meshed. Drawing
        OSM footprints, a basemap tile, or the OTHER building source would be
        worse than drawing nothing: it would look like a check while disagreeing
        with the mesh, and it would disagree most exactly where checking matters.

        Cached after the first fetch; `refresh=true` forces a re-query, and so
        does a cached row written by an older build or drawn from a source this
        case would not be drawn from now.
        """
        row = db.get_case(conn, case_id)
        if row is None:
            raise HTTPException(404, "no such case")
        row = dict(row)
        metrics = row.get("metrics") or {}
        if isinstance(metrics, str):
            metrics = json.loads(metrics)
        source, basis = footprints.mesh_source(row["state"], metrics,
                                               row.get("updated_at"))
        meshed = {"mesh_source": source, "mesh_source_basis": basis}

        def cached() -> dict[str, Any] | None:
            if refresh:
                return None
            hit = db.get_footprints(conn, case_id)
            if not hit:
                return None
            fc = json.loads(hit["geojson"])
            # A payload written by an older build is a miss, not a hit. The
            # cache is keyed on case_id alone, so without this check the first
            # version of this endpoint answers forever -- which is how adding
            # terrain and canopy produced a fleet of cases that reported no
            # trees anywhere rather than re-querying once.
            if fc.get("payload_v") != footprints.PAYLOAD_VERSION:
                return None
            # Nor is a row drawn from a source this case would not be drawn from
            # now a hit: the pre-switch Overture row of a case GBA will mesh, or
            # a fallback taken while the real source was down. Served from the
            # cache it would stay the wrong picture for good. Rows cached before
            # the switch carry no `source` at all: Overture.
            if (fc.get("source") or footprints.OVERTURE) != _drawn_from(source):
                return None
            return {**fc, **meshed, "cached": True,
                    "fetched_at": hit["fetched_at"]}

        if (hit := cached()) is not None:
            return hit
        spec = row.get("spec") or {}
        if isinstance(spec, str):
            spec = json.loads(spec)
        lat, lon = spec.get("lat"), spec.get("lon")
        if lat is None or lon is None:
            raise HTTPException(422, "case spec carries no lat/lon")
        lat, lon = float(lat), float(lon)
        with footprints_locks_guard:
            lock = footprints_locks.setdefault(case_id, threading.Lock())
        with lock:
            # Whoever held the lock before us may have just answered this case.
            if (hit := cached()) is not None:
                return hit
            # ONE preview at a time across the process, because the memory
            # ceilings are per call and not per process: every layer builds its
            # own DuckDB with its own budget and its own GDAL cache, and this
            # endpoint is sync, so it holds a threadpool slot for the 8-15
            # seconds the remote reads take while the next caller starts its own
            # everything. Two at once already asks for more than the instance
            # has. 503 with Retry-After is the honest answer when the queue is
            # full; the alternative is the platform killing the process, which is
            # how this branch started.
            try:
                with footprints.exclusive():
                    fc, transient = _building_query(lat, lon, source)
            except footprints.GeoBusy as busy:
                raise HTTPException(503, str(busy),
                                    headers={"Retry-After": "30"}) from busy
            fc["payload_v"] = footprints.PAYLOAD_VERSION
            if not transient:
                db.put_footprints(conn, case_id, json.dumps(fc), fc["n"])
        return {**fc, **meshed, "cached": False}

    @app.get("/v1/cases/{case_id}", dependencies=[ReadAuth])
    def get_case(case_id: str) -> dict[str, Any]:
        row = db.get_case(conn, case_id)
        if row is None:
            raise HTTPException(404, "no such case")
        # Where it is -- country, nearest town and how far -- answered offline
        # (casebroker/places.py). Computed on read, so every case has it,
        # including the ones posted before this existed. Never fatal: a case
        # whose spec has no usable coordinates simply has no place.
        try:
            spec = json.loads(row.get("spec") or "{}") if isinstance(row.get("spec"), str) else (row.get("spec") or {})
            row["place"] = places.locate(float(spec["lat"]), float(spec["lon"]))
        except (KeyError, TypeError, ValueError):
            row["place"] = None
        # Parsed, unlike `metrics` and `spec` (which this endpoint has always
        # returned as the TEXT they are stored as, and the dashboard parses):
        # telemetry is new, so there is no reader to keep compatible, and {} is
        # "nothing reported" without a null check.
        row["telemetry"] = dataset.parse_obj(row.get("telemetry"))
        if db.nests_deeper(row["telemetry"], db.TELEMETRY_MAX_DEPTH + 1):
            # Deeper than post_telemetry now accepts (+1 for the kind level):
            # a row written before that bound. pydantic-core cannot render it,
            # and a record that answers 500 is worse than one without telemetry.
            print(f"[telemetry] {case_id}: stored telemetry nests too deep to serve",
                  file=sys.stderr)
            row["telemetry"] = {}
        # Where this case sits in the campaign, from the same cached aggregate
        # /v1/dataset serves -- but never WAITING for it: the record does not
        # depend on the aggregate, and a cold one is seconds of work at campaign
        # scale. peek() serves what there is, even a minute stale, and refreshes
        # it in the background; {} until the first one after a start is ready.
        # Never fatal, like `place`: a ranking that cannot be had is absent.
        try:
            agg = dataset_cache.peek()
            row["percentiles"] = agg.percentiles(row) if agg is not None else {}
        except Exception as exc:                     # noqa: BLE001
            print(f"[dataset] percentiles for {case_id} failed: {exc!r}", file=sys.stderr)
            row["percentiles"] = {}
        return row


    @app.get("/v1/errors", dependencies=[ReadAuth])
    def list_errors(limit: int = 2000) -> dict[str, Any]:
        """Every case carrying an error, in full, with each failed attempt and the
        machine it failed on -- what the dashboard's "Copy all errors" pastes into
        a bug report. Read scope: an error message is a solver log tail, and
        nothing here is more than the case list already shows."""
        return db.list_errors(conn, limit=limit)

    @app.get("/v1/cases", dependencies=[ReadAuth])
    def list_cases(state: str | None = None, split: str | None = None,
                   city_cluster: str | None = None, limit: int = 50,
                   offset: int = 0, sort: str | None = None,
                   direction: str = "desc", include_spec: bool = True) -> dict[str, Any]:
        """A page of cases for the dashboard's case browser -- most recently
        touched first, optionally filtered by state/split/city. Distinct from
        ``GET /v1/cases/{case_id}`` (one case by id, used for a direct lookup).

        ``include_spec=false`` leaves out the largest column, which more than
        halves the page; a caller that wants one case's spec asks for that case."""
        return db.list_cases(conn, state=state, split=split, city_cluster=city_cluster,
                             limit=limit, offset=offset, sort=sort, direction=direction,
                             include_spec=include_spec)

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
