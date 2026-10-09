"""`casebroker export`: the campaign as a dataset snapshot, pulled over the read API.

A broker is built and filled the way the fleet fills one -- cases posted, leased,
telemetry and fields sent under the lease, completed -- and the export reads it
back through the same HTTP API a workstation would. The expected numbers are
worked out here from the values the test sent, not from the exporter's helpers,
wherever a helper would make the test agree with itself.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import pathlib
import socket
import struct
import sys
import threading
import time

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
zarr = pytest.importorskip("zarr")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import USER_AGENT, __version__, cli, db, export, ids  # noqa: E402
from casebroker.app import create_app  # noqa: E402

V4, V5 = "cyl-1008/of12-v4", "cyl-1008/of12-v5"
W = {"Authorization": "Bearer w"}
NAN = float("nan")


def umag(values, *, case_id, direction, deg, nx=3, ny=2, height=1.75, u_ref=3.1) -> bytes:
    """A umag/1 blob as the node sends it (MetaFOAM.Deploy.PedestrianField.Encode): gzip-wrapped."""
    header = {"format": "umag/1", "case_id": case_id, "direction": direction, "deg": deg,
              "height_m": height, "nx": nx, "ny": ny, "x0": -2.0, "y0": -1.0, "spacing_m": 2.0,
              "coverage": 0.8, "u_ref": u_ref, "umag_p999": 5.4}
    hb = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return gzip.compress(b"UMAG" + struct.pack("<II", 1, len(hb)) + hb
                         + struct.pack("<%df" % len(values), *values), mtime=0)


def plain(blob: bytes) -> bytes:
    """What the broker stores, serves and hashes: the container without the gzip."""
    return gzip.decompress(blob)


# Four sites on land, in three splits (ids.split_for: ber/par train, atl test, nyc val).
A = dict(lat=52.52, lon=13.40, recipe=V4, city_cluster="ber", lcz="LCZ2", labels={"campaign": "pilot", "v4_twin": "1"})
B = dict(lat=33.80, lon=-84.40, recipe=V4, city_cluster="atl", lcz="LCZ6", labels={"campaign": "pilot"})
C = dict(lat=48.86, lon=2.35, recipe=V5, city_cluster="par", lcz=None, labels={"campaign": "main"})
D = dict(lat=40.71, lon=-74.00, recipe=V4, city_cluster="nyc", lcz="LCZ1", labels={"campaign": "pilot"})


def cid(site) -> str:
    return ids.case_id(site["lat"], site["lon"], site["recipe"])


A_ID, B_ID, C_ID, D_ID = cid(A), cid(B), cid(C), cid(D)

# Per case, the fields it is given: (direction, deg, values, height). NaN is a building.
FIELDS = {
    A_ID: [("case_000", 0.0, [1.0, 2.0, NAN, 4.0, 5.5, 0.0], 1.75),
           ("case_011", 11.25, [2.0, 2.5, 3.0, NAN, NAN, 1.0], 1.75)],
    B_ID: [("case_000", 0.0, [0.5, 0.5, 0.5, 0.5, 0.5, 0.5], 1.75)],
    C_ID: [("case_000", 0.0, [3.0, 3.0, 3.0, 3.0, NAN, 6.0], 1.75),
           ("case_022", 22.5, [1.0, 1.0, 1.0, 1.0, 1.0, 9.0], 1.75),
           ("case_000", 0.0, [7.0, 7.0, 7.0, 7.0, 7.0, 7.0], 10.0)],
}


def _finish(c: TestClient, site, telemetry: dict, metrics: dict) -> str:
    """Post one case and take it through a node's whole life: lease, telemetry,
    fields under the lease, complete. One pending case at a time, so the lease is it."""
    r = c.post("/v1/cases", json=[site], headers=W)
    assert r.status_code == 200 and r.json()["rejected_not_on_land"] == 0, r.text
    lease = c.post("/v1/lease", json={"worker_id": "foam-1", "count": 1}, headers=W).json()[0]
    case_id, lease_id = lease["case_id"], lease["lease_id"]
    assert case_id == cid(site)
    for kind, data in telemetry.items():
        r = c.post("/v1/telemetry", json={"lease_id": lease_id, "case_id": case_id, "kind": kind, "data": data},
                   headers=W)
        assert r.status_code == 200, r.text
    for direction, deg, values, height in FIELDS[case_id]:
        r = c.put(f"/v1/cases/{case_id}/fields/{direction}", params={"lease_id": lease_id},
                  content=umag(values, case_id=case_id, direction=direction, deg=deg, height=height),
                  headers={**W, "content-type": "application/octet-stream"})
        assert r.status_code == 200, r.text
    r = c.post("/v1/complete", json={"lease_id": lease_id, "case_id": case_id,
                                     "result_uri": f"file:///done/{case_id}.tar.gz",
                                     "sha256": hashlib.sha256(case_id.encode()).hexdigest(),
                                     "bytes": 1234, "metrics": metrics}, headers=W)
    assert r.status_code == 200, r.text
    return case_id


def _seed(c: TestClient) -> None:
    _finish(c, A, {
        "site": {"urban_form": {"bcr": 0.31, "bht_m": 14.2, "lambda_f_min": 0.21, "lambda_f_max": 0.33,
                                "lambda_f_by_direction": {"000": 0.21, "011.25": 0.33}},
                 "n_buildings": 412, "terrain_relief_m": 6.5, "canopy_fraction": 0.18},
        "mesh": {"total_cells": 2_100_000, "build": "1.14.0.900+abc",
                 "meshes": {"case": {"ok": True, "max_skewness": 3.1, "max_non_orthogonality": 64.0}}}},
        {"case_seconds": 7200, "mesh_seconds": 900, "solve_seconds": 6000,
         "height_source": "gba-lod1", "build": "1.14.0.900+abc"})
    # No build in the completion: the mesh report's is the one there is.
    _finish(c, B, {"mesh": {"total_cells": 1_500_000, "build": "1.14.0.800+def"}},
            {"case_seconds": 3600})
    # Finished before telemetry: its urban form is in the completion metrics only.
    _finish(c, C, {}, {"urban_form": {"bcr": 0.5}, "mesh_cells": 900_000, "case_seconds": 1800})
    r = c.post("/v1/cases", json=[D], headers=W)        # never leased: pending
    assert r.status_code == 200, r.text


@pytest.fixture()
def broker(tmp_path):
    """(app, a read-scope client over it): three done cases and one pending."""
    app = create_app(db_path=str(tmp_path / "b.sqlite"), tokens=["w"], readonly_tokens=["r"])
    _seed(TestClient(app))
    return app, TestClient(app, headers={"Authorization": "Bearer r"})


class Counting:
    """The read client, counting what it is asked for -- and, with ``spoil``, handing
    back a field whose bytes are not the ones the broker hashed."""

    def __init__(self, http, spoil: str | None = None):
        self.http, self.spoil, self.paths = http, spoil, []
        self.lock = threading.Lock()

    def get(self, path, **kw):
        with self.lock:
            self.paths.append(path)
        r = self.http.get(path, **kw)
        if self.spoil and path.endswith(self.spoil) and r.status_code == 200:
            return httpx.Response(200, content=r.content[:-4] + b"\0\0\0\0", headers=r.headers)
        return r

    def blobs(self) -> list[str]:
        return [p for p in self.paths if "/fields/" in p]


def opts(out, **kw) -> export.Options:
    return export.Options(broker="http://testserver", out=pathlib.Path(out), **kw)


def table(path) -> list[dict]:
    return pq.read_table(path).to_pylist()


def by(rows, *keys):
    return {tuple(r[k] for k in keys) if len(keys) > 1 else r[keys[0]]: r for r in rows}


def store_digest(root: pathlib.Path) -> str:
    """sha256sum's lines for every file under root, sorted by path, hashed: what the card claims."""
    lines = []
    for dirpath, _, names in os.walk(root):
        for name in names:
            p = pathlib.Path(dirpath, name)
            lines.append((p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()))
    return hashlib.sha256("".join(f"{sha}  {rel}\n" for rel, sha in sorted(lines)).encode()).hexdigest()


def test_the_client_names_itself():
    """A proxy in front of a broker may refuse a library's default agent (c44091a)."""
    c = export.client("https://b.example/", "t")
    assert c.headers["User-Agent"] == USER_AGENT and c.headers["Authorization"] == "Bearer t"
    assert str(c.base_url) == "https://b.example"


def test_an_export_writes_the_cases_the_fields_the_store_and_the_card(broker, tmp_path):
    _, http = broker
    out = tmp_path / "ds"
    assert export.run(opts(out), http=http, say=lambda m: None) == 0

    cases = by(table(out / "cases.parquet"), "case_id")
    assert sorted(cases) == sorted([A_ID, B_ID, C_ID]), "done only, by default"
    a, b, c = cases[A_ID], cases[B_ID], cases[C_ID]
    assert (a["recipe"], a["split"], a["lcz"], a["city_cluster"], a["state"]) == (V4, "train", "LCZ2", "ber", "done")
    assert (a["lat"], a["lon"]) == (52.52, 13.40)
    assert dict(a["labels"]) == {"campaign": "pilot", "v4_twin": "1"}
    assert a["country_code"] == "DE" and b["country_code"] == "US" and c["country_code"] == "FR"
    assert (a["bcr"], a["bht_m"], a["lambda_f_min"], a["lambda_f_max"]) == (0.31, 14.2, 0.21, 0.33)
    assert (a["n_buildings"], a["terrain_relief_m"], a["canopy_fraction"]) == (412, 6.5, 0.18)
    assert (a["total_cells"], a["max_skewness"], a["max_non_orthogonality"]) == (2_100_000, 3.1, 64.0)
    assert (a["case_seconds"], a["mesh_seconds"], a["solve_seconds"]) == (7200, 900, 6000)
    assert (a["height_source"], a["build"], a["worker"]) == ("gba-lod1", "1.14.0.900+abc", "foam-1")
    assert a["result_uri"] == f"file:///done/{A_ID}.tar.gz"
    assert a["result_sha256"] == hashlib.sha256(A_ID.encode()).hexdigest() and a["result_bytes"] == 1234
    assert b["build"] == "1.14.0.800+def", "the mesh report's build when the completion names none"
    assert b["height_source"] is None and b["bcr"] is None, "unreported stays unreported, not 0"
    assert (c["bcr"], c["total_cells"]) == (0.5, 900_000), "the completion metrics when telemetry has none"
    assert (a["n_fields"], b["n_fields"], c["n_fields"]) == (2, 1, 2), "C's 10 m field is not at 1.75 m"

    fields = by(table(out / "fields.parquet"), "case_id", "direction")
    assert sorted(fields) == sorted([(A_ID, "case_000"), (A_ID, "case_011"), (B_ID, "case_000"),
                                     (C_ID, "case_000"), (C_ID, "case_022")])
    f = fields[(A_ID, "case_000")]
    valid = [1.0, 2.0, 4.0, 5.5, 0.0]
    assert (f["deg"], f["height_m"], f["u_ref"], f["coverage"]) == (0.0, 1.75, pytest.approx(3.1), pytest.approx(0.8))
    assert f["n_valid"] == 5 and f["umag_max"] == 5.5 and f["umag_min"] == 0.0
    assert f["umag_mean"] == pytest.approx(sum(valid) / 5)
    assert f["vr_max"] == pytest.approx(5.5 / 3.1) and f["vr_p999"] == pytest.approx(5.4 / 3.1)
    assert (f["nx"], f["ny"], f["x0"], f["y0"], f["spacing_m"]) == (3, 2, -2.0, -1.0, 2.0)
    blob = umag(FIELDS[A_ID][0][2], case_id=A_ID, direction="case_000", deg=0.0)
    assert f["sha256"] == hashlib.sha256(plain(blob)).hexdigest() and f["bytes"] == len(plain(blob))
    assert f["zarr"] == f"{A_ID}/case_000"
    assert f["lambda_f"] == 0.21 and fields[(A_ID, "case_011")]["lambda_f"] == 0.33, "matched by angle"
    assert fields[(C_ID, "case_000")]["lambda_f"] is None, "no table: no value, never 0"

    root = zarr.open_group(str(out / "fields.zarr"), mode="r")
    assert sorted(root.group_keys()) == sorted([A_ID, B_ID, C_ID])
    for case_id, sent in FIELDS.items():
        for direction, deg, values, height in sent:
            if height != 1.75:
                assert direction not in root[case_id] or root[case_id][direction].attrs["height_m"] == 1.75
                continue
            arr = root[case_id][direction]
            assert arr.shape == (2, 3) and arr.dtype == np.float32
            np.testing.assert_array_equal(arr[...], np.array(values, dtype=np.float32).reshape(2, 3))
            attrs = dict(arr.attrs)
            assert (attrs["deg"], attrs["height_m"], attrs["units"]) == (deg, 1.75, "m/s")
            assert attrs["u_ref"] == pytest.approx(3.1)
            assert (attrs["origin"], attrs["spacing_m"], attrs["shape"]) == ([-2.0, -1.0], 2.0, [2, 3])
            assert attrs["sha256"] == fields[(case_id, direction)]["sha256"]
    assert np.isnan(root[A_ID]["case_000"][0, 2]), "a building stays NaN"

    card = json.loads((out / "dataset.json").read_text())
    assert card["schema_version"] == export.SCHEMA_VERSION and card["format"] == "casebroker-dataset"
    assert card["broker"] == {"url": "http://testserver", "version": __version__, "auth": "token"}
    assert card["filters"] == {"recipe": [], "split": [], "label": [], "state": "done", "height_m": 1.75,
                               "limit": None, "fields": True}
    assert card["counts"] == {"cases": 3, "fields": 5, "fields_in_zarr": 5,
                              "by_split": {"test": 1, "train": 2}, "by_recipe": {V4: 2, V5: 1},
                              "by_lcz": {"LCZ2": 1, "LCZ6": 1, "unknown": 1},
                              "fields_per_case": {"1": 1, "2": 2}}
    for name in ("cases.parquet", "fields.parquet"):
        assert card["files"][name] == {"sha256": hashlib.sha256((out / name).read_bytes()).hexdigest(),
                                       "bytes": (out / name).stat().st_size}
    assert card["files"]["fields.zarr"]["sha256"] == store_digest(out / "fields.zarr")
    assert card["problems"] == []
    assert card["exported_at"].endswith("Z")


def test_a_second_run_downloads_nothing_and_replaces_the_tables_whole(broker, tmp_path):
    _, http = broker
    out = tmp_path / "ds"
    first = Counting(http)
    assert export.run(opts(out), http=first, say=lambda m: None) == 0
    assert len(first.blobs()) == 5
    before = {n: (out / n).stat().st_ino for n in ("cases.parquet", "fields.parquet", "dataset.json")}
    rows = table(out / "fields.parquet")

    again = Counting(http)
    assert export.run(opts(out), http=again, say=lambda m: None) == 0
    assert again.blobs() == [], "every stored field still matches the broker's hash"
    assert table(out / "fields.parquet") == rows
    after = {n: (out / n).stat().st_ino for n in before}
    assert all(before[n] != after[n] for n in before), "written beside and renamed over, not in place"
    assert not [p for p in out.iterdir() if p.name.startswith(".")], "no temporary left behind"


def test_a_stale_or_unfinished_array_is_fetched_again_and_only_that_one(broker, tmp_path):
    _, http = broker
    out = tmp_path / "ds"
    assert export.run(opts(out), http=http, say=lambda m: None) == 0
    stale = out / "fields.zarr" / A_ID / "case_011" / "zarr.json"
    meta = json.loads(stale.read_text())
    meta["attributes"]["sha256"] = "0" * 64                 # written from other bytes
    stale.write_text(json.dumps(meta))
    cut = out / "fields.zarr" / C_ID / "case_022" / "zarr.json"
    meta = json.loads(cut.read_text())
    del meta["attributes"]["sha256"]                         # a crash between values and hash
    cut.write_text(json.dumps(meta))

    again = Counting(http)
    assert export.run(opts(out), http=again, say=lambda m: None) == 0
    assert sorted(again.blobs()) == sorted([f"/v1/cases/{A_ID}/fields/case_011", f"/v1/cases/{C_ID}/fields/case_022"])
    root = zarr.open_group(str(out / "fields.zarr"), mode="r")
    np.testing.assert_array_equal(root[A_ID]["case_011"][...],
                                  np.array(FIELDS[A_ID][1][2], dtype=np.float32).reshape(2, 3))


@pytest.mark.parametrize("kw, expect", [
    ({"recipes": [V5]}, [C_ID]),
    ({"recipes": [V4, V5]}, [A_ID, B_ID, C_ID]),
    ({"splits": ["test"]}, [B_ID]),
    ({"splits": ["test", "train"], "recipes": [V4]}, [A_ID, B_ID]),
    ({"labels": ["campaign:pilot"]}, [A_ID, B_ID]),
    ({"labels": ["campaign:pilot", "v4_twin"]}, [A_ID]),
    ({"labels": ["v4_twin", "campaign:main"]}, []),
    ({"state": None}, [A_ID, B_ID, C_ID, D_ID]),
    ({"state": "pending"}, [D_ID]),
])
def test_the_filters_select_the_cases(broker, tmp_path, kw, expect):
    _, http = broker
    out = tmp_path / "ds"
    assert export.run(opts(out, **kw), http=http, say=lambda m: None) == 0
    assert sorted(r["case_id"] for r in table(out / "cases.parquet")) == sorted(expect)
    assert {r["case_id"] for r in table(out / "fields.parquet")} <= set(expect)


def test_limit_takes_the_first_cases_by_id(broker, tmp_path):
    _, http = broker
    out = tmp_path / "ds"
    assert export.run(opts(out, limit=2), http=http, say=lambda m: None) == 0
    assert [r["case_id"] for r in table(out / "cases.parquet")] == sorted([A_ID, B_ID, C_ID])[:2]


def test_a_narrower_rerun_keeps_what_it_can_and_removes_the_rest(broker, tmp_path):
    """--out is the snapshot: what the card does not vouch for is not left in it."""
    _, http = broker
    out = tmp_path / "ds"
    assert export.run(opts(out), http=http, say=lambda m: None) == 0
    narrow = Counting(http)
    assert export.run(opts(out, recipes=[V5]), http=narrow, say=lambda m: None) == 0
    assert narrow.blobs() == [], "C's fields were already there"
    root = zarr.open_group(str(out / "fields.zarr"), mode="r")
    assert list(root.group_keys()) == [C_ID]
    card = json.loads((out / "dataset.json").read_text())
    assert card["counts"]["cases"] == 1 and card["filters"]["recipe"] == [V5]
    assert card["files"]["fields.zarr"]["sha256"] == store_digest(out / "fields.zarr")


def test_another_height_is_its_own_selection(broker, tmp_path):
    _, http = broker
    out = tmp_path / "ds"
    assert export.run(opts(out, height_m=10.0), http=http, say=lambda m: None) == 0
    fields = table(out / "fields.parquet")
    assert [(f["case_id"], f["direction"], f["height_m"]) for f in fields] == [(C_ID, "case_000", 10.0)]
    root = zarr.open_group(str(out / "fields.zarr"), mode="r")
    np.testing.assert_array_equal(root[C_ID]["case_000"][...], np.full((2, 3), 7.0, dtype=np.float32))


def test_no_fields_writes_the_tables_and_touches_no_store(broker, tmp_path):
    _, http = broker
    out = tmp_path / "ds"
    counting = Counting(http)
    assert export.run(opts(out, fields=False), http=counting, say=lambda m: None) == 0
    assert counting.blobs() == [] and not (out / "fields.zarr").exists()
    fields = table(out / "fields.parquet")
    assert len(fields) == 5 and all(f["zarr"] is None for f in fields)
    assert fields[0]["umag_max"] is not None, "the statistics come from the broker's record"
    card = json.loads((out / "dataset.json").read_text())
    assert card["files"]["fields.zarr"] is None and card["counts"]["fields_in_zarr"] == 0


def test_a_field_whose_bytes_do_not_match_their_hash_is_named_and_not_stored(broker, tmp_path):
    _, http = broker
    out = tmp_path / "ds"
    said = []
    assert export.run(opts(out), http=Counting(http, spoil=f"{B_ID}/fields/case_000"), say=said.append) == 1
    assert not (out / "fields.zarr" / B_ID / "case_000").exists()
    row = by(table(out / "fields.parquet"), "case_id", "direction")[(B_ID, "case_000")]
    assert row["zarr"] is None and row["sha256"], "listed, with the broker's hash, but not in the store"
    card = json.loads((out / "dataset.json").read_text())
    assert [(p["case_id"], p["direction"]) for p in card["problems"]] == [(B_ID, "case_000")]
    assert "hash to" in card["problems"][0]["error"] and card["counts"]["fields_in_zarr"] == 4
    assert any("MISSING" in m and B_ID in m for m in said)
    # The next run, given the right bytes, fills the hole and nothing else.
    again = Counting(http)
    assert export.run(opts(out), http=again, say=lambda m: None) == 0
    assert again.blobs() == [f"/v1/cases/{B_ID}/fields/case_000"]


def test_a_field_the_broker_has_not_summarised_yet_gets_its_statistics_here(broker, tmp_path):
    """A field stored before the broker computed statistics has none until its
    background pass reaches it. The export computes the same function of the same
    bytes, on the run that downloads it and on every run that keeps it."""
    _, http = broker
    conn = db.connect(str(tmp_path / "b.sqlite"))
    conn.execute("UPDATE case_fields SET " + ", ".join(c + "=NULL" for c in db._FIELD_STATS_COLUMNS)
                 + " WHERE case_id=? AND direction='case_000'", (A_ID,))
    listed = {f["direction"]: f for f in http.get(f"/v1/cases/{A_ID}/fields").json()["fields"]}
    assert listed["case_000"]["n_valid"] is None and listed["case_011"]["n_valid"] == 4
    out = tmp_path / "ds"
    for _ in range(2):                                   # downloaded, then kept
        assert export.run(opts(out), http=http, say=lambda m: None) == 0
        f = by(table(out / "fields.parquet"), "case_id", "direction")[(A_ID, "case_000")]
        assert (f["n_valid"], f["umag_min"], f["umag_max"]) == (5, 0.0, 5.5)
        assert f["umag_mean"] == pytest.approx(2.5) and f["umag_p50"] == pytest.approx(2.0)
        assert f["vr_max"] == pytest.approx(5.5 / 3.1)


def test_a_refused_credential_stops_the_export(broker, tmp_path):
    app, _ = broker
    with pytest.raises(export.ExportError, match="refused the credential"):
        export.run(opts(tmp_path / "ds"), http=TestClient(app, headers={"Authorization": "Bearer nope"}),
                   say=lambda m: None)


def test_without_the_extra_the_command_says_which_one(broker, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "zarr", None)              # import zarr -> ImportError
    rc = cli.main(["export", "--broker", "http://testserver", "--out", str(tmp_path / "ds")])
    assert rc == 2
    assert capsys.readouterr().err.strip() == export.NEEDS_EXTRA
    # Tables only need pyarrow alone.
    _, http = broker
    assert export.run(opts(tmp_path / "t", fields=False), http=http, say=lambda m: None) == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_command_exports_from_a_live_broker_with_a_read_token_from_stdin(tmp_path, monkeypatch):
    """The real path: argparse, a token on stdin, the export's own httpx client over a
    socket, four workers -- and the broker sees the agent and nothing but reads."""
    import uvicorn
    app = create_app(db_path=str(tmp_path / "live.sqlite"), tokens=["w"], readonly_tokens=["r"])
    _seed(TestClient(app))
    seen: list[tuple[str, str, str | None]] = []

    async def recording(scope, receive, send):
        """The app, behind a note of every request's method, path and agent."""
        if scope["type"] == "http":
            agent = dict(scope["headers"]).get(b"user-agent")
            seen.append((scope["method"], scope["path"], agent.decode() if agent else None))
        await app(scope, receive, send)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(recording, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        deadline = time.time() + 30
        while not server.started:
            assert time.time() < deadline, "server did not come up"
            time.sleep(0.05)
        monkeypatch.setattr(sys, "stdin", io.StringIO("r\n"))
        out = tmp_path / "ds"
        rc = cli.main(["export", "--broker", f"http://127.0.0.1:{port}", "--token", "-", "--out", str(out),
                       "--workers", "4", "--label", "campaign:pilot"])
        assert rc == 0
    finally:
        server.should_exit = True
    assert sorted(r["case_id"] for r in table(out / "cases.parquet")) == sorted([A_ID, B_ID])
    assert len(table(out / "fields.parquet")) == 3
    assert seen and {m for m, _, _ in seen} == {"GET"}, "an export never writes to the broker"
    assert {ua for _, _, ua in seen} == {USER_AGENT}
    card = json.loads((out / "dataset.json").read_text())
    assert card["broker"]["url"] == f"http://127.0.0.1:{port}" and card["filters"]["label"] == ["campaign:pilot"]
    assert card["filters"]["height_m"] == 1.75 and card["filters"]["state"] == "done"
