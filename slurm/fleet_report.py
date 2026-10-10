#!/usr/bin/env python3
"""Tell the broker what SLURM holds for this account's E3D node jobs on this cluster.

A queued job has never called the broker -- it calls it when it starts -- so until then
the dashboard's Worker Fleet knew nothing of it: twenty jobs third in the queue looked
exactly like nothing being scheduled. This posts the scheduler's view (`POST /v1/fleet`:
counts, a one-line summary, and each job with its state, its reason and the scheduler's
own start estimate), and the Worker Fleet lists the jobs that have not started.

It needs nothing PACE does not have: python3's standard library, squeue, and the node's
own pairing (E3D setup-sim-node), whose credential it reads from the file -- the token is
never put on a command line, where `ps` would show it to every user on the machine.
submit_workers.sh runs it after submitting; each job runs it when it starts and every
FLEET_REPORT_SECONDS (300) while it runs.

    fleet_report.py --cluster ICE --name e3d-node-ice
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

#: What squeue calls a job that has not started; RUNNING is counted on its own, and the
#: rest (COMPLETING ...) are on their way out.
WAITING = ("PENDING", "CONFIGURING", "REQUEUED", "RESIZING", "SUSPENDED")


def credential_path() -> str:
    base = os.environ.get("EDDY3D_NODE_DIR") or os.path.join(os.path.expanduser("~"), ".local", "share", "Eddy3D", "node")
    return os.path.join(base, "credential.json")


def epoch(text: str) -> int | None:
    """squeue's local time ("2026-10-10T09:12:44"), or None for N/A, Unknown and the like."""
    try:
        return int(time.mktime(time.strptime(text.strip(), "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return None


def parse(out: str) -> list[dict]:
    """`squeue -h -o %i|%T|%r|%V|%S` lines -> [{id, state, reason, submitted_at, start_at}]."""
    jobs = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2 or not parts[0]:
            continue
        parts += [""] * (5 - len(parts))
        reason = parts[2].strip()
        jobs.append({
            "id": parts[0].strip()[:32],
            "state": parts[1].strip().upper()[:24] or "UNKNOWN",
            "reason": reason[:80] if reason and reason != "None" else None,
            "submitted_at": epoch(parts[3]),
            "start_at": epoch(parts[4]),
        })
    return jobs


def summary(name: str, jobs: list[dict]) -> str:
    """"e3d-node-ice: 18 pending (Priority 12, Dependency 6), 2 running"."""
    waiting = [j for j in jobs if j["state"] in WAITING]
    running = sum(1 for j in jobs if j["state"] == "RUNNING")
    why: dict[str, int] = {}
    for j in waiting:
        why[j["reason"] or "waiting"] = why.get(j["reason"] or "waiting", 0) + 1
    reasons = ", ".join("%s %d" % (r, n) for r, n in sorted(why.items(), key=lambda kv: -kv[1])[:3])
    text = "%s: %d pending%s, %d running" % (name, len(waiting), " (%s)" % reasons if reasons else "", running)
    return text[:200]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cluster", required=True, help="the cluster as the node names it (--cluster): ICE, Phoenix")
    ap.add_argument("--name", required=True, help="the job name to count (squeue -n), e.g. e3d-node-ice")
    ap.add_argument("--user", default=os.environ.get("USER", ""), help="squeue -u (default: $USER)")
    ap.add_argument("--credential", default=None, help="the node's credential.json (default: E3D's own)")
    ap.add_argument("--broker", default=None, help="the broker (default: the credential's)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent, send nothing")
    args = ap.parse_args(argv)

    try:
        out = subprocess.run(["squeue", "-h", "-u", args.user, "-n", args.name, "-o", "%i|%T|%r|%V|%S"],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print("fleet_report: squeue could not be run: %s" % e, file=sys.stderr)
        return 1
    if out.returncode != 0:
        print("fleet_report: squeue failed: %s" % out.stderr.strip()[:300], file=sys.stderr)
        return 1
    jobs = parse(out.stdout)[:500]
    body = {
        "cluster": args.cluster,
        "queued": sum(1 for j in jobs if j["state"] in WAITING),
        "running": sum(1 for j in jobs if j["state"] == "RUNNING"),
        "detail": summary(args.name, jobs),
        "jobs": jobs,
    }
    if args.dry_run:
        print(json.dumps(body, indent=2))
        return 0

    path = args.credential or credential_path()
    try:
        with open(path, encoding="utf-8") as f:
            cred = json.load(f)
        token, broker = cred["token"], args.broker or cred["broker"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        print("fleet_report: no usable node credential at %s (%s); pair this account first: "
              "E3D setup-sim-node <broker> --name <cluster> --no-browser" % (path, e), file=sys.stderr)
        return 1
    req = urllib.request.Request(broker.rstrip("/") + "/v1/fleet", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "Authorization": "Bearer " + token,
                                                         "User-Agent": "eddy3d-fleet-report"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except urllib.error.HTTPError as e:
        print("fleet_report: the broker refused the report: HTTP %d" % e.code, file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError) as e:
        print("fleet_report: the broker could not be reached: %s" % getattr(e, "reason", e), file=sys.stderr)
        return 2
    print("reported %s to the broker: %s" % (args.cluster, body["detail"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
