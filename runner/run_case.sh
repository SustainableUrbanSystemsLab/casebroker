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
# per rank, which is the worst access pattern Lustre has, and only the sampled
# arrays are copied back -- a raw case is ~2.1 GB against ~14 MB of output, and
# 5,000 of the former would be 10.5 TB.

set -uo pipefail

CASE_ID="${CASE_ID:?CASE_ID not set by the worker}"
SPEC="${CASE_SPEC:-$(cat)}"
NP="${WIND_NP:-24}"
WC="${WIND_ROOT:?WIND_ROOT (shared storage root) not set}"
IMG="${WIND_IMAGE:-docker.io/dicehub/openfoam:12}"
CLI="${EDDY3D_CLI:?EDDY3D_CLI (path to eddy3d-cli) not set}"
# The geometry builder lives in the parent repo beside this submodule.
REAL_CITIES="${REAL_CITIES:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../real_cities" 2>/dev/null && pwd)}"

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
    BUILDINGS="$GEO/${CASE_ID}_buildings.stl"
    TERRAIN="$GEO/${CASE_ID}_terrain.stl"
    if [ ! -f "$BUILDINGS" ] || [ ! -f "$TERRAIN" ]; then
        log "building geometry for $LAT,$LON"
        mkdir -p "$GEO"
        # Exit 3 is site_geometry's "no usable building here" -- a real property
        # of the site, not a transient fault, so that one IS fatal. Anything else
        # (a DTM host down, an Overture timeout) is retryable.
        "$PY" "$REAL_CITIES/site_geometry.py" --site "$CASE_ID"               --lat "$LAT" --lon "$LON" --out "$GEO" >"$GEO/geometry.log" 2>&1
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
TERRAIN_ZMAX=$("$PY" - "$SCRATCH/terrain.stl" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read()
zmax = None
is_binary = len(data) >= 84
if is_binary:
    n = struct.unpack_from("<I", data, 80)[0]
    is_binary = len(data) == 84 + 50 * n
if is_binary:
    off = 84
    for _ in range(n):
        v = struct.unpack_from("<9f", data, off + 12)
        zmax = max(zmax, v[2], v[5], v[8]) if zmax is not None else max(v[2], v[5], v[8])
        off += 50
else:
    for line in data.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            z = float(line.split()[3])
            zmax = z if zmax is None else max(zmax, z)
print(zmax if zmax is not None else "")
PY
)
PEDESTRIAN_Z=$("$PY" -c "print(($TERRAIN_ZMAX) + 1.5)" 2>/dev/null || echo "1.5")

# ── 2. build the study ────────────────────────────────────────────────────────
# $SPEC is written to a file rather than piped in: a `<<'PY'` heredoc on the same
# command ALWAYS wins the redirect race for stdin, so a `printf ... | "$PY" -
# ... <<'PY'` pipe is silently discarded -- python reads the heredoc as its own
# script source (that is what "$PY" - means), and by the time the script itself
# tries `json.load(sys.stdin)`, stdin is the already-exhausted heredoc, not the
# piped spec. That crashed on every case with "Expecting value: line 1 column 1"
# followed by eddy3d-cli rejecting the resulting empty cfg.json -- found on the
# very first real end-to-end validation run (Phoenix job 12910607).
printf '%s' "$SPEC" > "$SCRATCH/spec.json"
"$PY" - "$SCRATCH" "$CASE_ID" "$NP" > "$SCRATCH/cfg.json" <<'PY'
import json, sys
scratch, case_id, np_ = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(scratch + "/spec.json") as f:
    spec = json.load(f)
dom = spec["domain"]           # {"min": [...], "max": [...], "cellSize": n}
json.dump({
    "caseName": case_id,
    "workDir": scratch,
    "domain": dom,
    "wind": spec.get("wind", {"directions": [0, 45, 90, 135, 180, 225, 270, 315],
                              "speed": 5, "refHeight": 10, "roughness": 0.5, "groundZ": 0}),
    "geometry": {"buildingsStl": scratch + "/buildings.stl",
                 "terrainStl": scratch + "/terrain.stl",
                 **spec.get("geometry", {})},
    "simulation": {**{"iterations": 1868, "writeInterval": 1868,
                      "turbulenceModel": "kEpsilon", "numericsLevel": 4},
                   **spec.get("simulation", {}), "cpus": np_},
}, sys.stdout, indent=2)
PY
"$CLI" build-case "$SCRATCH/cfg.json" > "$SCRATCH/build.json" \
    || fatal_case "build-case rejected the spec (see build.json)"
STUDY="$SCRATCH/$CASE_ID"

# ── 3. mesh + solve, one direction at a time ──────────────────────────────────
# Rootless podman on PACE: no XDG_RUNTIME_DIR (no systemd user session), no
# subuid range (/etc/subuid is empty), and the image's own uid 1000 is
# unreachable from a single mapping. All three are handled here.
export XDG_RUNTIME_DIR="$SCRATCH/xdg"; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
PSTORE="$SCRATCH/pstore"; mkdir -p "$PSTORE"
POD="podman --root $PSTORE --runroot $XDG_RUNTIME_DIR/run --storage-driver overlay --storage-opt overlay.ignore_chown_errors=true --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
$POD pull "$IMG" >/dev/null 2>&1 || { log "image pull failed"; exit 1; }

# The "done and converged" gate lives in its own file, sourced here AND by
# tests/test_solve_gate.py against bare bash -- one function, so a change to
# what counts as "converged" cannot drift between what runs and what is tested.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/lib/solve_gate.sh" "$SCRATCH/solve_gate.sh"

cat > "$SCRATCH/inner.sh" <<'INNER'
#!/bin/bash
set -uo pipefail
STUDY=$1; NP=$2; SLICE_Z=$3
source /s/solve_gate.sh
cd "$STUDY" || exit 1
for m in mesh mesh_*; do
  [ -d "$m" ] || continue
  ( cd "$m" || exit 1
    blockMesh > 01.log 2>&1 || { echo "MESH_FAIL $m blockMesh"; exit 70; }
    surfaceFeatures > 02.log 2>&1
    decomposePar -force > 03.log 2>&1
    mpirun --allow-run-as-root --oversubscribe -np "$NP" snappyHexMesh -overwrite -parallel >> 03.log 2>&1
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
    if [ -L constant/polyMesh ]; then rm -f constant/polyMesh; else rm -rf constant/polyMesh; fi
    ln -sfn "../../$MESH/constant/polyMesh" constant/polyMesh
    rm -f 0/phi 0/Phi
    decomposePar -force > 11.log 2>&1
    mpirun --allow-run-as-root --oversubscribe -np "$NP" foamRun -solver incompressibleFluid -parallel > 12.log 2>&1
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
      2) echo "NOT_CONVERGED $c ($GATE_MSG)"; exit 72 ;;
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
    mpirun --allow-run-as-root --oversubscribe -np "$RANKS" \
        foamPostProcess -func "patchFlowRate(patch=inlet)" -time "$LATEST" -parallel > fr.log 2>&1
    FLUX=$(grep -a 'sum(inlet)' fr.log | tail -1 | awk '{print $NF}')
    echo "INLET_FLUX $c ${FLUX:-NONE}"

    # Sample U on a horizontal plane at pedestrian height so run_case.sh can
    # render a screenshot after the container exits (matplotlib is not on
    # this image, so the render itself happens host-side -- see step 4).
    # Best effort: a failed sample must never fail an otherwise-good case.
    mkdir -p system
    cat > system/sliceFO <<SLICEDICT
type            surfaces;
libs            (sampling);
writeControl    timeStep;
writeInterval   1;
surfaceFormat   raw;
fields          (U);
interpolationScheme cellPoint;
surfaces
{
    slice
    {
        type            cuttingPlane;
        planeType       pointAndNormal;
        pointAndNormalDict
        {
            point       (0 0 $SLICE_Z);
            normal      (0 0 1);
        }
        interpolate     true;
    }
}
SLICEDICT
    mpirun --allow-run-as-root --oversubscribe -np "$RANKS" \
        foamPostProcess -dict system/sliceFO -time "$LATEST" -parallel > slice.log 2>&1 \
        || echo "SLICE_SAMPLE_FAILED $c (see slice.log; not fatal)"
  ) || exit $?
done
echo INNER_OK
INNER
sed -i 's/\r$//' "$SCRATCH/inner.sh"

$POD run --rm --user 0:0 -e HOME=/home/openfoam -v "$SCRATCH:/s" "$IMG" \
    "bash /s/inner.sh /s/$CASE_ID $NP $PEDESTRIAN_Z" 2>&1 | grep -v "job control\|terminal process group" \
    | tee "$SCRATCH/run.log" >&2
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
for RAWFILE in "$STUDY"/case_*/postProcessing/surfaces/*/*_U.raw; do
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

OUT="$WC/results/$CASE_ID"
mkdir -p "$OUT"
cp "$SCRATCH"/run.log "$SCRATCH"/build.json "$SCRATCH"/cfg.json "$OUT/" 2>/dev/null
cp "$SCRATCH"/preview_*.png "$OUT/" 2>/dev/null
find "$STUDY" -maxdepth 2 -name "*.log" -exec cp {} "$OUT/" \; 2>/dev/null
BYTES=$(du -sb "$OUT" | cut -f1)

"$PY" - "$OUT" "$BYTES" <<'PY'
import hashlib, json, os, sys
out, nbytes = sys.argv[1], int(sys.argv[2])
h = hashlib.sha256()
for root, _, files in os.walk(out):
    for f in sorted(files):
        h.update(open(os.path.join(root, f), "rb").read())
print(json.dumps({"result_uri": "file://" + out, "sha256": h.hexdigest(),
                  "bytes": nbytes, "metrics": {"stage": "solve-only"}}))
PY
