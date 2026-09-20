"""The case detail card: what it claims about a case that is running right now.

Every test here comes from one pasted panel (2026-09-20) in which five of the
eight rows were wrong or empty at once: Host, Cluster and Wall Time blank for a
case that was actively solving, "Lease Expiry: just now (from now)" for a lease
with up to an hour of headroom, and an animated progress bar drawn from a line
nine hours old.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "casebroker" / "static" / "dashboard.html"


def test_the_panel_formatters_hold():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")
    result = subprocess.run([node, str(ROOT / "tests" / "telemetry_panel_check.js")],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hold" in result.stdout


def test_the_open_row_is_refreshed_not_frozen():
    """The expanded panel merges the list row with the full record fetched when
    the row opened. That fetch happens ONCE and is cached for the life of the
    page, so if the cached copy wins the merge, every field on the card freezes
    at the moment of expansion -- progress, lease expiry, state and attempts all
    stop moving while the table behind them keeps refreshing every 60 s."""
    src = DASH.read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if "const expandedCase" in l)
    # The live row `c` must be the LAST argument to Object.assign.
    assert line.index("caseDetails.get(") < line.index(", c)"), (
        "the cached record must come first so the live row overwrites it: " + line.strip())


def test_the_animated_bar_is_only_for_a_case_someone_is_working_on():
    """progressCell draws a moving band that means "a worker is on this now".
    Only a leased case is. The cases table gated this from the start; the detail
    panel did not, so a failed case animated under its own failure message."""
    src = DASH.read_text(encoding="utf-8")
    # Keyed on every call site, not one literal: a second ungated call site was
    # exactly how this got past the test that already existed.
    for lineno, line in enumerate(src.splitlines(), 1):
        if "progressCell(" not in line or "function progressCell" in line:
            continue
        if "w.current_progress" in line:
            continue          # the workers table: a worker row IS a live lease
        assert 'state === "leased"' in line or "c.state === \"leased\"" in line, (
            f"dashboard.html:{lineno} draws the animated bar without checking the state: {line.strip()}")


def test_a_case_nobody_is_working_on_still_shows_its_last_line():
    """Gating the bar must not throw the text away -- the last thing a failed
    case said is the most useful thing on the card."""
    src = DASH.read_text(encoding="utf-8")
    assert "function progressText(" in src
    body = src[src.index("function progressText("):]
    body = body[:body.index("\n  }")]
    assert "esc(line)" in body, "the line is worker-supplied and reaches the DOM"


# -- the server half ---------------------------------------------------------

def _client(tmp_path):
    import os
    os.environ["CASEBROKER_AUTH"] = "open"
    from casebroker.app import create_app
    from casebroker import db as D

    db_path = str(tmp_path / "telemetry.db")
    app = create_app(db_path=db_path)
    conn = D.connect(db_path)
    D.add_cases(conn, [{"case_id": "c0001", "spec": {"site": {"lat": 52.0, "lon": 13.0}},
                        "recipe": "v2", "split": "train", "lcz": "5",
                        "city_cluster": "c1", "priority": 0}])
    return TestClient(app), conn


def test_a_leased_case_says_where_it_is_running(tmp_path):
    """Host and Cluster came from `metrics`, which only complete() writes -- so
    they were empty for exactly the cases somebody opens the card to locate."""
    client, conn = _client(tmp_path)
    claimed = client.post("/v1/lease", json={"worker_id": "foam-1", "host": "cod-mbp",
                                             "cluster": "ICE"}).json()
    assert [c["case_id"] for c in claimed] == ["c0001"], claimed

    case = client.get("/v1/cases/c0001").json()
    assert case["state"] == "leased"
    assert case["worker_host"] == "cod-mbp"
    assert case["worker_cluster"] == "ICE"
    # And the elapsed time the card computes from is present.
    assert case["leased_at"], "leased_at is what a live wall time is measured from"


def test_an_unleased_case_claims_no_host(tmp_path):
    """The control. A subquery keyed on lease_worker must answer NULL rather
    than the last worker to touch anything."""
    client, _ = _client(tmp_path)
    case = client.get("/v1/cases/c0001").json()
    assert case["state"] == "pending"
    assert case["worker_host"] is None
    assert case["worker_cluster"] is None


def test_the_lean_case_page_carries_the_worker_too(tmp_path):
    """The table shows a worker column; dropping spec must not drop this."""
    client, _ = _client(tmp_path)
    client.post("/v1/lease", json={"worker_id": "foam-1", "host": "cod-mbp", "cluster": "ICE"})
    lean = client.get("/v1/cases?include_spec=false&limit=1").json()["cases"][0]
    assert lean["worker_host"] == "cod-mbp"


def test_a_failed_case_still_names_the_worker_that_had_it(tmp_path):
    """fail() nulls lease_worker in the same statement that writes last_error, so
    the card went blank about the worker on exactly the rows showing a failure --
    the rows where somebody wants to know which machine to go and look at. The
    events trail kept it."""
    client, _ = _client(tmp_path)
    leased = client.post("/v1/lease", json={"worker_id": "foam-1", "host": "cod-mbp",
                                            "cluster": "ICE"}).json()
    lease_id = leased[0]["lease_id"]
    client.post("/v1/fail", json={"lease_id": lease_id, "error": "a direction ended without converging",
                                  "retryable": True})

    case = client.get("/v1/cases/c0001").json()
    assert case["lease_worker"] is None, "the lease really is gone"
    assert case["last_error"]
    assert case["last_worker"] == "foam-1", "who last held it must survive the failure"


def test_a_reopened_case_does_not_keep_its_failure_message(tmp_path):
    """reopen_cases resets attempts because the history says nothing about the
    case. The message is that same history in prose: leaving it behind puts a red
    failure banner on a case that is now pending and blameless."""
    from casebroker import db as D
    client, conn = _client(tmp_path)
    leased = client.post("/v1/lease", json={"worker_id": "foam-1"}).json()
    client.post("/v1/fail", json={"lease_id": leased[0]["lease_id"],
                                  "error": "Engine 'docker' is not available", "retryable": False})
    assert client.get("/v1/cases/c0001").json()["state"] == "quarantined"

    D.reopen_cases(conn, case_ids=["c0001"], dry_run=False)

    case = client.get("/v1/cases/c0001").json()
    assert case["state"] == "pending"
    assert case["attempts"] == 0
    assert not case["last_error"], "a reopened case carries no failure banner"


def test_the_workers_table_knows_when_its_progress_line_was_written(tmp_path):
    """The case row folds both the progress detail and its timestamp; the workers
    query folded only the detail, so that table drew a moving bar with nothing
    beside it to say the line was hours old."""
    client, _ = _client(tmp_path)
    leased = client.post("/v1/lease", json={"worker_id": "foam-1"}).json()
    client.post("/v1/heartbeat", json={"lease_id": leased[0]["lease_id"],
                                       "detail": "solve 3/8 dirs \u00b7 iter 412/2000"})

    worker = next(w for w in client.get("/v1/status").json()["workers"]
                  if w["worker_id"] == "foam-1")
    assert worker["current_progress"], "the line itself"
    assert worker["current_progress_at"], "and when it was said"


def test_the_attempts_row_says_what_it_counts():
    """It is a charged-FAILURE counter: a release refunds one and a worker
    resuming its own case spends none, so a case can have run four times and read
    "1 / 3". Labelling that "Attempts" reads as the opposite."""
    src = DASH.read_text(encoding="utf-8")
    assert "<dt>Attempts</dt>" not in src, "the bare label reads as a restart counter"
    assert "Attempts used" in src


def test_a_failure_message_on_a_running_case_says_it_is_old():
    """last_error outlives the attempt that wrote it, so a case that failed once
    and is now running again showed a red "Last Failure Error" about work that is
    no longer happening."""
    src = DASH.read_text(encoding="utf-8")
    banner = src[src.index("Last Failure Error") - 800:src.index("Last Failure Error") + 800]
    assert "Previous Attempt Failed" in banner
    assert 'c.state === "leased"' in banner

