"""pendant-transcribe with a stand-in model (no audio or model needed)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent.parent


@pytest.fixture
def tr(tmp_path, monkeypatch):
    monkeypatch.setenv("PENDANT_ARCHIVE", str(tmp_path))
    spec = importlib.util.spec_from_file_location("pendant_transcribe", HERE / "pendant-transcribe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    real = mod.read_wav

    def tracking(wav):  # lets FakeModel know which recording the samples came from
        FakeModel.current = Path(wav).name
        return real(wav)

    mod.read_wav = tracking
    return mod


class FakeModel:
    current = None

    def __init__(self, script=None, fail=()):
        self.script = script or {}
        self.fail = set(fail)
        self.calls = []

    def transcribe(self, audio, **kw):
        assert audio.dtype.name == "float32"  # raw samples, not a path
        name = self.current
        self.calls.append((name, kw))
        if name in self.fail:
            raise RuntimeError("bad audio")
        segs = [SimpleNamespace(start=s, end=e, text=t) for s, e, t in self.script.get(name, [])]
        return iter(segs), SimpleNamespace(duration=120.0)


def _write_wav(path, seconds=0.5, rate=16000, channels=1):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x10" * int(seconds * rate) * channels)


def _rec(archive, stamp, start_ms=None):
    day = archive / f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    day.mkdir(parents=True, exist_ok=True)
    wav = day / f"pendant_{stamp}.wav"
    _write_wav(wav)
    if start_ms is not None:
        (day / f"pendant_{stamp}.json").write_text(json.dumps({"recording_start_ms": start_ms}))
    return wav


def test_writes_clock_time_lines(tr, tmp_path):
    from datetime import datetime
    start = datetime(2026, 10, 10, 12, 45, 0)
    wav = _rec(tmp_path, "20261010_124500", int(start.timestamp() * 1000))
    m = FakeModel({wav.name: [(31.2, 34.0, " Good morning. "), (65.0, 66.0, "  "), (90.0, 92.5, "Coffee?")]})
    assert tr.main([], model_factory=lambda: m) == 0
    assert wav.with_suffix(".txt").read_text() == "[12:45:31] Good morning.\n[12:46:30] Coffee?\n"
    words = json.loads(wav.with_suffix(".words.json").read_text())
    assert words["segments"][0] == {"start": 31.2, "end": 34.0, "at": "2026-10-10T12:45:31",
                                    "text": "Good morning.", "starred": False}
    assert m.calls[0][1]["vad_filter"] is True and m.calls[0][1]["language"] == "en"


def test_falls_back_to_file_name_time(tr, tmp_path):
    wav = _rec(tmp_path, "20261010_130107")
    tr.main([], model_factory=lambda: FakeModel({wav.name: [(5, 6, "hi")]}))
    assert wav.with_suffix(".txt").read_text() == "[13:01:12] hi\n"


def test_no_speech_is_marked_done(tr, tmp_path):
    wav = _rec(tmp_path, "20261010_130544")
    tr.main([], model_factory=lambda: FakeModel())
    assert wav.with_suffix(".txt").read_text() == "(no speech found)\n"
    assert list(tr.pending(tmp_path)) == []


def test_skips_done_incoming_and_loose_files(tr, tmp_path):
    done = _rec(tmp_path, "20261010_115949")
    done.with_suffix(".txt").write_text("x\n")
    todo = _rec(tmp_path, "20261010_124531")
    (tmp_path / ".incoming").mkdir()
    (tmp_path / ".incoming" / "pendant_20261010_140000.wav").write_bytes(b"RIFF")
    (tmp_path / "pendant_20261010_100000.wav").write_bytes(b"RIFF")
    assert list(tr.pending(tmp_path)) == [todo]


def test_oldest_first_and_limit(tr, tmp_path):
    b = _rec(tmp_path, "20261011_090000")
    a = _rec(tmp_path, "20261010_090000")
    m = FakeModel()
    tr.main(["--limit", "1"], model_factory=lambda: m)
    assert [c[0] for c in m.calls] == [a.name]
    assert not b.with_suffix(".txt").exists()


def test_failure_leaves_it_pending_and_continues(tr, tmp_path):
    bad = _rec(tmp_path, "20261010_090000")
    good = _rec(tmp_path, "20261010_100000")
    rc = tr.main([], model_factory=lambda: FakeModel({good.name: [(1, 2, "ok")]}, fail={bad.name}))
    assert rc == 1
    assert not bad.with_suffix(".txt").exists() and not list(bad.parent.glob("*.tmp"))
    assert good.with_suffix(".txt").exists()
    assert list(tr.pending(tmp_path)) == [bad]


def test_list_loads_no_model(tr, tmp_path, capsys):
    _rec(tmp_path, "20261010_090000")
    def boom():
        raise AssertionError("model loaded")
    assert tr.main(["--list"], model_factory=boom) == 0
    assert "1 waiting" in capsys.readouterr().out


def test_nothing_to_do_loads_no_model(tr, tmp_path):
    def boom():
        raise AssertionError("model loaded")
    assert tr.main([], model_factory=boom) == 0


def test_redo(tr, tmp_path):
    wav = _rec(tmp_path, "20261010_090000")
    wav.with_suffix(".txt").write_text("old\n")
    tr.main(["--redo", str(wav.with_suffix(".txt"))], model_factory=lambda: FakeModel({wav.name: [(0, 1, "new")]}))
    assert wav.with_suffix(".txt").read_text() == "[09:00:00] new\n"


def test_second_run_waits_for_lock(tr, tmp_path):
    import fcntl
    _rec(tmp_path, "20261010_090000")
    held = open(tmp_path / ".transcribe.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    m = FakeModel()
    assert tr.main([], model_factory=lambda: m) == 0
    assert m.calls == []


def test_reads_real_wav_samples(tr, tmp_path):
    wav = tmp_path / "a.wav"
    _write_wav(wav, seconds=1.0)
    audio = tr.read_wav.__wrapped__(wav) if hasattr(tr.read_wav, "__wrapped__") else tr.read_wav(wav)
    assert audio.shape == (16000,) and audio.dtype.name == "float32"
    assert abs(float(audio[0]) - 0x1000 / 32768.0) < 1e-6


def test_wrong_wav_format_fails_that_one_only(tr, tmp_path):
    bad = _rec(tmp_path, "20261010_090000")
    _write_wav(bad, rate=44100, channels=2)
    good = _rec(tmp_path, "20261010_100000")
    rc = tr.main([], model_factory=lambda: FakeModel({good.name: [(1, 2, "ok")]}))
    assert rc == 1 and not bad.with_suffix(".txt").exists() and good.with_suffix(".txt").exists()


def test_stars_mark_lines_around_a_press(tr, tmp_path):
    from datetime import datetime
    start = datetime(2026, 10, 10, 14, 0, 0)
    wav = _rec(tmp_path, "20261010_140000", int(start.timestamp() * 1000))
    press = datetime(2026, 10, 10, 14, 5, 0)
    meta = json.loads(wav.with_suffix(".json").read_text())
    meta["stars_ms"] = [int(press.timestamp() * 1000)]
    wav.with_suffix(".json").write_text(json.dumps(meta))
    m = FakeModel({wav.name: [(200, 202, "Before the window."),   # 14:03:20, 100 s before
                              (250, 252, "Worth keeping."),        # 14:04:10, inside
                              (305, 306, "Right after."),          # 14:05:05, inside
                              (330, 331, "Later on.")]})           # 14:05:30, after
    tr.main([], model_factory=lambda: m)
    assert wav.with_suffix(".txt").read_text().splitlines() == [
        "[14:03:20] Before the window.",
        "★ [14:04:10] Worth keeping.",
        "★ [14:05:05] Right after.",
        "[14:05:30] Later on.",
    ]
    words = json.loads(wav.with_suffix(".words.json").read_text())
    assert words["stars"] == ["2026-10-10T14:05:00"]


def test_star_from_a_sync_without_audio_still_counts(tr, tmp_path):
    from datetime import datetime
    start = datetime(2026, 10, 10, 15, 0, 0)
    wav = _rec(tmp_path, "20261010_150000", int(start.timestamp() * 1000))
    other = wav.parent / "pendant_20261010_151500.json"  # a later sync that only carried a press
    other.write_text(json.dumps({"stars_ms": [int(datetime(2026, 10, 10, 15, 1, 0).timestamp() * 1000)]}))
    m = FakeModel({wav.name: [(30, 32, "Remember this.")]})
    tr.main([], model_factory=lambda: m)
    assert wav.with_suffix(".txt").read_text() == "★ [15:00:30] Remember this.\n"


def test_clock_time_follows_the_timeline_across_gaps(tr, tmp_path):
    from datetime import datetime
    start = datetime(2026, 10, 10, 14, 1, 39)
    later = datetime(2026, 10, 10, 14, 16, 0)  # speech resumed after a quiet stretch
    wav = _rec(tmp_path, "20261010_140139", int(start.timestamp() * 1000))
    meta = json.loads(wav.with_suffix(".json").read_text())
    meta["timeline"] = [[0.0, int(start.timestamp() * 1000)], [300.0, int(later.timestamp() * 1000)]]
    meta["stars_ms"] = [int(datetime(2026, 10, 10, 14, 16, 28).timestamp() * 1000)]
    wav.with_suffix(".json").write_text(json.dumps(meta))
    m = FakeModel({wav.name: [(10, 12, "Morning bit."), (312, 314, "I pressed the button right there.")]})
    tr.main([], model_factory=lambda: m)
    assert wav.with_suffix(".txt").read_text().splitlines() == [
        "[14:01:49] Morning bit.",
        "★ [14:16:12] I pressed the button right there.",
    ]


def test_older_recordings_estimate_from_byte_positions(tr, tmp_path):
    from datetime import datetime
    t0 = datetime(2026, 10, 10, 14, 0, 0)
    t1 = datetime(2026, 10, 10, 14, 10, 0)
    wav = _rec(tmp_path, "20261010_140000", int(t0.timestamp() * 1000))
    _write_wav(wav, seconds=20.0)
    meta = {"recording_start_ms": int(t0.timestamp() * 1000), "total_audio_bytes": 1000, "pages": [
        {"absolute_timestamp_ms": int(t0.timestamp() * 1000), "audio_offset_bytes": 0,
         "chunks": [{"time_offset_ms": 0, "audio_offset_bytes": 0}]},
        {"absolute_timestamp_ms": int(t1.timestamp() * 1000), "audio_offset_bytes": 500,
         "chunks": [{"time_offset_ms": 0, "audio_offset_bytes": 0}]}]}
    wav.with_suffix(".json").write_text(json.dumps(meta))
    m = FakeModel({wav.name: [(2, 3, "first"), (12, 13, "second")]})
    tr.main([], model_factory=lambda: m)
    assert wav.with_suffix(".txt").read_text().splitlines() == ["[14:00:02] first", "[14:10:02] second"]


def test_redo_day(tr, tmp_path):
    a = _rec(tmp_path, "20261010_090000"); a.with_suffix(".txt").write_text("old\n")
    b = _rec(tmp_path, "20261010_100000"); b.with_suffix(".txt").write_text("old\n")
    other = _rec(tmp_path, "20261011_090000")
    m = FakeModel()
    tr.main(["--redo-day", "2026-10-10"], model_factory=lambda: m)
    assert [c[0] for c in m.calls] == [a.name, b.name]
    assert a.with_suffix(".txt").read_text() == "(no speech found)\n"


def _day_files(archive, day, name, wav=True, opus=True, txt=True):
    d = archive / day
    d.mkdir(parents=True, exist_ok=True)
    base = d / name
    if wav: _write_wav(base.with_suffix(".wav"), seconds=1.0)
    if opus: base.with_suffix(".opus").write_bytes(b"OPUS")
    if txt: base.with_suffix(".txt").write_text("[09:00:00] hi\n")
    return base


def test_tidy_deletes_only_old_safe_wavs(tr, tmp_path):
    from datetime import date
    today = date(2026, 10, 30)
    old = _day_files(tmp_path, "2026-10-10", "pendant_20261010_090000")
    no_opus = _day_files(tmp_path, "2026-10-10", "pendant_20261010_100000", opus=False)
    no_txt = _day_files(tmp_path, "2026-10-10", "pendant_20261010_110000", txt=False)
    recent = _day_files(tmp_path, "2026-10-20", "pendant_20261020_090000")
    deleted, freed = tr.tidy(tmp_path, "14", today=today, verify=lambda opus: True)
    assert deleted == 1 and freed > 0
    assert not old.with_suffix(".wav").exists()
    assert old.with_suffix(".opus").exists() and old.with_suffix(".txt").exists()
    assert no_opus.with_suffix(".wav").exists()   # nothing to rebuild it from
    assert no_txt.with_suffix(".wav").exists()    # not transcribed yet
    assert recent.with_suffix(".wav").exists()    # within 14 days


def test_tidy_never_and_bad_values_keep_everything(tr, tmp_path):
    from datetime import date
    old = _day_files(tmp_path, "2026-01-01", "pendant_20260101_090000")
    assert tr.tidy(tmp_path, "never", today=date(2026, 10, 30), verify=lambda o: True) == (0, 0)
    assert tr.tidy(tmp_path, "two weeks", today=date(2026, 10, 30), verify=lambda o: True) == (0, 0)
    assert old.with_suffix(".wav").exists()


def test_nightly_run_tidies_but_redo_does_not(tr, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "run_tidy", lambda archive: calls.append("tidy"))
    tr.main([], model_factory=lambda: FakeModel())            # nothing to do: still tidies
    assert calls == ["tidy"]
    wav = _rec(tmp_path, "20261010_090000")
    calls.clear()
    tr.main(["--redo", str(wav)], model_factory=lambda: FakeModel())
    assert calls == []


def test_setting_reads_env_file(tr, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PENDANT_KEEP_WAV_DAYS", raising=False)
    (tmp_path / ".config" / "pendant").mkdir(parents=True)
    (tmp_path / ".config" / "pendant" / "env").write_text("PENDANT_ADDRESS=x\nPENDANT_KEEP_WAV_DAYS=30\n")
    assert tr.setting("PENDANT_KEEP_WAV_DAYS", "14") == "30"
    monkeypatch.setenv("PENDANT_KEEP_WAV_DAYS", "7")
    assert tr.setting("PENDANT_KEEP_WAV_DAYS", "14") == "7"


def test_tidy_keeps_wav_when_opus_is_corrupt(tr, tmp_path):
    from datetime import date
    bad = _day_files(tmp_path, "2026-10-01", "pendant_20261001_090000")
    bad.with_suffix(".opus").write_bytes(b"not really opus data, but nonempty")
    deleted, _ = tr.tidy(tmp_path, "14", today=date(2026, 10, 30), verify=lambda opus: False)
    assert deleted == 0 and bad.with_suffix(".wav").exists()


def test_opus_decodes_uses_the_real_decoder(tr, tmp_path, monkeypatch):
    good = tmp_path / "good.opus"; good.write_bytes(b"x")
    bad = tmp_path / "bad.opus"; bad.write_bytes(b"x")
    fake = tmp_path / "pendant"
    # decode writes a 2-second WAV for "good", fails for "bad"
    fake.write_text(
        "#!/bin/sh\ncase \"$2\" in *good*) python3 -c \"import wave,sys;w=wave.open(sys.argv[1],'wb');"
        "w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\\\\x00'*64000);w.close()\" \"$3\";;"
        " *) exit 1;; esac\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PENDANT_BIN", str(fake))
    assert tr.opus_decodes(good) is True
    assert tr.opus_decodes(bad) is False
