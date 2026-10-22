# AI control service (`xsphere-aicontrol`)

The AI control service lets a Claude session read the whole experiment and,
inside limits a human sets, change three things: the heaters, the LN2 fill
valves and the pneumatic gas valves. It runs on xbox-pi as the systemd unit
`xsphere-aicontrol`, on port 8089. It offers an MCP endpoint (`POST /mcp`, for
Claude Code) and a REST API (`/api/v1`) with the same tools.

It talks to the slow control service only over MQTT, as the web panel does.
It reads `xsphere/state/snapshot` and the `xsphere/status/#` topics, and
publishes on a fixed list of `xsphere/commands/…` topics. Slow control and the
PLC are unchanged.

**Why a separate service?** Every AI write passes the same checks in one
process: is the group armed, is the value inside the rails, is the system in a
state where the write is sane. Every write is then confirmed from the status
topics and logged. The service only works if the AI has no other route to the
hardware. The limitation section below says what that requires.

## Safety model

### Arms

There are three groups: **heaters**, **ln2** and **pneumatic**. A human arms
each one on the Control page (`http://xbox-pi:8088/control`, "AI control"
card).

- Every arm is off when the service starts. Arms are never saved to disk.
- An arm expires after `general.arm_hours` (default 8 h).
- Disarming moves no hardware. It only stops further AI writes, and it stops a
  running heater ramp.
- **disarm all** on the card disarms every group at once.

### Rails

Rails are the human's limits. Lucas edits them in the card's "rails" editor.
The service stores them in `aicontrol/rails.json` on the Pi (gitignored), with
a revision number.

- The only way to change them is a full rails document published on
  `xsphere/commands/ai_control/rails`, carrying the revision the editor
  started from. If someone saved in between, the save is refused.
- No AI tool and no REST endpoint can change rails.
- Writes are denied by default:
  - A missing rails file means nothing is writable.
  - An unset min / max pair, or a permission left false, means the item it
    covers is not writable. An unset step, rate or arm time means no limit.
  - An invalid file means nothing is writable until the rails are saved again.

| Group | Rails (per item) |
|---|---|
| heaters, per zone (top / bottom / nozzle) | `setpoint_min_k` and `setpoint_max_k`: both are needed before anything for the zone can be written. `max_step_k`: largest single change. `max_rate_k_per_min`: average rate, also the ramp-speed cap. `pv_sources`: `cube` / `clamp`, the sensors the AI may make the PID's process value (nozzle: `cube` only) |
| heaters, per supply channel (1 / 2 / 3) | `voltage_min_v` / `voltage_max_v` (≤ 32 V): both are needed to change the voltage. `current_min_a` / `current_max_a` (≤ 3 A): both are needed to change the current. `may_switch_on`, `may_switch_off` |
| ln2, per vessel (cryostat / primary_xe / ballast) | `may_operate`. `max_open_minutes` (≤ 120): required before the AI may open the valve. `may_set_auto_open`, `may_disarm_auto_close` |
| pneumatic, per valve (ballast / pump / bottle) | `may_open`, `may_close`. Also, for the group: `forbid_open_together` (pairs that may not be open at once) and `min_interval_s` (default 10 s between commands to one valve) |
| general | `arm_hours`: default 8; null means arms never expire |

Zone rails are tied to the sensors each software zone used when they were
saved. If the zone-to-sensor mapping changes, for example when branch
`claude/swap-top-bottom` is deployed, that zone's heater rails count as unset,
and the card says so, until the rails are saved again. This keeps a rail
meant for one heater from applying to the other.

### Checks on every write

These checks run in this order:

1. **Arguments.** They must have exact JSON types (no NaN, no booleans posing
   as numbers, no unknown keys), and include a `reason` of 200 characters or
   fewer.
2. **One write at a time.** A second concurrent write is refused, not queued.
3. **Group armed.**
4. **Rails** for the specific item.
5. **Preconditions:**
   - The broker is connected.
   - The snapshot is at most 5 s old, and slow control is alive.
   - The Sequencer is not running, and no tracker is enabled.
   - Each group adds its own preconditions:
     - **Heaters:**
       - The PV interlock is not tripped.
       - The setpoint is at least 5 K inside the interlock band.
       - The PID status is live.
       - A setpoint increase needs a live PV source.
       - A non-numeric setpoint expression is only replaced when the call
         says `replace_expression: true`.
       - `max_step_k` and `max_rate_k_per_min` apply, but a move toward the
         rails range is always allowed.
     - **LN2:**
       - Opening needs `auto_close` on (live) and a fresh level reading: the
         probe for the cryostat; the level plus the GHS ESP32 for
         ballast / primary_xe.
       - Closing turns `auto_open` off first.
       - `auto_open` can only be switched on while `auto_close` is on.
     - **Pneumatic:**
       - The PLC's pneumatic gate on the Control page must also be enabled.
         This is a second, independent gate.
       - A `forbid_open_together` partner must not be open.
6. **Rate limits:**
   - 20 write calls per minute overall.
   - The pneumatic `min_interval_s` and 10 s per LN2 valve.
   - At most 6 heater-supply commands per minute, each one waiting for the
     supply's status.
7. **Publish.** The arm is re-checked immediately before every single publish.
   A disarm in the middle of a multi-step call stops the remaining steps.
8. **Confirm** from the status topics, within 5 s, on two consecutive messages
   where possible. When a PV-source change is not confirmed, it is reverted.
9. **Audit.** The call is written to `aicontrol/audit.jsonl` and to the
   retained `xsphere/status/ai_control/audit` (last 50 entries, shown on the
   card).

### Automatic disarm

The service disarms by itself; it never re-arms. Each disarm is audited and
shown on the card with its reason.

- **All groups:**
  - an arm expires;
  - this service reconnects to the broker;
  - slow control restarts (heartbeat `uptime_s` goes down, or `service_alive`
    goes false → true);
  - the snapshot is older than 30 s.
- **Heaters:** the PV interlock trips, or `xsphere/status/interlocks` turns
  not-ok.
- **LN2:** the auto-close timer below fires.

### LN2 auto-close timer

When the AI opens a fill valve, the service records the opening time and a
deadline `max_open_minutes` later in `aicontrol/runtime.json`. If the valve is
still open at the deadline, the service:

1. turns `auto_open` off, if it is on;
2. closes the valve;
3. audits "auto-closed after N min";
4. disarms ln2.

This is the service's own safety action, so it runs even while ln2 is
disarmed. The timer survives a service restart, because it is reloaded at
start. It does **not** run while the service is stopped; the PLC ladder's own
level and timer logic still applies then.

### What the AI can and cannot do

**Can:**
- read everything: the overview, any state, InfluxDB history, the rails and
  the audit;
- set PID setpoints, directly, as base + ΔV + ΔL, or as a ramp;
- put a manual zone back to auto;
- choose a zone's PV source from the sensors the rails allow;
- set NGE supply voltage, current and output;
- open and close LN2 fill valves and set their auto flags;
- open and close the pneumatic valves;
- write notes into the audit log.

**Cannot:**
- touch the turbo pumps;
- change interlock limits, the PV interlock band, or the pneumatic / turbo
  gates;
- change arms or rails;
- start the Sequencer or touch trackers;
- use the `gradient/*` commands;
- set PID manual mode, manual output, gains, autotune, or a free-form setpoint
  or PV expression;
- use NGE hold;
- publish any MQTT topic outside the fixed list in `bus.py`;
- run Flux of its own (history queries are built from fixed templates).

### The limitation

Arms and rails bind only clients that go through this service. Everything else
on the lab network can still reach the hardware:

- The MQTT broker accepts anonymous publishes from anyone.
- The web panel's `/api/cmd` relays any `xsphere/commands/…` topic without
  authentication. For `ai_control/*` it only insists on JSON and an allowed
  Origin.
- Node-RED, code-server (a shell as `xbox`) and InfluxDB are all open on the LAN.

A Claude session with a shell, a browser, web access or a way to message
another session can therefore bypass all of this, including arming itself.
**Only use the operator profile (`aicontrol/operator/`) for an AI that should
be confined,** and run its acceptance check.

## Tools

The same tools are available over MCP and REST. Zone names are the
*software* names. `get_overview` prints which physical heater, sensors and
supply channel each zone is, worked out from `slowcontrol/state.yaml`.

**Reads (no arm needed):**

| Tool | What it does |
|---|---|
| `get_overview` | One-page plain-text status: arms and what the AI may do now, the interlock, each zone, supply, LN2, valves, pressures, turbos, warnings, the last AI actions (`format: "json"` for JSON) |
| `list_states` | Compact rows of registry states (id, label, value, freshness, which tool writes it); filter by group or text |
| `get_states` | Full snapshot views for up to 40 state ids, with the raw driver status for PID / valve / NGE / pneumatic ones |
| `get_ai_control` | Arms, the full rails, rails errors or stale zones, active ramps, LN2 timers, counters, the last 20 audit entries |
| `query_history` | InfluxDB history for up to 8 ids: relative or RFC3339 start, aggregation (`mean`/`min`/`max`/`last`/`count`/`changes`), at most 1000 points per series and 2000 per call |
| `get_guide` | Markdown guide: heaters, ln2, pneumatic, history, safety |

**Writes:**

| Tool | Group | What it does |
|---|---|---|
| `set_heater_setpoints` | heaters | Set one or more zone setpoints in K, all validated before any is published |
| `set_heater_gradient` | heaters | Set top = base, bottom = base + dv, nozzle = base + dl (software zone names), through the same path |
| `start_heater_ramp` | heaters | Move zones to targets at a rate in K/min. The service steps the setpoint every 10 s and re-checks everything at each step |
| `stop_heater_ramp` | none | Stop ramps (always allowed, audited) |
| `set_heater_auto` | heaters | Put a manual zone back into auto, if its setpoint is numeric and inside the rails |
| `set_pv_source` | heaters | Make the zone's cube or clamp RTD its PID process value, as the rails allow |
| `set_heater_supply` | heaters | NGE channel voltage → current → output, each step confirmed |
| `set_ln2_valve` | ln2 | Open (with the auto-close timer) or close a fill valve |
| `set_ln2_auto` | ln2 | Set a vessel's `auto_open` / `auto_close` flags, in the safe order |
| `set_pneumatic_valve` | pneumatic | Open or close ballast, pump or bottle |
| `add_note` | none | A logbook entry in the audit, shown on the card |

Every write returns `{"ok", "tool", "refused"?, "error"?, "published": [...],
"confirmed", "readback", "warnings", "note"?}`, and MCP marks it `isError`
whenever `ok` is false:

- `refused`: nothing was sent. The sentence says what is missing (an arm, a
  rail, a precondition).
- `error`: something was sent but did not take effect: a PV source that never
  evaluated (it is put back), a supply command the NGE rejected, a gas valve
  the PLC's gate refused, or an earlier step of a multi-step call that was not
  confirmed (the later steps are not sent). `published` lists what went out.
- `error` with `"invalid_arguments": true`: the arguments failed validation;
  nothing was sent.
- `confirmed: false` with `ok: true`: the command went out but the status did
  not show it within 5 s; `warnings` says so.

Reads return their data with `ok: true`, or `ok: false` and an `error`
(`query_history` without an InfluxDB token, a bad id, …).

## REST reference

The base URL is `http://xbox-pi:8089`. Responses are JSON, apart from `/`.

| Method and path | Same as |
|---|---|
| `GET /healthz` | liveness |
| `GET /` | read-only status page: arms, rails, ramps, timers, audit, how to connect |
| `POST /mcp` | the MCP endpoint (Streamable HTTP, JSON responses, no sessions; protocol 2026-07-28, and 2025-11-25 / 2025-06-18 for older clients) |
| `GET /api/v1/overview` | `get_overview` (`?format=json`) |
| `GET /api/v1/states` | `list_states` (`?group=&query=&limit=`) |
| `GET /api/v1/states/<id>` | `get_states` for one id |
| `GET /api/v1/history` | `query_history` (`?ids=a,b&start=-6h&stop=&every=&fn=&max_points=`) |
| `GET /api/v1/ai_control` | `get_ai_control` |
| `GET /api/v1/guide` | `get_guide` (`?topic=`) |
| `POST /api/v1/actions/<tool>` | any tool, with its arguments as the JSON body |

| Status | Meaning |
|---|---|
| 200 | the tool ran |
| 400 | invalid arguments (also an unknown or repeated query parameter), or a read that failed |
| 403 | the request carried an `Origin` header; browsers are refused on `/mcp` and `/api/v1` |
| 404 | unknown tool or endpoint |
| 405 | `/mcp` with any method but POST (`Allow: POST`) |
| 409 | a write refused (not armed, outside the rails, a precondition failed) or one that failed after it was sent |
| 413 | a body over 1 MiB |
| 415 | a POST without `Content-Type: application/json` |
| 421 | the `Host` header is not in `XSPHERE_AICONTROL_ALLOWED_HOSTS` |
| 500 | an internal error (logged with its traceback) |

A REST write is held to exactly the same checks as an MCP write; REST is not a
side door.

```bash
curl -s http://xbox-pi:8089/api/v1/overview
curl -s -X POST http://xbox-pi:8089/api/v1/actions/get_states \
  -H 'Content-Type: application/json' -d '{"ids": ["pid_top_setpoint"]}'
```

**Human-only MQTT** (sent by the Control page card through `/api/cmd`):

| Topic | Payload |
|---|---|
| `xsphere/commands/ai_control/arm` | `{"group": "heaters"\|"ln2"\|"pneumatic", "armed": true\|false, "by": "…"}`. Use `"group": "all"` with `false` only |
| `xsphere/commands/ai_control/rails` | `{"rails": {full document}, "base_revision": N, "by": "…"}` |

Status goes out retained on `xsphere/status/ai_control`, every 2 s and on
change; the retained last will says offline and disarmed. The audit goes out
retained on `xsphere/status/ai_control/audit`. Telegraf stores the status as
measurement `ai_control` (`online`, `armed_*`, `actions`, `refused`,
`auto_disarms`).

## Connecting Claude Code

- **A confined operator:** follow `aicontrol/operator/README.md`. Copy the
  profile out of the repo, open Claude Code there, trust the folder once, and
  run the acceptance check.
- **Any other session**, for example while developing:
  `claude mcp add --transport http xsphere http://xbox-pi:8089/mcp`.
  That session keeps all its other tools, so it is **not** confined.

No authentication is used. The service is LAN-only, like the rest of the Pi.

## Deploy (on xbox-pi)

Once the code is on the Pi's checkout (`~/xsphere-slow-control`):

1. **Check that it imports** with the services' interpreter:
   ```bash
   ~/miniconda3/envs/slowcontrol/bin/python -c "import aicontrol.app"
   ```
2. **Optional but recommended:** give it a read-only InfluxDB token (next
   section).
3. **Install and start the unit:**
   ```bash
   sudo cp aicontrol/xsphere-aicontrol.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now xsphere-aicontrol
   ```
4. **Check it:**
   ```bash
   journalctl -u xsphere-aicontrol -n 30      # instance=production, topics, token source
   curl -s http://localhost:8089/healthz
   docker exec mosquitto mosquitto_sub -h localhost -t xsphere/status/ai_control -C 1 -v
   ```
   Slow control does **not** need a restart, and no heater or valve is
   touched. The service starts with everything disarmed and no rails, so it is
   read-only until someone saves rails and arms a group.
5. **The Control page card** goes live only after a web panel restart, because
   webcontrol caches its templates. That restart drops `/diag` and any follow
   or ramp loop the panel is running, so first check that
   `curl -s localhost:8088/diag/status` shows `"active": false`. Then run
   `sudo systemctl restart xsphere-webcontrol`.
6. **The Telegraf input** is stored only after
   `bash telegraf/deploy-iotstack.sh` (preview) and then `--apply`. That should
   report 15/15 MQTT inputs connected; see OPERATIONS.md, "Changing what
   Telegraf records".

To take it out: `sudo systemctl disable --now xsphere-aicontrol`. It publishes
"offline, disarmed" as it stops. Nothing else depends on it.

### InfluxDB token

By default the service uses `INFLUX_TOKEN` from the repo's `.env`, which is
all-access. The service never sends Flux the AI wrote, only fixed templates
over a whitelist of ids, but a read-only token limits what a bug could do.

1. In the InfluxDB UI (`http://xbox-pi:8087`), go to **Load Data → API Tokens →
   Generate API Token → Custom API Token**. Choose **Read** on bucket
   `xsphere` only, and describe it as "xsphere-aicontrol (read)".
2. Put it in an environment file only root can read, and point the unit at it
   with a drop-in. The unit file itself stays as it is in the repo.
   ```bash
   sudo install -d -m 755 /etc/xsphere
   sudo install -m 600 /dev/null /etc/xsphere/aicontrol.env
   sudo nano /etc/xsphere/aicontrol.env       # one line: XSPHERE_AICONTROL_INFLUX_TOKEN=<token>
   sudo mkdir -p /etc/systemd/system/xsphere-aicontrol.service.d
   printf '[Service]\nEnvironmentFile=/etc/xsphere/aicontrol.env\n' \
     | sudo tee /etc/systemd/system/xsphere-aicontrol.service.d/influx-token.conf
   sudo systemctl daemon-reload
   sudo systemctl restart xsphere-aicontrol   # disarms everything (safe)
   journalctl -u xsphere-aicontrol -n 30 | grep -i token
   ```
   systemd reads the file as root before it starts the service as `xbox`, so
   mode 600 is enough. The log names the token's source, never its value.

## Testing

- **On the DAQ PC**, run
  `C:\Users\xbox\anaconda3\python.exe -m pytest tests/test_aicontrol_*.py -q`.
  The tests use an in-process MQTT loopback and a fake slow-control responder,
  with no broker and no network.
- **`python -m aicontrol`** without the unit's environment starts a *test*
  instance:
  - its client id and topics live under `xsphere/test/…`;
  - it refuses any broker other than localhost;
  - it falls back to Flask's server when waitress is missing.

  Never set `XSPHERE_AICONTROL_INSTANCE=production` off the Pi. The broker
  would keep kicking the real service off its client id, and the copy would
  obey the real arm buttons.
- **The operator profile:** the acceptance check in
  `aicontrol/operator/README.md`.

## Files

| Path | Contents |
|---|---|
| `config.py` | settings from the environment; production vs test instance |
| `bus.py` | MQTT client, caches, command-topic allowlist, waits |
| `zones.py` | zone, sensor, channel and valve maps detected from `state.yaml` |
| `rails.py` | rails schema, validation, `RailsStore` |
| `arms.py` | arms, the auto-disarm supervisor, LN2 timers, heater ramps |
| `actions.py`, `reads.py` | the write and read tools |
| `history.py` | registry → Telegraf map and the Flux templates |
| `mcp.py`, `tools.py`, `app.py` | MCP protocol, tool registry, Flask app |
| `templates/index.html` | the read-only status page |
| `xsphere-aicontrol.service` | systemd unit |
| `operator/` | the Claude Code operator profile template |
| `rails.json`, `audit.jsonl`, `runtime.json` | runtime state on the Pi (gitignored) |
