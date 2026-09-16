"""Overture is an optional extra now, and must fail legibly when absent.

Its client pulls pyarrow: 122 MB of a 328 MB site-packages, 37% of the image,
for a code path that is off unless CASEBROKER_OVERTURE_FALLBACK is set and that
runs in a subprocess, so the broker never imports it even when installed. That
is the wrong thing to ship by default to a memory-capped service.

What must not happen is the fallback failing with a Python traceback fragment
that reads like a bug in the broker.
"""

from __future__ import annotations

import subprocess
import tomllib
import pathlib

import pytest

from casebroker import footprints

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_overturemaps_is_not_a_base_dependency():
    deps = " ".join(_pyproject()["project"]["dependencies"])
    assert "overturemaps" not in deps, (
        "overturemaps pulls pyarrow (122 MB) into every install for a path that "
        "is off by default and runs in a subprocess")


def test_it_is_still_installable_as_an_extra():
    extras = _pyproject()["project"]["optional-dependencies"]
    assert "overture" in extras, "the fallback must stay installable on purpose"
    assert any("overturemaps" in d for d in extras["overture"])


def test_a_missing_extra_says_how_to_fix_it(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "/usr/bin/python: No module named overturemaps\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError) as ei:
        footprints.fetch(33.749, -84.388)
    msg = str(ei.value)
    assert "casebroker[overture]" in msg, "does not say how to install it"
    assert "GlobalBuildingAtlas" in msg, "does not say what to use instead"
    assert "Traceback" not in msg


def test_a_real_overture_failure_still_reports_itself(monkeypatch):
    """The missing-module branch must not swallow every other failure."""
    class _R:
        returncode = 2
        stdout = ""
        stderr = "HTTP 503 from the Overture mirror\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError, match="503"):
        footprints.fetch(33.749, -84.388)


def test_the_fallback_stays_off_by_default(monkeypatch):
    monkeypatch.delenv("CASEBROKER_OVERTURE_FALLBACK", raising=False)
    import importlib
    reloaded = importlib.reload(footprints)
    try:
        assert reloaded.OVERTURE_FALLBACK is False
    finally:
        importlib.reload(footprints)
