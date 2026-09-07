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
| `casebroker/ids.py` | Stable case ids, split assignment by **city** (not tile), and the recipe-independent `selection_rank`/`is_selected` used for append-only site sampling |
| `casebroker/db.py` | Storage for both engines (SQLite for dev/tests, Postgres for production) and the atomic lease/complete/fail/release transitions |
| `casebroker/app.py` | FastAPI app (`create_app(db_path, tokens)`) — the JSON API plus the `/` dashboard route |
| `casebroker/static/dashboard.html` | The dashboard itself: one dependency-free HTML/JS file, no build step |
| `casebroker/worker.py` | The client: lease → run → report, with heartbeat and SIGTERM release |
| `runner/run_case.sh` | The per-case seam to the CFD: build → mesh → solve → sample, on node-local scratch |
| `slurm/phoenix_worker.sbatch` | A pool of pull-based workers on Phoenix's free, preemptible `embers` QOS |
| `tests/` | 34 tests against SQLite (no external dependency), plus 7 more in `test_db_postgres.py` that run only when `CASEBROKER_TEST_PG_DSN` points at a real Postgres instance |

## Run it

```bash
# server
cd benchmark/casebroker
CASEBROKER_DB=campaign.sqlite CASEBROKER_TOKENS=some-long-random-token \
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

- campaign status (counts by state and by split, expired leases, 24h throughput, ETA)
- the worker table
- a one-case lookup by id

Open it in a browser, paste in the broker's URL and your `CASEBROKER_TOKENS` value
(kept only in that browser's local storage, sent as a bearer header on each API
call), and it starts pulling `/v1/status`. The page itself carries no secrets and
loads with no auth; every request it makes for actual data goes through the same
token check as any other client.

## The protocol

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/cases` | Append cases. **Idempotent** — re-posting an existing id is a no-op, which is how the dataset grows |
| `POST /v1/lease` | Claim up to N cases. Empty list = drained, not an error |
| `POST /v1/heartbeat` | Extend the lease. **409 means stop working on that case** |
| `POST /v1/complete` | Report a result pointer + metrics |
| `POST /v1/fail` | Report a failure; `retryable=false` quarantines immediately |
| `POST /v1/release` | Graceful preemption — requeues and **refunds the attempt** |
| `GET /v1/status` | Counts by state and split, expired leases, 24 h throughput, ETA |

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

## Deploying

`Dockerfile` and `compose.yaml` are here for a cloud VM; either works equally
for a platform that builds from a Dockerfile directly (Render, Fly, Railway).
Three things are **not** done and must be before this faces the internet:

1. **Put it behind TLS.** The token is a bearer credential in a header; over
   plain HTTP it is readable by anything on the path. Terminate TLS at Caddy or
   nginx, or use a platform that terminates it for you (Render, Fly).
2. **Set `CASEBROKER_TOKENS`.** With none set, auth is *off* — `GET /healthz`
   reports `"auth": "OPEN"` so this is visible rather than silent, but nothing
   stops it.
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
