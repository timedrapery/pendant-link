#!/usr/bin/env python3
"""File new downloads from .incoming into ~/recordings/YYYY-MM-DD/, never losing a better copy.

A recording is named after the time of its first page, so when pages weren't freed on the
pendant, the next sync downloads them again under the same name. The new download is then
compared with every copy already filed under that name (the plain name and any _r1, _r2, ...):

- identical to a filed copy: the new download is a duplicate and is dropped;
- it provably holds everything a filed copy does: that copy is moved aside to
  ~/recordings/.replaced/ (transcript included), never deleted. Proof is either
    * the audio of the filed copy is, byte for byte, the start of the new audio, or
    * both copies list their pages, the new one covers the filed one's range, holds at least
      as many pages, and has at least as much audio;
- copies it can't be proven to contain stay where they are, and the new one is filed beside
  them (under the plain name if it's free, otherwise the next _rN).

Usage:
  pendant_file.py INCOMING_DIR ARCHIVE_DIR        file new downloads
  pendant_file.py --tidy ARCHIVE_DIR [--dry-run]  one-off: where a recording has several copies
                                                  (_r1, _r2, ...), keep the one that provably holds
                                                  all the others and move the rest aside
"""

import fcntl
import hashlib
import json
import re
import shutil
import sys
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
    'same', 'contains' (new holds all of old), or 'unproven'."""
    old_opus, new_opus = old_dir / f"{old}.opus", new_dir / f"{new}.opus"
    if not old_opus.exists() or not new_opus.exists():
        return "unproven"
    old_p, new_p = _pages(old_dir / f"{old}.json"), _pages(new_dir / f"{new}.json")
    size_old, size_new = old_opus.stat().st_size, new_opus.stat().st_size
    if old_p == new_p and size_old == size_new and _digest(old_opus) == _digest(new_opus):
        return "same"
    if size_new > size_old and _is_prefix(old_opus, new_opus):
        return "contains"  # the old audio is exactly the start of the new
    if old_p and new_p:
        (of, ol, oc), (nf, nl, nc) = old_p, new_p
        if nf <= of and nl >= ol and nc >= oc and size_new >= size_old:
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


def _move_aside(archive: Path, day: Path, name: str, dry_run=False):
    stem = archive / ".replaced" / f"{name}-{datetime.now():%Y%m%d_%H%M%S}"
    aside, n = stem, 1
    while aside.exists():  # never move into a folder that already holds a copy
        n += 1
        aside = stem.with_name(f"{stem.name}-{n}")
    if not dry_run:
        aside.mkdir(parents=True)
        for f in _files(day, name):
            shutil.move(str(f), str(aside / f.name))
    return aside


def _free_name(day: Path, base: str):
    if not (day / f"{base}.opus").exists() and not (day / f"{base}.json").exists():
        return base
    n = 1
    while (day / f"{base}_r{n}.opus").exists() or (day / f"{base}_r{n}.json").exists():
        n += 1
    return f"{base}_r{n}"


def _place(src_dir: Path, src: str, day: Path, target: str):
    """Copy, rename into place, then remove the source: an interruption never leaves zero copies."""
    for f in _files(src_dir, src):
        dest = day / f"{target}{f.name[len(src):]}"
        tmp = dest.with_name(dest.name + ".filing")
        shutil.copy2(str(f), str(tmp))
        tmp.replace(dest)
        f.unlink()


def file_incoming(incoming: Path, archive: Path):
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
        _place(incoming, base, day, target)


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
    """A .filing file is a copy that never finished; its source is still in place, so it can go."""
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
        _tidy(archive, dry_run)
    finally:
        for f in locks:
            f.close()
    return True


def _tidy(archive: Path, dry_run):
    for day in sorted(archive.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]")):
        if not dry_run:
            remove_leftovers(day)
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
                    _place(day, best, day, base)
                    _log(f"  renamed {best} to {base}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--tidy"] and len(args) in (2, 3) and args[2:] in ([], ["--dry-run"]):
        sys.exit(0 if tidy(Path(args[1]), dry_run=args[2:] == ["--dry-run"]) else 1)
    elif len(args) == 2 and not args[0].startswith("-"):
        file_incoming(Path(args[0]), Path(args[1]))
    else:
        sys.exit(__doc__)
