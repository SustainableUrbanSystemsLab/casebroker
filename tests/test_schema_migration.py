"""What happens when the code's schema is ahead of the database's.

Every statement in SCHEMA is `IF NOT EXISTS`, which upgrades cleanly whenever a
release ADDS A TABLE -- and that is the only kind of schema change this repo had
ever made, so the gap went unnoticed. Adding a COLUMN was a different story:
`CREATE TABLE IF NOT EXISTS` no-ops on a table that already exists without
comparing columns, so the column never appeared and the first index over it
failed at connect() with `no such column: priority` -- which reads like a corrupt
database rather than one release behind.

These tests pin the reconciler that closes that, and the two properties it
depends on: that the hand-written DDL parser agrees with what the engine
actually creates, and that the two schema constants have not drifted apart.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db  # noqa: E402


def _old_cases_table(path, *, without):
    """A `cases` table one release behind: every column except `without`."""
    columns = db.parse_schema_columns(db.SCHEMA)["cases"]
    kept = [ddl for name, ddl in columns.items() if name not in without]
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cases (%s)" % ", ".join(kept))
    seed = {"case_id": "'c1'", "spec": "'{}'", "recipe": "'r'",
            "city_cluster": "'atl'", "split": "'train'", "state": "'done'",
            "created_at": "1", "updated_at": "1"}
    present = {k: v for k, v in seed.items() if k not in without}
    conn.execute("INSERT INTO cases(%s) VALUES (%s)"
                 % (", ".join(present), ", ".join(present.values())))
    conn.commit()
    conn.close()


# -- the parser the reconciler trusts ---------------------------------------

def test_the_ddl_parser_agrees_with_what_sqlite_actually_creates(tmp_path):
    """The reconciler decides what to ALTER from a hand-written parse of the
    schema text. If that parse disagreed with the engine it would be
    confidently wrong, so it is checked against the engine itself."""
    conn = sqlite3.connect(tmp_path / "probe.sqlite")
    conn.executescript(db.SCHEMA)
    parsed = db.parse_schema_columns(db.SCHEMA)
    for table, columns in parsed.items():
        actual = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        assert set(columns) == actual, table
    conn.close()


def test_the_two_schema_constants_have_not_drifted(tmp_path):
    """SCHEMA and PG_SCHEMA are two hand-maintained copies of one design. A
    column added to one and forgotten in the other is a bug that only shows up
    on whichever engine production happens to run."""
    sqlite_tables = db.parse_schema_columns(db.SCHEMA)
    pg_tables = db.parse_schema_columns(db.PG_SCHEMA)
    assert set(sqlite_tables) == set(pg_tables)
    for table in sqlite_tables:
        assert set(sqlite_tables[table]) == set(pg_tables[table]), table


def test_only_a_32_bit_column_the_schema_now_declares_BIGINT_is_widened():
    """The decision half of the Postgres widening, which needs no Postgres."""
    schema = ("CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, "
              "result_bytes BIGINT, attempts INTEGER NOT NULL DEFAULT 0);")
    reported = [("cases", "result_bytes", "integer"), ("cases", "attempts", "integer"),
                ("cases", "case_id", "text"), ("somebody_elses", "result_bytes", "integer")]
    assert db.columns_to_widen(schema, reported) == [("cases", "result_bytes")]
    already = [("cases", "result_bytes", "bigint")]
    assert db.columns_to_widen(schema, already) == []


def test_production_result_bytes_is_64_bit_on_postgres():
    """An archive passes 2.1 GB. SQLite's INTEGER holds it; Postgres's does not."""
    assert "BIGINT" in db.parse_schema_columns(db.PG_SCHEMA)["cases"]["result_bytes"].upper()
    reported = [("cases", "result_bytes", "integer")]          # production, before this
    assert db.columns_to_widen(db.PG_SCHEMA, reported) == [("cases", "result_bytes")]
    assert db.widen_columns(None, db.SCHEMA, is_pg=False) == []  # SQLite: nothing to do, conn untouched


# -- a fresh database --------------------------------------------------------

def test_a_fresh_database_gets_every_table_and_index(tmp_path):
    conn = db.connect(str(tmp_path / "fresh.sqlite"))
    tables = {r["name"] for r in
              conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(db.parse_schema_columns(db.SCHEMA)) <= tables
    # The identity tables specifically -- doctor used to check only the five
    # campaign ones and pass on a database with no auth layer at all.
    assert {"users", "sessions", "worker_tokens"} <= tables
    indexes = {r["name"] for r in
               conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_cases_claim" in indexes


def test_a_fresh_database_records_the_schema_version(tmp_path):
    conn = db.connect(str(tmp_path / "fresh.sqlite"))
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_applying_the_schema_twice_changes_nothing(tmp_path):
    path = str(tmp_path / "twice.sqlite")
    first = db.connect(path)
    first.execute(
        "INSERT INTO cases(case_id, spec, recipe, city_cluster, split, state,"
        "                  created_at, updated_at) "
        "VALUES ('c1', '{}', 'r', 'atl', 'train', 'pending', 1, 1)")
    first.close()
    second = db.connect(path)
    assert second.execute("SELECT count(*) AS n FROM cases").fetchone()["n"] == 1
    assert db.schema_version(second) == db.SCHEMA_VERSION


# -- a database one release behind ------------------------------------------

def test_a_missing_nullable_column_is_added_and_rows_survive(tmp_path):
    path = str(tmp_path / "old.sqlite")
    _old_cases_table(path, without={"metrics"})
    conn = db.connect(path)
    row = conn.execute("SELECT case_id, state, metrics FROM cases").fetchone()
    assert row["case_id"] == "c1" and row["state"] == "done"
    assert row["metrics"] is None


def test_a_missing_column_with_a_default_backfills_existing_rows(tmp_path):
    path = str(tmp_path / "old.sqlite")
    _old_cases_table(path, without={"priority"})
    conn = db.connect(path)
    # The existing row must come out with the schema's default, not NULL --
    # the claim query orders by priority and a NULL there would sort the whole
    # pre-upgrade backlog to one end of the queue.
    assert conn.execute("SELECT priority FROM cases").fetchone()["priority"] == 100


def test_an_index_over_a_newly_added_column_is_built(tmp_path):
    """The original failure: idx_cases_claim covers `priority`, so creating the
    index BEFORE reconciling the column is what raised `no such column`."""
    path = str(tmp_path / "old.sqlite")
    _old_cases_table(path, without={"priority"})
    conn = db.connect(path)
    indexes = {r["name"] for r in
               conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_cases_claim" in indexes


def test_a_database_predating_the_identity_tables_gains_them(tmp_path):
    """The real upgrade this repo shipped: a live campaign database gaining
    users/sessions/worker_tokens."""
    path = str(tmp_path / "preauth.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE cases (%s);" % ", ".join(
        db.parse_schema_columns(db.SCHEMA)["cases"].values()))
    conn.commit()
    conn.close()
    upgraded = db.connect(path)
    tables = {r["name"] for r in
              upgraded.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "sessions", "worker_tokens"} <= tables
    assert db.count_users(upgraded) == 0


def test_settings_a_removed_feature_left_behind_are_deleted_once_and_not_audited(tmp_path):
    """0.9.0's ntfy notifier wrote ``notify_cursor`` at every start, and an admin
    may have stored a topic URL and a token. The feature is gone; its rows go
    with it on the next connect -- without an audit event (nobody changed a
    setting) and without touching any other setting."""
    path = str(tmp_path / "v090.sqlite")
    conn = db.connect(path)
    db.set_setting(conn, "target_build", "b1", by="admin")
    for key in db.RETIRED_SETTINGS:
        conn.execute("INSERT INTO settings(key, value, updated_at, updated_by)"
                     " VALUES (?, 'x', 1, 'notifier')", (key,))
    events = conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
    conn.close()

    upgraded = db.connect(path)
    assert db.get_settings(upgraded) == {"target_build": "b1"}
    assert upgraded.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == events
    upgraded.close()

    again = db.connect(path)                                 # idempotent
    assert db.get_settings(again) == {"target_build": "b1"}
    assert db.drop_retired_settings(again) == []


def test_a_column_the_schema_no_longer_has_is_left_alone(tmp_path):
    """Only ever ADD. Dropping a column to match the code would destroy data for
    a version that may be about to be rolled back."""
    path = str(tmp_path / "extra.sqlite")
    columns = db.parse_schema_columns(db.SCHEMA)["cases"]
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cases (%s, legacy_note TEXT)"
                 % ", ".join(columns.values()))
    conn.execute(
        "INSERT INTO cases(case_id, spec, recipe, city_cluster, split, state,"
        "                  created_at, updated_at, legacy_note) "
        "VALUES ('c1', '{}', 'r', 'atl', 'train', 'done', 1, 1, 'keep me')")
    conn.commit()
    conn.close()
    upgraded = db.connect(path)
    assert upgraded.execute(
        "SELECT legacy_note FROM cases").fetchone()["legacy_note"] == "keep me"


def test_a_column_that_cannot_be_added_says_so_by_name(tmp_path):
    """`recipe` is NOT NULL with no default, which no engine can bolt onto an
    existing table. The point is that the operator is told which column and
    why, instead of the pre-reconciler `no such column: recipe`."""
    path = str(tmp_path / "hard.sqlite")
    _old_cases_table(path, without={"recipe"})
    with pytest.raises(RuntimeError) as excinfo:
        db.connect(path)
    message = str(excinfo.value)
    assert "cases.recipe" in message
    assert "NOT NULL without a DEFAULT" in message


# -- the parser's edge cases -------------------------------------------------

def test_comments_and_literals_do_not_confuse_the_parser():
    parsed = db.parse_schema_columns("""
        -- a comment mentioning (parentheses) and a semicolon;
        CREATE TABLE IF NOT EXISTS t (
            a TEXT NOT NULL DEFAULT 'has -- two dashes inside',
            b INTEGER,            -- trailing comment
            PRIMARY KEY (a, b)
        );
    """)
    assert parsed == {"t": {"a": "a TEXT NOT NULL DEFAULT 'has -- two dashes inside'",
                            "b": "b INTEGER"}}


def test_a_table_constraint_is_not_mistaken_for_a_column():
    parsed = db.parse_schema_columns(
        "CREATE TABLE t (a TEXT, b TEXT, UNIQUE(a, b), CHECK (a <> b));")
    assert set(parsed["t"]) == {"a", "b"}


@pytest.mark.parametrize("ddl,expected", [
    # Addable: the column NAME merely contains a constraint keyword.
    ("references_count INTEGER NOT NULL DEFAULT 0", None),
    ("unique_id TEXT", None),
    ("note TEXT", None),
    ("priority INTEGER NOT NULL DEFAULT 100", None),
    # Genuinely unaddable, and named as such.
    ("token_hash TEXT UNIQUE NOT NULL", "UNIQUE"),
    ("id INTEGER PRIMARY KEY AUTOINCREMENT", "PRIMARY KEY"),
    ("recipe TEXT NOT NULL", "NOT NULL without a DEFAULT"),
    ("owner INTEGER REFERENCES users(id)", "REFERENCES"),
])
def test_only_genuinely_unaddable_columns_are_refused(ddl, expected):
    """A substring search over the whole definition would refuse
    `references_count` and `unique_id`, blocking a legitimate upgrade. The
    column's own name is dropped first, and the match is on whole words."""
    assert db._why_unaddable(ddl) == expected
