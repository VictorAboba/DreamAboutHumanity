import pytest

from dah.engine import (GameState, SeatOrders, TurnReport, build_ruleset, issue_order_id, new_game_state, resolve,
                        scores, state_hash, submit_orders)
from dah.engine.model import P2

from dah.engine.content import LEGACY_PACKS_DIR

AS, AR, AT = "core:assault", "core:armor", "core:artillery"
CORE040 = LEGACY_PACKS_DIR / "core-0.4.0"
ALLOY, FOOD, CREED = "core:alloy", "core:provisions", "core:creed"


# The slice rules: core 0.4.0 turns espionage and conquest on in slice2/slice3; these tests are about the slice.
OFF = {"espionage": False, "conquest": False, "discoveries": False}     # the plain slice flow
LAND = {"territory": 1}      # core 0.5.0 scores every source; these tests keep the slice score


@pytest.fixture(scope="module")
def rs2():
    return build_ruleset(None, "slice2", OFF, scoring=LAND)


@pytest.fixture(scope="module")
def rs3():
    return build_ruleset(None, "slice3", OFF, scoring=LAND)


def layout(rs, nseats, owners):
    """State with explicit owners {pid: seat}; every province starts empty and without buildings."""
    seats = [{"seat": s, "nick": f"Ник{s}"} for s in "ABC"[:nseats]]
    st = new_game_state(rs, seats, 1)
    for pid, prov in st.provinces.items():
        prov.owner, prov.units, prov.buildings = owners.get(pid), {}, []
    return st


@pytest.fixture
def duel(rs2):
    st = layout(rs2, 2, {**dict.fromkeys(["P01", "P02", "P03", "P04"], "A"),
                         **dict.fromkeys(["P05", "P06", "P07", "P08"], "B")})
    st.provinces["P01"].buildings = ["core:barracks"]
    st.provinces["P05"].buildings = ["core:barracks"]
    return st


def orders(st, rs, seat, items, phase="actions", book=None):
    res = submit_orders(st, rs, seat, book or SeatOrders(), items, phase, id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def mv(src, dst, units):
    return {"kind": "move", "from": src, "to": dst, "units": units}


def battles(report, stage=None):
    return [e for e in report.events if e["kind"] == "battle" and (stage is None or e["stage"] == stage)]


def test_new_game_state(rs3):
    st = new_game_state(rs3, [{"seat": "A", "nick": "Нико"}, {"seat": "B", "nick": "Соло"}], 7)
    assert st.turn == 1 and st.phase == "negotiation_1"
    for seat in "AB":
        owned = st.owned(seat)
        assert len(owned) == 4
        capital = next(p for p in owned if p.id in rs3.map.capitals)
        assert capital.units == {AS: 4} and capital.buildings == ["core:barracks"]   # core 0.2: 4 assault
        assert st.players[seat].resources == {ALLOY: 30, FOOD: 15, CREED: 0}
        assert st.players[seat].command_points == 3
    # the free start is neutral and empty
    neutral = [p for p in st.provinces.values() if p.owner is None]
    assert len(neutral) == 7 and all(not p.units and not p.buildings for p in neutral)
    assert GameState.from_dict(st.to_dict()) == st
    assert state_hash(new_game_state(rs3, [{"seat": "A", "nick": "Нико"}, {"seat": "B", "nick": "Соло"}], 7)) \
        == state_hash(st)


def test_capture_empty_neutral_and_economy(duel, rs2):
    duel.provinces["P04"].units = {AS: 2}
    new, rep = resolve(duel, {"A": orders(duel, rs2, "A", [mv("P04", "P09", {"assault": 2})])}, rs2, 1)
    assert new.provinces["P09"].owner == "A" and new.provinces["P09"].units == {AS: 2}
    assert rep.public["ownership_changes"] == [{"province": "P09", "from": None, "to": "A"}]
    # core 0.2: start 30/15; income capital 3/2, forge 4, agri 4, waste 1/1, captured forge 4; upkeep 2 provisions
    assert new.players["A"].resources == {ALLOY: 30 + 12, FOOD: 15 + 7 - 2, CREED: 3}   # core 0.3: capital creed +3
    assert new.turn == 2 and new.phase == "negotiation_1"
    assert "scores" not in rep.public    # the turn score is private (SCORING.md 5)
    assert rep.scores == scores(new, rs2) == {"A": 8, "B": 7}


def test_recruit_and_builds(duel, rs2):
    duel.players["A"].resources[ALLOY] = 100
    book = orders(duel, rs2, "A", [{"kind": "recruit", "province": "P01", "unit": "assault", "count": 1},
                                   {"kind": "build", "province": "P02", "building": "mine"},
                                   {"kind": "build", "province": "P03", "building": "barracks"}])
    t2, _ = resolve(duel, {"A": book}, rs2, 1)
    assert t2.provinces["P01"].units == {AS: 1}
    assert t2.provinces["P02"].buildings == ["core:mine"]
    assert t2.provinces["P03"].buildings == [] and t2.pending_builds[0].turns_left == 1
    t3, rep = resolve(t2, {}, rs2, 1)
    assert t3.provinces["P03"].buildings == ["core:barracks"] and t3.pending_builds == []
    assert any(e["kind"] == "build_done" for e in rep.events)
    # the mine produces from the turn after completion
    assert t2.players["A"].resources[ALLOY] == 100 - 12 - 10 - 30 + 8      # assault, mine, barracks; income 8
    assert t3.players["A"].resources[ALLOY] == t2.players["A"].resources[ALLOY] + 8 + 5   # + the mine's 5


def test_resolve_is_pure_and_deterministic(duel, rs2):
    duel.provinces["P04"].units = {AS: 3}
    duel.provinces["P08"].units = {AS: 3}
    books = {"A": orders(duel, rs2, "A", [mv("P04", "P09", {"assault": 3})]),
             "B": orders(duel, rs2, "B", [mv("P08", "P09", {"assault": 3})])}
    before = state_hash(duel)
    a, ra = resolve(duel, books, rs2, 42)
    b, rb = resolve(duel, books, rs2, 42)
    assert state_hash(duel) == before
    assert state_hash(a) == state_hash(b) and ra.to_dict() == rb.to_dict()
    assert TurnReport.from_dict(ra.to_dict()) == ra
    hashes = {state_hash(resolve(duel, books, rs2, seed)[0]) for seed in range(12)}
    assert len(hashes) > 1


def test_attacker_repelled_retreats(duel, rs2):
    duel.provinces["P02"].units = {AS: 3}
    duel.provinces["P07"].units = {AS: 5}
    duel.provinces["P07"].owner = "B"
    new, rep = resolve(duel, {"A": orders(duel, rs2, "A", [mv("P02", "P07", {"assault": 3})])}, rs2, 3)
    (b,) = battles(rep, "defense")
    assert not b["sides"][0]["won"]
    assert new.provinces["P07"].owner == "B"
    survivors = 3 - sum(b["sides"][0]["losses"].values())
    assert new.provinces["P02"].units.get(AS, 0) == survivors
    assert survivors == 0 or any(e["kind"] == "retreat" and e["province"] == "P02" for e in rep.events)


def test_defender_beaten_survivors_retreat(duel, rs2):
    duel.provinces["P02"].units = {AR: 6}
    duel.provinces["P07"].units = {AT: 4}
    duel.provinces["P07"].owner = "B"
    duel.provinces["P05"].units = {AS: 1}
    new, rep = resolve(duel, {"A": orders(duel, rs2, "A", [mv("P02", "P07", {"armor": 6})])}, rs2, 3)
    (b,) = battles(rep, "defense")
    assert b["sides"][0]["won"]
    assert new.provinces["P07"].owner == "A" and set(new.provinces["P07"].units) == {AR}
    left = 4 - b["sides"][1]["losses"].get(AT, 0)
    assert new.provinces["P05"].units.get(AT, 0) == left      # retreated to the adjacent own province


def test_fortify(duel, rs2):
    duel.provinces["P02"].units = {AS: 4}
    duel.provinces["P07"].owner = "B"
    duel.provinces["P07"].units = {AS: 4}
    attack = orders(duel, rs2, "A", [mv("P02", "P07", {"assault": 4})])
    plain = battles(resolve(duel, {"A": attack}, rs2, 5)[1])[0]
    fort = resolve(duel, {"A": attack, "B": orders(duel, rs2, "B", [{"kind": "fortify", "province": "P07"}])}, rs2, 5)
    fb = battles(fort[1])[0]
    assert fb["fortified"] and not plain["fortified"]
    bonus = 1 + rs2.stat("combat.fortify_bonus")   # 1.15 in the core scenarios
    assert fb["sides"][1]["strength"] == pytest.approx(plain["sides"][1]["strength"] * bonus, rel=1e-3)
    assert fort[0].provinces["P07"].fortified
    # core 0.5.0 (combat.fortify_persists): a part of the stack may leave; emptying the province breaks it
    duel.provinces["P07"].units = {AS: 5}
    part = orders(duel, rs2, "B", [{"kind": "fortify", "province": "P07"}, mv("P07", "P05", {"assault": 1})])
    new, rep = resolve(duel, {"A": attack, "B": part}, rs2, 5)
    assert not any(e["kind"] == "fortify_broken" for e in rep.events) and battles(rep)[0]["fortified"]
    duel.provinces["P02"].units = {}
    empty = orders(duel, rs2, "B", [{"kind": "fortify", "province": "P07"}, mv("P07", "P05", {"assault": 5})])
    new, rep = resolve(duel, {"B": empty}, rs2, 5)
    assert any(e["kind"] == "fortify_broken" for e in rep.events) and not new.provinces["P07"].fortified


def test_fortify_one_turn_in_core_040(duel):
    """The frozen core 0.4.0 (and older): any unit leaving breaks the fortify; it lasts one turn."""
    old = build_ruleset([str(CORE040)], "slice2", OFF)
    assert old.stat("combat.fortify_persists") == 0
    duel.provinces["P02"].units = {AS: 4}
    duel.provinces["P07"].owner = "B"
    duel.provinces["P07"].units = {AS: 5}
    attack = orders(duel, old, "A", [mv("P02", "P07", {"assault": 4})])
    broken = orders(duel, old, "B", [{"kind": "fortify", "province": "P07"}, mv("P07", "P05", {"assault": 1})])
    new, rep = resolve(duel, {"A": attack, "B": broken}, old, 5)
    assert any(e["kind"] == "fortify_broken" for e in rep.events)
    assert not battles(rep)[0]["fortified"]
    duel.provinces["P02"].units = {}
    held = resolve(duel, {"B": orders(duel, old, "B", [{"kind": "fortify", "province": "P07"}])}, old, 5)[0]
    assert held.provinces["P07"].fortified
    assert not resolve(held, {}, old, 5)[0].provinces["P07"].fortified      # no order: gone next turn


def test_fortification_persists_while_the_army_stays(duel, rs2):
    """combat.fortify_persists (core 0.5.0): one fortify order, then it lasts while units stay: through a partial
    move out and a recruit; lost when the province is emptied by its own moves or captured; the units in it are not
    idle."""
    assert rs2.stat("combat.fortify_persists") == 1
    duel.provinces["P07"].owner = "B"
    duel.provinces["P07"].units = {AS: 5}
    duel.provinces["P07"].buildings = ["core:barracks"]
    st, rep = resolve(duel, {"B": orders(duel, rs2, "B", [{"kind": "fortify", "province": "P07"}])}, rs2, 5)
    assert st.provinces["P07"].fortified and rep.idle["B"]["units"] == 0
    st, rep = resolve(st, {}, rs2, 5)                                    # no order: still fortified
    assert st.provinces["P07"].fortified and rep.idle["B"]["units"] == 0
    book = orders(st, rs2, "B", [mv("P07", "P05", {"assault": 2}),
                                 {"kind": "recruit", "province": "P07", "unit": "assault", "count": 1}])
    st, rep = resolve(st, {"B": book}, rs2, 5)
    assert st.provinces["P07"].fortified and st.provinces["P07"].units == {AS: 4}
    assert not any(e["kind"] == "fortify_broken" for e in rep.events)
    assert rep.idle["B"]["units"] == 0                    # the 2 that moved out are not idle either
    # attacked while kept: the defense counts it
    st.provinces["P02"].units = {AS: 1}
    st2, rep = resolve(st, {"A": orders(st, rs2, "A", [mv("P02", "P07", {"assault": 1})])}, rs2, 5)
    assert battles(rep, "defense")[0]["fortified"] and st2.provinces["P07"].fortified
    # emptied by its own moves: broken (the seat is told), and not fortified once the army comes back
    st3, rep = resolve(st2, {"B": orders(st2, rs2, "B", [mv("P07", "P08", {"assault": 4})])}, rs2, 5)
    assert any(e["kind"] == "fortify_broken" for e in rep.by_seat["B"]) and not st3.provinces["P07"].fortified
    # captured: lost
    st2.provinces["P02"].units = {AR: 9}
    st4, rep = resolve(st2, {"A": orders(st2, rs2, "A", [mv("P02", "P07", {"armor": 9})])}, rs2, 5)
    assert st4.provinces["P07"].owner == "A" and not st4.provinces["P07"].fortified


def test_hasty_penalty_in_resolution(duel, rs2):
    duel.provinces["P02"].units = {AS: 4}
    duel.provinces["P07"].owner = "B"
    duel.provinces["P07"].units = {AS: 4}
    calm = orders(duel, rs2, "A", [mv("P02", "P07", {"assault": 4})])
    hasty = orders(duel, rs2, "A", [mv("P02", "P07", {"assault": 4})], phase=P2)
    assert hasty.orders[0].hasty and hasty.cp_spent == 2
    s_calm = battles(resolve(duel, {"A": calm}, rs2, 9)[1])[0]["sides"][0]["strength"]
    s_hasty = battles(resolve(duel, {"A": hasty}, rs2, 9)[1])[0]["sides"][0]["strength"]
    assert s_hasty == pytest.approx(s_calm * 0.85, rel=1e-3)


def test_head_on_movement(duel, rs2):
    duel.provinces["P09"].owner = "B"
    duel.provinces["P04"].units = {AR: 4}
    duel.provinces["P09"].units = {AS: 8}
    books = {"A": orders(duel, rs2, "A", [mv("P04", "P09", {"armor": 4})]),
             "B": orders(duel, rs2, "B", [mv("P09", "P04", {"assault": 8})])}
    new, rep = resolve(duel, books, rs2, 11)
    (edge,) = [e for e in rep.events if e["kind"] == "edge_battle"]
    assert edge["edge"] == ["P04", "P09"]
    a_side, b_side = edge["sides"]
    assert a_side["won"] and not b_side["won"]           # 4 armor (×1.5 vs assault) beat 8 assault
    assert new.provinces["P04"].owner == "A"             # B never reached P04
    assert any(e["kind"] == "retreat" and e["province"] == "P09" for e in rep.events)
    assert new.provinces["P09"].owner == "A"             # winner continued and beat the retreated remnant


def test_swap_between_own_provinces_is_not_a_battle(duel, rs2):
    duel.provinces["P01"].units = {AS: 2}
    duel.provinces["P02"].units = {AR: 1}
    book = orders(duel, rs2, "A", [mv("P01", "P02", {"assault": 2}), mv("P02", "P01", {"armor": 1})])
    new, rep = resolve(duel, {"A": book}, rs2, 1)
    assert new.provinces["P01"].units == {AR: 1} and new.provinces["P02"].units == {AS: 2}
    assert not battles(rep) and not [e for e in rep.events if e["kind"] == "edge_battle"]


def test_starvation(duel, rs2):
    duel.provinces["P01"].units = {AS: 30}
    duel.players["A"].resources[FOOD] = 0
    new, rep = resolve(duel, {}, rs2, 1)
    # income 9 provisions, upkeep 30 -> starvation: 10% of 30 units lost, stock set to 0
    assert new.provinces["P01"].units == {AS: 27}
    assert new.players["A"].resources[FOOD] == 0
    (ev,) = [e for e in rep.events if e["kind"] == "starvation"]
    assert ev["losses"] == {AS: 3} and ev["seats"] == ["A"]
    duel.provinces["P01"].units = {AS: 10, AR: 1}
    new, _ = resolve(duel, {}, rs2, 1)
    assert new.provinces["P01"].units == {AS: 9, AR: 1}   # minimum one, cheapest first


def test_multi_side_battle(rs3):
    st = layout(rs3, 3, {"P04": "A", "P08": "B", "P12": "C"})
    st.provinces["P13"].units = {AS: 2}                    # neutral garrison
    st.provinces["P04"].units = {AR: 5}
    st.provinces["P08"].units = {AS: 3}
    st.provinces["P12"].units = {AS: 2}
    books = {"A": orders(st, rs3, "A", [mv("P04", "P13", {"armor": 5})]),
             "B": orders(st, rs3, "B", [mv("P08", "P13", {"assault": 3})]),
             "C": orders(st, rs3, "C", [mv("P12", "P13", {"assault": 2})])}
    new, rep = resolve(st, books, rs3, 4)
    first, second = battles(rep, "attackers")
    assert first["sides"][0]["owner"] == "A" and first["sides"][1]["owner"] == "B"   # strongest vs next
    assert second["sides"][1]["owner"] == "C"
    (final,) = battles(rep, "defense")
    assert final["sides"][1]["owner"] is None
    assert new.provinces["P13"].owner == "A"
    assert all(e["province"] == "P13" for e in battles(rep))
    for seat, origin in (("B", "P08"), ("C", "P12")):
        assert set(new.provinces[origin].units) <= {AS}
        assert new.provinces[origin].owner == seat


def test_invalid_orders_dropped_at_resolution(duel, rs2):
    duel.provinces["P01"].units = {AS: 1}
    book = orders(duel, rs2, "A", [mv("P01", "P02", {"assault": 1})])
    duel.provinces["P01"].units = {}
    new, rep = resolve(duel, {"A": book}, rs2, 1)
    (ev,) = [e for e in rep.events if e["kind"] == "order_rejected"]
    assert ev["reason"] == "NOT_ENOUGH_UNITS"
    assert rep.by_seat["A"].count(ev) == 1 and ev not in rep.by_seat["B"]


def test_elimination_and_last_standing(duel, rs2):
    for pid in ("P06", "P07", "P08"):
        duel.provinces[pid].owner = "A"
    duel.provinces["P06"].units = {AR: 5}
    new, rep = resolve(duel, {"A": orders(duel, rs2, "A", [mv("P06", "P05", {"armor": 5})])}, rs2, 1)
    assert not new.players["B"].alive and rep.public["eliminated"] == ["B"]
    assert new.phase == "game_over" and new.result["winners"] == ["A"]
    assert new.result["reason"] == "last_standing" and new.turn == 1


def test_turn_limit_victory_and_draw(duel, rs2):
    duel.turn = rs2.scenario.turn_limit
    new, rep = resolve(duel, {}, rs2, 1)
    assert new.result == {"winners": ["A", "B"], "reason": "turn_limit", "scores": {"A": 7, "B": 7}}
    assert rep.public["result"] == new.result
    duel.provinces["P09"].owner = "B"
    new, _ = resolve(duel, {}, rs2, 1)
    assert new.result["winners"] == ["B"]


def test_report_visibility_and_idle(duel, rs2):
    duel.provinces["P01"].units = {AS: 3}
    duel.provinces["P05"].units = {AS: 3}
    book = orders(duel, rs2, "B", [{"kind": "recruit", "province": "P05", "unit": "assault", "count": 1},
                                   mv("P05", "P06", {"assault": 2})])
    new, rep = resolve(duel, {"B": book}, rs2, 1)
    assert any(e["kind"] == "recruited" for e in rep.by_seat["B"])
    assert not any(e["kind"] in ("recruited", "economy") and "A" not in e["seats"] for e in rep.by_seat["A"])
    assert any(e["kind"] == "economy" for e in rep.by_seat["A"])
    assert rep.idle["A"] == {"resources": {ALLOY: 30, FOOD: 15, CREED: 0}, "units": 3}   # core start resources
    assert rep.idle["B"] == {"resources": {ALLOY: 18, FOOD: 15, CREED: 0}, "units": 2}


def test_attack_from_two_origins_retreats_each_part_home(duel, rs2):
    duel.provinces["P10"].owner = "A"
    duel.provinces["P04"].units = {AS: 3}
    duel.provinces["P10"].units = {AS: 3}
    duel.provinces["P09"].owner = "B"
    duel.provinces["P09"].units = {AS: 8}
    book = orders(duel, rs2, "A", [mv("P04", "P09", {"assault": 3}), mv("P10", "P09", {"assault": 3})])
    new, rep = resolve(duel, {"A": book}, rs2, 3)
    (b,) = battles(rep, "defense")
    assert not b["sides"][0]["won"]
    survivors = 6 - b["sides"][0]["losses"][AS]
    assert 0 < survivors < 6
    back = {e["province"]: e["units"][AS] for e in rep.events if e["kind"] == "retreat" and e["owner"] == "A"} \
        if False else {e["province"]: e["units"][AS] for e in rep.events if e["kind"] == "retreat"}
    assert sum(back.values()) == survivors and set(back) <= {"P04", "P10"}
    assert abs(back.get("P04", 0) - back.get("P10", 0)) <= 1          # losses shared in proportion
    assert new.provinces["P04"].units.get(AS, 0) + new.provinces["P10"].units.get(AS, 0) == survivors


def test_fortify_broken_is_private(duel, rs2):
    duel.provinces["P07"].owner = "B"
    duel.provinces["P07"].units = {AS: 5}
    book = orders(duel, rs2, "B", [{"kind": "fortify", "province": "P07"}, mv("P07", "P05", {"assault": 5})])
    _, rep = resolve(duel, {"B": book}, rs2, 5)
    assert any(e["kind"] == "fortify_broken" for e in rep.by_seat["B"])
    assert not any(e["kind"] == "fortify_broken" for e in rep.by_seat["A"])  # A sees P07 from P02


def test_retreat_is_seen_only_where_its_battle_is_seen(rs3):
    st = layout(rs3, 3, {})
    adj = rs3.map.adjacency
    # A attacks X from Y and is repelled; C sees Y (A's retreat target) but not X.
    x, y, c = next((x, y, c) for x in sorted(adj) for y in sorted(adj[x]) for c in sorted(adj[y])
                   if c != x and x not in adj[c] and x not in rs3.map.capitals)
    st.provinces[y].owner, st.provinces[y].units = "A", {AS: 6}
    st.provinces[x].owner, st.provinces[x].units = "B", {AS: 8}
    st.provinces[c].owner = "C"
    new, rep = resolve(st, {"A": orders(st, rs3, "A", [mv(y, x, {"assault": 6})])}, rs3, 2)
    retreats = [e for e in rep.events if e["kind"] == "retreat"]
    assert retreats and retreats[0]["province"] == y and retreats[0]["from"] == x
    assert all(any(e["kind"] == "retreat" for e in rep.by_seat[seat]) for seat in "AB")
    assert not any(e["kind"] == "retreat" for e in rep.by_seat["C"])


def test_order_ids_are_per_seat(duel, rs2):
    a1 = orders(duel, rs2, "A", [{"kind": "fortify", "province": "P01"}])
    orders(duel, rs2, "B", [{"kind": "fortify", "province": "P05"}, {"kind": "fortify", "province": "P06"}])
    a2 = orders(duel, rs2, "A", [{"kind": "fortify", "province": "P02"}])
    assert [a1.orders[0].id, a2.orders[0].id] == ["o1", "o2"]
