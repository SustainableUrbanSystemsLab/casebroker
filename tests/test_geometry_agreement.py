"""The preview's whole claim is that it draws what the mesh will contain.

That claim rests on arithmetic shared with code in another repository
(`benchmark/real_cities/` of JP-Wind-ML-Comparision): the same half-widths, the
same degrees-per-metre constants, the same bbox predicate. Nothing enforces the
agreement across a repo boundary, and nothing would fail if it broke -- the
panel would keep drawing a plausible city that is simply not the one being
solved, which is the failure the panel exists to prevent.

So the numbers are pinned here. A change that makes one of these fail is fine;
it just has to be made on both sides in the same breath.
"""

from __future__ import annotations

import math

from casebroker import footprints


def test_the_building_query_half_width_matches_the_runner():
    """`gba.fetch(lat, lon, half_m=520.0)` -- the 504 m core plus a margin."""
    assert footprints.HALF_M == 520.0


def test_the_raster_half_width_matches_the_meshed_domain():
    """site_geometry.build_site reads terrain over `half_t = HALF_M + buffer_m`.

    HALF_M there is the 504 m sampled core and terrain.BUFFER_M is 800.0, so the
    rasters span 1304 m -- not the 520 m the buildings are queried over. Cropping
    them to the building box would show a third of the ground the solve sits on.
    """
    assert footprints.DOMAIN_HALF_M == 504.0 + 800.0


def test_the_degrees_per_metre_constants_match():
    """Same two constants the runner uses, and they are not interchangeable.

    111_320 is the equatorial degree of longitude and 110_540 the mean degree of
    latitude; swapping them shifts a site by hundreds of metres in a way that
    still produces a perfectly plausible-looking tile.
    """
    lat, lon, half = 33.749, -84.388, footprints.HALF_M
    dlat = half / 110_540.0
    dlon = half / (111_320.0 * math.cos(math.radians(lat)))
    got = footprints.bbox_for(lat, lon, half)
    want = (f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}")
    assert got == want


def test_the_bbox_is_symmetric_and_the_right_size_on_the_ground():
    """A metre check, independent of the formula above."""
    lat, lon = 33.749, -84.388
    xmin, ymin, xmax, ymax = (float(v) for v in
                              footprints.bbox_for(lat, lon).split(","))
    north_south_m = (ymax - ymin) * 110_540.0
    east_west_m = (xmax - xmin) * 111_320.0 * math.cos(math.radians(lat))
    assert abs(north_south_m - 2 * footprints.HALF_M) < 1.0, north_south_m
    assert abs(east_west_m - 2 * footprints.HALF_M) < 1.0, east_west_m
    assert abs((xmin + xmax) / 2 - lon) < 1e-9
    assert abs((ymin + ymax) / 2 - lat) < 1e-9


def test_the_release_pin_is_still_the_one_the_runner_meshes():
    """Overture's release, for the fallback path and for pre-switch cases."""
    assert footprints.RELEASE == "2026-08-19.0"


def test_the_gba_mirror_is_the_source_cooperative_one():
    """Not TUM's WFS, which answers GetFeature with PARAMETER_NOT_ALLOWED."""
    assert footprints.GBA_BASE == (
        "https://data.source.coop/tge-labs/globalbuildingatlas-lod1")


def test_the_canopy_threshold_matches_the_crown_builder():
    """canopy_zones.py reports coverage above 2 m; below that it is shrub noise."""
    assert footprints.CANOPY_MIN_M == 2.0


def test_the_vegetation_table_matches_the_runners():
    """canopy_zones.VEGETATION_BY_LATITUDE, mirrored.

    One class per case, chosen by latitude alone -- so this table plus the site
    centre IS the entire vegetation model. If it drifts from the runner's, the
    preview states a drag coefficient the solve will not use.
    """
    from casebroker.footprints import VEGETATION_BY_LATITUDE as V

    assert [lim for lim, _ in V] == [23.5, 55.0, 90.1]
    assert [c["label"] for _, c in V] == [
        "Ficus (banyan)", "Deciduous tree", "Conifer"]
    assert [(c["lad"], c["cd"]) for _, c in V] == [
        (1.5, 0.25), (1.2, 0.20), (2.5, 0.25)]


def test_the_class_is_picked_by_latitude_and_nothing_else():
    from casebroker.footprints import vegetation_class as v

    assert v(1.35)["label"] == "Ficus (banyan)"       # Singapore
    assert v(33.75)["label"] == "Deciduous tree"      # Atlanta
    assert v(-33.87)["label"] == "Deciduous tree"     # Sydney: |lat|, so symmetric
    assert v(55.68)["label"] == "Conifer"             # Copenhagen
    assert v(89.0)["label"] == "Conifer"


def test_the_band_edges_are_where_the_runner_puts_them():
    """Hard cuts, and the 55-degree one is a 2.6x jump in drag."""
    from casebroker.footprints import vegetation_class as v

    assert v(54.99)["label"] == "Deciduous tree"
    assert v(55.01)["label"] == "Conifer"
    assert round(v(55.01)["f_per_m"] / v(54.99)["f_per_m"], 2) == 2.60


def test_f_is_twice_cd_times_lad():
    """The porosityForce the canopy cellZone actually carries."""
    from casebroker.footprints import vegetation_class as v

    for lat in (1.0, 33.0, 60.0):
        g = v(lat)
        assert g["f_per_m"] == round(2 * g["cd"] * g["lad"], 4)
