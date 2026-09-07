# Changelog

This project follows [Semantic Versioning](https://semver.org). The version is
declared **once**, in `pyproject.toml`; see "Versioning" in the README for what
each component means here and how to cut a release.

The format follows [Keep a Changelog](https://keepachangelog.com).

## [Unreleased]

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
