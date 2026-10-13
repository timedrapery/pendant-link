#!/usr/bin/env bash
# Sets up transcripts made on the Pi (faster-whisper). Nothing leaves the Pi.
# Usage (on the Pi): curl -fsSL <raw URL of this file> | bash -s <commit>
# Downloads the speech model once (about 150 MB). Does not start the timer.
set -euo pipefail

REF="${1:-main}"
BASE="https://raw.githubusercontent.com/timedrapery/pendant-link/$REF/pi"
SHARE="$HOME/.local/share/pendant"
VENV="$SHARE/whisper-venv"
MODEL="${PENDANT_WHISPER_MODEL:-base.en}"

say() { echo "==> $*"; }

if ! pendant --help 2>/dev/null | grep -qw transcribe; then
  echo "The pendant command doesn't have 'transcribe' yet. Run install-sync.sh (same commit) first, then this."
  exit 1
fi

mkdir -p "$SHARE" "$HOME/.config/systemd/user"
if [ ! -x "$VENV/bin/python" ]; then
  say "Making a separate Python environment for transcription"
  python3 -m venv "$VENV"
fi
say "Installing faster-whisper (a few minutes the first time)"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "faster-whisper>=1.1,<2"

"$VENV/bin/pip" install -q "sherpa-onnx>=1.12,<2"

say "Downloading the $MODEL speech model (once)"
"$VENV/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('$MODEL', device='cpu', compute_type='int8')"

# The voice model tells your voice from others' (see pendant_voice.py). About 40 MB, once.
VOICE_MODEL="$SHARE/models/nemo_en_titanet_small.onnx"
VOICE_SHA="ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"
mkdir -p "$SHARE/models"
if ! echo "$VOICE_SHA  $VOICE_MODEL" | sha256sum -c --status 2>/dev/null; then
  say "Downloading the voice model (once, about 40 MB)"
  curl -fsSL "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx" -o "$VOICE_MODEL.part"
  if ! echo "$VOICE_SHA  $VOICE_MODEL.part" | sha256sum -c --status; then
    rm -f "$VOICE_MODEL.part"
    echo "The voice model didn't match its expected checksum; not installed. Transcripts work without it."
  else
    mv "$VOICE_MODEL.part" "$VOICE_MODEL"
  fi
fi

say "Installing pendant transcribe"
curl -fsSL "$BASE/pendant-transcribe.py" -o "$SHARE/pendant-transcribe.py"
curl -fsSL "$BASE/pendant_voice.py" -o "$SHARE/pendant_voice.py"
curl -fsSL "$BASE/pendant-transcribe.service" -o "$HOME/.config/systemd/user/pendant-transcribe.service"
curl -fsSL "$BASE/pendant-transcribe.timer" -o "$HOME/.config/systemd/user/pendant-transcribe.timer"
systemctl --user daemon-reload

say "Installed. Next: 'pendant transcribe --limit 1' to try one recording."
if [ ! -f "$HOME/.config/pendant/voiceprint.json" ]; then
  say "To label your own voice and turn on consent mode, see 'Your voice and consent mode' in the guide."
fi
