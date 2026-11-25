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
Stage 3 (docs/SPY_AND_CONQUEST.md section 7): after the economy techs it researches the «Зеркала» branch
(mirror_wards, watch_drill); from turn 3 it hires one Shade (with 35 alloy, before building) and reads the ledger of its
strongest neighbour (core 0.6.0: it holds one Shade from the start, and first reads that neighbour's secret objective
once, mission `secret`). Fates: the policy is subjugate while 15 provisions are free; it subjugates the occupations it can
pay for and holds the rest (one unit stays in each) until it can.
Stage 4 (docs/SCORING.md section 10, dah.bots.score): with great works on it saves provisions for the Monument (held
back from recruiting while they reach the price in time) and lays it once the free provisions cover it and still feed
the army 3 turns, and the Spire after a mine and a temple, outside the capital, in its most
protected allowed province. Its objective makes the provinces that advance it preferred targets (and keeps one unit
in those held) or its branch first in research.
Stage 5 (docs/ABANDONED_LANDS.md 14.1, dah.bots.finds): cautious in Abandoned land: only an army of 4 units or more
goes where a find may lie; the ruins' guard and a find's neutrals at estimate >= 0.8 (a lair >= 0.95); mixed finds by
its rank on the visible score.
"""

from __future__ import annotations

from dah.bots import conquest as cq
from dah.bots import finds, score, spy
from dah.bots.base import Bot, DiplomacyApi, Estimator, Order
from dah.bots.tactics import (
    ArmyOk,
    Reserved,
    capital_of,
    fortify_orders,
    garrison,
    merge_reserved,
    plan_moves,
    recruit_site,
    reserved_by_orders,
    unique_recruit,
)
from dah.bots.view import (
    BUILD_RESOURCE,
    BUILDING_COST_ALLOY,
    FOOD_RESOURCE,
    RESEARCH_BUILDING,
    UNIT_COST_ALLOY,
    UNIT_UPKEEP_FOOD,
    Province,
    StateView,
    farm_site,
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
SHADE_FROM_TURN = 3
SHADE_MIN_ALLOY = 35
LEDGER_ROUNDS = 2
SECRET_ROUNDS = 2      # core 0.6.0: the secret objective mission (one report is enough)
MONUMENT = "monument"   # stage 4: great works (docs/SCORING.md 10)
SPIRE = "dream_spire"
FIND_ARMY = 4           # stage 5: an army into Abandoned land that may hold a find (dah.bots.finds)
GUARD_MARGIN = 0.05     # the ruins' guard, a find's neutrals: estimate >= 0.8 (late: 0.65)
LAIR_MARGIN = 0.2       # a lair: >= 0.95 (late: 0.8)


class BuilderBot(Bot):
    name = "builder"
    faction_style = ("litany", "ash_choir", "growth", "captains", "synclite")
    artifact_likes = ("ore_vein", "tithe_chalice", "citadel_keystone", "ancient_blueprint", "smith_relic")
    branch_priority = ("industry", "mirrors", "war", "mystery", "logistics")
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
        """Accept every pact offered (and keep it); propose none, but renew a fixed pact in its last turn."""
        self.answer_pacts(view, api)
        self.renew_expiring_pacts(view, api)

    def answer_pacts(self, view: StateView, api: DiplomacyApi) -> None:
        for prop in view.pact_proposals:
            if str(prop.get("to")) in view.me:
                api.respond_pact(str(prop["id"]), "accept")

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        extra, food_held, alloy_held = self._stage3(view)
        full = view
        view = spy.food_view(view, food_held, alloy_held)
        works, view = score.lay_works(view, self._works(view), FOOD_TURNS)
        early = not self._core_economy(view) and not score.wants_work(view)    # its objective: save at once
        # core 0.7.0: with the Monument standing, the builder saves for the Spire (paid in parts: the stock needs only
        # what makes every share payable with the income)
        goal = SPIRE if score.has_work(view, MONUMENT) and self._core_economy(view) else MONUMENT
        saving = None if works or early else score.saving_rate(view, goal, FOOD_TURNS)
        if saving is not None:  # saving for a work: the stock is kept, recruits eat only what income leaves
            view = spy.food_view(view, view.resource(FOOD_RESOURCE))
        orders: list[Order] = list(works)
        alloy = view.resource(BUILD_RESOURCE)
        upkeep = total_upkeep(view)
        late = bool(view.turn_limit) and view.turn > view.turn_limit - self.build_stop

        build = None if late else self._choose_build(view, upkeep)
        if saving is not None and not late and (view.net_income or {}).get(FOOD_RESOURCE, 0) < saving:
            # a saver short of income lays a farm first (its alloy is idle anyway)
            build = next(((p, "farm") for p in [farm_site(view)] if p is not None), build)
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
        if saving is not None:
            count = min(count, (view.net_income or {}).get(FOOD_RESOURCE, 0) - saving - unique_upkeep)
        if site is not None and count > 0:
            orders.append(
                {"kind": "recruit", "province": site.id, "unit": view.qualify(RECRUIT_UNIT), "count": count}
            )

        threshold = LATE_WIN if late else SURE_WIN
        keep = merge_reserved(garrison(capital_of(view), self.garrison), score.objective_garrison(view))
        if view.conquest:
            keep = merge_reserved(keep, cq.occupation_garrison(view))
        pre = self.pre_moves(view, keep)        # the scout's sweep (none for the builder)
        keep = merge_reserved(keep, reserved_by_orders(pre))
        bonus = finds.merge_bonus(score.attack_bonus(view),
                                  finds.attack_bonus(view, self.finds, GUARD_MARGIN, LAIR_MARGIN))
        moves = pre + plan_moves(view, estimate, threshold, self.rng, keep,
                                 lambda target: not view.is_partner(target.owner), bonus, army_ok=self.army_ok())
        return orders + moves + fortify_orders(full, moves) + extra

    def pre_moves(self, view: StateView, keep: Reserved) -> list[Order]:
        """Moves planned before the armies' targets (stage 5: the scout bot's sweep); none for the builder."""
        return []

    def army_ok(self) -> ArmyOk:
        return finds.army_ok(self.finds, FIND_ARMY)

    @staticmethod
    def _core_economy(view: StateView) -> bool:
        """A mine and, in games with techs, a temple: the builder saves for a work only after these."""
        own = view.full_provinces()
        return any("mine" in p.buildings for p in own) and (
            not view.research_enabled or any(RESEARCH_BUILDING in p.buildings for p in own))

    @staticmethod
    def _works(view: StateView) -> list[tuple[str, bool]]:
        """Stage 4: the Monument as soon as the provisions allow; the Spire (not in the capital) once the seat has a
        mine and, in games with techs, a temple."""
        works = [(MONUMENT, True)]
        if BuilderBot._core_economy(view):
            works.append((SPIRE, False))
        return works

    def _stage3(self, view: StateView) -> tuple[list[Order], int, int]:
        """Fates, policy, the Shade and its ledger mission: (orders, provisions held, alloy held)."""
        out: list[Order] = []
        food, alloy = view.free_food(), view.resource(BUILD_RESOURCE)
        if view.conquest:
            fates, spent = cq.occupation_orders(view, food)
            food -= spent
            policy = cq.policy_order(view, food)
            out += fates + ([policy] if policy else [])
        if view.espionage is not None:
            hire = spy.hire_order(view, self.memory, from_turn=SHADE_FROM_TURN, max_shades=1, alloy=alloy,
                                  min_alloy=SHADE_MIN_ALLOY)
            if hire is not None:
                out.append(hire)
                alloy -= spy.shade_cost()
            idle = spy.idle_shades(view)
            target = spy.rival_target(view)       # core 0.7.0: a far rival too while intel points are left
            if idle and target is not None and spy.can_start(view):
                mission = spy.SECRET if spy.secret_wanted(view, self.memory, target) else "ledger"
                got = spy.spy_order(view, idle[0], mission, target, SECRET_ROUNDS if mission == spy.SECRET
                                    else LEDGER_ROUNDS, alloy, food)
                if got is not None:
                    out.append(got[0])
                    alloy -= got[1]
                    food -= got[2]
        return out, view.free_food() - food, view.resource(BUILD_RESOURCE) - alloy

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
    own = view.full_provinces()
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
