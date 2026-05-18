"""Proprioception layer — the system's centralized state registry + store.

`state.yaml` (one directory up) declares every state the system tracks;
`schema.py` parses it into typed objects; `store.py` (added in build step 2)
subscribes to MQTT, computes per-state freshness and moving averages, evaluates
the derived expressions, and republishes a consolidated snapshot on
`xsphere/state/snapshot`.  See `slowcontrol/STATE_LAYER_PLAN.md`.
"""

from slowcontrol.state.schema import (
    ControlSpec,
    Freshness,
    SchemaError,
    SourceSpec,
    StateDef,
    StateSchema,
    default_schema_path,
    load_state_schema,
)

__all__ = [
    "ControlSpec",
    "Freshness",
    "SchemaError",
    "SourceSpec",
    "StateDef",
    "StateSchema",
    "default_schema_path",
    "load_state_schema",
]
