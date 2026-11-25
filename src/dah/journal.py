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
    "pact_expired", "pact_ended", "pact_lapsed", "pact_renewed", "pact_renewal_cancelled",
    # stage 2 (TECH_AND_SETUP.md): setup phase and research. `setup_choice` (seat, kind, prefs/artifacts) is private
    # until `setup_closed`; the artifacts in `setup_closed` stay hidden from players and spectators until game_over.
    "setup_choice", "setup_closed", "research_started", "tech_completed",
    # stage 3 (SPY_AND_CONQUEST.md 8). Conquest: fates of captured provinces, policies, claims, revolts, ruins.
    "province_captured", "disposition", "policy_set", "policy_unaffordable", "dispose_failed", "occupation_revolt",
    "claim_created", "claim_ended", "ruin_ended",
    # espionage (private to their seats; full level only)
    "shade_hired", "mission_started", "spy_round", "intel_report", "intel_read", "shade_captured", "mission_end",
    "sabotage", "strike_cancelled", "feint", "feint_exposed", "mission_rejected",
    # stage 4 (SCORING.md 9): objectives (private to the seat until game_over), war credit (to the fight's sides),
    # great works (public), the reveal at the end
    "objectives_dealt", "war_credit", "work_started", "work_completed", "objectives_revealed",
    # stage 5 (ABANDONED_LANDS.md 10, 16): the layout (admin-only until game_over), finds and choices (to their seats)
    "discoveries_placed", "discoveries_unfair", "fair_mirror_skipped", "discovery_found", "discovery_chosen",
    "discovery_units", "discovery_pending", "order_clipped", "guard_cleared", "lair_grew",
    # core 0.7.0 (decision 27, to their seat): war points per province, intel points, the shares of works paid in parts
    "war_points", "intel_point", "build_paid", "build_stalled",
})

# Visibility of scoring events in a non-admin view of the journal (view_events)
_SEAT_PRIVATE = {"objectives_dealt": ("seat",), "war_credit": ("killer", "victim"),
                 # stage 5: a find to its entrants, a choice and its consequences to the holder
                 "discovery_found": ("seats",), "discovery_chosen": ("seat",), "discovery_units": ("seat",),
                 "discovery_pending": ("seat",), "order_clipped": ("seat",),
                 # core 0.7.0
                 "war_points": ("seat",), "intel_point": ("seat",), "build_paid": ("seat",), "build_stalled": ("seat",)}
# the layout of finds: nobody but the admin until game_over
_ADMIN_ONLY = frozenset({"discoveries_placed", "discoveries_unfair", "fair_mirror_skipped"})


# config fields that deal the secret objectives: admin-only until game_over
_SECRET_CONFIG = ("seed", "objectives_salt")


def view_events(events: list[dict[str, Any]], *, seat: str | None = None, admin: bool = False,
                spectator: bool = False) -> list[dict[str, Any]]:
    """A journal as a non-admin may read it (SCORING.md 5, 9). Until game_over: `game_created` loses the secret
    `seed` and `objectives_salt` (also inside `config`); `objectives_dealt` and `war_credit` only for their seats (`seat`); `turn_resolved`
    keeps only the seat's own `scores`/`score_parts` entry. A `spectator` (the delayed level) sees every score, never
    objectives or war credit. After game_over (or for the admin) everything is shown."""
    if admin or any(e.get("type") == "game_over" for e in events):
        return [dict(e) for e in events]
    out = []
    for e in events:
        kind = e.get("type")
        if kind == "game_created":
            e = {k: v for k, v in e.items() if k not in _SECRET_CONFIG}
            if isinstance(e.get("config"), dict):
                e["config"] = {k: v for k, v in e["config"].items() if k not in _SECRET_CONFIG}
        elif kind in _ADMIN_ONLY:
            continue
        elif kind in _SEAT_PRIVATE:
            sides = [x for f in _SEAT_PRIVATE[kind] for x in (e.get(f) if isinstance(e.get(f), list) else [e.get(f)])]
            if spectator or seat is None or seat not in sides:
                continue
        elif kind == "turn_resolved" and not spectator:
            e = dict(e)
            for field in ("scores", "score_parts"):
                if isinstance(e.get(field), dict):
                    e[field] = {k: v for k, v in e[field].items() if k == seat}
        out.append(dict(e))
    return out


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
