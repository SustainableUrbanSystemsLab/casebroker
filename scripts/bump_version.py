#!/usr/bin/env python3
"""Move the version, and check that something moved it.

The version had stopped moving. Twice. After `v0.2.0` four pull requests
merged with no bump; after `v0.3.0`, thirty-seven more, including seven
features. Both times the mechanism was fine and the *step* was skipped, because
it was a step -- something a person had to remember at the end of work that felt
finished. The first repair automated the tagging half and left the bump manual,
which is why it failed again immediately.

So the bump is no longer remembered. It is required (`--check`, which CI runs on
every pull request and which fails until the version rises), derived (`--suggest`,
from the conventional-commit subjects this repo already writes), and applied
(`--level`, which edits all three files that have to agree).

Why the bump belongs in the PR rather than in a bot commit after the merge: a
commit pushed by CI with GITHUB_TOKEN does not re-trigger workflows, so the
deploy job would never run for it. The tag would say 0.4.0 while the deployed
service still reported 0.3.0 -- the exact disagreement this is meant to end. The
version, the code and the deployment have to land together.

    scripts/bump_version.py --level minor
    scripts/bump_version.py --suggest --since v0.3.0
    scripts/bump_version.py --check --base origin/main
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
CHANGELOG = ROOT / "CHANGELOG.md"

# MAJOR.MINOR.PATCH only. A pre-release (1.2.3-rc.1) is a deliberate, hand-cut
# thing; refusing to do arithmetic on one is better than guessing what "bump the
# patch of 1.2.3-rc.1" ought to mean.
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Conventional commits, which every commit in this repo already uses:
#   feat(pair): ...        fix(db): ...           feat(lease)!: ...
#   docs,slurm: ...        fix(dashboard,healthz): ...
#
# Two comma forms, and they are not the same. A comma inside the parentheses
# splits the SCOPE (`fix(dashboard,healthz)`), which says nothing about the
# level. A comma in the TYPE (`docs,slurm`) lists several types, and this repo
# writes both -- so the type is a list, and `feat` anywhere in it makes the
# change a feature.
SUBJECT = re.compile(r"^(?P<type>[a-z]+(?:,[a-z]+)*)(?:\([^)]*\))?(?P<bang>!)?:")


def read_version() -> str:
    m = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"),
                  re.MULTILINE)
    if not m:
        sys.exit("pyproject.toml has no top-level version")
    return m.group(1)


def version_at(ref: str) -> str:
    """The version pyproject declared at `ref`. Used to compare a branch with
    the base it is asking to merge into."""
    out = subprocess.run(["git", "show", f"{ref}:pyproject.toml"],
                         cwd=ROOT, capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"cannot read pyproject.toml at {ref}: {out.stderr.strip()}")
    m = re.search(r'^version = "([^"]+)"', out.stdout, re.MULTILINE)
    if not m:
        sys.exit(f"pyproject.toml at {ref} has no top-level version")
    return m.group(1)


def parts(version: str) -> tuple[int, int, int]:
    m = SEMVER.match(version)
    if not m:
        sys.exit(f"{version!r} is not a plain MAJOR.MINOR.PATCH version")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bumped(version: str, level: str) -> str:
    major, minor, patch = parts(version)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def subjects_since(ref: str) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s", f"{ref}..HEAD"],
                         cwd=ROOT, capture_output=True, text=True)
    if out.returncode:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def suggest(ref: str) -> str:
    """The level the commits themselves argue for.

    A suggestion, never the last word: MAJOR here means "a breaking change to
    the worker-facing protocol" (see docs/operations.md), and whether a change
    breaks a long-lived worker is a judgement no commit prefix can make. The
    author can always pass a higher `--level`.
    """
    level = "patch"
    for subject in subjects_since(ref):
        m = SUBJECT.match(subject)
        if not m:
            continue                       # not conventional; contributes patch
        if m.group("bang") or "BREAKING CHANGE" in subject:
            return "major"                 # nothing outranks it, so stop here
        if "feat" in m.group("type").split(","):
            level = "minor"
    return level


def apply(level: str) -> str:
    """Write the new version into all three files that have to agree."""
    current = read_version()
    new = bumped(current, level)

    text = PYPROJECT.read_text(encoding="utf-8")
    old_line = f'version = "{current}"'
    if text.count(old_line) != 1:
        sys.exit(f"expected exactly one {old_line!r} in pyproject.toml")
    PYPROJECT.write_text(text.replace(old_line, f'version = "{new}"', 1),
                         encoding="utf-8")

    # uv.lock names many packages; only this project's entry moves.
    lock = UV_LOCK.read_text(encoding="utf-8")
    entry = f'name = "casebroker"\nversion = "{current}"'
    if lock.count(entry) != 1:
        sys.exit("could not find casebroker's own entry in uv.lock")
    UV_LOCK.write_text(
        lock.replace(entry, f'name = "casebroker"\nversion = "{new}"', 1),
        encoding="utf-8")

    # release.yml refuses to publish a version CHANGELOG.md does not name, so
    # the heading is part of the bump rather than a second thing to remember.
    # Everything currently under Unreleased becomes this release by sitting
    # below the new heading; Unreleased stays, empty, for the next change.
    log = CHANGELOG.read_text(encoding="utf-8")
    anchor = "## [Unreleased]\n"
    if log.count(anchor) != 1:
        sys.exit("CHANGELOG.md needs exactly one '## [Unreleased]' section")
    today = dt.date.today().isoformat()
    CHANGELOG.write_text(
        log.replace(anchor, f"{anchor}\n## [{new}] - {today}\n", 1),
        encoding="utf-8")
    return new


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--level", choices=("major", "minor", "patch"),
                   help="apply this bump to pyproject.toml, uv.lock and CHANGELOG.md")
    g.add_argument("--suggest", action="store_true",
                   help="print the level the commits argue for, and nothing else")
    g.add_argument("--check", action="store_true",
                   help="exit non-zero unless this branch raises the version")
    p.add_argument("--since", default=None, help="base ref for --suggest")
    p.add_argument("--base", default=None, help="base ref for --check")
    a = p.parse_args(argv)

    if a.suggest:
        print(suggest(a.since or "HEAD~1"))
        return 0

    if a.check:
        base_ref = a.base or "origin/main"
        base, head = version_at(base_ref), read_version()
        if parts(head) > parts(base):
            print(f"✓ version rises {base} → {head}")
            # Caught here rather than at release time, where the failure would
            # be a merged change that cannot be published.
            if f"## [{head}]" not in CHANGELOG.read_text(encoding="utf-8"):
                print(f"::error::CHANGELOG.md has no '## [{head}]' heading — "
                      f"release.yml will refuse to publish this version.")
                return 1
            return 0
        level = suggest(base_ref)
        print(f"::error::This branch leaves the version at {head}. Every change "
              f"that lands on main has to move it, or the number stops meaning "
              f"anything — it has silently stalled twice already.")
        print(f"::error::The commits here look like a {level.upper()} change. Run: "
              f"python3 scripts/bump_version.py --level {level}")
        return 1

    was = read_version()
    now = apply(a.level)
    print(f"{was} → {now}")
    print("  pyproject.toml, uv.lock, CHANGELOG.md")
    print(f"  write this release's entries under '## [{now}]'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
