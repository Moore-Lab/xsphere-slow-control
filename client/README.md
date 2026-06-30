# xsphere remote control panel — client-side build brief

This directory is meant to be **copied to a lab client computer** (Windows,
macOS, or Linux) and handed to a fresh Claude Code session. It contains
everything that session needs to know to build:

1. A small **Python GUI** that talks to the slow-control Pi over SSH + HTTP
   and shows live status of the services and the cryostat.
2. **Start / Stop / Restart** buttons for the slow-control + web-control
   services.
3. A **desktop shortcut / launcher** that opens the GUI with one click.
4. A button on the GUI that opens the web app in the user's default browser.

The Python script is the *client* of an already-running *server* — closing
the GUI window must NOT touch the services running on the Pi.

---

## TL;DR for a fresh Claude on the client machine

> *Read the entire file before writing code.* The contract with the server
> (sudoers permissions, HTTP endpoints, MQTT topics) is fixed at the
> server side and described below; you are building only the client.
> When in doubt, prefer the simplest cross-platform standard-library
> approach (tkinter + subprocess + urllib). No extra pip dependencies.

The deliverables are:

- `xsphere_panel.py` — a single-file Python 3 GUI.
- A platform-appropriate desktop launcher (`.lnk` shortcut on Windows,
  `.desktop` file on Linux, `.command` on macOS).
- Brief setup notes for the operator (SSH key copy, icon location).

---

## What is already set up on the server (the Pi)

You do not need to change anything on the Pi. These are *guarantees* the
client can rely on:

### 1. Hostname / IP

- Pi hostname: `xbox-pi` (resolves on the lab subnet)
- IP: `192.168.8.116`
- SSH user: `xbox`

### 2. Passwordless `sudo systemctl …` over SSH

A `/etc/sudoers.d/xsphere-restart` file allows `xbox` to run, without a
password prompt, **exactly** these commands and nothing else:

```
sudo systemctl start    xsphere-slowcontrol
sudo systemctl stop     xsphere-slowcontrol
sudo systemctl restart  xsphere-slowcontrol
sudo systemctl status   xsphere-slowcontrol
sudo systemctl is-active xsphere-slowcontrol
sudo systemctl is-failed xsphere-slowcontrol
sudo systemctl reset-failed xsphere-slowcontrol

sudo systemctl start    xsphere-webcontrol
sudo systemctl stop     xsphere-webcontrol
sudo systemctl restart  xsphere-webcontrol
sudo systemctl status   xsphere-webcontrol
sudo systemctl is-active xsphere-webcontrol
sudo systemctl is-failed xsphere-webcontrol
sudo systemctl reset-failed xsphere-webcontrol
```

`is-active` / `is-failed` do not actually need `sudo` (they're read-only),
but the entry covers them so the GUI can call them via the same path
without an extra branch.

`reset-failed` is included because the slow-control's systemd unit caps
restarts at 5 in 10 minutes — past that the unit goes to `failed` state
and `restart` alone is a no-op until the failure counter is cleared.
**A "Restart" click in the GUI should always do `reset-failed && restart`
in that order**, so the operator never has to think about the cap.

### 3. HTTP endpoint for live state

The web-control service serves a JSON snapshot of the entire system at:

```
http://192.168.8.116:8088/api/state
```

Response shape (subset relevant to a status panel):

```json
{
  "now": 1781234567.89,
  "snapshot": {
    "generated_at": 1781234567.10,
    "counts": {"fresh": 74, "invalid": 11, "stale": 0},
    "states": {
      "service_alive":         {"value": true,  "freshness": "fresh", "age_s": 1.2},
      "labjack_connected":     {"value": true,  "freshness": "fresh"},
      "ghs_esp32_alive":       {"value": true,  "freshness": "fresh"},
      "interlocks_ok":         {"value": true,  "freshness": "fresh"},
      "pv_interlock_tripped":  {"value": false, "freshness": "fresh"},
      "pv_interlock_min_k":    {"value": 77.0,  "freshness": "fresh", "unit": "K"},
      "pv_interlock_max_k":    {"value": 310.0, "freshness": "fresh", "unit": "K"},
      "t_cube_top":            {"value": 207.5, "freshness": "fresh", "unit": "K"},
      "t_cube_bottom":         {"value": 209.1, "freshness": "fresh", "unit": "K"},
      "t_cube_nozzle":         {"value": 215.3, "freshness": "fresh", "unit": "K"},
      "pid_top_output":        {"value": 0.0,   "freshness": "fresh", "unit": "%"},
      "pid_bottom_output":     {"value": 0.0,   "freshness": "fresh", "unit": "%"},
      "pid_nozzle_output":     {"value": 0.0,   "freshness": "fresh", "unit": "%"}
    }
  },
  "snapshot_age": 0.6,
  "mqtt_connected": true
}
```

The `snapshot` field is **null** if the web-control hasn't received a snapshot
from the slow-control yet (i.e. slow-control is down). `snapshot_age` lets
you tell "data is fresh" from "we have stale data". A reasonable rule:

- `snapshot_age` < 5 s → **healthy**
- 5 s ≤ `snapshot_age` < 60 s → **degraded**
- `snapshot_age` ≥ 60 s OR snapshot is null → **stale / down**

All numeric values in the `states` block are already in their displayed
unit (`unit` field tells you which). Use `freshness` ∈ {`fresh`, `stale`,
`invalid`} to colour individual rows.

### 4. Web GUI URLs

The "Open Web GUI" button should launch the operator's default browser
to:

- `http://192.168.8.116:8088/control` — the main control page (valves,
  PID, interlock band)

Other tabs the operator may want (don't need their own buttons; the
control page links to them):

- `http://192.168.8.116:8088/` — register view (all states, read-only)
- `http://192.168.8.116:8088/sequencer` — automation sequencer
- `http://192.168.8.116:8088/readme` — repo README rendered in-browser

---

## One-time client-side setup the operator must do before the GUI works

The Claude session writing the GUI should *also* generate clear setup
instructions for these steps.

### A. Install OpenSSH client

- **Windows 10/11**: built in. Verify with `ssh -V` in PowerShell.
- **macOS**: built in.
- **Linux**: `apt install openssh-client` or equivalent.

### B. SSH key auth (no passwords)

On the client machine:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/xsphere_panel
ssh-copy-id -i ~/.ssh/xsphere_panel.pub xbox@192.168.8.116
```

(Windows PowerShell equivalent: `ssh-keygen` + manually append the public
key to `xbox@192.168.8.116:~/.ssh/authorized_keys`. The Windows OpenSSH
client uses keys at `%USERPROFILE%\.ssh\`.)

Confirm:

```bash
ssh -i ~/.ssh/xsphere_panel xbox@192.168.8.116 sudo systemctl is-active xsphere-slowcontrol
```

should print `active` and exit, **without prompting for anything**. If it
prompts, the key isn't installed; the GUI won't work unattended.

### C. Python 3

Any Python ≥ 3.9 with tkinter is fine. tkinter ships with the official
Python installers on Windows and macOS; on Linux it may be a separate
package (`python3-tk`).

---

## What to build

A single-file `xsphere_panel.py` that opens a tkinter window and:

### Layout

```
┌─────────────────────────────────────────────────────────┐
│ xsphere slow control — remote panel                     │
├─────────────────────────────────────────────────────────┤
│ Connection: ● connected   xbox@192.168.8.116            │
│ Snapshot:   ● fresh (age 0.6 s)                         │
├─────────────────────────────────────────────────────────┤
│ slow-control                            ● active        │
│   [Start]  [Stop]  [Restart]                            │
│ web-control                             ● active        │
│   [Start]  [Stop]  [Restart]                            │
├─────────────────────────────────────────────────────────┤
│ System health                                           │
│   PV interlock: ● OK   band [77, 310] K                 │
│   LabJack:      ● connected                             │
│   GHS ESP32:    ● alive                                 │
│   Interlocks:   ● OK                                    │
│   States:       74 fresh / 11 invalid / 0 stale         │
├─────────────────────────────────────────────────────────┤
│ Cube temperatures                                       │
│   top:    207.5 K     bottom: 209.1 K                   │
│   nozzle: 215.3 K                                       │
├─────────────────────────────────────────────────────────┤
│ Heater outputs                                          │
│   top:    0.0 %       bottom: 0.0 %    nozzle: 0.0 %    │
├─────────────────────────────────────────────────────────┤
│   [Open Web GUI]   [Refresh now]   [About]              │
└─────────────────────────────────────────────────────────┘
```

The exact text and layout can be adjusted; the **information density** is
what matters. Use coloured circles or coloured text:
green = fresh/active/ok, yellow = stale/degraded, red = invalid/down/tripped.

### Behaviour

- **Auto-refresh** every 5 s by polling `http://192.168.8.116:8088/api/state`.
- **Service status** comes from
  `ssh ... sudo systemctl is-active xsphere-slowcontrol`
  (and the same for `xsphere-webcontrol`) — refresh on the same 5 s tick.
  These return `active`, `inactive`, `failed`, or `activating` to stdout.
- **Connection status** is just "can we reach `http://…/api/state` in
  under 2 s" + "does `ssh ... echo ok` succeed in under 3 s". Don't keep
  retrying inside one tick; if a call times out, mark the row red and
  move on.
- **Buttons** invoke the matching SSH command in a background thread so
  the GUI doesn't freeze. After the command returns, force an immediate
  refresh so the operator sees the new state without waiting for the
  next 5 s tick.
- **The Restart button must run `reset-failed` first**, then `restart`.
  Without that, restarts past the systemd `StartLimitBurst` cap silently
  no-op.
- **Closing the window** quits the GUI but does NOT touch the services.
  This is trivially true because the GUI never holds an SSH connection
  open — it spawns a fresh `ssh` for each action.
- **No password prompts ever.** If the operator sees one, the SSH key
  isn't set up; the GUI should detect that case (subprocess exit code
  255 + stderr "Permission denied" or "publickey") and show a clear
  message rather than just looking broken.

### Implementation hints

- `subprocess.run([... , "ssh", "-i", KEY_PATH, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=3", "xbox@192.168.8.116", REMOTE_CMD], capture_output=True, text=True, timeout=10)`
- HTTP: `urllib.request.urlopen("http://192.168.8.116:8088/api/state", timeout=2).read()` + `json.loads(...)`.
- Threading: `threading.Thread(target=..., daemon=True).start()` for each
  button-action and for the periodic refresh; never call a blocking SSH
  / HTTP from the tkinter mainloop directly.
- Marshal results back to the UI thread with `widget.after(0, ...)`.
- For the "Open Web GUI" button: `webbrowser.open("http://192.168.8.116:8088/control")`.
- For the SSH `KEY_PATH`, default to `~/.ssh/xsphere_panel` (the key
  created in step B above) but let the user override via the
  `XSPHERE_SSH_KEY` env var.

### Things to make robust

- A first run where the SSH key isn't set up yet should show a clear
  red banner like "SSH unreachable — set up your key first (see README)"
  instead of just blank rows.
- A first run where `xsphere-slowcontrol` is stopped or has never
  produced a snapshot should still load the window — the System Health
  section just shows "no data" and the action buttons still work.
- Network blip: a single failed poll shouldn't flip every row red.
  Consider "two consecutive failures before showing red".

---

## Desktop shortcut

After the GUI is working, generate the right shortcut for the operator's
OS. Detect the OS at script time and have the Claude session ask which
to generate (or generate all three and let the operator pick).

### Windows (`.lnk` shortcut on Desktop)

A `.lnk` is binary; the standard way to generate one from Python is via
COM (`pywin32`) or PowerShell. PowerShell is cleaner because there's no
extra dependency:

```powershell
$WshShell  = New-Object -ComObject WScript.Shell
$Shortcut  = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\xsphere panel.lnk")
$Shortcut.TargetPath = "pythonw.exe"          # pythonw = no console window
$Shortcut.Arguments  = "`"$PSScriptRoot\xsphere_panel.py`""
$Shortcut.IconLocation = "$PSScriptRoot\xsphere.ico"
$Shortcut.Save()
```

(Run once during setup. The script `xsphere_panel.py` lives wherever the
operator unzipped this folder; the shortcut points at it.)

If `pywin32` is available, the same in Python:

```python
import win32com.client
shell = win32com.client.Dispatch("WScript.Shell")
sc = shell.CreateShortcut(os.path.expanduser("~/Desktop/xsphere panel.lnk"))
sc.TargetPath = sys.executable.replace("python.exe", "pythonw.exe")
sc.Arguments  = f'"{os.path.abspath("xsphere_panel.py")}"'
sc.IconLocation = os.path.abspath("xsphere.ico")
sc.Save()
```

### Linux (`.desktop` file)

```ini
[Desktop Entry]
Type=Application
Name=xsphere panel
Comment=Control the xsphere slow-control service on xbox-pi
Exec=python3 /full/path/to/xsphere_panel.py
Icon=/full/path/to/xsphere.png
Terminal=false
Categories=Utility;Monitor;
```

Place at `~/Desktop/xsphere-panel.desktop` and `chmod +x` it. On most
Linux desktops the user has to right-click → "Allow Launching" the
first time.

### macOS (`.command` script on Desktop)

A `.command` file is just a shell script with the executable bit set;
double-clicking it opens Terminal and runs it. To launch a GUI without
keeping a Terminal window in the foreground:

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")"
exec python3 xsphere_panel.py
```

`chmod +x ~/Desktop/xsphere-panel.command`.

For a "real" .app bundle, use `py2app` — overkill for this purpose.

### Icon

The repo's web GUI uses `webcontrol/static/favicon.svg` (an icy blue
disc with the Xe symbol). For a desktop launcher you want a raster
icon at multiple sizes; convert with:

```bash
# Linux/macOS — produces .ico with multiple sizes from the SVG
inkscape favicon.svg -o icon-256.png -w 256 -h 256
convert icon-256.png \( -clone 0 -resize 16x16 \) \
                    \( -clone 0 -resize 32x32 \) \
                    \( -clone 0 -resize 48x48 \) \
                    \( -clone 0 -resize 64x64 \) \
                    \( -clone 0 -resize 128x128 \) \
                    -delete 0 xsphere.ico
```

The SVG is included in this folder as `favicon.svg`; if Inkscape/
ImageMagick aren't available on the client, any 256×256 PNG of a
cryostat / snowflake / Xe glyph is fine. Or skip the icon entirely
and accept the default Python rocket.

---

## Where to put the shortcut on the operator's machine

The natural place is the desktop. For lab use, also consider:

- A taskbar/dock pin so it's one click instead of two.
- A start-menu entry / Applications folder entry.

The shortcut should point at `xsphere_panel.py` wherever the operator
unzipped this folder, **not** at a copy in a system location — that way
re-pulling the repo updates the GUI automatically.

---

## Quick smoke test the GUI should pass

After the operator follows the setup steps, opening the GUI should:

1. Show the slow-control row as **green / active** within ~2 seconds.
2. Show the web-control row as **green / active** within ~2 seconds.
3. Show all "System health" rows populated within ~5 seconds.
4. Cube temperatures match what the web GUI shows at
   `http://192.168.8.116:8088/control` (cross-check).
5. Click **Restart** on slow-control → row goes yellow (activating) →
   green (active) within ~10 seconds, with no operator password prompt
   anywhere.
6. Click **Open Web GUI** → default browser opens the control page.
7. Close the GUI window → services keep running (verify by reopening).

If any of the above fail, fix before declaring done.

---

## Optional polish (do only if the basics are solid)

- A "View log" button that runs
  `ssh ... journalctl -u xsphere-slowcontrol -n 50 --no-pager`
  in the background and displays the output in a scrolling read-only
  text widget.
- A small graph of `pid_*_output` over the last 5 min — but this is
  what the web GUI is for; don't reimplement Grafana in tkinter.
- An "Open Grafana" button that points at the dashboard:
  `http://192.168.8.116:3000/d/xsphere-slowcontrol`.
- Reset-failed-only button for when restart still fails (rare).

---

## Files in this folder

| File | Purpose |
|---|---|
| `README.md` | this file — the brief Claude reads |
| `favicon.svg` | (optional) icon source from the web GUI |

The Claude session should add `xsphere_panel.py` plus the OS-appropriate
launcher file (`.lnk` / `.desktop` / `.command`) and possibly a
generated `xsphere.ico` / `xsphere.png`.
