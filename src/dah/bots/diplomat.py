"""Diplomat bot: signs pacts with its neighbours, grows a mixed economy, attacks only outside its pacts.

- Negotiations: accepts every pact proposed to it, proposes an indefinite pact to each neighbour it has none with
  (twice at most to a seat that never accepted) and now and then sends a small gift of alloy with the proposal.
- Actions: a farm or a mine while the game is young (a farm whenever food limits the army), then recruits what it
  can pay for and feed; sends each army at the best adjacent province of a non-partner (or a neutral one) with
  estimate >= 0.65, marches interior armies towards the border (the capital keeps a garrison of 1-3) and fortifies
  every province whose army stays put. The garrison and how long it builds (to turn 3-5) are fixed per bot by its
  seed.
- Betrayal: in the last two turns it attacks a partner's province with "pact": "break" when the estimate shows a
  decisive win (>= 0.85), since a broken pact costs little once the game is about to end.
- Stage 2: prefers factions of command and cunning (synclite, captains, ash_choir), artifacts of command and income,
  researches logistics first (more command points), then industry; recruits one of its faction's unique unit per
  turn before assault.
"""

from __future__ import annotations

from dah.bots.base import Bot, DiplomacyApi, Estimator, Order
from dah.bots.builder import choose_build, total_upkeep
from dah.bots.tactics import (
    best_attack,
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
    UNIT_COST_ALLOY,
    StateView,
    farm_site,
)

ATTACK_WIN = 0.65
BETRAY_WIN = 0.85
BETRAY_TURNS = 2  # the last N turns of the game
# Builds a farm or mine only up to this turn (a farm later too, when food limits the army); one per bot, by its seed.
BUILD_UNTILS = (3, 4, 5)
GARRISONS = (1, 2, 3)  # units the capital keeps; one per bot
GIFT_CHANCE = 0.3
GIFT_ALLOY = 4
GIFT_MIN_ALLOY = 30  # never gifts when poorer than this
MAX_PROPOSALS = 2  # to one seat that never accepted
FOOD_TURNS = 3
RECRUIT_UNIT = "assault"
UNIQUE_PER_TURN = 1


class DiplomatBot(Bot):
    name = "diplomat"
    faction_style = ("synclite", "captains", "ash_choir", "litany", "growth")
    artifact_likes = ("seal_of_command", "tithe_chalice", "ore_vein", "trail_maps", "calm_banner")
    branch_priority = ("logistics", "industry", "mystery", "war")
    tech_first = ("treasury_order", "staff_litanies")

    def __init__(self, seat: str, seed: int) -> None:
        super().__init__(seat, seed)
        self.proposed: dict[str, int] = {}  # seat -> proposals made to it
        self.garrison = self.rng.choice(GARRISONS)
        self.build_until = self.rng.choice(BUILD_UNTILS)

    def diplomacy(self, view: StateView, api: DiplomacyApi) -> None:
        open_with: set[str] = set()
        for prop in view.pact_proposals:
            mine_to = str(prop.get("to")) in view.me
            open_with.add(str(prop.get("from") if mine_to else prop.get("to")))
            if mine_to:
                api.respond_pact(str(prop["id"]), "accept")
        alloy = view.resource(BUILD_RESOURCE)
        for other in neighbours(view):
            if view.is_partner(other) or other in open_with or self.proposed.get(other, 0) >= MAX_PROPOSALS:
                continue
            api.propose_pact(other, None)
            self.proposed[other] = self.proposed.get(other, 0) + 1
            if alloy >= GIFT_MIN_ALLOY and self.rng.random() < GIFT_CHANCE:
                api.send_gift(other, {view.qualify(BUILD_RESOURCE): GIFT_ALLOY})
                alloy -= GIFT_ALLOY

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        orders: list[Order] = []
        alloy = view.resource(BUILD_RESOURCE)
        feedable = view.feedable(RECRUIT_UNIT, FOOD_TURNS)
        build = None
        if feedable is not None and feedable < alloy // UNIT_COST_ALLOY[RECRUIT_UNIT]:
            site = farm_site(view)  # food, not alloy, limits the army
            build = (site, "farm") if site is not None else None
        elif view.turn <= self.build_until:
            build = choose_build(view, total_upkeep(view))
        if build is not None and alloy >= BUILDING_COST_ALLOY[build[1]]:
            province, building = build
            orders.append({"kind": "build", "province": province.id, "building": view.qualify(building)})
            alloy -= BUILDING_COST_ALLOY[building]

        site = recruit_site(view)
        unique, spent, unique_upkeep = unique_recruit(view, alloy, FOOD_TURNS, UNIQUE_PER_TURN)
        if unique is not None:
            orders.append(unique)
            alloy -= spent
        count = alloy // UNIT_COST_ALLOY[RECRUIT_UNIT]
        if feedable is not None:
            count = min(count, feedable - unique_upkeep)
        if site is not None and count > 0:
            orders.append(
                {"kind": "recruit", "province": site.id, "unit": view.qualify(RECRUIT_UNIT), "count": count}
            )

        betrayal = self._betrayal(view, estimate)
        moves = [betrayal] if betrayal is not None else []
        keep = merge_reserved(garrison(capital_of(view), self.garrison), reserved_by_orders(moves))
        moves += plan_moves(view, estimate, ATTACK_WIN, self.rng, keep, lambda t: not view.is_partner(t.owner))
        return orders + moves + fortify_orders(view, moves)

    def _betrayal(self, view: StateView, estimate: Estimator) -> Order | None:
        if not view.turn_limit or view.turn <= view.turn_limit - BETRAY_TURNS:
            return None
        attack = best_attack(view, {}, estimate, False, BETRAY_WIN, self.rng, lambda t: view.is_partner(t.owner))
        if attack is not None:
            attack["pact"] = "break"
        return attack


def neighbours(view: StateView) -> list[str]:
    """Living other seats that own a province next to one of ours."""
    seen: set[str] = set()
    for own in view.own_provinces():
        for p in view.neighbors(own):
            if p.owner is not None and not view.is_mine(p):
                seen.add(p.owner)
    living = set(view.players) if view.players else seen
    return sorted(seen & living)
