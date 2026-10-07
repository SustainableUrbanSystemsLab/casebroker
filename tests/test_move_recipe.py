"""Moving a queue from one recipe to another, a batch at a time (scripts/move_recipe.py).

The respec endpoint takes case ids, never a recipe, so the script pages the old
recipe's pending cases and hands them over. What has to hold: every pending case
moves, a leased one never does, and a broker that moves nothing cannot make the
loop spin.
"""
from __future__ import annotations

import importlib.util
import pathlib
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("move_recipe", ROOT / "scripts" / "move_recipe.py")
mover = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mover)

V4, V6 = "cyl-1008/of12-v4", "cyl-1008/of12-v6"


class FakeBroker:
    """GET /v1/cases and POST /v1/cases/respec over an in-memory queue, as the broker answers them."""

    def __init__(self, cases: dict[str, dict], refuse: bool = False, lease_on_respec: str | None = None):
        self.cases = cases
        self.refuse = refuse
        self.lease_on_respec = lease_on_respec
        self.calls: list[tuple[str, str, dict]] = []

    def call(self, method, path, params):
        self.calls.append((method, path, params))
        if method == "GET":
            rows = [{"case_id": k, **v} for k, v in sorted(self.cases.items())
                    if v["recipe"] == params["recipe"] and v["state"] == params["state"]]
            return {"cases": rows[:params["limit"]], "total": len(rows)}
        if self.lease_on_respec in self.cases:            # a node leases it between the page and the move
            self.cases[self.lease_on_respec]["state"] = "leased"
            self.lease_on_respec = None
        moved, skipped = 0, []
        for cid in params["case_id"]:
            row = self.cases[cid]
            if self.refuse or row["state"] != "pending":
                skipped.append({"case_id": cid, "why": "leased" if row["state"] == "leased" else "refused"})
                continue
            if params["dry_run"] == "false":
                row["state"] = "quarantined"
                self.cases["new-" + cid] = {"recipe": params["recipe"], "state": "pending"}
            moved += 1
        return {"moved": moved if params["dry_run"] == "false" else 0, "matched": moved, "skipped": skipped}


def queue(pending: int, leased: int = 0, done: int = 0) -> dict[str, dict]:
    out = {f"p{i:03d}": {"recipe": V4, "state": "pending"} for i in range(pending)}
    out.update({f"l{i:03d}": {"recipe": V4, "state": "leased"} for i in range(leased)})
    out.update({f"d{i:03d}": {"recipe": V4, "state": "done"} for i in range(done)})
    return out


def run(broker, **kw):
    said = []
    totals = mover.move(broker, V4, V6, sleep=lambda s: None, say=said.append, **kw)
    return totals, said


def test_every_pending_case_moves_in_batches_and_leased_and_done_ones_stay():
    broker = FakeBroker(queue(pending=250, leased=3, done=4))
    totals, _ = run(broker, batch=100)
    assert totals == {"passes": 3, "moved": 250, "skipped": 0}
    states = [v["state"] for k, v in broker.cases.items() if v["recipe"] == V4]
    assert states.count("pending") == 0 and states.count("leased") == 3 and states.count("done") == 4
    assert sum(1 for v in broker.cases.values() if v["recipe"] == V6 and v["state"] == "pending") == 250
    asked = [p["case_id"] for m, _, p in broker.calls if m == "POST"]
    assert [len(a) for a in asked] == [100, 100, 50]
    assert not any(cid.startswith(("l", "d")) for a in asked for cid in a)


def test_a_case_leased_between_the_page_and_the_move_is_skipped_not_lost():
    broker = FakeBroker(queue(pending=3), lease_on_respec="p001")
    totals, said = run(broker, batch=100)
    assert totals == {"passes": 1, "moved": 2, "skipped": 1}
    assert broker.cases["p001"]["state"] == "leased" and "p001: leased" in said[0]


def test_a_broker_that_moves_nothing_stops_the_loop():
    broker = FakeBroker(queue(pending=5), refuse=True)
    totals, said = run(broker)
    assert totals == {"passes": 1, "moved": 0, "skipped": 5}
    assert said[-1] == "a pass moved nothing; stopping"


def test_the_moves_are_real_and_carry_the_reason():
    broker = FakeBroker(queue(pending=1))
    run(broker, reason="v4 -> v6")
    (_, path, params), = [c for c in broker.calls if c[0] == "POST"]
    assert path == "/v1/cases/respec" and params["recipe"] == V6
    assert params["dry_run"] == "false" and params["reason"] == "v4 -> v6" and params["limit"] == 1


def test_the_ids_go_as_repeated_query_parameters(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"moved": 2}'

    monkeypatch.setattr(mover.urllib.request, "urlopen", lambda req, timeout: seen.append(req) or Response())
    mover.respec(mover.Broker("https://b/", "t"), ["a", "b"], V6, None, dry_run=True)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0].full_url).query)
    assert query["case_id"] == ["a", "b"] and query["dry_run"] == ["true"]
    assert seen[0].get_method() == "POST"
