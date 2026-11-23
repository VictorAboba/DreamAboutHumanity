"""Rush bot: recruits the maximum and attacks an adjacent foreign or neutral province with estimate >= 0.6.

Armies that have no foreign neighbour march one step towards the nearest foreign or neutral province.
"""

from __future__ import annotations

from dah.bots.base import Bot, Estimator, Order
from collections import deque

from dah.bots.view import BUILD_RESOURCE, RECRUIT_BUILDINGS, UNIT_COST_ALLOY, StateView, short_id

WIN_THRESHOLD = 0.6
RECRUIT_UNIT = "assault"
P2_NEW_ATTACK_CP = 2  # registry default of p2.cost.new_attack


class RushBot(Bot):
    name = "rush"

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        orders: list[Order] = []
        recruit_site = next((p for p in view.own_provinces() if RECRUIT_BUILDINGS & set(p.buildings)), None)
        count = view.resource(BUILD_RESOURCE) // UNIT_COST_ALLOY[RECRUIT_UNIT]
        if recruit_site is not None and count > 0:
            orders.append(
                {"kind": "recruit", "province": recruit_site.id, "unit": view.qualify(RECRUIT_UNIT), "count": count}
            )
        attack = self._best_attack(view, {}, estimate, hasty=False) or _advance(view)
        if attack is not None:
            orders.append(attack)
        return orders

    def p2_orders(self, view: StateView, current: list[Order], cp_left: int, estimate: Estimator) -> list[Order]:
        if cp_left < P2_NEW_ATTACK_CP or any(o.get("kind") == "move" for o in current):
            return []
        attack = self._best_attack(view, _reserved_units(current), estimate, hasty=True)
        return [attack] if attack is not None else []

    def _best_attack(
        self, view: StateView, reserved: dict[str, dict[str, int]], estimate: Estimator, hasty: bool
    ) -> Order | None:
        """Send every free unit of one own province at its best adjacent target if win_prob >= threshold."""
        candidates: list[tuple[float, Order]] = []
        for source in view.own_provinces():
            taken = reserved.get(source.id, {})
            free = {unit: n - taken.get(unit, 0) for unit, n in (source.units or {}).items()}
            free = {unit: n for unit, n in free.items() if n > 0}
            if not free:
                continue
            for target in view.neighbors(source):
                if view.is_mine(target):
                    continue
                win = estimate(free, target.units or {}, target.type, target.fortified, hasty)
                if win >= WIN_THRESHOLD:
                    units = {view.qualify(unit): n for unit, n in free.items()}
                    candidates.append((win, {"kind": "move", "from": source.id, "to": target.id, "units": units}))
        if not candidates:
            return None
        best = max(win for win, _ in candidates)
        return self.rng.choice([order for win, order in candidates if win == best])


def _advance(view: StateView) -> Order | None:
    """Move the largest interior army one step closer to the nearest foreign or neutral province."""
    distance = _distance_to_foreign(view)
    interior = [
        p for p in view.own_provinces()
        if p.unit_count and all(view.is_mine(n) for n in view.neighbors(p)) and p.id in distance
    ]
    if not interior:
        return None
    source = max(interior, key=lambda p: p.unit_count)  # first by id on ties
    step = min(view.neighbors(source), key=lambda n: (distance.get(n.id, len(view.provinces)), n.id))
    units = {view.qualify(unit): n for unit, n in (source.units or {}).items()}
    return {"kind": "move", "from": source.id, "to": step.id, "units": units}


def _distance_to_foreign(view: StateView) -> dict[str, int]:
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


def _reserved_units(orders: list[Order]) -> dict[str, dict[str, int]]:
    reserved: dict[str, dict[str, int]] = {}
    for order in orders:
        if order.get("kind") != "move":
            continue
        per_unit = reserved.setdefault(str(order["from"]), {})
        for unit, n in order.get("units", {}).items():
            per_unit[short_id(unit)] = per_unit.get(short_id(unit), 0) + int(n)
    return reserved
