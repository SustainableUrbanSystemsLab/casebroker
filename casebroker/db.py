"""Storage for the E3D Simulation Broker: SQLite for local dev/tests, Postgres for production.

Every statement lives in this one module, so the storage engine is a property of
what string you hand :func:`connect` — a file path opens SQLite, a
``postgres(ql)://`` DSN opens Postgres — and nothing above this module (the API
layer, the worker, the tests that talk through the public functions) needs to
know which one it got. That is the promise this module makes and the reason the
two engines share one set of function names and one :class:`Lease` shape.

Why two engines rather than migrating outright: SQLite needs no network and no
credentials, so the 24-plus tests that exercise lease semantics run in
milliseconds with zero external dependencies — exactly what you want for the
property that matters most (**two workers can never be handed the same case**)
to be cheap to re-verify on every change. Postgres is what a real campaign
deploys against, because a managed database survives a service restart or
redeploy where a container's local disk does not, and because the whole point of
centralising state is that MANY processes on MANY machines hit it at once, which
a single SQLite file (correctly) serialises down to one writer at a time.

The two engines therefore do not share identical SQL for the one place it would
be actively wrong to pretend they could: **claiming work under concurrency**.
SQLite's ``BEGIN IMMEDIATE`` takes a whole-database write lock immediately, so
concurrent callers simply queue up one at a time. Postgres instead uses
``SELECT ... FOR UPDATE SKIP LOCKED`` — a per-ROW lock that lets two callers claim
two different rows without blocking each other, which is the entire reason to
move off one file in the first place. See :func:`lease` for the two branches,
and :func:`_by_lease` for the matching row lock the read side needs so a
heartbeat in flight cannot be quietly outraced by a reclaim.

State machine
-------------
    pending --lease--> leased --complete--> done
       ^                  |
       |                  +--fail(retryable), or lease expiry--> pending
       |                  +--fail(fatal), or attempts > max----> quarantined
       +--release (graceful preemption) --------------------------+

Lease expiry is what makes this safe on Phoenix's preemptible ``embers`` QOS: a
worker killed without warning simply stops renewing, and the case returns to the
pool on its own. Nothing has to notice the death.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    spec           TEXT NOT NULL,
    recipe         TEXT NOT NULL,
    city_cluster   TEXT NOT NULL,
    lcz            TEXT,
    split          TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    priority       INTEGER NOT NULL DEFAULT 100,
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    lease_id       TEXT,
    lease_worker   TEXT,
    lease_expires  INTEGER,
    -- When the CURRENT lease was handed out. Deliberately not touched by
    -- heartbeat, which is what makes it an age rather than a liveness signal:
    -- lease_expires only ever says "someone said they were alive recently", and
    -- a worker wedged mid-solve says that forever.
    leased_at      INTEGER,
    last_error     TEXT,
    result_uri     TEXT,
    result_sha256  TEXT,
    result_bytes   INTEGER,
    metrics        TEXT,
    -- What the node REPORTED while it worked, one JSON object keyed by kind
    -- ("site", "mesh", "solve"): see post_telemetry. Kept through complete,
    -- fail and release, because it describes what happened; NULL = nothing
    -- reported, including every case from before it existed.
    telemetry      TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

-- The claim query filters on state and orders by (priority, case_id); this index
-- is what keeps a lease O(log n) instead of a scan over 30,000 rows.
CREATE INDEX IF NOT EXISTS idx_cases_claim ON cases(state, priority, case_id);
CREATE INDEX IF NOT EXISTS idx_cases_lease ON cases(lease_id);
-- What a worker is holding right now. Without it the dashboard's
-- "working on" lookup scans every case once per worker.
CREATE INDEX IF NOT EXISTS idx_cases_lease_worker ON cases(lease_worker, state);
CREATE INDEX IF NOT EXISTS idx_cases_split ON cases(split, state);
-- Free key/value labels a case is posted with ("campaign": "v2-pilot",
-- "batch": "2026-09-21"): what a browser filters by later, and what the fixed
-- columns (recipe, split, city, LCZ) could not foresee. One row per key;
-- re-posting a case rewrites its labels.
CREATE TABLE IF NOT EXISTS case_labels (
    case_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (case_id, key)
);
CREATE INDEX IF NOT EXISTS idx_case_labels_kv ON case_labels(key, value, case_id);
-- The dashboard's case list orders by updated_at DESC, and without this the
-- plan is "SCAN cases" plus a temp B-tree: a full sort of the whole table for
-- every page, on a timer, for every open dashboard. That is not merely slow --
-- list_cases holds _LOCK while it runs, so the sort stalls every worker's lease
-- and heartbeat behind it. Measured at 50,000 cases: 8 ms for the first page and
-- 155 ms for a deep one, against roughly 0.05 ms with the index.
CREATE INDEX IF NOT EXISTS idx_cases_updated ON cases(updated_at DESC);

CREATE TABLE IF NOT EXISTS workers (
    worker_id    TEXT PRIMARY KEY,
    host         TEXT,
    cluster      TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    cases_done   INTEGER NOT NULL DEFAULT 0,
    cases_failed INTEGER NOT NULL DEFAULT 0,
    -- WHICH CODE this worker is. A campaign runs for months and its nodes are
    -- updated while it runs; the product version is the same for every push, so
    -- `build` (version+commit) is the only thing that tells two nodes apart.
    -- NULL = a worker from before builds were declared.
    build        TEXT,
    version      TEXT,
    platform     TEXT,
    -- JSON list of the exact recipes it can produce; NULL = never declared.
    recipes      TEXT,
    -- Drain is "no NEW work": the case in flight finishes, and the worker may
    -- still resume its OWN case after a restart. The operator's handle for any
    -- maintenance, not only an update.
    drain        INTEGER NOT NULL DEFAULT 0,
    drain_reason TEXT,
    -- A per-worker target overrides the fleet's: how ONE node is moved to a new
    -- build first, watched, and only then followed by the rest.
    target_build TEXT,
    -- "<build>: <why>" when this node tried a build, could not start it, and
    -- went back to the one before. Cleared when it reaches its target.
    update_failed TEXT,
    -- What the node last SAID about moving to its target ("the broker wants
    -- build X and has no file registered for win-x64", "installed and verified;
    -- switching after the case"), and when. Cleared once it is there.
    update_state     TEXT,
    update_state_at  INTEGER,
    -- When it last asked /v1/node/release. A node that never asks cannot update
    -- itself whatever the target says: a Python worker, or an E3D.exe from
    -- before releases existed.
    release_asked_at INTEGER,
    -- When the canary target was set, so "behind for six hours" can be said.
    target_set_at    INTEGER,
    -- The build before the last change, when it changed, and how many times in
    -- a row the change undid the previous one: two clients sharing a worker_id
    -- show up as the build flipping back and forth on every poll.
    prev_build       TEXT,
    build_changed_at INTEGER,
    build_flips      INTEGER,
    id_conflict      TEXT,
    id_conflict_at   INTEGER
);
-- What a SCHEDULER holds that has not reached the broker yet. A worker queued in
-- SLURM has never contacted this service -- it does not exist here until its
-- first lease -- so "5,000 pending, 0 leased" is accurate and still tells an
-- operator nothing about whether anything is coming. This table is the missing
-- half: a snapshot somebody with squeue access pushes in. It is deliberately
-- REPORTED rather than inferred, and always read back with its age, because a
-- stale snapshot presented as live is worse than no snapshot at all.
CREATE TABLE IF NOT EXISTS fleet (
    cluster      TEXT PRIMARY KEY,
    queued       INTEGER NOT NULL DEFAULT 0,
    running      INTEGER NOT NULL DEFAULT 0,
    detail       TEXT,
    reported_at  INTEGER NOT NULL
);

-- What a case will be meshed from -- GlobalBuildingAtlas footprints and heights,
-- GEDTM30 relief, Meta/WRI canopy -- cached as one GeoJSON blob. The three
-- queries cost seconds against object storage and cannot change for pinned
-- sources, so they are paid once per case rather than on every dashboard open.
CREATE TABLE IF NOT EXISTS footprints (
    case_id     TEXT PRIMARY KEY,
    geojson     TEXT NOT NULL,
    n           INTEGER NOT NULL DEFAULT 0,
    fetched_at  INTEGER NOT NULL
);


-- Append-only audit trail. Every transition lands here, so "why is this case
-- still pending after three days" stays answerable after the fact.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    case_id   TEXT,
    worker_id TEXT,
    event     TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_id, id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(event, ts);

-- Two kinds of principal, deliberately not one table with a flag.
--
-- A HUMAN is interactive: they log in with a password and get a session, which
-- expires. A MACHINE is not: an unattended worker cannot type a password, so it
-- carries a long-lived token. The old design gave both the same shared secret
-- out of an environment variable, which meant no way to tell which machine had
-- used it, and no way to revoke one machine without rotating every machine.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    -- scrypt, salted per user; see auth.hash_password. Never the password.
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER
);

-- Server-side sessions rather than signed cookies carrying claims: logging a
-- user out, or revoking everything after a laptop is lost, has to be a DELETE
-- that takes effect immediately, not a wait for a signature to expire.
-- Only the HASH is stored, so a database dump does not hand over live sessions.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- One row per MACHINE. `name` is the worker id, so the dashboard can say which
-- box last used a credential and when -- and revoking one is an UPDATE here
-- rather than an environment-variable edit plus a redeploy.
-- Stored hashed for the same reason as sessions.
CREATE TABLE IF NOT EXISTS worker_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT UNIQUE NOT NULL,
    token_hash   TEXT UNIQUE NOT NULL,
    created_by   TEXT,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER,
    revoked_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_worker_tokens_hash ON worker_tokens(token_hash);

-- A machine asking to join. The node generates its OWN token and sends only the
-- SHA-256, so approving a request promotes a hash into worker_tokens and the raw
-- credential never exists on the broker at all -- not in this table, not for the
-- seconds between "approve" and the node's next poll. The textbook device flow
-- has the server mint the token, which means holding it readable until it is
-- collected; that would be the one place in this database a live credential
-- could be read back out.
CREATE TABLE IF NOT EXISTS pairings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_code    TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL,
    host         TEXT,
    platform     TEXT,
    requested_ip TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    resolved_by  TEXT,
    resolved_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pairings_status ON pairings(status, expires_at);

-- What schema revision this database is at. Written by apply_schema on every
-- connect; read by `casebroker doctor`. Nothing branches on it -- the column
-- reconciler makes the schema self-healing without a version to compare -- but
-- "which revision is production actually at" was previously unanswerable.
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Fleet-wide switches an operator sets while a campaign runs: which build the
-- nodes should be on, how eagerly they move to it, and which builds are refused.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT
);

-- The builds an operator has PUBLISHED to the nodes' release share, one row per
-- platform, with the hash a node verifies before it runs a byte of it. The
-- broker never serves the file: it says WHICH build and WHAT it must hash to.
CREATE TABLE IF NOT EXISTS releases (
    build     TEXT NOT NULL,
    platform  TEXT NOT NULL,
    file      TEXT NOT NULL,
    sha256    TEXT NOT NULL,
    notes     TEXT,
    added_at  INTEGER NOT NULL,
    added_by  TEXT,
    PRIMARY KEY (build, platform)
);

-- What each build has DONE, which is what decides whether a canary is promoted.
CREATE TABLE IF NOT EXISTS build_stats (
    build       TEXT PRIMARY KEY,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    unconverged INTEGER NOT NULL DEFAULT 0,
    -- Summed wall time of the done cases, so a mean per build can be compared
    -- with the build before it. BIGINT: the 2 GiB lesson of result_bytes.
    wall_seconds BIGINT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL
);
"""

# Same schema, Postgres-flavoured: no PRAGMAs (meaningless there), and the
# events table's autoincrement id is a SERIAL rather than SQLite's
# INTEGER PRIMARY KEY AUTOINCREMENT. Everything else -- types, IF NOT EXISTS,
# indexes, the ON CONFLICT syntax used elsewhere -- is valid, identical DDL/DML
# on both engines.
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    spec           TEXT NOT NULL,
    recipe         TEXT NOT NULL,
    city_cluster   TEXT NOT NULL,
    lcz            TEXT,
    split          TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    priority       INTEGER NOT NULL DEFAULT 100,
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    lease_id       TEXT,
    lease_worker   TEXT,
    lease_expires  INTEGER,
    -- When the CURRENT lease was handed out. Deliberately not touched by
    -- heartbeat, which is what makes it an age rather than a liveness signal:
    -- lease_expires only ever says "someone said they were alive recently", and
    -- a worker wedged mid-solve says that forever.
    leased_at      INTEGER,
    last_error     TEXT,
    result_uri     TEXT,
    result_sha256  TEXT,
    -- BIGINT, not INTEGER: Postgres INTEGER is 32 bits, and a wind case's archive
    -- passes 2.1 GB. Every such /v1/complete then died with "integer out of
    -- range" -- an unhandled 500 the worker could only report as "HTTP 500",
    -- for work that had FINISHED -- three times, and the case was quarantined.
    -- SQLite's INTEGER is 64 bits, so the suite never saw it.
    result_bytes   BIGINT,
    metrics        TEXT,
    -- What the node REPORTED while it worked, one JSON object keyed by kind
    -- ("site", "mesh", "solve"): see post_telemetry. Kept through complete,
    -- fail and release, because it describes what happened; NULL = nothing
    -- reported, including every case from before it existed.
    telemetry      TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_claim ON cases(state, priority, case_id);
CREATE INDEX IF NOT EXISTS idx_cases_lease ON cases(lease_id);
-- What a worker is holding right now. Without it the dashboard's
-- "working on" lookup scans every case once per worker.
CREATE INDEX IF NOT EXISTS idx_cases_lease_worker ON cases(lease_worker, state);
CREATE INDEX IF NOT EXISTS idx_cases_split ON cases(split, state);
-- Free key/value labels a case is posted with ("campaign": "v2-pilot",
-- "batch": "2026-09-21"): what a browser filters by later, and what the fixed
-- columns (recipe, split, city, LCZ) could not foresee. One row per key;
-- re-posting a case rewrites its labels.
CREATE TABLE IF NOT EXISTS case_labels (
    case_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (case_id, key)
);
CREATE INDEX IF NOT EXISTS idx_case_labels_kv ON case_labels(key, value, case_id);
-- The dashboard's case list orders by updated_at DESC, and without this the
-- plan is "SCAN cases" plus a temp B-tree: a full sort of the whole table for
-- every page, on a timer, for every open dashboard. That is not merely slow --
-- list_cases holds _LOCK while it runs, so the sort stalls every worker's lease
-- and heartbeat behind it. Measured at 50,000 cases: 8 ms for the first page and
-- 155 ms for a deep one, against roughly 0.05 ms with the index.
CREATE INDEX IF NOT EXISTS idx_cases_updated ON cases(updated_at DESC);

CREATE TABLE IF NOT EXISTS workers (
    worker_id    TEXT PRIMARY KEY,
    host         TEXT,
    cluster      TEXT,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    cases_done   INTEGER NOT NULL DEFAULT 0,
    cases_failed INTEGER NOT NULL DEFAULT 0,
    -- WHICH CODE this worker is. A campaign runs for months and its nodes are
    -- updated while it runs; the product version is the same for every push, so
    -- `build` (version+commit) is the only thing that tells two nodes apart.
    -- NULL = a worker from before builds were declared.
    build        TEXT,
    version      TEXT,
    platform     TEXT,
    -- JSON list of the exact recipes it can produce; NULL = never declared.
    recipes      TEXT,
    -- Drain is "no NEW work": the case in flight finishes, and the worker may
    -- still resume its OWN case after a restart. The operator's handle for any
    -- maintenance, not only an update.
    drain        INTEGER NOT NULL DEFAULT 0,
    drain_reason TEXT,
    -- A per-worker target overrides the fleet's: how ONE node is moved to a new
    -- build first, watched, and only then followed by the rest.
    target_build TEXT,
    -- "<build>: <why>" when this node tried a build, could not start it, and
    -- went back to the one before. Cleared when it reaches its target.
    update_failed TEXT,
    -- What the node last SAID about moving to its target ("the broker wants
    -- build X and has no file registered for win-x64", "installed and verified;
    -- switching after the case"), and when. Cleared once it is there.
    update_state     TEXT,
    update_state_at  INTEGER,
    -- When it last asked /v1/node/release. A node that never asks cannot update
    -- itself whatever the target says: a Python worker, or an E3D.exe from
    -- before releases existed.
    release_asked_at INTEGER,
    -- When the canary target was set, so "behind for six hours" can be said.
    target_set_at    INTEGER,
    -- The build before the last change, when it changed, and how many times in
    -- a row the change undid the previous one: two clients sharing a worker_id
    -- show up as the build flipping back and forth on every poll.
    prev_build       TEXT,
    build_changed_at INTEGER,
    build_flips      INTEGER,
    id_conflict      TEXT,
    id_conflict_at   INTEGER
);
-- What a SCHEDULER holds that has not reached the broker yet. A worker queued in
-- SLURM has never contacted this service -- it does not exist here until its
-- first lease -- so "5,000 pending, 0 leased" is accurate and still tells an
-- operator nothing about whether anything is coming. This table is the missing
-- half: a snapshot somebody with squeue access pushes in. It is deliberately
-- REPORTED rather than inferred, and always read back with its age, because a
-- stale snapshot presented as live is worse than no snapshot at all.
CREATE TABLE IF NOT EXISTS fleet (
    cluster      TEXT PRIMARY KEY,
    queued       INTEGER NOT NULL DEFAULT 0,
    running      INTEGER NOT NULL DEFAULT 0,
    detail       TEXT,
    reported_at  INTEGER NOT NULL
);

-- What a case will be meshed from -- GlobalBuildingAtlas footprints and heights,
-- GEDTM30 relief, Meta/WRI canopy -- cached as one GeoJSON blob. The three
-- queries cost seconds against object storage and cannot change for pinned
-- sources, so they are paid once per case rather than on every dashboard open.
CREATE TABLE IF NOT EXISTS footprints (
    case_id     TEXT PRIMARY KEY,
    geojson     TEXT NOT NULL,
    n           INTEGER NOT NULL DEFAULT 0,
    fetched_at  INTEGER NOT NULL
);


CREATE TABLE IF NOT EXISTS events (
    id        SERIAL PRIMARY KEY,
    ts        INTEGER NOT NULL,
    case_id   TEXT,
    worker_id TEXT,
    event     TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_id, id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(event, ts);

-- Two kinds of principal, deliberately not one table with a flag.
--
-- A HUMAN is interactive: they log in with a password and get a session, which
-- expires. A MACHINE is not: an unattended worker cannot type a password, so it
-- carries a long-lived token. The old design gave both the same shared secret
-- out of an environment variable, which meant no way to tell which machine had
-- used it, and no way to revoke one machine without rotating every machine.
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    -- scrypt, salted per user; see auth.hash_password. Never the password.
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER
);

-- Server-side sessions rather than signed cookies carrying claims: logging a
-- user out, or revoking everything after a laptop is lost, has to be a DELETE
-- that takes effect immediately, not a wait for a signature to expire.
-- Only the HASH is stored, so a database dump does not hand over live sessions.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- One row per MACHINE. `name` is the worker id, so the dashboard can say which
-- box last used a credential and when -- and revoking one is an UPDATE here
-- rather than an environment-variable edit plus a redeploy.
-- Stored hashed for the same reason as sessions.
CREATE TABLE IF NOT EXISTS worker_tokens (
    id           SERIAL PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    token_hash   TEXT UNIQUE NOT NULL,
    created_by   TEXT,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER,
    revoked_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_worker_tokens_hash ON worker_tokens(token_hash);

-- A machine asking to join. The node generates its OWN token and sends only the
-- SHA-256, so approving a request promotes a hash into worker_tokens and the raw
-- credential never exists on the broker at all -- not in this table, not for the
-- seconds between "approve" and the node's next poll. The textbook device flow
-- has the server mint the token, which means holding it readable until it is
-- collected; that would be the one place in this database a live credential
-- could be read back out.
CREATE TABLE IF NOT EXISTS pairings (
    id           SERIAL PRIMARY KEY,
    user_code    TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL,
    host         TEXT,
    platform     TEXT,
    requested_ip TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    resolved_by  TEXT,
    resolved_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pairings_status ON pairings(status, expires_at);

-- What schema revision this database is at. Written by apply_schema on every
-- connect; read by `casebroker doctor`. Nothing branches on it -- the column
-- reconciler makes the schema self-healing without a version to compare -- but
-- "which revision is production actually at" was previously unanswerable.
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Fleet-wide switches an operator sets while a campaign runs: which build the
-- nodes should be on, how eagerly they move to it, and which builds are refused.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT
);

-- The builds an operator has PUBLISHED to the nodes' release share, one row per
-- platform, with the hash a node verifies before it runs a byte of it. The
-- broker never serves the file: it says WHICH build and WHAT it must hash to.
CREATE TABLE IF NOT EXISTS releases (
    build     TEXT NOT NULL,
    platform  TEXT NOT NULL,
    file      TEXT NOT NULL,
    sha256    TEXT NOT NULL,
    notes     TEXT,
    added_at  INTEGER NOT NULL,
    added_by  TEXT,
    PRIMARY KEY (build, platform)
);

-- What each build has DONE, which is what decides whether a canary is promoted.
CREATE TABLE IF NOT EXISTS build_stats (
    build       TEXT PRIMARY KEY,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    unconverged INTEGER NOT NULL DEFAULT 0,
    -- Summed wall time of the done cases, so a mean per build can be compared
    -- with the build before it. BIGINT: the 2 GiB lesson of result_bytes.
    wall_seconds BIGINT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL
);
"""


# -- applying the schema, including to a database that predates part of it ----
#
# Every statement above is `IF NOT EXISTS`, which makes ADDING A TABLE upgrade
# itself on the next restart -- and that is exactly what this repo's history has
# always done (a262e4f added users/sessions/worker_tokens, dd3e831 added fleet).
# It does nothing at all for ADDING A COLUMN: `CREATE TABLE IF NOT EXISTS` sees
# the table already there and no-ops without comparing columns, so the new column
# never appears. The failure that follows is not subtle but it is badly
# misleading -- an index over the new column raises
#
#     sqlite3.OperationalError: no such column: priority
#
# at connect() time, which reads like a corrupt database rather than a schema one
# release behind. So the schema is applied in three passes instead of one
# executescript: create the tables, reconcile the columns of the ones that
# already existed, and only then build the indexes -- an index is very often the
# thing that references the newly added column.

SCHEMA_VERSION = 6

# A column definition that cannot be bolted onto a table that already exists.
# Detected and reported by name, because the alternative -- quietly adding the
# column without its constraint -- produces a database that looks migrated and
# is not.
_UNADDABLE = ("primary key", "unique", "autoincrement", "serial",
              "references", "generated")

# The first word of an entry in a CREATE TABLE body, when it names a TABLE
# constraint rather than a column.
_TABLE_CONSTRAINTS = ("primary", "foreign", "unique", "check",
                      "constraint", "exclude")


def _strip_sql_comments(script: str) -> str:
    """Drop `--` comments, respecting single-quoted literals.

    Quote-aware because the schema really does carry literals (`DEFAULT
    'pending'`), and a blind strip would be one stray `--` inside one of them
    away from truncating a statement.
    """
    out, i, n, in_str = [], 0, len(script), False
    while i < n:
        ch = script[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                in_str = False
            i += 1
        elif ch == "'":
            in_str = True
            out.append(ch)
            i += 1
        elif ch == "-" and i + 1 < n and script[i + 1] == "-":
            while i < n and script[i] != "\n":
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _split_statements(script: str) -> list[str]:
    """Split on semicolons that are at paren depth 0 and outside a literal."""
    stmts, cur, depth, in_str = [], [], 0, False
    for ch in script:
        if in_str:
            cur.append(ch)
            if ch == "'":
                in_str = False
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            if "".join(cur).strip():
                stmts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        stmts.append("".join(cur).strip())
    return stmts


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas at paren depth 0."""
    parts, cur, depth = [], [], 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def parse_schema_columns(script: str) -> dict[str, dict[str, str]]:
    """``{table: {column: its full DDL}}`` for every CREATE TABLE in `script`.

    Public because a test asserts it agrees with what the database itself
    reports after running the same DDL -- a hand-written parser that silently
    disagreed with the engine would make the reconciler below confidently wrong.
    """
    clean = _strip_sql_comments(script)
    tables: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                         r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", clean, re.IGNORECASE):
        depth, i = 1, m.end()
        while i < len(clean) and depth:
            if clean[i] == "(":
                depth += 1
            elif clean[i] == ")":
                depth -= 1
            i += 1
        cols: dict[str, str] = {}
        for part in _split_top_level(clean[m.end():i - 1]):
            # The leading identifier, stopping at whitespace OR an opening
            # paren -- `UNIQUE(a, b)` is a table constraint just as much as
            # `UNIQUE (a, b)` is, and splitting on whitespace alone would read
            # the first as a column named "UNIQUE(a,".
            lead = re.match(r"[A-Za-z_][A-Za-z0-9_]*", part)
            if lead is None or lead.group(0).lower() in _TABLE_CONSTRAINTS:
                continue
            cols[lead.group(0)] = " ".join(part.split())
        tables[m.group(1)] = cols
    return tables


def _existing_tables(conn, is_pg: bool) -> set[str]:
    if is_pg:
        rows = conn.execute(
            "SELECT table_name AS n FROM information_schema.tables "
            "WHERE table_schema = current_schema()").fetchall()
    else:
        rows = conn.execute(
            "SELECT name AS n FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["n"] for r in rows}


def _existing_columns(conn, table: str, is_pg: bool) -> set[str]:
    if is_pg:
        rows = conn.execute(
            "SELECT column_name AS n FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,)).fetchall()
        return {r["n"] for r in rows}
    # PRAGMA takes no placeholder. `table` comes from our own schema constant,
    # never from a caller, so there is no injection surface here.
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _why_unaddable(ddl: str) -> str | None:
    """Why this column cannot be added to a table that already exists.

    The column's own NAME is dropped before looking for constraint keywords, and
    the match is on whole words: `references_count INTEGER DEFAULT 0` and
    `unique_id TEXT` are both perfectly addable, and a substring search over the
    whole definition would refuse them and block a legitimate upgrade.
    """
    words = ddl.split()
    rest = " ".join(words[1:]).lower() if len(words) > 1 else ""
    for kw in _UNADDABLE:
        if re.search(r"\b" + kw.replace(" ", r"\s+") + r"\b", rest):
            return kw.upper()
    if re.search(r"\bnot\s+null\b", rest) and not re.search(r"\bdefault\b", rest):
        return "NOT NULL without a DEFAULT"
    return None


def reconcile_columns(conn, script: str, is_pg: bool) -> list[str]:
    """ALTER TABLE ADD COLUMN for every column the schema has and the database
    does not. Returns what it added, as ``table.column`` strings.

    Only ever ADDS. A column the database has and the schema no longer does is
    left strictly alone: dropping it would destroy data to satisfy a code version
    that may itself be about to be rolled back.
    """
    added: list[str] = []
    present = _existing_tables(conn, is_pg)
    for table, columns in parse_schema_columns(script).items():
        if table not in present:
            continue                      # the CREATE TABLE pass just made it
        have = _existing_columns(conn, table, is_pg)
        for column, ddl in columns.items():
            if column in have:
                continue
            why = _why_unaddable(ddl)
            if why is not None:
                raise RuntimeError(
                    "cannot bring this database up to date automatically: "
                    "%s.%s is declared %s, which cannot be added to a table that "
                    "already exists. Add it by hand, or recreate the table, "
                    "before starting this version." % (table, column, why))
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, ddl))
            except Exception:                                # noqa: BLE001
                # _LOCK serialises this process only. Two of them starting at
                # once -- a rolling redeploy, or several uvicorn workers --
                # both see the column missing and both ALTER, and the loser
                # gets "duplicate column". Losing that race is a success: the
                # column is there. Anything else is a real failure and is
                # re-raised, so this cannot mask a broken migration.
                if column not in _existing_columns(conn, table, is_pg):
                    raise
                continue
            added.append("%s.%s" % (table, column))
    return added


def columns_to_widen(script: str, reported: Iterable[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """``(table, column)`` for every column the schema declares BIGINT that the
    database still holds as a 32-bit ``integer``.

    `reported` is ``(table, column, data_type)`` as information_schema spells it.
    Pure, so the decision is testable without a Postgres: the reconciler above
    only ever ADDS columns, and `CREATE TABLE IF NOT EXISTS` never compares
    types, so without this a declared BIGINT reaches fresh databases only and
    production keeps the INTEGER it was created with.
    """
    declared = parse_schema_columns(script)
    out = []
    for table, column, data_type in reported:
        ddl = declared.get(table, {}).get(column)
        if ddl and data_type.lower() == "integer" and re.search(r"\bBIGINT\b", ddl, re.IGNORECASE):
            out.append((table, column))
    return sorted(out)


def widen_columns(conn, script: str, is_pg: bool) -> list[str]:
    """Postgres only; SQLite's INTEGER is already 64 bits. int4 -> int8 is a
    lossless rewrite, and repeating it on a column that is already BIGINT is a
    no-op, so two processes starting at once cannot hurt each other."""
    if not is_pg:
        return []
    rows = conn.execute(
        "SELECT table_name AS t, column_name AS c, data_type AS d "
        "FROM information_schema.columns WHERE table_schema = current_schema()").fetchall()
    widened = []
    for table, column in columns_to_widen(script, [(r["t"], r["c"], r["d"]) for r in rows]):
        conn.execute("ALTER TABLE %s ALTER COLUMN %s TYPE BIGINT" % (table, column))
        widened.append("%s.%s" % (table, column))
    return widened


#: Settings rows a removed feature left behind. The ntfy push notifier (shipped
#: in one release, then removed) wrote ``notify_cursor`` at every start, and an
#: admin may have set the others from Settings -- a topic URL and a token among
#: them, which are secrets. Nothing reads any of them now, so they are deleted
#: when a database is opened rather than left in the table for good.
RETIRED_SETTINGS = ("notify_cursor", "notify_url", "notify_token",
                    "notify_events", "notify_public_url")


def drop_retired_settings(conn) -> list[str]:
    """Delete whichever RETIRED_SETTINGS rows exist; returns their keys.

    Read before writing, because this runs on every connect and opening a
    connection should not be a write: once the rows are gone, a connect pays
    one primary-key lookup and nothing else. Idempotent, and two processes
    starting at once cannot hurt each other -- the second deletes nothing.
    Not audited: nobody changed a setting, a release retired it.
    """
    marks = ",".join("?" * len(RETIRED_SETTINGS))
    found = [r["key"] for r in conn.execute(
        "SELECT key FROM settings WHERE key IN (%s)" % marks, RETIRED_SETTINGS).fetchall()]
    if found:
        conn.execute("DELETE FROM settings WHERE key IN (%s)" % marks, RETIRED_SETTINGS)
    return sorted(found)


def apply_schema(conn, script: str, is_pg: bool) -> list[str]:
    """Create the tables, reconcile the ones that predate this version, then
    build the indexes. Returns the columns added, so a caller can log that an
    upgrade actually happened rather than leaving it silent.
    """
    pre, tables, indexes = [], [], []
    for stmt in _split_statements(_strip_sql_comments(script)):
        if re.match(r"CREATE\s+TABLE", stmt, re.IGNORECASE):
            tables.append(stmt)
        elif re.match(r"CREATE\s+(UNIQUE\s+)?INDEX", stmt, re.IGNORECASE):
            indexes.append(stmt)
        else:
            pre.append(stmt)              # the PRAGMAs, on SQLite
    for stmt in pre + tables:
        conn.execute(stmt)
    added = reconcile_columns(conn, script, is_pg)
    for stmt in indexes:
        conn.execute(stmt)
    if added:
        # Bringing a database forward is exactly the event an operator wants in
        # the log when something looks different afterwards, and it happens
        # unattended on the first connection after a deploy.
        print("[schema] brought this database forward: added " + ", ".join(added),
              file=sys.stderr)
    dropped = drop_retired_settings(conn)
    if dropped:
        print("[schema] removed retired settings: " + ", ".join(dropped), file=sys.stderr)
    # Only when it actually changed. This runs on EVERY connection, and on a
    # transaction pooler every connection is a new backend -- an unconditional
    # upsert would make opening a connection a write.
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    if row is None or row["value"] != str(SCHEMA_VERSION):
        # Behind the version check, so a connection to an up-to-date database
        # pays nothing for it.
        widened = widen_columns(conn, script, is_pg)
        if widened:
            print("[schema] widened to BIGINT: " + ", ".join(widened), file=sys.stderr)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),))
    return added


def schema_version(conn) -> int | None:
    """What schema revision this database is at, or None if it predates the
    marker. `casebroker doctor` reports it; nothing branches on it."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    except Exception:                                        # noqa: BLE001
        return None
    return int(row["value"]) if row else None


@dataclass(frozen=True)
class Lease:
    case_id: str
    lease_id: str
    expires_at: int
    spec: dict[str, Any]
    attempt: int


# One process-wide lock around every statement. FastAPI runs sync endpoints in a
# threadpool, so the shared connection is touched from many threads. For SQLite
# this is what makes a shared, check_same_thread=False connection safe at all;
# for Postgres it is pure belt-and-suspenders (correctness across PROCESSES/
# machines comes from FOR UPDATE SKIP LOCKED in the database itself, not from
# this in-process lock) but costs nothing to keep uniform across both engines.
_LOCK = threading.RLock()

# The longest ONE lease may live, however healthy its heartbeats look.
#
# lease_expires answers "did a worker speak recently", and heartbeat pushes it
# forward every few minutes for as long as the process is alive. That catches a
# worker that DIES -- preemption, walltime, a dropped node -- within the TTL.
# It cannot catch a worker that is alive and reporting and simply never
# finishes, because the heartbeat thread runs independently of the runner: a
# solve that wedges keeps renewing its own lease and the case is never
# reclaimed. Seven days is far beyond any real case (66 core-hours is roughly
# three wall-hours on 24 cores) so this only ever fires on something genuinely
# stuck.
MAX_LEASE_AGE_SECONDS = int(os.environ.get("CASEBROKER_MAX_LEASE_AGE", str(7 * 86400)))
# ...and that age alone no longer takes a case back from a worker that is still
# MOVING. The cap was sized when a case was ~66 core-hours (~3 wall-hours on 24
# cores). A cyl-1008/of12-v4 case is ~800 (measured: 23.7 h x 36 ranks, 16.7 h x
# 48), so a 4-CPU node needs ~8 days, and at day 7 the cap released it -- a week
# of healthy solving discarded, an attempt charged, and three of those
# quarantine a site nothing is wrong with. What the cap is FOR is a solve that
# is alive but wedged; a wedged solve's progress line stops changing, a slow one
# moves on to its next direction. So an old lease is reclaimed only once its
# progress line has also not changed for this long. A lease that never reported
# progress at all (an older worker) is judged on age alone, as before.
LEASE_STALL_SECONDS = int(os.environ.get("CASEBROKER_LEASE_STALL", str(86400)))

# The newest 'progress' event of a case -- heartbeat records one only when the
# line CHANGED, so this is when the worker last said something new.
_LAST_PROGRESS_SQL = ("COALESCE((SELECT MAX(e.ts) FROM events e WHERE e.case_id = cases.case_id"
                      " AND e.event = 'progress'), 0)")


def _is_connection_error(exc: BaseException) -> bool:
    """Is this the connection dying, rather than the query being wrong?

    A bad query must not trigger a reconnect -- that would paper over real bugs
    and churn connections under a syntax error. psycopg raises OperationalError
    (and InterfaceError for an already-closed connection) for transport-level
    failures specifically, which is the distinction being drawn here.
    """
    try:
        import psycopg
    except Exception:
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


class PgConnection:
    """Thin shim so call sites written for SQLite keep working against Postgres.

    Two things this repo's other modules and its own tests do against the raw
    connection, unchanged, that this class exists to keep true: pass ``?``
    positional placeholders (SQLite's style; psycopg wants ``%s``), and iterate a
    cursor's result directly or call ``.fetchone()``/``.fetchall()`` on it and
    index a row by column name (``row["state"]``) the way ``sqlite3.Row`` allows.
    ``?`` never appears inside a string LITERAL anywhere in this module's SQL --
    only ever as a placeholder -- so a blind text substitution is safe here
    without a real SQL parser.

    ``BEGIN IMMEDIATE`` is SQLite-only syntax (Postgres has no ``IMMEDIATE``
    transaction mode and would raise a syntax error on it), so that one literal
    is translated to plain ``BEGIN``; the row-level locking SQLite gets for free
    from the whole-database lock, Postgres gets instead from ``FOR UPDATE`` /
    ``FOR UPDATE SKIP LOCKED`` in the specific queries that need it (see
    :func:`lease` and :func:`_by_lease`).
    """

    def __init__(self, raw, dsn: str | None = None):
        self._raw = raw
        # Whether an explicit BEGIN..COMMIT is currently open on this connection.
        # Reconnecting inside one silently discards it: the replacement has no
        # transaction, so the caller's COMMIT succeeds against nothing and every
        # UPDATE between the BEGIN and the failure is gone -- while the caller,
        # `lease()`, returns its Lease objects as though they had been written.
        # The worker then holds cases the database still lists as pending, and
        # the next worker leases the same ones.
        self._in_tx = False
        # Set when a repair was needed but had to be deferred out of a
        # transaction, so the next call outside one performs it.
        self._needs_reconnect = False
        # Kept so a dead connection can be replaced in place. The identity of
        # THIS object never changes, which is what makes the repair invisible:
        # create_app() opens one connection at startup and every route closes
        # over it, so without a stable wrapper there is no way to hand the
        # running process a new one short of restarting it.
        self._dsn = dsn

    def _reconnect(self) -> bool:
        """Replace a dead underlying connection. True if a new one was opened.

        Postgres connections do not last forever and nothing here pretended
        otherwise on purpose -- they are dropped by a pooler timing out, a
        Supabase maintenance restart, or (the case that prompted this) enabling
        SSL enforcement, which terminates connections established before it. The
        process then held one permanently broken connection and answered 500 to
        every query for the rest of its life, while /healthz -- which touches no
        database -- kept reporting ok. Only a manual redeploy cleared it.
        """
        if not self._dsn:
            return False
        import psycopg
        from psycopg.rows import dict_row
        try:
            self._raw.close()
        except Exception:
            pass                      # already dead; nothing to salvage
        self._raw = psycopg.connect(self._dsn, autocommit=True,
                                    row_factory=dict_row, prepare_threshold=None)
        return True

    def execute(self, sql: str, params: Iterable[Any] = ()) -> "_PgCursor":
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            sql = "BEGIN"
        sql = sql.replace("?", "%s")
        params = tuple(params) if params else None
        verb = sql.strip().upper()
        # Known-dead up front: reconnect before running anything. Safe because
        # nothing has been sent yet, so there is no half-applied work to repeat --
        # but only OUTSIDE a transaction, for the reason in __init__.
        if not self._in_tx and (getattr(self._raw, "closed", False)
                                or self._needs_reconnect):
            self._reconnect()
            self._needs_reconnect = False
        try:
            cur = self._raw.cursor()
            cur.execute(sql, params)
        except Exception as exc:
            # Deliberately NOT a retry. This statement may be one inside an
            # explicit BEGIN..COMMIT (see lease/complete/fail), and re-running it
            # on a new connection would execute it outside the transaction its
            # caller believes it is in -- a far worse failure than the 500 the
            # caller is already getting. So: repair the connection for whoever
            # comes next, and let THIS request fail honestly.
            if _is_connection_error(exc):
                if self._in_tx:
                    # Doomed either way, so fail honestly and repair later. A
                    # reconnect here would hand the caller's COMMIT a fresh
                    # connection with nothing in it.
                    self._needs_reconnect = True
                else:
                    try:
                        self._reconnect()
                    except Exception:
                        pass          # next request tries again
            raise
        if verb.startswith("BEGIN"):
            self._in_tx = True
        elif verb.startswith("COMMIT") or verb.startswith("ROLLBACK"):
            self._in_tx = False
        return _PgCursor(cur)

    def executescript(self, sql: str) -> None:
        if getattr(self._raw, "closed", False):
            self._reconnect()
        with self._raw.cursor() as cur:
            cur.execute(sql)


class _PgCursor:
    """Wraps a psycopg cursor (dict-row factory) with the bit of sqlite3.Cursor's
    surface this module actually uses: fetchone/fetchall/rowcount/iteration."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount


def connect(path_or_dsn: str):
    """SQLite for a file path, Postgres for a ``postgres(ql)://`` DSN.

    The caller (``CASEBROKER_DB`` in practice) decides the engine purely by what
    string it passes; nothing else in this module, or above it, branches on
    which one it got except the handful of statements in this file that
    genuinely differ between the two.
    """
    if path_or_dsn.startswith(("postgres://", "postgresql://")):
        return _connect_postgres(path_or_dsn)

    # check_same_thread=False because the connection is shared across the
    # threadpool; _LOCK is what makes that safe.
    conn = sqlite3.connect(path_or_dsn, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _LOCK:
        apply_schema(conn, SCHEMA, is_pg=False)
    return conn


def _connect_postgres(dsn: str) -> PgConnection:
    import psycopg
    from psycopg.rows import dict_row

    # autocommit=True mirrors SQLite's isolation_level=None: every statement runs
    # standalone until an explicit BEGIN starts a transaction that a later
    # COMMIT/ROLLBACK closes, which is exactly the pattern every function below
    # already uses. dict_row makes a fetched row support row["col"], matching
    # sqlite3.Row.
    #
    # prepare_threshold=None turns off psycopg's automatic server-side prepared
    # statements. Measured against Supabase's pooler (port 6543, PgBouncer in
    # TRANSACTION mode): after ~5 uses of the same query text psycopg names and
    # prepares it server-side, but a transactional pooler can route the NEXT
    # transaction to a different backend connection than the one that prepared
    # it -- and to one where a DIFFERENT client's session already used that same
    # generated name for something else, which raised
    # "prepared statement \"_pg3_0\" already exists" here on exactly the sixth
    # call. Every statement in this module is either a one-off or cheap enough
    # that losing server-side preparation costs nothing worth trading for a
    # connection that is correct on a transactional pooler.
    # sslmode=require unless the DSN already says otherwise. libpq defaults to
    # "prefer", which tries TLS and silently FALLS BACK to plaintext -- and once
    # Supabase had SSL enforcement switched on, that fallback was rejected with
    #
    #     FATAL: (ESSLREQUIRED) SSL connection is required for user: postgres
    #
    # then the repeated rejections tripped the pooler's own defence:
    #
    #     FATAL: (ECIRCUITBREAKER) too many authentication failures
    #
    # which looked like a rate limit and was really a misconfiguration. Requiring
    # TLS is also simply correct here: the credential and every case spec cross a
    # public network, and "prefer" means an attacker who can break the TLS
    # handshake gets a plaintext session instead of a failure.
    kwargs = {"autocommit": True, "row_factory": dict_row, "prepare_threshold": None}
    if "sslmode=" not in dsn:
        kwargs["sslmode"] = "require"
    raw = psycopg.connect(dsn, **kwargs)
    wrapped = PgConnection(raw, dsn)
    with _LOCK:
        apply_schema(wrapped, PG_SCHEMA, is_pg=True)
    return wrapped


def _now() -> int:
    return int(time.time())


def _event(conn, case_id, worker_id, event, detail=None, now=None) -> None:
    conn.execute(
        "INSERT INTO events(ts, case_id, worker_id, event, detail) VALUES (?,?,?,?,?)",
        (now or _now(), case_id, worker_id, event, detail),
    )


# -- ingest -------------------------------------------------------------------

def _locked(fn):
    """Serialise a public entry point against the shared connection."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with _LOCK:
            return fn(*a, **kw)
    return wrapper


@_locked
def add_cases(conn, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Append cases. Idempotent by case_id, so re-adding an existing case is a
    no-op -- which is what makes "extend the dataset by 10,000" a safe, repeatable
    command rather than a one-shot migration you must not run twice."""
    now = _now()
    added = skipped = 0
    columns = (" (case_id, spec, recipe, city_cluster, lcz, split, priority,"
              "  max_attempts, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    # Same idempotent-insert intent, two dialects: SQLite's OR IGNORE clause sits
    # on INSERT itself, Postgres's sits after the VALUES list as ON CONFLICT.
    insert_sql = (
        "INSERT INTO cases" + columns + " ON CONFLICT (case_id) DO NOTHING"
        if isinstance(conn, PgConnection)
        else "INSERT OR IGNORE INTO cases" + columns
    )
    labelled = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            cur = conn.execute(
                insert_sql,
                (r["case_id"], json.dumps(r["spec"], sort_keys=True), r["recipe"],
                 r["city_cluster"], r.get("lcz"), r["split"], r.get("priority", 100),
                 r.get("max_attempts", 3), now, now),
            )
            if cur.rowcount:
                added += 1
                _event(conn, r["case_id"], None, "created", r["recipe"], now)
            else:
                skipped += 1
            # Labels are rewritten for an EXISTING case too: "post the list
            # again, with labels" is then a way to label a campaign after the
            # fact, without a second endpoint.
            if r.get("labels"):
                conn.execute("DELETE FROM case_labels WHERE case_id = ?", (r["case_id"],))
                for key, value in r["labels"].items():
                    conn.execute("INSERT INTO case_labels(case_id, key, value) VALUES (?,?,?)",
                                 (r["case_id"], str(key), str(value)))
                labelled += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"added": added, "skipped": skipped, "labelled": labelled}


def _attach_labels(conn, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold each case's labels onto its row, in one query for the page."""
    if not rows:
        return rows
    ids = [r["case_id"] for r in rows]
    by_case: dict[str, dict[str, str]] = {i: {} for i in ids}
    for chunk in range(0, len(ids), 200):
        part = ids[chunk:chunk + 200]
        for r in conn.execute(
                "SELECT case_id, key, value FROM case_labels WHERE case_id IN ("
                + ",".join("?" for _ in part) + ")", part).fetchall():
            by_case[r["case_id"]][r["key"]] = r["value"]
    for r in rows:
        r["labels"] = by_case.get(r["case_id"], {})
    return rows


# -- lease / report -----------------------------------------------------------

@_locked
def lease(conn, worker_id: str, count: int = 1,
          lease_seconds: int = 3600, splits: list[str] | None = None,
          now: int | None = None, host: str | None = None,
          cluster: str | None = None,
          resume_case_ids: list[str] | None = None,
          build: str | None = None, version: str | None = None,
          platform: str | None = None,
          recipes: list[str] | None = None) -> list[Lease]:
    """Atomically claim up to ``count`` cases.

    ``recipes`` are the exact recipes this worker can produce. When it declares
    any, it is handed only those: a recipe is the contract a training set is
    partitioned by, and a node that does not know one must never be given it (the
    alternative was measured -- recipe selection by prefix solved a v4 case as v3
    and archived it labelled v4). A worker that declares NOTHING predates
    declarations and is left unfiltered, which is what `require_build` exists to
    fence off when a campaign needs it.

    A DRAINING worker gets no new case, and may still resume its own: that is how
    a node restarts onto a new build in the middle of a case without losing it.

    Expired leases are reclaimed by the same statement that hands out fresh work,
    so a crashed or preempted worker's cases re-enter the pool with no reaper
    process and no operator action.

    ``resume_case_ids`` are cases this worker holds a local checkpoint for. They
    are claimed FIRST, ahead of the priority order, and one still leased to this
    same ``worker_id`` is handed straight back without spending an attempt: a
    worker that restarted (walltime, preemption, Ctrl-C) is continuing its own
    work, not retrying a failure. The old lease_id is superseded, so a zombie of
    the previous process gets 409 on its next heartbeat exactly as before. A case
    is never resumed by a DIFFERENT worker -- the checkpoint is on that machine's
    local disk -- so a foreign id in the list simply does not match and the case
    stays where it is.
    """
    now = now or _now()
    expires = now + lease_seconds
    out: list[Lease] = []
    is_pg = isinstance(conn, PgConnection)
    # SQLite already has the whole database exclusively locked by BEGIN
    # IMMEDIATE below, so no per-row locking clause is needed or valid there.
    # Postgres instead locks only the rows this call is about to claim, and
    # SKIPS any row a concurrent lease() or an in-flight heartbeat/complete/
    # fail/release already holds (see _by_lease) rather than blocking on it --
    # which is the entire point of moving off one file that serialises
    # everything to begin with.
    lock_clause = " FOR UPDATE SKIP LOCKED" if is_pg else ""
    recipe_sql, recipe_params = "", []
    if recipes:
        recipe_sql = " AND recipe IN (" + ",".join("?" for _ in recipes) + ")"
        recipe_params = list(recipes)

    def claim(rows, resumed: bool) -> None:
        for row in rows:
            own = resumed and row["state"] == "leased" and row["lease_worker"] == worker_id
            attempt = row["attempts"] if own else row["attempts"] + 1
            if attempt > row["max_attempts"]:
                # Poison case: its retries are spent. Park it rather than let it
                # cycle forever through every worker in the fleet.
                conn.execute(
                    "UPDATE cases SET state='quarantined', lease_id=NULL,"
                    " lease_worker=NULL, lease_expires=NULL, leased_at=NULL,"
                    " updated_at=?"
                    " WHERE case_id=?", (now, row["case_id"]))
                # Named after the worker whose attempt ran out -- the previous
                # holder, whose lease expired -- not the one that happened to ask
                # next and found it spent; that one is recorded in the detail.
                _event(conn, row["case_id"], row["lease_worker"] or None, "quarantined",
                       "attempts exhausted (%d); found by %s" % (row["max_attempts"], worker_id), now)
                continue

            lease_id = uuid.uuid4().hex
            # A FRESH claim starts the case over, so the telemetry of whatever
            # attempt came before describes a site build, a mesh and a solve
            # this attempt will not use. Kept, it would outlive the attempt that
            # finishes the case whenever that one reports less -- a node from
            # before telemetry, or one whose report was dropped -- and the
            # dataset would rank a done case by a failed attempt's mesh. A
            # RESUME continues the same work from the same disk, and the node
            # re-reports from it, so what it said before still holds.
            forget = "" if resumed else ", telemetry=NULL"
            conn.execute(
                "UPDATE cases SET state='leased', lease_id=?, lease_worker=?,"
                " lease_expires=?, leased_at=?, attempts=?, updated_at=?" + forget +
                " WHERE case_id=?",
                (lease_id, worker_id, expires, now, attempt, now, row["case_id"]))
            _event(conn, row["case_id"], worker_id, "resumed" if resumed else "leased",
                   "attempt %d" % attempt, now)
            out.append(Lease(case_id=row["case_id"], lease_id=lease_id,
                             expires_at=expires, spec=json.loads(row["spec"]),
                             attempt=attempt))

    conn.execute("BEGIN IMMEDIATE")
    try:
        known = conn.execute(
            "SELECT drain, build, prev_build, build_changed_at, build_flips"
            " FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        draining = bool(known and known["drain"])

        if resume_case_ids:
            ids = list(dict.fromkeys(resume_case_ids))[:count]
            placeholders = ",".join("?" for _ in ids)
            # Draining narrows a resume to the case this worker STILL HOLDS: taking
            # a pending case back would be new work by another name.
            own_only = ("(state = 'leased' AND lease_worker = ?)" if draining else
                        "(state = 'pending'"
                        "      OR (state = 'leased' AND (lease_expires < ?"
                        "          OR (leased_at IS NOT NULL AND leased_at < ?"
                        "              AND " + _LAST_PROGRESS_SQL + " < ?)"
                        "          OR lease_worker = ?)))")
            own_params = ([worker_id] if draining
                          else [now, now - MAX_LEASE_AGE_SECONDS, now - LEASE_STALL_SECONDS,
                                worker_id])
            rows = conn.execute(
                "SELECT case_id, spec, attempts, max_attempts, state, lease_worker"
                " FROM cases WHERE case_id IN (" + placeholders + ")"
                " AND " + own_only + recipe_sql +
                " ORDER BY case_id ASC" + lock_clause,
                [*ids, *own_params, *recipe_params],
            ).fetchall()
            claim(rows, resumed=True)

        remaining = 0 if draining else count - len(out)
        if remaining > 0:
            params: list[Any] = [now, now - MAX_LEASE_AGE_SECONDS, now - LEASE_STALL_SECONDS]
            split_sql = ""
            if splits:
                placeholders = ",".join("?" for _ in splits)
                split_sql = " AND split IN (" + placeholders + ")"
                params.extend(splits)
            params.extend(recipe_params)
            params.append(remaining)
            rows = conn.execute(
                "SELECT case_id, spec, attempts, max_attempts, state, lease_worker FROM cases"
                # A lease is reclaimable when it has EXPIRED (the worker stopped
                # speaking) or when it is simply too OLD (the worker is still
                # speaking and has been for a week). The second is the only one
                # that catches a wedged-but-alive solve, because its heartbeat
                # keeps the first from ever firing.
                " WHERE (state = 'pending'"
                "        OR (state = 'leased' AND (lease_expires < ?"
                "            OR (leased_at IS NOT NULL AND leased_at < ?"
                "                AND " + _LAST_PROGRESS_SQL + " < ?))))"
                + split_sql + recipe_sql +
                # Every worker targets the same "lowest" rows. That is contention by
                # design, not by accident: under SKIP LOCKED a locked row is simply
                # skipped, and case_id is a hash so the tiebreak is effectively random
                # -- deterministic ordering with no hot spot.
                " ORDER BY priority ASC, case_id ASC LIMIT ?" + lock_clause,
                params,
            ).fetchall()
            claim(rows, resumed=False)

        # host/cluster are refreshed on every lease call (not just insert): the
        # same worker_id can in principle move machines across a restart, and a
        # stale "where did this run" answer is worse than a slightly redundant
        # write on every poll.
        # ...and so is the build: it changes under a worker_id every time the node
        # is updated, which is the whole point of recording it.
        conn.execute(
            "INSERT INTO workers(worker_id, host, cluster, first_seen, last_seen,"
            " build, version, platform, recipes)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(worker_id) DO UPDATE SET"
            " last_seen=excluded.last_seen, host=excluded.host, cluster=excluded.cluster,"
            " build=excluded.build, version=excluded.version,"
            " platform=excluded.platform, recipes=excluded.recipes",
            (worker_id, host, cluster, now, now, build, version, platform,
             json.dumps(list(recipes)) if recipes else None))
        if known is not None and (known["build"] or None) != (build or None):
            _note_build_change(conn, worker_id, known, build, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def _count_for_build(conn, build: str | None, worker_id: str | None, now: int,
                     done: int = 0, failed: int = 0, unconverged: int = 0,
                     wall_seconds: int = 0) -> None:
    """Add to a build's tally. Called inside the caller's transaction.

    `build` None means "whatever this worker last said it was" -- a failure has no
    archive to name one. A worker that never declared a build is not counted:
    a row called NULL would only ever be a bucket of everything unknown.
    """
    if not build and worker_id:
        row = conn.execute("SELECT build FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        build = row["build"] if row else None
    if not build:
        return
    conn.execute(
        "INSERT INTO build_stats(build, done, failed, unconverged, wall_seconds,"
        " first_seen, last_seen)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(build) DO UPDATE SET done = build_stats.done + excluded.done,"
        " failed = build_stats.failed + excluded.failed,"
        " unconverged = build_stats.unconverged + excluded.unconverged,"
        " wall_seconds = COALESCE(build_stats.wall_seconds, 0) + excluded.wall_seconds,"
        " last_seen = excluded.last_seen",
        (build, done, failed, unconverged, wall_seconds, now, now))


#: Two changes of build under one worker_id closer together than this, each
#: undoing the one before, are read as two clients sharing the id, not as updates.
ID_CONFLICT_WINDOW = 3600


def _note_build_change(conn, worker_id: str, known, build: str | None, now: int) -> None:
    """The build under a worker_id changed. Once, that is an update, and it goes
    in the audit trail. Flipping back and forth is something else: two clients
    sharing one id -- an E3D node and a Python worker started from the same
    machine.env -- each overwriting the other's build on every poll. The fleet
    table shows whichever wrote last, so the flip itself is what is recorded.
    """
    def name(b):
        return b or "undeclared"
    old = known["build"] or None
    # A flip is a change back to what the build was before the LAST change,
    # within the window. Two flips in a row (A -> B -> A -> B) is the verdict:
    # one alone is a rollback an operator may well have made on purpose.
    flip = ((known["prev_build"] or None) == (build or None)
            and known["build_changed_at"] is not None
            and now - known["build_changed_at"] < ID_CONFLICT_WINDOW)
    flips = (known["build_flips"] or 0) + 1 if flip else 0
    conn.execute(
        "UPDATE workers SET prev_build = ?, build_changed_at = ?, build_flips = ?"
        " WHERE worker_id = ?", (old, now, flips, worker_id))
    _event(conn, None, worker_id, "build-changed", "%s -> %s" % (name(old), name(build)), now)
    if flips >= 2:
        said = "%s and %s" % (name(build), name(old))
        conn.execute(
            "UPDATE workers SET id_conflict = ?, id_conflict_at = ? WHERE worker_id = ?",
            (said, now, worker_id))
        _event(conn, None, worker_id, "shared-id", said, now)


# -- node releases: WHICH build the fleet runs, never the build itself ---------
#
# A campaign runs for months and its nodes are updated while it runs. SLURM's
# answer to the same problem is the model here: the controller is upgraded first
# and says what version it expects; nodes DRAIN rather than die; running work
# keeps the binary it started with; and the controller never ships code. So the
# broker holds a catalog of published builds with their hashes, one fleet target
# (and per-worker overrides, for a canary), and a drain flag -- and the node
# fetches the file from its own release share and verifies it against the hash
# it was given over this authenticated channel.

#: When a node moves to the target build. "case" waits for the case in flight;
#: "direction" stops after the wind direction being solved and resumes the same
#: case on the new build; "now" gives the case back and restarts at once.
APPLY_MODES = ("case", "direction", "now")


def _setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


@_locked
def get_settings(conn) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()}


@_locked
def set_setting(conn, key: str, value: str | None, by: str | None = None,
                now: int | None = None) -> None:
    """Set, or with ``value=None`` clear, one fleet setting. Audited: a change to
    what thousands of machine-hours will run is exactly the event someone asks
    about afterwards."""
    _set_setting(conn, key, value, by, now)


def _set_setting(conn, key: str, value: str | None, by: str | None = None,
                 now: int | None = None) -> None:
    now = now or _now()
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (key, value, now, by))
    _event(conn, None, by, "setting", "%s = %s" % (key, value if value is not None else "(cleared)"), now)


@_locked
def register_release(conn, build: str, platform: str, file: str, sha256: str,
                     notes: str | None = None, by: str | None = None,
                     now: int | None = None) -> None:
    """Record that a build's file for one platform is on the release share.

    Re-registering replaces the row: the operator re-published the file, and the
    hash a node verifies has to be the hash of what is actually there.
    """
    now = now or _now()
    conn.execute(
        "INSERT INTO releases(build, platform, file, sha256, notes, added_at, added_by)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(build, platform) DO UPDATE SET file = excluded.file,"
        " sha256 = excluded.sha256, notes = excluded.notes,"
        " added_at = excluded.added_at, added_by = excluded.added_by",
        (build, platform, file, sha256.lower(), notes, now, by))
    _event(conn, None, by, "release", "%s %s %s" % (build, platform, sha256.lower()[:12]), now)


@_locked
def delete_release(conn, build: str, by: str | None = None, now: int | None = None) -> int:
    now = now or _now()
    n = conn.execute("DELETE FROM releases WHERE build = ?", (build,)).rowcount
    if n:
        _event(conn, None, by, "release", "%s removed" % build, now)
    return n


#: How long a worker may lag its target before it is called stuck, by how it
#: was told to switch: "now" restarts at once, "direction" waits for the wind
#: direction being solved, "case" for the case in flight -- which takes hours.
STUCK_AFTER = {"now": 30 * 60, "direction": 4 * 3600, "case": 12 * 3600}


def _live_workers(conn, now: int):
    """Seen in the last 24 h: the same fleet /v1/status shows."""
    return conn.execute(
        "SELECT worker_id, build, platform, target_build, target_set_at, update_failed,"
        " release_asked_at, last_seen FROM workers WHERE last_seen > ?",
        (now - 86400,)).fetchall()


@_locked
def list_releases(conn, now: int | None = None) -> dict[str, Any]:
    """Everything the release panel draws, in one locked read."""
    now = now or _now()
    settings = {r["key"]: dict(r) for r in conn.execute(
        "SELECT key, value, updated_at FROM settings").fetchall()}

    def value(key, default=None):
        return settings[key]["value"] if key in settings else default

    releases = [dict(r) for r in conn.execute(
        "SELECT build, platform, file, sha256, notes, added_at, added_by FROM releases"
        " ORDER BY added_at DESC, build, platform").fetchall()]
    published: dict[str, set[str]] = {}
    for r in releases:
        published.setdefault(r["build"], set()).add(r["platform"])
    stats = {r["build"]: dict(r) for r in conn.execute("SELECT * FROM build_stats").fetchall()}
    # The earliest date this build shows up anywhere: when a case first
    # completed or failed on it (build_stats.first_seen), or -- for a build
    # with no cases yet -- when it was first published. A build seen only by a
    # worker that has neither finished a case nor been published has no date
    # to show; nothing here invents one.
    first_published: dict[str, int] = {}
    for r in releases:
        if r["build"] not in first_published or r["added_at"] < first_published[r["build"]]:
            first_published[r["build"]] = r["added_at"]
    live = now - 300
    workers = _live_workers(conn, now)
    running: dict[str, dict[str, int]] = {}
    platforms_in_use: set[str] = set()
    for r in workers:
        if r["platform"]:
            platforms_in_use.add(r["platform"])
        if r["build"]:
            b = running.setdefault(r["build"], {"workers": 0, "active": 0})
            b["workers"] += 1
            b["active"] += 1 if r["last_seen"] > live else 0
    target = value("target_build")
    apply = value("target_apply", "case")
    # What each build KNOWS: the recipes its workers declared, ever -- a
    # build's knowledge does not expire with a worker's last_seen -- against
    # what the queue still needs. A target that does not know a queued recipe
    # would have every node on it refuse those cases.
    knows, queue = _recipe_knowledge(conn)
    builds = sorted(set(stats) | set(running) | set(published) | set(knows))

    def row(b):
        s = stats.get(b, {})
        done, failed, unconverged = s.get("done", 0), s.get("failed", 0), s.get("unconverged", 0)
        return {
            "build": b, "done": done, "failed": failed, "unconverged": unconverged,
            **running.get(b, {"workers": 0, "active": 0}),
            "knows": sorted(knows[b]) if b in knows else None,
            "missing_recipes": sorted(r for r in queue if r not in knows[b]) if b in knows else [],
            "published": sorted(published.get(b, ())),
            # Live workers on a platform this build has no file for: told to move,
            # they could not, and nothing said so.
            "missing_platforms": sorted(platforms_in_use - published.get(b, set())),
            # Counts alone do not say "worse than the build before": rates and a
            # mean do, once there are enough cases to mean anything.
            "mean_wall_seconds": (round(s["wall_seconds"] / done)
                                  if done and s.get("wall_seconds") else None),
            "unconverged_rate": round(unconverged / done, 3) if done else None,
            "failed_rate": round(failed / (done + failed), 3) if done + failed else None,
            "first_seen": min(d for d in (s.get("first_seen"), first_published.get(b)) if d is not None)
                          if s.get("first_seen") or b in first_published else None,
        }

    # The fleet, counted the way the panel's summary line reads it.
    summary = {"workers": len(workers), "on_target": 0, "behind": 0, "stuck": 0,
               "undeclared": 0, "update_failed": 0, "cannot_update": 0, "canaries": 0}
    since_fleet = settings["target_build"]["updated_at"] if target else None
    for r in workers:
        want = r["target_build"] or target
        if r["target_build"]:
            summary["canaries"] += 1
        if not r["build"]:
            summary["undeclared"] += 1
        if r["update_failed"]:
            summary["update_failed"] += 1
        if not want or not r["build"]:
            continue
        if want == r["build"]:
            summary["on_target"] += 1
            continue
        summary["behind"] += 1
        if not r["release_asked_at"]:
            summary["cannot_update"] += 1
        since = r["target_set_at"] if r["target_build"] else since_fleet
        if since and now - since > STUCK_AFTER.get(apply, STUCK_AFTER["case"]):
            summary["stuck"] += 1
    return {
        "target_build": target,
        "target_since": since_fleet,
        "target_apply": apply,
        "previous_target": value("previous_target"),
        "require_build": value("require_build") == "1",
        "blocked_builds": json.loads(value("blocked_builds") or "[]"),
        "release_repo": value("release_repo"),
        "stuck_after": STUCK_AFTER,
        "queue_recipes": queue,
        "releases": releases,
        "builds": [row(b) for b in builds],
        "fleet": summary,
    }


def _recipe_knowledge(conn) -> tuple[dict[str, set[str]], dict[str, int]]:
    """Per build, the union of recipes its workers ever declared; and the
    recipes the queue still holds (pending or leased) with their counts."""
    knows: dict[str, set[str]] = {}
    for r in conn.execute(
            "SELECT build, recipes FROM workers WHERE build IS NOT NULL AND recipes IS NOT NULL"
    ).fetchall():
        try:
            names = json.loads(r["recipes"])
        except (TypeError, ValueError):
            continue
        if isinstance(names, list):
            knows.setdefault(r["build"], set()).update(str(n) for n in names)
    queue = {r["recipe"]: r["n"] for r in conn.execute(
        "SELECT recipe, COUNT(*) AS n FROM cases WHERE state IN ('pending', 'leased')"
        " GROUP BY recipe").fetchall()}
    return knows, queue


@_locked
def recipe_gaps(conn, build: str) -> list[dict[str, Any]]:
    """The queued recipes `build` is known NOT to know, with how many cases
    need each; empty when nothing is known about the build (no worker on it
    has declared recipes), which is not the same as knowing them all."""
    knows, queue = _recipe_knowledge(conn)
    if build not in knows:
        return []
    return [{"recipe": r, "cases": n} for r, n in sorted(queue.items()) if r not in knows[build]]


@_locked
def platform_gaps(conn, build: str, worker_id: str | None = None,
                  now: int | None = None) -> list[str]:
    """Platforms of the live workers -- or of ONE worker -- that `build` has no
    file for. A target every node learns and some cannot reach is the silent
    failure the guard on setting one exists for."""
    now = now or _now()
    have = {r["platform"] for r in conn.execute(
        "SELECT platform FROM releases WHERE build = ?", (build,)).fetchall()}
    if worker_id is not None:
        w = conn.execute("SELECT platform FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        used = {w["platform"]} if w and w["platform"] else set()
    else:
        used = {r["platform"] for r in _live_workers(conn, now) if r["platform"]}
    return sorted(used - have)


@_locked
def release_in_use(conn, build: str, now: int | None = None) -> str | None:
    """Why this build's catalog entry has to stay, or None: it is the fleet's
    target, a canary's target, or what a live worker is running right now."""
    now = now or _now()
    if _setting(conn, "target_build") == build:
        return "%s is the fleet's target; move the target first" % build
    canaries = sorted(r["worker_id"] for r in conn.execute(
        "SELECT worker_id FROM workers WHERE target_build = ?", (build,)).fetchall())
    if canaries:
        return "%s is the canary target of %s; clear that first" % (build, ", ".join(canaries))
    running = sorted(r["worker_id"] for r in _live_workers(conn, now) if r["build"] == build)
    if running:
        return "%s is what %s is running; move or drain them first" % (build, ", ".join(running))
    return None


def _set_target(conn, build: str | None, by: str | None, now: int) -> None:
    """Point the fleet at a build and remember where it was: what a roll back
    returns to. Two targets in a row toggle, which is what "back" means."""
    current = _setting(conn, "target_build")
    if build == current:
        return
    if current:
        _set_setting(conn, "previous_target", current, by, now)
    _set_setting(conn, "target_build", build, by, now)


@_locked
def set_target(conn, build: str | None, by: str | None = None, now: int | None = None) -> None:
    _set_target(conn, build, by, now or _now())


@_locked
def promote_canary(conn, worker_id: str, by: str | None = None, now: int | None = None) -> str:
    """The canary's build becomes the fleet's target and its override is
    cleared: the two calls an operator made by hand, as one that cannot be
    left half done."""
    now = now or _now()
    w = conn.execute("SELECT target_build FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
    if w is None:
        raise KeyError(worker_id)
    if not w["target_build"]:
        raise ValueError("%s is not a canary: it follows the fleet" % worker_id)
    build = w["target_build"]
    conn.execute("UPDATE workers SET target_build = NULL, target_set_at = NULL WHERE worker_id = ?",
                 (worker_id,))
    _set_target(conn, build, by, now)
    _event(conn, None, by, "promote", "%s, proven on %s, is the fleet's target" % (build, worker_id), now)
    return build


@_locked
def roll_back(conn, by: str | None = None, block: bool = False, now: int | None = None) -> str:
    """Return the fleet to the build it was on before the current target. With
    `block`, the current one is refused to every node as well: the kill switch,
    for a build that has to stop NOW rather than at each node's next ask."""
    now = now or _now()
    current, previous = _setting(conn, "target_build"), _setting(conn, "previous_target")
    if not previous:
        raise ValueError("nothing to roll back to: no earlier target is remembered")
    if block and current:
        blocked = set(json.loads(_setting(conn, "blocked_builds") or "[]"))
        blocked.add(current)
        _set_setting(conn, "blocked_builds", json.dumps(sorted(blocked)), by, now)
    _set_target(conn, previous, by, now)
    _event(conn, None, by, "rollback",
           "%s -> %s%s" % (current or "(none)", previous, ", blocked" if block else ""), now)
    return previous


@_locked
def set_worker_target(conn, worker_id: str, build: str | None, by: str | None = None,
                      now: int | None = None) -> bool:
    now = now or _now()
    n = conn.execute("UPDATE workers SET target_build = ?, target_set_at = ? WHERE worker_id = ?",
                     (build, now if build else None, worker_id)).rowcount
    if n:
        _event(conn, None, by, "worker-target", "%s -> %s" % (worker_id, build or "(fleet)"), now)
    return bool(n)


@_locked
def set_worker_drain(conn, worker_id: str, drain: bool, reason: str | None = None,
                     by: str | None = None, now: int | None = None) -> bool:
    now = now or _now()
    n = conn.execute("UPDATE workers SET drain = ?, drain_reason = ? WHERE worker_id = ?",
                     (1 if drain else 0, reason if drain else None, worker_id)).rowcount
    if n:
        _event(conn, None, by, "drain" if drain else "undrain",
               "%s%s" % (worker_id, (": " + reason) if drain and reason else ""), now)
    return bool(n)


@_locked
def lease_refusal(conn, build: str | None) -> str | None:
    """Why this build may not lease at all, or None.

    Builds are NAMED, never ordered -- a rollback is just another target -- so the
    fence is two explicit rules rather than a minimum version: a campaign can
    insist on a declared build (which excludes every node from before builds
    existed, the ones that select a recipe by prefix), and can block named builds
    that are known to be bad.
    """
    if not build:
        if _setting(conn, "require_build") == "1":
            return ("this campaign only leases to nodes that declare their build, and this one "
                    "declared none: it predates build identity and must be updated")
        return None
    if build in json.loads(_setting(conn, "blocked_builds") or "[]"):
        return "build %s is blocked for this campaign; update this node" % build
    return None


@_locked
def node_release(conn, worker_id: str, platform: str | None, build: str | None,
                 failed_build: str | None = None, failed_reason: str | None = None,
                 state: str | None = None, now: int | None = None) -> dict[str, Any]:
    """What one node should be running, and whether it may take new work.

    Asked before every lease and during a solve. The answer names a build, the
    file it is on the release share under and the hash that file must have; the
    node does the rest.

    `state` is what the node says about the move it was last told to make --
    waiting for the file, installed and verified, switching after the case --
    kept until it is on target. Without it an operator who set a target saw
    every worker as "behind" and nothing about whether anything was happening.
    """
    now = now or _now()
    w = conn.execute(
        "SELECT drain, drain_reason, target_build, update_failed FROM workers WHERE worker_id = ?",
        (worker_id,)).fetchone()
    canary = bool(w and w["target_build"])
    target = (w["target_build"] if canary else None) or _setting(conn, "target_build")
    if w is not None:
        # A node that rolled itself back says so with every ask; recorded (and
        # audited) once per failure, and forgotten the moment it is on target.
        said = ("%s: %s" % (failed_build, (failed_reason or "could not be started")[:300])
                if failed_build else None)
        if said != w["update_failed"]:
            conn.execute("UPDATE workers SET update_failed = ? WHERE worker_id = ?", (said, worker_id))
            if said:
                _event(conn, None, worker_id, "update-failed", said, now)
        # The ask itself is evidence -- a node that never asks cannot update
        # itself -- and what it says about the move is kept until it is there.
        if not target or target == build:
            conn.execute("UPDATE workers SET release_asked_at = ?, update_state = NULL,"
                         " update_state_at = NULL WHERE worker_id = ?", (now, worker_id))
        elif state:
            conn.execute("UPDATE workers SET release_asked_at = ?, update_state = ?,"
                         " update_state_at = ? WHERE worker_id = ?",
                         (now, state[:200], now, worker_id))
        else:
            conn.execute("UPDATE workers SET release_asked_at = ? WHERE worker_id = ?",
                         (now, worker_id))
    out: dict[str, Any] = {
        "target_build": target, "canary": canary, "current": bool(target) and target == build,
        "apply": _setting(conn, "target_apply", "case"),
        "drain": bool(w and w["drain"]), "drain_reason": w["drain_reason"] if w else None,
        "blocked": bool(build) and build in json.loads(_setting(conn, "blocked_builds") or "[]"),
        "file": None, "sha256": None,
    }
    if target and target != build and platform:
        rel = conn.execute(
            "SELECT file, sha256 FROM releases WHERE build = ? AND platform = ?",
            (target, platform)).fetchone()
        if rel:
            out["file"], out["sha256"] = rel["file"], rel["sha256"]
    return out


def _by_lease(conn, lease_id: str):
    """The row a lease_id currently owns, row-locked under Postgres.

    The lock is what stops a concurrent lease() from reclaiming this exact row
    (via its own FOR UPDATE SKIP LOCKED, which will skip a row this transaction
    holds) for the whole duration of a heartbeat/complete/fail/release call --
    the same guarantee SQLite gets for free from BEGIN IMMEDIATE's whole-database
    lock, here narrowed to the one row that is actually contended.
    """
    sql = "SELECT * FROM cases WHERE lease_id=? AND state='leased'"
    if isinstance(conn, PgConnection):
        sql += " FOR UPDATE"
    return conn.execute(sql, (lease_id,)).fetchone()


def _still_progressing(conn, case_id: str, detail: str | None, now: int) -> bool:
    """Has this case's worker said something NEW within LEASE_STALL_SECONDS --
    counting the heartbeat being handled, whose line is not recorded yet?"""
    previous = conn.execute(
        "SELECT detail, ts FROM events WHERE case_id = ? AND event = 'progress' "
        "ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if detail and (previous is None or previous["detail"] != detail):
        return True
    return previous is not None and previous["ts"] >= now - LEASE_STALL_SECONDS


@_locked
def heartbeat(conn, lease_id: str, lease_seconds: int = 3600,
              detail: str | None = None, now: int | None = None) -> bool:
    """Extend a lease. Returns False when the lease is gone -- the worker must
    then STOP working that case, because someone else may already own it."""
    now = now or _now()
    row = _by_lease(conn, lease_id)
    if row is None:
        return False
    # A heartbeat cannot extend a lease indefinitely. Past MAX_LEASE_AGE_SECONDS
    # the case is released here rather than waiting for some other worker's
    # lease() to notice, so the worker finds out on its very next heartbeat --
    # it already treats a refused heartbeat as "stop, someone else owns this",
    # which is exactly the right behaviour for a solve that has been running for
    # a week. Returning False without releasing would leave it held by a worker
    # that has been told to let go.
    leased_at = row["leased_at"] if "leased_at" in row.keys() else None
    if leased_at is None:
        # A lease handed out before this column existed. Start its clock now
        # rather than leaving it exempt forever: NULL means "unknown", and
        # treating unknown as "not old" would let exactly the cases that predate
        # the cap -- the ones most likely to be stuck -- escape it permanently.
        conn.execute("UPDATE cases SET leased_at=? WHERE lease_id=?", (now, lease_id))
        leased_at = now
    if leased_at < now - MAX_LEASE_AGE_SECONDS and not _still_progressing(conn, row["case_id"], detail, now):
        conn.execute(
            "UPDATE cases SET state='pending', lease_id=NULL, lease_worker=NULL,"
            " lease_expires=NULL, leased_at=NULL, updated_at=? WHERE case_id=?",
            (now, row["case_id"]))
        _event(conn, row["case_id"], row["lease_worker"], "released",
               "abandoned: held %d days without completing, progress unchanged for %d h"
               % (MAX_LEASE_AGE_SECONDS // 86400, LEASE_STALL_SECONDS // 3600), now)
        return False
    conn.execute("UPDATE cases SET lease_expires=?, updated_at=? WHERE lease_id=?",
                 (now + lease_seconds, now, lease_id))
    # The WORKER was heard from too. Only lease, complete and fail used to touch
    # this, and the dashboard calls a worker Offline after 300 s without it -- so
    # a node solving a three-hour case, heartbeating every five minutes exactly as
    # designed, went Offline five minutes in and stayed there until the case
    # finished. The heartbeat IS the liveness signal; nothing else says a long
    # solve is alive.
    conn.execute("UPDATE workers SET last_seen=? WHERE worker_id=?", (now, row["lease_worker"]))
    if detail:
        # Only when it CHANGED. The worker heartbeats every 5 minutes for the
        # whole multi-hour solve, and reports "alive" whenever the runner has
        # not written a progress line yet -- so without this, a six-hour case
        # leaves ~72 identical rows saying nothing the one before it did not,
        # and a 30,000-case campaign carries millions of them for the life of
        # the campaign. A stalled solver dedupes the same way, which is also
        # the honest record: nothing happened.
        #
        # idx_events_case is (case_id, id), so this seeks straight to the case
        # and reads one row backwards rather than scanning.
        #
        # Changed since THIS lease began, not since the case's last line. A
        # re-leased case whose first line repeated the previous attempt's last
        # one -- a node before Eddy3D ae59812f says "solve 0/32 dirs · starting"
        # for hours -- recorded nothing for the whole attempt: the dashboard
        # showed no current stage, and v2-003a9149ad953d85, leased 1.4 h on
        # 2026-09-23, read "line changed 7.1 h ago" off its first attempt.
        previous = conn.execute(
            "SELECT detail FROM events WHERE case_id = ? AND event = 'progress'"
            " AND id > COALESCE((SELECT MAX(id) FROM events WHERE case_id = ?"
            "                    AND event IN ('leased', 'resumed')), 0)"
            " ORDER BY id DESC LIMIT 1", (row["case_id"], row["case_id"])).fetchone()
        if previous is None or previous["detail"] != detail:
            _event(conn, row["case_id"], row["lease_worker"], "progress", detail, now)
    return True


@_locked
def complete(conn, lease_id: str, result_uri: str,
             sha256: str | None = None, nbytes: int | None = None,
             metrics: dict[str, Any] | None = None, now: int | None = None,
             case_id: str | None = None) -> bool:
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None:
            # A complete whose RESPONSE was lost is retried by the worker with the
            # same lease_id -- which the successful first write already nulled, so
            # this lookup misses and the retry would be refused with 409. The
            # worker reads 409 as "another worker took the case" and logs a false
            # failure for work that landed. If a done case carries this exact
            # result_uri the write is already there: report success. A DIFFERENT
            # result for a finished lease still falls through to the refusal.
            # Scoped to the case the worker names, when it names one. Matching
            # on result_uri ALONE meant any done case that happened to carry the
            # same URI answered for this one -- so a runner that derives its URI
            # from anything less unique than the case (a template, a constant, a
            # date) would have retries on case B silently confirmed by case A's
            # row, reporting work as landed that never ran. case_id is optional
            # so a worker built before this still works; without it the old,
            # looser check stands, which is still better than a false 409.
            if case_id:
                dup = conn.execute(
                    "SELECT 1 FROM cases WHERE case_id = ? AND state = 'done'"
                    " AND result_uri = ?", (case_id, result_uri)).fetchone()
            else:
                dup = conn.execute(
                    "SELECT 1 FROM cases WHERE state = 'done' AND result_uri = ?",
                    (result_uri,)).fetchone()
            conn.execute("ROLLBACK")
            return dup is not None
        conn.execute(
            "UPDATE cases SET state='done', result_uri=?, result_sha256=?,"
            " result_bytes=?, metrics=?, lease_id=NULL, lease_worker=NULL,"
            " lease_expires=NULL, leased_at=NULL, last_error=NULL,"
            " updated_at=? WHERE case_id=?",
            (result_uri, sha256, nbytes, json.dumps(metrics or {}, sort_keys=True),
             now, row["case_id"]))
        conn.execute(
            "UPDATE workers SET cases_done = cases_done + 1, last_seen=? WHERE worker_id=?",
            (now, row["lease_worker"]))
        # The build the ARCHIVE names, not the worker's current one: a case can be
        # finished by a node that was updated after it started, and the metrics
        # say which build wrote the result.
        m = metrics or {}
        wall = m.get("wall_seconds")
        if not isinstance(wall, (int, float)) or wall < 0:
            # The worker's own clock when it kept one; the lease's age otherwise.
            wall = (now - row["leased_at"]) if row["leased_at"] else 0
        _count_for_build(conn, m.get("eddy3d_build"), row["lease_worker"], now,
                         done=1, unconverged=1 if m.get("unconverged_count") else 0,
                         wall_seconds=int(wall))
        _event(conn, row["case_id"], row["lease_worker"], "done", result_uri, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


# A worker stopping a case at its OWN clock is not the case failing. Nodes stop a
# case at --case-timeout (24 h on every build before Eddy3D 75549071) and report
# it as a retryable failure; the attempt it cost was charged at lease time and
# nothing gave it back. A cyl-1008/of12-v4 case is ~800 core-hours, 23.7 h on 36
# ranks, so a slower worker -- 4 CPUs, a 4.7M-cell mesh, a shared box -- was
# stopped mid-solve, charged, and three of those quarantined a site that was
# solving fine. Such a stop is refunded when the case's progress line had
# changed within TIMEOUT_REFUND_WINDOW_SECONDS: it was moving, and the time limit
# was the worker's, not the case's. A wedged case's line stops changing, so it is
# still charged and still quarantines. At most TIMEOUT_REFUNDS_MAX per case, so a
# case too big for every worker's limit still ends in quarantine instead of
# looping. The signatures are what each worker sends: the node's NodeWorker
# ("case exceeded 86400s and was stopped") and worker.py ("runner exceeded
# 86400s and was killed").
_TIMEOUT_SIGNATURE = re.compile(r"\b(?:case|runner) exceeded \d+s and was (?:stopped|killed)")
TIMEOUT_REFUND_WINDOW_SECONDS = int(os.environ.get("CASEBROKER_TIMEOUT_REFUND_WINDOW", str(12 * 3600)))
TIMEOUT_REFUNDS_MAX = 3
_TIMEOUT_REFUND_TAG = "stopped at the worker's own time limit while still progressing"

# The node's STEP budget is the same stop one level down, and it charged more
# cases than the case timeout ever did. Every node build before Eddy3D 00dfaba2
# kills any one step -- a direction's solve, one rung of its numerics ladder -- at
# 240 minutes and gives the case up ("not retried; a harder numerics path cannot
# fix it"), and case_000 of a cyl-1008/of12-v4 case, which carries the warm-up,
# does not fit in 240 minutes even on 36 ranks. On 2026-09-23 three of the nine
# leased cases had already been charged for exactly that, one on its last
# attempt, while 5 of the 8 live nodes still ran such a build. Later builds give a
# step 12 h and say why it ran out ("so it RAN the whole time ... The case itself
# is fine"), but that is still the worker's budget, not the case. The engines word
# it "Batch '<bat>'", "Container command" or "WSL command" "timed out after N
# minutes", after the runner's "failed at step <name>:".
_STEP_TIMEOUT = re.compile(r"\bfailed at step \S+: [^\n]*?\btimed out after (\d+) minutes")


def _refund_timeout(conn, case_id: str, error: str, now: int) -> bool:
    """Is this failure a worker's own time limit on a case that was still moving?"""
    step = _STEP_TIMEOUT.search(error or "")
    if step is None and not _TIMEOUT_SIGNATURE.search(error or ""):
        return False
    # A build that reports progress once per direction (all of them before Eddy3D
    # ae59812f) says nothing new for the whole of a long step, so a step that ran
    # its full budget is judged on the line from before it began: the window
    # reaches back by the budget the step was given.
    window = TIMEOUT_REFUND_WINDOW_SECONDS + (int(step.group(1)) * 60 if step else 0)
    last = conn.execute(
        "SELECT ts FROM events WHERE case_id = ? AND event = 'progress' "
        "ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if last is None or last["ts"] < now - window:
        return False
    refunded = conn.execute(
        "SELECT COUNT(*) n FROM events WHERE case_id = ? AND event = 'released' AND detail LIKE ?",
        (case_id, _TIMEOUT_REFUND_TAG + "%")).fetchone()["n"]
    return refunded < TIMEOUT_REFUNDS_MAX


@_locked
def fail(conn, lease_id: str, error: str, retryable: bool = True,
         now: int | None = None) -> bool:
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None:
            conn.execute("ROLLBACK")
            return False
        if retryable and _refund_timeout(conn, row["case_id"], error, now):
            conn.execute(
                "UPDATE cases SET state='pending', lease_id=NULL, lease_worker=NULL,"
                " lease_expires=NULL, leased_at=NULL, last_error=?,"
                " attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END, updated_at=?"
                " WHERE case_id=?", (error[:4000], now, row["case_id"]))
            _event(conn, row["case_id"], row["lease_worker"], "released",
                   (_TIMEOUT_REFUND_TAG + "; attempt refunded: " + error)[:500], now)
            conn.execute("COMMIT")
            return True
        exhausted = row["attempts"] >= row["max_attempts"]
        state = "pending" if (retryable and not exhausted) else "quarantined"
        conn.execute(
            "UPDATE cases SET state=?, lease_id=NULL, lease_worker=NULL,"
            " lease_expires=NULL, leased_at=NULL, last_error=?,"
            " updated_at=? WHERE case_id=?",
            (state, error[:4000], now, row["case_id"]))
        conn.execute(
            "UPDATE workers SET cases_failed = cases_failed + 1, last_seen=? WHERE worker_id=?",
            (now, row["lease_worker"]))
        _count_for_build(conn, None, row["lease_worker"], now, failed=1)
        _event(conn, row["case_id"], row["lease_worker"],
               "failed" if state == "pending" else "quarantined", error[:500], now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


@_locked
def release(conn, lease_id: str, reason: str = "released",
            now: int | None = None) -> bool:
    """Hand a case back untouched, without burning a retry.

    This is the preemption path: a SIGTERM'd worker calls it and the case becomes
    available immediately instead of sitting unavailable until its TTL runs out.
    The attempt is refunded because nothing about the case was wrong.
    """
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None:
            conn.execute("ROLLBACK")
            return False
        # CASE, not MAX(attempts - 1, 0): SQLite has a two-argument scalar MAX
        # and Postgres does not ("function max(integer, integer) does not
        # exist"), so on Postgres every release of a live lease answered 500
        # and the case sat leased until its TTL ran out.
        conn.execute(
            "UPDATE cases SET state='pending', lease_id=NULL, lease_worker=NULL,"
            " lease_expires=NULL, leased_at=NULL,"
            " attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END, updated_at=?"
            " WHERE case_id=?", (now, row["case_id"]))
        _event(conn, row["case_id"], row["lease_worker"], "released", reason, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


# -- telemetry: what the node measured while it worked -------------------------
#
# The completion metrics arrive once, at the end, and only for a case that
# finished. Everything a node learns on the way -- the site's urban form the
# moment the geometry exists, the mesh quality the moment checkMesh has spoken,
# where the solve is -- was either lost or squeezed into a one-line progress
# string. Telemetry is the structured channel for it: one small JSON object per
# KIND, the latest one replacing the one before, on the case row itself.

TELEMETRY_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")
# Per post, of the node's `data` AS STORED (see telemetry_size). A kind is a
# summary, not a log: the largest real one (a mesh report over several meshes)
# is ~1 KB.
TELEMETRY_MAX_BYTES = 32 * 1024
# Per case. With the byte limit measured on the stored form, this bounds the
# column at 16 x (32 KiB + the stamps) whatever a node does; the node sends 3.
TELEMETRY_MAX_KINDS = 16
# How deeply `data` may nest objects and arrays, `data` itself being level 1.
# The node's deepest kind is 4 (mesh -> meshes -> <mesh> -> failed_checks).
# The bound is what keeps the case's GET answerable: its response is rendered
# by pydantic-core, which refuses past ~255 levels ("Circular reference
# detected (depth exceeded)"), so a 300-level object -- 700 bytes, far inside
# the size limit -- stored once made that case's record answer 500 for good.
TELEMETRY_MAX_DEPTH = 16

# A UTF-16 half with no other half. Python's JSON parser accepts "\ud83d", and
# a .NET node that cuts a string mid-pair (a truncated path or host name) sends
# exactly that -- but no response containing one can be encoded as UTF-8.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def nests_deeper(value: Any, limit: int) -> bool:
    """Whether `value` holds objects/arrays nested more than `limit` deep.
    Iterative, so the check cannot itself hit the recursion limit on the very
    input it exists to refuse."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _text(s: str) -> str:
    return s if s.isascii() else _LONE_SURROGATE.sub("�", s)


def _clean(value: Any) -> Any:
    """`value` as it can be stored AND served again: every NaN/Infinity
    replaced by None, every lone surrogate (in keys too) by U+FFFD.

    Both are values Python parses happily and the broker could then never
    render: Starlette/pydantic refuse NaN (allow_nan=False) and cannot encode a
    lone surrogate as UTF-8, so either one stored made the case's GET answer
    500 for good. OpenFOAM prints `nan` for the residual of a diverging solve;
    unknown is what that means, so that is what is stored. Callers bound the
    depth first (TELEMETRY_MAX_DEPTH), which is what keeps this recursion
    shallow.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        return {_text(str(k)): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _telemetry_json(value: Any) -> str:
    """The one serialisation telemetry is stored in: compact, sorted keys, and
    ASCII -- every non-ASCII character escaped as \\uXXXX, which is also what
    keeps a NUL a six-character escape Postgres TEXT accepts."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def telemetry_size(data: dict[str, Any]) -> int:
    """How large `data` is for the purpose of TELEMETRY_MAX_BYTES: its size as
    STORED, after cleaning and in the stored serialisation.

    Measuring anything else lets the column outgrow its bound. Counted as UTF-8
    and stored escaped, a 4-byte emoji grew to 12 bytes on disk and a row of 16
    kinds "within the limit" measured 1.5 MB, three times what
    TELEMETRY_MAX_KINDS promises. Measuring the stored form is also why this
    cannot raise on a lone surrogate the way encoding to UTF-8 did.
    `data` must already be within TELEMETRY_MAX_DEPTH (see prepare_telemetry).
    """
    return len(_telemetry_json(_clean(data)))


def prepare_telemetry(kind: Any, data: Any) -> tuple[str, dict[str, Any] | None]:
    """Validate one post and clean its data, touching no database: the API
    runs it BEFORE taking db._LOCK, so an oversized body is refused without
    holding up anyone's heartbeat, and post_telemetry runs it again for
    callers that did not.

    Returns ``("ok", cleaned)``, or an outcome and None: ``"invalid"`` (a kind
    outside TELEMETRY_KIND, or `data` that is not an object), ``"too_deep"``
    (past TELEMETRY_MAX_DEPTH) or ``"too_large"`` (past TELEMETRY_MAX_BYTES).
    """
    if not isinstance(kind, str) or not TELEMETRY_KIND.fullmatch(kind) \
            or not isinstance(data, dict):
        return "invalid", None
    if nests_deeper(data, TELEMETRY_MAX_DEPTH):
        return "too_deep", None
    cleaned = _clean(data)
    if len(_telemetry_json(cleaned)) > TELEMETRY_MAX_BYTES:
        return "too_large", None
    return "ok", cleaned


@_locked
def post_telemetry(conn, lease_id: str, case_id: str, kind: str,
                   data: dict[str, Any], now: int | None = None,
                   worker_ok: Callable[[str | None], bool] | None = None) -> str:
    """Record one kind of telemetry for the case a lease holds.

    ``telemetry[kind] = {**data, "at": now, "worker": <lease_worker>}`` -- the
    new object REPLACES that kind and leaves every other kind alone. ``at`` and
    ``worker`` are stamped here rather than trusted from the body, and win over
    keys of the same name in it: they say when the broker heard it and from
    which lease holder, which a later attempt on another machine must not blur.

    The ownership check is heartbeat's and complete's: the lease must be the
    CURRENT one (``_by_lease``, row-locked under Postgres), and it must be the
    lease of `case_id` -- a node that confused two of its cases must not write
    one's mesh onto the other. `worker_ok`, when given, must also accept the
    lease's worker: the API passes it for a per-machine credential, so the
    `worker` stamp names the machine that actually sent the report (see the
    route).

    Returns ``"ok"``, ``"gone"`` (not this case's current lease, or not the
    caller's: the node stops sending for the case), ``"too_many_kinds"`` (a
    new kind past TELEMETRY_MAX_KINDS), or an outcome of prepare_telemetry.
    Nothing here touches ``updated_at`` or the events trail: telemetry is not a
    state change, and a solve reporting every five minutes must not become the
    case browser's idea of "what just happened".
    """
    outcome, cleaned = prepare_telemetry(kind, data)
    if outcome != "ok":
        return outcome
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None or row["case_id"] != case_id \
                or (worker_ok is not None and not worker_ok(row["lease_worker"])):
            conn.execute("ROLLBACK")
            return "gone"
        try:
            current = json.loads(row["telemetry"] or "{}")
        except (TypeError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        if kind not in current and len(current) >= TELEMETRY_MAX_KINDS:
            conn.execute("ROLLBACK")
            return "too_many_kinds"
        current[kind] = {**cleaned, "at": now, "worker": row["lease_worker"]}
        conn.execute("UPDATE cases SET telemetry=? WHERE case_id=? AND lease_id=?",
                     (_telemetry_json(current), case_id, lease_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "ok"


# How many cases one dataset_rows() page holds: ~8 MB of JSON TEXT at the
# campaign's ~4 KB per row, where reading the whole table at once measured
# +300 MB on Postgres at 30,000 cases (psycopg keeps the libpq result alive
# beside the Python rows) on a 512 MB instance.
DATASET_PAGE_ROWS = 2000


@_locked
def dataset_rows(conn, after: str | None = None,
                 limit: int = DATASET_PAGE_ROWS) -> list[dict[str, Any]]:
    """One page of every case, in case_id order from just past `after`,
    reduced to the columns the dataset statistics read.

    A PAGE, by key, because the whole table at once is the campaign's entire
    JSON in memory at once. Each page is its own short read under the lock;
    casebroker/dataset.py reduces it to numbers and drops it before asking for
    the next, so the peak is one page and db._LOCK is never held while
    counting. A page is found by the primary key, not an OFFSET, so reading
    page 15 costs what reading page 1 does. JSON stays as the TEXT it is stored
    as, for the same reason: parsing is not the lock's business.
    """
    select = ("SELECT case_id, state, split, lcz, recipe, spec, telemetry, metrics"
              " FROM cases")
    if after is None:
        rows = conn.execute(select + " ORDER BY case_id LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute(select + " WHERE case_id > ? ORDER BY case_id LIMIT ?",
                            (after, limit)).fetchall()
    return [dict(r) for r in rows]


@_locked
def cancel_case(conn, case_id: str, by: str | None, reason: str | None = None,
                park: bool = False, now: int | None = None) -> dict[str, Any]:
    """Take a LEASED case off its node, on purpose.

    The node hears at its next heartbeat -- 409, which every worker reads as
    "stop" -- and a result it delivers after that is refused as a duplicate.
    The attempt is refunded, nothing about the case was wrong, and the case goes
    back to the pool; with `park` it goes to quarantine carrying the reason,
    where reopen finds it. This is the one place a live worker's lease is
    released deliberately (see AGENTS.md on why nothing else may), so the trail
    records who, why, and which worker was holding it.

    Raises KeyError for an unknown case, ValueError for one that is not leased.
    """
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT state, lease_worker FROM cases WHERE case_id = ?",
                           (case_id,)).fetchone()
        # Raised inside the try: the one handler below rolls back, once.
        if row is None:
            raise KeyError(case_id)
        if row["state"] != "leased":
            raise ValueError("%s is %s, not leased: nothing to pull it off" % (case_id, row["state"]))
        said = "pulled off %s by %s%s" % (row["lease_worker"], by or "?",
                                          (": " + reason) if reason else "")
        if park:
            conn.execute(
                "UPDATE cases SET state='quarantined', lease_id=NULL, lease_worker=NULL,"
                " lease_expires=NULL, leased_at=NULL, attempts=MAX(attempts - 1, 0),"
                " last_error=?, updated_at=? WHERE case_id=?", (said[:4000], now, case_id))
        else:
            conn.execute(
                "UPDATE cases SET state='pending', lease_id=NULL, lease_worker=NULL,"
                " lease_expires=NULL, leased_at=NULL, attempts=MAX(attempts - 1, 0),"
                " updated_at=? WHERE case_id=?", (now, case_id))
        _event(conn, case_id, row["lease_worker"], "cancelled",
               said + (", parked" if park else ", requeued"), now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"case_id": case_id, "worker_id": row["lease_worker"],
            "state": "quarantined" if park else "pending"}

# -- observability ------------------------------------------------------------

# The site-geometry cache is bounded. It is a CACHE -- three remote reads that
# cost seconds and give the same answer again -- but it grew without limit:
# measured on production, 15 sites took 1.36 MB (~90 KB each, the GeoJSON with
# terrain and canopy grids), so browsing the whole 5,000-case campaign would put
# ~450 MB in a database whose quota is 500 MB, with the cases themselves in it.
# Past this many sites the ones fetched longest ago are dropped; opening one of
# them again costs one re-fetch. 0 turns the bound off.
FOOTPRINT_CACHE_MAX = int(os.environ.get("CASEBROKER_FOOTPRINT_CACHE_MAX", "500"))


@_locked
def put_footprints(conn, case_id: str, geojson: str, n: int, now: int | None = None,
                   cap: int | None = None) -> None:
    now = now or _now()
    cap = FOOTPRINT_CACHE_MAX if cap is None else cap
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not conn.execute(
                "UPDATE footprints SET geojson=?, n=?, fetched_at=? WHERE case_id=?",
                (geojson, n, now, case_id)).rowcount:
            conn.execute("INSERT INTO footprints (case_id, geojson, n, fetched_at)"
                         " VALUES (?, ?, ?, ?)", (case_id, geojson, n, now))
        if cap > 0:
            over = conn.execute("SELECT COUNT(*) n FROM footprints").fetchone()["n"] - cap
            if over > 0:
                # Oldest fetch first; the row just written is the newest, so it
                # is never the one dropped.
                conn.execute("DELETE FROM footprints WHERE case_id IN (SELECT case_id FROM"
                             " footprints WHERE case_id <> ? ORDER BY fetched_at ASC, case_id"
                             " LIMIT ?)", (case_id, over))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@_locked
def get_footprints(conn, case_id: str):
    r = conn.execute("SELECT geojson, n, fetched_at FROM footprints WHERE case_id=?",
                     (case_id,)).fetchone()
    return dict(r) if r else None


@_locked
def report_fleet(conn, cluster: str, queued: int, running: int,
                 detail: str | None = None, now: int | None = None) -> None:
    """Record what a scheduler currently holds for one cluster.

    Upsert on cluster, so a reporter can run on a timer and simply overwrite its
    own last snapshot rather than accumulating history nobody reads.
    """
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        updated = conn.execute(
            "UPDATE fleet SET queued=?, running=?, detail=?, reported_at=? WHERE cluster=?",
            (queued, running, detail, now, cluster)).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO fleet (cluster, queued, running, detail, reported_at)"
                " VALUES (?, ?, ?, ?, ?)", (cluster, queued, running, detail, now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@_locked
def fleet(conn, now: int | None = None) -> list[dict[str, Any]]:
    """Reported scheduler state, each row carrying how old it is.

    ``age_seconds`` is returned rather than left for the caller to compute
    because every consumer needs it: a snapshot nobody has refreshed for an hour
    describes a queue that has almost certainly moved on, and presenting that as
    current is the specific way this feature could mislead.
    """
    now = now or _now()
    return [{**dict(r), "age_seconds": now - r["reported_at"]}
            for r in conn.execute(
                "SELECT * FROM fleet ORDER BY cluster")]


# What each table is for, in the words an operator needs when one of them turns
# out to be the one filling the disk. Keyed by table name; a table this file
# does not list still appears in the answer, just without a note.
_TABLE_NOTES = {
    "cases": "one row per case: spec, state, result pointer, node telemetry",
    "events": "per-case history: every lease, heartbeat progress line, failure",
    "footprints": "cached site geometry for the dashboard preview (GeoJSON)",
    "workers": "one row per worker id ever seen",
    "fleet": "cluster queue snapshots",
    "users": "dashboard accounts",
    "sessions": "dashboard login sessions",
    "worker_tokens": "machine credentials (hashes only)",
    "pairings": "pending browser pairings",
    "releases": "node builds the broker points at",
    "build_stats": "per-build outcome counters",
    "settings": "broker settings",
    "schema_meta": "schema version",
}


@_locked
def storage(conn) -> dict[str, Any]:
    """How much space the database takes, and which tables take it.

    Postgres answers from its own catalog (``pg_total_relation_size`` is the
    table, its TOAST and its indexes together -- what a hosted plan counts).
    SQLite answers from the page counts, and per table from the ``dbstat``
    virtual table when this build of SQLite has it; when it does not, the table
    sizes are null rather than guessed.

    Row counts are exact (``COUNT(*)``): at this campaign's scale that is
    milliseconds, and an estimate that says 0 rows for a table Postgres has not
    analysed yet is exactly the wrong answer on this page.
    """
    tables: dict[str, dict[str, Any]] = {}
    if isinstance(conn, PgConnection):
        engine = "postgres"
        total = conn.execute(
            "SELECT pg_database_size(current_database()) AS n").fetchone()["n"]
        for r in conn.execute(
                "SELECT c.relname AS name, pg_total_relation_size(c.oid) AS total,"
                " pg_relation_size(c.oid) AS heap, pg_indexes_size(c.oid) AS indexes"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE c.relkind = 'r' AND n.nspname = current_schema()").fetchall():
            tables[r["name"]] = {"bytes": int(r["total"]), "data_bytes": int(r["heap"]),
                                 "index_bytes": int(r["indexes"]),
                                 # TOAST: large values stored out of line -- here,
                                 # the footprint GeoJSON blobs, which is why it is
                                 # reported rather than folded into "data".
                                 "toast_bytes": int(r["total"]) - int(r["heap"]) - int(r["indexes"])}
        files = None
    else:
        engine = "sqlite"
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        total = page * pages
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'").fetchall()]
        for name in names:
            tables[name] = {"bytes": None, "data_bytes": None, "index_bytes": None, "toast_bytes": None}
        try:
            owner = {r[0]: r[1] for r in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')").fetchall()}
            for name, size in conn.execute(
                    "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall():
                table = owner.get(name, name)
                if table not in tables:
                    continue
                t = tables[table]
                key = "data_bytes" if name == table else "index_bytes"
                t[key] = (t[key] or 0) + int(size)
                t["bytes"] = (t["bytes"] or 0) + int(size)
                t["toast_bytes"] = 0
        except sqlite3.OperationalError:
            pass                                  # no dbstat in this SQLite build
        path = next((r[2] for r in conn.execute("PRAGMA database_list").fetchall()
                     if r[1] == "main"), "")
        files = {"free_bytes": page * free}
        for suffix in ("", "-wal"):
            try:
                files["db_file_bytes" if not suffix else "wal_bytes"] = os.path.getsize(path + suffix)
            except OSError:
                pass
    for name, t in tables.items():
        t["rows"] = conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"] \
            if isinstance(conn, PgConnection) else conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        t["note"] = _TABLE_NOTES.get(name)
    ordered = sorted(tables.items(), key=lambda kv: -(kv[1]["bytes"] or 0))
    cases = tables.get("cases", {}).get("rows") or 0
    known = [t["bytes"] for t in tables.values()]
    table_bytes = sum(known) if known and all(b is not None for b in known) else None
    # Per case from the TABLES, not the database total: an empty Postgres is
    # already ~8 MB of system catalogs, which would make 50 cases look like
    # 160 KB each. The difference is reported on its own as overhead.
    return {"engine": engine, "total_bytes": int(total), "table_bytes": table_bytes,
            "overhead_bytes": int(total) - table_bytes if table_bytes is not None else None,
            "bytes_per_case": round(table_bytes / cases) if cases and table_bytes is not None else None,
            "cases": cases, "tables": [{"name": k, **v} for k, v in ordered], "files": files}



_PAIR = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")


def _solve_fraction(line: str | None) -> float | None:
    """How far through its SOLVE a node's progress line says the case is, 0..1.

    The node's grammar (Eddy3D ``NodeProgress.Solve``): ``solve 3/8 dirs · iter
    412/2000`` -- directions FINISHED, then the iteration within the current
    one. Anything that is not a solve line (meshing, archiving, a free-text
    line) is None: those phases are not what an ETA extrapolates.
    """
    if not line:
        return None
    head, _, rest = line.partition("\u00b7")
    if not head.strip().lower().startswith("solve"):
        return None
    outer = _PAIR.search(head)
    if not outer:
        return None
    done, total = float(outer.group(1)), float(outer.group(2))
    if total <= 0:
        return None
    inner = _PAIR.search(rest)
    part = 0.0
    if inner and float(inner.group(2)) > 0:
        part = min(1.0, float(inner.group(1)) / float(inner.group(2)))
    return max(0.0, min(1.0, (done + part) / total))


@_locked
def _solve_eta(conn, case_id: str, since: int | None) -> dict[str, Any] | None:
    """When the solve of the case a worker holds should end, from its own pace.

    The rate is measured across THIS lease's solve lines only -- first to latest
    -- so meshing time, and an earlier attempt on another machine, do not skew
    it. Needs two readings at least ten minutes apart that moved: before that
    the answer is None rather than a number from one data point. It is an
    UPPER estimate by construction: a direction that meets its tolerances stops
    before its iteration cap, which the rate cannot see coming.
    """
    rows = conn.execute(
        "SELECT ts, detail FROM events WHERE case_id=? AND event='progress' AND ts >= ?"
        " ORDER BY id LIMIT 5000", (case_id, since or 0)).fetchall()
    points = [(r["ts"], f) for r in rows if (f := _solve_fraction(r["detail"])) is not None]
    if len(points) < 2:
        return None
    (t0, f0), (t1, f1) = points[0], points[-1]
    if t1 - t0 < 600 or f1 <= f0:
        return None
    rate = (f1 - f0) / (t1 - t0)
    return {"at": int(t1 + (1.0 - f1) / rate), "fraction": round(f1, 4),
            "measured_over_s": int(t1 - t0)}


@_locked
def status(conn, now: int | None = None) -> dict[str, Any]:
    now = now or _now()
    by_state = {r["state"]: r["n"] for r in
                conn.execute("SELECT state, COUNT(*) n FROM cases GROUP BY state")}
    by_split = {r["split"] + "/" + r["state"]: r["n"] for r in
                conn.execute("SELECT split, state, COUNT(*) n FROM cases GROUP BY split, state")}
    stale = conn.execute(
        "SELECT COUNT(*) n FROM cases WHERE state='leased' AND lease_expires < ?",
        (now,)).fetchone()["n"]
    done_24h = conn.execute(
        "SELECT COUNT(*) n FROM events WHERE event='done' AND ts > ?",
        (now - 86400,)).fetchone()["n"]
    remaining = by_state.get("pending", 0) + by_state.get("leased", 0)
    workers = [dict(r) for r in conn.execute(
            "SELECT w.*,"
            " (SELECT c.case_id FROM cases c WHERE c.lease_worker = w.worker_id"
            "    AND c.state = 'leased' ORDER BY c.leased_at DESC LIMIT 1) AS current_case,"
            " (SELECT c.leased_at FROM cases c WHERE c.lease_worker = w.worker_id"
            "    AND c.state = 'leased' ORDER BY c.leased_at DESC LIMIT 1) AS current_leased_at,"
            " (SELECT e.detail FROM events e WHERE e.event = 'progress'"
            "    AND e.case_id = (SELECT c.case_id FROM cases c WHERE c.lease_worker = w.worker_id"
            "                       AND c.state = 'leased' ORDER BY c.leased_at DESC LIMIT 1)"
            "    ORDER BY e.id DESC LIMIT 1) AS current_progress,"
            # ...and WHEN it said so. The case row folds both detail and ts; this
            # query folded only the detail, so the workers table drew a moving
            # progress bar with nothing beside it to say the line was hours old.
            " (SELECT e.ts FROM events e WHERE e.event = 'progress'"
            "    AND e.case_id = (SELECT c.case_id FROM cases c WHERE c.lease_worker = w.worker_id"
            "                       AND c.state = 'leased' ORDER BY c.leased_at DESC LIMIT 1)"
            "    ORDER BY e.id DESC LIMIT 1) AS current_progress_at"
            " FROM workers w WHERE w.last_seen > ? ORDER BY w.last_seen DESC LIMIT 500",
            (_now() - 86400,))]
    for w in workers:
        w["current_eta"] = _solve_eta(conn, w["current_case"], w["current_leased_at"]) \
            if w.get("current_case") else None
    return {
        "fleet": fleet(conn, now),
        "by_state": by_state,
        "by_split": by_split,
        "expired_leases": stale,
        "done_last_24h": done_24h,
        "remaining": remaining,
        # None, not a fabricated infinity: with no completions the rate is unknown.
        "eta_days": round(remaining / done_24h, 1) if done_24h else None,
        # Seen in the last 24h, not "the 50 most recent": a Phoenix pool alone can
        # exceed 50, and a count cap silently aged live workers off the dashboard.
        # Plus what each one is holding right now. The workers table itself has no
        # in-flight state, so a fleet view could say how many a worker had finished
        # and never what it was doing -- which is the question asked of it while a
        # campaign is running. Two correlated subqueries rather than a join: a
        # worker with no case must still appear, and the progress line is the same
        # one _CASE_COLS folds onto a case.
        "workers": workers,
    }


#: What the case browser may sort by, and the expression each name means. An
#: ALLOWLIST because the value reaches an ORDER BY: a column name is not data,
#: and there is no placeholder for one.
#:
#: Sorting belongs on the server here even though the table is rendered on the
#: client, because the client only ever holds ONE PAGE. Sorting 50 rows of 40,000
#: would reorder what is on screen and call it "sorted by attempts", which is a
#: more convincing wrong answer than no sorting at all.
CASE_SORTS = {
    "case_id": "cases.case_id",
    "state": "cases.state",
    "split": "cases.split",
    "lcz": "cases.lcz",
    "city_cluster": "cases.city_cluster",
    "recipe": "cases.recipe",
    "attempts": "cases.attempts",
    "updated_at": "cases.updated_at",
    # The two folded-on columns. Ordering by the progress TEXT is what groups
    # "everything still meshing" together, which is the question the column is
    # there to answer.
    "last_progress": "last_progress",
    "last_error": "cases.last_error",
}


@_locked
def list_cases(conn, state: str | None = None, split: str | None = None,
               city_cluster: str | None = None, limit: int = 50,
               offset: int = 0, sort: str | None = None,
               direction: str = "desc", include_spec: bool = True,
               label: str | None = None) -> dict[str, Any]:
    """A page of cases for the dashboard's case browser, most-recently-touched
    first -- that ordering is what makes "what just happened" the default view
    rather than an arbitrary slice of a 40,000-row table.

    Returns both the page and the total matching count, so a client can render
    "N of M" and page controls without a second round trip.

    ``sort`` names one of ``CASE_SORTS``; anything else falls back to the default
    rather than raising, because a stale bookmark must not break the browser. The
    default ordering keeps using ``idx_cases_updated`` -- every other sort pays
    for a sort of the filtered set, which is the honest cost of asking for one.
    ``case_id`` breaks every tie, so paging through a sorted list cannot show the
    same row twice or skip one.

    ``include_spec`` drops the ``spec`` column, which is the largest thing on a
    case row and which a LIST of cases has no use for -- measured on a 4,000-case
    campaign, a 50-row page is 61.6 KB with it and 25.0 KB without. It defaults
    to keeping it because the endpoint is public API and a script reading specs
    out of a page must not silently stop getting them; the dashboard, which reads
    a spec only for the one row it expands (and fetches that row in full), asks
    for it to be dropped.
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, params = [], []
    if state:
        where.append("state = ?"); params.append(state)
    if split:
        where.append("split = ?"); params.append(split)
    if city_cluster:
        where.append("city_cluster = ?"); params.append(city_cluster)
    if label:
        # "key:value" is one label; "key" alone is every case carrying the key.
        key, _, value = label.partition(":")
        if value:
            where.append("cases.case_id IN (SELECT case_id FROM case_labels WHERE key = ? AND value = ?)")
            params.extend([key.strip(), value.strip()])
        else:
            where.append("cases.case_id IN (SELECT case_id FROM case_labels WHERE key = ?)")
            params.append(key.strip())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) n FROM cases" + clause, params).fetchone()["n"]
    column = CASE_SORTS.get(sort or "", "cases.updated_at")
    descending = str(direction).lower() != "asc"
    order = f"{column} {'DESC' if descending else 'ASC'}, cases.case_id ASC"
    columns = _CASE_LIST_COLS if include_spec else _CASE_COLS_NO_SPEC
    rows = conn.execute(
        "SELECT " + columns + " FROM cases" + clause +
        " ORDER BY " + order + " LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    return {"cases": _attach_labels(conn, [dict(r) for r in rows]), "total": total,
            "limit": limit, "offset": offset}


# A case row plus the newest thing its worker said about it. Workers ship a
# one-line progress summary ("case_270 iter 412 p=3.2e-05 ...") as the
# heartbeat's `detail`, which lands in `events` -- a correlated subquery folds
# the latest one back onto the case so the dashboard can show where a solve is
# without a second endpoint or an events API. Two columns: what was said, and
# when, so a stale line reads as stale.
# Every column of `cases` except `spec` and `telemetry`, spelled out: "cases.*"
# cannot subtract one, and a page of specs is most of the bytes the case browser
# transfers. Listed rather than derived, so a column added to the schema is a
# deliberate decision here too -- a reader that silently gained a field would be
# the same accident in the other direction.
#
# `telemetry` is never on a PAGE, with or without the spec: it is up to 16 kinds
# per case, the solve's per-direction table among them, and the case browser
# fetches the one case it opens in full (get_case, which keeps `cases.*`).
_CASE_COLS_WITHOUT_SPEC = (
    "cases.case_id, cases.recipe, cases.split, cases.lcz, cases.city_cluster,"
    " cases.priority, cases.state, cases.attempts, cases.max_attempts,"
    " cases.lease_id, cases.lease_worker, cases.leased_at, cases.lease_expires,"
    " cases.result_uri, cases.result_sha256, cases.result_bytes, cases.metrics,"
    " cases.last_error, cases.created_at, cases.updated_at"
)

_CASE_COLS = (
    "cases.*,"
    " (SELECT e.detail FROM events e WHERE e.case_id = cases.case_id"
    "   AND e.event = 'progress' ORDER BY e.id DESC LIMIT 1) AS last_progress,"
    " (SELECT e.ts FROM events e WHERE e.case_id = cases.case_id"
    "   AND e.event = 'progress' ORDER BY e.id DESC LIMIT 1) AS last_progress_at,"
    # Where the case is running RIGHT NOW. `cases.metrics` carries host/cluster
    # too, but only complete() writes it, so for a leased case -- the one anybody
    # opens the telemetry card to look at -- it is empty. The worker said where
    # it was when it registered, so the answer is one join away.
    #
    # It is the worker's CURRENT registration, not a snapshot taken when this
    # lease started: workers.host is overwritten on every lease call (see the
    # upsert in register()), so a worker that moved hosts between claiming this
    # case and now reports the new one. The window is small (a lease is released
    # or expires before the worker takes another case) and the alternative is a
    # per-lease copy of a field that is almost always identical; the recorded
    # metrics remain the authority once the case finishes, and the panel prefers
    # them.
    " (SELECT w.host FROM workers w WHERE w.worker_id = cases.lease_worker) AS worker_host,"
    " (SELECT w.cluster FROM workers w WHERE w.worker_id = cases.lease_worker) AS worker_cluster,"
    # Who last HELD it, which on a failed case is the one thing the row cannot
    # say for itself: fail() nulls lease_worker in the same statement that writes
    # last_error, so the card goes blank about the worker on exactly the cases
    # where somebody wants to know which machine to go and look at. The events
    # trail kept it -- _event() stamps worker_id on every lease, heartbeat,
    # failure and completion.
    " (SELECT e.worker_id FROM events e WHERE e.case_id = cases.case_id"
    "   AND e.worker_id IS NOT NULL ORDER BY e.id DESC LIMIT 1) AS last_worker"
)

_CASE_COLS_NO_SPEC = _CASE_COLS.replace("cases.*", _CASE_COLS_WITHOUT_SPEC, 1)
_CASE_LIST_COLS = _CASE_COLS.replace("cases.*", _CASE_COLS_WITHOUT_SPEC + ", cases.spec", 1)


@_locked
def ping(conn) -> None:
    """Cheapest possible "is the database answering".

    Exists so `/healthz` never reaches for the raw connection. It used to run
    `conn.execute("select 1")` inline, which put an UNAUTHENTICATED endpoint --
    polled by the platform's health check every 30 seconds -- on the shared
    connection with no lock, alongside whatever transaction a `lease()` had open
    at that moment.
    """
    conn.execute("SELECT 1").fetchone()


@_locked
def list_errors(conn, limit: int = 2000) -> dict[str, Any]:
    """Every case that currently carries an error, with each failed attempt.

    For pasting into a bug report, so it is one flat answer rather than a page:
    ``last_error`` in full (the case list truncates nothing but shows one case at
    a time), plus the per-attempt history from ``events`` -- a case that failed
    three different ways on three machines is a different bug from one that
    failed the same way three times, and ``last_error`` alone cannot tell them
    apart. Worst first: quarantined cases, then by recency.
    """
    limit = max(1, min(int(limit), 5000))
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM cases WHERE last_error IS NOT NULL").fetchone()["n"]
    rows = conn.execute(
        "SELECT case_id, state, attempts, max_attempts, spec, recipe, city_cluster,"
        " last_error, updated_at FROM cases WHERE last_error IS NOT NULL"
        " ORDER BY (state = 'quarantined') DESC, updated_at DESC LIMIT ?",
        (limit,)).fetchall()
    cases = [dict(r) for r in rows]
    by_id = {c["case_id"]: c for c in cases}
    for c in cases:
        # Only the coordinates: a site that fails is usually a PLACE that fails,
        # and the rest of the spec would multiply the paste for nothing.
        try:
            spec = json.loads(c.pop("spec") or "{}")
        except ValueError:
            spec = {}
        c["lat"], c["lon"] = spec.get("lat"), spec.get("lon")
        c["history"] = []
    # One query for all histories, not one per case: this runs under _LOCK, and a
    # campaign-wide failure is exactly when there are thousands of rows here.
    ids = list(by_id)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for e in conn.execute(
                "SELECT e.case_id, e.ts, e.event, e.worker_id, e.detail, w.host, w.cluster"
                " FROM events e LEFT JOIN workers w ON w.worker_id = e.worker_id"
                f" WHERE e.event IN ('failed','quarantined') AND e.case_id IN ({marks})"
                " ORDER BY e.id", chunk).fetchall():
            d = dict(e)
            by_id[d.pop("case_id")]["history"].append(d)
    return {"total": total, "returned": len(cases), "truncated": total > len(cases),
            "cases": cases}


@_locked
def get_case(conn, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT " + _CASE_COLS + " FROM cases WHERE case_id=?",
                       (case_id,)).fetchone()
    if row is None:
        return None
    out = _attach_labels(conn, [dict(row)])[0]
    # The stages, read off the trail: how long each took, which one a failure
    # landed in, and where a running case is now. One case at a time -- a page
    # of cases carries the last line only.
    from . import stages as _stages
    trail = [dict(e) for e in conn.execute(
        "SELECT ts, event, detail FROM events WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()]
    out.update(_stages.from_events(trail, _now()))
    # Same estimate the Workers table shows for this case's own lease -- the
    # detail card had the age of the last line but not when the solve should
    # end, which is the more useful of the two once a direction is a day in.
    out["eta"] = _solve_eta(conn, case_id, row["leased_at"]) if row["state"] == "leased" else None
    return out


@_locked
def purge_cases(conn, recipe: str | None = None, state: str | None = None,
                expect: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Delete cases (and their events and footprints) from the campaign.

    This is the one destructive operation in the API, and it exists because the
    alternative people reach for is a psql session against production. Three
    interlocks, in order of how much they have saved:

    * ``expect`` -- the caller states how many rows it believes it is deleting,
      and a mismatch aborts before anything is touched. A filter that is subtly
      wrong (a recipe that was renamed, a state spelled ``done`` when the column
      says ``completed``) then fails loudly instead of deleting the campaign.
    * ``dry_run`` -- returns the same counts having changed nothing, so the
      number can be checked before it is committed to.
    * one transaction -- events and footprints go with their cases, or nothing
      does. Orphan events would otherwise outlive the cases they describe and
      corrupt every later count.

    Deleting a case does NOT delete whatever a worker already wrote to disk;
    result_uri points at an archive on the machine that produced it. That is
    deliberate -- the broker tracks work, it does not own the results -- but it
    means a purge silently orphans archives, so the caller is told how many of
    the doomed rows carry one.
    """
    where, params = [], []
    if recipe is not None:
        where.append("recipe = ?"); params.append(recipe)
    if state is not None:
        where.append("state = ?"); params.append(state)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    n = conn.execute("SELECT COUNT(*) n FROM cases" + clause, params).fetchone()["n"]
    with_results = conn.execute(
        "SELECT COUNT(*) n FROM cases" + (clause + " AND " if clause else " WHERE ")
        + "result_uri IS NOT NULL", params).fetchone()["n"]
    out = {"matched": int(n), "with_results": int(with_results),
           "deleted": 0, "dry_run": bool(dry_run)}

    if expect is not None and int(expect) != int(n):
        out["error"] = (f"expected {int(expect)} matching cases, found {int(n)} -- "
                        "refusing to delete")
        return out
    if dry_run or n == 0:
        return out

    conn.execute("BEGIN IMMEDIATE")
    try:
        sub = "SELECT case_id FROM cases" + clause
        conn.execute(f"DELETE FROM events WHERE case_id IN ({sub})", params)
        conn.execute(f"DELETE FROM footprints WHERE case_id IN ({sub})", params)
        conn.execute(f"DELETE FROM case_labels WHERE case_id IN ({sub})", params)
        cur = conn.execute("DELETE FROM cases" + clause, params)
        out["deleted"] = int(cur.rowcount or 0)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


# -- identity: humans with sessions, machines with tokens ---------------------

@_locked
def count_users(conn) -> int:
    """How many accounts exist. Zero is what puts the service into first-run
    setup, so this is the check that decides whether /setup is open."""
    return int(conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"])


@_locked
def create_user(conn, username: str, password_hash: str, role: str = "admin",
                now: int | None = None) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError("unknown role %r; expected one of %s"
                         % (role, ", ".join(ROLES)))
    now = now or _now()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
        (username, password_hash, role, now))
    row = conn.execute(
        "SELECT id, username, role, created_at FROM users WHERE username = ?",
        (username,)).fetchone()
    # By column name, not position: a Postgres row here is a dict (dict_row),
    # while only sqlite3.Row supports both -- positional indexing passed every
    # test against SQLite and raised KeyError on every call in production.
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "created_at": row["created_at"]}


@_locked
def get_user(conn, username: str):
    row = conn.execute(
        "SELECT id, username, password_hash, role FROM users WHERE username = ?",
        (username,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "username": row["username"],
            "password_hash": row["password_hash"], "role": row["role"]}


# The two things a human account can be. `admin` manages identity itself --
# other accounts, and the per-machine worker credentials. `viewer` can log in
# and read the campaign and nothing else, which is what "let someone watch
# progress" needed all along: the read-only env token did it with a shared
# secret nobody could attribute or revoke individually.
# `operator` is the middle of the three, and the one most accounts should be:
# it reads and writes the CAMPAIGN -- add cases, lease, heartbeat, complete,
# fail, release, report fleet -- and manages NOTHING. It cannot create or delete
# accounts, cannot change anyone's role, cannot issue or revoke machine
# credentials, and cannot purge a campaign. Before it existed, "let this person
# run the campaign" and "let this person delete every account including yours"
# were the same grant, because `admin` was the only role that could write.
ROLES = ("admin", "operator", "viewer")


@_locked
def list_users(conn) -> list[dict[str, Any]]:
    """Every account, newest last. No password material of any kind."""
    rows = conn.execute(
        "SELECT id, username, role, created_at, last_login_at FROM users "
        "ORDER BY id").fetchall()
    return [{"id": r["id"], "username": r["username"], "role": r["role"],
             "created_at": r["created_at"], "last_login_at": r["last_login_at"]}
            for r in rows]


@_locked
def count_admins(conn) -> int:
    """Used to refuse the two operations that can lock everyone out: deleting
    the last admin, and demoting them."""
    return int(conn.execute(
        "SELECT COUNT(*) n FROM users WHERE role = 'admin'").fetchone()["n"])


@_locked
def set_password(conn, username: str, password_hash: str) -> bool:
    """Change a password and INVALIDATE every session that account holds.

    Revoking the sessions is the point, not a side effect: the reason to change
    a password in a hurry is that someone else may have it, and leaving their
    already-issued fortnight-long session alive would make the change
    cosmetic. The caller re-issues its own session afterwards, so changing your
    own password does not log you out of the tab you did it from.
    """
    cur = conn.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                       (password_hash, username))
    if not cur.rowcount:
        return False
    conn.execute(
        "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username = ?)",
        (username,))
    return True


@_locked
def set_role(conn, username: str, role: str) -> bool:
    if role not in ROLES:
        raise ValueError("unknown role %r; expected one of %s"
                         % (role, ", ".join(ROLES)))
    row = conn.execute("SELECT role FROM users WHERE username = ?",
                       (username,)).fetchone()
    if not row:
        return False
    if row["role"] == "admin" and role != "admin" and count_admins(conn) <= 1:
        raise ValueError(
            "%r is the only admin; promote another account before demoting it, "
            "or nobody will be able to manage this broker" % username)
    conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
    return True


@_locked
def delete_user(conn, username: str) -> bool:
    """Remove an account and every session it holds.

    Refuses the last admin. There is no recovery endpoint and no password-reset
    email -- deleting the only account that can manage the broker would leave
    the deployment permanently unmanageable, with the database the only way
    back in.
    """
    row = conn.execute("SELECT id, role FROM users WHERE username = ?",
                       (username,)).fetchone()
    if not row:
        return False
    if row["role"] == "admin" and count_admins(conn) <= 1:
        raise ValueError(
            "%r is the only admin; create another before deleting it, or "
            "nobody will be able to manage this broker" % username)
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
    conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
    return True


@_locked
def start_session(conn, user_id: int, token_hash: str, expires_at: int,
                  now: int | None = None) -> None:
    now = now or _now()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token_hash, user_id, now, expires_at))
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))


@_locked
def session_user(conn, token_hash: str, now: int | None = None):
    """The user behind a session id, or None if it is unknown or expired.

    Expiry is enforced HERE rather than by a background sweep: a sweep that
    stops running would silently extend every session forever, and this is one
    comparison on an indexed primary key.
    """
    now = now or _now()
    row = conn.execute(
        "SELECT u.id, u.username, u.role, s.expires_at FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (token_hash,)).fetchone()
    if not row or int(row["expires_at"]) <= now:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


@_locked
def end_session(conn, token_hash: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


@_locked
def purge_expired_sessions(conn, now: int | None = None) -> int:
    now = now or _now()
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    return int(cur.rowcount or 0)


@_locked
def create_worker_token(conn, name: str, token_hash: str, created_by: str | None = None,
                        now: int | None = None) -> dict[str, Any]:
    """Issue a credential for ONE machine. `name` is the worker id, which is what
    makes 'which box is this?' answerable on the dashboard.

    Re-issuing for a machine whose credential was REVOKED is the documented
    recovery path -- `casebroker worker setup --rotate`, and Revoke then Issue
    in the dashboard -- and it did not work. revoke_worker_token marks the row
    rather than deleting it (so `last_seen_at` and who issued it survive a
    revocation), which left UNIQUE(name) refusing the re-issue as well. Both
    paths answered 409 with "revoke it first", advice that could not succeed:
    the box that had just lost its token could not be given another one under
    its own worker id, and the id is what the lease check now enforces.

    A LIVE credential is still never replaced silently -- that would strand
    whichever token the machine is actually running on, which is the hazard the
    constraint exists for. Only a revoked row is reclaimed, and reclaiming it
    clears `last_seen_at` too: the new credential has not been seen, and
    inheriting the old one's timestamp would show a machine as alive on the
    strength of a token that no longer works.
    """
    now = now or _now()
    cur = conn.execute(
        "UPDATE worker_tokens SET token_hash = ?, created_by = ?, created_at = ?, "
        "revoked_at = NULL, last_seen_at = NULL "
        "WHERE name = ? AND revoked_at IS NOT NULL",
        (token_hash, created_by, now, name))
    if not cur.rowcount:
        # No revoked row to reclaim: either the name is new, or it is live and
        # the UNIQUE below is what refuses it.
        conn.execute(
            "INSERT INTO worker_tokens (name, token_hash, created_by, created_at) "
            "VALUES (?,?,?,?)", (name, token_hash, created_by, now))
    return {"name": name, "created_by": created_by, "created_at": now}


@_locked
def worker_token_owner(conn, token_hash: str, now: int | None = None):
    """The machine a token belongs to, or None if unknown or revoked.

    Also stamps last_seen_at, which is how the dashboard can show a machine as
    quiet without the worker having to report anything extra.
    """
    now = now or _now()
    row = conn.execute(
        "SELECT name, revoked_at FROM worker_tokens WHERE token_hash = ?",
        (token_hash,)).fetchone()
    if not row or row["revoked_at"] is not None:
        return None
    conn.execute("UPDATE worker_tokens SET last_seen_at = ? WHERE token_hash = ?",
                 (now, token_hash))
    return {"name": row["name"]}


@_locked
def revoke_worker_token(conn, name: str, now: int | None = None) -> bool:
    """Revoke by machine name. Takes effect on the next request -- it is a row
    read on every authenticated call, not a cached environment variable."""
    now = now or _now()
    cur = conn.execute(
        "UPDATE worker_tokens SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
        (now, name))
    return bool(cur.rowcount)


@_locked
def list_worker_tokens(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, created_by, created_at, last_seen_at, revoked_at "
        "FROM worker_tokens ORDER BY created_at DESC").fetchall()
    return [{"name": r["name"], "created_by": r["created_by"],
             "created_at": r["created_at"], "last_seen_at": r["last_seen_at"],
             "revoked_at": r["revoked_at"]} for r in rows]


@_locked
def quarantine_not_on_land(conn, is_land, dry_run: bool = True,
                           limit: int = 50) -> dict[str, Any]:
    """Find campaign cases whose coordinates are not on land, and park them.

    The gate on ``POST /v1/cases`` only protects cases added AFTER it existed.
    The published campaign predates it: the draw put sites in Antarctica and in
    the open ocean, and each one is 66 core-hours aimed at an empty flat plane.

    Quarantined rather than deleted. The state already means "this case is not
    going to run, and here is the trail of why" -- so nothing leases them, the
    rows and their history stay auditable, and the decision is reversible. The
    counts the dashboard shows stay honest for the same reason: these cases WERE
    drawn, and a campaign that silently shrank would misreport what its sampler
    produced.

    ``is_land`` is injected rather than imported so this stays a database
    function and the test does not need the tile list to exercise the sweep.

    ``dry_run`` defaults to TRUE. Answering "how bad is it" must not be the same
    keystroke as changing production.
    """
    found, ids_hit = [], []
    for r in conn.execute(
            "SELECT case_id, spec, state, city_cluster, lcz FROM cases"
            " WHERE state NOT IN ('done', 'quarantined')").fetchall():
        row = dict(r)
        spec = row["spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)
        lat, lon = spec.get("lat"), spec.get("lon")
        if lat is None or lon is None or is_land(float(lat), float(lon)):
            continue
        ids_hit.append(row["case_id"])
        if len(found) < limit:
            found.append({"case_id": row["case_id"], "lat": lat, "lon": lon,
                          "state": row["state"], "city_cluster": row["city_cluster"],
                          "lcz": row["lcz"]})

    out: dict[str, Any] = {"scanned_not_on_land": len(ids_hit),
                           "examples": found, "dry_run": dry_run,
                           "quarantined": 0}
    if dry_run or not ids_hit:
        return out

    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for cid in ids_hit:
            conn.execute(
                "UPDATE cases SET state='quarantined', lease_id=NULL, updated_at=?"
                " WHERE case_id=? AND state NOT IN ('done', 'quarantined')", (now, cid))
            _event(conn, cid, None, "quarantined",
                   "not on land: the building atlas publishes no tile here", now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    out["quarantined"] = len(ids_hit)
    return out



@_locked
def reopen_cases(conn, *, error_contains: str | None = None,
                 case_ids: list[str] | None = None,
                 dry_run: bool = True, limit: int = 50) -> dict[str, Any]:
    """Put quarantined cases back in the pool, with their attempts refunded.

    Quarantine is meant to say "this SITE is broken" -- degenerate geometry that
    fails identically everywhere. It also catches cases that merely ran three
    times on machines that could not run anything: a stopped Docker daemon
    charged an attempt per lease, and three of those quarantine a perfectly good
    site (COD-PKAST-7865, 2026-09-19). ``runner/run_case.sh`` has carried the
    warning for a year -- a wrongly fatal error "silently removes a site from the
    campaign with no way back short of editing the database" -- and this is that
    way back, so nobody has to open the database by hand.

    The attempts counter is RESET rather than decremented. A case reopened after
    a fleet-wide problem has a history of failures that say nothing about it, and
    leaving them counted would quarantine it again on the first real one.

    ``error_contains`` matches the last failure text the case recorded, which is
    what makes this usable as "undo what that one broken node did" instead of
    "reopen everything and hope". Matching is case-insensitive and substring, and
    it looks at the events trail rather than a summary column so a case that
    failed for two different reasons is judged on its LAST one.

    ``dry_run`` defaults to TRUE, as it does for the land audit and for purge:
    finding out how many cases are affected must not be the same keystroke as
    changing production.

    ``limit`` bounds the WRITES, not just the examples shown. It used to bound
    only the latter, which made the dry run actively misleading: it reported
    ``matched: 3000`` with fifty examples, and the same call with
    ``dry_run=False`` reopened all three thousand. A caller who passes a limit is
    asking for a bounded change to production, and this is the tool for undoing
    damage -- it must not be capable of a larger surprise than the one it repairs.
    Rows are taken in ``case_id`` order, so repeated calls drain the backlog
    deterministically, and ``capped`` beside ``matched`` and ``reopened`` is what
    keeps the truncation visible instead of silent.
    """
    where = ["state = 'quarantined'"]
    params: list[Any] = []
    if case_ids:
        where.append("case_id IN (%s)" % ",".join("?" for _ in case_ids))
        params.extend(case_ids)

    rows = conn.execute(
        "SELECT case_id, attempts, max_attempts, updated_at FROM cases"
        " WHERE " + " AND ".join(where) + " ORDER BY case_id", params).fetchall()

    found, ids_hit = [], []
    for r in rows:
        row = dict(r)
        last = conn.execute(
            "SELECT detail FROM events WHERE case_id=? AND event IN ('failed', 'quarantined')"
            " ORDER BY id DESC LIMIT 1", (row["case_id"],)).fetchone()
        detail = (dict(last)["detail"] if last else None) or ""
        if error_contains and error_contains.lower() not in detail.lower():
            continue
        ids_hit.append(row["case_id"])
        if len(found) < limit:
            found.append({"case_id": row["case_id"], "attempts": row["attempts"],
                          "last_error": detail[:200]})

    # The cap applies to what is CHANGED, not only to what is listed back.
    to_reopen = ids_hit[:limit]

    out: dict[str, Any] = {"matched": len(ids_hit), "examples": found,
                           "dry_run": dry_run, "reopened": 0,
                           "capped": len(ids_hit) > len(to_reopen)}
    if dry_run or not to_reopen:
        return out

    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for cid in to_reopen:
            conn.execute(
                # last_error goes with the attempts. The counter is reset because
                # the history says nothing about the case; the message is the same
                # history in prose, and leaving it behind puts a red "Last Failure
                # Error" banner on a case that is now pending and blameless --
                # which is the state an operator reopened it INTO.
                "UPDATE cases SET state='pending', lease_id=NULL, lease_worker=NULL,"
                " lease_expires=NULL, leased_at=NULL, attempts=0, last_error=NULL,"
                " updated_at=?"
                " WHERE case_id=? AND state='quarantined'", (now, cid))
            _event(conn, cid, None, "reopened",
                   "attempts refunded: " + (error_contains or "reopened by an operator"), now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    out["reopened"] = len(to_reopen)
    return out



# -- pairing: a machine asks, an admin approves ---------------------------------

PAIRING_TTL_SECONDS = 600
# Anyone who can reach the broker can ask to pair, so the queue is bounded: past
# this an admin is looking at a flood rather than a fleet, and the honest answer
# to one more request is "not now".
MAX_PENDING_PAIRINGS = 50


@_locked
def purge_expired_pairings(conn, now: int | None = None) -> None:
    """Expire what has timed out, and forget what expired more than a day ago.

    Kept for a day rather than deleted at once, so "I approved it and nothing
    happened" still has a row to explain it.
    """
    now = now or _now()
    conn.execute("UPDATE pairings SET status='expired' WHERE status='pending' AND expires_at < ?",
                 (now,))
    conn.execute("DELETE FROM pairings WHERE expires_at < ?", (now - 86400,))


@_locked
def create_pairing(conn, user_code: str, name: str, token_hash: str,
                   host: str | None = None, platform: str | None = None,
                   requested_ip: str | None = None,
                   ttl: int = PAIRING_TTL_SECONDS, now: int | None = None) -> dict[str, Any]:
    """Record a request to join. Raises ValueError with a reason a caller can show.

    Refused up front, rather than at approval, when it could never succeed: a
    name that already has a LIVE credential (approving would strand the token
    that box is actually running on) or a hash some credential already uses.
    The person sitting at the node learns now, not after an admin has clicked.
    """
    now = now or _now()
    purge_expired_pairings(conn, now)
    live = conn.execute(
        "SELECT 1 FROM worker_tokens WHERE name = ? AND revoked_at IS NULL", (name,)).fetchone()
    if live:
        raise ValueError("name-in-use")
    if conn.execute("SELECT 1 FROM worker_tokens WHERE token_hash = ?", (token_hash,)).fetchone():
        raise ValueError("token-in-use")
    pending = conn.execute(
        "SELECT COUNT(*) n FROM pairings WHERE status='pending'").fetchone()["n"]
    if pending >= MAX_PENDING_PAIRINGS:
        raise ValueError("too-many-pending")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-running setup on the same box replaces its earlier request instead
        # of stacking a second card for the admin to choose between.
        conn.execute("UPDATE pairings SET status='superseded', resolved_at=?"
                     " WHERE name = ? AND status='pending'", (now, name))
        conn.execute(
            "INSERT INTO pairings (user_code, name, token_hash, host, platform,"
            " requested_ip, status, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,'pending',?,?)",
            (user_code, name, token_hash, host, platform, requested_ip, now, now + ttl))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"user_code": user_code, "name": name, "expires_at": now + ttl}


@_locked
def get_pairing(conn, user_code: str, now: int | None = None) -> dict[str, Any] | None:
    purge_expired_pairings(conn, now)
    row = conn.execute("SELECT * FROM pairings WHERE user_code = ?", (user_code,)).fetchone()
    return dict(row) if row is not None else None


@_locked
def list_pending_pairings(conn, now: int | None = None) -> list[dict[str, Any]]:
    purge_expired_pairings(conn, now)
    rows = conn.execute(
        "SELECT user_code, name, host, platform, requested_ip, created_at, expires_at"
        " FROM pairings WHERE status='pending' ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


@_locked
def resolve_pairing(conn, user_code: str, approve: bool, by: str,
                    now: int | None = None) -> str:
    """Approve or deny. Returns the resulting status, or a reason it did not apply:
    'missing', 'expired', 'conflict' (the name or hash was taken meanwhile), or the
    status it already had if someone else resolved it first.
    """
    now = now or _now()
    purge_expired_pairings(conn, now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM pairings WHERE user_code = ?", (user_code,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return "missing"
        if row["status"] != "pending":
            conn.execute("ROLLBACK")
            return row["status"]
        if approve:
            try:
                create_worker_token(conn, row["name"], row["token_hash"], created_by=by, now=now)
            except Exception:
                # UNIQUE(name) on a live credential, or UNIQUE(token_hash): it was
                # free when the node asked and is not now.
                conn.execute("ROLLBACK")
                return "conflict"
        status = "approved" if approve else "denied"
        conn.execute("UPDATE pairings SET status=?, resolved_by=?, resolved_at=? WHERE user_code=?",
                     (status, by, now, user_code))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return status
