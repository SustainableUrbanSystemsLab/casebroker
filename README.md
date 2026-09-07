# casebroker — one queue for the v2 CFD campaign

A central database of cases plus an HTTP work queue, so that many machines —
PACE ICE, PACE Phoenix, the lab workstation, anyone else's box — can each ask
*"what should I simulate next?"* and never collide.

Why a broker rather than splitting the case list across machines up front:

- **Phoenix's free `embers` QOS preempts jobs after one hour.** A statically
  assigned case dies with its job. A leased case comes back to the pool by
  itself, so preemption costs one partial solve instead of a lost slot.
- **Machines are wildly unequal.** ICE, Phoenix and the workstation differ by
  more than an order of magnitude in throughput, and Phoenix's availability
  changes hour to hour. Pull beats push whenever the workers' speeds are unknown.
- **The dataset keeps growing** (5k → 10k → 30k). Adding cases is one idempotent
  POST; nothing is renumbered and no worker needs restarting.

## Layout

| Path | What |
| --- | --- |
| `casebroker/cli.py` | The `casebroker` command: generate a token, ask a broker what one can do, check a deployment's auth posture |
| `casebroker/ids.py` | Stable case ids, split assignment by **city** (not tile), and the recipe-independent `selection_rank`/`is_selected` used for append-only site sampling |
| `casebroker/db.py` | Storage for both engines (SQLite for dev/tests, Postgres for production) and the atomic lease/complete/fail/release transitions |
| `casebroker/app.py` | FastAPI app (`create_app(db_path, tokens)`) — the JSON API plus the `/` dashboard route |
| `casebroker/static/dashboard.html` | The dashboard itself: one dependency-free HTML/JS file, no build step |
| `casebroker/worker.py` | The client: lease → run → report, with heartbeat and SIGTERM release |
| `runner/run_case.sh` | The per-case seam to the CFD: build → mesh → solve → sample, on node-local scratch |
| `slurm/phoenix_worker.sbatch` | A pool of pull-based workers on Phoenix's free, preemptible `embers` QOS |
| `CHANGELOG.md` | What changed in each release, and the semantic-versioning contract |
| `.github/workflows/release.yml` | Tag-triggered GitHub Release; validates the tag against `pyproject.toml` first |
| `tests/` | 73 tests against SQLite (no external dependency), plus 7 more in `test_db_postgres.py` that run only when `CASEBROKER_TEST_PG_DSN` points at a real Postgres instance |

## Run it

```bash
# server
cd benchmark/casebroker
CASEBROKER_DB=campaign.sqlite CASEBROKER_WRITE_TOKENS=$(uv run casebroker token new --quiet) \
  uv run uvicorn casebroker.app:app --host 0.0.0.0 --port 8000

# a worker, anywhere that can reach it
uv run python -m casebroker.worker \
  --broker https://broker.example.org --token some-long-random-token \
  --runner /path/to/run_one_case.sh --max-cases 4
```

`--runner` is the seam to the CFD. The script receives the case spec as JSON on
stdin and in `$CASE_SPEC`, and must print a JSON object whose last stdout line
carries at least `result_uri`. Exit **64** means *this case is broken and must
never be retried* (bad geometry, for instance); any other non-zero exit is
treated as retryable. The broker therefore never needs to know what OpenFOAM is.

Without `--runner` the worker uses a built-in echo runner — useful to smoke a
new deployment without spending CFD time.

## Dashboard

`GET /` serves a small ops UI — no build step, one HTML file, no dependency on
anything outside the broker's own API:

- campaign status (counts by state and by split, expired leases, 24h throughput, ETA),
  auto-refreshing every 60s by default
- the worker table, including which host/cluster each worker actually ran on
- a browsable, paginated, filterable case list — click a row to expand every
  field the broker holds on it: state, split, LCZ, attempts, **which machine
  produced it** (worker/host/cluster, from the runner's own report at
  completion), **where its result is finally stored** (`result_uri`, bytes,
  sha256), wall time, and the last error if it has one — plus a jump-to-id box
  for a direct lookup

Open it in a browser, paste in the broker's URL and a write token value
(kept only in that browser's local storage, sent as a bearer header on each API
call). The token field is a real `<input type="password">` inside a `<form>` with
a submit control, so a browser's own password manager can recognise and offer to
save it, exactly like any other login form. The page itself carries no secrets and
loads with no auth; every request it makes for actual data goes through the same
token check as any other client.

**Sharing a read-only view.** Set `CASEBROKER_READ_TOKENS` (same
comma-separated shape as `CASEBROKER_WRITE_TOKENS`) to a *separate* value from your
worker token, paste it into the "Share a read-only link" box in the Connection
panel, and click **Copy read-only link** — it builds a
`https://.../?token=...&ro=1` URL that pre-fills the token, connects
automatically, and shows a banner. This is a real second credential, not a
client-side restriction: a read-only token is rejected with 401 by every
mutating endpoint (`POST /v1/cases`, `/v1/lease`, `/v1/heartbeat`,
`/v1/complete`, `/v1/fail`, `/v1/release`) regardless of how it is presented —
someone you send the link to could `curl` the API directly with it and still
could not lease, complete, fail or release a case, or add new ones. Never put
a write token in a link you hand out; it has none of these
restrictions.

## How the client and server interact

The broker is an HTTP service in front of a database. It hands out work and
records results, and it **never initiates anything** — it does not know which
machines exist until one asks for a case, and it cannot reach into a cluster to
start a job. Every machine runs the same client, and each one *pulls*:

```
  PACE Phoenix  ──┐
  PACE ICE      ──┤   POST /v1/lease        ┌──────────┐      ┌──────────┐
  workstation   ──┼──  "give me a case"  ──▶│ casebroker│ ───▶ │ Postgres │
  anyone else   ──┘                         └──────────┘      └──────────┘
                                             stateless         the campaign
```

That is the whole reason for a broker rather than splitting the case list up
front: the machines differ by more than an order of magnitude in throughput and
Phoenix's availability changes hour to hour, so a fast box simply comes back
sooner. Adding a machine needs no server-side change — only a URL and a token.

### One case, end to end

A worker loops: lease → run → report → repeat. The wrinkle is that a case takes
**hours** while a lease lasts **30 minutes**, so the client renews it from a
background thread while the CFD runs.

```mermaid
sequenceDiagram
    participant W as worker.py
    participant B as broker
    participant R as run_case.sh
    W->>B: POST /v1/lease
    B-->>W: lease_id + case spec (state → leased, 30 min TTL)
    W->>R: spec as JSON on stdin
    loop every 5 min, while the solve runs
        W->>B: POST /v1/heartbeat
        B-->>W: 200, lease extended
    end
    R-->>W: result_uri on the last stdout line
    W->>B: POST /v1/complete
    B-->>W: 200, state → done
```

**The lease is the unit of ownership, not the case.** Every call after the first
is keyed by `lease_id`, never by `case_id` — that is what lets the broker tell
the rightful owner from a worker that woke up late still holding a stale claim.

Step by step:

1. **Lease.** Ask for one case. An empty list back means the queue is drained —
   a normal answer, not an error; after ten idle polls the worker exits so the
   SLURM allocation is freed.
2. **Receive.** The broker atomically claims a row and returns a `lease_id` with
   the full spec. Concurrency is settled here, inside one SQL statement.
3. **Run.** The spec goes to `run_case.sh` as JSON on stdin. The broker knows
   nothing about OpenFOAM; this is the only seam.
4. **Heartbeat.** A background thread renews every 5 min. A **409** means the
   case was taken away — the worker abandons it rather than finishing work it no
   longer owns.
5. **Report.** The runner's last stdout line carries `result_uri`; the worker
   posts it with metrics, wall time and which machine produced it.
6. **Repeat.**

Defaults are the client's: `lease_seconds=1800`, `heartbeat_seconds=300`.

### When a worker dies

Phoenix's free `embers` QOS preempts after an hour, so a worker dying mid-solve
is the *normal* case, not an edge case. All three paths end with the work getting
done; they differ in how much is wasted.

| | What happens | Cost |
| --- | --- | --- |
| **Preempted** (SIGTERM first) | The client catches the signal and calls `POST /v1/release`. State → `pending`, **and the attempt is refunded** — preemption is not the case's fault. | One partial solve. Back in the pool in under a second. |
| **Killed** (no warning) | Heartbeats simply stop and the lease TTL lapses. Nothing detects this, and nothing needs to. | Up to one lease period of idle before someone re-leases it. |
| **Zombie returns** | The old worker finishes and calls `complete` with a superseded `lease_id`. Rejected with **409**; the real owner's result stands. | Nothing — but see below. |

The third is the dangerous one, and why every mutating call re-checks the lease
rather than trusting it — see *Three design decisions worth knowing* below for
what that prevents. The operational rule it leaves you with: **a 409 means
stop.** Wherever it appears, heartbeat or complete, the case belongs to someone
else now, and continuing burns core-hours on a result the broker will refuse.

## The protocol

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/cases` | Append cases. **Idempotent** — re-posting an existing id is a no-op, which is how the dataset grows |
| `GET /v1/cases` | Paginated, filterable (`state`, `split`, `city_cluster`) list of cases, most recently touched first — what the dashboard's case browser calls |
| `GET /v1/cases/{case_id}` | One case's full record by id |
| `POST /v1/lease` | Claim up to N cases. Empty list = drained, not an error. Workers report their `host`/`cluster` here (optional) so "what machine produced this" stays answerable later |
| `POST /v1/heartbeat` | Extend the lease. **409 means stop working on that case** |
| `POST /v1/complete` | Report a result pointer + metrics |
| `POST /v1/fail` | Report a failure; `retryable=false` quarantines immediately |
| `POST /v1/release` | Graceful preemption — requeues and **refunds the attempt** |
| `GET /v1/status` | Counts by state and split, expired leases, 24 h throughput, ETA |
| `GET /healthz` | Liveness, plus the running `version`, auth mode, per-scope token counts and redacted DB target. **Unauthenticated** — see Deploying |
| `GET /v1/whoami` | What the presented token can do (`write` / `read` / `none`). **Unauthenticated** — it answers *about* a credential rather than gating on one |

State machine:

```
pending --lease--> leased --complete--> done
   ^                  |
   |                  +-- fail(retryable) | lease expiry --> pending
   |                  +-- fail(fatal) | attempts > max ----> quarantined
   +-- release (preemption, attempt refunded) ---------------+
```

## Three design decisions worth knowing

**Lease expiry is the only liveness mechanism.** A worker that dies without
warning is not detected, reported, or reaped by anything; its lease simply lapses
and the next `POST /v1/lease` reclaims the case in the same statement that hands
out fresh work. There is no reaper process to run or monitor.

**A superseded lease cannot write.** If a worker is preempted, its case is
re-leased, and the original worker then wakes up and finishes, its `complete`
is rejected with 409. Without that, a zombie could overwrite the real owner's
result — and it would look exactly like a successful run.

**Splits are assigned by city, not by tile.** Tiles from one city share
morphology and often literal buildings at their edges, so a per-tile split leaks
the test set into training. `ids.split_for(city_cluster)` hashes the city, which
also means an existing case can never change split when the dataset grows —
unlike v1's `make_splits.py`, whose seed-42 shuffle over a fixed list reshuffles
everything the moment a case is appended.

## Storage: SQLite for dev, Postgres for production

`CASEBROKER_DB` decides the engine purely by its shape — a file path opens
SQLite, a `postgres://` or `postgresql://` DSN opens Postgres — and nothing
above `db.py` needs to know which one it got:

```bash
CASEBROKER_DB=campaign.sqlite                                    # local dev/tests
CASEBROKER_DB=postgresql://user:pass@host:5432/postgres           # production
```

SQLite needs no network and no credentials, so the tests that exercise lease
semantics (`test_db.py`) run in milliseconds with zero external dependencies.
Postgres is what a real campaign deploys against: a managed database survives a
service restart or redeploy where a container's local disk does not, and it is
what lets the service itself be **stateless and disk-free** — see Deploying below.

The two engines share every function name and diverge only where it would be
actively wrong to pretend they could not: **claiming work under concurrency**.
SQLite's `BEGIN IMMEDIATE` takes a whole-database lock; Postgres uses
`SELECT ... FOR UPDATE SKIP LOCKED`, a per-row lock that lets independent
connections claim different rows without blocking each other. `test_db_postgres.py`
verifies this against a real Postgres instance — set `CASEBROKER_TEST_PG_DSN` to
run it; it is skipped otherwise. It also isolates the exact locking clause with
two hand-driven transactions (bypassing `db.py`'s in-process lock entirely,
which a naive many-threads-in-one-process test cannot do — see that file's
docstrings for why the first attempt at this test could not have caught a
regression no matter how many threads it used).

**If you point `CASEBROKER_DB` at a connection POOLER** (Supabase, PgBouncer,
RDS Proxy) **in transaction-pooling mode**: `_connect_postgres` already passes
`prepare_threshold=None` to disable psycopg's automatic server-side prepared
statements. Without it, a transactional pooler can route consecutive
transactions to different backend connections, and a prepared-statement name
collides across sessions — measured as `DuplicatePreparedStatement` errors
starting on roughly the sixth call to the same query text.

## Tokens

Two buckets, **named for what they grant** rather than for what they are:

| Variable | Grants | Carried by |
| --- | --- | --- |
| `CASEBROKER_WRITE_TOKENS` | read **and** write — lease, heartbeat, complete, fail, release, add cases | workers, and `site_sampler.py publish` |
| `CASEBROKER_READ_TOKENS` | read only — 401 from every mutating endpoint | a dashboard link you hand out |

A write token also reads, so you never need both to operate. Each is a
comma-separated list, which is how you **rotate without stranding a worker
mid-lease**: add the new token, move the workers over, then drop the old one.

```bash
uv run casebroker token new                       # generate one, with a hint on where to put it
uv run casebroker token check --broker URL --token T   # what can this token actually do?
uv run casebroker health  --broker URL            # version + auth posture of a deployment
```

`token check` exits non-zero when `--expect` disagrees with the answer, so it
works as a deploy gate. `health` exits non-zero when auth is OFF.

`GET /v1/whoami` is what those calls use, and it is deliberately unauthenticated
and always `200`: it answers *about* a credential rather than gating on one, and
returning `{"scope": "none"}` instead of `401` is what makes "this token is wrong"
distinguishable from "the broker is unreachable". Previously the only way to
discover a token's capability was to attempt a write against production and see
whether it failed.

The older names — `CASEBROKER_TOKENS` and `CASEBROKER_READONLY_TOKENS` — still
work. Setting a variable *and* its deprecated twin to **different** values is
refused at startup rather than resolved by precedence: the quiet failure there is
a token you believe you revoked continuing to work.

## Versioning

[Semantic versioning](https://semver.org), declared **once** in
`pyproject.toml`. What the components mean for this service specifically:

- **MAJOR** — a breaking change to the worker-facing protocol in the table
  above. Workers are long-lived and deployed across machines nobody is going to
  restart in a hurry (a Phoenix `embers` pool can be mid-lease for an hour), so
  a broker that stops speaking the old protocol strands them.
- **MINOR** — a backwards-compatible addition: a new endpoint, a new optional
  field, a new dashboard feature.
- **PATCH** — a fix that changes no shape anybody can observe.

Nothing else in the tree hardcodes the number. `casebroker/__init__.py` reads it
back out of the installed distribution's metadata, and everything that reports a
version — the OpenAPI document, `/healthz`, the dashboard's header badge — goes
through `casebroker.__version__`. `tests/test_version.py` asserts they all agree
and **fails if a version literal is ever pasted into a source file again**, which
is how the three copies that used to exist got out of step in the first place.

Cutting a release is therefore: bump `version` in `pyproject.toml`, move the
`Unreleased` entries in `CHANGELOG.md` under the new number, commit, and tag it:

```bash
git tag -a v0.2.0 -m "v0.2.0" && git push origin v0.2.0
```

Pushing that tag is what publishes the release: `.github/workflows/release.yml`
fires on any `v*.*.*` tag, **refuses to publish if the tag disagrees with
`pyproject.toml`**, and creates the GitHub Release. So the tag cannot drift from
the declared version any more than the code can.

`/healthz` reporting `version` is what makes a deploy checkable from outside:

```bash
curl -s https://casebroker.example.org/healthz | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])"
```

## Deploying

`Dockerfile` and `compose.yaml` are here for a cloud VM; either works equally
for a platform that builds from a Dockerfile directly (Render, Fly, Railway).
Three things are **not** done and must be before this faces the internet:

1. **Put it behind TLS.** The token is a bearer credential in a header; over
   plain HTTP it is readable by anything on the path. Terminate TLS at Caddy or
   nginx, or use a platform that terminates it for you (Render, Fly).
2. **Set `CASEBROKER_WRITE_TOKENS`.** With none set, auth is *off* — `GET /healthz`
   reports `"auth": "OPEN"` so this is visible rather than silent, but nothing
   stops it. Optionally also set `CASEBROKER_READ_TOKENS` to a *different*
   value if you want a link you can hand out for viewing only — see
   "Sharing a read-only view" above.
3. **Point `CASEBROKER_DB` at Postgres, not a local SQLite file**, unless the
   platform gives that file a persistent disk. A container's local filesystem
   does not survive a redeploy; a managed Postgres database does. With state
   external, the service itself needs no disk at all — the cheapest compute
   tier a platform offers is enough.

**`/healthz` is intentionally unauthenticated** (so infrastructure health checks
work with no token) **and therefore must never return anything that could be a
credential.** It reports a redacted form of `CASEBROKER_DB` — the DSN with its
password masked (`_redact_db_target` in `app.py`), never the raw value. This was
not a hypothetical: an early version of this service returned the connection
string verbatim, which meant hitting `/healthz` against a real deployment
printed the live database password in plain text with no auth required to
trigger it. If you ever add something else to this endpoint, check it for the
same shape of mistake before shipping it.
