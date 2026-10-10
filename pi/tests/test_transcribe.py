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
    return mod


class FakeModel:
    def __init__(self, script=None, fail=()):
        self.script = script or {}
        self.fail = set(fail)
        self.calls = []

    def transcribe(self, path, **kw):
        name = Path(path).name
        self.calls.append((name, kw))
        if name in self.fail:
            raise RuntimeError("bad audio")
        segs = [SimpleNamespace(start=s, end=e, text=t) for s, e, t in self.script.get(name, [])]
        return iter(segs), SimpleNamespace(duration=120.0)


def _rec(archive, stamp, start_ms=None):
    day = archive / f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    day.mkdir(parents=True, exist_ok=True)
    wav = day / f"pendant_{stamp}.wav"
    wav.write_bytes(b"RIFF")
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
    assert words["segments"][0] == {"start": 31.2, "end": 34.0, "at": "2026-10-10T12:45:31", "text": "Good morning."}
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
