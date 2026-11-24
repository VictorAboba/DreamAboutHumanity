"""Stage 4 (docs/SCORING.md section 10): what the bots do for the score sources and their secret objective.

Pure functions of a StateView, shared by the bot types; each type picks what fits its style:

- great works (`works` on): a work goes into the most protected own full province it is allowed in (the fewest foreign
  neighbours, then the farthest from a foreign province, then the most own units) when the provisions free after the
  order still feed the army `food_turns` turns (stock + net income) and it can be ready before the game ends; a bot
  that saves for a work keeps its stock and recruits only what the net income leaves after the saving rate
  (`saving_rate`: the bots' armies eat their provisions, they have none idle);
- the objective: `hold_type` / `hold_count` / `hold_foreign` / `capital_other` / `rival_capital_taken` make the
  provinces that advance it preferred targets (a bonus on the attack estimate, see `tactics.plan_moves`) and keep one
  unit in those already held; `techs_branch` puts its branch first in research; `great_work` makes even a non-builder
  lay the cheapest work;
- war (`war` on): the seats' provinces get a small bonus over neutral ones while the war cap is not reached.

The bots read only their own state (`you.objectives`, `you.score_parts`, `you.war`); the others' score is not needed.
"""

from __future__ import annotations

from typing import Any, Mapping

from dah.bots.tactics import Reserved, distance_to_foreign
from dah.bots.view import (
    BUILD_RESOURCE,
    BUILDING_COST_ALLOY,
    BUILDING_COST_FOOD,
    FOOD_RESOURCE,
    GREAT_WORKS,
    UNIT_UPKEEP_FOOD,
    Province,
    StateView,
    short_id,
)

OBJECTIVE_BONUS = 0.1   # added to the estimate of an attack on a province that advances the open objective
WAR_BONUS = 0.05        # added to the estimate of an attack on a seat's province while the war cap is not reached
Order = dict[str, Any]
HOLD_KINDS = frozenset({"hold_type", "hold_count", "hold_foreign", "capital_other", "rival_capital_taken"})


def upkeep(view: StateView) -> int:
    return sum(UNIT_UPKEEP_FOOD.get(unit, 1) * n for p in view.own_provinces() for unit, n in (p.units or {}).items())


def has_work(view: StateView, work: str) -> bool:
    """The seat already has (or builds) this great work in one of its provinces (max_per_owner 1 in core)."""
    return any(work in p.buildings or work in p.building_now for p in view.own_provinces())


def foreign_neighbours(view: StateView, province: Province) -> int:
    return sum(1 for n in view.neighbors(province) if not view.is_mine(n))


def work_site(view: StateView, work: str, *, capital: bool = True,
              busy: set[str] | frozenset[str] = frozenset()) -> Province | None:
    """The most protected own full province the work may stand in (not one in `busy`)."""
    allowed = GREAT_WORKS[work][2]
    distance = distance_to_foreign(view)
    sites = [p for p in view.full_provinces()
             if work not in p.buildings and p.id not in busy and (allowed is None or p.kind in allowed)
             and (capital or p.kind != "capital")]
    if not sites:
        return None
    return min(sites, key=lambda p: (foreign_neighbours(view, p), -distance.get(p.id, 0), -p.unit_count, p.id))


def lay_works(view: StateView, works: list[tuple[str, bool]], food_turns: int) -> tuple[list[Order], StateView]:
    """Build orders for the great works in `works` [(work, capital allowed)] that the free provisions and the alloy
    cover, in that order; returns them and the view with their price taken off (what the rest is planned with)."""
    from dah.bots.spy import food_view
    out: list[Order] = []
    busy: set[str] = set()
    for work, capital in works:
        order = work_order(view, work, view.free_food(), view.resource(BUILD_RESOURCE), food_turns, capital=capital,
                           busy=busy)
        if order is not None:
            out.append(order)
            busy.add(str(order["province"]))
            food, alloy = work_cost(work)
            view = food_view(view, food, alloy)
    return out, view


def work_order(view: StateView, work: str, food: int, alloy: int, food_turns: int, *,
               capital: bool = True, busy: set[str] | frozenset[str] = frozenset()) -> Order | None:
    """A build order for the great work `work`, or None: works are off, the seat has one, it cannot be ready by the
    end, or the provisions (`food` free, keeping `food_turns` turns of upkeep) or `alloy` do not cover it."""
    if not view.scoring("works") or work not in GREAT_WORKS or has_work(view, work):
        return None
    turns = GREAT_WORKS[work][0]
    if view.turn_limit and view.turns_left() < turns:
        return None
    if food - BUILDING_COST_FOOD.get(work, 0) < food_need(view, food_turns) or alloy < BUILDING_COST_ALLOY[work]:
        return None
    site = work_site(view, work, capital=capital, busy=busy)
    if site is None:
        return None
    return {"kind": "build", "province": site.id, "building": view.qualify(work)}


def food_need(view: StateView, food_turns: int) -> int:
    """Provisions that must stay after paying for a work so the army stays fed `food_turns` turns: the upkeep the
    net income does not cover (all of it when the server sent no net income)."""
    if view.net_income is None:
        return upkeep(view) * food_turns
    return max(0, -view.net_income.get(FOOD_RESOURCE, 0)) * food_turns


SAVING_SLACK = 5    # provisions/turn beyond the net income a saver hopes to find per farm it can still lay


def saving_rate(view: StateView, work: str, food_turns: int) -> int | None:
    """Provisions per turn to put aside so the stock reaches the price of `work` (and `food_need`) by the last turn it
    can be ordered and still be ready; None when the bot is not saving for it (works off, had, affordable now, too
    late, or out of reach even with the army frozen and one more farm)."""
    if not view.scoring("works") or work not in GREAT_WORKS or has_work(view, work) or not view.turn_limit:
        return None
    turns_to_order = view.turns_left() - GREAT_WORKS[work][0]   # turns after this one while it can still be ordered
    need = BUILDING_COST_FOOD.get(work, 0) + food_need(view, food_turns) - view.resource(FOOD_RESOURCE)
    if turns_to_order < 1 or need <= 0:
        return None
    rate = -(-need // turns_to_order)
    net = (view.net_income or {}).get(FOOD_RESOURCE, 0)
    farms = sum(1 for p in view.full_provinces() if "farm" not in p.buildings)   # a saver lays farms first
    return rate if rate <= max(0, net) + SAVING_SLACK * max(1, farms) else None


def work_cost(work: str) -> tuple[int, int]:
    """(provisions, alloy) of a great work."""
    return BUILDING_COST_FOOD.get(work, 0), BUILDING_COST_ALLOY[work]


def wants_work(view: StateView) -> bool:
    """The open objective asks for a great work."""
    return any(o.get("kind") == "great_work" for o in view.open_objectives())


def objective_branch(view: StateView) -> str | None:
    """The branch an open `techs_branch` objective asks for (short id)."""
    for o in view.open_objectives():
        if o.get("kind") == "techs_branch" and (o.get("params") or {}).get("branch"):
            return short_id(str(o["params"]["branch"]))
    return None


def home_of(view: StateView, seat: str) -> Province | None:
    """The capital `seat` started with: by its origin (conquest games), else a capital it owns now."""
    capitals = [p for _, p in sorted(view.provinces.items()) if p.kind == "capital"]
    return (next((p for p in capitals if p.origin == seat), None)
            or next((p for p in capitals if p.owner == seat and p.origin is None), None))


def advances(view: StateView, objective: Mapping[str, Any], province: Province) -> bool:
    """Holding `province` (not ours now) would advance the objective."""
    kind, params = objective.get("kind"), objective.get("params") or {}
    if kind == "hold_type":
        return province.kind == short_id(str(params.get("type", "")))
    if kind == "hold_count":
        return True
    if kind == "hold_foreign":
        origin = province.origin or province.owner
        return origin is not None and origin not in view.me and (not view.players or origin in view.players)
    if kind == "capital_other":
        return province.kind == "capital" and (province.origin or province.owner) not in (None, *view.me)
    if kind == "rival_capital_taken":
        target = objective.get("target")
        home = home_of(view, str(target)) if target else None
        return home is not None and home.id == province.id
    return False


def attack_bonus(view: StateView, war: bool = False) -> dict[str, float]:
    """Province id -> bonus on the attack estimate: OBJECTIVE_BONUS for the provinces that advance an open hold
    objective; with `war` (and the source on, the cap not reached) WAR_BONUS for every province of another seat."""
    out: dict[str, float] = {}
    objectives = [o for o in view.open_objectives() if o.get("kind") in HOLD_KINDS]
    war_on = war and view.scoring("war") and int((view.war or {}).get("cap_left", 1)) > 0
    for pid, p in sorted(view.provinces.items()):
        if view.is_mine(p):
            continue
        bonus = OBJECTIVE_BONUS if any(advances(view, o, p) for o in objectives) else 0.0
        if war_on and p.owner is not None and not view.is_partner(p.owner):
            bonus += WAR_BONUS
        if bonus:
            out[pid] = bonus
    return out


def objective_garrison(view: StateView) -> Reserved:
    """One unit stays in each own province that counts for a hold objective (met or not: it is checked at the end)."""
    out: Reserved = {}
    objectives = [o for o in view.objectives if o.get("kind") in HOLD_KINDS - {"hold_count"}]
    if not objectives:
        return out
    for p in view.own_provinces():
        if not p.units:
            continue
        counts = False
        for o in objectives:
            kind, params = o.get("kind"), o.get("params") or {}
            if kind == "hold_type" and p.kind == short_id(str(params.get("type", ""))):
                counts = True
            elif kind == "hold_foreign" and p.origin is not None and p.origin not in view.me:
                counts = True
            elif kind in ("capital_other", "rival_capital_taken") and p.kind == "capital" and p.origin is not None \
                    and p.origin not in view.me:
                counts = True
        if counts:
            unit = sorted(p.units)[0]
            out[p.id] = {unit: 1}
    return out


def research_branches(view: StateView, branches: tuple[str, ...]) -> tuple[str, ...]:
    """The bot's branch order with an open `techs_branch` objective's branch first."""
    first = objective_branch(view)
    if first is None:
        return branches
    return (first, *(b for b in branches if b != first))
