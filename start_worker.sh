#!/bin/bash
# Start one worker on this machine, in the foreground. Ctrl-C stops it cleanly:
# the runner saves a checkpoint and the worker releases the lease, so the case
# resumes here next time rather than restarting somewhere else.
#
#   ./start_worker.sh [machine.env] [extra worker args...]
#
# Linux, WSL and git-bash. Windows without bash: start_worker.ps1.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVFILE="${1:-$HERE/machine.env}"
[ $# -gt 0 ] && shift
if [ -f "$ENVFILE" ]; then
    # KEY=VALUE lines, no shell expansion of the values (a token is a token).
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"; line="${line%"${line##*[![:space:]]}"}"
        [ -z "$line" ] && continue
        export "$line"
    done < "$ENVFILE"
else
    echo "no $ENVFILE -- copy machine.env.example and fill it in, or export the variables" >&2
fi
: "${CASEBROKER_URL:?set CASEBROKER_URL}"
: "${CASEBROKER_TOKEN:?set CASEBROKER_TOKEN}"
: "${WIND_ROOT:?set WIND_ROOT}"
: "${EDDY3D_CLI:?set EDDY3D_CLI}"
export WIND_CASES="${WIND_CASES:-$WIND_ROOT/cases}"
export WIND_DONE="${WIND_DONE:-$WIND_ROOT/done}"
mkdir -p "$WIND_ROOT" "$WIND_CASES" "$WIND_DONE"

# uv lives in ~/.local/bin, which a non-login shell (a SLURM step, a double-clicked
# script) does not put on PATH.
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || { echo "uv not found: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 127; }

cd "$HERE" || exit 1
echo "worker ${CASEBROKER_WORKER_ID:-<auto>} on $(hostname): runtime=${WIND_RUNTIME:-auto} ranks=${WIND_NP:-24} root=$WIND_ROOT"
exec uv run python -m casebroker.worker \
    --broker "$CASEBROKER_URL" --token "$CASEBROKER_TOKEN" \
    ${CASEBROKER_WORKER_ID:+--worker-id "$CASEBROKER_WORKER_ID"} \
    --runner "$HERE/runner/run_case.sh" \
    --cases-dir "$WIND_CASES" \
    --lease-seconds 1800 --heartbeat-seconds 300 --idle-backoff 120 \
    "$@"
