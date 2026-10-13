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
    monkeypatch.setenv("HOME", str(tmp_path / ".home"))  # no real voiceprint or settings
    spec = importlib.util.spec_from_file_location("pendant_transcribe", HERE / "pendant-transcribe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    real = mod.read_wav

    def tracking(wav):  # lets FakeModel know which recording the samples came from
        FakeModel.current = Path(wav).name
        return real(wav)

    mod.read_wav = tracking
    real_windows = mod.wav_windows

    def tracking_windows(wav, *a, **kw):
        FakeModel.current = Path(wav).name
        yield from real_windows(wav, *a, **kw)

    mod.wav_windows = tracking_windows
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
    deleted, freed = tr.tidy(tmp_path, "14", today=today, verify=lambda *a: True)
    assert deleted == 1 and freed > 0
    assert not old.with_suffix(".wav").exists()
    assert old.with_suffix(".opus").exists() and old.with_suffix(".txt").exists()
    assert no_opus.with_suffix(".wav").exists()   # nothing to rebuild it from
    assert no_txt.with_suffix(".wav").exists()    # not transcribed yet
    assert recent.with_suffix(".wav").exists()    # within 14 days


def test_tidy_never_and_bad_values_keep_everything(tr, tmp_path):
    from datetime import date
    old = _day_files(tmp_path, "2026-01-01", "pendant_20260101_090000")
    assert tr.tidy(tmp_path, "never", today=date(2026, 10, 30), verify=lambda *a: True) == (0, 0)
    assert tr.tidy(tmp_path, "two weeks", today=date(2026, 10, 30), verify=lambda *a: True) == (0, 0)
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
    deleted, _ = tr.tidy(tmp_path, "14", today=date(2026, 10, 30), verify=lambda *a: False)
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


def _fake_decoder(tmp_path, seconds):
    fake = tmp_path / f"pendant-{seconds}"
    fake.write_text(
        "#!/bin/sh\npython3 -c \"import wave,sys;w=wave.open(sys.argv[1],'wb');"
        f"w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(bytes({int(seconds*16000*2)}));w.close()\" \"$3\"\n")
    fake.chmod(0o755)
    return fake


def test_partial_decode_does_not_pass(tr, tmp_path, monkeypatch):
    opus = tmp_path / "x.opus"; opus.write_bytes(b"x")
    monkeypatch.setenv("PENDANT_BIN", str(_fake_decoder(tmp_path, 3)))   # only 3 s comes back
    assert tr.opus_decodes(opus, expected_seconds=60) is False
    assert tr.opus_decodes(opus, expected_seconds=3) is True


def test_tidy_keeps_wav_when_rebuild_would_be_short(tr, tmp_path, monkeypatch):
    from datetime import date
    base = _day_files(tmp_path, "2026-10-01", "pendant_20261001_090000")   # 1-second WAV
    _write_wav(base.with_suffix(".wav"), seconds=10.0)                      # make it 10 s
    monkeypatch.setenv("PENDANT_BIN", str(_fake_decoder(tmp_path, 2)))     # decoder gives 2 s
    deleted, _ = tr.tidy(tmp_path, "14", today=date(2026, 10, 30))
    assert deleted == 0 and base.with_suffix(".wav").exists()
    monkeypatch.setenv("PENDANT_BIN", str(_fake_decoder(tmp_path, 10)))    # full length
    deleted, _ = tr.tidy(tmp_path, "14", today=date(2026, 10, 30))
    assert deleted == 1


def _write_samples(path, samples):
    import wave
    import numpy as np
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def test_pieces_cover_every_sample_once_and_cut_in_quiet(tr, tmp_path):
    import numpy as np
    rng = np.random.default_rng(1)
    loud = (rng.standard_normal(16000 * 25) * 3000).astype("<i2")
    loud[16000 * 8: 16000 * 8 + 8000] = 0          # a half-second pause at 8.0-8.5 s
    loud[16000 * 17: 16000 * 17 + 8000] = 0        # and one at 17.0-17.5 s
    wav = tmp_path / "long.wav"
    _write_samples(wav, loud)
    pieces = list(tr.wav_windows(wav, seconds=10, search=4))
    back = np.concatenate([p for _, p in pieces])
    assert len(back) == len(loud)
    assert np.allclose(back, loud.astype(np.float32) / 32768.0)
    offsets = [o for o, _ in pieces]
    assert offsets[0] == 0 and 8.0 <= offsets[1] <= 8.5 and 17.0 <= offsets[2] <= 17.5


def test_short_recording_is_one_piece(tr, tmp_path):
    wav = tmp_path / "short.wav"
    _write_wav(wav, seconds=3.0)
    pieces = list(tr.wav_windows(wav, seconds=10, search=4))
    assert len(pieces) == 1 and pieces[0][0] == 0 and len(pieces[0][1]) == 48000


def test_times_stay_right_across_pieces(tr, tmp_path, monkeypatch):
    import numpy as np
    from datetime import datetime
    start = datetime(2026, 10, 10, 16, 11, 27)
    wav = _rec(tmp_path, "20261010_161127", int(start.timestamp() * 1000))
    _write_samples(wav, (np.random.default_rng(2).standard_normal(16000 * 25) * 3000).astype("<i2"))
    monkeypatch.setattr(tr, "WINDOW_SECONDS", 10)
    monkeypatch.setattr(tr, "CUT_SEARCH_SECONDS", 4)

    class PerPiece:
        def __init__(self):
            self.n = 0
        def transcribe(self, audio, **kw):
            self.n += 1
            return iter([SimpleNamespace(start=1.0, end=2.0, text=f"piece {self.n}")]), None

    m = PerPiece()
    tr.main([], model_factory=lambda: m)
    lines = wav.with_suffix(".txt").read_text().splitlines()
    assert m.n == 3 and lines[0] == "[16:11:28] piece 1"
    secs = [int(l[7:9]) + 60 * int(l[4:6]) for l in lines]   # mm:ss of each line
    assert secs[1] - secs[0] >= 6 and secs[2] - secs[1] >= 6  # later pieces are offset
    words = json.loads(wav.with_suffix(".words.json").read_text())
    assert words["duration"] == 25.0


def test_memory_stays_at_one_piece(tr, tmp_path):
    import tracemalloc
    import numpy as np
    wav = tmp_path / "evening.wav"
    _write_samples(wav, (np.random.default_rng(3).standard_normal(16000 * 600) * 3000).astype("<i2"))  # 10 min, 19 MB
    tracemalloc.start()
    total = sum(len(p) for _, p in tr.wav_windows(wav, seconds=60, search=10))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert total == 16000 * 600
    assert peak < 19_000_000  # a few one-minute pieces' worth: under half the whole recording (38 MB as float32)


def test_stars_found_in_a_large_metadata_file(tr, tmp_path):
    from datetime import datetime
    wav = _rec(tmp_path, "20261010_161127")
    press = int(datetime(2026, 10, 10, 18, 0, 0).timestamp() * 1000)
    meta = {"recording_start_ms": press - 3600_000, "pages": [{"chunks": [{"x": i}]} for i in range(20000)],
            "stars_ms": [press]}
    wav.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    assert tr.stars_for(wav) == [datetime(2026, 10, 10, 18, 0, 0)]


def test_pali_vocabulary_is_passed_as_hotwords(tr, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    wav = _rec(tmp_path, "20261010_153124")

    class HotwordModel:
        seen = []
        def transcribe(self, audio, language=None, beam_size=1, vad_filter=True,
                       condition_on_previous_text=False, hotwords=None, initial_prompt=None):
            self.seen.append((hotwords, initial_prompt))
            return iter([SimpleNamespace(start=1, end=2, text="the Pali Canon and dukkha")]), None

    m = HotwordModel()
    tr.main([], model_factory=lambda: m)
    hot, prompt = m.seen[0]
    assert "Pali Canon" in hot and "dukkha" in hot and "SuttaCentral" in hot and prompt is None
    assert "Pali Canon" in json.loads(wav.with_suffix(".words.json").read_text())["vocabulary"]


def test_older_model_gets_an_initial_prompt(tr, tmp_path):
    class OldModel:
        def transcribe(self, audio, language=None, beam_size=1, vad_filter=True,
                       condition_on_previous_text=False, initial_prompt=None):
            pass
    assert "initial_prompt" in tr._vocab_args(OldModel(), "dukkha")
    class NoPrompt:
        def transcribe(self, audio, language=None):
            pass
    assert tr._vocab_args(NoPrompt(), "dukkha") == {}
    assert tr._vocab_args(OldModel(), None) == {}


def test_own_vocabulary_file_and_turning_it_off(tr, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".config" / "pendant"
    cfg.mkdir(parents=True)
    (cfg / "vocabulary.txt").write_text("# my words\nAjahn Chah, Wat Pah Pong\nAnalayo  # a teacher\n")
    assert tr.vocabulary() == "Ajahn Chah, Wat Pah Pong, Analayo"
    (cfg / "vocabulary.txt").write_text("")
    assert tr.vocabulary() is None
    (cfg / "vocabulary.txt").unlink()
    assert "dukkha" in tr.vocabulary()
    monkeypatch.setenv("PENDANT_VOCABULARY", "off")
    assert tr.vocabulary() is None
