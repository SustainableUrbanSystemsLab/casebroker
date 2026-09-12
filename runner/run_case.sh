#!/bin/bash
# Per-case runner: the seam between the broker and the CFD.
#
# The worker invokes this once per leased case. The case spec arrives as JSON on
# stdin and in $CASE_SPEC; the last line of stdout must be a JSON object carrying
# at least "result_uri".
#
# Exit codes are a contract with the broker:
#   0   success, result reported
#   64  THIS CASE IS BROKEN and must never be retried (bad geometry, degenerate
#       tile). Quarantined on the first attempt instead of burning three.
#   any other non-zero: retryable (node died, image pull failed, transient I/O).
#
# Everything runs on node-local scratch. OpenFOAM writes thousands of small files
# per rank, which is the worst access pattern Lustre has. What leaves the node is
# ONE archive per case -- the reconstructed last time step, the mesh, dictionaries,
# logs and samples, ~250-300 MB at the 3.0 m default -- written to $WIND_DONE for
# Syncthing (workstations) or a master-side pull over SSH (PACE) to collect. See
# docs/fleet.md.
#
# Three OpenFOAM runtimes, one runner (WIND_RUNTIME=auto picks the first found):
#   podman   rootless, PACE ICE/Phoenix (no subuid range, handled below)
#   docker   Docker Desktop / WSL, the lab workstations
#   native   blueCFD-Core 2024 on Windows: OpenFOAM-12 + MS-MPI, no container
# The solve itself is the same inner script in all three; only how it is launched
# and how MPI is spelled differ.
#
# Checkpoint / resume, same machine only: SIGTERM (SLURM walltime, embers
# preemption, Ctrl-C) saves the study to $WIND_CASES/<case> and leaves a
# resume.json the worker hands to the broker on its next lease, so the solve
# continues from its last written time step instead of restarting. This needs a
# writeInterval that actually writes -- the default used to equal the iteration
# budget, i.e. a single write at the very end and nothing to resume from.

set -uo pipefail

CASE_ID="${CASE_ID:?CASE_ID not set by the worker}"
SPEC="${CASE_SPEC:-$(cat)}"
NP="${WIND_NP:-24}"
WC="${WIND_ROOT:?WIND_ROOT (shared storage root) not set}"
IMG="${WIND_IMAGE:-docker.io/dicehub/openfoam:12}"
CLI="${EDDY3D_CLI:?EDDY3D_CLI (path to eddy3d-cli) not set}"
RUNTIME="${WIND_RUNTIME:-auto}"
DONE_DIR="${WIND_DONE:-$WC/done}"
CASES_DIR="${WIND_CASES:-$WC/cases}"
PROGRESS_FILE="${CASEBROKER_PROGRESS_FILE:-}"
BLUECFD_HOME="${BLUECFD_HOME:-/c/blueCFD-Core-2024}"
MSMPI_BIN="${MSMPI_BIN:-/c/Program Files/Microsoft MPI/Bin}"
export WIND_WRITE_INTERVAL="${WIND_WRITE_INTERVAL:-200}"   # read by the config generator below
# The geometry builder lives in the parent repo beside this submodule.
REAL_CITIES="${REAL_CITIES:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../real_cities" 2>/dev/null && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEO_REPORT=""   # set when this run builds its own geometry; empty means zGround falls back to 0
PSTORE=""       # podman's image store, only ever set on the podman path
ENGINE_PID=""; PROGRESS_PID=""

# python3 on the clusters, python on the Windows workstation. Resolving it once
# here rather than hardcoding python3 keeps the runner usable for a local dry run,
# which is the only way to exercise it without a scheduler.
PY=$(command -v python3 || command -v python) || { echo "no python on PATH" >&2; exit 1; }

log() { echo "[$CASE_ID] $*" >&2; }
fatal_case() { log "FATAL (will not retry): $*"; exit 64; }
# Exit 64 is reserved for "this SITE is broken" -- degenerate geometry that will
# fail identically on every machine forever. A missing input file is not that: it
# says the tooling or the staging is not ready, and the site itself is fine. The
# distinction is expensive to get wrong in one direction only. A retryable error
# costs at most three attempts; a wrongly fatal one silently removes a site from
# the campaign with no way back short of editing the database -- which is exactly
# what happened on the first ICE run, where a missing STL quarantined 173 perfectly
# good sites in under a minute before anyone could stop it.
retry_case() { log "RETRYABLE: $*"; exit 1; }

SCRATCH="${TMPDIR:-/tmp}/wind-$CASE_ID-$$"
mkdir -p "$SCRATCH" || exit 1

# On ANY failure, the diagnostic logs that would explain it are the very
# thing about to be deleted: $SCRATCH lives on node-local /tmp, and the trap
# below runs unconditionally at exit. A gate that correctly CATCHES a
# problem but leaves nothing to diagnose it with is only half "bulletproof"
# -- found the hard way debugging a real SOLVE_FAIL that had already been
# cleaned up by the time its cause could be inspected. Preserved to shared
# storage (never counted as a case result -- a separate top-level directory
# from $WC/results, so a failed run can never be mistaken for a done one),
# and best-effort: a failure while preserving failure diagnostics must not
# mask the original error or itself abort the script.
cleanup() {
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        local fail_out="$WC/failed_logs/$CASE_ID"
        mkdir -p "$fail_out" 2>/dev/null && {
            cp "$SCRATCH"/run.log "$SCRATCH"/build.json "$SCRATCH"/cfg.json "$fail_out/" 2>/dev/null
            # --parents keeps each direction's own 11.log/12.log/fr.log distinct
            # (case_000/12.log, case_045/12.log, ...) -- a flat copy would let
            # every direction after the first silently overwrite the one
            # before it, which for a multi-direction case throws away every
            # log but the last.
            (cd "$SCRATCH" 2>/dev/null && find . -maxdepth 5 -name "*.log" \
                -exec cp --parents {} "$fail_out/" \; 2>/dev/null)
            echo "exit $rc" > "$fail_out/exit_code.txt" 2>/dev/null
        }
    fi
    # ${PSTORE:-} because PSTORE is not assigned until the podman section far
    # below: any failure BEFORE that -- a missing STL, a bad spec -- runs this
    # trap with it still unset, and under `set -u` the cleanup handler then dies
    # itself, on the way out of the error it was written to report.
    rm -rf "$SCRATCH" "${PSTORE:-}" 2>/dev/null
}
trap cleanup EXIT

# ── 0. runtime, checkpoint, progress ─────────────────────────────────────────
detect_runtime() {
    case "$RUNTIME" in
        podman|docker|native) ;;
        auto)
            if command -v podman >/dev/null 2>&1; then RUNTIME=podman
            elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then RUNTIME=docker
            elif [ -x "$BLUECFD_HOME/OpenFOAM-12/platforms/mingw_w64Gcc122DPInt32Opt/bin/foamRun.exe" ]; then RUNTIME=native
            else retry_case "no OpenFOAM runtime found: no podman, no running docker, no blueCFD at $BLUECFD_HOME"
            fi ;;
        *) retry_case "unknown WIND_RUNTIME '$RUNTIME' (podman|docker|native|auto)" ;;
    esac
    log "runtime: $RUNTIME, $NP ranks"
}

# A host path as the container engine wants it. Docker Desktop on Windows needs
# C:\... for -v; MSYS bash would otherwise hand it /c/... and get an empty mount.
hostpath() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) cygpath -w "$1" ;;
        *) printf '%s' "$1" ;;
    esac
}

# blueCFD-Core's own setvars_OF12.bat, translated. Sourcing OpenFOAM's bashrc
# inside its MSYS2 does NOT work (empty WM_PROJECT_DIR, no foamRun on PATH); the
# batch file is the real environment, and MS-MPI's mpiexec is not under blueCFD
# at all but in Program Files -- measured on the lab workstation, 2026-09-11.
bluecfd_env() {
    local b="$BLUECFD_HOME" opt=mingw_w64Gcc122DPInt32Opt bw
    bw=$(cygpath -w "$b" 2>/dev/null || printf '%s' "$b")
    export WM_PROJECT=OpenFOAM WM_PROJECT_VERSION=12 WM_OPTIONS=$opt WM_MPLIB=MSMPI101 \
           FOAM_MPI=MS-MPI-10.1 FOAM_SIGFPE=1 MPI_BUFFER_SIZE=20000000
    export WM_PROJECT_DIR="$bw\\OpenFOAM-12" FOAM_ETC="$bw\\OpenFOAM-12\\etc"
    export PATH="$b/OpenFOAM-12/platforms/$opt/bin:$b/OpenFOAM-12/bin:$b/ThirdParty-12/platforms/mingw_w64x86_64-w64-mingw32/gcc-12.2.0/bin:$b/msys64/mingw64/bin:$b/OpenFOAM-12/platforms/$opt/lib/MS-MPI-10.1:$b/ThirdParty-12/platforms/mingw_w64Gcc122DPInt32/lib/MS-MPI-10.1:$b/ThirdParty-12/platforms/mingw_w64Gcc122DPInt32/lib:$b/OpenFOAM-12/platforms/$opt/lib:$b/OpenFOAM-12/platforms/$opt/lib/dummy:$MSMPI_BIN:$PATH"
}

# Run the inner script under the chosen runtime. Inside a container the scratch
# dir is /s; natively it is itself -- the inner script takes that root as its
# first argument and never hardcodes either.
engine_run() {
    case "$RUNTIME" in
        podman)
            # The image's ENTRYPOINT is "/bin/bash -i -c", which consumes only the
            # FIRST word of whatever follows -- pass the whole command as one
            # string or it silently runs bare "bash" and exits 0 in seconds.
            $POD run --rm --user 0:0 -e HOME=/home/openfoam -e WIND_MPIRUN -e WIND_ALLOW_UNCONVERGED \
                -v "$SCRATCH:/s" "$IMG" "bash /s/inner.sh /s $*" ;;
        docker)
            MSYS_NO_PATHCONV=1 docker run --rm --user 0:0 -e HOME=/home/openfoam -e WIND_MPIRUN \
                -e WIND_ALLOW_UNCONVERGED \
                -v "$(hostpath "$SCRATCH"):/s" --entrypoint bash "$IMG" /s/inner.sh /s "$@" ;;
        native)
            ( bluecfd_env; export WIND_NATIVE=1 WIND_ALLOW_UNCONVERGED="${WIND_ALLOW_UNCONVERGED:-0}"
              bash "$SCRATCH/inner.sh" "$SCRATCH" "$@" ) ;;
    esac
}

CKPT="$CASES_DIR/$CASE_ID"
save_checkpoint() {
    mkdir -p "$CKPT" || return 1
    # The whole study, minus the container store and this run's own log. tar
    # rather than rsync: git-bash has no rsync, and a pipe needs nothing but tar.
    (cd "$SCRATCH" && tar -cf - --exclude=./pstore --exclude=./xdg --exclude=./run.log .) \
        | (cd "$CKPT" && tar -xf -) || return 1
    printf '{"case_id": "%s", "worker_id": "%s", "ranks": %s, "runtime": "%s", "saved_at": %s}\n' \
        "$CASE_ID" "${CASEBROKER_WORKER_ID:-}" "$NP" "$RUNTIME" "$(date +%s)" > "$CKPT/resume.json"
}

# SIGTERM is the NORMAL way a solve stops on PACE (walltime, embers preemption)
# and how a person stops a workstation worker (Ctrl-C). Stop the solver, keep
# its last written time step, and leave a marker the worker will find.
on_term() {
    trap - TERM INT
    log "TERM/INT: stopping the solver and saving a checkpoint"
    [ -n "$PROGRESS_PID" ] && kill "$PROGRESS_PID" 2>/dev/null
    if [ -n "$ENGINE_PID" ]; then
        kill -TERM "$ENGINE_PID" 2>/dev/null
        [ "$RUNTIME" = native ] && { taskkill //F //IM foamRun.exe >/dev/null 2>&1 || pkill -f foamRun 2>/dev/null; }
        wait "$ENGINE_PID" 2>/dev/null
    fi
    if ls "$SCRATCH/$CASE_ID"/case_*/processor0/[1-9]* >/dev/null 2>&1; then
        save_checkpoint && log "checkpoint saved to $CKPT" || log "checkpoint save FAILED"
    fi
    exit 143
}
trap on_term TERM INT

# Every 60 s, one line about the direction currently solving, for the worker's
# heartbeat. Written atomically so the reader never sees a half line.
start_progress_writer() {
    [ -n "$PROGRESS_FILE" ] || return 0
    (
        t0=$(date +%s)
        while sleep 60; do
            latest=$(ls -t "$SCRATCH/$CASE_ID"/case_*/12.log 2>/dev/null | head -1)
            [ -n "$latest" ] || continue
            dir=$(basename "$(dirname "$latest")")
            ndone=$(grep -ls "SIMPLE solution converged" "$SCRATCH/$CASE_ID"/case_*/12.log 2>/dev/null | wc -l | tr -d ' ')
            ntot=$(ls -d "$SCRATCH/$CASE_ID"/case_* 2>/dev/null | wc -l | tr -d ' ')
            line=$("$PY" "$SCRIPT_DIR/lib/progress.py" "$latest" "$dir [$ndone/$ntot dirs]" "$(( $(date +%s) - t0 ))" 2>/dev/null)
            [ -n "$line" ] || continue
            printf '%s\n' "$line" > "$PROGRESS_FILE.tmp" && mv -f "$PROGRESS_FILE.tmp" "$PROGRESS_FILE"
        done
    ) &
    PROGRESS_PID=$!
}

detect_runtime

RESUMING=0
if [ -f "$CKPT/resume.json" ] && [ -d "$CKPT/$CASE_ID" ]; then
    if cp -r "$CKPT"/. "$SCRATCH"/ 2>/dev/null; then
        RESUMING=1
        log "resuming from checkpoint $CKPT ($(cat "$CKPT/resume.json"))"
    else
        log "checkpoint at $CKPT could not be restored; starting fresh"
    fi
fi

# ── 1. geometry ───────────────────────────────────────────────────────────────
# The spec names STLs already staged on shared storage by the tile pipeline.
BUILDINGS=$(printf '%s' "$SPEC" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("buildings_stl",""))')
TERRAIN=$(printf '%s' "$SPEC" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("terrain_stl",""))')
# No STL in the spec means nobody has built this site's geometry yet, which for
# the v2 campaign is every case: the sampler publishes coordinates, not meshes.
# Build it here rather than pre-staging 5,000 tiles, because a worker already
# knows the one site it needs, compute nodes have outbound internet, and the two
# sources involved (Overture over S3, GEDTM30 as a COG) are windowed reads with
# no shared rate limit -- so this parallelises with the fleet instead of
# serialising behind a staging job. Cached under $WC/geometry, so a case that is
# preempted and re-leased does not re-download.
if [ -z "$BUILDINGS" ] || [ -z "$TERRAIN" ]; then
    LAT=$(printf '%s' "$SPEC" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["lat"])')
    LON=$(printf '%s' "$SPEC" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["lon"])')
    GEO="$WC/geometry/$CASE_ID"
    GEO_REPORT="$GEO/$CASE_ID.json"
    BUILDINGS="$GEO/${CASE_ID}_buildings.stl"
    TERRAIN="$GEO/${CASE_ID}_terrain.stl"
    if [ ! -f "$BUILDINGS" ] || [ ! -f "$TERRAIN" ]; then
        log "building geometry for $LAT,$LON"
        mkdir -p "$GEO"
        # Exit 3 is site_geometry's "no usable building here" -- a real property
        # of the site, not a transient fault, so that one IS fatal. Anything else
        # (a DTM host down, an Overture timeout) is retryable.
        # uv run --project, not $PY: site_geometry needs numpy, rasterio,
        # trimesh and manifold3d, which live in the real_cities project venv.
        # $PY is a bare interpreter resolved for parsing the spec -- it has none
        # of them, and the failure surfaces as ModuleNotFoundError on the first
        # import, identically for every case.
        (cd "$REAL_CITIES" && uv run --project . python site_geometry.py               --site "$CASE_ID" --lat "$LAT" --lon "$LON" --out "$GEO")               >"$GEO/geometry.log" 2>&1
        grc=$?
        [ $grc -eq 3 ] && fatal_case "site has no usable buildings (see geometry.log)"
        [ $grc -ne 0 ] && retry_case "geometry build failed rc=$grc (see $GEO/geometry.log)"
    fi
fi
[ -f "$BUILDINGS" ] || retry_case "buildings STL not found: $BUILDINGS"
[ -f "$TERRAIN" ]   || retry_case "terrain STL not found: $TERRAIN"
cp "$BUILDINGS" "$SCRATCH/buildings.stl"
cp "$TERRAIN"   "$SCRATCH/terrain.stl"

# Pedestrian-height reference for the flow-field screenshot below: the
# terrain's own highest point, not the domain floor -- the domain typically
# extends well BELOW the terrain (so blockMesh's floor seals under it), and
# slicing at domain_min_z + 1.5 would sample INSIDE solid ground on any case
# where the two differ, which is the common case, not the exception.
TERRAIN_Z=$("$PY" - "$SCRATCH/terrain.stl" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read()
zmax = zmin = None
is_binary = len(data) >= 84
if is_binary:
    n = struct.unpack_from("<I", data, 80)[0]
    is_binary = len(data) == 84 + 50 * n
if is_binary:
    off = 84
    for _ in range(n):
        v = struct.unpack_from("<9f", data, off + 12)
        hi, lo = max(v[2], v[5], v[8]), min(v[2], v[5], v[8])
        zmax = hi if zmax is None else max(zmax, hi)
        zmin = lo if zmin is None else min(zmin, lo)
        off += 50
else:
    for line in data.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            z = float(line.split()[3])
            zmax = z if zmax is None else max(zmax, z)
            zmin = z if zmin is None else min(zmin, z)
print(f"{zmax} {zmin}" if zmax is not None else "")
PY
)
TERRAIN_ZMAX=$(printf '%s' "$TERRAIN_Z" | cut -d" " -f1)
TERRAIN_ZMIN=$(printf '%s' "$TERRAIN_Z" | cut -d" " -f2)
PEDESTRIAN_Z=$("$PY" -c "print(($TERRAIN_ZMAX) + 1.5)" 2>/dev/null || echo "1.5")
# The domain floor is placed just ABOVE the terrain's lowest point, so the
# terrain itself seals it. Measuring terrain.stl is the right way to find that
# point and it survived the slab->sheet change, because both spell the same
# thing: the closed slab's minimum was its artificial underside and the domain
# sealed against that; the open sheet's minimum is the real ground and the
# domain seals against the sheet.
#
# It must not go BELOW the terrain. A fixed -60 m floor did, on a site whose
# ground sat 25 m higher, and snappyHexMesh meshed the gap as fluid so the inlet
# blew air UNDER the terrain; eddy3d-cli now refuses such a domain outright.
# Going below is also tempting for a different wrong reason: buildings are
# extruded ~20 m down into the ground, and that skirt does dip below this floor.
# It is meant to -- the skirt exists to guarantee the buildings INTERSECT the
# terrain, and that intersection happens at the surface. Lowering the floor to
# contain it only buys a void that snappy then has to carve away.
DOMAIN_ZMIN=$("$PY" -c "print(($TERRAIN_ZMIN) + 0.5)" 2>/dev/null || echo "-60")
export DOMAIN_ZMIN   # read by the config generator below, which is a separate process
# The ABL datum, from the geometry report the builder wrote beside the STLs.
GROUND_Z=$("$PY" -c "
import json,sys
try:
    print(json.load(open(sys.argv[1]))['terrain_z_min'])
except Exception:
    print(0)
" "$GEO_REPORT" 2>/dev/null || echo 0)
export GROUND_Z
log "domain floor ${DOMAIN_ZMIN}, ABL zGround ${GROUND_Z}"

# ── 2. build the study ────────────────────────────────────────────────────────
# $SPEC is written to a file rather than piped in: a `<<'PY'` heredoc on the same
# command ALWAYS wins the redirect race for stdin, so a `printf ... | "$PY" -
# ... <<'PY'` pipe is silently discarded -- python reads the heredoc as its own
# script source (that is what "$PY" - means), and by the time the script itself
# tries `json.load(sys.stdin)`, stdin is the already-exhausted heredoc, not the
# piped spec. That crashed on every case with "Expecting value: line 1 column 1"
# followed by eddy3d-cli rejecting the resulting empty cfg.json -- found on the
# very first real end-to-end validation run (Phoenix job 12910607).
#
# Skipped entirely on a resume: build-case would regenerate the study and
# overwrite the mesh and the time steps the checkpoint just restored.
if [ "$RESUMING" -eq 0 ]; then
printf '%s' "$SPEC" > "$SCRATCH/spec.json"
"$PY" - "$SCRATCH" "$CASE_ID" "$NP" > "$SCRATCH/cfg.json" <<'PY'
import json, os, sys
scratch, case_id, np_ = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(scratch + "/spec.json") as f:
    spec = json.load(f)
# The v2 sampler publishes a SITE, not a mesh box: coordinates, recipe and LCZ,
# with no domain. The domain is a property of the recipe, so it is derived here
# rather than carried 5,000 times through the database. fixed-box-1008 means a
# 1008 m sampled core (half 504) inside an 800 m buffer -- the same +/-1304 m
# box, -60..600 m tall at 16 m cells, that the validated Braselton case used.
dom = spec.get("domain")
if dom is None:
    # 4 m inside the terrain sheet, which spans 504+800 exactly. While terrain
    # was a closed slab with vertical sides, a domain of identical extent was
    # fine -- the slab's walls were coincident with the domain's and snappy had a
    # closed volume to cut against. An open sheet has no walls, so a domain of
    # exactly the same footprint leaves the sheet's edge lying IN the side patch,
    # where castellation cannot reliably decide which side is fluid. Insetting
    # the domain makes the sheet overhang it on all four sides, which is what
    # gives snappy an unambiguous cut.
    half = 504.0 + 800.0 - 4.0
    zmin = float(os.environ.get("DOMAIN_ZMIN", "-60"))
    # 24 m, not 16 -- see the geometry block below. A COARSER background refined
    # harder measured strictly better on this campaign's geometry, and is also
    # about three times cheaper.
    dom = {"min": [-half, -half, zmin], "max": [half, half, 600], "cellSize": 24}
json.dump({
    "caseName": case_id,
    "workDir": scratch,
    "domain": dom,
    # groundZ is OpenFOAM's zGround: the datum the ABL log profile is measured
    # from, U = (U*/kappa) ln((z - zGround + z0)/z0). It was pinned at 0, which is
    # only correct where the ground happens to sit at zero. On real terrain the
    # profile is then displaced by the site's elevation, and at the actual ground
    # surface the log argument can go negative. Taken from the geometry report's
    # ground-surface minimum -- NOT the terrain STL's minimum, which is the slab's
    # artificial base some 20 m lower.
    "wind": spec.get("wind", {"directions": [0, 45, 90, 135, 180, 225, 270, 315],
                              "speed": 5, "refHeight": 10, "roughness": 0.5,
                              "groundZ": float(os.environ.get("GROUND_Z", "0"))}),
    # From a 55-configuration mesh study on this campaign's own geometry (one
    # GlobalBuildingAtlas tile, 1263 buildings, median height 9.3 m). Against
    # build-case's defaults on that same tile:
    #
    #   defaults  cell 16, b2, g1, feature 4, ncbl 4   16,133,853 cells  skew 18.4  82 bad faces
    #   these     cell 24, b3, g3, feature 0, ncbl 2    3,892,837 cells  skew  3.77  0 bad faces
    #
    # 4.1x fewer cells AND 5x lower skewness. Two of the changes carry it:
    #
    #   featureLevel 0 -- snapping to edges extracted from thousands of extruded
    #     footprints, many of them slivers, was the dominant source of skew and,
    #     at the template's level 4, of cell count (8.8M of the 16.1M).
    #   cellSize 24 with levels 3/3 -- a COARSER background refined harder beats
    #     a finer one, which is not the direction intuition suggests. cellSize 16
    #     at the same levels gives skew 8.19 with 23 bad faces; 20 m gives 5.92
    #     with 7. Measured, not reasoned.
    #
    # 3.0 m is an optimum rather than a budget compromise: EVERY finer setting
    # tested came out worse -- 2.5 m -> 5.92, 2.0 m -> 6.11-8.27, 1.5 m -> 5.71
    # -- including by the same coarse-background route that produced this
    # result. Two traps found on the way: groundLevel 5 is a silent no-op (it
    # yields a mesh byte-identical to groundLevel 4, no warning), and every
    # groundLevel 4 attempt OOM-killed until WSL2 was given more than its
    # default half of host RAM.
    #
    # nCellsBetweenLevels 2 is free: quality is flat to three decimals from 2 to
    # 6 while the cell count doubles.
    #
    # Spread AFTER, so a per-case spec can still override any of these.
    "geometry": {"buildingsStl": scratch + "/buildings.stl",
                 "terrainStl": scratch + "/terrain.stl",
                 "buildingLevel": 3,
                 "groundLevel": 3,
                 "featureLevel": 0,
                 "cellsBetweenLevels": 2,
                 **spec.get("geometry", {})},
    # writeInterval is a checkpoint cadence, not a "how often to look" knob:
    # every write is a point a preempted or walltime-killed solve can resume
    # from. It used to equal the iteration budget -- one write at the very end
    # -- which is why an 8 h chunk cut at iteration 749 had nothing on disk but
    # time 0. 200 is ~1-2 h of solving at this campaign's rates; purgeWrite in
    # build-case keeps only the last few, so disk stays bounded.
    "simulation": {**{"iterations": 1868,
                      "writeInterval": int(os.environ.get("WIND_WRITE_INTERVAL", "200")),
                      "turbulenceModel": "kEpsilon", "numericsLevel": 4},
                   **spec.get("simulation", {}), "cpus": np_},
}, sys.stdout, indent=2)
PY
"$CLI" build-case "$SCRATCH/cfg.json" > "$SCRATCH/build.json" \
    || fatal_case "build-case rejected the spec (see build.json)"
fi
STUDY="$SCRATCH/$CASE_ID"
[ -d "$STUDY" ] || retry_case "study directory missing after build/restore: $STUDY"

# ── 3. mesh + solve, one direction at a time ──────────────────────────────────
case "$RUNTIME" in
    podman)
        # Rootless podman on PACE: no XDG_RUNTIME_DIR (no systemd user session), no
        # subuid range (/etc/subuid is empty), and the image's own uid 1000 is
        # unreachable from a single mapping. All three are handled here.
        export XDG_RUNTIME_DIR="$SCRATCH/xdg"; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
        PSTORE="$SCRATCH/pstore"; mkdir -p "$PSTORE"
        POD="podman --root $PSTORE --runroot $XDG_RUNTIME_DIR/run --storage-driver overlay --storage-opt overlay.ignore_chown_errors=true --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
        $POD pull "$IMG" >/dev/null 2>&1 || retry_case "image pull failed"
        export WIND_MPIRUN="mpirun --allow-run-as-root --oversubscribe -np" ;;
    docker)
        docker image inspect "$IMG" >/dev/null 2>&1 || docker pull "$IMG" >/dev/null 2>&1 \
            || retry_case "image pull failed"
        export WIND_MPIRUN="mpirun --allow-run-as-root --oversubscribe -np" ;;
    native)
        # MS-MPI: no --oversubscribe, no --allow-run-as-root, and -n not -np.
        export WIND_MPIRUN="mpiexec -n" ;;
esac

# The "done and converged" gate lives in its own file, sourced here AND by
# tests/test_solve_gate.py against bare bash -- one function, so a change to
# what counts as "converged" cannot drift between what runs and what is tested.
cp "$SCRIPT_DIR/lib/solve_gate.sh" "$SCRATCH/solve_gate.sh"

cat > "$SCRATCH/inner.sh" <<'INNER'
#!/bin/bash
set -uo pipefail
# inner.sh <root> <case_id> <ranks> <slice_z> <resuming>
#   root      the scratch directory as THIS process sees it (/s in a container,
#             the host path natively)
#   resuming  1 when the study was restored from a checkpoint: meshes already
#             built are kept, converged directions are skipped, and a direction
#             with a written time step continues from it.
ROOT=$1; CASE=$2; NP=$3; SLICE_Z=$4; RESUMING=${5:-0}
MPIRUN="${WIND_MPIRUN:-mpirun --allow-run-as-root --oversubscribe -np}"
NATIVE="${WIND_NATIVE:-0}"
# The podman path enters through the image's interactive entrypoint, which
# sources OpenFOAM's bashrc; the docker path deliberately overrides that
# entrypoint (`bash inner.sh`, non-interactive) and gets no environment at
# all -- foamRun simply is not on PATH. The native path already exported
# WM_PROJECT_DIR. So: source it only when nothing has.
if [ -z "${WM_PROJECT_DIR:-}" ] && [ -f /home/openfoam/OpenFOAM-12/etc/bashrc ]; then
  # Mirror the image's own ~/.bashrc, which a non-interactive bash never reads:
  # MPI on PATH FIRST -- config.sh/mpi needs mpicc to set MPI_ARCH_PATH, and
  # without it mpirun is simply absent (exit 127, second smoke run) -- then
  # OpenFOAM. The bashrc also reads variables it never sets (ZSH_NAME) and
  # dies under `set -u` (first smoke run), so -u is relaxed for the source.
  if [ -d /opt/amazon/openmpi/bin ]; then
    export PATH="/opt/amazon/openmpi/bin:/opt/amazon/efa/bin:$PATH"
    export LD_LIBRARY_PATH="/opt/amazon/openmpi/lib:/opt/amazon/efa/lib:${LD_LIBRARY_PATH:-}"
  fi
  set +u; source /home/openfoam/OpenFOAM-12/etc/bashrc; set -u
fi
source "$ROOT/solve_gate.sh"
STUDY="$ROOT/$CASE"
cd "$STUDY" || exit 1
for m in mesh mesh_*; do
  [ -d "$m" ] || continue
  if [ "$RESUMING" = 1 ] && [ -f "$m/constant/polyMesh/owner" ] && [ ! -d "$m/processor0" ]; then
    echo "MESH_KEPT $m"; continue
  fi
  ( cd "$m" || exit 1
    blockMesh > 01.log 2>&1 || { echo "MESH_FAIL $m blockMesh"; exit 70; }
    surfaceFeatures > 02.log 2>&1
    decomposePar -force > 03.log 2>&1
    $MPIRUN "$NP" snappyHexMesh -overwrite -parallel >> 03.log 2>&1
    # snappy can leave a rank's hexRef8 level lists stale against the mesh it wrote;
    # reconstructPar then aborts building hexRef8Data even though the mesh is fine.
    for d in processor*/constant/polyMesh; do
      rm -f "$d/cellLevel" "$d/pointLevel" "$d/surfaceIndex" "$d/refinementHistory"; :
    done
    reconstructPar -constant -latestTime -noFields >> 03.log 2>&1
    # "does constant/polyMesh exist" is vacuous -- blockMesh wrote one at step 01.
    # Newer-than-the-decomposed-copy is the check that actually discriminates.
    for _ in 1; do
      if [ constant/polyMesh/owner -nt processor0/constant/polyMesh/owner ]; then rm -rf processor*
      else echo "RECONSTRUCT_DID_NOT_REFRESH $m"; fi; :
    done
    checkMesh 2>/dev/null | grep -E "^ *cells:|Failed" | sed "s/^/$m /"
  ) || exit $?
done
for c in case_*; do
  [ -d "$c" ] || continue
  ( cd "$c" || exit 1
    MESH=mesh_${c#case_}
    [ -d "../$MESH" ] || MESH=mesh
    if [ "$RESUMING" = 1 ] && grep -aq "SIMPLE solution converged" 12.log 2>/dev/null; then
      # Finished in an earlier chunk. Only the archive step below is owed.
      echo "SKIP_DONE $c"
      [ -d "$(ls -d [1-9]* 2>/dev/null | sort -g | tail -1)" ] || reconstructPar -latestTime > 13.log 2>&1
      exit 0
    fi
    if [ "$NATIVE" = 1 ]; then
      # No symlinks: MSYS `ln -s` copies by default and Windows symlinks need a
      # privilege the worker cannot count on. A copy of the mesh per direction
      # costs disk on the local drive and nothing else.
      [ -d constant/polyMesh ] && [ ! -L constant/polyMesh ] && [ -f constant/polyMesh/owner ] \
        || { rm -rf constant/polyMesh; cp -r "../$MESH/constant/polyMesh" constant/polyMesh; }
    else
      if [ -L constant/polyMesh ]; then rm -f constant/polyMesh; else rm -rf constant/polyMesh; fi
      ln -sfn "../../$MESH/constant/polyMesh" constant/polyMesh
    fi
    LATEST0=$(ls -d processor0/[0-9]* 2>/dev/null | xargs -n1 basename 2>/dev/null | sort -g | tail -1)
    RANKS0=$(ls -d processor[0-9]* 2>/dev/null | wc -l | tr -d ' ')
    if [ "$RESUMING" = 1 ] && [ -n "$LATEST0" ] && [ "$LATEST0" != 0 ] && [ "$RANKS0" = "$NP" ]; then
      # Continue from the last written time step. Same rank count is required:
      # the decomposition on disk IS the checkpoint.
      echo "RESUME $c from time $LATEST0"
      sed -i 's/^startFrom .*;/startFrom    latestTime;/' system/controlDict
      $MPIRUN "$NP" foamRun -solver incompressibleFluid -parallel >> 12.log 2>&1
    else
      [ "$RESUMING" = 1 ] && echo "RESTART $c (latest=${LATEST0:-none}, ranks=$RANKS0 vs $NP)"
      rm -f 0/phi 0/Phi
      rm -rf processor[0-9]*
      decomposePar -force > 11.log 2>&1
      $MPIRUN "$NP" foamRun -solver incompressibleFluid -parallel > 12.log 2>&1
    fi
    SOLVE_RC=$?

    # RANKS/LATEST are computed BEFORE the gate (not just for the flux check
    # below) because "did it actually write a result" is itself part of
    # "done" -- see check_solve_converged.
    RANKS=$(ls -d processor[0-9]* 2>/dev/null | wc -l)
    LATEST=$(ls -d processor0/[0-9]* 2>/dev/null | xargs -n1 basename 2>/dev/null | sort -g | tail -1)

    GATE_MSG=$(check_solve_converged "$SOLVE_RC" 12.log processor0 "$LATEST")
    GATE_RC=$?
    case "$GATE_RC" in
      0) : ;;                                      # OK -- fall through
      2) if [ "${WIND_ALLOW_UNCONVERGED:-0}" = 1 ]; then
           # Dev/smoke only: keep going so the archive step can be exercised on
           # a mesh too crude to converge. Never the default -- the case is
           # marked converged=false in its manifest and metrics, and the
           # campaign's production gate (quarantine) is unchanged.
           echo "UNCONVERGED_ACCEPTED $c (WIND_ALLOW_UNCONVERGED=1)"
         else
           echo "NOT_CONVERGED $c ($GATE_MSG)"; exit 72
         fi ;;
      *) echo "$GATE_MSG ($c)"; exit 71 ;;
    esac

    # Direction sanity: phi uses the outward normal, so a correct inlet is NEGATIVE.
    #
    # This MUST run -parallel with the rank count taken from the DISK. The case is
    # still decomposed (results live only in processor*/), and a serial
    # foamPostProcess against that root silently resolves latestTime to 0/ and
    # writes nothing at all -- which reads as "no flux" rather than as an error,
    # so the check would quietly stop checking. Reconstructing first is not an
    # option either: it costs minutes per direction for four numbers. The gate
    # above already REQUIRES RANKS>0 and a non-empty LATEST to reach here (that
    # is what "no result time directory" fails on), so the old serial fallback
    # for RANKS==0 -- which silently resolved latestTime to 0/ and reported
    # "no flux" as if it were a real answer -- is dead code and has been removed.
    $MPIRUN "$RANKS" \
        foamPostProcess -func "patchFlowRate(patch=inlet)" -time "$LATEST" -parallel > fr.log 2>&1
    FLUX=$(grep -a 'sum(inlet)' fr.log | tail -1 | awk '{print $NF}')
    echo "INLET_FLUX $c ${FLUX:-NONE}"

    # Sample U on a horizontal plane at pedestrian height so run_case.sh can
    # render a screenshot after the container exits (matplotlib is not on
    # this image, so the render itself happens host-side -- see step 4).
    # Best effort: a failed sample must never fail an otherwise-good case.
    mkdir -p system
    cat > system/sliceFO <<SLICEDICT
FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      sliceFO;
}
// foamPostProcess -dict reads this as a "functions" dictionary: a LIST of
// named function objects, so the sampler is one named entry, not the file.
pedestrianSlice
{
    type            surfaces;
    libs            ("libsampling.so");
    writeControl    timeStep;
    writeInterval   1;
    surfaceFormat   raw;
    fields          (U);
    interpolationScheme cellPoint;
    // OpenFOAM 12 syntax (tutorials/incompressibleFluid/movingCone/system/
    // cutPlane): surfaces is a LIST, the type is cutPlane, point/normal are
    // top-level. The older dictionary form ("Attempt to return dictionary
    // entry as a primitive") and pointAndNormalDict are refused.
    surfaces
    (
        slice
        {
            type            cutPlane;
            planeType       pointAndNormal;
            point           (0 0 $SLICE_Z);
            normal          (0 0 1);
            interpolate     true;
        }
    );
}
SLICEDICT
    $MPIRUN "$RANKS" \
        foamPostProcess -dict system/sliceFO -time "$LATEST" -parallel > slice.log 2>&1 \
        || echo "SLICE_SAMPLE_FAILED $c (see slice.log; not fatal)"

    # The archive ships ONE reconstructed time step, not 24 processor
    # directories: rank counts differ per machine, so a decomposed result is
    # unusable anywhere else without this step, and doing it once here makes
    # every archive self-contained and ParaView-readable. Best effort -- the
    # packer below falls back to the decomposed fields if it did not happen.
    reconstructPar -latestTime > 13.log 2>&1 || echo "RECONSTRUCT_FAILED $c (archive will carry decomposed fields)"
  ) || exit $?
done
echo INNER_OK
INNER
sed -i 's/\r$//' "$SCRATCH/inner.sh"

start_progress_writer
engine_run "$CASE_ID" "$NP" "$PEDESTRIAN_Z" "$RESUMING" > "$SCRATCH/run.raw" 2>&1 &
ENGINE_PID=$!
wait "$ENGINE_PID"
ENGINE_PID=""
[ -n "$PROGRESS_PID" ] && { kill "$PROGRESS_PID" 2>/dev/null; PROGRESS_PID=""; }
grep -v "job control\|terminal process group" "$SCRATCH/run.raw" > "$SCRATCH/run.log"
cat "$SCRATCH/run.log" >&2
grep -q "INNER_OK" "$SCRATCH/run.log" || {
    grep -q "MESH_FAIL" "$SCRATCH/run.log" && fatal_case "meshing failed - see run.log"
    # NOT_CONVERGED is fatal, not retried: convergence is a property of the
    # RECIPE (iteration budget, geometry, mesh), never of which machine ran
    # it, so retrying elsewhere would reproduce the identical outcome -- same
    # reasoning as the non-negative-inlet-flux check below.
    grep -q "NOT_CONVERGED" "$SCRATCH/run.log" && \
        fatal_case "a direction ran to completion without meeting residualControl - see 12.log"
    log "solve failed"; exit 1
}

# Every direction must have admitted the wind. A positive inlet flux means the
# box was not turned to face it, which is a silent, plausible-looking wrong answer
# rather than a crash -- so it is checked, not assumed.
if grep -a "INLET_FLUX" "$SCRATCH/run.log" | awk '{print $3}' | grep -qvE '^-[0-9]'; then
    log "$(grep -a INLET_FLUX "$SCRATCH/run.log")"
    fatal_case "a direction has non-negative inlet flux: the wind did not enter the domain"
fi

# ── 4. render + sample + ship ─────────────────────────────────────────────────
# TODO(v2-contract): replace with the terrain-following slice export once the
# contract is settled. Until then the case's own logs and the flux check are
# the artefact that actually gates success -- the screenshot below is a
# convenience for a human looking at the campaign, not something anything
# downstream depends on, so a failed render must never fail an otherwise
# good case. It is a PNG on disk, nothing more: never stored in the broker's
# database, only referenced by the same result_uri directory as everything
# else in this case.
#
# matplotlib/numpy are not on the OpenFOAM image, so this runs HOST-side
# against the .raw sample foamPostProcess wrote inside the container (visible
# here unchanged -- $STUDY is the same bind-mounted path on both sides).
# OpenFOAM 12's raw surface writer lays the sample out as
# postProcessing/<functionObject>/<time>/<surface>.xy (same columns as the
# older surfaces/<time>/<surface>_U.raw: x y z Ux Uy Uz); both are matched.
for RAWFILE in "$STUDY"/case_*/postProcessing/pedestrianSlice/*/*.xy \
               "$STUDY"/case_*/postProcessing/surfaces/*/*_U.raw; do
    [ -f "$RAWFILE" ] || continue
    CDIR=$(echo "$RAWFILE" | sed -n "s#.*/\(case_[^/]*\)/postProcessing.*#\1#p")
    PNG="$SCRATCH/preview_${CDIR#case_}.png"
    uv run --with matplotlib --with numpy python - "$RAWFILE" "$PNG" >>"$SCRATCH/preview.log" 2>&1 <<'PY' \
        || log "preview render failed for $CDIR (see preview.log; not fatal)"
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

raw_path, out_png = sys.argv[1], sys.argv[2]
data = np.loadtxt(raw_path, comments="#")
x, y = data[:, 0], data[:, 1]
ux, uy, uz = data[:, 3], data[:, 4], data[:, 5]
umag = np.sqrt(ux**2 + uy**2 + uz**2)

fig, ax = plt.subplots(figsize=(6, 6), dpi=120)
tpc = ax.tricontourf(x, y, umag, levels=30, cmap="turbo")
fig.colorbar(tpc, ax=ax, label="|U| (m/s)")
ax.set_aspect("equal")
ax.set_xlabel("x (m)"); ax.set_ylabel("y (m)")
ax.set_title("Pedestrian-height wind speed")
fig.tight_layout()
fig.savefig(out_png)
PY
done

# ── 5. archive + ship ─────────────────────────────────────────────────────────
# One .tar.gz per case, laid out as <case_id>/...:
#   mesh*/constant/polyMesh        the mesh (once -- case_*/constant/polyMesh is a
#                                  link to it and is deliberately NOT included)
#   case_<dir>/<latest>/           the reconstructed LAST time step, every field
#   case_<dir>/system, constant/*  dictionaries (constant minus the mesh link)
#   case_<dir>/*.log, postProcessing/   solver logs, the pedestrian-plane sample
#   run.log build.json cfg.json spec.json preview_*.png manifest.json
# gzip, not zstd: git-bash and the WSL image have no zstd, PACE and blueCFD do;
# one format everywhere beats a faster one on three machines out of five.
# Written under .tmp and renamed into place, so neither Syncthing nor a
# master-side rsync ever picks up a half-written archive.
PACK="$SCRATCH/pack.list"
: > "$PACK"
cp "$SCRATCH"/run.log "$SCRATCH"/build.json "$SCRATCH"/cfg.json "$SCRATCH"/spec.json "$STUDY/" 2>/dev/null
cp "$SCRATCH"/preview_*.png "$STUDY/" 2>/dev/null
for f in run.log build.json cfg.json spec.json "$STUDY"/preview_*.png; do
    f=$(basename "$f"); [ -f "$STUDY/$f" ] && echo "$CASE_ID/$f" >> "$PACK"
done
for m in "$STUDY"/mesh "$STUDY"/mesh_*; do
    [ -d "$m/constant/polyMesh" ] || continue
    mn=$(basename "$m")
    echo "$CASE_ID/$mn/constant/polyMesh" >> "$PACK"
    for l in "$m"/*.log; do [ -f "$l" ] && echo "$CASE_ID/$mn/$(basename "$l")" >> "$PACK"; done
done
TIMES=""
for c in "$STUDY"/case_*; do
    [ -d "$c" ] || continue
    cn=$(basename "$c")
    latest=$(ls -d "$c"/[0-9]* 2>/dev/null | xargs -n1 basename 2>/dev/null | sort -g | tail -1)
    if [ -n "$latest" ] && [ "$latest" != 0 ]; then
        echo "$CASE_ID/$cn/$latest" >> "$PACK"
    else
        latest=$(ls -d "$c"/processor0/[0-9]* 2>/dev/null | xargs -n1 basename 2>/dev/null | sort -g | tail -1)
        log "$cn: no reconstructed time step; archiving decomposed fields at ${latest:-none}"
        for p in "$c"/processor[0-9]*; do
            [ -n "$latest" ] && echo "$CASE_ID/$cn/$(basename "$p")/$latest" >> "$PACK"
            echo "$CASE_ID/$cn/$(basename "$p")/constant" >> "$PACK"
        done
    fi
    echo "$CASE_ID/$cn/system" >> "$PACK"
    for f in "$c"/constant/*; do [ -f "$f" ] && echo "$CASE_ID/$cn/constant/$(basename "$f")" >> "$PACK"; done
    [ -d "$c/postProcessing" ] && echo "$CASE_ID/$cn/postProcessing" >> "$PACK"
    for l in "$c"/*.log; do [ -f "$l" ] && echo "$CASE_ID/$cn/$(basename "$l")" >> "$PACK"; done
    if grep -aq "^SIMPLE solution converged in" "$c/12.log" 2>/dev/null; then conv=1; else conv=0; fi
    TIMES="$TIMES$cn=$latest:$conv "
done

"$PY" - "$STUDY/manifest.json" "$CASE_ID" "$NP" "$RUNTIME" "$RESUMING" "$TIMES" <<'PY'
import json, os, platform, socket, sys, time
out, case_id, ranks, runtime, resumed, times = sys.argv[1:7]
per_dir = {}
for kv in times.split():
    d, rest = kv.split("=", 1)
    t, conv = rest.rsplit(":", 1)
    per_dir[d] = {"latest_time": t, "converged": conv == "1"}
json.dump({
    "case_id": case_id, "worker": os.environ.get("CASEBROKER_WORKER_ID"),
    "host": socket.gethostname(), "platform": platform.platform(),
    "runtime": runtime, "ranks": int(ranks), "resumed": resumed == "1",
    "directions": per_dir,
    # False only under WIND_ALLOW_UNCONVERGED=1 (dev/smoke); a production run
    # never reaches this file unconverged -- the gate quarantines it first.
    "converged": all(v["converged"] for v in per_dir.values()) if per_dir else False,
    "layout": "mesh*/constant/polyMesh is the mesh; case_*/constant/polyMesh is not "
              "included -- relink it to ../../mesh/constant/polyMesh to open a case",
    "packed_at": int(time.time()),
}, open(out, "w"), indent=2)
PY
echo "$CASE_ID/manifest.json" >> "$PACK"

mkdir -p "$DONE_DIR/.tmp" || retry_case "cannot create $DONE_DIR"
ARCHIVE="$DONE_DIR/$CASE_ID.tar.gz"
tar -czf "$DONE_DIR/.tmp/$CASE_ID.tar.gz.part" -C "$SCRATCH" -T "$PACK" \
    || retry_case "archiving failed (see pack.list)"
mv -f "$DONE_DIR/.tmp/$CASE_ID.tar.gz.part" "$ARCHIVE" || retry_case "could not move archive into $DONE_DIR"
# The checkpoint has served its purpose; the archive is the result now.
rm -rf "$CKPT" 2>/dev/null
[ -n "$PROGRESS_FILE" ] && rm -f "$PROGRESS_FILE" 2>/dev/null

"$PY" - "$ARCHIVE" "$NP" "$RUNTIME" "$RESUMING" "$TIMES" <<'PY'
import hashlib, json, os, sys
path, ranks, runtime, resumed, times = sys.argv[1:6]
h = hashlib.sha256()
with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
dirs = dict(kv.split("=", 1) for kv in times.split() if "=" in kv)
print(json.dumps({"result_uri": "file://" + path, "sha256": h.hexdigest(),
                  "bytes": os.path.getsize(path),
                  "metrics": {"stage": "archived", "runtime": runtime, "ranks": int(ranks),
                              "resumed": resumed == "1",
                              "latest_time": {d: v.rsplit(":", 1)[0] for d, v in dirs.items()},
                              "converged": all(v.endswith(":1") for v in dirs.values()) and bool(dirs)}}))
PY
