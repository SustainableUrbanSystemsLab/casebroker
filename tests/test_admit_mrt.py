"""Which MRT cases scripts/admit_mrt.py posts, and what each carries (docs/mrt.md).

One per finished surface case whose site has none yet; the surface case named
as the need and in the spec, its weather and links carried over, so the node
reads the archive the same file was computed with.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("admit_mrt", ROOT / "scripts" / "admit_mrt.py")
admit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admit)

WEATHER = {"key": "USA_GA_x_TMYx", "url": "https://climate.onebuilding.org/x.zip", "distance_km": 12.74}


def surf(i: int, state: str = "done", recipe: str = admit.SURF_RECIPE, weather: dict | None = WEATHER) -> dict:
    spec = {"lat": 10.0 + i, "lon": 20.0 + i, "recipe": recipe, "lcz": "LCZ3", "wind_case": f"v2-wind{i:012x}"}
    if weather is not None:
        spec["weather"] = weather
    return {"case_id": f"v2-surf{i:012x}", "state": state, "recipe": recipe, "lcz": "LCZ3",
            "city_cluster": "atlanta", "spec": spec}


def test_only_finished_surface_cases_of_the_pinned_version_are_proposed_in_a_stable_order():
    cases = [surf(3), surf(1), surf(2, state="pending"), surf(4, recipe="surf-1008/rad6R0P2-fft-v1")]
    assert [c["case_id"] for c in admit.done_surface_cases(cases)] == ["v2-surf000000000001", "v2-surf000000000003"]


def test_a_surface_case_that_already_has_an_mrt_case_is_not_proposed_again():
    mrt = admit.mrt_case(surf(1), "mrt-v1")
    assert admit.taken_surface_cases([{"recipe": admit.RECIPE, "spec": mrt["spec"]},
                                      {"recipe": "cyl-1008/of12-v6", "spec": {"surf_case": "v2-surf000000000002"}}]) \
        == {"v2-surf000000000001"}


def test_an_mrt_case_needs_its_surface_case_and_carries_its_weather_and_links():
    case = admit.mrt_case(surf(7), "mrt-v1")
    assert (case["lat"], case["lon"], case["city_cluster"], case["lcz"]) == (17.0, 27.0, "atlanta", "LCZ3")
    assert case["recipe"] == admit.RECIPE and case["priority"] == 300
    assert case["needs"] == "v2-surf000000000007"
    assert case["spec"]["surf_case"] == "v2-surf000000000007"
    assert case["spec"]["weather"] == WEATHER
    assert case["spec"]["wind_case"] == "v2-wind000000000007"
    assert case["labels"] == {"campaign": "mrt-v1"}


def test_a_surface_case_without_weather_cannot_seed_an_mrt_case():
    with pytest.raises(ValueError, match="weather"):
        admit.mrt_case(surf(1, weather=None), "mrt-v1")
