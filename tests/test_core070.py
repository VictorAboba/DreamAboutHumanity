"""Core 0.7.0 (decision 27, measurement m8): the frozen core 0.6.0 for older games, great works paid in parts, points for
successful spy missions (`intel`) and the war score as a scale per province."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dah.engine import (build_ruleset, describe, issue_order_id, new_game_state, resolve, rules_text, rules_topic,
                        submit_orders, view_state)
from dah.engine import espionage as esp
from dah.engine import scoring as sc
from dah.engine.content import DEFAULT_PACKS_DIR, LEGACY_PACKS_DIR, split_cost
from dah.engine.model import GameState, SeatOrders, state_hash
from dah.engine.view import render_state_text
from dah.game import Game, make_config, pick_ruleset, replay
from dah.journal import read_events, view_events
from dah.report import build_report, render_report
from dah.store import GameDir, init_game_dir
from test_review_stage2 import FakeClock, _play_out

CORE = DEFAULT_PACKS_DIR / "core"
CORE060 = LEGACY_PACKS_DIR / "core-0.6.0"
CORE071 = LEGACY_PACKS_DIR / "core-0.7.1"     # the intel rule of 0.7.0 (every success scores) lives on there
# ruleset hashes of the frozen core 0.6.0, as core 0.6.0 itself built them at b7c6d6c (measurement m8: quad4)
CORE060_HASHES = {"slice3": "96e7815e10f4", "slice2": "3e6b42c166e3", "quad4": "4b1cae4580d8",
                  "blitz_test": "d6feaf2ec597", "blitz_spy": "ffe962f8d312", "blitz_score": "9db225137694",
                  "blitz_finds": "acee1f8fdb37"}
SEATS3 = [{"seat": s, "nick": s} for s in "ABC"]
ALLOY, FOOD = "core:alloy", "core:provisions"


def _seats(n: int) -> list[dict]:
    return [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(n)]


def _orders(st: GameState, rs, seat: str, items: list[dict]) -> SeatOrders:
    st.phase = "actions"
    res = submit_orders(st, rs, seat, SeatOrders(), items, "actions", id_factory=lambda: issue_order_id(st, seat))
    assert all(c.ok for c in res.results), [c.to_dict() for c in res.results]
    return res.book


def _kinds(rep) -> list[str]:
    return [e["kind"] for e in rep.events]


# ---------------------------------------------------------------- 6. freeze

def test_core_060_is_frozen_exactly_with_its_hashes():
    assert "version: 0.8.2" in (CORE / "pack.yaml").read_text(encoding="utf-8")
    head = (CORE060 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.6.0" in head and "Frozen copy of core 0.6.0" in head and "Do not edit" in head
    for sid, prefix in CORE060_HASHES.items():
        old = build_ruleset([str(CORE060)], sid)
        assert old.hash.startswith(prefix), (sid, old.hash[:12])
        assert old.packs == {"core": "0.6.0"}
        assert all(b.payment == "full" for b in old.buildings.values())
        assert not old.weight("intel") and not sc.war_by_province(old)


@pytest.mark.parametrize("sid", ["blitz_test", "blitz_spy", "slice3"])
def test_with_the_new_rules_off_only_the_version_changes(sid):
    """Espionage, conquest, finds and the scoring off: 0.7.0 builds exactly the rules of 0.6.0 but for the army fog
    switch the played scenarios set (fog.army_presence)."""
    off = {"espionage": False, "conquest": False, "discoveries": False, "trade": False}
    a = build_ruleset([str(CORE060)], sid, off, scoring={"territory": 1}).to_dict()
    b = build_ruleset(None, sid, off, scoring={"territory": 1}).to_dict()
    assert (a.pop("packs"), b.pop("packs")) == ({"core": "0.6.0"}, {"core": "0.8.2"})
    for d in (b["stats"], *b["faction_stats"].values()):
        d.pop("fog.army_presence", None)
    assert a == b


def test_a_core_060_game_replays_and_loads_with_the_frozen_pack(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="old", scenario="core:blitz_score", seats=_seats(3), seed=11, turn_limit=2,
                         pack_dirs=[str(CORE060)])
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    _play_out(game, clock)
    old_hash = game.ruleset.hash
    assert old_hash.startswith(CORE060_HASHES["blitz_score"])
    raw = json.loads(gdir.config_path.read_text(encoding="utf-8"))
    raw["pack_dirs"] = None
    gdir.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    rep = replay(GameDir(gdir.path))
    assert rep.ok and [t.turn for t in rep.turns] == [1, 2]
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == "core-0.6.0"
    pick = pick_ruleset(raw, old_hash)
    assert pick.matches and pick.ruleset.packs == {"core": "0.6.0"}
    assert Game.load(GameDir(gdir.path), clock).ruleset.hash == old_hash


# ---------------------------------------------------------------- 1. great works paid in parts

def test_split_cost_puts_the_remainder_on_the_first_share():
    assert [split_cost({FOOD: 25, ALLOY: 10}, 2, i) for i in range(2)] == [{FOOD: 13, ALLOY: 5}, {FOOD: 12, ALLOY: 5}]
    assert [split_cost({FOOD: 100, ALLOY: 60}, 4, i) for i in range(4)] == [{FOOD: 25, ALLOY: 15}] * 4
    assert split_cost({FOOD: 3, ALLOY: 1}, 4, 1) == {}            # nothing in this share: left out
    assert split_cost({FOOD: 7}, 1, 0) == {FOOD: 7}


def test_both_great_works_are_paid_in_parts_with_price_time_and_points_unchanged():
    rs = build_ruleset(None, "quad4")
    mon, spire = rs.buildings["core:monument"], rs.buildings["core:dream_spire"]
    assert (mon.payment, spire.payment) == ("parts", "parts")
    assert rs.building_cost("core:monument") == {FOOD: 25, ALLOY: 10} and mon.build_turns == 2
    assert rs.building_cost("core:dream_spire") == {FOOD: 100, ALLOY: 60} and spire.build_turns == 4
    assert (rs.stat("building.core:monument.score"), rs.stat("building.core:dream_spire.score")) == (3, 7)
    assert rs.building_order_cost("core:dream_spire") == {FOOD: 25, ALLOY: 15}
    assert all(b.payment == "full" for bid, b in rs.buildings.items() if not b.score)
    d = describe(rs, "building", "core:dream_spire")
    assert d["payment"] == "parts" and len(d["shares"]) == 4 and "оплата долями" in d["text"]
    assert "Долями по ходам" in rules_text(rs) and "Долями: цена делится" in rules_topic(rs, "buildings")


def _spire_game():
    rs = build_ruleset(None, "blitz_score")
    st = new_game_state(rs, SEATS3, 4)
    st.players["A"].resources.update({FOOD: 60, ALLOY: 30})
    return rs, st


def test_the_order_pays_the_first_share_and_validation_checks_only_it():
    rs, st = _spire_game()
    # 25+15 now, though the whole price (100+60) is far beyond the stock
    book = _orders(st, rs, "A", [{"kind": "build", "province": "P02", "building": "dream_spire"}])
    before = dict(st.players["A"].resources)
    new, rep = resolve(st, {"A": book}, rs, 3)
    eco = next(e for e in rep.events if e["kind"] == "economy" and "A" in e["seats"])
    paid = {r: before[r] + eco["income"][r] - eco["upkeep"][r] - new.players["A"].resources[r] for r in (FOOD, ALLOY)}
    assert paid == {FOOD: 25, ALLOY: 15}
    b = new.pending_builds[0]
    assert (b.building, b.turns_left, b.cost, b.stalled) == ("core:dream_spire", 3, {FOOD: 100, ALLOY: 60}, False)
    started = next(e for e in rep.events if e["kind"] == "build_started")
    assert started["shares"] == 4 and started["paid"] == {FOOD: 25, ALLOY: 15}
    mine = next(p for p in view_state(new, rs, "A", None)["provinces"] if p["id"] == "P02")
    assert mine["building_now"] == [{"building": "core:dream_spire", "turns_left": 3, "share": "2/4",
                                     "next_share": {FOOD: 25, ALLOY: 15}}]
    other = next(p for p in view_state(new, rs, "B", None)["provinces"] if p["id"] == "P02")
    assert "next_share" not in json.dumps(other)          # the shares are the owner's


def test_an_unpaid_share_stalls_the_work_which_resumes_when_paid():
    rs, st = _spire_game()
    book = _orders(st, rs, "A", [{"kind": "build", "province": "P02", "building": "dream_spire"}])
    st, _ = resolve(st, {"A": book}, rs, 3)
    st.players["A"].resources.update({FOOD: 0, ALLOY: 0})       # income alone does not cover 25+15
    hash_before = None
    st, rep = resolve(st, {}, rs, 3)
    stall = next(e for e in rep.events if e["kind"] == "build_stalled")
    assert stall["seats"] == ["A"] and stall["share"] == 2 and stall["due"] == {FOOD: 25, ALLOY: 15}
    assert set(stall["missing"]) == {FOOD, ALLOY} and "build_paid" not in _kinds(rep)
    b = st.pending_builds[0]
    assert b.turns_left == 3 and b.stalled                     # no progress, nothing taken
    eco = next(e for e in rep.events if e["kind"] == "economy" and "A" in e["seats"])
    assert st.players["A"].resources[ALLOY] == eco["income"][ALLOY] - eco["upkeep"][ALLOY]
    text = render_state_text(st, rs, "A")
    assert "СТОИТ: нет доли 2/4" in text
    assert "stalled" in json.dumps(view_state(st, rs, "A", None)["provinces"])
    # paid at the next build step: progress goes on
    st.players["A"].resources.update({FOOD: 200, ALLOY: 200})
    for share in (2, 3, 4):
        st, rep = resolve(st, {}, rs, 3)
        paid = next(e for e in rep.events if e["kind"] == "build_paid")
        assert paid["share"] == share and paid["paid"] == {FOOD: 25, ALLOY: 15} and paid["seat"] == "A"
        hash_before = state_hash(st)
    assert "work_completed" in _kinds(rep) and not st.pending_builds
    assert "core:dream_spire" in st.provinces["P02"].buildings and hash_before


def test_a_captured_work_in_progress_loses_the_paid_shares():
    rs, st = _spire_game()
    book = _orders(st, rs, "A", [{"kind": "build", "province": "P02", "building": "dream_spire"}])
    st, _ = resolve(st, {"A": book}, rs, 3)
    st.provinces["P07"].units = {"core:assault": 8}               # C next door takes P02 (empty)
    book = _orders(st, rs, "C", [{"kind": "move", "from": "P07", "to": "P02", "units": {"assault": 8}}])
    st, rep = resolve(st, {"C": book}, rs, 3)
    assert st.provinces["P02"].owner == "C" and not st.pending_builds


def test_full_payment_buildings_hash_and_pay_as_before():
    rs, st = _spire_game()
    book = _orders(st, rs, "A", [{"kind": "build", "province": "P02", "building": "mine"}])
    new, rep = resolve(st, {"A": book}, rs, 3)
    assert all(not b.cost and not b.stalled for b in new.pending_builds)
    assert "cost" not in json.dumps(new.to_dict()["pending_builds"]) or all(
        not b.get("cost") for b in new.to_dict()["pending_builds"])
    started = next(e for e in rep.events if e["kind"] == "build_started")
    assert "shares" not in started


# ---------------------------------------------------------------- 2. intel

def _rounds(*items):
    return [{"seat": s, "mission": m, "target": t, "victim": v, "success": ok} for s, m, t, v, ok in items]


def test_intel_counts_successes_against_living_rivals_once_per_target_and_kind_a_turn_up_to_the_cap():
    rs = build_ruleset([str(CORE071)], "quad4")      # core 0.8.0: by the mission's kind (test_core080_spy.py)
    assert rs.weight("intel") == 1 and rs.stat("score.intel.per_success") == 1 and rs.stat("score.intel.cap") == 3
    st = new_game_state(rs, _seats(3), 3)
    got = sc.credit_intel(st, rs, _rounds(("A", "core:ledger", "B", "B", True),
                                          ("A", "core:ledger", "B", "B", True),      # the same target and kind
                                          ("A", "core:ledger", "C", "C", False),     # failed
                                          ("A", "core:ledger", "A", "A", True),      # itself
                                          ("A", "core:observe", "P05", None, True)), rs.missions)   # neutral land
    assert [(g["seat"], g["target"], g["points"]) for g in got] == [("A", "B", 1)]
    st.players["C"].alive = False
    assert sc.credit_intel(st, rs, _rounds(("A", "core:secret", "C", "C", True)), rs.missions) == []
    sc.credit_intel(st, rs, _rounds(("A", "core:secret", "B", "B", True)), rs.missions)
    sc.credit_intel(st, rs, _rounds(("A", "core:read_orders", "B", "B", True)), rs.missions)
    assert sc.intel(st, rs, "A") == 3 and sc.intel_left(st, rs, "A") == 0
    assert sc.credit_intel(st, rs, _rounds(("A", "core:ledger", "B", "B", True)), rs.missions) == []   # capped
    assert sc.score_parts(st, rs)["A"]["intel"] == 3 and "intel" in sc.score_parts(st, rs)["B"]
    # others see a lower bound without it ("?" like science and war)
    assert sc.score_known(st, rs, "B", "A")["parts"].get("intel") in (None, 0)


def test_intel_is_off_with_its_weight_or_without_espionage():
    for rs in (build_ruleset(None, "quad4", scoring={"territory": 1, "war": 1}),
               build_ruleset(None, "blitz_test"), build_ruleset([str(CORE060)], "quad4")):
        st = new_game_state(rs, _seats(3), 3)
        assert not sc.on(rs, "intel")
        assert sc.credit_intel(st, rs, _rounds(("A", "core:ledger", "B", "B", True)), rs.missions) == []
        assert "intel" not in rs.to_dict()["scenario"].get("scoring", {})


def test_the_estimate_and_the_rules_name_the_payoff():
    rs = build_ruleset([str(CORE071)], "quad4")
    st = new_game_state(rs, _seats(3), 3)
    est = esp.estimate_mission(st, rs, "A", "ledger", "B", 1)
    assert est["intel_points"] == 1 and "+1 очк. разведки" in est["text"]
    st.players["B"].tally["intel"] = 3
    spent = esp.estimate_mission(st, rs, "B", "ledger", "A", 1)
    assert spent["intel_points"] == 0 and "очков разведки не даст (предел" in spent["text"]
    assert "Успех задания: +1 очко (≤3)" in rules_text(rs)
    assert "- Разведка: +1 за успешное задание Тени" in rules_topic(rs, "victory")
    assert "разведка" in next(line for line in rules_text(rs).splitlines() if line.startswith("ПОБЕДА"))
    you = view_state(st, rs, "A", None)["you"]
    assert you["intel"] == {"points": 0, "left": 3}


# ---------------------------------------------------------------- 7. war per province

def _capture_state(packs: list[str] | None = None):
    """blitz_score: A next to C's empty agri P07 with a strong army in its forge P02 (`packs`: e.g. the frozen 0.7.0)."""
    rs = build_ruleset(packs, "blitz_score")
    st = new_game_state(rs, SEATS3, 4)
    st.provinces["P02"].units = {"core:assault": 8}
    return rs, st


def _war_events(rep) -> list[dict]:
    return [e for e in rep.events if e["kind"] == "war_points"]


def test_a_province_taken_from_a_rival_scores_one_and_holding_it_more_up_to_the_cap():
    rs, st = _capture_state()
    assert sc.war_by_province(rs) and rs.stat("score.war.province_cap") == 3 and rs.stat("score.war.hold_turns") == 2
    book = _orders(st, rs, "A", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 8}}])
    st, rep = resolve(st, {"A": book}, rs, 3)
    assert st.provinces["P07"].owner == "A"
    ev = _war_events(rep)
    assert [(e["seat"], e["province"], e["reason"], e["total"]) for e in ev] == [("A", "P07", "capture", 1)]
    assert ev[0]["seats"] == ["A"] and "war_credit" not in _kinds(rep)        # no kill credit any more
    assert sc.war(st, rs, "A") == 1
    totals = []
    for _ in range(6):
        st, rep = resolve(st, {}, rs, 3)
        totals.append(sc.war(st, rs, "A"))
    assert totals == [1, 2, 2, 3, 3, 3]                            # +1 at 2 and 4 turns held, then the cap
    view = view_state(st, rs, "A", None)["you"]["war"]
    assert view["provinces"] == {"P07": 3} and view["per_province"] == 3 and view["cap_left"] == 5


def test_ping_pong_recaptures_cannot_farm_beyond_the_cap_and_points_stay_after_a_loss():
    # core 0.7.0's rule (frozen copy): a retake pays; core 0.7.1 does not (test_core071.py)
    rs, st = _capture_state([str(LEGACY_PACKS_DIR / "core-0.7.0")])
    st.provinces["P05"].units = {}
    for turn in range(4):
        taker, other = ("A", "C") if turn % 2 == 0 else ("C", "A")
        src = "P02" if taker == "A" else "P05"
        units = sum(st.provinces[src].units.values())
        if not units:
            st.provinces[src].units = {"core:assault": 8}
        st.provinces["P07"].units = {}
        book = _orders(st, rs, taker, [{"kind": "move", "from": src, "to": "P07",
                                        "units": {"assault": sum(st.provinces[src].units.values())}}])
        st, rep = resolve(st, {taker: book}, rs, 3)
        assert st.provinces["P07"].owner == taker
    # A took P07 twice (turns 1 and 3), C retook it twice: each side 2 points from it, kept after losing it
    assert sc.war_provinces(st, "A")["P07"]["points"] == 2 and sc.war_provinces(st, "C")["P07"]["points"] == 2
    assert sc.war_provinces(st, "A")["P07"]["since"] is None or st.provinces["P07"].owner == "A"
    entries = st.players["A"].tally["war_provinces"]
    entries["P07"]["points"] = 3
    st.provinces["P02"].units = {"core:assault": 8}
    st.provinces["P07"].units = {}
    book = _orders(st, rs, "A", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 8}}])
    st, rep = resolve(st, {"A": book}, rs, 3)
    assert st.provinces["P07"].owner == "A" and not _war_events(rep)           # at its cap: no more
    assert sc.war_provinces(st, "A")["P07"]["points"] == 3


def test_neutral_land_scores_no_war_and_the_total_cap_holds():
    rs, st = _capture_state()
    st.provinces["P04"].units = {"core:assault": 8}
    book = _orders(st, rs, "A", [{"kind": "move", "from": "P04", "to": "P13", "units": {"assault": 8}}])
    st, rep = resolve(st, {"A": book}, rs, 3)
    assert st.provinces["P13"].owner == "A" and not _war_events(rep)
    assert rs.stat("score.war.province_total") == 8
    st.players["A"].tally["war_provinces"] = {f"P{i:02d}": {"points": 3, "since": None} for i in (5, 6, 7, 8)}
    assert sc.war(st, rs, "A") == 8 and sc.war_cap_left(st, rs, "A") == 0
    free = build_ruleset(None, "blitz_score")
    assert sc.war(st, free, "A") == 8
    uncapped = build_ruleset(None, "quad4", scoring={"territory": 1, "war": 1})
    assert uncapped.stat("score.war.province_total") == 8      # the scenario's override stays with any weights


def test_the_growth_takes_four_points_from_a_province():
    rs = build_ruleset(None, "quad4")
    assert rs.stat("score.war.province_cap", "core:growth") == 4
    assert rs.stat("score.war.province_cap", "core:covenant") == 3
    d = describe(rs, "faction", "core:growth")
    assert any("очков войны с одной провинции" in b and "= 4" in b for b in d["bonuses"])


def test_legacy_rulesets_keep_the_kill_credit():
    old = build_ruleset([str(CORE060)], "blitz_score")
    assert not sc.war_by_province(old) and old.stat("score.war.per_point") == 15
    st = new_game_state(old, SEATS3, 4)
    st.provinces["P02"].units = {"core:assault": 8}
    st.provinces["P07"].units = {"core:assault": 2}
    book = _orders(st, old, "A", [{"kind": "move", "from": "P02", "to": "P07", "units": {"assault": 8}}])
    _, rep = resolve(st, {"A": book}, old, 3)
    assert "war_credit" in _kinds(rep) and "war_points" not in _kinds(rep)
    assert "Война: цена убитых" in rules_topic(old, "victory")


def test_rules_text_names_the_war_scale():
    rs = build_ruleset(None, "quad4")
    topic = rules_topic(rs, "victory")
    assert "- Война (шкала за провинцию)" in topic and "не больше 3 с одной провинции за партию и 8 всего" in topic


# ---------------------------------------------------------------- the journal, the report, the spectator

def test_a_played_game_journals_the_new_events_privately_and_reports_them(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="g7", scenario="core:blitz_score", seats=_seats(3), seed=5, turn_limit=4)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    game.start()
    while game.phase != "actions":
        clock.now += 10_000
        game.tick()
    # the Monument's first share (13+5) fits the starting stock (15+30); the second (12+5) does not at once
    site = next(p.id for p in game.state.owned("A") if p.type == "core:forge")
    res = game.submit("A", [{"kind": "build", "province": site, "building": "monument"}])
    assert res["accepted"], res
    while not game.game_over:
        clock.now += 10_000
        game.tick()
    events = read_events(gdir.journal_path)
    types = {e["type"] for e in events}
    assert "work_started" in types
    started = next(e for e in events if e["type"] == "work_started")
    assert started["shares"] == 2 and started["paid"] == {FOOD: 13, ALLOY: 5}
    assert types & {"build_paid", "build_stalled"}
    for e in events:
        if e["type"] in ("build_paid", "build_stalled"):
            assert e["seat"] == "A"
    # private to their seat until game_over (the journal here is over: check a cut copy)
    live = [e for e in events if e["type"] != "game_over"]
    assert not [e for e in view_events(live, seat="B") if e["type"] in ("build_paid", "build_stalled")]
    report = build_report(events)
    assert "stalled" in report["scoring"]["works"]["A"]
    assert "великие стройки" in render_report(report)
    assert replay(gdir).ok


def test_spectator_page_shows_intel_and_stalled_works():
    """Part 3 (integration): the spectator page labels the intel source and marks builds that stand without a share."""
    html = (Path(__file__).parent.parent / "src" / "dah" / "server" / "watch.html").read_text(encoding="utf-8")
    assert html.count('intel: "разв"') == 2                     # turn score by source and the final breakdown
    assert "works_stalled" in html and "b.stalled" in html


def test_spy_bot_targets_a_far_rival_only_while_intel_pays(monkeypatch):
    """Part 3: without a neighbour the spy target is the living rival with the highest public score, and only while
    a success still scores (`score.intel_pays`)."""
    from dah.bots import score as bs
    from dah.bots import spy

    class View:
        me = frozenset({"A", "Аргос"})
        players = ("A", "B", "C")
        scores = {"B": 4, "C": 7}
        intel = {"points": 1, "left": 2}

        def scoring(self, source):
            return source == "intel"

    view = View()
    monkeypatch.setattr(spy, "strongest_neighbour", lambda v, among=None: None)
    assert bs.intel_pays(view) and spy.rival_target(view) == "C"
    view.intel = {"points": 3, "left": 0}
    assert not bs.intel_pays(view) and spy.rival_target(view) is None
