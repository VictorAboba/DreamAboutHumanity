"""Self-contained wake texts sent to a DreamTeam session as `continue`."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

PHASE_NAMES = {"negotiation_1": "ПЕРЕГОВОРЫ 1", "actions": "ДЕЙСТВИЯ", "negotiation_2": "ПЕРЕГОВОРЫ 2"}


def _deadline_hhmm(deadline: Any) -> str | None:
    if isinstance(deadline, (int, float)):
        moment = datetime.fromtimestamp(deadline, tz=timezone.utc)
    elif isinstance(deadline, str):
        try:
            moment = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError:
            return deadline
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    else:
        return None
    return moment.astimezone(timezone.utc).strftime("%H:%MZ")


def _hint(phase: str, turn: int) -> str:
    since = f"since_turn={turn - 1}" if turn > 1 else ""
    if phase == "negotiation_1":
        return (f"Посмотри dah.get_state({since}) и dah.inbox(), договорись с соседями (dah.send_message), "
                "нажми dah.ready.")
    if phase == "negotiation_2":
        return ("Посмотри dah.inbox() и dah.list_orders(), при нужде поправь приказы "
                "(dah.amend_order / dah.cancel_order), нажми dah.ready.")
    return f"Посмотри dah.get_state({since}), подай приказы, нажми dah.ready."


def wake_text(phase_info: dict[str, Any], *, reminder: bool = False) -> str:
    """E.g. `[DAH] Ход 3/10, фаза ДЕЙСТВИЯ, до 14:30Z (ещё 20 мин). Готово 1/3. Посмотри ...`."""
    turn = int(phase_info.get("turn") or 0)
    phase = str(phase_info.get("phase", ""))
    limit = phase_info.get("turn_limit")
    parts = [f"Ход {turn}/{limit}" if limit else f"Ход {turn}", f"фаза {PHASE_NAMES.get(phase, phase)}"]
    timing = []
    hhmm = _deadline_hhmm(phase_info.get("deadline"))
    if hhmm:
        timing.append(f"до {hhmm}")
    seconds_left = phase_info.get("seconds_left")
    if isinstance(seconds_left, (int, float)):
        timing.append(f"(ещё {max(1, math.ceil(seconds_left / 60))} мин)")
    head = ", ".join(parts) + (", " + " ".join(timing) if timing else "")
    prefix = "[DAH] Напоминание: фаза скоро закроется, ты ещё не нажал ready. " if reminder else "[DAH] "
    ready = f" Готово {phase_info['ready']}." if phase_info.get("ready") else ""
    return f"{prefix}{head}.{ready} {_hint(phase, turn)}"
