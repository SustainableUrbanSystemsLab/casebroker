"""Quarantining the sampler-v3 west-mirror cases, a batch at a time (scripts/park_mirrored_west.py).

windcomfort-real-cities#1: every western tile decoded at -lat, so every PENDING case
with spec.lon < 0 is misplaced. What has to hold: only pending western cases are
found and parked; eastern, leased, done and already-quarantined cases are left alone.
"""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("park_mirrored_west", ROOT / "scripts" / "park_mirrored_west.py")
pmw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pmw)


class FakeBroker:
    """GET /v1/cases and POST /v1/cases/park over an in-memory queue."""

    def __init__(self, cases: dict[str, dict]):
        self.cases = cases
        self.calls: list[tuple[str, str, dict]] = []

    def call(self, method, path, params):
        self.calls.append((method, path, params))
        if method == "GET":
            rows = [{"case_id": k, **v} for k, v in sorted(self.cases.items())
                    if v["state"] == params["state"]]
            page = rows[params["offset"]:params["offset"] + params["limit"]]
            return {"cases": page, "total": len(rows)}
        parked, skipped = 0, {}
        for cid in params["case_id"]:
            row = self.cases[cid]
            if row["state"] != "pending":
                skipped[row["state"]] = skipped.get(row["state"], 0) + 1
                continue
            if params["dry_run"] == "false":
                row["state"] = "quarantined"
            parked += 1
        return {"matched": parked, "parked": parked if params["dry_run"] == "false" else 0,
                "skipped": skipped}


def queue(west_pending=0, east_pending=0, west_leased=0, west_done=0, west_quarantined=0) -> dict[str, dict]:
    out = {}
    out.update({f"wp{i:03d}": {"state": "pending", "city_cluster": f"c{i}",
                               "spec": {"lat": 10.0, "lon": -70.0}} for i in range(west_pending)})
    out.update({f"ep{i:03d}": {"state": "pending", "city_cluster": f"c{i}",
                               "spec": {"lat": 10.0, "lon": 70.0}} for i in range(east_pending)})
    out.update({f"wl{i:03d}": {"state": "leased", "city_cluster": f"c{i}",
                               "spec": {"lat": 10.0, "lon": -70.0}} for i in range(west_leased)})
    out.update({f"wd{i:03d}": {"state": "done", "city_cluster": f"c{i}",
                               "spec": {"lat": 10.0, "lon": -70.0}} for i in range(west_done)})
    out.update({f"wq{i:03d}": {"state": "quarantined", "city_cluster": f"c{i}",
                               "spec": {"lat": 10.0, "lon": -70.0}} for i in range(west_quarantined)})
    return out


def test_only_pending_western_cases_are_found():
    broker = FakeBroker(queue(west_pending=3, east_pending=5, west_leased=1, west_done=2, west_quarantined=1))
    found = pmw.pending_west(broker, batch=100)
    assert {c["case_id"] for c in found} == {"wp000", "wp001", "wp002"}


def test_pending_west_pages():
    broker = FakeBroker(queue(west_pending=5))
    found = pmw.pending_west(broker, batch=2)
    assert {c["case_id"] for c in found} == {f"wp{i:03d}" for i in range(5)}
    gets = [p for m, _, p in broker.calls if m == "GET"]
    assert [g["offset"] for g in gets] == [0, 2, 4]


def test_park_all_parks_in_batches_and_carries_the_reason():
    broker = FakeBroker(queue(west_pending=5))
    ids = [f"wp{i:03d}" for i in range(5)]
    totals = pmw.park_all(broker, ids, batch=2, sleep=lambda s: None, say=lambda s: None)
    assert totals == {"parked": 5}
    assert all(v["state"] == "quarantined" for k, v in broker.cases.items())
    posts = [p for m, _, p in broker.calls if m == "POST"]
    assert [len(p["case_id"]) for p in posts] == [2, 2, 1]
    assert all(p["reason"] == pmw.REASON and p["dry_run"] == "false" for p in posts)


def test_a_leased_or_done_case_is_never_among_the_ids_parked():
    broker = FakeBroker(queue(west_pending=2, west_leased=1, west_done=1, west_quarantined=1))
    found = pmw.pending_west(broker, batch=100)
    pmw.park_all(broker, [c["case_id"] for c in found], sleep=lambda s: None, say=lambda s: None)
    assert broker.cases["wl000"]["state"] == "leased"
    assert broker.cases["wd000"]["state"] == "done"
    assert broker.cases["wq000"]["state"] == "quarantined"
