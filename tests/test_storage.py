"""GET /v1/storage: how much space the database uses, and where it goes."""
from fastapi.testclient import TestClient

from casebroker.app import create_app

W = {"Authorization": "Bearer w"}
R = {"Authorization": "Bearer r"}


def _client(tmp_path):
    return TestClient(create_app(str(tmp_path / "b.sqlite"), tokens=["w"], readonly_tokens=["r"]))


def _cases(c, n):
    body = [{"lat": 48.10 + i * 0.001, "lon": 11.57, "recipe": "r", "city_cluster": "munich"} for i in range(n)]
    assert c.post("/v1/cases", json=body, headers=W).json()["added"] == n


def test_it_reports_the_whole_database_and_every_table(tmp_path, monkeypatch):
    monkeypatch.delenv("CASEBROKER_DB_QUOTA_MB", raising=False)
    c = _client(tmp_path)
    _cases(c, 40)
    d = c.get("/v1/storage", headers=R).json()

    assert d["engine"] == "sqlite"
    assert d["total_bytes"] > 0
    names = {t["name"]: t for t in d["tables"]}
    assert {"cases", "events", "footprints"} <= set(names)
    assert names["cases"]["rows"] == 40 and names["events"]["rows"] == 40, "one 'created' event per case"
    assert d["cases"] == 40
    if d["table_bytes"] is not None:
        assert d["bytes_per_case"] == round(d["table_bytes"] / 40)
        assert d["overhead_bytes"] == d["total_bytes"] - d["table_bytes"] >= 0
    assert names["cases"]["note"]
    # No limit is invented for a database whose plan nobody stated.
    assert d["quota_bytes"] is None and d["quota_source"] is None
    assert d["files"]["db_file_bytes"] > 0


def test_tables_are_largest_first_and_their_sizes_add_up(tmp_path):
    c = _client(tmp_path)
    _cases(c, 300)
    d = c.get("/v1/storage", headers=R).json()
    sizes = [t["bytes"] for t in d["tables"]]
    if any(s is None for s in sizes):
        return                              # this SQLite has no dbstat: sizes are honestly null
    assert sizes == sorted(sizes, reverse=True)
    # Every page belongs to some table or index, the schema, or the free list.
    assert sum(sizes) <= d["total_bytes"]
    assert sum(sizes) + d["files"]["free_bytes"] >= d["total_bytes"] * 0.9


def test_a_stated_limit_is_used_and_a_bad_one_is_reported_not_guessed(tmp_path, monkeypatch):
    c = _client(tmp_path)
    monkeypatch.setenv("CASEBROKER_DB_QUOTA_MB", "8192")
    d = c.get("/v1/storage", headers=R).json()
    assert d["quota_bytes"] == 8192 * 1024 * 1024 and d["quota_source"] == "CASEBROKER_DB_QUOTA_MB"

    monkeypatch.setenv("CASEBROKER_DB_QUOTA_MB", "lots")
    d = c.get("/v1/storage", headers=R).json()
    assert d["quota_bytes"] is None and "not a number" in d["quota_source"]


def test_it_needs_a_credential(tmp_path):
    assert _client(tmp_path).get("/v1/storage").status_code == 401
