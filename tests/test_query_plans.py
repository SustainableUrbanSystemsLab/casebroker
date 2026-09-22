"""The queries that run on a timer, and the indexes that keep them cheap.

Every one of these holds `db._LOCK` while it runs, so a slow query here is not
just a slow page -- it stalls every worker's lease and heartbeat behind it. That
is what makes an index on this table a correctness-adjacent concern rather than
a tuning preference.
"""

from __future__ import annotations

import sqlite3

import pytest

from casebroker import db


@pytest.fixture()
def big(tmp_path):
    """Enough rows that a missing index shows up in the plan, few enough to be fast."""
    conn = db.connect(str(tmp_path / "big.sqlite"))
    db.add_cases(conn, [{
        "case_id": f"v2-{i:016x}", "spec": {"lat": 33.0, "lon": -84.0},
        "recipe": "r", "city_cluster": f"c{i % 20}", "lcz": "LCZ6",
        "split": "train", "priority": 100, "max_attempts": 3}
        for i in range(2000)])
    return conn, str(tmp_path / "big.sqlite")


def _plan(path, sql):
    raw = sqlite3.connect(path)
    try:
        return " | ".join(r[-1] for r in raw.execute("EXPLAIN QUERY PLAN " + sql))
    finally:
        raw.close()


def test_the_dashboard_page_does_not_sort_the_whole_table(big):
    """The dashboard polls this every 60 seconds, per open tab.

    Without idx_cases_updated the plan is "SCAN cases" plus a temp B-tree -- a
    full sort of every case in the campaign, holding the global lock throughout.
    Measured at 50,000 cases: 8 ms for the first page, 155 ms for a deep one,
    against ~0.4 ms and ~2 ms with the index.
    """
    _, path = big
    plan = _plan(path, "SELECT * FROM cases ORDER BY updated_at DESC LIMIT 50")
    assert "idx_cases_updated" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


def test_claiming_work_uses_its_index(big):
    """The hottest query in the system: every worker, every poll."""
    _, path = big
    plan = _plan(path, "SELECT case_id FROM cases WHERE state = 'pending'"
                       " ORDER BY priority ASC, case_id ASC LIMIT 4")
    assert "idx_cases_claim" in plan, plan


def test_a_lease_is_found_by_index_not_by_scan(big):
    """heartbeat, complete, fail and release all resolve a lease_id first."""
    _, path = big
    plan = _plan(path, "SELECT * FROM cases WHERE lease_id = 'x'")
    assert "idx_cases_lease" in plan, plan


def test_a_dataset_page_is_found_by_key_not_by_sorting_the_table(big):
    """GET /v1/dataset reads the campaign a page at a time (db.dataset_rows),
    every minute a dashboard is open. Each page must seek to its key in the
    primary key's index: a full scan plus a sort per page, under db._LOCK,
    would make the paging cost more than the one big read it replaced."""
    _, path = big
    plan = _plan(path, "SELECT case_id, state, split, lcz, recipe, spec, telemetry, metrics"
                       " FROM cases WHERE case_id > 'v2-00000000000003e8'"
                       " ORDER BY case_id LIMIT 2000")
    assert "sqlite_autoindex_cases_1" in plan and "case_id>?" in plan.replace(" ", ""), plan
    assert "TEMP B-TREE" not in plan.upper(), plan


def test_every_index_the_schema_declares_actually_exists(big):
    """The schema is applied by a reconciler, not by hand.

    An index added to SCHEMA but never created -- a typo, a block that only the
    other engine's branch reaches -- is invisible: every query still returns the
    right answer, just by scanning.
    """
    conn, path = big
    raw = sqlite3.connect(path)
    have = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    raw.close()
    for name in ("idx_cases_claim", "idx_cases_lease", "idx_cases_split",
                 "idx_cases_updated", "idx_events_case", "idx_events_ts"):
        assert name in have, f"{name} is declared in SCHEMA but not in the database"


def test_the_index_is_added_to_a_database_that_predates_it(tmp_path):
    """Which is what will happen on the deployment: an existing campaign.

    The schema is brought forward on connect, so an index introduced after the
    database was created has to appear without anyone running a migration.
    """
    path = str(tmp_path / "old.sqlite")
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA.replace(
        "CREATE INDEX IF NOT EXISTS idx_cases_updated ON cases(updated_at DESC);", ""))
    raw.commit()
    have = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    raw.close()
    assert "idx_cases_updated" not in have, "fixture did not actually omit the index"

    db.connect(path)                       # brings the schema forward
    raw = sqlite3.connect(path)
    have = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    raw.close()
    assert "idx_cases_updated" in have, "an existing database never gained the index"
