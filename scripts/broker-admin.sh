#!/bin/bash
# Run a casebroker ADMIN command with the credentials in admin.env, so nothing
# prompts: `scripts/broker-admin.sh release target 1.17.0.827+529fa324 --apply case`
#
# Everything after the script name is passed to the CLI verbatim, and --broker,
# --username and --password-stdin are appended from admin.env. The password
# goes in on STDIN and never into argv, where `ps` would show it to every user
# on the box -- the same reason the CLI refuses a --password flag at all.
#
# The CLI logs out at the end of every admin call by design, so this grants no
# standing session; it only removes the typing.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
env_file=${CASEBROKER_ADMIN_ENV:-$here/admin.env}

[ $# -gt 0 ] || { echo "usage: ${0##*/} <casebroker admin command...>" >&2; exit 2; }
[ -f "$env_file" ] || {
    echo "no $env_file -- copy admin.env.example to admin.env and fill it in" >&2; exit 2; }

set -a; . "$env_file"; set +a
: "${CASEBROKER_URL:?set CASEBROKER_URL in $env_file}"
: "${CASEBROKER_RELEASE_USER:?set CASEBROKER_RELEASE_USER in $env_file}"
: "${CASEBROKER_RELEASE_PASSWORD:?set CASEBROKER_RELEASE_PASSWORD in $env_file}"

# The project's own interpreter, so this works from a bare shell with no venv
# activated; `uv run` is the fallback for a checkout without one.
if   [ -x "$here/.venv/Scripts/python.exe" ]; then cli=("$here/.venv/Scripts/python.exe" -m casebroker.cli)
elif [ -x "$here/.venv/bin/python" ];        then cli=("$here/.venv/bin/python" -m casebroker.cli)
elif command -v uv >/dev/null 2>&1;          then cli=(uv run --project "$here" casebroker)
else echo "no .venv and no uv in $here" >&2; exit 2
fi

printf '%s\n' "$CASEBROKER_RELEASE_PASSWORD" | "${cli[@]}" "$@" \
    --broker "$CASEBROKER_URL" \
    --username "$CASEBROKER_RELEASE_USER" \
    --password-stdin
