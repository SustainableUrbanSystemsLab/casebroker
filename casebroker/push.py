"""Browser push notifications: the campaign tells a phone or a laptop what happened,
with no dashboard tab open.

The dashboard's own notifications (static/dashboard.html, "Finished-case
notifications") fire only while a tab is open, and say so. Reaching a CLOSED tab
takes four things, and this module is the broker's share of them:

- a service worker (static/sw.js, served at /sw.js), which the browser wakes when a
  message arrives and which shows it;
- the Push API, which hands the page a SUBSCRIPTION -- a URL at the browser
  vendor's push service plus two keys -- that the page posts here;
- a VAPID key pair (RFC 8292): each message is signed with it, and the push service
  carries a message only for the server the browser subscribed to;
- a broker that POSTs to that URL, encrypted to those keys (RFC 8291). pywebpush
  does the cryptography.

What can be announced is a catalog (EVENTS). Each kind has a broker-wide switch an
admin sets (``PUT /v1/push/policy``, kept in the settings table) and each browser
keeps its own list; ``CASEBROKER_PUSH=0`` switches the whole thing off. Two sources
feed it:

- the ``events`` table, read after a cursor. Every transition db.py records is
  written there inside the transaction that made it true, so reading it back
  announces only what committed -- the design of the ntfy notifier 0.9.0 ran,
  including the compare-and-set claim that keeps two processes overlapping in a
  deploy from both announcing one event;
- standing CONDITIONS judged every tick -- a machine gone silent holding a case,
  the part store nearly full, nothing finishing, an empty queue -- each announced
  once when it becomes true and re-armed only once it has cleared, so a condition
  that lasts a day is one notice, not 2,880.

``tick()`` is the whole behaviour, pure up to the database and the ``send`` it is
handed, which is what tests/test_push.py drives. ``Notifier`` only runs it on a
timer, in a daemon thread the app starts with itself -- never at import and never
under test unless a test asks for one.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

from pydantic import BaseModel, Field

from . import db, places

# -- configuration ---------------------------------------------------------------

#: The VAPID ``sub`` claim is a contact for the push service's operators, mailto: or
#: https: (RFC 8292), and Apple refuses one naming localhost (BadJwtToken). Unless
#: CASEBROKER_VAPID_SUBJECT says otherwise it is the dashboard's own public origin,
#: learned from the first browser that subscribes from https (a broker cannot know
#: its public address otherwise: behind Cloudflare and a proxy it binds loopback).
#: Before any has, this -- the project's page, not a made-up address.
DEFAULT_SUBJECT = "https://github.com/SustainableUrbanSystemsLab/casebroker"
_ORIGIN = re.compile(r"https://[a-z0-9-]+(\.[a-z0-9-]+)+")

INTERVAL_SECONDS = 30
#: A row is read only once it is this old: ids come from a sequence, and a row whose
#: transaction commits a moment late can land with a lower id than one already
#: visible. db.push_events_after stops at the first row younger than this.
LAG_SECONDS = 10
#: Older than this and an event is history, not news: a broker that was down for a
#: day, or had push switched off for a month, does not wake everyone with the backlog.
MAX_AGE_SECONDS = 3600
#: Sends in a row a push service may refuse before a subscription is dropped. One
#: refusal is a blip; ten, at most a few hours apart, is a browser that is gone in a
#: way the service did not spell as 404/410.
DROP_AFTER = 10
#: How long a push service keeps a message for a device that is offline. A laptop
#: closed overnight still hears that the queue ran dry; a phone off for a weekend is
#: not handed Friday's case list on Monday -- the dashboard has it.
DEFAULT_TTL = 6 * 3600
#: How often the part store is measured: usage() stats every object in it (35 parts
#: a case, so ~175,000 files at 5,000 cases), and a store fills over hours.
STORE_EVERY_SECONDS = 600
STORE_FIRE, STORE_CLEAR = 0.90, 0.85


def enabled() -> bool:
    """False when CASEBROKER_PUSH says off. On by default: nothing is sent until a
    browser subscribes, so an unconfigured broker costs a query per tick."""
    return os.environ.get("CASEBROKER_PUSH", "").strip().lower() not in ("0", "false", "no", "off")


def _number(name: str, default: float, low: float) -> float:
    try:
        return max(low, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def silent_seconds() -> int:
    """CASEBROKER_PUSH_SILENT_MINUTES, default 20: four heartbeats (300 s apart) missed,
    and past the 900 s lease, so a node that merely paused does not trip it."""
    return int(_number("CASEBROKER_PUSH_SILENT_MINUTES", 20, 1) * 60)


def stall_seconds() -> int:
    """CASEBROKER_PUSH_STALL_HOURS, default 6. A wind case is hours of solve on a
    fleet node, so a fleet that finishes nothing for six is not just between cases."""
    return int(_number("CASEBROKER_PUSH_STALL_HOURS", 6, 0.25) * 3600)


_warned: set[str] = set()


def origin_subject(origin: str | None) -> str | None:
    """``origin`` when it is a public https origin a push service would accept as a
    subject -- a dotted host name, no port, nothing local -- else None."""
    o = (origin or "").strip().rstrip("/").lower()
    if not _ORIGIN.fullmatch(o) or "localhost" in o or o.endswith((".local", ".test", ".internal", ".lan")):
        return None
    if o[len("https://"):].replace(".", "").isdigit():
        return None                          # an IPv4 address is not a contact
    return o


def subject(conn=None) -> str:
    """CASEBROKER_VAPID_SUBJECT; else the dashboard's learned origin; else DEFAULT_SUBJECT."""
    raw = os.environ.get("CASEBROKER_VAPID_SUBJECT", "").strip()
    if raw and raw.startswith(("mailto:", "https://")) and "localhost" not in raw.lower():
        return raw
    if raw and raw not in _warned:
        _warned.add(raw)
        print("[push] CASEBROKER_VAPID_SUBJECT must be mailto: or https: and must not name "
              "localhost (Apple refuses it); ignoring it", file=sys.stderr)
    if conn is not None:
        learned = origin_subject(db.push_setting(conn, db.PUSH_ORIGIN_KEY))
        if learned:
            return learned
    return DEFAULT_SUBJECT


# -- the catalog ---------------------------------------------------------------------

@dataclass(frozen=True)
class Kind:
    key: str
    label: str                 # the checkbox
    help: str                  # one line beside it; {silent_min} {stall_h} {burst_n} {burst_min}
    default: bool              # on for a browser that subscribes without choosing
    admin_only: bool = False
    urgency: str = "normal"    # RFC 8030: "high" may wake a phone in power saving
    ttl: int = DEFAULT_TTL
    many: str = ""             # the title of a batch: "{n} cases finished"
    many_url: str = "/"
    # False: a second notice of the kind updates the one on screen without a sound.
    renotify: bool = True


EVENTS: tuple[Kind, ...] = (
    Kind("pair_request", "a machine asks to join",
         "approve or deny it under Settings > Machines; the request lapses in 10 minutes",
         True, admin_only=True, urgency="high", ttl=db.PAIRING_TTL_SECONDS,
         many="{n} machines ask to join", many_url="/#settings=machines"),
    # Batched and silent after the first: the count on screen adds up (sw.js) until
    # it is dismissed, and the next finish after that makes a sound again.
    Kind("case_done", "cases finish",
         "one notice per half minute at most; the count adds up until you dismiss it",
         True, many="{n} cases finished", many_url="/#state=done", renotify=False),
    Kind("case_quarantined", "a case is quarantined",
         "its attempts ran out, or a node called the site broken; a sweep an operator ran is not announced",
         True, many="{n} cases quarantined", many_url="/#state=quarantined"),
    Kind("worker_drained", "the broker drains a failing machine",
         "{burst_n} cases failed on one machine within {burst_min} min: it gets no new case until undrained",
         True, urgency="high", many="{n} machines drained"),
    # Off by default: a cluster job between allocations, or a preempted one, is
    # silent by design while its case waits for it -- on a PACE chain that is every
    # gap. Whoever watches workstations turns it on.
    Kind("worker_silent", "a machine holding a case goes silent",
         "not heard from for {silent_min} min (CASEBROKER_PUSH_SILENT_MINUTES)",
         False, many="{n} machines silent", many_url="/#state=leased"),
    Kind("update_failed", "a node's update fails",
         "it tried a new build, could not start it, and went back to the one before",
         True, urgency="high", many="{n} node updates failed", many_url="/#settings=machines"),
    Kind("store_nearly_full", "the part store is nearly full",
         "90% of its size limit, or close to the free space it keeps in reserve; uploads stop there",
         True, admin_only=True, many_url="/#storage"),
    Kind("campaign_stalled", "nothing finishes for hours",
         "cases are leased but none finished for {stall_h} h (CASEBROKER_PUSH_STALL_HOURS)",
         True, many_url="/#state=leased"),
    Kind("queue_empty", "the queue runs dry", "no case is pending any more", True),
)
CATALOG: dict[str, Kind] = {k.key: k for k in EVENTS}
_ORDER = {k.key: i for i, k in enumerate(EVENTS)}

#: events-table names -> notice kinds. db.py writes each of these already except
#: pair-request, which create_pairing records for this.
_FROM_EVENT = {"done": "case_done", "quarantined": "case_quarantined", "drain": "worker_drained",
               "update-failed": "update_failed", "pair-request": "pair_request"}
#: Quarantines an operator caused on purpose -- the land audit, a re-spec -- move
#: hundreds of cases at once, and the person who clicked does not need telling.
_SWEEPS = ("not on land:", "moved to ")
_BROKER_DRAIN = ": drained by the broker: "


def _help(k: Kind) -> str:
    return k.help.format(silent_min=silent_seconds() // 60, stall_h=f"{stall_seconds() / 3600:g}",
                         burst_n=db.FAIL_BURST_CASES, burst_min=max(1, db.FAIL_BURST_SECONDS // 60))


def policy(conn) -> dict[str, bool]:
    """The broker-wide switch per kind. Every kind starts ON here -- this is what the
    broker is willing to send; what a browser gets by default is ``Kind.default``."""
    out = {k.key: True for k in EVENTS}
    try:
        stored = json.loads(db.push_setting(conn, db.PUSH_POLICY_KEY) or "{}")
    except ValueError:
        stored = {}
    if isinstance(stored, dict):
        out.update({k: bool(v) for k, v in stored.items() if k in CATALOG})
    return out


def catalog(conn, role: str | None) -> list[dict[str, Any]]:
    on = policy(conn)
    return [{"key": k.key, "label": k.label, "help": _help(k), "default": k.default,
             "admin_only": k.admin_only, "broker": on[k.key],
             "allowed": not k.admin_only or role == "admin"} for k in EVENTS]


def events_for(requested: list[str] | None, role: str | None) -> list[str]:
    """What a subscription may hold: the kinds asked for (the defaults when none
    were), in catalog order, without the admin-only ones for anyone else -- dropped
    silently, since the page offers them only to admins anyway."""
    want = {k.key for k in EVENTS if k.default} if requested is None else set(requested)
    return [k.key for k in EVENTS if k.key in want and (not k.admin_only or role == "admin")]


# -- what a browser may subscribe with ---------------------------------------------

#: The push services browsers actually use. The broker POSTs to whatever endpoint a
#: subscription names, from inside the server's network, so an endpoint is a URL any
#: READER can make this host request -- the router's admin page, the database's
#: port, a cloud metadata address. Only https on these hosts is accepted: Chrome,
#: Edge, Opera, Brave and Samsung Internet use FCM; Firefox, Mozilla's autopush;
#: Safari (macOS, and iOS 16.4+ from the Home Screen), Apple's; legacy Edge and
#: Windows, WNS. CASEBROKER_PUSH_HOSTS adds more ("push.example.org,*.example.net").
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com",
              "*.push.services.mozilla.com", "web.push.apple.com", "*.push.apple.com",
              "*.notify.windows.com")
ENDPOINT_MAX = 2048


def _hosts() -> tuple[str, ...]:
    extra = os.environ.get("CASEBROKER_PUSH_HOSTS", "")
    return PUSH_HOSTS + tuple(h.strip().lower() for h in extra.split(",") if h.strip())


def _host_allowed(host: str) -> bool:
    for pat in _hosts():
        if pat.startswith("*."):
            if host.endswith(pat[1:]) and len(host) > len(pat) - 1:
                return True
        elif host == pat:
            return True
    return False


def check_endpoint(endpoint: str) -> str | None:
    """Why the broker will not POST to this endpoint, or None."""
    if not endpoint or len(endpoint) > ENDPOINT_MAX or any(c.isspace() or ord(c) < 32 for c in endpoint):
        return "not a push endpoint"
    try:
        u = urllib.parse.urlsplit(endpoint)
        port = u.port
    except ValueError:
        return "not a push endpoint"
    if u.scheme != "https":
        return "a push endpoint is https"
    if u.username or u.password or port not in (None, 443) or not u.hostname:
        return "not a push endpoint"
    host = u.hostname.lower().rstrip(".")
    if not _host_allowed(host):
        return (f"{host} is not a push service this broker sends to (FCM, Mozilla, Apple, WNS; "
                "CASEBROKER_PUSH_HOSTS adds more)")
    return None


def _b64decode(s: str) -> bytes | None:
    try:
        t = s.strip().replace("+", "-").replace("/", "_").rstrip("=")
        return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))
    except (binascii.Error, ValueError):
        return None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def check_keys(p256dh: str, auth: str) -> str | None:
    """Why these are not a browser's subscription keys, or None: p256dh is an
    uncompressed P-256 point (65 bytes, 0x04 first), auth a 16-byte secret."""
    p, a = _b64decode(p256dh), _b64decode(auth)
    if p is None or len(p) != 65 or p[0] != 4:
        return "keys.p256dh is not a P-256 public key"
    if a is None or len(a) != 16:
        return "keys.auth is not a 16-byte secret"
    return None


class _Keys(BaseModel):
    p256dh: str = Field(min_length=40, max_length=200)
    auth: str = Field(min_length=16, max_length=64)


class _Subscription(BaseModel):
    """PushSubscription.toJSON(), as the browser hands it over."""
    endpoint: str = Field(min_length=12, max_length=ENDPOINT_MAX)
    keys: _Keys


class SubscribeIn(BaseModel):
    subscription: _Subscription
    events: list[str] | None = Field(default=None, max_length=64)
    # The subscription this one replaces, when the browser rotated it
    # (pushsubscriptionchange in sw.js): its kinds carry over and it is dropped.
    replaces: str | None = Field(default=None, max_length=ENDPOINT_MAX)


class EventsIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=ENDPOINT_MAX)
    events: list[str] = Field(max_length=64)


class EndpointIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=ENDPOINT_MAX)


class PolicyIn(BaseModel):
    events: dict[str, bool] = Field(max_length=64)


# -- VAPID ---------------------------------------------------------------------

_VAPID: dict[str, Any] = {}


def _generate_private() -> str:
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    return _b64(key.private_numbers().private_value.to_bytes(32, "big"))


def vapid(conn):
    """This broker's VAPID key (a py_vapid object): CASEBROKER_VAPID_PRIVATE_KEY when
    set -- the raw base64url form web-push tools print, or a PEM -- else one made on
    first use and kept in the settings table, so it survives a redeploy (a new key
    would orphan every subscribed browser). Raises when it cannot be had."""
    raw = os.environ.get("CASEBROKER_VAPID_PRIVATE_KEY", "").strip().replace("\\n", "\n")
    if not raw:
        raw = db.push_vapid_key(conn, _generate_private)
    if raw in _VAPID:
        return _VAPID[raw]
    from py_vapid import Vapid
    key = Vapid.from_pem(raw.encode()) if raw.startswith("-----BEGIN") else Vapid.from_string(raw)
    _VAPID.clear()
    _VAPID[raw] = key
    return key


def public_key(conn) -> str:
    """The applicationServerKey a page subscribes with: the uncompressed point, base64url."""
    from cryptography.hazmat.primitives import serialization
    return _b64(vapid(conn).public_key.public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint))


def key_state(conn) -> dict[str, Any]:
    """``{enabled, public_key, reason}`` for GET /v1/push/key and the page."""
    if not enabled():
        return {"enabled": False, "public_key": None,
                "reason": "push notifications are switched off on this broker (CASEBROKER_PUSH=0)"}
    try:
        return {"enabled": True, "public_key": public_key(conn), "reason": None}
    except ImportError:
        return {"enabled": False, "public_key": None, "reason": "pywebpush is not installed on this broker"}
    except Exception as e:                                # noqa: BLE001
        print(f"[push] no usable VAPID key: {type(e).__name__}", file=sys.stderr)
        return {"enabled": False, "public_key": None,
                "reason": "this broker has no usable VAPID key (check CASEBROKER_VAPID_PRIVATE_KEY)"}


# -- sending ---------------------------------------------------------------------

class Delivery(NamedTuple):
    """What a push service said: an HTTP status, or None when it was not reached;
    ``error`` is None on success. Coarse on purpose: it is shown to the person who
    subscribed and kept in last_error, and must never carry the endpoint or keys."""
    status: int | None
    error: str | None = None


def _origin(endpoint: str) -> str:
    u = urllib.parse.urlsplit(endpoint)
    return f"{u.scheme}://{u.netloc}"


def make_sender(conn, timeout: float = 10.0) -> Callable[..., Delivery]:
    """A ``send(sub, payload, ttl=, urgency=)`` over pywebpush with this broker's key."""
    key = vapid(conn)
    sub_claim = subject(conn)
    # subject() has already held it to mailto:/https: and no localhost; py_vapid's
    # own check would also refuse an https URL with a path, which RFC 8292 allows.
    key.conf["no-strict"] = True

    def send(sub: dict[str, Any], payload: dict[str, Any], *, ttl: int = DEFAULT_TTL,
             urgency: str = "normal") -> Delivery:
        import requests
        from pywebpush import WebPusher
        endpoint = sub["endpoint"]
        # Checked again here, not only at subscribe: a row from before a host was
        # dropped from the list, or written by hand, must not reach the network.
        if check_endpoint(endpoint):
            return Delivery(None, "not a push service this broker sends to")
        headers = dict(key.sign({"sub": sub_claim, "aud": _origin(endpoint),
                                 "exp": int(time.time()) + 12 * 3600}))
        headers["Urgency"] = urgency
        session = requests.Session()
        # A push service answers 201; a redirect is not something to follow from
        # inside the server's network to wherever it points.
        session.max_redirects = 0
        try:
            resp = WebPusher({"endpoint": endpoint, "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                             requests_session=session).send(
                json.dumps(payload, separators=(",", ":")), headers, ttl=ttl, timeout=timeout)
        except requests.TooManyRedirects:
            return Delivery(None, "the push service answered with a redirect; not followed")
        except requests.Timeout:
            return Delivery(None, "timed out")
        except requests.RequestException:
            return Delivery(None, "could not reach the push service")
        except Exception as e:                            # noqa: BLE001
            return Delivery(None, f"could not encrypt or send ({type(e).__name__})")
        finally:
            session.close()
        if 200 <= resp.status_code < 300:
            return Delivery(resp.status_code)
        # Apple and FCM say why in a short body ({"reason":"BadJwtToken"}), which is
        # what tells a wrong subject or key from a browser that is gone.
        said = " ".join((resp.text or "").split())[:120]
        return Delivery(resp.status_code, f"the push service answered {resp.status_code}"
                        + (f": {said}" if said else ""))

    return send


def _normal(result: Any) -> Delivery:
    if isinstance(result, Delivery):
        return result
    if isinstance(result, int):
        return Delivery(result, None if 200 <= result < 300 else f"the push service answered {result}")
    return Delivery(None, "no answer")


def _deliver_one(conn, send, sub, payload, ttl, urgency, now) -> str:
    try:
        d = _normal(send(sub, payload, ttl=ttl, urgency=urgency))
    except Exception as e:                                # noqa: BLE001
        d = Delivery(None, f"could not send ({type(e).__name__})")
    gone = d.status in (404, 410)
    outcome = db.note_push_delivery(conn, sub["endpoint"], error=d.error, gone=gone,
                                    drop_after=DROP_AFTER, now=now)
    if d.error:
        # The host only: the rest of an endpoint is the browser's address.
        host = urllib.parse.urlsplit(sub["endpoint"]).hostname
        print(f"[push] {payload.get('kind')} to {host} ({sub['subscriber_kind']}:{sub['subscriber']}): "
              f"{d.error}{' -- subscription dropped' if outcome == 'dropped' else ''}", file=sys.stderr)
    return outcome


def default_role_of(conn, sub: dict[str, Any]) -> str | None:
    """The subscriber's role NOW (None: the credential is gone). Accounts, share links
    and machine credentials are looked up; anything else keeps the role it had,
    and app.py checks the environment's tokens before calling this."""
    if sub["subscriber_kind"] in ("user", "share", "machine"):
        return db.push_subscriber_role(conn, sub["subscriber_kind"], sub["subscriber"])
    return sub.get("role")


# -- notices ---------------------------------------------------------------------

@dataclass
class Notice:
    kind: str
    title: str
    body: str
    url: str
    item: str | None = None        # what a batch lists: a case id, a machine


def _line(text: Any, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _listed(items: list[str], n: int) -> str:
    shown = items[:3]
    return ", ".join(shown) + (f" and {n - len(shown)} more" if n > len(shown) else "")


def _case_url(case_id: str) -> str:
    return "/#case=" + urllib.parse.quote(case_id, safe="")


def _where(spec: Any) -> str:
    """"near Nanjing, China", from the case's own coordinates (places.py, offline)."""
    try:
        s = json.loads(spec) if isinstance(spec, str) else (spec or {})
        p = places.locate(float(s["lat"]), float(s["lon"]))
    except Exception:                                     # noqa: BLE001 -- a place is a nicety
        return ""
    if not p.get("country"):
        return ""
    town = p.get("town")
    return (f"near {town}, " if town and (p.get("town_km") or 1e9) <= 25 else "") + p["country"]


def _from_event(row: dict[str, Any]) -> Notice | None:
    kind = _FROM_EVENT.get(row["event"])
    detail = row.get("detail") or ""
    case, worker = row.get("case_id"), row.get("worker_id")
    if kind == "case_done" and case:
        body = " · ".join(x for x in (case, _where(row.get("spec")), worker) if x)
        return Notice(kind, "Case finished", body, _case_url(case), case)
    if kind == "case_quarantined" and case:
        if detail.startswith(_SWEEPS):
            return None
        body = " · ".join(x for x in (case, _line(detail, 160), worker) if x)
        return Notice(kind, "Case quarantined", body, _case_url(case), case)
    if kind == "worker_drained":
        # The broker's own drain is written with no worker and names the rule; an
        # operator's (worker_id = who clicked) is not news to the operator.
        if worker or _BROKER_DRAIN not in detail:
            return None
        who, why = detail.split(_BROKER_DRAIN, 1)
        return Notice(kind, f"Machine drained: {_line(who, 64)}", _line(why, 300), "/", _line(who, 64))
    if kind == "update_failed" and worker:
        build, _, why = detail.partition(": ")
        return Notice(kind, f"Update failed on {_line(worker, 64)}",
                      _line(f"Tried {build} and went back to the build before: {why or 'it could not be started'}", 300),
                      "/#settings=machines", worker)
    if kind == "pair_request" and worker:
        lead = f"{_line(detail, 120)}. " if detail else ""
        return Notice(kind, f"{_line(worker, 64)} asks to join",
                      lead + "Approve or deny it under Settings > Machines; the request lapses in 10 minutes.",
                      "/#settings=machines", worker)
    return None


def _size(n: float) -> str:
    for unit, scale in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{int(n)} B"


def _ago(seconds: float) -> str:
    if seconds >= 2 * 86400:
        return f"{seconds / 86400:.0f} d"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f} h"
    return f"{max(1, round(seconds / 60))} min"


def _store_levels(u: Any) -> tuple[float, float]:
    used = u.bytes + u.incoming_bytes
    by_limit = used / u.max_bytes if u.max_bytes else 0.0
    by_reserve = 0.0
    if u.reserve_bytes:
        by_reserve = u.reserve_bytes / u.free_bytes if u.free_bytes > 0 else float("inf")
    return by_limit, by_reserve


def store_level(u: Any) -> float:
    """How close the part store is to refusing uploads, as one number: its share of
    CASEBROKER_PARTS_MAX_GB, or reserve / free space -- 0.9 when the volume's free
    space is down to 1.11x the reserve the store never eats into (PartStore.admit
    refuses past either). The larger of the two."""
    return max(_store_levels(u))


def _store_notice(u: Any) -> Notice:
    used = u.bytes + u.incoming_bytes
    by_limit, by_reserve = _store_levels(u)
    if by_limit >= by_reserve:
        body = (f"{_size(used)} of its {_size(u.max_bytes)} limit ({100 * by_limit:.0f}%). "
                "Past it a node is told 507 and keeps its parts.")
    else:
        body = (f"{_size(u.free_bytes)} free on its volume, and uploads stop at the "
                f"{_size(u.reserve_bytes)} it keeps in reserve.")
    return Notice("store_nearly_full", "Part store nearly full", body, "/#storage")


def _conditions(snap: dict[str, Any], now: int, usage: Any, state: dict[str, Any]
                ) -> tuple[list[Notice], dict[str, Any]]:
    """The standing conditions: what is newly true, and the state that remembers what
    has been announced. A condition fires once and re-arms only after it clears."""
    notices: list[Notice] = []
    by_state = snap["by_state"]

    # A machine holding a case and not heard from. Keyed per machine AND case, so a
    # machine that comes back (or whose case is reclaimed) re-arms.
    seen = set(state.get("silent") or [])
    current = []
    for r in snap["silent"]:
        key = f"{r['worker_id']}\t{r['case_id']}"
        current.append(key)
        if key not in seen:
            quiet = _ago(now - int(r["last_seen"] or now))
            notices.append(Notice("worker_silent", f"Machine silent: {_line(r['worker_id'], 64)}",
                                  f"Not heard from for {quiet} while holding {r['case_id']}.",
                                  _case_url(r["case_id"]), r["worker_id"]))
    out: dict[str, Any] = {"silent": sorted(current)[:500]}

    # Hysteresis: fired at 90%, re-armed below 85%, so a store hovering at the line
    # is one notice and not one per measurement. Unmeasured (no store, or not this
    # tick) leaves the flag as it was.
    fired = bool(state.get("store"))
    if usage is not None:
        level = store_level(usage)
        if not fired and level >= STORE_FIRE:
            notices.append(_store_notice(usage))
            fired = True
        elif fired and level < STORE_CLEAR:
            fired = False
    out["store"] = fired

    # Leased work and nothing finished: measured from the later of the last finish
    # and the oldest lease now running, so a fleet that started an hour ago after a
    # quiet week is not "stalled for a week".
    leased = by_state.get("leased", 0)
    stalled = False
    if leased and snap["oldest_lease"] is not None:
        since = max(snap["last_done"] or 0, snap["oldest_lease"])
        stalled = now - since >= stall_seconds()
        if stalled and not state.get("stall"):
            last = (f"the last one finished {_ago(now - snap['last_done'])} ago"
                    if snap["last_done"] else "none has finished yet")
            notices.append(Notice("campaign_stalled", f"Nothing finished for {_ago(now - since)}",
                                  f"{leased:,} case{'s' if leased != 1 else ''} leased; {last}.",
                                  "/#state=leased"))
    out["stall"] = stalled

    total, pending = sum(by_state.values()), by_state.get("pending", 0)
    empty = bool(state.get("empty"))
    if total and not pending:
        if not empty:
            notices.append(Notice("queue_empty", "Queue empty",
                                  f"No case is pending. {leased:,} leased, {by_state.get('done', 0):,} done, "
                                  f"{by_state.get('quarantined', 0):,} quarantined.", "/"))
        empty = True
    elif pending:
        empty = False
    out["empty"] = empty
    return notices, out


def compose(kind: str, items: list[Notice], now: int, admin: bool) -> dict[str, Any]:
    """One push for one kind: the notice itself, or a batch ("3 cases finished:
    a, b, c"). ``items`` and ``many`` let sw.js fold it into a notice of the same
    kind still on screen; ``tag`` makes it replace that notice rather than stack.
    case_done shares its tag with the tab's own finished-case notification, so a
    browser with both on sees one."""
    k = CATALOG[kind]
    n = len(items)
    listed = [i.item for i in items if i.item][:20]
    if n == 1:
        title, body, url = items[0].title, items[0].body, items[0].url
    else:
        title = k.many.format(n=f"{n:,}") if k.many else items[-1].title
        body = _listed(listed, n) if listed else items[-1].body
        url = k.many_url
    if kind == "update_failed" and not admin:
        url = "/"        # Settings > Machines is an admin's tab
    payload = {"kind": kind, "tag": "casebroker-done" if kind == "case_done" else f"casebroker-{kind}",
               "title": _line(title, 120), "body": _line(body, 600), "url": url, "ts": now,
               "count": n, "items": listed, "renotify": k.renotify}
    if k.many and listed:
        payload["many"] = {"title": k.many, "url": k.many_url}
    return payload


def deliver(conn, notices: list[Notice], now: int, *, send=None, role_of=None) -> dict[str, int]:
    """Send ``notices`` to every subscription that wants them: one push per kind per
    subscription. The subscriber's role is checked NOW, not as it was when they
    subscribed, and a subscription whose credential is gone is dropped."""
    counts = {"sent": 0, "failed": 0, "dropped": 0}
    subs = db.push_subscriptions(conn)
    if not subs or not notices:
        return counts
    on = policy(conn)
    groups: dict[str, list[Notice]] = {}
    for n in sorted(notices, key=lambda n: _ORDER[n.kind]):
        groups.setdefault(n.kind, []).append(n)
    role_of = role_of or (lambda s: default_role_of(conn, s))
    if send is None:
        send = make_sender(conn)
    for sub in subs:
        role = role_of(sub)
        if role is None:
            db.delete_push_subscription(conn, sub["endpoint"])
            counts["dropped"] += 1
            continue
        want = set(sub["events"])
        for kind, items in groups.items():
            k = CATALOG[kind]
            if kind not in want or not on[kind] or (k.admin_only and role != "admin"):
                continue
            outcome = _deliver_one(conn, send, sub, compose(kind, items, now, role == "admin"),
                                   k.ttl, k.urgency, now)
            counts[outcome] += 1
            if outcome == "dropped":
                break
    return counts


def _load_state(raw: str | None) -> dict[str, Any]:
    try:
        st = json.loads(raw) if raw else {}
    except ValueError:
        st = {}
    return st if isinstance(st, dict) else {}


def tick(conn, now: int | None = None, *, send=None, role_of=None,
         store_usage: Callable[[], Any] | None = None) -> dict[str, Any]:
    """One pass: read the events since the cursor, judge the standing conditions,
    claim the new state, deliver. Returns what it did, for a test or a log line.

    The state is CLAIMED before anything is sent: a process that loses the
    compare-and-set to another sends nothing, so an event is announced at most once.
    A push service that is down loses that notice; it does not repeat it later."""
    now = int(now or time.time())
    out: dict[str, Any] = {"notices": [], "sent": 0, "failed": 0, "dropped": 0}
    if not enabled():
        out["skipped"] = "switched off"
        return out
    raw = db.push_setting(conn, db.PUSH_STATE_KEY)
    state = _load_state(raw)
    snap = db.push_snapshot(conn, now - silent_seconds())
    notices: list[Notice] = []
    if not isinstance(state.get("cursor"), int):
        # First run: from now on. The campaign's history is not news.
        cursor = snap["newest_event"]
    else:
        rows, cursor = db.push_events_after(conn, state["cursor"], list(_FROM_EVENT), before=now - LAG_SECONDS,
                                            not_before=now - MAX_AGE_SECONDS)
        notices += [n for n in map(_from_event, rows) if n is not None]
    usage = None
    if store_usage is not None:
        try:
            usage = store_usage()
        except Exception as e:                            # noqa: BLE001
            print(f"[push] could not measure the part store: {e}", file=sys.stderr)
    found, conditions = _conditions(snap, now, usage, state)
    notices += found
    new_raw = json.dumps({"cursor": cursor, **conditions}, sort_keys=True, separators=(",", ":"))
    if new_raw != raw and not db.claim_push_state(conn, raw, new_raw, now):
        out["skipped"] = "another process claimed this tick"
        return out
    out["notices"] = [n.kind for n in sorted(notices, key=lambda n: _ORDER[n.kind])]
    if notices:
        out.update(deliver(conn, notices, now, send=send, role_of=role_of))
    return out


def send_test(conn, sub: dict[str, Any], *, send=None, now: int | None = None) -> dict[str, Any]:
    """One push now, to one subscription: the page's "Send a test". Sent in the
    request, unlike everything else, because the answer -- did the push service take
    it, and if not what it said -- is the whole point of the button."""
    now = int(now or time.time())
    labels = [CATALOG[e].label for e in sub["events"] if e in CATALOG]
    payload = {"kind": "test", "tag": "casebroker-test", "title": "Test from the case broker",
               "body": "Push works on this device. " + (f"You will be told when {', '.join(labels)}."
                                                         if labels else "No kind of notice is chosen yet."),
               "url": "/#settings=prefs", "ts": now, "count": 1, "items": [], "renotify": True}
    try:
        d = _normal((send or make_sender(conn))(sub, payload, ttl=300, urgency="normal"))
    except Exception as e:                                # noqa: BLE001
        d = Delivery(None, f"could not send ({type(e).__name__})")
    outcome = db.note_push_delivery(conn, sub["endpoint"], error=d.error, gone=d.status in (404, 410),
                                    drop_after=DROP_AFTER, now=now)
    return {"ok": d.error is None, "status": d.status, "error": d.error, "dropped": outcome == "dropped"}


class Notifier:
    """tick() every INTERVAL_SECONDS in a daemon thread. A failed tick is logged and
    the next one runs: the notifier must never take the broker down with it."""

    def __init__(self, conn, *, interval: float = INTERVAL_SECONDS, role_of=None,
                 store_usage: Callable[[], Any] | None = None, send=None):
        self.conn, self.interval, self.role_of, self.send = conn, max(5.0, interval), role_of, send
        self._store_usage = store_usage
        self._usage: Any = None
        self._usage_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _measured_store(self) -> Any:
        if self._store_usage is None:
            return None
        if time.monotonic() - self._usage_at >= STORE_EVERY_SECONDS:
            self._usage, self._usage_at = self._store_usage(), time.monotonic()
        return self._usage

    def run_once(self) -> dict[str, Any]:
        return tick(self.conn, send=self.send, role_of=self.role_of, store_usage=self._measured_store)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception as e:                        # noqa: BLE001
                print(f"[push] tick failed: {type(e).__name__}: {e}", file=sys.stderr)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="push-notifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


#: /manifest.webmanifest. What iOS needs before it offers push at all: Safari on an
#: iPhone or iPad exposes the Push API only to a page opened from the Home Screen,
#: and a manifest with display "standalone" is what makes Add to Home Screen open
#: the dashboard as its own app rather than a bookmark into Safari.
MANIFEST = {
    "name": "E3D Simulation Broker",
    "short_name": "Case broker",
    "description": "The campaign's cases, workers and results",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#22272e",
    "theme_color": "#22272e",
}
