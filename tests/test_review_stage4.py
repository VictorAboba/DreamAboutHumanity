"""Review of stage 4 (scoring): regression tests for the verified findings.

ENGINE-1 tie rule texts, FOG-1 objectives salt and wide seed, LIVE-2 score stat labels, LIVE-4 own claims first,
LIVE-5 the research order in the book, SPEC-1/SPEC-6 great works for the delayed spectator, SPEC-4 the missing
SCORING.md 12.5/12.6/12.12 tests.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from dah.engine import SeatOrders, build_ruleset, new_game_state, resolve, rules_text, rules_topic
from dah.engine import diplomacy as dip
from dah.engine import scoring as sc
from dah.engine.catalog import describe, stat_text
from dah.engine.combat import Force, fight
from dah.engine.registry import SCORING_STATS
from dah.game import Game, make_config, replay
from dah.journal import read_events, view_events
from dah.store import init_game_dir
from conquest_helpers import dispose
from test_scoring import FACTIONS, MIL, PACKS, Clock, _ready_all, board, book, kinds, mv, ruleset

ROOT = Path(__file__).resolve().parents[1]


class _Even(random.Random):
    """An rng whose uniform() is always the midpoint: the ±10% roll is 1.0 for both sides, so equal forces tie."""

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


# ---------------------------------------------------------------- ENGINE-1 ties


def test_fight_tie_goes_to_side_b():
    rs = ruleset("tri_score")
    a, b = Force("A", {MIL: 3}), Force("B", {MIL: 3})
    res = fight(rs, a, b, _Even(0), b_defending=False, factions={"A": "t4:iron", "B": "t4:iron"})
    assert res.strength_a == res.strength_b and not res.a_won


def _resolver_class():
    import importlib
    return importlib.import_module("dah.engine.resolve")._Resolution


def test_tie_between_attackers_goes_to_the_challenger_and_on_an_edge_to_the_larger_id(monkeypatch):
    monkeypatch.setattr(_resolver_class(), "rng", lambda self, *a: _Even(0))
    rs = ruleset("tri_score")
    st = board(rs, {"A2": {MIL: 3}, "B2": {MIL: 3}, "A1": {MIL: 3}, "B1": {MIL: 3}, "C1": {MIL: 3}})
    _, rep = resolve(st, {"A": book(st, rs, "A", [mv("A2", "M", 3)]),
                          "B": book(st, rs, "B", [mv("B2", "M", 3)])}, rs, 1)
    fight_ = next(e for e in kinds(rep, "battle") if e.get("stage") == "attackers")
    champion, challenger = fight_["sides"]
    assert champion["strength"] == challenger["strength"] and challenger["won"] and not champion["won"]
    st = board(rs, {"A2": {MIL: 3}, "B2": {MIL: 3}, "A1": {MIL: 3}, "B1": {MIL: 3}, "C1": {MIL: 3}})
    _, rep = resolve(st, {"A": book(st, rs, "A", [mv("A2", "B2", 3)]),
                          "B": book(st, rs, "B", [mv("B2", "A2", 3)])}, rs, 1)
    edge = kinds(rep, "edge_battle")[0]
    assert edge["edge"] == ["A2", "B2"]
    ab, ba = edge["sides"]
    assert ab["strength"] == ba["strength"] and ba["won"] and ba["owner"] == "B"      # the move from B2 (larger id)


def test_tie_rule_text_matches_the_engine():
    rs = build_ruleset(None, "core:quad4")
    combat = rules_topic(rs, "combat")
    text = combat["text"] if isinstance(combat, dict) else combat
    assert "слабее до боя" in text and "большим id" in text
    assert "сильнее до боя" not in text and "меньшим id" not in text
    core = rules_text(rs)
    assert "Равная сила — победа защитника" in core or "равенство — защитнику" in core
    for doc in ("docs/ru/rules.md", "docs/en/rules.md", "docs/SLICE.md"):
        body = (ROOT / doc).read_text(encoding="utf-8")
        assert "меньшим id" not in body.replace("с меньшим id как «a»", "") and "smaller id" not in body


# ---------------------------------------------------------------- FOG-1 objectives salt


def test_objectives_salt_changes_the_deal_but_not_the_public_streams():
    rs = build_ruleset(None, "core:slice3")
    seats = [{"seat": s, "nick": s} for s in "abc"]
    plain = new_game_state(rs, seats, 460740)
    salted = new_game_state(rs, seats, 460740, objectives_salt=123456789)
    assert {p: v.owner for p, v in plain.provinces.items()} == {p: v.owner for p, v in salted.provinces.items()}
    deals = {tuple(tuple(sorted(o.items())) for p in st.players.values() for o in p.objectives)
             for st in (new_game_state(rs, seats, 460740, objectives_salt=s) for s in range(12))}
    assert len(deals) > 1                                   # the salt, not the seed alone, deals the objectives
    assert sc.deal_objectives(rs, 460740, ["a", "b", "c"]) == {s: p.objectives for s, p in plain.players.items()}


def test_make_config_keeps_salt_only_with_objectives_and_views_strip_it(tmp_path):
    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "human"} for s in "abc"]
    cfg = make_config(game_id="salt", scenario="core:blitz_score", seats=seats, seed=5, objectives_salt=99)
    assert cfg["objectives_salt"] == 99
    plain = make_config(game_id="nosalt", scenario="core:blitz_test", seats=seats, seed=5, objectives_salt=99)
    assert "objectives_salt" not in plain
    gdir, _ = init_game_dir(tmp_path, cfg)
    game = Game.create(gdir, Clock())
    expected = new_game_state(game.ruleset, [{"seat": s, "nick": f"Ник{s}"} for s in "abc"], 5, 99)
    assert {s: p.objectives for s, p in game.state.players.items()} == \
        {s: p.objectives for s, p in expected.players.items()}
    events = read_events(gdir.journal_path)
    created = next(e for e in view_events(events, seat="a") if e["type"] == "game_created")
    assert "seed" not in created and "objectives_salt" not in created["config"] and "seed" not in created["config"]
    assert replay(gdir).ok


def test_cli_default_seed_is_wide():
    src = (ROOT / "src" / "dah" / "cli.py").read_text(encoding="utf-8")
    assert "randbelow(10**6)" not in src and "secrets.randbits(63)" in src and "objectives_salt=" in src


# ---------------------------------------------------------------- LIVE-2 labels


def test_every_scoring_stat_has_a_russian_label():
    rs = build_ruleset(None, "core:quad4")
    for spec in SCORING_STATS:
        path = spec.pattern.replace("*", "core:monument")
        label = stat_text(rs, path)
        assert label != path and "score" not in label, path
    assert stat_text(rs, "building.core:monument.score") == "очки стройки «Монумент Предела»"


def test_no_core_bonus_text_shows_a_raw_score_path():
    rs = build_ruleset(None, "core:quad4")
    for kind, table in (("faction", rs.factions), ("artifact", rs.artifacts), ("tech", rs.techs)):
        for fid in table:
            d = describe(rs, kind, fid)
            assert "score." not in d["text"] and not any("score." in b for b in d.get("bonuses") or []), fid
    growth = describe(rs, "faction", "core:growth")
    assert any("очков войны с одной провинции" in b and "= 4" in b for b in growth["bonuses"])   # core 0.7.0


# ---------------------------------------------------------------- LIVE-4 own claims first


def test_own_claims_are_listed_before_others():
    from dah.engine.view import render_state_text
    rs = ruleset("tri_score_all")
    st = board(rs)
    st.claims = [{"seat": "B", "province": "C2", "until_turn": 9}, {"seat": "C", "province": "B2", "until_turn": 9},
                 {"seat": "B", "province": "M", "until_turn": 9}, {"seat": "C", "province": "A2", "until_turn": 9},
                 {"seat": "A", "province": "B1", "until_turn": 9}]
    line = next(x for x in render_state_text(st, rs, "A").splitlines() if x.startswith("ПРИТЯЗАНИЯ"))
    items = line.split(": ", 1)[1].split("; ")
    assert items[0].startswith("B1 — ТЫ") and items[1].startswith("A2 ")       # own, then on own land
    assert items[-1] == "+2"


# ---------------------------------------------------------------- LIVE-5 research order in the book


def test_research_order_in_book_is_shown_in_state_and_list_techs(tmp_path):
    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "human", "faction": "iron"} for s in "ABC"]
    cfg = make_config(game_id="res", scenario="tri_score", seats=seats, seed=3, pack_dirs=PACKS)
    game = Game.create(init_game_dir(tmp_path, cfg)[0], Clock())
    _ready_all(game)                                                        # П1 -> Д
    tech = next(t["id"] for t in game.list_techs("A")["techs"] if t["status"] == "available")
    assert "исследование не выбрано" in game.state_text("A")
    oid = game.submit("A", [{"kind": "research", "tech": tech}])["accepted"][0]["id"]
    name = game.ruleset.techs[tech].name
    text = game.state_text("A")
    assert f"исследование: «{name}» (приказ {oid}, начнётся в расчёте)" in text and "не выбрано" not in text
    techs = game.list_techs("A")
    assert techs["ordered"] == {"tech": tech, "order": oid} and "начнётся в расчёте" in techs["text"]
    assert "не выбрано" not in game.list_techs("A", full=True)["text"]
    assert "research_ordered" not in game.state_json("B")["you"]
    _ready_all(game)                                                        # Д -> П2: still from the book
    assert "начнётся в расчёте" in game.state_text("A")
    _ready_all(game)                                                        # resolved: active now
    text = game.state_text("A")
    assert f"исследуется «{name}»" in text and "начнётся в расчёте" not in text


# ---------------------------------------------------------------- SPEC-1 / SPEC-6 delayed spectator


def test_delayed_snapshot_marks_great_works_only(tmp_path):
    from dah.server import spectate
    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "human", "faction": "iron"} for s in "ABC"]
    cfg = make_config(game_id="sp", scenario="tri_score", seats=seats, seed=3, pack_dirs=PACKS)
    game = Game.create(init_game_dir(tmp_path, cfg)[0], Clock())
    homes = {s: game.state.players[s].home for s in "ABC"}
    forge = {s: next(p.id for p in game.state.owned(s) if p.id != homes[s]) for s in "ABC"}
    _ready_all(game)
    game.submit("A", [{"kind": "build", "province": forge["A"], "building": "spire"}])
    game.state.provinces[homes["B"]].buildings.append("t4:monument")
    game.state.provinces[homes["C"]].buildings.append("t4:barracks")
    _ready_all(game)
    _ready_all(game)                                                        # turn 1 resolved
    for _ in range(3):
        _ready_all(game)                                                    # turn 2 resolved: turn 1 is viewable
    snap = spectate.snapshot(game, spectate.GameCache(), 1, spectate.DELAYED)
    provs = {p["id"]: p for p in snap["provinces"]}
    assert provs[forge["A"]]["great_works"][0]["building"] == "t4:spire"
    assert "turns_left" in provs[forge["A"]]["great_works"][0]
    assert provs[homes["B"]]["great_works"] == [{"building": "t4:monument", "done": True}]
    assert "great_works" not in provs[homes["C"]] and "buildings" not in provs[homes["C"]]
    meta = spectate.meta(game, "dahw", spectate.DELAYED)
    assert meta["works"] and meta["objectives"] == sc.on(game.ruleset, "objectives")
    full = spectate.snapshot(game, spectate.GameCache(), 1, spectate.LIVE)
    assert not any("great_works" in p for p in full["provinces"])
    html = (ROOT / "src" / "dah" / "server" / "watch.html").read_text(encoding="utf-8")
    assert "великие стройки (начало и готовность)" in html and "sp.great_works" in html


# ---------------------------------------------------------------- SPEC-4: SCORING.md 12.5


def _work_province(rs, policy: str, to: str | None = None):
    st = board(rs, {"A2": {MIL: 6}, "B1": {MIL: 3}, "C1": {MIL: 3}})
    st.provinces["B2"].buildings = ["t4:spire"]
    orders = [mv("A2", "B2", 6), dispose("B2", policy, to=to)]
    return st, resolve(st, {"A": book(st, rs, "A", orders)}, rs, 1)


def test_raid_to_neutrals_and_liberation_to_a_third_seat_give_no_works():
    rs = ruleset("tri_score_all")
    st = board(rs, {"A2": {MIL: 6}, "C1": {MIL: 3}})
    st.provinces["B2"].buildings = ["t4:spire"]
    st.provinces["B2"].status["occupied"] = {"by": "B", "from": None, "since": 0}   # B's occupation of a neutral
    new, rep = resolve(st, {"A": book(st, rs, "A", [mv("A2", "B2", 6), dispose("B2", "raid")])}, rs, 1)
    fate = kinds(rep, "disposition")[0]
    assert new.provinces["B2"].owner is None and fate["owner_after"] is None
    assert all(sc.works(new, rs, s) == 0 for s in "ABC")
    _, (new, rep) = _work_province(rs, "liberate", to="НикC")
    assert new.provinces["B2"].owner == "C" and new.provinces["B2"].status.get("occupied")
    assert all(sc.works(new, rs, s) == 0 for s in "ABC")


# ---------------------------------------------------------------- SPEC-4: SCORING.md 12.6


def test_attackers_fight_credits_both_in_full_and_three_sides_pairwise():
    rs = ruleset("tri_score")
    st = board(rs, {"A2": {MIL: 5}, "B2": {MIL: 4}, "C2": {MIL: 3}, "A1": {MIL: 1}, "B1": {MIL: 1}, "C1": {MIL: 1}})
    _, rep = resolve(st, {s: book(st, rs, s, [mv(f"{s}2", "M", n)]) for s, n in (("A", 5), ("B", 4), ("C", 3))},
                     rs, 1)
    fights = [e for e in kinds(rep, "battle") if e.get("stage") == "attackers"]
    pairs = {frozenset(x["owner"] for x in f["sides"]) for f in fights}
    assert len(fights) == 2 and all(len(p) == 2 for p in pairs)
    credits = kinds(rep, "war_credit")
    assert credits and all(not e["defender"] for e in credits)
    assert {frozenset((e["killer"], e["victim"])) for e in credits} <= pairs
    for e in credits:                                                       # each side's own losses
        f = next(f for f in fights if {x["owner"] for x in f["sides"]} == {e["killer"], e["victim"]})
        assert e["units"] == next(x for x in f["sides"] if x["owner"] == e["victim"])["losses"] and e["value"] > 0


def test_revolt_and_retreat_lost_give_no_war_credit():
    rs = ruleset("tri_score_all")
    st = board(rs, {"A2": {MIL: 3}})
    st, rep = resolve(st, {"A": book(st, rs, "A", [mv("A2", "B2"), dispose("B2", "liberate", to="НикC")])}, rs, 1)
    st, rep = resolve(st, {}, rs, 1)
    assert kinds(rep, "occupation_revolt") and not kinds(rep, "war_credit")
    # A attacks B1 from A1 and loses; C takes the emptied A1 meanwhile: A's survivors have no home left
    st = board(rs, {"A1": {MIL: 3}, "B1": {MIL: 2}, "C1": {MIL: 3}})
    st.provinces["A2"].owner = "C"
    st, rep = resolve(st, {"A": book(st, rs, "A", [mv("A1", "B1", 3)]),
                           "C": book(st, rs, "C", [mv("C1", "A1", 3)])}, rs, 1)
    lost = kinds(rep, "retreat_lost")
    assert lost and lost[0]["seats"] == ["A"] and sum(lost[0]["units"].values()) > 0
    battle = next(e for e in kinds(rep, "battle") if e["province"] == "B1")
    a_side = next(x for x in battle["sides"] if x["owner"] == "A")
    on_a = [e for e in kinds(rep, "war_credit") if e["victim"] == "A"]
    assert [e["units"] for e in on_a] == [a_side["losses"]]                 # the battle losses only, not the lost
    assert not any(e["killer"] == "C" for e in kinds(rep, "war_credit"))


def test_pact_break_fight_is_credited():
    rs = ruleset("tri_score")
    st = board(rs, {"A2": {MIL: 6}, "B2": {MIL: 2}, "C1": {MIL: 3}})
    op = dip.make_proposal(st, "A", "B", None, "negotiation_1")
    dip.apply_op(st, op)
    dip.apply_op(st, dip.make_response(st, "B", op["id"], "accept", "negotiation_1"))
    _, rep = resolve(st, {"A": book(st, rs, "A", [{**mv("A2", "B2", 6), "pact": "break"}])}, rs, 1)
    assert kinds(rep, "pact_broken") and kinds(rep, "battle")
    assert any(e["killer"] == "A" and e["victim"] == "B" for e in kinds(rep, "war_credit"))


# ---------------------------------------------------------------- SPEC-4: SCORING.md 12.12 on blitz_score


def test_blitz_score_end_to_end(tmp_path):
    from dah.engine import espionage as esp
    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "human"} for s in "abc"]
    cfg = make_config(game_id="bs", scenario="core:blitz_score", seats=seats, seed=41, objectives_salt=7)
    gdir, _ = init_game_dir(tmp_path, cfg)
    game = Game.create(gdir, Clock())
    setup = game.get_setup("a")
    assert setup["objectives"] and "Твоя тайная цель" in setup["text"]
    dealt = {s: list(p.objectives) for s, p in game.state.players.items()}
    _ready_all(game)                                                        # setup closes: the same deal
    assert {s: p.objectives for s, p in game.state.players.items()} == dealt
    st, rs = game.state, game.ruleset
    assert (st.provinces["P01"].owner, st.provinces["P09"].owner) == ("a", "b")      # seed 41's starts
    plan = {1: {"a": [{"kind": "move", "from": "P01", "to": "P04", "units": {"assault": 3}}],
                "b": [{"kind": "move", "from": "P09", "to": "P12", "units": {"assault": 4}}]},
            2: {"a": [{"kind": "move", "from": "P04", "to": "P13", "units": {"assault": 3}}],
                "b": [{"kind": "move", "from": "P12", "to": "P13", "units": {"assault": 4}}]}}
    built = False
    for turn in (1, 2, 3, 4, 5):
        _ready_all(game)                                                    # П1 -> Д
        for seat, orders in plan.get(turn, {}).items():
            res = game.submit(seat, orders)
            assert res["accepted"] and not res["rejected"], res
        if not built:                                                       # the Monument once a can pay for it
            res = game.submit("a", [{"kind": "build", "province": "P01", "building": "monument"}])
            built = bool(res["accepted"])
        _ready_all(game)
        _ready_all(game)                                                    # turn resolved
        if turn == 2:
            # a spy's ledger reads b's exact score by source, without the objective
            facts, truth = esp._ledger(game.state, rs, game.state.turn, "a", "b", SeatOrders(), 0.3)
            assert set(truth["score_parts"]) == {"territory", "science", "works", "war", "intel"}   # core 0.7.0
            assert truth["score_parts"] == sc.score_parts(game.state, rs)["b"]
            assert facts["score_parts"] == truth["score_parts"]
            others = [p for p in game.state_json("a")["players"] if p["seat"] != "a"]
            assert others and not any(p["score_exact"] for p in others)
    assert built
    while not game.game_over:
        _ready_all(game)
    events = read_events(gdir.journal_path)
    types = [e["type"] for e in events]
    # core 0.7.0 (decision 27): war scores per province taken from a rival, killed units give nothing
    assert "work_started" in types and "work_completed" in types and "war_credit" not in types
    assert types.count("objectives_revealed") == 3
    over = next(e for e in events if e["type"] == "game_over")
    assert {"score_parts", "objectives", "winners_open"} <= set(over)
    assert over["score_parts"]["a"]["works"] >= 3 or not game.state.players["a"].alive
    assert replay(gdir).ok
