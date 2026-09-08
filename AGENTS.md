# Working in this repo

Read [`DOMAIN.md`](DOMAIN.md) first — it defines the nouns. This file is about
how to change things without breaking the campaign.

## The shape of it

A lease-based work queue: one Postgres database of CFD cases, many machines
pulling from it. `casebroker/` is the service, `runner/run_case.sh` is the seam
to the CFD, `slurm/` submits workers. The sampler and geometry pipeline live in
the **parent repo** (`benchmark/real_cities/`), which vendors this one as a
submodule.

Production is Supabase Postgres behind Render. **SQLite is the test engine, not a
smaller production option** — it needs no network and no credentials, which is
why 103 tests run in seconds with zero external dependencies.

## Before you change anything

- **`uv run --with pytest --with httpx python -m pytest tests/ --ignore=tests/test_db_postgres.py -q`**
  is the fast suite. It must stay green and needs nothing external.
- `tests/test_db_postgres.py` runs against a real Postgres when
  `CASEBROKER_TEST_PG_DSN` is set; skipped otherwise.
- Don't hardcode the version anywhere. `tests/test_version.py` fails if you do,
  including a `v`-prefixed literal.

## Things that have actually gone wrong here

Each of these cost real time or real data. They are not hypotheticals.

**Exit 64 is not a general failure code.** It means *this site is broken
forever*. A missing file, a dead host, an unavailable DTM are **retryable**.
Treating a missing STL as fatal quarantined 173 good sites in about a minute.
When in doubt, retryable: it costs three attempts, where a wrong quarantine costs
a hand-edit of the database.

**Never release a lease a live worker holds.** Doing it kills an in-flight solve
that may be hours in. Check `lease_worker` and whether that worker is still
heartbeating before touching a `leased` row.

**A batch shell is not a login shell.** SLURM scripts don't source your profile,
so `~/.local/bin` is not on `PATH` and `uv` is not found. Both sbatch scripts
export it explicitly and check.

**`$PY` in `run_case.sh` is a bare interpreter** for parsing the spec. Anything
needing numpy/rasterio/trimesh must go through
`uv run --project "$REAL_CITIES"`, or it dies with `ModuleNotFoundError` on
every case identically.

**Check which branch and which commit a cluster is on.** A Phoenix job queued
hours earlier started running old code and quarantined ~900 cases. Clusters
check out `v2-dataset-extension` in the parent and `main` in this submodule.

**The dashboard's data path is `/v1/status`, not `/healthz`.** A browser that
could reach one but not the other connected fine and then couldn't name what it
had connected to. Both carry `version` and `db` now; prefer `status`.

**Watch for silently overwritten dict keys.** `tile_lod()` returns
`height_provenance` and `frac_measured_height` and is spread *after* the caller's
own keys, so identically named ones vanished and the report showed pre-inference
numbers for post-inference geometry.

**Delete `__pycache__` after editing modules in `real_cities/`.** A stale
bytecode cache made a fixed function look broken for several rounds.

## Conventions

- **Commits explain *why*, and name the failure that motivated the change.** The
  history here is the only record of a lot of hard-won detail; a message that
  says "fix bug" throws it away.
- Comments carry the reasoning, not the mechanics. If a constant was chosen
  because of a measurement, record the measurement.
- Dashboard is one HTML file, no build step, no framework. Keep it that way.
- Shell scripts are LF (`.gitattributes` enforces it); a CRLF in a script breaks
  on the cluster with `bad interpreter: /bin/bash^M`.
- `.env` is gitignored. This repo is public and `.env.example` tells you to copy
  it — an untracked `.env` with a live write token is one `git add -A` from
  publication.

## Deploying

Push to `main` → CI runs → the deploy job triggers a Render deploy of **that
commit** and polls **that deploy id**. It never trusts "the latest deploy is
live" as a proxy for "my commit is live" — that mistake let three pushes go
undeployed while reporting green.

The Postgres CI job gates deploys. If Supabase trips its circuit breaker
(`ECIRCUITBREAKER — too many authentication failures`, usually from too many
connections in a short window), that job fails and nothing deploys even though
the code is fine.

Releases: bump `version` in `pyproject.toml`, move `CHANGELOG.md`'s `Unreleased`
under the new number, tag `vX.Y.Z`. `release.yml` refuses to publish if the tag
disagrees with `pyproject.toml`. **MAJOR** is a breaking change to the
worker-facing protocol — workers are long-lived and can be mid-lease for an hour,
so a broker that stops speaking the old protocol strands them.

## Open problems

- **Overture coverage.** Only ~45% of sampled sites return buildings at all, and
  LCZ 1 — the scarcest and most valuable class — is ~25%. Coverage is thin across
  China and much of Africa. Sites that *do* pass may still have most heights
  inferred rather than measured.
- **The published campaign predates the polar gate.** Production holds the
  ungated draw, including sites in Antarctica and one in the open Pacific.
- **No land/water mask.** The polar gate catches poles, not oceans.
