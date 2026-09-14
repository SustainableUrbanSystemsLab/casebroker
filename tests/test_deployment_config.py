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
