"""Asking the stored wind field questions without downloading it (casebroker/umag.py).

The broker holds every finished direction's |U| at 1.75 m as a megabyte of float32;
a campaign is 160,000 of them. What has to hold: each field is summarised when it is
stored (and fields stored before that are summarised after), fields are found across
cases by what they hold, a point reads |U| per direction from a few bytes in place, a
region reads only its rows -- and a field stored gzip-wrapped, which cannot be read in
place, answers every question the same.
"""
from __future__ import annotations

import gzip
import json
import math
import pathlib
import struct
import sys

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, footprints, umag  # noqa: E402
from casebroker.app import create_app  # noqa: E402

V4, V6 = "cyl-1008/of12-v4", "cyl-1008/of12-v6"
W = {"Authorization": "Bearer w"}
R = {"Authorization": "Bearer r"}
NAN = float("nan")

# A 4 x 3 lattice, 2 m apart: x -3, -1, 1, 3 and y -2, 0, 2 (row-major, x fastest).
GRID = {"nx": 4, "ny": 3, "x0": -3.0, "y0": -2.0, "spacing_m": 2.0}
VALUES = [1.0, 2.0, 3.0, 4.0,
          5.0, NAN, 7.0, 8.0,
          9.0, 10.0, 11.0, 12.0]


def blob(values, direction="case_000", deg=0.0, u_ref=2.0, height=1.75, gz=True, **extra) -> bytes:
    header = {"format": "umag/1", "direction": direction, "deg": deg, "height_m": height,
              "coverage": 0.9, "u_ref": u_ref, "umag_p999": 12.0, **GRID, **extra}
    hb = json.dumps(header, separators=(",", ":")).encode()
    container = b"UMAG" + struct.pack("<II", 1, len(hb)) + hb + struct.pack("<%df" % len(values), *values)
    return gzip.compress(container, mtime=0) if gz else container


class Broker:
    """An app over SQLite with cases added and leased, fields put the way a node does."""

    def __init__(self, tmp_path):
        self.app = create_app(db_path=str(tmp_path / "q.sqlite"), tokens=["w"], readonly_tokens=["r"])
        self.c = TestClient(self.app)
        self.conn = None

    def case(self, lat, lon, recipe=V4, lcz="LCZ2", city="atl") -> tuple[str, str]:
        r = self.c.post("/v1/cases", json=[{"lat": lat, "lon": lon, "recipe": recipe, "city_cluster": city,
                                            "lcz": lcz}], headers=W)
        assert r.status_code == 200, r.text
        lease = self.c.post("/v1/lease", json={"worker_id": "foam-%s" % lat, "count": 1, "recipes": [recipe]},
                            headers=W).json()[0]
        return lease["case_id"], lease["lease_id"]

    def put(self, case_id, lease_id, direction, body):
        r = self.c.put(f"/v1/cases/{case_id}/fields/{direction}", params={"lease_id": lease_id},
                       content=body, headers={**W, "Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text

    def get(self, path, **params):
        return self.c.get(path, params=params, headers=R)


@pytest.fixture()
def broker(tmp_path):
    return Broker(tmp_path)


@pytest.fixture()
def one(broker):
    """One case at 33.8 N, 84.4 W with two directions; case_090 is twice case_000."""
    case_id, lease_id = broker.case(33.8, -84.4)
    broker.put(case_id, lease_id, "case_000", blob(VALUES))
    broker.put(case_id, lease_id, "case_090", blob([2 * v for v in VALUES], direction="case_090", deg=90.0))
    return broker, case_id


# -- the arithmetic --------------------------------------------------------------------

def test_a_field_is_summarised_over_its_air_and_nothing_else():
    st = umag.stats(np.array(VALUES, dtype=np.float32))
    air = [v for v in VALUES if not math.isnan(v)]
    assert st["n_valid"] == 11
    assert st["umag_min"] == 1.0 and st["umag_max"] == 12.0
    assert st["umag_mean"] == pytest.approx(sum(air) / 11)
    assert st["umag_p50"] == pytest.approx(float(np.percentile(air, 50)))
    assert st["umag_p95"] == pytest.approx(float(np.percentile(air, 95)))
    assert umag.stats(np.array([NAN, NAN]))["umag_mean"] is None


def test_the_site_frame_is_the_one_the_node_and_the_preview_use():
    """Eddy3D MetaFOAM.Site.SiteFrame: 110 540 m per degree of latitude and 111 320
    cos(lat) per degree of longitude -- and footprints' preview window says the same."""
    west, south, east, north = (float(v) for v in footprints.bbox_for(33.8, -84.4, 500.0).split(","))
    assert umag.to_local(north, east, 33.8, -84.4) == pytest.approx((500.0, 500.0), abs=0.1)
    assert umag.to_local(south, west, 33.8, -84.4) == pytest.approx((-500.0, -500.0), abs=0.1)
    lat, lon = umag.to_geographic(250.0, -125.0, 33.8, -84.4)
    assert umag.to_local(lat, lon, 33.8, -84.4) == pytest.approx((250.0, -125.0))


def test_a_point_between_nodes_is_bilinear_and_a_wall_is_left_out():
    grid = umag.Lattice(**{"x0": -3.0, "y0": -2.0, "spacing_m": 2.0, "nx": 4, "ny": 3})
    assert umag.corners(grid, 0.0, 1.0) == (1, 2, 1, 2, 0.5, 0.5)
    assert umag.bilinear(1.0, 3.0, 5.0, 7.0, 0.5, 0.5) == (4.0, 4)
    # One node inside a building: the other three, reweighted.
    value, n = umag.bilinear(NAN, 3.0, 5.0, 7.0, 0.5, 0.5)
    assert n == 3 and value == pytest.approx(5.0)
    assert umag.bilinear(NAN, NAN, NAN, NAN, 0.5, 0.5) == (None, 0)
    # Half a spacing past the outermost node is that node; further is outside.
    assert umag.corners(grid, -4.0, -3.0)[4:] == (0.0, 0.0)
    with pytest.raises(umag.OutsideGrid):
        umag.corners(grid, -4.1, 0.0)


# -- stored with the field --------------------------------------------------------------

def test_the_broker_summarises_a_field_as_it_stores_it(one):
    broker, case_id = one
    f = {r["direction"]: r for r in broker.get(f"/v1/cases/{case_id}/fields").json()["fields"]}
    expected = umag.stats(np.array(VALUES, dtype=np.float32))
    for k, v in expected.items():
        assert f["case_000"][k] == pytest.approx(v), k
    assert f["case_090"]["umag_max"] == pytest.approx(24.0)
    assert "data_offset" not in f["case_000"], "how the broker reads the blob is its own business"


def test_fields_stored_before_the_statistics_get_them_and_gzip_ones_too(broker):
    """A row stored gzip-wrapped (before 2026-10-06) cannot be read in place: it gets
    its statistics and no data_offset, and every question reads it whole instead."""
    case_id, lease_id = broker.case(10.0, 20.0)
    broker.put(case_id, lease_id, "case_000", blob(VALUES))
    conn = db.connect(broker.app.state.db_path)
    legacy = blob(VALUES, direction="case_090", deg=90.0)
    conn.execute(
        "INSERT INTO case_fields(case_id, direction, height_m, deg, nx, ny, x0, y0, spacing_m, u_ref,"
        " sha256, bytes, blob, reported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, "case_090", 1.75, 90.0, 4, 3, -3.0, -2.0, 2.0, 2.0, "x", len(legacy), legacy, 1))
    conn.execute("UPDATE case_fields SET n_valid=NULL, umag_mean=NULL, data_offset=NULL WHERE direction='case_000'")
    assert db.summarise_stored_fields(conn) == {"summarised": 2, "failed": 0, "remaining": 0}
    rows = {r["direction"]: dict(r) for r in conn.execute(
        "SELECT direction, data_offset, n_valid, umag_mean FROM case_fields").fetchall()}
    assert rows["case_000"]["data_offset"] == umag.data_offset(gzip.decompress(blob(VALUES)))
    assert rows["case_090"]["data_offset"] is None and rows["case_090"]["n_valid"] == 11
    assert db.summarise_stored_fields(conn)["summarised"] == 0, "nothing left to do"

    point = broker.get(f"/v1/cases/{case_id}/umag", x=1.0, y=0.0).json()["directions"]
    assert [d["umag"] for d in point] == [pytest.approx(7.0), pytest.approx(7.0)]
    region = broker.get(f"/v1/cases/{case_id}/umag/stats", bbox="-1,-2,3,0").json()["directions"]
    assert region[0]["umag_mean"] == pytest.approx(region[1]["umag_mean"])


# -- across cases --------------------------------------------------------------------------

def test_fields_are_found_across_cases_by_what_they_hold(broker):
    calm, lease = broker.case(48.14, 11.58, lcz="LCZ2")
    broker.put(calm, lease, "case_000", blob(VALUES))
    windy, lease = broker.case(40.42, -3.70, lcz="LCZ5")
    broker.put(windy, lease, "case_000", blob([3 * v for v in VALUES]))
    broker.put(windy, lease, "case_180", blob([0.5 * v for v in VALUES], direction="case_180", deg=180.0))

    every = broker.get("/v1/fields").json()
    assert every["total"] == 3
    rec = [f for f in every["fields"] if f["case_id"] == calm][0]
    assert (rec["lat"], rec["lon"], rec["lcz"], rec["recipe"]) == (48.14, 11.58, "LCZ2", V4)
    assert rec["vr_max"] == pytest.approx(12.0 / 2.0) and "blob" not in rec

    got = broker.get("/v1/fields", where="vr_p95>=10").json()["fields"]
    assert [(f["case_id"], f["direction"]) for f in got] == [(windy, "case_000")]
    got = broker.get("/v1/fields", lcz="LCZ5", sort="-umag_mean").json()["fields"]
    assert [f["direction"] for f in got] == ["case_000", "case_180"]
    got = broker.get("/v1/fields", where=["deg>=90", "umag_max<10"]).json()["fields"]
    assert [(f["case_id"], f["direction"]) for f in got] == [(windy, "case_180")]
    assert broker.get("/v1/fields", direction="case_180").json()["total"] == 1
    assert broker.get("/v1/fields", recipe=V6).json()["total"] == 0


def test_a_query_that_is_not_one_is_refused_by_name(broker):
    r = broker.get("/v1/fields", where="vr_p95 >> 1")
    assert r.status_code == 422 and "vr_p95>=1.2" in r.text
    r = broker.get("/v1/fields", where="blob>1")
    assert r.status_code == 422 and "not a field number" in r.text
    assert broker.get("/v1/fields", sort="-secret").status_code == 422


def test_a_ratio_without_a_reference_wind_is_missing_not_a_division_by_zero(broker):
    case_id, lease = broker.case(-1.29, 36.82)
    broker.put(case_id, lease, "case_000", blob(VALUES, u_ref=0.0))
    rec = broker.get("/v1/fields").json()["fields"][0]
    assert rec["vr_mean"] is None and rec["umag_mean"] is not None
    assert broker.get("/v1/fields", where="vr_mean>0").json()["total"] == 0
    assert broker.get("/v1/fields", sort="vr_mean").status_code == 200


def test_the_published_height_is_the_default_and_another_is_asked_for_exactly(broker):
    case_id, lease = broker.case(28.61, 77.21)
    broker.put(case_id, lease, "case_000", blob(VALUES))
    broker.put(case_id, lease, "case_000", blob([v * 2 for v in VALUES], height=10.0))
    assert [f["height_m"] for f in broker.get("/v1/fields").json()["fields"]] == [1.75]
    assert [f["height_m"] for f in broker.get("/v1/fields", height_m=10.0).json()["fields"]] == [10.0]
    pt = broker.get(f"/v1/cases/{case_id}/umag", x=-3, y=-2, height_m=10.0).json()["directions"]
    assert pt[0]["umag"] == pytest.approx(2.0)


# -- a point -------------------------------------------------------------------------------

def test_a_point_reads_every_direction_in_place(one):
    broker, case_id = one
    got = broker.get(f"/v1/cases/{case_id}/umag", x=0.0, y=1.0).json()
    assert [d["direction"] for d in got["directions"]] == ["case_000", "case_090"]
    # Nodes (1,1)=NaN, (2,1)=7, (1,2)=10, (2,2)=11 at the middle: the wall left out.
    assert got["directions"][0]["umag"] == pytest.approx((7 + 10 + 11) / 3)
    assert got["directions"][0]["nodes_valid"] == 3
    assert got["directions"][1]["umag"] == pytest.approx(2 * (7 + 10 + 11) / 3)
    assert got["directions"][0]["vr"] == pytest.approx(got["directions"][0]["umag"] / 2.0)
    assert got["lat"] == pytest.approx(33.8 + 1.0 / 110_540.0)
    on_node = broker.get(f"/v1/cases/{case_id}/umag", x=3.0, y=2.0, direction="case_090").json()["directions"]
    assert [(d["direction"], d["umag"]) for d in on_node] == [("case_090", pytest.approx(24.0))]


def test_a_point_by_latitude_and_longitude_is_placed_by_the_site_frame(one):
    broker, case_id = one
    lat, lon = umag.to_geographic(-1.0, 2.0, 33.8, -84.4)
    got = broker.get(f"/v1/cases/{case_id}/umag", lat=lat, lon=lon).json()
    assert (got["x"], got["y"]) == (pytest.approx(-1.0), pytest.approx(2.0))
    assert got["directions"][0]["umag"] == pytest.approx(10.0)


def test_a_point_off_the_field_or_given_badly_is_refused(one):
    broker, case_id = one
    r = broker.get(f"/v1/cases/{case_id}/umag", x=50.0, y=0.0)
    assert r.status_code == 422 and "outside the field" in r.text
    assert broker.get(f"/v1/cases/{case_id}/umag", x=1.0).status_code == 422
    assert broker.get(f"/v1/cases/{case_id}/umag", x=1.0, y=1.0, lat=33.8, lon=-84.4).status_code == 422
    assert broker.get("/v1/cases/v2-nope/umag", x=1.0, y=1.0).status_code == 404
    assert broker.get(f"/v1/cases/{case_id}/umag", x=1.0, y=1.0, direction="case_270").status_code == 404


def test_a_point_inside_a_building_has_no_wind_rather_than_zero(broker):
    case_id, lease = broker.case(51.51, -0.13)
    walled = list(VALUES)
    walled[0] = walled[1] = walled[4] = NAN                 # (0,0) (1,0) (0,1); (1,1) is NaN already
    broker.put(case_id, lease, "case_000", blob(walled))
    got = broker.get(f"/v1/cases/{case_id}/umag", x=-2.0, y=-1.0).json()["directions"][0]
    assert got["umag"] is None and got["vr"] is None and got["nodes_valid"] == 0


# -- a region ---------------------------------------------------------------------------------

def test_the_whole_field_is_its_stored_statistics(one):
    broker, case_id = one
    whole = broker.get(f"/v1/cases/{case_id}/umag/stats").json()
    stored = {r["direction"]: r for r in broker.get(f"/v1/cases/{case_id}/fields").json()["fields"]}
    for d in whole["directions"]:
        for k in ("n_valid", "umag_mean", "umag_p50", "umag_p95", "umag_max"):
            assert d[k] == pytest.approx(stored[d["direction"]][k]), k
        assert d["cells"] == 12
    assert whole["region"]["area_m2"] == 12 * 4.0


def test_a_box_and_a_disc_read_their_own_cells(one):
    broker, case_id = one
    box = broker.get(f"/v1/cases/{case_id}/umag/stats", bbox="-1,0,3,2", direction="case_000").json()
    d = box["directions"][0]
    assert (d["cells"], d["n_valid"]) == (6, 5)            # x -1,1,3 x y 0,2; (−1,0) is the wall
    assert d["umag_mean"] == pytest.approx((7 + 8 + 10 + 11 + 12) / 5)
    disc = broker.get(f"/v1/cases/{case_id}/umag/stats", x=1.0, y=0.0, radius_m=2.0,
                      direction="case_000").json()
    d = disc["directions"][0]
    assert d["cells"] == 5                                  # (1,0) and its four neighbours
    assert d["umag_mean"] == pytest.approx((3 + 7 + 8 + 11) / 4)
    assert disc["region"]["centre"]["lat"] == pytest.approx(33.8)


def test_exceedance_is_the_share_of_air_strictly_above_each_threshold(one):
    broker, case_id = one
    got = broker.get(f"/v1/cases/{case_id}/umag/stats", above=[5, 10.5], above_vr=[1, 3],
                     direction="case_000").json()["directions"][0]
    assert got["above"] == {"5": pytest.approx(6 / 11), "10.5": pytest.approx(2 / 11)}   # 7..12; 11, 12
    assert got["above_vr"] == {"1": pytest.approx(9 / 11), "3": pytest.approx(6 / 11)}  # u_ref 2: U > 2, U > 6


def test_a_region_given_badly_is_refused(one):
    broker, case_id = one
    assert broker.get(f"/v1/cases/{case_id}/umag/stats", bbox="1,2,3").status_code == 422
    assert broker.get(f"/v1/cases/{case_id}/umag/stats", bbox="100,100,200,200").status_code == 422
    assert broker.get(f"/v1/cases/{case_id}/umag/stats", x=1.0, y=1.0).status_code == 422
