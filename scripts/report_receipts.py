#!/usr/bin/env python3
"""Tell the broker which finished cases' archives have arrived in a folder (DOMAIN.md, "Custody").

Run on the machine holding a copy of the archives: a node's done folder, or one pulled from a
cluster (copied there by hand; scripts/pull_done.sh went with the Python worker). The broker's own part store writes its receipts itself. For every case the broker lists
as done but without an archive receipt (GET /v1/custody), it looks in the folder: a case whose
archive and every part its manifest names are here, and whose parts hash to what the manifest
says (casebroker.archives.status, verify), is hashed and reported (POST /v1/cases/{id}/receipts).
The broker checks that hash against the one the node reported at completion and refuses a
different one -- a corrupted transfer stays on the custody list.

Only what the broker is missing is hashed: a campaign archive is gigabytes, and a scan of the
whole folder every run would re-read terabytes for nothing. A case still syncing (parts missing,
or no case archive yet) is skipped and named.

A dry run by default; --post reports. The token is CASEBROKER_TOKEN (write scope) or --token.

    python scripts/report_receipts.py --broker https://casebroker.eddy3d.com --done D:/campaign/done [--post]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import USER_AGENT, archives  # noqa: E402


class Broker:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"null")

    def missing_archives(self) -> list[str]:
        view = self.call("GET", "/v1/custody?" + urllib.parse.urlencode({"limit": 5000}))
        return [c["case_id"] for c in view.get("cases", []) if "archive" in c.get("missing", [])]

    def receipt(self, case_id: str, sha256: str, size: int, path: str) -> Any:
        return self.call("POST", f"/v1/cases/{urllib.parse.quote(case_id)}/receipts",
                         {"kind": "archive", "location": "master", "sha256": sha256, "bytes": size, "path": path})


def report(broker: Broker, done: pathlib.Path, post: bool, pause: float = 0.5) -> dict[str, list[str]]:
    """Reports what is here and complete; returns what happened to each case, by outcome."""
    out: dict[str, list[str]] = {"reported": [], "not_here": [], "syncing": [], "corrupt": [], "refused": []}
    for cid in broker.missing_archives():
        st = archives.status(done, cid, verify=True)
        if st.state in ("missing",):
            out["not_here"].append(cid)
            continue
        if st.state in ("partial", "waiting"):
            out["syncing"].append(cid)
            continue
        if st.state == "corrupt":
            out["corrupt"].append(cid)
            continue
        sha = archives._sha256(st.archive)
        size = st.archive.stat().st_size
        if not post:
            out["reported"].append(cid)
            continue
        try:
            broker.receipt(cid, sha, size, st.archive.name)
            out["reported"].append(cid)
        except urllib.error.HTTPError as e:
            out["refused"].append(f"{cid}: HTTP {e.code} {e.read().decode()[:200]}")
        time.sleep(pause)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", required=True)
    ap.add_argument("--token", default=os.environ.get("CASEBROKER_TOKEN"))
    ap.add_argument("--done", required=True, help="the folder holding the archives")
    ap.add_argument("--post", action="store_true", help="report them (default: say what would be reported)")
    a = ap.parse_args(argv)
    if not a.token:
        ap.error("no token: pass --token or set CASEBROKER_TOKEN")
    done = pathlib.Path(a.done)
    if not done.is_dir():
        ap.error(f"--done {done} is not a folder")
    out = report(Broker(a.broker, a.token), done, a.post)
    verb = "reported" if a.post else "would report"
    print(f"{verb} {len(out['reported'])}; still syncing {len(out['syncing'])}; not on this master "
          f"{len(out['not_here'])}; corrupt {len(out['corrupt'])}; refused {len(out['refused'])}")
    for k in ("syncing", "corrupt", "refused"):
        for line in out[k]:
            print(f"  {k}: {line}")
    return 1 if out["refused"] or out["corrupt"] else 0


if __name__ == "__main__":
    sys.exit(main())
