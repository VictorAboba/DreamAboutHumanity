"""Core 0.8.0, espionage v2 part B (docs/SPY_V2.md 4.5, 4.6, 7, 14.3, 14.9, 14.10, 14.13, 14.14): the sabotage
stops a build (stall_max 1 a game, immunity the next turn, one stop for several strikes, the share of the order's
turn paid once), raises the revolt of an occupation held with one unit (2 militia stay as rebels without an owner),
both scored only by effect; the Ward (+C_loc at the recon step); what the sides, the others and the delayed
spectator see."""

from __future__ import annotations

import pytest

from dah import journal
from dah.engine import build_ruleset, check_order, new_game_state, resolve
from dah.engine import espionage as esp
from dah.engine import scoring as sc
from dah.engine.model import PendingBuild, SeatOrders, state_hash
from dah.engine.rules import rules_topic
from dah.engine.view import event_text

from test_core080_spy import AS, CORE071, MIL, SEATS3, _book, _shade, _start, sure  # noqa: F401

FOOD, ALLOY = "core:provisions", "core:alloy"
MON = "core:monument"                     # a great work paid in 2 shares (25 provisions + 10 alloy)


def _state(packs: list[str] | None = None, sid: str = "blitz_score"):
    rs = build_ruleset(packs, sid)
    st = new_game_state(rs, SEATS3, 4)
    for p in st.players.values():
        p.resources.update({FOOD: 300, ALLOY: 300})
    return rs, st


def _run(st, rs, books_d=None, books=None):
    st, rep = esp.recon(st, books_d or {}, rs, 4)
    new, report = resolve(st, books or {}, rs, 4)
    return st, rep, new, report


def _kinds(events, kind):
    return [e for e in events if e["kind"] == kind]


def _monument(st, turns_left=1):
    b = PendingBuild("B", "P10", MON, turns_left, {FOOD: 25, ALLOY: 10})
    st.pending_builds.append(b)
    return b


# ---------------------------------------------------------------- 14.9 the stop of a build

def test_the_pack_gives_the_stop_and_the_revolt():
    rs = build_ruleset(None, "blitz_score")
    assert rs.missions["core:sabotage"].params == {
        "defense_mul": 0.85, "deny_fortify": True, "income_mul": 0.0, "revolt_garrison": {MIL: 2}, "stall_build": True,
        "stall_max": 1, "unrest_units": 1}
    old = build_ruleset([str(CORE071)], "blitz_score")
    assert set(old.missions["core:sabotage"].params) == {"defense_mul", "deny_fortify", "income_mul"}


def test_a_strike_stops_a_build_whose_share_is_covered(sure):
    rs, st = _state()
    _monument(st)
    _start(st, "A", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs)
    mark = esp.cq.strike_marks(st.provinces["P10"], st.turn)[0]
    assert (mark["stall_build"], mark["stall_max"], mark["unrest_units"], mark["revolt_garrison"]) == (True, 1, 1,
                                                                                                         {MIL: 2})
    b = next(b for b in new.pending_builds if b.building == MON)
    assert (b.turns_left, b.sabotage_stalls, b.sabotage_immune_turn, b.stalled) == (1, 1, st.turn + 1, False)
    stop = _kinds(report.events, "build_sabotaged")
    assert [(e["seat"], e["province"], e["building"], e["share"], e["shares"], e["turns_left"], e["seats"])
            for e in stop] == [("B", "P10", MON, 2, 2, 1, ["B"])]
    assert not _kinds(report.events, "build_paid") and not _kinds(report.events, "build_stalled")
    assert not _kinds(report.events, "build_done")
    text = event_text(stop[0], rs, new, "B")
    assert text == "стройка «Монумент Предела» в P10 стоит этот ход: саботаж; исполнитель не установлен"
    eff = _kinds(report.events, "sabotage_effect")[0]
    assert (eff["effects"], eff["building"], eff["intel_point"]) == (["stall"], MON, True)
    assert sc.intel(new, rs, "A") == 1
    assert _kinds(report.by_seat["B"], "build_sabotaged") and not _kinds(report.by_seat["A"], "build_sabotaged")
    assert not _kinds(report.by_seat["C"], "build_sabotaged") and not _kinds(report.by_seat["B"], "sabotage_effect")


def test_without_the_strike_the_share_is_paid_and_the_work_is_done(sure):
    rs, st = _state()
    _monument(st)
    st, rep, new, report = _run(st, rs)
    assert _kinds(report.events, "build_paid") and _kinds(report.events, "build_done")
    assert not [b for b in new.pending_builds if b.building == MON]


def test_a_share_the_stock_misses_is_the_usual_stall_without_an_effect(sure):
    rs, st = _state()
    _monument(st)
    st.players["B"].resources.update({FOOD: 0, ALLOY: 0})
    _start(st, "A", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs)
    assert not _kinds(report.events, "build_sabotaged") and _kinds(report.events, "build_stalled")
    b = next(b for b in new.pending_builds if b.building == MON)
    assert (b.sabotage_stalls, b.sabotage_immune_turn, b.stalled) == (0, None, True)
    eff = _kinds(report.events, "sabotage_effect")[0]
    assert eff["effects"] == [] and eff["intel_point"] is False and sc.intel(new, rs, "A") == 0


def test_stall_max_and_the_immunity_of_the_next_turn(sure):
    for field, value in (("sabotage_stalls", 1), ("sabotage_immune_turn", 1)):
        rs, st = _state()
        setattr(_monument(st), field, value)
        _start(st, "A", "core:sabotage", "P10")
        st, rep, new, report = _run(st, rs)
        assert not _kinds(report.events, "build_sabotaged"), field
        assert _kinds(report.events, "build_done"), field                     # the build went on
        assert _kinds(report.events, "sabotage"), field                       # the strike itself stands
        eff = _kinds(report.events, "sabotage_effect")[0]
        assert eff["effects"] == [] and sc.intel(new, rs, "A") == 0, field


def test_two_strikes_in_one_turn_give_one_stop_and_one_point(sure):
    rs, st = _state()
    _monument(st)
    _start(st, "A", "core:sabotage", "P10")
    _start(st, "C", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs)
    assert len(_kinds(report.events, "build_sabotaged")) == 1
    b = next(b for b in new.pending_builds if b.building == MON)
    assert b.sabotage_stalls == 1 and b.turns_left == 1
    eff = {e["seat"]: e for e in _kinds(report.events, "sabotage_effect")}
    assert eff["A"]["effects"] == eff["C"]["effects"] == ["stall"]
    assert (eff["A"]["intel_point"], eff["C"]["intel_point"], eff["C"]["reason"]) == (True, False, "taken")


def test_a_build_stopped_on_its_order_turn_pays_its_first_share_once(sure):
    rs, st = _state()
    book = _book(st, rs, "B", [{"kind": "build", "province": "P10", "building": "monument"}])
    _start(st, "A", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs, {"B": book}, {"B": book})
    b = next(b for b in new.pending_builds if b.building == MON)
    assert (b.turns_left, b.sabotage_stalls) == (2, 1)
    assert [e["share"] for e in _kinds(report.events, "build_sabotaged")] == [1]
    assert _kinds(report.events, "sabotage_effect")[0]["intel_point"] is True
    # the next turn: no strike, the first share is not taken again (turns_left 2 -> 1)
    new.phase = "actions"
    st2, rep2 = esp.recon(new, {}, rs, 4)
    after, report2 = resolve(st2, {}, rs, 4)
    assert not _kinds(report2.events, "build_paid") and not _kinds(report2.events, "build_sabotaged")
    assert next(b for b in after.pending_builds if b.building == MON).turns_left == 1


def test_an_older_sabotage_stops_no_build(sure):
    rs, st = _state([str(CORE071)])
    _monument(st)
    _start(st, "A", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs)
    assert not _kinds(report.events, "build_sabotaged") and _kinds(report.events, "build_done")


def test_the_new_build_fields_stay_out_of_the_state_hash_while_empty():
    rs, st = _state()
    b = _monument(st)
    h = state_hash(st)
    b.sabotage_stalls, b.sabotage_immune_turn = 1, 3
    assert state_hash(st) != h
    b.sabotage_stalls, b.sabotage_immune_turn = 0, None
    assert state_hash(st) == h


# ---------------------------------------------------------------- 14.10 the revolt

def _occupy(st, pid="P10", by="A", units=None, since=None):
    prov = st.provinces[pid]
    prov.owner, prov.units = by, dict(units or {AS: 1})
    prov.status["occupied"] = {"by": by, "from": "B", "since": st.turn - 1 if since is None else since}


def test_an_occupation_with_one_unit_revolts_and_keeps_rebels(sure):
    rs, st = _state()
    st.turn = 3
    _occupy(st)
    _start(st, "C", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs)
    prov = new.provinces["P10"]
    assert (prov.owner, prov.units, "occupied" in prov.status) == (None, {MIL: 2}, False)
    rev = _kinds(report.events, "occupation_revolt")
    assert [(e["seat"], e["reason"], e["lost"], e["rebels"]) for e in rev] == [("A", "sabotage", {AS: 1}, {MIL: 2})]
    side = _kinds(report.by_seat["A"], "occupation_revolt")[0]
    assert side["reason"] == "sabotage"
    assert "повстанцы" in event_text(side, rs, new, "A")
    for seat in ("B", "C"):                                    # the others: no cause, no rebels, no lost units
        seen = _kinds(report.by_seat[seat], "occupation_revolt")[0]
        assert not {"reason", "rebels", "lost"} & set(seen), seat
        assert event_text(seen, rs, new, seat) == "бунт в P10: НикA её потерял, провинция нейтральна"
    pub = [e for e in report.public["conquest"] if e["kind"] == "occupation_revolt"]
    assert pub and not {"reason", "rebels", "lost"} & set(pub[0])
    eff = _kinds(report.events, "sabotage_effect")[0]
    assert (eff["seat"], eff["victim"], eff["effects"], eff["intel_point"]) == ("C", "A", ["revolt"], True)
    assert not _kinds(report.events, "war_points") and not _kinds(report.events, "war_credit")
    assert {"province": "P10", "from": "A", "to": None, "status": "revolt"} in report.public["ownership_changes"]


def test_the_old_owner_has_to_fight_the_rebels(sure):
    rs, st = _state()
    st.turn = 3
    _occupy(st)
    _start(st, "C", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs)
    new.provinces["P09"].units = {AS: 1}
    book = _book(new, rs, "B", [{"kind": "move", "from": "P09", "to": "P10", "unit": "assault", "count": 1}])
    st2, rep2 = esp.recon(new, {"B": book}, rs, 4)
    after, report2 = resolve(st2, {"B": book}, rs, 4)
    fights = [e for e in report2.events if e["kind"] == "battle" and e["province"] == "P10"]
    assert fights and any(s["owner"] is None for s in fights[0]["sides"])
    assert not [e for e in report2.events if e["kind"] == "war_points" and e.get("points")]


def test_no_revolt_with_two_units_in_a_fresh_occupation_or_a_full_province(sure):
    cases = {"two units": dict(units={AS: 2}), "taken this turn": dict(since=3)}
    for name, kw in cases.items():
        rs, st = _state()
        st.turn = 3
        _occupy(st, **kw)
        _start(st, "C", "core:sabotage", "P10")
        st, rep, new, report = _run(st, rs)
        assert not _kinds(report.events, "occupation_revolt"), name
        assert new.provinces["P10"].owner == "A", name
    rs, st = _state()
    st.provinces["P10"].units = {MIL: 1}
    _start(st, "A", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs)
    assert not _kinds(report.events, "occupation_revolt") and new.provinces["P10"].owner == "B"


def test_a_revolt_without_a_garrison_is_the_old_empty_one(sure):
    rs, st = _state()
    st.turn = 3
    _occupy(st, units={AS: 0})
    st, rep, new, report = _run(st, rs)
    rev = _kinds(report.events, "occupation_revolt")
    assert [(e["reason"], "rebels" in e) for e in rev] == [("no_garrison", False)]
    assert (new.provinces["P10"].owner, new.provinces["P10"].units) == (None, {})
    assert "reason" not in _kinds(report.by_seat["B"], "occupation_revolt")[0]          # 0.8.0: no cause for others
    # a ruleset without unrest_units keeps the public reason of 0.7.x
    rs, st = _state([str(CORE071)])
    st.turn = 3
    _occupy(st, units={AS: 0})
    st, rep, new, report = _run(st, rs)
    assert _kinds(report.by_seat["B"], "occupation_revolt")[0]["reason"] == "no_garrison"
    assert [e["reason"] for e in report.public["conquest"] if e["kind"] == "occupation_revolt"] == ["no_garrison"]


def test_an_older_sabotage_raises_no_revolt(sure):
    rs, st = _state([str(CORE071)])
    st.turn = 3
    _occupy(st)
    _start(st, "C", "core:sabotage", "P10")
    st, rep, new, report = _run(st, rs)
    assert not _kinds(report.events, "occupation_revolt") and new.provinces["P10"].owner == "A"


# ---------------------------------------------------------------- 14.13 visibility

def test_the_delayed_spectator_and_other_seats_never_see_the_private_kinds():
    events = [{"type": "build_sabotaged", "seat": "B", "province": "P10", "building": MON, "turn": 3},
              {"type": "sabotage_effect", "seat": "A", "province": "P10", "effects": ["stall"], "turn": 3},
              {"type": "ward_set", "seat": "B", "province": "P10", "bonus": 2, "turn": 3},
              {"type": "occupation_revolt", "seat": "A", "province": "P10", "reason": "sabotage", "lost": {AS: 1},
               "rebels": {MIL: 2}, "cause_private": True, "turn": 3}]
    spec = journal.view_events(events, spectator=True)
    assert [e["type"] for e in spec] == ["occupation_revolt"]
    assert not {"reason", "lost", "rebels", "cause_private"} & set(spec[0])
    b = journal.view_events(events, seat="B")
    assert [e["type"] for e in b] == ["build_sabotaged", "ward_set", "occupation_revolt"]
    assert "reason" not in b[-1]
    a = journal.view_events(events, seat="A")
    assert [e["type"] for e in a] == ["sabotage_effect", "occupation_revolt"]
    assert a[-1]["reason"] == "sabotage" and "cause_private" not in a[-1]
    old = [{"type": "occupation_revolt", "seat": "A", "province": "P10", "reason": "no_garrison", "turn": 3}]
    assert journal.view_events(old, spectator=True)[0]["reason"] == "no_garrison"       # 0.7.x journals unchanged


def test_the_game_journals_the_revolt_cause_as_private(tmp_path, sure):
    from dah.game import Game, make_config
    from dah.store import init_game_dir
    from test_core080_spy import Clock
    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "agent"} for s in "ABC"]
    gdir, _ = init_game_dir(tmp_path, make_config(game_id="j", scenario="core:blitz_score", seats=seats, seed=4))
    game = Game.create(gdir, Clock())
    assert esp.unrest_in(game.ruleset)
    ev = {"kind": "occupation_revolt", "seats": ["A"], "province": "P10", "seat": "A", "reason": "sabotage"}
    game._journal_conquest(ev, 3)
    last = [e for e in game.journal.events() if e["type"] == "occupation_revolt"][-1]
    assert last["cause_private"] is True and last["reason"] == "sabotage"


# ---------------------------------------------------------------- 14.14 the Ward

def test_the_ward_order_its_price_and_its_limits():
    rs, st = _state()
    ok = lambda item, phase="actions", b=None: check_order(st, rs, "B", b or SeatOrders(), item, phase)  # noqa: E731
    ward = {"kind": "ward", "province": "P10"}
    assert ok(ward).ok and esp.order_cost(rs, None, ok(ward).order) == {ALLOY: 5}
    book = _book(st, rs, "B", [ward])
    assert ok(ward, b=book).reason == "INVALID"                                 # one per province
    assert ok({"kind": "ward", "province": "P01"}).reason == "NOT_OWNER"
    assert ok(ward, "negotiation_2").reason == "PHASE_LOCKED"
    _occupy(st, pid="P12", by="B")
    assert ok({"kind": "ward", "province": "P12"}).reason == "OCCUPIED"
    st.players["B"].techs = ["core:watch_drill"]
    key = esp.stat_key(st, rs, "B")
    assert esp.ward_cost(rs, key) == {ALLOY: 3} and esp.ward_bonus(rs, key) == 3
    plain = build_ruleset(None, "blitz_test")
    st2 = new_game_state(plain, SEATS3, 4)
    assert check_order(st2, plain, "A", SeatOrders(), {"kind": "ward", "province": "P01"},
                       "actions").reason == "NOT_AVAILABLE"


def test_the_ward_raises_c_loc_at_the_recon_step_and_is_paid_there(sure):
    rs, st = _state()
    _start(st, "A", "core:observe", "P10")
    _shade(st, "A")
    _start(st, "A", "core:ledger", "B", "s2")                     # checked at B's capital P09: no Ward there
    c10, c09 = esp.local_mirrors(st, rs, "P10"), esp.local_mirrors(st, rs, "P09")
    book = _book(st, rs, "B", [{"kind": "ward", "province": "P10"}])
    alloy = st.players["B"].resources[ALLOY]
    st2, rep = esp.recon(st, {"B": book}, rs, 4)
    assert rep["wards"] == [{"seat": "B", "province": "P10", "bonus": 2, "cost": {ALLOY: 5}}]
    assert st2.players["B"].resources[ALLOY] == alloy - 5
    rounds = {r["mission"]: r for r in rep["rounds"]}
    assert (rounds["core:observe"]["C_loc"], rounds["core:observe"]["ward"]) == (c10 + 2, 2)
    assert rounds["core:ledger"]["C_loc"] == c09 and "ward" not in rounds["core:ledger"]
    from dah.engine.orders import seat_reserved
    assert seat_reserved(st2, rs, "B", book).get(ALLOY, 0) == 0               # recon paid it: nothing held after
    new, report = resolve(st2, {"B": book}, rs, 4)
    without, _ = resolve(st2, {}, rs, 4)
    assert not _kinds(report.events, "order_rejected")
    assert new.players["B"].resources == without.players["B"].resources       # the resolution does not pay it again


def test_a_ward_at_a_capital_covers_the_missions_against_the_seat(sure):
    rs, st = _state()
    _start(st, "A", "core:ledger", "B")
    c09 = esp.local_mirrors(st, rs, "P09")
    book = _book(st, rs, "B", [{"kind": "ward", "province": "P09"}])
    st2, rep = esp.recon(st, {"B": book}, rs, 4)
    assert rep["rounds"][0]["C_loc"] == c09 + 2


def test_a_recon_without_wards_keeps_its_report_shape(sure):
    rs, st = _state()
    _start(st, "A", "core:observe", "P10")
    st2, rep = esp.recon(st, {}, rs, 4)
    assert "wards" not in rep and "ward" not in rep["rounds"][0]


def test_the_topic_tells_the_stop_the_revolt_and_the_ward():
    for sid in ("quad4", "slice3", "blitz_score"):
        topic = rules_topic(build_ruleset(None, sid), "spying")
        for words in ("стройка там стоит ход (1 раз на стройку)", "оккупация с ≤1 юн. бунтует (повстанцы",
                      "Застава этого хода (ward{province} в Д, 5 Сплав", "hire_shade, ward и новая ложная запись"):
            assert words in topic, (sid, words)
        assert len(topic) <= 4087 + 500, sid
    old = rules_topic(build_ruleset([str(CORE071)], "quad4"), "spying")
    assert "Застава" not in old and "бунтует" not in old and "Голод Теней не трогает" in old


@pytest.mark.parametrize("sid", ("blitz_score", "quad4"))
def test_the_bridge_lists_the_ward(sid):
    from dah.bridge.tools import _ORDER_ITEM
    assert "ward" in _ORDER_ITEM["properties"]["kind"]["enum"]
