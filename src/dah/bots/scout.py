"""Scout bot (stage 5, CONCEPT decision 25 a): a builder that sweeps the Abandoned lands with single cheap units.

It plays as the builder, except that every Abandoned province next to its land that may still hold a hidden find gets
one unit (the cheapest free one; the capital's garrison stays) every turn, and no bigger army goes there; while such
provinces lie next to it, it also recruits up to SWEEP_RECRUITS Militia a turn for the sweep. It exists to measure the
"scout sweep" strategy: decision 25 wants hindrances serious enough that this does not pay (the simulator's
`--scout-sweep` plays it against the builder on the same seeds).
"""

from __future__ import annotations

from dah.bots import finds, spy
from dah.bots.base import Estimator, Order
from dah.bots.builder import BuilderBot
from dah.bots.tactics import ArmyOk, Reserved, free_units, merge_reserved, recruit_site
from dah.bots.view import BUILD_RESOURCE, UNIT_COST_ALLOY, StateView

SWEEP_UNIT = "militia"
SWEEP_RECRUITS = 2


class ScoutBot(BuilderBot):
    name = "scout"

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        targets = finds.sweep_targets(view, self.finds)
        site = recruit_site(view)
        price = UNIT_COST_ALLOY[SWEEP_UNIT]
        count = min(SWEEP_RECRUITS, len(targets), view.resource(BUILD_RESOURCE) // price)
        if site is None or count <= 0:
            return super().action_orders(view, estimate)
        recruit = {"kind": "recruit", "province": site.id, "unit": view.qualify(SWEEP_UNIT), "count": count}
        return [recruit, *super().action_orders(spy.food_view(view, 0, count * price), estimate)]

    def pre_moves(self, view: StateView, keep: Reserved) -> list[Order]:
        """One unit (the cheapest) into every Abandoned province next to the seat's land that may hold a find."""
        reserved = merge_reserved(keep)
        out: list[Order] = []
        for target in finds.sweep_targets(view, self.finds):
            sources = [p for p in view.neighbors(target) if view.is_mine(p) and free_units(p, reserved)]
            if not sources:
                continue
            source = max(sources, key=lambda p: (sum(free_units(p, reserved).values()), p.id))
            free = free_units(source, reserved)
            unit = min(free, key=lambda u: (UNIT_COST_ALLOY.get(u, 99), u))
            reserved.setdefault(source.id, {})[unit] = reserved.get(source.id, {}).get(unit, 0) + 1
            out.append({"kind": "move", "from": source.id, "to": target.id, "units": {view.qualify(unit): 1}})
        return out

    def army_ok(self) -> ArmyOk:
        base = super().army_ok()
        return lambda source, target, free: not finds.may_hold_find(target, self.finds) and base(source, target, free)
