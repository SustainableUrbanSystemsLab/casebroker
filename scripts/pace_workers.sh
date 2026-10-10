#!/bin/bash
# Queue E3D simulation-node workers on PACE (ICE or Phoenix) from this machine: copies the job
# files in slurm/ to the cluster, then runs slurm/submit_workers.sh there with the arguments given.
#
#   scripts/pace_workers.sh ice chain 20          20 ICE shifts back to back: one worker at a time,
#                                                 each picks up the case the last one released
#   scripts/pace_workers.sh ice parallel 4        4 ICE workers at once
#   scripts/pace_workers.sh ice lanes 4 10        4 parallel lanes of 10 chained shifts each
#   scripts/pace_workers.sh phoenix parallel 10   10 nodes on Phoenix's free, preemptible embers QOS
#   scripts/pace_workers.sh ice chain 20 --dry-run
#
# Needs the Georgia Tech VPN and a login (password + Duo). To type those once, open a master
# connection first and leave it running:
#   ssh -M -S /tmp/ice.sock -o ControlPersist=12h pkastner3@login-ice.pace.gatech.edu
#   ssh -M -S /tmp/phoenix.sock -o ControlPersist=12h pkastner3@login-phoenix.pace.gatech.edu
# PACE_HOST and PACE_SOCK override the login and the socket path (ICE_HOST / ICE_SOCK still work
# for ice). The E3D binary and the pairing ("ice", "phoenix": each cluster has its own home and
# its own credential) are set up once per cluster: docs/pace-hpc.md section 8.
set -euo pipefail

cluster=${1:-}
case $cluster in
    ice)     login=login-ice.pace.gatech.edu ;;
    phoenix) login=login-phoenix.pace.gatech.edu ;;
    *)       sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
shift
default_host="pkastner3@$login"
[ "$cluster" = ice ] && default_host=${ICE_HOST:-$default_host}
host=${PACE_HOST:-$default_host}
default_sock="/tmp/$cluster.sock"
[ "$cluster" = ice ] && default_sock=${ICE_SOCK:-$default_sock}
sock=${PACE_SOCK:-$default_sock}      # used only if a master connection is open there
script="${cluster}_e3d_node.sbatch"
src=$(cd "$(dirname "${BASH_SOURCE[0]}")/../slurm" && pwd)
opts=(-o "ControlPath=$sock")

scp -q "${opts[@]}" "$src/submit_workers.sh" "$src/$script" "$src/podman-pace.sh" "$src/fleet_report.py" "$src/upload_done.sbatch" "$host:windcomfort/bin/"
# shellcheck disable=SC2029  # the arguments are meant to expand here, quoted for the remote shell
ssh "${opts[@]}" "$host" "chmod +x ~/windcomfort/bin/submit_workers.sh ~/windcomfort/bin/podman-pace.sh && ~/windcomfort/bin/submit_workers.sh $(printf '%q ' "$@") --script ~/windcomfort/bin/$script"
