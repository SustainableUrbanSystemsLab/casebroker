# Changelog

This project follows [Semantic Versioning](https://semver.org). The version is
declared **once**, in `pyproject.toml`; see "Versioning" in the README for what
each component means here and how to cut a release.

The format follows [Keep a Changelog](https://keepachangelog.com).

## [Unreleased]

### Added
- **Cases that are not on land are refused at the door.** `POST /v1/cases` drops
  any site the building atlas does not cover and reports it as
  `rejected_not_on_land`, with a sample of what went. The rest of the batch
  still lands: a 5,000-case draw with twenty bad sites should not be blocked by
  them, and the sampler's loader raises on a rejected batch. The mask is the 922
  tile keys GBA actually publishes (a global 5° grid would be 2,592; the rest
  are ocean and ice), vendored in `casebroker/_gba_tiles.py`, so the land test
  and the building source are the same source. Coarse by design — it catches the
  mid-Atlantic and the ice sheets, not a point 2 km offshore — and it keeps every
  real Arctic city the sampler deliberately retains.
- **`POST /v1/cases/land-audit`** sweeps the cases that predate that gate.
  Dry-run by default; with `dry_run=false` it quarantines them rather than
  deleting them, so nothing leases them while the rows, their history and the
  campaign's own record of what the draw produced all survive. `done` cases are
  left alone.
- **A third view: the side elevation.** Looking north across the whole mesh
  domain, everything projected onto one vertical plane, with a true metre scale.
  Neither of the other two could answer whether the terrain or the buildings
  dominate what the wind meets: in plan the relief is a wash of colour, and the
  isometric exaggerates height against plan distance, so the comparison there is
  rigged by construction. Here both are the same axis. The terrain is drawn as a
  band rather than a line because each column spans 2608 m of ground north to
  south and both its extremes are true; the building mass is faint with a hard
  skyline over it, because a thousand roofs in a projection otherwise merge into
  one blue wall that hides the ground they stand on.

### Changed
- **Buildings pierce the terrain instead of sitting on it.** `watertight.py`
  extrudes every prism from its roof down to a common slab about 20 m below the
  lowest ground in the domain, which is what closes the solid where a footprint
  meets a hill -- the preview drew them perched on the surface, which hid that
  and, on a slope, left flat-bottomed boxes floating over the downhill side. The
  elevation draws the full buried length; the isometric shows only the first 9 m
  of it, because at that exaggeration a prism on a hilltop trails more ghost
  than building and 227 of them are stalactites.

### Fixed
- **Nobody saw any trees, anywhere.** The footprints cache is keyed on
  `case_id` alone and carries no schema column, so a payload written before
  terrain and canopy existed was served forever -- every case anyone had already
  opened kept answering with neither, which is indistinguishable from a world
  with no trees in it. Payloads are now stamped with `payload_v` and a hit
  carrying anything else is treated as a miss, so the first open after a shape
  change re-queries once and caches the new answer.
- **A site the building atlas does not cover returned 502.** GlobalBuildingAtlas
  publishes 922 tiles of a possible 2,592; the rest are ocean and ice. A 404 on
  the tile URL propagated as `building query failed`, which took the terrain and
  the canopy down with it -- so a case in the Atlantic showed a transport error
  instead of the far more useful answer the other two sources were ready to
  give. A missing tile is now `TileNotPublished`, distinct from a transport
  failure, and comes back as an empty building list. The three layers are
  fetched independently, and when all three report a gap the panel says so
  outright: *this case is not on land*. A missing tile is cached (it is a fact
  about the site); an unreachable mirror is reported but NOT cached, so
  reopening the case retries instead of freezing a blip into an empty city.

### Added
- **The site preview shows terrain and trees, not just buildings.**
  `GET /v1/cases/{id}/footprints` now returns `terrain` (a GEDTM30 relief grid)
  and `canopy` (Meta/WRI 1 m tree heights) alongside the footprints, both over
  the full 1304 m mesh domain rather than the 520 m building box -- which is
  what `site_geometry.build_site` reads (`half_t = HALF_M + buffer_m`). The
  inspector draws all three: relief shaded under the plan view, canopy in green,
  and in the isometric the buildings now stand *on* the terrain with see-through
  green crown volumes beside them. Crowns are drawn see-through because that is
  what they are in the solve -- the upper 59% of each tree becomes a porous
  cellZone carrying `f = 2*Cd*LAD`, not a solid the flow goes around.

  A treeless site and a site the canopy model does not cover are reported
  differently (`canopy.source` is `meta-wri-chm-v1` with `frac_canopy: 0` versus
  `none`), because only one of them means something is wrong.

  The canopy tile key is computed rather than looked up: the Meta/WRI tiles are
  named by zoom-9 Bing quadkey, which the product's own 1.194 m resolution
  fixes, so the broker skips the 15 MB `tiles.geojson` index `canopy.py`
  downloads. Pinned against published objects on four continents in
  `tests/test_chm_tiles.py`.

### Fixed
- **The web service exceeded its memory limit and was restarted.** Two causes,
  both in the footprints path. DuckDB sizes `memory_limit` and `threads` from
  `/proc/meminfo`, which inside a container reports the HOST rather than the
  cgroup limit the platform enforces: it reported a 51.1 GiB limit and 12
  threads while running in a far smaller instance, so it never spilled and was
  OOM-killed instead. And the handler used a bare `duckdb.connect()` that was
  never closed, so every request leaked a whole buffer pool. Ceilings are now
  explicit (`CASEBROKER_DUCKDB_MEMORY`, `CASEBROKER_DUCKDB_THREADS`,
  `CASEBROKER_GDAL_CACHE_MB`) and the connection is a context manager. Measured
  after: ~55 MB at rest, ~280 MB once the geo libraries are resident, and flat
  across repeated queries instead of climbing. See "Sizing the instance" in
  `docs/operations.md`.
- **The Overture fallback is off unless asked for.** It shells out to a second
  Python interpreter that loads pyarrow and materialises a whole GeoJSON, which
  is the one part of this path no ceiling above can reach -- so it ran
  automatically on every GBA hiccup inside a memory-capped process. Set
  `CASEBROKER_OVERTURE_FALLBACK=1` to restore it while the mirror is down.
- **The preview's elapsed-time counter jumped around, and the drawing vanished
  once a minute.** The 60 s auto-refresh replaces the whole case table, which
  threw away any rendered panel and restarted its fetch against a new `t0` while
  the previous interval was still writing into an element both now shared -- so
  the count could run backwards. The counter is now scoped to its own panel and
  the payload is cached per case for the life of the page, so a refresh redraws
  instantly and issues no request.
- **The panel said "Overture" while drawing GlobalBuildingAtlas.** Labels,
  legend and captions now describe what is actually fetched, including that
  every GBA height is a PREDICTION (published RMSE 1.5-8.9 m) and what its
  per-building variance is -- the "measured vs inferred" split the panel used to
  show was an Overture-era question that GBA does not have.
- **Raster statistics were computed at drawing resolution.** Averaging a 65 m
  preview cell over trees, roofs and road reported Atlanta, a city of 25 m oaks,
  as having a tallest tree of 8 m. Both rasters are now read four times finer
  than they are drawn and the statistics taken there; the read costs the same,
  since what it fetches is fixed by the window and not by the shape asked back.

## [0.3.0] - 2026-09-15

Hooking heterogeneous machines into the campaign: Windows workstations (Docker
or native blueCFD-Core) alongside the PACE clusters, with progress visible on
the dashboard, stopped solves resuming on the same machine, and one archive per
finished case for Syncthing / a master-side pull to collect. See
`docs/fleet.md`. MINOR: every protocol change is an optional addition.

### Added
- **An `operator` role, and a Users tab to hand it out.** `admin` was the only
  role that could write anything, so "let this person run the campaign" and
  "let this person delete every account including yours" were the same grant --
  which is how a deployment ends up with everyone an admin. An operator adds
  cases, leases, heartbeats, completes, fails, releases and reports fleet
  counts, and manages nothing: no accounts, no machine credentials, and no
  purging. It is the role most accounts should have.

  Two things stay with `admin` deliberately. **Purging** (`DELETE /v1/cases`)
  takes the cases, their events and their footprints, and is the one campaign
  operation with nothing behind it. **Machine credentials** can write the
  campaign and outlive the account that issued them, so an operator who could
  mint one would be an admin with extra steps -- revoking the person would not
  revoke what they left behind. A write BEARER token can still purge, as it
  always could: the documented `curl` depends on it, and narrowing it would not
  make anything safer, since whoever holds the token can simply use it. What
  changed is that an operator session is not enough.

  Handing out a role no longer needs shell access to a box holding the DSN:
  **Settings ▸ Users** lists every account with its role and last login, adds
  one, changes a role (effective on that account's next request, with no
  re-login), resets a password and deletes. `GET /v1/auth/state` now reports
  the `roles` this broker accepts, so the picker is built from the server
  rather than from a list in the page that could drift -- a drift that would
  surface as a `400` at the moment someone is adding a colleague. Rows are
  wired by data attribute rather than inline `onclick`, since a username is
  attacker-chosen text. `casebroker account create --role operator` reads the
  same list, so the CLI cannot fall behind either.
- **Settings, with the first-run wizard behind it.** The dashboard opened on a
  Connection panel -- a form you had already filled in, a bearer-token box a
  signed-in operator has no use for, and a bar naming the live database host on
  a page that needs no credential to load. All of it moves behind a gear:
  Setup, Connection, Machines and Preferences, with the campaign now the first
  thing on the page. The wizard is `docs/operations.md`'s "First run" answered
  from state the page has already fetched -- a database, an admin account, a
  session, a machine credential, and a worker that has actually leased
  something -- so it costs no extra request. A finished step collapses to one
  line and only the first unfinished one expands, which is what makes it a
  checklist rather than five forms at once; when every step is done the gear's
  attention dot goes out and the panel says so in one line. The one state that
  cannot be left behind a gear -- a broker with no account at all -- still gets
  a banner on the page itself, because an operator cannot be expected to go
  looking for a drawer they have never opened.
- **One credential per cluster.** A machine token may lease as its own name or
  as any worker id under it -- `phoenix` covers `phoenix-<job>-<task>`, the id
  every SLURM task runs as (`slurm/*.sbatch`). Without this the per-machine
  model stopped at the workstation door: a cluster's tasks are named by the
  scheduler, one credential per task is impossible, and a token issued for
  `phoenix` refused every lease with a `403` -- so clusters stayed on the
  shared env token, precisely the un-revocable, un-attributed model the
  feature exists to replace. Attribution holds at the granularity a cluster
  credential can have: the lease records the per-task id and the credential
  is its prefix. The dash is load-bearing; `lab` does not cover `laboratory`.
- **A contract E3D can implement against** -- `docs/e3d-contract.md`. When
  `$E3D_TRACE_FILE` is set, the solver appends one JSON record per outer
  iteration; the runner tails the last line for the heartbeat and archives the
  whole file beside the result, so one file serves both readers and there is no
  second live-progress file to keep in sync. Undecimated on purpose: ~290 KB per
  2,000 iterations is noise beside the fields in the same archive, you can
  always downsample a full trace but never upsample a decimated one, and
  decimation hides exactly the oscillation worth finding later. A solver that
  writes no trace, or a truncated final record from a crash, falls back to
  parsing the log exactly as before -- that path is not deprecated.
  Deliberately NOT over HTTP: E3D never reads `CASEBROKER_TOKEN`, and not
  reading it is the guarantee that a misbehaving solver cannot touch the
  campaign.
- Heartbeats no longer write an event row per beat. The worker beats every 5
  minutes for the whole multi-hour solve and reports "alive" until the runner
  has a progress line, so a six-hour case left ~72 identical rows saying nothing
  the one before it did not -- millions across a 30,000-case campaign, kept for
  its lifetime. An identical detail now extends the lease without recording
  anything, via the existing `(case_id, id)` index.
- **`casebroker worker setup`** -- the one command to run ON a new worker box.
  It asks for your broker login, mints a credential for THAT machine, writes it
  into `machine.env` (preserving `WIND_NP` and the runtime paths already there),
  and drops the admin session again, so nothing long-lived is left on a shared
  lab machine. The worker id defaults to the hostname. Previously every machine
  cost an admin a browser round-trip -- sign in, Machines, Issue token, copy it
  out, carry it over -- which does not scale past a handful of boxes and is the
  step people skip, falling back to pasting the shared token everywhere and
  losing the per-machine attribution entirely.
- **A machine token may only lease as its own worker id.** The dashboard has
  always said the token name "must match the machine's CASEBROKER_WORKER_ID" and
  nothing enforced it, so every row in the Machines list and the workers table
  was a claim rather than a fact -- which is exactly the attribution that
  issuing one credential per box exists to provide. `/v1/lease` is the only
  place identity is asserted (heartbeat, complete, fail and release are keyed by
  `lease_id`, and the lease already records its holder), so the check lives
  there. Shared environment tokens are deliberately unaffected: they are shared
  by design, so there is no machine identity for a worker id to contradict, and
  enforcing there would strand the fleet the live campaign runs on.
- The product is the **E3D Simulation Broker** (was "Wind Simulation Broker").
- **Onboarding that a new operator can actually follow.** A complete first-run
  path -- database, admin account, per-machine credential -- and the tooling and
  documentation for each step. `casebroker account create|list|passwd|role|delete`
  manages the human accounts straight against the database, so a headless
  deployment can be bootstrapped and a forgotten password recovered without
  hand-writing an scrypt hash into production; `casebroker init-db` applies the
  schema without starting the service. Over HTTP the same is
  `GET|POST /v1/users`, `POST /v1/users/{username}/{role,password}` and
  `DELETE /v1/users/{username}`. Previously `/v1/auth/setup` was the ONLY way an
  account could come into being, it closed permanently after the first success,
  and nothing could add a second operator or change a password.
- **`role` now authorises something.** The column was stored, returned by every
  auth response, and read by nothing, so every account was an admin whatever its
  row said. `admin` and `viewer` are now enforced: a viewer reads the campaign
  and gets `403` from every mutating endpoint, from `/v1/users` and from
  `/v1/workers/tokens`. That is the attributable, individually revocable version
  of what a shared read-only env token was doing. The last admin cannot be
  deleted or demoted -- there is no recovery endpoint, so that would leave a
  deployment permanently unmanageable.
- **Setup no longer hands the admin account to a stranger.** `/v1/auth/setup`
  cannot require a login -- there is nobody to log in as yet -- and it used to
  be open to ANY anonymous caller whenever no account existed. That is exactly
  the shape production was in: `CASEBROKER_WRITE_TOKENS` set, no account
  created. Verified end to end before the fix -- an anonymous POST created the
  admin and then minted a worker token. Setup now asks whether the deployment
  already holds a credential identifying its operator, and demands it:
  `CASEBROKER_SETUP_TOKEN` if set, else one of `CASEBROKER_WRITE_TOKENS` if any
  are, else nothing (a laptop, or a host behind a firewall). A READ token is
  never enough -- a credential that cannot change the campaign must not create
  the account that can.
- **`CASEBROKER_SETUP_TOKEN`**, optional. `/v1/auth/setup` cannot require a
  login, so on a broker reachable before anyone has set it up, the first
  stranger to find the form became its permanent sole admin. Setting this makes
  setup demand a secret the operator already holds; the dashboard shows a field
  for it when `GET /v1/auth/state` reports `setup_token_required`.
- **A schema that upgrades itself.** Every `CREATE TABLE` is `IF NOT EXISTS`,
  which upgraded cleanly whenever a release added a TABLE -- the only kind of
  schema change this repo had ever made -- and did nothing whatsoever for a new
  COLUMN: the statement no-ops on an existing table without comparing columns,
  so the column never appeared and the first index over it failed at connect
  time with `no such column: priority`, which reads like a corrupt database
  rather than one release behind. The schema is now applied in three passes --
  create tables, `ALTER TABLE ADD COLUMN` whatever is missing, then build the
  indexes -- and records a version in a new `schema_meta` table. It only ever
  adds; a column declared `NOT NULL` with no `DEFAULT` cannot be added to an
  existing table on any engine, so startup refuses with a message naming the
  column instead of failing later and obscurely.
- Login throttling: 10 failures per account per source address in 5 minutes,
  then `429`. The slot is reserved BEFORE the password is checked and released
  on success -- counting the failure afterwards bounds nothing under
  concurrency, since verify_password is ~100 ms of scrypt, so a whole wave of
  simultaneous attempts passes while the count is still zero. Measured at 15
  attempts admitted against a limit of 10 before that change. Expired sessions
  are swept on login rather than accumulating, and the failure map is capped so
  unauthenticated callers cannot grow it without bound.
- compose forwards the DEPRECATED token names again. Dropping them while making
  the canonical ones optional was a fail-OPEN: there is no `env_file:` on the
  broker service, so a variable reaches the container only by being named, and
  every deployment predating the rename has `CASEBROKER_TOKENS` in its `.env`
  because it is the only name the old compose accepted. `docker compose up -d`
  after a pull would have restarted the broker with no tokens at all -- auth
  off, on a public TLS endpoint, with the live workers carrying on as if
  nothing had changed. The old `:?` at least refused to start; the new `:-`
  started wide open.
- `/healthz` no longer answers 500 when the database is unreachable. The account
  lookup added for the new posture ran unguarded in the handler, while every
  other database touch there goes through the cached probe that deliberately
  fails soft. That defeated the one distinction the endpoint exists to draw:
  the Dockerfile HEALTHCHECK reads a 500 as unhealthy and would have
  restart-looped a broker that was itself fine, and `casebroker health` reports
  an HTTP error as "could not reach", misdiagnosing a database outage as an
  unreachable service. The lookup is now folded into the same 30-second probe
  (so it is also no longer a query per unauthenticated request), reports
  `accounts: null` and `auth: "unknown"` when it cannot tell, and the cache is
  dropped the moment an account is created or deleted -- otherwise the first
  run would report "OPEN" for another 30 seconds, which is exactly when the
  instructions tell an operator to check it.
- `casebroker account` and `init-db` find a SQLite `CASEBROKER_DB`. Discovery
  matched only `postgres://`, so the usage the docs give without `--db` refused
  to run against the engine every local deployment uses.
- The column reconciler tolerates losing a race. `_LOCK` serialises one
  process; a rolling redeploy or several uvicorn workers start together, both
  see the column missing, both ALTER, and the loser gets "duplicate column".
  Losing that race is a success -- the column is there -- so it is swallowed
  only when a re-check confirms the column now exists, and re-raised otherwise
  so a genuinely broken migration still fails loudly. Bringing a database
  forward is also logged rather than silent, and `schema_meta` is written only
  when the version actually changed: apply_schema runs on every connection, and
  on a transaction pooler every connection is a new backend, so an
  unconditional upsert made opening a connection a write.
- A viewer's session cookie no longer vetoes a stronger credential on the same
  request. It rides along on every request from that browser, and rejecting on
  sight refused requests that also carried a perfectly good write token; the
  viewer is now checked last, after the machine and environment credentials.
- A non-ASCII credential no longer 500s. `hmac.compare_digest` refuses a
  non-ASCII `str` with TypeError, and the bearer header is attacker-chosen --
  the server decodes it as latin-1, so any byte becomes a character. A single
  `\xe9` in an Authorization header returned 500 from `/healthz`, which needs no
  credential to reach and which the uptime badge polls, and from `/v1/whoami`
  and `/v1/auth/setup`.
- **Accounts for people, per-machine tokens for machines.** First run serves a
  setup form (the one endpoint that cannot require auth, so it closes itself
  after the first account); thereafter password login and a server-side
  session, so the dashboard no longer asks a human to paste the same
  worker-grade secret that can delete the campaign. Machines get one credential
  each, issued from the dashboard and shown once -- the row carries the worker
  id and `last_seen_at`, and revoking one is a row update checked on every
  request, so it takes effect immediately rather than at the next redeploy. A
  machine token deliberately cannot mint machine tokens. The old shared env
  tokens keep working through the transition. New: `GET /v1/auth/state`,
  `POST /v1/auth/{setup,login,logout}`, `GET|POST /v1/workers/tokens`,
  `DELETE /v1/workers/tokens/{name}`.
- `casebroker doctor`: checks the identity tables (`users`, `sessions`,
  `worker_tokens`) and whether an admin account exists, and reports the schema
  version. It previously counted only the five campaign tables, so it reported
  "schema present" against a database with no auth layer at all. It also
  checks the database, the broker and the token in one go
  and names whichever is broken. It DISCOVERS every connection string it can
  find (`$CASEBROKER_DB`, `$DBSTRING`, `.env`, `DBSTRING.md`) and tests each
  rather than trusting the first, because the failure it was written for was
  three copies of the DSN in three files with two of them stale. It also
  translates the one error that actively misleads: Supabase's pooler answers a
  BAD PASSWORD with `password authentication failed for user "postgres"`,
  naming the upstream role instead of the `postgres.<project-ref>` supplied --
  which reads as a username problem, and the "fix" for that produces
  `Tenant or user not found`, which looks like progress and is not. Passwords
  are redacted in the output.
- `DELETE /v1/cases` and `db.purge_cases`: delete cases with their events and
  footprints, so retiring a superseded campaign is not a psql session against
  production. Three interlocks -- `dry_run` defaults to TRUE, `expect` aborts
  untouched when the caller's row count disagrees with the filter's, and cases,
  events and footprints go in one transaction. Optional `recipe`/`state`
  filters, since republishing under a new recipe name is the non-destructive
  alternative and the destructive form must be able to target only the old one.
  The response reports how many doomed rows carried a `result_uri`, because
  deleting a case does not delete the archive a worker already wrote.
- Dashboard: optional desktop notifications when cases finish. A "notify on
  finished cases" toggle asks for permission from its own click (the only
  moment a browser will grant it), then each 60 s refresh diffs the most
  recently finished cases and raises one notification naming them, with the
  true total taken from `/v1/status`. Opt-in, remembered, baselined on enable
  so a finished backlog is never announced, and disabled with the reason shown
  where the API cannot work (no https, no support). No broker change: it rides
  the refresh that already happens, and it works while the tab is open --
  surviving a closed tab would need a service worker and the Push API.
- `POST /v1/lease` accepts `resume_case_ids`: cases this worker holds a local
  checkpoint for are claimed first, and one still leased to the same
  `worker_id` is handed back without spending an attempt. Never honoured for a
  different worker.
- Case rows (`GET /v1/cases`, `GET /v1/cases/{id}`) carry `last_progress` and
  `last_progress_at`, the newest heartbeat `detail`; the dashboard's case
  detail shows it as a Progress row.
- Worker: `--progress-file` (the heartbeat ships the runner's one-line
  progress summary instead of `"alive"`) and `--cases-dir` (where
  `resume.json` markers are looked for). Both are passed to the runner as
  `CASEBROKER_PROGRESS_FILE` / `WIND_CASES`, with `CASEBROKER_WORKER_ID`.
- Runner: `WIND_RUNTIME` = `podman` | `docker` | `native` (blueCFD-Core 2024,
  OpenFOAM-12 + MS-MPI) | `auto`; checkpoint on SIGTERM/SIGINT and resume with
  `startFrom latestTime`; per-outer-iteration progress line
  (`runner/lib/progress.py`); one `<case_id>.tar.gz` per case in `WIND_DONE`
  holding the reconstructed last time step, mesh, dictionaries, logs and
  samples, written atomically.
- `setup_windows.ps1` + `docs/windows-worker.md`: one script that checks a
  Windows box's prerequisites (uv, Git bash, Docker Desktop or blueCFD, e3d,
  real_cities), writes `machine.env`, proves the token has write scope on the
  broker, and with `-Smoke` runs `runner/smoke_spec.json` through the whole
  runner. Rehearsed on a 48-core Docker workstation.
- `start_worker.sh`, `start_worker.ps1`, `machine.env.example`,
  `runner/run_case.cmd` (Windows launcher), `scripts/pull_done.sh` (master
  pulls archives from PACE over SSH), `docs/fleet.md`.

### Changed
- **The worker exits when its credential is refused.** A `401` or `403` on
  `/v1/lease` was caught by the same handler as a network blip and retried
  every `--idle-backoff` seconds -- forever. On a workstation that is a
  warning scrolling past; on Phoenix it is a twenty-task array holding twenty
  allocations for their whole walltime with nothing but `[warn] lease failed:
  403` in the logs, because a credential problem does not fix itself by
  waiting. The worker now prints the broker's reason and exits `2`, so the
  scheduler frees the nodes and the sbatch log ends with the sentence that
  explains it. Every other failure retries exactly as before.
- **The dashboard wears the RhinoPackages design language.** Its neutrals
  replace the GitHub Primer ones wholesale -- Tailwind gray 50/100/200/500/600/
  900 on white in the light theme, and in the dark the four zinc values that
  project's `tailwind.config.ts` overrides Tailwind's own zinc with (950
  `#0d1117`, 900 `#161b22`, 800 `#30363d`, 700 `#484f58`), which is a full step
  harder than the "Dark Dimmed" it replaces. Its brand pink arrives as a
  separate `--brand` family wired ONLY to identity and interaction: the logo
  mark's gradient, the primary button, the focus ring. It is deliberately not
  wired to `--accent`/`--primary`/`--warn`/`--bad`, because those four ARE the
  campaign's state scale -- they read green / blue / amber / red across the stat
  cards, the progress bar and every badge, and recolouring a "done" case to
  brand pink would make the dashboard prettier and unreadable at a glance.
  Every pairing was measured rather than eyeballed: brand-600 is the source's
  text colour but lands at 4.40:1 on this page's gray-50 canvas and 3.91:1 on
  its own brand-100 chip, so light uses brand-700, and dark lifts muted text a
  step to zinc-300/400 because zinc-500 on zinc-900 is 3.58:1. One deliberate
  deviation: `--border` stays at zinc-700 (2.09:1), faithful to the source, for
  dividers that carry no identification duty, while `--btn-border` is lifted to
  zinc-500 (3.58:1) so the edges of interactive controls clear WCAG 1.4.11.
- **The KPI row fits on one line by default.** The rule said eight columns; the
  row renders seven (total, done, leased, pending, quarantined, remaining, and
  the SLURM queue), so there was a permanently empty column making every card
  narrower than it needed to be, and the breakpoint that applied it sat at
  1480px -- above the 1280 and 1366 laptops most of this is read on, which left
  the set wrapping into two rows on the machines where reading the campaign's
  state as a single set matters most. Seven columns now, from 1240px up.
- **`casebroker doctor` now looks in sibling checkouts** (`../*/.env`), not
  only its own working directory. The credential that was actually live sat
  in a neighbouring repo, so doctor reported "no connection string found"
  while a working one lay a directory over -- the worst possible answer for a
  command whose job is to end exactly that confusion. Bounded to one level
  and to files named `.env`; each string is still reported once even when two
  paths reach the same file.
- **A finished case is no longer lost to a broker restart.** `/v1/complete` is
  the one call whose failure throws away real work -- the case is solved and
  the archive is on disk, and only the broker has not been told, so giving up
  means the lease expires and hours of CFD are recomputed elsewhere. Its retry
  budget was ~30 s, shorter than a platform redeploy, which made rotating the
  database credential quietly cost whichever case happened to finish during the
  restart. Now about four minutes. A `409` is still never retried.
- **Documented how to rotate the database password.** The DSN lives in four
  places (Supabase, Render, the `DBSTRING` Actions secret, and a workstation
  file) and missing the third turns CI red on a commit that is fine. See
  "Rotating the database password" in `docs/operations.md`.
- Runner: the inlet roughness is per wind direction. `build-case` gets
  `wind.roughnessByDirection` from the geometry report's `z0_by_direction`
  (real_cities `upstream_z0.py`: ESA WorldCover, log-mean z0 over a 3 km
  upwind sector beyond the domain edge, per direction); `roughness` 0.5 stays
  the fallback for a site without a WorldCover tile. Needs an `e3d` build
  with `WindConfig.RoughnessByDirection`; older builds ignore the key.
- Runner: porous tree crowns. The geometry step writes a crown-volume STL and
  a vegetation class (real_cities `canopy_zones.py`, Meta/WRI 1 m canopy
  height model, LAD/Cd from Eddy3D's vegetation library by latitude band);
  `build-case` gets `geometry.canopyStl` + `vegetation`, and the runner runs
  `topoSet` after meshing so the `canopy` cellZone carries the
  `porosityForce` sink (`f = 2·Cd·LAD`) into every direction case.
- Runner: optional Syncthing hand-off -- with `WIND_SYNCTHING_APIKEY` and
  `WIND_SYNCTHING_FOLDER` set, one `/rest/db/scan` for the new archive after
  it is renamed into place, so a folder with its watcher and rescans off
  moves nothing but finished cases.
- `result_uri` now points at the case archive rather than a `results/`
  directory; `result_bytes`/`result_sha256` describe the archive.
- The runner's default solver `writeInterval` is 200, not the whole iteration
  budget -- the old value wrote once at the very end, so a walltime-cut solve
  had nothing to resume from.
- `slurm/*_worker.sbatch` default `EDDY3D_CLI` to `$WC/bin/e3d` (the CLI's new
  name; a fresh `linux-x64` build was deployed to both clusters) and pass
  `--cases-dir`; ICE keeps checkpoints and archives on ice1 scratch, not the
  30 GB home.

### Fixed
- **Semantic versioning that nothing enforced had stopped happening.** Between
  `v0.2.0` and `v0.3.0` four pull requests merged, the version moved once, and
  nothing tagged it: the README's version badge read `v0.2.0` while the running
  service's `/healthz` read `0.3.0`. Every version test passed the entire time,
  because all seven of them compare the three *copies* of the number to each
  other — and those agreed perfectly. The guard was on the wrong axis: internal
  consistency, never release consistency. Merging a version bump now cuts the
  tag and publishes the release itself, after running the suite and checking
  `CHANGELOG.md` has a heading for that version; a merge that does not move the
  version is a no-op. Tagging by hand is unchanged, mismatch check included.
  The tag and the release are created in **one job** deliberately: a tag pushed
  with `GITHUB_TOKEN` does not start another workflow run, so splitting them
  would create tags that never became releases — the same bug, one layer
  quieter. `tests/test_version.py` gains the check that was missing, that the
  newest `CHANGELOG.md` release heading is the version the package declares.
- **A revoked machine could never be given a new credential.** Revoke-then-
  reissue is the documented recovery for a box that has lost its token --
  `casebroker worker setup --rotate`, and Revoke then Issue in the dashboard --
  and both answered `409`. `revoke_worker_token` MARKS the row rather than
  deleting it, so that `last_seen_at` and who issued it survive a revocation,
  and `UNIQUE(name)` then refused the re-issue as well. The 409 even read
  "revoke it first", advice that could not succeed. It matters more since a
  machine token may only lease as its own worker id: a box that lost its
  credential could not get a working one back under the id it runs as. A
  revoked row is now reclaimed in place, with `last_seen_at` cleared -- a fresh
  credential has not been seen, and inheriting the old one's timestamp would
  show a machine as alive on the strength of a token that no longer works. A
  LIVE credential is still never replaced silently, which is the stranding
  hazard the constraint exists for.
- **A stale `DBSTRING` no longer fails the build it says nothing about.** The
  production-database job turned `main` red whenever that secret drifted -- on
  a branch that gates deploys, which is how people learn to ignore a red X. It
  was also inconsistent: with the secret ABSENT the same job went green having
  tested exactly as much, because the module's `skipif` fires, so the line sat
  at "is a secret set" rather than at "did this coverage run". An unusable
  credential now skips the module. What it must never be is silent, and the
  first attempt was: pytest captures stdout inside a fixture and discards it
  for a skip, so a printed `::warning::` reached nobody (measured, not
  assumed). The reason now travels two ways that survive -- pytest's short
  summary, for which the workflow passes `-rs`, and `$GITHUB_STEP_SUMMARY`,
  which is a file and renders on the run's own page.
  `CASEBROKER_TEST_PG_REQUIRED=1` turns it back into a hard failure.
- The main-only Postgres CI job no longer amplifies a bad credential into a
  pooler outage. With the `DBSTRING` secret stale, sixteen tests each opened
  their own connection, psycopg tried three pooler addresses per attempt, and
  `fresh_conn`'s retry-on-`ECIRCUITBREAKER` backoff -- written for a transient
  burst -- turned each later refusal into five more: close to five minutes of
  failed logins per push to main. Supabase answers that by blocking NEW
  connections project-wide, which is what the live broker and every
  reconnecting worker need, and `deploy-smoke-test` triggers a Render deploy
  inside that very window. One pre-flight connection now runs before any test;
  if it is refused, the run stops there with the reason (a stale secret is
  named as such) after a single failed login. The throwaway-container job is
  unaffected: its connection succeeds and nothing else changes.
- **`/healthz` and `/v1/whoami` no longer report a secured broker as OPEN.**
  Both judged the auth posture from the environment token buckets ALONE, never
  asking whether an account existed -- so a deployment secured entirely by
  accounts (the whole from-scratch path) reported `"auth": "OPEN"`, and
  `whoami` returned `scope: write` for any string whatsoever. Both documented
  deploy gates therefore said the exact opposite of the truth: `casebroker
  health` exited non-zero on a correctly locked-down broker. `/healthz` now
  reports a third posture, `accounts`.
- **`whoami` recognises per-machine tokens and sessions**, which it never did.
  This broke the documented worker onboarding end to end: `bootstrap_worker.ps1`
  reaches `setup_windows.ps1`, which runs `casebroker token check --expect
  write`, which asks `whoami` -- and a dashboard-issued machine token, the
  credential the docs tell you to use, came back `scope: none`, so the script
  aborted. `token check` now also names which kind of credential answered.
- **`docker compose up -d` after `cp .env.example .env` failed immediately.**
  `compose.yaml` hard-required the DEPRECATED `CASEBROKER_TOKENS` while
  `.env.example` defined `CASEBROKER_WRITE_TOKENS`, so the documented sequence
  died before the broker started. No token is required to start at all now --
  the broker is secured by an account.
- **Documentation caught up with the accounts work.** `docs/dashboard.md` still
  told operators to "paste in the broker's URL and a write token value", which
  had not been true since the dashboard grew a login; `docs/operations.md` never
  mentioned accounts and its deploy checklist stated an auth rule the code no
  longer followed; `docs/protocol.md` listed none of the identity endpoints;
  `README.md`'s quickstart still generated a shared token inside a command
  substitution the operator never saw. There is now a "First run" section that
  goes from an empty database to a logged-in admin issuing machine credentials.
- `/healthz` no longer hands the DSN summary to anonymous callers. Masking the
  password was necessary but not sufficient -- what remained still named the
  exact database instance, its host, port and username, on an endpoint that by
  design needs no credential. That is reconnaissance for free, and it narrows an
  attack from "find their database" to "guess the password for this known
  tenant". `ok`, `version` and `db_ok` stay public (a badge needs to tell
  "service down" from "database down"); the `db` field is present-but-null
  unless the caller is authenticated.
- **The native blueCFD runtime never worked, and failed quietly.** `FOAM_MPI`
  was hardcoded to `MS-MPI-10.1` while the installed directory is
  `MS-MPI-10.1.2`, so blueCFD's MPI `libPstream.dll` was never on PATH and only
  `lib/dummy` was -- every rank loaded the SERIAL library. snappyHexMesh then
  "succeeded" with each rank printing "This dummy library cannot be used in
  parallel mode", mpiexec exited 1, and the run continued on the unrefined
  blockMesh background: 39,325 cells where 177,214 were expected. `FOAM_MPI` is
  discovered from disk now, and `bluecfd_env` refuses rather than proceeding if
  no MPI `libPstream.dll` is found.
- The inlet-flux gate only matched `sum(inlet)`; blueCFD prints `sum("inlet")`
  WITH QUOTES, so every native case reported `NONE` and then failed the
  non-negative-flux check -- which is fatal and non-retryable, so it would have
  quarantined every case on a blueCFD machine. Both spellings match now.
  With these two fixed, native and Docker agree exactly on the same case:
  177,214 cells, canopy zone 6,063 vs 6,064, inlet flux -15,280,149 both.
- **The pedestrian sample was not at pedestrian height.** The runner sliced one
  horizontal plane at `terrain_zmax + 1.5`; on a site with relief that is not
  "1.5 m above grade" anywhere but the summit. Measured on the campaign's own
  Nanjing tile (terrain -24.4 .. 38.5 m): the plane sat a MEDIAN 47.6 m above
  the ground and came within 10 m of it over 0.2% of the surface, so every
  pedestrian label on a site with terrain was free-stream flow tens of metres
  up. It is now a `distanceSurface` draped at `WIND_PEDESTRIAN_H` (default
  1.5 m) over the terrain triSurface, `signed false` because that STL is an open
  sheet. Verified end to end: the sample spans 62.6 m of z on a tile with
  62.8 m of relief.

  This one hid behind a plausible-looking render, and it also made the mesh look
  settled: up in the free stream two independently converged meshes agree to
  1.6%, while on the real pedestrian surface they differ by 24% in the mean and
  by more than 10% at 84% of points. No mesh should be called converged for
  training data on the old measurement.
- `start_worker.ps1 --max-cases 1` bound `--max-cases` to `-EnvFile` (PowerShell
  positional binding), so the profile was never read and the worker died with
  `set CASEBROKER_URL`; extra worker arguments are positional now and `-EnvFile`
  is named-only.
- Solve gate: the nan/inf scan matched the `sigFpe : Enabling floating point
  exception trapping` banner every OpenFOAM log opens with, and then matched
  `iNf` in the raw bytes of `format binary` field files -- both failed clean,
  converged solves. Only ascii fields are text-scanned now; binary fields are
  judged by `nan` in the solver's residual lines. The gate names the file and
  line it matched.
- Runner: the pedestrian-plane slice sample (and the preview PNG built from it)
  had never run. The `sliceFO` dictionary was written in an older syntax:
  no `FoamFile` header, a bare `libs (sampling)`, `surfaces` as a dictionary
  rather than a list, and no named function-object wrapper (OpenFOAM 12's
  `foamPostProcess -dict` reads a `functions` list). Now the exact form of the
  image's own `movingCone/system/cutPlane` tutorial, and the renderer reads
  the raw writer's `postProcessing/<fo>/<time>/<surface>.xy` layout.

## [0.2.0] - 2026-09-08

First tagged release. 0.1.0 was never cut, so everything below shipped under a
version that never moved: sixty commits, a dozen new endpoints, and `/healthz`
reporting `0.1.0` throughout. MINOR rather than MAJOR because every addition is
backwards compatible -- no endpoint changed shape, no field was removed, and the
deprecated `CASEBROKER_TOKENS` spelling still works.

New endpoints in this release: `GET /v1/whoami`, `GET /v1/share-token`,
`POST /v1/fleet`, `GET /v1/cases/{id}/footprints`. `/v1/status` gained
`version`, `db` and `fleet`; `/healthz` gained `version` and `scopes`.

### Added

- `/healthz` now reports `version`, so "is the commit I just pushed actually the
  one serving traffic?" is answerable from an unauthenticated endpoint rather
  than inferred from a deploy's own status field.
- `casebroker.__version__`, read from the installed distribution's metadata, is
  the single source the OpenAPI document, `/healthz` and the dashboard badge all
  report from.
- `tests/test_version.py` — asserts every surface agrees, and fails if a version
  literal is ever pasted back into a source file.
- `CHANGELOG.md` (this file).
- `.github/workflows/release.yml` — pushing a `v*.*.*` tag publishes a GitHub
  Release, refusing to do so if the tag disagrees with `pyproject.toml`.

- `CASEBROKER_WRITE_TOKENS` / `CASEBROKER_READ_TOKENS` — token variables named
  for the capability they grant. The old `CASEBROKER_TOKENS` never said anywhere
  that it was read **and** write, while its partner was explicitly `READONLY`, so
  the only way to learn the relationship was to read `app.py`. Both old spellings
  still work; setting a variable and its deprecated twin to *different* values is
  refused at startup rather than resolved by precedence.
- `GET /v1/whoami` — reports what the presented token can do (`write` / `read` /
  `none`). Unauthenticated and always `200`, so "this credential is wrong" stays
  distinguishable from "the broker is unreachable". Previously the only way to
  discover a token's scope was to attempt a mutating call against production.
- `/healthz` now reports `scopes` — the *count* of configured tokens per
  capability, never values — so "did my rotation actually land?" is answerable.
- The `casebroker` command is back, and now exists: `casebroker token new`,
  `casebroker token check --broker … --token …` (non-zero exit on `--expect`
  mismatch, so it works as a deploy gate) and `casebroker health --broker …`
  (non-zero exit when auth is OFF). Dependency-free, so it runs on a login node.

### Changed

- The Docker image no longer defaults `CASEBROKER_DB` to `/data/campaign.sqlite`
  with a `VOLUME`. That is correct on a VM and wrong on Render, which has no
  persistent disk: the campaign was written to a container filesystem discarded
  on every deploy, and nothing reported it — the service came back up healthy and
  empty. Production points at Supabase Postgres; `app.py` now warns on startup
  whenever `CASEBROKER_DB` resolves to SQLite. Docs name Supabase and the `6543`
  transaction-pooler port, which is why `prepare_threshold=None` is needed.

### Fixed

- The dashboard's `color-scheme` is now bound to the selected theme instead of
  the OS preference. Dark is the default theme, so on a machine set to light
  mode the browser was painting native scrollbars, `<select>`/`<input>`
  internals and the token field's autofill highlight in **light** on a dark
  page.
- The header brand no longer links to a hardcoded
  `https://casebroker.onrender.com/`. It is now `href="/"`, so on a local or
  self-hosted instance the logo goes to that instance's own dashboard rather
  than navigating away to production.
- The `casebroker` console script declared in `[project.scripts]` pointed at
  `casebroker.cli:main`, a module that has never existed — the command
  installed and then failed with `ModuleNotFoundError` on every invocation.
  Removed; the service runs as `uvicorn casebroker.app:app` and the worker as
  `python -m casebroker.worker`, neither of which used it.
- The theme toggle's tooltip described the current theme in one state and the
  resulting action in the other; both now describe what clicking does.

### Changed

- The GitHub Dark Dimmed palette is defined once. It had been duplicated between
  `[data-theme="dark"]` and a `@media (prefers-color-scheme: dark)` block — 26
  tokens and three component overrides restated verbatim. Because `data-theme`
  is always set explicitly, the media-query copy could never apply a *different*
  value, only silently disagree with the block that does. The remaining
  definition is annotated with the Primer token each value corresponds to.
- The dashboard header badge shows the running broker's version alongside the
  storage engine and auth mode. It previously shipped the literal string
  `v0.1.0`, which was a fourth hardcoded copy of the version and was overwritten
  by the health check on connect anyway.

## [0.1.0]

Initial version: the case database, the HTTP lease protocol, both storage
engines (SQLite and Postgres), the worker client, the runner seam and the
dashboard. No git tag was cut for this release.
