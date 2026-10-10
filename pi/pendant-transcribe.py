#!/usr/bin/env python3
"""Transcribe pendant recordings on this computer with faster-whisper. Nothing leaves the Pi.

For each ~/recordings/YYYY-MM-DD/pendant_*.wav without a transcript, writes next to it:
    pendant_....txt         one line per stretch of speech: "[12:45:31] words"
    pendant_....words.json  the same with start/end times, for tools to use later
A recording with no speech gets a .txt saying so, so it isn't tried again.

Run it as `pendant transcribe` (the nightly timer does), or:
    pendant transcribe --list         show what's waiting
    pendant transcribe --limit 3      do at most 3 recordings
    pendant transcribe --redo FILE    transcribe one recording again
    pendant transcribe --check FILE   loudness and how much speech is detected (numbers only)
Settings (environment): PENDANT_WHISPER_MODEL (default base.en), PENDANT_WHISPER_THREADS (4),
PENDANT_WHISPER_BEAM (1), PENDANT_ARCHIVE (~/recordings).
"""

import argparse
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ARCHIVE = Path(os.environ.get("PENDANT_ARCHIVE", Path.home() / "recordings"))
MODEL = os.environ.get("PENDANT_WHISPER_MODEL", "base.en")
THREADS = int(os.environ.get("PENDANT_WHISPER_THREADS", "4"))
BEAM = int(os.environ.get("PENDANT_WHISPER_BEAM", "1"))
NO_SPEECH = "(no speech found)"


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def pending(archive: Path):
    """Recordings filed in date folders that have no transcript yet, oldest first."""
    for wav in sorted(archive.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/pendant_*.wav")):
        if not wav.with_suffix(".txt").exists():
            yield wav


def recording_start(wav: Path):
    """Clock time the recording began: from the .json sidecar, else from the file name."""
    meta = wav.with_suffix(".json")
    try:
        ms = json.loads(meta.read_text()).get("recording_start_ms")
        if ms:
            return datetime.fromtimestamp(ms / 1000)
    except (OSError, ValueError):
        pass
    m = re.search(r"pendant_(\d{8}_\d{6})", wav.name)
    return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") if m else None


def read_wav(wav: Path):
    """16 kHz mono 16-bit WAV (what pendant-cli writes) as float32 samples.

    Read directly instead of through faster-whisper's PyAV decoder, which breaks with
    newer PyAV versions ("unexpected keyword argument 'metadata_errors'").
    """
    import wave
    import numpy as np

    with wave.open(str(wav), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if (rate, channels, width) != (16000, 1, 2):
        raise ValueError(f"expected 16 kHz mono 16-bit WAV, got {rate} Hz, {channels} ch, {8 * width}-bit")
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def _write_atomic(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def transcribe_one(model, wav: Path):
    """Transcribe one WAV. Returns (seconds of audio, number of speech lines)."""
    start = recording_start(wav)
    segments, info = model.transcribe(
        read_wav(wav), language="en", beam_size=BEAM, vad_filter=True,
        condition_on_previous_text=False,
    )
    lines, words = [], []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        at = (start + timedelta(seconds=seg.start)) if start else None
        stamp = at.strftime("%H:%M:%S") if at else _clock(seg.start)
        lines.append(f"[{stamp}] {text}")
        words.append({
            "start": round(seg.start, 2), "end": round(seg.end, 2),
            "at": at.isoformat(timespec="seconds") if at else None, "text": text,
        })
    # Write the JSON first: the .txt is what marks a recording as done.
    _write_atomic(wav.with_suffix(".words.json"), json.dumps({
        "recording": wav.name, "model": MODEL,
        "recording_start": start.isoformat(timespec="seconds") if start else None,
        "duration": round(getattr(info, "duration", 0.0), 1), "segments": words,
    }, indent=1))
    _write_atomic(wav.with_suffix(".txt"), ("\n".join(lines) if lines else NO_SPEECH) + "\n")
    return getattr(info, "duration", 0.0), len(lines)


def _clock(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def check(wav: Path):
    """Numbers only: is the audio there, how loud is it, how much speech does VAD find."""
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    audio = read_wav(wav)
    secs = len(audio) / 16000
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
    db = lambda x: 20 * np.log10(max(x, 1e-9))
    print(f"{wav.name}: {secs / 60:.1f} min")
    print(f"  loudness: peak {db(peak):.1f} dBFS, average {db(rms):.1f} dBFS")
    print(f"  silent (near zero): {100 * float(np.mean(np.abs(audio) < 1e-4)):.0f}% of samples")
    for threshold in (0.5, 0.35, 0.2):
        ts = get_speech_timestamps(audio, VadOptions(threshold=threshold))
        found = sum(t["end"] - t["start"] for t in ts) / 16000
        print(f"  speech detected at sensitivity {threshold}: {found:.0f} s in {len(ts)} stretches")
    if peak > 0:
        louder = audio * min(0.5 / peak, 50.0)
        ts = get_speech_timestamps(louder, VadOptions())
        found = sum(t["end"] - t["start"] for t in ts) / 16000
        print(f"  speech detected after turning it up: {found:.0f} s in {len(ts)} stretches")
    return 0


def load_model():
    from faster_whisper import WhisperModel
    return WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS)


def main(argv=None, model_factory=load_model):
    ap = argparse.ArgumentParser(prog="pendant transcribe", description="Transcribe recordings on this computer.")
    ap.add_argument("--list", action="store_true", help="show recordings waiting for a transcript")
    ap.add_argument("--limit", type=int, default=0, help="do at most this many recordings")
    ap.add_argument("--redo", metavar="FILE", help="transcribe this recording again")
    ap.add_argument("--check", metavar="FILE", help="show loudness and detected speech for one recording")
    args = ap.parse_args(argv)

    if args.check:
        return check(Path(args.check).expanduser().with_suffix(".wav"))

    if args.redo:
        wav = Path(args.redo).expanduser().with_suffix(".wav")
        if not wav.exists():
            sys.exit(f"Not found: {wav}")
        todo = [wav]
    else:
        todo = list(pending(ARCHIVE))
        if args.limit:
            todo = todo[: args.limit]

    if args.list:
        for wav in todo:
            print(wav.relative_to(ARCHIVE) if wav.is_relative_to(ARCHIVE) else wav)
        print(f"{len(todo)} waiting")
        return 0
    if not todo:
        log("Nothing new to transcribe.")
        return 0

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    lock = open(ARCHIVE / ".transcribe.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("A transcription is already running; skipping.")
        return 0

    log(f"Transcribing {len(todo)} recording(s) with {MODEL}")
    model = model_factory()
    failed = 0
    for wav in todo:
        t0 = time.monotonic()
        try:
            audio, n = transcribe_one(model, wav)
        except Exception as e:  # keep going; this one stays pending and is tried next time
            failed += 1
            log(f"  {wav.name}: failed ({e})")
            continue
        took = time.monotonic() - t0
        speed = f", {audio / took:.1f}x real time" if took > 0 and audio else ""
        log(f"  {wav.name}: {n} lines from {audio / 60:.1f} min of audio in {took / 60:.1f} min{speed}")
    log("Done." if not failed else f"Done, {failed} failed (they'll be tried again).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
