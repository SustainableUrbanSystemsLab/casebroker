# casebroker — one queue for the v2 CFD campaign

[![broker](https://img.shields.io/website?url=https%3A%2F%2Fcasebroker.onrender.com%2Fhealthz&label=broker&up_message=live&down_message=down&style=flat-square)](https://casebroker.onrender.com/healthz)
[![tests](https://img.shields.io/github/actions/workflow/status/SustainableUrbanSystemsLab/casebroker/test.yml?branch=main&label=tests&style=flat-square)](https://github.com/SustainableUrbanSystemsLab/casebroker/actions/workflows/test.yml)
[![version](https://img.shields.io/github/v/tag/SustainableUrbanSystemsLab/casebroker?label=version&style=flat-square)](https://github.com/SustainableUrbanSystemsLab/casebroker/releases)
[![license](https://img.shields.io/github/license/SustainableUrbanSystemsLab/casebroker?style=flat-square)](LICENSE)

The broker badge pings `/healthz`, which is unauthenticated precisely so
infrastructure checks work without a token. It reports whether the service is up,
not whether the campaign is progressing — the dashboard answers that.


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

## Where things are

| Path | What |
| --- | --- |
| [`DOMAIN.md`](DOMAIN.md) | The nouns — case, lease, worker, fleet, geometry — and the invariants |
| [`AGENTS.md`](AGENTS.md) | How to change this without breaking the campaign |
| [`docs/protocol.md`](docs/protocol.md) | Client/server interaction, every endpoint, the state machine |
| [`docs/operations.md`](docs/operations.md) | Tokens, storage, releases, deploying |
| [`docs/dashboard.md`](docs/dashboard.md) | The ops UI and read-only sharing |
| `casebroker/` | The service: `app.py` (API), `db.py` (both engines), `worker.py` (the client), `ids.py` (case identity and splits) |
| `runner/run_case.sh` | The seam to the CFD: geometry → mesh → solve → sample |
| `slurm/` | Worker pools for Phoenix and ICE |
| `tests/` | 103 tests on SQLite with no external dependency, plus 9 against a real Postgres |

## Run it

```bash
# server
CASEBROKER_DB=campaign.sqlite \
CASEBROKER_WRITE_TOKENS=$(uv run casebroker token new --quiet) \
  uv run uvicorn casebroker.app:app --host 0.0.0.0 --port 8000

# a worker, anywhere that can reach it
uv run python -m casebroker.worker \
  --broker https://broker.example.org --token <write token> \
  --runner runner/run_case.sh --max-cases 4
```

Without `--runner` the worker uses a built-in echo runner — useful to smoke a new
deployment without spending CFD time, though never against a real campaign, since
it reports fabricated results.

<details>
<summary><b>The runner contract</b> — what <code>--runner</code> must do</summary>

The case spec arrives as JSON on stdin and in `$CASE_SPEC`. The last line of
stdout must be a JSON object carrying at least `result_uri`.

Exit codes are a contract with the broker:

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `64` | **This site is broken and must never be retried** — degenerate geometry that will fail identically everywhere, forever |
| anything else | Retryable: a node died, an image pull failed, a host was down |

Getting that distinction wrong is expensive in one direction only. A wrongly
retryable error costs at most three attempts; a wrongly fatal one removes a site
from the campaign permanently. Treating a missing input file as fatal once
quarantined 173 perfectly good sites in under a minute.

</details>

<details>
<summary><b>Why a broker</b> rather than splitting the case list up front</summary>

- **Phoenix's free `embers` QOS preempts jobs after one hour.** A statically
  assigned case dies with its job. A leased case comes back to the pool by
  itself, so preemption costs one partial solve instead of a lost slot.
- **Machines are wildly unequal.** ICE, Phoenix and the workstation differ by
  more than an order of magnitude in throughput, and Phoenix's availability
  changes hour to hour. Pull beats push whenever the workers' speeds are unknown.
- **The dataset keeps growing** (5k → 10k → 30k). Adding cases is one idempotent
  POST; nothing is renumbered and no worker needs restarting.
- **A Windows box behind a firewall can take part.** Workers need outbound HTTPS
  and nothing else — the broker never calls a worker.

</details>

<details>
<summary><b>Quick reference</b> — the calls you will actually type</summary>

```bash
uv run casebroker token new                                   # generate a token
uv run casebroker token check --broker URL --expect write     # what can this one do?
uv run casebroker health  --broker URL                        # version and auth posture
uv run casebroker fleet   --broker URL --cluster ICE          # report squeue (login node)
```

Full endpoint table in [docs/protocol.md](docs/protocol.md).

</details>

## Status

The v2 campaign runs on Supabase Postgres behind Render, with workers on PACE
Phoenix and ICE. Two things are worth knowing before trusting a result:

- **Overture building coverage is uneven.** Roughly 45% of sampled sites return
  buildings at all, and LCZ 1 — the scarcest class — about 25%. Where a site does
  have footprints, many carry no height and are extruded from the tile median;
  the case inspector shows that ratio per case, and it is what to judge a case on.
- **Terrain is effectively complete on land.** Gaps are ocean, not missing data —
  GEDTM30 is a land DTM.
