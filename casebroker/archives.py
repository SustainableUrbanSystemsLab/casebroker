"""A case's archives on the master: one per case, or a case shipped in parts.

An Eddy3D node ships a case while it runs (Eddy3D's CaseParts, docs/fleet.md):
``<case>.mesh.tar.gz`` as soon as meshing passed, ``<case>.case_NNN.tar.gz`` as
soon as each direction is finished, and ``<case>.tar.gz`` last. That last one
carries ``manifest.json``, whose ``parts`` names every part with its sha256.
Every archive is laid out under the same ``<case>/`` folder, so unpacking all of
them into one place, the case archive last, gives the tree one archive used to.

Before this, a node switched off mid-case took every finished direction with it
(COD-359-38, 2026-09-24: 7 of 32 directions, lost). Parts without a case
archive are now what such a case leaves on the master.

A case is in one of these states here:

- ``complete``: the case archive and every part its manifest names;
- ``waiting``: the case archive is here, but some of its parts are not yet --
  Syncthing does not deliver files in the order they were written;
- ``partial``: parts and no case archive -- the node stopped, or is still
  solving;
- ``corrupt``: a part is here but its sha256 is not the one the manifest names
  (only with ``verify``).

run_case.sh and nodes from before parts write only ``<case>.tar.gz``, with no
``parts`` in the manifest: ``complete``, as they always were.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

_PART = re.compile(r"^(?P<id>.+?)\.(?P<part>mesh|case_[^.]+)\.tar\.gz$")
_CASE = re.compile(r"^(?P<id>[^.]+)\.tar\.gz$")


@dataclass
class CaseArchives:
    case_id: str
    state: str
    archive: Path | None = None
    parts: list[Path] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "case_id": self.case_id, "state": self.state,
            "archive": self.archive.name if self.archive else None,
            "parts": [p.name for p in self.parts], "missing": self.missing, "corrupt": self.corrupt,
        }


def _part_sort_key(path: Path) -> tuple[int, str]:
    # The mesh first, then the directions in order: the order they were written.
    m = _PART.match(path.name)
    return (0 if m and m["part"] == "mesh" else 1, path.name)


def parts_of(done: Path, case_id: str) -> list[Path]:
    """The parts of one case in the done folder, mesh first."""
    found = [p for p in done.glob(f"{case_id}.*.tar.gz")
             if (m := _PART.match(p.name)) and m["id"] == case_id]
    return sorted(found, key=_part_sort_key)


def case_ids(done: Path) -> list[str]:
    """Every case the done folder holds anything of, archive or part."""
    ids = set()
    for p in done.glob("*.tar.gz"):
        m = _PART.match(p.name) or _CASE.match(p.name)
        if m:
            ids.add(m["id"])
    return sorted(ids)


def read_manifest(archive: Path, case_id: str) -> dict | None:
    """``<case>/manifest.json`` out of a case archive, or None when it has none."""
    try:
        with tarfile.open(archive, "r:gz") as tf:
            for name in (f"{case_id}/manifest.json", "manifest.json"):
                try:
                    member = tf.getmember(name)
                except KeyError:
                    continue
                f = tf.extractfile(member)
                return json.load(f) if f else None
    except (OSError, tarfile.TarError, json.JSONDecodeError):
        return None
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def status(done: Path, case_id: str, verify: bool = False) -> CaseArchives:
    """Where one case stands on the master. ``verify`` hashes every part the
    manifest names -- gigabytes on a campaign case, so off by default."""
    done = Path(done)
    archive = done / f"{case_id}.tar.gz"
    parts = parts_of(done, case_id)
    if not archive.is_file():
        return CaseArchives(case_id, "partial" if parts else "missing", None, parts)

    manifest = read_manifest(archive, case_id) or {}
    named = manifest.get("parts") or []
    present = {p.name: p for p in parts}
    missing = [p["archive"] for p in named if p.get("archive") not in present]
    corrupt = []
    if verify:
        for p in named:
            path = present.get(p.get("archive"))
            if path is not None and p.get("sha256") and _sha256(path) != p["sha256"]:
                corrupt.append(path.name)
    state = "corrupt" if corrupt else "waiting" if missing else "complete"
    return CaseArchives(case_id, state, archive, parts, missing, corrupt)


def scan(done: Path, verify: bool = False) -> list[CaseArchives]:
    return [status(done, cid, verify) for cid in case_ids(Path(done))]


def extract_case(done: Path, case_id: str, into: Path) -> Path:
    """Unpack every archive of a case into ``into`` -- parts first, the case
    archive last so its copies win -- and return the ``<case>/`` folder.

    A partial case unpacks too: the mesh and the directions that were finished.
    tarfile's "data" filter refuses absolute paths and links out of ``into``."""
    done, into = Path(done), Path(into)
    archives = parts_of(done, case_id)
    whole = done / f"{case_id}.tar.gz"
    if whole.is_file():
        archives.append(whole)
    if not archives:
        raise FileNotFoundError(f"no archive of {case_id} in {done}")
    into.mkdir(parents=True, exist_ok=True)
    for a in archives:
        with tarfile.open(a, "r:gz") as tf:
            tf.extractall(into, filter="data")
    return into / case_id
