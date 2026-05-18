# Session 0 Kickoff — xsphere state layer (and beyond)

The first thing to read at the start of any Claude Code session that touches
`slowcontrol/state/`, `state.yaml`, the webcontrol GUIs, or the controllers'
interaction with the state layer. This is **workflow** guidance, not
architecture — the architecture lives in [STATE_LAYER_PLAN.md](STATE_LAYER_PLAN.md).

It is loosely adapted from the kickoff doc the microsphere-automation repo
uses for its Layer 3 work; the shape is the same, the project specifics aren't.

## Reading order

Before writing code, in this order:

1. [STATE_LAYER_PLAN.md](STATE_LAYER_PLAN.md) — the design contract. What the
   layer is, what's deliberately out of scope, the `state.yaml` field
   reference, the freshness model, the `StateStore` contract.
2. [SESSION_LOG.md](SESSION_LOG.md) — the running record of past sessions.
   Read at least the most recent entry; skim earlier ones for context.
3. [state.yaml](state.yaml) — the registry you are most likely to touch.
4. [state/schema.py](state/schema.py) — the loader and types; understand the
   `StateDef` shape before adding fields to it.
5. [state/store.py](state/store.py) — the runtime. Read the docstring; trace
   one MQTT message from `_on_message` to the snapshot dict.
6. The matching GUI template — `webcontrol/templates/index.html` (register) or
   `webcontrol/templates/control.html` (control) — depending on what you're
   changing.
7. [../SYSTEM_ARCHITECTURE.md](../SYSTEM_ARCHITECTURE.md) — the broader system
   context (PLC register map, MQTT schema, hardware topology). Don't memorise
   it; know where it is.

If any of these files are missing, **stop and ask**. Don't guess.

## Session scoping

Each session targets one well-defined slice:

- "Add a new state to `state.yaml`" — usually pure config; restart the service;
  verify it shows up in both GUIs.
- "Add a new derived expression" — register it under `derived:`; verify the
  evaluator accepts the syntax (`+ - * /`, parens, unary `±`, numbers, bare
  ids) and that all references exist.
- "Add a new bespoke control card to `control.html`" — wire it from the
  snapshot the same way the existing cards do.
- "Refactor a controller onto the StateStore" — flagged in STATE_LAYER_PLAN.md
  § Out of scope; do as a dedicated session.

A session does **not** silently fix problems outside its scope. If a previous
session's code is wrong, surface it; don't quietly correct it. Silent fixes
hide regressions.

If the targeted slice turns out to be larger than expected, stop and report.
The slice boundaries are the right granularity; if a slice feels too large the
implementation has probably grown unauthorised abstractions.

## Before writing code in a session

After reading the files, in order:

1. State the session's target in your own words. One sentence.
2. List the files you will create or modify. If the list includes files
   outside the slice, that's a sign you're about to do too much.
3. Raise clarifying questions about ambiguous requirements **before** writing
   code. Defaults: yes to schema-only changes, yes to additive GUI changes
   tracking the registry, **no** to changes that affect controller behaviour
   without explicit authorisation (these touch the live cryostat).
4. Sketch the public API of any new module (function/class signatures, no
   bodies). Wait for sign-off before filling in.

## When to push back

Push back when:

- The plan contradicts STATE_LAYER_PLAN.md. The plan doc is the contract;
  contradictions are worth fixing.
- A request would require breaking the freshness/`Entry` invariants
  (mutating `Entry`, swallowing INVALID, etc.).
- A request would put complex logic into a Jinja template or HTML — the GUI
  is supposed to be a thin renderer of `xsphere/state/snapshot`. If the
  computation belongs in the StateStore (a derived state) or a controller,
  surface that.
- A request would put hardware logic in the StateStore. It is a pure
  observer; it must never write to the broker outside its own
  `xsphere/state/...` namespace.
- Asynchronous primitives feel more natural than threading. The slow-control
  service is threading-based throughout; matching it keeps things uniform.

Push back means: stop, write a paragraph explaining the issue, suggest a
resolution, wait for confirmation. Don't push back means: proceed without
comment.

## What "done" looks like for a session

A session is done when:

- The targeted change is implemented.
- `python -m slowcontrol.state.schema` passes if `state.yaml` was touched.
- The slow-control service starts cleanly (`sudo systemctl restart
  xsphere-slowcontrol` then `journalctl -u xsphere-slowcontrol -n 30`); no new
  errors in the log.
- Both GUIs load (`curl -sS http://localhost:8088/` and `.../control`) if
  they were touched; the register page lists the new state(s); the control
  page's relevant card reflects them where applicable.
- A SESSION_LOG.md entry is appended (date, target, files touched, validation
  done, open questions).

A session is **not** done when:

- A change was made and the service wasn't restarted to verify it actually
  loads on the live system.
- A new state was added to `state.yaml` but no GUI surface was updated /
  verified.
- The SESSION_LOG.md entry was skipped.
- A controller's behaviour changed without an explicit decision to do so.

## Conventions

These hold across the slow-control codebase:

- Threading, not asyncio.
- All MQTT topics live under `xsphere/...`. Sensors under
  `xsphere/sensors/`, status under `xsphere/status/` (retained), commands
  under `xsphere/commands/`, the consolidated state on
  `xsphere/state/snapshot` (retained).
- All units canonical SI at API boundaries; the GUI may display in °C / mbar
  / "0–10" / etc. via the `unit` annotation in `state.yaml`.
- `Entry` is frozen; never mutate.
- No bare `except:`. Catch specific exception types; the StateStore's
  `_on_message` translation point is the one allowed `except Exception`.
- Format strings in log calls use lazy interpolation (`log.info("foo %s", x)`,
  not f-strings).
- Type annotations on every public function in the `slowcontrol.state`
  package.

## Live system caveats

- `xbox-pi` is the broker and runs both `xsphere-slowcontrol` and
  `xsphere-webcontrol` as systemd units. Restarts are fast and safe (autofill
  is owned by the PLC ladder, not Python), but **don't** restart blindly when
  an experiment is in progress without checking.
- The PLC's Modbus TCP server is single-owner; if a probe (`tools/plc_probe`)
  is open, the slow-control service's polls will fail.
- `xsphere/state/snapshot` is currently a one-blob retained topic. If you
  start consuming it from a second process, prefer the snapshot's
  `generated_at` field for change detection over the broker's retained-flag.

## Session log

After each session, append to [SESSION_LOG.md](SESSION_LOG.md):

- Date, session number, target.
- What was implemented (file paths).
- What was validated (schema check / service restart / GUI fetch).
- Open questions raised.
- Anything that surprised the implementation.

The log is the artifact that lets the next session start with context. It is
not optional.
