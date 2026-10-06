"""A reload draws its skeletons in the shape the last load drew.

Until its first answers land, the dashboard shows skeletons, and a skeleton of
the wrong size is a layout shift when the answer replaces it. Measured in
headless Chrome against a seeded broker with ~350 ms round trips (2026-10-06),
reloads scored a cumulative layout shift of 0.004 to 0.42 depending on the
width and where the page was scrolled to -- a phone reloaded at the fleet panel
watched it drop 125 px. With the last load's shape remembered, every one
scores 0.

There is no browser here, so these pin the mechanism in the page source, as
the other dashboard tests do.
"""
from __future__ import annotations

import re
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "casebroker" / "static" / "dashboard.html"


def _src() -> str:
    return DASH.read_text(encoding="utf-8")


def _body(name: str) -> str:
    src = _src()
    start = src.index(f"function {name}(")
    return src[start:src.index("\n  }\n", start)]


def _render_status() -> str:
    # Not _body: renderStatus declares sortWorkers and renderWorkersHead inside
    # itself at the outer indent, so its first "\n  }\n" is theirs.
    src = _src()
    start = src.index("function renderStatus(")
    return src[start:src.index("// -- Case Browser Logic", start)]


def _recorded() -> dict[str, str]:
    """Every field any recordShape({...}) call writes, with its expression."""
    fields = {}
    for call in re.findall(r"recordShape\(\{(.*?)\}\);", _src(), re.S):
        fields.update((k, v.strip()) for k, v in re.findall(r"^\s*(\w+): (.*?),?$", call, re.M))
    return fields


def test_every_remembered_shape_is_one_an_answer_records():
    """A key the skeletons read and no answer writes is a memory that never
    fills: the skeleton keeps its defaults for good, and nothing says so."""
    read = set(re.findall(r'shape(?:Of|Text)\("(\w+)"\)', _src()))
    assert read, "the skeletons no longer read the last load's shape"
    assert read <= set(_recorded()), f"read but never recorded: {sorted(read - set(_recorded()))}"


def test_the_skeletons_take_the_shape_of_the_last_load():
    status, workers, cases = (_body("statusSkeleton"), _body("workersSkeleton"),
                              _body("casesSkeleton"))
    for key in ("cards", "split"):
        assert f'shapeOf("{key}")' in status
    for key in ("pct", "eta"):
        assert f'shapeText("{key}")' in status
    for key in ("fleet", "workers"):
        assert f'shapeOf("{key}")' in workers
    assert 'shapeOf("cases")' in cases
    assert 'shapeText("badge")' in _body("badgeSkeleton")


def test_a_reserved_height_is_given_back_when_its_skeleton_ends():
    """A skeleton holds a region at the height the last answer drew. Left in
    place, that height would outlive the answer: a fleet that shrank would keep
    the old strip's empty space until the next reload."""
    reserved = re.findall(r"^\s*(\S+)\.style\.minHeight = (?!\"\")", _src(), re.M)
    # A new reservation needs its release added below, and here.
    assert sorted(reserved) == ['$("stateStats")', "fq"], reserved
    assert "const fq = $(\"fleetQueue\");" in _body("workersSkeleton")
    assert '$("stateStats").style.minHeight = "";' in _body("statusSkeletonDone")
    assert '$("fleetQueue").style.minHeight = "";' in _body("workersSkeletonDone")


def test_the_shape_keeps_no_campaign_figures():
    """Strings are kept where their length decides a width or a wrap. The
    progress and ETA lines are made of the campaign's counts, so their digits
    are not kept; the badge says only what /healthz tells anyone."""
    fields = _recorded()
    for key in ("pct", "eta"):
        assert fields[key].endswith('.replace(/\\d/g, "0")'), f"{key} is stored with its digits"
    assert fields["badge"] == "badge.textContent"


def test_the_badge_keeps_what_the_status_payload_does_not_carry():
    """/healthz says the auth mode and /v1/status does not, so drawing each
    payload on its own made the badge lose "auth required" on every refresh
    (212 px, then 114). It accumulates what it has been told."""
    body = _body("setBadge")
    assert "badgeFrom[k] = h[k]" in body and "const b = badgeFrom;" in body
    assert not re.search(r"\bh\.(version|commit|db|auth)\b", body), (
        "a part read from the payload alone is lost when the next one lacks it")
    assert "badgeFrom = {}" in _body("forgetCampaign"), (
        "another broker's badge must not inherit this one's parts")


def test_the_progress_bar_arrives_drawn_rather_than_grown():
    """Grown from zero, each segment's width transition moved the segments
    after it on every frame: twenty to forty layout shifts per load."""
    src = _src()
    assert ".progress-bar-wrap.no-anim .progress-segment { transition: none; }" in src
    body = _render_status()
    assert "const arriving = skelOn.status;" in body
    assert body.index("const arriving") < body.index("statusSkeletonDone(true)"), (
        "read after statusSkeletonDone, `arriving` is always false")
    assert 'bar.classList.toggle("no-anim", arriving)' in body


def test_the_connection_pill_starts_as_wide_as_it_stays():
    """The pill reserves the width of its widest loading word. Its first text
    is that word, so the first frame is already the width every later one has."""
    src = _src()
    ghost = re.search(r'#connText::after \{ content: "([^"]+)"', src).group(1)
    first = re.search(r'<span id="connText">([^<]+)</span>', src).group(1)
    assert first == ghost
