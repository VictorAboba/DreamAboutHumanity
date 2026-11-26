"""Game report built from the journal (SLICE.md section 12): timings, calls, orders, idle, scores, money."""

from __future__ import annotations

from statistics import median
from typing import Any

PHASE_SHORT = {"setup": "СТАРТ", "negotiation_1": "П1", "actions": "Д", "negotiation_2": "П2"}
PHASE_ORDER = ("negotiation_1", "actions", "negotiation_2")
# Seat events counted in the phase open when they were written (a `call` names its own phase).
# `for_phase` of a spend figure taken after game_over (dah-waker, while the session wraps up).
AFTER_GAME_OVER = "after_game_over"
# Stage 3 (docs/SPY_AND_CONQUEST.md section 8): journal types counted into `report["stage3"]`.
_STAGE3_EVENTS = frozenset({"shade_hired", "mission_started", "intel_report", "intel_read", "shade_captured",
                            "mission_end", "sabotage", "strike_cancelled", "feint", "feint_exposed", "mission_rejected",
                            "province_captured", "disposition", "occupation_revolt", "claim_created",
                            "policy_unaffordable"})
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
            "idle_resource_turns": {}, "final_resources": None, "idle_units": 0, "no_orders_turns": [], "not_ready_phases": [], "spent_usd": None,
            "budget_exhausted_turn": None, "telemetry": {}, "spent_in_game_usd": None,
            "spent_after_game_usd": None, "in_game_s": None, "after_game_s": None, "post_game_watch_s": None,
            "wakes_sent": 0, "wakes_answered": 0, "wakes_unanswered": 0, "wake_answer_median_s": None,
            "pacts_signed": 0, "pacts_kept": 0, "pacts_broken": 0, "pacts_renewed": 0, "gifts_sent": 0,
            "gifts_received": 0, "gifted_resources": {}, "received_resources": {}, "setup_choices": 0, "techs": []}


def _stage3_seat() -> dict[str, Any]:
    return {"shades_hired": 0, "shades_free": 0, "shades_lost": 0, "missions": {}, "mission_ends": {}, "reports": 0, "intel_reads": 0,
            "caught_here": 0, "caught_here_attributed": 0, "sabotages": 0, "strikes_cancelled": 0, "feints": 0,
            "feints_exposed": 0, "missions_rejected": 0, "captures": {}, "dispositions": {}, "disposition_cost": {},
            "loot": {}, "revolts": 0, "claims": 0, "policy_unaffordable": 0}


def _stage3(report: dict[str, Any], ev: dict[str, Any]) -> None:
    """Count one stage-3 journal event into `report["stage3"]` (per seat; created on the first such event)."""
    block = report.setdefault("stage3", {s: _stage3_seat() for s in report["seats"]})

    def seat(s: Any) -> dict[str, Any] | None:
        return block.get(s) if isinstance(s, str) else None

    kind = ev["type"]
    if kind == "shade_hired" and (t := seat(ev.get("seat"))):
        t["shades_free" if ev.get("free") else "shades_hired"] += 1     # free: held from the start (core 0.6.0)
    elif kind == "mission_started" and (t := seat(ev.get("seat"))):
        mid = str(ev.get("mission")).rsplit(":", 1)[-1]
        t["missions"][mid] = t["missions"].get(mid, 0) + 1
    elif kind == "mission_end" and (t := seat(ev.get("seat"))):
        reason = str(ev.get("reason"))
        t["mission_ends"][reason] = t["mission_ends"].get(reason, 0) + 1
        t["shades_lost"] += reason == "caught"
    elif kind == "intel_report" and (t := seat(ev.get("seat"))):
        t["reports"] += 1
    elif kind == "intel_read" and (t := seat(ev.get("seat"))):
        t["intel_reads"] += 1
    elif kind == "shade_captured" and (t := seat(ev.get("victim"))):
        t["caught_here"] += 1
        t["caught_here_attributed"] += bool(ev.get("attributed"))
    elif kind == "sabotage" and (t := seat(ev.get("seat"))):
        t["sabotages"] += 1
    elif kind == "strike_cancelled" and (t := seat(ev.get("seat"))):
        t["strikes_cancelled"] += 1
    elif kind == "feint" and not ev.get("converted_in_p2") and (t := seat(ev.get("seat"))):
        t["feints"] += 1
    elif kind == "feint_exposed" and (t := seat(ev.get("victim_seat"))):
        t["feints_exposed"] += 1
    elif kind == "mission_rejected" and (t := seat(ev.get("seat"))):
        t["missions_rejected"] += 1
    elif kind == "province_captured" and (t := seat(ev.get("by"))):
        mode = str(ev.get("mode"))
        t["captures"][mode] = t["captures"].get(mode, 0) + 1
    elif kind == "disposition" and (t := seat(ev.get("seat"))):
        did = str(ev.get("disposition")).rsplit(":", 1)[-1]
        t["dispositions"][did] = t["dispositions"].get(did, 0) + 1
        _add(t["disposition_cost"], ev.get("cost") or {})
        _add(t["loot"], ev.get("loot") or {})
    elif kind == "occupation_revolt" and (t := seat(ev.get("seat"))):
        t["revolts"] += 1
    elif kind == "claim_created" and (t := seat(ev.get("seat"))):
        t["claims"] += 1
    elif kind == "policy_unaffordable" and (t := seat(ev.get("seat"))):
        t["policy_unaffordable"] += 1


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
    order, `pacts_kept` the rest (signed and not broken by the seat), `pacts_renewed` renewals the seat agreed to
    (a renewal is not a new signed pact); `gifts_sent` / `gifts_received` counts and
    `gifted_resources` / `received_resources` summed amounts.
    Wakes (dah-waker): `wakes_sent` phase wakes delivered (the wrap-up not counted), `wakes_answered` /
    `wakes_unanswered` from its `wake_answered` / `wake_unanswered`, `wake_answer_median_s` the median seconds from a
    wake to the player's first game call after it.
    Stage 2 (TECH_AND_SETUP.md section 4): each seat's `faction` and `artifacts` (from `setup_closed`; without a
    setup phase the faction of the config, if any), `auto_faction` / `faction_conflict`; `setup_choices` counts
    a seat's choice calls; `techs` per seat are [turn, tech] in order of completion, and each turn lists
    `techs_completed {seat: [tech]}` and `research_started {seat: tech}`. The report is the host's (and the
    revealed spectator's) view: it shows artifacts, which the players see only after game_over.
    Stage 3 (docs/SPY_AND_CONQUEST.md section 8; only when the journal has such events): `stage3 {seat: ...}` counts
    Shades hired and lost, missions by mission and by end reason, reports, intel reads, Shades caught at the seat (and
    how many with the owner named), sabotages, cancelled strikes, false entries (and how many of the seat's were
    exposed), rejected missions, captures by mode, fates by disposition with their price and loot, revolts against
    the seat's occupations, claims it got, and policies it could not pay for. The rates of section 8 (accuracy,
    intel used, fooled by a feint) are 3b and stay offline.
    Core 0.8.0 (docs/SPY_V2.md 11; only when the journal has Shade missions): `spy_v2` (dah.report_spy): per seat the
    deep missions, missions after the intel cap, by turn bucket, idle Shades, needless Observations, intel points by
    kind and target, denials, empty missions, informativeness, catches by C_loc and at Wards, expected against actual
    catches, reports turned into decisions and the sabotage effects; per game the forewarned pact breaks and the builds
    sabotage stopped; and the m10 criteria.
    Core 0.8.0 (docs/TRADE.md 13; only when the journal has trade events): `trade` (dah.report_trade): per seat offers,
    answers, settled and failed trades, accept rate, answer time, bargaining chains, provinces and report copies sold
    and bought, value traded, terms_of_trade, promises (kept / partial / broken / void), reciprocity, kingmaking; per
    game the rates, reference prices, transfers (recaptured, attacked), copies, manual swaps and the promises.
    Stage 5 (docs/ABANDONED_LANDS.md 14.2; only with Abandoned land): `discoveries` — the layout and per seat the finds
    set off, the value they realized (P), the luck against the mean of the seats, choices, the ruins' Treasury, and
    whether the luckiest seat won (see `_discoveries`).
    """
    report: dict[str, Any] = {"game_id": None, "scenario": None, "rules_chars": None, "seats": {}, "phases": [],
                              "turns": [], "totals": {}, "result": None, "setup": None}
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
        return turns.setdefault(turn, {"turn": turn, "scores": {}, "idle_resources": {}, "resources": {},
                                       "idle_units": {},
                                       "no_orders": [], "spent_usd": {}, "techs_completed": {},
                                       "research_started": {}})

    ready_now: set[str] = set()
    first_open_ts: float | None = None
    over_ts: float | None = None
    after_spend: dict[str, tuple[float, float]] = {}  # seat -> (last after_game_over figure, its ts)
    post_game: dict[str, tuple[dict[str, Any], float]] = {}  # seat -> (detail of its last `post_game_finished`, ts)
    answer_s: dict[str, list[float]] = {}  # seat -> seconds of each answered wake

    for ev in events:
        kind = ev["type"]
        if kind in _STAGE3_EVENTS:
            _stage3(report, ev)
        seat = ev.get("seat")
        totals = report["totals"].get(seat) if seat else None
        sp = seat_phase(seat, phase) if seat and kind in _PHASE_SEAT_EVENTS else None
        if kind == "game_created":
            cfg = ev["config"]
            report.update(game_id=cfg["game_id"], scenario=cfg["scenario"], rules_chars=ev.get("rules_chars"),
                          ruleset_hash=ev.get("ruleset_hash"), turn_limit=cfg.get("turn_limit"))
            report["setup"] = ({"artifacts": cfg["setup"].get("artifacts"), "closed": None}
                               if cfg.get("setup") else None)
            for s in cfg["seats"]:
                report["seats"][s["seat"]] = {"nick": s["nick"], "kind": s["kind"], "declared": s.get("declared"),
                                              "faction": s.get("faction"), "artifacts": []}
                report["totals"][s["seat"]] = _seat_total()
        elif kind == "setup_choice" and totals is not None:
            totals["setup_choices"] += 1
        elif kind == "setup_closed":
            if report.get("setup") is not None:
                report["setup"]["closed"] = ev.get("reason")
            for s, res in (ev.get("results") or {}).items():
                if s in report["seats"]:
                    report["seats"][s].update(faction=res.get("faction"), artifacts=list(res.get("artifacts") or []),
                                              auto_faction=res.get("auto_faction"),
                                              faction_conflict=res.get("faction_conflict"))
        elif kind == "research_started":
            turn_entry(ev["turn"])["research_started"][seat] = ev.get("tech")
        elif kind == "tech_completed":
            turn_entry(ev["turn"])["techs_completed"].setdefault(seat, []).append(ev.get("tech"))
            if totals is not None:
                totals["techs"].append([ev["turn"], ev.get("tech")])
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
            if "score_parts" in ev:        # non-default scoring (SCORING.md 9): the breakdown of every seat
                entry["score_parts"] = ev["score_parts"]
            entry.update(scores=ev.get("scores", {}), idle_resources=ev.get("idle_resources", {}),
                         resources=ev.get("resources", {}), idle_units=ev.get("idle_units", {}),
                         no_orders=ev.get("autopilot", []), state_hash=ev.get("state_hash"))
            for s, res in entry["idle_resources"].items():
                if s in report["totals"]:
                    # the stock left after the turn's orders, summed over turns (resource-turns, not a stock);
                    # `idle_resources` is the old key of the same sum, kept for compatibility
                    acc = report["totals"][s]["idle_resource_turns"]
                    for r, n in res.items():
                        acc[r] = acc.get(r, 0) + n
                    report["totals"][s]["idle_resources"] = dict(acc)
            for s, res in entry["resources"].items():      # journals since m6: the stock at the end of the turn
                if s in report["totals"]:
                    report["totals"][s]["final_resources"] = dict(res)
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
        elif kind == "pact_renewed":
            for s in ev.get("seats", []):
                if s in report["totals"]:
                    report["totals"][s]["pacts_renewed"] += 1
        elif kind == "pact_broken":
            for s in ev.get("broken_by", []):
                if s in report["totals"]:
                    report["totals"][s]["pacts_broken"] += 1
        elif kind == "game_over":
            over_ts = ev["ts"]
            report["result"] = {"winners": ev.get("winners", []), "reason": ev.get("reason"),
                                "scores": ev.get("scores", {}), "turn": ev.get("turn")}
            for key in ("score_parts", "objectives", "winners_open"):
                if key in ev:
                    report["result"][key] = ev[key]
    report["turns"] = [turns[t] for t in sorted(turns)]
    scoring = _scoring(events, report)
    if scoring is not None:
        report["scoring"] = scoring
    finds = _discoveries(events, report)
    if finds is not None:
        report["discoveries"] = finds
    from dah.report_spy import spy_v2
    spy = spy_v2(events, list(report["seats"]))     # core 0.8.0 (docs/SPY_V2.md 11); only with Shade missions
    if spy is not None:
        report["spy_v2"] = spy
    from dah.report_trade import trade_report
    trade = trade_report(events, list(report["seats"]))     # core 0.8.0 (docs/TRADE.md 13); only with trade events
    if trade is not None:
        report["trade"] = trade
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


SCORE_LABELS = {"territory": "зем", "science": "нау", "works": "стр", "war": "вой", "intel": "разв",
                "objectives": "цель", "betrayal": "разрыв"}


def _scoring(events: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any] | None:
    """Stage 4 (SCORING.md 9), journals with a non-default scoring only: per seat the final `score_parts`, the revealed
    `objectives` and `strategy_profile` (share of each source in the final total); `reveal_flip` (did the objectives
    change the winners); `war_pairs` [{killer, victim, value, defense_value, fights}]; `works` {started, completed,
    lost} per seat (lost: a completed work whose province was captured later); `fights` per turn (war_credit events
    and attacks suffered by each seat). Metrics are for the checking side (decision 18): the game only journals."""
    created = next((e for e in events if e.get("type") == "game_created"), {})
    scored = bool(created.get("scoring")) or any(e.get("type") in ("war_credit", "work_started", "objectives_dealt")
                                                 or "score_parts" in e for e in events)
    if not scored:
        return None
    result = report.get("result") or {}
    parts = result.get("score_parts") or {}
    finals = result.get("scores") or {}
    seats: dict[str, Any] = {}
    for seat in report["seats"]:
        p = parts.get(seat) or {}
        total = sum(p.values())
        seats[seat] = {"score_parts": p, "objectives": (result.get("objectives") or {}).get(seat, []),
                       "strategy_profile": {k: round(v / total, 3) for k, v in p.items()} if total > 0 else {},
                       "final": finals.get(seat)}
    flip = None
    if result.get("winners_open") is not None and result.get("reason") == "turn_limit":
        flip = sorted(result["winners_open"]) != sorted(result.get("winners") or [])
    revealed = {(e.get("seat"), e.get("id")): e for e in events if e.get("type") == "objectives_revealed"}
    for seat, t in seats.items():
        t["objectives"] = [{**o, **{k: revealed[(seat, o.get("id"))][k] for k in ("counted", "evidence")
                                    if k in revealed.get((seat, o.get("id")), {})}} for o in t["objectives"]]
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    fights: dict[int, dict[str, Any]] = {}
    works: dict[str, dict[str, int]] = {s: {"started": 0, "completed": 0, "lost": 0, "stalled": 0}
                                        for s in report["seats"]}
    # core 0.7.0 (decision 27): war points per province {seat: {province: points}} and intel points per seat
    war_provinces: dict[str, dict[str, int]] = {}
    intel: dict[str, int] = {}
    reclaims: dict[str, dict[str, int]] = {}     # core 0.7.1: {seat: {province: returns of own land (0 points)}}
    done: dict[str, str] = {}        # province -> seat whose completed work stands there
    for e in events:
        kind = e.get("type")
        if kind == "war_credit":
            key = (e["killer"], e["victim"])
            row = pairs.setdefault(key, {"killer": key[0], "victim": key[1], "value": 0, "defense_value": 0,
                                         "fights": 0})
            row["value"] += e.get("value", 0)
            row["fights"] += 1
            if e.get("defender"):
                row["defense_value"] += e.get("value", 0)
            f = fights.setdefault(e.get("turn", 0), {"turn": e.get("turn", 0), "credits": 0, "attacked": {}})
            f["credits"] += 1
            if e.get("defender"):
                f["attacked"][e["killer"]] = f["attacked"].get(e["killer"], 0) + 1
        elif kind == "work_started" and e.get("seat") in works:
            works[e["seat"]]["started"] += 1
        elif kind == "build_stalled" and e.get("seat") in works:
            works[e["seat"]]["stalled"] += 1          # turns a build paid in parts stood without its share
        elif kind == "war_points" and e.get("seat"):
            row = war_provinces.setdefault(e["seat"], {})
            row[e["province"]] = row.get(e["province"], 0) + int(e.get("points", 0))
            if e.get("reason") == "reclaim":       # core 0.7.1: a return of one's own land, 0 war points
                reclaims.setdefault(e["seat"], {})
                reclaims[e["seat"]][e["province"]] = reclaims[e["seat"]].get(e["province"], 0) + 1
        elif kind == "intel_point" and e.get("seat"):
            intel[e["seat"]] = intel.get(e["seat"], 0) + int(e.get("points", 0))
        elif kind == "work_completed" and e.get("seat") in works:
            works[e["seat"]]["completed"] += 1
            done[e["province"]] = e["seat"]
        elif kind == "province_captured" and done.get(e.get("province")) in works:
            works[done.pop(e["province"])]["lost"] += 1
    total_war = sum(r["value"] for r in pairs.values())
    return {"seats": seats, "reveal_flip": flip, "war_pairs": sorted(pairs.values(), key=lambda r: (r["killer"],
                                                                                                    r["victim"])),
            "defense_share": round(sum(r["defense_value"] for r in pairs.values()) / total_war, 3) if total_war
            else None,
            "works": works, "fights": [fights[t] for t in sorted(fights)],
            "war_provinces": war_provinces, "war_reclaims": reclaims, "intel_points": intel,
            "gift_share_of_score_spend": _gift_share(events, list(report["seats"]))}


# Stage 5 (docs/ABANDONED_LANDS.md 10, 14.2): journal types counted into `report["discoveries"]`
_FIND_EVENTS = frozenset({"discoveries_placed", "discoveries_unfair", "discovery_found", "discovery_chosen",
                          "discovery_units", "guard_cleared", "lair_grew", "order_clipped"})


def _find_seat() -> dict[str, Any]:
    return {"finds": 0, "by_kind": {}, "value": 0.0, "find_value": 0.0, "choice_value": 0.0, "reward_value": 0.0,
            "luck": None, "units_lost": 0, "units_gained": 0, "resources": {}, "artifacts": [], "choices": [],
            "treasury": None, "lairs_cleared": 0, "guards_cleared": 0, "joint": 0, "orders_clipped": 0}


def _discoveries(events: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any] | None:
    """Stage 5, journals with Abandoned land only: the layout (finds placed, accepted, spread), per seat the finds it set
    off (by kind), what they realized in P (finds, mixed choices, rewards of the guards of finds; the ruins' Treasury
    apart, since it is no luck), units lost and gained, artifacts, the choices made, the ruins it took; `luck` = the
    seat's realized value minus the mean of the seats; `luckiest` and whether that seat won (the G3 metric of 14.3).
    Report only: the game neither knows nor uses it."""
    if not any(e.get("type") in _FIND_EVENTS for e in events):
        return None
    seats = {s: _find_seat() for s in report["seats"]}
    placed = next((e for e in events if e.get("type") == "discoveries_placed"), {})
    spawned: set[str] = set()      # provinces where a find put neutrals (their guard is the find's; else the ruins')
    out: dict[str, Any] = {"placed": len(placed.get("finds") or {}), "accepted": placed.get("accepted"),
                           "spread": placed.get("spread"), "abandoned": list(placed.get("abandoned") or []),
                           "triggered": 0, "lairs_grew": 0, "seats": seats}

    def add(acc: dict[str, int], amounts: dict[str, Any] | None) -> None:
        for r, n in (amounts or {}).items():
            acc[r] = acc.get(r, 0) + int(n)

    for e in events:
        kind = e.get("type")
        if kind == "discovery_found":
            out["triggered"] += 1
            if (e.get("province_effects") or {}).get("neutrals"):
                spawned.add(str(e.get("province")))
            entrants = [s for s in e.get("entrants") or [] if s in seats]
            for s in entrants:
                t = seats[s]
                share = (e.get("shares") or {}).get(s) or {}
                v = float((e.get("value") or {}).get(s, 0.0))
                t["finds"] += 1
                fk = str(e.get("find_kind"))
                t["by_kind"][fk] = t["by_kind"].get(fk, 0) + 1
                t["find_value"] += v
                t["joint"] += len(entrants) > 1
                t["units_lost"] += sum((share.get("lost") or {}).values())
                t["units_gained"] += sum((share.get("units") or {}).values())
                add(t["resources"], share.get("resources"))
                if share.get("artifact"):
                    t["artifacts"].append(share["artifact"])
        elif kind == "discovery_chosen" and e.get("seat") in seats:
            t = seats[e["seat"]]
            v = float(e.get("value") or 0.0)
            t["choice_value"] += v
            t["units_lost"] += sum((e.get("lost") or {}).values())
            t["units_gained"] += sum((e.get("units") or {}).values())
            add(t["resources"], e.get("resources"))
            if e.get("artifact"):
                t["artifacts"].append(e["artifact"])
            t["choices"].append({"turn": e.get("turn"), "province": e.get("province"), "id": e.get("id"),
                                 "choice": e.get("choice"), "by_default": bool(e.get("by_default")),
                                 "outcome": e.get("outcome"), "value": v})
        elif kind == "guard_cleared" and e.get("by") in seats:
            t = seats[e["by"]]
            v = float(e.get("value") or 0.0)
            add(t["resources"], e.get("resources"))
            t["units_gained"] += sum((e.get("units") or {}).values())
            if e.get("province") not in spawned:
                t["treasury"] = {"province": e.get("province"), "turn": e.get("turn"), "value": v}
                continue
            t["reward_value"] += v
            t["lairs_cleared" if e.get("lair") else "guards_cleared"] += 1
        elif kind == "lair_grew":
            out["lairs_grew"] += 1
        elif kind == "order_clipped" and e.get("seat") in seats:
            seats[e["seat"]]["orders_clipped"] += 1
    for t in seats.values():
        t["value"] = round(t["find_value"] + t["choice_value"] + t["reward_value"], 2)
        for key in ("find_value", "choice_value", "reward_value"):
            t[key] = round(t[key], 2)
    if seats:
        mean = sum(t["value"] for t in seats.values()) / len(seats)
        for t in seats.values():
            t["luck"] = round(t["value"] - mean, 2)
        top = max(t["value"] for t in seats.values())
        best = [s for s, t in seats.items() if t["value"] == top]
        out["luckiest"] = best[0] if len(best) == 1 and any(t["finds"] for t in seats.values()) else None
        winners = (report.get("result") or {}).get("winners") or []
        out["luckiest_won"] = (out["luckiest"] in winners) if out["luckiest"] and report.get("result") else None
    return out


_FIND_KIND_TEXT = {"boon": "плюшки", "hindrance": "помехи", "mixed": "смешанные"}


def _discovery_lines(report: dict[str, Any], nick: dict[str, str]) -> list[str]:
    d = report.get("discoveries")
    if not d:
        return []
    fair = {True: "в бюджете честности", False: "вне бюджета (discoveries_unfair)"}.get(d.get("accepted"), "—")
    lines = ["", f"НАХОДКИ (заброшенные: {', '.join(d['abandoned']) or '—'}; разложено {d['placed']}, {fair}, "
                 f"разброс {d.get('spread')}; сработало {d['triggered']}; логово росло {d['lairs_grew']} раз):"]
    for seat, t in d["seats"].items():
        kinds = ", ".join(f"{_FIND_KIND_TEXT.get(k, k)} {n}" for k, n in sorted(t["by_kind"].items()))
        choices = ", ".join(f"{c['id'].rsplit(':', 1)[-1] if c.get('id') else '?'}: {c['choice']}"
                            + (" (по умолч.)" if c["by_default"] else "") + f" {c['value']:+g}" for c in t["choices"])
        tre = t["treasury"]
        parts = [f"находок {t['finds']}" + (f" ({kinds})" if kinds else ""),
                 f"ценность {t['value']:+g} P (находки {t['find_value']:+g}, выбор {t['choice_value']:+g}, "
                 f"награды {t['reward_value']:+g}), удача {t['luck']:+g} к среднему"]
        if choices:
            parts.append(f"выбор: {choices}")
        if t["units_lost"] or t["units_gained"]:
            parts.append(f"юнитов потеряно {t['units_lost']}, получено {t['units_gained']}")
        if t["resources"]:
            parts.append("ресурсы " + _amounts(t["resources"]))
        if t["artifacts"]:
            parts.append("артефакты " + ", ".join(_short(a) for a in t["artifacts"]))
        if tre:
            parts.append(f"Казна руин {tre['province']} на ходу {tre['turn']} ({tre['value']:+g} P)")
        if t["lairs_cleared"] or t["guards_cleared"]:
            parts.append(f"зачищено: логов {t['lairs_cleared']}, стражей находок {t['guards_cleared']}")
        if t["joint"]:
            parts.append(f"совместных входов {t['joint']}")
        lines.append(f"{nick.get(seat, seat)}: " + "; ".join(parts))
    if d.get("luckiest"):
        won = {True: "победил", False: "не победил", None: "партия не окончена"}[d.get("luckiest_won")]
        lines.append(f"удачливее всех: {nick.get(d['luckiest'], d['luckiest'])} ({won})")
    return lines


GIFT_WINDOW = 3     # turns: a gift counts towards the recipient's score spend of this turn and the next two


def _gift_share(events: list[dict[str, Any]], seat_ids: list[str]) -> dict[str, float | None]:
    """Per seat: the share of what it paid for great works and research (journaled `cost` of `work_started` and
    `tech_completed`) that gifts received in the last GIFT_WINDOW turns covered (each gift counted once, all
    resources summed). None: no such spend (or a journal from before the costs were journaled). Report only
    (decision 18): the game neither knows nor uses it."""
    gifts: dict[str, list[list[int]]] = {s: [] for s in seat_ids}      # recipient -> [[turn, amount left]]
    spend: dict[str, list[int]] = {s: [0, 0] for s in seat_ids}         # [spent, covered by gifts]
    for e in events:
        kind = e.get("type")
        if kind == "gift" and e.get("to") in gifts:
            amount = sum(n for n in (e.get("resources") or {}).values() if isinstance(n, int))
            gifts[e["to"]].append([int(e.get("turn") or 0), amount])
        elif kind in ("work_started", "tech_completed") and e.get("seat") in spend and "cost" in e:
            cost = e["cost"] if isinstance(e["cost"], int) else sum((e["cost"] or {}).values())
            turn = int(e.get("turn") or 0)
            row = spend[e["seat"]]
            row[0] += cost
            need = cost
            for g in gifts[e["seat"]]:
                if need <= 0:
                    break
                if turn - GIFT_WINDOW < g[0] <= turn and g[1] > 0:
                    used = min(need, g[1])
                    g[1] -= used
                    need -= used
                    row[1] += used
    return {s: (round(c / p, 3) if p else None) for s, (p, c) in spend.items()}


def _score_lines(report: dict[str, Any], nick: dict[str, str]) -> list[str]:
    """«Аргос 21 = зем 10 + нау 4 + стр 2 + вой 2 + цель 3 (forge_lord ✓)» per seat."""
    sc = report.get("scoring")
    if not sc:
        return []
    lines = ["", "ОЧКИ (итог по источникам):"]
    for seat, t in sc["seats"].items():
        body = " + ".join(f"{SCORE_LABELS.get(k, k)} {v}" for k, v in t["score_parts"].items()) or "—"
        objs = ", ".join(_objective_line(o, nick) for o in t["objectives"])
        lines.append(f"{nick.get(seat, seat)} {t['final'] if t['final'] is not None else '—'} = {body}"
                     + (f" ({objs})" if objs else ""))
    if sc.get("reveal_flip") is not None:
        lines.append(f"Цели поменяли победителя: {'да' if sc['reveal_flip'] else 'нет'}")
    for row in sc["war_pairs"]:
        lines.append(f"война: {nick.get(row['killer'], row['killer'])} ← {nick.get(row['victim'], row['victim'])} "
                     f"{row['value']} (в обороне {row['defense_value']}), боёв {row['fights']}")
    if sc.get("defense_share") is not None:
        lines.append(f"доля обороны в очках войны: {round(100 * sc['defense_share'])}%")
    works = "; ".join(f"{nick.get(s, s)} {w['started']}/{w['completed']}/{w['lost']}"
                      + (f", стояли {w['stalled']} х." if w.get("stalled") else "")
                      for s, w in sc["works"].items() if any(w.values()))
    if works:
        lines.append(f"великие стройки (начато/достроено/потеряно): {works}")
    back = sc.get("war_reclaims") or {}
    wp = "; ".join(f"{nick.get(s, s)} " + ", ".join(
        f"{p} {n}" + (f" (возврат своей ×{back[s][p]}: 0)" if p in back.get(s, {}) else "")
        for p, n in sorted(rows.items())) for s, rows in sorted((sc.get("war_provinces") or {}).items()))
    if wp:
        lines.append(f"война по провинциям (очки): {wp}")
    ip = "; ".join(f"{nick.get(s, s)} {n}" for s, n in sorted((sc.get("intel_points") or {}).items()) if n)
    if ip:
        lines.append(f"разведка (очки за успешные задания): {ip}")
    gifts = "; ".join(f"{nick.get(s, s)} {round(100 * v)}%" for s, v in (sc.get("gift_share_of_score_spend") or {}).items()
                      if v)
    if gifts:
        lines.append(f"подношения в цене строек и исследований получателя (за 3 хода): {gifts}")
    return lines


def _objective_line(o: dict[str, Any], nick: dict[str, str]) -> str:
    """«forge_lord +3 ✓ (2/2)», «kingbreaker → Ник ✗ (0/1)»; «не в счёт» for an eliminated seat's objective."""
    target = f" → {nick.get(o['target'], o['target'])}" if o.get("target") else ""
    ev = o.get("evidence") or {}
    have = f" ({ev['have']}/{ev['need']})" if "have" in ev and "need" in ev else ""
    counted = " не в счёт" if o.get("met") and o.get("counted") is False else ""
    return f"{_short(o['id'])}{target} +{o.get('points', 0)} {'✓' if o.get('met') else '✗'}{have}{counted}"


def _parts_short(parts: dict[str, Any]) -> str:
    return " ".join(f"{SCORE_LABELS.get(k, k)} {v}" for k, v in parts.items())


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

    renewed = f", продлено {t['pacts_renewed']}" if t.get("pacts_renewed") else ""
    return (f"; пакты: заключено {t['pacts_signed']}{renewed}, соблюдено {t['pacts_kept']}, нарушено "
            f"{t['pacts_broken']}; "
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


def _short(full_id: Any) -> str:
    return str(full_id).rsplit(":", 1)[-1]


def _seat_setup(info: dict[str, Any], t: dict[str, Any]) -> str:
    """`, фракция litany, артефакты ore_vein; технологии 4: deep_drilling (х2), ...` when there is stage-2 data."""
    out = ""
    if info.get("faction"):
        out += f", фракция {_short(info['faction'])}" + (" (по умолчанию)" if info.get("auto_faction") else "") + (
            " (уступил при совпадении)" if info.get("faction_conflict") else "")
    if info.get("artifacts"):
        out += ", артефакты " + ", ".join(_short(a) for a in info["artifacts"])
    if t.get("techs"):
        out += f"; технологии {len(t['techs'])}: " + ", ".join(f"{_short(x)} (х{n})" for n, x in t["techs"])
    return out


def _stage3_line(t: dict[str, Any]) -> str:
    def counts(d: dict[str, Any]) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "—"

    def amounts(d: dict[str, Any]) -> str:
        return " ".join(f"{_short(r)} {n}" for r, n in sorted(d.items())) or "0"

    free = f" (+{t['shades_free']} даром)" if t.get("shades_free") else ""
    return (f"Тени: нанято {t['shades_hired']}{free}, потеряно {t['shades_lost']}; задания {counts(t['missions'])} "
            f"(итоги: {counts(t['mission_ends'])}); донесений {t['reports']}, прочтений {t['intel_reads']}; поймано у "
            f"него {t['caught_here']} (хозяин установлен {t['caught_here_attributed']}); саботажей {t['sabotages']}"
            f" (отменено {t['strikes_cancelled']}); ложных записей {t['feints']} (разоблачено чужими "
            f"{t['feints_exposed']}); отклонено заданий {t['missions_rejected']}; захваты {counts(t['captures'])}; "
            f"судьбы {counts(t['dispositions'])}, цена {amounts(t['disposition_cost'])}, добыча {amounts(t['loot'])}; "
            f"бунтов {t['revolts']}, притязаний {t['claims']}, политика не по средствам {t['policy_unaffordable']}")


def _amounts(amounts: dict[str, Any]) -> str:
    """`alloy 12 provisions 5` (short resource ids), or `0`."""
    return " ".join(f"{r.rsplit(':', 1)[-1]} {n}" for r, n in sorted(amounts.items())) or "0"


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
    if report.get("setup"):
        st = report["setup"]
        lines.append(f"Фаза старта: артефакты {st.get('artifacts')}, закрыта "
                     f"{st.get('closed') or 'ещё открыта'}")
    lines.append("")
    lines.append("МЕСТА:")
    for seat, t in report["totals"].items():
        info = report["seats"][seat]
        idle_res = _amounts(t.get("idle_resource_turns", t["idle_resources"]))
        final_res = "—" if t.get("final_resources") is None else _amounts(t["final_resources"])
        by_phase = " / ".join(str(t["submits_by_phase"][p]) for p in PHASE_ORDER)
        not_ready = ", ".join(f"{n} {PHASE_SHORT.get(p, p)}" for n, p in t["not_ready_phases"]) or "—"
        money = "—" if t["spent_usd"] is None else f"${t['spent_usd']:.2f}"
        telemetry = ", ".join(f"{k} {n}" for k, n in sorted(t["telemetry"].items())) or "нет"
        lines.append(
            f"{seat} {info['nick']} ({info['kind']}{_seat_setup(info, t)}): вызовов {t['calls']}, запрос {t['req_chars']} / ответ "
            f"{t['resp_chars']} симв.; служебных вызовов (waker) {t['host_calls']}; подач {t['submits']} "
            f"(подач в П1 / Д / П2: {by_phase}), правок "
            f"{t['edits']}, изменений в П2 {t['p2_edits']}; отклонено: validate "
            f"{t['validate_rejected']}, submit {t['submit_rejected']}; простой: остаток ресурсов после приказов, "
            f"сумма по ходам (ресурс-ходы) {idle_res}, юниты-ходы {t['idle_units']}; запасы к концу партии "
            f"{final_res}; не подал приказов в ходах {t['no_orders_turns'] or '—'}; не нажал готово "
            f"(закрыто по таймеру): {not_ready}; деньги {money}{_money_split(t)}"
            + (f", кончились на ходу {t['budget_exhausted_turn']}" if t["budget_exhausted_turn"] else "")
            + _wakes(t) + _diplomacy(t) + f"; телеметрия: {telemetry}")
    if report.get("stage3"):
        lines.append("")
        lines.append("ШПИОНАЖ И ЗАВОЕВАНИЕ:")
        for seat, t in report["stage3"].items():
            lines.append(f"{seat} {nick.get(seat, seat)}: " + _stage3_line(t))
        if report.get("spy_v2"):
            from dah.report_spy import spy_v2_lines
            lines += spy_v2_lines(report["spy_v2"], nick)
    if report.get("trade"):
        from dah.report_trade import trade_lines
        lines += trade_lines(report["trade"], nick)
    lines += _score_lines(report, nick)
    lines += _discovery_lines(report, nick)
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
    lines.append("ХОДЫ (счёт [по источникам]; запасы к концу хода; деньги нарастающим итогом):")
    for t in report["turns"]:
        by_src = t.get("score_parts") or {}
        sc = ", ".join(f"{nick.get(s, s)} {n}" + (f" [{_parts_short(by_src[s])}]" if by_src.get(s) else "")
                       for s, n in sorted(t["scores"].items()))
        money = ", ".join(f"{nick.get(s, s)} ${v:.2f}" for s, v in sorted(t["spent_usd"].items()))
        techs = "; ".join(f"{nick.get(s, s)} {', '.join(_short(x) for x in v)}"
                          for s, v in sorted(t.get("techs_completed", {}).items()))
        stock = "; ".join(f"{nick.get(s, s)} {_amounts(v)}" for s, v in sorted(t.get("resources", {}).items()))
        lines.append(f"ход {t['turn']}: {sc or '—'}" + (f"; запасы: {stock}" if stock else "")
                     + (f"; изучено: {techs}" if techs else "")
                     + (f"; деньги: {money}" if money else ""))
    return "\n".join(lines)
