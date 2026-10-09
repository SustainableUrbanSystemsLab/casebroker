#!/bin/bash
#SBATCH -J e3d-node-ice
#SBATCH -N 1 -n 24
#SBATCH -t 08:00:00
#SBATCH -p ice-cpu
#SBATCH -q coe-ice
#SBATCH -A coa
#SBATCH --signal=B:TERM@180
#SBATCH -o logs/e3d_node_%j.out
#
# One E3D simulation node on ICE: the only thing installed here is the E3D binary, paired with the
# broker as "ice" (E3D setup-sim-node). Submit with slurm/submit_workers.sh, not by hand.
# docs/pace-hpc.md section 8 has the setup and what to watch.
#
# 24 ranks is the most one PACE job gets. A direction takes ~22 min on it, so a 32-direction case is
# ~12 h and outlives this 8 h shift. That is fine: every finished direction is sent to the broker as
# it finishes, and after CHUNK_HOURS (default 7) of one lease the node hands the case on at the next
# direction boundary, and the next worker continues it from the broker's copy of the mesh and the
# directions. Should a direction still be running at the wall, SIGTERM (180 s before it) releases the
# case with its attempt refunded and only that direction is lost.
#
# Not preemptible (coe-ice has no embers), so no --requeue.
#
#   CHUNK_HOURS=6.5   hand off after 6.5 h instead; CHUNK_HOURS=0 never hands off (a case then runs
#                     until the wall's SIGTERM takes the direction in flight)

set -uo pipefail

# Chain guard. submit_workers.sh passes CHAIN_PREV, the job this one follows. If that job ended
# within CHAIN_MIN_SECONDS something is wrong -- a broken shim, a node that lost its pairing, an
# empty queue, a scancel -- so end quietly instead of leasing cases only to fail them. This job
# then ends in seconds too, and the next in the chain sees exactly that: the rest of the chain
# falls through without ever leasing. (scancel one pending job of a chain to stop the rest.)
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

# Hand the case on at a direction boundary before the wall. Passed as E3D_CHUNK_HOURS, not as
# --chunk-hours: an E3D from before the option refuses the whole command line, and this job would
# end in seconds; one from before the variable ignores it and runs as it always did.
chunk=${CHUNK_HOURS:-7}
if ! [[ "$chunk" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "CHUNK_HOURS=$chunk is not a number of hours (0 turns handing off off) -- not starting a worker"
    exit 2
fi
if [[ "$chunk" =~ ^0+([.]0+)?$ ]]; then unset E3D_CHUNK_HOURS; else export E3D_CHUNK_HOURS=$chunk; fi

# Take the fleet's target build from the broker first: a job runs E3D unsupervised, so nothing
# else ever moves it to a new build. sync verifies the file against the hash the broker's admin
# registered and replaces E3D by a rename, which leaves a job already running the old one
# undisturbed. An E3D from before sync, a broker that cannot be reached or holds no file for the
# target: this job runs the E3D that is installed, as before.
timeout 900 "$B/E3D" node-release sync --exe "$B/E3D" \
    || echo "E3D was not updated (the line above says why); this job runs the build installed"

# Rootless podman needs a runtime dir and a graph root; both live on node-local scratch (wiped at
# job end, so each job pulls the image again). The shim adds the rest.
export XDG_RUNTIME_DIR=${TMPDIR:-/tmp}/xdg; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export PACE_PSTORE=${TMPDIR:-/tmp}/pstore; mkdir -p "$PACE_PSTORE"
export EDDY3D_CONTAINER_CLI=$B/podman-pace.sh

# --done is on the persistent scratch so a part that did not finish uploading before the wall is
# still there for the next job's sweep. --work is node-local: a case in flight is not resumed here,
# the next worker continues it from the broker.
DONE=${E3D_DONE:-/storage/ice1/0/3/pkastner3/windcomfort/done}; mkdir -p "$DONE"
WORK=${TMPDIR:-/tmp}/e3d-work; mkdir -p "$WORK"

echo "node ice-${SLURM_JOB_ID} on $(hostname), $(nproc) cores, work=$WORK, chain prev=${CHAIN_PREV:-none}," \
     "${E3D_CHUNK_HOURS:+hands a case on after $E3D_CHUNK_HOURS h}${E3D_CHUNK_HOURS:-never hands a case on}"
exec "$B/E3D" run-sim-node --engine docker --cpus "${SLURM_NTASKS:-24}" --cluster ICE \
    --work "$WORK" --done "$DONE" ${MAX_CASES:+--max-cases "$MAX_CASES"} --drain --max-idle-polls 10
