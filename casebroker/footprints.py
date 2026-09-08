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
        feats.append({"type": "Feature",
                      "properties": {"h": _height(f.get("properties") or {})},
                      "geometry": geom})
    return {"type": "FeatureCollection", "release": RELEASE,
            "centre": [lat, lon], "half_m": HALF_M,
            "n": len(feats), "features": feats}
