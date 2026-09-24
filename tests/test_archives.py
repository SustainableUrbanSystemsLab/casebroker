"""A case shipped in parts reads back on the master as the one archive it used to be.

The layout is Eddy3D's CaseParts: <case>.mesh.tar.gz once meshing passed,
<case>.case_NNN.tar.gz per finished direction, <case>.tar.gz last with the
manifest naming every part.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

from casebroker import archives, cli

CASE = "v2-00e76e426bea6d52"


def _tar(path: Path, files: dict[str, str]) -> str:
    with tarfile.open(path, "w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{CASE}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mesh(done: Path) -> str:
    return _tar(done / f"{CASE}.mesh.tar.gz", {
        "cfg.json": "{}", f"{CASE}/mesh/constant/polyMesh/owner": "o",
        f"{CASE}/eddy3d-study.json": "as meshed",
    })


def _direction(done: Path, d: str) -> str:
    return _tar(done / f"{CASE}.{d}.tar.gz", {f"{CASE}/{d}/2000/U": f"U of {d}"})


def _case(done: Path, parts: dict[str, str | None]) -> None:
    manifest = {"case_id": CASE, "parts": [
        {"part": p, "archive": f"{CASE}.{p}.tar.gz", "sha256": sha} for p, sha in parts.items()]}
    _tar(done / f"{CASE}.tar.gz", {"manifest.json": json.dumps(manifest),
                                   f"{CASE}/eddy3d-study.json": "at the end"})


def test_a_node_switched_off_mid_case_leaves_a_partial_case_that_still_unpacks(tmp_path):
    # COD-359-38, 2026-09-24: 7 of 32 directions solved, then the machine was off.
    done = tmp_path / "done"
    done.mkdir()
    _mesh(done)
    for d in ("case_000", "case_011"):
        _direction(done, d)

    st = archives.status(done, CASE)
    assert st.state == "partial"
    assert [p.name for p in st.parts] == [f"{CASE}.mesh.tar.gz", f"{CASE}.case_000.tar.gz", f"{CASE}.case_011.tar.gz"]

    root = archives.extract_case(done, CASE, tmp_path / "x")
    assert (root / CASE / "mesh/constant/polyMesh/owner").read_text() == "o"
    assert (root / CASE / "case_011/2000/U").read_text() == "U of case_011"


def test_a_case_is_complete_when_every_part_its_manifest_names_is_here(tmp_path):
    done = tmp_path / "done"
    done.mkdir()
    shas = {"mesh": _mesh(done), "case_000": _direction(done, "case_000")}
    _case(done, shas)
    assert archives.status(done, CASE, verify=True).state == "complete"

    root = archives.extract_case(done, CASE, tmp_path / "x")
    assert (root / "manifest.json").is_file()
    assert (root / CASE / "eddy3d-study.json").read_text() == "at the end", \
        "the case archive is unpacked last: its copy of a shared file wins"
    assert (root / CASE / "case_000/2000/U").is_file()


def test_a_case_archive_that_arrived_before_its_parts_is_waiting(tmp_path):
    # Syncthing does not deliver in the order the node wrote.
    done = tmp_path / "done"
    done.mkdir()
    sha = _mesh(done)
    _case(done, {"mesh": sha, "case_000": "0" * 64})
    st = archives.status(done, CASE)
    assert st.state == "waiting"
    assert st.missing == [f"{CASE}.case_000.tar.gz"]


def test_a_part_whose_hash_is_not_the_manifests_is_corrupt_when_verified(tmp_path):
    done = tmp_path / "done"
    done.mkdir()
    _direction(done, "case_000")
    _case(done, {"case_000": "f" * 64})
    assert archives.status(done, CASE).state == "complete", "not hashed unless asked: gigabytes"
    st = archives.status(done, CASE, verify=True)
    assert (st.state, st.corrupt) == ("corrupt", [f"{CASE}.case_000.tar.gz"])


def test_a_single_archive_from_before_parts_is_complete_as_it_always_was(tmp_path):
    done = tmp_path / "done"
    done.mkdir()
    _tar(done / f"{CASE}.tar.gz", {"manifest.json": json.dumps({"case_id": CASE}),
                                   f"{CASE}/case_000/2000/U": "U"})
    assert archives.status(done, CASE).state == "complete"
    assert archives.case_ids(done) == [CASE]


def test_the_cli_lists_each_case_with_its_state(tmp_path, capsys):
    done = tmp_path / "done"
    done.mkdir()
    _mesh(done)
    _direction(done, "case_000")
    (done / "notes.txt").write_text("not an archive")

    assert cli.main(["archives", str(done)]) == 0
    out = capsys.readouterr().out
    assert f"{CASE}  partial   mesh, 1 direction part(s)" in out
    assert "1 case(s): 1 partial" in out

    assert cli.main(["archives", str(done), "--json", "--state", "complete"]) == 0
    assert json.loads(capsys.readouterr().out) == []
