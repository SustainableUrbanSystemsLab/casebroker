"""Syncthing is retired (2026-10-06): the broker is no longer its rendezvous.

Finished archives used to travel from every node to one master over Syncthing,
paired through the broker (``/v1/syncthing``). Nodes now send every part of a
case to the broker's own part store, so the rendezvous is gone -- but nodes from
before still lease with the fields they always sent, and a database from before
still holds the master an admin named.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from casebroker import db
from casebroker.app import create_app

DEVICE = "JTTJTLR-WPDOPTQ-BQDK3ZH-HICNXUI-2PBOM5J-652EAN3-VXGQPLA-UWMQPAL"
W, R = {"Authorization": "Bearer w-token"}, {"Authorization": "Bearer r-token"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEBROKER_TOKENS", "w-token")
    monkeypatch.setenv("CASEBROKER_READONLY_TOKENS", "r-token")
    return TestClient(create_app(str(tmp_path / "api.sqlite")))


def test_the_rendezvous_is_gone_which_a_node_from_before_reads_as_no_master(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    # An Eddy3D node from before the removal reads 404 (or 405) as "no master
    # configured", and goes on working exactly as without one.
    assert client.get("/v1/syncthing", headers=R).status_code == 404
    assert client.put("/v1/syncthing", headers=W, json={"device_id": DEVICE}).status_code in (404, 405)


def test_a_node_from_before_still_leases_with_what_it_always_sent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    got = client.post("/v1/lease", headers=W,
                      json={"worker_id": "cod-1", "host": "COD-1", "syncthing_id": DEVICE, "can_continue": True})
    assert got.status_code == 200, got.text
    assert got.json() == []                       # nothing queued; the point is it was not refused


def test_the_master_an_admin_named_is_forgotten_on_the_next_connect(tmp_path):
    path = str(tmp_path / "old.sqlite")
    conn = db.connect(path)
    for key, value in (("syncthing_master", DEVICE), ("syncthing_folder", "wind-done"), ("keep_me", "1")):
        conn.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?, ?, 1, 'ada')",
                     (key, value))
    conn.commit()
    conn.close()

    again = db.connect(path)                      # connecting is the migration
    left = {r["key"] for r in again.execute("SELECT key FROM settings").fetchall()}
    assert {"syncthing_master", "syncthing_folder"}.isdisjoint(left)
    assert "keep_me" in left, "no other setting is touched"


def test_a_database_from_before_keeps_its_device_column_and_leases_as_ever(tmp_path):
    # The schema no longer declares workers.syncthing_id; a database that has it keeps
    # it, unread -- the reconciler only ever adds (dropping would destroy data for a
    # version that may be rolled back).
    path = str(tmp_path / "old.sqlite")
    conn = db.connect(path)
    conn.execute("ALTER TABLE workers ADD COLUMN syncthing_id TEXT")
    conn.commit()
    conn.close()

    again = db.connect(path)
    assert db.lease(again, "cod-1", host="COD-1") == []
    row = again.execute("SELECT worker_id, syncthing_id FROM workers").fetchone()
    assert (row["worker_id"], row["syncthing_id"]) == ("cod-1", None)
