"""Which sites the thermal pilot takes, and which weather each gets (scripts/admit_thermal.py).

The pilot is a sample of the wind campaign's finished sites, and a sample is only
as good as its spread: across LCZs first, then across cities within one. The
weather is chosen once, at admission, and written into the spec, so it has to be
the same kind of year everywhere: the whole-record TMYx, never one of its windows.
"""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("admit_thermal", ROOT / "scripts" / "admit_thermal.py")
admit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admit)


def wind(i: int, lcz: str, city: str) -> dict:
    return {"case_id": f"v2-{i:016x}", "lcz": lcz, "city_cluster": city,
            "spec": {"lat": 10.0 + i, "lon": 20.0 + i, "recipe": "cyl-1008/of12-v5"}}


def test_the_sample_spreads_across_lczs_before_it_repeats_one():
    done = [wind(i, lcz, "c0") for i, lcz in enumerate(["LCZ2"] * 5 + ["LCZ5"] * 5 + ["LCZ9"])]
    picked = admit.pick_sites(done, 4, set())
    assert [p["lcz"] for p in picked] == ["LCZ2", "LCZ5", "LCZ9", "LCZ2"]


def test_within_an_lcz_a_city_is_not_repeated_until_every_city_has_had_a_turn():
    done = [wind(1, "LCZ2", "a"), wind(2, "LCZ2", "a"), wind(3, "LCZ2", "b")]
    assert [p["city_cluster"] for p in admit.pick_sites(done, 3, set())] == ["a", "b", "a"]


def test_a_site_with_a_thermal_case_is_not_proposed_again_and_the_order_is_stable():
    done = [wind(i, "LCZ2", f"c{i}") for i in range(4)]
    first = admit.pick_sites(done, 2, set())
    again = admit.pick_sites(list(reversed(done)), 2, {first[0]["case_id"]})
    assert first[1] in again and first[0] not in again


def test_the_weather_is_the_nearest_whole_record_tmyx():
    stations = [
        {"key": "a", "dataset": "TMY3", "distanceKm": 1.0, "url": "u"},
        {"key": "b", "dataset": "TMYx.2009-2023", "distanceKm": 2.0, "url": "u"},
        {"key": "c", "dataset": "TMYx", "distanceKm": 9.0, "url": "u"},
        {"key": "d", "dataset": "TMYx", "distanceKm": 4.0, "url": "u"},
    ]
    assert admit.choose_station(stations)["key"] == "d"
    assert admit.choose_station([stations[0], stations[1]]) is None


def test_a_thermal_case_keeps_the_wind_cases_site_split_and_a_link_to_it():
    station = {"key": "USA_GA_x_TMYx", "url": "https://climate.onebuilding.org/x.zip", "dataset": "TMYx",
               "distanceKm": 12.739, "name": "X", "period": "53 years"}
    case = admit.thermal_case(wind(7, "LCZ3", "atlanta"), station, "2026-09-01", "thermal-pilot")
    assert (case["lat"], case["lon"], case["city_cluster"], case["lcz"]) == (17.0, 27.0, "atlanta", "LCZ3")
    assert case["recipe"] == admit.RECIPE and case["priority"] == 200
    assert case["spec"]["wind_case"] == "v2-0000000000000007"
    assert case["spec"]["weather"]["url"].endswith(".zip") and case["spec"]["weather"]["distance_km"] == 12.74
    assert case["labels"] == {"campaign": "thermal-pilot"}
