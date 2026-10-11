"""pendant_file.py: filing new downloads without losing a better copy."""

import importlib.util
import json
import shutil
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
    rec(inc, 5162, 7133, audio=b"A" * 100 + b"B" * 800)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").read_bytes() == b"A" * 100 + b"B" * 800
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
    rec(day, 20, 30, audio=b"C" * 50, base=BASE + "_r1")
    rec(inc, 1, 4, audio=b"D" * 60)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}_r1.opus").read_bytes() == b"C" * 50
    assert (day / f"{BASE}_r2.opus").read_bytes() == b"D" * 60


def test_new_copy_is_compared_with_every_filed_copy(dirs):
    inc, arc, day = dirs
    rec(day, 1, 10, audio=b"A" * 500)
    rec(day, 1, 3, audio=b"C" * 50, base=BASE + "_r1")
    rec(inc, 1, 4, audio=b"C" * 50 + b"D" * 10)   # holds all of _r1, not of the plain copy
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").read_bytes() == b"A" * 500
    assert (day / f"{BASE}_r1.opus").read_bytes() == b"C" * 50 + b"D" * 10
    assert not (day / f"{BASE}_r2.opus").exists()
    assert len(list((arc / ".replaced").iterdir())) == 1


def test_audio_prefix_proves_it_without_page_ranges(dirs):
    inc, arc, day = dirs
    rec(day, None, None, audio=b"start" * 20, transcript="old\n")  # filed before page ranges existed
    rec(inc, 1, 10, audio=b"start" * 20 + b"more" * 50)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").read_bytes() == b"start" * 20 + b"more" * 50
    assert not (day / f"{BASE}_r1.opus").exists()
    assert len(list((arc / ".replaced").iterdir())) == 1


def test_pages_never_freed_leave_one_copy(dirs):
    """Oct 10: an unreadable first page kept the pendant from freeing, so each sync downloaded
    the same growing recording again. Each new download should replace the last, not pile up."""
    inc, arc, day = dirs
    audio = b""
    for n in range(1, 6):
        audio += bytes([n]) * 100
        rec(inc, 6055, 6055 + n * 100, audio=audio)
        pf.file_incoming(inc, arc)
    assert pf.copies(day, BASE) == [BASE]
    assert (day / f"{BASE}.opus").read_bytes() == audio
    assert len(list((arc / ".replaced").iterdir())) == 4


def test_page_fields_read_from_large_metadata(tmp_path):
    meta = {"page_first": 6055, "page_last": 19007, "page_count": 12953,
            "bad_pages": list(range(6055, 6474)), "pages": [{"x": 1}] * 50000}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(meta, indent=2))
    assert pf._pages(p) == (6055, 19007, 12953)


def test_tidy_keeps_the_copy_that_holds_the_others(dirs):
    inc, arc, day = dirs
    audio = b""
    for n in range(0, 4):
        audio += bytes([n + 1]) * 100
        rec(day, None if n == 0 else 6055, 6055 + n * 100, audio=audio,
            base=BASE if n == 0 else f"{BASE}_r{n}", transcript=f"copy {n}\n")
    rec(day, 1, 2, audio=b"other" * 10, base=f"{BASE}_r9")   # a different recording: can't be proven
    pf.tidy(arc, dry_run=True)
    assert len(pf.copies(day, BASE)) == 5                    # dry run moves nothing
    pf.tidy(arc)
    assert pf.copies(day, BASE) == [BASE, f"{BASE}_r9"]
    assert (day / f"{BASE}.opus").read_bytes() == audio      # the fullest copy, now under the plain name
    assert (day / f"{BASE}.txt").read_text() == "copy 3\n"
    assert len(list((arc / ".replaced").iterdir())) == 3


def test_no_half_filed_leftovers(dirs):
    inc, arc, day = dirs
    rec(inc, 1, 10)
    pf.file_incoming(inc, arc)
    assert not list(day.glob("*.filing"))


def test_tidy_waits_for_a_running_transcription(dirs):
    import fcntl
    inc, arc, day = dirs
    rec(day, 1, 10, audio=b"A" * 100)
    rec(day, 1, 20, audio=b"A" * 100 + b"B" * 100, base=f"{BASE}_r1")
    lock = open(arc / ".transcribe.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert pf.tidy(arc) is False
    assert len(pf.copies(day, BASE)) == 2            # nothing touched
    lock.close()
    assert pf.tidy(arc) is True
    assert pf.copies(day, BASE) == [BASE]


def test_tidy_clears_an_unfinished_copy(dirs):
    inc, arc, day = dirs
    rec(day, 1, 10, audio=b"A" * 100)
    (day / f"{BASE}.wav.filing").write_bytes(b"half")
    pf.tidy(arc)
    assert not list(day.glob("*.filing"))
    assert (day / f"{BASE}.wav").exists()


def test_more_pages_and_more_audio_is_not_proof(dirs):
    """Review, Oct 11: a download covering the same pages with more audio can still differ
    inside. Without byte-for-byte proof both copies are kept."""
    inc, arc, day = dirs
    rec(day, 5162, 5326, audio=b"A" * 100, transcript="old\n")
    rec(inc, 5162, 7133, audio=b"B" * 900)
    pf.file_incoming(inc, arc)
    assert (day / f"{BASE}.opus").read_bytes() == b"A" * 100
    assert (day / f"{BASE}.txt").read_text() == "old\n"
    assert (day / f"{BASE}_r1.opus").read_bytes() == b"B" * 900
    assert not (arc / ".replaced").exists()


def test_interrupted_filing_is_finished_next_run(dirs, monkeypatch):
    """Power lost after some files were copied: the next run completes the job, and at
    every point at least one complete set of the recording's files exists."""
    inc, arc, day = dirs
    rec(inc, 1, 10, audio=b"A" * 100, transcript="t\n")
    real_unlink = Path.unlink
    calls = {"n": 0}

    def crash_on_first_unlink(self, *a, **kw):
        if self.parent == inc:
            calls["n"] += 1
            raise KeyboardInterrupt("power cut")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", crash_on_first_unlink)
    with pytest.raises(KeyboardInterrupt):
        pf.file_incoming(inc, arc)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    # copies are complete, originals still there, journal waiting
    assert (day / f"{BASE}.opus").read_bytes() == b"A" * 100
    assert (inc / f"{BASE}.opus").exists()
    assert list((arc / pf.JOURNAL_DIR).glob("*.json"))
    pf.file_incoming(inc, arc)                      # next sync
    assert not list(inc.iterdir())
    assert not list((arc / pf.JOURNAL_DIR).glob("*.json"))
    for ext in ("opus", "json", "wav", "txt"):
        assert (day / f"{BASE}.{ext}").exists()
    assert pf.copies(day, BASE) == [BASE]


def test_interrupted_mid_copy_leaves_originals_and_is_redone(dirs, monkeypatch):
    inc, arc, day = dirs
    rec(inc, 1, 10, audio=b"A" * 100)
    real_copy = shutil.copy2
    n = {"c": 0}

    def crash_on_second_copy(src, dst, *a, **kw):
        n["c"] += 1
        if n["c"] == 2:
            Path(dst).write_bytes(b"half")          # a partial .filing file
            raise KeyboardInterrupt("power cut")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", crash_on_second_copy)
    with pytest.raises(KeyboardInterrupt):
        pf.file_incoming(inc, arc)
    monkeypatch.setattr(shutil, "copy2", real_copy)
    assert len(list(inc.iterdir())) == 3            # nothing removed from incoming
    pf.file_incoming(inc, arc)
    assert not list(inc.iterdir()) and not list(day.glob("*.filing"))
    assert (day / f"{BASE}.wav").read_bytes() == b"RIFF" + b"A" * 100
