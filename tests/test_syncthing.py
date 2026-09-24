"""The broker is the Syncthing rendezvous for finished archives.

On 2026-09-23 no remote machine had ever been paired with the Syncthing master,
because pairing meant exchanging device IDs by hand on both sides for every
machine -- so every archive a remote node finished was still on its own disk. An
admin now names the master once; each node reports its own device with every
lease and pairs itself with the master named here; the master accepts exactly
the worker devices listed here.
"""

from __future__ import annotations

import pytest

from casebroker import db

MASTER = "DW2QZ5L-CFGJL7D-X4VRTG5-SZ6535Q-Y2JGKT6-RFCZ6AN-RBRFEXB-IQ54MQ7"
WORKER = "JTTJTLR-WPDOPTQ-BQDK3ZH-HICNXUI-2PBOM5J-652EAN3-VXGQPLA-UWMQPAL"
T0 = 1_000_000


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "s.sqlite"))


def test_a_device_id_is_normalised_and_anything_else_is_not_one():
    assert db.normalize_device_id(MASTER) == MASTER
    assert db.normalize_device_id(MASTER.replace("-", "").lower()) == MASTER, "pasted without dashes"
    assert db.normalize_device_id(" " + MASTER + "\n") == MASTER
    for bad in (None, "", "master", MASTER[:-1], MASTER.replace("D", "1", 1), MASTER + "-AAAAAAA"):
        assert db.normalize_device_id(bad) is None, bad


def test_no_master_until_an_admin_names_one(conn):
    assert db.syncthing_view(conn, now=T0) == {"master": None, "workers": [], "continuations": []}


def test_naming_the_master_and_its_folder(conn):
    out = db.set_syncthing_master(conn, MASTER.lower(), "wind-done", by="ada", now=T0)
    assert out["master"] == {"device_id": MASTER, "folder": "wind-done"}
    # The folder defaults, and survives setting the device alone.
    db.set_syncthing_master(conn, MASTER, None, by="ada", now=T0)
    assert db.syncthing_view(conn, now=T0)["master"]["folder"] == "wind-done"
    trail = [r["detail"] for r in conn.execute("SELECT detail FROM events WHERE event='setting'")]
    assert any("syncthing_master = " + MASTER in d for d in trail), "who pointed the fleet where is audited"


def test_a_typo_is_refused_rather_than_pairing_the_fleet_with_nobody(conn):
    with pytest.raises(ValueError):
        db.set_syncthing_master(conn, "DW2QZ5L-CFGJL7D", None, by="ada", now=T0)
    with pytest.raises(ValueError):
        db.set_syncthing_master(conn, MASTER, "wind done/../x", by="ada", now=T0)
    assert db.syncthing_view(conn, now=T0)["master"] is None


def test_clearing_the_master(conn):
    db.set_syncthing_master(conn, MASTER, None, by="ada", now=T0)
    assert db.set_syncthing_master(conn, None, None, by="ada", now=T0)["master"] is None


def test_a_node_reports_its_device_with_every_lease(conn):
    db.lease(conn, "cod-1", now=T0, host="COD-1", syncthing_id=WORKER.lower())
    workers = db.syncthing_view(conn, now=T0)["workers"]
    assert workers == [{"worker_id": "cod-1", "host": "COD-1", "device_id": WORKER, "last_seen": T0}]


def test_a_lease_that_leaves_it_out_or_garbles_it_keeps_the_known_device(conn):
    db.lease(conn, "cod-1", now=T0, host="COD-1", syncthing_id=WORKER)
    db.lease(conn, "cod-1", now=T0 + 60, host="COD-1")                      # an older client
    db.lease(conn, "cod-1", now=T0 + 120, host="COD-1", syncthing_id="nope")  # never fails a lease
    assert [w["device_id"] for w in db.syncthing_view(conn, now=T0 + 120)["workers"]] == [WORKER]


def test_a_new_device_replaces_the_old_one(conn):
    db.lease(conn, "cod-1", now=T0, syncthing_id=WORKER)
    db.lease(conn, "cod-1", now=T0 + 60, syncthing_id=MASTER)   # reinstalled: a new key pair
    assert [w["device_id"] for w in db.syncthing_view(conn, now=T0 + 60)["workers"]] == [MASTER]


def test_a_machine_gone_for_a_month_is_no_longer_offered_to_the_master(conn):
    db.lease(conn, "old", now=T0, syncthing_id=WORKER)
    later = T0 + db.SYNCTHING_WORKER_WINDOW_SECONDS + 1
    assert db.syncthing_view(conn, now=later)["workers"] == []


def test_the_api_reads_with_read_scope_and_writes_only_as_admin(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from casebroker.app import create_app

    monkeypatch.setenv("CASEBROKER_TOKENS", "w-token")
    monkeypatch.setenv("CASEBROKER_READONLY_TOKENS", "r-token")
    client = TestClient(create_app(str(tmp_path / "api.sqlite")))
    worker, reader = {"Authorization": "Bearer w-token"}, {"Authorization": "Bearer r-token"}

    lease = client.post("/v1/lease", headers=worker,
                        json={"worker_id": "cod-1", "host": "COD-1", "syncthing_id": WORKER})
    assert lease.status_code == 200
    got = client.get("/v1/syncthing", headers=reader)
    assert got.status_code == 200
    assert got.json() == {"master": None, "workers": [
        {"worker_id": "cod-1", "host": "COD-1", "device_id": WORKER, "last_seen": got.json()["workers"][0]["last_seen"]}],
        "continuations": []}

    # Pointing the fleet's archives somewhere is an admin decision, like a release target.
    assert client.put("/v1/syncthing", headers=worker, json={"device_id": MASTER}).status_code in (401, 403)
    assert client.get("/v1/syncthing").status_code == 401
