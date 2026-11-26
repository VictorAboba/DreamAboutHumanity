"""Balance simulator: bots play many games against each other in-process (the engine directly, no server).

    uv run python scripts/simulate.py                      # every configuration, 200 games each
    uv run python scripts/simulate.py --games 50 --config rush,builder,diplomat
    uv run python scripts/simulate.py --combat             # only the combat table (estimate by army ratio)
    uv run python scripts/simulate.py --packs path/to/packs_dir   # another copy of the packs (e.g. an older core)
    uv run python scripts/simulate.py --scenario core:quad4 --games 30     # 4 players on mirror4 (4-seat configs)
    uv run python scripts/simulate.py --factions bots --artifacts points   # bots pick factions; points mode

The bots are the real ones from `dah.bots` (the same decision code that plays over HTTP); an in-process adapter
stands in for the server API: state views, orders, estimate, gifts and pacts go through the engine functions the
server uses. Each configuration is played with the bot types rotated over the seats (and the starts dealt by the
seed), so no type keeps one seat. Output: plain-text tables (or --json).

Stage 2 (TECH_AND_SETUP.md): a scenario with a setup phase (or `--artifacts draft|points`) plays it in-process:
factions are dealt by rotation over the scenario's list (`--factions rotate`, the default, so a faction's win rate is
not tied to one bot type) or chosen by the bots' styles (`--factions bots`); artifacts are always the bots' own picks
(the passive bot takes the defaults). The bots research through the same `list_techs` view as over HTTP. The tables
add win rate per faction and per artifact (with its lift, raw and adjusted for the holders' factions and bot types),
the share of games reaching a tier-2 tech, techs per game, and the most researched techs. `--json-out FILE` also
writes the JSON tallies (per configuration and ALL). The balance pass of core 0.3.0: docs/SLICE.md.

Stage 3 (docs/SPY_AND_CONQUEST.md section 7): with espionage on, the recon step runs in-process at the close of
actions (the same `recon` the server runs) and the bots read their reports from its result (the same data as
`get_intel(format=json)`). `--espionage on|off|both` and `--conquest on|off|both` (default: the scenario's) pick the
switch combinations; each combination gets its own tables (win rates per bot type and faction, Synclite apart) and its
own `--json` key ("espionage=on,conquest=off"). The espionage table: Shades per seat, missions per mission (success,
caught, empty), Shade survival, sabotage strikes, P2 attacks after a strike and their wins, false entries (placed,
exposed, made real), captures (attributed or not) and the alloy spent on Shades (and on replacing lost ones). The
conquest table: fates by disposition and source, provisions spent on subjugation and idle at the end, occupation
length, revolts, returns, and loot per province (how often one province was looted and what it gave).

Stage 4 (docs/SCORING.md section 10): `--scoring SPEC` (`land` = territory only, `scenario` = the scenario's weights,
`all` = every source at 1, `both` = land and the scenario's, or weights such as `territory=1,war=2`; repeatable) and
`--objectives N` give each weight set its own tables and `--json` key ("...,scoring=land"). The score table: the true
final score by source per bot type and faction (decision 18), the winner's score by source, objectives met per id x
bot type (a passive probe plays alongside when objectives are on: `passive` is the floor of easy objectives),
`reveal_flip`, provisions / alloy / creed idle at turn 8, great works (started, completed, lost), war pairs with a
feeding flag, the defence share of war credit, battles per turn and the share of seats attacked by a seat, and the
win share of seats holding no central province at turn 6 (provinces outside every start: a simulator notion only).
The pass corridors of SCORING.md 10 (gates, at most six) and the tracked ones are printed per weight set with
PASS / FAIL / track, and under "corridors" in the JSON.

Stage 5 (docs/ABANDONED_LANDS.md 14): with the Abandoned lands on (seats fewer than the map's starts, e.g.
`--scenario core:slice3 --config rush,builder`), the finds table (layout, finds that went off, the value realized per
entry by army size and per seat, mixed choices by rank, ruins and Treasury, lairs, losses to neutrals, lift per find,
win share by start position) and the gates G1-G8 (`######## discovery gates`; G1, G2 and last_standing against the
same switches with the system off when `--discoveries both` is played). `--discovery-knob key=value` sets a scenario
knob (e.g. `souls=halve`, `density=1.0`); `--scout-sweep` replays every configuration holding a builder with that
builder as the `scout` bot (one unit into every Abandoned province) on the same seeds, and once more as its control
(`scout_control`: the scout's extra Militia, no sweep), and gates the scout against the control (decision 25 a; the
row against the builder, which also counts the extra Militia, is tracked). Step 5 (balance core 0.6.0) added the whole
worth of an entry by army size (the find, the seat's later choice and guard reward there, minus every unit it lost to
the find's neutrals), 1-2 unit entries by find, great works by work and the pact breakers (share, win share, penalty,
wins the penalty cost). Core 0.7.0 (decision 27) adds the `core 0.7.0` line (JSON `scoring.core070`): the Spire
(started / completed, turns), stalled works, seat-to-seat captures, `war_points` captures / recaptures / holds, provinces
taken >= 3 times (ping-pong), pairs at the per-province cap, war per seat and seats at the total cap, the intel points
per seat, Shade missions per game and the winner's points by source.

Core 0.8.0 (docs/SPY_V2.md 10): with espionage on, the `spy v2` line and the gates printed as `######## spy gates`
(JSON `spy_gates`; `spy` in the tallies): the bot checks (share of deep missions secret / read_orders / sabotage,
missions after the intel cap per seat, the G5 pace of turns 10-12 against turns 1-3, the rush bot's P2 cancels and
hasty attacks after a report) and the balance gates (Shade survival per mission, great works stopped by sabotage per
game and those left unfinished, sabotage revolts per game, the shift of the win share per bot type against
`--spy-base FILE` (the `--json-out` of the same run on the previous core, e.g. `--packs` with content/legacy/core-0.7.1),
the intel share of the winner's score, read_orders points from one target per seat, empty Observations).
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dah.bots import BOT_TYPES
from dah.bots.builder import BuilderBot
from dah.bots.scout import ScoutBot
from dah.bots.tactics import ArmyOk, Reserved
from dah.bots.view import StateView, parse_state
from dah.bots.base import Bot, seat_seed
from dah.bots.client import ApiError
from dah.bots.choices import pick_artifacts
from dah.engine import (
    SeatOrders,
    SetupChoiceError,
    cancel_order,
    artifact_pool,
    build_ruleset,
    check_artifacts,
    check_faction_prefs,
    cp_left,
    default_pack_dirs,
    draft_offers,
    estimate,
    income_of,
    new_game_state,
    orders_hash,
    resolve,
    resolve_setup,
    rules_text,
    seat_reserved,
    setup_config,
    stat_key,
    submit_orders,
    techs_view,
    upkeep_of,
    view_state,
)
from dah.engine import diplomacy as dip
from dah.engine import discoveries as disc
from dah.engine import espionage as esp
from dah.engine.combat import unit_price
from dah.engine import scoring as sc_
from dah.engine import trade as tr
from dah.engine.registry import SCORE_SOURCES, TURN_SOURCES
from dah.engine.content import Ruleset
from dah.engine.model import GameState
from dah.engine.setup import artifact_choices, faction_choices

PHASES = ("negotiation_1", "actions", "negotiation_2")
REPLY_PASSES = 2        # core 0.8.0 (docs/TRADE.md 12.2): answer-only passes after a negotiation phase
SEATS = ("A", "B", "C", "D", "E", "F", "G", "H")
DEFAULT_CONFIGS = (
    "rush,builder,diplomat",
    "rush,rush,builder", "builder,builder,rush",
    "rush,rush,diplomat", "diplomat,diplomat,rush",
    "builder,builder,diplomat", "diplomat,diplomat,builder",
    "rush,rush,rush", "builder,builder,builder", "diplomat,diplomat,diplomat",
)
DEFAULT_CONFIGS_4 = (  # maps with 4 starts or more (quad4)
    "rush,builder,diplomat,builder", "rush,builder,diplomat,rush", "rush,builder,diplomat,diplomat",
)
# nominal ratio (sum of attack of the attackers / sum of defence of the defenders, no multipliers) -> bucket
RATIO_BUCKETS = ((0.0, 0.8, "<0.8"), (0.8, 1.25, "0.8-1.25 (even)"), (1.25, 1.75, "1.25-1.75 (~1.5x)"),
                 (1.75, 99.0, ">=1.75"))


# ---------------------------------------------------------------- in-process API

class Match:
    """One game: the state, the turn's order books and diplomacy ops, and per-phase diplomacy counts."""

    def __init__(self, rs: Ruleset, seats: list[dict[str, Any]], seed: str) -> None:
        self.rs = rs
        self.seed = seed
        self.state: GameState = new_game_state(rs, seats, seed)
        self.books: dict[str, SeatOrders] = {}
        self.ops: list[dict[str, Any]] = []
        self.counts: Counter[tuple[str, str]] = Counter()
        self.recon: dict[str, Any] | None = None    # the last recon step (espionage)
        self.messages: list[tuple[int, str, str, str]] = []     # (turn, phase, seat, text) of the bots' messages
        self.recons: list[dict[str, Any]] = []      # every recon step played (a trade may sell any own report)
        # core 0.8.0 (docs/TRADE.md 12.2): the trade ops of the game with what they did ({op, turn, phase, trade})
        self.trade_log: list[dict[str, Any]] = []
        self.gifts: list[tuple[int, str, str, str, dict[str, int]]] = []   # (turn, phase, from, to, resources)
        self.state_line_max = 0     # the longest ОБМЕН line a seat's get_state showed (gate G7)


_ESTIMATES: dict[Any, float] = {}


class EngineApi:
    """The bots' `Api`/`DiplomacyApi` over the engine, for one seat of a Match."""

    def __init__(self, match: Match, seat: str) -> None:
        self.m = match
        self.seat = seat

    def _book(self) -> SeatOrders:
        return self.m.books.setdefault(self.seat, SeatOrders())

    def whoami(self) -> dict[str, Any]:
        return {"seat": self.seat, "nick": self.m.state.players[self.seat].nick}

    def phase(self) -> dict[str, Any]:
        return {"phase": self.m.state.phase}

    def wait_phase(self, after_seq: int, max_seconds: int) -> dict[str, Any]:
        return self.phase()

    def state(self) -> dict[str, Any]:
        return {"state": view_state(self.m.state, self.m.rs, self.seat, self._book())}

    def orders(self) -> dict[str, Any]:
        book = self._book()
        return {"orders": [o.to_dict() for o in book.orders], "orders_hash": orders_hash(book),
                "cp_left": cp_left(self.m.state, self.seat, book)}

    def submit_orders(self, orders: list[dict[str, Any]], mode: str) -> dict[str, Any]:
        res = submit_orders(self.m.state, self.m.rs, self.seat, self._book(), orders, self.m.state.phase, mode)
        self.m.books[self.seat] = res.book
        return {"orders_hash": orders_hash(res.book)}

    def estimate(self, attacker: dict[str, int], defender: dict[str, int], province_type: str, fortified: bool,
                 hasty: bool) -> float:
        key = (self.m.rs.hash, tuple(sorted(attacker.items())), tuple(sorted(defender.items())), province_type,
               bool(fortified), bool(hasty))
        if key not in _ESTIMATES:
            _ESTIMATES[key] = estimate(self.m.rs, attacker, defender, province_type, fortified, hasty)["win_prob"]
        return _ESTIMATES[key]

    def estimate_report(self, attacker: dict[str, int], report_id: str, hasty: bool) -> dict[str, Any]:
        """Core 0.8.0: estimate(defender="r3") by one of the seat's Observation reports of this recon step (the
        engine's `report_defender`, as the server; base stats, like `estimate`)."""
        report = next((r for r in ((self.m.recon or {}).get("reports") or {}).get(self.seat, [])
                       if r.get("id") == report_id), None)
        if report is None or report.get("behavior") != esp.OBSERVE:
            raise ApiError("UNKNOWN_ID", report_id)
        d = esp.report_defender(report, self.m.state.turn, report_id,
                                persists=bool(self.m.rs.stat("combat.fortify_persists")))
        ptype = self.m.state.provinces[d["province"]].type
        out = {"win_prob": self.estimate(attacker, d["units"], ptype, d["fortified"], hasty),
               "defender": d["units"], "report": d["header"]}
        if not d["fortified"]:
            out["win_prob_if_fortified"] = self.estimate(attacker, d["units"], ptype, True, hasty)
        return out

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        s = self.m.state
        res = cancel_order(s, self.m.rs, self.seat, self._book(), order_id, s.phase)
        if not res.ok:
            raise ApiError(str(res.reason), str(res.message))
        self.m.books[self.seat] = res.book
        return {"orders_hash": orders_hash(res.book)}

    def send_message(self, to: list[str] | str, text: str) -> dict[str, Any]:
        self.m.messages.append((self.m.state.turn, self.m.state.phase, self.seat, text))
        return {}

    def ready(self, confirm_hash: str | None) -> dict[str, Any]:
        return {}

    def intel(self) -> dict[str, Any]:
        """The seat's reports of the last recon step as data: what `GET /v1/intel?format=json` gives."""
        return {"turn": (self.m.recon or {}).get("turn"), "intel": esp.intel_json(self.m.recon, self.seat)}

    def list_techs(self) -> dict[str, Any]:
        s, rs = self.m.state, self.m.rs
        res = rs.research_resource
        income = None
        if res is not None:
            key = stat_key(s, rs, self.seat)
            income = income_of(s, rs, self.seat, key)[res] - upkeep_of(s, rs, self.seat, key)[res]
        return techs_view(s, rs, self.seat, income)

    # diplomacy: the same checks and limits as the live game
    def _apply(self, kind: str | None, make: Any) -> dict[str, Any]:
        if kind is not None:
            limits = self.m.rs.scenario.diplomacy_limits
            if self.m.counts[(self.seat, kind)] >= limits[f"{kind}_per_phase"]:
                raise ApiError("LIMIT_EXCEEDED", kind)
        try:
            op = make()
        except dip.DiplomacyError as exc:
            raise ApiError(exc.code, exc.message) from None
        dip.apply_op(self.m.state, op)
        self.m.ops.append(op)
        if kind is not None:
            self.m.counts[(self.seat, kind)] += 1
        return {"op": op}

    def send_gift(self, to: str, resources: dict[str, int]) -> dict[str, Any]:
        s, rs = self.m.state, self.m.rs
        reserved = seat_reserved(s, rs, self.seat, self._book())
        out = self._apply("gifts", lambda: dip.make_gift(s, rs, self.seat, to, resources, s.phase, reserved))
        op = out["op"]
        self.m.gifts.append((s.turn, s.phase, self.seat, op["recipient"], dict(op["resources"])))
        return out

    # core 0.8.0 (docs/TRADE.md 9, 12.2): trade, with the checks and limits of the live game
    def _seat_ref(self, ref: Any) -> Any:
        nicks = {p.nick: seat for seat, p in self.m.state.players.items()}
        return nicks.get(ref, ref) if isinstance(ref, str) else ref

    def _trade_ctx(self) -> tr.Ctx:
        m = self.m
        return tr.Ctx(reserved=lambda x: seat_reserved(m.state, m.rs, x, m.books.get(x, SeatOrders())),
                      orders=lambda x: list(m.books.get(x, SeatOrders()).orders),
                      report=lambda x, rid: own_report(m, x, rid), turn_limit=m.rs.scenario.turn_limit)

    def propose_trade(self, to: str, give: dict[str, Any] | None = None, want: dict[str, Any] | None = None,
                      **extra: Any) -> dict[str, Any]:
        s, rs = self.m.state, self.m.rs
        limit = rs.scenario.diplomacy_limits.get("trade_proposals_per_phase", 3)
        try:
            made = tr.make_offer(s, rs, self.seat, self._seat_ref(to), give, want, extra.get("later"),
                                 extra.get("reply_to"), extra.get("replaces"), extra.get("fulfils"), s.phase,
                                 self._trade_ctx())
        except dip.DiplomacyError as exc:
            raise ApiError(exc.code, exc.message) from None
        if "already" in made:
            return {"id": made["already"]["id"], "status": made["already"]["status"], "already": True}
        if self.m.counts[(self.seat, "trade_proposals")] >= limit:
            raise ApiError("LIMIT_EXCEEDED", "trade_proposals")
        trade = dip.apply_op(s, made)
        self.m.ops.append(made)
        self.m.counts[(self.seat, "trade_proposals")] += 1
        self.m.trade_log.append({"op": made, "turn": s.turn, "phase": s.phase, "trade": copy.deepcopy(trade)})
        return {"id": made["id"], "status": trade["status"]}

    def respond_trade(self, trade_id: str, decision: str) -> dict[str, Any]:
        s, rs = self.m.state, self.m.rs
        try:
            op = tr.make_answer(s, rs, self.seat, trade_id, decision, s.phase, self._trade_ctx())
        except dip.DiplomacyError as exc:
            raise ApiError(exc.code, exc.message) from None
        trade = dip.apply_op(s, op)
        self.m.ops.append(op)
        self.m.trade_log.append({"op": op, "turn": s.turn, "phase": s.phase, "trade": copy.deepcopy(trade)})
        return {"id": trade["id"], "status": trade["status"]}

    def list_trades(self) -> dict[str, Any]:
        s = self.m.state
        return {"trades": [tr.trade_json(t, self.seat, lambda x: s.players[x].nick) for t in tr.mine(s, self.seat)]}

    def propose_pact(self, to: str, turns: int | None) -> dict[str, Any]:
        s = self.m.state
        return self._apply("pact_proposals", lambda: dip.make_proposal(s, self.seat, to, turns, s.phase))

    def respond_pact(self, pact_id: str, decision: str) -> dict[str, Any]:
        s = self.m.state
        return self._apply(None, lambda: dip.make_response(s, self.seat, pact_id, decision, s.phase))

    def leave_pact(self, pact_id: str) -> dict[str, Any]:
        s = self.m.state
        return self._apply(None, lambda: dip.make_leave(s, self.seat, pact_id, s.phase))


# ---------------------------------------------------------------- one game

def own_report(m: Match, seat: str, rid: str) -> dict[str, Any] | None:
    """One of the seat's own reports of the recon steps played so far (`Game.find_report` without copies)."""
    if not rid.startswith("r"):
        return None
    for rep in reversed(m.recons):
        for r in (rep.get("reports") or {}).get(seat, []):
            if r.get("id") == rid:
                return r
    return None


def reply_passes(m: Match, bots: dict[str, Bot], apis: dict[str, EngineApi]) -> None:
    """Core 0.8.0 (docs/TRADE.md 12.2): after a negotiation phase's pass, up to REPLY_PASSES passes in which the seats
    with open offers made to them only answer (the `[DAH]` wake of the live game), so an offer of a later seat to an
    earlier one is answered in the same phase."""
    for _ in range(REPLY_PASSES):
        waiting = [seat for seat in sorted(bots) if m.state.players[seat].alive and tr.waiting_for(m.state, seat)]
        if not waiting:
            return
        for seat in waiting:
            me = {seat, m.state.players[seat].nick}
            bots[seat].answer_trades(parse_state(apis[seat].state(), me), apis[seat])


def setup_view(rs: Ruleset, setup: dict[str, Any], offer: list[str]) -> dict[str, Any]:
    """What `GET /v1/setup` gives a seat while the phase is open (the parts the bots read)."""
    return {"enabled": True, "open": True, "artifacts_mode": setup.get("artifacts"),
            "artifact_points": setup.get("artifact_points"), "artifact_offer": setup.get("artifact_offer"),
            "unique_factions": bool(setup.get("unique_factions", True)), "factions": faction_choices(rs),
            "offer": artifact_choices(rs, offer)}


def play_setup(rs: Ruleset, setup: dict[str, Any], bots: dict[str, Bot], seed: str,
               assigned: dict[str, str] | None) -> dict[str, dict[str, Any]]:
    """The setup phase in-process: each bot chooses (or gets its assigned faction), choices are checked as the
    server checks them, then settled by the engine. Returns resolve_setup's result."""
    seats = sorted(bots)
    offers = draft_offers(rs, setup, seats, seed)
    pool = artifact_pool(rs, setup) if setup.get("artifacts") == "points" else []
    choices: dict[str, dict[str, Any]] = {}
    for seat in seats:
        bot = bots[seat]
        offer = list(offers.get(seat) or pool)
        view = setup_view(rs, setup, offer)
        prefs, arts = bot.setup_choices(view)
        if assigned is not None:
            prefs = [assigned[seat]]
            arts = pick_artifacts(view, assigned[seat], bot.artifact_likes) if bot.faction_style else None
        choice: dict[str, Any] = {}
        try:
            if prefs:
                choice["factions"] = check_faction_prefs(rs, prefs)
            if arts is not None:
                choice["artifacts"] = check_artifacts(rs, setup, offer, arts)
        except SetupChoiceError:
            pass
        choices[seat] = choice
    return resolve_setup(rs, setup, seed, [{"seat": s} for s in seats], choices, offers)


class ScoutControlBot(ScoutBot):
    """Stage 5 balance (decision 25 a): the scout bot's control for `--scout-sweep` - it recruits the sweep's Militia
    like the scout but never sends single units into Abandoned land (the builder's armies only). The scout minus this
    control is what the sweep itself is worth; the scout minus the builder also counts the extra Militia."""
    name = "scout_control"

    def pre_moves(self, view: StateView, keep: Reserved) -> list[dict[str, Any]]:
        return []

    def army_ok(self) -> ArmyOk:
        return BuilderBot.army_ok(self)


SIM_BOTS: dict[str, type[Bot]] = {**BOT_TYPES, ScoutControlBot.name: ScoutControlBot}


def play_game(rs: Ruleset, kinds: dict[str, str], seed: str, setup: dict[str, Any] | None = None,
              assigned: dict[str, str] | None = None) -> dict[str, Any]:
    """Play one game; returns the raw facts the tables are built from. `setup`: the game's setup config (a setup
    phase is played); `assigned`: seat -> faction (dealt, not chosen)."""
    bots: dict[str, Bot] = {seat: SIM_BOTS[kind](seat, seat_seed(seed, seat))
                            for seat, kind in kinds.items()}
    seats: list[dict[str, Any]] = [{"seat": seat, "nick": f"{kind}-{seat}"} for seat, kind in sorted(kinds.items())]
    if setup is not None:
        result = play_setup(rs, setup, bots, seed, assigned)
        for spec in seats:
            spec.update(faction=result[spec["seat"]]["faction"], artifacts=result[spec["seat"]]["artifacts"])
    elif assigned is not None:
        for spec in seats:
            spec["faction"] = assigned[spec["seat"]]
    m = Match(rs, seats, seed)
    apis = {seat: EngineApi(m, seat) for seat in kinds}
    # stage 5 (ABANDONED_LANDS.md 14.2): the layout (the simulator's path has no salt), start positions, Abandoned land
    layout = disc.journal_layout(rs, m.state, seed, None) if disc.active(rs, m.state) else None
    starts = [st.capital for st in rs.map.starts]
    position = {m.state.provinces[c].owner: i for i, c in enumerate(starts) if m.state.provinces[c].owner}
    free_starts = [i for i, c in enumerate(starts) if m.state.provinces[c].owner is None]
    abandoned = list(m.state.abandoned)
    events: list[dict[str, Any]] = []
    recons: list[dict[str, Any]] = []
    feints_real: list[tuple[int, str, str]] = []   # (turn, seat, order id) of false entries made real in P2
    occupied_turns = 0
    scores_by_turn: dict[int, dict[str, int]] = {}
    parts_by_turn: dict[int, dict[str, dict[str, int]]] = {}
    res_by_turn: dict[int, dict[str, dict[str, int]]] = {}
    central_by_turn: dict[int, dict[str, int]] = {}
    central = central_provinces(rs)
    espionage = rs.system("espionage")
    trade = tr.on(rs)
    transfers: list[dict[str, Any]] = []        # core 0.8.0: trade events of the resolutions
    while m.state.phase != "game_over":
        m.books, m.ops = {}, []
        for phase in PHASES:
            if phase == "negotiation_2" and espionage:   # the recon step at the close of actions
                m.state.phase = "actions"
                m.state, m.recon = esp.recon(m.state, m.books, rs, seed)
                recons.append(m.recon)
                m.recons.append(m.recon)
            m.state.phase = phase
            m.counts.clear()
            for seat in sorted(kinds):
                if m.state.players[seat].alive:
                    bots[seat].play_phase(apis[seat], phase, {seat, m.state.players[seat].nick})
            if trade and phase != "actions":
                for seat in sorted(kinds):      # what get_state shows before the answers (gate G7)
                    if m.state.players[seat].alive:
                        line = tr.state_line(m.state, rs, seat, lambda x: m.state.players[x].nick)
                        m.state_line_max = max(m.state_line_max, len(line))
                reply_passes(m, bots, apis)
        turn = m.state.turn
        m.state.phase = "resolution"
        alive = [s for s, p in m.state.players.items() if p.alive]
        keys = {s: stat_key(m.state, rs, s) for s in alive}  # the stats each seat fights with this turn
        books = {s: m.books.get(s, SeatOrders()) for s in alive}
        if espionage and m.recon is not None:
            feint_ids = {(f["seat"], f["order_id"]) for f in m.recon.get("feints", [])}
            feints_real += [(turn, s, o.id) for s, b in books.items() for o in b.orders
                            if o.kind == "move" and not o.feint and (s, o.id) in feint_ids]
        p2_moves = [(s, o.dst) for s, b in books.items() for o in b.orders
                    if o.kind == "move" and not o.feint and o.phase_added == "negotiation_2"]
        hidden = {pid for pid, e in m.state.discoveries.items() if e.get("state") == "hidden"}
        ordered: dict[tuple[str, str], list[int]] = {}      # (seat, province) -> [units, price] sent into a find
        for s, b in books.items():
            for o in b.orders:
                if o.kind == "move" and not o.feint and o.dst in hidden:
                    acc = ordered.setdefault((s, o.dst), [0, 0])
                    acc[0] += sum((o.units or {}).values())
                    acc[1] += sum(unit_price(rs, u) * n for u, n in (o.units or {}).items())
        m.state, report = resolve(m.state, books, rs, seed, m.ops)
        occupied_turns += sum(1 for p in m.state.provinces.values() if (p.status or {}).get("occupied"))
        events.append({"kind": "_p2_moves", "moves": p2_moves, "turn": turn, "seats": []})
        for e in report.events:
            if e["kind"] == "battle":
                e = {**e, "keys": [keys.get(side["owner"]) for side in e["sides"]]}
            elif e["kind"] == "discovery_found":
                e = {**e, "army": {x: ordered.get((x, e["province"])) for x in e.get("entrants") or []}}
            events.append({**e, "turn": turn})
        scores_by_turn[turn] = report.scores_of()      # the turn score is private since stage 4
        parts_by_turn[turn] = report.score_parts or {s: {"territory": v} for s, v in scores_by_turn[turn].items()}
        res_by_turn[turn] = {s: {short(r): n for r, n in p.resources.items()} for s, p in m.state.players.items()}
        central_by_turn[turn] = {s: sum(1 for pid in central if m.state.provinces[pid].owner == s)
                                 for s in m.state.players}
    result = m.state.result or {}
    held = {s: {(pid, b) for pid, b, _ in sc_.works_held(m.state, rs, s)} for s in m.state.players} \
        if sc_.on(rs, "works") else {}
    completed = [(e["seat"], e["province"], e["building"]) for e in events if e["kind"] == "work_completed"]
    return {"kinds": kinds, "winners": result.get("winners", []), "scores": result.get("scores", {}),
            "events": events, "scores_by_turn": scores_by_turn, "turns": max(scores_by_turn),
            "resources": {s: dict(p.resources) for s, p in m.state.players.items()},
            "alive": {s: p.alive for s, p in m.state.players.items()},
            "factions": {s: p.faction for s, p in m.state.players.items()},
            "artifacts": {s: list(p.artifacts) for s, p in m.state.players.items()},
            "techs": {s: list(p.techs) for s, p in m.state.players.items()},
            "recons": recons, "feints_real": feints_real, "occupied_turns": occupied_turns,
            "shades_end": {s: len(p.shades) for s, p in m.state.players.items()},
            # stage 4 (SCORING.md 10): the true score by source, objectives, flip, stocks and central provinces
            "reason": result.get("reason"), "parts_by_turn": parts_by_turn, "res_by_turn": res_by_turn,
            "central_by_turn": central_by_turn,
            "score_parts": result.get("score_parts") or {s: {"territory": v} for s, v in
                                                          result.get("scores", {}).items()},
            "objectives": result.get("objectives") or {}, "winners_open": result.get("winners_open"),
            "objective_kinds": {oid: (o.kind, dict(o.params)) for oid, o in rs.objectives.items()},
            "works_lost": sum(1 for seat, pid, b in completed if (pid, b) not in held.get(seat, set())),
            "scored": not rs.default_scoring,
            # stage 5: the layout, start positions (index in map.starts) and the free starts, the Abandoned land
            # the seats hold at the end (points: 1, a capital's ruins +3 unless in Ashes)
            "layout": layout, "position": position, "free_starts": free_starts, "n_starts": len(starts),
            "abandoned": abandoned, "abandoned_points": _abandoned_points(rs, m.state, abandoned),
            "placed": len((layout or {}).get("finds") or {}),
            # core 0.7.0 (decision 27): the total war cap each seat plays under (0 = none)
            "war_total_cap": {s: int(rs.stat("score.war.province_total", stat_key(m.state, rs, s)))
                              for s in m.state.players},
            # core 0.8.0 (docs/SPY_V2.md 10): the intel cap, the great works, the bots' P2 decisions after a report
            "intel_cap": int(rs.stat("score.intel.cap")) if sc_.on(rs, "intel") else None,
            "great_works": sorted(b for b, bd in rs.buildings.items() if bd.score),
            "decisions": {s: dict(b.memory.decisions) for s, b in bots.items() if b.memory.decisions},
            # core 0.8.0 (docs/TRADE.md 12.2): the trade ops, gifts, the bots' promises, the ОБМЕН line, the end map
            "trade": trade, "trade_log": m.trade_log, "gifts": m.gifts, "state_line_max": m.state_line_max,
            "promises": {s: dict(b.trade_mem.promises) for s, b in bots.items()},
            "final_owner": {pid: p.owner for pid, p in m.state.provinces.items()},
            "ptype": {pid: short(p.type) for pid, p in m.state.provinces.items()}}


def _abandoned_points(rs: Ruleset, state: GameState, abandoned: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pid in abandoned:
        p = state.provinces[pid]
        if p.owner is None or int((p.status or {}).get("ruined_until", 0)) > state.turn:
            continue
        out[p.owner] = out.get(p.owner, 0) + 1 + (3 if pid in rs.map.capitals else 0)
    return out


def central_provinces(rs: Ruleset) -> set[str]:
    """Provinces outside every start of the map ("central": a notion of the simulator only, SCORING.md 10)."""
    starts = {pid for st in rs.map.starts for pid in (st.capital, *st.provinces)}
    return set(rs.map.provinces) - starts


# ---------------------------------------------------------------- statistics

def nominal_ratio(rs: Ruleset, attacker: dict[str, int], defender: dict[str, int], keys: Any = (None, None)) -> float:
    """Sum of attack / sum of defence, with each side's own unit stats (faction, techs, artifacts) but without the
    province, fortification, ability and random multipliers."""
    att = sum(rs.stat(f"unit.{u}.attack", keys[0]) * n for u, n in attacker.items())
    dfn = sum(rs.stat(f"unit.{u}.defense", keys[1]) * n for u, n in defender.items())
    return att / dfn if dfn else 99.0


class Tally:
    def __init__(self) -> None:
        self.games = 0
        self.win_share: dict[str, float] = defaultdict(float)
        self.seats: Counter[str] = Counter()
        self.scores: dict[str, list[int]] = defaultdict(list)
        self.idle: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.starvation: Counter[str] = Counter()
        self.eliminated: Counter[str] = Counter()
        self.battles = 0
        self.edge_battles = 0
        self.attacks: dict[str, list[int]] = defaultdict(lambda: [0, 0])   # bucket -> [attacker wins, fights]
        self.attacks_fort: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.first_capture: list[int] = []      # first player-to-player capture turn (games that had one)
        self.first_neutral: list[int] = []
        self.no_capture = 0
        self.leader_t3_wins = 0
        self.leader_t3_games = 0
        self.lengths: list[int] = []
        self.diplo: Counter[str] = Counter()
        self.built: dict[str, Counter[str]] = defaultdict(Counter)    # bot type -> building -> count
        self.recruited: Counter[str] = Counter()
        # stage 2: per faction and per artifact; techs researched per seat (by bot type), most researched techs
        self.f_seats: Counter[str] = Counter()
        self.f_wins: dict[str, float] = defaultdict(float)
        self.f_scores: dict[str, list[int]] = defaultdict(list)
        self.f_techs: dict[str, list[int]] = defaultdict(list)
        self.a_seats: Counter[str] = Counter()
        self.a_wins: dict[str, float] = defaultdict(float)
        self.techs: dict[str, list[int]] = defaultdict(list)
        self.tech_done: Counter[str] = Counter()
        self.has_techs = False
        self.tier2_games = 0                    # games where some seat completed a tech of tier 2 or more
        self.tier2_seats = 0
        self.seat_rows: list[tuple[str, str, list[str], float]] = []  # (faction, bot type, artifacts, win share)
        self.s3 = Stage3Tally()
        self.sv2 = SpyTally()      # core 0.8.0 (docs/SPY_V2.md 10)
        self.trd = TradeTally()    # core 0.8.0 (docs/TRADE.md 12.2)
        self.s4 = ScoreTally()
        self.s5 = DiscoveryTally()

    def add(self, rs: Ruleset, g: dict[str, Any]) -> None:
        self.games += 1
        kinds = g["kinds"]
        for seat, kind in kinds.items():
            self.seats[kind] += 1
            self.scores[kind].append(g["scores"].get(seat, 0))
            for res, n in g["resources"][seat].items():
                self.idle[kind][res.split(":")[-1]].append(n)
            if not g["alive"][seat]:
                self.eliminated[kind] += 1
        for seat in g["winners"]:
            self.win_share[kinds[seat]] += 1 / len(g["winners"])
        share = {seat: 1 / len(g["winners"]) for seat in g["winners"]}
        for seat in kinds:
            faction = short(g["factions"][seat])
            techs = g["techs"][seat]
            self.f_seats[faction] += 1
            self.f_wins[faction] += share.get(seat, 0.0)
            self.f_scores[faction].append(g["scores"].get(seat, 0))
            self.f_techs[faction].append(len(techs))
            self.techs[kinds[seat]].append(len(techs))
            self.tech_done.update(short(t) for t in techs)
            for art in g["artifacts"][seat]:
                self.a_seats[short(art)] += 1
                self.a_wins[short(art)] += share.get(seat, 0.0)
            self.seat_rows.append((faction, kinds[seat], [short(a) for a in g["artifacts"][seat]],
                                   share.get(seat, 0.0)))
        self.has_techs = self.has_techs or bool(rs.techs)
        tier2 = [s for s in kinds if any(rs.techs[t].tier >= 2 for t in g["techs"][s] if t in rs.techs)]
        self.tier2_seats += len(tier2)
        self.tier2_games += bool(tier2)
        self.lengths.append(g["turns"])
        first_cap = first_neu = None
        for e in g["events"]:
            kind = e["kind"]
            if kind == "build_started":
                for seat in e["seats"]:
                    self.built[kinds[seat]][e["building"].split(":")[-1]] += 1
            elif kind == "recruited":
                for seat in e["seats"]:
                    self.recruited[kinds[seat]] += e["count"]
            elif kind == "starvation":
                for seat in e["seats"]:
                    self.starvation[kinds[seat]] += 1
            elif kind == "edge_battle":
                self.edge_battles += 1
            elif kind == "battle":
                self.battles += 1
                if e.get("stage") == "defense":
                    att, dfn = e["sides"]
                    ratio = nominal_ratio(rs, att["units"], dfn["units"], e.get("keys") or (None, None))
                    bucket = next(name for lo, hi, name in RATIO_BUCKETS if lo <= ratio < hi)
                    table = self.attacks_fort if e.get("fortified") else self.attacks
                    table[bucket][0] += att["won"]
                    table[bucket][1] += 1
            elif kind == "captured":
                if e["from"] is None and first_neu is None:
                    first_neu = e["turn"]
                if e["from"] is not None and first_cap is None:
                    first_cap = e["turn"]
            elif kind in ("gift", "pact_proposed", "pact_signed", "pact_renewed", "pact_broken", "pact_bounce",
                          "pact_joint"):
                self.diplo[kind] += 1
        self.s3.add(g)
        self.sv2.add(g)
        self.trd.add(g)
        self.s4.add(g)
        self.s5.add(rs, g)
        if first_cap is None:
            self.no_capture += 1
        else:
            self.first_capture.append(first_cap)
        if first_neu is not None:
            self.first_neutral.append(first_neu)
        t3 = g["scores_by_turn"].get(3)
        if t3:
            best = max(t3.values())
            leaders = [s for s, v in t3.items() if v == best]
            if len(leaders) == 1:
                self.leader_t3_games += 1
                self.leader_t3_wins += leaders[0] in g["winners"]


class Stage3Tally:
    """Espionage and conquest figures (docs/SPY_AND_CONQUEST.md section 7)."""

    def __init__(self) -> None:
        self.games = 0
        self.esp_games = 0
        self.con_games = 0
        self.seats: Counter[str] = Counter()                  # bot type -> seats (espionage games)
        self.hired: Counter[str] = Counter()                  # bot type -> Shades hired
        self.shade_alloy: Counter[str] = Counter()            # bot type -> alloy on Shades
        self.replace_alloy: Counter[str] = Counter()          # ... on Shades hired after a loss
        self.missions: dict[str, Counter[str]] = defaultdict(Counter)   # mission -> end reason -> count
        self.started: Counter[str] = Counter()                # mission -> started
        self.rounds: Counter[str] = Counter()                 # round outcome -> count
        self.strikes = 0
        self.window_attacks = [0, 0]                          # [won, fought]: P2 attacks into a struck province
        self.feints = [0, 0, 0]                               # placed, exposed (in reports), made real in P2
        self.captures: Counter[str] = Counter()               # attributed | unattributed | neutral
        self.dispositions: Counter[tuple[str, str]] = Counter()  # (disposition, source) -> count
        self.subjugate_food: list[int] = []                   # provisions paid per subjugation
        self.food_end: list[int] = []                         # provisions idle at the end, per seat
        self.occupations = 0
        self.occupied_turns = 0
        self.revolts = 0
        self.returns = 0
        self.claims = 0
        self.loot: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # province -> [times looted, total loot]

    def add(self, g: dict[str, Any]) -> None:
        self.games += 1
        kinds = g["kinds"]
        recons = g.get("recons") or []
        if recons:
            self.esp_games += 1
            for kind in kinds.values():
                self.seats[kind] += 1
        lost_before: set[str] = set()
        for rep in recons:
            for h in rep.get("hired", []):
                alloy = sum(n for r, n in (h.get("cost") or {}).items() if r.endswith("alloy"))
                self.hired[kinds[h["seat"]]] += 1
                self.shade_alloy[kinds[h["seat"]]] += alloy
                if h["seat"] in lost_before:
                    self.replace_alloy[kinds[h["seat"]]] += alloy
            for st in rep.get("started", []):
                self.started[short(st["mission"])] += 1
            for e in rep.get("ended", []):
                self.missions[short(e["mission"])][e["reason"]] += 1
            for r in rep.get("rounds", []):
                self.rounds[r["outcome"]] += 1
            self.strikes += len(rep.get("strikes", []))
            self.feints[0] += len(rep.get("feints", []))
            for reports in (rep.get("reports") or {}).values():
                for r in reports:
                    self.feints[1] += sum(1 for f in r.get("feints", []) if f.get("exposed"))
            for c in rep.get("caught", []):
                self.captures["neutral" if c.get("victim") is None else
                              "attributed" if c.get("attributed") else "unattributed"] += 1
            for seat in rep.get("lost", {}):
                lost_before.add(seat)
        self.feints[2] += len(g.get("feints_real") or [])
        con = any(e["kind"] in ("disposition", "occupation_revolt", "claim_created") or
                  (e["kind"] == "captured" and "mode" in e) for e in g["events"])
        self.con_games += con
        if con or recons or g["events"]:
            for seat in kinds:
                self.food_end.append(next((n for r, n in g["resources"][seat].items() if r.endswith("provisions")),
                                          0))
        self.occupied_turns += g.get("occupied_turns", 0)
        struck: dict[tuple[int, str], str] = {}
        p2: dict[int, list[tuple[str, str]]] = {}
        for e in g["events"]:
            kind = e["kind"]
            if kind == "_p2_moves":
                p2[e["turn"]] = [tuple(x) for x in e["moves"]]
            elif kind == "sabotage":
                struck[(e["turn"], e["province"])] = e.get("seat")
            elif kind == "battle" and e.get("stage") == "defense":
                by = struck.get((e["turn"], e["province"]))
                att = e["sides"][0]
                if by is not None and att.get("owner") == by and (by, e["province"]) in p2.get(e["turn"], []):
                    self.window_attacks[0] += bool(att.get("won"))
                    self.window_attacks[1] += 1
            elif kind == "captured" and e.get("mode"):
                if e["mode"] == "occupied" or e.get("status") == "occupied":
                    self.occupations += 1
                if e["mode"] == "reclaimed":
                    self.returns += 1
            elif kind == "disposition":
                self.dispositions[(short(e.get("disposition", "?")), e.get("source", "?"))] += 1
                if short(e.get("disposition", "")) == "subjugate":
                    self.subjugate_food.append(sum((e.get("cost") or {}).values()))
                loot = sum((e.get("loot") or {}).values())
                if loot:
                    acc = self.loot[e["province"]]
                    acc[0] += 1
                    acc[1] += loot
            elif kind == "occupation_revolt":
                self.revolts += 1
            elif kind == "claim_created":
                self.claims += 1

    def summary(self) -> dict[str, Any]:
        ended = sum(sum(c.values()) for c in self.missions.values())
        caught = sum(c["caught"] for c in self.missions.values())
        sabotages = self.started.get("sabotage", 0)
        loot_counts = sorted((v[0] for v in self.loot.values()), reverse=True)
        return {
            "espionage_games": self.esp_games, "conquest_games": self.con_games,
            "shades_per_seat": {k: self.hired[k] / n for k, n in self.seats.items()},
            "shade_alloy_per_seat": {k: self.shade_alloy[k] / n for k, n in self.seats.items()},
            "replacement_alloy_per_seat": {k: self.replace_alloy[k] / n for k, n in self.seats.items()},
            "missions": {m: {"started": self.started[m], **dict(c)} for m, c in sorted(self.missions.items())},
            "rounds": dict(self.rounds), "shade_survival": 1 - caught / ended if ended else None,
            "sabotage_strike_rate": self.strikes / sabotages if sabotages else None,
            "window_attacks": {"fought": self.window_attacks[1], "won": self.window_attacks[0]},
            "feints": {"placed": self.feints[0], "exposed": self.feints[1], "made_real": self.feints[2]},
            "captures": dict(self.captures),
            "dispositions": {f"{d}/{src}": n for (d, src), n in sorted(self.dispositions.items())},
            "subjugate_food_mean": statistics.mean(self.subjugate_food) if self.subjugate_food else None,
            "subjugate_food_total": sum(self.subjugate_food),
            "idle_food_end_mean": statistics.mean(self.food_end) if self.food_end else None,
            "occupations": self.occupations, "occupied_turns": self.occupied_turns,
            "occupation_mean_turns": self.occupied_turns / self.occupations if self.occupations else None,
            "revolts": self.revolts, "returns": self.returns, "claims": self.claims,
            "loot_per_province": {pid: {"times": v[0], "loot": v[1]} for pid, v in sorted(self.loot.items())},
            "loot_max_times_one_province": loot_counts[0] if loot_counts else 0,
        }

    def report(self) -> list[str]:
        if not self.esp_games and not self.con_games:
            return []
        out: list[str] = []
        sm = self.summary()
        g = max(1, self.games)
        if self.esp_games:
            per = ", ".join(f"{k} {v:.2f}" for k, v in sorted(sm["shades_per_seat"].items()))
            spend = ", ".join(f"{k} {v:.1f} ({sm['replacement_alloy_per_seat'][k]:.1f} replacing lost)"
                              for k, v in sorted(sm["shade_alloy_per_seat"].items()))
            out.append(f"  espionage: Shades hired/seat {per or '-'}; alloy on Shades/seat {spend or '-'}")
            rows = []
            for mid, c in sm["missions"].items():
                ok = c.get("done", 0) + c.get("struck", 0)
                rows.append(f"{mid} {c['started']} (success {ok}, caught {c.get('caught', 0)}, empty "
                            f"{c.get('empty', 0)}, void {c.get('void', 0)}, recalled {c.get('recalled', 0)})")
            out.append("  missions: " + ("; ".join(rows) or "-"))
            surv = sm["shade_survival"]
            rate = sm["sabotage_strike_rate"]
            wa = sm["window_attacks"]
            out.append(f"  Shade survival per mission {'-' if surv is None else f'{100 * surv:.0f}%'}; sabotage "
                       f"strikes {self.strikes / g:.2f}/game ({'-' if rate is None else f'{100 * rate:.0f}%'} of "
                       f"sabotage missions); P2 attacks after a strike {wa['fought']} (won {wa['won']})")
            f = sm["feints"]
            cap = sm["captures"]
            out.append(f"  false entries {f['placed'] / g:.2f}/game (exposed {f['exposed']}, made real "
                       f"{f['made_real']}); captures {sum(cap.values()) / g:.2f}/game (attributed "
                       f"{cap.get('attributed', 0)}, unattributed {cap.get('unattributed', 0)}, neutral "
                       f"{cap.get('neutral', 0)})")
        if self.con_games or self.dispositions:
            fates = ", ".join(f"{k} {n / g:.2f}" for k, n in sm["dispositions"].items()) or "-"
            out.append(f"  conquest: fates/game {fates}")
            mean_food = sm["subjugate_food_mean"]
            occ = sm["occupation_mean_turns"]
            idle = sm["idle_food_end_mean"]
            out.append(f"  provisions on subjugation {sm['subjugate_food_total'] / g:.1f}/game (mean "
                       f"{'-' if mean_food is None else f'{mean_food:.1f}'}), idle at the end "
                       f"{'-' if idle is None else f'{idle:.1f}'}/seat; occupations {self.occupations / g:.2f}/game, "
                       f"mean {'-' if occ is None else f'{occ:.1f}'} turns; revolts {self.revolts / g:.2f}, returns "
                       f"{self.returns / g:.2f}, claims {self.claims / g:.2f} per game")
            top = sorted(self.loot.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))[:5]
            out.append("  loot per province (times, total): " + (
                ", ".join(f"{pid} {v[0]}x {v[1]}" for pid, v in top) or "-"))
        return out


DEEP_MISSIONS = ("secret", "read_orders", "sabotage")    # docs/SPY_V2.md 10: the «deep» missions
EARLY_TURNS = (1, 2, 3)         # G5: the pace of the first turns ...
LATE_TURNS = (10, 11, 12)       # ... against the last ones (a shorter game: its last three turns)


class SpyTally:
    """Core 0.8.0 (docs/SPY_V2.md 10): the bot checks and the balance gates of espionage v2."""

    def __init__(self) -> None:
        self.games = 0
        self.seats = 0
        self.started: Counter[str] = Counter()          # mission -> started
        self.after_cap = 0                              # missions started by a seat already at the intel cap
        self.cap_seats = 0                              # seats that reached the cap
        self.early = 0                                  # missions started on EARLY_TURNS
        self.late = 0                                   # ... on the last three turns
        self.late_turns = 0                             # turns counted as late (a shorter game has fewer)
        self.early_turns = 0
        self.ended: Counter[str] = Counter()            # end reason -> count (all missions)
        self.observe_ended: Counter[str] = Counter()    # end reason -> count (observe)
        self.decisions: Counter[str] = Counter()        # rush: cancel | hasty after a report
        self.stopped_works = 0                          # great works a sabotage stopped (each stop)
        self.stopped_unfinished = 0                     # ... of them not completed by the end
        self.stopped_builds = 0                         # any build a sabotage stopped
        self.revolts = 0                                # occupation revolts by sabotage
        self.winner_intel: list[float] = []             # intel share of each winner's final score
        self.read_points: Counter[tuple[int, str, str]] = Counter()   # (game, seat, target) -> read_orders points
        self.wards = 0
        self.points_by_kind: Counter[str] = Counter()

    def add(self, g: dict[str, Any]) -> None:
        recons = g.get("recons") or []
        if not recons:
            return
        self.games += 1
        game = self.games
        self.seats += len(g["kinds"])
        cap = g.get("intel_cap")
        parts = g.get("parts_by_turn") or {}
        last = g.get("turns") or 0
        late = set(range(max(1, last - 2), last + 1)) if last < LATE_TURNS[-1] else set(LATE_TURNS)
        self.late_turns += len(late)
        self.early_turns += len([t for t in EARLY_TURNS if t <= last])
        capped: set[str] = set()
        for rep_ in recons:
            turn = int(rep_.get("turn") or 0)
            for st in rep_.get("started", []):
                mid = short(st["mission"])
                self.started[mid] += 1
                if turn in EARLY_TURNS:
                    self.early += 1
                if turn in late:
                    self.late += 1
                done = (parts.get(turn - 1) or {}).get(st["seat"], {}).get("intel", 0)
                if cap and done >= cap:
                    self.after_cap += 1
                    capped.add(st["seat"])
            for e in rep_.get("ended", []):
                self.ended[e["reason"]] += 1
                if short(e["mission"]) == "observe":
                    self.observe_ended[e["reason"]] += 1
            for pt in rep_.get("intel_points") or []:
                self.points_by_kind[pt.get("kind", "each_turn")] += 1
                if short(pt["mission"]) == "read_orders" and pt.get("kind") == "once_per_target":
                    self.read_points[(game, pt["seat"], pt["target"])] += int(pt["points"])
            self.wards += len(rep_.get("wards") or [])
        self.cap_seats += len(capped)
        for seat, d in (g.get("decisions") or {}).items():
            self.decisions.update(d)
        works = set(g.get("great_works") or ())
        done_works = {(e["seat"], e["province"], e["building"]) for e in g["events"] if e["kind"] == "work_completed"}
        stopped: set[tuple[str, str, str]] = set()
        for e in g["events"]:
            if e["kind"] == "build_sabotaged":
                self.stopped_builds += 1
                if e.get("building") in works:
                    self.stopped_works += 1
                    stopped.add((e["seat"], e["province"], e["building"]))
            elif e["kind"] == "occupation_revolt" and e.get("reason") == "sabotage":
                self.revolts += 1
            elif e["kind"] == "intel_point":
                self.points_by_kind[e.get("intel_kind", "effect")] += 1
        self.stopped_unfinished += len(stopped - done_works)
        for w in g.get("winners") or []:
            sp = (g.get("score_parts") or {}).get(w) or {}
            total = sum(v for v in sp.values() if v > 0)
            if total > 0:
                self.winner_intel.append(sp.get("intel", 0) / total)

    def summary(self) -> dict[str, Any]:
        started = sum(self.started.values())
        ended = sum(self.ended.values())
        obs = sum(self.observe_ended.values())
        g = self.games or 1
        early = self.early / self.early_turns if self.early_turns else None
        late = self.late / self.late_turns if self.late_turns else None
        return {
            "games": self.games, "missions": dict(sorted(self.started.items())),
            "deep_share": sum(self.started[m] for m in DEEP_MISSIONS) / started if started else None,
            "after_cap_per_capped_seat": self.after_cap / self.cap_seats if self.cap_seats else None,
            "capped_seats": self.cap_seats,
            "pace_early": early, "pace_late": late,
            "late_vs_early": late / early if early else None,
            "rush_p2_after_report": dict(self.decisions),
            "shade_survival": 1 - self.ended["caught"] / ended if ended else None,
            "works_stopped_per_game": self.stopped_works / g, "builds_stopped_per_game": self.stopped_builds / g,
            "stopped_unfinished_share": self.stopped_unfinished / self.stopped_works if self.stopped_works else None,
            "sabotage_revolts_per_game": self.revolts / g,
            "winner_intel_share": statistics.mean(self.winner_intel) if self.winner_intel else None,
            "read_orders_points_max_per_target": max(self.read_points.values()) if self.read_points else None,
            "empty_observe_share": self.observe_ended["empty"] / obs if obs else None,
            "wards_per_game": self.wards / g, "intel_points_by_kind": dict(sorted(self.points_by_kind.items())),
        }

    def report(self) -> list[str]:
        if not self.games:
            return []
        sm = self.summary()

        def pct(x: float | None) -> str:
            return "-" if x is None else f"{100 * x:.0f}%"

        after = sm["after_cap_per_capped_seat"]
        return [f"  spy v2: deep missions {pct(sm['deep_share'])}; after the intel cap "
                f"{'-' if after is None else f'{after:.2f}'}/capped seat; pace turns 10-12 vs 1-3 {pct(sm['late_vs_early'])}; works stopped "
                f"{sm['works_stopped_per_game']:.2f}/game (unfinished {pct(sm['stopped_unfinished_share'])}); "
                f"sabotage revolts {sm['sabotage_revolts_per_game']:.2f}/game; wards {sm['wards_per_game']:.2f}/game; "
                f"empty observe {pct(sm['empty_observe_share'])}; rush P2 after a report {sm['rush_p2_after_report']}"]


def spy_gates(t: SpyTally, win: dict[str, float], base: dict[str, float] | None) -> list[dict[str, Any]]:
    """docs/SPY_V2.md 10: the bot checks (a miss is a bug in a bot) and the balance gates. `win`: win share per bot
    type of this run; `base`: the same of the previous core (`--spy-base`), or None."""
    rows: list[dict[str, Any]] = []
    sm = t.summary()

    def row(name: str, value: Any, corridor: str, ok: bool | None, gate: bool = True) -> None:
        status = "-" if ok is None else ("PASS" if ok else "FAIL") if gate else ("track" if ok else "miss")
        rows.append({"name": name, "value": round(value, 3) if isinstance(value, float) else value,
                     "corridor": corridor, "status": status, "gate": gate})

    deep = sm["deep_share"]
    row("bots: deep missions (secret / read_orders / sabotage)", deep, ">= 0.45", None if deep is None else deep >= 0.45)
    after = sm["after_cap_per_capped_seat"]
    row("bots: missions after the intel cap per capped seat", after, ">= 1.5", None if after is None else after >= 1.5)
    pace = sm["late_vs_early"]
    row("bots G5: mission pace turns 10-12 vs 1-3", pace, ">= 0.60", None if pace is None else pace >= 0.60)
    p2 = sum(sm["rush_p2_after_report"].values())
    row("bots: rush attacks cancelled or joined in P2 after a report", p2, "> 0", p2 > 0)
    surv = sm["shade_survival"]
    row("Shade survival per mission", surv, "0.65-0.80", None if surv is None else 0.65 <= surv <= 0.80)
    stop = sm["works_stopped_per_game"]
    row("great works stopped by sabotage per game", stop, "0.3-1.5", 0.3 <= stop <= 1.5)
    left = sm["stopped_unfinished_share"]
    row("stopped works left unfinished", left, "<= 0.25", None if left is None else left <= 0.25)
    rev = sm["sabotage_revolts_per_game"]
    row("revolts by sabotage per game", rev, "<= 1", rev <= 1)
    if base:
        shift = {k: round(win.get(k, 0.0) - base.get(k, 0.0), 3) for k in sorted(set(win) | set(base))}
        row("win share shift per bot type vs --spy-base", shift, "<= 0.05 each",
            all(abs(v) <= 0.05 for v in shift.values()))
    else:
        row("win share shift per bot type vs --spy-base", "no base", "<= 0.05 each", None)
    share = sm["winner_intel_share"]
    row("intel share of the winner's score", share, "<= 0.10", None if share is None else share <= 0.10)
    most = sm["read_orders_points_max_per_target"]
    row("read_orders points from one target per seat (engine, once_per_target)", most, "<= 1",
        None if most is None else most <= 1)
    empty = sm["empty_observe_share"]
    row("empty Observations", empty, "<= 0.35", None if empty is None else empty <= 0.35)
    return rows


IDLE_TURN = 8          # SCORING.md 10: idle stocks "к ходу 8" (the last turn of a shorter game)
NO_CENTRE_TURN = 6     # ... the seat without central provinces "на ходу 6"
FEED_FIGHTS = 3        # a war pair is flagged as feeding: >= this many credits one way and none back, or a pact
# the bot type whose style pursues an objective kind (excluded from the upper bound of the objective corridor)
PROFILE_KINDS = {"great_work": "builder", "hold_foreign": "rush", "capital_other": "rush",
                 "rival_capital_taken": "rush"}
PROFILE_BRANCHES = {"war": "rush", "industry": "builder", "logistics": "diplomat"}


IDLE_STOCK = 30          # core 0.8.0 (docs/TRADE.md 12.2): a resource at or above this at a turn's end is idle
RECAPTURE_WINDOW = 3     # turns: a province given away and taken back by its giver (report constant, docs/TRADE.md 13.2)


def _amount_sum(side: dict[str, Any] | None) -> int:
    return sum(int(n) for n in ((side or {}).get("resources") or {}).values())


class TradeTally:
    """Core 0.8.0 (docs/TRADE.md 12.2): trades settled per game, by kind, rates of resource-for-resource trades,
    province transfers and their price, report copies, failures by reason, lapsed offers, the bots' promises kept /
    broken, transfers taken back by their giver within RECAPTURE_WINDOW turns (by the giver's bot type), objectives
    met with a bought province, and per bot type the idle resource-turns (stock >= IDLE_STOCK at a turn's end) and the
    final stocks. The idle and final figures are kept with trade off too (the base of gate G2)."""

    def __init__(self) -> None:
        self.games = 0          # games with trade on
        self.seen = 0           # all games (idle / final)
        self.settled: list[int] = []
        self.kinds: Counter[str] = Counter()            # resources | province | report
        self.proposed = 0
        self.proposed_by: Counter[str] = Counter()      # bot type
        self.accepted_by: Counter[str] = Counter()      # bot type of the answerer
        self.rejected = 0
        self.failed: Counter[str] = Counter()
        self.lapsed = 0
        self.rates: dict[str, list[float]] = defaultdict(list)
        self.province_prices: list[int] = []
        self.transfers = 0
        self.reports = 0
        self.promises: Counter[str] = Counter()
        self.recapture: Counter[str] = Counter()
        self.hold_type = [0, 0]         # [met, met while holding a bought province of the type]
        self.hold_foreign_traded = 0    # hold_foreign met while holding a traded province (via: capture excludes it)
        self.idle: dict[str, list[int]] = defaultdict(list)
        self.final: dict[str, list[int]] = defaultdict(list)
        self.state_line_max = 0

    def add(self, g: dict[str, Any]) -> None:
        kinds = g["kinds"]
        self.seen += 1
        for seat, kind in kinds.items():
            self.idle[kind].append(sum(1 for t in sorted(g["res_by_turn"])
                                       for n in g["res_by_turn"][t].get(seat, {}).values() if n >= IDLE_STOCK))
            self.final[kind].append(sum(g["resources"][seat].values()))
        if not g.get("trade"):
            return
        self.games += 1
        self.state_line_max = max(self.state_line_max, g.get("state_line_max", 0))
        for e in g["trade_log"]:
            op = e["op"]
            if op["op"] == "trade_offer":
                self.proposed += 1
                self.proposed_by[kinds[op["sender"]]] += 1
            elif op.get("accept"):
                self.accepted_by[kinds[op["seat"]]] += 1
            elif op["seat"] != e["trade"]["a"]:
                self.rejected += 1
        settled = 0
        received: dict[str, set[str]] = defaultdict(set)    # seat -> provinces it got by trade
        transfers: list[dict[str, Any]] = []
        for e in g["events"]:
            kind = e["kind"]
            if kind == "trade_settled":
                settled += 1
                give, want = e.get("give") or {}, e.get("want") or {}
                if give.get("provinces") or want.get("provinces"):
                    self.kinds["province"] += 1
                    self.province_prices.append(_amount_sum(want if give.get("provinces") else give))
                elif give.get("reports"):
                    self.kinds["report"] += 1
                    self.reports += len(give["reports"])
                else:
                    self.kinds["resources"] += 1
                    gr, wr = give.get("resources") or {}, want.get("resources") or {}
                    if len(gr) == 1 and len(wr) == 1:
                        (r1, n1), (r2, n2) = next(iter(gr.items())), next(iter(wr.items()))
                        self.rates[f"{short(r1)}→{short(r2)}"].append(n2 / n1)
            elif kind == "trade_failed":
                self.failed[e.get("reason") or "?"] += 1
            elif kind == "trade_lapsed":
                self.lapsed += 1
            elif kind == tr.TRANSFERRED:
                self.transfers += 1
                received[e["to"]].add(e["province"])
                transfers.append(e)
        self.settled.append(settled)
        for t in transfers:
            if any(c["kind"] == "captured" and c.get("province") == t["province"] and c.get("to") == t["from"]
                   and t["turn"] < c["turn"] <= t["turn"] + RECAPTURE_WINDOW for c in g["events"]):
                self.recapture[kinds[t["from"]]] += 1
        for counts in (g.get("promises") or {}).values():
            self.promises.update(counts)
        for seat, entries in (g.get("objectives") or {}).items():
            held = {pid for pid in received.get(seat, ()) if g["final_owner"].get(pid) == seat}
            for o in entries:
                okind, params = g["objective_kinds"].get(o["id"], (None, {}))
                if not o.get("met"):
                    continue
                if okind == "hold_type":
                    self.hold_type[0] += 1
                    self.hold_type[1] += any(g["ptype"].get(pid) == short(str(params.get("type", ""))) for pid in held)
                elif okind == "hold_foreign" and held:
                    self.hold_foreign_traded += 1

    def summary(self) -> dict[str, Any]:
        g = max(1, self.games)

        def mean(v: list[int]) -> float | None:
            return statistics.mean(v) if v else None

        kept, broken = self.promises["kept"], self.promises["broken"]
        return {"games": self.games, "settled_median": statistics.median(self.settled) if self.settled else None,
                "settled_per_game": sum(self.settled) / g, "by_kind": dict(self.kinds),
                "proposed_per_game": self.proposed / g, "proposed_by": dict(self.proposed_by),
                "accepted_by": dict(self.accepted_by), "rejected_per_game": self.rejected / g,
                "failed": dict(self.failed), "lapsed_per_game": self.lapsed / g,
                "rates": {k: {"median": statistics.median(v), "min": min(v), "max": max(v), "n": len(v)}
                          for k, v in sorted(self.rates.items())},
                "provinces_per_game": self.transfers / g,
                "province_price": mean(self.province_prices), "reports_per_game": self.reports / g,
                "promises": {"kept": kept, "broken": broken,
                             "kept_rate": kept / (kept + broken) if kept + broken else None},
                "transfer_recapture": dict(self.recapture),
                "hold_type_met": self.hold_type[0], "hold_type_bought": self.hold_type[1],
                "hold_foreign_traded": self.hold_foreign_traded,
                "idle_resource_turns": {k: mean(v) for k, v in sorted(self.idle.items())},
                "final_resources": {k: mean(v) for k, v in sorted(self.final.items())},
                "state_line_max": self.state_line_max}

    def report(self) -> list[str]:
        if not self.games:
            return []
        sm = self.summary()
        rates = ", ".join(f"{k} {v['median']:.2f} (n={v['n']})" for k, v in list(sm["rates"].items())[:4]) or "-"
        kept = sm["promises"]["kept_rate"]
        price = sm["province_price"]
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(sm["by_kind"].items())) or "-"
        return [f"  trade: settled {sm['settled_per_game']:.2f}/game (median {sm['settled_median']}), {kinds}; "
                f"offers {sm['proposed_per_game']:.2f}/game, lapsed {sm['lapsed_per_game']:.2f}, failed "
                f"{sm['failed'] or '-'}; rates {rates}; provinces {sm['provinces_per_game']:.2f}/game (price "
                f"{'-' if price is None else f'{price:.1f}'}); report copies {sm['reports_per_game']:.2f}/game; "
                f"promises kept {sm['promises']['kept']} / broken {sm['promises']['broken']} "
                f"({'-' if kept is None else f'{100 * kept:.0f}%'}); recaptured transfers "
                f"{sm['transfer_recapture'] or '-'}"]


def trade_base(path: str | None) -> dict[str, Any] | None:
    """The base of the trade gates (docs/TRADE.md 12.2): a `--json-out` of the same run with `--trade off` - its win
    share per bot type and idle / final stocks."""
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "ALL" not in data:
        data = next((v for v in data.values() if isinstance(v, dict) and "ALL" in v), {})
    al = data.get("ALL") or {}
    return {"win": al.get("win_per_seat") or {},
            **{k: (al.get("trade") or {}).get(k) or {} for k in ("idle_resource_turns", "final_resources")}}


def trade_gates(rs: Ruleset, t: TradeTally, win: dict[str, float], base: dict[str, Any] | None
                ) -> list[dict[str, Any]]:
    """docs/TRADE.md 12.2, gates G1-G7 (`base`: trade_base(), or None: G1 and G2 are not measured)."""
    rows: list[dict[str, Any]] = []
    sm = t.summary()

    def row(name: str, value: Any, corridor: str, ok: bool | None, gate: bool = True) -> None:
        status = "-" if ok is None else ("PASS" if ok else "FAIL") if gate else ("track" if ok else "miss")
        rows.append({"name": name, "value": round(value, 3) if isinstance(value, float) else value,
                     "corridor": corridor, "status": status, "gate": gate})

    if base and base.get("win"):
        shift = {k: round(100 * (win.get(k, 0.0) - base["win"].get(k, 0.0)), 1) for k in sorted(win)}
        row("G1: win share shift per bot type vs trade off (p.p.)", shift, "|shift| <= 10",
            all(abs(v) <= 10 for v in shift.values()))
    else:
        row("G1: win share shift per bot type vs trade off (p.p.)", None, "|shift| <= 10 (--trade-base)", None)
    for key, need, label in (("idle_resource_turns", 0.25, "idle resource-turns"),
                             ("final_resources", 0.20, "final stocks")):
        drops = {}
        for kind in ("builder", "diplomat"):
            old, new = (base or {}).get(key, {}).get(kind), sm[key].get(kind)
            if old and new is not None:
                drops[kind] = round(1 - new / old, 3)
        row(f"G2: {label} drop, builder / diplomat", drops or None, f">= {need:.0%}",
            all(v >= need for v in drops.values()) if drops else None)
    med = sm["settled_median"]
    row("G3: settled trades per game (median)", med, ">= 2", None if med is None else med >= 2)
    row("G3: provinces passed by trade per 10 games", 10 * sm["provinces_per_game"], ">= 1",
        10 * sm["provinces_per_game"] >= 1)
    row("G4: hold_foreign met holding a traded province (via: capture excludes it)", sm["hold_foreign_traded"],
        "0 (track: an upper bound)", sm["hold_foreign_traded"] == 0, gate=False)
    met, bought = sm["hold_type_met"], sm["hold_type_bought"]
    share = bought / met if met else None
    row("G4: hold_type met holding a bought province of the type", share, "< 20%",
        None if share is None else share < 0.2)
    rec = sm["transfer_recapture"]
    row("G5: transfers taken back by an honest giver (builder)", rec.get("builder", 0), "0", rec.get("builder", 0) == 0)
    row("G5: transfers taken back by their giver, all bot types", rec or 0, "track (no war points: decision 28)",
        True, gate=False)
    row("G6: kingmaker against gifts", None, "not played (no forced kingmaker run)", None, gate=False)
    text = rules_text(rs)
    row("G7: rules core chars (rules_text, uncut)", len(text), "<= 6000, no «сокращено»",
        len(text) <= 6000 and "сокращено" not in text)
    row("G7: longest ОБМЕН line of get_state", sm["state_line_max"], "<= 160", sm["state_line_max"] <= 160)
    return rows


def objective_profile(kind: str, params: dict[str, Any]) -> str | None:
    if kind == "techs_branch":
        return PROFILE_BRANCHES.get(short(str(params.get("branch", ""))))
    return PROFILE_KINDS.get(kind)


def _at(by_turn: dict[int, Any], turn: int) -> Any:
    """The entry of `turn`, or of the last turn played when the game ended before it."""
    if not by_turn:
        return {}
    return by_turn.get(turn, by_turn[max(t for t in by_turn if t <= turn)] if min(by_turn) <= turn else {})


class ScoreTally:
    """Stage-4 figures (docs/SCORING.md section 10): the true score by source, objectives and the corridor inputs."""

    def __init__(self) -> None:
        self.games = 0
        self.scored = False
        self.by_kind: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.by_faction: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.winner_share: dict[str, list[float]] = defaultdict(list)   # source -> share in the winner's total
        self.idle: dict[str, list[int]] = defaultdict(list)             # resource -> stock at turn 8, per seat
        self.objectives: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        self.objective_meta: dict[str, tuple[str, dict[str, Any]]] = {}
        self.flip = [0, 0]                                              # [flipped, games to the turn limit]
        self.no_centre = [0.0, 0]                                       # [win share, seats]
        self.battles = 0
        self.turns = 0
        self.attacked = [0, 0]                                          # [seats attacked by a seat, seats]
        self.games_attacked = 0                                         # games with an attack on a seat
        self.works = [0, 0, 0]                                          # started, completed, lost
        self.works_by: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # work -> [started, completed]
        self.betrayal = [0, 0.0, 0, 0, 0]   # core 0.6.0: seats that broke a pact, their wins, penalty, wins lost, seats
        self.war_value = 0
        self.war_defense = 0
        self.pairs = 0
        self.feeding = 0
        # core 0.7.0 (decision 27): works paid in parts, war per province, intel
        self.spire_turns: list[list[int]] = [[], []]                    # turns the Spire was started / completed
        self.spire_by: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # bot type -> Spires started / completed
        self.stalls = [0, 0, 0]          # stalled turns, works stalled at least once, of those completed
        self.war_ev: Counter[str] = Counter()       # war_points events: capture, hold, recapture (a capture again)
        self.pingpong = [0, 0]           # provinces taken between seats >= 3 times in a game, pairs at their cap
        self.war_seat: dict[str, list[int]] = defaultdict(list)          # bot type -> war points per seat
        self.war_at_total = [0, 0]       # seats at the total war cap, seats under one
        self.intel_dist: Counter[int] = Counter()   # intel points per seat
        self.missions_started = 0
        self.seat_captures = 0          # provinces taken from a seat by a seat (any rules)
        self.winner_points: dict[str, list[int]] = defaultdict(list)    # source -> the winner's points

    def add_070(self, g: dict[str, Any]) -> None:
        """Core 0.7.0 figures of one game: the Spire, stalled works, war points per province, intel, missions."""
        kinds, parts = g["kinds"], g.get("score_parts") or {}
        stalled: set[tuple[Any, str]] = set()
        captures: Counter[str] = Counter()
        done: set[tuple[Any, str]] = set()
        for e in g["events"]:
            kind = e["kind"]
            if kind in ("work_started", "work_completed") and short(str(e.get("building"))) == "dream_spire":
                i = int(kind == "work_completed")
                self.spire_turns[i].append(e["turn"])
                self.spire_by[kinds.get(e.get("seat"), "?")][i] += 1
            if kind == "work_completed":
                done.add((e.get("seat"), e["province"]))
            elif kind == "build_stalled":
                self.stalls[0] += 1
                stalled.add((e.get("seat"), e["province"]))
            elif kind == "war_points":
                self.war_ev[e["reason"]] += 1
                if e["reason"] == "capture" and e.get("total", 1) > 1:
                    self.war_ev["recapture"] += 1
                if e.get("points") and e.get("total") == e.get("cap"):     # a reclaim (core 0.7.1) pays 0
                    self.pingpong[1] += 1
            elif kind == "captured" and e.get("from") in kinds and e.get("to") in kinds:
                captures[e["province"]] += 1
        self.stalls[1] += len(stalled)
        self.stalls[2] += sum(1 for k in stalled if k in done)
        self.pingpong[0] += sum(1 for n in captures.values() if n >= 3)
        self.seat_captures += sum(captures.values())
        caps = g.get("war_total_cap") or {}
        for seat, kind in kinds.items():
            p = parts.get(seat) or {}
            if "war" in p:
                self.war_seat[kind].append(p["war"])
                if caps.get(seat):
                    self.war_at_total[1] += 1
                    self.war_at_total[0] += p["war"] >= caps[seat]
            if "intel" in p:
                self.intel_dist[p["intel"]] += 1
        self.missions_started += sum(len(r.get("started") or []) for r in g.get("recons") or [])
        for seat in g.get("winners") or []:
            for src, n in (parts.get(seat) or {}).items():
                self.winner_points[src].append(n)

    def summary_070(self) -> dict[str, Any]:
        g = max(1, self.games)

        def mean(v: list[int]) -> float | None:
            return statistics.mean(v) if v else None

        n_intel = sum(self.intel_dist.values())
        return {"spire": {"started_per_game": len(self.spire_turns[0]) / g,
                          "completed_per_game": len(self.spire_turns[1]) / g,
                          "start_turn": mean(self.spire_turns[0]), "done_turn": mean(self.spire_turns[1]),
                          "by_type": {k: v for k, v in sorted(self.spire_by.items())}},
                "stalls": {"stalled_turns_per_game": self.stalls[0] / g, "works_stalled": self.stalls[1],
                           "works_stalled_then_completed": self.stalls[2]},
                "war": {"seat_captures_per_game": self.seat_captures / g,
                        "captures_per_game": self.war_ev["capture"] / g,
                        "recaptures_per_game": self.war_ev["recapture"] / g, "holds_per_game": self.war_ev["hold"] / g,
                        "reclaims_per_game": self.war_ev["reclaim"] / g,      # core 0.7.1: own land back, 0
                        "pingpong_provinces_per_game": self.pingpong[0] / g,
                        "pairs_at_province_cap_per_game": self.pingpong[1] / g,
                        "per_seat_by_type": {k: mean(v) for k, v in sorted(self.war_seat.items())},
                        "at_total_cap_share": self.war_at_total[0] / self.war_at_total[1]
                        if self.war_at_total[1] else None},
                "intel": {"dist": {str(k): v for k, v in sorted(self.intel_dist.items())},
                          "mean": sum(k * v for k, v in self.intel_dist.items()) / n_intel if n_intel else None},
                "missions_per_game": self.missions_started / g,
                "winner_points": {k: mean(v) for k, v in sorted(self.winner_points.items())}}

    def report_070(self) -> str:
        sm = self.summary_070()
        sp, st, w, it = sm["spire"], sm["stalls"], sm["war"], sm["intel"]

        def f(x: float | None) -> str:
            return "-" if x is None else f"{x:.1f}"

        wt = ", ".join(f"{k} {f(v)}" for k, v in w["per_seat_by_type"].items()) or "-"
        cap = w["at_total_cap_share"]
        return (f"  core 0.7.0: Spire {sp['started_per_game']:.2f}/{sp['completed_per_game']:.2f} per game started/"
                f"completed (turn {f(sp['start_turn'])}/{f(sp['done_turn'])}); stalled turns "
                f"{st['stalled_turns_per_game']:.2f}/game, works stalled {st['works_stalled']} (completed "
                f"{st['works_stalled_then_completed']}); seat-to-seat captures {w['seat_captures_per_game']:.2f}/game; "
                f"war events/game capture {w['captures_per_game']:.2f} "
                f"(recapture {w['recaptures_per_game']:.2f}) hold {w['holds_per_game']:.2f} reclaim "
                f"{w['reclaims_per_game']:.2f}, ping-pong provinces "
                f"{w['pingpong_provinces_per_game']:.2f}, pairs at the province cap "
                f"{w['pairs_at_province_cap_per_game']:.2f}; war/seat {wt}; at the total cap "
                f"{f(None if cap is None else 100 * cap)}%; intel/seat {f(it['mean'])} {it['dist']}; missions "
                f"{sm['missions_per_game']:.1f}/game")

    def add(self, g: dict[str, Any]) -> None:
        self.games += 1
        self.scored = self.scored or bool(g.get("scored"))
        self.add_070(g)
        kinds = g["kinds"]
        parts = g.get("score_parts") or {}
        for seat, kind in kinds.items():
            p = parts.get(seat) or {}
            for src in sc_.PART_ORDER:          # the sources and the pact-break penalty (core 0.6.0)
                if src in p:
                    self.by_kind[kind][src].append(p[src])
                    self.by_faction[short(g["factions"][seat])][src].append(p[src])
        winners = g.get("winners") or []
        for seat in winners:
            # shares of the sources only: the pact-break penalty (core 0.6.0) is no source of points
            p = {src: n for src, n in (parts.get(seat) or {}).items() if src != sc_.BETRAYAL}
            total = sum(p.values())
            for src, n in p.items():
                self.winner_share[src].append(n / total if total > 0 else 0.0)
        stock = _at(g.get("res_by_turn") or {}, IDLE_TURN)
        for seat in kinds:
            for res, n in (stock.get(seat) or {}).items():
                self.idle[res].append(n)
        self.objective_meta.update(g.get("objective_kinds") or {})
        for seat, objs in (g.get("objectives") or {}).items():
            for o in objs:
                row = self.objectives[o["id"]][kinds[seat]]
                row[0] += bool(o.get("met"))
                row[1] += 1
        finals = g.get("scores") or {}
        for seat in kinds:      # core 0.6.0: the pact-break penalty and the wins it cost (a tie counts as a loss)
            self.betrayal[4] += 1
            pen = (parts.get(seat) or {}).get(sc_.BETRAYAL)
            if not pen:
                continue
            self.betrayal[0] += 1
            self.betrayal[1] += (1 / len(winners)) if seat in winners else 0.0
            self.betrayal[2] += pen
            top = max((v for s, v in finals.items() if s != seat), default=None)
            self.betrayal[3] += seat not in winners and top is not None and finals.get(seat, 0) - pen >= top
        if g.get("reason") == "turn_limit" and g.get("winners_open") is not None:
            self.flip[1] += 1
            self.flip[0] += sorted(g["winners_open"]) != sorted(winners)
        central = _at(g.get("central_by_turn") or {}, NO_CENTRE_TURN)
        for seat in kinds:
            if central and central.get(seat, 0) == 0:
                self.no_centre[1] += 1
                self.no_centre[0] += (1 / len(winners)) if seat in winners else 0.0
        self.turns += g["turns"]
        attacked: set[str] = set()
        credits: Counter[tuple[str, str]] = Counter()
        for e in g["events"]:
            kind = e["kind"]
            if kind in ("battle", "edge_battle"):
                self.battles += 1
                if kind == "battle" and e.get("stage") == "defense":
                    att, dfn = e["sides"]
                    if att.get("owner") in kinds and dfn.get("owner") in kinds:
                        attacked.add(dfn["owner"])
            elif kind == "work_started":
                self.works[0] += 1
                self.works_by[short(str(e.get("building")))][0] += 1
            elif kind == "work_completed":
                self.works[1] += 1
                self.works_by[short(str(e.get("building")))][1] += 1
            elif kind == "war_credit":
                self.war_value += e.get("value", 0)
                if e.get("defender"):
                    self.war_defense += e.get("value", 0)
                credits[(e["killer"], e["victim"])] += 1
        self.works[2] += g.get("works_lost", 0)
        self.attacked[0] += len(attacked)
        self.attacked[1] += len(kinds)
        self.games_attacked += bool(attacked)
        pairs = {tuple(sorted(k)) for k in credits}
        self.pairs += len(pairs)
        partners = {tuple(sorted(e["seats"])) for e in g["events"] if e["kind"] in ("pact_signed", "pact_renewed")
                    and len(e.get("seats") or []) == 2}
        for a, b in pairs:
            one_way = (credits[(a, b)] >= FEED_FIGHTS and not credits[(b, a)]) or \
                      (credits[(b, a)] >= FEED_FIGHTS and not credits[(a, b)])
            self.feeding += one_way or (a, b) in partners

    def objective_rates(self) -> dict[str, dict[str, float]]:
        return {oid: {k: v[0] / v[1] for k, v in sorted(rows.items()) if v[1]}
                for oid, rows in sorted(self.objectives.items())}

    def summary(self) -> dict[str, Any]:
        def means(d: dict[str, dict[str, list[int]]]) -> dict[str, dict[str, float]]:
            return {k: {src: statistics.mean(v) for src, v in rows.items() if v} for k, rows in sorted(d.items())}

        def median(res: str) -> float | None:
            v = self.idle.get(res) or []
            return statistics.median(v) if v else None

        return {
            "score_by_source_per_type": means(self.by_kind),
            "score_by_source_per_faction": means(self.by_faction),
            "winner_share_by_source": {src: statistics.mean(v) for src, v in sorted(self.winner_share.items()) if v},
            "winner_share_samples": max((len(v) for v in self.winner_share.values()), default=0),
            "idle_median_turn8": {r: median(r) for r in sorted(self.idle)},
            "objectives_met": self.objective_rates(),
            "objectives_dealt": {oid: {k: v[1] for k, v in rows.items()} for oid, rows in sorted(self.objectives.items())},
            "reveal_flip": self.flip[0] / self.flip[1] if self.flip[1] else None,
            "no_centre_win_share": self.no_centre[0] / self.no_centre[1] if self.no_centre[1] else None,
            "no_centre_seats": self.no_centre[1],
            "battles_per_turn": self.battles / self.turns if self.turns else 0.0,
            "seats_attacked_share": self.attacked[0] / self.attacked[1] if self.attacked[1] else 0.0,
            "games_with_attack_share": self.games_attacked / self.games if self.games else 0.0,
            "works": {"started": self.works[0], "completed": self.works[1], "lost": self.works[2],
                      "by_work": {k: {"started": v[0], "completed": v[1]} for k, v in sorted(self.works_by.items())}},
            "works_lost_share": self.works[2] / self.works[1] if self.works[1] else None,
            "war_defense_share": self.war_defense / self.war_value if self.war_value else None,
            "war_pairs": self.pairs, "war_pairs_feeding": self.feeding,
            "betrayal": {"breakers": self.betrayal[0], "seats": self.betrayal[4],
                         "breaker_share": self.betrayal[0] / self.betrayal[4] if self.betrayal[4] else None,
                         "breaker_win_share": self.betrayal[1] / self.betrayal[0] if self.betrayal[0] else None,
                         "penalty_mean": self.betrayal[2] / self.betrayal[0] if self.betrayal[0] else None,
                         "wins_lost": self.betrayal[3]},
            "core070": self.summary_070(),
        }

    def report(self) -> list[str]:
        sm = self.summary()
        g = max(1, self.games)
        out: list[str] = []
        if self.scored:
            srcs = [src for src in sc_.PART_ORDER if any(src in r for r in sm["score_by_source_per_type"].values())]
            out.append("  score by source (true final, mean)   " + "  ".join(f"{s[:5]:>5}" for s in srcs))
            for label, table in (("type", sm["score_by_source_per_type"]),
                                 ("faction", sm["score_by_source_per_faction"])):
                for k, row in table.items():
                    out.append(f"    {label} {k:<12}" + " " * 18 + "  ".join(f"{row.get(s, 0):5.2f}" for s in srcs))
            share = ", ".join(f"{src} {100 * v:.0f}%" for src, v in sm["winner_share_by_source"].items())
            out.append(f"  winner's score by source: {share or '-'}")
            for oid, rates in sm["objectives_met"].items():
                dealt = sm["objectives_dealt"][oid]
                cells = ", ".join(f"{k} {100 * r:.0f}% of {dealt[k]}" for k, r in rates.items())
                out.append(f"  objective {short(oid)}: met {cells}")
            flip, dshare = sm["reveal_flip"], sm["war_defense_share"]
            out.append(f"  reveal_flip {'-' if flip is None else f'{100 * flip:.0f}%'}; great works started "
                       f"{self.works[0] / g:.2f}/game, completed {self.works[1] / g:.2f}, lost {self.works[2] / g:.2f}"
                       f" ({', '.join(f'{k} {v[0]}/{v[1]}' for k, v in sorted(self.works_by.items())) or '-'}"
                       f" started/completed)"
                       f"; war pairs {self.pairs / g:.2f}/game (feeding {self.feeding}); defence share of war "
                       f"{'-' if dshare is None else f'{100 * dshare:.0f}%'}")
            bt = sm["betrayal"]
            if bt["breakers"]:
                out.append(f"  pact breakers {bt['breakers']} of {bt['seats']} seats ({100 * bt['breaker_share']:.1f}%):"
                           f" win share {100 * bt['breaker_win_share']:.1f}%, penalty {bt['penalty_mean']:.2f},"
                           f" wins the penalty cost {bt['wins_lost']}")
            out.append(self.report_070())
        idle = ", ".join(f"{r} {v:.0f}" for r, v in sm["idle_median_turn8"].items() if v is not None)
        nc = sm["no_centre_win_share"]
        out.append(f"  idle at turn {IDLE_TURN} (median/seat): {idle or '-'}; battles/turn {sm['battles_per_turn']:.2f}; "
                   f"seats attacked by a seat {100 * sm['seats_attacked_share']:.0f}% (games with one "
                   f"{100 * sm['games_with_attack_share']:.0f}%); no centre at turn {NO_CENTRE_TURN}: win share "
                   f"{'-' if nc is None else f'{100 * nc:.1f}%'} of {sm['no_centre_seats']} seats")
        return out


# ---------------------------------------------------------------- stage 5: the Abandoned lands

SIZE_BUCKETS = ((1, 1, "1"), (2, 2, "2"), (3, 4, "3-4"), (5, 10 ** 6, "5+"))
SMALL_ARMY = 2          # decision 25 a: 1-2 unit armies are the "scout sweep" the hindrances must punish
RUINS_TURN = 6          # G6: ruins taken by this turn
LIFT_MIN_SEATS = 30     # G8: a find's lift is gated only with at least this many seats that got it
LAIR_MIN = 20           # G7: the share of lairs cleared is gated only with at least this many lairs that went off


def _size_bucket(n: int) -> str:
    return next(name for lo, hi, name in SIZE_BUCKETS if lo <= n <= hi)


def score_rank(scores: dict[str, int], seat: str) -> str | None:
    """dah.bots.finds.rank on true scores: behind (last or > 3 behind the leader), leader (> 3 ahead of all), middle."""
    from dah.bots.finds import BEHIND_POINTS
    others = [v for s, v in scores.items() if s != seat]
    if seat not in scores or not others:
        return None
    mine = scores[seat]
    if mine < min(others) or max(others) - mine > BEHIND_POINTS:
        return "behind"
    return "leader" if mine - max(others) > BEHIND_POINTS else "middle"


class DiscoveryTally:
    """Stage-5 figures (docs/ABANDONED_LANDS.md 14.2, 14.3): layouts, finds that went off (by id, turn, size of the
    army sent in), the realized value per seat and per entry (every find event carries `value` in P), mixed choices
    by rank, lairs and guarded finds, the ruins and their Treasury, losses to neutrals, the winner's points from
    Abandoned land, each find's lift on the win share, win share by start position, comeback and last_standing."""

    def __init__(self) -> None:
        self.games = 0
        self.with_land = 0
        self.layouts = [0, 0]                                   # [accepted, layouts with finds]
        self.spreads: list[float] = []
        self.placed = 0
        self.found: Counter[str] = Counter()                    # find id -> went off
        self.found_turns: list[int] = []
        self.entries: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])   # army size bucket -> [n, value]
        self.small: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])     # bot type -> 1-2 unit entries
        self.entries_kind: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
        # step 5: the whole worth of an entry by army size: the find, the seat's later choice and guard reward there,
        # minus every unit the seat lost to the find's neutrals in that province (any turn)
        self.entries_full: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
        self.wiped = [0, 0]                                     # [small entries wiped out, small entries]
        self.small_find: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0])   # find -> [n, value, wiped]
        self.value_kind: dict[str, list[float]] = defaultdict(list)            # bot type -> total per seat
        self.land_kind: dict[str, list[int]] = defaultdict(list)    # bot type -> points of Abandoned land at the end
        self.value_abs: list[float] = []
        self.luck = [0.0, 0]                                    # [the luckiest seat's win share, games]
        self.luck_pairs = [0, 0]        # same-type seat pairs whose values differ: [the luckier scored more, pairs]
        self.choices: dict[str, Counter[str]] = defaultdict(Counter)            # find id -> choice -> n
        self.choice_rank: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # rank -> [not the default, all]
        self.lairs = [0, 0]                                     # [lairs that went off, cleared]
        self.guarded = [0, 0]                                   # [guarded boons that went off, cleared]
        self.ruins = [0, 0, 0, 0]                               # [games, taken by turn 6, taken, by last at t3]
        self.treasury_turns: list[int] = []
        self.losses = [0, 0, 0]                                 # [to neutrals, in finds, all]
        self.joint = 0
        self.clipped = 0
        self.winner_land: list[float] = []                      # share of the winner's score from Abandoned land
        self.lift_rows: list[tuple[frozenset[str], float, str]] = []   # (finds got, win share, bot type)
        self.pos_wins: dict[int, float] = defaultdict(float)
        self.pos_seats: Counter[int] = Counter()
        self.last_t3 = [0.0, 0]                                 # [win share of the seat last at turn 3, games]
        self.last_standing = 0

    def add(self, rs: Ruleset, g: dict[str, Any]) -> None:
        self.games += 1
        kinds = g["kinds"]
        winners = g.get("winners") or []
        share = {s: 1 / len(winners) for s in winners}
        self.last_standing += g.get("reason") == "last_standing"
        t3 = (g.get("scores_by_turn") or {}).get(3) or {}
        last = None
        if t3:
            low = min(t3.values())
            lows = [s for s, v in t3.items() if v == low]
            if len(lows) == 1:
                last = lows[0]
                self.last_t3[1] += 1
                self.last_t3[0] += share.get(last, 0.0)
        n = g.get("n_starts") or 0
        base = min(g.get("free_starts") or [0])
        for seat, i in (g.get("position") or {}).items():
            off = (i - base) % n if n else i
            self.pos_seats[off] += 1
            self.pos_wins[off] += share.get(seat, 0.0)
        if not g.get("abandoned"):
            return
        self.with_land += 1
        layout = g.get("layout") or {}
        if layout.get("finds"):
            self.layouts[1] += 1
            self.layouts[0] += bool(layout.get("accepted"))
            self.spreads.append(float(layout.get("spread") or 0.0))
        self.placed += g.get("placed", 0)
        lair_ids = {fid for fid, d in rs.discoveries.items()
                    if any(e["type"] == "spawn_neutral" and e.get("grow") for e in d.effects)}
        guard_ids = {fid for fid, d in rs.discoveries.items()
                     if any(e["type"] == "spawn_neutral" and not e.get("grow") and e.get("reward") for e in d.effects)}
        ruins = {pid for pid in g["abandoned"] if pid in rs.map.capitals}
        # an entry's value includes the fight with the neutrals the find put there in the same turn (a scout that
        # dies to a guard loses its unit): (turn, province, seat) -> P lost in that battle
        fought: dict[tuple[int, str, str], float] = {}
        fought_all: dict[tuple[str, str], float] = {}
        for e in g["events"]:
            if e["kind"] == "battle" and any(x.get("owner") is None for x in e.get("sides") or []):
                for x in e["sides"]:
                    if x.get("owner") is not None:
                        key = (e["turn"], e["province"], x["owner"])
                        lost_p = sum(unit_price(rs, u) * n for u, n in (x.get("losses") or {}).items()) / 20
                        fought[key] = fought.get(key, 0.0) + lost_p
                        fought_all[(e["province"], x["owner"])] = fought_all.get((e["province"], x["owner"]),
                                                                                 0.0) + lost_p
        entered: list[tuple[str, str, str, float]] = []     # (size bucket, province, seat, the find's own value)
        later: dict[tuple[str, str], float] = {}            # (province, seat) -> its choice and guard reward there
        spawned: dict[str, str] = {}        # province -> the find whose neutrals stand there
        value: dict[str, float] = {s: 0.0 for s in kinds}
        got: dict[str, set[str]] = {s: set() for s in kinds}
        ruins_taken: dict[str, tuple[str, int]] = {}
        for e in g["events"]:
            kind = e["kind"]
            if kind == "discovery_found":
                fid = e["id"]
                self.found[short(fid)] += 1
                self.found_turns.append(e["turn"])
                entrants = e.get("entrants") or []
                self.joint += len(entrants) > 1
                if (e.get("province_effects") or {}).get("neutrals"):
                    spawned[e["province"]] = fid
                if fid in lair_ids:
                    self.lairs[0] += 1
                elif fid in guard_ids:
                    self.guarded[0] += 1
                for seat in entrants:
                    v = float((e.get("value") or {}).get(seat, 0.0))
                    value[seat] = value.get(seat, 0.0) + v
                    got.setdefault(seat, set()).add(short(fid))
                    size, _ = (e.get("army") or {}).get(seat) or (0, 0)
                    lost = sum(((e.get("shares") or {}).get(seat) or {}).get("lost", {}).values())
                    self.losses[1] += lost
                    self.losses[2] += lost
                    if size:
                        entered.append((_size_bucket(size), e["province"], seat, v))
                        v -= fought.get((e["turn"], e["province"], seat), 0.0) if e["province"] in spawned else 0.0
                        row = self.entries[_size_bucket(size)]
                        row[0] += 1
                        row[1] += v
                        k = self.entries_kind[kinds.get(seat, "?")]
                        k[0] += 1
                        k[1] += v
                        if size <= SMALL_ARMY:
                            sm = self.small[kinds.get(seat, "?")]
                            sm[0] += 1
                            sm[1] += v
                            self.wiped[1] += 1
                            self.wiped[0] += lost >= size
                            sf = self.small_find[short(fid)]
                            sf[0] += 1
                            sf[1] += v
                            sf[2] += lost >= size
            elif kind == "discovery_chosen":
                seat = e["seat"]
                v = float(e.get("value") or 0.0)
                value[seat] = value.get(seat, 0.0) + v
                later[(e["province"], seat)] = later.get((e["province"], seat), 0.0) + v
                lost = sum((e.get("lost") or {}).values())
                self.losses[1] += lost
                self.losses[2] += lost
                self.choices[short(e["id"])][e["choice"]] += 1
                d = rs.discoveries.get(e["id"])
                rank = score_rank((g.get("scores_by_turn") or {}).get(e["turn"] - 1) or {}, seat)
                if rank is not None and d is not None:
                    row = self.choice_rank[rank]
                    row[0] += e["choice"] != d.default_choice
                    row[1] += 1
            elif kind == "guard_cleared":
                seat = e.get("by")
                if e["province"] in ruins:
                    ruins_taken.setdefault(e["province"], (seat, e["turn"]))
                    continue
                if e.get("lair"):
                    self.lairs[1] += 1
                elif spawned.get(e["province"]) in guard_ids:
                    self.guarded[1] += 1
                value[seat] = value.get(seat, 0.0) + float(e.get("value") or 0.0)
                later[(e["province"], seat)] = later.get((e["province"], seat), 0.0) + float(e.get("value") or 0.0)
            elif kind == "order_clipped":
                self.clipped += 1
            elif kind in ("battle", "edge_battle"):
                sides = e.get("sides") or []
                for i, side in enumerate(sides):
                    if side.get("owner") is None:
                        continue
                    lost = sum((side.get("losses") or {}).values())
                    self.losses[2] += lost
                    if any(o.get("owner") is None for j, o in enumerate(sides) if j != i):
                        self.losses[0] += lost
        counted: set[tuple[str, str]] = set()
        for bucket, pid, seat, v in entered:
            row = self.entries_full[bucket]
            row[0] += 1
            row[1] += v
            if (pid, seat) not in counted:      # a later reward or fight belongs to the seat's first entry there
                counted.add((pid, seat))
                row[1] += later.get((pid, seat), 0.0) - (fought_all.get((pid, seat), 0.0) if pid in spawned else 0.0)
        if ruins:
            self.ruins[0] += 1
            if ruins_taken:
                self.ruins[2] += 1
                seat, turn = min(ruins_taken.values(), key=lambda st: st[1])
                self.ruins[1] += turn <= RUINS_TURN
                self.ruins[3] += seat == last
                self.treasury_turns.append(turn)
        for seat, kind in kinds.items():
            self.value_kind[kind].append(value.get(seat, 0.0))
            self.land_kind[kind].append((g.get("abandoned_points") or {}).get(seat, 0))
            self.value_abs.append(abs(value.get(seat, 0.0)))
            self.lift_rows.append((frozenset(got.get(seat, set())), share.get(seat, 0.0), kind))
        finals = g.get("scores") or {}
        seats_ = sorted(kinds)
        for i, a in enumerate(seats_):
            for b in seats_[i + 1:]:
                if kinds[a] == kinds[b] and value[a] != value[b] and finals.get(a) != finals.get(b):
                    lucky, other = (a, b) if value[a] > value[b] else (b, a)
                    self.luck_pairs[1] += 1
                    self.luck_pairs[0] += finals.get(lucky, 0) > finals.get(other, 0)
        if any(got.values()):
            top = max(value.values())
            best = [s for s, v in value.items() if v == top]
            if len(best) == 1:
                self.luck[1] += 1
                self.luck[0] += share.get(best[0], 0.0)
        pts = g.get("abandoned_points") or {}
        for seat in winners:
            total = sum(v for k, v in ((g.get("score_parts") or {}).get(seat) or {}).items() if k != sc_.BETRAYAL)
            self.winner_land.append(pts.get(seat, 0) / total if total > 0 else 0.0)

    def lift(self) -> dict[str, tuple[float, int]]:
        """Per find: (win share of the seats that got it minus the win share of the seats of their bot types that got
        any find, seats). Adjusted like `artifact_lift`: the strong bot types and the seats strong enough to go into
        Abandoned land enter more finds, and that is no find's doing."""
        if not self.lift_rows:
            return {}
        by_kind: dict[str, list[float]] = defaultdict(list)
        for ids, w, k in self.lift_rows:
            if ids:
                by_kind[k].append(w)
        rate = {k: sum(v) / len(v) for k, v in by_kind.items()}
        out = {}
        for fid in sorted({f for ids, _, _ in self.lift_rows for f in ids}):
            rows = [w - rate[k] for ids, w, k in self.lift_rows if fid in ids]
            out[fid] = (sum(rows) / len(rows), len(rows))
        return out

    def summary(self) -> dict[str, Any]:
        def rate(pair: list[Any]) -> float | None:
            return pair[0] / pair[1] if pair[1] else None

        def entry_rows(table: dict[str, list[float]]) -> dict[str, dict[str, Any]]:
            return {k: {"n": v[0], "value_mean": v[1] / v[0] if v[0] else None} for k, v in sorted(table.items())}

        pos = {str(k): self.pos_wins[k] / self.pos_seats[k] for k in sorted(self.pos_seats)}
        triggered = sum(self.found.values())
        n_small = sum(v[0] for v in self.small.values())
        return {
            "games_with_abandoned": self.with_land,
            "layout_accepted": rate(self.layouts),
            "layout_spread_mean": statistics.mean(self.spreads) if self.spreads else None,
            "finds_placed_per_game": self.placed / self.with_land if self.with_land else 0,
            "finds_triggered_share": triggered / self.placed if self.placed else None,
            "found": dict(sorted(self.found.items())),
            "found_turn_mean": statistics.mean(self.found_turns) if self.found_turns else None,
            "entries_by_size": entry_rows(self.entries), "entries_by_type": entry_rows(self.entries_kind),
            "entries_full_by_size": entry_rows(self.entries_full),
            "small_entries_by_find": {k: {"n": v[0], "value_mean": v[1] / v[0], "wiped": v[2] / v[0]}
                                      for k, v in sorted(self.small_find.items()) if v[0]},
            "small_entries": entry_rows(self.small),
            "small_entry_value_mean": sum(v[1] for v in self.small.values()) / n_small if n_small else None,
            "small_entries_wiped": rate(self.wiped),
            "value_per_seat": {k: statistics.mean(v) for k, v in sorted(self.value_kind.items()) if v},
            "value_abs_per_seat": statistics.mean(self.value_abs) if self.value_abs else None,
            "abandoned_points_per_seat": {k: statistics.mean(v) for k, v in sorted(self.land_kind.items()) if v},
            "luckiest_wins": rate(self.luck), "luck_games": self.luck[1],
            "luck_same_type_pairs": rate(self.luck_pairs), "luck_pairs": self.luck_pairs[1],
            "choices": {fid: dict(c) for fid, c in sorted(self.choices.items())},
            "risky_choice_by_rank": {k: rate(v) for k, v in sorted(self.choice_rank.items())},
            "lairs": {"went_off": self.lairs[0], "cleared": self.lairs[1]},
            "guarded": {"went_off": self.guarded[0], "cleared": self.guarded[1]},
            "ruins": {"games": self.ruins[0], "taken_by_turn6": self.ruins[1], "taken": self.ruins[2],
                      "taken_by_last_at_t3": self.ruins[3],
                      "turn_mean": statistics.mean(self.treasury_turns) if self.treasury_turns else None},
            "losses": {"to_neutrals": self.losses[0], "in_finds": self.losses[1], "all": self.losses[2]},
            "joint_entries": self.joint, "orders_clipped": self.clipped,
            "winner_share_from_abandoned": statistics.mean(self.winner_land) if self.winner_land else None,
            "lift": {k: {"lift": round(v, 3), "seats": n} for k, (v, n) in self.lift().items()},
            "win_by_position": pos, "last_t3_wins": rate(self.last_t3), "last_t3_games": self.last_t3[1],
            "last_standing": self.last_standing / self.games if self.games else 0.0,
        }

    def report(self) -> list[str]:
        if not self.with_land:
            return []
        sm = self.summary()
        g = max(1, self.with_land)
        trig = sm["finds_triggered_share"]
        out = [f"  Abandoned land in {self.with_land} games: layout accepted {_pct(*self.layouts).strip()}, finds "
               f"placed {sm['finds_placed_per_game']:.2f}/game, went off "
               + ("-" if trig is None else f"{100 * trig:.0f}%")
               + f" (mean turn {_mean(self.found_turns).strip()}); joint entries {self.joint}, orders clipped "
               f"{self.clipped}"]
        out.append("  finds went off/game: " + (", ".join(f"{k} {v / g:.2f}" for k, v in sorted(self.found.items()))
                                               or "-"))
        sizes = "; ".join(f"{k} units: {v['n']} (value incl. the fight with the find's neutrals "
                          f"{v['value_mean']:+.2f})"
                          for k, v in sm["entries_by_size"].items() if v["n"])
        out.append(f"  entries by army size sent: {sizes or '-'}")
        full = "; ".join(f"{k} units {v['value_mean']:+.2f}" for k, v in sm["entries_full_by_size"].items() if v["n"])
        per = ", ".join(f"{k} {v['n']} ({v['value_mean']:+.2f}, wiped {100 * v['wiped']:.0f}%)"
                        for k, v in sm["small_entries_by_find"].items())
        if per:
            out.append(f"  1-2 unit entries by find (value incl. the fight, wiped by the find): {per}")
        out.append(f"  whole worth of an entry (find + later choice and guard reward there - all losses to the find's"
                   f" neutrals), by army size: {full or '-'}")
        small, wiped = sm["small_entry_value_mean"], sm["small_entries_wiped"]
        by_type = ", ".join(f"{k} {v['n']} ({v['value_mean']:+.2f})" for k, v in sm["small_entries"].items())
        out.append(f"  1-2 unit entries: value {'-' if small is None else f'{small:+.2f}'} P/entry, wiped out "
                   f"{'-' if wiped is None else f'{100 * wiped:.0f}%'}; by type {by_type or '-'}")
        vals = ", ".join(f"{k} {v:+.2f}" for k, v in sm["value_per_seat"].items())
        luck = sm["luckiest_wins"]
        land = ", ".join(f"{k} {v:.2f}" for k, v in sm["abandoned_points_per_seat"].items())
        out.append(f"  points of Abandoned land held at the end per seat: {land}")
        out.append(f"  realized value of finds per seat: {vals}; mean |value| {sm['value_abs_per_seat'] or 0:.2f}; "
                   f"the luckiest seat wins {'-' if luck is None else f'{100 * luck:.0f}%'} of {self.luck[1]} games; "
                   f"of two seats of one bot type the luckier scores more in {_pct(*self.luck_pairs).strip()} of "
                   f"{self.luck_pairs[1]} pairs")
        ch = "; ".join(f"{fid}: " + ", ".join(f"{c} {n}" for c, n in sorted(cs.items()))
                       for fid, cs in sm["choices"].items())
        risky = ", ".join(f"{k} {100 * v:.0f}%" for k, v in sm["risky_choice_by_rank"].items() if v is not None)
        out.append(f"  mixed choices: {ch or '-'}; not the default, by rank: {risky or '-'}")
        r = sm["ruins"]
        out.append(f"  ruins: taken in {r['taken']}/{r['games']} games ({r['taken_by_turn6']} by turn {RUINS_TURN}, "
                   f"{r['taken_by_last_at_t3']} by the seat last at turn 3, mean turn "
                   f"{_mean(self.treasury_turns).strip()}); lairs {self.lairs[0]} (cleared {self.lairs[1]}); guarded "
                   f"boons {self.guarded[0]} (cleared {self.guarded[1]})")
        ls, wl = sm["losses"], sm["winner_share_from_abandoned"]
        out.append(f"  unit losses: to neutrals {ls['to_neutrals']}, in finds {ls['in_finds']}, all {ls['all']}; "
                   f"winner's score from Abandoned land {'-' if wl is None else f'{100 * wl:.0f}%'}")
        lift = ", ".join(f"{k} {100 * v['lift']:+.0f} ({v['seats']})" for k, v in sm["lift"].items())
        out.append(f"  lift on the win share per find, adjusted for the bot types (seats): {lift or '-'}")
        pos = ", ".join(f"{k} {100 * v:.0f}%" for k, v in sm["win_by_position"].items())
        out.append(f"  win share by start position (offset from the first free start): {pos or '-'}; seat last at "
                   f"turn 3 wins {_pct(*self.last_t3).strip()}")
        return out


def discovery_gates(on: Tally, off: Tally | None, players: int, scout: dict[str, Any] | None = None
                    ) -> list[dict[str, Any]]:
    """G1-G8 of ABANDONED_LANDS.md 14.3 for one switch set with the system on (`off`: the same switches with it off,
    when played) and the scout-sweep gates of decision 25 a. Rows as `corridors`."""
    rows: list[dict[str, Any]] = []

    def row(name: str, value: Any, corridor: str, ok: bool | None, gate: bool = True) -> None:
        status = "-" if ok is None else ("PASS" if ok else "FAIL") if gate else ("track" if ok else "miss")
        rows.append({"name": name, "value": value, "corridor": corridor, "status": status, "gate": gate})

    def r3(x: Any) -> Any:
        return round(x, 3) if isinstance(x, float) else x

    sm = on.s5.summary()
    base = off.s5.summary() if off is not None else None
    back, back_off = sm["last_t3_wins"], (base or {}).get("last_t3_wins")
    row("G1 comeback: win share of the seat last at turn 3", {"on": r3(back), "base": r3(back_off)}, ">= off + 0.04",
        None if back is None or back_off is None else back >= back_off + 0.04)
    lead = on.leader_t3_wins / on.leader_t3_games if on.leader_t3_games else None
    lead_off = off.leader_t3_wins / off.leader_t3_games if off is not None and off.leader_t3_games else None
    row("G2 snowball: leader after turn 3 wins", {"on": r3(lead), "base": r3(lead_off)}, "<= off + 0.03 and <= 0.60",
        None if lead is None else lead <= 0.60 and (lead_off is None or lead <= lead_off + 0.03))
    luck = sm["luckiest_wins"]
    cap = 0.65 if players <= 2 else 0.48
    row("G3 luck: the seat with the most realized value wins", r3(luck), f"<= {cap}",
        None if luck is None else luck <= cap)
    pairs = sm["luck_same_type_pairs"]
    row("luck between two seats of one bot type: the luckier scores more", {"share": r3(pairs),
                                                                            "pairs": sm["luck_pairs"]},
        "<= 0.65 (0.50: no effect)", None if pairs is None else pairs <= 0.65, False)
    acc = sm["layout_accepted"]
    row("G4 layout within the fairness budget", r3(acc), ">= 0.90", None if acc is None else acc >= 0.90)
    pos = {k: r3(v) for k, v in sm["win_by_position"].items()}
    if players == 2 and pos:
        lo, hi = 1 / players - 0.08, 1 / players + 0.08
        row("G4 win share by start position", pos, f"{lo:.2f}-{hi:.2f}", all(lo <= v <= hi for v in pos.values()))
    else:
        row("G4 win share by start position (published, no gate)", pos, "report only", None, False)
    picks = {fid: {c: round(n / sum(cs.values()), 3) for c, n in cs.items()} for fid, cs in sm["choices"].items()}
    low = [f"{fid}/{c}" for fid, cs in picks.items() for c, v in cs.items() if v < 0.15]
    row("G5 every choice of a mixed find taken >= 15%", {"shares": picks, "low": low}, ">= 0.15",
        None if not picks else not low)
    r = sm["ruins"]
    if r["games"]:
        by6 = r["taken_by_turn6"] / r["games"]
        lag = r["taken_by_last_at_t3"] / r["taken"] if r["taken"] else None
        row("G6 ruins taken by turn 6 / by the seat last at turn 3", {"by_turn6": r3(by6), "by_last": r3(lag)},
            "0.40-0.85 / >= 0.35", 0.40 <= by6 <= 0.85 and (lag is None or lag >= 0.35))
    lairs, ls = sm["lairs"], sm["losses"]
    neutral = ls["to_neutrals"] / ls["all"] if ls["all"] else None
    cleared = lairs["cleared"] / lairs["went_off"] if lairs["went_off"] >= LAIR_MIN else None
    row(f"G7 lair cleared (gated from {LAIR_MIN} lairs) / losses to neutrals",
        {"cleared": r3(cleared), "lairs": lairs["went_off"],
                                                 "neutral_losses": r3(neutral)}, "0.30-0.70 / <= 0.15",
        None if neutral is None else neutral <= 0.15 and (cleared is None or 0.30 <= cleared <= 0.70))
    lift = {k: v["lift"] for k, v in sm["lift"].items() if v["seats"] >= LIFT_MIN_SEATS}
    top = max(lift.values(), key=abs, default=None)
    mean_abs = sm["value_abs_per_seat"]
    ls_on, ls_off = sm["last_standing"], (base or {}).get("last_standing")
    row("G8 max lift of one find / mean |value| per seat / last_standing",
        {"max_lift": r3(top), "value_abs": r3(mean_abs), "last_standing": r3(ls_on), "base": r3(ls_off)},
        "<= 0.10 / <= 4 / <= off + 0.05",
        (top is None or abs(top) <= 0.10) and (mean_abs is None or mean_abs <= 4)
        and (ls_off is None or ls_on <= ls_off + 0.05))
    small = sm["small_entry_value_mean"]
    if small is None and scout:     # the ordinary bots send no 1-2 unit armies there: the scout's entries
        small = scout.get("entry_value")
    row("scout sweep: realized value of a 1-2 unit entry (all bots, else the scout's)", r3(small),
        "< 0 (decision 25 a)", None if small is None else small < 0)
    if scout:
        # "does not pay" (step 5): the sweep wins no more than the same seat with the same Militia kept at home (the
        # control), beyond the noise of the paired games; against the builder it also counts the extra Militia
        # (the builder bot under-recruits), so that row is tracked, not gated
        row("scout sweep: the scout seat's win share vs the control (same Militia, no sweep), same seeds",
            {k: r3(scout[k]) for k in ("games", "scout_win", "control_win", "control_diff_se")},
            "scout <= control + 2 standard errors",
            scout["scout_win"] <= scout["control_win"] + 2 * scout.get("control_diff_se", 0.0))
        row("scout sweep: the scout seat's win share vs the builder's on the same seeds",
            {k: r3(scout[k]) for k in ("scout_win", "builder_win", "diff_se", "scout_small_entries", "entry_value")},
            "scout <= builder + 2 standard errors",
            scout["scout_win"] <= scout["builder_win"] + 2 * scout.get("diff_se", 0.0), False)
    wl = sm["winner_share_from_abandoned"]
    row("winner's score from Abandoned land", r3(wl), "0.05-0.20", None if wl is None else 0.05 <= wl <= 0.20, False)
    risky = sm["risky_choice_by_rank"]
    row("risk by rank: behind takes a non-default choice >= leader + 0.15", {k: r3(v) for k, v in risky.items()},
        "behind >= leader + 0.15", None if risky.get("behind") is None or risky.get("leader") is None else
        risky["behind"] >= risky["leader"] + 0.15, False)
    trig = sm["finds_triggered_share"]
    row("finds that went off by the end", r3(trig), ">= 0.60", None if trig is None else trig >= 0.60, False)
    return rows


def corridors(rs: Ruleset, overall: Tally, per_config: dict[str, Tally], probe: Tally | None,
              base: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The corridors of SCORING.md 10 for one weight set: [{name, value, corridor, status}] (status PASS / FAIL for
    the gates, track / miss for the tracked ones; "-" when there is nothing to measure). `base`: the summary of the
    land-only weight set of the same switches, when it was played too."""
    rows: list[dict[str, Any]] = []

    def row(name: str, value: Any, corridor: str, ok: bool | None, gate: bool) -> None:
        status = "-" if ok is None else ("PASS" if ok else "FAIL") if gate else ("track" if ok else "miss")
        rows.append({"name": name, "value": value, "corridor": corridor, "status": status, "gate": gate})

    # the mix of three: the rush,builder,diplomat configuration; else (quad4) the configurations holding all three
    three = {"builder", "diplomat", "rush"}
    mixes = [cfg for cfg in per_config if three <= set(cfg.split(","))]
    mix = next((per_config[cfg] for cfg in mixes if sorted(cfg.split(",")) == sorted(three)),
               overall if mixes and len(mixes) == len(per_config) else per_config[mixes[0]] if mixes else None)
    best = max((mix.win_share[k] / mix.seats[k] for k in mix.seats), default=None) if mix is not None else None
    row("mix: win share of any bot type", None if best is None else round(best, 3), "<= 0.50",
        None if best is None else best <= 0.50, True)
    lead = overall.leader_t3_wins / overall.leader_t3_games if overall.leader_t3_games else None
    row("leader after turn 3 wins", None if lead is None else round(lead, 3), "<= 0.60 (target 0.55)",
        None if lead is None else lead <= 0.60, True)
    sm = overall.s4.summary()
    terr = sm["winner_share_by_source"].get("territory")
    measured = terr is not None and not rs.default_scoring     # land only: the whole score, nothing to measure
    row("territory share of the winner's score", None if terr is None else round(terr, 3), "0.40-0.55",
        0.40 <= terr <= 0.55 if measured else None, True)
    food = next((v for r, v in sm["idle_median_turn8"].items() if r.endswith("provisions")), None)
    row(f"idle provisions at turn {IDLE_TURN} (median)", food, "< 120", None if food is None else food < 120, True)
    if sc_.on(rs, "objectives"):
        rates = sm["objectives_met"]
        passive = (probe.s4.objective_rates() if probe is not None else {})
        bad = []
        detail = {}
        for oid, by_type in rates.items():
            kind, params = overall.s4.objective_meta.get(oid, ("?", {}))
            prof = objective_profile(kind, params)
            others = [r for k, r in by_type.items() if k not in ("passive", prof)]
            mean = statistics.mean(others) if others else None
            pas = (passive.get(oid) or {}).get("passive", by_type.get("passive"))
            detail[short(oid)] = {"mean_non_profile": None if mean is None else round(mean, 3),
                                  "passive": None if pas is None else round(pas, 3), "profile": prof,
                                  "profile_rate": None if prof is None else by_type.get(prof)}
            if (pas is not None and pas > 0.15) or (mean is not None and not 0.30 <= mean <= 0.60):
                bad.append(short(oid))
        row("objectives met: passive <= 15%, non-profile mean 30-60%", {"off": bad, "per_objective": detail},
            "every objective", not bad if rates else None, True)
        flip = sm["reveal_flip"]
        row("reveal_flip", None if flip is None else round(flip, 3), "0.10-0.30",
            None if flip is None else 0.10 <= flip <= 0.30, True)
    # tracked
    nc = sm["no_centre_win_share"]
    nc_base = (base or {}).get("no_centre_win_share")
    row("no centre at turn 6: win share vs land-only", {"value": nc, "land": nc_base}, ">= land + 0.05",
        None if nc is None or nc_base is None else nc >= nc_base + 0.05, False)
    if not rs.default_scoring:
        shares = {}
        for cfg, t in per_config.items():
            for src, v in t.s4.summary()["winner_share_by_source"].items():
                shares[src] = max(shares.get(src, 0.0), v)
        weak = [src for src in SCORE_SOURCES if sc_.on(rs, src) and shares.get(src, 0.0) < 0.10]
        row("every source >= 10% of the winner's score in some configuration",
            {src: round(v, 3) for src, v in sorted(shares.items())}, "all sources", not weak, False)
        sci = sm["winner_share_by_source"].get("science")
        row("science share of the winner's score", None if sci is None else round(sci, 3), "<= 0.30",
            None if sci is None else sci <= 0.30, False)
    creed = next((v for r, v in sm["idle_median_turn8"].items() if r.endswith("creed")), None)
    row(f"idle creed at turn {IDLE_TURN} (median)", creed, "< 15", None if creed is None else creed < 15, False)
    bt, bt_base = sm["battles_per_turn"], (base or {}).get("battles_per_turn")
    att, att_base = sm["seats_attacked_share"], (base or {}).get("seats_attacked_share")
    row("battles per turn / seats attacked vs land-only",
        {"battles_per_turn": round(bt, 3), "land": bt_base, "seats_attacked": round(att, 3), "land_attacked": att_base},
        ">= land", None if bt_base is None else (bt >= bt_base and att >= att_base), False)
    if sc_.on(rs, "works"):
        lost = sm["works_lost_share"]
        row("great works lost / completed", None if lost is None else round(lost, 3), ">= 0.15",
            None if lost is None else lost >= 0.15, False)
    if sc_.on(rs, "war"):
        row("defence share of war credit", sm["war_defense_share"], "report only", None, False)
    return rows


def corridor_lines(rows: list[dict[str, Any]]) -> list[str]:
    out = ["  corridors (SCORING.md 10; gates first):"]
    for r in rows:
        value = r["value"]
        if isinstance(value, dict) and "off" in value:
            value = "off: " + (", ".join(value["off"]) or "none")
        out.append(f"    [{r['status']:>5}] {r['name']}: {value} (corridor {r['corridor']})")
    return out


def artifact_lift(t: Tally) -> dict[str, tuple[float, float]]:
    """Per artifact: (raw lift, adjusted lift) in win share. Raw: the artifact's win/seat minus the mean win/seat of
    all seats; adjusted: minus what the holders' factions and bot types predict (faction rate + bot type rate - mean),
    so a strong faction or bot type does not make the artifacts it likes look strong."""
    if not t.seat_rows:
        return {}
    base = sum(w for *_, w in t.seat_rows) / len(t.seat_rows)
    f_rate = {f: t.f_wins[f] / t.f_seats[f] for f in t.f_seats}
    k_rate = {k: t.win_share[k] / t.seats[k] for k in t.seats}
    out = {}
    for art, n in t.a_seats.items():
        rows = [(f, k) for f, k, arts, _ in t.seat_rows if art in arts]
        expected = sum(f_rate[f] + k_rate[k] - base for f, k in rows) / n
        rate = t.a_wins[art] / n
        out[art] = (rate - base, rate - expected)
    return out


def short(full_id: str) -> str:
    return full_id.split(":")[-1]


def _pct(a: float, b: float) -> str:
    return f"{100 * a / b:5.1f}%" if b else "    - "


def _mean(xs: list[float]) -> str:
    return f"{statistics.mean(xs):5.1f}" if xs else "    -"


def report(name: str, t: Tally) -> list[str]:
    out = [f"== {name}: {t.games} games, mean length {_mean(t.lengths)} turns"]
    out.append("  type       seats  win/seat  mean score  eliminated  starved/game  idle alloy  idle food  recruits"
               "  farms  mines")
    for kind in sorted(t.seats):
        seats = t.seats[kind]
        idle = t.idle[kind]
        out.append(f"  {kind:<9} {seats:6d}  {_pct(t.win_share[kind], seats)}  {_mean(t.scores[kind]):>10}"
                   f"  {_pct(t.eliminated[kind], seats):>10}  {t.starvation[kind] / seats:12.2f}"
                   f"  {_mean(idle.get('alloy', [])):>10}  {_mean(idle.get('provisions', [])):>9}"
                   f"  {t.recruited[kind] / seats:8.1f}  {t.built[kind]['farm'] / seats:5.1f}"
                   f"  {t.built[kind]['mine'] / seats:5.1f}")
    out.append(f"  battles/game {t.battles / t.games:.2f} (province) + {t.edge_battles / t.games:.2f} (edge); "
               f"first capture from a player: mean turn {_mean(t.first_capture)}, none in {_pct(t.no_capture, t.games)}"
               f"; first neutral capture: turn {_mean(t.first_neutral)}")
    out.append(f"  leader after turn 3 wins: {_pct(t.leader_t3_wins, t.leader_t3_games)} "
               f"(of {t.leader_t3_games} games with a single leader)")
    for label, table in (("unfortified", t.attacks), ("fortified", t.attacks_fort)):
        cells = [f"{name} {_pct(*table[name]).strip()} of {table[name][1]}" for _, _, name in RATIO_BUCKETS
                 if table[name][1]]
        total = [sum(v[0] for v in table.values()), sum(v[1] for v in table.values())]
        out.append(f"  attacker wins ({label}): all {_pct(*total).strip()} of {total[1]}; " + "; ".join(cells))
    if t.diplo:
        out.append("  diplomacy/game: " + ", ".join(f"{k} {v / t.games:.2f}" for k, v in sorted(t.diplo.items())))
    if len(t.f_seats) > 1 or t.a_seats or t.has_techs:
        out += stage2_report(t)
    out += t.s3.report()
    out += t.sv2.report()
    out += t.trd.report()
    out += t.s4.report()
    out += t.s5.report()
    return out


def stage2_report(t: Tally) -> list[str]:
    """Win rate per faction and per artifact, techs per seat (stage 2)."""
    out = ["  faction       seats  win/seat  mean score  techs/seat"]
    for faction in sorted(t.f_seats, key=lambda f: (-t.f_wins[f] / t.f_seats[f], f)):
        seats = t.f_seats[faction]
        out.append(f"  {faction:<12} {seats:6d}  {_pct(t.f_wins[faction], seats)}  {_mean(t.f_scores[faction]):>10}"
                   f"  {_mean(t.f_techs[faction]):>10}")
    if t.a_seats:
        lift = artifact_lift(t)
        out.append("  artifact            seats  win/seat   lift  lift(adj.)")
        for art in sorted(t.a_seats, key=lambda a: (-t.a_wins[a] / t.a_seats[a], a)):
            raw, adj = lift[art]
            out.append(f"  {art:<18} {t.a_seats[art]:6d}  {_pct(t.a_wins[art], t.a_seats[art])}"
                       f"  {100 * raw:+5.1f}  {100 * adj:+5.1f}")
    if t.has_techs:
        out.append(f"  tier-2 tech: in {_pct(t.tier2_games, t.games).strip()} of games, "
                   f"{_pct(t.tier2_seats, sum(t.f_seats.values())).strip()} of seats")
        per_type = ", ".join(f"{kind} {_mean(v).strip()}" for kind, v in sorted(t.techs.items()))
        out.append(f"  techs/seat by bot type: {per_type}")
        top = ", ".join(f"{tech} {n / t.games:.2f}" for tech, n in t.tech_done.most_common(10))
        out.append(f"  most researched (per game): {top or '-'}")
    return out


def combat_table(rs: Ruleset) -> list[str]:
    """Attacker win probability (engine estimate) by army ratio, province type and fortification; assault only."""
    out = ["== combat: P(attacker wins), assault vs assault, defenders 4 (ratio = attackers / defenders)"]
    ratios = (1.0, 1.25, 1.5, 2.0, 2.5)
    unit = next(iter(sorted(rs.units)))
    unit = rs.resolve("unit", "assault") or unit
    out.append("  province         " + "".join(f"{r:>8g}x" for r in ratios))
    for ptype in sorted(rs.province_types):
        for fort in (False, True):
            cells = []
            for r in ratios:
                p = estimate(rs, {unit: round(4 * r)}, {unit: 4}, ptype, fortified=fort)["win_prob"]
                cells.append(f"{p:9.2f}")
            out.append(f"  {ptype.split(':')[-1] + (' +fort' if fort else ''):<16}" + "".join(cells))
    return out


def sync_bot_numbers(rs: Ruleset) -> list[str]:
    """The bots plan with the core pack's numbers (dah.bots.view); point them at this ruleset's numbers so a run with
    another pack (an older core, say) is fair. Returns what differed from the bots' built-in numbers."""
    from dah.bots import view as bv

    build, food = rs.roles["build"], rs.roles["upkeep_food"]
    changed = []
    for full in rs.units:
        other = {short(r): n for r, n in rs.unit_cost(full).items() if r != build and n}
        if bv.UNIT_COST_OTHER.get(short(full), {}) != other:
            changed.append(f"{short(full)} {bv.UNIT_COST_OTHER.get(short(full))} -> {other}")
        if other:
            bv.UNIT_COST_OTHER[short(full)] = other
        else:
            bv.UNIT_COST_OTHER.pop(short(full), None)
    for table, items in ((bv.UNIT_COST_ALLOY, {u: rs.unit_cost(u).get(build, 0) for u in rs.units}),
                         (bv.UNIT_UPKEEP_FOOD, {u: rs.unit_upkeep(u).get(food, 0) for u in rs.units}),
                         (bv.BUILDING_COST_ALLOY, {b: rs.building_cost(b).get(build, 0) for b in rs.buildings})):
        for full, value in items.items():
            sid = short(full)
            if table.get(sid) != value:
                changed.append(f"{sid} {table.get(sid)} -> {value}")
            table[sid] = value
    # core 0.6.0: the pact-break penalty the diplomat weighs before a betrayal (0 when it is off)
    penalty = int(rs.stat("score.betrayal.per_break")) if sc_.betrayal_on(rs) else 0
    if bv.BETRAYAL_PENALTY != penalty:
        changed.append(f"betrayal {bv.BETRAYAL_PENALTY} -> {penalty}")
    bv.BETRAYAL_PENALTY = penalty
    # stage 4: great works (provisions beyond alloy, turns, points, allowed types) while works score
    if sc_.on(rs, "works"):
        for full, bd in rs.buildings.items():
            if not bd.score:
                continue
            sid = short(full)
            value = (bd.build_turns, int(rs.stat(f"building.{full}.score")),
                     tuple(short(t) for t in bd.allowed_types) or None)
            if bv.GREAT_WORKS.get(sid) != value:
                changed.append(f"work {sid} {bv.GREAT_WORKS.get(sid)} -> {value}")
            bv.GREAT_WORKS[sid] = value
            cost = rs.building_cost(full).get(food, 0)
            if bv.BUILDING_COST_FOOD.get(sid, 0) != cost:
                changed.append(f"{sid} provisions {bv.BUILDING_COST_FOOD.get(sid, 0)} -> {cost}")
            bv.BUILDING_COST_FOOD[sid] = cost
        # core 0.7.0: the works paid in parts (synced in place: dah.bots.score holds the same set)
        parts = {short(f) for f, bd in rs.buildings.items() if bd.score and bd.payment == "parts"}
        if parts != bv.WORKS_IN_PARTS:
            changed.append(f"works in parts {sorted(bv.WORKS_IN_PARTS)} -> {sorted(parts)}")
            bv.WORKS_IN_PARTS.clear()
            bv.WORKS_IN_PARTS.update(parts)
    # stage 3: Shade, missions, fates and conquest values (only while the system is on)
    if rs.espionage is not None:
        cost = rs.espionage.shade_cost.get(build, 0)
        if bv.SHADE_COST_ALLOY != cost:
            changed.append(f"shade {bv.SHADE_COST_ALLOY} -> {cost}")
        bv.SHADE_COST_ALLOY = cost
        for full, md in rs.missions.items():
            value = (md.whisper, md.rounds_max, md.cost.get(build, 0), md.cost_per_round.get(food, 0))
            if bv.MISSIONS.get(short(full)) != value:
                changed.append(f"mission {short(full)} {bv.MISSIONS.get(short(full))} -> {value}")
            bv.MISSIONS[short(full)] = value
        for sid in sorted(set(bv.MISSIONS) - {short(f) for f in rs.missions}):     # e.g. `secret` on core 0.5.0
            changed.append(f"mission {sid} {bv.MISSIONS[sid]} -> none")
            bv.MISSIONS.pop(sid)
        # core 0.8.0 (docs/SPY_V2.md 10): the missions' `intel` kinds and the Ward's price
        intel = {short(f): md.intel for f, md in rs.missions.items()}
        if intel != bv.MISSION_INTEL:
            changed.append(f"intel {bv.MISSION_INTEL} -> {intel}")
            bv.MISSION_INTEL.clear()
            bv.MISSION_INTEL.update(intel)
        ward = rs.espionage.ward_cost.get(build, 0)
        if bv.WARD_COST_ALLOY != ward:
            changed.append(f"ward {bv.WARD_COST_ALLOY} -> {ward}")
        bv.WARD_COST_ALLOY = ward
    if rs.system("conquest"):
        for full, dd in rs.dispositions.items():
            value = dd.cost.get(food, 0)
            if bv.DISPOSITION_FOOD.get(short(full)) != value:
                changed.append(f"disposition {short(full)} {bv.DISPOSITION_FOOD.get(short(full))} -> {value}")
            bv.DISPOSITION_FOOD[short(full)] = value
        for full, pt in rs.province_types.items():
            if pt.conquest_value != bv.CONQUEST_VALUE.get(short(full), 1):
                changed.append(f"value {short(full)} {bv.CONQUEST_VALUE.get(short(full), 1)} -> {pt.conquest_value}")
            bv.CONQUEST_VALUE[short(full)] = pt.conquest_value
    return changed


def summary(t: Tally) -> dict[str, Any]:
    """The JSON tallies of one configuration (or of all of them)."""
    return {"games": t.games, "win_per_seat": {k: t.win_share[k] / t.seats[k] for k in t.seats},
            "win_per_faction": {f: t.f_wins[f] / t.f_seats[f] for f in t.f_seats},
            "seats_per_faction": dict(t.f_seats),
            "win_per_artifact": {a: t.a_wins[a] / t.a_seats[a] for a in t.a_seats},
            "seats_per_artifact": dict(t.a_seats),
            "artifact_lift": {a: list(v) for a, v in artifact_lift(t).items()},
            "tier2_games": t.tier2_games / t.games if t.games else 0.0,
            "techs_per_faction": {f: statistics.mean(v) for f, v in t.f_techs.items() if v},
            "techs_per_seat": {k: statistics.mean(v) for k, v in t.techs.items() if v},
            "synclite_win_per_seat": (t.f_wins["synclite"] / t.f_seats["synclite"]) if t.f_seats["synclite"] else None,
            "stage3": t.s3.summary(), "spy": t.sv2.summary(), "scoring": t.s4.summary(), "discoveries": t.s5.summary(),
            "trade": t.trd.summary()}


def rotations(kinds: list[str]) -> list[dict[str, str]]:
    perms = sorted(set(itertools.permutations(kinds)))
    return [dict(zip(SEATS, p)) for p in perms]


def dealt_factions(factions: list[str], seats: list[str], block: int) -> dict[str, str]:
    """Factions dealt by block (one full rotation of the bot types over the seats): block b takes the b-th set of
    len(seats) different factions (all such sets in turn, so every faction meets every other one equally often), in
    an order shifted every full pass over the sets, so a faction meets every bot type and seat. With fewer factions
    than seats: seat j gets factions[(b + j) % n] (a faction repeats)."""
    seats = sorted(seats)
    if len(factions) < len(seats):
        return {seat: factions[(block + j) % len(factions)] for j, seat in enumerate(seats)}
    combos = list(itertools.combinations(factions, len(seats)))
    combo = combos[block % len(combos)]
    shift = (block // len(combos) + block) % len(seats)
    return dict(zip(seats, combo[shift:] + combo[:shift]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--games", type=int, default=200, help="games per configuration (default 200)")
    ap.add_argument("--config", action="append", help="comma-separated bot types for seats, e.g. rush,builder,"
                                                      "diplomat (repeatable; default: a fixed set)")
    ap.add_argument("--scenario", default="core:slice3")
    ap.add_argument("--packs", help="directory holding pack directories (default: content/packs of the repo)")
    ap.add_argument("--combat", action="store_true", help="print only the combat table")
    ap.add_argument("--factions", default="rotate",
                    help="rotate (deal the scenario's factions by rotation, default), bots (bots choose by style) "
                         "or a comma-separated list of faction ids to rotate over")
    ap.add_argument("--artifacts", choices=("draft", "points", "none"),
                    help="artifact mode of the setup phase (default: the scenario's)")
    ap.add_argument("--artifact-offer", type=int)
    ap.add_argument("--artifact-points", type=int)
    ap.add_argument("--espionage", choices=("on", "off", "both"), help="espionage (default: the scenario's)")
    ap.add_argument("--conquest", choices=("on", "off", "both"), help="conquest (default: the scenario's)")
    ap.add_argument("--discoveries", choices=("on", "off", "both"),
                    help="Abandoned lands and finds (default: the scenario's); seats fewer than the map's starts")
    ap.add_argument("--discovery-knob", action="append",
                    help="an Abandoned-land knob, key=value (repeatable), e.g. souls=halve, extra=2, fair_mirror=true")
    ap.add_argument("--scout-sweep", action="store_true",
                    help="stage 5: replay every configuration holding a builder with that builder as the scout bot "
                         "(1-unit armies into every Abandoned province) on the same seeds, and gate it")
    ap.add_argument("--scoring", action="append",
                    help="weight set: land (territory only), scenario (default), all (every source 1), both (land "
                         "and the scenario's) or SRC=W,... (repeatable)")
    ap.add_argument("--objectives", type=int, help="objectives dealt per seat (a host's --objectives)")
    ap.add_argument("--spy-base", help="core 0.8.0: the --json-out of the same run on the previous core (the win "
                                       "share shift of the spy gates, docs/SPY_V2.md 10)")
    ap.add_argument("--trade", choices=("on", "off", "both"), help="core 0.8.0: trade (default: the scenario's)")
    ap.add_argument("--trade-base", help="core 0.8.0: the --json-out of the same run with --trade off (gates G1, G2 of "
                                         "docs/TRADE.md 12.2)")
    ap.add_argument("--json", action="store_true", help="print the tallies as JSON")
    ap.add_argument("--json-out", help="also write the JSON tallies to this file")
    args = ap.parse_args(argv)
    pack_dirs = None
    if args.packs:
        root = Path(args.packs)
        pack_dirs = sorted((p for p in root.iterdir() if (p / "pack.yaml").is_file()), key=lambda p: p.name != "core")
    packs = pack_dirs or default_pack_dirs()
    base = build_ruleset(packs, args.scenario)
    if args.combat:
        print("\n".join(combat_table(base)))
        return 0
    lines: list[str] = []
    data: dict[str, Any] = {}
    combos = switch_combos(base, args.espionage, args.conquest, args.discoveries, args.trade)
    try:
        knobs = discovery_knobs(args.discovery_knob)
    except ValueError as exc:
        ap.error(str(exc))
    try:
        weight_sets = scoring_sets(base, args.scoring)
    except ValueError as exc:
        ap.error(str(exc))
    started = time.monotonic()
    games = 0
    played: dict[tuple[Any, ...], dict[bool, tuple[str, dict[str, Any]]]] = {}     # stage 5: on/off pairs
    for combo in combos:
        land: dict[str, Any] | None = None
        for name, weights in weight_sets:
            try:
                rs = build_ruleset(packs, args.scenario, systems=combo, scoring=weights, objectives=args.objectives,
                                   discoveries=knobs) \
                    if len(combos) > 1 or weights is not None or args.objectives is not None or knobs or any(
                        v is not None for v in (args.espionage, args.conquest, args.discoveries, args.trade)) else base
            except Exception as exc:    # ContentError: a bad weight set
                ap.error(str(exc))
            label = ",".join(f"{k}={'on' if rs.system(k) else 'off'}" for k in ("espionage", "conquest")
                             + (("discoveries",) if "discoveries" in combo else ())
                             + (("trade",) if "trade" in combo else ()))
            if len(weight_sets) > 1 or weights is not None:
                label += f",scoring={name}"
            if len(combos) > 1 or len(weight_sets) > 1:
                lines.append(f"######## {label}")
            part, out, n, extra = run_combo(ap, args, rs, land)
            if rs.default_scoring:
                land = out["ALL"]["scoring"]
            lines += part
            games += n
            data[label] = out
            if "discoveries" in combo:
                key = (combo.get("espionage"), combo.get("conquest"), name)
                played.setdefault(key, {})[bool(combo["discoveries"])] = (label, extra)
    for pair in played.values():     # stage 5: the gates of ABANDONED_LANDS.md 14.3 (with the system off as base)
        if True not in pair:
            continue
        label, extra = pair[True]
        off = pair[False][1]["overall"] if False in pair else None
        rows = discovery_gates(extra["overall"], off, extra["players"], extra.get("scout"))
        data[label]["discovery_gates"] = rows
        base = pair[False][0] if False in pair else "none (the system was not played off)"
        lines.append(f"######## discovery gates ({label}; base {base})")
        lines += corridor_lines(rows)[1:]
    lines.append(f"({games} games in {time.monotonic() - started:.0f} s)")
    if len(data) == 1:
        data = next(iter(data.values()))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print("\n".join(lines))
    return 0


def scoring_sets(rs: Ruleset, specs: list[str] | None) -> list[tuple[str, dict[str, int] | None]]:
    """(label, weights) per `--scoring` value; None weights: the scenario's own. `land` comes first, so the other sets
    can be compared with it."""
    if not specs:
        return [("scenario", None)]
    out: list[tuple[str, dict[str, int] | None]] = []
    for spec in specs:
        for item in ([("land", {"territory": 1}), ("scenario", None)] if spec == "both" else [(spec, None)]):
            name, weights = item
            if name == "land":
                weights = {"territory": 1}
            elif name == "all":
                weights = {src: 1 for src in SCORE_SOURCES}
            elif name != "scenario" and weights is None:
                from dah.cli import parse_scoring
                weights = parse_scoring(name)
            if all(name != n for n, _ in out):
                out.append((name, weights))
    return sorted(out, key=lambda nw: nw[0] != "land")


def switch_combos(rs: Ruleset, espionage: str | None, conquest: str | None,
                  discoveries: str | None = None, trade: str | None = None) -> list[dict[str, bool]]:
    """The switch combinations to play: each of `on|off|both` (None: the scenario's value). `discoveries` (stage 5,
    ABANDONED_LANDS.md 14.2) joins the combination only when given or on in the scenario; `trade` (core 0.8.0) only
    when given."""
    def values(name: str, arg: str | None) -> list[bool]:
        if arg == "both":
            return [True, False]
        if arg is None:
            return [rs.system(name)]
        return [arg == "on"]

    finds = values("discoveries", discoveries) if discoveries is not None or rs.system("discoveries") else [None]
    trades = values("trade", trade) if trade is not None else [None]
    return [{"espionage": e, "conquest": c, **({} if d is None else {"discoveries": d}),
             **({} if x is None else {"trade": x})}
            for e in values("espionage", espionage) for c in values("conquest", conquest) for d in finds
            for x in trades]


def discovery_knobs(items: list[str] | None) -> dict[str, Any] | None:
    """`--discovery-knob key=value` (repeatable): the scenario's Abandoned-land knobs without editing the pack
    (ABANDONED_LANDS.md 11, decision 25 d: souls=ravage|halve|ashes); values are read as YAML."""
    import yaml
    out: dict[str, Any] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"bad --discovery-knob {item!r}; expected key=value")
        out[key.strip()] = yaml.safe_load(value)
    return out or None


def run_combo(ap: argparse.ArgumentParser, args: argparse.Namespace, rs: Ruleset,
              land: dict[str, Any] | None = None) -> tuple[list[str], dict, int, dict[str, Any]]:
    """Every configuration of one switch combination and weight set: (text lines, JSON tallies, games played,
    {overall: the Tally of all configurations, players, scout: the scout-sweep pairing or None}).
    `land`: the score summary of the land-only weight set played before (the base of the tracked corridors)."""
    changed = sync_bot_numbers(rs)
    lines = combat_table(rs)
    if changed:
        lines.append("(bots plan with this pack's numbers, not their built-in ones: " + ", ".join(changed) + ")")
    setup = setup_config(rs, args.artifacts, args.artifact_offer, args.artifact_points)
    deal: list[str] | None
    if args.factions == "bots":
        deal = None
    elif args.factions == "rotate":
        deal = list(rs.scenario.factions)
    else:
        deal = [rs.resolve("faction", f.strip()) or "" for f in args.factions.split(",")]
        if not all(deal):
            ap.error(f"unknown faction in {args.factions!r}")
    if deal is not None and len(deal) < 2:
        deal = None  # one faction: nothing to deal
    if deal is not None and setup is not None and len(set(deal)) < len(rs.map.starts):
        setup = {**setup, "unique_factions": False}  # a dealt list shorter than the seats repeats factions
    if setup is not None:
        lines.append(f"(setup phase: artifacts {setup['artifacts']}, factions "
                     f"{'chosen by the bots' if deal is None else 'dealt by rotation'})")
    configs = args.config or list(DEFAULT_CONFIGS_4 if len(rs.map.starts) >= 4 else DEFAULT_CONFIGS)
    overall = Tally()
    per_config: dict[str, Tally] = {}
    data: dict[str, Any] = {}
    base_games: dict[tuple[str, int], tuple[dict[str, str], list[str]]] = {}    # for --scout-sweep
    for cfg in configs:
        kinds = [k.strip() for k in cfg.split(",")]
        unknown = [k for k in kinds if k not in BOT_TYPES]
        if unknown or len(kinds) > len(rs.map.starts):
            ap.error(f"bad config {cfg!r}")
        tally = Tally()
        rots = rotations(kinds)
        for i in range(args.games):
            kinds_i = rots[i % len(rots)]
            assigned = dealt_factions(deal, list(kinds_i), i // len(rots)) if deal else None
            g = play_game(rs, kinds_i, f"sim|{cfg}|{i}", setup, assigned)
            tally.add(rs, g)
            overall.add(rs, g)
            base_games[(cfg, i)] = (kinds_i, list(g["winners"]))
        lines += report(cfg, tally)
        data[cfg] = summary(tally)
        per_config[cfg] = tally
    played = overall.games
    probe: Tally | None = None
    if sc_.on(rs, "objectives") and not any("passive" in cfg.split(",") for cfg in configs):
        # the floor of easy objectives (SCORING.md 10): a passive seat in the first configuration, apart from ALL
        kinds = ["passive", *[k.strip() for k in configs[0].split(",")][1:]]
        probe = Tally()
        rots = rotations(kinds)
        for i in range(max(4, args.games // 4)):
            kinds_i = rots[i % len(rots)]
            assigned = dealt_factions(deal, list(kinds_i), i // len(rots)) if deal else None
            probe.add(rs, play_game(rs, kinds_i, f"sim|probe|{i}", setup, assigned))
        played += probe.games
        rates = {short(o): f"{100 * r.get('passive', 0):.0f}%" for o, r in probe.s4.objective_rates().items()}
        lines.append(f"== passive probe ({','.join(kinds)}, {probe.games} games): objectives met by passive "
                     + (", ".join(f"{o} {v}" for o, v in rates.items()) or "-"))
        data["passive_probe"] = summary(probe)
    scout = None
    if getattr(args, "scout_sweep", False) and rs.system("discoveries"):
        scout, part, n = scout_sweep(rs, args, configs, base_games, setup, deal)
        lines += part
        played += n
        data["scout_sweep"] = scout
    lines += report("ALL configurations", overall)
    if overall.f_seats["synclite"]:
        lines.append(f"  synclite: win/seat {_pct(overall.f_wins['synclite'], overall.f_seats['synclite']).strip()} "
                     f"over {overall.f_seats['synclite']} seats")
    data["ALL"] = summary(overall)
    rows = corridors(rs, overall, per_config, probe, land if not rs.default_scoring else None)
    lines += corridor_lines(rows)
    data["corridors"] = rows
    if overall.sv2.games:       # core 0.8.0 (docs/SPY_V2.md 10)
        win = data["ALL"]["win_per_seat"]
        gates = spy_gates(overall.sv2, win, spy_base(getattr(args, "spy_base", None)))
        lines.append("######## spy gates (docs/SPY_V2.md 10; bots: a miss is a bot bug)")
        lines += corridor_lines(gates)[1:]
        data["spy_gates"] = gates
    if overall.trd.games:       # core 0.8.0 (docs/TRADE.md 12.2)
        gates = trade_gates(rs, overall.trd, data["ALL"]["win_per_seat"], trade_base(getattr(args, "trade_base", None)))
        lines.append("######## trade gates (docs/TRADE.md 12.2)")
        lines += corridor_lines(gates)[1:]
        data["trade_gates"] = gates
    players = len([k for k in configs[0].split(",") if k.strip()]) if configs else 0
    return lines, data, played, {"overall": overall, "players": players, "scout": scout}


def spy_base(path: str | None) -> dict[str, float] | None:
    """The win share per bot type of a previous run (`--json-out`; its ALL tallies, or the first combination's)."""
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "ALL" not in data:
        data = next((v for v in data.values() if isinstance(v, dict) and "ALL" in v), {})
    return (data.get("ALL") or {}).get("win_per_seat")


def scout_sweep(rs: Ruleset, args: argparse.Namespace, configs: list[str],
                base_games: dict[tuple[str, int], tuple[dict[str, str], list[str]]], setup: dict[str, Any] | None,
                deal: list[str] | None) -> tuple[dict[str, Any] | None, list[str], int]:
    """Decision 25 a: every game of a configuration holding a builder is played again with that builder (the first
    by seat) as the scout bot, on the same seed, seats and factions. Returns ({games, scout_win, builder_win,
    scout_small_entries, entry_value} or None without such a configuration, text lines, games played)."""
    tally = Tally()
    control = Tally()
    wins = [0.0, 0.0, 0.0]          # scout, builder, control
    diffs: list[float] = []         # per game: the scout seat's win share minus the builder's on the same seed
    diffs_c: list[float] = []       # ... minus the control's (the scout's Militia, no sweep)
    for cfg in configs:
        kinds = [k.strip() for k in cfg.split(",")]
        if "builder" not in kinds:
            continue
        rots = rotations(kinds)
        for i in range(args.games):
            kinds_i, winners = base_games[(cfg, i)]
            seat = min(s for s, k in kinds_i.items() if k == "builder")
            assigned = dealt_factions(deal, list(kinds_i), i // len(rots)) if deal else None
            g = play_game(rs, {**kinds_i, seat: "scout"}, f"sim|{cfg}|{i}", setup, assigned)
            tally.add(rs, g)
            c = play_game(rs, {**kinds_i, seat: ScoutControlBot.name}, f"sim|{cfg}|{i}", setup, assigned)
            control.add(rs, c)
            got = (1 / len(g["winners"])) if seat in g["winners"] else 0.0
            base = (1 / len(winners)) if seat in winners else 0.0
            ctl = (1 / len(c["winners"])) if seat in c["winners"] else 0.0
            wins[0] += got
            wins[1] += base
            wins[2] += ctl
            diffs.append(got - base)
            diffs_c.append(got - ctl)
    n = tally.games
    if not n:
        return None, [], 0
    small = tally.s5.small.get("scout", [0, 0.0])
    se = statistics.stdev(diffs) / n ** 0.5 if n > 1 else 0.0
    se_c = statistics.stdev(diffs_c) / n ** 0.5 if n > 1 else 0.0
    out = {"games": n, "scout_win": wins[0] / n, "builder_win": wins[1] / n, "diff_se": se,
           "control_win": wins[2] / n, "control_diff_se": se_c,
           "scout_small_entries": small[0], "entry_value": small[1] / small[0] if small[0] else None}
    lines = report("scout sweep (the first builder replaced by the scout bot, same seeds)", tally)
    lines += report("scout control (the scout's Militia, no sweep; same seeds)", control)
    value = "-" if out["entry_value"] is None else f"{out['entry_value']:+.2f}"
    lines.append(f"  scout seat wins {100 * out['scout_win']:.1f}% vs the builder on the same seeds "
                 f"{100 * out['builder_win']:.1f}% ({n} games, standard error of the paired difference "
                 f"{100 * se:.1f} p.p.); the scout's 1-2 unit entries {small[0]}, value "
                 f"{value} P/entry")
    lines.append(f"  scout seat wins {100 * out['scout_win']:.1f}% vs the control (the same Militia, no sweep) "
                 f"{100 * out['control_win']:.1f}% (standard error of the paired difference {100 * se_c:.1f} p.p.)")
    return out, lines, 2 * n


if __name__ == "__main__":
    sys.exit(main())
