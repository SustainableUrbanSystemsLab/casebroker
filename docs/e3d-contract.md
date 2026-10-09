# The E3D contract

What the **E3D Simulation Broker** requires of `E3D.exe` — the Eddy3D CLI,
published as one self-contained file per platform (`E3D.exe`, `E3D-linux-x64`,
`E3D-macos-arm64`) — and what it promises in return. This is the seam between
the campaign and the CFD: everything above it is scheduling, everything below
it is fluid dynamics, and neither needs to know how the other works.

`E3D.exe` is built from the Eddy3D repository (project `Eddy3DCli`), a separate
codebase. Nothing here is enforced by this repository; the broker degrades for
a node that does less, as each section says.

**Contract version: 3.** Version 3 drops the Python-worker arrangement
(requirements 1–9 of version 2: the runner's stdin/stdout/exit-code contract,
the `$E3D_TRACE_FILE` solver trace, `CASEBROKER_URL`/`CASEBROKER_WORKER_ID` in
the environment). That worker, `casebroker.worker` driving `runner/run_case.sh`,
is retired: its runner could not build any campaign recipe. Identity (version
2's requirement 10) stands, below.

## One arrangement: the node is E3D

Since 2026-09-21 a simulation node **is** `E3D.exe`. `E3D node` pairs with the
broker (device code in a browser; the broker keeps only the token's hash),
leases, builds the site geometry natively, solves, sends every part of the case
to the broker's part store as it finishes (and keeps it in its done folder),
reports, and updates itself when the broker names a build. The node's own
contract is Eddy3D's `docs/SIMULATION_NODE.md`; what the broker asks of it is
[`protocol.md`](protocol.md) and, for updates, [`releases.md`](releases.md). In
short, a node:

- declares `build`, `version`, `platform` and `recipes` with every lease, and
  from protocol 2 its `features`, `cpus` and `mem_gb`;
- reads the broker's `protocol` and `features` from `/healthz` at start and
  after an outage, and sends only what the broker lists;
- asks `GET /v1/node/release` before every lease and at every heartbeat, saying
  which build it is, what it is doing about the target (`state`), or that it
  tried a build and rolled back (`failed_build`);
- takes a new build's file from its own release share and verifies it against
  the hash the broker gave, over the channel already authenticated per machine;
- holds nothing but its own per-machine credential, which acts only on leases
  held under its own name.

## Retryable or fatal

A node reports a failure with `retryable` (`POST /v1/fail`). Fatal means *this
site is broken and must never be retried*: degenerate geometry that will fail
identically on every machine, forever. Anything else -- a node died, an image
pull failed, a host was down, a step ran out of the node's own time -- is
retryable, and goes to another machine first. A machine that cannot run at all
(no engine, a broken install, a full disk) does not fail the case: it RELEASES
it, refunded, and stops.

Getting this wrong is expensive in one direction only. A wrongly *retryable*
error costs at most three attempts; a wrongly *fatal* one removes a site from
the campaign permanently. Treating a missing input file as fatal once
quarantined 173 perfectly good sites in under a minute. When in doubt,
retryable.

## Identity

**E3D shall** answer `E3D version --json` with one JSON object on stdout
carrying at least `build` (`version+commit`, as in `1.14.0.827+e044a147`),
`version`, and `platform` (`win-x64` / `linux-x64` / `osx-arm64`: the names its
published files carry). `recipes`, the exact recipes it knows, is welcome.

The node build workflow names a release by asking the binary itself
(`release.json`: `[{build, platform, file, sha256}]`), so the build a broker
targets can never disagree with what the file says it is; the node declares the
same `build` with every lease. That is how the fleet table says which build a
machine runs and whether it is behind the campaign's target, and how a campaign
that insists on a declared build (`require_build`) tells this machine from one
nobody has updated.

## Compatibility

Every requirement degrades rather than breaks.

- A node that declares no build leases as it always did and shows as
  *undeclared*; a campaign that has switched on `require_build` refuses it with
  426.
- A node that sends no `features`, `cpus` or `mem_gb` is leased exactly as
  before protocol 2; a broker without `features` on `/healthz` is one the node
  treats by its older 404 rules.
- A node that sends no telemetry still reports progress: the heartbeat's line
  is what the dashboard draws, and the broker assembles a coarse residual curve
  from `solve` reports when a node sends no series.

The rule throughout: a progress report is decoration on a heartbeat, and must
never be able to break the solve it describes or the node reporting it.
