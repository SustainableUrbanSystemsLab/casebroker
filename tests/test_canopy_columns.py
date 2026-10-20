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
