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


def load_meta(wav: Path):
    """What transcription needs from a recording's .json, without keeping the rest.

    A long recording's .json can be over 100 MB (every page's details), so it's read once
    and only the start time, stars, timeline, and (for older files) a compact page list
    are kept.
    """
    try:
        meta = json.loads(wav.with_suffix(".json").read_text())
    except (OSError, ValueError):
        return {}
    keep = {k: meta.get(k) for k in ("recording_start_ms", "stars_ms", "timeline", "total_audio_bytes")}
    if not meta.get("timeline") and meta.get("pages"):
        keep["pages"] = [
            {"absolute_timestamp_ms": p.get("absolute_timestamp_ms", 0),
             "audio_offset_bytes": p.get("audio_offset_bytes"),
             "chunks": [{"time_offset_ms": c.get("time_offset_ms", 0),
                         "audio_offset_bytes": c.get("audio_offset_bytes", 0)} for c in p.get("chunks", [])]}
            for p in meta["pages"]]
    del meta
    return keep


def recording_start(wav: Path, meta=None):
    """Clock time the recording began: from the .json sidecar, else from the file name."""
    meta = load_meta(wav) if meta is None else meta
    ms = meta.get("recording_start_ms")
    if ms:
        return datetime.fromtimestamp(ms / 1000)
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
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    del frames
    audio /= 32768.0  # in place: no second full-size copy
    return audio


WINDOW_SECONDS = 600   # transcribe long recordings in pieces of about this length
CUT_SEARCH_SECONDS = 20  # ...cut at the quietest moment in the last this-many seconds of each


def wav_seconds(wav: Path) -> float:
    import wave
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 16000)


def wav_windows(wav: Path, seconds=WINDOW_SECONDS, search=CUT_SEARCH_SECONDS):
    """Yield (offset in seconds, float32 samples) pieces of a 16 kHz mono 16-bit WAV.

    Memory stays at one piece however long the recording is (a whole evening is over
    500 MB of WAV, more than a Pi can hold in the form the model wants). Each cut is
    made at the quietest quarter-second near the end of the piece, so words aren't split.
    """
    import wave
    import numpy as np

    rate = 16000
    with wave.open(str(wav), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (rate, 1, 2):
            raise ValueError(f"expected 16 kHz mono 16-bit WAV, got {w.getframerate()} Hz, "
                             f"{w.getnchannels()} ch, {8 * w.getsampwidth()}-bit")
        size, block = int(seconds * rate), rate // 4
        carry = np.zeros(0, dtype="<i2")
        offset = 0
        while True:
            new = np.frombuffer(w.readframes(size - len(carry)), dtype="<i2")
            buf = np.concatenate([carry, new]) if len(carry) else new
            if len(buf) == 0:
                return
            if len(buf) < size or w.tell() >= w.getnframes():
                piece = buf.astype(np.float32)
                piece /= 32768.0
                yield offset / rate, piece
                return
            lo = max(len(buf) - int(search * rate), block)
            tail = buf[lo:lo + ((len(buf) - lo) // block) * block].astype(np.float32).reshape(-1, block)
            cut = lo + int(np.argmin((tail ** 2).mean(axis=1))) * block + block // 2
            piece = buf[:cut].astype(np.float32)
            piece /= 32768.0
            carry, offset = buf[cut:].copy(), offset + cut
            del buf, new, tail
            yield (offset - cut) / rate, piece
            del piece


def clock_for(wav: Path, duration: float, meta=None):
    """A function giving the real clock time (datetime) of a moment `t` seconds into the WAV.

    The pendant records only while it hears speech, so a recording is stretches with gaps.
    New syncs save a "timeline" (seconds into the audio -> clock time) in the .json. For
    older ones, estimate each chunk's place from its byte position in the audio. Falls back
    to start + t when there's nothing to go on.
    """
    import bisect

    meta = load_meta(wav) if meta is None else meta
    start = recording_start(wav, meta)
    anchors = []
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


STARS_RE = re.compile(r'"stars_ms":\s*(\[[^\]]*\])')


def _stars_in(meta_path: Path):
    """stars_ms from a .json, found by its key so a huge file isn't parsed whole."""
    try:
        text = meta_path.read_text()
    except OSError:
        return []
    m = STARS_RE.search(text)
    try:
        return json.loads(m.group(1)) if m else []
    except ValueError:
        return []


def stars_for(wav: Path):
    """Star times (datetimes) from every sync's metadata for that day.

    A press can arrive in a sync with no audio of its own, so look across the day's folder.
    """
    stars = []
    for meta in sorted(wav.parent.glob("pendant_*.json")):
        if meta.name.endswith(".words.json"):
            continue
        for ms in _stars_in(meta):
            try:
                stars.append(datetime.fromtimestamp(ms / 1000))
            except (TypeError, ValueError, OverflowError):
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
    meta = load_meta(wav)
    start = recording_start(wav, meta)
    stars = stars_for(wav)
    duration = wav_seconds(wav)
    clock = clock_for(wav, duration, meta)
    del meta
    lines, words = [], []
    for offset, audio in wav_windows(wav, WINDOW_SECONDS, CUT_SEARCH_SECONDS):
        segments, _ = model.transcribe(
            audio, language="en", beam_size=BEAM, vad_filter=True,
            condition_on_previous_text=False,
        )
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            t0, t1 = seg.start + offset, seg.end + offset
            at = clock(t0)
            stamp = at.strftime("%H:%M:%S") if at else _clock(t0)
            starred = _starred(at, stars)
            lines.append(f"{STAR + ' ' if starred else ''}[{stamp}] {text}")
            words.append({
                "start": round(t0, 2), "end": round(t1, 2),
                "at": at.isoformat(timespec="seconds") if at else None, "text": text,
                "starred": starred,
            })
        del audio
    # Write the JSON first: the .txt is what marks a recording as done.
    _write_atomic(wav.with_suffix(".words.json"), json.dumps({
        "recording": wav.name, "model": MODEL,
        "recording_start": start.isoformat(timespec="seconds") if start else None,
        "duration": round(duration, 1),
        "stars": [t.isoformat(timespec="seconds") for t in stars], "segments": words,
    }, indent=1))
    _write_atomic(wav.with_suffix(".txt"), ("\n".join(lines) if lines else NO_SPEECH) + "\n")
    return duration, len(lines)


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


def opus_decodes(opus: Path, expected_seconds=None, min_seconds=1.0):
    """True if the compact audio decodes in full (via `pendant decode` into a scratch file).

    With expected_seconds (the WAV's length), the rebuilt audio must be at least 98% as long,
    so a damaged file that only yields a short playable start doesn't pass. A WAV is only
    deleted once this passes, so the .opus can always give it back.
    """
    import subprocess
    import tempfile

    pendant = setting("PENDANT_BIN", "pendant")
    with tempfile.TemporaryDirectory(prefix="pendant-tidy-") as d:
        out = Path(d) / "check.wav"
        try:
            r = subprocess.run([pendant, "decode", str(opus), str(out)],
                               capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError):
            return False
        if r.returncode != 0 or not out.exists():
            return False
        try:
            import wave
            with wave.open(str(out), "rb") as w:
                got = w.getnframes() / max(w.getframerate(), 1)
        except (OSError, wave.Error, EOFError):
            return False
        if expected_seconds:
            return got >= 0.98 * expected_seconds
        return got >= min_seconds


def wav_seconds(wav: Path):
    import wave
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / max(w.getframerate(), 1)
    except (OSError, wave.Error, EOFError):
        return None


def tidy(archive: Path, keep_days, today=None, verify=opus_decodes):
    """Delete WAVs from days older than keep_days when their .opus decodes and a transcript exists.

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
            if not verify(opus, wav_seconds(wav)):
                log(f"  keeping {wav.name}: its .opus didn't decode in full")
                continue
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
