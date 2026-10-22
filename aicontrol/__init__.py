"""
xsphere AI control service.

The only path by which an AI (Claude, over MCP or REST) may change anything on
the experiment.  It reads the slow-control state from MQTT, and publishes a
small allowlist of commands (heater setpoints / PV source / supply, LN2 fill
valves, pneumatic gas valves) only while a human has armed that group on the
Control page and only within the human's rails.  See aicontrol/README.md.
"""

__version__ = "1.0.0"
