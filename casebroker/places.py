"""Where a case is: its country, and the nearest town with its distance.

Answered offline from two bundled files (built by ``scripts/build_places.py``):
Natural Earth 1:50m countries (public domain) and GeoNames towns of at least
15,000 people (CC BY 4.0, geonames.org). No request leaves the broker, so there
is no rate limit to share and nothing to fail when a third party is down.

The DISTANCE is part of the answer, not decoration. This campaign samples all
nine built LCZ classes, and the sparse ones are rural: across its 5,000 sites the
median distance to such a town is 6 km, but a quarter are more than 25 km from
one. "Kulebaki" for a site 70 km out would read as a claim that it is in Kulebaki.
So the region is only given when the town is close enough for its region to be
the site's, and the country always comes from the polygon the site is IN.

Loaded on first use and then held: ~34k towns and ~100k polygon vertices, kept
in flat arrays (a few MB) rather than as Python objects per point, because this
runs in a 512 MB instance.
"""
from __future__ import annotations

import gzip
import json
import math
import pathlib
import threading
from array import array
from functools import lru_cache
from typing import Any

DATA = pathlib.Path(__file__).resolve().parent / "geodata"
# Beyond this the town's region is not reported as the site's.
REGION_MAX_KM = 25.0
# Near a coast the 1:50m polygon can miss a site on the shore; the town's country
# is used instead when the town is this close, and the answer says so.
COAST_FALLBACK_KM = 50.0

_lock = threading.Lock()
_towns: dict[str, Any] | None = None
_countries: list[dict[str, Any]] | None = None


def _load() -> None:
    global _towns, _countries
    with _lock:
        if _towns is not None:
            return
        names, cc, region, pop = [], [], [], array("l")
        lat, lon = array("d"), array("d")
        grid: dict[tuple[int, int], list[int]] = {}
        text = gzip.decompress((DATA / "places_cities.tsv.gz").read_bytes()).decode("utf-8")
        for i, line in enumerate(l for l in text.split("\n") if l):
            n, la, lo, c, r, p = line.split("\t")
            names.append(n); cc.append(c); region.append(r); pop.append(int(p or 0))
            lat.append(float(la)); lon.append(float(lo))
            grid.setdefault((math.floor(float(la)), math.floor(float(lo))), []).append(i)
        countries = []
        for c in json.loads(gzip.decompress((DATA / "places_countries.json.gz").read_bytes())):
            rings = []
            for ring in c["rings"]:
                xs, ys = array("d", (p[0] for p in ring)), array("d", (p[1] for p in ring))
                rings.append((min(xs), min(ys), max(xs), max(ys), xs, ys))
            countries.append({"name": c["name"], "iso2": c["iso2"], "rings": rings})
        _countries = countries
        _towns = {"name": names, "cc": cc, "region": region, "pop": pop,
                  "lat": lat, "lon": lon, "grid": grid}
        _country_names.cache_clear()


@lru_cache(maxsize=1)
def _country_names() -> dict[str, str]:
    return {c["iso2"]: c["name"] for c in (_countries or []) if c["iso2"]}


def _km(la1: float, lo1: float, la2: float, lo2: float) -> float:
    a = (math.sin(math.radians(la2 - la1) / 2) ** 2
         + math.cos(math.radians(la1)) * math.cos(math.radians(la2))
         * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _inside(xs: array, ys: array, x: float, y: float) -> bool:
    inside, j = False, len(xs) - 1
    for i in range(len(xs)):
        if (ys[i] > y) != (ys[j] > y) and x < (xs[j] - xs[i]) * (y - ys[i]) / (ys[j] - ys[i]) + xs[i]:
            inside = not inside
        j = i
    return inside


def _country_at(lat: float, lon: float) -> dict[str, Any] | None:
    best, best_area = None, math.inf
    for c in _countries or []:
        for x0, y0, x1, y1, xs, ys in c["rings"]:
            if x0 <= lon <= x1 and y0 <= lat <= y1 and _inside(xs, ys, lon, lat):
                area = (x1 - x0) * (y1 - y0)
                # An enclave lies inside its surrounding country's ring as well;
                # the tighter polygon is the one the point is actually in.
                if area < best_area:
                    best, best_area = c, area
    return best


def _nearest_town(lat: float, lon: float) -> tuple[int, float] | None:
    t = _towns
    cy, cx = math.floor(lat), math.floor(lon)
    best, best_km = None, math.inf
    seen: list[tuple[float, int]] = []
    for r in range(0, 30):
        for a in range(cy - r, cy + r + 1):
            for b in range(cx - r, cx + r + 1):
                if max(abs(a - cy), abs(b - cx)) != r:
                    continue
                for i in t["grid"].get((a, ((b + 180) % 360) - 180), ()):
                    d = _km(lat, lon, t["lat"][i], t["lon"][i])
                    seen.append((d, i))
                    if d < best_km:
                        best, best_km = i, d
        # Everything beyond ring r is at least r cells away -- r degrees of
        # latitude, but only r*cos(lat) degrees' worth of kilometres in
        # longitude, and the lower bound has to use the narrower of the two or the
        # search stops before it has seen the nearest town at high latitude.
        shrink = math.cos(math.radians(min(89.0, abs(lat) + r)))
        if best is not None and best_km < r * 111.0 * shrink:
            break
    if best is None:
        return None
    # Among towns about as near as the nearest, the LARGEST: a point in
    # Manhattan is 4.0 km from Hoboken and 4.3 km from New York City, and
    # "Hoboken, New Jersey" is the wrong answer to where it is.
    # Bounded in km, not only as a ratio: 700 km from anywhere, a town 50 %
    # further away is another 350 km, not "about as near".
    close = [(t["pop"][i], -d, i) for d, i in seen if d <= best_km + min(best_km * 0.5, 10.0) + 2.0]
    _, neg_d, pick = max(close)
    return pick, -neg_d


@lru_cache(maxsize=20_000)
def locate(lat: float, lon: float) -> dict[str, Any]:
    """``{country, country_code, town, town_km, region, source}`` for a point."""
    _load()
    country = _country_at(lat, lon)
    near = _nearest_town(lat, lon)
    out: dict[str, Any] = {"country": None, "country_code": None, "town": None,
                           "town_km": None, "region": None, "country_source": None}
    if near:
        i, km = near
        out["town"], out["town_km"] = _towns["name"][i], round(km, 1)
        if km <= REGION_MAX_KM:
            out["region"] = _towns["region"][i] or None
    if country:
        out["country"], out["country_code"] = country["name"], country["iso2"]
        out["country_source"] = "polygon"
    elif near and near[1] <= COAST_FALLBACK_KM:
        code = _towns["cc"][near[0]]
        out["country"], out["country_code"] = _country_names().get(code, code), code
        out["country_source"] = "nearest town"
    return out
