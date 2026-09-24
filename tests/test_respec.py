"""Moving a case to another recipe, so the nodes that cannot build it stop trying.

v2-00ed64225d4979d7 (Faridabad) cannot be meshed under cyl-1008/of12-v4: snappy
aborts in its post-snap face merge on every machine and rank count it met, and
cyl-1008/of12-v5 meshes it (Mesh OK, 2026-09-24). Reopening it only handed it to
the next v4 node to fail again -- and there was no way to say "this site is a v5
case now" short of editing the database.

A move is a NEW case, not an edited one: a case id is a function of the site and
the recipe (DOMAIN.md, invariant 1). Pinned here: only a node that declares the
new recipe is handed it, nobody can resume the old recipe's mesh as it, a live
lease and a finished result are never touched, and reopen cannot quietly bring the
old recipe back by the error that got the case moved.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402

V4, V5 = "cyl-1008/of12-v4", "cyl-1008/of12-v5"


def site(i: int) -> tuple[float, float]:
    return 28.40 + i * 0.01, 77.30


def make_db(tmp_path, n: int = 3):
    conn = db.connect(str(tmp_path / "b.sqlite"))
    rows = []
    for i in range(n):
        lat, lon = site(i)
        rows.append({"case_id": ids.case_id(lat, lon, V4),
                     "spec": {"lat": lat, "lon": lon, "recipe": V4, "dirs": [0, 30]},
                     "recipe": V4, "city_cluster": "c31_75", "lcz": "LCZ3",
                     "split": ids.split_for("c31_75"), "priority": 40, "max_attempts": 4,
                     "labels": {"campaign": "v2"}})
    db.add_cases(conn, rows)
    # A node that knows v5, so the recipe is not a typo; it holds nothing yet.
    assert db.lease(conn, "v5-node", 1, 900, build="1.0+v5", recipes=[V5]) == []
    return conn


def old_id(i: int = 0) -> str:
    return ids.case_id(*site(i), V4)


def new_id(i: int = 0) -> str:
    return ids.case_id(*site(i), V5)


def row(conn, case_id):
    r = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    return dict(r) if r else None


def fail_once(conn, worker: str, error: str, now: int):
    got = db.lease(conn, worker, 1, 900, recipes=[V4], now=now)
    assert got, "expected the v4 node to be handed a case"
    db.fail(conn, got[0].lease_id, error, retryable=True, now=now + 60)
    return got[0].case_id


def test_the_moved_site_goes_only_to_a_node_that_declares_the_new_recipe(tmp_path):
    conn = make_db(tmp_path, n=1)

    out = db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False,
                          by="pat", reason="v4's snappy aborts; v5 meshes it")

    assert out["moved"] == 1 and out["known_to_builds"] == ["1.0+v5"]
    # The node that knows only the old recipe has nothing left to fail.
    assert db.lease(conn, "v4-node", 1, 900, recipes=[V4]) == []
    got = db.lease(conn, "v5-node", 1, 900, build="1.0+v5", recipes=[V5])
    assert [g.case_id for g in got] == [new_id()]
    assert got[0].spec["recipe"] == V5 and got[0].attempt == 1


def test_the_new_case_is_what_posting_the_site_under_the_new_recipe_would_make(tmp_path):
    conn = make_db(tmp_path, n=1)
    db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False)

    old, new = row(conn, old_id()), row(conn, new_id())
    assert new_id() != old_id(), "a case id is a function of site AND recipe"
    assert new["recipe"] == V5 and new["state"] == "pending" and new["attempts"] == 0
    assert json.loads(new["spec"]) == dict(json.loads(old["spec"]), recipe=V5)
    for col in ("city_cluster", "lcz", "split", "priority", "max_attempts"):
        assert new[col] == old[col], col
    assert db.get_case(conn, new_id())["labels"] == {"campaign": "v2"}

    # Posting the same site under the new recipe afterwards is the no-op it
    # would have been anyway -- the two routes cannot disagree.
    lat, lon = site(0)
    again = db.add_cases(conn, [{"case_id": ids.case_id(lat, lon, V5), "spec": {"lat": lat, "lon": lon},
                                 "recipe": V5, "city_cluster": "c31_75", "split": "train"}])
    assert again == {"added": 0, "skipped": 1, "labelled": 0}


def test_the_old_case_is_parked_and_says_where_its_site_went(tmp_path):
    conn = make_db(tmp_path, n=1)
    db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False, by="pat", reason="v4 cannot mesh it")

    old = db.get_case(conn, old_id())
    new = db.get_case(conn, new_id())
    assert old["state"] == "quarantined"
    # Nobody needs to look at it any more: the errors list is for what does.
    assert old["last_error"] is None
    assert old["moved_to"]["case_id"] == new_id() and old["moved_to"]["recipe"] == V5
    assert new["moved_from"]["case_id"] == old_id() and new["moved_from"]["recipe"] == V4
    assert (new["moved_from"]["by"], new["moved_from"]["reason"]) == ("pat", "v4 cannot mesh it")
    assert old["moved_from"] is None and new["moved_to"] is None
    trail = [dict(r)["detail"] for r in conn.execute(
        "SELECT detail FROM events WHERE case_id=? AND event='quarantined'", (old_id(),)).fetchall()]
    assert trail == ["moved to %s as %s by pat: v4 cannot mesh it" % (V5, new_id())]


def test_the_scan_changes_nothing_until_it_is_asked_to(tmp_path):
    """dry_run defaults to TRUE, as it does for reopen, the land audit and purge."""
    conn = make_db(tmp_path, n=1)

    out = db.respec_cases(conn, V5, case_ids=[old_id()])

    assert out["dry_run"] is True and out["moved"] == 0 and out["matched"] == 1
    assert out["examples"] == [{"case_id": old_id(), "state": "pending", "recipe": V4,
                                "new_case_id": new_id(), "new_exists": False}]
    assert row(conn, old_id())["state"] == "pending" and row(conn, new_id()) is None


def test_no_node_can_resume_the_old_recipe_s_mesh_as_the_moved_case(tmp_path):
    """v2-00427078fdfaa380 lost three attempts to exactly this after a reopen: the
    node holding its old scratch asked for it first and resumed the old mesh. A
    moved site has a new id, and the parked case is not a resumable one."""
    conn = make_db(tmp_path, n=1)
    fail_once(conn, "v4-node", "snappyHexMesh: Multiple outside loops", now=1_000_000)
    db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False)

    later = 1_000_000 + db.FAIL_COOLDOWN_SECONDS + 3600
    assert db.lease(conn, "v4-node", 1, 900, recipes=[V4], resume_case_ids=[old_id()], now=later) == []
    # Even a node that knows both recipes gets the NEW case -- whose scratch no
    # machine holds -- and never the old one back.
    both = db.lease(conn, "v4-node", 1, 900, recipes=[V4, V5], resume_case_ids=[old_id()], now=later)
    assert [g.case_id for g in both] == [new_id()]


def test_a_case_a_node_is_solving_is_left_to_it(tmp_path):
    conn = make_db(tmp_path, n=1)
    held = db.lease(conn, "v4-node", 1, 900, recipes=[V4])[0]

    out = db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False)

    assert out["moved"] == 0
    assert out["skipped"][0]["case_id"] == old_id() and "cancel it first" in out["skipped"][0]["why"]
    r = row(conn, old_id())
    assert (r["state"], r["lease_id"], r["lease_worker"]) == ("leased", held.lease_id, "v4-node")
    assert row(conn, new_id()) is None


def test_a_done_case_keeps_its_result(tmp_path):
    conn = make_db(tmp_path, n=1)
    held = db.lease(conn, "v4-node", 1, 900, recipes=[V4])[0]
    db.complete(conn, held.lease_id, "file:///done.tar.gz")

    out = db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False)

    assert out["moved"] == 0 and "result stands" in out["skipped"][0]["why"]
    assert row(conn, old_id())["state"] == "done" and row(conn, new_id()) is None


def test_a_recipe_nobody_has_heard_of_is_refused_as_a_typo(tmp_path):
    conn = make_db(tmp_path, n=1)
    with pytest.raises(ValueError, match="typo"):
        db.respec_cases(conn, "cyl-1008/of12-v55", case_ids=[old_id()], dry_run=False)
    assert row(conn, old_id())["state"] == "pending"
    with pytest.raises(ValueError, match="name the cases"):
        db.respec_cases(conn, V5)


def test_a_recipe_the_campaign_already_carries_is_accepted_before_any_node_declares_it(tmp_path):
    """Known is known: a recipe cases already carry is not a typo, even while no
    node declares it -- and the answer says nobody will take the cases yet."""
    conn = db.connect(str(tmp_path / "b.sqlite"))
    lat, lon = site(0)
    lat1, lon1 = site(1)
    db.add_cases(conn, [
        {"case_id": ids.case_id(lat, lon, V4), "spec": {"lat": lat, "lon": lon}, "recipe": V4,
         "city_cluster": "c", "split": "train"},
        {"case_id": ids.case_id(lat1, lon1, V5), "spec": {"lat": lat1, "lon": lon1}, "recipe": V5,
         "city_cluster": "c", "split": "train"}])

    out = db.respec_cases(conn, V5, case_ids=[ids.case_id(lat, lon, V4)])

    assert out["matched"] == 1 and out["known_to_builds"] == []


def test_moving_twice_moves_once(tmp_path):
    conn = make_db(tmp_path, n=1)
    db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False)

    again = db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False)

    assert again["moved"] == 0 and "already moved" in again["skipped"][0]["why"]
    assert conn.execute("SELECT COUNT(*) AS n FROM cases WHERE recipe=?", (V5,)).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE case_id=? AND event='respec'",
                        (new_id(),)).fetchone()["n"] == 1


def test_a_site_already_posted_under_the_new_recipe_is_linked_not_added_twice(tmp_path):
    conn = make_db(tmp_path, n=1)
    lat, lon = site(0)
    db.add_cases(conn, [{"case_id": new_id(), "spec": {"lat": lat, "lon": lon, "recipe": V5},
                         "recipe": V5, "city_cluster": "c31_75", "split": "train", "priority": 7}])

    out = db.respec_cases(conn, V5, case_ids=[old_id()], dry_run=False)

    assert out["moved"] == 1 and out["examples"][0]["new_exists"] is True
    assert row(conn, new_id())["priority"] == 7, "the case already there is left as it was"
    assert row(conn, old_id())["state"] == "quarantined"
    assert db.get_case(conn, new_id())["moved_from"]["case_id"] == old_id()


def test_reopen_by_the_error_that_got_a_case_moved_does_not_bring_the_old_recipe_back(tmp_path):
    """Reopen matches a case's LAST failure. If the move left the v4 failure as
    the last word, "reopen everything that failed with that" would put the old
    recipe back in the queue beside the new one."""
    conn = make_db(tmp_path, n=1)
    now = 1_000_000
    for _ in range(4):
        fail_once(conn, "v4-node", "FOAM FATAL ERROR: Multiple outside loops", now)
        now += db.FAIL_COOLDOWN_SECONDS + 3600
    assert row(conn, old_id())["state"] == "quarantined"

    moved = db.respec_cases(conn, V5, error_contains="multiple outside loops", dry_run=False)
    assert moved["moved"] == 1

    out = db.reopen_cases(conn, error_contains="Multiple outside loops", dry_run=False)
    assert out["reopened"] == 0 and row(conn, old_id())["state"] == "quarantined"


def test_selected_by_failure_text_it_moves_only_what_failed_that_way(tmp_path):
    conn = make_db(tmp_path, n=3)
    now = 1_000_000
    first = fail_once(conn, "v4-node", "FOAM FATAL ERROR: Multiple outside loops", now)
    # The same node, inside its cooldown for the first case, is handed the next.
    second = fail_once(conn, "v4-node", "Docker daemon is not running", now + 120)
    assert second != first

    out = db.respec_cases(conn, V5, error_contains="outside loops", dry_run=False)

    assert out["moved"] == 1 and out["examples"][0]["case_id"] == first
    assert row(conn, second)["state"] == "pending" and row(conn, second)["recipe"] == V4


def test_the_limit_bounds_what_it_changes(tmp_path):
    conn = make_db(tmp_path, n=5)
    every = [old_id(i) for i in range(5)]

    first = db.respec_cases(conn, V5, case_ids=every, dry_run=False, limit=2)
    assert (first["moved"], first["matched"], first["capped"]) == (2, 5, True)
    second = db.respec_cases(conn, V5, case_ids=every, dry_run=False, limit=2)
    assert second["moved"] == 2
    rest = db.respec_cases(conn, V5, case_ids=every, dry_run=False, limit=50)
    assert rest["moved"] == 1 and rest["capped"] is False
    assert conn.execute("SELECT COUNT(*) AS n FROM cases WHERE recipe=?", (V5,)).fetchone()["n"] == 5


def test_the_endpoint_needs_a_credential_and_changes_nothing_by_default(tmp_path):
    from fastapi.testclient import TestClient

    from casebroker.app import create_app

    app = create_app(str(tmp_path / "api.sqlite"), ["w"], ["r"])
    auth = {"Authorization": "Bearer w"}
    lat, lon = site(0)
    with TestClient(app) as c:
        assert c.post("/v1/cases", headers=auth, json=[
            {"lat": lat, "lon": lon, "recipe": V4, "city_cluster": "c31_75"}]).status_code == 200
        c.post("/v1/lease", headers=auth, json={"worker_id": "v5-node", "recipes": [V5], "build": "1.0+v5"})
        q = {"recipe": V5, "case_id": old_id()}

        assert c.post("/v1/cases/respec", params=q).status_code in (401, 403)
        assert c.post("/v1/cases/respec", params=q,
                      headers={"Authorization": "Bearer r"}).status_code in (401, 403)

        scan = c.post("/v1/cases/respec", params=q, headers=auth).json()
        assert scan["dry_run"] is True and scan["moved"] == 0 and scan["matched"] == 1

        assert c.post("/v1/cases/respec", params={"recipe": "no/such-recipe", "case_id": old_id()},
                      headers=auth).status_code == 422
        assert c.post("/v1/cases/respec", params={"recipe": V5}, headers=auth).status_code == 422

        done = c.post("/v1/cases/respec", params={**q, "dry_run": "false", "reason": "v4 cannot mesh it"},
                      headers=auth).json()
        assert done["moved"] == 1
        old = c.get("/v1/cases/" + old_id(), headers=auth).json()
        assert old["state"] == "quarantined" and old["moved_to"]["case_id"] == new_id()
        # A shared env token names nobody, and the trail does not pretend it does.
        assert old["moved_to"]["by"] is None and old["moved_to"]["reason"] == "v4 cannot mesh it"
