# Node releases: moving a fleet between builds while a campaign runs

A campaign runs for months and its nodes are updated in the middle of it, with
cases in flight, on machines nobody restarts in a hurry. The broker says
**which** build to run: it holds a catalog of published builds with their
hashes, one fleet target, a per-worker override for a canary, and the switches
an operator flips while it runs. It also holds the files, once they are
uploaded, and a node fetches its target from it over the channel it already
authenticates on; whether the file came from the broker or from the node's own
release share, the node runs it only if it hashes to what an admin registered.

## The words

| | |
| --- | --- |
| **build** | `version+commit`, as in `1.14.0.827+e044a147`. The product version is the same for every push to `dev`, so the commit is what tells two nodes apart. `E3D version --json` prints it. Builds are **named, never ordered**: a roll back is just another target. |
| **release** | one build's file for one platform (`win-x64`, `linux-x64`, `osx-arm64`), registered with its sha256. The broker serves the file once it has been uploaded (`stored` in the catalog); see [The files](#the-files). |
| **target** | the build the fleet should be on. A worker's own `target_build` overrides it: the canary. |
| **apply** | when a node switches: `case` (after the case in flight), `direction` (after the wind direction being solved; `--resume` skips solved ones), `now` (the case is given back). |
| **drain** | no new case for this worker; the one in flight finishes; it may still resume its own. |
| **blocked** | a build refused new cases at every lease (426). `require_build` refuses workers that declare none. |
| **undeclared recipes** | what a worker that declares **no recipes** may be handed. Unset, it takes any recipe (such workers predate declarations, and every recipe used to be a CFD one); `[]`, none. Set it to the wind recipes before posting a recipe of another kind: a script worker handed a Radiance case gives it back with exit 69 and stops. |

## The loop

1. **Build.** Every push to Eddy3D `dev` runs `e3d-node-build.yml`, which
   publishes `E3D.exe`, `E3D-linux-x64` and `E3D-macos-arm64` to the rolling
   `e3d-node-latest` GitHub release, with `SHA256SUMS.txt` and `release.json`
   (`[{build, platform, file, sha256}]`).
2. **Register and upload.** The same workflow does this itself once the Eddy3D
   repository has the secrets `CASEBROKER_RELEASE_USER` and
   `CASEBROKER_RELEASE_PASSWORD` (an admin account on the broker; the variable
   `CASEBROKER_URL` names another broker). By hand, from the folder the release
   was downloaded to:

   ```bash
   casebroker release register release.json --upload . --broker https://casebroker.eddy3d.com --username ada
   ```

   (`--password-stdin` for a CI secret; `--notes` for a line shown beside the
   build.) `--upload DIR` sends each row's `file` from `DIR` in chunks, resuming
   where an interrupted upload stopped, and waits until the broker has checked
   it against the registered sha256. Without it the build is only named: paste
   `release.json` into the dashboard's *Register a published build* to do just
   that.
3. **Fetch.** Nothing to do. A node under the `E3D node` supervisor looks on
   its release share first (`<node dir>/releases`, a folder on that machine)
   and otherwise downloads the target from the broker, a piece at every
   heartbeat, then installs and switches at the boundary chosen. A SLURM job
   runs `E3D node-release sync` before it starts its node: see
   [Nodes that cannot update themselves](#nodes-that-cannot-update-themselves).
   A machine can instead build `dev` itself (Eddy3D `scripts/node-update`),
   which installs and switches to that build without the broker; clear the
   fleet target while machines update that way, or it moves them back.
4. **Canary.** Point **one** worker at it: the *Canary target* select on the
   panel, `PUT /v1/workers/{id}/target`. Watch the *Node builds* table: done,
   unconverged and failed **with rates**, mean wall time, and — once both have
   five cases — *worse than &lt;previous&gt;* when it is.
5. **Promote.** The *Promote* button beside the canary, or
   `casebroker release promote <worker>`: the override is cleared and the
   fleet's target set, in one call that cannot be left half done.
6. **Watch the line.** *12 workers seen in 24 h: 9 on target · 2 behind
   (1 stuck, 1 cannot update by themselves) · 1 undeclared.* Each badge on the
   Build column says why.
7. **If it goes wrong.** *Roll back* returns to the target before this one
   (`casebroker release rollback`). The **kill switch** (`--block`, the button
   beside it) also refuses the build being left to every node at its next
   lease — for a build that has to stop now, not at each node's next ask.

## What the Build column says

| badge | meaning |
| --- | --- |
| `1.14.0.827+e044a147` | the build this worker declared with its last lease; hover for its platform |
| `→ <build> · 3h` | behind that target for that long. The tooltip says **why**, from what the broker knows: the node's own word (*the broker wants X and has no file on the share yet*, *installed and verified; switching after the case*); *nothing to fetch* (no file for its platform); *it tried this build and rolled back*; or *it has never asked the broker what to run* — a Python worker, or an `E3D.exe` from before releases, which cannot update itself |
| `· stuck` | behind for longer than a switch at the chosen boundary should take: 30 min for `now`, 4 h for `direction`, 12 h for `case` |
| `update failed` | it tried the target, could not start it, and went back; the tooltip has the reason |
| `blocked` | its build is refused new cases |
| `undeclared` | its client sent no build: update it by hand once (`gh release download e3d-node-latest -R Eddy3D-Dev/Eddy3D -p E3D.exe --clobber`, then `E3D node`), and it declares itself from then on |
| `shared id?` | its build flipped back and forth within minutes: two clients share this worker id — two E3D nodes started under one name (or, before it was retired, an E3D node and a Python worker sharing a `machine.env`). Stop one |

## What the Recipes column says

A node is handed only the recipes it declares with its lease, so the column
beside Build is where a recipe that never leaves pending is explained.

| chip | meaning |
| --- | --- |
| `of12-v6`, `rad6R0P2-fft-v2` | a recipe this worker declared with its last lease (hover for the family: `cyl-1008/…`, `surf-1008/…`). An E3D node declares the wind recipes it can build, `of12-v6` only where its site build has a canopy source, and the thermal recipe only where Radiance answers — the `radiance-energyplus` image through a container engine, or a native rad6R0P2 install. Its console says which at start: *declares the thermal recipe …* or *does not declare the thermal recipe …: <why>* |
| ~~`rad6R0P2-fft-v2`~~ (struck through) | the queue holds this recipe and this worker did not declare it, so it is never handed one |
| `none declared` | its lease names no recipes: an E3D from before declarations, or a `--runner` script. It is handed what the policy's *Recipes for nodes that declare none* says, or **anything** when that is unset — a Radiance case included, which it gives back with exit 69 and stops |

Above the table, one line per recipe the queue holds: *rad6R0P2-fft-v2: 48
pending · 2 leased · 3 of 20 workers declare it*. In red when no worker does.

## The guards

- A target, fleet or canary, must be a **published** build, and must have a file
  for **every platform the live fleet runs on** — otherwise 409 naming the
  platform, and `force` to insist and leave those workers behind. The catalog
  lists `missing_platforms` per build.
- A fleet target must **know the recipes the queue holds**. A build's
  knowledge is what its workers declared with their leases (`knows` per build,
  `queue_recipes` for the queue); a target that does not know a queued recipe
  would have every node on it refuse those cases, so it is refused with 409
  naming the recipe and how many cases need it, and `force` insists. A build
  no worker has described is not refused: unknown is not ignorant.
- A build cannot be **removed** from the catalog while it is the fleet's
  target, a canary's target, or what a live worker is running (409 says
  which).
- Pointing a fleet at code is remote execution by design, so **who may do it is
  the security model**: every change needs an admin session, never a machine
  token, and every one is in the audit trail — `release`, `setting`,
  `worker-target`, `promote`, `rollback`, `drain`, `update-failed`,
  `build-changed`, `shared-id`.

## What a node reports

| call | fields |
| --- | --- |
| `POST /v1/lease` | `build`, `version`, `platform`, `recipes` — with every lease, so an updated node is known as such at once |
| `GET /v1/node/release` | `worker_id`, `platform`, `build`; `state` (what it is doing about the target); `failed_build` + `failed_reason` (it tried and rolled back). Asked before every lease and at every heartbeat |
| `POST /v1/complete` metrics | `eddy3d_build` (the build the **archive** names — a case can be finished by a node updated after it started), `wall_seconds`, `unconverged_count` |

## Endpoints

| | |
| --- | --- |
| `GET /v1/releases` | the catalog (each row with `stored` and `bytes`: whether the broker holds that file), target, `previous_target`, policy, `stuck_after`, per-build stats (`published`, `missing_platforms`, rates, `mean_wall_seconds`), the `fleet` summary, and `release_files` (whether this broker can hold files at all) |
| `POST /v1/releases` | register `{build, platform, file, sha256, notes}` |
| `POST /v1/releases/{build}/{platform}/upload` | `{sha256, bytes}` of the file in hand: begin, resume or ask after its upload. 409 unless it is the registered sha256. Answers `stored`, `verifying`, or `absent`/`partial` with the `offset` to send from |
| `PUT /v1/releases/{build}/{platform}/upload?offset=&bytes=` | one chunk (at most 64 MiB) at `offset`; 409 with the broker's offset when that is not where the upload stands |
| `GET /v1/releases/{build}/{platform}/file` | the file, for a node: `ETag` and `X-Sha256` are the hash, byte ranges are answered (a node resumes a broken download). 410 while the broker does not hold it |
| `DELETE /v1/releases/{build}` | remove a build from the catalog (guarded), and its files with it (`files_removed`) |
| `PUT /v1/releases/target` | `{build, apply, force}`; `build: null` clears it |
| `PUT /v1/releases/policy` | `{require_build, blocked_builds, undeclared_recipes, release_repo}` — the repo (`owner/name`) gives the panel commit and compare links; `undeclared_recipes: null` clears that policy |
| `POST /v1/releases/promote` | `{worker_id}` |
| `POST /v1/releases/rollback` | `{block}` |
| `PUT /v1/workers/{id}/target` | `{build, force}`; `null` follows the fleet |
| `POST /v1/workers/{id}/drain`, `/undrain` | `{reason}` |
| `GET /v1/node/release` | what this node should run: `target_build`, `canary`, `current`, `apply`, `file`, `sha256`, `drain`, `blocked`, and `url` + `bytes` (where to fetch the file, relative to the broker) when the broker holds it, else null |

The broker also drains a worker by itself, when it fails
`CASEBROKER_FAIL_BURST_CASES` (5) different cases within
`CASEBROKER_FAIL_BURST_SECONDS` (600). The reason then starts `drained by the
broker:`, and it stays drained until someone undrains it.

The terminal has the same: `casebroker release list|register|target|promote|rollback`.

## The files

A build is about 535 MB over its three platforms (Linux ~155 MB, Windows ~160 MB,
macOS ~220 MB), and every push to `dev` makes one. They are kept in the part
store, content-addressed like a case's parts, and only these are:

- the newest `CASEBROKER_RELEASE_KEEP_BUILDS` builds (default 5);
- whatever the fleet still needs, however old: the fleet's target, the target
  before it (where *Roll back* goes), a canary's target, and any build a worker
  seen in the last 24 hours is running.

The others are let go when a new file lands; their catalog rows stay, and the
panel says *files not held here* (red for the target or the previous one, which
the fleet would need). Upload one again before pointing the fleet at it. Removing
a build from the catalog removes its files, unless a build that stays is the same
content. `/v1/parts/sweep` never touches a release file. Without a part store
(`CASEBROKER_PARTS_DIR=""`) the broker holds no files and nodes use their shares.

Who may do what is the same as for the catalog, and for the same reason:
uploading is an admin's (or the CI step's admin login), and the content must hash
to the sha256 an admin registered -- a chunk that does not is dropped when the
upload is checked, and the node checks again before it installs. Fetching takes a
credential that may write: a machine's token, or an operator's or admin's
session. A viewer and a shared read-only link cannot: these are executables, built
from a private repository.

## Nodes that cannot update themselves

A node started as `E3D run-sim-node` (not under the `E3D node` supervisor) asks
`/v1/node/release` but never switches while it runs; the fleet table says
*cannot update itself*. That is how every PACE job runs, and each job therefore
takes the target **before** it starts its node:

```bash
E3D node-release sync --exe ~/windcomfort/bin/E3D
```

It asks the broker what this worker should run, fetches the file (the release
share first, else the broker), checks its sha256, asks the new file what build it
is, and replaces `--exe` by a rename. The job scripts then run the node from a
copy on the job's own scratch, because `run-sim-node` starts every step of a case
by its own path: a job running the shared file would switch builds mid-case when
another job's sync swapped a new one in. *Up to date*, *updated*, or a one-line reason; the job scripts run
the E3D that is installed whatever it says. An E3D from before `sync` cannot do
this, so the first update on each cluster is by hand (docs/pace-hpc.md §8).

Anywhere else, update it by hand -- copy the new `E3D.exe` over the old and start
it again -- and it declares the new build with its next lease. (The Python worker,
`casebroker.worker`, which never asked at all, is retired: it could not build any
campaign recipe.)
