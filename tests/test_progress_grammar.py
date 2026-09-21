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
import re
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


def test_a_worker_row_says_what_it_is_holding():
    """The Workers table could say how many cases a worker had finished and never
    what it was doing -- which is the question asked of it while a campaign runs.

    The headers are GENERATED now (they carry the sort state), so the count comes
    from WORKER_COLUMNS rather than from static markup; the cells still have to
    match it or the table shears.
    """
    html = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    block = html[html.index("const WORKER_COLUMNS = ["):]
    block = block[:block.index("];")]
    assert len(re.findall(r'\["[^"]+",\s*"[^"]+"\]', block)) == 11

    row = html[html.index('$("workersBody").innerHTML'):]
    row = row[:row.index("}).join")]
    assert row.count("<td") == 11, f"the workers row draws {row.count('<td')} cells"
    assert "w.current_case" in html and "w.current_progress" in html


def test_a_failed_case_says_why_without_being_opened():
    """`last_error` was in the list payload for every row and reachable only by
    expanding one case at a time -- so "7 failed" was as much as the table said."""
    html = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    assert "c.last_error" in html
    # Escaped in both the title and the text: a worker credential writes this string.
    cell = html[html.index("c.state === \"leased\" ? progressCell(c.last_progress)"):]
    cell = cell[:cell.index("</td>")]
    assert cell.count("esc(") >= 2, "the failure text reaches the DOM twice and must be escaped twice"


def test_the_progress_text_is_a_block_so_it_cannot_run_into_the_case_id():
    """In the fleet table the case id and the progress sit in one cell. An inline
    span put them flush against each other -- "v2-05b5c7e73e5ddcb2step 3/5: …" is
    what that looked like on screen."""
    html = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    cell = html[html.index("function progressCell(line)"):]
    cell = cell[:cell.index("\n  }")]
    # Keyed on the hazard, not on the tag: the caption's label and percentage are
    # spans INSIDE a flex row of their own and cannot touch the case id. What may
    # never be inline is the top-level element each branch returns, which is what
    # sits directly beside the id.
    returned = [line.strip() for line in cell.splitlines()
                if "return `<" in line or "+ `<div" in line]
    assert returned, "progressCell() returns nothing that looks like markup"
    for line in returned:
        if "return `<" in line:
            assert "return `<div" in line, f"progressCell returns an inline element: {line}"
    assert 'flex-direction:column' in html[html.index("w.current_case"):][:400]


def test_the_fleet_strip_shows_the_whole_fleet():
    """The panel is titled "Worker Fleet" and its strip was the reported SLURM
    queues alone -- so two clusters showed and every workstation actually solving
    cases did not. Runs the shipped fleetCards, like the progress grammar."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")

    result = subprocess.run([node, str(ROOT / "tests" / "fleet_cards_check.js")],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all cases agree" in result.stdout

