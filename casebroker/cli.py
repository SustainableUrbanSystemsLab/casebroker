"""``casebroker`` -- set up a broker, and work out why one is misbehaving.

Two jobs. The first is credentials: generate a token, ask a deployment what a
given one can actually do, and manage the human accounts that log into the
dashboard. The second is diagnosis: `doctor` finds every connection string on
the box, says which one works, and names the broken piece rather than a symptom.

Existed as a dangling ``[project.scripts]`` entry pointing at a module that was
never written, so the installed command died with ``ModuleNotFoundError``. It was
removed; this is the version that earns the entry back, because "how do I get a
token, and what can this one do?" turned out to be the genuinely confusing part
of operating this service and had no tooling at all.

Deliberately dependency-free (``secrets``, ``urllib``, ``argparse``): the thing
you reach for when a deployment is misbehaving must not itself need an install
step, and this has to run on a login node where ``uv sync`` may not have happened.
The ``account`` and ``init-db`` commands import ``casebroker.db``, which is also
standard library only until it opens a Postgres DSN -- and they import it inside
the command, so the commands that do not need a database stay unaffected.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import subprocess
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
    # Which credential answered, not just what it can do. A per-machine token
    # and a shared env token both report write, and "which one is this box
    # actually using?" is the question that matters when revoking one.
    kind = got.get("auth")
    # Only when something was actually recognised. `whoami` labels an
    # UNRECOGNISED credential `auth: "token"` too, so printing the kind
    # unconditionally told you a revoked machine token was a shared env one.
    if scope == "none":
        kind = None
    if kind == "machine":
        print(f"kind:  per-machine token for {got.get('machine')!r}")
    elif kind == "session":
        print(f"kind:  logged in as {got.get('user')!r} ({got.get('role')})")
    elif kind == "token":
        print("kind:  shared environment token")
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
    if got.get("auth") == "accounts":
        print("         (secured by accounts -- no shared token configured)")
    if got.get("auth") == "OPEN":
        print("\nWARNING: auth is OFF -- every caller can lease, complete and add "
              "cases.\nOpen the dashboard and create an admin account, or run "
              "`casebroker account create`,\nbefore this faces a network. "
              "(CASEBROKER_WRITE_TOKENS still works too.)",
              file=sys.stderr)
        return 1
    return 0


def cmd_fleet(args) -> int:
    """Report what a scheduler is holding, from squeue, to the broker.

    Run on a login node -- that is the only place squeue exists. The broker
    cannot see SLURM itself: a queued worker has never contacted it, so until its
    first lease it does not exist there at all. Without this the dashboard can
    say "5,000 pending, 0 leased" while twenty workers sit third in the queue,
    and cannot tell that apart from nothing being scheduled.

    Intended for cron, e.g. every 5 minutes:
        */5 * * * * casebroker fleet --broker https://... --cluster Phoenix
    """
    import shutil
    if not shutil.which("squeue"):
        print("squeue not found — run this on a cluster login node", file=sys.stderr)
        return 2
    user = args.user or os.environ.get("USER") or ""
    cmd = ["squeue", "-h", "-u", user, "-o", "%T"]
    if args.name:
        cmd += ["-n", args.name]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:                       # noqa: BLE001
        print(f"squeue failed: {e}", file=sys.stderr)
        return 2
    if out.returncode != 0:
        print(f"squeue failed: {out.stderr.strip()[:300]}", file=sys.stderr)
        return 2

    states = [ln.strip().upper() for ln in out.stdout.splitlines() if ln.strip()]
    running = sum(1 for st in states if st == "RUNNING")
    # Everything not running and not finishing is waiting on the scheduler.
    # COMPLETING is counted as neither: it is on its way out, and counting it as
    # queued would show capacity that is actually leaving.
    queued = sum(1 for st in states if st in ("PENDING", "CONFIGURING", "REQUEUED",
                                              "RESIZING", "SUSPENDED"))
    token = _resolve_token(args.token, "write")
    body = json.dumps({"cluster": args.cluster, "queued": queued, "running": running,
                       "detail": args.detail}).encode("utf-8")
    req = urllib.request.Request(args.broker.rstrip("/") + "/v1/fleet", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        print(f"broker rejected the report: {e.code} "
              f"{e.read()[:200].decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"could not reach {args.broker}: {e}", file=sys.stderr)
        return 2
    print(f"{args.cluster}: {running} running, {queued} queued")
    return 0


# -- doctor -------------------------------------------------------------------

# Where a connection string plausibly lives. Every one found is tried and
# reported, which is the entire point: the failure this command exists for was
# three copies of the DSN in three files, two of them stale, with no way to tell
# which one the tooling was actually using.
DSN_ENV = ("CASEBROKER_DB", "DBSTRING")
DSN_FILES = (".env", "../.env", "DBSTRING.md", "DBSTRIG.md")

# Sibling checkouts. The copy that was ACTUALLY live during the 2026-09 rotation
# sat in a neighbouring repo's `.env`, not in this one -- so a doctor that looks
# only at its own working directory reports "no connection string found" while
# a working credential sits one directory over. A rotation runbook that says
# "doctor finds every copy" has to be true, or it quietly leaves a stale secret
# behind. Bounded to one level and to files named `.env`, so this stays a
# targeted look at where checkouts live rather than a scan of the disk.
DSN_GLOBS = ("../*/.env",)
MAX_SCANNED_BYTES = 1 << 20

DSN_RE = r"postgres(?:ql)?://([^:\s]+):([^@\s]+)@([^:/\s]+):(\d+)/(\w+)"


def _redact(dsn: str) -> str:
    import re
    return re.sub(r"(postgres(?:ql)?://[^:]+:)[^@]+(@)", r"\1***\2", dsn)


def _dsn_candidates(explicit):
    """[(origin, dsn)], de-duplicated, most explicit first."""
    import re
    out = []
    seen = set()

    def add(origin, text):
        for m in re.finditer(DSN_RE, text or ""):
            dsn = m.group(0)
            if dsn not in seen:
                seen.add(dsn)
                out.append((origin, dsn))

    if explicit:
        add("--dsn", explicit)
    for name in DSN_ENV:
        add("$" + name, os.environ.get(name, ""))
    paths = [pathlib.Path(rel) for rel in DSN_FILES]
    for pattern in DSN_GLOBS:
        try:
            paths.extend(sorted(pathlib.Path().glob(pattern)))
        except OSError:
            pass

    done = set()
    for path in paths:
        try:
            if not path.is_file():
                continue
            key = path.resolve()
            if key in done:
                continue
            done.add(key)
            if path.stat().st_size > MAX_SCANNED_BYTES:
                continue
            add(str(path), path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return out


def _diagnose_pg(err, user):
    """Turn libpq's message into the thing that is actually wrong.

    Supabase's pooler answers a bad password with `password authentication
    failed for user "postgres"` -- naming the UPSTREAM role, not the
    `postgres.<project-ref>` that was actually supplied. That reads like a
    username problem and cost this project real time. The two failures ARE
    distinguishable and the distinction is the useful part: a recognised tenant
    with a bad password says "password authentication failed", while a tenant
    the pooler cannot resolve says "Tenant or user not found".
    """
    low = err.lower()
    if "tenant or user not found" in low or "enotenant" in low:
        return ("the pooler did not recognise the tenant -- for Supabase the user must keep its "
                "project suffix, e.g. postgres.<project-ref>, not bare postgres")
    if "password authentication failed" in low:
        return ("the tenant WAS recognised and the password was rejected. The message names the "
                "upstream role, not the " + repr(user) + " actually supplied -- so this is a "
                "stale password, not a wrong username")
    if "ssl connection is required" in low or "esslrequired" in low:
        return "the server demands TLS -- connect with sslmode=require"
    if "timeout" in low or "timed out" in low:
        return "no answer in time -- wrong host or port, or egress is blocked from here"
    if "could not translate host name" in low or "name or service not known" in low:
        return "the host name does not resolve"
    return err.strip().splitlines()[0][:200]


def _check_dsn(dsn):
    """(ok, [detail lines]) for one connection string."""
    import re
    lines = []
    m = re.match(DSN_RE, dsn)
    user = m.group(1)
    try:
        import psycopg
    except ImportError:
        lines.append("    psycopg not installed here, so the DSN cannot be tested "
                     "(the broker check below uses the service's own connection)")
        return None, lines
    try:
        sep = "&" if "?" in dsn else "?"
        with psycopg.connect(dsn + sep + "sslmode=require", connect_timeout=20) as c:
            with c.cursor() as cur:
                cur.execute("SELECT current_user, version()")
                who, ver = cur.fetchone()
                lines.append("    connected as " + who + " -- " + ver.split(",")[0])
                # Campaign tables AND identity tables. Checking only the
                # five campaign ones reported "schema present" on a database
                # with no auth layer at all -- which is exactly the database
                # you get from a deployment that never finished upgrading.
                cur.execute("SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public'")
                have = {r[0] for r in cur.fetchall()}
                campaign = {'cases', 'events', 'workers', 'fleet', 'footprints'}
                identity = {'users', 'sessions', 'worker_tokens'}
                if not have & campaign:
                    lines.append("    schema NOT present -- no campaign tables here")
                    return False, lines
                lines.append("    campaign tables: %d/%d" % (len(have & campaign), len(campaign)))
                missing_campaign = sorted(campaign - have)
                if missing_campaign:
                    lines.append("    MISSING: " + ", ".join(missing_campaign))
                if identity <= have:
                    cur.execute("SELECT count(*) FILTER (WHERE role = 'admin'), count(*) "
                                "FROM users")
                    admins, users = cur.fetchone()
                    if users == 0:
                        lines.append("    accounts: none -- this broker is still in "
                                     "first-run setup, and /v1/auth/setup is open to "
                                     "whoever reaches it first")
                    elif admins == 0:
                        lines.append("    accounts: %d, but NONE is an admin -- nobody "
                                     "can manage this broker" % users)
                    else:
                        lines.append("    accounts: %d (%d admin)" % (users, admins))
                else:
                    lines.append("    identity tables MISSING (%s) -- accounts, sessions "
                                 "and per-machine worker tokens cannot work; start the "
                                 "broker once, or run `casebroker init-db`, to create them"
                                 % ", ".join(sorted(identity - have)))
                if 'schema_meta' in have:
                    cur.execute("SELECT value FROM schema_meta WHERE key = 'version'")
                    row = cur.fetchone()
                    lines.append("    schema version: " + (row[0] if row else "unrecorded"))
                else:
                    lines.append("    schema version: predates the version marker")
                cur.execute("SELECT state, count(*) FROM cases GROUP BY state ORDER BY 2 DESC")
                rows = cur.fetchall()
                summary = ", ".join("%s %s" % (st, format(n, ",")) for st, n in rows)
                lines.append("    cases: " + (summary or "none"))
        return True, lines
    except Exception as e:                      # noqa: BLE001 -- reported, never fatal
        lines.append("    " + _diagnose_pg(str(e), user))
        return False, lines



# -- accounts, straight against the database ---------------------------------
#
# The browser flow (first visit -> "Set up this broker") is the pleasant path and
# stays the recommended one. This exists for the three cases it cannot serve:
# a broker already on the internet, where whoever reaches /v1/auth/setup first
# becomes the permanent admin; a forgotten password, which previously meant
# hand-writing an scrypt hash into production; and any headless deployment where
# there is no browser to open.
#
# These talk to the DATABASE, not to the broker's API, so they work when the
# service is down and need no credential beyond the one that reaches Postgres.


def _account_db(args):
    """Open the campaign database the same way `doctor` finds it."""
    from . import db
    dsn = args.db
    if not dsn:
        # $CASEBROKER_DB first, and taken VERBATIM. _dsn_candidates finds
        # connection strings by matching DSN_RE, which only recognises
        # `postgres://` -- so a SQLite path, which is what the service itself
        # accepts and what every local deployment sets, was invisible to it and
        # these commands answered "no database given" with CASEBROKER_DB
        # plainly set. db.connect() decides the engine from the shape of the
        # string, exactly as the service does, so nothing here needs to.
        for env in DSN_ENV:
            value = os.environ.get(env, "").strip()
            if value:
                dsn = value
                print("using $%s: %s" % (env, _redact(dsn)), file=sys.stderr)
                break
    if not dsn:
        candidates = _dsn_candidates(None)
        if not candidates:
            print("no database given and none found in " +
                  ", ".join("$" + e for e in DSN_ENV) +
                  "\n  -> pass --db <path-or-dsn>", file=sys.stderr)
            return None
        origin, dsn = candidates[0]
        print("using %s: %s" % (origin, _redact(dsn)), file=sys.stderr)
    return db.connect(dsn)


def _read_password(args, prompt: str) -> str | None:
    """From stdin when asked, otherwise an interactive double-entry prompt.

    Never from a command-line flag: an argv password lands in shell history, in
    `ps` output, and in any process listing the machine keeps.
    """
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
        if len(pw) < 12:
            print("password must be at least 12 characters", file=sys.stderr)
            return None
        return pw
    import getpass
    first = getpass.getpass(prompt)
    if len(first) < 12:
        # Length over composition rules, matching the API: it is the only
        # property that reliably predicts a guessable password.
        print("password must be at least 12 characters", file=sys.stderr)
        return None
    if first != getpass.getpass("again: "):
        print("passwords do not match", file=sys.stderr)
        return None
    return first


def cmd_account_create(args) -> int:
    from . import auth, db
    conn = _account_db(args)
    if conn is None:
        return 2
    pw = _read_password(args, "password for %s: " % args.username)
    if pw is None:
        return 2
    try:
        user = db.create_user(conn, args.username, auth.hash_password(pw), role=args.role)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:                                    # noqa: BLE001
        print("an account named %r already exists" % args.username, file=sys.stderr)
        return 1
    print("created %s (%s)" % (user["username"], user["role"]))
    return 0


def cmd_account_list(args) -> int:
    from . import db
    conn = _account_db(args)
    if conn is None:
        return 2
    users = db.list_users(conn)
    if not users:
        print("no accounts yet -- this broker is still in first-run setup")
        return 0
    width = max(len(u["username"]) for u in users)
    for u in users:
        seen = u["last_login_at"]
        print("  %-*s  %-6s  last login %s"
              % (width, u["username"], u["role"],
                 _stamp(seen) if seen else "never"))
    return 0


def cmd_account_passwd(args) -> int:
    """Set a password without knowing the old one. This is the recovery path."""
    from . import auth, db
    conn = _account_db(args)
    if conn is None:
        return 2
    pw = _read_password(args, "new password for %s: " % args.username)
    if pw is None:
        return 2
    if not db.set_password(conn, args.username, auth.hash_password(pw)):
        print("no account named %r" % args.username, file=sys.stderr)
        return 1
    # Worth saying out loud: the operator may be doing this because a laptop was
    # lost, and "did that actually kick them out?" is the question they have.
    print("password changed for %s; every session it held is now revoked"
          % args.username)
    return 0


def cmd_account_role(args) -> int:
    from . import db
    conn = _account_db(args)
    if conn is None:
        return 2
    try:
        changed = db.set_role(conn, args.username, args.role)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not changed:
        print("no account named %r" % args.username, file=sys.stderr)
        return 1
    print("%s is now %s" % (args.username, args.role))
    return 0


def cmd_account_delete(args) -> int:
    from . import db
    conn = _account_db(args)
    if conn is None:
        return 2
    try:
        removed = db.delete_user(conn, args.username)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not removed:
        print("no account named %r" % args.username, file=sys.stderr)
        return 1
    print("deleted %s" % args.username)
    return 0


def cmd_initdb(args) -> int:
    """Create or bring forward the schema without starting the service.

    The broker already does this on its first connection, so this is not
    normally necessary. It is here for the two moments when doing it separately
    is the point: proving a new database and its credentials work before a
    deploy depends on them, and applying a column that a running service would
    hit mid-request.
    """
    from . import db
    conn = _account_db(args)
    if conn is None:
        return 2
    version = db.schema_version(conn)
    tables = sorted(db.parse_schema_columns(db.SCHEMA))
    print("schema applied -- version %s, %d tables" % (version, len(tables)))
    print("  " + ", ".join(tables))
    if db.count_users(conn) == 0:
        print("\nno accounts yet. Either open the dashboard and set one up, or:")
        print("  casebroker account create --username <you> --role admin")
    return 0


def _stamp(epoch) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        int(epoch), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def cmd_doctor(args) -> int:
    """Check the database, the broker and the token, and name the broken one.

    Written after an evening lost to a stale connection string: the tooling had
    three copies of the DSN, two out of date, and the error pointed at the wrong
    thing. Every check here is one whose failure was once mistaken for something
    else.
    """
    ok = True
    print("Wind Simulation Broker -- doctor\n")

    print("database")
    candidates = _dsn_candidates(args.dsn)
    if not candidates:
        print("    no connection string in " + ", ".join("$" + e for e in DSN_ENV)
              + " or " + ", ".join(DSN_FILES))
        print("    (only matters for direct database work -- the broker carries its own)")
    working, untested = [], False
    for origin, dsn in candidates:
        good, lines = _check_dsn(dsn)
        if good is None:
            untested = True
            print("  ----  " + origin + ": " + _redact(dsn))
        else:
            print(("  PASS  " if good else "  FAIL  ") + origin + ": " + _redact(dsn))
        for line in lines:
            print(line)
        if good:
            working.append(origin)
    stale = [o for o, _ in candidates if o not in working]
    if candidates and not working and not untested:
        ok = False
        print("    -> none of the connection strings found here works")
    elif working and stale:
        # Not a failure -- one of them works, which is what matters. But a stale
        # duplicate lying around IS how the wrong one gets picked up next time,
        # so it is called out and reflected in the summary rather than buried
        # under "all checks passed" while a FAIL line sits on screen.
        print("    -> use " + working[0] + "; stale, delete or correct: " + ", ".join(stale))

    print("\nbroker")
    if not args.broker:
        print("    skipped (pass --broker)")
    else:
        try:
            h = _get(args.broker, "/healthz", None)
            print("  PASS  " + args.broker + " -- version " + str(h.get("version"))
                  + ", auth " + str(h.get("auth")))
            if not h.get("db_ok"):
                ok = False
                print("  FAIL  the broker cannot reach ITS database -- that is the service's "
                      "own DSN, not necessarily any of the ones above")
            else:
                print("    its database: " + str(h.get("db", "not reported")))
            if h.get("auth") == "OPEN":
                print("    WARNING: auth is OFF -- anyone who can reach this can lease, "
                      "complete and delete cases")
        except Exception as e:                  # noqa: BLE001
            ok = False
            print("  FAIL  cannot reach " + args.broker + ": " + str(e))

    print("\ntoken")
    token = _resolve_token(args.token, "write")
    if not args.broker:
        print("    skipped (needs --broker)")
    elif not token:
        print("    none supplied and none in " + ", ".join(SCOPE_ENV["write"]))
    else:
        try:
            scope = _get(args.broker, "/v1/whoami", token).get("scope", "none")
            if scope == "none":
                ok = False
                print("  FAIL  the broker does not recognise this token.")
                print("        A per-machine token (dashboard > Machines > Issue token) "
                      "works immediately\n        and needs no redeploy -- if this is one, "
                      "it has been revoked or mistyped.")
                print("        A shared environment token only works once it is in "
                      "CASEBROKER_WRITE_TOKENS\n        on the service AND the service "
                      "has been redeployed.")
            else:
                print("  PASS  scope " + scope)
        except Exception as e:                  # noqa: BLE001
            ok = False
            print("  FAIL  " + str(e))

    if not ok:
        verdict = "one or more checks FAILED (see above)"
    elif stale:
        verdict = ("working, but " + str(len(stale)) + " stale connection string"
                   + ("s" if len(stale) > 1 else "") + " should be deleted before "
                   "something picks one up")
    else:
        verdict = "all checks passed"
    print("\n" + verdict)
    return 0 if ok else 1


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

    fl = sub.add_parser("fleet", help="report squeue counts to the broker (run on a login node)")
    fl.add_argument("--broker", required=True)
    fl.add_argument("--cluster", required=True, help="name shown on the dashboard, e.g. Phoenix")
    fl.add_argument("--token", default=None, help="write token; '-' reads stdin; omitted reads the environment")
    fl.add_argument("--user", default=None, help="squeue -u (default: $USER)")
    fl.add_argument("--name", default=None, help="only count jobs with this --job-name")
    fl.add_argument("--detail", default=None, help="free text shown under the counts, e.g. the QOS")
    fl.set_defaults(func=cmd_fleet)

    h = sub.add_parser("health", help="a broker's version and auth posture")
    h.add_argument("--broker", required=True)
    h.set_defaults(func=cmd_health)

    d = sub.add_parser("doctor", help="check the database, the broker and the token, and "
                                      "name whichever one is broken")
    d.add_argument("--broker", default=None, help="the service to check (optional)")
    d.add_argument("--token", default=None,
                   help="the token; '-' reads stdin; omitted reads the environment")
    d.add_argument("--dsn", default=None,
                   help="a connection string to test in addition to the ones discovered")
    d.set_defaults(func=cmd_doctor)

    ac = sub.add_parser("account", help="create and manage the human accounts that "
                                       "log into the dashboard").add_subparsers(
        dest="subcmd", required=True)

    def _account_common(p, *, role_default=None):
        p.add_argument("--db", default=None,
                       help="database path or DSN (default: discovered, like doctor)")
        p.add_argument("--username", required=True)
        if role_default is not None:
            p.add_argument("--role", choices=("admin", "viewer"), default=role_default)
        return p

    acn = _account_common(ac.add_parser(
        "create", help="create an account (the headless equivalent of first-run setup)"),
        role_default="admin")
    acn.add_argument("--password-stdin", action="store_true",
                     help="read the password from stdin instead of prompting")
    acn.set_defaults(func=cmd_account_create)

    acl = ac.add_parser("list", help="every account, with when it last logged in")
    acl.add_argument("--db", default=None)
    acl.set_defaults(func=cmd_account_list)

    acp = _account_common(ac.add_parser(
        "passwd", help="set a password without knowing the old one -- the recovery path"))
    acp.add_argument("--password-stdin", action="store_true")
    acp.set_defaults(func=cmd_account_passwd)

    acr = _account_common(ac.add_parser("role", help="promote or demote an account"),
                          role_default=None)
    acr.add_argument("--role", choices=("admin", "viewer"), required=True)
    acr.set_defaults(func=cmd_account_role)

    acd = _account_common(ac.add_parser("delete", help="remove an account and its sessions"))
    acd.set_defaults(func=cmd_account_delete)

    idb = sub.add_parser("init-db", help="create or bring forward the schema without "
                                         "starting the service")
    idb.add_argument("--db", default=None,
                     help="database path or DSN (default: discovered, like doctor)")
    idb.set_defaults(func=cmd_initdb)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
