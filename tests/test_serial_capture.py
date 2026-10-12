"""
Offline tests for the serial console capture tool.

    python -m pytest tests/test_serial_capture.py -q          (from the repo root)

The device lines are what the gas handling cart's firmware prints.
"""

import os
import re

import pytest

from slowcontrol.tools import serial_capture as sc
from slowcontrol.tools.serial_capture import Capture, DailyLog

ROUTINE = re.compile(r"^(Temperature: |Pressure: |CH[0-9])")
BLOCK = (b"Temperature: 23.80 \xc2\xb0C, Humidity: 47.00 %\r\n"
         b"Pressure: 1006.77 hPa\r\n"
         b"CH0: 0.01 V  ->  0.75 mbar\r\n"
         b"CH4 (legacy level): 0.01 V\r\n"
         b"\r\n")


@pytest.fixture
def cap():
    lines, events = [], []
    capture = Capture(lines.append, events.append, ROUTINE, silence_s=15.0)
    capture.lines, capture.events = lines, events
    capture.opened(0.0)
    return capture


def test_routine_lines_go_to_the_file_only(cap):
    cap.feed(BLOCK, 1.0)
    assert cap.lines == ["Temperature: 23.80 °C, Humidity: 47.00 %", "Pressure: 1006.77 hPa",
                         "CH0: 0.01 V  ->  0.75 mbar", "CH4 (legacy level): 0.01 V"]
    assert cap.events == []


def test_other_lines_are_events_and_repeats_are_counted(cap):
    retry = b"Attempting MQTT connection...failed, rc=-2 retrying in 5 seconds.\r\n"
    for i in range(4):
        cap.feed(retry, 1.0 + 5 * i)
    assert cap.events == ["Attempting MQTT connection...failed, rc=-2 retrying in 5 seconds."]
    cap.feed(b"Attempting MQTT connection...connected.\r\n", 30.0)
    assert cap.events[1:] == ["(the line above came 3 more times)",
                              "Attempting MQTT connection...connected."]
    assert len(cap.lines) == 5                  # the file still has every line


def test_a_repeat_is_printed_again_after_the_window(cap):
    cap.feed(b"Failed to read from DHT11 sensor.\r\n", 1.0)
    cap.feed(b"Failed to read from DHT11 sensor.\r\n", 2.0)
    cap.feed(b"Failed to read from DHT11 sensor.\r\n", 2.0 + sc.REPEAT_WINDOW_S)
    assert cap.events == ["Failed to read from DHT11 sensor.", "(the line above came 1 more time)",
                          "Failed to read from DHT11 sensor."]


def test_line_split_across_reads(cap):
    cap.feed(b"Failed to find BMP", 1.0)
    cap.tick(1.5)
    assert cap.lines == []
    cap.feed(b"3XX sensor.\r\n", 1.6)
    assert cap.lines == ["Failed to find BMP3XX sensor."]


def test_output_stopping_and_resuming(cap):
    cap.feed(BLOCK, 1.0)
    cap.tick(10.0)
    assert cap.events == []
    cap.tick(16.5)
    cap.tick(60.0)                              # reported once, not every tick
    assert cap.events == ["no output for 16 s - last line: CH4 (legacy level): 0.01 V"]
    cap.feed(BLOCK, 100.0)
    assert cap.events[1:] == ["output resumed after 99 s"]
    assert "# output resumed after 99 s" in cap.lines


def test_silent_from_the_moment_the_port_was_opened(cap):
    cap.tick(20.0)
    assert cap.events == ["no output for 20 s - last line: (nothing since the port was opened)"]


def test_unfinished_line_is_logged(cap):
    cap.feed(b"Attempting MQTT connection...", 1.0)
    cap.tick(2.0)
    assert cap.lines == []
    cap.tick(3.5)
    assert cap.lines == ["Attempting MQTT connection...  [no line end yet]"]
    assert cap.events == ["Attempting MQTT connection..."]


def test_dots_without_a_line_end(cap):
    # setup() waiting for Wi-Fi prints a dot every 500 ms and no newline
    cap.feed(b"Connecting to WiFi", 1.0)
    t = 1.0
    while t < 32.0:
        t += 0.5
        cap.feed(b".", t)
        cap.tick(t)
    assert cap.lines[0].startswith("Connecting to WiFi....")
    assert len(cap.lines) == 3 and all(line.endswith("[no line end yet]") for line in cap.lines)
    assert cap.events[0].startswith("Connecting to WiFi....")
    assert not any("no output" in e for e in cap.events)


def test_boot_garbage_is_flagged(cap):
    cap.feed(bytes([0xfe, 0x1c, 0x00, 0xe0, 0x9c, 0xfc, 0x8e, 0x70, 0x1c, 0xff]) + b"\r\n"
             b"Connecting to WiFi\r\n", 1.0)
    assert cap.events == ["unreadable bytes - what the boot text after a reset looks like",
                          "Connecting to WiFi"]
    assert cap.lines[0].startswith("# 10 unreadable bytes: b'")


def test_daily_log_stamps_lines_and_prunes(tmp_path):
    old = tmp_path / "ghs-20200101.log"
    old.write_text("old\n")
    other = tmp_path / "other-20200101.log"
    other.write_text("not ours\n")
    log = DailyLog(str(tmp_path), "ghs", keep_days=14)
    log.write("CH0: 0.01 V  ->  0.75 mbar")
    files = sorted(os.listdir(tmp_path))
    assert not old.exists() and other.exists() and len(files) == 2
    text = (tmp_path / [f for f in files if f.startswith("ghs-")][0]).read_text(encoding="utf-8")
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d\d\d CH0: 0.01 V  ->  0.75 mbar\n", text)


def test_only_usb_serial_adapters_are_candidates(monkeypatch):
    ttys = {"/dev/serial/by-path/a-usb-0:1.2:1.0": "/dev/ttyACM0",          # a turbo pump
            "/dev/serial/by-path/a-usb-0:1.3:1.0-port0": "/dev/ttyUSB0"}
    monkeypatch.setattr(sc.glob, "glob", lambda pattern: list(ttys))
    monkeypatch.setattr(sc.os.path, "realpath", lambda p: ttys[p])
    assert sc.usb_serial_adapters() == {"/dev/serial/by-path/a-usb-0:1.3:1.0-port0": "ttyUSB0"}
