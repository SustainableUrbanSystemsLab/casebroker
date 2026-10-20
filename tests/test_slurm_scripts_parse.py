"""Every SLURM/shell script under slurm/ parses.

puma_e3d_node.sbatch shipped unparseable: an apostrophe inside
``${E3D_DONE:?... group's storage}`` opens a quote bash never closes, so the
job died before its first line. ``bash -n`` sees it; nothing ran it.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = sorted(p for p in (Path(__file__).resolve().parent.parent / "slurm").iterdir()
                 if p.suffix in (".sbatch", ".sh"))


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash on this machine")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_there_are_scripts_to_check():
    assert any(p.name == "puma_e3d_node.sbatch" for p in SCRIPTS)
