"""Running a fleet update: what the panel needs beyond "point at a build".

An operator who set a target saw every worker as "behind" and nothing about
whether anything was happening; a target with no file for a platform left the
nodes on it stranded silently; promoting a canary was two calls that could be
left half done; deleting a build a node was running was allowed; and two
clients sharing a worker_id looked like one worker whose build kept changing.
Each is pinned here, with the control that the ordinary path still works.
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
OLD, NEW = "1.14.0.827+aaaaaaaa", "1.14.0.827+bbbbbbbb"
SHA = "ab" * 32
W = {"Authorization": "Bearer w"}
T0 = 1_800_000_000


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "r.sqlite"))


@pytest.fixture()
def admin(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "a.sqlite"), tokens=["w"], readonly_tokens=[]))
    r = c.post("/v1/auth/setup", json={"username": "ada", "password": PW, "setup_token": "w"})
    assert r.status_code == 200, r.text
    return c


def publish(c, build, *platforms):
    for p in platforms:
        r = c.post("/v1/releases", json={"build": build, "platform": p,
                                         "file": f"E3D-{build}-{p}", "sha256": SHA})
        assert r.status_code == 200, r.text


def worker_row(conn, worker_id, *cols):
    row = conn.execute("SELECT %s FROM workers WHERE worker_id = ?" % ", ".join(cols),
                       (worker_id,)).fetchone()
    return tuple(row[c] for c in cols)


# -- 1. what the node says about its update ------------------------------------

def test_a_nodes_own_account_of_its_update_is_kept_until_it_is_there(conn):
    db.lease(conn, "foam-1", build=OLD, platform="win-x64", now=T0)
    db.register_release(conn, NEW, "win-x64", "f.exe", SHA, by="ada", now=T0)
    db.set_target(conn, NEW, by="ada", now=T0)
    db.node_release(conn, "foam-1", "win-x64", OLD,
                    state="the broker wants build X and has no file on the share yet", now=T0 + 60)
    assert worker_row(conn, "foam-1", "update_state", "update_state_at", "release_asked_at") == \
        ("the broker wants build X and has no file on the share yet", T0 + 60, T0 + 60)
    # An ask without a word keeps the last word, and moves the ask.
    db.node_release(conn, "foam-1", "win-x64", OLD, now=T0 + 120)
    assert worker_row(conn, "foam-1", "update_state", "release_asked_at") == \
        ("the broker wants build X and has no file on the share yet", T0 + 120)
    # On target: nothing left to say, whatever it says.
    db.node_release(conn, "foam-1", "win-x64", NEW, state="stale", now=T0 + 180)
    assert worker_row(conn, "foam-1", "update_state", "update_state_at") == (None, None)


def test_a_node_that_never_asks_is_counted_as_one_that_cannot_update(conn):
    """A Python worker, or an E3D.exe from before releases: told to move, it
    would never hear. The summary says how many of those the fleet has."""
    db.lease(conn, "python-1", build=OLD, platform="linux-x64", now=T0)
    db.lease(conn, "e3d-1", build=OLD, platform="linux-x64", now=T0)
    db.register_release(conn, NEW, "linux-x64", "E3D", SHA, by="ada", now=T0)
    db.set_target(conn, NEW, by="ada", now=T0)
    db.node_release(conn, "e3d-1", "linux-x64", OLD, now=T0 + 5)
    fleet = db.list_releases(conn, now=T0 + 10)["fleet"]
    assert (fleet["behind"], fleet["cannot_update"], fleet["on_target"]) == (2, 1, 0)


# -- 2. a target every platform can reach ---------------------------------------

def test_a_target_with_no_file_for_a_live_platform_is_refused_unless_forced(admin):
    admin.post("/v1/lease", headers=W, json={"worker_id": "win", "build": OLD, "platform": "win-x64"})
    admin.post("/v1/lease", headers=W, json={"worker_id": "mac", "build": OLD, "platform": "osx-arm64"})
    publish(admin, NEW, "win-x64")
    r = admin.put("/v1/releases/target", json={"build": NEW})
    assert r.status_code == 409 and "osx-arm64" in r.json()["detail"]
    assert admin.get("/v1/releases").json()["target_build"] is None, "nothing moved"
    assert admin.put("/v1/releases/target", json={"build": NEW, "force": True}).status_code == 200
    # ...and the catalog says which builds leave whom behind.
    row = {b["build"]: b for b in admin.get("/v1/releases").json()["builds"]}[NEW]
    assert (row["published"], row["missing_platforms"]) == (["win-x64"], ["osx-arm64"])


def test_a_canary_is_only_pointed_at_a_file_its_platform_has(admin):
    admin.post("/v1/lease", headers=W, json={"worker_id": "mac", "build": OLD, "platform": "osx-arm64"})
    publish(admin, NEW, "win-x64")
    assert admin.put("/v1/workers/mac/target", json={"build": NEW}).status_code == 409
    assert admin.put("/v1/workers/mac/target", json={"build": NEW, "force": True}).status_code == 200
    # A worker that never said its platform cannot be checked, so it is not refused.
    admin.post("/v1/lease", headers=W, json={"worker_id": "quiet"})
    assert admin.put("/v1/workers/quiet/target", json={"build": NEW}).status_code == 200


# -- 3. promote, roll back, kill switch -------------------------------------------

def test_promoting_a_canary_is_one_call_that_clears_the_override_and_sets_the_target(admin):
    for w in ("foam-1", "foam-2"):
        admin.post("/v1/lease", headers=W, json={"worker_id": w, "build": OLD, "platform": "win-x64"})
    publish(admin, OLD, "win-x64")
    publish(admin, NEW, "win-x64")
    admin.put("/v1/releases/target", json={"build": OLD})
    admin.put("/v1/workers/foam-1/target", json={"build": NEW})
    r = admin.post("/v1/releases/promote", json={"worker_id": "foam-1"})
    assert r.status_code == 200, r.text
    assert (r.json()["target_build"], r.json()["previous_target"]) == (NEW, OLD)
    q = {"platform": "win-x64", "build": OLD}
    canary = admin.get("/v1/node/release", headers=W, params={"worker_id": "foam-1", **q}).json()
    assert (canary["target_build"], canary["canary"]) == (NEW, False), "it follows the fleet again"
    other = admin.get("/v1/node/release", headers=W, params={"worker_id": "foam-2", **q}).json()
    assert other["target_build"] == NEW
    assert admin.post("/v1/releases/promote", json={"worker_id": "foam-2"}).status_code == 409, "not a canary"
    assert admin.post("/v1/releases/promote", json={"worker_id": "nobody"}).status_code == 404


def test_rolling_back_returns_to_the_target_before_and_can_block_the_one_left(admin):
    publish(admin, OLD, "win-x64")
    publish(admin, NEW, "win-x64")
    assert admin.post("/v1/releases/rollback", json={}).status_code == 409, "nothing remembered yet"
    admin.put("/v1/releases/target", json={"build": OLD})
    admin.put("/v1/releases/target", json={"build": NEW})
    r = admin.post("/v1/releases/rollback", json={"block": True})
    assert r.status_code == 200, r.text
    assert (r.json()["target_build"], r.json()["previous_target"]) == (OLD, NEW)
    assert r.json()["blocked_builds"] == [NEW]
    # The kill switch is felt at the next lease, not at the next case.
    refused = admin.post("/v1/lease", headers=W, json={"worker_id": "n", "build": NEW})
    assert refused.status_code == 426


def test_the_target_history_is_in_the_audit_trail(conn):
    db.register_release(conn, OLD, "win-x64", "f", SHA, by="ada", now=T0)
    db.set_target(conn, OLD, by="ada", now=T0)
    db.set_target(conn, NEW, by="ada", now=T0 + 1)
    db.roll_back(conn, by="ada", now=T0 + 2)
    events = [(r["event"], r["detail"]) for r in conn.execute(
        "SELECT event, detail FROM events WHERE case_id IS NULL ORDER BY id").fetchall()]
    assert ("rollback", f"{NEW} -> {OLD}") in events
    assert ("setting", f"previous_target = {OLD}") in events


# -- 4. stuck ---------------------------------------------------------------------

def test_a_worker_behind_its_target_longer_than_the_switch_should_take_is_stuck(conn):
    db.lease(conn, "foam-1", build=OLD, platform="win-x64", now=T0)
    db.lease(conn, "foam-2", build=OLD, platform="win-x64", now=T0)
    db.register_release(conn, NEW, "win-x64", "f.exe", SHA, by="ada", now=T0)
    db.set_target(conn, NEW, by="ada", now=T0)
    db.set_setting(conn, "target_apply", "now", by="ada", now=T0)
    soon = db.list_releases(conn, now=T0 + 60)["fleet"]
    assert (soon["behind"], soon["stuck"]) == (2, 0)
    late = db.list_releases(conn, now=T0 + db.STUCK_AFTER["now"] + 1)
    assert late["fleet"]["stuck"] == 2 and late["target_since"] == T0
    # A canary's clock is its own.
    db.set_worker_target(conn, "foam-2", NEW, by="ada", now=T0 + 3000)
    assert worker_row(conn, "foam-2", "target_set_at") == (T0 + 3000,)
    assert db.list_releases(conn, now=T0 + 3100)["fleet"]["stuck"] == 1


# -- 6. what a build has done, as rates and a mean --------------------------------

def test_a_build_is_measured_by_rate_and_mean_wall_time_not_counts_alone(conn):
    db.add_cases(conn, [{"case_id": f"c{i}", "spec": {"lat": 1.0, "lon": 2.0, "recipe": "r"},
                         "recipe": "r", "city_cluster": "x", "split": "train"} for i in range(4)])
    a, b, c, _ = db.lease(conn, "foam-1", count=4, build=NEW, now=T0)
    db.complete(conn, a.lease_id, "file:///a", metrics={"eddy3d_build": NEW, "wall_seconds": 100},
                now=T0 + 1000)
    # No clock of its own: the lease's age, 300 s.
    db.complete(conn, b.lease_id, "file:///b", metrics={"eddy3d_build": NEW, "unconverged_count": 1},
                now=T0 + 300)
    db.fail(conn, c.lease_id, "boom", now=T0 + 10)
    row = {x["build"]: x for x in db.list_releases(conn, now=T0 + 2000)["builds"]}[NEW]
    assert (row["done"], row["failed"], row["unconverged"]) == (2, 1, 1)
    assert row["mean_wall_seconds"] == 200
    assert (row["unconverged_rate"], row["failed_rate"]) == (0.5, 0.333)


# -- 7. delete guard --------------------------------------------------------------

def test_a_build_a_live_worker_runs_or_a_canary_targets_cannot_be_deleted(admin):
    publish(admin, OLD, "win-x64")
    publish(admin, NEW, "win-x64")
    admin.post("/v1/lease", headers=W, json={"worker_id": "foam-1", "build": OLD, "platform": "win-x64"})
    r = admin.delete(f"/v1/releases/{OLD}")
    assert r.status_code == 409 and "foam-1" in r.json()["detail"]
    admin.put("/v1/workers/foam-1/target", json={"build": NEW})
    r = admin.delete(f"/v1/releases/{NEW}")
    assert r.status_code == 409 and "canary" in r.json()["detail"]
    admin.put("/v1/workers/foam-1/target", json={"build": None})
    assert admin.delete(f"/v1/releases/{NEW}").status_code == 200, "nobody runs or targets it"


# -- 8. two clients, one id -------------------------------------------------------

def test_a_build_flipping_back_and_forth_under_one_id_is_called_out(conn):
    """An E3D node and a Python worker started from the same machine.env, each
    overwriting the other's build on every poll."""
    db.lease(conn, "ws-01", build=NEW, now=T0)
    db.lease(conn, "ws-01", now=T0 + 60)              # the Python worker: undeclared
    db.lease(conn, "ws-01", build=NEW, now=T0 + 120)
    assert worker_row(conn, "ws-01", "id_conflict") == (None,), "one flip may be a rollback"
    db.lease(conn, "ws-01", now=T0 + 180)
    assert worker_row(conn, "ws-01", "id_conflict", "id_conflict_at") == (f"undeclared and {NEW}", T0 + 180)
    said = [r["detail"] for r in conn.execute("SELECT detail FROM events WHERE event='shared-id'").fetchall()]
    assert said == [f"undeclared and {NEW}"]


def test_an_ordinary_update_is_one_audited_change_and_no_conflict(conn):
    db.lease(conn, "foam-1", build=OLD, now=T0)
    db.lease(conn, "foam-1", build=NEW, now=T0 + 60)
    db.lease(conn, "foam-1", build=NEW, now=T0 + 120)
    changes = [r["detail"] for r in conn.execute("SELECT detail FROM events WHERE event='build-changed'").fetchall()]
    assert changes == [f"{OLD} -> {NEW}"]
    assert worker_row(conn, "foam-1", "id_conflict") == (None,)
    # A slow flip -- an hour later -- is a rollback, not a shared id.
    db.lease(conn, "foam-1", build=OLD, now=T0 + 120 + db.ID_CONFLICT_WINDOW + 1)
    db.lease(conn, "foam-1", build=NEW, now=T0 + 120 + 2 * db.ID_CONFLICT_WINDOW + 2)
    assert worker_row(conn, "foam-1", "id_conflict") == (None,)


# -- 9. the panel's copy ----------------------------------------------------------

def test_the_catalog_carries_who_published_what_and_the_repo_for_commit_links(admin):
    r = admin.post("/v1/releases", json={"build": NEW, "platform": "win-x64", "file": "f.exe",
                                         "sha256": SHA, "notes": "fixes the dense-site crash"})
    assert r.status_code == 200
    assert admin.put("/v1/releases/policy", json={"release_repo": "Eddy3D-Dev/Eddy3D"}).status_code == 200
    got = admin.get("/v1/releases").json()
    rel = got["releases"][0]
    assert (rel["notes"], rel["added_by"]) == ("fixes the dense-site crash", "ada") and rel["added_at"]
    assert got["release_repo"] == "Eddy3D-Dev/Eddy3D"
    assert got["stuck_after"]["case"] == db.STUCK_AFTER["case"]
    assert admin.put("/v1/releases/policy", json={"release_repo": "not a repo"}).status_code == 422
    assert admin.put("/v1/releases/policy", json={"release_repo": ""}).json()["release_repo"] is None


def test_the_fleet_summary_counts_what_the_panels_first_line_says(admin):
    for w, b in (("a", OLD), ("b", NEW), ("c", None)):
        admin.post("/v1/lease", headers=W, json={"worker_id": w, "build": b, "platform": "win-x64"})
    publish(admin, NEW, "win-x64")
    admin.put("/v1/releases/target", json={"build": NEW})
    admin.get("/v1/node/release", headers=W, params={"worker_id": "a", "platform": "win-x64", "build": OLD,
                                                    "failed_build": NEW, "failed_reason": "exit 1"})
    fleet = admin.get("/v1/releases").json()["fleet"]
    assert fleet == {"workers": 3, "on_target": 1, "behind": 1, "stuck": 0, "undeclared": 1,
                     "update_failed": 1, "cannot_update": 0, "canaries": 0}


def test_the_deployed_commit_is_reported_beside_the_version(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "e044a1470f0f4c2b")
    c = TestClient(create_app(db_path=str(tmp_path / "c.sqlite"), tokens=["w"], readonly_tokens=[]))
    assert c.get("/healthz").json()["commit"] == "e044a147"
    assert c.get("/v1/status", headers=W).json()["commit"] == "e044a147"
    monkeypatch.delenv("RENDER_GIT_COMMIT")
    assert c.get("/healthz").json()["commit"] is None
