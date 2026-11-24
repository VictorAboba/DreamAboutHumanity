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
    rules_text(ruleset, *, setup=..., extra=()) -> str                # /v1/rules: core <= 6000 chars + contents
    rules_topic(ruleset, topic, *, setup=..., extra=()) -> str | None  # /v1/rules?topic=techs|factions|...
    estimate(ruleset, attacker_units, defender_units, province_type, fortified=False, hasty=False,
             *, attacker_key=None, defender_key=None, defense_mul=None)
        -> {win_prob, attacker_losses_avg, defender_losses_avg}     # 200 runs, fixed seed; KeyError on bad ids
    rng.stream(seed, turn, system, entity_id) -> random.Random

Stage 2 (docs/TECH_AND_SETUP.md: techs, abilities, setup phase, artifacts)
    stat_key(state, ruleset, seat) -> faction id | Profile   # the seat's key for ruleset.stat(path, key): techs,
        artifacts and owner-scoped buildings; a faction id while it has none (the slice's per-faction table)
    Ruleset: .techs/.branches/.artifacts, .research_resource, .tech_cost(tech, key), .passive_income(key),
             .unlocks(key) -> {"unit:<id>", "building:<id>", "order:<kind>"}, .gated_orders, .scenario.setup
    income_of(state, ruleset, seat, key=...) / upkeep_of(...)    # key defaults to stat_key
    seat_reserved(state, ruleset, seat, book)                    # reserved_resources with the key and discounts
    Order kind "research" {tech}: only in actions (validate-only in negotiation_1, PHASE_LOCKED in negotiation_2);
        reasons NOT_AVAILABLE (locked unit/building/tech/order), ALREADY_RESEARCHED
    techs_view(state, ruleset, seat, research_income=None) -> own tree;  techs_text(view, ruleset) -> str
    tech_counts(state, rs) -> {seat: n}                          # public (artifact grants not counted)
    describe(ruleset, kind, id) -> dict with Russian "text";  find(ruleset, ref) -> [(kind, id)]
    setup_config(ruleset, artifacts=None, offer=None, points=None) -> dict | None
    draft_offers(ruleset, setup, seats, seed) -> {seat: [artifact ids]}; artifact_pool(ruleset, setup)
    check_faction_prefs(ruleset, prefs) / check_artifacts(ruleset, setup, offer, ids) -> ids  # SetupChoiceError
    resolve_setup(ruleset, setup, seed, seats, choices, offers) -> {seat: {faction, prefs, artifacts,
        auto_faction, auto_artifacts, faction_conflict, dropped}}
    new_game_state(..., seats=[{seat, nick, faction?, artifacts?}], ...)   # artifacts applied at the start

Stage 3 (docs/SPY_AND_CONQUEST.md; 3a so far: switches and conquest)
    build_ruleset(pack_dirs, scenario_id, systems={espionage|conquest: bool | None} | None)  # host override of the
        scenario's `systems`; Ruleset.system(name) -> bool; .scenario.systems (the ones on; hashed only while any)
    Ruleset.dispositions {id: DispositionDef(id, name, name_en, behavior annex|raze|raid|release, cost, loot, params)}
        (empty while conquest is off); ProvinceTypeDef.conquest_value (1..5, default 1)
    registry: SYSTEMS, ORDER_SYSTEMS, ORDER_PHASE_RULES {kind: {p2_new, p2_edit, recon_consumed}},
        MISSION_BEHAVIORS, DISPOSITION_BEHAVIORS, StatSpec.system (stage-3 stats are never seeded)
    State: Province.status {occupied{by,from,since}, ruined_until, ravaged_until, looted_turn, sabotaged};
        Player.home / shades / spy_seq / conquest_policy; GameState.claims [{seat, province, until_turn}] / origins
        {pid: seat} — all left out of state_hash while empty
    Orders: {"kind":"dispose","province","disposition","to"?} and {"kind":"policy","disposition": id | null}
        (conquest; 0 CP in Д and П2, free P2 edits); hire_shade/spy/recall/ward (espionage); move "feint": true.
        Reasons BAD_TARGET NOT_OCCUPIED OCCUPIED RUINED NO_RECIPIENT (+ espionage ones in orders.STAGE3_REASONS)
    engine.conquest: is_full/occupied/is_ruined/is_ravaged, living_capital, check_place, claim_of, disposition_cost,
        disposition_loot, statuses_for (public statuses), ConquestStep (resolution step 4b)
    Ruleset.missions {id: MissionDef(behavior, target province|owned_province|seat, difficulty, rounds_min/max,
        whisper, requires_tech, cost, cost_per_round, params, loud)} and Ruleset.espionage (EspionageDef: Shade name,
        cost, upkeep, ward cost) — empty / None while espionage is off; pack dir `espionage/*.yaml` {config?, missions}
    engine.espionage (3a): recon(state_at_close_of_actions, books_d, ruleset, seed) -> (state' in negotiation_2,
        report {turn, rounds, reports{seat: [{id, facts, truth, ...}]}, captures{victim: [...]}, lost, strikes, hired,
        started, ended, rejected, feints, hash}); EspionageStep (resolution steps 1, 1b, 2b, the end);
        estimate_mission, spies_text (list_spies), intel_text (get_intel), report_text, state_line (ДОНЕСЕНИЯ),
        notice_text (the inbox entry), honest_range, chances; orders: spy {shade, mission, target, rounds},
        recall {shade}, hire_shade; move "feint": true costs feint.cp_cost on cp_spent in Д

Stage 4 (docs/SCORING.md: victory points, score under fog)
    build_ruleset(pack_dirs, scenario_id, systems=None, scoring={src: weight} | None, objectives=N | None)
    Ruleset.weight(src), .default_scoring, .system("scoring.<src>"), .objectives {id: ObjectiveDef};
        ScenarioDef.scoring (all five sources), .objectives {dealt, offer, pool}; BuildingDef.score / max_per_owner /
        allowed_types; registry.SCORE_SOURCES, OBJECTIVE_KINDS, score.* stats (system scoring.<src>)
    engine.scoring: scores, score_parts (weighted), final_scores, final_parts, score_known(state, rs, viewer, seat),
        objective_status/objective_progress, credit_war, deal_objectives, validate_objectives, score_line,
        objectives_view, science_view; Player.tally {war_value, homes_taken}, Player.objectives [{id, target?}]
    TurnReport.scores / .score_parts (private; public has no scores), scores_of(); report events war_credit (sides),
        work_started / work_completed (public); result + score_parts, objectives, winners_open (non-default scoring)
"""

from .catalog import describe, describe_effect, find
from .combat import estimate
from .content import ContentError, Issue, Profile, Ruleset, build_ruleset, default_pack_dirs, validate_packs
from .model import GameState, Order, SeatOrders, issue_order_id, state_hash
from .orders import (Check, EditResult, SubmitResult, amend_order, cancel_order, check_order, cp_left,
                     orders_hash, reserved_resources, seat_reserved, submit_orders)
from .resolve import SetupError, TurnReport, income_of, new_game_state, resolve, scores, upkeep_of, visible_provinces
from .setup import (SetupChoiceError, artifact_pool, check_artifacts, check_faction_prefs, draft_offers,
                    resolve_setup, setup_config)
from .techs import stat_key, tech_counts, techs_text, techs_view
from .rules import RULES_LIMIT, rules_text, rules_topic, topic_key
from .view import DeadlineInfo, render_report_text, render_state_text, view_state

__all__ = [
    "Check", "ContentError", "DeadlineInfo", "EditResult", "GameState", "Issue", "Order", "Profile", "Ruleset",
    "SeatOrders", "SetupChoiceError", "SetupError", "SubmitResult", "TurnReport", "amend_order", "artifact_pool",
    "build_ruleset", "cancel_order", "check_artifacts", "check_faction_prefs", "check_order", "cp_left",
    "default_pack_dirs", "describe", "describe_effect", "draft_offers", "estimate", "find", "income_of",
    "issue_order_id", "new_game_state", "orders_hash", "render_report_text", "render_state_text",
    "RULES_LIMIT", "reserved_resources", "resolve", "resolve_setup", "rules_text", "rules_topic", "scores", "seat_reserved", "setup_config",
    "stat_key", "state_hash", "submit_orders", "tech_counts", "techs_text", "techs_view", "topic_key", "upkeep_of",
    "validate_packs", "view_state", "visible_provinces",
]
