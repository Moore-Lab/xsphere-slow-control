"""
USB-serial adapters (/dev/ttyUSB*) on this machine, for drivers that have to
work out which one is theirs.

Adapter chips such as the CH340 on an ESP32 dev board carry no serial number,
so two boards with the same chip look identical to the kernel: the same
vendor:product id, the same /dev/serial/by-id name (it ends up pointing at
whichever was plugged in last), and a ttyUSB number that follows plug-in
order.  The only thing that tells them apart on the USB side is the socket
each is in — and that changes whenever a cable is moved.

So a driver does not name a port at all.  It listens to each candidate adapter
and takes the one that prints what its device prints (drivers/levelprobe.py;
the turbo pumps are found the same way, by the serial number they answer
with).  This module lists the candidates and says where each one is.
"""

from __future__ import annotations

import glob
import os
from typing import List


def usb_id(tty: str) -> str:
    """'1a86:7523' for /dev/ttyUSB0 — the adapter chip's USB vendor:product
    id — or '' if it cannot be read."""
    node = os.path.realpath(f"/sys/class/tty/{os.path.basename(tty)}/device")
    for _ in range(4):
        try:
            with open(os.path.join(node, "idVendor")) as vendor, \
                    open(os.path.join(node, "idProduct")) as product:
                return f"{vendor.read().strip()}:{product.read().strip()}".lower()
        except OSError:
            node = os.path.dirname(node)
    return ""


def adapters(only_usb_id: str = "") -> List[str]:
    """The /dev/ttyUSB* devices present, optionally only those whose chip has
    this vendor:product id.  Never /dev/ttyACM* — those are instruments with a
    USB stack of their own (the turbo pumps), not adapters."""
    found = sorted(glob.glob("/dev/ttyUSB*"), key=lambda tty: (len(tty), tty))
    if only_usb_id:
        found = [tty for tty in found if usb_id(tty) == only_usb_id.lower()]
    return found


def plugged_in_at(tty: str) -> int:
    """Changes whenever the device behind this name is re-plugged or replaced
    (the device node is created anew); 0 if it is not there."""
    try:
        return os.stat(tty).st_ctime_ns
    except OSError:
        return 0


def describe(tty: str) -> str:
    """'/dev/ttyUSB0 (socket usb-0:1.3)' — the name plus which USB socket it
    is in, for log and status messages."""
    real = os.path.realpath(tty)
    for link in glob.glob("/dev/serial/by-path/*"):
        if os.path.realpath(link) == real:
            where = os.path.basename(link)
            start = where.find("usb-")
            where = where[start:] if start >= 0 else where
            return f"{tty} (socket {where.split(':1.0')[0]})"
    return tty
