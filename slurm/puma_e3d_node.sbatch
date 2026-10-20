#!/bin/bash
#SBATCH -J e3d-node-puma
#SBATCH -N 1 -n 24
#SBATCH -t 08:00:00
#SBATCH --partition=standard
#SBATCH --account=YOUR_GROUP        # verify with `va` / `hpc_access` -- this is a placeholder, not PACE's
#SBATCH --signal=B:TERM@180
#SBATCH -o logs/e3d_node_puma_%j.out
#
# One E3D simulation node on University of Arizona's Puma, run through APPTAINER -- not Podman.
# Podman is merely present on Puma's compute-node image; it is not what the cluster provisions or
# supports (hpcdocs.hpc.arizona.edu/software/containers/containers_on_hpc names Apptainer only,
# "installed on the operating systems of all HPC compute nodes"). A rootless Podman run on a shared
# account fails immediately -- no subuid/subgid range, and the storage root defaults to $HOME,
# which does not support the xattrs overlay needs. Apptainer needs neither: no user-namespace
# remapping, images are single read-only .sif files. See casebroker docs/fleet.md "Rootless podman
# on a shared cluster" for the incident this script exists to avoid (yang-24315048-0, 2026-10-20:
# 7 cases failed identically at 01_blockMesh within 17 minutes, auto-drained before any were lost).
#
# This is the whole fix, and it needs NO env vars the way ice_e3d_node.sbatch's podman-pace.sh
# shim does (no XDG_RUNTIME_DIR, no storage root, no --user 0:0): --engine apptainer is the only
# thing a Puma job needs to say. docs/pace-hpc.md section 8 has the general node setup
# (pairing with E3D setup-sim-node); this script is its Puma/Apptainer twin.
#
# Fill in --account above before submitting -- "YOUR_GROUP" is a placeholder, not a real Puma
# group, unlike this file's PACE siblings which carry a real (if not yours) account. Check
# `--partition` too: "standard" is Puma's usual CPU partition, but confirm with your allocation.
#
# Submit directly (sbatch puma_e3d_node.sbatch), or get the chain/parallel/lanes shapes
# docs/fleet.md describes for PACE for free: submit_workers.sh already takes any sbatch script by
# name, so `~/windcomfort/bin/submit_workers.sh --script puma_e3d_node.sbatch chain 20` works
# unmodified once this file, submit_workers.sh and fleet_report.py are all in that directory.
# scripts/pace_workers.sh itself stays PACE-only (it SSHes into Patrick's own PACE login with his
# account) -- Puma needs Yang's own login, so she runs submit_workers.sh herself on Puma.

set -uo pipefail

# Chain guard, same as ice_e3d_node.sbatch / phoenix_e3d_node.sbatch: a predecessor that ended
# within CHAIN_MIN_SECONDS means something is wrong (lost pairing, empty queue, broken binary), so
# end quietly instead of leasing cases only to fail them.
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

# Hand the case on at a direction boundary before the wall, as the PACE scripts do. Passed as
# E3D_CHUNK_HOURS, not as --chunk-hours: an E3D from before the option refuses the whole command
# line, and this job would end in seconds; one from before the variable ignores it.
chunk=${CHUNK_HOURS:-7}
if ! [[ "$chunk" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "CHUNK_HOURS=$chunk is not a number of hours (0 turns handing off off) -- not starting a worker"
    exit 2
fi
if [[ "$chunk" =~ ^0+([.]0+)?$ ]]; then unset E3D_CHUNK_HOURS; else export E3D_CHUNK_HOURS=$chunk; fi

timeout 900 "$B/E3D" node-release sync --exe "$B/E3D" \
    || echo "E3D was not updated (the line above says why); this job runs the build installed"

# From this job's own copy, on node-local scratch: run-sim-node starts every step by its own path
# again, so a job running $B/E3D itself would switch builds mid-case the moment another job's sync
# swapped a new one in.
E3D=$B/E3D
if bin=$(mktemp -d "${TMPDIR:-/tmp}/e3d-bin.XXXXXX") && cp "$B/E3D" "$bin/E3D"; then
    E3D=$bin/E3D
else
    echo "could not copy E3D to node-local scratch; running $B/E3D itself, which another job's sync can replace mid-case"
fi

fleet_report() { python3 "$B/fleet_report.py" --cluster Puma --name "${SLURM_JOB_NAME:-e3d-node-puma}" >/dev/null 2>&1; }
if command -v python3 >/dev/null && [ -f "$B/fleet_report.py" ]; then
    fleet_report &
    every=${FLEET_REPORT_SECONDS:-300}
    if [[ "$every" =~ ^[0-9]+$ ]] && [ "$every" -gt 0 ]; then
        ( while sleep "$every"; do fleet_report; done ) >/dev/null 2>&1 &
    fi
fi

# EDDY3D_APPTAINER_CACHE: unset here, so ApptainerImageCache.CacheDir() defaults to node-local
# scratch ($TMPDIR, wiped at job end -- each job re-pulls the ~GB image, same tradeoff the PACE
# podman scripts already accept). Point it at group/project storage instead to avoid the re-pull
# on every job -- fill in your own allocation's path; there is no single answer across accounts.
# export EDDY3D_APPTAINER_CACHE=/xdisk/YOUR_GROUP/$USER/eddy3d-apptainer

# --done needs somewhere OUTSIDE your 50 GB home quota for archives to accumulate in -- fill in
# your own group/project storage path (hpcdocs.hpc.arizona.edu/resources/storage_transfer). --work
# is node-local scratch on purpose: a case in flight is never resumed here, the next worker
# continues it from the broker's copy.
DONE=${E3D_DONE:?set E3D_DONE to a done-archive path outside your home quota, e.g. your group's storage}
mkdir -p "$DONE"
WORK=${TMPDIR:-/tmp}/e3d-work; mkdir -p "$WORK"

echo "node puma-${SLURM_JOB_ID} on $(hostname), $(nproc) cores, work=$WORK, chain prev=${CHAIN_PREV:-none}," \
     "${E3D_CHUNK_HOURS:+hands a case on after $E3D_CHUNK_HOURS h}${E3D_CHUNK_HOURS:-never hands a case on}"
exec "$E3D" run-sim-node --engine apptainer --cpus "${SLURM_NTASKS:-24}" --cluster Puma \
    --work "$WORK" --done "$DONE" ${MAX_CASES:+--max-cases "$MAX_CASES"} --drain --max-idle-polls 10
