"""Core 0.8.0, espionage v2 step C (docs/SPY_V2.md 9-11): the rules gate and the bridge's mission description, the
bots' deep missions (rush: sabotage site, the Observation of its least sure attack, the P2 review by
estimate(defender="rN"); builder: Read Orders at its borders and the Ward; diplomat: the partner's orders and the
public warning), the simulator's spy gates and `--spy-base`, the report's `spy_v2` block and the new journal entries."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dah.bots import BuilderBot, DiplomatBot, RushBot
from dah.bots import spy
from dah.bots import view as bv
from dah.bots.view import parse_state
from dah.bridge.tools import TOOLS
from dah.engine import build_ruleset
from dah.engine import espionage as esp
from dah.journal import view_events
from dah.report import build_report, render_report
from dah.report_spy import spy_v2

from test_stage3_bots import ME, make_state, province

ROOT = Path(__file__).resolve().parents[1]


def _sim():
    spec = importlib.util.spec_from_file_location("dah_simulate_c", ROOT / "scripts" / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate_c"] = module
    spec.loader.exec_module(module)
    return module


def estimate_all(value: float):
    def estimate(attacker, defender, ptype, fortified, hasty):
        return value
    return estimate


# ---------------------------------------------------------------- rules (section 9)

def test_the_bridge_names_what_observe_and_read_orders_give():
    spec = next(t for t in TOOLS if t.name == "submit_orders")
    text = json.dumps(spec.schema, ensure_ascii=False)
    assert "кто будет защищать провинцию" in text and "удары по тебе и нарушения пактов" in text


def test_report_defender_is_the_servers_header():
    report = {"id": "r3", "turn": 4, "facts": {"province": "P07", "units": {"core:assault": 3},
                                               "planned": {"recruit": {"core:assault": 2}, "fortify": True}}}
    d = esp.report_defender(report, 5)
    assert d["units"] == {"core:assault": 5} and d["fortified"] and d["province"] == "P07"
    assert d["header"].startswith("по донесению r3: состав конца Д хода 4") and "устарело на 1 ход" in d["header"]
    assert "найм этого хода учтён" in d["header"]


# ---------------------------------------------------------------- bots (section 10)

def test_the_bots_know_the_deep_missions_of_the_pack():
    assert [m for m in bv.MISSIONS if spy.deep(m)] == ["secret", "read_orders", "sabotage"]
    rs = build_ruleset(None, "core:quad4")
    assert {m.rsplit(":", 1)[-1]: md.intel for m, md in rs.missions.items()} == bv.MISSION_INTEL
    assert bv.WARD_COST_ALLOY == rs.espionage.ward_cost["core:alloy"]


def _rush_view(turn: int = 3, phase: str = "actions", **extra: Any):
    shades = [{"id": "s1", "status": "idle"}, {"id": "s2", "status": "idle"}]
    state = make_state(turn=turn, shades=shades, whisper=2, **extra)
    state["state"]["you"]["espionage"]["active_limit"] = 3
    state["state"]["phase"] = phase
    return parse_state(state, ME)


def test_rush_sabotages_a_rival_great_work_and_observes_its_attack():
    view = _rush_view()
    work = view.provinces["P07"]
    view.provinces["P07"] = replace(work, building_now=("monument",))
    bot = RushBot("A", 2)
    out = bot.action_orders(view, estimate_all(0.7))
    spies = [o for o in out if o["kind"] == "spy"]
    assert spies[0]["mission"] == "sabotage" and spies[0]["target"] == "P07"
    assert [o["mission"] for o in spies[1:]] == ["observe"]
    assert any(o["kind"] == "move" and o["to"] == "P07" for o in out)      # attacked the same turn


def test_rush_remembers_a_great_work_an_observation_showed():
    bot = RushBot("A", 2)
    bot.memory.absorb({"turn": 3, "reports": [
        {"id": "r1", "behavior": "observe_province", "facts": {"province": "P10", "units": {"core:assault": 2},
                                                               "building_now": [["core:monument", 2]]}},
        {"id": "r2", "behavior": "observe_province", "facts": {"province": "P08", "units": {},
                                                               "building_now": [["core:monument", 2]]}}]})
    assert bot.memory.works == {"P10": 4, "P08": 4} and bot.memory.report_on("P10", 3) == "r1"

    def view(turn: int):
        v = _rush_view(turn=turn)
        far = parse_state({"ok": True, "state": {"turn": turn, "phase": "actions", "you": {"seat": "A"},
                                                 "provinces": [province("P10", "forge", "B", None, ["P08"])]}},
                          ME).provinces["P10"]
        v.provinces["P10"] = far
        return v

    site = spy.rival_work_site(view(4), lambda p: True, bot.memory)
    assert site is not None and site.id == "P10"     # P08 is in sight: what the view shows there counts
    assert spy.rival_work_site(view(5), lambda p: True, bot.memory) is None    # no longer due


def test_rush_reviews_its_attack_in_p2_by_the_report():
    view = _rush_view(phase="negotiation_2")
    attack = {"id": "o1", "kind": "move", "from": "P02", "to": "P07", "units": {"core:assault": 2},
              "phase_added": "actions"}
    bot = RushBot("A", 2)
    bot.memory.observed["P07"] = (3, "r5")
    asked: list[tuple[Any, ...]] = []

    def weak(attacker, rid, hasty):
        asked.append((attacker, rid, hasty))
        return {"win_prob": 0.3, "win_prob_if_fortified": 0.1}

    assert bot.p2_review(view, [attack], 3, weak) == ["o1"]
    assert asked == [({"core:assault": 2}, "r5", False)] and bot.memory.decisions["cancel"] == 1
    assert bot.p2_review(view, [attack], 3, lambda a, r, h: {"win_prob": 0.95, "win_prob_if_fortified": 0.9}) == []
    assert bot._sure
    extra = bot.p2_orders(view, [attack], 3, estimate_all(0.9))
    assert extra and extra[0]["kind"] == "move" and bot.memory.decisions["hasty"] == 1    # a hasty attack besides
    old = RushBot("A", 2)       # no report this turn: no review, and the old rule (no hasty attack besides a move)
    assert old.p2_review(view, [attack], 3, weak) == [] and old.p2_orders(view, [attack], 3, estimate_all(0.9)) == []


def test_rush_researches_whisper_veil_only_with_espionage():
    techs = {"enabled": True, "techs": [{"id": "core:whisper_veil", "branch": "core:mystery", "status": "available",
                                         "tier": 1, "cost": 10},
                                        {"id": "core:march_drill", "branch": "core:logistics", "status": "available",
                                         "tier": 1, "cost": 10}]}
    on = parse_state(make_state(), ME)
    off = parse_state(make_state(espionage=False), ME)
    assert RushBot("A", 1).research_order(on, techs) == {"kind": "research", "tech": "core:whisper_veil"}
    assert RushBot("A", 1).research_order(off, techs) == {"kind": "research", "tech": "core:march_drill"}


def test_builder_sets_a_ward_at_its_great_work_against_a_spy_neighbour():
    state = make_state(shades=[{"id": "s1", "status": "mission", "mission": {"type": "core:ledger", "target": "B"}}])
    state["state"]["provinces"][1]["building_now"] = [{"building": "core:monument", "turns_left": 2}]
    view = parse_state(state, ME)
    bot = BuilderBot("A", 2)
    assert not [o for o in bot.action_orders(view, estimate_all(0.0)) if o["kind"] == "ward"]
    bot.memory.whisper["B"] = 2       # its ledger showed S2: sabotage is open to it
    wards = [o for o in bot.action_orders(view, estimate_all(0.0)) if o["kind"] == "ward"]
    assert wards == [{"kind": "ward", "province": "P02"}]


def test_builder_fortifies_where_a_read_book_attacks():
    bot = BuilderBot("A", 2)
    bot.memory.orders["B"] = (3, [{"kind": "move", "from": "P07", "to": "P02", "units": {"core:assault": 1}}])
    view = parse_state(make_state(phase="negotiation_2"), ME)
    assert bot.p2_orders(view, [], 3, estimate_all(0.0)) == [{"kind": "fortify", "province": "P02"}]


def test_diplomat_reads_its_partner_every_two_turns_and_warns_of_a_break():
    pacts = [{"seats": ["A", "B"], "id": "p1"}]
    idle = [{"id": "s1", "status": "idle"}, {"id": "s2", "status": "idle"}]
    state = make_state(shades=idle, pacts=pacts)
    state["state"]["you"]["espionage"]["active_limit"] = 2
    bot = DiplomatBot("A", 2)
    bot.memory.objectives["B"] = []
    bot.memory.objectives["C"] = []
    plans = bot._mission_plan(parse_state(state, ME), set())
    assert plans[0] == ("read_orders", "B", 1)
    bot.memory.read_turn["B"] = 2
    assert all(p[1] != "B" for p in bot._mission_plan(parse_state(make_state(turn=3, pacts=pacts), ME), set()))
    bot.memory.orders["B"] = (3, [{"kind": "move", "from": "P07", "to": "P01", "units": {"core:assault": 2},
                                   "pact": "break"}])
    p2 = parse_state(make_state(turn=3, phase="negotiation_2", pacts=pacts), ME)
    assert spy.breaches(p2, bot.memory) == [("B", "P01")]
    text = bot.message(p2)
    assert text and "нарушение пакта" in text and "B → P01" in text
    assert {"kind": "fortify", "province": "P01"} in bot.p2_orders(p2, [], 3, estimate_all(0.0))


# ---------------------------------------------------------------- simulator (section 10)

def test_the_simulator_prints_the_spy_gates(capsys, tmp_path):
    sim = _sim()
    rs = build_ruleset(None, "core:quad4")
    sim.sync_bot_numbers(rs)
    g = sim.play_game(rs, {"A": "rush", "B": "builder", "C": "diplomat"}, "c|0", sim.setup_config(rs, None, None, None))
    assert g["intel_cap"] == 3 and "core:monument" in g["great_works"]
    t = sim.SpyTally()
    t.add(g)
    sm = t.summary()
    assert sm["games"] == 1 and sm["deep_share"] is not None and (sm["read_orders_points_max_per_target"] or 0) <= 1
    rows = sim.spy_gates(t, {"rush": 0.5}, {"rush": 0.3})
    shift = next(r for r in rows if r["name"].startswith("win share shift"))
    assert shift["value"] == {"rush": 0.2} and shift["status"] == "FAIL"
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"ALL": {"win_per_seat": {"rush": 0.4}}}), encoding="utf-8")
    assert sim.spy_base(str(base)) == {"rush": 0.4} and sim.spy_base(None) is None
    assert sim.main(["--scenario", "core:quad4", "--games", "1", "--config", "rush,builder,diplomat",
                     "--spy-base", str(base)]) == 0
    out = capsys.readouterr().out
    assert "######## spy gates" in out and "spy v2: deep missions" in out


def test_no_spy_gates_without_espionage(capsys):
    sim = _sim()
    assert sim.main(["--scenario", "core:slice3", "--games", "1", "--config", "rush,builder,diplomat",
                     "--espionage", "off"]) == 0
    assert "spy gates" not in capsys.readouterr().out


# ---------------------------------------------------------------- report and journal (section 11)

def _journal() -> list[dict[str, Any]]:
    seats = [{"seat": s, "nick": f"Н{s}", "kind": "agent"} for s in "AB"]
    ev = [{"type": "game_created", "ts": 0, "config": {"game_id": "g", "scenario": "core:quad4", "seats": seats}},
          {"type": "shade_hired", "seat": "A", "shade": "s1", "turn": 0, "free": True},
          {"type": "shade_hired", "seat": "B", "shade": "s1", "turn": 0, "free": True}]
    for turn in (1, 2):
        ev.append({"type": "mission_started", "seat": "A", "shade": "s1", "mission": "core:observe", "target": "P07",
                   "rounds": 1, "turn": turn, "in_sight": turn == 2})
        ev.append({"type": "spy_round", "seat": "A", "shade": "s1", "mission": "core:observe", "turn": turn,
                   "C_loc": 2, "p_det": 0.25, "detected": False, "S": 1, "outcome": "found"})
        ev.append({"type": "intel_report", "seat": "A", "report_id": f"r{turn}", "mission": "core:observe",
                   "target": "P07", "turn": turn, "informativeness": 1.0})
        ev.append({"type": "mission_end", "seat": "A", "shade": "s1", "mission": "core:observe", "reason": "done",
                   "turn": turn})
    ev += [{"type": "estimate_report", "seat": "A", "report_id": "r1", "province": "P07", "turn": 1,
            "phase": "negotiation_2"},
           {"type": "orders_summary", "seat": "A", "turn": 2, "moves": [{"to": "P07", "owner": "B", "p2": True}],
            "fortify": []},
           {"type": "mission_started", "seat": "B", "shade": "s1", "mission": "core:read_orders", "target": "A",
            "rounds": 1, "turn": 3},
           {"type": "spy_round", "seat": "B", "shade": "s1", "mission": "core:read_orders", "turn": 3, "C_loc": 1,
            "p_det": 0.2, "detected": False, "S": 1, "outcome": "found"},
           {"type": "intel_report", "seat": "B", "report_id": "r1", "mission": "core:read_orders", "target": "A",
            "turn": 3, "informativeness": 1.0},
           {"type": "intel_point", "seat": "B", "target": "A", "mission": "core:read_orders", "points": 1, "total": 1,
            "kind": "once_per_target", "turn": 3},
           {"type": "orders_summary", "seat": "A", "turn": 3,
            "moves": [{"to": "P03", "owner": "B", "p2": False, "pact": "break"}], "fortify": []},
           {"type": "orders_summary", "seat": "B", "turn": 3, "moves": [],
            "fortify": [{"province": "P03", "p2": True}]},
           {"type": "build_sabotaged", "seat": "B", "province": "P04", "building": "core:monument", "turn": 3},
           {"type": "sabotage_effect", "seat": "A", "shade": "s2", "victim": "B", "effects": ["stall"],
            "intel_point": True, "turn": 3}]
    return ev


def test_spy_v2_block_of_the_report():
    block = spy_v2(_journal(), ["A", "B"])
    a, b = block["seats"]["A"], block["seats"]["B"]
    assert a["missions"] == {"observe": 2} and a["deep_share"] == 0.0 and a["needless_observe"] == 1
    assert a["decisions"] == {"attack_within_2": 2, "estimate_by_report": 1, "p2_same_turn": 1}
    assert a["expected_caught"] == 0.5 and a["caught"] == 0 and a["rounds_by_c"] == {"2": 2}
    assert a["sabotage"]["stall"] == 1 and a["sabotage"]["points"] == 1
    assert b["points_by_kind"] == {"once_per_target": 1} and b["read_s1"] == [0, 1] and b["deep_share"] == 1.0
    assert b["idle_shade_turns"] == 2           # its Shade sat out turns 1 and 2
    warned = block["game"]["warned_breaks"]
    assert warned == [{"seat": "A", "victim": "B", "province": "P03", "turn": 3, "reacted": True}]
    assert block["game"]["builds_stopped"][0]["unfinished"]
    names = {r["name"]: r for r in block["m10"]}
    assert names["связок «донесение → решение»"]["ok"] and names["лишних наблюдений на место"]["ok"]
    assert spy_v2([e for e in _journal() if e["type"] != "mission_started"], ["A", "B"]) is None


def test_the_report_renders_the_spy_v2_lines():
    report = build_report(_journal())
    assert "spy_v2" in report
    text = render_report(report)
    assert "v2: глубоких" in text and "предупреждённые нарушения: ход 3" in text and "критерии m10:" in text


def test_the_new_journal_entries_are_private_to_their_seat():
    ev = [{"type": "orders_summary", "seat": "A", "turn": 1, "moves": [], "fortify": []},
          {"type": "estimate_report", "seat": "A", "report_id": "r1", "turn": 1}]
    assert len(view_events(ev, seat="A")) == 2
    assert view_events(ev, seat="B") == [] and view_events(ev, spectator=True) == []


@pytest.mark.parametrize("scenario", ["blitz_spy"])
def test_the_game_journals_the_decision_entries(tmp_path, scenario):
    from dah.game import Game, make_config
    from dah.journal import read_events
    from dah.store import init_game_dir
    from test_core080_spy import Clock, _ready_all

    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "agent"} for s in "ABC"]
    gdir, _ = init_game_dir(tmp_path, make_config(game_id="g", scenario=f"core:{scenario}", seats=seats, seed=5))
    game = Game.create(gdir, Clock())
    while game.phase != "actions":
        _ready_all(game)
    a = [p.id for p in game.state.owned("A")]
    target = next(n for pid in a for n in game.ruleset.map.adjacency[pid]
                  if game.state.provinces[n].owner not in (None, "A"))
    game.submit("A", [{"kind": "spy", "shade": "s1", "mission": "observe", "target": target, "rounds": 1},
                      {"kind": "fortify", "province": a[0]}], "append")
    _ready_all(game)
    assert game.phase == "negotiation_2"
    events = read_events(gdir.journal_path)
    started = [e for e in events if e["type"] == "mission_started" and e["seat"] == "A"]
    assert started and started[0]["in_sight"] is True          # a neighbour: in sight at the close of actions
    reports = [r for r in (game.recon_report(game.state.turn) or {}).get("reports", {}).get("A", [])]
    if reports:
        game.estimate({"attacker": {"assault": 3}, "defender": reports[0]["id"]}, "A")
        assert any(e["type"] == "estimate_report" and e["seat"] == "A" for e in read_events(gdir.journal_path))
    _ready_all(game)
    summary = [e for e in read_events(gdir.journal_path) if e["type"] == "orders_summary" and e["seat"] == "A"]
    assert summary and summary[0]["fortify"] == [{"province": a[0], "p2": False}]
