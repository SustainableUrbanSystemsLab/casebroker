"""The "is this direction done AND converged" gate, executed for real.

`runner/lib/solve_gate.sh` is sourced here exactly as `run_case.sh`'s in-container
inner runner sources it -- these tests drive the SAME function against synthetic
logs and field-file trees, not a re-implementation of its logic. A string
assertion on run_case.sh's generated bash could only ever prove the text says
what was meant; it cannot prove the gate actually classifies a real log/field-tree
shape the way the comment above the function claims. See
Eddy3D-Dev/Eddy3D's engine-commands.md for why that distinction matters --
generated shell is only trustworthy once it has been EXECUTED against the
failure shapes it claims to catch.

`grep -aq "^End" 12.log` alone (what this gate replaces) called a case "done"
whenever foamRun reached its last time step, whether or not SIMPLE's own
residualControl was ever satisfied. test_ran_to_completion_without_converging_is_not_ok
is the test that would have caught that: it is red against the OLD one-line
check and green against this gate.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

GATE = pathlib.Path(__file__).resolve().parents[1] / "runner" / "lib" / "solve_gate.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="no bash on PATH to source solve_gate.sh")


def run_gate(rc, log, proc0, latest):
    """Invoke the REAL function via a real bash, exactly as inner.sh does."""
    script = f'source "{GATE.as_posix()}"; check_solve_converged "{rc}" "{log}" "{proc0}" "{latest}"'
    p = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def make_fields(tmp_path, latest="250", turbulence="epsilon", extra_text=""):
    """A processor0/<latest>/ directory with the field files a real solve
    writes, non-empty, so "did it actually write output" has something
    real to check against."""
    proc0 = tmp_path / "processor0"
    t = proc0 / latest
    t.mkdir(parents=True)
    for f in ("U", "p", "phi", "k", turbulence):
        (t / f).write_text(f"FoamFile {{ class volVectorField; }}\n(0 0 0)\n{extra_text}\n")
    return proc0


def converged_log(tmp_path, name="12.log"):
    log = tmp_path / name
    log.write_text(
        "Time = 250s\n\n"
        "DILUPBiCGStab:  Solving for Ux, Initial residual = 5e-06, Final residual = 1.6e-07\n"
        "ExecutionTime = 10.2 s  ClockTime = 11 s\n\n"
        "SIMPLE solution converged in 250 iterations\n\n"
        "End\n\n"
        "Finalising parallel run\n"
    )
    return log


def test_a_real_converged_case_is_ok(tmp_path):
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path)
    rc, out = run_gate(0, log, proc0, "250")
    assert (rc, out) == (0, "OK")


def test_ran_to_completion_without_converging_is_not_ok(tmp_path):
    """The exact failure mode `grep -aq "^End"` alone could never catch: the
    solver reached its last time step and exited cleanly, but SIMPLE's own
    residualControl criteria were never satisfied -- OpenFOAM only prints
    "SIMPLE solution converged in N iterations" when they are."""
    log = tmp_path / "12.log"
    log.write_text(
        "Time = 1868s\n\n"
        "DILUPBiCGStab:  Solving for Ux, Initial residual = 0.043, Final residual = 0.038\n"
        "ExecutionTime = 900.1 s  ClockTime = 905 s\n\n"
        "End\n\n"
        "Finalising parallel run\n"
    )
    proc0 = make_fields(tmp_path, latest="1868")
    rc, out = run_gate(0, log, proc0, "1868")
    assert (rc, out) == (2, "NOT_CONVERGED")


def test_a_nonzero_mpirun_exit_is_solve_fail_even_with_a_perfect_log(tmp_path):
    """A rank that segfaults AFTER writing "End" during finalisation is a real
    upstream failure mode -- the exit code must be checked independently of
    the log text, not inferred from it."""
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path)
    rc, out = run_gate(139, log, proc0, "250")
    assert rc == 1
    assert "mpirun exited 139" in out


def test_a_fatal_error_string_fails_even_with_exit_code_zero(tmp_path):
    log = tmp_path / "12.log"
    log.write_text(
        "Time = 40s\n\n"
        "#0  Foam::error::printStack(Foam::Ostream&)\n"
        "#1  Foam::error::abort()\n"
        "\n\n"
        "--> FOAM FATAL ERROR: \n"
        "Maximum number of iterations exceeded\n"
    )
    proc0 = make_fields(tmp_path, latest="40")
    rc, out = run_gate(0, log, proc0, "40")
    assert rc == 1
    assert "fatal error in" in out
    # The context lines, not just the bare "FOAM FATAL ERROR" marker -- that
    # marker alone rarely carries the actual explanation, which is almost
    # always the line right after it.
    assert "Maximum number of iterations exceeded" in out


def test_a_floating_point_exception_is_caught_case_insensitively(tmp_path):
    log = tmp_path / "12.log"
    log.write_text("Time = 12s\n\nFloating point exception (core dumped)\n")
    proc0 = make_fields(tmp_path, latest="12")
    rc, out = run_gate(136, log, proc0, "12")
    assert rc == 1


def test_no_result_time_directory_is_solve_fail(tmp_path):
    """A clean exit that wrote nothing must never read as success."""
    log = converged_log(tmp_path)
    proc0 = tmp_path / "processor0"
    proc0.mkdir()
    rc, out = run_gate(0, log, proc0, "")
    assert rc == 1
    assert "no result time directory" in out


def test_missing_field_file_is_solve_fail(tmp_path):
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path)
    (proc0 / "250" / "phi").unlink()
    rc, out = run_gate(0, log, proc0, "250")
    assert rc == 1
    assert "missing or empty phi" in out


def test_an_empty_field_file_is_solve_fail_not_just_a_missing_one(tmp_path):
    """A truncated write (disk full, killed mid-flush) leaves the file
    PRESENT but empty -- `-f` would miss this; the gate uses `-s`."""
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path)
    (proc0 / "250" / "U").write_text("")
    rc, out = run_gate(0, log, proc0, "250")
    assert rc == 1
    assert "missing or empty U" in out


def test_komega_turbulence_fields_are_accepted_not_just_kepsilon(tmp_path):
    """The turbulence-field check must not hardcode k-epsilon: a kOmegaSST
    case writes omega, never epsilon, and that is equally valid."""
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path, turbulence="omega")
    rc, out = run_gate(0, log, proc0, "250")
    assert (rc, out) == (0, "OK")


def test_missing_both_turbulence_fields_is_solve_fail(tmp_path):
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path, turbulence="omega")
    (proc0 / "250" / "omega").unlink()
    rc, out = run_gate(0, log, proc0, "250")
    assert rc == 1
    assert "epsilon and omega" in out


def test_nan_in_the_velocity_field_fails_even_with_a_converged_log(tmp_path):
    """The nastiest case: everything upstream looks perfect (clean exit,
    converged message, End line, every file present) but the field itself
    diverged to nan on the very last iteration."""
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path)
    (proc0 / "250" / "U").write_text("FoamFile {}\n(nan nan nan)\n")
    rc, out = run_gate(0, log, proc0, "250")
    assert rc == 1
    assert "nan/inf" in out


def test_inf_in_the_pressure_field_fails(tmp_path):
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path)
    (proc0 / "250" / "p").write_text("FoamFile {}\n-inf\n")
    rc, out = run_gate(0, log, proc0, "250")
    assert rc == 1
    assert "nan/inf" in out


def test_a_word_that_merely_contains_nan_as_a_substring_does_not_false_positive(tmp_path):
    """Word-boundary matching matters: a field file legitimately containing
    something like a boundary/patch name must not trip the nan/inf scan."""
    log = converged_log(tmp_path)
    proc0 = make_fields(tmp_path, extra_text="nonuniform List<scalar> canopy_inflow")
    rc, out = run_gate(0, log, proc0, "250")
    assert (rc, out) == (0, "OK")
