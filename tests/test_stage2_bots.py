"""Stage 2 for bots, simulator, spectator and report (TECH_AND_SETUP.md section 4): setup choices by style, artifact
valuation (draft and points), research by branch priority, unique units and temples, the in-process simulator with a
setup phase, the spectator's setup/faction/techs views and their fog, the report's factions, artifacts and techs."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from dah.bots import BOT_TYPES, BuilderBot, DiplomatBot, PassiveBot, RushBot
from dah.bots.base import seat_seed
from dah.bots.choices import artifact_value, faction_prefs, pick_artifacts, pick_research
from dah.bots.client import ApiClient
from dah.bots.view import parse_state
from dah.engine import build_ruleset
from dah.game import Game, make_config, replay
from dah.journal import read_events
from dah.report import build_report, render_report
from dah.server import create_app
from dah.store import GameDir, init_game_dir
from stage2_helpers import FakeClock

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "admin-secret"
ME = {"A", "Нико"}


# ---------------------------------------------------------------- pure choices

def faction(fid: str) -> dict[str, Any]:
    return {"id": f"core:{fid}", "name": fid}


def artifact(aid: str, power: int, cost: int = 1, factions: list[str] | None = None,
             exclusive: list[str] | None = None) -> dict[str, Any]:
    return {"id": f"core:{aid}", "name": aid, "power": power, "cost": cost, "factions": factions or [],
            "exclusive_with": exclusive or []}


SETUP_FACTIONS = [faction(f) for f in ("covenant", "litany", "synclite", "ash_choir", "growth", "captains")]


def test_faction_prefs_follow_style_then_scenario_order():
    setup = {"factions": SETUP_FACTIONS}
    assert faction_prefs(setup, ("captains", "growth"))[:3] == ["core:captains", "core:growth", "core:covenant"]
    assert len(faction_prefs(setup, ("captains", "nobody"))) == 6
    assert faction_prefs({"factions": [faction("iron")]}, ("captains",)) == ["core:iron"]  # another pack


def test_draft_picks_best_value_and_respects_faction():
    offer = [artifact("ore_vein", 2), artifact("calm_banner", 1), artifact("ash_censer", 3, factions=["core:ash_choir"])]
    setup = {"artifacts_mode": "draft", "offer": offer}
    assert pick_artifacts(setup, "core:litany", ()) == ["core:ore_vein"]          # ash_censer: not for litany
    assert pick_artifacts(setup, "core:ash_choir", ()) == ["core:ash_censer"]
    assert pick_artifacts(setup, "core:litany", ("calm_banner",)) == ["core:calm_banner"]  # a liked one wins ties
    assert artifact_value(offer[1], ("calm_banner",)) > artifact_value(offer[1], ())
    assert pick_artifacts({"artifacts_mode": "none", "offer": []}, None, ()) is None


def test_points_picks_best_set_within_budget_without_exclusive_pairs():
    pool = [artifact("seal_of_command", 2, 3, exclusive=["core:calm_banner"]), artifact("calm_banner", 1, 1),
            artifact("trail_maps", 1, 1), artifact("ore_vein", 2, 2), artifact("war_codex", 1, 1)]
    setup = {"artifacts_mode": "points", "artifact_points": 3, "offer": pool}
    picked = pick_artifacts(setup, None, ())
    assert sum(next(a["cost"] for a in pool if a["id"] == p) for p in picked) <= 3
    assert sum(next(a["power"] for a in pool if a["id"] == p) for p in picked) == 3
    with_seal = pick_artifacts({**setup, "artifact_points": 4}, None, ("seal_of_command",))
    assert "core:seal_of_command" in with_seal and "core:calm_banner" not in with_seal


def tech(tid: str, branch: str, tier: int, cost: int, status: str = "available") -> dict[str, Any]:
    return {"id": f"core:{tid}", "branch": f"core:{branch}", "tier": tier, "cost": cost, "status": status}


def test_pick_research_by_branch_priority_tier_and_first_list():
    techs = {"enabled": True, "techs": [tech("a1", "industry", 1, 10), tech("w1", "war", 1, 8),
                                        tech("w2", "war", 2, 20), tech("w0", "war", 1, 12, "researched"),
                                        tech("m1", "mystery", 1, 5)]}
    assert pick_research(techs, ("war", "industry")) == "core:w2"               # higher tier first
    assert pick_research(techs, ("industry",)) == "core:a1"
    assert pick_research(techs, ("logistics",)) == "core:w2"                   # unranked: higher tier, cheaper
    assert pick_research({"enabled": True, "techs": techs["techs"][:1] + techs["techs"][4:]}, ()) == "core:m1"
    assert pick_research(techs, ("war",), ("a1",)) == "core:a1"
    assert pick_research({"enabled": False}, ("war",)) is None


def test_bot_styles_and_passive_defaults():
    for cls in (RushBot, BuilderBot, DiplomatBot):
        assert cls.faction_style and cls.branch_priority and cls.artifact_likes
    assert len({cls.faction_style[0] for cls in (RushBot, BuilderBot, DiplomatBot)}) == 3
    assert PassiveBot("A", 1).setup_choices({"factions": SETUP_FACTIONS}) == (None, None)
    prefs, arts = RushBot("A", 1).setup_choices({"factions": SETUP_FACTIONS, "artifacts_mode": "draft",
                                                 "offer": [artifact("mercenary_band", 2), artifact("ore_vein", 2)]})
    assert prefs[0] == "core:captains" and arts == ["core:mercenary_band"]


def test_core_names_the_bots_use_exist():
    rs = build_ruleset(None, "quad4")
    shorts = {kind: {i.split(":")[-1] for i in getattr(rs, kind)} for kind in ("factions", "artifacts", "techs",
                                                                              "branches")}
    for cls in (RushBot, BuilderBot, DiplomatBot):
        assert set(cls.faction_style) <= shorts["factions"]
        assert set(cls.artifact_likes) <= shorts["artifacts"]
        assert set(cls.tech_first) <= shorts["techs"]
        assert set(cls.branch_priority) == shorts["branches"]


# ---------------------------------------------------------------- stage-2 orders from a state

def stage2_state(faction_id: str = "core:litany", techs: list[str] | None = None, researching: Any = None,
                 alloy: int = 60, creed: int = 0, buildings: list[str] | None = None) -> dict[str, Any]:
    provinces = [
        {"id": "P01", "type": "core:capital", "owner": "A", "units": {"core:assault": 3},
         "buildings": ["core:barracks", *(buildings or [])], "neighbors": ["P02", "P07"]},
        {"id": "P02", "type": "core:forge", "owner": "A", "units": {}, "buildings": ["core:mine"],
         "neighbors": ["P01", "P07"]},
        {"id": "P03", "type": "core:shrine", "owner": "A", "units": {}, "buildings": [], "neighbors": ["P01"]},
        {"id": "P07", "type": "core:forge", "owner": "B", "units": {"core:assault": 5}, "buildings": [],
         "neighbors": ["P01", "P02"]},
    ]
    you = {"seat": "A", "faction": faction_id, "command_points": 3,
           "resources": {"core:alloy": alloy, "core:provisions": 40, "core:creed": creed},
           "net_income": {"core:alloy": 10, "core:provisions": 5, "core:creed": 2},
           "techs": techs or [], "researching": researching, "research": {}, "artifacts": ["core:ore_vein"]}
    return {"turn": 2, "turn_limit": 10, "phase": "actions", "you": you, "provinces": provinces,
            "players": [{"seat": "A", "alive": True}, {"seat": "B", "alive": True}]}


def no_win(*_args: Any) -> float:
    return 0.0


def test_parse_state_reads_stage2_fields():
    view = parse_state(stage2_state(techs=["core:fortification"], researching={"tech": "core:deep_drilling"}), ME)
    assert view.faction == "litany" and view.unique_unit == "throne_warden"
    assert view.techs == ("fortification",) and view.researching == "deep_drilling"
    assert view.artifacts == ("ore_vein",) and view.research_enabled
    assert parse_state({"you": {"seat": "A", "resources": {}}, "provinces": []}, ME).research_enabled is False


def test_bots_recruit_their_unique_unit_first():
    view = parse_state(stage2_state(alloy=60), ME)
    rush = RushBot("A", 1).action_orders(view, no_win)
    recruits = [o for o in rush if o["kind"] == "recruit"]
    assert recruits[0] == {"kind": "recruit", "province": "P01", "unit": "core:throne_warden", "count": 2}
    assert recruits[1]["unit"] == "core:assault" and recruits[1]["count"] == (60 - 28) // 12
    builder = BuilderBot("A", 1).action_orders(view, no_win)
    assert {"kind": "recruit", "province": "P01", "unit": "core:throne_warden", "count": 1} in builder
    # ash cantors cost only alloy (creed is spent only on techs): recruited with no creed in stock
    ash = parse_state(stage2_state("core:ash_choir", alloy=60, creed=0), ME)
    assert {"kind": "recruit", "province": "P01", "unit": "core:ash_cantor", "count": 2} in \
        RushBot("A", 1).action_orders(ash, no_win)


def test_builder_lays_a_temple_and_a_citadel_with_fortification():
    builder = BuilderBot("A", 1)
    builder.build_stop = 3
    orders = builder.action_orders(parse_state(stage2_state(alloy=100), ME), no_win)
    assert {"kind": "build", "province": "P03", "building": "core:temple"} in orders   # a shrine first
    no_techs = stage2_state(alloy=100)
    del no_techs["you"]["techs"]
    assert all(o.get("building") != "core:temple" for o in builder.action_orders(parse_state(no_techs, ME), no_win))
    fort = builder.action_orders(parse_state(stage2_state(alloy=100, techs=["core:fortification"]), ME), no_win)
    assert {"kind": "build", "province": "P01", "building": "core:citadel"} in fort
    has = builder.action_orders(parse_state(stage2_state(alloy=100, techs=["core:fortification"],
                                                         buildings=["core:citadel", "core:temple"]), ME), no_win)
    assert all(o.get("building") not in ("core:citadel", "core:temple") for o in has)


def test_research_order_only_when_nothing_is_active():
    techs = {"enabled": True, "techs": [tech("deep_drilling", "industry", 1, 10), tech("drill_doctrine", "war", 1, 10)]}
    idle = parse_state(stage2_state(), ME)
    assert RushBot("A", 1).research_order(idle, techs) == {"kind": "research", "tech": "core:drill_doctrine"}
    assert BuilderBot("A", 1).research_order(idle, techs) == {"kind": "research", "tech": "core:deep_drilling"}
    busy = parse_state(stage2_state(researching={"tech": "core:agro_vats", "progress": 1, "cost": 10}), ME)
    assert RushBot("A", 1).research_order(busy, techs) is None
    assert PassiveBot("A", 1).research_order(idle, techs) is None


def test_bot_tables_match_the_core_pack_beyond_alloy():
    from dah.bots import view as bv

    rs = build_ruleset(None, "quad4")
    for unit, other in bv.UNIT_COST_OTHER.items():
        cost = rs.unit_cost(f"core:{unit}")
        assert other == {r.split(":")[-1]: n for r, n in cost.items() if r != "core:alloy"}, unit
    for unit in bv.UNIT_COST_ALLOY:
        cost = rs.unit_cost(f"core:{unit}")
        assert {r for r in cost if r != "core:alloy"} == {f"core:{r}" for r in bv.UNIT_COST_OTHER.get(unit, {})}
    for fac, unit in bv.UNIQUE_UNITS.items():
        assert list(rs.units[f"core:{unit}"].factions) == [f"core:{fac}"]
    assert all(rs.buildings[f"core:{b}"].recruit for b in bv.RECRUIT_BUILDINGS)


# ---------------------------------------------------------------- bots against the app, in process

class World:
    """A game served by the real app in process; ApiClients reach it through a mock transport."""

    def __init__(self, tmp_path: Path, seats: list[dict[str, Any]], scenario: str = "core:slice3",
                 turn_limit: int = 3, **kw: Any) -> None:
        self.clock = FakeClock()
        config = make_config(game_id="s2", scenario=scenario, seats=seats, seed=5, turn_limit=turn_limit, **kw)
        self.gdir, self.keys = init_game_dir(tmp_path, config)
        self.spectator_key = self.gdir.issue_spectator_key()
        Game.create(self.gdir, self.clock)
        self.client = TestClient(create_app(str(tmp_path), admin_key=ADMIN, clock=self.clock, tick_interval=None))
        transport = httpx.MockTransport(self._forward)
        self.apis = {seat: ApiClient("http://game.test", key, transport=transport) for seat, key in self.keys.items()}

    def _forward(self, request: httpx.Request) -> httpx.Response:
        resp = self.client.request(request.method, str(request.url.raw_path, "ascii"), headers=dict(request.headers),
                                   content=request.content)
        return httpx.Response(resp.status_code, content=resp.content, headers={"content-type": "application/json"})

    @property
    def game(self) -> Game:
        return self.client.app.state.registry.games["s2"]

    def watch(self, who: str, path: str, status: int = 200) -> dict[str, Any]:
        key = ADMIN if who == "admin" else self.spectator_key
        resp = self.client.get(f"/v1/spectate/s2/{path}", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == status, resp.text
        return resp.json()

    def play_phase(self, bots: dict[str, Any]) -> None:
        phase = self.game.phase
        for seat, bot in sorted(bots.items()):
            if self.game.phase != phase:
                break
            if seat in self.game.alive_seats():
                bot.play_phase(self.apis[seat], phase, {seat, self.game.state.players[seat].nick})

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in read_events(self.gdir.journal_path) if e["type"] == kind]


BOT_SEATS = [{"seat": "A", "nick": "Раш", "kind": "bot", "bot": "rush"},
             {"seat": "B", "nick": "Строй", "kind": "bot", "bot": "builder"},
             {"seat": "C", "nick": "Дип", "kind": "bot", "bot": "diplomat"}]


def test_bots_play_setup_and_research_through_the_api(tmp_path):
    world = World(tmp_path, BOT_SEATS)
    bots = {s["seat"]: BOT_TYPES[s["bot"]](s["seat"], seat_seed("s2", s["seat"])) for s in BOT_SEATS}
    assert world.game.phase == "setup"
    for _ in range(40):
        if world.game.game_over:
            break
        world.play_phase(bots)
    assert world.game.game_over

    choices = world.events("setup_choice")
    assert {(e["seat"], e["kind"]) for e in choices} == {(s, k) for s in "ABC" for k in ("factions", "artifacts")}
    closed = world.events("setup_closed")[0]
    assert closed["reason"] == "all_ready"
    assert closed["factions"] == {"A": "core:captains", "B": "core:litany", "C": "core:synclite"}
    assert all(len(arts) == 1 for arts in closed["artifacts"].values())        # draft: one each
    research = world.events("research_started")
    assert {e["seat"] for e in research} == {"A", "B", "C"}
    orders = json.loads((world.gdir.path / "private" / "orders" / "turn_1.json").read_text(encoding="utf-8"))
    assert all(any(o["kind"] == "research" for o in orders[s]["orders"]) for s in "ABC")
    assert any(o["kind"] == "recruit" and o["unit"] == "core:throne_warden"
               for t in range(1, 4)
               for o in json.loads((world.gdir.path / "private" / "orders" / f"turn_{t}.json")
                                   .read_text(encoding="utf-8"))["B"]["orders"])
    result = replay(GameDir(world.gdir.path))
    assert result.ok, result


# ---------------------------------------------------------------- spectator

TWO = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "swarm"}]


def _ready_all(world: World) -> None:
    for seat in ("A", "B"):
        api = world.apis[seat]
        api.ready(None if world.game.phase == "setup" else api.orders()["orders_hash"])


def test_spectator_setup_factions_and_techs_fog(tmp_path):
    world = World(tmp_path, TWO, scenario="core:slice2", turn_limit=2)
    a, b = world.apis["A"], world.apis["B"]
    offer_a = a.get_setup()["offer"]
    a.choose_faction(["litany"])
    a.choose_artifacts([offer_a[0]["id"]])
    b.choose_faction(["synclite"])
    artifact_a = offer_a[0]["id"]

    live = world.watch("admin", "live")
    assert live["phase"] == "setup" and live["setup"]["open"] and live["setup"]["artifacts"] == "draft"
    assert set(live["setup"]["choices"]) == {"A", "B"} and live["setup"]["choices"]["B"]["artifacts"] == []
    spect = world.watch("spectator", "live")
    assert spect["setup"]["open"] and "choices" not in spect["setup"]
    start = world.watch("spectator", "turn/0")
    assert start["provisional"] is True and all("faction" not in p for p in start["players"])
    dumped = json.dumps(world.watch("spectator", "turn/0")) + json.dumps(spect)
    assert "litany" not in dumped and "synclite" not in dumped and artifact_a not in dumped
    assert world.watch("admin", "turn/1")["provisional"] is True

    _ready_all(world)                                   # setup closes: turn 1 is rebuilt
    assert world.game.phase == "negotiation_1"
    start = world.watch("spectator", "turn/0")         # not the cached provisional snapshot
    assert "provisional" not in start
    assert {p["seat"]: p["faction"] for p in start["players"]} == {"A": "core:litany", "B": "core:synclite"}
    assert all(isinstance(p["techs_count"], int) and "techs" not in p and "artifacts" not in p
               for p in start["players"])        # an artifact may grant a tech: the count is public, not which
    assert start["techs_total"] > 0 and artifact_a not in json.dumps(start)
    assert "choices" not in world.watch("admin", "live")["setup"]
    now = world.watch("admin", "turn/1")
    a_entry = next(p for p in now["players"] if p["seat"] == "A")
    assert a_entry["artifacts"] == [artifact_a] and a_entry["faction_name"] and "tech_names" in now

    # turn 1: A researches
    _ready_all(world)                                   # -> actions
    a.submit_orders([{"kind": "research", "tech": "dream_lore"}], "replace_all")
    _ready_all(world)                                   # -> П2
    _ready_all(world)                                   # -> resolution, turn 2
    t1 = world.watch("admin", "turn/1")
    a_entry = next(p for p in t1["players"] if p["seat"] == "A")
    assert a_entry["researching"]["tech"] == "core:dream_lore" and a_entry["researching"]["progress"] > 0
    assert any(e["kind"] == "research_started" and e["text"].startswith("Нико: ") for e in t1["events"])
    for _ in range(3):
        _ready_all(world)                               # turn 2 -> game over
    assert world.game.game_over
    revealed = world.watch("spectator", "turn/2")
    assert next(p for p in revealed["players"] if p["seat"] == "A")["artifacts"] == [artifact_a]


def test_spectator_snapshots_hide_research_from_the_delayed_view(tmp_path):
    world = World(tmp_path, TWO, scenario="core:slice2", turn_limit=4)
    _ready_all(world)                                   # setup (defaults)
    for _ in range(2):                                  # two turns with research
        _ready_all(world)
        world.apis["A"].submit_orders([{"kind": "research", "tech": "dream_lore"}], "replace_all")
        _ready_all(world)
        _ready_all(world)
    snap = world.watch("spectator", "turn/1")
    text = json.dumps(snap)
    assert "dream_lore" not in text and "research" not in text.replace("techs_count", "")
    assert all(set(p) >= {"faction", "techs_count"} for p in snap["players"])


# ---------------------------------------------------------------- report

def test_report_has_factions_artifacts_and_techs_by_turn():
    cfg = {"game_id": "r1", "scenario": "core:slice3", "turn_limit": 3, "setup": {"artifacts": "draft"},
           "seats": [{"seat": "A", "nick": "Раш", "kind": "bot"}, {"seat": "B", "nick": "Строй", "kind": "bot"}]}
    events = [
        {"type": "game_created", "ts": 0, "config": cfg},
        {"type": "phase_open", "ts": 1, "turn": 1, "phase": "setup"},
        {"type": "setup_choice", "ts": 2, "seat": "A", "kind": "factions", "factions": ["core:captains"],
         "turn": 1, "phase": "setup"},
        {"type": "phase_close", "ts": 3, "turn": 1, "phase": "setup", "reason": "all_ready", "not_ready": []},
        {"type": "setup_closed", "ts": 3, "turn": 1, "reason": "all_ready",
         "factions": {"A": "core:captains", "B": "core:covenant"},
         "artifacts": {"A": ["core:calm_banner"], "B": ["core:ore_vein"]},
         "results": {"A": {"faction": "core:captains", "artifacts": ["core:calm_banner"], "auto_faction": False,
                           "faction_conflict": False},
                     "B": {"faction": "core:covenant", "artifacts": ["core:ore_vein"], "auto_faction": True,
                           "faction_conflict": False}}},
        {"type": "research_started", "ts": 4, "seat": "A", "tech": "core:drill_doctrine", "turn": 1},
        {"type": "turn_resolved", "ts": 5, "turn": 1, "scores": {"A": 4, "B": 4}},
        {"type": "tech_completed", "ts": 6, "seat": "A", "tech": "core:drill_doctrine", "turn": 3},
        {"type": "turn_resolved", "ts": 7, "turn": 3, "scores": {"A": 5, "B": 4}},
    ]
    rep = build_report(events)
    assert rep["seats"]["A"]["faction"] == "core:captains" and rep["seats"]["B"]["artifacts"] == ["core:ore_vein"]
    assert rep["seats"]["B"]["auto_faction"] is True and rep["setup"] == {"artifacts": "draft", "closed": "all_ready"}
    assert rep["totals"]["A"]["techs"] == [[3, "core:drill_doctrine"]] and rep["totals"]["A"]["setup_choices"] == 1
    turns = {t["turn"]: t for t in rep["turns"]}
    assert turns[1]["research_started"] == {"A": "core:drill_doctrine"}
    assert turns[3]["techs_completed"] == {"A": ["core:drill_doctrine"]}
    text = render_report(rep)
    assert "фракция captains" in text and "артефакты calm_banner" in text and "фракция covenant (по умолчанию)" in text
    assert "ход 3: Раш 5, Строй 4; изучено: Раш drill_doctrine" in text
    assert "Фаза старта: артефакты draft, закрыта all_ready" in text and "ход 1 СТАРТ" in text


# ---------------------------------------------------------------- simulator

def _simulate_module():
    spec = importlib.util.spec_from_file_location("dah_simulate", ROOT / "scripts" / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate"] = module
    spec.loader.exec_module(module)
    return module


def test_simulator_plays_setup_and_reports_factions(capsys):
    sim = _simulate_module()
    rs = build_ruleset(None, "slice3")
    assert rs.scenario.setup is not None
    kinds = {"A": "rush", "B": "builder", "C": "diplomat"}
    g = sim.play_game(rs, kinds, "t|0", sim.setup_config(rs), None)
    assert g["factions"] == {"A": "core:captains", "B": "core:litany", "C": "core:synclite"}
    assert all(len(a) == 1 for a in g["artifacts"].values())
    again = sim.play_game(rs, kinds, "t|0", sim.setup_config(rs), None)
    assert again["scores"] == g["scores"] and again["techs"] == g["techs"]  # deterministic
    dealt = sim.dealt_factions(list(rs.scenario.factions), ["A", "B", "C"], 1)
    g = sim.play_game(rs, kinds, "t|1", sim.setup_config(rs), dealt)
    assert g["factions"] == dealt
    assert sim.main(["--games", "2", "--scenario", "core:quad4", "--config", "rush,builder,diplomat,passive"]) == 0
    out = capsys.readouterr().out
    assert "faction" in out and "artifact" in out and "techs/seat by bot type" in out
    assert sim.main(["--games", "1", "--config", "rush,builder", "--factions", "bots", "--artifacts", "points"]) == 0
