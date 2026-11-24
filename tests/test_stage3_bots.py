"""Stage-3 bots, simulator and report (docs/SPY_AND_CONQUEST.md sections 7, 8): the bots' numbers match core 0.4.0;
the adapter reads Shades, statuses and claims; the bots hire, run missions by style, set the policy, decide fates,
keep garrisons, remember captures (decision 22); the intel route as data; the simulator per switch combination."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from conquest_helpers import PACKS
from dah.bots import BuilderBot, DiplomatBot, RushBot
from dah.bots import conquest as bcq
from dah.bots import spy
from dah.bots import view as bv
from dah.bots.view import parse_state
from dah.engine import build_ruleset, default_pack_dirs
from dah.engine import espionage as esp
from dah.game import Game, make_config
from dah.journal import read_events
from dah.server import create_app
from dah.store import init_game_dir

ROOT = Path(__file__).resolve().parents[1]
ME = {"A", "Нико"}


def province(pid: str, ptype: str, owner: str | None, units: Any, neighbors: list[str], **extra: Any) -> dict:
    return {"id": pid, "name": pid, "type": ptype, "owner": owner, "units": units, "neighbors": neighbors,
            "buildings": extra.pop("buildings", []), **extra}


def make_state(turn: int = 3, alloy: int = 60, provisions: int = 40, shades: list | None = None, whisper: int = 1,
               policy: str | None = None, espionage: bool = True, conquest: bool = True, **extra: Any) -> dict:
    provinces = [
        province("P01", "capital", "A", {"assault": 3}, ["P02", "P07"], buildings=["barracks"], origin="A"),
        province("P02", "forge", "A", {"assault": 2}, ["P01", "P07", "P09"], origin="A"),
        province("P07", "forge", "B", {"assault": 1}, ["P01", "P02", "P08"], origin="B"),
        province("P08", "capital", "B", None, ["P07", "P09"], origin="B"),
        province("P09", "capital", "A", {"assault": 2}, ["P02", "P08"], origin="C",
                 status={"occupied": {"by": "A", "from": "C", "since": 2}}),
    ]
    you: dict[str, Any] = {"seat": "A", "faction": "core:covenant", "command_points": 3,
                           "resources": {"alloy": alloy, "provisions": provisions},
                           "net_income": {"alloy": 10, "provisions": 2}}
    if espionage:
        you["espionage"] = {"whisper": whisper, "mirrors": 1, "shades_max": 2, "active_limit": 1,
                            "shades": shades or []}
    if conquest:
        you["conquest_policy"] = policy
    state = {"turn": turn, "turn_limit": 10, "phase": "actions", "you": you, "provinces": provinces,
             "players": [{"seat": s, "alive": True, "score": sc} for s, sc in (("A", 5), ("B", 7), ("C", 3))]}
    if conquest:
        state["claims"] = [{"seat": "C", "province": "P09", "until_turn": 12}]
    state.update(extra)
    return {"ok": True, "state": state}


def estimate_all(value: float):
    def estimate(attacker, defender, ptype, fortified, hasty):
        return value
    return estimate


# ---------------------------------------------------------------- numbers and adapter

def test_bot_numbers_match_core_040():
    rs = build_ruleset(default_pack_dirs(), "core:slice3")
    build, food = rs.roles["build"], rs.roles["upkeep_food"]
    assert bv.SHADE_COST_ALLOY == rs.espionage.shade_cost[build]
    assert bv.SHADE_UPKEEP_FOOD == rs.espionage.shade_upkeep[food]
    for full, m in rs.missions.items():
        assert bv.MISSIONS[full.split(":")[-1]] == (m.whisper, m.rounds_max, m.cost.get(build, 0),
                                                    m.cost_per_round.get(food, 0))
    for full, d in rs.dispositions.items():
        assert bv.DISPOSITION_FOOD[full.split(":")[-1]] == d.cost.get(food, 0)
    for full, pt in rs.province_types.items():
        assert bv.CONQUEST_VALUE.get(full.split(":")[-1], 1) == pt.conquest_value
    muls = {}
    for full, f in rs.factions.items():
        for m in (*f.bonuses, *f.penalties):
            if m.get("target", "").endswith(".cost_mul") and m["target"].startswith("disposition."):
                did = m["target"].split(".")[1].split(":")[-1]
                muls.setdefault(full.split(":")[-1], {})[did] = m["value"]
    assert muls == bv.DISPOSITION_COST_MUL
    assert spy.FEINT_CP == rs.stat("feint.cp_cost", None)


def test_parse_state_reads_stage3_fields():
    view = parse_state(make_state(shades=[{"id": "s1", "status": "idle"}], policy="core:subjugate"), ME)
    assert view.espionage is not None and view.whisper == 1 and spy.idle_shades(view) == ["s1"]
    assert view.conquest and view.conquest_policy == "subjugate" and view.claims[0]["province"] == "P09"
    occ = view.provinces["P09"]
    assert occ.occupied_by == "A" and occ.origin == "C" and [p.id for p in view.own_occupations()] == ["P09"]
    assert "P09" not in [p.id for p in view.full_provinces()]
    assert view.scores == {"A": 5, "B": 7, "C": 3}
    plain = parse_state(make_state(espionage=False, conquest=False), ME)
    assert plain.espionage is None and not plain.conquest and not plain.claims
    # a captured capital is never "the capital" (garrison, citadel, Shade hire)
    from dah.bots.tactics import capital_of
    assert capital_of(view).id == "P01"


def test_disposition_price_follows_value_and_faction():
    view = parse_state(make_state(), ME)
    assert view.disposition_food("subjugate", view.provinces["P09"]) == 15
    assert view.disposition_food("subjugate", view.provinces["P07"]) == 5
    growth = replace(view, faction="growth")
    assert growth.disposition_food("subjugate", growth.provinces["P09"]) == 18     # 15 x 1.2 = 18


# ---------------------------------------------------------------- conquest

def test_policy_and_fates():
    view = parse_state(make_state(provisions=40), ME)
    assert bcq.policy_order(view, 15) == {"kind": "policy", "disposition": "subjugate"}
    assert bcq.policy_order(view, 14) is None                      # none, and none is set already
    set_view = parse_state(make_state(policy="core:subjugate"), ME)
    assert bcq.policy_order(set_view, 10) == {"kind": "policy", "disposition": None}
    assert bcq.policy_order(set_view, 20) is None
    orders, spent = bcq.occupation_orders(view, 40)
    assert orders == [{"kind": "dispose", "province": "P09", "disposition": "subjugate"}] and spent == 15
    orders, spent = bcq.occupation_orders(view, 10)
    assert orders == [] and spent == 0                             # held until it can pay
    orders, _ = bcq.occupation_orders(view, 5, raid_short=True)    # 5 + 2 x 2 < 15: raided
    assert orders == [{"kind": "dispose", "province": "P09", "disposition": "raid"}]
    orders, _ = bcq.occupation_orders(view, 40, liberate_for=lambda p: "C")
    assert orders == [{"kind": "dispose", "province": "P09", "disposition": "liberate", "to": "C"}]
    assert bcq.occupation_garrison(view) == {"P09": {"assault": 1}}


def test_rush_directive_on_enemy_capital_and_garrison():
    view = parse_state(make_state(provisions=40), ME)
    moves = [{"kind": "move", "from": "P02", "to": "P08", "units": {"assault": 2}}]
    orders, spent = bcq.capital_directives(view, moves, 40)
    assert orders == [{"kind": "dispose", "province": "P08", "disposition": "subjugate"}] and spent == 15
    orders, spent = bcq.capital_directives(view, moves, 10)
    assert orders == [{"kind": "dispose", "province": "P08", "disposition": "exterminate"}] and spent == 0
    bot = RushBot("A", 1)
    out = bot.action_orders(parse_state(make_state(provisions=40), ME), estimate_all(0.9))
    assert not any(o["kind"] == "move" and o["from"] == "P09" and sum(o["units"].values()) >= 2 for o in out
                   if not o.get("feint"))                          # the occupation keeps one unit
    assert {"kind": "dispose", "province": "P09", "disposition": "subjugate"} in out


# ---------------------------------------------------------------- espionage

def test_memory_absorbs_intel_and_remembers_hostiles():
    mem = spy.SpyMemory()
    intel = {"turn": 4, "reports": [
        {"id": "r1", "behavior": "observe_province", "facts": {"province": "P08", "units": {"core:assault": [2, 5]}}},
        {"id": "r2", "behavior": "read_orders", "facts": {"seat": "B", "entries": [
            {"kind": "move", "from": "P07", "to": "P02", "units": {"core:assault": 1}}]}}],
        "rounds": [{"shade": "s2", "target": "P07", "outcome": "struck"}],
        "caught": [{"province": "P01", "mission": "core:ledger", "target": "A", "owner": "B"},
                   {"province": "P02", "mission": "core:observe", "target": "P02", "owner": None}],
        "lost": ["s3"]}
    mem.absorb(intel)
    assert mem.hostile == {"B"} and mem.caught == 2 and mem.lost_turn == 4 and mem.struck(4) == {"P07"}
    assert mem.orders_of("B", 4)[0]["to"] == "P02" and mem.orders_of("B", 5) == []
    mem.absorb({**intel, "caught": [{"owner": "C"}]})              # the same step twice: ignored
    assert mem.hostile == {"B"}
    view = spy.apply_memory(parse_state(make_state(turn=5), ME), mem)
    assert view.provinces["P08"].units == {"assault": 5}           # the upper bound instead of the fog
    old = spy.apply_memory(parse_state(make_state(turn=8), ME), mem)
    assert old.provinces["P08"].units is None


def test_hire_rules_and_rehire_delay():
    view = parse_state(make_state(turn=3, alloy=60), ME)
    mem = spy.SpyMemory()
    assert spy.hire_order(view, mem, from_turn=2, max_shades=1, alloy=60, min_alloy=35) == {"kind": "hire_shade"}
    assert spy.hire_order(view, mem, from_turn=4, max_shades=1, alloy=60, min_alloy=35) is None
    assert spy.hire_order(view, mem, from_turn=2, max_shades=1, alloy=30, min_alloy=35) is None
    held = parse_state(make_state(shades=[{"id": "s1", "status": "idle"}]), ME)
    assert spy.hire_order(held, mem, from_turn=2, max_shades=1, alloy=60, min_alloy=35) is None
    mem.lost_turn = 2
    assert spy.hire_order(view, mem, from_turn=2, max_shades=1, alloy=60, min_alloy=35) is None    # 3 - 2 < 2
    later = parse_state(make_state(turn=4), ME)
    assert spy.hire_order(later, mem, from_turn=2, max_shades=1, alloy=60, min_alloy=35) is not None
    last = parse_state(make_state(turn=10), ME)                                                     # no turn after
    assert spy.hire_order(last, spy.SpyMemory(), from_turn=2, max_shades=1, alloy=60, min_alloy=35) is None
    off = parse_state(make_state(espionage=False), ME)
    assert spy.hire_order(off, mem, from_turn=1, max_shades=1, alloy=99, min_alloy=0) is None


def test_mission_styles_of_the_bots():
    idle = [{"id": "s1", "status": "idle"}]
    rush = RushBot("A", 2).action_orders(parse_state(make_state(shades=idle, whisper=2), ME), estimate_all(0.9))
    sab = [o for o in rush if o["kind"] == "spy"]
    assert sab and sab[0]["mission"] == "sabotage" and sab[0]["rounds"] == 2
    rush1 = RushBot("A", 2).action_orders(parse_state(make_state(shades=idle, whisper=1), ME), estimate_all(0.0))
    obs = [o for o in rush1 if o["kind"] == "spy"]
    assert obs and obs[0]["mission"] == "observe" and obs[0]["target"] == "P08"     # the hidden capital first
    builder = BuilderBot("A", 2).action_orders(parse_state(make_state(shades=idle), ME), estimate_all(0.0))
    assert [o for o in builder if o["kind"] == "spy"] == [
        {"kind": "spy", "shade": "s1", "mission": "ledger", "target": "B", "rounds": 2}]
    diplomat = DiplomatBot("A", 2).action_orders(parse_state(make_state(shades=idle, whisper=2), ME),
                                                 estimate_all(0.0))
    read = [o for o in diplomat if o["kind"] == "spy"]
    assert read and read[0]["mission"] == "read_orders" and read[0]["rounds"] == 3
    weak = DiplomatBot("A", 2).action_orders(parse_state(make_state(shades=idle, whisper=1), ME), estimate_all(0.0))
    assert [o["mission"] for o in weak if o["kind"] == "spy"] == ["ledger"]


def test_feint_and_window_attack_of_rush():
    hits = 0
    for seed in range(30):
        bot = RushBot("A", seed)
        out = bot.action_orders(parse_state(make_state(), ME), estimate_all(0.0))
        feints = [o for o in out if o.get("feint")]
        assert len(feints) <= 1
        hits += bool(feints)
        for f in feints:
            assert f["kind"] == "move" and f["to"] in ("P07", "P08")
    assert 3 <= hits <= 20                                        # in about 30% of turns
    bot = RushBot("A", 1)
    bot.memory.strikes, bot.memory.strike_turn = {"P07"}, 3
    view = parse_state(make_state(turn=3, phase="negotiation_2"), ME)
    extra = bot.p2_orders(view, [], 3, estimate_all(0.7))
    assert extra and extra[0]["to"] == "P07" and extra[0]["kind"] == "move"
    assert bot.p2_orders(view, [], 1, estimate_all(0.7)) == []


def test_diplomat_fortifies_against_read_moves_and_spares_hostile_seats():
    bot = DiplomatBot("A", 1)
    bot.memory.orders["B"] = (3, [{"kind": "move", "from": "P07", "to": "P02", "units": {}},
                                  {"kind": "move", "from": "P07", "to": "P01", "units": {}, "exposed": True}])
    view = parse_state(make_state(turn=3, phase="negotiation_2"), ME)
    assert bot.p2_orders(view, [], 3, estimate_all(0.0)) == [{"kind": "fortify", "province": "P02"}]

    class Fake:
        def __init__(self) -> None:
            self.calls: list = []

        def respond_pact(self, *a):
            self.calls.append(("respond", *a))

        def propose_pact(self, to, turns):
            self.calls.append(("propose", to))

        def send_gift(self, *a):
            self.calls.append(("gift", *a))

    api = Fake()
    bot.memory.hostile.add("B")
    bot.diplomacy(parse_state(make_state(phase="negotiation_1"), ME), api)
    assert ("propose", "B") not in api.calls


def test_systems_off_leave_the_bots_as_before():
    plain = make_state(espionage=False, conquest=False)
    for cls in (RushBot, BuilderBot, DiplomatBot):
        out = cls("A", 7).action_orders(parse_state(plain, ME), estimate_all(0.9))
        assert not {o["kind"] for o in out} & {"hire_shade", "spy", "dispose", "policy", "recall"}
        assert not any(o.get("feint") for o in out)


# ---------------------------------------------------------------- intel as data

def test_intel_route_gives_own_facts_only(tmp_path):
    seats = [{"seat": s, "nick": f"Ник{s}", "kind": "agent", "faction": "iron"} for s in "ABC"]
    config = make_config(game_id="s3", scenario="tri_all", seats=seats, seed=4, pack_dirs=PACKS)
    gdir, keys = init_game_dir(tmp_path, config)
    Game.create(gdir, lambda: 1_700_000_000.0)
    client = TestClient(create_app(str(tmp_path), admin_key="x", clock=lambda: 1_700_000_000.0, tick_interval=None))
    h = {s: {"Authorization": f"Bearer {k}"} for s, k in keys.items()}
    data = client.get("/v1/intel", params={"format": "json"}, headers=h["A"]).json()
    assert data["ok"] and data["intel"] == {"turn": None, "reports": [], "rounds": [], "caught": [], "lost": []}
    bad = client.get("/v1/intel", params={"format": "xml"}, headers=h["A"]).json()
    assert not bad["ok"] and bad["error"]["code"] == "INVALID"
    reads = [e for e in read_events(gdir.journal_path) if e["type"] == "intel_read"]
    assert reads and reads[-1]["format"] == "json"
    state = client.get("/v1/state", params={"format": "json"}, headers=h["A"]).json()["state"]
    assert state["you"]["espionage"]["shades"] == [] and all("origin" in p for p in state["provinces"]
                                                             if p["owner"])


def test_intel_json_never_carries_truth():
    rep = {"turn": 2, "reports": {"A": [{"id": "r1", "seat": "A", "shade": "s1", "mission": "m", "behavior": "b",
                                         "target": "B", "round": 1, "rounds": 2, "turn": 2, "facts": {"x": [1, 2]},
                                         "truth": {"x": 1}}]},
           "rounds": [{"seat": "A", "shade": "s1", "mission": "m", "target": "B", "outcome": "found",
                       "report_id": "r1", "u_det": 0.3}],
           "captures": {"A": [{"province": "P1", "mission": "m", "target": "A", "owner": None}]},
           "lost": {"B": ["s2"]}}
    a = esp.intel_json(rep, "A")
    assert "truth" not in a["reports"][0] and a["reports"][0]["facts"] == {"x": [1, 2]}
    assert a["rounds"] == [{"shade": "s1", "mission": "m", "target": "B", "outcome": "found", "report_id": "r1"}]
    assert a["caught"] and a["lost"] == []
    assert esp.intel_json(rep, "B")["lost"] == ["s2"] and esp.intel_json(rep, "B")["reports"] == []


# ---------------------------------------------------------------- simulator

def _simulate_module():
    spec = importlib.util.spec_from_file_location("dah_simulate", ROOT / "scripts" / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate"] = module
    spec.loader.exec_module(module)
    return module


def test_simulator_plays_recon_and_reports_stage3(capsys):
    sim = _simulate_module()
    rs = build_ruleset(None, "core:slice3")
    sim.sync_bot_numbers(rs)
    kinds = {"A": "rush", "B": "builder", "C": "diplomat"}
    g = sim.play_game(rs, kinds, "s3|0")
    assert len(g["recons"]) == g["turns"]
    t = sim.Tally()
    t.add(rs, g)
    summary = sim.summary(t)["stage3"]
    assert summary["espionage_games"] == 1 and "dispositions" in summary and "loot_per_province" in summary
    off = build_ruleset(None, "core:slice3", systems={"espionage": False, "conquest": False})
    g_off = sim.play_game(off, kinds, "s3|0")
    assert g_off["recons"] == [] and not any(e["kind"] in ("disposition", "sabotage") for e in g_off["events"])
    assert sim.switch_combos(rs, "both", None) == [{"espionage": True, "conquest": True},
                                                   {"espionage": False, "conquest": True}]
    assert sim.main(["--games", "1", "--config", "rush,builder,diplomat", "--espionage", "both", "--conquest",
                     "off", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"espionage=on,conquest=off", "espionage=off,conquest=off"}
    assert "stage3" in data["espionage=on,conquest=off"]["ALL"]
