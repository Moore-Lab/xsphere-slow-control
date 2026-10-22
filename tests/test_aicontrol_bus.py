"""
Offline tests for the AI control service's foundation: the MQTT bus
(aicontrol/bus.py), its settings (aicontrol/config.py) and the audit trail
(aicontrol/audit.py).

    python -m pytest tests/test_aicontrol_bus.py -q      (from the repo root)

The bus runs on LoopbackTransport — an in-process stand-in for the broker — so
no MQTT connection is ever made.  Ages are driven by a fake clock.
"""

import json
import re
import threading
import time
from pathlib import Path

import pytest

from aicontrol import __version__
from aicontrol.audit import KINDS, Audit
from aicontrol.bus import (ALLOWED_COMMAND_PATTERNS, OFFLINE_STATUS, SNAPSHOT_TOPIC, Bus,
                           ForbiddenTopicError, LoopbackTransport, PahoTransport,
                           decode_payload, is_allowed_command)
from aicontrol.config import (ConfigError, Settings, host_without_port, load_env_file,
                              parse_allowed_hosts)

REPO = Path(__file__).resolve().parent.parent
T0 = 1_792_650_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_settings(tmp_path, **env):
    base = {"XSPHERE_AICONTROL_STATE_DIR": str(tmp_path),
            "XSPHERE_AICONTROL_ENV_FILE": str(tmp_path / "no.env")}
    base.update(env)
    return Settings.from_env(base)


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    transport = LoopbackTransport()
    bus = Bus(make_settings(tmp_path), transport, clock=clock)
    bus.start()
    return bus, transport, clock


def view(value, freshness="fresh", **extra):
    v = {"id": "x", "value": value, "freshness": freshness, "ts": None, "age_s": 0.1}
    v.update(extra)
    return v


def snapshot(generated_at, **states):
    return {"generated_at": generated_at, "counts": {}, "states": states}


# ──────────────────────────────────────────────────────────────────────────
# config
# ──────────────────────────────────────────────────────────────────────────

def test_version():
    assert __version__ == "1.0.0"


def test_test_instance_defaults(tmp_path):
    s = make_settings(tmp_path)
    assert s.instance == "test" and s.production is False
    assert re.fullmatch(r"xsphere-aicontrol-test-[A-Za-z0-9_.-]+-\d+", s.client_id)
    assert s.client_id.endswith(f"-{__import__('os').getpid()}")
    assert s.status_topic == "xsphere/test/ai_control"
    assert s.cmd_prefix == "xsphere/test/commands/ai_control"
    assert s.audit_topic == "xsphere/test/ai_control/audit"
    assert (s.mqtt_host, s.mqtt_port) == ("localhost", 1883)
    assert (s.http_host, s.http_port) == ("0.0.0.0", 8089)
    assert s.state_dir == tmp_path
    assert s.repo_root == REPO
    assert s.rails_path == tmp_path / "rails.json"
    assert s.audit_path == tmp_path / "audit.jsonl"
    assert s.runtime_path == tmp_path / "runtime.json"
    assert s.state_yaml.is_file() and s.telegraf_conf.is_file()
    assert s.allowed_hosts == frozenset({"xbox-pi", "xbox-pi.local", "192.168.8.116",
                                         "localhost", "127.0.0.1"})
    assert s.influx_url == "http://localhost:8087"
    assert (s.influx_org, s.influx_bucket) == ("xbox-server", "xsphere")
    assert s.influx_token is None and s.influx_token_source == "none"


def test_default_state_dir_is_in_the_repo(tmp_path):
    s = Settings.from_env({"XSPHERE_AICONTROL_ENV_FILE": str(tmp_path / "x")})
    assert s.state_dir == REPO / "aicontrol"


def test_production_instance(tmp_path):
    s = make_settings(tmp_path, XSPHERE_AICONTROL_INSTANCE="production",
                      XSPHERE_MQTT_HOST="192.168.8.116", XSPHERE_MQTT_PORT="1884",
                      XSPHERE_AICONTROL_PORT="9000", XSPHERE_AICONTROL_HOST="127.0.0.1")
    assert s.production is True
    assert s.client_id == "xsphere-aicontrol"
    assert s.status_topic == "xsphere/status/ai_control"
    assert s.cmd_prefix == "xsphere/commands/ai_control"
    assert (s.mqtt_host, s.mqtt_port, s.http_host, s.http_port) == (
        "192.168.8.116", 1884, "127.0.0.1", 9000)


@pytest.mark.parametrize("host", ["192.168.8.116", "xbox-pi", "broker.example.org"])
def test_test_instance_refuses_a_remote_broker(tmp_path, host):
    with pytest.raises(ConfigError, match="test instance refuses MQTT host"):
        make_settings(tmp_path, XSPHERE_MQTT_HOST=host)
    with pytest.raises(ConfigError):
        Settings(instance="anything-but-production", mqtt_host=host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "LOCALHOST"])
def test_test_instance_accepts_localhost(tmp_path, host):
    assert make_settings(tmp_path, XSPHERE_MQTT_HOST=host).mqtt_host == host


@pytest.mark.parametrize("var, value", [("XSPHERE_MQTT_PORT", "abc"),
                                        ("XSPHERE_MQTT_PORT", "0"),
                                        ("XSPHERE_AICONTROL_PORT", "70000")])
def test_bad_ports_rejected(tmp_path, var, value):
    with pytest.raises(ConfigError):
        make_settings(tmp_path, **{var: value})


def test_allowed_hosts_ignore_ports_and_case(tmp_path):
    s = make_settings(tmp_path, XSPHERE_AICONTROL_ALLOWED_HOSTS=" Foo:80, bar ,,[::1]:8089")
    assert s.allowed_hosts == frozenset({"foo", "bar", "::1"})
    assert s.host_allowed("FOO:8089") and s.host_allowed("bar") and s.host_allowed("[::1]:1")
    assert not s.host_allowed("evil.example") and not s.host_allowed("") \
        and not s.host_allowed(None)
    d = make_settings(tmp_path)
    assert d.host_allowed("xbox-pi:8089") and d.host_allowed("192.168.8.116:8089")
    assert d.host_allowed("xbox-pi.local.") and not d.host_allowed("xbox-pi.evil.example")
    assert host_without_port("[::1]:8089") == "::1"
    assert host_without_port("::1") == "::1"
    assert parse_allowed_hosts("a:1,b") == frozenset({"a", "b"})


def test_load_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("\n".join([
        "# InfluxDB",
        "INFLUX_URL=http://influxdb2:8086",
        "INFLUX_TOKEN=\"abc def==\"",
        "export INFLUX_ORG='xbox-server'",
        "INFLUX_BUCKET=xsphere   # inline comment",
        "",
        "not a line",
        "BAD KEY=1",
        "EMPTY=",
        "  SPACED = value with = sign  ",
    ]), encoding="utf-8")
    env = load_env_file(p)
    assert env == {"INFLUX_URL": "http://influxdb2:8086", "INFLUX_TOKEN": "abc def==",
                   "INFLUX_ORG": "xbox-server", "INFLUX_BUCKET": "xsphere", "EMPTY": "",
                   "SPACED": "value with = sign"}
    assert load_env_file(tmp_path / "missing") == {}


def test_influx_token_sources_and_secrecy(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("INFLUX_TOKEN=from-dotenv-SECRET\nINFLUX_ORG=org2\nINFLUX_BUCKET=b2\n"
                    "INFLUX_URL=http://influxdb2:8086\n", encoding="utf-8")
    s = make_settings(tmp_path, XSPHERE_AICONTROL_ENV_FILE=str(envf))
    assert s.influx_token == "from-dotenv-SECRET"
    assert s.influx_token_source == f"{envf} INFLUX_TOKEN"
    assert (s.influx_org, s.influx_bucket) == ("org2", "b2")
    assert s.influx_url == "http://localhost:8087"          # the Docker-internal URL is not used
    assert "SECRET" not in repr(s)

    o = make_settings(tmp_path, XSPHERE_AICONTROL_ENV_FILE=str(envf),
                      XSPHERE_AICONTROL_INFLUX_TOKEN="override-SECRET",
                      XSPHERE_AICONTROL_INFLUX_URL="http://127.0.0.1:8087/")
    assert o.influx_token == "override-SECRET"
    assert o.influx_token_source == "environment XSPHERE_AICONTROL_INFLUX_TOKEN"
    assert o.influx_url == "http://127.0.0.1:8087"


def test_log_startup_never_logs_the_token(tmp_path, caplog):
    s = make_settings(tmp_path, XSPHERE_AICONTROL_INFLUX_TOKEN="tok-SECRET")
    with caplog.at_level("INFO", logger="aicontrol.config"):
        s.log_startup()
    text = caplog.text
    assert "SECRET" not in text
    assert "xsphere/test/ai_control" in text and "xsphere/test/commands/ai_control" in text
    assert "environment XSPHERE_AICONTROL_INFLUX_TOKEN" in text


# ──────────────────────────────────────────────────────────────────────────
# bus — connection and subscriptions
# ──────────────────────────────────────────────────────────────────────────

def test_subscriptions_on_connect(rig):
    bus, t, _ = rig
    assert bus.connected()
    assert t.subscriptions == [
        (SNAPSHOT_TOPIC, 0, False),
        ("xsphere/status/#", 1, False),
        ("xsphere/test/commands/ai_control/#", 1, True),     # retainHandling = 2
    ]


def test_connect_hooks_reconnect_and_retained_replay(tmp_path):
    t = LoopbackTransport()
    clock = Clock()
    bus = Bus(make_settings(tmp_path), t, clock=clock)
    hooks, arms = [], []
    bus.on_connect_hook(hooks.append)
    bus.on_command("arm", arms.append)
    bus.set_status_provider(lambda: {"online": 1, "n": len(hooks)})
    bus.start()
    assert hooks == [False]
    assert t.published[-1] == ("xsphere/test/ai_control", {"online": 1, "n": 1}, 1, True)

    t.inject("xsphere/status/pid/top", {"setpoint_k": 160.0})            # live
    clock.advance(30)
    t.set_connected(False)
    assert not bus.connected()
    # While we were away: a retained status update and a retained arm command.
    t.inject("xsphere/status/pid/top", {"setpoint_k": 170.0}, retain=True)
    t.inject("xsphere/test/commands/ai_control/arm", {"group": "heaters", "armed": True},
             retain=True)
    clock.advance(5)
    t.set_connected(True)
    assert hooks == [False, True]
    assert len(t.subscriptions) == 3                                      # re-subscribed
    raw = bus.raw("xsphere/status/pid/top")
    assert raw["payload"] == {"setpoint_k": 170.0}
    assert raw["retained"] is True
    assert bus.live_age("xsphere/status/pid/top") == pytest.approx(35.0)  # last LIVE copy
    assert arms == []                                  # retained command never replayed


def test_stop_publishes_offline_status(rig):
    bus, t, _ = rig
    bus.stop()
    assert t.published[-1] == ("xsphere/test/ai_control", OFFLINE_STATUS, 1, True)
    assert t.retained_payload("xsphere/test/ai_control") == OFFLINE_STATUS
    assert not bus.connected()


def test_paho_transport_sets_the_retained_will(tmp_path):
    s = make_settings(tmp_path)
    client = PahoTransport(s)._build_client()          # built only, never connected
    assert client._will_topic == s.status_topic.encode()
    assert json.loads(client._will_payload) == {"online": 0, "armed_heaters": 0,
                                                "armed_ln2": 0, "armed_pneumatic": 0}
    assert (client._will_qos, client._will_retain) == (1, True)
    assert (client._reconnect_min_delay, client._reconnect_max_delay) == (1, 10)
    assert client._client_id == s.client_id.encode()
    assert PahoTransport(s).is_connected() is False
    assert PahoTransport(s).publish("xsphere/commands/pid/top/setpoint", "{}") is False


# ──────────────────────────────────────────────────────────────────────────
# bus — snapshot and freshness
# ──────────────────────────────────────────────────────────────────────────

def test_no_snapshot_yet(rig):
    bus, _, _ = rig
    assert bus.snapshot() == {}
    assert bus.snapshot_age() is None and bus.snapshot_rx_age() is None
    assert bus.state("t_cube_top") is None
    assert bus.state_value("t_cube_top") is None


def test_snapshot_age_is_from_generated_at_not_receipt(rig):
    bus, t, clock = rig
    t.inject(SNAPSHOT_TOPIC, snapshot(clock.t - 12, t_cube_top=view(165.2)))
    assert bus.snapshot_age() == pytest.approx(12.0)
    assert bus.snapshot_rx_age() == pytest.approx(0.0)       # just received, still 12 s old
    assert bus.state("t_cube_top")["value"] == 165.2
    assert bus.state_value("t_cube_top") is None             # older than 5 s
    assert bus.state_value("t_cube_top", max_age_s=20) == 165.2
    assert bus.state_value("t_cube_top", max_age_s=None) == 165.2

    t.inject(SNAPSHOT_TOPIC, snapshot(clock.t - 0.5, t_cube_top=view(165.3)))
    assert bus.state_value("t_cube_top") == 165.3
    clock.advance(4.4)
    assert bus.state_value("t_cube_top") == 165.3            # 4.9 s ≤ 5
    clock.advance(0.2)
    assert bus.state_value("t_cube_top") is None             # 5.1 s


def test_retained_snapshot_after_reconnect_is_old(rig):
    bus, t, clock = rig
    # The broker replays a dead slowcontrol's last snapshot: received now, made 13 min ago.
    t.inject(SNAPSHOT_TOPIC, snapshot(clock.t - 780, pneumatic_control_enabled=view(True)),
             retain=True)
    assert bus.snapshot_age() == pytest.approx(780)
    assert bus.state_value("pneumatic_control_enabled") is None


@pytest.mark.parametrize("freshness", ["stale", "invalid", None])
def test_state_value_requires_fresh(rig, freshness):
    bus, t, clock = rig
    t.inject(SNAPSHOT_TOPIC, snapshot(clock.t, a=view(1.0, freshness), b=view(2.0)))
    assert bus.state_value("a") is None
    assert bus.state_value("b") == 2.0
    assert bus.state_value("missing") is None


@pytest.mark.parametrize("bad", [
    b'{"generated_at": NaN, "states": {}}',
    b'{"generated_at": Infinity, "states": {}}',
    b'{"generated_at": 1e999, "states": {}}',
    b'{"generated_at": true, "states": {}}',
    b'{"generated_at": "now", "states": {}}',
    b'{"states": {}}',
    b'{"generated_at": 1, "states": []}',
    b'[1, 2]',
    b'not json',
    b'',
    b'{"generated_at": 1792650000, "states": {"a": {"value": NaN}}}',
])
def test_invalid_snapshots_are_ignored(rig, bad):
    bus, t, clock = rig
    t.inject(SNAPSHOT_TOPIC, snapshot(clock.t - 1, a=view(1.0)))
    t.inject(SNAPSHOT_TOPIC, bad)
    assert bus.snapshot()["states"]["a"]["value"] == 1.0
    assert bus.snapshot_age() == pytest.approx(1.0)


def test_snapshot_and_state_are_copies(rig):
    bus, t, clock = rig
    t.inject(SNAPSHOT_TOPIC, snapshot(clock.t, a=view({"x": 1})))
    bus.snapshot()["states"]["a"]["value"]["x"] = 2
    bus.state("a")["value"]["x"] = 3
    bus.state_value("a")["x"] = 4
    assert bus.state_value("a") == {"x": 1}


# ──────────────────────────────────────────────────────────────────────────
# bus — raw status cache, retained vs live
# ──────────────────────────────────────────────────────────────────────────

def test_retained_and_live_ages(rig):
    bus, t, clock = rig
    topic = "xsphere/status/pneumatic_control"
    assert bus.raw(topic) is None and bus.live_age(topic) is None

    t.inject(topic, {"enabled": 1}, retain=True)             # replay at subscribe: age unknown
    r = bus.raw(topic)
    assert r["payload"] == {"enabled": 1} and r["retained"] is True
    assert r["live_rx_ts"] is None and r["rx_ts"] == clock.t and r["seq"] == 1
    assert bus.live_age(topic) is None
    assert bus.live_payload(topic, 3.0) is None

    clock.advance(5)
    t.inject(topic, {"enabled": 1, "n": 2})                  # live
    assert bus.live_age(topic) == pytest.approx(0)
    assert bus.live_payload(topic, 3.0) == {"enabled": 1, "n": 2}
    r = bus.raw(topic)
    assert r["retained"] is False and r["seq"] == 2 and r["prev_payload"] == {"enabled": 1}

    clock.advance(2.5)
    assert bus.live_age(topic) == pytest.approx(2.5)
    assert bus.live_payload(topic, 3.0) == {"enabled": 1, "n": 2}
    clock.advance(1)
    assert bus.live_payload(topic, 3.0) is None              # 3.5 s > 3 s

    t.inject(topic, {"enabled": 0}, retain=True)             # replay again (reconnect)
    assert bus.raw(topic)["retained"] is True
    assert bus.live_age(topic) == pytest.approx(3.5)         # still the last LIVE receipt
    assert bus.live_payload(topic, 10.0) is None             # latest copy is a replay
    assert bus.raw_topics("xsphere/status/pneu") == [topic]


@pytest.mark.parametrize("raw, expected", [
    (b'{"a": 1.5}', {"a": 1.5}),
    (b'{"a": NaN}', '{"a": NaN}'),
    (b'{"a": Infinity}', '{"a": Infinity}'),
    (b'{"a": -Infinity}', '{"a": -Infinity}'),
    (b'{"a": 1e999}', '{"a": 1e999}'),
    (b'[1, NaN]', '[1, NaN]'),
    (b'12', 12),
    (b'hello', "hello"),
    (b'', ""),
    (b'\xff\xfe', "��"),
])
def test_decode_payload_rejects_non_finite(raw, expected):
    assert decode_payload(raw) == expected


def test_nan_status_payload_cached_as_text(rig):
    bus, t, _ = rig
    t.inject("xsphere/status/pid/top", b'{"setpoint_k": NaN, "mode": "auto"}')
    assert bus.raw("xsphere/status/pid/top")["payload"] == '{"setpoint_k": NaN, "mode": "auto"}'


def test_status_observers(rig):
    bus, t, _ = rig
    seen = []
    bus.on_status("xsphere/status/pv_interlock",
                  lambda topic, p: seen.append((topic, p, bus.raw(topic)["seq"])))
    bus.on_status("xsphere/status/service/+", lambda topic, p: seen.append(("svc", p)))

    def boom(topic, payload):
        raise RuntimeError("observer bug")
    bus.on_status("#", boom)                                 # must not break delivery

    t.inject("xsphere/status/pv_interlock", {"tripped": False}, retain=True)
    t.inject("xsphere/status/pv_interlock", {"tripped": True})
    t.inject("xsphere/status/service/heartbeat", {"uptime_s": 5})
    t.inject("xsphere/status/pid/top", {"mode": "auto"})
    assert seen == [("xsphere/status/pv_interlock", {"tripped": False}, 1),
                    ("xsphere/status/pv_interlock", {"tripped": True}, 2),
                    ("svc", {"uptime_s": 5})]
    assert bus.raw("xsphere/status/pid/top")["payload"] == {"mode": "auto"}


def test_unsubscribed_topics_are_not_cached(rig):
    bus, t, _ = rig
    assert t.inject("xsphere/sensors/temperature/labjack/rtd/1", {"value_k": 1}) is False
    assert bus.raw("xsphere/sensors/temperature/labjack/rtd/1") is None


def test_own_status_and_audit_are_not_cached(tmp_path):
    # Production publishes on xsphere/status/ai_control, under its own
    # xsphere/status/# subscription: those echoes are dropped.
    t = LoopbackTransport()
    bus = Bus(make_settings(tmp_path, XSPHERE_AICONTROL_INSTANCE="production"), t,
              clock=Clock())
    bus.start()
    assert bus.publish_status({"online": 1}) is True
    assert bus.publish_audit([{"kind": "note"}]) is True
    t.inject("xsphere/status/ai_control_other", {"x": 1})
    assert bus.raw("xsphere/status/ai_control") is None
    assert bus.raw("xsphere/status/ai_control/audit") is None
    assert bus.raw("xsphere/status/ai_control_other")["payload"] == {"x": 1}


# ──────────────────────────────────────────────────────────────────────────
# bus — human commands (arm / rails)
# ──────────────────────────────────────────────────────────────────────────

def test_commands_live_only_and_dicts_only(rig):
    bus, t, _ = rig
    arms, rails = [], []
    bus.on_command("arm", arms.append)
    bus.on_command("rails", rails.append)
    prefix = "xsphere/test/commands/ai_control"

    t.inject(f"{prefix}/arm", {"group": "heaters", "armed": True})
    t.inject(f"{prefix}/arm", {"group": "ln2", "armed": True}, retain=True)   # ignored
    t.inject(f"{prefix}/arm", [1, 2])                                          # ignored
    t.inject(f"{prefix}/arm", b'{"group": "heaters", "armed": NaN}')           # ignored
    t.inject(f"{prefix}/arm", b"")                                             # ignored
    t.inject(f"{prefix}/rails", {"rails": {}, "base_revision": 0})
    t.inject(f"{prefix}/unknown", {"x": 1})                                    # no handler
    t.inject("xsphere/commands/ai_control/arm", {"group": "pneumatic", "armed": True})  # prod topic
    assert arms == [{"group": "heaters", "armed": True}]
    assert rails == [{"rails": {}, "base_revision": 0}]
    assert bus.raw(f"{prefix}/arm") is None                  # commands are not status


def test_command_callback_exception_is_contained(rig):
    bus, t, _ = rig
    calls = []

    def bad(payload):
        calls.append(payload)
        raise ValueError("bug")
    bus.on_command("arm", bad)
    t.inject("xsphere/test/commands/ai_control/arm", {"n": 1})
    t.inject("xsphere/test/commands/ai_control/arm", {"n": 2})
    assert calls == [{"n": 1}, {"n": 2}]


# ──────────────────────────────────────────────────────────────────────────
# bus — publishing commands
# ──────────────────────────────────────────────────────────────────────────

ALLOWED = [
    ("xsphere/commands/pid/top/setpoint", {"value_k": 165.0}),
    ("xsphere/commands/pid/bottom/mode", {"mode": "auto"}),
    ("xsphere/commands/pid/nozzle/pv_expr", {"expr": "rtd1"}),
    ("xsphere/commands/nge/1/output", {"state": 1}),
    ("xsphere/commands/nge/2/voltage", {"value_v": 12.5}),
    ("xsphere/commands/nge/3/current", {"value_a": 1.25}),
    ("xsphere/commands/valve/cryostat/state", {"state": 1}),
    ("xsphere/commands/valve/primary_xe/auto_open", {"enabled": False}),
    ("xsphere/commands/valve/ballast/auto_close", {"enabled": True}),
    ("xsphere/commands/pneumatic/ballast/state", {"state": 0}),
    ("xsphere/commands/pneumatic/pump/state", {"state": 1}),
    ("xsphere/commands/pneumatic/bottle/state", {"state": 1}),
]


@pytest.mark.parametrize("topic, payload", ALLOWED)
def test_allowed_commands_publish_qos0_not_retained(rig, topic, payload):
    bus, t, _ = rig
    t.clear()
    assert bus.publish_command(topic, payload) is True
    assert t.published == [(topic, payload, 0, False)]
    assert t.retained_payload(topic) is None


FORBIDDEN = [
    "xsphere/commands/pneumatic_control/enable",
    "xsphere/commands/ai_control/arm",
    "xsphere/commands/ai_control/rails",
    "xsphere/test/commands/ai_control/arm",
    "xsphere/commands/pv_interlock/limits",
    "xsphere/commands/turbo/ghs/mode",
    "xsphere/commands/turbo/outer/mode",
    "xsphere/commands/turbo_control/enable",
    "xsphere/commands/pid/top/output",
    "xsphere/commands/pid/top/setpoint_expr",
    "xsphere/commands/pid/top/pv",
    "xsphere/commands/pid/top/gains",
    "xsphere/commands/pid/top/autotune",
    "xsphere/commands/pid/top/controller_type",
    "xsphere/commands/pid/middle/setpoint",
    "xsphere/commands/pid/+/setpoint",
    "xsphere/commands/pid/top/setpoint/x",
    "xsphere/commands/pid/top/setpoint\n",
    " xsphere/commands/pid/top/setpoint",
    "xsphere/commands/nge/hold",
    "xsphere/commands/nge/4/output",
    "xsphere/commands/nge/1/state",
    "xsphere/commands/gradient/base",
    "xsphere/commands/gradient/mode",
    "xsphere/commands/valve/cryostat/desired",
    "xsphere/commands/valve/dewar/state",
    "xsphere/commands/pneumatic/vent/state",
    "xsphere/commands/pneumatic/pump/enable",
    "xsphere/commands/sequencer/run",
    "xsphere/commands/trackers/add",
    "xsphere/status/pid/top",
    "xsphere/commands/#",
    "",
]


@pytest.mark.parametrize("topic", FORBIDDEN)
def test_forbidden_topics_raise(rig, topic):
    bus, t, _ = rig
    t.clear()
    assert not is_allowed_command(topic)
    with pytest.raises(ForbiddenTopicError):
        bus.publish_command(topic, {"enabled": True})
    assert t.published == []


def test_forbidden_topic_raises_even_when_disconnected(rig):
    bus, t, _ = rig
    t.set_connected(False)
    with pytest.raises(ForbiddenTopicError):
        bus.publish_command("xsphere/commands/pneumatic_control/enable", {"enabled": True})


def test_allowlist_is_compiled_full_match():
    assert len(ALLOWED_COMMAND_PATTERNS) == 4
    assert all(isinstance(p, re.Pattern) for p in ALLOWED_COMMAND_PATTERNS)
    assert all(is_allowed_command(topic) for topic, _ in ALLOWED)
    assert not is_allowed_command(None) and not is_allowed_command(b"x")


@pytest.mark.parametrize("payload, exc", [
    ({"value_k": float("nan")}, ValueError),
    ({"value_k": float("inf")}, ValueError),
    ({"value_k": [float("-inf")]}, ValueError),
    ([165.0], TypeError),
    ("165", TypeError),
    (None, TypeError),
])
def test_bad_payloads_raise_and_publish_nothing(rig, payload, exc):
    bus, t, _ = rig
    t.clear()
    with pytest.raises(exc):
        bus.publish_command("xsphere/commands/pid/top/setpoint", payload)
    assert t.published == []


def test_nothing_published_when_disconnected(rig):
    bus, t, _ = rig
    t.clear()
    t.set_connected(False)
    assert bus.publish_command("xsphere/commands/valve/cryostat/state", {"state": 1}) is False
    assert bus.publish_status({"online": 1}) is False
    assert bus.publish_audit([]) is False
    assert t.published == []


def test_publish_failure_reported(rig):
    bus, t, _ = rig
    t.fail_publish = True
    assert bus.publish_command("xsphere/commands/valve/cryostat/state", {"state": 0}) is False


def test_publish_status_and_audit_retained(rig):
    bus, t, _ = rig
    t.clear()
    assert bus.publish_status({"online": 1, "armed_heaters": 0}) is True
    assert bus.publish_audit([{"kind": "note"}]) is True
    assert t.published == [
        ("xsphere/test/ai_control", {"online": 1, "armed_heaters": 0}, 1, True),
        ("xsphere/test/ai_control/audit", {"entries": [{"kind": "note"}]}, 1, True),
    ]
    assert bus.publish_status({"x": float("nan")}) is False          # never non-strict JSON
    assert bus.publish_status({"x": object()}) is False
    assert len(t.published) == 2


def test_responder_answers_commands(rig):
    """The hook the action tests use: a fake slowcontrol reacting to commands."""
    bus, t, _ = rig

    def fake_plc(topic, payload, transport):
        zone = topic.split("/")[3]
        transport.inject(f"xsphere/status/pid/{zone}",
                         {"setpoint_expr": repr(float(payload["value_k"]))})

    t.add_responder("xsphere/commands/pid/+/setpoint", fake_plc)
    assert bus.publish_command("xsphere/commands/pid/top/setpoint", {"value_k": 165})
    assert bus.raw("xsphere/status/pid/top")["payload"] == {"setpoint_expr": "165.0"}
    assert t.published_to("xsphere/commands/pid/#") == [
        ("xsphere/commands/pid/top/setpoint", {"value_k": 165}, 0, False)]


# ──────────────────────────────────────────────────────────────────────────
# bus — waiting
# ──────────────────────────────────────────────────────────────────────────

def test_wait_for_wakes_on_message(rig):
    bus, t, _ = rig
    topic = "xsphere/status/valve/cryostat"

    def later():
        time.sleep(0.1)
        t.inject(topic, {"state": 1})

    th = threading.Thread(target=later)
    start = time.monotonic()
    th.start()
    ok = bus.wait_for(lambda: (bus.raw(topic) or {}).get("payload", {}).get("state") == 1, 5.0)
    elapsed = time.monotonic() - start
    th.join()
    assert ok is True
    assert elapsed < 0.45          # woken by the message, not by the 0.5 s re-check


def test_wait_for_times_out(rig):
    bus, _, _ = rig
    start = time.monotonic()
    assert bus.wait_for(lambda: False, 0.3) is False
    assert 0.25 <= time.monotonic() - start < 1.5


def test_wait_for_true_immediately_and_zero_timeout(rig):
    bus, _, _ = rig
    assert bus.wait_for(lambda: True, 0) is True
    assert bus.wait_for(lambda: False, 0) is False


def test_wake_waiters(rig):
    bus, _, _ = rig
    flag = threading.Event()

    def later():
        time.sleep(0.1)
        flag.set()
        bus.wake_waiters()

    th = threading.Thread(target=later)
    start = time.monotonic()
    th.start()
    assert bus.wait_for(flag.is_set, 5.0)
    th.join()
    assert time.monotonic() - start < 0.45


# ──────────────────────────────────────────────────────────────────────────
# audit
# ──────────────────────────────────────────────────────────────────────────

def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_audit_record_appends_jsonl(tmp_path):
    clock = Clock()
    a = Audit(tmp_path / "audit.jsonl", clock=clock)
    e = a.record("action", "set top to 165 K", tool="set_heater_setpoints",
                 args={"setpoints": {"top": 165}}, reason="cool down", confirmed=True,
                 published=[{"topic": "t", "payload": {"value_k": 165.0}}], by=None)
    assert e["utc"] == "2026-10-22T06:20:00.000Z"
    assert e["kind"] == "action" and e["detail"] == "set top to 165 K"
    assert "by" not in e                                     # None fields dropped
    lines = read_lines(tmp_path / "audit.jsonl")
    assert lines == [e]
    a.record("note", "hello")
    assert [x["kind"] for x in read_lines(tmp_path / "audit.jsonl")] == ["action", "note"]


def test_audit_recent_and_ring(tmp_path):
    a = Audit(tmp_path / "audit.jsonl", ring=5)
    for i in range(8):
        a.record("note", f"n{i}")
    assert [e["detail"] for e in a.recent(3)] == ["n7", "n6", "n5"]
    assert [e["detail"] for e in a.recent(100)] == ["n7", "n6", "n5", "n4", "n3"]
    assert a.recent(0) == []
    a.recent(1)[0]["detail"] = "changed"
    assert a.recent(1)[0]["detail"] == "n7"
    assert len(read_lines(tmp_path / "audit.jsonl")) == 8    # the file keeps everything


def test_audit_reloads_tail_on_start(tmp_path):
    a = Audit(tmp_path / "audit.jsonl")
    for i in range(3):
        a.record("action", f"a{i}")
    with open(tmp_path / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write("garbage line\n")
    b = Audit(tmp_path / "audit.jsonl", ring=2)
    assert [e["detail"] for e in b.recent()] == ["a2", "a1"]


def test_audit_rotation(tmp_path):
    path = tmp_path / "audit.jsonl"
    a = Audit(path, rotate_bytes=400)
    for i in range(10):
        a.record("note", f"entry {i} " + "x" * 50)
    assert path.exists() and (tmp_path / "audit.jsonl.1").exists()
    assert path.stat().st_size <= 400
    assert (tmp_path / "audit.jsonl.1").stat().st_size <= 400
    both = read_lines(tmp_path / "audit.jsonl.1") + read_lines(path)
    assert both[-1]["detail"].startswith("entry 9")
    assert [e["detail"][:7] for e in a.recent(2)] == ["entry 9", "entry 8"]


def test_audit_never_writes_secrets_or_nan(tmp_path):
    a = Audit(tmp_path / "audit.jsonl")
    e = a.record("action", "x", args={"token": "tok-SECRET", "nested": {"Authorization": "S"},
                                      "value": float("nan"), "ok": [1, float("inf")]},
                 password="pw-SECRET", reason=("a", "b"))
    assert e["args"]["token"] == "[redacted]"
    assert e["args"]["nested"]["Authorization"] == "[redacted]"
    assert e["args"]["value"] is None and e["args"]["ok"] == [1, None]
    assert e["password"] == "[redacted]"
    assert e["reason"] == ["a", "b"]
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "SECRET" not in text and "NaN" not in text and "Infinity" not in text


def test_audit_unknown_kind_still_recorded(tmp_path, caplog):
    a = Audit(tmp_path / "audit.jsonl")
    assert "auto_close" in KINDS and "rails_refused" in KINDS
    with caplog.at_level("WARNING"):
        a.record("weird", "x")
    assert a.recent(1)[0]["kind"] == "weird"
    assert "unexpected kind" in caplog.text


def test_audit_survives_io_errors(tmp_path):
    (tmp_path / "audit.jsonl").mkdir()                       # a directory: every write fails
    a = Audit(tmp_path / "audit.jsonl")
    e = a.record("note", "still in memory")
    assert a.recent(1) == [e]


def test_audit_publish_is_throttled_and_catches_up(tmp_path):
    calls = []
    a = Audit(tmp_path / "audit.jsonl", publish=lambda entries: calls.append(entries) or True)
    a.record("note", "one")
    assert len(calls) == 1 and [e["detail"] for e in calls[0]] == ["one"]
    a.record("note", "two")
    a.record("note", "three")
    assert len(calls) == 1                                   # inside the 1 s window
    assert a.pending
    deadline = time.monotonic() + 3
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(calls) == 2                                   # the deferred publish
    assert [e["detail"] for e in calls[1]] == ["three", "two", "one"]
    assert not a.pending
    time.sleep(1.2)
    assert len(calls) == 2                                   # no extra publishes
    a.close()


def test_audit_publishes_last_50(tmp_path):
    calls = []
    a = Audit(tmp_path / "audit.jsonl", publish=lambda e: calls.append(e) or True)
    for i in range(60):
        a.record("note", f"n{i}")
    a.flush_publish(force=True)
    assert len(calls[-1]) == 50
    assert calls[-1][0]["detail"] == "n59" and calls[-1][-1]["detail"] == "n10"
    a.close()


def test_audit_failed_publish_stays_pending(tmp_path):
    state = {"ok": False, "n": 0}

    def publish(entries):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("broker gone")
        return state["ok"]

    a = Audit(tmp_path / "audit.jsonl", publish=publish)
    a.record("note", "x")                                    # raises inside: contained
    assert a.pending
    a.flush_publish(force=True)                              # returns False
    assert a.pending
    state["ok"] = True
    a.flush_publish(force=True)
    assert not a.pending
    a.close()


def test_audit_through_the_bus(rig, tmp_path):
    bus, t, _ = rig
    t.clear()
    a = Audit(tmp_path / "audit.jsonl", publish=bus.publish_audit)
    a.record("arm", "heaters armed", by="control page")
    topic, payload, qos, retain = t.published[-1]
    assert (topic, qos, retain) == ("xsphere/test/ai_control/audit", 1, True)
    assert payload["entries"][0]["detail"] == "heaters armed"
    assert payload["entries"][0]["by"] == "control page"
    a.close()
