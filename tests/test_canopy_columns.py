"""The preview's crown map is the runner's: 4 m columns whose MEAN height is
at least 2 m (Eddy3D CanopyZones.Columns), not 21 m cells whose rounded mean
is (v2-022bfaf49d4a18a3, 2026-10-20: the coarse rule dropped every scattered
tree and drew a staircase where the mesh has a street row)."""

from __future__ import annotations

import base64

import numpy as np
import pytest

from casebroker import footprints


def _columns(monkeypatch, fine):
    pytest.importorskip("rasterio")
    seen = {}

    def window(url, lat, lon, half_m, n, resampling):
        if "CHM_acquisition_date" in url:               # the scene year: one cell, read beside the map
            return np.array([[18.83]], np.float32), 1.0
        seen["url"], seen["n"] = url, n
        return fine, 1.0

    monkeypatch.setattr(footprints, "_read_window", window)
    return seen


def test_a_column_is_a_crown_exactly_when_its_mean_reaches_two_metres(monkeypatch):
    fine = np.zeros((1008, 1008), np.float32)
    fine[0:4, 0:4] = 2.0                 # mean 2.0: a crown
    fine[0:4, 4:8] = 1.99                # mean 1.99: not
    fine[0:2, 8:10] = 8.0                # one tall tree, 4 of 16 m2: mean 2.0 -- a crown
    fine[4:6, 0:2] = 7.0                 # 4 of 16 m2 at 7 m: mean 1.75 -- not, however tall
    seen = _columns(monkeypatch, fine)

    c = footprints.canopy(30.0, 120.0, recipe="cyl-1008/of12-v6")
    assert seen["n"] == 1008, "read at ~1 m, a whole number of cells per column and per drawing cell"
    cols = c["columns"]
    assert (cols["n"], cols["cell_m"]) == (252, 4.0)
    a = np.frombuffer(base64.b64decode(cols["b64"]), np.uint8).reshape(252, 252)
    assert list(a[0, :3]) == [2, 1, 2] and a[1, 0] == 1
    assert c["frac_columns"] == round(2 / 252 ** 2, 4)
    assert c["n"] == 48 and len(c["grid"]) == 48 * 48, "the drawing grid is unchanged"


def test_the_v7_preview_reads_the_v2_tiles(monkeypatch):
    seen = _columns(monkeypatch, np.zeros((1008, 1008), np.float32))
    c = footprints.canopy(30.2377, 120.2454, recipe="cyl-1008/of12-v7")
    assert c["source"] == "meta-wri-chm-v2" and c["tile"] == "1321210312"
    assert "/forests/v2/global/dinov3_global_chm_v2_ml3/chm/1321210312.tif" in seen["url"]
    v6 = footprints.canopy(30.2377, 120.2454, recipe="cyl-1008/of12-v6")
    assert v6["source"] == "meta-wri-chm-v1" and v6["tile"] == "132121031"


def test_a_column_where_a_building_stands_is_dropped_only_for_a_recipe_that_drops_it(monkeypatch):
    """The runner's rule (CampaignCaseConfig.DropsCrownsUnderBuildings, v7 on):
    the preview mirrors it, by column centre -- the centre column under a
    footprint goes, its neighbours stay, and the count says what went."""
    fine = np.zeros((1008, 1008), np.float32)
    fine[500:512, 500:512] = 10.0              # a 3 x 3 block of 10 m columns at the centre
    _columns(monkeypatch, fine)
    lat, lon = 30.0, 120.0
    my, mx = 110_540.0, 111_320.0 * np.cos(np.radians(lat))
    # One footprint over the middle column only: its centre is at (2, 2) m of the local
    # frame (column 125, row 125 of 252: x in [0, 4), y in (0, -4]) -- wait, rows run north
    # down: the block spans rows 500-511 = y from +4 to -8, cols 500-511 = x from -4 to +8.
    # The middle column is x in [0, 4), y in (-4, 0]: centre (2, -2).
    ring = [((x / mx) + lon, (y / my) + lat) for x, y in ((0.5, -0.5), (3.5, -0.5), (3.5, -3.5), (0.5, -3.5), (0.5, -0.5))]
    fc = {"features": [{"geometry": {"type": "Polygon", "coordinates": [ring]}}]}

    c = footprints.canopy(lat, lon, recipe="cyl-1008/of12-v7")
    before = np.frombuffer(base64.b64decode(c["columns"]["b64"]), np.uint8).reshape(252, 252)
    assert (before >= 2).sum() == 9
    d = footprints.drop_columns_under_buildings(c, fc, lat, lon)
    after = np.frombuffer(base64.b64decode(d["columns"]["b64"]), np.uint8).reshape(252, 252)
    assert (after >= 2).sum() == 8 and after[126, 126] == 0 and after[125, 126] == 10
    assert d["columns_under_buildings"] == 1
    assert d["frac_columns"] == round(8 / 252 ** 2, 4)
    assert footprints.drops_crowns_under_buildings("cyl-1008/of12-v7")
    assert not footprints.drops_crowns_under_buildings("cyl-1008/of12-v6")
    # Nothing to drop against: the payload is handed back as it was.
    assert footprints.drop_columns_under_buildings(c, {"features": []}, lat, lon) is c


def test_the_scene_year_rides_along_when_the_date_raster_answers(monkeypatch):
    pytest.importorskip("rasterio")
    fine = np.zeros((1008, 1008), np.float32)

    def window(url, lat, lon, half_m, n, resampling):
        if "CHM_acquisition_date" in url:
            assert n == 1
            return np.array([[18.833334]], np.float32), 1.0
        return fine, 1.0

    monkeypatch.setattr(footprints, "_read_window", window)
    assert footprints.canopy(30.2377, 120.2454)["imagery_year"] == 2018.83

    def down(url, *a, **k):
        if "CHM_acquisition_date" in url:
            raise RuntimeError("curl: timeout")
        return fine, 1.0

    monkeypatch.setattr(footprints, "_read_window", down)
    c = footprints.canopy(30.2377, 120.2454)
    assert c["imagery_year"] is None and c["source"] == "meta-wri-chm-v1", "the date is best effort; the canopy is not"


def test_from_v7_the_vegetation_class_is_the_nodes_and_the_preview_says_so():
    """The node picks the row by Köppen from of12-v7 (Eddy3D KoppenVegetation);
    the broker has no climate index, so it shows the latitude row as a guess
    and marks it as the node's decision rather than claiming it."""
    v6 = footprints.vegetation_class(55.68, "cyl-1008/of12-v6")
    v7 = footprints.vegetation_class(55.68, "cyl-1008/of12-v7")
    assert "decided_by" not in v6 and v6["label"] == "Conifer"
    assert v7["decided_by"] == "node:koppen" and v7["label"] == "Conifer", "the row shown is still the table's"
    assert footprints.vegetation_class(55.68) == v6, "no recipe: the table, as before"
