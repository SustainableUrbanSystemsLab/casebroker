# Node releases: moving a fleet between builds while a campaign runs

A campaign runs for months and its nodes are updated in the middle of it, with
cases in flight, on machines nobody restarts in a hurry. The broker's answer is
SLURM's: the controller says **which** build to run and never ships code. It
holds a catalog of published builds with their hashes, one fleet target, a
per-worker override for a canary, and the switches an operator flips while it
runs; each node takes the file from its own release share and verifies it
against the hash it was given.

## The words

| | |
| --- | --- |
| **build** | `version+commit`, as in `1.14.0.827+e044a147`. The product version is the same for every push to `dev`, so the commit is what tells two nodes apart. `E3D version --json` prints it. Builds are **named, never ordered**: a roll back is just another target. |
| **release** | one build's file for one platform (`win-x64`, `linux-x64`, `osx-arm64`), registered with its sha256. The broker never holds or serves the file. |
| **target** | the build the fleet should be on. A worker's own `target_build` overrides it: the canary. |
| **apply** | when a node switches: `case` (after the case in flight), `direction` (after the wind direction being solved; `--resume` skips solved ones), `now` (the case is given back). |
| **drain** | no new case for this worker; the one in flight finishes; it may still resume its own. |
| **blocked** | a build refused new cases at every lease (426). `require_build` refuses workers that declare none. |

## The loop

1. **Build.** Every push to Eddy3D `dev` runs `e3d-node-build.yml`, which
   publishes `E3D.exe`, `E3D-linux-x64` and `E3D-macos-arm64` to the rolling
   `e3d-node-latest` GitHub release, with `SHA256SUMS.txt` and `release.json`
   (`[{build, platform, file, sha256}]`).
2. **Share.** Put the files on the release share: the receive-only Syncthing
   folder each node has next to the one its archives leave through.
3. **Register.** From a terminal or a CI step:

   ```bash
   casebroker release register release.json --broker https://casebroker.onrender.com --username ada
   ```

   (`--password-stdin` for a CI secret; `--notes` for a line shown beside the
   build.) Or paste `release.json` into the dashboard's *Register a published
   build*.
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
| `shared id?` | its build flipped back and forth within minutes: two clients share this worker id — an E3D node and a Python worker started from the same `machine.env`. Stop one |

## The guards

- A target, fleet or canary, must be a **published** build, and must have a file
  for **every platform the live fleet runs on** — otherwise 409 naming the
  platform, and `force` to insist and leave those workers behind. The catalog
  lists `missing_platforms` per build.
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
| `GET /v1/releases` | the catalog, target, `previous_target`, policy, `stuck_after`, per-build stats (`published`, `missing_platforms`, rates, `mean_wall_seconds`), and the `fleet` summary |
| `POST /v1/releases` | register `{build, platform, file, sha256, notes}` |
| `DELETE /v1/releases/{build}` | remove a build from the catalog (guarded) |
| `PUT /v1/releases/target` | `{build, apply, force}`; `build: null` clears it |
| `PUT /v1/releases/policy` | `{require_build, blocked_builds, release_repo}` — the repo (`owner/name`) gives the panel commit and compare links |
| `POST /v1/releases/promote` | `{worker_id}` |
| `POST /v1/releases/rollback` | `{block}` |
| `PUT /v1/workers/{id}/target` | `{build, force}`; `null` follows the fleet |
| `POST /v1/workers/{id}/drain`, `/undrain` | `{reason}` |
| `GET /v1/node/release` | what this node should run: `target_build`, `canary`, `current`, `apply`, `file`, `sha256`, `drain`, `blocked` |

The terminal has the same: `casebroker release list|register|target|promote|rollback`.

## The Python worker

`casebroker.worker` declares its build from `E3D version --json` and shows on
the fleet table like any node, but it never asks `/v1/node/release`, so it
never updates itself; the table says *cannot update itself*. Update it by
hand — copy the new `E3D.exe` over the old, start the worker again — and it
declares the new build with its next lease. A campaign that blocks its build,
or insists on a declared one it lacks, stops it with exit code 3 and the
broker's reason.
