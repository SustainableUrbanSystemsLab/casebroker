"""slurm/fleet_report.py: what SLURM holds for the node jobs, posted to the broker.

Queued jobs have never called the broker, so the Worker Fleet showed nothing of them
until one started. The reporter runs on PACE, where the only Python is the system's
and the only credential is the node's own pairing; it is run here against a fake squeue
and a real broker app, and the token must never reach a command line or its output.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from casebroker.app import create_app

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT = ROOT / "slurm" / "fleet_report.py"
TOKEN = "node-token-not-to-be-seen-anywhere-x" * 2

SQUEUE = """#!/bin/bash
echo "$*" >> "$SQUEUE_ARGS"
cat <<'OUT'
4711|PENDING|Priority|2026-10-10T08:00:00|2026-10-10T13:30:00
4712|PENDING|Dependency|2026-10-10T08:00:01|N/A
4713|PENDING|Priority|2026-10-10T08:00:02|N/A
4709|RUNNING|None|2026-10-10T06:00:00|2026-10-10T06:05:00
OUT
"""


@pytest.fixture()
def env(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    squeue = stubs / "squeue"
    squeue.write_text(SQUEUE)
    squeue.chmod(0o755)
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    return {"tmp": tmp_path, "node_dir": node_dir,
            "env": {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "USER": "pkastner3",
                    "EDDY3D_NODE_DIR": str(node_dir), "SQUEUE_ARGS": str(tmp_path / "squeue.args"),
                    "TZ": "UTC"}}


def _run(env, *args):
    return subprocess.run([sys.executable, str(REPORT), *args], env=env["env"], capture_output=True,
                          text=True, timeout=60)


def test_a_dry_run_says_what_it_would_send(env):
    r = _run(env, "--cluster", "ICE", "--name", "e3d-node-ice", "--dry-run")
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout)
    assert (body["cluster"], body["queued"], body["running"]) == ("ICE", 3, 1)
    assert body["detail"] == "e3d-node-ice: 3 pending (Priority 2, Dependency 1), 1 running"
    first = body["jobs"][0]
    assert first == {"id": "4711", "state": "PENDING", "reason": "Priority",
                     "submitted_at": 1791619200, "start_at": 1791639000}
    assert body["jobs"][1]["start_at"] is None, "N/A is no estimate, not a time"
    assert body["jobs"][3]["reason"] is None, "a running job's None is no reason"
    assert (env["tmp"] / "squeue.args").read_text().split() == \
        ["-h", "-u", "pkastner3", "-n", "e3d-node-ice", "-o", "%i|%T|%r|%V|%S"]


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def broker(tmp_path):
    import uvicorn

    app = create_app(db_path=str(tmp_path / "f.sqlite"), tokens=[TOKEN], readonly_tokens=["r"])
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while not server.started:
        assert time.time() < deadline
        time.sleep(0.05)
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True


def test_the_queue_reaches_the_worker_fleet_with_the_node_s_own_credential(env, broker):
    app, url = broker
    (env["node_dir"] / "credential.json").write_text(json.dumps(
        {"broker": url, "name": "ice", "token": TOKEN, "paired_at": "2026-10-07T12:00:00Z"}))
    r = _run(env, "--cluster", "ICE", "--name", "e3d-node-ice")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "reported ICE to the broker: e3d-node-ice: 3 pending (Priority 2, Dependency 1), 1 running"
    assert TOKEN not in r.stdout + r.stderr

    reader = TestClient(app)
    reader.headers.update({"Authorization": "Bearer r"})
    fleet = {f["cluster"]: f for f in reader.get("/v1/status").json()["fleet"]}
    ice = fleet["ICE"]
    assert (ice["queued"], ice["running"]) == (3, 1)
    assert [j["id"] for j in ice["jobs"]] == ["4711", "4712", "4713", "4709"]
    assert ice["jobs"][1]["reason"] == "Dependency"


def test_without_a_pairing_or_a_broker_it_says_why_and_fails(env, broker):
    r = _run(env, "--cluster", "ICE", "--name", "e3d-node-ice")
    assert r.returncode == 1 and "no usable node credential" in r.stderr and "setup-sim-node" in r.stderr
    _, url = broker
    (env["node_dir"] / "credential.json").write_text(json.dumps({"broker": url, "name": "ice", "token": "revoked"}))
    r = _run(env, "--cluster", "ICE", "--name", "e3d-node-ice")
    assert r.returncode == 2 and "refused the report: HTTP 401" in r.stderr
    assert "revoked" not in r.stderr


def test_the_broker_bounds_what_a_report_may_carry(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "g.sqlite"), tokens=["w"], readonly_tokens=[]))
    c.headers.update({"Authorization": "Bearer w"})
    job = {"id": "1", "state": "PENDING"}
    assert c.post("/v1/fleet", json={"cluster": "ICE", "jobs": [job] * 501}).status_code == 422
    assert c.post("/v1/fleet", json={"cluster": "ICE", "jobs": [{"id": "x" * 33, "state": "PENDING"}]}).status_code == 422
    # A reporter that sends counts only (casebroker fleet) leaves no list, not an empty one.
    assert c.post("/v1/fleet", json={"cluster": "ICE", "queued": 2}).status_code == 200
    assert c.get("/v1/status").json()["fleet"][0]["jobs"] is None


def test_the_worker_fleet_lists_the_jobs_that_have_not_started():
    import shutil
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine")
    page = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    finite = next(line for line in page.splitlines() if line.startswith("  const finite = "))

    def fn(name):
        start = page.index(f"  function {name}(")
        return page[start:page.index("\n  }\n", start) + 4]

    script = finite + "\n" + fn("queuedJobs") + "\n" + fn("queueReason") + """
console.log(JSON.stringify({
  listed: queuedJobs([
    { cluster: "ICE", age_seconds: 60, jobs: [{ id: "4711", state: "PENDING", reason: "Priority" },
                                             { id: "4709", state: "RUNNING" }, { id: "4700", state: "COMPLETING" }] },
    { cluster: "Phoenix", age_seconds: 30, jobs: null }, { cluster: "x" }]),
  none: queuedJobs(null),
  words: ["Priority", "Resources", "Dependency", "QOSMaxJobsPerUserLimit", "None", null, "Licenses"].map(queueReason),
}));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["listed"] == [{"cluster": "ICE", "age": 60, "job": {"id": "4711", "state": "PENDING", "reason": "Priority"}}], \
        "a running job is a worker by now, or about to be; only the waiting ones are listed"
    assert got["none"] == []
    assert got["words"] == ["behind higher-priority jobs", "waiting for nodes to free up",
                            "after the job before it in its chain", "at a limit of the account or QOS",
                            "waiting", "waiting", "Licenses"]
    # And where they are drawn: in the Worker Fleet table, after the workers, and counted in its badge.
    assert "const queued = queuedJobs(st.fleet);" in page and 'class="queued-job"' in page
    assert "if (workers.length || queued.length) {" in page, "the table shows for queued jobs alone"
    assert "queued.length ? ` · ${queued.length} queued`" in page
