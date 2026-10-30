# Session log: xsphere-slow-control

Newest first. Absolute dates; times are UTC (Z) or US Eastern (EDT) as marked.
The state layer's own history is in `slowcontrol/SESSION_LOG.md`.

---

## 2026-10-30, 11:21 EDT: the heater grace is deployed

Deployed at Lucas's request, with him at the Control page to re-arm.

- **Running.** The Pi's `~/xsphere-slow-control` is on
  `claude/heater-arm-esp32-grace` at 1c20b49 (it was `claude/pid-windup-fix`,
  b51f193).
  - Only `xsphere-aicontrol` was restarted, at 15:21:42Z.
  - Slow control and the web panel kept running; their code is unchanged.
  - The untracked `query_db.py` on the Pi is left as it was.
- **Checked.**
  - The journal shows "Level-stale grace 120 s".
  - Rails revision 4 and the audit reloaded.
  - `get_ai_control` shows `level_stale_grace_s` 120 and `heater_hold` null.
  - The Pi's Python 3.11 imports the branch. Its env has no pytest, so the
    tests ran on the DAQ PC only.
- **How the code got there.** The Pi cannot fetch from GitHub: its `origin` is
  `git@github.com:…`, and GitHub refuses it. So the commit went from the DAQ PC
  straight into the Pi's repo:
  ```bash
  git push ssh://192.168.8.116/home/xbox/xsphere-slow-control <sha>:refs/heads/<branch>
  ```
  - On the DAQ PC, `ssh xbox-pi` fails host-key verification, but the IP
    works (key login, passwordless sudo).
  - The branch is on GitHub as well.
- **What it interrupted.** The restart dropped every arm.
  - The restart waited until the thermostat's XV3 pulse had closed (11:21:00).
  - The day's wobble run (`wobble_ab.py`, block 15) then stopped on
    "ln2 disarmed".
  - Lucas re-armed heaters and LN2 at 13:39 EDT (17:39Z), so the AI's LN2
    thermostat was off for 2 h 18 min. The arms came back with limits
    revision 4, the 120 s grace and no hold. The control session relaunches
    the run.
- **Rollback.** On the Pi, run `git checkout claude/pid-windup-fix` and
  `sudo systemctl restart xsphere-aicontrol`, then re-arm.
- **What to look for.** At the next ESP32 dropout, the audit should show
  `heater_hold` entries instead of an `auto_disarm`.

---

## 2026-10-30: the AI heater arm no longer drops on a GHS ESP32 dropout

Branch `claude/heater-arm-esp32-grace`, worktree
`.claude/worktrees/heater-arm-esp32-grace`, on top of `claude/pid-windup-fix`
(b51f193). Committed only: not pushed, not deployed. Nothing on the Pi was
changed and no aicontrol write tool was called.

### Cause

**What the Pi runs.** I checked this by reading only. Both slow control and
aicontrol run `claude/pid-windup-fix` at b51f193:
- aicontrol reports `version` 1.0.0, `instance` production, started
  2026-10-25T14:04:19Z;
- its overview prints b51f193's PV-interlock sentence ("the PV is left as it
  was");
- slow control publishes `pid_*_stuck_off`, which no other branch has.

So the 10-29 grace (`claude/level-stale-grace`, in
`Desktop\ghs-dropout-mitigations`) was never deployed. It was built on
`claude/ai-control-api` and must not be deployed as it is: that branch lacks
the top/bottom swap and the windup fix.

**The rule.** `slowcontrol/controllers/interlocks.py`:
- every 15 s, the watchdog takes each level's age since its last message on
  `xsphere/sensors/level/<vessel>`. That is the arrival time on the Pi's
  monotonic clock, not the ESP32's own timestamp;
- over 60 s (`LEVEL_STALE_S`), it raises `level_stale/<vessel>`;
- it publishes `{"rules_active": […], "ok": …}` on `xsphere/status/interlocks`,
  retained, every 15 s;
- there is no debounce, so a gap is flagged 60-75 s in;
- ballast and primary_xe come from the GHS ESP32; cryostat comes from the PLC
  driver;
- nothing else the ESP32 sends is checked by interlocks.py. Its temperature
  goes to `sensors/environment/…`, not `sensors/temperature/…`.

**The watcher.** `aicontrol/arms.py`, `Supervisor._on_interlocks`:
- on the first live ok → not-ok message, it disarms `heaters`: "slowcontrol
  interlocks turned not-ok (…)";
- any rule counts, with no grace, and only the heaters group. That is why ln2
  and pneumatic stayed armed and the PIDs kept their setpoints.

**10-30.**
- Setra #4, sent by the same ESP32, went silent at 02:47:10 EDT.
- `level_stale` fired at 02:48:18, 68 s in, and the heaters were disarmed at
  06:48:18Z.
- The thermostat closed XV3 and stopped 3 s later.
- The data were back by 02:49:11.

From `runs/hold_20261030.csv` (xsphere-daq), 00:01-02:59 EDT:
- 49 gaps over 10 s;
- only that one was longer than 58 s;
- 6.6 % of the 2 s samples were more than 10 s old.

### Change

- **Port.** I ported the 10-29 grace commit (2d32313) onto b51f193 with no
  conflicts. A not-ok made only of `level_stale/ballast` and
  `level_stale/primary_xe` now **holds** the heaters instead of disarming them:
  - the arm stays;
  - every AI heater write is refused: setpoints, gradient, ramps, auto, PV
    source and the NGE supply. The check runs again before each publish, so a
    call already under way stops too;
  - a running ramp stops;
  - the heaters disarm if the rules are still active when the grace runs out;
  - any other rule disarms at once, as before.
- **Default 120 s.** On the 10-29 branch the grace was 0 unless set. Now:
  - a dropout of up to about 3 min keeps the arm;
  - a dead ESP32 still disarms the heaters 3-3.3 min after it went quiet;
  - `XSPHERE_AICONTROL_LEVEL_STALE_GRACE_S=0` gives the old behaviour back.
- **Audit.** The hold's start and end are audited as `heater_hold`. On 10-29
  they were audited as `note`, which is the kind of the AI's own notes.
- **Visibility.** A hold shows in `get_ai_control` (`heater_hold`,
  `level_stale_grace_s`) and in `may_now`, which the Control page card shows
  with the disarm time. It also shows in the overview warnings and
  `get_guide`. The aicontrol README and OPERATIONS.md are updated.
- **Scope.** Only aicontrol files and docs changed. `slowcontrol/` and
  `webcontrol/` are identical to b51f193.
- **Tests.** 31 in `tests/test_aicontrol_grace.py`: the 19 from 10-29, adjusted,
  and 12 new ones.
  - The timeline tests run the real `interlocks.py` watchdog against the
    service on one fake clock:
    - a 120 s gap, the longest 10-30's can have been, keeps the arm in every
      watchdog phase, and it disarms with grace 0;
    - a dead ESP32 disarms 180-195 s after it went quiet;
    - a 58 s gap never reaches the heaters;
    - heater writes are refused during the hold and allowed after it.
  - Two mutations (the grace never runs out; nothing is held) each fail them.
  - The aicontrol suite has 1052 passing tests (1021 before); the whole suite
    has 1396.
- **Reasoning.** `docs/level-stale-grace.md` has the reasoning, with a table
  of the 10-28, 10-29 and 10-30 dropouts.

**What the DAQ's overnight runs will see.**
- During a hold, `arms.heaters.armed` stays true, so `thermo_keeper.py` and
  `tree_thermo.py` keep going.
- `tree_thermo.py` still stops on its own Setra #4 age guard
  (`--p-max-age`), and the keeper restarts it after a "stale" stop.
- If the grace runs out, the reason still contains "interlocks", so the
  keeper and `--ride-interlock-disarms` treat it as before.

### Deploy

Done at 11:21 EDT (entry above). Step 2's `git fetch origin` fails on the Pi;
push straight to the Pi's repo instead, as described there.

Do this between experiments: restarting aicontrol disarms all three groups.

1. Push the branch, from the DAQ PC (HTTPS works):
   ```bash
   git -C C:/Users/xbox/Desktop/xsphere-slow-control push origin claude/heater-arm-esp32-grace
   ```
2. On the Pi:
   ```bash
   cd ~/xsphere-slow-control
   git status        # expect claude/pid-windup-fix at b51f193, clean
   git fetch origin
   git checkout claude/heater-arm-esp32-grace
   ~/miniconda3/envs/slowcontrol/bin/python -c "import aicontrol.app"
   sudo systemctl restart xsphere-aicontrol
   journalctl -u xsphere-aicontrol -n 30 | grep -i "level-stale grace"   # "Level-stale grace 120 s"
   ```
3. Re-arm on the Control page. Do not restart `xsphere-slowcontrol` or
   `xsphere-webcontrol`: their code is unchanged.
4. Check that `get_ai_control` shows `"level_stale_grace_s": 120` and
   `"heater_hold": null`.

**Rollback:** run `git checkout claude/pid-windup-fix`, then restart
xsphere-aicontrol. For the old behaviour without a rollback, set the grace to
0 with the drop-in in `aicontrol/README.md`, "Level-stale grace".

### Open questions for Lucas

1. **Writes resume by themselves.** After a short dropout, the AI's heater
   writes resume when the interlocks clear, with no re-arm. That is the one
   policy change. The arm itself is never given back without you. Is this OK?
   If not, set the grace to 0.
2. **The value, 120 s.** The table in `docs/level-stale-grace.md` covers it.
   90 s would do once the ESP32 firmware is flashed.
3. **On by default.** I turned the grace on so that deploying the branch fixes
   the problem; on 10-29 it was opt-in. Would you rather opt in with the
   drop-in?
4. **An arm made during a hold** is dropped when the grace runs out, like
   every other arm. Is this OK?
5. **Edge-only disarms.** This was true before and I did not change it: the
   supervisor disarms only on an ok → not-ok edge. After a disarm, while the
   interlocks stay not-ok, a re-arm is not dropped when a new rule (say
   `temperature_stale`) joins. Should a new rule disarm too?
6. **The source.** The dropouts themselves are still there. The firmware
   (`gas-handling-system` `claude/esp32-link-watchdog`) restarts a stuck link
   after 60 s, but it is not built or flashed (`pio run` first). The hardware
   suspects are in `docs/ghs-esp32-dropouts.md` on
   `claude/ghs-esp32-dropout-note`.

### Follow-ups, not done here

- **xsphere-daq.** `tree_thermo.py`, `thermo_keeper.py` and `hold_watch.py`
  could read `heater_hold` and report "held" instead of inferring it. For
  gaps under about 3 min, `--ride-interlock-disarms` is no longer needed.
- **Control page.** It could colour `heater_hold` audit entries. That needs a
  webcontrol restart. Until then they show in the default colour.
- **Telegraf.** Nothing records `xsphere/status/ghs_esp32` yet. It needs a
  Telegraf input (from the 10-29 note).
