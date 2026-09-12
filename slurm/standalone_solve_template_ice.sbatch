#!/bin/bash
#SBATCH -J solve-CASETAG
#SBATCH -N 1 -n 24
#SBATCH -t 08:00:00
#SBATCH -p ice-cpu
#SBATCH -q coe-ice
#SBATCH -A coa
#SBATCH --signal=B:TERM@300
#SBATCH -o /storage/ice1/0/3/pkastner3/pace_cfd/CASETAG/slurm_%j.out
#
# ICE twin of standalone_solve_template.sbatch -- see docs/pace-hpc.md.
# Moved here from Phoenix because gts-pkastner3's Phoenix compute-hour
# balance (pace-quota's "Job Charge Account Balances") is exhausted
# (Balance: 0.82, essentially zero) -- inferno draws against that balance
# and cpu-gnr bills CPU-minutes at 155x, so it burns fast. ICE's coa/coe-ice
# combination shows no such balance in pace-quota output at all, matching
# ice_worker.sbatch's note that ICE has no embers/preemption tier -- this
# looks like a different (course/flat) allocation model, not the same
# metered SU pool. Confirm this holds if jobs here also start pending on
# AssocGrpBillingMinutes.
#
# ICE scratch (/storage/ice1/0/3/pkastner3) was 97.5% full (7.4 GB free) as
# of 2026-09-10 -- tight. Case + mesh here is ~3.7 GB; keep purgeWrite low
# and writeInterval small-but-not-huge so checkpoints don't balloon past
# what's left. Check `pace-quota` on ICE before adding more cases here.
#
# CIRCUIT BREAKER: do not remove this. See standalone_solve_template.sbatch
# for why -- a version without it chained 120 times in 90 minutes once.
set -uo pipefail

TAG=CASETAG
PERSIST="/storage/ice1/0/3/pkastner3/pace_cfd/$TAG"
NP=${SLURM_NTASKS:-24}
IMG="docker.io/dicehub/openfoam:12"

RESUMING=0
[ -f "$PERSIST/case_270/processor0/constant/polyMesh/owner" ] && RESUMING=1

FAILCOUNT_FILE="$PERSIST/.failcount"
LOG_SIZE_BEFORE=$(stat -c%s "$PERSIST/12.log" 2>/dev/null || echo 0)

echo "$(date) starting $TAG on $(hostname), $NP ranks, TMPDIR=$TMPDIR, resuming=$RESUMING"
df -h "$TMPDIR" | tail -1

SCRATCH="$TMPDIR/$TAG"
mkdir -p "$SCRATCH"
cp -r "$PERSIST/mesh" "$PERSIST/case_270" "$SCRATCH/"

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

    if grep -q "SIMPLE solution converged" "$PERSIST/12.log" 2>/dev/null; then
        echo "$(date) $TAG CONVERGED"
        rm -f "$FAILCOUNT_FILE"
    elif [ "$PROGRESSED" -eq 1 ]; then
        echo "$(date) $TAG made progress this chunk (12.log grew $LOG_SIZE_BEFORE -> $LOG_SIZE_AFTER bytes) -- chaining a follow-up 8h job"
        rm -f "$FAILCOUNT_FILE"
        sbatch --dependency=afterany:"$SLURM_JOB_ID" "$PERSIST/solve.sbatch"
    else
        N=$(( $(cat "$FAILCOUNT_FILE" 2>/dev/null || echo 0) + 1 ))
        echo "$N" > "$FAILCOUNT_FILE"
        if [ "$N" -ge 2 ]; then
            echo "$(date) $TAG FAILED: $N consecutive chunks with no progress -- NOT re-chaining. Needs a human look at $PERSIST/run.log and the latest slurm_*.out."
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

$POD run --rm --user 0:0 -e HOME=/home/openfoam -v "$SCRATCH:/s" "$IMG" \
    "bash /s/inner.sh /s $NP" 2>&1 | grep -v "job control\|terminal process group" | tee "$SCRATCH/run.log"

grep -q "SIMPLE solution converged" "$SCRATCH/case_270/12.log" && echo "$(date) $TAG CONVERGED"
echo "$(date) $TAG this chunk done (finalize will rsync results back and chain if needed)"
