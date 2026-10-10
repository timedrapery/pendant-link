"""pendant_file.py: filing new downloads without losing a better copy."""

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pendant_file", HERE / "pendant_file.py")
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)

BASE = "pendant_20261010_153124"


def rec(d: Path, first, last, audio=b"A" * 100, transcript=None, base=BASE):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{base}.opus").write_bytes(audio)
    meta = {} if first is None else {"page_first": first, "page_last": last, "page_count": last - first + 1}
    (d / f"{base}.json").write_text(json.dumps(meta))
    (d / f"{base}.wav").write_bytes(b"RIFF" + audio)
    if transcript:
        (d / f"{base}.txt").write_text(transcript)


@pytest.fixture
def dirs(tmp_path):
    inc, arc = tmp_path / ".incoming", tmp_path
    inc.mkdir()
    return inc, arc, arc / "2026-10-10"


def test_new_recording_is_filed(dirs):
    inc, arc, day = dirs
    rec(inc, 1, 10)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").exists() and not list(inc.iterdir())


def test_identical_retry_is_dropped(dirs):
    inc, arc, day = dirs
    rec(day, 1, 10, transcript="[15:31:24] hello\n")
    rec(inc, 1, 10)
    pf.file_incoming(inc, arc)
    assert not list(inc.iterdir())
    assert (day / f"{BASE}.txt").read_text() == "[15:31:24] hello\n"   # transcript untouched
    assert not (day / f"{BASE}_r1.opus").exists()


def test_superset_replaces_and_keeps_old_aside(dirs):
    inc, arc, day = dirs
    rec(day, 5162, 5326, audio=b"A" * 100, transcript="old\n")
    rec(inc, 5162, 7133, audio=b"B" * 900)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").read_bytes() == b"B" * 900
    assert not (day / f"{BASE}.txt").exists()          # will be transcribed again
    aside = list((arc / ".replaced").iterdir())
    assert len(aside) == 1
    assert (aside[0] / f"{BASE}.opus").read_bytes() == b"A" * 100
    assert (aside[0] / f"{BASE}.txt").read_text() == "old\n"


def test_shorter_retry_keeps_both_and_old_untouched(dirs):
    inc, arc, day = dirs
    rec(day, 5162, 7133, audio=b"B" * 900, transcript="full\n")
    rec(inc, 5162, 5326, audio=b"A" * 100)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").read_bytes() == b"B" * 900
    assert (day / f"{BASE}.txt").read_text() == "full\n"
    assert (day / f"{BASE}_r1.opus").read_bytes() == b"A" * 100


def test_damaged_retry_with_more_pages_but_less_audio_keeps_both(dirs):
    inc, arc, day = dirs
    rec(day, 1, 10, audio=b"A" * 500)
    rec(inc, 1, 12, audio=b"x" * 20)   # claims more pages, but little audio came through
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").read_bytes() == b"A" * 500
    assert (day / f"{BASE}_r1.opus").exists()


def test_unknown_pages_keep_both(dirs):
    inc, arc, day = dirs
    rec(day, None, None, audio=b"A" * 100)   # filed before page ranges were recorded
    rec(inc, 1, 10, audio=b"B" * 900)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").read_bytes() == b"A" * 100
    assert (day / f"{BASE}_r1.opus").read_bytes() == b"B" * 900


def test_second_conflict_gets_r2(dirs):
    inc, arc, day = dirs
    rec(day, 1, 10, audio=b"A" * 500)
    rec(day, 1, 3, audio=b"C" * 50, base=BASE + "_r1")
    rec(inc, 1, 4, audio=b"D" * 60)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}_r2.opus").read_bytes() == b"D" * 60


def test_no_half_filed_leftovers(dirs):
    inc, arc, day = dirs
    rec(inc, 1, 10)
    pf.file_incoming(inc, arc)
    assert not list(day.glob("*.filing"))
