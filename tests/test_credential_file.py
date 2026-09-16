"""The machine credential lands on a shared filesystem.

`casebroker worker setup` writes a live bearer token into machine.env on cluster
nodes and lab workstations. PACE home directories are routinely group-readable,
so the mode that file is created with is the whole exposure -- and creating it
wide and narrowing it afterwards leaves a window where it is not.
"""

from __future__ import annotations

import os
import stat

import pytest

from casebroker.cli import _write_env

pytestmark = pytest.mark.skipif(os.name == "nt",
                                reason="POSIX mode bits; Windows uses directory ACLs")


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_a_new_file_is_never_world_readable(tmp_path):
    p = tmp_path / "machine.env"
    _write_env(p, {"CASEBROKER_TOKEN": "a-live-credential"})
    assert _mode(p) == 0o600, oct(_mode(p))
    assert "a-live-credential" in p.read_text()


def test_it_is_created_restricted_rather_than_narrowed_afterwards(tmp_path, monkeypatch):
    """The window, pinned.

    If the file is created with the umask's mode and chmodded after, then at the
    moment the token hits the disk it is readable by the whole group. This
    asserts the mode at CREATE time, not at the end.
    """
    p = tmp_path / "machine.env"
    seen = {}
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **k):
        if str(path) == str(p):
            seen["mode"] = mode
            seen["excl_create"] = bool(flags & os.O_CREAT)
        return real_open(path, flags, mode, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(os, "umask", lambda m: 0o022)
    _write_env(p, {"CASEBROKER_TOKEN": "t"})
    assert seen.get("excl_create"), "did not create the file through os.open"
    assert seen["mode"] == 0o600, f"created with {oct(seen.get('mode', 0))}"


def test_an_existing_permissive_file_is_narrowed_before_the_token_lands(tmp_path):
    """Enrolling twice, or onto a box that already had a machine.env."""
    p = tmp_path / "machine.env"
    p.write_text("WIND_NP=24\n")
    p.chmod(0o644)
    _write_env(p, {"CASEBROKER_TOKEN": "second-credential"})
    assert _mode(p) == 0o600, oct(_mode(p))
    # and the unrelated settings survived
    assert "WIND_NP=24" in p.read_text()
    assert "second-credential" in p.read_text()


def test_other_settings_are_preserved(tmp_path):
    p = tmp_path / "machine.env"
    p.write_text("WIND_NP=24\nCASEBROKER_URL=https://old\n")
    _write_env(p, {"CASEBROKER_URL": "https://new", "CASEBROKER_TOKEN": "t"})
    text = p.read_text()
    assert "WIND_NP=24" in text
    assert "https://new" in text and "https://old" not in text
