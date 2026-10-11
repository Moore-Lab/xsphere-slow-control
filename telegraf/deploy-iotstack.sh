#!/usr/bin/env bash
# =============================================================================
# Deploy telegraf/telegraf.conf to the IOTstack telegraf container (xbox-pi).
#
# The container reads ~/IOTstack/volumes/telegraf/telegraf.conf (bind-mounted
# to /etc/telegraf), NOT the copy in this repo, and it is not given the
# INFLUX_* / MQTT_* values as environment variables.  So after every change to
# telegraf/telegraf.conf the @PLACEHOLDER@ tokens have to be replaced with
# literal values, the result installed in the volume, and telegraf restarted.
#
#   bash telegraf/deploy-iotstack.sh            preview: diff + validate, change nothing
#   bash telegraf/deploy-iotstack.sh --apply    also install it and restart telegraf
#   ... --env                                   take the connection values from
#                                               telegraf/.env (first deploy, or to
#                                               change the token / URL / org / bucket)
#
# Without --env the connection values are carried over from the config that is
# already deployed, so a routine redeploy cannot change them.  The InfluxDB
# token is never printed.  Restarting telegraf loses the MQTT messages
# published while it is down (a few seconds).
# =============================================================================
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${TELEGRAF_CONF:-$HERE/telegraf.conf}
STACK=${IOTSTACK_DIR:-$HOME/IOTstack}
DEST=$STACK/volumes/telegraf/telegraf.conf

APPLY=0; USE_ENV=0
for arg in "$@"; do
  case $arg in
    --apply) APPLY=1 ;;
    --env)   USE_ENV=1 ;;
    *) echo "usage: $0 [--apply] [--env]" >&2; exit 2 ;;
  esac
done

[ -f "$SRC" ] || { echo "no such file: $SRC" >&2; exit 1; }
[ -d "$STACK/volumes/telegraf" ] || { echo "no telegraf volume under $STACK" >&2; exit 1; }

# --- connection values -------------------------------------------------------
# First literal value of a `key = "..."` / `key = ["..."]` line in the deployed file.
deployed() {
  [ -f "$DEST" ] || return 0
  sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\[?\"([^\"]*)\"\]?.*/\1/p" "$DEST" | head -1
}
OLD_TOKEN=$(deployed token)

if [ "$USE_ENV" = 1 ]; then
  [ -f "$HERE/.env" ] || { echo "--env given but $HERE/.env does not exist (see .env.example)" >&2; exit 1; }
  set -a; . "$HERE/.env"; set +a
  echo "connection values: telegraf/.env"
else
  INFLUX_URL=$(deployed urls)
  INFLUX_TOKEN=$OLD_TOKEN
  INFLUX_ORG=$(deployed organization)
  INFLUX_BUCKET=$(deployed bucket)
  broker=$(deployed servers); broker=${broker#tcp://}
  MQTT_HOST=${broker%:*}
  MQTT_PORT=${broker##*:}
  echo "connection values: carried over from $DEST"
fi

for v in INFLUX_URL INFLUX_TOKEN INFLUX_ORG INFLUX_BUCKET MQTT_HOST MQTT_PORT; do
  case ${!v:-} in
    ""|*@*@*) echo "$v is not set - create telegraf/.env from .env.example and rerun with --env" >&2; exit 1 ;;
  esac
done
echo "  influx: $INFLUX_URL  org=$INFLUX_ORG  bucket=$INFLUX_BUCKET"
echo "  mqtt:   $MQTT_HOST:$MQTT_PORT"
[ "$INFLUX_TOKEN" = "$OLD_TOKEN" ] && echo "  token:  unchanged" || echo "  token:  CHANGED"

# --- render ------------------------------------------------------------------
# Comment lines are left alone, so the header keeps documenting the placeholders
# and the token appears exactly once in the result.
NEW=$(mktemp); chmod 600 "$NEW"; trap 'rm -f "$NEW"' EXIT
esc() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
sed -E "/^[[:space:]]*#/!{
  s|@INFLUX_URL@|$(esc "$INFLUX_URL")|g
  s|@INFLUX_TOKEN@|$(esc "$INFLUX_TOKEN")|g
  s|@INFLUX_ORG@|$(esc "$INFLUX_ORG")|g
  s|@INFLUX_BUCKET@|$(esc "$INFLUX_BUCKET")|g
  s|@MQTT_HOST@|$(esc "$MQTT_HOST")|g
  s|@MQTT_PORT@|$(esc "$MQTT_PORT")|g
}" "$SRC" > "$NEW"

if grep -nE '^[^#]*@[A-Z_]+@' "$NEW"; then
  echo "unsubstituted placeholder(s) left in the rendered config (above)" >&2; exit 1
fi

# --- validate with the same telegraf build the container runs ---------------
IMAGE=$(docker inspect telegraf --format '{{.Config.Image}}' 2>/dev/null || echo iotstack-telegraf)
check=$(docker run --rm -i --network none --entrypoint sh "$IMAGE" -c \
  'cat > /tmp/t.conf; telegraf config check --config /tmp/t.conf 2>&1; echo "rc=$?"' < "$NEW")
if ! grep -q '^rc=0$' <<<"$check" || grep -q ' E! ' <<<"$check"; then
  echo "telegraf rejected the rendered config:" >&2
  grep -E ' E! |^rc=' <<<"$check" >&2
  exit 1
fi
echo "config check: ok ($IMAGE)"

# --- diff --------------------------------------------------------------------
redact() {
  T1=$INFLUX_TOKEN T2=$OLD_TOKEN awk '
    function scrub(s, t,   i) {
      if (t == "") return s
      while ((i = index(s, t))) s = substr(s, 1, i - 1) "<TOKEN>" substr(s, i + length(t))
      return s
    }
    { print scrub(scrub($0, ENVIRON["T1"]), ENVIRON["T2"]) }'
}
if [ -f "$DEST" ] && cmp -s "$DEST" "$NEW"; then
  echo "deployed config is already up to date - nothing to do"
  exit 0
fi
echo "--- changes: deployed (-) -> new (+) ---"
if [ -f "$DEST" ]; then
  diff -u --label deployed --label new <(redact < "$DEST") <(redact < "$NEW") || true
else
  echo "(no config deployed yet)"
fi

if [ "$APPLY" != 1 ]; then
  echo "--- preview only; rerun with --apply to install this and restart telegraf ---"
  exit 0
fi

# --- install + restart -------------------------------------------------------
if [ -f "$DEST" ]; then
  BAK=$DEST.bak-$(date +%Y%m%d-%H%M%S)
  sudo cp -p "$DEST" "$BAK"
  echo "backup: $BAK"
  sudo cp "$NEW" "$DEST"          # keeps the existing owner and mode
else
  sudo install -m 644 "$NEW" "$DEST"
fi

T0=$(date +%s)
(cd "$STACK" && docker compose restart telegraf)

want=$(grep -cE '^[[:space:]]*\[\[inputs\.mqtt_consumer\]\]' "$NEW")
got=0
for _ in $(seq 20); do
  sleep 1
  got=$(docker logs --since "$T0" telegraf 2>&1 | grep -c 'inputs.mqtt_consumer\] Connected' || true)
  [ "$got" -ge "$want" ] && break
done
errors=$(docker logs --since "$T0" telegraf 2>&1 | grep ' E! ' || true)
echo "telegraf: $(docker inspect telegraf --format '{{.State.Status}}'), $got/$want MQTT inputs connected"
if [ "$got" -lt "$want" ] || [ -n "$errors" ]; then
  [ -n "$errors" ] && echo "$errors" | redact >&2
  echo "PROBLEM - check 'docker logs telegraf'.${BAK:+  Roll back with:}" >&2
  [ -n "${BAK:-}" ] && echo "  sudo cp $BAK $DEST && (cd $STACK && docker compose restart telegraf)" >&2
  exit 1
fi
echo "deployed.  Verify in InfluxDB - see OPERATIONS.md, 'Changing what Telegraf records'."
