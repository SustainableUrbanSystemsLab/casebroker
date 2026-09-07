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

  if [ "$rc" -ne 0 ]; then
    echo "SOLVE_FAIL mpirun exited $rc"; return 1
  fi
  if [ ! -f "$log" ]; then
    echo "SOLVE_FAIL no log file ($log)"; return 1
  fi
  if grep -aqE "$fatal_rx" "$log"; then
    echo "SOLVE_FAIL fatal error string in $(basename "$log")"; return 1
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

  if grep -alqiE '\bnan\b|\binf\b' "$proc0/$latest/U" "$proc0/$latest/p" 2>/dev/null; then
    echo "SOLVE_FAIL nan/inf in the written U or p field"; return 1
  fi

  # The authoritative convergence signal: OpenFOAM's SIMPLE control loop
  # prints this ONLY when every field's residualControl threshold (set in
  # fvSolution) was satisfied -- never merely because iterations ran out.
  grep -aq "^SIMPLE solution converged in" "$log" || {
    echo "NOT_CONVERGED"; return 2
  }

  echo "OK"; return 0
}
