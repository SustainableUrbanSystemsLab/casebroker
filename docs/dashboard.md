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
- **Push notifications**: the broker tells a phone or a laptop what happened, with every tab closed

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

The token is displayed **once**: a bearer credential for a script or a CI step
acting as that name. A simulation node does not need one issued here -- it pairs
itself (`E3D setup-sim-node <broker-url>`, a device code you approve on this page)
and generates its own token, of which the broker keeps only the hash. Only an
issued token's hash is stored too, so this is the only moment it exists in
readable form — a credential the server could show you again is one an
attacker could read out of the database.

The list shows **last seen** per machine, which is the question a shared secret
could never answer: which box is this, and is it still alive? **Revoke** takes
effect on that machine's very next request — the check is a row read, not a
cached environment variable, so there is no redeploy — and leaves every other
machine running.

A cluster is one machine here, not one per node. Name the credential after the
cluster (`phoenix`) and it covers every worker id under that name —
`phoenix-<job>-<task>`. (A cluster's E3D nodes pair once per cluster account as
`ice` or `phoenix`: `slurm/ice_e3d_node.sbatch`, `slurm/phoenix_e3d_node.sbatch`.) Revoking it stops that cluster's workers on their next lease and
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

## Push notifications: told with the tab closed

**Settings ▸ Preferences** has two kinds of notification. *Browser notifications* are the
page's own: it compares one refresh with the next, so they fire only while a dashboard tab
is open. **Push notifications** are sent by the broker itself
([`casebroker/push.py`](../casebroker/push.py)) through the browser vendor's push service to
a small service worker (`/sw.js`), so they arrive with every tab closed, on a phone in a
pocket included.

**Turn on for this device** asks for the browser's permission, registers the worker,
subscribes and hands the subscription to the broker. Tick what this device should be told;
each change is saved on the broker at once. **Send a test** pushes one notice now and says
what the push service answered. **Turn off** removes it from the broker, then from the
browser. Signing out does the same, so a shared browser is not told the signed-out
account's notices.

| Kind | When | Default | Who |
| --- | --- | --- | --- |
| a machine asks to join | a node runs `E3D --setup-sim-node` and waits for approval (Settings ▸ Machines); lapses in 10 min | on | admins |
| cases finish | batched: at most one notice per half minute, and the count on screen adds up until it is dismissed | on | anyone |
| a case is quarantined | its attempts ran out, or a node called the site broken. The land audit and a re-spec, which an operator ran, are not announced | on | anyone |
| the broker drains a failing machine | `CASEBROKER_FAIL_BURST_CASES` cases failed on one machine within `CASEBROKER_FAIL_BURST_SECONDS` (an operator's own drain is not announced) | on | anyone |
| a machine holding a case goes silent | not heard from for `CASEBROKER_PUSH_SILENT_MINUTES` (20) while its case is leased | **off** | anyone |
| a node's update fails | it tried a new build, could not start it, and went back | on | anyone |
| the part store is nearly full | 90% of `CASEBROKER_PARTS_MAX_GB`, or the volume's free space down to 1.11× `CASEBROKER_PARTS_RESERVE_GB` | on | admins |
| nothing finishes for hours | cases are leased and none finished for `CASEBROKER_PUSH_STALL_HOURS` (6) | on | anyone |
| the queue runs dry | no case is pending any more | on | anyone |

*Silent* is off by default because a cluster job between allocations, or a preempted one,
is silent by design while its case waits: on a PACE chain that is every gap. The last four
are **conditions**: each is announced once when it becomes true and again only after it has
cleared (the store at below 85%), so a condition that lasts a day is one notice.

**Two levels of on/off, both on the broker.** Each subscription keeps its own list. An admin
also sees **Broker-wide** switches: a kind switched off there is sent to nobody, whatever
their device asked for (`PUT /v1/push/policy`, audited as a setting change).
`CASEBROKER_PUSH=0` switches push off entirely.

A click on a notice brings an open dashboard tab forward on the place it is about -- the
case, `#state=done`, Settings ▸ Machines, the Storage drawer, through the same
[deep links](#deep-links) -- or opens one. A second notice of a kind replaces the first
(`case_done` shares its tag with the tab's own finished-case notification, so a browser with
both on sees one).

### Where it cannot work, and what the page says instead

- **Plain http.** The Push API exists only in a secure context: https, or `localhost`.
- **iPhone and iPad.** Safari offers push only to a site added to the Home Screen
  (iOS 16.4+): Share ▸ **Add to Home Screen**, open the dashboard from that icon, sign in
  there (a Home Screen app keeps cookies of its own) and turn push on. The
  `/manifest.webmanifest` the page links is what makes it open as an app of its own.
- **Notifications blocked** for the site in the browser's settings: allow them there.
- **A browser with no Push API**: only the in-tab notifications are available.

### How it is built, and why

- **Every 30 s** a background thread started with the app reads the `events` table after a
  cursor kept in `settings` (`push_state`), and judges the four conditions. Every transition
  the broker records is written there inside the transaction that made it true, so reading it
  back announces only what committed. The new state is claimed with a compare-and-set before
  anything is sent, so two broker processes overlapping in a deploy never both announce one
  event. Rows are read once they are 10 s old, stopping at the first younger one, so a row
  that commits a moment late is not skipped; rows older than an hour are passed over, so a
  broker that was down for a day does not wake everyone with the backlog. The first run
  starts from now.
- **One push per kind per subscription per tick**: "3 cases finished: …". Time to live is 6 h
  (a laptop closed overnight still hears the queue ran dry), 10 min for a pairing request,
  which is useless once it lapses. Urgency is *high* for a pairing request, a drained
  machine and a failed update, which may wake a phone in power saving.
- **A subscription the push service calls gone** (404/410) is deleted at once; any other
  refusal counts, and ten in a row delete it. Its last error is shown on the device.
- **Who subscribed is re-checked at every send.** A subscription belongs to the credential
  that made it -- an account, a share link, a read or write token -- and only that credential
  can read, change, test or delete it (anyone else gets `404`). A revoked share link or a
  deleted account is told nothing more and its subscriptions are dropped; a demoted admin
  stops getting the admin-only kinds; a token taken out of the environment ends its own.
- **The broker POSTs only to push services.** A subscription names a URL the broker will
  request from inside the server's network, and any reader may submit one, so that is an
  open door into the LAN unless it is closed: only `https` endpoints on FCM
  (`fcm.googleapis.com`: Chrome, Edge, Opera, Brave, Samsung), Mozilla
  (`updates.push.services.mozilla.com`), Apple (`web.push.apple.com`, `*.push.apple.com`) and
  WNS (`*.notify.windows.com`) are accepted, with bounded lengths, and redirects are never
  followed. `CASEBROKER_PUSH_HOSTS` adds hosts. Keys and endpoints are never logged (the
  host only). At most 25 subscriptions per credential (the least recently used goes) and 500
  in all.
- **VAPID** (RFC 8292) is what makes a push service carry a message only for the server the
  browser subscribed to. The key is `CASEBROKER_VAPID_PRIVATE_KEY` when set (the raw
  base64url form `web-push generate-vapid-keys` prints, or a PEM); otherwise the broker makes
  one on first use and keeps it in `settings` (`push_vapid_private`, never written to the audit
  trail). Changing it orphans every subscribed browser -- a subscription is bound to the key
  it was made with -- and the page then offers to subscribe again. The `sub` claim is
  `CASEBROKER_VAPID_SUBJECT` (`mailto:` or `https:`); unset, it is the dashboard's own public
  https origin, learned from the first browser that subscribes from it, because Apple refuses
  a subject naming localhost and the broker, behind a proxy, cannot otherwise know its address.
- The **service worker** is a separate file (`casebroker/static/sw.js`) only because a browser
  runs one from a script URL of the site's own. It shows pushes, folds a batch into the notice
  of its kind still on screen, and routes clicks; it caches nothing and intercepts none of the
  page's requests. `/sw.js` is served `no-cache`: Cloudflare caches `.js` by extension, and a
  stale worker at the edge would pin every browser to it.

| Variable | Default | |
| --- | --- | --- |
| `CASEBROKER_PUSH` | on | `0` switches push off |
| `CASEBROKER_VAPID_PRIVATE_KEY` | made and kept in `settings` | the server's VAPID key |
| `CASEBROKER_VAPID_SUBJECT` | the dashboard's https origin | contact for the push services |
| `CASEBROKER_PUSH_SILENT_MINUTES` | 20 | *a machine goes silent* |
| `CASEBROKER_PUSH_STALL_HOURS` | 6 | *nothing finishes for hours* |
| `CASEBROKER_PUSH_HOSTS` | (none) | push service hosts beyond the four, `push.example.org,*.example.net` |

The API, all under read scope except the policy: `GET /v1/push/key` (public key, whether
push is on), `GET /v1/push/events` (the catalog, the broker-wide switch of each kind and
whether the caller may have it), `POST /v1/push/subscriptions`
(`{subscription: PushSubscription.toJSON(), events}`), `GET /v1/push/subscriptions?endpoint=`,
`PUT /v1/push/subscriptions` (`{endpoint, events}`), `DELETE /v1/push/subscriptions`
(`{endpoint}`), `POST /v1/push/test` (`{endpoint}`) and `PUT /v1/push/policy`
(`{events: {kind: bool}}`, admin). Admin-only kinds asked for by anyone else are dropped
silently.
