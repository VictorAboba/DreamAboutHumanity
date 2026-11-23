"""Builder bot: grows the economy, recruits from surplus, keeps the army home and fortifies the capital."""

from __future__ import annotations

from dah.bots.base import Bot, Estimator, Order
from dah.bots.view import (
    BUILD_RESOURCE,
    BUILDING_COST_ALLOY,
    FOOD_RESOURCE,
    RECRUIT_BUILDINGS,
    UNIT_COST_ALLOY,
    UNIT_UPKEEP_FOOD,
    Province,
    StateView,
)

GREETINGS = (
    "Приветствую, соседи. Я занят хозяйством и войны не ищу — предлагаю мир.",
    "Добрый день всем. Мои армии остаются дома; давайте жить спокойно.",
    "Мир вашим мирам. Я строю, а не воюю, и надеюсь на добрососедство.",
)
ALLOY_RESERVE = 20  # alloy kept unspent after building
FOOD_TURNS = 3  # turns of upkeep the builder keeps in stock
RECRUIT_UNIT = "assault"
PREFERRED_SITE = {"farm": "agri", "mine": "forge"}


class BuilderBot(Bot):
    name = "builder"

    def message(self, view: StateView) -> str | None:
        if view.turn <= 1 and view.phase == "negotiation_1":
            return self.rng.choice(GREETINGS)
        return None

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        orders: list[Order] = []
        alloy = view.resource(BUILD_RESOURCE)
        upkeep = _total_upkeep(view)

        build = self._choose_build(view, upkeep)
        if build is not None and alloy >= BUILDING_COST_ALLOY[build[1]]:
            province, building = build
            orders.append({"kind": "build", "province": province.id, "building": view.qualify(building)})
            alloy -= BUILDING_COST_ALLOY[building]

        recruit_site = next((p for p in view.own_provinces() if RECRUIT_BUILDINGS & set(p.buildings)), None)
        food_margin = view.resource(FOOD_RESOURCE) - upkeep * FOOD_TURNS
        affordable = (alloy - ALLOY_RESERVE) // UNIT_COST_ALLOY[RECRUIT_UNIT]
        feedable = food_margin // (UNIT_UPKEEP_FOOD[RECRUIT_UNIT] * FOOD_TURNS)
        count = min(affordable, feedable)
        if recruit_site is not None and count > 0:
            orders.append(
                {"kind": "recruit", "province": recruit_site.id, "unit": view.qualify(RECRUIT_UNIT), "count": count}
            )

        capital = next((p for p in view.own_provinces() if p.kind == "capital"), None)
        if capital is not None:
            orders.append({"kind": "fortify", "province": capital.id})
        return orders

    def _choose_build(self, view: StateView, upkeep: int) -> tuple[Province, str] | None:
        own = view.own_provinces()
        farms = sum("farm" in p.buildings for p in own)
        mines = sum("mine" in p.buildings for p in own)
        hungry = view.resource(FOOD_RESOURCE) < upkeep * FOOD_TURNS
        for building in ("farm", "mine") if hungry or farms < mines else ("mine", "farm"):
            sites = [p for p in own if building not in p.buildings]
            if sites:
                preferred = [p for p in sites if p.kind == PREFERRED_SITE[building]]
                return (preferred or sites)[0], building
        return None


def _total_upkeep(view: StateView) -> int:
    return sum(UNIT_UPKEEP_FOOD.get(unit, 1) * n for p in view.own_provinces() for unit, n in (p.units or {}).items())
