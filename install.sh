#!/usr/bin/env bash
# One-time setup on a Linux server (Ubuntu/Debian with systemd): tests the
# Apple check, connects your Telegram bot, and installs the watcher as a
# service that runs 24/7 and restarts itself if it fails. Run as root from this
# folder, after creating your config.json:   sudo bash install.sh
#
# The config.json beside this script is the source of truth: running the
# script again installs the current files and that config, and restarts the
# service. Edits made under /opt/stock-alert are overwritten.
#
# Remove everything again (service, files, token):   sudo bash install.sh --uninstall
set -euo pipefail

APP=/opt/stock-alert
ENV_FILE=/etc/stock-alert.env
UNIT=/etc/systemd/system/stock-alert.service
FILES=(stock_alert.py stores_uk.json models_uk.json config.json LICENSE)
SRC="$(cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
  "") MODE=install ;;
  --uninstall) MODE=uninstall ;;
  *) echo "Usage: sudo bash install.sh [--uninstall]"; exit 1 ;;
esac

if [ "$MODE" = install ] && [ ! -f "$SRC/config.json" ]; then
  echo "No config.json yet: copy config.example.json to config.json beside this script, edit it, then run this again."
  exit 1
fi
if [ "$(id -u)" != "0" ]; then echo "Run as root:  sudo bash install.sh${1:+ $1}"; exit 1; fi

if [ "$MODE" = uninstall ]; then
  # Safe to run twice, and when nothing is installed.
  if command -v systemctl >/dev/null; then
    systemctl disable --now stock-alert >/dev/null 2>&1 || true
    systemctl reset-failed stock-alert >/dev/null 2>&1 || true
  fi
  rm -f "$UNIT" "$UNIT.tmp" "$ENV_FILE" /etc/.stock-alert.env.*
  rm -rf "$APP" "$APP.old" /opt/.stock-alert.* "$UNIT.d"   # "$UNIT.d": drop-ins from systemctl edit
  if command -v systemctl >/dev/null; then systemctl daemon-reload || true; fi
  echo "Removed the stock-alert service, $APP and $ENV_FILE (the bot token)."
  echo "The token may still work: revoke it in Telegram by messaging @BotFather, sending /revoke and choosing your bot."
  exit 0
fi

command -v systemctl >/dev/null || { echo "This installer needs systemd."; exit 1; }
# The service runs as an unprivileged dynamic user that cannot see /root or
# /home, so it must use the system interpreter, never one in a virtualenv there.
PY=""
for candidate in /usr/bin/python3 /usr/local/bin/python3; do
  if [ -x "$candidate" ]; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "STOP: no system python3 in /usr/bin or /usr/local/bin. Install it (apt install python3) and run this again."
  exit 1
fi
case "$(readlink -f "$PY")" in
  /root/*|/home/*) echo "STOP: $PY points into a home directory, which the service cannot read. Install python3 with apt."; exit 1 ;;
esac
# 3.9 matches MIN_PYTHON in stock_alert.py.
if ! "$PY" -I -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  echo "STOP: $PY is $("$PY" -V 2>&1); Night Shift needs Python 3.9 or newer. Upgrade python3 and run this again."
  exit 1
fi

echo "Installing with $SRC/config.json. That copy is the one that counts: to change settings later,"
echo "edit config.json here and re-run this script; changes made under $APP are overwritten."

# Stage a fresh, root-owned copy. Nothing left over from an earlier install is
# kept, and Python runs isolated (-I) so no stray file next to the script is imported.
install -d -m 0755 /opt
STAGE="$(mktemp -d /opt/.stock-alert.XXXXXX)"
cleanup() { if [ -n "${STAGE:-}" ] && [ -d "$STAGE" ]; then rm -rf "$STAGE"; fi; }
trap cleanup EXIT
for f in "${FILES[@]}"; do
  install -o root -g root -m 0644 "$SRC/$f" "$STAGE/$f"
done
chmod 0755 "$STAGE"
run() { "$PY" -I -B "$STAGE/stock_alert.py" --config "$STAGE/config.json" "$@"; }

echo
echo "STEP 1 of 4: checking this server can read Apple's stock..."
if ! run --once; then
  echo
  echo "STOP: the check failed. If it says Apple is not answering, this server may be"
  echo "blocked; try a different provider. If it is a config problem, fix config.json."
  exit 1
fi

echo
echo "STEP 2 of 4: connect Telegram."
TELEGRAM_BOT_TOKEN=""
read -rsp "Paste your bot token from @BotFather and press Enter (it will not be shown): " TELEGRAM_BOT_TOKEN || true
echo
if ! [[ "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  echo "STOP: that does not look like a bot token (digits, a colon, then letters). Run this again."
  exit 1
fi
export TELEGRAM_BOT_TOKEN
echo "Finding your chat: open your bot in Telegram and send it any message (for example: hi)."
echo "If you already messaged it in the last day, that message is used and nothing more is needed."
if ! TELEGRAM_CHAT_ID="$(run --chat-id)"; then
  echo "STOP: could not get your chat id (see the message above). Run this again."
  exit 1
fi
if ! [[ "$TELEGRAM_CHAT_ID" =~ ^-?[0-9]+$ ]]; then echo "STOP: unexpected chat id. Run this again."; exit 1; fi
export TELEGRAM_CHAT_ID

echo
echo "STEP 3 of 4: sending a test alert to chat $TELEGRAM_CHAT_ID..."
run --test || { echo "STOP: the test alert failed (see the message above)."; exit 1; }
OK=""
read -rp "Did the test message arrive? Type y and press Enter: " OK || true
if [ "$OK" != "y" ]; then
  echo "Not installed. Send your bot a new message from your own Telegram account, then run this again."
  exit 1
fi

echo
echo "STEP 4 of 4: installing the watcher service..."
# The token file: written to a new 0600 file, then moved over the old one, so it
# ends up 0600 and root-owned whatever was there before.
TMP_ENV="$(mktemp /etc/.stock-alert.env.XXXXXX)"
printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" > "$TMP_ENV"
chown root:root "$TMP_ENV"
chmod 0600 "$TMP_ENV"
mv -f "$TMP_ENV" "$ENV_FILE"

rm -rf "$APP.old"
if [ -e "$APP" ]; then mv "$APP" "$APP.old"; fi
mv "$STAGE" "$APP"
STAGE=""
rm -rf "$APP.old"

cat > "$UNIT.tmp" <<UNIT
[Unit]
Description=Night Shift stock watcher (Apple Store pickup alerts to Telegram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$PY -I -B $APP/stock_alert.py --config $APP/config.json
Restart=on-failure
RestartSec=30
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallFilter=@system-service
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
LockPersonality=yes
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$UNIT.tmp"
mv -f "$UNIT.tmp" "$UNIT"

systemctl daemon-reload
systemctl enable stock-alert
systemctl restart stock-alert   # a re-run always moves the service onto the new files and token
sleep 5
if systemctl is-active --quiet stock-alert; then
  echo
  echo "DONE. The watcher is running; you should get a 'Stock watcher started' message."
  echo "Watch it live with:"
  echo "  journalctl -u stock-alert -f"
  echo "Change settings: edit $SRC/config.json and run this script again (edits under $APP are overwritten)."
  echo "Remove it:       sudo bash $SRC/install.sh --uninstall"
else
  echo
  echo "The watcher did not start. See:  journalctl -u stock-alert -n 30"
  exit 1
fi
