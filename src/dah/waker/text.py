"""Self-contained wake texts sent to a DreamTeam session (a message to all members, or `continue`)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

PHASE_NAMES = {"setup": "СТАРТ", "negotiation_1": "ПЕРЕГОВОРЫ 1", "actions": "ДЕЙСТВИЯ", "negotiation_2": "ПЕРЕГОВОРЫ 2"}


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


def _ready_call(orders_hash: Any) -> str:
    """The exact ready call of an orders phase: with the hash when the phase info carried it."""
    if isinstance(orders_hash, str) and orders_hash:
        return f'dah.ready(confirm_hash="{orders_hash}")'
    return "dah.ready(confirm_hash=orders_hash из dah.phase() или dah.list_orders())"


def _intel(intel: Any) -> str:
    """Espionage games, negotiation_2: «донесений: N; поймано у тебя: M» from the phase info's `intel`."""
    if not isinstance(intel, dict):
        return ""
    text = f"Донесений: {int(intel.get('reports') or 0)}"
    if intel.get("caught"):
        text += f"; поймано у тебя: {int(intel['caught'])}"
    return f" {text} — dah.get_intel()."


def _hint(phase: str, turn: int, orders_hash: Any = None, intel: Any = None) -> str:
    since = f"since_turn={turn - 1}" if turn > 1 else ""
    if phase == "setup":
        return ("Фаза старта перед ходом 1: посмотри dah.get_setup(), выбери фракции по предпочтению "
                "(dah.choose_faction) и артефакт (dah.choose_artifacts), подробности — dah.describe(id) и "
                'dah.rules(topic="setup"); нажми dah.ready() (confirm_hash не нужен).')
    if phase == "negotiation_1":
        return (f"Посмотри dah.get_state({since}) и dah.inbox(), договорись с соседями (dah.send_message, "
                "пакты и подношения), приказы можно только проверить (dah.validate_orders), "
                "подаются они в ДЕЙСТВИЯХ. Как только сказать и сделать больше нечего — сразу dah.ready() "
                "(в П1 без confirm_hash), не жди таймера.")
    if phase == "negotiation_2":
        return ("Посмотри dah.inbox() и dah.list_orders(), при нужде поправь приказы "
                "(dah.amend_order / dah.cancel_order); как только сказать и сделать больше нечего — "
                f"{_ready_call(orders_hash)} (после правки хэш новый).{_intel(intel)}")
    # actions: the hash of the phase's start is stale once orders are given; the submit answer has the new one
    return (f"Посмотри dah.get_state({since}), подай приказы (dah.submit_orders), затем "
            "dah.ready(confirm_hash=orders_hash из ответа dah.submit_orders).")


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
    return (f"{prefix}{head}.{ready} {_hint(phase, turn, phase_info.get('orders_hash'), phase_info.get('intel'))}"
            + _idle_shades(phase, phase_info.get("idle_shades"))
            + _trades_waiting(phase, phase_info.get("trades_waiting")))


def _trades_waiting(phase: str, n: Any) -> str:
    """Core 0.8.0 (docs/TRADE.md 9): the trade offers the seat has to answer in negotiation_1 and negotiation_2
    (phase info `trades_waiting`)."""
    if phase not in ("negotiation_1", "negotiation_2") or isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        return ""
    return f" Обменов ждут ответа: {n} — dah.list_trades(), dah.respond_trade(id, decision)."


def _idle_shades(phase: str, n: Any) -> str:
    """Core 0.7.0 (decision 27): one line about the seat's idle Shades in negotiation_1 and actions (phase info
    `idle_shades`, the server counts the seat's own only)."""
    if phase not in ("negotiation_1", "actions") or isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        return ""
    shades = "свободная Тень" if n == 1 else "свободные Тени" if n < 5 else "свободных Теней"
    return f" У тебя {n} {shades} — dah.list_spies(), dah.estimate_mission(); задание spy подаётся в ДЕЙСТВИЯХ."


RESULT_REASONS = {"turn_limit": "лимит ходов", "last_standing": "остался последний"}


def wrap_up_text(phase_info: dict[str, Any], seat: str | None = None) -> str:
    """The one wrap-up after game_over (a message to all members, or `continue` with --wake-mode continue), e.g. `[DAH] Партия окончена: ход 10, лимит ходов, победитель A; ты B,
    твоё место 2 из 3; счёт: A 12, B 9, C 4. Заверши цель одним коротким итоговым сообщением, больше ничего не
    делай.`"""
    result = phase_info.get("result") if isinstance(phase_info.get("result"), dict) else {}
    parts = []
    turn = phase_info.get("turn")
    if turn:
        parts.append(f"ход {turn}")
    reason = result.get("reason")
    if reason:
        parts.append(RESULT_REASONS.get(reason, str(reason)))
    winners = [str(w) for w in result.get("winners") or []]
    if winners:
        parts.append(("победитель " if len(winners) == 1 else "победители ") + ", ".join(winners))
    elif result:
        parts.append("победителей нет")
    scores = result.get("scores") if isinstance(result.get("scores"), dict) else {}
    head = ", ".join(parts) or "итог в dah.phase()"
    if seat and seat in scores:
        better = sum(1 for s, n in scores.items() if s != seat and n > scores[seat])
        head += f"; ты {seat}, твоё место {better + 1} из {len(scores)}"
    if scores:
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        head += "; счёт: " + ", ".join(f"{s} {n}" for s, n in ranked)
    return (f"[DAH] Партия окончена: {head}. Заверши цель одним коротким итоговым сообщением, "
            "больше ничего не делай.")


def eliminated_text(phase_info: dict[str, Any], seat: str | None = None) -> str:
    """The one notice to an eliminated seat (the game goes on without it), e.g. `[DAH] Ты выбыл (B): к ходу 4
    провинций не осталось, партия для тебя окончена. Заверши цель одним итоговым сообщением, больше ничего не
    делай.`"""
    turn = phase_info.get("turn")
    who = f" ({seat})" if seat else ""
    when = f"к ходу {turn} " if turn else ""
    return (f"[DAH] Ты выбыл{who}: {when}провинций не осталось, партия для тебя окончена. Заверши цель одним "
            "итоговым сообщением, больше ничего не делай.")
