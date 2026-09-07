"""Case broker for the v2 real-city CFD campaign.

**The version is declared in ``pyproject.toml`` and nowhere else.** Everything
that reports it -- the FastAPI app's OpenAPI metadata, ``/healthz``, the
dashboard's header badge -- reads ``__version__`` from here, which reads the
installed distribution's own metadata. Before this, the version string was
written out by hand in three places that had no way of noticing when one of
them was bumped and the others were not.

The fallback matters: ``importlib.metadata`` describes what is *installed*, so a
source tree that was never installed (a bare ``python -c "import casebroker"``
with the repo on ``sys.path``) has no metadata to read. That reports as
``0+unknown`` rather than guessing a number, because a wrong version is worse
than an obviously absent one -- ``/healthz`` is how a deploy is confirmed, and
it must never claim a release that is not actually what is running.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _distribution_version

try:
    __version__ = _distribution_version("casebroker")
except PackageNotFoundError:  # source tree, not an installed distribution
    __version__ = "0+unknown"

__all__ = ["__version__"]
