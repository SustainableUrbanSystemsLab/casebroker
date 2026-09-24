"""A case's stages, read off the progress trail it left.

A worker reports one line per heartbeat -- "site geometry", "build-case",
"mesh 3/5 · 03_snappyHexMesh", "solve 3/8 dirs · iter 412/2000", "archiving"
-- and the broker keeps every change of line as an event. Grouped by the stage
each line names, that trail says how long each stage took, which one a failure
happened in, and where a running case is now: what a case page should say
first, and what the raw list of lines said only to someone who read it all.

The grammar is the one Eddy3D's ``NodeProgress`` writes and the dashboard's
``progressPhase`` reads; the legacy shapes (``step 3/5: 03_snappyHexMesh``,
``case_270 [3/8 dirs] iter 412 ...``) are what runner/run_case.sh and nodes
from before the grammar still send. Anything else keeps the stage it arrived
in: a mesh warning is decoration on the mesh, not a stage of its own.
"""
from __future__ import annotations

import re
from typing import Any

#: In the order a case goes through them. "resume" is what a restarted node
#: says before it picks up where it stopped; "gate" is the convergence gate.
STAGES = ("geometry", "build-case", "resume", "mesh", "solve", "gate", "archive")

_SOLVER = re.compile(r"foamrun|simplefoam|urbanmicroclimatefoam|foammultirun|potentialfoam", re.I)
_LEGACY_STEP = re.compile(r"^step\s+\d+/\d+:\s*(?P<name>\S+)", re.I)
_LEGACY_DIRS = re.compile(r"\[\d+/\d+ dirs\]", re.I)


def stage_of(detail: str | None) -> str | None:
    """The stage a progress line belongs to, or None for a line that names
    none ("alive", a warning): the reader keeps the stage it was in."""
    if not detail:
        return None
    line = detail.strip().lower()
    if line.startswith("site geometry"):
        return "geometry"
    if line.startswith("build-case"):
        return "build-case"
    if line.startswith(("resuming", "rebuilding")):
        return "resume"
    if line.startswith("mesh"):
        return "mesh"
    if line.startswith("solve"):
        return "solve"
    if line.startswith("convergence gate"):
        return "gate"
    if line.startswith("archiving"):
        return "archive"
    m = _LEGACY_STEP.match(line)
    if m:
        name = m.group("name")
        if _SOLVER.search(name):
            return "solve"
        if "reconstruct" in name:
            return "archive"
        return "mesh"
    if _LEGACY_DIRS.search(line):
        return "solve"
    return None


def from_events(events: list[dict[str, Any]], now: int) -> dict[str, Any]:
    """Stage segments from a case's events, oldest first.

    Each segment: ``stage``, ``started_at``, ``ended_at`` (None while open),
    ``seconds``, the last ``detail`` seen in it, and the ``attempt`` it belongs
    to -- a lease or a resume starts one; done, failed, quarantined, released,
    cancelled and stale-released end it. A stale release (db._release_stale_leases) ends the
    segment at the last thing its worker said, not at the sweep 48 h later: the
    run stopped when the machine went silent. ``failed_in`` is the stage that was open when the
    most recent failure landed, ``current`` the open one of a running case.
    """
    segments: list[dict[str, Any]] = []
    open_seg: dict[str, Any] | None = None
    attempt = 0
    failed_in: str | None = None
    last_ts: int | None = None

    def close(ts: int) -> None:
        nonlocal open_seg
        if open_seg is not None:
            open_seg["ended_at"] = ts
            open_seg["seconds"] = max(0, ts - open_seg["started_at"])
            open_seg = None

    for e in events:
        kind, ts, detail = e["event"], e["ts"], e.get("detail")
        # Before any branch: the progress branch `continue`s on a repeated line.
        said_last, last_ts = last_ts, ts
        if kind in ("leased", "resumed"):
            close(ts)
            attempt += 1
        elif kind == "progress":
            stage = stage_of(detail)
            if stage is None:
                if open_seg is not None:
                    open_seg["detail"] = detail
                continue
            if open_seg is not None and open_seg["stage"] == stage:
                open_seg["detail"] = detail
                continue
            close(ts)
            open_seg = {"stage": stage, "started_at": ts, "ended_at": None, "seconds": None,
                        "detail": detail, "attempt": attempt}
            segments.append(open_seg)
        elif kind in ("failed", "quarantined"):
            failed_in = open_seg["stage"] if open_seg is not None else None
            close(ts)
        elif kind in ("done", "released", "cancelled"):
            close(ts)
        elif kind == "stale-released":
            close(said_last if said_last is not None else ts)
    current = open_seg["stage"] if open_seg is not None else None
    if open_seg is not None:
        open_seg["seconds"] = max(0, now - open_seg["started_at"])
    return {"stages": segments, "failed_in": failed_in, "current": current}
