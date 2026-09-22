"""Suite-wide test settings. Kept to what cannot live in one test module."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import dataset  # noqa: E402


@pytest.fixture(autouse=True)
def _dataset_refresh_runs_inline(monkeypatch):
    """The dataset aggregate refreshes in a background thread in production
    (casebroker/dataset.py, DatasetCache.peek) so a case's GET never waits for
    it. Under test that thread would outlive the test that started it and read
    the NEXT test's database, or its monkeypatched db functions -- so here it
    runs inline, in the request that asked for it. The threaded path has its
    own test (test_dataset_stats.py)."""
    monkeypatch.setattr(dataset, "_in_background", lambda fn: fn())
