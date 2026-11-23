"""DreamAboutHumanity engine: a pure library (no I/O except reading pack files).

Public API (everything importable from ``dah.engine``):

Content
    build_ruleset(pack_dirs | None, scenario_id) -> Ruleset        # None = default_pack_dirs(); "slice3" or "core:slice3"
    validate_packs(pack_dirs) -> (errors: list[Issue], warnings: list[Issue])   # for `dah pack validate`
    default_pack_dirs() -> list[Path]                              # content/packs/* of this repo
    ContentError(issues: list[Issue]);  Issue{file, path, message}.to_dict()
    Ruleset: .hash, .scenario (id, name, map, factions, turn_limit,
             phase_limits{negotiation_1, actions, negotiation_2} in seconds,
             message_limits{negotiation_1:{per_recipient, private, public}, negotiation_2:{...}, max_chars}),
             .map (provinces, adjacency, starts, capitals), .units/.buildings/.resources/.province_types/.factions,
             .stat(path, faction=None), .resolve(kind, short_or_full_id), .short(full_id), .warnings

State (model)
    new_game_state(ruleset, seats=[{seat, nick, faction?}], seed) -> GameState   # turn 1, phase "negotiation_1"
    GameState{turn, phase, provinces, players, pending_builds, seq, result, pacts}; .to_dict() / .from_dict()
    state_hash(state) -> str          # sha256 of canonical JSON, `seq` excluded (and `pacts` while empty)
    issue_order_id(state, seat) -> "o<N>"   # bumps state.seq[seat]
    Order (.to_dict()/from_dict(); JSON keys: id, kind, phase_added, from, to, units, pact | province, unit, count,
           building); a move's `pact` is "honor" (default) or "break"
    SeatOrders{orders, cp_spent, forfeit}: one seat's book for the current turn; persist it per turn for replay.

Orders (phase "actions" is free, "negotiation_2" is priced in CP, anything else PHASE_LOCKED: in "negotiation_1"
        orders are only validated, as for "actions"; Check/EditResult carry a `warning` for a move into a pact
        partner's province)
    submit_orders(state, ruleset, seat, book, items, phase, mode="append"|"replace_all", id_factory=None)
        -> SubmitResult{book (new), results: list[Check], accepted, rejected}   # dry-run validate = ignore .book
    check_order(state, ruleset, seat, book, item, phase) -> Check{ok, reason, message, cp_cost, order}.to_dict()
    cancel_order(state, ruleset, seat, book, order_id, phase) -> EditResult{ok, book, reason, message, cp_cost}
    amend_order(state, ruleset, seat, book, order_id, patch{units?|to?|count?}, phase) -> EditResult
    orders_hash(book) -> str;  cp_left(state, seat, book) -> int;  reserved_resources(ruleset, faction, book)
    Reasons: NOT_ADJACENT NOT_OWNER NOT_ENOUGH_UNITS NOT_ENOUGH_RESOURCES NO_RECRUIT_BUILDING ALREADY_BUILT
             PHASE_LOCKED NOT_ENOUGH_CP UNKNOWN_ID INVALID (malformed input)

Resolution
    resolve(state, orders_by_seat{seat: SeatOrders | list[Order]}, ruleset, seed, diplomacy=()) -> (new_state,
            TurnReport)          # diplomacy: the turn's ops (already applied to state), for the report only
        new_state.turn is incremented and phase set to "negotiation_1", or phase "game_over" with
        new_state.result = {winners, reason: last_standing|turn_limit, scores}.
    TurnReport{turn, events, by_seat{seat: [visible events]}, public{ownership_changes, eliminated, scores,
               result, diplomacy}, idle{seat: {resources, units}}}.to_dict() / from_dict()

Diplomacy (engine/diplomacy.py: gifts and pacts)
    make_gift / make_proposal / make_response / make_leave(state, ...) -> op   # DiplomacyError on refusal
    apply_op(state, op) / apply_ops(state, ops)   # the live game and replay apply the same ops in the same order
    scores(state, ruleset) -> {seat: points};  visible_provinces(state, ruleset, seat) -> set[str]

Views (Russian text)
    view_state(state, ruleset, seat, book=None) -> dict              # fog-filtered JSON (format=json)
    render_state_text(state, ruleset, seat, deadline_info: DeadlineInfo(deadline "14:30Z", seconds_left) | None,
                      since_turn=None, since_state=None, reports=(), book=None) -> str
        since_state = snapshot at the start of `since_turn`, reports = TurnReports of turns >= since_turn.
    render_report_text(report, ruleset, state, seat) -> str           # /v1/history
    rules_text(ruleset) -> str                                        # /v1/rules, <= 6000 chars
    estimate(ruleset, attacker_units, defender_units, province_type, fortified=False, hasty=False)
        -> {win_prob, attacker_losses_avg, defender_losses_avg}     # 200 runs, fixed seed; KeyError on bad ids
    rng.stream(seed, turn, system, entity_id) -> random.Random
"""

from .combat import estimate
from .content import ContentError, Issue, Ruleset, build_ruleset, default_pack_dirs, validate_packs
from .model import GameState, Order, SeatOrders, issue_order_id, state_hash
from .orders import (Check, EditResult, SubmitResult, amend_order, cancel_order, check_order, cp_left,
                     orders_hash, reserved_resources, submit_orders)
from .resolve import SetupError, TurnReport, new_game_state, resolve, scores, visible_provinces
from .view import DeadlineInfo, render_report_text, render_state_text, rules_text, view_state

__all__ = [
    "Check", "ContentError", "DeadlineInfo", "EditResult", "GameState", "Issue", "Order", "Ruleset", "SeatOrders",
    "SetupError", "SubmitResult", "TurnReport", "amend_order", "build_ruleset", "cancel_order", "check_order",
    "cp_left", "default_pack_dirs", "estimate", "issue_order_id", "new_game_state", "orders_hash",
    "render_report_text", "render_state_text", "reserved_resources", "resolve", "rules_text", "scores",
    "state_hash", "submit_orders", "validate_packs", "view_state", "visible_provinces",
]
