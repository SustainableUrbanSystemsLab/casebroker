#!/usr/bin/env python3
"""Admit MRT cases beside finished surface-temperature cases (docs/mrt.md).

An MRT case is built ON the site's surface-temperature case: it reads that
case's archive (`t_surface.npy`, `sensors.npy`) for the long-wave half of the
answer. So one is posted for each DONE `surf-1008/rad6R0P2-fft-v2` case whose
site has none yet, and:

* `needs` names the surface case, so the broker hands the MRT case to no node
  until that case is done -- which it already is, unless it is reopened or
  moved later, and then the gate holds;
* the spec carries the surface case's own `weather` (the same file: the node
  refuses a surface archive computed with another), its `lcz`, `surf_case`,
  and the `wind_case` link it carried;
* the same coordinates and city cluster, so the same split; priority 300, so
  wind (50) and surface-temperature (200) cases lease first; the label
  `campaign`.

A dry run by default: it prints what it would post. `--post` posts, in batches
of at most 25 with a pause between them (a bulk write against production runs
under the lock every lease and heartbeat waits on).

It refuses to post while the broker hands every recipe to workers that declare
none (the release policy's `undeclared_recipes`): a script worker handed a
Radiance case gives it back with exit 69 and stops.

    python scripts/admit_mrt.py --broker https://casebroker.eddy3d.com [--post]

The token is CASEBROKER_TOKEN (write scope to post) or --token. Sites with a
wind case and no surface case first need one: scripts/admit_thermal.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Any, Iterable

RECIPE = "mrt-1008/rad6R0P2-solarcal-v1"
# The surface-temperature recipe this version of the MRT recipe is defined against
# (docs/mrt.md, "Versions"). Exact: a surface case of another version is another
# training set, and an MRT case built on it would be too.
SURF_RECIPE = "surf-1008/rad6R0P2-fft-v2"
PRIORITY = 300
BATCH = 25


def spec_of(case: dict[str, Any]) -> dict[str, Any]:
    spec = case.get("spec") or {}
    return json.loads(spec) if isinstance(spec, str) else spec


def done_surface_cases(cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The finished surface cases of SURF_RECIPE, by case id so a re-run proposes
    the same order. A broker from before `recipe` was a filter answers with every
    case, so the recipe is checked here as well."""
    return sorted((c for c in cases if c.get("recipe") == SURF_RECIPE and c.get("state") == "done"),
                  key=lambda c: c["case_id"])


def taken_surface_cases(mrt_cases: Iterable[dict[str, Any]]) -> set[str]:
    """The surface cases that already have an MRT case, whatever its state."""
    out = set()
    for c in mrt_cases:
        if c.get("recipe") != RECIPE:
            continue
        surf = spec_of(c).get("surf_case")
        if surf:
            out.add(surf)
    return out


def mrt_case(surf: dict[str, Any], campaign: str, priority: int = PRIORITY) -> dict[str, Any]:
    """The POST body for one MRT case, from its surface case."""
    spec = spec_of(surf)
    if spec.get("lat") is None or spec.get("lon") is None:
        raise ValueError(f"{surf['case_id']}: its spec has no lat/lon")
    if not isinstance(spec.get("weather"), dict) or not spec["weather"].get("url"):
        raise ValueError(f"{surf['case_id']}: its spec names no weather; an MRT case needs the same file")
    body_spec: dict[str, Any] = {
        "lcz": surf.get("lcz"),
        "surf_case": surf["case_id"],
        "weather": spec["weather"],
    }
    if spec.get("wind_case"):
        body_spec["wind_case"] = spec["wind_case"]
    return {
        "lat": spec["lat"], "lon": spec["lon"], "recipe": RECIPE,
        "city_cluster": surf["city_cluster"], "lcz": surf.get("lcz"),
        "priority": priority, "labels": {"campaign": campaign},
        "needs": surf["case_id"],
        "spec": body_spec,
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
                                              "Content-Type": "application/json",
                                              "User-Agent": "casebroker-admit-mrt"})
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
    ap.add_argument("--count", type=int, default=None, help="at most this many (default: every finished surface case)")
    ap.add_argument("--campaign", default="mrt-v1", help="the `campaign` label")
    ap.add_argument("--priority", type=int, default=PRIORITY,
                    help=f"lease order, lower first (default {PRIORITY}: after wind at 50 and surface temperatures at 200)")
    ap.add_argument("--post", action="store_true", help="post them (default: print what would be posted)")
    a = ap.parse_args(argv)
    if not a.token:
        ap.error("no token: pass --token or set CASEBROKER_TOKEN")
    broker = Broker(a.broker, a.token)

    if a.post:
        policy = broker.call("GET", "/v1/releases")
        if policy.get("undeclared_recipes") is None:
            print("refusing to post: the broker hands every recipe to workers that declare none. Set the release "
                  "policy's undeclared_recipes to the wind recipes first; see docs/releases.md.", file=sys.stderr)
            return 2

    surfaces = done_surface_cases(broker.cases(recipe=SURF_RECIPE, state="done"))
    taken = taken_surface_cases(broker.cases(recipe=RECIPE))
    todo = [s for s in surfaces if s["case_id"] not in taken]
    if a.count is not None:
        todo = todo[:a.count]
    print(f"{len(surfaces)} finished surface cases; {len(taken)} already have an MRT case; picked {len(todo)}")

    cases = []
    for surf in todo:
        try:
            cases.append(mrt_case(surf, a.campaign, a.priority))
        except ValueError as e:
            print(f"  skipped: {e}")
            continue
        w = spec_of(surf).get("weather") or {}
        print(f"  {surf['case_id']} {surf.get('lcz') or '?':>6} {surf['city_cluster']:<24} {w.get('key', '?')}")

    if not a.post:
        print(f"dry run: {len(cases)} would be posted under {RECIPE} at priority {a.priority}; pass --post")
        return 0
    added = 0
    for i in range(0, len(cases), BATCH):
        out = broker.call("POST", "/v1/cases", cases[i:i + BATCH])
        added += out.get("added", 0)
        print(f"  batch {i // BATCH + 1}: {json.dumps(out)[:300]}")
        if i + BATCH < len(cases):
            time.sleep(10)
    print(f"posted {added} new MRT case(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
