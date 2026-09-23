# casebroker — one queue for the v2 CFD campaign

[![broker](https://img.shields.io/website?url=https%3A%2F%2Fcasebroker.onrender.com%2Fhealthz&label=broker&up_message=live&down_message=down&style=flat-square)](https://casebroker.onrender.com/healthz)
[![tests](https://img.shields.io/github/actions/workflow/status/SustainableUrbanSystemsLab/casebroker/test.yml?branch=main&label=tests&style=flat-square)](https://github.com/SustainableUrbanSystemsLab/casebroker/actions/workflows/test.yml)
[![version](https://img.shields.io/github/v/tag/SustainableUrbanSystemsLab/casebroker?label=version&style=flat-square)](https://github.com/SustainableUrbanSystemsLab/casebroker/releases)
[![license](https://img.shields.io/github/license/SustainableUrbanSystemsLab/casebroker?style=flat-square)](LICENSE)

A central database of cases plus an HTTP work queue, so that many machines —
PACE ICE, PACE Phoenix, the lab workstation, anyone else's box — can each ask
*"what should I simulate next?"* and never collide.

## Where things are

| Path | What |
| --- | --- |
| [`DOMAIN.md`](DOMAIN.md) | The nouns — case, lease, worker, fleet, geometry — and the invariants |
| [`AGENTS.md`](AGENTS.md) | How to change this without breaking the campaign |
| [`docs/protocol.md`](docs/protocol.md) | Client/server interaction, every endpoint, the state machine |
| [`docs/operations.md`](docs/operations.md) | First run, accounts, tokens, storage, releases, deploying |
| [`docs/dashboard.md`](docs/dashboard.md) | The ops UI and read-only sharing |
| [`docs/releases.md`](docs/releases.md) | Moving the fleet between builds while a campaign runs: catalog, canary, promote, roll back, the badges |
| [`docs/e3d-contract.md`](docs/e3d-contract.md) | The seam to the CFD: what `E3D.exe` must do on the Python path, and why it never holds a broker credential there |
| `casebroker/` | The service: `app.py` (API), `db.py` (both engines, and the schema), `auth.py` (passwords, sessions, machine tokens), `worker.py` (the client), `cli.py` (`casebroker`), `ids.py` (case identity and splits) |
| `runner/run_case.sh` | The seam to the CFD: geometry → mesh → solve → sample |
| `slurm/` | Worker pools for Phoenix and ICE |
| `tests/` | The suite: SQLite-only by default, with a Postgres set that runs when `CASEBROKER_TEST_PG_DSN` is set |

## Run it

```bash
# server. The schema creates itself on the first connection -- there is no
# migration step, and no token needed to start.
CASEBROKER_DB=campaign.sqlite \
  uv run uvicorn casebroker.app:app --host 0.0.0.0 --port 8000
```

Then open <http://localhost:8000> and **create the admin account** the page asks
for. That is what turns auth on: until an account exists (and with no env tokens
set) every caller has full access, and `/healthz` says so with `"auth": "OPEN"`.
Signed in, **Machines** ▸ *Issue token* mints one credential per box and shows it
once.

```bash
# a worker, anywhere that can reach it
uv run python -m casebroker.worker \
  --broker https://broker.example.org --token <that machine's token> \
  --runner runner/run_case.sh --max-cases 4
```

No browser? The same first two steps, headless:

```bash
uv run casebroker init-db      --db campaign.sqlite     # optional; the server does this too
uv run casebroker account create --db campaign.sqlite --username ada --role admin
```

**Before this faces the internet**, set `CASEBROKER_SETUP_TOKEN` — the setup form
cannot require a login, so otherwise whoever reaches it first becomes your
permanent admin. Full sequence in
[docs/operations.md](docs/operations.md#first-run-from-nothing-to-a-working-broker).

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

The broker badge pings `/healthz` (unauthenticated, so uptime checks need no
token). It says whether the service is up, not whether the campaign is moving —
the dashboard answers that.

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
uv run casebroker worker setup --broker URL                   # enrol THIS machine as a worker
uv run casebroker account create --username ada --role admin  # the first admin, headless
uv run casebroker account passwd --username ada               # forgot it -- no old password needed
uv run casebroker account list                                # who exists, and last login
uv run casebroker init-db                                     # create/upgrade the schema alone
uv run casebroker token new                                   # generate a shared env token
uv run casebroker token check --broker URL --expect write     # what can this one do?
uv run casebroker health  --broker URL                        # version and auth posture
uv run casebroker doctor  --broker URL                        # find the broken piece
uv run casebroker fleet   --broker URL --cluster ICE          # report squeue (login node)
uv run casebroker release register release.json --broker URL  # add a node build to the catalog
uv run casebroker release promote <worker> --broker URL       # canary is good: whole fleet onto it
uv run casebroker release rollback --broker URL [--block]     # back to the previous build (kill switch)
uv run casebroker repro <case_id> --e3d E3D.exe --engine bluecfd  # re-run a failed case HERE
uv run casebroker triage <study_dir>                          # name the known failure in its logs
```

Full endpoint table in [docs/protocol.md](docs/protocol.md).

</details>

## Updating the nodes — and testing the auto-updater

A node updates itself only when it runs under the **supervisor**, `E3D node`.
Started as `E3D run-sim-node` (or `run-simulation-node`) it never switches, and
says so at start-up: *"started directly, so it will not switch builds by
itself"*. The broker never ships a file. It names the build and its SHA-256,
and the node installs that file from its **release share**, by default
`%LOCALAPPDATA%\Eddy3D\node\releases` (point Syncthing at it on a real fleet).
The whole model, with the guards and badges, is in
[docs/releases.md](docs/releases.md).

To test it on ONE machine before trusting it with the fleet:

1. **Run the machine under the supervisor.** Stop its node, then start it
   with the same options, `node` in place of `run-sim-node`:

   ```powershell
   E3D.exe node --done E:\wind\done --cpus 36
   ```

   A case in flight is resumed, not restarted.
2. **Put a different build on its share.** Every push to Eddy3D `dev` builds
   one (`gh release download e3d-node-latest -R Eddy3D-Dev/Eddy3D -p E3D.exe`).
   Publish it into the share; this prints the `release.json` the broker needs:

   ```powershell
   E3D.exe node-release publish --file .\E3D.exe --to $env:LOCALAPPDATA\Eddy3D\node\releases > release.json
   ```

3. **Register it**:
   `uv run casebroker release register release.json --broker URL --username <admin>`
   (or paste `release.json` into *Machines → Node builds → Register a published build*).
4. **Make this one machine the canary.** *Machines → Node builds*: set its
   *Canary target* to the new build, and pick when it switches: `direction` is
   the right test, since it proves a case survives the switch without waiting
   for the case to finish.
5. **Check that it switched.** Within a heartbeat, the node reports that it
   installed and verified the file and will switch. At the boundary it exits
   and the supervisor starts the new build, which asks for its own case back
   and continues at the next direction **without spending an attempt**. Look
   for all four:
   - `%LOCALAPPDATA%\Eddy3D\node\versions\<build>\` exists and `current.txt`
     names it;
   - the dashboard's Build column shows the new build and no `→` badge;
   - the case still says `attempt 1`;
   - the finished direction folders carry an `.e3d-build` stamp from the build
     that solved each one.
6. **Test the way back.** Set the canary target back to the old build. The
   node switches again at the boundary, and nothing is downloaded, because
   the old build is still installed side by side.
7. **Test the refusal.** Register a build whose file you did NOT put on the
   share, and make it the canary target. The node must stay where it is, and
   the Build column's `→` tooltip must say *the broker wants X and has no file
   on the share yet*. A node that runs something it could not verify is the
   failure this step is looking for.
8. **Then the fleet.** `uv run casebroker release promote <canary worker>` moves
   everyone to the canary's build in one call. `uv run casebroker release
   rollback` goes back to the previous fleet target, and `--block` also refuses
   the build being left, at every node's next lease.

Machines that are not under the supervisor yet stay on their build and are
counted *cannot update by themselves*. So step 1 has to be done once on every
machine, by hand.

## Status

The v2 campaign runs on Supabase Postgres behind Render, with workers on PACE
Phoenix and ICE. Two things are worth knowing before trusting a result:

- **Every building height is a prediction.** GlobalBuildingAtlas covers >97% of
  buildings, which retired Overture's coverage problem, but it did so by
  predicting a height for each one from lidar, optical and radar rather than
  measuring it — published RMSE 1.5–8.9 m by continent. The case inspector draws
  the per-building variance, and that, not the building count, is what to judge
  a case on.
- **Terrain is effectively complete on land.** Gaps are ocean, not missing data —
  GEDTM30 is a land DTM.
- **Trees are modelled where the canopy map reaches.** The Meta/WRI 1 m canopy
  height model covers the inhabited world and has polar and open-ocean gaps; a
  treeless site and an uncovered one are reported differently, because only one
  of them means the case is wrong.
