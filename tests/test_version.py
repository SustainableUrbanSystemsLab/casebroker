"""The version is reported, and there is exactly one of it.

Three copies of the literal ``0.1.0`` used to live in this tree -- in
``pyproject.toml``, in the ``FastAPI(...)`` constructor and in the dashboard's
header badge -- with nothing to notice when one moved and the others did not.
The badge's copy was already stale in a way no test could have caught, because
nothing asserted the three agreed.

These tests are the guard: the version comes from the installed distribution's
metadata, every surface reports that same value, and ``test_no_hardcoded_version``
fails if a fourth copy is ever pasted back in.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]

# MAJOR.MINOR.PATCH with the optional pre-release / build metadata semver allows.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient
    from casebroker.app import create_app
    return TestClient(create_app(db_path=str(tmp_path / "v.sqlite"), tokens=["t"]))


def test_version_is_semver():
    from casebroker import __version__
    assert SEMVER.match(__version__), (
        f"{__version__!r} is not a semantic version. If this is '0+unknown' the "
        "package is not installed, so importlib.metadata has nothing to read."
    )


def test_pyproject_is_the_single_source_of_truth():
    """The installed metadata must agree with the file a release actually edits."""
    from casebroker import __version__
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert declared, "pyproject.toml has no top-level version"
    assert declared.group(1) == __version__, (
        f"pyproject declares {declared.group(1)}, installed metadata says "
        f"{__version__} -- reinstall the package, or a release was half-applied."
    )


def test_healthz_reports_the_version_without_auth(client):
    """/healthz is unauthenticated on purpose, so a deploy check needs no token."""
    from casebroker import __version__
    r = client.get("/healthz")  # deliberately no Authorization header
    assert r.status_code == 200
    assert r.json()["version"] == __version__


def test_openapi_reports_the_same_version(client):
    from casebroker import __version__
    assert client.get("/openapi.json").json()["info"]["version"] == __version__


def test_healthz_still_leaks_no_credential(client):
    """Adding a field to /healthz is exactly when the redaction rule gets broken."""
    body = client.get("/healthz").text
    assert "password" not in body.lower()
    assert "secret" not in body.lower()


def _hardcoded(line: str, version: str) -> bool:
    """True if `line` restates `version` as a literal, with or without a v prefix."""
    return bool(re.search(rf"(?<![\w.])v?{re.escape(version)}(?!\w)(?!\.\d)", line))


def test_the_drift_guard_actually_catches_the_form_that_motivated_it():
    """The dashboard badge shipped `v0.1.0`; a guard blind to that is decorative."""
    v = "0.1.0"
    assert _hardcoded('<span class="brand-badge" id="b">v0.1.0</span>', v)
    assert _hardcoded('app = FastAPI(title="x", version="0.1.0")', v)
    assert _hardcoded('USER_AGENT = "casebroker/v0.1.0"', v)
    assert _hardcoded('# broker v0.1.0', v)
    assert _hardcoded('released 0.1.0.', v)
    # ...without firing on a longer token that merely contains it
    assert not _hardcoded('0.1.0rc1', v)
    assert not _hardcoded('foo0.1.0', v)
    assert not _hardcoded('1.0.1.0', v)
    assert not _hardcoded('0.1.0.5', v)


def test_no_hardcoded_version():
    """No source file may restate the version as a literal.

    Matches an x.y.z that is not part of a longer token, skipping the one
    legitimate declaration in pyproject.toml and any dependency pin.

    The optional ``v?`` matters more than it looks: the copy that motivated this
    guard was the dashboard badge's ``<span ...>v0.1.0</span>``, and ``v`` is a
    word character -- so a bare ``(?<![\w.])`` lookbehind rejects the only
    candidate position and the guard sails straight past the exact string it
    exists to catch. ``(?!\w)`` still rules out a longer token (``0.1.0rc1``)
    and ``(?!\.\d)`` a longer dotted number (``0.1.0.5``), while a sentence-final
    period (``released 0.1.0.``) is still reported.
    """
    from casebroker import __version__
    targets = [
        *(ROOT / "casebroker").rglob("*.py"),
        *(ROOT / "casebroker" / "static").rglob("*.html"),
    ]
    offenders = []
    for f in targets:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _hardcoded(line, __version__):
                offenders.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, (
        "the version is hardcoded here; read casebroker.__version__ instead:\n"
        + "\n".join(offenders)
    )
