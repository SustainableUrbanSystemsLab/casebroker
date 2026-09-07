"""Site selection must be append-only, and independent of how a site is solved.

Two failures these guard against, both silent:

* **Ranking by ``case_id``.** ``case_id`` includes the recipe, deliberately, so that
  re-specifying a site is a new case. Rank by it and changing the CFD recipe -- a box
  domain swapped for a cylinder -- re-draws the entire sample into a different 5,000
  sites. Nothing would report that; the campaign would simply start solving different
  places.
* **Top-N selection.** "Take the best N by rank" is not growth-stable: when a class
  runs out, the allocation has to be re-cut, and the re-cut can drop a site that has
  already cost 66 core-hours. A threshold can only ever add.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import ids  # noqa: E402


def sites(n, lat0=34.0, lon0=-84.0):
    """A spread of distinct sites; 0.01 deg apart is ~1.1 km, so these are real tiles."""
    return [(lat0 + (i // 100) * 0.01, lon0 + (i % 100) * 0.01) for i in range(n)]


def test_rank_is_reproducible_from_the_coordinates_alone():
    lat, lon = 40.75491, -73.98402
    assert ids.selection_rank(lat, lon) == ids.selection_rank(lat, lon)
    # A float round-trip through text, JSON or a raster must not move a site.
    assert ids.selection_rank(lat, lon) == ids.selection_rank(float(f"{lat:.10f}"), lon)
    # Quantisation is ~1.1 m: a sub-millimetre wobble is the same site.
    assert ids.selection_rank(lat, lon) == ids.selection_rank(lat + 1e-9, lon - 1e-9)


def test_ranking_by_case_id_would_redraw_the_sample_and_ours_cannot():
    """The regression that would be catastrophic and invisible.

    Our guarantee is STRUCTURAL -- ``selection_rank`` has no recipe parameter, so the
    recipe cannot reach it. Asserting "our rank equals our rank" would be vacuous, so
    this instead demonstrates the hazard on the tempting wrong implementation and
    pins the structural property that rules it out.
    """
    import inspect
    pool = sites(1000)

    assert "recipe" not in inspect.signature(ids.selection_rank).parameters
    assert "recipe" not in inspect.signature(ids.is_selected).parameters

    # The wrong implementation: rank by case_id, which carries the recipe.
    def rank_by_case_id(lat, lon, recipe):
        return int(ids.case_id(lat, lon, recipe)[3:19], 16) / float(1 << 64)

    box = {s for s in pool if rank_by_case_id(*s, "fixed-box-1008/of12") < 0.3}
    cyl = {s for s in pool if rank_by_case_id(*s, "cylinder-500/of12") < 0.3}
    assert box and cyl
    churn = len(box ^ cyl) / len(box)
    assert churn > 0.5, (
        "if this ever stops churning the demonstration is broken; ranking by case_id "
        f"re-drew {churn:.0%} of the sample on a recipe change")

    # A recipe change must still produce a distinct CASE -- that part is by design.
    lat, lon = pool[0]
    assert ids.case_id(lat, lon, "fixed-box-1008/of12") != ids.case_id(lat, lon, "cylinder-500/of12")


def test_raising_the_threshold_only_adds():
    """The append-only guarantee, stated as a set inclusion."""
    pool = sites(2000)
    prev = {s for s in pool if ids.is_selected(*s, 0.05)}
    assert prev, "fixture too small to say anything"
    for th in (0.10, 0.25, 0.5, 0.8, 1.0):
        cur = {s for s in pool if ids.is_selected(*s, th)}
        assert prev <= cur, f"threshold {th} dropped {len(prev - cur)} already-selected sites"
        prev = cur
    assert len(prev) == len(pool), "threshold 1.0 selects everything"


def test_adding_cities_never_displaces_an_existing_site():
    """The growth axis Patrick asked for: extend the candidate pool, not the rank cut."""
    city_a = sites(500, lat0=40.0, lon0=-74.0)
    city_b = sites(500, lat0=35.7, lon0=139.7)     # added later
    th = 0.3
    before = {s for s in city_a if ids.is_selected(*s, th)}
    after = {s for s in city_a + city_b if ids.is_selected(*s, th)}
    assert before <= after
    assert after - before, "the new city should contribute something at this threshold"
    # And every site the new city contributes really is from the new city.
    assert all(s in city_b for s in after - before)


def test_a_saturated_class_does_not_disturb_the_others():
    """Under thresholds, running out is saturation, not a re-cut.

    LCZ 1 has on the order of a thousand non-overlapping tiles on Earth, so a dense-
    weighted 5,000-tile draw exhausts it. That must not perturb any other class.
    """
    rare = sites(60, lat0=1.0, lon0=1.0)      # a small class
    common = sites(3000, lat0=20.0, lon0=20.0)

    # A saturated class means: take all of it.
    assert {s for s in rare if ids.is_selected(*s, 1.0)} == set(rare)

    # The property that makes per-class thresholds independent: whether a site is in
    # depends ONLY on (site, threshold) -- never on what else is in the pool, on how
    # many classes there are, or on the order anything was evaluated. Checked by
    # deciding the same sites against three different surrounding pools.
    th = 0.35
    alone = {s for s in common if ids.is_selected(*s, th)}
    with_rare = {s for s in common + rare if ids.is_selected(*s, th)} & set(common)
    reversed_order = {s for s in reversed(common) if ids.is_selected(*s, th)}
    assert alone == with_rare == reversed_order

    small = {s for s in common if ids.is_selected(*s, 0.2)}
    assert small <= alone, "raising a class's own threshold only adds"


def test_rank_is_in_range_and_roughly_uniform():
    r = [ids.selection_rank(*s) for s in sites(4000)]
    assert all(0.0 <= x < 1.0 for x in r)
    # A threshold is only a fair sampler if the rank is uniform: a threshold of t must
    # select about t of the pool, or the per-class quotas mean nothing.
    for t in (0.1, 0.25, 0.5, 0.75):
        frac = sum(x < t for x in r) / len(r)
        assert frac == pytest.approx(t, abs=0.03), f"threshold {t} selected {frac:.3f}"


def test_a_different_salt_draws_a_different_sample():
    """Re-drawing must be possible, but only deliberately."""
    pool = sites(1000)
    a = {s for s in pool if ids.is_selected(*s, 0.3)}
    b = {s for s in pool if ids.is_selected(*s, 0.3, salt="sampler-v2")}
    assert a != b
    # Overlap should be about 0.3 of the pool (independent draws), not near-identical.
    assert len(a & b) < 0.8 * len(a)
