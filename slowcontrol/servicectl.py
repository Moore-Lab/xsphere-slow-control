#!/usr/bin/env python3
"""
Start / stop / restart the xsphere slow control systemd services.

The services run on xbox-pi as system units owned by user `xbox`, so every
control action needs `sudo systemctl`.  This module drives them either
locally (when run on the Pi) or over SSH (when run from the Windows DAQ
machine), and provides both a CLI and an embeddable Tk panel.

    python -m slowcontrol.servicectl                  # GUI
    python -m slowcontrol.servicectl status           # CLI, all units
    python -m slowcontrol.servicectl restart slowcontrol
    python -m slowcontrol.servicectl logs webcontrol -n 100

Connection settings are remembered in ~/.xsphere/servicectl.json and can be
overridden per invocation with --host / --user / --local.

Remote control prerequisites
────────────────────────────
  1. Key-based SSH from this machine to the Pi.  Password prompts cannot be
     answered from a GUI, so SSH runs with BatchMode=yes and fails fast with
     a clear message rather than hanging:
         ssh-keygen -t ed25519
         ssh-copy-id xbox@192.168.8.116
  2. `sudo -n systemctl ...` must work for the SSH user on the Pi without a
     password.  webcontrol already relies on exactly that to stop and start
     the slow control service for its /diag page, and this module issues the
     same command line it does.

Stopping the slow control service is not inert
──────────────────────────────────────────────
The heater PID loops run in the PLC, but it is this service that feeds them:
it mirrors the LabJack temperatures into the PLC every poll, evaluates the
setpoint / PV expressions, and enforces the PV safety interlock.  While it is
stopped the PLC keeps regulating on the last values written, with nothing
watching them — so the GUI asks for confirmation before stopping.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

#: short name → (systemd unit, human label)
#
# Bare unit names, with no ".service" suffix, on purpose: sudo can match a
# command line literally, and webcontrol/app.py already runs
# `sudo -n systemctl stop xsphere-slowcontrol` on the Pi.  Spelling the unit
# the same way means whatever sudo rule lets that through lets this through.
UNITS: Dict[str, tuple] = {
    "slowcontrol": ("xsphere-slowcontrol", "Slow Control Service"),
    "webcontrol":  ("xsphere-webcontrol",  "Web Control Panel"),
}

ACTIONS = ("start", "stop", "restart", "enable", "disable")

#: Extra warning shown before stopping a unit. Only the slow control service
#: has hardware consequences; saying the same thing about the web panel would
#: be false and would teach the operator to click through the dialog.
STOP_WARNINGS: Dict[str, str] = {
    "slowcontrol":
        "The heater PID loops keep running in the PLC, but this service is "
        "what feeds them: while it is stopped nothing updates the "
        "temperatures they regulate on, and the PV safety interlock is not "
        "enforced. The PLC acts on the last values written.\n\n"
        "Use Restart if you only need it to pick up a change. If it has to "
        "stay stopped, put the heaters in a safe state first.",
    "webcontrol":
        "The browser control panel on port 8088 will be unreachable until it "
        "is started again, and any follow or ramp loop it is running stops. "
        "The slow control service and the PLC are not affected.",
}

DEFAULT_HOST = "192.168.8.116"      # xbox-pi
DEFAULT_USER = "xbox"

SETTINGS_PATH = Path.home() / ".xsphere" / "servicectl.json"

# Suppress the console window that would otherwise flash on every call on
# Windows when launched from pythonw.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# Connection target
# ---------------------------------------------------------------------------

@dataclass
class Target:
    """Where the systemd units live."""
    host: str = DEFAULT_HOST
    user: str = DEFAULT_USER
    local: bool = False          # True → run systemctl on this machine
    ssh_key: str = ""            # optional explicit identity file
    timeout_s: float = 20.0

    @classmethod
    def load(cls) -> "Target":
        try:
            with open(SETTINGS_PATH) as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_PATH, "w") as fh:
            json.dump(asdict(self), fh, indent=2)


@dataclass
class Result:
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    command: str = ""

    @property
    def text(self) -> str:
        out = (self.stdout or "").rstrip()
        err = (self.stderr or "").rstrip()
        if out and err:
            return f"{out}\n{err}"
        return out or err


@dataclass
class UnitStatus:
    key: str
    unit: str
    label: str
    active: str = "unknown"      # active | inactive | failed | activating | ...
    sub: str = ""                # running | dead | exited | ...
    enabled: str = ""            # enabled | disabled | static | ...
    since: str = ""
    error: str = ""

    @property
    def is_running(self) -> bool:
        return self.active == "active"

    @property
    def is_failed(self) -> bool:
        return self.active == "failed" or bool(self.error)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class ServiceController:
    """Runs systemctl / journalctl against the target, locally or over SSH."""

    def __init__(self, target: Optional[Target] = None):
        self.target = target or Target()

    # -- command construction ------------------------------------------

    def _wrap(self, argv: List[str]) -> List[str]:
        """Prefix argv with an SSH invocation unless we are running locally."""
        if self.target.local:
            return argv
        ssh = ["ssh", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"ConnectTimeout={int(max(5, self.target.timeout_s // 2))}"]
        if self.target.ssh_key:
            ssh += ["-i", self.target.ssh_key]
        ssh.append(f"{self.target.user}@{self.target.host}")
        return ssh + argv

    def _run(self, argv: List[str]) -> Result:
        cmd = self._wrap(argv)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.target.timeout_s,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError:
            exe = cmd[0]
            hint = ("OpenSSH client not found. On Windows 11 install it with:\n"
                    "  Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
                    ) if exe == "ssh" else f"{exe} not found on PATH."
            return Result(False, 127, stderr=hint, command=" ".join(cmd))
        except subprocess.TimeoutExpired:
            return Result(False, 124,
                          stderr=f"Timed out after {self.target.timeout_s:.0f}s. "
                                 f"Is {self.target.host} reachable?",
                          command=" ".join(cmd))

        res = Result(proc.returncode == 0, proc.returncode,
                     proc.stdout, proc.stderr, " ".join(cmd))
        if not res.ok:
            res.stderr = (res.stderr or "") + self._diagnose(res)
        return res

    def _diagnose(self, res: Result) -> str:
        """Turn the usual opaque failures into something actionable."""
        blob = f"{res.stdout}\n{res.stderr}".lower()
        if "permission denied (publickey" in blob or "no supported authentication" in blob:
            dest = f"{self.target.user}@{self.target.host}"
            # Windows has no ssh-copy-id outside Git Bash; this form works in
            # PowerShell and Git Bash alike.
            copy = (f'  cat ~/.ssh/id_ed25519.pub | ssh {dest} "mkdir -p ~/.ssh '
                    '&& cat >> ~/.ssh/authorized_keys '
                    '&& chmod 600 ~/.ssh/authorized_keys"'
                    if os.name == "nt" else f"  ssh-copy-id {dest}")
            return ("\n\nSSH key authentication failed — this machine's public "
                    f"key is not authorised on {self.target.host}. In a "
                    "terminal, once:\n"
                    "  ssh-keygen -t ed25519      (skip if ~/.ssh/id_ed25519 "
                    "already exists)\n"
                    f"{copy}\n"
                    "Password login cannot be used here — the GUI has no way to "
                    "answer a prompt, so BatchMode is on deliberately.")
        if "sudo: a password is required" in blob or "sudo: no tty" in blob:
            return ("\n\nsudo asked for a password. `sudo -n systemctl` has "
                    f"to work for {self.target.user} on the Pi; reading "
                    "status does not need it.")
        if "could not resolve hostname" in blob or "no route to host" in blob:
            return (f"\n\nCannot reach {self.target.host}. Check the network / VPN, "
                    "and that the address in Settings is current.")
        return ""

    # -- actions --------------------------------------------------------

    def action(self, action: str, key: str) -> Result:
        if action not in ACTIONS:
            return Result(False, 2, stderr=f"Unknown action {action!r}")
        if key not in UNITS:
            return Result(False, 2, stderr=f"Unknown service {key!r}")
        unit = UNITS[key][0]
        # sudo -n: never prompt. A prompt would hang forever behind the GUI.
        return self._run(["sudo", "-n", "systemctl", action, unit])

    def status(self, key: str) -> UnitStatus:
        unit, label = UNITS[key]
        st = UnitStatus(key=key, unit=unit, label=label)
        # One machine-readable call rather than parsing `systemctl status`.
        # This needs no sudo, so status works even before sudoers is set up.
        res = self._run([
            "systemctl", "show", unit, "--no-pager",
            "-p", "ActiveState", "-p", "SubState",
            "-p", "UnitFileState", "-p", "ActiveEnterTimestamp",
        ])
        if not res.ok and not res.stdout:
            st.active = "unreachable"
            st.error = res.text
            return st
        for line in res.stdout.splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if   k == "ActiveState":          st.active  = v.strip()
            elif k == "SubState":             st.sub     = v.strip()
            elif k == "UnitFileState":        st.enabled = v.strip()
            elif k == "ActiveEnterTimestamp": st.since   = v.strip()
        if not st.enabled:
            # systemd reports an empty UnitFileState for a unit it has never
            # seen — that means the unit file was never installed.
            st.error = f"{unit} is not installed on {self.target.host}"
            st.active = "missing"
        return st

    def status_all(self) -> List[UnitStatus]:
        return [self.status(k) for k in UNITS]

    def status_text(self, keys: List[str], lines: int = 8) -> Result:
        """The human-readable `systemctl status` block for one or more units.

        Needs no sudo.  systemctl exits 3 when a unit is not running, so a
        non-zero return code here is not by itself a failed query — the block
        is on stdout either way, and that is what counts.
        """
        res = self._run(["systemctl", "status", *(UNITS[k][0] for k in keys),
                         "--no-pager", "--full", "-n", str(lines)])
        if res.stdout.strip():
            res.ok = True
        return res

    def logs(self, key: str, lines: int = 200) -> Result:
        unit = UNITS[key][0]
        # No sudo: on Raspberry Pi OS the default user is in the `adm` group
        # and can already read the journal. If yours cannot, add the user to
        # the systemd-journal group.
        return self._run(["journalctl", "-u", unit, "-n", str(lines),
                          "--no-pager"])


# ---------------------------------------------------------------------------
# Tk panel
# ---------------------------------------------------------------------------

def _build_panel(parent, controller: "ServiceController", **kw):
    """Construct the service control panel. Imports Tk lazily so the
    controller stays usable on a headless Pi."""
    import queue
    import threading
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    class ServicePanel(ttk.Frame):
        POLL_MS = 5000          # status refresh interval
        # ... and the interval while the target cannot be reached at all.
        # Every poll is a fresh SSH login, so polling a host that is refusing
        # our key at the normal rate is a steady stream of failed logins.
        POLL_UNREACHABLE_MS = 30000

        def __init__(self, master, ctrl: ServiceController, **kw):
            super().__init__(master, **kw)
            self._ctrl = ctrl
            self._q: "queue.Queue" = queue.Queue()
            self._rows: Dict[str, dict] = {}
            self._busy = 0
            self._auto_job = None
            self._closing = False
            self._unreachable = False

            self._build()
            self.after(100, self._drain)
            self.refresh()
            # Open with the full status block already in the output pane, so
            # the window answers "is it running?" without a click.
            self._status()
            self._reschedule()

        # -- layout ----------------------------------------------------

        def _build(self) -> None:
            conn = ttk.LabelFrame(self, text="Connection", padding=8)
            conn.pack(fill="x", padx=10, pady=(10, 4))

            self._v_local = tk.BooleanVar(value=self._ctrl.target.local)
            self._v_host  = tk.StringVar(value=self._ctrl.target.host)
            self._v_user  = tk.StringVar(value=self._ctrl.target.user)

            ttk.Checkbutton(conn, text="Run locally (this machine IS the Pi)",
                            variable=self._v_local,
                            command=self._on_local_toggle).grid(
                row=0, column=0, columnspan=6, sticky="w", pady=(0, 4))

            ttk.Label(conn, text="Host:").grid(row=1, column=0, sticky="e", padx=(0, 4))
            self._e_host = ttk.Entry(conn, textvariable=self._v_host, width=18)
            self._e_host.grid(row=1, column=1, sticky="w")

            ttk.Label(conn, text="User:").grid(row=1, column=2, sticky="e", padx=(12, 4))
            self._e_user = ttk.Entry(conn, textvariable=self._v_user, width=12)
            self._e_user.grid(row=1, column=3, sticky="w")

            ttk.Button(conn, text="Save", width=8,
                       command=self._save_settings).grid(row=1, column=4, padx=(12, 4))
            ttk.Button(conn, text="Test", width=8,
                       command=self._test).grid(row=1, column=5)

            # -- one row per unit --------------------------------------
            for key, (unit, label) in UNITS.items():
                box = ttk.LabelFrame(self, text=label, padding=8)
                box.pack(fill="x", padx=10, pady=4)

                dot = ttk.Label(box, text="●", foreground="gray",
                                font=("TkDefaultFont", 15))
                dot.grid(row=0, column=0, padx=(0, 6))

                state = tk.StringVar(value="checking…")
                # wraplength a little under the 42-character width, so a long
                # line (an SSH error, mostly) wraps instead of being cut off.
                ttk.Label(box, textvariable=state, width=42, anchor="w",
                          wraplength=330, justify="left",
                          font=("Consolas", 10)).grid(row=0, column=1, sticky="w")

                btns = ttk.Frame(box)
                btns.grid(row=0, column=2, sticky="e", padx=(10, 0))
                box.columnconfigure(2, weight=1)

                ttk.Button(btns, text="Start", width=9,
                           command=lambda k=key: self._act("start", k)
                           ).pack(side="left", padx=2)
                ttk.Button(btns, text="Restart", width=9,
                           command=lambda k=key: self._act("restart", k)
                           ).pack(side="left", padx=2)
                ttk.Button(btns, text="Stop", width=9,
                           command=lambda k=key: self._act("stop", k)
                           ).pack(side="left", padx=2)
                ttk.Button(btns, text="Logs", width=9,
                           command=lambda k=key: self._logs(k)
                           ).pack(side="left", padx=(10, 2))

                self._rows[key] = {"dot": dot, "state": state, "unit": unit}

            # -- global controls ---------------------------------------
            bar = ttk.Frame(self)
            bar.pack(fill="x", padx=10, pady=(6, 4))
            ttk.Button(bar, text="Restart Both",
                       command=self._restart_all).pack(side="left")
            ttk.Button(bar, text="Refresh",
                       command=self.refresh).pack(side="left", padx=6)
            ttk.Button(bar, text="Print Status",
                       command=self._status).pack(side="left")

            self._v_auto = tk.BooleanVar(value=True)
            ttk.Checkbutton(bar, text="Auto-refresh", variable=self._v_auto,
                            command=self._reschedule).pack(side="left", padx=12)

            self._v_busy = tk.StringVar(value="")
            ttk.Label(bar, textvariable=self._v_busy,
                      foreground="#1d6fa5").pack(side="right")

            # -- output ------------------------------------------------
            outf = ttk.LabelFrame(self, text="Output", padding=4)
            outf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
            self._out = scrolledtext.ScrolledText(outf, font=("Consolas", 9),
                                                  state="disabled", wrap="word",
                                                  height=14)
            self._out.pack(fill="both", expand=True)

            self._on_local_toggle()

        # -- helpers ---------------------------------------------------

        def _on_local_toggle(self) -> None:
            state = "disabled" if self._v_local.get() else "normal"
            self._e_host.configure(state=state)
            self._e_user.configure(state=state)
            self._apply_settings()

        def _apply_settings(self) -> Target:
            """Fold the entry fields into a NEW Target and return it.

            A fresh object rather than an in-place mutation: worker threads
            read the target while they run, so editing the one they hold would
            change the host mid-command.
            """
            t = replace(
                self._ctrl.target,
                local=self._v_local.get(),
                host=self._v_host.get().strip() or DEFAULT_HOST,
                user=self._v_user.get().strip() or DEFAULT_USER,
            )
            self._ctrl.target = t
            return t

        def _save_settings(self) -> None:
            self._apply_settings()
            try:
                self._ctrl.target.save()
                self._log(f"Settings saved to {SETTINGS_PATH}")
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc))

        def _log(self, msg: str) -> None:
            self._out.configure(state="normal")
            self._out.insert("end", msg.rstrip() + "\n")
            self._out.see("end")
            self._out.configure(state="disabled")

        def _submit(self, fn, tag: str, note: str = "") -> None:
            """Run a controller call on a worker thread.

            `fn` is handed its own ServiceController bound to a snapshot of the
            settings as they are right now, so editing Host/User while a
            command is in flight cannot redirect it mid-run.
            """
            snapshot = ServiceController(self._apply_settings())
            self._busy += 1
            if note:
                self._v_busy.set(note)

            def _work():
                # Always put something back, or _busy never unwinds and the
                # panel is stuck showing "restarting…" forever.
                try:
                    self._q.put((tag, fn(snapshot)))
                except Exception as exc:
                    self._q.put(("error", Result(False, 1, stderr=repr(exc))))

            threading.Thread(target=_work, daemon=True).start()

        # -- actions ---------------------------------------------------

        def _act(self, action: str, key: str) -> None:
            label = UNITS[key][1]
            if action == "stop":
                warning = STOP_WARNINGS.get(key, "")
                body = f"Stop {label}?"
                if warning:
                    body += f"\n\n{warning}"
                body += "\n\nContinue?"
                if not messagebox.askyesno(f"Stop {label}?", body,
                                           icon="warning"):
                    return
            self._log(f"$ sudo systemctl {action} {UNITS[key][0]}")
            self._submit(lambda c: (key, c.action(action, key)),
                         "action", f"{action}ing {label}…")

        def _restart_all(self) -> None:
            for key in UNITS:
                self._log(f"$ sudo systemctl restart {UNITS[key][0]}")
                self._submit(lambda c, k=key: (k, c.action("restart", k)),
                             "action", "restarting both…")

        def _logs(self, key: str) -> None:
            self._log(f"$ journalctl -u {UNITS[key][0]} -n 200")
            self._submit(lambda c: c.logs(key), "logs",
                         f"fetching {UNITS[key][1]} logs…")

        def _status(self, keys: Optional[List[str]] = None) -> None:
            """Print the full `systemctl status` block to the output pane."""
            keys = list(keys or UNITS)
            self._log("$ systemctl status "
                      + " ".join(UNITS[k][0] for k in keys))
            self._submit(lambda c: c.status_text(keys), "status_text",
                         "fetching status…")

        def _after_action(self, key: str) -> None:
            if self._closing:
                return
            self.refresh()
            self._status([key])

        def _test(self) -> None:
            self._log("Testing connection…")
            # systemctl --version needs no sudo and exists wherever the units
            # do, so this checks reachability without touching anything.
            self._submit(lambda c: c._run(["systemctl", "--version"]),
                         "test", "testing…")

        def refresh(self) -> None:
            self._submit(lambda c: c.status_all(), "status")

        def _reschedule(self) -> None:
            if self._auto_job is not None:
                self.after_cancel(self._auto_job)
                self._auto_job = None
            if self._v_auto.get() and not self._closing:
                delay = (self.POLL_UNREACHABLE_MS if self._unreachable
                         else self.POLL_MS)
                self._auto_job = self.after(delay, self._tick)

        def _tick(self) -> None:
            self._auto_job = None
            if self._busy == 0:
                self.refresh()
            self._reschedule()

        # -- result pump ----------------------------------------------

        def _drain(self) -> None:
            try:
                while True:
                    tag, payload = self._q.get_nowait()
                    self._busy = max(0, self._busy - 1)
                    if self._busy == 0:
                        self._v_busy.set("")
                    self._handle(tag, payload)
            except queue.Empty:
                pass
            if not self._closing:
                self.after(150, self._drain)

        def _handle(self, tag: str, payload) -> None:
            if tag == "error":
                self._log(f"Internal error: {payload.text}")
                return

            if tag == "status":
                for st in payload:
                    self._render(st)
                unreachable = all(st.active == "unreachable"
                                  for st in payload)
                if unreachable != self._unreachable:
                    self._unreachable = unreachable
                    self._reschedule()
                return

            if tag == "test":
                self._log("Connection OK." if payload.ok
                          else f"Connection FAILED:\n{payload.text}")
                return

            if tag in ("logs", "status_text"):
                self._log(payload.text or "(no output)")
                return

            # action
            key, res = payload
            if not res.ok:
                self._log(f"FAILED (exit {res.returncode})\n{res.text}")
                self.after(1200, self.refresh)
                return
            self._log(res.text or "OK")
            # systemd needs a moment to settle before the new state is real.
            # Then show what the unit actually did: "OK" from systemctl only
            # means the request was accepted, not that the service stayed up.
            self.after(1200, lambda: self._after_action(key))

        def _render(self, st: UnitStatus) -> None:
            row = self._rows.get(st.key)
            if row is None:
                return
            colour = {
                "active":     "#1b7f3b",
                "activating": "#e0a800",
                "deactivating": "#e0a800",
                "failed":     "#b32020",
                "inactive":   "gray",
            }.get(st.active, "#b32020")
            row["dot"].configure(foreground=colour)

            if st.error:
                row["state"].set(st.error.splitlines()[0][:80])
                return
            text = st.active + (f" ({st.sub})" if st.sub else "")
            if st.enabled:
                text += f"   — {st.enabled} at boot"
            if st.is_running and st.since:
                # On its own line: the whole thing does not fit in one.
                text += f"\nsince {st.since.split('.')[0]}"
            row["state"].set(text)

        # -- teardown --------------------------------------------------

        def destroy(self) -> None:
            self._closing = True
            if self._auto_job is not None:
                try:
                    self.after_cancel(self._auto_job)
                except Exception:
                    pass
            super().destroy()

    return ServicePanel(parent, controller, **kw)


def ServicePanel(parent, controller: Optional[ServiceController] = None, **kw):
    """Public factory — returns a Tk frame with the service controls."""
    return _build_panel(parent, controller or ServiceController(Target.load()), **kw)


def run_gui(target: Optional[Target] = None) -> int:
    import tkinter as tk

    root = tk.Tk()
    root.title("xsphere Slow Control — Services")
    root.geometry("820x620")
    root.minsize(700, 500)
    panel = ServicePanel(root, ServiceController(target or Target.load()))
    panel.pack(fill="both", expand=True)

    def _close():
        panel.destroy()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _close)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="slowcontrol.servicectl",
        description="Control the xsphere slow control systemd services.",
    )
    p.add_argument("action", nargs="?", default="gui",
                   choices=["gui", "status", "logs", *ACTIONS],
                   help="what to do (default: gui)")
    p.add_argument("service", nargs="?", default="all",
                   choices=["all", *UNITS],
                   help="which service (default: all)")
    p.add_argument("--host", help=f"Pi hostname or IP (default {DEFAULT_HOST})")
    p.add_argument("--user", help=f"SSH user (default {DEFAULT_USER})")
    p.add_argument("--local", action="store_true",
                   help="run systemctl here instead of over SSH")
    p.add_argument("-n", "--lines", type=int, default=200,
                   help="log lines to fetch (default 200)")
    args = p.parse_args(argv)

    target = Target.load()
    if args.host:
        target.host = args.host
    if args.user:
        target.user = args.user
    if args.local:
        target.local = True

    if args.action == "gui":
        # Pass the target through — the Pi desktop entry launches with --local,
        # and without this the GUI would fall back to SSHing the Pi into itself.
        return run_gui(target)

    ctrl = ServiceController(target)
    keys = list(UNITS) if args.service == "all" else [args.service]

    if args.action == "status":
        for st in (ctrl.status(k) for k in keys):
            mark = "OK  " if st.is_running else ("FAIL" if st.is_failed else "--  ")
            detail = st.error or f"{st.active} ({st.sub}), {st.enabled} at boot"
            print(f"[{mark}] {st.label:<28} {detail}")
        return 0

    if args.action == "logs":
        rc = 0
        for k in keys:
            res = ctrl.logs(k, args.lines)
            print(f"===== {UNITS[k][0]} =====")
            print(res.text)
            rc |= 0 if res.ok else 1
        return rc

    rc = 0
    for k in keys:
        res = ctrl.action(args.action, k)
        status = "OK" if res.ok else f"FAILED ({res.returncode})"
        print(f"{args.action} {UNITS[k][0]}: {status}")
        if res.text:
            print(res.text)
        rc |= 0 if res.ok else 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
