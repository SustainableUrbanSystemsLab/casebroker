"""Anything a worker can write reaches an admin's browser through innerHTML.

The dashboard builds its DOM from template literals. Most interpolations go
through `esc()`, and the ones that did not were fields a WORKER supplies --
`cluster` via POST /v1/fleet, `result_sha256` via POST /v1/complete. A worker
credential is the weakest link in this system by construction: it lives
unattended on cluster nodes and Windows workstations. Script running in the
admin's dashboard acts with the admin's session, so an unescaped worker string
is a path from write scope to admin.

This is a static check rather than a browser test, because the property worth
pinning is "no interpolation of server data skips esc()" -- which a rendering
test would only catch for the exact payload it happened to try.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from casebroker.app import create_app

DASH = (pathlib.Path(__file__).resolve().parents[1]
        / "casebroker" / "static" / "dashboard.html")

# Fields that originate outside the browser: a worker, an operator, or the
# database. Anything here must be escaped before it becomes markup.
SERVER_FIELDS = (
    "case_id", "city_cluster", "lcz", "recipe", "last_error", "result_uri",
    "result_sha256", "worker_id", "host", "cluster", "detail", "username",
    "role", "split", "machine", "last_progress", "hostname",
)
# Helpers that escape, or that cannot produce markup (numbers, dates).
SAFE_CALLS = ("esc(", "formatBytes(", "relTime(", "badge(", "toLocaleString(",
              "toFixed(", "Number(", "Math.")


def _interpolations():
    for i, line in enumerate(DASH.read_text().splitlines(), 1):
        for m in re.finditer(r"\$\{([^{}]*)\}", line):
            yield i, m.group(1).strip()


def test_no_server_supplied_field_reaches_the_dom_unescaped():
    bad = []
    for lineno, expr in _interpolations():
        if any(c in expr for c in SAFE_CALLS):
            continue
        if any(re.search(rf"\b{f}\b", expr) for f in SERVER_FIELDS):
            bad.append(f"dashboard.html:{lineno}: ${{{expr[:80]}}}")
    assert not bad, "unescaped server data in markup:\n  " + "\n  ".join(bad)


def test_badge_escapes_both_positions():
    """`state` lands in a class attribute as well as in text."""
    src = DASH.read_text()
    m = re.search(r"function badge\(state\)\s*\{(.+?)\n  \}", src, re.S)
    assert m, "badge() not found"
    body = m.group(1)
    assert body.count("esc(state)") >= 2, (
        "badge() must escape `state` in the attribute AND the text")


def test_esc_covers_every_character_that_matters():
    src = DASH.read_text()
    m = re.search(r"function esc\(s\)\s*\{(.+?)\n  \}", src, re.S)
    assert m, "esc() not found"
    body = m.group(1)
    for ch in ("&", "<", ">", '"', "'"):
        assert ch in body, f"esc() does not handle {ch!r}"


# -- and the server side of it -----------------------------------------------

@pytest.fixture()
def broker(tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "x.sqlite"), tokens=["s"]))
    c.headers.update({"Authorization": "Bearer s"})
    return c


def test_the_broker_stores_worker_strings_verbatim(broker):
    """Deliberate, and the reason escaping is the dashboard's job.

    The broker does not sanitise on the way in: a cluster name is data, the
    database is not an HTML document, and stripping characters here would
    corrupt a legitimate name while still not making the dashboard safe. The
    invariant is that it is escaped on the way OUT, which the tests above pin.
    """
    payload = "<img src=x onerror=alert(1)>"
    broker.post("/v1/fleet", json={"cluster": payload, "queued": 1, "running": 0})
    got = broker.get("/v1/status").json()
    clusters = [f["cluster"] for f in got.get("fleet", [])]
    assert payload in clusters, "stored verbatim, to be escaped at render time"
