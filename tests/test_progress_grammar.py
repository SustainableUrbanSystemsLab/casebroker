"""The dashboard's progress bar reads a grammar another repository writes.

`MetaFOAM.Lib/Node/NodeProgress.cs` in Eddy3D puts `mesh 3/5 · 03_snappyHexMesh`
and `solve 3/8 dirs · iter 412/2000` on the heartbeat; `progressFraction` in
dashboard.html turns them into a bar. Two implementations of one format drift
unless something compares them, and the failure is silent in both directions: a
bar that never appears, or one that appears and is wrong.

So the check runs the SHIPPED JavaScript -- pulled out of dashboard.html, not a
copy -- against the exact strings the C# side's own tests pin.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_dashboard_reads_what_a_node_writes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")

    result = subprocess.run([node, str(ROOT / "tests" / "progress_grammar_check.js")],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "agree with the C# writer" in result.stdout


def test_the_bar_is_only_drawn_for_a_case_that_is_running():
    """A finished case's last line is whatever it said before archiving, and a bar
    frozen at 87% under a case marked done reads as a stuck case rather than a
    finished one."""
    html = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    assert 'c.state === "leased" ? progressCell(c.last_progress)' in html


def test_the_column_count_still_matches_the_detail_row():
    """A Progress column that does not move the detail row's colspan leaves the
    expanded panel one cell short, which silently breaks the table layout."""
    html = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    body = html[html.index('<table id="casesTable">'):]
    head = body[:body.index("</thead>")]
    assert head.count("<th>") == 9, f"the case table has {head.count('<th>')} columns"
    assert 'colspan="9"' in html
    assert 'colspan="8"' not in html
