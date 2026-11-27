"""Pacts in resolution (CONCEPT.md section 7, user decision 14): armies of an honored pact never fight each other."""

from __future__ import annotations

import pytest

from dah.engine import SeatOrders, build_ruleset, issue_order_id, new_game_state, resolve, state_hash, submit_orders
from dah.engine import diplomacy as dip

AS, AR = "core:assault", "core:armor"


@pytest.fixture(scope="module")
def rs():
    return build_ruleset(None, "slice3")


def board(rs):
    """mirror3: A owns P01-P04, B P05-P08, C P09-P12; P13-P15 are neutral. All provinces start empty."""
    st = new_game_state(rs, [{"seat": s, "nick": f"Ник{s}"} for s in "ABC"], 1)
    owners = {**dict.fromkeys(["P01", "P02", "P03", "P04"], "A"), **dict.fromkeys(["P05", "P06", "P07", "P08"], "B"),
              **dict.fromkeys(["P09", "P10", "P11", "P12"], "C")}
    for pid, prov in st.provinces.items():
        prov.owner, prov.units, prov.buildings = owners.get(pid), {}, []
    return st


def sign(st, a, b, turns=None, phase="negotiation_1"):
    """Propose and accept a pact between a and b in the current turn; returns the pact."""
    op = dip.make_proposal(st, a, b, turns, phase)
    dip.apply_op(st, op)
    return dip.apply_op(st, dip.make_response(st, b, op["id"], "accept", phase))


def book(st, rs, seat, items, phase="actions"):
    res = submit_orders(st, rs, seat, SeatOrders(), items, phase, id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def mv(src, dst, units, pact=None):
    order = {"kind": "move", "from": src, "to": dst, "units": units}
    if pact:
        order["pact"] = pact
    return order


def kinds(report, kind):
    return [e for e in report.events if e["kind"] == kind]


def test_head_on_with_pact_both_stay_home(rs):
    st = board(rs)
    st.provinces["P02"].units = {AS: 3}
    st.provinces["P07"].units = {AS: 2}
    sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P02", "P07", {"assault": 3})]),
                            "B": book(st, rs, "B", [mv("P07", "P02", {"assault": 2})])}, rs, 1)
    assert not kinds(rep, "edge_battle") and not kinds(rep, "battle")
    assert new.provinces["P02"].units == {AS: 3} and new.provinces["P02"].owner == "A"
    assert new.provinces["P07"].units == {AS: 2} and new.provinces["P07"].owner == "B"
    bounces = kinds(rep, "pact_bounce")
    assert {(e["seat"], e["from"]) for e in bounces} == {("A", "P02"), ("B", "P07")}
    assert all(e["edge"] for e in bounces)
    assert dip.active_pacts(new)[0]["status"] == "active"


def test_entering_partner_province_bounces_without_fight(rs):
    st = board(rs)
    st.provinces["P02"].units = {AS: 5}
    st.provinces["P07"].units = {AS: 1}       # defended
    sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P02", "P07", {"assault": 5})])}, rs, 1)
    assert not kinds(rep, "battle")
    assert new.provinces["P07"].owner == "B" and new.provinces["P07"].units == {AS: 1}
    assert new.provinces["P02"].units == {AS: 5}
    (bounce,) = kinds(rep, "pact_bounce")
    assert (bounce["seat"], bounce["province"], bounce["from"], bounce["edge"]) == ("A", "P07", "P02", None)
    # the empty partner province is not taken either
    st2 = board(rs)
    st2.provinces["P02"].units = {AS: 2}
    sign(st2, "A", "B")
    new2, _ = resolve(st2, {"A": book(st2, rs, "A", [mv("P02", "P07", {"assault": 2})])}, rs, 1)
    assert new2.provinces["P07"].owner == "B" and new2.provinces["P02"].units == {AS: 2}


def test_partners_take_third_party_province_together(rs):
    st = board(rs)
    st.provinces["P13"].owner = "C"
    st.provinces["P13"].units = {AS: 2}
    st.provinces["P04"].units = {AS: 4}
    st.provinces["P08"].units = {AS: 3}
    sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P04", "P13", {"assault": 4})]),
                            "B": book(st, rs, "B", [mv("P08", "P13", {"assault": 3})])}, rs, 1)
    assert not [e for e in kinds(rep, "battle") if e["stage"] == "attackers"]
    defense = [e for e in kinds(rep, "battle") if e["stage"] == "defense"]
    assert defense and defense[0]["sides"][0]["owner"] == "A"          # the stronger partner fights first
    assert new.provinces["P13"].owner == "A"                            # the strongest partner holds it
    assert set(new.provinces["P13"].units) == {AS}
    (joint,) = kinds(rep, "pact_joint")
    assert joint["holder"] == "A" and joint["partners"] == ["B"]
    assert all({s["owner"] for s in e["sides"]} != {"A", "B"} for e in kinds(rep, "battle"))
    assert new.provinces["P08"].units.get(AS, 0) >= 1            # B went home


def test_partners_enter_empty_neutral_province_without_battle(rs):
    st = board(rs)
    st.provinces["P04"].units = {AS: 2}
    st.provinces["P08"].units = {AR: 1, AS: 1}     # stronger: armor 5 + assault 3 against assault 3 + 3
    sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P04", "P14", {"assault": 2})]),
                            "B": book(st, rs, "B", [mv("P08", "P14", {"armor": 1, "assault": 1})])}, rs, 1)
    assert not kinds(rep, "battle")
    assert new.provinces["P14"].owner == "B" and new.provinces["P14"].units == {AR: 1, AS: 1}
    assert new.provinces["P04"].units == {AS: 2}           # the other partner went home whole


def test_pacts_are_not_transitive(rs):
    """A-B and B-C pacts, none between A and C: all three enter P14; A and C fight, B fights nobody."""
    st = board(rs)
    st.provinces["P04"].units = {AS: 3}
    st.provinces["P08"].units = {AS: 2}
    st.provinces["P12"].units = {AS: 3}
    sign(st, "A", "B")
    sign(st, "B", "C")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P04", "P14", {"assault": 3})]),
                            "B": book(st, rs, "B", [mv("P08", "P14", {"assault": 2})]),
                            "C": book(st, rs, "C", [mv("P12", "P14", {"assault": 3})])}, rs, 1)
    fights = [{s["owner"] for s in e["sides"]} for e in kinds(rep, "battle")]
    assert fights == [{"A", "C"}]
    assert new.provinces["P14"].owner in ("A", "B", "C")


def test_break_fights_ends_pact_publicly_and_names_the_breaker(rs):
    st = board(rs)
    st.provinces["P02"].units = {AS: 5}
    st.provinces["P07"].units = {AS: 1}
    pact = sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P02", "P07", {"assault": 5}, "break")])}, rs, 1)
    assert kinds(rep, "battle") and not kinds(rep, "pact_bounce")
    assert new.provinces["P07"].owner == "A"
    ended = dip.pact_by_id(new, pact["id"])
    assert (ended["status"], ended["broken_by"], ended["ended_turn"]) == ("broken", ["A"], 1)
    (broken,) = kinds(rep, "pact_broken")
    assert broken["broken_by"] == ["A"] and broken["pact"] == pact["id"]
    # everyone sees the breach, a third party without the pact id
    seen_by_c = [e for e in rep.by_seat["C"] if e["kind"] == "pact_broken"]
    assert seen_by_c == [{k: v for k, v in broken.items() if k != "pact"}]
    assert [e for e in rep.public["diplomacy"] if e["kind"] == "pact_broken"] == seen_by_c
    assert [e for e in rep.by_seat["B"] if e["kind"] == "pact_broken"] == [broken]


def test_a_breach_is_fought_head_on_but_the_victims_honor_move_still_bounces(rs):
    """Head-on: A breaks, B honors; the breaking move is fought on the edge. B wins it (seed 1), and its honor move
    into A's province then bounces home: only the breaking move lifts the protection."""
    st = board(rs)
    st.provinces["P02"].units = {AS: 3}
    st.provinces["P07"].units = {AS: 3}
    sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P02", "P07", {"assault": 3}, "break")]),
                            "B": book(st, rs, "B", [mv("P07", "P02", {"assault": 3})])}, rs, 1)
    (edge,) = kinds(rep, "edge_battle")
    assert [s["owner"] for s in edge["sides"] if s["won"]] == ["B"]
    assert [(e["seat"], e["province"]) for e in kinds(rep, "pact_bounce")] == [("B", "P02")]
    assert new.provinces["P02"].owner == "A" and new.provinces["P07"].owner == "B"
    assert kinds(rep, "pact_broken")[0]["broken_by"] == ["A"]
    assert not dip.active_pacts(new)


def test_joint_capture_goes_to_the_partner_that_beat_the_defender(rs):
    """Review finding: a slightly weaker partner that never fought must not take the province from the fighter."""
    st = board(rs)
    st.provinces["P13"].owner = "C"
    st.provinces["P13"].units = {AS: 3}
    st.provinces["P04"].units = {AS: 9}
    st.provinces["P08"].units = {AS: 9}
    sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P04", "P13", {"assault": 9})]),
                            "B": book(st, rs, "B", [mv("P08", "P13", {"assault": 9})])}, rs, 1)
    (defense,) = [e for e in kinds(rep, "battle") if e["stage"] == "defense"]
    fighter = defense["sides"][0]["owner"]
    assert defense["sides"][0]["won"] and defense["sides"][0]["losses"]
    (joint,) = kinds(rep, "pact_joint")
    assert joint["holder"] == fighter and new.provinces["P13"].owner == fighter
    other = "B" if fighter == "A" else "A"
    assert new.provinces["P08" if other == "B" else "P04"].units == {AS: 9}     # the other went home whole


def test_joint_partner_whose_origin_fell_falls_back_to_an_own_neighbour(rs):
    """Review finding: the non-holder goes home after the battles; if its origin fell meanwhile, it goes to an own
    province next to the jointly taken one instead of being lost."""
    st = board(rs)
    st.provinces["P13"].owner = "C"
    st.provinces["P13"].units = {AS: 1}
    st.provinces["P14"].owner = "C"
    st.provinces["P14"].units = {AS: 4}
    st.provinces["P15"].owner = "B"            # B's own province next to P13
    st.provinces["P04"].units = {AS: 9}
    st.provinces["P08"].units = {AS: 3}
    sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P04", "P13", {"assault": 9})]),
                            "B": book(st, rs, "B", [mv("P08", "P13", {"assault": 3})]),
                            "C": book(st, rs, "C", [mv("P14", "P08", {"assault": 4})])}, rs, 1)
    assert new.provinces["P13"].owner == "A" and new.provinces["P08"].owner == "C"
    assert not [e for e in kinds(rep, "retreat_lost") if e["seats"] == ["B"]]
    assert new.provinces["P15"].units == {AS: 3}


def test_break_is_aimed_at_the_target_owner_not_at_partners_moving_there_too(rs):
    """Review finding: pacts A-B and A-C. A breaks with C at C's P13 while ally B (honor) also attacks P13: only
    A-C is broken; A and B do not fight in P13, and B's honor move into A's province still bounces."""
    st = board(rs)
    st.provinces["P13"].owner = "C"
    st.provinces["P13"].units = {AS: 3}
    st.provinces["P04"].units = {AS: 8}
    st.provinces["P08"].units = {AS: 6}
    st.provinces["P07"].units = {AS: 3}
    ab = sign(st, "A", "B")
    ac = sign(st, "A", "C")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P04", "P13", {"assault": 8}, "break")]),
                            "B": book(st, rs, "B", [mv("P08", "P13", {"assault": 6}),
                                                    mv("P07", "P02", {"assault": 3})])}, rs, 1)
    assert dip.pact_by_id(new, ac["id"])["status"] == "broken"
    assert dip.pact_by_id(new, ab["id"])["status"] == "active"
    assert not [e for e in kinds(rep, "battle") if {s["owner"] for s in e["sides"]} == {"A", "B"}]
    assert new.provinces["P02"].owner == "A"
    assert ("B", "P02") in [(e["seat"], e["province"]) for e in kinds(rep, "pact_bounce")]


def test_honor_moves_of_the_victim_do_not_turn_into_attacks_when_the_partner_breaks_elsewhere(rs):
    """Review finding: A breaks with B at B's P13; B's honor move into A's empty P02 still bounces (it does not
    become a free attack), and only A is named as the breaker."""
    st = board(rs)
    st.provinces["P13"].owner = "B"
    st.provinces["P13"].units = {AS: 1}
    st.provinces["P04"].units = {AS: 8}
    st.provinces["P07"].units = {AS: 4}
    pact = sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P04", "P13", {"assault": 8}, "break")]),
                            "B": book(st, rs, "B", [mv("P07", "P02", {"assault": 4})])}, rs, 1)
    assert new.provinces["P13"].owner == "A"
    assert new.provinces["P02"].owner == "A" and new.provinces["P07"].units == {AS: 4}
    assert ("B", "P02") in [(e["seat"], e["province"]) for e in kinds(rep, "pact_bounce")]
    assert dip.pact_by_id(new, pact["id"])["broken_by"] == ["A"]


def test_break_against_a_non_partner_changes_nothing(rs):
    st = board(rs)
    st.provinces["P02"].units = {AS: 3}
    sign(st, "A", "B")
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P02", "P01", {"assault": 3}, "break")])}, rs, 1)
    assert not kinds(rep, "pact_broken") and dip.active_pacts(new)


def test_leave_from_p1_ends_before_this_resolution_and_from_p2_before_the_next(rs):
    st = board(rs)
    st.provinces["P02"].units = {AS: 5}
    st.provinces["P07"].units = {AS: 1}
    pact = sign(st, "A", "B")
    dip.apply_op(st, dip.make_leave(st, "B", pact["id"], "negotiation_1"))
    assert dip.pact_by_id(st, pact["id"])["ends_before_turn"] == 1
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P02", "P07", {"assault": 5})])}, rs, 1)
    assert kinds(rep, "battle") and new.provinces["P07"].owner == "A"   # no protection, no breach needed
    assert dip.pact_by_id(new, pact["id"])["status"] == "left" and not kinds(rep, "pact_broken")
    assert kinds(rep, "pact_ended")[0]["reason"] == "left"

    st = board(rs)
    st.provinces["P02"].units = {AS: 5}
    pact = sign(st, "A", "B")
    dip.apply_op(st, dip.make_leave(st, "A", pact["id"], "negotiation_2"))
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("P02", "P07", {"assault": 5})])}, rs, 1)
    assert kinds(rep, "pact_bounce") and dip.pact_by_id(new, pact["id"])["status"] == "active"
    new2, rep2 = resolve(new, {}, rs, 1)
    assert dip.pact_by_id(new2, pact["id"])["status"] == "left" and kinds(rep2, "pact_ended")


def test_expiry_lapse_and_elimination(rs):
    st = board(rs)
    pact = sign(st, "A", "B", turns=1)
    assert pact["until_turn"] == 1
    lapsing = dip.make_proposal(st, "A", "C", 2, "negotiation_2")
    dip.apply_op(st, lapsing)
    new, rep = resolve(st, {}, rs, 1)
    assert dip.pact_by_id(new, pact["id"])["status"] == "expired" and kinds(rep, "pact_expired")
    assert dip.pact_by_id(new, lapsing["id"])["status"] == "lapsed"
    assert [e["kind"] for e in rep.by_seat["B"] if e["kind"].startswith("pact_")] == ["pact_expired"]
    assert not [e for e in rep.by_seat["B"] if e["kind"] in ("pact_proposed", "pact_lapsed")]   # A-C's own business
    # a side that is eliminated ends its pacts
    st = board(rs)
    for pid in ("P09", "P10", "P11", "P12"):
        st.provinces[pid].owner = "A"
    st.players["C"].alive = True
    pact = sign(st, "B", "C")
    new, rep = resolve(st, {}, rs, 1)
    assert dip.pact_by_id(new, pact["id"])["status"] == "ended" and kinds(rep, "pact_ended")[0]["reason"] == \
        "eliminated"


def test_pacts_change_no_hash_without_diplomacy_and_resolve_deterministically(rs):
    st = board(rs)
    st.provinces["P02"].units = {AS: 3}
    plain = state_hash(st)
    assert "pacts" not in st.to_dict() or st.to_dict()["pacts"] == []
    sign(st, "A", "B")
    assert state_hash(st) != plain
    books = {"A": book(st, rs, "A", [mv("P02", "P07", {"assault": 3}, "break")])}
    one, _ = resolve(st, books, rs, 5)
    two, _ = resolve(st, books, rs, 5)
    assert state_hash(one) == state_hash(two)
