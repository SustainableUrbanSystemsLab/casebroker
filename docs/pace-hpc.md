# Running a case on PACE ICE / Phoenix

For joining a machine -- PACE or not -- to the campaign as a worker (profiles,
progress, resume, archives, getting results to the master) see
[`fleet.md`](fleet.md). This page is the cluster-specific traps.

Two different jobs, don't confuse them:

- **The broker worker fleet** (`slurm/ice_worker.sbatch`, `slurm/phoenix_worker.sbatch`) —
  a pool of long-lived workers that lease cases from the casebroker queue,
  run them through `runner/run_case.sh`, and report back. This is how the
  production campaign runs. If you're draining the queue, submit these.
- **Standalone single-case offload** (`slurm/standalone_solve_template.sbatch`) —
  running ONE already-built case directly, outside the broker, e.g. to get a
  mesh-study probe onto a dedicated node instead of fighting local cores for
  it. This doc is mostly about this path, since it's the one with sharp
  edges nobody had hit before.

Both use **Podman**, not Docker — PACE has no Docker daemon. Check for it
before reaching for anything else (`which podman`); do not assume you need
to port a case to whatever `module avail openfoam` offers instead. Both
clusters were confirmed to have Podman 5.4.0 as of 2026-09.

## 0. SSH access (Windows workstations only)

If you're driving this from a Windows machine: **git-bash / MSYS `ssh` fails
publickey auth against PACE even with a correct key and correct
`~/.ssh/config`.** The exact same key, same fingerprint, authenticates fine
through the **native Windows OpenSSH client** (`C:\Windows\System32\OpenSSH\ssh.exe`).
Practically: run `ssh`/`scp` through PowerShell, not through a bash/MSYS
shell, when targeting `login-ice.pace.gatech.edu` or
`login-phoenix.pace.gatech.edu`.

This also means: **don't shell out to `powershell.exe` from inside a bash
process** (a Bash-tool call, a Monitor-tool script, a cron job running under
bash) to run these `ssh` commands — the nested `powershell.exe` does not
inherit the working `ssh-agent` access and fails the exact same way MSYS
does, but *silently* (empty output, not an error), which reads as "the job
disappeared" rather than "the check itself failed." Any polling loop for a
PACE job's status needs to be a real PowerShell process making the `ssh`
call directly, not a bash loop wrapping one.

## 1. Storage: use scratch, not `$HOME`

Both clusters give `$HOME` a small hard quota that is very likely already
near full from unrelated work:

- ICE: 30 GB (`quota -s`; the reported filesystem path is cosmetically wrong —
  it may show someone else's username — but the numeric limit is real and
  enforced)
- Phoenix: 20 GB

Neither fits a multi-GB CFD case. Use **Phoenix's `/storage/scratch1`**
instead — a 6+ PB Lustre filesystem with essentially no relevant quota.
Convention already used by the broker (`WIND_ROOT` in
`phoenix_worker.sbatch`): `/storage/scratch1/3/pkastner3/...`. ICE has no
equivalent scratch mount for this account — this is the concrete reason the
standalone-offload path targets Phoenix, not ICE.

Inside a running job, solve on **node-local scratch** (`$TMPDIR`, ~1.4 TB,
wiped at job end), not directly on the Lustre mount — OpenFOAM's
thousands-of-small-files-per-rank access pattern is the worst case for a
networked filesystem. Copy the case in at job start, copy results (and the
growing log, periodically) back out before the job ends. See the template
script for the exact pattern.

## 2. Packaging a case for transfer

A built case mixes static config with live solve state. Only tar the static
part:

```
mesh/constant/polyMesh/            # the built mesh
case_XXX/0/ case_XXX/constant/ case_XXX/system/ case_XXX/UMCfoam.foam
```

**Never** the `processor*/` directories or numbered time directories from a
case that's mid-solve elsewhere — those are actively being written and a tar
mid-write produces a case that fails in confusing ways on the other end.

```powershell
tar -C <local case dir>/box -czf mycase.tar.gz mesh/constant/polyMesh case_270/0 case_270/constant case_270/system case_270/UMCfoam.foam
scp mycase.tar.gz pkastner3@login-phoenix.pace.gatech.edu:/storage/scratch1/3/pkastner3/pace_cfd/<tag>/
```

## 3. Rootless Podman on PACE: three things every invocation needs

None of this is optional; each one reproduces a real failure if skipped.

1. **`XDG_RUNTIME_DIR` must be exported and point somewhere writable.**
   There's no systemd user session on a compute node, so podman's default
   lookup (`/run/user/<uid>`) doesn't exist:
   `Failed to obtain podman configuration: lstat /run/user/<uid>: no such file or directory`.
   Fix: `export XDG_RUNTIME_DIR=$SCRATCH/xdg; mkdir -p $XDG_RUNTIME_DIR; chmod 700 $XDG_RUNTIME_DIR`.
2. **`--root`/`--runroot` need to point at scratch too**, plus
   `--storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs` — there's no
   `/etc/subuid` range for this account, so podman falls back to a
   "rootless single mapping," which needs fuse-overlayfs rather than the
   kernel overlay driver.
3. **Run containers as `--user 0:0` with `-e HOME=/home/openfoam`.** The
   image's default user (uid 1000, "openfoam") cannot be mapped under that
   single-mapping rootless setup and fails immediately:
   `crun: setresgid to \`1000\`: Invalid argument`. Running as root (uid 0
   maps trivially to the host's own single-mapped uid) is the workaround,
   not a real privilege escalation in this context.

```bash
export XDG_RUNTIME_DIR="$SCRATCH/xdg"; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
PSTORE="$SCRATCH/pstore"; mkdir -p "$PSTORE"
POD="podman --root $PSTORE --runroot $XDG_RUNTIME_DIR/run --storage-driver overlay --storage-opt overlay.ignore_chown_errors=true --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
$POD run --rm --user 0:0 -e HOME=/home/openfoam -v "$SCRATCH:/s" "$IMG" ...
```

## 4. The `dicehub/openfoam:12` image's `ENTRYPOINT` is a trap

Its `ENTRYPOINT` is `/bin/bash -i -c`. Podman appends whatever command you
give `run` onto that. `-c` only takes **one** argument as its script — every
further word becomes an ignored positional parameter (`$0`, `$1`, ...). So:

```bash
# WRONG -- silently runs bare "bash" (interactive, no command), which exits
# instantly on closed stdin. Looks like success (exit 0, fast) and solves
# nothing. This is exactly what caused a 120-job runaway retry loop before
# anyone noticed nothing was actually being solved.
$POD run ... "$IMG" bash /s/inner.sh /s 24

# RIGHT -- the whole inner command as ONE pre-quoted string
$POD run ... "$IMG" "bash /s/inner.sh /s 24"
```

If a container run finishes suspiciously fast (under a minute) with exit 0
and nothing in the expected log file, this is the first thing to check.

## 5. Core and billing limits (measured on Phoenix, 2026-09)

- **24 cores per node is the practical ceiling for a single job**,
  regardless of a node's physical size (`cpu-gnr` nodes have 192 cores each).
  Confirmed by bisection: `-N 1 -n 24` submits fine, `-N 1 -n 28` and above
  fail immediately with `Requested node configuration is not available`
  (SLURM's message for "no node can ever satisfy this," not a queue-length
  issue — `-N 2 -n 48`, i.e. 24/node across two nodes, submits fine). This
  matches why the broker's own `*_worker.sbatch` scripts already use
  exactly `-n 24`.
- **`cpu-gnr` bills CPU-minutes at a 155x weight**
  (`TRESBillingWeights=CPU=155` — actually the *cheapest* of the CPU
  partitions checked, `cpu-small`/`medium`/`large` are all higher). A 24-core
  job at 20 h blew the `gts-pkastner3` account's inferno-QOS billing-minute
  budget (`AssocGrpBillingMinutes`) when 3 were queued at once; 8 h per job
  reliably fits. The standalone template chains itself in 8h increments
  (checkpointing via `startFrom latestTime`) rather than asking for a long
  walltime up front.
- Going past 24 cores for one case needs genuine multi-node MPI, which for
  rootless Podman means host networking coordinated across nodes via
  `srun --mpi=pmix` — real extra engineering, not attempted here. For the
  mesh sizes in this campaign (5-15M cells), 24 dedicated cores already gave
  **~7-10x** the outer-iterations/hour of a locally-contended 12-24-rank
  container, so it wasn't worth the risk.

## 6. Self-chaining and the circuit breaker

`standalone_solve_template.sbatch` resubmits itself (`sbatch --dependency=afterany:$SLURM_JOB_ID`)
until the case converges (`SIMPLE solution converged` in the log), tracking
progress by whether `12.log` grew this chunk. **Do not remove the failcount
check.** A version without it chained **120 times in under 90 minutes** when
a bug (see §7) made every chunk fail in seconds — hammering the scheduler
and very likely tripping Docker Hub's anonymous pull rate limit partway
through, which then made a *second*, unrelated symptom (`FATAL: image pull
failed`) show up too. The template caps re-chaining at 2 consecutive
no-progress chunks and leaves an explicit `FAILED` marker instead of
retrying forever.

A shared `queue.txt` next to the case directories lets one case's
`CONVERGED` finalize step start the next queued case automatically — see
the template's `finalize()` function.

## 7. Debugging a chunk that fails instantly

If a chunk exits in under a minute with no real solver log content, check
in this order — every one of these was the actual cause at some point
building this:

1. **Syntax-check the script first**: `bash -n solve.sbatch`. A `set -u`
   script with an unquoted heredoc (`<<INNER`, not `<<'INNER'`) expands
   `$VARNAME` **inside `#`-comment lines too** — bash doesn't know it's "just
   a comment" until the heredoc is already written. Writing an explanatory
   comment that happens to contain a real-looking `$SOMENAME` token you
   never define is a silent, total script abort (`SOMENAME: unbound
   variable`) with no useful stack trace. `bash -n` won't catch this either
   (it's a runtime expansion, not a syntax error) — inspect any comment
   inside an unquoted heredoc by eye.
2. **The ENTRYPOINT-quoting bug** (§4) — check for a suspiciously fast,
   suspiciously "successful" exit.
3. **The mesh symlink** — `case_270/constant/polyMesh` must point
   `../../mesh/constant/polyMesh` (two levels up from the *link's own*
   directory), not one. A one-level mistake produces the unrelated-looking
   FOAM error `Cannot find file "points" in directory "polyMesh"`.
4. **A weak "is this a resume or a fresh start" check.** Don't test just
   `-d processor0` — a partially-written or stale `processor0` from an
   earlier failed attempt passes that check and then `mpirun` fails with
   `cannot open case directory ".../processor0"` since a *fresh* copy of a
   stale/incomplete directory has no real content. Test for a real mesh
   file inside it instead: `-f processor0/constant/polyMesh/owner`.
5. **Check `decomposePar`'s own exit**, not just whether the overall script
   returned 0 — a plain sequential script (no `&&` chaining) happily runs
   `mpirun` right after a `decomposePar` that silently failed. The template
   checks for `processor0/constant/polyMesh/owner` immediately after
   `decomposePar` and fails loudly if it's missing.

If none of these are it: `srun --jobid=<id> --overlap bash -c '...'` peeks
at a *running* job's node-local state without disturbing it — useful since
`$TMPDIR` content isn't synced back until the job ends.
