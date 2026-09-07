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
| `casebroker/ids.py` | Stable case ids, and split assignment by **city** (not tile) |
| `casebroker/db.py` | SQLite storage and the atomic lease/complete/fail/release transitions |
| `casebroker/app.py` | FastAPI app (`create_app(db_path, tokens)`) |
| `casebroker/worker.py` | The client: lease → run → report, with heartbeat and SIGTERM release |
| `tests/` | 17 tests, including a real-uvicorn / 8-worker / 60-case concurrency run |

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

## Deploying

`Dockerfile` and `compose.yaml` are here for a cloud VM. Two things are **not**
done and must be before this faces the internet:

1. **Put it behind TLS.** The token is a bearer credential in a header; over
   plain HTTP it is readable by anything on the path. Terminate TLS at Caddy or
   nginx, or run behind a tunnel.
2. **Set `CASEBROKER_TOKENS`.** With none set, auth is *off* — `GET /healthz`
   reports `"auth": "OPEN"` so this is visible rather than silent, but nothing
   stops it.

SQLite is deliberate for the prototype and is fine to roughly 30k cases and a few
dozen workers: every write is one short serialised transaction. If workers ever
outgrow it, every statement lives in `db.py` and nothing else needs to change.
