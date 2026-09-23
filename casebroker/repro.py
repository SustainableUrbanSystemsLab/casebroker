"""Reproduce one case on this machine, and read what its logs say about why it failed.

`casebroker repro <case_id>` re-runs a production case through the whole node
pipeline here: its own spec from the broker, a throwaway broker on SQLite that
holds only that case, a node pointed at it through its own credential directory,
one case, then `triage`. `casebroker triage <study_dir>` reads the evidence a
failed case leaves on disk and names the known failure signatures.

Both exist because of `v2-00697fb4542aa4c6` (2026-09-22). It was quarantined
3/3 with a floating-point trap on every rung and a mesh warning of skewness
13.98; two fixes went where that text pointed -- the skewed mesh, then
potentialFoam -- and neither was the cause. Reproduced here by hand, it failed
identically, and the evidence was on disk the whole time: checkMesh had starred
`*Number of regions: 69` (68 single cells sealed inside building voids, each a
zero diagonal) and both backtraces ran through
`DICPreconditioner::calcReciprocalD`. The same session found a snappyHexMesh
that had died mid-snap passing as a finished mesh. Each of those is a check
below, so the next failure is read by code rather than by whoever remembers.

Standard library only, like the rest of the CLI: `repro` launches the broker
with this interpreter's uvicorn and the node as a subprocess.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --- triage: the signatures ----------------------------------------------------

_REGIONS = re.compile(r"Number of regions:\s*(\d+)")
_SKEW = re.compile(r"Max skewness = ([0-9.eE+-]+)")
_ORTHO = re.compile(r"non-orthogonality Max: ([0-9.eE+-]+)")
_TIME = re.compile(r"^Time = ", re.M)
_CONTINUITY = re.compile(r"continuity errors : sum local = ([0-9.eE+-]+)")


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _last(rx: re.Pattern, text: str) -> str | None:
    found = rx.findall(text)
    return found[-1] if found else None


def triage(study: str | os.PathLike) -> list[str]:
    """What a failed study's logs say, most decisive first. Lines starting
    ``info:`` are context (the mesh's numbers), not a signature; a list with no
    other line means nothing known was found -- worth saying, not a clean bill.

    Every finding names the evidence it rests on, so it can be checked by eye.
    """
    root = pathlib.Path(study)
    out: list[str] = []

    for mesh in sorted(p for p in root.glob("mesh*") if p.is_dir()):
        snappy = _read(mesh / "03_snappyHexMesh.txt")
        if snappy and "Finished meshing" not in snappy:
            out.append(
                f"{mesh.name}: snappyHexMesh never printed 'Finished meshing' "
                f"(03_snappyHexMesh.txt). The mesh on disk is whatever the ranks last "
                f"wrote -- possibly the castellated, unsnapped one -- and anything "
                f"solved or measured on it says nothing about the site.")
        check = _read(mesh / "06_checkMesh.txt")
        regions = _last(_REGIONS, check)
        if regions and int(regions) > 1:
            out.append(
                f"{mesh.name}: {regions} disconnected mesh regions (06_checkMesh.txt; "
                f"checkMesh does not fail a mesh for this). Any region without an inlet "
                f"or outlet has a singular pressure equation, and a single sealed cell has "
                f"a zero diagonal: every pressure solve dies on it, on every rung. Eddy3D "
                f"8b416080+ removes them after snappy (03b_keepFlowRegion).")
        skew, ortho = _last(_SKEW, check), _last(_ORTHO, check)
        if check and (skew or ortho):
            out.append(f"info: {mesh.name}: checkMesh max skewness {skew or '?'}, "
                       f"max non-orthogonality {ortho or '?'} "
                       f"({'passed' if 'Mesh OK.' in check else 'FAILED'}). Compare with "
                       f"the production mesh before reasoning from a difference.")

    for case in sorted(p for p in root.glob("case_*") if p.is_dir()):
        pf = _read(case / "run_00b_potentialFoam.txt")
        logs = {p.name: _read(p) for p in sorted(case.glob("run_01*.txt"))}
        zero_pivot = [n for n, t in {"run_00b_potentialFoam.txt": pf, **logs}.items()
                      if "calcReciprocalD" in t]
        if zero_pivot:
            out.append(
                f"{case.name}: floating-point trap inside DICPreconditioner::"
                f"calcReciprocalD ({', '.join(zero_pivot)}) -- a ZERO PIVOT, not a "
                f"divergence: a cell or region connected to nothing that fixes the "
                f"pressure. Look at the mesh region count; no numerics rung reaches this.")
        for name, text in logs.items():
            # A healthy continuity sum is O(1e-9 .. 1e2); v2-00283bf3eca50684 went 2.9e10, 1.5e15,
            # 4.6e136 in four iterations -- the cold-start blow-up Eddy3D e044a147 answers with
            # a clamped warm-up laplacian and a potentialFoam start.
            sums = [float(x) for x in _CONTINUITY.findall(text)]
            if sums and max(sums) > 1e5:
                out.append(
                    f"{case.name}: the solution DIVERGED ({name}: continuity error reached "
                    f"{max(sums):.1e}). A start that blows up is what the hardened numerics rungs "
                    f"are for -- on a node older than Eddy3D e044a147 there are none.")
                break
        for name, text in logs.items():
            if "Generating stack trace" not in text and "job aborted" not in text:
                continue
            # The step list tees with -a, so the log holds every rung: count this one's.
            banner = text.rfind("\nExec ")
            iterations = len(_TIME.findall(text[banner:] if banner >= 0 else text))
            if iterations <= 1:
                out.append(
                    f"{case.name}: the solver died in iteration {iterations} ({name}). "
                    f"A start-up death with healthy numbers above it is structural (mesh, "
                    f"boundary conditions), not a start the numerics ladder can fix.")
            break
        if pf and ("Generating stack trace" in pf or "ended prematurely" in pf) \
                and not zero_pivot:
            out.append(f"{case.name}: potentialFoam crashed (run_00b_potentialFoam.txt).")
        if zero_pivot or any("Generating stack trace" in t for t in logs.values()):
            break  # the first direction that died says it; the rest repeat it
    return out


# --- repro: the throwaway broker and the node ----------------------------------

def signatures(findings: list[str]) -> list[str]:
    """The findings that are signatures, not context."""
    return [f for f in findings if not f.startswith("info:")]


def case_input(case: dict) -> list[dict]:
    """The POST /v1/cases body that recreates a production case row. The case id is
    derived from the coordinates and recipe, so the local copy gets the same id.
    `max_attempts` 1: in a reproduction one failure is the answer."""
    spec = case["spec"]
    if isinstance(spec, str):
        spec = json.loads(spec)
    return [{"lat": spec["lat"], "lon": spec["lon"], "recipe": case["recipe"],
             "city_cluster": case["city_cluster"], "lcz": case.get("lcz"),
             "spec": spec, "max_attempts": 1}]


def _http(url: str, token: str, method: str = "GET", payload=None, timeout: float = 60.0):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _wait_up(url: str, token: str, seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            _http(url + "/v1/cases?limit=1", token, timeout=5)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1)
    return False


def run_repro(case_id: str, *, source: str, source_token: str, e3d: str,
              root: str, cpus: int, engine: str, port: int, case_timeout: int) -> int:
    base = pathlib.Path(root).resolve()
    run = base / time.strftime("%Y%m%d-%H%M%S")     # fresh: a finished mesh would be resumed, not re-meshed
    for d in ("node", "work", "done"):
        (run / d).mkdir(parents=True, exist_ok=True)

    try:
        case = _http(f"{source.rstrip('/')}/v1/cases/{case_id}", source_token)
    except urllib.error.HTTPError as e:
        print(f"could not read {case_id} from {source}: HTTP {e.code}", file=sys.stderr)
        return 2
    (run / "case.json").write_text(json.dumps(case, indent=1), encoding="utf-8")
    print(f"production: {case.get('state')} {case.get('attempts')}/{case.get('max_attempts')} "
          f"on {case.get('last_worker')}")

    token = secrets.token_urlsafe(24)
    local = f"http://127.0.0.1:{port}"
    env = dict(os.environ, CASEBROKER_DB=str(run / "broker.sqlite"), CASEBROKER_WRITE_TOKENS=token)
    for leaked in ("CASEBROKER_READ_TOKENS", "CASEBROKER_TOKENS", "DATABASE_URL"):
        env.pop(leaked, None)                        # the throwaway broker must never see production's
    broker_log = open(run / "broker.log", "wb")
    broker = subprocess.Popen([sys.executable, "-m", "uvicorn", "casebroker.app:app",
                               "--host", "127.0.0.1", "--port", str(port)],
                              env=env, stdout=broker_log, stderr=subprocess.STDOUT)
    try:
        if not _wait_up(local, token):
            print(f"the local broker did not come up (see {run / 'broker.log'})", file=sys.stderr)
            return 2
        added = _http(local + "/v1/cases", token, "POST", case_input(case))
        if added.get("added") != 1:
            print(f"the local broker did not take the case: {added}", file=sys.stderr)
            return 2
        # Its OWN credential directory: the machine's real one belongs to any live node here.
        (run / "node" / "credential.json").write_text(json.dumps(
            {"broker": local, "name": "repro", "token": token,
             "paired_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}), encoding="utf-8")
        node_env = dict(os.environ, EDDY3D_NODE_DIR=str(run / "node"))
        # The long verb on purpose: every node build accepts it, older ones only it.
        cmd = [e3d, "run-simulation-node", "--work", str(run / "work"), "--done", str(run / "done"),
               "--cpus", str(cpus), "--engine", engine, "--max-cases", "1",
               # A reproduction needs to reach the failure, not finish 32 directions.
               "--case-timeout", str(case_timeout),
               "--drain", "--max-idle-polls", "1"]
        print(f"node: {' '.join(cmd)}\n  scratch: {run}")
        with open(run / "node.log", "wb") as node_log:
            node = subprocess.run(cmd, env=node_env, stdout=node_log, stderr=subprocess.STDOUT)
        final = _http(f"{local}/v1/cases/{case_id}", token)
    finally:
        broker.terminate()
        try:
            broker.wait(timeout=20)
        except subprocess.TimeoutExpired:
            broker.kill()
        broker_log.close()

    print(f"reproduction: {final.get('state')} (node exit {node.returncode})")
    if final.get("last_error"):
        print("  " + final["last_error"].splitlines()[0])
    study = run / "work" / "cases" / case_id / case_id
    findings = triage(study)
    print(f"\ntriage of {study}:")
    for f in findings + ([] if signatures(findings) else
                         ["no known signature found -- read the step logs under mesh*/ and case_*/"]):
        print(f"  - {f}")
    return 0 if final.get("state") == "done" else 1
