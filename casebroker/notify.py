"""Push notifications for the campaign: a case started, began meshing, began
solving, finished, failed or was quarantined -- delivered by ntfy
(https://ntfy.sh), so they reach a phone or a closed laptop, which the
dashboard's in-tab browser notifications cannot.

Off unless ``CASEBROKER_NOTIFY_URL`` is set to an ntfy topic URL
(``https://ntfy.sh/<long-random-topic>`` -- on the public server the topic name
IS the secret -- or a self-hosted one). Optional:

  CASEBROKER_NOTIFY_TOKEN     bearer token, for a protected topic
  CASEBROKER_NOTIFY_EVENTS    which ones, comma-separated; default all of
                              started,meshing,solving,done,failed,quarantined
  CASEBROKER_NOTIFY_INTERVAL  seconds between polls, default 30
  CASEBROKER_PUBLIC_URL       the dashboard's address, for a tap-to-open link

Why a poller over the events table rather than a call in each route: the
events table is the one place every one of these moments is already written,
and it is written INSIDE the transaction that makes it true -- so reading it
back afterwards announces only what committed (a lease that rolled back never
reaches a phone). The poll also batches: one message per poll however many
cases moved, which is what keeps a busy fleet under ntfy.sh's daily message
allowance for a free topic. And it keeps every network call out of db._LOCK.

Starts from the NEWEST event when the process starts: a redeploy must not
replay the campaign's history to someone's phone.
"""
from __future__ import annotations

import json
import logging
import os
import threading
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
    if ev == "done":
        return "done"
    if ev == "failed":
        return "failed"
    if ev == "quarantined":
        return "quarantined"
    if ev == "progress":
        now, before = _phase(row.get("detail")), _phase(row.get("previous_detail"))
        if now and now != before:
            return now
    return None


def _where(row: dict[str, Any]) -> str:
    try:
        spec = json.loads(row.get("spec") or "{}")
        p = places.locate(float(spec["lat"]), float(spec["lon"]))
    except (KeyError, TypeError, ValueError):
        return ""
    if not p.get("country"):
        return ""
    town = p.get("town")
    near = f"{town}, " if town and (p.get("town_km") or 1e9) <= 25 else ""
    return near + p["country"]


def compose(rows: list[dict[str, Any]], wanted: set[str]) -> tuple[str, str, str] | None:
    """(title, body, tags) for one batch, or None when nothing in it is wanted."""
    items = [(kind, r) for r in rows if (kind := classify(r)) and kind in wanted]
    if not items:
        return None
    order = {k: i for i, k in enumerate(_TAGS)}
    items.sort(key=lambda kr: (order[kr[0]], kr[1]["id"]))
    lines = []
    for kind, r in items[:20]:
        where = _where(r)
        extra = ""
        if kind in ("failed", "quarantined") and r.get("detail"):
            extra = " -- " + " ".join(str(r["detail"]).split())[:140]
        lines.append(f"{r['case_id']} {_VERB[kind]}"
                     + (f" · {where}" if where else "")
                     + (f" · {r['worker_id']}" if r.get("worker_id") else "") + extra)
    if len(items) > 20:
        lines.append(f"... and {len(items) - 20} more")
    counts: dict[str, int] = {}
    for kind, _ in items:
        counts[kind] = counts.get(kind, 0) + 1
    if len(items) == 1:
        kind, r = items[0]
        title = f"Case {_VERB[kind]}"
    else:
        title = ", ".join(f"{n} {_VERB[k].split(' ')[0]}" for k, n in
                          sorted(counts.items(), key=lambda kv: order[kv[0]]))
    return title, "\n".join(lines), _TAGS[items[0][0]]


def send_ntfy(url: str, title: str, body: str, tags: str, token: str | None = None,
              click: str | None = None, timeout: float = 10.0) -> None:
    headers = {"Title": title.encode("utf-8").decode("latin-1", "replace"), "Tags": tags,
               "Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if click:
        headers["Click"] = click
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


#: Settings-table keys (casebroker/db.py ``settings``). Set from the dashboard's
#: Settings -> Notifications by an admin; each falls back to its environment
#: variable when unset, so an existing env-only deployment keeps working.
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


def resolve(settings: dict[str, str]) -> dict[str, Any]:
    """The configuration in force: each field from the settings table when an
    admin set it there, else from the environment. ``source`` says which, per
    field, because "why is it still sending" is answered by where it is set."""
    out: dict[str, Any] = {"source": {}}
    for field in KEYS:
        value = settings.get(KEYS[field])
        src = "settings"
        if value is None:
            value = os.environ.get(ENV[field], "").strip() or None
            src = "env" if value else None
        out[field] = value
        out["source"][field] = src
    out["events"] = sorted(parse_events(out["events"]), key=ALL_EVENTS.index)
    return out


def mask(url: str | None) -> str | None:
    """A topic URL for display: on ntfy.sh the topic name is the only secret."""
    if not url:
        return None
    head, _, topic = url.rstrip("/").rpartition("/")
    return f"{head}/{topic[:4]}…" if len(topic) > 4 else f"{head}/…"


def valid_url(url: str) -> bool:
    return url.startswith(("https://", "http://")) and " " not in url and len(url) <= 500


class Notifier:
    """One poll loop per broker process. ``poll_once`` is the whole behaviour and
    is what the tests drive; ``start`` only runs it on a timer in a daemon thread."""

    def __init__(self, conn, url: str | None = None, *, token: str | None = None,
                 events: set[str] | None = None, interval: float = 30.0,
                 public_url: str | None = None, sender: Callable[..., None] = send_ntfy,
                 live_config: bool = False):
        """With ``live_config`` the settings table (then the environment) is
        re-read on every poll, so a change made in the dashboard applies within
        one interval and needs no redeploy; the explicit arguments are then only
        what the tests pin."""
        self.conn, self.url, self.token = conn, url, token
        self.events = set(events or ALL_EVENTS)
        self.interval = max(5.0, interval)
        self.public_url = (public_url or "").rstrip("/") or None
        self.sender = sender
        self.live_config = live_config
        self.last_id = db.max_event_id(conn)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> int:
        """Announce everything since the last poll; returns how many rows were read.
        The position only advances past what was SENT: a failed delivery is tried
        again next poll rather than lost."""
        if self.live_config:
            cfg = resolve(db.get_settings(self.conn))
            self.url, self.token = cfg["url"], cfg["token"]
            self.events = set(cfg["events"])
            self.public_url = (cfg["public_url"] or "").rstrip("/") or None
        rows = db.events_after(self.conn, self.last_id)
        if not rows:
            return 0
        if not self.url:
            # Switched off: move past what happened, so switching it on later
            # announces from THEN rather than the whole backlog at once.
            self.last_id = rows[-1]["id"]
            return len(rows)
        msg = compose(rows, self.events)
        if msg:
            title, body, tags = msg
            try:
                self.sender(self.url, title, body, tags, token=self.token, click=self.public_url)
            except Exception as e:          # noqa: BLE001 -- a phone being unreachable is not the broker's failure
                log.warning("notification not delivered (%s); will retry", type(e).__name__)
                return 0
        self.last_id = rows[-1]["id"]
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
        self._stop.set()


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
    """One message now, with the configuration in force -- what the dashboard's
    "Send test" button calls, so a wrong topic is found in seconds rather than
    at the first finished case."""
    cfg = resolve(settings)
    if not cfg["url"]:
        return {"ok": False, "error": "no ntfy topic configured"}
    try:
        sender(cfg["url"], "Test from the case broker",
               "Notifications work. You will be told: " + ", ".join(cfg["events"]) + ".",
               "bell", token=cfg["token"], click=(cfg["public_url"] or "").rstrip("/") or None)
    except Exception as e:                    # noqa: BLE001 -- reported to the admin, verbatim
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    return {"ok": True}
