"""Contain the native building reader's crashes and bound its total lifetime.

Only the child imports DuckDB. The broker still owns the preview semaphore,
cache and job result, so a signal or timeout becomes a transient layer failure
instead of taking heartbeats down or losing every queued preview on restart.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys


def _run(command: list[str], timeout: float) -> dict:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills and waits for the child before raising: the next
        # preview cannot overlap a timed-out native query's memory allocation.
        raise RuntimeError(f"building reader exceeded {timeout:g}s; retry the preview") from exc
    if result.returncode:
        reason = f"exit {result.returncode}"
        if result.returncode < 0:
            try:
                reason = signal.Signals(-result.returncode).name
            except ValueError:
                reason = f"signal {-result.returncode}"
        print(f"[geometry] building reader failed ({reason}): {result.stderr[-4000:]}",
              file=sys.stderr)
        raise RuntimeError(f"building reader failed ({reason}); retry the preview")
    try:
        message = json.loads(result.stdout)
        if not isinstance(message, dict):
            raise ValueError("expected an object")
    except (ValueError, TypeError) as exc:
        raise RuntimeError("building reader returned an invalid response") from exc
    if message.get("error") == "tile_not_published":
        from .footprints import TileNotPublished
        raise TileNotPublished(message["detail"])
    if "error" in message:
        raise RuntimeError(message.get("detail") or "building read failed")
    return message


def read_buildings(lat: float, lon: float, half_m: float, timeout: int) -> dict:
    # Four attempts is DuckDB's per-request retry budget. Add startup margin,
    # but cap the WHOLE read too: many slow requests must not hold the slot for
    # longer than the dashboard's five-minute polling window.
    deadline = min(150.0, max(1.0, timeout * 4 + 15.0))
    return _run([sys.executable, "-X", "faulthandler", "-m", "casebroker.geo_process",
                 str(lat), str(lon), str(half_m), str(timeout)], deadline)


def main() -> None:
    # A native fault needs its faulthandler stack, not a potentially gigabyte
    # core dump that keeps the dying reader alive and fills the server's disk.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, OSError, ValueError):
        pass  # Windows, or a host that does not allow changing this limit.
    from .footprints import TileNotPublished, _fetch_gba

    lat, lon, half_m = map(float, sys.argv[1:4])
    timeout = int(sys.argv[4])
    try:
        payload = _fetch_gba(lat, lon, half_m, timeout)
    except TileNotPublished as exc:
        payload = {"error": "tile_not_published", "detail": str(exc)}
    except Exception as exc:  # a source outage remains retryable and uncached
        payload = {"error": "read_failed", "detail": str(exc)[:200]}
    print(json.dumps(payload), flush=True)


if __name__ == "__main__":
    main()
