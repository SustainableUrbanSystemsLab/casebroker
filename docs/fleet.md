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
   both clusters); a `git clone --recurse-submodules` of
   [windcomfort-real-cities](https://github.com/SustainableUrbanSystemsLab/windcomfort-real-cities)
   — the campaign repo, which carries this one as `benchmark/casebroker` and
   `real_cities` beside it. **Not** JP-Wind-ML-Comparison: that is the paper,
   and its casebroker pin is stale by design now that the campaign has moved;
   Git for Windows on a Windows box (`run_case.cmd` finds its bash).
2. **Profile**: copy `machine.env.example` to `machine.env` (gitignored) and
   fill it in. The three that matter most:
   - `CASEBROKER_WORKER_ID` -- stable **per machine**. It is what lets a
     restarted worker get its own half-finished case back. Never reuse one
     across machines.
   - `CASEBROKER_TOKEN` -- this machine's own credential. Get it from the
     dashboard: sign in, **Machines** > *Issue token*, naming it after the
     `CASEBROKER_WORKER_ID` above. It is shown once, works immediately, and can
     be revoked for this one box without touching the rest of the fleet. A
     shared `CASEBROKER_WRITE_TOKENS` value still works too -- it is simply the
     older model. A cluster gets **one credential per cluster**, named after
     it: every SLURM task runs as `phoenix-<job>-<task>`, and a credential
     covers every worker id under its own name.
     `uv run casebroker worker setup --broker <url>`, run on the box itself,
     does the same without the dashboard and writes the token into
     `machine.env` for you (it asks for your admin login once and keeps no
     session). Browser pairing (`eddy3d-cli setup-sim-node`) is a
     different path: its credential serves the native `run-sim-node`
     runner and never lands in `machine.env` -- see
     [operations.md](operations.md#first-run-from-nothing-to-a-working-broker).
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
  case_<dir>/*.log  postProcessing/  solver logs, residuals, yPlus
  pedestrian/U.npz meta.json grid.json   the pedestrian field (below)
  <case_id>.wfld                   the dashboard's viewer bundle
  terrain.stl eddy3d-study.json    the sheet the field was cut on; angle + z0 per direction
  run.log build.json cfg.json spec.json preview_*.png manifest.json
```

(The Windows node's archives differ in layout -- `<id>/<id>/case_*`, `geometry/`.
Since Eddy3D #935 the node takes the same pedestrian sample -- same dictionary, same
cropped sheet -- and ships the raw surfaces (`postProcessing/pedestrianSurface/`),
`pedestrian/grid.json` and `geometry/<id>_terrain.stl`; `ped_field.py` on the master
turns them into `U.npz` and the `.wfld`. Archives from before it carry no field: see below.)

Full field data, last time step only, reconstructed on the client -- rank
counts differ per machine, so a decomposed result would be unusable anywhere
else. About 250-300 MB at the 3.0 m default; ~1.5 TB for 5,000 cases on the
master. `case_*/constant/polyMesh` is deliberately not in the archive (it is a
link to `mesh/`); relink it to open a case in ParaView. The broker's
`result_uri` is the archive's path on the machine that made it, and
`result_sha256`/`result_bytes` are the archive's.

### Shipped in parts (Eddy3D node)

An Eddy3D node no longer holds a case until its last direction is solved. On
2026-09-24 COD-359-38 was switched off with 7 of 32 directions of
v2-00e76e426bea6d52 solved, and all of them were lost with it. The node now
ships each piece of a case the moment it is done (Eddy3D `CaseParts`):

| when | file in `$WIND_DONE` |
| --- | --- |
| meshing passed | `<case>.mesh.tar.gz`: mesh, `cfg.json`, `spec.json`, site JSON, terrain sheet |
| a direction is finished | `<case>.case_NNN.tar.gz`: its latest time, `system/`, `postProcessing/`, logs |
| the case is done | `<case>.tar.gz`: `manifest.json` (its `parts` name every part with its sha256), the rest |

All of them unpack under the same `<case_id>/`, and unpacked together, the case
archive last, they are exactly the single archive above. The broker's
`result_uri` is still `<case>.tar.gz`. On the master:

```
uv run casebroker archives E:/wind/done               # every case: complete / waiting / partial
uv run casebroker archives E:/wind/done --state partial   # what stopped nodes left behind
uv run casebroker archives E:/wind/done --verify      # also hash every part against its manifest
```

- **complete**: the case archive and every part its manifest names;
- **waiting**: the case archive arrived before some of its parts (Syncthing does
  not deliver in write order);
- **partial**: parts and no case archive -- the node stopped, or is still solving.
  The mesh and the finished directions are here and unpack
  (`casebroker.archives.extract_case`); `scripts/backfill_pedestrian.py` takes
  any of a case's archives and unpacks them all.

Runner-script archives and nodes from before parts write only `<case>.tar.gz`,
which reads as complete, as before.

## The pedestrian field

U at 1.5 m and 1.75 m above grade, on a regular 2 m grid over the 1008 m core
(504 x 504 points), for every direction: `pedestrian/U.npz` (`U[direction,
height, y, x, (ux, uy, uz)]`, float32, NaN inside buildings, row 0 = south),
with `meta.json` saying which direction is which, the inlet reference speed at
each height, how much of the core is air, and how far each value's height above
grade is from its label.

**How it is made.** OpenFOAM cuts one `distanceSurface` per height over the
builder's terrain sheet cropped to the core (`runner/lib/ped_grid.py`), under
MPI on the still-decomposed case, and writes U on its vertices (cellPoint).
`runner/lib/ped_field.py` reads each surface onto the grid by linear
interpolation inside the surface's own triangles, so a building stays a hole
rather than being bridged over. Measured on v2-1410516cea4c5d7b (4.8M cells):
the cut takes 32 s on 8 ranks for both heights; the read is ~2 s a surface; every
value lands 1.75 m above the terrain to 1 mm median, 5.6 cm p99.

**Two things it is not**, both measured on that case:

- *Not the grid points sampled directly.* A `sets`/`probes` sample of 508,032
  points never finished -- 20 min serial, 3.5 min a rank on 8 ranks, an ordered
  (particle-tracked) set 12 min on 8, a surface of point-sized triangles 10 min
  on 8 -- because OpenFOAM 12 finds each point's cell with an octree search that
  is milliseconds a point on a snappyHexMesh mesh. Whatever samples many points
  has to be a surface, and has to run `-parallel`.
- *Not identical to a cellPoint probe at the same point.* A surface vertex sits
  on a cell edge and carries the point-interpolated value; a probe inside the
  cell also weights the cell-centre value. At 1.75 m, within the first cell
  layers, the two differ by 6.8% median (18% max, 25 points). The surface is
  what ParaView draws for the same slice; a cellPoint-exact grid would need the
  interpolation done outside OpenFOAM from the archived mesh and fields.

**Cases finished before it**: `scripts/backfill_pedestrian.py <archive> --out
<dir> --e3d <eddy3d-cli>` rebuilds the field from the archive alone -- mesh and
last time step, no re-solve. An archive without `terrain.stl` gets its sheet
regenerated with `eddy3d-cli site-geometry`, and the script refuses unless that
matches the archived geometry report on DEM source, extent, stride and z range.
~2 min a direction on 8 ranks, dominated by decomposePar.

**Seeing it**: the dashboard reads `<source>/<case_id>.wfld`. On the master,
`uv run python scripts/serve_fields.py <folder of .wfld>` serves them on
`http://localhost:8765` (read-only, CORS and Chrome's private-network header
set); put that URL in Settings -> Preferences -> Wind-field source. A bucket's
public URL works the same way once there is one. Or drag a `.wfld` onto an
opened case.

## Getting archives to the master

**Pairing is the broker's job now, and needs no admin rights on any machine.**
Syncthing only moves a file between two devices that know each other's device
ID, so every machine had to be paired by hand on both sides. On 2026-09-23 no
remote machine had ever been paired: every archive a remote node finished was
still on that node's own disk. Now:

1. **The master is named once**, in the dashboard (Settings, then Machines, then
   *Syncthing master*) or with `PUT /v1/syncthing {device_id, folder}`, admin
   session. The master node prints its device ID when it starts.
2. **Each E3D node pairs itself.** It runs Syncthing as the logged-in user. It
   adopts one already installed, or downloads the pinned release and checks its
   hash. It creates the send-only `wind-done` folder at its done directory,
   adds the master named by the broker, and reports its own device with every
   lease (`syncthing_id`).
3. **The master accepts exactly the devices the broker lists**
   (`GET /v1/syncthing`). The broker's credentials decide who may send the
   master anything.

Workers only dial out, so they need no firewall rule. A rule on the master
(which does need admin) makes transfers direct, and so faster; without one,
Syncthing still connects through NAT traversal or its relays.

The rest of this section covers what the node does, and the manual setup for a
runner that is not an E3D node.

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

**Keep it quiet.** A default Syncthing folder watches the filesystem, rescans
every hour, and keeps discovery/relay chatter going -- on a solving machine
that is constant background traffic for nothing. Since a case finishes at
one known moment, the runner announces it instead:

- on the client folder: *Watch for Changes* off, *Rescan Interval* 0
  (`fsWatcherEnabled=false`, `rescanIntervalS=0`), so Syncthing hashes and
  sends nothing on its own;
- in `machine.env`: `WIND_SYNCTHING_APIKEY` (Actions > Settings > General) and
  `WIND_SYNCTHING_FOLDER` (the folder ID); the runner then calls
  `POST /rest/db/scan?folder=<id>&sub=<case>.tar.gz` right after the archive
  is renamed into place, and only that file is hashed and transferred.

Unset, the runner does nothing and Syncthing behaves as configured. On a
machine that should be silent between cases you can also pause the folder
and have a cron unpause/pause it; the scan call is simpler and enough.

**The same variables drive the Windows node.** An Eddy3D node (`E3D node`,
`run-sim-node`) makes the same scan call right after its archive is renamed
into place -- from Eddy3D 7c4229cb (#937) on -- when `WIND_SYNCTHING_APIKEY`
and `WIND_SYNCTHING_FOLDER` (and `WIND_SYNCTHING_URL` if its GUI is not on
127.0.0.1:8384) are in the node's environment: `setx` them for the account
the node runs as, then restart the node. **Quiet mode with neither the
variables nor a build that has the call ships nothing, silently.** Until a
node runs such a build, give its folder a periodic rescan instead
(`rescanIntervalS` 900): the done folder holds only finished archives, never
a live case tree, so a rescan costs a directory listing.

What that cost, found 2026-09-23 on the master (COD-PKAST-7865): its
Syncthing had been down since 14 Sep -- the instances were started by a
logon trigger, and an RDP reconnect is not a logon -- no remote machine had
ever been paired with it, and its own worker folder was in quiet mode under
an Eddy3D node that never announced a file. The master held one 23 MB test
archive from 11 Sep and none of the campaign's results; every finished case
existed only on the disk of the machine that solved it.
`install_master_autostart.ps1` now also restarts both instances every 15
minutes if they are not running.

**Change a folder through the GUI or the REST API, never by rewriting
`config.xml`.** The same night, the 900 s rescan was set by loading the
worker's `config.xml` into an XML library and saving it back. The
pretty-printer turned every empty `<encryptionPassword></encryptionPassword>`
into one holding a newline and indentation. Syncthing takes that whitespace
as a real password, so it treated the master as an *untrusted, encrypted*
peer. Both instances then connected every 20 s and dropped within a second:
`remote expects to exchange plain data, but local data is encrypted` on one
side, `remote device missing in cluster config` on the other. The master
received nothing, and no error was visible outside the Syncthing log. Use
`PATCH /rest/config/folders/wind-done` (or the GUI), which validates the change
and writes the file itself. To repair a damaged file, `GET /rest/config`, blank
every whitespace-only string, `PUT` it back and restart.

**Checklist for a machine that solves cases** -- all four, or its results
stay on its own disk:

1. Syncthing running there, with the `wind-done` folder Send Only at its
   done directory and `.tmp` ignored;
2. the master's device ID added there AND that machine's device ID added on
   the master, with the folder shared to it (step 2-3 below);
3. the scan variables in the environment of whatever runs cases (or, for a
   node on an older build, a 900 s rescan);
4. proof, not configuration: `GET /rest/system/connections` there shows the
   master `"connected": true` for longer than a minute, and after an archive
   lands, `GET /rest/db/completion?folder=wind-done&device=<master id>` reaches
   100. A pairing that connects and drops every 20 s shows as *configured* in
   every listing, which is how it went unnoticed.

**Measured, 2026-09-12** (two Syncthing v2.1.5 instances, one standing in for a
remote worker, `C:\rc2\syncthing\`): a 23 MB case archive replicated
worker -> master over a direct QUIC connection with identical SHA-256 and
`needBytes 0`; a new 5 MB file then sat in `$WIND_DONE` for **45 s with
nothing transferred** (watcher off, `rescanIntervalS 0`), and appeared on the
master **2 s** after the runner's
`POST /rest/db/scan?folder=wind-done&sub=<case>.tar.gz`. No inbound port was
opened on either side.

### Setting it up on a worker

1. Run Syncthing (no admin needed: the release zip is a single binary,
   `syncthing --home=<dir> --gui-address=127.0.0.1:8384 --no-browser --no-upgrade`;
   v2 dropped `--no-default-folder`, so delete the auto-created folder).
2. Add the master's device ID; the master adds the worker's.
3. Create folder id **`wind-done`** on both -- worker: path `$WIND_DONE`,
   `type sendonly`, `fsWatcherEnabled false`, `rescanIntervalS 0`; master:
   its aggregation directory, `type receiveonly`, `ignoreDelete true`. Ignore
   pattern `.tmp` on both. One folder id is shared by EVERY worker: case ids
   are unique, so many send-only workers accumulate into one receive-only
   master directory with no collisions and no per-worker folder admin.
4. Put `WIND_SYNCTHING_URL/APIKEY/FOLDER` in `machine.env`.

`C:\rc2\syncthing\configure.ps1` does steps 2-3 over the REST API and is the
reference for the exact field values.

**PACE: the master pulls.** A daemon does not fit shared login nodes or
job-lifetime compute nodes, and compute nodes cannot be reached from outside
anyway. The master already has SSH to the login nodes, so
`scripts/pull_done.sh <master done dir> ice:<WIND_DONE> phoenix:<WIND_DONE>`
on a timer collects finished archives; `PULL_REMOVE=1` deletes on the cluster
after a size-verified copy, which is what keeps ICE's 300 GB scratch from
filling with results.

## What the geometry step adds beyond buildings and terrain

`real_cities/site_geometry.py` writes, next to the two STLs, a site report the
runner reads into `build-case`:

- `z0_by_direction` -- the inlet roughness per wind direction
  (`upstream_z0.py`: ESA WorldCover, log-mean z0 of a 3 km upwind sector
  beyond the domain edge). Goes to `wind.roughnessByDirection`; `roughness`
  0.5 is the fallback for a site without a WorldCover tile.
- `<case>_canopy.stl` + `vegetation` -- tree crown volumes from the Meta/WRI
  1 m canopy height model (`canopy_zones.py`: crown = upper 59 % of the tree,
  4 m columns, one closed shell) and the LAD/Cd class (a latitude-band
  default from Eddy3D's vegetation library; recorded as such). Goes to
  `geometry.canopyStl` + `vegetation`; `build-case` writes a `topoSetDict`
  (mesh case) and a `porosityForce` on the `canopy` cellZone with
  `f = 2·Cd·LAD` (direction cases); the runner runs `topoSet` after
  reconstructing the mesh and prints `CANOPY ... now: N cells` in `run.log`.
  A treeless site has no STL and no zone.

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

## Reproducing one case on another machine

When a case is quarantined and the machine that failed it is out of reach, run
the SAME case through the whole node pipeline somewhere else. Reasoning from
the broker's excerpt is how `v2-00697fb4542aa4c6` (2026-09-22) collected two
wrong diagnoses -- a skewed mesh, then potentialFoam -- before a reproduction
showed 68 cells sealed off from the flow, which checkMesh only stars as
`*Number of regions: 69` without failing the mesh.

The case id is derived from the coordinates and recipe, so a throwaway broker
holding just that case hands the node exactly what production did:

```bash
# 1. a broker on SQLite, reachable only from this machine
CASEBROKER_DB=E:/wind/repro/broker.sqlite CASEBROKER_WRITE_TOKENS=repro-local-token \
  uv run uvicorn casebroker.app:app --host 127.0.0.1 --port 8799 &

# 2. the production case's own spec, with max_attempts 1 (one failure is the answer)
curl -s -H "Authorization: Bearer $PROD_TOKEN" \
  https://casebroker.onrender.com/v1/cases/<case_id> > case.json
python - <<'EOF'
import json
d = json.load(open("case.json"))
s = json.loads(d["spec"]) if isinstance(d["spec"], str) else d["spec"]
json.dump([dict(lat=s["lat"], lon=s["lon"], recipe=d["recipe"], city_cluster=d["city_cluster"],
                lcz=d["lcz"], spec=s, max_attempts=1)], open("case_in.json", "w"))
EOF
curl -s -X POST -H "Authorization: Bearer repro-local-token" -H 'Content-Type: application/json' \
  --data @case_in.json http://127.0.0.1:8799/v1/cases

# 3. a node credential for that broker in its OWN node dir -- never the
#    machine's real one, which a live node on the same box is using
mkdir -p E:/wind/repro/node
echo '{"broker": "http://127.0.0.1:8799", "name": "repro", "token": "repro-local-token", "paired_at": "2026-01-01T00:00:00+00:00"}' \
  > E:/wind/repro/node/credential.json

# 4. one case, then exit. Separate work and done folders: done must NOT be the
#    Syncthing folder, or a reproduction's archive reaches the master.
EDDY3D_NODE_DIR='E:\wind\repro\node' E3D.exe run-simulation-node \
  --work 'E:\wind\repro\work' --done 'E:\wind\repro\done' \
  --cpus 12 --engine bluecfd --max-cases 1 --drain --max-idle-polls 1
```

Match the failing worker's engine, and leave alone the cores a live node on
the same machine is using. The node reports progress to the broker, not to its
own log: read `last_progress` from the local broker, and the step logs under
`<work>/cases/<id>/<id>/{mesh,case_NNN}/`. To try a new build on the same
case, `POST /v1/cases/reopen?case_id=<id>&dry_run=false` on the local broker
and start the node from a FRESH `--work`: a scratch holding a finished mesh is
resumed, not re-meshed, so a meshing fix would never run.

Before reasoning from a difference between the reproduction and production,
check that both meshes finished: `Finished meshing` in `03_snappyHexMesh.txt`,
and similar checkMesh numbers. The first reproduction of that case checked at
skewness 1.28 against production's 13.98 and looked like a machine-dependent
mesher; snappy had died mid-snap and the step had passed the castellated mesh
on as finished (Eddy3D builds from `8b416080` on fail that step instead).

Once it reproduces, read the evidence before the error text: the region count
in `06_checkMesh.txt`, and the frames under `sigFpeHandler` in the backtrace.
`DICPreconditioner::calcReciprocalD` there is a zero pivot -- a cell or region
with no connection to anything that fixes the pressure -- not a divergence,
and no numerics setting reaches it.

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
