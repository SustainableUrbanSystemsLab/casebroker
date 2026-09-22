"""A case says where it is: country by polygon, nearest town and its distance."""
import pytest
from fastapi.testclient import TestClient

from casebroker import places
from casebroker.app import create_app


@pytest.mark.parametrize("lat, lon, country, town", [
    (48.137, 11.575, "Germany", "Munich"),
    (-33.87, 151.21, "Australia", "Sydney"),
    (51.51, -0.13, "United Kingdom", "London"),
    (48.85, 2.35, "France", "Paris"),
    (-29.31, 27.48, "Lesotho", "Maseru"),          # an enclave: the tighter polygon wins
])
def test_well_known_places(lat, lon, country, town):
    p = places.locate(lat, lon)
    assert (p["country"], p["town"]) == (country, town)
    assert p["town_km"] < 5 and p["region"]


def test_a_city_section_is_not_a_town_and_the_bigger_of_two_near_towns_wins():
    # Midtown Manhattan: GeoNames lists "Times Square" (a section) and Hoboken
    # (4.0 km) nearer than New York City (4.3 km).
    p = places.locate(40.7484, -73.9857)
    assert p["town"] == "New York City" and p["region"] == "New York"


def test_a_remote_site_names_its_nearest_town_with_the_distance_and_no_region():
    p = places.locate(71.88399, 106.79183)          # a campaign site on the Taymyr Peninsula
    assert p["country"] == "Russia" and p["country_source"] == "polygon"
    assert p["town_km"] > 300 and p["region"] is None, "a region 600 km away is not the site's region"


def test_open_ocean_has_no_country():
    p = places.locate(7.5, -37.0)
    assert p["country"] is None and p["country_source"] is None


def test_the_case_record_carries_it(tmp_path):
    c = TestClient(create_app(str(tmp_path / "b.sqlite"), tokens=["w"]))
    W = {"Authorization": "Bearer w"}
    cid = None
    assert c.post("/v1/cases", json=[{"lat": 48.137, "lon": 11.575, "recipe": "r", "city_cluster": "m"}],
                  headers=W).json()["added"] == 1
    cid = c.get("/v1/cases", headers=W).json()["cases"][0]["case_id"]
    place = c.get(f"/v1/cases/{cid}", headers=W).json()["place"]
    assert place["country"] == "Germany" and place["town"] == "Munich"
