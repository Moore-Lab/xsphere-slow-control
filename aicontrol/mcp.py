"""
MCP over Streamable HTTP: the protocol half of ``POST /mcp``.

handle() takes one HTTP request (method, headers, raw body) and returns
(status, headers, body).  It imports no Flask and keeps no per-client state,
so app.py only copies the request in and the tuple out, and every wire detail
below is covered by tests/test_aicontrol_mcp.py without a server.

Two protocol eras share the endpoint, because Claude Code (2.1.299 on the DAQ
PC) carries both client runtimes and either one may connect:

  modern  2026-07-28 -- stateless.  Every request carries
          ``_meta["io.modelcontextprotocol/protocolVersion"]`` and
          ``clientCapabilities``, and the MCP-Protocol-Version / Mcp-Method /
          Mcp-Name headers must repeat what the body says: something in
          between could route on a header while we execute the body, so the
          spec makes the server reject any disagreement.  Claude Code's v2
          runtime first probes with ``server/discover``.  A reply that fails
          its schema or does not echo the id makes it fall back to legacy
          without a word, and it remembers that verdict, so the discover and
          tools/list shapes here are exact: ttlMs an int (300000.0 fails its
          schema and no tools load), and no ``listChanged`` (with it, Claude
          Code opens subscriptions/listen, which is not served here).
  legacy  2025-11-25 / 2025-06-18 -- ``initialize`` handshake, then plain
          requests carrying the negotiated MCP-Protocol-Version header.  This
          is the MCP TypeScript SDK 1.x behind Claude Code's v1 runtime.
          2025-03-26 is never echoed: that revision allowed JSON-RPC batches,
          which are refused here.

Deliberately absent: sessions (no Mcp-Session-Id, ever -- optional in legacy,
removed in modern), SSE (a server may answer every request with one JSON
object and clients must accept that), the GET stream (405), batches, and
server-to-client requests.  A tool refusal is a *result* with isError true,
not a JSON-RPC error, so the model gets to read why it was refused; JSON-RPC
errors are kept for protocol faults (unknown tool, malformed request).

Host and Origin are checked by app.py for every route.  The Origin check is
repeated here as a backstop, because /mcp is the one route a browser page
must never reach (DNS rebinding).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import threading
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple

log = logging.getLogger(__name__)

MODERN = "2026-07-28"
#: Legacy revisions an ``initialize`` is answered with verbatim; any other
#: request is answered with LEGACY_DEFAULT (the spec lets the server pick).
LEGACY_ECHO = ("2025-11-25", "2025-06-18")
LEGACY_DEFAULT = "2025-11-25"
SERVER_NAME = "xsphere-aicontrol"

MAX_BODY = 1024 * 1024
#: Cache hint on server/discover and tools/list.  Must stay a Python int:
#: Claude Code validates it as an integer and drops the whole list otherwise.
TTL_MS = 300000
#: Claude Code cuts server instructions at 4096 characters.
MAX_INSTRUCTIONS = 4000
MODERN_METHODS = ("server/discover", "tools/list", "tools/call")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"

_SENTINEL_HEAD = "=?base64?"
_SENTINEL_TAIL = "?="
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_JSON_HEADERS = {"Content-Type": "application/json"}

Response = Tuple[int, Dict[str, str], bytes]


class ToolSource(Protocol):
    """What handle() needs from the tool registry (aicontrol/tools.py)."""

    def mcp_tools(self) -> List[dict]: ...

    def has(self, name: str) -> bool: ...

    def call(self, name: str, args: dict) -> Tuple[bool, object]:
        """Run a tool; returns (is_error, result).  A str result is sent as
        the text verbatim, anything else as JSON."""
        ...


# Things logged once per process: the first request of each era (to learn
# what the client really sends) and an over-long instructions text.
_once: set = set()
_once_lock = threading.Lock()


def _first_time(key: str) -> bool:
    with _once_lock:
        if key in _once:
            return False
        _once.add(key)
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle(method: str, headers: Mapping[str, str], body: bytes,
           tools: ToolSource, instructions: str,
           server_version: str = "1.0.0") -> Response:
    """Answer one HTTP request to the MCP endpoint.

    Returns (status, response headers, response body).  The checks run in a
    fixed order (spec section 11) because clients tell the eras apart by
    which error comes back first.
    """
    hdrs = _lower_headers(headers)

    if method != "POST":
        # SDK 1.x tries a GET for its optional SSE stream after every
        # initialize and accepts 405 -- routine traffic, not a warning.
        log.debug("mcp: %s refused with 405", method)
        status, out, data = _bare_error(
            405, "Method not allowed: the MCP endpoint accepts POST only")
        out["Allow"] = "POST"
        return status, out, data
    if "origin" in hdrs:
        log.warning("mcp: refused a request with Origin %r (403)", hdrs["origin"])
        return _bare_error(403, "Forbidden: browser requests (Origin header) "
                                "are not accepted on the MCP endpoint")
    if _media_type(hdrs.get("content-type", "")) != "application/json":
        log.warning("mcp: refused Content-Type %r (415)", hdrs.get("content-type"))
        return _bare_error(415, "Unsupported Media Type: send application/json")
    body = bytes(body or b"")
    if len(body) > MAX_BODY:
        log.warning("mcp: refused a %d-byte body (413)", len(body))
        return _bare_error(413, f"Payload Too Large: limit {MAX_BODY} bytes")

    try:
        msg = _parse(body)
    except (ValueError, RecursionError) as exc:
        log.warning("mcp: unparseable body: %s", exc)
        return _error(400, None, PARSE_ERROR, "Parse error")
    problem = _shape_problem(msg)
    if problem:
        log.warning("mcp: invalid JSON-RPC message: %s", problem)
        return _error(400, None, INVALID_REQUEST, f"Invalid Request: {problem}")

    if "method" not in msg:
        # A response from the client.  This server never sends requests, so
        # there is nothing to match it to; accepting it is what the spec asks.
        log.debug("mcp: client response id=%r accepted and ignored", msg.get("id"))
        return 202, {}, b""
    rpc = msg["method"]
    if "id" not in msg:
        # notifications/initialized, notifications/cancelled, ...: nothing to
        # do for any of them, and no headers are defined for them.
        log.debug("mcp: notification %s accepted", rpc)
        return 202, {}, b""

    rid = msg["id"]
    params = msg.get("params")
    era = _era(rpc, params, hdrs)
    if _first_time("era:" + era):
        _log_first_request(era, rpc, params, hdrs)
    log.debug("mcp: %s %s id=%r", era, rpc, rid)

    server_info = {"name": SERVER_NAME, "version": server_version}
    text = _instructions(instructions)
    try:
        if era == "modern":
            return _modern(rid, rpc, params, hdrs, tools, text, server_info)
        return _legacy(rid, rpc, params, hdrs, tools, text, server_info)
    except Exception:  # noqa: BLE001 -- never let a request kill the worker
        log.exception("mcp: %s %s failed", era, rpc)
        return _error(500, rid, INTERNAL_ERROR, "Internal error")


# ---------------------------------------------------------------------------
# Eras
# ---------------------------------------------------------------------------

def _era(rpc: str, params: Any, hdrs: Dict[str, str]) -> str:
    """initialize always means legacy; per-request _meta or the modern header
    means modern; anything else is a legacy client after its handshake."""
    if rpc == "initialize":
        return "legacy"
    meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict) and META_VERSION in meta:
        return "modern"
    if hdrs.get("mcp-protocol-version") == MODERN:
        return "modern"
    return "legacy"


def _legacy(rid: Any, rpc: str, params: Any, hdrs: Dict[str, str],
            tools: ToolSource, instructions: str,
            server_info: dict) -> Response:
    if rpc == "initialize":
        p = params if isinstance(params, dict) else {}
        asked = p.get("protocolVersion")
        version = asked if isinstance(asked, str) and asked in LEGACY_ECHO \
            else LEGACY_DEFAULT
        log.info("mcp: legacy initialize from %s asking %r, answering %s",
                 _client_name(p.get("clientInfo")), asked, version)
        return _result(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": server_info,
            "instructions": instructions,
        })

    # After initialize the client must send the negotiated version on every
    # request.  Absent means a pre-2025-06-18 client (served as such);
    # present but not one we negotiate is a 400 by the 2025-11-25 spec,
    # which names no code.
    header = hdrs.get("mcp-protocol-version")
    if header is not None and header not in LEGACY_ECHO:
        return _error(400, rid, INVALID_REQUEST,
                      f"Unsupported MCP-Protocol-Version {header!r}",
                      {"supported": [MODERN, *LEGACY_ECHO], "requested": header})
    if params is not None and not isinstance(params, dict):
        return _error(200, rid, INVALID_PARAMS, "Invalid params: params must be an object")
    p = params or {}

    if rpc == "ping":
        return _result(rid, {})
    if rpc == "tools/list":
        return _result(rid, {"tools": list(tools.mcp_tools())})
    if rpc == "tools/call":
        return _tools_call(rid, p, tools, None)
    return _error(200, rid, METHOD_NOT_FOUND, f"Method not found: {rpc}")


def _modern(rid: Any, rpc: str, params: Any, hdrs: Dict[str, str],
            tools: ToolSource, instructions: str,
            server_info: dict) -> Response:
    # The order matters: a dual-era client decides "this server is modern"
    # from the first of these errors it sees.
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict):
        return _error(400, rid, INVALID_PARAMS,
                      "Invalid params: params._meta with "
                      f"{META_VERSION} and {META_CAPS} is required")
    version = meta.get(META_VERSION)
    if not isinstance(version, str):
        return _error(400, rid, INVALID_PARAMS,
                      f"Invalid params: _meta {META_VERSION} must be a string")
    if not isinstance(meta.get(META_CAPS), dict):
        return _error(400, rid, INVALID_PARAMS,
                      f"Invalid params: _meta {META_CAPS} must be an object")

    header = hdrs.get("mcp-protocol-version")
    if header is None:
        return _error(400, rid, HEADER_MISMATCH,
                      "Header mismatch: MCP-Protocol-Version header is missing")
    if header != version:
        return _error(400, rid, HEADER_MISMATCH,
                      f"Header mismatch: MCP-Protocol-Version header {header!r} "
                      f"does not match body value {version!r}")
    if version != MODERN:
        return _error(400, rid, UNSUPPORTED_VERSION, "Unsupported protocol version",
                      {"supported": [MODERN], "requested": version})

    header = hdrs.get("mcp-method")
    if header is None:
        return _error(400, rid, HEADER_MISMATCH,
                      "Header mismatch: Mcp-Method header is missing")
    if header != rpc:
        return _error(400, rid, HEADER_MISMATCH,
                      f"Header mismatch: Mcp-Method header value {header!r} "
                      f"does not match body value {rpc!r}")
    if rpc == "tools/call":
        problem = _name_header_problem(hdrs.get("mcp-name"), params.get("name"))
        if problem:
            return _error(400, rid, HEADER_MISMATCH, f"Header mismatch: {problem}")

    if rpc not in MODERN_METHODS:
        # ping, resources/*, prompts/*, subscriptions/listen, ...
        return _error(404, rid, METHOD_NOT_FOUND, f"Method not found: {rpc}")

    out_meta = {META_SERVER: server_info}
    if rpc == "server/discover":
        log.info("mcp: server/discover from %s", _client_name(meta.get(META_CLIENT)))
        return _result(rid, {
            "resultType": "complete",
            "supportedVersions": [MODERN],
            "capabilities": {"tools": {}},
            "instructions": instructions,
            "ttlMs": TTL_MS,
            "cacheScope": "public",
            "_meta": out_meta,
        })
    if rpc == "tools/list":
        return _result(rid, {
            "resultType": "complete",
            "tools": list(tools.mcp_tools()),
            "ttlMs": TTL_MS,
            "cacheScope": "public",
            "_meta": out_meta,
        })
    return _tools_call(rid, params, tools, out_meta)


def _tools_call(rid: Any, params: dict, tools: ToolSource,
                out_meta: Optional[dict]) -> Response:
    """Shared by both eras; out_meta is None for legacy (no resultType/_meta)."""
    name = params.get("name")
    if not isinstance(name, str):
        return _error(200, rid, INVALID_PARAMS,
                      "Invalid params: tools/call needs params.name (a string)")
    if not tools.has(name):
        return _error(200, rid, INVALID_PARAMS, f"Unknown tool: {name}")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _error(200, rid, INVALID_PARAMS,
                      "Invalid params: arguments must be an object")

    try:
        is_error, obj = tools.call(name, args)
    except Exception as exc:  # noqa: BLE001 -- the registry should catch; backstop
        log.exception("mcp: tool %s raised", name)
        is_error, obj = True, {"ok": False, "tool": name,
                               "error": f"internal error: {exc}"}
    text = _result_text(obj)
    if text is None:
        is_error = True
        text = json.dumps({"ok": False, "tool": name, "error":
                           "internal error: the tool result could not be serialised"})

    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}],
                              "isError": bool(is_error)}
    if out_meta is not None:
        result = {"resultType": "complete", **result, "_meta": out_meta}
    return _result(rid, result)


# ---------------------------------------------------------------------------
# Parsing and validation helpers
# ---------------------------------------------------------------------------

def _lower_headers(headers: Any) -> Dict[str, str]:
    """Header names compare case-insensitively (RFC 9110); values do not.
    Optional whitespace around a value is not part of it.  A name given twice
    with different values is combined the way HTTP combines it, so it can no
    longer match a body value."""
    out: Dict[str, str] = {}
    items = headers.items() if hasattr(headers, "items") else (headers or ())
    for key, value in items:
        k = str(key).lower()
        v = str(value).strip(" \t")
        if k in out and out[k] != v:
            out[k] = f"{out[k]}, {v}"
        else:
            out[k] = v
    return out


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _reject_constant(token: str) -> Any:
    raise ValueError(f"{token} is not valid JSON")


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):          # 1e400 parses as inf
        raise ValueError(f"number {token} is out of range")
    return value


def _parse(body: bytes) -> Any:
    """json.loads accepts NaN, Infinity and 1e400 (= inf); none of them may
    reach a tool argument, so they are parse errors here."""
    return json.loads(body.decode("utf-8"), parse_constant=_reject_constant,
                      parse_float=_finite_float)


def _valid_id(value: Any) -> bool:
    # MCP: string or integer, never null.  bool is an int subclass in Python.
    return isinstance(value, str) or (isinstance(value, int)
                                      and not isinstance(value, bool))


def _shape_problem(msg: Any) -> Optional[str]:
    if isinstance(msg, list):
        return "batches are not supported; send one JSON-RPC object per POST"
    if not isinstance(msg, dict):
        return "the body must be a JSON-RPC object"
    if msg.get("jsonrpc") != "2.0":
        return 'jsonrpc must be "2.0"'
    if "id" in msg and not _valid_id(msg["id"]):
        return "id must be a string or an integer"
    if "method" in msg:
        if not isinstance(msg["method"], str):
            return "method must be a string"
    elif "result" not in msg and "error" not in msg:
        return "no method (and not a response either)"
    return None


def _decode_header_value(raw: str) -> Optional[str]:
    """Undo the =?base64?...?= sentinel; None when the value is malformed or
    holds characters a plain header value may not carry."""
    if (len(raw) >= len(_SENTINEL_HEAD) + len(_SENTINEL_TAIL)
            and raw.startswith(_SENTINEL_HEAD) and raw.endswith(_SENTINEL_TAIL)):
        encoded = raw[len(_SENTINEL_HEAD):-len(_SENTINEL_TAIL)]
        encoded += "=" * (-len(encoded) % 4)
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, ValueError):   # incl. UnicodeDecodeError
            return None
    if any(not (c == "\t" or " " <= c <= "~") for c in raw):
        return None
    return raw


def _name_header_problem(raw: Optional[str], name: Any) -> Optional[str]:
    if raw is None:
        return "Mcp-Name header is missing"
    value = _decode_header_value(raw)
    if value is None:
        return "Mcp-Name header value is malformed"
    if not isinstance(name, str) or value != name:
        return (f"Mcp-Name header value {value!r} does not match body value "
                f"{name!r}")
    return None


def _instructions(text: Any) -> str:
    text = text if isinstance(text, str) else str(text)
    if len(text) > MAX_INSTRUCTIONS:
        if _first_time("long-instructions"):
            log.warning("mcp: instructions are %d characters; sending the first %d",
                        len(text), MAX_INSTRUCTIONS)
        text = text[:MAX_INSTRUCTIONS]
    return text


def _client_name(info: Any) -> str:
    if isinstance(info, dict):
        return repr(f"{info.get('name')} {info.get('version')}")
    return "an unnamed client"


def _log_first_request(era: str, rpc: str, params: Any,
                       hdrs: Dict[str, str]) -> None:
    shown = {k: ("<redacted>" if k in _SECRET_HEADERS else v)
             for k, v in hdrs.items()}
    meta = params.get("_meta") if isinstance(params, dict) else None
    meta_keys = sorted(map(str, meta)) if isinstance(meta, dict) else []
    log.info("mcp: first %s-era request %r; headers %s; _meta keys %s",
             era, rpc, shown, meta_keys)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _json_safe(obj: Any) -> Any:
    """Non-finite floats become null and odd dict keys strings, so one NaN
    reading does not turn a whole overview into an error."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {(k if isinstance(k, str) else str(k)): _json_safe(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _dumps_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, default=str)


def _result_text(obj: Any) -> Optional[str]:
    """The text block of a tool result.  Plain text (get_overview, get_guide)
    goes as is; quoting it as a JSON string would show the model \\n escapes."""
    if isinstance(obj, str):
        return obj
    try:
        return _dumps_text(obj)
    except (ValueError, TypeError, RecursionError):
        pass
    try:
        text = _dumps_text(_json_safe(obj))
    except (ValueError, TypeError, RecursionError):
        log.exception("mcp: tool result is not serialisable")
        return None
    log.warning("mcp: tool result held non-finite numbers or non-string keys; "
                "sent as null / strings")
    return text


def _result(rid: Any, result: dict) -> Response:
    return _send(200, {"jsonrpc": "2.0", "id": rid, "result": result})


def _error(status: int, rid: Any, code: int, message: str,
           data: Any = None) -> Response:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _send(status, {"jsonrpc": "2.0", "id": rid, "error": error})


def _bare_error(status: int, message: str) -> Response:
    """Transport-level refusal: a JSON-RPC error with no id, as the spec
    allows for a bad Origin."""
    return _send(status, {"jsonrpc": "2.0",
                          "error": {"code": INVALID_REQUEST, "message": message}})


def _send(status: int, payload: dict) -> Response:
    try:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        log.exception("mcp: response could not be serialised")
        status = 500
        payload = {"jsonrpc": "2.0", "id": payload.get("id"),
                   "error": {"code": INTERNAL_ERROR,
                             "message": "Internal error: response could not be serialised"}}
        text = json.dumps(payload, separators=(",", ":"))
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate (a "\ud800" escape in the request, echoed back)
        # has no UTF-8 form; \u-escaping everything is still valid JSON.
        data = json.dumps(payload, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("ascii")
    return status, dict(_JSON_HEADERS), data
