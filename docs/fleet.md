# Hooking a machine into the campaign

How any Linux or Windows machine becomes a simulation node that pulls cases from
the broker, solves them, reports progress, survives being stopped, and gets its
finished cases to where they are kept. A node is `E3D.exe` -- the Eddy3D CLI, one
self-contained file -- and nothing else; its side of the contract is Eddy3D's
`docs/SIMULATION_NODE.md`, the broker's is [protocol.md](protocol.md). For the
cluster-specific traps (SSH, quotas, Podman, billing) see
[`pace-hpc.md`](pace-hpc.md).

(Until 2026-10 a second kind of worker existed: `casebroker.worker` driving
`runner/run_case.sh`, with `machine.env`, `start_worker.sh`/`.ps1` and
`scripts/pull_done.sh`. Its runner could not build any campaign recipe -- it
handed every case back -- and it is retired. Git history has it.)

## The shape

```
            pair once; then lease / heartbeat (progress, stage) / telemetry /
            parts as they finish / complete
   E3D node <-------------------------------------------------------->  broker
     |                                                            (self-hosted,
     |  --engine docker (Docker Desktop, WSL; podman on PACE)      part store)
     |           bluecfd (native Windows, OpenFOAM-12 + MS-MPI)
     v
   site geometry -> build-case -> mesh -> solve (per direction) -> gate -> archive
                                   |              |                        |
                          mesh part -> broker   each direction's part     <case>.tar.gz
                                                and its 1.75 m field      -> --done and
                                                -> broker                 the broker
   SIGTERM / Ctrl-C: the case is released (attempt refunded); its scratch (--work)
   stays, and the next lease either gives it back to this node or another node
   continues it from the broker's mesh and finished directions.
```

## Setting up a machine

1. **The binary**: `gh release download e3d-node-latest -R Eddy3D-Dev/Eddy3D -p E3D.exe`
   (`E3D-linux-x64` on Linux), checked against `release.json` in the same
   release. One OpenFOAM runtime: Docker (Desktop or WSL), blueCFD-Core 2024 on
   Windows, Podman on PACE.
2. **Pair it**: `E3D setup-sim-node <broker-url>` shows a code; approve it on the
   dashboard (**Settings ▸ Machines**). The node generates its own credential;
   the broker keeps only its hash. Its name is its worker id, stable **per
   machine** -- it is how a restarted node gets its own case back; never reuse
   one across machines. A cluster pairs once as itself (`--name ice`) and its
   jobs lease as `ice-<job>`.
3. **Run it**: `E3D node --done <folder> --cpus <N> [--engine bluecfd --bluecfd-dir ...]`.
   `node` is the supervisor: it updates the node when the broker names a new
   build ([releases.md](releases.md)); `run-sim-node` runs the same loop without
   it. On PACE the sbatch scripts under `slurm/` do this inside a job.
4. **Optional, sharing a big case out** (protocol 2): `--max-directions N` hands
   the case on after N directions, `--chunk-hours H` at the first direction
   boundary after H hours -- a small box contributes directions without holding
   an 800-core-hour case for a week ([protocol.md](protocol.md#protocol-2-capabilities-machines-stages-handoffs)).

That is the whole procedure. Everything below is what the pieces do.

## Progress on the dashboard

Every heartbeat (the first at once, then every 5 min) carries the node's
current line -- `mesh 3/5 · 03_snappyHexMesh`, `solve 3/32 dirs · iter 412/2000`
-- and, from a protocol-2 node, the stage it belongs to. The broker keeps each
change of line, so a case page shows how long each stage took and where it is
now. Alongside, the node posts structured telemetry (`site`, `mesh`, `solve`, and
each direction's `residuals` curve: protocol.md, "Telemetry and the dataset"),
and the machine it runs on (`cpus`, `mem_gb`) with every lease.

## Stopping and resuming

`SIGTERM`/`SIGINT` -- SLURM walltime, `embers` preemption, Ctrl-C -- is the
normal way a solve stops, not an error. The node stops the solver and releases
the case; the attempt is refunded. Its scratch (`--work`) keeps the mesh and
every direction it reached.

On its next lease the node lists the cases it holds scratch for as
`resume_case_ids`. The broker claims those **first**, and a case still leased to
the same worker id comes back **without spending an attempt** -- continuing, not
retrying -- and the node skips what it already finished.

If another node is handed the case first, it continues from the broker's copy:
the mesh and each finished direction were uploaded as they finished, so it
fetches the mesh and solves only the directions that are missing. Rules that
follow:

- **A direction in flight never moves.** A case moves between machines only at
  a direction boundary, through the broker's part store; what a stopped node
  loses is the direction it was solving.
- **A failed case goes to another machine first.** For
  `CASEBROKER_FAIL_COOLDOWN` (12 h) no worker on the failing host is handed it
  again (DOMAIN.md, "Three chances means three machines").

## What a finished case is

One `tar.gz` per case in the node's `--done` folder, written under `.tmp/` and renamed into
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

(That listing is the retired runner's layout; a node's archive is laid out as
`<id>/<id>/case_*` with `geometry/`, and ships the raw pedestrian surfaces
(`postProcessing/pedestrianSurface/`), `pedestrian/grid.json` and
`geometry/<id>_terrain.stl`. The field itself goes to the broker as each direction
finishes: see "The pedestrian field".)

Full field data, last time step only, reconstructed on the client -- rank
counts differ per machine, so a decomposed result would be unusable anywhere
else. About 250-300 MB at the 3.0 m default; ~1.5 TB for 5,000 cases. `case_*/constant/polyMesh` is deliberately not in the archive (it is a
link to `mesh/`); relink it to open a case in ParaView. The broker's
`result_uri` is the archive's path on the machine that made it, and
`result_sha256`/`result_bytes` are the archive's.

### Shipped in parts (Eddy3D node)

An Eddy3D node no longer holds a case until its last direction is solved. On
2026-09-24 COD-359-38 was switched off with 7 of 32 directions of
v2-00e76e426bea6d52 solved, and all of them were lost with it. The node now
ships each piece of a case the moment it is done (Eddy3D `CaseParts`):

| when | file in `--done` |
| --- | --- |
| meshing passed | `<case>.mesh.tar.gz`: mesh, `cfg.json`, `spec.json`, site JSON, terrain sheet |
| a direction is finished | `<case>.case_NNN.tar.gz`: its latest time, `system/`, `postProcessing/`, logs |
| the case is done | `<case>.tar.gz`: `manifest.json` (its `parts` name every part with its sha256), the rest |

All of them unpack under the same `<case_id>/`, and unpacked together, the case
archive last, they are exactly the single archive above. The broker's
`result_uri` is still `<case>.tar.gz`. On a folder of them -- a node's done
folder, or a copy pulled from one:

```
uv run casebroker archives E:/wind/done               # every case: complete / waiting / partial
uv run casebroker archives E:/wind/done --state partial   # what stopped nodes left behind
uv run casebroker archives E:/wind/done --verify      # also hash every part against its manifest
```

- **complete**: the case archive and every part its manifest names;
- **waiting**: the case archive arrived before some of its parts (a copy need not
  arrive in write order);
- **partial**: parts and no case archive -- the node stopped, or is still solving.
  The mesh and the finished directions are here and unpack
  (`casebroker.archives.extract_case`).

Archives from the retired runner and from nodes before parts are only
`<case>.tar.gz`, which reads as complete, as before.

### A case outlives its machine

Each part is also reported to the broker (`POST /v1/parts`, lease-scoped like
telemetry): the mesh with its sha256, each direction with the sha256 of the mesh
it was solved on and its convergence verdict. The broker keeps them in
`case_parts` and hands them out with the next lease of the case, so the node that
takes it over continues on the **same** mesh -- fetched from the broker's part
store (below) -- and solves only the directions not listed. One case is never answered on two
meshes:

- a direction reported against a mesh that is no longer the case's is refused (409);
- a NEW mesh for a case (the site changed, or an admin reset it) drops every
  part of the old one, with a `parts_reset` event.

A node leases with `can_continue_from_broker: true` when it can fetch a mesh from
the broker, and the broker hands a case that has a mesh on record only to such a
node, and only while its part store holds that mesh. A case whose mesh it never
got -- shipped before the store existed, or refused by a full one -- waits for the
node that made it, which resumes it from its own disk. If that node is gone for
good:

```
uv run casebroker parts reset <case_id> --broker <broker url>
```

and the next node meshes it afresh. `GET /v1/cases/<case_id>/parts` shows what a
case has on record.

## The pedestrian field

**Since 0.24.0 the field is also in the broker's own database.** The native node reads
each direction's surface the moment the direction is solved (`MetaFOAM.Deploy.PedestrianField`,
a port of `ped_field.py`) and `PUT`s |U| at 1.75 m as a `umag/1` blob to
`/v1/cases/<id>/fields/<direction>` under its lease; the dashboard reads it back from
there, exact, with no field source and no master in the way. A case finished by a node
from before this has no field at the broker until the node that holds its archive
backfills it (`PUT /v1/cases/<id>/fields/<dir>/backfill`; the node does it when it sweeps
its done folder, asking `POST /v1/parts/wanted` which fields the broker lacks). `GET /v1/cases/<id>/fields` lists what the broker holds;
`metrics.pedestrian.fields` on the completion says what the node managed to send.
**Since 0.28.0 the broker stores it uncompressed**, whichever way the node sent it: gzip
saved 13% of a float32 field and Postgres's own compression nothing, while the plain
container reads in place (`numpy.frombuffer`, or one cell with SQL `substring()`; the
column is `STORAGE EXTERNAL`). Rows stored before are gzip-wrapped; the first two bytes
(`1f 8b`) say so, and the dashboard reads both. **Since 0.30.0 each listed field carries
`lambda_f`**, the frontal area index of the direction it was solved for, read off the
site report the node sent (protocol.md, "Telemetry and the dataset").

U at 1.5 m and 1.75 m above grade, on a regular 2 m grid over the 1008 m core
(504 x 504 points), for every direction: `pedestrian/U.npz` (`U[direction,
height, y, x, (ux, uy, uz)]`, float32, NaN inside buildings, row 0 = south),
with `meta.json` saying which direction is which, the inlet reference speed at
each height, how much of the core is air, and how far each value's height above
grade is from its label.

**How it is made.** OpenFOAM cuts one `distanceSurface` per height over the
builder's terrain sheet cropped to the core, under MPI on the still-decomposed
case, and writes U on its vertices (cellPoint). The node reads each surface onto
the grid by linear interpolation inside the surface's own triangles
(`MetaFOAM.Deploy.PedestrianField`, a port of the retired runner's `ped_field.py`), so a building stays a hole
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

**Seeing it**: the dashboard reads `<source>/<case_id>.wfld`. On the master,
`uv run python scripts/serve_fields.py <folder of .wfld>` serves them on
`http://localhost:8765` (read-only, CORS and Chrome's private-network header
set); put that URL in Settings -> Preferences -> Wind-field source. A bucket's
public URL works the same way once there is one. Or drag a `.wfld` onto an
opened case.

## Where a finished case goes

**An Eddy3D node sends every part to the broker** as it ships it -- the mesh, each
finished direction, the case archive once `/v1/complete` has taken it -- in
resumable chunks, into the broker's part store (docs/protocol.md, "Parts the broker
holds"; on the broker, `CASEBROKER_PARTS_DIR`). The files also stay in the node's
done folder. A broker without a store, or with a full one, leaves them there only:
enable the store before relying on it. `GET /v1/cases/<case_id>/blobs` shows what the
broker holds of a case, and its archive receipt at location `broker` says the whole
case is there.

**Syncthing is gone (2026-10-06).** Archives used to travel from every node to one
master over Syncthing, paired through the broker (`/v1/syncthing`). That endpoint,
the dashboard's *Syncthing master* panel and the `syncthing_id` a node sent with its
leases are removed; a node from before still leases (the field is ignored, and the
404 reads as "no master"). A Syncthing that an earlier node build started keeps
running until it is stopped; what it already delivered stays where it is.

**PACE.** An E3D node in a job uploads its parts itself and waits up to 20 min
for them when it drains; its `--done` is on persistent scratch, so a part that
had not gone up when the job ended is sent by the next job's sweep.

## What the geometry step adds beyond buildings and terrain

The node's site build (its native port of `real_cities/site_geometry.py`)
writes, next to the two STLs, a site report that goes into `build-case`:

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
  `f = 2·Cd·LAD` (direction cases), and `topoSet` selects the zone's cells after
  the mesh is reconstructed.
  A treeless site has no STL and no zone.

## Ranks per machine

Different machines have different knees. `C:\rc2\scaling_test.sh` (local) and
the `scaling-sweep` job (PACE) run the same 3.9M-cell case for a short bounded
number of iterations at several rank counts and record iterations/hour and
per-rank efficiency; the knee -- where adding ranks stops paying -- is the
machine's `--cpus`. Results so far live in `scaling_results.csv` on each
machine. Known ceilings that no sweep will move: **24 ranks per job on PACE**
(a scheduler policy, not hardware -- `-N 1 -n 28` is refused outright), and on
Windows the count MS-MPI's `mpiexec -n` will launch on one box.

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
  https://casebroker.eddy3d.com/v1/cases/<case_id> > case.json
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

# 4. one case, then exit. Separate work and done folders, so nothing of the
#    reproduction mixes with what a live node on this machine ships.
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
- ICE/Phoenix `$HOME` is 20-30 GB and usually nearly full -- scratch and
  archives must go to scratch (`--work` and `--done` in the sbatch files),
  and a 100%-full scratch silently loses rank data during the copy-back.
- A worker shown as **Drained**, with a reason that starts `drained by the
  broker:`, failed five different cases within ten minutes. That points at the
  machine, not the sites: a full disk (COD-358-21, 2026-09-26, 663 cases in 38
  minutes), a stopped container daemon, or leftover processes. Fix the machine
  and undrain it. Then give back the attempts it charged with
  `POST /v1/cases/reopen?error_contains=<its error>&include_pending=true`: look
  at the dry run first, then repeat the call with `dry_run=false`.
