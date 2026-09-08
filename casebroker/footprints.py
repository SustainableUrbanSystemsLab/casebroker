"""Building footprints for one case, from the same Overture release the runner uses.

This exists so a case can be eyeballed before it finishes. The dashboard cannot
fetch Overture itself: Overture publishes GeoParquet on S3 and a Python client,
with no REST API and no published tile endpoints, so a browser has nothing to
call. The broker does the query and hands back GeoJSON.

**It must be the same data the CFD actually meshes.** The release is pinned to the
identical string ``overture_3d.RELEASE`` uses, and the bbox is derived the same
way from the case's own coordinates. Rendering something merely similar -- OSM
footprints, a map tile -- would be worse than rendering nothing: it would look
like a check while quietly disagreeing with the geometry, and the sites where it
disagreed most would be exactly the ones worth checking (China and much of
Africa, where Overture is empty but OSM is not).

Cached in the database after the first fetch. The query takes a few seconds and
the answer cannot change for a pinned release, so paying it once per case keeps
a dashboard refresh from re-querying S3 on every inspector open.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from typing import Any

# Same pin as benchmark/real_cities/overture_3d.py. If that moves, this moves
# with it, or the picture stops matching the mesh.
RELEASE = "2026-08-19.0"
HALF_M = 520.0          # what the runner requests: the 504 m core plus a margin


def bbox_for(lat: float, lon: float, half_m: float = HALF_M) -> str:
    dlat = half_m / 110_540.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat)))
    return f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}"


def _round_geom(geom, nd=6):
    def ring(r):
        return [[round(float(x), nd), round(float(y), nd)] for x, y, *_ in r]
    t = geom.get("type")
    if t == "Polygon":
        return {"type": t, "coordinates": [ring(r) for r in geom["coordinates"]]}
    return {"type": t,
            "coordinates": [[ring(r) for r in poly] for poly in geom["coordinates"]]}


def _height(props: dict[str, Any]) -> float | None:
    """Best available height, mirroring overture_3d._height."""
    h = props.get("height")
    if h:
        return float(h)
    n = props.get("num_floors")
    if n:
        return float(n) * 3.0
    return None


def fetch(lat: float, lon: float, timeout: int = 120) -> dict[str, Any]:
    """Footprints as a compact GeoJSON FeatureCollection.

    Only the polygon rings and a height survive: the full Overture record carries
    sources, ids and classifications that would multiply the payload for a
    picture that shows none of it.
    """
    with tempfile.NamedTemporaryFile(suffix=".geojson", delete=False) as tmp:
        path = tmp.name
    try:
        r = subprocess.run(
            [sys.executable, "-m", "overturemaps", "download",
             "--bbox", bbox_for(lat, lon), "-f", "geojson",
             "-t", "building", "-r", RELEASE, "-o", path],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(
                f"overture download failed (rc={r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()[-400:]}")
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    finally:
        import os
        try:
            os.unlink(path)
        except OSError:
            pass

    feats = []
    for f in raw.get("features", []):
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        # Coordinates rounded to 6 decimals, about 0.1 m. Overture ships far more
        # precision than a building footprint means, and the raw payload for one
        # dense tile is ~360 KB of digits nobody can see -- this roughly halves
        # what crosses the wire for a drawing whose finest feature is one pixel.
        feats.append({"type": "Feature",
                      "properties": {"h": _height(f.get("properties") or {})},
                      "geometry": _round_geom(geom)})
    return {"type": "FeatureCollection", "release": RELEASE,
            "centre": [lat, lon], "half_m": HALF_M,
            "n": len(feats), "features": feats}

# Same COG the geometry builder reads (benchmark/real_cities/terrain.py). Global
# 30 m bare-earth DTM, CC BY 4.0, Cloud-Optimized, so a windowed read is about a
# second and downloads none of the global file.
GEDTM30 = ("/vsicurl/https://s3.opengeohub.org/global/dtm/v1.2/"
           "gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif")


def terrain(lat: float, lon: float, half_m: float = 1304.0, n: int = 48) -> dict:
    """Whether this site has real bare-earth terrain, and how much relief.

    Answers before the case runs what the runner would otherwise only discover
    while building geometry: GEDTM30 has gaps, and where it does the builder
    falls back to a flat plane. A flat tile is a legitimate case -- it is what
    dense cores get anyway -- but it is a different case, and finding out after
    66 core-hours is worse than finding out now.

    Coarse on purpose: 48x48 over the whole domain is enough to say "there is
    relief here and roughly this much" without pulling a full-resolution window
    for a one-line answer.

    Never raises. A dead DTM host is a fact about today, not about the site, and
    a preview panel must not fail because a third party is down.
    """
    try:
        import math as _math

        import rasterio
        from rasterio.enums import Resampling
        from rasterio.windows import from_bounds
    except Exception:                                    # noqa: BLE001
        return {"source": "unknown", "detail": "rasterio not installed on the broker"}

    dlat = half_m / 110_540.0
    dlon = half_m / (111_320.0 * _math.cos(_math.radians(lat)))
    try:
        with rasterio.open(GEDTM30) as ds:
            w = from_bounds(lon - dlon, lat - dlat, lon + dlon, lat + dlat, ds.transform)
            a = ds.read(1, window=w, out_shape=(n, n), resampling=Resampling.bilinear)
    except Exception as e:                               # noqa: BLE001
        return {"source": "unavailable", "detail": str(e)[:160]}

    vals = [float(v) for row in a for v in row if v is not None and float(v) < 1e30]
    if not vals:
        # Genuine nodata, not an outage: the builder will use a flat plane here.
        return {"source": "flat", "detail": "GEDTM30 has no data at this site"}
    lo, hi = min(vals), max(vals)
    return {"source": "gedtm30", "relief_m": round(hi - lo, 1),
            "min_m": round(lo, 1), "max_m": round(hi, 1)}
