"""
Make the AI control service's secrets — for Lucas to run in his own terminal.

    python -m aicontrol.enroll totp            # the one-time-code secret (arming, rails)
    python -m aicontrol.enroll token operator  # a write token for the AI client "operator"
    python -m aicontrol.enroll check [FILE]    # test a code from the phone against FILE

Never run this through a Claude session: whatever it prints would end up in
that session's transcript.  Nothing here writes a file; it prints what to put
where, once.

``totp`` prints a fresh random secret (160 bits, base32) as the line for
``/etc/xsphere/aicontrol.env`` and as an ``otpauth://`` link; with ``qrencode``
installed it also draws the QR code to scan with an authenticator app.

``token NAME`` prints a fresh random token (for ``XSPHERE_AI_TOKEN`` on the
machine that runs that client) and the ``NAME:sha256`` entry for
``XSPHERE_AICONTROL_CLIENTS`` in the env file — the service keeps only the hash.

``check`` reads the env file (default ``/etc/xsphere/aicontrol.env``; it is
root-only, so run it with sudo), asks for a code and says whether the service
would accept it now.  It checks only; it uses nothing up.

Standard library only (plus ``qrencode`` if present), so it runs on the Pi and
on the DAQ PC alike.
"""

from __future__ import annotations

import base64
import getpass
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from typing import List, Optional

from .auth import CLIENT_NAME_RE, STEP_S, WINDOW, ClientTokens, decode_secret, token_hash, totp_code

DEFAULT_ENV_FILE = "/etc/xsphere/aicontrol.env"


def new_totp_secret() -> str:
    """20 random bytes as unpadded base32 (what authenticator apps take)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def otpauth_uri(secret_b32: str, account: str = "aicontrol", issuer: str = "xsphere") -> str:
    """The ``otpauth://totp/...`` link an authenticator app imports (SHA-1, 6 digits, 30 s)."""
    label = urllib.parse.quote(f"{issuer}:{account}")
    query = urllib.parse.urlencode({"secret": secret_b32, "issuer": issuer,
                                    "algorithm": "SHA1", "digits": 6, "period": STEP_S})
    return f"otpauth://totp/{label}?{query}"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def client_entry(name: str, token: str) -> str:
    """``name:sha256hex`` — one entry of XSPHERE_AICONTROL_CLIENTS."""
    if not CLIENT_NAME_RE.fullmatch(name):
        raise ValueError(f"client name must match {CLIENT_NAME_RE.pattern}")
    return f"{name}:{token_hash(token)}"


def read_env_file(path: str) -> dict:
    out = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key.strip()] = value
    return out


def code_ok_now(secret: bytes, code: str, now: Optional[float] = None) -> bool:
    step = int((time.time() if now is None else now) // STEP_S)
    return any(totp_code(secret, s) == code.strip()
               for s in range(step - WINDOW, step + WINDOW + 1))


def _cmd_totp(_args: List[str]) -> int:
    secret = new_totp_secret()
    uri = otpauth_uri(secret)
    print("One-time-code secret for the AI control service — shown ONCE.\n")
    print("1. Put this line into /etc/xsphere/aicontrol.env on the Pi (root, mode 600):\n")
    print(f"   XSPHERE_AICONTROL_TOTP_SECRET={secret}\n")
    print("2. Add it to your authenticator app — scan the QR code below, or enter the")
    print("   secret above by hand (time-based, 6 digits, 30 s, SHA-1):\n")
    print(f"   {uri}\n")
    qrencode = shutil.which("qrencode")
    if qrencode:
        subprocess.run([qrencode, "-t", "ansiutf8"], input=uri.encode("ascii"), check=False)
    else:
        print("   (install qrencode to see a QR code here: sudo apt install qrencode)\n")
    print("3. sudo systemctl restart xsphere-aicontrol, then test a code:")
    print("   sudo python3 -m aicontrol.enroll check\n")
    print("Clear this terminal afterwards (clear; history -c).")
    return 0


def _cmd_token(args: List[str]) -> int:
    if len(args) != 1:
        print("usage: python -m aicontrol.enroll token NAME   (e.g. operator)", file=sys.stderr)
        return 2
    name = args[0]
    try:
        token = new_token()
        entry = client_entry(name, token)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"Write token for AI client {name!r} — shown ONCE.\n")
    print("1. On the machine that runs this client, as the user that runs it:\n")
    print(f'   setx XSPHERE_AI_TOKEN "{token}"        (Windows; then open a new terminal)')
    print(f"   export XSPHERE_AI_TOKEN='{token}'      (Linux / macOS shell profile)\n")
    print("2. On the Pi, add this entry to XSPHERE_AICONTROL_CLIENTS in")
    print("   /etc/xsphere/aicontrol.env (comma-separated with any others):\n")
    print(f"   {entry}\n")
    print("3. sudo systemctl restart xsphere-aicontrol")
    return 0


def _cmd_check(args: List[str]) -> int:
    path = args[0] if args else DEFAULT_ENV_FILE
    try:
        env = read_env_file(path)
    except OSError as e:
        print(f"cannot read {path}: {e} (it is root-only: use sudo)", file=sys.stderr)
        return 2
    secret = decode_secret(env.get("XSPHERE_AICONTROL_TOTP_SECRET"))
    tokens, problems = ClientTokens.parse(env.get("XSPHERE_AICONTROL_CLIENTS"))
    mode = os.stat(path).st_mode & 0o777
    print(f"{path}: mode {mode:o}" + ("" if mode & 0o077 == 0 else
                                       "  ← readable by others: chmod 600 it"))
    print("client tokens: " + (", ".join(tokens.names()) or "none"))
    for p in problems:
        print(f"  problem: {p}")
    if secret is None:
        print("XSPHERE_AICONTROL_TOTP_SECRET: missing or not base32 — nothing can be armed")
        return 1
    code = getpass.getpass("code from the authenticator app (hidden): ")
    if re.fullmatch(r"\s*[0-9]{6}\s*", code) and code_ok_now(secret, code):
        print("OK — the service would accept this code now (if it has not been used).")
        return 0
    print("NOT accepted — wrong code, or this machine's clock is off (check: timedatectl).")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"totp": _cmd_totp, "token": _cmd_token, "check": _cmd_check}
    if not args or args[0] not in commands:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    return commands[args[0]](args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
