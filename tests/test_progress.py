"""The progress line the heartbeat carries.

Pinned against a synthetic solver log because the one misread it exists to
prevent is easy to reintroduce: p is solved several times per outer iteration,
and counting every "Solving for p" line instead of the first one per step
turns a clean descent into an apparent oscillation.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runner" / "lib"))

import progress  # noqa: E402

LOG = """\
Starting time loop

Time = 1s

DILUPBiCGStab:  Solving for Ux, Initial residual = 1, Final residual = 0.01, No Iterations 1
DILUPBiCGStab:  Solving for Uy, Initial residual = 1, Final residual = 0.01, No Iterations 1
DICPCG:  Solving for p, Initial residual = 1, Final residual = 0.009, No Iterations 40
DICPCG:  Solving for p, Initial residual = 0.2, Final residual = 0.002, No Iterations 60
DICPCG:  Solving for p, Initial residual = 0.05, Final residual = 0.0005, No Iterations 80
ExecutionTime = 10 s  ClockTime = 10 s

Time = 2s

DILUPBiCGStab:  Solving for Ux, Initial residual = 0.4, Final residual = 0.004, No Iterations 1
DICPCG:  Solving for p, Initial residual = 0.3, Final residual = 0.003, No Iterations 40
DICPCG:  Solving for p, Initial residual = 0.02, Final residual = 0.0002, No Iterations 60
DICPCG:  Solving for p, Initial residual = 0.001, Final residual = 1e-05, No Iterations 80
ExecutionTime = 20 s  ClockTime = 20 s
"""


def test_first_solve_per_outer_iteration_is_the_one_reported():
    info = progress.parse_log(LOG)
    assert info["iteration"] == 2
    # 0.3 is the FIRST p solve of step 2 -- not 0.001, the last corrector stage,
    # which is what a naive "last match" parser returns.
    assert info["p"] == 0.3
    assert info["Ux"] == 0.4
    assert info["converged"] is False


def test_converged_only_when_the_solver_says_so():
    assert progress.parse_log(LOG + "\nSIMPLE solution converged in 2 iterations\n")["converged"] is True


def test_empty_or_pre_solve_log_says_nothing(tmp_path):
    assert progress.parse_log("") == {}
    assert progress.parse_log("Create time\nCreate mesh\n") == {}
    p = tmp_path / "12.log"
    p.write_text("Create time\n")
    assert progress.summarize(str(p)) == ""
    assert progress.summarize(str(tmp_path / "missing.log")) == ""


def test_summary_line_is_compact(tmp_path):
    p = tmp_path / "12.log"
    p.write_text(LOG)
    line = progress.summarize(str(p), "case_270", 8280.0)
    assert line == "case_270 iter 2 p=3.0e-01 Ux=4.0e-01 (2.3 h)"


def test_cli_prints_the_line_and_never_fails_on_a_missing_log(tmp_path, capsys):
    assert progress.main([str(tmp_path / "nope.log")]) == 0
    assert capsys.readouterr().out.strip() == ""
    assert progress.main([str(tmp_path / "nope.log"), "--json"]) == 0
    assert capsys.readouterr().out.strip() == "{}"
