"""Rush bot: spends everything on the army and attacks wherever the estimate is >= 0.6.

Every turn it recruits the most it can pay for and feed (up to two of its faction's unique unit first; a farm first
when food, not alloy, is the limit), sends each
army at its best adjacent foreign or neutral province with estimate >= 0.6 (the capital keeps 0-2 units, fixed per
bot by its seed), marches interior armies towards the border and fortifies what stays. In negotiation_2 it adds one
hasty attack if CP allow.
Stage 2: it prefers aggressive factions (captains, growth, synclite), artifacts that give armies, and researches war
first (then logistics).
Stage 3 (docs/SPY_AND_CONQUEST.md section 7): from turn 2 it hires its first Shade as soon as it has the price
(before recruiting; until then it keeps back what next turn's income lacks for it: m6, hired by turn 3-4, not 5-8)
and a replacement for a lost one with 35 alloy;
sabotages the province it attacks when S >= 2 (an enemy one next to it otherwise), else watches its next target
(observe, 2 rounds; a province the fog hides first). In negotiation_2 a found window (its strike landed) gets a hasty
attack when CP allow. In 30% of turns it writes one false entry at a neighbour it does not attack. Fates: an enemy
capital it attacks gets a directive (subjugate when the provisions cover it, else exterminate); the policy is
subjugate while 15 provisions are free; it subjugates the occupations it can pay for and raids those it could not pay
for within 2 turns; one unit stays in every occupation.
Stage 4 (docs/SCORING.md section 10, dah.bots.score): war scores by itself; while the war cap is not reached a seat's
province gets a small bonus over a neutral one. Its objective makes the provinces that advance it preferred targets
(one unit stays in those held), puts its branch first in research, or (great_work) has it save for the Monument
(recruits only what the net income leaves) and lay it.
"""

from __future__ import annotations

from dah.bots import conquest as cq
from dah.bots import score, spy
from dah.bots.base import Bot, Estimator, Order
from dah.bots.tactics import (
    Reserved,
    advance,
    best_attack,
    capital_of,
    distance_to_foreign,
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
    UNIT_COST_ALLOY,
    Province,
    StateView,
    farm_site,
)

WIN_THRESHOLD = 0.6
RECRUIT_UNIT = "assault"
FOOD_TURNS = 2  # recruits stay fed for this many turns
GARRISONS = (0, 1, 2)  # units the capital keeps: one of these per bot (its seed picks)
P2_NEW_ATTACK_CP = 2  # registry default of p2.cost.new_attack
UNIQUE_PER_TURN = 2  # unique units recruited per turn at most
SHADE_FROM_TURN = 2
SHADE_MIN_ALLOY = 35  # a replacement Shade; the first needs only its price, saved for (m6: 35 was never reached)
OBSERVE_ROUNDS = 2
SABOTAGE_ROUNDS = 2
FEINT_CHANCE = 0.3
MONUMENT = "monument"   # stage 4: built only for a great_work objective
WINDOW_WIN = 0.5  # a hasty attack into a struck province needs this estimate (the strike is not counted in it)

__all__ = ["RushBot", "advance", "best_attack", "distance_to_foreign"]


class RushBot(Bot):
    name = "rush"
    faction_style = ("captains", "growth", "synclite", "ash_choir", "litany")
    artifact_likes = ("mercenary_band", "mercenary_contract", "calm_banner", "war_codex", "seal_of_command")
    branch_priority = ("war", "logistics", "industry", "mystery")
    tech_first = ("drill_doctrine", "surprise_doctrine")

    def __init__(self, seat: str, seed: int) -> None:
        super().__init__(seat, seed)
        self.garrison = self.rng.choice(GARRISONS)

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        keep = merge_reserved(garrison(capital_of(view), self.garrison), score.objective_garrison(view))
        if view.conquest:
            keep = merge_reserved(keep, cq.occupation_garrison(view))
        moves = plan_moves(view, estimate, WIN_THRESHOLD, self.rng, keep, bonus=score.attack_bonus(view, war=True))
        extra, food, alloy = self._stage3(view, moves, keep)
        plan = spy.food_view(view, food, alloy)
        works: list[Order] = []
        saving = None
        if score.wants_work(plan):      # stage 4: only its objective makes it save for and build a great work
            works, plan = score.lay_works(plan, [(MONUMENT, True)], FOOD_TURNS)
            saving = None if works else score.saving_rate(plan, MONUMENT, FOOD_TURNS)
            if saving is not None:
                plan = spy.food_view(plan, plan.resource(FOOD_RESOURCE))
        cap = None if saving is None else (plan.net_income or {}).get(FOOD_RESOURCE, 0) - saving
        orders = recruit_orders(plan, plan.resource(BUILD_RESOURCE), FOOD_TURNS, farm_first=True,
                                unique_max=UNIQUE_PER_TURN, food_cap=cap)
        return works + orders + moves + fortify_orders(view, moves) + extra

    def _stage3(self, view: StateView, moves: list[Order], keep: Reserved) -> tuple[list[Order], int, int]:
        """Conquest and espionage orders of the actions phase: (orders, provisions held, alloy held)."""
        out: list[Order] = []
        food, alloy = view.free_food(), view.resource(BUILD_RESOURCE)
        if view.conquest:
            directives, spent = cq.capital_directives(view, moves, food)
            fates, spent2 = cq.occupation_orders(view, food - spent, raid_short=True)
            food -= spent + spent2
            policy = cq.policy_order(view, food)
            out += directives + fates + ([policy] if policy else [])
        if view.espionage is not None:
            hire = spy.hire_order(view, self.memory, from_turn=SHADE_FROM_TURN, max_shades=1, alloy=alloy,
                                  min_alloy=0 if spy.first_shade(view, self.memory) else SHADE_MIN_ALLOY)
            if hire is not None:
                out.append(hire)
                alloy -= spy.shade_cost()
            mission = self._mission(view, moves, alloy, food)
            if mission is not None:
                out.append(mission[0])
                alloy -= mission[1]
                food -= mission[2]
            if hire is None:
                alloy -= spy.shade_savings(view, self.memory, from_turn=SHADE_FROM_TURN, alloy=alloy)
            if self.rng.random() < FEINT_CHANCE:
                attacked = {view.provinces[str(m["to"])].owner for m in moves if str(m["to"]) in view.provinces}
                feint = spy.feint_order(view, keep, moves, lambda t: t.owner not in attacked, self.rng)
                if feint is not None:
                    out.append(feint)
        return out, view.free_food() - food, view.resource(BUILD_RESOURCE) - alloy

    def _mission(self, view: StateView, moves: list[Order], alloy: int, food: int) -> tuple[Order, int, int] | None:
        idle = spy.idle_shades(view)
        if not idle or not spy.can_start(view):
            return None

        def enemy(p: Province) -> bool:
            return not view.is_partner(p.owner)

        if view.whisper >= spy.MISSIONS["sabotage"][0]:
            attacked = [view.provinces[str(m["to"])] for m in moves if str(m["to"]) in view.provinces]
            target = next((p for p in attacked if p.owner is not None and not view.is_mine(p) and enemy(p)), None)
            target = target or spy.enemy_target(view, enemy)
            if target is not None:
                got = spy.spy_order(view, idle[0], "sabotage", target.id, SABOTAGE_ROUNDS, alloy, food)
                if got is not None:
                    return got
        target = spy.hidden_target(view, enemy) or spy.enemy_target(view, enemy)
        if target is None:
            return None
        return spy.spy_order(view, idle[0], "observe", target.id, OBSERVE_ROUNDS, alloy, food)

    def p2_orders(self, view: StateView, current: list[Order], cp_left: int, estimate: Estimator) -> list[Order]:
        if cp_left < P2_NEW_ATTACK_CP:
            return []
        window = self._window_attack(view, current, estimate)
        if window is not None:
            return [window]
        if any(o.get("kind") == "move" and not o.get("feint") for o in current):
            return []
        attack = self._best_attack(view, reserved_by_orders(current), estimate, hasty=True)
        return [attack] if attack is not None else []

    def _window_attack(self, view: StateView, current: list[Order], estimate: Estimator) -> Order | None:
        """A hasty attack into a province our sabotage struck in this turn's recon (its fortify is denied)."""
        struck = self.memory.struck(view.turn)
        if not struck:
            return None
        if any(o.get("kind") == "move" and not o.get("feint") and str(o.get("to")) in struck for o in current):
            return None
        reserved = reserved_by_orders(current)
        best: tuple[float, Order] | None = None
        for pid in sorted(struck):
            target = view.provinces.get(pid)
            if target is None or view.is_mine(target):
                continue
            for source in view.neighbors(target):
                if not view.is_mine(source):
                    continue
                taken = reserved.get(source.id, {})
                free = {u: n - taken.get(u, 0) for u, n in sorted((source.units or {}).items())}
                free = {u: n for u, n in free.items() if n > 0}
                if not free:
                    continue
                win = estimate(free, target.units or {}, target.type, False, True)
                if win >= WINDOW_WIN and (best is None or win > best[0]):
                    best = (win, {"kind": "move", "from": source.id, "to": pid,
                                  "units": {view.qualify(u): n for u, n in free.items()}})
        return best[1] if best else None

    def _best_attack(self, view: StateView, reserved: Reserved, estimate: Estimator, hasty: bool) -> Order | None:
        return best_attack(view, reserved, estimate, hasty, WIN_THRESHOLD, self.rng)


def recruit_orders(view: StateView, alloy: int, food_turns: int, *, farm_first: bool,
                   unique_max: int = 0, food_cap: int | None = None) -> list[Order]:
    """Recruit the most `alloy` pays for and the food keeps fed for `food_turns` turns; with `farm_first`, when food
    rather than alloy is the limit, lay a farm first (an agri world first). Up to `unique_max` of the faction's unique
    unit come first, assault with the rest. `food_cap` (stage 4, saving for a work): at most this much new upkeep."""
    orders: list[Order] = []
    count = alloy // UNIT_COST_ALLOY[RECRUIT_UNIT]
    feedable = view.feedable(RECRUIT_UNIT, food_turns)
    if food_cap is not None:
        feedable = max(0, min(feedable if feedable is not None else food_cap, food_cap))
    if feedable is not None and feedable < count:
        site = farm_site(view)
        if farm_first and site is not None and alloy >= BUILDING_COST_ALLOY["farm"]:
            orders.append({"kind": "build", "province": site.id, "building": view.qualify("farm")})
            alloy -= BUILDING_COST_ALLOY["farm"]
        count = min(alloy // UNIT_COST_ALLOY[RECRUIT_UNIT], feedable)
    unique, spent, upkeep = unique_recruit(view, alloy, food_turns, unique_max)
    if unique is not None:
        orders.append(unique)
        alloy -= spent
        count = alloy // UNIT_COST_ALLOY[RECRUIT_UNIT]
        if feedable is not None:
            count = min(count, max(0, feedable - upkeep))
    site = recruit_site(view)
    if site is not None and count > 0:
        orders.append({"kind": "recruit", "province": site.id, "unit": view.qualify(RECRUIT_UNIT), "count": count})
    return orders
