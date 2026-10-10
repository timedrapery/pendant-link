#!/usr/bin/env bash
# Sets up the nightly encrypted backup of ~/recordings to Google Drive.
# Usage (on the Pi): curl -fsSL <raw URL of this file> | bash -s <commit>
# You'll be asked to paste the token from `rclone authorize` on your desktop.
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
if ! rclone listremotes 2>/dev/null | grep -qx "gdrive:"; then
  echo
  echo "Paste the token that 'rclone authorize' printed on your desktop"
  echo "(the whole line starting with {\"access_token\"), then press Enter:"
  read -r TOKEN < "${PENDANT_TTY:-/dev/tty}"
  case "$TOKEN" in
    \{*access_token*\}) ;;
    *) echo "That doesn't look like the token. Nothing was changed; run this again."; exit 1 ;;
  esac
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
echo "    The backup passwords are in $PASSFILE. Copy your keys folder to your desktop again:"
echo "    without these passwords the backup can't be read."
