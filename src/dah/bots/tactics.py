"""Tactics shared by the bots: where armies attack, march and fortify, and how much the capital keeps at home.

The bots differ in strategy (what they spend on, how sure a fight must be, whom they make pacts with); the movement
code is the same for all of them, so a simulation compares strategies rather than tactical bugs.
"""

from __future__ import annotations

import random
from collections import deque
from typing import Callable, Mapping

from dah.bots.base import Estimator, Order
from dah.bots.view import RECRUIT_BUILDINGS, UNIT_COST_ALLOY, UNIT_UPKEEP_FOOD, Province, StateView, short_id

Reserved = dict[str, dict[str, int]]   # province -> short unit id -> units that stay


def free_units(province: Province, reserved: Reserved) -> dict[str, int]:
    taken = reserved.get(province.id, {})
    return {u: n - taken.get(u, 0) for u, n in sorted((province.units or {}).items()) if n > taken.get(u, 0)}


def garrison(capital: Province | None, size: int) -> Reserved:
    """Up to `size` units of the capital that stay at home (unit kinds in id order)."""
    if capital is None or not capital.units or size <= 0:
        return {}
    keep: dict[str, int] = {}
    left = size
    for unit, n in sorted(capital.units.items()):
        if left <= 0:
            break
        keep[unit] = min(n, left)
        left -= keep[unit]
    return {capital.id: keep}


def capital_of(view: StateView) -> Province | None:
    """The own capital: in conquest games the start one (a captured capital is not it), else the first by id."""
    capitals = [p for p in view.full_provinces() if p.kind == "capital"]
    return next((p for p in capitals if p.origin is None or p.origin in view.me), capitals[0] if capitals else None)


def merge_reserved(*parts: Reserved) -> Reserved:
    out: Reserved = {}
    for part in parts:
        for pid, units in part.items():
            mine = out.setdefault(pid, {})
            for unit, n in units.items():
                mine[unit] = mine.get(unit, 0) + n
    return out


def reserved_by_orders(orders: list[Order]) -> Reserved:
    """Units already sent by move orders (server form: full or short ids)."""
    reserved: Reserved = {}
    for order in orders:
        if order.get("kind") != "move":
            continue
        per_unit = reserved.setdefault(str(order["from"]), {})
        for unit, n in order.get("units", {}).items():
            per_unit[short_id(unit)] = per_unit.get(short_id(unit), 0) + int(n)
    return reserved


def best_attack(
    view: StateView,
    reserved: Reserved,
    estimate: Estimator,
    hasty: bool,
    threshold: float,
    rng: random.Random,
    target_ok: Callable[[Province], bool] | None = None,
) -> Order | None:
    """Send every free unit of one own province at its best adjacent target if win_prob >= threshold.

    `target_ok` filters the foreign or neutral targets (default: all of them).
    """
    candidates: list[tuple[float, Order]] = []
    for source in view.own_provinces():
        free = free_units(source, reserved)
        if not free:
            continue
        for target in view.neighbors(source):
            if view.is_mine(target) or (target_ok is not None and not target_ok(target)):
                continue
            win = estimate(free, target.units or {}, target.type, target.fortified, hasty)
            if win >= threshold:
                units = {view.qualify(unit): n for unit, n in free.items()}
                candidates.append((win, {"kind": "move", "from": source.id, "to": target.id, "units": units}))
    if not candidates:
        return None
    best = max(win for win, _ in candidates)
    return rng.choice([order for win, order in candidates if win == best])


def advance(view: StateView, reserved: Reserved | None = None) -> Order | None:
    """Move the largest interior army one step closer to the nearest foreign or neutral province."""
    distance = distance_to_foreign(view)
    reserved = reserved or {}
    interior = [
        p for p in view.own_provinces()
        if free_units(p, reserved) and all(view.is_mine(n) for n in view.neighbors(p)) and p.id in distance
    ]
    if not interior:
        return None
    source = max(interior, key=lambda p: sum(free_units(p, reserved).values()))  # first by id on ties
    step = min(view.neighbors(source), key=lambda n: (distance.get(n.id, len(view.provinces)), n.id))
    units = {view.qualify(unit): n for unit, n in free_units(source, reserved).items()}
    return {"kind": "move", "from": source.id, "to": step.id, "units": units}


def plan_moves(
    view: StateView,
    estimate: Estimator,
    threshold: float,
    rng: random.Random,
    reserved: Reserved | None = None,
    target_ok: Callable[[Province], bool] | None = None,
    bonus: Mapping[str, float] | None = None,
) -> list[Order]:
    """One move per own army: the biggest armies first, each at its best adjacent target (not yet targeted) with
    win_prob >= threshold; an interior army with no target marches one step towards the nearest foreign province
    (one of the nearest steps, by the bot's rng);
    a border army with no good target stays (and can fortify).
    `bonus` (stage 4: province id -> b, from dah.bots.score): such a target counts with win_prob + b, both for the
    threshold and for the choice among targets (no bonus: the plain rule)."""
    bonus = bonus or {}
    reserved = reserved or {}
    distance = distance_to_foreign(view)
    sources = sorted(
        (p for p in view.own_provinces() if free_units(p, reserved)),
        key=lambda p: (-sum(free_units(p, reserved).values()), p.id),
    )
    orders: list[Order] = []
    targeted: set[str] = set()
    for source in sources:
        free = free_units(source, reserved)
        units = {view.qualify(unit): n for unit, n in free.items()}
        candidates: list[tuple[float, str]] = []
        for target in view.neighbors(source):
            if view.is_mine(target) or target.id in targeted or (target_ok is not None and not target_ok(target)):
                continue
            win = estimate(free, target.units or {}, target.type, target.fortified, False) + bonus.get(target.id, 0.0)
            if win >= threshold:
                candidates.append((win, target.id))
        if candidates:
            best = max(win for win, _ in candidates)
            target_id = rng.choice([pid for win, pid in candidates if win == best])
            targeted.add(target_id)
            orders.append({"kind": "move", "from": source.id, "to": target_id, "units": units})
        elif all(view.is_mine(n) for n in view.neighbors(source)) and source.id in distance:
            near = min(distance.get(n.id, len(view.provinces)) for n in view.neighbors(source))
            step = rng.choice([n for n in view.neighbors(source) if distance.get(n.id, len(view.provinces)) == near])
            orders.append({"kind": "move", "from": source.id, "to": step.id, "units": units})
    return orders


def fortify_orders(view: StateView, moves: list[Order]) -> list[Order]:
    """Fortify every own province that holds an army (the capital always) and that no move leaves."""
    leaving = {str(o["from"]) for o in moves}
    holders = sorted(
        (p for p in view.own_provinces() if p.unit_count or p.kind == "capital"),
        key=lambda p: (p.kind != "capital", p.id),
    )
    return [{"kind": "fortify", "province": p.id} for p in holders if p.id not in leaving]


def distance_to_foreign(view: StateView) -> dict[str, int]:
    """Edge distance from every province to the nearest province that is not ours (multi-source BFS)."""
    distance = {pid: 0 for pid, p in view.provinces.items() if not view.is_mine(p)}
    queue = deque(sorted(distance))
    while queue:
        current = view.provinces[queue.popleft()]
        for neighbor in view.neighbors(current):
            if neighbor.id not in distance:
                distance[neighbor.id] = distance[current.id] + 1
                queue.append(neighbor.id)
    return distance


def recruit_site(view: StateView) -> Province | None:
    """First own province with a recruiting building (id order)."""
    return next((p for p in view.full_provinces() if RECRUIT_BUILDINGS & set(p.buildings)), None)


def unique_recruit(view: StateView, alloy: int, food_turns: int, max_count: int) -> tuple[Order | None, int, int]:
    """Recruit up to `max_count` of the own faction's unique unit (stage 2) that `alloy` (and the other stocks) pay
    for and the food keeps fed for `food_turns` turns. Returns (order or None, alloy spent, upkeep added)."""
    unit = view.unique_unit
    site = recruit_site(view)
    if unit is None or site is None or max_count <= 0 or unit not in UNIT_COST_ALLOY:
        return None, 0, 0
    count = min(view.affordable(unit, alloy), max_count)
    feedable = view.feedable(unit, food_turns)
    if feedable is not None:
        count = min(count, feedable)
    if count <= 0:
        return None, 0, 0
    order = {"kind": "recruit", "province": site.id, "unit": view.qualify(unit), "count": count}
    return order, count * UNIT_COST_ALLOY[unit], count * UNIT_UPKEEP_FOOD.get(unit, 1)
