"""A node that refuses work on its own side says why, and the page shows it.

A lab machine (COD-PKAST-7865-style, 2026-10-10) sat with 11.7 GB free under the 20 GB
a case needs and printed "this machine cannot run cases" to a console nobody watched.
It never leased, so the broker saw nothing but a last_seen growing older: the Workers
table called it Offline, and after a day it dropped off the list altogether -- while it
was alive and asking what to run every few minutes. The node now says why with those
asks (`unfit` on /v1/node/release); the broker keeps it on the worker's row until the
node says nothing or leases, announces when a spell begins, and lists a worker that
asks what to run even when it has not leased for a day.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, push  # noqa: E402
from casebroker.app import create_app  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
DISK = ("the disk holding C:\\Users\\lab\\AppData\\Local\\Eddy3D\\node\\work has 11.7 GB free, under the "
        "20.0 GB a case needs. Make room (old case folders under the work root, archives the master already "
        "has), or pass --min-free-gb to change the threshold")


def _row(conn, worker):
    r = conn.execute("SELECT unfit, unfit_since, last_seen, release_asked_at FROM workers WHERE worker_id = ?",
                     (worker,)).fetchone()
    return dict(r) if r else None


def _events(conn, worker):
    return [dict(r) for r in conn.execute(
        "SELECT event, detail FROM events WHERE worker_id = ? AND event = 'unfit' ORDER BY id", (worker,))]


def test_a_node_that_has_never_leased_is_recorded_with_why_it_cannot_run(tmp_path):
    conn = db.connect(str(tmp_path / "u.sqlite"))
    db.node_release(conn, "COD-1", "win-x64", "1.17.0+a", unfit=DISK, now=1_000)
    row = _row(conn, "COD-1")
    assert row is not None, "a machine that cannot run from its first start is exactly the one nobody would see"
    assert row["unfit"] == DISK and row["unfit_since"] == 1_000 and row["release_asked_at"] == 1_000
    assert _events(conn, "COD-1") == [{"event": "unfit", "detail": DISK}]

    # The free GB moves on every ask: the text follows it, the spell keeps its start, and it is not news again.
    db.node_release(conn, "COD-1", "win-x64", "1.17.0+a", unfit=DISK.replace("11.7", "11.6"), now=1_300)
    row = _row(conn, "COD-1")
    assert "11.6 GB" in row["unfit"] and row["unfit_since"] == 1_000
    assert len(_events(conn, "COD-1")) == 1

    # An ask that says nothing: it can run again.
    db.node_release(conn, "COD-1", "win-x64", "1.17.0+a", now=1_600)
    assert _row(conn, "COD-1")["unfit"] is None and _row(conn, "COD-1")["unfit_since"] is None

    # A second spell is a second event.
    db.node_release(conn, "COD-1", "win-x64", "1.17.0+a", unfit="docker: the daemon is not running", now=2_000)
    assert [e["detail"] for e in _events(conn, "COD-1")] == [DISK, "docker: the daemon is not running"]


def test_a_lease_is_a_node_past_its_own_check(tmp_path):
    conn = db.connect(str(tmp_path / "u.sqlite"))
    db.node_release(conn, "COD-2", "win-x64", "1.17.0+a", unfit=DISK)
    assert _row(conn, "COD-2")["unfit"] == DISK
    assert db.lease(conn, "COD-2", count=1) == []                # nothing queued; the ask itself counts
    assert _row(conn, "COD-2")["unfit"] is None


def test_an_ask_without_a_reason_never_makes_a_row(tmp_path):
    """Old nodes, and every ask of a node that can run: nothing to record, nothing created."""
    conn = db.connect(str(tmp_path / "u.sqlite"))
    db.node_release(conn, "COD-3", "win-x64", "1.17.0+a")
    assert _row(conn, "COD-3") is None


@pytest.fixture()
def app(tmp_path):
    return create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=["r"])


def test_the_reason_reaches_the_workers_list_and_a_worker_that_only_asks_stays_on_it(app):
    node = TestClient(app)
    node.headers.update({"Authorization": "Bearer w"})
    r = node.get("/v1/node/release", params={"worker_id": "COD-4", "platform": "win-x64",
                                              "build": "1.17.0+a", "unfit": DISK})
    assert r.status_code == 200, r.text
    reader = TestClient(app)
    reader.headers.update({"Authorization": "Bearer r"})
    workers = {w["worker_id"]: w for w in reader.get("/v1/status").json()["workers"]}
    assert workers["COD-4"]["unfit"] == DISK and workers["COD-4"]["unfit_since"]

    # A node that leased two days ago and since then only asks what to run (an E3D build from
    # before it could say why): still listed, which the page reads as "not taking cases".
    conn = db.connect(app.state.db_path)
    db.lease(conn, "COD-5", count=1)
    conn.execute("UPDATE workers SET last_seen = ? WHERE worker_id = 'COD-5'", (int(time.time()) - 2 * 86400,))
    node.get("/v1/node/release", params={"worker_id": "COD-5", "platform": "win-x64", "build": "1.13.0+old"})
    workers = {w["worker_id"]: w for w in reader.get("/v1/status").json()["workers"]}
    assert "COD-5" in workers, "alive and asking; it used to age off the list after a day"
    assert workers["COD-5"]["unfit"] is None


def test_a_spell_beginning_is_announced_once(app):
    conn = db.connect(app.state.db_path)
    later = lambda: int(time.time()) + push.LAG_SECONDS + 5            # noqa: E731
    sent = []
    send = lambda sub, payload, *, ttl, urgency: (sent.append(payload), 201)[1]   # noqa: E731
    c = TestClient(app)
    assert c.post("/v1/auth/setup", json={"username": "ada", "password": "a-sufficiently-long-passphrase",
                                          "setup_token": "w"}).status_code == 200
    from tests.test_push import browser, subscribe                     # noqa: E402 -- the same fake browser
    sub, _, _ = browser()
    assert subscribe(c, sub, ["worker_unfit"]).status_code == 200
    push.tick(conn, later(), send=send)
    db.node_release(conn, "COD-6", "win-x64", "1.17.0+a", unfit=DISK)
    db.node_release(conn, "COD-6", "win-x64", "1.17.0+a", unfit=DISK.replace("11.7", "11.5"))
    push.tick(conn, later(), send=send)
    assert [p["kind"] for p in sent] == ["worker_unfit"]
    assert sent[0]["title"] == "COD-6 cannot run cases" and "11.7 GB free" in sent[0]["body"]
    assert sent[0]["url"] == "/#settings=machines"
    push.tick(conn, later(), send=send)
    assert len(sent) == 1, "once per spell, not once per ask"


def test_the_page_reads_a_worker_the_way_the_cards_and_the_table_both_do():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine")
    page = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    finite = next(line for line in page.splitlines() if line.startswith("  const finite = "))
    start = page.index("  function workerCondition(")
    fn = page[start:page.index("\n  }\n", start) + 4]
    start = page.index("  function firstSentence(")
    first = page[start:page.index("\n  }\n", start) + 4]
    script = finite + "\n" + fn + "\n" + first + """
const now = 100000;
const out = {
  unfit: workerCondition({unfit: "disk full. make room", unfit_since: 99000, last_seen: 1}, now),
  active: workerCondition({last_seen: now - 60}, now),
  refusing: workerCondition({last_seen: now - 7200, release_asked_at: now - 120}, now),
  busy: workerCondition({last_seen: now - 7200, release_asked_at: now - 120, current_case: "v2-x"}, now),
  offline: workerCondition({last_seen: now - 7200, release_asked_at: now - 7200}, now),
  never: workerCondition({}, now),
  first: firstSentence("the disk holding C:\\\\work has 11.7 GB free, under the 20.0 GB a case needs. Make room"),
};
console.log(JSON.stringify(out));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert (got["unfit"]["key"], got["unfit"]["label"], got["unfit"]["since"]) == ("unfit", "Cannot run cases", 99000)
    assert got["active"]["key"] == "active"
    assert got["refusing"]["key"] == "refusing", "asks what to run, not for a case: an older build refusing"
    assert got["busy"]["key"] == "offline", "a worker holding a case is not refusing work"
    assert got["offline"]["key"] == "offline" and got["never"]["key"] == "offline"
    assert got["first"] == "the disk holding C:\\work has 11.7 GB free, under the 20.0 GB a case needs"


def test_the_table_and_the_cards_both_use_that_reading():
    page = (ROOT / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    assert "const cond = workerCondition(w, nowS);" in page
    assert 'class="worker-unfit"' in page and "firstSentence(w.unfit)" in page
    cards = page[page.index("  function fleetCards("):page.index("  function progressPhase(")]
    assert "workerCondition(w, nowSeconds)" in cards and "c.unfit++" in cards
    assert "can't run</span>" in page
