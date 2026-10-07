"""Questions asked of the pedestrian wind field: |U| per direction, where it is stored.

The broker holds every finished direction's field as a umag/1 container in
``case_fields.blob`` (``db.decode_umag``): a JSON header, then float32 |U| on a
regular lattice, row-major with x fastest and y ascending, NaN where there is no
fluid (inside a building, or no mesh). This module is the arithmetic over that
layout, kept apart from the SQL that fetches the bytes (``db.field_point``,
``db.field_rows``) so it can be tested on its own:

* :func:`stats` -- what a field is summarised by. Computed once, when the field
  is stored, into ``case_fields`` columns a query filters and sorts on.
* :func:`to_local` / :func:`to_geographic` -- the campaign's site frame: metres
  east (+x) and north (+y) of the site centre, by the SAME equirectangular
  constants the node builds the site with (Eddy3D ``MetaFOAM.Site.SiteFrame``)
  and :mod:`casebroker.footprints` previews it with. A better projection here
  would put a queried point a few decimetres away from where the node put the
  buildings.
* :func:`corners` and :func:`bilinear` -- |U| at a point between lattice nodes.
* :func:`region` and :func:`exceedance` -- a part of the field, and how much of
  it is windier than a threshold.

U/U_ref ("VR", the velocity ratio) is |U| over the field's ``u_ref``, the inlet
log law at the field's height: a field solved for a reference wind becomes the
same field for any other by scaling, so a ratio is what compares sites.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

#: The site frame's constants (Eddy3D MetaFOAM.Site.SiteFrame; footprints.py).
METRES_PER_DEG_LAT = 110_540.0
METRES_PER_DEG_LON_AT_EQUATOR = 111_320.0

#: Quantiles a field is summarised by, as (name, percent). Linear interpolation,
#: the method /v1/dataset uses.
QUANTILES = (("p05", 5.0), ("p25", 25.0), ("p50", 50.0), ("p75", 75.0), ("p95", 95.0), ("p99", 99.0))
#: Every statistic a stored field carries, in order: the column is ``umag_<name>``.
STATS = ("mean", "min") + tuple(q for q, _ in QUANTILES) + ("max",)


class OutsideGrid(ValueError):
    """A point the field's lattice does not cover."""


@dataclass(frozen=True)
class Lattice:
    x0: float
    y0: float
    spacing_m: float
    nx: int
    ny: int

    @classmethod
    def of(cls, row: dict[str, Any]) -> "Lattice":
        return cls(float(row["x0"]), float(row["y0"]), float(row["spacing_m"]), int(row["nx"]), int(row["ny"]))

    def x(self, i: float) -> float:
        return self.x0 + i * self.spacing_m

    def y(self, j: float) -> float:
        return self.y0 + j * self.spacing_m


# -- the container -----------------------------------------------------------------

def data_offset(container: bytes) -> int:
    """Where the float32 values start in an UNCOMPRESSED umag/1 container: after the
    magic, the version, the header length and the header."""
    (hlen,) = struct.unpack("<I", container[8:12])
    return 12 + hlen


def values(container: bytes) -> tuple[dict[str, Any], np.ndarray]:
    """The header and the field, ``(ny, nx)`` float32, of an uncompressed container
    whose shape ``db.decode_umag`` has already checked."""
    off = data_offset(container)
    header = json.loads(container[12:off].decode("utf-8"))
    nx, ny = int(header["nx"]), int(header["ny"])
    return header, np.frombuffer(container, dtype="<f4", count=nx * ny, offset=off).reshape(ny, nx)


# -- statistics ----------------------------------------------------------------------

def stats(field: np.ndarray) -> dict[str, Any]:
    """``n_valid`` and ``umag_<stat>`` for every :data:`STATS` over the finite cells of
    a field (or any part of one); every statistic None when no cell has a value."""
    v = np.asarray(field, dtype=np.float64).ravel()
    v = v[np.isfinite(v)]
    out: dict[str, Any] = {"n_valid": int(v.size)}
    if not v.size:
        out.update({f"umag_{k}": None for k in STATS})
        return out
    qs = np.percentile(v, [p for _, p in QUANTILES])
    out["umag_mean"] = float(v.mean())
    out["umag_min"] = float(v.min())
    out.update({f"umag_{name}": float(q) for (name, _), q in zip(QUANTILES, qs)})
    out["umag_max"] = float(v.max())
    return out


def ratios(record: dict[str, Any]) -> dict[str, float | None]:
    """``vr_<stat>`` = ``umag_<stat>`` / ``u_ref`` for every statistic the record has;
    None where either is missing or ``u_ref`` is not positive."""
    u_ref = record.get("u_ref")
    ok = isinstance(u_ref, (int, float)) and math.isfinite(u_ref) and u_ref > 0
    out = {}
    for k in STATS + ("p999",):
        v = record.get(f"umag_{k}")
        out[f"vr_{k}"] = (v / u_ref) if ok and isinstance(v, (int, float)) else None
    return out


def exceedance(field: np.ndarray, thresholds: list[float]) -> dict[str, float | None]:
    """For each threshold, the share of the field's finite cells STRICTLY above it,
    keyed by :func:`threshold_key`; None when no cell has a value."""
    v = np.asarray(field, dtype=np.float64).ravel()
    v = v[np.isfinite(v)]
    return {threshold_key(t): (float((v > t).sum()) / v.size if v.size else None) for t in thresholds}


def threshold_key(t: float) -> str:
    """A threshold as a JSON key: ``5`` -> ``"5"``, ``1.2`` -> ``"1.2"``."""
    return "%g" % t


# -- the site frame -------------------------------------------------------------------

def metres_per_deg_lon(lat: float) -> float:
    return METRES_PER_DEG_LON_AT_EQUATOR * math.cos(math.radians(lat))


def to_local(lat: float, lon: float, site_lat: float, site_lon: float) -> tuple[float, float]:
    """(x, y): metres east and north of the site centre."""
    return (lon - site_lon) * metres_per_deg_lon(site_lat), (lat - site_lat) * METRES_PER_DEG_LAT


def to_geographic(x: float, y: float, site_lat: float, site_lon: float) -> tuple[float, float]:
    """(lat, lon) of a point x metres east and y metres north of the site centre."""
    return site_lat + y / METRES_PER_DEG_LAT, site_lon + x / metres_per_deg_lon(site_lat)


# -- a point ---------------------------------------------------------------------------

def corners(lat: Lattice, x: float, y: float) -> tuple[int, int, int, int, float, float]:
    """The lattice cell a point falls in: ``(i0, i1, j0, j1, fx, fy)`` -- the columns and
    rows of the (up to) four nodes around it and its fractional position between them.

    A point up to half a spacing outside the outermost nodes is taken as ON them (the
    node stands for the cell around it); anything further is :class:`OutsideGrid`.
    """
    fi = (x - lat.x0) / lat.spacing_m
    fj = (y - lat.y0) / lat.spacing_m
    if not (-0.5 <= fi <= lat.nx - 0.5) or not (-0.5 <= fj <= lat.ny - 0.5):
        raise OutsideGrid(
            "(%.1f, %.1f) m is outside the field, which covers x %.1f..%.1f and y %.1f..%.1f m"
            % (x, y, lat.x0, lat.x(lat.nx - 1), lat.y0, lat.y(lat.ny - 1)))
    fi = min(max(fi, 0.0), lat.nx - 1.0)
    fj = min(max(fj, 0.0), lat.ny - 1.0)
    i0, j0 = min(int(math.floor(fi)), max(lat.nx - 2, 0)), min(int(math.floor(fj)), max(lat.ny - 2, 0))
    i1, j1 = min(i0 + 1, lat.nx - 1), min(j0 + 1, lat.ny - 1)
    return i0, i1, j0, j1, fi - i0, fj - j0


def bilinear(v00: float, v10: float, v01: float, v11: float, fx: float, fy: float) -> tuple[float | None, int]:
    """|U| between four nodes (``vIJ`` at column offset I, row offset J), and how many
    of them had a value. A node without one (a building) is left out and the weights of
    the others renormalised, so a point beside a wall reads the air beside it rather
    than nothing; with no node valued, None."""
    num = den = 0.0
    n = 0
    for v, w in ((v00, (1 - fx) * (1 - fy)), (v10, fx * (1 - fy)), (v01, (1 - fx) * fy), (v11, fx * fy)):
        if v is not None and math.isfinite(v):
            n += 1
            if w > 0:
                num += w * v
                den += w
    if n == 0:
        return None, 0
    if den == 0:
        # Every valued node has zero weight: the point sits exactly on a node without a
        # value, or on the far edge of a valueless one. The nearest valued node answers.
        return next(v for v in (v00, v10, v01, v11) if v is not None and math.isfinite(v)), n
    return num / den, n


# -- a region ---------------------------------------------------------------------------

def region(lat: Lattice, *, bbox: tuple[float, float, float, float] | None = None,
           centre: tuple[float, float] | None = None, radius_m: float | None = None) -> tuple[int, int, np.ndarray]:
    """The rows a region spans and which of their nodes it holds: ``(j0, j1, mask)`` with
    ``mask`` of shape ``(j1 - j0 + 1, nx)``. ``bbox`` is (xmin, ymin, xmax, ymax) in site
    metres; ``centre`` and ``radius_m`` a disc; both given, their intersection; neither,
    the whole field. A region that holds no node is :class:`OutsideGrid`."""
    xs = lat.x0 + np.arange(lat.nx) * lat.spacing_m
    ys = lat.y0 + np.arange(lat.ny) * lat.spacing_m
    rows = np.ones(lat.ny, dtype=bool)
    cols = np.ones(lat.nx, dtype=bool)
    if bbox is not None:
        xmin, ymin, xmax, ymax = bbox
        if xmin > xmax or ymin > ymax:
            raise OutsideGrid("bbox is xmin,ymin,xmax,ymax with xmin <= xmax and ymin <= ymax")
        cols &= (xs >= xmin) & (xs <= xmax)
        rows &= (ys >= ymin) & (ys <= ymax)
    if centre is not None:
        if radius_m is None or not radius_m > 0:
            raise OutsideGrid("a disc needs a positive radius_m")
        cx, cy = centre
        rows &= np.abs(ys - cy) <= radius_m
        cols &= np.abs(xs - cx) <= radius_m
    if not rows.any() or not cols.any():
        raise OutsideGrid("the region holds no node of the field")
    j = np.flatnonzero(rows)
    j0, j1 = int(j[0]), int(j[-1])
    mask = np.zeros((j1 - j0 + 1, lat.nx), dtype=bool)
    mask[:, cols] = True
    mask &= rows[j0:j1 + 1, None]
    if centre is not None:
        cx, cy = centre
        dx = xs[None, :] - cx
        dy = ys[j0:j1 + 1, None] - cy
        mask &= dx * dx + dy * dy <= radius_m * radius_m
    if not mask.any():
        raise OutsideGrid("the region holds no node of the field")
    return j0, j1, mask
