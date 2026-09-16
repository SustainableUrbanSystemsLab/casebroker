"""The canopy tile key, and the ceilings that keep the broker inside its box.

Two unrelated things that share a module and a failure mode: both are silent
when wrong. A wrong quadkey is either a 404 (loud, fine) or a real tile of
canopy for somewhere else, which draws trees a continent away onto this site.
An unset memory ceiling is invisible until the platform kills the process --
which is how it was found.
"""

from __future__ import annotations

import math

from casebroker import footprints
from casebroker.footprints import chm_tile_for


def _quadkey_bounds(qk: str):
    """The lon/lat box a quadkey covers, decoded independently of the encoder."""
    z = len(qk)
    x = y = 0
    for ch in qk:
        d = int(ch)
        x = (x << 1) | (d & 1)
        y = (y << 1) | ((d >> 1) & 1)
    n = 2 ** z

    def lon(xi):
        return xi / n * 360.0 - 180.0

    def lat(yi):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yi / n))))

    return lon(x), lat(y + 1), lon(x + 1), lat(y)      # w, s, e, n


def test_keys_are_zoom_nine():
    """The product's own geometry fixes the zoom, and nothing else works.

    56,145 tiles of 65,536 cells at 1.19 m: 40,075,017 m / 512 / 65,536 =
    1.194 m, which is zoom 9 and only zoom 9. A key one level out is a valid
    quadkey naming a real object four times the size, so this is not a detail
    the loader would catch.
    """
    for lat, lon in ((33.749, -84.388), (32.06, 118.80), (-23.55, -46.63)):
        assert len(chm_tile_for(lat, lon)) == 9


def test_published_tiles_for_four_continents():
    """Keys verified against the objects actually published in the bucket."""
    assert chm_tile_for(33.7490, -84.3880) == "032002311"    # Atlanta
    assert chm_tile_for(32.0603, 118.7969) == "132103222"    # Nanjing
    assert chm_tile_for(52.5200, 13.4050) == "120210233"     # Berlin
    assert chm_tile_for(-23.5500, -46.6300) == "210311121"   # Sao Paulo


def test_a_point_always_lands_inside_its_own_tile():
    """Whatever the key says, it must contain the point that produced it."""
    for lat in (-67.3, -33.9, -0.4, 0.0, 12.5, 51.5, 71.9):
        for lon in (-179.9, -122.4, -0.1, 0.0, 4.9, 118.8, 179.9):
            w, s, e, n = _quadkey_bounds(chm_tile_for(lat, lon))
            assert w <= lon <= e, (lat, lon)
            assert s <= lat <= n, (lat, lon)


def test_web_mercator_latitude_is_clamped():
    """Past +-85.05 the projection has no tile, and the key must not explode."""
    for lat in (-89.9, -85.06, 85.06, 89.9):
        assert len(chm_tile_for(lat, 0.0)) == 9


def test_duckdb_connection_is_bounded_and_closed():
    """The OOM regression, in one test.

    DuckDB sizes memory_limit and threads from /proc/meminfo, which inside a
    container reports the HOST -- so it reported 51 GiB and 12 threads while
    running under a cap a fraction of that, never spilled, and was killed. It
    also held its buffer pool until close, and the caller used to be a bare
    connect() in a request handler that never closed one.
    """
    with footprints._duck() as con:
        limit, threads = con.execute(
            "SELECT current_setting('memory_limit'), current_setting('threads')"
        ).fetchone()
        assert limit != "51.1 GiB"           # i.e. not the host default
        assert int(threads) == footprints.DUCKDB_THREADS
        # The ceiling has to be a real one, in the low hundreds of MB at most.
        value, unit = float(limit.split()[0]), limit.split()[1]
        as_mib = value * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[unit]
        assert as_mib <= 512, limit

    # Closed on the way out, whatever happened inside.
    try:
        con.execute("SELECT 1")
    except Exception:
        pass
    else:
        raise AssertionError("connection outlived the context manager")


def test_duckdb_connection_closes_on_error():
    """A failing query must not leak the pool either -- that was the leak."""
    import pytest

    with pytest.raises(RuntimeError):
        with footprints._duck() as con:
            raise RuntimeError("boom")
    with pytest.raises(Exception):
        con.execute("SELECT 1")


def test_overture_fallback_is_off_by_default(monkeypatch):
    """Shelling out to the Overture client is the one unbounded thing here.

    It spawns a second interpreter that loads pyarrow and materialises a whole
    GeoJSON, none of which the ceilings above can reach. Automatic on every GBA
    hiccup is the wrong default for a memory-capped web process.
    """
    monkeypatch.delenv("CASEBROKER_OVERTURE_FALLBACK", raising=False)
    import importlib

    reloaded = importlib.reload(footprints)
    try:
        assert reloaded.OVERTURE_FALLBACK is False
    finally:
        importlib.reload(footprints)
