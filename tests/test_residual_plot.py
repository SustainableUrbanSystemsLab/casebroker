"""The residual chart on the case page.

A solve report's latest residuals cannot say whether a direction is converging;
the curve can. The chart is hand-drawn SVG in the one-file dashboard, so its
arithmetic (decades, ticks, gaps, the pointer's nearest point) and what it does
with a node's strings are checked by running the shipped functions in node, and
what has to be wired around it -- the fetch, the refresh, the thermal exception --
by reading the source.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "casebroker" / "static" / "dashboard.html"


def test_the_chart_arithmetic_and_markup_hold_for_the_odd_series_too():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")
    result = subprocess.run([node, str(ROOT / "tests" / "residual_plot_check.js")],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "residual chart cases hold" in result.stdout


def _body(src: str, name: str) -> str:
    start = src.index(f"  function {name}(")
    return src[start:src.index("\n  }\n", start)]


def test_the_solve_card_draws_the_curve_above_the_table_of_latest_numbers():
    src = DASH.read_text(encoding="utf-8")
    solve = _body(src, "solveSectionHtml")
    assert "residualBlockHtml(c, solve)" in solve
    assert solve.index("+ curve + residuals + finished") > 0


def test_the_open_case_is_asked_for_its_curves_on_every_render_and_never_for_a_thermal_one():
    src = DASH.read_text(encoding="utf-8")
    assert "ensureResiduals(openDetailId, cases.find((c) => c.case_id === openDetailId));" in src
    ensure = _body(src, "ensureResiduals")
    # A Radiance surface-temperature case has no solve, so no residuals to ask for.
    assert "isThermal(row)" in ensure
    # A running curve is re-read as often as the case record; an idle one far less.
    assert "RS_LIVE_MS" in ensure and "RS_IDLE_MS" in ensure
    assert 'row.state === "leased"' in ensure
    # A failing endpoint is not asked again at once, and a curve already on screen survives it.
    assert "residualFailed" in ensure and "have.at = Date.now()" in ensure
    assert "/residuals" in ensure and "encodeURIComponent(pick)" in ensure


def test_the_chart_is_redrawn_in_place_not_by_rebuilding_the_panel():
    """A pointer moving over it, a toggle and a direction change all come through
    here; rebuilding the whole case table for each would blink the panel."""
    src = DASH.read_text(encoding="utf-8")
    redraw = _body(src, "redrawResiduals")
    assert "residualInner(" in redraw and "redrawOpenCase" in redraw
    # The list of directions is not taken out from under a hand that is choosing from
    # it: while the picker has focus, only the parts around it are replaced.
    assert "picker === document.activeElement" in redraw
    for part in (".rs-at", ".rs-verdict", ".rs-plot", ".rs-keys", ".rs-eps", ".rs-basis"):
        assert part in redraw
    assert "residualOff" in src and "residualPick" in src
