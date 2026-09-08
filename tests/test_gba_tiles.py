"""The GlobalBuildingAtlas tile key, which fails silently when it is wrong.

A wrong key is either a 404 (loud, fine) or -- worse -- a real tile for the
wrong part of the world, which returns buildings and produces a case that meshes
and solves and is somewhere else entirely. The naming hides two traps: the
hemisphere letters carry ABSOLUTE values, and the pairs are ordered
(west, north) then (east, south), so latitude descends while longitude ascends.
"""

from __future__ import annotations

import math

from casebroker.footprints import gba_tile_for


def test_known_published_keys():
    """Keys taken verbatim from the published listing of 922 tiles."""
    # Nanjing, the case this was developed against.
    assert gba_tile_for(32.05546, 118.76829) == "e115_n35_e120_n30"
    # West of Greenwich: letters change, values stay positive.
    assert gba_tile_for(22.5, -112.5) == "w115_n25_w110_n20"
    # Southern hemisphere: 's' on both latitude fields, north still first.
    assert gba_tile_for(-12.5, 47.5) == "e045_s10_e050_s15"
    # Straddling the equator from above: zero takes the POSITIVE letter.
    assert gba_tile_for(2.5, 107.5) == "e105_n05_e110_n00"


def test_zero_and_sign_boundaries():
    """The equator and the prime meridian, from both sides."""
    assert gba_tile_for(1.0, 1.0).startswith("e000_n05_e005_n00")
    # Just south of the equator: south is -5 -> s05, north is 0 -> n00.
    assert gba_tile_for(-1.0, 1.0) == "e000_n00_e005_s05"
    # Just west of Greenwich: west is -5 -> w005, east is 0 -> e000.
    assert gba_tile_for(1.0, -1.0) == "w005_n05_e000_n00"


def test_a_point_always_lands_inside_its_own_tile():
    """Whatever the key says, it must contain the point that produced it."""
    import re

    pat = re.compile(r"^([ew])(\d{3})_([ns])(\d{2})_([ew])(\d{3})_([ns])(\d{2})$")
    for lat in (-67.3, -33.9, -0.4, 0.0, 12.5, 51.5, 71.9):
        for lon in (-179.9, -122.4, -0.1, 0.0, 4.9, 118.8, 179.9):
            key = gba_tile_for(lat, lon)
            m = pat.match(key)
            assert m, f"{lat},{lon} -> unparseable {key}"
            wl, wv, nl, nv, el, ev, sl, sv = m.groups()
            west = int(wv) * (1 if wl == "e" else -1)
            north = int(nv) * (1 if nl == "n" else -1)
            east = int(ev) * (1 if el == "e" else -1)
            south = int(sv) * (1 if sl == "n" else -1)
            assert east == west + 5 and north == south + 5, key
            assert west <= lon < east, f"{lon} not in [{west},{east}) for {key}"
            assert south <= lat < north, f"{lat} not in [{south},{north}) for {key}"


def test_tile_is_five_degrees_and_aligned():
    """Bounds are always multiples of five -- the grid has no offset."""
    for lat, lon in ((32.05546, 118.76829), (-33.87, 151.21), (30.27, -97.74)):
        assert math.floor(lon / 5) * 5 <= lon
        key = gba_tile_for(lat, lon)
        assert len(key.split("_")) == 4
