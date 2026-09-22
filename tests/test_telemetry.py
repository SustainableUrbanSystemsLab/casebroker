"""POST /v1/telemetry: structured reports from a node while it works a case.

What the contract with the node promises, and what each answer makes the node
do: 200 stored; 409 "stop sending for this case" (never retried); 413/422 the
body is wrong; and 404 ONLY from a broker that predates the route -- the node
then stops sending telemetry for the rest of its process, so this broker must
never answer 404 for anything else.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db  # noqa: E402
from casebroker.app import create_app  # noqa: E402

WRITE, READ = "w-secret", "r-secret"


def _cases(n=3):
    return [{"lat": 40.70 + i * 0.01, "lon": -74.0, "recipe": "v2", "city_cluster": "nyc",
             "lcz": "LCZ1", "spec": {"dirs": [0, 90]}} for i in range(n)]


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "tel.sqlite")
    c = TestClient(create_app(db_path=path, tokens=[WRITE], readonly_tokens=[READ]))
    c.headers.update({"Authorization": f"Bearer {WRITE}"})
    assert c.post("/v1/cases", json=_cases()).json()["added"] == 3
    return c, path


def _lease(c, worker="node-a", count=1):
    got = c.post("/v1/lease", json={"worker_id": worker, "count": count}).json()
    assert len(got) == count
    return got


def _post(c, lease, kind="site", data=None, case_id=None, **headers):
    body = {"lease_id": lease["lease_id"], "case_id": case_id or lease["case_id"],
            "kind": kind, "data": {"n_buildings": 12} if data is None else data}
    return c.post("/v1/telemetry", json=body, headers=headers or None)


def _telemetry(c, case_id):
    return c.get(f"/v1/cases/{case_id}").json()["telemetry"]


# -- auth ------------------------------------------------------------------------

def test_telemetry_needs_write_scope(env):
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, Authorization="").status_code == 401
    assert _post(c, lease, Authorization="Bearer wrong").status_code == 401
    # A shared dashboard link can read the campaign and must not be able to
    # write into it -- telemetry lands on the case record an operator trusts.
    assert _post(c, lease, Authorization=f"Bearer {READ}").status_code == 401
    assert _telemetry(c, lease["case_id"]) == {}
    assert _post(c, lease).status_code == 200


# -- what is stored ----------------------------------------------------------------

def test_a_post_is_stored_stamped_and_read_back_parsed(env):
    c, _ = env
    lease = _lease(c, "node-a")[0]
    r = _post(c, lease, "site", {"urban_form": {"bcr": 0.31}, "n_buildings": 40,
                                 "at": 1, "worker": "somebody-else"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    got = _telemetry(c, lease["case_id"])
    assert set(got) == {"site"}
    site = got["site"]
    assert site["urban_form"] == {"bcr": 0.31} and site["n_buildings"] == 40
    # Stamped by the broker, and the stamp wins over the body's own keys: WHO
    # held the lease and WHEN the broker heard it are not the node's to claim.
    assert site["worker"] == "node-a"
    assert isinstance(site["at"], int) and site["at"] > 1_700_000_000


def test_posting_a_kind_replaces_that_kind_only(env):
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, "site", {"n_buildings": 1, "dem": "gedtm30"}).status_code == 200
    assert _post(c, lease, "mesh", {"total_cells": 1_000_000}).status_code == 200
    assert _post(c, lease, "site", {"n_buildings": 2}).status_code == 200
    got = _telemetry(c, lease["case_id"])
    assert set(got) == {"site", "mesh"}
    # Replaced, not merged: a key the newer report dropped is gone.
    assert got["site"]["n_buildings"] == 2 and "dem" not in got["site"]
    assert got["mesh"]["total_cells"] == 1_000_000


def test_a_nan_from_a_diverging_solve_is_stored_as_unknown(env):
    """OpenFOAM prints `nan` for a diverging residual. Stored as-is, the case's
    GET could never be serialised again (responses refuse NaN), so it is null."""
    c, _ = env
    lease = _lease(c)[0]
    raw = json.dumps({"lease_id": lease["lease_id"], "case_id": lease["case_id"],
                      "kind": "solve", "data": {"residuals": {"Ux": float("nan"), "p": 0.1},
                                                "iteration": float("inf")}})
    r = c.post("/v1/telemetry", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 200
    got = c.get(f"/v1/cases/{lease['case_id']}")
    assert got.status_code == 200
    solve = got.json()["telemetry"]["solve"]
    assert solve["residuals"] == {"Ux": None, "p": 0.1} and solve["iteration"] is None


# -- 409: stop sending for this case ----------------------------------------------

def test_a_superseded_lease_is_409_and_writes_nothing(env):
    c, path = env
    zombie = _lease(c, "node-a")[0]
    assert _post(c, zombie, "site", {"n_buildings": 1}).status_code == 200
    # The lease lapses and another node takes the case over -- starting it
    # over, so the old attempt's report goes with it (see the retry tests).
    raw = db.connect(path)
    raw.execute("UPDATE cases SET lease_expires = 0 WHERE case_id = ?", (zombie["case_id"],))
    fresh = next(l for l in _lease(c, "node-b", count=3) if l["case_id"] == zombie["case_id"])
    r = _post(c, zombie, "site", {"n_buildings": 999})
    assert r.status_code == 409
    assert _telemetry(c, zombie["case_id"]) == {}
    # The rightful owner's report is the one that lands.
    assert _post(c, fresh, "site", {"n_buildings": 2}).status_code == 200
    got = _telemetry(c, zombie["case_id"])
    assert got["site"]["n_buildings"] == 2 and got["site"]["worker"] == "node-b"


def test_a_lease_of_another_case_is_409(env):
    """The cross-check: a node holding two cases must not write one's mesh onto
    the other, even with a lease that is perfectly current."""
    c, _ = env
    a, b = _lease(c, "node-a", count=2)
    assert _post(c, a, "mesh", {"total_cells": 5}, case_id=b["case_id"]).status_code == 409
    assert _telemetry(c, b["case_id"]) == {} and _telemetry(c, a["case_id"]) == {}


def test_nothing_this_route_refuses_is_404(env):
    """404 means "old broker" to the node, which then stops sending telemetry
    for good. An unknown lease or case is 409, like a heartbeat's."""
    c, _ = env
    lease = _lease(c)[0]
    unknown_lease = {"lease_id": "no-such-lease", "case_id": lease["case_id"]}
    unknown_case = {"lease_id": lease["lease_id"], "case_id": "v2-doesnotexist"}
    for bad in (unknown_lease, unknown_case):
        assert _post(c, bad).status_code == 409


def test_after_complete_the_lease_is_gone(env):
    c, _ = env
    lease = _lease(c)[0]
    assert c.post("/v1/complete", json={"lease_id": lease["lease_id"], "case_id": lease["case_id"],
                                        "result_uri": "file:///r"}).status_code == 200
    assert _post(c, lease).status_code == 409


# -- 413 / 422: the body is wrong --------------------------------------------------

def test_data_over_32_kib_is_413_and_at_the_limit_is_accepted(env):
    c, _ = env
    lease = _lease(c)[0]
    # {"x":"..."} compact is 8 bytes of framing around the string.
    at_limit = {"x": "a" * (db.TELEMETRY_MAX_BYTES - 8)}
    assert db.telemetry_size(at_limit) == db.TELEMETRY_MAX_BYTES
    assert _post(c, lease, "big", at_limit).status_code == 200
    over = {"x": "a" * (db.TELEMETRY_MAX_BYTES - 7)}
    assert _post(c, lease, "big", over).status_code == 413
    assert len(_telemetry(c, lease["case_id"])["big"]["x"]) == db.TELEMETRY_MAX_BYTES - 8


def test_a_seventeenth_new_kind_is_413_but_replacing_one_is_not(env):
    c, _ = env
    lease = _lease(c)[0]
    for i in range(db.TELEMETRY_MAX_KINDS):
        assert _post(c, lease, f"k{i}", {"i": i}).status_code == 200
    assert _post(c, lease, "one_too_many", {"i": 99}).status_code == 413
    assert _post(c, lease, "k3", {"i": 333}).status_code == 200
    got = _telemetry(c, lease["case_id"])
    assert len(got) == db.TELEMETRY_MAX_KINDS and "one_too_many" not in got
    assert got["k3"]["i"] == 333


@pytest.mark.parametrize("kind", ["Site", "1site", "_site", "si-te", "si te", "", "s" * 33,
                                  "site\n", "sité"])
def test_a_kind_outside_the_grammar_is_422(env, kind):
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, kind).status_code == 422
    assert _telemetry(c, lease["case_id"]) == {}


def test_the_longest_kind_is_accepted(env):
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, "s" + "_9" * 15 + "z").status_code == 200   # 32 chars


@pytest.mark.parametrize("data", [[1, 2], "text", 3, None])
def test_data_that_is_not_an_object_is_422(env, data):
    c, _ = env
    lease = _lease(c)[0]
    body = {"lease_id": lease["lease_id"], "case_id": lease["case_id"], "kind": "site",
            "data": data}
    assert c.post("/v1/telemetry", json=body).status_code == 422


def _nested(levels):
    """`data` nesting `levels` containers deep, `data` itself being the first."""
    inner = 1
    for _ in range(levels - 1):
        inner = [inner]
    return {"a": inner}


@pytest.mark.parametrize("levels", [17, 300, 1000, 5000])
def test_data_nested_too_deep_is_422_and_the_case_stays_readable(env, levels):
    """300 levels is ~700 bytes, far inside the size limit, and was stored; the
    case's GET then answered 500 for good (pydantic-core refuses to render past
    ~255 levels). 1000+ levels answered 500 on the POST itself."""
    c, _ = env
    lease = _lease(c)[0]
    r = _post(c, lease, "site", _nested(levels))
    assert r.status_code == 422 and "16 levels" in r.json()["detail"]
    got = c.get(f"/v1/cases/{lease['case_id']}")
    assert got.status_code == 200 and got.json()["telemetry"] == {}


def test_data_at_the_depth_limit_is_accepted(env):
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, "deep", _nested(db.TELEMETRY_MAX_DEPTH)).status_code == 200
    assert c.get(f"/v1/cases/{lease['case_id']}").status_code == 200


def test_a_row_stored_too_deep_before_the_limit_still_serves_its_record(env):
    c, path = env
    lease = _lease(c)[0]
    raw = db.connect(path)
    raw.execute("UPDATE cases SET telemetry=? WHERE case_id=?",
                (json.dumps({"site": _nested(300)}), lease["case_id"]))
    got = c.get(f"/v1/cases/{lease['case_id']}")
    assert got.status_code == 200 and got.json()["telemetry"] == {}


def test_the_size_limit_is_measured_as_stored(env, tmp_path):
    """Stored JSON escapes non-ASCII, so an emoji is 12 bytes in the column,
    not the 4 of UTF-8. Measured as UTF-8, 16 kinds each "within the limit"
    stored 1.5 MB against the 16 x 32 KiB the kind cap promises."""
    c, path = env
    lease = _lease(c)[0]
    emoji = "\U0001F600"
    # 4,000 emoji: 16,000 bytes of UTF-8, 48,000 as stored.
    over = {"x": emoji * 4000}
    assert len(json.dumps(over, ensure_ascii=False).encode("utf-8")) < db.TELEMETRY_MAX_BYTES
    assert _post(c, lease, "big", over).status_code == 413
    fits = {"x": emoji * ((db.TELEMETRY_MAX_BYTES - 8) // 12)}
    for i in range(db.TELEMETRY_MAX_KINDS):
        assert _post(c, lease, f"k{i}", fits).status_code == 200
    raw = sqlite3.connect(path)
    try:
        stored = raw.execute("SELECT telemetry FROM cases WHERE case_id=?",
                             (lease["case_id"],)).fetchone()[0]
    finally:
        raw.close()
    stamps = 128        # {"at":...,"worker":"node-a"} and the kind's own key
    assert len(stored.encode("utf-8")) <= db.TELEMETRY_MAX_KINDS * (db.TELEMETRY_MAX_BYTES + stamps)
    assert _telemetry(c, lease["case_id"])["k0"]["x"] == fits["x"]


def test_a_lone_surrogate_is_stored_as_a_replacement_character(env):
    """A .NET node that cuts a string mid-pair sends "\\ud83d". It answered 500
    on the POST (UTF-8 encoding in the size check); stored, it would have made
    the case's GET 500 for good. U+FFFD keeps the rest of the report."""
    c, _ = env
    lease = _lease(c)[0]
    raw = ('{"lease_id": "%s", "case_id": "%s", "kind": "site", "data": '
           '{"name": "ab\\ud83d", "nested": {"k\\udc00": ["x\\udc00y"]}, "ok": "\\ud83d\\ude00"}}'
           % (lease["lease_id"], lease["case_id"]))
    r = c.post("/v1/telemetry", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 200
    got = c.get(f"/v1/cases/{lease['case_id']}")
    assert got.status_code == 200
    site = got.json()["telemetry"]["site"]
    assert site["name"] == "ab�"
    assert site["nested"] == {"k�": ["x�y"]}
    assert site["ok"] == "\U0001F600"          # a whole pair is a character, untouched


# -- a machine credential reports only on its own leases -----------------------------

def _machine(path, name):
    from casebroker import auth
    tok = auth.new_token()
    db.create_worker_token(db.connect(path), name, auth.hash_token(tok))
    return {"Authorization": f"Bearer {tok}"}


def test_a_machine_credential_cannot_report_on_another_machine_s_lease(env):
    """lease_id is no secret (the case list shows it to any reader), and the
    report is stamped with the LEASE's worker -- so without this, lab-a could
    put invented mesh numbers into node-b's case under node-b's name, and into
    the dataset's statistics."""
    c, path = env
    lab_a, node_b = _machine(path, "lab-a"), _machine(path, "node-b")
    lease = c.post("/v1/lease", json={"worker_id": "node-b"}, headers=node_b).json()[0]
    forged = _post(c, lease, "mesh", {"max_skewness": 999}, **lab_a)
    assert forged.status_code == 409
    assert _telemetry(c, lease["case_id"]) == {}
    assert _post(c, lease, "mesh", {"max_skewness": 2.5}, **node_b).status_code == 200
    got = _telemetry(c, lease["case_id"])["mesh"]
    assert got["max_skewness"] == 2.5 and got["worker"] == "node-b"


def test_a_cluster_credential_reports_on_its_tasks_leases(env):
    """The same rule /v1/lease uses: `phoenix` covers `phoenix-<job>-<task>`."""
    c, path = env
    phoenix = _machine(path, "phoenix")
    lease = c.post("/v1/lease", json={"worker_id": "phoenix-812-3"}, headers=phoenix).json()[0]
    assert _post(c, lease, "site", **phoenix).status_code == 200
    # The shared env token has no machine identity to contradict.
    assert _post(c, lease, "mesh", {"total_cells": 1}).status_code == 200


# -- it describes what happened, so it outlives the lease ---------------------------

@pytest.mark.parametrize("ending", ["complete", "fail", "release"])
def test_telemetry_survives_the_end_of_the_lease(env, ending):
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, "mesh", {"total_cells": 7}).status_code == 200
    body = {"lease_id": lease["lease_id"]}
    if ending == "complete":
        body.update(case_id=lease["case_id"], result_uri="file:///r", metrics={"mesh_cells": 7})
    elif ending == "fail":
        body.update(error="solver diverged", retryable=True)
    assert c.post(f"/v1/{ending}", json=body).status_code == 200
    assert _telemetry(c, lease["case_id"])["mesh"]["total_cells"] == 7


def test_a_retry_that_starts_over_forgets_the_attempt_before(env):
    """Attempt 1 posts a mesh and fails; attempt 2 -- on a node that sends no
    telemetry, or whose report was dropped -- finishes. Kept, attempt 1's mesh
    would have described the done case in the dataset instead of the mesh
    that produced its result."""
    c, _ = env
    first = _lease(c, "new-node")[0]
    assert _post(c, first, "mesh", {"total_cells": 2_100_000}).status_code == 200
    assert c.post("/v1/fail", json={"lease_id": first["lease_id"], "error": "diverged",
                                    "retryable": True}).status_code == 200
    # Survives the fail itself: the pending case still says what happened.
    assert _telemetry(c, first["case_id"])["mesh"]["total_cells"] == 2_100_000
    second = next(x for x in _lease(c, "old-node", count=3) if x["case_id"] == first["case_id"])
    assert _telemetry(c, first["case_id"]) == {}
    assert c.post("/v1/complete", json={"lease_id": second["lease_id"], "case_id": second["case_id"],
                                        "result_uri": "file:///r",
                                        "metrics": {"mesh_cells": 1_400_000}}).status_code == 200
    ranked = c.get(f"/v1/cases/{first['case_id']}").json()["percentiles"]
    assert ranked["total_cells"]["value"] == 1_400_000


def test_a_resume_keeps_what_the_node_already_reported(env):
    """A resume continues the same work from the same disk: the site and mesh
    reports still describe it."""
    c, _ = env
    lease = _lease(c, "node-a")[0]
    assert _post(c, lease, "mesh", {"total_cells": 7}).status_code == 200
    # Still leased to it (a restart) ...
    again = c.post("/v1/lease", json={"worker_id": "node-a",
                                      "resume_case_ids": [lease["case_id"]]}).json()[0]
    assert again["case_id"] == lease["case_id"]
    assert _telemetry(c, lease["case_id"])["mesh"]["total_cells"] == 7
    # ... and released first (a SIGTERM), then resumed.
    assert c.post("/v1/release", json={"lease_id": again["lease_id"]}).status_code == 200
    back = c.post("/v1/lease", json={"worker_id": "node-a",
                                     "resume_case_ids": [lease["case_id"]]}).json()[0]
    assert back["case_id"] == lease["case_id"] and back["attempt"] == 1
    assert _telemetry(c, lease["case_id"])["mesh"]["total_cells"] == 7


def test_a_purge_deletes_it_with_the_row(env):
    c, path = env
    lease = _lease(c)[0]
    assert _post(c, lease, "site").status_code == 200
    out = c.delete("/v1/cases", params={"dry_run": "false", "expect": 3}).json()
    assert out["deleted"] == 3
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT COUNT(*) FROM cases WHERE telemetry IS NOT NULL").fetchone()[0] == 0
    finally:
        raw.close()


# -- the list stays lean --------------------------------------------------------------

def test_the_case_list_does_not_carry_telemetry(env):
    c, _ = env
    lease = _lease(c)[0]
    assert _post(c, lease, "solve", {"finished": {f"case_{i}": {"status": "converged"}
                                                  for i in range(8)}}).status_code == 200
    for q in ("", "?include_spec=false", "?include_spec=true"):
        page = c.get("/v1/cases" + q).json()["cases"]
        assert page and all("telemetry" not in row for row in page), q
    assert "solve" in _telemetry(c, lease["case_id"])


def test_the_page_columns_are_every_column_but_spec_and_telemetry():
    """The page's column list is written out by hand (`cases.*` cannot subtract
    telemetry), so a column added to the schema must be added there too --
    deliberately, which this makes a failing test rather than a quiet gap."""
    listed = {c.strip().removeprefix("cases.") for c in db._CASE_COLS_WITHOUT_SPEC.split(",")}
    assert listed == set(db.parse_schema_columns(db.SCHEMA)["cases"]) - {"spec", "telemetry"}


# -- the column itself ------------------------------------------------------------------

def test_the_column_is_in_both_schemas():
    for schema in (db.SCHEMA, db.PG_SCHEMA):
        assert db.parse_schema_columns(schema)["cases"]["telemetry"].upper() == "TELEMETRY TEXT"


def test_a_database_from_before_telemetry_gains_the_column(tmp_path):
    path = str(tmp_path / "old.sqlite")
    columns = db.parse_schema_columns(db.SCHEMA)["cases"]
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE cases (%s)" % ", ".join(
        ddl for name, ddl in columns.items() if name != "telemetry"))
    raw.execute("INSERT INTO cases(case_id, spec, recipe, city_cluster, split, state,"
                " created_at, updated_at) VALUES ('c1', '{}', 'r', 'x', 'train', 'done', 1, 1)")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert conn.execute("SELECT telemetry FROM cases").fetchone()["telemetry"] is None
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_post_telemetry_answers_by_name(tmp_path):
    """The db-level outcomes the route maps to status codes."""
    conn = db.connect(str(tmp_path / "d.sqlite"))
    db.add_cases(conn, [{"case_id": "c1", "spec": {}, "recipe": "r", "city_cluster": "x",
                         "split": "train"}])
    lease = db.lease(conn, "w")[0]
    assert db.post_telemetry(conn, lease.lease_id, "c1", "site", {"a": 1}, now=100) == "ok"
    assert db.post_telemetry(conn, lease.lease_id, "c2", "site", {"a": 1}) == "gone"
    assert db.post_telemetry(conn, "nope", "c1", "site", {"a": 1}) == "gone"
    assert db.post_telemetry(conn, lease.lease_id, "c1", "Bad", {"a": 1}) == "invalid"
    assert db.post_telemetry(conn, lease.lease_id, "c1", "site", [1]) == "invalid"
    assert db.post_telemetry(conn, lease.lease_id, "c1", "site",
                             {"x": "a" * db.TELEMETRY_MAX_BYTES}) == "too_large"
    stored = json.loads(conn.execute("SELECT telemetry FROM cases").fetchone()["telemetry"])
    assert stored == {"site": {"a": 1, "at": 100, "worker": "w"}}
    # Not a state change: the case browser's "most recently touched" must not
    # reorder itself around a solve that reports every five minutes.
    before = conn.execute("SELECT updated_at FROM cases").fetchone()["updated_at"]
    assert db.post_telemetry(conn, lease.lease_id, "c1", "mesh", {"b": 2}, now=before + 500) == "ok"
    assert conn.execute("SELECT updated_at FROM cases").fetchone()["updated_at"] == before
