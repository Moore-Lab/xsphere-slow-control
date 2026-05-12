#!/usr/bin/env python3
"""Generate the "xsphere Control" Node-RED flow.

Builds a node-red-dashboard (1.x) flow with operator controls for valves,
heater PID loops, the gradient abstraction, an automated gradient scan, a
"follow an RTD" mode, and a small ramp sequencer — plus a System Status group.

Usage:
    python nodered/build_control_flow.py            # write nodered/control-flows.json
    python nodered/build_control_flow.py --deploy   # also merge into the running
                                                    # Node-RED at localhost:1880

The generated nodes are a *new* flow tab + dashboard tab; deploying merges
them into the existing flows (nothing is removed). It references the existing
`ui_base` config node if one is found, and creates its own `mqtt-broker`
config node pointing at `mosquitto:1883`.
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# ids / layout helpers
# ---------------------------------------------------------------------------
TAB_ID      = "xsphere_control_flow"
UI_TAB_ID   = "xsphere_ui_tab_control"
BROKER_ID   = "xsphere_mqtt_broker"
UI_BASE_ID  = "xsphere_ui_base"          # overridden by the existing one when deploying

_seq = [0]
def nid(prefix="n"):
    _seq[0] += 1
    return f"xsc_{prefix}_{_seq[0]:03d}"

_nodes: list = []
def add(n: dict) -> str:
    _nodes.append(n)
    return n["id"]

# auto column/row layout: each "chain" gets a row; nodes step right
_row = [0]
def newrow():
    _row[0] += 1
def pos(col):
    return 140 + col * 200, 60 + _row[0] * 64

# ---------------------------------------------------------------------------
# config nodes
# ---------------------------------------------------------------------------
add({"id": TAB_ID, "type": "tab", "label": "xsphere Control",
     "disabled": False, "info": "Operator control dashboard — see the 'Control' tab in the Node-RED dashboard UI."})

add({"id": BROKER_ID, "type": "mqtt-broker", "name": "mosquitto",
     "broker": "mosquitto", "port": "1883", "clientid": "", "autoConnect": True,
     "usetls": False, "protocolVersion": "4", "keepalive": "60", "cleansession": True})

add({"id": UI_TAB_ID, "type": "ui_tab", "name": "Control",
     "icon": "dashboard", "order": 1, "disabled": False, "hidden": False})

def group(name, gid, width=6, order=1):
    add({"id": gid, "type": "ui_group", "name": name, "tab": UI_TAB_ID,
         "order": order, "disp": True, "width": str(width), "collapse": False})
    return gid

# ---------------------------------------------------------------------------
# generic widget/wiring helpers
# ---------------------------------------------------------------------------
def mqtt_in(topic, name=""):
    newrow()
    x, y = pos(0)
    return add({"id": nid("in"), "type": "mqtt in", "z": TAB_ID, "name": name or topic,
                "topic": topic, "qos": "1", "datatype": "auto-detect", "broker": BROKER_ID,
                "x": x, "y": y, "wires": [[]]})

def mqtt_out(topic="", name=""):
    x, y = pos(4)
    return add({"id": nid("out"), "type": "mqtt out", "z": TAB_ID, "name": name or topic,
                "topic": topic, "qos": "1", "retain": "", "broker": BROKER_ID,
                "x": x, "y": y, "wires": []})

def fn(name, code, outputs=1, col=2):
    x, y = pos(col)
    return add({"id": nid("fn"), "type": "function", "z": TAB_ID, "name": name,
                "func": code, "outputs": outputs, "noerr": 0, "initialize": "", "finalize": "",
                "libs": [], "x": x, "y": y, "wires": [[] for _ in range(outputs)]})

def ui_text(group, label, gorder, col=3):
    x, y = pos(col)
    return add({"id": nid("txt"), "type": "ui_text", "z": TAB_ID, "group": group,
                "order": gorder, "width": "0", "height": "0", "name": label, "label": label,
                "format": "{{msg.payload}}", "layout": "row-spread", "className": "",
                "x": x, "y": y, "wires": []})

def ui_button(group, label, gorder, payload="", payloadType="str", col=0):
    newrow()
    x, y = pos(col)
    return add({"id": nid("btn"), "type": "ui_button", "z": TAB_ID, "group": group,
                "order": gorder, "width": "0", "height": "0", "name": label, "label": label,
                "tooltip": "", "color": "", "bgcolor": "", "className": "", "icon": "",
                "payload": payload, "payloadType": payloadType, "topic": "", "topicType": "str",
                "x": x, "y": y, "wires": [[]]})

def ui_switch(group, label, gorder, col=0):
    newrow()
    x, y = pos(col)
    return add({"id": nid("sw"), "type": "ui_switch", "z": TAB_ID, "group": group,
                "order": gorder, "width": "0", "height": "0", "label": label, "tooltip": "",
                "style": "", "className": "", "passthru": True, "decouple": "false",
                "topic": label, "topicType": "str", "style0": "", "onvalue": "true",
                "onvalueType": "bool", "onicon": "", "oncolor": "", "offvalue": "false",
                "offvalueType": "bool", "officon": "", "offcolor": "", "animate": False,
                "x": x, "y": y, "wires": [[]]})

def ui_numeric(group, label, gorder, default=0, col=0):
    newrow()
    x, y = pos(col)
    return add({"id": nid("num"), "type": "ui_numeric", "z": TAB_ID, "group": group,
                "order": gorder, "width": "0", "height": "0", "name": label, "label": label,
                "tooltip": "", "className": "", "format": "{{value}}", "min": "", "max": "",
                "step": 1, "passthru": False, "topic": label, "topicType": "str",
                "x": x, "y": y, "wires": [[]]})

def ui_dropdown(group, label, options, gorder, col=0):
    newrow()
    x, y = pos(col)
    opts = [{"label": str(o), "value": o, "type": "str"} for o in options]
    return add({"id": nid("dd"), "type": "ui_dropdown", "z": TAB_ID, "group": group,
                "order": gorder, "width": "0", "height": "0", "label": label, "tooltip": "",
                "place": "Select", "className": "", "propertyType": "msg", "property": "payload",
                "multiple": False, "options": opts, "payload": "", "topic": label,
                "topicType": "str", "x": x, "y": y, "wires": [[]]})

def ui_textinput(group, label, gorder, mode="text", col=0):
    newrow()
    x, y = pos(col)
    return add({"id": nid("ti"), "type": "ui_text_input", "z": TAB_ID, "group": group,
                "order": gorder, "width": "0", "height": "0", "name": label, "label": label,
                "tooltip": "", "mode": mode, "delay": "0", "topic": label, "topicType": "str",
                "sendOnBlur": True, "className": "", "x": x, "y": y, "wires": [[]]})

def wire(src, *dsts, port=0):
    flat = [d for arg in dsts for d in (arg if isinstance(arg, list) else [arg])]
    n = next(x for x in _nodes if x["id"] == src)
    while len(n["wires"]) <= port:
        n["wires"].append([])
    n["wires"][port] = list(flat)

# ---------------------------------------------------------------------------
# GROUP: System status
# ---------------------------------------------------------------------------
g_sys = group("System status", "xsc_g_sys", width=6, order=1)

def status_readout(topic, label, code, gorder):
    i = mqtt_in(topic); f = fn(label, code); t = ui_text(g_sys, label, gorder)
    wire(i, f); wire(f, t)

status_readout("xsphere/status/interlocks", "Interlocks", """
var p = msg.payload || {};
msg.payload = (p.ok ? "OK" : "ALERT") + (p.rules_active && p.rules_active.length ? " — " + p.rules_active.join(", ") : "");
return msg;""", 1)
status_readout("xsphere/status/service/heartbeat", "Slow-control uptime", """
var s = (msg.payload && msg.payload.uptime_s) || 0;
var h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
msg.payload = h + "h " + m + "m  (alive)";
return msg;""", 2)
status_readout("xsphere/status/labjack_t7", "LabJack T7", """
var p = msg.payload || {};
msg.payload = p.connected ? ("connected, serial " + p.serial) : ("DISCONNECTED" + (p.error ? " — " + p.error : ""));
return msg;""", 3)
status_readout("xsphere/status/gradient", "Gradient mode / setpoints (K)", """
var p = msg.payload || {}, sp = p.setpoints_k || {};
msg.payload = (p.mode || "?") + "  base=" + (p.base_k) + "  dv=" + (p.delta_v_k) + "  dl=" + (p.delta_l_k) +
  "  | top=" + sp.top + " bot=" + sp.bottom + " noz=" + sp.nozzle;
return msg;""", 4)

# ---------------------------------------------------------------------------
# GROUP: Valves
# ---------------------------------------------------------------------------
g_valve = group("Valves (XV)", "xsc_g_valve", width=6, order=2)
VESSELS = [("cryostat", "XV3 cryostat"), ("primary_xe", "XV2 primary Xe"), ("ballast", "XV1 ballast")]
go = 1
for v, lbl in VESSELS:
    # status readout
    si = mqtt_in(f"xsphere/status/valve/{v}")
    sf = fn(f"{v} state", f"""
var p = msg.payload || {{}};
msg.payload = "{lbl}: " + (p.state ? "OPEN" : "closed") + "  (desired " + (p.desired?1:0) + ")  auto_open=" + (p.auto_open?1:0) + " auto_close=" + (p.auto_close?1:0);
return msg;""")
    st = ui_text(g_valve, lbl, go); go += 1
    wire(si, sf); wire(sf, st)
    # one command publisher per vessel: ui controls tagged via msg.topic
    cmd_fn = fn(f"{v} cmd", f"""
var t = msg.topic;
if (t === "open") {{ msg.topic = "xsphere/commands/valve/{v}/state"; msg.payload = {{state: msg.payload ? 1 : 0}}; }}
else if (t === "auto_open") {{ msg.topic = "xsphere/commands/valve/{v}/auto_open"; msg.payload = {{enabled: !!msg.payload}}; }}
else if (t === "auto_close") {{ msg.topic = "xsphere/commands/valve/{v}/auto_close"; msg.payload = {{enabled: !!msg.payload}}; }}
else return null;
return msg;""")
    cmd_out = mqtt_out("")  # topic from msg.topic
    wire(cmd_fn, cmd_out)
    sw_open  = ui_switch(g_valve, f"{lbl}: OPEN", go); go += 1
    sw_aopen = ui_switch(g_valve, f"{lbl}: auto-open", go); go += 1
    sw_aclos = ui_switch(g_valve, f"{lbl}: auto-close", go); go += 1
    for sw, topic in ((sw_open, "open"), (sw_aopen, "auto_open"), (sw_aclos, "auto_close")):
        # the ui_switch's `topic` is its label; retag to the short key before cmd_fn
        retag = fn(f"{v} {topic}", f'msg.topic = "{topic}"; return msg;')
        wire(sw, retag); wire(retag, cmd_fn)

# ---------------------------------------------------------------------------
# GROUP: Heater PID (one group per zone)
# ---------------------------------------------------------------------------
ZONES = ["top", "bottom", "nozzle"]
for zi, zone in enumerate(ZONES):
    g = group(f"Heater — {zone}", f"xsc_g_pid_{zone}", width=6, order=3 + zi)
    o = 1
    # readout
    ri = mqtt_in(f"xsphere/status/pid/{zone}")
    rf = fn(f"{zone} pid readout", """
var p = msg.payload || {};
msg.payload = "SP " + p.setpoint_k + " K (" + p.setpoint_c + " °C)  |  PV " + p.pv_k + " K  |  out " + p.output_pct + " %  |  Kp/Ki/Kd " + p.kp + "/" + p.ki + "/" + p.kd;
return msg;""")
    rt = ui_text(g, "readout", o); o += 1
    wire(ri, rf); wire(rf, rt)
    # setpoint entry
    sp_num = ui_numeric(g, f"{zone} setpoint (K)", o, default=165); o += 1
    sp_btn = ui_button(g, f"Set {zone} setpoint", o, payload="set", payloadType="str"); o += 1
    sp_fn  = fn(f"{zone} setpoint cmd", f"""
var v = global.get("xsc_sp_{zone}");
if (v === undefined || v === null) return null;
return {{ topic: "xsphere/commands/pid/{zone}/setpoint", payload: {{ value_k: Number(v) }} }};""")
    sp_store = fn(f"store {zone} sp", f'global.set("xsc_sp_{zone}", Number(msg.payload)); return null;')
    sp_out = mqtt_out("")
    wire(sp_num, sp_store)
    wire(sp_btn, sp_fn); wire(sp_fn, sp_out)
    # gains entry
    kp = ui_numeric(g, f"{zone} Kp", o); o += 1
    ki = ui_numeric(g, f"{zone} Ki", o); o += 1
    kd = ui_numeric(g, f"{zone} Kd", o); o += 1
    gain_store = fn(f"store {zone} gains", f"""
var g = global.get("xsc_gains_{zone}") || {{}};
if (msg.topic && msg.topic.indexOf("Kp")>=0) g.kp = Number(msg.payload);
if (msg.topic && msg.topic.indexOf("Ki")>=0) g.ki = Number(msg.payload);
if (msg.topic && msg.topic.indexOf("Kd")>=0) g.kd = Number(msg.payload);
global.set("xsc_gains_{zone}", g); return null;""")
    gain_btn = ui_button(g, f"Set {zone} gains", o, payload="set", payloadType="str"); o += 1
    gain_fn  = fn(f"{zone} gains cmd", f"""
var g = global.get("xsc_gains_{zone}") || {{}};
return {{ topic: "xsphere/commands/pid/{zone}/gains", payload: g }};""")
    gain_out = mqtt_out("")
    for w in (kp, ki, kd): wire(w, gain_store)
    wire(gain_btn, gain_fn); wire(gain_fn, gain_out)

# ---------------------------------------------------------------------------
# GROUP: Gradient
# ---------------------------------------------------------------------------
g_grad = group("Gradient", "xsc_g_grad", width=6, order=10)
o = 1
mode_sw = ui_switch(g_grad, "Gradient mode (off = absolute)", o); o += 1
mode_fn = fn("gradient mode cmd", 'return { topic: "xsphere/commands/gradient/mode", payload: { mode: msg.payload ? "gradient" : "absolute" } };')
mode_out = mqtt_out(""); wire(mode_sw, mode_fn); wire(mode_fn, mode_out)
for param, cmd, key in [("base (K)", "base", "value_k"), ("Δvertical (K)", "vertical", "delta_k"), ("Δlongitudinal (K)", "longitudinal", "delta_k")]:
    num = ui_numeric(g_grad, f"gradient {param}", o, default=165 if cmd == "base" else 0); o += 1
    store = fn(f"store grad {cmd}", f'global.set("xsc_grad_{cmd}", Number(msg.payload)); return null;')
    btn = ui_button(g_grad, f"Set {param}", o, payload="set"); o += 1
    cfn = fn(f"grad {cmd} cmd", f"""
var v = global.get("xsc_grad_{cmd}");
if (v === undefined || v === null) return null;
return {{ topic: "xsphere/commands/gradient/{cmd}", payload: {{ "{key}": Number(v) }} }};""")
    cout = mqtt_out("")
    wire(num, store); wire(btn, cfn); wire(cfn, cout)

# ---------------------------------------------------------------------------
# GROUP: Follow an RTD (zone setpoint tracks a sensor)
# ---------------------------------------------------------------------------
g_follow = group("Follow a sensor", "xsc_g_follow", width=6, order=11)
o = 1
SENSORS = [
    "xsphere/sensors/temperature/plc/rtd/4", "xsphere/sensors/temperature/plc/rtd/5",
    "xsphere/sensors/temperature/plc/rtd/6", "xsphere/sensors/temperature/labjack/rtd/1",
    "xsphere/sensors/temperature/labjack/rtd/2", "xsphere/sensors/temperature/labjack/rtd/3",
]
zone_dd = ui_dropdown(g_follow, "Zone to drive", ZONES, o); o += 1
src_dd  = ui_dropdown(g_follow, "Source sensor (RTD topic)", SENSORS, o); o += 1
en_sw   = ui_switch(g_follow, "Follow enabled", o); o += 1
follow_cfg = fn("follow cfg", """
if (msg.topic === "Zone to drive") flow.set("follow_zone", msg.payload);
else if (msg.topic === "Source sensor (RTD topic)") flow.set("follow_src", msg.payload);
else if (msg.topic === "Follow enabled") flow.set("follow_on", !!msg.payload);
var on = flow.get("follow_on"), z = flow.get("follow_zone"), s = flow.get("follow_src");
return { payload: on ? (z + " ← " + (s || "(no source)")) : "off" };""")
follow_txt = ui_text(g_follow, "follow status", o); o += 1
wire(zone_dd, follow_cfg); wire(src_dd, follow_cfg); wire(en_sw, follow_cfg)
wire(follow_cfg, follow_txt)
# tap all temperature sensor topics; when follow is on and the topic matches the chosen source, republish as the zone setpoint
follow_in = mqtt_in("xsphere/sensors/temperature/#")
follow_fn = fn("follow -> setpoint", """
if (!flow.get("follow_on")) return null;
var src = flow.get("follow_src"), zone = flow.get("follow_zone");
if (!src || !zone || msg.topic !== src) return null;
var vk = msg.payload && (msg.payload.value_k);
if (vk === undefined || vk === null) return null;
return { topic: "xsphere/commands/pid/" + zone + "/setpoint", payload: { value_k: Number(vk) } };""")
follow_out = mqtt_out("")
wire(follow_in, follow_fn); wire(follow_fn, follow_out)

# ---------------------------------------------------------------------------
# GROUP: Ramp sequencer (ad-hoc multi-step, pure Node-RED)
# ---------------------------------------------------------------------------
g_seq = group("Ramp sequencer", "xsc_g_seq", width=6, order=12)
o = 1
seq_txt_in = ui_textinput(g_seq, "Steps (one per line: ZONE TARGET_K HOLD_MIN; ZONE = top/bottom/nozzle/base)", o, mode="multiline"); o += 1
seq_store = fn("store seq text", 'flow.set("seq_text", String(msg.payload || "")); return null;')
seq_run = ui_button(g_seq, "Run sequence", o, payload="run"); o += 1
seq_stop = ui_button(g_seq, "Stop sequence", o, payload="stop"); o += 1
seq_status_txt = ui_text(g_seq, "sequence status", o); o += 1
seq_fn = fn("ramp sequencer", """
// run-state lives in this node's context: steps[], idx, timer
function emit(zone, vk){
    var t = (zone === "base") ? "xsphere/commands/gradient/base"
                              : "xsphere/commands/pid/" + zone + "/setpoint";
    node.send([{ topic: t, payload: { value_k: vk } }, null]);
}
function status(s){ node.send([null, { payload: s }]); }
function clearT(){ var h = context.get("timer"); if (h){ clearTimeout(h); context.set("timer", null); } }
function runStep(){
    var steps = context.get("steps") || [], i = context.get("idx") || 0;
    if (i >= steps.length){ status("done (" + steps.length + " steps)"); clearT(); return; }
    var st = steps[i];
    emit(st.zone, st.vk);
    status("step " + (i+1) + "/" + steps.length + ": " + st.zone + " -> " + st.vk + " K, hold " + st.hold + " min");
    context.set("idx", i+1);
    context.set("timer", setTimeout(runStep, st.hold * 60000));
}
if (msg.payload === "stop"){ clearT(); status("stopped"); return null; }
if (msg.payload === "run"){
    var raw = flow.get("seq_text") || "", steps = [];
    raw.split(/\\r?\\n/).forEach(function(line){
        line = line.trim(); if (!line) return;
        var p = line.split(/[\\s,]+/);
        if (p.length >= 3 && !isNaN(Number(p[1])) && !isNaN(Number(p[2])))
            steps.push({ zone: p[0], vk: Number(p[1]), hold: Number(p[2]) });
    });
    if (!steps.length){ status("no valid steps parsed"); return null; }
    clearT(); context.set("steps", steps); context.set("idx", 0); runStep(); return null;
}
return null;""", outputs=2)
seq_out = mqtt_out("")
wire(seq_txt_in, seq_store)
wire(seq_run, seq_fn); wire(seq_stop, seq_fn)
wire(seq_fn, seq_out, port=0); wire(seq_fn, seq_status_txt, port=1)

# ---------------------------------------------------------------------------
# GROUP: Automated gradient scan (drives the slowcontrol gradient_scanner plugin)
# ---------------------------------------------------------------------------
g_scan = group("Gradient scan", "xsc_g_scan", width=6, order=13)
o = 1
sc_start_k = ui_numeric(g_scan, "scan start (K)", o, default=160); o += 1
sc_end_k   = ui_numeric(g_scan, "scan end (K)", o, default=200); o += 1
sc_step_k  = ui_numeric(g_scan, "scan step (K)", o, default=5); o += 1
sc_dwell_m = ui_numeric(g_scan, "dwell per step (min)", o, default=10); o += 1
sc_collect = fn("collect scan params", """
var c = flow.get("scan") || {};
if (msg.topic === "scan start (K)") c.start_k = Number(msg.payload);
if (msg.topic === "scan end (K)")   c.end_k   = Number(msg.payload);
if (msg.topic === "scan step (K)")  c.step_k  = Number(msg.payload);
if (msg.topic === "dwell per step (min)") c.dwell_m = Number(msg.payload);
flow.set("scan", c); return null;""")
for w in (sc_start_k, sc_end_k, sc_step_k, sc_dwell_m): wire(w, sc_collect)
sc_run_btn = ui_button(g_scan, "Start scan", o, payload="start"); o += 1
sc_stop_btn = ui_button(g_scan, "Stop scan", o, payload="stop"); o += 1
sc_cmd = fn("scan cmd", """
if (msg.payload === "stop"){ return { topic: "xsphere/commands/gradient_scanner/stop", payload: {} }; }
var c = flow.get("scan") || {};
if (c.start_k===undefined || c.end_k===undefined || c.step_k===undefined || c.dwell_m===undefined){
    node.warn("scan params incomplete"); return null;
}
return { topic: "xsphere/commands/gradient_scanner/start",
         payload: { start_k: c.start_k, end_k: c.end_k, step_k: c.step_k, dwell_s: c.dwell_m * 60 } };""")
sc_out = mqtt_out("")
wire(sc_run_btn, sc_cmd); wire(sc_stop_btn, sc_cmd); wire(sc_cmd, sc_out)
sc_status_in = mqtt_in("xsphere/status/gradient_scanner")
sc_status_fn = fn("scan status txt", 'msg.payload = JSON.stringify(msg.payload); return msg;')
sc_status_txt = ui_text(g_scan, "scan status", o); o += 1
wire(sc_status_in, sc_status_fn); wire(sc_status_fn, sc_status_txt)

# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------
def main():
    out_path = "nodered/control-flows.json"
    with open(out_path, "w") as fh:
        json.dump(_nodes, fh, indent=2)
    print(f"wrote {out_path} ({len(_nodes)} nodes)")

    if "--deploy" in sys.argv:
        base = "http://localhost:1880"
        existing = json.loads(urllib.request.urlopen(base + "/flows").read())
        new_nodes = list(_nodes)
        # add our own ui_base only if the install has none yet
        if not any(n.get("type") == "ui_base" for n in existing):
            new_nodes.append({"id": UI_BASE_ID, "type": "ui_base",
                              "theme": {"name": "theme-light", "lightTheme": {"default": "#0094CE", "baseColor": "#0094CE",
                                        "baseFont": "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Oxygen-Sans,Ubuntu,Cantarell,Helvetica Neue,sans-serif", "edited": False}},
                              "site": {"name": "xsphere", "hideToolbar": "false", "allowSwipe": "false",
                                       "lockMenu": "false", "allowTempTheme": "true", "dateFormat": "DD/MM/YYYY", "sizes": {}}})
        # drop any nodes we previously added (idempotent re-deploy), keep everything else
        keep = [n for n in existing if not str(n.get("id", "")).startswith(("xsc_", "xsphere_"))]
        merged = keep + new_nodes
        req = urllib.request.Request(base + "/flows", data=json.dumps(merged).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Node-RED-Deployment-Type": "full"}, method="POST")
        try:
            r = urllib.request.urlopen(req)
            print("deploy:", r.status, r.read().decode()[:200])
        except urllib.error.HTTPError as e:
            print("deploy FAILED:", e.code, e.read().decode()[:500])
            sys.exit(1)


if __name__ == "__main__":
    main()
