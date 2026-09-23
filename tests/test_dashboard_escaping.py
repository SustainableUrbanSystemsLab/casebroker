"""Anything a worker can write reaches an admin's browser through innerHTML.

The dashboard builds its DOM from template literals. Most interpolations go
through `esc()`, and the ones that did not were fields a WORKER supplies --
`cluster` via POST /v1/fleet, `result_sha256` via POST /v1/complete. A worker
credential is the weakest link in this system by construction: it lives
unattended on cluster nodes and Windows workstations. Script running in the
admin's dashboard acts with the admin's session, so an unescaped worker string
is a path from write scope to admin.

This is a static check rather than a browser test, because the property worth
pinning is "no interpolation of server data skips esc()" -- which a rendering
test would only catch for the exact payload it happened to try.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from casebroker.app import create_app

DASH = (pathlib.Path(__file__).resolve().parents[1]
        / "casebroker" / "static" / "dashboard.html")

# Fields that originate outside the browser: a worker, an operator, or the
# database. Anything here must be escaped before it becomes markup.
SERVER_FIELDS = (
    "case_id", "city_cluster", "lcz", "recipe", "last_error", "result_uri",
    "result_sha256", "worker_id", "host", "cluster", "detail", "username",
    "role", "split", "machine", "last_progress", "hostname",
)
# Helpers that escape, or that cannot produce markup (numbers, dates).
SAFE_CALLS = ("esc(", "formatBytes(", "relTime(", "badge(", "progressCell(",
              "toLocaleString(", "toFixed(", "Number(", "Math.")


def _interpolations():
    for i, line in enumerate(DASH.read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r"\$\{([^{}]*)\}", line):
            yield i, m.group(1).strip()


def test_no_server_supplied_field_reaches_the_dom_unescaped():
    bad = []
    for lineno, expr in _interpolations():
        if any(c in expr for c in SAFE_CALLS):
            continue
        if any(re.search(rf"\b{f}\b", expr) for f in SERVER_FIELDS):
            bad.append(f"dashboard.html:{lineno}: ${{{expr[:80]}}}")
    assert not bad, "unescaped server data in markup:\n  " + "\n  ".join(bad)



# The renderers of what a NODE reports (POST /v1/telemetry, stored verbatim
# under a worker credential): mesh names, engine, build, meshed_by, the
# failed-check names, the direction a solve is on, residual fields, finished
# directions and their status, the worker, the DEM. Those names are too
# generic ("status", "current", "build") to sweep the whole file for, so the
# sweep above sees none of them. Inside these functions, then, EVERY
# interpolation goes through a safe call, unless it is one of the locals below
# -- markup this file builds from checked parts, or a choice between literals.
TELEMETRY_RENDERERS = ("reportedHtml", "meshSectionHtml", "solveSectionHtml", "pctCardInner")
# solveStatusClass answers one of five class names this file chose.
TELEMETRY_SAFE = SAFE_CALLS + ("formatCells(", "formatMetric(", "formatDuration(",
                               "verdictBadge(", "reportedHtml(", "histSvg(", "solveStatusClass(")
BUILT_LOCALS = {"by", "rows", "v", "solveSecs", "fig", "tiles", "useOwn", "!useOwn",
                'notes.join(" · ")', 'foot.join(" · ")', 'it ? " · " + it + end : ""'}
_LITERAL = r"""(?:"[^"]*"|'[^']*')"""


def _function_body(src, name):
    m = re.search(rf"\n  function {name}\([^)]*\)\s*\{{(.+?)\n  \}}\n", src, re.S)
    assert m, f"{name}() not found"
    return m.group(1)


def test_telemetry_renderers_escape_every_node_string():
    src = DASH.read_text(encoding="utf-8")
    bad = []
    for name in TELEMETRY_RENDERERS:
        for line in _function_body(src, name).splitlines():
            # A histogram's tip is text: histSvg escapes it where it becomes
            # markup (pinned below).
            if re.search(r"\btip\s*[:=]", line):
                continue
            for expr in (e.strip() for e in re.findall(r"\$\{([^{}]*)\}", line)):
                if any(c in expr for c in TELEMETRY_SAFE) or expr in BUILT_LOCALS:
                    continue
                if re.fullmatch(rf"[^?]+\?\s*{_LITERAL}\s*:\s*{_LITERAL}", expr):
                    continue
                bad.append(f"{name}: ${{{expr[:80]}}}")
            # Concatenated rather than interpolated: `" by " + k.worker`.
            for m in re.finditer(r"\+\s*[A-Za-z_]\w*(?:\.\w+|\[[^\]]+\])+(?!\s*\()"
                                 r"|(?<![\w.(])[A-Za-z_]\w*(?:\.\w+)+\s*\+(?!\+)", line):
                bad.append(f"{name}: {m.group(0).strip()} (concatenated unescaped)")
    assert not bad, "a node's string reaches the DOM unescaped:\n  " + "\n  ".join(bad)


def test_hist_svg_escapes_its_tip():
    body = _function_body(DASH.read_text(encoding="utf-8"), "histSvg")
    assert "esc(o.tip)" in body, "histSvg() must escape the tip it puts in <title>"

def test_badge_escapes_both_positions():
    """`state` lands in a class attribute as well as in text."""
    src = DASH.read_text(encoding="utf-8")
    m = re.search(r"function badge\(state\)\s*\{(.+?)\n  \}", src, re.S)
    assert m, "badge() not found"
    body = m.group(1)
    assert body.count("esc(state)") >= 2, (
        "badge() must escape `state` in the attribute AND the text")


def test_progress_cell_escapes_the_line_it_draws():
    """`progressCell` is on the allowlist above, so its own escaping is what the
    allowlist is asserting. The line it draws is `last_progress` -- a string a
    worker credential writes -- and it reaches the DOM twice: as the bar's title
    and as the text under it."""
    src = DASH.read_text(encoding="utf-8")
    m = re.search(r"function progressCell\(line\)\s*\{(.+?)\n  \}", src, re.S)
    assert m, "progressCell() not found"
    body = m.group(1)
    # Every interpolation of the line itself must go through esc(); the only other
    # things interpolated are a rounded number and a phase word this file chose.
    # `label` counts as the line too: it is BUILT from the heartbeat (it carries
    # the step's program name verbatim), so an unescaped label is the same hole
    # one indirection further along.
    for expr in re.findall(r"\$\{([^{}]*)\}", body):
        if "line" in expr or "label" in expr:
            assert "esc(" in expr, f"progressCell interpolates the line unescaped: ${{{expr}}}"
    assert body.count("esc(line)") >= 2, (
        "progressCell() must escape the line in the title AND the text")


def test_esc_covers_every_character_that_matters():
    src = DASH.read_text(encoding="utf-8")
    m = re.search(r"function esc\(s\)\s*\{(.+?)\n  \}", src, re.S)
    assert m, "esc() not found"
    body = m.group(1)
    for ch in ("&", "<", ">", '"', "'"):
        assert ch in body, f"esc() does not handle {ch!r}"


# -- and the server side of it -----------------------------------------------

@pytest.fixture()
def broker(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "x.sqlite"), tokens=["s"]))
    c.headers.update({"Authorization": "Bearer s"})
    return c


def test_the_broker_stores_worker_strings_verbatim(broker):
    """Deliberate, and the reason escaping is the dashboard's job.

    The broker does not sanitise on the way in: a cluster name is data, the
    database is not an HTML document, and stripping characters here would
    corrupt a legitimate name while still not making the dashboard safe. The
    invariant is that it is escaped on the way OUT, which the tests above pin.
    """
    payload = "<img src=x onerror=alert(1)>"
    broker.post("/v1/fleet", json={"cluster": payload, "queued": 1, "running": 0})
    got = broker.get("/v1/status").json()
    clusters = [f["cluster"] for f in got.get("fleet", [])]
    assert payload in clusters, "stored verbatim, to be escaped at render time"
