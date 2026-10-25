"""
Offline tests for the MCP protocol handler (aicontrol/mcp.py).

    python -m pytest tests/test_aicontrol_mcp.py -q      (from the repo root)

handle() is pure, so these drive it with byte bodies and header dicts and a
fake tool registry -- no Flask, no broker.  Both protocol eras are walked end
to end the way Claude Code's two runtimes do it, and every refusal is checked
for its exact HTTP status and JSON-RPC code, because a dual-era client decides
which era a server speaks from exactly those.
"""

import base64
import json
import logging

import pytest

from aicontrol import mcp

MODERN = "2026-07-28"
INSTRUCTIONS = "xsphere test instructions: read get_overview first."
VERSION = "9.8.7"
SERVER_INFO = {"name": "xsphere-aicontrol", "version": VERSION}
UNICODE_TOOL = "temp_°K_世界"


class FakeTools:
    """Stands in for aicontrol.tools.ToolRegistry."""

    def __init__(self):
        self.calls = []

    def mcp_tools(self):
        return [
            {"name": "get_overview", "title": "Overview",
             "description": "Read the overview.",
             "inputSchema": {"type": "object", "properties": {},
                             "additionalProperties": False},
             "annotations": {"readOnlyHint": True, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False},
             "_meta": {"anthropic/maxResultSizeChars": 150000}},
            {"name": "set_thing", "title": "Set thing",
             "description": "Write something.",
             "inputSchema": {"type": "object",
                             "properties": {"value": {"type": "number"},
                                            "reason": {"type": "string"}},
                             "required": ["value", "reason"],
                             "additionalProperties": False},
             "annotations": {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": False, "openWorldHint": False}},
        ]

    def has(self, name):
        return name in ("get_overview", "set_thing", "text_tool", "nan_tool",
                        "boom", "bad_keys", UNICODE_TOOL)

    def call(self, name, args, client=None):
        self.calls.append((name, args))
        self.clients = getattr(self, "clients", []) + [client]
        if name == "get_overview":
            return False, {"ok": True, "t_top_k": 293.5, "unit": "°C", "args": args}
        if name == "set_thing":
            if args.get("armed"):
                return False, {"ok": True, "published": [{"topic": "x", "payload": 1}]}
            return True, {"ok": False, "refused": "heaters are not armed"}
        if name == "text_tool":
            return False, "line one\nline two — ok"
        if name == "nan_tool":
            return False, {"ok": True, "value": float("nan"), "inf": [float("inf")]}
        if name == "bad_keys":
            return False, {("a", 1): "tuple key"}
        if name == "boom":
            raise RuntimeError("kaboom")
        return False, {"ok": True, "tool": name}


@pytest.fixture
def tools():
    return FakeTools()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def call(tools, body, headers=None, method="POST", instructions=INSTRUCTIONS):
    """POST a message (dict/list → JSON, bytes → raw) and return
    (status, headers, parsed body or None).  Every response is checked for
    the things that must hold everywhere."""
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
    else:
        raw = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    h.update(headers or {})
    status, out_headers, data = mcp.handle(method, h, raw, tools, instructions,
                                           server_version=VERSION)
    assert isinstance(status, int)
    assert isinstance(data, bytes)
    assert "mcp-session-id" not in {k.lower() for k in out_headers}
    if status == 202:
        assert data == b""
        return status, out_headers, None
    assert out_headers.get("Content-Type") == "application/json"
    return status, out_headers, json.loads(data.decode("utf-8"))


def meta(version=MODERN, caps=None):
    return {mcp.META_VERSION: version,
            mcp.META_CAPS: {} if caps is None else caps,
            mcp.META_CLIENT: {"name": "pytest", "version": "0"}}


def modern(method, rid=1, params=None, name=None, version=MODERN, headers=None):
    """A well-formed modern request and its headers; tests then break one thing."""
    p = dict(params or {})
    p.setdefault("_meta", meta(version))
    if name is not None:
        p["name"] = name
    msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": p}
    h = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if name is not None:
        h["Mcp-Name"] = name
    h.update(headers or {})
    return msg, h


def legacy(method, rid=1, params=None):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


LEGACY_H = {"MCP-Protocol-Version": "2025-11-25"}


def assert_error(resp, status, code, rid="absent"):
    st, _, body = resp
    assert st == status, body
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert "result" not in body
    if rid != "absent":
        assert "id" in body and body["id"] == rid
    return body


def b64_sentinel(text):
    return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


# ---------------------------------------------------------------------------
# Legacy era (2025-11-25 initialize handshake), end to end
# ---------------------------------------------------------------------------

def test_legacy_session_end_to_end(tools):
    st, _, body = call(tools, legacy("initialize", 0, {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "claude-code", "version": "2.1.299"}}))
    assert st == 200
    assert body == {"jsonrpc": "2.0", "id": 0, "result": {
        "protocolVersion": "2025-11-25",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": INSTRUCTIONS}}

    st, h, body = call(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                       LEGACY_H)
    assert (st, body) == (202, None)

    st, _, body = call(tools, legacy("tools/list", 1), LEGACY_H)
    assert st == 200 and body["id"] == 1
    assert body["result"] == {"tools": tools.mcp_tools()}

    st, _, body = call(tools, legacy("tools/call", 2, {
        "name": "get_overview", "arguments": {"format": "json"}}), LEGACY_H)
    assert st == 200 and body["id"] == 2
    res = body["result"]
    assert set(res) == {"content", "isError"}
    assert res["isError"] is False
    assert res["content"][0]["type"] == "text"
    assert json.loads(res["content"][0]["text"]) == {
        "ok": True, "t_top_k": 293.5, "unit": "°C", "args": {"format": "json"}}
    assert "°C" in res["content"][0]["text"]          # ensure_ascii=False

    st, _, body = call(tools, legacy("tools/call", "s-3", {
        "name": "set_thing", "arguments": {"value": 1, "reason": "test"}}), LEGACY_H)
    assert st == 200 and body["id"] == "s-3"
    assert body["result"]["isError"] is True
    assert json.loads(body["result"]["content"][0]["text"]) == {
        "ok": False, "refused": "heaters are not armed"}

    assert tools.calls == [("get_overview", {"format": "json"}),
                           ("set_thing", {"value": 1, "reason": "test"})]


@pytest.mark.parametrize("asked,answered", [
    ("2025-11-25", "2025-11-25"),
    ("2025-06-18", "2025-06-18"),
    ("2025-03-26", "2025-11-25"),      # batches era: not echoed
    ("2024-11-05", "2025-11-25"),
    (MODERN, "2025-11-25"),
    ("garbage", "2025-11-25"),
    (None, "2025-11-25"),
])
def test_legacy_initialize_version_choice(tools, asked, answered):
    params = {"capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}
    if asked is not None:
        params["protocolVersion"] = asked
    st, _, body = call(tools, legacy("initialize", 1, params))
    assert st == 200
    assert body["result"]["protocolVersion"] == answered


def test_initialize_is_legacy_even_with_modern_headers_and_meta(tools):
    msg = legacy("initialize", 7, {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "_meta": meta()})
    st, _, body = call(tools, msg, {"MCP-Protocol-Version": MODERN,
                                    "Mcp-Method": "something/else"})
    assert st == 200
    assert body["result"]["protocolVersion"] == "2025-11-25"
    assert "resultType" not in body["result"]


def test_legacy_ping_and_missing_version_header(tools):
    # No MCP-Protocol-Version at all = a pre-2025-06-18 client; still served.
    st, _, body = call(tools, legacy("ping", 5))
    assert (st, body) == (200, {"jsonrpc": "2.0", "id": 5, "result": {}})
    st, _, body = call(tools, legacy("tools/list", 6))
    assert st == 200 and len(body["result"]["tools"]) == 2


def test_legacy_2025_06_18_header_accepted(tools):
    st, _, body = call(tools, legacy("tools/list", 1), {"MCP-Protocol-Version": "2025-06-18"})
    assert st == 200 and "tools" in body["result"]


@pytest.mark.parametrize("version", ["2025-03-26", "2024-11-05", "garbage", ""])
def test_legacy_bad_protocol_version_header(tools, version):
    body = assert_error(call(tools, legacy("tools/list", 9),
                             {"MCP-Protocol-Version": version}),
                        400, -32600, rid=9)
    assert body["error"]["data"]["requested"] == version


def test_legacy_unknown_method(tools):
    assert_error(call(tools, legacy("resources/list", 3), LEGACY_H), 200, -32601, rid=3)
    # server/discover without modern _meta/header is a legacy request too
    assert_error(call(tools, legacy("server/discover", 4), LEGACY_H), 200, -32601, rid=4)


def test_legacy_unknown_tool(tools):
    body = assert_error(call(tools, legacy("tools/call", 3, {"name": "nope", "arguments": {}}),
                             LEGACY_H), 200, -32602, rid=3)
    assert body["error"]["message"] == "Unknown tool: nope"
    assert tools.calls == []


@pytest.mark.parametrize("params", [
    {"arguments": {}},                           # no name
    {"name": 5, "arguments": {}},                # name not a string
    {"name": "get_overview", "arguments": [1]},  # arguments not an object
    {"name": "get_overview", "arguments": "x"},
])
def test_legacy_tools_call_bad_params(tools, params):
    assert_error(call(tools, legacy("tools/call", 3, params), LEGACY_H), 200, -32602, rid=3)
    assert tools.calls == []


def test_legacy_params_not_object(tools):
    assert_error(call(tools, legacy("tools/list", 3, [1, 2]), LEGACY_H), 200, -32602, rid=3)


def test_missing_or_null_arguments_become_empty_object(tools):
    call(tools, legacy("tools/call", 1, {"name": "get_overview"}), LEGACY_H)
    call(tools, legacy("tools/call", 2, {"name": "get_overview", "arguments": None}), LEGACY_H)
    assert tools.calls == [("get_overview", {}), ("get_overview", {})]


# ---------------------------------------------------------------------------
# Tool result text
# ---------------------------------------------------------------------------

def test_string_result_is_sent_verbatim(tools):
    st, _, body = call(tools, legacy("tools/call", 1, {"name": "text_tool"}), LEGACY_H)
    assert body["result"]["content"][0]["text"] == "line one\nline two — ok"
    assert body["result"]["isError"] is False


def test_non_finite_numbers_in_result_become_null(tools):
    st, _, body = call(tools, legacy("tools/call", 1, {"name": "nan_tool"}), LEGACY_H)
    text = body["result"]["content"][0]["text"]
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"ok": True, "value": None, "inf": [None]}
    assert body["result"]["isError"] is False


def test_unserialisable_keys_are_stringified(tools):
    st, _, body = call(tools, legacy("tools/call", 1, {"name": "bad_keys"}), LEGACY_H)
    assert json.loads(body["result"]["content"][0]["text"]) == {"('a', 1)": "tuple key"}


def test_tool_exception_is_an_error_result(tools, caplog):
    with caplog.at_level(logging.ERROR, logger="aicontrol.mcp"):
        st, _, body = call(tools, legacy("tools/call", 1, {"name": "boom"}), LEGACY_H)
    assert st == 200
    assert body["result"]["isError"] is True
    out = json.loads(body["result"]["content"][0]["text"])
    assert out["ok"] is False and "kaboom" in out["error"]


def test_registry_failure_is_internal_error(tools):
    def broken():
        raise RuntimeError("registry broke")
    tools.mcp_tools = broken
    assert_error(call(tools, legacy("tools/list", 4), LEGACY_H), 500, -32603, rid=4)


def test_lone_surrogate_echo_still_valid_json(tools):
    raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"\\ud800x"}}'
    st, _, body = call(tools, raw, LEGACY_H)
    assert st == 200 and body["error"]["code"] == -32602


def test_long_instructions_are_cut(tools):
    st, _, body = call(tools, legacy("initialize", 1, {"protocolVersion": "2025-11-25"}),
                       instructions="x" * 5000)
    assert len(body["result"]["instructions"]) == mcp.MAX_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Modern era (2026-07-28), end to end
# ---------------------------------------------------------------------------

def test_modern_discover(tools):
    msg, h = modern("server/discover", rid="discover-1")
    st, out_h, body = call(tools, msg, h)
    assert st == 200
    assert body == {"jsonrpc": "2.0", "id": "discover-1", "result": {
        "resultType": "complete",
        "supportedVersions": [MODERN],
        "capabilities": {"tools": {}},
        "instructions": INSTRUCTIONS,
        "ttlMs": 300000,
        "cacheScope": "public",
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}}
    assert "listChanged" not in json.dumps(body)


def test_ttl_is_an_integer_on_the_wire(tools):
    for method in ("server/discover", "tools/list"):
        msg, h = modern(method)
        h = {"Content-Type": "application/json", **h}
        _, _, raw = mcp.handle("POST", h, json.dumps(msg).encode(), tools, INSTRUCTIONS)
        assert b'"ttlMs":300000' in raw
        assert b"300000.0" not in raw and b"3e5" not in raw.lower()
        assert type(json.loads(raw)["result"]["ttlMs"]) is int
    assert type(mcp.TTL_MS) is int


def test_modern_session_end_to_end(tools):
    msg, h = modern("server/discover", rid=0)
    assert call(tools, msg, h)[0] == 200

    msg, h = modern("tools/list", rid=1)
    st, _, body = call(tools, msg, h)
    assert st == 200 and body["id"] == 1
    assert body["result"] == {
        "resultType": "complete", "tools": tools.mcp_tools(),
        "ttlMs": 300000, "cacheScope": "public",
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO}}

    msg, h = modern("tools/call", rid=2, name="get_overview",
                    params={"arguments": {"format": "text"}})
    st, _, body = call(tools, msg, h)
    assert st == 200 and body["id"] == 2
    res = body["result"]
    assert res["resultType"] == "complete"
    assert res["isError"] is False
    assert res["_meta"] == {"io.modelcontextprotocol/serverInfo": SERVER_INFO}
    assert json.loads(res["content"][0]["text"])["args"] == {"format": "text"}

    msg, h = modern("tools/call", rid=3, name="set_thing",
                    params={"arguments": {"value": 300, "reason": "r"}})
    st, _, body = call(tools, msg, h)
    assert st == 200 and body["result"]["isError"] is True
    assert body["result"]["resultType"] == "complete"

    msg, h = modern("tools/call", rid=4, name="set_thing",
                    params={"arguments": {"value": 300, "reason": "r", "armed": True}})
    st, _, body = call(tools, msg, h)
    assert body["result"]["isError"] is False

    assert [c[0] for c in tools.calls] == ["get_overview", "set_thing", "set_thing"]


def test_modern_detected_by_header_alone_needs_meta(tools):
    # Modern header but no _meta at all → the modern -32602, not legacy.
    msg = legacy("tools/list", 8)
    assert_error(call(tools, msg, {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}),
                 400, -32602, rid=8)


def test_header_names_are_case_insensitive(tools):
    msg, _ = modern("tools/call", rid=1, name="get_overview")
    h = {"content-type": "Application/JSON; charset=utf-8",
         "mcp-protocol-version": MODERN, "MCP-METHOD": "tools/call", "mCp-NaMe": "get_overview"}
    status, out, raw = mcp.handle("POST", h, json.dumps(msg).encode(), tools, INSTRUCTIONS)
    assert status == 200
    assert json.loads(raw)["result"]["isError"] is False


def test_header_values_are_case_sensitive(tools):
    msg, h = modern("tools/list", rid=1, headers={"Mcp-Method": "Tools/List"})
    assert_error(call(tools, msg, h), 400, -32020, rid=1)


def test_mcp_name_base64_sentinel(tools):
    msg, h = modern("tools/call", rid=1, name=UNICODE_TOOL,
                    headers={"Mcp-Name": b64_sentinel(UNICODE_TOOL)})
    st, _, body = call(tools, msg, h)
    assert st == 200 and body["result"]["isError"] is False
    assert tools.calls[-1][0] == UNICODE_TOOL


def test_mcp_name_sentinel_for_plain_name_and_missing_padding(tools):
    enc = b64_sentinel("nan_tool")
    assert enc.endswith("=?=")             # padded form ...
    msg, h = modern("tools/call", rid=1, name="nan_tool", headers={"Mcp-Name": enc})
    st, _, body = call(tools, msg, h)
    assert st == 200 and body["result"]["isError"] is False
    unpadded = enc[:-2].rstrip("=") + "?="  # ... and without the padding
    msg, h = modern("tools/call", rid=2, name="nan_tool", headers={"Mcp-Name": unpadded})
    assert call(tools, msg, h)[0] == 200
    assert [c[0] for c in tools.calls] == ["nan_tool", "nan_tool"]


@pytest.mark.parametrize("header", [
    "=?base64?not base64!?=",                    # not base64
    "=?base64?" + base64.b64encode(b"\xff\xfe").decode() + "?=",   # not UTF-8
    b64_sentinel("set_thing"),                   # decodes, but to another name
    "get_overview\x01",                          # control character in a plain value
    UNICODE_TOOL,                                # non-ASCII sent without the sentinel
    "set_thing",                                 # plain mismatch
])
def test_mcp_name_bad_values(tools, header):
    name = UNICODE_TOOL if header == UNICODE_TOOL else "get_overview"
    msg, h = modern("tools/call", rid=5, name=name, headers={"Mcp-Name": header})
    assert_error(call(tools, msg, h), 400, -32020, rid=5)
    assert tools.calls == []


def test_session_id_header_ignored_and_never_minted(tools):
    msg, h = modern("tools/list", rid=1, headers={"Mcp-Session-Id": "abc"})
    st, out_h, _ = call(tools, msg, h)
    assert st == 200
    st, out_h, _ = call(tools, legacy("initialize", 1, {"protocolVersion": "2025-11-25"}),
                        {"Mcp-Session-Id": "abc"})
    assert st == 200 and "Mcp-Session-Id" not in out_h


# ---------------------------------------------------------------------------
# Modern-era refusals, in validation order
# ---------------------------------------------------------------------------

def test_modern_meta_missing(tools):
    msg, h = modern("tools/list", rid=11)
    del msg["params"]["_meta"]
    assert_error(call(tools, msg, h), 400, -32602, rid=11)
    msg, h = modern("tools/list", rid=12)
    msg["params"]["_meta"] = "not an object"
    assert_error(call(tools, msg, h), 400, -32602, rid=12)
    msg, h = modern("tools/list", rid=13)
    msg["params"] = [1]
    assert_error(call(tools, msg, h), 400, -32602, rid=13)


@pytest.mark.parametrize("version", [20260728, None, ["2026-07-28"]])
def test_modern_protocol_version_not_a_string(tools, version):
    msg, h = modern("tools/list", rid=14)
    msg["params"]["_meta"][mcp.META_VERSION] = version
    assert_error(call(tools, msg, h), 400, -32602, rid=14)


@pytest.mark.parametrize("caps", ["missing", None, [], "x"])
def test_modern_client_capabilities_not_an_object(tools, caps):
    msg, h = modern("server/discover", rid=15)
    if caps == "missing":
        del msg["params"]["_meta"][mcp.META_CAPS]
    else:
        msg["params"]["_meta"][mcp.META_CAPS] = caps
    assert_error(call(tools, msg, h), 400, -32602, rid=15)


def test_modern_protocol_version_header_missing(tools):
    msg, h = modern("server/discover", rid=16)
    del h["MCP-Protocol-Version"]
    assert_error(call(tools, msg, h), 400, -32020, rid=16)


def test_modern_protocol_version_header_mismatch(tools):
    msg, h = modern("server/discover", rid=17, headers={"MCP-Protocol-Version": "2025-11-25"})
    assert_error(call(tools, msg, h), 400, -32020, rid=17)


@pytest.mark.parametrize("version", ["2027-01-01", "2025-11-25", "1900-01-01"])
def test_modern_unsupported_version(tools, version):
    msg, h = modern("server/discover", rid=18, version=version)
    body = assert_error(call(tools, msg, h), 400, -32022, rid=18)
    assert body["error"]["data"] == {"supported": [MODERN], "requested": version}


def test_modern_unsupported_version_checked_after_header_match(tools):
    # header says modern, body says something else: mismatch wins
    msg, h = modern("server/discover", rid=19, version="2027-01-01",
                    headers={"MCP-Protocol-Version": MODERN})
    assert_error(call(tools, msg, h), 400, -32020, rid=19)


def test_modern_mcp_method_missing(tools):
    msg, h = modern("tools/list", rid=20)
    del h["Mcp-Method"]
    assert_error(call(tools, msg, h), 400, -32020, rid=20)


def test_modern_mcp_method_mismatch(tools):
    msg, h = modern("tools/call", rid=21, name="get_overview",
                    headers={"Mcp-Method": "tools/list"})
    assert_error(call(tools, msg, h), 400, -32020, rid=21)
    assert tools.calls == []


def test_modern_mcp_name_missing(tools):
    msg, h = modern("tools/call", rid=22, name="get_overview")
    del h["Mcp-Name"]
    assert_error(call(tools, msg, h), 400, -32020, rid=22)


def test_modern_mcp_name_mismatch(tools):
    msg, h = modern("tools/call", rid=23, name="set_thing",
                    headers={"Mcp-Name": "get_overview"})
    body = assert_error(call(tools, msg, h), 400, -32020, rid=23)
    assert "Mcp-Name" in body["error"]["message"]
    assert tools.calls == []


def test_duplicate_header_with_different_values_mismatches(tools):
    msg, _ = modern("tools/list", rid=24)
    pairs = [("Content-Type", "application/json"), ("MCP-Protocol-Version", MODERN),
             ("Mcp-Method", "tools/list"), ("mcp-method", "tools/call")]

    class MultiHeaders:                     # werkzeug.Headers-like: items() repeats keys
        def items(self):
            return iter(pairs)
    status, _, raw = mcp.handle("POST", MultiHeaders(), json.dumps(msg).encode(),
                                tools, INSTRUCTIONS)
    assert status == 400 and json.loads(raw)["error"]["code"] == -32020


@pytest.mark.parametrize("method", ["ping", "resources/list", "prompts/list",
                                    "subscriptions/listen", "initialize/x"])
def test_modern_unknown_method_is_404(tools, method):
    msg, h = modern(method, rid=25)
    assert_error(call(tools, msg, h), 404, -32601, rid=25)


def test_modern_unknown_tool(tools):
    msg, h = modern("tools/call", rid=26, name="nope")
    body = assert_error(call(tools, msg, h), 200, -32602, rid=26)
    assert body["error"]["message"] == "Unknown tool: nope"


def test_modern_arguments_not_object(tools):
    msg, h = modern("tools/call", rid=27, name="get_overview", params={"arguments": [1]})
    assert_error(call(tools, msg, h), 200, -32602, rid=27)
    assert tools.calls == []


# ---------------------------------------------------------------------------
# Before dispatch: HTTP method, Origin, media type, size, JSON, JSON-RPC shape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("http_method", ["GET", "DELETE", "PUT", "OPTIONS", "HEAD"])
def test_non_post_is_405(tools, http_method):
    st, h, body = call(tools, legacy("ping", 1), method=http_method)
    assert st == 405
    assert h["Allow"] == "POST"
    assert "id" not in body and body["error"]["code"] == -32600


def test_origin_is_403(tools):
    msg, h = modern("server/discover")
    for origin in ("http://evil.example", "http://xbox-pi:8089", "null"):
        st, _, body = call(tools, msg, {**h, "Origin": origin})
        assert st == 403 and "id" not in body
    assert tools.calls == []


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data", "application/jsonx",
                                   "application/json-seq"])
def test_wrong_content_type_is_415(tools, ctype):
    h = {"Content-Type": ctype} if ctype else {}
    raw = json.dumps(legacy("ping", 1)).encode()
    hdrs = {"MCP-Protocol-Version": "2025-11-25", **h}
    status, out, data = mcp.handle("POST", hdrs, raw, tools, INSTRUCTIONS)
    assert status == 415 and json.loads(data)["error"]["code"] == -32600


def test_content_type_with_charset_ok(tools):
    st, _, body = call(tools, legacy("ping", 1),
                       {"Content-Type": "application/json; charset=utf-8"})
    assert st == 200


def test_body_too_large_is_413(tools):
    raw = json.dumps(legacy("ping", 1)).encode()
    st, _, _ = call(tools, raw + b" " * (mcp.MAX_BODY - len(raw)))
    assert st == 200                                   # exactly the limit is fine
    st, _, body = call(tools, raw + b" " * (mcp.MAX_BODY - len(raw) + 1))
    assert st == 413 and "id" not in body


@pytest.mark.parametrize("raw", [
    b"", b"{", b"not json", b'{"jsonrpc":"2.0","id":1,"method":"ping"',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":Infinity}}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":-Infinity}}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":1e400}}',
    b'{"jsonrpc":"2.0","id":1,"method":"\xff"}',          # not UTF-8
    b"[" * 100000 + b"]" * 100000,                        # absurd nesting
], ids=["empty", "open-brace", "text", "truncated", "NaN", "Infinity", "-Infinity",
        "1e400", "not-utf8", "deep-nesting"])
def test_parse_errors(tools, raw):
    body = assert_error(call(tools, raw), 400, -32700)
    assert body["id"] is None


def test_nan_in_tool_arguments_never_reaches_the_tool(tools):
    raw = (b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
           b'"params":{"name":"set_thing","arguments":{"value":NaN,"reason":"r"}}}')
    assert_error(call(tools, raw, LEGACY_H), 400, -32700)
    assert tools.calls == []


@pytest.mark.parametrize("msg", [
    [legacy("ping", 1)],                                   # batch
    [],                                                    # empty batch
    [{"jsonrpc": "2.0", "method": "notifications/initialized"}],
    "ping", 1, None, True,                                 # not an object
    {"id": 1, "method": "ping"},                           # no jsonrpc
    {"jsonrpc": "1.0", "id": 1, "method": "ping"},
    {"jsonrpc": 2.0, "id": 1, "method": "ping"},
    {"jsonrpc": "2.0", "id": None, "method": "ping"},     # null id
    {"jsonrpc": "2.0", "id": True, "method": "ping"},     # bool id
    {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},      # float id
    {"jsonrpc": "2.0", "id": 1.0, "method": "ping"},
    {"jsonrpc": "2.0", "id": [1], "method": "ping"},
    {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},
    {"jsonrpc": "2.0", "id": 1, "method": 5},              # method not a string
    {"jsonrpc": "2.0", "method": None},
    {"jsonrpc": "2.0", "id": 1},                           # nothing at all
])
def test_invalid_requests(tools, msg):
    body = assert_error(call(tools, msg), 400, -32600)
    assert body["id"] is None


@pytest.mark.parametrize("headers", [
    {},                                                    # bare
    {"MCP-Protocol-Version": "garbage"},                   # headers not validated
    {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"},
])
def test_notifications_are_202(tools, headers):
    for method in ("notifications/initialized", "notifications/cancelled", "anything/else"):
        st, _, body = call(tools, {"jsonrpc": "2.0", "method": method,
                                   "params": {"requestId": 1}}, headers)
        assert (st, body) == (202, None)
    assert tools.calls == []


@pytest.mark.parametrize("msg", [
    {"jsonrpc": "2.0", "id": 1, "result": {}},
    {"jsonrpc": "2.0", "id": "x", "error": {"code": -1, "message": "no"}},
])
def test_client_response_objects_are_202(tools, msg):
    st, _, body = call(tools, msg, LEGACY_H)
    assert (st, body) == (202, None)


def test_string_and_large_integer_ids_echoed(tools):
    for rid in ("abc", "", 0, -5, 2 ** 53 + 1):
        st, _, body = call(tools, legacy("ping", rid), LEGACY_H)
        assert st == 200 and body["id"] == rid and type(body["id"]) is type(rid)


def test_first_request_headers_logged_once_without_secrets(tools, caplog, monkeypatch):
    monkeypatch.setattr(mcp, "_once", set())
    with caplog.at_level(logging.INFO, logger="aicontrol.mcp"):
        msg, h = modern("server/discover", headers={"Authorization": "Bearer s3cret"})
        call(tools, msg, h)
        call(tools, msg, h)
    firsts = [r for r in caplog.records if "first modern-era request" in r.getMessage()]
    assert len(firsts) == 1
    assert "s3cret" not in caplog.text
    assert "mcp-method" in firsts[0].getMessage()


def test_the_caller_reaches_tools_call(tools):
    """app.py resolves the Bearer token to a client name; handle() hands it
    to the registry with every tools/call (and nothing else)."""
    msg = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
           "params": {"name": "get_overview", "arguments": {}}}
    h = {"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
    status, _h, _d = mcp.handle("POST", h, json.dumps(msg).encode(), tools, INSTRUCTIONS,
                                client="operator")
    assert status == 200 and tools.clients[-1] == "operator"
    mcp.handle("POST", h, json.dumps(msg).encode(), tools, INSTRUCTIONS)
    assert tools.clients[-1] is None
