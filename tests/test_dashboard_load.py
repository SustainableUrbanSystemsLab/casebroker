"""What the dashboard costs to LOAD, which is latency and bytes, not query time.

Measured against the deployed broker (2026-09-20, warm connection): every round
trip is ~350 ms while the queries behind them run in about a millisecond, and
the page itself is 203,794 bytes. So the three things worth guarding are the
number of round trips the load waits on in series, the size of the case page,
and whether a repeat load has to download the HTML again.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "casebroker" / "static" / "dashboard.html"


def _refresh_body() -> str:
    src = DASH.read_text(encoding="utf-8")
    start = src.index("async function refresh()")
    return src[start:src.index("\n  }\n", start)]


def test_the_load_requests_go_out_together_rather_than_in_a_chain():
    """/healthz, /v1/status and the case page do not depend on each other, and
    each one costs a round trip the user waits through. They used to be awaited
    one after another -- about a second of nothing happening before the first
    row appeared."""
    body = _refresh_body()
    first_await = body.index("await ")
    started_before = [name for name in ("checkHealth()", 'api("/v1/status")', "startCases()")
                      if name in body[:first_await]]
    assert len(started_before) == 3, (
        "every load request must be STARTED before the first await, or the one "
        f"after it pays a round trip to find out; started early: {started_before}")


def test_a_rejection_on_the_gated_path_is_handled_where_it_is_created():
    """The auth gate returns before awaiting the other two. A promise nobody ever
    awaits is an unhandled rejection, which in a browser is a console error on
    every anonymous page load."""
    body = _refresh_body()
    gate = body.index("auth required")
    for name in ("statusP.catch(", "casesReq.promise.catch("):
        assert name in body[:gate], f"{name} must be attached before the gate can return"


def test_the_case_page_the_dashboard_asks_for_carries_no_specs():
    src = DASH.read_text(encoding="utf-8")
    start = src.index("function startCases()")
    assert 'q.set("include_spec", "false")' in src[start:src.index("\n  }\n", start)], (
        "the case table draws no spec; shipping 50 of them was most of the page")


def test_the_expanded_row_fetches_the_spec_it_actually_needs():
    """Dropping specs from the list is only correct because the one row that
    reads one asks for that case in full."""
    src = DASH.read_text(encoding="utf-8")
    assert "function ensureCaseDetail(" in src
    body = src[src.index("function ensureCaseDetail("):]
    body = body[:body.index("\n  }\n")]
    assert 'api("/v1/cases/"' in body, "the open row must fetch its own full record"
    assert "caseDetails.set(id, null)" in body, (
        "an in-flight fetch must be recorded, or every re-render starts another")


# -- the server half ---------------------------------------------------------

def _seeded_client(tmp_path, n=30):
    import os
    os.environ["CASEBROKER_AUTH"] = "open"
    from casebroker.app import create_app
    from casebroker import db as D

    db_path = str(tmp_path / "load.db")
    app = create_app(db_path=db_path)
    conn = D.connect(db_path)
    D.add_cases(conn, [{"case_id": f"c{i:04d}", "spec": {"site": {"lat": 52.0, "lon": 13.0},
                                                         "notes": "x" * 400},
                        "recipe": "v2", "split": "train", "lcz": "5",
                        "city_cluster": "c1", "priority": 0} for i in range(n)])
    return TestClient(app)


def test_include_spec_false_drops_the_spec_and_nothing_else(tmp_path):
    """The column list for the no-spec page is written out by hand, because
    `cases.*` cannot subtract one column. That list can drift from the schema --
    silently, as a field the dashboard stops receiving -- so the two pages are
    compared against each other rather than against a remembered set of names."""
    client = _seeded_client(tmp_path)
    full = client.get("/v1/cases?limit=30").json()["cases"]
    lean = client.get("/v1/cases?limit=30&include_spec=false").json()["cases"]

    assert full and lean
    assert set(full[0]) - set(lean[0]) == {"spec"}, "exactly one column may differ"
    assert set(lean[0]) - set(full[0]) == set()
    # And it is worth doing: the spec is most of the bytes.
    assert len(json.dumps(lean)) * 2 < len(json.dumps(full))


def test_the_default_still_carries_the_spec(tmp_path):
    """`/v1/cases` is public API. A script reading specs out of a page must not
    silently stop getting them because the dashboard wanted a smaller payload."""
    client = _seeded_client(tmp_path)
    assert "spec" in client.get("/v1/cases?limit=1").json()["cases"][0]


def test_the_dashboard_answers_a_conditional_request_with_304(tmp_path):
    """FileResponse computes an ETag and then ignores the If-None-Match that comes
    back, so every load re-downloaded the whole 200 KB page. Confirmed against the
    deployed broker before fixing it: a request carrying the server's own ETag
    returned 200 with 203,794 bytes."""
    client = _seeded_client(tmp_path)
    first = client.get("/")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag, "the page must carry a validator"
    assert first.headers.get("cache-control") == "no-cache", (
        "the dashboard talks to this broker's API and must never be served stale "
        "from a cache after a deploy -- it revalidates, and the 304 is what makes "
        "revalidating cheap")

    again = client.get("/", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content

    # A validator that never changes would be worse than none.
    DASH_COPY = first.content
    assert len(DASH_COPY) > 1000
    stale = client.get("/", headers={"If-None-Match": '"deadbeef-0"'})
    assert stale.status_code == 200
