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
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from dah.bots import score as bs
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
    objectives: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # seat -> its secret objectives read
    # core 0.8.0 (docs/SPY_V2.md 10)
    observed: dict[str, tuple[int, str]] = field(default_factory=dict)   # pid -> (turn, Observation report id)
    whisper: dict[str, int] = field(default_factory=dict)       # seat -> S its last ledger showed
    read_turn: dict[str, int] = field(default_factory=dict)     # seat -> last turn its orders were read
    decisions: Counter[str] = field(default_factory=Counter)    # P2 after a report: cancel | hasty (the simulator)
    works: dict[str, int] = field(default_factory=dict)         # pid -> last turn a great work is due there (observe)
    # balance core 0.8.0: pid -> (first turn seen, turns_left) of a rival great work in sight; the same turns_left a
    # turn later means it stands for want of a share, and a sabotage would not stop it (docs/SPY_V2.md 4.5)
    work_seen: dict[str, tuple[int, int]] = field(default_factory=dict)
    # core 0.8.0 (docs/TRADE.md 12.1): own reports a trade may sell, id -> (turn, mission short id, target)
    reports: dict[str, tuple[int, str, str]] = field(default_factory=dict)

    def absorb(self, intel: Mapping[str, Any] | None) -> None:
        """Take in one recon step's own intel (`intel_json`); a step already absorbed is skipped."""
        if not intel or intel.get("turn") is None or intel["turn"] == self.seen_turn:
            return
        turn = int(intel["turn"])
        self.seen_turn = turn
        for r in intel.get("reports") or []:
            facts = r.get("facts") or {}
            if r.get("id") and r.get("target") is not None:
                self.reports[str(r["id"])] = (turn, short_id(str(r.get("mission") or "")), str(r["target"]))
            if r.get("behavior") == "observe_province" and facts.get("province"):
                # an interval's upper end, or the exact number (core 0.8.0 observe `exact`)
                self.units[str(facts["province"])] = (turn, {short_id(u): int(span[1] if isinstance(span, list)
                                                                              else span)
                                                             for u, span in (facts.get("units") or {}).items()})
                if r.get("id"):
                    self.observed[str(facts["province"])] = (turn, str(r["id"]))
                due = [turn + int(left) - 1 for b, left in facts.get("building_now") or []
                       if short_id(str(b)) in bv.GREAT_WORKS]
                if due:
                    self.works[str(facts["province"])] = max(due)
                else:
                    self.works.pop(str(facts["province"]), None)
            elif r.get("behavior") == "read_orders" and facts.get("seat"):
                self.orders[str(facts["seat"])] = (turn, list(facts.get("entries") or []))
                self.read_turn[str(facts["seat"])] = turn
            elif r.get("behavior") == "read_ledger" and facts.get("seat") and facts.get("whisper") is not None:
                self.whisper[str(facts["seat"])] = int(facts["whisper"])
            elif r.get("behavior") == "read_objective" and facts.get("seat"):
                self.objectives[str(facts["seat"])] = [dict(o) for o in facts.get("objectives") or []]
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

    def report_on(self, pid: str, turn: int) -> str | None:
        """The id of this turn's own Observation report on `pid` (estimate(defender="r3")), else None."""
        got = self.observed.get(pid)
        return got[1] if got and got[0] == turn else None


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


def first_shade(view: StateView, memory: SpyMemory) -> bool:
    """The seat has never held a Shade it lost and holds none now: its first hire (m6: hired at its price, saved
    for); a replacement after a loss keeps the bot's old, higher alloy threshold."""
    return not view.shades and memory.lost_turn is None


def shade_savings(view: StateView, memory: SpyMemory, *, from_turn: int, alloy: int) -> int:
    """Alloy to keep back this turn so that the first Shade can be hired next turn (m6: armies that spend all their
    alloy every turn never reach the price). Only for the first Shade (`first_shade`), with espionage on, from
    `from_turn`, while the start capital stands and 3+ turns are left: the price less next turn's net alloy income
    (all of `alloy` when the income is unknown), at most `alloy`; else 0."""
    esp = view.espionage
    if esp is None or not first_shade(view, memory) or int(esp.get("shades_max", 0)) < 1 or view.turn < from_turn:
        return 0
    if alloy >= shade_cost() or home_capital(view) is None:
        return 0
    if view.turn_limit and view.turns_left() < 3:
        return 0
    income = (view.net_income or {}).get(BUILD_RESOURCE) if view.net_income is not None else None
    need = shade_cost() if income is None else shade_cost() - max(0, int(income))
    return max(0, min(alloy, need))


SECRET = "secret"       # core 0.6.0: the mission that reads a seat's secret objective (bots.view MISSIONS)


def secret_wanted(view: StateView, memory: SpyMemory, target: str | None) -> bool:
    """Read the target's secret objective first (core 0.6.0): the game deals objectives (the bot holds one), the pack
    has the mission and the bot has not read this target's yet (an objective never changes: one report is enough)."""
    return (target is not None and SECRET in MISSIONS and bool(view.objectives) and target not in memory.objectives
            and target not in {str((sh.get("mission") or {}).get("target")) for sh in active_shades(view)
                               if str((sh.get("mission") or {}).get("type", "")).endswith(SECRET)})


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


def rival_target(view: StateView) -> str | None:
    """Core 0.7.0 (the source `intel`): the strongest neighbour, else any living rival (highest public score), while a
    success still scores; without intel points just the strongest neighbour."""
    near = strongest_neighbour(view)
    if near is not None or not bs.intel_pays(view):
        return near
    others = [s for s in view.players if s not in view.me]
    return min(others, key=lambda s: (-view.scores.get(s, 0), s)) if others else None


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


# ---------------------------------------------------------------- core 0.8.0: deep missions (docs/SPY_V2.md 10)

def deep(mission: str) -> bool:
    """A «deep» mission of the pack (`intel` once / once_per_target / effect: secret, read_orders, sabotage)."""
    return bv.MISSION_INTEL.get(mission) in bv.DEEP_KINDS


def in_sight(view: StateView, province: Province) -> bool:
    """The province is in the seat's sight (its own or next to its own): an Observation there only adds the block
    «в расчёте этого хода» (docs/SPY_V2.md 5.3, the report's «лишние наблюдения»)."""
    return view.is_mine(province) or any(view.is_mine(n) for n in view.neighbors(province))


def mission_targets(view: StateView) -> set[str]:
    """Targets (province ids or seats) of the seat's Shades now on a mission."""
    return {str((sh.get("mission") or {}).get("target")) for sh in active_shades(view)}


def rival_work_site(view: StateView, ok: Callable[[Province], bool],
                    memory: SpyMemory | None = None) -> Province | None:
    """A province of another seat where a great work is being built: seen (a great work in progress is public in
    sight) or remembered from an Observation report while it is still due. The sabotage target of the rush bot (the
    stop costs its owner a turn); one next to our land first (it can be attacked the same turn)."""
    near = {p.id for own in view.own_provinces() for p in view.neighbors(own)}
    due = {pid for pid, last in (memory.works if memory is not None else {}).items() if last >= view.turn}
    cands = [p for p in view.provinces.values() if p.owner is not None and not view.is_mine(p) and ok(p)
             and ((any(b in bv.GREAT_WORKS for b in p.building_now) and not _work_stands(view, p, memory))
                  or (p.id in due and p.id not in near))]
    return min(cands, key=lambda p: (p.id not in near, p.id)) if cands else None


def _work_stands(view: StateView, p: Province, memory: SpyMemory | None) -> bool:
    """A rival great work in sight whose turns_left has not moved since an earlier turn: it stands for want of a
    share (balance core 0.8.0). Records what it sees in `memory.work_seen`."""
    left = min((n for b, n in p.work_left if b in bv.GREAT_WORKS), default=None)
    if memory is None or left is None:
        return False
    first, seen = memory.work_seen.get(p.id, (view.turn, -1))
    if seen != left:
        memory.work_seen[p.id] = (view.turn, left)
        return False
    return first < view.turn


def weak_occupation(view: StateView, ok: Callable[[Province], bool], most: int = 1) -> Province | None:
    """Our former land (its origin is us) occupied by another seat with 1..`most` units seen: a sabotage there raises
    a revolt (core 0.8.0 `unrest_units`)."""
    cands = {p.id: p for own in view.own_provinces() for p in view.neighbors(own)
             if p.owner is not None and not view.is_mine(p) and p.origin in view.me and p.occupied_by
             and p.units is not None and 1 <= p.unit_count <= most and ok(p)}
    return min(cands.values(), key=lambda p: p.id) if cands else None


def border_threat(view: StateView, among: Callable[[str], bool] | None = None) -> str | None:
    """The living seat with the most units seen next to our land (Read Orders shows its free troops there)."""
    count: Counter[str] = Counter()
    seen: set[str] = set()
    for own in view.own_provinces():
        for p in view.neighbors(own):
            if p.id in seen or p.owner is None or view.is_mine(p) or (view.players and p.owner not in view.players):
                continue
            seen.add(p.id)
            if among is None or among(p.owner):
                count[p.owner] += p.unit_count
    if not count:
        return None
    return min(count, key=lambda s: (-count[s], -view.scores.get(s, 0), s))


def ward_order(view: StateView, memory: SpyMemory, alloy: int) -> tuple[Order, int] | None:
    """Core 0.8.0 (docs/SPY_V2.md 10, the builder): a Ward in the province of an own great work under construction
    while a neighbour's last ledger showed S >= 2 (sabotage needs S2); (order, alloy) or None."""
    if view.espionage is None or alloy < bv.WARD_COST_ALLOY:
        return None
    near = {p.owner for own in view.own_provinces() for p in view.neighbors(own)
            if p.owner is not None and not view.is_mine(p)}
    if not any(memory.whisper.get(str(s), 0) >= MISSIONS.get("sabotage", (2,))[0] for s in near):
        return None
    sites = [p for p in view.full_provinces() if any(b in bv.GREAT_WORKS for b in p.building_now)]
    if not sites:
        return None
    site = min(sites, key=lambda p: (p.kind != "capital", p.id))
    return {"kind": "ward", "province": site.id}, bv.WARD_COST_ALLOY


P2_FORTIFY_CP = 1       # registry: a fortify given in negotiation_2


def fortify_threatened(view: StateView, memory: SpyMemory, current: list[Order], cp_left: int) -> list[Order]:
    """Negotiation_2: a fortify (1 CP each) in every own province a move of this turn's Read Orders reports attacks
    (a false entry the report exposed is skipped), unless already fortified or left by an own move."""
    out: list[Order] = []
    fortified = {str(o.get("province")) for o in current if o.get("kind") == "fortify"}
    leaving = {str(o.get("from")) for o in current if o.get("kind") == "move" and not o.get("feint")}
    for seat in sorted(view.players or memory.orders):
        for entry in memory.orders_of(seat, view.turn):
            dst = str(entry.get("to"))
            prov = view.provinces.get(dst)
            if (entry.get("kind") != "move" or entry.get("exposed") or prov is None or not view.is_mine(prov)
                    or dst in fortified or dst in leaving or cp_left < P2_FORTIFY_CP):
                continue
            out.append({"kind": "fortify", "province": dst})
            fortified.add(dst)
            cp_left -= P2_FORTIFY_CP
    return out


def breaches(view: StateView, memory: SpyMemory) -> list[tuple[str, str]]:
    """(seat, own province) of the moves with "pact": "break" this turn's Read Orders reports show against us."""
    out = []
    for seat in sorted(view.players or memory.orders):
        for entry in memory.orders_of(seat, view.turn):
            prov = view.provinces.get(str(entry.get("to")))
            if (entry.get("kind") == "move" and entry.get("pact") == "break" and not entry.get("exposed")
                    and prov is not None and view.is_mine(prov)):
                out.append((seat, prov.id))
    return out


def info_mission(view: StateView, shade: str, alloy: int, food: int, *, attacks: list[Province],
                 ledger_of: str | None, rounds: int = 1) -> tuple[Order, int, int] | None:
    """A free Shade does not idle once the intel points are spent (docs/SPY_V2.md 10, «все»): an Observation of the
    target of an own attack (the recruits and fortify of the turn), else the ledger of the seat of a coming war or
    deal; None when neither is wanted."""
    busy = mission_targets(view)
    for target in attacks:
        if target.id not in busy:
            got = spy_order(view, shade, "observe", target.id, rounds, alloy, food)
            if got is not None:
                return got
    if ledger_of is not None and ledger_of not in busy:
        return spy_order(view, shade, "ledger", ledger_of, rounds, alloy, food)
    return None


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
