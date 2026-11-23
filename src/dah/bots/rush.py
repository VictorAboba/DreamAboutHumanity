"""Rush bot: spends everything on the army and attacks wherever the estimate is >= 0.6.

Every turn it recruits the most it can pay for and feed (a farm first when food, not alloy, is the limit), sends each
army at its best adjacent foreign or neutral province with estimate >= 0.6 (the capital keeps 0-2 units, fixed per
bot by its seed), marches interior armies towards the border and fortifies what stays. In negotiation_2 it adds one
hasty attack if CP allow.
"""

from __future__ import annotations

from dah.bots.base import Bot, Estimator, Order
from dah.bots.tactics import (
    Reserved,
    advance,
    best_attack,
    capital_of,
    distance_to_foreign,
    fortify_orders,
    garrison,
    plan_moves,
    reserved_by_orders,
)
from dah.bots.view import (
    BUILD_RESOURCE,
    BUILDING_COST_ALLOY,
    RECRUIT_BUILDINGS,
    UNIT_COST_ALLOY,
    StateView,
    farm_site,
)

WIN_THRESHOLD = 0.6
RECRUIT_UNIT = "assault"
FOOD_TURNS = 2  # recruits stay fed for this many turns
GARRISONS = (0, 1, 2)  # units the capital keeps: one of these per bot (its seed picks)
P2_NEW_ATTACK_CP = 2  # registry default of p2.cost.new_attack

__all__ = ["RushBot", "advance", "best_attack", "distance_to_foreign"]


class RushBot(Bot):
    name = "rush"

    def __init__(self, seat: str, seed: int) -> None:
        super().__init__(seat, seed)
        self.garrison = self.rng.choice(GARRISONS)

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        orders = recruit_orders(view, view.resource(BUILD_RESOURCE), FOOD_TURNS, farm_first=True)
        moves = plan_moves(view, estimate, WIN_THRESHOLD, self.rng, garrison(capital_of(view), self.garrison))
        return orders + moves + fortify_orders(view, moves)

    def p2_orders(self, view: StateView, current: list[Order], cp_left: int, estimate: Estimator) -> list[Order]:
        if cp_left < P2_NEW_ATTACK_CP or any(o.get("kind") == "move" for o in current):
            return []
        attack = self._best_attack(view, reserved_by_orders(current), estimate, hasty=True)
        return [attack] if attack is not None else []

    def _best_attack(self, view: StateView, reserved: Reserved, estimate: Estimator, hasty: bool) -> Order | None:
        return best_attack(view, reserved, estimate, hasty, WIN_THRESHOLD, self.rng)


def recruit_orders(view: StateView, alloy: int, food_turns: int, *, farm_first: bool) -> list[Order]:
    """Recruit the most `alloy` pays for and the food keeps fed for `food_turns` turns; with `farm_first`, when food
    rather than alloy is the limit, lay a farm first (an agri world first)."""
    orders: list[Order] = []
    count = alloy // UNIT_COST_ALLOY[RECRUIT_UNIT]
    feedable = view.feedable(RECRUIT_UNIT, food_turns)
    if feedable is not None and feedable < count:
        site = farm_site(view)
        if farm_first and site is not None and alloy >= BUILDING_COST_ALLOY["farm"]:
            orders.append({"kind": "build", "province": site.id, "building": view.qualify("farm")})
            alloy -= BUILDING_COST_ALLOY["farm"]
        count = min(alloy // UNIT_COST_ALLOY[RECRUIT_UNIT], feedable)
    site = next((p for p in view.own_provinces() if RECRUIT_BUILDINGS & set(p.buildings)), None)
    if site is not None and count > 0:
        orders.append({"kind": "recruit", "province": site.id, "unit": view.qualify(RECRUIT_UNIT), "count": count})
    return orders
