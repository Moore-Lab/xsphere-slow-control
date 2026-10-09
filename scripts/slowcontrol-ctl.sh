#!/usr/bin/env bash
#
# xsphere slow control service control — run this ON xbox-pi.
#
# Install somewhere on PATH if you like:
#   sudo install -m 755 slowcontrol-ctl.sh /usr/local/bin/slowcontrol-ctl
#
# NOTE: stopping the slow control service stops its PLC watchdog counter
# (DS1099), so the ladder shuts the MKS valve and drives the flow setpoint
# to 0 V. That is the intended fail-safe — but it does close the gas path.

set -uo pipefail

# Fully qualified on purpose: the sudoers rule in
# scripts/xsphere-slowcontrol-sudoers matches the command line literally, so
# "xsphere-slowcontrol" and "xsphere-slowcontrol.service" are NOT the same
# thing to sudo. Keep these in step with that file and with servicectl.py.
SLOWCONTROL_UNIT="xsphere-slowcontrol.service"
OMEGA_UNIT="xsphere-omega-logger.service"

if [[ -t 1 ]]; then
    RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
else
    RED=''; GRN=''; YEL=''; DIM=''; RST=''
fi

usage() {
    cat <<'EOF'
Usage: slowcontrol-ctl.sh ACTION [SERVICE] [extra journalctl args]

ACTION   start | stop | restart | status | logs | follow | enable | disable
SERVICE  slowcontrol (sc) | omega (om) | all      (default: all)

Examples
  slowcontrol-ctl.sh restart                # restart both services
  slowcontrol-ctl.sh status                 # one line of state per unit
  slowcontrol-ctl.sh follow slowcontrol     # tail -f the journal
  slowcontrol-ctl.sh logs omega -n 500
EOF
    exit "${1:-0}"
}

# systemctl needs root; only reach for sudo when we are not already root.
as_root() {
    if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

show_status() {
    local unit=$1 active enabled since colour=$RED
    active=$(systemctl is-active   "$unit" 2>/dev/null || true)
    enabled=$(systemctl is-enabled "$unit" 2>/dev/null || echo unknown)
    since=$(systemctl show "$unit" -p ActiveEnterTimestamp --value 2>/dev/null || true)

    case "$active" in
        active)                   colour=$GRN ;;
        activating|deactivating)  colour=$YEL ;;
        inactive)                 colour=$DIM ;;
    esac
    printf '%-30s %s%-12s%s %-10s %s\n' \
           "$unit" "$colour" "${active:-unknown}" "$RST" "$enabled" "${DIM}${since}${RST}"
}

# ── argument parsing ────────────────────────────────────────────────────────
action=${1:-status}
case "$action" in -h|--help|help) usage 0 ;; esac
[[ $# -gt 0 ]] && shift

service=all
# A leading non-flag word is the service name; anything else is passed through
# to journalctl (so `logs omega -n 500` and `logs -n 500` both work).
if [[ $# -gt 0 && "$1" != -* ]]; then
    service=$1
    shift
fi

# Resolve the service name here in the main shell, NOT inside a subshell:
# a bad name has to be able to abort the script, and `exit` from a process
# substitution only kills the subshell.
case "$service" in
    slowcontrol|sc)  UNITS=("$SLOWCONTROL_UNIT") ;;
    omega|om)        UNITS=("$OMEGA_UNIT") ;;
    all)             UNITS=("$SLOWCONTROL_UNIT" "$OMEGA_UNIT") ;;
    *)
        printf "%sUnknown service '%s'%s — expected slowcontrol, omega, or all\n" \
               "$RED" "$service" "$RST" >&2
        exit 2
        ;;
esac

# ── dispatch ────────────────────────────────────────────────────────────────
rc=0
case "$action" in
    start|stop|restart|enable|disable)
        if [[ "$action" == stop ]] && printf '%s\n' "${UNITS[@]}" | grep -qF "$SLOWCONTROL_UNIT"; then
            printf '%sNote:%s stopping the slow control service stalls the PLC watchdog,\n' "$YEL" "$RST" >&2
            printf '      so the ladder will close the MKS valve and zero the setpoint.\n' >&2
        fi
        for u in "${UNITS[@]}"; do
            printf 'systemctl %s %s ... ' "$action" "$u"
            if as_root systemctl "$action" "$u"; then
                printf '%sok%s\n' "$GRN" "$RST"
            else
                printf '%sfailed%s\n' "$RED" "$RST"
                rc=1
            fi
        done
        echo
        for u in "${UNITS[@]}"; do show_status "$u"; done
        ;;

    status)
        printf '%-30s %-12s %-10s %s\n' UNIT ACTIVE BOOT SINCE
        for u in "${UNITS[@]}"; do show_status "$u"; done
        ;;

    logs)
        # Default to the last 200 lines unless the caller passed their own
        # journalctl arguments.
        if [[ $# -eq 0 ]]; then set -- -n 200; fi
        for u in "${UNITS[@]}"; do
            printf '===== %s =====\n' "$u"
            journalctl -u "$u" --no-pager "$@" || rc=1
        done
        ;;

    follow|tail)
        # journalctl accepts repeated -u, so both units interleave in one view.
        args=()
        for u in "${UNITS[@]}"; do args+=(-u "$u"); done
        journalctl "${args[@]}" -f "$@"
        ;;

    *)
        printf '%sUnknown action '\''%s'\''%s\n' "$RED" "$action" "$RST" >&2
        usage 2
        ;;
esac

exit $rc
