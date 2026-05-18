# Session log — xsphere slow control (state layer work)

A running record of work on the centralized state layer ("proprioception"),
modelled on the session-log workflow from the microsphere-automation repo. Each
session appends an entry. The log is the artifact that lets the next session
start with context. The design contract is [STATE_LAYER_PLAN.md](STATE_LAYER_PLAN.md);
read that first.

Entry format:

```
## Session N — YYYY-MM-DD — Step X

**Targeted step**: one line from STATE_LAYER_PLAN.md § Build order.

**Implemented**:
- File path — what was added.

**Open questions**:
- Anything raised that needs a decision before the next session.

**Notes**:
- Anything else worth recording.
```

Sessions append to the bottom. Don't edit prior entries except to mark open
questions resolved (with a date).

---

## Session 1 — 2026-05-13 — Step 1 (registry + schema loader)

**Targeted step**: build step 1 — the declarative state registry and its
loader; nothing that touches MQTT or hardware yet.

**Implemented**:
- [state.yaml](state.yaml) — the registry: 51 states across 9 groups
  (`temperature`, `level`, `pressure`, `vacuum`, `environment`, `valve`, `pid`,
  `gradient`, `service`) plus a `derived` block. 16 are `control:`-annotated
  (valves + auto-enables, PID setpoints, gradient mode/base/Δv/Δl); 3 are
  derived (`dt_vertical_meas`, `dt_longitudinal_meas`, `pid_top_error`).
  Sources cross-checked against `drivers/plc.py`, the `labjack:` config block /
  `LJ-python-controller/controller.py`, and the GHS firmware
  (`firmware/gas-handling-system/.../main.cpp`).
- [state/schema.py](state/schema.py) — typed model (`Freshness` enum,
  `SourceSpec`, `ControlSpec`, `StateDef`, `StateSchema`) + `load_state_schema()`
  with full validation (kinds, unique ids, enum values, derived-expr references,
  layered per-kind/`defaults:`/entry overrides, `presence` vs `key`/`keys`
  exclusivity, `avg_windows_s` analog-only) + `default_schema_path()` +
  a `python -m slowcontrol.state.schema` CLI that validates and pretty-dumps
  the registry.
- [state/__init__.py](state/__init__.py) — re-exports the schema API.
- [STATE_LAYER_PLAN.md](STATE_LAYER_PLAN.md) — the design contract (why, where
  it lives, `state.yaml` field reference, freshness model, the `StateStore`
  contract for step 2, build order, what's deliberately out of scope, growth
  path toward automation/AI).
- [SESSION_LOG.md](SESSION_LOG.md) — repurposed from the microsphere-automation
  repo's log into this repo's; this is the first entry.

**Validation**: `python -m slowcontrol.state.schema` →
`OK … 51 states (16 controllable, 3 derived), 33 source topics`. No code paths
exercised yet beyond the loader (no store, no MQTT).

**Open questions** (also in STATE_LAYER_PLAN.md § 7):
- Granularity: valves/PID split into separate scalar states vs. composite
  entries with sub-fields. Currently split.
- PID `kp/ki/kd` and valve `desired` left out of the registry on purpose.
- Heater PWM coil state isn't on MQTT yet (`output_pct` stands in).
- `pid_*_setpoint` control only "sticks" in `gradient_mode == absolute`.

**Notes**:
- Running `python -m slowcontrol.state.schema` emits a benign `RuntimeWarning`
  from `runpy` because `state/__init__.py` imports the submodule before `-m`
  re-execs it; harmless.

---

## Session 1 (continued) — 2026-05-13 — Steps 2–5 (store + service wiring + register GUI + control GUI)

**Targeted step**: continue through STATE_LAYER_PLAN.md build steps 2–5 in one
sitting, with intermediate checks. After step 1 the user clarified the GUI
split: a separate **register GUI** (centralized read-out, cross-linked to
Grafana + the control GUI) and a separate **control GUI** ("temperature & valve
control") with state shown next to each control.

**Implemented**:
- [state/store.py](state/store.py) — `StateStore`. Frozen `Entry`; per-state
  ring buffers (deque of `(t_mono, value)`); per-source-topic subscription
  fan-out (one topic ⇒ N states), with type-coerced extraction via the schema's
  `keys` fallback list; `presence:true` topics tracked separately and resolved
  at tick time. A safe-AST evaluator (`+ - * /`, parentheses, unary `±`, numbers,
  bare ids) for derived `expr`s; derived states are dependency-sorted (cycles
  warned + left in declaration order). Tick (1 Hz default): recompute freshness
  (`FRESH` if `age ≤ stale_factor · period`, `STALE` until `invalid_factor ·
  period`, else `INVALID`; `period=null` ⇒ FRESH once seen, INVALID until then),
  evaluate derived states (worst-input freshness), compute moving averages,
  publish `xsphere/state/snapshot` retained. `get()` and `snapshot()` are
  read-only (don't feed derived values back into buffers — only the tick does).
- [core/service.py](core/service.py) — load the schema next to the config file
  and construct the `StateStore` between drivers and controllers. Schema-load
  failure logs a warning and the rest of the service still runs.
  [app.py](app.py) passes the config path through.
- [state.yaml](state.yaml) — grew to **65 states** (was 51): added the
  `valve_*_desired` reads (3) so the control GUI can show "signal sent" next to
  "actual", `pid_*_kp/ki/kd` (9) so the gains form shows current values next to
  each input, and `pid_bottom_error` / `pid_nozzle_error` derived states so all
  three zones have a tracking-error read.
- [../webcontrol/app.py](../webcontrol/app.py) — subscribes to
  `xsphere/state/snapshot`, caches it, returns it under `snapshot` in
  `/api/state` (legacy `state` cache kept for the bespoke cards that need
  detail not in the registry — scanner step/total/elapsed and the follow-source
  dropdown). New `/control` route serves `control.html`.
- [../webcontrol/templates/index.html](../webcontrol/templates/index.html) —
  **the register GUI**: header pills driven by `service_alive` / `interlocks_ok`
  / `labjack_connected` / `ghs_esp32_alive` + counts + snapshot age; one card
  per group rendered generically; analog rows show value + unit + moving avg;
  binary rows show a coloured dot + yes/no; freshness colouring per row; nav
  links to **/control** and **Grafana**.
- [../webcontrol/templates/control.html](../webcontrol/templates/control.html)
  — **the control GUI**: valve card (per vessel: actual ● + desired + Open/Close
  + auto-open/auto-close toggles); PID card (per zone: PV / SET / OUT / err
  inline summary, setpoint input + current, Kp/Ki/Kd inputs + current as
  placeholders); gradient card (mode segmented buttons, base / Δv / Δl inputs
  with current + measured-ΔT for the deltas, resulting per-zone setpoints
  echo); automation cards (gradient scan, follow-a-sensor, ramp sequencer) kept
  bespoke. Built once, updated in place each tick so input focus and partly-typed
  values aren't clobbered.

**Validation**:
- `python -m slowcontrol.state.schema` → `OK … 65 states (16 controllable, 5 derived), 33 source topics`.
- Live read-only test against the broker (separate `client_id`): retained
  messages flowed in; 64 fresh / 0 stale / 1 invalid (the never-seen
  `gradient_scanner_state`); derived states (`dt_vertical_meas`, `pid_top_error`,
  etc.) computed correctly; moving averages populated. The retained snapshot
  was cleared on exit.
- `sudo systemctl restart xsphere-slowcontrol` → clean start; logs show
  `state_store started — 65 states, 33 source topics, tick 1.0s` between
  drivers and controllers as designed.
- `sudo systemctl restart xsphere-webcontrol` → clean start. `GET /` → 200
  (8.2 kB), `GET /control` → 200 (18.9 kB), `GET /api/state` returns
  `snapshot_age ≈ 0.6 s`, `counts {fresh:64, stale:0, invalid:1}`, 65 snapshot
  states + 32 legacy cache topics. Spot-checked: `t_cube_top: 207.565 K fresh`,
  `valve_cryostat: False fresh ctl=True`, `pid_top_error: 216.33 fresh`,
  `service_alive: True fresh`, `dt_vertical_meas: 0.537 fresh`.

**Open questions**:
- `pid_top_pv` reads ~381 K on the live system (≈108 °C) — probably a clamp RTD
  reading from when the heater was on. Surfaced here for awareness, not a
  state-layer bug; the registry is faithfully reporting reality.
- The "follow-a-sensor" source dropdown is still populated from the legacy
  `xsphere/sensors/temperature/...` topic cache rather than from the snapshot,
  because the snapshot doesn't currently carry each state's source topic for
  never-seen states. Fine for now; revisit if the legacy cache is dropped.

**Notes**:
- The slow-control service restart was clean and the existing controllers
  (gradient, autovalve [disabled], interlocks, gradient_scanner, LabJack)
  continue to work exactly as before — the StateStore is a pure observer at
  this point. No controller refactoring yet; that's flagged as
  out-of-scope-for-now in STATE_LAYER_PLAN.md.
- The state layer is now the single place to add a tracked quantity: edit
  `state.yaml`, restart the service, and it appears in both GUIs without any
  HTML changes. If it has a `control:` block, it appears as a widget in the
  control GUI's bespoke card *only if* the card references it (controlled by
  the hand-written valve/PID/gradient sections). To get a fully-generic
  controls grid we'd need a new bespoke-free "Misc controls" card; not done.

### Redundancy cleanup (same session)

A targeted survey for things made obsolete by Telegraf → InfluxDB → Grafana
ingestion and by the new state layer / web GUIs. What was removed, why, and
how to revert if something turns out to depend on it:

| What | Where | Why removed | Revert |
|---|---|---|---|
| `OmegaConfig` dataclass + the `omega:` block in `core/config.load()` | [core/config.py](core/config.py) | The Omega RDXL6SD-USB logger is no longer in use (config.yaml already had the comment "Omega logger is no longer in use"); the systemd unit `xsphere-omega-logger` is inactive; **no driver in the codebase ever referenced `cfg.omega`** — pure dead code. | `git revert` the cleanup commit; nothing else depended on it. |
| `slowcontrol/requirements.txt` | the slowcontrol package dir | Stale duplicate of the repo-root [requirements.txt](../requirements.txt). The systemd unit installs from the root file; `SETUP.md` step 2a uses the root file. Nothing referenced the slowcontrol-local one. | restore from git. |
| `omega-logger/` directory (4 files) | repo root | The Omega RDXL6SD-USB logger service. `systemctl is-active xsphere-omega-logger` ⇒ inactive; the slow-control code never imports anything from here; the directory was a self-contained, dormant subsystem. | restore from git history if the Omega is ever brought back. |
| "Future: OmegaDriver, LevelSensorDriver if integrated here" comment | [core/service.py](core/service.py) | Outdated — level sensors are owned by ESP32 firmware (`xsphere/sensors/level/...`); no plan to add an in-process Omega driver. | replace the comment if needed. |
| README.md: top-of-file feature list, system overview diagram, repository layout, quick-orientation table, MQTT schema row | [../README.md](../README.md) | Mentioned Omega logger, the old "Node-RED dashboard" framing, and `slowcontrol/requirements.txt` / `omega-logger/` paths. Replaced with the LabJack T7 + state layer + webcontrol picture; added `xsphere/state/snapshot` to the schema table. | restore from git. |

**Deliberately left in place** (mentioned for transparency):

- The `nodered/` directory — historical Node-RED flow JSON files. The flow
  files in the repo are out-of-sync backups, not live config. Useful as a
  reference. (The Pi's Node-RED Docker container itself was stopped in a
  follow-up — see "Node-RED container stopped" below.)
- `plc_nodered.json` (repo root) — backup of the **PLC's embedded** Node-RED
  flows (ladder-side, runs on the CLICK PLC's NRED module, *not* the Pi's
  Node-RED container). Still authoritative for the autofill logic.
- The autovalve controller (`controllers/autovalve.py`) — disabled in config
  (`autovalve.enabled: false`); kept because the user may want to bring it
  online as the primary autofill brain. Currently the PLC ladder is the sole
  autofill authority.

**Validation done after cleanup**:
- `python -c 'from slowcontrol.core.config import load; cfg=load("slowcontrol/config.yaml"); print(cfg.__dataclass_fields__.keys())'` → no `omega` field present, all others intact.
- `sudo systemctl restart xsphere-slowcontrol` → clean start; logs show the same component set as before (drivers / state store / 65 states / controllers / heartbeat) with no warnings or errors.
- `sudo systemctl restart xsphere-webcontrol` → clean; `GET /` and `GET /control` both `HTTP 200`; `/api/state` returns `mqtt: True, snapshot_age: 0.66 s, counts {fresh:64, invalid:1, stale:0}, 65 states`.

If something later breaks that traces to one of these removals: `git log --diff-filter=D --name-only -- omega-logger/` (etc.) finds the deletion commit; `git revert <sha>` undoes it.

### Node-RED container stopped — 2026-05-13

`docker stop nodered`. The container had been running ("Up 21 hours (healthy)")
but a downstream audit showed nothing actually consumes its outputs:

- It writes to InfluxDB v1 databases `Cryostat` / `Gas Handling System` /
  `Nanosphere`; the repo's Grafana dashboard ([grafana/xsphere-dashboard.json](../grafana/xsphere-dashboard.json))
  queries **only the v2 `xsphere` bucket** that Telegraf writes — none of the
  v1 databases. *Caveat:* the live Grafana datasource list couldn't be queried
  (auth required); if there are non-repo dashboards pointing at v1 databases,
  they'll go quiet for new data — `docker start nodered` restores writes.
- It publishes `valves/ds1001…1106`, `RDXL6SD/status*` — no subscribers in the
  slow-control / webcontrol code; a live broker sniff confirmed zero traffic
  on these.
- Its dashboard at `http://xbox-pi:1880/ui` had no recent `GET /ui` traffic in
  the container logs.

So it was duplicating ingestion (into databases nothing reads) plus a dead-end
publish path. After stopping, verified: `xsphere-slowcontrol` and
`xsphere-webcontrol` both active; `/api/state` returns the snapshot at
~1 s freshness (64 fresh / 1 invalid / 65 states); the broker continues to
deliver the `xsphere/sensors` + `xsphere/status` + `xsphere/state` streams.

The container's restart policy is `unless-stopped`, so it stays stopped across
Pi/Docker daemon restarts. To revert: `docker start nodered`.

The PLC's *embedded* Node-RED (the `plc_nodered.json` flows on the CLICK PLC's
NRED module — a separate process) is untouched; it still publishes the
`PLC RTD` / `PLC PID*` / `PLC XV*` topics, which are duplicates of the
slow-control's `xsphere/...` publishes. Nothing consumes them now, which is
fine.

**Related discovery flagged for the operator** (not fixed here): `pid_top_pv`
reads ≈381 K, which is exactly the PLC's `RTD4 = 108.18 °C` — i.e. Zone 1
(top)'s PID PV is sourced from RTD4, the channel `drivers/plc.py` thinks is
"unused". Worth a look in the CLICK programming software to confirm the PV
source is wired to the intended RTD.

### Trackers (state-tied-to-state, with offset) — 2026-05-13

The user asked whether the old "set PID 1 setpoint = RTD 2" feature was still
available, and added: also support `PID 1 setpoint = RTD 1 + 10 °C`. The old
webcontrol "Follow a sensor → zone setpoint" card did the basic relay
(single zone, no offset). Re-built as a first-class slow-control feature:

**New: TrackerController** ([controllers/trackers.py](controllers/trackers.py)).
A tracker writes `target_value = source_value + offset` on every tick
(~1 Hz, with a `0.005` deadband to avoid hammering identical PLC writes).
Optional `min_value` / `max_value` clamp. Defined entirely at runtime via MQTT
(`xsphere/commands/trackers/set` upsert, `.../remove`, `.../enable`); status
published retained on `xsphere/status/trackers`. Persists to
`slowcontrol/trackers.json` (gitignored) so trackers survive a service restart.
Refuses to write if the source is INVALID (never propagates stale-derived
setpoints) or if the target has no `control:` block in the registry. Reuses
the snapshot's `control:` mapping to know which command topic to publish and
how to template the payload, so any analog state with a `control:` annotation
is a valid target.

**Removed**: the webcontrol-side `_follow` global, the `/api/follow` route,
and the "Follow a sensor → zone setpoint" card in `control.html`. Subsumed
fully — the single-zone-no-offset case is just `{source, target, offset:0}`.

**New GUI card**: "Trackers — keep a setpoint following a sensor (+ offset)"
on `/control`. Lists current trackers (live from `xsphere/status/trackers`),
shows `last_sent` and `last_error` per row, has enable / disable / remove
buttons. The add-tracker form populates its dropdowns from the snapshot:
*target* lists every analog state that has a `control:` block; *source* lists
every analog state. An auto-generated id is used if you leave the id field
blank.

**End-to-end verification** (against the live broker, service running):
- Created `test_top_from_bottom: pid_top_setpoint = t_cube_bottom + 10`,
  disabled. Status topic updated, `slowcontrol/trackers.json` written.
- Enabled briefly. Confirmed the controller computed `261.16 K`
  (= `t_cube_bottom 251.16 + 10`) and the PLC driver wrote it to the
  Modbus register (visible in journalctl: `[plc] PID top setpoint → 261.16 K`).
- Disabled then removed. Status went back to `[]`, persistence file `[]`.
- Restored `pid_top_setpoint` to its pre-test value (165 K) — note for next
  time: enabling a tracker *immediately* writes the target; don't enable as a
  test step against the live PLC, or use `enabled: false` throughout.

**Caveats** (documented inline in [trackers.py](controllers/trackers.py) and
the GUI card's hint text):
- Enabling a tracker writes the target on the next tick — no grace period.
- A PID-setpoint tracker only "sticks" when `gradient_mode == absolute`;
  in `gradient` mode the GradientController overwrites the per-zone setpoints
  from `gradient_base`.
- Trackers can create feedback loops if you point them at each other or at
  a derived state that depends on the target. Not detected; document.
- Currently only `source + offset` (linear). If users want more (e.g.
  `2 * source`), the StateStore's existing safe-AST evaluator (in
  `state/store.py:_compile_expr`) is a clear extension path — make a tracker
  accept an `expr` string instead of `source + offset`.

### Sequencer (multi-step programs with actions) — 2026-05-13

The user wanted a more capable replacement for the old "Ramp sequencer" card
in the control GUI: build a list of *steps*, each step has *actions* (write
one or more states to constants and/or create/replace trackers) plus a *hold
time*, then run the program. Built as a slow-control-service controller (like
trackers), with a dedicated tab.

**New: SequencerController** ([controllers/sequencer.py](controllers/sequencer.py)).
Each step is `{label, hold_s, actions: [...]}`; each action is either
`{type:"set", target, value}` (one-shot write to the target's control topic)
or `{type:"track", target, source, offset}` (creates/updates a sequencer-owned
tracker with id `seq:<target>`). On entering a step, the controller reconciles
sequencer-owned trackers — any from the previous step whose target is not
covered by this step is removed; the rest are upserted via the existing
`xsphere/commands/trackers/set`. Then the constant-write actions fire (using
the snapshot's `control:` mapping for each target's command topic / payload
template). Then it holds for `hold_s` seconds before advancing. Stops promptly
on `xsphere/commands/sequencer/stop`. At end-of-program the **last step's
trackers are left in place** — the cryostat stays where the program landed
it; the operator manages them via the Trackers card on `/control`.

Persists to `slowcontrol/sequencer.json` (gitignored) so the program survives
a service restart. Refuses to mutate the program while a run is in progress.

**New GUI tab**: [`/sequencer`](http://xbox-pi:8088/sequencer)
([webcontrol/templates/sequencer.html](../webcontrol/templates/sequencer.html)):

- Top: run / stop / clear-program buttons, a progress bar with the remaining
  time on the current step, and a status pill (`idle` / `running step N/M` / etc.).
- Left card "Build a step (staging)": pick a target (any analog state with a
  `control:` block), pick mode (`set constant` shows a value input;
  `track source + offset` shows a source dropdown + offset input), `+ add
  action` to add it to the staging list. When all actions for a step are
  staged, give it an optional label + hold (minutes), `+ add step to program`.
- Right card "Program": the persisted ordered list of steps from
  `xsphere/status/sequencer`, with the running step highlighted; per-step
  remove button (only when idle).
- Header nav: `Register` / `Control` / `README` / `Grafana`; all other tabs
  got a `Sequencer ↗` link added.

**Removed** (subsumed): the old `_seq_worker`/`/api/seq` ramp sequencer in
`webcontrol/app.py` and its textarea card in `control.html`. `/api/seq` now
returns `HTTP 410` with a hint pointing at
`xsphere/commands/sequencer/...`.

**End-to-end verification** (against the live broker):
- Append two steps via MQTT (`xsphere/commands/sequencer/append`) — status
  topic + `sequencer.json` reflected each step in order.
- `set` with a fresh program replaces the whole list (1 step).
- `clear` empties it; `sequencer.json: {"steps": []}`.
- **Live run**: appended a 1-step program with a `set pid_top_setpoint =
  165.0` (the *current* value, so a no-op from the PLC's perspective) and a
  5 s hold; ran it. Logs show: `[sequencer] program set: 1 steps` →
  `starting sequence (1 steps)` → `step 1/1: noop test` →
  `[plc] PID top setpoint → 165.00 K (-108.15 °C): OK` →
  (5 s later) `sequence complete`. `pid_top_setpoint` unchanged at 165 K
  throughout. The status topic ticked `ends_in=4s → 3s → 2s → 1s → 0s →
  running=False/msg=done` correctly.

**Caveats** (also called out in the GUI's hint text):
- Running a step writes the actions *immediately* on step entry — confirm the
  values are sane before clicking *run*.
- Trackers created by the sequencer (id prefix `seq:`) coexist with manually-
  created trackers from the Control page. If a manual tracker and a sequencer
  tracker target the same state, both will write each tick; whichever fires
  later wins. The user should disable manual conflicting trackers before
  running.
- The same gradient-mode caveat as trackers applies: a `set` or `track`
  targeting `pid_*_setpoint` only sticks when `gradient_mode == absolute`;
  in `gradient` mode the GradientController will overwrite per-zone setpoints
  from `gradient_base`. To use the gradient as the lever from a sequence,
  target `gradient_base` / `gradient_dv` / `gradient_dl` instead.
- The program is mutable only while *not running* (`set` / `append` /
  `clear` rejected with a warning while a sequence is in progress).

### Gradient scanner removed; Sweep item added — 2026-05-13

The user asked: now that the Sequencer exists, what does the old "Automated
gradient scan" do, and is it still earning its keep? It only did one thing
— linearly sweep `gradient_base` with optional stability-wait — which the
Sequencer mostly covers, except it would require N hand-staged identical
steps. Replaced with a compact **Sweep** item type in the Sequencer.

**Removed**:
- `slowcontrol/plugins/gradient_scanner.py` (entire plugin).
- Its import + instantiation in `core/service.py` (the docstring's startup
  order ASCII updated).
- The `gradient_scanner_state` entry in `state.yaml` (registry is now
  64 states / 32 source topics, down from 65 / 33).
- The "Automated gradient scan" card in `webcontrol/templates/control.html`
  and the small block of JS in `update()` that read it.
- The retained `xsphere/status/gradient_scanner` topic on the broker was
  explicitly cleared (`mqtt publish -r -n ...`).

**Added**: `Sweep` dataclass in [controllers/sequencer.py](controllers/sequencer.py)
alongside `Step`. Items in the program are now a discriminated union:

  - `{"type":"step",  "label":..., "hold_s":<s>, "actions":[...]}`
  - `{"type":"sweep", "label":..., "target":<state-id>, "start":<f>,
                      "stop":<f>, "step":<f>, "dwell_s":<s>}`

Backward-compat: an item without `"type"` defaults to `"step"`. The runner
dispatches: a `Step` applies its actions and holds; a `Sweep` expands inline
into one sub-step per generated value, writing `target = v` and holding
`dwell_s` at each. Status now carries `sub_step` / `sub_step_total` for the
GUI to show "point 4/9 — 7:23 left" during a sweep.

Also fixed an existing bug while editing: `_apply_step` previously called
a `_current_seq_trackers()` stub that returned `[]`, so sequencer-owned
trackers from earlier steps in the same run were never auto-removed. Now
maintained as `self._run_active_trackers: Set[str]`; transitions between
steps in the same run correctly reconcile (cleared on run-end so a new run
doesn't accidentally remove leftover trackers from a prior run).

**GUI changes** ([webcontrol/templates/sequencer.html](../webcontrol/templates/sequencer.html)):
- New "Sweep a target (scan start → stop)" staging card next to the existing
  "Build a step (staging)". Has target dropdown (analog control-states),
  start / stop / step / dwell / label inputs, and a live preview ("9 points ·
  total 90 min (1.5 h)").
- Program list renders sweep rows with a sweep badge and the compact
  "`target` from start to stop step step" expression, plus the current
  sub-point when running ("point 4/9").
- Header pill `step ID/N · point P/T` while running a sweep.

**Verified end-to-end**:
- `Sweep.from_dict({...start:160,stop:200,step:5...}).values()` →
  `[160, 165, …, 200]` (9 points). Backward-compat: a legacy step dict (no
  `"type"` field) parses as `Step` correctly.
- Restart slowcontrol: state store at 64 states / 32 source topics; no
  GradientScannerPlugin in the controllers list; sequencer up at 0 items.
- Live 1-point safe sweep (`pid_top_setpoint = 165.0`, dwell 3 s, same as
  current value): status correctly ticked
  `running=True  step=0  sub_step=0/1  rem=2s → 1s → 0s` then flipped to
  `running=False msg=done`. One no-op PLC write to 165 K; `pid_top_setpoint`
  unchanged after. Cleared the program afterward.
- The retained `xsphere/status/gradient_scanner` topic was cleared (no
  ghosts left on the broker).
