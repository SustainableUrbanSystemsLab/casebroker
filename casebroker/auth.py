"""Passwords, sessions and machine tokens.

Two kinds of principal, deliberately handled differently:

* A **human** is interactive. They log in with a password and get a session
  that expires. Passwords are hashed with scrypt.
* A **machine** is not. An unattended worker cannot type a password, so it
  carries a long-lived random token. What changed is that the token is now
  per-machine and lives in the database, so the dashboard can say which box
  used it and when, and revoking one machine is a row update rather than an
  environment-variable edit plus a redeploy.

Everything here is standard library. The broker deploys to a service where
adding a native dependency is a build risk, and the CLI that shares these
helpers is deliberately dependency-free so it still runs on a login node where
nobody has run `uv sync`.

Secrets are stored HASHED -- passwords, sessions and machine tokens alike. A
database dump should not hand over live credentials, and the purge tooling means
dumps get taken.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

# scrypt, not PBKDF2: it is memory-hard, so a stolen hash costs an attacker RAM
# as well as time. These are the parameters RFC 7914 suggests for interactive
# logins (~100 ms, 16 MB); n must be a power of two.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
DKLEN = 32

# 32 bytes of urandom -> 43 url-safe characters. Same strength as the tokens the
# CLI has always generated.
TOKEN_BYTES = 32

SESSION_TTL_SECONDS = 14 * 24 * 3600      # a fortnight; a lab laptop, not a bank


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, all base64url. Self-describing on purpose: the
    parameters travel with the hash, so they can be raised later without
    invalidating everyone's password."""
    if not password:
        raise ValueError("empty password")
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a hash produced by `hash_password`.

    Returns False rather than raising on a malformed hash: a corrupt row must
    fail the login, not 500 the endpoint and reveal that the row exists.
    """
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        salt, expected = unb64(salt_b64), unb64(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError, KeyError):
        return False
    return hmac.compare_digest(dk, expected)


def new_token() -> str:
    """A fresh credential. Shown to the operator once and never stored in the
    clear -- only `token_hash` goes to the database."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 of a machine token or session id.

    Plain SHA-256, not scrypt, and the difference matters: these are 256-bit
    random strings, not human-chosen passwords, so there is no dictionary to
    attack and no work factor to buy. Using scrypt here would add ~100 ms to
    EVERY authenticated request -- every lease, every heartbeat -- for no gain.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# A hash no password verifies against, built ONCE at import.
#
# The login path must run the same work for a username that exists and one that
# does not, or response time enumerates accounts. It did that by calling
# `hash_password("decoy")` per request -- but that is itself a full scrypt, so a
# miss ran scrypt TWICE and a hit ran it once. Measured at exactly 2.0x: the
# defence against enumeration was the thing enumerating, with the sign flipped.
# It also doubled the cost of the most expensive unauthenticated operation the
# service has.
#
# Built from random bytes rather than a fixed string so that nothing an attacker
# can send verifies against it even in principle.
DECOY_HASH = hash_password(secrets.token_urlsafe(32))


def session_expiry(now: int | None = None, ttl: int = SESSION_TTL_SECONDS) -> int:
    return int(now if now is not None else time.time()) + ttl
