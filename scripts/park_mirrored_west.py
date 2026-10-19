#!/usr/bin/env python3
"""Quarantine every PENDING case west of Greenwich (POST /v1/cases/park, in batches).

Field case, 2026-10-19 (windcomfort-real-cities#1): `site_sampler.py`'s `scan()`
packed each cell as `(klat << 40) ^ (klon << 8) ^ lcz`, and a negative `klon`
sign-extended over the latitude bits -- every tile west of Greenwich decoded at
`-lat`. Before this fix, every western site the campaign ever admitted is one
of these; there is no legitimately-sampled western site to avoid catching.

Only PENDING cases are touched -- `park_cases` never quarantines a leased,
done, or already-quarantined one -- so this is safe to run more than once, and
it leaves the 2 done western cases as they are: quarantine is reversible,
relabelling a finished one is not.

Pages GET /v1/cases (state=pending, include_spec so lon is visible), parks
whatever has spec.lon < 0 in batches of 200, and stops when a page comes back
empty.

A dry run by default: it prints what it would park. `--post` parks them.

    python scripts/park_mirrored_west.py --broker https://casebroker.eddy3d.com \\
        [--post]

The token is CASEBROKER_TOKEN (write scope) or --token. `reopen
?error_contains=west+mirror` undoes it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

REASON = "sampler-v3 west mirror (windcomfort-real-cities#1): tile decoded at -lat"
BATCH = 200
PAUSE = 2.0


class Broker:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, params: dict[str, Any]) -> Any:
        url = self.url + path + "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "User-Agent": "casebroker-park-mirrored-west"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"null")


def pending_west(broker: Broker, batch: int) -> list[dict[str, Any]]:
    """Every PENDING case with spec.lon < 0, a page at a time."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = broker.call("GET", "/v1/cases", {"state": "pending", "include_spec": "true",
                                                 "limit": batch, "offset": offset})
        cases = page["cases"]
        for c in cases:
            if c.get("state") != "pending":
                continue
            spec = c.get("spec") or {}
            if isinstance(spec, str):
                spec = json.loads(spec)
            lon = spec.get("lon")
            if lon is not None and float(lon) < 0:
                out.append({"case_id": c["case_id"], "lat": spec.get("lat"), "lon": lon,
                           "city_cluster": c.get("city_cluster")})
        offset += len(cases)
        if not cases or offset >= page["total"]:
            return out
        time.sleep(1)                                  # gently: this runs against production


def park(broker: Broker, ids: list[str], dry_run: bool) -> dict[str, Any]:
    return broker.call("POST", "/v1/cases/park",
                       {"case_id": ids, "reason": REASON,
                        "dry_run": str(dry_run).lower(), "limit": len(ids)})


def park_all(broker: Broker, ids: list[str], *, batch: int = BATCH, pause: float = PAUSE,
            sleep=time.sleep, say=print) -> dict[str, int]:
    """Park every id, a batch at a time. Returns the total parked."""
    parked = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        out = park(broker, chunk, dry_run=False)
        parked += out["parked"]
        say(f"batch {i // batch + 1}: asked {len(chunk)}, parked {out['parked']}, skipped {out['skipped']}")
        if i + batch < len(ids):
            sleep(pause)
    return {"parked": parked}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", required=True)
    ap.add_argument("--token", default=os.environ.get("CASEBROKER_TOKEN"))
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--pause", type=float, default=PAUSE, help="seconds between batches")
    ap.add_argument("--post", action="store_true", help="park them (default: dry-run)")
    a = ap.parse_args(argv)
    if not a.token:
        ap.error("no token: pass --token or set CASEBROKER_TOKEN")
    broker = Broker(a.broker, a.token)

    found = pending_west(broker, a.batch)
    print(f"found {len(found)} pending case(s) west of Greenwich")
    for c in found[:10]:
        print(f"  {c['case_id']}: lat={c['lat']} lon={c['lon']} city_cluster={c['city_cluster']}")
    if len(found) > 10:
        print(f"  ... and {len(found) - 10} more")
    if not found:
        return 0

    if not a.post:
        print("dry run; --post parks them")
        return 0

    ids = [c["case_id"] for c in found]
    totals = park_all(broker, ids, batch=a.batch, pause=a.pause, say=print)
    print(f"done: parked {totals['parked']} of {len(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
