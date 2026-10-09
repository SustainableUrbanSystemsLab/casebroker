"""``casebroker export`` -- the campaign as a dataset a model trains on, on disk.

The broker holds every finished direction's pedestrian field and everything the
node said about the site it was solved on, and it answers questions of them in
place (docs/protocol.md, "Asking the wind field"). Training wants the opposite:
every field of the selection, local, in a layout a data loader reads without a
broker in the loop, and a record of exactly which broker and which filters it
came from, so two runs of an experiment can be shown to have read one set.

This is a CLIENT of the broker's read API and nothing else: it pulls with a read
credential (a write one passes too, DOMAIN.md invariant 7) and sends no write.

    <out>/dataset.json    the card: broker and version, filters, counts, sha256 of each file
    <out>/cases.parquet   one row per case: what it is, where, the site's numbers, its result
    <out>/fields.parquet  one row per (case, direction): grid, u_ref, lambda_f, statistics
    <out>/fields.zarr     a group per case, an array per direction: float32 |U|, NaN in buildings

``--out`` IS the snapshot. A re-run into it keeps every field whose bytes the
broker still holds unchanged -- the array carries the sha256 of the umag/1 blob
it was written from, and the broker's field record carries the same hash, so
the comparison costs no download -- fetches the rest, removes what the new
selection no longer holds, and replaces the tables and the card atomically. The
card is written last, so it never describes files that are not there yet.

pyarrow and zarr are the ``export`` extra (pyproject.toml): the server image
never runs this and does not carry them.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from . import USER_AGENT, dataset, db, umag

#: Bumped when a column, an attribute or the card changes meaning or goes away.
#: Adding a column is not a bump: a reader that names its columns keeps working.
SCHEMA_VERSION = 1
CARD, CASES, FIELDS, STORE = "dataset.json", "cases.parquet", "fields.parquet", "fields.zarr"
NEEDS_EXTRA = "casebroker export needs the export extra: uv run --extra export casebroker export ..."

#: db.list_cases serves at most 200 rows a page.
PAGE = 200
#: A request is tried this many times on a dropped connection, a 5xx or a 429,
#: backing off from RETRY_DELAY, as the node worker does (worker._post).
RETRIES = 4
RETRY_DELAY = 2.0
#: Field statistics as the broker stores them (db._FIELD_SUMMARY_COLUMNS) and the
#: node's own 99.9th percentile; each also becomes vr_<stat> = umag_<stat> / u_ref.
_STATS = umag.STATS + ("p999",)
#: A case id or direction off the wire becomes a directory in the store, so it must
#: be a plain name. The broker's own are (ids.case_id, db.PART_NAME); this holds the
#: line against one that is not, rather than writing wherever "../" points.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ExportError(RuntimeError):
    """Nothing sensible can be exported: the broker cannot be reached, or it
    refuses the credential. Raised out of the run; the command exits 2."""


class _Gone(Exception):
    """404: the case or field went between the listing and the fetch (a purge)."""


class _Failed(Exception):
    """One case or field could not be had (a 5xx past the retries, bytes that do
    not match their hash). Recorded on the card; the rest of the export goes on."""


@dataclass
class Options:
    broker: str
    out: pathlib.Path
    token: str | None = None
    recipes: list[str] = field(default_factory=list)
    splits: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    state: str | None = "done"
    height_m: float = db.FIELD_HEIGHT_M
    limit: int | None = None
    workers: int = 4
    fields: bool = True


def _require(fields: bool) -> None:
    """Import what the export writes with, or say which extra provides it."""
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
        if fields:
            import zarr  # noqa: F401
    except ImportError as exc:
        raise ExportError(NEEDS_EXTRA) from exc


# -- talking to the broker ------------------------------------------------------------

def client(broker: str, token: str | None, timeout: float = 120.0) -> httpx.Client:
    """The HTTP client the export reads with: one connection pool shared by every
    worker thread. It names itself (casebroker.USER_AGENT): a proxy in front of a
    broker may refuse a library's default agent outright (commit c44091a)."""
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=broker.rstrip("/"), headers=headers, timeout=timeout)


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    return str(body.get("detail", body))[:200] if isinstance(body, dict) else str(body)[:200]


def _get(http, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
    """GET with the worker's backoff. A refused credential is fatal for the whole
    export, and so is a broker that stops answering at all: every case left would
    otherwise spend the whole backoff learning the same thing. A 404 and a 5xx
    that outlasts the retries belong to ONE case or field."""
    delay, last = RETRY_DELAY, None
    for attempt in range(RETRIES):
        try:
            r = http.get(path, params=params)
        except httpx.TransportError as exc:
            last = exc
        else:
            if r.status_code in (401, 403):
                raise ExportError(f"the broker refused the credential ({r.status_code} on {path}: {_detail(r)}); "
                                  "a read token is enough: --token, '-' for stdin, or $CASEBROKER_TOKEN")
            if r.status_code == 404:
                raise _Gone(_detail(r))
            if r.status_code < 400:
                return r
            if r.status_code < 500 and r.status_code != 429:
                raise _Failed(f"{r.status_code} on {path}: {_detail(r)}")
            last = _Failed(f"{r.status_code} on {path}: {_detail(r)}")
        if attempt + 1 < RETRIES:
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    if isinstance(last, _Failed):
        raise last
    raise ExportError(f"the broker stopped answering ({path}: {last})")


def _health(http) -> dict[str, Any]:
    try:
        return _get(http, "/healthz").json()
    except (_Gone, _Failed) as exc:
        raise ExportError(f"the broker's /healthz did not answer: {exc}") from None


def _has_label(row: dict[str, Any], label: str) -> bool:
    """``key:value`` or ``key``, as the broker reads ``label`` (db.list_cases)."""
    key, _, value = label.partition(":")
    labels = row.get("labels") or {}
    key = key.strip()
    return key in labels and (not value or labels[key] == value.strip())


def select(http, opts: Options) -> list[dict[str, Any]]:
    """The cases the filters name, in case_id order.

    ``GET /v1/cases`` takes one recipe, one split and one label per request, so a
    repeated ``--recipe`` or ``--split`` is a request per combination (a case has
    exactly one of each: repeating either is OR), and labels past the first are
    checked here on each row's own labels (repeating ``--label`` is AND, as each
    narrows). Paged by case_id, never by the default "recently touched" order,
    which a live campaign reorders under the reader between pages."""
    first, rest = (opts.labels[0], opts.labels[1:]) if opts.labels else (None, [])
    found: dict[str, dict[str, Any]] = {}
    for recipe in opts.recipes or [None]:
        for split in opts.splits or [None]:
            offset = kept = 0
            after: str | None = None
            while True:
                params: dict[str, Any] = {"limit": PAGE, "offset": offset, "sort": "case_id",
                                          "direction": "asc", "include_spec": "false"}
                if after is not None:
                    # By key, once the broker has shown it pages that way: an
                    # offset skips a case that changed state between two pages.
                    params["after"] = after
                    params.pop("offset")
                for k, v in (("state", opts.state), ("recipe", recipe), ("split", split), ("label", first)):
                    if v:
                        params[k] = v
                try:
                    page = _get(http, "/v1/cases", params).json()
                except (_Gone, _Failed) as exc:
                    raise ExportError(f"the broker would not list cases: {exc}") from None
                rows = page.get("cases") or []
                for row in rows:
                    if all(_has_label(row, lab) for lab in rest):
                        found[row["case_id"]] = row
                        kept += 1
                offset += len(rows)
                # Each combination is read in case_id order, so its first `limit`
                # matches hold every one of its cases the merged first `limit` can.
                if not rows or (opts.limit and kept >= opts.limit):
                    break
                if "next_after" in page:
                    # A broker that pages by key (0.40.1 on) says where the next
                    # page starts, and None on the last one.
                    if page["next_after"] is None:
                        break
                    after = page["next_after"]
                elif offset >= int(page.get("total") or 0):
                    break
    out = [found[k] for k in sorted(found)]
    return out[:opts.limit] if opts.limit else out


# -- one case -------------------------------------------------------------------------

def _float(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> int | None:
    f = _float(v)
    return int(f) if f is not None and f.is_integer() else None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _case_row(detail: dict[str, Any]) -> dict[str, Any]:
    """The case as a row: identity, place, and the site's numbers.

    The numbers are /v1/dataset's own registry (dataset.METRICS, read the way it
    reads them: the node's telemetry first, the completion metrics second), so a
    column here and a histogram on the dashboard are the same number for the same
    case and cannot drift apart."""
    spec = dataset.parse_obj(detail.get("spec"))
    metrics = dataset.parse_obj(detail.get("metrics"))
    telemetry = dataset.parse_obj(detail.get("telemetry"))
    place = detail.get("place") if isinstance(detail.get("place"), dict) else {}
    labels = detail.get("labels") if isinstance(detail.get("labels"), dict) else {}
    row: dict[str, Any] = {
        "case_id": detail["case_id"], "recipe": _str(detail.get("recipe")),
        "split": _str(detail.get("split")), "lcz": _str(detail.get("lcz")),
        "city_cluster": _str(detail.get("city_cluster")), "state": _str(detail.get("state")),
        "lat": _float(spec.get("lat")), "lon": _float(spec.get("lon")),
        "labels": sorted((str(k), str(v)) for k, v in labels.items()),
        "country": _str(place.get("country")), "country_code": _str(place.get("country_code")),
        "town": _str(place.get("town")), "town_km": _float(place.get("town_km")),
        "region": _str(place.get("region")),
    }
    values = dataset.values_of(dataset._case(detail))
    for m in dataset.METRICS:
        row[m.key] = _float(values.get(m.key))
    mesh = telemetry.get("mesh") if isinstance(telemetry.get("mesh"), dict) else {}
    # Which building source the mesh was built from (DOMAIN.md, "Which buildings,
    # for this case"): None for a case finished before the runner reported it,
    # which is a different answer from "gba-lod1" and must stay one.
    row["height_source"] = _str(metrics.get("height_source"))
    row["build"] = _str(metrics.get("build")) or _str(mesh.get("build"))
    # The Python worker writes its id into the metrics; an E3D node does not, and the
    # broker's own trail (the worker of the case's last event: for a done case, the
    # completion's) answers for it.
    row["worker"] = _str(metrics.get("worker")) or _str(detail.get("last_worker"))
    row["result_uri"] = _str(detail.get("result_uri"))
    row["result_sha256"] = _str(detail.get("result_sha256"))
    row["result_bytes"] = _int(detail.get("result_bytes"))
    return row


def _field_row(case_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"case_id": case_id, "direction": rec.get("direction")}
    for k in ("deg", "height_m", "u_ref", "lambda_f", "coverage", "x0", "y0", "spacing_m"):
        row[k] = _float(rec.get(k))
    row["n_valid"] = _int(rec.get("n_valid"))
    for k in _STATS:
        row[f"umag_{k}"] = _float(rec.get(f"umag_{k}"))
    row.update(umag.ratios(row))
    for k in ("nx", "ny", "bytes", "reported_at"):
        row[k] = _int(rec.get(k))
    row["sha256"] = _str(rec.get("sha256"))
    row["zarr"] = None
    return row


def _stored_sha(array_dir: pathlib.Path) -> str | None:
    """The sha256 a stored array was written from, or None when there is no array
    or it never finished. Read from the array's zarr.json directly (the Zarr v3
    spec's own metadata document) rather than through zarr: a re-run checks every
    field of the campaign, 160,000 of them, and this is one small file read each."""
    try:
        meta = json.loads((array_dir / "zarr.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    attrs = meta.get("attributes") if isinstance(meta, dict) else None
    sha = attrs.get("sha256") if isinstance(attrs, dict) else None
    return sha if isinstance(sha, str) else None


def _fetch_field(http, case_id: str, rec: dict[str, Any]):
    """One field's values, ``(ny, nx)`` float32, and the sha256 of the blob they came
    from -- checked against the broker's record of it before anything is written."""
    r = _get(http, f"/v1/cases/{case_id}/fields/{rec['direction']}", {"height_m": float(rec["height_m"])})
    blob = r.content
    got = hashlib.sha256(blob).hexdigest()
    if rec.get("sha256") and got != rec["sha256"]:
        raise _Failed(f"its bytes hash to {got[:16]}..., the broker's record says {rec['sha256'][:16]}...")
    try:
        db.decode_umag(blob)                       # the container: magic, version, header, size
        _, values = umag.values(db.umag_container(blob))
    except ValueError as exc:
        raise _Failed(f"not a umag/1 field: {exc}") from None
    return values, got


def _write_array(group, case_id: str, rec: dict[str, Any], values, sha: str) -> None:
    import numpy as np
    ny, nx = values.shape
    attrs = {
        "case_id": case_id, "direction": rec["direction"], "units": "m/s",
        "quantity": "|U|, the wind speed at height_m above grade; NaN where there is no air (a building)",
        "deg": _float(rec.get("deg")), "height_m": _float(rec.get("height_m")), "u_ref": _float(rec.get("u_ref")),
        "coverage": _float(rec.get("coverage")), "lambda_f": _float(rec.get("lambda_f")),
        # The site frame: metres east (+x) and north (+y) of the case's lat/lon, by the
        # node's own constants (umag.to_local). Row j, column i is (x0 + i*s, y0 + j*s).
        "origin": [float(rec["x0"]), float(rec["y0"])], "spacing_m": float(rec["spacing_m"]),
        "shape": [int(ny), int(nx)],
    }
    # One chunk per field: a field is read whole (a 504 x 504 lattice is 1 MB), and a
    # chunk per tile would multiply the files of a 160,000-field store for nothing.
    arr = group.create_array(rec["direction"], shape=(ny, nx), chunks=(ny, nx), dtype="float32",
                             fill_value=float("nan"), dimension_names=("y", "x"),
                             overwrite=True, attributes=attrs)
    arr[...] = np.asarray(values, dtype="float32")
    # Last, and only now: the hash is what a re-run trusts to skip this field, so it
    # must never be on an array whose values a crash cut short.
    arr.update_attributes({"sha256": sha})


@dataclass
class _CaseResult:
    case_id: str
    case: dict[str, Any] | None = None
    fields: list[dict[str, Any]] = field(default_factory=list)
    stored: set[str] = field(default_factory=set)      # directions whose array is current
    downloaded: int = 0
    kept: int = 0
    gone: bool = False
    problems: list[dict[str, Any]] = field(default_factory=list)


def _export_case(http, row: dict[str, Any], opts: Options, store) -> _CaseResult:
    cid = row["case_id"]
    out = _CaseResult(cid)
    try:
        detail = _get(http, f"/v1/cases/{cid}").json()
    except _Gone:
        out.gone = True
        return out
    except _Failed as exc:
        out.problems.append({"case_id": cid, "direction": None, "error": str(exc)})
        return out
    out.case = _case_row(detail)
    # GET /v1/cases/{id} lists every height a case holds; one per direction is
    # taken, at exactly --height-m, as GET /v1/fields?height_m= selects it.
    records = db.choose_heights([f for f in detail.get("fields") or [] if isinstance(f, dict)], opts.height_m)
    group = store.require_group(cid) if store is not None and records and _SAFE_NAME.fullmatch(cid) else None
    for rec in records:
        frow = _field_row(cid, rec)
        values = None
        direction = rec.get("direction")
        if store is not None and not (group is not None and _SAFE_NAME.fullmatch(str(direction))):
            out.problems.append({"case_id": cid, "direction": direction,
                                 "error": "not a name the store can hold as a path"})
        elif group is not None:
            try:
                if rec.get("sha256") and _stored_sha(opts.out / STORE / cid / direction) == rec["sha256"]:
                    out.kept += 1
                    if frow["n_valid"] is None:
                        values = group[direction][...]
                else:
                    values, sha = _fetch_field(http, cid, rec)
                    _write_array(group, cid, rec, values, sha)
                    out.downloaded += 1
                out.stored.add(direction)
                frow["zarr"] = f"{cid}/{direction}"
            except _Gone:
                out.problems.append({"case_id": cid, "direction": direction,
                                     "error": "the broker no longer has this field (purged since it was listed?)"})
            except _Failed as exc:
                out.problems.append({"case_id": cid, "direction": direction, "error": str(exc)})
        if frow["n_valid"] is None and values is not None:
            # A field stored before the broker summarised fields has no statistics
            # until its background pass reaches it. They are the same function of
            # the same bytes (umag.stats, as db.field_summary calls it), so the
            # table gets them now rather than a hole.
            frow.update(umag.stats(values))
            frow.update(umag.ratios(frow))
        out.fields.append(frow)
    out.case["n_fields"] = len(out.fields)
    return out


# -- the files ------------------------------------------------------------------------

def _cases_schema():
    import pyarrow as pa
    cols = [("case_id", pa.string()), ("recipe", pa.string()), ("split", pa.string()), ("lcz", pa.string()),
            ("city_cluster", pa.string()), ("state", pa.string()), ("lat", pa.float64()), ("lon", pa.float64()),
            ("labels", pa.map_(pa.string(), pa.string())),
            ("country", pa.string()), ("country_code", pa.string()), ("town", pa.string()),
            ("town_km", pa.float64()), ("region", pa.string())]
    cols += [(m.key, pa.float64()) for m in dataset.METRICS]
    cols += [("height_source", pa.string()), ("build", pa.string()), ("worker", pa.string()),
             ("n_fields", pa.int32()), ("result_uri", pa.string()), ("result_sha256", pa.string()),
             ("result_bytes", pa.int64())]
    return pa.schema(cols)


def _fields_schema():
    import pyarrow as pa
    cols = [("case_id", pa.string()), ("direction", pa.string()), ("deg", pa.float64()),
            ("height_m", pa.float64()), ("u_ref", pa.float64()), ("lambda_f", pa.float64()),
            ("coverage", pa.float64()), ("n_valid", pa.int64())]
    cols += [(f"umag_{k}", pa.float64()) for k in _STATS]
    cols += [(f"vr_{k}", pa.float64()) for k in _STATS]
    cols += [("nx", pa.int32()), ("ny", pa.int32()), ("x0", pa.float64()), ("y0", pa.float64()),
             ("spacing_m", pa.float64()), ("sha256", pa.string()), ("bytes", pa.int64()),
             ("reported_at", pa.int64()), ("zarr", pa.string())]
    return pa.schema(cols)


def _replace(path: pathlib.Path, write: Callable[[pathlib.Path], None]) -> None:
    """Write beside, then rename over: a reader (or a crash) sees the old file or the
    new one, never half of either. os.replace is atomic within one filesystem."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_parquet(path: pathlib.Path, rows: list[dict[str, Any]], schema) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.Table.from_pylist(rows, schema=schema)
    _replace(path, lambda tmp: pq.write_table(table, tmp))


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree_digest(root: pathlib.Path, workers: int = 4) -> dict[str, Any]:
    """One sha256 for a directory: of the line ``<sha256>  <path>\\n`` for every file
    in it, by path (POSIX, relative to ``root``) -- ``sha256sum``'s own output
    format, so it can be checked without this code. A field store is one file per
    array plus its metadata, 320,000 files for the campaign: listing each in the
    card would make the card the largest file in the snapshot."""
    files = sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file())
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        shas = list(pool.map(lambda item: sha256_file(item[1]), files))
    h = hashlib.sha256()
    for (rel, _), sha in zip(files, shas, strict=True):
        h.update(f"{sha}  {rel}\n".encode("utf-8"))
    return {"sha256": h.hexdigest(), "files": len(files), "bytes": sum(p.stat().st_size for _, p in files),
            "digest": "sha256 of '<sha256>  <path>\\n' for every file in the store, sorted by path"}


def _prune(store, keep: dict[str, set[str]]) -> int:
    """Remove every array the export does not vouch for: cases the selection no longer
    holds, directions the broker no longer has at this height, and a field whose
    stored copy is stale and could not be fetched again. What is left is exactly
    what fields.parquet points at. Returns how many arrays went."""
    removed = 0
    for name in sorted(store.array_keys()):
        del store[name]
        removed += 1
    for cid in sorted(store.group_keys()):
        group = store[cid]
        names = sorted(group.array_keys())
        if cid not in keep:
            del store[cid]
            removed += len(names)
            continue
        for name in names:
            if name not in keep[cid]:
                del group[name]
                removed += 1
    return removed


def _counts(cases: list[dict[str, Any]], fields: list[dict[str, Any]]) -> dict[str, Any]:
    def tally(key: str) -> dict[str, int]:
        c = Counter((r.get(key) or "unknown") for r in cases)
        return {k: c[k] for k in sorted(c)}
    per_case = Counter(r["n_fields"] for r in cases)
    return {"cases": len(cases), "fields": len(fields),
            "fields_in_zarr": sum(1 for f in fields if f.get("zarr")),
            "by_split": tally("split"), "by_recipe": tally("recipe"), "by_lcz": tally("lcz"),
            # How many cases have how many directions: 32 is a whole CFD case, fewer
            # a case whose fields did not all reach the broker (GET /v1/custody).
            "fields_per_case": {str(k): per_case[k] for k in sorted(per_case)}}


# -- the run --------------------------------------------------------------------------

def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(opts: Options, http=None, say: Callable[[str], None] = _stderr) -> int:
    """Export the selection into ``opts.out``. 0 when everything selected is there, 1
    when a case or field could not be had (named on the card and here; a re-run
    retries just those). ExportError when nothing sensible can be written.

    ``http`` is anything with httpx.Client's ``get`` against the broker (the tests
    pass the app's TestClient); without one, a client is made from ``opts``."""
    _require(opts.fields)
    own = http is None
    if own:
        http = client(opts.broker, opts.token)
    try:
        return _run(opts, http, say)
    finally:
        if own:
            http.close()


def _run(opts: Options, http, say: Callable[[str], None]) -> int:
    health = _health(http)
    rows = select(http, opts)
    out = opts.out
    out.mkdir(parents=True, exist_ok=True)
    store = None
    if opts.fields:
        import zarr
        store = zarr.open_group(str(out / STORE), mode="a", zarr_format=3)
        store.update_attributes({"format": "casebroker-dataset fields", "schema_version": SCHEMA_VERSION,
                                 "height_m": opts.height_m, "units": "m/s"})
    say(f"[export] {len(rows)} case(s) from {opts.broker} (broker {health.get('version')})"
        + (", fields into " + str(out / STORE) if store is not None else ", tables only"))

    results: list[_CaseResult] = []
    lock = threading.Lock()
    last_said = [time.monotonic()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
        futures = [pool.submit(_export_case, http, row, opts, store) for row in rows]
        try:
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                with lock:
                    results.append(res)
                    if time.monotonic() - last_said[0] >= 5.0:
                        last_said[0] = time.monotonic()
                        say(f"[export] {len(results)}/{len(rows)} cases; fields: "
                            f"{sum(r.downloaded for r in results)} downloaded, "
                            f"{sum(r.kept for r in results)} already here")
        except BaseException:
            for f in futures:
                f.cancel()
            raise

    results.sort(key=lambda r: r.case_id)
    cases = [r.case for r in results if r.case is not None]
    fields = [f for r in results for f in r.fields]
    fields.sort(key=lambda f: (f["case_id"], f["deg"] is None, f["deg"] or 0.0, f["direction"] or ""))
    problems = [p for r in results for p in r.problems]
    gone = [r.case_id for r in results if r.gone]
    removed = _prune(store, {r.case_id: r.stored for r in results if r.case is not None}) if store is not None else 0

    _write_parquet(out / CASES, cases, _cases_schema())
    _write_parquet(out / FIELDS, fields, _fields_schema())
    files: dict[str, Any] = {name: {"sha256": sha256_file(out / name), "bytes": (out / name).stat().st_size}
                             for name in (CASES, FIELDS)}
    files[STORE] = tree_digest(out / STORE, opts.workers) if store is not None else None
    card = {
        "format": "casebroker-dataset", "schema_version": SCHEMA_VERSION,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exporter": USER_AGENT,
        "broker": {"url": opts.broker, "version": health.get("version"), "auth": health.get("auth")},
        "filters": {"recipe": list(opts.recipes), "split": list(opts.splits), "label": list(opts.labels),
                    "state": opts.state, "height_m": opts.height_m, "limit": opts.limit, "fields": opts.fields},
        "counts": _counts(cases, fields),
        "files": files,
        # What the selection held that this snapshot does not: re-running retries it.
        "problems": problems,
    }
    _replace(out / CARD, lambda tmp: tmp.write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8"))

    say(f"[export] wrote {out}: {len(cases)} case(s), {len(fields)} field(s)"
        + (f"; {sum(r.downloaded for r in results)} downloaded, {sum(r.kept for r in results)} already here, "
           f"{removed} removed" if store is not None else ""))
    if gone:
        say(f"[export] {len(gone)} case(s) left the broker while it ran: {', '.join(gone[:5])}"
            + (" ..." if len(gone) > 5 else ""))
    for p in problems[:20]:
        say(f"[export] MISSING {p['case_id']}" + (f" {p['direction']}" if p["direction"] else "")
            + f": {p['error']}")
    if len(problems) > 20:
        say(f"[export] ... and {len(problems) - 20} more on the card ({out / CARD})")
    return 1 if problems else 0
