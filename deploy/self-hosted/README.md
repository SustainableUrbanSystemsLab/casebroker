# Broker on the self-hosted server

Postgres and the broker run as one Docker Compose project on the server
(`compose.yaml` here). A push to `main` deploys itself:

```
push main ─► CI: tests, image smoke test ─► publish-image: ghcr.io/…/casebroker:latest + :sha-<sha>
                                                  │
server: Watchtower (polls every 10 min, label-enabled) ─► pulls :latest, recreates `casebroker`
                                                  │
CI: deploy-check ─► polls $BROKER_URL/healthz until `commit` is this push
```

Traffic: `<broker-host>` ─► Cloudflare (proxied) ─► the host reverse
proxy :443 ─► `127.0.0.1:8010` ─► broker ─► `postgres:5432`.

## One-time setup

1. **GHCR package public.** After the first `publish-image` run: GitHub ▸ org
   ▸ Packages ▸ `casebroker` ▸ Package settings ▸ Change visibility ▸ Public.
   The repo is public already; this only spares the server a registry login.
2. **Project folder.** `/srv/docker/casebroker/` with `compose.yaml` and a
   filled-in `.env` (from `.env.example`).
3. **Replace the standalone `postgres` project.** It uses the same data
   directory and the same container name. `docker compose down` in the old `postgres` project (the project only — the data in
   `/srv/docker/postgres/data` stays). **Never run both**: two servers on
   one data directory corrupt it.
4. **Create the project.** `docker compose up -d` in the project folder.
5. **Reverse proxy.** Create a rule: source HTTPS `<broker-host>` :443, destination HTTP
   `localhost` :8010. Custom headers: add `X-Forwarded-Proto` = `https`
   (the broker sets the session cookie's Secure flag from it).
6. **DNS.** Cloudflare ▸ your zone ▸ a proxied record for the broker host,
   the same as the server's other proxied hostnames.
7. **CI.** Repo ▸ Settings ▸ Variables ▸ `BROKER_URL` =
   `https://<broker-host>`. Until it is set, `deploy-check` is
   skipped.

## The part store: the cases themselves

Nodes can upload every part of a case to the broker (docs/protocol.md, "Parts the
broker holds"), which keeps them in `/srv/casebroker-parts`, one file per part,
named by its sha256. One-time setup:

1. **Create the folder** `casebroker-parts`: no recycle bin
   (parts are replaced, not edited), and only `admin` with read/write. Not inside the
   `docker` share, whose permissions let every share user write.
2. Rebuild the `casebroker` project; `/v1/storage` then shows `parts_store`.
3. Add the folder to **backups**. Every file is written once and never changed,
   so an incremental backup copies each part exactly once.

**Capacity, measured 2026-10-06.** A case is ~8.5 GB of parts: the mesh ~185 MB, each
of 32 directions ~260 MB. 5,000 cases are ~42 TB; this volume had a few TB free. The
compose file caps the store at 1.5 TB and keeps 500 GB of the volume free whatever
happens, so it holds the first ~170 complete cases. Past that a node is told 507 and
keeps shipping that part to the Syncthing master, exactly as today, so nothing is lost --
but Syncthing cannot be retired until there is room for what it carries. The levers:

- `CASEBROKER_PARTS_KEEP=mesh,archive` keeps every case's mesh and archive (~1 TB for
  the campaign) and leaves the directions to the master.
- More disk.
- Smaller parts: what fills a direction is worth measuring before buying disks --
  `scripts/part_sizes.py` on the master says, per file type, where the 260 MB goes.
## Speed: what the app does, and the Cloudflare settings around it

Measured 2026-10-06: the app answers in ~2 ms and the host's TLS handshake costs ~67 ms;
everything else was transfer. The app now serves the dashboard as a ~44 KB shell
plus content-named `/assets/dashboard.<sha>.{js,css}` (`immutable`, one year),
gzips HTML and JSON itself, and answers `If-None-Match` and `If-Modified-Since`
with 304. What is left is configuration, in the broker's DNS zone:

- **Caching ▸ Configuration ▸ Browser Cache TTL: Respect Existing Headers.** The
  assets say a year, the shell says `no-cache`, the API says nothing.
- **Caching ▸ Tiered Cache ▸ Smart Tiered Caching: on** (free). One upper tier
  fetches from the server, so an asset crosses to the origin once, not once per edge.
- **Speed ▸ Optimization: HTTP/3 and 0-RTT on**; **Rocket Loader off** (it rewrites
  the dashboard's script tags).
- **Never "Cache Everything"** for this hostname: `/v1/*` answers depend on who is
  asking. JSON is not cached by default, which is what keeps it safe.
- Optional, paid: **Argo Smart Routing** shortens the long origin
  leg for the uncacheable API calls.
- On the server: an **ECDSA** certificate (`acme.sh --keylength ec-256`) makes the host's
  TLS handshake far cheaper on its CPU than the RSA 2048 one it has now.

## Moving the campaign off Supabase

Workers hold leases, so do this in a quiet window.

1. Stop the workers (or let them finish).
2. On the server, dump Supabase through its **session** pooler (port `5432`, not
   the `6543` transaction pooler) straight into the new database:
   ```bash
   sudo docker exec -i \
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
   gone; `deploy-check` is the production deploy check.)

## Roll back

Pin the image in `compose.yaml` to an earlier tag
(`ghcr.io/sustainableurbansystemslab/casebroker:sha-<sha>`) and rebuild the
project. Watchtower leaves a pinned tag alone unless that tag itself moves.

## Backups

The data directory is not safe to copy while Postgres runs. A nightly cron job (root) that dumps instead:

```bash
docker exec postgres pg_dump -U admin -Fc casebroker \
  > /srv/docker/casebroker/backup/casebroker_$(date +%F).dump
find /srv/docker/casebroker/backup -name '*.dump' -mtime +14 -delete
```
