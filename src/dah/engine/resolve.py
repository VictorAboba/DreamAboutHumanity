"""Game setup and turn resolution (pure functions: inputs are never mutated)."""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence, Union

from . import diplomacy as dip
from .combat import Force, fight, pick_losses, round_half_up, strength
from .content import Ruleset, StatKey
from .model import BREAK, GameState, Order, PendingBuild, Player, Province, SeatOrders
from .orders import executable_orders, order_cost
from .rng import stream
from .techs import (grant_tech, province_defense_mul, province_stat, research_complete, research_transfer,
                    stat_key, tech_counts)

OrdersInput = Mapping[str, Union[SeatOrders, Sequence[Order]]]


class SetupError(ValueError):
    pass


# ---------------------------------------------------------------- setup

def new_game_state(ruleset: Ruleset, seats: Sequence[Mapping[str, Any]], seed: int | str) -> GameState:
    """Initial state. ``seats``: [{seat, nick, faction?, artifacts?}]; starts are dealt to seats by a seeded
    shuffle (independent of factions and artifacts, so a setup phase never moves a start).

    Unused starts stay neutral and empty. Artifacts (full ids, from the setup phase) apply their one-off effects now
    (resources, units and buildings in the capital, flags, techs); their passive effects count through the seat's
    stat key for the whole game.
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
            _apply_start_effect(eff, player, capital, ruleset)
        for art in seat_cfg.get("artifacts") or ():
            if art not in ruleset.artifacts:
                raise SetupError(f"artifact {art!r} is not in the ruleset")
            if art not in player.artifacts:
                player.artifacts.append(art)
                for eff in ruleset.artifacts[art].effects:
                    _apply_start_effect(eff, player, capital, ruleset)
        players[seat] = player
    state = GameState(turn=1, phase="negotiation_1", provinces=provinces, players=dict(sorted(players.items())))
    for seat, player in state.players.items():
        player.command_points = int(ruleset.stat("command_points", stat_key(state, ruleset, seat)))
    return state


def _apply_start_effect(eff: Mapping[str, Any], player: Player, capital: Province,
                        ruleset: Ruleset | None = None) -> None:
    kind = eff["type"]
    if kind == "grant_tech" and ruleset is not None:
        grant_tech(ruleset, player, eff["tech"])
    if kind == "add_resource":
        player.resources[eff["resource"]] = player.resources.get(eff["resource"], 0) + eff["amount"]
    elif kind == "grant_units":
        capital.units[eff["unit"]] = capital.units.get(eff["unit"], 0) + eff["count"]
    elif kind == "grant_building" and eff["building"] not in capital.buildings:
        capital.buildings = sorted([*capital.buildings, eff["building"]])
    elif kind == "set_flag" and eff["flag"] not in player.flags:
        player.flags.append(eff["flag"])


# ---------------------------------------------------------------- economy helpers (also used by view)

def income_of(state: GameState, ruleset: Ruleset, seat: str, key: StatKey = ...) -> dict[str, int]:
    """Income per turn: flat faction/tech/artifact income plus, per own province, (type + buildings) x
    `province.income_mul.<res>`; all x `resource.<res>.income_mul`, rounded half up."""
    if key is ...:
        key = stat_key(state, ruleset, seat)
    total: dict[str, float] = {r: float(n) for r, n in ruleset.passive_income(key).items()}
    for prov in state.owned(seat):
        here: Counter[str] = Counter(ruleset.province_types[prov.type].income)
        for b in prov.buildings:
            here.update(ruleset.buildings[b].income)
        for r, n in here.items():
            mul = province_stat(state, ruleset, prov.id, f"province.income_mul.{r}", key)
            total[r] = total.get(r, 0.0) + (n if mul == 1.0 else n * mul)
    return {r: round_half_up(total.get(r, 0) * ruleset.stat(f"resource.{r}.income_mul", key))
            for r in ruleset.resources}


def upkeep_of(state: GameState, ruleset: Ruleset, seat: str, key: StatKey = ...) -> dict[str, int]:
    faction = stat_key(state, ruleset, seat) if key is ... else key
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
            seed: int | str, diplomacy: Sequence[Mapping[str, Any]] = ()) -> tuple[GameState, TurnReport]:
    """Resolve one turn. Orders are re-checked; invalid ones are dropped and reported.

    ``diplomacy``: the turn's diplomacy ops, already applied to ``state`` (engine/diplomacy.py); they only go into
    the report (gifts and signed pacts are public facts of the turn, amounts and ids stay with the two seats).
    """
    r = _Resolution(copy.deepcopy(state), ruleset, seed)
    return r.run({seat: _book(v) for seat, v in orders_by_seat.items()}, diplomacy)


def _book(value: SeatOrders | Sequence[Order]) -> SeatOrders:
    return value if isinstance(value, SeatOrders) else SeatOrders(list(value))


class _Resolution:
    def __init__(self, state: GameState, ruleset: Ruleset, seed: int | str):
        self.s = state
        self.rs = ruleset
        self.seed = seed
        self.turn = state.turn
        self.events: list[dict[str, Any]] = []
        # each seat's stat key at the start of the resolution (techs researched now count from the next turn)
        self.factions: dict[str | None, StatKey] = {seat: stat_key(state, ruleset, seat) for seat in state.players}
        self.retreats: list[tuple[str | None, list[str], dict[str, int], str]] = []
        self.pacts: dict[frozenset[str], dict[str, Any]] = {}     # pacts in force in this resolution, by pair
        self.broken: dict[frozenset[str], set[str]] = {}          # pair -> seats whose "break" orders broke it
        self.break_targets: list[set[str]] = []                   # per move (in `moves` order): partners it breaks

    def shielded(self, force: Force, other: str | None, other_force: Force | None = None) -> bool:
        """`force` and the seat `other` (or its force) do not fight: a pact is in force between them and neither
        side's force here breaks it. A breach is aimed: only the breaking move fights; the victim's own moves into
        the breaker's provinces still bounce, and the pact protects every other meeting of the two seats."""
        x = force.owner
        if x is None or other is None or x == other or frozenset((x, other)) not in self.pacts:
            return False
        return other not in force.breaks and (other_force is None or x not in other_force.breaks)

    def ev(self, kind: str, seats: Sequence[str | None] = (), province: str | None = None, **data: Any) -> None:
        self.events.append({"kind": kind, "seats": sorted(s for s in set(seats) if s), "province": province, **data})

    def rng(self, system: str, entity: str):
        return stream(self.seed, self.turn, system, entity)

    def run(self, books: dict[str, SeatOrders],
            diplomacy: Sequence[Mapping[str, Any]] = ()) -> tuple[GameState, TurnReport]:
        s = self.s
        alive = [seat for seat, p in s.players.items() if p.alive]
        seen_before = {seat: visible_provinces(s, self.rs, seat) for seat in alive}
        for op in diplomacy:
            event = dip.op_event(op, s)
            if event is not None:
                self.events.append(event)
        self.start_pacts()
        moves, fortify = self.apply_orders(alive, books)
        self.find_breaks(moves)
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
        self.research(alive)
        eliminated = self.eliminate(alive)
        self.end_pacts(eliminated)
        sc = scores(s, self.rs)
        self.finish(sc)
        seen_after = {seat: visible_provinces(s, self.rs, seat) for seat in alive}
        by_seat = {seat: [_as_seen(e, seat) for e in self.events
                          if seat in e["seats"] or e["kind"] in dip.PUBLIC_KINDS
                          or (_seen_at(e) in seen_before[seat] | seen_after[seat]
                              and e["kind"] not in _PRIVATE_KINDS)]
                   for seat in alive}
        public_diplomacy = [dip.public_event(e) for e in self.events if e["kind"] in dip.PUBLIC_KINDS]
        public = {"ownership_changes": changes, "eliminated": eliminated, "scores": sc, "result": s.result,
                  "diplomacy": public_diplomacy}
        if self.rs.techs:
            public["techs_count"] = tech_counts(s, self.rs)   # public: how many (not which; no artifact grants)
        report = TurnReport(self.turn, self.events, by_seat, public, idle)
        return s, report

    # 0. pacts: notices to leave take effect; the pacts in force; who breaks one with an explicit "break" order
    def start_pacts(self) -> None:
        for pact in dip.active_pacts(self.s):
            if not dip.in_force(pact, self.turn):
                pact.update(status=dip.LEFT, ended_turn=self.turn, ended_reason="left")
                self.ev("pact_ended", [pact["a"], pact["b"]], None, pact=pact["id"], reason="left",
                        seat=pact.get("leaving"))
        self.pacts = {dip.pair_of(p): p for p in dip.active_pacts(self.s)}

    def find_breaks(self, moves: list[tuple[str, Order]]) -> None:
        """A "break" move is aimed at one seat: the owner of its target at the start of resolution (this covers a
        head-on meeting on an edge too); only when the target has no owner, at the partners whose moves go into the
        same province. If that seat is a pact partner, the pact is broken: this move fights it as without a pact,
        and the pact ends after the resolution. Other meetings of the two seats in this resolution stay protected."""
        self.break_targets = [set() for _ in moves]
        if not self.pacts:
            return
        owner = {pid: p.owner for pid, p in self.s.provinces.items()}
        for i, (seat, o) in enumerate(moves):
            if o.pact != BREAK:
                continue
            target = owner.get(o.dst)
            others = {target} if target is not None else {s2 for s2, o2 in moves if o2.dst == o.dst}
            for other in sorted(x for x in others if x and x != seat):
                pair = frozenset((seat, other))
                if pair in self.pacts:
                    self.broken.setdefault(pair, set()).add(seat)
                    self.break_targets[i].add(other)

    def end_pacts(self, eliminated: list[str]) -> None:
        for pair, breakers in sorted(self.broken.items(), key=lambda kv: self.pacts[kv[0]]["id"]):
            pact = self.pacts[pair]
            pact.update(status=dip.BROKEN, ended_turn=self.turn, ended_reason="broken", broken_by=sorted(breakers))
            self.ev("pact_broken", sorted(pair), None, pact=pact["id"], broken_by=sorted(breakers))
        for pact in dip.active_pacts(self.s):
            gone = sorted(x for x in (pact["a"], pact["b"]) if x in eliminated)
            if gone:
                pact.update(status=dip.ENDED, ended_turn=self.turn, ended_reason="eliminated")
                self.ev("pact_ended", [pact["a"], pact["b"]], None, pact=pact["id"], reason="eliminated",
                        seat=gone[0])
            elif pact["until_turn"] is not None and pact["until_turn"] <= self.turn:
                pact.update(status=dip.EXPIRED, ended_turn=self.turn, ended_reason="expired")
                self.ev("pact_expired", [pact["a"], pact["b"]], None, pact=pact["id"])
        for pact in self.s.pacts:
            if pact["status"] == dip.PROPOSED:
                pact.update(status=dip.LAPSED, ended_turn=self.turn)
                self.ev("pact_lapsed", [pact["a"], pact["b"]], None, pact=pact["id"])

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
                for res, n in order_cost(self.rs, self.factions[seat], o, start).items():
                    player.resources[res] -= n
                if o.kind == "research":
                    if player.researching != o.tech:
                        player.researching = o.tech
                        self.ev("research_started", [seat], None, tech=o.tech,
                                progress=player.research.get(o.tech, 0))
                elif o.kind == "move":
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
        for i, (seat, o) in enumerate(moves):
            src = self.s.provinces[o.src]
            for unit, n in (o.units or {}).items():
                src.units[unit] -= n
                if not src.units[unit]:
                    del src.units[unit]
            force = Force(seat, dict(o.units or {}), dict(o.units or {}) if o.hasty else {}, origin=o.src)
            force.breaks = set(self.break_targets[i]) if i < len(self.break_targets) else set()
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
                if self.shielded(edges[(a, b)], edges[(b, a)].owner, edges[(b, a)]):
                    for force, (src, dst) in ((edges[(a, b)], (a, b)), (edges[(b, a)], (b, a))):
                        self.bounce(force, src, dst, head_on=True)
                else:
                    self.edge_battle(edges[(a, b)], edges[(b, a)], a, b)
        for (src, dst), force in sorted(edges.items()):
            if force.total() and self.shielded(force, self.s.provinces[dst].owner):
                self.bounce(force, src, dst)
        arrivals: dict[str, list[Force]] = {}
        for (_, dst), force in sorted(edges.items()):
            if force.total():
                arrivals.setdefault(dst, []).append(force)
        return arrivals, fortified

    def bounce(self, force: Force, src: str, dst: str, head_on: bool = False) -> None:
        """Honored pact: the move goes back to where it came from, whole and at once (it defends there), no fight."""
        other = self.s.provinces[dst].owner
        for origin_id, units in sorted(force.parts.items()):
            origin = self.s.provinces[origin_id]
            for unit, n in units.items():
                origin.units[unit] = origin.units.get(unit, 0) + n
            self.ev("pact_bounce", [force.owner, other], dst, seat=force.owner, units=dict(units),
                    edge=[src, dst] if head_on else None, **{"from": origin_id})
        force.clear()

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

        def power(f: Force) -> float:
            return strength(self.rs, f, {}, defending=False, faction=self.factions.get(f.owner))

        attackers.sort(key=lambda f: (-power(f), ties[f.owner]))
        # Attackers meet in order of strength. `group` holds the attackers still standing, all of them mutually
        # protected by pacts; a newcomer fights every group member it is not shielded from (strongest first) and
        # joins the group if it beats them all. Without pacts the group is one champion, as before pacts existed.
        # The group keeps the order of strength at the start of the battle.
        group = [attackers[0]]
        for challenger in attackers[1:]:
            standing = True
            for champion in [g for g in group if not self.shielded(g, challenger.owner, challenger)]:
                snap = (dict(champion.units), dict(challenger.units))
                res = fight(self.rs, champion, challenger, rng, b_defending=False, factions=self.factions)
                self.ev("battle", [champion.owner, challenger.owner], pid, stage="attackers",
                        sides=[_side(champion, snap[0], res.losses_a, res.a_won, res.strength_a),
                               _side(challenger, snap[1], res.losses_b, not res.a_won, res.strength_b)])
                if res.a_won:
                    self.retreat_home(challenger, pid)
                    standing = False
                    break
                self.retreat_home(champion, pid)
                group.remove(champion)
            if standing:
                group.append(challenger)
        group = [g for g in group if g.total()]
        if not group:
            prov.units = defender.units
            return
        if defender.total():
            # The defender meets the group one by one (in the same order) until one of them beats it.
            winner_at = None
            for i, champion in enumerate(group):
                snap = (dict(champion.units), dict(defender.units))
                res = fight(self.rs, champion, defender, rng, b_defending=True, factions=self.factions,
                            defense_mul=province_defense_mul(self.s, self.rs, pid, self.factions.get(prov.owner)),
                            fortified=fortified)
                self.ev("battle", [champion.owner, defender.owner], pid, stage="defense", fortified=fortified,
                        sides=[_side(champion, snap[0], res.losses_a, res.a_won, res.strength_a),
                               _side(defender, snap[1], res.losses_b, not res.a_won, res.strength_b)])
                if res.a_won:
                    winner_at = i
                    break
                self.retreat_home(champion, pid)
            if winner_at is None:
                prov.units = defender.units
                return
            group = [g for g in group[winner_at:] if g.total()]
            if defender.owner is not None:
                self.retreat(defender, [n for n in self.rs.map.adjacency[pid]], pid)
            elif defender.total():
                self.ev("destroyed", [], pid, units=dict(defender.units))
        # Pact partners took the province together. The holder is the one that beat the defender (the earlier ones
        # lost to it and went home); with no defender, the strongest at the start of the battle. The others go home
        # whole after the battles: to their origin, or else to an own province next to this one.
        holder = group[0]
        if len(group) > 1:
            self.ev("pact_joint", [g.owner for g in group], pid, holder=holder.owner,
                    partners=sorted(g.owner for g in group if g is not holder))
            for g in group[1:]:
                self.retreat_home(g, pid, fallback=True)
        prov.owner = holder.owner
        prov.units = holder.units

    def retreat(self, force: Force, candidates: list[str], from_pid: str) -> None:
        if force.total():
            self.retreats.append((force.owner, sorted(candidates), dict(force.units), from_pid))

    def retreat_home(self, attacker: Force, from_pid: str, fallback: bool = False) -> None:
        """A losing attacker's survivors go back to the provinces they came from, each part to its own. With
        `fallback` (a pact partner leaving a jointly taken province), a part whose origin fell goes to the first
        (by id) own province next to `from_pid` instead."""
        near = sorted(self.rs.map.adjacency[from_pid]) if fallback else []
        for origin, units in sorted(attacker.parts.items()):
            self.retreats.append((attacker.owner, [origin] + [n for n in near if n != origin], dict(units),
                                  from_pid))

    def apply_retreats(self) -> None:
        for owner, candidates, units, from_pid in self.retreats:
            target = next((c for c in candidates if self.s.provinces[c].owner == owner), None)
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
            key = self.factions[seat]
            income = income_of(self.s, self.rs, seat, key)
            upkeep = upkeep_of(self.s, self.rs, seat, key)
            for res in self.rs.resources:
                player.resources[res] = player.resources.get(res, 0) + income[res] - upkeep[res]
            self.ev("economy", [seat], None, income=income, upkeep=upkeep)
            event = self.events[-1]
            if player.resources[food] < 0:
                self.starve(seat)
            for res in self.rs.resources:
                player.resources[res] = max(0, player.resources[res])
            moved = research_transfer(self.rs, player, key)
            if moved is not None:
                event["research"] = {"tech": player.researching, **moved}

    # 8. research: the active tech is done when its points reach its cost; effects count from the next turn
    def research(self, alive: list[str]) -> None:
        if not self.rs.techs:
            return
        for seat in alive:
            done = research_complete(self.rs, self.s.players[seat], self.factions[seat])
            if done is not None:
                self.ev("tech_completed", [seat], None, tech=done, techs_count=len(self.s.players[seat].techs))

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
        for seat, p in s.players.items():
            p.command_points = int(self.rs.stat("command_points", stat_key(s, self.rs, seat))) if p.alive else 0


# Events only their seats see: they would reveal resources or orders (fortify_broken reveals a fortify order).
_PRIVATE_KINDS = frozenset({"order_rejected", "economy", "starvation", "build_started", "recruited",
                            "fortify_broken", "research_started", "tech_completed"}) | dip.PRIVATE_KINDS


def _as_seen(event: dict[str, Any], seat: str) -> dict[str, Any]:
    """A public diplomacy event loses its private fields (gift amounts, pact id) for a seat that is not a side."""
    if event["kind"] in dip.PUBLIC_KINDS and seat not in event["seats"]:
        return dip.public_event(event)
    return event


def _seen_at(event: Mapping[str, Any]) -> str | None:
    """Province whose visibility decides who else sees the event: a retreat is seen where its battle was."""
    return event.get("from") if event["kind"] == "retreat" else event["province"]


def _side(force: Force, before: dict[str, int], losses: dict[str, int], won: bool, power: float) -> dict[str, Any]:
    return {"owner": force.owner, "units": before, "losses": losses, "won": won, "strength": round(power, 3)}
