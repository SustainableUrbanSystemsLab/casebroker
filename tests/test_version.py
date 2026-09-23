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
    r"""No source file may restate the version as a literal.

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


# -- the release actually happening ------------------------------------------
#
# Everything above compares the three COPIES of the version to each other. That
# is drift, and it was the bug of the day when it was written -- but it says
# nothing about whether the declared version was ever RELEASED, and it cannot.
# Between v0.2.0 and v0.3.0 four pull requests merged, the version moved once,
# and nothing tagged it: the README's tag badge read v0.2.0 while the running
# service's /healthz read 0.3.0, and every test in this file passed the whole
# time because the copies agreed perfectly with one another.

WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_the_declared_version_is_the_newest_one_in_the_changelog():
    """A bump and its entry are one commit, by convention -- so the newest
    release heading must be the version the package declares. Catches both
    halves of the mistake: bumping without writing it up, and writing up a
    version that was never declared."""
    from casebroker import __version__
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([0-9][^\]]*)\]", text, re.MULTILINE)
    assert headings, "CHANGELOG.md has no release headings at all"
    assert headings[0] == __version__, (
        f"CHANGELOG.md's newest release is {headings[0]}, but the package "
        f"declares {__version__}. Move the Unreleased entries under a "
        f"'## [{__version__}]' heading, or bump to {headings[0]}."
    )


def test_a_version_bump_on_main_cuts_its_own_tag():
    """The half of a release that gets forgotten is the tag, so merging the
    bump has to be enough. Tagging by hand must keep working too -- it is what
    the runbook in docs/operations.md tells you to do."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    triggers = wf.split("jobs:", 1)[0]
    assert "branches: [main]" in triggers, \
        "a merge that moves the version has to be able to cut the tag"
    assert "tags:" in triggers, "and an explicit tag push must still publish"
    assert "contents: write" in wf, "creating a tag and a release both need it"


def test_the_release_is_published_by_the_job_that_creates_the_tag():
    """A tag pushed with GITHUB_TOKEN does NOT start another workflow run --
    GitHub suppresses that to stop loops. So a job that pushed the tag and left
    the `on: push: tags:` trigger to publish would create tags that never
    became releases: the same bug, one layer quieter. Both steps therefore live
    in one job."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    jobs = wf.split("jobs:", 1)[1]
    assert jobs.count("\n  release:") == 1 and "\n  tag:" not in jobs, \
        "splitting tagging and publishing across jobs reintroduces the trap"
    assert "git push origin" in jobs and "gh release create" in jobs


def test_a_tag_that_disagrees_with_pyproject_still_fails():
    """The original guard. Automating the usual path must not quietly drop the
    check on the manual one."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "does not match pyproject.toml" in wf
    assert "exit 1" in wf


def test_nothing_is_published_without_tests_and_a_changelog_entry():
    """Automating the bump makes the write-up the easy half to skip."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "pytest tests/" in wf
    assert "CHANGELOG.md" in wf and "no '## [$pkg]' heading" in wf


# -- the version has to move with the work -----------------------------------
#
# It stalled twice. Four merges after v0.2.0; thirty-seven after v0.3.0,
# including seven features. The first repair automated the TAGGING and left the
# BUMP a step someone had to remember, so it stalled again straight away. These
# pin the forcing function, because the lesson of both stalls is that a step
# nobody is compelled to take does not get taken.

import subprocess  # noqa: E402

BUMP = ROOT / "scripts" / "bump_version.py"


def _bump(*args, repo=None):
    """Run the helper. With `repo`, run the COPY inside that scratch repo.

    The script finds the project from its own __file__, so pointing only `cwd`
    at the scratch repo would leave it editing and checking THIS one -- which is
    exactly what the first draft of these tests did, and why they passed against
    a working tree whose version had already moved.
    """
    script = (repo / "scripts" / "bump_version.py") if repo else BUMP
    return subprocess.run([sys.executable, str(script), *args],
                          cwd=str(repo or ROOT), capture_output=True, text=True,
                          encoding="utf-8")  # what the script writes; the locale default is cp1252 on Windows


def test_the_bump_arithmetic_is_semver():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("bump_version", BUMP)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.bumped("0.3.0", "patch") == "0.3.1"
    assert mod.bumped("0.3.0", "minor") == "0.4.0"
    assert mod.bumped("0.3.0", "major") == "1.0.0"
    # The lower components reset -- a minor bump off 1.2.9 is 1.3.0, not 1.3.9.
    assert mod.bumped("1.2.9", "minor") == "1.3.0"
    assert mod.bumped("1.2.9", "major") == "2.0.0"
    # Ordering is numeric, not lexicographic: "0.10.0" > "0.9.0" only if the
    # components are compared as integers, and that comparison is what --check
    # uses to decide whether a branch raised the version.
    assert mod.parts("0.10.0") > mod.parts("0.9.0")


def test_the_suggested_level_comes_from_the_commit_subjects():
    """This repo writes conventional commits on every change, so the level it
    is arguing for is already written down -- nobody has to classify anything
    a second time."""
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("bump_version", BUMP)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.SUBJECT.match("feat(pair): a machine pairs itself").group("type") == "feat"
    assert mod.SUBJECT.match("fix(db): a dropped connection").group("type") == "fix"
    assert mod.SUBJECT.match("feat(lease)!: drop the old field").group("bang") == "!"
    assert mod.SUBJECT.match("no conventional prefix here") is None

    # Both comma forms this repo actually writes, and they mean different
    # things: a comma in the SCOPE says nothing about the level, a comma in the
    # TYPE lists several types and `feat` anywhere in it is a feature.
    assert mod.SUBJECT.match("docs,slurm: the campaign runs from").group("type") == "docs,slurm"
    assert mod.SUBJECT.match("fix(dashboard,healthz): the wizard").group("type") == "fix"
    assert "feat" in mod.SUBJECT.match("feat,docs: both").group("type").split(",")


def _scratch_repo(tmp_path, version="0.3.0", subject="feat(x): something new"):
    """A throwaway repo whose version has NOT moved.

    The script locates the project from its own path, so copying it into
    tmp_path/scripts makes tmp_path the project -- which is the only way to
    exercise the failure without a ref in this repo that is ahead of the
    working tree.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "bump_version.py").write_text(
        BUMP.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "casebroker"\nversion = "{version}"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "casebroker"\nversion = "{version}"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- something\n", encoding="utf-8")
    run = lambda *a: subprocess.run(a, cwd=str(tmp_path), capture_output=True, text=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", subject)
    return tmp_path


def test_check_fails_when_a_branch_leaves_the_version_alone(tmp_path):
    """The whole point: red until the version moves."""
    repo = _scratch_repo(tmp_path)
    r = _bump("--check", "--base", "HEAD", repo=repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "has to move it" in r.stdout
    # And it says what to run, with a level, rather than only complaining.
    assert "scripts/bump_version.py --level" in r.stdout


def test_the_bump_makes_the_check_pass(tmp_path):
    """End to end in a scratch repo: the command the failure recommends is the
    command that satisfies it -- including the changelog heading, which is a
    separate way to fail at release time."""
    repo = _scratch_repo(tmp_path)
    assert _bump("--check", "--base", "HEAD", repo=repo).returncode == 1

    applied = _bump("--level", "minor", repo=repo)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "0.3.0 → 0.4.0" in applied.stdout

    ok = _bump("--check", "--base", "HEAD", repo=repo)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.4.0] - " in text
    assert "## [Unreleased]" in text, "Unreleased stays, empty, for the next change"
    assert '"0.4.0"' in (repo / "uv.lock").read_text(encoding="utf-8"), \
        "uv.lock is a third copy of the number and has to move with the others"


def test_check_also_demands_the_changelog_heading(tmp_path):
    """A version release.yml would refuse to publish is caught in the pull
    request rather than after the merge, where the failure would be a landed
    change that cannot be released.

    Driven rather than grepped: the earlier version of this asserted on the
    script's source text, which proves the line exists and nothing about
    whether it fires.
    """
    repo = _scratch_repo(tmp_path)
    # The version rises, but nothing writes the heading -- exactly what doing
    # the bump by hand instead of with --level produces.
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "casebroker"\nversion = "0.4.0"\n', encoding="utf-8")

    r = _bump("--check", "--base", "HEAD", repo=repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "version rises 0.3.0 → 0.4.0" in r.stdout, "the rise itself is fine"
    assert "no '## [0.4.0]' heading" in r.stdout
    assert "release.yml will refuse to publish" in r.stdout


def test_ci_runs_the_check_on_every_pull_request():
    """A gate that does not run is not a gate."""
    wf = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "bump_version.py --check" in wf
    job = wf.split("  version:", 1)[1].split("\n  sqlite:", 1)[0]
    assert "pull_request" in job, "the check has to run on PRs, where it can block"
    assert "fetch-depth: 0" in job, "comparing against the base needs its history"


def test_status_identifies_the_broker_without_needing_healthz(client):
    """The dashboard must not need a second request to name what it connected to.

    It took `version` and `db` from /healthz, so a browser that could reach
    /v1/status but not /healthz -- a filter objecting to a response containing
    something shaped like a connection string, say -- connected successfully and
    then could not report which broker or database it had connected TO.
    """
    from casebroker import __version__
    st = client.get("/v1/status", headers={"Authorization": "Bearer t"}).json()
    assert st["version"] == __version__
    assert st["db"], "status must name its database"
    # and it must still be a status response, not just an identity one
    assert "by_state" in st and "done_last_24h" in st


def test_status_redacts_the_database_password_like_healthz_does(client):
    """A second endpoint exposing the DSN is a second place to leak it."""
    body = client.get("/v1/status", headers={"Authorization": "Bearer t"}).text
    assert "password" not in body.lower()


# -- doctor: diagnosing the thing that is actually wrong ---------------------

def test_doctor_separates_a_stale_password_from_a_wrong_username():
    """The message that cost this project an evening.

    Supabase's pooler answers a BAD PASSWORD with `password authentication
    failed for user "postgres"` -- naming the upstream role rather than the
    `postgres.<project-ref>` that was actually supplied. Read literally it looks
    like the username is wrong, and the fix people then reach for (stripping the
    project suffix) produces a DIFFERENT error, `Tenant or user not found`,
    which looks like progress and is not. The two must be told apart.
    """
    from casebroker.cli import _diagnose_pg

    stale_pw = _diagnose_pg(
        'connection failed: FATAL:  password authentication failed for user "postgres"',
        "postgres.projectref")
    assert "stale password" in stale_pw
    assert "not a wrong username" in stale_pw

    no_tenant = _diagnose_pg("connection failed: FATAL: Tenant or user not found", "postgres")
    assert "project suffix" in no_tenant
    assert "stale password" not in no_tenant


def test_doctor_finds_every_connection_string_not_just_the_first(tmp_path, monkeypatch):
    """The actual failure mode: three copies of the DSN, two of them stale, and
    no way to tell which the tooling was using. Reporting only the first would
    reproduce exactly that."""
    import os
    from casebroker.cli import _dsn_candidates

    (tmp_path / ".env").write_text(
        "DBSTRING=postgresql://postgres.abc:oldpw@aws-0-us-west-2.pooler.supabase.com:6543/postgres\n",
        encoding="utf-8")
    (tmp_path / "DBSTRIG.md").write_text(
        "the string is postgresql://postgres.abc:otherpw@aws-0-us-west-2.pooler.supabase.com:6543/postgres\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CASEBROKER_DB", raising=False)
    monkeypatch.delenv("DBSTRING", raising=False)

    found = _dsn_candidates("postgresql://postgres.abc:explicit@h.example:6543/postgres")
    origins = [o for o, _ in found]
    assert origins[0] == "--dsn", "an explicitly supplied DSN must be tried first"
    assert ".env" in origins and "DBSTRIG.md" in origins
    assert len({d for _, d in found}) == 3, "distinct strings must not be collapsed"


def test_doctor_looks_in_sibling_checkouts_too(tmp_path, monkeypatch):
    """Where the live credential was actually hiding during the rotation.

    It sat in a NEIGHBOURING repo's `.env`, so a doctor that searched only its
    own working directory printed "no connection string found" while a working
    one lay a directory over. That is the worst possible answer: a rotation
    runbook that trusts it leaves a stale secret behind on the box.
    """
    from casebroker.cli import _dsn_candidates

    root = tmp_path / "workspace"
    (root / "broker").mkdir(parents=True)
    (root / "other-project").mkdir()
    (root / "other-project" / ".env").write_text(
        "DBSTRING=postgresql://postgres.abc:siblingpw@h.example:6543/postgres\n",
        encoding="utf-8")

    monkeypatch.chdir(root / "broker")
    monkeypatch.delenv("CASEBROKER_DB", raising=False)
    monkeypatch.delenv("DBSTRING", raising=False)

    found = _dsn_candidates(None)
    assert any("other-project" in origin for origin, _ in found), \
        "a sibling checkout's .env must be found"
    assert any("siblingpw" in dsn for _, dsn in found)


def test_doctor_reports_each_connection_string_once(tmp_path, monkeypatch):
    """`.env` is reachable both directly and through the sibling glob. Listing
    the same file twice would make one credential look like two, which is
    precisely the confusion this command exists to end."""
    from casebroker.cli import _dsn_candidates

    root = tmp_path / "workspace"
    (root / "broker").mkdir(parents=True)
    (root / "broker" / ".env").write_text(
        "DBSTRING=postgresql://postgres.abc:onlypw@h.example:6543/postgres\n",
        encoding="utf-8")

    monkeypatch.chdir(root / "broker")
    monkeypatch.delenv("CASEBROKER_DB", raising=False)
    monkeypatch.delenv("DBSTRING", raising=False)

    found = _dsn_candidates(None)
    assert len(found) == 1, f"one credential, reported once -- got {found}"


def test_doctor_never_prints_a_password():
    """Doctor output is the thing people paste into chat when asking for help."""
    from casebroker.cli import _redact

    out = _redact("postgresql://postgres.abc:SuperSecret123@host.example:6543/postgres")
    assert "SuperSecret123" not in out
    assert "postgres.abc" in out and "host.example" in out
