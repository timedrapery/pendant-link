#!/usr/bin/env bash
# Sets up the nightly encrypted backup of ~/recordings to Google Drive.
# Usage (on the Pi): curl -fsSL <raw URL of this file> | bash -s <commit>
# Connect with `ssh -L 53682:127.0.0.1:53682 ...` first: the Google sign-in opens in your desktop's browser.
# Does not start the timer. Run `pendant-backup` once by hand first.
set -euo pipefail

REF="${1:-main}"
BASE="https://raw.githubusercontent.com/timedrapery/pendant-link/$REF/pi"
KEYS="$HOME/.config/pendant/keys"
PASSFILE="$KEYS/backup-crypt.txt"

say() { echo "==> $*"; }

if ! command -v rclone >/dev/null; then
  say "Installing rclone"
  sudo apt-get install -y rclone
fi

# Encryption passwords for the backup: made once, never replaced.
mkdir -p "$KEYS"
if [ ! -f "$PASSFILE" ]; then
  say "Making backup encryption passwords"
  umask 077
  {
    echo "password=$(head -c 32 /dev/urandom | base64 | tr -d '/+=')"
    echo "password2=$(head -c 32 /dev/urandom | base64 | tr -d '/+=')"
  } > "$PASSFILE"
  chmod 600 "$PASSFILE"
fi
P1=$(sed -n 's/^password=//p' "$PASSFILE")
P2=$(sed -n 's/^password2=//p' "$PASSFILE")

# Google Drive connection (only files rclone itself creates: drive.file).
# The Pi runs the sign-in itself. Your desktop's browser reaches it through the SSH
# connection (ssh -L 53682:127.0.0.1:53682 ...), and nothing is installed or kept there.
if ! rclone listremotes 2>/dev/null | grep -qx "gdrive:"; then
  OUT="$(mktemp)"
  echo
  echo "Open the link below in your desktop's browser, sign in to Google, and click Allow."
  echo "(This works only if you connected with: ssh -L 53682:127.0.0.1:53682 alexander@xrxpix.local)"
  echo
  rclone authorize "drive" "eyJzY29wZSI6ImRyaXZlLmZpbGUifQ" --auth-no-open-browser > "$OUT" || true
  TOKEN="$(grep -m1 '^{.*access_token.*}$' "$OUT" || true)"
  rm -f "$OUT"
  if [ -z "$TOKEN" ]; then
    echo "Didn't get a token from Google. Nothing was changed; run this again."
    exit 1
  fi
  rclone config create gdrive drive scope=drive.file token="$TOKEN" --non-interactive >/dev/null
  say "Connected to Google Drive"
fi

if ! rclone listremotes 2>/dev/null | grep -qx "pendant-backup:"; then
  rclone config create pendant-backup crypt remote=gdrive:pendant-backup \
    password="$P1" password2="$P2" --obscure --non-interactive >/dev/null
  say "Set up the encrypted backup folder (pendant-backup on your Drive)"
fi

say "Installing pendant-backup"
mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user"
curl -fsSL "$BASE/pendant-backup" -o "$HOME/.local/bin/pendant-backup"
chmod +x "$HOME/.local/bin/pendant-backup"
curl -fsSL "$BASE/pendant-backup.service" -o "$HOME/.config/systemd/user/pendant-backup.service"
curl -fsSL "$BASE/pendant-backup.timer" -o "$HOME/.config/systemd/user/pendant-backup.timer"
systemctl --user daemon-reload

say "Installed. Next: run 'pendant-backup' once by hand."
echo "    The backup's passwords are in $PASSFILE. Keep a copy off the Pi"
echo "    (a USB stick or a password manager): without them the backup can't be read."
