"""`casebroker account` and `casebroker init-db`.

The browser flow is the pleasant path and stays the recommended one. These
commands exist for the three cases it cannot serve: a broker already reachable
from the internet, where whoever opens /v1/auth/setup first becomes the
permanent admin; a forgotten password, which previously meant hand-writing an
scrypt hash into production; and a headless deployment with no browser at all.

Until these existed the CLI knew nothing whatsoever about the account system --
`token new/check`, `fleet`, `health` and `doctor` were the entire surface, and
every one of them spoke only the older shared-secret model.
"""

from __future__ import annotations

import io
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from casebroker import auth, db  # noqa: E402
from casebroker.cli import main  # noqa: E402

PW = "a-sufficiently-long-passphrase"


@pytest.fixture()
def dbpath(tmp_path):
    return str(tmp_path / "campaign.sqlite")


def run(argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin + "\n"))
    return main(argv)


# -- init-db ------------------------------------------------------------------

def test_init_db_creates_the_whole_schema(dbpath, capsys):
    assert run(["init-db", "--db", dbpath]) == 0
    out = capsys.readouterr().out
    assert "schema applied" in out
    conn = db.connect(dbpath)
    assert {"users", "sessions", "worker_tokens"} <= set(
        db.parse_schema_columns(db.SCHEMA))
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_init_db_says_when_there_is_still_no_account(dbpath, capsys):
    run(["init-db", "--db", dbpath])
    assert "no accounts yet" in capsys.readouterr().out


def test_init_db_is_safe_to_run_twice(dbpath):
    assert run(["init-db", "--db", dbpath]) == 0
    assert run(["init-db", "--db", dbpath]) == 0


# -- creating accounts --------------------------------------------------------

def test_account_create_makes_a_usable_admin(dbpath, monkeypatch, capsys):
    code = run(["account", "create", "--db", dbpath, "--username", "ada",
                "--role", "admin", "--password-stdin"], stdin=PW, monkeypatch=monkeypatch)
    assert code == 0
    assert "created ada (admin)" in capsys.readouterr().out
    conn = db.connect(dbpath)
    user = db.get_user(conn, "ada")
    assert user["role"] == "admin"
    # The password must actually verify -- the point of the command is to avoid
    # anyone hand-writing an scrypt hash.
    assert auth.verify_password(PW, user["password_hash"])


def test_account_create_defaults_to_admin_for_the_first_operator(dbpath, monkeypatch):
    """Unlike the API, whose default is `viewer`: someone reaching for the CLI
    is bootstrapping a broker, and an inaccessible broker is the failure mode."""
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    assert db.get_user(db.connect(dbpath), "ada")["role"] == "admin"


def test_account_create_can_make_a_viewer(dbpath, monkeypatch):
    run(["account", "create", "--db", dbpath, "--username", "bob", "--role", "viewer",
         "--password-stdin"], stdin=PW, monkeypatch=monkeypatch)
    assert db.get_user(db.connect(dbpath), "bob")["role"] == "viewer"


def test_account_create_refuses_a_short_password(dbpath, monkeypatch, capsys):
    code = run(["account", "create", "--db", dbpath, "--username", "ada",
                "--password-stdin"], stdin="short", monkeypatch=monkeypatch)
    assert code == 2
    assert "at least 12 characters" in capsys.readouterr().err
    assert db.get_user(db.connect(dbpath), "ada") is None


def test_account_create_refuses_a_duplicate(dbpath, monkeypatch, capsys):
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    code = run(["account", "create", "--db", dbpath, "--username", "ada",
                "--password-stdin"], stdin=PW, monkeypatch=monkeypatch)
    assert code == 1
    assert "already exists" in capsys.readouterr().err


# -- the recovery path --------------------------------------------------------

def test_account_passwd_sets_a_password_without_the_old_one(dbpath, monkeypatch, capsys):
    """The lockout recovery that did not exist: previously a forgotten password
    meant editing production by hand."""
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    new = "a-brand-new-long-passphrase"
    assert run(["account", "passwd", "--db", dbpath, "--username", "ada",
                "--password-stdin"], stdin=new, monkeypatch=monkeypatch) == 0
    assert "revoked" in capsys.readouterr().out
    user = db.get_user(db.connect(dbpath), "ada")
    assert auth.verify_password(new, user["password_hash"])
    assert not auth.verify_password(PW, user["password_hash"])


def test_account_passwd_revokes_that_accounts_sessions(dbpath, monkeypatch):
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    conn = db.connect(dbpath)
    user = db.get_user(conn, "ada")
    db.start_session(conn, user["id"], auth.hash_token("live-session"),
                     auth.session_expiry())
    assert db.session_user(conn, auth.hash_token("live-session")) is not None
    run(["account", "passwd", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin="a-brand-new-long-passphrase", monkeypatch=monkeypatch)
    assert db.session_user(db.connect(dbpath), auth.hash_token("live-session")) is None


def test_account_passwd_on_an_unknown_account_is_an_error(dbpath, monkeypatch, capsys):
    run(["init-db", "--db", dbpath])
    code = run(["account", "passwd", "--db", dbpath, "--username", "nobody",
                "--password-stdin"], stdin=PW, monkeypatch=monkeypatch)
    assert code == 1
    assert "no account named" in capsys.readouterr().err


# -- listing, roles and deletion ---------------------------------------------

def test_account_list_shows_every_account_and_its_role(dbpath, monkeypatch, capsys):
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    run(["account", "create", "--db", dbpath, "--username", "bob", "--role", "viewer",
         "--password-stdin"], stdin=PW, monkeypatch=monkeypatch)
    capsys.readouterr()
    assert run(["account", "list", "--db", dbpath]) == 0
    out = capsys.readouterr().out
    assert "ada" in out and "admin" in out
    assert "bob" in out and "viewer" in out
    assert "never" in out                      # neither has logged in


def test_account_list_never_prints_password_material(dbpath, monkeypatch, capsys):
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    capsys.readouterr()
    run(["account", "list", "--db", dbpath])
    out = capsys.readouterr().out
    assert PW not in out and "scrypt" not in out


def test_account_list_on_a_fresh_database_says_so(dbpath, capsys):
    run(["init-db", "--db", dbpath])
    capsys.readouterr()
    run(["account", "list", "--db", dbpath])
    assert "first-run setup" in capsys.readouterr().out


def test_account_role_promotes_and_demotes(dbpath, monkeypatch):
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    run(["account", "create", "--db", dbpath, "--username", "bob", "--role", "viewer",
         "--password-stdin"], stdin=PW, monkeypatch=monkeypatch)
    assert run(["account", "role", "--db", dbpath, "--username", "bob",
                "--role", "admin"]) == 0
    assert db.get_user(db.connect(dbpath), "bob")["role"] == "admin"


def test_the_last_admin_cannot_be_demoted_from_the_cli(dbpath, monkeypatch, capsys):
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    code = run(["account", "role", "--db", dbpath, "--username", "ada", "--role", "viewer"])
    assert code == 1
    assert "only admin" in capsys.readouterr().err


def test_the_last_admin_cannot_be_deleted_from_the_cli(dbpath, monkeypatch, capsys):
    run(["account", "create", "--db", dbpath, "--username", "ada", "--password-stdin"],
        stdin=PW, monkeypatch=monkeypatch)
    code = run(["account", "delete", "--db", dbpath, "--username", "ada"])
    assert code == 1
    assert "only admin" in capsys.readouterr().err
    assert db.get_user(db.connect(dbpath), "ada") is not None


def test_an_account_can_be_deleted_once_another_admin_exists(dbpath, monkeypatch):
    for name in ("ada", "cleo"):
        run(["account", "create", "--db", dbpath, "--username", name, "--password-stdin"],
            stdin=PW, monkeypatch=monkeypatch)
    assert run(["account", "delete", "--db", dbpath, "--username", "ada"]) == 0
    assert db.get_user(db.connect(dbpath), "ada") is None


def test_account_commands_find_a_sqlite_database_from_the_environment(tmp_path, monkeypatch, capsys):
    """docs/operations.md documents `casebroker account create --username ada`
    with no --db. The DSN discovery it used recognises only `postgres://`, so a
    SQLite CASEBROKER_DB -- what the service itself accepts, and what every
    local deployment sets -- was invisible and the command refused to run."""
    path = str(tmp_path / "fromenv.sqlite")
    monkeypatch.setenv("CASEBROKER_DB", path)
    assert run(["init-db"]) == 0
    code = run(["account", "create", "--username", "ada", "--password-stdin"],
               stdin=PW, monkeypatch=monkeypatch)
    assert code == 0
    assert db.get_user(db.connect(path), "ada")["role"] == "admin"
    assert "using $CASEBROKER_DB" in capsys.readouterr().err
