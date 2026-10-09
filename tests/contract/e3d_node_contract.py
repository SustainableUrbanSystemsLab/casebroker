#!/usr/bin/env python3
"""Cross-repo contract test: a REAL casebroker against a REAL E3D simulation node.

    uv run --project <casebroker> python tests/contract/e3d_node_contract.py --e3d /path/to/E3D [--keep]

Each side has its own tests against a fake of the other, and a fake agrees with whatever its
author believed. This puts the two programs in one room: a broker started from this checkout
(uvicorn, SQLite, a temp dir, no env tokens), the first admin created through the API, the node
paired FOR REAL (``E3D setup-sim-node``, the device code approved through the admin API), one
case seeded, and ``E3D run-sim-node --max-cases 1`` solving it with a script runner
(``e3d_fake_runner.py`` beside this file) that reports progress and telemetry and writes a
result. Then it asserts what the protocol promises (docs/protocol.md; Eddy3D
docs/SIMULATION_NODE.md):

* the case is ``done``;
* the worker row names the node's build and platform -- and, from a broker that lists its
  features (protocol 2), the node's declared ``features``; its ``cpus`` and ``mem_gb`` when the
  broker lists ``hardware``;
* the heartbeat's progress reached the case's stage trail -- and, when the broker lists
  ``heartbeat_stage``, carried the NODE's stage: the fake runner says ``continuing: ...``, a line
  only the node can stage (``resume``);
* telemetry is stored on the case (``site``, ``mesh``), and the residual series when the broker
  lists ``residuals``.

Assertions on the protocol-2 fields are made only when ``/healthz`` lists the matching feature,
so the same driver passes against a broker from before them. Run by casebroker CI (with
``E3D-linux-x64`` from Eddy3D's ``e3d-node-latest`` release) and by Eddy3D CI (with the binary it
just built, against casebroker ``main``). Needs Python 3.11+, ``httpx`` (a casebroker
dependency), bash, and a container engine on the machine: ``run-sim-node`` checks for an
OpenFOAM engine even when a script does the work.

Exit 0 when everything holds; 1 with the broker's and the node's logs printed when anything
does not; 2 for a usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
FAKE_RUNNER = HERE / "e3d_fake_runner.py"
#: The stages a broker knows (casebroker stages.STAGES).
STAGES = {"geometry", "build-case", "resume", "mesh", "solve", "gate", "scene", "trace", "surface", "archive"}
#: Somewhere the building atlas publishes a tile for: POST /v1/cases drops sites off land.
SITE = {"lat": 33.749, "lon": -84.388}
RECIPE = "contract/script-runner"
WORKER = "contract"
CPUS = 2


class ContractFailure(Exception):
    """One promise the protocol makes that did not hold."""


class Logged:
    """A child process whose output goes to a log file and is also kept in memory, line by line,
    for whoever needs to read something off it (the pairing code)."""

    def __init__(self, name: str, args: list[str], env: dict[str, str], cwd: Path, log: Path):
        self.name, self.log = name, log
        self.lines: list[str] = []
        self._lock = threading.Lock()
        self._file = log.open("w", encoding="utf-8", errors="replace")
        self._file.write("$ " + " ".join(args) + "\n")
        self._file.flush()
        self.proc = subprocess.Popen(args, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._reader = threading.Thread(target=self._read, name=f"{name}-log", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._lock:
                self.lines.append(line.rstrip("\n"))
            self._file.write(line)
            self._file.flush()

    def find(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        with self._lock:
            for line in self.lines:
                if m := pattern.search(line):
                    return m
        return None

    def wait(self, timeout: float) -> int:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            raise ContractFailure(f"{self.name} did not finish within {timeout:.0f} s") from None
        self._reader.join(timeout=10)
        return code

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        self._reader.join(timeout=10)
        self._file.close()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def say(line: str = "") -> None:
    print(line, flush=True)


class Checks:
    """What held, what did not, and what was not asked of this broker."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def ok(self, what: str, detail: str = "") -> None:
        self.rows.append(("PASS", what, detail))
        say(f"  PASS  {what}" + (f"  ({detail})" if detail else ""))

    def skip(self, what: str, why: str) -> None:
        self.rows.append(("SKIP", what, why))
        say(f"  SKIP  {what}  ({why})")

    def require(self, cond: bool, what: str, detail: str = "") -> None:
        if not cond:
            self.rows.append(("FAIL", what, detail))
            raise ContractFailure(f"{what}" + (f": {detail}" if detail else ""))
        self.ok(what, detail)


def node_environment(tmp: Path) -> dict[str, str]:
    """The node's world, all under the temp dir: its credential, its config, its scratch, its
    temp files. Nothing of the machine's own node (or of a SLURM job around this) leaks in."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLURM_", "PBS_", "E3D_", "EDDY3D_", "CASEBROKER_"))}
    home = tmp / "home"
    for sub in ("", ".config", ".local/share", ".cache", ".local/state", "AppData/Local", "AppData/Roaming"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    # The container engine's own client configuration, LINKED through (never copied, never
    # written): run-sim-node probes for an OpenFOAM engine before it leases, even when a script
    # does the work, and podman on macOS finds its VM only through these. The node's own state --
    # credential, scratch, config -- stays under the temp dir.
    real_home = Path(os.path.expanduser("~"))
    for rel in (".docker", ".config/containers", ".local/share/containers"):
        source, link = real_home / rel, home / rel
        if source.exists() and not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(source, target_is_directory=True)
    (tmp / "tmp").mkdir(exist_ok=True)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local/state"),
        "LOCALAPPDATA": str(home / "AppData/Local"), "APPDATA": str(home / "AppData/Roaming"),
        "EDDY3D_NODE_DIR": str(tmp / "node"),
        "TMPDIR": str(tmp / "tmp"), "TEMP": str(tmp / "tmp"), "TMP": str(tmp / "tmp"),
        # A 2 s heartbeat, so a run of a minute leaves a progress trail. The environment rather
        # than --heartbeat: a node from before the option ignores the variable instead of refusing
        # the command line (and its trail is then only the first beat -- said below).
        "E3D_HEARTBEAT_SECONDS": "2",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    })
    return env


def broker_environment(tmp: Path) -> dict[str, str]:
    """No env tokens and no setup token: the from-scratch path, secured by the first account."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CASEBROKER_")}
    env["CASEBROKER_DB"] = str(tmp / "c.sqlite")
    # The broker is imported from the interpreter running this file (uv run --project <casebroker>).
    import casebroker  # noqa: PLC0415 -- only to find where it lives
    root = str(Path(casebroker.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def wait_for_broker(url: str, broker: Logged, timeout: float = 90) -> dict:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if broker.proc.poll() is not None:
            raise ContractFailure(f"the broker exited {broker.proc.returncode} before it answered")
        try:
            r = httpx.get(url + "/healthz", timeout=2)
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise ContractFailure(f"the broker did not answer /healthz within {timeout:.0f} s")


def as_list(value) -> list | None:
    """A list the broker may keep as JSON text in a column."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def run(args: argparse.Namespace, tmp: Path, checks: Checks, logs: list[Logged]) -> None:
    e3d = str(Path(args.e3d).resolve())
    node_env = node_environment(tmp)
    port = free_port()
    url = f"http://127.0.0.1:{port}"

    # -- the node, before anything: what it is, and whether it knows the newer options -------
    version = subprocess.run([e3d, "--version", "--json"], env=node_env, capture_output=True, text=True, timeout=120)
    if version.returncode != 0:
        raise ContractFailure(f"{e3d} --version --json exited {version.returncode}: {version.stderr.strip()[-500:]}")
    identity = json.loads([l for l in version.stdout.splitlines() if l.strip().startswith("{")][-1])
    help_text = subprocess.run([e3d, "run-sim-node", "--help"], env=node_env, capture_output=True, text=True, timeout=120).stdout
    node_v2 = "--heartbeat" in help_text
    say(f"node:   {identity.get('build')} ({identity.get('platform')})" +
        ("" if node_v2 else "  -- a node from before protocol 2: its new fields are not asked for"))

    # -- the broker ------------------------------------------------------------------------
    broker = Logged("broker", [sys.executable, "-m", "uvicorn", args.app, "--host", "127.0.0.1",
                               "--port", str(port), "--log-level", "info"],
                    broker_environment(tmp), tmp, tmp / "broker.log")
    logs.append(broker)
    healthz = wait_for_broker(url, broker)
    protocol = healthz.get("protocol")
    features = healthz.get("features")
    listed = set(features) if isinstance(features, list) else None
    say(f"broker: {url}  version {healthz.get('version')}  " +
        (f"protocol {protocol}, features: {', '.join(sorted(listed))}" if listed is not None
         else "no protocol/features in /healthz (a broker from before protocol 2)"))

    def lists(feature: str) -> bool:
        return listed is not None and feature in listed

    http = httpx.Client(base_url=url, timeout=30)
    password = secrets.token_urlsafe(18)
    r = http.post("/v1/auth/setup", json={"username": "contract-admin", "password": password})
    checks.require(r.status_code == 200, "the first admin is created", f"HTTP {r.status_code} {r.text[:200]}")
    r = http.post("/v1/auth/login", json={"username": "contract-admin", "password": password})
    checks.require(r.status_code == 200 and "wsb_session" in http.cookies, "the admin logs in (session cookie)",
                   f"HTTP {r.status_code}")

    # -- pairing, for real ---------------------------------------------------------------------
    setup = Logged("setup-sim-node", [e3d, "setup-sim-node", url, "--name", WORKER, "--no-browser", "--no-diagnose",
                                      "--timeout", "180"], node_env, tmp, tmp / "setup.log")
    logs.append(setup)
    code_line = re.compile(r"Code:\s+(\S+)")
    until = time.monotonic() + 120
    match = None
    while time.monotonic() < until and (match := setup.find(code_line)) is None:
        if setup.proc.poll() is not None:
            break
        time.sleep(0.2)
    if match is None:
        setup.stop()
        raise ContractFailure("setup-sim-node printed no pairing code")
    code = match.group(1)
    r = http.post(f"/v1/pair/{code}/approve")
    checks.require(r.status_code == 200, "the admin approves the node's device code", f"code {code}, HTTP {r.status_code} {r.text[:200]}")
    exit_code = setup.wait(timeout=120)
    checks.require(exit_code == 0, "setup-sim-node pairs the node", f"exit {exit_code}")

    # -- one case --------------------------------------------------------------------------------
    r = http.post("/v1/cases", json=[{**SITE, "recipe": RECIPE, "city_cluster": "contract-atlanta",
                                      "priority": 100, "max_attempts": 1, "labels": {"campaign": "contract"}}])
    checks.require(r.status_code == 200 and r.json().get("rejected_not_on_land", 0) == 0,
                   "one case is seeded", f"HTTP {r.status_code} {r.text[:200]}")
    cases = http.get("/v1/cases", params={"recipe": RECIPE, "include_spec": "false"}).json()
    rows = cases.get("cases") or cases.get("rows") or cases.get("items") or []
    case_id = next((c["case_id"] for c in rows if c.get("recipe") == RECIPE), None)
    if case_id is None:
        raise ContractFailure(f"the seeded case is not in the case list: {json.dumps(cases)[:300]}")
    say(f"case:   {case_id}")

    # -- the node solves it ----------------------------------------------------------------------
    runner = tmp / "runner.sh"
    runner.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "{FAKE_RUNNER}" "$@"\n', encoding="utf-8")
    runner.chmod(0o755)
    run_env = {**node_env, "CONTRACT_HOLD": str(args.hold), "CONTRACT_OUT": str(tmp / "results")}
    node = Logged("run-sim-node", [e3d, "run-sim-node", "--runner", str(runner), "--max-cases", "1",
                                   "--drain", "--max-idle-polls", "1", "--cpus", str(CPUS), "--worker-id", WORKER,
                                   "--min-free-gb", "0", "--skip-mpi-check", "--no-live",
                                   "--work", str(tmp / "work")],
                  run_env, tmp, tmp / "node.log")
    logs.append(node)
    exit_code = node.wait(timeout=args.timeout)
    checks.require(exit_code == 0, "run-sim-node leases, runs and completes the case, then exits 0", f"exit {exit_code}")

    # -- what the broker now knows -----------------------------------------------------------
    case = http.get(f"/v1/cases/{case_id}").json()
    checks.require(case.get("state") == "done", "the case is done", f"state {case.get('state')!r}")

    status = http.get("/v1/status").json()
    worker = next((w for w in status.get("workers", []) if w.get("worker_id") == WORKER), None)
    checks.require(worker is not None, "the node has a worker row", f"workers: {[w.get('worker_id') for w in status.get('workers', [])]}")
    checks.require(bool(worker.get("build")) and worker.get("build") == identity.get("build"),
                   "the worker row names the node's build", f"{worker.get('build')!r}")
    checks.require(bool(worker.get("platform")) and worker.get("platform") == identity.get("platform"),
                   "the worker row names the node's platform", f"{worker.get('platform')!r}")

    if listed is None:
        checks.skip("the worker row holds the node's features", "the broker names no features")
    elif not node_v2:
        checks.skip("the worker row holds the node's features", "a node from before protocol 2")
    else:
        declared = as_list(worker.get("features")) or []
        checks.require({"telemetry", "heartbeat_stage"} <= set(declared), "the worker row holds the node's features",
                       f"{declared}")

    if not lists("hardware"):
        checks.skip("the worker row holds cpus and mem_gb", "the broker does not list hardware")
    elif not node_v2:
        checks.skip("the worker row holds cpus and mem_gb", "a node from before protocol 2")
    else:
        cpus, mem = worker.get("cpus"), worker.get("mem_gb")
        checks.require(cpus == CPUS, "the worker row holds the node's cpus", f"{cpus!r}")
        checks.require(isinstance(mem, (int, float)) and 0.25 <= mem <= 65536, "the worker row holds the node's mem_gb", f"{mem!r}")

    segments = [s.get("stage") for s in case.get("stages") or []]
    if not node_v2:
        checks.skip("the heartbeat's progress reached the case's stage trail",
                    "a node from before protocol 2 beats every 5 min: only its first, empty beat lands in a short run")
    else:
        checks.require({"geometry", "mesh", "solve"} <= set(segments),
                       "the heartbeat's progress reached the case's stage trail", f"stages {segments}")
        if not lists("heartbeat_stage"):
            checks.skip("progress events carry the node's stage", "the broker does not list heartbeat_stage")
        else:
            checks.require("resume" in segments, "progress events carry the node's stage",
                           f"stages {segments}: 'continuing: ...' is staged 'resume' by the node alone")
            stored = stages_in_events(tmp / "c.sqlite", case_id)
            if stored is None:
                say("        (the events table has no stage column to look at; the stage trail above is the evidence)")
            else:
                checks.require(bool(stored) and set(stored) <= STAGES, "the stored progress events carry known stages",
                               f"{stored}")

    telemetry = case.get("telemetry") or {}
    mesh = telemetry.get("mesh") or {}
    mesh_data = mesh.get("data", mesh) if isinstance(mesh, dict) else {}
    checks.require("site" in telemetry and mesh_data.get("total_cells") == 12345, "telemetry is stored on the case",
                   f"kinds {sorted(telemetry)}")
    if not lists("residuals"):
        checks.skip("the residual series is stored per direction", "the broker does not list residuals")
    else:
        res = http.get(f"/v1/cases/{case_id}/residuals").json()
        directions = [d.get("direction") for d in res.get("directions", [])]
        checks.require("case_000" in directions, "the residual series is stored per direction", f"{directions}")


def stages_in_events(db: Path, case_id: str) -> list[str] | None:
    """The stages the broker stored with the case's progress events, or None when its events table
    keeps none (a broker from before heartbeat_stage, or one that keeps it elsewhere)."""
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "stage" not in columns:
                return None
            return [r[0] for r in conn.execute(
                "SELECT stage FROM events WHERE case_id = ? AND event = 'progress' AND stage IS NOT NULL ORDER BY id",
                (case_id,))]
    except sqlite3.Error:
        return None


def dump(logs: list[Logged]) -> None:
    for log in logs:
        say(f"\n----- {log.name} ({log.log}) -----")
        try:
            text = log.log.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            say(f"(could not read: {e})")
            continue
        lines = text.splitlines()
        if len(lines) > 400:
            say(f"[... {len(lines) - 400} earlier lines ...]")
        say("\n".join(lines[-400:]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--e3d", required=True, help="the E3D executable to test (a published single file)")
    parser.add_argument("--keep", action="store_true", help="keep the temp dir (database, logs, node dirs)")
    parser.add_argument("--timeout", type=float, default=300, help="seconds run-sim-node may take (default 300)")
    parser.add_argument("--hold", type=float, default=5, help="seconds the fake runner holds each line (default 5)")
    parser.add_argument("--app", default="casebroker.app:app",
                        help="the ASGI app uvicorn serves (default casebroker.app:app; another one wraps it)")
    args = parser.parse_args(argv)
    if not Path(args.e3d).is_file():
        parser.error(f"--e3d: no such file: {args.e3d}")
    if shutil.which("bash") is None:
        parser.error("bash is needed to start the fake runner")

    tmp = Path(tempfile.mkdtemp(prefix="e3d-contract-"))
    say(f"work:   {tmp}")
    checks = Checks()
    logs: list[Logged] = []
    failed: str | None = None
    try:
        run(args, tmp, checks, logs)
    except ContractFailure as e:
        failed = str(e)
    except Exception as e:  # noqa: BLE001 -- anything unexpected is a failure, with the logs
        failed = f"{type(e).__name__}: {e}"
    finally:
        for log in logs:
            log.stop()

    passed = sum(1 for r in checks.rows if r[0] == "PASS")
    skipped = sum(1 for r in checks.rows if r[0] == "SKIP")
    say("")
    if failed:
        say(f"FAIL  {failed}")
        dump(logs)
        say(f"\ncontract: FAILED after {passed} check(s) passed, {skipped} skipped. Work dir: {tmp}")
        return 1
    say(f"contract: OK -- {passed} check(s) passed, {skipped} not asked of this broker/node")
    if args.keep:
        say(f"kept: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
