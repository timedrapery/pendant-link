#!/usr/bin/env bash
# Installs pendant-sync and its timer, and applies the pendant-cli fixes it depends on.
# Usage (on the Pi): curl -sL <raw URL of this file> | bash -s <commit>
# Does not start the timer. Run `pendant sync` once by hand first, then enable the timer.
set -euo pipefail

REF="${1:-main}"
BASE="https://raw.githubusercontent.com/timedrapery/pendant-link/$REF/pi"
ADDRESS="${PENDANT_ADDRESS:-}"

say() { echo "==> $*"; }

if [ -z "$ADDRESS" ]; then
  echo "PENDANT_ADDRESS isn't set in this shell. Run: export PENDANT_ADDRESS=AA:BB:CC:DD:EE:FF  (your pendant's address), then rerun."
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
git checkout -- .
curl -fsSL "$BASE/pendant-cli-fixes.patch" | git apply
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
printf "PENDANT_ADDRESS=%s\nPENDANT_BIN=%s\n" "$ADDRESS" "$PENDANT_BIN" > "$HOME/.config/pendant/env"
systemctl --user daemon-reload

say "Installed. Next: run 'pendant sync' once by hand and check ~/recordings."
