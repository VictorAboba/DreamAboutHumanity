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
    scores_by_turn: dict[int, dict[str, int]] = {}
    while m.state.phase != "game_over":
        m.books, m.ops = {}, []
        for phase in PHASES:
            m.state.phase = phase
            m.counts.clear()
            for seat in sorted(kinds):
                if m.state.players[seat].alive:
                    bots[seat].play_phase(apis[seat], phase, {seat, m.state.players[seat].nick})
        turn = m.state.turn
        m.state.phase = "resolution"
        alive = [s for s, p in m.state.players.items() if p.alive]
        keys = {s: stat_key(m.state, rs, s) for s in alive}  # the stats each seat fights with this turn
        m.state, report = resolve(m.state, {s: m.books.get(s, SeatOrders()) for s in alive}, rs, seed, m.ops)
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
            "techs": {s: list(p.techs) for s, p in m.state.players.items()}}


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
            elif kind in ("gift", "pact_proposed", "pact_signed", "pact_broken", "pact_bounce", "pact_joint"):
                self.diplo[kind] += 1
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
            "techs_per_seat": {k: statistics.mean(v) for k, v in t.techs.items() if v}}


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
    ap.add_argument("--json", action="store_true", help="print the tallies as JSON")
    ap.add_argument("--json-out", help="also write the JSON tallies to this file")
    args = ap.parse_args(argv)
    pack_dirs = None
    if args.packs:
        root = Path(args.packs)
        pack_dirs = sorted((p for p in root.iterdir() if (p / "pack.yaml").is_file()), key=lambda p: p.name != "core")
    rs = build_ruleset(pack_dirs or default_pack_dirs(), args.scenario)
    changed = sync_bot_numbers(rs)
    lines = combat_table(rs)
    if changed:
        lines.append("(bots plan with this pack's numbers, not their built-in ones: " + ", ".join(changed) + ")")
    if args.combat:
        print("\n".join(lines))
        return 0
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
    started = time.monotonic()
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
    lines.append(f"({overall.games} games in {time.monotonic() - started:.0f} s)")
    data["ALL"] = summary(overall)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
