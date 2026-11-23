import pytest

from dah.engine import (SeatOrders, amend_order, build_ruleset, cancel_order, check_order, cp_left, new_game_state,
                        orders_hash, reserved_resources, resolve, submit_orders)
from dah.engine.model import P2

ACT = "actions"


@pytest.fixture(scope="module")
def rs():
    return build_ruleset(None, "slice2")


@pytest.fixture
def st(rs):
    state = new_game_state(rs, [{"seat": "A", "nick": "Аня"}, {"seat": "B", "nick": "Боб"}], 1)
    # deterministic layout regardless of the start shuffle: A holds P01-P04, B holds P05-P08
    for pid, prov in state.provinces.items():
        prov.owner = "A" if pid in ("P01", "P02", "P03", "P04") else "B" if pid in ("P05", "P06", "P07", "P08") else None
        prov.units, prov.buildings = {}, []
    state.provinces["P01"].units = {"core:assault": 3}
    state.provinces["P01"].buildings = ["core:barracks"]
    state.provinces["P05"].units = {"core:assault": 3}
    state.provinces["P05"].buildings = ["core:barracks"]
    return state


def submit(st, rs, items, phase=ACT, book=None, seat="A", mode="append"):
    return submit_orders(st, rs, seat, book or SeatOrders(), items, phase, mode)


def reason(st, rs, item, phase=ACT, book=None):
    return check_order(st, rs, "A", book or SeatOrders(), item, phase).reason


def mv(src, dst, n=1, unit="assault"):
    return {"kind": "move", "from": src, "to": dst, "units": {unit: n}}


def test_valid_orders_accepted(st, rs):
    res = submit(st, rs, [mv("P01", "P02", 2), {"kind": "recruit", "province": "P01", "unit": "assault", "count": 1},
                          {"kind": "build", "province": "P02", "building": "mine"},
                          {"kind": "fortify", "province": "P03"}])
    assert [c.ok for c in res.results] == [True] * 4
    assert all(c.cp_cost == 0 for c in res.results)
    assert res.book.orders[0].units == {"core:assault": 2}          # short ids normalized
    assert reserved_resources(rs, "core:covenant", res.book) == {"core:alloy": 22}   # assault 12 + mine 10 (core 0.2)


def test_reasons(st, rs):
    assert reason(st, rs, mv("P01", "P05")) == "NOT_ADJACENT"
    assert reason(st, rs, mv("P05", "P06")) == "NOT_OWNER"
    assert reason(st, rs, mv("P01", "P02", 4)) == "NOT_ENOUGH_UNITS"
    assert reason(st, rs, mv("P01", "P02", 1, "armor")) == "NOT_ENOUGH_UNITS"
    assert reason(st, rs, {"kind": "recruit", "province": "P01", "unit": "armor", "count": 2}) == "NOT_ENOUGH_RESOURCES"
    assert reason(st, rs, {"kind": "recruit", "province": "P02", "unit": "assault", "count": 1}) == "NO_RECRUIT_BUILDING"
    assert reason(st, rs, {"kind": "build", "province": "P01", "building": "barracks"}) == "ALREADY_BUILT"
    assert reason(st, rs, {"kind": "build", "province": "P05", "building": "mine"}) == "NOT_OWNER"
    assert reason(st, rs, mv("P01", "P99")) == "UNKNOWN_ID"
    assert reason(st, rs, mv("P01", "P02", 1, "dragon")) == "UNKNOWN_ID"
    assert reason(st, rs, {"kind": "build", "province": "P02", "building": "castle"}) == "UNKNOWN_ID"
    assert reason(st, rs, {"kind": "teleport"}) == "INVALID"
    assert reason(st, rs, {"kind": "recruit", "province": "P01", "unit": "assault", "count": 0}) == "INVALID"
    assert reason(st, rs, mv("P01", "P02"), phase="resolution") == "PHASE_LOCKED"


def test_reservations_between_orders(st, rs):
    st.players["A"].resources["core:alloy"] = 40   # 3 assault (36) fit, then nothing else does
    res = submit(st, rs, [mv("P01", "P02", 2), mv("P01", "P03", 2),
                          {"kind": "recruit", "province": "P01", "unit": "assault", "count": 3},
                          {"kind": "build", "province": "P02", "building": "mine"},
                          {"kind": "build", "province": "P03", "building": "farm"},
                          {"kind": "build", "province": "P04", "building": "mine"}])
    assert [c.reason for c in res.results] == [None, "NOT_ENOUGH_UNITS", None, "NOT_ENOUGH_RESOURCES",
                                               "NOT_ENOUGH_RESOURCES", "NOT_ENOUGH_RESOURCES"]
    res = submit(st, rs, [{"kind": "build", "province": "P02", "building": "mine"},
                          {"kind": "build", "province": "P02", "building": "mine"}])
    assert res.results[1].reason == "ALREADY_BUILT"


def test_replace_all_and_hash(st, rs):
    first = submit(st, rs, [mv("P01", "P02", 3)])
    again = submit(st, rs, [mv("P01", "P03", 3)], book=first.book, mode="replace_all")
    assert [o.dst for o in again.book.orders] == ["P03"]
    assert orders_hash(first.book) != orders_hash(again.book)
    assert orders_hash(first.book) == orders_hash(SeatOrders(list(first.book.orders)))
    assert submit(st, rs, [mv("P01", "P02")], book=first.book).results[0].reason == "NOT_ENOUGH_UNITS"


def test_free_edits_in_actions(st, rs):
    book = submit(st, rs, [mv("P01", "P02", 2)]).book
    oid = book.orders[0].id
    amended = amend_order(st, rs, "A", book, oid, {"units": {"assault": 3}, "to": "P03"}, ACT)
    assert amended.ok and amended.cp_cost == 0 and amended.order.phase_added == ACT
    cancelled = cancel_order(st, rs, "A", amended.book, oid, ACT)
    assert cancelled.ok and cancelled.book.orders == [] and cancelled.book.cp_spent == 0
    assert cancel_order(st, rs, "A", book, "nope", ACT).reason == "UNKNOWN_ID"


def test_p2_locks_recruit_and_build(st, rs):
    assert reason(st, rs, {"kind": "recruit", "province": "P01", "unit": "assault", "count": 1}, P2) == "PHASE_LOCKED"
    assert reason(st, rs, {"kind": "build", "province": "P02", "building": "mine"}, P2) == "PHASE_LOCKED"
    assert submit(st, rs, [mv("P01", "P02")], P2, mode="replace_all").results[0].reason == "PHASE_LOCKED"
    book = submit(st, rs, [{"kind": "recruit", "province": "P01", "unit": "assault", "count": 1}]).book
    assert amend_order(st, rs, "A", book, book.orders[0].id, {"count": 2}, P2).reason == "PHASE_LOCKED"


def test_p2_prices_and_hasty(st, rs):
    assert cp_left(st, "A", SeatOrders()) == 3
    res = submit(st, rs, [mv("P01", "P02", 1), {"kind": "fortify", "province": "P03"}], P2)
    move, fort = res.results
    assert move.ok and move.cp_cost == 2 and move.order.phase_added == P2 and move.order.hasty
    assert fort.ok and fort.cp_cost == 1 and not fort.order.hasty
    assert cp_left(st, "A", res.book) == 0
    assert submit(st, rs, [mv("P01", "P02", 1)], P2, book=res.book).results[0].reason == "NOT_ENOUGH_CP"
    # CP do not accumulate: next turn starts with the base value again
    new, _ = resolve(st, {"A": res.book}, rs, 1)
    assert new.players["A"].command_points == 3


def test_p2_amend_prices(st, rs):
    book = submit(st, rs, [mv("P01", "P02", 2)]).book
    oid = book.orders[0].id
    reduced = amend_order(st, rs, "A", book, oid, {"units": {"assault": 1}}, P2)
    assert reduced.ok and reduced.cp_cost == 1 and reduced.order.phase_added == ACT
    retarget = amend_order(st, rs, "A", book, oid, {"to": "P03"}, P2)
    assert retarget.ok and retarget.cp_cost == 2 and retarget.order.hasty
    reinforce = amend_order(st, rs, "A", book, oid, {"units": {"assault": 3}}, P2)
    assert reinforce.ok and reinforce.cp_cost == 2 and reinforce.order.phase_added == P2
    assert amend_order(st, rs, "A", book, oid, {"units": {"assault": 4}}, P2).reason == "NOT_ENOUGH_UNITS"
    assert amend_order(st, rs, "A", book, oid, {"units": {"assault": 2}}, P2).reason == "INVALID"
    assert amend_order(st, rs, "A", book, oid, {"count": 1}, P2).reason == "INVALID"
    spent = SeatOrders(book.orders, cp_spent=2)
    assert amend_order(st, rs, "A", spent, oid, {"to": "P03"}, P2).reason == "NOT_ENOUGH_CP"
    assert book.orders[0].dst == "P02"          # inputs are never mutated


def test_p2_cancel_refund(st, rs):
    book = submit(st, rs, [{"kind": "recruit", "province": "P01", "unit": "assault", "count": 1},
                           {"kind": "build", "province": "P02", "building": "mine"}]).book
    res = cancel_order(st, rs, "A", book, book.orders[0].id, P2)
    assert res.ok and res.cp_cost == 1
    assert res.book.forfeit == {"core:alloy": 3}               # 12 - floor(12 * 0.75)
    assert reserved_resources(rs, "core:covenant", res.book) == {"core:alloy": 13}   # mine 10 + forfeit 3
    new, _ = resolve(st, {"A": res.book}, rs, 1)
    # 30 - 10 (mine) - 3 (forfeit) + income 3 (capital) + 4 (forge) + 1 (waste)
    assert new.players["A"].resources["core:alloy"] == 25
    assert "core:mine" in new.provinces["P02"].buildings
    st.players["A"].resources["core:alloy"] = 40   # room for 3 assault (core 0.2 starts with 30)
    reduce = amend_order(st, rs, "A", submit(st, rs, [{"kind": "recruit", "province": "P01", "unit": "assault",
                                                       "count": 3}]).book, "tmp1", {"count": 1}, P2)
    assert reduce.ok and reduce.cp_cost == 1 and reduce.book.forfeit == {"core:alloy": 6}   # 24 - 18


def test_dead_player_locked(st, rs):
    st.players["A"].alive = False
    assert reason(st, rs, mv("P01", "P02")) == "PHASE_LOCKED"
