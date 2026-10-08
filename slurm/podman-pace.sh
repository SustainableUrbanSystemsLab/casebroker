#!/bin/bash
# EDDY3D_CONTAINER_CLI shim: E3D calls this wherever it would call `podman`, on PACE (ICE / Phoenix).
# It adds the three things rootless podman needs there (docs/pace-hpc.md section 3):
#
#   storage   under PACE_PSTORE (node-local scratch), through fuse-overlayfs: the account has no
#             /etc/subuid range, so podman falls back to a single mapping.
#   --user    0:0 for `run`: the image's uid 1000 cannot be mapped under that single mapping. This
#             is the host user's own uid, not real root.
#   OMPI_*    OpenMPI refuses to start as uid 0 unless told otherwise; without these two variables
#             mpirun exits right after decomposePar and every solve fails (seen on ICE, 2026-10-07).
#
# XDG_RUNTIME_DIR and PACE_PSTORE are set by ice_e3d_node.sbatch.
: "${PACE_PSTORE:?}"; : "${XDG_RUNTIME_DIR:?}"
base=(podman --root "$PACE_PSTORE" --runroot "$XDG_RUNTIME_DIR/run" --storage-driver overlay
      --storage-opt overlay.ignore_chown_errors=true --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs)
if [ "${1:-}" = run ]; then
    shift
    exec "${base[@]}" run --user 0:0 -e HOME=/home/openfoam \
        -e OMPI_ALLOW_RUN_AS_ROOT=1 -e OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 "$@"
fi
exec "${base[@]}" "$@"
