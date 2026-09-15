# The dashboard

`GET /` serves a small ops UI — no build step, one HTML file, no dependency on
anything outside the broker's own API:

- campaign status (counts by state and by split, expired leases, 24h throughput, ETA),
  auto-refreshing every 60s by default
- the worker table, including which host/cluster each worker actually ran on
- a browsable, paginated, filterable case list — click a row to expand every
  field the broker holds on it: state, split, LCZ, attempts, **which machine
  produced it** (worker/host/cluster, from the runner's own report at
  completion), **where its result is finally stored** (`result_uri`, bytes,
  sha256), wall time, and the last error if it has one — plus a jump-to-id box
  for a direct lookup
- **Machines**: one credential per box, issued and revoked here

## Getting in

Open the broker's URL in a browser. The URL is filled in from the page's own
origin, so there is normally nothing to type; the field is behind a *change*
link for the case where you are pointing one dashboard at another deployment.

The panel then shows one of three things, decided by `GET /v1/auth/state` rather
than guessed client-side:

| State | What you see |
| --- | --- |
| No account exists yet | **Set up this broker** — the first account you create becomes the admin, and the form then closes for good |
| An account exists | **Sign in** — username and password |
| Signed in | your username and role, and the **Machines** panel if you are an admin |

Setup and login are a real `<form>` with a real `<input type="password">`, so a
browser's password manager recognises and offers to save them.

Signing in issues a **server-side session cookie** — HttpOnly, SameSite=Lax,
`Secure` whenever the request arrived over TLS — that lasts a fortnight. Being
server-side is what makes logging out, or revoking everything after a laptop goes
missing, take effect immediately rather than waiting for a signature to expire.
There is no token to paste and nothing kept in local storage.

> The setup form asks for a credential whenever the broker already holds one:
> `CASEBROKER_SETUP_TOKEN` if it is set, otherwise one of `CASEBROKER_WRITE_TOKENS`.
> `/v1/auth/setup` cannot require a login — there is nobody to log in as yet —
> so on a deployment reachable before you have set it up, whoever found the form
> first would otherwise become its permanent admin. A broker with neither
> configured (a laptop, or a host behind a firewall) has nothing to ask for.
> See [First run](operations.md#first-run-from-nothing-to-a-working-broker).

**Admins and viewers.** A `viewer` can log in and read the campaign and nothing
else: every mutating endpoint answers `403`, and the Machines panel is not shown
because every action in it would be refused. That is the safe way to let someone
watch progress — unlike a shared read-only token, it is attributable to a person
and revocable on its own. Create one with
`casebroker account create --username bob --role viewer`, or `POST /v1/users`.

## Machines: one credential per box

Signed in as an admin, **Machines** ▸ *Issue token* mints a credential for one
machine, named after the worker id that box will run under.

The token is displayed **once**, with the exact `bootstrap_worker.ps1` line to
paste on that machine. Only its hash is stored, so this is the only moment it
exists in readable form — a credential the server could show you again is one an
attacker could read out of the database.

The list shows **last seen** per machine, which is the question a shared secret
could never answer: which box is this, and is it still alive? **Revoke** takes
effect on that machine's very next request — the check is a row read, not a
cached environment variable, so there is no redeploy — and leaves every other
machine running.

A cluster is one machine here, not one per node. Name the credential after the
cluster (`phoenix`) and it covers every worker id under that name —
`phoenix-<job>-<task>`, which is what `slurm/phoenix_worker.sbatch` runs each
task as. Revoking it stops that cluster's workers on their next lease and
nothing else.

## Sharing a read-only view

The good way is a `viewer` account: attributable, individually revocable, and it
needs no link handling.

The older way still works. Set `CASEBROKER_READ_TOKENS` (same comma-separated
shape as `CASEBROKER_WRITE_TOKENS`) to a *separate* value from your worker
token, then click **Copy read-only link** in the Connection panel. It fetches
that token from `GET /v1/share-token` — which itself requires write auth, and is
not an escalation because a write credential already passes every read gate —
and builds a `https://.../?token=...&ro=1` URL that pre-fills the token,
connects automatically, and shows a banner.

This is a real second credential, not a client-side restriction: a read-only
token is rejected with
`401` by every mutating endpoint (`POST /v1/cases`, `/v1/lease`, `/v1/heartbeat`,
`/v1/complete`, `/v1/fail`, `/v1/release`) regardless of how it is presented —
someone you send the link to could `curl` the API directly with it and still
could not lease, complete, fail or release a case, or add new ones.

Never put a **write** token in a link you hand out; it has none of these
restrictions.
