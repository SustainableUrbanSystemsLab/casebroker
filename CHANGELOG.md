# Changelog

This project follows [Semantic Versioning](https://semver.org). The version is
declared **once**, in `pyproject.toml`; see "Versioning" in the README for what
each component means here and how to cut a release.

The format follows [Keep a Changelog](https://keepachangelog.com).

## [Unreleased]

Hooking heterogeneous machines into the campaign: Windows workstations (Docker
or native blueCFD-Core) alongside the PACE clusters, with progress visible on
the dashboard, stopped solves resuming on the same machine, and one archive per
finished case for Syncthing / a master-side pull to collect. See
`docs/fleet.md`. MINOR: every protocol change is an optional addition.

### Added
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
