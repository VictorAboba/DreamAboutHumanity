"""Append-only JSONL game journal (SLICE.md section 6): one event `{seq, ts, type, ...}` per line."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

EVENT_TYPES = frozenset({
    "game_created", "seat_info", "phase_open", "phase_close", "recon", "call", "first_call_in_phase",
    "orders_validated", "orders_submitted", "order_edit", "order_edit_p2", "ready", "unready", "ready_cleared",
    "message", "turn_resolved", "spend_report", "telemetry", "game_over",
    # diplomacy (CONCEPT.md section 7): gifts and pacts
    "gift", "pact_proposed", "pact_accepted", "pact_rejected", "pact_withdrawn", "pact_left", "pact_broken",
    "pact_expired", "pact_ended", "pact_lapsed",
    # stage 2 (TECH_AND_SETUP.md): setup phase and research. `setup_choice` (seat, kind, prefs/artifacts) is private
    # until `setup_closed`; the artifacts in `setup_closed` stay hidden from players and spectators until game_over.
    "setup_choice", "setup_closed", "research_started", "tech_completed",
})


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """All events of a journal file; a torn last line (crash mid-write) is skipped."""
    return list(_iter_events(Path(path)))


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


class Journal:
    """Thread-safe writer. `seq` continues from the existing file, so a restarted server appends seamlessly."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = max((e.get("seq", 0) for e in _iter_events(self.path)), default=0)
        self._end_torn_line()
        self._size = self._file_size()

    def _file_size(self) -> int:
        return self.path.stat().st_size if self.path.is_file() else 0

    def _end_torn_line(self) -> None:
        """After a crash mid-write, close the torn last line so the next event starts on a line of its own."""
        if not self.path.is_file() or not self.path.stat().st_size:
            return
        with self.path.open("rb") as fh:
            fh.seek(-1, 2)
            torn = fh.read(1) != b"\n"
        if torn:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write("\n")

    def append(self, type_: str, **fields: Any) -> dict[str, Any]:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown journal event type {type_!r}")
        with self._lock:
            if self._file_size() != self._size:
                # Another writer appended (`dah game set-session` next to a running server): continue its seq.
                self._seq = max(self._seq, max((e.get("seq", 0) for e in _iter_events(self.path)), default=0))
            self._seq += 1
            event = {"seq": self._seq, "ts": round(self._clock(), 3), "type": type_, **fields}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._size = self._file_size()
        return event

    def events(self) -> list[dict[str, Any]]:
        return read_events(self.path)
