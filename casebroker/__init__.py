"""Case Broker -- central scheduling service for the Wind v2 CFD campaign.

**The version is declared in ``pyproject.toml`` and nowhere else.** Everything
that reports it -- the FastAPI app's OpenAPI metadata, ``/healthz``, the
dashboard's header badge -- reads ``__version__`` from here, which reads the
installed distribution's own metadata. Before this, the version string was
written out by hand in three places that had no way of noticing when one of
them was bumped and the others were not.

The fallback matters: ``importlib.metadata`` describes what is *installed*, so a
source tree that was never installed (a bare ``python -m`` with the repo on
``sys.path``) has no metadata to read. It reports ``0.0.0+dev`` -- a valid
semantic version, so the checks in ``tests/test_version.py`` still hold, while
the ``+dev`` build metadata makes it obvious this is not a released build.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("casebroker")
except PackageNotFoundError:
    # Running from source tree without the package installed (e.g. bare python -m)
    __version__ = "0.0.0+dev"

__all__ = ["__version__"]
