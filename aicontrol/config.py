"""
Settings for the AI control service, read from the environment.

Production values come from the systemd unit (``XSPHERE_AICONTROL_INSTANCE=
production``).  Everything else is a *test* instance, and a test instance is
deliberately fenced off from the live experiment:

* its MQTT client id is ``xsphere-aicontrol-test-<host>-<pid>`` — a duplicate
  of the production id would make the broker kick the production service off;
* its own status / command topics live under ``xsphere/test/...``, so a test
  run on the DAQ PC can never overwrite what the Control page shows or obey the
  real arm buttons;
* it refuses to start unless the broker is on localhost (tests normally inject
  an in-process transport and use no broker at all).

The InfluxDB token is read from the repo's ``.env`` (``INFLUX_TOKEN``) unless
``XSPHERE_AICONTROL_INFLUX_TOKEN`` overrides it.  Only *where* the token came
from is ever logged or shown, never its value.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

#: Repo root (the directory holding slowcontrol/, webcontrol/, aicontrol/).
REPO_ROOT = Path(__file__).resolve().parent.parent

PRODUCTION = "production"

DEFAULT_ALLOWED_HOSTS = "xbox-pi,xbox-pi.local,192.168.8.116,localhost,127.0.0.1"
DEFAULT_INFLUX_URL = "http://localhost:8087"
DEFAULT_INFLUX_ORG = "xbox-server"
DEFAULT_INFLUX_BUCKET = "xsphere"

#: Broker hosts a test instance may use.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ConfigError(ValueError):
    """The environment asks for something the service must not do."""


def host_without_port(value: str) -> str:
    """``"xbox-pi:8089"`` → ``"xbox-pi"``, ``"[::1]:8089"`` → ``"::1"``.

    Lower-cased, trailing dot dropped.  Used for both the allowed-hosts list
    and the Host / Origin headers, so ports never matter when comparing.
    """
    v = (value or "").strip().lower()
    if v.startswith("["):                       # [v6]  or  [v6]:port
        end = v.find("]")
        v = v[1:end] if end > 0 else v[1:]
    elif v.count(":") == 1:                     # name:port  (bare v6 has ≥2)
        v = v.split(":", 1)[0]
    return v.rstrip(".")


def parse_allowed_hosts(text: str) -> FrozenSet[str]:
    """Comma-separated host list → frozenset of bare lower-case hostnames."""
    return frozenset(h for h in (host_without_port(p) for p in (text or "").split(","))
                     if h)


def load_env_file(path) -> Dict[str, str]:
    """Read a ``KEY=value`` file (the repo's ``.env``).

    Blank lines and ``#`` comments are skipped, an optional ``export`` prefix
    is accepted, and a value may be wrapped in single or double quotes.  An
    unquoted value ends at `` #`` (an inline comment).  A missing or unreadable
    file gives ``{}`` — the service then simply has no history token.
    """
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return out
    except OSError as e:
        log.warning("Could not read env file %s: %s", path, e)
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at].rstrip()
        out[key] = value
    return out


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        port = int(str(raw).strip())
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a port number") from None
    if not 0 < port < 65536:
        raise ConfigError(f"{name}={raw!r} is out of range")
    return port


def _client_id_token(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text) or "host"


class Settings:
    """Everything the service needs to know about where it runs.

    Construct with :meth:`from_env` in the service; tests may construct it
    directly (the test-instance broker check applies either way).
    """

    def __init__(self, *,
                 instance: str = "test",
                 mqtt_host: str = "localhost",
                 mqtt_port: int = 1883,
                 http_host: str = "0.0.0.0",
                 http_port: int = 8089,
                 state_dir: Optional[Path] = None,
                 repo_root: Optional[Path] = None,
                 allowed_hosts: Iterable[str] = parse_allowed_hosts(DEFAULT_ALLOWED_HOSTS),
                 influx_url: str = DEFAULT_INFLUX_URL,
                 influx_org: str = DEFAULT_INFLUX_ORG,
                 influx_bucket: str = DEFAULT_INFLUX_BUCKET,
                 influx_token: Optional[str] = None,
                 influx_token_source: str = "none",
                 env_file: Optional[Path] = None):
        self.instance = (instance or "test").strip() or "test"
        self.production = self.instance == PRODUCTION
        self.mqtt_host = mqtt_host
        self.mqtt_port = int(mqtt_port)
        self.http_host = http_host
        self.http_port = int(http_port)
        self.repo_root = Path(repo_root) if repo_root is not None else REPO_ROOT
        self.state_dir = (Path(state_dir) if state_dir is not None
                          else self.repo_root / "aicontrol")
        self.env_file = env_file
        self.allowed_hosts: FrozenSet[str] = frozenset(
            host_without_port(h) for h in allowed_hosts if host_without_port(h))
        self.influx_url = influx_url.rstrip("/")
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.influx_token = influx_token or None
        self.influx_token_source = influx_token_source if self.influx_token else "none"

        if self.production:
            self.client_id = "xsphere-aicontrol"
            self.status_topic = "xsphere/status/ai_control"
            self.cmd_prefix = "xsphere/commands/ai_control"
        else:
            self.client_id = (f"xsphere-aicontrol-test-"
                              f"{_client_id_token(socket.gethostname())}-{os.getpid()}")
            self.status_topic = "xsphere/test/ai_control"
            self.cmd_prefix = "xsphere/test/commands/ai_control"
            # A test instance must never join the live broker: it would answer
            # to nothing real (its topics are under xsphere/test/) but it could
            # still publish real command topics with test rails.
            if host_without_port(self.mqtt_host) not in _LOCAL_HOSTS:
                raise ConfigError(
                    f"test instance refuses MQTT host {self.mqtt_host!r}: a "
                    f"non-production instance only talks to a broker on localhost "
                    f"(set XSPHERE_AICONTROL_INSTANCE=production for the real service)")

    @property
    def audit_topic(self) -> str:
        return f"{self.status_topic}/audit"

    # Where things live (one place, so every module agrees).
    @property
    def rails_path(self) -> Path:
        return self.state_dir / "rails.json"

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def runtime_path(self) -> Path:
        return self.state_dir / "runtime.json"

    @property
    def state_yaml(self) -> Path:
        return self.repo_root / "slowcontrol" / "state.yaml"

    @property
    def telegraf_conf(self) -> Path:
        return self.repo_root / "telegraf" / "telegraf.conf"

    def host_allowed(self, host_header: Optional[str]) -> bool:
        """True when a Host / Origin hostname (port ignored) is allowed."""
        return bool(host_header) and host_without_port(host_header) in self.allowed_hosts

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        instance = (env.get("XSPHERE_AICONTROL_INSTANCE") or "test").strip() or "test"
        repo_root = REPO_ROOT
        state_dir = env.get("XSPHERE_AICONTROL_STATE_DIR") or str(repo_root / "aicontrol")
        env_file = Path(env.get("XSPHERE_AICONTROL_ENV_FILE") or str(repo_root / ".env"))
        dotenv = load_env_file(env_file)

        override = (env.get("XSPHERE_AICONTROL_INFLUX_TOKEN") or "").strip()
        if override:
            token, source = override, "environment XSPHERE_AICONTROL_INFLUX_TOKEN"
        elif dotenv.get("INFLUX_TOKEN", "").strip():
            token, source = dotenv["INFLUX_TOKEN"].strip(), f"{env_file} INFLUX_TOKEN"
        else:
            token, source = None, "none"

        # The URL deliberately does NOT come from .env: on the Pi that file
        # mirrors the Telegraf values, whose INFLUX_URL is the Docker-internal
        # http://influxdb2:8086 — unreachable from a host service.
        return cls(
            instance=instance,
            mqtt_host=(env.get("XSPHERE_MQTT_HOST") or "localhost").strip(),
            mqtt_port=_port(env, "XSPHERE_MQTT_PORT", 1883),
            http_host=(env.get("XSPHERE_AICONTROL_HOST") or "0.0.0.0").strip(),
            http_port=_port(env, "XSPHERE_AICONTROL_PORT", 8089),
            state_dir=Path(state_dir),
            repo_root=repo_root,
            allowed_hosts=parse_allowed_hosts(
                env.get("XSPHERE_AICONTROL_ALLOWED_HOSTS") or DEFAULT_ALLOWED_HOSTS),
            influx_url=(env.get("XSPHERE_AICONTROL_INFLUX_URL") or DEFAULT_INFLUX_URL).strip(),
            influx_org=(env.get("XSPHERE_AICONTROL_INFLUX_ORG")
                        or dotenv.get("INFLUX_ORG") or DEFAULT_INFLUX_ORG).strip(),
            influx_bucket=(env.get("XSPHERE_AICONTROL_INFLUX_BUCKET")
                           or dotenv.get("INFLUX_BUCKET") or DEFAULT_INFLUX_BUCKET).strip(),
            influx_token=token,
            influx_token_source=source,
            env_file=env_file,
        )

    def log_startup(self) -> None:
        """One block of INFO lines naming the instance and every topic."""
        log.info("AI control instance %r (%s)", self.instance,
                 "PRODUCTION" if self.production else "test — not the live service")
        log.info("MQTT %s:%d as %s; status %s (+/audit); human commands %s/{arm,rails}",
                 self.mqtt_host, self.mqtt_port, self.client_id,
                 self.status_topic, self.cmd_prefix)
        log.info("HTTP %s:%d; allowed hosts %s", self.http_host, self.http_port,
                 ", ".join(sorted(self.allowed_hosts)))
        log.info("State dir %s", self.state_dir)
        log.info("InfluxDB %s org %s bucket %s; token from %s", self.influx_url,
                 self.influx_org, self.influx_bucket, self.influx_token_source)

    def __repr__(self) -> str:      # never shows the token
        return (f"Settings(instance={self.instance!r}, mqtt={self.mqtt_host}:{self.mqtt_port}, "
                f"client_id={self.client_id!r}, status_topic={self.status_topic!r}, "
                f"cmd_prefix={self.cmd_prefix!r}, http={self.http_host}:{self.http_port}, "
                f"state_dir={str(self.state_dir)!r}, token_source={self.influx_token_source!r})")
