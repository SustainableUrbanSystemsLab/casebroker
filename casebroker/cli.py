"""``casebroker`` -- generate, inspect and verify the broker's tokens.

Existed as a dangling ``[project.scripts]`` entry pointing at a module that was
never written, so the installed command died with ``ModuleNotFoundError``. It was
removed; this is the version that earns the entry back, because "how do I get a
token, and what can this one do?" turned out to be the genuinely confusing part
of operating this service and had no tooling at all.

Deliberately dependency-free (``secrets``, ``urllib``, ``argparse``): the thing
you reach for when a deployment is misbehaving must not itself need an install
step, and this has to run on a login node where ``uv sync`` may not have happened.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

from . import __version__

# 32 bytes of urandom, base64url -> 43 chars. Same generator the .env.example has
# always recommended; having it in the tool means nobody has to remember it, and
# nobody reaches for a weaker source because the one-liner was inconvenient.
TOKEN_BYTES = 32

SCOPE_ENV = {
    "write": ("CASEBROKER_WRITE_TOKENS", "CASEBROKER_TOKENS"),
    "read": ("CASEBROKER_READ_TOKENS", "CASEBROKER_READONLY_TOKENS"),
}


def _get(broker: str, path: str, token: str | None, timeout: float = 30.0):
    req = urllib.request.Request(broker.rstrip("/") + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _resolve_token(explicit: str | None, scope: str) -> str | None:
    """--token, else stdin, else the environment variable for that scope.

    Reading from stdin matters: a token passed as an argv value is visible in the
    process table and lands in shell history, which is precisely how a credential
    outlives the terminal it was typed into.
    """
    if explicit == "-":
        return sys.stdin.read().strip()
    if explicit:
        return explicit
    for name in SCOPE_ENV.get(scope, ()):
        raw = os.environ.get(name, "")
        if raw.strip():
            return raw.split(",")[0].strip()
    return None


def cmd_new(args) -> int:
    for _ in range(args.count):
        print(secrets.token_urlsafe(TOKEN_BYTES))
    if args.count and not args.quiet:
        canonical, legacy = SCOPE_ENV[args.scope]
        print(f"\n# Set as {canonical} on the service, then redeploy.", file=sys.stderr)
        print(f"# ({legacy} is the deprecated spelling and still works.)", file=sys.stderr)
        print("# Comma-separate to run several at once -- that is how you rotate",
              file=sys.stderr)
        print("# without stranding a worker mid-lease: add the new one, move the",
              file=sys.stderr)
        print("# workers over, then drop the old one.", file=sys.stderr)
    return 0


def cmd_check(args) -> int:
    """Ask the broker what a token can do, and optionally assert it."""
    token = _resolve_token(args.token, args.expect or "write")
    try:
        got = _get(args.broker, "/v1/whoami", token)
    except urllib.error.URLError as e:
        print(f"could not reach {args.broker}: {e}", file=sys.stderr)
        return 2
    scope = got.get("scope", "none")
    detail = got.get("detail")
    print(f"scope: {scope}" + (f"  ({detail})" if detail else ""))
    if not token:
        print("note: no token was sent (none given, none in the environment)",
              file=sys.stderr)
    if args.expect and scope != args.expect:
        print(f"FAILED: expected scope {args.expect!r}, got {scope!r}", file=sys.stderr)
        return 1
    return 0


def cmd_health(args) -> int:
    try:
        got = _get(args.broker, "/healthz", None)
    except urllib.error.URLError as e:
        print(f"could not reach {args.broker}: {e}", file=sys.stderr)
        return 2
    scopes = got.get("scopes", {})
    print(f"ok:      {got.get('ok')}")
    print(f"version: {got.get('version')}")
    print(f"auth:    {got.get('auth')}"
          f"  (write tokens: {scopes.get('write', '?')},"
          f" read tokens: {scopes.get('read', '?')})")
    print(f"db:      {got.get('db')}")
    if got.get("auth") == "OPEN":
        print("\nWARNING: auth is OFF -- every caller can lease, complete and add "
              "cases.\nSet CASEBROKER_WRITE_TOKENS before this faces a network.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="casebroker", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"casebroker {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    tok = sub.add_parser("token", help="generate or inspect tokens").add_subparsers(
        dest="subcmd", required=True)

    n = tok.add_parser("new", help="generate a token")
    n.add_argument("--scope", choices=("write", "read"), default="write",
                   help="which variable the hint text should name (default: write)")
    n.add_argument("--count", type=int, default=1)
    n.add_argument("--quiet", action="store_true", help="token only, no hint")
    n.set_defaults(func=cmd_new)

    c = tok.add_parser("check", help="ask a broker what a token can do")
    c.add_argument("--broker", required=True)
    c.add_argument("--token", default=None,
                   help="the token; '-' reads stdin; omitted reads the environment")
    c.add_argument("--expect", choices=("write", "read", "none"), default=None,
                   help="exit non-zero unless the scope is this (for CI)")
    c.set_defaults(func=cmd_check)

    h = sub.add_parser("health", help="a broker's version and auth posture")
    h.add_argument("--broker", required=True)
    h.set_defaults(func=cmd_health)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
