# Hooking a machine into the campaign

How any Linux or Windows machine becomes a worker that pulls cases from the
broker, solves them, reports progress, survives being stopped, and gets its
finished cases back to the master. For the cluster-specific traps (SSH,
quotas, Podman, billing) see [`pace-hpc.md`](pace-hpc.md).

## The shape

```
                lease / heartbeat(progress) / complete
   worker  <------------------------------------------>  broker (Supabase/Render)
     |
     |  runner/run_case.sh          WIND_RUNTIME = podman | docker | native
     v
   geometry -> e3d build-case -> mesh -> solve (per direction) -> reconstruct
     |                                          |
     |  SIGTERM: checkpoint to $WIND_CASES      |  done: <case_id>.tar.gz -> $WIND_DONE
     v                                          v
   resume.json (asked for on next lease)     Syncthing (workstations) /
                                             scripts/pull_done.sh over SSH (PACE)
                                                          -> master done/
```

One runner, three OpenFOAM runtimes. The solve is the same inner script in
all three; only how it is launched and how MPI is spelled differ:

| `WIND_RUNTIME` | where | how |
| --- | --- | --- |
| `podman` | PACE ICE / Phoenix | rootless, no subuid range -- the runner sets `--root`/`--runroot`, fuse-overlayfs and `--user 0:0` itself |
| `docker` | lab workstations (Docker Desktop, WSL) | `docker run --entrypoint bash`, host path converted with `cygpath` on Windows |
| `native` | Windows with blueCFD-Core 2024 | OpenFOAM-12 `foamRun.exe` under MS-MPI `mpiexec`, no container. blueCFD's environment is rebuilt from its `setvars_OF12.bat` (sourcing OpenFOAM's `bashrc` in its MSYS2 does not work) |

`auto` (the default) takes the first found, in that order.

## Setting up a machine

1. **Prerequisites**: `uv`; one runtime from the table; the `e3d` CLI for the
   OS (Linux: the `linux-x64` single-file build, currently at `$WC/bin/e3d` on
   both clusters); a checkout of this repo and of `real_cities` beside it;
   Git for Windows on a Windows box (`run_case.cmd` finds its bash).
2. **Profile**: copy `machine.env.example` to `machine.env` (gitignored) and
   fill it in. The two that matter most:
   - `CASEBROKER_WORKER_ID` -- stable **per machine**. It is what lets a
     restarted worker get its own half-finished case back. Never reuse one
     across machines.
   - `WIND_NP` -- ranks per case, **measured per machine** (below). Until
     measured, `min(24, cores/2)`.
3. **Start it**: `./start_worker.sh` (Linux, WSL, git-bash) or
   `.\start_worker.ps1` (Windows). Foreground; Ctrl-C stops it cleanly. On
   PACE the sbatch scripts under `slurm/` do the same inside a job.

That is the whole procedure. Everything below is what the pieces do.

## Progress on the dashboard

The runner parses the solver log every 60 s -- **one residual per outer
iteration**, the first `Solving for` after each `Time =`; `p` is solved ~6x per
step and reading every line makes a clean descent look like an oscillation --
and writes one line such as

    case_270 [3/8 dirs] iter 412 p=3.2e-05 Ux=8.1e-07 (2.3 h)

to the file the worker names in `CASEBROKER_PROGRESS_FILE`. The heartbeat ships
it as `detail` instead of `"alive"`; the broker keeps the latest per case as
`last_progress` / `last_progress_at` on the case row, and the dashboard's
case detail shows it. No new endpoint: it rides the heartbeat that already had
to happen.

## Stopping and resuming (same machine only)

`SIGTERM`/`SIGINT` -- SLURM walltime, `embers` preemption, Ctrl-C -- is the
normal way a solve stops, not an error. The runner:

1. stops the solver,
2. copies the study (mesh, every direction's last written time step,
   dictionaries, logs) to `$WIND_CASES/<case_id>/`,
3. writes `resume.json` there naming the case and this `CASEBROKER_WORKER_ID`,
4. exits; the worker releases the lease (attempt refunded).

On its next lease the worker lists the `resume.json` markers that carry its
own id and sends them as `resume_case_ids`. The broker claims those **first**,
and a case still leased to the same worker id comes back **without spending an
attempt** -- continuing, not retrying. The runner restores the checkpoint,
keeps meshes already built, skips directions that already converged, and
continues each unfinished direction from its last time step with
`startFrom latestTime`.

Rules that follow from this:

- **A case never moves between machines mid-solve.** The checkpoint is on that
  machine's local disk; another worker's `resume_case_ids` for it is ignored.
- **Same `WIND_NP` on resume.** The decomposition on disk *is* the checkpoint;
  a different rank count restarts that direction from 0 (the runner says
  `RESTART` in `run.log` when it does).
- **`WIND_WRITE_INTERVAL` is the resume granularity**, default 200 iterations
  (~1-2 h). The old default was the whole iteration budget -- one write at the
  very end -- and an 8 h chunk cut at iteration 749 had nothing to resume from.

## What a finished case is

One `tar.gz` per case in `$WIND_DONE`, written under `.tmp/` and renamed into
place so nothing ever picks up a half-written archive:

```
<case_id>/
  mesh/constant/polyMesh/          the mesh, once
  case_<dir>/<latest>/             the RECONSTRUCTED last time step, every field
  case_<dir>/system/  constant/*   dictionaries (constant minus the mesh link)
  case_<dir>/*.log  postProcessing/  solver logs, pedestrian-plane sample
  run.log build.json cfg.json spec.json preview_*.png manifest.json
```

Full field data, last time step only, reconstructed on the client -- rank
counts differ per machine, so a decomposed result would be unusable anywhere
else. About 250-300 MB at the 3.0 m default; ~1.5 TB for 5,000 cases on the
master. `case_*/constant/polyMesh` is deliberately not in the archive (it is a
link to `mesh/`); relink it to open a case in ParaView. The broker's
`result_uri` is the archive's path on the machine that made it, and
`result_sha256`/`result_bytes` are the archive's.

## Getting archives to the master

**Workstations: Syncthing.** Share `$WIND_DONE` -- *only* that folder, never a
live case tree; a solve writes thousands of files per rank per step and
Syncthing would spend its life hashing them.

- client folder **Send Only**, master folder **Receive Only** with
  `ignoreDelete` on, so a client may delete its local archive after upload
  without that deleting the master's copy;
- add `.tmp` to the folder's ignore patterns (the in-progress archive);
- no inbound port is needed on the master: every device connects out to the
  discovery/relay network. Direct connections (port 22000 reachable on at
  least one side) are much faster than public relays for GB-scale archives.

**PACE: the master pulls.** A daemon does not fit shared login nodes or
job-lifetime compute nodes, and compute nodes cannot be reached from outside
anyway. The master already has SSH to the login nodes, so
`scripts/pull_done.sh <master done dir> ice:<WIND_DONE> phoenix:<WIND_DONE>`
on a timer collects finished archives; `PULL_REMOVE=1` deletes on the cluster
after a size-verified copy, which is what keeps ICE's 300 GB scratch from
filling with results.

## Ranks per machine

Different machines have different knees. `C:\rc2\scaling_test.sh` (local) and
the `scaling-sweep` job (PACE) run the same 3.9M-cell case for a short bounded
number of iterations at several rank counts and record iterations/hour and
per-rank efficiency; the knee -- where adding ranks stops paying -- is the
machine's `WIND_NP`. Results so far live in `scaling_results.csv` on each
machine. Known ceilings that no sweep will move: **24 ranks per job on PACE**
(a scheduler policy, not hardware -- `-N 1 -n 28` is refused outright), and on
Windows the count MS-MPI's `mpiexec -n` will launch on one box.

## Smoke-testing a machine without a real solve

`WIND_ALLOW_UNCONVERGED=1` makes the runner accept a direction that ran out
of iterations without meeting `residualControl`, so a deliberately crude case
(one direction, a 48 m background, a few hundred iterations) exercises every
stage -- geometry, `build-case`, mesh, solve, checkpoint writes, reconstruct,
archive, result line -- in minutes. The archive's `manifest.json` and the
reported metrics carry `converged: false`. It is a dev switch: never set it
on a production worker, where an unconverged case is quarantined on purpose.

## Where each thing can go wrong

- A worker that exits in seconds "successfully": the podman path passes the
  inner command as ONE string because the image's entrypoint is
  `bash -i -c`; the docker path overrides the entrypoint instead. Do not
  "simplify" either into separate words.
- `Cannot find file "points" in directory "polyMesh"` from `decomposePar`:
  the mesh link is wrong. It is resolved relative to `case_*/constant/`, so it
  is two levels up (`../../mesh/...`). On the native runtime it is a copy.
- Everything resumed from time 0: the checkpoint had no written time step.
  Check `WIND_WRITE_INTERVAL` and that the solve ran longer than one interval.
- `RESTART` in `run.log` on a resume: rank count changed; see above.
- ICE/Phoenix `$HOME` is 20-30 GB and usually nearly full -- checkpoints and
  archives must go to scratch (`WIND_CASES`/`WIND_DONE` in the sbatch files),
  and a 100%-full scratch silently loses rank data during the copy-back.
