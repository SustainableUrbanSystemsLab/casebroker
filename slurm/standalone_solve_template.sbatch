#!/bin/bash
#SBATCH -J solve-CASETAG
#SBATCH -N 1 -n 24
#SBATCH -t 08:00:00
#SBATCH -p cpu-gnr
#SBATCH -q inferno
#SBATCH -A gts-pkastner3
#SBATCH --signal=B:TERM@300
#SBATCH -o /storage/scratch1/3/pkastner3/pace_cfd/CASETAG/slurm_%j.out
#
# Standalone offload of ONE already-built OpenFOAM case (not through the
# casebroker broker/worker -- this is for running a specific hand-picked
# case, e.g. a mesh-study probe, directly). Mirrors the podman invocation
# in runner/run_case.sh (rootless podman, foamRun -solver incompressibleFluid),
# but skips meshing entirely: the mesh was already built and validated
# elsewhere and only shipped here as a tarball (see docs/pace-hpc.md for the
# packaging step and every gotcha this script works around).
#
# Template usage: replace CASETAG (3 places: -J, -o, TAG=) with the real
# case tag, `sbatch` it. See docs/pace-hpc.md for the full runbook,
# including WHY every non-obvious line below is there -- most of them are
# fixes for a real failure encountered running this for the first time.
#
# 8 h, not longer: PACE's inferno QOS rejects jobs at a size that would push
# GrpTRESRunMins ("billing minutes") over budget, which cpu-gnr's 155x
# per-CPU-minute weight hits fast. Chains itself via sbatch --dependency
# rather than asking for more up front, so each link stays inside whatever
# budget is actually free at that moment.
#
# CIRCUIT BREAKER: do not remove this. An earlier version of this script
# (no failcount check) chained itself 120 times in under 90 minutes when a
# bug made every chunk fail in seconds -- hammering the scheduler and
# almost certainly tripping Docker Hub's anonymous pull rate limit. FAILCOUNT
# below refuses to re-chain past 2 consecutive chunks with no measurable
# progress, and leaves a clear FAILED marker instead.
set -uo pipefail

TAG=CASETAG
PERSIST="/storage/scratch1/3/pkastner3/pace_cfd/$TAG"
NP=${SLURM_NTASKS:-24}
IMG="docker.io/dicehub/openfoam:12"

# A REAL decomposed mesh, not just a directory that happens to exist -- a
# weaker check (bare `-d processor0`) is what let the runaway chain above
# skip decomposePar against a case with no real per-processor mesh.
RESUMING=0
[ -f "$PERSIST/case_270/processor0/constant/polyMesh/owner" ] && RESUMING=1

FAILCOUNT_FILE="$PERSIST/.failcount"
LOG_SIZE_BEFORE=$(stat -c%s "$PERSIST/12.log" 2>/dev/null || echo 0)

echo "$(date) starting $TAG on $(hostname), $NP ranks, TMPDIR=$TMPDIR, resuming=$RESUMING"
df -h "$TMPDIR" | tail -1

# Everything runs on node-local scratch ($TMPDIR), not the Lustre-backed
# $PERSIST -- OpenFOAM writes thousands of small per-rank files, the worst
# access pattern for a networked filesystem. Only 12.log (periodically) and
# the final case state (once) go back to $PERSIST.
SCRATCH="$TMPDIR/$TAG"
mkdir -p "$SCRATCH"
cp -r "$PERSIST/mesh" "$PERSIST/case_270" "$SCRATCH/"

# Rootless podman on PACE: no systemd user session (no XDG_RUNTIME_DIR by
# default) and no /etc/subuid range for this user, so the image's default
# non-root user cannot be mapped. Both handled explicitly below; skipping
# either reproduces a real failure (see docs/pace-hpc.md).
export XDG_RUNTIME_DIR="$SCRATCH/xdg"; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
PSTORE="$SCRATCH/pstore"; mkdir -p "$PSTORE"
POD="podman --root $PSTORE --runroot $XDG_RUNTIME_DIR/run --storage-driver overlay --storage-opt overlay.ignore_chown_errors=true --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
if ! $POD image exists "$IMG" 2>/dev/null; then
    echo "$(date) image not cached, pulling..."
    $POD pull "$IMG" || { echo "FATAL: image pull failed (see stderr above)"; exit 1; }
fi

( while true; do cp -f "$SCRATCH/case_270/12.log" "$PERSIST/12.log" 2>/dev/null; sleep 120; done ) &
SYNC_PID=$!
finalize() {
    kill "$SYNC_PID" 2>/dev/null
    cp -f "$SCRATCH/case_270/12.log" "$PERSIST/12.log" 2>/dev/null
    cp -f "$SCRATCH/run.log" "$PERSIST/run.log" 2>/dev/null
    echo "$(date) copying result state back to $PERSIST (this can take a while for $NP processor dirs)"
    rsync -a --exclude 'pstore' --exclude 'xdg' "$SCRATCH/case_270/" "$PERSIST/case_270/" 2>&1 | tail -5

    LOG_SIZE_AFTER=$(stat -c%s "$PERSIST/12.log" 2>/dev/null || echo 0)
    PROGRESSED=0
    [ "$LOG_SIZE_AFTER" -gt "$LOG_SIZE_BEFORE" ] && PROGRESSED=1

    QUEUE=/storage/scratch1/3/pkastner3/pace_cfd/queue.txt
    if grep -q "SIMPLE solution converged" "$PERSIST/12.log" 2>/dev/null; then
        echo "$(date) $TAG CONVERGED -- checking for next queued case instead of re-chaining"
        rm -f "$FAILCOUNT_FILE"
        (
            flock -x 200
            NEXT=$(head -1 "$QUEUE" 2>/dev/null)
            [ -n "$NEXT" ] && sed -i '1d' "$QUEUE"
            if [ -n "$NEXT" ]; then
                echo "$(date) starting next queued case: $NEXT"
                sbatch "/storage/scratch1/3/pkastner3/pace_cfd/$NEXT/solve.sbatch"
            else
                echo "$(date) queue empty -- nothing more to start on PACE"
            fi
        ) 200>"$QUEUE.lock"
    elif [ "$PROGRESSED" -eq 1 ]; then
        echo "$(date) $TAG made progress this chunk (12.log grew $LOG_SIZE_BEFORE -> $LOG_SIZE_AFTER bytes) -- chaining a follow-up 8h job"
        rm -f "$FAILCOUNT_FILE"
        sbatch --dependency=afterany:"$SLURM_JOB_ID" "$PERSIST/solve.sbatch"
    else
        N=$(( $(cat "$FAILCOUNT_FILE" 2>/dev/null || echo 0) + 1 ))
        echo "$N" > "$FAILCOUNT_FILE"
        if [ "$N" -ge 2 ]; then
            echo "$(date) $TAG FAILED: $N consecutive chunks with no progress (12.log did not grow) -- NOT re-chaining. Needs a human look at $PERSIST/run.log and the latest slurm_*.out."
        else
            echo "$(date) $TAG chunk made no progress (attempt $N/2) -- retrying once before giving up"
            sbatch --dependency=afterany:"$SLURM_JOB_ID" "$PERSIST/solve.sbatch"
        fi
    fi
}
trap finalize EXIT

cat > "$SCRATCH/inner.sh" <<INNER
#!/bin/bash
set -uo pipefail
cd "\$1/case_270" || exit 1
NP=\$2
if [ -L constant/polyMesh ]; then rm -f constant/polyMesh; else rm -rf constant/polyMesh; fi
# Symlink target is resolved relative to the LINK's own directory
# (case_270/constant/), not the cwd where ln runs -- needs TWO levels up to
# reach the sibling mesh dir. Getting this wrong (one level) is the single
# most expensive mistake made building this script: decomposePar fails with
# an unrelated-looking "Cannot find file points in polyMesh" and gives no
# hint the symlink itself is broken.
ln -sfn "../../mesh/constant/polyMesh" constant/polyMesh
if [ $RESUMING -eq 1 ]; then
  sed -i "s/startFrom.*;/startFrom    latestTime;/" system/controlDict
  mpirun --allow-run-as-root --oversubscribe -np "\$NP" foamRun -solver incompressibleFluid -parallel >> 12.log 2>&1
else
  sed -i "s/numberOfSubdomains.*;/numberOfSubdomains    \$NP;/" system/decomposeParDict
  decomposePar -force > 11.log 2>&1
  if [ ! -f processor0/constant/polyMesh/owner ]; then
    echo "FATAL: decomposePar did not produce processor0 -- see 11.log" >&2
    tail -20 11.log >&2
    exit 1
  fi
  mpirun --allow-run-as-root --oversubscribe -np "\$NP" foamRun -solver incompressibleFluid -parallel > 12.log 2>&1
fi
echo "SOLVE_RC=\$?"
INNER
sed -i 's/\r$//' "$SCRATCH/inner.sh"

# The image's ENTRYPOINT is "/bin/bash -i -c", which consumes only the FIRST
# WORD of whatever podman appends as its -c script; anything after becomes
# ignored positional params ($0, $1, ...). Passing multiple separate words
# here (e.g. `bash /s/inner.sh /s "$NP"` unquoted) makes podman silently run
# bare "bash" (exits instantly on closed stdin, LOOKS like a clean success)
# instead of inner.sh. The fix is to pass the whole inner command as ONE
# pre-quoted string, exactly as below -- this is not optional.
$POD run --rm --user 0:0 -e HOME=/home/openfoam -v "$SCRATCH:/s" "$IMG" \
    "bash /s/inner.sh /s $NP" 2>&1 | grep -v "job control\|terminal process group" | tee "$SCRATCH/run.log"

grep -q "SIMPLE solution converged" "$SCRATCH/case_270/12.log" && echo "$(date) $TAG CONVERGED"
echo "$(date) $TAG this chunk done (finalize will rsync results back and chain if needed)"
