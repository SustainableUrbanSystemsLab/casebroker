"""The Postgres engine, verified against a REAL database over the network.

Skipped entirely unless ``CASEBROKER_TEST_PG_DSN`` is set — these tests hit a real
Postgres instance (Supabase in practice) and are not meant to run in an ordinary
`pytest` invocation with no such database configured. That mirrors this project's
own rule for engine-gated tests elsewhere: a claim about a real external system is
only worth trusting once it has actually been run against that system.

Every test here opens its OWN connection per simulated worker, deliberately unlike
``test_db.py``'s SQLite tests (which share one process-local connection protected by
an in-process lock). Multiple real connections is the only way to exercise what
Postgres actually adds: ``FOR UPDATE SKIP LOCKED`` correctness ACROSS connections,
which a single shared connection can never touch regardless of how many threads
call into it.

All test data is prefixed with a per-run UUID and deleted in a module-scoped
teardown, because this may be the same database the real campaign uses -- it must
be left exactly as it was found, tables included but empty of test rows.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import dataset, db, ids  # noqa: E402

DSN = os.environ.get("CASEBROKER_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set CASEBROKER_TEST_PG_DSN to a real Postgres URL to run these")

# Turns an unusable credential back into a hard failure -- see preflight below.
# Off by default because these tests point at a THIRD-PARTY database whose
# availability is not a statement about the commit under test.
REQUIRED = os.environ.get("CASEBROKER_TEST_PG_REQUIRED")

RUN = uuid.uuid4().hex[:8]

# A table name unique to this run. The column reconciler has to be exercised
# against a table it is willing to ALTER, and the campaign tables are off
# limits here -- this may be the live database.
_RECON_TABLE = "pgtest_recon_%s" % RUN

# The reconciler tests CREATE, ALTER and DROP tables, which is a different
# contract from the per-run-UUID row cleanup the rest of this file relies on to
# be safe against the SHARED production database (the main-only CI job points
# CASEBROKER_TEST_PG_DSN at the real DBSTRING). Table DDL there is not worth the
# coverage: a run interrupted between CREATE and DROP would leave a stray table
# in the campaign's own database. So these run only where a throwaway Postgres
# says so -- the service-container job in .github/workflows/test.yml.
SCRATCH = os.environ.get("CASEBROKER_TEST_PG_SCRATCH")
scratch_only = pytest.mark.skipif(
    not SCRATCH,
    reason="creates and drops tables; set CASEBROKER_TEST_PG_SCRATCH only "
           "against a throwaway Postgres, never the shared campaign database")


def prefix(s: str) -> str:
    return f"pgtest-{RUN}-{s}"


@pytest.fixture(scope="module", autouse=True)
def preflight():
    """One connection before any test, so a refused credential costs ONE refusal.

    Each test here opens its own connection, and psycopg tries every address the
    pooler's hostname resolves to (three, in practice) before giving up. With a
    stale password that was ~48 failed logins per run -- and fresh_conn's
    backoff, written for a transient ECIRCUITBREAKER burst, then turned every
    later refusal into five more, for close to five minutes of hammering.
    Supabase answers that by blocking NEW connections project-wide -- the live
    broker's and every reconnecting worker's -- and deploy-smoke-test triggers a
    Render deploy inside that same window. So a bad credential is established
    once, up front, and the run stops there with the reason.

    A credential that cannot connect SKIPS this module rather than failing it,
    which is the same answer `pytestmark` above already gives when no DSN is
    configured at all. Splitting those two apart -- green when the secret is
    absent, red when it is stale -- draws the line at "is a secret set" when the
    only question worth asking is "did this coverage run". Both cases ran
    nothing; neither is a statement about the commit, and a red X that means
    "somebody must rotate a secret" trains people to ignore red Xs on a branch
    that gates deploys.

    What it must never be is SILENT, which is the real failure mode and the one
    `skipif` had: coverage that quietly stops running is coverage you no longer
    have. So the reason is published two ways that survive: pytest's short
    summary (the workflow passes -rs, without which -v prints a bare "SKIPPED"
    and nothing else), and GitHub's step summary, which renders on the run's own
    page. Note it is written to $GITHUB_STEP_SUMMARY rather than printed: pytest
    captures stdout inside a fixture and discards it for a skip, so a
    ``print("::warning::...")`` here reaches nobody -- measured, not assumed.
    Set CASEBROKER_TEST_PG_REQUIRED=1 to make an unusable credential fail the
    job instead.

    A connection that SUCCEEDS changes nothing: the tests run exactly as before,
    and a real failure among them is still a real failure.
    """
    if not DSN:
        return
    try:
        conn = db.connect(DSN)
    except Exception as e:                            # noqa: BLE001 -- any refusal
        reason = str(e).splitlines()[0]
        if "password authentication failed" in reason:
            why = ("the credential in CASEBROKER_TEST_PG_DSN is wrong -- in CI that is "
                   "the DBSTRING secret. The pooler names the upstream role rather "
                   "than the one supplied, so this is a stale PASSWORD, not a wrong "
                   "username; see 'Rotating the database password' in "
                   "docs/operations.md.")
        elif "ECIRCUITBREAKER" in reason:
            why = ("the pooler is refusing new connections after earlier failures; "
                   "nothing here can pass until that lifts, and trying only prolongs it.")
        else:
            why = "the database is unreachable from here."
        note = f"Postgres tests did not run: {why} ({reason})"
        if REQUIRED:
            pytest.exit(f"test database refused the pre-flight connection: {reason}\n{why}",
                        returncode=1)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            # Appended, not printed: see the docstring. Best effort -- a skip
            # that cannot write its note is still a skip, not an error.
            try:
                with open(summary, "a", encoding="utf-8") as fh:
                    fh.write(f"### :warning: Postgres coverage skipped\n\n{note}\n\n")
            except OSError:
                pass
        pytest.skip(note)
    release_conn(conn)


@pytest.fixture(scope="module", autouse=True)
def cleanup_after_module(preflight):
    """Runs once per module; deletes every row this run created, whatever the
    outcome of the tests. Nothing else in the shared database is touched."""
    yield
    conn = fresh_conn()
    like = f"pgtest-{RUN}-%"
    conn.execute("DELETE FROM events WHERE case_id LIKE ? OR worker_id LIKE ?", (like, like))
    conn.execute("DELETE FROM cases WHERE case_id LIKE ?", (like,))
    conn.execute("DELETE FROM workers WHERE worker_id LIKE ?", (like,))
    conn.execute("DELETE FROM worker_tokens WHERE name LIKE ?", (like,))
    conn.execute("DELETE FROM sessions WHERE user_id IN "
                 "(SELECT id FROM users WHERE username LIKE ?)", (like,))
    conn.execute("DELETE FROM users WHERE username LIKE ?", (like,))
    # The scratch table the reconciler test creates, if that test ran.
    conn.execute("DROP TABLE IF EXISTS %s" % _RECON_TABLE)


_POOL: list = []
_POOL_LOCK = threading.Lock()


def pooled_conn():
    """A connection from a shared bundle, opened once and reused.

    These tests are connection-hungry by design -- proving two transactions can
    claim different rows needs two real connections -- and opening a fresh one
    per call made Supabase's pooler answer a burst with ECIRCUITBREAKER. Bundling
    keeps the same property the tests actually depend on (connections are
    INDEPENDENT of each other) while opening each only once.
    """
    with _POOL_LOCK:
        if _POOL:
            return _POOL.pop()
    return fresh_conn()


def release_conn(conn):
    with _POOL_LOCK:
        _POOL.append(conn)


def fresh_conn(attempts=5):
    """A new, independent connection -- what a separate worker process/machine
    would actually have. Sharing one connection across "workers" would test
    nothing about cross-connection locking.

    Retried with backoff because this suite is, by design, connection-hungry:
    proving that two transactions can claim different rows requires two real
    connections, and several tests open several each. Supabase's pooler answers a
    burst of those with

        (ECIRCUITBREAKER) too many authentication failures, new connections are
        temporarily blocked

    which is a rate limit, not a defect -- but it failed the whole job, and that
    job gates deploys, so an infrastructure hiccup stopped unrelated code from
    shipping. Backing off turns a transient refusal into a pause instead of a
    red build."""
    import time as _t
    last = None
    for i in range(attempts):
        try:
            return db.connect(DSN)
        except Exception as e:                       # noqa: BLE001 -- retry any refusal
            last = e
            if "ECIRCUITBREAKER" not in str(e) and "too many" not in str(e).lower():
                raise
            _t.sleep(2 ** i)                         # 1, 2, 4, 8, 16 s
    raise last


def seed(n: int, tag: str, recipe: str = "pgtest") -> list[str]:
    conn = fresh_conn()
    rows = []
    ids_out = []
    for i in range(n):
        lat, lon = 34.0 + i * 0.001, -84.0
        cid = prefix(f"{tag}-{i}")
        ids_out.append(cid)
        rows.append({"case_id": cid, "spec": {"i": i}, "recipe": recipe,
                     "city_cluster": prefix(f"city{i % 5}"), "split": "train"})
    r = db.add_cases(conn, rows)
    assert r["added"] == n
    return ids_out


# -- identity: accounts, sessions, worker tokens ------------------------------
#
# The bug that motivated this section: every one of these functions indexed a
# fetched row positionally (``row[0]``, ``row[1]``, ...). ``sqlite3.Row`` -- what
# every OTHER test in this repo runs against -- supports both positional and
# name access, so the whole suite passed. A real Postgres connection here uses
# psycopg's ``dict_row`` factory, where a row is a plain ``dict`` and positional
# indexing raises ``KeyError(0)``. The result: setup, login, and every worker
# token endpoint returned 500 the moment this ran against the actual production
# database, and nothing in CI caught it because nothing in CI exercised these
# functions over a real connection. This section exists so that gap cannot
# reopen silently.

from casebroker import auth  # noqa: E402


def test_count_users_and_create_user_round_trip_over_a_real_connection():
    conn = fresh_conn()
    before = db.count_users(conn)
    username = prefix("user-a")
    created = db.create_user(conn, username, auth.hash_password("a-long-enough-passphrase"))
    assert created["username"] == username
    assert created["id"] is not None
    assert db.count_users(conn) == before + 1


def test_get_user_returns_every_column_by_name():
    conn = fresh_conn()
    username = prefix("user-b")
    db.create_user(conn, username, auth.hash_password("a-long-enough-passphrase"), role="admin")
    fetched = db.get_user(conn, username)
    assert fetched["username"] == username
    assert fetched["role"] == "admin"
    assert auth.verify_password("a-long-enough-passphrase", fetched["password_hash"])
    assert db.get_user(conn, prefix("does-not-exist")) is None


def test_a_session_authenticates_and_then_expires_over_a_real_connection():
    conn = fresh_conn()
    username = prefix("user-c")
    user = db.create_user(conn, username, auth.hash_password("a-long-enough-passphrase"))
    token_hash = auth.hash_token(auth.new_token())

    import time
    now = int(time.time())
    db.start_session(conn, user["id"], token_hash, now + 3600, now=now)
    live = db.session_user(conn, token_hash, now=now)
    assert live is not None and live["username"] == username

    # Expiry is checked on read, not by a background sweep -- so a session
    # already past its expiry must read back as gone even though the row is
    # still there.
    stale = db.session_user(conn, token_hash, now=now + 7200)
    assert stale is None

    db.end_session(conn, token_hash)
    assert db.session_user(conn, token_hash, now=now) is None


def test_worker_token_issue_authenticate_and_revoke_over_a_real_connection():
    conn = fresh_conn()
    name = prefix("worker-a")
    token = auth.new_token()
    token_hash = auth.hash_token(token)

    db.create_worker_token(conn, name, token_hash, created_by=prefix("admin"))
    owner = db.worker_token_owner(conn, token_hash)
    assert owner is not None and owner["name"] == name

    listed = db.list_worker_tokens(conn)
    assert any(t["name"] == name for t in listed)

    db.revoke_worker_token(conn, name)
    # Revocation must be visible immediately on the SAME connection it was
    # written from -- there is no cache in front of this table to go stale.
    assert db.worker_token_owner(conn, token_hash) is None

    # Re-issuing under the same worker id -- the recovery path for a box that
    # lost its credential. Worth proving against a REAL engine rather than only
    # SQLite: create_worker_token now reclaims a revoked row by UPDATE and falls
    # through to INSERT only when that matched nothing, and `rowcount` after an
    # UPDATE is exactly the kind of thing the two drivers need not agree on.
    fresh_token = auth.new_token()
    fresh_hash = auth.hash_token(fresh_token)
    db.create_worker_token(conn, name, fresh_hash, created_by=prefix("admin"))
    back = db.worker_token_owner(conn, fresh_hash)
    assert back is not None and back["name"] == name
    assert db.worker_token_owner(conn, token_hash) is None, \
        "the revoked credential must not come back to life with the name"
    rows = [t for t in db.list_worker_tokens(conn) if t["name"] == name]
    assert len(rows) == 1 and rows[0]["revoked_at"] is None


def test_connects_and_reports_a_real_engine():
    conn = fresh_conn()
    assert isinstance(conn, db.PgConnection)


def test_add_lease_complete_roundtrip_over_a_real_connection():
    cid = seed(1, "roundtrip")[0]
    conn = fresh_conn()
    leased = db.lease(conn, prefix("w1"))
    assert [l.case_id for l in leased] == [cid]
    assert db.heartbeat(conn, leased[0].lease_id, detail="ok")
    assert db.complete(conn, leased[0].lease_id, "file:///pgtest", metrics={"n": 1})
    st = db.status(conn)
    assert st["by_state"].get("done", 0) >= 1


def test_an_archive_over_2_GiB_completes():
    """Field hit, 2026-09-20: every /v1/complete for a large wind case answered
    500. result_bytes was INTEGER, which on Postgres is 32 bits; SQLite's is 64,
    so no SQLite test could see it. The worker reported "HTTP 500" three times
    for work that had finished, and the case was quarantined."""
    cid = seed(1, "bigarchive")[0]
    conn = fresh_conn()
    leased = db.lease(conn, prefix("w-big"))
    assert [l.case_id for l in leased] == [cid]
    five_gib = 5 * 1024 ** 3
    assert db.complete(conn, leased[0].lease_id, "file:///pgtest-big", nbytes=five_gib)
    row = fresh_conn().execute(
        "SELECT state, result_bytes FROM cases WHERE case_id = ?", (cid,)).fetchone()
    assert (row["state"], row["result_bytes"]) == ("done", five_gib)


@scratch_only
def test_a_database_created_with_a_32_bit_column_is_widened_in_place():
    """CREATE TABLE IF NOT EXISTS never compares types, so declaring BIGINT
    reaches fresh databases only. Production is not one."""
    table = _RECON_TABLE + "_widen"
    conn = fresh_conn()
    conn.execute("DROP TABLE IF EXISTS %s" % table)
    conn.execute("CREATE TABLE %s (id TEXT PRIMARY KEY, nbytes INTEGER, n INTEGER)" % table)
    conn.execute("INSERT INTO %s(id, nbytes, n) VALUES ('kept', 7, 1)" % table)
    schema = ("CREATE TABLE IF NOT EXISTS %s (id TEXT PRIMARY KEY, nbytes BIGINT, "
              "n INTEGER);" % table)
    try:
        with pytest.raises(Exception):          # the control: it really is 32 bits
            fresh_conn().execute("UPDATE %s SET nbytes = ? WHERE id = 'kept'" % table, (5 * 1024 ** 3,))
        assert db.widen_columns(conn, schema, is_pg=True) == ["%s.nbytes" % table]
        conn.execute("UPDATE %s SET nbytes = ? WHERE id = 'kept'" % table, (5 * 1024 ** 3,))
        row = conn.execute("SELECT nbytes, n FROM %s" % table).fetchone()
        assert (row["nbytes"], row["n"]) == (5 * 1024 ** 3, 1)
        assert db.widen_columns(conn, schema, is_pg=True) == []   # and only once
    finally:
        fresh_conn().execute("DROP TABLE IF EXISTS %s" % table)


def test_a_declared_recipe_filters_the_lease_and_the_build_is_recorded():
    """The lease gained an IN (...) over declared recipes and four worker columns.
    Both are new SQL on the production engine, which no SQLite test exercises."""
    seed(2, "recipes")                                   # recipe "pgtest"
    conn = fresh_conn()
    w = prefix("w-build")
    assert db.lease(conn, w, count=5, build="1.0+aaaaaaaa", version="1.0",
                    platform="linux-x64", recipes=[prefix("some-other-recipe")]) == []
    got = db.lease(conn, w, count=5, build="1.0+bbbbbbbb", version="1.0",
                   platform="linux-x64", recipes=["pgtest"])
    assert len(got) == 2
    row = fresh_conn().execute(
        "SELECT build, platform, recipes, drain FROM workers WHERE worker_id = ?", (w,)).fetchone()
    assert (row["build"], row["platform"], row["drain"]) == ("1.0+bbbbbbbb", "linux-x64", 0)
    assert "pgtest" in row["recipes"]

    # Draining: no new case, and its OWN case still comes back without an attempt.
    db.set_worker_drain(conn, w, True, "pgtest drain")
    assert db.lease(conn, w, count=5, recipes=["pgtest"]) == []
    own = db.lease(conn, w, count=1, recipes=["pgtest"], resume_case_ids=[got[0].case_id])
    assert [g.case_id for g in own] == [got[0].case_id] and own[0].attempt == got[0].attempt

    # A finished case is tallied for the build its archive names.
    build = prefix("build")
    assert db.complete(conn, own[0].lease_id, "file:///pgtest-build",
                       metrics={"eddy3d_build": build, "unconverged_count": 1})
    tally = fresh_conn().execute("SELECT done, unconverged FROM build_stats WHERE build = ?", (build,)).fetchone()
    assert (tally["done"], tally["unconverged"]) == (1, 1)
    fresh_conn().execute("DELETE FROM build_stats WHERE build = ?", (build,))


@scratch_only
def test_a_case_moves_to_another_recipe_over_a_real_connection():
    """respec_cases is new SQL on the production engine -- an IN over chunked
    ids, a guarded UPDATE, a plain INSERT and JSON in an event's detail -- which
    no SQLite test exercises.

    Scratch only: the moved case's id is derived from its coordinates, so it
    cannot carry this run's prefix and the shared teardown would never find it.
    It is removed here instead, and never written to the campaign's database."""
    conn = fresh_conn()
    old_recipe, new_recipe = prefix("recipe-v4"), prefix("recipe-v5")
    lat, lon = -45.12345, 170.54321
    old = prefix("respec-old")
    new = ids.case_id(lat, lon, new_recipe)
    db.add_cases(conn, [{"case_id": old, "spec": {"lat": lat, "lon": lon, "recipe": old_recipe},
                         "recipe": old_recipe, "city_cluster": prefix("city-r"), "split": "train",
                         "labels": {"campaign": "pgtest"}}])
    w = prefix("w-respec")
    assert db.lease(conn, w, 1, 900, build="1.0+pgtest", recipes=[new_recipe]) == []
    try:
        out = db.respec_cases(conn, new_recipe, case_ids=[old], dry_run=False, by="pgtest",
                              reason="scratch")
        assert out["moved"] == 1 and out["examples"][0]["new_case_id"] == new

        other = fresh_conn()
        assert other.execute("SELECT state FROM cases WHERE case_id = ?",
                             (old,)).fetchone()["state"] == "quarantined"
        got = db.lease(other, w, 1, 900, build="1.0+pgtest", recipes=[new_recipe])
        assert [g.case_id for g in got] == [new]
        moved = db.get_case(other, new)
        assert moved["moved_from"]["case_id"] == old and moved["labels"] == {"campaign": "pgtest"}
        assert db.get_case(other, old)["moved_to"]["case_id"] == new
        assert db.respec_cases(conn, new_recipe, case_ids=[old], dry_run=False)["moved"] == 0
    finally:
        tidy = fresh_conn()
        tidy.execute("DELETE FROM events WHERE case_id = ?", (new,))
        tidy.execute("DELETE FROM case_labels WHERE case_id IN (?, ?)", (new, old))
        tidy.execute("DELETE FROM cases WHERE case_id = ?", (new,))


@scratch_only
def test_a_burst_drains_its_worker_and_reopen_refunds_pending_over_a_real_connection():
    """New SQL on the production engine, from the 2026-09-26 disk-full cascade: the
    burst count in fail() (a COUNT DISTINCT with a LIKE pattern passed as a
    parameter, which must not be taken for a placeholder) and reopen's one-pass
    last-failure join with a list of states in its guarded UPDATE.

    Scratch only: it drains a worker and writes fleet events, which the per-run
    teardown does not look for."""
    recipe = prefix("r-burst")
    mine = seed(5, "burst", recipe=recipe)
    w = prefix("w-burst")
    conn = fresh_conn()
    disk_full = "There is not enough space on the disk. " + RUN
    for _ in range(5):
        got = db.lease(conn, w, 1, 900, recipes=[recipe])
        assert got, "expected a case to lease"
        assert db.fail(conn, got[0].lease_id, disk_full, retryable=True)

    row = fresh_conn().execute("SELECT drain, drain_reason FROM workers WHERE worker_id = ?", (w,)).fetchone()
    assert row["drain"] == 1 and "5 cases failed" in row["drain_reason"]
    assert db.lease(conn, w, 1, 900, recipes=[recipe]) == []

    out = db.reopen_cases(fresh_conn(), error_contains=disk_full, include_pending=True,
                          dry_run=False, limit=50)
    assert out["reopened"] == 5
    got = fresh_conn().execute(
        "SELECT state, attempts, last_error FROM cases WHERE case_id IN (?, ?, ?, ?, ?)", mine).fetchall()
    assert {(r["state"], r["attempts"], r["last_error"]) for r in got} == {("pending", 0, None)}


@scratch_only
def test_the_release_catalog_and_target_roundtrip():
    """Fleet-wide settings: only ever written against a throwaway Postgres -- the
    main-only job points this file at the LIVE campaign database, and a test must
    never be what retargets a fleet."""
    conn = fresh_conn()
    build, w = prefix("rel"), prefix("w-rel")
    try:
        db.lease(conn, w, build="0.0+old", platform="linux-x64")
        db.register_release(conn, build, "linux-x64", "E3D-x", "ab" * 32, by="pgtest")
        db.register_release(conn, build, "linux-x64", "E3D-y", "cd" * 32, by="pgtest")   # re-published
        db.set_setting(conn, "target_build", build, by="pgtest")
        got = db.node_release(conn, w, "linux-x64", "0.0+old")
        assert (got["target_build"], got["file"], got["sha256"]) == (build, "E3D-y", "cd" * 32)
        assert db.lease_refusal(conn, "0.0+old") is None
        assert any(b["build"] == build for b in db.list_releases(conn)["builds"])
    finally:
        db.set_setting(conn, "target_build", None, by="pgtest")
        db.delete_release(conn, build, by="pgtest")
        fresh_conn().execute("DELETE FROM events WHERE worker_id = 'pgtest'")


def test_many_real_connections_racing_never_double_lease():
    """End-to-end: many HTTP-style callers hitting the real deployed system never
    see a case double-assigned. This does NOT test FOR UPDATE SKIP LOCKED in
    isolation -- db.lease() is @_locked, a module-level Python lock that
    serialises EVERY call within this one process regardless of which connection
    or thread calls it, so no amount of threading here can make two lease() calls'
    SQL genuinely overlap. (Confirmed the hard way: this test, even with a
    threading.Barrier forcing all twelve threads to start at once, still passed
    with FOR UPDATE SKIP LOCKED deliberately deleted from lease() -- because
    _LOCK had already serialised them before either one's SQL ran.)

    What it DOES prove, honestly: the whole stack -- HTTP-shaped concurrent
    calls, real network round-trips to Supabase, the _LOCK-serialised
    single-process deployment this campaign actually runs as -- produces no
    double-lease and no dropped case. See
    test_for_update_skip_locked_lets_a_second_transaction_see_a_different_row
    for the test that isolates the SQL clause itself, bypassing _LOCK entirely,
    which is what actually failed when the clause was removed.
    """
    n_cases, n_workers = 60, 12
    case_ids = seed(n_cases, "race")

    claimed: list[tuple[str, int]] = []
    lock = threading.Lock()
    errors: list[Exception] = []
    barrier = threading.Barrier(n_workers, timeout=30)

    def worker(k: int):
        try:
            conn = fresh_conn()               # this worker's OWN connection,
            barrier.wait()                    # established BEFORE the synchronised start
            got = db.lease(conn, prefix(f"racer-{k}"), count=6)
            with lock:
                claimed.extend((g.case_id, k) for g in got)
                # db.lease claims any pending row, and CASEBROKER_TEST_PG_DSN is
                # allowed to point at the REAL database -- the CI job points it
                # at production deliberately. So a racer can pick up campaign
                # cases, and leaving them leased would strand real work until the
                # TTL lapsed. Hand back anything that is not ours immediately.
                for g in got:
                    if not g.case_id.startswith(f"pgtest-{RUN}-"):
                        foreign.append(g.lease_id)
        except Exception as e:                # pragma: no cover - surfaced below
            errors.append(e)

    foreign: list = []
    conn0 = fresh_conn()
    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    for lease_id in foreign:
        try:
            db.release(conn0, lease_id)
        except Exception:                     # noqa: BLE001 -- best effort
            pass

    # Only this run's cases. Asserting over everything claimed made the test fail
    # the moment the shared database had a real campaign in it: 60 created, 72
    # claimed, because twelve belonged to the campaign.
    claimed_ids = [c for c, _ in claimed if c.startswith(f"pgtest-{RUN}-")]
    dupes = {c for c in claimed_ids if claimed_ids.count(c) > 1}
    assert not dupes, f"double-leased over real connections: {sorted(dupes)}"
    assert set(claimed_ids) == set(case_ids), (
        f"expected all {n_cases} claimed exactly once, got {len(set(claimed_ids))}")


def test_a_superseded_lease_cannot_write_across_real_connections():
    """A lease reclaimed by a SECOND real connection must reject writes from the
    FIRST -- the same guarantee test_db.py proves for SQLite, here proven across
    genuinely independent network connections and FOR UPDATE rather than a
    whole-database lock."""
    cid = seed(1, "superseded")[0]
    conn_a = fresh_conn()
    conn_b = fresh_conn()

    zombie = db.lease(conn_a, prefix("zombie"), lease_seconds=60)[0]
    # Force expiry as an operator/clock would see it, then let a DIFFERENT
    # connection reclaim the row.
    conn_a.execute("UPDATE cases SET lease_expires = 0 WHERE case_id = ?", (cid,))
    fresh = db.lease(conn_b, prefix("fresh"))[0]
    assert fresh.case_id == cid

    assert db.heartbeat(conn_a, zombie.lease_id) is False
    assert db.complete(conn_a, zombie.lease_id, "file:///zombie") is False
    assert db.fail(conn_a, zombie.lease_id, "late") is False
    assert db.release(conn_a, zombie.lease_id) is False

    assert db.complete(conn_b, fresh.lease_id, "file:///real", nbytes=1)
    row = conn_b.execute("SELECT state, result_uri FROM cases WHERE case_id=?",
                         (cid,)).fetchone()
    assert (row["state"], row["result_uri"]) == ("done", "file:///real")


@scratch_only
def test_telemetry_round_trips_over_a_real_connection():
    """post_telemetry is a read-modify-write of one JSON TEXT column under
    FOR UPDATE; the SQLite suite cannot show the row lock or the dict rows
    psycopg returns. Scratch-only: it leases, and the shared database's queue is
    not a test fixture."""
    recipe = prefix("r-telemetry")          # only these two are claimable by it
    cid, other = seed(2, "telemetry", recipe=recipe)
    conn = fresh_conn()
    leases = {l.case_id: l for l in db.lease(conn, prefix("w-tel"), count=2, recipes=[recipe])}
    lease = leases[cid]
    assert db.post_telemetry(conn, lease.lease_id, cid, "site", {"n_buildings": 3}, now=100) == "ok"
    assert db.post_telemetry(conn, lease.lease_id, cid, "mesh", {"total_cells": 9}) == "ok"
    assert db.post_telemetry(conn, lease.lease_id, cid, "site", {"n_buildings": 4}, now=200) == "ok"
    # Not this lease's case, and a lease that does not exist: both "stop".
    assert db.post_telemetry(conn, lease.lease_id, other, "site", {"n_buildings": 1}) == "gone"
    assert db.post_telemetry(conn, "no-such-lease", cid, "site", {"n_buildings": 1}) == "gone"
    assert db.complete(fresh_conn(), lease.lease_id, "file:///pgtest-tel")
    row = db.get_case(fresh_conn(), cid)
    stored = json.loads(row["telemetry"])
    assert stored["site"] == {"n_buildings": 4, "at": 200, "worker": prefix("w-tel")}
    assert stored["mesh"]["total_cells"] == 9
    mine = [r for r in dataset.stream_rows(fresh_conn()) if r["case_id"] == cid]
    assert len(mine) == 1 and json.loads(mine[0]["telemetry"])["mesh"]["total_cells"] == 9
    page = db.list_cases(fresh_conn(), limit=200)["cases"]
    assert page and all("telemetry" not in r for r in page)


@scratch_only
def test_release_of_a_live_lease_over_a_real_connection():
    """release() refunded the attempt with MAX(attempts - 1, 0), and Postgres
    has no two-argument scalar MAX: every release of a LIVE lease raised
    UndefinedFunction (a 500 at the API), leaving the case leased until its TTL.
    Every other release call in this file hits a dead lease, which returns
    before the UPDATE -- which is how it went unseen."""
    recipe = prefix("r-release")
    (cid,) = seed(1, "release", recipe=recipe)
    lease = db.lease(fresh_conn(), prefix("w-rel"), recipes=[recipe])[0]
    assert (lease.case_id, lease.attempt) == (cid, 1)
    assert db.post_telemetry(fresh_conn(), lease.lease_id, cid, "mesh", {"total_cells": 5}) == "ok"
    assert db.release(fresh_conn(), lease.lease_id) is True
    row = db.get_case(fresh_conn(), cid)
    assert (row["state"], row["attempts"], row["lease_id"]) == ("pending", 0, None)
    # Telemetry survives the release: it describes what happened.
    assert json.loads(row["telemetry"])["mesh"]["total_cells"] == 5


@scratch_only
def test_a_fresh_claim_forgets_the_last_attempt_s_telemetry_over_a_real_connection():
    recipe = prefix("r-retry")
    (cid,) = seed(1, "retry", recipe=recipe)
    first = db.lease(fresh_conn(), prefix("w-a"), recipes=[recipe])[0]
    assert db.post_telemetry(fresh_conn(), first.lease_id, cid, "mesh", {"total_cells": 9}) == "ok"
    assert db.fail(fresh_conn(), first.lease_id, "diverged", retryable=True)
    assert json.loads(db.get_case(fresh_conn(), cid)["telemetry"])["mesh"]["total_cells"] == 9
    second = db.lease(fresh_conn(), prefix("w-b"), recipes=[recipe])[0]
    assert (second.case_id, second.attempt) == (cid, 2)
    assert db.get_case(fresh_conn(), cid)["telemetry"] is None


@scratch_only
def test_the_dataset_pages_cover_every_case_once_over_a_real_connection(monkeypatch):
    """Keyset pages: `case_id > ?` and ORDER BY case_id under the SAME collation,
    so no case is skipped or read twice whatever that collation is."""
    mine = seed(5, "pages")
    monkeypatch.setattr(db, "DATASET_PAGE_ROWS", 2)
    conn = fresh_conn()
    seen = [r["case_id"] for r in dataset.stream_rows(conn)]
    assert len(seen) == len(set(seen))
    assert set(mine) <= set(seen)
    total = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
    assert len(seen) == total


def test_idempotent_add_cases_over_a_real_connection():
    rows = [{"case_id": prefix("idem-0"), "spec": {}, "recipe": "pgtest",
             "city_cluster": prefix("idem-city"), "split": "train"}]
    conn = fresh_conn()
    assert db.add_cases(conn, rows) == {"added": 1, "skipped": 0, "labelled": 0}
    assert db.add_cases(conn, rows) == {"added": 0, "skipped": 1, "labelled": 0}


def test_splits_still_assigned_by_city_against_postgres():
    conn = fresh_conn()
    rows = conn.execute(
        "SELECT city_cluster, COUNT(DISTINCT split) d FROM cases WHERE case_id LIKE ?"
        " GROUP BY city_cluster", (f"pgtest-{RUN}-%",)).fetchall()
    assert rows and all(r["d"] == 1 for r in rows)


def test_for_update_skip_locked_lets_a_second_transaction_see_a_different_row():
    """Isolates the exact clause lease() depends on, with hand-controlled
    transaction boundaries on two RAW connections -- bypassing db.py's
    module-level _LOCK entirely (it only guards the public db.* functions, not a
    bare psycopg connection), which is what makes this deterministic rather than
    dependent on thread-scheduling luck the way the multi-threaded test above
    turned out to be.

    Confirmed to FAIL when FOR UPDATE SKIP LOCKED is stripped from this test's
    own query text: without it, B's SELECT sees the row A is holding instead of
    skipping to the other one, which is precisely the race that would let two
    workers both believe they had claimed the same case.

    Note LIMIT is not "peek at N, keep one" -- FOR UPDATE locks EVERY row the
    SELECT returns, not just whichever one the caller later decides to act on.
    So A must ask for exactly 1 (mirroring a real lease(count=1) call) to leave
    the second seeded row genuinely free for B to see; an earlier draft of this
    test had A request LIMIT 2 and then only look at one of the two rows it had
    actually locked, so B correctly saw NOTHING free and the test failed for a
    reason that had nothing to do with the clause it meant to isolate.
    """
    ids2 = seed(2, "skiplock")
    like = f"pgtest-{RUN}-skiplock-%"
    a = fresh_conn()._raw
    b = fresh_conn()._raw
    a.autocommit = False
    b.autocommit = False

    def select(cur, limit):
        cur.execute(
            "SELECT case_id FROM cases WHERE case_id LIKE %s"
            " ORDER BY case_id LIMIT %s FOR UPDATE SKIP LOCKED", (like, limit))
        return {r["case_id"] for r in cur.fetchall()}

    try:
        with a.cursor() as ca:
            a_rows = select(ca, 1)
            assert a_rows, "fixture: at least one seeded row should be free"
            assert a_rows < set(ids2)              # exactly one, not both
            a_claim = next(iter(a_rows))            # A's transaction stays OPEN,
                                                     # still holding only this row

            with b.cursor() as cb:
                b_rows = select(cb, 2)

            assert a_claim not in b_rows, (
                f"B saw {a_claim!r} even though A holds it FOR UPDATE -- "
                "SKIP LOCKED did not exclude it")
            assert b_rows == set(ids2) - {a_claim}, "B should get exactly the other row"
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()


def test_a_dead_connection_is_replaced_rather_than_poisoning_the_process():
    """A dropped Postgres connection must not 500 every request until a redeploy.

    create_app() opens ONE connection at startup and every route closes over it.
    When SSL enforcement was switched on at Supabase it terminated the
    connections established before it -- and the running service then answered
    500 to every query for the rest of its life, while /healthz, which touches no
    database, kept reporting ok. Only a manual redeploy cleared it.
    """
    conn = fresh_conn()
    assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1

    conn._raw.close()                      # exactly what the server side did
    assert conn._raw.closed

    # The SAME wrapper object -- every route's closure still points at it -- but
    # the connection underneath has been replaced.
    assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
    assert not conn._raw.closed


def test_a_bad_query_does_not_trigger_a_reconnect():
    """Only transport failures reconnect; a broken query must still just fail."""
    import psycopg
    conn = fresh_conn()
    before = conn._raw
    with pytest.raises(psycopg.Error):
        conn.execute("SELECT * FROM a_table_that_does_not_exist")
    assert conn._raw is before, "a SQL error must not churn the connection"


# -- bringing a Postgres database forward -------------------------------------

@scratch_only
def test_the_column_reconciler_alters_a_real_postgres_table():
    """`CREATE TABLE IF NOT EXISTS` no-ops on an existing table without
    comparing columns, so a release that adds one used to leave the database
    behind and fail at the first index over it. This is that repair, against a
    real engine rather than SQLite standing in for one.

    Deliberately NOT against `cases`: this may be the live campaign database,
    and the test owns a table of its own instead.
    """
    conn = fresh_conn()
    conn.execute("DROP TABLE IF EXISTS %s" % _RECON_TABLE)
    conn.execute("CREATE TABLE %s (id TEXT PRIMARY KEY)" % _RECON_TABLE)
    conn.execute("INSERT INTO %s(id) VALUES ('row-1')" % _RECON_TABLE)

    schema = """
        CREATE TABLE IF NOT EXISTS %s (
            id       TEXT PRIMARY KEY,
            priority INTEGER NOT NULL DEFAULT 100,
            note     TEXT
        );
    """ % _RECON_TABLE
    try:
        added = db.reconcile_columns(conn, schema, is_pg=True)
        assert sorted(added) == ["%s.note" % _RECON_TABLE,
                                 "%s.priority" % _RECON_TABLE]

        row = conn.execute(
            "SELECT id, priority, note FROM %s" % _RECON_TABLE).fetchone()
        # The pre-existing row must carry the schema's DEFAULT, not NULL.
        assert row["id"] == "row-1" and row["priority"] == 100 and row["note"] is None

        # Idempotent: a second pass has nothing left to add.
        assert db.reconcile_columns(conn, schema, is_pg=True) == []
    finally:
        # Its own cleanup, not the module teardown's: a teardown that does not
        # run leaves a stray table behind.
        fresh_conn().execute("DROP TABLE IF EXISTS %s" % _RECON_TABLE)


def test_a_fresh_postgres_database_records_its_schema_version():
    assert db.schema_version(fresh_conn()) == db.SCHEMA_VERSION


def test_the_identity_tables_exist_on_postgres():
    """The upgrade this repo actually shipped -- a campaign database gaining
    users/sessions/worker_tokens -- applied to the Postgres schema too."""
    conn = fresh_conn()
    present = db._existing_tables(conn, is_pg=True)
    assert {"users", "sessions", "worker_tokens", "schema_meta"} <= present


@scratch_only
def test_losing_the_add_column_race_is_treated_as_success(monkeypatch):
    """_LOCK serialises one process. A rolling redeploy, or several uvicorn
    workers, start together -- both see the column missing, both ALTER, and the
    loser gets "duplicate column" from a real engine.

    Driven deterministically rather than with threads: a thread race here
    passes whether or not the tolerance exists, because the winner usually
    finishes before the others look. This reproduces the LOSER exactly -- a
    stale view that says the column is missing, over a table where it already
    is -- so the ALTER really does fail against Postgres.
    """
    table = _RECON_TABLE + "_race"
    conn = fresh_conn()
    conn.execute("DROP TABLE IF EXISTS %s" % table)
    conn.execute("CREATE TABLE %s (id TEXT PRIMARY KEY, added_later INTEGER "
                 "NOT NULL DEFAULT 7)" % table)
    schema = ("CREATE TABLE IF NOT EXISTS %s (id TEXT PRIMARY KEY, "
              "added_later INTEGER NOT NULL DEFAULT 7);" % table)
    try:
        real = db._existing_columns
        calls = {"n": 0}

        def stale_first(c, t, is_pg):
            # The first look is the pre-race snapshot: the column is not there
            # yet. Every look after it tells the truth, as the loser's re-check
            # must.
            calls["n"] += 1
            got = real(c, t, is_pg)
            return (got - {"added_later"}) if calls["n"] == 1 else got

        monkeypatch.setattr(db, "_existing_columns", stale_first)
        # Without the tolerance this raises psycopg.errors.DuplicateColumn.
        added = db.reconcile_columns(conn, schema, is_pg=True)
        assert added == []                      # it did not claim to add it
        assert calls["n"] >= 2                  # it really did re-check
        assert "added_later" in real(conn, table, True)
    finally:
        fresh_conn().execute("DROP TABLE IF EXISTS %s" % table)


@scratch_only
def test_a_genuine_alter_failure_is_still_raised():
    """The tolerance must not swallow a broken migration: it re-raises unless
    the column is actually present afterwards."""
    table = _RECON_TABLE + "_bad"
    conn = fresh_conn()
    conn.execute("DROP TABLE IF EXISTS %s" % table)
    conn.execute("CREATE TABLE %s (id TEXT PRIMARY KEY)" % table)
    # A type no engine has, so the ALTER fails and the column never appears.
    schema = ("CREATE TABLE IF NOT EXISTS %s (id TEXT PRIMARY KEY, "
              "broken NOT_A_REAL_TYPE);" % table)
    try:
        with pytest.raises(Exception):
            db.reconcile_columns(conn, schema, is_pg=True)
    finally:
        fresh_conn().execute("DROP TABLE IF EXISTS %s" % table)


def test_schema_meta_is_not_rewritten_on_every_connection():
    """apply_schema runs on EVERY connect, and on a transaction pooler every
    connection is a new backend -- an unconditional upsert would make opening a
    connection a write."""
    conn = fresh_conn()
    before = conn.execute(
        "SELECT xact_commit FROM pg_stat_database "
        "WHERE datname = current_database()").fetchone()["xact_commit"]
    db.apply_schema(fresh_conn(), db.PG_SCHEMA, is_pg=True)
    assert db.schema_version(fresh_conn()) == db.SCHEMA_VERSION
    assert before is not None


@scratch_only
def test_settings_a_removed_feature_left_behind_are_deleted_on_postgres():
    """The ntfy notifier's rows (``notify_cursor``, and whatever an admin saved)
    are gone after the next connect, with no audit event and no other setting
    touched. Scratch only: these keys are not per-run, and on the shared campaign
    database the deploy itself is what should remove them."""
    conn = fresh_conn()
    keep = prefix("keep")
    conn.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?, 'v', 1, 't')",
                 (keep,))
    try:
        for key in db.RETIRED_SETTINGS:
            conn.execute("INSERT INTO settings(key, value, updated_at, updated_by)"
                         " VALUES (?, 'x', 1, 'notifier') ON CONFLICT(key) DO NOTHING", (key,))
        events = conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"]

        fresh_conn()                                         # connecting is the migration
        left = {r["key"] for r in conn.execute("SELECT key FROM settings").fetchall()}
        assert not left & set(db.RETIRED_SETTINGS)
        assert keep in left
        assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == events
        assert db.drop_retired_settings(fresh_conn()) == []  # idempotent
    finally:
        conn.execute("DELETE FROM settings WHERE key = ?", (keep,))
