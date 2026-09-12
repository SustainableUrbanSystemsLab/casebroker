#!/bin/bash
# Master-side collector for the PACE clusters: pull finished case archives out
# of each cluster's $WIND_DONE into the master's done/ folder over SSH.
#
# Why pull rather than push: PACE compute nodes can open connections OUT but
# cannot be reached, and a long-running Syncthing daemon does not fit shared
# login nodes or job-lifetime compute nodes. The master already has SSH access
# to the login nodes (Windows: through the native OpenSSH client, see
# docs/pace-hpc.md), and the archives are single files written into place
# atomically, so a plain rsync is safe and resumable.
#
#   scripts/pull_done.sh <local done dir> <host:remote done dir> [...]
#   PULL_REMOVE=1 scripts/pull_done.sh ...   # delete on the cluster after a verified copy
#
# Run it on a timer (Task Scheduler / cron on the master). Only *.tar.gz are
# taken; .tmp/ (archives still being written) is never touched.
set -uo pipefail
[ $# -ge 2 ] || { echo "usage: $0 <local done dir> <host:remote done dir> [more...]" >&2; exit 2; }
LOCAL="$1"; shift
mkdir -p "$LOCAL" || exit 1
rc=0
for src in "$@"; do
    echo "== $src"
    # --partial keeps a broken transfer for the next run to finish; --ignore-existing
    # because a finished archive never changes, so a name already here is done.
    if rsync -av --partial --ignore-existing --include='*.tar.gz' --exclude='*' \
            "$src/" "$LOCAL/"; then
        if [ "${PULL_REMOVE:-0}" = 1 ]; then
            # Remove only what is now verifiably here, by name and size.
            host="${src%%:*}"; rdir="${src#*:}"
            for f in "$LOCAL"/*.tar.gz; do
                [ -f "$f" ] || continue
                n=$(basename "$f"); sz=$(wc -c < "$f" | tr -d ' ')
                ssh -o BatchMode=yes "$host" "[ -f '$rdir/$n' ] && [ \"\$(wc -c < '$rdir/$n' | tr -d ' ')\" = '$sz' ] && rm -f '$rdir/$n'" 2>/dev/null
            done
        fi
    else
        echo "pull from $src failed" >&2; rc=1
    fi
done
exit $rc
