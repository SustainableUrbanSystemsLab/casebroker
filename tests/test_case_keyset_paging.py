"""GET /v1/cases?after=<case_id>: paging by key, so a moving campaign skips nothing.

`casebroker export` read a selection a page at a time by offset. A case that
left `done` between two pages shifted every later row up by one, and the row
that slid across the page boundary was never read. Paging after the last id
seen cannot lose a row that way.
"""
from __future__ import annotations

from casebroker import db


def _cases(n):
    return [{"case_id": f"c{i:02d}", "spec": {}, "recipe": "r", "city_cluster": "x",
             "split": "train", "priority": 100, "max_attempts": 3} for i in range(n)]


def _all_done(conn, n):
    db.add_cases(conn, _cases(n))
    conn.execute("UPDATE cases SET state = 'done'")


def test_key_pages_cover_every_case_once(tmp_path):
    conn = db.connect(str(tmp_path / "k.sqlite"))
    _all_done(conn, 7)
    seen, after = [], None
    while True:
        page = db.list_cases(conn, state="done", limit=3, sort="case_id", direction="asc", after=after)
        seen += [r["case_id"] for r in page["cases"]]
        assert page["total"] == 7, "the total is the whole selection, not what is left"
        if page["next_after"] is None:
            break
        after = page["next_after"]
    assert seen == [f"c{i:02d}" for i in range(7)]


def test_a_case_leaving_the_selection_mid_read_costs_no_other_case(tmp_path):
    conn = db.connect(str(tmp_path / "k.sqlite"))
    _all_done(conn, 6)
    first = db.list_cases(conn, state="done", limit=3, sort="case_id", direction="asc")
    conn.execute("UPDATE cases SET state = 'pending' WHERE case_id = 'c01'")    # re-opened meanwhile
    by_offset = db.list_cases(conn, state="done", limit=3, offset=3, sort="case_id", direction="asc")
    by_key = db.list_cases(conn, state="done", limit=3, after=first["next_after"])
    assert [r["case_id"] for r in by_offset["cases"]] == ["c04", "c05"], "the offset skips c03"
    assert [r["case_id"] for r in by_key["cases"]] == ["c03", "c04", "c05"]


def test_only_a_case_id_ordered_page_says_where_the_next_starts(tmp_path):
    conn = db.connect(str(tmp_path / "k.sqlite"))
    _all_done(conn, 2)
    assert "next_after" not in db.list_cases(conn)                       # the dashboard's default order
    assert db.list_cases(conn, sort="case_id", direction="asc", limit=5)["next_after"] is None
