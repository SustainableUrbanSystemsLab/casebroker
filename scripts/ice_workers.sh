#!/bin/bash
# Queue E3D simulation-node workers on PACE ICE from this machine: copies the job files in slurm/
# to ICE, then runs slurm/submit_workers.sh there with the arguments given.
#
#   scripts/ice_workers.sh chain 20          20 shifts back to back: one worker at a time, each
#                                            picks up the case the last one released
#   scripts/ice_workers.sh parallel 4        4 workers at once
#   scripts/ice_workers.sh lanes 4 10        4 parallel lanes of 10 chained shifts each
#   scripts/ice_workers.sh chain 20 --dry-run
#
# Needs the Georgia Tech VPN and a login (password + Duo). To type those once, open a master
# connection first and leave it running:
#   ssh -M -S /tmp/ice.sock -o ControlPersist=12h pkastner3@login-ice.pace.gatech.edu
# ICE_HOST and ICE_SOCK override the login and the socket path. The E3D binary and the pairing
# ("ice") are set up once: docs/pace-hpc.md section 8.
set -euo pipefail

host=${ICE_HOST:-pkastner3@login-ice.pace.gatech.edu}
sock=${ICE_SOCK:-/tmp/ice.sock}      # used only if a master connection is open there
src=$(cd "$(dirname "${BASH_SOURCE[0]}")/../slurm" && pwd)
opts=(-o "ControlPath=$sock")

scp -q "${opts[@]}" "$src/submit_workers.sh" "$src/ice_e3d_node.sbatch" "$src/podman-pace.sh" "$host:windcomfort/bin/"
# shellcheck disable=SC2029  # the arguments are meant to expand here, quoted for the remote shell
ssh "${opts[@]}" "$host" "chmod +x ~/windcomfort/bin/submit_workers.sh ~/windcomfort/bin/podman-pace.sh && ~/windcomfort/bin/submit_workers.sh $(printf '%q ' "$@")"
