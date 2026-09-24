"""Sorting the case browser, on the server where the data is.

The table is rendered on the client but the client holds ONE PAGE: 50 rows of
40,000. Reordering those and labelling it "sorted by attempts" is a more
convincing wrong answer than no sorting at all, so the order is chosen in SQL.

A column name reaches an ORDER BY, where there is no placeholder to bind it
with, so the set of sortable columns is an allowlist and anything else falls
back rather than raising -- a stale bookmark must not break the browser.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import db, ids  # noqa: E402


def make_db(tmp_path):
    conn = db.connect(str(tmp_path / "b.sqlite"))
    rows = []
    for i, (city, lcz) in enumerate([("zurich", "LCZ2"), ("atlanta", "LCZ6"), ("munich", "LCZ1")]):
        lat = 34.0 + i * 0.01
        rows.append({
            "case_id": ids.case_id(lat, -84.0, "r1"),
            "spec": {"lat": lat, "lon": -84.0}, "recipe": "r%d" % i,
            "city_cluster": city, "lcz": lcz, "split": "train",
        })
    db.add_cases(conn, rows)
    return conn


def ids_in(result):
    return [c["case_id"] for c in result["cases"]]


def test_every_column_the_browser_shows_can_be_sorted_by():
    """The UI offers a sort on each column; each one has to name a real
    expression here, or the click falls back to the default and looks broken."""
    for column in ("case_id", "state", "split", "lcz", "city_cluster", "recipe",
                   "attempts", "updated_at", "last_progress", "last_error"):
        assert column in db.CASE_SORTS, f"{column} is shown but cannot be sorted"


def test_sorting_orders_the_whole_set_not_the_page(tmp_path):
    conn = make_db(tmp_path)

    ascending = db.list_cases(conn, sort="city_cluster", direction="asc")
    descending = db.list_cases(conn, sort="city_cluster", direction="desc")

    assert [c["city_cluster"] for c in ascending["cases"]] == ["atlanta", "munich", "zurich"]
    assert [c["city_cluster"] for c in descending["cases"]] == ["zurich", "munich", "atlanta"]


def test_the_default_is_still_most_recently_touched_first(tmp_path):
    """The default keeps using idx_cases_updated -- test_query_plans pins that
    plan, and every other sort pays for a sort of the filtered set."""
    conn = make_db(tmp_path)
    leased = db.lease(conn, "ws-01", 1, 900)[0]

    first = db.list_cases(conn)["cases"][0]

    assert first["case_id"] == leased.case_id, "the case just touched must lead"


def test_a_sort_nobody_offers_falls_back_instead_of_raising(tmp_path):
    """The value arrives in a query string. It must not reach the ORDER BY, and a
    stale link must not 500."""
    conn = make_db(tmp_path)

    for bad in ("spec", "1; DROP TABLE cases", "", "cases.case_id"):
        out = db.list_cases(conn, sort=bad)
        assert len(out["cases"]) == 3, f"{bad!r} broke the listing"

    assert ids_in(db.list_cases(conn, sort="nonsense")) == ids_in(db.list_cases(conn))


def test_paging_a_sorted_list_never_repeats_or_skips_a_row(tmp_path):
    """Three cases share a state, so ordering by it alone is ambiguous and SQLite
    may return them in any order per page. case_id breaks every tie."""
    conn = make_db(tmp_path)

    pages = []
    for offset in range(0, 3):
        pages += ids_in(db.list_cases(conn, sort="state", direction="asc", limit=1, offset=offset))

    assert len(set(pages)) == 3, f"paging a sorted list returned {pages}"
    assert pages == ids_in(db.list_cases(conn, sort="state", direction="asc"))


def test_every_case_column_the_dashboard_offers_is_one_the_server_can_sort():
    """The header sends a key straight to the API. A label with a key the
    allowlist does not know falls back to the default and reads as a dead
    click -- which is exactly the kind of thing nobody notices until a demo."""
    dash = (pathlib.Path(__file__).resolve().parents[1]
            / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    block = dash[dash.index("const CASE_COLUMNS = ["):]
    block = block[:block.index("];")]
    import re
    keys = re.findall(r'\["[^"]+",\s*"([^"]+)"\]', block)
    assert len(keys) == 9, f"the case table draws {len(keys)} headers"
    for key in keys:
        assert key in db.CASE_SORTS, f"the dashboard offers {key}, the server cannot sort it"


def test_the_workers_table_sorts_every_column_it_draws():
    """The fleet table sorts in the browser, over the whole payload -- so every
    column it shows has to have a key, including the two derived ones."""
    dash = (pathlib.Path(__file__).resolve().parents[1]
            / "casebroker" / "static" / "dashboard.html").read_text(encoding="utf-8")
    block = dash[dash.index("const WORKER_COLUMNS = ["):]
    block = block[:block.index("];")]
    import re
    labels = re.findall(r'\["([^"]+)",\s*"([^"]+)"\]', block)
    # 8: the Build column added one, the Reliability column (done / done+failed)
    # was dropped, and so were the Done and Failed counts themselves (2026-09-24;
    # /v1/status still carries them).
    assert len(labels) == 8, f"the workers table draws {len(labels)} headers"
    # Status is a reading of last_seen; it has to sort by something real rather
    # than by a missing field.
    by_label = dict(labels)
    assert by_label["Status"] == "last_seen"
    assert by_label["Build"] == "build"
    assert "Reliability" not in by_label
