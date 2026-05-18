# State Layer ("Proprioception") — Plan & Spec

**Status:** all 5 build steps **done** (registry, store, wired, register GUI,
control GUI), running live on `xbox-pi`. **Read this first** if you're picking
up work on `slowcontrol/state/` or the GUIs.

This is the design contract for a lean, centralized state layer for the xsphere
slow control: one authoritative description of *every state the system has*, one
object that holds their current values + freshness + moving averages, and a GUI
that reads (and eventually writes) those states. It is deliberately *not* the
full "Layer 3 proprioception store" from the microsphere-automation repo — see
[§ Out of scope](#out-of-scope-for-now) and [§ Growth path](#growth-path-toward-automationai).

---

## 1. Why

Today the broker + retained MQTT messages is the de-facto state store, and
[`webcontrol/app.py`](../webcontrol/app.py) caches every `xsphere/status/#` and
`xsphere/sensors/#` message with a timestamp — a poor-man's snapshot. What's
missing, and what this layer adds:

1. **A declarative registry** — `state.yaml` — the single source of truth for
   what states exist, each annotated with kind (analog / binary / enum / text),
   unit, the topic+JSON-key it's read from (or a derived expression), an
   expected update period, moving-average windows, and — if controllable — the
   command topic and payload shape. Today that knowledge is scattered across
   `drivers/plc.py` register maps, the `labjack:` block in `config.yaml`, the
   `SYSTEM_ARCHITECTURE.md` tables, and hardcoded strings in `index.html`.
2. **Centrally-computed freshness** (FRESH / STALE / INVALID from age vs.
   declared period) so consumers don't each re-derive it.
3. **Derived channels in one place** — for every analog state, an instantaneous
   value *and* one or more moving averages; plus computed quantities (measured
   ΔT vertical/longitudinal, PID tracking error, …).
4. **One object the service's controllers and the GUI both read** —
   `store.get("t_cube_top") → {value, avg_60s, freshness, ts, source}`.

---

## 2. Architecture

The `StateStore` lives **inside the slow-control service** (`slowcontrol/state/`),
constructed before the controllers so they can hold a reference. It subscribes
to the broker like everything else; it does not poll hardware.

```
PLC / LabJack / ESP32s ──MQTT──▶ Mosquitto ──┬──▶ Telegraf ──▶ InfluxDB ──▶ Grafana
                                              │
              ┌───────────────────────────────┴───────────────────────────┐
              │                 slow-control service                       │
              │  drivers (PLC, …) ──▶ xsphere/sensors/#  xsphere/status/#  │
              │  controllers (gradient, autovalve, interlocks, scanner)     │
              │                          │                                  │
              │                    ┌─────▼──────┐  reads state.yaml          │
              │                    │ StateStore │  subscribes to all sources │
              │                    └─────┬──────┘  computes freshness + avgs │
              │                          │         evaluates `derived:` exprs │
              └──────────────────────────┼─────────────────────────────────-─┘
                                         │ publishes (retained)
                                  xsphere/state/snapshot
                                  xsphere/state/derived/<id>   (per analog avg, opt.)
                                         │
                              ┌──────────▼───────────┐
                              │ webcontrol (Flask)   │  /api/state  ← snapshot
                              │   /         index    │  /api/cmd    → commands
                              │   /control  control  │  /api/follow /api/seq
                              └──────────────────────┘
                       Register GUI ( / )   = the centralized read-out of every
                                              state, grouped, freshness-tinted,
                                              links to Grafana + control GUI.
                       Control GUI (/control) = valves (actual + desired + auto),
                                              PID (PV/SET/OUT/err + setpoint +
                                              gains), gradient (mode/base/Δv/Δl +
                                              measured ΔTs + resulting setpoints),
                                              automation (scan/follow/ramp seq).
```

`config.yaml` does not need a new block: `state.yaml` is found next to it
(`slowcontrol/state.yaml`); the service passes the path through. If absent the
state layer is skipped with a warning (the rest of the service runs unchanged).

---

## 3. `state.yaml` — schema reference

Top level:

```yaml
defaults:               # per-kind defaults, layered under each entry's fields
  analog: {period_s: 1.0, stale_factor: 1.5, invalid_factor: 5.0, avg_windows_s: [60]}
  binary: {period_s: 5.0}
  enum:   {period_s: 5.0}
  text:   {period_s: 5.0}

groups:                 # group name → { state id → entry }
  temperature:
    t_cube_top: {kind: analog, unit: K, label: "...", source: {...}}
    ...
  valve: {...}
  ...

derived:                # state id → entry (group is forced to "derived")
  dt_vertical_meas: {kind: analog, unit: K, expr: "t_cube_bottom - t_cube_top", avg_windows_s: [60]}
  ...
```

Per-entry fields (loader: [`slowcontrol/state/schema.py`](state/schema.py)):

| field | meaning |
|---|---|
| `kind` | `analog` \| `binary` \| `enum` \| `text` |
| `label` | display name (defaults to the id) |
| `unit` | display unit, free text; analog only |
| `source` | one of: `{topic, key}` · `{topic, keys: [k1,k2,…]}` (first present wins) · `{topic}` (payload *is* the scalar) · `{topic, presence: true}` (binary; `True` iff a message arrived on `topic` within `period_s` — for heartbeats). Derived states omit `source` and give `expr:` instead. |
| `expr` | derived states only: restricted arithmetic over other state ids — `+ - * /`, parentheses, unary minus, numeric literals, bare ids. A derived state inherits the worst freshness of its inputs. |
| `period_s` | expected update interval (s); `null` ⇒ never self-stales (retained status that only changes on an event, e.g. gradient mode) |
| `stale_factor` / `invalid_factor` | freshness thresholds (× `period_s`); defaults 1.5 / 5.0 |
| `avg_windows_s` | list of moving-average windows (s); analog only |
| `values` | allowed values; enum only |
| `control` | `{topic: <command topic>, payload: {<key>: <template>, …}}` — present ⇒ actuable. Templates: `"$value"` → operator input as-is (bool/number/str), `"$value01"` → `int(bool(input))`. |

Validate / dump the registry at any time:

```
python -m slowcontrol.state.schema           # uses slowcontrol/state.yaml
python -m slowcontrol.state.schema path.yaml
```

### Freshness model

For a state with `period_s = P`, last updated `age` seconds ago:
`age < stale_factor·P` → **FRESH**; `< invalid_factor·P` → **STALE**; else
**INVALID**. Never updated → INVALID. `period_s = null` → always FRESH once
seen, INVALID until then. `presence: true` → the *value* is `freshness == FRESH`.

---

## 4. The `StateStore` contract (build step 2)

`slowcontrol/state/store.py`, constructed `StateStore(config, mqtt, schema)`:

- `start()` — subscribe to every `schema.subscribe_topics()`. On each message:
  resolve the StateDef(s) that read from that topic, extract the value
  (`keys` fallback, or the whole payload, or — for `presence` — just stamp the
  receipt time), update an `Entry(value, ts_wall, ts_monotonic, source, seq)`,
  and for analog states append `(t, value)` to that channel's ring buffer.
- A ~1 Hz tick: recompute freshness for everything; evaluate `derived` exprs
  (worst-input freshness; INVALID if any input is); compute each analog state's
  moving averages over its windows; publish `xsphere/state/snapshot` (retained,
  one JSON object: `{generated_at, states: {id: {value, freshness, ts, source,
  unit, kind, group, label, avg: {window_s: mean}, control?}}}`) and, optionally,
  `xsphere/state/derived/<id>` per averaged channel so Grafana can trend it.
- Read API: `get(id) -> Entry | None`, `snapshot() -> dict`, `schema` property.
  (No `subscribe()` callback API yet — see Out of scope.)
- `stop()` — stop the tick thread; nothing to flush.

Wiring (build step 3): construct in [`core/service.py`](core/service.py) before
the controllers, `start()`/`stop()` in the lifecycle. Controllers keep working
unchanged for now; refactoring `interlocks` and the scanner's stability check to
read freshness/averages from the store is a *later, separate* step.

---

## 5. Build order

All five steps **done** in session 1 (2026-05-13); see SESSION_LOG.md.

1. **Registry + schema loader** ✅ — `state.yaml`, `slowcontrol/state/schema.py`,
   `slowcontrol/state/__init__.py`.
2. **`StateStore`** ✅ — `slowcontrol/state/store.py`: subscribe, freshness,
   moving averages, derived `expr` evaluator, `get`/`snapshot`, publish
   `xsphere/state/snapshot`.
3. **Wire into the service** ✅ — constructed in `core/service.py` between
   drivers and controllers; schema path threaded through `app.py`. Disabled
   gracefully if `state.yaml` is missing/malformed.
4. **Register GUI** ✅ — `webcontrol/app.py` subscribes to
   `xsphere/state/snapshot`; `/api/state` returns it; `/` (`index.html`) shows
   one card per group, every state, freshness-tinted, links to Grafana +
   `/control`.
5. **Control GUI** ✅ — `/control` (`control.html`): valves with
   actual + desired + auto-open / auto-close toggles; PID with PV/SET/OUT/err
   + setpoint + Kp/Ki/Kd form (current values shown next to every input);
   gradient mode/base/Δv/Δl + measured ΔTs + resulting per-zone setpoints;
   automation cards (scan, follow, ramp sequencer) kept bespoke. Header pills
   read from the snapshot's `service_alive`/`interlocks_ok`/`labjack_connected`.

---

## 6. Out of scope for now

Deliberately *not* built (they earn their keep when there's automation driving
sequences — see growth path):

- A `subscribe(id_or_pattern, callback)` API on the store (consumers poll
  `snapshot()` / read the retained MQTT topic instead).
- SQLite persistence of the snapshot (Telegraf → InfluxDB already keeps history;
  the store would only persist *its* view, which is reconstructable).
- `wait_until(state, predicate, timeout, *, after=…)` and its race semantics.
- A formal `HealthReport` dataclass / endpoint (the snapshot already carries
  per-state freshness; the GUI can count).
- Per-state retained topics (`xsphere/state/<id>`); one snapshot blob is simpler
  for the GUI to consume.
- Refactoring `interlocks` / `gradient_scanner` onto the store.

---

## 7. Open questions / decisions baked into step 1

- **Granularity:** each valve is split into `_state` / `_auto_open` /
  `_auto_close` separate states (one toggle each in the panel) rather than one
  composite "valve" with sub-fields; PID likewise (`_pv` / `_output` /
  `_setpoint`). Revisit if the panel wants composites.
- **PID gains** (`kp/ki/kd`) and the valve `desired` field are intentionally
  *not* in the registry — gains are parameters not dynamic states, and the
  bespoke PID-gains form handles them. Add them if a real need appears.
- **Heater PWM coil state** (`REG_HTR_COIL` in `plc.py`) isn't published to MQTT
  today — only `output_pct` is. `output_pct` covers it for now; add a coil
  publish in `plc.py` if a hard on/off binary is wanted.
- **`pid_*_setpoint` control** writes the PLC register directly but only
  "sticks" when `gradient_mode == absolute`; in `gradient` mode use the
  `gradient_*` controls (the gradient controller owns the per-zone setpoints).
- **GHS pressure/vacuum/environment** entries match the firmware's documented
  topics (`firmware/gas-handling-system/.../main.cpp`). If a board isn't live,
  those states simply read INVALID — which is the correct report.
- **`xsphere/state/snapshot` as one blob** vs. per-state topics: blob first.

---

## 8. Growth path toward automation/AI

When the system grows a sequencing/automation layer (the "Layer 4 skills" in the
microsphere repo's terms), the natural additions, in order: a `subscribe()`
callback API on the store → `wait_until(...)` for "do X, wait for condition" →
SQLite persistence for post-hoc analysis of the store's view → a `HealthReport`
surface. None of these change the `state.yaml` contract; they extend the store.
Keeping the registry declarative now is the investment that makes all of that
cheap later.
