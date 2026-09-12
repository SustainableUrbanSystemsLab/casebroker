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
- `start_worker.sh`, `start_worker.ps1`, `machine.env.example`,
  `runner/run_case.cmd` (Windows launcher), `scripts/pull_done.sh` (master
  pulls archives from PACE over SSH), `docs/fleet.md`.

### Changed
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
