# Operating the broker

Tokens, storage, releases and deployment.

## Tokens

Two buckets, **named for what they grant** rather than for what they are:

| Variable | Grants | Carried by |
| --- | --- | --- |
| `CASEBROKER_WRITE_TOKENS` | read **and** write — lease, heartbeat, complete, fail, release, add cases | workers, and `site_sampler.py publish` |
| `CASEBROKER_READ_TOKENS` | read only — 401 from every mutating endpoint | a dashboard link you hand out |

A write token also reads, so you never need both to operate. Each is a
comma-separated list, which is how you **rotate without stranding a worker
mid-lease**: add the new token, move the workers over, then drop the old one.

```bash
uv run casebroker token new                       # generate one, with a hint on where to put it
uv run casebroker token check --broker URL --token T   # what can this token actually do?
uv run casebroker health  --broker URL            # version + auth posture of a deployment
```

`token check` exits non-zero when `--expect` disagrees with the answer, so it
works as a deploy gate. `health` exits non-zero when auth is OFF.

`GET /v1/whoami` is what those calls use, and it is deliberately unauthenticated
and always `200`: it answers *about* a credential rather than gating on one, and
returning `{"scope": "none"}` instead of `401` is what makes "this token is wrong"
distinguishable from "the broker is unreachable". Previously the only way to
discover a token's capability was to attempt a write against production and see
whether it failed.

The older names — `CASEBROKER_TOKENS` and `CASEBROKER_READONLY_TOKENS` — still
work. Setting a variable *and* its deprecated twin to **different** values is
refused at startup rather than resolved by precedence: the quiet failure there is
a token you believe you revoked continuing to work.

## Storage: Supabase in production, SQLite for tests

`CASEBROKER_DB` decides the engine purely by its shape — a file path opens
SQLite, a `postgres://` or `postgresql://` DSN opens Postgres — and nothing
above `db.py` needs to know which one it got:

```bash
CASEBROKER_DB=campaign.sqlite                       # local dev and the test suite
CASEBROKER_DB=postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres
```

**The live campaign runs on Supabase Postgres.** SQLite is not a smaller
production option, it is the *test* engine: it needs no network and no
credentials, which is why the 97 tests that exercise lease semantics run in
milliseconds with zero external dependencies. A managed database survives a
service restart or redeploy where a container's filesystem does not, and with
state external the service itself is **stateless and disk-free** — the cheapest
compute tier is enough.

**Supabase gives you two ports, and the difference matters here.** `6543` is the
*transaction* pooler and is what this deploys against; `5432` is a direct
connection. Transaction pooling is exactly the mode that breaks server-side
prepared statements — see the pooler note below, which is already handled.

The image ships **no default `CASEBROKER_DB`** on purpose. It used to default to
`/data/campaign.sqlite` with a `VOLUME`, which is right on a VM and wrong on
Render, where there is no persistent disk: the campaign was written to a
filesystem discarded on every deploy, and nothing said so — the service came
back up healthy and simply empty. `app.py` now prints a startup warning whenever
`CASEBROKER_DB` resolves to SQLite.

The two engines share every function name and diverge only where it would be
actively wrong to pretend they could not: **claiming work under concurrency**.
SQLite's `BEGIN IMMEDIATE` takes a whole-database lock; Postgres uses
`SELECT ... FOR UPDATE SKIP LOCKED`, a per-row lock that lets independent
connections claim different rows without blocking each other. `test_db_postgres.py`
verifies this against a real Postgres instance — set `CASEBROKER_TEST_PG_DSN` to
run it; it is skipped otherwise. It also isolates the exact locking clause with
two hand-driven transactions (bypassing `db.py`'s in-process lock entirely,
which a naive many-threads-in-one-process test cannot do — see that file's
docstrings for why the first attempt at this test could not have caught a
regression no matter how many threads it used).

**If you point `CASEBROKER_DB` at a connection POOLER** (Supabase, PgBouncer,
RDS Proxy) **in transaction-pooling mode**: `_connect_postgres` already passes
`prepare_threshold=None` to disable psycopg's automatic server-side prepared
statements. Without it, a transactional pooler can route consecutive
transactions to different backend connections, and a prepared-statement name
collides across sessions — measured as `DuplicatePreparedStatement` errors
starting on roughly the sixth call to the same query text.

## Versioning

[Semantic versioning](https://semver.org), declared **once** in
`pyproject.toml`. What the components mean for this service specifically:

- **MAJOR** — a breaking change to the worker-facing protocol in the table
  above. Workers are long-lived and deployed across machines nobody is going to
  restart in a hurry (a Phoenix `embers` pool can be mid-lease for an hour), so
  a broker that stops speaking the old protocol strands them.
- **MINOR** — a backwards-compatible addition: a new endpoint, a new optional
  field, a new dashboard feature.
- **PATCH** — a fix that changes no shape anybody can observe.

Nothing else in the tree hardcodes the number. `casebroker/__init__.py` reads it
back out of the installed distribution's metadata, and everything that reports a
version — the OpenAPI document, `/healthz`, the dashboard's header badge — goes
through `casebroker.__version__`. `tests/test_version.py` asserts they all agree
and **fails if a version literal is ever pasted into a source file again**, which
is how the three copies that used to exist got out of step in the first place.

Cutting a release is therefore: bump `version` in `pyproject.toml`, move the
`Unreleased` entries in `CHANGELOG.md` under the new number, commit, and tag it:

```bash
git tag -a v0.2.0 -m "v0.2.0" && git push origin v0.2.0
```

Pushing that tag is what publishes the release: `.github/workflows/release.yml`
fires on any `v*.*.*` tag, **refuses to publish if the tag disagrees with
`pyproject.toml`**, and creates the GitHub Release. So the tag cannot drift from
the declared version any more than the code can.

`/healthz` reporting `version` is what makes a deploy checkable from outside:

```bash
curl -s https://casebroker.example.org/healthz | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])"
```

## Deploying

`Dockerfile` and `compose.yaml` are here for a cloud VM; either works equally
for a platform that builds from a Dockerfile directly (Render, Fly, Railway).
Three things are **not** done and must be before this faces the internet:

1. **Put it behind TLS.** The token is a bearer credential in a header; over
   plain HTTP it is readable by anything on the path. Terminate TLS at Caddy or
   nginx, or use a platform that terminates it for you (Render, Fly).
2. **Set `CASEBROKER_WRITE_TOKENS`.** With none set, auth is *off* — `GET /healthz`
   reports `"auth": "OPEN"` so this is visible rather than silent, but nothing
   stops it. Optionally also set `CASEBROKER_READ_TOKENS` to a *different*
   value if you want a link you can hand out for viewing only — see
   "Sharing a read-only view" above.
3. **Point `CASEBROKER_DB` at the Supabase DSN** (port `6543`, the transaction
   pooler). A container's local filesystem does not survive a redeploy; a
   managed database does. With state external, the service needs no disk at all
   — the cheapest compute tier a platform offers is enough. If this is left
   unset, or set to a file path, the service starts anyway and logs a warning:
   it will look healthy right up until a redeploy silently empties it.

**`/healthz` is intentionally unauthenticated** (so infrastructure health checks
work with no token) **and therefore must never return anything that could be a
credential.** It reports a redacted form of `CASEBROKER_DB` — the DSN with its
password masked (`_redact_db_target` in `app.py`), never the raw value. This was
not a hypothetical: an early version of this service returned the connection
string verbatim, which meant hitting `/healthz` against a real deployment
printed the live database password in plain text with no auth required to
trigger it. If you ever add something else to this endpoint, check it for the
same shape of mistake before shipping it.
