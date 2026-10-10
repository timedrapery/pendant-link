#!/usr/bin/env python3
"""Transcribe pendant recordings on this computer with faster-whisper. Nothing leaves the Pi.

For each ~/recordings/YYYY-MM-DD/pendant_*.wav without a transcript, writes next to it:
    pendant_....txt         one line per stretch of speech: "[12:45:31] words"
    pendant_....words.json  the same with start/end times, for tools to use later
A recording with no speech gets a .txt saying so, so it isn't tried again.

Stars: a quick press of the pendant button marks a moment ("keep this"), as in the Limitless
app. Lines from STAR_BEFORE seconds before a press to STAR_AFTER seconds after it start with ★.

Run it as `pendant transcribe` (the nightly timer does), or:
    pendant transcribe --list         show what's waiting
    pendant transcribe --limit 3      do at most 3 recordings
    pendant transcribe --redo FILE    transcribe one recording again
    pendant transcribe --redo-day 2026-10-10   transcribe a whole day again
    pendant transcribe --check FILE   loudness and how much speech is detected (numbers only)
    pendant transcribe --tidy         only tidy up old WAVs (see below)

Tidying: WAVs are about 15 times bigger than the .opus audio. After each nightly run, WAVs
from days more than PENDANT_KEEP_WAV_DAYS ago (default 14) are deleted, but only when the
.opus and the transcript are both there. Rebuild one any time with `pendant decode FILE.opus`.
Set PENDANT_KEEP_WAV_DAYS=never to keep them all.

Settings (environment, or ~/.config/pendant/env): PENDANT_WHISPER_MODEL (default base.en),
PENDANT_WHISPER_THREADS (4), PENDANT_WHISPER_BEAM (1), PENDANT_KEEP_WAV_DAYS (14),
PENDANT_ARCHIVE (~/recordings).
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ARCHIVE = Path(os.environ.get("PENDANT_ARCHIVE", Path.home() / "recordings"))
MODEL = os.environ.get("PENDANT_WHISPER_MODEL", "base.en")
THREADS = int(os.environ.get("PENDANT_WHISPER_THREADS", "4"))
BEAM = int(os.environ.get("PENDANT_WHISPER_BEAM", "1"))
NO_SPEECH = "(no speech found)"
STAR = "★"
STAR_BEFORE, STAR_AFTER = 90, 15  # seconds around a button press


def setting(name, default):
    """From the environment, else ~/.config/pendant/env, else the default."""
    if os.environ.get(name):
        return os.environ[name]
    env = Path.home() / ".config" / "pendant" / "env"
    try:
        for line in env.read_text().splitlines():
            if line.startswith(name + "="):
                value = line.split("=", 1)[1].strip()
                if value:
                    default = value
    except OSError:
        pass
    return default


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


def clock_for(wav: Path, duration: float):
    """A function giving the real clock time (datetime) of a moment `t` seconds into the WAV.

    The pendant records only while it hears speech, so a recording is stretches with gaps.
    New syncs save a "timeline" (seconds into the audio -> clock time) in the .json. For
    older ones, estimate each chunk's place from its byte position in the audio. Falls back
    to start + t when there's nothing to go on.
    """
    import bisect

    start = recording_start(wav)
    anchors = []
    try:
        meta = json.loads(wav.with_suffix(".json").read_text())
    except (OSError, ValueError):
        meta = {}
    if meta.get("timeline"):
        anchors = [(float(s), int(ms)) for s, ms in meta["timeline"]]
    elif meta.get("pages") and meta.get("total_audio_bytes") and duration:
        total = meta["total_audio_bytes"]
        for page in meta["pages"]:
            ts, base = page.get("absolute_timestamp_ms", 0), page.get("audio_offset_bytes")
            if ts < 1577836800000 or base is None:
                continue
            for chunk in page.get("chunks", []):
                secs = (base + chunk.get("audio_offset_bytes", 0)) / total * duration
                if not anchors or secs > anchors[-1][0]:
                    anchors.append((secs, ts + chunk.get("time_offset_ms", 0)))
    if not anchors:
        return lambda t: (start + timedelta(seconds=t)) if start else None
    keys = [a[0] for a in anchors]

    def at(t):
        i = max(bisect.bisect_right(keys, t) - 1, 0)
        secs, ms = anchors[i]
        return datetime.fromtimestamp(ms / 1000) + timedelta(seconds=max(t - secs, 0))
    return at


def stars_for(wav: Path):
    """Star times (datetimes) from every sync's metadata for that day.

    A press can arrive in a sync with no audio of its own, so look across the day's folder.
    """
    stars = []
    for meta in sorted(wav.parent.glob("pendant_*.json")):
        if meta.name.endswith(".words.json"):
            continue
        try:
            for ms in json.loads(meta.read_text()).get("stars_ms", []):
                stars.append(datetime.fromtimestamp(ms / 1000))
        except (OSError, ValueError):
            continue
    return sorted(set(stars))


def _starred(at, stars):
    return at is not None and any(
        s - timedelta(seconds=STAR_BEFORE) <= at <= s + timedelta(seconds=STAR_AFTER) for s in stars)


def _write_atomic(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def transcribe_one(model, wav: Path):
    """Transcribe one WAV. Returns (seconds of audio, number of speech lines)."""
    start = recording_start(wav)
    stars = stars_for(wav)
    audio = read_wav(wav)
    clock = clock_for(wav, len(audio) / 16000)
    segments, info = model.transcribe(
        audio, language="en", beam_size=BEAM, vad_filter=True,
        condition_on_previous_text=False,
    )
    lines, words = [], []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        at = clock(seg.start)
        stamp = at.strftime("%H:%M:%S") if at else _clock(seg.start)
        starred = _starred(at, stars)
        lines.append(f"{STAR + ' ' if starred else ''}[{stamp}] {text}")
        words.append({
            "start": round(seg.start, 2), "end": round(seg.end, 2),
            "at": at.isoformat(timespec="seconds") if at else None, "text": text,
            "starred": starred,
        })
    # Write the JSON first: the .txt is what marks a recording as done.
    _write_atomic(wav.with_suffix(".words.json"), json.dumps({
        "recording": wav.name, "model": MODEL,
        "recording_start": start.isoformat(timespec="seconds") if start else None,
        "duration": round(getattr(info, "duration", 0.0), 1),
        "stars": [t.isoformat(timespec="seconds") for t in stars], "segments": words,
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


def tidy(archive: Path, keep_days, today=None):
    """Delete WAVs from days older than keep_days when their .opus and transcript exist.

    Returns (files deleted, bytes freed). keep_days "never" keeps everything.
    """
    if str(keep_days).strip().lower() in ("never", "", "none"):
        return 0, 0
    try:
        keep = int(keep_days)
    except ValueError:
        log(f"PENDANT_KEEP_WAV_DAYS should be a number of days or 'never', not {keep_days!r}; keeping WAVs.")
        return 0, 0
    today = today or datetime.now().date()
    cutoff = today - timedelta(days=keep)
    deleted = freed = 0
    for day in sorted(archive.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]")):
        try:
            day_date = datetime.strptime(day.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day_date >= cutoff:
            continue
        for wav in sorted(day.glob("pendant_*.wav")):
            opus, txt = wav.with_suffix(".opus"), wav.with_suffix(".txt")
            if not (opus.is_file() and opus.stat().st_size > 0 and txt.is_file()):
                continue  # not safe to delete: nothing to rebuild it from, or not transcribed yet
            size = wav.stat().st_size
            wav.unlink()
            deleted += 1
            freed += size
    return deleted, freed


def report_space(archive: Path):
    """Log free space, with a warning when the card is getting full."""
    try:
        usage = shutil.disk_usage(archive)
    except OSError:
        return
    free_gb = usage.free / 1e9
    pct = 100 * usage.free / usage.total if usage.total else 100
    msg = f"Free space: {free_gb:.1f} GB ({pct:.0f}%)."
    if pct < 10 or free_gb < 2:
        msg += " Getting full: lower PENDANT_KEEP_WAV_DAYS, or move old recordings off the Pi."
    log(msg)


def run_tidy(archive: Path):
    keep = setting("PENDANT_KEEP_WAV_DAYS", "14")
    deleted, freed = tidy(archive, keep)
    if deleted:
        log(f"Tidied {deleted} old WAV file(s), freed {freed / 1e6:.0f} MB (the .opus audio and transcripts are kept).")
    report_space(archive)


def load_model():
    from faster_whisper import WhisperModel
    return WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS)


def main(argv=None, model_factory=load_model):
    ap = argparse.ArgumentParser(prog="pendant transcribe", description="Transcribe recordings on this computer.")
    ap.add_argument("--list", action="store_true", help="show recordings waiting for a transcript")
    ap.add_argument("--limit", type=int, default=0, help="do at most this many recordings")
    ap.add_argument("--redo", metavar="FILE", help="transcribe this recording again")
    ap.add_argument("--redo-day", metavar="YYYY-MM-DD", help="transcribe every recording from that day again")
    ap.add_argument("--check", metavar="FILE", help="show loudness and detected speech for one recording")
    ap.add_argument("--tidy", action="store_true", help="only delete old WAVs (see PENDANT_KEEP_WAV_DAYS)")
    args = ap.parse_args(argv)

    if args.tidy:
        run_tidy(ARCHIVE)
        return 0
    nightly = not (args.redo or args.redo_day or args.list or args.limit)

    if args.check:
        return check(Path(args.check).expanduser().with_suffix(".wav"))

    if args.redo:
        wav = Path(args.redo).expanduser().with_suffix(".wav")
        if not wav.exists():
            sys.exit(f"Not found: {wav}")
        todo = [wav]
    elif args.redo_day:
        todo = sorted((ARCHIVE / args.redo_day).glob("pendant_*.wav"))
        if not todo:
            sys.exit(f"No recordings found for {args.redo_day}")
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
        if nightly:
            run_tidy(ARCHIVE)
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
    if nightly:
        run_tidy(ARCHIVE)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
