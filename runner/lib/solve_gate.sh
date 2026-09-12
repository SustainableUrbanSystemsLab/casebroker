# The "is this direction done AND converged" gate.
#
# Sourced by run_case.sh's in-container inner runner (real OpenFOAM logs and
# field files) AND by tests/test_solve_gate.py (bare bash, synthetic fixtures,
# no container or solver needed) -- one function, one place it can go stale.
#
# Layered on purpose: no single signal below is trustworthy alone.
#   * mpirun's exit code misses a rank that wrote "End" and then crashed
#     during finalisation -- rare, but it happens upstream.
#   * "^End" alone is printed whenever foamRun reaches its LAST time step,
#     regardless of whether SIMPLE's own residualControl was ever satisfied --
#     a run that simply exhausts its iteration budget without converging
#     prints it too. That is exactly the silent, plausible-looking failure
#     this gate exists to catch: `grep -aq "^End" 12.log` alone (the check
#     this replaces) called that case "done".
#   * a clean exit with no field files on disk is a run that "succeeded" at
#     writing nothing.
#   * nan/inf can appear in a field even when every check above is happy, if
#     the divergence happened on the very last iteration.
#
# check_solve_converged <mpirun_exit_code> <log_file> <processor0_dir> <latest_time>
# Prints exactly one line (the verdict, or SOLVE_FAIL plus a reason) and
# returns 0 for OK, 1 for SOLVE_FAIL (retryable upstream), 2 for NOT_CONVERGED
# (treated as fatal upstream -- see run_case.sh: retrying an unconverged case
# anywhere else reproduces the identical outcome, since convergence is a
# property of the recipe -- iteration budget, geometry, mesh -- not the
# machine).
check_solve_converged() {
  local rc="$1" log="$2" proc0="$3" latest="$4"
  local fatal_rx='FOAM FATAL ERROR|[Ff]loating point exception|[Ss]egmentation fault|core dumped|std::bad_alloc|MPI_ABORT|double free or corruption'
  # Every OpenFOAM log opens with "sigFpe : Enabling floating point exception
  # trapping (FOAM_SIGFPE)" -- the announcement that FPEs WILL be fatal, not
  # one happening. It matches the pattern above, and until this exclusion the
  # gate failed every real solve on it (found on the first end-to-end run of
  # the fleet runner, 2026-09-11). A real FPE prints "Floating point exception"
  # on its own, with a stack trace after it, and still matches.
  local benign_rx='Enabling floating point exception trapping'

  if [ "$rc" -ne 0 ]; then
    echo "SOLVE_FAIL mpirun exited $rc"; return 1
  fi
  if [ ! -f "$log" ]; then
    echo "SOLVE_FAIL no log file ($log)"; return 1
  fi
  if grep -avE "$benign_rx" "$log" | grep -aqE "$fatal_rx"; then
    local ctx
    # -A3: the matched line plus a few after it, since the pattern itself
    # (e.g. "FOAM FATAL ERROR") rarely carries the actual explanation --
    # that is almost always the line right after it. Squashed onto one line
    # so it survives being embedded in a broker error message / JSON string.
    ctx=$(grep -avE "$benign_rx" "$log" | grep -aA3 -E "$fatal_rx" | head -4 | tr '\n' ' ' | tr -s ' ')
    echo "SOLVE_FAIL fatal error in $(basename "$log"): $ctx"; return 1
  fi
  grep -aq "^End$" "$log" || {
    echo "SOLVE_FAIL no End line in $(basename "$log") -- did not finish cleanly"; return 1
  }

  if [ -z "$latest" ] || [ ! -d "$proc0" ]; then
    echo "SOLVE_FAIL no result time directory was written"; return 1
  fi
  local f
  for f in U p phi k; do
    [ -s "$proc0/$latest/$f" ] || {
      echo "SOLVE_FAIL missing or empty $f at t=$latest"; return 1
    }
  done
  # RAS turbulence models write either epsilon (k-epsilon family) or omega
  # (k-omega family); wind studies here always run one of the two, never
  # laminar, so requiring one of them is a real check, not a loophole.
  if [ ! -s "$proc0/$latest/epsilon" ] && [ ! -s "$proc0/$latest/omega" ]; then
    echo "SOLVE_FAIL missing both epsilon and omega at t=$latest"; return 1
  fi

  # Which file and which line, not just "somewhere": the first time this
  # fired on a real solve (fourth smoke run, 2026-09-11) the log was clean,
  # the residuals healthy, and the scratch already deleted -- there was no way
  # to tell a diverged field from a word in a boundary entry that merely
  # matched the pattern.
  #
  # And ONLY for ascii fields. The campaign writes `format binary`, where the
  # raw doubles are arbitrary bytes and "iNf" turns up by chance (fifth smoke
  # run: a clean 30-iteration solve failed on byte-noise at U:1636). A binary
  # field is judged by the solver's own arithmetic instead: a diverged field
  # prints nan in the residual/continuity lines of the log.
  local hit f
  for f in "$proc0/$latest/U" "$proc0/$latest/p"; do
    if head -c 2000 "$f" | grep -aq 'format *ascii'; then
      hit=$(grep -aHniE '\bnan\b|\binf\b' "$f" 2>/dev/null | head -1 | cut -c1-200)
      if [ -n "$hit" ]; then
        echo "SOLVE_FAIL nan/inf in the written field: $hit"; return 1
      fi
    fi
  done
  hit=$(grep -aiE 'residual = *(nan|-?inf)|continuity errors.*(nan|inf)' "$log" | head -1 | cut -c1-200)
  if [ -n "$hit" ]; then
    echo "SOLVE_FAIL nan/inf in solver residuals: $hit"; return 1
  fi

  # The authoritative convergence signal: OpenFOAM's SIMPLE control loop
  # prints this ONLY when every field's residualControl threshold (set in
  # fvSolution) was satisfied -- never merely because iterations ran out.
  grep -aq "^SIMPLE solution converged in" "$log" || {
    echo "NOT_CONVERGED"; return 2
  }

  echo "OK"; return 0
}
