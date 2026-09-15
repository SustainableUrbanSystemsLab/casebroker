"""What the shipped deployment files actually configure.

These are text assertions over compose.yaml and .env.example rather than
behaviour tests, because the bug they exist for lived entirely in configuration
and no amount of application testing could have caught it: compose forwards a
variable to the container only by NAMING it -- there is no `env_file:` on the
broker service -- so dropping a name silently stops that credential reaching the
service, while every test that constructs the app directly keeps passing.

That is a fail-OPEN. Every deployment predating the write/read rename has
`CASEBROKER_TOKENS` in its `.env`, since it is the only name the old compose
would accept; forwarding only the new spellings means `docker compose up -d`
after a pull restarts the broker with no tokens at all, on a public TLS
endpoint, while the live workers carry on as though nothing had changed.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "compose.yaml").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()


def _forwarded(text: str) -> set[str]:
    """Environment names compose actually passes to the broker service."""
    service = text.split("services:", 1)[1].split("caddy:", 1)[0]
    body = service.split("environment:", 1)[1]
    return {m.group(1) for m in re.finditer(r"^\s+(CASEBROKER_\w+):", body, re.M)}


def test_compose_forwards_every_credential_variable_the_app_reads():
    """Including the DEPRECATED spellings. app.py collapses canonical-or-legacy
    and refuses the both-set-to-different-values case, so forwarding both is
    safe -- and not forwarding the legacy pair disarms every deployment that
    still uses it."""
    assert {
        "CASEBROKER_DB",
        "CASEBROKER_WRITE_TOKENS",
        "CASEBROKER_READ_TOKENS",
        "CASEBROKER_TOKENS",
        "CASEBROKER_READONLY_TOKENS",
        "CASEBROKER_SETUP_TOKEN",
    } <= _forwarded(COMPOSE)


def test_compose_requires_no_credential_to_start():
    """A `:?` on a token variable made compose refuse to start without it. That
    was the old guard against an unauthenticated broker; an ACCOUNT is the guard
    now, so the variable is optional -- but every one of them must then carry a
    `:-` default, or compose fails on a deployment that legitimately sets none."""
    for name in _forwarded(COMPOSE) - {"CASEBROKER_DB"}:
        assert re.search(r"%s: \$\{%s:-" % (name, name), COMPOSE), name
        assert not re.search(r"%s: \$\{%s:\?" % (name, name), COMPOSE), name


def test_the_documented_first_command_needs_nothing_uncommented():
    """`cp .env.example .env && docker compose up -d` is the documented
    sequence. It died before starting when compose demanded a variable
    .env.example did not define, so nothing in .env.example may be required."""
    required = {m.group(1) for m in
                re.finditer(r"\$\{(\w+):\?", COMPOSE)}
    uncommented = {m.group(1) for m in
                   re.finditer(r"^([A-Z_]+)=", ENV_EXAMPLE, re.M)}
    assert required <= uncommented, (
        "compose requires %s, which .env.example does not set"
        % sorted(required - uncommented))


def test_env_example_names_only_variables_the_app_actually_reads():
    """A stale name in the shipped example is a credential an operator believes
    they set and the service never sees."""
    app_py = (ROOT / "casebroker" / "app.py").read_text()
    for name in re.findall(r"^#?([A-Z_]*CASEBROKER\w+)=", ENV_EXAMPLE, re.M):
        assert name in app_py or name in COMPOSE, name


# -- the production-database CI job: what an unusable credential must do -----
#
# Run as a SUBPROCESS pytest rather than by calling the fixture, because the
# thing being pinned is the exit code of the whole run -- that is what turns a
# CI job red or green, and it is not observable from inside the session the
# fixture belongs to.

def _pg_suite(env_extra: dict) -> "tuple[int, str]":
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    # A DSN that cannot possibly connect, pointed at a port nothing listens on,
    # so this test never touches a real database of any kind.
    env["CASEBROKER_TEST_PG_DSN"] = "postgresql://nobody:wrong@127.0.0.1:5/nothing"
    env.pop("CASEBROKER_TEST_PG_REQUIRED", None)
    env.pop("GITHUB_STEP_SUMMARY", None)
    env.update(env_extra)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests" / "test_db_postgres.py"),
         "-rs", "-p", "no:cacheprovider"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_an_unusable_production_credential_skips_rather_than_failing():
    """A stale DBSTRING said nothing about the commit under test, and turned
    main red anyway -- on a branch that gates deploys, which trains people to
    ignore a red X. It is also inconsistent: with the secret ABSENT the same
    job went green having tested exactly as much (nothing), because the
    module's skipif fires. The line belongs at "did this coverage run", not at
    "is a secret set"."""
    code, out = _pg_suite({})
    assert code == 0, out[-3000:]
    assert "19 skipped" in out or "skipped" in out


def test_the_skip_says_why_rather_than_passing_silently():
    """The real hazard of a skip is coverage that quietly stops running. -rs is
    what makes the reason visible: with -v alone pytest prints a bare SKIPPED,
    and a print() inside the fixture is swallowed by pytest's own capture and
    discarded for a skip -- so the reason has to travel this way."""
    code, out = _pg_suite({})
    assert code == 0
    assert "Postgres tests did not run" in out, out[-3000:]


def test_github_gets_the_reason_on_the_run_page(tmp_path):
    """$GITHUB_STEP_SUMMARY is a FILE, so it survives the output capture that
    eats a printed annotation."""
    summary = tmp_path / "summary.md"
    code, _ = _pg_suite({"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)})
    assert code == 0
    written = summary.read_text(encoding="utf-8")
    assert "Postgres coverage skipped" in written
    assert "Postgres tests did not run" in written


def test_required_turns_an_unusable_credential_back_into_a_failure():
    """The escape hatch for a deployment that would rather CI enforce this
    coverage than report on it."""
    code, out = _pg_suite({"CASEBROKER_TEST_PG_REQUIRED": "1"})
    assert code == 1, out[-3000:]
    assert "refused the pre-flight connection" in out


def test_ci_passes_rs_so_the_skip_reason_is_not_swallowed():
    """The behaviour above is only visible in CI if the workflow asks for it."""
    wf = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    real_pg = wf.split("real postgres (main only)", 1)[1].split("deploy-smoke-test", 1)[0]
    assert "-rs" in real_pg, "without -rs a skipped production-DB job states no reason"
