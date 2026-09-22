"""The client half: lease a case, simulate it, report, repeat.

Runs anywhere that can reach the broker over HTTPS. Measured 2026-09-07, ICE
compute nodes have outbound internet (``https://api.github.com`` answered 200 in
57 ms from ``atl1-1-01-005-1-2``), so a SLURM job can talk to the broker directly
and no login-node relay is needed.

Three things here are not optional at campaign scale:

* **A heartbeat thread.** A case takes hours; a lease that cannot outlive one
  network hiccup is useless, and a lease long enough to cover the whole solve
  would strand a dead worker's case for that same duration. Renewing on a timer
  gives a short TTL and a long job at once.
* **SIGTERM -> release.** Phoenix's free ``embers`` QOS preempts after an hour.
  SLURM sends SIGTERM before SIGKILL, so the case goes straight back to the pool
  with its retry refunded instead of waiting out its TTL.
* **Refusing to finish a case whose lease is gone.** If the heartbeat ever comes
  back 409, another worker owns the case now and this one's result would be a
  duplicate write racing the real owner. It stops immediately.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable

import httpx

LeaseDict = dict[str, Any]
Runner = Callable[[LeaseDict, "Worker"], dict[str, Any]]


class LeaseLost(RuntimeError):
    """The broker says this worker no longer owns the case."""


class CredentialRefused(RuntimeError):
    """The broker rejected this worker's credential outright (401 or 403).

    Waiting does not fix a credential, so this is the one lease failure the
    run loop does not retry. main() turns it into exit code 2.
    """


class BuildRefused(RuntimeError):
    """The broker refused this worker's BUILD (426), not its credential.

    The campaign insists on a declared build, or has blocked this one. Only an
    update answers that, and this worker cannot perform one: `e3d` is a file an
    operator copies onto the box. Waiting does not change the answer either, so
    main() turns it into exit code 3 -- distinct from the credential's 2, because
    the fix is a different person's -- and says which build was refused.
    """


class Worker:
    def __init__(self, broker: str, token: str | None, worker_id: str | None = None,
                 lease_seconds: int = 900, heartbeat_seconds: int = 300,
                 timeout: float = 30.0, host: str | None = None,
                 cluster: str | None = None, progress_file: str | None = None,
                 cases_dir: str | None = None, build: str | None = None,
                 version: str | None = None, platform: str | None = None,
                 recipes: list[str] | None = None):
        self.broker = broker.rstrip("/")
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_seconds = lease_seconds
        # Where the runner drops its one-line "iter 412 p=3.2e-05 ..." summary
        # (runner/lib/progress.py). The heartbeat ships whatever is there in
        # place of "alive", so the dashboard shows where a solve actually is.
        self.progress_file = progress_file
        # Per-case checkpoints this machine keeps on its own disk (the runner's
        # $WIND_CASES). A `resume.json` in there names a case this worker was
        # mid-way through when it last stopped; it is asked for first on the
        # next lease so the solve continues instead of restarting.
        self.cases_dir = cases_dir
        # "What machine produced this case" is otherwise unanswerable once the
        # SLURM job has ended and its log has rotated out of easy reach.
        # SLURM_CLUSTER_NAME is set by the scheduler on both ICE and Phoenix, so
        # a cluster run needs no extra configuration; CASEBROKER_CLUSTER lets a
        # non-SLURM box (the lab workstation) name itself explicitly.
        self.host = host or socket.gethostname()
        self.cluster = cluster or os.environ.get("CASEBROKER_CLUSTER") \
            or os.environ.get("SLURM_CLUSTER_NAME") or None
        # WHICH CODE this is, sent with every lease: it is how the fleet table
        # says what build each machine runs and whether it is behind the
        # campaign's target. Every row read "undeclared" before this, because
        # the broker recorded a build and only the E3D node ever sent one. All
        # optional: a broker older than build identity ignores the keys, and a
        # worker that knows nothing (no EDDY3D_CLI, or an e3d too old to answer
        # `version --json`) leases exactly as it always did.
        self.build = build
        self.version = version
        self.platform = platform
        self.recipes = list(recipes) if recipes else None
        self.heartbeat_seconds = heartbeat_seconds
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.Client(base_url=self.broker, headers=headers, timeout=timeout)
        self._stop = threading.Event()
        self._current_lease: str | None = None
        # Carried alongside the lease id so `complete` can name the case it is
        # reporting. The broker uses it to scope its "was this already written?"
        # check to THIS case rather than to any case sharing a result_uri.
        self._current_case: str | None = None
        self._lease_lost = threading.Event()

    # -- transport ------------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any], retries: int = 4) -> httpx.Response:
        """POST with backoff on transport errors and 5xx.

        A 409 is NOT retried: it is a definitive answer ("you no longer own this")
        and retrying it would only delay the worker noticing.
        """
        delay = 2.0
        last: Exception | None = None
        for _ in range(retries):
            try:
                r = self.http.post(path, json=payload)
                if r.status_code < 500:
                    return r
                last = RuntimeError(f"{r.status_code} {r.text[:200]}")
            except httpx.HTTPError as e:
                last = e
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise RuntimeError(f"broker unreachable for {path}: {last}")

    # -- lease lifecycle ------------------------------------------------------

    def resume_case_ids(self) -> list[str]:
        """Cases with a checkpoint on THIS machine that this worker id left
        behind. Read from disk on every call: the runner deletes the marker when
        a case completes, so nothing here needs to track state."""
        if not self.cases_dir:
            return []
        ids: list[str] = []
        for marker in sorted(glob.glob(os.path.join(self.cases_dir, "*", "resume.json"))):
            try:
                with open(marker, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            # A checkpoint another worker id on this same box left is not ours to
            # continue -- its ranks, runtime and lease history are that worker's.
            if m.get("worker_id") == self.worker_id and m.get("case_id"):
                ids.append(m["case_id"])
        return ids[:64]

    def lease(self, count: int = 1, splits: list[str] | None = None) -> list[LeaseDict]:
        r = self._post("/v1/lease", {
            "worker_id": self.worker_id, "count": count,
            "lease_seconds": self.lease_seconds, "splits": splits,
            "host": self.host, "cluster": self.cluster,
            "resume_case_ids": self.resume_case_ids() or None,
            "build": self.build, "version": self.version, "platform": self.platform,
            "recipes": self.recipes})
        r.raise_for_status()
        return r.json()

    def progress_detail(self) -> str:
        """The runner's latest progress line, or "alive" when there is none yet.
        Best effort: a progress line is decoration on a heartbeat and must never
        be able to break one."""
        if self.progress_file:
            try:
                with open(self.progress_file, encoding="utf-8", errors="ignore") as f:
                    line = f.read().strip().splitlines()
                if line and line[-1].strip():
                    return line[-1].strip()[:400]
            except OSError:
                pass
        return "alive"

    def heartbeat(self, detail: str | None = None) -> None:
        if not self._current_lease:
            return
        r = self._post("/v1/heartbeat", {
            "lease_id": self._current_lease,
            "lease_seconds": self.lease_seconds, "detail": detail}, retries=2)
        if r.status_code == 409:
            self._lease_lost.set()
            raise LeaseLost(r.text[:200])
        r.raise_for_status()

    def complete(self, result_uri: str, sha256: str | None = None,
                 nbytes: int | None = None, metrics: dict[str, Any] | None = None) -> None:
        # A longer retry budget than anything else here, because this is the one
        # call whose failure throws away real work: the case is solved, the
        # archive is on disk, and only the broker has not been told. Losing it
        # means the lease expires and hours of CFD get recomputed on some other
        # machine. The default budget spans about 30 s, which is shorter than a
        # platform redeploy -- so rotating the database credential, which
        # restarts the service, would silently cost whichever case happened to
        # finish during the restart. Nine attempts span roughly five minutes.
        # Retrying is safe: a lease that has genuinely gone answers 409, which
        # `_post` never retries.
        r = self._post("/v1/complete", {
            "lease_id": self._current_lease, "case_id": self._current_case,
            "result_uri": result_uri,
            "sha256": sha256, "bytes": nbytes, "metrics": metrics or {}}, retries=9)
        if r.status_code == 409:
            raise LeaseLost(r.text[:200])
        r.raise_for_status()

    def fail(self, error: str, retryable: bool = True) -> None:
        self._post("/v1/fail", {"lease_id": self._current_lease,
                                "error": error, "retryable": retryable})

    def release(self, reason: str = "released") -> None:
        if self._current_lease:
            try:
                self._post("/v1/release", {"lease_id": self._current_lease,
                                           "reason": reason}, retries=2)
            except Exception as e:                       # best effort by design
                # A failed release is survivable: the lease TTL reclaims the case
                # anyway. Losing the exit path over it would not be.
                print(f"[warn] release failed ({e}); lease will expire instead",
                      file=sys.stderr)

    # -- main loop ------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            if not self._current_lease:
                continue
            try:
                self.heartbeat(self.progress_detail())
            except LeaseLost:
                print("[warn] lease lost; abandoning current case", file=sys.stderr)
                return
            except Exception as e:
                print(f"[warn] heartbeat failed: {e}", file=sys.stderr)

    def install_signal_handlers(self) -> None:
        def on_term(signum, _frame):
            name = signal.Signals(signum).name
            print(f"[info] {name} received - releasing lease and exiting", file=sys.stderr)
            self._stop.set()
            self.release(f"preempted ({name})")
            os._exit(0)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, on_term)
            except (ValueError, OSError):
                pass                                  # not on the main thread

    def run_forever(self, runner: Runner, splits: list[str] | None = None,
                    max_cases: int | None = None, idle_backoff: int = 60,
                    max_idle_polls: int = 10) -> int:
        self.install_signal_handlers()
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb.start()

        done = idle = 0
        while not self._stop.is_set():
            if max_cases is not None and done >= max_cases:
                print(f"[info] reached max_cases={max_cases}")
                break
            try:
                got = self.lease(count=1, splits=splits)
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (401, 403):
                    # A credential problem does not fix itself by waiting. The
                    # retry below would otherwise run every idle_backoff seconds
                    # for the whole SLURM walltime, holding the allocation with
                    # nothing but "[warn] lease failed: 403" in the log -- twenty
                    # of them, on an array job. Stop, say why, and let the
                    # scheduler have the nodes back.
                    self._stop.set()
                    raise CredentialRefused(
                        f"broker refused this credential ({status}): "
                        f"{e.response.text[:300]}") from e
                if status == 426:
                    # The credential is fine and the BUILD is not. The retry
                    # below would log one warning per idle_backoff for the whole
                    # walltime and never once say what was wrong with the node.
                    self._stop.set()
                    raise BuildRefused(
                        f"broker refused this worker's build "
                        f"({self.build or 'undeclared'}): {e.response.text[:300]}") from e
                print(f"[warn] lease failed: {e}", file=sys.stderr)
                time.sleep(idle_backoff)
                continue

            if not got:
                idle += 1
                if idle >= max_idle_polls:
                    print("[info] queue drained; exiting so the allocation is freed")
                    break
                time.sleep(idle_backoff)
                continue

            idle = 0
            lease = got[0]
            self._current_lease = lease["lease_id"]
            self._current_case = lease["case_id"]
            self._lease_lost.clear()
            t0 = time.time()
            # A stale line from the previous case must not be reported as this
            # one's progress on the first heartbeat.
            if self.progress_file:
                try:
                    os.remove(self.progress_file)
                except OSError:
                    pass
            print(f"[info] leased {lease['case_id']} (attempt {lease['attempt']})")
            try:
                out = runner(lease, self)
                if self._lease_lost.is_set():
                    raise LeaseLost("heartbeat reported the lease was taken")
                metrics = dict(out.get("metrics", {}))
                metrics["wall_seconds"] = round(time.time() - t0, 1)
                metrics["worker"] = self.worker_id
                # Baked into the case's own record, not just the workers table:
                # a worker row can age out of /v1/status's top-50, but a done
                # case must stay able to answer "what machine produced this"
                # forever.
                metrics["host"] = self.host
                if self.cluster:
                    metrics["cluster"] = self.cluster
                self.complete(out["result_uri"], out.get("sha256"),
                              out.get("bytes"), metrics)
                done += 1
                print(f"[info] completed {lease['case_id']} in {metrics['wall_seconds']}s")
            except LeaseLost as e:
                print(f"[warn] lease lost on {lease['case_id']}: {e}", file=sys.stderr)
            except Exception as e:
                if getattr(e, "unfit", False):
                    # Nothing to do with the case. Releasing refunds the attempt --
                    # the treatment preemption already gets -- and stopping is what
                    # keeps one dead daemon from walking the whole queue.
                    print(f"[error] this machine cannot run cases: {e}", file=sys.stderr)
                    print(f"[info] releasing {lease['case_id']}; its attempt is refunded",
                          file=sys.stderr)
                    try:
                        self.release("node cannot run cases")
                    except Exception as e2:
                        print(f"[warn] could not release: {e2}", file=sys.stderr)
                    self._current_lease = None
                    self._current_case = None
                    break
                # retryable unless the runner explicitly says the case itself is
                # broken -- a bad STL will fail identically on every machine, and
                # cycling it through the fleet three times helps nobody.
                retryable = not getattr(e, "fatal", False)
                print(f"[error] {lease['case_id']}: {e}", file=sys.stderr)
                try:
                    self.fail(str(e)[:2000], retryable=retryable)
                except Exception as e2:
                    print(f"[warn] could not report failure: {e2}", file=sys.stderr)
            finally:
                self._current_lease = None
                self._current_case = None

        self._stop.set()
        return done


# -- runners ------------------------------------------------------------------

class FatalCaseError(RuntimeError):
    """Raised by a runner when the case can never succeed anywhere."""
    fatal = True


class NodeUnfitError(RuntimeError):
    """This MACHINE cannot run anything -- no runtime, a stopped daemon.

    The opposite report from every other failure: the case is released rather
    than failed, which REFUNDS the attempt, and the worker stops. A node that
    kept leasing would charge every case in the queue for one local problem, and
    three of those quarantine a site that nothing is wrong with -- which is how
    COD-PKAST-7865 turned a stopped Docker Desktop into seven failed cases on
    2026-09-19.
    """
    unfit = True


def echo_runner(lease: LeaseDict, worker: Worker) -> dict[str, Any]:
    """Test runner: pretends to simulate. Used by the integration test and to
    smoke a new deployment without burning CFD time."""
    time.sleep(float(os.environ.get("CASEBROKER_FAKE_SECONDS", "0.05")))
    return {"result_uri": f"memory://{lease['case_id']}", "bytes": 0,
            "metrics": {"fake": True}}


# How long one case may run before the worker gives up on it, and how much of
# its output is kept.
#
# Neither had a bound. `subprocess.run(capture_output=True)` with no timeout
# meant a hung solve ran forever -- and the heartbeat thread is INDEPENDENT of
# the runner, so it kept renewing the lease the whole time. The case was
# therefore never reclaimed, the worker never moved on, and the allocation
# burned to walltime with nothing to show. It self-corrected only when SLURM
# killed the job.
#
# And capture_output buffers everything in memory. An OpenFOAM solve is verbose
# -- thousands of files per rank per step, and a run.log to match -- while the
# code here needs exactly two things from it: the last stdout line, and a tail
# of stderr for the error message. Keeping a bounded deque gives both for a
# fixed cost instead of holding the whole log in the worker's RAM.
CASE_TIMEOUT_SECONDS = int(os.environ.get("CASEBROKER_CASE_TIMEOUT", str(24 * 3600)))
CAPTURE_LINES = int(os.environ.get("CASEBROKER_CAPTURE_LINES", "2000"))


def _drain(stream, sink) -> None:
    """Copy a pipe into a bounded deque, so a chatty runner cannot grow the heap."""
    try:
        for line in stream:
            sink.append(line.rstrip("\n"))
    except Exception:                                    # noqa: BLE001
        pass                                             # the pipe closed under us
    finally:
        try:
            stream.close()
        except Exception:                                # noqa: BLE001
            pass


def _terminate_tree(proc) -> None:
    """Kill the runner and everything it started.

    A solve is `mpirun` and its ranks, not one process, so killing only the
    direct child leaves the ranks running -- still holding the cores this worker
    is about to ask for again. On POSIX the runner gets its own session
    (`start_new_session`) so the whole group can be signalled at once.
    """
    try:
        if os.name == "nt":
            proc.kill()
        else:
            import signal as _signal
            os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:                                # noqa: BLE001
            pass


def script_runner(script: str, timeout: int | None = None,
                  capture_lines: int | None = None) -> Runner:
    """Run an external script per case.

    The case spec arrives as JSON on stdin and in ``$CASE_SPEC``; the script must
    print a JSON object with at least ``result_uri`` as its LAST line of stdout.
    This is the seam where ``eddy3d-cli build-case`` + mesh + solve + sample
    plugs in, so the broker never needs to know what OpenFOAM is.

    Bounded in both directions: a case that outruns ``timeout`` is killed (with
    its whole process group) and reported as a retryable failure, and only the
    last ``capture_lines`` lines of each stream are kept.
    """
    import collections

    limit = CASE_TIMEOUT_SECONDS if timeout is None else timeout
    keep = CAPTURE_LINES if capture_lines is None else capture_lines

    def run(lease: LeaseDict, worker: Worker) -> dict[str, Any]:
        env = dict(os.environ)
        env["CASE_ID"] = lease["case_id"]
        env["CASE_SPEC"] = json.dumps(lease["spec"])
        env["LEASE_ID"] = lease["lease_id"]
        # What the runner needs to talk BACK: where to write progress for the
        # heartbeat, where to keep a checkpoint, and which worker it belongs to
        # (so a resume marker is only honoured by the worker that wrote it).
        if worker is not None:
            if worker.progress_file:
                env["CASEBROKER_PROGRESS_FILE"] = worker.progress_file
            if worker.cases_dir:
                env["WIND_CASES"] = worker.cases_dir
            env["CASEBROKER_WORKER_ID"] = worker.worker_id
        # POSIX: run via `bash <script>` rather than exec'ing the file directly --
        # the latter depends on the git executable bit surviving checkout, which a
        # script authored on Windows is not guaranteed to carry (found on Phoenix:
        # PermissionError on a freshly cloned run_case.sh). Windows has no such bit
        # (and a bash on PATH there could not run a .cmd launcher anyway), so it
        # keeps exec'ing the script by path.
        argv = [script] if os.name == "nt" else ["bash", script]
        out: "collections.deque[str]" = collections.deque(maxlen=keep)
        err_lines: "collections.deque[str]" = collections.deque(maxlen=keep)
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, shell=False,
            # Its own process group, so a timeout can signal the ranks too.
            start_new_session=(os.name != "nt"))
        pumps = [threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
                 threading.Thread(target=_drain, args=(proc.stderr, err_lines), daemon=True)]
        for t in pumps:
            t.start()
        try:
            proc.stdin.write(json.dumps(lease))
        except (BrokenPipeError, OSError):
            pass            # a runner is entitled to ignore stdin
        finally:
            try:
                proc.stdin.close()
            except Exception:                            # noqa: BLE001
                pass
        try:
            proc.wait(timeout=limit if limit > 0 else None)
        except subprocess.TimeoutExpired:
            _terminate_tree(proc)
            for t in pumps:
                t.join(timeout=10)
            raise RuntimeError(
                f"runner exceeded {limit}s and was killed; last stderr: "
                + " | ".join(list(err_lines)[-5:])[:1000])
        for t in pumps:
            t.join(timeout=30)

        if proc.returncode != 0:
            tail = "\n".join(err_lines or out)[-2000:]
            # 64 is the campaign's agreed "this case is broken, do not retry"
            # code, so a bad tile is quarantined on its first attempt.
            # 69 is sysexits' EX_UNAVAILABLE, and the runner uses it for "this
            # machine cannot run cases" -- the mirror of 64's "this case cannot
            # be run anywhere". The two are deliberately the same shape: one
            # blames the case forever, the other blames the node and costs the
            # case nothing.
            err = (FatalCaseError if proc.returncode == 64
                   else NodeUnfitError if proc.returncode == 69
                   else RuntimeError)
            raise err(f"runner exited {proc.returncode}: {tail}")
        lines = [ln for ln in out if ln.strip()]
        if not lines:
            raise RuntimeError("runner produced no output; expected a JSON result line")
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as e:
            raise RuntimeError(f"last stdout line is not JSON ({e}): {lines[-1][:200]}")
    return run


# -- identity -----------------------------------------------------------------

def msys_to_windows(path: str) -> str:
    """``/e/wind/bin/e3d.exe`` -> ``E:/wind/bin/e3d.exe``.

    machine.env spells EDDY3D_CLI the way run_case.sh wants it, as an MSYS path,
    because the runner is a bash script. This worker is native Python, for
    which that spelling names nothing. Anything else comes back unchanged.
    """
    m = re.match(r"^/([A-Za-z])/(.*)$", path)
    return f"{m.group(1).upper()}:/{m.group(2)}" if m else path


def e3d_identity(cli: str | None, timeout: float = 30.0) -> dict[str, Any] | None:
    """What `e3d` says it is -- ``build``, ``version``, ``platform``,
    ``recipes`` from ``e3d version --json`` -- or None when there is nothing to
    declare.

    None is the backwards-compatible answer, never an error: no EDDY3D_CLI, an
    e3d from before ``--json`` (it prints a bare ``1.14.0`` and ignores the
    flag), a file that will not start. The worker then leases as it always did
    and the fleet table shows it as undeclared, which is the truth.
    """
    if not cli:
        return None
    exe = cli
    if os.name == "nt" and not os.path.exists(exe):
        exe = msys_to_windows(exe)
    try:
        proc = subprocess.run([exe, "version", "--json"], capture_output=True,
                              text=True, timeout=timeout)
        # The LAST line: a build that logs before it answers has still answered.
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        got = json.loads(lines[-1]) if lines else {}
        build = got.get("build") if isinstance(got, dict) else None
        if proc.returncode != 0 or not isinstance(build, str) or not build:
            raise ValueError(f"exit {proc.returncode}, stdout {proc.stdout[-200:]!r}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"[warn] {cli} did not answer `version --json` ({e}); "
              "this worker declares no build", file=sys.stderr)
        return None
    recipes = got.get("recipes")
    return {
        "build": build[:96],
        "version": str(got.get("version") or "")[:64] or None,
        "platform": str(got.get("platform") or "")[:32] or None,
        "recipes": [str(r) for r in recipes][:64] if isinstance(recipes, list) else None,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Case-broker worker")
    p.add_argument("--broker", default=os.environ.get("CASEBROKER_URL", "http://127.0.0.1:8000"))
    p.add_argument("--token", default=os.environ.get("CASEBROKER_TOKEN"))
    p.add_argument("--worker-id", default=os.environ.get("CASEBROKER_WORKER_ID"))
    p.add_argument("--runner", help="path to a per-case script; omit for the echo test runner")
    p.add_argument("--case-timeout", type=int, default=CASE_TIMEOUT_SECONDS,
                   help="seconds before a case is killed and reported as a retryable "
                        "failure; 0 disables. A hung solve otherwise heartbeats "
                        "forever and burns the allocation to walltime.")
    p.add_argument("--capture-lines", type=int, default=CAPTURE_LINES,
                   help="how many lines of the runner's stdout/stderr to keep in memory")
    p.add_argument("--splits", nargs="*", default=None)
    p.add_argument("--max-cases", type=int, default=None)
    p.add_argument("--lease-seconds", type=int, default=900)
    p.add_argument("--heartbeat-seconds", type=int, default=300)
    p.add_argument("--idle-backoff", type=int, default=60)
    p.add_argument("--progress-file", default=os.environ.get("CASEBROKER_PROGRESS_FILE"),
                   help="where the runner writes its progress line for the heartbeat "
                        "(default: a per-worker file in the temp dir)")
    p.add_argument("--cases-dir", default=os.environ.get("WIND_CASES"),
                   help="local checkpoint directory; cases with a resume.json here are "
                        "asked for first so they continue instead of restarting")
    p.add_argument("--e3d", default=os.environ.get("EDDY3D_CLI"),
                   help="the e3d executable the runner drives, asked `version --json` once "
                        "so every lease says which build this machine runs (default: "
                        "$EDDY3D_CLI; unset, or an e3d too old to answer, declares none)")
    p.add_argument("--recipes", default=os.environ.get("CASEBROKER_RECIPES"),
                   help="comma-separated recipes this runner produces EXACTLY; the broker "
                        "then hands it only those. Omitted, it may be handed any: the "
                        "runner script is its own contract, and nothing here can vouch "
                        "for it (default: $CASEBROKER_RECIPES)")
    a = p.parse_args(argv)

    ident = e3d_identity(a.e3d) or {}
    recipes = [r.strip() for r in (a.recipes or "").split(",") if r.strip()] or None
    w = Worker(a.broker, a.token, a.worker_id, a.lease_seconds, a.heartbeat_seconds,
               cases_dir=a.cases_dir, build=ident.get("build"), version=ident.get("version"),
               platform=ident.get("platform"), recipes=recipes)
    if w.build:
        print(f"[info] build {w.build} ({w.platform or 'platform unknown'}); "
              + (f"recipes {', '.join(recipes)}" if recipes
                 else "no recipes declared, so any case may be handed to this worker"))
    else:
        print("[info] no build declared (set EDDY3D_CLI to an e3d that answers "
              "`version --json`); the fleet table will show this worker as undeclared")
    w.progress_file = a.progress_file or os.path.join(
        tempfile.gettempdir(), f"casebroker-progress-{w.worker_id}.txt")
    runner = (script_runner(a.runner, timeout=a.case_timeout,
                            capture_lines=a.capture_lines)
              if a.runner else echo_runner)
    try:
        n = w.run_forever(runner, splits=a.splits, max_cases=a.max_cases,
                          idle_backoff=a.idle_backoff)
    except CredentialRefused as e:
        # Non-zero on purpose: it is what makes an sbatch log end with
        # "exit=2" and the sentence that explains it, instead of the job
        # holding its nodes to the wall clock.
        print(f"[fatal] {e}", file=sys.stderr)
        return 2
    except BuildRefused as e:
        print(f"[fatal] {e}", file=sys.stderr)
        print("[fatal] update e3d on this machine (and EDDY3D_CLI, if it moved), "
              "then start the worker again", file=sys.stderr)
        return 3
    print(f"[info] worker {w.worker_id} finished {n} case(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
