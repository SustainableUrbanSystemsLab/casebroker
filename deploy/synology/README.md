# Broker on the lab Synology

Postgres and the broker run as one Container Manager project on the NAS
(`compose.yaml` here). A push to `main` deploys itself:

```
push main ─► CI: tests, image smoke test ─► publish-image: ghcr.io/…/casebroker:latest + :sha-<sha>
                                                  │
NAS: Watchtower (polls every 10 min, label-enabled) ─► pulls :latest, recreates `casebroker`
                                                  │
CI: nas-deploy-check ─► polls $NAS_BROKER_URL/healthz until `commit` is this push
```

Traffic: `<broker-host>` ─► Cloudflare (proxied) ─► DSM reverse
proxy :443 ─► `127.0.0.1:8010` ─► broker ─► `postgres:5432`.

## One-time setup

1. **GHCR package public.** After the first `publish-image` run: GitHub ▸ org
   ▸ Packages ▸ `casebroker` ▸ Package settings ▸ Change visibility ▸ Public.
   The repo is public already; this only spares the NAS a registry login.
2. **Project folder.** `/volume1/docker/casebroker/` with `compose.yaml` and a
   filled-in `.env` (from `.env.example`).
3. **Replace the standalone `postgres` project.** It uses the same data
   directory and the same container name. Container Manager ▸ Project ▸
   `postgres` ▸ Stop, then Delete (the project only — the data in
   `/volume1/docker/postgres/data` stays). **Never run both**: two servers on
   one data directory corrupt it.
4. **Create the project.** Container Manager ▸ Project ▸ Create ▸ path
   `/docker/casebroker` ▸ use the existing `compose.yaml`. Skip the web portal
   step it offers.
5. **Reverse proxy.** Control Panel ▸ Login Portal ▸ Advanced ▸ Reverse Proxy ▸
   Create: source HTTPS `<broker-host>` :443, destination HTTP
   `localhost` :8010. Custom headers: add `X-Forwarded-Proto` = `https`
   (the broker sets the session cookie's Secure flag from it).
6. **DNS.** Cloudflare ▸ your zone ▸ a proxied record for the broker host,
   the same as the NAS's other proxied hostnames.
7. **CI.** Repo ▸ Settings ▸ Variables ▸ `NAS_BROKER_URL` =
   `https://<broker-host>`. Until it is set, `nas-deploy-check` is
   skipped.

## Moving the campaign off Supabase

Workers hold leases, so do this in a quiet window.

1. Stop the workers (or let them finish).
2. On the NAS, dump Supabase through its **session** pooler (port `5432`, not
   the `6543` transaction pooler) straight into the new database:
   ```bash
   sudo /usr/local/bin/docker exec -i \
     -e SRC='postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres' \
     postgres sh -c \
     'pg_dump --no-owner --no-acl -n public "$SRC" | psql -v ON_ERROR_STOP=1 -U admin -d casebroker'
   ```
   Restore into an EMPTY `casebroker` database: if the broker has already
   started once, it has created the schema, and the restore collides with it.
   Stop the `casebroker` container first and `DROP DATABASE casebroker;
   CREATE DATABASE casebroker;` if so. Then, still before the restore, run
   `DROP SCHEMA public;` inside `casebroker`: a dump taken with `-n public`
   contains its own `CREATE SCHEMA public`, which `ON_ERROR_STOP` would
   otherwise abort on.
3. Start the broker; `curl https://<broker-host>/healthz` must say
   `"ok": true`, and `casebroker doctor` should pass against it.
4. Point every worker's `CASEBROKER_URL` (machine.env, SLURM scripts) at the
   new URL. Accounts and per-machine tokens moved with the data; the shared
   `CASEBROKER_WRITE_TOKENS` moved via `.env`.
5. Keep Supabase for a week as the fallback, then retire it. (Done for
   Render on 2026-10-06: the service and its `deploy-smoke-test` CI job are
   gone; `nas-deploy-check` is the production deploy check.)

## Roll back

Pin the image in `compose.yaml` to an earlier tag
(`ghcr.io/sustainableurbansystemslab/casebroker:sha-<sha>`) and rebuild the
project. Watchtower leaves a pinned tag alone unless that tag itself moves.

## Backups

The data directory is not safe to copy while Postgres runs. A nightly Task
Scheduler job (root) that dumps instead:

```bash
/usr/local/bin/docker exec postgres pg_dump -U admin -Fc casebroker \
  > /volume1/docker/casebroker/backup/casebroker_$(date +%F).dump
find /volume1/docker/casebroker/backup -name '*.dump' -mtime +14 -delete
```
