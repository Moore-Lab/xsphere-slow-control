#!/usr/bin/env python3
"""
xsphere slow control GUI.

Usage:
    python -m slowcontrol.gui                  # default config
    python -m slowcontrol.gui -c config.yaml   # custom config path
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
import tkinter as tk
from tkinter import ttk
from typing import Optional, Type

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import LabJackPanel — works whether the package is installed or not
# ---------------------------------------------------------------------------

def _find_lj_panel() -> Optional[Type]:
    """Return LabJackPanel class, trying installed package then dev submodule path."""
    try:
        from labjack_t7.gui import LabJackPanel  # installed via pip install -e
        return LabJackPanel
    except ImportError:
        pass

    # Dev layout: LJ-python-controller/ sits next to slowcontrol/ at repo root
    gui_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "LJ-python-controller", "gui.py")
    )
    if os.path.exists(gui_path):
        spec = importlib.util.spec_from_file_location("lj_gui", gui_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return getattr(mod, "LabJackPanel", None)

    return None


LabJackPanel = _find_lj_panel()
_HAS_LJ = LabJackPanel is not None


# ---------------------------------------------------------------------------
# Individual tab panels
# ---------------------------------------------------------------------------

class _OverviewTab(ttk.Frame):
    """System-wide status at a glance."""

    def __init__(self, parent: tk.Misc, **kw) -> None:
        super().__init__(parent, **kw)
        self._build()

    def _build(self) -> None:
        ttk.Label(
            self,
            text="xsphere Slow Control",
            font=("TkDefaultFont", 16, "bold"),
        ).pack(pady=(24, 4))
        ttk.Label(
            self,
            text="Select a tab above to control a subsystem.",
            foreground="gray",
        ).pack()

        info = ttk.LabelFrame(self, text="System", padding=10)
        info.pack(fill="x", padx=20, pady=20)

        rows = [
            ("PLC",      "CLICK PLC via Modbus TCP"),
            ("LabJack",  "T7 DAQ (AIN / DAC / DIO / PT100 / TC)"),
            ("MQTT",     "paho-mqtt broker (localhost:1883)"),
        ]
        for i, (label, desc) in enumerate(rows):
            ttk.Label(info, text=f"{label}:", font=("TkDefaultFont", 10, "bold"),
                      anchor="e", width=12).grid(row=i, column=0, sticky="e", padx=(4, 6), pady=3)
            ttk.Label(info, text=desc, anchor="w").grid(row=i, column=1, sticky="w")


class _LabJackTab(ttk.Frame):
    """Hosts the full LabJack GUI panel with its own nested notebook tabs."""

    def __init__(self, parent: tk.Misc, **kw) -> None:
        super().__init__(parent, **kw)
        if _HAS_LJ:
            panel = LabJackPanel(self)
            panel.pack(fill="both", expand=True)
        else:
            ttk.Label(
                self,
                text=(
                    "LabJack GUI not available.\n\n"
                    "Install the package:\n"
                    "    pip install -e LJ-python-controller/"
                ),
                foreground="red",
                font=("TkDefaultFont", 11),
                justify="center",
            ).pack(expand=True)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class SlowControlApp(tk.Tk):
    """Main slow control GUI window with one tab per subsystem."""

    def __init__(self, config_path: str = "config.yaml") -> None:
        super().__init__()
        self.title("xsphere Slow Control")
        self.geometry("1100x780")
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._config_path = config_path
        self._build_ui()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        # Overview tab
        overview = _OverviewTab(nb)
        nb.add(overview, text="Overview")

        # LabJack T7 tab — contains the full LJ panel with its own nested tabs
        lj = _LabJackTab(nb)
        nb.add(lj, text="LabJack T7")

    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        # Destroy all child widgets (LabJackPanel.destroy() cleans up threads)
        for child in self.winfo_children():
            child.destroy()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="xsphere slow control GUI")
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="Path to YAML config file (default: config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    app = SlowControlApp(config_path=args.config)
    app.mainloop()


if __name__ == "__main__":
    main()
