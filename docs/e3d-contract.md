# The E3D contract

What the **E3D Simulation Broker** requires of `E3D.exe` — the Eddy3D CLI,
published as one self-contained file per platform (`E3D.exe`, `E3D-linux-x64`,
`E3D-macos-arm64`) — and what it promises in return. This is the seam between
the campaign and the CFD: everything above it is scheduling, everything below
it is fluid dynamics, and neither needs to know how the other works.

`E3D.exe` is built from the Eddy3D repository (project `Eddy3DCli`), a separate
codebase. This file is the specification that side implements against; nothing
here is enforced by this repository, and an `E3D.exe` that implements none of
it still works — see [Compatibility](#compatibility).

**Contract version: 2.** Version 2 adds requirement 10, identity; every earlier
requirement is unchanged.

## Two arrangements

Since 2026-09-21 a simulation node **is** `E3D.exe`. `E3D node` pairs with the
broker (device code in a browser, the broker keeps only the token's hash),
leases, builds the site geometry natively, solves, drops the archive into the
Syncthing folder the master collects from, reports, and updates itself when
the broker names a build. That contract is Eddy3D's own —
`docs/SIMULATION_NODE.md` in the Eddy3D repository — and what the broker asks
of such a node is the protocol in [`protocol.md`](protocol.md) and, for
updates, [`releases.md`](releases.md). In short, an E3D node:

- declares `build`, `version`, `platform` and `recipes` with every lease;
- asks `GET /v1/node/release` before every lease and at every heartbeat,
  saying which build it is, what it is doing about the target (`state`), or
  that it tried a build and rolled back (`failed_build`);
- takes a new build's file from its own release share and verifies it against
  the hash the broker gave, over the channel that is already authenticated per
  machine;
- holds nothing but its own per-machine credential.

The second arrangement is the **Python worker** — `casebroker.worker` driving
[`runner/run_case.sh`](../runner/run_case.sh), which runs `E3D.exe` as a child
process — the validation reference and the fallback, on PACE and on any
workstation that was set up before the node existed. The rest of this file is
that arrangement's contract with `E3D.exe`.

## Why the Python worker, not E3D, talks to the broker

It would be natural to have `E3D.exe` lease its own work and report its own
results over HTTP — and in the first arrangement above it now does. It took a
year to get there, for two reasons that still hold for the Python path.

The first is that the interesting parts of a worker are not about CFD at all.
Catching `SIGTERM` to release a lease and refund the attempt when Phoenix
preempts; the rule that a `409` means stop; retry budgets; backing off on a
drained queue; resuming a half-finished case after a restart — that is campaign
logic. Moving it into the CFD binary means every protocol change needs a
rebuilt binary redistributed to every machine, when the protocol is versioned
MAJOR precisely *because* workers are long-lived and nobody restarts them in a
hurry. (The E3D node answers this with the release mechanism in
[`releases.md`](releases.md): a rebuilt binary now redistributes itself.)

The second is blast radius. On the Python path `E3D.exe` never reads
`CASEBROKER_TOKEN`, and **not reading it is the guarantee**: a compromised or
misbehaving solver cannot lease, complete, fail, or purge anything. It computes,
and it writes files.

Everything `E3D.exe` needs to tell the broker on this path, it tells by writing
a file that the runner already reads.

## E3D shall — the runner contract

1. **E3D shall** accept the case spec as a single JSON object on stdin, and
   identically in `$CASE_SPEC`.
2. **E3D shall** emit exactly one JSON object as the **last line of stdout**,
   carrying at least `result_uri`.
3. **E3D shall** exit `0` on success, `64` when this geometry can never succeed
   on any machine, and any other code for a retryable failure.

   Getting (3) wrong is expensive in one direction only. A wrongly *retryable*
   error costs at most three attempts; a wrongly *fatal* one removes a site from
   the campaign permanently. Treating a missing input file as fatal once
   quarantined 173 perfectly good sites in under a minute. When in doubt,
   retryable.

## E3D shall — the solver trace

4. **E3D shall**, when `$E3D_TRACE_FILE` is set, append one JSON object per
   **outer iteration** to that path, newline-delimited, flushed as it goes.
5. **E3D shall** record **every** outer iteration, without decimation.
6. **E3D shall** treat the trace as best-effort: failing to write it shall never
   fail a solve.

One record:

```json
{"iteration": 412, "phase": "solve", "elapsed_s": 8280,
 "residuals": {"p": 3.2e-05, "Ux": 8.1e-07, "Uy": 6.4e-07, "k": 2.2e-06},
 "converged": false}
```

`iteration` is required; everything else is optional and may be omitted when it
does not apply. Unknown keys are ignored, so the schema can grow without
breaking an older reader.

**One file, two readers.** The runner tails the **last line** for the heartbeat
the dashboard shows, and archives the **whole file** beside the result. There is
no second live-progress file to keep in sync, and the tail is read by seeking to
the end, so the cost of reporting progress does not grow with the length of the
solve.

**Why every iteration.** The trace lands next to a multi-megabyte result
archive; a two-thousand-iteration solve is about 290 KB of JSONL, which is
noise by comparison. You can always downsample a full trace, never upsample a
decimated one — and decimation hides exactly the oscillation you would want to
find later. The live signal is throttled by the runner's own 60-second timer,
not by how often `E3D.exe` writes, so writing every iteration costs nothing
upstream.

**Why the solver and not the log.** Without a trace the runner recovers
residuals by regex from the solver log, and has to infer the outer-iteration
boundary itself: `p` is solved several times per step
(`nCorrectors` × `nNonOrthogonalCorrectors`, about six on this campaign), so a
naive read interleaves corrector stages and makes a monotone descent look like a
two-decade oscillation. That misread cost a real detour. `E3D.exe` knows where
its own iteration boundaries are; the regex only guesses.

## E3D shall — deployment awareness

7. **E3D shall** read `CASEBROKER_URL` and `CASEBROKER_WORKER_ID` from the
   environment when present, and stamp them into its own run metadata.
8. **E3D shall** stamp `case_id` and `worker_id` into the result directory, so
   an archive is traceable to the machine that produced it without consulting
   the broker.
9. **E3D shall not** read `CASEBROKER_TOKEN`.

## E3D shall — identity

10. **E3D shall** answer `E3D version --json` with one JSON object on stdout
    carrying at least `build` (`version+commit`, as in `1.14.0.827+e044a147`),
    `version`, and `platform` (`win-x64` / `linux-x64` / `osx-arm64`: the names
    its published files carry). `recipes`, the exact recipes it knows, is
    welcome and not required.

The Python worker asks once at start-up (`--e3d`, default `$EDDY3D_CLI`) and
sends the answer with every lease. That is how the fleet table says which build
a machine runs and whether it is behind the campaign's target, and how a
campaign that insists on a declared build (`require_build`) tells this machine
from one nobody has updated. The recipes are **not** forwarded on E3D's word:
the runner script, not `E3D.exe`, decides what this machine can produce, and an
operator who knows declares them with `--recipes`.

The Python worker never asks `/v1/node/release`, so it never updates itself.
The fleet table says so — "cannot update itself" — and the update is by hand:
copy the new `E3D.exe` over the old one and start the worker again.

## What the broker side provides

| Variable | Set by | Meaning |
| --- | --- | --- |
| `CASE_SPEC` | the worker | the case spec, also on stdin |
| `E3D_TRACE_FILE` | `run_case.sh` | where to append the trace; archived beside the result |
| `CASEBROKER_URL` | `machine.env` | which broker this machine serves |
| `CASEBROKER_WORKER_ID` | `machine.env` | this machine's stable id |
| `EDDY3D_CLI` | `machine.env` | where `E3D.exe` is; asked `version --json` at start-up |

`machine.env` is written by `casebroker worker setup` — see
[First run](operations.md#first-run-from-nothing-to-a-working-broker).

## Compatibility

Every requirement here degrades rather than breaks.

- An `E3D.exe` that writes **no trace** still reports progress: the runner falls
  back to parsing the solver log, exactly as it did before this contract
  existed. That fallback is not deprecated and is not going away.
- A **malformed or truncated** trace line falls back the same way. The trace is
  read last-line-first and parsed defensively; a partial write during a crash
  costs one heartbeat's detail, nothing more.
- `$E3D_TRACE_FILE` **unset** means the runner is older than the solver.
  `E3D.exe` should skip the trace entirely rather than guessing a path.
- An `E3D.exe` that ignores `--json` and prints a bare version declares
  nothing: the worker leases as it always did and the fleet table shows the
  machine as *undeclared*. A campaign that has switched on `require_build`
  refuses it with 426, and the worker stops with exit code 3 and the reason.

The rule throughout: a progress report is decoration on a heartbeat, and must
never be able to break the solve it describes or the worker reporting it.
