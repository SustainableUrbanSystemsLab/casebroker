#!/bin/bash
# Everything CI will check, run locally, BEFORE pushing to main.
#
# This exists because of a specific failure. Making Overture an optional extra
# and then fixing the site preview both touched what the container installs, and
# neither could be verified by the test suite: rasterio imports fine on a dev
# machine, which has libexpat, and fails at import inside python:3.12-slim,
# which does not. The wheel installs cleanly either way. So the suite stayed
# green while production reported terrain and trees as "unavailable" for every
# case -- and the way that got diagnosed was by pushing probe commits to main
# and reading the CI logs, which left two red marks on a branch that gates
# deploys. Using main's CI as a debugger is the thing this script replaces.
#
#   scripts/preflight.sh            # tests + container checks
#   scripts/preflight.sh --fast     # tests only, skip the image build
#
# Exit 0 means the same checks CI runs have passed here.

set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
bad()  { printf '\033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }
good() { printf '\033[32mok\033[0m    %s\n' "$1"; }

step "test suite"
if uv run --extra dev pytest tests/ -q 2>&1 | tail -3; then
  good "tests"
else
  bad "tests"
fi

if [ "${1:-}" = "--fast" ]; then
  [ $fail -eq 0 ] && echo && good "preflight (--fast: container checks skipped)"
  exit $fail
fi

# podman and docker are interchangeable here; podman is what this project's
# macOS machines actually have.
ENGINE=""
for e in docker podman; do
  if command -v "$e" >/dev/null 2>&1 && "$e" info >/dev/null 2>&1; then ENGINE="$e"; break; fi
done
if [ -z "$ENGINE" ]; then
  printf '\n\033[33mSKIP\033[0m  no container engine reachable -- the image checks below are\n'
  printf '      exactly the ones the test suite CANNOT do. Install podman, or push to a\n'
  printf '      BRANCH and let CI run them before main.\n'
  exit $fail
fi

step "build the image Render deploys ($ENGINE)"
if $ENGINE build -q -t casebroker:preflight . >/dev/null 2>&1; then
  good "image builds"
else
  bad "image build"
  exit 1
fi

step "the geo stack must work INSIDE that image"
# Not "does it import" -- whether GDAL's bundled curl can read a remote COG from
# this container. That is the check that would have caught libexpat.so.1.
if $ENGINE run --rm casebroker:preflight python -c "
import rasterio, numpy
print('  rasterio', rasterio.__version__, '| GDAL', rasterio.__gdal_version__, '| numpy', numpy.__version__)
from casebroker import footprints as F
t = F.terrain(33.749, -84.388)
c = F.canopy(33.749, -84.388)
print('  terrain:', t.get('source'), t.get('relief_m'), t.get('detail', ''))
print('  canopy :', c.get('source'), c.get('frac_canopy'), c.get('detail', ''))
assert t['source'] == 'gedtm30', t
assert c['source'] == 'meta-wri-chm-v1', c
assert c['frac_canopy'] > 0.05, c
import casebroker.app, casebroker.worker, casebroker.cli
assert F.on_land(33.749, -84.388) and not F.on_land(7.5, -37.5)
"; then
  good "rasterio reads remote COGs; land mask and every module present"
else
  bad "geo stack inside the image"
fi

step "it boots and answers"
cid=$($ENGINE run -d -p 8099:8000 -e CASEBROKER_DB=/tmp/pf.sqlite casebroker:preflight 2>/dev/null)
ok=1
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8099/healthz >/dev/null 2>&1; then ok=0; break; fi
  sleep 1
done
if [ $ok -eq 0 ]; then good "healthz answers"; else bad "never became healthy"; $ENGINE logs "$cid" 2>&1 | tail -15; fi
$ENGINE rm -f "$cid" >/dev/null 2>&1

echo
if [ $fail -eq 0 ]; then good "preflight passed -- safe to push"; else bad "preflight FAILED -- do not push to main"; fi
exit $fail
