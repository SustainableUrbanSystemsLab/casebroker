#!/usr/bin/env python3
"""Admit thermal cases beside finished wind cases (docs/thermal.md).

The thermal recipe's pilot takes about 50 sites whose wind case is DONE, spread
across LCZs, and posts a surface-temperature case for each:

* the same coordinates, city cluster and LCZ -- so the same split -- as the wind
  case, and a `wind_case` link to it (a link only: a thermal case never waits);
* the weather chosen HERE, at admission, and written into the spec: the nearest
  station whose dataset is exactly `TMYx` (the whole period of record) in
  Eddy3D's climate catalogue, as `E3D climate-index find` ranks them. A node
  downloads what the spec names, so every attempt of a case, on any machine,
  uses the same file;
* priority 200, so wind cases lease first, and the label `campaign`.

A dry run by default: it prints what it would post. `--post` posts, in batches
of at most 25 with a pause between them. A bulk write against production runs
under the lock every lease and heartbeat waits on, ~80 ms a statement, and one
large call took the broker down on 2026-09-26.

It refuses to post while the broker hands every recipe to workers that declare
none (the release policy's `undeclared_recipes`): such a worker gives a thermal
case back with exit 69 and stops, which ends a PACE allocation.

    python scripts/admit_thermal.py --broker https://casebroker.onrender.com \\
        --e3d C:/E3D/E3D.exe --count 50 [--post]

The token is CASEBROKER_TOKEN (write scope to post) or --token.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from typing import Any, Callable, Iterable

RECIPE = "surf-1008/rad6R0P2-fft-v1"
WIND_RECIPES = ("cyl-1008/of12-v5", "cyl-1008/of12-v4")
PRIORITY = 200
BATCH = 25
STATIONS = 300


def pick_sites(done: Iterable[dict[str, Any]], count: int, taken: set[str]) -> list[dict[str, Any]]:
    """Up to `count` finished wind cases, spread across LCZs and, within one,
    across cities: one per LCZ per round, LCZs in order, each LCZ's cases by
    case id, a city not repeated in an LCZ until every city there has had a
    turn. Deterministic, so a re-run proposes the same sites. `taken` holds the
    wind case ids that already have a thermal case."""
    by_lcz: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in sorted(done, key=lambda c: c["case_id"]):
        if c["case_id"] in taken:
            continue
        by_lcz[c.get("lcz") or "unknown"].append(c)
    queues = {}
    for lcz, cases in by_lcz.items():
        # Interleave cities: each city's first case, then each city's second, ...
        by_city: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in cases:
            by_city[c.get("city_cluster") or ""].append(c)
        order = []
        depth = 0
        while len(order) < len(cases):
            for city in sorted(by_city):
                if depth < len(by_city[city]):
                    order.append(by_city[city][depth])
            depth += 1
        queues[lcz] = order
    picked: list[dict[str, Any]] = []
    while len(picked) < count and any(queues.values()):
        for lcz in sorted(queues):
            if queues[lcz] and len(picked) < count:
                picked.append(queues[lcz].pop(0))
    return picked


def choose_station(stations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The nearest station whose dataset is exactly `TMYx`: the whole period of
    record, not one of its fixed windows (TMYx.2009-2023 and the like), so every
    site's weather is the same kind of year."""
    tmyx = [s for s in stations if s.get("dataset") == "TMYx" and s.get("url")]
    return min(tmyx, key=lambda s: s.get("distanceKm") if s.get("distanceKm") is not None else 1e9) if tmyx else None


def e3d_find(e3d: str) -> Callable[[float, float], list[dict[str, Any]]]:
    def find(lat: float, lon: float) -> list[dict[str, Any]]:
        # Wide: the catalogue ranks every dataset together, and around one pilot site the 25
        # nearest were all EnergyPlus TMY/TMY2/TMY3 and IWEC files, with its TMYx further out.
        out = subprocess.run([e3d, "climate-index", "find", "--lat", repr(lat), "--lon", repr(lon), "--limit", str(STATIONS)],
                             capture_output=True, text=True, timeout=300)
        if out.returncode not in (0, 1):
            raise RuntimeError(f"E3D climate-index find failed ({out.returncode}): {out.stderr.strip()[:300]}")
        return json.loads(out.stdout or "[]")
    return find


def e3d_catalogue(e3d: str) -> str | None:
    """When the catalogue the choices come from was built, for the spec."""
    out = subprocess.run([e3d, "climate-index", "show"], capture_output=True, text=True, timeout=300)
    try:
        return json.loads(out.stdout).get("builtAtUtc")
    except (ValueError, AttributeError):
        return None


def thermal_case(wind: dict[str, Any], station: dict[str, Any], catalogue: str | None, campaign: str) -> dict[str, Any]:
    spec = wind.get("spec") or {}
    if isinstance(spec, str):
        spec = json.loads(spec)
    return {
        "lat": spec["lat"], "lon": spec["lon"], "recipe": RECIPE,
        "city_cluster": wind["city_cluster"], "lcz": wind.get("lcz"),
        "priority": PRIORITY, "labels": {"campaign": campaign},
        "spec": {
            "lcz": wind.get("lcz"),
            "wind_case": wind["case_id"],
            "weather": {
                "key": station["key"], "url": station["url"], "name": station.get("name"),
                "dataset": station.get("dataset"), "period": station.get("period"),
                "distance_km": round(float(station["distanceKm"]), 2) if station.get("distanceKm") is not None else None,
                "catalogue": catalogue,
            },
        },
    }


# -- the broker ----------------------------------------------------------------------

class Broker:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"null")

    def cases(self, **params: str) -> list[dict[str, Any]]:
        out, offset = [], 0
        while True:
            q = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in {**params, "limit": 200, "offset": offset}.items())
            page = self.call("GET", f"/v1/cases?{q}")
            out.extend(page["cases"])
            offset += len(page["cases"])
            if not page["cases"] or offset >= page["total"]:
                return out
            time.sleep(1)                              # gently: this runs against production


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", required=True)
    ap.add_argument("--token", default=os.environ.get("CASEBROKER_TOKEN"))
    ap.add_argument("--e3d", required=True, help="the E3D executable whose climate catalogue picks the weather")
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--campaign", default="thermal-pilot", help="the `campaign` label")
    ap.add_argument("--any-state", action="store_true",
                    help="take wind sites in any state, not only finished ones (a thermal case never waits for its "
                         "wind case; this is for a campaign with too few finished sites to sample)")
    ap.add_argument("--post", action="store_true", help="post them (default: print what would be posted)")
    a = ap.parse_args(argv)
    if not a.token:
        ap.error("no token: pass --token or set CASEBROKER_TOKEN")
    broker = Broker(a.broker, a.token)

    if a.post:
        policy = broker.call("GET", "/v1/releases")
        if policy.get("undeclared_recipes") is None:
            print("refusing to post: the broker hands every recipe to workers that declare none. Set the release "
                  f"policy's undeclared_recipes (e.g. {list(WIND_RECIPES)}) first; see docs/releases.md.", file=sys.stderr)
            return 2

    wanted = {} if a.any_state else {"state": "done"}
    # By case id and by recipe here as well: a broker from before `recipe` was a filter ignores
    # it and answers with every case, twice.
    done = list({c["case_id"]: c for r in WIND_RECIPES for c in broker.cases(recipe=r, **wanted)
                 if c.get("recipe") == r and (a.any_state or c.get("state") == "done")}.values())
    already = [c for c in broker.cases(recipe=RECIPE) if c.get("recipe") == RECIPE]
    taken = set()
    for c in already:
        spec = c.get("spec") or {}
        if isinstance(spec, str):
            spec = json.loads(spec)
        if spec.get("wind_case"):
            taken.add(spec["wind_case"])
    picked = pick_sites(done, a.count, taken)
    print(f"{len(done)} {'wind' if a.any_state else 'finished wind'} cases; {len(taken)} already have a thermal case; "
          f"picked {len(picked)}")

    find, catalogue = e3d_find(a.e3d), e3d_catalogue(a.e3d)
    cases = []
    for wind in picked:
        spec = wind.get("spec") or {}
        if isinstance(spec, str):
            spec = json.loads(spec)
        station = choose_station(find(spec["lat"], spec["lon"]))
        if station is None:
            print(f"  {wind['case_id']}: no TMYx station among the nearest {STATIONS}; skipped")
            continue
        cases.append(thermal_case(wind, station, catalogue, a.campaign))
        print(f"  {wind['case_id']} {wind.get('lcz') or '?':>6} {wind['city_cluster']:<24} "
              f"{station['key']} ({station['distanceKm']:.1f} km)")

    if not a.post:
        print(f"dry run: {len(cases)} would be posted under {RECIPE} at priority {PRIORITY}; pass --post")
        return 0
    added = 0
    for i in range(0, len(cases), BATCH):
        out = broker.call("POST", "/v1/cases", cases[i:i + BATCH])
        added += out.get("added", 0)
        print(f"  batch {i // BATCH + 1}: {json.dumps(out)[:300]}")
        if i + BATCH < len(cases):
            time.sleep(10)
    print(f"posted {added} new thermal case(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
