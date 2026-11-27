"""Espionage, stage 3a (docs/SPY_AND_CONQUEST.md 1.5, 2, 4, 10) on the fixture pack tests/packs/t3: content, formulas,
the recon step (determinism, rounds, capture, reports and their honesty), sabotage, false entries, P2 rules, fog and
replay of an espionage game.

Map `tri` (conquest_helpers): capitals A1, B1, C1, forges A2, B2, C2, neutral M. Scenario `tri_spy`: espionage only,
6 turns; `tri_all`: espionage and conquest.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from conquest_helpers import MIL, PACK, PACKS, board, book, mv, ruleset
from dah.engine import (ContentError, GameState, SeatOrders, amend_order, build_ruleset, cancel_order, check_order,
                        orders_hash, render_state_text, resolve, seat_reserved, state_hash, submit_orders,
                        view_state)
from dah.engine import espionage as esp
from dah.engine.model import P2
from dah.engine.orders import cp_left
from dah.engine.resolve import income_of, upkeep_of
from dah.game import Game, make_config, replay
from dah.journal import read_events
from dah.store import init_game_dir

ALLOY, FOOD = "t3:alloy", "t3:provisions"


@pytest.fixture
def rs():
    return ruleset("tri_spy")


def give_shade(st: GameState, seat: str, xp: int = 0, hired_turn: int = 0) -> str:
    player = st.players[seat]
    player.spy_seq["shade"] = player.spy_seq.get("shade", 0) + 1
    sid = f"s{player.spy_seq['shade']}"
    player.shades.append({"id": sid, "xp": xp, "status": "idle", "hired_turn": hired_turn, "mission": None})
    return sid


def spy(shade: Any, mission: str, target: str, rounds: int) -> dict[str, Any]:
    return {"kind": "spy", "shade": shade, "mission": mission, "target": target, "rounds": rounds}


def first_seed(run: Callable[[int], Any], ok: Callable[[Any], bool], tries: int = 400) -> tuple[int, Any]:
    """The first seed (0, 1, ...) whose run passes `ok`: tests pick an outcome of the seeded rolls this way."""
    for seed in range(tries):
        out = run(seed)
        if ok(out):
            return seed, out
    raise AssertionError("no seed gives the wanted outcome")


def rounds_of(rep: dict[str, Any], seat: str) -> list[dict[str, Any]]:
    return [r for r in rep["rounds"] if r["seat"] == seat]


def kinds(report: Any, kind: str) -> list[dict[str, Any]]:
    return [e for e in report.events if e["kind"] == kind]


# ---------------------------------------------------------------- content

def pack_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "t3"
    shutil.copytree(PACK, dst)
    return dst


def edit(path: Path, change) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = change(data)
    path.write_text(yaml.safe_dump(out if out is not None else data, allow_unicode=True), encoding="utf-8")


def test_missions_load_only_with_espionage_and_are_hashed_only_then(rs):
    assert set(rs.missions) == {"t3:observe", "t3:ledger", "t3:read_orders", "t3:sabotage"}
    sab = rs.missions["t3:sabotage"]
    assert sab.loud and sab.target == "owned_province" and sab.params == {"deny_fortify": True, "defense_mul": 0.85,
                                                                          "income_mul": 0.0}
    assert rs.stat("spy.mission.t3:read_orders.difficulty") == 3 and rs.stat("spy.shade.cost.t3:alloy") == 25
    assert rs.espionage is not None and rs.espionage.shade_upkeep == {FOOD: 1}
    assert {"t3:veil", "t3:glass"} <= set(rs.techs) and "t3:mirror_hall" in rs.buildings
    off = ruleset("tri_conquest")
    data = off.to_dict()
    assert not off.missions and off.espionage is None and "missions" not in data and "espionage" not in data
    assert "t3:veil" not in off.techs and "t3:mirror_hall" not in off.buildings
    assert not any(k.startswith("spy.") for k in off.stats)
    assert rs.to_dict()["scenario"]["systems"] == ["espionage"]
    assert ruleset("tri_all").system("espionage") and ruleset("tri_all").system("conquest")


@pytest.mark.parametrize("change, match", [
    (lambda d: {**d, "missions": [{**d["missions"][0], "behavior": "steal"}, *d["missions"][1:]]}, "unknown behavior"),
    (lambda d: {**d, "missions": [*d["missions"][:3], {**d["missions"][3], "params": {"defense_mul": 2}}]},
     "defense_mul"),
    (lambda d: {**d, "missions": [*d["missions"][:3], {**d["missions"][3], "params": {"boom": 1}}]},
     "unknown parameter"),
    (lambda d: {**d, "missions": [{**d["missions"][0], "rounds": [3, 2]}, *d["missions"][1:]]}, "minimum"),
    (lambda d: {**d, "missions": [{**d["missions"][0], "target": "planet"}, *d["missions"][1:]]}, "planet"),
])
def test_bad_missions_are_content_errors(tmp_path, change, match):
    pack = pack_copy(tmp_path)
    edit(pack / "espionage" / "espionage.yaml", change)
    with pytest.raises(ContentError, match=match):
        build_ruleset([str(pack)], "tri_spy")


def test_two_espionage_configs_are_an_error(tmp_path):
    pack = pack_copy(tmp_path)
    (pack / "espionage" / "more.yaml").write_text("config:\n  shade: {cost: {alloy: 5}}\n", encoding="utf-8")
    with pytest.raises(ContentError, match="second espionage config"):
        build_ruleset([str(pack)], "tri_spy")


def test_mission_stats_take_short_ids_in_effects(tmp_path):
    pack = pack_copy(tmp_path)
    edit(pack / "scenarios" / "scenarios.yaml", lambda d: [
        {**s, "overrides": [{"effect": "modify", "target": "spy.mission.observe.max_rounds", "op": "add", "value": 1},
                            {"effect": "modify", "target": "spy.shade.cost.alloy", "op": "set", "value": 10}]}
        if s["id"] == "tri_spy" else s for s in d])
    rs = build_ruleset([str(pack)], "tri_spy")
    assert rs.stat("spy.mission.t3:observe.max_rounds") == 6 and esp.shade_cost(rs, None) == {ALLOY: 10}


# ---------------------------------------------------------------- formulas and estimate_mission

def test_estimate_mission_gives_the_concept_table(rs):
    st = board(rs)
    st.players["A"].techs = ["t3:veil"]                     # S2; B's capital has its army: C_loc = C1 + 1
    out = esp.estimate_mission(st, rs, "A", "read_orders", "НикB", 3)
    assert [(round(r["success"], 2), round(r["detect"], 2)) for r in out["per_round"]] == \
        [(0.45, 0.20), (0.50, 0.16), (0.55, 0.12)]
    assert round(out["survive"], 2) == 0.59 and round(out["any"], 2) == 0.63
    assert "45/20, 50/16, 55/12" in out["text"] and "дожить 59%" in out["text"] and "63%" in out["text"]
    assert len(out["text"]) <= esp.ESTIMATE_CHARS
    st.players["A"].techs = []                              # S1 again
    obs = esp.estimate_mission(st, rs, "A", "observe", "B2", 3)        # no army in B2: C_loc 1
    assert [(round(r["success"], 2), round(r["detect"], 2)) for r in obs["per_round"]] == \
        [(0.55, 0.10), (0.60, 0.06), (0.65, 0.03)]


def test_estimate_mission_trims_the_term_and_clamps(rs):
    st = board(rs)
    st.turn = 5                                             # turn_limit 6: two rounds left from actions
    out = esp.estimate_mission(st, rs, "A", "observe", "B2", 5)
    assert out["rounds"] == 2 and "обрезан" in out["text"]
    hard = esp.estimate_mission(st, rs, "A", "ledger", "B", 1, assume_mirrors=5)
    assert hard["per_round"][0]["detect"] == pytest.approx(0.75)     # .05+.10+.12*(6-1): below the .90 cap
    st.players["A"].techs = ["t3:veil"]
    easy = esp.estimate_mission(st, rs, "A", "observe", "B2", 1, assume_mirrors=0)
    assert easy["per_round"][0]["detect"] == pytest.approx(0.03)     # clamped at spy.detect.min
    with pytest.raises(ValueError):
        esp.estimate_mission(st, rs, "A", "sabotage", "M", 1)       # neutral: not for sabotage


def test_local_mirrors_count_army_hall_and_techs(rs):
    st = board(rs)
    assert esp.local_mirrors(st, rs, "B1") == 2 and esp.local_mirrors(st, rs, "B2") == 1
    assert esp.local_mirrors(st, rs, "M") == 0
    st.provinces["B2"].buildings = ["t3:mirror_hall"]
    st.players["B"].techs = ["t3:glass"]
    assert esp.local_mirrors(st, rs, "B2") == 3
    p_low = esp.attribution_chance(rs, None, 1, 1)
    from dah.engine.techs import stat_key
    p_glass = esp.attribution_chance(rs, stat_key(st, rs, "B"), 1, 1)
    assert p_low == pytest.approx(0.5) and p_glass == pytest.approx(0.65)


def test_honest_ranges_hold_the_truth_and_keep_the_observer_error():
    rnd = random.Random(7)
    for _ in range(1000):
        n, width = rnd.randint(0, 80), rnd.choice([0.0, 0.1, 0.3, 0.5, 1.0])
        lo, hi = esp.honest_range(rnd.randint(0, 9), rnd.choice("ABC"), "B", "stock", f"r{rnd.randint(0, 3)}", n,
                                  width)
        assert 0 <= lo <= n <= hi and hi - lo == max(2, -(-int(width * n * 1000) // 1000) if width else 2) or \
            (0 <= lo <= n <= hi)
    assert esp.honest_range(1, "A", "B", "stock", "x", 20, 0.3) == esp.honest_range(1, "A", "B", "stock", "x", 20, 0.3)
    windows = {tuple(esp.honest_range(1, seat, "B", "stock", "x", 20, 0.3)) for seat in "ACDEFGH"}
    assert len(windows) > 1                       # different observers, different windows
    lo, hi = esp.honest_range(1, "A", "B", "stock", "x", 0, 0.3)
    assert lo == 0 and hi == 2                    # small numbers are not pulled to a visible 0


# ---------------------------------------------------------------- orders

def test_espionage_orders_validate(rs):
    st = board(rs)
    sid = give_shade(st, "A")
    ok = lambda item, phase="actions", b=None: check_order(st, rs, "A", b or SeatOrders(), item, phase)  # noqa: E731
    assert ok({"kind": "hire_shade"}).ok
    assert ok(spy(1, "observe", "B2", 2)).ok and ok(spy("s1", "Наблюдение", "M", 1)).ok
    assert ok(spy(sid, "ledger", "НикB", 1)).order.target == "B"
    assert ok(spy(sid, "read_orders", "B", 1)).reason == "MISSION_LOCKED"       # S1 < 2
    assert ok(spy(sid, "observe", "A2", 1)).reason == "BAD_TARGET"
    assert ok(spy(sid, "sabotage", "M", 1)).reason in ("BAD_TARGET", "MISSION_LOCKED")
    assert ok(spy(sid, "observe", "B2", 6)).reason == "BAD_ROUNDS"
    assert ok(spy(sid, "observe", "B2", 0)).reason == "INVALID"
    assert ok(spy("s9", "observe", "B2", 1)).reason == "UNKNOWN_ID"
    assert ok({"kind": "spy", "shade": 1, "mission": "observe", "target": "B2"}).reason == "INVALID"
    assert ok({"kind": "recall", "shade": 1}).reason == "INVALID"              # not on a mission
    # core 0.8.0 (docs/SPY_V2.md 4.6): the Ward is given in actions, in the seat's own province, one per province
    assert ok({"kind": "ward", "province": "A1"}).ok
    assert ok({"kind": "ward", "province": "B2"}).reason == "NOT_OWNER"
    assert ok({"kind": "ward", "province": "A1"}, P2).reason == "PHASE_LOCKED"
    st.players["A"].techs = ["t3:veil"]
    assert ok(spy(sid, "sabotage", "M", 1)).reason == "BAD_TARGET"
    assert ok(spy(sid, "sabotage", "B2", 1)).ok
    st.turn = 6                                                                 # the last turn
    assert ok(spy(sid, "observe", "B2", 1)).ok and ok(spy(sid, "observe", "B2", 2)).reason == "BAD_ROUNDS"
    assert ok(spy(sid, "observe", "B2", 1), P2).reason == "BAD_ROUNDS"          # from P2: next turn, none left


def test_hire_limits_capital_and_one_per_turn(rs):
    st = board(rs)
    two = submit_orders(st, rs, "A", SeatOrders(), [{"kind": "hire_shade"}, {"kind": "hire_shade"}], "actions")
    assert [c.ok for c in two.results] == [True, False] and two.results[1].reason == "INVALID"
    assert seat_reserved(st, rs, "A", two.book) == {ALLOY: 25}
    give_shade(st, "A")
    give_shade(st, "A")
    assert check_order(st, rs, "A", SeatOrders(), {"kind": "hire_shade"}, "actions").reason == "SHADE_LIMIT"
    st.players["A"].shades.pop()
    st.provinces["A1"].owner = "B"
    assert check_order(st, rs, "A", SeatOrders(), {"kind": "hire_shade"}, "actions").reason == "NO_CAPITAL"
    st.provinces["A1"].owner = "A"
    st.players["A"].resources[ALLOY] = 10
    assert check_order(st, rs, "A", SeatOrders(), {"kind": "hire_shade"}, "actions").reason == "NOT_ENOUGH_RESOURCES"


def test_active_missions_are_limited_by_whisper(rs):
    st = board(rs)
    s1, s2 = give_shade(st, "A"), give_shade(st, "A")
    res = submit_orders(st, rs, "A", SeatOrders(), [spy(s1, "observe", "B2", 1), spy(s2, "observe", "C2", 1)],
                        "actions")
    assert res.results[0].ok and res.results[1].reason == "TOO_MANY_MISSIONS"     # 1 + S1 // 2 = 1
    st.players["A"].techs = ["t3:veil"]
    res = submit_orders(st, rs, "A", SeatOrders(), [spy(s1, "observe", "B2", 1), spy(s2, "observe", "C2", 1)],
                        "actions")
    assert all(c.ok for c in res.results)                                          # 1 + S2 // 2 = 2
    again = check_order(st, rs, "A", res.book, spy(s1, "observe", "M", 1), "actions")
    assert again.reason == "SHADE_BUSY"


# ---------------------------------------------------------------- the recon step

def test_hire_in_actions_is_done_and_paid_by_recon(rs):
    st = board(rs)
    b = book(st, rs, "A", [{"kind": "hire_shade"}])
    new, rep = esp.recon(st, {"A": b}, rs, 1)
    a = new.players["A"]
    assert a.shades == [{"id": "s1", "xp": 0, "status": "idle", "hired_turn": 1, "mission": None}]
    assert a.resources[ALLOY] == 5 and a.spy_seq == {"shade": 1} and new.phase == P2
    assert seat_reserved(new, rs, "A", b) == {}            # recon paid it: the P2 reserve holds nothing for it
    assert cancel_order(new, rs, "A", b, b.orders[0].id, P2).reason == "PHASE_LOCKED"
    late = check_order(new, rs, "A", b, spy("s1", "observe", "B2", 1), P2)
    assert late.reason == "SHADE_BUSY"                     # a Shade hired this turn takes missions from the next Д
    after, report = resolve(new, {"A": b}, rs, 1)
    assert not kinds(report, "order_rejected") and len(after.players["A"].shades) == 1
    assert upkeep_of(after, rs, "A")[FOOD] == 3 + 1        # 3 militia + the Shade
    assert check_order(after, rs, "A", SeatOrders(), spy("s1", "observe", "B2", 1), "actions").ok
    assert state_hash(new) != state_hash(st)


def test_recon_is_pure_and_deterministic(rs):
    st = board(rs)
    give_shade(st, "A")
    b = book(st, rs, "A", [spy(1, "observe", "B2", 2)])
    before = state_hash(st)
    one = esp.recon(st, {"A": b}, rs, 3)
    two = esp.recon(st, {"A": b}, rs, 3)
    assert state_hash(st) == before
    assert one[1]["hash"] == two[1]["hash"] and state_hash(one[0]) == state_hash(two[0])


def test_a_new_shade_of_one_seat_does_not_move_anothers_rolls(rs):
    st = board(rs)
    give_shade(st, "B")
    bb = book(st, rs, "B", [spy(1, "observe", "A2", 3)])
    _, alone = esp.recon(st, {"B": bb}, rs, 9)
    give_shade(st, "A")
    give_shade(st, "C")
    ba = book(st, rs, "A", [spy(1, "observe", "C2", 2), {"kind": "hire_shade"}])
    bc = book(st, rs, "C", [spy(1, "observe", "B2", 2)])
    _, crowd = esp.recon(st, {"A": ba, "B": bb, "C": bc}, rs, 9)
    pick = lambda rep: [(r["u_det"], r.get("u_ok")) for r in rounds_of(rep, "B")]  # noqa: E731
    assert pick(alone) == pick(crowd)


def test_round_order_of_checks_and_a_full_term(rs):
    st = board(rs)
    give_shade(st, "A")
    b = book(st, rs, "A", [spy(1, "observe", "B2", 2)])

    def play(seed: int) -> list[dict[str, Any]]:
        s1, rep1 = esp.recon(st, {"A": b}, rs, seed)
        assert cancel_order(s1, rs, "A", b, b.orders[0].id, P2).reason == "PHASE_LOCKED"
        assert amend_order(s1, rs, "A", b, b.orders[0].id, {"rounds": 1}, P2).reason == "PHASE_LOCKED"
        s1, report = resolve(s1, {"A": b}, rs, seed)
        assert not kinds(report, "order_rejected")          # the recon step carried it out: not re-checked
        s2, rep2 = esp.recon(s1, {}, rs, seed)
        return [rounds_of(rep1, "A"), rounds_of(rep2, "A"), s2]

    seed, (r1, r2, s2) = first_seed(play, lambda o: o[0] and o[1] and not o[0][0]["detected"]
                                    and not o[1][0]["detected"])
    assert [r1[0]["round"], r2[0]["round"]] == [1, 2] and r1[0]["K"] == 0 and r2[0]["K"] == 1
    m = s2.players["A"].shades[0]["mission"]
    assert m["played"] == 2 and m["ended"] in ("done", "empty")
    after, _ = resolve(s2, {}, rs, seed)
    shade = after.players["A"].shades[0]
    assert shade["status"] == "idle" and shade["mission"] is None
    assert shade["xp"] == (1 if m["successes"] else 0)
    s3, rep3 = esp.recon(after, {}, rs, seed)
    assert not rounds_of(rep3, "A")                         # exactly N rounds


def test_empty_term_and_the_report_of_a_success(rs):
    st = board(rs, {"A1": {MIL: 3}, "B1": {MIL: 3}, "B2": {MIL: 2}, "C1": {MIL: 3}})
    give_shade(st, "A")
    b = book(st, rs, "A", [spy(1, "observe", "B2", 1)])
    run = lambda seed: esp.recon(st, {"A": b}, rs, seed)  # noqa: E731
    _, (s_empty, rep_empty) = first_seed(run, lambda o: rounds_of(o[1], "A")[0]["outcome"] == "nothing")
    assert s_empty.players["A"].shades[0]["mission"]["ended"] == "empty"
    assert "ничего" in esp.state_line(rep_empty, rs, s_empty, "A")
    _, (s_found, rep) = first_seed(run, lambda o: rounds_of(o[1], "A")[0]["outcome"] == "found")
    report = rep["reports"]["A"][0]
    assert report["id"] == "r1" and s_found.players["A"].spy_seq["report"] == 1
    assert report["truth"]["units"] == {MIL: 2}
    lo, hi = report["facts"]["units"][MIL]
    assert lo <= 2 <= hi and hi - lo == 2
    text = esp.report_text(report, rs, s_found, "A")
    assert text.startswith("#r1 s1 → B2") and "снимок: конец Д хода 1" in text and len(text) <= 400
    assert "найдено #r1" in esp.state_line(rep, rs, s_found, "A")
    assert esp.notice_text(rep, "A").startswith("Разведсрез хода 1: донесений 1 (найдено 1)")
    assert esp.notice_text(rep, "B") is None                 # the target learns nothing of a round that did not catch


def test_ledger_shows_free_stock_in_ranges_and_exact_skills(rs):
    st = board(rs)
    give_shade(st, "A")
    give_shade(st, "B")
    a = book(st, rs, "A", [spy(1, "ledger", "B", 1)])
    b = book(st, rs, "B", [{"kind": "hire_shade"}, {"kind": "research", "tech": "t3:drill"}])
    _, (_, rep) = first_seed(lambda seed: esp.recon(st, {"A": a, "B": b}, rs, seed),
                             lambda o: o[1]["reports"].get("A"))
    report = rep["reports"]["A"][0]
    truth, facts = report["truth"], report["facts"]
    assert truth["stock"][ALLOY] == 5                         # 30 minus the 25 the hire holds at the close of Д
    assert truth["researching"] == "t3:drill" and truth["shades"] == 1 and truth["whisper"] == 1
    lo, hi = facts["stock"][ALLOY]
    assert lo <= 5 <= hi
    assert "S1 C1; Теней 1" in esp.report_text(report, rs, board(rs), "A")


def test_persistent_observer_error_gives_one_window_for_an_unchanged_value(rs):
    st = board(rs)
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "ledger", "B", 1)])
    later = GameState.from_dict(st.to_dict())
    later.turn = 3                                           # another turn of the same game, the same stock

    def both(seed: int) -> list[Any]:
        return [esp.recon(x, {"A": a}, rs, seed)[1]["reports"].get("A") for x in (st, later)]

    seed, (one, two) = first_seed(both, lambda o: o[0] and o[1])
    assert one[0]["facts"]["stock"] == two[0]["facts"]["stock"]          # repeated reports do not narrow it
    assert one[0]["turn"] == 1 and two[0]["turn"] == 3


def test_read_orders_snapshot_canonical_feints_and_command_points(rs):
    st = board(rs, {"A1": {MIL: 3}, "B1": {MIL: 3}, "B2": {MIL: 4}, "C1": {MIL: 3}})
    st.players["A"].techs = ["t3:veil"]                        # S2
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "read_orders", "B", 1)])
    b = book(st, rs, "B", [mv("B2", "A2", 2), {**mv("B2", "C2", 1), "feint": True}, {"kind": "fortify",
                                                                                      "province": "B1"},
                           {"kind": "research", "tech": "t3:drill"}])
    assert b.cp_spent == 1 and cp_left(st, "B", b) == 2       # the false entry cost 1 CP of the turn
    _, (new, rep) = first_seed(lambda seed: esp.recon(st, {"A": a, "B": b}, rs, seed),
                               lambda o: o[1]["reports"].get("A"))
    report = rep["reports"]["A"][0]
    entries = report["facts"]["entries"]
    assert [e["kind"] for e in entries] == ["fortify", "move", "move"]          # sorted, no research
    assert all("id" not in e and "feint" not in e and "phase_added" not in e for e in entries)
    truth = report["truth"]["entries"]
    assert [e.get("feint", False) for e in truth] == [False, False, True] or \
        [e.get("feint", False) for e in truth] == [False, True, False]
    assert report["facts"]["command_points"] == 3                 # command_points, not what is left
    # next to A's land: B1 (3, no move) and B2 (4 minus 3 in moves, the false one counts too)
    assert report["facts"]["free_at_borders"] == {"B1": {MIL: 3}, "B2": {MIL: 1}}
    assert len(report["feints"]) == 1
    # P2 edits never change this turn's report: it is the snapshot of the close of Д
    after = amend_order(new, rs, "B", b, b.orders[0].id, {"units": {"militia": 1}}, P2)
    assert after.ok and rep["reports"]["A"][0]["truth"]["entries"] == truth


def test_read_orders_exposes_false_entries_by_its_roll(rs):
    st = board(rs, {"A1": {MIL: 3}, "B1": {MIL: 3}, "B2": {MIL: 4}, "C1": {MIL: 3}})
    st.players["A"].techs = ["t3:veil"]
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "read_orders", "B", 1)])
    b = book(st, rs, "B", [{**mv("B2", "C2", 1), "feint": True}])
    _, (new, rep) = first_seed(lambda seed: esp.recon(st, {"A": a, "B": b}, rs, seed),
                               lambda o: o[1]["reports"].get("A") and o[1]["reports"]["A"][0]["feints"][0]["exposed"])
    report = rep["reports"]["A"][0]
    assert report["facts"]["entries"][0]["exposed"] is True
    assert "[ЛОЖНАЯ ЗАПИСЬ]" in esp.report_text(report, rs, new, "A")
    assert 0 < report["feints"][0]["p"] <= 0.9


def test_read_orders_compact_text_of_a_long_book_and_full_text(rs):
    st = board(rs, {"A1": {MIL: 3}, "B1": {MIL: 9}, "B2": {MIL: 9}, "C1": {MIL: 3}})
    st.players["A"].techs = ["t3:veil"]
    st.players["B"].resources = {ALLOY: 300, FOOD: 30, "t3:creed": 0}
    st.provinces["B1"].buildings = ["t3:barracks"]
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "read_orders", "B", 1)])
    items = [mv("B1", "A1", 1), mv("B1", "C1", 1), mv("B2", "A2", 1), mv("B2", "C2", 1), mv("B2", "M", 1),
             {**mv("B1", "B2", 1), "feint": True}, {"kind": "fortify", "province": "B1"},
             {"kind": "fortify", "province": "B2"}, {"kind": "recruit", "province": "B1", "unit": "militia", "count": 1},
             {"kind": "build", "province": "B2", "building": "hq"}, {"kind": "build", "province": "B1",
                                                                     "building": "hall"},
             {"kind": "build", "province": "B2", "building": "hall"}, mv("B1", "B2", 1), mv("B2", "B1", 1),
             {"kind": "recruit", "province": "B1", "unit": "militia", "count": 2}]
    b = book(st, rs, "B", items)
    assert len(b.orders) == 15
    _, (new, rep) = first_seed(lambda seed: esp.recon(st, {"A": a, "B": b}, rs, seed),
                               lambda o: o[1]["reports"].get("A"))
    report = rep["reports"]["A"][0]
    compact = esp.report_text(report, rs, new, "A")
    full = esp.report_text(report, rs, new, "A", full=True)
    assert len(compact) <= 400 and "ещё" in compact
    assert len(full) <= 1200 and "ещё" not in full and len(full) > len(compact)
    assert esp.intel_text(rep, rs, new, "A", report_id="r1") == full
    assert len(esp.intel_text(rep, rs, new, "A")) <= 1500


# ---------------------------------------------------------------- capture

def _caught(rs, st, books, seat="A", attributed=None):
    def ok(o):
        caught = o[1].get("caught", [])
        return any(c["owner"] == seat and (attributed is None or c["attributed"] == attributed) for c in caught)
    return first_seed(lambda seed: esp.recon(st, books, rs, seed), ok)


def test_a_caught_shade_is_lost_and_only_the_victim_learns_where_and_what(rs):
    st = board(rs)
    give_shade(st, "A", xp=2)
    a = book(st, rs, "A", [spy(1, "ledger", "B", 3)])
    for attributed in (True, False):
        seed, (new, rep) = _caught(rs, st, {"A": a}, attributed=attributed)
        assert new.players["A"].shades == []                    # lost for good, with its experience
        assert new.players["A"].resources[FOOD] == 30 - 6       # the whole term was paid and is not returned
        capture = rep["captures"]["B"][0]
        assert capture["province"] == "B1" and capture["mission"] == "t3:ledger" and capture["target"] == "B"
        assert capture["owner"] == ("A" if attributed else None)
        line = esp.state_line(rep, rs, new, "B")
        assert "ПОЙМАНА У ТЕБЯ: B1" in line and "НикA" not in line
        spies_b = esp.spies_text(new, rs, "B", rep)
        assert ("Тень НикA" in spies_b) == attributed and ("хозяин не установлен" in spies_b) != attributed
        own = esp.state_line(rep, rs, new, "A")
        assert own == "ДОНЕСЕНИЯ: s1: нет связи"                 # no word of a capture, its taker or attribution
        assert "нет связи, потеряна" in esp.spies_text(new, rs, "A", rep)
        assert esp.notice_text(rep, "C") is None and esp.state_line(rep, rs, new, "C") is None
        assert "поймано у тебя: 1" in esp.notice_text(rep, "B")
        assert upkeep_of(new, rs, "A")[FOOD] == 3                # no upkeep for a lost Shade
        assert check_order(new, rs, "A", SeatOrders(), {"kind": "hire_shade"}, "actions").ok   # the slot is free


def test_counter_intelligence_raises_attribution(rs):
    st = board(rs)
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "ledger", "B", 1)])
    _, (_, plain) = _caught(rs, st, {"A": a})
    st.players["B"].techs = ["t3:glass"]
    _, (_, glass) = _caught(rs, st, {"A": a})
    assert glass["caught"][0]["p_attrib"] > plain["caught"][0]["p_attrib"]


def test_capture_in_a_neutral_province_tells_nobody(rs):
    st = board(rs, {"A1": {MIL: 3}, "B1": {MIL: 3}, "C1": {MIL: 3}, "M": {MIL: 5}})
    st.provinces["M"].owner = None
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "observe", "M", 5)])
    _, (new, rep) = _caught(rs, st, {"A": a})
    assert rep["captures"] == {} and rep["lost"] == {"A": ["s1"]}
    assert all(esp.notice_text(rep, s) is None for s in "BC")


def test_catalog_names_espionage_stats(rs):
    from dah.engine import describe
    assert "Шёпот (S) +1" in describe(rs, "tech", "t3:veil")["text"]
    assert "Зеркала в провинции" in describe(rs, "building", "t3:mirror_hall")["text"]


def test_no_captive_decisions_exist():
    import dah.engine as engine
    import dah.game as game
    assert not hasattr(engine, "decide_captive") and not hasattr(game.Game, "decide_captive")
    from dah.engine.registry import ORDER_KINDS
    assert "captive" not in ORDER_KINDS


# ---------------------------------------------------------------- recalls, void, elimination

def test_recall_in_actions_skips_the_round_without_refund(rs):
    st = board(rs)
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "observe", "B2", 3)])
    seed, (s1, _) = first_seed(lambda seed: esp.recon(st, {"A": a}, rs, seed),
                               lambda o: o[0].players["A"].shades)
    s1, _ = resolve(s1, {"A": a}, rs, seed)
    food = s1.players["A"].resources[FOOD]
    r = book(s1, rs, "A", [{"kind": "recall", "shade": "s1"}])
    s2, rep = esp.recon(s1, {"A": r}, rs, seed)
    assert not rounds_of(rep, "A") and s2.players["A"].shades[0]["mission"]["ended"] == "recalled"
    assert s2.players["A"].resources[FOOD] == food
    s3, _ = resolve(s2, {"A": r}, rs, seed)
    assert s3.players["A"].shades[0]["status"] == "idle"


def test_cancelled_spy_entry_before_the_close_costs_nothing(rs):
    st = board(rs)
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "observe", "B2", 3)])
    assert seat_reserved(st, rs, "A", a) == {FOOD: 3}
    a = cancel_order(st, rs, "A", a, a.orders[0].id, "actions").book
    new, rep = esp.recon(st, {"A": a}, rs, 1)
    assert new.players["A"].resources[FOOD] == 30 and not rep["rounds"]


def test_p2_spy_starts_next_turn_and_p2_recall_costs_cp(rs):
    st = board(rs)
    give_shade(st, "A")
    s1, _ = esp.recon(st, {}, rs, 1)
    a = submit_orders(s1, rs, "A", SeatOrders(), [spy(1, "observe", "B2", 2)], P2,
                      id_factory=lambda: "o1").book
    assert a.cp_spent == 1 and a.orders[0].phase_added == P2
    assert amend_order(s1, rs, "A", a, "o1", {"rounds": 1}, P2).reason in ("PHASE_LOCKED", "INVALID")
    s2, report = resolve(s1, {"A": a}, rs, 1)
    m = s2.players["A"].shades[0]["mission"]
    assert m["first_turn"] == 2 and m["played"] == 0 and s2.players["A"].resources[FOOD] < 30
    assert kinds(report, "mission_started")[0]["seats"] == ["A"]
    _, (s3, rep) = first_seed(lambda seed: esp.recon(s2, {}, rs, seed), lambda o: not o[1]["caught"]
                              if "caught" in o[1] else True)
    assert rounds_of(rep, "A")[0]["round"] == 1
    rc = submit_orders(s3, rs, "A", SeatOrders(), [{"kind": "recall", "shade": "s1"}], P2,
                       id_factory=lambda: "o2")
    assert rc.results[0].ok and rc.book.cp_spent == 1
    s4, report = resolve(s3, {"A": rc.book}, rs, 1)
    assert s4.players["A"].shades[0]["status"] == "idle"
    assert kinds(report, "mission_end")[0]["reason"] == "recalled"


def test_void_missions_when_the_target_province_becomes_own_or_the_seat_is_out(rs):
    st = board(rs)
    give_shade(st, "A")
    give_shade(st, "A")
    st.players["A"].techs = ["t3:veil"]
    a = book(st, rs, "A", [spy(1, "observe", "B2", 3), spy(2, "ledger", "C", 3)])
    seed, (s1, _) = first_seed(lambda seed: esp.recon(st, {"A": a}, rs, seed),
                               lambda o: len(o[0].players["A"].shades) == 2)
    s1, _ = resolve(s1, {"A": a}, rs, seed)
    s1.provinces["B2"].owner = "A"
    s1.provinces["C1"].owner = s1.provinces["C2"].owner = "B"
    s1.provinces["C1"].units = {}
    s1, report = resolve(s1, {}, rs, seed)               # C has no land: out; the ledger on it is void
    assert "C" in report.public["eliminated"]
    ends = {e["shade"]: e["reason"] for e in kinds(report, "mission_end")}
    assert ends == {"s2": "void"}
    s2, rep = esp.recon(s1, {}, rs, seed)
    outcomes = {r["shade"]: r["outcome"] for r in rounds_of(rep, "A")}
    assert outcomes == {"s1": "void"}                     # B2 became A's own: no round, the mission is over
    s3, _ = resolve(s2, {}, rs, seed)
    assert all(sh["status"] == "idle" for sh in s3.players["A"].shades)


def test_an_eliminated_owner_loses_its_shades(rs):
    st = board(rs)
    give_shade(st, "C")
    st.provinces["C1"].owner = st.provinces["C2"].owner = "B"
    after, _ = resolve(st, {}, rs, 1)
    assert not after.players["C"].alive and after.players["C"].shades == []


def test_check_place_after_the_capital_is_lost(rs):
    st = board(rs)
    m = rs.missions["t3:ledger"]
    assert esp.check_place(st, rs, m, "B") == "B1"
    st.provinces["B1"].owner = "C"
    assert esp.check_place(st, rs, m, "B") == "B2"


# ---------------------------------------------------------------- sabotage

def _strike(rs, st, a, extra=None):
    books = {"A": a, **(extra or {})}
    return first_seed(lambda seed: esp.recon(st, books, rs, seed),
                      lambda o: any(r["outcome"] == "struck" for r in rounds_of(o[1], "A")))



def test_sabotage_denies_a_kept_fortification_for_its_turn_only():
    """combat.fortify_persists: the strike's deny_fortify takes the fortification away in its turn's battles; it is
    back the turn after (the army never left)."""
    rs = ruleset("tri_spy_hold")
    st = board(rs, {"A1": {MIL: 3}, "A2": {MIL: 3}, "B1": {MIL: 3}, "B2": {MIL: 2}, "C1": {MIL: 3}})
    st.provinces["B2"].fortified = True                        # fortified earlier, kept since
    st.players["A"].techs = ["t3:veil"]
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "sabotage", "B2", 2), mv("A2", "B2", 1)])
    seed, (new, rep) = _strike(rs, st, a)
    after, report = resolve(new, {"A": a}, rs, seed)
    battle = next(e for e in kinds(report, "battle") if e["province"] == "B2")
    assert not battle["fortified"] and not battle["sides"][0]["won"]     # denied in this turn; the attack failed
    assert after.provinces["B2"].owner == "B" and after.provinces["B2"].fortified     # kept: back next turn
    assert resolve(after, {}, rs, seed)[0].provinces["B2"].fortified

def test_sabotage_strikes_in_this_resolution(rs):
    st = board(rs, {"A1": {MIL: 3}, "A2": {MIL: 3}, "B1": {MIL: 3}, "B2": {MIL: 2}, "C1": {MIL: 3}})
    st.players["A"].techs = ["t3:veil"]
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "sabotage", "B2", 2)])
    b = book(st, rs, "B", [{"kind": "fortify", "province": "B2"}])
    seed, (new, rep) = _strike(rs, st, a, {"B": b})
    mark = new.provinces["B2"].status["sabotaged"]
    assert mark == [{"turn": 1, "by": "A", "shade": "s1", "deny_fortify": True, "defense_mul": 0.85,
                     "income_mul": 0.0}]
    assert "окно найдено: удар по B2" in esp.state_line(rep, rs, new, "A")
    assert view_state(new, rs, "B")["provinces"][3].get("status") is None      # the mark is not public
    income_b = income_of(new, rs, "B")
    assert income_b == income_of(st, rs, "B")                  # P2 shows the victim nothing of the coming strike
    after, report = resolve(new, {"A": a, "B": b}, rs, seed)
    assert not after.provinces["B2"].fortified and "sabotaged" not in after.provinces["B2"].status
    economy = next(e for e in kinds(report, "economy") if e["seats"] == ["B"])
    assert economy["income"][ALLOY] == income_b[ALLOY] - 4                     # forge B2 gave 0 this turn
    assert income_of(after, rs, "B")[ALLOY] == income_b[ALLOY]                 # and gives again next turn
    strike = kinds(report, "sabotage")[0]
    assert strike["effects"]["defense_mul"] == 0.85
    seen_b = next(e for e in report.by_seat["B"] if e["kind"] == "sabotage")
    assert "seat" not in seen_b and "shade" not in seen_b                       # the victim never learns the author
    assert seen_b["seats"] == ["B"]                                             # not even from the event's seats
    assert next(e for e in report.by_seat["A"] if e["kind"] == "sabotage")["seat"] == "A"
    assert not any(e["kind"] == "sabotage" for e in report.by_seat["C"])
    assert after.players["A"].shades[0]["status"] == "idle" and after.players["A"].shades[0]["xp"] == 1


def test_sabotage_lowers_the_defense(rs):
    st = board(rs, {"A1": {MIL: 3}, "A2": {MIL: 3}, "B1": {MIL: 3}, "B2": {MIL: 2}, "C1": {MIL: 3}})
    st.players["A"].techs = ["t3:veil"]
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "sabotage", "B2", 1)])
    seed, (new, _) = _strike(rs, st, a)
    attack = book(new, rs, "A", [mv("A2", "B2", 3)], phase=P2)
    _, report = resolve(new, {"A": attack}, rs, seed)
    battle = next(e for e in kinds(report, "battle") if e["province"] == "B2")
    calm = SeatOrders(list(attack.orders))
    clean = GameState.from_dict(new.to_dict())
    clean.provinces["B2"].status = {}
    _, plain = resolve(clean, {"A": calm}, rs, seed)
    plain_battle = next(e for e in kinds(plain, "battle") if e["province"] == "B2")
    assert battle["sides"][1]["strength"] == pytest.approx(plain_battle["sides"][1]["strength"] * 0.85, rel=1e-3)


def test_p2_recall_cancels_the_strike(rs):
    st = board(rs)
    st.players["A"].techs = ["t3:veil"]
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "sabotage", "B2", 2)])
    seed, (new, _) = _strike(rs, st, a)
    food = new.players["A"].resources[FOOD]
    rc = submit_orders(new, rs, "A", a, [{"kind": "recall", "shade": "s1"}], P2, id_factory=lambda: "o9")
    assert rc.results[0].ok and rc.book.cp_spent == 1
    after, report = resolve(new, {"A": rc.book}, rs, seed)
    assert not kinds(report, "sabotage")
    cancelled = kinds(report, "strike_cancelled")
    assert cancelled and cancelled[0]["seats"] == ["A"] and not any(e["kind"] == "strike_cancelled"
                                                                    for e in report.by_seat["B"])
    assert after.players["A"].resources[FOOD] <= food        # no refund of the term


def test_victims_p2_view_does_not_show_the_coming_strike(rs):
    """A strike set in recon stays hidden in P2: the victim's state (JSON and text) is that of the clean state."""
    st = board(rs, {"A1": {MIL: 3}, "A2": {MIL: 3}, "B1": {MIL: 3}, "B2": {MIL: 2}, "C1": {MIL: 3}})
    st.players["A"].techs = ["t3:veil"]
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "sabotage", "B2", 2)])
    _, (new, _) = _strike(rs, st, a)
    clean = GameState.from_dict(new.to_dict())
    clean.provinces["B2"].status.pop("sabotaged")
    assert view_state(new, rs, "B") == view_state(clean, rs, "B")
    assert render_state_text(new, rs, "B") == render_state_text(clean, rs, "B")


def _double_strike(rs):
    """A and C both strike B2 in one recon (the first seed where both succeed)."""
    st = board(rs, {"A1": {MIL: 3}, "A2": {MIL: 3}, "B1": {MIL: 3}, "B2": {MIL: 2}, "C1": {MIL: 3}, "C2": {MIL: 3}})
    for seat in ("A", "C"):
        st.players[seat].techs = ["t3:veil"]
        give_shade(st, seat)
    books = {s: book(st, rs, s, [spy(1, "sabotage", "B2", 2)]) for s in ("A", "C")}
    seed, (new, rep) = first_seed(lambda sd: esp.recon(st, books, rs, sd),
                                  lambda o: {x["seat"] for x in o[1]["strikes"]} == {"A", "C"})
    return seed, new, rep, books


def test_two_strikes_on_one_province_both_stand(rs):
    seed, new, rep, books = _double_strike(rs)
    marks = new.provinces["B2"].status["sabotaged"]
    assert sorted(m["by"] for m in marks) == ["A", "C"]
    after, report = resolve(new, books, rs, seed)
    strikes = kinds(report, "sabotage")
    assert sorted(e["seat"] for e in strikes) == ["A", "C"]                     # one event per author
    for seat in ("A", "C"):
        own = [e for e in report.by_seat[seat] if e["kind"] == "sabotage"]
        assert [e["seat"] for e in own if "seat" in e] == [seat]                # the other author stays unnamed
    seen_b = [e for e in report.by_seat["B"] if e["kind"] == "sabotage"]
    assert len(seen_b) == 2 and all(e["seats"] == ["B"] and "seat" not in e for e in seen_b)
    assert "sabotaged" not in after.provinces["B2"].status


def test_recall_cancels_only_the_recalling_shades_strike(rs):
    seed, new, rep, books = _double_strike(rs)
    rc = submit_orders(new, rs, "C", books["C"], [{"kind": "recall", "shade": "s1"}], P2, id_factory=lambda: "o9")
    assert rc.results[0].ok
    after, report = resolve(new, {"A": books["A"], "C": rc.book}, rs, seed)
    assert [e["seat"] for e in kinds(report, "strike_cancelled")] == ["C"]
    assert [e["seat"] for e in kinds(report, "sabotage")] == ["A"]              # A's paid strike still lands
    assert "sabotaged" not in after.provinces["B2"].status


def test_p2_recall_of_an_ended_mission_is_invalid(rs):
    st = board(rs)
    sid = give_shade(st, "A")
    a = book(st, rs, "A", [spy(sid, "observe", "M", 1)])
    new, rep = esp.recon(st, {"A": a}, rs, 1)
    shade = new.players["A"].shades[0]
    assert shade["status"] == "mission" and shade["mission"]["ended"] in ("done", "empty")
    rc = submit_orders(new, rs, "A", a, [{"kind": "recall", "shade": sid}], P2, id_factory=lambda: "o9")
    assert not rc.results[0].ok and rc.results[0].reason == "INVALID" and rc.book.cp_spent == 0
    assert "закончено" in rc.results[0].message


def test_list_spies_speaks_russian_and_prices_missions(rs):
    st = board(rs)
    sid = give_shade(st, "A")
    a = book(st, rs, "A", [spy(sid, "observe", "M", 1)])
    new, rep = esp.recon(st, {"A": a}, rs, 1)
    text = esp.spies_text(new, rs, "A", rep)
    ended = new.players["A"].shades[0]["mission"]["ended"]
    assert f"[{esp.END_TEXT[ended]}]" in text and f"[{ended}]" not in text
    sab = rs.missions["t3:sabotage"]
    cost = "+".join(f"{n} {rs.resources[r].short}" for r, n in sorted(sab.cost.items()))
    per = "+".join(f"{n} {rs.resources[r].short}" for r, n in sorted(sab.cost_per_round.items()))
    assert f"{cost}+{per}/р" in text and len(text) <= esp.SPIES_CHARS


def test_feint_exposed_carries_the_order_id(rs):
    st = board(rs)
    st.players["A"].techs = ["t3:veil"]
    sid = give_shade(st, "A", xp=3)
    a = book(st, rs, "A", [spy(sid, "read_orders", "B", 1)])
    b = book(st, rs, "B", [{**mv("B1", "A1", 1), "feint": True}])
    _, (_, rep) = first_seed(lambda sd: esp.recon(st, {"A": a, "B": b}, rs, sd),
                             lambda o: any(f["exposed"] for r in o[1]["reports"].get("A", [])
                                           for f in r.get("feints", [])))
    exposed = [f for r in rep["reports"]["A"] for f in r["feints"] if f["exposed"]]
    assert exposed[0]["order_id"] == b.orders[0].id
    assert b.orders[0].id not in str(rep["reports"]["A"][0]["facts"])           # never shown to the reader


# ---------------------------------------------------------------- false entries

def test_false_entries_cost_cp_reserve_units_and_never_move(rs):
    st = board(rs)
    res = submit_orders(st, rs, "A", SeatOrders(), [{**mv("A1", "B1", 2), "feint": True}, mv("A1", "A2", 2),
                                                    {"kind": "fortify", "province": "A1"}], "actions")
    assert [c.ok for c in res.results] == [True, False, True]
    assert res.results[1].reason == "NOT_ENOUGH_UNITS"           # the false entry holds its units
    b = res.book
    assert b.cp_spent == 1 and cp_left(st, "A", b) == 2
    assert "feint" in b.orders[0].to_dict() and "feint" not in b.orders[1].to_dict()   # older books hash as before
    big = board(rs, {"A1": {MIL: 6}})
    many = submit_orders(big, rs, "A", SeatOrders(), [{**mv("A1", "C1", 1), "feint": True}] * 4, "actions")
    assert [c.reason for c in many.results] == [None, None, None, "NOT_ENOUGH_CP"]
    s1, _ = esp.recon(st, {"A": b}, rs, 1)
    after, report = resolve(s1, {"A": b}, rs, 1)
    assert after.provinces["A1"].units == {MIL: 3} and after.provinces["A1"].fortified    # stayed home, fortify held
    assert kinds(report, "feint")[0]["seats"] == ["A"]
    assert not any(e["kind"] == "feint" for e in report.by_seat["B"])
    back = cancel_order(st, rs, "A", b, b.orders[0].id, "actions")
    assert back.ok and back.book.cp_spent == 0


def test_false_entry_to_real_in_p2_is_a_hasty_new_attack_and_back_is_invalid(rs):
    st = board(rs)
    b = book(st, rs, "A", [{**mv("A1", "B1", 2), "feint": True}, mv("A1", "A2", 1)])
    s1, _ = esp.recon(st, {"A": b}, rs, 1)
    assert cp_left(s1, "A", b) == 2
    fid, rid = b.orders[0].id, b.orders[1].id
    real = amend_order(s1, rs, "A", b, fid, {"feint": False}, P2)
    assert real.ok and real.cp_cost == 2 and real.order.hasty and not real.order.feint
    assert cp_left(s1, "A", real.book) == 0                       # 3 CP in all: 1 for the entry, 2 to make it real
    assert amend_order(s1, rs, "A", b, rid, {"feint": True}, P2).reason == "INVALID"
    assert check_order(s1, rs, "A", b, {**mv("A1", "C1", 1), "feint": True}, P2).reason == "PHASE_LOCKED"
    cancel = cancel_order(s1, rs, "A", b, fid, P2)
    assert cancel.ok and cancel.cp_cost == 1 and cp_left(s1, "A", cancel.book) == 1
    in_d = amend_order(st, rs, "A", b, fid, {"feint": False}, "actions")
    assert in_d.ok and in_d.book.cp_spent == 0


# ---------------------------------------------------------------- fog and views

def test_other_seats_never_see_shades_reports_or_skills(rs):
    st = board(rs)
    give_shade(st, "A")
    a = book(st, rs, "A", [spy(1, "observe", "B2", 2)])
    new, rep = esp.recon(st, {"A": a}, rs, 2)
    for seat in "BC":
        view = view_state(new, rs, seat)
        own = view["you"].pop("espionage")        # the seat's own Shades only (bots, programs): none here
        assert own["shades"] == [] and own["whisper"] >= 1
        text = repr(view)
        assert "shades" not in text and "spy_seq" not in text and "s1" not in text
    assert "s1" in esp.spies_text(new, rs, "A", rep) and "S1 C1" in esp.spies_text(new, rs, "A", rep)
    assert "s1" not in esp.spies_text(new, rs, "B", rep)
    assert len(esp.spies_text(new, rs, "A", rep)) <= 600


def test_systems_off_keep_the_old_recon_and_hashes():
    rs = ruleset("tri_plain")
    st = board(rs)
    new, rep = esp.recon(st, {}, rs, 1)
    assert rep["rounds"] == [] and new.phase == P2
    new.phase = st.phase
    assert state_hash(new) == state_hash(st)


# ---------------------------------------------------------------- the game: journal, inbox, replay

SEATS = [{"seat": s, "nick": f"Ник{s}", "kind": "human", "faction": "iron"} for s in "ABC"]


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


def _ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)))


def test_espionage_game_journals_delivers_and_replays(tmp_path):
    config = make_config(game_id="sp", scenario="tri_all", seats=SEATS, seed=4, pack_dirs=PACKS)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, Clock())
    assert "list_spies" in game.phase_info("A")["allowed"]
    homes = {s: game.state.players[s].home for s in "ABC"}
    _ready_all(game)                                                        # П1 -> Д (turn 1)
    game.submit("A", [{"kind": "hire_shade"}])
    game.submit("B", [{"kind": "hire_shade"}])
    _ready_all(game)                                                        # Д -> разведсрез -> П2
    assert game.phase == P2 and len(game.state.players["A"].shades) == 1
    from dah.game import GameError
    with pytest.raises(GameError) as locked:
        game.cancel("A", game._book("A").orders[0].id)
    assert locked.value.code == "PHASE_LOCKED"
    game.send_gift("A", "НикB", {"provisions": 1})                           # an op of P2 of turn 1
    _ready_all(game)                                                        # расчёт хода 1
    _ready_all(game)                                                        # П1 -> Д (turn 2)
    b_forge = next(p.id for p in game.state.owned("B") if p.id != homes["B"])
    game.submit("A", [spy("s1", "observe", b_forge, 2), {**mv(homes["A"], homes["B"], 1), "feint": True}])
    game.submit("B", [spy("s1", "ledger", "НикA", 1)])
    _ready_all(game)                                                        # разведсрез хода 2
    text = game.state_text("A")
    assert "ДОНЕСЕНИЯ:" in text
    intel = game.get_intel("A")
    assert intel["turn"] == 2
    assert game.list_spies("A")["text"].startswith("S1 C1 · Теней 1/2")
    assert "spy s1 «Наблюдение»" in game.orders("A")["text"] and "ЛОЖНАЯ ЗАПИСЬ" in game.orders("A")["text"]
    feint_id = next(o.id for o in game._book("A").orders if o.feint)
    game.amend("A", feint_id, {"feint": False})                             # made real in P2: 2 CP, hasty
    est = game.estimate_mission("A", {"mission": "observe", "target": b_forge, "rounds": 1})
    assert "раунды" in est["text"]
    msgs = [m for m in game.inbox("A")["messages"] if m.get("event") == "recon"]
    assert msgs and msgs[-1]["text"].startswith("[ИГРА]") and len(msgs[-1]["text"]) <= 200
    game.send_gift("B", "НикA", {"provisions": 1})                           # an op of P2, after the recon step
    _ready_all(game)                                                        # расчёт хода 2
    events = read_events(gdir.journal_path)
    types = {e["type"] for e in events}
    assert {"recon", "shade_hired", "mission_started", "spy_round", "feint", "intel_read"} <= types
    assert any(e["type"] == "feint" and e.get("converted_in_p2") for e in events)
    recon_events = [e for e in events if e["type"] == "recon"]
    assert all("state_hash" in e and "recon_hash" in e for e in recon_events)
    assert gdir.load_turn("books_d", 2) is not None and gdir.load_turn("recon", 2)["hash"] == recon_events[-1][
        "recon_hash"]
    result = replay(gdir)
    assert result.ok and len(result.turns) == 2 and len(result.recons) == 2


def test_espionage_tools_refuse_without_espionage(tmp_path):
    config = make_config(game_id="np", scenario="tri_conquest", seats=SEATS, seed=4, pack_dirs=PACKS)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, Clock())
    assert "list_spies" not in game.phase_info("A")["allowed"]
    from dah.game import GameError
    with pytest.raises(GameError, match="шпионаж выключен"):
        game.list_spies("A")
    _ready_all(game)
    _ready_all(game)
    recon = [e for e in read_events(gdir.journal_path) if e["type"] == "recon"]
    assert recon == [{**recon[0], "turn": 1}] and "state_hash" not in recon[0]
