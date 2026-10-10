"""How busy each worker's machine is, as the node measures it.

A lab workstation is somebody's desk: a node that solves slowly there may be sharing
it with a person's work, and the Worker Fleet had no way to say so. The node now sends
the machine's whole CPU utilisation with its release asks (`cpu`, percent), and the
table shows it beside the cores and memory the node declared.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from casebroker import db
from casebroker.app import create_app

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _cpu(conn, worker):
    r = conn.execute("SELECT cpu_pct, cpu_at FROM workers WHERE worker_id = ?", (worker,)).fetchone()
    return (r["cpu_pct"], r["cpu_at"]) if r else None


def test_a_node_s_measurement_is_kept_with_its_time(tmp_path):
    conn = db.connect(str(tmp_path / "c.sqlite"))
    db.lease(conn, "COD-1", count=0)
    db.node_release(conn, "COD-1", "win-x64", "1.0+a", cpu=87.44, now=1_000)
    assert _cpu(conn, "COD-1") == (87.4, 1_000)
    # Rounding past the ends is clamped; what is not a percentage is dropped, never an error.
    db.node_release(conn, "COD-1", "win-x64", "1.0+a", cpu=100.3, now=1_100)
    assert _cpu(conn, "COD-1") == (100.0, 1_100)
    for bad in (float("nan"), 140.0, -3.0, "lots"):
        db.node_release(conn, "COD-1", "win-x64", "1.0+a", cpu=bad, now=1_200)
        assert _cpu(conn, "COD-1") == (100.0, 1_100), bad
    # An ask that says nothing keeps the last measurement; the page ages it out by its time.
    db.node_release(conn, "COD-1", "win-x64", "1.0+a", now=1_300)
    assert _cpu(conn, "COD-1") == (100.0, 1_100)


def test_a_measurement_alone_makes_no_worker(tmp_path):
    conn = db.connect(str(tmp_path / "c.sqlite"))
    db.node_release(conn, "COD-2", "win-x64", "1.0+a", cpu=12.0)
    assert _cpu(conn, "COD-2") is None


@pytest.fixture()
def client(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"]))
    c.headers.update({"Authorization": "Bearer w"})
    return c


def test_the_ask_carries_it_and_the_status_shows_it(client):
    assert client.post("/v1/lease", json={"worker_id": "COD-3", "count": 1}).json() == []   # nothing queued: registers it
    ask = {"worker_id": "COD-3", "platform": "win-x64", "build": "1.0+a"}
    assert client.get("/v1/node/release", params={**ask, "cpu": "43.25"}).status_code == 200
    w = {x["worker_id"]: x for x in client.get("/v1/status").json()["workers"]}["COD-3"]
    assert w["cpu_pct"] == 43.2 and w["cpu_at"]
    # A value that is not a number is dropped; the ask still answers.
    assert client.get("/v1/node/release", params={**ask, "cpu": "n/a"}).status_code == 200


def test_the_page_shows_it_beside_the_cores_and_memory_while_it_is_fresh():
    page = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    start = page.index("  function machineLine(w) {")
    line = page[start:page.index("\n  }\n", start)]
    assert "bits.push(`CPU ${cpu}%`)" in line
    assert "cpuAge < 900" in line, "a measurement a quarter of an hour old is not shown as current"
    assert "not only this node's" in line, "the tooltip says it is the whole machine"
