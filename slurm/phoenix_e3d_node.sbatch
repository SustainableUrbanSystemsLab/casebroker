#!/bin/bash
#SBATCH -J e3d-node-phoenix
#SBATCH -N 1 -n 24
#SBATCH -t 08:00:00
#SBATCH -p cpu-small
#SBATCH -q embers
#SBATCH -A gts-pkastner3
#SBATCH --requeue
#SBATCH --signal=B:TERM@180
#SBATCH -o logs/e3d_node_phoenix_%j.out
#
# One E3D simulation node on Phoenix's free `embers` QOS: the only thing installed here is the E3D
# binary, paired with the broker as "phoenix" (E3D setup-sim-node). Submit with
# slurm/submit_workers.sh --script phoenix_e3d_node.sbatch (or scripts/pace_workers.sh phoenix ...),
# not by hand. docs/pace-hpc.md section 8 has the setup.
#
# embers costs nothing and is PREEMPTED after an hour whenever a paying job wants the node. That
# used to be the Python worker pool's whole reason for a lease, and it suits the E3D node better:
# a preempted job's SIGTERM releases the case with its attempt refunded, every finished direction
# is already at the broker, and --requeue puts the job back in the queue to continue the same
# case -- or another node takes it from the broker's copy first. A preemption costs the direction
# in flight (~22 min on 24 ranks) and an image pull, nothing else. The Python pool that ran here
# (phoenix_worker.sbatch) could not build any campaign recipe and is retired.
#
# After CHUNK_HOURS (default 7) of one lease the node hands the case on at the next direction
# boundary, before the 8 h wall's SIGTERM would take the direction in flight. CHUNK_HOURS=0 never
# hands off.

set -uo pipefail

# Chain guard, as in ice_e3d_node.sbatch: a predecessor that ended within CHAIN_MIN_SECONDS means
# something is wrong (lost pairing, empty queue, broken shim), so end quietly instead of leasing
# cases to fail them. A requeued job keeps its own CHAIN_PREV, which has long finished.
min=${CHAIN_MIN_SECONDS:-900}
if [ -n "${CHAIN_PREV:-}" ]; then
    prev=$(sacct -j "$CHAIN_PREV" -X -n -P -o State,ElapsedRaw 2>/dev/null | head -1)
    secs=${prev##*|}
    if [[ "$secs" =~ ^[0-9]+$ ]] && [ "$secs" -lt "$min" ]; then
        echo "chain stopped: job $CHAIN_PREV (${prev%%|*}) ran ${secs}s, under ${min}s -- not starting a worker"
        exit 0
    fi
fi

B=$HOME/windcomfort/bin

# As ice_e3d_node.sbatch: the hand-off as E3D_CHUNK_HOURS (a build from before --chunk-hours would
# refuse the option; one from before the variable ignores it), and the fleet's target build taken
# from the broker before starting -- a requeued job takes it again.
chunk=${CHUNK_HOURS:-7}
if ! [[ "$chunk" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "CHUNK_HOURS=$chunk is not a number of hours (0 turns handing off off) -- not starting a worker"
    exit 2
fi
if [[ "$chunk" =~ ^0+([.]0+)?$ ]]; then unset E3D_CHUNK_HOURS; else export E3D_CHUNK_HOURS=$chunk; fi
timeout 900 "$B/E3D" node-release sync --exe "$B/E3D" \
    || echo "E3D was not updated (the line above says why); this job runs the build installed"

# From this job's own copy, as in ice_e3d_node.sbatch: run-sim-node starts each step by its own
# path, and another job's sync must not switch this one's build mid-case.
E3D=$B/E3D
if bin=$(mktemp -d "${TMPDIR:-/tmp}/e3d-bin.XXXXXX") && cp "$B/E3D" "$bin/E3D"; then
    E3D=$bin/E3D
else
    echo "could not copy E3D to node-local scratch; running $B/E3D itself, which another job's sync can replace mid-case"
fi

# What SLURM holds for these jobs, to the broker's Worker Fleet -- now, and every
# FLEET_REPORT_SECONDS (300; 0: never) while this job runs: a job that has not started has
# never called the broker, so the dashboard knows of it only through this (fleet_report.py).
fleet_report() { python3 "$B/fleet_report.py" --cluster Phoenix --name "${SLURM_JOB_NAME:-e3d-node-phoenix}" >/dev/null 2>&1; }
if command -v python3 >/dev/null && [ -f "$B/fleet_report.py" ]; then
    fleet_report &
    every=${FLEET_REPORT_SECONDS:-300}
    if [[ "$every" =~ ^[0-9]+$ ]] && [ "$every" -gt 0 ]; then
        ( while sleep "$every"; do fleet_report; done ) >/dev/null 2>&1 &
    fi
fi

# Rootless podman on node-local scratch, wiped at job end (each job pulls the image again).
export XDG_RUNTIME_DIR=${TMPDIR:-/tmp}/xdg; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export PACE_PSTORE=${TMPDIR:-/tmp}/pstore; mkdir -p "$PACE_PSTORE"
export EDDY3D_CONTAINER_CLI=$B/podman-pace.sh

# Warm podman before E3D probes it. At start E3D asks the engine `info` with a 5 s budget and
# pulls the Radiance image only when that answers; a cold rootless store on node-local scratch
# can take longer, and the node then runs the whole shift WITHOUT the thermal recipe
# (surf-1008/..., casebroker docs/thermal.md) and says so: "does not declare the thermal recipe".
# A shim that does not answer at all means no containers, so no cases of any recipe.
timeout 300 "$B/podman-pace.sh" info >/dev/null 2>&1 \
    || echo "podman did not answer within 300 s; E3D will find no container engine (docs/pace-hpc.md section 3)"

# --done on persistent scratch, so a part that had not finished uploading when the job was
# preempted is still there for the next job's sweep; --work node-local (Lustre hates OpenFOAM's
# thousands of small files), so a case in flight is continued from the broker, not resumed here.
DONE=${E3D_DONE:-/storage/scratch1/3/pkastner3/windcomfort/done}; mkdir -p "$DONE"
WORK=${TMPDIR:-/tmp}/e3d-work; mkdir -p "$WORK"

echo "node phoenix-${SLURM_JOB_ID} on $(hostname), $(nproc) cores, restart ${SLURM_RESTART_COUNT:-0}, work=$WORK," \
     "chain prev=${CHAIN_PREV:-none}, ${E3D_CHUNK_HOURS:+hands a case on after $E3D_CHUNK_HOURS h}${E3D_CHUNK_HOURS:-never hands a case on}"
exec "$E3D" run-sim-node --engine docker --cpus "${SLURM_NTASKS:-24}" --cluster Phoenix \
    --work "$WORK" --done "$DONE" ${MAX_CASES:+--max-cases "$MAX_CASES"} --drain --max-idle-polls 10
