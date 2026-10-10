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
chmod +x "$HOME/.local/share/pendant/pendant-sync"
rm -f "$HOME/.local/bin/pendant-sync"   # older installs put it here
curl -fsSL "$BASE/pendant-sync.service" -o "$HOME/.config/systemd/user/pendant-sync.service"
curl -fsSL "$BASE/pendant-sync.timer" -o "$HOME/.config/systemd/user/pendant-sync.timer"
# Update only our two lines in the settings file; keep everything else (web password, options).
ENV="$HOME/.config/pendant/env"
touch "$ENV"
set_setting() {
  if grep -q "^$1=" "$ENV"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV"
  else
    echo "$1=$2" >> "$ENV"
  fi
}
set_setting PENDANT_ADDRESS "$ADDRESS"
set_setting PENDANT_BIN "$PENDANT_BIN"
systemctl --user daemon-reload

say "Installed. Next: run 'pendant sync' once by hand and check ~/recordings."
