"""Balance simulator: bots play many games against each other in-process (the engine directly, no server).

    uv run python scripts/simulate.py                      # every configuration, 200 games each
    uv run python scripts/simulate.py --games 50 --config rush,builder,diplomat
    uv run python scripts/simulate.py --combat             # only the combat table (estimate by army ratio)
    uv run python scripts/simulate.py --packs path/to/packs_dir   # another copy of the packs (e.g. an older core)

The bots are the real ones from `dah.bots` (the same decision code that plays over HTTP); an in-process adapter
stands in for the server API: state views, orders, estimate, gifts and pacts go through the engine functions the
server uses. Each configuration is played with the bot types rotated over the seats (and the starts dealt by the
seed), so no type keeps one seat. Output: plain-text tables (or --json).
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
from dah.engine import (
    SeatOrders,
    build_ruleset,
    cp_left,
    default_pack_dirs,
    estimate,
    new_game_state,
    orders_hash,
    reserved_resources,
    resolve,
    submit_orders,
    view_state,
)
from dah.engine import diplomacy as dip
from dah.engine.content import Ruleset
from dah.engine.model import GameState

PHASES = ("negotiation_1", "actions", "negotiation_2")
SEATS = ("A", "B", "C")
DEFAULT_CONFIGS = (
    "rush,builder,diplomat",
    "rush,rush,builder", "builder,builder,rush",
    "rush,rush,diplomat", "diplomat,diplomat,rush",
    "builder,builder,diplomat", "diplomat,diplomat,builder",
    "rush,rush,rush", "builder,builder,builder", "diplomat,diplomat,diplomat",
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

    def ready(self, confirm_hash: str) -> dict[str, Any]:
        return {}

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
        reserved = reserved_resources(rs, s.players[self.seat].faction, self._book())
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

def play_game(rs: Ruleset, kinds: dict[str, str], seed: str) -> dict[str, Any]:
    """Play one game; returns the raw facts the tables are built from."""
    seats = [{"seat": seat, "nick": f"{kind}-{seat}"} for seat, kind in sorted(kinds.items())]
    m = Match(rs, seats, seed)
    bots: dict[str, Bot] = {seat: BOT_TYPES[kind](seat, seat_seed(seed, seat))
                            for seat, kind in kinds.items()}
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
        m.state, report = resolve(m.state, {s: m.books.get(s, SeatOrders()) for s in alive}, rs, seed, m.ops)
        for e in report.events:
            events.append({**e, "turn": turn})
        scores_by_turn[turn] = dict(report.public["scores"])
    result = m.state.result or {}
    return {"kinds": kinds, "winners": result.get("winners", []), "scores": result.get("scores", {}),
            "events": events, "scores_by_turn": scores_by_turn, "turns": max(scores_by_turn),
            "resources": {s: dict(p.resources) for s, p in m.state.players.items()},
            "alive": {s: p.alive for s, p in m.state.players.items()}}


# ---------------------------------------------------------------- statistics

def nominal_ratio(rs: Ruleset, attacker: dict[str, int], defender: dict[str, int]) -> float:
    att = sum(rs.stat(f"unit.{u}.attack") * n for u, n in attacker.items())
    dfn = sum(rs.stat(f"unit.{u}.defense") * n for u, n in defender.items())
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
                    ratio = nominal_ratio(rs, att["units"], dfn["units"])
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
    for table, items in ((bv.UNIT_COST_ALLOY, {u: rs.unit_cost(u).get(build, 0) for u in rs.units}),
                         (bv.UNIT_UPKEEP_FOOD, {u: rs.unit_upkeep(u).get(food, 0) for u in rs.units}),
                         (bv.BUILDING_COST_ALLOY, {b: rs.building_cost(b).get(build, 0) for b in rs.buildings})):
        for full, value in items.items():
            short = full.split(":")[-1]
            if table.get(short) != value:
                changed.append(f"{short} {table.get(short)} -> {value}")
            table[short] = value
    return changed


def rotations(kinds: list[str]) -> list[dict[str, str]]:
    perms = sorted(set(itertools.permutations(kinds)))
    return [dict(zip(SEATS, p)) for p in perms]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--games", type=int, default=200, help="games per configuration (default 200)")
    ap.add_argument("--config", action="append", help="comma-separated bot types for seats, e.g. rush,builder,"
                                                      "diplomat (repeatable; default: a fixed set)")
    ap.add_argument("--scenario", default="core:slice3")
    ap.add_argument("--packs", help="directory holding pack directories (default: content/packs of the repo)")
    ap.add_argument("--combat", action="store_true", help="print only the combat table")
    ap.add_argument("--json", action="store_true", help="print the tallies as JSON")
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
    configs = args.config or list(DEFAULT_CONFIGS)
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
            g = play_game(rs, rots[i % len(rots)], f"sim|{cfg}|{i}")
            tally.add(rs, g)
            overall.add(rs, g)
        lines += report(cfg, tally)
        data[cfg] = {"games": tally.games, "win_per_seat": {k: tally.win_share[k] / tally.seats[k]
                                                             for k in tally.seats}}
    lines += report("ALL configurations", overall)
    lines.append(f"({overall.games} games in {time.monotonic() - started:.0f} s)")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
