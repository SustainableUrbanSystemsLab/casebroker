# The E3D contract

What the **E3D Simulation Broker** requires of `e3d`, and what it promises in
return. This is the seam between the campaign and the CFD: everything above it
is scheduling, everything below it is fluid dynamics, and neither needs to know
how the other works.

`e3d` is built from the Eddy3D CLI, a separate codebase. This file is the
specification that side implements against; nothing here is enforced by this
repository, and an `e3d` that implements none of it still works — see
[Compatibility](#compatibility).

**Contract version: 1.**

## Why E3D does not talk to the broker

It would be natural to have `e3d` lease its own work and report its own results
over HTTP. It should not, for two reasons.

The first is that the interesting parts of a worker are not about CFD at all.
Catching `SIGTERM` to release a lease and refund the attempt when Phoenix
preempts; the rule that a `409` means stop; retry budgets; backing off on a
drained queue; resuming a half-finished case after a restart — that is campaign
logic. Moving it into `e3d` means every protocol change needs a rebuilt CFD
binary redistributed to every machine, when the protocol is versioned MAJOR
precisely *because* workers are long-lived and nobody restarts them in a hurry.

The second is blast radius. `e3d` never reads `CASEBROKER_TOKEN`, and **not
reading it is the guarantee**: a compromised or misbehaving solver cannot lease,
complete, fail, or purge anything. It computes, and it writes files.

Everything `e3d` needs to tell the broker, it tells by writing a file that the
runner already reads.

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
not by how often `e3d` writes, so writing every iteration costs nothing upstream.

**Why the solver and not the log.** Without a trace the runner recovers
residuals by regex from the solver log, and has to infer the outer-iteration
boundary itself: `p` is solved several times per step
(`nCorrectors` × `nNonOrthogonalCorrectors`, about six on this campaign), so a
naive read interleaves corrector stages and makes a monotone descent look like a
two-decade oscillation. That misread cost a real detour. `e3d` knows where its
own iteration boundaries are; the regex only guesses.

## E3D shall — deployment awareness

7. **E3D shall** read `CASEBROKER_URL` and `CASEBROKER_WORKER_ID` from the
   environment when present, and stamp them into its own run metadata.
8. **E3D shall** stamp `case_id` and `worker_id` into the result directory, so
   an archive is traceable to the machine that produced it without consulting
   the broker.
9. **E3D shall not** read `CASEBROKER_TOKEN`.

## What the broker side provides

| Variable | Set by | Meaning |
| --- | --- | --- |
| `CASE_SPEC` | the worker | the case spec, also on stdin |
| `E3D_TRACE_FILE` | `run_case.sh` | where to append the trace; archived beside the result |
| `CASEBROKER_URL` | `machine.env` | which broker this machine serves |
| `CASEBROKER_WORKER_ID` | `machine.env` | this machine's stable id |

`machine.env` is written by `casebroker worker setup` — see
[First run](operations.md#first-run-from-nothing-to-a-working-broker).

## Compatibility

Every requirement here degrades rather than breaks.

- An `e3d` that writes **no trace** still reports progress: the runner falls
  back to parsing the solver log, exactly as it did before this contract
  existed. That fallback is not deprecated and is not going away.
- A **malformed or truncated** trace line falls back the same way. The trace is
  read last-line-first and parsed defensively; a partial write during a crash
  costs one heartbeat's detail, nothing more.
- `$E3D_TRACE_FILE` **unset** means the runner is older than the solver. `e3d`
  should skip the trace entirely rather than guessing a path.

The rule throughout: a progress report is decoration on a heartbeat, and must
never be able to break the solve it describes or the worker reporting it.
