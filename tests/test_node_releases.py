"""Updating nodes while a campaign runs.

A campaign lasts months and its nodes are updated in the middle of it. Three
things have to hold, and each was broken or absent before this:

* the broker knows WHICH BUILD every worker is (the product version is the same
  for every push, so it cannot tell two nodes apart);
* a case is only ever handed to a node that knows its RECIPE -- selection by
  prefix once solved a v4 case as v3 and archived it labelled v4;
* an operator can point the fleet, or one canary, at a build, and DRAIN a node,
  without the broker ever serving a byte of code.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

PW = "a-sufficiently-long-passphrase"
V3, V4 = "fixed-box-1008/of12-v3", "cyl-1008/of12-v4"
OLD, NEW = "1.14.0.827+aaaaaaaa", "1.14.0.827+bbbbbbbb"
SHA = "ab" * 32


def case(i: int, recipe: str) -> dict:
    return {"case_id": f"c{i:03d}", "spec": {"lat": 1.0, "lon": 2.0, "recipe": recipe},
            "recipe": recipe, "city_cluster": "x", "split": "train"}


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "r.sqlite"))


@pytest.fixture()
def admin(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=[]))
    # A broker that holds a write token demands it to create the first account.
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW, "setup_token": "w"})
    assert r.status_code == 200, r.text
    return c


W = {"Authorization": "Bearer w"}


# -- which build, which recipes ------------------------------------------------

def test_a_lease_records_the_build_and_it_follows_the_worker_through_an_update(conn):
    db.add_cases(conn, [case(1, V4), case(2, V4)])
    db.lease(conn, "foam-1", build=OLD, version="1.14.0.827", platform="win-x64", recipes=[V3, V4])
    row = conn.execute("SELECT build, platform, recipes FROM workers WHERE worker_id='foam-1'").fetchone()
    assert (row["build"], row["platform"]) == (OLD, "win-x64")
    assert V4 in row["recipes"]
    # The same worker_id, after its node was updated.
    db.lease(conn, "foam-1", build=NEW, version="1.14.0.827", platform="win-x64", recipes=[V3, V4])
    assert conn.execute("SELECT build FROM workers WHERE worker_id='foam-1'").fetchone()["build"] == NEW


def test_a_node_is_only_handed_recipes_it_declared(conn):
    """The v3-only node must never see the v4 case, however long it waits."""
    db.add_cases(conn, [case(1, V4), case(2, V3)])
    got = db.lease(conn, "old-node", count=5, build=OLD, recipes=[V3])
    assert [g.case_id for g in got] == ["c002"]
    assert db.lease(conn, "old-node", count=5, build=OLD, recipes=[V3]) == []
    # ...and the v4 case is still there for a node that knows it.
    assert [g.case_id for g in db.lease(conn, "new-node", count=5, build=NEW, recipes=[V3, V4])] == ["c001"]


def test_a_worker_that_declares_nothing_is_left_unfiltered(conn):
    """The control, and the backwards-compatible default: a worker from before
    declarations (or a script runner, whose contract is its author's) leases as
    it always did. `require_build` is the fence for a campaign that needs one."""
    db.add_cases(conn, [case(1, V4), case(2, V3)])
    assert len(db.lease(conn, "legacy", count=5)) == 2


def test_the_recipe_filter_also_guards_a_resume(conn):
    db.add_cases(conn, [case(1, V4)])
    assert db.lease(conn, "n", count=1, recipes=[V3], resume_case_ids=["c001"]) == []


# -- fencing builds off --------------------------------------------------------

def test_a_campaign_can_insist_on_a_declared_build(conn):
    assert db.lease_refusal(conn, None) is None            # default: everyone may lease
    db.set_setting(conn, "require_build", "1", by="ada")
    assert "declare" in db.lease_refusal(conn, None)
    assert db.lease_refusal(conn, OLD) is None


def test_a_named_bad_build_is_refused_and_says_so(admin):
    admin.post("/v1/cases", headers=W, json=[])
    r = admin.put("/v1/releases/policy", json={"blocked_builds": [OLD]})
    assert r.status_code == 200 and r.json()["blocked_builds"] == [OLD]
    refused = admin.post("/v1/lease", headers=W, json={"worker_id": "n", "build": OLD})
    assert refused.status_code == 426, refused.text      # not 401/403: the credential is fine
    assert OLD in refused.json()["detail"]
    assert admin.post("/v1/lease", headers=W, json={"worker_id": "n", "build": NEW}).status_code == 200


# -- the target, the canary, the catalog ---------------------------------------

def test_the_fleet_target_names_a_file_and_the_hash_it_must_have(admin):
    admin.post("/v1/lease", headers=W, json={"worker_id": "foam-1", "build": OLD, "platform": "win-x64"})
    assert admin.post("/v1/releases", json={"build": NEW, "platform": "win-x64",
                                            "file": f"E3D-{NEW}-win-x64.exe", "sha256": SHA}).status_code == 200
    assert admin.put("/v1/releases/target", json={"build": NEW, "apply": "direction"}).status_code == 200

    got = admin.get("/v1/node/release", headers=W,
                    params={"worker_id": "foam-1", "platform": "win-x64", "build": OLD}).json()
    assert (got["target_build"], got["file"], got["sha256"], got["apply"]) == \
        (NEW, f"E3D-{NEW}-win-x64.exe", SHA, "direction")
    assert got["current"] is False

    # Already there: nothing to fetch, and it says so.
    same = admin.get("/v1/node/release", headers=W,
                     params={"worker_id": "foam-1", "platform": "win-x64", "build": NEW}).json()
    assert same["current"] is True and same["file"] is None


def test_a_build_with_no_file_for_this_platform_offers_nothing_to_fetch(admin):
    admin.post("/v1/releases", json={"build": NEW, "platform": "win-x64", "file": "f.exe", "sha256": SHA})
    admin.put("/v1/releases/target", json={"build": NEW})
    got = admin.get("/v1/node/release", headers=W,
                    params={"worker_id": "mac", "platform": "osx-arm64", "build": OLD}).json()
    assert got["target_build"] == NEW and got["file"] is None


def test_a_target_nobody_published_is_refused(admin):
    """Every node would learn it should move and none of them could."""
    r = admin.put("/v1/releases/target", json={"build": "9.9.9+deadbeef"})
    assert r.status_code == 409 and "no published file" in r.json()["detail"]


def test_the_canary_moves_ONE_worker_and_leaves_the_fleet_alone(admin):
    for w in ("foam-1", "foam-2"):
        admin.post("/v1/lease", headers=W, json={"worker_id": w, "build": OLD, "platform": "win-x64"})
    admin.post("/v1/releases", json={"build": NEW, "platform": "win-x64", "file": "f.exe", "sha256": SHA})
    assert admin.put("/v1/workers/foam-1/target", json={"build": NEW}).status_code == 200

    q = {"platform": "win-x64", "build": OLD}
    canary = admin.get("/v1/node/release", headers=W, params={"worker_id": "foam-1", **q}).json()
    other = admin.get("/v1/node/release", headers=W, params={"worker_id": "foam-2", **q}).json()
    assert (canary["target_build"], canary["canary"]) == (NEW, True)
    assert other["target_build"] is None

    # Promoting is clearing the override and setting the fleet's target.
    admin.put("/v1/workers/foam-1/target", json={"build": None})
    admin.put("/v1/releases/target", json={"build": NEW})
    assert admin.get("/v1/node/release", headers=W, params={"worker_id": "foam-2", **q}).json()["target_build"] == NEW


def test_the_target_in_use_cannot_be_deleted_out_from_under_the_fleet(admin):
    admin.post("/v1/releases", json={"build": NEW, "platform": "win-x64", "file": "f.exe", "sha256": SHA})
    admin.put("/v1/releases/target", json={"build": NEW})
    assert admin.delete(f"/v1/releases/{NEW}").status_code == 409


def test_pointing_the_fleet_at_code_needs_an_ADMIN_never_a_machine_token(admin, tmp_path):
    """This is remote code execution by design, so who may do it is the security
    model. A worker credential that could retarget the fleet would let one
    compromised workstation take the rest."""
    anonymous = TestClient(admin.app)
    body = {"build": NEW, "platform": "win-x64", "file": "f.exe", "sha256": SHA}
    for call in (lambda c: c.post("/v1/releases", headers=W, json=body),
                 lambda c: c.put("/v1/releases/target", headers=W, json={"build": NEW}),
                 lambda c: c.put("/v1/releases/policy", headers=W, json={"require_build": True}),
                 lambda c: c.put("/v1/workers/x/target", headers=W, json={"build": NEW}),
                 lambda c: c.post("/v1/workers/x/drain", headers=W, json={})):
        assert call(anonymous).status_code in (401, 403)


def test_every_change_to_what_the_fleet_runs_is_in_the_audit_trail(conn):
    """Who pointed thousands of machine-hours at which code, and when."""
    db.lease(conn, "foam-1")
    db.register_release(conn, NEW, "win-x64", "f.exe", SHA, by="ada")
    db.set_setting(conn, "target_build", NEW, by="ada")
    db.set_worker_target(conn, "foam-1", NEW, by="ada")
    db.set_worker_drain(conn, "foam-1", True, "reboot", by="ada")
    trail = [(r["event"], r["worker_id"], r["detail"]) for r in conn.execute(
        "SELECT event, worker_id, detail FROM events WHERE case_id IS NULL ORDER BY id").fetchall()]
    assert [e for e, _, _ in trail] == ["release", "setting", "worker-target", "drain"]
    assert all(who == "ada" for _, who, _ in trail)
    assert NEW in trail[1][2] and "reboot" in trail[3][2]


# -- drain ---------------------------------------------------------------------

def test_a_draining_worker_gets_no_new_case_but_may_resume_its_OWN(conn):
    """How a node restarts onto a new build mid-case without losing the case: it
    keeps its lease across the restart and asks for the same case back."""
    db.add_cases(conn, [case(1, V4), case(2, V4)])
    mine = db.lease(conn, "foam-1", count=1)[0]
    db.set_worker_drain(conn, "foam-1", True, "update to " + NEW, by="ada")

    assert db.lease(conn, "foam-1", count=5) == [], "no NEW work"
    again = db.lease(conn, "foam-1", count=1, resume_case_ids=[mine.case_id])
    assert [g.case_id for g in again] == [mine.case_id]
    assert again[0].attempt == mine.attempt, "its own case costs no attempt"
    # A PENDING case asked for by name is still new work.
    assert db.lease(conn, "foam-1", count=1, resume_case_ids=["c002"]) == []

    db.set_worker_drain(conn, "foam-1", False, by="ada")
    assert len(db.lease(conn, "foam-1", count=5)) == 1


def test_a_drained_node_is_told_why(admin):
    admin.post("/v1/lease", headers=W, json={"worker_id": "foam-1"})
    assert admin.post("/v1/workers/foam-1/drain", json={"reason": "new GPU"}).status_code == 200
    got = admin.get("/v1/node/release", headers=W, params={"worker_id": "foam-1"}).json()
    assert (got["drain"], got["drain_reason"]) == (True, "new GPU")
    assert admin.post("/v1/workers/nobody/drain", json={}).status_code == 404


# -- what a build has DONE: the evidence a canary is promoted on ---------------

def test_a_finished_case_is_counted_for_the_build_its_ARCHIVE_names(conn):
    """Not the worker's current build: a case can be finished by a node that was
    updated after it started, and the metrics say which build wrote the result."""
    db.add_cases(conn, [case(1, V4), case(2, V4), case(3, V4)])
    a, b, c = db.lease(conn, "foam-1", count=3, build=OLD)
    db.complete(conn, a.lease_id, "file:///a", metrics={"eddy3d_build": NEW, "unconverged_count": 2})
    db.complete(conn, b.lease_id, "file:///b", metrics={"eddy3d_build": NEW})
    db.fail(conn, c.lease_id, "boom")          # no archive: counted for the worker's build

    builds = {x["build"]: x for x in db.list_releases(conn)["builds"]}
    assert (builds[NEW]["done"], builds[NEW]["unconverged"], builds[NEW]["failed"]) == (2, 1, 0)
    assert (builds[OLD]["done"], builds[OLD]["failed"]) == (0, 1)
    assert builds[OLD]["workers"] == 1


def test_a_node_that_rolled_back_says_so_once_and_it_clears_when_it_gets_there(conn):
    """An unattended update must never hide that it failed."""
    db.lease(conn, "foam-1", build=OLD, platform="win-x64")
    for _ in range(3):                                   # it says so with EVERY ask
        db.node_release(conn, "foam-1", "win-x64", OLD, failed_build=NEW, failed_reason="exit -532462766 at startup")
    row = conn.execute("SELECT update_failed FROM workers WHERE worker_id='foam-1'").fetchone()
    assert NEW in row["update_failed"] and "startup" in row["update_failed"]
    audited = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event='update-failed'").fetchone()["n"]
    assert audited == 1, "recorded once per failure, not once per minute"

    db.node_release(conn, "foam-1", "win-x64", NEW)       # later: on the build, nothing failed
    assert conn.execute("SELECT update_failed FROM workers WHERE worker_id='foam-1'").fetchone()["update_failed"] is None
