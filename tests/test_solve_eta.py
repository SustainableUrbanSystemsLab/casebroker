"""A worker's current case carries an estimate of when its solve ends."""
import time

from casebroker import db

# Real time: status() lists workers seen in the last 24 h of the WALL clock.
T = int(time.time()) - 20_000


def _setup(tmp_path):
    conn = db.connect(str(tmp_path / "e.sqlite"))
    db.add_cases(conn, [{"case_id": "A", "spec": {"lat": 48.1, "lon": 11.57}, "recipe": "r",
                         "city_cluster": "m", "lcz": "LCZ2", "split": "train",
                         "priority": 100, "max_attempts": 3}])
    lease = db.lease(conn, "w1", 1, now=T + 1_000)[0]
    return conn, lease


def _eta(conn, now):
    w = [w for w in db.status(conn, now=now)["workers"] if w["worker_id"] == "w1"][0]
    return w["current_eta"]


def test_the_rate_of_this_leases_solve_lines_is_extrapolated(tmp_path):
    conn, lease = _setup(tmp_path)
    # Meshing first: not part of the rate.
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 3/5 · 03_snappyHexMesh", now=T + 1_100)
    db.heartbeat(conn, lease.lease_id, 3600, "solve 0/8 dirs · iter 0/2000", now=T + 2_000)
    db.heartbeat(conn, lease.lease_id, 3600, "solve 2/8 dirs · iter 0/2000", now=T + 4_000)   # 25 % in 2000 s
    eta = _eta(conn, T + 4_000)
    assert eta["fraction"] == 0.25
    assert eta["at"] == T + 4_000 + 6_000, "75 % left at 25 % per 2000 s"


def test_no_estimate_from_one_reading_or_ten_seconds(tmp_path):
    conn, lease = _setup(tmp_path)
    db.heartbeat(conn, lease.lease_id, 3600, "solve 0/8 dirs · iter 0/2000", now=T + 2_000)
    assert _eta(conn, T + 2_000) is None
    db.heartbeat(conn, lease.lease_id, 3600, "solve 0/8 dirs · iter 50/2000", now=T + 2_010)
    assert _eta(conn, T + 2_010) is None, "ten seconds is not a pace"


def test_meshing_alone_gives_no_estimate(tmp_path):
    conn, lease = _setup(tmp_path)
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 1/5 · 01_blockMesh", now=T + 1_100)
    db.heartbeat(conn, lease.lease_id, 3600, "mesh 4/5 · 04_renumberMesh", now=T + 3_100)
    assert _eta(conn, T + 3_100) is None
