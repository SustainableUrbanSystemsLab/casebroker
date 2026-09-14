# Operating the broker

First run, accounts, tokens, storage, releases and deployment.

## First run: from nothing to a working broker

Three things have to exist before a worker can lease a case: a database, an
admin account, and a credential for each machine. Only the middle one needs a
decision from you.

**1. The database creates itself.** Point `CASEBROKER_DB` at a Postgres DSN (or
leave it at a SQLite path for a laptop) and start the service. Every table and
index is created on the first connection, and a database from an earlier release
is brought forward at the same moment — see
[The schema creates itself](#the-schema-creates-itself) below. There is no
migration command to run and no SQL to paste. To do it without starting the
service — to prove a new database and its credentials work before a deploy
depends on them:

```bash
uv run casebroker init-db --db "$CASEBROKER_DB"
```

**2. Create the first admin.** Open the broker's URL in a browser. With no
account yet, the panel offers **Set up this broker** rather than a login: pick a
username and a password of at least 12 characters, and the account you create
becomes the admin. That form then closes permanently — a second caller gets
`409`, not another admin.

Headless, or recovering a deployment nobody can log into:

```bash
uv run casebroker account create --username ada --role admin
```

It prompts for the password (twice, never as a command-line flag, which would
land it in shell history and `ps`). It talks to the database directly, so it
works when the service is down.

> **Who is allowed to claim that first account.** `POST /v1/auth/setup` cannot
> require a login — there is nobody to log in as yet — so it instead asks
> whether this deployment already holds a credential that identifies its
> operator, and demands one if so:
>
> | The deployment has | Setup requires |
> | --- | --- |
> | `CASEBROKER_SETUP_TOKEN` | that token |
> | `CASEBROKER_WRITE_TOKENS` (the shape production was already in) | one of those write tokens |
> | neither — a laptop, or a host behind a firewall | nothing; it is open |
>
> Present it as `Authorization: Bearer <value>` or as `setup_token` in the body;
> the dashboard shows a field for it whenever `GET /v1/auth/state` reports
> `setup_token_required`. A **read** token is deliberately not accepted: a
> credential that cannot change the campaign must not create the account that
> can. Generate a setup token with `casebroker token new --quiet`.
>
> Set `CASEBROKER_SETUP_TOKEN` before a broker with no tokens at all becomes
> reachable, or the first stranger to find the form becomes its permanent admin.

**3. Issue one credential per machine.** Signed in, go to **Machines** ▸ *Issue
token*, naming it after the worker id that box will run under. The token is
shown **once** — only its hash is stored — along with the exact
`bootstrap_worker.ps1` line to paste on that machine. Revoking one is a button,
takes effect on that machine's next request, and leaves every other machine
running.

Then confirm from outside:

```bash
uv run casebroker health --broker https://broker.example.org
# auth: accounts   <- not OPEN. If it says OPEN, step 2 has not happened.
```

## Accounts

Two roles, and the difference is real rather than cosmetic:

| Role | Can |
| --- | --- |
| `admin` | everything: lease and complete cases, add and purge them, create accounts, issue and revoke machine credentials |
| `viewer` | read the campaign — status, case list, one case. `403` from every mutating endpoint, from `/v1/workers/tokens`, and from every `/v1/users` route **except changing their own password** |

`viewer` is what "let someone watch the campaign" should have meant all along:
a read-only *env token* did the same job with a shared secret that nobody could
attribute to a person or revoke individually.

```bash
uv run casebroker account list                                  # who exists, and last login
uv run casebroker account create --username bob --role viewer
uv run casebroker account passwd --username ada                 # forgot it -- no old password needed
uv run casebroker account role   --username bob --role admin
uv run casebroker account delete --username bob
```

The same operations exist over HTTP for an admin session: `GET`/`POST /v1/users`,
`POST /v1/users/{username}/role`, `POST /v1/users/{username}/password`,
`DELETE /v1/users/{username}`.

Changing a password **revokes every session that account holds**. That is the
point rather than a side effect: the reason to change one in a hurry is that
someone else may have it, and a fortnight-long session left alive would make the
change cosmetic. Changing your *own* requires the current one — a session cookie
lifted from a logged-in laptop should not be enough to lock the owner out — and
the endpoint re-issues yours, so you stay signed in where you did it.

**You cannot delete or demote the last admin.** There is no password-reset email
and no recovery endpoint, so that operation would leave the deployment
permanently unmanageable with the database the only way back in. Both the API
and the CLI refuse it.

Failed logins are throttled per account and source address (10 in 5 minutes).
The counter lives in the process, so it resets on a redeploy: it exists to make
online guessing impractical against a handful of lab accounts, not to survive a
distributed attack. scrypt already makes each attempt cost about 100 ms.

## Tokens

Accounts cover humans. Machines cannot type a password, so they carry a token —
and there are two kinds, one of which is the older model.

**Per-machine tokens** are the current one: issued from the dashboard, stored
hashed, one row per box. They carry the machine's name, so the dashboard can say
which box last used a credential and when, and revoking one is a row update that
takes effect on the next request rather than an environment-variable edit plus a
redeploy.

**Shared environment tokens** predate accounts and still work — the live fleet
runs on one, and an auth change that strands workers mid-lease is worse than a
transitional period with both. Two buckets, **named for what they grant** rather
than for what they are:

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
works as a deploy gate, and it now names *which kind* of credential answered — a
per-machine token, a session, or a shared env token. `health` exits non-zero
when auth is OFF, and reports one of three postures:

| `auth` | Means |
| --- | --- |
| `token` | one or more shared env tokens are configured |
| `accounts` | no env tokens, but an account exists — the from-scratch path |
| `OPEN` | neither. Every caller has full access. A laptop smoke test only |

`accounts` used to be reported as `OPEN`, because both `/healthz` and
`/v1/whoami` judged the posture from the env token buckets alone and never asked
whether an account existed. A broker secured entirely by accounts therefore
reported that it had no auth at all, `whoami` returned `scope: write` for any
string whatsoever, and both documented deploy gates said the opposite of the
truth — which also aborted the per-machine worker bootstrap, since
`setup_windows.ps1` runs `token check --expect write`.

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
credentials, which is why the tests that exercise lease semantics run in
milliseconds with zero external dependencies. A managed database survives a
service restart or redeploy where a container's filesystem does not, and with
state external the service itself is **stateless and disk-free** — the cheapest
compute tier is enough.

**Supabase gives you two ports, and the difference matters here.** `6543` is the
*transaction* pooler and is what this deploys against; `5432` is a direct
connection. Transaction pooling is exactly the mode that breaks server-side
prepared statements — see the pooler note below, which is already handled.

### The schema creates itself

`db.connect()` applies the whole schema on every connection, so a brand-new
database is ready the moment the service first reaches it, and one from an
earlier release is brought forward at the same time. Nothing needs running by
hand; `casebroker init-db` exists only to do it *without* starting the service.

Every `CREATE TABLE` is `IF NOT EXISTS`, which upgrades cleanly whenever a
release **adds a table** — which, until recently, was the only kind of schema
change this repo had ever made. Adding a **column** was a different story: the
`IF NOT EXISTS` no-ops on a table that already exists without comparing columns,
so the column never appeared and the first index over it failed at connection
time with `no such column: priority` — which reads like a corrupt database
rather than one release behind. So the schema is now applied in three passes:
create the tables, `ALTER TABLE ... ADD COLUMN` whatever the database is
missing, and only then build the indexes, since an index is very often the thing
that references the new column.

Two limits worth knowing:

- It only ever **adds**. A column the database has and the schema no longer does
  is left alone — dropping it would destroy data to satisfy a code version that
  may itself be about to be rolled back.
- A column declared `NOT NULL` with no `DEFAULT` (or `UNIQUE`, or `PRIMARY KEY`)
  cannot be bolted onto a table that already exists, on any engine. Startup
  refuses with a message naming the column and why, rather than failing later
  and obscurely. If you are adding a column that existing rows must have, give
  it a `DEFAULT`.

`casebroker doctor` reports the schema version and checks the identity tables
(`users`, `sessions`, `worker_tokens`) alongside the campaign ones. It used to
count only the five campaign tables, which meant it reported "schema present"
against a database with no auth layer at all.

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
2. **Turn auth on**, by creating the admin account — step 2 of
   [First run](#first-run-from-nothing-to-a-working-broker). With no account
   *and* no env tokens, auth is off entirely: `GET /healthz` reports
   `"auth": "OPEN"` so it is visible rather than silent, but nothing stops it.
   If the host will be reachable before you get to it, set
   `CASEBROKER_SETUP_TOKEN` in the same breath, or the first stranger to find
   the setup form becomes your admin. Setting `CASEBROKER_WRITE_TOKENS` is
   still supported and still turns auth on, but a new deployment does not need
   it — and a new *machine* should get its own credential from **Machines**
   rather than a copy of a shared one.
3. **Point `CASEBROKER_DB` at the Supabase DSN** (port `6543`, the transaction
   pooler). A container's local filesystem does not survive a redeploy; a
   managed database does. With state external, the service needs no disk at all
   — the cheapest compute tier a platform offers is enough. If this is left
   unset, or set to a file path, the service starts anyway and logs a warning:
   it will look healthy right up until a redeploy silently empties it.

**`/healthz` is intentionally unauthenticated** (so infrastructure health checks
work with no token) **and therefore must never return anything that could be a
credential.** This was not a hypothetical: an early version of this service
returned the connection string verbatim, which meant hitting `/healthz` against
a real deployment printed the live database password in plain text with no auth
required to trigger it. It was then fixed to mask the password
(`_redact_db_target` in `app.py`).

Masking the password turned out to be necessary but **not sufficient**. What
survives redaction still names the exact database instance — host, port, user
and database — which is reconnaissance handed out for free to anyone who can
reach the URL. So the endpoint now answers two different callers differently:

| field | anonymous | authenticated |
| --- | --- | --- |
| `ok`, `version`, `auth`, `db_ok` | yes | yes |
| `db` (redacted DSN summary) | `null` | the summary |

`db_ok` is the part a health check actually needs — it still distinguishes
"service down" from "database down" with no credential. The `db` key stays
*present but null* rather than disappearing, so a client that reads it by name
does not break. If you ever add something else to this endpoint, ask not only
"could this be a credential?" but "does this help someone attack the thing it
describes?"

### Rotating the database password

The DSN lives in **four** places, and a rotation that misses one strands
something. In this order:

1. **Supabase** ▸ Project Settings ▸ Database ▸ Reset database password.
   Copy the new DSN for the **transaction pooler** (port `6543`), not the
   direct connection.
2. **Render** ▸ the service ▸ Environment ▸ `CASEBROKER_DB`. Saving triggers a
   redeploy, which is the restart the new credential needs.
3. **GitHub** ▸ repo ▸ Settings ▸ Secrets ▸ Actions ▸ `DBSTRING`. The
   `postgres` job in `.github/workflows/test.yml` runs against the real
   database on every push to `main`; a stale secret turns that job red on a
   commit that is perfectly fine.
4. **Your workstation** — whichever file you keep it in. `casebroker doctor`
   finds every copy on the box and tells you which ones still authenticate, so
   run it rather than trying to remember.

Then confirm, in this order:

```bash
casebroker doctor                       # every local copy, and whether it works
curl -s https://casebroker.onrender.com/healthz   # db_ok must be true
casebroker health --broker https://casebroker.onrender.com
```

Workers do **not** hold the database credential — they talk to the broker over
HTTP with a worker token — so the fleet needs no attention beyond surviving the
Render restart, which it does. A heartbeat that cannot reach the broker logs a
warning and tries again on the next tick, and the 15-minute lease TTL is far
longer than a redeploy. The one call that used to be at risk was `/v1/complete`
— the case solved, the archive written, and only the broker not yet told — so
it now retries across about four minutes rather than the ~30 s the other calls
use. A definitive `409` is still never retried.
