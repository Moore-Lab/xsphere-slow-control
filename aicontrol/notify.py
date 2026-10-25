"""
Phone alerts for the AI control service, and a disarm-only phone e-stop.

Run as its own unit (``xsphere-ainotify``, ``python -m aicontrol.notify``), so
it can report the AI control service's own death.  It reads MQTT, pushes to
an `ntfy <https://ntfy.sh>`_ topic (the phone app needs no account) and needs
nothing beyond paho and the standard library.  It imports only
``aicontrol.bus`` (the transport) and ``aicontrol.config`` (the env-file
reader) from the service, so a broken aicontrol deploy cannot stop it.

**What it watches** (all read-only):

* ``xsphere/status/ai_control`` (retained, every 2 s): online, arms, LN2
  timers, ramps;
* ``xsphere/status/ai_control/audit`` (retained, the last 50 entries);
* ``xsphere/status/pneumatic_control`` and ``…/turbo_control``: the PLC and
  turbo gates (for the digests and the weekend warning).

**What it pushes:**

* *urgent (5):* any audit entry saying "check the valve"; the AI service
  offline or silent while an LN2 timer was pending;
* *high (4):* ``auto_disarm`` and ``auto_close`` entries; the AI service
  offline (``online 0``) or silent for more than 30 s (saying whether LN2
  timers were pending, so nothing is closing them); 5 or more refusals in
  10 min, or a "counted but not audited" summary; an arm or a rails save whose
  ``by`` is not an expected source (:data:`DEFAULT_EXPECTED_BY`); this
  notifier losing the broker; armed at Saturday 22:00 (the Sunday 02:00
  backup restarts the broker);
* *normal (3):* a human arm or disarm; rails saved (with the diff line); an
  arm about to lapse while a ramp or an LN2 timer is active; the AI service
  back online; the phone e-stop's confirmation;
* *low (2):* a digest when an arm period ends (who, how long, actions by
  tool, refusals by reason, ramps, every AI note in full), and every day at
  08:00 America/New_York.  The daily digest doubles as this notifier's
  heartbeat: no digest means the notifier is down.

**Phone e-stop.**  Every push carries an ntfy ``http`` action, "Disarm AI",
that POSTs to a second topic.  :class:`EstopListener` keeps a streaming GET on
that topic; for any message on it the notifier publishes
:data:`DISARM_ALL` (``{"group":"all","armed":false,"by":"phone e-stop"}``) on
``xsphere/commands/ai_control/arm`` at qos 1, then pushes whether the AI
service's status confirms it.  That payload is a constant and the only
command this module can publish (:meth:`Notifier._publish` refuses any other
topic or payload): it never arms and never sends rails, so whoever learns the
e-stop topic can only disarm.

**Configuration** comes from the environment; the unit reads it from
``/etc/xsphere/notify.env`` (mode 600).  Topic names are the secret and are
never logged or published on MQTT.  See ``aicontrol/README.md``,
"Phone alerts".

Tests inject :class:`aicontrol.bus.LoopbackTransport` and a fake ntfy client:
no broker, no network.
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

import paho.mqtt.client as mqtt

from .bus import PahoTransport, decode_payload
from .config import ConfigError, host_without_port, load_env_file

log = logging.getLogger(__name__)

VERSION = "1.0.0"
PRODUCTION = "production"
DEFAULT_ENV_FILE = "/etc/xsphere/notify.env"
DEFAULT_SERVER = "https://ntfy.sh"
DEFAULT_TZ = "America/New_York"
DEFAULT_CLICK_URL = "http://xbox-pi:8088/control"
#: Sources whose arms and rails saves are expected: today's Control page
#: card, and the stamp R5 plans ("<name> @ <address> (browser)").  Anything
#: else (curl, python, mosquitto_pub, "unknown") is a high-priority alert.
DEFAULT_EXPECTED_BY = r"control page|.+ @ \S+ \(browser\)"
#: The IOTstack backup (root crontab, Sunday 02:00) recreates every container,
#: mosquitto included: 11–15 min without a broker, slowcontrol restarts, and
#: the AI service disarms everything.
DEFAULT_MAINTENANCE = "Sun 01:45-02:30"
DEFAULT_MAINTENANCE_WARN = "Sat 22:00"
DEFAULT_DAILY_DIGEST = "08:00"

GROUPS = ("heaters", "ln2", "pneumatic")
#: Which arm each AI write tool needs — a copy of ``aicontrol.actions.GROUP_OF``
#: (the tests check they agree), so this process imports nothing heavy.
GROUP_OF: Dict[str, Optional[str]] = {
    "set_heater_setpoints": "heaters", "set_heater_gradient": "heaters",
    "start_heater_ramp": "heaters", "stop_heater_ramp": None, "set_heater_auto": "heaters",
    "set_pv_source": "heaters", "set_heater_supply": "heaters", "set_ln2_valve": "ln2",
    "set_ln2_auto": "ln2", "set_pneumatic_valve": "pneumatic", "add_note": None,
}

#: The one command this process may publish.
DISARM_ALL: Dict[str, Any] = {"group": "all", "armed": False, "by": "phone e-stop"}
ESTOP_BY = DISARM_ALL["by"]

# ntfy priorities
P_LOW, P_DEFAULT, P_HIGH, P_URGENT = 2, 3, 4, 5
TAGS = {P_LOW: ["clipboard"], P_DEFAULT: ["robot"], P_HIGH: ["warning"],
        P_URGENT: ["rotating_light"]}

#: No live AI status for this long → "silent".
SILENT_AFTER_S = 30.0
#: This notifier without a broker for this long → alert.
BROKER_LOST_AFTER_S = 30.0
#: A clean stop (``stopped_utc`` in the offline status) with nothing armed
#: and no LN2 timer is only reported if the service is not back by then.
CLEAN_STOP_GRACE_S = 60.0
#: Back online = live "online" statuses for this long without an offline.
RECOVERED_AFTER_S = 20.0
STORM_COUNT = 5
STORM_WINDOW_S = 600.0
STORM_COOLDOWN_S = 1800.0
EXPIRY_WARN_S = 1800.0
#: The audit is published at most once a second, after the status: wait
#: this long after an arm ends before summing up its activity.
DIGEST_DELAY_S = 5.0
ESTOP_CONFIRM_S = 10.0
ESTOP_RETRY_S = 5.0
ESTOP_GIVE_UP_S = 600.0
ESTOP_LISTENER_ALERT_S = 300.0
#: Audit entries this much older than the notifier's start are history
#: (not alerted); younger ones were written during a restart and are.
STARTUP_CATCHUP_S = 120.0
OWN_STATUS_EVERY_S = 30.0
SCHEDULE_LATE_S = 3600.0
#: ntfy.sh limits anonymous senders; past this many pushes an hour only
#: urgent ones go out, the rest are summed up in one push later.
PUSH_BUDGET_PER_HOUR = 30
PUSH_RETRY_S = 30.0
PUSH_MAX_AGE_S = 6 * 3600.0
#: ntfy turns longer messages into attachments; split before that.
MAX_MESSAGE_BYTES = 3800
RING = 5000
CHECK_VALVE = "check the valve"

_TOPIC_RE = re.compile(r"[-_A-Za-z0-9]{1,64}")
MIN_TOPIC_LEN = 20
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ──────────────────────────────────────────────────────────────────────────
# Time helpers
# ──────────────────────────────────────────────────────────────────────────

def load_tz(name: str):
    """A ZoneInfo, or None (then the system's local time is used)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as e:                       # noqa: BLE001 — degrade, not crash
        log.warning("Time zone %r unavailable (%s); using the system's local time", name, e)
        return None


def to_local(ts: float, tz) -> datetime:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def local_ts(day: date, hh: int, mm: int, tz) -> float:
    """Epoch seconds of ``hh:mm`` local time on ``day``."""
    if tz is not None:
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).timestamp()
    return time.mktime((day.year, day.month, day.day, hh, mm, 0, 0, 0, -1))


def next_daily(after: float, tz, hh: int, mm: int) -> float:
    """The first ``hh:mm`` local strictly after ``after``."""
    d = to_local(after, tz).date()
    for i in range(3):
        t = local_ts(d + timedelta(days=i), hh, mm, tz)
        if t > after:
            return t
    raise AssertionError("unreachable")


def next_weekly(after: float, tz, weekday: int, hh: int, mm: int) -> float:
    """The first ``<weekday> hh:mm`` local strictly after ``after`` (Mon = 0)."""
    d = to_local(after, tz).date()
    for i in range(15):
        day = d + timedelta(days=i)
        if day.weekday() == weekday:
            t = local_ts(day, hh, mm, tz)
            if t > after:
                return t
    raise AssertionError("unreachable")


def parse_utc(text: Any) -> Optional[float]:
    """``2026-10-22T05:00:00Z`` / ``…00.123Z`` → epoch seconds (None if not that)."""
    if not isinstance(text, str) or not text.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00").timestamp()
    except ValueError:
        return None


def utc_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_dur(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 90:
        return f"{s} s"
    m = s // 60
    if m < 90:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m:02d} min"
    return f"{h // 24} d {h % 24} h"


def _short(text: Any, n: int) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _mask(topic: str) -> str:
    return f"{topic[:3]}…({len(topic)} chars)" if topic else "(none)"


# ──────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Window:
    """A weekly maintenance window in local time (may cross midnight)."""
    weekday: int
    start: Tuple[int, int]
    end: Tuple[int, int]
    text: str

    def bounds_around(self, ts: float, tz) -> Optional[Tuple[float, float]]:
        """``(start, end)`` epoch seconds if ``ts`` is inside this window."""
        local_day = to_local(ts, tz).date()
        for back in (0, 1):
            day = local_day - timedelta(days=back)
            if day.weekday() != self.weekday:
                continue
            start = local_ts(day, *self.start, tz)
            end_day = day if self.end > self.start else day + timedelta(days=1)
            end = local_ts(end_day, *self.end, tz)
            if start <= ts < end:
                return start, end
        return None

    def next_start(self, after: float, tz) -> float:
        return next_weekly(after, tz, self.weekday, *self.start)


def _hhmm(text: str, what: str) -> Tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ConfigError(f"{what}={text!r}: expected HH:MM")
    return int(m.group(1)), int(m.group(2))


def _weekday(text: str, what: str) -> int:
    key = (text or "").strip().lower()[:3]
    if key not in _DAYS:
        raise ConfigError(f"{what}: {text!r} is not a weekday (Mon … Sun)")
    return _DAYS.index(key)


def parse_windows(text: str) -> List[Window]:
    """``"Sun 01:45-02:30; Wed 03:00-03:10"`` → windows (``""``/``none`` → [])."""
    out = []
    if (text or "").strip().lower() in ("", "none", "off"):
        return out
    for part in re.split(r"[;,]", text):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\w+)\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", part)
        if not m:
            raise ConfigError(f"NOTIFY_MAINTENANCE: {part!r} is not like 'Sun 01:45-02:30'")
        start = _hhmm(m.group(2), "NOTIFY_MAINTENANCE")
        end = _hhmm(m.group(3), "NOTIFY_MAINTENANCE")
        if start == end:
            raise ConfigError(f"NOTIFY_MAINTENANCE: {part!r} is empty")
        out.append(Window(_weekday(m.group(1), "NOTIFY_MAINTENANCE"), start, end, part))
    return out


def parse_weekly(text: str, what: str) -> Optional[Tuple[int, int, int]]:
    """``"Sat 22:00"`` → (5, 22, 0); ``""``/``none`` → None."""
    if (text or "").strip().lower() in ("", "none", "off"):
        return None
    m = re.fullmatch(r"\s*(\w+)\s+(\d{1,2}:\d{2})\s*", text)
    if not m:
        raise ConfigError(f"{what}={text!r}: expected like 'Sat 22:00'")
    return (_weekday(m.group(1), what),) + _hhmm(m.group(2), what)


def _check_topic(name: str, value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise ConfigError(f"{name} is not set (see aicontrol/README.md, 'Phone alerts')")
    if not _TOPIC_RE.fullmatch(v):
        raise ConfigError(f"{name}: an ntfy topic is 1–64 letters, digits, '-' or '_'")
    if len(v) < MIN_TOPIC_LEN:
        raise ConfigError(f"{name} is {len(v)} characters: use a long random name (at least "
                          f"{MIN_TOPIC_LEN}) — anyone who guesses it can read the alerts or "
                          f"disarm the AI")
    return v


class NotifySettings:
    """Where to listen and where to push.  Construct with :meth:`from_env`."""

    def __init__(self, *, alert_topic: str, estop_topic: str,
                 instance: str = "test", mqtt_host: str = "localhost", mqtt_port: int = 1883,
                 ntfy_server: str = DEFAULT_SERVER, ntfy_token: Optional[str] = None,
                 expected_by: str = DEFAULT_EXPECTED_BY, click_url: Optional[str] =
                 DEFAULT_CLICK_URL, tz_name: str = DEFAULT_TZ,
                 daily_digest: str = DEFAULT_DAILY_DIGEST,
                 maintenance: str = DEFAULT_MAINTENANCE,
                 maintenance_warn: str = DEFAULT_MAINTENANCE_WARN):
        self.instance = (instance or "test").strip() or "test"
        self.production = self.instance == PRODUCTION
        self.mqtt_host = mqtt_host
        self.mqtt_port = int(mqtt_port)
        self.alert_topic = _check_topic("NTFY_ALERT_TOPIC", alert_topic)
        self.estop_topic = _check_topic("NTFY_ESTOP_TOPIC", estop_topic)
        if self.alert_topic == self.estop_topic:
            # Every alert would then arrive on the e-stop stream and disarm.
            raise ConfigError("NTFY_ALERT_TOPIC and NTFY_ESTOP_TOPIC must be different")
        server = (ntfy_server or DEFAULT_SERVER).strip().rstrip("/")
        parsed = urllib.parse.urlparse(server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(f"NTFY_SERVER={server!r} is not an http(s) URL")
        if parsed.scheme == "http" and host_without_port(parsed.netloc) not in (
                "localhost", "127.0.0.1", "::1"):
            log.warning("NTFY_SERVER is plain http: topic names (and a token) cross the "
                        "network unencrypted")
        self.ntfy_server = server
        self.ntfy_token = (ntfy_token or "").strip() or None
        try:
            self.expected_by = re.compile(expected_by or DEFAULT_EXPECTED_BY)
        except re.error as e:
            raise ConfigError(f"NOTIFY_EXPECTED_BY is not a valid regex: {e}") from None
        self.click_url = (click_url or "").strip() or None
        self.tz_name = tz_name or DEFAULT_TZ
        self.daily_digest = _hhmm(daily_digest or DEFAULT_DAILY_DIGEST, "NOTIFY_DAILY_DIGEST")
        self.maintenance = parse_windows(maintenance)
        self.maintenance_warn = parse_weekly(maintenance_warn, "NOTIFY_MAINTENANCE_WARN")

        if self.production:
            self.client_id = "xsphere-ainotify"
            self.ai_status_topic = "xsphere/status/ai_control"
            self.arm_topic = "xsphere/commands/ai_control/arm"
            self.status_topic = "xsphere/status/ai_notify"
        else:
            # Pairs with a test aicontrol instance (aicontrol/config.py); a
            # duplicate of the production id would kick the real notifier off.
            host = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname()) or "host"
            self.client_id = f"xsphere-ainotify-test-{host}-{os.getpid()}"
            self.ai_status_topic = "xsphere/test/ai_control"
            self.arm_topic = "xsphere/test/commands/ai_control/arm"
            self.status_topic = "xsphere/test/ai_notify"
            if host_without_port(self.mqtt_host) not in ("localhost", "127.0.0.1", "::1"):
                raise ConfigError(f"test instance refuses MQTT host {self.mqtt_host!r}: only "
                                  f"a broker on localhost (set XSPHERE_NOTIFY_INSTANCE="
                                  f"production for the real notifier)")
        self.audit_topic = f"{self.ai_status_topic}/audit"
        self.pneumatic_gate_topic = "xsphere/status/pneumatic_control"
        self.turbo_gate_topic = "xsphere/status/turbo_control"

    @property
    def estop_url(self) -> str:
        return f"{self.ntfy_server}/{self.estop_topic}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "NotifySettings":
        def get(name: str, default: str = "") -> str:
            v = env.get(name)
            return default if v is None else str(v)
        port_text = get("XSPHERE_MQTT_PORT", "1883").strip() or "1883"
        try:
            port = int(port_text)
        except ValueError:
            raise ConfigError(f"XSPHERE_MQTT_PORT={port_text!r} is not a port number") from None
        return cls(
            instance=get("XSPHERE_NOTIFY_INSTANCE", "test"),
            mqtt_host=get("XSPHERE_MQTT_HOST", "localhost").strip() or "localhost",
            mqtt_port=port,
            ntfy_server=get("NTFY_SERVER", DEFAULT_SERVER),
            alert_topic=get("NTFY_ALERT_TOPIC"),
            estop_topic=get("NTFY_ESTOP_TOPIC"),
            ntfy_token=get("NTFY_TOKEN") or None,
            expected_by=get("NOTIFY_EXPECTED_BY", DEFAULT_EXPECTED_BY),
            click_url=get("NOTIFY_CLICK_URL", DEFAULT_CLICK_URL),
            tz_name=get("NOTIFY_TZ", DEFAULT_TZ).strip() or DEFAULT_TZ,
            daily_digest=get("NOTIFY_DAILY_DIGEST", DEFAULT_DAILY_DIGEST),
            maintenance=get("NOTIFY_MAINTENANCE", DEFAULT_MAINTENANCE),
            maintenance_warn=get("NOTIFY_MAINTENANCE_WARN", DEFAULT_MAINTENANCE_WARN),
        )

    def describe(self) -> List[str]:
        """Startup log lines.  Topic names are masked; the token is never shown."""
        return [
            f"AI notifier {VERSION}, instance {self.instance!r} "
            f"({'PRODUCTION' if self.production else 'test'})",
            f"MQTT {self.mqtt_host}:{self.mqtt_port} as {self.client_id}; watching "
            f"{self.ai_status_topic} (+/audit), {self.pneumatic_gate_topic}, "
            f"{self.turbo_gate_topic}; e-stop publishes on {self.arm_topic}",
            f"ntfy {self.ntfy_server}: alerts {_mask(self.alert_topic)}, e-stop "
            f"{_mask(self.estop_topic)}, token {'set' if self.ntfy_token else 'none'}",
            f"time zone {self.tz_name}; daily digest {self.daily_digest[0]:02d}:"
            f"{self.daily_digest[1]:02d}; maintenance "
            f"{'; '.join(w.text for w in self.maintenance) or 'none'}; expected sources "
            f"/{self.expected_by.pattern}/",
        ]

    def __repr__(self) -> str:     # never shows the topics or the token
        return (f"NotifySettings(instance={self.instance!r}, client_id={self.client_id!r}, "
                f"server={self.ntfy_server!r})")


# ──────────────────────────────────────────────────────────────────────────
# ntfy
# ──────────────────────────────────────────────────────────────────────────

class NtfyError(Exception):
    """A push or the e-stop stream failed (message carries no topic name)."""


class NtfyClient:
    """Publish (JSON to the server root) and subscribe (``/<topic>/json`` stream).

    ``opener`` is ``urllib.request.urlopen`` unless a test injects one.
    """

    def __init__(self, server: str, token: Optional[str] = None, *,
                 opener: Optional[Callable[..., Any]] = None, timeout: float = 15.0,
                 stream_timeout: float = 120.0):
        self.server = server.rstrip("/")
        self._token = token
        self._open = opener if opener is not None else urllib.request.urlopen
        self.timeout = timeout
        # ntfy sends a keepalive every 45 s (ntfy.sh); a silent socket past
        # this is a dead stream.
        self.stream_timeout = stream_timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"User-Agent": f"xsphere-ainotify/{VERSION}"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        h.update(extra or {})
        return h

    @staticmethod
    def _http_error(e: urllib.error.HTTPError) -> NtfyError:
        try:
            body = e.read(500).decode("utf-8", "replace")
            text = json.loads(body).get("error") or body
        except Exception:                        # noqa: BLE001
            text = ""
        return NtfyError(f"HTTP {e.code}{': ' + _short(text, 120) if text else ''}")

    def publish(self, topic: str, *, message: str, title: Optional[str] = None,
                priority: int = P_DEFAULT, tags: Iterable[str] = (),
                actions: Iterable[dict] = (), click: Optional[str] = None) -> Optional[str]:
        """One push; returns ntfy's message id.  Raises :class:`NtfyError`."""
        body: Dict[str, Any] = {"topic": topic, "message": message, "priority": int(priority)}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = list(tags)
        if actions:
            body["actions"] = list(actions)
        if click:
            body["click"] = click
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.server + "/", data=data, method="POST",
                                     headers=self._headers({"Content-Type": "application/json"}))
        try:
            with self._open(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
                text = resp.read(4096)
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise NtfyError(f"{type(e).__name__}: {_short(e, 120)}") from None
        if not 200 <= int(status) < 300:
            raise NtfyError(f"HTTP {status}")
        try:
            return json.loads(text).get("id")
        except Exception:                        # noqa: BLE001 — sent is sent
            return None

    def stream(self, topic: str, since: Optional[str] = None) -> Iterator[dict]:
        """Yield the JSON events of a subscription until the server closes it."""
        url = f"{self.server}/{topic}/json"
        if since:
            url += "?" + urllib.parse.urlencode({"since": since})
        req = urllib.request.Request(url, headers=self._headers())
        try:
            resp = self._open(req, timeout=self.stream_timeout)
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise NtfyError(f"{type(e).__name__}: {_short(e, 120)}") from None
        try:
            for raw in resp:
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    log.warning("e-stop stream: ignoring a line that is not JSON")
                    continue
                if isinstance(ev, dict):
                    yield ev
        except (OSError, ValueError) as e:       # socket timeout / reset mid-stream
            raise NtfyError(f"stream broken: {type(e).__name__}: {_short(e, 120)}") from None
        finally:
            try:
                resp.close()
            except Exception:                    # noqa: BLE001
                pass


class EstopListener:
    """The outbound streaming GET on the e-stop topic.

    Reconnects with ``since=<last message id>`` (or the notifier's start
    time) so a tap made while the stream was down still arrives; the notifier
    drops ids it has seen and taps from before it started.
    """

    def __init__(self, ntfy, topic: str, on_event: Callable[[dict], None], *,
                 since: float, mono: Callable[[], float] = time.monotonic):
        self._ntfy = ntfy
        self._topic = topic
        self._on_event = on_event
        self._mono = mono
        self.since: str = str(int(since))
        self.connected = False
        self.down_since: Optional[float] = mono()
        self.last_event: Optional[float] = None
        self.error: Optional[str] = None
        self.connects = 0

    def run_once(self) -> None:
        """One subscription, until it ends or fails (never raises)."""
        try:
            for ev in self._ntfy.stream(self._topic, self.since):
                if not self.connected:
                    self.connected = True
                    self.down_since = None
                    self.connects += 1
                    self.error = None
                    log.info("e-stop stream connected")
                self.last_event = self._mono()
                if ev.get("event") == "message":
                    if isinstance(ev.get("id"), str) and ev["id"]:
                        self.since = ev["id"]
                    try:
                        self._on_event(ev)
                    except Exception:            # noqa: BLE001
                        log.exception("e-stop handler failed")
            self.error = "the server closed the stream"
        except NtfyError as e:
            self.error = str(e)
        except Exception as e:                   # noqa: BLE001 — keep the thread alive
            log.exception("e-stop stream failed")
            self.error = f"{type(e).__name__}: {_short(e, 120)}"
        finally:
            if self.connected:
                log.warning("e-stop stream lost: %s", self.error)
            self.connected = False
            if self.down_since is None:
                self.down_since = self._mono()

    def run_forever(self, stop: threading.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            t0 = self._mono()
            self.run_once()
            if self._mono() - t0 > 60:
                backoff = 1.0
            stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)


# ──────────────────────────────────────────────────────────────────────────
# MQTT transport
# ──────────────────────────────────────────────────────────────────────────

class NotifyTransport(PahoTransport):
    """PahoTransport with the notifier's own retained last-will."""

    def __init__(self, settings: NotifySettings, keepalive: int = 30):
        class _S:
            mqtt_host = settings.mqtt_host
            mqtt_port = settings.mqtt_port
            client_id = settings.client_id
            status_topic = settings.status_topic
        super().__init__(_S, keepalive=keepalive)

    def _build_client(self) -> mqtt.Client:
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=self._client_id, protocol=mqtt.MQTTv5)
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        client.will_set(self._will_topic, json.dumps({"online": 0}), qos=1, retain=True)
        return client


# ──────────────────────────────────────────────────────────────────────────
# Notifier
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Push:
    priority: int
    title: str
    message: str
    key: str
    created: float
    tags: List[str] = field(default_factory=list)
    next_part: int = 0


def _armed_groups(status: Optional[dict]) -> List[str]:
    if not isinstance(status, dict) or status.get("online") != 1:
        return []
    return [g for g in GROUPS if status.get(f"armed_{g}") == 1]


def _timers(status: Optional[dict]) -> Dict[str, dict]:
    t = status.get("ln2_timers") if isinstance(status, dict) else None
    return t if isinstance(t, dict) else {}


def _ramps(status: Optional[dict]) -> List[dict]:
    r = status.get("ramps") if isinstance(status, dict) else None
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


def _risky(status: Optional[dict]) -> bool:
    """Anything armed, an LN2 timer or a ramp in this status."""
    return bool(_armed_groups(status) or _timers(status) or _ramps(status))


def split_message(text: str, limit: int = MAX_MESSAGE_BYTES) -> List[str]:
    """Split on line breaks into parts of at most ``limit`` UTF-8 bytes."""
    if len(text.encode("utf-8")) <= limit:
        return [text]
    parts: List[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line.encode("utf-8")) > limit:          # one very long line
            cut = limit
            while len(line[:cut].encode("utf-8")) > limit:
                cut -= 1
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:cut])
            line = line[cut:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate.encode("utf-8")) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


class Notifier:
    """Everything except the threads: tests drive it with ``tick()`` and
    ``pump_outbox()`` on a fake clock.

    MQTT messages arrive on the transport's thread, the e-stop on the
    listener's; both only update state and queue pushes.  HTTP happens in
    :meth:`pump_outbox`, on the sender thread.
    """

    def __init__(self, settings: NotifySettings, transport=None, ntfy=None, *,
                 clock: Callable[[], float] = time.time,
                 mono: Optional[Callable[[], float]] = None):
        self.s = settings
        self._clock = clock
        self._mono = mono if mono is not None else (
            time.monotonic if clock is time.time else clock)
        self._transport = transport if transport is not None else NotifyTransport(settings)
        self._ntfy = ntfy if ntfy is not None else NtfyClient(settings.ntfy_server,
                                                              settings.ntfy_token)
        self.tz = load_tz(settings.tz_name)
        self._lock = threading.RLock()
        self._out_cond = threading.Condition()
        self.outbox: Deque[Push] = collections.deque()
        self._held: List[Push] = []
        self._sent_times: Deque[float] = collections.deque()
        self._retry_at = float("-inf")
        self.push_stats = {"sent": 0, "failed": 0, "dropped": 0, "held": 0}
        self.last_push_utc: Optional[str] = None
        self.last_push_error: Optional[str] = None

        self.started_wall = clock()
        self.started_mono = self._mono()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.estop_listener: Optional[EstopListener] = None

        # the AI service
        self._status: Optional[dict] = None
        self._last_online: Optional[dict] = None
        self._status_live: Optional[float] = None          # mono
        self.ai_phase = "unknown"                           # unknown | up | down
        self._down: Optional[dict] = None
        self._up_since: Optional[float] = None              # mono, while recovering
        self._periods: Dict[str, dict] = {}
        self._pending_digests: List[Tuple[float, list]] = []
        self._expiry_warned: set = set()
        self._no_status_noted = False
        # audit
        self._seen: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self._audit_baselined = False
        self.ring: Deque[dict] = collections.deque(maxlen=RING)
        self._refusals: Deque[Tuple[float, str, str]] = collections.deque()
        self._storm_quiet_until = float("-inf")
        self._flood_quiet_until = float("-inf")
        # gates
        self._gates: Dict[str, Optional[dict]] = {"pneumatic": None, "turbo": None}
        # this notifier's broker connection
        self._broker_since: Optional[float] = None          # mono of the current connection
        self._broker_down: Optional[float] = self.started_mono
        self._broker_alerted = False
        self._broker_deferred = False
        # e-stop
        self._estop_ids: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self._estop_pending: Optional[dict] = None
        self._estop_retry: Optional[dict] = None
        self._estop_listener_alerted = False
        self.estops = 0
        # schedule
        self._next_daily = next_daily(self.started_wall, self.tz, *settings.daily_digest)
        self._next_warn = (next_weekly(self.started_wall, self.tz, *settings.maintenance_warn)
                           if settings.maintenance_warn else None)
        self._maint_log: List[str] = []
        self._own_status_at = float("-inf")
        self._last_daily: Optional[float] = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, threads: bool = True) -> None:
        self._push(P_LOW, "notifier started",
                   f"Watching the AI control service ({self.s.instance}). Every message has a "
                   f"'Disarm AI' button: it disarms every AI group (it never arms). A daily "
                   f"digest at {self.s.daily_digest[0]:02d}:{self.s.daily_digest[1]:02d} "
                   f"says this notifier is alive.", key="started")
        self.estop_listener = EstopListener(self._ntfy, self.s.estop_topic,
                                            self.handle_estop_event, since=self.started_wall,
                                            mono=self._mono)
        self._transport.start(self._handle_message, self._handle_connect)
        if threads:
            for target, name in ((self._tick_loop, "ainotify-tick"),
                                 (self._send_loop, "ainotify-send"),
                                 (lambda: self.estop_listener.run_forever(self._stop),
                                  "ainotify-estop")):
                t = threading.Thread(target=target, name=name, daemon=True)
                t.start()
                self._threads.append(t)

    def shutdown(self) -> None:
        """Say so on the phone (best effort), go offline, disconnect."""
        self._stop.set()
        with self._out_cond:
            self._out_cond.notify_all()
        try:
            self._ntfy.publish(self.s.alert_topic, title="xsphere: notifier stopped",
                               message="The AI notifier stopped: no phone alerts and no "
                                       "'Disarm AI' button until it starts again.",
                               priority=P_DEFAULT, tags=TAGS[P_DEFAULT],
                               actions=[self._action()], click=self.s.click_url)
        except Exception as e:                   # noqa: BLE001
            log.warning("Could not push the shutdown notice: %s", e)
        try:
            if self._transport.is_connected():
                self._transport.publish(self.s.status_topic,
                                        json.dumps({"online": 0, "stopped_utc":
                                                    utc_iso(self._clock())}),
                                        qos=1, retain=True, wait_s=2.0)
        except Exception:                        # noqa: BLE001
            log.exception("Could not publish the offline status")
        try:
            self._transport.stop()
        except Exception:                        # noqa: BLE001
            log.exception("Error stopping the MQTT transport")

    def _tick_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception:                    # noqa: BLE001
                log.exception("tick failed")

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            with self._out_cond:
                if not self.outbox and not self._held:
                    self._out_cond.wait(5.0)
            try:
                self.pump_outbox()
            except Exception:                    # noqa: BLE001
                log.exception("sending failed")
            self._stop.wait(1.0)

    # ── MQTT ──────────────────────────────────────────────────────────────

    def _handle_connect(self) -> None:
        try:
            for topic in (self.s.ai_status_topic, self.s.audit_topic,
                          self.s.pneumatic_gate_topic, self.s.turbo_gate_topic):
                self._transport.subscribe(topic, qos=1)
            with self._lock:
                self._broker_since = self._mono()
            log.info("MQTT connected; subscribed to the AI status, audit and both gates")
            self._publish_own_status()
        except Exception:                        # noqa: BLE001 — never kill paho's loop
            log.exception("Error handling MQTT connect")

    def _handle_message(self, msg) -> None:
        try:
            topic = msg.topic
            retained = bool(getattr(msg, "retain", False))
            payload = decode_payload(msg.payload or b"")
            with self._lock:
                if topic == self.s.ai_status_topic:
                    self._on_status(payload, retained)
                elif topic == self.s.audit_topic:
                    self._on_audit(payload)
                elif topic == self.s.pneumatic_gate_topic:
                    self._gates["pneumatic"] = payload if isinstance(payload, dict) else None
                elif topic == self.s.turbo_gate_topic:
                    self._gates["turbo"] = payload if isinstance(payload, dict) else None
        except Exception:                        # noqa: BLE001
            log.exception("Error handling an MQTT message")

    def _publish(self, topic: str, payload: dict, *, qos: int, retain: bool) -> bool:
        """The only way out to MQTT: this notifier's own status, or DISARM_ALL."""
        if topic == self.s.arm_topic:
            if payload != DISARM_ALL or retain:
                raise ValueError("the notifier only ever publishes the disarm-all command")
        elif topic != self.s.status_topic:
            raise ValueError(f"the notifier may not publish on {topic!r}")
        try:
            return bool(self._transport.publish(topic, json.dumps(payload, allow_nan=False),
                                                qos=qos, retain=retain))
        except Exception:                        # noqa: BLE001
            log.exception("MQTT publish on %s failed", topic)
            return False

    def own_status(self) -> dict:
        lis = self.estop_listener
        return {"online": 1, "version": VERSION, "instance": self.s.instance,
                "started_utc": utc_iso(self.started_wall), "ai_phase": self.ai_phase,
                "estop_listener": "connected" if lis and lis.connected else "down",
                "pushes_sent": self.push_stats["sent"],
                "push_failures": self.push_stats["failed"],
                "last_push_utc": self.last_push_utc, "last_push_error": self.last_push_error,
                "next_daily_digest_utc": utc_iso(self._next_daily)}

    def _publish_own_status(self) -> None:
        """Never call it holding ``self._lock`` (see :meth:`_send_disarm`)."""
        self._own_status_at = self._mono()
        if self._transport.is_connected():
            self._publish(self.s.status_topic, self.own_status(), qos=0, retain=True)

    # ── the AI service's status ─────────────────────────────────────────────

    def _on_status(self, payload: Any, retained: bool) -> None:
        if not isinstance(payload, dict):
            return
        prev = self._status
        self._status = payload
        online = payload.get("online") == 1
        if online:
            self._last_online = payload
        if retained:
            # A replay (our start or reconnect) says nothing about now; it is
            # the baseline the first time only.
            if prev is None:
                self._baseline(payload)
            return
        self._status_live = self._mono()
        if online:
            self._on_online(payload, prev)
        else:
            self._on_offline(payload)

    def _baseline(self, payload: dict) -> None:
        if payload.get("online") == 1:
            self._open_periods(payload, baseline=True)
        else:
            self._declare_down("the AI control service was already offline when the notifier "
                               "started", clean="stopped_utc" in payload, last=None,
                               priority=P_HIGH, since=parse_utc(payload.get("stopped_utc")))

    def _on_online(self, payload: dict, prev: Optional[dict]) -> None:
        now_m = self._mono()
        if self.ai_phase == "down":
            if self._up_since is None:
                self._up_since = now_m
        elif self.ai_phase == "unknown":
            self.ai_phase = "up"
        if (isinstance(prev, dict) and prev.get("online") == 1
                and prev.get("started_utc") and payload.get("started_utc")
                and prev["started_utc"] != payload["started_utc"]):
            # Restarted between two statuses without an offline we saw.
            self._close_periods(list(self._periods), "the AI control service restarted",
                                self._clock())
        self._close_disarmed(payload)
        self._open_periods(payload)
        self._check_expiry(payload)
        if self._estop_pending is not None and not _armed_groups(payload):
            self._estop_confirmed()

    def _on_offline(self, payload: dict) -> None:
        last = self._last_online
        self._close_periods(list(self._periods), "the AI control service went offline",
                            self._clock())
        if self.ai_phase == "down":
            if self._down is not None and self._up_since is not None:
                self._down["restarts"] += 1
            self._up_since = None
            return
        clean = "stopped_utc" in payload
        self._declare_down("the AI control service stopped (clean shutdown)" if clean else
                           "the AI control service went offline (connection lost — crash or "
                           "network)", clean=clean, last=last)

    def _declare_down(self, why: str, *, clean: bool, last: Optional[dict],
                      priority: Optional[int] = None, since: Optional[float] = None) -> None:
        now_m = self._mono()
        risky = _risky(last)
        self.ai_phase = "down"
        self._up_since = None
        self._down = {"since": since if since is not None else self._clock(), "why": why, "last": last, "alerted": False,
                      "deferred": False, "restarts": 0, "clean": clean,
                      "priority": priority,
                      # A clean stop with nothing at stake gets a grace period:
                      # a restart only says "restarted".
                      "alert_at": now_m + (CLEAN_STOP_GRACE_S if clean and not risky else 0)}
        log.warning("AI control: %s", why)
        self._check_down(now_m)

    def _down_text(self, d: dict) -> Tuple[int, str, str]:
        last = d["last"]
        timers = _timers(last)
        lines = [f"{d['why'][0].upper()}{d['why'][1:]} at {self._hm(d['since'])}."]
        if last is None:
            lines.append("LN2 timers: unknown (no status from the AI service since the "
                         "notifier started).")
        elif timers:
            lines.append("LN2 timers NOT running: " + "; ".join(
                f"{v} opened {self._hm(parse_utc(t.get('opened_utc')))}, close due "
                f"{self._hm(parse_utc(t.get('deadline_utc')))}" for v, t in timers.items())
                + ". Nothing closes those valves now except the PLC ladder's own fill timer "
                  "(only with auto-close on): check them.")
        else:
            lines.append("No LN2 timers were pending.")
        armed = _armed_groups(last)
        if armed:
            lines.append(f"Armed at its last status: {', '.join(armed)} (it starts disarmed "
                         f"when it comes back).")
        ramps = _ramps(last)
        if ramps:
            lines.append("Ramps stopped: " + ", ".join(
                f"{r.get('zone')} at {r.get('current_k')} K (target {r.get('target_k')} K)"
                for r in ramps))
        lines.append("Logs: journalctl -u xsphere-aicontrol -n 50")
        priority = d.get("priority") or (P_URGENT if timers else P_HIGH)
        return priority, "AI control is DOWN", "\n".join(lines)

    def _check_down(self, now_m: float) -> None:
        d = self._down
        if d is None or d["alerted"] or now_m < d["alert_at"]:
            return
        if self._up_since is not None:
            return                              # back already: recovery decides
        if not _risky(d["last"]) and self._in_maintenance(self._clock()):
            if not d["deferred"]:
                d["deferred"] = True
                log.info("AI control down inside a maintenance window: alert deferred")
            return
        d["alerted"] = True
        priority, title, text = self._down_text(d)
        self._push(priority, title, text, key="ai_down")

    def _check_recovery(self, now_m: float) -> None:
        d = self._down
        if self.ai_phase != "down" or self._up_since is None:
            return
        if now_m - self._up_since < RECOVERED_AFTER_S:
            return
        self.ai_phase = "up"
        self._up_since = None
        self._down = None
        if d is None:
            return
        took = fmt_dur(self._clock() - d["since"])
        st = self._status if isinstance(self._status, dict) else {}
        extra = ""
        if _timers(st):
            extra = " Its LN2 timers were reloaded: " + ", ".join(_timers(st)) + "."
        restarts = f" ({d['restarts']} more restart(s) meanwhile)" if d["restarts"] else ""
        armed = _armed_groups(st)
        if d["alerted"]:
            # A hung service that recovers did not restart: say what is armed.
            self._push(P_DEFAULT, "AI control back online",
                       f"Back after {took}{restarts}. "
                       + (f"Armed now: {', '.join(armed)}." if armed else "Nothing is armed.")
                       + extra, key="ai_up")
        elif d["deferred"]:
            self._maint_log.append(f"AI control down {self._hm(d['since'])} for {took} "
                                   f"inside the maintenance window")
        elif d["clean"]:
            self._push(P_DEFAULT, "AI control restarted",
                       f"Stopped at {self._hm(d['since'])}, back {took} later{restarts}. "
                       f"Everything starts disarmed.{extra}", key="ai_restarted")

    def _check_silence(self, now_m: float) -> None:
        if self._broker_since is None or self._broker_down is not None:
            return                               # our own broker problem, not theirs
        heard = max(self._status_live or float("-inf"), self._broker_since)
        if now_m - heard <= SILENT_AFTER_S:
            return
        if self.ai_phase == "up":
            self._declare_down(f"the AI control service has been silent for over "
                               f"{SILENT_AFTER_S:.0f} s (hung, or its broker connection is "
                               f"stuck)", clean=False, last=self._last_online)
        elif self.ai_phase == "unknown":
            if self._status is not None and self._status.get("online") == 1:
                self._declare_down(f"the AI control service's last status says online, but "
                                   f"nothing has come for over {SILENT_AFTER_S:.0f} s since "
                                   f"the notifier started", clean=False,
                                   last=self._last_online)
            elif self._status is None and not self._no_status_noted:
                self._no_status_noted = True
                self._push(P_DEFAULT, "no AI control status",
                           f"Nothing on {self.s.ai_status_topic} since the notifier started: "
                           f"the AI control service is not installed or has never run.",
                           key="ai_none")

    # ── arm periods and per-arm digests ───────────────────────────────────

    def _open_periods(self, payload: dict, baseline: bool = False) -> None:
        arms = payload.get("arms") if isinstance(payload.get("arms"), dict) else {}
        for g in GROUPS:
            a = arms.get(g) if isinstance(arms.get(g), dict) else {}
            if payload.get(f"armed_{g}") == 1 and g not in self._periods:
                since = parse_utc(a.get("since_utc")) or self._clock()
                self._periods[g] = {"since": since, "by": a.get("by"),
                                    "expires": parse_utc(a.get("expires_utc")),
                                    "partial": baseline or since < self.started_wall}

    def _close_disarmed(self, payload: dict) -> None:
        arms = payload.get("arms") if isinstance(payload.get("arms"), dict) else {}
        ended = []
        for g in list(self._periods):
            if payload.get(f"armed_{g}") == 1:
                continue
            a = arms.get(g) if isinstance(arms.get(g), dict) else {}
            ld = a.get("last_disarm") if isinstance(a.get("last_disarm"), dict) else {}
            ended.append((g, self._periods.pop(g), ld.get("reason") or "disarmed",
                          parse_utc(ld.get("utc")) or self._clock()))
        if ended:
            self._push_arm_digest(ended)

    def _close_periods(self, groups: List[str], reason: str, end: float) -> None:
        ended = [(g, self._periods.pop(g), reason, end) for g in groups if g in self._periods]
        if ended:
            self._push_arm_digest(ended)

    def _push_arm_digest(self, ended: List[Tuple[str, dict, str, float]]) -> None:
        self._pending_digests.append((self._mono() + DIGEST_DELAY_S, ended))

    def _flush_digests(self, now_m: float, force: bool = False) -> None:
        due = [e for t, e in self._pending_digests if force or now_m >= t]
        self._pending_digests = [(t, e) for t, e in self._pending_digests
                                 if not (force or now_m >= t)]
        for ended in due:
            self._arm_digest(ended)

    def _arm_digest(self, ended: List[Tuple[str, dict, str, float]]) -> None:
        start = min(p["since"] for _g, p, _r, _e in ended)
        end = max(e for _g, _p, _r, e in ended)
        lines = []
        for g, p, reason, e in ended:
            lines.append(f"{g}: armed {self._hm(p['since'])} by {p.get('by') or '?'}, ended "
                         f"{self._hm(e)} after {fmt_dur(e - p['since'])} — {reason}")
        lines.append("")
        lines += self._activity_lines(start - 1.0, end + 2.0, notes_in_full=True)
        if any(p.get("partial") for _g, p, _r, _e in ended):
            lines.append(f"(The notifier started {self._hm(self.started_wall)}; it only has "
                         f"the activity it saw since and the service's last 50 entries.)")
        groups = ", ".join(g for g, _p, _r, _e in ended)
        self._push(P_LOW, f"AI arm ended: {groups} ({fmt_dur(end - start)})",
                   "\n".join(lines), key="arm_digest")

    def _activity_lines(self, t0: float, t1: float, *, notes_in_full: bool) -> List[str]:
        entries = [e for e in self.ring if t0 <= e["_ts"] <= t1]
        actions = collections.Counter(e.get("tool") or "?" for e in entries
                                      if e.get("kind") == "action")
        refusals = collections.Counter()
        n_refused = 0
        for e in entries:
            if e.get("kind") != "refused":
                continue
            n = int(e["unaudited"]) if isinstance(e.get("unaudited"), int) else 1
            n_refused += n
            refusals[(e.get("tool") or "-", _short(e.get("detail", ""), 110))] += n
        out = ["Actions: " + (", ".join(f"{t} ×{c}" for t, c in actions.most_common())
                              or "none")]
        out.append(f"Refusals: {n_refused}" + (":" if refusals else ""))
        for (tool, detail), c in refusals.most_common(8):
            out.append(f"  {tool} ×{c}: {detail}")
        if len(refusals) > 8:
            out.append(f"  … and {len(refusals) - 8} other reason(s)")
        for kind, label in (("ramp", "Ramps"), ("auto_close", "LN2 auto-closes"),
                            ("auto_disarm", "Auto-disarms")):
            items = [e for e in entries if e.get("kind") == kind]
            if items:
                out.append(f"{label}:")
                out += [f"  {self._hm(e['_ts'])} {_short(e.get('detail', ''), 300)}"
                        for e in items]
        notes = [e for e in entries if e.get("kind") == "note"]
        if notes and notes_in_full:
            out.append(f"AI notes ({len(notes)}):")
            for e in notes:
                out.append(f"— {self._hm(e['_ts'])}: {str(e.get('detail', '')).strip()}")
        elif notes:
            out.append(f"AI notes: {len(notes)} (the last: "
                       f"{_short(notes[-1].get('detail', ''), 200)})")
        return out

    def _check_expiry(self, payload: dict) -> None:
        timers, ramps = _timers(payload), _ramps(payload)
        if not timers and not ramps:
            return
        arms = payload.get("arms") if isinstance(payload.get("arms"), dict) else {}
        now = self._clock()
        for g in GROUPS:
            a = arms.get(g) if isinstance(arms.get(g), dict) else {}
            exp = parse_utc(a.get("expires_utc"))
            if payload.get(f"armed_{g}") != 1 or exp is None:
                continue
            key = (g, a.get("expires_utc"))
            if 0 < exp - now <= EXPIRY_WARN_S and key not in self._expiry_warned:
                self._expiry_warned.add(key)
                what = []
                if ramps:
                    what.append("ramp(s) " + ", ".join(f"{r.get('zone')} → "
                                                        f"{r.get('target_k')} K" for r in ramps)
                                + " stop when heaters disarm (the setpoint stays where it is)")
                if timers:
                    what.append("LN2 timer(s) " + ", ".join(timers) + " keep running: the "
                                "service still closes those valves while disarmed")
                self._push(P_DEFAULT, f"AI {g} arm lapses at {self._hm(exp)}",
                           f"The {g} arm expires in {fmt_dur(exp - now)}. "
                           + "; ".join(what) + ". Re-arm on the Control page to continue.",
                           key="expiry")

    # ── audit ─────────────────────────────────────────────────────────────

    @staticmethod
    def _fingerprint(e: dict) -> str:
        return json.dumps([e.get("utc"), e.get("kind"), e.get("detail"), e.get("tool"),
                           e.get("by")], ensure_ascii=True, default=str)

    def _on_audit(self, payload: Any) -> None:
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return
        now = self._clock()
        new = []
        for e in reversed(entries):                     # oldest first
            if not isinstance(e, dict):
                continue
            fp = self._fingerprint(e)
            if fp in self._seen:
                continue
            self._seen[fp] = None
            while len(self._seen) > 3 * RING // 5:
                self._seen.popitem(last=False)
            ts = parse_utc(e.get("utc"))
            rec = dict(e, _ts=ts if ts is not None else now)
            self.ring.append(rec)
            if not self._audit_baselined and (ts is None
                                              or ts < self.started_wall - STARTUP_CATCHUP_S):
                continue                                # history from before we started
            new.append(rec)
        self._audit_baselined = True
        for rec in new:
            try:
                self._classify(rec)
            except Exception:                    # noqa: BLE001 — one odd entry
                log.exception("Could not classify an audit entry")

    def _expected(self, by: Any) -> bool:
        return isinstance(by, str) and bool(self.s.expected_by.fullmatch(by.strip()))

    def _classify(self, e: dict) -> None:
        kind = e.get("kind")
        detail = str(e.get("detail") or "")
        by = e.get("by")
        if CHECK_VALVE in detail.lower():
            self._push(P_URGENT, "LN2: check the valve", detail, key="check_valve")
        elif kind == "auto_close":
            self._push(P_HIGH, "LN2 valve auto-closed by the AI service", detail,
                       key="auto_close")
        elif kind == "auto_disarm":
            self._push(P_HIGH, "AI auto-disarmed", detail, key="auto_disarm")
        elif kind == "arm":
            ignored = detail.startswith("arm command ignored")
            if not self._expected(by):
                self._push(P_HIGH, "AI arm from an UNEXPECTED source",
                           f"{detail}\nSource (by): {by!r} — not the Control page. If this was "
                           f"not you, tap Disarm AI.", key="arm_unexpected")
            elif ignored:
                self._push(P_DEFAULT, "AI arm command ignored", detail, key="arm_ignored")
            else:
                m = re.match(r"(\w+) (re-)?armed", detail)
                self._push(P_DEFAULT, f"AI {m.group(2) or ''}armed: {m.group(1)}" if m
                           else "AI armed", detail, key="arm")
        elif kind == "disarm":
            if by == ESTOP_BY:
                return                                  # the e-stop confirms on its own
            self._push(P_DEFAULT, "AI disarmed", detail, key="disarm")
        elif kind == "rails":
            m = re.match(r"rails revision (\d+) saved by .*?: (.*)$", detail, re.S)
            body = (f"Saved by {by}.\n" + m.group(2).replace("; ", "\n")) if m else detail
            rev = f" (revision {m.group(1)})" if m else ""
            if self._expected(by):
                self._push(P_DEFAULT, f"AI rails saved{rev}", body, key="rails")
            else:
                self._push(P_HIGH, f"AI rails saved from an UNEXPECTED source{rev}",
                           f"{body}\nSource (by): {by!r} — not the Control page. If this was "
                           f"not you, tap Disarm AI and restore the rails.",
                           key="rails_unexpected")
        elif kind == "rails_refused":
            if not self._expected(by):
                self._push(P_HIGH, "AI rails save attempt from an UNEXPECTED source",
                           f"{detail}\nSource (by): {by!r}.", key="rails_refused")
        elif kind == "refused":
            self._count_refusal(e, detail)

    def _count_refusal(self, e: dict, detail: str) -> None:
        now = self._clock()
        if isinstance(e.get("unaudited"), int) or "counted but not audited" in detail:
            if now >= self._flood_quiet_until:
                self._flood_quiet_until = now + STORM_COOLDOWN_S
                self._push(P_HIGH, "AI refusal flood",
                           f"{detail}\nA session is looping on calls that do nothing. Tap "
                           f"Disarm AI, or stop that session.", key="refusal_flood")
            return
        self._refusals.append((e["_ts"], e.get("tool") or "-", detail))
        while self._refusals and self._refusals[0][0] < now - STORM_WINDOW_S:
            self._refusals.popleft()
        if len(self._refusals) >= STORM_COUNT and now >= self._storm_quiet_until:
            self._storm_quiet_until = now + STORM_COOLDOWN_S
            tools = collections.Counter(t for _ts, t, _d in self._refusals)
            self._push(P_HIGH, f"AI refusal storm: {len(self._refusals)} in "
                               f"{STORM_WINDOW_S / 60:.0f} min",
                       "Refused: " + ", ".join(f"{t} ×{c}" for t, c in tools.most_common())
                       + f"\nLatest: {_short(self._refusals[-1][2], 300)}\nRefusals move "
                         f"nothing; a session may be stuck or probing. (Next storm alert in "
                         f"{STORM_COOLDOWN_S / 60:.0f} min at the earliest.)",
                       key="refusal_storm")

    # ── the e-stop ────────────────────────────────────────────────────────

    def handle_estop_event(self, ev: Any) -> None:
        """An event from the e-stop stream: any *message* disarms every group."""
        if not isinstance(ev, dict) or ev.get("event") != "message":
            return
        if ev.get("topic") not in (None, self.s.estop_topic):
            log.warning("e-stop stream: ignoring a message for another topic")
            return
        mid = str(ev.get("id") or "")
        sent = ev.get("time")
        with self._lock:
            if mid:
                if mid in self._estop_ids:
                    return
                self._estop_ids[mid] = None
                while len(self._estop_ids) > 500:
                    self._estop_ids.popitem(last=False)
            if isinstance(sent, (int, float)) and sent < self.started_wall - 5:
                log.info("e-stop: ignoring a tap from %s, before this notifier started",
                         utc_iso(sent))
                return
            self.estops += 1
            log.warning("PHONE E-STOP received (sent %s) — disarming every AI group",
                        utc_iso(sent) if isinstance(sent, (int, float)) else "?")
            now_m = self._mono()
            armed = _armed_groups(self._status)
            created = self._begin_disarm(now_m, armed)
        ok = self._send_disarm()                        # never under self._lock
        with self._lock:
            self._end_disarm(ok, created)
            if not ok and self._estop_retry is None:
                self._estop_retry = {"since": now_m, "last": now_m, "armed": armed}
                self._push(P_HIGH, "E-stop received — broker unreachable",
                           "The phone e-stop arrived, but the notifier cannot reach the MQTT "
                           "broker, so the disarm is not sent yet; it retries every "
                           f"{ESTOP_RETRY_S:.0f} s. The AI service disarms itself when it "
                           "reconnects to the broker anyway. To be sure: sudo systemctl stop "
                           "xsphere-aicontrol", key="estop_no_broker")

    def _begin_disarm(self, now_m: float, armed: List[str]) -> bool:
        """Expect a confirmation *before* publishing: a status answering the
        disarm may arrive before the publish call returns.  Hold the lock."""
        created = self._estop_pending is None
        if created:
            self._estop_pending = {"at": now_m, "armed": armed}
        return created

    def _end_disarm(self, ok: bool, created: bool) -> None:
        if ok:
            self._estop_retry = None
        elif created:
            self._estop_pending = None

    def _send_disarm(self) -> bool:
        """Publish DISARM_ALL.  Never call it holding ``self._lock``: paho's
        network thread takes that lock in on_message, and a publish may wait
        on paho's own locks."""
        if not self._transport.is_connected():
            return False
        return self._publish(self.s.arm_topic, DISARM_ALL, qos=1, retain=False)

    def _retry_disarm(self, now_m: float) -> None:
        """A disarm that could not be sent yet (the broker was down)."""
        with self._lock:
            r = self._estop_retry
            if r is None or now_m - r["last"] < ESTOP_RETRY_S:
                return
            r["last"] = now_m
            created = self._begin_disarm(now_m, r["armed"])
        ok = self._send_disarm()
        with self._lock:
            self._end_disarm(ok, created)
            if not ok and self._estop_retry is r and now_m - r["since"] >= ESTOP_GIVE_UP_S:
                self._estop_retry = None
                self._push(P_URGENT, "E-stop NOT delivered",
                           f"The broker was unreachable for {fmt_dur(ESTOP_GIVE_UP_S)}; the "
                           f"disarm was never sent. Disarm on the Control page, or: sudo "
                           f"systemctl stop xsphere-aicontrol", key="estop_failed")

    def _estop_confirmed(self) -> None:
        p, self._estop_pending = self._estop_pending, None
        were = ", ".join(p["armed"]) if p["armed"] else None
        self._push(P_DEFAULT, "AI disarmed by the phone e-stop",
                   (f"Disarmed: {were}." if were else "Nothing was armed — the button works.")
                   + " Arm again only from the Control page.", key="estop_ok",
                   tags=["white_check_mark"])

    def _check_estop(self, now_m: float) -> None:
        p = self._estop_pending
        if p is not None and now_m - p["at"] >= ESTOP_CONFIRM_S:
            self._estop_pending = None
            if self.ai_phase == "down":
                why = ("The AI control service is down, and nothing can be armed while it is "
                       "(it starts disarmed).")
            else:
                why = (f"Its status still shows: {', '.join(_armed_groups(self._status))} "
                       f"armed." if _armed_groups(self._status) else
                       "No fresh status from the AI service since.")
            self._push(P_HIGH, "E-stop sent, disarm NOT confirmed",
                       f"The disarm went out {fmt_dur(now_m - p['at'])} ago. {why} Check "
                       f"the Control page; to be sure: sudo systemctl stop xsphere-aicontrol",
                       key="estop_unconfirmed")
        lis = self.estop_listener
        if lis is not None:
            if lis.connected:
                self._estop_listener_alerted = False
            elif (lis.down_since is not None and not self._estop_listener_alerted
                  and now_m - lis.down_since >= ESTOP_LISTENER_ALERT_S):
                self._estop_listener_alerted = True
                self._push(P_DEFAULT, "phone e-stop not listening",
                           f"The notifier has not reached the e-stop topic for "
                           f"{fmt_dur(now_m - lis.down_since)} ({lis.error or 'no answer'}). "
                           f"'Disarm AI' does nothing until it reconnects; use the Control "
                           f"page.", key="estop_listener")

    # ── this notifier's broker ─────────────────────────────────────────────

    def _check_broker(self, now_m: float) -> None:
        if self._transport.is_connected():
            if self._broker_down is not None:
                lost = now_m - self._broker_down
                if self._broker_alerted:
                    self._push(P_DEFAULT, "notifier back on the broker",
                               f"MQTT broker reachable again after {fmt_dur(lost)}.",
                               key="broker_up")
                elif self._broker_deferred:
                    self._maint_log.append(f"MQTT broker unreachable for {fmt_dur(lost)} "
                                           f"inside the maintenance window")
                self._broker_down = None
                self._broker_alerted = self._broker_deferred = False
                if self._broker_since is None:
                    self._broker_since = now_m
            return
        if self._broker_down is None:
            self._broker_down = now_m
            self._broker_since = None
            log.warning("MQTT broker lost")
        if self._broker_alerted or now_m - self._broker_down < BROKER_LOST_AFTER_S:
            return
        last = self._last_online if self.ai_phase != "down" else None
        if not _risky(last) and self._in_maintenance(self._clock()):
            self._broker_deferred = True
            return
        self._broker_alerted = True
        what = ""
        if _risky(last):
            what = (" At the last AI status: armed " + (", ".join(_armed_groups(last)) or
                                                       "nothing")
                    + (f"; LN2 timers {', '.join(_timers(last))} cannot be closed by the "
                       f"service until the broker is back" if _timers(last) else "") + ".")
        self._push(P_URGENT if _timers(last) else P_HIGH, "notifier lost the MQTT broker",
                   f"No broker for {fmt_dur(now_m - self._broker_down)}: the notifier cannot "
                   f"watch the AI service, and slow control and the AI service lose it too "
                   f"(the AI service disarms everything when it reconnects).{what}",
                   key="broker_lost")

    # ── schedule ──────────────────────────────────────────────────────────

    def _in_maintenance(self, ts: float) -> Optional[Tuple[float, float]]:
        for w in self.s.maintenance:
            b = w.bounds_around(ts, self.tz)
            if b:
                return b
        return None

    def _next_maintenance(self, after: float) -> Optional[Tuple[float, Window]]:
        cands = [(w.next_start(after, self.tz), w) for w in self.s.maintenance]
        return min(cands, key=lambda c: c[0]) if cands else None

    def _check_schedule(self, now: float) -> None:
        # A time more than SCHEDULE_LATE_S past is skipped, not sent: the
        # clock jumped (NTP at boot), it did not just come round.
        if now >= self._next_daily:
            late = now - self._next_daily
            self._next_daily = next_daily(now, self.tz, *self.s.daily_digest)
            if late <= SCHEDULE_LATE_S:
                self._last_daily = now
                self._push(P_LOW, f"daily AI digest — {to_local(now, self.tz):%a %d %b}",
                           self.daily_digest(now), key="daily")
        if self._next_warn is not None and now >= self._next_warn:
            late = now - self._next_warn
            self._next_warn = next_weekly(now, self.tz, *self.s.maintenance_warn)
            if late <= SCHEDULE_LATE_S:
                self._maintenance_warning(now)
        # A deferred outage that outlasts its window is reported now.
        if not self._in_maintenance(now):
            d = self._down
            if d is not None and d["deferred"] and not d["alerted"]:
                d["deferred"] = False
                d["why"] += " (and it is still down after the maintenance window)"
                self._check_down(self._mono())
            if self._broker_deferred and not self._broker_alerted:
                self._broker_deferred = False
                self._check_broker(self._mono())

    def _maintenance_warning(self, now: float) -> None:
        st = self._status if self.ai_phase != "down" else None
        armed, timers, ramps = _armed_groups(st), _timers(st), _ramps(st)
        if not (armed or timers or ramps):
            return
        nxt = self._next_maintenance(now)
        when = f"{self._hm(nxt[0], day=True)}" if nxt else "tonight"
        arms = (st or {}).get("arms") or {}
        bits = []
        for g in armed:
            exp = parse_utc((arms.get(g) or {}).get("expires_utc"))
            bits.append(f"{g} (until {self._hm(exp, day=True)})" if exp else g)
        lines = [f"Armed: {', '.join(bits) or 'nothing'}."]
        if timers:
            lines.append(f"LN2 timers pending: {', '.join(timers)}.")
        if ramps:
            lines.append(f"Ramps: {', '.join(str(r.get('zone')) for r in ramps)}.")
        lines.append(f"The IOTstack backup at {when} restarts the MQTT broker for about 15 "
                     f"min: slow control restarts, the AI service disarms everything, and an "
                     f"LN2 close that falls due then waits for the broker. Disarm, or make "
                     f"sure nothing needs the AI then.")
        self._push(P_HIGH, "armed before the Sunday backup", "\n".join(lines),
                   key="maintenance_warning")

    def daily_digest(self, now: float) -> str:
        st = self._status if isinstance(self._status, dict) else None
        lines = []
        if self.ai_phase == "down" or st is None or st.get("online") != 1:
            since = self._down["since"] if self._down else None
            lines.append("AI control: DOWN" + (f" since {self._hm(since, day=True)}"
                                              if since else "") if st is not None
                         else "AI control: no status seen")
        else:
            lines.append(f"AI control: online, version {st.get('version')}, up since "
                         f"{self._hm(parse_utc(st.get('started_utc')), day=True)}")
            arms = st.get("arms") or {}
            armed = _armed_groups(st)
            if armed:
                lines.append("Armed: " + ", ".join(
                    f"{g} by {(arms.get(g) or {}).get('by')} until "
                    f"{self._hm(parse_utc((arms.get(g) or {}).get('expires_utc')), day=True)}"
                    for g in armed))
            else:
                lines.append("Armed: nothing")
            if _timers(st):
                lines.append("LN2 timers: " + ", ".join(_timers(st)))
            if _ramps(st):
                lines.append("Ramps: " + ", ".join(str(r.get("zone")) for r in _ramps(st)))
            rails = st.get("rails") if isinstance(st.get("rails"), dict) else {}
            lines.append(f"Rails: revision {rails.get('revision', '?')}, saved by "
                         f"{st.get('rails_updated_by') or '—'} "
                         f"{self._hm(parse_utc(st.get('rails_updated_utc')), day=True)}"
                         + (f" — ERROR: {st['rails_error']}" if st.get("rails_error") else ""))
        for name, label in (("pneumatic", "Pneumatic valve control"),
                            ("turbo", "Turbo pump control")):
            g = self._gates.get(name)
            if not isinstance(g, dict):
                lines.append(f"{label}: no status")
            elif g.get("enabled") == 1:
                since = parse_utc(g.get("since_utc"))
                lines.append(f"{label}: ENABLED" + (f" since {self._hm(since, day=True)} "
                                                    f"({fmt_dur(now - since)})" if since
                                                    else ""))
            else:
                lines.append(f"{label}: disabled")
        lines.append("")
        lines.append("Last 24 h:")
        entries = [e for e in self.ring if now - 86400 <= e["_ts"] <= now]
        kinds = collections.Counter(e.get("kind") for e in entries)
        lines.append("  " + (", ".join(f"{k} ×{c}" for k, c in sorted(kinds.items(),
                                                                     key=lambda x: str(x[0])))
                             or "no AI activity"))
        for e in entries:
            if e.get("kind") in ("arm", "disarm", "auto_disarm", "rails", "auto_close"):
                lines.append(f"  {self._hm(e['_ts'])} {_short(e.get('detail', ''), 160)}")
        if entries:
            lines += ["  " + x for x in self._activity_lines(now - 86400, now,
                                                             notes_in_full=False)]
        if self._maint_log:
            lines += ["  " + x for x in self._maint_log]
            self._maint_log = []
        nxt = self._next_maintenance(now)
        if nxt:
            lines.append(f"Next maintenance window: {self._hm(nxt[0], day=True)} "
                         f"({nxt[1].text}: the weekly IOTstack backup restarts the broker)")
        lis = self.estop_listener
        lines.append("")
        lines.append(f"Notifier: up {fmt_dur(now - self.started_wall)}; pushes sent "
                     f"{self.push_stats['sent']}, failed {self.push_stats['failed']}, dropped "
                     f"{self.push_stats['dropped']}; phone e-stop "
                     f"{'listening' if lis and lis.connected else 'NOT listening'}; broker "
                     f"{'connected' if self._transport.is_connected() else 'NOT connected'}")
        return "\n".join(lines)

    # ── tick ──────────────────────────────────────────────────────────────

    def tick(self) -> None:
        now_m = self._mono()
        self._retry_disarm(now_m)
        with self._lock:
            self._check_broker(now_m)
            self._check_silence(now_m)
            self._check_down(now_m)
            self._check_recovery(now_m)
            self._check_estop(now_m)
            self._flush_digests(now_m)
            self._check_schedule(self._clock())
            own_due = now_m - self._own_status_at >= OWN_STATUS_EVERY_S
        if own_due:                                     # publish outside the lock
            self._publish_own_status()

    # ── pushing ───────────────────────────────────────────────────────────

    def _hm(self, ts: Optional[float], day: bool = False) -> str:
        if ts is None:
            return "?"
        dt = to_local(ts, self.tz)
        same_day = to_local(self._clock(), self.tz).date() == dt.date()
        return dt.strftime("%H:%M" if same_day and not day else "%a %d %b %H:%M")

    def _action(self) -> dict:
        return {"action": "http", "label": "Disarm AI", "url": self.s.estop_url,
                "method": "POST", "body": "disarm", "clear": True}

    def _push(self, priority: int, title: str, message: str, *, key: str,
              tags: Optional[List[str]] = None) -> Push:
        p = Push(priority=priority, title=f"xsphere: {title}", message=message, key=key,
                 created=self._clock(), tags=list(tags or TAGS.get(priority, [])))
        (log.warning if priority >= P_HIGH else log.info)("push [%s] p%d %s", key, priority,
                                                          title)
        with self._out_cond:
            self.outbox.append(p)
            self._out_cond.notify_all()
        return p

    def pump_outbox(self) -> int:
        """Send what is queued (call from the sender thread, or from a test).
        Returns how many pushes went out."""
        now_m, now = self._mono(), self._clock()
        if now_m < self._retry_at:
            return 0
        sent = 0
        while True:
            with self._out_cond:
                if not self.outbox:
                    break
                p = self.outbox[0]
            if now - p.created > PUSH_MAX_AGE_S:
                with self._out_cond:
                    self.outbox.popleft()
                self.push_stats["dropped"] += 1
                log.warning("push [%s] dropped: undeliverable for %s", p.key,
                            fmt_dur(now - p.created))
                continue
            self._prune_budget(now_m)
            if len(self._sent_times) >= PUSH_BUDGET_PER_HOUR and p.priority < P_URGENT:
                with self._out_cond:
                    self.outbox.popleft()
                self._held.append(p)
                self.push_stats["held"] += 1
                continue
            if not self._send(p, now):
                return sent
            with self._out_cond:
                self.outbox.popleft()
            self._sent_times.append(now_m)
            sent += 1
        self._prune_budget(now_m)
        if self._held and len(self._sent_times) < PUSH_BUDGET_PER_HOUR:
            held = self._held
            summary = Push(priority=max(h.priority for h in held),
                           title=f"xsphere: {len(held)} more alert(s)",
                           message="Held back by the push budget "
                                   f"({PUSH_BUDGET_PER_HOUR} an hour):\n" + "\n".join(
                                       f"{self._hm(h.created)} {h.title[len('xsphere: '):]}: "
                                       f"{_short(h.message, 140)}" for h in held),
                           key="held", created=now, tags=TAGS[P_HIGH])
            if self._send(summary, now):
                self._held = []
                self._sent_times.append(now_m)
                sent += 1
        return sent

    def _prune_budget(self, now_m: float) -> None:
        while self._sent_times and now_m - self._sent_times[0] >= 3600:
            self._sent_times.popleft()

    def _send(self, p: Push, now: float) -> bool:
        message = p.message
        if now - p.created > 60:
            message = f"(Delayed: this happened at {self._hm(p.created, day=True)}.)\n{message}"
        parts = split_message(message)
        try:
            for i in range(p.next_part, len(parts)):
                title = p.title if len(parts) == 1 else f"{p.title} ({i + 1}/{len(parts)})"
                self._ntfy.publish(self.s.alert_topic, title=title, message=parts[i],
                                   priority=p.priority, tags=p.tags, actions=[self._action()],
                                   click=self.s.click_url)
                p.next_part = i + 1
        except NtfyError as e:
            self.push_stats["failed"] += 1
            self.last_push_error = str(e)
            self._retry_at = self._mono() + PUSH_RETRY_S
            log.warning("push [%s] failed (%s); retrying in %.0f s", p.key, e, PUSH_RETRY_S)
            return False
        self.push_stats["sent"] += 1
        self.last_push_utc = utc_iso(now)
        self.last_push_error = None
        return True


# ──────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────

def _env_with_file(path: Optional[str]) -> Dict[str, str]:
    env = dict(os.environ)
    if path:
        if not os.path.exists(path):
            raise ConfigError(f"env file {path} does not exist")
        try:
            mode = os.stat(path).st_mode & 0o077
        except OSError:
            mode = 0
        if mode and os.name == "posix":
            log.warning("%s is readable by others (mode %o): it holds the topic names; "
                        "chmod 600 it", path, os.stat(path).st_mode & 0o777)
        env.update(load_env_file(path))
    return env


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aicontrol.notify",
                                 description="Phone alerts and a disarm-only phone e-stop for "
                                             "the xsphere AI control service.")
    ap.add_argument("--env-file", help="read settings from this KEY=value file too (its "
                                       "values win); the unit uses EnvironmentFile= instead")
    ap.add_argument("--check", action="store_true", help="validate the settings and exit")
    ap.add_argument("--test", action="store_true",
                    help="send one test push (with the Disarm AI button) and exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("XSPHERE_NOTIFY_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    try:
        settings = NotifySettings.from_env(_env_with_file(args.env_file))
    except ConfigError as e:
        log.error("Refusing to start: %s", e)
        return 2
    for line in settings.describe():
        log.info("%s", line)
    if args.check:
        return 0
    ntfy = NtfyClient(settings.ntfy_server, settings.ntfy_token)
    if args.test:
        try:
            ntfy.publish(settings.alert_topic, title="xsphere: test push",
                         message="The notifier can reach your phone. 'Disarm AI' posts to "
                                 "the e-stop topic; with the notifier running it disarms "
                                 "every AI group and confirms here.",
                         priority=P_DEFAULT, tags=["test_tube"],
                         actions=[{"action": "http", "label": "Disarm AI",
                                   "url": settings.estop_url, "method": "POST",
                                   "body": "disarm", "clear": True}],
                         click=settings.click_url)
        except NtfyError as e:
            log.error("Test push failed: %s", e)
            return 1
        log.info("Test push sent")
        return 0

    notifier = Notifier(settings, ntfy=ntfy)
    stop = threading.Event()

    def _on_term(signum, _frame):
        log.warning("Signal %d received — stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    notifier.start()
    try:
        while not stop.wait(1.0):
            pass
    finally:
        notifier.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
