#!/usr/bin/env python3
"""File new downloads from .incoming into ~/recordings/YYYY-MM-DD/, never losing a better copy.

A recording is named after the time of its first page, so when pages weren't freed on the
pendant, the next sync downloads them again under the same name. The new download is then
compared with every copy already filed under that name (the plain name and any _r1, _r2, ...):

- identical to a filed copy: the new download is a duplicate and is dropped;
- the filed copy's audio is, byte for byte, the start of the new audio (the only proof
  accepted that the new one holds everything the old one does): the filed copy is moved
  aside to ~/recordings/.replaced/ (transcript included), never deleted;
- anything else stays where it is, and the new one is filed beside it (under the plain name
  if it's free, otherwise the next _rN).

Every move of a recording's files (it has several: .opus, .json, .wav, ...) is one job:
it's written to a journal first, each file is copied, flushed and renamed into place, and
only once all of them are in place are the originals removed. If the job is interrupted,
the next run finds the journal and finishes it, so a recording is never left split or
half-copied, and there are always at least one complete set of its files.

Usage:
  pendant_file.py INCOMING_DIR ARCHIVE_DIR        file new downloads
  pendant_file.py --tidy ARCHIVE_DIR [--dry-run]  one-off: where a recording has several copies
                                                  (_r1, _r2, ...), keep the one that provably holds
                                                  all the others and move the rest aside
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

NAME_RE = re.compile(r"^(pendant_(\d{4})(\d{2})(\d{2})_\d{6})\.(.+)$")
COPY_RE = re.compile(r"^(pendant_\d{8}_\d{6})(?:_r(\d+))?$")
AUDIO_EXTS = ("opus", "json", "wav", "raw", "enc.jsonl", "badpages.jsonl")
SIDE_EXTS = ("txt", "words.json")  # derived from the audio; follow it


def _log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _pages(meta_path: Path):
    """(first, last, count) of pages a recording holds, or None if unknown.

    Reads only the start of the file: the page fields come before the (large) page list.
    """
    try:
        with meta_path.open("rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    vals = {}
    for key in ("page_first", "page_last", "page_count"):
        m = re.search(rf'"{key}":\s*(-?\d+|null)', head)
        vals[key] = int(m.group(1)) if m and m.group(1) != "null" else None
    if vals["page_first"] is None or vals["page_last"] is None:
        try:  # fall back to reading it all (small or differently ordered files)
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            return None
        vals = {k: meta.get(k) for k in vals}
        if vals["page_first"] is None or vals["page_last"] is None:
            return None
    first, last = vals["page_first"], vals["page_last"]
    return first, last, vals["page_count"] or (last - first + 1)


def _digest(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _is_prefix(short: Path, long: Path) -> bool:
    """True if every byte of `short` is the start of `long` (and short isn't empty)."""
    n = short.stat().st_size
    if n == 0 or n > long.stat().st_size:
        return False
    with short.open("rb") as a, long.open("rb") as b:
        while True:
            x = a.read(1 << 20)
            if not x:
                return True
            if b.read(len(x)) != x:
                return False


def _files(directory: Path, base: str):
    return [directory / f"{base}.{ext}" for ext in AUDIO_EXTS + SIDE_EXTS if (directory / f"{base}.{ext}").exists()]


def compare(old_dir: Path, old: str, new_dir: Path, new: str):
    """How the recording `new` relates to the filed copy `old`:
    'same' (identical audio), 'contains' (old's audio is exactly the start of new's), or 'unproven'.

    Page ranges and sizes are never enough on their own: two downloads of the same pages
    can differ inside (a page read on one try and not the next), so only the bytes decide.
    """
    old_opus, new_opus = old_dir / f"{old}.opus", new_dir / f"{new}.opus"
    if not old_opus.exists() or not new_opus.exists():
        return "unproven"
    size_old, size_new = old_opus.stat().st_size, new_opus.stat().st_size
    if size_old == size_new and size_old > 0 and _digest(old_opus) == _digest(new_opus):
        return "same"
    if size_new > size_old and _is_prefix(old_opus, new_opus):
        return "contains"
    return "unproven"


def copies(day: Path, base: str):
    """Names filed for this recording: the plain name, then _r1, _r2, ... (that have audio)."""
    found = []
    for p in day.glob(f"{base}*.opus"):
        m = COPY_RE.match(p.name[:-5])
        if m and m.group(1) == base:
            found.append((int(m.group(2) or 0), p.name[:-5]))
    return [name for _, name in sorted(found)]


JOURNAL_DIR = ".filing-journal"


def _fsync_path(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_journal(archive: Path, pairs):
    d = archive / JOURNAL_DIR
    d.mkdir(parents=True, exist_ok=True)
    j = d / f"{uuid.uuid4().hex}.json"
    tmp = j.with_name(j.name + ".tmp")
    with tmp.open("w") as f:
        json.dump({"moves": [[str(a), str(b)] for a, b in pairs]}, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(j)
    _fsync_path(d)
    return j


def _run_moves(pairs):
    """Copy each source to its destination (flushed, then renamed into place); then, only when
    every destination is complete, remove the sources. Safe to run again after an interruption."""
    folders = set()
    for src, dest in pairs:
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".filing")
            shutil.copy2(str(src), str(tmp))
            with tmp.open("rb") as f:
                os.fsync(f.fileno())
            tmp.replace(dest)
            folders.add(dest.parent)
        elif not dest.exists():
            raise RuntimeError(f"neither {src} nor {dest} exists")
    for d in folders:
        _fsync_path(d)
    for src, _ in pairs:
        if src.exists():
            src.unlink()
    for d in {s.parent for s, _ in pairs}:
        if d.exists():
            _fsync_path(d)


def move_recording(archive: Path, pairs):
    """Move a recording's files as one job, journaled so an interruption is finished next run."""
    if not pairs:
        return
    j = _write_journal(archive, pairs)
    _run_moves(pairs)
    j.unlink()


def finish_interrupted(archive: Path):
    """Complete any move an earlier run started but didn't finish, and clear unfinished copies."""
    d = archive / JOURNAL_DIR
    if d.is_dir():
        for j in sorted(d.glob("*.json")):
            try:
                pairs = [(Path(a), Path(b)) for a, b in json.loads(j.read_text())["moves"]]
            except (OSError, ValueError, KeyError) as e:
                _log(f"couldn't read {j.name} ({e}); leaving it for a person to look at")
                continue
            _run_moves(pairs)
            j.unlink()
            _log(f"finished a filing an earlier run didn't complete ({len(pairs)} file(s))")
        for t in d.glob("*.tmp"):
            t.unlink()
    for day in archive.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"):
        remove_leftovers(day)


def _aside_dir(archive: Path, name: str):
    stem = archive / ".replaced" / f"{name}-{datetime.now():%Y%m%d_%H%M%S}"
    aside, n = stem, 1
    while aside.exists():  # never move into a folder that already holds a copy
        n += 1
        aside = stem.with_name(f"{stem.name}-{n}")
    return aside


def _move_aside(archive: Path, day: Path, name: str, dry_run=False):
    aside = _aside_dir(archive, name)
    if not dry_run:
        aside.mkdir(parents=True)
        move_recording(archive, [(f, aside / f.name) for f in _files(day, name)])
    return aside


def _free_name(day: Path, base: str):
    if not (day / f"{base}.opus").exists() and not (day / f"{base}.json").exists():
        return base
    n = 1
    while (day / f"{base}_r{n}.opus").exists() or (day / f"{base}_r{n}.json").exists():
        n += 1
    return f"{base}_r{n}"


def _place(archive: Path, src_dir: Path, src: str, day: Path, target: str):
    """File every file of a recording under a new folder or name, as one journaled job."""
    move_recording(archive, [(f, day / f"{target}{f.name[len(src):]}") for f in _files(src_dir, src)])


def file_incoming(incoming: Path, archive: Path):
    finish_interrupted(archive)
    bases = sorted({m.group(1) for p in incoming.iterdir() if (m := NAME_RE.match(p.name))})
    for base in bases:
        m = re.match(r"pendant_(\d{4})(\d{2})(\d{2})_", base)
        day = archive / f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        day.mkdir(parents=True, exist_ok=True)
        filed = copies(day, base)
        results = {name: compare(day, name, incoming, base) for name in filed}
        if "same" in results.values():
            for f in _files(incoming, base):
                f.unlink()
            same = next(n for n, r in results.items() if r == "same")
            _log(f"{base}: same as {same}, already filed; dropped the duplicate")
            continue
        for name, r in results.items():
            if r == "contains":
                aside = _move_aside(archive, day, name)
                _log(f"{base}: the new download holds all of {name}; moved that copy to {aside}")
        target = _free_name(day, base)
        if target != base:
            _log(f"{base}: a copy it can't be proven to contain is already filed; this one is {target}")
        _place(archive, incoming, base, day, target)


def _hold_locks(archive: Path):
    """Take the sync and transcription locks, or None if either is running right now."""
    held = []
    for name in (".sync.lock", ".transcribe.lock"):
        f = open(archive / name, "a")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            for g in held:
                g.close()
            return None
        held.append(f)
    return held


def remove_leftovers(day: Path):
    """A .filing file is a copy that never finished; its source is still in place (sources are
    removed only after every copy is complete), so it can go."""
    for f in day.glob("*.filing"):
        f.unlink()
        _log(f"removed {day.name}/{f.name}, an unfinished copy from an interrupted run")


def tidy(archive: Path, dry_run=False):
    """For each recording filed more than once, keep the copy that provably holds all the others.

    Refuses while a sync or a transcription is running, since either may be using these files.
    """
    locks = _hold_locks(archive)
    if locks is None:
        _log("A sync or a transcription is running right now. Try again when it's finished "
             "(stop transcription with: systemctl --user stop pendant-transcribe.service).")
        return False
    try:
        if not dry_run:
            finish_interrupted(archive)
        _tidy(archive, dry_run)
    finally:
        for f in locks:
            f.close()
    return True


def _tidy(archive: Path, dry_run):
    for day in sorted(archive.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]")):
        bases = sorted({m.group(1) for p in day.glob("pendant_*_r*.opus") if (m := COPY_RE.match(p.name[:-5]))})
        for base in bases:
            names = copies(day, base)
            if len(names) < 2:
                continue
            # The largest audio is the only candidate that could hold all the others.
            best = max(names, key=lambda n: (day / f"{n}.opus").stat().st_size)
            others = [n for n in names if n != best]
            held = [n for n in others if compare(day, n, day, best) in ("same", "contains")]
            unproven = [n for n in others if n not in held]
            verb = "would move" if dry_run else "moved"
            _log(f"{day.name}/{base}: {len(names)} copies; keeping {best}")
            for n in held:
                aside = _move_aside(archive, day, n, dry_run)
                _log(f"  {verb} {n} to {aside} (all of it is in {best})")
            for n in unproven:
                _log(f"  kept {n}: can't prove {best} holds all of it")
            if best != base and base in held:
                if dry_run:
                    _log(f"  would rename {best} to {base}")
                else:
                    _place(archive, day, best, day, base)
                    _log(f"  renamed {best} to {base}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--tidy"] and len(args) in (2, 3) and args[2:] in ([], ["--dry-run"]):
        sys.exit(0 if tidy(Path(args[1]), dry_run=args[2:] == ["--dry-run"]) else 1)
    elif len(args) == 2 and not args[0].startswith("-"):
        file_incoming(Path(args[0]), Path(args[1]))
    else:
        sys.exit(__doc__)
