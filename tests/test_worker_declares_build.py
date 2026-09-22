"""The worker says WHICH CODE it is.

Every row on the fleet table read "undeclared". The broker records a build with
each lease and the E3D node sends one, but this worker -- the Python path, on
every workstation and every PACE job -- sent nothing at all. `e3d version --json`
already prints exactly what a lease carries, so the worker asks once and forwards
it. Three things are pinned: what is forwarded, what an e3d too old to answer
means (nothing, not a crash), and what a 426 -- "update this node" -- does to a
worker that cannot update itself.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import worker  # noqa: E402
from casebroker.app import create_app  # noqa: E402

BUILD = "1.14.0.827+e044a147"
ANSWER = {"version": "1.14.0.827", "build": BUILD, "commit": "e044a1470f0f", "platform": "win-x64",
          "recipes": ["fixed-box-1008/of12-v3", "cyl-1008/of12-v4"]}


def says(stdout: str, code: int = 0):
    """An `e3d` that prints `stdout` when asked `version --json`."""
    def run(argv, **kw):
        assert argv[1:] == ["version", "--json"]
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")
    return run


def test_the_worker_forwards_what_e3d_says_it_is(monkeypatch):
    # A build that logs before it answers has still answered: the LAST line counts.
    monkeypatch.setattr(worker.subprocess, "run", says("[info] warming up\n" + json.dumps(ANSWER) + "\n"))
    got = worker.e3d_identity("/e/wind/bin/e3d.exe")
    assert (got["build"], got["version"], got["platform"]) == (BUILD, "1.14.0.827", "win-x64")
    assert got["recipes"] == ANSWER["recipes"]


def test_the_lease_carries_the_build_and_the_broker_records_it(tmp_path):
    """End to end through the real app: what this worker sends is what the
    fleet table reads, so a row that says "undeclared" now means the client
    sent nothing -- not that the worker never had a way to say."""
    app = create_app(db_path=str(tmp_path / "b.sqlite"), tokens=["w"])
    client = TestClient(app, headers={"Authorization": "Bearer w"})
    w = worker.Worker("http://testserver", "w", worker_id="ws-01",
                      build=BUILD, version="1.14.0.827", platform="win-x64")
    w.http = client
    assert w.lease() == []                      # an empty queue still records who asked
    row = client.get("/v1/status").json()["workers"][0]
    assert (row["worker_id"], row["build"], row["version"], row["platform"]) == \
        ("ws-01", BUILD, "1.14.0.827", "win-x64")
    assert row["recipes"] is None, "nothing vouched for the runner's recipes, so none were declared"


def test_an_e3d_too_old_to_answer_means_undeclared_not_a_crash(monkeypatch, capsys):
    monkeypatch.setattr(worker.subprocess, "run", says("1.14.0\n"))      # ignores --json
    assert worker.e3d_identity("/x/e3d") is None
    monkeypatch.setattr(worker.subprocess, "run", says("", code=1))
    assert worker.e3d_identity("/x/e3d") is None

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(worker.subprocess, "run", missing)
    assert worker.e3d_identity("/nowhere/e3d") is None
    assert worker.e3d_identity(None) is None
    assert "declares no build" in capsys.readouterr().err
    # ...and a worker told nothing leases exactly as before.
    w = worker.Worker("http://broker.invalid", None, worker_id="ws-01")
    assert (w.build, w.version, w.platform, w.recipes) == (None, None, None, None)


def test_machine_envs_msys_spelling_reaches_a_native_python():
    """setup_windows.ps1 writes EDDY3D_CLI=/e/wind/bin/e3d.exe, for run_case.sh."""
    assert worker.msys_to_windows("/e/wind/bin/e3d.exe") == "E:/wind/bin/e3d.exe"
    assert worker.msys_to_windows("C:/wind/bin/e3d.exe") == "C:/wind/bin/e3d.exe"
    assert worker.msys_to_windows("/usr/local/bin/e3d") == "/usr/local/bin/e3d"


def test_recipes_are_declared_only_when_the_operator_vouches_for_them(monkeypatch):
    """e3d's own recipe list is NOT forwarded: run_case.sh is its own contract,
    and a worker that declared v4 on e3d's word would take v4 cases into a
    runner that builds v3 boxes -- the mislabelled-archive bug, back again."""
    seen: dict = {}

    def record(self, *a, **k):
        seen.update(build=self.build, platform=self.platform, recipes=self.recipes)
        return 0
    monkeypatch.setattr(worker.Worker, "run_forever", record)
    monkeypatch.setattr(worker.subprocess, "run", says(json.dumps(ANSWER) + "\n"))
    base = ["--broker", "http://broker.invalid", "--e3d", "/x/e3d"]
    assert worker.main(base + ["--recipes", "fixed-box-1008/of12-v3, "]) == 0
    assert seen == {"build": BUILD, "platform": "win-x64", "recipes": ["fixed-box-1008/of12-v3"]}
    seen.clear()
    assert worker.main(base) == 0
    assert seen == {"build": BUILD, "platform": "win-x64", "recipes": None}


def test_a_refused_build_stops_the_worker_and_says_which(monkeypatch):
    """426 is "update this node", which this worker cannot do for itself. Before,
    it retried every idle_backoff seconds for the whole walltime, logging one
    "[warn] lease failed: 426" per try and never the reason."""
    def refuse(request):
        return httpx.Response(426, json={"detail": f"build {BUILD} is blocked for this campaign; update this node"})
    w = worker.Worker("http://broker.invalid", None, worker_id="ws-01", build=BUILD)
    w.http = httpx.Client(base_url="http://broker.invalid", transport=httpx.MockTransport(refuse))
    monkeypatch.setattr(w, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(w, "_heartbeat_loop", lambda: None)
    slept: list = []
    monkeypatch.setattr("casebroker.worker.time.sleep", slept.append)
    with pytest.raises(worker.BuildRefused) as e:
        w.run_forever(worker.echo_runner, idle_backoff=0)
    assert BUILD in str(e.value) and "blocked" in str(e.value)
    assert slept == [], "it stopped at once rather than backing off into another try"


def test_main_turns_a_refused_build_into_exit_3_not_the_credentials_2(monkeypatch):
    def refused(self, *a, **k):
        raise worker.BuildRefused("broker refused this worker's build (undeclared): declare one")
    monkeypatch.setattr(worker.Worker, "run_forever", refused)
    assert worker.main(["--broker", "http://broker.invalid", "--e3d", ""]) == 3
