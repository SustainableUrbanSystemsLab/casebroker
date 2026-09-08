# The dashboard

## Dashboard

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

Open it in a browser, paste in the broker's URL and a write token value
(kept only in that browser's local storage, sent as a bearer header on each API
call). The token field is a real `<input type="password">` inside a `<form>` with
a submit control, so a browser's own password manager can recognise and offer to
save it, exactly like any other login form. The page itself carries no secrets and
loads with no auth; every request it makes for actual data goes through the same
token check as any other client.

**Sharing a read-only view.** Set `CASEBROKER_READ_TOKENS` (same
comma-separated shape as `CASEBROKER_WRITE_TOKENS`) to a *separate* value from your
worker token, paste it into the "Share a read-only link" box in the Connection
panel, and click **Copy read-only link** — it builds a
`https://.../?token=...&ro=1` URL that pre-fills the token, connects
automatically, and shows a banner. This is a real second credential, not a
client-side restriction: a read-only token is rejected with 401 by every
mutating endpoint (`POST /v1/cases`, `/v1/lease`, `/v1/heartbeat`,
`/v1/complete`, `/v1/fail`, `/v1/release`) regardless of how it is presented —
someone you send the link to could `curl` the API directly with it and still
could not lease, complete, fail or release a case, or add new ones. Never put
a write token in a link you hand out; it has none of these
restrictions.
