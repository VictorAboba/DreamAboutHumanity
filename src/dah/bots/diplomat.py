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
- Stage 3 (docs/SPY_AND_CONQUEST.md section 7): never proposes a pact first to a seat whose Shade it caught with the
  owner named. From turn 2 it keeps 1-2 Shades (35 alloy each) reading the orders of its strongest neighbouring
  partner (3 rounds; the ledger while S < 2; the strongest neighbour when it has no partner). In negotiation_2, a
  `move` of that book into its land not marked as false gets a fortify there (1 CP); it recalls a sabotage aimed at a
  partner. One false entry in the last 2 turns, before the betrayal. Fates: occupations of a partner's own land are
  liberated to that partner; else subjugate what the provisions pay for (policy subjugate while 15 are free).
"""

from __future__ import annotations

from dah.bots import conquest as cq
from dah.bots import spy
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
    Province,
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
SHADE_FROM_TURN = 2
SHADE_MIN_ALLOY = 35
MAX_SHADES = 2
READ_ROUNDS = 3
P2_FORTIFY_CP = 1   # registry: a fortify given in negotiation_2
P2_RECALL_CP = 1    # registry default of p2.cost.recall


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
        self.feinted = False    # stage 3: one false entry per game, in the last turns

    def answer_pacts(self, view: StateView, api: DiplomacyApi) -> None:
        for prop in view.pact_proposals:
            if str(prop.get("to")) in view.me:
                api.respond_pact(str(prop["id"]), "accept")

    def diplomacy(self, view: StateView, api: DiplomacyApi) -> None:
        open_with = {str(p.get("from") if str(p.get("to")) in view.me else p.get("to")) for p in view.pact_proposals}
        self.answer_pacts(view, api)
        self.renew_expiring_pacts(view, api)
        alloy = view.resource(BUILD_RESOURCE)
        for other in neighbours(view):
            if view.is_partner(other) or other in open_with or self.proposed.get(other, 0) >= MAX_PROPOSALS:
                continue
            if other in self.memory.hostile:     # its Shade was caught spying on us (decision 22)
                continue
            api.propose_pact(other, None)
            self.proposed[other] = self.proposed.get(other, 0) + 1
            if alloy >= GIFT_MIN_ALLOY and self.rng.random() < GIFT_CHANCE:
                api.send_gift(other, {view.qualify(BUILD_RESOURCE): GIFT_ALLOY})
                alloy -= GIFT_ALLOY

    def action_orders(self, view: StateView, estimate: Estimator) -> list[Order]:
        extra, food_held, alloy_held = self._stage3(view)
        full = view
        view = spy.food_view(view, food_held, alloy_held)
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
        if view.conquest:
            keep = merge_reserved(keep, cq.occupation_garrison(view))
        moves += plan_moves(view, estimate, ATTACK_WIN, self.rng, keep, lambda t: not view.is_partner(t.owner))
        late = bool(view.turn_limit) and view.turn > view.turn_limit - BETRAY_TURNS
        if view.espionage is not None and not self.feinted and late:
            feint = spy.feint_order(full, merge_reserved(keep, reserved_by_orders(moves)), moves,
                                    lambda t: not view.is_partner(t.owner), self.rng)
            if feint is not None:
                self.feinted = True
                extra.append(feint)
        return orders + moves + fortify_orders(full, moves) + extra

    def _stage3(self, view: StateView) -> tuple[list[Order], int, int]:
        """Fates, policy, Shades and their missions: (orders, provisions held, alloy held)."""
        out: list[Order] = []
        food, alloy = view.free_food(), view.resource(BUILD_RESOURCE)
        if view.conquest:
            fates, spent = cq.occupation_orders(view, food, liberate_for=lambda p: self._liberate_to(view, p))
            food -= spent
            policy = cq.policy_order(view, food)
            out += fates + ([policy] if policy else [])
        if view.espionage is not None:
            hire = spy.hire_order(view, self.memory, from_turn=SHADE_FROM_TURN, max_shades=MAX_SHADES, alloy=alloy,
                                  min_alloy=SHADE_MIN_ALLOY)
            if hire is not None:
                out.append(hire)
                alloy -= spy.shade_cost()
            target = spy.strongest_neighbour(view, view.is_partner) or spy.strongest_neighbour(view)
            busy = {str((sh.get("mission") or {}).get("target")) for sh in spy.active_shades(view)}
            idle = spy.idle_shades(view)
            if idle and target is not None and target not in busy and spy.can_start(view):
                mission = "read_orders" if view.whisper >= spy.MISSIONS["read_orders"][0] else "ledger"
                got = spy.spy_order(view, idle[0], mission, target, READ_ROUNDS, alloy, food)
                if got is not None:
                    out.append(got[0])
                    alloy -= got[1]
                    food -= got[2]
        return out, view.free_food() - food, view.resource(BUILD_RESOURCE) - alloy

    @staticmethod
    def _liberate_to(view: StateView, prov: Province) -> str | None:
        """A partner's own land (its origin) goes back to that partner."""
        living = not view.players or prov.origin in view.players
        if prov.origin and prov.origin not in view.me and view.is_partner(prov.origin) and living:
            return prov.origin
        return None

    def p2_orders(self, view: StateView, current: list[Order], cp_left: int, estimate: Estimator) -> list[Order]:
        """Espionage games: fortify where a read book moves into our land (an entry not marked false); recall a
        sabotage aimed at a partner."""
        if view.espionage is None:
            return []
        out: list[Order] = []
        for sh in spy.active_shades(view):
            m = sh.get("mission") or {}
            target = view.provinces.get(str(m.get("target")))
            sabotage = str(m.get("type") or "").endswith("sabotage")
            if sabotage and target is not None and view.is_partner(target.owner) and cp_left >= P2_RECALL_CP:
                out.append({"kind": "recall", "shade": sh["id"]})
                cp_left -= P2_RECALL_CP
        fortified = {str(o.get("province")) for o in current if o.get("kind") == "fortify"}
        leaving = {str(o.get("from")) for o in current if o.get("kind") == "move" and not o.get("feint")}
        for seat in sorted(view.players or ()):
            for entry in self.memory.orders_of(seat, view.turn):
                dst = str(entry.get("to"))
                prov = view.provinces.get(dst)
                if (entry.get("kind") != "move" or entry.get("exposed") or prov is None or not view.is_mine(prov)
                        or dst in fortified or dst in leaving or cp_left < P2_FORTIFY_CP):
                    continue
                out.append({"kind": "fortify", "province": dst})
                fortified.add(dst)
                cp_left -= P2_FORTIFY_CP
        return out

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
