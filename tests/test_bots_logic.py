"""Bot decision logic on hand-made state payloads (no server)."""

from __future__ import annotations

from typing import Any

from dah.bots import BOT_TYPES, BuilderBot, PassiveBot, RushBot
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


def test_builder_builds_mine_and_fortifies_capital():
    orders = BuilderBot("A", 1).action_orders(parse_state(make_state(), ME), no_estimate)
    assert orders == [
        {"kind": "build", "province": "P02", "building": "mine"},
        {"kind": "fortify", "province": "P01"},
    ]


def test_builder_prefers_farm_when_food_is_short():
    orders = BuilderBot("A", 1).action_orders(parse_state(make_state(provisions=5), ME), no_estimate)
    assert orders[0] == {"kind": "build", "province": "P03", "building": "farm"}
    assert not any(o["kind"] == "recruit" for o in orders)


def test_builder_recruits_from_surplus_with_full_ids():
    orders = BuilderBot("A", 1).action_orders(parse_state(make_state(alloy=100, prefixed=True), ME), no_estimate)
    kinds = [o["kind"] for o in orders]
    assert kinds == ["build", "recruit", "fortify"]
    assert orders[0]["building"] == "core:mine"
    # 100 - 20 (mine) - 20 reserve = 60 -> 5 assault; food 30 - 3*3 = 21 -> 7 feedable
    assert orders[1] == {"kind": "recruit", "province": "P01", "unit": "core:assault", "count": 5}


def test_builder_never_moves_and_greets_only_on_first_turn():
    bot = BuilderBot("A", 1)
    first = parse_state(make_state(phase="negotiation_1"), ME)
    later = parse_state(make_state(phase="negotiation_1", turn=2), ME)
    assert isinstance(bot.message(first), str) and bot.message(first)
    assert bot.message(later) is None
    assert bot.p2_orders(first, [], 3, no_estimate) == []
    assert all(o["kind"] != "move" for o in bot.action_orders(first, no_estimate))


# --- rush ---


def test_rush_recruits_max_and_attacks_best_target():
    estimate = fixed_estimate({"forge": 0.7})
    orders = RushBot("A", 1).action_orders(parse_state(make_state(alloy=40), ME), estimate)
    assert orders == [
        {"kind": "recruit", "province": "P01", "unit": "assault", "count": 3},
        {"kind": "move", "from": "P01", "to": "P07", "units": {"assault": 3}},
    ]
    assert estimate.calls == [({"assault": 3}, {"assault": 1}, "forge", False, False)]


def test_rush_holds_when_estimate_below_threshold():
    orders = RushBot("A", 1).action_orders(parse_state(make_state(alloy=0), ME), fixed_estimate({"forge": 0.59}))
    assert orders == []


def test_rush_advances_interior_army_towards_nearest_foreign_province():
    state = make_state(alloy=0)
    provinces = state["state"]["provinces"]
    provinces[0].update(units={}, neighbors=["P02", "P03"])  # capital no longer borders P07
    provinces[2].update(units={"assault": 4}, neighbors=["P01"])  # army deep inside, P03 -> P01 -> P02 -> P07
    orders = RushBot("A", 1).action_orders(parse_state(state, ME), no_estimate)
    assert orders == [{"kind": "move", "from": "P03", "to": "P01", "units": {"assault": 4}}]


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
    state["state"]["provinces"][2].update(neighbors=["P01", "P09"], units={"assault": 2})
    state["state"]["provinces"].append(province("P09", "forge", "C", {"assault": 1}, ["P03"]))
    view = parse_state(state, ME)
    estimate = fixed_estimate({"forge": 0.9})

    def target(seat: str) -> str:
        return RushBot(seat, seat_seed("g1", seat)).action_orders(view, estimate)[0]["to"]

    assert target("A") == target("A")
    targets = {RushBot("A", seed).action_orders(view, estimate)[0]["to"] for seed in range(20)}
    assert targets == {"P07", "P09"}


def test_bot_registry_names():
    assert set(BOT_TYPES) == {"passive", "builder", "rush"}
