#!/bin/bash
#SBATCH -J windworker-ice
#SBATCH -N 1 -n 24
#SBATCH -t 08:00:00
#SBATCH -p ice-cpu
#SBATCH -q coe-ice
#SBATCH -A coa
#SBATCH --array=1-2%2
#SBATCH --signal=B:TERM@180
#SBATCH -o worker_%A_%a.out
#
# The ICE twin of phoenix_worker.sbatch. Same worker, same runner, same broker --
# only the scheduler's vocabulary differs, and every difference below is a fact
# about ICE rather than a preference:
#
#   partition  ice-cpu    (Phoenix's cpu-small does not exist here)
#   account    coa        (not gts-pkastner3)
#   QOS        coe-ice    (there is NO embers on ICE, so nothing is preemptible)
#   wall       18 h max   (Phoenix allows 21 days; 8 h is a shift either way)
#   storage    $HOME      48 TB free, and ICE has no /storage/scratch1 for this
#                         user -- the "ICE scratch is full" note in AGENTS.md
#                         refers to a path that is not what this uses.
#
# Not preemptible means --requeue and the embers reasoning do not apply. The
# SIGTERM handler is kept anyway: it is what releases a lease on ANY clean
# shutdown, including hitting the wall clock, and a released lease returns to the
# pool immediately instead of waiting out its TTL.

set -uo pipefail

: "${CASEBROKER_URL:?set CASEBROKER_URL}"
: "${CASEBROKER_TOKEN:?set CASEBROKER_TOKEN}"

WC=${WIND_ROOT:-$HOME/windcomfort}
REPO=${WIND_REPO:-$WC/repos/JP-Wind-ML-Comparison}
export WIND_ROOT="$WC"
export EDDY3D_CLI=${EDDY3D_CLI:-$WC/bin/eddy3d-cli}
export WIND_NP=${SLURM_NTASKS:-24}
export WIND_IMAGE=${WIND_IMAGE:-docker.io/dicehub/openfoam:12}
export CASEBROKER_CLUSTER=${CASEBROKER_CLUSTER:-ICE}

export CASEBROKER_WORKER_ID="ice-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}"

echo "worker $CASEBROKER_WORKER_ID on $(hostname), $(nproc) cores, TMPDIR=${TMPDIR:-unset}"
df -h "${TMPDIR:-/tmp}" | tail -1

MAX_HOURS=${MAX_HOURS:-7.0}

cd "$REPO/benchmark/casebroker" || exit 1

timeout "${MAX_HOURS}h" uv run python -m casebroker.worker \
    --broker "$CASEBROKER_URL" \
    --token "$CASEBROKER_TOKEN" \
    --worker-id "$CASEBROKER_WORKER_ID" \
    --runner "$REPO/benchmark/casebroker/runner/run_case.sh" \
    --lease-seconds 1800 \
    --heartbeat-seconds 300 \
    --idle-backoff 120
rc=$?

if [ $rc -eq 124 ]; then echo "worker reached MAX_HOURS=$MAX_HOURS; exiting cleanly"; rc=0; fi
echo "worker $CASEBROKER_WORKER_ID exit=$rc"
exit $rc
