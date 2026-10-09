#!/usr/bin/env python3
"""The case runner the contract test hands an E3D node (``run-sim-node --runner``).

It speaks the node's script-runner contract (Eddy3D ``MetaFOAM.Lib/Node/ScriptCaseRunner.cs``,
the same one casebroker's ``runner/run_case.sh`` keeps): the case spec arrives as JSON on stdin
and in ``CASE_SPEC``; ``CASE_ID`` and ``LEASE_ID`` are set; ONE progress line at a time goes to
``CASEBROKER_PROGRESS_FILE`` (the node reads the whole file as the line, at most 200 characters);
the LAST stdout line is a JSON object with ``result_uri``. Nothing is simulated.

What it says is chosen to exercise the protocol, not to look like a solve:

* the heartbeat grammar the node writes (``site geometry``, ``mesh i/n``, ``solve i/n dirs``,
  ``convergence gate``, ``archiving``), whose stage both sides agree on;
* ``continuing: ...`` -- a line the native runner writes when it takes a case over, whose stage
  (``resume``) the NODE knows and the broker's old line parser does not. A ``resume`` segment on
  the case page is therefore proof that the stage came from the node's heartbeat;
* telemetry lines (``telemetry:{"kind":...,"data":...}``): ``site``, ``mesh`` and one direction's
  ``residuals`` series.

Each line is held for ``CONTRACT_HOLD`` seconds (default 5) so the node's progress poll and its
heartbeat both see it; the driver runs the node with a 2 s heartbeat.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path


def say(line: str, hold: float) -> None:
    """One progress line, replaced whole (never seen half-written), then held."""
    target = Path(os.environ["CASEBROKER_PROGRESS_FILE"])
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(line, encoding="utf-8")
    os.replace(temp, target)
    time.sleep(hold)


def telemetry(kind: str, data: dict) -> str:
    line = "telemetry:" + json.dumps({"kind": kind, "data": data}, separators=(",", ":"))
    if len(line) > 200:
        raise SystemExit(f"telemetry line for {kind} is {len(line)} characters; the node keeps 200")
    return line


def main() -> int:
    spec = json.loads(sys.stdin.read() or os.environ.get("CASE_SPEC") or "{}")
    case_id = os.environ.get("CASE_ID", "unknown")
    hold = float(os.environ.get("CONTRACT_HOLD", "5"))
    out = Path(os.environ.get("CONTRACT_OUT") or tempfile.gettempdir())
    out.mkdir(parents=True, exist_ok=True)

    say("site geometry", hold)
    say(telemetry("site", {"n_buildings": 3, "contract": True}), hold)
    say("continuing: fetching the mesh contract-upstream made", hold)
    say("mesh 1/2 · 01_blockMesh", hold)
    say("mesh 2/2 · 02_snappyHexMesh", hold)
    say(telemetry("mesh", {"total_cells": 12345, "all_ok": True}), hold)
    say("solve 0/2 dirs · case_000 iter 10/100", hold)
    say(telemetry("residuals", {"direction": "case_000", "iterations": [1, 2, 3],
                                "fields": {"p": [0.1, 0.01, 0.001]}, "end_time": 3, "complete": True}), hold)
    say("solve 1/2 dirs · case_001 iter 50/100", hold)
    say("convergence gate", hold)
    say("archiving", hold)

    # A result the node can point the broker at: a real archive, hashed.
    archive = out / f"{case_id}.tar.gz"
    payload = json.dumps({"case_id": case_id, "spec": spec, "runner": "e3d_fake_runner"}).encode()
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(f"{case_id}/manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    data = archive.read_bytes()
    print(json.dumps({
        "result_uri": archive.resolve().as_uri(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "metrics": {"stage": "archived", "runner": "e3d_fake_runner"},
    }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
