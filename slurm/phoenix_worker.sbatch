#!/bin/bash
#SBATCH -J windworker
#SBATCH -N 1 -n 24
#SBATCH -t 08:00:00
#SBATCH -p cpu-small
#SBATCH -q embers
#SBATCH -A gts-pkastner3
#SBATCH --array=1-20%20
#SBATCH --requeue
#SBATCH --signal=B:TERM@180
#SBATCH -o worker_%A_%a.out
#
# A pool of pull-based workers on Phoenix. Each array task leases cases from the
# broker, runs them, reports, and repeats until its wall clock runs low or the
# queue drains.
#
# Why this shape rather than a case-per-array-task:
#
#   * `embers` is free but PREEMPTIBLE after one hour, and jobs are requeued. A
#     statically assigned case dies with its task; a leased one returns to the pool
#     by itself. --signal=B:TERM@180 gives the worker three minutes' warning, and
#     casebroker's SIGTERM handler releases the lease immediately with its retry
#     REFUNDED, rather than leaving the case stranded until the lease TTL lapses.
#   * Cases are wildly unequal (a dense LCZ 1 tile against an exurban LCZ 9 one),
#     so pull beats push: a fast worker simply takes more.
#   * %20 caps concurrency so the campaign shares the cluster politely. Raise it to
#     go faster; nothing else changes.
#
# Prerequisites, all verified on Phoenix 2026-09-07:
#   - /usr/bin/podman 5.4.0, no subuid range (see run_case.sh for the workaround)
#   - compute nodes have outbound internet, so the broker is reachable directly
#   - $TMPDIR is 1.4 TB node-local scratch
#
# Submit with:
#   CASEBROKER_URL=https://broker.example.org CASEBROKER_TOKEN=... \
#     sbatch --export=ALL phoenix_worker.sbatch

set -uo pipefail

: "${CASEBROKER_URL:?set CASEBROKER_URL}"
: "${CASEBROKER_TOKEN:?set CASEBROKER_TOKEN}"

WC=${WIND_ROOT:-/storage/scratch1/3/pkastner3/windcomfort/v2}
REPO=${WIND_REPO:-$WC/repo/JP-Wind-ML-Comparison}
export WIND_ROOT="$WC"
export EDDY3D_CLI=${EDDY3D_CLI:-$WC/bin/e3d}
export WIND_NP=${SLURM_NTASKS:-24}
export WIND_IMAGE=${WIND_IMAGE:-docker.io/dicehub/openfoam:12}
export WIND_CASES=${WIND_CASES:-$WC/cases}
export WIND_DONE=${WIND_DONE:-$WC/done}

# One worker id per array task, stable across a requeue so the broker's worker
# table shows a task's whole history rather than a new row each preemption.
# Names this cluster on the dashboard and in every case's metrics. SLURM sets
# SLURM_CLUSTER_NAME on PACE, but the fleet reporter below runs whether or not it
# does, and an unnamed cluster would report as an empty string.
export CASEBROKER_CLUSTER=${CASEBROKER_CLUSTER:-${SLURM_CLUSTER_NAME:-Phoenix}}
export CASEBROKER_WORKER_ID="phoenix-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}"

echo "worker $CASEBROKER_WORKER_ID on $(hostname), $(nproc) cores, TMPDIR=${TMPDIR:-unset}"
df -h "${TMPDIR:-/tmp}" | tail -1

# Leave a margin under the 8 h wall clock: a case must be able to finish, and the
# worker exits cleanly rather than being killed mid-solve.
MAX_HOURS=${MAX_HOURS:-7.0}

# uv lives in ~/.local/bin, which a NON-LOGIN shell does not put on PATH -- and a
# SLURM batch script is exactly that. The first ICE submission died in one second
# with "timeout: failed to run command 'uv': No such file or directory" (exit
# 127) while uv sat installed and working on the very same filesystem. Resolved
# here rather than relying on the node's profile, and checked explicitly so a
# missing uv fails with a sentence instead of a bare 127.
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || {
    echo "FATAL: uv not found on PATH. Install with:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=\"$HOME/.local/bin\" sh" >&2
    exit 127
}

cd "$REPO/benchmark/casebroker" || exit 1

# Keep the dashboard's queue card fresh. PACE forbids crontab ("You (pkastner3)
# are not allowed to use this program"), so the reporting rides along with the
# job that is already running: every five minutes for as long as this worker
# lives. A worker is exactly the right reporter -- if none is running there is
# nothing to report, and the card correctly goes stale.
#
# The token goes through the environment, not --token, so it stays out of the
# process table. Failures are swallowed: a queue report must never be able to
# take down the worker it is reporting on.
export CASEBROKER_WRITE_TOKENS="$CASEBROKER_TOKEN"
( while true; do
      uv run casebroker fleet --broker "$CASEBROKER_URL"           --cluster "$CASEBROKER_CLUSTER" >/dev/null 2>&1 || true
      sleep 300
  done ) &
FLEET_REPORTER=$!
trap 'kill "$FLEET_REPORTER" 2>/dev/null' EXIT


# The lease is short relative to the job but long relative to a heartbeat: a dead
# node's case returns to the pool in 30 minutes, while a live worker holds a case
# for as long as it needs by renewing every 5.
timeout "${MAX_HOURS}h" uv run python -m casebroker.worker \
    --broker "$CASEBROKER_URL" \
    --token "$CASEBROKER_TOKEN" \
    --worker-id "$CASEBROKER_WORKER_ID" \
    --runner "$REPO/benchmark/casebroker/runner/run_case.sh" \
    --cases-dir "$WIND_CASES" \
    --lease-seconds 1800 \
    --heartbeat-seconds 300 \
    --idle-backoff 120
rc=$?

# 124 is timeout's own code: the worker hit MAX_HOURS, which is the normal end of a
# shift, not a failure. Anything the worker was holding is released by its SIGTERM
# handler on the way out.
if [ $rc -eq 124 ]; then echo "worker reached MAX_HOURS=$MAX_HOURS; exiting cleanly"; rc=0; fi
echo "worker $CASEBROKER_WORKER_ID exit=$rc"
exit $rc
