#!/bin/bash
# Submit E3D simulation-node jobs (ice_e3d_node.sbatch, phoenix_e3d_node.sbatch) as a chain, in
# parallel, or both. Run on the cluster login node; scripts/pace_workers.sh does that from your own
# machine.
#
#   submit_workers.sh chain N       N jobs, each starting when the one before has ENDED (wall clock,
#                                   failure, scancel -- any way). One worker at a time: a case that
#                                   outlives a shift is continued by the next job from the broker's
#                                   copy of its mesh and finished directions.
#   submit_workers.sh parallel N    N independent jobs: N workers at once (N x 24 cores).
#   submit_workers.sh lanes L N     L parallel lanes, each a chain of N jobs
#                                   (chain N is lanes 1 N, parallel N is lanes N 1).
#
#   --after JOBID   the first job of every lane waits for JOBID to end: extends a chain
#   --script FILE   the sbatch script (default: ice_e3d_node.sbatch beside this file;
#                   phoenix_e3d_node.sbatch on Phoenix, where `parallel N` is the usual shape:
#                   embers jobs are preempted and requeue themselves)
#   --chunk-hours H hand a case on after H hours of one lease (default 7, under the 8 h wall;
#                   0: never, the wall's SIGTERM then takes the direction in flight)
#   --dry-run       print the sbatch commands and submit nothing
#
# Prints every job id and each lane's last one; pass that as --after to add more to the lane.
# A job whose predecessor ended in under CHAIN_MIN_SECONDS (default 900) does not start a worker,
# and neither does anything after it (see ice_e3d_node.sbatch): scancel one pending job to stop
# the rest of a lane. To stop everything: scancel -u $USER -n e3d-node-ice (or e3d-node-phoenix)
# (a running job gets SIGTERM and releases its case).

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
script="$here/ice_e3d_node.sbatch"
after=""
chunk=""
dry=0
max_total=200

usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2; }
is_count() { [[ "${1:-}" =~ ^[1-9][0-9]*$ ]]; }

[ $# -ge 1 ] || usage
mode=$1; shift
pos=()
while [ $# -gt 0 ]; do
    case $1 in
        --after)   after=${2:?--after needs a job id}; shift 2 ;;
        --script)  script=${2:?--script needs a file}; shift 2 ;;
        --chunk-hours) chunk=${2:?--chunk-hours needs hours}; shift 2 ;;
        --dry-run) dry=1; shift ;;
        -h|--help) usage ;;
        *)         pos+=("$1"); shift ;;
    esac
done

case $mode in
    chain)    lanes=1;           n=${pos[0]:-} ;;
    parallel) lanes=${pos[0]:-}; n=1 ;;
    lanes)    lanes=${pos[0]:-}; n=${pos[1]:-} ;;
    *)        usage ;;
esac
is_count "$lanes" && is_count "$n" || usage
[ $((lanes * n)) -le "$max_total" ] || { echo "refusing $((lanes * n)) jobs (limit $max_total)" >&2; exit 1; }
[ -f "$script" ] || { echo "no such sbatch script: $script" >&2; exit 1; }
[ -z "$after" ] || [[ "$after" =~ ^[0-9]+$ ]] || { echo "--after wants a job id, got '$after'" >&2; exit 1; }
[ -z "$chunk" ] || [[ "$chunk" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "--chunk-hours wants hours (0: never), got '$chunk'" >&2; exit 1; }

name=$(awk '/^#SBATCH[[:space:]]+-J[[:space:]]/{print $3; exit}' "$script")
pairing=${name#e3d-node-}          # the name this cluster's node pairs as: ice, phoenix

if [ "$dry" = 0 ]; then
    # What the job needs on this cluster, checked here so a missing piece is one sentence now and
    # not a chain of jobs that all end in seconds.
    [ -x "$HOME/windcomfort/bin/E3D" ] || { echo "no $HOME/windcomfort/bin/E3D (docs/pace-hpc.md section 8)" >&2; exit 1; }
    [ -x "$HOME/windcomfort/bin/podman-pace.sh" ] || { echo "no $HOME/windcomfort/bin/podman-pace.sh (copy slurm/podman-pace.sh there)" >&2; exit 1; }
    [ -s "${EDDY3D_NODE_DIR:-$HOME/.local/share/Eddy3D/node}/credential.json" ] \
        || { echo "this account is not paired: $HOME/windcomfort/bin/E3D setup-sim-node <broker-url> --name $pairing --no-browser" >&2; exit 1; }
    # The sbatch script writes its log to logs/ under the directory it is submitted from.
    cd "$HOME/windcomfort"
    mkdir -p logs
    queued=$(squeue -h -u "$USER" -n "$name" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$queued" != 0 ]; then
        echo "note: $queued '$name' job(s) already queued or running; these are added to them"
    fi
fi

for lane in $(seq 1 "$lanes"); do
    prev=$after
    ids=()
    for i in $(seq 1 "$n"); do
        args=(--parsable "--export=ALL,CHAIN_PREV=$prev${chunk:+,CHUNK_HOURS=$chunk}")
        if [ -n "$prev" ]; then args+=("--dependency=afterany:$prev"); fi
        if [ "$dry" = 1 ]; then
            echo "sbatch ${args[*]} $script"
            id="dry-$lane-$i"
        else
            if ! out=$(sbatch "${args[@]}" "$script"); then
                echo "sbatch refused job $i of lane $lane; lane $lane so far: ${ids[*]:-none}" >&2
                exit 1
            fi
            id=${out%%;*}      # --parsable prints "jobid;cluster" on a federated cluster
        fi
        ids+=("$id")
        prev=$id
    done
    printf 'lane %s (%s job%s): %s\n' "$lane" "$n" "$([ "$n" = 1 ] || echo s)" "$(IFS=,; echo "${ids[*]}" | sed 's/,/ -> /g')"
    echo "  last: $prev   (add more to this lane: --after $prev)"
done

if [ "$dry" = 0 ]; then
    # What SLURM now holds for these jobs, to the broker's Worker Fleet: a queued job has never
    # called the broker, so it shows there only through this report (fleet_report.py, which each
    # job repeats while it runs). The cluster is the one the job script's node names.
    cluster=$(grep -oE -- '--cluster [A-Za-z0-9_-]+' "$script" | head -1 | awk '{print $2}')
    if command -v python3 >/dev/null && [ -f "$here/fleet_report.py" ] && [ -n "$cluster" ]; then
        python3 "$here/fleet_report.py" --cluster "$cluster" --name "$name" \
            || echo "the queue was not reported to the broker (the line above says why)"
    fi
    squeue -u "$USER" -n "$name" -o '%.10i %.9T %.10M %.11L %R' | head -$((lanes * n + 1))
fi
