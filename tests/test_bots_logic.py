"""Bot decision logic on hand-made state payloads (no server)."""

from __future__ import annotations

from typing import Any

from dah.bots import BOT_TYPES, BuilderBot, DiplomatBot, PassiveBot, RushBot
from dah.bots.base import seat_seed
from dah.bots.view import parse_state

ME = {"A", "Нико"}


def province(pid: str, ptype: str, owner: str | None, units: Any, neighbors: list[str], **extra: Any) -> dict:
    return {"id": pid, "name": pid, "type": ptype, "owner": owner, "units": units, "neighbors": neighbors, **extra}


def make_state(alloy: int = 40, provisions: int = 30, prefixed: bool = False, **overrides: Any) -> dict:
    p = "core:" if prefixed else ""
    provinces = [
        province("P01", f"{p}capital", "A", {f"{p}assault": 3}, ["P02", "P03", "P07"], buildings=[f"{p}barracks"]),
        province("P02", f"{p}forge", "A", {}, ["P01", "P07"], buildings=[]),
        province("P03", f"{p}agri", "A", {}, ["P01"], buildings=[]),
        province("P07", f"{p}forge", "B", {f"{p}assault": 1}, ["P01", "P02", "P08"], buildings=[]),
        province("P08", f"{p}waste", None, "?", ["P07"], buildings=[]),
    ]
    state = {
        "turn": 1,
        "phase": "actions",
        "you": {"seat": "A", "resources": {f"{p}alloy": alloy, f"{p}provisions": provisions}, "command_points": 3},
        "provinces": provinces,
    }
    state.update(overrides)
    return {"ok": True, "state": state}


def fixed_estimate(table: dict[str, float]):
    calls: list[tuple] = []

    def estimate(attacker, defender, ptype, fortified, hasty):
        calls.append((dict(attacker), dict(defender), ptype, fortified, hasty))
        return table.get(ptype, 0.0)

    estimate.calls = calls
    return estimate


PLAYERS = [{"seat": "A", "alive": True}, {"seat": "B", "alive": True}]


class FakeDiplomacy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def respond_pact(self, pact_id, decision):
        self.calls.append(("respond", pact_id, decision))

    def propose_pact(self, to, turns):
        self.calls.append(("propose", to, turns))

    def send_gift(self, to, resources):
        self.calls.append(("gift", to, resources))

    def leave_pact(self, pact_id):
        self.calls.append(("leave", pact_id))


def no_estimate(*_args):
    raise AssertionError("estimate must not be called")


# --- adapter ---


def test_parse_state_reads_core_fields():
    view = parse_state(make_state(), ME)
    assert view.turn == 1 and view.command_points == 3
    assert view.resources == {"alloy": 40, "provisions": 30}
    assert [p.id for p in view.own_provinces()] == ["P01", "P02", "P03"]
    assert view.provinces["P01"].units == {"assault": 3}
    assert view.provinces["P08"].units is None  # hidden
    assert [p.id for p in view.neighbors(view.provinces["P01"])] == ["P02", "P03", "P07"]
    assert view.qualify("farm") == "farm"


def test_parse_state_full_ids_dict_provinces_and_edges():
    payload = {
        "turn": 2,
        "phase": "negotiation_2",
        "resources": {"core:alloy": {"amount": 5}},
        "provinces": {
            "P01": {"type": "core:capital", "owner": {"seat": "A"}, "units": {"A": {"core:armor": 2}}},
            "P02": {"type": "core:forge", "owner": "B"},
        },
        "edges": [["P01", "P02"]],
    }
    view = parse_state(payload, ME)
    assert view.id_prefix == "core:" and view.qualify("assault") == "core:assault"
    assert view.resources == {"alloy": 5}
    assert view.provinces["P01"].units == {"armor": 2}
    assert view.provinces["P01"].kind == "capital"
    assert view.provinces["P02"].neighbors == ("P01",)
    assert view.provinces["P02"].units is None


# --- passive ---


def test_passive_does_nothing():
    bot = PassiveBot("A", 1)
    view = parse_state(make_state(), ME)
    assert bot.message(view) is None
    assert bot.action_orders(view, no_estimate) == []
    assert bot.p2_orders(view, [], 3, no_estimate) == []


# --- builder ---
# Builder, rush and diplomat share the movement code (dah.bots.tactics): each army goes at its best target, interior
# armies march, armies that stay fortify. A bot's capital garrison is picked by its seed; the tests set it.


def builder(garrison: int = 3, seed: int = 1) -> BuilderBot:
    bot = BuilderBot("A", seed)
    bot.garrison = garrison
    return bot


def test_builder_builds_mine_recruits_and_fortifies_capital():
    orders = builder().action_orders(parse_state(make_state(), ME), no_estimate)  # the whole army is the garrison
    # 40 - 10 (mine) = 30 -> 2 assault (12 each); food 30 - 3*3 = 21 -> 7 feedable
    assert orders == [
        {"kind": "build", "province": "P02", "building": "mine"},
        {"kind": "recruit", "province": "P01", "unit": "assault", "count": 2},
        {"kind": "fortify", "province": "P01"},
    ]


def test_builder_prefers_farm_when_food_is_short():
    orders = builder().action_orders(parse_state(make_state(provisions=5), ME), no_estimate)
    assert orders[0] == {"kind": "build", "province": "P03", "building": "farm"}
    assert not any(o["kind"] == "recruit" for o in orders)


def test_builder_farm_by_net_food_income():
    fed = make_state()
    fed["state"]["you"]["net_income"] = {"alloy": 8, "provisions": 4}
    assert builder().action_orders(parse_state(fed, ME), no_estimate)[0]["building"] == "mine"
    hungry = make_state(provisions=12)
    hungry["state"]["you"]["net_income"] = {"alloy": 8, "provisions": 1}
    orders = builder().action_orders(parse_state(hungry, ME), no_estimate)
    assert orders[0] == {"kind": "build", "province": "P03", "building": "farm"}
    # food 12 + 3 turns x 1 = 15 feeds 5 more for 3 turns; alloy (40 - 8) // 12 = 2
    assert orders[1]["count"] == 2


def test_builder_recruits_from_surplus_with_full_ids():
    orders = builder().action_orders(parse_state(make_state(alloy=100, prefixed=True), ME), no_estimate)
    kinds = [o["kind"] for o in orders]
    assert kinds == ["build", "recruit", "fortify"]
    assert orders[0]["building"] == "core:mine"
    # 100 - 10 (mine) = 90 -> 7 assault; food 30 - 3*3 = 21 -> 7 feedable
    assert orders[1] == {"kind": "recruit", "province": "P01", "unit": "core:assault", "count": 7}


def test_builder_attacks_only_sure_fights_and_keeps_its_garrison():
    view = parse_state(make_state(alloy=0), ME)
    unsure = fixed_estimate({"forge": 0.7})
    assert builder(garrison=1).action_orders(view, unsure) == [{"kind": "fortify", "province": "P01"}]
    assert unsure.calls == [({"assault": 2}, {"assault": 1}, "forge", False, False)]   # 3 - garrison 1
    sure = builder(garrison=1).action_orders(view, fixed_estimate({"forge": 0.8}))
    assert sure == [{"kind": "move", "from": "P01", "to": "P07", "units": {"assault": 2}}]   # capital left: no fortify
    late = parse_state(make_state(alloy=0, turn=9, turn_limit=10), ME)   # stopped building: 0.6 is enough
    assert builder(garrison=1).action_orders(late, fixed_estimate({"forge": 0.65}))[0]["kind"] == "move"


def test_builder_greets_only_on_first_turn():
    bot = BuilderBot("A", 1)
    first = parse_state(make_state(phase="negotiation_1"), ME)
    later = parse_state(make_state(phase="negotiation_1", turn=2), ME)
    assert isinstance(bot.message(first), str) and bot.message(first)
    assert bot.message(later) is None
    assert bot.p2_orders(first, [], 3, no_estimate) == []


def test_builder_accepts_pacts_and_spares_partners():
    state = make_state(alloy=0, players=PLAYERS, pact_proposals=[{"id": "pact-A-B-1", "from": "B", "to": "A"}])
    api = FakeDiplomacy()
    builder().diplomacy(parse_state(state, ME), api)
    assert api.calls == [("respond", "pact-A-B-1", "accept")]
    allied = parse_state(make_state(alloy=0, players=PLAYERS, pacts=[{"seats": ["A", "B"], "id": "pact-A-B-1"}]), ME)
    assert builder(garrison=0).action_orders(allied, no_estimate) == [{"kind": "fortify", "province": "P01"}]


# --- rush ---


def rush(garrison: int = 0, seed: int = 1) -> RushBot:
    bot = RushBot("A", seed)
    bot.garrison = garrison
    return bot


def test_rush_recruits_max_and_attacks_best_target():
    estimate = fixed_estimate({"forge": 0.7})
    orders = rush().action_orders(parse_state(make_state(alloy=40), ME), estimate)
    assert orders == [
        {"kind": "recruit", "province": "P01", "unit": "assault", "count": 3},
        {"kind": "move", "from": "P01", "to": "P07", "units": {"assault": 3}},
    ]
    assert estimate.calls == [({"assault": 3}, {"assault": 1}, "forge", False, False)]


def test_rush_keeps_its_garrison():
    estimate = fixed_estimate({"forge": 0.7})
    orders = rush(garrison=1).action_orders(parse_state(make_state(alloy=0), ME), estimate)
    assert orders == [{"kind": "move", "from": "P01", "to": "P07", "units": {"assault": 2}}]


def test_rush_builds_a_farm_when_food_limits_the_army():
    state = make_state(alloy=40, provisions=2)
    state["state"]["you"]["net_income"] = {"alloy": 8, "provisions": 0}
    orders = rush().action_orders(parse_state(state, ME), fixed_estimate({}))
    # food 2 + 2 turns x 0 feeds 1 more; the farm (8) comes first
    assert orders[:2] == [{"kind": "build", "province": "P03", "building": "farm"},
                          {"kind": "recruit", "province": "P01", "unit": "assault", "count": 1}]


def test_rush_holds_when_estimate_below_threshold():
    orders = rush().action_orders(parse_state(make_state(alloy=0), ME), fixed_estimate({"forge": 0.59}))
    assert orders == [{"kind": "fortify", "province": "P01"}]


def test_rush_advances_interior_army_towards_nearest_foreign_province():
    state = make_state(alloy=0)
    provinces = state["state"]["provinces"]
    provinces[0].update(units={}, neighbors=["P02", "P03"])  # capital no longer borders P07
    provinces[2].update(units={"assault": 4}, neighbors=["P01"])  # army deep inside, P03 -> P01 -> P02 -> P07
    orders = rush().action_orders(parse_state(state, ME), no_estimate)
    assert orders == [{"kind": "move", "from": "P03", "to": "P01", "units": {"assault": 4}},
                      {"kind": "fortify", "province": "P01"}]


def test_rush_p2_adds_one_hasty_attack_with_free_units():
    estimate = fixed_estimate({"forge": 0.8})
    view = parse_state(make_state(phase="negotiation_2"), ME)
    orders = RushBot("A", 1).p2_orders(view, [{"kind": "recruit", "province": "P01"}], 2, estimate)
    assert orders == [{"kind": "move", "from": "P01", "to": "P07", "units": {"assault": 3}}]
    assert estimate.calls[0][4] is True


def test_rush_p2_respects_cp_and_existing_attack():
    view = parse_state(make_state(phase="negotiation_2"), ME)
    bot = RushBot("A", 1)
    assert bot.p2_orders(view, [], 1, no_estimate) == []
    existing = [{"kind": "move", "from": "P01", "to": "P07", "units": {"assault": 3}}]
    assert bot.p2_orders(view, existing, 3, no_estimate) == []


def test_rush_p2_skips_reserved_units():
    view = parse_state(make_state(phase="negotiation_2"), ME)
    reserved = RushBot("A", 1)._best_attack(view, {"P01": {"assault": 3}}, fixed_estimate({"forge": 1.0}), True)
    assert reserved is None


def test_rush_tie_break_is_deterministic_per_seat_seed():
    state = make_state(alloy=0)
    state["state"]["provinces"][0]["neighbors"].append("P09")
    state["state"]["provinces"].append(province("P09", "forge", "C", {"assault": 1}, ["P01"]))
    view = parse_state(state, ME)
    estimate = fixed_estimate({"forge": 0.9})

    def target(seat: str) -> str:
        return RushBot(seat, seat_seed("g1", seat)).action_orders(view, estimate)[0]["to"]

    assert target("A") == target("A")
    targets = {RushBot("A", seed).action_orders(view, estimate)[0]["to"] for seed in range(20)}
    assert targets == {"P07", "P09"}


# --- diplomat ---


def diplomat(garrison: int = 0, seed: int = 1) -> DiplomatBot:
    bot = DiplomatBot("A", seed)
    bot.garrison = garrison
    return bot


def test_diplomat_proposes_to_neighbours_and_accepts_offers():
    state = make_state(players=PLAYERS + [{"seat": "C", "alive": True}],
                       pact_proposals=[{"id": "pact-A-C-1", "from": "C", "to": "A", "turns": None}])
    api = FakeDiplomacy()
    bot = diplomat()
    bot.diplomacy(parse_state(state, ME), api)
    # C offered: accept; B borders us (P07): propose an indefinite pact; C (no neighbour) gets no proposal
    assert ("respond", "pact-A-C-1", "accept") in api.calls and ("propose", "B", None) in api.calls
    assert not any(c[0] == "propose" and c[1] == "C" for c in api.calls)
    for _ in range(3):  # gives up on a seat after two unanswered proposals
        bot.diplomacy(parse_state(make_state(players=PLAYERS), ME), api)
    assert sum(1 for c in api.calls if c[:2] == ("propose", "B")) == 2


def test_diplomat_spares_partners_until_a_decisive_late_betrayal():
    pacts = [{"seats": ["A", "B"], "id": "pact-A-B-1"}]
    mid = parse_state(make_state(alloy=0, turn=5, turn_limit=10, players=PLAYERS, pacts=pacts), ME)
    assert diplomat().action_orders(mid, fixed_estimate({"forge": 1.0})) == [{"kind": "fortify", "province": "P01"}]
    late = parse_state(make_state(alloy=0, turn=10, turn_limit=10, players=PLAYERS, pacts=pacts), ME)
    assert diplomat().action_orders(late, fixed_estimate({"forge": 0.8})) == [{"kind": "fortify", "province": "P01"}]
    orders = diplomat().action_orders(late, fixed_estimate({"forge": 0.9}))
    assert orders == [{"kind": "move", "from": "P01", "to": "P07", "units": {"assault": 3}, "pact": "break"}]


def test_bot_numbers_match_the_core_pack():
    """The bots plan with the core pack's costs (dah.bots.view); a balance change must update both."""
    from dah.bots import view as bv
    from dah.engine import build_ruleset

    rs = build_ruleset(None, "slice3")
    assert bv.UNIT_COST_ALLOY == {u.split(":")[1]: rs.unit_cost(u)["core:alloy"] for u in rs.units}
    assert bv.UNIT_UPKEEP_FOOD == {u.split(":")[1]: rs.unit_upkeep(u)["core:provisions"] for u in rs.units}
    assert bv.BUILDING_COST_ALLOY == {b.split(":")[1]: rs.building_cost(b)["core:alloy"] for b in rs.buildings}


def test_bot_registry_names():
    assert set(BOT_TYPES) == {"passive", "builder", "rush", "diplomat"}
