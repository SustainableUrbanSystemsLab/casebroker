#!/usr/bin/env python3
"""Move every PENDING case of one recipe to another (POST /v1/cases/respec, in batches).

The respec endpoint selects by case id or by failure text, never by recipe, and
takes its ids in the query string: moving a queue means paging the pending cases
of the old recipe and handing them over a batch at a time. That is all this does.

* Only pending cases are selected. A leased case is left to finish under the
  recipe it was leased as (respec would skip it anyway), and a done one keeps its
  result: post the site under the new recipe for a second run.
* Each pass takes the FIRST page of what is still pending: a moved case leaves
  that set (it is quarantined, pointing at its new case), so offset 0 is always
  the next batch, and a case leased in between is simply not in it.
* It stops when nothing is pending, or when a pass moved nothing, so a broker
  that refuses every case cannot make it spin.

A dry run by default: it asks the broker to dry-run the first batch and prints
the answer. `--post` moves. Batches of 100 with a pause between them: a bulk
write runs under the lock every lease and heartbeat waits on.

    python scripts/move_recipe.py --broker https://<broker-host> \\
        --from cyl-1008/of12-v4 --to cyl-1008/of12-v6 --reason "..." [--post]

The token is CASEBROKER_TOKEN (write scope) or --token.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import USER_AGENT  # noqa: E402

BATCH = 100
PAUSE = 3.0


class Broker:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, params: dict[str, Any]) -> Any:
        url = self.url + path + "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"null")


def pending(broker: Broker, recipe: str, batch: int) -> list[str]:
    """The first page of `recipe`'s pending case ids. Filtered here as well: a broker
    from before `recipe` was a filter ignores it and answers with every case."""
    page = broker.call("GET", "/v1/cases", {"recipe": recipe, "state": "pending",
                                            "limit": batch, "include_spec": "false"})
    return [c["case_id"] for c in page["cases"]
            if c.get("recipe") == recipe and c.get("state") == "pending"]


def respec(broker: Broker, ids: list[str], to: str, reason: str | None, dry_run: bool) -> dict[str, Any]:
    params: dict[str, Any] = {"recipe": to, "case_id": ids, "dry_run": str(dry_run).lower(),
                              "limit": len(ids)}
    if reason:
        params["reason"] = reason
    return broker.call("POST", "/v1/cases/respec", params)


def move(broker: Broker, src: str, dst: str, *, reason: str | None = None, batch: int = BATCH,
         pause: float = PAUSE, sleep: Callable[[float], None] = time.sleep,
         say: Callable[[str], None] = print) -> dict[str, int]:
    """Move every pending `src` case to `dst`; returns the totals."""
    moved = skipped = passes = 0
    while True:
        ids = pending(broker, src, batch)
        if not ids:
            break
        out = respec(broker, ids, dst, reason, dry_run=False)
        passes += 1
        n, skips = out.get("moved", 0), out.get("skipped") or []
        moved += n
        skipped += len(skips)
        say(f"batch {passes}: asked {len(ids)}, moved {n}, skipped {len(skips)}"
            + (f" (first: {skips[0].get('case_id')}: {skips[0].get('why')})" if skips else ""))
        if n == 0:
            say("a pass moved nothing; stopping")
            break
        sleep(pause)
    return {"passes": passes, "moved": moved, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", required=True)
    ap.add_argument("--token", default=os.environ.get("CASEBROKER_TOKEN"))
    ap.add_argument("--from", dest="src", required=True, help="the recipe whose pending cases move")
    ap.add_argument("--to", dest="dst", required=True, help="the recipe they move to")
    ap.add_argument("--reason", help="recorded on both cases of every move")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--pause", type=float, default=PAUSE, help="seconds between batches")
    ap.add_argument("--post", action="store_true", help="move them (default: dry-run the first batch)")
    a = ap.parse_args(argv)
    if not a.token:
        ap.error("no token: pass --token or set CASEBROKER_TOKEN")
    if a.src == a.dst:
        ap.error("--from and --to name the same recipe")
    broker = Broker(a.broker, a.token)

    if not a.post:
        ids = pending(broker, a.src, a.batch)
        if not ids:
            print(f"nothing pending under {a.src}")
            return 0
        out = respec(broker, ids, a.dst, a.reason, dry_run=True)
        print(json.dumps({k: v for k, v in out.items() if not isinstance(v, list)}, indent=1))
        for k, v in out.items():
            if isinstance(v, list) and v:
                print(f"{k}: {len(v)}, e.g. {json.dumps(v[0])}")
        print("dry run of the first batch; --post moves them all")
        return 0

    totals = move(broker, a.src, a.dst, reason=a.reason, batch=a.batch, pause=a.pause,
                  say=lambda s: print(s, flush=True))
    print(f"done: {totals['passes']} batches, moved {totals['moved']}, skipped {totals['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
