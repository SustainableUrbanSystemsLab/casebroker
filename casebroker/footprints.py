"""What one case is actually made of: buildings, terrain and trees.

The dashboard cannot fetch any of this itself. Every source here is GeoParquet
or a Cloud-Optimized GeoTIFF on anonymous object storage, read over HTTP range
requests -- no REST API, no tile endpoint, nothing a browser can call. The
broker runs the queries and hands back one compact drawing.

**It must be the same data the CFD actually meshes.** A preview built from
something merely similar -- OSM footprints, a basemap tile, a 10 m canopy
raster -- would be worse than no preview: it would look like a check while
quietly disagreeing with the geometry, and it would disagree most exactly where
checking matters. So each of the three mirrors what ``real_cities`` does:

* **buildings** -- GlobalBuildingAtlas LoD1 (TUM), same Source Cooperative
  mirror and same bbox derivation as ``gba.py``. A height is PREDICTED for
  every building, and GBA's own per-building variance comes back with it,
  because "this height is a prediction, and here is how sure the model was" is
  the distinction the campaign kept losing.
* **terrain** -- GEDTM30, the same COG as ``terrain.py``. Answers before the
  case runs whether it will be meshed over real relief or over a flat plane.
* **trees** -- the Meta/WRI 1 m canopy height model, the same S3 objects as
  ``canopy.py``. A crown enters the solve as a porous momentum sink over a
  crown volume, not as an STL solid, so what a preview owes is where the
  canopy is and how tall -- which is exactly the field the crown volume is
  built from.

Overture survives as a buildings fallback, off unless asked for: see
:func:`fetch`.

Cached in the database after the first look. The three queries cost some
seconds between them and cannot change for pinned sources, so paying that once
per case keeps a dashboard refresh from re-querying S3 on every inspector open.

All of this runs inside a web process under a hard memory cap, which is what
the ceilings in :func:`_duck` and :data:`_GDAL_ENV` are for.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import subprocess
import sys
import tempfile
from typing import Any

# Same pin as benchmark/real_cities/overture_3d.py. If that moves, this moves
# with it, or the picture stops matching the mesh.
RELEASE = "2026-08-19.0"
HALF_M = 520.0          # what the runner requests for BUILDINGS: 504 m core + margin
# The mesh domain: 504 m core plus an 800 m buffer, a side of 1304 m from the
# centre. Terrain and canopy are previewed over the whole of it, not over the
# building box, because the buffer is most of what gets meshed and a raster
# cropped to the buildings would show a third of the ground the solve sees.
DOMAIN_HALF_M = 1304.0


# -- Ceilings ---------------------------------------------------------------
# DuckDB and GDAL both size their caches from the host's memory. Inside a
# container that is the WRONG number: /proc/meminfo reports the machine, not
# the cgroup limit the platform enforces, so DuckDB here reported a
# memory_limit of 51.1 GiB and 12 threads while running in a web instance
# capped far below that. It therefore never spilled -- by its own accounting it
# had room to spare -- and the platform OOM-killed the process instead. Both
# ceilings are set explicitly rather than left to a default that cannot see the
# limit it is being measured against.
#
# The numbers are sized for the smallest instance this is deployed on, not for
# speed. Measured: the process settles around 280 MB once DuckDB, its httpfs and
# spatial extensions, GDAL and PROJ are all resident, and that floor is library
# code rather than anything a query holds -- lowering these ceilings barely moves
# it. What they buy is the tail: a pathological tile spills to disk and the
# service stays up, instead of allocating past the container limit and being
# killed. Raise them on a bigger instance if a dense site is slow.
DUCKDB_MEMORY = os.environ.get("CASEBROKER_DUCKDB_MEMORY", "128MB")
DUCKDB_THREADS = int(os.environ.get("CASEBROKER_DUCKDB_THREADS", "2"))
GDAL_CACHE_MB = int(os.environ.get("CASEBROKER_GDAL_CACHE_MB", "48"))

# Spawning a second interpreter to shell out to the Overture client is the
# single largest thing this process can do to itself: it loads pyarrow and
# geopandas, writes a whole GeoJSON and reads it back with json.load, and none
# of that is bounded by the ceilings above because none of it is in this
# process. Off by default for that reason; the fallback is still one env var
# away when the GBA mirror is genuinely down.
OVERTURE_FALLBACK = os.environ.get(
    "CASEBROKER_OVERTURE_FALLBACK", "0").strip().lower() not in ("", "0", "false", "no")

# VSI_CACHE keeps re-read blocks in memory instead of re-fetching them, capped
# at 16 MB; DISABLE_READDIR stops GDAL probing for sidecar files that do not
# exist on these buckets, which is several wasted round trips per open.
_GDAL_ENV = dict(GDAL_CACHEMAX=GDAL_CACHE_MB, VSI_CACHE=True,
                 VSI_CACHE_SIZE=16_777_216,
                 GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                 GDAL_HTTP_MAX_RETRY=2, GDAL_HTTP_RETRY_DELAY=1)


@contextlib.contextmanager
def _duck():
    """A bounded DuckDB connection with httpfs + spatial, always closed.

    Closing matters as much as the ceiling. A connection holds its buffer pool
    until it is closed, and this was a bare ``duckdb.connect()`` inside a
    request handler -- so every footprint fetch leaked one whole DuckDB
    instance, and the leak was what turned a heavy endpoint into a restart.
    """
    import duckdb

    con = duckdb.connect(config={"memory_limit": DUCKDB_MEMORY,
                                 "threads": DUCKDB_THREADS,
                                 "preserve_insertion_order": False})
    try:
        con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
        yield con
    finally:
        con.close()


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


# -- Buildings: GlobalBuildingAtlas LoD1 -------------------------------------
# TUM's LoD1 atlas via the Source Cooperative mirror. The geometry builder
# switched to this as its default height source and this endpoint follows: an
# inspector that draws a DIFFERENT set of buildings than the mesh contains is
# worse than no inspector, because it looks like a check.
#
# Not TUM's own WFS (tubvsig-so2sat-vm1.srv.mwn.de, layer global3D:lod1_global,
# which the MetaMAP Grasshopper plugin queries): that proxy now answers
# GetFeature with PARAMETER_NOT_ALLOWED, serving only GetCapabilities and
# DescribeFeatureType, and wants a browser User-Agent and Referer besides. The
# mirror is anonymous GeoParquet with a bbox column, so a bounding-box
# predicate prunes row groups and one site costs a few MB of range reads.
GBA_BASE = "https://data.source.coop/tge-labs/globalbuildingatlas-lod1"
# Reported RMSE range by continent (ESSD 2025), carried into the payload so the
# dashboard can say what a predicted height is worth instead of implying it was
# surveyed.
GBA_RMSE_M = (1.5, 8.9)
# A 520 m box holding more than this is not a dense site, it is a bad bbox or a
# bad tile. Refusing to materialise it keeps one pathological case from being
# the thing that takes the web process down.
MAX_BUILDINGS = 20_000

# Bumped whenever the SHAPE of a cached preview changes. The footprints cache is
# keyed on case_id alone and has no schema column, so without this a payload
# written by an older build is served forever: adding terrain and canopy made
# every case anyone had already opened keep answering with neither, which looks
# exactly like a site with no trees rather than a stale cache. A hit stamped
# with anything other than the current value is treated as a miss.
PAYLOAD_VERSION = 2


class TileNotPublished(Exception):
    """The source publishes no tile covering this point.

    Distinct from every other failure on purpose, because it is not a failure.
    GBA publishes 922 tiles of a possible 2,592 -- the rest are ocean and ice --
    so a 404 on the tile URL is a statement about the SITE: there are no
    buildings here, and there is nothing to retry. An unreachable mirror is the
    opposite claim and must keep raising, or a transport blip becomes a cached
    empty city.
    """


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
    dlat = half_m / 110_540.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat)))
    xmin, ymin, xmax, ymax = lon - dlon, lat - dlat, lon + dlon, lat + dlat
    url = f"{GBA_BASE}/{gba_tile_for(lat, lon)}.parquet"

    with _duck() as con:
        con.execute(f"SET http_timeout={int(timeout) * 1000};")
        try:
            rows = con.execute(
                f"""
                SELECT ST_AsGeoJSON(geometry) AS gj, height, var
                FROM read_parquet('{url}')
                WHERE bbox.xmin < {xmax} AND bbox.xmax > {xmin}
                  AND bbox.ymin < {ymax} AND bbox.ymax > {ymin}
                LIMIT {MAX_BUILDINGS}
                """
            ).fetchall()
        except Exception as e:                       # noqa: BLE001
            if "404" in str(e):
                raise TileNotPublished(gba_tile_for(lat, lon)) from e
            raise

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
            "height_rmse_m": list(GBA_RMSE_M),
            "centre": [lat, lon], "half_m": half_m,
            "truncated": len(rows) >= MAX_BUILDINGS,
            "n": len(feats), "features": feats}


def fetch(lat: float, lon: float, timeout: int = 120) -> dict[str, Any]:
    """Overture footprints as a compact GeoJSON FeatureCollection.

    Superseded by :func:`fetch_gba`, and disabled unless
    ``CASEBROKER_OVERTURE_FALLBACK`` is set -- see :data:`OVERTURE_FALLBACK` for
    why a subprocess is the expensive option here. Kept because a case built
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
            "height_kind": "tagged",
            "centre": [lat, lon], "half_m": HALF_M,
            "n": len(feats), "features": feats}


# -- Rasters: one windowed read, bounded ------------------------------------
def _read_window(url: str, lat: float, lon: float, half_m: float, n: int,
                 resampling):
    """An ``n x n`` float32 grid over +-half_m of (lat, lon), north-up.

    Returns ``(array, covered)``, where ``covered`` is the fraction of the
    requested box the raster actually spans and cells outside it are NaN.

    The window is clipped to the dataset and the result pasted back at its own
    offset rather than simply read from whatever overlap exists. A site near a
    tile seam then comes back correctly REGISTERED with a hole in it, instead of
    a full grid slid sideways -- the grid is drawn over the domain square, and
    half a tile of canopy shifted 300 m east is a quieter kind of wrong than a
    visible gap.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds, intersection

    dlat = half_m / 110_540.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat)))
    box = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

    with rasterio.Env(**_GDAL_ENV), rasterio.open(url) as ds:
        epsg = ds.crs.to_epsg() if ds.crs else None
        b = box if epsg == 4326 else transform_bounds("EPSG:4326", ds.crs, *box)
        full = from_bounds(*b, ds.transform)
        clip = intersection(full, Window(0, 0, ds.width, ds.height))
        if clip.width < 1 or clip.height < 1:
            raise RuntimeError("requested window falls outside the raster")
        nx = max(1, round(clip.width / full.width * n))
        ny = max(1, round(clip.height / full.height * n))
        ox = min(n - nx, max(0, round((clip.col_off - full.col_off) / full.width * n)))
        oy = min(n - ny, max(0, round((clip.row_off - full.row_off) / full.height * n)))
        a = ds.read(1, window=clip, out_shape=(ny, nx), resampling=resampling,
                    masked=True)

    out = np.full((n, n), np.nan, np.float32)
    out[oy:oy + ny, ox:ox + nx] = a.astype(np.float32).filled(np.nan)
    return out, (nx * ny) / float(n * n)


# Both rasters are read OVERSAMPLE times finer than they are drawn, and the
# statistics are taken at the fine resolution before the drawing grid is
# averaged down. This costs nothing: the expensive part of a windowed read is
# the blocks GDAL fetches over HTTP, which is fixed by the window, not by the
# shape asked back. Getting it wrong was not free -- a canopy grid computed at
# the drawing resolution averages a 65 m cell of trees, roofs and road into one
# number, and reported Atlanta, a city of 25 m oaks, as having a tallest tree of
# 8 m.
OVERSAMPLE = 4


def _blockmean(a, n):
    """Average an ``(n*k, n*k)`` grid down to ``n x n``, ignoring NaN.

    A block that is entirely NaN -- the part of a site past a tile seam -- stays
    NaN, which is the right answer and not a problem, so numpy's warning about
    it is silenced rather than left to repeat once per uncovered cell in the
    service log.
    """
    import warnings

    import numpy as np

    k = a.shape[0] // n
    b = a.reshape(n, k, n, k)
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(np.nanmean(b, axis=3), axis=1)


# -- Terrain: GEDTM30 --------------------------------------------------------
# Same COG the geometry builder reads (benchmark/real_cities/terrain.py). Global
# 30 m bare-earth DTM, CC BY 4.0, Cloud-Optimized, so a windowed read is about a
# second and downloads none of the global file.
GEDTM30 = ("/vsicurl/https://s3.opengeohub.org/global/dtm/v1.2/"
           "gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif")


def terrain(lat: float, lon: float, half_m: float = DOMAIN_HALF_M,
            n: int = 48) -> dict:
    """Whether this site has real bare-earth terrain, how much relief, and where.

    Answers before the case runs what the runner would otherwise only discover
    while building geometry: GEDTM30 has gaps, and where it does the builder
    falls back to a flat plane. A flat tile is a legitimate case -- it is what
    dense cores get anyway -- but it is a different case, and finding out after
    66 core-hours is worse than finding out now.

    ``grid`` is ``n*n`` metres ABOVE ``min_m``, row-major from the north-west
    corner of the mesh domain, so the dashboard can shade the ground the solve
    actually sits on rather than print one relief number over a blank square.
    Coarse on purpose: 48x48 over 2608 m is a 54 m cell, which is enough to say
    "there is a ridge here" without pulling a full-resolution window for a
    picture 300 px wide.

    Never raises. A dead DTM host is a fact about today, not about the site, and
    a preview panel must not fail because a third party is down.
    """
    try:
        import numpy as np
        from rasterio.enums import Resampling
    except Exception:                                    # noqa: BLE001
        return {"source": "unknown", "detail": "rasterio not installed on the broker"}

    try:
        fine, covered = _read_window(GEDTM30, lat, lon, half_m, n * OVERSAMPLE,
                                     Resampling.bilinear)
    except Exception as e:                               # noqa: BLE001
        return {"source": "unavailable", "detail": str(e)[:160]}

    # GEDTM30 marks nodata with a sentinel in some builds and a mask in others;
    # both end up here, and anything past 1e30 is the sentinel leaking through.
    fine = np.where(np.abs(fine) < 1e30, fine, np.nan)
    if not np.isfinite(fine).any():
        # Genuine nodata, not an outage: the builder will use a flat plane here.
        return {"source": "flat", "half_m": half_m,
                "detail": "GEDTM30 has no data at this site"}
    lo, hi = float(np.nanmin(fine)), float(np.nanmax(fine))
    a = _blockmean(fine, n)
    rel = np.where(np.isfinite(a), a - lo, 0.0)
    return {"source": "gedtm30", "relief_m": round(hi - lo, 1),
            "min_m": round(lo, 1), "max_m": round(hi, 1),
            "half_m": half_m, "n": n, "covered": round(covered, 3),
            "grid": [int(round(v)) for v in rel.ravel().tolist()]}


# -- Trees: Meta/WRI 1 m canopy height model ---------------------------------
# Tolan et al. (2024), Remote Sensing of Environment 300: global canopy height
# from self-supervised ViT over RGB, trained on aerial lidar. uint8 metres,
# EPSG:3857 at 1.19 m, CC BY 4.0, anonymous on AWS Open Data. The same objects
# real_cities/canopy.py reads to build the crown volumes.
#
# Why this product and not ETH's 10 m Sentinel-2 map: at 10 m a street tree is
# one blob, and street trees are most of what shelters a pedestrian.
CHM_BASE = ("https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/"
            "alsgedi_global_v6_float/chm")
# Below this the map is mostly shrub and grass noise rather than crown, and it
# is the threshold canopy.py reports coverage above.
CANOPY_MIN_M = 2.0


def chm_tile_for(lat: float, lon: float, z: int = 9) -> str:
    """The zoom-9 Bing quadkey naming the CHM tile covering this point.

    The tiles are 65,536^2 cells at 1.19 m in EPSG:3857, which is exactly a
    zoom-9 web-mercator tiling: 40,075,017 m / 512 / 65,536 = 1.194 m. So the
    key is computable, and ``canopy.py``'s route -- download a 15 MB
    ``tiles.geojson`` index and intersect it -- is the same answer for 15 MB
    more resident memory, which is the wrong trade in a capped web process
    looking at one site.

    Verified against the published objects on four continents.
    """
    n = 2 ** z
    s = math.sin(math.radians(max(-85.05, min(85.05, lat))))
    x = min(n - 1, max(0, int((lon + 180.0) / 360.0 * n)))
    y = min(n - 1, max(0, int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)))
    out = []
    for i in range(z, 0, -1):
        d, mask = 0, 1 << (i - 1)
        if x & mask:
            d += 1
        if y & mask:
            d += 2
        out.append(str(d))
    return "".join(out)


def canopy(lat: float, lon: float, half_m: float = DOMAIN_HALF_M,
           n: int = 64) -> dict:
    """Where the trees are on this site, and how tall, over the mesh domain.

    ``grid`` is ``n*n`` canopy heights in whole metres, row-major from the
    north-west corner, 0 where there is none. The runner turns this field into
    crown volumes (upper 59% of the tree, 4 m columns) and meshes them as a
    ``canopy`` cellZone carrying ``f = 2*Cd*LAD``; it is a POROUS obstacle, not
    an STL solid, which is why a preview shows a field and not blocks.

    A treeless site is a real answer and a common one. So is a site the product
    does not cover -- it has polar and open-ocean gaps -- and the two are
    reported differently, because "no trees here" and "no data here" lead to
    different decisions about whether the case is worth running.

    Never raises, for the same reason :func:`terrain` does not.
    """
    try:
        import numpy as np
        from rasterio.enums import Resampling
    except Exception:                                    # noqa: BLE001
        return {"source": "unknown", "detail": "rasterio not installed on the broker"}

    tile = chm_tile_for(lat, lon)
    url = f"/vsicurl/{CHM_BASE}/{tile}.tif"
    try:
        # average, not bilinear: a 41 m preview cell spans ~34 native cells and
        # the mean canopy height over the cell is what a crown-volume drag term
        # integrates. Picking the nearest pixel would make the field flicker
        # between 0 and a whole tree.
        fine, covered = _read_window(url, lat, lon, half_m, n * OVERSAMPLE,
                                     Resampling.average)
    except Exception as e:                               # noqa: BLE001
        msg = str(e)
        gap = "404" in msg or "does not exist" in msg or "Not Found" in msg
        return {"source": "none" if gap else "unavailable", "tile": tile,
                "half_m": half_m,
                "detail": ("the canopy model publishes no tile here"
                           if gap else msg[:160])}

    fine = np.clip(np.nan_to_num(fine, nan=0.0), 0.0, None)
    a = _blockmean(fine, n)
    under = fine >= CANOPY_MIN_M
    return {"source": "meta-wri-chm-v1", "licence": "CC BY 4.0",
            "native_res_m": 1.2, "tile": tile, "min_canopy_m": CANOPY_MIN_M,
            "half_m": half_m, "n": n, "covered": round(covered, 3),
            "frac_canopy": round(float(under.mean()), 4),
            "max_height_m": round(float(fine.max()), 1),
            "mean_height_where_canopy_m": (round(float(fine[under].mean()), 1)
                                           if under.any() else 0.0),
            "grid": [int(round(v)) for v in a.ravel().tolist()]}


# -- Is this case even on land? ----------------------------------------------
def on_land(lat: float, lon: float) -> bool:
    """Whether the building atlas publishes a tile covering this point.

    A coarse land mask, and deliberately the SAME source the buildings come
    from: GBA publishes 922 of a possible 2,592 tiles, and the 1,670 it omits
    are ocean and ice. So "no tile here" and "no buildings here" are one
    statement rather than two that have to be kept in agreement.

    It is a 5 degree grid, so it rejects the middle of the Atlantic and accepts
    a point 2 km off a fjord. That is the right trade for an ADMISSION check,
    which has to be cheap enough to run on 5,000 cases in one request and must
    never reject a real coastal city: the expensive, exact question is the one
    the site preview already answers per case, from three independent sources.

    See :mod:`casebroker._gba_tiles` for the list and how to regenerate it.
    """
    from casebroker._gba_tiles import PUBLISHED

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    return gba_tile_for(lat, lon) in PUBLISHED
