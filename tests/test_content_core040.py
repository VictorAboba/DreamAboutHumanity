"""Core content 0.4.0 (docs/SPY_AND_CONQUEST.md section 5): espionage (Shade, four missions), dispositions, the
capital's conquest value, Mystery spy techs, the counter-intelligence branch «Зеркала» (decision 21) and its Hall,
faction tweaks, espionage artifacts, the scenario switches, and the frozen core 0.3.0 for older games."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from dah.engine import build_ruleset, default_pack_dirs, describe, new_game_state, techs_view, validate_packs
from dah.engine import conquest as cq
from dah.engine import espionage as esp
from dah.engine.content import DEFAULT_PACKS_DIR, LEGACY_PACKS_DIR
from dah.engine.resolve import scores
from dah.engine.setup import draft_offers
from dah.engine.techs import stat_key, techs_text_short
from dah.game import Game, make_config, pick_ruleset, replay
from dah.store import GameDir, init_game_dir
from test_review_stage2 import FakeClock, _play_out

CORE = DEFAULT_PACKS_DIR / "core"
CORE030 = LEGACY_PACKS_DIR / "core-0.3.0"
CORE040 = LEGACY_PACKS_DIR / "core-0.4.0"   # frozen when core moved to 0.5.0 (m6 fixes: lasting fortification)
CORE050 = LEGACY_PACKS_DIR / "core-0.5.0"   # frozen when core moved to 0.6.0 (m7 fixes, decision 26)
OFF = {"espionage": False, "conquest": False}
ALLOY, FOOD = "core:alloy", "core:provisions"
SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "agent"}]
STAGE3_TECHS = {"core:long_leash", "core:mirror_wards", "core:watch_drill", "core:glass_inquest"}
SPY_ARTIFACTS = {"core:faceless_mask", "core:crooked_mirror"}


@pytest.fixture(scope="module")
def on():
    return build_ruleset(None, "quad4")


@pytest.fixture(scope="module")
def off():
    return build_ruleset(None, "quad4", OFF)


def seat_state(rs, faction="core:covenant", techs=(), artifacts=()):
    st = new_game_state(rs, [{"seat": "A", "nick": "А", "faction": faction, "artifacts": list(artifacts)},
                             {"seat": "B", "nick": "Б", "faction": "core:covenant"}], 1)
    st.players["A"].techs = list(techs)
    return st


def key(rs, **kw):
    st = seat_state(rs, **kw)
    return stat_key(st, rs, "A")


# ---------------------------------------------------------------- version, freeze, switches

def test_version_and_clean_validation():
    assert "version: 0.8.3" in (CORE / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.5.0" in (CORE050 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.4.0" in (CORE040 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.3.0" in (CORE030 / "pack.yaml").read_text(encoding="utf-8")
    assert validate_packs(default_pack_dirs()) == ([], [])
    assert validate_packs([CORE030]) == ([], [])
    assert validate_packs([CORE040]) == ([], [])
    for scenario in ("slice3", "slice2", "quad4", "blitz_test", "blitz_spy", "blitz_score"):
        assert build_ruleset(None, scenario).warnings == ()


def test_frozen_core_030_is_the_old_pack():
    """content/legacy/core-0.3.0 is core 0.3.0 untouched: no stage-3 folder, field or switch."""
    assert not (CORE030 / "espionage").exists() and not (CORE030 / "dispositions").exists()
    text = "\n".join(p.read_text(encoding="utf-8") for p in CORE030.rglob("*.yaml"))
    for word in ("systems:", "requires_system", "conquest_value", "spy.", "disposition."):
        assert word not in text, word


@pytest.mark.parametrize("scenario", ["slice3", "slice2", "quad4", "blitz_test"])
def test_core_040_with_the_systems_off_is_core_030_but_for_the_version(scenario):
    old = build_ruleset([str(CORE030)], scenario).to_dict()
    new = build_ruleset([str(CORE040)], scenario, OFF).to_dict()
    assert new["packs"] == {"core": "0.4.0"} and old["packs"] == {"core": "0.3.0"}
    new["packs"] = old["packs"]
    assert new == old


@pytest.mark.parametrize("scenario", ["slice3", "slice2", "quad4", "blitz_test", "blitz_spy"])
def test_core_050_is_core_040_with_a_lasting_fortification(scenario):
    """Under the default score core 0.5.0 differs from the frozen 0.4.0 only by its version,
    combat.fortify_persists = 1 in every scenario (an unseeded stat: 0.4.0 and older games keep their hashes and
    their one-turn fortify) and the two non-scoring numbers of the 0.5.0 balance (SCORING.md «Баланс core 0.5.0»:
    the Growth's creed −30% instead of −50%, the Tithe Chalice +2 creed instead of +3); the scoring content and the
    score.* modifiers are hidden (test_content_core050.py)."""
    old = build_ruleset([str(CORE040)], scenario)
    new = build_ruleset([str(CORE050)], scenario, scoring={"territory": 1})
    assert old.stat("combat.fortify_persists") == 0 and new.stat("combat.fortify_persists") == 1
    a, b = old.to_dict(), new.to_dict()
    assert b["packs"] == {"core": "0.5.0"} and a["packs"] == {"core": "0.4.0"}
    assert old.hash != new.hash
    b["packs"] = a["packs"]
    for x, y in [(a["stats"], b["stats"]), *((a["faction_stats"][f], b["faction_stats"][f])
                                              for f in b["faction_stats"])]:
        assert y.pop("combat.fortify_persists") == 1 and "combat.fortify_persists" not in x
    a["scenario"]["overrides"] = [o for o in a["scenario"].get("overrides", ())]
    b["scenario"]["overrides"] = [o for o in b["scenario"].get("overrides", ())
                                  if o.get("target") != "combat.fortify_persists"]
    creed = "resource.core:creed.income_mul"
    if "core:growth" in b["factions"]:
        assert (a["faction_stats"]["core:growth"][creed], b["faction_stats"]["core:growth"][creed]) == \
            (pytest.approx(0.5), pytest.approx(0.7))
        b["faction_stats"]["core:growth"][creed] = a["faction_stats"]["core:growth"][creed]
        b["factions"]["core:growth"] = a["factions"]["core:growth"]
    tithe = [d["artifacts"]["core:tithe_chalice"]["effects"][0]["amount"] for d in (a, b)]
    assert tithe == [3, 2]
    b["artifacts"]["core:tithe_chalice"] = a["artifacts"]["core:tithe_chalice"]
    assert a == b


def test_a_core_040_game_replays_with_the_frozen_pack(tmp_path):
    """A game created on core 0.4.0 (stage 3a) replays and loads with content/legacy/core-0.4.0."""
    clock = FakeClock()
    config = make_config(game_id="g040", scenario="core:slice2", seats=SEATS, seed=7, turn_limit=2,
                         pack_dirs=[str(CORE040)])
    config.pop("setup", None)
    config["phase_limits"].pop("setup", None)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    _play_out(game, clock)
    old_hash = game.ruleset.hash
    raw = json.loads(gdir.config_path.read_text(encoding="utf-8"))
    raw["pack_dirs"] = None
    gdir.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    rep = replay(GameDir(gdir.path))
    assert rep.ok and [t.turn for t in rep.turns] == [1, 2]
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == "core-0.4.0"
    loaded = Game.load(GameDir(gdir.path), clock)
    assert loaded.ruleset.hash == old_hash and loaded.ruleset.stat("combat.fortify_persists") == 0


def test_scenario_switches():
    raw = {s["id"]: s for s in yaml.safe_load((CORE / "scenarios" / "scenarios.yaml").read_text(encoding="utf-8"))}
    assert all("systems" in s for s in raw.values())                 # every scenario states its switches
    for scenario in ("slice3", "slice2", "quad4"):           # core 0.6.0: Abandoned lands in slice3 and quad4
        expected = {"espionage", "conquest", "trade"} | ({"discoveries"} if scenario != "slice2" else set())   # trade: 0.8.0
        assert set(build_ruleset(None, scenario).scenario.systems) == expected
    assert raw["blitz_test"]["systems"] == {"espionage": False, "conquest": False}
    assert build_ruleset(None, "blitz_test").scenario.systems == ()
    spy = build_ruleset(None, "blitz_spy")
    assert set(spy.scenario.systems) == {"espionage", "conquest"} and spy.map.id == "core:mirror3"
    assert spy.scenario.turn_limit == 6 and spy.scenario.setup is None and spy.scenario.factions == ("core:covenant",)
    assert spy.missions and spy.dispositions
    seats = SEATS
    assert "systems" not in make_config(game_id="b", scenario="core:blitz_test", seats=seats, seed=1)
    assert make_config(game_id="s", scenario="core:slice2", seats=seats, seed=1)["systems"] == \
        {"espionage": True, "conquest": True, "trade": True}       # core 0.8.0: trade on in slice2
    off_cfg = make_config(game_id="o", scenario="core:slice2", seats=seats, seed=1, systems=OFF)
    assert off_cfg["systems"] == {"espionage": False, "conquest": False, "trade": True}


def test_a_core_030_game_replays_and_loads_with_the_frozen_pack(tmp_path):
    """A game created before core 0.4.0 (its config has no `systems`, pack_dirs null) is played under core 0.3.0:
    replay and load pick content/legacy/core-0.3.0 by the journaled hash; the current core does not match."""
    clock = FakeClock()
    config = make_config(game_id="old", scenario="core:slice2", seats=SEATS, seed=7, turn_limit=2,
                         pack_dirs=[str(CORE030)])
    config.pop("setup", None)
    config["phase_limits"].pop("setup", None)
    assert "systems" not in config
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, clock)
    _play_out(game, clock)
    old_hash = game.ruleset.hash
    assert old_hash.startswith("60fff6e3a539")                       # core 0.3.0 slice2
    raw = json.loads(gdir.config_path.read_text(encoding="utf-8"))
    raw["pack_dirs"] = None
    gdir.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    rep = replay(GameDir(gdir.path))
    assert rep.ok and [t.turn for t in rep.turns] == [1, 2] and not rep.recons
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == "core-0.3.0"
    pick = pick_ruleset(raw, old_hash)
    assert pick.matches and not pick.ruleset.scenario.systems and pick.ruleset.packs == {"core": "0.3.0"}
    loaded = Game.load(GameDir(gdir.path), clock)
    assert loaded.ruleset.hash == old_hash and not loaded.ruleset.missions
    assert loaded.admin_status()["ruleset_warning"] is None


# ---------------------------------------------------------------- espionage

MISSIONS = {   # id: (behavior, target, D, rounds, S needed, once, per round, loud)
    "observe": ("observe_province", "province", 0, (1, 5), 1, {}, {FOOD: 1}, False),     # core 0.8.0: was D1
    "ledger": ("read_ledger", "seat", 1, (1, 3), 1, {}, {FOOD: 1}, False),
    "secret": ("read_objective", "seat", 1, (1, 3), 1, {}, {FOOD: 2}, False),      # core 0.6.0
    "read_orders": ("read_orders", "seat", 2, (1, 3), 1, {}, {FOOD: 2}, False),     # core 0.8.0: was D3 at S2
    "sabotage": ("sabotage", "owned_province", 3, (1, 4), 2, {ALLOY: 8}, {FOOD: 1}, True),
}


def test_espionage_config_and_missions(on, off):
    e = on.espionage
    assert (e.shade_name, e.shade_name_en) == ("Тень", "Shade")
    # core 0.6.0 (decision 26): 20 alloy (0.4.0 and 0.5.0: 25) and one free Shade per seat
    assert e.shade_cost == {ALLOY: 20} and e.shade_upkeep == {FOOD: 1} and e.ward_cost == {ALLOY: 5}
    assert esp.shade_cost(on, None) == {ALLOY: 20} and esp.shade_upkeep(on, None) == {FOOD: 1}
    assert e.shade_free == 1 and on.stat("spy.shades_free") == 1
    assert set(on.missions) == {f"core:{m}" for m in MISSIONS}
    for mid, (behavior, target, d, rounds, s, once, per, loud) in MISSIONS.items():
        m = on.missions[f"core:{mid}"]
        assert (m.behavior, m.target, m.difficulty, (m.rounds_min, m.rounds_max), m.whisper) == \
            (behavior, target, d, rounds, s), mid
        assert (m.cost, m.cost_per_round, m.loud, m.requires_tech) == (once, per, loud, ()), mid
        assert m.name and m.name_en and m.description, mid
    assert on.missions["core:sabotage"].params == {"deny_fortify": True, "defense_mul": 0.85, "income_mul": 0,
                                                   # core 0.8.0 (docs/SPY_V2.md 4.5)
                                                   "stall_build": True, "stall_max": 1, "unrest_units": 1,
                                                   "revolt_garrison": {"core:militia": 2}}
    assert esp.mission_cost(on, "core:sabotage", 4) == {ALLOY: 8, FOOD: 4}
    assert esp.mission_cost(on, "core:read_orders", 3) == {FOOD: 6}
    assert off.espionage is None and not off.missions


def test_concept_example_with_core_numbers(on):
    """2.3: Read Orders S2 against C_loc 2, D3, V0 -> 45/20, 50/16, 55/12; survive 59%, a report 63% (catch base 5%).
    Balance core 0.8.0 (docs/SPY_V2.md «Баланс core 0.8.0»): the espionage scenarios catch from 6%, +1 point every
    round: 45/21, 50/17, 55/13, survive 57%."""
    per = [esp.chances(on, None, S=2, C=2, D=3, V=0, K=k) for k in range(3)]
    assert per == [(0.45, 0.21), (0.5, 0.17), (0.55, 0.13)]
    survive = (1 - .21) * (1 - .17) * (1 - .13)
    assert round(survive * 100) == 57
    assert [esp.chances(on, None, S=1, C=1, D=1, V=0, K=k) for k in range(3)] == \
        [(0.55, 0.11), (0.6, 0.07), (0.65, 0.03)]                 # observe, no army, C1
    assert [esp.chances(on, None, S=2, C=4, D=3, V=0, K=k)[1] for k in range(3)] == [0.45, 0.41, 0.37]


# ---------------------------------------------------------------- conquest

def test_dispositions_and_conquest_value(on, off):
    assert {k: d.behavior for k, d in on.dispositions.items()} == {
        "core:subjugate": "annex", "core:exterminate": "raze", "core:raid": "raid", "core:liberate": "release"}
    assert on.dispositions["core:exterminate"].params == {"ruin_turns": 6}
    assert on.dispositions["core:raid"].params == {"ravage_turns": 2}
    assert {t: d.conquest_value for t, d in on.province_types.items()} == {
        "core:forge": 1, "core:agri": 1, "core:waste": 1, "core:capital": 3, "core:shrine": 1}
    assert not off.dispositions
    cap = on.map.provinces[sorted(on.map.capitals)[0]]
    plain = next(p for p in on.map.provinces.values() if p.type == "core:forge")
    cost, loot = cq.disposition_cost, cq.disposition_loot
    assert cost(on, "core:subjugate", cap) == {FOOD: 15} and cost(on, "core:subjugate", plain) == {FOOD: 5}
    assert loot(on, "core:exterminate", cap) == {ALLOY: 15, FOOD: 9}
    assert loot(on, "core:raid", plain) == {ALLOY: 3, FOOD: 3}
    assert cost(on, "core:exterminate", cap) == {} and loot(on, "core:liberate", cap) == {}
    # growth: subjugation x1.2 (half up), extermination loot x1.5; captains: raid loot x1.5
    assert cost(on, "core:subjugate", cap, "core:growth") == {FOOD: 18}
    assert cost(on, "core:subjugate", plain, "core:growth") == {FOOD: 6}
    assert loot(on, "core:exterminate", cap, "core:growth") == {ALLOY: 23, FOOD: 14}
    assert loot(on, "core:raid", cap, "core:captains") == {ALLOY: 14, FOOD: 14}
    assert loot(on, "core:raid", cap, "core:growth") == {ALLOY: 9, FOOD: 9}


# ---------------------------------------------------------------- techs, Hall, factions, artifacts

def test_mystery_spy_techs(on, off):
    veil_on = [e["target"] for e in on.techs["core:whisper_veil"].effects if e["type"] == "modify"]
    veil_off = [e["target"] for e in off.techs["core:whisper_veil"].effects if e["type"] == "modify"]
    assert veil_on == ["resource.core:creed.income_mul", "spy.whisper", "spy.shades_max"]
    assert veil_off == ["resource.core:creed.income_mul"]               # the tech does not go empty without espionage
    leash = on.techs["core:long_leash"]
    assert (leash.branch, leash.tier, leash.cost) == ("core:mystery", 2, 18)
    assert leash.requires_tier == {"tier": 1, "count": 2, "same_branch": True}
    assert {t: on.techs[t].effects for t in ("core:dream_lore", "core:mirror_halls")} == \
        {t: off.techs[t].effects for t in ("core:dream_lore", "core:mirror_halls")}
    base = key(on)
    k = key(on, techs=["core:dream_lore", "core:whisper_veil", "core:long_leash"])
    assert (esp.whisper(on, base), on.stat("spy.shades_max", base), on.stat("spy.rounds_bonus", base)) == (1, 2, 0)
    assert (esp.whisper(on, k), on.stat("spy.shades_max", k), on.stat("spy.rounds_bonus", k)) == (3, 4, 1)
    # core 0.8.0: spy.active_base 2 (was 1)
    assert esp.max_rounds(on, "core:observe", k) == 6 and esp.active_limit(on, k) == 3


def test_mirrors_branch_and_hall(on, off):
    br = on.branches["core:mirrors"]
    assert (br.name, br.name_en, br.order) == ("Зеркала", "Mirrors", 5) and br.color
    assert "core:mirrors" not in off.branches and not STAGE3_TECHS & set(off.techs)
    assert "core:mirror_hall" not in off.buildings
    mirrors = {t: d for t, d in on.techs.items() if d.branch == "core:mirrors"}
    assert {t: (d.tier, d.cost) for t, d in mirrors.items()} == {
        "core:mirror_wards": (1, 10), "core:watch_drill": (1, 10), "core:glass_inquest": (2, 18)}
    assert on.techs["core:glass_inquest"].requires_tier == {"tier": 1, "count": 2, "same_branch": True}
    assert on.techs["core:mirror_wards"].unlocks == ("core:mirror_hall",)
    hall = on.buildings["core:mirror_hall"]
    assert hall.requires_tech == ("core:mirror_wards",) and on.building_cost("core:mirror_hall") == {ALLOY: 15}
    assert hall.build_turns == 1 and [m["target"] for m in hall.province_mods] == ["province.mirrors"]
    k = key(on, techs=["core:mirror_wards", "core:watch_drill", "core:glass_inquest"])
    assert (esp.mirrors(on, key(on)), esp.mirrors(on, k)) == (1, 3)
    assert on.stat("spy.detect.army", k) == 2 and on.stat("spy.attribution.base", k) == pytest.approx(0.65)
    # C_loc: own C + Hall + army (2 with the drill): up to 6 without a faction
    st = seat_state(on, techs=["core:mirror_wards", "core:watch_drill", "core:glass_inquest"])
    cap = next(p for p in st.owned("A") if p.type == "core:capital")
    assert esp.local_mirrors(st, on, cap.id) == 3 + 2
    cap.buildings.append("core:mirror_hall")
    assert esp.local_mirrors(st, on, cap.id) == 6
    cap.units = {}
    assert esp.local_mirrors(st, on, cap.id) == 4
    cap.status = {"occupied": {"by": "A", "from": "B", "since": 1}}  # an occupied Hall does not work (1.3)
    assert esp.local_mirrors(st, on, cap.id) == 3


def test_mirrors_is_the_fifth_branch_in_list_techs(on, off):
    st = seat_state(on)
    names = [on.branches[b].name for b in sorted(on.branches, key=lambda b: on.branches[b].order)]
    assert names[-1] == "Зеркала" and len(names) == 5
    assert "Зеркал" in techs_text_short(techs_view(st, on, "A"), on)
    st_off = seat_state(off)
    assert "Зеркал" not in techs_text_short(techs_view(st_off, off, "A"), off)


def test_faction_tweaks(on, off):
    assert esp.whisper(on, key(on, faction="core:synclite")) == 2             # Read Orders from turn 1
    for faction in ("core:litany", "core:ash_choir", "core:growth", "core:captains", "core:covenant"):
        assert esp.whisper(on, key(on, faction=faction)) == 1, faction
        assert esp.mirrors(on, key(on, faction=faction)) == 1, faction
    assert on.stat("disposition.core:raid.loot_mul", "core:captains") == 1.5
    assert on.stat("disposition.core:exterminate.loot_mul", "core:growth") == 1.5
    assert on.stat("disposition.core:subjugate.cost_mul", "core:growth") == 1.2
    for faction in ("core:synclite", "core:growth", "core:captains"):
        text_on, text_off = describe(on, "faction", faction)["text"], describe(off, "faction", faction)["text"]
        assert "Шёпот" not in text_off and "судьб" not in text_off, faction
        assert "Шёпот" in text_on or "судьб" in text_on, faction
    # no faction has «Зеркала» cheaper (decision 21, 5)
    assert all("tech.branch.mirrors" not in e["target"] for f in on.factions.values()
               for e in (*f.bonuses, *f.penalties) if e["type"] == "modify")
    # a Mystery tech without espionage promises nothing about spying
    assert "Шёпот" not in describe(off, "tech", "core:whisper_veil")["text"]
    assert "Шёпот" in describe(on, "tech", "core:whisper_veil")["text"]


def test_espionage_artifacts(on, off):
    assert SPY_ARTIFACTS <= set(on.artifacts) and not SPY_ARTIFACTS & set(off.artifacts)
    for aid in SPY_ARTIFACTS:
        a = on.artifacts[aid]
        assert (a.power, a.cost) == (1, 1) and a.description and not a.factions
    assert esp.whisper(on, key(on, artifacts=["core:faceless_mask"])) == 2
    assert esp.mirrors(on, key(on, artifacts=["core:crooked_mirror"])) == 2
    seats = list("ABCD")
    for seed in range(1, 30):
        offers_off = draft_offers(off, off.scenario.setup, seats, seed)
        offers_on = draft_offers(on, on.scenario.setup, seats, seed)
        assert not SPY_ARTIFACTS & {a for o in offers_off.values() for a in o}
        assert [len(o) for o in offers_off.values()] == [len(o) for o in offers_on.values()] == [3] * 4


def test_rules_texts_follow_the_switches(on, off):
    from dah.engine import rules_text
    assert len(rules_text(on)) <= 6000
    assert "ЗАВОЕВАНИЕ" in rules_text(on) and "ЗАВОЕВАНИЕ" not in rules_text(off)


@pytest.mark.parametrize("scenario,seats,artifacts", [
    ("core:slice3", 3, None), ("core:quad4", 4, None), ("core:slice2", 2, None), ("core:blitz_spy", 3, None),
    ("core:quad4", 4, "draft"), ("core:quad4", 4, "points"), ("core:quad4", 3, "draft"), ("core:quad4", 2, "draft")])
def test_game_rules_core_is_never_cut(tmp_path, scenario, seats, artifacts):
    """The real rules() of a game (with its setup and СООБЩЕНИЯ) fits 6000 without the last-resort cut, with every
    system on and every section present."""
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)]
    config = make_config(game_id="r", scenario=scenario, seats=specs, seed=1, artifacts=artifacts)
    gdir, _ = init_game_dir(tmp_path, config)
    text = Game.create(gdir, FakeClock()).rules()
    assert len(text) <= 6000 and "сокращено" not in text, len(text)
    titles = ["ПРИКАЗЫ", "ШПИОНАЖ", "ЗАВОЕВАНИЕ", "СООБЩЕНИЯ", "АРТЕФАКТЫ"]
    if scenario != "core:blitz_spy":           # one faction and no setup: factions are only a topic there
        titles.append("ФРАКЦИИ")
    for title in titles:
        assert title in text, title


# ---------------------------------------------------------------- balance invariants (SPY_AND_CONQUEST.md 9, «Баланс»)

def _simulate_module():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("dah_simulate", Path(__file__).resolve().parents[1] / "scripts"
                                                  / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate"] = module
    spec.loader.exec_module(module)
    return module


def test_balance_040_invariants(on):
    """The three 0.4.0 balance invariants, with the bases fixed in «Реализация 3a: уточнения» (шаг «исправления»):
    (1) subjugating a capital costs at most 2 turns of the average provisions income on quad4 - the average is the
    mean gross provisions income per seat and turn of 3 seeded bot games (rush, builder, diplomat, builder); (2) over
    5 turns Exterminate gives no more points than Subjugate; (3) the default survival of a Shade per mission - base
    S (or the mission's S), V0, the full longest term, a victim with base counter-intel C or C+1 (its first Mirror)
    in equal shares - is 55-80% for every mission."""
    rs = on
    capital = next(pid for pid in rs.map.capitals)
    prov = new_game_state(rs, [{"seat": "A", "nick": "А"}, {"seat": "B", "nick": "Б"}], 1).provinces[capital]
    sub = next(k for k, d in rs.dispositions.items() if d.behavior == cq.ANNEX)
    raze = next(k for k, d in rs.dispositions.items() if d.behavior == cq.RAZE)
    worst = max(cq.disposition_cost(rs, sub, prov, key(rs, faction=f)).get(FOOD, 0) for f in rs.scenario.factions)
    sim = _simulate_module()
    sim.sync_bot_numbers(rs)
    incomes = []
    for i in range(3):
        g = sim.play_game(rs, {"A": "rush", "B": "builder", "C": "diplomat", "D": "builder"}, f"inv040|{i}")
        incomes += [e["income"][FOOD] for e in g["events"] if e["kind"] == "economy"]
    average = sum(incomes) / len(incomes)
    assert worst <= 2 * average, (worst, average)

    # (2) points of the taken capital over the next 5 turns
    st = new_game_state(rs, [{"seat": "A", "nick": "А"}, {"seat": "B", "nick": "Б"}], 1)

    def points(behavior_status: dict) -> int:
        """Points the capital alone gives its taker A over turns 1..5 with this status (taken in turn 1)."""
        total = 0
        for t in range(1, 6):
            s = st.from_dict(st.to_dict())
            s.turn = t
            for p in s.provinces.values():
                p.owner = None
            s.provinces[capital].owner = "A"
            s.provinces[capital].status = dict(behavior_status)
            total += scores(s, rs)["A"]
        return total

    ruin = int(rs.dispositions[raze].params["ruin_turns"])
    subjugated = points({})
    assert subjugated == 5 * 4                                     # a full capital: 1 + 3 points a turn
    assert points({"ruined_until": 1 + ruin}) <= subjugated

    # (3) survival per mission
    S0, C0, cover = esp.whisper(rs, None), esp.mirrors(rs, None), int(rs.stat("spy.cover_max"))
    for mid, m in rs.missions.items():
        D, S = esp.difficulty(rs, mid, None), max(S0, m.whisper)
        survive = []
        for C in (C0, C0 + 1):
            alive = 1.0
            for i in range(esp.max_rounds(rs, mid, None)):
                alive *= 1 - esp.chances(rs, None, S=S, C=C, D=D, V=0, K=min(i, cover))[1]
            survive.append(alive)
        assert 0.55 <= sum(survive) / 2 <= 0.80, (mid, survive)
