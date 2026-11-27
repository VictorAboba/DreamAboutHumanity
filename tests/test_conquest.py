"""Conquest in resolution (docs/SPY_AND_CONQUEST.md 3, 4): occupation, fates, claims, revolts, retreats, scores."""

from __future__ import annotations

import pytest

from conquest_helpers import MIL, board, book, dispose, kinds, mv, occupy, ruleset
from dah.engine import (SeatOrders, check_order, income_of, resolve, scores, seat_reserved, stat_key, state_hash)
from dah.engine import diplomacy as dip
from dah.engine.model import PendingBuild
from dah.engine.techs import province_defense_mul

PROV, ALLOY = "t3:provisions", "t3:alloy"


@pytest.fixture(scope="module")
def rs():
    return ruleset()


def run(st, rs, books, seed=1):
    return resolve(st, books, rs, seed)


def recruit(pid):
    return {"kind": "recruit", "province": pid, "unit": "militia", "count": 1}


# ---------------------------------------------------------------- captures and occupation

def test_neutral_capture_is_full_and_free(rs):
    st = board(rs, {"A2": {"militia": 2}})
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "M", 2)])})
    assert new.provinces["M"].owner == "A" and not new.provinces["M"].status
    cap = kinds(rep, "captured")[0]
    assert cap["mode"] == "free" and cap["status"] == "full"
    assert not new.claims


def test_capture_from_a_player_is_an_occupation_without_income_score_builds(rs):
    st = board(rs, {"A2": {"militia": 3}})
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2")])})
    b2 = new.provinces["B2"]
    assert b2.owner == "A" and b2.status["occupied"] == {"by": "A", "from": "B", "since": 1}
    cap = kinds(rep, "captured")[0]
    assert (cap["mode"], cap["status"]) == ("occupied", "occupied")
    assert new.claims == [{"seat": "B", "province": "B2", "until_turn": 11}]
    assert kinds(rep, "claim_created")[0]["seat"] == "B"
    # no income and no points from the occupation; the capital and own forge still count
    assert income_of(new, rs, "A")[ALLOY] == 3 + 4
    assert scores(new, rs)["A"] == 4 + 1
    for item in (recruit("B2"), {"kind": "build", "province": "B2", "building": "hq"}):
        assert check_order(new, rs, "A", SeatOrders(), item, "actions").reason == "OCCUPIED"
    # the public form of a capture has no mode (a directive or a policy would tell the price was free)
    seen_by_c = [e for e in rep.by_seat["C"] if e["kind"] == "captured"]
    assert seen_by_c and "mode" not in seen_by_c[0] and seen_by_c[0]["status"] == "occupied"
    assert rep.public["ownership_changes"] == [{"province": "B2", "from": "B", "to": "A", "status": "occupied"}]


def test_revolt_without_garrison_only_for_an_occupation_older_than_the_resolution(rs):
    st = board(rs, {"A2": {"militia": 3}})
    st, _ = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2")])})
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("B2", "A2")])})
    assert new.provinces["B2"].owner is None and "occupied" not in new.provinces["B2"].status
    revolt = kinds(rep, "occupation_revolt")[0]
    assert revolt["province"] == "B2" and revolt["seat"] == "A"
    assert any(e["kind"] == "occupation_revolt" for e in rep.by_seat["C"])      # public


def test_directive_pays_from_its_reserve_and_the_province_is_full(rs):
    st = board(rs, {"A2": {"militia": 3}})
    plain, _ = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2")])})
    b = book(st, rs, "A", [mv("A2", "B2"), dispose("B2", "subjugate")])
    assert seat_reserved(st, rs, "A", b) == {PROV: 5}
    new, rep = run(st, rs, {"A": b})
    assert new.provinces["B2"].owner == "A" and not new.provinces["B2"].status.get("occupied")
    fate = kinds(rep, "disposition")[0]
    assert (fate["source"], fate["cost"], fate["status"]) == ("directive", {PROV: 5}, "subjugated")
    assert kinds(rep, "captured")[0]["mode"] == "directive"
    # B2 is a forge (alloy only), so the provisions differ by exactly the price; alloy differs by its income
    assert new.players["A"].resources[PROV] == plain.players["A"].resources[PROV] - 5
    assert new.players["A"].resources[ALLOY] == plain.players["A"].resources[ALLOY] + 4
    assert new.claims == [{"seat": "B", "province": "B2", "until_turn": 11}]
    # a third seat sees the fate without price, loot or source
    public = next(e for e in rep.by_seat["C"] if e["kind"] == "disposition")
    assert not {"cost", "loot", "source", "disposition"} & public.keys() and public["status"] == "subjugated"


def test_directive_without_a_capture_comes_back_in_full(rs):
    st = board(rs)
    base, _ = run(st, rs, {})
    new, rep = run(st, rs, {"A": book(st, rs, "A", [dispose("B1", "subjugate")])})
    assert new.players["A"].resources == base.players["A"].resources
    assert not kinds(rep, "disposition")


def test_policy_needs_free_resources_else_occupation(rs):
    st = board(rs, {"A1": {"militia": 9}, "B1": {"militia": 1}})
    st.players["A"].resources[PROV] = 10           # a capital costs 5 x 3 = 15
    new, rep = run(st, rs, {"A": book(st, rs, "A", [{"kind": "policy", "disposition": "subjugate"},
                                                     mv("A1", "B1", 9)])})
    assert new.players["A"].conquest_policy == "t3:subjugate"
    assert new.provinces["B1"].status["occupied"]["by"] == "A"
    miss = kinds(rep, "policy_unaffordable")[0]
    assert miss["reason"] == "resources" and miss["cost"] == {PROV: 15}
    assert not any(e["kind"] in ("policy_set", "policy_unaffordable") for e in rep.by_seat["B"])   # private
    st.players["A"].resources[PROV] = 30
    new, rep = run(st, rs, {"A": book(st, rs, "A", [{"kind": "policy", "disposition": "subjugate"},
                                                     mv("A1", "B1", 9)])})
    assert not new.provinces["B1"].status.get("occupied")
    fate = kinds(rep, "disposition")[0]
    assert fate["source"] == "policy" and fate["cost"] == {PROV: 15}
    assert kinds(rep, "captured")[0]["mode"] == "policy"


def test_policy_persists_and_null_clears_it(rs):
    st = board(rs)
    st, rep = run(st, rs, {"A": book(st, rs, "A", [{"kind": "policy", "disposition": "Разграбить"}])})
    assert st.players["A"].conquest_policy == "t3:raid" and kinds(rep, "policy_set")
    st, rep = run(st, rs, {})
    assert st.players["A"].conquest_policy == "t3:raid" and not kinds(rep, "policy_set")
    st, rep = run(st, rs, {"A": book(st, rs, "A", [{"kind": "policy", "disposition": None}])})
    assert st.players["A"].conquest_policy is None


# ---------------------------------------------------------------- fates

def test_exterminate_loots_destroys_buildings_and_builds_and_ruins(rs):
    st = board(rs, {"A2": {"militia": 3}})
    st.provinces["B2"].buildings = ["t3:hall"]
    st.pending_builds.append(PendingBuild("B", "B2", "t3:hq", 1))
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2"), dispose("B2", "exterminate")])})
    b2 = new.provinces["B2"]
    assert b2.owner == "A" and b2.buildings == [] and not new.pending_builds
    assert b2.status["ruined_until"] == 7 and b2.status["looted_turn"] == 1
    fate = kinds(rep, "disposition")[0]
    assert fate["loot"] == {ALLOY: 5, PROV: 3} and fate["cost"] == {}
    assert kinds(rep, "captured")[0]["status"] == "ruined"
    assert scores(new, rs)["A"] == 5
    assert check_order(new, rs, "A", SeatOrders(), recruit("B2"), "actions").reason == "RUINED"
    assert check_order(new, rs, "C", SeatOrders(), dispose("B2", "subjugate"), "actions").reason == "RUINED"
    assert check_order(new, rs, "C", SeatOrders(), dispose("B2", "raid"), "actions").reason == "RUINED"
    assert check_order(new, rs, "C", SeatOrders(), dispose("B2", "exterminate"), "actions").ok


def test_ruins_end_after_ruin_turns_and_income_resumes(rs):
    st = board(rs, {"A2": {"militia": 3}})
    st, _ = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2"), dispose("B2", "exterminate")])})
    ended_at = None
    while ended_at is None:
        assert income_of(st, rs, "A")[ALLOY] == 3 + 4    # A1 and A2; nothing from the ruins
        st, rep = run(st, rs, {})
        if kinds(rep, "ruin_ended"):
            ended_at = rep.turn
    assert ended_at == 6                                  # ruined in the economies of turns 1..6 (6 turns)
    assert "ruined_until" not in st.provinces["B2"].status and income_of(st, rs, "A")[ALLOY] == 3 + 4 + 4


def test_raid_returns_the_province_ravaged_troops_go_home_defenders_come_back_builds_survive(rs):
    st = board(rs, {"C2": {"militia": 4}, "B2": {"militia": 3}})
    st.pending_builds.append(PendingBuild("B", "B2", "t3:hq", 2))
    new, rep = run(st, rs, {"C": book(st, rs, "C", [mv("C2", "B2", 4), dispose("B2", "raid")])})
    b2 = new.provinces["B2"]
    assert b2.owner == "B" and b2.status["ravaged_until"] == 3 and "occupied" not in b2.status
    fate = kinds(rep, "disposition")[0]
    assert fate["loot"] == {ALLOY: 5, PROV: 5}            # 3 x 1.5 (ash), half up
    assert fate["owner_after"] == "B" and fate["status"] == "raided_returned"
    assert new.provinces["C2"].units.get(MIL, 0) > 0     # the raiders went back to their source
    assert new.provinces["B2"].units == {MIL: 1}         # the defender's survivor went back into its province
    assert [b.province for b in new.pending_builds] == ["B2"]
    assert not new.claims                                 # a raid creates no claim
    changes = rep.public["ownership_changes"]
    assert [(c["from"], c["to"]) for c in changes] == [("B", "C"), ("C", "B")]
    assert [e["kind"] for e in rep.public["conquest"]] == ["captured", "disposition"]
    assert "loot" not in rep.public["conquest"][1] and "mode" not in rep.public["conquest"][0]
    assert income_of(new, rs, "B")[ALLOY] == 3            # ravaged: no income from B2 this turn


def test_loot_once_ravaged_and_cooldown(rs):
    st = board(rs, {"C2": {"militia": 4}})
    loots = {}
    for turn in range(1, 7):
        raid = turn in (1, 2, 4, 6)          # two raids in a row, then one in the cooldown, then one after it
        orders = [mv("C2", "B2", st.provinces["C2"].units[MIL]), dispose("B2", "raid")] if raid else []
        st, rep = run(st, rs, {"C": book(st, rs, "C", orders)} if orders else {})
        if raid:
            fate = kinds(rep, "disposition")[0]
            loots[turn] = (bool(fate["loot"]), fate.get("loot_reason"))
    assert loots == {1: (True, None), 2: (False, "ravaged"), 4: (False, "cooldown"), 6: (True, None)}


def test_release_to_a_third_seat_is_its_occupation_checked_for_revolt_from_the_next_resolution(rs):
    st = board(rs, {"A2": {"militia": 3}})
    st, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2"), dispose("B2", "liberate", to="НикC")])})
    b2 = st.provinces["B2"]
    assert b2.owner == "C" and b2.status["occupied"] == {"by": "C", "from": "B", "since": 1} and not b2.units
    assert st.provinces["A2"].units == {MIL: 3}           # the liberator's troops went home
    assert not kinds(rep, "occupation_revolt")
    assert st.claims == [{"seat": "B", "province": "B2", "until_turn": 11}]
    st, rep = run(st, rs, {})
    assert st.provinces["B2"].owner is None and kinds(rep, "occupation_revolt")


def test_release_by_default_goes_back_to_the_previous_owner_full(rs):
    st = board(rs, {"A2": {"militia": 3}})
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2"), dispose("B2", "liberate")])})
    assert new.provinces["B2"].owner == "B" and not new.provinces["B2"].status
    assert not new.claims


def test_decisions_on_an_older_occupation(rs):
    st = board(rs)
    occupy(st, "B2", "A", "B", since=0, units=2)
    new, rep = run(st, rs, {"A": book(st, rs, "A", [dispose("B2", "subjugate")])})
    fate = kinds(rep, "disposition")[0]
    assert fate["source"] == "decision" and fate["cost"] == {PROV: 5} and not new.provinces["B2"].status
    # a release by decision: the troops go to the first own neighbour, full provinces first
    new, rep = run(st, rs, {"A": book(st, rs, "A", [dispose("B2", "liberate")])})
    assert new.provinces["B2"].owner == "B" and new.provinces["A2"].units == {MIL: 2}


def test_dispose_validation(rs):
    st = board(rs)
    occupy(st, "B2", "A", "B", since=0)

    def reason(item, seat="A", base=None, phase="actions"):
        return check_order(st, rs, seat, base or SeatOrders(), item, phase).reason

    assert reason(dispose("A2", "subjugate")) == "NOT_OCCUPIED"
    assert reason(dispose("M", "subjugate")) == "BAD_TARGET"
    assert reason(dispose("B1", "nope")) == "UNKNOWN_ID"
    assert reason(dispose("B1", "raid", to="C")) == "INVALID"
    assert reason(dispose("B1", "liberate", to="A")) == "NO_RECIPIENT"
    assert reason(dispose("B1", "liberate", to="Никто")) == "NO_RECIPIENT"
    assert reason(dispose("B2", "subjugate")) is None and reason(dispose("B1", "Подчинить")) is None
    first = book(st, rs, "A", [dispose("B1", "subjugate")])
    assert reason(dispose("B1", "raid"), base=first) == "INVALID"
    assert reason({"kind": "policy", "disposition": "raid"}, base=book(st, rs, "A", [
        {"kind": "policy", "disposition": "subjugate"}])) == "INVALID"
    assert reason({"kind": "policy", "disposition": "liberate", "to": "B"}) == "INVALID"
    st.players["A"].resources[PROV] = 14
    assert reason(dispose("B1", "subjugate")) == "NOT_ENOUGH_RESOURCES"
    # faction multipliers: ash subjugates at x1.3 (5 x 1 x 1.3 = 6.5, half up)
    assert seat_reserved(st, rs, "C", book(st, rs, "C", [dispose("B1", "subjugate")])) == {PROV: 20}
    assert seat_reserved(st, rs, "C", book(st, rs, "C", [dispose("A2", "subjugate")])) == {PROV: 7}
    # a nick becomes the seat id
    order = check_order(st, rs, "A", SeatOrders(), dispose("B1", "liberate", to="НикC"), "actions").order
    assert order.to == "C" and order.to_dict()["to"] == "C"


def test_dispose_and_policy_are_free_in_p2_and_their_cancel_returns_everything(rs):
    from dah.engine import amend_order, cancel_order
    st = board(rs)
    p2 = book(st, rs, "A", [dispose("B1", "subjugate"), {"kind": "policy", "disposition": "raid"}],
              phase="negotiation_2")
    assert p2.cp_spent == 0 and seat_reserved(st, rs, "A", p2) == {PROV: 15}
    amended = amend_order(st, rs, "A", p2, p2.orders[0].id, {"disposition": "exterminate"}, "negotiation_2")
    assert amended.ok and amended.cp_cost == 0 and seat_reserved(st, rs, "A", amended.book) == {}
    cancelled = cancel_order(st, rs, "A", p2, p2.orders[0].id, "negotiation_2")
    assert cancelled.ok and cancelled.cp_cost == 0 and cancelled.book.forfeit == {}
    assert check_order(st, rs, "A", SeatOrders(), dispose("B1", "raid"), "negotiation_1").reason == "PHASE_LOCKED"


# ---------------------------------------------------------------- returns, claims, pacts

def test_reclaim_own_land_is_free_full_and_ends_the_claim(rs):
    st = board(rs, {"B1": {"militia": 3}})
    occupy(st, "B2", "A", "B", since=0, units=0)
    st.claims = [{"seat": "B", "province": "B2", "until_turn": 5}]
    new, rep = run(st, rs, {"B": book(st, rs, "B", [mv("B1", "B2")])})
    assert new.provinces["B2"].owner == "B" and not new.provinces["B2"].status
    assert kinds(rep, "captured")[0]["mode"] == "reclaimed"
    assert not new.claims and kinds(rep, "claim_ended")[0]["reason"] == "returned"


def test_reclaim_origin_from_a_subjugated_holder_and_after_the_claim_expired(rs):
    st = board(rs, {"B1": {"militia": 3}})
    st.provinces["B2"].owner = "C"            # C subjugated it long ago: full, the claim is gone
    new, rep = run(st, rs, {"B": book(st, rs, "B", [mv("B1", "B2")])})
    assert new.provinces["B2"].owner == "B" and not new.provinces["B2"].status
    assert kinds(rep, "captured")[0]["mode"] == "reclaimed"
    assert new.claims == [{"seat": "C", "province": "B2", "until_turn": 11}]     # C lost a full province


def test_claim_multiplies_attack_by_its_bonus(rs):
    st = board(rs, {"B1": {"militia": 3}})
    st.provinces["B2"].owner = "A"
    st.provinces["B2"].units = {MIL: 3}
    orders = {"B": book(st, rs, "B", [mv("B1", "B2")])}
    _, plain = run(st, rs, orders)
    st.claims = [{"seat": "B", "province": "B2", "until_turn": 3}]
    _, claimed = run(st, rs, orders)
    s0 = next(e for e in plain.events if e["kind"] == "battle")["sides"][0]["strength"]
    s1 = next(e for e in claimed.events if e["kind"] == "battle")["sides"][0]["strength"]
    assert s1 == pytest.approx(s0 * 1.25, abs=2e-3)


def test_claims_expire_after_their_last_turn(rs):
    st = board(rs)
    st.claims = [{"seat": "B", "province": "M", "until_turn": 1}, {"seat": "C", "province": "M", "until_turn": 2}]
    new, rep = run(st, rs, {})
    assert new.claims == [{"seat": "C", "province": "M", "until_turn": 2}]
    assert kinds(rep, "claim_ended")[0]["reason"] == "expired"


def test_joint_capture_only_the_holders_directive_runs_and_the_partners_reserve_returns(rs):
    st = board(rs, {"A2": {"militia": 4}, "C2": {"militia": 4}, "B2": {"militia": 1}})
    op = dip.make_proposal(st, "A", "C", None, "negotiation_1")
    dip.apply_op(st, op)
    dip.apply_op(st, dip.make_response(st, "C", op["id"], "accept", "negotiation_1"))
    both = {"A": book(st, rs, "A", [mv("A2", "B2", 4), dispose("B2", "subjugate")]),
            "C": book(st, rs, "C", [mv("C2", "B2", 4), dispose("B2", "subjugate")])}
    new, rep = run(st, rs, both)
    holder = new.provinces["B2"].owner
    partner = "C" if holder == "A" else "A"
    fates = kinds(rep, "disposition")
    assert [f["seat"] for f in fates] == [holder] and not new.provinces["B2"].status.get("occupied")
    alone = dict(both)
    alone[partner] = book(st, rs, partner, [mv(f"{partner}2", "B2", 4)])
    ref, _ = run(st, rs, alone)
    assert new.players[partner].resources == ref.players[partner].resources


def test_a_seat_with_only_occupations_stays_alive_with_no_points(rs):
    st = board(rs)
    for pid in ("B1", "B2"):
        st.provinces[pid].owner = "A"
    occupy(st, "A2", "B", "A", since=0, units=2)
    new, rep = run(st, rs, {})
    assert new.players["B"].alive and scores(new, rs)["B"] == 0 and not kinds(rep, "eliminated")


def test_buildings_of_an_occupation_do_not_act(rs):
    st = board(rs)
    st.provinces["B1"].buildings = ["t3:hall", "t3:hq"]
    occupy(st, "B1", "A", "B", since=0, units=2)
    assert rs.stat("command_points", stat_key(st, rs, "A")) == 3
    assert province_defense_mul(st, rs, "B1") == 1.5
    assert income_of(st, rs, "A")[ALLOY] == 3 + 4
    del st.provinces["B1"].status["occupied"]
    assert rs.stat("command_points", stat_key(st, rs, "A")) == 4
    assert province_defense_mul(st, rs, "B1") == 1.5 * 1.5
    assert income_of(st, rs, "A")[ALLOY] == 3 + 4 + 3 + 2


def test_eliminated_seat_loses_its_claims(rs):
    st = board(rs, {"A1": {"militia": 9}, "A2": {"militia": 9}})
    st.provinces["B2"].owner = "A"
    st.claims = [{"seat": "B", "province": "B2", "until_turn": 9}]
    st.provinces["B1"].units = {}
    new, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A1", "B1", 9)])})
    assert not new.players["B"].alive and all(c["seat"] != "B" for c in new.claims)
    assert any(e["kind"] == "claim_ended" and e["reason"] == "eliminated" for e in rep.events)


# ---------------------------------------------------------------- switches and determinism

def test_conquest_off_keeps_the_slice_rule(rs):
    off = ruleset("tri_plain")
    st = board(off, {"A2": {"militia": 3}})
    assert not st.origins and all(p.home is None for p in st.players.values())
    new, rep = run(st, off, {"A": book(st, off, "A", [mv("A2", "B2")])})
    assert new.provinces["B2"].owner == "A" and not new.provinces["B2"].status and not new.claims
    assert "mode" not in kinds(rep, "captured")[0] and "conquest" not in rep.public
    for item in (dispose("B1", "subjugate"), {"kind": "policy", "disposition": "subjugate"}):
        assert check_order(st, off, "A", SeatOrders(), item, "actions").reason == "NOT_AVAILABLE"


def test_resolution_with_conquest_is_deterministic(rs):
    st = board(rs, {"A2": {"militia": 4}, "C2": {"militia": 4}, "B2": {"militia": 3}})
    books = {"A": book(st, rs, "A", [mv("A2", "B2", 4), dispose("B2", "raid")]),
             "C": book(st, rs, "C", [mv("C2", "B2", 4), {"kind": "policy", "disposition": "exterminate"}])}
    a, ra = run(st, rs, books, seed=9)
    b, rb = run(st, rs, books, seed=9)
    assert state_hash(a) == state_hash(b) and ra.to_dict() == rb.to_dict()


def test_taking_an_occupation_over_does_not_make_the_former_occupant_its_own(rs):
    st = board(rs, {"C2": {"militia": 4}})
    occupy(st, "B2", "A", "B", since=0, units=1)
    st, rep = run(st, rs, {"C": book(st, rs, "C", [mv("C2", "B2", 4)])})
    assert st.provinces["B2"].status["occupied"] == {"by": "C", "from": "B", "since": 1}
    assert not st.claims                                  # A held it occupied, not full: no claim
    st.provinces["A2"].units = {MIL: 6}
    st, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2", 6)])})
    assert st.provinces["B2"].owner == "A" and st.provinces["B2"].status["occupied"]["from"] == "B"
    assert kinds(rep, "captured")[0]["mode"] == "occupied"
    st.provinces["B1"].units = {MIL: 9}
    st, rep = run(st, rs, {"B": book(st, rs, "B", [mv("B1", "B2", 9)])})
    assert kinds(rep, "captured")[0]["mode"] == "reclaimed" and not st.provinces["B2"].status


# ---------------------------------------------------------------- review fixes

def test_claim_ends_when_the_holder_retakes_its_land_from_neutral(rs):
    """3.5: a claim ends on any return of the province to its holder, the neutral free capture included."""
    st = board(rs, {"A2": {"militia": 3}, "B1": {"militia": 4}})
    st, _ = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2")])})
    assert st.claims == [{"seat": "B", "province": "B2", "until_turn": 11}]
    st, _ = run(st, rs, {"A": book(st, rs, "A", [mv("B2", "A2")])})            # A leaves, the occupation revolts
    assert st.provinces["B2"].owner is None and st.claims
    st, rep = run(st, rs, {"B": book(st, rs, "B", [mv("B1", "B2", 2)])})
    assert st.provinces["B2"].owner == "B"
    assert next(e for e in kinds(rep, "captured") if e["province"] == "B2")["mode"] == "free"
    assert st.claims == [] and [e["reason"] for e in kinds(rep, "claim_ended")] == ["returned"]


def test_claim_ends_when_the_holder_takes_the_province_as_an_occupation(rs):
    """C lost A2 (A's origin) full to B, so C holds a claim; C retakes A2 from B: not its own land, so an
    Occupation - the claim ends all the same, and B (who held it full) gets one."""
    st = board(rs, {"C2": {"militia": 4}})
    st.provinces["A2"].owner = "B"
    st.claims = [{"seat": "C", "province": "A2", "until_turn": 9}]
    new, rep = run(st, rs, {"C": book(st, rs, "C", [mv("C2", "A2", 4)])})
    a2 = new.provinces["A2"]
    assert a2.owner == "C" and a2.status["occupied"]["from"] == "B"
    assert new.claims == [{"seat": "B", "province": "A2", "until_turn": 11}]
    assert [(e["seat"], e["reason"]) for e in kinds(rep, "claim_ended")] == [("C", "returned")]


def test_fate_and_capture_are_full_only_for_the_taker(rs):
    """2.7: the raid victim (owner_after) and the loser of a capture get the public form: no price, loot, source,
    disposition id or capture mode; the taker keeps the full form."""
    st = board(rs, {"C2": {"militia": 4}, "B2": {"militia": 1}})
    _, rep = run(st, rs, {"C": book(st, rs, "C", [mv("C2", "B2", 4), dispose("B2", "raid")])})
    for seat in ("A", "B"):
        fate = next(e for e in rep.by_seat[seat] if e["kind"] == "disposition")
        assert not {"cost", "loot", "source", "disposition", "loot_reason"} & set(fate), seat
        cap = next(e for e in rep.by_seat[seat] if e["kind"] == "captured" and e["to"] == "C")
        assert "mode" not in cap, seat
    mine = next(e for e in rep.by_seat["C"] if e["kind"] == "disposition")
    assert mine["source"] == "directive" and mine["loot"]
    assert next(e for e in rep.by_seat["C"] if e["kind"] == "captured")["mode"] == "directive"


def test_release_recipient_sees_the_public_fate(rs):
    st = board(rs, {"A2": {"militia": 3}})
    _, rep = run(st, rs, {"A": book(st, rs, "A", [mv("A2", "B2"), dispose("B2", "liberate", to="НикC")])})
    fate = next(e for e in rep.by_seat["C"] if e["kind"] == "disposition")
    assert fate["owner_after"] == "C" and "source" not in fate and "cost" not in fate


def test_dispose_error_texts_name_what_the_agent_can_type(rs):
    st = board(rs)
    occupy(st, "B2", "A", "B", since=0)
    bad_to = check_order(st, rs, "A", SeatOrders(), dispose("B1", "raid", to="C"), "actions")
    assert bad_to.reason == "INVALID" and "liberate" in bad_to.message and "(release)" not in bad_to.message
    self_to = check_order(st, rs, "A", SeatOrders(), dispose("B1", "liberate", to="НикA"), "actions")
    assert self_to.reason == "NO_RECIPIENT" and "НикA" in self_to.message and "'A'" not in self_to.message


def test_raid_on_land_of_an_eliminated_seat_goes_neutral(rs):
    """3.3 (CONCEPT: «Набег — прежнему владельцу или нейтралам»): a raid never falls back to the origin seat."""
    st = board(rs, {"C2": {"militia": 4}})
    occupy(st, "A2", "C", "B", since=0, units=1)         # A's origin, taken by C from B (who had subjugated it)
    st.players["B"].alive = False
    for pid in ("B1", "B2"):
        st.provinces[pid].owner = None
    new, rep = run(st, rs, {"C": book(st, rs, "C", [dispose("A2", "raid")])})
    assert new.provinces["A2"].owner is None and new.provinces["A2"].status["ravaged_until"] > 1
