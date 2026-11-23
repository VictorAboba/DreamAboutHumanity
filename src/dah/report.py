"""Game report built from the journal (SLICE.md section 12): timings, calls, orders, idle, scores, money."""

from __future__ import annotations

from statistics import median
from typing import Any

PHASE_SHORT = {"negotiation_1": "П1", "actions": "Д", "negotiation_2": "П2"}
PHASE_ORDER = ("negotiation_1", "actions", "negotiation_2")
# Seat events counted in the phase open when they were written (a `call` names its own phase).
# `for_phase` of a spend figure taken after game_over (dah-waker, while the session wraps up).
AFTER_GAME_OVER = "after_game_over"
_PHASE_SEAT_EVENTS = frozenset({"first_call_in_phase", "orders_validated", "orders_submitted", "order_edit",
                                "order_edit_p2", "ready"})


def _seat_phase() -> dict[str, Any]:
    return {"first_call_s": None, "first_submit_s": None, "ready_s": None, "calls": 0, "req_chars": 0,
            "resp_chars": 0, "submits": 0, "edits": 0, "p2_edits": 0, "validate_rejected": 0,
            "submit_rejected": 0}


def _seat_total() -> dict[str, Any]:
    return {"calls": 0, "req_chars": 0, "resp_chars": 0, "host_calls": 0, "host_req_chars": 0,
            "host_resp_chars": 0, "submits": 0, "edits": 0, "p2_edits": 0, "validate_rejected": 0,
            "submit_rejected": 0, "submits_by_phase": {p: 0 for p in PHASE_ORDER}, "idle_resources": {},
            "idle_units": 0, "no_orders_turns": [], "not_ready_phases": [], "spent_usd": None,
            "budget_exhausted_turn": None, "telemetry": {}, "spent_in_game_usd": None,
            "spent_after_game_usd": None, "in_game_s": None, "after_game_s": None, "post_game_watch_s": None,
            "wakes_sent": 0, "wakes_answered": 0, "wakes_unanswered": 0, "wake_answer_median_s": None,
            "pacts_signed": 0, "pacts_kept": 0, "pacts_broken": 0, "gifts_sent": 0, "gifts_received": 0,
            "gifted_resources": {}, "received_resources": {}}


def _phase_wake(kind: str, detail: Any) -> bool:
    """One phase wake delivered by dah-waker: `wake_sent` (not the extra `continue` of `--wake-mode both`) or an older
    waker's `continue_sent`, but not the wrap-up after game_over."""
    detail = detail if isinstance(detail, dict) else {}
    if detail.get("reason") == "game_over":
        return False
    return kind == "continue_sent" or (kind == "wake_sent" and not detail.get("also"))


def _after_game_figure(ev: dict[str, Any]) -> bool:
    """A spend figure of the session after game_over: `for_phase` `after_game_over`, or a baseline (no "for" phase)
    taken when the game was already over (a waker started after the match, older versions)."""
    if ev.get("for_phase") == AFTER_GAME_OVER:
        return True
    return "for_turn" in ev and ev.get("for_phase") is None and ev.get("at_phase") == "game_over"


def build_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate journal events into a JSON-ready report.

    A `call` counts in the phase named by its own turn/phase (the one current when it arrived); host calls
    (`host: true`, dah-waker traffic) are counted apart and never in the player's per-phase figures.
    `edits` are cancels/amends in any phase; `p2_edits` are all P2 order changes (adds included).
    `no_orders_turns`: turns the seat resolved with no orders at all (armies stood); `not_ready_phases`:
    [turn, phase] the deadline closed while the seat had not pressed ready. `submits_by_phase` counts submissions
    per phase kind. Money: a `spend_report` counts for the phase it names in `for_turn`/`for_phase` (older
    journals: `turn`/`phase`); a baseline without one only sets the total. A figure for `after_game_over` (the
    session's spend while it wraps up after game_over; a baseline taken after game_over too) counts in no turn or
    phase: `spent_usd` is the last figure of all, `spent_in_game_usd` the last one before it (or the waker's
    `cost_at_game_over_usd` when that is higher: its game_over post was lost), `spent_after_game_usd` their
    difference (or the waker's `post_game_finished` delta when there is no such pair). Both times come from the
    journal clock: `in_game_s` from the first phase to game_over, `after_game_s` from game_over to the waker's
    `post_game_finished` (else to its last after-game figure); `post_game_watch_s` is the waker's own measure of
    its watch (`duration_s`).
    Diplomacy: `pacts_signed` pacts the seat was a side of (accepted), `pacts_broken` those it broke with a "break"
    order, `pacts_kept` the rest (signed and not broken by the seat); `gifts_sent` / `gifts_received` counts and
    `gifted_resources` / `received_resources` summed amounts.
    Wakes (dah-waker): `wakes_sent` phase wakes delivered (the wrap-up not counted), `wakes_answered` /
    `wakes_unanswered` from its `wake_answered` / `wake_unanswered`, `wake_answer_median_s` the median seconds from a
    wake to the player's first game call after it.
    """
    report: dict[str, Any] = {"game_id": None, "scenario": None, "rules_chars": None, "seats": {}, "phases": [],
                              "turns": [], "totals": {}, "result": None}
    phase: dict[str, Any] | None = None
    phase_by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    turns: dict[int, dict[str, Any]] = {}
    current_turn = 1

    def seat_phase(seat: str, entry: dict[str, Any] | None) -> dict[str, Any] | None:
        if entry is None or seat not in report["seats"]:
            return None
        return entry["seats"].setdefault(seat, _seat_phase())

    def since_open(ev: dict[str, Any]) -> float | None:
        return round(ev["ts"] - phase["opened_ts"], 3) if phase else None

    def turn_entry(turn: int) -> dict[str, Any]:
        return turns.setdefault(turn, {"turn": turn, "scores": {}, "idle_resources": {}, "idle_units": {},
                                       "no_orders": [], "spent_usd": {}})

    ready_now: set[str] = set()
    first_open_ts: float | None = None
    over_ts: float | None = None
    after_spend: dict[str, tuple[float, float]] = {}  # seat -> (last after_game_over figure, its ts)
    post_game: dict[str, tuple[dict[str, Any], float]] = {}  # seat -> (detail of its last `post_game_finished`, ts)
    answer_s: dict[str, list[float]] = {}  # seat -> seconds of each answered wake

    for ev in events:
        kind = ev["type"]
        seat = ev.get("seat")
        totals = report["totals"].get(seat) if seat else None
        sp = seat_phase(seat, phase) if seat and kind in _PHASE_SEAT_EVENTS else None
        if kind == "game_created":
            cfg = ev["config"]
            report.update(game_id=cfg["game_id"], scenario=cfg["scenario"], rules_chars=ev.get("rules_chars"),
                          ruleset_hash=ev.get("ruleset_hash"), turn_limit=cfg.get("turn_limit"))
            for s in cfg["seats"]:
                report["seats"][s["seat"]] = {"nick": s["nick"], "kind": s["kind"], "declared": s.get("declared")}
                report["totals"][s["seat"]] = _seat_total()
        elif kind == "phase_open":
            current_turn = ev["turn"]
            if first_open_ts is None:
                first_open_ts = ev["ts"]
            phase = {"turn": ev["turn"], "phase": ev["phase"], "opened_ts": ev["ts"], "closed": None,
                     "duration_s": None, "not_ready": None, "spent_usd": {}, "seats": {}}
            ready_now = set()
            report["phases"].append(phase)
            phase_by_key[(ev["turn"], ev["phase"])] = phase
        elif kind == "phase_close" and phase is not None:
            phase["closed"] = ev["reason"]
            phase["duration_s"] = since_open(ev)
            # Newer journals name the seats; older ones: every seat that had not pressed ready (eliminated too).
            not_ready = ev.get("not_ready")
            if not_ready is None:
                not_ready = sorted(set(report["seats"]) - ready_now)
            phase["not_ready"] = not_ready
            if ev["reason"] == "deadline":
                for s in not_ready:
                    if s in report["totals"]:
                        report["totals"][s]["not_ready_phases"].append([phase["turn"], phase["phase"]])
        elif kind == "first_call_in_phase" and sp is not None:
            sp["first_call_s"] = since_open(ev)
        elif kind == "call" and totals is not None:
            if ev.get("host"):
                totals["host_calls"] += 1
                totals["host_req_chars"] += ev.get("req_chars", 0)
                totals["host_resp_chars"] += ev.get("resp_chars", 0)
                continue
            for target in (totals, seat_phase(seat, phase_by_key.get((ev.get("turn"), ev.get("phase"))))):
                if target is not None:
                    target["calls"] += 1
                    target["req_chars"] += ev.get("req_chars", 0)
                    target["resp_chars"] += ev.get("resp_chars", 0)
        elif kind == "orders_validated" and totals is not None:
            for target in (totals, sp):
                if target is not None:
                    target["validate_rejected"] += len(ev.get("rejected", []))
        elif kind == "orders_submitted" and totals is not None:
            for target in (totals, sp):
                if target is not None:
                    target["submits"] += 1
                    target["submit_rejected"] += len(ev.get("rejected", []))
            submitted_in = ev.get("phase") or (phase["phase"] if phase else None)
            if submitted_in in totals["submits_by_phase"]:
                totals["submits_by_phase"][submitted_in] += 1
            if sp is not None and ev.get("accepted") and sp["first_submit_s"] is None:
                sp["first_submit_s"] = since_open(ev)
        elif kind in ("order_edit", "order_edit_p2") and totals is not None:
            for target in (totals, sp):
                if target is None:
                    continue
                if ev.get("op") != "add":
                    target["edits"] += 1
                if kind == "order_edit_p2":
                    target["p2_edits"] += 1
        elif kind == "ready":
            ready_now.add(seat)
            if sp is not None and sp["ready_s"] is None:
                sp["ready_s"] = since_open(ev)
        elif kind in ("unready", "ready_cleared"):
            ready_now.discard(seat)
        elif kind == "turn_resolved":
            entry = turn_entry(ev["turn"])
            entry.update(scores=ev.get("scores", {}), idle_resources=ev.get("idle_resources", {}),
                         idle_units=ev.get("idle_units", {}), no_orders=ev.get("autopilot", []),
                         state_hash=ev.get("state_hash"))
            for s, res in entry["idle_resources"].items():
                if s in report["totals"]:
                    acc = report["totals"][s]["idle_resources"]
                    for r, n in res.items():
                        acc[r] = acc.get(r, 0) + n
            for s, n in entry["idle_units"].items():
                if s in report["totals"]:
                    report["totals"][s]["idle_units"] += n
            for s in entry["no_orders"]:
                if s in report["totals"]:
                    report["totals"][s]["no_orders_turns"].append(ev["turn"])
        elif kind == "spend_report" and totals is not None and _after_game_figure(ev):
            after_spend[seat] = (ev["spent_usd"], ev["ts"])
            totals["spent_usd"] = ev["spent_usd"]
        elif kind == "spend_report" and totals is not None:
            totals["spent_in_game_usd"] = ev["spent_usd"]
            if "for_turn" in ev:
                for_turn, for_phase = ev["for_turn"], ev.get("for_phase")
            else:
                for_turn, for_phase = ev.get("turn", current_turn), ev.get("phase")
            if for_turn is not None:
                turn_entry(for_turn)["spent_usd"][seat] = ev["spent_usd"]
                target_phase = phase_by_key.get((for_turn, for_phase))
                if target_phase is not None:
                    target_phase["spent_usd"][seat] = ev["spent_usd"]
            totals["spent_usd"] = ev["spent_usd"]
        elif kind == "telemetry" and totals is not None:
            totals["telemetry"][ev["kind"]] = totals["telemetry"].get(ev["kind"], 0) + 1
            detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
            if _phase_wake(ev["kind"], detail):
                totals["wakes_sent"] += 1
            elif ev["kind"] == "wake_answered":
                totals["wakes_answered"] += 1
                if isinstance(detail.get("seconds"), (int, float)) and not isinstance(detail["seconds"], bool):
                    answer_s.setdefault(seat, []).append(float(detail["seconds"]))
            elif ev["kind"] == "wake_unanswered":
                totals["wakes_unanswered"] += 1
            if ev["kind"] == "post_game_finished" and isinstance(ev.get("detail"), dict):
                post_game[seat] = (ev["detail"], ev["ts"])
            if ev["kind"] == "budget_exhausted" and totals["budget_exhausted_turn"] is None:
                totals["budget_exhausted_turn"] = ev.get("turn", current_turn)
        elif kind == "gift" and totals is not None:
            totals["gifts_sent"] += 1
            _add(totals["gifted_resources"], ev.get("resources") or {})
            other = report["totals"].get(ev.get("to"))
            if other is not None:
                other["gifts_received"] += 1
                _add(other["received_resources"], ev.get("resources") or {})
        elif kind == "pact_accepted":
            for s in ev.get("seats", []):
                if s in report["totals"]:
                    report["totals"][s]["pacts_signed"] += 1
        elif kind == "pact_broken":
            for s in ev.get("broken_by", []):
                if s in report["totals"]:
                    report["totals"][s]["pacts_broken"] += 1
        elif kind == "game_over":
            over_ts = ev["ts"]
            report["result"] = {"winners": ev.get("winners", []), "reason": ev.get("reason"),
                                "scores": ev.get("scores", {}), "turn": ev.get("turn")}
    report["turns"] = [turns[t] for t in sorted(turns)]
    in_game_s = round(over_ts - first_open_ts, 3) if over_ts is not None and first_open_ts is not None else None
    report["in_game_s"] = in_game_s
    for seat, totals in report["totals"].items():
        totals["pacts_kept"] = max(0, totals["pacts_signed"] - totals["pacts_broken"])
        totals["in_game_s"] = in_game_s
        if answer_s.get(seat):
            totals["wake_answer_median_s"] = round(median(answer_s[seat]), 1)
        detail, finished_ts = post_game.get(seat, ({}, None))
        at_over = detail.get("cost_at_game_over_usd")
        if (isinstance(at_over, (int, float)) and not isinstance(at_over, bool)
                and not detail.get("started_after_game_over")
                and (totals["spent_in_game_usd"] is None or at_over > totals["spent_in_game_usd"])):
            totals["spent_in_game_usd"] = float(at_over)
        in_game = totals["spent_in_game_usd"]
        delta = detail.get("spent_delta_usd")
        if seat in after_spend and in_game is not None:
            totals["spent_after_game_usd"] = round(max(0.0, after_spend[seat][0] - in_game), 6)
        elif isinstance(delta, (int, float)) and not isinstance(delta, bool):
            totals["spent_after_game_usd"] = round(float(delta), 6)
        if over_ts is not None:
            end_ts = finished_ts if finished_ts is not None else after_spend.get(seat, (None, None))[1]
            if end_ts is not None:
                totals["after_game_s"] = round(max(0.0, end_ts - over_ts), 3)
        if isinstance(detail.get("duration_s"), (int, float)):
            totals["post_game_watch_s"] = float(detail["duration_s"])
    return report


def _add(acc: dict[str, int], amounts: dict[str, Any]) -> None:
    for r, n in amounts.items():
        if isinstance(n, int) and not isinstance(n, bool):
            acc[r] = acc.get(r, 0) + n


def _diplomacy(t: dict[str, Any]) -> str:
    """`; пакты: заключено 2, соблюдено 1, нарушено 1; подношения: отдано 1 (alloy 10), получено 0` when there were any."""
    if not (t.get("pacts_signed") or t.get("pacts_broken") or t.get("gifts_sent") or t.get("gifts_received")):
        return ""

    def amounts(a: dict[str, int]) -> str:
        return f" ({' '.join(f'{r.rsplit(chr(58), 1)[-1]} {n}' for r, n in sorted(a.items()))})" if a else ""

    return (f"; пакты: заключено {t['pacts_signed']}, соблюдено {t['pacts_kept']}, нарушено {t['pacts_broken']}; "
            f"подношения: отдано {t['gifts_sent']}{amounts(t['gifted_resources'])}, получено "
            f"{t['gifts_received']}{amounts(t['received_resources'])}")


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}с"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:.2f}"


def _money_split(t: dict[str, Any]) -> str:
    """` (во время партии $0.67, 3600.0с; после game_over $0.28, 750.0с)` when the seat has after-game data (as
    /watch shows it): human seats and seats without a waker get no split."""
    if t.get("spent_after_game_usd") is None and t.get("after_game_s") is None:
        return ""
    return (f" (во время партии {_usd(t.get('spent_in_game_usd'))}, {_secs(t.get('in_game_s'))}; после game_over "
            f"{_usd(t.get('spent_after_game_usd'))}, {_secs(t.get('after_game_s'))})")


def _wakes(t: dict[str, Any]) -> str:
    """`; пробуждения: отправлено 6, ответ 4, без ответа 2, медиана до хода 35.0с` for a seat a waker woke."""
    if not (t.get("wakes_sent") or t.get("wakes_answered") or t.get("wakes_unanswered")):
        return ""
    return (f"; пробуждения: отправлено {t['wakes_sent']}, ответ {t['wakes_answered']}, без ответа "
            f"{t['wakes_unanswered']}, медиана до хода {_secs(t.get('wake_answer_median_s'))}")


def render_report(report: dict[str, Any]) -> str:
    """Russian text version of build_report()."""
    nick = {s: info["nick"] for s, info in report["seats"].items()}
    lines = [f"ОТЧЁТ ПАРТИИ {report['game_id']} ({report['scenario']})",
             f"Правила: {report['rules_chars']} символов"]
    result = report["result"]
    if result:
        winners = ", ".join(nick.get(w, w) for w in result["winners"]) or "никто"
        lines.append(f"Итог: ход {result['turn']}, {result['reason']}, победители: {winners}")
    else:
        lines.append("Итог: партия не окончена")
    lines.append("")
    lines.append("МЕСТА:")
    for seat, t in report["totals"].items():
        info = report["seats"][seat]
        idle_res = " ".join(f"{r.rsplit(':', 1)[-1]} {n}" for r, n in sorted(t["idle_resources"].items())) or "0"
        by_phase = " / ".join(str(t["submits_by_phase"][p]) for p in PHASE_ORDER)
        not_ready = ", ".join(f"{n} {PHASE_SHORT.get(p, p)}" for n, p in t["not_ready_phases"]) or "—"
        money = "—" if t["spent_usd"] is None else f"${t['spent_usd']:.2f}"
        telemetry = ", ".join(f"{k} {n}" for k, n in sorted(t["telemetry"].items())) or "нет"
        lines.append(
            f"{seat} {info['nick']} ({info['kind']}): вызовов {t['calls']}, запрос {t['req_chars']} / ответ "
            f"{t['resp_chars']} симв.; служебных вызовов (waker) {t['host_calls']}; подач {t['submits']} "
            f"(подач в П1 / Д / П2: {by_phase}), правок "
            f"{t['edits']}, изменений в П2 {t['p2_edits']}; отклонено: validate "
            f"{t['validate_rejected']}, submit {t['submit_rejected']}; простой: ресурсы {idle_res}, юниты-ходы "
            f"{t['idle_units']}; не подал приказов в ходах {t['no_orders_turns'] or '—'}; не нажал готово "
            f"(закрыто по таймеру): {not_ready}; деньги {money}{_money_split(t)}"
            + (f", кончились на ходу {t['budget_exhausted_turn']}" if t["budget_exhausted_turn"] else "")
            + _wakes(t) + _diplomacy(t) + f"; телеметрия: {telemetry}")
    lines.append("")
    lines.append("ФАЗЫ (до первого вызова / первой принятой подачи / ready; вызовы; правки):")
    for ph in report["phases"]:
        head = (f"ход {ph['turn']} {PHASE_SHORT.get(ph['phase'], ph['phase'])}: закрыта "
                f"{ph['closed'] or 'ещё открыта'}, {_secs(ph['duration_s'])}")
        parts = [f"{nick.get(s, s)} {_secs(v['first_call_s'])}/{_secs(v['first_submit_s'])}/{_secs(v['ready_s'])}"
                 f" {v['calls']}в {v['edits']}пр" for s, v in sorted(ph["seats"].items())]
        money = ", ".join(f"{nick.get(s, s)} ${v:.2f}" for s, v in sorted(ph["spent_usd"].items()))
        lines.append(head + ("; " + "; ".join(parts) if parts else "")
                     + (f"; деньги к концу фазы: {money}" if money else ""))
    lines.append("")
    lines.append("ХОДЫ (счёт; деньги нарастающим итогом):")
    for t in report["turns"]:
        sc = ", ".join(f"{nick.get(s, s)} {n}" for s, n in sorted(t["scores"].items()))
        money = ", ".join(f"{nick.get(s, s)} ${v:.2f}" for s, v in sorted(t["spent_usd"].items()))
        lines.append(f"ход {t['turn']}: {sc or '—'}" + (f"; деньги: {money}" if money else ""))
    return "\n".join(lines)
