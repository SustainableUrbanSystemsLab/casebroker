"""Case Broker — central scheduling service for the Wind v2 CFD campaign."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("casebroker")
except PackageNotFoundError:
    # Running from source tree without the package installed (e.g. bare python -m)
    __version__ = "0.0.0+dev"
