"""Game report built from the journal (SLICE.md section 12): timings, calls, orders, idle, scores, money."""

from __future__ import annotations

from typing import Any

PHASE_SHORT = {"negotiation_1": "П1", "actions": "Д", "negotiation_2": "П2"}
PHASE_ORDER = ("negotiation_1", "actions", "negotiation_2")
# Seat events counted in the phase open when they were written (a `call` names its own phase).
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
            "budget_exhausted_turn": None, "telemetry": {}}


def build_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate journal events into a JSON-ready report.

    A `call` counts in the phase named by its own turn/phase (the one current when it arrived); host calls
    (`host: true`, dah-waker traffic) are counted apart and never in the player's per-phase figures.
    `edits` are cancels/amends in any phase; `p2_edits` are all P2 order changes (adds included).
    `no_orders_turns`: turns the seat resolved with no orders at all (armies stood); `not_ready_phases`:
    [turn, phase] the deadline closed while the seat had not pressed ready. `submits_by_phase` counts submissions
    per phase kind. Money: a `spend_report` counts for the phase it names in `for_turn`/`for_phase` (older
    journals: `turn`/`phase`); a baseline without one only sets the total.
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
        elif kind == "spend_report" and totals is not None:
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
            if ev["kind"] == "budget_exhausted" and totals["budget_exhausted_turn"] is None:
                totals["budget_exhausted_turn"] = ev.get("turn", current_turn)
        elif kind == "game_over":
            report["result"] = {"winners": ev.get("winners", []), "reason": ev.get("reason"),
                                "scores": ev.get("scores", {}), "turn": ev.get("turn")}
    report["turns"] = [turns[t] for t in sorted(turns)]
    return report


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}с"


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
            f"(закрыто по таймеру): {not_ready}; деньги {money}"
            + (f", кончились на ходу {t['budget_exhausted_turn']}" if t["budget_exhausted_turn"] else "")
            + f"; телеметрия: {telemetry}")
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
