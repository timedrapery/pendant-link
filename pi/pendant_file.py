#!/usr/bin/env python3
"""File new downloads from .incoming into ~/recordings/YYYY-MM-DD/, never losing a better copy.

Usually a download's name is new and it's simply moved into place. When a recording with the
same name is already there (pages that weren't freed were downloaded again), the two are
compared by the pages each holds (from their .json) and their audio size:

- identical: the new copy is a duplicate and is dropped;
- the new copy holds every page the old one did, and at least as much audio: it replaces the
  old one, which is moved aside to ~/recordings/.replaced/ (transcript included), never deleted;
- anything else, or when either copy's pages are unknown: both are kept, the new one under
  the same name with "_r1", "_r2", ... added.

Usage: pendant_file.py INCOMING_DIR ARCHIVE_DIR
"""

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

NAME_RE = re.compile(r"^(pendant_(\d{4})(\d{2})(\d{2})_\d{6})\.(.+)$")
AUDIO_EXTS = ("opus", "json", "wav", "raw", "enc.jsonl")
SIDE_EXTS = ("txt", "words.json")  # derived from the audio; follow it


def _log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _pages(meta_path: Path):
    """(first, last, count) of pages a recording holds, or None if unknown."""
    try:
        m = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    if m.get("page_first") is None or m.get("page_last") is None:
        return None
    return m["page_first"], m["page_last"], m.get("page_count") or (m["page_last"] - m["page_first"] + 1)


def _digest(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _files(directory: Path, base: str):
    return [directory / f"{base}.{ext}" for ext in AUDIO_EXTS + SIDE_EXTS if (directory / f"{base}.{ext}").exists()]


def decide(old_dir: Path, new_dir: Path, base: str):
    """'new', 'duplicate', 'replace' or 'keep-both' for the recording `base`."""
    old_opus, new_opus = old_dir / f"{base}.opus", new_dir / f"{base}.opus"
    if not old_opus.exists():
        return "new"
    if not new_opus.exists():
        return "keep-both"
    old_p, new_p = _pages(old_dir / f"{base}.json"), _pages(new_dir / f"{base}.json")
    if old_p == new_p and old_opus.stat().st_size == new_opus.stat().st_size and _digest(old_opus) == _digest(new_opus):
        return "duplicate"
    if old_p and new_p:
        (of, ol, oc), (nf, nl, nc) = old_p, new_p
        if nf <= of and nl >= ol and nc >= oc and new_opus.stat().st_size >= old_opus.stat().st_size:
            return "replace"
    return "keep-both"


def file_incoming(incoming: Path, archive: Path):
    bases = sorted({m.group(1) for p in incoming.iterdir() if (m := NAME_RE.match(p.name))})
    for base in bases:
        m = re.match(r"pendant_(\d{4})(\d{2})(\d{2})_", base)
        day = archive / f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        day.mkdir(parents=True, exist_ok=True)
        choice = decide(day, incoming, base)
        if choice == "duplicate":
            for f in _files(incoming, base):
                f.unlink()
            _log(f"{base}: same as the copy already filed; dropped the duplicate")
            continue
        target = base
        if choice == "replace":
            aside = archive / ".replaced" / f"{base}-{datetime.now():%Y%m%d_%H%M%S}"
            aside.mkdir(parents=True, exist_ok=True)
            for f in _files(day, base):
                shutil.move(str(f), str(aside / f.name))
            _log(f"{base}: replaced with a longer download of the same recording (old copy kept in {aside})")
        elif choice == "keep-both":
            n = 1
            while (day / f"{base}_r{n}.opus").exists() or (day / f"{base}_r{n}.json").exists():
                n += 1
            target = f"{base}_r{n}"
            _log(f"{base}: a different copy is already filed; keeping both (new one is {target})")
        for f in _files(incoming, base):
            suffix = f.name[len(base):]
            dest = day / f"{target}{suffix}"
            tmp = dest.with_name(dest.name + ".filing")
            shutil.copy2(str(f), str(tmp))       # copy, then rename into place,
            tmp.replace(dest)                     # then remove the incoming copy:
            f.unlink()                            # an interruption never leaves zero copies


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    file_incoming(Path(sys.argv[1]), Path(sys.argv[2]))
