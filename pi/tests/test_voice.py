"""Your voice vs. everyone else's, and consent mode, with a stand-in voice model.

The stand-in "hears" a voice from the sign of the audio: positive samples are you,
negative samples are someone else. That's enough to test what gets labelled, what is left
out, and that other people's words never reach a file.
"""

import json
import os
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from test_transcribe import FakeModel, tr  # noqa: F401  (fixture)

ME_VEC, OTHER_VEC, MIXED = np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0.35, 0, 0.9368])


def fake_embed(samples):
    m = float(np.mean(samples)) if len(samples) else 0.0
    if abs(m) < 0.002:         # a mix of voices sounds like neither
        return MIXED
    return ME_VEC if m > 0 else OTHER_VEC


def _wav(path, parts):
    """parts: [(seconds, +1 for you / -1 for someone else)]"""
    data = np.concatenate([np.full(int(s * 16000), 3000 * sign, dtype="<i2") for s, sign in parts])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(data.tobytes())


def _rec(archive, stamp, parts, start=None):
    day = archive / f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    day.mkdir(parents=True, exist_ok=True)
    wav = day / f"pendant_{stamp}.wav"
    _wav(wav, parts)
    if start is not None:
        (day / f"pendant_{stamp}.json").write_text(json.dumps({"recording_start_ms": int(start.timestamp() * 1000)}))
    return wav


def _voice(tr):
    return tr.pv.Voice(ME_VEC, fake_embed)


def _run(tr, m, voice=True, argv=()):
    factory = (lambda setting: (_voice(tr), None)) if voice else (lambda setting: (None, "not learned"))
    return tr.main(list(argv), model_factory=lambda: m, voice_factory=factory)


START = datetime(2026, 10, 12, 9, 0, 0)
SCRIPT = [(1, 4, "I'll take the early shift."), (11, 14, "Their secret plans."),
          (15, 18, "More of their words."), (21, 24, "Sounds good to me.")]
PARTS = [(10, +1), (10, -1), (10, +1)]


def test_labels_you_and_leaves_others_out(tr, tmp_path):
    wav = _rec(tmp_path, "20261012_090000", PARTS, START)
    assert _run(tr, FakeModel({wav.name: SCRIPT})) == 0
    txt = wav.with_suffix(".txt").read_text()
    assert txt == ("[09:00:01] Me: I'll take the early shift.\n"
                   "[09:00:11] (someone else spoke)\n"          # two lines of theirs, one marker
                   "[09:00:21] Me: Sounds good to me.\n")
    words = json.loads(wav.with_suffix(".words.json").read_text())
    assert [s["speaker"] for s in words["segments"]] == ["me", "other", "other", "me"]
    assert words["segments"][1]["text"] is None and words["segments"][1]["withheld"] is True
    assert words["voice"]["consent_mode"] is True and words["voice"]["others_left_out"] == 2
    for f in wav.parent.iterdir():   # their words are in no file at all
        if f.suffix != ".wav":
            assert "secret" not in f.read_text() and "their words" not in f.read_text()


def test_allowed_time_keeps_their_words(tr, tmp_path):
    wav = _rec(tmp_path, "20261012_090000", PARTS, START)
    tr.pv.add_allowed(tmp_path, datetime(2026, 10, 12, 9, 0, 10), datetime(2026, 10, 12, 9, 0, 12), "agreed")
    _run(tr, FakeModel({wav.name: SCRIPT}))
    lines = wav.with_suffix(".txt").read_text().splitlines()
    assert lines[1] == "[09:00:11] Someone else: Their secret plans."
    assert lines[2] == "[09:00:15] (someone else spoke)"     # 09:00:15 wasn't in the allowed stretch


def test_consent_mode_off_keeps_everything_labelled(tr, tmp_path, monkeypatch):
    monkeypatch.setenv("PENDANT_CONSENT_MODE", "off")
    wav = _rec(tmp_path, "20261012_090000", PARTS, START)
    _run(tr, FakeModel({wav.name: SCRIPT}))
    assert "[09:00:15] Someone else: More of their words." in wav.with_suffix(".txt").read_text()


def test_without_a_learned_voice_nothing_changes(tr, tmp_path):
    wav = _rec(tmp_path, "20261012_090000", PARTS, START)
    _run(tr, FakeModel({wav.name: SCRIPT}), voice=False)
    assert wav.with_suffix(".txt").read_text().splitlines()[1] == "[09:00:11] Their secret plans."
    assert json.loads(wav.with_suffix(".words.json").read_text())["voice"] is None


def test_your_name_and_stars(tr, tmp_path, monkeypatch):
    monkeypatch.setenv("PENDANT_MY_NAME", "Analayo")
    wav = _rec(tmp_path, "20261012_090000", PARTS, START)
    _run(tr, FakeModel({wav.name: SCRIPT}))
    assert wav.with_suffix(".txt").read_text().startswith("[09:00:01] Analayo: ")


def test_short_replies_are_unclear():
    """Oct 13: "Yes." and "That's right." from listeners scored as you, padded with the
    talk around them. Replies that short can't be told apart; leave them unclear."""
    pv = _pv()
    v = pv.Voice(ME_VEC, fake_embed)
    window = np.full(16000 * 10, 0.1, np.float32)
    assert v.label(window, 2.0, 4.0, words=1) == ("unclear", None)
    assert v.label(window, 2.0, 4.0, words=2) == ("unclear", None)
    assert v.label(window, 2.0, 4.0, words=5)[0] == "me"
    assert v.label(window, 2.0, 2.6)[0] == "unclear"        # under a second of voice


def test_someone_quieter_than_you_is_not_you():
    pv = _pv()
    v = pv.Voice(ME_VEC, fake_embed, my_db=-20.0)
    near = np.full(16000 * 4, 0.1, np.float32)                  # about -20 dB
    far = np.full(16000 * 4, 0.008, np.float32)                 # about -42 dB, same "voice"
    assert v.label(near, 0, 4, words=5)[0] == "me"
    assert v.label(far, 0, 4, words=5)[0] == "other"


def test_silence_inside_a_line_doesnt_count():
    pv = _pv()
    v = pv.Voice(ME_VEC, fake_embed)
    window = np.concatenate([np.zeros(16000 * 6, np.float32), np.full(24000, 0.1, np.float32),
                             np.zeros(16000 * 6, np.float32)])
    who, score, db = v.check(window, 0, len(window) / 16000, words=6)
    assert who == "me" and db is not None and db > -25


def test_learn_keeps_only_pieces_that_agree(tr):
    me = [np.full(16000 * 40, 0.1, np.float32)]
    other = [np.full(16000 * 10, -0.1, np.float32)]
    vec, kept, dropped, my_db = tr.pv.learn(me + other, fake_embed)
    assert float(vec @ ME_VEC) > 0.99 and round(kept) == 40 and round(dropped) == 10
    assert abs(my_db - 20 * np.log10(0.1)) < 0.5
    with pytest.raises(ValueError, match="need 30"):
        tr.pv.learn([np.full(16000 * 20, 0.1, np.float32)], fake_embed)


def test_learn_voice_from_a_stretch_of_a_day(tr, tmp_path, monkeypatch, capsys):
    wav = _rec(tmp_path, "20261012_090000", [(40, +1), (10, -1)], START)
    segs = [{"start": s, "end": s + 5, "at": f"2026-10-12T09:00:{s:02d}", "text": "x"} for s in range(0, 50, 5)]
    wav.with_suffix(".words.json").write_text(json.dumps({"segments": segs}))
    monkeypatch.setattr(tr.pv, "Embedder", lambda: fake_embed)
    assert tr.main(["--learn-voice", "2026-10-12", "09:00", "09:01"]) == 0
    out = capsys.readouterr().out
    assert "Learned your voice from 40 s" in out and "Left out 10 s" in out
    vp = Path(os.environ["HOME"]) / ".config" / "pendant" / "voiceprint.json"
    assert oct(vp.stat().st_mode & 0o777) == "0o600"
    rec = json.loads(vp.read_text())
    assert rec["from"] == "2026-10-12 09:00-09:01" and rec["vector"][0] > 0.99
    assert rec["loudness_db"] is not None
    assert tr.main(["--forget-voice"]) == 0 and not vp.exists()


def test_allow_points_at_recordings_to_redo(tr, tmp_path, capsys):
    wav = _rec(tmp_path, "20261012_090000", PARTS, START)
    _run(tr, FakeModel({wav.name: SCRIPT}))
    assert tr.main(["--allow", "2026-10-12", "09:00", "09:30", "team", "lunch"]) == 0
    out = capsys.readouterr().out
    assert f"--redo {wav}" in out
    rows = json.loads((tmp_path / "consent.json").read_text())
    assert rows[0]["note"] == "team lunch" and rows[0]["from"] == "2026-10-12T09:00:00"
    _run(tr, FakeModel({wav.name: SCRIPT}), argv=["--redo", str(wav)])
    assert "Their secret plans." in wav.with_suffix(".txt").read_text()


def test_load_voice_needs_a_learned_voice(tr):
    voice, why = tr.pv.load_voice(embedder=lambda: fake_embed)
    assert voice is None and "learn-voice" in why


def _pv():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pv", Path(__file__).resolve().parent.parent / "pendant_voice.py")
    pv = importlib.util.module_from_spec(spec); spec.loader.exec_module(pv)
    return pv


def test_long_stretches_are_scored_in_pieces():
    """Oct 12: the voice model failed on a 155-second stretch. Score it in pieces."""
    pv = _pv()
    seen = []

    def limited(samples):
        seen.append(len(samples))
        if len(samples) > 120 * 16000:
            raise RuntimeError("Attempting to broadcast an axis by a dimension other than 1")
        return fake_embed(samples)
    v = pv.Voice(ME_VEC, limited)
    window = np.full(16000 * 160, 0.1, np.float32)
    assert v.label(window, 2.0, 157.0)[0] == "me"
    assert max(seen) <= 20 * 16000


def test_a_stretch_the_model_cant_take_is_unclear_not_a_crash():
    pv = _pv()
    def broken(samples):
        raise RuntimeError("onnxruntime error")
    assert pv.Voice(ME_VEC, broken).label(np.zeros(16000 * 5, np.float32), 0, 4) == ("unclear", None)
