"""Every commit advances the version -- by a hook, not by memory.

The bump stalled a third time: the CI check ran on pull requests only, and this
repo pushes to main. So the hook moves PATCH on every commit that does not move
the version itself, and CI checks a push commit by commit. Pinned against a
throwaway repository, because the mechanism is git, not string editing.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bump_version as bv  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.org")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "casebroker"\nversion = "0.5.0"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "casebroker"\nversion = "0.5.0"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- a note for the next version\n\n## [0.5.0] - 2026-09-17\n",
        encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    for name in ("PYPROJECT", "UV_LOCK", "CHANGELOG"):
        monkeypatch.setattr(bv, name, tmp_path / getattr(bv, name).name)
    monkeypatch.setattr(bv, "ROOT", tmp_path)
    monkeypatch.delenv("SKIP_VERSION_BUMP", raising=False)
    return tmp_path


def test_the_hook_moves_patch_and_stages_all_three_files(repo):
    (repo / "a.txt").write_text("x")
    git(repo, "add", "a.txt")
    assert bv.main(["--auto"]) == 0
    assert 'version = "0.5.1"' in git(repo, "show", ":pyproject.toml")
    assert 'version = "0.5.1"' in git(repo, "show", ":uv.lock")
    log = git(repo, "show", ":CHANGELOG.md")
    assert log.index("## [Unreleased]") < log.index("## [0.5.1]") < log.index("- a note"), \
        "what was under Unreleased now sits under the version it lands in"


def test_a_bump_made_by_hand_is_left_alone(repo):
    (repo / "pyproject.toml").write_text('[project]\nname = "casebroker"\nversion = "0.6.0"\n')
    git(repo, "add", "pyproject.toml")
    assert bv.main(["--auto"]) == 0
    assert 'version = "0.6.0"' in git(repo, "show", ":pyproject.toml")


def test_the_committer_can_say_no_and_ci_then_refuses(repo, monkeypatch, capsys):
    monkeypatch.setenv("SKIP_VERSION_BUMP", "1")
    assert bv.main(["--auto"]) == 0
    assert 'version = "0.5.0"' in git(repo, "show", ":pyproject.toml")
    (repo / "a.txt").write_text("x")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "left at 0.5.0")
    assert bv.main(["--check", "--range", "HEAD~1..HEAD"]) == 1
    assert "leaves it at 0.5.0" in capsys.readouterr().out


def test_a_push_is_checked_commit_by_commit(repo, capsys):
    (repo / "a.txt").write_text("x")
    git(repo, "add", "a.txt")
    assert bv.main(["--auto"]) == 0
    git(repo, "commit", "-q", "-m", "one, bumped")
    first = git(repo, "rev-parse", "HEAD").strip()
    (repo / "b.txt").write_text("y")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "two, not bumped")
    assert bv.main(["--check", "--range", f"{first}~1..{first}"]) == 0
    assert bv.main(["--check", "--range", f"{first}~1..HEAD"]) == 1, "one bad commit fails the push"
    assert "leaves it at 0.5.1" in capsys.readouterr().out
    # A push with no `before` (a new branch) checks the last commit alone.
    assert bv.main(["--check", "--range", "0" * 40 + "..HEAD"]) == 1
