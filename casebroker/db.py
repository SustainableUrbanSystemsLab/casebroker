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

import contextlib
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
    updated_at     INTEGER NOT NULL,
    -- How many cells this site's mesh had, from the node's `mesh` telemetry
    -- (total_cells). Unlike `telemetry` it is NOT cleared when a new attempt
    -- starts over: it describes the site, and is what lets lease() keep a case
    -- from a node too small to hold its mesh (the memory gate).
    mesh_cells     INTEGER,
    -- A case this one is built ON, and so waits for: an MRT case (docs/mrt.md)
    -- reads the site's finished surface-temperature archive, and is handed to
    -- no node until that case is done. A column, not a spec key, so the lease
    -- query checks it without opening a spec. NULL for every other case.
    needs_case     TEXT
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
    id_conflict_at   INTEGER,
    -- What the node says it can do (JSON list: continue_from_broker, handoff,
    -- heartbeat_stage, ...) and the machine it runs on: the cores it gives a case
    -- and the memory it has. Declared with every lease; NULL = never said.
    features         TEXT,
    cpus             INTEGER,
    mem_gb           REAL,
    -- Why the node will not take a case, in its own words (a full disk, an engine
    -- that is not running), and since when. Said with its release asks, which it
    -- goes on making while it waits; cleared when it says nothing, and by its next
    -- lease. A node that refused on its own side used to look like one that died.
    unfit            TEXT,
    unfit_since      INTEGER,
    -- How busy the whole machine is (percent of every core, its solve and anything else
    -- on it -- a lab workstation in use, a node shared with another job), and when the
    -- node measured it. Said with its release asks; NULL from a node that does not.
    cpu_pct          REAL,
    cpu_at           INTEGER
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
    reported_at  INTEGER NOT NULL,
    -- The jobs themselves, as JSON [{id, state, reason, submitted_at, start_at}]:
    -- what the Worker Fleet table lists for a job that has not started, so it has
    -- not yet called the broker and has no worker row. NULL from a reporter that
    -- sends counts only.
    jobs         TEXT
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
    detail    TEXT,
    -- The stage a progress line belongs to, as the node that wrote it said
    -- (heartbeat `stage`); NULL when it did not, and stages.stage_of(detail)
    -- reads it off the text instead.
    stage     TEXT
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

-- A link someone can be sent to look at the campaign and change nothing: one row
-- per link an admin made, so "who holds one, did they open it, take it back" can
-- all be answered. The link carries a random token and only its SHA-256 is kept
-- here, for the reason sessions and machine tokens are hashed -- a dump of this
-- table must not hand over a working link. `expires_at` NULL means until revoked.
-- A revoked or expired row stays for a month, so the list can say what became of
-- it, and is then swept.
CREATE TABLE IF NOT EXISTS share_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT UNIQUE NOT NULL,
    label        TEXT NOT NULL,
    created_by   TEXT,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    last_used_at INTEGER,
    opened       INTEGER NOT NULL DEFAULT 0,
    revoked_at   INTEGER,
    revoked_by   TEXT
);

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
-- What a node has already shipped of a case: the mesh once meshing passed, each
-- direction as it finished (Eddy3D CaseParts). A node that takes the case over
-- continues from the broker's copy of THIS mesh (case_blobs) and solves only the
-- directions not listed here. A direction is valid only with
-- the mesh it was solved on, so every row carries that mesh's sha256, and a new
-- mesh for the case deletes the rows of the old one (report_part).
CREATE TABLE IF NOT EXISTS case_parts (
    case_id     TEXT NOT NULL,
    part        TEXT NOT NULL,
    archive     TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       BIGINT,
    mesh_sha256 TEXT,
    -- A direction's convergence entry (JSON), as the node's gate writes it.
    verdict     TEXT,
    worker_id   TEXT,
    reported_at INTEGER NOT NULL,
    PRIMARY KEY (case_id, part)
);

-- The pedestrian wind field itself, per finished direction: |U| at height_m above
-- grade on the case's regular grid (the 2 m lattice over the 1008 m core), as the
-- node read it off OpenFOAM's own surface sample. One self-describing gzip blob
-- per row (umag/1: b"UMAG" | u32 version | u32 header length | header JSON |
-- float32 LE nx*ny, NaN where there was no fluid), about a megabyte each. The
-- broker used to hold POINTERS only, because its database was 500 MB; it now
-- holds the campaign's published field (Patrick, 2026-09-28), so a case's answer
-- can be read without opening its archive, and a lost master costs no field.
CREATE TABLE IF NOT EXISTS case_fields (
    case_id     TEXT NOT NULL,
    direction   TEXT NOT NULL,
    height_m    REAL NOT NULL,
    deg         REAL,
    nx          INTEGER NOT NULL,
    ny          INTEGER NOT NULL,
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    spacing_m   REAL NOT NULL,
    -- Share of the grid with a value (outside buildings, mesh present).
    coverage    REAL,
    -- The inlet log law at height_m: the denominator of U/U_ref.
    u_ref       REAL,
    -- The 99.9th percentile of |U|: the top of a colour scale a viewer shares
    -- across a case's directions without reading every field first.
    umag_p999   REAL,
    -- Where the float32 values start in the stored container (12 + header
    -- length): a cell or a band of rows is then substr() of the blob, read in
    -- place. NULL for a row stored gzip-wrapped (before 2026-10-06), which has to
    -- be read whole.
    data_offset INTEGER,
    -- The field summarised over its finite cells (casebroker/umag.py, `stats`),
    -- computed by the broker when the field is stored, for GET /v1/fields to
    -- filter and sort on. U/U_ref is each of them over u_ref. NULL until computed
    -- (POST /v1/fields/stats fills rows stored before the columns existed).
    n_valid     INTEGER,
    umag_mean   REAL,
    umag_min    REAL,
    umag_p05    REAL,
    umag_p25    REAL,
    umag_p50    REAL,
    umag_p75    REAL,
    umag_p95    REAL,
    umag_p99    REAL,
    umag_max    REAL,
    sha256      TEXT NOT NULL,
    bytes       BIGINT NOT NULL,
    blob        BLOB NOT NULL,
    worker_id   TEXT,
    reported_at INTEGER NOT NULL,
    PRIMARY KEY (case_id, direction, height_m)
);

-- The residual history of each wind direction a node solved: the curves a CFD
-- engineer reads convergence off. Telemetry keeps only the LATEST report of a
-- kind, which is a number; this is the series behind it, one row per (case,
-- direction) and 32 to a case. `series` is JSON, {iterations: [...], fields:
-- {Ux: [...], ...}, total, complete}, a node's decimated copy of the solver's
-- own trace; `source` says where it came from -- 'trace' (the node sent the
-- series) or 'reports' (the broker assembled it from the latest-residual
-- numbers of successive `solve` reports, so a node that predates traces still
-- gets a coarse curve). n, iteration and end_time repeat what is inside
-- `series` so a case's list of directions needs no JSON parsing. See
-- post_telemetry (kind 'residuals') and protocol.md, "Telemetry".
CREATE TABLE IF NOT EXISTS case_residuals (
    case_id     TEXT NOT NULL,
    direction   TEXT NOT NULL,
    source      TEXT NOT NULL,
    n           INTEGER NOT NULL,
    iteration   REAL,
    end_time    REAL,
    series      TEXT NOT NULL,
    worker_id   TEXT,
    reported_at INTEGER NOT NULL,
    PRIMARY KEY (case_id, direction)
);

-- What has ARRIVED where results are kept (DOMAIN.md, "Custody"). `state` says what
-- became of the computation; a receipt says where its result is, proven by whoever
-- holds it -- the broker's own part store, or an operator's scan of a copy. One row per
-- (case, artifact, location); a second report of the same artifact replaces the
-- first. A pedestrian field's receipt is its case_fields row: the broker stored it.
CREATE TABLE IF NOT EXISTS case_artifacts (
    case_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    location    TEXT NOT NULL,
    bytes       BIGINT,
    sha256      TEXT NOT NULL,
    path        TEXT,
    received_at INTEGER NOT NULL,
    reported_by TEXT,
    PRIMARY KEY (case_id, kind, location)
);
-- The parts the BROKER holds (partstore.py), uploaded by the node that made them:
-- one row per (case, part), `part` being 'mesh', 'case_<dir>' or 'archive' (the
-- case's own <case>.tar.gz). The bytes are a file named by `sha256` in the part
-- store, not a value here: a campaign case is ~8.5 GB of parts. Every row is a
-- part whose whole content was hashed and matched the hash the node reported.
CREATE TABLE IF NOT EXISTS case_blobs (
    case_id     TEXT NOT NULL,
    part        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       BIGINT NOT NULL,
    stored_at   INTEGER NOT NULL,
    stored_by   TEXT,
    PRIMARY KEY (case_id, part)
);
CREATE INDEX IF NOT EXISTS idx_case_blobs_sha ON case_blobs(sha256);

-- A browser that asked to be told things with its tab closed (casebroker/push.py).
-- `endpoint` is the push service's URL for that browser; p256dh and auth are the
-- keys a message to it is encrypted with (RFC 8291), so they are never logged.
-- `events` is the JSON list of notice kinds this browser wants. Who subscribed is
-- kept as the credential that did it -- an account, a share link, a token -- and
-- re-checked at every send: a revoked link or a deleted account must not go on
-- being told about the campaign, and a demoted admin stops getting admin notices.
-- `failures` counts sends in a row the push service refused; past ~10 the row is
-- dropped (a 404 or 410 drops it at once: the browser unsubscribed).
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint        TEXT PRIMARY KEY,
    p256dh          TEXT NOT NULL,
    auth            TEXT NOT NULL,
    events          TEXT NOT NULL,
    subscriber_kind TEXT NOT NULL,
    subscriber      TEXT NOT NULL,
    role            TEXT,
    user_agent      TEXT,
    created_at      INTEGER NOT NULL,
    last_sent_at    INTEGER,
    last_error      TEXT,
    failures        INTEGER NOT NULL DEFAULT 0
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
    updated_at     INTEGER NOT NULL,
    -- How many cells this site's mesh had, from the node's `mesh` telemetry
    -- (total_cells). Unlike `telemetry` it is NOT cleared when a new attempt
    -- starts over: it describes the site, and is what lets lease() keep a case
    -- from a node too small to hold its mesh (the memory gate).
    mesh_cells     BIGINT,
    -- A case this one is built on and waits for (docs/mrt.md); see the SQLite schema.
    needs_case     TEXT
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
    id_conflict_at   INTEGER,
    -- What the node says it can do (JSON list: continue_from_broker, handoff,
    -- heartbeat_stage, ...) and the machine it runs on: the cores it gives a case
    -- and the memory it has. Declared with every lease; NULL = never said.
    features         TEXT,
    cpus             INTEGER,
    mem_gb           REAL,
    -- Why the node will not take a case, in its own words (a full disk, an engine
    -- that is not running), and since when. Said with its release asks, which it
    -- goes on making while it waits; cleared when it says nothing, and by its next
    -- lease. A node that refused on its own side used to look like one that died.
    unfit            TEXT,
    unfit_since      INTEGER,
    -- How busy the whole machine is (percent of every core, its solve and anything else
    -- on it -- a lab workstation in use, a node shared with another job), and when the
    -- node measured it. Said with its release asks; NULL from a node that does not.
    cpu_pct          REAL,
    cpu_at           INTEGER
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
    reported_at  INTEGER NOT NULL,
    -- The jobs themselves, as JSON [{id, state, reason, submitted_at, start_at}]:
    -- what the Worker Fleet table lists for a job that has not started, so it has
    -- not yet called the broker and has no worker row. NULL from a reporter that
    -- sends counts only.
    jobs         TEXT
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
    detail    TEXT,
    -- The stage a progress line belongs to, as the node that wrote it said
    -- (heartbeat `stage`); NULL when it did not, and stages.stage_of(detail)
    -- reads it off the text instead.
    stage     TEXT
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

-- A link someone can be sent to look at the campaign and change nothing: one row
-- per link an admin made, so "who holds one, did they open it, take it back" can
-- all be answered. The link carries a random token and only its SHA-256 is kept
-- here, for the reason sessions and machine tokens are hashed -- a dump of this
-- table must not hand over a working link. `expires_at` NULL means until revoked.
-- A revoked or expired row stays for a month, so the list can say what became of
-- it, and is then swept.
CREATE TABLE IF NOT EXISTS share_links (
    id           SERIAL PRIMARY KEY,
    token_hash   TEXT UNIQUE NOT NULL,
    label        TEXT NOT NULL,
    created_by   TEXT,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    last_used_at INTEGER,
    opened       INTEGER NOT NULL DEFAULT 0,
    revoked_at   INTEGER,
    revoked_by   TEXT
);

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
-- What a node has already shipped of a case: the mesh once meshing passed, each
-- direction as it finished (Eddy3D CaseParts). A node that takes the case over
-- continues from the broker's copy of THIS mesh (case_blobs) and solves only the
-- directions not listed here. A direction is valid only with
-- the mesh it was solved on, so every row carries that mesh's sha256, and a new
-- mesh for the case deletes the rows of the old one (report_part).
CREATE TABLE IF NOT EXISTS case_parts (
    case_id     TEXT NOT NULL,
    part        TEXT NOT NULL,
    archive     TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       BIGINT,
    mesh_sha256 TEXT,
    -- A direction's convergence entry (JSON), as the node's gate writes it.
    verdict     TEXT,
    worker_id   TEXT,
    reported_at INTEGER NOT NULL,
    PRIMARY KEY (case_id, part)
);

-- The pedestrian wind field itself, per finished direction: |U| at height_m above
-- grade on the case's regular grid (the 2 m lattice over the 1008 m core), as the
-- node read it off OpenFOAM's own surface sample. One self-describing gzip blob
-- per row (umag/1: b"UMAG" | u32 version | u32 header length | header JSON |
-- float32 LE nx*ny, NaN where there was no fluid), about a megabyte each. The
-- broker used to hold POINTERS only, because its database was 500 MB; it now
-- holds the campaign's published field (Patrick, 2026-09-28), so a case's answer
-- can be read without opening its archive, and a lost master costs no field.
CREATE TABLE IF NOT EXISTS case_fields (
    case_id     TEXT NOT NULL,
    direction   TEXT NOT NULL,
    height_m    REAL NOT NULL,
    deg         REAL,
    nx          INTEGER NOT NULL,
    ny          INTEGER NOT NULL,
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    spacing_m   REAL NOT NULL,
    -- Share of the grid with a value (outside buildings, mesh present).
    coverage    REAL,
    -- The inlet log law at height_m: the denominator of U/U_ref.
    u_ref       REAL,
    -- The 99.9th percentile of |U|: the top of a colour scale a viewer shares
    -- across a case's directions without reading every field first.
    umag_p999   REAL,
    -- Where the float32 values start in the stored container (12 + header
    -- length): a cell or a band of rows is then substr() of the blob, read in
    -- place. NULL for a row stored gzip-wrapped (before 2026-10-06), which has to
    -- be read whole.
    data_offset INTEGER,
    -- The field summarised over its finite cells (casebroker/umag.py, `stats`),
    -- computed by the broker when the field is stored, for GET /v1/fields to
    -- filter and sort on. U/U_ref is each of them over u_ref. NULL until computed
    -- (POST /v1/fields/stats fills rows stored before the columns existed).
    n_valid     INTEGER,
    umag_mean   REAL,
    umag_min    REAL,
    umag_p05    REAL,
    umag_p25    REAL,
    umag_p50    REAL,
    umag_p75    REAL,
    umag_p95    REAL,
    umag_p99    REAL,
    umag_max    REAL,
    sha256      TEXT NOT NULL,
    bytes       BIGINT NOT NULL,
    blob        BYTEA NOT NULL,
    worker_id   TEXT,
    reported_at INTEGER NOT NULL,
    PRIMARY KEY (case_id, direction, height_m)
);

-- The residual history of each wind direction a node solved (see the SQLite
-- copy of this schema for what the columns mean).
CREATE TABLE IF NOT EXISTS case_residuals (
    case_id     TEXT NOT NULL,
    direction   TEXT NOT NULL,
    source      TEXT NOT NULL,
    n           INTEGER NOT NULL,
    iteration   REAL,
    end_time    REAL,
    series      TEXT NOT NULL,
    worker_id   TEXT,
    reported_at INTEGER NOT NULL,
    PRIMARY KEY (case_id, direction)
);

-- What has ARRIVED where results are kept (DOMAIN.md, "Custody"). `state` says what
-- became of the computation; a receipt says where its result is, proven by whoever
-- holds it -- the broker's own part store, or an operator's scan of a copy. One row per
-- (case, artifact, location); a second report of the same artifact replaces the
-- first. A pedestrian field's receipt is its case_fields row: the broker stored it.
CREATE TABLE IF NOT EXISTS case_artifacts (
    case_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    location    TEXT NOT NULL,
    bytes       BIGINT,
    sha256      TEXT NOT NULL,
    path        TEXT,
    received_at INTEGER NOT NULL,
    reported_by TEXT,
    PRIMARY KEY (case_id, kind, location)
);
-- The parts the BROKER holds (partstore.py), uploaded by the node that made them:
-- one row per (case, part), `part` being 'mesh', 'case_<dir>' or 'archive' (the
-- case's own <case>.tar.gz). The bytes are a file named by `sha256` in the part
-- store, not a value here: a campaign case is ~8.5 GB of parts. Every row is a
-- part whose whole content was hashed and matched the hash the node reported.
CREATE TABLE IF NOT EXISTS case_blobs (
    case_id     TEXT NOT NULL,
    part        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       BIGINT NOT NULL,
    stored_at   INTEGER NOT NULL,
    stored_by   TEXT,
    PRIMARY KEY (case_id, part)
);
CREATE INDEX IF NOT EXISTS idx_case_blobs_sha ON case_blobs(sha256);

-- A browser that asked to be told things with its tab closed (casebroker/push.py).
-- `endpoint` is the push service's URL for that browser; p256dh and auth are the
-- keys a message to it is encrypted with (RFC 8291), so they are never logged.
-- `events` is the JSON list of notice kinds this browser wants. Who subscribed is
-- kept as the credential that did it -- an account, a share link, a token -- and
-- re-checked at every send: a revoked link or a deleted account must not go on
-- being told about the campaign, and a demoted admin stops getting admin notices.
-- `failures` counts sends in a row the push service refused; past ~10 the row is
-- dropped (a 404 or 410 drops it at once: the browser unsubscribed).
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint        TEXT PRIMARY KEY,
    p256dh          TEXT NOT NULL,
    auth            TEXT NOT NULL,
    events          TEXT NOT NULL,
    subscriber_kind TEXT NOT NULL,
    subscriber      TEXT NOT NULL,
    role            TEXT,
    user_agent      TEXT,
    created_at      INTEGER NOT NULL,
    last_sent_at    INTEGER,
    last_error      TEXT,
    failures        INTEGER NOT NULL DEFAULT 0
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

SCHEMA_VERSION = 10

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


#: Postgres columns kept out of TOAST compression. A field is float32, which no
#: general-purpose compressor shrinks: measured on the campaign's own fields, lz4
#: saved 0% and pglz cannot do better, so trying is CPU spent on every write on a
#: four-core CPU. EXTERNAL keeps the value out of line and uncompressed, and
#: lets substring() read a byte range of it without fetching the rest.
UNCOMPRESSED_COLUMNS = (("case_fields", "blob"),)


def set_column_storage(conn, is_pg: bool) -> list[str]:
    """Postgres only: SET STORAGE EXTERNAL on UNCOMPRESSED_COLUMNS that are not
    already. Read before writing, and a no-op repeated, like widen_columns. It
    changes how NEW values are stored; rows already written keep their form."""
    if not is_pg:
        return []
    changed = []
    for table, column in UNCOMPRESSED_COLUMNS:
        row = conn.execute(
            "SELECT a.attstorage AS s FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = ? AND a.attname = ? AND NOT a.attisdropped",
            (table, column)).fetchone()
        if row is None or row["s"] == "e":
            continue
        conn.execute("ALTER TABLE %s ALTER COLUMN %s SET STORAGE EXTERNAL" % (table, column))
        changed.append("%s.%s" % (table, column))
    return changed


#: Settings rows a removed feature left behind. The ntfy push notifier (shipped
#: in one release, then removed) wrote ``notify_cursor`` at every start, and an
#: admin may have set the others from Settings -- a topic URL and a token among
#: them, which are secrets. Nothing reads any of them now, so they are deleted
#: when a database is opened rather than left in the table for good.
RETIRED_SETTINGS = ("notify_cursor", "notify_url", "notify_token",
                    "notify_events", "notify_public_url",
                    # The fleet's Syncthing master and folder, named on the dashboard
                    # until Syncthing was retired (2026-10-06).
                    "syncthing_master", "syncthing_folder")


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
        stored = set_column_storage(conn, is_pg)
        if stored:
            print("[schema] stored without compression: " + ", ".join(stored), file=sys.stderr)
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
    # What earlier attempts already shipped (case_parts): a node continues from
    # that mesh and skips those directions.
    parts: tuple[dict[str, Any], ...] = ()


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
#: The recipes that are not CFD jobs: Radiance surface temperatures (docs/thermal.md)
#: and MRT built on them (docs/mrt.md). No mesh, no directions, no pedestrian wind
#: field; progress is counted in chunks and the long phase is the trace.
RADIANCE_RECIPE_PREFIXES = ("surf-", "mrt-")

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


def _strip_nul(value: Any) -> Any:
    """A string parameter without NUL characters; anything else unchanged.

    Postgres TEXT cannot hold U+0000 and psycopg refuses the whole statement
    ("PostgreSQL text fields cannot contain NUL (0x00) bytes"), where SQLite
    stores it happily. Node output reaches the database verbatim -- a /v1/fail
    error is the tail of a step log -- and `wsl.exe` writes UTF-16, which a
    node reading it as UTF-8 turns into text with a NUL after every character.
    Every such failure report answered 500, so the failure was never counted and
    the node that could not run the case leased it again at the same attempt.
    """
    return value.replace("\x00", "") if isinstance(value, str) and "\x00" in value else value


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
        params = tuple(_strip_nul(p) for p in params) if params else None
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


class PgPool(PgConnection):
    """Several Postgres connections behind the one object every route closes over.

    One shared connection under one process-wide lock serialised the whole broker:
    a heartbeat waited behind a /v1/fields scan, a custody query, a dataset page
    or a 1 MB field insert, and the code had grown comments apologising for it
    ("list_cases holds _LOCK while it runs, so the sort stalls every worker's
    lease"). That lock was only ever needed for SQLite; on Postgres, correctness
    across concurrent callers already comes from the database -- FOR UPDATE SKIP
    LOCKED in lease(), FOR UPDATE in _by_lease() -- exactly as it must across
    machines. The single connection was a Supabase-era economy (a transaction
    pooler with a connection cap); the self-hosted Postgres has neither.

    So each locked entry point (`_locked`) takes a SESSION -- one of up to `size`
    PgConnections, each with its own reconnect logic and its own transaction
    state -- binds it to the calling thread for the whole call, and returns it
    after. Nested locked calls on one thread reuse the bound session, so a
    function that calls another stays inside one transaction. A statement issued
    outside any locked call (a test, a script) takes a session for that one
    statement. Callers that wait for a free session simply queue, as they queued
    on the lock before -- only now `size` of them run at once.

    A subclass so every `isinstance(conn, PgConnection)` dialect check holds.
    """

    def __init__(self, dsn: str, size: int, connect: Callable[[], Any]):
        # Not super().__init__: this object owns no connection of its own.
        self._dsn = dsn
        self._size = max(1, int(size))
        self._connect_raw = connect
        self._idle: list[PgConnection] = []
        self._idle_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(self._size)
        self._local = threading.local()

    # PgConnection's own state, answered for whichever session this thread holds,
    # so code (and tests) written against one connection keep working.
    @property
    def _raw(self):
        return self._held_or_any()._raw

    @property
    def _in_tx(self) -> bool:
        held = getattr(self._local, "conn", None)
        return bool(held and held._in_tx)

    def _held_or_any(self) -> PgConnection:
        held = getattr(self._local, "conn", None)
        if held is not None:
            return held
        with self._idle_lock:
            if self._idle:
                return self._idle[-1]
        with self.session() as c:
            return c

    @contextlib.contextmanager
    def session(self):
        """Bind one connection to this thread for the duration of the block."""
        held = getattr(self._local, "conn", None)
        if held is not None:
            yield held
            return
        self._slots.acquire()
        try:
            with self._idle_lock:
                c = self._idle.pop() if self._idle else None
            if c is None:
                c = PgConnection(self._connect_raw(), self._dsn)
            self._local.conn = c
            try:
                yield c
            finally:
                self._local.conn = None
                if c._in_tx:
                    # A call that returned with a transaction still open is a bug
                    # somewhere above, and the next caller must not inherit it:
                    # its COMMIT would commit someone else's half-done work.
                    try:
                        c.execute("ROLLBACK")
                    except Exception:                        # noqa: BLE001
                        c._in_tx = False
                        c._needs_reconnect = True
                with self._idle_lock:
                    self._idle.append(c)
        finally:
            self._slots.release()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> "_PgCursor":
        held = getattr(self._local, "conn", None)
        if held is not None:
            return held.execute(sql, params)
        # psycopg's client-side cursor holds its whole result, so the session can
        # go back to the pool before the caller fetches from it.
        with self.session() as c:
            return c.execute(sql, params)

    def executescript(self, sql: str) -> None:
        with self.session() as c:
            c.executescript(sql)

    def _reconnect(self) -> bool:
        held = getattr(self._local, "conn", None)
        return held._reconnect() if held is not None else False


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


def connect(path_or_dsn: str, pool: int = 1):
    """SQLite for a file path, Postgres for a ``postgres(ql)://`` DSN.

    ``pool`` > 1 on Postgres gives a `PgPool` of that many connections, so that
    many database calls run at once; 1 (the default, and every script and test
    that does not ask) is one connection under the process-wide lock, as before.
    SQLite ignores it: one file has one writer whatever the process does.

    The caller (``CASEBROKER_DB`` in practice) decides the engine purely by what
    string it passes; nothing else in this module, or above it, branches on
    which one it got except the handful of statements in this file that
    genuinely differ between the two.
    """
    if path_or_dsn.startswith(("postgres://", "postgresql://")):
        return _connect_postgres(path_or_dsn, pool)

    # check_same_thread=False because the connection is shared across the
    # threadpool; _LOCK is what makes that safe.
    conn = sqlite3.connect(path_or_dsn, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _LOCK:
        apply_schema(conn, SCHEMA, is_pg=False)
    return conn


def _connect_postgres(dsn: str, pool: int = 1) -> PgConnection:
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
    if pool <= 1:
        return wrapped
    # The schema is in place; the connection that brought it forward becomes the
    # pool's first session rather than being thrown away.
    pooled = PgPool(dsn, pool, lambda: psycopg.connect(dsn, **kwargs))
    pooled._idle.append(wrapped)
    return pooled


def _now() -> int:
    return int(time.time())


def _event(conn, case_id, worker_id, event, detail=None, now=None, stage=None) -> None:
    if stage is None:
        conn.execute(
            "INSERT INTO events(ts, case_id, worker_id, event, detail) VALUES (?,?,?,?,?)",
            (now or _now(), case_id, worker_id, event, detail),
        )
        return
    conn.execute(
        "INSERT INTO events(ts, case_id, worker_id, event, detail, stage) VALUES (?,?,?,?,?,?)",
        (now or _now(), case_id, worker_id, event, detail, stage),
    )


# -- ingest -------------------------------------------------------------------

# Postgres errors that mean "your transaction lost a race and was rolled back;
# run it again". With several sessions (PgPool) two transactions can now meet in
# the database instead of in Python, and the database settles it by aborting one.
_PG_RETRY_SQLSTATES = {"40P01", "40001"}          # deadlock_detected, serialization_failure
_PG_RETRIES = 2


def _pg_retryable(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) in _PG_RETRY_SQLSTATES


def _locked(fn):
    """Serialise a public entry point against the shared connection -- or, on a
    `PgPool`, give it a session of its own for the whole call (see PgPool)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        conn = a[0] if a else kw.get("conn")
        if isinstance(conn, PgPool):
            for attempt in range(_PG_RETRIES + 1):
                with conn.session() as session:
                    nested = session._in_tx
                    try:
                        return fn(*a, **kw)
                    except Exception as exc:                 # noqa: BLE001
                        # Only a whole call is re-run, never one nested inside a
                        # transaction its caller opened: that caller's work went
                        # with the rollback, and it must see the error.
                        if nested or attempt == _PG_RETRIES or not _pg_retryable(exc):
                            raise
                        print(f"[db] {fn.__name__}: {exc.__class__.__name__}; "
                              f"running it again ({attempt + 1}/{_PG_RETRIES})", file=sys.stderr)
                time.sleep(0.05 * (attempt + 1))
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
              "  max_attempts, created_at, updated_at, needs_case) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
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
                 r.get("max_attempts", 3), now, now, r.get("needs_case")),
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

# An expired lease is immediately eligible for a new worker to reclaim.  This
# longer interval is only for clearing rows that no worker has asked for, so the
# dashboard does not describe dead work as in-flight forever.
STALE_LEASE_RELEASE_SECONDS = 48 * 3600


@_locked
def _release_stale_leases(conn, now: int) -> int:
    """Return abandoned expired leases to pending without refunding attempts.

    `release()` is deliberately not used: graceful preemption refunds an
    attempt, whereas this worker already consumed one before disappearing.
    Heartbeats reject expired lease ids, so no live worker can be displaced.
    """
    rows = conn.execute(
        "SELECT case_id, lease_worker FROM cases"
        " WHERE state='leased' AND lease_expires <= ?",
        (now - STALE_LEASE_RELEASE_SECONDS,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE cases SET state='pending', lease_id=NULL, lease_worker=NULL,"
            " lease_expires=NULL, leased_at=NULL, updated_at=? WHERE case_id=?",
            (now, row["case_id"]),
        )
        _event(conn, row["case_id"], row["lease_worker"], "stale-released",
               "expired lease was not reclaimed within 48 hours", now)
    return len(rows)


# A case a MACHINE just failed goes to a different machine first. A worker asks
# for its next case the moment it reports a failure, and the failed case -- back
# in pending with its own priority and case_id -- is first in the queue, so a
# case's three attempts were three tries on ONE machine within minutes and the
# second opinion they exist for never happened. On 2026-09-23 every quarantine
# in the campaign had been spent that way: v2-00ed64225d4979d7 on cod-359-40-2
# at 18:37, 18:44 and 18:51; v2-0057457805ddf4bf three times on cod-359-38
# inside one minute, each lease meeting the files the one before it left;
# v2-00427078fdfaa380 three times in six minutes on cod-358-21, then three more
# in sixteen after a reset. A bad build, a leftover process or a machine that
# cannot mesh a site read as a broken site. For this long after a failure, no
# worker on that host is handed the case again -- fresh or as a resume -- while
# any other machine can take it. Only a failure counts: a refunded stop is
# recorded as 'released', and that is exactly the case a machine should get
# back to continue its own checkpoint. A one-machine fleet still retries, later.
FAIL_COOLDOWN_SECONDS = int(os.environ.get("CASEBROKER_FAIL_COOLDOWN", str(12 * 3600)))
_RECENTLY_FAILED_HERE_SQL = (
    " AND NOT EXISTS (SELECT 1 FROM events f WHERE f.case_id = cases.case_id"
    " AND f.event IN ('failed', 'quarantined') AND f.ts > ?"
    " AND (f.worker_id = ? OR f.worker_id IN"
    "      (SELECT w.worker_id FROM workers w WHERE w.host = ?)))")

# A case a machine HANDED OFF (release with handoff=true: it solved its
# --max-directions or --chunk-hours share) is for another machine first, by the
# same host rule as a failure and for a much shorter time: nothing is wrong with
# the case or the machine, the point is only that the node letting go does not
# take it straight back on its next poll. Past the cooldown, a fleet with nobody
# else free lets the same node continue -- from its own scratch, as a resume.
HANDOFF_COOLDOWN_SECONDS = int(os.environ.get("CASEBROKER_HANDOFF_COOLDOWN", "900"))
_RECENTLY_HANDED_OFF_HERE_SQL = (
    " AND NOT EXISTS (SELECT 1 FROM events h WHERE h.case_id = cases.case_id"
    " AND h.event = 'handed-off' AND h.ts > ?"
    " AND (h.worker_id = ? OR h.worker_id IN"
    "      (SELECT w.worker_id FROM workers w WHERE w.host = ?)))")

# Memory a node needs per million cells of a case's mesh, for the lease's memory
# gate. 2 GB is the conservative end for snappyHexMesh + a steady RANS solve on
# OpenFOAM 12 (the solve alone is ~1 GB/Mcell); a node that is turned away for a
# case it could have held costs a little queue order, one that takes a case it
# cannot hold costs an attempt and an OOM-killed machine.
GB_PER_MCELL = float(os.environ.get("CASEBROKER_GB_PER_MCELL", "2.0"))

# Which of a pending case's meshes the BROKER holds: the case another node
# started, which a node that fetches from the part store can continue.
# A case built on another (cases.needs_case, docs/mrt.md) is handed out only once
# that one is done. Checked in the fresh-case query alone: a resume is this
# worker's own case, which it could only hold if the need was met when it leased.
_NEEDS_MET_SQL = (" AND (cases.needs_case IS NULL OR EXISTS (SELECT 1 FROM cases n"
                  " WHERE n.case_id = cases.needs_case AND n.state = 'done'))")

_MESHED_AT_BROKER_SQL = (
    "EXISTS (SELECT 1 FROM case_parts p JOIN case_blobs b"
    " ON b.case_id = p.case_id AND b.part = 'mesh' AND b.sha256 = p.sha256"
    " WHERE p.case_id = cases.case_id AND p.part = 'mesh')")


@_locked
def lease(conn, worker_id: str, count: int = 1,
          lease_seconds: int = 3600, splits: list[str] | None = None,
          now: int | None = None, host: str | None = None,
          cluster: str | None = None,
          resume_case_ids: list[str] | None = None,
          build: str | None = None, version: str | None = None,
          platform: str | None = None,
          recipes: list[str] | None = None,
          can_continue: bool | None = None,
          can_continue_from_broker: bool | None = None,
          features: list[str] | None = None,
          cpus: int | None = None,
          mem_gb: float | None = None) -> list[Lease]:
    """Atomically claim up to ``count`` cases.

    ``features``, ``cpus`` and ``mem_gb`` (protocol 2) are what the node can do and
    the machine it runs on; all are kept on the worker row. ``continue_from_broker``
    in ``features`` means ``can_continue_from_broker``. The machine decides three
    things here (see `_hardware_sql`): a node that continues from the broker takes
    a case another node started before a fresh one at the same priority -- started
    work first, so a handed-off case does not wait behind the whole queue; a node
    is not handed a case whose known mesh needs more memory than it declared; and,
    when the campaign sets `small_node_cpus`, a node with fewer cores than that is
    handed only cases already meshed at the broker.

    ``recipes`` are the exact recipes this worker can produce. When it declares
    any, it is handed only those: a recipe is the contract a training set is
    partitioned by, and a node that does not know one must never be given it (the
    alternative was measured -- recipe selection by prefix solved a v4 case as v3
    and archived it labelled v4). A worker that declares NOTHING predates
    declarations: it is handed only the campaign's ``undeclared_recipes`` when
    that policy is set (see `_undeclared_recipes`), and anything when it is not.
    `require_build` fences such workers off entirely.

    A DRAINING worker gets no new case, and may still resume its own: that is how
    a node restarts onto a new build in the middle of a case without losing it.

    Expired leases are reclaimed by the same statement that hands out fresh work.
    A status check also clears one that nobody reclaimed for 48 hours, recording
    that cleanup without refunding its consumed attempt.

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
    allowed = list(recipes) if recipes else _undeclared_recipes(conn)
    if allowed is not None:
        recipe_sql = (" AND recipe IN (" + ",".join("?" for _ in allowed) + ")") if allowed \
            else " AND 1 = 0"
        recipe_params = allowed
    # Not a case this machine failed within FAIL_COOLDOWN_SECONDS (see there).
    cooldown_params = [now - FAIL_COOLDOWN_SECONDS, worker_id, host]
    # A case another node started continues on THAT node's mesh, and the only place a
    # node can fetch it from is the broker (the part store, partstore.py): the Syncthing
    # master that used to offer meshes is gone. So a case with a mesh on record goes
    # only to a node that says it can fetch from the broker, and only while the broker
    # holds that mesh -- any other node would give it back, and take it again, forever.
    # `can_continue` is the old "can fetch from the Syncthing master"; true no longer
    # means it can. A node that says neither is from before parts: handed anything, as
    # before (it meshes afresh). Its own case a node may always resume -- the mesh is on
    # its disk, and resume_case_ids is claimed above this filter.
    if can_continue_from_broker is None and features and "continue_from_broker" in features:
        can_continue_from_broker = True
    continue_sql = ""
    if can_continue is not None or can_continue_from_broker is not None:
        continue_sql = (" AND (NOT EXISTS (SELECT 1 FROM case_parts p WHERE p.case_id = cases.case_id"
                        " AND p.part = 'mesh')")
        if can_continue_from_broker:
            continue_sql += " OR " + _MESHED_AT_BROKER_SQL
        continue_sql += ")"
    # Not a case this host handed off within HANDOFF_COOLDOWN_SECONDS.
    handoff_params = [now - HANDOFF_COOLDOWN_SECONDS, worker_id, host]
    hw_sql, hw_params, order_sql = _hardware_sql(conn, can_continue_from_broker, cpus, mem_gb)

    def claim(rows, resumed: bool) -> None:
        for row in rows:
            own = resumed and row["state"] == "leased" and row["lease_worker"] == worker_id
            attempt = row["attempts"] if own else row["attempts"] + 1
            if attempt > row["max_attempts"]:
                # Poison case: its retries are spent. Park it rather than let it
                # cycle forever through every worker in the fleet.
                #
                # And say why in last_error, which is what /v1/errors -- the
                # dashboard's "Copy all errors" -- selects on. A case whose last
                # attempt ended in an expired lease never had a failure to record,
                # so it was quarantined with none and appeared in no error report:
                # v2-00b8665f5c6ecefe (its node replaced mid-solve) and
                # v2-0082073ee09f09f9 (its machine went dark), 2026-09-24. The first
                # line is the same for every such case, so the report groups them.
                held = row["lease_worker"]
                why = ("attempts exhausted (%d): the last attempt ended without a result%s\n"
                       "%sfound by %s" % (row["max_attempts"],
                                           " -- its lease expired" if held else "",
                                           ("held by %s; " % held) if held else "", worker_id))
                conn.execute(
                    "UPDATE cases SET state='quarantined', lease_id=NULL,"
                    " lease_worker=NULL, lease_expires=NULL, leased_at=NULL,"
                    " last_error=?, updated_at=?"
                    " WHERE case_id=?", (why, now, row["case_id"]))
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
            if not resumed:
                # The residual curves of the attempt before are its solves'. A
                # direction the case still HOLDS (case_parts) was solved once
                # and is not solved again -- the node takes the case over from
                # the broker's copy and does the rest -- so its curve is still
                # the one that describes the result, and is kept.
                conn.execute(
                    "DELETE FROM case_residuals WHERE case_id=? AND direction NOT IN"
                    " (SELECT part FROM case_parts WHERE case_id=?)",
                    (row["case_id"], row["case_id"]))
            _event(conn, row["case_id"], worker_id, "resumed" if resumed else "leased",
                   "attempt %d" % attempt, now)
            out.append(Lease(case_id=row["case_id"], lease_id=lease_id,
                             expires_at=expires, spec=json.loads(row["spec"]),
                             attempt=attempt, parts=tuple(_parts_of(conn, row["case_id"]))))

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
                " AND " + own_only + recipe_sql + _RECENTLY_FAILED_HERE_SQL +
                _RECENTLY_HANDED_OFF_HERE_SQL +
                " ORDER BY case_id ASC" + lock_clause,
                [*ids, *own_params, *recipe_params, *cooldown_params, *handoff_params],
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
            params.extend(cooldown_params)
            params.extend(handoff_params)
            params.extend(hw_params)
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
                + split_sql + recipe_sql + continue_sql + _RECENTLY_FAILED_HERE_SQL +
                _RECENTLY_HANDED_OFF_HERE_SQL + hw_sql + _NEEDS_MET_SQL +
                # Every worker targets the same "lowest" rows. That is contention by
                # design, not by accident: under SKIP LOCKED a locked row is simply
                # skipped, and case_id is a hash so the tiebreak is effectively random
                # -- deterministic ordering with no hot spot.
                " ORDER BY priority ASC, " + order_sql + "case_id ASC LIMIT ?" + lock_clause,
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
            " build, version, platform, recipes, features, cpus, mem_gb)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(worker_id) DO UPDATE SET"
            " last_seen=excluded.last_seen, host=excluded.host, cluster=excluded.cluster,"
            " build=excluded.build, version=excluded.version,"
            " platform=excluded.platform, recipes=excluded.recipes,"
            " features=excluded.features, cpus=excluded.cpus, mem_gb=excluded.mem_gb,"
            # Asking for a case is a node past its own check: whatever it said it could
            # not do, it can now.
            " unfit=NULL, unfit_since=NULL",
            (worker_id, host, cluster, now, now, build, version, platform,
             json.dumps(list(recipes)) if recipes else None,
             json.dumps(sorted(set(features))) if features else None, cpus, mem_gb))
        if known is not None and (known["build"] or None) != (build or None):
            _note_build_change(conn, worker_id, known, build, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def _small_node_cpus(conn) -> int:
    """The campaign's `small_node_cpus` (0 = off): below it a node does not mesh."""
    try:
        return max(0, int(_setting(conn, "small_node_cpus") or 0))
    except ValueError:
        return 0


def _hardware_sql(conn, can_continue_from_broker: bool | None, cpus: int | None,
                  mem_gb: float | None) -> tuple[str, list[Any], str]:
    """The machine's part of lease()'s fresh-case query: extra WHERE clauses, their
    parameters, and an ORDER BY term to put before the case_id tiebreak.

    Started work first. A case a node handed off (or a node that died mid-case left)
    holds a mesh and some finished directions at the broker. Ordered only by
    priority and case_id it waits behind every fresh case of its priority -- 5,000
    of them on this campaign -- with ~8 GB of parts sitting in the store and a site
    half answered. So a node that can continue from the broker takes such a case
    first.

    The memory gate needs a cell count, which a site has only once some node meshed
    it (cases.mesh_cells, from `mesh` telemetry): a fresh site passes, and the gate
    catches the retry that would OOM the same way on the next small machine.

    `small_node_cpus` is the operator's: meshing is the heaviest single step and a
    4-core box spends most of a day on snappyHexMesh, so below that many cores a
    node is handed only cases already meshed at the broker. Off unless set.
    """
    where, params = "", []
    if mem_gb and mem_gb > 0 and GB_PER_MCELL > 0:
        where += " AND (cases.mesh_cells IS NULL OR cases.mesh_cells <= ?)"
        params.append(int(mem_gb / GB_PER_MCELL * 1e6))
    small = _small_node_cpus(conn)
    if small and cpus is not None and cpus < small:
        where += " AND " + _MESHED_AT_BROKER_SQL
    order = ("CASE WHEN " + _MESHED_AT_BROKER_SQL + " THEN 0 ELSE 1 END ASC, "
             if can_continue_from_broker else "")
    return where, params, order


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


# A worker that declares no recipes predates declarations, and every such worker
# was built for the wind campaign: casebroker's own worker behind
# runner/run_case.sh, or an E3D node started with --runner. Until 2026-09 every
# recipe in the queue was a CFD recipe, so handing it anything was harmless. The
# Radiance surface-temperature recipe is the first one that is not, and such a
# worker handed one gives it back with exit 69 and STOPS -- harmless to the case,
# the end of a PACE allocation. So the campaign names what an undeclared worker may
# take: None (the policy unset) is anything, as before; [] is nothing at all.
def _undeclared_recipes(conn) -> list[str] | None:
    raw = _setting(conn, "undeclared_recipes")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return [str(r) for r in value] if isinstance(value, list) else None


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


@_locked
def release_row(conn, build: str, platform: str) -> dict[str, Any] | None:
    """One registered file: {build, platform, file, sha256}, or None."""
    row = conn.execute("SELECT build, platform, file, sha256 FROM releases WHERE build = ? AND platform = ?",
                       (build, platform)).fetchone()
    return dict(row) if row else None


@_locked
def release_shas(conn, build: str | None = None) -> set[str]:
    """The content hashes the catalog names -- of one build, or of all of them."""
    if build is None:
        rows = conn.execute("SELECT DISTINCT sha256 FROM releases").fetchall()
    else:
        rows = conn.execute("SELECT DISTINCT sha256 FROM releases WHERE build = ?", (build,)).fetchall()
    return {r["sha256"] for r in rows}


@_locked
def release_files_to_drop(conn, keep_builds: int, now: int | None = None) -> list[str]:
    """The release files the broker may stop holding: those of builds older than the
    newest ``keep_builds`` that nothing needs.

    A build's files are ~535 MB (three platforms) and every push to Eddy3D ``dev``
    publishes one, so holding every build ever registered fills the store in weeks.
    Kept whatever their age: the fleet's target, the target before it (a roll back
    goes there, and has to be able to fetch it), a canary's target, and what a live
    worker runs (release_in_use). A hash a kept build or a case's part shares is
    never listed. The catalog rows stay either way -- a build can be uploaded again.
    """
    now = now or _now()
    builds = [r["build"] for r in conn.execute(
        "SELECT build, MAX(added_at) AS at FROM releases GROUP BY build ORDER BY at DESC, build DESC").fetchall()]
    keep = set(builds[:max(0, keep_builds)])
    previous = _setting(conn, "previous_target")
    if previous:
        keep.add(previous)
    keep |= {b for b in builds if b not in keep and release_in_use(conn, b, now=now)}
    rows = conn.execute("SELECT build, sha256 FROM releases").fetchall()
    held_elsewhere = {r["sha256"] for r in rows if r["build"] in keep}
    held_elsewhere |= {r["sha256"] for r in conn.execute("SELECT DISTINCT sha256 FROM case_blobs").fetchall()}
    return sorted({r["sha256"] for r in rows if r["build"] not in keep and r["sha256"] not in held_elsewhere})


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
        "undeclared_recipes": _undeclared_recipes(conn),
        "small_node_cpus": _small_node_cpus(conn),
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
                 state: str | None = None, now: int | None = None,
                 unfit: str | None = None, cpu: float | None = None) -> dict[str, Any]:
    """What one node should be running, and whether it may take new work.

    Asked before every lease and during a solve. The answer names a build, the
    file it is on the release share under and the hash that file must have; the
    node does the rest.

    `state` is what the node says about the move it was last told to make --
    waiting for the file, installed and verified, switching after the case --
    kept until it is on target. Without it an operator who set a target saw
    every worker as "behind" and nothing about whether anything was happening.

    `unfit` is why the node will not take a case at all -- its own check refused:
    a full disk, a container engine that is not running, no MPI. Kept, with when
    it began, until an ask says nothing or the node leases; the moment it begins
    is an event (and a push notice). A node that has never leased gets a worker
    row for it: a machine that cannot run from its first start is exactly the
    one nobody would otherwise see.

    `cpu` is how busy the whole machine is, in percent, as the node measured it since
    its last ask: kept with its time. A value that is not a percentage is dropped --
    a measurement must never be what fails the ask.
    """
    now = now or _now()
    try:
        cpu = float(cpu) if cpu is not None else None
    except (TypeError, ValueError):
        cpu = None
    if cpu is not None and not (math.isfinite(cpu) and -0.5 <= cpu <= 100.5):
        cpu = None
    unfit = (unfit or "").strip()[:300] or None
    if unfit:
        conn.execute(
            "INSERT INTO workers(worker_id, first_seen, last_seen, build, platform)"
            " VALUES (?,?,?,?,?) ON CONFLICT(worker_id) DO NOTHING",
            (worker_id, now, now, build, platform))
    w = conn.execute(
        "SELECT drain, drain_reason, target_build, update_failed, unfit FROM workers WHERE worker_id = ?",
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
        # Why it will not take a case. The text moves on every ask (a disk's free GB),
        # so the spell is dated from its first ask, and only its start is an event.
        if unfit and not w["unfit"]:
            conn.execute("UPDATE workers SET unfit = ?, unfit_since = ? WHERE worker_id = ?",
                         (unfit, now, worker_id))
            _event(conn, None, worker_id, "unfit", unfit, now)
        elif unfit:
            conn.execute("UPDATE workers SET unfit = ? WHERE worker_id = ?", (unfit, worker_id))
        elif w["unfit"]:
            conn.execute("UPDATE workers SET unfit = NULL, unfit_since = NULL WHERE worker_id = ?",
                         (worker_id,))
        if cpu is not None:
            conn.execute("UPDATE workers SET cpu_pct = ?, cpu_at = ? WHERE worker_id = ?",
                         (round(min(100.0, max(0.0, cpu)), 1), now, worker_id))
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


def _not_theirs(row, worker_ok: Callable[[str | None], bool] | None) -> bool:
    """Whether a per-machine credential is asking about a lease it does not hold.

    `worker_ok` is the route's "may this credential act as that worker" test
    (app._may_lease_as), passed only for a per-machine token. A lease_id is no
    secret -- the case list used to show it to every reader -- so without this a
    machine's token could heartbeat, complete, fail or release ANOTHER machine's
    case: quarantine it with retryable=false, or mark it done with a result that
    never ran. /v1/lease, telemetry, parts and fields already held the line; these
    four did not. Answered exactly like a lease that is gone (409), because to the
    caller it is one: not its case, stop.
    """
    return worker_ok is not None and not worker_ok(row["lease_worker"])


@_locked
def heartbeat(conn, lease_id: str, lease_seconds: int = 3600,
              detail: str | None = None, now: int | None = None,
              worker_ok: Callable[[str | None], bool] | None = None,
              stage: str | None = None) -> bool:
    """Extend a lease. Returns False when the lease is gone -- the worker must
    then STOP working that case, because someone else may already own it.

    ``stage`` is the stage the node says ``detail`` belongs to (protocol 2): kept
    with the progress event, and preferred by stages.from_events over reading it
    off the text -- the node writes the line, so it knows; the broker can only
    guess from a grammar three codebases had to keep in step."""
    now = now or _now()
    row = _by_lease(conn, lease_id)
    if row is None or _not_theirs(row, worker_ok):
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
            _event(conn, row["case_id"], row["lease_worker"], "progress", detail, now,
                   stage=stage)
    return True


@_locked
def complete(conn, lease_id: str, result_uri: str,
             sha256: str | None = None, nbytes: int | None = None,
             metrics: dict[str, Any] | None = None, now: int | None = None,
             case_id: str | None = None,
             worker_ok: Callable[[str | None], bool] | None = None) -> bool:
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is not None and _not_theirs(row, worker_ok):
            # A live lease another machine holds: never the retry path below,
            # which would confirm a result for a case this caller never ran.
            conn.execute("ROLLBACK")
            return False
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
# What an Eddy3D node gives as the reason when it hands a case back because the
# machine cannot run it (NodeWorker.GiveBackAndStop): its engine went away, its
# install broke, or -- since Eddy3D #942 -- its scratch cannot be cleared.
_MACHINE_RELEASE = "node cannot run cases"

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
         now: int | None = None,
         worker_ok: Callable[[str | None], bool] | None = None) -> bool:
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None or _not_theirs(row, worker_ok):
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
        _drain_on_burst(conn, row["lease_worker"], error, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


# A worker that fails case after case is a broken MACHINE, not a queue of broken
# sites. A stopped Docker daemon (COD-PKAST-7865, 2026-09-19), leftover processes
# (COD-359-38, 2026-09-23) and a full disk (COD-358-21, 2026-09-26: 663 cases in 38
# minutes, the node leasing the next one every four seconds and charging each an
# attempt) all looked exactly like this, and a healthy node fails a few cases a DAY.
# So a worker that reports FAIL_BURST_CASES different cases failed within
# FAIL_BURST_SECONDS is drained -- no new case at its next lease, its own case still
# resumable -- with the reason on the workers table, until an operator undrains it.
# Only the broker sees the burst, so this works for every node build, old ones
# included. A lease-time "attempts exhausted" quarantine is not counted: it is named
# after a worker that has usually just died, and would drain it when it came back.
FAIL_BURST_CASES = int(os.environ.get("CASEBROKER_FAIL_BURST_CASES", "5"))
FAIL_BURST_SECONDS = int(os.environ.get("CASEBROKER_FAIL_BURST_SECONDS", "600"))
_EXHAUSTED_AT_LEASE = "attempts exhausted%"


def _drain_on_burst(conn, worker_id: str | None, error: str, now: int) -> bool:
    """Drain `worker_id` if this failure completes a burst (see FAIL_BURST_CASES).
    Inside the caller's transaction; True when it drained the worker just now."""
    if not worker_id or FAIL_BURST_CASES <= 0:
        return False
    # An undrain starts the count over. The operator has looked at the machine,
    # and the burst that drained it must not drain it again at its next failure.
    since = now - FAIL_BURST_SECONDS
    undrained = conn.execute("SELECT MAX(ts) AS t FROM events WHERE event = 'undrain'"
                             " AND detail = ?", (worker_id,)).fetchone()["t"]
    if undrained is not None:
        since = max(since, undrained)
    n = conn.execute(
        "SELECT COUNT(DISTINCT case_id) AS n FROM events WHERE worker_id = ?"
        " AND event IN ('failed', 'quarantined') AND ts > ?"
        " AND (detail IS NULL OR detail NOT LIKE ?)",
        (worker_id, since, _EXHAUSTED_AT_LEASE)).fetchone()["n"]
    if n < FAIL_BURST_CASES:
        return False
    # Read in the node's log ("this node is DRAINING (<reason>)") and on the
    # dashboard's Drained badge, so it names the rule and the last error.
    last = (error or "").strip().split("\n")[0][:160]
    reason = ("drained by the broker: %d cases failed here within %d min, which points at "
              "this machine, not the sites; last error: %s"
              % (n, max(1, FAIL_BURST_SECONDS // 60), last))
    if not conn.execute("UPDATE workers SET drain = 1, drain_reason = ? WHERE worker_id = ?"
                        " AND drain = 0", (reason, worker_id)).rowcount:
        return False
    _event(conn, None, None, "drain", "%s: %s" % (worker_id, reason), now)
    return True


@_locked
def release(conn, lease_id: str, reason: str = "released",
            now: int | None = None,
            worker_ok: Callable[[str | None], bool] | None = None,
            handoff: bool = False) -> bool:
    """Hand a case back untouched, without burning a retry.

    This is the preemption path: a SIGTERM'd worker calls it and the case becomes
    available immediately instead of sitting unavailable until its TTL runs out.
    The attempt is refunded because nothing about the case was wrong.

    ``handoff`` is a node that solved its share of the case on purpose -- its
    `--max-directions` or `--chunk-hours` -- and passes the rest on (protocol 2,
    "sequential chunks"). Recorded as `handed-off`, not `released`, because
    lease() then keeps the case from every worker on that host for
    HANDOFF_COOLDOWN_SECONDS: otherwise the node that just let go asks again at
    once and is handed its own case straight back, which is no handoff at all.
    """
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None or _not_theirs(row, worker_ok):
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
        _event(conn, row["case_id"], row["lease_worker"],
               "handed-off" if handoff else "released", reason, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


# -- parts: what of a case already reached the master --------------------------
#
# A node ships a case while it runs: <case>.mesh.tar.gz once meshing passed,
# <case>.case_NNN.tar.gz as each direction finishes (Eddy3D CaseParts), and it
# reports each one here. Before, a machine switched off mid-case took every
# finished direction with it (COD-359-38, 2026-09-24: 7 of 32, lost). With the
# parts on record, whichever node leases the case next is told what exists, takes
# the master's copy of the mesh and solves only the rest.
#
# A direction is only worth keeping WITH the mesh it was solved on: snappyHexMesh
# on another machine, or another rank count, is a different mesh, and one case
# must never be answered on two. So each direction carries its mesh's sha256, a
# direction reported against a mesh that is no longer the case's is refused, and
# a NEW mesh for the case deletes every part recorded for the old one.

PART_NAME = re.compile(r"(mesh|case_[A-Za-z0-9_-]{1,32})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
ARCHIVE_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}\.tar\.gz")


def _parts_of(conn, case_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT part, archive, sha256, bytes, mesh_sha256, verdict, worker_id, reported_at"
        " FROM case_parts WHERE case_id=?", (case_id,)).fetchall()
    # Which of them the broker itself holds, as reported: a node continuing the case
    # can then fetch the mesh from the broker rather than wait on the master.
    held = {(r["part"], r["sha256"]) for r in conn.execute(
        "SELECT part, sha256 FROM case_blobs WHERE case_id=?", (case_id,)).fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        d["at_broker"] = (d["part"], (d["sha256"] or "").lower()) in held
        try:
            d["verdict"] = json.loads(d["verdict"]) if d["verdict"] else None
        except (TypeError, ValueError):
            d["verdict"] = None
        out.append(d)
    # The mesh first, then the directions in order.
    return sorted(out, key=lambda r: (r["part"] != "mesh", r["part"]))


@_locked
def case_parts(conn, case_id: str) -> list[dict[str, Any]]:
    return _parts_of(conn, case_id)


@_locked
def report_part(conn, lease_id: str, case_id: str, part: str, archive: str,
                sha256: str, size: int | None = None, mesh_sha256: str | None = None,
                verdict: dict[str, Any] | None = None, now: int | None = None,
                worker_ok: Callable[[str | None], bool] | None = None) -> str:
    """Record one part the lease holder shipped to the master.

    Returns ``"ok"``; ``"gone"`` (not this case's current lease, or not the
    caller's -- the same ownership rule as post_telemetry); ``"invalid"`` (a part
    name, archive name or hash that is not one); or ``"stale_mesh"`` (a
    direction solved on a mesh that is no longer this case's: it is not kept).
    """
    sha256 = (sha256 or "").lower()
    mesh_sha256 = (mesh_sha256 or "").lower() or None
    if not PART_NAME.fullmatch(part or "") or not ARCHIVE_NAME.fullmatch(archive or "") \
            or not _SHA256.fullmatch(sha256) or (mesh_sha256 and not _SHA256.fullmatch(mesh_sha256)) \
            or (size is not None and size < 0):
        return "invalid"
    verdict_json = None
    if verdict is not None:
        outcome, cleaned = prepare_telemetry("verdict", verdict)
        if outcome != "ok":
            return "invalid"
        verdict_json = _telemetry_json(cleaned)
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None or row["case_id"] != case_id \
                or (worker_ok is not None and not worker_ok(row["lease_worker"])):
            conn.execute("ROLLBACK")
            return "gone"
        mesh = conn.execute("SELECT sha256 FROM case_parts WHERE case_id=? AND part='mesh'",
                            (case_id,)).fetchone()
        if part == "mesh":
            if mesh is not None and mesh["sha256"] != sha256:
                gone = conn.execute("SELECT COUNT(*) AS n FROM case_parts WHERE case_id=?",
                                    (case_id,)).fetchone()["n"]
                conn.execute("DELETE FROM case_parts WHERE case_id=?", (case_id,))
                # What the broker holds of the old mesh is no longer this case's;
                # its files go at the next sweep (unreferenced_blobs). Nor are the
                # residual curves of the directions solved on it.
                conn.execute("DELETE FROM case_blobs WHERE case_id=? AND part != 'archive'", (case_id,))
                conn.execute("DELETE FROM case_residuals WHERE case_id=?", (case_id,))
                _event(conn, case_id, row["lease_worker"], "parts_reset",
                       "a new mesh; %d part(s) of the old one dropped" % gone, now)
            mesh_sha256 = sha256
        elif mesh is not None and mesh_sha256 != mesh["sha256"]:
            conn.execute("ROLLBACK")
            return "stale_mesh"
        conn.execute(
            "INSERT INTO case_parts(case_id, part, archive, sha256, bytes, mesh_sha256,"
            " verdict, worker_id, reported_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(case_id, part) DO UPDATE SET archive=excluded.archive,"
            " sha256=excluded.sha256, bytes=excluded.bytes, mesh_sha256=excluded.mesh_sha256,"
            " verdict=excluded.verdict, worker_id=excluded.worker_id, reported_at=excluded.reported_at",
            (case_id, part, archive, sha256, size, mesh_sha256, verdict_json, row["lease_worker"], now))
        _event(conn, case_id, row["lease_worker"], "part_shipped", part, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "ok"


@_locked
def reset_parts(conn, case_id: str, by: str | None = None, now: int | None = None) -> int:
    """Forget what a case shipped, so the next node meshes it afresh. For a master
    that is gone for good: a node will not solve a case on a mesh it cannot get."""
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM case_parts WHERE case_id=?",
                         (case_id,)).fetchone()["n"]
        conn.execute("DELETE FROM case_parts WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM case_blobs WHERE case_id=? AND part != 'archive'", (case_id,))
        conn.execute("DELETE FROM case_residuals WHERE case_id=?", (case_id,))
        if n:
            _event(conn, case_id, by, "parts_reset", "%d part(s) dropped by an admin" % n, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n



# -- the pedestrian wind field: the answer itself, not a pointer to it -------------
#
# One row per (case, direction, height): |U| on the case's grid, as the node read it
# off OpenFOAM's surface sample the moment the direction finished. The blob is the
# umag/1 container the node built (self-describing, gzip), stored verbatim: what the
# broker checks is that it IS one -- magic, version, a header naming a grid whose
# nx*ny float32 values are exactly the bytes that follow -- and the header is what
# fills the columns a browser filters and sorts by, so they cannot disagree with it.

UMAG_MAGIC = b"UMAG"
UMAG_VERSION = 1
# A 504 x 504 float32 field is 1.0 MB; gzip takes it to ~0.8. A blob this far past
# that is not a pedestrian field, whatever its header says.
FIELD_MAX_BYTES = int(os.environ.get("CASEBROKER_FIELD_MAX_BYTES", str(64 * 1024 * 1024)))
FIELD_MAX_POINTS = 4_000_000
_FIELD_HEADER_KEYS = ("nx", "ny", "x0", "y0", "spacing_m", "height_m")
_FIELD_HEADER_MAX = 1_000_000
# The largest container a field can be: magic, version and header length, the
# largest header, the largest grid. A gzip stream that inflates past it is
# refused there, not after it has been inflated: 64 MB of gzip can be gigabytes.
_FIELD_RAW_MAX = 12 + _FIELD_HEADER_MAX + 4 * FIELD_MAX_POINTS
_GZIP_MAGIC = b"\x1f\x8b"


def umag_container(blob: bytes) -> bytes:
    """The umag/1 container of a blob as a node sends it -- gzip-wrapped (every
    node so far) or as it is -- WITHOUT the gzip: the form the broker stores.

    The broker keeps fields uncompressed (Patrick, 2026-10-06). Measured on the
    campaign's own fields, gzip saved 13% of a float32 field and Postgres's own
    compression nothing at all, while the uncompressed form can be read in place
    -- numpy.frombuffer, or a single cell straight out of SQL with substring() --
    without inflating a megabyte first.
    """
    import zlib
    if len(blob) > FIELD_MAX_BYTES:
        raise ValueError(f"field is {len(blob)} bytes; the limit is {FIELD_MAX_BYTES}")
    if blob[:4] == UMAG_MAGIC:
        return blob
    if blob[:2] != _GZIP_MAGIC:
        raise ValueError("not a gzip stream or a UMAG container")
    inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = inflate.decompress(blob, _FIELD_RAW_MAX + 1)
    except zlib.error as exc:
        raise ValueError(f"not a gzip stream: {exc}") from None
    if len(raw) > _FIELD_RAW_MAX or inflate.unconsumed_tail:
        raise ValueError(f"the gzip stream inflates past {_FIELD_RAW_MAX} bytes, "
                         "more than any field this broker stores")
    if not inflate.eof:
        raise ValueError("not a gzip stream: it ends before its end")
    return raw


def decode_umag(blob: bytes) -> dict[str, Any]:
    """The header of a umag/1 blob, checked against its body; ValueError otherwise.

    Reads the CONTAINER only -- header and byte count -- never the values: the
    broker stores what the node sampled and does not re-derive it. Takes the blob
    gzip-wrapped or not, and returns the header with ``nbytes`` (the size of the
    container, uncompressed) added.
    """
    import struct
    raw = umag_container(blob)
    if len(raw) < 12 or raw[:4] != UMAG_MAGIC:
        raise ValueError("not a wind-field blob (no UMAG header)")
    version, hlen = struct.unpack("<II", raw[4:12])
    if version != UMAG_VERSION:
        raise ValueError(f"umag version {version}; this broker reads version {UMAG_VERSION}")
    if hlen > _FIELD_HEADER_MAX or 12 + hlen > len(raw):
        raise ValueError("header length runs past the blob")
    try:
        header = json.loads(raw[12:12 + hlen].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"header is not JSON: {exc}") from None
    if not isinstance(header, dict) or header.get("format") != "umag/1":
        raise ValueError("header does not say format umag/1")
    for k in _FIELD_HEADER_KEYS:
        if k not in header:
            raise ValueError(f"header has no {k!r}")
    try:
        nx, ny = int(header["nx"]), int(header["ny"])
        for k in ("x0", "y0", "spacing_m", "height_m"):
            header[k] = float(header[k])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"header grid is not numeric: {exc}") from None
    if nx <= 0 or ny <= 0 or nx * ny > FIELD_MAX_POINTS:
        raise ValueError(f"grid {nx} x {ny} is not one this broker stores")
    if header["spacing_m"] <= 0 or not math.isfinite(header["spacing_m"]):
        raise ValueError("spacing_m must be a positive number")
    body = len(raw) - 12 - hlen
    if body != 4 * nx * ny:
        raise ValueError(f"body is {body} bytes; a {nx} x {ny} float32 field is {4 * nx * ny}")
    for k in ("deg", "coverage", "u_ref", "umag_p999"):
        v = header.get(k)
        if v is not None:
            try:
                header[k] = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"header {k!r} is not a number") from None
            if not math.isfinite(header[k]):
                header[k] = None
    header["nbytes"] = len(raw)
    return header


def put_field(conn, lease_id: str, case_id: str, direction: str, blob: bytes,
              now: int | None = None,
              worker_ok: Callable[[str | None], bool] | None = None) -> str:
    """Store one direction's pedestrian field for the lease holder's case.

    Returns ``"ok"``; ``"gone"`` (not this case's current lease, or not the
    caller's -- the ownership rule of report_part); or ``"invalid: <why>"`` for
    a direction name or blob that is not one. A second upload for the same
    (case, direction, height) replaces the first: a direction solved again is a
    different field.

    Stored UNCOMPRESSED (umag_container), whichever way it was sent; ``sha256``
    and ``bytes`` describe what is stored. Rows from before are gzip-wrapped and
    stay so -- the first two bytes say which, and the dashboard reads both.

    The broker summarises the field as it stores it (``field_summary``: where its
    values start, and its statistics), outside the lock every lease waits on.
    """
    if not PART_NAME.fullmatch(direction or "") or direction == "mesh":
        return "invalid: not a direction name"
    try:
        header = decode_umag(blob)
        blob = umag_container(blob)
    except ValueError as exc:
        return f"invalid: {exc}"
    if header.get("direction") not in (None, direction):
        return f"invalid: the blob says it is {header['direction']!r}, not {direction!r}"
    if header.get("case_id") not in (None, case_id):
        return f"invalid: the blob says it is {header['case_id']!r}, not {case_id!r}"
    import hashlib
    sha = hashlib.sha256(blob).hexdigest()
    summary = field_summary(blob)
    return _store_field(conn, lease_id, case_id, direction, header, blob, sha, summary,
                        now or _now(), worker_ok)


#: The columns field_summary fills, in INSERT order.
_FIELD_SUMMARY_COLUMNS = ("data_offset", "n_valid") + tuple("umag_" + k for k in (
    "mean", "min", "p05", "p25", "p50", "p75", "p95", "p99", "max"))


def field_summary(blob: bytes) -> dict[str, Any]:
    """What the broker derives from a stored field: ``data_offset`` (None for a
    gzip-wrapped row, which cannot be read in place) and its statistics
    (casebroker/umag.py, ``stats``). Takes the blob as it is stored."""
    from . import umag
    raw = umag_container(blob)
    _, field = umag.values(raw)
    out = umag.stats(field)
    out["data_offset"] = umag.data_offset(raw) if blob[:4] == UMAG_MAGIC else None
    return {k: out[k] for k in _FIELD_SUMMARY_COLUMNS}


@_locked
def _store_field(conn, lease_id: str, case_id: str, direction: str, header: dict[str, Any],
                 blob: bytes, sha: str, summary: dict[str, Any], now: int,
                 worker_ok: Callable[[str | None], bool] | None) -> str:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _by_lease(conn, lease_id)
        if row is None or row["case_id"] != case_id \
                or (worker_ok is not None and not worker_ok(row["lease_worker"])):
            conn.execute("ROLLBACK")
            return "gone"
        _insert_field(conn, case_id, direction, header, blob, sha, summary, row["lease_worker"], now)
        _event(conn, case_id, row["lease_worker"], "field_stored",
               "%s @ %g m, %d bytes" % (direction, header["height_m"], len(blob)), now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "ok"


def _insert_field(conn, case_id: str, direction: str, header: dict[str, Any], blob: bytes, sha: str,
                  summary: dict[str, Any], worker_id: str | None, now: int) -> None:
    """One field row, replacing the same (case, direction, height). Inside the caller's transaction."""
    extra = ", ".join(_FIELD_SUMMARY_COLUMNS)
    conn.execute(
        "INSERT INTO case_fields(case_id, direction, height_m, deg, nx, ny, x0, y0,"
        " spacing_m, coverage, u_ref, umag_p999, sha256, bytes, blob, worker_id, reported_at, "
        + extra + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?"
        + ",?" * len(_FIELD_SUMMARY_COLUMNS) + ")"
        " ON CONFLICT(case_id, direction, height_m) DO UPDATE SET deg=excluded.deg,"
        " nx=excluded.nx, ny=excluded.ny, x0=excluded.x0, y0=excluded.y0,"
        " spacing_m=excluded.spacing_m, coverage=excluded.coverage, u_ref=excluded.u_ref,"
        " umag_p999=excluded.umag_p999, sha256=excluded.sha256, bytes=excluded.bytes,"
        " blob=excluded.blob, worker_id=excluded.worker_id, reported_at=excluded.reported_at, "
        + ", ".join("%s=excluded.%s" % (c, c) for c in _FIELD_SUMMARY_COLUMNS),
        (case_id, direction, header["height_m"], header.get("deg"), int(header["nx"]),
         int(header["ny"]), header["x0"], header["y0"], header["spacing_m"],
         header.get("coverage"), header.get("u_ref"), header.get("umag_p999"), sha, len(blob), blob,
         worker_id, now) + tuple(summary[c] for c in _FIELD_SUMMARY_COLUMNS))


def backfill_field(conn, case_id: str, direction: str, blob: bytes,
                   may_act_as: Callable[[str | None], bool] | None = None,
                   now: int | None = None) -> str:
    """Store the pedestrian field of a DONE case that has none for this direction, from
    the archive the node that solved it still holds -- a case finished by a build from
    before fields went to the broker, or whose field did not get through.

    Returns ``"ok"``; ``"exists"`` (the case has this field: a solve's own is never
    replaced); ``"not_done"``; ``"no_case"``; ``"forbidden"`` (``may_act_as`` accepts
    neither the worker that completed the case nor the one that reported this
    direction's part -- the machines whose disk holds it); or ``"invalid: <why>"``.
    ``may_act_as`` None is a caller who may (an admin, the fleet's shared token)."""
    if not PART_NAME.fullmatch(direction or "") or direction == "mesh":
        return "invalid: not a direction name"
    try:
        header = decode_umag(blob)
        blob = umag_container(blob)
    except ValueError as exc:
        return f"invalid: {exc}"
    if header.get("direction") not in (None, direction):
        return f"invalid: the blob says it is {header['direction']!r}, not {direction!r}"
    if header.get("case_id") not in (None, case_id):
        return f"invalid: the blob says it is {header['case_id']!r}, not {case_id!r}"
    import hashlib
    sha = hashlib.sha256(blob).hexdigest()
    summary = field_summary(blob)
    return _backfill_field(conn, case_id, direction, header, blob, sha, summary, may_act_as, now or _now())


@_locked
def _backfill_field(conn, case_id: str, direction: str, header: dict[str, Any], blob: bytes, sha: str,
                    summary: dict[str, Any], may_act_as: Callable[[str | None], bool] | None, now: int) -> str:
    conn.execute("BEGIN IMMEDIATE")
    try:
        case = conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if case is None or case["state"] != "done":
            conn.execute("ROLLBACK")
            return "no_case" if case is None else "not_done"
        if conn.execute("SELECT 1 FROM case_fields WHERE case_id=? AND direction=? AND height_m=?",
                        (case_id, direction, header["height_m"])).fetchone():
            conn.execute("ROLLBACK")
            return "exists"
        done = conn.execute("SELECT worker_id FROM events WHERE case_id=? AND event='done'"
                            " ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
        part = conn.execute("SELECT worker_id FROM case_parts WHERE case_id=? AND part=?",
                            (case_id, direction)).fetchone()
        holders = [r["worker_id"] for r in (done, part) if r is not None and r["worker_id"]]
        if may_act_as is not None and not any(may_act_as(w) for w in holders):
            conn.execute("ROLLBACK")
            return "forbidden"
        worker = next((w for w in holders if may_act_as is None or may_act_as(w)), None)
        _insert_field(conn, case_id, direction, header, blob, sha, summary, worker, now)
        _event(conn, case_id, worker, "field_stored",
               "%s @ %g m, %d bytes, backfilled from the archive" % (direction, header["height_m"], len(blob)), now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "ok"


# What a field record carries, without its bytes: the grid, the node's own numbers,
# and the statistics the broker computed (field_summary) -- not data_offset, which is
# how the broker reads the blob and nobody else's business.
_FIELD_STATS_COLUMNS = _FIELD_SUMMARY_COLUMNS[1:]
_FIELD_RECORD = ("direction, height_m, deg, nx, ny, x0, y0, spacing_m, coverage, u_ref, umag_p999, "
                 + ", ".join(_FIELD_STATS_COLUMNS) + ", sha256, bytes, worker_id, reported_at")
#: The published pedestrian height, and how far from it a field is still "the" one.
FIELD_HEIGHT_M = 1.75


@_locked
def case_fields(conn, case_id: str) -> list[dict[str, Any]]:
    """What fields a case has, without the bytes: one record per (direction, height),
    directions in order, the lower height first."""
    rows = conn.execute("SELECT " + _FIELD_RECORD + " FROM case_fields WHERE case_id=?",
                        (case_id,)).fetchall()
    out = [dict(r) for r in rows]
    return sorted(out, key=lambda r: (r["direction"], r["height_m"]))


@_locked
def case_field(conn, case_id: str, direction: str, height_m: float | None = None):
    """One field's blob and its record, or None. Without a height, the one nearest
    1.75 m (the published pedestrian height)."""
    rows = conn.execute(
        "SELECT " + _FIELD_RECORD + ", blob FROM case_fields WHERE case_id=? AND direction=?",
        (case_id, direction)).fetchall()
    if not rows:
        return None
    best = choose_heights([dict(r) for r in rows], height_m)
    if not best:
        return None
    d = best[0]
    d["blob"] = bytes(d["blob"])
    return d


def choose_heights(rows: list[dict[str, Any]], height_m: float | None = None) -> list[dict[str, Any]]:
    """One record per direction: the one at ``height_m`` exactly (directions without
    one are left out), or without a height the one nearest the published 1.75 m.
    Directions in order."""
    target = FIELD_HEIGHT_M if height_m is None else float(height_m)
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = abs(float(r["height_m"]) - target)
        if height_m is not None and d > 1e-6:
            continue
        have = best.get(r["direction"])
        if have is None or d < abs(float(have["height_m"]) - target):
            best[r["direction"]] = r
    return [best[k] for k in sorted(best)]


# -- asking the field questions (casebroker/umag.py does the arithmetic) ----------------
#
# A field is a megabyte and a campaign is 160,000 of them, so nothing here reads more
# of one than the question needs. A point is four float32 values and a region a band
# of rows: substr() of the blob, which Postgres serves from the TOAST chunks that hold
# those bytes because the column is STORAGE EXTERNAL (UNCOMPRESSED_COLUMNS). A row
# stored gzip-wrapped (data_offset NULL) is read whole and inflated. Every call takes
# the lock once per DIRECTION at most, so a lease never waits behind a whole case.

@_locked
def field_lattices(conn, case_id: str) -> list[dict[str, Any]]:
    """Every field of a case as its grid and where its values start, without bytes."""
    return [dict(r) for r in conn.execute(
        "SELECT direction, height_m, deg, nx, ny, x0, y0, spacing_m, u_ref, data_offset"
        " FROM case_fields WHERE case_id=?", (case_id,)).fetchall()]


@_locked
def field_ranges(conn, case_id: str, directions: list[str],
                 ranges: list[tuple[int, int]]) -> dict[tuple[str, float], list[bytes]]:
    """Byte ranges of the VALUES of several fields of a case at once: each range is
    (offset from the first value, length). Rows without a data_offset are left out.
    Keyed by (direction, height_m); every height of the named directions is read, so
    the caller keeps the ones whose lattice the ranges were computed for."""
    if not directions or not ranges:
        return {}
    cols = ", ".join("substr(blob, CAST(data_offset + ? AS INTEGER), ?) AS r%d" % k for k in range(len(ranges)))
    params: list[Any] = []
    for off, n in ranges:
        params.extend([off + 1, n])                      # substr counts from 1
    marks = ",".join("?" * len(directions))
    rows = conn.execute(
        "SELECT direction, height_m, " + cols + " FROM case_fields"
        " WHERE case_id=? AND data_offset IS NOT NULL AND direction IN (" + marks + ")",
        params + [case_id] + list(directions)).fetchall()
    return {(r["direction"], float(r["height_m"])): [bytes(r["r%d" % k]) for k in range(len(ranges))]
            for r in rows}


@_locked
def fields_without_stats(conn, limit: int = 200) -> list[dict[str, Any]]:
    """Fields stored before the broker summarised them, oldest first."""
    return [dict(r) for r in conn.execute(
        "SELECT case_id, direction, height_m FROM case_fields WHERE n_valid IS NULL"
        " ORDER BY reported_at, case_id, direction LIMIT ?", (int(limit),)).fetchall()]


@_locked
def _field_blob(conn, case_id: str, direction: str, height_m: float):
    row = conn.execute(
        "SELECT sha256, blob FROM case_fields WHERE case_id=? AND direction=? AND ABS(height_m - ?) < 1e-6",
        (case_id, direction, float(height_m))).fetchone()
    return None if row is None else (row["sha256"], bytes(row["blob"]))


@_locked
def _set_field_summary(conn, case_id: str, direction: str, height_m: float, sha: str,
                       summary: dict[str, Any]) -> bool:
    # Only if the field is still the one summarised: a direction stored again in
    # between has its own summary already, written with it.
    cur = conn.execute(
        "UPDATE case_fields SET " + ", ".join("%s=?" % c for c in _FIELD_SUMMARY_COLUMNS)
        + " WHERE case_id=? AND direction=? AND ABS(height_m - ?) < 1e-6 AND sha256=?",
        tuple(summary[c] for c in _FIELD_SUMMARY_COLUMNS) + (case_id, direction, float(height_m), sha))
    return (cur.rowcount or 0) > 0


def summarise_stored_fields(conn, limit: int = 200) -> dict[str, int]:
    """Compute data_offset and the statistics of up to ``limit`` fields stored before
    the broker did so on arrival. One field per locked read and per locked write, the
    arithmetic between them unlocked. ``remaining`` counts what is still without."""
    done = failed = 0
    for row in fields_without_stats(conn, limit):
        got = _field_blob(conn, row["case_id"], row["direction"], row["height_m"])
        if got is None:
            continue
        sha, blob = got
        try:
            summary = field_summary(blob)
        except (ValueError, KeyError) as exc:
            print("[fields] %s/%s @ %s m: cannot summarise: %s" % (row["case_id"], row["direction"],
                                                                     row["height_m"], exc), file=sys.stderr)
            failed += 1
            continue
        done += _set_field_summary(conn, row["case_id"], row["direction"], row["height_m"], sha, summary)
    left = _count_fields_without_stats(conn)
    return {"summarised": done, "failed": failed, "remaining": left}


@_locked
def _count_fields_without_stats(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM case_fields WHERE n_valid IS NULL").fetchone()["n"])


#: What GET /v1/fields filters and sorts on. ``vr_<stat>`` is ``umag_<stat>`` / u_ref.
FIELD_QUERY_NUMBERS = ("deg", "height_m", "coverage", "u_ref", "n_valid", "umag_p999") + tuple(
    c for c in _FIELD_STATS_COLUMNS if c != "n_valid")
_FIELD_VR = tuple("vr_" + c[len("umag_"):] for c in FIELD_QUERY_NUMBERS if c.startswith("umag_"))
_WHERE_RE = re.compile(r"^\s*([a-z0-9_]+)\s*(<=|>=|!=|<|>|=)\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$")


def _field_expr(key: str) -> str:
    """The SQL for one queryable number, or ValueError naming what is."""
    if key in FIELD_QUERY_NUMBERS:
        return "f." + key
    if key in _FIELD_VR:
        # Guarded rather than divided: Postgres raises on a division by zero where
        # SQLite answers NULL, and a field without u_ref has no ratio on either.
        return "(CASE WHEN f.u_ref > 0 THEN f.umag_%s / f.u_ref END)" % key[len("vr_"):]
    raise ValueError("%r is not a field number; use one of %s" % (key, ", ".join(FIELD_QUERY_NUMBERS + _FIELD_VR)))


def parse_field_where(clause: str) -> tuple[str, str, float]:
    """``"vr_p95>=1.2"`` -> ("vr_p95", ">=", 1.2); ValueError for anything else."""
    m = _WHERE_RE.match(clause or "")
    if not m:
        raise ValueError("%r is not <number><op><value>, e.g. vr_p95>=1.2 (ops < <= = != >= >)" % clause)
    key, op, value = m.group(1), m.group(2), float(m.group(3))
    _field_expr(key)
    return key, op, value


@_locked
def query_fields(conn, *, recipe: str | None = None, lcz: str | None = None, split: str | None = None,
                 city_cluster: str | None = None, state: str | None = None,
                 case_ids: list[str] | None = None, directions: list[str] | None = None,
                 label: str | None = None, height_m: float | None = None,
                 where: Iterable[str] = (), sort: str | None = None,
                 limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Fields across cases, by what the case is and by what the field holds: one
    record per (case, direction) at the published height (or exactly ``height_m``),
    with the case's recipe, LCZ, split, city, state and spec. ``where`` clauses are
    ``<number><op><value>`` over FIELD_QUERY_NUMBERS and their ``vr_`` ratios, ANDed;
    ``sort`` is one of those, ``-`` first for descending, missing values last.
    ValueError for a clause or sort key that is not one."""
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    sql_where: list[str] = []
    params: list[Any] = []
    for column, value in (("c.recipe", recipe), ("c.lcz", lcz), ("c.split", split),
                          ("c.city_cluster", city_cluster), ("c.state", state)):
        if value:
            sql_where.append(column + " = ?")
            params.append(value)
    for column, values in (("f.case_id", case_ids), ("f.direction", directions)):
        if values:
            sql_where.append(column + " IN (" + ",".join("?" * len(values)) + ")")
            params.extend(values)
    if label:
        key, _, value = label.partition(":")
        if value:
            sql_where.append("f.case_id IN (SELECT case_id FROM case_labels WHERE key = ? AND value = ?)")
            params.extend([key.strip(), value.strip()])
        else:
            sql_where.append("f.case_id IN (SELECT case_id FROM case_labels WHERE key = ?)")
            params.append(key.strip())
    if height_m is None:
        sql_where.append("f.height_m BETWEEN ? AND ?")
        params.extend(_FIELD_HEIGHT_BAND)
    else:
        sql_where.append("ABS(f.height_m - ?) < 1e-6")
        params.append(float(height_m))
    for clause in where:
        key, op, value = parse_field_where(clause)
        sql_where.append("%s %s ?" % (_field_expr(key), "<>" if op == "!=" else op))
        params.append(value)
    order = "f.case_id ASC, f.deg ASC, f.direction ASC"
    if sort:
        key = sort.lstrip("-+")
        expr = _field_expr(key)
        order = "(%s IS NULL) ASC, %s %s, %s" % (expr, expr, "DESC" if sort.startswith("-") else "ASC", order)
    base = " FROM case_fields f JOIN cases c ON c.case_id = f.case_id WHERE " + " AND ".join(sql_where)
    total = int(conn.execute("SELECT COUNT(*) AS n" + base, params).fetchone()["n"])
    cols = ", ".join("f." + c.strip() for c in _FIELD_RECORD.split(",") if c.strip() not in ("worker_id",))
    rows = conn.execute(
        "SELECT f.case_id, " + cols + ", c.recipe, c.lcz, c.split, c.city_cluster, c.state, c.spec"
        + base + " ORDER BY " + order + " LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    return {"fields": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@_locked
def field_band(conn, case_id: str, direction: str, height_m: float, j0: int, j1: int):
    """Rows j0..j1 of one field's values: ``(band, None)``, the band read in place and
    ``4 * nx * (j1 - j0 + 1)`` bytes long -- or, for a row stored gzip-wrapped, which
    cannot be, ``(None, container)``, the whole container inflated for the caller to
    slice. None for no such field."""
    row = conn.execute(
        "SELECT nx, data_offset FROM case_fields WHERE case_id=? AND direction=? AND ABS(height_m - ?) < 1e-6",
        (case_id, direction, float(height_m))).fetchone()
    if row is None:
        return None
    nx, off = int(row["nx"]), row["data_offset"]
    if off is None:
        blob = conn.execute(
            "SELECT blob FROM case_fields WHERE case_id=? AND direction=? AND ABS(height_m - ?) < 1e-6",
            (case_id, direction, float(height_m))).fetchone()["blob"]
        return None, umag_container(bytes(blob))
    band = conn.execute(
        "SELECT substr(blob, CAST(data_offset + ? AS INTEGER), ?) AS b FROM case_fields"
        " WHERE case_id=? AND direction=? AND ABS(height_m - ?) < 1e-6",
        (4 * j0 * nx + 1, 4 * nx * (j1 - j0 + 1), case_id, direction, float(height_m))).fetchone()["b"]
    return bytes(band), None


@_locked
def field_counts(conn, case_ids: Iterable[str]) -> dict[str, int]:
    """How many field rows each of these cases has, for a case list."""
    ids = [c for c in case_ids]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["case_id"]: int(r["n"]) for r in conn.execute(
        f"SELECT case_id, COUNT(*) AS n FROM case_fields WHERE case_id IN ({marks})"
        " GROUP BY case_id", tuple(ids)).fetchall()}



# -- custody: what has arrived where results are kept ----------------------------
#
# `done` is the node's word: POST /v1/complete names an archive on the node's own
# disk (result_uri is file:///C:/wind/done/... on a Windows node) and its sha256.
# Whether that archive ever reached a place it is kept, intact, the broker could
# not say; nor whether every direction's field reached the database (a node from
# before the fields endpoint, or one whose upload failed, finishes the case without
# them). A RECEIPT is that missing half: written by whoever holds the artifact and
# has checked it, kept per artifact, and never folded into `state`, which leasing,
# every count and the reopen/respec rules read.

ARTIFACT_KINDS = ("archive",)
ARTIFACT_LOCATIONS = ("master", "broker")
# A field is stored at the published pedestrian height; case_fields may hold more.
_FIELD_HEIGHT_BAND = (1.7, 1.8)


class ReceiptRefused(ValueError):
    """A receipt the broker will not record: ``status`` is the HTTP code the API answers."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@_locked
def record_receipt(conn, case_id: str, kind: str, location: str, sha256: str,
                   size: int | None = None, path: str | None = None,
                   by: str | None = None, now: int | None = None) -> dict[str, Any]:
    """Record that an artifact of a case has arrived at ``location``, hashed to ``sha256``.

    An ``archive`` receipt is for a DONE case, and its hash must be the one the node
    reported at completion: a different hash is a transfer that corrupted the file,
    or an archive of another attempt, and is refused (409) rather than recorded --
    the case then stays on the custody list, which is where it belongs."""
    kind, location = (kind or "").strip(), (location or "").strip()
    sha = (sha256 or "").strip().lower()
    if kind not in ARTIFACT_KINDS:
        raise ReceiptRefused(422, f"kind must be one of {', '.join(ARTIFACT_KINDS)}")
    if location not in ARTIFACT_LOCATIONS:
        raise ReceiptRefused(422, f"location must be one of {', '.join(ARTIFACT_LOCATIONS)}")
    if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ReceiptRefused(422, "sha256 must be 64 hex characters")
    if size is not None and size < 0:
        raise ReceiptRefused(422, "bytes cannot be negative")
    row = conn.execute("SELECT state, result_sha256 FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise ReceiptRefused(404, "no such case")
    if kind == "archive":
        if row["state"] != "done":
            raise ReceiptRefused(409, f"the case is {row['state']}, not done: there is no archive to receive")
        expected = (row["result_sha256"] or "").strip().lower()
        if expected and expected != sha:
            raise ReceiptRefused(409, f"sha256 {sha} is not the {expected} the node reported at completion"
                                      " -- a corrupted or different archive")
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO case_artifacts(case_id, kind, location, bytes, sha256, path, received_at, reported_by)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(case_id, kind, location) DO UPDATE SET bytes=excluded.bytes,"
            " sha256=excluded.sha256, path=excluded.path, received_at=excluded.received_at,"
            " reported_by=excluded.reported_by",
            (case_id, kind, location, size, sha, path, now, by))
        _event(conn, case_id, None, "received",
               "%s at %s%s" % (kind, location, (", %d bytes" % size) if size is not None else ""), now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"case_id": case_id, "kind": kind, "location": location, "bytes": size, "sha256": sha,
            "path": path, "received_at": now, "reported_by": by}


# -- parts the broker holds (partstore.py) -------------------------------------------
#
# A node uploads each part it ships to the broker: the only copy that leaves the
# machine since the Syncthing master is gone. The database says what each part should
# be -- the hash the node reported for it -- and which of them have arrived; the bytes
# are files in the part store. A case's archive receipt at location "broker" is
# written the moment the broker holds its archive AND every part the case reported:
# that is "stored", the same thing an operator's scan of a copy says for location
# "master".

BLOB_ARCHIVE = "archive"


def blob_part_ok(part: str) -> bool:
    return part == BLOB_ARCHIVE or bool(PART_NAME.fullmatch(part or ""))


@_locked
def blob_expected(conn, case_id: str, part: str) -> dict[str, Any]:
    """What the broker expects ``part`` of ``case_id`` to be: ``sha256``, ``bytes``
    (None when the node did not say) and ``archive`` (its file name). Raises
    ReceiptRefused: 422 for a part name that is not one, 404 for a case or part the
    broker has no record of, 409 for a case's archive before the case is done.

    Only a part the node REPORTED can be uploaded, and only as the bytes it
    reported: the upload is checked against this hash, so the store never holds
    anything the broker did not already know the content of."""
    if not blob_part_ok(part):
        raise ReceiptRefused(422, "part must be 'mesh', 'case_<dir>' or 'archive'")
    if part == BLOB_ARCHIVE:
        row = conn.execute("SELECT state, result_sha256, result_bytes FROM cases WHERE case_id=?",
                           (case_id,)).fetchone()
        if row is None:
            raise ReceiptRefused(404, "no such case")
        if row["state"] != "done":
            raise ReceiptRefused(409, f"the case is {row['state']}, not done: it has no archive yet")
        sha = (row["result_sha256"] or "").strip().lower()
        if not _SHA256.fullmatch(sha):
            raise ReceiptRefused(409, "the node reported no sha256 for this case's archive")
        size = row["result_bytes"]
        return {"sha256": sha, "bytes": int(size) if size is not None else None,
                "archive": f"{case_id}.tar.gz"}
    row = conn.execute("SELECT archive, sha256, bytes FROM case_parts WHERE case_id=? AND part=?",
                       (case_id, part)).fetchone()
    if row is None:
        raise ReceiptRefused(404, f"the case has no part {part!r} on record: report it first (POST /v1/parts)")
    return {"sha256": row["sha256"].lower(), "bytes": int(row["bytes"]) if row["bytes"] is not None else None,
            "archive": row["archive"]}


@_locked
def record_blob(conn, case_id: str, part: str, sha256: str, size: int, by: str | None = None,
                now: int | None = None) -> dict[str, Any]:
    """The broker now holds ``part`` of ``case_id`` (the part store hashed it). Checked
    again against what is expected, because the case may have moved on while the
    bytes travelled -- a new mesh, a reset: then the part is not recorded (409), and
    its file is left for the sweep. Returns ``{"stored": True, "complete": bool}``,
    ``complete`` meaning this made the case's archive receipt at "broker"."""
    sha = (sha256 or "").lower()
    want = blob_expected(conn, case_id, part)
    if want["sha256"] != sha:
        raise ReceiptRefused(409, f"the case's {part} is now {want['sha256']}, not {sha}")
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO case_blobs(case_id, part, sha256, bytes, stored_at, stored_by) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(case_id, part) DO UPDATE SET sha256=excluded.sha256, bytes=excluded.bytes,"
            " stored_at=excluded.stored_at, stored_by=excluded.stored_by",
            (case_id, part, sha, int(size), now, by))
        _event(conn, case_id, None, "stored", "%s at the broker, %d bytes" % (part, size), now)
        complete = _blob_receipt(conn, case_id, by, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"stored": True, "complete": complete}


def _blob_receipt(conn, case_id: str, by: str | None, now: int) -> bool:
    """Write the archive receipt at "broker" when the broker holds the case's archive
    and every part the case reported, each as reported. Inside the caller's
    transaction. Returns whether it did."""
    held = {r["part"]: r for r in conn.execute(
        "SELECT part, sha256, bytes FROM case_blobs WHERE case_id=?", (case_id,)).fetchall()}
    archive = held.get(BLOB_ARCHIVE)
    if archive is None:
        return False
    case = conn.execute("SELECT state, result_sha256 FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if case is None or case["state"] != "done" or (case["result_sha256"] or "").lower() != archive["sha256"]:
        return False
    for p in conn.execute("SELECT part, sha256 FROM case_parts WHERE case_id=?", (case_id,)).fetchall():
        have = held.get(p["part"])
        if have is None or have["sha256"] != p["sha256"].lower():
            return False
    total = sum(int(r["bytes"]) for r in held.values())
    conn.execute(
        "INSERT INTO case_artifacts(case_id, kind, location, bytes, sha256, path, received_at, reported_by)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(case_id, kind, location) DO UPDATE SET bytes=excluded.bytes,"
        " sha256=excluded.sha256, path=excluded.path, received_at=excluded.received_at,"
        " reported_by=excluded.reported_by",
        (case_id, "archive", "broker", total, archive["sha256"], "parts:%d" % len(held), now, by))
    _event(conn, case_id, None, "received", "archive at broker, %d part(s), %d bytes" % (len(held), total), now)
    return True


#: How many cases one parts_wanted call answers for. A node with a long backlog asks in batches.
PARTS_WANTED_MAX = 500


@_locked
def parts_wanted(conn, case_ids: list[str]) -> dict[str, dict[str, Any]]:
    """For each of ``case_ids`` the broker has, the parts it knows the content of and does not
    hold yet: every reported part (case_parts) not in case_blobs with that hash, and a done
    case's archive by the completion's hash. ``{case_id: {"state": ..., "wanted": [{part,
    sha256, bytes, archive}]}}``; a case the broker has never heard of is left out.

    What a node asks when it sweeps its done folder: the parts that reached nobody -- shipped
    before the broker kept parts, or while it could not be reached -- go up in bulk the next
    time it can, each checked against this hash on arrival as any upload is."""
    ids = list(dict.fromkeys(i for i in case_ids if isinstance(i, str) and i))[:PARTS_WANTED_MAX]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT case_id, state, result_sha256, result_bytes FROM cases"
                          " WHERE case_id IN (%s)" % marks, ids).fetchall():
        out[r["case_id"]] = {"state": r["state"], "wanted": [],
                             "_archive": (r["result_sha256"] or "").strip().lower(),
                             "_bytes": r["result_bytes"]}
    if not out:
        return {}
    held = {(r["case_id"], r["part"]): (r["sha256"] or "").lower() for r in conn.execute(
        "SELECT case_id, part, sha256 FROM case_blobs WHERE case_id IN (%s)" % marks, ids).fetchall()}
    for r in conn.execute("SELECT case_id, part, archive, sha256, bytes FROM case_parts"
                          " WHERE case_id IN (%s) ORDER BY case_id, part" % marks, ids).fetchall():
        sha = (r["sha256"] or "").lower()
        if r["case_id"] in out and held.get((r["case_id"], r["part"])) != sha:
            out[r["case_id"]]["wanted"].append({"part": r["part"], "sha256": sha, "archive": r["archive"],
                                                "bytes": int(r["bytes"]) if r["bytes"] is not None else None})
    fields: dict[str, list[str]] = {}
    for r in conn.execute("SELECT case_id, direction FROM case_fields WHERE case_id IN (%s)"
                          " AND ABS(height_m - ?) < 1e-6 ORDER BY case_id, direction" % marks,
                          [*ids, FIELD_HEIGHT_M]).fetchall():
        fields.setdefault(r["case_id"], []).append(r["direction"])
    for case_id, row in out.items():
        # The directions whose field the broker holds: what a node can backfill is the rest
        # (backfill_field), from the archives of a done case on its disk.
        row["fields"] = fields.get(case_id, [])
        sha, size = row.pop("_archive"), row.pop("_bytes")
        if row["state"] == "done" and _SHA256.fullmatch(sha) and held.get((case_id, BLOB_ARCHIVE)) != sha:
            row["wanted"].append({"part": BLOB_ARCHIVE, "sha256": sha, "archive": f"{case_id}.tar.gz",
                                  "bytes": int(size) if size is not None else None})
        # The mesh first: it is what a node continuing the case needs; the archive last.
        row["wanted"].sort(key=lambda w: (w["part"] == BLOB_ARCHIVE, w["part"] != "mesh", w["part"]))
    return out


@_locked
def case_blobs(conn, case_id: str) -> list[dict[str, Any]]:
    """What of a case the broker holds, the mesh first, the archive last."""
    rows = [dict(r) for r in conn.execute(
        "SELECT part, sha256, bytes, stored_at, stored_by FROM case_blobs WHERE case_id=?",
        (case_id,)).fetchall()]
    return sorted(rows, key=lambda r: (r["part"] == BLOB_ARCHIVE, r["part"] != "mesh", r["part"]))


@_locked
def blob_of(conn, case_id: str, part: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT part, sha256, bytes, stored_at FROM case_blobs WHERE case_id=? AND part=?",
                       (case_id, part)).fetchone()
    return dict(row) if row else None


@_locked
def drop_blob(conn, case_id: str, part: str, by: str | None = None, now: int | None = None) -> dict[str, Any] | None:
    """Forget that the broker holds ``part`` of ``case_id`` (and the case's broker
    receipt, which no longer holds). Returns the row and whether any other row still
    refers to the same content, so the caller deletes the file only when none does."""
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT sha256, bytes FROM case_blobs WHERE case_id=? AND part=?",
                           (case_id, part)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        conn.execute("DELETE FROM case_blobs WHERE case_id=? AND part=?", (case_id, part))
        conn.execute("DELETE FROM case_artifacts WHERE case_id=? AND kind='archive' AND location='broker'",
                     (case_id,))
        others = conn.execute("SELECT COUNT(*) AS n FROM case_blobs WHERE sha256=?",
                              (row["sha256"],)).fetchone()["n"]
        _event(conn, case_id, by, "dropped", "%s at the broker" % part, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"sha256": row["sha256"], "bytes": int(row["bytes"]), "still_referenced": int(others) > 0}


@_locked
def referenced_blobs(conn) -> set[str]:
    """Every content hash some case's row -- or the release catalog -- refers to: what a
    sweep must keep. Release files live in the same store (a build's file is held here
    for the nodes to fetch), and without the catalog's hashes here a sweep would delete
    every one of them as an orphan."""
    cases = {r["sha256"] for r in conn.execute("SELECT DISTINCT sha256 FROM case_blobs").fetchall()}
    return cases | {r["sha256"] for r in conn.execute("SELECT DISTINCT sha256 FROM releases").fetchall()}


@_locked
def blob_totals(conn) -> dict[str, Any]:
    row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b, COUNT(DISTINCT case_id) AS c"
                       " FROM case_blobs").fetchone()
    complete = conn.execute("SELECT COUNT(*) AS n FROM case_artifacts WHERE kind='archive' AND location='broker'"
                            ).fetchone()["n"]
    return {"parts": int(row["n"]), "bytes": int(row["b"]), "cases": int(row["c"]),
            "cases_complete": int(complete)}


@_locked
def case_receipts(conn, case_id: str) -> list[dict[str, Any]]:
    """The receipts a case has, by kind and location."""
    rows = conn.execute(
        "SELECT kind, location, bytes, sha256, path, received_at, reported_by FROM case_artifacts"
        " WHERE case_id=? ORDER BY kind, location", (case_id,)).fetchall()
    return [dict(r) for r in rows]


def expected_fields(recipe: str | None, telemetry: Any) -> int | None:
    """How many pedestrian fields a done case should have reached the database with: one per
    wind direction, as its telemetry counted them (solve.directions_total, else mesh.directions);
    None for a recipe without fields (the thermal and MRT recipes) or a case that never said."""
    if not recipe or recipe.startswith(RADIANCE_RECIPE_PREFIXES):
        return None
    t = telemetry if isinstance(telemetry, dict) else {}
    for block, key in (("solve", "directions_total"), ("mesh", "directions")):
        v = (t.get(block) or {}).get(key) if isinstance(t.get(block), dict) else None
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, list) and v:
            return len(v)
    return None


@_locked
def custody(conn, recipe: str | None = None, older_than: int = 0, limit: int = 200,
            now: int | None = None) -> dict[str, Any]:
    """The done cases whose result has not all arrived where results are kept: no archive
    receipt, a part its nodes shipped (the mesh, a direction) that the part store does not
    hold with the hash they reported -- the archive leaves out what was shipped, so it is
    not the whole case without them -- or fewer pedestrian fields in the database than
    directions. ``by_location`` counts the incomplete cases by where their result was left
    (the directory of ``result_uri``): what is lost when that disk is.

    ``older_than`` (seconds since the case finished) leaves out what is still syncing, so the
    list is what needs someone: a node that went away before its archive left it, a field
    upload that failed. ``stored`` counts the done cases with nothing missing."""
    now = now or _now()
    where, params = ["state = 'done'"], []
    if recipe:
        where.append("recipe = ?"); params.append(recipe)
    rows = conn.execute(
        "SELECT case_id, recipe, updated_at, telemetry, result_uri, result_sha256 FROM cases WHERE "
        + " AND ".join(where) + " ORDER BY updated_at", params).fetchall()
    archived = {r["case_id"] for r in conn.execute(
        "SELECT DISTINCT case_id FROM case_artifacts WHERE kind = 'archive'").fetchall()}
    held = {(r["case_id"], r["part"]): (r["sha256"] or "").lower()
            for r in conn.execute("SELECT case_id, part, sha256 FROM case_blobs").fetchall()}
    unheld: dict[str, list[tuple[str, int]]] = {}
    for r in conn.execute("SELECT p.case_id, p.part, p.sha256, p.bytes FROM case_parts p"
                          " JOIN cases c ON c.case_id = p.case_id WHERE c.state = 'done'").fetchall():
        if held.get((r["case_id"], r["part"])) != (r["sha256"] or "").lower():
            unheld.setdefault(r["case_id"], []).append((r["part"], int(r["bytes"] or 0)))
    lo, hi = _FIELD_HEIGHT_BAND
    fields = {r["case_id"]: int(r["n"]) for r in conn.execute(
        "SELECT case_id, COUNT(DISTINCT direction) AS n FROM case_fields"
        " WHERE height_m >= ? AND height_m <= ? GROUP BY case_id", (lo, hi)).fetchall()}
    out: list[dict[str, Any]] = []
    stored = no_archive = short_fields = short_parts = 0
    parts_bytes = 0
    by_location: dict[str, int] = {}
    for r in rows:
        try:
            telemetry = json.loads(r["telemetry"]) if r["telemetry"] else {}
        except (TypeError, ValueError):
            telemetry = {}
        want = expected_fields(r["recipe"], telemetry)
        have = fields.get(r["case_id"], 0)
        missing = []
        if r["case_id"] not in archived:
            missing.append("archive")
        if want is not None and have < want:
            missing.append("fields")
        parts = sorted(unheld.get(r["case_id"], []))
        if parts:
            missing.append("parts")
        if not missing:
            stored += 1
            continue
        no_archive += "archive" in missing
        short_fields += "fields" in missing
        short_parts += bool(parts)
        parts_bytes += sum(n for _, n in parts)
        uri = r["result_uri"] or ""
        where = uri.rsplit("/", 1)[0] if "/" in uri else (uri or "unknown")
        by_location[where] = by_location.get(where, 0) + 1
        age = now - int(r["updated_at"] or now)
        if age < older_than:
            continue
        out.append({"case_id": r["case_id"], "recipe": r["recipe"], "done_at": r["updated_at"],
                    "age_seconds": age, "missing": missing, "fields": have, "fields_expected": want,
                    "parts_missing": [part for part, _ in parts], "result_uri": r["result_uri"]})
    out.sort(key=lambda c: -c["age_seconds"])
    return {"done": len(rows), "stored": stored, "missing_archive": no_archive,
            "missing_fields": short_fields, "missing_parts": short_parts,
            "missing_parts_bytes": parts_bytes, "by_location": by_location,
            "listed": len(out[:limit]), "total": len(out), "cases": out[:limit]}


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
    (past TELEMETRY_MAX_DEPTH), ``"too_large"`` (past TELEMETRY_MAX_BYTES) or,
    for kind ``residuals`` only, ``"bad_series"`` (not a residual series: see
    normalize_residuals). For that kind `cleaned` is the series as it will be
    stored, not just cleaned.
    """
    if not isinstance(kind, str) or not TELEMETRY_KIND.fullmatch(kind) \
            or not isinstance(data, dict):
        return "invalid", None
    if nests_deeper(data, TELEMETRY_MAX_DEPTH):
        return "too_deep", None
    cleaned = _clean(data)
    if len(_telemetry_json(cleaned)) > TELEMETRY_MAX_BYTES:
        return "too_large", None
    if kind == RESIDUAL_KIND:
        series = normalize_residuals(cleaned)
        return ("ok", series) if series is not None else ("bad_series", None)
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
    new kind past TELEMETRY_MAX_KINDS), ``"too_many_directions"`` (a residual
    series for a 65th direction), or an outcome of prepare_telemetry.
    Nothing here touches ``updated_at`` or the events trail: telemetry is not a
    state change, and a solve reporting every five minutes must not become the
    case browser's idea of "what just happened".

    Kind ``residuals`` is the one exception to "the latest report of a kind
    replaces the one before": it is a series per wind direction, kept apart in
    case_residuals (see there), and does not count toward TELEMETRY_MAX_KINDS.
    A ``solve`` report also feeds that table, for a node that sends no series
    of its own (_note_solve_report).
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
        if kind == RESIDUAL_KIND:
            outcome = _put_residuals(conn, case_id, cleaned, "trace", row["lease_worker"], now)
            conn.execute("COMMIT" if outcome == "ok" else "ROLLBACK")
            return outcome
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
        if kind == "solve":
            _note_solve_report(conn, case_id, cleaned, row["lease_worker"], now)
        cells = cleaned.get("total_cells") if kind == "mesh" else None
        if isinstance(cells, int) and not isinstance(cells, bool) and 0 < cells < 1 << 40:
            conn.execute("UPDATE cases SET mesh_cells=? WHERE case_id=?", (cells, case_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "ok"


# -- residual histories ----------------------------------------------------------
#
# Telemetry is a summary: the latest report of a kind, replacing the one before.
# That is the right shape for "where is the solve" and the wrong one for "is it
# converging", which is a curve -- a residual that fell three decades and one
# that has sat at 1e-3 for an hour print the same latest number. A case has a
# curve per wind direction (32), which neither the 16 kinds nor the 32 KiB a
# kind may hold can carry, so the series live in their own table, written by
# post_telemetry (kind `residuals`) and read by case_residuals.

RESIDUAL_KIND = "residuals"
#: Points in one stored series. A node sends at most ~160 (it decimates a
#: 2,000-iteration solve for the 32 KiB a telemetry post may carry); this only
#: bounds what the broker will keep of a node that sends more.
RESIDUAL_MAX_POINTS = 1000
#: Fields (Ux, Uy, Uz, p, k, epsilon, ... ) in one series.
RESIDUAL_MAX_FIELDS = 16
#: Directions with a series, per case. A case has 32; the bound is what keeps a
#: node that invents direction names from growing the table without limit.
RESIDUAL_MAX_DIRECTIONS = 64
#: Points kept of a series assembled from `solve` reports (one per ~5 minutes,
#: so 50 hours): the oldest go first.
RESIDUAL_SAMPLE_MAX = 600
#: An OpenFOAM field name as it appears in "Solving for <field>".
_RESIDUAL_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,31}")


def _real(v: Any) -> float | None:
    """`v` as a finite float, or None for anything that is not one (a bool is
    not a number here: JSON `true` is not an iteration)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


#: The largest iteration or iteration cap taken at its word. They are stored in
#: REAL columns, which are 32-bit floats on Postgres: 1e300 in one is "real out
#: of range", an error inside the transaction that also holds the node's solve
#: report -- so a node that sent nonsense would lose the report, not just the
#: curve. No solve runs a quadrillion iterations.
_ITERATION_MAX = 1e15


def _iteration(v: Any) -> float | None:
    """`v` as an iteration number the broker will store, else None."""
    x = _real(v)
    return x if x is not None and abs(x) <= _ITERATION_MAX else None


def _sig4(v: float) -> float | int:
    """`v` to four significant digits. A residual is read on a log axis to a
    decade or a third of one; the 17 digits a double prints are 60% of a stored
    series."""
    if v == 0:
        return 0
    return float("%.4g" % v)


def _whole(v: float) -> float | int:
    return int(v) if v == int(v) else v


def normalize_residuals(data: dict[str, Any]) -> dict[str, Any] | None:
    """One direction's residual series as it is stored, or None when `data` is
    not one: ``{"direction", "iterations", "fields", "end_time", "total",
    "complete"}`` (protocol.md, "Telemetry").

    Strict about the shape, because a chart is drawn from it -- `iterations` a
    list of numbers, every field a list of numbers-or-null as long as it, a
    direction named like a case directory -- and forgiving about what a solver
    legitimately produces: a non-finite residual is null (OpenFOAM prints `nan`
    for a diverging field; `data` was cleaned of them already), and a series
    whose iterations go BACKWARDS is cut to its last run. That second is not
    defensive: the numerics ladder restarts a direction from 0 on a safer rung
    and its solver tees into the same log, so a naive read of the log is the
    failed run followed by the one that is current, and a line chart of that
    doubles back on itself.
    """
    direction, xs_in, fields_in = data.get("direction"), data.get("iterations"), data.get("fields")
    if not isinstance(direction, str) or direction == "mesh" or not PART_NAME.fullmatch(direction):
        return None
    if not isinstance(xs_in, list) or not 1 <= len(xs_in) <= RESIDUAL_MAX_POINTS:
        return None
    if not isinstance(fields_in, dict) or not 1 <= len(fields_in) <= RESIDUAL_MAX_FIELDS:
        return None
    xs = [_iteration(x) for x in xs_in]
    if any(x is None for x in xs):
        return None
    cols: dict[str, list[float | int | None]] = {}
    for name, values in fields_in.items():
        if not _RESIDUAL_FIELD.fullmatch(str(name)) or not isinstance(values, list) \
                or len(values) != len(xs):
            return None
        col: list[float | int | None] = []
        for v in values:
            if v is None:
                col.append(None)
                continue
            y = _real(v)
            if y is None:                       # a string, a bool, a nested value
                return None
            col.append(_sig4(y))
        cols[str(name)] = col
    start = 0
    for i in range(1, len(xs)):
        if xs[i] < xs[i - 1]:
            start = i
    total = data.get("total")
    return {
        "direction": direction,
        "iterations": [_whole(x) for x in xs[start:]],
        "fields": {k: v[start:] for k, v in cols.items()},
        "end_time": _iteration(data.get("end_time")),
        "total": int(total) if isinstance(total, int) and not isinstance(total, bool)
                 and total >= len(xs) - start else None,
        "complete": data.get("complete") is True,
    }


def _put_residuals(conn, case_id: str, record: dict[str, Any], source: str,
                   worker: str | None, now: int) -> str:
    """Store one direction's series, replacing that direction's; inside the
    caller's transaction. ``"ok"``, or ``"too_many_directions"``."""
    direction = record["direction"]
    known = conn.execute("SELECT 1 FROM case_residuals WHERE case_id=? AND direction=?",
                         (case_id, direction)).fetchone()
    if known is None and conn.execute(
            "SELECT COUNT(*) AS n FROM case_residuals WHERE case_id=?",
            (case_id,)).fetchone()["n"] >= RESIDUAL_MAX_DIRECTIONS:
        return "too_many_directions"
    xs = record["iterations"]
    body = _telemetry_json({"iterations": xs, "fields": record["fields"],
                            "total": record["total"], "complete": record["complete"]})
    conn.execute(
        "INSERT INTO case_residuals(case_id, direction, source, n, iteration, end_time, series,"
        " worker_id, reported_at) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(case_id, direction) DO UPDATE SET source=excluded.source, n=excluded.n,"
        " iteration=excluded.iteration, end_time=excluded.end_time, series=excluded.series,"
        " worker_id=excluded.worker_id, reported_at=excluded.reported_at",
        (case_id, direction, source, len(xs), xs[-1], record["end_time"], body, worker, now))
    return "ok"


def _note_solve_report(conn, case_id: str, solve: dict[str, Any], worker: str | None,
                       now: int) -> None:
    """Add the latest residuals of a `solve` report to the current direction's
    series, for a node that sends no series of its own.

    A node from before kind `residuals` reports only the newest number per
    field, every five minutes at most: a coarse curve (a dozen points an hour)
    but the same shape, which is better than a table of one row, and it is
    there the moment this broker is, without waiting for a fleet to update. A
    series the node itself sent (``source = 'trace'``) is never touched: it is
    the solver's own and finer, and a report's point would only be noise in it.

    Never raises on what a node sent -- a progress report must not fail its
    own post -- it just records nothing. An iteration at or before the last
    point is not new (a repeated report), except that going BACKWARDS is a
    rung of the numerics ladder restarting the direction, which starts the
    series over.
    """
    direction, it, res = solve.get("current"), _iteration(solve.get("iteration")), solve.get("residuals")
    if not isinstance(direction, str) or direction == "mesh" or not PART_NAME.fullmatch(direction) \
            or it is None or not isinstance(res, dict):
        return
    point: dict[str, float | int | None] = {}
    for name, v in res.items():
        if not _RESIDUAL_FIELD.fullmatch(str(name)) or len(point) >= RESIDUAL_MAX_FIELDS:
            continue
        y = _real(v)
        point[str(name)] = _sig4(y) if y is not None else None
    if not any(v is not None for v in point.values()):
        return
    row = conn.execute("SELECT source, series FROM case_residuals WHERE case_id=? AND direction=?",
                       (case_id, direction)).fetchone()
    xs: list[float | int] = []
    cols: dict[str, list[float | int | None]] = {}
    if row is not None:
        if row["source"] != "reports":
            return
        try:
            old = json.loads(row["series"])
            xs, cols = list(old["iterations"]), {k: list(v) for k, v in old["fields"].items()}
        except (TypeError, ValueError, KeyError, AttributeError):
            xs, cols = [], {}
    if xs and it == xs[-1]:
        return
    if xs and it < xs[-1]:
        xs, cols = [], {}
    for name in point:
        cols.setdefault(name, [None] * len(xs))
    for name, col in cols.items():
        col.append(point.get(name))
    xs.append(_whole(it))
    if len(xs) > RESIDUAL_SAMPLE_MAX:
        cut = len(xs) - RESIDUAL_SAMPLE_MAX
        xs, cols = xs[cut:], {k: v[cut:] for k, v in cols.items()}
    end = _iteration(solve.get("end_time"))
    _put_residuals(conn, case_id, {
        "direction": direction, "iterations": xs, "fields": cols, "end_time": end,
        "total": None, "complete": False}, "reports", worker, now)


@_locked
def case_residuals(conn, case_id: str, direction: str | None = None) -> dict[str, Any] | None:
    """A case's residual series: the list of directions that have one, and the
    series of one of them. None for a case that does not exist.

    ``directions`` is one record per direction (``direction``, ``source``,
    ``n`` points, the last ``iteration``, ``end_time``, ``reported_at``,
    ``worker``), sorted by name as text -- the order a node solved them in is
    not recorded, and the dashboard sorts the names as numbers. ``series`` is
    that of the direction asked for, else the one reported most recently (the
    one a viewer watching a live solve wants), or None when there is none. The
    index never carries the points, so the list stays small however many
    directions there are; the series is one request away.
    """
    if conn.execute("SELECT 1 FROM cases WHERE case_id=?", (case_id,)).fetchone() is None:
        return None
    rows = conn.execute(
        "SELECT direction, source, n, iteration, end_time, reported_at, worker_id"
        " FROM case_residuals WHERE case_id=? ORDER BY direction", (case_id,)).fetchall()
    index = [{"direction": r["direction"], "source": r["source"], "n": r["n"],
              "iteration": r["iteration"], "end_time": r["end_time"],
              "reported_at": r["reported_at"], "worker": r["worker_id"]} for r in rows]
    chosen = direction
    if chosen is None and index:
        chosen = max(index, key=lambda r: (r["reported_at"], r["direction"]))["direction"]
    series = None
    if chosen is not None:
        got = conn.execute("SELECT series FROM case_residuals WHERE case_id=? AND direction=?",
                           (case_id, chosen)).fetchone()
        if got is not None:
            try:
                series = json.loads(got["series"])
            except (TypeError, ValueError):
                series = None
    return {"case_id": case_id, "directions": index, "direction": chosen, "series": series}


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
                 detail: str | None = None, now: int | None = None,
                 jobs: list[dict[str, Any]] | None = None) -> None:
    """Record what a scheduler currently holds for one cluster.

    Upsert on cluster, so a reporter can run on a timer and simply overwrite its
    own last snapshot rather than accumulating history nobody reads. ``jobs`` is
    the snapshot's job list, replaced with it; None (a reporter that sends counts
    only) leaves no list.
    """
    now = now or _now()
    listed = json.dumps(jobs) if jobs is not None else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        updated = conn.execute(
            "UPDATE fleet SET queued=?, running=?, detail=?, reported_at=?, jobs=? WHERE cluster=?",
            (queued, running, detail, now, listed, cluster)).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO fleet (cluster, queued, running, detail, reported_at, jobs)"
                " VALUES (?, ?, ?, ?, ?, ?)", (cluster, queued, running, detail, now, listed))
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
    out = []
    for r in conn.execute("SELECT * FROM fleet ORDER BY cluster"):
        row = {**dict(r), "age_seconds": now - r["reported_at"]}
        try:
            row["jobs"] = json.loads(row["jobs"]) if row.get("jobs") else None
        except (TypeError, ValueError):
            row["jobs"] = None
        out.append(row)
    return out


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
    "share_links": "read-only links an admin made (hashes only)",
    "releases": "node builds the broker points at",
    "build_stats": "per-build outcome counters",
    "settings": "broker settings",
    "schema_meta": "schema version",
    "push_subscriptions": "browsers that asked for push notifications",
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
    # A Radiance case's long phase is the trace, "trace 12/36 chunks" -- chunks
    # FINISHED, like directions (docs/thermal.md, docs/mrt.md).
    if not head.strip().lower().startswith(("solve", "trace")):
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
def status(conn, now: int | None = None, recipe: str | None = None) -> dict[str, Any]:
    """The campaign at a glance. `recipe` scopes the counts, the splits and the
    ETA to one recipe -- since 2026-09 the queue holds CFD wind and Radiance
    surface temperatures, whose throughputs have nothing to do with each other,
    so one pooled ETA describes neither. `by_recipe` always lists every recipe's
    states, and the workers stay fleet-wide: a machine serves both."""
    now = now or _now()
    # A normal expired lease is claimable immediately by lease().  After 48
    # hours with no claimant, clear it here as well: status is polled by the
    # dashboard, giving abandoned rows a bounded lifetime even while the fleet
    # is idle.  The event records that this was timeout cleanup, not preemption.
    _release_stale_leases(conn, now)
    only, only_params = (" AND recipe = ?", (recipe,)) if recipe else ("", ())
    by_state = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) n FROM cases WHERE 1 = 1" + only + " GROUP BY state", only_params)}
    by_split = {r["split"] + "/" + r["state"]: r["n"] for r in conn.execute(
        "SELECT split, state, COUNT(*) n FROM cases WHERE 1 = 1" + only + " GROUP BY split, state",
        only_params)}
    by_recipe: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT recipe, state, COUNT(*) n FROM cases GROUP BY recipe, state"):
        by_recipe.setdefault(r["recipe"], {})[r["state"]] = r["n"]
    stale = conn.execute(
        "SELECT COUNT(*) n FROM cases WHERE state='leased' AND lease_expires < ?" + only,
        (now, *only_params)).fetchone()["n"]
    next_stale_release = conn.execute(
        "SELECT MIN(lease_expires + ?) AS at FROM cases"
        " WHERE state='leased' AND lease_expires < ?" + only,
        (STALE_LEASE_RELEASE_SECONDS, now, *only_params),
    ).fetchone()["at"]
    if recipe:
        done_24h = conn.execute(
            "SELECT COUNT(*) n FROM events e JOIN cases c ON c.case_id = e.case_id"
            " WHERE e.event='done' AND e.ts > ? AND c.recipe = ?",
            (now - 86400, recipe)).fetchone()["n"]
    else:
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
            # Seen, or still asking what to run: a node that refuses work on its own side
            # (a full disk) never leases, and used to age off this list while it waited.
            " FROM workers w WHERE w.last_seen > ? OR w.release_asked_at > ?"
            " ORDER BY w.last_seen DESC LIMIT 500",
            (_now() - 86400, _now() - 86400))]
    for w in workers:
        w["current_eta"] = _solve_eta(conn, w["current_case"], w["current_leased_at"]) \
            if w.get("current_case") else None
        try:
            w["features"] = json.loads(w["features"]) if w.get("features") else None
        except (TypeError, ValueError):
            w["features"] = None
        # The recipes it declared with its last lease, or None for a worker that
        # declares none (handed the `undeclared_recipes` policy's, or anything).
        # The workers table shows them beside the queue: the one answer to "why
        # does a surface-temperature case sit pending while twenty nodes idle" is
        # that none of the twenty declares it, and nothing else on the page said so.
        try:
            w["recipes"] = json.loads(w["recipes"]) if w.get("recipes") else None
        except (TypeError, ValueError):
            w["recipes"] = None
    return {
        "fleet": fleet(conn, now),
        "recipe": recipe,
        "by_state": by_state,
        "by_split": by_split,
        "by_recipe": by_recipe,
        "expired_leases": stale,
        # The earliest deadline is enough for the banner: it tells operators
        # when the first still-visible expired row will be cleared.
        "expired_lease_release_in_seconds": (
            max(0, int(next_stale_release) - now) if next_stale_release is not None else None),
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
               label: str | None = None, recipe: str | None = None,
               after: str | None = None) -> dict[str, Any]:
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

    ``after`` pages by KEY instead of by offset: the cases whose id sorts after it,
    in case_id order (``sort`` and ``offset`` are then ignored). An offset counts
    rows, and a live campaign moves rows: a case that leaves ``done`` between two
    pages of an export shifts every later row up by one, and one case is never
    read. The answer to a case_id-ordered page carries ``next_after`` -- the id to
    ask after for the next page, None on the last -- so a client knows the broker
    understood (one that ignored ``after`` would hand back page one forever).
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
    if recipe:
        where.append("recipe = ?"); params.append(recipe)
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
    keyset = after is not None or (sort == "case_id" and str(direction).lower() == "asc")
    if after is not None:
        clause += (" AND " if clause else " WHERE ") + "cases.case_id > ?"
        params = params + [after]
        order, offset = "cases.case_id ASC", 0
    else:
        column = CASE_SORTS.get(sort or "", "cases.updated_at")
        descending = str(direction).lower() != "asc"
        order = f"{column} {'DESC' if descending else 'ASC'}, cases.case_id ASC"
    columns = _CASE_LIST_COLS if include_spec else _CASE_COLS_NO_SPEC
    rows = conn.execute(
        "SELECT " + columns + " FROM cases" + clause +
        " ORDER BY " + order + " LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    extra = {"next_after": rows[-1]["case_id"] if len(rows) == limit else None} if keyset else {}
    return {**extra, "cases": _attach_labels(conn, [dict(r) for r in rows]), "total": total,
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
# `lease_id` is never on a page either: it is the bearer's proof of holding a
# case on every call after /v1/lease, and a page goes to every reader -- viewer
# sessions and share links included. The holder is `lease_worker`.
#
# `telemetry` is never on a PAGE, with or without the spec: it is up to 16 kinds
# per case, the solve's per-direction table among them, and the case browser
# fetches the one case it opens in full (get_case, which keeps `cases.*`).
_CASE_COLS_WITHOUT_SPEC = (
    "cases.case_id, cases.recipe, cases.split, cases.lcz, cases.city_cluster,"
    " cases.priority, cases.state, cases.attempts, cases.max_attempts,"
    " cases.lease_worker, cases.leased_at, cases.lease_expires,"
    " cases.result_uri, cases.result_sha256, cases.result_bytes, cases.metrics,"
    " cases.last_error, cases.created_at, cases.updated_at, cases.mesh_cells, cases.needs_case"
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
    #
    # A stop the broker forgave is still a stop, and so is a node giving a case
    # back because the MACHINE cannot run it: both are 'released', and a history of
    # failures alone showed neither. Field case, 2026-09-24: v2-000c178c579bf034's
    # snappy ran out of its 120 minutes on cod-358-21 five times in ten hours; three
    # stops were refunded, and the export listed ONE failure on that machine -- a
    # loop that read as a single timeout. An ordinary release (a preemption, a node
    # stopped for an update) stays out: nothing went wrong in it.
    ids = list(by_id)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for e in conn.execute(
                "SELECT e.case_id, e.ts, e.event, e.worker_id, e.detail, w.host, w.cluster"
                " FROM events e LEFT JOIN workers w ON w.worker_id = e.worker_id"
                " WHERE (e.event IN ('failed','quarantined')"
                "        OR (e.event = 'released' AND (e.detail LIKE ? OR e.detail LIKE ?)))"
                f" AND e.case_id IN ({marks})"
                " ORDER BY e.id",
                [_TIMEOUT_REFUND_TAG + "%", _MACHINE_RELEASE + "%", *chunk]).fetchall():
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
        "SELECT ts, event, detail, stage FROM events WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()]
    out.update(_stages.from_events(trail, _now()))
    # Which case this one was moved to another recipe as, or from (respec_cases):
    # a parked case says where its site went, and the new one where it came from.
    out.update(_respec_links(trail, case_id))
    # Same estimate the Workers table shows for this case's own lease -- the
    # detail card had the age of the last line but not when the solve should
    # end, which is the more useful of the two once a direction is a day in.
    out["eta"] = _solve_eta(conn, case_id, row["leased_at"]) if row["state"] == "leased" else None
    # The state of the case this one waits for (docs/mrt.md): a pending MRT case
    # whose surface case failed or was parked would otherwise wait forever and
    # say nothing. None when it needs none; "missing" when the broker has no such
    # case (it was purged after the need was posted).
    need = row["needs_case"] if "needs_case" in row.keys() else None
    if need:
        n = conn.execute("SELECT state FROM cases WHERE case_id = ?", (need,)).fetchone()
        out["needs_state"] = n["state"] if n else "missing"
    else:
        out["needs_state"] = None
    return out


@_locked
def purge_cases(conn, recipe: str | None = None, state: str | None = None,
                expect: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Delete cases (and their events, footprints, labels and residual series) from the campaign.

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
        conn.execute(f"DELETE FROM case_residuals WHERE case_id IN ({sub})", params)
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


# -- share links: a read-only look at the campaign, for someone with no account ---
#
# The older ways to show a friend the dashboard were a `viewer` account (a
# password to invent and hand over) and CASEBROKER_READ_TOKENS (one shared secret
# in the host's environment: no name, no expiry, and taking it back is a
# redeploy). A share link is the middle: an admin makes one in the browser, for
# a named person, for a stated time, and revokes it with a click. The raw token
# exists once, in the response that made it; only its hash is stored.

#: How many links may be live at once. A link is a credential: a stuck button or
#: a runaway script minting thousands would leave a pile nobody can audit.
MAX_ACTIVE_SHARE_LINKS = 50

#: A revoked or expired link stays this long, so the list can still say what
#: became of it, and is then dropped.
SHARE_LINK_KEEP_SECONDS = 30 * 24 * 3600

#: `last_used_at` moves at most this often. It is stamped from the read gate, so
#: on requests, and one dashboard refresh makes a dozen of them.
SHARE_LINK_TOUCH_SECONDS = 60


def share_link_state(link: dict[str, Any], now: int | None = None) -> str:
    """`live`, `expired` or `revoked` -- revoked wins, because it is the answer
    to "did somebody take this back", which an expiry date cannot undo."""
    now = now or _now()
    if link["revoked_at"] is not None:
        return "revoked"
    if link["expires_at"] is not None and int(link["expires_at"]) <= now:
        return "expired"
    return "live"


def _share_row(r) -> dict[str, Any]:
    # By column name, never position: a Postgres row is a dict.
    return {"id": r["id"], "label": r["label"], "created_by": r["created_by"],
            "created_at": r["created_at"], "expires_at": r["expires_at"],
            "last_used_at": r["last_used_at"], "opened": r["opened"],
            "revoked_at": r["revoked_at"], "revoked_by": r["revoked_by"]}


@_locked
def create_share_link(conn, label: str, token_hash: str, created_by: str | None = None,
                      expires_at: int | None = None, now: int | None = None) -> dict[str, Any]:
    """Store a link by the hash of its token. `ValueError("too-many")` once
    MAX_ACTIVE_SHARE_LINKS are live: revoke one, or let one expire."""
    now = now or _now()
    purge_share_links(conn, now)
    live = conn.execute(
        "SELECT COUNT(*) n FROM share_links WHERE revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?)", (now,)).fetchone()["n"]
    if int(live) >= MAX_ACTIVE_SHARE_LINKS:
        raise ValueError("too-many")
    conn.execute(
        "INSERT INTO share_links (token_hash, label, created_by, created_at, expires_at) "
        "VALUES (?,?,?,?,?)", (token_hash, label, created_by, now, expires_at))
    row = conn.execute("SELECT * FROM share_links WHERE token_hash = ?",
                       (token_hash,)).fetchone()
    return _share_row(row)


@_locked
def share_link_by_token(conn, token_hash: str) -> dict[str, Any] | None:
    """The link a token belongs to in WHATEVER state it is in, or None.

    The exchange endpoint needs the dead ones too: telling someone their link
    expired, or was withdrawn, is kinder than "not valid", and says nothing a
    stranger could use -- the token is 256 random bits, so it is only ever in
    the hands of somebody it was given to."""
    row = conn.execute("SELECT * FROM share_links WHERE token_hash = ?",
                       (token_hash,)).fetchone()
    return _share_row(row) if row else None


@_locked
def live_share_link(conn, token_hash: str, now: int | None = None) -> dict[str, Any] | None:
    """The link behind a token if it can be used NOW, else None.

    Checked against the database on every request, as a machine token is, so
    revoking a link ends it on its holder's next click and not at some later
    sweep. Stamps `last_used_at`, at most once a minute."""
    now = now or _now()
    link = share_link_by_token(conn, token_hash)
    if link is None or share_link_state(link, now) != "live":
        return None
    last = link["last_used_at"]
    if last is None or now - int(last) >= SHARE_LINK_TOUCH_SECONDS:
        conn.execute("UPDATE share_links SET last_used_at = ? WHERE id = ?", (now, link["id"]))
        link["last_used_at"] = now
    return link


@_locked
def note_share_link_opened(conn, link_id: int, now: int | None = None) -> None:
    """Somebody redeemed the link (as opposed to a request made under it)."""
    now = now or _now()
    conn.execute("UPDATE share_links SET opened = opened + 1, last_used_at = ? WHERE id = ?",
                 (now, link_id))


@_locked
def list_share_links(conn, now: int | None = None) -> list[dict[str, Any]]:
    """Every link still on record, newest first, each with its `state`. Never the
    token, and not its hash either: nothing here can be used to sign in."""
    now = now or _now()
    rows = conn.execute("SELECT * FROM share_links ORDER BY created_at DESC, id DESC").fetchall()
    out = []
    for r in rows:
        link = _share_row(r)
        link["state"] = share_link_state(link, now)
        out.append(link)
    return out


@_locked
def revoke_share_link(conn, link_id: int, by: str | None = None, now: int | None = None) -> bool:
    """Take a link back. False if there is no such link or it was already revoked."""
    now = now or _now()
    cur = conn.execute(
        "UPDATE share_links SET revoked_at = ?, revoked_by = ? WHERE id = ? AND revoked_at IS NULL",
        (now, by, link_id))
    return bool(cur.rowcount)


@_locked
def purge_share_links(conn, now: int | None = None) -> int:
    """Drop links that ended more than SHARE_LINK_KEEP_SECONDS ago."""
    cutoff = (now or _now()) - SHARE_LINK_KEEP_SECONDS
    cur = conn.execute(
        "DELETE FROM share_links WHERE (revoked_at IS NOT NULL AND revoked_at < ?) "
        "OR (expires_at IS NOT NULL AND expires_at < ?)", (cutoff, cutoff))
    return int(cur.rowcount or 0)


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
                 dry_run: bool = True, limit: int = 50,
                 include_pending: bool = False) -> dict[str, Any]:
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

    ``include_pending`` also refunds cases that were charged an attempt and are
    still PENDING, which a broken machine produces far more of than quarantines.
    COD-358-21's full disk (2026-09-26) failed 663 cases in 38 minutes, each once,
    and none of them was quarantined, so reopen could not reach any. Their attempts
    are reset and their error cleared; their state is unchanged. It needs
    ``error_contains`` or ``case_ids``: refunding every pending case with a failure
    on record would also forgive the failures that were real.
    """
    if include_pending and not (error_contains or case_ids):
        raise ValueError("include_pending needs error_contains or case_id: refunding every "
                         "pending failure would forgive the real ones too")
    states = ("quarantined", "pending") if include_pending else ("quarantined",)
    where = ["(state = 'quarantined'"
             + (" OR (state = 'pending' AND (attempts > 0 OR last_error IS NOT NULL))"
                if include_pending else "") + ")"]
    params: list[Any] = []
    if case_ids:
        where.append("case_id IN (%s)" % ",".join("?" for _ in case_ids))
        params.extend(case_ids)

    rows = conn.execute(
        "SELECT case_id, state, attempts, max_attempts, updated_at FROM cases"
        " WHERE " + " AND ".join(where) + " ORDER BY case_id", params).fetchall()
    # Each case's LAST failure, in one pass rather than a query per case: with
    # pending cases in the scan there can be thousands, and this holds the lock
    # every lease and heartbeat waits on.
    last_failure = {r["case_id"]: r["detail"] or "" for r in conn.execute(
        "SELECT e.case_id, e.detail FROM events e JOIN (SELECT case_id, MAX(id) AS id"
        " FROM events WHERE event IN ('failed', 'quarantined') GROUP BY case_id) f"
        " ON f.id = e.id").fetchall()}

    found, ids_hit = [], []
    for r in rows:
        row = dict(r)
        detail = last_failure.get(row["case_id"], "")
        if error_contains and error_contains.lower() not in detail.lower():
            continue
        ids_hit.append(row["case_id"])
        if len(found) < limit:
            found.append({"case_id": row["case_id"], "state": row["state"],
                          "attempts": row["attempts"], "last_error": detail[:200]})

    # The cap applies to what is CHANGED, not only to what is listed back.
    to_reopen = ids_hit[:limit]

    out: dict[str, Any] = {"matched": len(ids_hit), "examples": found,
                           "dry_run": dry_run, "reopened": 0,
                           "capped": len(ids_hit) > len(to_reopen)}
    if dry_run or not to_reopen:
        return out

    # A few statements per chunk, never two per case. This holds the lock every
    # lease, heartbeat and /healthz ping waits on, and each statement is a round
    # trip to Supabase: about 80 ms from production (measured 2026-09-26, 25
    # cases in 4.5 s). Refunding COD-358-21's 663 cases in one call was ~1,300 of
    # them -- well over a minute of a broker that answered nothing -- and it came
    # back 502 with nothing applied: the broker went down mid-transaction.
    now = _now()
    detail = "attempts refunded: " + (error_contains or "reopened by an operator")
    state_marks = ",".join("?" for _ in states)
    changed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for i in range(0, len(to_reopen), 500):
            chunk = to_reopen[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            # A case leased between the scan and here is left alone: it is
            # running, and its attempt is its own.
            ids = [r["case_id"] for r in conn.execute(
                "SELECT case_id FROM cases WHERE case_id IN (" + marks + ")"
                " AND state IN (" + state_marks + ")", (*chunk, *states)).fetchall()]
            if not ids:
                continue
            id_marks = ",".join("?" for _ in ids)
            conn.execute(
                # last_error goes with the attempts. The counter is reset because
                # the history says nothing about the case; the message is the same
                # history in prose, and leaving it behind puts a red "Last Failure
                # Error" banner on a case that is now pending and blameless --
                # which is the state an operator reopened it INTO.
                "UPDATE cases SET state='pending', lease_id=NULL, lease_worker=NULL,"
                " lease_expires=NULL, leased_at=NULL, attempts=0, last_error=NULL,"
                " updated_at=?"
                " WHERE case_id IN (" + id_marks + ") AND state IN (" + state_marks + ")",
                (now, *ids, *states))
            conn.execute(
                "INSERT INTO events(ts, case_id, worker_id, event, detail) VALUES "
                + ",".join("(?,?,?,?,?)" for _ in ids),
                [v for cid in ids for v in (now, cid, None, "reopened", detail)])
            changed += len(ids)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    out["reopened"] = changed
    return out


def _respec_links(trail: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
    """`moved_from` / `moved_to` for one case, read off its 'respec' events.

    Both cases of a move carry the same event, so which side this case was on is
    read from the ids in it. A case moved twice (v4 -> v5 -> v6) was moved TO
    the last one: the latest link wins.
    """
    links: dict[str, Any] = {"moved_from": None, "moved_to": None}
    for e in trail:
        if e.get("event") != "respec":
            continue
        try:
            d = json.loads(e.get("detail") or "")
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        said = {"at": e.get("ts"), "by": d.get("by"), "reason": d.get("reason")}
        if d.get("to") == case_id:
            links["moved_from"] = {"case_id": d.get("from"), "recipe": d.get("from_recipe"), **said}
        elif d.get("from") == case_id:
            links["moved_to"] = {"case_id": d.get("to"), "recipe": d.get("to_recipe"), **said}
    return links


@_locked
def respec_cases(conn, recipe: str, *, case_ids: list[str] | None = None,
                 error_contains: str | None = None, reason: str | None = None,
                 by: str | None = None, dry_run: bool = True, limit: int = 50,
                 now: int | None = None) -> dict[str, Any]:
    """Move cases to another recipe: each site is admitted again under ``recipe``
    and the case it came from is parked, so nothing spends attempts on it again.

    A NEW case, never an edited one. A case id is a function of the site and the
    recipe (DOMAIN.md, invariant 1), so a re-spec coexists with its original, and
    that is exactly what makes the move safe:

    * a node that does not declare ``recipe`` is never handed the new case -- the
      lease filters on the exact recipe -- so a site the old recipe cannot build
      (v2-00ed64225d4979d7: v4's snappy aborts in its post-snap merge, every
      machine, every rank count) stops costing attempts on nodes that only know
      the old one;
    * a node's scratch and its resume list are keyed by case id, so no machine can
      resume the old recipe's mesh as the new case -- the failure that cost
      v2-00427078fdfaa380 three attempts after a reopen.

    The new case is what ``POST /v1/cases`` makes of the same site and recipe: the
    old spec with its recipe replaced, the same city, LCZ, split, priority, attempt
    budget and labels. Posting the site under the new recipe later is therefore a
    no-op, and one that was posted already is linked rather than duplicated.

    The old case goes to quarantine, which already means "not going to run, and
    the trail says why", with a 'quarantined' event naming the new case. That
    event is what reopen matches on, so reopening by the error that got a case
    moved cannot quietly put the old recipe back in the queue beside the new one;
    by id it still can, on purpose. Its ``last_error`` is cleared: the case needs
    nobody now, and ``/v1/errors`` is the list of what does. Both cases get a
    'respec' event, which ``get_case`` turns into ``moved_to`` / ``moved_from``.

    Selected by ``case_ids``, by ``error_contains`` (the case's LAST failure, as
    reopen matches it), or both as an AND; one of them is required. A leased case
    is skipped (a node is solving it: cancel it first -- nothing here releases a
    live lease), a done one too (its result stands; post the site under the new
    recipe for a second run), and so is one already on ``recipe`` or already
    moved to it. Each skip says why.

    A recipe no worker has ever declared and no case carries raises ValueError:
    that is a typo, and the cases would wait for a node that does not exist.
    ``known_to_builds`` in the answer names the builds that do declare it.

    ``dry_run`` defaults to TRUE and ``limit`` bounds the WRITES, as in reopen.
    """
    from . import ids as _ids

    recipe = (recipe or "").strip()
    if not recipe:
        raise ValueError("name the recipe to move the cases to")
    if not case_ids and not error_contains:
        raise ValueError("name the cases to move: case_id, error_contains, or both")
    limit = max(1, min(int(limit), 5000))

    knows, _queue = _recipe_knowledge(conn)
    declared: set[str] = set()
    for r in conn.execute("SELECT recipes FROM workers WHERE recipes IS NOT NULL").fetchall():
        try:
            names = json.loads(r["recipes"])
        except (TypeError, ValueError):
            continue
        if isinstance(names, list):
            declared.update(str(n) for n in names)
    carried = conn.execute("SELECT 1 FROM cases WHERE recipe = ? LIMIT 1", (recipe,)).fetchone()
    if recipe not in declared and carried is None:
        raise ValueError(
            "no worker has declared recipe %r and no case carries it -- a typo? Declared: %s"
            % (recipe, ", ".join(sorted(declared)) or "none"))

    needle = (error_contains or "").lower()
    if case_ids:
        wanted = list(dict.fromkeys(case_ids))
    else:
        # By failure text alone: the cases whose LAST failure says it, found in
        # one pass over the failure events rather than a query per case in the
        # pool -- this holds the lock every lease and heartbeat waits on, and the
        # pool is thousands of cases on a database across a network.
        wanted = [r["case_id"] for r in conn.execute(
            "SELECT e.case_id, e.detail FROM events e JOIN (SELECT case_id, MAX(id) AS id"
            " FROM events WHERE event IN ('failed', 'quarantined') GROUP BY case_id) f"
            " ON f.id = e.id").fetchall() if needle in (r["detail"] or "").lower()]
    cols = ("SELECT case_id, spec, recipe, state, lease_worker, city_cluster, lcz, split,"
            " priority, max_attempts FROM cases")
    rows: list[dict[str, Any]] = []
    for i in range(0, len(wanted), 500):
        part = wanted[i:i + 500]
        rows.extend(dict(r) for r in conn.execute(
            cols + " WHERE case_id IN (" + ",".join("?" for _ in part) + ")", part).fetchall())
    if not case_ids:
        # Only what the queue holds or has parked: a done case's old failure says
        # nothing about it now.
        rows = [r for r in rows if r["state"] in ("pending", "leased", "quarantined")]
    rows.sort(key=lambda r: r["case_id"])

    movable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        cid = row["case_id"]
        if case_ids and needle:
            last = conn.execute(
                "SELECT detail FROM events WHERE case_id=? AND event IN ('failed', 'quarantined')"
                " ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            if needle not in ((dict(last)["detail"] if last else None) or "").lower():
                continue
        spec = row["spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)
        lat, lon = spec.get("lat"), spec.get("lon")
        why = None
        if row["recipe"] == recipe:
            why = "already on %s" % recipe
        elif row["state"] == "leased":
            why = "leased to %s: a node is working on it -- cancel it first" % row["lease_worker"]
        elif row["state"] == "done":
            why = "done: its result stands -- post the site under %s for a second run" % recipe
        elif lat is None or lon is None:
            why = "its spec has no coordinates to derive the new case from"
        new_id = None if why else _ids.case_id(float(lat), float(lon), recipe)
        if new_id and row["state"] == "quarantined":
            trail = [dict(e) for e in conn.execute(
                "SELECT ts, event, detail FROM events WHERE case_id=? AND event='respec'"
                " ORDER BY id", (cid,)).fetchall()]
            if (_respec_links(trail, cid)["moved_to"] or {}).get("case_id") == new_id:
                why = "already moved to %s as %s" % (recipe, new_id)
        if why:
            skipped.append({"case_id": cid, "state": row["state"], "recipe": row["recipe"], "why": why})
            continue
        exists = conn.execute("SELECT 1 FROM cases WHERE case_id=?", (new_id,)).fetchone() is not None
        movable.append({**row, "spec": spec, "new_case_id": new_id, "new_exists": exists})

    to_move = movable[:limit]
    shown = ("case_id", "state", "recipe", "new_case_id", "new_exists")
    out: dict[str, Any] = {
        "recipe": recipe, "known_to_builds": sorted(b for b, n in knows.items() if recipe in n),
        "matched": len(movable), "moved": 0, "capped": len(movable) > len(to_move),
        "dry_run": dry_run,
        "examples": [{k: m[k] for k in shown} for m in movable[:limit]],
        "skipped": skipped[:limit], "skipped_total": len(skipped)}
    if dry_run or not to_move:
        return out

    now = now or _now()
    # The same row add_cases writes for this site and recipe.
    insert_sql = ("INSERT INTO cases (case_id, spec, recipe, city_cluster, lcz, split, priority,"
                  " max_attempts, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    moved = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for m in to_move:
            old, new = m["case_id"], m["new_case_id"]
            # Guarded on the state it was read in: a case leased since the scan
            # belongs to its node now, and is left to it.
            parked = conn.execute(
                "UPDATE cases SET state='quarantined', last_error=NULL, updated_at=?"
                " WHERE case_id=? AND state IN ('pending', 'quarantined')", (now, old)).rowcount
            if not parked:
                continue
            if conn.execute("SELECT 1 FROM cases WHERE case_id=?", (new,)).fetchone() is None:
                spec = dict(m["spec"], recipe=recipe)
                conn.execute(insert_sql, (new, json.dumps(spec, sort_keys=True), recipe,
                                          m["city_cluster"], m["lcz"], m["split"], m["priority"],
                                          m["max_attempts"], now, now))
                for lab in conn.execute("SELECT key, value FROM case_labels WHERE case_id=?",
                                        (old,)).fetchall():
                    conn.execute("INSERT INTO case_labels(case_id, key, value) VALUES (?,?,?)",
                                 (new, lab["key"], lab["value"]))
                _event(conn, new, None, "created", recipe, now)
            link = json.dumps({"from": old, "to": new, "from_recipe": m["recipe"],
                               "to_recipe": recipe, "by": by, "reason": reason}, sort_keys=True)
            _event(conn, new, None, "respec", link, now)
            _event(conn, old, None, "respec", link, now)
            _event(conn, old, None, "quarantined",
                   "moved to %s as %s%s%s" % (recipe, new, (" by " + by) if by else "",
                                              (": " + reason) if reason else ""), now)
            moved += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    out["moved"] = moved
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
        # The moment an admin has ten minutes to act on, so it is in the trail
        # (and push.py announces it) -- not the code: approving needs the
        # Machines tab, where the code is shown, so the trail needs only who.
        _event(conn, None, name, "pair-request",
               " · ".join(x for x in (host, platform) if x) or None, now)
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


# -- push notifications (casebroker/push.py) -------------------------------------
#
# The statements behind browser push, here for the reason every statement is: this
# module owns the SQL and the lock. push.py keeps three settings rows. `push_policy`
# (the broker-wide switch per notice kind) is written through set_setting, which
# audits it, because a switch an admin flips is what a trail is for. The other two
# are written here and NOT audited: `push_state` moves on every tick, and
# `push_vapid_private` is a private key, which an audit line would copy into every
# trail that lists settings changes.

PUSH_VAPID_KEY = "push_vapid_private"
PUSH_STATE_KEY = "push_state"
PUSH_POLICY_KEY = "push_policy"
PUSH_ORIGIN_KEY = "push_origin"


@_locked
def push_setting(conn, key: str) -> str | None:
    """push.py's policy, state or learned origin. Not the VAPID key, which has its
    own reader, so nothing that asks for "a push setting" can be handed it by name."""
    if key not in (PUSH_STATE_KEY, PUSH_POLICY_KEY, PUSH_ORIGIN_KEY):
        raise ValueError("not a push setting: %s" % key)
    return _setting(conn, key)


@_locked
def remember_push_origin(conn, origin: str, now: int | None = None) -> bool:
    """The dashboard's public https origin, as a browser subscribing from it named
    it: the VAPID subject when none is configured. Learned, not set, so not audited,
    and written only when it changes -- a subscribe is not otherwise a settings write."""
    if _setting(conn, PUSH_ORIGIN_KEY) == origin:
        return False
    conn.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (PUSH_ORIGIN_KEY, origin, now or _now(), "push"))
    return True


@_locked
def push_vapid_key(conn, generate: Callable[[], str], now: int | None = None) -> str:
    """This broker's VAPID private key, made by ``generate`` on first use and kept.

    Inserted with DO NOTHING and then read back, so two processes starting at once
    agree on ONE key. A second key would orphan every browser subscribed under the
    first: a subscription is bound to the public key it was made with, and a push
    signed by any other is refused."""
    have = _setting(conn, PUSH_VAPID_KEY)
    if have:
        return have
    conn.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?)"
                 " ON CONFLICT(key) DO NOTHING", (PUSH_VAPID_KEY, generate(), now or _now(), "push"))
    return _setting(conn, PUSH_VAPID_KEY)


@_locked
def claim_push_state(conn, old: str | None, new: str, now: int | None = None) -> bool:
    """Move the notifier's state from ``old`` to ``new`` unless somebody moved it
    first -- compare-and-set, so two broker processes overlapping in a deploy never
    both announce one event. True when this caller won."""
    now = now or _now()
    if old is None:
        cur = conn.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?)"
                           " ON CONFLICT(key) DO NOTHING", (PUSH_STATE_KEY, new, now, "push"))
    else:
        cur = conn.execute("UPDATE settings SET value = ?, updated_at = ? WHERE key = ? AND value = ?",
                           (new, now, PUSH_STATE_KEY, old))
    return bool(cur.rowcount)


@_locked
def push_events_after(conn, after_id: int, events: Iterable[str], before: int,
                      not_before: int = 0, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
    """``(rows, cursor)``: the named events after ``after_id``, oldest first, each
    with its case's spec, and how far a reader has now read.

    Read in id order and stopped at the first row of ANY kind stamped at or after
    ``before`` (a few seconds ago): a row whose transaction took a moment to commit
    can land with a lower id than one already visible, and reading past it would
    skip it for good. Rows older than ``not_before`` are passed over rather than
    announced -- a broker that was down for a day does not wake everyone with it."""
    names = list(events)
    stop = conn.execute("SELECT MIN(id) AS n FROM events WHERE id > ? AND ts >= ?",
                        (after_id, before)).fetchone()["n"]
    upper = int(stop) if stop is not None else None
    sql = ("SELECT e.id, e.ts, e.case_id, e.worker_id, e.event, e.detail, c.spec"
           " FROM events e LEFT JOIN cases c ON c.case_id = e.case_id"
           " WHERE e.id > ? AND e.ts >= ? AND e.event IN (%s)" % ",".join("?" * len(names)))
    params: list[Any] = [after_id, not_before, *names]
    if upper is not None:
        sql += " AND e.id < ?"
        params.append(upper)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY e.id LIMIT ?", (*params, limit)).fetchall()]
    if len(rows) >= limit:
        return rows, int(rows[-1]["id"])
    if upper is not None:
        return rows, upper - 1
    top = conn.execute("SELECT MAX(id) AS n FROM events WHERE id > ?", (after_id,)).fetchone()["n"]
    return rows, int(top) if top is not None else after_id


@_locked
def push_snapshot(conn, silent_before: int) -> dict[str, Any]:
    """What push.py judges its standing conditions from, in one locked read: cases
    by state, the leased cases whose worker was last heard from before
    ``silent_before``, when the oldest current lease began, when a case last
    finished, and the newest event id."""
    by_state = {r["state"]: int(r["n"]) for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM cases GROUP BY state").fetchall()}
    silent = [dict(r) for r in conn.execute(
        "SELECT c.case_id, c.lease_worker AS worker_id, w.last_seen FROM cases c"
        " JOIN workers w ON w.worker_id = c.lease_worker"
        " WHERE c.state = 'leased' AND w.last_seen < ? ORDER BY c.case_id",
        (silent_before,)).fetchall()]
    oldest = conn.execute("SELECT MIN(leased_at) AS t FROM cases WHERE state = 'leased'").fetchone()["t"]
    last_done = conn.execute("SELECT MAX(ts) AS t FROM events WHERE event = 'done'").fetchone()["t"]
    newest = conn.execute("SELECT MAX(id) AS n FROM events").fetchone()["n"]
    return {"by_state": by_state, "silent": silent,
            "oldest_lease": int(oldest) if oldest is not None else None,
            "last_done": int(last_done) if last_done is not None else None,
            "newest_event": int(newest) if newest is not None else 0}


def _push_row(r) -> dict[str, Any]:
    d = dict(r)
    try:
        d["events"] = [str(e) for e in json.loads(d.get("events") or "[]")]
    except ValueError:
        d["events"] = []
    return d


@_locked
def save_push_subscription(conn, endpoint: str, p256dh: str, auth: str, events: list[str],
                           subscriber_kind: str, subscriber: str, role: str | None,
                           user_agent: str | None, now: int | None = None,
                           per_subscriber: int = 25, total: int = 500) -> str:
    """Insert or replace one browser's subscription; ``created`` or ``updated``.

    Bounded, because every row is a POST this broker makes on every notice and
    any reader may add one: past ``total`` rows a new one is refused
    (ValueError("too-many")), and past ``per_subscriber`` for one credential its
    least recently used is dropped -- browsers that were reset or thrown away
    leave rows behind, and the person adding a new one is the one who knows."""
    now = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        had = conn.execute("SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        if had is None:
            if conn.execute("SELECT COUNT(*) AS n FROM push_subscriptions").fetchone()["n"] >= total:
                raise ValueError("too-many")
            mine = conn.execute(
                "SELECT endpoint FROM push_subscriptions WHERE subscriber_kind = ? AND subscriber = ?"
                " ORDER BY COALESCE(last_sent_at, created_at), created_at",
                (subscriber_kind, subscriber)).fetchall()
            for r in mine[:max(0, len(mine) - per_subscriber + 1)]:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (r["endpoint"],))
        conn.execute(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, events, subscriber_kind,"
            " subscriber, role, user_agent, created_at, failures) VALUES (?,?,?,?,?,?,?,?,?,0)"
            " ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth,"
            " events = excluded.events, subscriber_kind = excluded.subscriber_kind,"
            " subscriber = excluded.subscriber, role = excluded.role,"
            " user_agent = excluded.user_agent, failures = 0, last_error = NULL",
            (endpoint, p256dh, auth, json.dumps(list(events)), subscriber_kind, subscriber,
             role, user_agent, now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "updated" if had is not None else "created"


@_locked
def push_subscription(conn, endpoint: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
    return _push_row(row) if row is not None else None


@_locked
def push_subscriptions(conn) -> list[dict[str, Any]]:
    return [_push_row(r) for r in conn.execute(
        "SELECT * FROM push_subscriptions ORDER BY created_at, endpoint").fetchall()]


@_locked
def set_push_subscription_events(conn, endpoint: str, events: list[str]) -> bool:
    return bool(conn.execute("UPDATE push_subscriptions SET events = ? WHERE endpoint = ?",
                             (json.dumps(list(events)), endpoint)).rowcount)


@_locked
def delete_push_subscription(conn, endpoint: str) -> bool:
    return bool(conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?",
                             (endpoint,)).rowcount)


@_locked
def note_push_delivery(conn, endpoint: str, error: str | None = None, gone: bool = False,
                       drop_after: int = 10, now: int | None = None) -> str:
    """Record one send: ``sent``, ``failed`` or ``dropped``. ``gone`` is the push
    service saying the browser unsubscribed (404/410), which drops the row at once;
    any other failure counts, and ``drop_after`` of them in a row drop it."""
    now = now or _now()
    if gone:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        return "dropped"
    if error is None:
        conn.execute("UPDATE push_subscriptions SET last_sent_at = ?, failures = 0, last_error = NULL"
                     " WHERE endpoint = ?", (now, endpoint))
        return "sent"
    conn.execute("UPDATE push_subscriptions SET failures = failures + 1, last_error = ?"
                 " WHERE endpoint = ?", (error[:300], endpoint))
    row = conn.execute("SELECT failures FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
    if row is not None and int(row["failures"]) >= drop_after:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        return "dropped"
    return "failed"


@_locked
def push_subscriber_role(conn, kind: str, who: str, now: int | None = None) -> str | None:
    """The role the credential behind a subscription has NOW, or None when it is
    gone: an account (its current role), a share link (viewer while it is live), a
    machine credential (operator while it is not revoked). The environment's tokens
    are not in this database; app.py answers for those."""
    if kind == "user":
        row = conn.execute("SELECT role FROM users WHERE username = ?", (who,)).fetchone()
        return row["role"] if row is not None else None
    if kind == "share":
        try:
            link_id = int(who)
        except ValueError:
            return None
        row = conn.execute("SELECT expires_at, revoked_at FROM share_links WHERE id = ?",
                           (link_id,)).fetchone()
        return "viewer" if row is not None and share_link_state(dict(row), now) == "live" else None
    if kind == "machine":
        row = conn.execute("SELECT 1 FROM worker_tokens WHERE name = ? AND revoked_at IS NULL",
                           (who,)).fetchone()
        return "operator" if row is not None else None
    return None
