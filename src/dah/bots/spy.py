"""Espionage of the bots (docs/SPY_AND_CONQUEST.md section 7): Shades, missions, false entries and what the bots
remember of their reports.

Pure functions of a StateView (and the bot's `SpyMemory`), so the same code plays over HTTP and in the simulator. The
bots read their reports as data (`GET /v1/intel?format=json`, in-process the recon report itself): the same facts the
text of get_intel shows, never the truth. All numbers come from `dah.bots.view` (MISSIONS, SHADE_COST_ALLOY).

Decision 22 (no prisoners): a Shade caught at the bot with its owner attributed marks that seat as hostile for the rest
of the game (`SpyMemory.hostile`); after losing a Shade of its own the bot hires a new one no sooner than
REHIRE_TURNS turns later.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from dah.bots import view as bv
from dah.bots.tactics import Reserved, free_units
from dah.bots.view import BUILD_RESOURCE, FOOD_RESOURCE, MISSIONS, Province, StateView, short_id

Order = dict[str, Any]
REHIRE_TURNS = 2        # a lost Shade is replaced no sooner than this many turns after the loss
MEMORY_TURNS = 2        # an observe report fills a hidden province for this many turns
FEINT_CP = 1            # registry default of feint.cp_cost


@dataclass
class SpyMemory:
    """What a bot keeps from its reports between phases (one per bot, for the whole game)."""

    hostile: set[str] = field(default_factory=set)      # seats whose Shade was caught at us, owner attributed
    lost_turn: int | None = None                        # last turn a Shade of ours was lost ("нет связи")
    units: dict[str, tuple[int, dict[str, int]]] = field(default_factory=dict)  # pid -> (turn, unit -> upper bound)
    orders: dict[str, tuple[int, list[dict[str, Any]]]] = field(default_factory=dict)  # seat -> (turn, entries)
    strikes: set[str] = field(default_factory=set)      # provinces our sabotage struck in this turn's recon
    strike_turn: int | None = None
    seen_turn: int | None = None                        # the recon step last absorbed
    caught: int = 0                                     # Shades caught at us (all, attributed or not)

    def absorb(self, intel: Mapping[str, Any] | None) -> None:
        """Take in one recon step's own intel (`intel_json`); a step already absorbed is skipped."""
        if not intel or intel.get("turn") is None or intel["turn"] == self.seen_turn:
            return
        turn = int(intel["turn"])
        self.seen_turn = turn
        for r in intel.get("reports") or []:
            facts = r.get("facts") or {}
            if r.get("behavior") == "observe_province" and facts.get("province"):
                self.units[str(facts["province"])] = (turn, {short_id(u): int(span[1]) for u, span in
                                                             (facts.get("units") or {}).items()})
            elif r.get("behavior") == "read_orders" and facts.get("seat"):
                self.orders[str(facts["seat"])] = (turn, list(facts.get("entries") or []))
        self.strikes = {str(r["target"]) for r in intel.get("rounds") or [] if r.get("outcome") == "struck"}
        self.strike_turn = turn
        for c in intel.get("caught") or []:
            self.caught += 1
            if c.get("owner"):
                self.hostile.add(str(c["owner"]))
        if intel.get("lost"):
            self.lost_turn = turn

    def struck(self, turn: int) -> set[str]:
        return set(self.strikes) if self.strike_turn == turn else set()

    def orders_of(self, seat: str, turn: int) -> list[dict[str, Any]]:
        got = self.orders.get(seat)
        return list(got[1]) if got and got[0] == turn else []


def apply_memory(view: StateView, memory: SpyMemory) -> StateView:
    """Hidden provinces with a recent observe report get its upper bounds as their units (the estimate then plans
    with the worst case instead of the fog)."""
    filled = {}
    for pid, (turn, units) in memory.units.items():
        prov = view.provinces.get(pid)
        if prov is not None and prov.units is None and view.turn - turn <= MEMORY_TURNS:
            filled[pid] = replace(prov, units={u: n for u, n in units.items() if n > 0})
    if not filled:
        return view
    return replace(view, provinces={**view.provinces, **filled})


# ---------------------------------------------------------------- Shades

def idle_shades(view: StateView) -> list[str]:
    return [str(sh["id"]) for sh in view.shades if sh.get("status") == "idle"]


def active_shades(view: StateView) -> list[Mapping[str, Any]]:
    return [sh for sh in view.shades if sh.get("status") == "mission" and not (sh.get("mission") or {}).get("ended")]


def home_capital(view: StateView) -> Province | None:
    """The own full start capital (hiring needs it): a capital of the seat's own origin, not occupied or ruined."""
    for p in view.own_provinces():
        if p.kind == "capital" and (p.origin is None or p.origin in view.me) and not p.status:
            return p
    return None


def shade_cost() -> int:
    """Alloy a Shade costs (bots.view, re-read: the simulator points it at its pack)."""
    return bv.SHADE_COST_ALLOY


def hire_order(view: StateView, memory: SpyMemory, *, from_turn: int, max_shades: int, alloy: int,
               min_alloy: int) -> Order | None:
    """Hire a Shade when the game has espionage, the turn has come, fewer than `max_shades` (and the limit) are held,
    `alloy` is at least `min_alloy`, the start capital stands, a mission can still follow (2+ turns left) and no
    Shade was lost in the last REHIRE_TURNS turns."""
    esp = view.espionage
    if esp is None or view.turn < from_turn or alloy < max(min_alloy, shade_cost()):
        return None
    held = len(view.shades)
    if held >= min(max_shades, int(esp.get("shades_max", 0))) or home_capital(view) is None:
        return None
    if view.turn_limit and view.turns_left() < 2:
        return None
    if memory.lost_turn is not None and view.turn - memory.lost_turn < REHIRE_TURNS:
        return None
    return {"kind": "hire_shade"}


def can_start(view: StateView) -> bool:
    esp = view.espionage or {}
    return len(active_shades(view)) < int(esp.get("active_limit", 0))


def mission_price(mission: str, rounds: int) -> tuple[int, int]:
    """(alloy, provisions) of a mission for `rounds` rounds."""
    _, _, once, per_round = MISSIONS[mission]
    return once, per_round * rounds


def spy_order(view: StateView, shade: str, mission: str, target: str, rounds: int, alloy: int,
              food: int) -> tuple[Order, int, int] | None:
    """A `spy` order (rounds trimmed to the mission's maximum and the turns left) if S allows it and `alloy` / `food`
    pay for it; returns (order, alloy, provisions) or None."""
    if mission not in MISSIONS or view.espionage is None:
        return None
    need, top, *_ = MISSIONS[mission]
    if view.whisper < need:
        return None
    if view.turn_limit:
        top = min(top, view.turns_left())
    rounds = min(rounds, top)
    if rounds < 1:
        return None
    a, f = mission_price(mission, rounds)
    if a > alloy or f > food:
        return None
    return {"kind": "spy", "shade": shade, "mission": mission, "target": target, "rounds": rounds}, a, f


def strongest_neighbour(view: StateView, among: Callable[[str], bool] | None = None) -> str | None:
    """The living neighbour seat with the highest public score (seat id on ties), optionally only `among`."""
    seats = set()
    for own in view.own_provinces():
        for p in view.neighbors(own):
            if p.owner is not None and not view.is_mine(p):
                seats.add(p.owner)
    if view.players:
        seats &= set(view.players)
    if among is not None:
        seats = {s for s in seats if among(s)}
    if not seats:
        return None
    return min(seats, key=lambda s: (-view.scores.get(s, 0), s))


def enemy_target(view: StateView, ok: Callable[[Province], bool]) -> Province | None:
    """A province of another seat next to our land for a mission: a capital first, then the most defended."""
    seen: dict[str, Province] = {}
    for own in view.own_provinces():
        for p in view.neighbors(own):
            if p.owner is not None and not view.is_mine(p) and ok(p):
                seen[p.id] = p
    if not seen:
        return None
    return min(seen.values(), key=lambda p: (p.kind != "capital", -p.unit_count, p.id))


def hidden_target(view: StateView, ok: Callable[[Province], bool]) -> Province | None:
    """A province of another seat whose army the fog hides, two steps from our land (a capital first)."""
    near = {n.id for own in view.own_provinces() for n in view.neighbors(own)}
    two = {m.id for pid in near for m in view.neighbors(view.provinces[pid])}
    cands = [view.provinces[pid] for pid in sorted(two)
             if view.provinces[pid].units is None and view.provinces[pid].owner is not None
             and not view.is_mine(view.provinces[pid]) and ok(view.provinces[pid])]
    if not cands:
        return None
    return min(cands, key=lambda p: (p.kind != "capital", p.id))


# ---------------------------------------------------------------- false entries

def feint_order(view: StateView, reserved: Reserved, moves: list[Order], target_ok: Callable[[Province], bool],
                rng: random.Random) -> Order | None:
    """One false entry (`feint: true` move) from an own province no move leaves, with the units that stay there, at an
    adjacent province of another seat that `target_ok` allows and no move goes to; needs FEINT_CP command points."""
    if view.espionage is None or view.command_points < FEINT_CP:
        return None
    leaving = {str(o["from"]) for o in moves}
    targeted = {str(o["to"]) for o in moves}
    cands: list[tuple[str, str]] = []
    for source in view.own_provinces():
        if source.id in leaving or not free_units(source, reserved):
            continue
        for target in view.neighbors(source):
            if (target.owner is not None and not view.is_mine(target) and target.id not in targeted
                    and target_ok(target)):
                cands.append((source.id, target.id))
    if not cands:
        return None
    src, dst = rng.choice(cands)
    units = {view.qualify(u): n for u, n in free_units(view.provinces[src], reserved).items()}
    return {"kind": "move", "from": src, "to": dst, "units": units, "feint": True}


def food_view(view: StateView, food_spent: int, alloy_spent: int = 0) -> StateView:
    """The view with provisions / alloy already committed to stage-3 orders taken off (recruits plan with the rest)."""
    if not food_spent and not alloy_spent:
        return view
    res = dict(view.resources)
    res[FOOD_RESOURCE] = max(0, res.get(FOOD_RESOURCE, 0) - food_spent)
    res[BUILD_RESOURCE] = max(0, res.get(BUILD_RESOURCE, 0) - alloy_spent)
    return replace(view, resources=res)
