"""The campaign as a DATASET: what its cases are, per LCZ, and where one sits.

A campaign is run to produce a training set, and the questions asked of it are
about the set rather than the queue: how the sites spread across urban form,
whether one LCZ is all flat ground or all 2-million-cell meshes, which countries
the draw actually landed in, how long a case costs. ``GET /v1/dataset`` answers
them from every case at once, and ``GET /v1/cases/{id}`` places one case inside
the same distributions (``percentiles``).

Where the numbers come from, in one table (:data:`METRICS`): the node's
telemetry first (``cases.telemetry``, see :func:`casebroker.db.post_telemetry`),
which exists while a case is still running, and the completion metrics second,
which exist for cases that finished before telemetry did. Run times come from
the completion metrics only, and only for ``done`` cases -- a leased case has no
wall time yet, and a failed one's would describe the failure.

Computed in-process and cached for :data:`TTL_SECONDS`, from the columns it
needs read a page at a time (:func:`casebroker.db.dataset_rows`, each page one
short locked read), with everything else outside ``db._LOCK`` and each page
reduced to numbers and dropped before the next is read -- so neither the raw nor
the parsed JSON of a whole campaign is ever held at once. The sorted value
arrays are kept beside the answer, so a case's percentile is exact rather than
read off a histogram.
"""

from __future__ import annotations

import bisect
import json
import math
import sys
import threading
import time
from array import array
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import db, places

#: How long one aggregation is served before the next request recomputes it.
TTL_SECONDS = 60.0
#: Histogram resolution. 24 bins read well at dashboard width and divide the
#: p1..p99 range into round-ish steps.
N_BINS = 24
#: Countries listed by name; the rest are summed into "other". "unknown" (no
#: country polygon, or no coordinates) is listed apart and takes none of these.
TOP_COUNTRIES = 40

_STATS = ("min", "p10", "p25", "median", "p75", "p90", "max", "mean")


def parse_obj(value: Any) -> dict[str, Any]:
    """A JSON object column as a dict, or {} for NULL, garbage, or a non-object.
    Rows are TEXT on both engines; one bad row must not take the aggregate down."""
    if isinstance(value, dict):
        return value
    if not value or not isinstance(value, (str, bytes)):
        return {}
    try:
        out = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return {}
    return out if isinstance(out, dict) else {}


# Past this a number is not a measurement but a broken one, and the statistics
# below would overflow on it: the sum behind a mean, or the difference behind a
# bin width, of numbers near 1.8e308 is infinite (math.fsum raises), which took
# GET /v1/dataset down for as long as the row existed. Bounded here, a sum of
# 1e8 values and the difference of any two stay finite, and no real metric
# comes within 280 orders of magnitude of it.
MAX_MAGNITUDE = 1e300


def _num(value: Any) -> float | int | None:
    """A finite number within MAX_MAGNITUDE, or None. A bool is not a number
    here (``mesh_ok: true`` is not 1 cell), and neither is a numeric string:
    the node sends numbers.

    Converted with float() under a try, never passed to math.isfinite as it
    is: JSON has no integer limit, and math.isfinite(10**400) raises
    OverflowError rather than answering -- one such posted value made every
    aggregate fail. An int too large for a float is not finite here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(f) or abs(f) > MAX_MAGNITUDE:
        return None
    return value if isinstance(value, float) or abs(value) <= 2 ** 53 else f


# -- the metric registry --------------------------------------------------------

@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str | None
    group: str                      # "urban" | "site" | "mesh" | "run"
    extract: Callable[["_Case"], Any]


@dataclass
class _Case:
    """One row, parsed once and read by every extractor."""
    state: str | None
    lcz: str | None
    telemetry: dict[str, Any]
    metrics: dict[str, Any]

    def kind(self, name: str) -> dict[str, Any]:
        k = self.telemetry.get(name)
        return k if isinstance(k, dict) else {}


def _urban(key: str) -> Callable[[_Case], Any]:
    def get(c: _Case) -> Any:
        form = c.kind("site").get("urban_form")
        if isinstance(form, dict) and _num(form.get(key)) is not None:
            return form[key]
        form = c.metrics.get("urban_form")
        return form.get(key) if isinstance(form, dict) else None
    return get


def _site(key: str) -> Callable[[_Case], Any]:
    return lambda c: c.kind("site").get(key)


def _worst_mesh(key: str) -> Callable[[_Case], Any]:
    """The worst of a case's meshes: a site with one bad mesh is a bad site."""
    def get(c: _Case) -> Any:
        meshes = c.kind("mesh").get("meshes")
        if not isinstance(meshes, dict):
            return None
        vals = [v for m in meshes.values() if isinstance(m, dict)
                and (v := _num(m.get(key))) is not None]
        return max(vals) if vals else None
    return get


def _total_cells(c: _Case) -> Any:
    v = c.kind("mesh").get("total_cells")
    return v if _num(v) is not None else c.metrics.get("mesh_cells")


def _run(key: str) -> Callable[[_Case], Any]:
    return lambda c: c.metrics.get(key) if c.state == "done" else None


#: The one table. Order is the order the dashboard lists them in.
METRICS: tuple[Metric, ...] = (
    Metric("bcr", "Built coverage ratio", None, "urban", _urban("bcr")),
    Metric("bht_m", "Mean building height (area-weighted)", "m", "urban", _urban("bht_m")),
    Metric("bdr_m", "Built density (volume / plot area)", "m", "urban", _urban("bdr_m")),
    Metric("vr_ring", "Vertical area ratio (full perimeter)", None, "urban", _urban("vr_ring")),
    Metric("vr_exposed", "Vertical area ratio (exposed walls)", None, "urban", _urban("vr_exposed")),
    Metric("ar", "Aspect ratio (height / open-space width)", None, "urban", _urban("ar")),
    Metric("open_space_width_m", "Mean open-space width", "m", "urban", _urban("open_space_width_m")),
    Metric("bht_sigma_m", "Building height uncertainty (1 sigma)", "m", "urban", _urban("bht_sigma_m")),
    Metric("rar", "Road area ratio", None, "urban", _urban("rar")),
    Metric("svf", "Sky view factor (cosine-weighted)", None, "urban", _urban("svf")),
    Metric("svf_dome", "Sky view factor (unweighted dome)", None, "urban", _urban("svf_dome")),
    Metric("n_buildings", "Buildings", None, "site", _site("n_buildings")),
    Metric("terrain_relief_m", "Terrain relief", "m", "site", _site("terrain_relief_m")),
    Metric("canopy_fraction", "Tree canopy fraction", None, "site", _site("canopy_fraction")),
    Metric("total_cells", "Mesh cells", "cells", "mesh", _total_cells),
    Metric("max_skewness", "Max skewness (worst mesh)", None, "mesh", _worst_mesh("max_skewness")),
    Metric("max_non_orthogonality", "Max non-orthogonality (worst mesh)", "deg", "mesh",
           _worst_mesh("max_non_orthogonality")),
    Metric("case_seconds", "Case wall time", "s", "run", _run("case_seconds")),
    Metric("mesh_seconds", "Mesh wall time", "s", "run", _run("mesh_seconds")),
    Metric("solve_seconds", "Solve wall time", "s", "run", _run("solve_seconds")),
)


def values_of(case: _Case) -> dict[str, float | int]:
    """Every registry metric this case has a finite value for."""
    out = {}
    for m in METRICS:
        v = _num(m.extract(case))
        if v is not None:
            out[m.key] = v
    return out


def _case(row: dict[str, Any]) -> _Case:
    return _Case(state=row.get("state"), lcz=row.get("lcz") or None,
                 telemetry=parse_obj(row.get("telemetry")),
                 metrics=parse_obj(row.get("metrics")))


# -- statistics ------------------------------------------------------------------

def quantile(sorted_values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks -- numpy's default, so a
    number here can be checked against ``numpy.quantile`` by anyone."""
    n = len(sorted_values)
    if n == 0:
        return None
    h = (n - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, n - 1)
    return sorted_values[lo] + (h - lo) * (sorted_values[hi] - sorted_values[lo])


def bin_edges(sorted_values: Sequence[float]) -> list[float]:
    """N_BINS + 1 edges over the global p1..p99, so one extreme site cannot
    squash every other case into a single bar. Values outside land in the end
    bins (see :func:`histogram`). A range that collapses to a point -- one
    value, or every value equal -- is widened around it so the edges still
    ascend and that value sits in a middle bin."""
    if not sorted_values:
        return []
    lo, hi = quantile(sorted_values, 0.01), quantile(sorted_values, 0.99)
    if not hi > lo:
        pad = abs(lo) * 0.05 or 0.5
        lo, hi = lo - pad, hi + pad
    step = (hi - lo) / N_BINS
    edges = [lo + step * i for i in range(N_BINS)]
    edges.append(hi)
    return edges


def histogram(values: Iterable[float], edges: list[float]) -> list[int]:
    if not edges:
        return []
    counts = [0] * N_BINS
    for v in values:
        i = bisect.bisect_right(edges, v) - 1
        counts[min(max(i, 0), N_BINS - 1)] += 1
    return counts


def summary(sorted_values: Sequence[float], edges: list[float]) -> dict[str, Any]:
    """n, min, p10..p90, max, mean, and the histogram over the SHARED edges."""
    n = len(sorted_values)
    if n == 0:
        return {"n": 0, **{k: None for k in _STATS}, "hist": []}
    return {"n": n, "min": sorted_values[0],
            "p10": quantile(sorted_values, 0.10), "p25": quantile(sorted_values, 0.25),
            "median": quantile(sorted_values, 0.50), "p75": quantile(sorted_values, 0.75),
            "p90": quantile(sorted_values, 0.90), "max": sorted_values[-1],
            "mean": math.fsum(sorted_values) / n,
            "hist": histogram(sorted_values, edges)}


def percentile_rank(sorted_values: Sequence[float], value: float) -> float | None:
    """Where `value` sits among `sorted_values`, 0..100: the share strictly
    below it plus half the share equal to it (the textbook percentile rank).
    The median of an odd set is 50, the largest of 100 is 99.5, and a value
    tied with everything is 50 -- not 100 because it happens to be the max."""
    n = len(sorted_values)
    if n == 0:
        return None
    below = bisect.bisect_left(sorted_values, value)
    equal = bisect.bisect_right(sorted_values, value) - below
    return round(100.0 * (below + 0.5 * equal) / n, 2)


# -- country -----------------------------------------------------------------------

# places.locate is lru_cached at 20,000 entries. A campaign larger than that,
# swept in the same order every minute, is the pattern an LRU cache is worst at:
# every lookup evicts the entry the next sweep needs first, and the hit rate is
# zero. A site's coordinates never change, so the country is remembered here for
# the life of the process -- a few MB at campaign scale, bounded below anyway.
_COUNTRY: dict[tuple[float, float], str | None] = {}
_COUNTRY_MAX = 250_000


def _country(spec: dict[str, Any]) -> str | None:
    try:
        lat, lon = float(spec["lat"]), float(spec["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    key = (lat, lon)
    if key not in _COUNTRY:
        if len(_COUNTRY) >= _COUNTRY_MAX:
            _COUNTRY.clear()
        try:
            _COUNTRY[key] = places.locate(lat, lon).get("country")
        except Exception:                            # noqa: BLE001 -- a place is never fatal
            _COUNTRY[key] = None
    return _COUNTRY[key]


def _ranked(counter: Counter) -> list[tuple[str, int]]:
    """Largest first, ties by name: an order that depends on the counts alone.
    Counter.most_common() breaks ties by the order keys were first SEEN, and
    rows arrive in whatever order the table yields them."""
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def _counts(counter: Counter) -> dict[str, int]:
    return dict(_ranked(counter))


def _top_countries(counter: Counter) -> dict[str, int]:
    """The TOP_COUNTRIES largest countries by name, then ``unknown`` (a site no
    country polygon claims, or one without coordinates) and ``other`` (every
    country past the cut), each only when non-zero.

    Ranked BEFORE the cut, so which of two equal countries is named does not
    flip between refreshes as rows change order in the table. ``unknown`` is
    not a country, so it takes none of the named slots."""
    unknown = counter.get(None, 0)
    ranked = _ranked(Counter({k: n for k, n in counter.items() if k is not None}))
    out = dict(ranked[:TOP_COUNTRIES])
    if unknown:
        out["unknown"] = unknown
    rest = sum(n for _, n in ranked[TOP_COUNTRIES:])
    if rest:
        out["other"] = rest
    return out


def _unknown(value: Any) -> str:
    return str(value) if value not in (None, "") else "unknown"


# -- the aggregate -----------------------------------------------------------------

@dataclass
class Aggregate:
    """One computation: the public answer, and what exact percentiles need."""
    public: dict[str, Any]
    sorted_all: dict[str, array] = field(default_factory=dict)
    sorted_lcz: dict[str, dict[str, array]] = field(default_factory=dict)

    def percentiles(self, row: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """For every metric this case has a value for: the value, its rank among
        all cases, and among cases of its own LCZ (None without an LCZ, or when
        the aggregate holds none of that LCZ yet). `row` is the case's LIVE row,
        so a value posted since the aggregate was computed is still ranked --
        against the population as of then."""
        case = _case(row)
        out = {}
        for key, v in values_of(case).items():
            same = self.sorted_lcz.get(key, {}).get(case.lcz) if case.lcz else None
            out[key] = {"value": v,
                        "all": percentile_rank(self.sorted_all.get(key, []), v),
                        "lcz": percentile_rank(same, v) if same else None}
        return out


def stream_rows(conn) -> Iterator[dict[str, Any]]:
    """Every case, one :func:`casebroker.db.dataset_rows` page at a time.

    Each page is its own short locked read, and each row is let go as
    :func:`compute` takes it, so what is in memory at once is one page -- not
    the campaign: reading the whole table in one go measured +300 MB on
    Postgres at 30,000 cases, on a 512 MB instance. Lazy, so nothing is read
    until compute() asks, and with db._LOCK free between pages."""
    after = None
    while True:
        limit = db.DATASET_PAGE_ROWS
        page = db.dataset_rows(conn, after=after, limit=limit)
        if not page:
            return
        last_page = len(page) < limit
        after = page[-1]["case_id"]
        page.reverse()
        while page:
            yield page.pop()
        if last_page:
            return


def compute(rows: Iterable[dict[str, Any]], now: float | None = None) -> Aggregate:
    """The whole dataset answer from the rows :func:`stream_rows` yields (or
    any iterable of rows shaped like :func:`casebroker.db.dataset_rows`').

    One pass, keeping only counters and the numbers: a row's JSON is parsed,
    reduced and dropped before the next is read.
    """
    total = 0
    counts = {name: Counter() for name in ("state", "split", "lcz", "recipe", "country")}
    # Flat arrays of doubles, not lists of Python numbers: 8 bytes a value
    # where a list costs a pointer plus a ~24-byte object, and a campaign holds
    # ~20 per case twice over (all, and its LCZ) for as long as it is cached.
    per_all: dict[str, array] = {m.key: array("d") for m in METRICS}
    per_lcz: dict[str, dict[str, array]] = {m.key: {} for m in METRICS}
    for row in rows:
        total += 1
        counts["state"][_unknown(row.get("state"))] += 1
        counts["split"][_unknown(row.get("split"))] += 1
        counts["lcz"][_unknown(row.get("lcz"))] += 1
        counts["recipe"][_unknown(row.get("recipe"))] += 1
        counts["country"][_country(parse_obj(row.get("spec"))) or None] += 1
        case = _case(row)
        for key, v in values_of(case).items():
            per_all[key].append(v)
            if case.lcz:
                per_lcz[key].setdefault(case.lcz, array("d")).append(v)

    agg = Aggregate(public={})
    metrics_out: dict[str, Any] = {}
    for m in METRICS:
        values = array("d", sorted(per_all[m.key]))
        edges = bin_edges(values)
        by_lcz = {}
        agg.sorted_lcz[m.key] = {}
        for lcz in sorted(per_lcz[m.key]):
            group = array("d", sorted(per_lcz[m.key][lcz]))
            agg.sorted_lcz[m.key][lcz] = group
            by_lcz[lcz] = summary(group, edges)
        agg.sorted_all[m.key] = values
        metrics_out[m.key] = {"label": m.label, "unit": m.unit, "group": m.group,
                              "bins": edges, "all": summary(values, edges),
                              "by_lcz": by_lcz}
    agg.public = {
        "generated_at": int(now if now is not None else time.time()),
        "cases": total,
        "counts": {"state": _counts(counts["state"]), "split": _counts(counts["split"]),
                   "lcz": _counts(counts["lcz"]), "recipe": _counts(counts["recipe"]),
                   "country": _top_countries(counts["country"])},
        "metrics": metrics_out,
    }
    return agg


class Unavailable(RuntimeError):
    """There is no aggregate to serve: the last computation failed, and none
    succeeded before it."""


def _in_background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="dataset-refresh", daemon=True).start()


class DatasetCache:
    """The aggregate, recomputed at most once per `ttl` seconds.

    One computation at a time (`_computing`), and nobody waits for one who has
    something to be served instead:

    * :meth:`get` (``GET /v1/dataset``) serves a fresh aggregate; a stale one
      it recomputes -- unless another request already is, in which case it
      serves the stale one rather than queueing behind it. Only with nothing at
      all to serve does it wait, and then shares the one computation.
    * :meth:`peek` (a case's ``percentiles``) never waits: it serves whatever
      there is, even stale, and starts the refresh in the background. A waiting
      request holds one of the REQUEST_CONCURRENCY threadpool slots that
      leases and heartbeats run on too, and a cold aggregate at campaign scale
      is seconds of work.

    A FAILED computation is remembered for the TTL just like a successful one,
    with the last good aggregate kept and served: a row that breaks the
    statistics then costs one pass over the campaign per TTL, not one per
    request (it cost one per request, every case GET included, before).

    `_computing` is taken BEFORE ``db._LOCK`` (inside `load`) and never while
    holding it, so the two cannot deadlock. `spawn` runs a background refresh;
    a test can make it synchronous.
    """

    def __init__(self, load: Callable[[], Iterable[dict[str, Any]]],
                 ttl: float = TTL_SECONDS, clock: Callable[[], float] = time.monotonic,
                 spawn: Callable[[Callable[[], None]], None] | None = None):
        self._load = load
        self.ttl = ttl
        self.clock = clock
        # Looked up now, not bound as a default argument, so the test suite can
        # swap the module's _in_background for an inline call in one place.
        self.spawn = spawn if spawn is not None else _in_background
        self._computing = threading.Lock()
        # (aggregate, when it was last attempted, what that attempt raised),
        # replaced as ONE tuple so a reader without the lock never sees half.
        self._state: tuple[Aggregate | None, float | None, BaseException | None] = (
            None, None, None)

    def _fresh(self, state) -> bool:
        at = state[1]
        return at is not None and self.clock() - at < self.ttl

    def _refresh(self) -> None:
        """One computation. The caller holds `_computing`."""
        started = self.clock()
        try:
            value = compute(self._load())
        except Exception as exc:                     # noqa: BLE001 -- remembered, served as 503
            print(f"[dataset] aggregate failed, keeping the last one for "
                  f"{self.ttl:.0f} s: {exc!r}", file=sys.stderr)
            self._state = (self._state[0], started, exc)
        else:
            self._state = (value, started, None)

    def get(self) -> Aggregate:
        state = self._state
        if not self._fresh(state):
            # Something to serve: never queue behind a refresh already running.
            if self._computing.acquire(blocking=state[0] is None):
                try:
                    if not self._fresh(self._state):   # done while this one waited?
                        self._refresh()
                finally:
                    self._computing.release()
            state = self._state
        value, _, error = state
        if value is None:
            raise Unavailable(f"dataset statistics could not be computed: {error!r}")
        return value

    def peek(self) -> Aggregate | None:
        """The aggregate if there is one, fresh or not, without ever waiting;
        a stale or missing one is refreshed in the background."""
        if not self._fresh(self._state) and self._computing.acquire(blocking=False):
            ran = []

            def run() -> None:
                ran.append(True)
                try:
                    if not self._fresh(self._state):
                        self._refresh()
                finally:
                    self._computing.release()
            try:
                self.spawn(run)
            except Exception as exc:                 # noqa: BLE001 -- no thread, no refresh
                if not ran:
                    self._computing.release()
                print(f"[dataset] could not start a refresh: {exc!r}", file=sys.stderr)
        return self._state[0]

    def invalidate(self) -> None:
        with self._computing:
            self._state = (None, None, None)
