#!/usr/bin/env python3
"""Rebuild casebroker/geodata/places_*.gz -- the offline country/town lookup behind a
case's "where is this" line (casebroker/places.py).

Offline on purpose. A geocoding API is either rate-limited to one request a
second (Nominatim's policy forbids bulk use outright) or billed, and the
dashboard asks the question for whichever case is open, as often as it is
opened. Two public datasets answer it locally instead:

* Natural Earth 1:50m admin-0 countries (public domain) -- the country, by
  point-in-polygon. Coordinates rounded to 0.001 deg (~100 m), which is far
  finer than the 1:50m source itself.
* GeoNames cities15000 (CC BY 4.0, https://www.geonames.org) -- the nearest
  town of at least 15,000 people, with its first-level region. Smaller towns
  were measured and bought little: on the campaign's 5,000 sites the median
  distance to a town is 6.0 km at >=15k and 5.4 km at >=5k.

    python3 scripts/build_places.py
"""
from __future__ import annotations

import gzip
import io
import json
import pathlib
import urllib.request
import zipfile

OUT = pathlib.Path(__file__).resolve().parents[1] / "casebroker" / "geodata"
NE = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
      "master/geojson/ne_50m_admin_0_countries.geojson")
CITIES = "https://download.geonames.org/export/dump/cities15000.zip"
ADMIN1 = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as r:
        return r.read()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    countries = []
    for f in json.loads(fetch(NE))["features"]:
        p, g = f["properties"], f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        # Exterior rings only: the few holes at 1:50m (enclaves such as Lesotho)
        # are their own features, so a point in one matches that country too and
        # the SMALLER polygon wins at lookup time.
        rings = [[[round(x, 3), round(y, 3)] for x, y in poly[0]] for poly in polys]
        iso = p.get("ISO_A2_EH") if p.get("ISO_A2") in (None, "-99") else p["ISO_A2"]
        countries.append({"name": p["NAME"], "iso2": iso if iso not in (None, "-99") else None,
                          "rings": rings})
    (OUT / "places_countries.json.gz").write_bytes(
        gzip.compress(json.dumps(countries, separators=(",", ":")).encode(), 9, mtime=0))

    admin1 = {}
    for line in fetch(ADMIN1).decode("utf-8").splitlines():
        code, name, *_ = line.split("\t")
        admin1[code] = name
    raw = zipfile.ZipFile(io.BytesIO(fetch(CITIES))).read("cities15000.txt").decode("utf-8")
    rows = []
    for line in raw.splitlines():
        c = line.split("\t")
        name, lat, lon, code, cc, a1, pop = c[1], c[4], c[5], c[7], c[8], c[10], c[14]
        # PPLX is a SECTION of a populated place ("Times Square", "Kreuzberg") and
        # PPLH/PPLQ/PPLW are historical, abandoned or destroyed ones: none is a
        # town a site is "near".
        if code in ("PPLX", "PPLH", "PPLQ", "PPLW"):
            continue
        rows.append("\t".join([name, f"{float(lat):.4f}", f"{float(lon):.4f}", cc,
                               admin1.get(f"{cc}.{a1}", ""), pop]))
    (OUT / "places_cities.tsv.gz").write_bytes(
        gzip.compress(("\n".join(sorted(rows)) + "\n").encode(), 9, mtime=0))
    print(f"{len(countries)} countries, {len(rows)} towns -> {OUT}")


if __name__ == "__main__":
    main()
