"""Deep links: the address bar says where you are, and a link opens the same place.

The routing code is pure on purpose -- it parses and writes the URL's fragment and
does nothing else -- so the check runs the SHIPPED JavaScript, sliced out of
dashboard.html between its markers, on links of every kind, hostile ones
included. The rest is wiring, which can only go wrong by being left out, so those
checks read the page the same way the other dashboard tests do.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "casebroker" / "static" / "dashboard.html"
SRC = DASH.read_text(encoding="utf-8")


def _body(start: str, end: str = "\n  }\n") -> str:
    i = SRC.index(start)
    return SRC[i:SRC.index(end, i)]


def test_links_read_and_write_as_the_page_reads_and_writes_them():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine; the JS half cannot be executed here")

    result = subprocess.run([node, str(ROOT / "tests" / "deeplink_check.js")],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a share secret never survives into a route" in result.stdout


def test_every_question_the_case_table_asks_goes_through_the_address():
    """Filters, sort, page, lookup, reset, the 60 s refresh: all of them start
    their request in startCases(), so one call at its top is what keeps the
    address true to all of them. Left out, a filter would change the table and
    leave the bar saying something else."""
    body = _body("function startCases()")
    assert body.index("syncRoute()") < body.index("++casesGen"), \
        "startCases must write the address before it asks"


def test_a_case_opening_or_closing_writes_the_address_too():
    row_click = _body("tr.addEventListener(\"click\"", "\n    });\n")
    assert row_click.index("renderCasesTable(cases)") < row_click.index("syncRoute()")


def test_the_drawers_and_their_tabs_are_part_of_the_address():
    for fn in ("function openSettings(", "function closeSettings(", "function openStorage(",
               "function closeStorage(", "function openDataset(", "function closeDataset(",
               "function showTab("):
        assert "syncRoute()" in _body(fn), f"{fn} must write the address"


def test_a_share_secret_leaves_the_address_before_anything_is_awaited():
    """The link is a credential. Whatever the request does or how slow it is,
    the bar and this history entry must already be rid of it."""
    body = _body("async function redeemShareLink()")
    assert body.index("history.replaceState") < body.index("await "), \
        "the secret must be taken out of the address before the first await"
    # And what replaces it is built from the parsed ROUTE, which has no secret in it.
    assert "routeHash(parseRoute(location.hash" in body


def test_no_address_is_ever_written_before_the_page_has_read_its_own():
    """Until the first fragment is read and its drawers are open, a refresh that
    happens to start first would write a bare address over the link the page was
    opened with."""
    assert "if (!routeReady || routeApplying) return;" in _body("function syncRoute()")
    boot = SRC[SRC.index("const bootRoute = parseRoute("):]
    boot = boot[:boot.index("badgeSkeleton(true);")]
    assert boot.index("applyRoute(bootRoute, true)") < boot.index("routeReady = true")
    assert boot.index("applyDrawers(bootRoute)") < boot.index("routeReady = true")


def test_a_share_link_is_redeemed_before_the_page_asks_who_is_looking():
    assert "const authReady = redeemShareLink().then(() => loadAuthState());" in SRC


def test_a_visitor_through_a_link_is_not_sent_to_a_sign_in_that_does_not_exist():
    """refresh() sends someone with no credential to 'sign in'. A share visitor
    has a cookie, not a login, so the gate has to count it."""
    assert "authState.user || authState.share" in _body("async function refresh()")


def test_the_pairing_link_no_longer_wipes_the_fragment():
    """?pair= opens Settings, and the replaceState that tidies its query used to
    drop everything after the path, so the drawer it had just opened lost its
    address."""
    assert 'window.history.replaceState({}, "", window.location.pathname);' not in SRC
    assert "window.location.pathname + window.location.hash" in SRC


def test_every_settings_tab_has_its_pane():
    tabs = re.findall(r'class="drawer-tab"[^>]*data-tab="([a-z]+)"', SRC)
    assert "sharing" in tabs
    for name in tabs:
        assert f'id="tab-{name}"' in SRC, f"the {name} tab has no pane"


def test_the_sharing_controls_are_for_admins_only():
    """The server refuses everyone else; the page just must not offer it."""
    load = _body("async function loadAuthState()", "\n  }\n\n  async function submitAuth")
    assert '$("shareBtn").style.display = isAdmin ? "" : "none"' in load
    assert '$("sharing").style.display = isAdmin ? "" : "none"' in load
    assert "function canShare() { return !!(authState && authState.user && authState.role === \"admin\"); }" in SRC
