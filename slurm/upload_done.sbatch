#!/bin/bash
#SBATCH -J e3d-upload-done
#SBATCH -N 1 -n 2
#SBATCH -t 12:00:00
#SBATCH -p ice-cpu
#SBATCH -q coe-ice
#SBATCH -A coa
#SBATCH -o logs/e3d_upload_done_%j.out
#
# Before ICE's scratch is purged (the end of every semester): send the broker every part of every
# case in the done folder that it does not hold yet -- each case's mesh, its directions, its archive --
# and wait until it has. Leases nothing. Needs an E3D with `node-parts sweep` (Eddy3D dev 2026-10-10 on).
# Submit from ~/windcomfort (scripts/pace_workers.sh copies this file there):
#     sbatch ~/windcomfort/bin/upload_done.sbatch
# then, from your own machine:  casebroker custody --broker https://casebroker.eddy3d.com --username <you> --under /storage/ice1
# Exit 0 = the broker holds everything this folder could give it; 1 = submit it again (it resumes).

set -uo pipefail
B=$HOME/windcomfort/bin
DONE=${E3D_DONE:-/storage/ice1/0/3/pkastner3/windcomfort/done}
echo "upload of $DONE to the broker, job ${SLURM_JOB_ID:-?} on $(hostname)"
exec "$B/E3D" node-parts sweep --done "$DONE" --again --hours 11
