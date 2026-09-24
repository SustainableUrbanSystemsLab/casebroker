"""A lease the broker gave up on (0617f36, `stale-released`) ends its stage.

stages.from_events() closed a segment only on done / released / cancelled /
failed / quarantined, so after the 48 h sweep the case page still drew the dead
run's stage as running -- "solve, 49 h and counting" for a machine that had been
silent for two days. The segment now ends at the last thing its worker said,
not at the sweep: the run stopped when the machine went quiet.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids, stages  # noqa: E402

T0 = 1_790_000_000
HOUR = 3600


def _dead_run(tmp_path):
    conn = db.connect(str(tmp_path / "b.sqlite"))
    db.add_cases(conn, [{
        "case_id": ids.case_id(10.16, 76.2, "r1"), "spec": {"lat": 10.16, "lon": 76.2},
        "recipe": "r1", "city_cluster": "c0", "lcz": "LCZ9", "split": "train",
    }])
    [lease] = db.lease(conn, "cod-358-21-2", 1, 900, now=T0)
    # Repeated lines on purpose: the segmenter `continue`s on a line that does not
    # change the stage, which is where the time of the last line used to be lost.
    for i, line in enumerate(["solve 0/32 dirs · case_000 iter 10/2000",
                              "solve 0/32 dirs · case_000 iter 20/2000",
                              "solve 0/32 dirs · case_000 iter 30/2000"]):
        db._event(conn, lease.case_id, "cod-358-21-2", "progress", line, T0 + 60 * (i + 1))
    return conn, lease


def _stages(conn, case_id, now):
    events = [dict(e) for e in conn.execute(
        "SELECT event, ts, detail FROM events WHERE case_id=? ORDER BY id", (case_id,))]
    return stages.from_events(events, now=now)


def test_a_stale_released_run_is_not_drawn_as_running(tmp_path):
    conn, lease = _dead_run(tmp_path)
    db.status(conn, now=T0 + 900 + 49 * HOUR)          # the sweep runs from status()
    assert dict(conn.execute("SELECT state FROM cases WHERE case_id=?",
                             (lease.case_id,)).fetchone())["state"] == "pending"

    got = _stages(conn, lease.case_id, now=T0 + 900 + 50 * HOUR)
    assert got["current"] is None, "the dead run is not running"
    assert got["stages"][-1]["ended_at"] == T0 + 180, "it ended at the last line its worker sent"
    assert got["failed_in"] is None, "a machine going silent is not the case failing"


def test_the_control_a_live_run_is_still_open(tmp_path):
    conn, lease = _dead_run(tmp_path)
    got = _stages(conn, lease.case_id, now=T0 + 240)
    assert got["current"] is not None and got["stages"][-1]["ended_at"] is None
