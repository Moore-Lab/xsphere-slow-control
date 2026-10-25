# Heater PIDs stuck off: integrator windup

**Symptom.** A heater zone is in Auto with its PV below the setpoint, but the
output stays at 0 %. Switching the zone to Manual and back to Auto turns the
heater on at once. This has been happening for months. On 2026-10-25 it took
the bottom zone to 160.5 K, which is below xenon's triple point (161.4 K), so a
heater stuck off is the dangerous direction.

**Cause.** The PLC PID's integrator (its Bias, `DFn+04`) is driven far below
0 % in a single sample, and the PID's anti-windup then holds it there. Most
episodes were triggered by the slow control itself: the PV interlock wrote a
500 K surrogate into each PID's PV, and dropping that back to the real PV set
off the jump. The manual→auto toggle fixes it because the PLC's bumpless
transfer sets Bias = output.

**Fix (this branch).**
- The interlock no longer touches the PIDs' PV.
- The service watches each loop's Bias.
- A zone that is still stuck off gets the same Manual → Auto toggle an
  operator would do. The toggle is verified, and retried until the zone is
  back in Auto.

An optional ladder rung (section 6) makes the PLC itself immune.

---

## 1. How the CLICK PID really works

The CLICK help (topics CL246 and CL254) and the project's PID setup describe
the loop. A fit to live data from 2026-10-25 matched it to 4–5 digits. Every
sample time Ts:

```
e     = 100 · (SP − PV) / 220            % of the PV range (−200 … 20 °C)
Bias += Kp · (Ts / Ti) · e               "I_Reset" (DFn+06) is a reset TIME Ti, in s
CO    = Bias + Kp·e + Kp·(Td / Ts)·Δe    "D_Rate"  (DFn+07) is a rate TIME Td, in s
CO is clamped to [DFn+09, DFn+10] = [0, 100] %
```

What the setup adds to that formula:

- **Freeze Bias is on** (`C105 / C145 / C185 = 1`). The Bias does not move
  while moving it would push CO further past a limit. When moving it would
  pull CO back from the limit, it still integrates. The fits support that
  reading.
- **Derivative on error, no derivative limit** (`C104 = 0`). A step in SP or
  PV gives one huge D term.
- **Bumpless transfer Mode 2** (`C108 / C148 / C188 = 1`). On Manual → Auto,
  "the Bias Term is made equal to the CO".
- **PV input** = `RawPVAddr` = DF111 / DF136 / DF161. That is exactly the
  register the PV expressions (and, until now, the interlock) write. There is
  a 20 % PV filter.
- The **sample times**, measured, are 3.69 s (bottom), 7.26 s (top) and
  13.2 s (nozzle).
- The "pi"/"pid" bit `C107` only chooses the autotune algorithm. The loops
  run P + I + D.

Sensitivities with today's gains (Kp / Ti / Td: bottom 41 / 158 / 26,
top 35 / 311 / 51, nozzle 14 / 565 / 94):

|                                          | bottom | top  | nozzle |
|------------------------------------------|-------:|-----:|-------:|
| P action, % output per K of error        |  18.6  | 15.9 |   6.4  |
| Bias change per sample, % per K of error |  0.436 | 0.371|  0.149 |
| D kick per K of error step, %            |  131   | 112  |   45   |

## 2. The mechanism

1. While the interlock was tripped, the old code wrote PV ← 500 K, SP ← 3 K
   and OUT ← 0 on all three zones, every poll. Every service start trips it:
   no LabJack reading has arrived yet.
2. When the trip cleared, the next poll's expressions put SP and PV back
   together. Through the 20 % filter, the PV drops about 500 → 433 K in one
   sample, an error step of roughly +100 % of the range.
3. In that sample the D term (+8770 % on the bottom zone) beats the P term
   (−8010 %), so CO = 100 % while the error is about −430 K. Freeze Bias lets
   the Bias integrate in that direction, so it jumps to about −187 % in one
   sample. Later samples add to it while the filtered PV is still falling.
4. From then on CO = Bias + P + D sits at the 0 % floor, and Freeze Bias
   holds the Bias there. Hours spent with PV above SP change nothing.
5. Once PV < SP, the Bias unwinds at the rates in the table. The heater comes
   back when P·e exceeds |Bias|: minutes on the bottom zone, 40–80 min on the
   nozzle, about 5× slower.
6. A Manual → Auto toggle sets Bias = CO = 0, so the next sample gives
   CO = P + D > 0.

An operator raising a setpoint while the PV is far above it triggers the same
jump. That was the 09-16 episode: SP 1 → 165 K with the PV at 299 K. Pressing
Manual→Auto on a zone that is sitting at 100 % does the opposite harm: it sets
Bias = 100 and the zone overshoots.

## 3. Evidence

- **Tonight (EDT).** The 02:00 backup took MQTT down. At 02:00:15 the old
  process tripped and wrote PV = 500 K, which stayed in the PLC. Slowcontrol
  restarted at 02:16:13, tripped, and cleared at 02:16:14. The PV
  expressions came back at 02:17:05; the fitted Bias went 3 → −183 % at
  02:17:09. The setpoints came back at 02:18:41. The bottom zone then stayed
  at 0 % in Auto from 02:18:52 to 02:24:23 while its PV fell 165 → 160.5 K.
  The model predicts the heater back at 02:24:07; it came back at about
  02:24:25–36 (rms 1.0 %). Under a "freeze at both limits" rule the fit is
  47.8 % rms, so that rule is rejected. The top zone's integrator was read
  directly at 02:30:16: DF129 = −15.99 %, below the 0 % floor. Lucas toggled
  the nozzle (DF179, 02:23:49 → 51), and it came on 3 s later. That is within
  one 13.2 s sample, so the transfer acts at scan time.
- **90 days.** There were 52 stuck episodes, about 49 of them tied to the
  500 K surrogate. At the first PV < SP crossing:

  | PV < SP crossings          | stuck      |
  |----------------------------|-----------:|
  | after a windup event       | 77 % (33/43) |
  | without one                | 5 % (3/55) |

  Long stretches with PV above SP do not cause it: 9 stretches of 2 h or more
  with no event, none stuck, and episode length vs time above SP has
  ρ = 0.08. In 10 of 13 episodes the fitted model predicts the heater's
  return to the sample. Four episodes ended with a toggle; they jumped
  straight to 49 / 60 / 80 %.
- **Ruled out:** windup from long PV > SP stretches; a mode write every poll;
  writes to the output in Auto (CO is recomputed each sample); the PI/PID bit;
  the PV filter on its own.
- **Not measured yet:** the Bias during a stuck episode (DF104 / 129 / 154
  were never logged; they are now), whether the freeze is directional at the
  0 % end (inferred from fits), and whether the PLC keeps a Bias written over
  Modbus in Auto (section 5).

## 4. What changed in the slow control

All of this is in `slowcontrol/drivers/plc.py`, with settings in
`slowcontrol/config.yaml` under `plc:`.

**PV interlock** (`_check_pv_interlock`, `_write_labjack_to_plc`):
- The trip is decided at the start of every poll. While tripped:
  - every poll writes SP ← 3 K and OUT ← 0 on all three zones, and the
    LabJack mirrors DF210-212 get the 500 K surrogate;
  - the PIDs' own PVs are not written by anything;
  - the expression writes are skipped;
  - a setpoint command (page, AI, gradient controller) is stored and written
    when the trip clears;
  - a one-shot PV command is dropped.
- So the trip is the only writer of SP, and a bad RTD value can never reach a
  PID's PV. Because the trip no longer overwrites the PV, that has to hold
  without it:
  - The expressions are all evaluated first, and the interlock is asked
    again before anything is written. A LabJack reading that arrives in the
    middle of a poll is caught there. Without this, an absurdly low PV could
    sit in the PID for the whole trip, and under SP = 3 K that means the
    heater full on. The emulator shows exactly that with the re-check
    removed.
  - A LabJack RTD reading of NaN trips the interlock. NaN compares false
    against both band edges, so it used to pass.
  - A PV expression value that is NaN or below the band is never written,
    whatever its source (e.g. an open PLC RTD in `rtd5`). The PID keeps its
    last good PV, and the journal says so once.
- With today's gains, any real PV of 77 K or more and SP = 3 K keep the
  output at 0 % whatever the Bias, and Freeze Bias keeps the Bias where it
  was. When the trip clears there is still one 100 % sample per zone, from
  the SP step.
- `pv_interlock_writes_pv: true` brings the old PV leg back. Don't.

**Reading the PIDs** (`_read_pid_zone`, `_publish_pid_status`):
- The C-relay base was wrong: `C_BASE = 0`, while C1 is Modbus coil 16384.
  It is fixed. Each zone is now read in four requests: DF+00..12, DS+00..05,
  C+00..27, and the DF175/176 pair.
- New fields in `xsphere/status/pid/<zone>`:

  | field | meaning |
  |---|---|
  | `bias_pct` | the integrator |
  | `co_lo_pct`, `co_hi_pct` | the output limits |
  | `pv_raw_k` | the PV before the filter |
  | `error_code` | DS+00 |
  | `sample_s` | the sample time |
  | `mode_source` | where `mode` came from |
  | `stuck_off`, `stuck_off_s` | the stuck-off flag, and how long it has held |
  | `rescue_remedy`, `rescue_count`, `rescue_pending`, `rescue_last` | what the remedy has done |
  | `guard`, `guard_writes` | the integrator guard |

- `mode` now comes from the PID's own indication bits (C+25 Manual, C+26
  Auto, C+27 Autotune). The ladder mirror DF176/178/180 copies the mode
  *request*, so it said "auto" for a loop that a PID error had dropped to
  Manual. The mirror is now only a fallback.
- The registry has `pid_<zone>_bias` and `pid_<zone>_stuck_off`. Telegraf
  records `bias_pct`, `stuck_off` and `rescue_count` in `pid_state` once
  `telegraf/deploy-iotstack.sh --apply` has been run.
- The **autotune** and **controller-type** commands are refused. They never
  reached the PLC because of the wrong base. Fixed, autotune would start a
  real closed-loop autotune, which is a heater limit cycle, so it needs an
  enable gate first.

**Stuck-off detection** (`_stuck_condition`, `_update_stuck_off`):
- A zone counts as stuck off when all of these hold at once:
  - the PID's indication says Auto;
  - no PID error;
  - PV more than `pid_stuck_off_delta_k` (1 K) below SP;
  - output at most `pid_stuck_off_output_pct` (0.5 %);
  - an output range that allows heat;
  - the Bias below the output floor.
- That must last `pid_stuck_off_s` (20 s), and at least 2 samples + 5 s, so
  12 / 19 / 31 s for bottom / top / nozzle.
- It never applies while the interlock is tripped.
- The Bias test is what separates windup from the D term braking a fast rise.
  It is also why resetting the Bias to the output can only add heat.
- When a zone is confirmed stuck: a journal warning, the retained alert
  `xsphere/alerts/pid_stuck_off/<zone>`, and `stuck_off: 1` in the status.

**Remedy** (`pid_stuck_off_remedy: toggle`; `_pid_rescue`, `_pulse_auto`):
- The remedy acts only if the mode command DF175/177/179, the mirror and the
  indication all say Auto, and only with bumpless Mode 2 on (C+08), which is
  what makes the toggle a Bias reset. It acts at most once per zone per
  `pid_rescue_holdoff_s` (600 s), on a fresh re-read of the zone.
- It also needs the zone's PV to be fed: a PV expression that evaluated this
  poll, with the PID's PV register within 5 K of it. With no PV source the
  PV is a frozen number and "below SP" may be stale, so the service only
  alerts and logs why.
- The sequence:
  1. Write the zone to `slowcontrol/pid_rescue.json`, fsynced, before
     anything else.
  2. Write the mode command 0, the same register the page's Manual button
     writes.
  3. Wait until the indication shows Manual.
  4. Write 1.
  5. Wait until the indication shows Auto.
  6. Remove the zone from the file.

  Each wait is 0.2 s steps for up to 3 s. A zone typically spends about 0.4 s
  in Manual.
- **Not back in Auto?** The service asks again every 10 s, as Manual (0)
  then Auto (1), until the indication shows Auto.
- **Crash or restart in the middle?** The file makes the next start do the
  same. A clean stop also sends one last Auto request.
- **Operator wins.** Any mode command for that zone, from the page, the AI or
  a sequence, ends the remedy at once. One that arrives after the service
  has started deciding, but before its first write, stops the toggle before
  it writes anything. If it lands in the middle of a toggle, the operator's
  value is written again so it is the one that stands.
- Each rescue is logged. The status shows `rescue_count` and `rescue_last`:
  time, result, PV, SP, the Bias before and the Bias read straight after
  (`bias_after_pct`, which should be the output, about 0 %). If the Bias did
  not move, the journal says the toggle did not reset it.
- `alert` gives the alert without the toggle. `off` turns detection off.

**Integrator guard** (`pid_integrator_guard: false`; `_pid_guard_bias`):
- Every poll, in Auto only, it clamps the Bias into the output limits.
- That would stop windup before it can hold a heater off, but it depends on
  the PLC keeping a Bias written in Auto, which has not been tested (section
  5).
- If the Bias is still outside the limits after two samples of clamping, the
  guard logs an error and turns itself off for that zone. The toggle remedy
  still covers it.

## 5. Acceptance test before turning the guard (or the rung in section 6) on

Does the PLC keep a Bias written in Auto? The test puts no heat in. Lucas
runs it in the CLICK software:

1. Pick a zone that is in **Auto at 0 % with PV above SP**, so its heater is
   off anyway. For example, lower its SP 2 K below PV for the test, then put
   it back.
2. **Monitor → Data View → Add New Data View**, add the zone's Bias (DF104,
   DF129 or DF154) and its output (DF108, DF133 or DF158). Write down the
   Bias, B.
3. Press **Edit**, type B − 1 as the New Value for the Bias, and double-click
   the write icon.
4. Wait longer than one sample (4 s bottom, 8 s top, 14 s nozzle) and read
   the Bias:
   - **B − 1** means the PLC keeps the write. With PV above SP and the output
     at 0 % the Bias is frozen, so it stays where you put it.
   - **Back at B** means it does not.
5. Write B back. The output stays at 0 % throughout.

If the PLC keeps the write, set `pid_integrator_guard: true` in
`slowcontrol/config.yaml` and restart the service. If it does not, leave the
guard off: the toggle remedy is the fix, and the ladder rung below would not
work either.

## 6. Optional, most robust: a clamp rung in the ladder

The same clamp done by the PLC every scan works with the Pi off or the
service down. Only do it if the section 5 test passed. Plan it for a quiet
window, not with condensed xenon held near the triple point. The help says
nothing about whether a run-time program write leaves the PID loops in Auto,
and a STOP → RUN certainly does not: all loops start in Manual.

In **CLICK Programming Software v3.71**:

1. **Connect to the live PLC.** **PLC → Connect** (Ethernet, PLC
   192.168.8.119). When asked, choose **Read project from the PLC**. The live
   ladder is newer than the `.ckp` files on the DAQ PC and the 2026-04 PDF,
   so edit what is in the PLC.
2. **Back it up.** **File → Save Project As…**, for example
   `XMS-control-2026-10-25-before-clamp.ckp`.
3. **Open the Main Program** and click the last rung, the one with **END**.
   Use **Edit → Insert → Insert Rung Before Cursor** for each new rung below.
   Contacts come from the **Instruction List** (View → Window → Instruction
   List): *Contact NO*, *Contact NC* and *Compare*. The coil side is
   *Copy → Single Copy*, with **One Shot left unticked** so it runs every
   scan.
4. **Add two rungs per PID**, six in all:

   | rung | NO contact (Auto ind.) | NC contact (Autotune ind.) | Compare | Single Copy |
   |---|---|---|---|---|
   | HTR1 low  | C126 | C127 | `DF104 < DF109` | DF109 → DF104 |
   | HTR1 high | C126 | C127 | `DF104 > DF110` | DF110 → DF104 |
   | HTR2 low  | C166 | C167 | `DF129 < DF134` | DF134 → DF129 |
   | HTR2 high | C166 | C167 | `DF129 > DF135` | DF135 → DF129 |
   | HTR3 low  | C206 | C207 | `DF154 < DF159` | DF159 → DF154 |
   | HTR3 high | C206 | C207 | `DF154 > DF160` | DF160 → DF154 |

   HTR1 = PID 1 = bottom (Y004), HTR2 = PID 2 = top (Y003), HTR3 = PID 3 =
   nozzle (Y002). DF109/DF110 etc. are each PID's output limits (0 / 100 %).
   For the comment, use **Edit Rung Comments**: "Anti-windup: keep HTRn Bias
   inside the CO limits (docs/pid-windup.md)".
5. **Program → Syntax Check** (F8) and fix anything it lists.
6. **File → Save Project.**
7. **PLC → Write Project into PLC.** Tick **RUN Time Edit**: the Ethernet
   CPU then downloads while the old program keeps running and switches
   without stopping. Confirm both dialogs. If SC30 (_Run Edit Project Error)
   comes on, the old program is still running and nothing changed.
8. **Check.** Open **Monitor → PID Monitor → HTR1 / HTR2 / HTR3**. Each must
   still be in Auto; if one is in Manual, switch it back to Auto from the
   Control page. In a Data View, DF104, DF129 and DF154 should stay between
   0 and 100.

The slow control needs no change for this. With the rung in place the Bias
never stays below the floor, so the stuck-off detection never fires.

The Control page's mode readback (DF176/178/180) copies the request bits. The
service no longer relies on it, so there is no need to repoint that rung at
the indication bits C125/C126, but you can.

## 7. Deploying

- **Restart.** The service needs a restart (`xsphere-slowcontrol`), so ask
  Lucas first. The tree on the Pi does not keep the PV and setpoint
  expressions across a restart. Save them from `/api/state` just before and
  re-send them right after. At 02:30 on 10-25 they were bottom rtd2 / 165 K,
  top rtd3 / 170 K and nozzle rtd1 / 165 K.
- **What the restart does.** The start-up trip now writes only SP 3 K and
  OUT 0. Expect one 100 % sample per zone when its setpoint comes back, but
  not the minutes-long swings or the windup that previous restarts caused.
- **No toggle on the old code.** Until it is deployed, toggle Manual → Auto
  only on a zone that is in Auto at 0 % with PV below SP. Never toggle a zone
  sitting at 100 %: that sets Bias = 100 and it overshoots.
- **Telegraf.** `telegraf/deploy-iotstack.sh --apply` starts recording
  `bias_pct`, `stuck_off` and `rescue_count`. This is optional, but it is how
  the Bias finally gets logged.
- **`claude/pid-state-persistence`** (467d2ab, unpushed) puts the expressions
  back after a restart, on the first poll that finds the interlock clear.
  - On the old code that replays tonight's 500 K → real-PV kick
    automatically, so merge it only on top of this branch.
  - It needs a rebase: it uses `_write_labjack_to_plc`'s return value, and
    here the trip is decided by `_check_pv_interlock` at the top of
    `poll()`.
  - With this branch, its restore is just an SP step from 3 K: a single
    100 % sample.
- **Weekly backup.** Keeping MQTT up during the Sunday 02:00 IOTstack backup
  would avoid the trip altogether.
- **Gains.** Setting Td = 0 (PI) would remove the D kick entirely. That is a
  tuning decision for Lucas. The old "pi" readback never meant the loops
  were PI.
