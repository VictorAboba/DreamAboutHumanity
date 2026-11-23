"""Game setup and turn resolution (pure functions: inputs are never mutated)."""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence, Union

from .combat import Force, fight, pick_losses, round_half_up, strength
from .content import Ruleset
from .model import GameState, Order, PendingBuild, Player, Province, SeatOrders
from .orders import executable_orders, order_cost
from .rng import stream

OrdersInput = Mapping[str, Union[SeatOrders, Sequence[Order]]]


class SetupError(ValueError):
    pass


# ---------------------------------------------------------------- setup

def new_game_state(ruleset: Ruleset, seats: Sequence[Mapping[str, Any]], seed: int | str) -> GameState:
    """Initial state. ``seats``: [{seat, nick, faction?}]; starts are dealt to seats by a seeded shuffle.

    Unused starts stay neutral and empty.
    """
    starts = list(ruleset.map.starts)
    if not 1 <= len(seats) <= len(starts):
        raise SetupError(f"map {ruleset.map.id} takes 1..{len(starts)} seats, got {len(seats)}")
    ids = [s["seat"] for s in seats]
    nicks = [s["nick"] for s in seats]
    if len(set(ids)) != len(ids) or len(set(nicks)) != len(nicks):
        raise SetupError("seat ids and nicks must be unique")
    stream(seed, 0, "setup", "starts").shuffle(starts)
    provinces = {p.id: Province(p.id, p.name, p.type, None) for p in ruleset.map.provinces.values()}
    players: dict[str, Player] = {}
    for seat_cfg, start in zip(seats, starts):
        seat = seat_cfg["seat"]
        fref = seat_cfg.get("faction") or ruleset.scenario.factions[0]
        faction_id = ruleset.resolve("faction", fref)
        if faction_id is None:
            raise SetupError(f"faction {fref!r} is not available in {ruleset.scenario.id}")
        faction = ruleset.factions[faction_id]
        for pid in (start.capital, *start.provinces):
            provinces[pid].owner = seat
        capital = provinces[start.capital]
        capital.units = dict(faction.start_units)
        capital.buildings = sorted(faction.start_buildings)
        resources = {r: faction.start_resources.get(r, 0) for r in ruleset.resources}
        player = Player(seat, seat_cfg["nick"], faction_id, resources,
                        command_points=int(ruleset.stat("command_points", faction_id)))
        for eff in faction.start_effects:
            _apply_start_effect(eff, player, capital)
        players[seat] = player
    return GameState(turn=1, phase="negotiation_1", provinces=provinces, players=dict(sorted(players.items())))


def _apply_start_effect(eff: Mapping[str, Any], player: Player, capital: Province) -> None:
    kind = eff["type"]
    if kind == "add_resource":
        player.resources[eff["resource"]] = player.resources.get(eff["resource"], 0) + eff["amount"]
    elif kind == "grant_units":
        capital.units[eff["unit"]] = capital.units.get(eff["unit"], 0) + eff["count"]
    elif kind == "grant_building" and eff["building"] not in capital.buildings:
        capital.buildings = sorted([*capital.buildings, eff["building"]])
    elif kind == "set_flag" and eff["flag"] not in player.flags:
        player.flags.append(eff["flag"])


# ---------------------------------------------------------------- economy helpers (also used by view)

def income_of(state: GameState, ruleset: Ruleset, seat: str) -> dict[str, int]:
    faction = state.players[seat].faction
    total: Counter[str] = Counter(ruleset.factions[faction].income)
    for prov in state.owned(seat):
        total.update(ruleset.province_types[prov.type].income)
        for b in prov.buildings:
            total.update(ruleset.buildings[b].income)
    return {r: round_half_up(total[r] * ruleset.stat(f"resource.{r}.income_mul", faction)) for r in ruleset.resources}


def upkeep_of(state: GameState, ruleset: Ruleset, seat: str) -> dict[str, int]:
    faction = state.players[seat].faction
    total: Counter[str] = Counter()
    for prov in state.owned(seat):
        for unit, n in prov.units.items():
            total.update({r: v * n for r, v in ruleset.unit_upkeep(unit, faction).items()})
    return {r: total[r] for r in ruleset.resources}


def scores(state: GameState, ruleset: Ruleset) -> dict[str, int]:
    """1 point per province, +3 per capital (own or captured)."""
    out = {seat: 0 for seat in state.players}
    for prov in state.provinces.values():
        if prov.owner in out:
            out[prov.owner] += 1 + (3 if prov.id in ruleset.map.capitals else 0)
    return out


def visible_provinces(state: GameState, ruleset: Ruleset, seat: str) -> set[str]:
    own = {p.id for p in state.owned(seat)}
    return own | {n for pid in own for n in ruleset.map.adjacency[pid]}


# ---------------------------------------------------------------- report

@dataclass
class TurnReport:
    turn: int
    events: list[dict[str, Any]] = field(default_factory=list)       # full (private) event list
    by_seat: dict[str, list[dict[str, Any]]] = field(default_factory=dict)   # what each seat may see
    public: dict[str, Any] = field(default_factory=dict)  # ownership_changes, eliminated, scores, result
    idle: dict[str, dict[str, Any]] = field(default_factory=dict)  # seat -> {resources:{}, units:n}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TurnReport:
        return cls(**{k: copy.deepcopy(v) for k, v in data.items()})


# ---------------------------------------------------------------- resolution

def resolve(state: GameState, orders_by_seat: OrdersInput, ruleset: Ruleset,
            seed: int | str) -> tuple[GameState, TurnReport]:
    """Resolve one turn. Orders are re-checked; invalid ones are dropped and reported."""
    r = _Resolution(copy.deepcopy(state), ruleset, seed)
    return r.run({seat: _book(v) for seat, v in orders_by_seat.items()})


def _book(value: SeatOrders | Sequence[Order]) -> SeatOrders:
    return value if isinstance(value, SeatOrders) else SeatOrders(list(value))


class _Resolution:
    def __init__(self, state: GameState, ruleset: Ruleset, seed: int | str):
        self.s = state
        self.rs = ruleset
        self.seed = seed
        self.turn = state.turn
        self.events: list[dict[str, Any]] = []
        self.factions: dict[str | None, str | None] = {seat: p.faction for seat, p in state.players.items()}
        self.retreats: list[tuple[str | None, list[str], dict[str, int], str]] = []

    def ev(self, kind: str, seats: Sequence[str | None] = (), province: str | None = None, **data: Any) -> None:
        self.events.append({"kind": kind, "seats": sorted(s for s in set(seats) if s), "province": province, **data})

    def rng(self, system: str, entity: str):
        return stream(self.seed, self.turn, system, entity)

    def run(self, books: dict[str, SeatOrders]) -> tuple[GameState, TurnReport]:
        s = self.s
        alive = [seat for seat, p in s.players.items() if p.alive]
        seen_before = {seat: visible_provinces(s, self.rs, seat) for seat in alive}
        moves, fortify = self.apply_orders(alive, books)
        idle = self.idle_stats(alive, moves, fortify)
        arrivals, fortified = self.move(moves, fortify)
        owners_before = {pid: p.owner for pid, p in s.provinces.items()}
        for pid in sorted(s.provinces):
            self.battle(pid, arrivals.get(pid, []), pid in fortified)
        self.apply_retreats()
        for pid, prov in s.provinces.items():
            prov.fortified = pid in fortified
        changes = [{"province": pid, "from": owners_before[pid], "to": p.owner}
                   for pid, p in sorted(s.provinces.items()) if p.owner != owners_before[pid]]
        for c in changes:
            self.ev("captured", [c["from"], c["to"]], c["province"], **{"from": c["from"], "to": c["to"]})
        s.pending_builds = [b for b in s.pending_builds if s.provinces[b.province].owner == b.seat]
        self.economy(alive)
        self.builds()
        eliminated = self.eliminate(alive)
        sc = scores(s, self.rs)
        self.finish(sc)
        seen_after = {seat: visible_provinces(s, self.rs, seat) for seat in alive}
        by_seat = {seat: [e for e in self.events
                          if seat in e["seats"] or (_seen_at(e) in seen_before[seat] | seen_after[seat]
                                                    and e["kind"] not in _PRIVATE_KINDS)]
                   for seat in alive}
        report = TurnReport(self.turn, self.events, by_seat,
                            {"ownership_changes": changes, "eliminated": eliminated, "scores": sc,
                             "result": s.result}, idle)
        return s, report

    # 1. orders: reserves, recruits, builds
    def apply_orders(self, alive: list[str], books: dict[str, SeatOrders]) -> tuple[list[tuple[str, Order]], set[str]]:
        moves: list[tuple[str, Order]] = []
        fortify: set[str] = set()
        start = copy.deepcopy(self.s)
        for seat in alive:
            book = books.get(seat, SeatOrders())
            valid, rejected = executable_orders(start, self.rs, seat, book)
            for order, reason in rejected:
                self.ev("order_rejected", [seat], None, order=order.to_dict(), reason=reason)
            player = self.s.players[seat]
            for res, n in book.forfeit.items():
                player.resources[res] = max(0, player.resources.get(res, 0) - n)
            for o in valid:
                for res, n in order_cost(self.rs, player.faction, o).items():
                    player.resources[res] -= n
                if o.kind == "move":
                    moves.append((seat, o))
                elif o.kind == "fortify":
                    fortify.add(o.province)
                elif o.kind == "recruit":
                    prov = self.s.provinces[o.province]
                    prov.units[o.unit] = prov.units.get(o.unit, 0) + o.count
                    self.ev("recruited", [seat], o.province, unit=o.unit, count=o.count)
                else:
                    turns = self.rs.buildings[o.building].build_turns
                    self.s.pending_builds.append(PendingBuild(seat, o.province, o.building, turns))
                    self.ev("build_started", [seat], o.province, building=o.building, turns=turns)
        return moves, fortify

    def idle_stats(self, alive: list[str], moves: list[tuple[str, Order]], fortify: set[str]) -> dict[str, dict]:
        moving: Counter[str] = Counter()
        for seat, o in moves:
            moving[seat] += sum((o.units or {}).values())
        out = {}
        for seat in alive:
            units = sum(p.unit_total() for p in self.s.owned(seat) if p.id not in fortify)
            out[seat] = {"resources": dict(self.s.players[seat].resources), "units": max(0, units - moving[seat])}
        return out

    # 2. movement and head-on battles
    def move(self, moves: list[tuple[str, Order]], fortify: set[str]) -> tuple[dict[str, list[Force]], set[str]]:
        edges: dict[tuple[str, str], Force] = {}
        for seat, o in moves:
            src = self.s.provinces[o.src]
            for unit, n in (o.units or {}).items():
                src.units[unit] -= n
                if not src.units[unit]:
                    del src.units[unit]
            force = Force(seat, dict(o.units or {}), dict(o.units or {}) if o.hasty else {}, origin=o.src)
            key = (o.src, o.dst)
            if key in edges:
                edges[key].merge(force)
            else:
                edges[key] = force
        leaving = {src for src, _ in edges}
        fortified = set()
        for pid in sorted(fortify):
            if pid in leaving:
                self.ev("fortify_broken", [self.s.provinces[pid].owner], pid)
            else:
                fortified.add(pid)
        for (a, b) in sorted(edges):
            if a < b and (b, a) in edges and edges[(a, b)].owner != edges[(b, a)].owner:
                self.edge_battle(edges[(a, b)], edges[(b, a)], a, b)
        arrivals: dict[str, list[Force]] = {}
        for (_, dst), force in sorted(edges.items()):
            if force.total():
                arrivals.setdefault(dst, []).append(force)
        return arrivals, fortified

    def edge_battle(self, ab: Force, ba: Force, a: str, b: str) -> None:
        before = (dict(ab.units), dict(ba.units))
        res = fight(self.rs, ab, ba, self.rng("combat", f"edge:{a}-{b}"), b_defending=False, factions=self.factions)
        loser = ba if res.a_won else ab
        self.ev("edge_battle", [ab.owner, ba.owner], None, edge=[a, b],
                sides=[_side(ab, before[0], res.losses_a, res.a_won, res.strength_a),
                       _side(ba, before[1], res.losses_b, not res.a_won, res.strength_b)])
        for origin_id, units in sorted(loser.parts.items()):
            origin = self.s.provinces[origin_id]
            for unit, n in units.items():
                origin.units[unit] = origin.units.get(unit, 0) + n
            self.ev("retreat", [loser.owner], origin_id, units=dict(units), **{"from": f"{a}-{b}"})
        loser.clear()

    # 3-4. province battles and ownership
    def battle(self, pid: str, incoming: list[Force], fortified: bool) -> None:
        prov = self.s.provinces[pid]
        if not incoming:
            return
        defender = Force(prov.owner, dict(prov.units))
        by_owner: dict[str | None, Force] = {}
        for f in incoming:
            if f.owner == prov.owner:
                defender.merge(Force(f.owner, dict(f.units)))
            elif f.owner in by_owner:
                by_owner[f.owner].merge(f)
            else:
                by_owner[f.owner] = copy.deepcopy(f)
        if not by_owner:
            prov.units = defender.units
            return
        rng = self.rng("combat", pid)
        attackers = sorted(by_owner.values(), key=lambda f: f.owner or "")
        ties = {f.owner: rng.random() for f in attackers}
        attackers.sort(key=lambda f: (-strength(self.rs, f, {}, defending=False, faction=self.factions.get(f.owner)),
                                      ties[f.owner]))
        champion = attackers[0]
        for challenger in attackers[1:]:
            snap = (dict(champion.units), dict(challenger.units))
            res = fight(self.rs, champion, challenger, rng, b_defending=False, factions=self.factions)
            self.ev("battle", [champion.owner, challenger.owner], pid, stage="attackers",
                    sides=[_side(champion, snap[0], res.losses_a, res.a_won, res.strength_a),
                           _side(challenger, snap[1], res.losses_b, not res.a_won, res.strength_b)])
            loser = challenger if res.a_won else champion
            champion = champion if res.a_won else challenger
            self.retreat_home(loser, pid)
        if not champion.total():
            prov.units = defender.units
            return
        if defender.total():
            snap = (dict(champion.units), dict(defender.units))
            res = fight(self.rs, champion, defender, rng, b_defending=True, factions=self.factions,
                        defense_mul=self.rs.province_types[prov.type].defense_mul, fortified=fortified)
            self.ev("battle", [champion.owner, defender.owner], pid, stage="defense", fortified=fortified,
                    sides=[_side(champion, snap[0], res.losses_a, res.a_won, res.strength_a),
                           _side(defender, snap[1], res.losses_b, not res.a_won, res.strength_b)])
            if not res.a_won:
                self.retreat_home(champion, pid)
                prov.units = defender.units
                return
            if defender.owner is not None:
                self.retreat(defender, [n for n in self.rs.map.adjacency[pid]], pid)
            elif defender.total():
                self.ev("destroyed", [], pid, units=dict(defender.units))
        prov.owner = champion.owner
        prov.units = champion.units

    def retreat(self, force: Force, candidates: list[str], from_pid: str) -> None:
        if force.total():
            self.retreats.append((force.owner, candidates, dict(force.units), from_pid))

    def retreat_home(self, attacker: Force, from_pid: str) -> None:
        """A losing attacker's survivors go back to the provinces they came from, each part to its own."""
        for origin, units in sorted(attacker.parts.items()):
            self.retreats.append((attacker.owner, [origin], dict(units), from_pid))

    def apply_retreats(self) -> None:
        for owner, candidates, units, from_pid in self.retreats:
            target = next((c for c in sorted(candidates) if self.s.provinces[c].owner == owner), None)
            if target is None:
                self.ev("retreat_lost", [owner], from_pid, units=units)
                continue
            prov = self.s.provinces[target]
            for unit, n in units.items():
                prov.units[unit] = prov.units.get(unit, 0) + n
            self.ev("retreat", [owner], target, units=units, **{"from": from_pid})

    # 5. economy and starvation
    def economy(self, alive: list[str]) -> None:
        food = self.rs.roles["upkeep_food"]
        for seat in alive:
            player = self.s.players[seat]
            income = income_of(self.s, self.rs, seat)
            upkeep = upkeep_of(self.s, self.rs, seat)
            for res in self.rs.resources:
                player.resources[res] = player.resources.get(res, 0) + income[res] - upkeep[res]
            self.ev("economy", [seat], None, income=income, upkeep=upkeep)
            if player.resources[food] < 0:
                self.starve(seat)
            for res in self.rs.resources:
                player.resources[res] = max(0, player.resources[res])

    def starve(self, seat: str) -> None:
        owned = self.s.owned(seat)
        total: Counter[str] = Counter()
        for prov in owned:
            total.update(prov.units)
        n = sum(total.values())
        frac = self.rs.stat("starvation.loss_frac", self.s.players[seat].faction)
        losses = pick_losses(self.rs, total, min(n, max(1, round_half_up(frac * n)))) if n else {}
        for unit, k in losses.items():
            for prov in owned:
                take = min(k, prov.units.get(unit, 0))
                if take:
                    prov.units[unit] -= take
                    if not prov.units[unit]:
                        del prov.units[unit]
                    k -= take
        self.ev("starvation", [seat], None, losses=losses)

    # 6. construction
    def builds(self) -> None:
        still = []
        for b in self.s.pending_builds:
            b.turns_left -= 1
            if b.turns_left > 0:
                still.append(b)
                continue
            prov = self.s.provinces[b.province]
            prov.buildings = sorted({*prov.buildings, b.building})
            self.ev("build_done", [b.seat], b.province, building=b.building)
        self.s.pending_builds = still

    # 7. elimination and victory
    def eliminate(self, alive: list[str]) -> list[str]:
        out = []
        for seat in alive:
            if not self.s.owned(seat):
                self.s.players[seat].alive = False
                out.append(seat)
                self.ev("eliminated", [seat], None, seat=seat)
        return out

    def finish(self, sc: dict[str, int]) -> None:
        s = self.s
        alive = [seat for seat, p in s.players.items() if p.alive]
        if len(alive) <= 1 and len(s.players) > 1:
            s.result = {"winners": alive, "reason": "last_standing", "scores": sc}
        elif self.turn >= self.rs.scenario.turn_limit:
            best = max((sc[seat] for seat in alive), default=0)
            s.result = {"winners": [seat for seat in alive if sc[seat] == best], "reason": "turn_limit", "scores": sc}
        if s.result:
            s.phase = "game_over"
        else:
            s.turn += 1
            s.phase = "negotiation_1"
        for p in s.players.values():
            p.command_points = int(self.rs.stat("command_points", p.faction)) if p.alive else 0


# Events only their seats see: they would reveal resources or orders (fortify_broken reveals a fortify order).
_PRIVATE_KINDS = frozenset({"order_rejected", "economy", "starvation", "build_started", "recruited",
                            "fortify_broken"})


def _seen_at(event: Mapping[str, Any]) -> str | None:
    """Province whose visibility decides who else sees the event: a retreat is seen where its battle was."""
    return event.get("from") if event["kind"] == "retreat" else event["province"]


def _side(force: Force, before: dict[str, int], losses: dict[str, int], won: bool, power: float) -> dict[str, Any]:
    return {"owner": force.owner, "units": before, "losses": losses, "won": won, "strength": round(power, 3)}
