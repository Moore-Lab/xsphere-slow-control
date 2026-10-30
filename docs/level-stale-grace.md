# Level-stale grace: the reasoning

**For review.** This changes when a safety interlock disarms the AI heaters.
Since 2026-10-30 the grace is on by default (120 s);
`XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S=0` restores the old immediate disarm.
How to set it: `aicontrol/README.md`, "Level-stale grace". Deploy steps and
open questions: `docs/session-log.md`, 2026-10-30.

**The problem.**
- The gas-cart ESP32 drops off Wi-Fi several times a day: 26 s to 23 min on
  2026-10-28 (`docs/ghs-esp32-dropouts.md` on `claude/ghs-esp32-dropout-note`).
- After 60 s without its LN2 levels, `slowcontrol/controllers/interlocks.py`
  raises `level_stale/ballast` and `level_stale/primary_xe`. It checks every
  15 s, so the rule fires 60-75 s into a dropout.
- aicontrol disarms the AI heaters on any not-ok edge of
  `xsphere/status/interlocks`.
- On 10-28 it did so at 16:49, 18:58 and 19:41, and on 10-29 at 00:47:01,
  01:04:46 and 03:38:31. The last came 79 s after Lucas had armed. On 10-30 it
  did so at 06:48:18Z, during the overnight wobble tests.
- Each time the AI's run stopped until Lucas re-armed. That includes the DAQ's
  LN2 thermostat (`runs/tree_thermo.py`), which stops on a heater disarm by
  design. The control session runs a stopgap (`runs/thermo_keeper.py`) that
  waits for the re-arm.
- During a hold the heater arm stays, so `tree_thermo.py` is not stopped by a
  disarm. Its own chamber guard still stops it once Setra #4 is older than
  `--p-max-age` (45 s by default, 90 s on 10-30).

## 1. What each side protects

**`level_stale` protects the LN2 fills, not the heaters.**
- The two levels are the ESP32's legacy analog readings of the ballast and
  primary Xe cryoflasks. The PLC ladder fills those flasks through XV1 and XV2
  using them (DF251/DF252 → DF351/DF352). A frozen level means the ladder acts
  on an old reading.
- The AI's own opens of XV1 and XV2 already refuse a stale level or a stale
  ESP32 heartbeat (`Actions._require_level_fresh`). That holds whether or not
  anything is disarmed.

**What a dropout costs the heater side** is the AI's view of the xenon. The
same ESP32 sends Setra #1-#4 and the GHS vacuum gauges, and pressure is how the
AI judges where the liquid is. So the AI should not change heater setpoints
during a dropout. That doesn't mean it has to lose its arm.

## 2. The change

| | Before (grace 0) | Now (grace G, default 120 s) |
|---|---|---|
| Not-ok with only the two `level_stale` rules | heaters disarmed at once | heaters **held**: the arm stays, every AI heater write is refused (re-checked before each publish, like the arm), a running ramp stops |
| …and it clears within G | Lucas re-arms | writes allowed again, no re-arm; audited as `heater_hold` |
| …and it lasts past G | – | heaters disarmed, reason "slowcontrol interlocks stayed not-ok (…) past the G s level-stale grace" |
| Any other rule, alone or added during a hold | disarmed at once | disarmed at once (the hold ends) |
| AI heater writes in the 60-75 s before the rule fires | allowed | allowed (unchanged) |

Unchanged:
- `interlocks.py` itself, so the alert still shows on the Control page and on
  `xsphere/alerts/…`; slow control needs no restart;
- every other disarm: PV interlock, temperature stale or out of range,
  `pid_saturated`, snapshot age, heartbeat, slow-control restart, reconnect,
  arm expiry;
- the `ln2` and `pneumatic` groups.

`level_stale/cryostat` is not covered. It comes from the PLC driver, not the
ESP32.

A hold shows in `get_ai_control` (`heater_hold`, `level_stale_grace_s`), in
`may_now` (also on the Control page card, with the time it would disarm), in
the overview's warnings and in `get_guide`.

## 3. Why it is not less safe

- **No blind heater writes.** During a hold the AI can write nothing to the
  heaters, which is exactly where a disarm leaves it. The PLC PIDs keep
  regulating on their current setpoints either way.
- **The long-outage backstop stays.** A dropout that outlasts the grace
  disarms as before, only G seconds later: 3-3.3 min after the ESP32 went
  quiet at the default.
- **The policy change** is that, after a short dropout, the AI may write to
  the heaters again without a human looking first. The audit records the hold
  and its end. Note also that an arm Lucas makes during a hold is still
  dropped when the grace runs out.

## 4. Choosing G

With today's firmware, the rule is active from 60-75 s into each gap
(15 s quantised):

| Gap | `level_stale` active | G = 90 s | G = 120 s |
|---|---|---|---|
| 26, 47, 49, 55 s (10-28); 48 gaps of 10-58 s (10-30 00:01-02:59 EDT) | never | – | – |
| 85 s (10-28) | 15-30 s | kept | kept |
| 68-119 s (10-30, from 02:47:10 EDT) | up to ~60 s | kept | kept |
| 167 s (10-28) | 105-120 s | disarmed | kept, or disarmed at exactly 120 |
| ~3.5 min (10-29 03:37, back by 03:41) | ~1.5-2.75 min | disarmed | likely disarmed |
| 23 min (10-28) | ~21 min | disarmed | disarmed |

The 10-30 numbers come from `runs/hold_20261030.csv` in xsphere-daq: Setra #4's
age every 2 s, 00:01-02:59 EDT. The watcher stopped with the disarm at
02:48:18, when the age was 68 s, and was back at 02:49:11 with a fresh reading.

`tests/test_aicontrol_grace.py` runs the real `interlocks.py` watchdog against
the service on one clock. With G = 120 s, a 120 s gap keeps the arm whatever the
watchdog's phase, and a dead ESP32 disarms 180-195 s after it went quiet.

The firmware branch (`gas-handling-system` `claude/esp32-link-watchdog`)
changes this:
- it restarts the ESP32 after 60 s without a link, so a stuck link comes back
  after about 65-100 s;
- `level_stale` is then active for 45 s at most, and often not at all.

**The default is 120 s.** It saves gaps up to about 3 min, such as the 167 s
one, but not the 3.5 min or 23 min ones. Once the firmware is flashed, 90 s
would cover every gap a restart fixes, with margin. 120 s still works then; it
only disarms a dead ESP32 30 s later.

## 5. The alternative not taken

The alternative is to scope `level_stale` to the LN2 actions it protects:
never disarm the heaters on it, only block XV1/XV2 opens. That is the cleaner
model, and the opens are already gated. But without a separate rule that heater
writes need fresh pressure, the AI could keep writing heater setpoints through
a 23-minute dropout. On `claude/aicontrol-hardening` the pressure guard has
such a rule for warming writes. The grace keeps the existing disarm as the
backstop and adds the missing write hold, with less change.

Raising `LEVEL_STALE_S` in `interlocks.py` was not taken either. It would also
delay the alert that guards the XV1/XV2 fills, and slow control would need a
restart, which clears the PID expressions and drops every setpoint to 3 K.

## 6. Merging

- Now on `claude/heater-arm-esp32-grace`, built on `claude/pid-windup-fix`
  (b51f193), the code the Pi ran from 2026-10-25. Its slow-control files are
  unchanged. Deployed on the Pi at 1c20b49 on 2026-10-30 at 15:21Z
  (`docs/session-log.md`).
- First written on `claude/ai-control-api` (7023236). Do not deploy that
  branch: it lacks the top/bottom swap and the windup fix.
- Trial merges of the first version, on 10-29:
  - **`claude/ai-control-plus-swap`:** clean; 1040 tests pass.
  - **`claude/ai-notifier`:** conflicts only in `aicontrol/README.md`, where
    both add a section after "InfluxDB token". Keep both; this one goes before
    "Phone alerts". 1122 tests pass. Hold entries now have their own audit
    kind, `heater_hold`, so the notifier no longer lists them as "AI notes";
    it only counts them in its tally of kinds.
  - **`claude/aicontrol-hardening`:** 19 conflict hunks in `arms.py`,
    `actions.py`, `app.py`, `config.py` and `reads.py`, so it needs a port.
    There the pressure guard's `blind_disarm_s` (a rails setting, off by
    default) is a second disarm for the same dropout.
