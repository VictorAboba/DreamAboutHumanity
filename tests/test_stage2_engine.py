"""Stage-2 engine: research, unit/building availability, movement range, abilities, province stats, fog, hashes
(TECH_AND_SETUP.md sections 1 and 5)."""

from __future__ import annotations

import copy

import pytest

from dah.engine import (Profile, SeatOrders, build_ruleset, estimate, income_of, new_game_state, resolve,
                        stat_key, state_hash, submit_orders, techs_view, view_state)
from dah.engine.combat import Force, strength
from dah.engine.model import sha256_json
from dah.engine.techs import province_defense_mul, province_stat
from stage2_helpers import CORE020, capital, center, own, ruleset, state


@pytest.fixture
def rs():
    return ruleset()


def submit(st, rs, seat, items, phase="actions", book=None):
    return submit_orders(st, rs, seat, book or SeatOrders(), items, phase)


def reasons(res):
    return [c.reason for c in res.results]


def step(st, rs, books=None, seed=5):
    """Resolve one turn with {seat: [order dicts]} given in actions."""
    out = {}
    for seat, items in (books or {}).items():
        res = submit(st, rs, seat, items)
        assert not res.rejected, [c.to_dict() for c in res.rejected]
        out[seat] = res.book
    return resolve(st, out, rs, seed)


# ---------------------------------------------------------------- research orders

def test_research_order_phases_and_validation(rs):
    st = state(rs)
    assert reasons(submit(st, rs, "A", [{"kind": "research", "tech": "smithing"}], phase="negotiation_1")) == [
        "PHASE_LOCKED"]
    assert submit(st, rs, "A", [{"kind": "research", "tech": "Smithing"}]).results[0].ok       # en name
    assert submit(st, rs, "A", [{"kind": "research", "tech": "Кузнечное дело"}]).results[0].ok  # ru name
    assert reasons(submit(st, rs, "A", [{"kind": "research", "tech": "smithing"}], phase="negotiation_2")) == [
        "PHASE_LOCKED"]
    assert reasons(submit(st, rs, "A", [{"kind": "research", "tech": "alchemy"}])) == ["UNKNOWN_ID"]
    assert reasons(submit(st, rs, "A", [{"kind": "research"}])) == ["INVALID"]
    assert reasons(submit(st, rs, "A", [{"kind": "research", "tech": "doctrine"}])) == ["NOT_AVAILABLE"]
    assert reasons(submit(st, rs, "B", [{"kind": "research", "tech": "iron_rite"}])) == ["NOT_AVAILABLE"]  # ash
    assert submit(st, rs, "A", [{"kind": "research", "tech": "iron_rite"}]).results[0].ok              # iron
    two = submit(st, rs, "A", [{"kind": "research", "tech": "smithing"}, {"kind": "research", "tech": "drills"}])
    assert reasons(two) == [None, "INVALID"]
    st.players["A"].techs.append("t2:smithing")
    assert reasons(submit(st, rs, "A", [{"kind": "research", "tech": "smithing"}])) == ["ALREADY_RESEARCHED"]


def test_research_order_round_trips_and_is_amendable_only_in_actions(rs):
    from dah.engine import amend_order, cancel_order
    from dah.engine.model import Order

    st = state(rs)
    res = submit(st, rs, "A", [{"kind": "research", "tech": "smithing"}])
    order = res.book.orders[0]
    assert order.to_dict() == {"id": order.id, "kind": "research", "phase_added": "actions", "tech": "t2:smithing"}
    assert Order.from_dict(order.to_dict()) == order
    amended = amend_order(st, rs, "A", res.book, order.id, {"tech": "drills"}, "actions")
    assert amended.ok and amended.order.tech == "t2:drills"
    assert amend_order(st, rs, "A", res.book, order.id, {"tech": "drills"}, "negotiation_2").reason == "PHASE_LOCKED"
    assert cancel_order(st, rs, "A", res.book, order.id, "actions").ok


def test_research_without_research_resource_is_not_available():
    rs = build_ruleset(CORE020, "slice2")
    st = new_game_state(rs, [{"seat": "A", "nick": "А"}, {"seat": "B", "nick": "Б"}], 1)
    assert reasons(submit(st, rs, "A", [{"kind": "research", "tech": "x"}])) == ["NOT_AVAILABLE"]


# ---------------------------------------------------------------- research in resolution

def test_research_transfer_completion_and_effects_next_turn(rs):
    st = state(rs)                                  # A iron (war x0.5), B ash; creed income 2 + 3 = 5
    assert st.players["A"].resources["t2:creed"] == 0
    st1, rep = step(st, rs, {"A": [{"kind": "research", "tech": "smithing"}],
                             "B": [{"kind": "research", "tech": "drills"}]})
    a, b = st1.players["A"], st1.players["B"]
    assert a.techs == ["t2:smithing"] and a.researching is None and a.research == {}
    assert a.resources["t2:creed"] == 5 - 3          # cost 6 x 0.5; the surplus stays in stock
    assert b.techs == ["t2:drills"] and b.resources["t2:creed"] == 0
    kinds = [(e["kind"], e["seats"]) for e in rep.events if e["kind"] in ("research_started", "tech_completed")]
    assert kinds == [("research_started", ["A"]), ("research_started", ["B"]), ("tech_completed", ["A"]),
                     ("tech_completed", ["B"])]
    economy = next(e for e in rep.events if e["kind"] == "economy" and e["seats"] == ["B"])
    assert economy["research"] == {"tech": "t2:drills", "added": 5, "progress": 5, "cost": 5}
    assert rep.public["techs_count"] == {"A": 1, "B": 1}
    assert all(e["kind"] not in ("research_started", "tech_completed") for e in rep.by_seat["A"]
               if e["seats"] == ["B"])            # private to the researcher
    # drills (alloy x1.5) did not change this turn's income, it does the next one
    alloy_before = st.players["B"].resources["t2:alloy"]
    assert b.resources["t2:alloy"] - alloy_before == round(7 * 1.25) - 0      # 3 capital + 4 forge, ash x1.25
    assert income_of(st1, rs, "B")["t2:alloy"] == round(7 * 1.25 * 1.5 + 1e-9)


def test_research_progress_is_kept_per_tech_and_active_tech_persists(rs):
    st = state(rs, factions=("ash", "iron"))         # A ash: fortification costs 8
    st, _ = step(st, rs, {"A": [{"kind": "research", "tech": "fortification"}]})
    a = st.players["A"]
    assert a.research == {"t2:fortification": 5} and a.researching == "t2:fortification"
    st, _ = step(st, rs, {"A": [{"kind": "research", "tech": "drills"}]})      # switch: progress kept
    a = st.players["A"]
    assert a.techs == ["t2:drills"] and a.research == {"t2:fortification": 5} and a.researching is None
    st, _ = step(st, rs, {"A": [{"kind": "research", "tech": "fortification"}]})
    a = st.players["A"]
    assert a.techs == ["t2:drills", "t2:fortification"] and a.resources["t2:creed"] == 5 - 3
    st, _ = step(st, rs, {"A": [{"kind": "research", "tech": "smithing"}]})     # 2 + 5 >= 6
    assert st.players["A"].techs[-1] == "t2:smithing" and st.players["A"].resources["t2:creed"] == 1
    st, _ = step(st, rs, {"A": [{"kind": "research", "tech": "doctrine"}]})
    assert st.players["A"].research == {"t2:doctrine": 6} and st.players["A"].resources["t2:creed"] == 0
    st, _ = step(st, rs)                              # no order: the active tech keeps getting points
    assert st.players["A"].techs[-1] == "t2:doctrine" and st.players["A"].resources["t2:creed"] == 1


def test_requires_tier_requires_any_and_flags(rs):
    st = state(rs)
    a = st.players["A"]
    assert reasons(submit(st, rs, "A", [{"kind": "research", "tech": "doctrine"}])) == ["NOT_AVAILABLE"]
    a.techs += ["t2:smithing", "t2:fortification"]
    assert submit(st, rs, "A", [{"kind": "research", "tech": "doctrine"}]).results[0].ok
    assert reasons(submit(st, rs, "B", [{"kind": "research", "tech": "banking"}])) == ["NOT_AVAILABLE"]
    st.players["B"].techs += ["t2:drills"]
    assert reasons(submit(st, rs, "B", [{"kind": "research", "tech": "banking"}])) == ["NOT_AVAILABLE"]
    st.players["B"].techs += ["t2:fortification"]
    st.players["B"].resources["t2:creed"] = 10
    st1, _ = step(st, rs, {"B": [{"kind": "research", "tech": "banking"}]})
    assert "t2:banking" in st1.players["B"].techs and "banking" in st1.players["B"].flags
    assert rs.passive_income(stat_key(st1, rs, "B")) == {"t2:alloy": 2}


def test_command_points_from_a_tech_count_from_the_next_turn(rs):
    st = state(rs)
    st.players["A"].techs += ["t2:smithing", "t2:fortification"]
    st.players["A"].resources["t2:creed"] = 20
    st1, _ = step(st, rs, {"A": [{"kind": "research", "tech": "doctrine"}]})
    assert "t2:doctrine" in st1.players["A"].techs and st1.players["A"].command_points == 4
    assert st1.players["B"].command_points == 3


def test_techs_view_shows_own_tree(rs):
    st = state(rs)
    st.players["A"].researching = "t2:fortification"
    st.players["A"].research = {"t2:fortification": 2}
    view = techs_view(st, rs, "A", 5)
    assert view["enabled"] and view["resource"] == "t2:creed" and view["income"] == 5
    assert view["active"] == {"tech": "t2:fortification", "progress": 2, "cost": 4}
    status = {t["id"]: t["status"] for t in view["techs"]}
    assert status["t2:fortification"] == "active" and status["t2:smithing"] == "available"
    assert status["t2:doctrine"] == "locked" and "t2:iron_rite" in status
    assert "t2:iron_rite" not in {t["id"] for t in techs_view(st, rs, "B")["techs"]}   # another faction's tech
    assert [b["id"] for b in view["branches"]] == ["t2:war", "t2:economy"]


# ---------------------------------------------------------------- availability of units and buildings

def test_unit_requires_tech_and_building(rs):
    st = state(rs)
    cap = capital(st, "A")
    order = {"kind": "recruit", "province": cap, "unit": "knight", "count": 1}
    res = submit(st, rs, "A", [order])
    assert reasons(res) == ["NOT_AVAILABLE"] and "Кузнечное дело" in res.results[0].message
    st.players["A"].techs.append("t2:smithing")
    res = submit(st, rs, "A", [order])
    assert reasons(res) == ["NOT_AVAILABLE"] and "Кузня" in res.results[0].message
    st.provinces[cap].buildings.append("t2:smithy")
    assert submit(st, rs, "A", [order]).results[0].ok


def test_unique_unit_and_unlock_from_an_artifact(rs):
    st = state(rs, artifacts={"A": ["banner"]})
    for seat in "AB":
        st.provinces[capital(st, seat)].buildings.append("t2:smithy")
    raider = {"kind": "recruit", "province": capital(st, "B"), "unit": "raider", "count": 1}
    assert reasons(submit(st, rs, "B", [raider])) == ["NOT_AVAILABLE"]                  # ash
    assert submit(st, rs, "A", [{**raider, "province": capital(st, "A")}]).results[0].ok  # iron
    knight = {"kind": "recruit", "unit": "knight", "count": 1}
    assert submit(st, rs, "A", [{**knight, "province": capital(st, "A")}]).results[0].ok   # banner: no tech needed
    assert reasons(submit(st, rs, "B", [{**knight, "province": capital(st, "B")}])) == ["NOT_AVAILABLE"]


def test_building_requires_tech_and_faction(rs):
    st = state(rs)
    forge_a, forge_b = own(st, "A", "forge"), own(st, "B", "forge")
    assert reasons(submit(st, rs, "A", [{"kind": "build", "province": forge_a, "building": "citadel"}])) == [
        "NOT_AVAILABLE"]
    st.players["A"].techs.append("t2:fortification")
    assert submit(st, rs, "A", [{"kind": "build", "province": forge_a, "building": "citadel"}]).results[0].ok
    assert reasons(submit(st, rs, "A", [{"kind": "build", "province": forge_a, "building": "market"}])) == [
        "NOT_AVAILABLE"]
    assert submit(st, rs, "B", [{"kind": "build", "province": forge_b, "building": "market"}]).results[0].ok


# ---------------------------------------------------------------- building scopes and province stats

def test_citadel_province_defense_and_owner_command_points(rs):
    st = state(rs)
    cap = capital(st, "A")
    assert province_defense_mul(st, rs, cap) == 1.5
    st.provinces[cap].buildings.append("t2:citadel")
    assert province_stat(st, rs, cap, "province.defense_mul") == 1.5
    assert province_defense_mul(st, rs, cap) == pytest.approx(2.25)
    assert province_defense_mul(st, rs, own(st, "A", "forge")) == 1.0      # province scope: only its province
    key = stat_key(st, rs, "A")
    assert key == Profile("t2:iron", buildings=(("t2:citadel", 1),))
    assert rs.stat("command_points", key) == 4
    st1, _ = step(st, rs)
    assert st1.players["A"].command_points == 4 and st1.players["B"].command_points == 3


def test_market_income_multiplier_and_recruit_discount(rs):
    st = state(rs, factions=("ash", "iron"))
    forge, cap = own(st, "A", "forge"), capital(st, "A")
    base = income_of(st, rs, "A")["t2:alloy"]
    st.provinces[forge].buildings.append("t2:market")
    assert income_of(st, rs, "A")["t2:alloy"] == round((3 + 4 * 1.5) * 1.25)
    assert base == round(7 * 1.25)
    st.provinces[cap].buildings.append("t2:market")
    res = submit(st, rs, "A", [{"kind": "recruit", "province": cap, "unit": "militia", "count": 3}])
    from dah.engine import seat_reserved
    assert seat_reserved(st, rs, "A", res.book) == {"t2:alloy": 15}        # 30 - 50%
    st1, _ = resolve(st, {"A": res.book}, rs, 1)
    assert st1.provinces[cap].units["t2:militia"] == 6


# ---------------------------------------------------------------- movement range

def test_move_through_own_land_up_to_range(rs):
    st = state(rs)
    cap, shrine = capital(st, "A"), own(st, "A", "shrine")
    mid = center(st, rs, "A")
    ok = submit(st, rs, "A", [{"kind": "move", "from": cap, "to": shrine, "unit": "militia", "count": 2}])
    assert ok.results[0].ok                                            # 2 edges via own forge
    far = submit(st, rs, "A", [{"kind": "move", "from": cap, "to": mid, "unit": "militia", "count": 1}])
    assert reasons(far) == ["NOT_ADJACENT"] and "до 2 рёбер" in far.results[0].message
    st1, _ = resolve(st, {"A": ok.book}, rs, 1)
    assert st1.provinces[shrine].units == {"t2:militia": 2} and st1.provinces[cap].units == {"t2:militia": 1}
    st.provinces[cap].units["t2:ram"] = 1
    assert reasons(submit(st, rs, "A", [{"kind": "move", "from": cap, "to": shrine, "unit": "ram",
                                         "count": 1}])) == ["NOT_ADJACENT"]          # move: 1


def test_move_onto_foreign_land_is_one_edge_after_own_ones(rs):
    st = state(rs)
    forge, mid = own(st, "A", "forge"), center(st, rs, "A")
    st.provinces[forge].units = {"t2:militia": 2}
    res = submit(st, rs, "A", [{"kind": "move", "from": forge, "to": mid, "unit": "militia", "count": 2}])
    assert res.results[0].ok                                           # forge -> own shrine -> neutral centre
    st1, rep = resolve(st, {"A": res.book}, rs, 1)
    assert st1.provinces[mid].owner == "A"
    other = next(n for n in rs.map.adjacency[mid] if st.provinces[n].owner is None)
    st.provinces[own(st, "A", "shrine")].units = {"t2:militia": 1}
    assert reasons(submit(st, rs, "A", [{"kind": "move", "from": own(st, "A", "shrine"), "to": other,
                                         "unit": "militia", "count": 1}])) == ["NOT_ADJACENT"]


def test_fast_units_cross_foreign_land(rs):
    st = state(rs)
    shrine, mid = own(st, "A", "shrine"), center(st, rs, "A")
    beyond = next(n for n in rs.map.adjacency[mid] if n != shrine)
    st.provinces[shrine].units = {"t2:raider": 1, "t2:militia": 1}
    assert submit(st, rs, "A", [{"kind": "move", "from": shrine, "to": beyond, "unit": "raider",
                                 "count": 1}]).results[0].ok
    mixed = submit(st, rs, "A", [{"kind": "move", "from": shrine, "to": beyond,
                                  "units": {"raider": 1, "militia": 1}}])
    assert reasons(mixed) == ["NOT_ADJACENT"]                          # the whole stack must be fast


# ---------------------------------------------------------------- abilities in combat

def test_garrison_counts_only_for_a_defender_in_its_own_province(rs):
    guards = {"t2:guard": 2}
    assert strength(rs, Force("A", dict(guards)), {}, defending=True) == (2 + 2) * 2
    assert strength(rs, Force(None, dict(guards)), {}, defending=True) == 2 * 2
    assert strength(rs, Force("A", dict(guards)), {}, defending=False) == 1 * 2


def test_siege_caps_the_province_multiplier_by_attack_share(rs):
    defender = Force("B", {"t2:militia": 2})
    plain = strength(rs, defender, {"t2:militia": 1}, defending=True, defense_mul=1.5)
    assert plain == pytest.approx(2 * 2 * 1.5)
    rams = strength(rs, defender, {"t2:ram": 1}, defending=True, defense_mul=1.5)
    assert rams == pytest.approx(2 * 2 * 1.0)
    mixed = strength(rs, defender, {"t2:ram": 1, "t2:militia": 2}, defending=True, defense_mul=1.5)
    assert mixed == pytest.approx(4 * (0.5 * 1.0 + 0.5 * 1.5))         # ram 4 of 8 attack power


def test_ignore_fortify_removes_the_bonus_for_its_share(rs):
    defender = Force("B", {"t2:militia": 2})
    bonus = rs.stat("combat.fortify_bonus")
    assert strength(rs, defender, {"t2:militia": 1}, defending=True, fortified=True) == pytest.approx(
        4 * (1 + bonus))
    assert strength(rs, defender, {"t2:ram": 1}, defending=True, fortified=True) == pytest.approx(4)
    assert strength(rs, defender, {"t2:ram": 1, "t2:militia": 2}, defending=True, fortified=True) == pytest.approx(
        4 * (1 + bonus / 2))


def test_estimate_accepts_stat_keys_and_province_multiplier(rs):
    base = estimate(rs, {"militia": 3}, {"guard": 2}, "capital")
    assert base == estimate(rs, {"militia": 3}, {"guard": 2}, "capital", attacker_key=None, defender_key=None)
    walls = estimate(rs, {"militia": 3}, {"guard": 2}, "capital", defense_mul=3.0)
    assert walls["win_prob"] <= base["win_prob"]


def test_abilities_and_techs_change_resolution(rs):
    st = state(rs)
    cap_b = capital(st, "B")
    forge_b = own(st, "B", "forge")
    st.provinces[forge_b].units = {"t2:militia": 2}
    st.provinces[cap_b].units = {"t2:guard": 2}
    attacker = copy.deepcopy(st)
    # A cannot reach B's land in one turn on this map: put A's army next to B's capital through a test edit
    attacker.provinces[forge_b].owner = "A"
    attacker.provinces[forge_b].units = {"t2:militia": 3}
    res = submit(attacker, rs, "A", [{"kind": "move", "from": forge_b, "to": cap_b, "unit": "militia", "count": 3}])
    st1, rep = resolve(attacker, {"A": res.book}, rs, 3)
    battle = next(e for e in rep.events if e["kind"] == "battle")
    defense = next(s for s in battle["sides"] if s["owner"] == "B")
    # guard 2+2 garrison, x2 units, x1.5 capital, x rng 0.9..1.1
    assert 4 * 2 * 1.5 * 0.9 <= defense["strength"] <= 4 * 2 * 1.5 * 1.1


# ---------------------------------------------------------------- start artifacts

def test_start_artifacts_apply_one_off_and_passive_effects(rs):
    st = state(rs, artifacts={"A": ["ember", "warband", "seal"], "B": ["tithe", "cache"]})
    a, b = st.players["A"], st.players["B"]
    assert a.artifacts == ["t2:ember", "t2:warband", "t2:seal"] and a.techs == ["t2:smithing"]
    assert a.resources["t2:alloy"] == 30 + 15 and b.resources["t2:alloy"] == 30 + 30
    assert st.provinces[capital(st, "A")].units == {"t2:militia": 5}
    assert a.command_points == 4 and b.command_points == 3
    assert income_of(st, rs, "B")["t2:creed"] == 2 + 3 + 2


# ---------------------------------------------------------------- hashes, fog, determinism

def test_state_hash_ignores_empty_stage2_fields(rs):
    st = state(rs)
    data = st.to_dict()
    data.pop("seq")
    data.pop("pacts")
    for key in ("claims", "origins", "abandoned", "discoveries", "trades", "trade_seq"):    # stages 3, 5, trade
        data.pop(key)
    for p in data["players"].values():
        for key in ("techs", "research", "researching", "artifacts", "home", "shades", "spy_seq", "conquest_policy",
                    "tally", "objectives", "intel_copies"):        # stage 4 (SCORING.md 3), empty: not hashed either
            p.pop(key)
    for prov in data["provinces"].values():
        prov.pop("status")
    assert state_hash(st) == sha256_json(data)
    st.players["A"].techs.append("t2:smithing")
    assert state_hash(st) != sha256_json(data)


def test_slice_state_and_stat_key_are_unchanged():
    rs = build_ruleset(CORE020, "slice2")
    st = new_game_state(rs, [{"seat": "A", "nick": "А"}, {"seat": "B", "nick": "Б"}], 3)
    assert stat_key(st, rs, "A") == "core:covenant"
    assert "techs" not in view_state(st, rs, "A")["you"]
    assert all("techs_count" not in p for p in view_state(st, rs, "A")["players"])


def test_fog_own_techs_full_others_only_count(rs):
    st = state(rs, artifacts={"B": ["cache"]})
    st.players["B"].techs = ["t2:drills"]
    st.players["B"].researching = "t2:smithing"
    view = view_state(st, rs, "A")
    assert view["you"]["techs"] == [] and view["you"]["artifacts"] == []
    b = next(p for p in view["players"] if p["seat"] == "B")
    assert b == {"seat": "B", "nick": "Бета", "alive": True, "score": b["score"], "faction": "t2:ash",
                 "techs_count": 1, "score_exact": False}       # another seat's score: a lower bound (SCORING.md 5)
    assert "t2:drills" not in str(view) and "t2:cache" not in str(view) and "t2:smithing" not in str(view)
    mine = view_state(st, rs, "B")["you"]
    assert mine["techs"] == ["t2:drills"] and mine["artifacts"] == ["t2:cache"]
    assert mine["researching"] == {"tech": "t2:smithing", "progress": 0, "cost": 6}


def test_resolution_with_research_is_deterministic(rs):
    def play(seed):
        st = state(rs, artifacts={"A": ["ember"], "B": ["seal"]})
        hashes = []
        plan = [{"A": [{"kind": "research", "tech": "fortification"}], "B": [{"kind": "research", "tech": "drills"}]},
                {"A": [{"kind": "move", "from": capital(st, "A"), "to": own(st, "A", "shrine"), "unit": "militia",
                        "count": 2}]},
                {"A": [{"kind": "move", "from": own(st, "A", "shrine"), "to": center(st, rs, "A"),
                        "unit": "militia", "count": 2}], "B": [{"kind": "research", "tech": "fortification"}]},
                {}]
        for books in plan:
            st, _ = step(st, rs, books, seed)
            hashes.append(state_hash(st))
        return hashes, st

    first, st = play(11)
    assert first == play(11)[0]
    assert st.players["A"].techs == ["t2:smithing", "t2:fortification"]
    assert st.provinces[center(state(rs), rs, "A")].owner == "A"


def test_unlock_order_gates_the_order_kind(tmp_path):
    from stage2_helpers import edit_yaml, pack_copy

    pack = pack_copy(tmp_path)
    edit_yaml(pack / "artifacts" / "artifacts.yaml",
              lambda d: d + [{"id": "drill", "name": "Устав", "power": 1, "cost": 1,
                              "effects": [{"effect": "unlock", "kind": "order", "id": "fortify"}]}])
    rs = build_ruleset([pack], "duel_draft")
    assert rs.gated_orders == {"fortify"}
    st = state(rs, artifacts={"A": ["drill"]})
    assert submit(st, rs, "A", [{"kind": "fortify", "province": capital(st, "A")}]).results[0].ok
    assert reasons(submit(st, rs, "B", [{"kind": "fortify", "province": capital(st, "B")}])) == ["NOT_AVAILABLE"]
    assert ruleset().gated_orders == frozenset()
