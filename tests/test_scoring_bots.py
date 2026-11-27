"""Stage 4, step 4 (docs/SCORING.md sections 9-10): the bots pursue their objective and the score sources by style,
the simulator's scoring flags and corridors, the spectator's score by source and objectives, the report by source."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from dah.bots import BuilderBot, DiplomatBot, RushBot
from dah.bots import score as bs
from dah.bots import view as bv
from dah.bots.tactics import plan_moves
from dah.bots.view import parse_state
from dah.engine import build_ruleset, new_game_state, view_state
from dah.game import Game, make_config
from dah.journal import read_events
from dah.report import _gift_share, build_report, render_report
from dah.store import init_game_dir
from test_review_stage2 import FakeClock, _play_out

ROOT = Path(__file__).resolve().parents[1]
ME = {"A"}
PARTS = {"territory": 7, "science": 0, "works": 0, "war": 0}


def province(pid: str, ptype: str, owner: str | None, units: Any, neighbors: list[str], **extra: Any) -> dict:
    return {"id": pid, "name": pid, "type": ptype, "owner": owner, "units": units, "neighbors": neighbors,
            "buildings": extra.pop("buildings", []), **extra}


def scored_state(turn: int = 3, provisions: int = 120, alloy: int = 60, objectives: list | None = None,
                 parts: dict | None = None, net_food: int = 4, **you_extra: Any) -> dict:
    """A small map: A holds P01 (capital, border) and P02 (forge, interior behind P01) and P03 (agri, border to B)."""
    provinces = [
        province("P01", "capital", "A", {"assault": 3}, ["P02", "P07"], buildings=["barracks"], origin="A"),
        province("P02", "forge", "A", {"assault": 1}, ["P01", "P03"], buildings=["mine"], origin="A"),
        province("P03", "agri", "A", {"assault": 2}, ["P02", "P07", "P08"], origin="A"),
        province("P07", "forge", "B", {"assault": 1}, ["P01", "P03", "P08"], origin="B"),
        province("P08", "capital", "B", {"assault": 2}, ["P03", "P07"], origin="B"),
    ]
    you: dict[str, Any] = {"seat": "A", "faction": "core:covenant", "command_points": 3,
                           "resources": {"alloy": alloy, "provisions": provisions},
                           "net_income": {"alloy": 10, "provisions": net_food},
                           "score_parts": dict(PARTS if parts is None else parts), "objectives": objectives or [],
                           "war": {"points": 0, "cap_left": 5}, **you_extra}
    state = {"turn": turn, "turn_limit": 10, "phase": "actions", "you": you, "provinces": provinces,
             "players": [{"seat": s, "alive": True, "score": 5} for s in ("A", "B")]}
    return {"ok": True, "state": state}


def estimate_all(value: float):
    def estimate(attacker, defender, ptype, fortified, hasty):
        return value
    return estimate


def objective(kind: str, params: dict, **extra: Any) -> dict:
    return {"id": f"core:{kind}", "kind": kind, "params": params, "met_now": False, "have": 0, "need": 1, **extra}


# ---------------------------------------------------------------- numbers and the adapter

def test_great_work_numbers_of_the_bots_match_core():
    rs = build_ruleset(None, "slice3")
    for sid, (turns, points, types) in bv.GREAT_WORKS.items():
        bd = rs.buildings[f"core:{sid}"]
        assert (turns, points) == (bd.build_turns, bd.score)
        assert types == (tuple(t.split(":")[-1] for t in bd.allowed_types) or None)
        assert bv.BUILDING_COST_FOOD[sid] == rs.building_cost(f"core:{sid}")["core:provisions"]
        assert bv.BUILDING_COST_ALLOY[sid] == rs.building_cost(f"core:{sid}")["core:alloy"]


def test_parse_state_reads_the_own_score_objectives_and_war():
    rs = build_ruleset(None, "core:slice3")
    seats = [{"seat": s, "nick": f"N{s}"} for s in "ABC"]
    st = new_game_state(rs, seats, 3)
    view = parse_state({"state": view_state(st, rs, "A", None)}, {"A", "NA"})
    assert view.scoring("territory") and view.scoring("works") and view.scoring("war")
    assert not view.scoring("objectives")       # the objective is not a turn source: it is in view.objectives
    assert len(view.objectives) == 1 and view.objectives[0]["kind"] and "params" in view.objectives[0]
    # core 0.7.0: the war scale per province (at most 8 in all)
    # core 0.7.1: `reclaim` lists the provinces owned earlier that pay no war points when taken back (none yet)
    assert view.war == {"points": 0, "cap_left": 8, "per_province": 3, "hold_turns": 2, "provinces": {}, "reclaim": []}
    plain = build_ruleset(None, "core:blitz_test")
    st = new_game_state(plain, seats, 3)
    view = parse_state({"state": view_state(st, plain, "A", None)}, {"A", "NA"})
    assert view.score_parts is None and view.objectives == () and not view.scoring("works")


def test_building_now_is_read_per_province():
    payload = scored_state()
    payload["state"]["provinces"][0]["building_now"] = [{"building": "core:monument", "turns_left": 2}]
    view = parse_state(payload, ME)
    assert view.provinces["P01"].building_now == ("monument",) and bs.has_work(view, "monument")


# ---------------------------------------------------------------- objectives steer the attacks

def test_attack_bonus_marks_the_provinces_that_advance_the_objective():
    view = parse_state(scored_state(objectives=[objective("hold_type", {"type": "core:forge", "count": 2})]), ME)
    assert bs.attack_bonus(view) == {"P07": bs.OBJECTIVE_BONUS}
    view = parse_state(scored_state(objectives=[objective("rival_capital_taken", {}, target="B")]), ME)
    assert bs.attack_bonus(view) == {"P08": bs.OBJECTIVE_BONUS}
    view = parse_state(scored_state(objectives=[objective("capital_other", {"count": 1})]), ME)
    assert set(bs.attack_bonus(view)) == {"P08"}
    met = objective("hold_type", {"type": "core:forge", "count": 2}) | {"met_now": True}
    view = parse_state(scored_state(objectives=[met]), ME)
    assert bs.attack_bonus(view) == {}
    # rush: every province of another seat while the war cap is not reached
    view = parse_state(scored_state(), ME)
    assert bs.attack_bonus(view, war=True) == {"P07": bs.WAR_BONUS, "P08": bs.WAR_BONUS}
    view = parse_state(scored_state(war={"points": 5, "cap_left": 0}), ME)
    assert bs.attack_bonus(view, war=True) == {}
    plain = parse_state(scored_state(parts={"territory": 7}), ME)       # war off
    assert bs.attack_bonus(plain, war=True) == {}


def test_plan_moves_takes_a_bonus_target_below_the_threshold():
    import random
    view = parse_state(scored_state(), ME)
    assert not [m for m in plan_moves(view, estimate_all(0.55), 0.6, random.Random(1)) if m["to"] in ("P07", "P08")]
    moves = plan_moves(view, estimate_all(0.55), 0.6, random.Random(1), bonus={"P07": 0.1})
    assert {m["to"] for m in moves if m["from"] in ("P01", "P03")} == {"P07"}


def test_objective_garrison_keeps_one_unit_where_the_objective_is_held():
    view = parse_state(scored_state(objectives=[objective("hold_type", {"type": "core:forge", "count": 2})]), ME)
    assert bs.objective_garrison(view) == {"P02": {"assault": 1}}
    assert bs.objective_garrison(parse_state(scored_state(), ME)) == {}


def test_research_puts_the_objective_branch_first():
    techs = {"enabled": True, "techs": [
        {"id": "core:t_ind", "branch": "core:industry", "tier": 1, "cost": 5, "status": "available"},
        {"id": "core:t_war", "branch": "core:war", "tier": 1, "cost": 5, "status": "available"}]}
    bot = BuilderBot("A", 1)
    view = parse_state(scored_state(techs=[], researching=None), ME)
    assert bot.research_order(view, techs) == {"kind": "research", "tech": "core:t_ind"}
    view = parse_state(scored_state(techs=[], researching=None,
                                    objectives=[objective("techs_branch", {"branch": "core:war", "count": 3})]), ME)
    assert bot.research_order(view, techs) == {"kind": "research", "tech": "core:t_war"}


# ---------------------------------------------------------------- great works

def test_work_order_picks_the_most_protected_allowed_site():
    view = parse_state(scored_state(), ME)
    order = bs.work_order(view, "monument", 120, 60, 3)
    assert order == {"kind": "build", "province": "P02", "building": "monument"}     # interior, no foreign neighbour
    spire = bs.work_order(view, "dream_spire", 200, 60, 3, capital=False)
    assert spire is not None and spire["province"] in ("P02", "P03")
    # core 0.7.0: paid in parts (13+5 now, 12+5 at the next build step from the rest and the net income +4/+10)
    assert bs.work_order(view, "monument", 24, 60, 3) is not None      # 24 - 13 + 4 covers the second share
    assert bs.work_order(view, "monument", 13, 60, 3) is None          # the second share would stall
    assert bs.work_order(view, "monument", 120, 4, 3) is None          # alloy short of the first share (5)
    late = parse_state(scored_state(turn=10), ME)
    assert bs.work_order(late, "monument", 120, 60, 3) is None         # could not be ready by the end
    off = parse_state(scored_state(parts={"territory": 7}), ME)
    assert bs.work_order(off, "monument", 120, 60, 3) is None          # works off
    built = scored_state()
    built["state"]["provinces"][2]["buildings"] = ["monument"]
    assert bs.work_order(parse_state(built, ME), "monument", 120, 60, 3) is None   # one per owner


def test_saving_rate_spreads_the_price_over_the_turns_left():
    # core 0.7.0, paid in parts: the stock must cover the first share (13) and, with the net income, the second (12)
    view = parse_state(scored_state(turn=3, provisions=5, net_food=5), ME)
    assert bs.saving_rate(view, "monument", 3) == 3            # 15 more over turns 4..9
    poor = parse_state(scored_state(turn=3, provisions=0, net_food=5), ME)
    assert bs.saving_rate(poor, "monument", 3) == 4            # 20 over turns 4..9
    assert bs.saving_rate(parse_state(scored_state(turn=3, provisions=20, net_food=5), ME), "monument", 3) is None
    assert bs.saving_rate(parse_state(scored_state(turn=9, provisions=20), ME), "monument", 3) is None   # too late
    assert bs.saving_rate(parse_state(scored_state(provisions=80), ME), "monument", 3) is None   # affordable now
    assert bs.saving_rate(parse_state(scored_state(provisions=20, parts={"territory": 7}), ME), "monument", 3) is None


def test_builder_lays_the_monument_and_diplomat_too_rush_only_for_its_objective():
    view = parse_state(scored_state(), ME)
    builds = [o for o in BuilderBot("A", 1).action_orders(view, estimate_all(0.0)) if o["kind"] == "build"]
    assert {"kind": "build", "province": "P02", "building": "monument"} in builds
    diplomat = DiplomatBot("A", 1)
    builds = [o for o in diplomat.action_orders(view, estimate_all(0.0)) if o["kind"] == "build"]
    assert any(o["building"] == "monument" for o in builds)
    rush = RushBot("A", 1)
    assert not [o for o in rush.action_orders(view, estimate_all(0.0)) if o.get("building") == "monument"]
    wants = parse_state(scored_state(objectives=[objective("great_work", {"count": 1})]), ME)
    assert [o for o in rush.action_orders(wants, estimate_all(0.0)) if o.get("building") == "monument"]


def test_a_saving_builder_recruits_only_what_income_leaves():
    view = parse_state(scored_state(turn=3, provisions=5, net_food=9, alloy=200), ME)
    assert bs.saving_rate(view, "monument", 3) == 2
    orders = BuilderBot("A", 1).action_orders(view, estimate_all(0.0))
    recruits = sum(o["count"] for o in orders if o["kind"] == "recruit")
    assert recruits <= 9 - bs.saving_rate(view, "monument", 3)


# ---------------------------------------------------------------- a scored game: report and spectator

@pytest.fixture(scope="module")
def scored_game(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scored")
    clock = FakeClock()
    seats = [{"seat": "A", "nick": "Ар", "kind": "agent"}, {"seat": "B", "nick": "Бо", "kind": "agent"}]
    config = make_config(game_id="g4", scenario="core:slice2", seats=seats, seed=11, turn_limit=3)
    config.pop("setup", None)
    config["phase_limits"].pop("setup", None)
    gdir, _ = init_game_dir(tmp, config)
    game = Game.create(gdir, clock)
    _play_out(game, clock)
    return game, gdir


def test_report_prints_the_score_by_source_per_seat_and_turn_and_the_objectives(scored_game):
    game, gdir = scored_game
    report = build_report(read_events(gdir.journal_path))
    sc = report["scoring"]
    assert {"gift_share_of_score_spend", "defense_share", "seats", "works", "war_pairs"} <= set(sc)
    for seat, row in sc["seats"].items():
        assert row["objectives"] and {"met", "points", "evidence", "counted"} <= set(row["objectives"][0])
    text = render_report(report)
    turns = text.split("ХОДЫ", 1)[1]
    assert "[зем " in turns and "нау " in turns
    score_part = text.split("ОЧКИ", 1)[1]
    assert "+5 " in score_part and ("✓" in score_part or "✗" in score_part) and "/" in score_part


def test_gift_share_counts_gifts_of_the_last_three_turns_once():
    events = [
        {"type": "gift", "seat": "B", "to": "A", "resources": {"core:provisions": 30}, "turn": 1},
        {"type": "gift", "seat": "B", "to": "A", "resources": {"core:alloy": 10}, "turn": 5},
        {"type": "work_started", "seat": "A", "building": "core:monument", "turn": 2,
         "cost": {"core:provisions": 60, "core:alloy": 20}},
        {"type": "tech_completed", "seat": "A", "tech": "core:x", "turn": 9, "cost": 20},
        {"type": "work_started", "seat": "B", "building": "core:monument", "turn": 3},     # no cost: old journal
    ]
    share = _gift_share(events, ["A", "B"])
    assert share == {"A": round(30 / 100, 3), "B": None}


def test_journal_writes_what_works_and_techs_cost(scored_game):
    _, gdir = scored_game
    for e in read_events(gdir.journal_path):
        if e["type"] in ("work_started", "tech_completed"):
            assert "cost" in e


def test_spectator_shows_the_score_by_source_and_the_objectives_only_at_the_end(scored_game):
    from dah.server import spectate
    game, _ = scored_game
    cache = spectate.GameCache()
    mid = spectate.snapshot(game, cache, 1, spectate.DELAYED)
    assert all(p["objectives"] == "?" for p in mid["players"])
    assert all(set(p["score_parts"]) >= {"territory", "science", "works", "war"} for p in mid["players"])
    last = spectate.resolved_turns(game)
    end = spectate.snapshot(game, cache, last, spectate.REVEALED)
    result = game.state.result
    for p in end["players"]:
        assert p["score_parts"] == result["score_parts"][p["seat"]] and "objectives" in p["score_parts"]
        assert p["score"] == result["scores"][p["seat"]]
        assert isinstance(p["objectives"], list) and p["objectives"][0]["kind"]
    live = spectate.live(game, cache, "spectator", spectate.REVEALED)
    assert all(h["by_source"] for h in live["scores_history"]) and live["result"]["objectives"]


# ---------------------------------------------------------------- simulator

def _simulate_module():
    spec = importlib.util.spec_from_file_location("dah_simulate_s4", ROOT / "scripts" / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate_s4"] = module
    spec.loader.exec_module(module)
    return module


def test_simulator_compares_land_and_the_scenario_weights_with_corridors(tmp_path, capsys):
    sim = _simulate_module()
    out = tmp_path / "sim.json"
    assert sim.main(["--games", "2", "--config", "rush,builder,diplomat", "--scenario", "core:blitz_score",
                     "--scoring", "both", "--json-out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "corridors (SCORING.md 10" in text and "passive probe" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    land = next(v for k, v in data.items() if k.endswith("scoring=land"))
    full = next(v for k, v in data.items() if k.endswith("scoring=scenario"))
    names = {r["name"] for r in full["corridors"]}
    assert {"mix: win share of any bot type", "leader after turn 3 wins", "territory share of the winner's score",
            "idle provisions at turn 8 (median)", "reveal_flip"} <= names
    assert any(n.startswith("objectives met") for n in names)
    assert sum(r["gate"] for r in full["corridors"]) <= 6
    scoring = full["ALL"]["scoring"]
    assert {"score_by_source_per_type", "score_by_source_per_faction", "winner_share_by_source", "objectives_met",
            "reveal_flip", "idle_median_turn8", "works", "war_defense_share", "no_centre_win_share",
            "battles_per_turn", "seats_attacked_share", "war_pairs_feeding"} <= set(scoring)
    assert set(scoring["score_by_source_per_type"]["rush"]) >= {"territory", "science", "works", "war", "objectives"}
    assert "passive_probe" in full and "passive_probe" not in land
    assert set(land["ALL"]["scoring"]["score_by_source_per_type"]["rush"]) == {"territory"}
    tracked = next(r for r in full["corridors"] if r["name"].startswith("no centre"))
    assert tracked["value"]["land"] == land["ALL"]["scoring"]["no_centre_win_share"]


def test_simulator_weight_sets():
    sim = _simulate_module()
    rs = build_ruleset(None, "core:slice3")
    assert sim.scoring_sets(rs, None) == [("scenario", None)]
    assert sim.scoring_sets(rs, ["all", "land"]) == [("land", {"territory": 1}),
                                                    ("all", dict.fromkeys(sim.SCORE_SOURCES, 1))]
    assert sim.scoring_sets(rs, ["territory=1,war=2"]) == [("territory=1,war=2", {"territory": 1, "war": 2})]
    with pytest.raises(ValueError):
        sim.scoring_sets(rs, ["nonsense=1"])
