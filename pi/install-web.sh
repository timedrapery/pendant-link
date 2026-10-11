#!/usr/bin/env bash
# Sets up the page for browsing your pendant days, served by the Pi on your home network.
# Usage (on the Pi): curl -fsSL <raw URL of this file> | bash -s <commit>
# Makes a password the first time (kept in ~/.config/pendant/env) and starts the page.
set -euo pipefail

REF="${1:-main}"
BASE="https://raw.githubusercontent.com/timedrapery/pendant-link/$REF/pi"
SHARE="$HOME/.local/share/pendant"
ENV="$HOME/.config/pendant/env"
PORT="${PENDANT_WEB_PORT:-8765}"

say() { echo "==> $*"; }

mkdir -p "$SHARE" "$HOME/.config/systemd/user" "$(dirname "$ENV")"
touch "$ENV"
# The settings file holds the page's password: readable by you only.
chmod 700 "$(dirname "$ENV")"
chmod 600 "$ENV"

if ! grep -q '^PENDANT_WEB_PASSWORD=.' "$ENV"; then
  say "Making a password for the page"
  PW="$(head -c 12 /dev/urandom | base64 | tr -d '/+=' | tr 'A-Z' 'a-z' | cut -c1-12)"
  echo "PENDANT_WEB_PASSWORD=$PW" >> "$ENV"
fi
PW="$(sed -n 's/^PENDANT_WEB_PASSWORD=//p' "$ENV" | tail -1)"

say "Installing the page"
curl -fsSL "$BASE/pendant-web.py" -o "$SHARE/pendant-web.py"
curl -fsSL "$BASE/pendant-web.service" -o "$HOME/.config/systemd/user/pendant-web.service"
systemctl --user daemon-reload
systemctl --user enable pendant-web.service >/dev/null 2>&1
systemctl --user restart pendant-web.service

sleep 2
if systemctl --user is-active --quiet pendant-web.service; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  say "Running. On your phone or computer (same Wi-Fi), open:"
  echo "      http://$(hostname).local:$PORT"
  [ -n "$IP" ] && echo "   or http://$IP:$PORT"
  echo "    User name: anything.  Password: $PW"
  echo "    (Your browser can remember it. The password is also in $ENV.)"
  echo "    It's plain HTTP, meant for your own Wi-Fi: it only answers home-network addresses."
  echo "    Don't open a router port to it; to reach it from outside, use a VPN such as Tailscale."
else
  echo "The page didn't start. See: systemctl --user status pendant-web.service --no-pager"
  exit 1
fi
