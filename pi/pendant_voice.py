"""Tell your own voice apart from everyone else's, on this computer, and keep consent mode.

Used by pendant-transcribe.py. Nothing here leaves the Pi.

What it keeps:
  ~/.config/pendant/voiceprint.json   a summary of YOUR voice (192 numbers), made once by
                                      `pendant transcribe --learn-voice ...`. Only yours:
                                      other voices are compared against it and forgotten.
                                      Not in the backup (re-learn it on a new Pi).
  ~/recordings/consent.json           stretches of time where the people you were with
                                      agreed to be transcribed (`--allow`).

What it does: each stretch of speech is labelled "me" if it sounds like you, otherwise
"other" (or "unclear" if too short to tell). It never tries to tell other people apart from
each other, so no one else's voice is ever summarised or stored.

Consent mode (on by default once your voice is learned; PENDANT_CONSENT_MODE=off turns it
off): what other people say is left out of the transcript, which shows "(someone else spoke)"
instead -- unless that time is in consent.json. The words of others exist only in memory
while a recording is being transcribed. The audio itself is kept as before (on the Pi and in
the encrypted backup), so a conversation you're later given consent for can be transcribed.
"""

import json
import os
from datetime import datetime
from pathlib import Path

RATE = 16000
MODEL_NAME = "nemo_en_titanet_small.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             "speaker-recongition-models/" + MODEL_NAME)
MODEL_SHA256 = "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"

MATCH = 0.42        # at or above: sounds like you
NOT_ME = 0.30       # below: someone else; in between: unclear
MIN_SECONDS = 1.5   # shorter stretches are widened to PAD_TO seconds of surrounding audio
PAD_TO = 2.0
LEARN_PIECE = 6.0   # learning splits your speech into pieces of at most this long...
LEARN_MIN_PIECE = 2.0
LEARN_NEED = 30.0   # ...and needs at least this many seconds that agree with each other
LEARN_AGREE = 0.5

ME, OTHER, UNCLEAR = "me", "other", "unclear"
WITHHELD = "(someone else spoke)"


def config_dir():
    return Path.home() / ".config" / "pendant"


def voiceprint_path():
    return config_dir() / "voiceprint.json"


def model_path():
    return Path(os.environ.get("PENDANT_VOICE_MODEL",
                               Path.home() / ".local" / "share" / "pendant" / "models" / MODEL_NAME))


def _unit(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


class Embedder:
    """Turns a stretch of 16 kHz audio into a voice summary, with sherpa-onnx."""

    def __init__(self, model=None, threads=1):
        import sherpa_onnx
        path = Path(model or model_path())
        if not path.is_file():
            raise FileNotFoundError(f"voice model missing: {path} (rerun install-transcribe.sh)")
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=threads)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def __call__(self, samples):
        s = self._ex.create_stream()
        s.accept_waveform(RATE, samples)
        s.input_finished()
        return _unit(self._ex.compute(s))


class Voice:
    """Labels stretches of speech as you, someone else, or unclear."""

    def __init__(self, me, embed, match=MATCH, not_me=NOT_ME):
        self.me, self.embed = _unit(me), embed
        self.match, self.not_me = match, min(not_me, match)

    def score(self, samples):
        return float(self.embed(samples) @ self.me)

    def label(self, window, start, end):
        """Label window[start:end] (seconds within the window). Returns (label, score)."""
        a, b = max(0.0, start), min(len(window) / RATE, end)
        if b - a < MIN_SECONDS:
            mid, half = (a + b) / 2, PAD_TO / 2
            a, b = max(0.0, mid - half), min(len(window) / RATE, mid + half)
            if b - a < 0.5:
                return UNCLEAR, None
        score = self.score(window[int(a * RATE):int(b * RATE)])
        if score >= self.match:
            return ME, score
        return (OTHER if score < self.not_me else UNCLEAR), score


def _setting(setting, name, default):
    return setting(name, default) if setting else os.environ.get(name, default)


def consent_mode_on(setting=None):
    return _setting(setting, "PENDANT_CONSENT_MODE", "on").lower() not in ("off", "no", "0", "false")


def load_voice(setting=None, embedder=Embedder):
    """(Voice, None) when your voice has been learned, else (None, reason)."""
    if _setting(setting, "PENDANT_VOICE", "on").lower() in ("off", "no", "0", "false"):
        return None, "voice labelling is off (PENDANT_VOICE=off)"
    try:
        rec = json.loads(voiceprint_path().read_text())
        me = rec["vector"]
    except FileNotFoundError:
        return None, "your voice hasn't been learned yet (pendant transcribe --learn-voice)"
    except (OSError, ValueError, KeyError) as e:
        return None, f"couldn't read {voiceprint_path()}: {e}"
    try:
        embed = embedder()
    except Exception as e:  # sherpa-onnx or the model missing
        return None, f"voice model not available ({e})"
    try:
        match = float(_setting(setting, "PENDANT_VOICE_MATCH", MATCH))
    except ValueError:
        match = MATCH
    return Voice(me, embed, match=match, not_me=min(NOT_ME, match)), None


def learn(pieces, embed):
    """Your voice summary from stretches of your own speech (float32 arrays).

    Long stretches are split; pieces that don't agree with the rest (someone else talking,
    noise) are left out. Returns (vector, seconds kept, seconds left out); raises ValueError
    when there isn't enough.
    """
    import numpy as np
    chunks = []
    step = int(LEARN_PIECE * RATE)
    for p in pieces:
        for i in range(0, len(p), step):
            c = p[i:i + step]
            if len(c) >= LEARN_MIN_PIECE * RATE:
                chunks.append(c)
    if not chunks:
        raise ValueError("no speech long enough to learn from")
    vecs = [embed(c) for c in chunks]
    secs = [len(c) / RATE for c in chunks]
    mean = _unit(np.mean(vecs, axis=0))
    keep = [i for i, v in enumerate(vecs) if float(v @ mean) >= LEARN_AGREE]
    if keep:
        mean = _unit(np.mean([vecs[i] for i in keep], axis=0))
        keep = [i for i, v in enumerate(vecs) if float(v @ mean) >= LEARN_AGREE]
    kept = sum(secs[i] for i in keep)
    dropped = sum(secs) - kept
    if kept < LEARN_NEED:
        raise ValueError(f"only {kept:.0f} s of speech that sounds like one voice; need {LEARN_NEED:.0f} s. "
                         "Pick a longer stretch where only you are talking.")
    return mean, kept, dropped


def save_voiceprint(vector, source, kept):
    path = voiceprint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"model": MODEL_NAME, "made": datetime.now().isoformat(timespec="seconds"),
                   "from": source, "seconds": round(kept, 1),
                   "vector": [round(float(x), 6) for x in vector]}, f)
    tmp.replace(path)
    return path


def forget_voiceprint():
    try:
        voiceprint_path().unlink()
        return True
    except FileNotFoundError:
        return False


# Consent: stretches of time where others agreed to be transcribed.

def consent_path(archive: Path):
    return Path(archive) / "consent.json"


def load_allowed(archive: Path):
    """[(start, end, note)] as datetimes."""
    try:
        rows = json.loads(consent_path(archive).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            out.append((datetime.fromisoformat(r["from"]), datetime.fromisoformat(r["to"]), r.get("note", "")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def add_allowed(archive: Path, start: datetime, end: datetime, note=""):
    path = consent_path(archive)
    try:
        rows = json.loads(path.read_text())
        if not isinstance(rows, list):
            rows = []
    except (OSError, ValueError):
        rows = []
    rows.append({"from": start.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds"),
                 "note": note, "added": datetime.now().isoformat(timespec="seconds")})
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rows, indent=1))
    tmp.replace(path)


def is_allowed(at, allowed):
    return at is not None and any(a <= at <= b for a, b, _ in allowed)
