"""
MQTT side of the AI control service: one client, caches, publish guards, waits.

What this module guarantees, whatever the code above it does:

* **A command allowlist at the publish layer.**  :meth:`Bus.publish_command`
  only ever emits the topics in :data:`ALLOWED_COMMAND_PATTERNS` (PID setpoint /
  mode / PV source, NGE output / voltage / current, LN2 valve state / auto
  flags, pneumatic valve state).  Anything else — the pneumatic gate, the AI's
  own arm / rails topics, the PV interlock limits, turbos, gradient, PID output
  or gains — raises :class:`ForbiddenTopicError`.  That is a programming error,
  not a refusal: no tool may reach those topics at all.
* **Commands are fire-once.**  qos 0, never retained, serialised with
  ``allow_nan=False`` and only sent while the client is connected.  paho keeps
  qos ≥ 1 messages queued across a broker outage and delivers them on
  reconnect; the Sunday IOTstack backup takes the broker down for ~15 min, so a
  queued "open valve" could land long after the tool reported "not confirmed".
  slowcontrol re-subscribes at qos 0 after a reconnect anyway, and the status
  topics are the real acknowledgement.
* **Honest ages.**  The snapshot's age is ``now - generated_at`` (the store's
  own clock, same host), never the time we received it: the broker replays the
  retained snapshot on every (re)subscribe, so a dead slowcontrol's last
  snapshot would otherwise look fresh.  For raw status topics we keep the
  retain flag, and :meth:`Bus.live_age` only counts deliveries that were *not*
  retained replays.
* **No NaN / Infinity.**  Incoming JSON with ``NaN``, ``Infinity`` or a float
  that overflows to inf is not decoded (kept as the raw string), so it can
  never reach a comparison as a number.
* **Retained commands are ignored.**  The command subscription asks the broker
  for no retained messages (MQTT 5 retainHandling = 2); a message that still
  arrives flagged retained is dropped with a warning.

Callbacks registered here (:meth:`Bus.on_command`, :meth:`Bus.on_status`,
:meth:`Bus.on_connect_hook`) run on the paho network thread.  They must not
block: no :meth:`Bus.wait_for`, no sleeping.  :meth:`Bus.wait_for` is for request
threads; it is woken by a Condition notified after every cache update, and the
cache lock is never held while waiting or while calling a callback.

Tests inject :class:`LoopbackTransport` instead of paho: no broker, no network.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

SNAPSHOT_TOPIC = "xsphere/state/snapshot"
STATUS_FILTER = "xsphere/status/#"

#: Retained last-will (and the explicit goodbye on SIGTERM): the Control page
#: then shows the service as down and nothing as armed.
OFFLINE_STATUS: Dict[str, int] = {"online": 0, "armed_heaters": 0, "armed_ln2": 0,
                                  "armed_pneumatic": 0}

#: The only command topics this service may ever publish (full match).
ALLOWED_COMMAND_PATTERNS: Tuple[Pattern[str], ...] = tuple(re.compile(p) for p in (
    r"xsphere/commands/pid/(top|bottom|nozzle)/(setpoint|mode|pv_expr)",
    r"xsphere/commands/nge/(1|2|3)/(output|voltage|current)",
    r"xsphere/commands/valve/(cryostat|primary_xe|ballast)/(state|auto_open|auto_close)",
    r"xsphere/commands/pneumatic/(ballast|pump|bottle)/state",
))


class ForbiddenTopicError(ValueError):
    """A caller tried to publish a command topic outside the allowlist."""


def is_allowed_command(topic: str) -> bool:
    return isinstance(topic, str) and any(p.fullmatch(topic) for p in ALLOWED_COMMAND_PATTERNS)


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):          # "1e999" parses to inf
        raise ValueError(f"non-finite JSON number {text}")
    return value


def loads_strict(text: str) -> Any:
    """``json.loads`` that refuses NaN, Infinity and floats that overflow."""
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)


def decode_payload(raw: bytes) -> Any:
    """MQTT payload → JSON value, or the text itself when it is not strict JSON."""
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(raw).decode("utf-8", errors="replace")
    try:
        return loads_strict(text)
    except ValueError:                    # JSONDecodeError is a ValueError
        return text


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _valid_snapshot(payload: Any) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("states"), dict):
        return False
    gen = payload.get("generated_at")
    try:
        return _is_number(gen) and math.isfinite(float(gen))
    except OverflowError:
        return False


# ──────────────────────────────────────────────────────────────────────────
# Transports
# ──────────────────────────────────────────────────────────────────────────

class PahoTransport:
    """The real broker connection (paho-mqtt 2.x, MQTT 5).

    ``connect_async`` + ``loop_start`` keeps retrying a refused first
    connection instead of raising, so the service survives a broker that is
    down at boot or during the weekly backup.  The retained last-will is set
    before connecting.
    """

    def __init__(self, settings, keepalive: int = 30):
        self._host = settings.mqtt_host
        self._port = settings.mqtt_port
        self._client_id = settings.client_id
        self._will_topic = settings.status_topic
        self._keepalive = keepalive
        self._client: Optional[mqtt.Client] = None

    def _build_client(self) -> mqtt.Client:
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=self._client_id, protocol=mqtt.MQTTv5)
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        client.will_set(self._will_topic, json.dumps(OFFLINE_STATUS), qos=1, retain=True)
        return client

    def start(self, on_message: Callable[[Any], None], on_connect: Callable[[], None]) -> None:
        client = self._build_client()

        def _on_connect(c, userdata, flags, reason_code, properties=None):
            if getattr(reason_code, "is_failure", False):
                log.error("MQTT connect refused by %s:%d: %s", self._host, self._port, reason_code)
                return
            log.info("MQTT connected to %s:%d as %s", self._host, self._port, self._client_id)
            on_connect()

        def _on_disconnect(c, userdata, flags, reason_code, properties=None):
            log.warning("MQTT disconnected (%s); paho will reconnect", reason_code)

        # A broker that is down only shows up here (paho retries silently
        # every 1–10 s); say so once a minute, not on every retry.
        last_fail = [0.0]

        def _on_connect_fail(c, userdata):
            now = time.monotonic()
            if now - last_fail[0] >= 60.0:
                last_fail[0] = now
                log.warning("MQTT broker %s:%d unreachable; still retrying", self._host,
                            self._port)

        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_connect_fail = _on_connect_fail
        client.on_message = lambda c, userdata, msg: on_message(msg)
        self._client = client
        log.info("Connecting to MQTT broker %s:%d (async)", self._host, self._port)
        client.connect_async(self._host, self._port, keepalive=self._keepalive)
        client.loop_start()

    def subscribe(self, topic_filter: str, qos: int = 0, no_retained: bool = False) -> None:
        if self._client is None:
            return
        if no_retained:
            from paho.mqtt.subscribeoptions import SubscribeOptions
            self._client.subscribe(topic_filter,
                                   options=SubscribeOptions(qos=qos, retainHandling=2))
        else:
            self._client.subscribe(topic_filter, qos=qos)

    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False,
                wait_s: float = 0.0) -> bool:
        if not self.is_connected():
            return False
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("MQTT publish on %s failed: rc=%s", topic, info.rc)
            return False
        if wait_s > 0:
            try:
                info.wait_for_publish(timeout=wait_s)
            except (ValueError, RuntimeError) as e:
                log.warning("MQTT publish on %s not completed: %s", topic, e)
                return False
            return info.is_published()
        return True

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()


class _LoopbackMessage:
    """Duck-types paho's MQTTMessage for the bus: .topic, .payload, .retain, .qos."""

    __slots__ = ("topic", "payload", "retain", "qos")

    def __init__(self, topic: str, payload: bytes, retain: bool, qos: int = 0):
        self.topic = topic
        self.payload = payload
        self.retain = retain
        self.qos = qos


class LoopbackTransport:
    """In-process broker stand-in for tests (no network, no paho client).

    * ``publish`` records ``(topic, decoded_payload, qos, retain)`` in
      :attr:`published`, keeps retained payloads like a broker, delivers the
      message to the bus if one of its subscriptions matches (as a live
      message: ``retain=False``), then runs every responder whose MQTT filter
      matches — that is how a fake slowcontrol answers commands with status.
    * ``inject`` is "some other client published this": delivered with the
      given retain flag (``retain=True`` simulates the broker replaying a
      retained message), and stored as retained when ``retain`` is set.
    * ``set_connected(False)`` makes publishes fail; ``set_connected(True)``
      after that is a reconnect with a clean session: subscriptions are
      dropped, the bus's on_connect runs again and re-subscribes, and the
      retained store is replayed (except to ``no_retained`` subscriptions).

    Delivery is synchronous, on the calling thread.
    """

    def __init__(self, connected: bool = True):
        self._lock = threading.RLock()
        self._connected = connected
        self._on_message: Optional[Callable[[Any], None]] = None
        self._on_connect: Optional[Callable[[], None]] = None
        self._retained: Dict[str, bytes] = {}
        self._responders: List[Tuple[str, Callable]] = []
        self.subscriptions: List[Tuple[str, int, bool]] = []
        self.published: List[Tuple[str, Any, int, bool]] = []
        self.fail_publish = False            # tests: make publish() return False

    # transport interface -------------------------------------------------

    def start(self, on_message: Callable[[Any], None], on_connect: Callable[[], None]) -> None:
        self._on_message, self._on_connect = on_message, on_connect
        if self._connected:
            on_connect()

    def subscribe(self, topic_filter: str, qos: int = 0, no_retained: bool = False) -> None:
        with self._lock:
            self.subscriptions.append((topic_filter, qos, no_retained))
            replay = [] if no_retained else [
                (t, p) for t, p in self._retained.items()
                if mqtt.topic_matches_sub(topic_filter, t)]
        for t, p in replay:
            self._deliver(t, p, retain=True)

    def is_connected(self) -> bool:
        return self._connected

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False,
                wait_s: float = 0.0) -> bool:
        if not self._connected or self.fail_publish:
            return False
        data = self._to_bytes(payload)
        with self._lock:
            self.published.append((topic, decode_payload(data), qos, retain))
            if retain:
                self._store_retained(topic, data)
            responders = [fn for flt, fn in self._responders if mqtt.topic_matches_sub(flt, topic)]
        self._deliver(topic, data, retain=False)
        decoded = decode_payload(data)
        for fn in responders:
            fn(topic, decoded, self)
        return True

    def stop(self) -> None:
        self._connected = False

    # test helpers --------------------------------------------------------

    def inject(self, topic: str, payload, retain: bool = False) -> bool:
        """Deliver a message from "another client".  Returns False when not connected."""
        data = self._to_bytes(payload)
        if retain:
            with self._lock:
                self._store_retained(topic, data)
        if not self._connected:
            return False
        return self._deliver(topic, data, retain=retain)

    def set_connected(self, connected: bool) -> None:
        was = self._connected
        self._connected = connected
        if connected and not was:
            with self._lock:
                self.subscriptions = []          # clean session
            if self._on_connect is not None:
                self._on_connect()

    def add_responder(self, topic_filter: str,
                      fn: Callable[[str, Any, "LoopbackTransport"], None]) -> None:
        """``fn(topic, decoded_payload, transport)`` after each matching publish."""
        with self._lock:
            self._responders.append((topic_filter, fn))

    def retained_payload(self, topic: str) -> Any:
        with self._lock:
            data = self._retained.get(topic)
        return None if data is None else decode_payload(data)

    def published_to(self, topic_filter: str) -> List[Tuple[str, Any, int, bool]]:
        with self._lock:
            return [p for p in self.published if mqtt.topic_matches_sub(topic_filter, p[0])]

    def clear(self) -> None:
        with self._lock:
            self.published = []

    # internals -----------------------------------------------------------

    @staticmethod
    def _to_bytes(payload) -> bytes:
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        if isinstance(payload, str):
            return payload.encode("utf-8")
        if payload is None:
            return b""
        return json.dumps(payload).encode("utf-8")   # allow_nan: tests inject NaN on purpose

    def _store_retained(self, topic: str, data: bytes) -> None:
        if data:
            self._retained[topic] = data
        else:
            self._retained.pop(topic, None)          # empty retained = clear

    def _deliver(self, topic: str, data: bytes, retain: bool) -> bool:
        with self._lock:
            matched = any(mqtt.topic_matches_sub(flt, topic) for flt, _q, _nr in self.subscriptions)
            cb = self._on_message
        if not matched or cb is None:
            return False
        cb(_LoopbackMessage(topic, data, retain))
        return True


# ──────────────────────────────────────────────────────────────────────────
# Bus
# ──────────────────────────────────────────────────────────────────────────

class Bus:
    """One MQTT connection plus the caches every other module reads."""

    def __init__(self, settings, transport=None, *, clock: Callable[[], float] = time.time):
        self.settings = settings
        self._clock = clock
        self._transport = transport if transport is not None else PahoTransport(settings)
        self._cmd_prefix = settings.cmd_prefix.rstrip("/")
        self._own_status = settings.status_topic.rstrip("/")

        self._lock = threading.Lock()                 # caches only; never held while waiting
        self._cond = threading.Condition()            # wakes wait_for()
        self._generation = 0

        self._snapshot: Optional[dict] = None
        self._snapshot_rx_ts: Optional[float] = None
        self._raw: Dict[str, dict] = {}

        self._command_cbs: Dict[str, List[Callable[[dict], None]]] = {}
        self._status_cbs: List[Tuple[str, Callable[[str, Any], None]]] = []
        self._connect_hooks: List[Callable[[bool], None]] = []
        self._status_provider: Optional[Callable[[], dict]] = None

        self._connects = 0
        self._started = False
        self._bad_snapshot_logged = 0.0

    # lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._transport.start(self._handle_message, self._handle_connect)

    def stop(self, offline_payload: Optional[dict] = None, wait_s: float = 2.0) -> None:
        """Publish the retained offline status (waiting ≤ ``wait_s``), then disconnect.

        A clean disconnect suppresses the broker's last-will, so the goodbye
        has to be published explicitly.
        """
        payload = dict(OFFLINE_STATUS) if offline_payload is None else offline_payload
        if self.connected():
            try:
                data = json.dumps(payload, allow_nan=False)
                self._transport.publish(self.settings.status_topic, data, qos=1, retain=True,
                                        wait_s=wait_s)
            except Exception:
                log.exception("Could not publish the offline status")
        try:
            self._transport.stop()
        except Exception:
            log.exception("Error stopping the MQTT transport")

    def connected(self) -> bool:
        try:
            return bool(self._transport.is_connected())
        except Exception:
            return False

    # registration --------------------------------------------------------

    def on_command(self, suffix: str, cb: Callable[[dict], None]) -> None:
        """``cb(payload_dict)`` for live (non-retained) JSON-object messages on
        ``<cmd_prefix>/<suffix>`` — the human arm / rails topics."""
        self._command_cbs.setdefault(suffix.strip("/"), []).append(cb)

    def on_status(self, topic_filter: str, cb: Callable[[str, Any], None]) -> None:
        """``cb(topic, payload)`` after the cache is updated for a matching
        status topic (retained replays included — check ``raw(topic)["retained"]``)."""
        self._status_cbs.append((topic_filter, cb))

    def on_connect_hook(self, cb: Callable[[bool], None]) -> None:
        """``cb(is_reconnect)`` after every successful connect (False the first time)."""
        self._connect_hooks.append(cb)

    def set_status_provider(self, fn: Callable[[], dict]) -> None:
        """The status payload to publish once on every (re)connect."""
        self._status_provider = fn

    # incoming ------------------------------------------------------------

    def _handle_connect(self) -> None:
        try:
            self._connects += 1
            is_reconnect = self._connects > 1
            t = self._transport
            t.subscribe(SNAPSHOT_TOPIC, qos=0)
            t.subscribe(STATUS_FILTER, qos=1)
            t.subscribe(f"{self._cmd_prefix}/#", qos=1, no_retained=True)
            log.info("Subscribed: %s, %s, %s/# (no retained)%s", SNAPSHOT_TOPIC, STATUS_FILTER,
                     self._cmd_prefix, " — reconnect" if is_reconnect else "")
            for hook in list(self._connect_hooks):
                try:
                    hook(is_reconnect)
                except Exception:
                    log.exception("Error in connect hook")
            if self._status_provider is not None:
                try:
                    self.publish_status(self._status_provider())
                except Exception:
                    log.exception("Could not publish status on connect")
        except Exception:
            log.exception("Error handling MQTT connect")
        finally:
            self.wake_waiters()

    def _handle_message(self, msg) -> None:
        # Everything is caught: an exception escaping a paho callback stops
        # the network loop for good.
        try:
            topic = msg.topic
            retained = bool(getattr(msg, "retain", False))
            payload = decode_payload(msg.payload or b"")
            if topic.startswith(self._cmd_prefix + "/"):
                self._dispatch_command(topic, payload, retained)
                return
            if topic == self._own_status or topic.startswith(self._own_status + "/"):
                # Production's status topic sits under xsphere/status/#: our
                # own status and audit come back to us.  Nothing reads them.
                return
            now = self._clock()
            if topic == SNAPSHOT_TOPIC:
                if _valid_snapshot(payload):
                    with self._lock:
                        self._snapshot = payload
                        self._snapshot_rx_ts = now
                elif now - self._bad_snapshot_logged > 60:
                    self._bad_snapshot_logged = now
                    log.warning("Ignoring a snapshot that is not a valid finite JSON object")
                return
            with self._lock:
                prev = self._raw.get(topic)
                self._raw[topic] = {
                    "payload": payload,
                    "rx_ts": now,
                    "retained": retained,
                    # A retained replay says nothing about when it was
                    # published: keep the last *live* receipt time.
                    "live_rx_ts": (prev["live_rx_ts"] if prev else None) if retained else now,
                    "seq": (prev["seq"] + 1) if prev else 1,
                    "prev_payload": prev["payload"] if prev else None,
                }
            for flt, cb in list(self._status_cbs):
                if mqtt.topic_matches_sub(flt, topic):
                    try:
                        cb(topic, payload)
                    except Exception:
                        log.exception("Error in status observer for %s", topic)
        except Exception:
            log.exception("Error handling MQTT message")
        finally:
            self.wake_waiters()

    def _dispatch_command(self, topic: str, payload: Any, retained: bool) -> None:
        if retained:
            log.warning("Ignoring RETAINED message on command topic %s — commands are never "
                        "retained (clear it on the broker)", topic)
            return
        if not isinstance(payload, dict):
            log.warning("Ignoring command on %s: payload is not a JSON object", topic)
            return
        suffix = topic[len(self._cmd_prefix) + 1:]
        cbs = list(self._command_cbs.get(suffix, ()))
        if not cbs:
            log.warning("No handler for command topic %s", topic)
        for cb in cbs:
            try:
                cb(payload)
            except Exception:
                log.exception("Error handling command on %s", topic)

    # caches --------------------------------------------------------------

    def snapshot(self) -> dict:
        """A deep copy of the latest valid snapshot ({} before the first)."""
        with self._lock:
            snap = self._snapshot
        return copy.deepcopy(snap) if snap is not None else {}

    def snapshot_age(self) -> Optional[float]:
        """Seconds since the store *generated* the snapshot (None if none yet)."""
        with self._lock:
            snap = self._snapshot
        if snap is None:
            return None
        return self._clock() - float(snap["generated_at"])

    def snapshot_rx_age(self) -> Optional[float]:
        """Seconds since a snapshot was last *received* (informational only)."""
        with self._lock:
            rx = self._snapshot_rx_ts
        return None if rx is None else self._clock() - rx

    def state(self, state_id: str) -> Optional[dict]:
        """The snapshot view of one state (deep copy), or None."""
        with self._lock:
            snap = self._snapshot
        if snap is None:
            return None
        view = snap["states"].get(state_id)
        return copy.deepcopy(view) if isinstance(view, dict) else None

    def state_value(self, state_id: str, max_age_s: Optional[float] = 5.0) -> Any:
        """The state's value only if its freshness is "fresh" AND the snapshot
        itself is at most ``max_age_s`` old (None skips the age check); else None."""
        with self._lock:
            snap = self._snapshot
        if snap is None:
            return None
        view = snap["states"].get(state_id)
        if not isinstance(view, dict) or view.get("freshness") != "fresh":
            return None
        if max_age_s is not None:
            age = self._clock() - float(snap["generated_at"])
            if age > max_age_s:
                return None
        return copy.deepcopy(view.get("value"))

    def raw(self, topic: str) -> Optional[dict]:
        """``{"payload", "rx_ts", "retained", "live_rx_ts", "seq", "prev_payload"}``
        for a status topic (deep copy), or None if never received.

        ``seq`` counts messages on the topic since start; with ``prev_payload``
        it lets a caller require a condition on two consecutive messages."""
        with self._lock:
            entry = self._raw.get(topic)
            return copy.deepcopy(entry) if entry is not None else None

    def raw_topics(self, prefix: str = "") -> List[str]:
        with self._lock:
            return sorted(t for t in self._raw if t.startswith(prefix))

    def live_age(self, topic: str) -> Optional[float]:
        """Seconds since the last NON-retained delivery on ``topic`` (None if never)."""
        with self._lock:
            entry = self._raw.get(topic)
            live = entry["live_rx_ts"] if entry else None
        return None if live is None else self._clock() - live

    def live_payload(self, topic: str, max_age_s: float) -> Any:
        """The latest payload, only if it was delivered live within ``max_age_s``."""
        with self._lock:
            entry = self._raw.get(topic)
            if entry is None or entry["live_rx_ts"] is None or entry["retained"]:
                return None
            if self._clock() - entry["live_rx_ts"] > max_age_s:
                return None
            return copy.deepcopy(entry["payload"])

    # outgoing ------------------------------------------------------------

    def publish_command(self, topic: str, payload: dict) -> bool:
        """Publish one allowlisted command (qos 0, not retained).

        Raises :class:`ForbiddenTopicError` for any other topic and ValueError
        for a payload holding NaN / Infinity.  Returns False — nothing sent —
        when not connected or when the client did not accept the message.
        """
        if not is_allowed_command(topic):
            log.error("REFUSED to publish forbidden command topic %r", topic)
            raise ForbiddenTopicError(f"command topic not allowed: {topic!r}")
        if not isinstance(payload, dict):
            raise TypeError(f"command payload must be a dict, not {type(payload).__name__}")
        data = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        if not self.connected():
            log.warning("Not publishing %s: MQTT is not connected", topic)
            return False
        ok = bool(self._transport.publish(topic, data, qos=0, retain=False))
        if ok:
            log.info("Published %s %s", topic, data)
        else:
            log.warning("Publish of %s %s was not accepted", topic, data)
        return ok

    def publish_status(self, payload: dict) -> bool:
        """The service's own retained status (only while connected)."""
        return self._publish_json(self.settings.status_topic, payload, qos=1, retain=True)

    def publish_audit(self, entries: list) -> bool:
        """``{"entries": [...]}`` retained on ``<status_topic>/audit``."""
        return self._publish_json(f"{self.settings.status_topic}/audit",
                                  {"entries": entries}, qos=1, retain=True)

    def _publish_json(self, topic: str, obj: Any, qos: int, retain: bool) -> bool:
        try:
            data = json.dumps(obj, allow_nan=False)
        except (TypeError, ValueError) as e:
            log.error("Not publishing %s: payload is not strict JSON (%s)", topic, e)
            return False
        if not self.connected():
            return False
        return bool(self._transport.publish(topic, data, qos=qos, retain=retain))

    # waiting -------------------------------------------------------------

    def wake_waiters(self) -> None:
        """Make every :meth:`wait_for` re-evaluate its predicate now."""
        with self._cond:
            self._generation += 1
            self._cond.notify_all()

    def wait_for(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """Block until ``predicate()`` is true (True) or ``timeout`` s pass (False).

        The predicate runs on this thread, outside every lock, after each
        incoming message (and at least every 0.5 s); an exception from it
        propagates.  Never call this from a bus callback — those run on the
        network thread that delivers the messages being waited for.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._cond:
                gen = self._generation
            if predicate():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self._cond:
                if self._generation == gen:
                    self._cond.wait(min(remaining, 0.5))
