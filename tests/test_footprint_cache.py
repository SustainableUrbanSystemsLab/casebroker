"""The dashboard's site-geometry cache is bounded.

Measured on production: 15 cached sites took 1.36 MB (~90 KB of GeoJSON each),
so browsing the whole 5,000-case campaign would have put ~450 MB into a database
whose free-plan quota is 500 MB. The cache now keeps the most recently fetched
sites up to a cap and drops the oldest; an evicted site is simply re-fetched.
"""

from __future__ import annotations

from casebroker import db


def conn(tmp_path):
    return db.connect(str(tmp_path / "t.sqlite"))      # the schema creates itself


def cached(c):
    return sorted(r["case_id"] for r in c.execute("SELECT case_id FROM footprints").fetchall())


def test_the_oldest_fetches_are_dropped_past_the_cap(tmp_path):
    c = conn(tmp_path)
    for i in range(5):
        db.put_footprints(c, f"v2-{i}", "{}", 1, now=1000 + i, cap=3)
    assert cached(c) == ["v2-2", "v2-3", "v2-4"]


def test_a_refetch_refreshes_its_place_rather_than_duplicating(tmp_path):
    c = conn(tmp_path)
    for i in range(3):
        db.put_footprints(c, f"v2-{i}", "{}", 1, now=1000 + i, cap=3)
    db.put_footprints(c, "v2-0", '{"again": 1}', 2, now=2000, cap=3)   # re-fetched: now newest
    db.put_footprints(c, "v2-9", "{}", 1, now=2001, cap=3)
    assert cached(c) == ["v2-0", "v2-2", "v2-9"]
    assert db.get_footprints(c, "v2-0")["n"] == 2


def test_the_row_just_written_is_never_the_one_dropped(tmp_path):
    # Even when its fetch time is the oldest (a clock that went backwards).
    c = conn(tmp_path)
    for i in range(3):
        db.put_footprints(c, f"v2-{i}", "{}", 1, now=5000 + i, cap=3)
    db.put_footprints(c, "v2-late", "{}", 1, now=1, cap=3)
    assert "v2-late" in cached(c) and len(cached(c)) == 3


def test_zero_turns_the_bound_off(tmp_path):
    c = conn(tmp_path)
    for i in range(6):
        db.put_footprints(c, f"v2-{i}", "{}", 1, now=1000 + i, cap=0)
    assert len(cached(c)) == 6
