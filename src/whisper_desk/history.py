"""Dictation history: one JSON line per dictation, searchable, reinsertable.

~/.local/state/whisper-desk/history.jsonl holds the date, the duration, the
model and the text of each dictation. The old history.log — one sentence per
line, "date<TAB>text" — is still read; it is folded into the new file the
first time the history is rewritten (a deletion, a purge).
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import config as config_module

logger = logging.getLogger("whisper-desk.history")

FILE_NAME = "history.jsonl"
LEGACY_NAME = "history.log"
# Where the old log goes once folded in: kept, never silently deleted.
LEGACY_BACKUP = "history.log.bak"


@dataclass
class Entry:
    date: str                       # ISO 8601, to the second, local time
    text: str
    duration: float | None = None   # seconds of listening
    model: str | None = None

    @property
    def when(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.date)
        except ValueError:
            return None


def path() -> Path:
    return config_module.STATE_DIR / FILE_NAME


def legacy_path() -> Path:
    return config_module.STATE_DIR / LEGACY_NAME


def _read_legacy() -> list[Entry]:
    try:
        lines = legacy_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        date, tab, text = line.partition("\t")
        if tab and text.strip():
            entries.append(Entry(date=date, text=text.strip()))
    return entries


def _read_lines() -> list[Entry]:
    try:
        lines = path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            raw = json.loads(line)
            entries.append(Entry(
                date=str(raw["date"]),
                text=str(raw["text"]),
                duration=float(raw["duration"]) if raw.get("duration") is not None else None,
                model=str(raw["model"]) if raw.get("model") else None,
            ))
        except (ValueError, KeyError, TypeError):
            # A line cut short by a crash costs that line, not the history.
            continue
    return entries


def load() -> list[Entry]:
    """Every dictation, oldest first."""
    entries = _read_legacy() + _read_lines()
    # A stable sort: two dictations in the same second keep their order.
    return sorted(entries, key=lambda entry: entry.date)


def append(text: str, duration: float | None = None, model: str | None = None,
           keep_days: int = 0, now: datetime | None = None) -> Entry | None:
    """Records a dictation; older ones go if the history keeps only `keep_days`."""
    text = text.strip()
    if not text:
        return None
    now = now or datetime.now()
    entry = Entry(
        date=now.isoformat(timespec="seconds"),
        text=text,
        duration=round(duration, 1) if duration is not None else None,
        model=model,
    )
    try:
        config_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    except OSError as error:
        logger.warning("History not written: %s", error)
        return None
    if keep_days > 0:
        purge(keep_days, now=now)
    return entry


def save(entries: list[Entry]) -> None:
    """Rewrites the whole history — the old log included, folded in for good."""
    config_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path().with_suffix(".jsonl.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    # Written aside then swapped in: a crash leaves the old file, not half of it.
    os.replace(temporary, path())
    if legacy_path().exists():
        legacy_path().replace(config_module.STATE_DIR / LEGACY_BACKUP)


def delete(index: int) -> Entry:
    """Removes a dictation, by its index in load()."""
    entries = load()
    removed = entries.pop(index)
    save(entries)
    return removed


def clear() -> int:
    entries = load()
    save([])
    return len(entries)


def purge(keep_days: int, now: datetime | None = None) -> int:
    """Drops the dictations older than `keep_days`; returns how many went."""
    if keep_days <= 0:
        return 0
    limit = (now or datetime.now()) - timedelta(days=keep_days)
    entries = load()
    kept = [entry for entry in entries if entry.when is None or entry.when >= limit]
    if len(kept) == len(entries):
        return 0
    save(kept)
    return len(entries) - len(kept)


def _fold(text: str) -> str:
    """Case and accents set aside: "resume" finds "Résumé"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def matches(entry: Entry, query: str) -> bool:
    """Every word of the query appears in the text, in any order."""
    text = _fold(entry.text)
    return all(word in text for word in _fold(query).split())


def numbered(entries: list[Entry]) -> list[tuple[int, Entry]]:
    """Newest first, numbered from 1 — the numbers `history --copy N` takes."""
    return [(number, entry) for number, entry in enumerate(reversed(entries), start=1)]


def index_of(number: int, entries: list[Entry]) -> int:
    """The position in load() of the dictation shown as `number`."""
    if not 1 <= number <= len(entries):
        raise IndexError(number)
    return len(entries) - number
