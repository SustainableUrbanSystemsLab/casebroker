"""The wind-field view outlines the site's trees.

The pedestrian field shows where the wind is slow; on a canopy recipe, trees are one of the
reasons. The outline comes from the same /footprints canopy grid the Site Geometry panel
draws, so the two never disagree about where the trees are, and the view says whether this
case's solve contained them at all -- a v5 case was solved treeless, and a tree drawn over its
field must not read as the cause of anything.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "casebroker" / "static" / "dashboard.html"


def test_the_outline_is_closed_north_up_and_at_the_canopy_cut():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")
    result = subprocess.run([node, str(ROOT / "tests" / "canopy_outline_check.js")],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "canopy outlines are closed" in result.stdout


def test_the_view_shares_the_site_panels_load_and_says_whether_the_solve_had_trees():
    src = DASH.read_text(encoding="utf-8")
    canopy = src[src.index("  function wfCanopy(wrap)"):]
    canopy = canopy[:canopy.index("\n  }\n")]
    # One /footprints request per case for both panels, keyed the way the site panel keys it.
    assert "footprintsFor(wrap.dataset.case, wrap.dataset.state)" in canopy
    assert 'data-state="${esc(c.state)}"' in src
    # Whether the crowns were in the solve is the node's own record, not a guess from the recipe.
    assert "metrics.trees_modelled === true" in src and "metrics.trees_modelled === false" in src
    assert "not</b> in this solve" in src
    # The outline is drawn before the arrows, which are what the field is read by.
    paint = src[src.index("  function wfPaint(wrap, s)"):]
    assert paint.index("wfCanopyOutline(can)") < paint.index("if (s.arrows && v && d.vec)")
