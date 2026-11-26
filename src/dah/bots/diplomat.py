"""Diplomat bot: signs pacts with its neighbours, grows a mixed economy, attacks only outside its pacts.

- Negotiations: accepts every pact proposed to it, proposes an indefinite pact to each neighbour it has none with
  (twice at most to a seat that never accepted) and now and then sends a small gift of alloy with the proposal.
- Actions: a farm or a mine while the game is young (a farm whenever food limits the army), then recruits what it
  can pay for and feed; sends each army at the best adjacent province of a non-partner (or a neutral one) with
  estimate >= 0.65, marches interior armies towards the border (the capital keeps a garrison of 1-3) and fortifies
  every province whose army stays put. The garrison and how long it builds (to turn 3-5) are fixed per bot by its
  seed.
- Betrayal: in the last two turns it attacks a partner's province with "pact": "break" when the estimate shows a
  decisive win (>= 0.85), since a broken pact costs little once the game is about to end; in a scored game (core
  0.6.0: a break costs points) only a province worth more than the penalty (a capital, or one its objective needs).
- Stage 2: prefers factions of command and cunning (synclite, captains, ash_choir), artifacts of command and income,
  researches logistics first (more command points), then industry; recruits one of its faction's unique unit per
  turn before assault.
- Stage 3 (docs/SPY_AND_CONQUEST.md section 7): never proposes a pact first to a seat whose Shade it caught with the
  owner named. From turn 2 it keeps 1-2 Shades (the first as soon as it has the price, keeping back what next turn's
  income lacks for it; the second and replacements with 35 alloy) reading the orders of its strongest neighbouring
  partner (3 rounds; the ledger while S < 2; the strongest neighbour when it has no partner). In negotiation_2, a
  `move` of that book into its land not marked as false gets a fortify there (1 CP); it recalls a sabotage aimed at a
  partner. One false entry in the last 2 turns, before the betrayal. Core 0.6.0: it holds one Shade from the start and
  first reads the secret objective of that seat (mission `secret`, 2 rounds) once. Fates: occupations of a partner's own land are
  liberated to that partner; else subjugate what the provisions pay for (policy subjugate while 15 are free).
- Stage 4 (docs/SCORING.md section 10, dah.bots.score): with great works on it saves provisions after its building
  turns (from turn 1 for a great_work objective; recruits only what the net income leaves) and lays the Monument once the free provisions cover it and still
  feed the army 3 turns; with science scoring it lays a temple once it has a mine. Its objective
  makes the provinces that advance it preferred targets (and keeps one unit in those held) or its branch first.
- Stage 5 (docs/ABANDONED_LANDS.md 14.1, dah.bots.finds): Abandoned land that may hold a find only with 4 units or
  more; guards and a find's neutrals at estimate >= 0.75 (a lair >= 0.9); mixed finds by its rank.
- Core 0.8.0 (docs/SPY_V2.md 10): one Shade reads the orders of its strongest neighbouring partner every
  PARTNER_EVERY turns (no points against a partner: it reads for the pact-break line); the other works a rival (its
  secret, its orders once for the point, then its ledger before a war or a pact). A break against it seen in
  negotiation_2 gets the fortify and a public warning naming the breaker.
"""

from __future__ import annotations

from dah.bots import conquest as cq
from dah.bots import finds, score, spy
from dah.bots import view as bv
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
    FOOD_RESOURCE,
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
SHADE_MIN_ALLOY = 28   # the second Shade and replacements (balance core 0.8.0: was 35); the first: its price (m6)
MAX_SHADES = 2
READ_ROUNDS = 3
SECRET_ROUNDS = 2      # core 0.6.0: the secret objective mission (one report is enough)
PARTNER_EVERY = 2      # core 0.8.0: the partner's orders are read every this many turns (1 round)
LEDGER_ROUNDS = 1
P2_FORTIFY_CP = 1   # registry: a fortify given in negotiation_2
P2_RECALL_CP = 1    # registry default of p2.cost.recall
MONUMENT = "monument"   # stage 4 (docs/SCORING.md 10)
SPIRE = "dream_spire"   # core 0.7.0: paid in parts, within reach of the diplomat's income
FIND_ARMY = 4           # stage 5: an army into Abandoned land that may hold a find (dah.bots.finds)
GUARD_MARGIN = 0.1      # the ruins' guard, a find's neutrals: estimate >= 0.75
LAIR_MARGIN = 0.25      # a lair: >= 0.9


class DiplomatBot(Bot):
    name = "diplomat"
    trade_policy = "diplomat"  # core 0.8.0 (docs/TRADE.md 12.1, dah.bots.trade)
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
        # core 0.7.0: the Spire too (not in the capital) once the Monument stands, when its shares fit the income
        spire = [(SPIRE, False)] if score.has_work(view, MONUMENT) else []
        works, view = score.lay_works(view, [(MONUMENT, True), *spire], FOOD_TURNS)
        # after its building turns it saves for the Monument: the stock is kept, recruits eat what income leaves
        early = view.turn <= self.build_until and not score.wants_work(view)   # its objective: save at once
        saving = None if works or early else score.saving_rate(view, MONUMENT, FOOD_TURNS)
        if saving is not None:
            view = spy.food_view(view, view.resource(FOOD_RESOURCE))
        orders: list[Order] = list(works)
        alloy = view.resource(BUILD_RESOURCE)
        feedable = view.feedable(RECRUIT_UNIT, FOOD_TURNS)
        build = None
        if feedable is not None and feedable < alloy // UNIT_COST_ALLOY[RECRUIT_UNIT]:
            site = farm_site(view)  # food, not alloy, limits the army
            build = (site, "farm") if site is not None else None
        elif view.turn <= self.build_until:
            # stage 4: with science scoring it lays a temple once it has a mine (more research, more points)
            build = choose_build(view, total_upkeep(view), temple=view.scoring("science"))
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
        if saving is not None:
            count = min(count, (view.net_income or {}).get(FOOD_RESOURCE, 0) - saving - unique_upkeep)
        if site is not None and count > 0:
            orders.append(
                {"kind": "recruit", "province": site.id, "unit": view.qualify(RECRUIT_UNIT), "count": count}
            )

        betrayal = self._betrayal(view, estimate)
        moves = [betrayal] if betrayal is not None else []
        keep = merge_reserved(garrison(capital_of(view), self.garrison), reserved_by_orders(moves),
                              score.objective_garrison(view))
        if view.conquest:
            keep = merge_reserved(keep, cq.occupation_garrison(view))
        bonus = finds.merge_bonus(score.attack_bonus(view),
                                  finds.attack_bonus(view, self.finds, GUARD_MARGIN, LAIR_MARGIN))
        moves += plan_moves(view, estimate, ATTACK_WIN, self.rng, keep, lambda t: not view.is_partner(t.owner),
                            bonus, army_ok=finds.army_ok(self.finds, FIND_ARMY))
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
                                  min_alloy=0 if spy.first_shade(view, self.memory) else SHADE_MIN_ALLOY)
            if hire is not None:
                out.append(hire)
                alloy -= spy.shade_cost()
            else:
                alloy -= spy.shade_savings(view, self.memory, from_turn=SHADE_FROM_TURN, alloy=alloy)
            busy = spy.mission_targets(view)
            idle = spy.idle_shades(view)
            esp = view.espionage or {}
            slots = max(0, int(esp.get("active_limit", 0)) - len(spy.active_shades(view)))
            for mission, target, rounds in self._mission_plan(view, busy):
                if not idle or not slots:
                    break
                got = spy.spy_order(view, idle[0], mission, target, rounds, alloy, food)
                if got is None:
                    continue
                out.append(got[0])
                alloy -= got[1]
                food -= got[2]
                idle, slots = idle[1:], slots - 1
                busy.add(target)
        return out, view.free_food() - food, view.resource(BUILD_RESOURCE) - alloy

    def _mission_plan(self, view: StateView, busy: set[str]) -> list[tuple[str, str, int]]:
        """(mission, target, rounds) for the free Shades, best first: the partner's orders every PARTNER_EVERY turns
        (its secret objective first), then a rival: its secret, its orders while they still score, its ledger."""
        read = "read_orders" in spy.MISSIONS and view.whisper >= spy.MISSIONS["read_orders"][0]
        plans: list[tuple[str, str, int]] = []
        partner = spy.strongest_neighbour(view, view.is_partner)
        if partner is not None and partner not in busy:
            if spy.secret_wanted(view, self.memory, partner):
                plans.append((spy.SECRET, partner, SECRET_ROUNDS))
            elif read and view.turn - self.memory.read_turn.get(partner, -PARTNER_EVERY) >= PARTNER_EVERY:
                plans.append(("read_orders", partner, 1))
        rival = spy.strongest_neighbour(view, lambda s: not view.is_partner(s)) or spy.rival_target(view)
        if rival is not None and rival not in busy and rival != partner:
            if spy.secret_wanted(view, self.memory, rival):
                plans.append((spy.SECRET, rival, SECRET_ROUNDS))
            elif read and rival not in self.memory.read_turn:
                plans.append(("read_orders", rival, READ_ROUNDS))
            else:
                plans.append(("ledger", rival, LEDGER_ROUNDS))
        return plans

    def message(self, view: StateView) -> str | None:
        """Core 0.8.0: in negotiation_2 a pact break against us that this turn's Read Orders shows is named in
        public (the victim's warning; docs/SPY_V2.md 10)."""
        if view.phase != "negotiation_2" or view.espionage is None:
            return None
        seen = spy.breaches(view, self.memory)
        if not seen:
            return None
        parts = ", ".join(f"{seat} → {pid}" for seat, pid in seen[:3])
        return f"Вижу удар в нарушение пакта: {parts}. Укрепляюсь; все видят, кто нарушает слово."

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
        out += spy.fortify_threatened(view, self.memory, current + out, cp_left)
        return out

    def _betrayal(self, view: StateView, estimate: Estimator) -> Order | None:
        if not view.turn_limit or view.turn <= view.turn_limit - BETRAY_TURNS:
            return None
        attack = best_attack(view, {}, estimate, False, BETRAY_WIN, self.rng,
                             lambda t: view.is_partner(t.owner) and worth_betrayal(view, t))
        if attack is not None:
            attack["pact"] = "break"
        return attack


def worth_betrayal(view: StateView, target: Province) -> bool:
    """Core 0.6.0: in a scored game a broken pact costs bv.BETRAYAL_PENALTY points, so only a target worth more is
    attacked: a capital (1 + 3 points) or a province that advances the open objective."""
    penalty = bv.BETRAYAL_PENALTY if view.score_parts is not None else 0
    if penalty <= 0:
        return True
    value = 1 + (3 if target.kind == "capital" else 0)
    return value > penalty or any(score.advances(view, o, target) for o in view.open_objectives())


def neighbours(view: StateView) -> list[str]:
    """Living other seats that own a province next to one of ours."""
    seen: set[str] = set()
    for own in view.own_provinces():
        for p in view.neighbors(own):
            if p.owner is not None and not view.is_mine(p):
                seen.add(p.owner)
    living = set(view.players) if view.players else seen
    return sorted(seen & living)
