"""The Site Geometry panel's three views: one size, and each with its own zoom
buttons that survive the minute's refresh.

Checked by hand in a browser against a real case's cached geometry (2026-09-22):
three 300 x 300 views, + three times is 1.6^3 = 4.1x about the middle, − and
reset greyed at the whole drawing, and a forced refresh -- which rebuilds the
panel -- puts the zoomed view back where it was.
"""

from __future__ import annotations

import pathlib
import re

DASH = (pathlib.Path(__file__).resolve().parents[1]
        / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")


def _draw_geometry() -> str:
    start = DASH.index("function drawGeometry(")
    return DASH[start:DASH.index("\n  }\n", start)]


def test_the_three_views_are_one_size():
    body = _draw_geometry()
    assert "const EW = S, EH = S" in body, "the elevation is as tall as the other two are"
    sizes = re.findall(r'viewBox="0 0 \$\{(\w+)\} \$\{(\w+)\}"', body)
    assert sizes == [("S", "S"), ("ISO", "ISO"), ("EW", "EH")]
    assert "const S = 300" in body and "ISO = 300" in body
    # The loading placeholders stand where the views will be, at the views' size.
    assert "skelFigure(300, 168)" not in DASH
    assert DASH.count("skelFigure(300, 300)") == 6


def test_each_view_carries_its_own_buttons_outside_the_drawing():
    """Outside the <svg>: a press inside it starts a drag-pan."""
    body = _draw_geometry()
    assert body.count('<div class="geo-view">') == 3
    for view in ("plan view", "isometric view", "elevation"):
        call = f'${{geoControls("{view}")}}'
        assert call in body, view
        assert body.rindex("</svg>", 0, body.index(call)) > body.rindex("<svg", 0, body.index(call))
    for act in ("in", "out", "reset"):
        assert f'button("{act}"' in DASH


def test_the_caption_sits_directly_under_the_views_and_names_them():
    """Under the three views, not in the legend beside them, and by name: in a row
    of three, "middle" and "bottom" no longer pointed at anything."""
    body = _draw_geometry()
    views_end = body.rindex('${geoControls("elevation")}')
    caption = body.index('<div class="geo-caption">${caption}</div>')
    legend = body.index('<div class="geo-legend">')
    assert views_end < caption < legend
    for name in ("Plan:", "Isometric:", "Elevation:"):
        assert name in body
    assert "Middle: isometric" not in DASH and "Bottom: elevation" not in DASH


def test_the_legend_says_each_thing_once():
    """The building source was named twice, the RMSE sat apart from the uncertainty
    it qualifies, a crown-cell count described the preview grid rather than the
    mesh, and a usage hint repeated what the buttons now show."""
    body = _draw_geometry()
    assert body.count("GlobalBuildingAtlas LoD1") == 1, "one sources line"
    assert "GBA LoD1</b>" not in body
    assert "exceed the published RMSE (1.5–8.9 m)" in body
    for gone in ("porous crown cell", "scroll to zoom", "Height variance median"):
        assert gone not in DASH, gone


def test_a_zoom_survives_the_rebuild_the_refresh_does():
    """Auto-refresh is on by default and rebuilds the open case's panel every
    minute; without the restore every zoom reverted within a minute."""
    body = _draw_geometry()
    assert body.index("wrap.innerHTML =") < body.index('wrap.querySelectorAll("svg.geo-zoom").forEach(geoRestore)')
    assert body.count('data-view="${esc(viewKey)}:') == 3
