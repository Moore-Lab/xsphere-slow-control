"""
Who is asking: Lucas's one-time codes for arming and rails, and per-client
write tokens for the AI's tool calls.

**One-time codes (RFC 6238 TOTP).**  An arm and a rails save are MQTT messages
on an anonymous broker, so anything on the lab network could send one.  Each
must therefore carry a 6-digit code from Lucas's authenticator app, checked
here against a secret only this service and his phone hold
(``XSPHERE_AICONTROL_TOTP_SECRET``, base32, from the root-only
``/etc/xsphere/aicontrol.env``).  HMAC-SHA1, 30 s steps, 6 digits — what every
authenticator app does.  The code of the current step and of one step either
side is accepted (clock drift), and only for a step later than the last one
accepted, so a code seen on the wire cannot be used again.  After
:data:`MAX_FAILURES` wrong codes within :data:`FAILURE_WINDOW_S` every code is
refused for :data:`LOCKOUT_S` (6 digits do not survive a flood of guesses).  A
disarm never needs a code.  With no secret configured nothing can be armed and
the rails cannot be saved.

**Client tokens.**  ``XSPHERE_AICONTROL_CLIENTS`` lists ``name:sha256hex``
pairs (comma-separated).  An MCP or REST request carrying ``Authorization:
Bearer <token>`` whose SHA-256 matches one of them acts as that client; any
other request is anonymous.  Anonymous callers can read; every write tool
refuses them (a tool result, never HTTP 401 — Claude Code would take a 401 as
an OAuth challenge).  Only hashes are kept here, and nothing in this module
logs or returns a secret.

Lucas creates both secrets himself (``python -m aicontrol.enroll``, in his own
terminal) — never through a Claude session, where they would end up in a
transcript.
"""

from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import hmac
import logging
import re
import struct
import threading
import time
from typing import Callable, Deque, Dict, Optional, Tuple

log = logging.getLogger(__name__)

STEP_S = 30
DIGITS = 6
#: Steps either side of "now" that are still accepted.
WINDOW = 1
MIN_SECRET_BYTES = 10            # 80 bits; enroll.py makes 20 (160, as RFC 4226 advises)
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300.0
LOCKOUT_S = 300.0

CLIENT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_CODE_RE = re.compile(r"[0-9]{6}")


def decode_secret(text: Optional[str]) -> Optional[bytes]:
    """Base32 text (spaces and case ignored, padding optional) → bytes, or
    None when missing, malformed or shorter than :data:`MIN_SECRET_BYTES`."""
    if not isinstance(text, str):
        return None
    s = re.sub(r"[\s-]", "", text).upper().rstrip("=")
    if not s:
        return None
    try:
        raw = base64.b32decode(s + "=" * (-len(s) % 8))
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) >= MIN_SECRET_BYTES else None


def totp_code(secret: bytes, step: int, digits: int = DIGITS) -> str:
    """The RFC 6238 / RFC 4226 code for one time step."""
    mac = hmac.new(secret, struct.pack(">Q", int(step)), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{value:0{digits}d}"


class Totp:
    """Checks Lucas's one-time codes.  Thread-safe."""

    def __init__(self, secret: Optional[bytes], *, clock: Callable[[], float] = time.time,
                 mono: Optional[Callable[[], float]] = None):
        self._secret = secret
        self._clock = clock
        self._mono = mono if mono is not None else (time.monotonic if clock is time.time
                                                    else clock)
        self._lock = threading.Lock()
        self._last_step: Optional[int] = None
        self._failures: Deque[float] = collections.deque()
        self._locked_until: Optional[float] = None
        self.refused_while_locked = 0

    @property
    def configured(self) -> bool:
        return self._secret is not None

    def locked_for(self) -> float:
        """Seconds the lockout still runs (0 when not locked)."""
        with self._lock:
            return self._locked_left_locked()

    def _locked_left_locked(self) -> float:
        if self._locked_until is None:
            return 0.0
        left = self._locked_until - self._mono()
        if left <= 0:
            self._locked_until = None
            return 0.0
        return left

    def verify(self, code) -> Tuple[bool, str]:
        """``(True, "")`` for a valid, unused code (and it is then used up),
        else ``(False, why)`` — ``why`` is safe to show and to audit."""
        if self._secret is None:
            return False, ("no one-time-code secret is configured on the AI control service "
                           "(XSPHERE_AICONTROL_TOTP_SECRET) — nothing can be armed and the "
                           "rails cannot be saved until it is enrolled")
        with self._lock:
            left = self._locked_left_locked()
            if left > 0:
                self.refused_while_locked += 1
                return False, (f"too many wrong codes — codes are refused for another "
                               f"{left:.0f} s")
            if not isinstance(code, str) or not _CODE_RE.fullmatch(code.strip()):
                return self._fail("the code must be the 6 digits from the authenticator app")
            code = code.strip()
            now_step = int(self._clock() // STEP_S)
            matched = None
            for step in range(now_step - WINDOW, now_step + WINDOW + 1):
                if hmac.compare_digest(totp_code(self._secret, step), code):
                    matched = step
                    break
            if matched is None:
                return self._fail("wrong or expired code")
            if self._last_step is not None and matched <= self._last_step:
                return self._fail("this code was already used (each code works once) — wait "
                                  "for the next one")
            self._last_step = matched
            self._failures.clear()
            return True, ""

    def _fail(self, why: str) -> Tuple[bool, str]:
        now = self._mono()
        while self._failures and now - self._failures[0] > FAILURE_WINDOW_S:
            self._failures.popleft()
        self._failures.append(now)
        if len(self._failures) >= MAX_FAILURES:
            self._locked_until = now + LOCKOUT_S
            self._failures.clear()
            why += (f"; {MAX_FAILURES} wrong codes within {FAILURE_WINDOW_S / 60:.0f} min — "
                    f"every code is refused for {LOCKOUT_S / 60:.0f} min")
        return False, why


def token_hash(token: str) -> str:
    """SHA-256 hex of a client token (what XSPHERE_AICONTROL_CLIENTS holds)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ClientTokens:
    """``name → sha256 hex`` of each AI client's write token."""

    def __init__(self, hashes: Optional[Dict[str, str]] = None):
        self._hashes: Dict[str, str] = dict(hashes or {})

    @classmethod
    def parse(cls, spec: Optional[str]) -> Tuple["ClientTokens", list]:
        """``"operator:<hex>,dev:<hex>"`` → (tokens, problems).  Bad entries
        are skipped and named in ``problems`` (never with the hash shown)."""
        hashes: Dict[str, str] = {}
        problems = []
        for i, part in enumerate(p.strip() for p in (spec or "").split(",")):
            if not part:
                continue
            name, sep, digest = part.partition(":")
            name, digest = name.strip(), digest.strip().lower()
            if not sep or not CLIENT_NAME_RE.fullmatch(name):
                problems.append(f"entry {i + 1}: the client name must match "
                                f"{CLIENT_NAME_RE.pattern}")
            elif not _HEX64_RE.fullmatch(digest):
                problems.append(f"client {name}: not a SHA-256 hex digest")
            elif name in hashes:
                problems.append(f"client {name}: listed twice")
            else:
                hashes[name] = digest
        return cls(hashes), problems

    def names(self) -> list:
        return sorted(self._hashes)

    def __len__(self) -> int:
        return len(self._hashes)

    def resolve(self, authorization: Optional[str]) -> Optional[str]:
        """The client name for an ``Authorization: Bearer <token>`` header, else None."""
        if not isinstance(authorization, str):
            return None
        scheme, _sp, token = authorization.strip().partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token or len(token) > 512:
            return None
        digest = token_hash(token)
        found = None
        for name, h in self._hashes.items():      # compare all: no early exit
            if hmac.compare_digest(h, digest):
                found = name
        return found

    def known(self, name: Optional[str]) -> bool:
        return isinstance(name, str) and name in self._hashes


def parse_clients_or_log(spec: Optional[str]) -> ClientTokens:
    tokens, problems = ClientTokens.parse(spec)
    for p in problems:
        log.error("XSPHERE_AICONTROL_CLIENTS: %s — entry ignored", p)
    return tokens
