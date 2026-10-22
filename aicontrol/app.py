"""
The AI control service process: wiring, HTTP routes, main().

:class:`Service` builds every part (settings → zone map → rails → bus → audit
→ arms → LN2 timers → ramps → actions → supervisor → history → reads → tool
registry), connects the human-only MQTT commands (``<cmd_prefix>/arm`` and
``/rails``) and publishes the retained status every 2 s and on every change.

:func:`create_app` is the Flask side.  Every route sits behind a fixed Host
allowlist (421 otherwise — DNS rebinding would defeat any "same host" check,
and Claude Code treats a 401/403 as "needs auth").  ``/mcp`` and ``/api/v1``
refuse any request carrying an ``Origin`` header (no browser page should ever
call them; same-origin GETs from the status page carry none) and any POST
that is not ``application/json`` (a cross-site ``text/plain`` form post is a
"simple" request browsers send without asking).  There are no human write
endpoints on this port: arming and rails are MQTT commands from the Control
page only.

Routes::

    POST /mcp                       MCP (Streamable HTTP, JSON responses; GET/DELETE → 405)
    GET  /healthz                   liveness
    GET  /                          read-only status page
    GET  /api/v1/overview           get_overview      (?format=json)
    GET  /api/v1/states             list_states       (?group=&query=&limit=)
    GET  /api/v1/states/<id>        get_states for one id
    GET  /api/v1/history            query_history     (?ids=a,b&start=&stop=&every=&fn=&max_points=)
    GET  /api/v1/ai_control         get_ai_control
    GET  /api/v1/guide              get_guide         (?topic=)
    POST /api/v1/actions/<tool>     any tool, JSON-object body = its arguments

REST answers 200 when the tool ran, 400 for invalid arguments, 404 for an
unknown tool, 409 for a refused (or failed) write, 500 for an internal error.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from flask import Flask, Response, request
from werkzeug.exceptions import RequestEntityTooLarge

from . import __version__
from .actions import Actions
from .arms import (PV_INTERLOCK_TOPIC, ArmState, Ln2Timers, Ramps, Supervisor, default_mono,
                   utc_iso)
from .audit import Audit
from .bus import OFFLINE_STATUS, Bus, loads_strict
from .config import ConfigError, Settings
from .rails import RailsStore
from .reads import Reads, may_now
from .tools import INSTRUCTIONS, ToolRegistry
from .zones import ZoneMap

log = logging.getLogger(__name__)

MAX_BODY = 1024 * 1024
STATUS_INTERVAL_S = 2.0
_INDEX_HTML = Path(__file__).resolve().parent / "templates" / "index.html"
_AUTO = object()


def _utc_ms(ts: float) -> str:
    return (datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


# ──────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────

class Service:
    """One running AI control service, every part wired together.

    Nothing starts until :meth:`start`; tests build one on a
    LoopbackTransport and call ``start(threads=False)``, then drive the
    supervisor and ramps with ``tick()`` on a fake clock.
    """

    def __init__(self, settings: Settings, transport=None, *,
                 clock: Callable[[], float] = time.time,
                 mono: Optional[Callable[[], float]] = None,
                 zone_map: Optional[ZoneMap] = None,
                 history: Any = _AUTO, confirm_timeout_s: float = 5.0,
                 nge_timeout_s: float = 5.0, ramp_interval_s: float = 10.0,
                 status_interval_s: float = STATUS_INTERVAL_S):
        self.settings = settings
        self._clock = clock
        # Arm expiry and LN2 deadlines (default: time.monotonic, or the fake
        # clock a test injects).
        mono = mono if mono is not None else default_mono(clock)
        self.started_ts = clock()
        self.status_interval_s = status_interval_s
        self._stop = threading.Event()
        self._threads = []
        self._shut = False
        self._shut_lock = threading.Lock()
        # Computing and publishing the status as one step, so an older payload
        # (paho, request, ramp and supervisor threads all publish) can never
        # overwrite a newer one.
        self._status_lock = threading.RLock()
        self.last_rails_result: Optional[dict] = None

        self.zones = zone_map if zone_map is not None else ZoneMap.from_state_yaml(
            settings.state_yaml)
        self.rails = RailsStore(settings.rails_path, self.zones, clock=clock)
        self.rails.load()
        self.bus = Bus(settings, transport, clock=clock)
        self.audit = Audit(settings.audit_path, publish=self.bus.publish_audit, clock=clock)
        self.arms = ArmState(self.rails, self.audit, clock=clock, mono=mono)
        self.timers = Ln2Timers(settings.runtime_path, clock=clock, mono=mono)
        self.timers.load()
        self.ramps = Ramps(self.arms, self.audit, clock=clock, interval_s=ramp_interval_s)
        self.actions = Actions(self.bus, self.zones, self.rails, self.arms, self.audit,
                               ramps=self.ramps, timers=self.timers, clock=clock,
                               confirm_timeout_s=confirm_timeout_s, nge_timeout_s=nge_timeout_s)
        self.supervisor = Supervisor(self.bus, self.arms, self.audit, self.zones, self.timers,
                                     clock=clock)
        self.history = self._make_history() if history is _AUTO else history
        self.reads = Reads(self.bus, self.zones, self.rails, self.arms, self.audit,
                           actions=self.actions, ramps=self.ramps, timers=self.timers,
                           history=self.history, status_fn=self.status_payload, clock=clock)
        self.registry = ToolRegistry(self.actions, self.reads)

        # Human-only commands (the Control page card).
        self.bus.on_command("arm", self.arms.handle_command)
        self.bus.on_command("rails", self._on_rails_command)
        # Status: on connect (bus), on every change, and every 2 s.
        self.bus.set_status_provider(self.status_payload)
        self.bus.on_connect_hook(self._on_connect_audit)
        self.arms.add_listener(lambda _g, _a, _r: self.publish_status())
        self.ramps.set_on_change(self.publish_status)
        self.timers.set_on_change(self.publish_status)
        self.actions.set_on_change(self.publish_status)

        if self.timers.error:
            self.audit.record("note", f"LN2 auto-close timers from before this start are lost: "
                                      f"{self.timers.error}")
        log.info("AI control service %s ready: %d tools, rails revision %d%s", __version__,
                 len(self.registry.names()), self.rails.revision,
                 f" (ERROR: {self.rails.error})" if self.rails.error else "")

    def _make_history(self):
        try:
            from .history import History
        except Exception:                            # noqa: BLE001 — degrade, not crash
            log.exception("history module not importable — query_history disabled")
            return None
        try:
            s = self.settings
            return History(s.influx_url, s.influx_org, s.influx_bucket, s.influx_token,
                           s.state_yaml, s.telegraf_conf)
        except Exception:                            # noqa: BLE001
            log.exception("history could not be set up — query_history disabled")
            return None

    def _on_connect_audit(self, _reconnect: bool) -> None:
        self.audit.flush_publish(force=True)
        # And once more from the status loop, after anything the client may
        # still send from before the reconnect.
        self.audit.pending = True

    # human rails command -----------------------------------------------------

    def _on_rails_command(self, payload: Any) -> None:
        by = payload.get("by") if isinstance(payload, dict) else None
        by = by.strip()[:80] if isinstance(by, str) and by.strip() else "unknown"
        rid = payload.get("request_id") if isinstance(payload, dict) else None
        ok, err, diff = self.rails.replace_from_command(payload, default_by="unknown")
        # request_id (the editor's nonce) lets the page that sent this save tell
        # its own answer from another tab's.
        self.last_rails_result = {"utc": _utc_ms(self._clock()), "ok": ok, "error": err,
                                  "revision": self.rails.revision,
                                  "request_id": rid if isinstance(rid, str) and len(rid) <= 64
                                  else None}
        if ok:
            self.audit.record("rails", f"rails revision {self.rails.revision} saved by {by}: "
                                       f"{diff}", by=by)
        else:
            log.warning("Rails save from %s refused: %s", by, err)
            self.audit.record("rails_refused", f"rails save refused: {err}", by=by)
        self.publish_status()

    # status ------------------------------------------------------------------

    def status_payload(self) -> dict:
        arms = self.arms.snapshot()
        stats = self.actions.stats()
        r = self.bus.raw(PV_INTERLOCK_TOPIC)
        il = r["payload"] if r and isinstance(r["payload"], dict) else None
        return {
            "online": 1,
            "instance": self.settings.instance,
            "version": __version__,
            "started_utc": utc_iso(self.started_ts),
            "armed_heaters": int(arms["heaters"]["armed"]),
            "armed_ln2": int(arms["ln2"]["armed"]),
            "armed_pneumatic": int(arms["pneumatic"]["armed"]),
            "arms": arms,
            "rails": self.rails.stored(),
            "rails_error": self.rails.error,
            "rails_stale_zones": self.rails.stale_zones,
            "rails_updated_utc": self.rails.updated_utc,
            "rails_updated_by": self.rails.updated_by,
            "may_now": may_now(self.arms, self.rails, self.bus),
            "ramps": self.ramps.status(),
            "ln2_timers": self.timers.status(),
            "actions": stats["actions"],
            "refused": stats["refused"],
            "notes": stats["notes"],
            "auto_disarms": self.arms.auto_disarms,
            "last_action": stats["last_action"],
            "last_refused": stats["last_refused"],
            "last_note": stats["last_note"],
            "last_rails_result": self.last_rails_result,
            "zones": self.zones.describe_all(),
            "interlock": ({"min_k": il.get("min_k"), "max_k": il.get("max_k"),
                           "tripped": il.get("tripped")} if il else None),
        }

    def publish_status(self) -> bool:
        """Publish the retained status now (no-op while disconnected)."""
        if not self.bus.connected():
            return False
        try:
            with self._status_lock:
                return self.bus.publish_status(self.status_payload())
        except Exception:
            log.exception("Could not publish the status")
            return False

    def _status_loop(self) -> None:
        while not self._stop.wait(self.status_interval_s):
            self.publish_status()
            try:
                self.actions.flush_unaudited()
            except Exception:
                log.exception("Could not audit the refusals that were only counted")
            if self.audit.pending:
                self.audit.flush_publish()

    # lifecycle ---------------------------------------------------------------

    def start(self, threads: bool = True) -> None:
        self.bus.start()
        if threads:
            self.supervisor.start()
            self.ramps.start()
            t = threading.Thread(target=self._status_loop, name="aicontrol-status", daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        """Go offline: stop the loops, publish the retained offline status
        (waiting ≤ 2 s for it to leave), disconnect.  Idempotent."""
        with self._shut_lock:
            if self._shut:
                return
            self._shut = True
        self._stop.set()
        self.supervisor.stop()
        self.ramps.stop_thread()
        stopped = self.ramps.stop(None, "the AI control service is stopping")
        if stopped:
            log.warning("Ramps stopped at shutdown: %s", ", ".join(stopped))
        offline = dict(OFFLINE_STATUS, instance=self.settings.instance,
                       stopped_utc=utc_iso(self._clock()))
        self.bus.stop(offline_payload=offline, wait_s=2.0)
        self.audit.close()
        log.info("AI control service stopped")


# ──────────────────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────────────────

def _json_safe(obj: Any) -> Any:
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _json(obj: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> Response:
    try:
        text = json.dumps(obj, ensure_ascii=False, allow_nan=False, default=str)
    except ValueError:
        obj = _json_safe(obj)
        text = json.dumps(obj, ensure_ascii=False, allow_nan=False, default=str)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate (e.g. in an audit entry's "by") has no UTF-8 form;
        # \u-escaping everything is still the same JSON.
        text = json.dumps(obj, ensure_ascii=True, allow_nan=False, default=str)
    resp = Response(text, status=status, mimetype="application/json")
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _is_json_type(content_type: Optional[str]) -> bool:
    return (content_type or "").split(";", 1)[0].strip().lower() == "application/json"


def _mcp_error(status: int, message: str) -> Response:
    return _json({"jsonrpc": "2.0", "error": {"code": -32600, "message": message}}, status)


def _api_error(path: str, status: int, message: str) -> Response:
    """A transport-level refusal: JSON-RPC shaped on /mcp, plain JSON elsewhere."""
    if path == "/mcp":
        return _mcp_error(status, message)
    return _json({"ok": False, "error": message}, status)


class _BadQuery(ValueError):
    pass


def create_app(service: Service) -> Flask:
    """The Flask app for one :class:`Service`."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    settings = service.settings
    registry = service.registry
    mcp_mod: Dict[str, Any] = {}

    def is_api(path: str) -> bool:
        return path == "/mcp" or path == "/api/v1" or path.startswith("/api/v1/")

    @app.before_request
    def _guard():
        host = request.headers.get("Host")
        if not settings.host_allowed(host):
            log.warning("HTTP %s %s refused: Host %r not allowed (421)", request.method,
                        request.path, host)
            return Response("Misdirected Request: this host name is not served here\n",
                            status=421, mimetype="text/plain")
        path = request.path
        if not is_api(path):
            return None
        if path == "/mcp" and request.method != "POST":
            return None                              # mcp.handle answers 405 + Allow: POST
        if "Origin" in request.headers:
            log.warning("HTTP %s %s refused: Origin %r (403)", request.method, path,
                        request.headers.get("Origin"))
            msg = "Forbidden: browser requests (Origin header) are not accepted here"
            return _api_error(path, 403, msg)
        if request.method == "POST" and not _is_json_type(request.headers.get("Content-Type")):
            msg = "Unsupported Media Type: send Content-Type: application/json"
            return _api_error(path, 415, msg)
        return None

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_e):
        return _api_error(request.path, 413, f"Payload Too Large: limit {MAX_BODY} bytes")

    @app.errorhandler(404)
    def _not_found(_e):
        if is_api(request.path):
            return _json({"ok": False, "error": f"no such endpoint: {request.path}"}, 404)
        return Response("not found\n", status=404, mimetype="text/plain")

    @app.errorhandler(405)
    def _bad_method(_e):
        return _json({"ok": False, "error": f"method {request.method} not allowed here"}, 405)

    # MCP -----------------------------------------------------------------

    # Every method reaches mcp.handle, which answers anything but POST with
    # 405 + Allow: POST (Flask would otherwise answer OPTIONS itself).
    @app.route("/mcp", methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"],
               provide_automatic_options=False)
    def mcp_endpoint():
        if "handle" not in mcp_mod:
            try:
                from . import mcp as module
                mcp_mod["handle"] = module.handle
            except Exception:                         # noqa: BLE001
                log.exception("MCP module not importable")
                return _mcp_error(503, "MCP endpoint unavailable")
        body = request.get_data(cache=False) if request.method == "POST" else b""
        status, headers, data = mcp_mod["handle"](request.method, request.headers, body,
                                                  registry, INSTRUCTIONS,
                                                  server_version=__version__)
        resp = Response(data, status=status)
        for k, v in headers.items():
            resp.headers[k] = v
        return resp

    # plain pages -----------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        age = service.bus.snapshot_age()
        return _json({"ok": True, "service": "xsphere-aicontrol", "version": __version__,
                      "instance": settings.instance, "mqtt_connected": service.bus.connected(),
                      "snapshot_age_s": round(age, 1) if age is not None else None,
                      "armed": service.arms.armed_groups()})

    @app.get("/")
    def index():
        try:
            html = _INDEX_HTML.read_text(encoding="utf-8")
        except OSError:
            return Response("status page template missing\n", status=404, mimetype="text/plain")
        return Response(html, mimetype="text/html")

    # REST ------------------------------------------------------------------

    def run_tool(name: str, args: Any) -> Response:
        if not registry.has(name):
            return _json({"ok": False, "tool": name, "error": f"unknown tool: {name}"}, 404)
        is_error, result = registry.call(name, args)
        if not is_error:
            status = 200
        elif isinstance(result, dict) and result.get("invalid_arguments"):
            status = 400
        elif isinstance(result, dict) and result.get("internal"):
            status = 500
        elif registry.is_write(name):
            status = 409
        else:
            status = 400
        if isinstance(result, str):
            return Response(result, status=status, mimetype="text/plain")
        return _json(result, status)

    def query(allowed: Tuple[str, ...]) -> Dict[str, str]:
        extra = [k for k in request.args if k not in allowed]
        if extra:
            raise _BadQuery(f"unknown query parameter {extra[0]!r} (allowed: "
                            f"{', '.join(allowed) or 'none'})")
        out = {}
        for k in allowed:
            vals = request.args.getlist(k)
            if len(vals) > 1:
                raise _BadQuery(f"query parameter {k!r} given twice")
            # An empty value (``stop=`` in a filled-in template) means "not given".
            if vals and vals[0].strip():
                out[k] = vals[0]
        return out

    def int_param(q: Dict[str, str], key: str) -> None:
        if key in q:
            text = q[key].strip()
            # ASCII digits only: str.isdigit() also accepts "²" (which int()
            # then refuses) and "٣".
            if not re.fullmatch(r"[0-9]{1,6}", text):
                raise _BadQuery(f"{key} must be a positive integer")
            q[key] = int(text)

    def rest(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except _BadQuery as e:
                return _json({"ok": False, "error": f"invalid arguments: {e}",
                              "invalid_arguments": True}, 400)
        wrapper.__name__ = fn.__name__
        return wrapper

    @app.get("/api/v1/overview")
    @rest
    def api_overview():
        return run_tool("get_overview", query(("format",)))

    @app.get("/api/v1/states")
    @rest
    def api_states():
        q = query(("group", "query", "limit"))
        int_param(q, "limit")
        return run_tool("list_states", q)

    @app.get("/api/v1/states/<path:state_id>")
    @rest
    def api_state(state_id: str):
        query(())
        return run_tool("get_states", {"ids": [state_id]})

    @app.get("/api/v1/history")
    @rest
    def api_history():
        q = query(("ids", "start", "stop", "every", "fn", "max_points"))
        int_param(q, "max_points")
        if "ids" in q:
            q["ids"] = [s.strip() for s in q["ids"].split(",") if s.strip()]
        return run_tool("query_history", q)

    @app.get("/api/v1/ai_control")
    @rest
    def api_ai_control():
        query(())
        return run_tool("get_ai_control", {})

    @app.get("/api/v1/guide")
    @rest
    def api_guide():
        return run_tool("get_guide", query(("topic",)))

    @app.post("/api/v1/actions/<tool>")
    def api_action(tool: str):
        raw = request.get_data(cache=False)
        if raw.strip():
            try:
                args = loads_strict(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError) as e:
                return _json({"ok": False, "tool": tool, "error": f"invalid JSON body: {e}",
                              "invalid_arguments": True}, 400)
        else:
            args = {}
        if not isinstance(args, dict):
            return _json({"ok": False, "tool": tool, "error": "the body must be a JSON object "
                                                              "(the tool's arguments)",
                          "invalid_arguments": True}, 400)
        return run_tool(tool, args)

    return app


# ──────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    logging.basicConfig(
        level=os.environ.get("XSPHERE_AICONTROL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        log.error("Refusing to start: %s", e)
        return 2
    settings.log_startup()
    try:
        service = Service(settings)
    except Exception:
        log.exception("Could not set up the AI control service")
        return 1
    app = create_app(service)

    def _on_term(signum, _frame):
        log.warning("Signal %d received — publishing offline status and stopping", signum)
        service.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_term)
    service.start()
    try:
        try:
            from waitress import serve
        except ImportError:
            log.warning("waitress is not installed — serving with Flask's built-in server")
            app.run(host=settings.http_host, port=settings.http_port, threaded=True,
                    use_reloader=False)
        else:
            log.info("Serving on http://%s:%d (waitress, 8 threads)", settings.http_host,
                     settings.http_port)
            serve(app, host=settings.http_host, port=settings.http_port, threads=8,
                  ident="xsphere-aicontrol")
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown()
    return 0
