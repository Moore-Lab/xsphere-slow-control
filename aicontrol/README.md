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
process: does the caller hold a write token, is the group armed for it, is the
value inside the rails, does the gas pressure allow it, is the system in a
state where the write is sane. Every write is then confirmed from the status
topics and logged. The service only works if the AI has no other route to the
hardware. The limitation section below says what that requires.

## Safety model

### Arms

There are three groups: **heaters**, **ln2** and **pneumatic**. Lucas arms
each one on the Control page (`http://xbox-pi:8088/control`, "AI control"
card), with a one-time code from his authenticator app.

- An arm is a **lease**:
  - for one named AI client (for example `operator`). Every other client, and
    any caller without a write token, stays refused;
  - for at most `general.arm_hours` (default 2 h, at most 12 h; never
    open-ended). The arm message may ask for less;
  - for at most `general.write_budget` write calls (default 30). The arm
    message may ask for fewer. When the budget is used up, the group disarms.
- Arming the group again, for the same or another client, starts a new lease.
  A lease taken over by another client stops the old client's ramps.
- Every arm is off when the service starts. Arms are never saved to disk.
- Disarming moves no hardware. It only stops further AI writes, and it stops a
  running heater ramp. **Disarm needs no code**, and neither does
  **disarm all**.

### One-time codes

An arm or a rails save is an MQTT message, and the broker is anonymous, so each
one must carry a 6-digit code (RFC 6238 TOTP: HMAC-SHA1, 30 s steps). The
service checks it against `XSPHERE_AICONTROL_TOTP_SECRET`, a secret only it and
Lucas's phone hold (`auth.py`).

- The code of the current 30 s step and of one step either side is accepted.
- Each step is used once: a code that went over the wire cannot arm anything
  again, and an older code is refused once a later one was used. To arm a
  second group, wait for the next code.
- After 5 wrong codes within 5 minutes, every code is refused for 5 minutes.
  The status shows the remaining lockout (`codes_locked_s`).
- A command that is malformed or would be refused anyway (unknown client, more
  hours than the rails allow, an invalid rails document) is refused *before*
  its code is checked, so it does not use the code up.
- Every refused code is audited as kind `auth_failed`. The code itself is
  never written anywhere.
- With no secret configured, nothing can be armed and the rails cannot be
  saved. Disarming still works. See "Enrolling".

### Write tokens

Each AI client has its own write token. An MCP or REST request sends it as
`Authorization: Bearer <token>`. The service holds only SHA-256 hashes, in
`XSPHERE_AICONTROL_CLIENTS=name:hash,…`.

- Reads work without a token.
- Every write tool (including `add_note` and `stop_heater_ramp`) refuses a
  caller without a known token. The refusal is a tool result (`isError`), never
  HTTP 401: Claude Code would take a 401 as an OAuth challenge.
- Every audit entry names its `client`: the AI client, `anonymous` for a call
  without a valid token, `human` for a rails save, `service` for the service's
  own entries.

### Rails

Rails are the human's limits. Lucas edits them in the card's "rails" editor.
The service stores them in `aicontrol/rails.json` on the Pi (gitignored), with
a revision number.

- The only way to change them is a full rails document published on
  `xsphere/commands/ai_control/rails`, carrying the revision the editor
  started from and a one-time code. If someone saved in between, the save is
  refused.
- No AI tool and no REST endpoint can change rails.
- Writes are denied by default:
  - A missing rails file means nothing is writable.
  - An unset min / max pair, or a permission left false, means the item it
    covers is not writable. An unset step or rate means no limit.
  - An invalid file means nothing is writable until the rails are saved again.
- The schema is **version 3**. A version-2 `rails.json` (from before the
  guard, the leases and the clamp cross-check) counts as invalid: nothing is
  writable until the rails are saved again on the card.

| Group | Rails (per item) |
|---|---|
| heaters, per zone (top / bottom / nozzle) | `setpoint_min_k` and `setpoint_max_k`: both are needed before anything for the zone can be written; `setpoint_min_k` is also where the pressure guard puts an AI-set zone on a trip. `max_step_k`: largest single change. `max_rate_k_per_min`: average rate, also the ramp-speed cap. `pv_sources`: `cube` / `clamp`, the sensors the AI may make the PID's process value (nozzle: `cube` only) |
| heaters | `pv_cross_check_k` (≤ 50 K): a clamp sensor further than this from the zone's cube RTD is refused as a PV source. Unset: the AI may not choose a clamp at all |
| heaters, per supply channel (1 / 2 / 3) | `voltage_min_v` / `voltage_max_v` (≤ 32 V): both are needed to change the voltage. `current_min_a` / `current_max_a` (≤ 3 A): both are needed to change the current. `may_switch_on`, `may_switch_off` (a minimum of 0 V or 0 A needs it too: it unpowers the heater just as "off" does) |
| ln2, per vessel (cryostat / primary_xe / ballast) | `may_operate`. `max_open_minutes` (≤ 120): required before the AI may open the valve. `may_set_auto_open`, `may_disarm_auto_close` |
| pneumatic, per valve (ballast / pump / bottle) | `may_open`, `may_close`. Also, for the group: `forbid_open_together` (pairs that may not be open at once) and `min_interval_s` (default 10 s between commands to one valve) |
| general | `arm_hours`: the longest an arm may last, > 0 and ≤ 12 (default 2; null is refused). `write_budget`: the most write calls one arm allows, 1–1000 (default 30) |
| guard | the pressure guard, below |

Zone rails are tied to the sensors each software zone used when they were
saved. If the zone-to-sensor mapping changes, for example when branch
`claude/swap-top-bottom` is deployed, that zone's heater rails count as unset,
and the card says so, until the rails are saved again. This keeps a rail
meant for one heater from applying to the other.

### Pressure guard

Rails bound what the AI may command; they say nothing about the xenon. The
`guard` section of the rails adds checks on the gas pressure (`guard.py`). It
denies by default: with its fields unset, the writes that depend on them are
refused.

| `guard` field | Meaning |
|---|---|
| `p_xe_cube`, `p_primary_bottle`: `warn_mbar`, `trip_mbar` | both or neither, 0 < warn < trip ≤ 20000 |
| `fresh_s` | how old (s, ≤ 300) a pressure reading may be for a write to rely on it |
| `blind_disarm_s` | no fresh reading from a gauge for this long (s, ≤ 3600) disarms heaters and pneumatic; null: never |
| `xenon_present_mbar` | `p_xe_cube` at or above this means xenon is in the system |
| `pump_open_max_mbar` | the pump valve opens only with both gauges at or below this (≤ 2000) |
| `pump_open_with` | `ballast` / `bottle`: partners the pump valve may be open with. Default empty: pump + ballast and pump + bottle are never open together |

Both pressures come from the GHS ESP32. A reading counts as **unknown** when it
is missing, not fresh in slow control, older than `fresh_s`, or above its
gauge's range. The range is identified from the Setra channel the state reads
(`zones.SETRA_CLIP_MBAR`): Setra #1 (`p_xe_cube`) clips at about 1766 mbar,
Setra #2 (`p_primary_bottle`) at about 7064 mbar.

- A reading at or above 0.97 × the clip counts as above every limit: the real
  pressure may be anything higher.
- A reading above 1.02 × the clip is not physical and counts as unknown.

Write preconditions:

- **Warming heater writes** — a setpoint or ramp going up, `set_heater_auto`,
  `set_pv_source` that lowers the PV (or sets the first source), switching a
  supply channel on, or raising its V or I while it is on — need the guard set
  up (`warn_mbar` / `trip_mbar` for both gauges, `fresh_s`,
  `xenon_present_mbar`), both gauges known and below `warn_mbar`, and, with
  xenon present, the ballast and bottle valves both open. Lowering is never
  held back by the guard.
- **Closing the ballast or bottle valve** (the xenon's walk-away buffer) is
  refused while xenon is present or the cube pressure is unknown.
- **Opening the pump valve** needs both gauges known and at or below
  `pump_open_max_mbar`.
- **Pump + ballast, pump + bottle:** never open together unless
  `pump_open_with` names the partner. This adds to `forbid_open_together`.

Supervision, every second:

- **Trip.** A gauge at or above its `trip_mbar` (or at its clip) trips the
  guard. The service stops every ramp, disarms heaters and pneumatic, and sets
  each zone whose setpoint is still the AI's last one to that zone's
  `setpoint_min_k`. Zones someone changed since are left alone. This is a
  safety action, like the LN2 auto-close: it runs armed or not, and is audited
  as kind `guard`. The trip is latched until the gauge reads below `warn_mbar`
  again.
- **Blind.** With `blind_disarm_s` set, a gauge with no fresh reading for that
  long disarms heaters and pneumatic. An arm made while still blind is dropped
  again on the next tick.

The guard sees the cube only while the hand valves between the cube and its
gauge are open. It runs in the same process as the AI's writes; the PLC has no
pressure input of its own.

### PV-source plausibility

A PID regulating on a dead sensor heats or cools open-loop. Clamp RTD5 has
read a constant 200.75 K since 2026-10-12. A sensor is implausible as a PV
source when:

- it reads outside 50–350 K;
- it is a clamp, and differs from the zone's cube RTD by more than
  `heaters.pv_cross_check_k` (the re-check uses 15 K when the rail is unset;
  `set_pv_source` refuses a clamp outright then);
- it has stayed flat (≤ 0.2 K) while the zone's other sensor moved 2 K or more
  within 30 minutes. The service samples every zone's sensors every 10 s; when
  it has watched for less than 25 minutes, `set_pv_source` tops this up with
  InfluxDB 1-minute means.

`set_pv_source` refuses such a source. Every 10 s the service re-checks the
source each zone is using. When one fails, it disarms the heaters, and if the
AI chose that clamp sensor it puts the zone back on its cube RTD (if the cube
passes). The failure is audited as kind `pv_check` and shown in
`get_overview` ("IMPLAUSIBLE PV SOURCE"). A source a person chose is flagged,
not changed. While a zone's source fails, writes that need a live PV in that
zone are refused.

### Text from elsewhere is data

Strings reach the tool results from places anyone on the lab network can
write: MQTT status payloads (Sequencer messages, tracker ids, NGE events,
interlock reasons, PID expressions, snapshot labels), the `by` of an arm or
rails message, and the audit (notes and reasons from earlier sessions, kept
across restarts). `untrusted.py` makes them safe to show:

- control characters, line separators and text-direction marks are dropped;
  a line break is shown as ` ⏎ `, so one string never looks like several lines;
- the length is capped, and the text is quoted in `«…»`;
- `by` is kept only if it matches `[A-Za-z0-9 ._-]{1,40}`; anything else is
  recorded as `unknown`;
- an AI note reads `note by AI client <name> (not from Lucas): «…»`.

The instructions (the MCP `instructions`, `get_guide`, the operator's
`CLAUDE.md`) say that only Lucas's chat messages instruct the AI, that `«…»`
text is data, and that a refusal is reported once — the AI does not propose
arming, longer arms, wider rails or enabling a gate unless Lucas asks what it
would take.

### Checks on every write

These checks run in this order:

0. **A write token** from a known client.
1. **Arguments.** They must have exact JSON types (no NaN, no booleans posing
   as numbers, no unknown keys), and include a `reason` of 200 characters or
   fewer. Text is plain: no control characters (a newline in a reason would
   forge a journal line; only a note may span lines), no unpaired surrogates,
   no text-direction marks.
2. **One write at a time.** A second concurrent write is refused, not queued.
3. **Group armed for this client.**
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
       - A setpoint increase needs a live PV source that passes the
         plausibility check.
       - A warming write needs the pressure guard's consent.
       - A non-numeric setpoint expression is only replaced when the call
         says `replace_expression: true`.
       - `max_step_k` and `max_rate_k_per_min` apply to the change measured
         from the current setpoint — or, while that is outside the rails (3 K
         after every slow-control restart), from the zone's measured PV when
         it has a live source. When even that point is outside the rails, the
         new value may land at most `max_step_k` inside the nearest rail edge
         (PV 295 K, rails 150–200 K, step 10 K → 190–200 K; no step rail: any
         value inside). The result's readback names the `anchor_k` used.
       - Switching a supply channel on, or raising its V or I while it is on,
         needs the PID zone it feeds in auto, at a numeric setpoint inside its
         rails and the interlock band, with a live PV source.
     - **LN2:**
       - Opening needs `auto_close` on (live) and a fresh level reading: the
         probe for the cryostat; the level plus the GHS ESP32 for
         ballast / primary_xe.
       - Closing turns `auto_open` off first. After an AI open that was never
         seen (not confirmed), a close is sent even though the valve reads
         closed: the open may still be queued in slow control.
       - `auto_open` can only be switched on while `auto_close` is on, and
         `auto_close` only switched off while `auto_open` is off.
     - **Pneumatic:**
       - The PLC's pneumatic gate on the Control page must also be enabled.
         This is a second, independent gate.
       - A `forbid_open_together` partner (or a pump pair the guard forbids)
         must not be open, nor have an AI open sent to it in the last 2 min
         that was never confirmed (queued commands land late).
       - The pressure guard: ballast and bottle are not closed with xenon
         present; the pump opens only below its limit.
6. **Rate limits:**
   - 20 write calls per minute overall; 6 notes per minute.
   - One write of the lease's budget per call that gets this far (a ramp's
     10 s steps do not count; starting it did).
   - Calls that send nothing (refused, invalid, a no-op such as "already
     closed", or a dry run) are audited one by one up to 30 a minute; beyond
     that they are counted and summed up in one audit entry. A no-op is never
     the card's "last action".
   - The pneumatic `min_interval_s` and 10 s per LN2 valve.
   - At most 6 heater-supply commands per minute, each one waiting for the
     supply's status.
7. **Publish.** The arm (for this client) is re-checked immediately before
   every single publish. A disarm in the middle of a multi-step call stops the
   remaining steps.
8. **Confirm** from the status topics, within 5 s, on two consecutive messages
   where possible. When a PV-source change is not confirmed, it is reverted.
9. **Audit.** The call is written to `aicontrol/audit.jsonl` (rotated at
   5 MB, keeping `audit.jsonl.1` … `.5`) and to the retained
   `xsphere/status/ai_control/audit` (last 50 entries; the card shows 10, the
   status page 20). Each entry names the `client`, and an action that sent
   something records `previous`: the values it changed, as they were before
   (setpoint and expression, PV expression, supply V / I / output, valve state
   and auto flags), so a person can put them back. Notes do not count as
   actions: the card's "last action" is always a real one.

**Dry runs.** Every write tool takes `dry_run: true`. It runs steps 0–6 —
including the arm, rails, guard and PV checks, and while disarmed it returns
the refusal it would get — and returns `would_publish` instead of publishing.
It changes nothing (no timer, no ramp, no budget or rate used) and is audited
as kind `dry_run`. Use it to review a plan before arming.

### Automatic disarm

The service disarms by itself; it never re-arms. Each disarm is audited and
shown on the card with its reason.

- **All groups:**
  - an arm expires;
  - this service reconnects to the broker;
  - slow control restarts (heartbeat `uptime_s` goes down, or `service_alive`
    goes false → true);
  - the snapshot is older than 30 s.
- **The group whose lease used up its write budget.**
- **Heaters:** the PV interlock trips, `xsphere/status/interlocks` turns
  not-ok, or a zone's PV source fails the plausibility re-check.
- **Heaters and pneumatic:** the pressure guard trips, or a gauge has had no
  fresh reading for `guard.blind_disarm_s`.
- **LN2:** the auto-close timer below fires.

### LN2 auto-close timer

When the AI opens a fill valve, the service records the opening time and a
deadline `max_open_minutes` later in `aicontrol/runtime.json`. If a live
valve status shows it still open (state or desired 1) at the deadline, the
service:

1. turns `auto_open` off, if it is on;
2. closes the valve;
3. audits "auto-closed after N min";
4. disarms ln2.

This is the service's own safety action, so it runs even while ln2 is
disarmed. It never acts on an unknown valve state (no live status, e.g. in the
first seconds after a start): it waits, and if that lasts a minute past the
deadline it says so in the audit. A "closed" status ends the timer only once
the AI's open has been seen, or at the deadline. The timer survives a service
restart, because it is reloaded at start; the deadline (like an arm's expiry)
runs on the monotonic clock, so a step of the system clock cannot stretch it.
It does **not** run while the service is stopped; the PLC ladder's own level
and timer logic still applies then.

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
- change arms, rails or the pressure guard;
- write at all without a write token, or in a group armed for another client;
- start the Sequencer or touch trackers;
- use the `gradient/*` commands;
- set PID manual mode, manual output, gains, autotune, or a free-form setpoint
  or PV expression;
- use NGE hold;
- publish any MQTT topic outside the fixed list in `bus.py`;
- run Flux of its own (history queries are built from fixed templates).

### The limitation

Arms, rails and the guard bind only clients that go through this service.
Everything else on the lab network can still reach the hardware:

- The MQTT broker accepts anonymous publishes from anyone.
- The web panel's `/api/cmd` relays any `xsphere/commands/…` topic without
  authentication. For `ai_control/*` it only insists on JSON and an allowed
  Origin.
- Node-RED, code-server (a shell as `xbox`) and InfluxDB are all open on the LAN.

What such a client can no longer do is arm the AI or save rails (both need
Lucas's one-time code), or make an AI write (that needs a write token and an
arm for that client). It can still send hardware commands directly, outside
this service. A process that can SSH to the Pi as `xbox` can also read the
secret and the token hashes from `/proc/<pid>/environ` of the service, or
change its code: moving the service to its own system user is a separate
step, not done here.

A Claude session with a shell, a browser, web access or a way to message
another session can therefore bypass all of this. **Only use the operator
profile (`aicontrol/operator/`) for an AI that should be confined,** and run
its acceptance check.

## Tools

The same tools are available over MCP and REST. Zone names are the
*software* names. `get_overview` prints which physical heater, sensors and
supply channel each zone is, worked out from `slowcontrol/state.yaml`.

**Reads (no arm and no token needed):**

| Tool | What it does |
|---|---|
| `get_overview` | One-page plain-text status: arms (client, end, writes used) and what the AI may do now, the interlock, the pressure guard, each zone (with any implausible PV source), supply, LN2, valves, pressures, turbos, warnings, the last AI actions and notes (`format: "json"` for JSON) |
| `list_states` | Compact rows of registry states (id, label, value, freshness, which tool writes it); filter by group or text |
| `get_states` | Full snapshot views for up to 40 state ids, with the raw driver status for PID / valve / NGE / pneumatic ones |
| `get_ai_control` | Which client the caller is (`you_are`), the arms, the full rails, rails errors or stale zones, the guard and PV checks, active ramps, LN2 timers, counters, the last 20 audit entries |
| `query_history` | InfluxDB history for up to 8 ids: relative or RFC3339 start, aggregation (`mean`/`min`/`max`/`last`/`count`/`changes`), at most 1000 points per series and 2000 per call |
| `get_guide` | Markdown guide: heaters, ln2, pneumatic, history, safety |

Text the reads quote from MQTT or the audit is shown as `«…»` (see "Text from
elsewhere is data").

**Writes** (each needs a write token, and takes `dry_run: true`):

| Tool | Group | What it does |
|---|---|---|
| `set_heater_setpoints` | heaters | Set one or more zone setpoints in K, all validated before any is published |
| `set_heater_gradient` | heaters | Set top = base, bottom = base + dv, nozzle = base + dl (software zone names), through the same path |
| `start_heater_ramp` | heaters | Move zones to targets at a rate in K/min. The service steps the setpoint every 10 s and re-checks everything at each step |
| `stop_heater_ramp` | none | Stop ramps (any client with a token, audited) |
| `set_heater_auto` | heaters | Put a manual zone back into auto, if its setpoint is numeric and inside the rails |
| `set_pv_source` | heaters | Make the zone's cube or clamp RTD its PID process value, as the rails allow, if the sensor is plausible |
| `set_heater_supply` | heaters | NGE channel voltage → current → output, each step confirmed |
| `set_ln2_valve` | ln2 | Open (with the auto-close timer) or close a fill valve |
| `set_ln2_auto` | ln2 | Set a vessel's `auto_open` / `auto_close` flags, in the safe order |
| `set_pneumatic_valve` | pneumatic | Open or close ballast, pump or bottle |
| `add_note` | none | A logbook entry in the audit, shown on the card (and to later sessions) as a note by this AI client |

Every write returns `{"ok", "tool", "refused"?, "error"?, "published": [...],
"confirmed", "readback", "previous"?, "warnings", "note"?}`, and MCP marks it
`isError` whenever `ok` is false. A dry run returns `{"ok", "tool", "dry_run":
true, "would_publish": [...], "readback", "previous"?, "warnings", "note"}`
instead.

- `refused`: nothing was sent. The sentence states what is missing (a token,
  an arm, a rail, a precondition), as a fact — not as a request to Lucas.
- `error`: something was sent but did not take effect: a PV source that never
  evaluated (it is put back), a supply command the NGE rejected, a gas valve
  the PLC's gate refused, or an earlier step of a multi-step call that was not
  confirmed (the later steps are not sent). `published` lists what went out.
- `error` with `"invalid_arguments": true`: the arguments failed validation;
  nothing was sent.
- `confirmed: false` with `ok: true`: the command went out but the status did
  not show it within 5 s; `warnings` says so.
- `previous`: what the call changed, as it was before. Text values read back
  from status topics (`readback`, `previous`) are quoted `«…»`.

Reads return their data with `ok: true`, or `ok: false` and an `error`
(`query_history` without an InfluxDB token, a bad id, …).

## REST reference

The base URL is `http://xbox-pi:8089`. Responses are JSON, apart from `/`.

| Method and path | Same as |
|---|---|
| `GET /healthz` | liveness; also whether one-time codes are configured and how many client tokens there are |
| `GET /` | read-only status page: arms, rails, ramps, timers, audit, how to connect |
| `POST /mcp` | the MCP endpoint (Streamable HTTP, JSON responses, no sessions; protocol 2026-07-28, and 2025-11-25 / 2025-06-18 for older clients) |
| `GET /api/v1/overview` | `get_overview` (`?format=json`) |
| `GET /api/v1/states` | `list_states` (optional `group`, `query`, `limit`) |
| `GET /api/v1/states/<id>` | `get_states` for one id |
| `GET /api/v1/history` | `query_history` (`?ids=a,b&start=-6h`, optional `stop`, `every`, `fn`, `max_points`) |
| `GET /api/v1/ai_control` | `get_ai_control` |
| `GET /api/v1/guide` | `get_guide` (`?topic=`) |
| `POST /api/v1/actions/<tool>` | any tool, with its arguments as the JSON body |

`POST /mcp` and `POST /api/v1/actions/<tool>` read the caller's client from
`Authorization: Bearer <token>`. It is never required at the HTTP level (no
401): reads work without it, and a write without a known token is refused in
its result (409 on REST).

An empty query parameter (`stop=`) counts as not given. In a query string,
write times with `Z` or a negative offset, or percent-encode `+` as `%2B`
(a bare `+` decodes to a space).

| Status | Meaning |
|---|---|
| 200 | the tool ran |
| 400 | invalid arguments (also an unknown or repeated query parameter), or a read that failed |
| 403 | the request carried an `Origin` header; browsers are refused on `/mcp` and `/api/v1` |
| 404 | unknown tool or endpoint |
| 405 | `/mcp` with any method but POST (`Allow: POST`) |
| 409 | a write refused (no write token, not armed for this client, outside the rails, a precondition failed) or one that failed after it was sent |
| 413 | a body over 1 MiB |
| 415 | a POST without `Content-Type: application/json` |
| 421 | the `Host` header is not in `XSPHERE_AICONTROL_ALLOWED_HOSTS` |
| 500 | an internal error (logged with its traceback) |

A REST write is held to exactly the same checks as an MCP write; REST is not a
side door.

```bash
# Reads: no token.
curl -s http://xbox-pi:8089/api/v1/overview
curl -s -X POST http://xbox-pi:8089/api/v1/actions/get_states \
  -H 'Content-Type: application/json' -d '{"ids": ["pid_top_setpoint"]}'
# A write: the client's token, here as a dry run (checks only, nothing sent).
curl -s -X POST http://xbox-pi:8089/api/v1/actions/set_heater_setpoints \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $XSPHERE_AI_TOKEN" \
  -d '{"setpoints": {"top": 170}, "reason": "check the plan", "dry_run": true}'
```

**Human-only MQTT** (sent by the Control page card through `/api/cmd`):

| Topic | Payload |
|---|---|
| `xsphere/commands/ai_control/arm` | arm: `{"group": "heaters"\|"ln2"\|"pneumatic", "armed": true, "code": "123456", "client": "operator", "hours"?: num, "budget"?: int, "by"?: "…", "request_id"?: "…"}`. `hours` ≤ the rails' `arm_hours` (default: that), `budget` ≤ the rails' `write_budget` (default: that). Disarm: `{"group": "heaters"\|"ln2"\|"pneumatic"\|"all", "armed": false, "by"?: "…"}`, no code. The outcome is in the status as `last_arm_result` (with `request_id`) |
| `xsphere/commands/ai_control/rails` | `{"rails": {full document}, "base_revision": N, "code": "123456", "by"?: "…", "request_id"?: "…"}`; `request_id` (≤ 64 characters) comes back in `last_rails_result`, so the editor that saved can tell its own answer |

`by` is a short plain name (`[A-Za-z0-9 ._-]{1,40}`); anything else is recorded
as `unknown`. It is a label, not proof of who sent the message: the code is.

The web panel relays these only while it is connected to the broker (else
HTTP 503, and the card says "not sent"), and sends arm and rails at qos 0: an
arm clicked during a broker outage is never delivered minutes later.

Status goes out retained on `xsphere/status/ai_control`, every 2 s and on
change (and after every arm command, so the card sees a refused code at
once); the retained last will says offline and disarmed. Besides the arms and
rails it carries `clients` (names only), `totp_configured`, `codes_locked_s`,
`last_arm_result`, `guard` (readings, levels, xenon present, whether warming is
refused) and `pv_checks`. No secret is ever in it. The audit goes out
retained on `xsphere/status/ai_control/audit`. Telegraf stores the status as
measurement `ai_control` (`online`, `armed_*`, `actions`, `refused`,
`auto_disarms`).

## Connecting Claude Code

- **A confined operator:** follow `aicontrol/operator/README.md`. Copy the
  profile out of the repo, set `XSPHERE_AI_TOKEN` for the user that runs it,
  open Claude Code there, trust the folder once, and run the acceptance check.
  Its `.mcp.json` sends `Authorization: Bearer ${XSPHERE_AI_TOKEN}`; the token
  itself is never in the file.
- **Any other session**, for example while developing, can read:
  `claude mcp add --transport http xsphere http://xbox-pi:8089/mcp`. Without a
  token it is read-only: every write tool refuses it. Do not give a
  development session a write token; it keeps all its other tools, so it is
  **not** confined.

The service is LAN-only, like the rest of the Pi. Reads need no token.

## Deploy (on xbox-pi)

Once the code is on the Pi's checkout (`~/xsphere-slow-control`):

1. **Check that it imports** with the services' interpreter:
   ```bash
   ~/miniconda3/envs/slowcontrol/bin/python -c "import aicontrol.app"
   ```
2. **Enroll the secrets** (next section, "Enrolling"): Lucas does this himself.
   Without them the service runs read-only: nothing can be armed and every
   write is refused.
3. **Optional but recommended:** give it a read-only InfluxDB token ("InfluxDB
   token" below).
4. **Install and start the unit** (or, if it is already installed, copy the
   new unit file, `daemon-reload` and `restart`: it carries
   `EnvironmentFile=-/etc/xsphere/aicontrol.env`):
   ```bash
   sudo cp aicontrol/xsphere-aicontrol.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now xsphere-aicontrol
   ```
5. **Check it:**
   ```bash
   journalctl -u xsphere-aicontrol -n 30      # instance=production, topics, codes, client names
   curl -s http://localhost:8089/healthz      # "one_time_codes": "configured", "clients": N
   docker exec mosquitto mosquitto_sub -h localhost -t xsphere/status/ai_control -C 1 -v
   ```
   Slow control does **not** need a restart, and no heater or valve is
   touched. The service starts with everything disarmed. A rails file from
   before this version (schema 2) counts as invalid, so it is read-only until
   Lucas saves the rails again (with a code) and arms a group.
6. **The Control page card** (code boxes, client, hours, budget) goes live
   only after a web panel restart, because webcontrol caches its templates.
   That restart drops `/diag` and any follow or ramp loop the panel is
   running, so first check that `curl -s localhost:8088/diag/status` shows
   `"active": false`. Then run `sudo systemctl restart xsphere-webcontrol`.
7. **The Telegraf input** is stored only after
   `bash telegraf/deploy-iotstack.sh` (preview) and then `--apply`. That should
   report 15/15 MQTT inputs connected; see OPERATIONS.md, "Changing what
   Telegraf records".
8. **The first deploy only:** run the history and MCP checks in OPERATIONS.md,
   "Checks after deploying it". The Flux templates and a real Claude Code
   `tools/call` were only tested offline before.

To take it out: `sudo systemctl disable --now xsphere-aicontrol`. It publishes
"offline, disarmed" as it stops. Nothing else depends on it.

### Enrolling

Two kinds of secret, both made by Lucas **in his own terminal** (an SSH
session to the Pi, a PowerShell window on the DAQ PC) — never through a Claude
session, whose transcript would keep whatever the commands print.
`python -m aicontrol.enroll` (`enroll.py`, standard library only) prints each
secret once and writes no file.

| Variable in `/etc/xsphere/aicontrol.env` | What it is |
|---|---|
| `XSPHERE_AICONTROL_TOTP_SECRET` | the one-time-code secret (base32, 160 bits). Lucas's authenticator app holds the same secret |
| `XSPHERE_AICONTROL_CLIENTS` | `name:sha256hex,…`: the hash of each AI client's write token. The token itself stays on the machine that runs that client |
| `XSPHERE_AICONTROL_INFLUX_TOKEN` | optional, the read-only InfluxDB token (below) |

On the Pi, in `~/xsphere-slow-control`:

1. Make the directory and the file, root-only:
   ```bash
   sudo install -d -m 700 /etc/xsphere
   sudo install -m 600 /dev/null /etc/xsphere/aicontrol.env
   ```
2. Make the one-time-code secret (`sudo apt install qrencode` first to see a
   QR code):
   ```bash
   python3 -m aicontrol.enroll totp
   ```
   Put the printed `XSPHERE_AICONTROL_TOTP_SECRET=…` line into the file
   (`sudo nano /etc/xsphere/aicontrol.env`), and scan the QR code (or type the
   secret) into the authenticator app: time-based, 6 digits, 30 s. Then clear
   the terminal (`clear; history -c`).

On the DAQ PC, in the repo, for each AI client (normally just `operator`):

3. Make its write token:
   ```powershell
   C:\Users\xbox\anaconda3\python.exe -m aicontrol.enroll token operator
   ```
   As the Windows user that runs the operator profile, set the token (then
   open a new terminal; `setx` only reaches new processes):
   ```powershell
   setx XSPHERE_AI_TOKEN "<the printed token>"
   ```
   Add the printed `operator:<hash>` entry to `XSPHERE_AICONTROL_CLIENTS=` in
   the Pi's env file (comma-separated if there are several clients).

Back on the Pi:

4. Check ownership and mode, install the current unit, restart:
   ```bash
   sudo chown root:root /etc/xsphere/aicontrol.env
   sudo chmod 600 /etc/xsphere/aicontrol.env
   sudo cp aicontrol/xsphere-aicontrol.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl restart xsphere-aicontrol   # disarms everything (safe)
   ```
   Restart the web panel too, for the new card (deploy step 6: check
   `curl -s localhost:8088/diag/status` shows `"active": false` first):
   `sudo systemctl restart xsphere-webcontrol`.
5. Test a code from the phone (it only checks; nothing is used up):
   ```bash
   sudo python3 -m aicontrol.enroll check
   ```
   It also reports the file's mode and the client names it found. The journal
   (`journalctl -u xsphere-aicontrol -n 30`) says "One-time codes for arming
   and rails: configured" and lists the client names, never the values.
6. On the Control page card, open the rails, set them (the schema is now
   version 3: `general.write_budget`, `heaters.pv_cross_check_k` and the
   `guard` section are new; see OPERATIONS.md, "Rails"), enter a code and
   save.

systemd reads the env file as root before it starts the service as `xbox`, so
mode 600 is enough. The log never shows a secret.

**Revoking and recovery.**

- A client's token: delete its entry from `XSPHERE_AICONTROL_CLIENTS` and
  restart the service. Its writes are refused from then on.
- A lost or exposed phone secret: run `enroll totp` again, replace the line,
  restart, and enroll the new secret in the app. Without it nothing can be
  armed; disarming always works.
- A process that can SSH to the Pi as `xbox` can still read these values from
  the service's `/proc/<pid>/environ`. Running the service as its own system
  user closes that; it is a separate step, not done here.

### InfluxDB token

By default the service uses `INFLUX_TOKEN` from the repo's `.env`, which is
all-access. The service never sends Flux the AI wrote, only fixed templates
over a whitelist of ids, but a read-only token limits what a bug could do.

1. In the InfluxDB UI (`http://xbox-pi:8087`), go to **Load Data → API Tokens →
   Generate API Token → Custom API Token**. Choose **Read** on bucket
   `xsphere` only, and describe it as "xsphere-aicontrol (read)".
2. Add it to the same root-only env file the unit already reads
   (`EnvironmentFile=-/etc/xsphere/aicontrol.env`; see "Enrolling" for making
   the file):
   ```bash
   sudo nano /etc/xsphere/aicontrol.env       # add: XSPHERE_AICONTROL_INFLUX_TOKEN=<token>
   sudo systemctl restart xsphere-aicontrol   # disarms everything (safe)
   journalctl -u xsphere-aicontrol -n 30 | grep -i token
   ```
   The log names the token's source, never its value. An older drop-in
   `xsphere-aicontrol.service.d/influx-token.conf` pointing at the same file
   is harmless and can be removed.

## Testing

- **On the DAQ PC**, run
  `C:\Users\xbox\anaconda3\python.exe -m pytest tests/test_aicontrol_*.py -q`.
  The tests use an in-process MQTT loopback and a fake slow-control responder,
  with no broker and no network. `tests/test_aicontrol_hardening.py` covers
  the one-time codes (RFC 6238 vectors, replay, lockout), write tokens and
  leases, quoting of untrusted text, the operator profile, the pressure guard,
  the PV-source checks, `dry_run` and `previous`. Every secret in the tests is
  a test value.
- **`python -m aicontrol`** without the unit's environment starts a *test*
  instance:
  - its client id and topics live under `xsphere/test/…`;
  - it refuses any broker other than localhost;
  - its state (rails, audit, LN2 timers) is in `aicontrol/test-state/`, and
    it refuses production's `aicontrol/` state directory;
  - it falls back to Flask's server when waitress is missing.

  Run it on the DAQ PC, not on the Pi: there localhost **is** the live broker,
  so a test instance would see real data, and anything it is armed and railed
  for (on its own `xsphere/test/…` topics) would publish real commands.

  Never set `XSPHERE_AICONTROL_INSTANCE=production` off the Pi. The broker
  would keep kicking the real service off its client id, and the copy would
  obey the real arm buttons.
- **The operator profile:** the acceptance check in
  `aicontrol/operator/README.md`.

## Files

| Path | Contents |
|---|---|
| `config.py` | settings from the environment (including the secrets); production vs test instance |
| `auth.py` | one-time codes (TOTP) and client write tokens |
| `enroll.py` | `python -m aicontrol.enroll`: makes the secrets, for Lucas to run himself |
| `untrusted.py` | cleaning and quoting text from MQTT and the audit |
| `bus.py` | MQTT client, caches, command-topic allowlist, waits |
| `zones.py` | zone, sensor, channel, valve and pressure-gauge maps detected from `state.yaml` |
| `rails.py` | rails schema (version 3), validation, `RailsStore` |
| `arms.py` | arm leases, the auto-disarm supervisor, LN2 timers, heater ramps |
| `guard.py` | the pressure guard and the PV-source plausibility check |
| `actions.py`, `reads.py` | the write and read tools |
| `history.py` | registry → Telegraf map and the Flux templates |
| `mcp.py`, `tools.py`, `app.py` | MCP protocol, tool registry, Flask app |
| `templates/index.html` | the read-only status page |
| `xsphere-aicontrol.service` | systemd unit |
| `operator/` | the Claude Code operator profile template |
| `rails.json`, `audit.jsonl`, `runtime.json` | runtime state on the Pi (gitignored) |
| `test-state/` | a test instance's own runtime state (gitignored) |
