"""The case card's "domain" link: the domain, the core, and the site's buildings
and tree canopy, drawn on geojson.io.

The whole drawing travels in the URL, and geojson.io reads it through two
decodes -- a query-string pass over the fragment, then the data: URL inside it,
where a "#" ends the data. A link that parses here and draws nothing there is the
failure this guards: it happened once already ("Unterminated string in JSON at
position 127", every simplestyle colour cut at its "#").

So the check runs the SHIPPED JavaScript -- sliced out of dashboard.html, not a
copy -- on a small synthetic site and reads the link back the way the page does.
Verified by hand against geojson.io itself (Chromium, and WebKit headless) with
the real payloads of three campaign sites, 83-144 KB each, 2026-09-22.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "casebroker" / "static" / "dashboard.html"


def test_the_link_reads_back_as_geojson_io_reads_it():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")

    result = subprocess.run([node, str(ROOT / "tests" / "map_link_check.js")],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "read back as geojson.io reads them" in result.stdout


def test_the_open_case_link_is_upgraded_when_its_geometry_lands():
    """The link is rendered with the case card, usually before the site geometry
    has been read. Without the upgrade it would show the buildings only after
    the next minute's refresh -- or never, for someone who clicks straight away."""
    src = DASH.read_text(encoding="utf-8")
    load = src[src.index("async function loadFootprints("):]
    load = load[:load.index("\n  }\n")]
    assert load.index("drawGeometry(g.data, wrap)") < load.index("refreshMapLinks(caseId)")
    assert 'class="domain-map" data-case="${esc(caseId || "")}"' in src
    assert "placeHtml(c.place, spec, cylDomain, c.case_id)" in src, "the link is keyed on its case"
