#!/usr/bin/env bash
# Installs pendant-sync and its timer, and applies the pendant-cli fixes it depends on.
# Usage (on the Pi): curl -sL <raw URL of this file> | bash -s <commit>
# Does not start the timer. Run `pendant sync` once by hand first, then enable the timer.
set -euo pipefail

REF="${1:-main}"
BASE="https://raw.githubusercontent.com/timedrapery/pendant-link/$REF/pi"
ADDRESS="${PENDANT_ADDRESS:-$(sed -n 's/^PENDANT_ADDRESS=//p' "$HOME/.config/pendant/env" 2>/dev/null | tail -1)}"

say() { echo "==> $*"; }

if [ -z "$ADDRESS" ]; then
  echo "Your pendant's address isn't known yet. Run: export PENDANT_ADDRESS=AA:BB:CC:DD:EE:FF  then rerun."
  exit 1
fi
if [ ! -d "$HOME/pendant-cli/.git" ]; then
  echo "Expected pendant-cli at ~/pendant-cli. Install it first (see the Raspberry Pi setup steps)."
  exit 1
fi

PENDANT_BIN="$(command -v pendant || true)"
if [ -z "$PENDANT_BIN" ]; then
  echo "Can't find the pendant command. Make sure 'pendant status' works in this shell, then rerun."
  exit 1
fi

say "Applying pendant-cli fixes ($REF)"
cd "$HOME/pendant-cli"
PATCH="$(mktemp)"
curl -fsSL "$BASE/pendant-cli-fixes.patch" -o "$PATCH"
# Back to the original files: undo edits, and remove files an earlier version of the patch added.
git checkout -- .
# The patch is built and tested against this pendant-cli revision; use exactly that one.
UPSTREAM=1c71659
if ! git cat-file -e "$UPSTREAM^{commit}" 2>/dev/null; then git fetch -q origin; fi
if [ "$(git rev-parse --short=7 HEAD)" != "$UPSTREAM" ]; then
  say "Using pendant-cli revision $UPSTREAM (the one the patch is tested against)"
  git checkout -q "$UPSTREAM"
fi
awk '/^diff --git /{f=$4} /^new file mode/{sub(/^b\//,"",f); print f}' "$PATCH" | while read -r f; do rm -f -- "$f"; done
git apply "$PATCH"
rm -f "$PATCH"
protoc -I proto --python_out=proto proto/pendant.proto
cd "$HOME"

# Encryption needs the cryptography library in the same Python that runs pendant.
read -r -a PENDANT_PY < <(head -1 "$(readlink -f "$PENDANT_BIN")" | sed 's/^#!//')
if ! "${PENDANT_PY[@]}" -c "import cryptography" 2>/dev/null; then
  say "Installing the cryptography library"
  "${PENDANT_PY[@]}" -m pip install -q cryptography || sudo apt-get install -y python3-cryptography
fi

say "Installing pendant sync"
mkdir -p "$HOME/.local/share/pendant" "$HOME/.config/pendant" "$HOME/.config/systemd/user" "$HOME/recordings"
curl -fsSL "$BASE/pendant-sync" -o "$HOME/.local/share/pendant/pendant-sync"
curl -fsSL "$BASE/pendant_file.py" -o "$HOME/.local/share/pendant/pendant_file.py"
chmod +x "$HOME/.local/share/pendant/pendant-sync"
rm -f "$HOME/.local/bin/pendant-sync"   # older installs put it here
curl -fsSL "$BASE/pendant-sync.service" -o "$HOME/.config/systemd/user/pendant-sync.service"
# How often to sync, in minutes (PENDANT_SYNC_EVERY in ~/.config/pendant/env; default 30).
# Each sync wakes the pendant's radio, so less often is easier on its battery; the pendant
# holds days of recordings, so nothing is lost by waiting.
EVERY="${PENDANT_SYNC_EVERY:-$(sed -n 's/^PENDANT_SYNC_EVERY=//p' "$HOME/.config/pendant/env" 2>/dev/null | tail -1)}"
EVERY="${EVERY:-30}"
case "$EVERY" in
  15|20|30) CAL="*:0/$EVERY" ;;
  60)       CAL="*:00" ;;
  120|180|240|360) CAL="0/$((EVERY / 60)):00" ;;
  *) echo "PENDANT_SYNC_EVERY must be 15, 20, 30, 60, 120, 180, 240 or 360 (minutes), not '$EVERY'. Using 30."
     EVERY=30; CAL="*:0/30" ;;
esac
curl -fsSL "$BASE/pendant-sync.timer" \
  | sed -e "s|^OnCalendar=.*|OnCalendar=$CAL|" -e "s|^Description=.*|Description=Sync the Limitless pendant every $EVERY minutes|" \
  > "$HOME/.config/systemd/user/pendant-sync.timer"
# Update only our own lines in the settings file; keep everything else (web password, options).
ENV="$HOME/.config/pendant/env"
touch "$ENV"
# Your settings, keys and recordings are yours alone on this Pi.
chmod 700 "$HOME/.config/pendant" "$HOME/recordings"
chmod 600 "$ENV"
[ -d "$HOME/.config/pendant/keys" ] && chmod 700 "$HOME/.config/pendant/keys"
set_setting() {
  if grep -q "^$1=" "$ENV"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV"
  else
    echo "$1=$2" >> "$ENV"
  fi
}
set_setting PENDANT_ADDRESS "$ADDRESS"
set_setting PENDANT_BIN "$PENDANT_BIN"
set_setting PENDANT_SYNC_EVERY "$EVERY"
systemctl --user daemon-reload
# Pick up a new schedule, but only if the timer is running: a timer you've stopped stays stopped.
systemctl --user is-active -q pendant-sync.timer 2>/dev/null && systemctl --user restart pendant-sync.timer
say "Syncing every $EVERY minutes"

say "Installed. Next: run 'pendant sync' once by hand and check ~/recordings."
