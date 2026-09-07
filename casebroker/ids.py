"""Stable case identity and split assignment.

Both exist for one reason: the dataset must grow from 5k to 10k to 30k cases
without disturbing what is already there. That rules out the v1 scheme, where
``make_splits.py`` shuffled a fixed case list under seed 42 — appending cases to
that list reshuffles every existing case into a different split, silently
invalidating every trained model and every published number.

The rules here have the property that adding cases is a pure append:

* a case's id is a function of the *site and recipe alone*, so it never depends
  on how many cases exist or on the order they were created;
* a case's split is a function of its *city cluster id alone*, so an existing
  case can never change split, and neighbouring tiles from one city can never
  straddle train and test (which would leak geometry across the split).
"""

from __future__ import annotations

import hashlib

# Bump only when the meaning of an id changes. Ids carry it, so a re-spec of the
# same site is a NEW case rather than a silent redefinition of an old one.
ID_SCHEME = "v2"

# Coordinate quantisation for the site key, in decimal degrees. 1e-5 deg is
# ~1.1 m at the equator: fine enough that two deliberately distinct sites never
# collide, coarse enough that re-deriving a site from the same source twice
# always lands on the same id despite float round-trips.
COORD_QUANT = 5

_SPLIT_BOUNDS = (("train", 70), ("val", 85), ("test", 100))


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def site_key(lat: float, lon: float) -> str:
    """Quantised coordinate string. The canonical spelling of "where"."""
    return f"{lat:.{COORD_QUANT}f},{lon:.{COORD_QUANT}f}"


def case_id(lat: float, lon: float, recipe: str) -> str:
    """A case's permanent name.

    ``recipe`` names the geometry+domain+solver recipe version (e.g.
    ``"fixed-cyl-500/of12"``). Changing the recipe deliberately produces a
    different id for the same coordinates, so the two runs coexist in the
    database instead of one overwriting the other.
    """
    return f"{ID_SCHEME}-{_digest(site_key(lat, lon), recipe)[:16]}"


# Salt for site SELECTION, separate from ID_SCHEME. Bump it only to deliberately
# re-draw the sample -- that produces a different dataset, not a bigger one.
SAMPLER_SCHEME = "sampler-v1"


def selection_rank(lat: float, lon: float, salt: str = SAMPLER_SCHEME) -> float:
    """A site's stable position in [0, 1) for threshold-based selection.

    Deliberately NOT derived from :func:`case_id`. ``case_id`` includes the recipe,
    by design, so that re-specifying a site is a new case rather than a silent
    redefinition of an old one. Ranking by it would mean that changing the CFD recipe
    -- swapping a box domain for a cylinder, say -- re-draws the entire sample into a
    different 5,000 sites, and nothing would report it. **Selection is a property of
    WHERE, not of how the site is solved.**

    Reproducible from the coordinates alone: :func:`site_key` quantises to
    ``COORD_QUANT`` decimals (~1.1 m), so a float round-trip through a GeoTIFF, a JSON
    file or a database cannot move a site's rank. It follows that the coordinates
    themselves must be deterministic too -- derive them from the pinned sampling
    lattice, never from something that shifts with a map release.
    """
    return int(_digest(salt, site_key(lat, lon))[:16], 16) / float(1 << 64)


def is_selected(lat: float, lon: float, threshold: float,
                salt: str = SAMPLER_SCHEME) -> bool:
    """Whether a site is in a dataset drawn at ``threshold``.

    Growing the dataset raises thresholds, and raising a threshold can only ADD sites.
    That is the whole append-only guarantee, and it is why selection is expressed as a
    threshold rather than as "the top N by rank": under top-N, a class running out
    forces the allocation to be re-cut, which can drop a site that has already been
    solved. Under a threshold it merely saturates.
    """
    return selection_rank(lat, lon, salt) < threshold


def split_for(city_cluster: str, holdout_salt: str = "") -> str:
    """train / val / test, assigned by CITY rather than by tile.

    Tiles from one city share morphology, street pattern and often whole
    buildings at their edges, so splitting per tile leaks the test set into
    training. Hashing the city cluster keeps every tile of a city on one side.

    ``holdout_salt`` lets a future dataset version re-cut the splits on purpose
    (a new salt = a new, reproducible partition) without ever re-cutting them by
    accident.
    """
    bucket = int(_digest(ID_SCHEME, holdout_salt, city_cluster)[:8], 16) % 100
    for name, upper in _SPLIT_BOUNDS:
        if bucket < upper:
            return name
    raise AssertionError("unreachable: bucket is always < 100")
