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


# -- versions --------------------------------------------------------------------
#
# A recipe version is a training set (docs/thermal.md, "Versions"). v1 traced the
# sun matrix without -d, so every sun also carried the whole sky; v2 is sun-only.

V1 = "surf-1008/rad6R0P2-fft-v1"


def test_the_script_posts_v2_and_never_a_withdrawn_version():
    assert admit.RECIPE == "surf-1008/rad6R0P2-fft-v2"
    assert V1 in admit.WITHDRAWN and admit.RECIPE not in admit.WITHDRAWN


def test_only_a_withdrawn_case_still_waiting_or_running_counts_as_in_flight():
    cases = [{"recipe": V1, "state": s} for s in ("pending", "leased", "leased", "done", "quarantined")]
    cases.append({"recipe": admit.RECIPE, "state": "pending"})
    assert admit.withdrawn_in_flight(cases) == {V1: 3}
    assert admit.withdrawn_in_flight([c for c in cases if c["state"] in ("done", "quarantined")]) == {}


class FakeBroker:
    """The broker as main() sees it: a policy that fences undeclared workers, and `held`."""
    held: list[dict] = []
    posted: list = []

    def __init__(self, url: str, token: str):
        pass

    def call(self, method, path, body=None):
        if path == "/v1/releases":
            return {"undeclared_recipes": list(admit.WIND_RECIPES)}
        if method == "POST":
            FakeBroker.posted.append(body)
            return {"added": len(body)}
        raise AssertionError(f"unexpected {method} {path}")

    def cases(self, **params):
        # Unfiltered, as a broker from before `recipe` was a filter answers: main() must filter.
        return [c for c in FakeBroker.held if params.get("state") in (None, c["state"])]


def run_main(monkeypatch, held):
    FakeBroker.held, FakeBroker.posted = held, []
    monkeypatch.setattr(admit, "Broker", FakeBroker)
    monkeypatch.setattr(admit, "e3d_catalogue", lambda e3d: None)
    return admit.main(["--broker", "https://broker.test", "--token", "t", "--e3d", "E3D", "--post"])


def test_posting_is_refused_while_a_withdrawn_case_is_pending_or_leased(monkeypatch):
    held = [{"case_id": "t1", "recipe": V1, "state": "leased", "spec": {}}]
    assert run_main(monkeypatch, held) == 2
    assert FakeBroker.posted == []


def test_a_finished_withdrawn_case_does_not_block_posting(monkeypatch):
    # The control: the same broker with the v1 case done gets past the guard (and,
    # with no finished wind site to pair, posts nothing and succeeds).
    held = [{"case_id": "t1", "recipe": V1, "state": "done", "spec": {}}]
    assert run_main(monkeypatch, held) == 0


# -- the v6 move, and an E3D that fails ----------------------------------------------

def test_a_site_is_linked_to_its_newest_wind_case_and_a_moved_one_is_left_out():
    site = {"lat": 38.91964, "lon": 121.64291}
    v4 = {"case_id": "v2-old", "recipe": "cyl-1008/of12-v4", "state": "quarantined", "spec": dict(site)}
    v6 = {"case_id": "v2-new", "recipe": "cyl-1008/of12-v6", "state": "pending", "spec": dict(site)}
    other = {"case_id": "v2-oth", "recipe": "cyl-1008/of12-v5", "state": "done", "spec": {"lat": 1.0, "lon": 2.0}}
    assert sorted(c["case_id"] for c in admit.one_per_site([v4, v6, other])) == ["v2-new", "v2-oth"]
    # A pending v4 next to a pending v6 of the same site: still one case, the v6 one.
    v4p = dict(v4, state="pending")
    assert [c["case_id"] for c in admit.one_per_site([v4p, v6])] == ["v2-new"]


def test_an_e3d_that_printed_nothing_is_an_error_not_no_station(monkeypatch):
    import subprocess
    answers = {"out": ""}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout=answers["out"], stderr="error: .NET number values such as positive and negative infinity"))
    find = admit.e3d_find("E3D")
    try:
        find(1.0, 2.0)
        raise AssertionError("an E3D that crashed was read as no station")
    except RuntimeError as e:
        assert "nothing printed" in str(e)
    answers["out"] = "[]"                     # exit 1 with an empty list IS no station
    assert find(1.0, 2.0) == []


def test_the_priority_is_the_callers():
    station = {"key": "k", "url": "u", "distanceKm": 1.0}
    assert admit.thermal_case(wind(1, "LCZ1", "c"), station, None, "p")["priority"] == admit.PRIORITY
    assert admit.thermal_case(wind(1, "LCZ1", "c"), station, None, "p", priority=40)["priority"] == 40
