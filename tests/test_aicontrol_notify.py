"""
Offline tests for the AI notifier (aicontrol/notify.py): phone alerts about
the AI control service and the disarm-only phone e-stop.

    python -m pytest tests/test_aicontrol_notify.py -q      (from the repo root)

MQTT runs on aicontrol.bus.LoopbackTransport and ntfy is a fake: no broker,
no network.  Time is a fake clock; the notifier is driven with tick() and
pump_outbox() instead of its threads.
"""

import io
import json
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from aicontrol import notify
from aicontrol.bus import LoopbackTransport
from aicontrol.config import ConfigError
from aicontrol.notify import (DISARM_ALL, GROUPS, P_DEFAULT, P_HIGH, P_LOW, P_URGENT,
                              EstopListener, Notifier, NotifySettings, NtfyClient, NtfyError,
                              fmt_dur, next_daily, next_weekly, parse_windows, split_message)

REPO = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")
ALERT = "alerts_" + "a" * 24
ESTOP = "estop_" + "b" * 24
AI_STATUS = "xsphere/status/ai_control"
AI_AUDIT = "xsphere/status/ai_control/audit"
ARM_TOPIC = "xsphere/commands/ai_control/arm"
OWN_STATUS = "xsphere/status/ai_notify"
PN_GATE = "xsphere/status/pneumatic_control"


def ny(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=NY).timestamp()


#: Wednesday 2026-10-21 12:00 New York: no digest, no warning, no window.
T0 = ny(2026, 10, 21, 12, 0)


def iso(ts, ms=False):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if ms:
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeNtfy:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.events = []
        self.stream_calls = []

    def publish(self, topic, *, message, title=None, priority=3, tags=(), actions=(),
                click=None):
        if self.fail:
            raise NtfyError("HTTP 503")
        self.sent.append({"topic": topic, "title": title, "message": message,
                          "priority": priority, "tags": list(tags), "actions": list(actions),
                          "click": click})
        return f"id{len(self.sent)}"

    def stream(self, topic, since=None):
        self.stream_calls.append((topic, since))
        for ev in self.events:
            if isinstance(ev, Exception):
                raise ev
            yield ev

    def titles(self):
        return [s["title"] for s in self.sent]

    def find(self, text):
        return [s for s in self.sent if text in s["title"]]


def make_settings(**kw):
    base = dict(alert_topic=ALERT, estop_topic=ESTOP, instance="production")
    base.update(kw)
    return NotifySettings(**base)


def status(armed=(), timers=None, ramps=None, started="2026-10-21T15:00:00Z",
           by="control page", since=None, expires=None, last_disarm=None):
    arms = {}
    for g in GROUPS:
        a = g in armed
        arms[g] = {"armed": a,
                   "since_utc": iso(since if since is not None else T0 - 600) if a else None,
                   "expires_utc": iso(expires) if (a and expires) else None,
                   "by": by if a else None, "last_disarm": last_disarm}
    st = {"online": 1, "instance": "production", "version": "1.0.0", "started_utc": started,
          "arms": arms, "ln2_timers": timers or {}, "ramps": ramps or [],
          "rails": {"revision": 3}, "rails_updated_by": "control page",
          "rails_updated_utc": "2026-10-21T14:00:00Z", "rails_error": None}
    for g in GROUPS:
        st[f"armed_{g}"] = int(g in armed)
    return st


OFFLINE_WILL = {"online": 0, "armed_heaters": 0, "armed_ln2": 0, "armed_pneumatic": 0}
CLEAN_STOP = dict(OFFLINE_WILL, instance="production", stopped_utc="2026-10-21T16:00:00Z")
TIMER = {"cryostat": {"opened_utc": "2026-10-21T15:55:00Z",
                      "deadline_utc": "2026-10-21T16:15:00Z"}}


def make_rig(t0=T0, *, connected_listener=True, **settings_kw):
    clock = Clock(t0)
    tr = LoopbackTransport()
    nt = FakeNtfy()
    n = Notifier(make_settings(**settings_kw), tr, nt, clock=clock)
    rig = SimpleNamespace(n=n, tr=tr, nt=nt, clock=clock, audit=[])
    return rig


def start(rig, connected_listener=True):
    rig.n.start(threads=False)
    if connected_listener:
        rig.n.estop_listener.connected = True
        rig.n.estop_listener.down_since = None
    rig.n.tick()
    rig.n.pump_outbox()
    return rig


@pytest.fixture
def rig():
    r = start(make_rig())
    assert r.nt.titles() == ["xsphere: notifier started"]
    r.nt.sent.clear()
    return r


def live(r, payload):
    r.tr.inject(AI_STATUS, payload, retain=False)


def step(r, seconds, every=1.0, st=None):
    """Advance the clock, ticking every ``every`` s; inject a live status each
    tick when ``st`` is given."""
    t = 0.0
    while t < seconds - 1e-9:
        dt = min(every, seconds - t)
        r.clock.advance(dt)
        t += dt
        if st is not None:
            live(r, st)
        r.n.tick()
    r.n.pump_outbox()


def entry(r, kind, detail, ts=None, **fields):
    e = {"utc": iso(r.clock() if ts is None else ts, ms=True), "kind": kind, "detail": detail}
    e.update(fields)
    return e


def audit(r, *entries):
    """Publish like aicontrol: the last 50, newest first."""
    for e in entries:
        r.audit.insert(0, e)
    r.tr.inject(AI_AUDIT, {"entries": r.audit[:50]})
    r.n.tick()
    r.n.pump_outbox()


def pushes(r):
    r.n.pump_outbox()
    return r.nt.sent


# ──────────────────────────────────────────────────────────────────────────
# settings
# ──────────────────────────────────────────────────────────────────────────

def test_from_env_requires_both_topics():
    with pytest.raises(ConfigError, match="NTFY_ALERT_TOPIC is not set"):
        NotifySettings.from_env({"NTFY_ESTOP_TOPIC": ESTOP})
    with pytest.raises(ConfigError, match="NTFY_ESTOP_TOPIC is not set"):
        NotifySettings.from_env({"NTFY_ALERT_TOPIC": ALERT})


@pytest.mark.parametrize("bad, match", [
    ("xsphere", "long random name"),
    ("has spaces in it but is long enough", "1–64 letters"),
    ("x" * 65, "1–64 letters"),
])
def test_topics_must_be_long_random_names(bad, match):
    with pytest.raises(ConfigError, match=match):
        make_settings(alert_topic=bad)


def test_alert_and_estop_topics_must_differ():
    # Otherwise every alert would come back on the e-stop stream and disarm.
    with pytest.raises(ConfigError, match="must be different"):
        make_settings(estop_topic=ALERT)


def test_production_and_test_ids_and_topics():
    p = make_settings()
    assert p.client_id == "xsphere-ainotify"
    assert (p.ai_status_topic, p.audit_topic, p.arm_topic, p.status_topic) == (
        AI_STATUS, AI_AUDIT, ARM_TOPIC, OWN_STATUS)
    t = make_settings(instance="test")
    assert t.client_id.startswith("xsphere-ainotify-test-") and t.client_id != p.client_id
    assert t.arm_topic == "xsphere/test/commands/ai_control/arm"
    assert t.ai_status_topic == "xsphere/test/ai_control"
    with pytest.raises(ConfigError, match="refuses MQTT host"):
        make_settings(instance="test", mqtt_host="xbox-pi")


def test_from_env_reads_everything():
    s = NotifySettings.from_env({
        "XSPHERE_NOTIFY_INSTANCE": "production", "XSPHERE_MQTT_HOST": "localhost",
        "XSPHERE_MQTT_PORT": "1884", "NTFY_SERVER": "https://ntfy.example.org/",
        "NTFY_ALERT_TOPIC": ALERT, "NTFY_ESTOP_TOPIC": ESTOP, "NTFY_TOKEN": "tk_secret",
        "NOTIFY_DAILY_DIGEST": "07:30", "NOTIFY_MAINTENANCE": "Sun 01:45-02:30; Wed 23:50-00:10",
        "NOTIFY_MAINTENANCE_WARN": "none", "NOTIFY_CLICK_URL": ""})
    assert s.mqtt_port == 1884 and s.ntfy_server == "https://ntfy.example.org"
    assert s.estop_url == f"https://ntfy.example.org/{ESTOP}"
    assert s.daily_digest == (7, 30) and len(s.maintenance) == 2
    assert s.maintenance_warn is None and s.click_url is None
    assert s.ntfy_token == "tk_secret"


def test_secrets_never_in_repr_or_startup_lines():
    s = make_settings(ntfy_token="tk_secret")
    text = repr(s) + "\n".join(s.describe())
    assert ALERT not in text and ESTOP not in text and "tk_secret" not in text
    assert "token set" in text


def test_expected_sources():
    s = make_settings()
    for by in ("control page", "Lucas @ 192.168.8.20 (browser)"):
        assert s.expected_by.fullmatch(by)
    for by in ("unknown", "curl", "Lucas @ 192.168.8.20 (curl)", "control page; rm"):
        assert not s.expected_by.fullmatch(by)


def test_parse_windows_and_bad_values():
    (w,) = parse_windows("Sun 01:45-02:30")
    assert (w.weekday, w.start, w.end) == (6, (1, 45), (2, 30))
    assert parse_windows("none") == []
    for bad in ("Sun 1:45", "Funday 01:00-02:00", "Sun 25:00-26:00", "Sun 01:00-01:00"):
        with pytest.raises(ConfigError):
            parse_windows(bad)
    with pytest.raises(ConfigError):
        make_settings(daily_digest="8am")


def test_group_map_matches_the_service():
    from aicontrol.actions import GROUP_OF
    assert notify.GROUP_OF == GROUP_OF


def test_imports_nothing_heavy_from_the_service():
    # A broken aicontrol deploy (or a missing Flask) must not stop the notifier.
    code = ("import sys, aicontrol.notify; "
            "bad = [m for m in ('flask', 'aicontrol.actions', 'aicontrol.app', "
            "'aicontrol.arms', 'aicontrol.rails') if m in sys.modules]; "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], cwd=REPO, check=True)


def test_unit_file():
    text = (REPO / "aicontrol" / "xsphere-ainotify.service").read_text(encoding="utf-8")
    assert "-m aicontrol.notify" in text
    assert "EnvironmentFile=/etc/xsphere/notify.env" in text
    assert "XSPHERE_NOTIFY_INSTANCE=production" in text
    assert "RestartPreventExitStatus=2" in text and "Restart=always" in text
    settings = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    for coupling in ("Requires=", "BindsTo=", "PartOf="):
        # it must outlive xsphere-aicontrol
        assert not any(ln.startswith(coupling) for ln in settings)


# ──────────────────────────────────────────────────────────────────────────
# time helpers
# ──────────────────────────────────────────────────────────────────────────

def test_next_daily_across_dst_end():
    sat = ny(2026, 10, 31, 8, 0)
    sun = next_daily(sat, NY, 8, 0)
    assert sun == ny(2026, 11, 1, 8, 0) and sun - sat == 25 * 3600
    assert next_daily(sat - 1, NY, 8, 0) == sat


def test_next_weekly_and_window_bounds():
    assert next_weekly(T0, NY, 5, 22, 0) == ny(2026, 10, 24, 22, 0)
    (w,) = parse_windows("Sun 01:45-02:30")
    assert w.bounds_around(ny(2026, 10, 25, 2, 0), NY) == (ny(2026, 10, 25, 1, 45),
                                                          ny(2026, 10, 25, 2, 30))
    assert w.bounds_around(ny(2026, 10, 25, 2, 30), NY) is None
    (late,) = parse_windows("Sat 23:50-00:10")       # crosses midnight
    assert late.bounds_around(ny(2026, 10, 25, 0, 5), NY)


def test_fmt_dur():
    assert fmt_dur(50) == "50 s" and fmt_dur(35 * 60) == "35 min"
    assert fmt_dur(4 * 3600 + 12 * 60) == "4 h 12 min"


# ──────────────────────────────────────────────────────────────────────────
# the AI service going away
# ──────────────────────────────────────────────────────────────────────────

def test_silence_alerts_once_then_back_online(rig):
    live(rig, status())
    step(rig, 25, st=status(), every=2)
    assert pushes(rig) == []
    step(rig, 40)                                    # nothing for 40 s
    (p,) = pushes(rig)
    assert p["title"] == "xsphere: AI control is DOWN" and p["priority"] == P_HIGH
    assert "silent" in p["message"] and "No LN2 timers were pending" in p["message"]
    step(rig, 120)
    assert len(pushes(rig)) == 1                     # once per outage
    step(rig, 25, every=2, st=status())
    assert rig.nt.titles()[-1] == "xsphere: AI control back online"


def test_crash_with_ln2_timer_pending_is_urgent(rig):
    live(rig, status(armed=("ln2",), timers=TIMER))
    live(rig, OFFLINE_WILL)                          # the broker's last will
    rig.n.tick()
    down = [p for p in pushes(rig) if "DOWN" in p["title"]]
    assert len(down) == 1 and down[0]["priority"] == P_URGENT
    msg = down[0]["message"]
    assert "connection lost" in msg and "LN2 timers NOT running: cryostat" in msg
    assert "Armed at its last status: ln2" in msg


def test_clean_restart_is_one_normal_message(rig):
    live(rig, status())
    live(rig, CLEAN_STOP)
    step(rig, 15)
    assert pushes(rig) == []                         # grace period
    step(rig, 30, every=2, st=status(started="2026-10-21T16:00:20Z"))
    titles = rig.nt.titles()
    assert titles == ["xsphere: AI control restarted"]
    assert rig.nt.sent[0]["priority"] == P_DEFAULT


def test_clean_stop_that_stays_down_alerts_after_grace(rig):
    live(rig, status())
    live(rig, CLEAN_STOP)
    step(rig, 59)
    assert pushes(rig) == []
    step(rig, 2)
    (p,) = pushes(rig)
    assert "DOWN" in p["title"] and "clean shutdown" in p["message"]


def test_crash_loop_alerts_once(rig):
    live(rig, status())
    for _ in range(5):
        live(rig, OFFLINE_WILL)
        step(rig, 10)
        live(rig, status())
        step(rig, 5, every=1, st=status())
    assert [p["title"] for p in pushes(rig)] == ["xsphere: AI control is DOWN"]
    step(rig, 25, every=2, st=status())
    assert rig.nt.titles()[-1] == "xsphere: AI control back online"
    assert "4 more restart(s)" in rig.nt.sent[-1]["message"]     # after the first crash


def test_offline_at_notifier_start_is_reported():
    r = make_rig()
    r.tr.inject(AI_STATUS, OFFLINE_WILL, retain=True)
    start(r)
    assert any("DOWN" in t for t in r.nt.titles())
    assert "already offline when the notifier started" in r.nt.find("DOWN")[0]["message"]


def test_no_status_at_all(rig):
    step(rig, 31)
    assert rig.nt.titles() == ["xsphere: no AI control status"]


def test_broker_loss_is_not_blamed_on_the_ai_service(rig):
    live(rig, status())
    rig.tr.set_connected(False)
    step(rig, 45)
    titles = [p["title"] for p in pushes(rig)]
    assert titles == ["xsphere: notifier lost the MQTT broker"]
    rig.tr.set_connected(True)
    step(rig, 10, every=2, st=status())
    assert rig.nt.titles()[-1] == "xsphere: notifier back on the broker"
    assert not any("DOWN" in t for t in rig.nt.titles())


# ──────────────────────────────────────────────────────────────────────────
# the Sunday maintenance window
# ──────────────────────────────────────────────────────────────────────────

def test_backup_outage_with_nothing_armed_is_silent():
    r = start(make_rig(ny(2026, 10, 25, 1, 59)))
    r.nt.sent.clear()
    live(r, status())
    step(r, 60, every=2, st=status())               # 02:00
    r.tr.set_connected(False)
    step(r, 14 * 60, every=5)                       # 02:14, broker down
    r.tr.set_connected(True)
    step(r, 20 * 60, every=2, st=status(started="2026-10-25T06:15:00Z"))   # past 02:30
    assert pushes(r) == []
    r.clock.t = ny(2026, 10, 25, 8, 0)
    live(r, status())
    r.n.tick()
    (digest,) = pushes(r)
    assert "inside the maintenance window" in digest["message"]


def test_backup_outage_that_outlasts_the_window_is_reported():
    r = start(make_rig(ny(2026, 10, 25, 2, 0)))
    r.nt.sent.clear()
    live(r, status())
    r.tr.set_connected(False)
    step(r, 29 * 60, every=10)                      # 02:29
    assert pushes(r) == []
    step(r, 2 * 60, every=10)                       # 02:31
    assert [p["title"] for p in pushes(r)] == ["xsphere: notifier lost the MQTT broker"]


def test_backup_outage_while_armed_is_reported_at_once():
    r = start(make_rig(ny(2026, 10, 25, 2, 0)))
    r.nt.sent.clear()
    live(r, status(armed=("heaters",)))
    r.tr.set_connected(False)
    step(r, 40)
    (p,) = pushes(r)
    assert p["title"] == "xsphere: notifier lost the MQTT broker" and "heaters" in p["message"]


def test_saturday_warning_only_when_armed():
    sat = ny(2026, 10, 24, 21, 59, 50)
    r = start(make_rig(sat))
    r.nt.sent.clear()
    step(r, 20, every=2, st=status(armed=("heaters",), expires=sat + 6 * 3600))
    (p,) = pushes(r)
    assert p["title"] == "xsphere: armed before the Sunday backup" and p["priority"] == P_HIGH
    assert "heaters (until Sun 25 Oct 03:59)" in p["message"]
    assert "Sun 25 Oct 01:45" in p["message"]

    r = start(make_rig(sat))
    r.nt.sent.clear()
    step(r, 20, every=2, st=status())
    assert pushes(r) == []


# ──────────────────────────────────────────────────────────────────────────
# audit entries
# ──────────────────────────────────────────────────────────────────────────

def test_history_before_start_is_not_alerted_but_a_restart_gap_is():
    r = make_rig()
    old = entry(r, "auto_disarm", "heaters disarmed — long ago", ts=T0 - 3600)
    recent = entry(r, "auto_disarm", "heaters disarmed — the PV interlock tripped",
                   ts=T0 - 60)
    r.tr.inject(AI_AUDIT, {"entries": [recent, old]}, retain=True)
    start(r)
    found = r.nt.find("auto-disarmed")
    assert len(found) == 1 and "PV interlock" in found[0]["message"]
    assert len(r.n.ring) == 2                         # both kept for the digests


@pytest.mark.parametrize("kind, detail, by, title, priority", [
    ("auto_disarm", "heaters disarmed — the PV interlock tripped (RTD3: 210 K)", None,
     "AI auto-disarmed", P_HIGH),
    ("auto_close", "LN2 cryostat: auto-closed after 20.1 min", None,
     "LN2 valve auto-closed by the AI service", P_HIGH),
    ("auto_close", "LN2 cryostat: still open after 3 close attempts — check the valve", None,
     "LN2: check the valve", P_URGENT),
    ("auto_close", "LN2 cryostat: … the service cannot tell whether the valve is open and "
                   "has not acted; check the valve", None, "LN2: check the valve", P_URGENT),
    ("arm", "heaters armed by control page — expires 2026-10-22T00:00:00Z (8 h)",
     "control page", "AI armed: heaters", P_DEFAULT),
    ("arm", "ln2 re-armed by Lucas @ 192.168.8.20 (browser) — no time limit",
     "Lucas @ 192.168.8.20 (browser)", "AI re-armed: ln2", P_DEFAULT),
    ("arm", "heaters armed by curl — expires …", "curl", "AI arm from an UNEXPECTED source",
     P_HIGH),
    ("arm", "arm command ignored: group must be heaters, ln2, pneumatic or all", "unknown",
     "AI arm from an UNEXPECTED source", P_HIGH),
    ("disarm", "heaters disarmed — disarmed by control page", "control page", "AI disarmed",
     P_DEFAULT),
    ("rails", "rails revision 4 saved by control page: zones.top.sp_max_k: 175 → 180; "
              "general.arm_hours: 8 → 4", "control page", "AI rails saved (revision 4)",
     P_DEFAULT),
    ("rails", "rails revision 5 saved by unknown: general.arm_hours: 4 → 48", "unknown",
     "AI rails saved from an UNEXPECTED source (revision 5)", P_HIGH),
    ("rails_refused", "rails save refused: base_revision 3 is not the current 4", "python",
     "AI rails save attempt from an UNEXPECTED source", P_HIGH),
])
def test_audit_alerts(rig, kind, detail, by, title, priority):
    audit(rig, entry(rig, kind, detail, by=by) if by else entry(rig, kind, detail))
    (p,) = pushes(rig)
    assert p["title"] == f"xsphere: {title}" and p["priority"] == priority


def test_rails_alert_lists_the_diff_one_change_per_line(rig):
    audit(rig, entry(rig, "rails", "rails revision 4 saved by control page: a: 1 → 2; b: x → y",
                     by="control page"))
    assert pushes(rig)[0]["message"] == "Saved by control page.\na: 1 → 2\nb: x → y"


@pytest.mark.parametrize("kind, detail, by", [
    ("disarm", "heaters disarmed — disarmed by phone e-stop", "phone e-stop"),
    ("rails_refused", "rails save refused: stale base revision", "control page"),
    ("action", "set_heater_setpoints top 170 K", None),
    ("note", "Holding at 170 K.", None),
    ("ramp", "ramp top reached 170 K after 12 step(s)", None),
])
def test_quiet_entries(rig, kind, detail, by):
    audit(rig, entry(rig, kind, detail, by=by) if by else entry(rig, kind, detail))
    assert pushes(rig) == []


def test_republished_audit_is_not_alerted_twice(rig):
    e = entry(rig, "auto_disarm", "heaters disarmed — the arm expired")
    audit(rig, e)
    rig.clock.advance(5)
    audit(rig)                                         # aicontrol restart: same entries
    rig.tr.inject(AI_AUDIT, {"entries": rig.audit}, retain=True)
    assert len(pushes(rig)) == 1


def test_refusal_storm(rig):
    def refuse(i):
        rig.clock.advance(30)
        live(rig, status())
        audit(rig, entry(rig, "refused", f"Heaters are not armed for AI control ({i})",
                         tool="set_heater_setpoints"))
    for i in range(4):
        refuse(i)
    assert pushes(rig) == []
    refuse(4)
    (p,) = pushes(rig)
    assert p["title"] == "xsphere: AI refusal storm: 5 in 10 min" and p["priority"] == P_HIGH
    assert "set_heater_setpoints ×5" in p["message"]
    refuse(5)
    assert len(pushes(rig)) == 1                       # cooldown


def test_spread_out_refusals_are_no_storm(rig):
    for i in range(8):
        rig.clock.advance(200)
        live(rig, status())
        audit(rig, entry(rig, "refused", f"not armed ({i})", tool="set_ln2_valve"))
    assert pushes(rig) == []


def test_counted_but_not_audited_summary_is_a_flood_alert(rig):
    audit(rig, entry(rig, "refused", "41 more call(s) that sent nothing … were counted but not "
                                     "audited one by one (over 30 a minute): get_x ×41",
                     unaudited=41))
    (p,) = pushes(rig)
    assert p["title"] == "xsphere: AI refusal flood" and p["priority"] == P_HIGH


# ──────────────────────────────────────────────────────────────────────────
# arm periods and digests
# ──────────────────────────────────────────────────────────────────────────

def test_arm_period_digest(rig):
    armed_at = rig.clock()
    live(rig, status(armed=("heaters",), since=armed_at))
    audit(rig, entry(rig, "arm", "heaters armed by control page — expires …",
                     by="control page"))
    rig.nt.sent.clear()
    note = "Holding top at 170 K.\nThe cube is 2 K colder than the target; waiting 30 min."
    for i, e in enumerate([
            entry(rig, "action", "set_heater_setpoints top 168 → 170 K",
                  tool="set_heater_setpoints"),
            entry(rig, "refused", "Heaters: top setpoint 300 K is above the rails' 175 K",
                  tool="set_heater_setpoints"),
            entry(rig, "action", "start_heater_ramp top → 172 K", tool="start_heater_ramp"),
            entry(rig, "note", note, tool="add_note"),
            entry(rig, "action", "set_heater_setpoints top 172 K", tool="set_heater_setpoints"),
            entry(rig, "ramp", "ramp top reached 172 K after 3 step(s)", zone="top")]):
        rig.clock.advance(600)
        live(rig, status(armed=("heaters",), since=armed_at))
        e["utc"] = iso(rig.clock(), ms=True)
        audit(rig, e)
    rig.clock.advance(60)
    live(rig, status(last_disarm={"reason": "disarmed by control page",
                                  "utc": iso(rig.clock())}))
    audit(rig, entry(rig, "disarm", "heaters disarmed — disarmed by control page",
                     by="control page"))
    assert [p["title"] for p in pushes(rig)] == ["xsphere: AI disarmed"]
    step(rig, 6)
    digest = rig.nt.sent[-1]
    assert digest["title"] == "xsphere: AI arm ended: heaters (61 min)"
    assert digest["priority"] == P_LOW
    msg = digest["message"]
    assert "heaters: armed 12:00 by control page, ended 13:01 after 61 min" in msg
    assert "disarmed by control page" in msg
    assert "set_heater_setpoints ×2, start_heater_ramp ×1" in msg
    assert "Refusals: 1:" in msg and "above the rails' 175 K" in msg
    assert "ramp top reached 172 K" in msg
    assert note in msg                                    # every note in full


def test_service_going_offline_ends_the_period(rig):
    live(rig, status(armed=("heaters", "ln2")))
    live(rig, OFFLINE_WILL)
    step(rig, 6)
    (digest,) = rig.nt.find("arm ended")
    assert digest["title"].startswith("xsphere: AI arm ended: heaters, ln2")
    assert "the AI control service went offline" in digest["message"]


def test_arm_digest_when_the_notifier_started_mid_arm():
    r = make_rig()
    r.tr.inject(AI_STATUS, status(armed=("pneumatic",), since=T0 - 7200), retain=True)
    start(r)
    live(r, status(armed=("pneumatic",), since=T0 - 7200))
    live(r, status(last_disarm={"reason": "the arm expired", "utc": iso(T0)}))
    step(r, 6)
    (digest,) = r.nt.find("arm ended")
    assert "pneumatic" in digest["title"] and "the arm expired" in digest["message"]
    assert "only has the activity it saw" in digest["message"]


def test_arm_lapsing_during_a_ramp_warns_once(rig):
    ramp = [{"zone": "top", "target_k": 175, "rate_k_per_min": 0.2, "current_k": 171}]
    st = status(armed=("heaters",), expires=T0 + 25 * 60, ramps=ramp)
    step(rig, 10, every=2, st=st)
    (p,) = pushes(rig)
    assert p["title"] == "xsphere: AI heaters arm lapses at 12:25"
    assert "top → 175 K stop when heaters disarm" in p["message"]


def test_arm_lapsing_with_nothing_running_is_quiet(rig):
    step(rig, 10, every=2, st=status(armed=("heaters",), expires=T0 + 25 * 60))
    assert pushes(rig) == []


def test_daily_digest_is_the_heartbeat(rig):
    live(rig, status())
    rig.tr.inject(PN_GATE, {"enabled": 1, "since_utc": iso(T0 - 21 * 3600), "refused": 0})
    rig.tr.inject("xsphere/status/turbo_control", {"enabled": 0, "since_utc": None})
    audit(rig, entry(rig, "arm", "heaters armed by control page — …", by="control page"),
          entry(rig, "action", "set_heater_setpoints top 170 K", tool="set_heater_setpoints"))
    rig.nt.sent.clear()
    thu_8 = ny(2026, 10, 22, 8, 0)
    rig.clock.t = thu_8 - 1
    live(rig, status())
    rig.n.tick()
    assert pushes(rig) == []
    rig.clock.t = thu_8
    live(rig, status())
    rig.n.tick()
    (p,) = pushes(rig)
    assert p["title"] == "xsphere: daily AI digest — Thu 22 Oct" and p["priority"] == P_LOW
    msg = p["message"]
    assert "AI control: online, version 1.0.0" in msg and "Armed: nothing" in msg
    assert "Rails: revision 3, saved by control page" in msg
    assert "Pneumatic valve control: ENABLED since Tue 20 Oct 15:00 (41 h 00 min)" in msg
    assert "Turbo pump control: disabled" in msg
    assert "set_heater_setpoints ×1" in msg
    assert "Next maintenance window: Sun 25 Oct 01:45" in msg
    assert "phone e-stop listening" in msg
    rig.clock.t = thu_8 + 3600
    live(rig, status())
    rig.n.tick()
    assert len(pushes(rig)) == 1                       # next one tomorrow


# ──────────────────────────────────────────────────────────────────────────
# the phone e-stop
# ──────────────────────────────────────────────────────────────────────────

def estop_event(r, mid="m1", sent=None):
    return {"event": "message", "id": mid, "topic": ESTOP, "message": "disarm",
            "time": int(r.clock() if sent is None else sent)}


def fake_aicontrol_answers(r):
    """Answer an arm command the way the service does: a status with nothing armed."""
    def respond(topic, payload, transport):
        if payload == DISARM_ALL:
            transport.inject(AI_STATUS, status())
    r.tr.add_responder(ARM_TOPIC, respond)


def test_estop_disarms_everything_and_confirms(rig):
    live(rig, status(armed=("heaters", "ln2")))
    fake_aicontrol_answers(rig)
    rig.n.handle_estop_event(estop_event(rig))
    assert rig.tr.published_to(ARM_TOPIC) == [(ARM_TOPIC, DISARM_ALL, 1, False)]
    (p,) = pushes(rig)
    assert p["title"] == "xsphere: AI disarmed by the phone e-stop"
    assert "Disarmed: heaters, ln2." in p["message"] and p["tags"] == ["white_check_mark"]


def test_estop_with_nothing_armed_says_the_button_works(rig):
    live(rig, status())
    fake_aicontrol_answers(rig)
    rig.n.handle_estop_event(estop_event(rig))
    assert "Nothing was armed — the button works." in pushes(rig)[0]["message"]


def test_estop_ignores_duplicates_old_taps_and_other_events(rig):
    live(rig, status())
    fake_aicontrol_answers(rig)
    rig.n.handle_estop_event(estop_event(rig, "m1"))
    rig.n.handle_estop_event(estop_event(rig, "m1"))                 # same id again
    rig.n.handle_estop_event(estop_event(rig, "m0", sent=T0 - 3600))  # before we started
    for ev in ({"event": "open"}, {"event": "keepalive"}, None, "disarm",
               dict(estop_event(rig, "m9"), topic="someone_elses_topic_xxxxxxxx")):
        rig.n.handle_estop_event(ev)
    assert len(rig.tr.published_to(ARM_TOPIC)) == 1


def test_estop_not_confirmed(rig):
    live(rig, status(armed=("heaters",)))
    rig.n.handle_estop_event(estop_event(rig))
    step(rig, 11)
    (p,) = pushes(rig)
    assert p["title"] == "xsphere: E-stop sent, disarm NOT confirmed" and p["priority"] == P_HIGH
    assert "heaters armed" in p["message"]


def test_estop_waits_for_the_broker(rig):
    live(rig, status(armed=("heaters",)))
    rig.tr.set_connected(False)
    rig.n.handle_estop_event(estop_event(rig))
    assert rig.tr.published == [] or rig.tr.published_to(ARM_TOPIC) == []
    assert rig.nt.titles() == [] and pushes(rig)[0]["title"] == (
        "xsphere: E-stop received — broker unreachable")
    rig.tr.set_connected(True)
    fake_aicontrol_answers(rig)
    step(rig, 6)
    assert rig.tr.published_to(ARM_TOPIC) == [(ARM_TOPIC, DISARM_ALL, 1, False)]
    assert rig.nt.titles()[-1] == "xsphere: AI disarmed by the phone e-stop"


def test_estop_gives_up_loudly(rig):
    rig.tr.set_connected(False)
    rig.n.handle_estop_event(estop_event(rig))
    step(rig, 601, every=5)
    assert "xsphere: E-stop NOT delivered" in rig.nt.titles()
    assert rig.nt.find("NOT delivered")[0]["priority"] == P_URGENT


def test_estop_listener_feeds_the_notifier(rig):
    live(rig, status(armed=("ln2",)))
    fake_aicontrol_answers(rig)
    rig.nt.events = [{"event": "open"}, {"event": "keepalive"}, estop_event(rig, "abc")]
    lis = rig.n.estop_listener
    lis.run_once()
    assert rig.tr.published_to(ARM_TOPIC) == [(ARM_TOPIC, DISARM_ALL, 1, False)]
    assert lis.since == "abc" and lis.connected is False
    assert lis.error == "the server closed the stream"
    assert rig.nt.stream_calls[0] == (ESTOP, str(int(T0)))
    lis.run_once()
    assert rig.nt.stream_calls[1] == (ESTOP, "abc")     # resumes after the last message


def test_estop_listener_down_is_reported(rig):
    lis = rig.n.estop_listener
    rig.nt.events = [NtfyError("HTTP 403: forbidden")]
    lis.run_once()
    assert lis.connected is False and lis.error == "HTTP 403: forbidden"
    live(rig, status())
    step(rig, 299, every=5, st=status())
    assert rig.nt.find("not listening") == []
    step(rig, 5, every=5, st=status())
    (p,) = rig.nt.find("not listening")
    assert "HTTP 403" in p["message"]


def test_only_two_things_are_ever_published(rig):
    live(rig, status(armed=("heaters",), timers=TIMER))
    fake_aicontrol_answers(rig)
    rig.n.handle_estop_event(estop_event(rig))
    live(rig, OFFLINE_WILL)
    step(rig, 120)
    topics = {t for t, _p, _q, _r in rig.tr.published}
    assert topics <= {ARM_TOPIC, OWN_STATUS}
    assert all(p == DISARM_ALL and not retain for t, p, _q, retain in rig.tr.published
               if t == ARM_TOPIC)
    own = rig.tr.retained_payload(OWN_STATUS)
    assert own["online"] == 1 and ALERT not in json.dumps(own) and ESTOP not in json.dumps(own)
    for topic, payload in ((ARM_TOPIC, {"group": "heaters", "armed": True, "by": "x"}),
                           (ARM_TOPIC, dict(DISARM_ALL, group="heaters")),
                           ("xsphere/commands/ai_control/rails", {"rails": {}}),
                           ("xsphere/commands/pneumatic_control/enable", {"enabled": False})):
        with pytest.raises(ValueError):
            rig.n._publish(topic, payload, qos=1, retain=False)
    with pytest.raises(ValueError):
        rig.n._publish(ARM_TOPIC, DISARM_ALL, qos=1, retain=True)


def test_every_push_carries_the_disarm_button(rig):
    live(rig, status())
    live(rig, OFFLINE_WILL)
    audit(rig, entry(rig, "auto_disarm", "x"))
    sent = pushes(rig)
    assert sent
    for p in sent:
        assert p["topic"] == ALERT
        assert p["actions"] == [{"action": "http", "label": "Disarm AI",
                                 "url": f"https://ntfy.sh/{ESTOP}", "method": "POST",
                                 "body": "disarm", "clear": True}]
        assert p["click"] == "http://xbox-pi:8088/control"


def test_shutdown_says_so_and_goes_offline(rig):
    rig.n.shutdown()
    assert rig.nt.titles()[-1] == "xsphere: notifier stopped"
    assert rig.tr.retained_payload(OWN_STATUS)["online"] == 0


# ──────────────────────────────────────────────────────────────────────────
# sending
# ──────────────────────────────────────────────────────────────────────────

def test_failed_push_is_retried_and_marked_late(rig):
    rig.nt.fail = True
    audit(rig, entry(rig, "auto_disarm", "heaters disarmed — the arm expired"))
    assert rig.n.push_stats["failed"] == 1 and len(rig.n.outbox) == 1
    rig.nt.fail = False
    rig.clock.advance(10)
    assert rig.n.pump_outbox() == 0                    # waits PUSH_RETRY_S
    rig.clock.advance(90)
    assert rig.n.pump_outbox() == 1
    assert rig.nt.sent[0]["message"].startswith("(Delayed: this happened at Wed 21 Oct 12:00.)")


def test_very_old_pushes_are_dropped(rig):
    rig.nt.fail = True
    audit(rig, entry(rig, "auto_disarm", "x"))
    rig.nt.fail = False
    rig.clock.advance(7 * 3600)
    assert rig.n.pump_outbox() == 0 and rig.n.push_stats["dropped"] == 1


def test_push_budget_holds_back_and_sums_up(rig):
    left = notify.PUSH_BUDGET_PER_HOUR - len(rig.n._sent_times)   # "started" used one
    for i in range(left + 5):
        rig.n._push(P_DEFAULT, f"alert {i}", "m", key="t")
    rig.n._push(P_URGENT, "urgent one", "m", key="u")
    rig.n.pump_outbox()
    titles = rig.nt.titles()
    assert len(titles) == left + 1 and titles[-1] == "xsphere: urgent one"   # urgent bypasses
    rig.n.pump_outbox()
    assert len(rig.nt.titles()) == left + 1                     # the rest waits
    rig.clock.advance(3601)
    rig.n.pump_outbox()
    assert rig.nt.titles()[-1] == "xsphere: 5 more alert(s)"
    assert f"alert {left + 4}" in rig.nt.sent[-1]["message"]


def test_long_messages_are_split():
    text = "\n".join(f"line {i} " + "é" * 50 for i in range(200))
    parts = split_message(text)
    assert len(parts) > 1 and "\n".join(parts) == text
    assert all(len(p.encode("utf-8")) <= notify.MAX_MESSAGE_BYTES for p in parts)
    (one,) = split_message("short")
    huge = split_message("x" * 10000)
    assert "".join(huge) == "x" * 10000 and all(len(p) <= notify.MAX_MESSAGE_BYTES for p in huge)


def test_split_push_titles(rig):
    notes = [entry(rig, "note", f"note {i}: " + "words " * 150, tool="add_note")
             for i in range(10)]
    live(rig, status(armed=("heaters",), since=T0 - 60))
    audit(rig, *notes)
    live(rig, status(last_disarm={"reason": "disarmed by control page", "utc": iso(T0)}))
    step(rig, 6)
    titles = [t for t in rig.nt.titles() if "arm ended" in t]
    assert len(titles) >= 2 and titles[0].endswith(f"(1/{len(titles)})")


# ──────────────────────────────────────────────────────────────────────────
# the ntfy client (urllib with a fake opener)
# ──────────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, body=b"", lines=None, status=200):
        self.status = status
        self._body = body
        self._lines = lines or []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True

    def read(self, n=-1):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


def test_ntfy_publish_request():
    seen = []

    def opener(req, timeout):
        seen.append((req, timeout))
        return FakeResponse(b'{"id":"abc123","event":"message"}')
    c = NtfyClient("https://ntfy.sh/", "tk_x", opener=opener)
    mid = c.publish(ALERT, title="t", message="héllo", priority=P_HIGH, tags=["warning"],
                    actions=[{"action": "http"}], click="http://x")
    req, timeout = seen[0]
    assert mid == "abc123" and timeout == c.timeout
    assert req.full_url == "https://ntfy.sh/" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tk_x"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"topic": ALERT, "message": "héllo", "priority": 4, "title": "t",
                    "tags": ["warning"], "actions": [{"action": "http"}], "click": "http://x"}


def test_ntfy_publish_errors():
    def http_429(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {},
                                     io.BytesIO(b'{"error":"limit reached: too many requests"}'))
    with pytest.raises(NtfyError) as ei:
        NtfyClient("https://ntfy.sh", opener=http_429).publish(ALERT, message="m")
    assert str(ei.value) == "HTTP 429: limit reached: too many requests"
    assert ALERT not in str(ei.value)

    def offline(req, timeout):
        raise urllib.error.URLError("Temporary failure in name resolution")
    with pytest.raises(NtfyError, match="URLError"):
        NtfyClient("https://ntfy.sh", opener=offline).publish(ALERT, message="m")

    no_auth = []
    NtfyClient("https://ntfy.sh", opener=lambda r, timeout: no_auth.append(r) or
               FakeResponse(b"{}")).publish(ALERT, message="m")
    assert no_auth[0].get_header("Authorization") is None


def test_ntfy_stream():
    seen = []
    lines = [b'{"event":"open"}\n', b"\n", b"not json\n", b'["a list"]\n',
             b'{"event":"message","id":"x1","message":"disarm"}\n']
    resp = FakeResponse(lines=lines)

    def opener(req, timeout):
        seen.append((req, timeout))
        return resp
    c = NtfyClient("https://ntfy.sh", opener=opener)
    events = list(c.stream(ESTOP, since="1792650000"))
    assert [e["event"] for e in events] == ["open", "message"]
    req, timeout = seen[0]
    assert req.full_url == f"https://ntfy.sh/{ESTOP}/json?since=1792650000"
    assert timeout == c.stream_timeout and resp.closed


def test_ntfy_stream_errors_become_ntfy_errors():
    class Broken(FakeResponse):
        def __iter__(self):
            yield b'{"event":"open"}\n'
            raise TimeoutError("timed out")
    c = NtfyClient("https://ntfy.sh", opener=lambda r, timeout: Broken())
    it = c.stream(ESTOP)
    assert next(it)["event"] == "open"
    with pytest.raises(NtfyError, match="stream broken"):
        next(it)


def test_listener_survives_any_error():
    calls = []
    lis = EstopListener(SimpleNamespace(stream=lambda t, s: (_ for _ in ()).throw(
        RuntimeError("boom"))), ESTOP, calls.append, since=T0)
    lis.run_once()
    assert lis.error == "RuntimeError: boom" and not lis.connected and calls == []


def test_paho_client_has_its_own_will():
    c = notify.NotifyTransport(make_settings())._build_client()
    assert c._client_id == b"xsphere-ainotify"
    assert (c._will_topic, c._will_payload, c._will_retain) == (
        OWN_STATUS.encode(), b'{"online": 0}', True)


def test_a_clock_jump_skips_the_schedule_instead_of_sending(rig):
    # The Pi boots with an old clock; NTP then moves it hours ahead.
    rig.clock.t = ny(2026, 10, 22, 11, 0)               # 3 h past Thursday 08:00
    live(rig, status())
    rig.n.tick()
    assert pushes(rig) == []
    rig.clock.t = ny(2026, 10, 23, 8, 0)
    live(rig, status())
    rig.n.tick()
    assert [p["title"] for p in pushes(rig)] == ["xsphere: daily AI digest — Fri 23 Oct"]


def test_threads_end_to_end():
    """Real threads and the real clock: a tap on the stream disarms and confirms."""
    import threading
    import time
    tr = LoopbackTransport()
    nt = FakeNtfy()
    n = Notifier(make_settings(), tr, nt)
    r = SimpleNamespace(n=n, tr=tr, nt=nt, clock=time.time)
    fake_aicontrol_answers(r)
    tr.inject(AI_STATUS, status(armed=("heaters",)), retain=True)
    nt.events = [{"event": "open"}, estop_event(r, "t1")]
    before = set(threading.enumerate())
    n.start(threads=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not nt.find("by the phone e-stop"):
        time.sleep(0.05)
    n.shutdown()
    assert nt.find("by the phone e-stop"), nt.titles()
    assert tr.published_to(ARM_TOPIC) == [(ARM_TOPIC, DISARM_ALL, 1, False)]
    for t in set(threading.enumerate()) - before:
        t.join(timeout=5)
        assert not t.is_alive(), t.name


def test_main_check_and_bad_config(tmp_path, monkeypatch, capsys):
    env = tmp_path / "notify.env"
    env.write_text(f"NTFY_ALERT_TOPIC={ALERT}\nNTFY_ESTOP_TOPIC={ESTOP}\n", encoding="utf-8")
    for k in ("NTFY_ALERT_TOPIC", "NTFY_ESTOP_TOPIC", "XSPHERE_NOTIFY_INSTANCE"):
        monkeypatch.delenv(k, raising=False)
    assert notify.main(["--env-file", str(env), "--check"]) == 0
    env.write_text("NTFY_ALERT_TOPIC=short\n", encoding="utf-8")
    assert notify.main(["--env-file", str(env), "--check"]) == 2
    assert notify.main(["--env-file", str(tmp_path / "missing.env"), "--check"]) == 2
