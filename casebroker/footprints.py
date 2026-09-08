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


# GlobalBuildingAtlas LoD1, via the Source Cooperative mirror. The geometry
# builder switched to this as its default height source, and this endpoint has
# to follow: a case inspector that draws a DIFFERENT set of buildings than the
# mesh contains is worse than no inspector, because it looks like a check.
#
# Not TUM's own WFS (tubvsig-so2sat-vm1.srv.mwn.de, layer global3D:lod1_global,
# which the MetaMAP Grasshopper plugin queries): that proxy now answers
# GetFeature with PARAMETER_NOT_ALLOWED, serving only GetCapabilities and
# DescribeFeatureType, and wants a browser User-Agent and Referer besides. The
# mirror is anonymous GeoParquet with a bbox column, so a bounding-box predicate
# prunes row groups and one site costs a few MB of range reads.
GBA_BASE = "https://data.source.coop/tge-labs/globalbuildingatlas-lod1"


def gba_tile_for(lat: float, lon: float) -> str:
    """The 5x5 degree tile key covering this point.

    Mirrors benchmark/real_cities/gba.py exactly; validated against all 922
    published keys. Duplicated rather than imported because that module lives in
    the parent repo, which the broker does not vendor.
    """
    west = int(math.floor(lon / 5.0) * 5)
    south = int(math.floor(lat / 5.0) * 5)
    east, north = west + 5, south + 5
    lonf = lambda v: ("e" if v >= 0 else "w") + f"{abs(v):03d}"   # noqa: E731
    latf = lambda v: ("n" if v >= 0 else "s") + f"{abs(v):02d}"   # noqa: E731
    return f"{lonf(west)}_{latf(north)}_{lonf(east)}_{latf(south)}"


def fetch_gba(lat: float, lon: float, half_m: float = HALF_M,
              timeout: int = 300) -> dict[str, Any]:
    """GBA footprints and predicted heights, as a compact FeatureCollection.

    ``h`` is the predicted height and ``v`` its variance. The variance is kept
    because it is the honest part: GBA gives a height for every building, but a
    prediction with variance 3 and one with variance 175 are not the same claim,
    and the inspector should be able to say so.
    """
    import duckdb

    dlat = half_m / 110_540.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat)))
    xmin, ymin, xmax, ymax = lon - dlon, lat - dlat, lon + dlon, lat + dlat
    url = f"{GBA_BASE}/{gba_tile_for(lat, lon)}.parquet"

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute(f"SET http_timeout={int(timeout) * 1000};")
    rows = con.execute(
        f"""
        SELECT ST_AsGeoJSON(geometry) AS gj, height, var
        FROM read_parquet('{url}')
        WHERE bbox.xmin < {xmax} AND bbox.xmax > {xmin}
          AND bbox.ymin < {ymax} AND bbox.ymax > {ymin}
        """
    ).fetchall()

    feats = []
    for gj, height, var in rows:
        geom = json.loads(gj)
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        feats.append({"type": "Feature",
                      "properties": {"h": None if height is None else float(height),
                                     "v": None if var is None else round(float(var), 1)},
                      "geometry": _round_geom(geom)})
    return {"type": "FeatureCollection", "release": "GBA.LoD1",
            "source": "globalbuildingatlas", "height_kind": "predicted",
            "centre": [lat, lon], "half_m": half_m,
            "n": len(feats), "features": feats}


def fetch(lat: float, lon: float, timeout: int = 120) -> dict[str, Any]:
    """Overture footprints as a compact GeoJSON FeatureCollection.

    Superseded by :func:`fetch_gba` for the campaign; kept because a case built
    before the switch was meshed from THIS source, and redrawing it from GBA
    would misrepresent what was actually solved.

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
