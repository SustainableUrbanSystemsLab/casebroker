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
- **Deep links**: the address bar says where you are, so any view -- a case, a filtered
  list, a drawer -- is a link you can bookmark or send
- **Sharing**: an admin makes a read-only link for someone with no account, from the page

## Getting in

Open the broker's URL in a browser. The URL is filled in from the page's own
origin, so there is normally nothing to type; the field is behind a *change*
link for the case where you are pointing one dashboard at another deployment.

The panel then shows one of these, decided by `GET /v1/auth/state` rather
than guessed client-side:

| State | What you see |
| --- | --- |
| No account exists yet | **Set up this broker** — the first account you create becomes the admin, and the form then closes for good |
| An account exists | **Sign in** — username and password |
| Signed in | your username and role, and the **Machines**, **Users** and **Sharing** panels if you are an admin |
| Opened a share link | a *Read-only view* banner and **Leave this view**; no sign-in form, since there is nothing to sign in to |

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

**Three roles.** An `admin` can do everything. An **`operator`** runs the
campaign — adds cases, leases, completes, fails, releases — and manages nothing:
no accounts, no machine credentials, and no purging. A `viewer` reads and
nothing else. Operator is the one most accounts should have; before it existed,
`admin` was the only role that could write, which is how a deployment ends up
with everyone an admin.

A viewer or an operator sees neither the Users, the Sharing nor the Machines panel,
because every action in them would be refused.

## Users: people, as opposed to machines

Signed in as an admin, **Settings ▸ Users** lists every account with its role
and last login, and adds one with a username, an initial password and a role.
The role of an existing account is a dropdown — changing it takes effect on that
account's next request, without them signing in again — and each row offers a
password reset (which revokes every session that account holds) and a delete.

The role picker is built from what `GET /v1/auth/state` reports rather than from
a list written into the page, so it can never offer a role the broker would
refuse.

The last admin cannot be demoted or deleted here any more than over the API:
there is no recovery endpoint, so that would leave the deployment unmanageable.

Everything here is also `casebroker account ...` on a box that can reach the
database, which is what recovers a deployment nobody can log into.

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

## Deep links

The address bar always says where you are, so a link opens the same place. It all lives
in the URL's **fragment** (after the `#`), which a browser keeps to itself: it is not sent
to the broker, not written to the proxy's log and not passed on in a `Referer`.

| Link | Opens |
| --- | --- |
| `/#case=v2-00697fb4542aa4c6` | that case, expanded, on its own |
| `/#state=quarantined&split=test` | the case list, filtered (`state`, `split`, `city`, `label`, `recipe`) |
| `/#state=done&sort=attempts&dir=asc&offset=50` | and ordered, and on its third page |
| `/#settings=users` | Settings on that tab (`setup`, `connection`, `users`, `sharing`, `machines`, `campaign`, `prefs`) |
| `/#storage`, `/#dataset` | the Storage and Dataset drawers |

They combine (`#case=…&settings=sharing`). Open one in a tab that already has the dashboard
loaded and it takes effect without a reload; Back and Forward walk through the cases you
opened. A link that needs a sign-in waits for it: sign in and the page lands where the link
pointed.

Everything in a link is checked against what the page itself offers -- a state it lists, a
column it sorts by, a tab it has -- and anything else is dropped. A case on show makes the
address the case (and the recipe scope, which changes its percentiles) and nothing about the
list under it, so a link copied from the bar means the same to whoever it is sent to.

An open case has a **link** button beside its ID that copies its link. The older `?pair=CODE`
(a node asking to join) and `?token=…&ro=1` still work as they did.

## Sharing a read-only view

**The way to do it: a share link.** Signed in as an admin, click the share button in the
header (or **Settings ▸ Sharing**, or the share button in an open case's first card, which
makes the link open on that case). Say who it is for, how long it should last (a day, a
week, a month, three months, or until you revoke it) and **Create link**. The link is shown
once, with **Copy link**, **Email it** (opens your mail program with the message written)
and, where the browser has a share sheet, **Share…**.

Whoever opens it needs no account, sees everything a `viewer` account sees -- every case,
its results, the fleet -- and can change nothing. Not by the buttons and not by calling the
API: a link is a credential the write gates do not accept (`403`, "this is a read-only
link"), and it reaches neither the accounts nor the machines. The page shows a *Read-only
view* banner with **Leave this view**.

How it is built, and why:

- The token travels in the **fragment** (`/#share=…`), so it is never sent to the broker or
  logged on the way. The page trades it at once for an HttpOnly cookie
  (`POST /v1/auth/share`) and takes it out of the address bar and the history entry before
  anything else happens. What stays in the bar is a link to where they are, which is safe to
  copy again.
- Only the token's **hash** is stored, like a session or a machine credential, so a database
  dump does not hand over a working link -- and for the same reason the link cannot be shown
  again. If it is lost, make another and revoke the first.
- It is checked against the database on **every request**, so **Revoke** (in the same tab)
  ends it on the holder's next click, and an expired link stops by itself. Both show the
  holder "this shared link has expired or been withdrawn".
- The list says who each link is for (your note: the holder is never shown it), who made it,
  when it was last used and how many times it was opened. Links that ended stay on it for a
  month. At most 50 are live at once.
- Opening your own link while signed in changes nothing: you keep your login, and are told
  to use a private window to see what they see.
- It is the address the page was opened on that goes into the link. Make it from the
  dashboard's public address, not from `localhost` or a LAN number; the page warns when it
  is open on one.

Everything here is also the API: `POST /v1/shares`, `GET /v1/shares`,
`DELETE /v1/shares/{id}` (admin), `POST /v1/auth/share` ([protocol](protocol.md#identity)).
A link's token also works as a bearer for reading, so a script can be given one too.

**A `viewer` account** is the other way: attributable, individually revocable, and it needs a
password the person keeps. Settings ▸ Users creates one in about ten seconds. Worth it for
someone who will come back often.

**The older way** still works, for brokers that set it. Set `CASEBROKER_READ_TOKENS` (same
comma-separated shape as `CASEBROKER_WRITE_TOKENS`) to a *separate* value from your worker
token, then click **Copy read-only link** in the Connection panel (it appears only when the
broker has one). It fetches that token from `GET /v1/share-token` -- which itself requires
write auth, and is not an escalation because a write credential already passes every read
gate -- and builds a `https://.../?token=...&ro=1` URL that pre-fills the token, connects
automatically, and shows a banner. It is one shared secret with no name, no expiry and no
way back short of a redeploy, and it sits in the query string (so in the proxy's log),
which is why a share link is better.

A read-only token is a real second credential, not a client-side restriction: it is rejected
with `401` by every mutating endpoint (`POST /v1/cases`, `/v1/lease`, `/v1/heartbeat`,
`/v1/complete`, `/v1/fail`, `/v1/release`) regardless of how it is presented --
someone you send it to could `curl` the API directly with it and still could not lease,
complete, fail or release a case, or add new ones.

Never put a **write** token in a link you hand out; it has none of these
restrictions.
