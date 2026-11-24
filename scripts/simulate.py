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
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dah.bots import BOT_TYPES
from dah.bots.base import Bot, seat_seed
from dah.bots.client import ApiError
from dah.bots.choices import pick_artifacts
from dah.engine import (
    SeatOrders,
    SetupChoiceError,
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
    seat_reserved,
    setup_config,
    stat_key,
    submit_orders,
    techs_view,
    upkeep_of,
    view_state,
)
from dah.engine import diplomacy as dip
from dah.engine import espionage as esp
from dah.engine.content import Ruleset
from dah.engine.model import GameState
from dah.engine.setup import artifact_choices, faction_choices

PHASES = ("negotiation_1", "actions", "negotiation_2")
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

    def send_message(self, to: list[str] | str, text: str) -> dict[str, Any]:
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
        return self._apply("gifts", lambda: dip.make_gift(s, rs, self.seat, to, resources, s.phase, reserved))

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


def play_game(rs: Ruleset, kinds: dict[str, str], seed: str, setup: dict[str, Any] | None = None,
              assigned: dict[str, str] | None = None) -> dict[str, Any]:
    """Play one game; returns the raw facts the tables are built from. `setup`: the game's setup config (a setup
    phase is played); `assigned`: seat -> faction (dealt, not chosen)."""
    bots: dict[str, Bot] = {seat: BOT_TYPES[kind](seat, seat_seed(seed, seat))
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
    events: list[dict[str, Any]] = []
    recons: list[dict[str, Any]] = []
    feints_real: list[tuple[int, str, str]] = []   # (turn, seat, order id) of false entries made real in P2
    occupied_turns = 0
    scores_by_turn: dict[int, dict[str, int]] = {}
    espionage = rs.system("espionage")
    while m.state.phase != "game_over":
        m.books, m.ops = {}, []
        for phase in PHASES:
            if phase == "negotiation_2" and espionage:   # the recon step at the close of actions
                m.state.phase = "actions"
                m.state, m.recon = esp.recon(m.state, m.books, rs, seed)
                recons.append(m.recon)
            m.state.phase = phase
            m.counts.clear()
            for seat in sorted(kinds):
                if m.state.players[seat].alive:
                    bots[seat].play_phase(apis[seat], phase, {seat, m.state.players[seat].nick})
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
        m.state, report = resolve(m.state, books, rs, seed, m.ops)
        occupied_turns += sum(1 for p in m.state.provinces.values() if (p.status or {}).get("occupied"))
        events.append({"kind": "_p2_moves", "moves": p2_moves, "turn": turn, "seats": []})
        for e in report.events:
            if e["kind"] == "battle":
                e = {**e, "keys": [keys.get(side["owner"]) for side in e["sides"]]}
            events.append({**e, "turn": turn})
        scores_by_turn[turn] = dict(report.public["scores"])
    result = m.state.result or {}
    return {"kinds": kinds, "winners": result.get("winners", []), "scores": result.get("scores", {}),
            "events": events, "scores_by_turn": scores_by_turn, "turns": max(scores_by_turn),
            "resources": {s: dict(p.resources) for s, p in m.state.players.items()},
            "alive": {s: p.alive for s, p in m.state.players.items()},
            "factions": {s: p.faction for s, p in m.state.players.items()},
            "artifacts": {s: list(p.artifacts) for s, p in m.state.players.items()},
            "techs": {s: list(p.techs) for s, p in m.state.players.items()},
            "recons": recons, "feints_real": feints_real, "occupied_turns": occupied_turns,
            "shades_end": {s: len(p.shades) for s, p in m.state.players.items()}}


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
            "stage3": t.s3.summary()}


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
    combos = switch_combos(base, args.espionage, args.conquest)
    started = time.monotonic()
    games = 0
    for combo in combos:
        rs = build_ruleset(packs, args.scenario, systems=combo) if len(combos) > 1 or any(
            v is not None for v in (args.espionage, args.conquest)) else base
        label = ",".join(f"{k}={'on' if rs.system(k) else 'off'}" for k in ("espionage", "conquest"))
        if len(combos) > 1:
            lines.append(f"######## {label}")
        part, out, n = run_combo(ap, args, rs)
        lines += part
        games += n
        data[label] = out
    lines.append(f"({games} games in {time.monotonic() - started:.0f} s)")
    if len(combos) == 1:
        data = next(iter(data.values()))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print("\n".join(lines))
    return 0


def switch_combos(rs: Ruleset, espionage: str | None, conquest: str | None) -> list[dict[str, bool]]:
    """The switch combinations to play: each of `on|off|both` (None: the scenario's value)."""
    def values(name: str, arg: str | None) -> list[bool]:
        if arg == "both":
            return [True, False]
        if arg is None:
            return [rs.system(name)]
        return [arg == "on"]

    return [{"espionage": e, "conquest": c} for e in values("espionage", espionage)
            for c in values("conquest", conquest)]


def run_combo(ap: argparse.ArgumentParser, args: argparse.Namespace, rs: Ruleset) -> tuple[list[str], dict, int]:
    """Every configuration of one switch combination: (text lines, JSON tallies, games played)."""
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
    data: dict[str, Any] = {}
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
        lines += report(cfg, tally)
        data[cfg] = summary(tally)
    lines += report("ALL configurations", overall)
    if overall.f_seats["synclite"]:
        lines.append(f"  synclite: win/seat {_pct(overall.f_wins['synclite'], overall.f_seats['synclite']).strip()} "
                     f"over {overall.f_seats['synclite']} seats")
    data["ALL"] = summary(overall)
    return lines, data, overall.games


if __name__ == "__main__":
    sys.exit(main())
