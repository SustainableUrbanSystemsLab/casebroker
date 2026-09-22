"""Push notifications for the campaign: a case started, began meshing, began
solving, finished, failed or was quarantined -- delivered by ntfy
(https://ntfy.sh), so they reach a phone or a closed laptop, which the
dashboard's in-tab browser notifications cannot.

Configured by an admin in the dashboard (Settings -> Preferences -> Push
notifications, stored in the settings table) or by environment variables,
which each field falls back to:

  CASEBROKER_NOTIFY_URL            the ntfy topic URL; on ntfy.sh the topic
                                   name IS the secret
  CASEBROKER_NOTIFY_TOKEN          bearer token, for a protected topic
  CASEBROKER_NOTIFY_EVENTS         started,meshing,solving,done,failed,quarantined
  CASEBROKER_NOTIFY_INTERVAL       seconds between polls, default 30
  CASEBROKER_PUBLIC_URL            the dashboard, for a tap-to-open link
  CASEBROKER_NOTIFY_ALLOWED_HOSTS  extra hosts a URL set IN THE DASHBOARD may
                                   point at (ntfy.sh always may); an env URL is
                                   the operator's own and is not restricted

Why a poller over the events table rather than a call in each route: every one
of these moments is already written there, inside the transaction that makes it
true, so reading it back announces only what committed. One message per poll
however many cases moved, which keeps a busy fleet inside ntfy.sh's daily
allowance for a free topic; network I/O never happens under db._LOCK.

Delivery, after an adversarial review (2026-09-22):
- The position is kept in the settings table and CLAIMED with a compare-and-set
  before a batch is sent, so a restart resumes instead of dropping what happened
  in between, and two instances overlapping in a deploy never both send a range.
  A process that has been away a long time jumps to now rather than replaying.
- Rows are read with a few seconds' lag, so a row committed late by the other
  instance of a deploy (a lower sequence id landing after a higher one) is seen.
- A transient failure (timeout, refused, 5xx, 429 with its Retry-After) keeps
  the composed message and retries it with backoff for up to 30 minutes; a
  permanent one (any other 4xx, a redirect, a bad URL) is logged and dropped.
  Either way the cursor moves on -- one bad batch must not silence the notifier.
- The body stays under ntfy's 4,096-byte message limit (above it ntfy.sh turns
  the message into a file attachment, and a self-hosted server may refuse it).
- No redirects are followed, the response is read at most 4 KiB, a token is
  only ever sent with the URL it was configured for, and a URL set from the
  dashboard may only point at an allowed host.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import db, places

log = logging.getLogger("casebroker.notify")

ALL_EVENTS = ("started", "meshing", "solving", "done", "failed", "quarantined")
# Emoji tags ntfy renders in front of the title; the order is the order of the
# lines in a batched message, which is the order someone wants to read them in.
_TAGS = {"quarantined": "x", "failed": "warning", "done": "white_check_mark",
         "solving": "cyclone", "meshing": "triangular_ruler", "started": "arrow_forward"}
_VERB = {"started": "started", "meshing": "meshing", "solving": "solving",
         "done": "finished", "failed": "failed (will retry)", "quarantined": "quarantined"}

BODY_LIMIT = 3800            # bytes; ntfy's message limit is 4,096
LAG_SECONDS = 5
REPLAY_MAX_ROWS = 2000       # further behind than this after a restart: start from now
RETRY_FOR_SECONDS = 1800


def _phase(detail: str | None) -> str | None:
    """"mesh 3/5 · ..." -> meshing, "solve 2/8 dirs · ..." -> solving: the node's
    own grammar (Eddy3D NodeProgress), which starts every line with its phase."""
    if not detail:
        return None
    word = detail.strip().split(" ", 1)[0].lower()
    return {"mesh": "meshing", "solve": "solving"}.get(word)


def classify(row: dict[str, Any]) -> str | None:
    """Which notification, if any, one event row is."""
    ev = row["event"]
    if ev in ("leased", "resumed"):
        return "started"
    if ev in ("done", "failed", "quarantined"):
        return ev
    if ev == "progress":
        now, before = _phase(row.get("detail")), _phase(row.get("previous_detail"))
        if now and now != before:
            return now
    return None


def _where(row: dict[str, Any]) -> str:
    try:
        spec = json.loads(row.get("spec") or "{}")
        p = places.locate(float(spec["lat"]), float(spec["lon"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return ""
    if not p.get("country"):
        return ""
    town = p.get("town")
    near = f"{town}, " if town and (p.get("town_km") or 1e9) <= 25 else ""
    return near + p["country"]


def _one_line(text: Any, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def compose(rows: list[dict[str, Any]], wanted: set[str]) -> tuple[str, str, str] | None:
    """(title, body, tags) for one batch, or None when nothing in it is wanted.
    The body is kept under BODY_LIMIT bytes: lines are added most-important
    first until the next one would not fit, then "... and N more"."""
    items = [(kind, r) for r in rows if (kind := classify(r)) and kind in wanted]
    if not items:
        return None
    order = {k: i for i, k in enumerate(_TAGS)}
    items.sort(key=lambda kr: (order[kr[0]], kr[1]["id"]))
    lines: list[str] = []
    used = 0
    for n, (kind, r) in enumerate(items):
        verb = "resumed" if r["event"] == "resumed" else _VERB[kind]
        where = _where(r)
        extra = ""
        if kind in ("failed", "quarantined") and r.get("detail"):
            extra = " -- " + _one_line(r["detail"], 140)
        line = (_one_line(r["case_id"], 40) + " " + verb + (f" · {where}" if where else "")
                + (f" · {_one_line(r['worker_id'], 60)}" if r.get("worker_id") else "") + extra)
        tail = f"... and {len(items) - n} more"
        size = len(line.encode("utf-8")) + 1
        if used + size + len(tail) + 1 > BODY_LIMIT:
            lines.append(tail)
            break
        lines.append(line)
        used += size
    counts: dict[str, int] = {}
    for kind, _ in items:
        counts[kind] = counts.get(kind, 0) + 1
    if len(items) == 1:
        kind, r = items[0]
        title = "Case " + ("resumed" if r["event"] == "resumed" else _VERB[kind])
    else:
        title = ", ".join(f"{n} {_VERB[k].split(' ')[0]}" for k, n in
                          sorted(counts.items(), key=lambda kv: order[kv[0]]))
    return title, "\n".join(lines), _TAGS[items[0][0]]


class SendError(Exception):
    """A delivery that failed. ``transient`` failures are worth retrying;
    ``retry_after`` is ntfy's own answer to a 429 when it gave one. The message is
    deliberately coarse: it is shown to an admin, and must never carry the URL,
    the token, or what an internal host answered."""

    def __init__(self, message: str, transient: bool, retry_after: float | None = None):
        super().__init__(message)
        self.transient, self.retry_after = transient, retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # noqa: D401 -- never follow
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def send_ntfy(url: str, title: str, body: str, tags: str, token: str | None = None,
              click: str | None = None, timeout: float = 10.0) -> None:
    headers = {"Title": _one_line(title, 200).encode("utf-8").decode("latin-1", "replace"),
               "Tags": tags, "Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if click:
        headers["Click"] = click
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            r.read(4096)
            if not 200 <= r.status < 300:
                raise SendError(f"ntfy answered {r.status}", transient=False)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                wait = float(e.headers.get("Retry-After") or 0) or None
            except ValueError:
                wait = None
            raise SendError("ntfy is rate-limiting this topic (429)", transient=True, retry_after=wait) from None
        if 300 <= e.code < 400:
            raise SendError(f"ntfy answered a redirect ({e.code}); not followed", transient=False) from None
        raise SendError(f"ntfy answered {e.code}", transient=e.code >= 500) from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise SendError("timed out", transient=True) from None
        if isinstance(reason, ConnectionRefusedError):
            raise SendError("connection refused", transient=True) from None
        raise SendError("could not reach the host", transient=True) from None
    except TimeoutError:
        raise SendError("timed out", transient=True) from None
    except (ValueError, UnicodeError):
        raise SendError("invalid URL or header", transient=False) from None


#: Settings-table keys (casebroker/db.py ``settings``). Each falls back to its
#: environment variable when unset there.
KEYS = {"url": "notify_url", "token": "notify_token", "events": "notify_events",
        "public_url": "notify_public_url"}
ENV = {"url": "CASEBROKER_NOTIFY_URL", "token": "CASEBROKER_NOTIFY_TOKEN",
       "events": "CASEBROKER_NOTIFY_EVENTS", "public_url": "CASEBROKER_PUBLIC_URL"}


def parse_events(raw: str | None) -> set[str]:
    if not raw:
        return set(ALL_EVENTS)
    try:
        items = json.loads(raw) if raw.strip().startswith("[") else raw.split(",")
    except ValueError:
        items = raw.split(",")
    return {str(e).strip() for e in items if str(e).strip() in ALL_EVENTS}


def allowed_hosts() -> set[str]:
    extra = os.environ.get("CASEBROKER_NOTIFY_ALLOWED_HOSTS", "")
    return {"ntfy.sh"} | {h.strip().lower() for h in extra.split(",") if h.strip()}


def check_url(url: str, *, from_dashboard: bool, with_token: bool = False) -> str | None:
    """Why ``url`` may not be used, or None. A URL an admin types into the
    dashboard may only point at an allowed host: the broker would otherwise POST
    wherever it is told from inside its own network (cloud metadata endpoints,
    loopback services) and report what came back. An env URL is the operator's
    and is trusted."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return "not a URL"
    if u.scheme not in ("https", "http") or not u.hostname or " " in url or len(url) > 500:
        return "must be an http(s) URL"
    if u.username or u.password:
        return "must not carry credentials; use the token field"
    if with_token and u.scheme != "https":
        return "a token is only sent over https"
    if from_dashboard and u.hostname.lower() not in allowed_hosts():
        return (f"{u.hostname} is not an allowed host (ntfy.sh, or add it to "
                "CASEBROKER_NOTIFY_ALLOWED_HOSTS on the server)")
    return None


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{(u.hostname or '').lower()}:{u.port or ''}"


def resolve(settings: dict[str, str]) -> dict[str, Any]:
    """The configuration in force, each field from the settings table when an
    admin set it there, else from the environment; ``source`` says which. A
    token is only paired with the URL it was configured for: the dashboard's
    token with the dashboard's URL, the env token with the env URL."""
    out: dict[str, Any] = {"source": {}}
    for field in KEYS:
        value = settings.get(KEYS[field])
        src = "settings"
        if value is None:
            value = os.environ.get(ENV[field], "").strip() or None
            src = "env" if value else None
        out[field] = value
        out["source"][field] = src
    if out["token"] and out["source"]["token"] != out["source"]["url"]:
        out["token"], out["source"]["token"] = None, None
    out["events"] = sorted(parse_events(out["events"]), key=ALL_EVENTS.index)
    return out


def mask(url: str | None) -> str | None:
    """A topic URL for display: on ntfy.sh the topic name is the only secret."""
    if not url:
        return None
    head, _, topic = url.rstrip("/").rpartition("/")
    return f"{head}/{topic[:4]}…" if len(topic) > 4 else f"{head}/…"


def valid_url(url: str) -> bool:
    return check_url(url, from_dashboard=False) is None


def usable(cfg: dict[str, Any]) -> str | None:
    """Why the configuration in force cannot send, or None."""
    if not cfg["url"]:
        return "no ntfy topic configured"
    return check_url(cfg["url"], from_dashboard=cfg["source"]["url"] == "settings",
                     with_token=bool(cfg["token"]))


class Notifier:
    """One poll loop per broker process. ``poll_once`` is the whole behaviour and
    is what the tests drive; ``start`` only runs it on a timer in a daemon thread."""

    def __init__(self, conn, url: str | None = None, *, token: str | None = None,
                 events: set[str] | None = None, interval: float = 30.0,
                 public_url: str | None = None, sender: Callable[..., None] = send_ntfy,
                 live_config: bool = False, clock: Callable[[], float] = time.time):
        self.conn, self.url, self.token = conn, url, token
        self.events = set(events or ALL_EVENTS)
        self.interval = max(5.0, interval)
        self.public_url = (public_url or "").rstrip("/") or None
        self.sender, self.live_config, self.clock = sender, live_config, clock
        self.pending: list[dict[str, Any]] = []      # composed messages awaiting a retry
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._init_cursor()

    def _init_cursor(self) -> None:
        newest = db.max_event_id(self.conn)
        saved = db.notify_cursor(self.conn)
        if saved is None:
            db.claim_notify_cursor(self.conn, None, newest)
        elif newest - saved > REPLAY_MAX_ROWS or saved > newest:
            # Away too long (or the table was reset): announcing hours of history
            # at once is noise, not news. Start from now.
            db.claim_notify_cursor(self.conn, saved, newest)

    def _config(self) -> None:
        if not self.live_config:
            return
        cfg = resolve(db.get_settings(self.conn))
        problem = usable(cfg)
        if problem and cfg["url"]:
            log.warning("push notifications not sent: %s", problem)
        self.url = None if problem else cfg["url"]
        self.token = cfg["token"]
        self.events = set(cfg["events"])
        self.public_url = (cfg["public_url"] or "").rstrip("/") or None

    def _deliver(self, msg: dict[str, Any]) -> bool:
        """True when the message is finished with (sent, or permanently failed)."""
        try:
            self.sender(self.url, msg["title"], msg["body"], msg["tags"], token=self.token, click=self.public_url)
            return True
        except SendError as e:
            now = self.clock()
            if not e.transient or now - msg["first"] > RETRY_FOR_SECONDS:
                log.warning("notification dropped: %s", e)
                return True
            msg["tries"] += 1
            msg["next"] = now + (e.retry_after or min(600.0, 30.0 * 2 ** (msg["tries"] - 1)))
            log.warning("notification not delivered (%s); retrying", e)
            return False
        except Exception as e:                  # noqa: BLE001 -- an unknown failure is not retried forever
            log.warning("notification dropped: %s", type(e).__name__)
            return True

    def poll_once(self) -> int:
        """Announce everything since the cursor; returns how many rows were consumed."""
        self._config()
        now = self.clock()
        if self.url:
            self.pending = [m for m in self.pending if not (m["next"] <= now and self._deliver(m))]
        cursor = db.notify_cursor(self.conn) or 0
        rows = db.events_after(self.conn, cursor, before_ts=int(now) - LAG_SECONDS)
        if not rows:
            return 0
        # Claim the range FIRST: an instance that loses the race sends nothing,
        # so a deploy overlap cannot double a notice.
        if not db.claim_notify_cursor(self.conn, cursor, rows[-1]["id"]):
            return 0
        if not self.url:
            return len(rows)                     # switched off: the backlog is skipped, not saved up
        try:
            msg = compose(rows, self.events)
        except Exception:                        # noqa: BLE001 -- a batch that cannot be composed is skipped
            log.exception("could not compose a notification; skipping %d events", len(rows))
            return len(rows)
        if msg:
            title, body, tags = msg
            m = {"title": title, "body": body, "tags": tags, "first": now, "tries": 0, "next": now}
            if not self._deliver(m):
                self.pending = (self.pending + [m])[-20:]
        return len(rows)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                while self.poll_once() >= 500:      # a backlog drains in consecutive batches
                    pass
            except Exception:                        # noqa: BLE001 -- keep polling; never kill the process
                log.exception("notifier poll failed")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="casebroker-notify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop, after one last poll: what happened since the previous one is
        announced by THIS process rather than left to the next to find."""
        self._stop.set()
        try:
            self.poll_once()
        except Exception:                            # noqa: BLE001 -- shutting down regardless
            log.exception("final notifier poll failed")


def from_env(conn) -> Notifier:
    """The process's notifier. Always created: whether it SENDS is decided per
    poll from the settings table and the environment, so an admin can switch it
    on from the dashboard without a redeploy."""
    try:
        interval = float(os.environ.get("CASEBROKER_NOTIFY_INTERVAL", "30"))
    except ValueError:
        interval = 30.0
    return Notifier(conn, interval=interval, live_config=True)


def send_test(settings: dict[str, str], sender: Callable[..., None] = send_ntfy) -> dict[str, Any]:
    """One message now, with the configuration in force -- the dashboard's "Send
    test" button. Errors come back coarse (see SendError), never str(exception)."""
    cfg = resolve(settings)
    problem = usable(cfg)
    if problem:
        return {"ok": False, "error": problem}
    try:
        sender(cfg["url"], "Test from the case broker",
               "Notifications work. You will be told: " + ", ".join(cfg["events"]) + ".",
               "bell", token=cfg["token"], click=(cfg["public_url"] or "").rstrip("/") or None)
    except SendError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:                    # noqa: BLE001
        return {"ok": False, "error": f"could not send ({type(e).__name__})"}
    return {"ok": True}
