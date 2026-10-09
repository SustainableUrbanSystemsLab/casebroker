"""A per-machine credential acts only on leases held under its own name.

/v1/lease always refused a machine token claiming work as another machine, and
telemetry, parts and fields checked the lease's worker the same way. Heartbeat,
complete, fail and release did not: they trusted the lease_id, on the reasoning
that "the lease already records who holds it". It does, and nothing compared that
record with the caller -- while the case list handed every reader the lease_id.
So one machine's token could quarantine another machine's case (fail with
retryable=false), confirm a result that never ran, or pull a live solve. These
pin the fix: 409 for a lease the credential does not hold, the case untouched,
and no lease_id on any case read.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from casebroker.app import create_app  # noqa: E402

PW = "a-sufficiently-long-passphrase"


@pytest.fixture()
def broker(tmp_path):
    """An admin session, two machine credentials, and a case leased by `lab-a`."""
    admin = TestClient(create_app(db_path=str(tmp_path / "m.sqlite"),
                                  tokens=["env-write"], readonly_tokens=[]))
    r = admin.post("/v1/auth/setup", json={"username": "ada", "password": PW,
                                           "setup_token": "env-write"})
    assert r.status_code == 200, r.text
    tokens = {}
    for name in ("lab-a", "lab-b", "ice"):
        r = admin.post("/v1/workers/tokens", json={"name": name})
        assert r.status_code == 200, r.text
        tokens[name] = {"Authorization": f"Bearer {r.json()['token']}"}
    r = admin.post("/v1/cases", json=[
        {"lat": 35.0 + i, "lon": 139.0 + i, "recipe": "v2-wind", "city_cluster": "tokyo"}
        for i in range(3)])
    assert r.status_code == 200, r.text
    leased = admin.post("/v1/lease", json={"worker_id": "lab-a"}, headers=tokens["lab-a"]).json()
    assert len(leased) == 1
    return admin, tokens, leased[0]


def _state(admin, case_id):
    return admin.get(f"/v1/cases/{case_id}").json()["state"]


@pytest.mark.parametrize("call,body", [
    ("/v1/heartbeat", {"detail": "solve 3/32 dirs"}),
    ("/v1/complete", {"result_uri": "file:///forged.tar.gz"}),
    ("/v1/fail", {"error": "forged", "retryable": False}),
    ("/v1/release", {"reason": "forged"}),
])
def test_another_machine_cannot_act_on_the_lease(broker, call, body):
    admin, tokens, lease = broker
    r = admin.post(call, json={"lease_id": lease["lease_id"], "case_id": lease["case_id"], **body},
                   headers=tokens["lab-b"])
    assert r.status_code == 409, r.text
    # Nothing happened to the case: still leased, by lab-a, still heartbeatable.
    assert _state(admin, lease["case_id"]) == "leased"
    ok = admin.post("/v1/heartbeat", json={"lease_id": lease["lease_id"]}, headers=tokens["lab-a"])
    assert ok.status_code == 200, ok.text


def test_the_holder_completes_its_own_case(broker):
    admin, tokens, lease = broker
    r = admin.post("/v1/complete", json={"lease_id": lease["lease_id"], "case_id": lease["case_id"],
                                         "result_uri": "file:///a.tar.gz"}, headers=tokens["lab-a"])
    assert r.status_code == 200, r.text
    assert _state(admin, lease["case_id"]) == "done"
    # A lost response is retried; the retry is still confirmed for the holder...
    again = admin.post("/v1/complete", json={"lease_id": lease["lease_id"], "case_id": lease["case_id"],
                                             "result_uri": "file:///a.tar.gz"}, headers=tokens["lab-a"])
    assert again.status_code == 200


def test_a_cluster_credential_covers_the_ids_under_it(broker):
    admin, tokens, _ = broker
    leased = admin.post("/v1/lease", json={"worker_id": "ice-4411"}, headers=tokens["ice"]).json()
    assert len(leased) == 1
    r = admin.post("/v1/release", json={"lease_id": leased[0]["lease_id"], "reason": "wall"},
                   headers=tokens["ice"])
    assert r.status_code == 200, r.text


def test_shared_env_tokens_and_sessions_are_not_confined(broker):
    """Shared by design: there is no machine identity to contradict, and the
    operator's session is how a lease is cleaned up by hand."""
    admin, tokens, lease = broker
    env = {"Authorization": "Bearer env-write"}
    assert admin.post("/v1/heartbeat", json={"lease_id": lease["lease_id"]},
                      headers=env).status_code == 200
    assert admin.post("/v1/release", json={"lease_id": lease["lease_id"]}).status_code == 200


def test_no_case_read_carries_a_lease_id(broker):
    admin, tokens, lease = broker
    page = admin.get("/v1/cases").json()["cases"]
    assert page and all("lease_id" not in row for row in page)
    held = next(row for row in page if row["case_id"] == lease["case_id"])
    assert held["lease_worker"] == "lab-a", "who holds it stays visible"
    one = admin.get(f"/v1/cases/{lease['case_id']}").json()
    assert "lease_id" not in one
    # ...and a machine token, which can read, finds nothing to present either.
    assert lease["lease_id"] not in admin.get("/v1/cases", headers=tokens["lab-b"]).text
