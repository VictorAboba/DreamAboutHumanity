"""Builder bot: grows the economy first, recruits what it can feed, takes only sure fights and fortifies what it holds.

Each turn while the game is young it lays a farm or a mine (a farm when food runs short), recruits from what is left,
sends each army at an adjacent province only when the estimate is >= 0.75 (>= 0.6 once it has stopped building),
marches interior armies towards the border (the capital keeps a garrison of 1-3) and fortifies every province whose
army stays put. The garrison and when it stops building (3-5 turns before the end) are fixed per bot by its seed.
Peace suits it: it accepts every pact offered to it, keeps its pacts and never attacks a partner.
Stage 2: it prefers economic factions (litany, ash_choir, growth) and artifacts of income, researches industry first
(then war: fortification), lays one temple once it has a mine (when the game has techs), recruits one of its faction's
unique unit per turn before assault, and puts a citadel in its capital once fortification is known and alloy is
plenty.
"""

from __future__ import annotations

from dah.bots.base import Bot, DiplomacyApi, Estimator, Order
from dah.bots.tactics import capital_of, fortify_orders, garrison, plan_moves, recruit_site, unique_recruit
from dah.bots.view import (
    BUILD_RESOURCE,
    BUILDING_COST_ALLOY,
    FOOD_RESOURCE,
    RESEARCH_BUILDING,
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
FOOD_TURNS = 3  # turns of upkeep the builder keeps fed
FOOD_SPARE = 2  # net food income the builder wants to keep for new recruits
SURE_WIN = 0.75  # attacks only when the estimate is at least this
LATE_WIN = 0.6  # ... and this once it has stopped building
GARRISONS = (1, 2, 3)  # units the capital keeps: one of these per bot (its seed picks)
BUILD_STOPS = (3, 4, 5)  # no new buildings in the last N turns (they would not pay back); one per bot
RECRUIT_UNIT = "assault"
PREFERRED_SITE = {"farm": "agri", "mine": "forge", RESEARCH_BUILDING: "shrine"}
UNIQUE_PER_TURN = 1
CITADEL = "citadel"
CITADEL_TECH = "fortification"
CITADEL_SPARE = 12  # alloy left after a citadel (one assault)


class BuilderBot(Bot):
    name = "builder"
    faction_style = ("litany", "ash_choir", "growth", "captains", "synclite")
    artifact_likes = ("ore_vein", "tithe_chalice", "citadel_keystone", "ancient_blueprint", "smith_relic")
    branch_priority = ("industry", "war", "mystery", "logistics")
    tech_first = ("deep_drilling", "agro_vats", "fortification")

    def __init__(self, seat: str, seed: int) -> None:
        super().__init__(seat, seed)
        self.garrison = self.rng.choice(GARRISONS)
        self.build_stop = self.rng.choice(BUILD_STOPS)

    def message(self, view: StateView) -> str | None:
        if view.turn <= 1 and view.phase == "negotiation_1":
            return self.rng.choice(GREETINGS)
        return None

    def diplomacy(self, view: StateView, api: DiplomacyApi) -> None:
        """Accept every pact offered (and keep it); propose none."""
        for prop in view.pact_proposals:
            if str(prop.get("to")) in view.me:
                api.respond_pact(str(prop["id"]), "accept")

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        orders: list[Order] = []
        alloy = view.resource(BUILD_RESOURCE)
        upkeep = total_upkeep(view)
        late = bool(view.turn_limit) and view.turn > view.turn_limit - self.build_stop

        build = None if late else self._choose_build(view, upkeep)
        if build is not None and alloy >= BUILDING_COST_ALLOY[build[1]]:
            province, building = build
            orders.append({"kind": "build", "province": province.id, "building": view.qualify(building)})
            alloy -= BUILDING_COST_ALLOY[building]
        citadel = None if late else citadel_order(view, alloy, {o["province"] for o in orders})
        if citadel is not None:
            orders.append(citadel)
            alloy -= BUILDING_COST_ALLOY[CITADEL]

        site = recruit_site(view)
        unique, spent, unique_upkeep = unique_recruit(view, alloy, FOOD_TURNS, UNIQUE_PER_TURN)
        if unique is not None:
            orders.append(unique)
            alloy -= spent
        affordable = alloy // UNIT_COST_ALLOY[RECRUIT_UNIT]
        feedable = view.feedable(RECRUIT_UNIT, FOOD_TURNS)
        if feedable is None:  # no net income known: keep FOOD_TURNS of the whole upkeep in stock
            food_margin = view.resource(FOOD_RESOURCE) - upkeep * FOOD_TURNS
            feedable = food_margin // (UNIT_UPKEEP_FOOD[RECRUIT_UNIT] * FOOD_TURNS)
        count = min(affordable, feedable - unique_upkeep)
        if site is not None and count > 0:
            orders.append(
                {"kind": "recruit", "province": site.id, "unit": view.qualify(RECRUIT_UNIT), "count": count}
            )

        threshold = LATE_WIN if late else SURE_WIN
        moves = plan_moves(view, estimate, threshold, self.rng, garrison(capital_of(view), self.garrison),
                           lambda target: not view.is_partner(target.owner))
        return orders + moves + fortify_orders(view, moves)

    def _choose_build(self, view: StateView, upkeep: int) -> tuple[Province, str] | None:
        return choose_build(view, upkeep, temple=True)


def citadel_order(view: StateView, alloy: int, busy: set[str]) -> Order | None:
    """A citadel in the capital once the tech that opens it is known and alloy covers it with some to spare."""
    capital = capital_of(view)
    if (view.techs is None or CITADEL_TECH not in view.techs or capital is None or CITADEL in capital.buildings
            or capital.id in busy or alloy < BUILDING_COST_ALLOY[CITADEL] + CITADEL_SPARE):
        return None
    return {"kind": "build", "province": capital.id, "building": view.qualify(CITADEL)}


def choose_build(view: StateView, upkeep: int, *, temple: bool = False) -> tuple[Province, str] | None:
    """Next farm or mine on its preferred site: a farm first when food is short (net food income below FOOD_SPARE
    and the stock below 2 x FOOD_TURNS turns of upkeep; without net income known: the stock below FOOD_TURNS turns of
    upkeep, or farms lag behind mines), else a mine. With `temple` (and techs in the game), one temple comes before
    the next mine once the seat has a mine and food is not short."""
    own = view.own_provinces()
    if (temple and view.research_enabled and not any(RESEARCH_BUILDING in p.buildings for p in own)
            and any("mine" in p.buildings for p in own)):
        hungry_now = view.resource(FOOD_RESOURCE) < upkeep * FOOD_TURNS
        if view.net_income is not None:
            hungry_now = (view.net_income.get(FOOD_RESOURCE, 0) < FOOD_SPARE
                          and view.resource(FOOD_RESOURCE) < upkeep * 2 * FOOD_TURNS)
        if not hungry_now and own:
            preferred = [p for p in own if p.kind == PREFERRED_SITE[RESEARCH_BUILDING]]
            capital = capital_of(view)
            return (preferred or ([capital] if capital else []) or own)[0], RESEARCH_BUILDING
    hungry = view.resource(FOOD_RESOURCE) < upkeep * FOOD_TURNS
    if view.net_income is not None:  # food income decides; the stock only when it is running out
        low_income = view.net_income.get(FOOD_RESOURCE, 0) < FOOD_SPARE
        hungry = low_income and view.resource(FOOD_RESOURCE) < upkeep * 2 * FOOD_TURNS
        first = ("farm", "mine") if hungry else ("mine", "farm")
    else:
        farms = sum("farm" in p.buildings for p in own)
        mines = sum("mine" in p.buildings for p in own)
        first = ("farm", "mine") if hungry or farms < mines else ("mine", "farm")
    for building in first:
        sites = [p for p in own if building not in p.buildings]
        if sites:
            preferred = [p for p in sites if p.kind == PREFERRED_SITE[building]]
            return (preferred or sites)[0], building
    return None


def total_upkeep(view: StateView) -> int:
    return sum(UNIT_UPKEEP_FOOD.get(unit, 1) * n for p in view.own_provinces() for unit, n in (p.units or {}).items())
