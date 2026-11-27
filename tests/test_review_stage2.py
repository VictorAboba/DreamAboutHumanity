"""Regression tests for the stage-2 review findings (TECH_AND_SETUP.md, section 10)."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

import pytest

from dah.bridge.tools import EXAMPLES, RESEARCH, TOOLS, Bridge
from dah.cli import main as cli_main
from dah.engine import build_ruleset, default_pack_dirs, new_game_state, render_state_text, tech_counts, view_state
from dah.engine.catalog import find
from dah.engine.content import Profile, validate_packs
from dah.engine.setup import draft_offers, resolve_setup
from dah.engine.techs import move_range, techs_text, techs_view
from dah.game import ALLOWED, ALWAYS_TOOLS, READ_TOOLS, Game, make_config, replay
from dah.journal import read_events
from dah.store import GameDir, init_game_dir
from slice_helpers import slice_config
from stage2_helpers import CORE020, PACK, edit_yaml

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "agent"}]
SEATS3 = SEATS + [{"seat": "C", "nick": "Гамма", "kind": "agent"}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def _play_out(game: Game, clock: FakeClock) -> None:
    game.start()
    while not game.game_over:
        if game.phase == "actions":
            cap = next(p.id for p in game.state.owned("A") if p.type == "core:capital")
            game.submit("A", [{"kind": "recruit", "province": cap, "unit": "assault", "count": 1}])
        clock.now += 1200
        game.tick()


# ---------------------------------------------------------------- 1. old games replay and load with frozen core

def test_a_game_on_core_020_replays_and_loads_without_hand_editing(tmp_path, caplog):
    clock = FakeClock()
    config = slice_config(game_id="old", scenario="core:slice2", seats=SEATS, seed=7, turn_limit=2,
                          pack_dirs=CORE020)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    _play_out(game, clock)
    old_hash = game.ruleset.hash
    # every config written before stage 2 has pack_dirs null (the default packs, then core 0.2.0)
    raw = json.loads(gdir.config_path.read_text(encoding="utf-8"))
    raw["pack_dirs"] = None
    gdir.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    rep = replay(GameDir(gdir.path))
    assert rep.ok and [t.turn for t in rep.turns] == [1, 2]
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == "core-0.2.0"
    assert replay(GameDir(gdir.path), CORE020).ok                       # explicit packs work too
    assert not replay(GameDir(gdir.path), [str(p) for p in default_pack_dirs()]).ruleset_ok
    assert cli_main(["replay", "old", "--games-dir", str(tmp_path)]) == 0
    assert cli_main(["replay", "old", "--games-dir", str(tmp_path), "--packs", ",".join(CORE020)]) == 0

    loaded = Game.load(GameDir(gdir.path), clock)                       # a running game keeps its rules
    assert loaded.ruleset.hash == old_hash and "core:creed" not in loaded.ruleset.resources
    assert loaded.admin_status()["ruleset_warning"] is None

    # nothing matches the journal: the game loads under the packs it has, with a loud warning
    lines = gdir.journal_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["ruleset_hash"] = "0" * 64
    lines[0] = json.dumps(first, ensure_ascii=False)
    gdir.journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="dah.game"):
        drifted = Game.load(GameDir(gdir.path), clock)
    assert "ruleset hash" in caplog.text and drifted.admin_status()["ruleset_warning"]
    assert cli_main(["replay", "old", "--games-dir", str(tmp_path)]) == 1


# ---------------------------------------------------------------- 2. the research resource buys only techs

def test_no_core_unit_or_building_costs_the_research_resource():
    rs = build_ruleset(None, "quad4")
    res = rs.research_resource
    assert not [u for u in rs.units if res in rs.unit_cost(u)]
    assert not [b for b in rs.buildings if res in rs.building_cost(b)]
    assert rs.unit_cost("core:ash_cantor", "core:ash_choir") == {"core:alloy": 12}


def test_a_researching_ash_choir_seat_recruits_its_cantors(tmp_path):
    clock = FakeClock()
    config = make_config(game_id="cant", scenario="core:slice2", seats=SEATS, seed=3, turn_limit=3)
    config.pop("setup")
    config["phase_limits"].pop("setup")
    config["seats"][0]["faction"] = "ash_choir"
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    game.start()
    cap = next(p.id for p in game.state.owned("A") if p.type == "core:capital")
    for turn in (1, 2):
        while game.phase != "actions":
            clock.now += 1200
            game.tick()
        game.submit("A", [{"kind": "research", "tech": "dream_lore" if turn == 1 else "whisper_veil"},
                          {"kind": "recruit", "province": cap, "unit": "ash_cantor", "count": 1}])
        assert len(game._book("A").orders) == 2
        clock.now += 1200
        game.tick()
    while game.state.turn < 3:                           # N2 of turn 2: resolve it
        clock.now += 1200
        game.tick()
    cantors = sum(p.units.get("core:ash_cantor", 0) for p in game.state.owned("A"))
    assert cantors == 3                                  # 1 at the start + 1 each turn while researching
    assert game.state.players["A"].research or game.state.players["A"].techs


def test_validator_warns_when_a_unit_costs_the_research_resource(tmp_path):
    pack = tmp_path / "t2"
    shutil.copytree(PACK, pack)
    edit_yaml(pack / "units" / "units.yaml",
              lambda d: [{**u, "cost": {**u["cost"], "creed": 2}} if u["id"] == "militia" else u for u in d])
    errors, warnings = validate_packs([pack])
    assert errors == [] and any("research resource" in w.message for w in warnings)


# ---------------------------------------------------------------- 3/7. faction-locked artifacts in the setup

def test_draft_offers_only_artifacts_every_faction_may_hold():
    rs = build_ruleset(None, "slice3")
    setup = {"artifacts": "draft", "artifact_offer": 3}
    for seed in range(40):
        offers = draft_offers(rs, setup, ["A", "B", "C"], seed)
        assert all(not rs.artifacts[a].factions for offer in offers.values() for a in offer), seed


def test_points_mode_refills_the_budget_of_a_dropped_artifact():
    rs = build_ruleset(None, "slice3")
    setup = {"artifacts": "points", "artifact_points": 3, "unique_factions": True}
    choice = {"A": {"factions": ["core:litany"], "artifacts": ["core:ash_censer", "core:tithe_chalice"]}}
    out = resolve_setup(rs, setup, 1, [{"seat": "A"}], choice, {})
    assert out["A"]["dropped"] == ["core:ash_censer"]
    assert out["A"]["artifacts"] == ["core:ancient_blueprint", "core:tithe_chalice"]     # 1 point refilled


def _setup_game(tmp_path, artifacts: str) -> Game:
    config = make_config(game_id=f"s-{artifacts}", scenario="core:slice3", seats=SEATS3, seed=5, artifacts=artifacts)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock())
    game.start()
    return game


def test_setup_text_marks_faction_locked_artifacts_and_choices_warn(tmp_path):
    game = _setup_game(tmp_path, "points")
    text = game.get_setup("A")["text"]
    assert re.search(r"ash_censer «[^»]+» \(1\) \[только Хор Пепла\]", text)
    assert "covenant «Завет Предела»: нейтральная" in text and "в скобках стоимость" in text
    game.choose_faction("A", ["litany"])
    reply = game.choose_artifacts("A", ["ash_censer"])
    assert "warning" in reply and "ВНИМАНИЕ" in reply["text"] and "бюджет" in reply["warning"]
    assert "warning" not in game.choose_artifacts("A", ["tithe_chalice"])
    game.choose_artifacts("A", ["ash_censer"])
    assert "warning" in game.choose_faction("A", ["growth"])
    assert "warning" not in game.choose_faction("A", ["ash_choir"])

    draft = _setup_game(tmp_path, "draft")
    text = draft.get_setup("A")["text"]
    assert "в скобках сила" in text
    offer = draft.get_setup("A")["offer"]
    assert all(f"«{a['name']}» ({a['power']})" in text for a in offer)


def test_get_setup_after_close_is_compact(tmp_path):
    game = _setup_game(tmp_path, "draft")
    for seat in ("A", "B", "C"):
        game.set_ready(seat, None)
    closed = game.get_setup("A")
    assert not closed["open"] and closed["factions"] == [] and closed["offer"] == []
    assert closed["result"]["faction"] and len(closed["players"]) == 3
    assert len(json.dumps(closed, ensure_ascii=False)) < 2000


# ---------------------------------------------------------------- 4. cosmetic texts are not rules

def test_lore_descriptions_and_english_names_do_not_change_the_hash(tmp_path):
    core = tmp_path / "core"
    shutil.copytree(default_pack_dirs()[0], core)
    before = build_ruleset([core], "slice3").hash
    with (core / "lore" / "litany.md").open("a", encoding="utf-8") as fh:
        fh.write("\nЕщё одна строка летописи.\n")
    edit_yaml(core / "techs" / "industry.yaml",
              lambda d: [{**t, "description": "другое", "name": {"ru": t["name"]["ru"], "en": "Other"}}
                         if isinstance(t.get("name"), dict) else t for t in d])
    edit_yaml(core / "factions" / "covenant.yaml", lambda d: {**d, "lore": "другая", "description": "иное"})
    assert build_ruleset([core], "slice3").hash == before
    edit_yaml(core / "units" / "units.yaml",
              lambda d: [{**u, "attack": u["attack"] + 1} if u["id"] == "assault" else u for u in d])
    assert build_ruleset([core], "slice3").hash != before


# ---------------------------------------------------------------- 5. litany's move penalty stacks with techs

def test_litany_move_penalty_stacks_with_supply_routes():
    rs = build_ruleset(None, "slice3")
    roads = ("core:supply_routes",)
    assert move_range(rs, ["core:assault"], "core:litany") == 1
    assert move_range(rs, ["core:assault"], Profile("core:litany", roads)) == 2
    assert move_range(rs, ["core:assault"], Profile("core:covenant", roads)) == 3
    assert move_range(rs, ["core:throne_warden"], "core:litany") == 1


# ---------------------------------------------------------------- 6. the public tech count hides artifacts

def test_public_tech_count_leaves_out_artifact_grants():
    rs = build_ruleset(None, "slice3", scoring={"territory": 1})       # the tech count is in the default score line
    seats = [{"seat": "A", "nick": "А", "faction": "core:covenant"},
             {"seat": "B", "nick": "Б", "faction": "core:litany", "artifacts": ["core:smith_relic"]}]
    st = new_game_state(rs, seats, 7)
    assert st.players["B"].techs == ["core:smithcraft"]
    assert tech_counts(st, rs) == {"A": 0, "B": 0}
    rival = next(p for p in view_state(st, rs, "A")["players"] if p["seat"] == "B")
    assert rival["techs_count"] == 0
    assert view_state(st, rs, "B")["you"]["techs"] == ["core:smithcraft"]       # own: in full
    assert "тех.0" in render_state_text(st, rs, "A").split("Счёт:")[1]


# ---------------------------------------------------------------- 8/9. bridge examples and compact answers

def test_bridge_examples_use_core_ids():
    rs = build_ruleset(None, "quad4")
    texts = [EXAMPLES["choose_faction"], EXAMPLES["choose_artifacts"], EXAMPLES["describe"], RESEARCH]
    texts += [t.description for t in TOOLS if t.name in ("choose_faction", "choose_artifacts", "describe",
                                                         "list_techs")]
    ids = set()
    for text in texts:
        for args in re.findall(r"\(\w+=(\[[^\]]*\]|\"[^\"]*\")\)", text.replace('\\"', '"')):
            ids.update(json.loads(args) if args.startswith("[") else [json.loads(args)])
    ids.add(json.loads(RESEARCH)["tech"])
    assert {"litany", "captains", "ancient_blueprint", "throne_warden", "deep_drilling"} <= ids
    for i in ids:
        assert find(rs, i), i


class _StubClient:
    def __init__(self, data):
        self.data = data

    def get_setup(self):
        return self.data

    def list_techs(self):
        return self.data

    def scrub(self, text):
        return text


def test_bridge_sends_text_and_a_small_json_for_setup_and_techs():
    big = {"text": "ФАЗА СТАРТА", "phase": "setup", "open": True, "confirm_hash": "h1",
           "your_choice": {"factions": None, "artifacts": None},
           "factions": [{"id": "core:litany", "lore": "x" * 5000}], "offer": [{"id": "a", "lore": "y" * 3000}]}
    text, err = Bridge(_StubClient(big)).call("get_setup", {})
    assert not err and "ФАЗА СТАРТА" in text and "h1" in text and len(text) < 300 and "xxxx" not in text
    techs = {"text": "НАУКА", "enabled": True, "points": 3, "income": 2, "active": None, "researched": [],
             "branches": [{"id": "b"}], "techs": [{"id": "t", "description": "z" * 4000}]}
    text, err = Bridge(_StubClient(techs)).call("list_techs", {})
    assert not err and "НАУКА" in text and "zzzz" not in text and '"points":3' in text


# ---------------------------------------------------------------- 10. list_techs names what a tech unlocks

def test_techs_text_shows_unlocks():
    rs = build_ruleset(None, "slice3")
    st = new_game_state(rs, [{"seat": "A", "nick": "А", "faction": "core:covenant"},
                             {"seat": "B", "nick": "Б", "faction": "core:litany"}], 7)
    text = techs_text(techs_view(st, rs, "A"), rs)
    smith = next(line for line in text.splitlines() if "smithcraft" in line)
    assert "открывает:" in smith and rs.buildings["core:smithy"].name in smith
    assert rs.units["core:mortar"].name in smith


# ---------------------------------------------------------------- 11. factions in the state text

def test_score_line_names_factions_after_setup_only():
    rs = build_ruleset(None, "slice3")                  # core 0.5.0: the score line with breakdowns (SCORING.md 8)
    seats = [{"seat": "A", "nick": "Аль", "faction": "core:captains"},
             {"seat": "B", "nick": "Бет", "faction": "core:litany"}]
    st = new_game_state(rs, seats, 7)
    score = render_state_text(st, rs, "A").split("Счёт:")[1].splitlines()[0]
    assert "ты (Вольные Капитаны Разлома) " in score and "Бет (Литания Ржавого Престола) ≥" in score
    land = build_ruleset(None, "slice3", scoring={"territory": 1})              # the default score line
    score = render_state_text(new_game_state(land, seats, 7), land, "A").split("Счёт:")[1].splitlines()[0]
    assert "Аль (ты, Вольные Капитаны Разлома)" in score and "Бет (Литания Ржавого Престола)" in score
    st.phase = "setup"
    assert "Литания" not in render_state_text(st, rs, "A").split("Счёт:")[1]
    blitz = build_ruleset(None, "blitz_test")                       # one faction: nothing to tell apart
    st2 = new_game_state(blitz, [{"seat": "A", "nick": "Аль"}, {"seat": "B", "nick": "Бет"}], 7)
    assert "Завет" not in render_state_text(st2, blitz, "A").split("Счёт:")[1]


# ---------------------------------------------------------------- 12. always-open tools are listed

def test_every_phase_lists_rules_whoami_phase_and_wait_phase():
    for tools in [*ALLOWED.values(), READ_TOOLS]:
        assert set(ALWAYS_TOOLS) <= set(tools)


# ---------------------------------------------------------------- 13. partial names and a cheap topic miss

def test_find_accepts_a_unique_start_of_a_name(tmp_path):
    rs = build_ruleset(None, "slice3")
    assert find(rs, "Литания") == [("faction", "core:litany")]
    assert find(rs, "литан") == [("faction", "core:litany")]
    assert find(rs, "ab") == []                                    # too short to guess
    config = slice_config(game_id="g", scenario="core:slice3", seats=SEATS, seed=1)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock())
    assert game.describe("A", "Литания")["id"] == "core:litany"
    miss = game.rules("совсем-не-тема")
    assert "не найдена" in miss and "techs" in miss and "ОГЛАВЛЕНИЕ" not in miss and len(miss) < 800


@pytest.mark.parametrize("scenario", ["slice3", "quad4"])
def test_core_rules_text_fits(scenario):
    from dah.engine import RULES_LIMIT, rules_text
    assert len(rules_text(build_ruleset(None, scenario))) <= RULES_LIMIT
