"""Real child failures: Python try/except cannot catch a native process exit."""
import json
import os
import sys

import pytest

from casebroker import footprints, geo_process


def test_native_crash_is_contained_and_next_read_can_finish():
    if os.name != "posix":
        pytest.skip("POSIX signal exit status")
    with pytest.raises(RuntimeError, match="SIGSEGV"):
        geo_process._run([sys.executable, "-c",
                          "import os,signal,resource; "
                          "resource.setrlimit(resource.RLIMIT_CORE,(0,0)); "
                          "os.kill(os.getpid(),signal.SIGSEGV)"], 5)
    assert geo_process._run([sys.executable, "-c", "print('{\"n\": 79}')"], 5) == {"n": 79}


def test_timeout_kills_and_reaps_the_reader(tmp_path):
    pidfile = tmp_path / "pid"
    code = ("import os,time,pathlib; "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)")
    with pytest.raises(RuntimeError, match="exceeded"):
        geo_process._run([sys.executable, "-c", code], 1)
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


@pytest.mark.parametrize("message,missing_tile,detail", [
    ({"error": "tile_not_published", "detail": "w040_n10_w035_n05"},
     True, "w040_n10_w035_n05"),
    ({"error": "read_failed", "detail": "connection reset"}, False, "connection reset"),
])
def test_reader_preserves_missing_tile_versus_outage(message, missing_tile, detail):
    # Configuration tests reload footprints, replacing its exception class.
    exception = footprints.TileNotPublished if missing_tile else RuntimeError
    with pytest.raises(exception, match=detail):
        geo_process._run([sys.executable, "-c", f"print({json.dumps(message)!r})"], 5)


def test_invalid_child_output_is_a_transient_failure():
    with pytest.raises(RuntimeError, match="invalid response"):
        geo_process._run([sys.executable, "-c", "print('not JSON')"], 5)
