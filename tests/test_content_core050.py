"""Core 0.5.0 content (SCORING.md 11): great works, the secret objectives pool, the scored scenarios and the build
validator on every core map, faction set and seat count."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dah.engine import (build_ruleset, default_pack_dirs, describe, new_game_state, render_state_text, rules_text,
                        rules_topic, validate_packs)
from dah.engine import scoring as sc
from dah.engine.content import LEGACY_PACKS_DIR
from dah.engine.registry import SCORE_SOURCES
from dah.game import Game, make_config, replay
from dah.journal import read_events
from dah.store import GameDir, init_game_dir
from test_review_stage2 import FakeClock, _play_out

CORE040 = LEGACY_PACKS_DIR / "core-0.4.0"
SCORED = ("slice3", "slice2", "quad4", "blitz_score")
PLAIN = ("blitz_test", "blitz_spy")
ALL_ON = dict.fromkeys(SCORE_SOURCES, 1)
SMALL = ("core:mirror2", "core:mirror3")
POOL = {
    # id: (kind, params, maps, players min); numbers of the 0.5.0 balance (SCORING.md «Баланс core 0.5.0»); the
    # points by difficulty of core 0.6.0 (TIERS)
    "forge_lord": ("hold_type", {"type": "core:forge", "count": 2}, (), None),
    "granary": ("hold_type", {"type": "core:agri", "count": 2}, SMALL, None),
    "shrine_keeper": ("hold_type", {"type": "core:shrine", "count": 2}, ("core:mirror4",), None),
    "dominion": ("hold_count", {"count": 6}, ("core:mirror3", "core:mirror4"), 3),
    "foreign_throne": ("capital_other", {"count": 1}, (), 3),
    "mechanicus": ("techs_branch", {"branch": "core:war", "count": 4}, SMALL, None),
    "treasurer": ("techs_branch", {"branch": "core:industry", "count": 3}, SMALL, None),
    "architect": ("great_work", {"count": 1}, (), None),
    "conqueror": ("hold_foreign", {"via": "capture", "count": 1}, (), None),     # core 0.8.0: via (TRADE.md 6.3)
    "kingbreaker": ("rival_capital_taken", {}, (), 3),
}
TIERS = {"forge_lord": "medium", "granary": "medium", "shrine_keeper": "easy", "dominion": "medium",
         "foreign_throne": "hard", "mechanicus": "medium", "treasurer": "easy", "architect": "easy",
         "conqueror": "medium", "kingbreaker": "hard"}
POINTS = {"easy": 3, "medium": 5, "hard": 7}          # core 0.6.0 (decision 26); 0.5.0: all 5


def _seats(n: int) -> list[dict]:
    return [{"seat": "ABCD"[i], "nick": f"Ник{i}"} for i in range(n)]


def test_pack_version_and_validation():
    assert "version: 0.8.3" in (Path(default_pack_dirs()[0]) / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.5.0" in (LEGACY_PACKS_DIR / "core-0.5.0" / "pack.yaml").read_text(encoding="utf-8")
    assert validate_packs(default_pack_dirs()) == ([], [])
    for sid in SCORED + PLAIN:
        assert build_ruleset(None, sid).warnings == (), sid


@pytest.mark.parametrize("sid", SCORED)
def test_scored_scenarios_weigh_every_source_once_and_deal_one_objective(sid):
    rs = build_ruleset(None, sid)
    assert rs.scenario.scoring == ALL_ON and not rs.default_scoring
    assert rs.scenario.objectives["dealt"] == rs.scenario.objectives["offer"] == 1
    assert all(rs.system(f"scoring.{src}") for src in SCORE_SOURCES)
    assert rs.stat("combat.fortify_persists") == 1 and rs.system("espionage") and rs.system("conquest")
    # the first numbers of SCORING.md 3, with the 0.5.0 balance: a tier-2 tech scores 1; core 0.7.0 (decision 27):
    # the war scale per province (cap 3, +1 per 2 turns held, 8 in all) and intel (+1 a success, at most 3)
    assert [rs.stat(s) for s in ("score.territory.province", "score.territory.capital", "score.science.tier1",
                                 "score.science.tier2", "score.science.branch", "score.war.province_cap",
                                 "score.war.hold_turns", "score.war.province_total", "score.intel.per_success",
                                 "score.intel.cap")] == [1, 3, 1, 1, 1, 3, 2, 8, 1, 3]


@pytest.mark.parametrize("sid", PLAIN)
def test_blitz_test_scenarios_keep_the_default_score(sid):
    rs = build_ruleset(None, sid)
    assert rs.default_scoring and not rs.objectives
    assert not any(b.score for b in rs.buildings.values())
    old = build_ruleset([str(CORE040)], sid)
    assert rs.to_dict()["buildings"] == old.to_dict()["buildings"] and rs.hash != old.hash


def test_blitz_score_is_the_short_all_on_scoring_game():
    rs = build_ruleset(None, "blitz_score")
    s = rs.scenario
    assert (rs.map.id, s.turn_limit, s.setup["artifacts"]) == ("core:mirror3", 6, "draft")
    assert len(s.factions) == 3 and rs.system("espionage") and rs.system("conquest")


def test_great_works_follow_the_spec():
    rs = build_ruleset(None, "quad4")
    mon, spire = rs.buildings["core:monument"], rs.buildings["core:dream_spire"]
    assert (mon.name, spire.name) == ("Монумент Предела", "Шпиль Сна")
    assert mon.cost == {"core:provisions": 25, "core:alloy": 10} and mon.build_turns == 2 and mon.score == 3
    # core 0.6.0 (decision 26): the Spire scores 7 (0.5.0: 3); balance 0.6.0: 100 + 60 (0.5.0: 150 + 50)
    assert spire.cost == {"core:provisions": 100, "core:alloy": 60} and spire.build_turns == 4 and spire.score == 7
    assert mon.max_per_owner == spire.max_per_owner == mon.max_per_province == spire.max_per_province == 1
    assert not mon.allowed_types
    assert set(spire.allowed_types) == {"core:forge", "core:agri", "core:waste", "core:shrine"}
    assert rs.stat("building.core:monument.score") == 3 and rs.stat("building.core:dream_spire.score") == 7
    assert mon.lore and spire.lore and "Монумент" in describe(rs, "building", "core:monument")["text"]


def test_works_off_hides_the_great_works_and_architect():
    rs = build_ruleset(None, "slice3", scoring={**ALL_ON, "works": 0})
    assert "core:monument" not in rs.buildings and "core:dream_spire" not in rs.buildings
    assert "core:architect" not in rs.objectives and "core:forge_lord" in rs.objectives
    land = build_ruleset(None, "slice3", scoring={"territory": 1})
    assert not land.objectives and "core:monument" not in land.buildings


def test_objectives_pool_follows_the_spec():
    small, quad = build_ruleset(None, "slice3"), build_ruleset(None, "quad4")
    assert set(small.objectives) | set(quad.objectives) == {f"core:{k}" for k in POOL}
    for oid, (kind, params, maps, pmin) in POOL.items():
        o = small.objectives.get(f"core:{oid}") or quad.objectives[f"core:{oid}"]
        got = (o.kind, dict(o.params), tuple(o.maps), o.players_min, o.difficulty, o.points)
        assert got == (kind, params, maps, pmin, TIERS[oid], POINTS[TIERS[oid]]), oid
        assert o.name and o.description
        assert (f"core:{oid}" in quad.objectives) == (not maps or "core:mirror4" in maps), oid
    assert small.objectives["core:architect"].requires_system == "scoring.works"
    assert small.objectives["core:kingbreaker"].targeted


@pytest.mark.parametrize("sid,pools", [
    ("slice2", {2: 6}), ("slice3", {2: 6, 3: 9}), ("blitz_score", {2: 6, 3: 9}), ("quad4", {2: 4, 3: 7, 4: 7})])
def test_pool_per_seat_count_on_every_core_map(sid, pools):
    rs = build_ruleset(None, sid)
    got = {n: [o for o in rs.objectives.values() if o.fits(rs.map.id, n)] for n in pools}
    assert {n: len(v) for n, v in got.items()} == pools
    for n, pool in got.items():
        assert ("core:dominion" in {o.id for o in pool}) == (n >= 3 and rs.map.id != "core:mirror2")
        assert all(sc.infeasible(rs, o, n) is None for o in pool), n


@pytest.mark.parametrize("sid", SCORED)
def test_validator_passes_for_every_faction_and_seat_count(sid):
    """The build validator (SCORING.md 2.5) on the scenario, then on every single admitted faction (a faction set
    of one is the narrowest): no objective is free at any start, all feasible for 2..max_players."""
    rs = build_ruleset(None, sid)
    assert sc.validate_objectives(rs) == []
    for faction in rs.scenario.factions:
        one = replace(rs, scenario=replace(rs.scenario, factions=(faction,)))
        assert sc.validate_objectives(one) == [], faction
        for o in one.objectives.values():
            assert sc.free_at_start(one, o) is None, (faction, o.id)


def test_every_hold_objective_needs_a_province_beyond_the_own_start():
    """Not free by design: a mirror start is capital + forge + agri + waste (4 provinces)."""
    for sid in ("slice2", "slice3", "quad4"):
        rs = build_ruleset(None, sid)
        types = {pid: p.type for pid, p in rs.map.provinces.items()}
        for start in rs.map.starts:
            own = [types[p] for p in (start.capital, *start.provinces)]
            assert sorted(own) == ["core:agri", "core:capital", "core:forge", "core:waste"]


def test_validator_rejects_a_free_or_infeasible_core_objective():
    rs = build_ruleset(None, "slice3")
    free = replace(rs.objectives["core:forge_lord"], params={"type": "core:forge", "count": 1})
    assert sc.free_at_start(rs, free)
    too_many = replace(rs.objectives["core:treasurer"], params={"branch": "core:industry", "count": 5})
    assert "industry" in sc.infeasible(rs, too_many, 3)
    no_shrine = replace(rs.objectives["core:forge_lord"], params={"type": "core:shrine", "count": 1})
    assert sc.infeasible(rs, no_shrine, 3)                      # mirror3 has no shrine
    small = replace(rs, objectives={k: v for k, v in list(rs.objectives.items())[:2]})
    assert any("at least 3" in msg for _, msg in sc.validate_objectives(small))


@pytest.mark.parametrize("sid,n", [("slice3", 3), ("slice2", 2), ("quad4", 4), ("blitz_score", 3)])
def test_every_seat_is_dealt_one_objective(sid, n):
    rs = build_ruleset(None, sid)
    for seed in range(12):
        st = new_game_state(rs, _seats(n), seed)
        targets = []
        for seat, p in st.players.items():
            assert len(p.objectives) == 1 and p.objectives[0]["id"] in rs.objectives
            if "target" in p.objectives[0]:
                assert p.objectives[0]["target"] != seat and p.objectives[0]["id"] == "core:kingbreaker"
                targets.append(p.objectives[0]["target"])
            assert p.home in rs.map.capitals
        assert len(targets) == len(set(targets))
        assert {s: p.objectives for s, p in new_game_state(rs, _seats(n), seed).players.items()} == \
            {s: p.objectives for s, p in st.players.items()}


@pytest.mark.parametrize("sid", SCORED + PLAIN)
def test_rules_core_fits_and_names_the_sources(sid):
    rs = build_ruleset(None, sid)
    text = rules_text(rs)
    assert len(text) <= 6000 and "(сокращено" not in text
    victory = next(line for line in text.splitlines() if line.startswith("ПОБЕДА"))
    assert len(victory) <= 150
    if sid in SCORED:
        # core 0.7.0: with intel the secret objective is «цель» in the 150 characters
        assert "земля+наука+стройки+война+разведка+цель" in victory
        topic = rules_topic(rs, "victory")
        assert "Монумент Предела" in topic and "Шпиль Сна" in topic and "Хозяин кузниц" in topic
    else:
        assert "тайная цель" not in victory


@pytest.mark.parametrize("scenario,seats,artifacts", [
    ("core:slice3", 3, None), ("core:quad4", 4, None), ("core:quad4", 4, "points"), ("core:slice2", 2, None),
    ("core:blitz_score", 3, None), ("core:quad4", 3, "draft")])
def test_game_rules_with_scoring_are_never_cut(tmp_path, scenario, seats, artifacts):
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)]
    config = make_config(game_id="r", scenario=scenario, seats=specs, seed=1, artifacts=artifacts)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock())
    text = game.rules()
    assert len(text) <= 6000 and "сокращено" not in text, len(text)
    # core 0.8.0 (docs/SPY_V2.md 9.1): the gloss «secret — чужая тайная цель» went to the topic
    assert "secret" in text and "rules('victory')" in text
    topic = game.rules("victory")
    assert "Шпиль Сна" in topic and "Цареубийца" in topic


def test_score_line_on_quad4_fits_with_factions():
    rs = build_ruleset(None, "quad4")
    factions = ["core:litany", "core:synclite", "core:ash_choir", "core:captains"]
    seats = [{**s, "nick": f"Длинныйник{i}", "faction": factions[i]} for i, s in enumerate(_seats(4))]
    st = new_game_state(rs, seats, 5)
    line = next(x for x in render_state_text(st, rs, "A").splitlines() if x.startswith("Счёт:"))
    assert len(line) <= sc.SCORE_LINE_LIMIT and "твоя цель" in line and not line.endswith("…")


def test_a_scored_core_game_plays_out_reveals_and_replays(tmp_path):
    clock = FakeClock()
    seats = [{"seat": "A", "nick": "Ар", "kind": "agent"}, {"seat": "B", "nick": "Бо", "kind": "agent"}]
    config = make_config(game_id="g050", scenario="core:slice2", seats=seats, seed=11, turn_limit=3)
    config.pop("setup", None)
    config["phase_limits"].pop("setup", None)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    _play_out(game, clock)
    types = [e["type"] for e in read_events(gdir.journal_path)]
    assert types.count("objectives_dealt") == 2 and types.count("objectives_revealed") == 2
    over = next(e for e in read_events(gdir.journal_path) if e["type"] == "game_over")
    assert set(over["score_parts"]) == {"A", "B"} and set(over["objectives"]) == {"A", "B"}
    raw = json.loads(gdir.config_path.read_text(encoding="utf-8"))
    raw["pack_dirs"] = None
    gdir.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    rep = replay(GameDir(gdir.path))
    assert rep.ok and [t.turn for t in rep.turns] == [1, 2, 3]
    assert not rep.pack_dirs or "legacy" not in str(rep.pack_dirs[0])


def test_spectator_live_caches_the_start_breakdown_of_a_scored_game(tmp_path, monkeypatch):
    from dah.server import spectate
    seats = [{"seat": "A", "nick": "Ар", "kind": "agent"}, {"seat": "B", "nick": "Бо", "kind": "agent"}]
    config = make_config(game_id="g", scenario="core:slice2", seats=seats, seed=3)
    config.pop("setup", None)
    config["phase_limits"].pop("setup", None)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock())
    cache = spectate.GameCache()
    first = spectate.live(game, cache, "admin", spectate.LIVE)["scores_history"]
    calls = []
    original = GameDir.load_turn

    def counting(self, kind, turn):
        calls.append((kind, turn))
        return original(self, kind, turn)

    monkeypatch.setattr(GameDir, "load_turn", counting)
    assert spectate.live(game, cache, "admin", spectate.LIVE)["scores_history"] == first
    assert ("states", 1) not in calls
