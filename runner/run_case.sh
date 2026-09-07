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

log() { echo "[$CASE_ID] $*" >&2; }
fatal_case() { log "FATAL (will not retry): $*"; exit 64; }

SCRATCH="${TMPDIR:-/tmp}/wind-$CASE_ID-$$"
mkdir -p "$SCRATCH" || exit 1
cleanup() { rm -rf "$SCRATCH" "$PSTORE" 2>/dev/null; }
trap cleanup EXIT

# ── 1. geometry ───────────────────────────────────────────────────────────────
# The spec names STLs already staged on shared storage by the tile pipeline.
BUILDINGS=$(printf '%s' "$SPEC" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("buildings_stl",""))')
TERRAIN=$(printf '%s' "$SPEC" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("terrain_stl",""))')
[ -f "$BUILDINGS" ] || fatal_case "buildings STL missing: $BUILDINGS"
[ -f "$TERRAIN" ]   || fatal_case "terrain STL missing: $TERRAIN"
cp "$BUILDINGS" "$SCRATCH/buildings.stl"
cp "$TERRAIN"   "$SCRATCH/terrain.stl"

# ── 2. build the study ────────────────────────────────────────────────────────
printf '%s' "$SPEC" | python3 - "$SCRATCH" "$CASE_ID" "$NP" > "$SCRATCH/cfg.json" <<'PY'
import json, sys
spec = json.load(sys.stdin)
scratch, case_id, np_ = sys.argv[1], sys.argv[2], int(sys.argv[3])
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

cat > "$SCRATCH/inner.sh" <<'INNER'
#!/bin/bash
set -uo pipefail
STUDY=$1; NP=$2
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
    grep -aq "^End" 12.log || { echo "SOLVE_FAIL $c"; exit 71; }
    # Direction sanity: phi uses the outward normal, so a correct inlet is NEGATIVE.
    foamPostProcess -func "patchFlowRate(patch=inlet)" -latestTime > fr.log 2>&1
    echo "INLET_FLUX $c $(grep -a 'sum(inlet)' fr.log | tail -1 | awk '{print $NF}')"
  ) || exit $?
done
echo INNER_OK
INNER
sed -i 's/\r$//' "$SCRATCH/inner.sh"

$POD run --rm --user 0:0 -e HOME=/home/openfoam -v "$SCRATCH:/s" "$IMG" \
    "bash /s/inner.sh /s/$CASE_ID $NP" 2>&1 | grep -v "job control\|terminal process group" \
    | tee "$SCRATCH/run.log" >&2
grep -q "INNER_OK" "$SCRATCH/run.log" || {
    grep -q "MESH_FAIL" "$SCRATCH/run.log" && fatal_case "meshing failed - see run.log"
    log "solve failed"; exit 1
}

# Every direction must have admitted the wind. A positive inlet flux means the
# box was not turned to face it, which is a silent, plausible-looking wrong answer
# rather than a crash -- so it is checked, not assumed.
if grep -a "INLET_FLUX" "$SCRATCH/run.log" | awk '{print $3}' | grep -qv '^-'; then
    log "$(grep -a INLET_FLUX "$SCRATCH/run.log")"
    fatal_case "a direction has non-negative inlet flux: the wind did not enter the domain"
fi

# ── 4. sample + ship ──────────────────────────────────────────────────────────
# TODO(v2-contract): replace with the terrain-following slice export once the
# contract is settled. Until then the case's own logs and the flux check are the
# artefact, so a pilot run is still verifiable end to end.
OUT="$WC/results/$CASE_ID"
mkdir -p "$OUT"
cp "$SCRATCH"/run.log "$SCRATCH"/build.json "$SCRATCH"/cfg.json "$OUT/" 2>/dev/null
find "$STUDY" -maxdepth 2 -name "*.log" -exec cp {} "$OUT/" \; 2>/dev/null
BYTES=$(du -sb "$OUT" | cut -f1)

python3 - "$OUT" "$BYTES" <<'PY'
import hashlib, json, os, sys
out, nbytes = sys.argv[1], int(sys.argv[2])
h = hashlib.sha256()
for root, _, files in os.walk(out):
    for f in sorted(files):
        h.update(open(os.path.join(root, f), "rb").read())
print(json.dumps({"result_uri": "file://" + out, "sha256": h.hexdigest(),
                  "bytes": nbytes, "metrics": {"stage": "solve-only"}}))
PY
