"""One-line progress summary of a running SIMPLE solve, for the heartbeat.

Used by run_case.sh (host side, on a timer) to write the file the worker's
heartbeat thread ships to the broker as ``detail``. Kept as its own module so
tests/test_progress.py can pin the parsing against a log fixture with no
solver, container or scheduler involved.

Two things here are deliberate:

* **One residual per OUTER iteration, taken from the FIRST "Solving for
  <field>" after each "Time = " line.** ``p`` is solved several times per
  step (nCorrectors x nNonOrthogonalCorrectors, ~6 on this campaign), and the
  raw list interleaves corrector stages -- read naively it makes a monotone
  descent look like a two-decade oscillation. That misread cost a real
  detour earlier in this campaign.
* **Never raise.** A progress line is decoration on a heartbeat; a parse
  problem must never take down the worker that is reporting it. Anything
  unexpected degrades to whatever could be read, or to nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

_SOLVE = re.compile(r"Solving for (\w+),\s+Initial residual = ([0-9.eE+-]+)")
_CONVERGED = "SIMPLE solution converged"


def parse_log(text: str) -> dict[str, Any]:
    """Iteration count and the latest per-outer-iteration residuals.

    Returns ``{}`` for an empty or pre-solve log. ``converged`` is only ever
    True when the solver itself said so.
    """
    iterations = 0
    latest: dict[str, float] = {}
    cur: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("Time = "):
            if cur:
                latest = cur
            cur = {}
            iterations += 1
            continue
        m = _SOLVE.search(line)
        if m and m.group(1) not in cur:
            try:
                cur[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    if cur:
        latest = cur
    if not iterations:
        return {}
    out: dict[str, Any] = {"iteration": iterations, "converged": _CONVERGED in text}
    for f in ("p", "Ux"):
        if f in latest:
            out[f] = latest[f]
    return out


def last_line(path: str) -> str:
    """The final line of a file, read from the END rather than by scanning it.

    The trace grows for the whole solve -- hours, one record per outer
    iteration -- and this runs on a 60-second timer, so reading it whole would
    make the cost of reporting progress grow with the progress reported.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            window = min(size, 8192)
            f.seek(size - window)
            tail = f.read(window).decode("utf-8", "ignore")
    except OSError:
        return ""
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def parse_trace_line(line: str) -> dict[str, Any]:
    """One record of the solver's OWN trace, in the shape parse_log returns.

    E3D reports its residuals directly (see docs/e3d-contract.md) rather than
    leaving them to be recovered from its log with a regex. Same output shape,
    so every consumer below is indifferent to which source it came from -- and
    an older E3D that writes no trace still works through parse_log.
    """
    try:
        rec = json.loads(line)
    except (ValueError, TypeError):
        return {}
    if not isinstance(rec, dict) or "iteration" not in rec:
        return {}
    residuals = rec.get("residuals") or {}
    out: dict[str, Any] = {"iteration": rec["iteration"],
                           "converged": bool(rec.get("converged"))}
    for f in ("p", "Ux"):
        value = residuals.get(f)
        if isinstance(value, (int, float)):
            out[f] = float(value)
    return out


def summarize(log_path: str, direction: str | None = None,
              wall_seconds: float | None = None,
              trace_path: str | None = None) -> str:
    """The single line the heartbeat carries, e.g.
    ``case_270 iter 412 p=3.2e-05 Ux=8.1e-07 (2.3 h)``; empty string if there
    is nothing to say yet."""
    # The solver's own trace when there is one, the log otherwise. Preferring
    # it is not just tidier: parse_log has to infer the outer-iteration boundary
    # from the interleaved corrector stages, and that inference is exactly what
    # misread a monotone descent as an oscillation once already.
    info: dict[str, Any] = {}
    if trace_path:
        info = parse_trace_line(last_line(trace_path))
    if not info:
        try:
            with open(log_path, errors="ignore") as f:
                info = parse_log(f.read())
        except OSError:
            return ""
    if not info:
        return ""
    parts = []
    if direction:
        parts.append(direction)
    parts.append(f"iter {info['iteration']}")
    for f in ("p", "Ux"):
        if f in info:
            parts.append(f"{f}={info[f]:.1e}")
    if info.get("converged"):
        parts.append("CONVERGED")
    if wall_seconds is not None:
        parts.append(f"({wall_seconds / 3600:.1f} h)")
    return " ".join(parts)


def main(argv: list[str]) -> int:
    """``progress.py <12.log> [direction] [wall_seconds]`` -> one line on stdout.
    ``--json`` after the log path prints the parsed dict instead."""
    if not argv:
        print("usage: progress.py <log> [--json | direction [wall_seconds]]", file=sys.stderr)
        return 2
    log = argv[0]
    if len(argv) > 1 and argv[1] == "--json":
        try:
            with open(log, errors="ignore") as f:
                print(json.dumps(parse_log(f.read())))
        except OSError:
            print("{}")
        return 0
    direction = argv[1] if len(argv) > 1 else None
    trace = os.environ.get("E3D_TRACE_FILE") or None
    wall = float(argv[2]) if len(argv) > 2 else None
    print(summarize(log, direction, wall, trace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
