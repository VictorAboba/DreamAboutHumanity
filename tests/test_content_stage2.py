"""Core content of stage 2 (core 0.3.0, TECH_AND_SETUP.md section 3): factions, units, buildings, techs, artifacts,
the mirror4 map and the scenarios. Everything is pack data; these tests check that it is complete and consistent.
Core 0.4.0 keeps all of it and adds stage-3 content (tests/test_content_core040.py)."""

from __future__ import annotations

import json
import math
from collections import Counter

import pytest

from dah.cli import main
from dah.engine import (SeatOrders, build_ruleset, default_pack_dirs, describe, draft_offers, issue_order_id,
                        new_game_state, render_report_text, render_state_text, resolve, rules_text, state_hash,
                        submit_orders, techs_view, validate_packs)
from dah.engine.content import DEFAULT_PACKS_DIR, artifact_power_guess
from dah.engine.rng import stream
from dah.engine.techs import building_problem, unit_problem
from dah.game import GameDir, make_config

CORE = DEFAULT_PACKS_DIR / "core"
CONCEPT_FACTIONS = ["core:litany", "core:synclite", "core:ash_choir", "core:growth", "core:captains"]
NEUTRAL = "core:covenant"
SCENARIOS = ["slice3", "slice2", "quad4", "blitz_test"]


@pytest.fixture(scope="module")
def rs():
    return build_ruleset(None, "quad4")


def unique_units(rs, faction):
    return [u for u, d in rs.units.items() if d.factions == (faction,)]


# ---------------------------------------------------------------- the pack builds and validates


def test_pack_version_and_clean_validation():
    assert "version: 0.7.1" in (CORE / "pack.yaml").read_text(encoding="utf-8")
    assert validate_packs(default_pack_dirs()) == ([], [])
    for scenario in SCENARIOS:
        built = build_ruleset(None, scenario)
        assert built.warnings == () and built.packs == {"core": "0.7.1"}
    assert len({build_ruleset(None, s).hash for s in SCENARIOS}) == len(SCENARIOS)


def test_lore_files_are_short_and_used():
    lore = sorted((CORE / "lore").glob("*.md"))
    assert lore
    texts = "\n".join(p.read_text(encoding="utf-8") for p in CORE.rglob("*.yaml"))
    for path in lore:
        assert 0 < len(path.read_text(encoding="utf-8")) <= 600, path.name
        assert f"lore/{path.name}" in texts, f"{path.name} is not referenced"


def test_names_have_russian_and_english(rs):
    for table in (rs.techs, rs.branches, rs.artifacts):
        for d in table.values():
            assert d.name and d.name_en and d.name != d.name_en, d.id
    raw = "\n".join(p.read_text(encoding="utf-8") for p in (CORE / "factions").glob("*.yaml"))
    assert raw.count("en: ") >= len(rs.factions)


# ---------------------------------------------------------------- factions


def test_factions_from_concept_plus_neutral(rs):
    assert set(rs.factions) == {*CONCEPT_FACTIONS, NEUTRAL}
    names = {f: rs.factions[f].name for f in rs.factions}
    assert names["core:litany"] == "Литания Ржавого Престола" and names["core:captains"] == "Вольные Капитаны Разлома"
    neutral = rs.factions[NEUTRAL]
    assert rs.faction_mods.get(NEUTRAL, ()) == () and not neutral.income and not unique_units(rs, NEUTRAL)


@pytest.mark.parametrize("faction", CONCEPT_FACTIONS)
def test_every_faction_is_asymmetric(rs, faction):
    """A unique unit, a cheap tech branch, a bonus and a drawback — all through data."""
    assert len(unique_units(rs, faction)) == 1
    cheap = [b for b in rs.branches if rs.stat(f"tech.branch.{b}.cost_mul", faction) < 1.0]
    assert len(cheap) == 1, cheap
    for t in rs.techs:
        if rs.techs[t].branch == cheap[0]:
            assert rs.tech_cost(t, faction) < rs.tech_cost(t, NEUTRAL)
    raw = (CORE / "factions" / f"{faction.split(':')[1]}.yaml").read_text(encoding="utf-8")
    assert "penalties:\n  - " in raw and "bonuses:\n  - " in raw
    assert rs.factions[faction].description
    assert len(rs.faction_mods[faction]) + len(rs.factions[faction].income) >= 3


def test_faction_numbers(rs):
    st = rs.stat
    assert st("unit.core:assault.move", "core:litany") == 1 and st("unit.core:assault.move", NEUTRAL) == 2
    assert st("resource.core:alloy.income_mul", "core:litany") == pytest.approx(1.1)
    assert rs.building_cost("core:citadel", "core:litany") == {"core:alloy": 32}
    assert st("command_points", "core:synclite") == 4 and st("unit.core:armor.attack", "core:synclite") == 4
    assert st("resource.core:creed.income_mul", "core:ash_choir") == pytest.approx(1.5)
    assert rs.unit_cost("core:dreamwalker", "core:ash_choir") == {"core:alloy": 10}    # 14 x 0.7; no creed
    assert rs.unit_cost("core:militia", "core:growth") == {"core:provisions": 4}
    assert st("resource.core:creed.income_mul", "core:growth") == pytest.approx(0.7)   # core 0.5.0 balance
    assert st("p2.hasty_penalty", "core:captains") == 0 and st("province.defense_mul", "core:captains") == 0.9
    assert rs.factions["core:captains"].income == {"core:alloy": 2}


def test_balance_030_invariants(rs):
    """The balance pass of core 0.3.0 (docs/SLICE.md, «Баланс core 0.3.0»): no unique unit counters the basic line
    unit (assault), no faction changes assault (the unit every army is built from), and the war tech makes assault
    hit harder (attack beats defence, as in 0.2.0) rather than hold better."""
    assault = "core:assault"
    for u, d in rs.units.items():
        if d.factions:
            assert assault not in d.counters, u
    for faction in rs.factions:
        for stat in ("attack", "defense"):
            assert rs.stat(f"unit.{assault}.{stat}", faction) == rs.stat(f"unit.{assault}.{stat}"), (faction, stat)
    drill = [e["target"] for e in rs.techs["core:drill_doctrine"].effects]
    assert "unit.core:assault.attack" in drill and "unit.core:assault.defense" not in drill
    home = rs.stat("unit.core:throne_warden.defense") + rs.stat("unit.core:throne_warden.ability.garrison.defense")
    assert home <= 5   # the Throne Warden at home: 0.3.0 had 5 + 2 and countered assault (Litany won 60-85%)


def test_starting_armies_are_comparable(rs):
    """Every faction starts with the barracks, 30 alloy and an army worth about the neutral 4 assault."""
    base = sum(rs.unit_cost(u)["core:alloy"] * n for u, n in rs.factions[NEUTRAL].start_units.items())
    for fid, f in rs.factions.items():
        assert f.start_buildings == ("core:barracks",) and f.start_resources["core:alloy"] == 30
        worth = sum(sum(rs.unit_cost(u).values()) * n for u, n in f.start_units.items())
        assert 0.8 * base <= worth <= 1.25 * base, (fid, worth)
        assert 4 <= sum(f.start_units.values()) <= 5


# ---------------------------------------------------------------- units and buildings


def test_units_common_and_unique(rs):
    common = [u for u, d in rs.units.items() if not d.factions]
    unique = [u for u, d in rs.units.items() if d.factions]
    assert 6 <= len(common) <= 8 and len(unique) == 5
    assert {"core:assault", "core:armor", "core:artillery", "core:militia"} <= set(common)
    assert sorted(f for u in unique for f in rs.units[u].factions) == sorted(CONCEPT_FACTIONS)
    for u, d in rs.units.items():
        for target in d.counters:
            assert target in rs.units
        assert 1 <= rs.stat(f"unit.{u}.move") <= 5


def test_heavy_units_need_the_smithy(rs):
    heavy = [u for u, d in rs.units.items() if "core:smithy" in d.requires_building]
    assert sorted(heavy) == ["core:mortar", "core:titan"]
    assert rs.buildings["core:smithy"].requires_tech == ("core:smithcraft",)
    assert rs.units["core:titan"].requires_tech == ("core:war_engines",)


def test_stage2_buildings(rs):
    names = {b: rs.buildings[b].name for b in rs.buildings}
    for bid, name in [("temple", "Храм"), ("smithy", "Кузня"), ("citadel", "Цитадель"), ("warehouse", "Склад"),
                      ("hq", "Штаб")]:
        assert names[f"core:{bid}"] == name
    assert rs.buildings["core:temple"].income == {"core:creed": 3}
    assert rs.province_types["core:shrine"].income == {"core:creed": 3, "core:provisions": 1}
    assert rs.province_types["core:capital"].income["core:creed"] == 3   # balance core 0.3.0
    assert rs.buildings["core:hq"].recruit
    assert [m["target"] for m in rs.buildings["core:citadel"].province_mods] == ["province.defense_mul"]


def test_every_unit_and_building_is_obtainable_by_every_allowed_faction(rs):
    """With all techs researched (and a Smithy where needed), every common unit and building is open to every
    faction, and every unique unit to its own faction only."""
    st = new_game_state(rs, [{"seat": "A", "nick": "А", "faction": "litany"}], 1)
    cap = next(p for p in st.owned("A") if p.type == "core:capital")
    cap.buildings = sorted({*cap.buildings, "core:smithy"})
    for faction in rs.factions:
        st.players["A"].faction = faction
        st.players["A"].techs = sorted(rs.techs)
        for u, d in rs.units.items():
            problem = unit_problem(st, rs, "A", u, cap.id)
            assert (problem is None) == (not d.factions or faction in d.factions), (faction, u, problem)
        for b in rs.buildings:
            assert building_problem(st, rs, "A", b) is None, (faction, b)
        st.players["A"].techs = []
        gated = {u for u, d in rs.units.items() if d.requires_tech}
        assert all(unit_problem(st, rs, "A", u, cap.id) for u in gated)


# ---------------------------------------------------------------- techs


def test_tech_tree_shape(rs):
    assert [rs.branches[b].name for b in sorted(rs.branches, key=lambda b: rs.branches[b].order)] == \
        ["Промышленность", "Война", "Логистика", "Тайна", "Зеркала"]     # «Зеркала»: espionage only
    assert all(rs.branches[b].color for b in rs.branches)
    assert 15 <= len(build_ruleset(None, "blitz_test").techs) <= 18 and len(rs.techs) <= 22
    tiers = Counter((t.branch, t.tier) for t in rs.techs.values())
    assert {tier for _, tier in tiers} == {1, 2}
    for b in rs.branches:
        assert tiers[(b, 1)] >= 2 and tiers[(b, 2)] >= 1, b


def test_tier_two_needs_two_tier_one_techs_of_its_branch(rs):
    for t in rs.techs.values():
        if t.tier == 2:
            assert t.requires_tier == {"tier": 1, "count": 2, "same_branch": True}, t.id
            assert all(rs.techs[r].branch == t.branch for r in t.requires)
        else:
            assert not t.requires and not t.requires_any and not t.requires_tier, t.id
        stage3 = t.id in ("core:long_leash", "core:glass_inquest")     # SPY_AND_CONQUEST.md section 5: 18
        assert t.cost >= (18 if stage3 else 20 if t.tier == 2 else 10)


@pytest.mark.parametrize("faction", [*CONCEPT_FACTIONS, NEUTRAL])
def test_every_tech_is_reachable(rs, faction):
    """Research what the tree says is available, one at a time: every tech gets researched."""
    st = new_game_state(rs, [{"seat": "A", "nick": "А", "faction": faction}], 1)
    while True:
        view = techs_view(st, rs, "A")
        available = [t["id"] for t in view["techs"] if t["status"] == "available"]
        if not available:
            break
        st.players["A"].techs.append(available[0])
    assert sorted(st.players["A"].techs) == sorted(rs.techs)


def test_tech_unlocks_point_at_gated_content(rs):
    for t in rs.techs.values():
        for ref in t.unlocks:
            gated = rs.units.get(ref) or rs.buildings.get(ref)
            assert gated is not None and t.id in gated.requires_tech, (t.id, ref)
    for table in (rs.units, rs.buildings):
        for d in table.values():
            for tech in d.requires_tech:
                assert d.id in rs.techs[tech].unlocks, (d.id, tech)


def test_mystery_branch_gives_only_stats_and_flags(rs):
    for t in rs.techs.values():
        if t.branch == "core:mystery":
            assert {e["type"] for e in t.effects} <= {"modify", "income", "set_flag"}


# ---------------------------------------------------------------- artifacts


def test_artifacts_pool(rs):
    # core 0.4.0: two espionage artifacts join the pool only while espionage is on
    assert 12 <= len(build_ruleset(None, "blitz_test").artifacts) <= 14 and len(rs.artifacts) <= 16
    for a in rs.artifacts.values():
        assert 1 <= a.cost <= 3 and 1 <= a.power <= 3, a.id
        assert a.description, a.id
        for other in a.exclusive_with:
            assert a.id in rs.artifacts[other].exclusive_with
    restricted = [a for a in rs.artifacts.values() if a.factions]
    assert [a.id for a in restricted] == ["core:ash_censer"]


def test_artifact_power_matches_effects(rs):
    for a in rs.artifacts.values():
        guess = artifact_power_guess(a.effects, rs.units, rs.buildings, rs.techs)
        assert abs(a.power - guess) <= 1.0, (a.id, a.power, guess)
        assert abs(a.cost - a.power) <= 1, a.id


@pytest.mark.parametrize("scenario,seats", [("slice2", "AB"), ("slice3", "ABC"), ("quad4", "ABCD")])
def test_draft_offers_are_fair(scenario, seats):
    built = build_ruleset(None, scenario)
    for seed in range(5):
        offers = draft_offers(built, built.scenario.setup, list(seats), seed)
        assert all(len(o) == 3 for o in offers.values())
        totals = [sum(built.artifacts[a].power for a in o) for o in offers.values()]
        assert max(totals) - min(totals) <= 1, (seed, offers)


def test_describe_covers_new_content(rs):
    for kind, table in (("faction", rs.factions), ("unit", rs.units), ("building", rs.buildings),
                        ("tech", rs.techs), ("artifact", rs.artifacts)):
        for cid in table:
            assert describe(rs, kind, cid)["text"], (kind, cid)


# ---------------------------------------------------------------- map and scenarios


def test_mirror4_is_symmetric_under_rotation(rs):
    m = rs.map
    assert m.id == "core:mirror4" and m.max_players == 4 and len(m.starts) == 4 and len(m.provinces) == 21
    types = Counter(p.type for p in m.provinces.values())
    assert types == {"core:capital": 4, "core:forge": 5, "core:agri": 4, "core:waste": 4, "core:shrine": 4}
    for start in m.starts:
        assert m.provinces[start.capital].type == "core:capital"
        assert sorted(m.provinces[p].type for p in start.provinces) == ["core:agri", "core:forge", "core:waste"]
    assert not m.layout_computed and set(m.layout) == set(m.provinces)
    by_pos = {tuple(xy): p for p, xy in m.layout.items()}
    assert len(by_pos) == len(m.provinces)
    rot = {p: by_pos[(100 - y, x)] for p, (x, y) in m.layout.items()}
    assert all(m.provinces[p].type == m.provinces[rot[p]].type for p in m.provinces)
    edges = {frozenset((a, b)) for a in m.adjacency for b in m.adjacency[a]}
    assert {frozenset(rot[x] for x in e) for e in edges} == edges


def test_scenarios_setup_and_factions():
    for scenario, n in (("slice3", 3), ("slice2", 2)):
        built = build_ruleset(None, scenario)
        assert built.scenario.factions[0] == NEUTRAL                # no choice: the neutral faction, as in the slice
        assert set(built.scenario.factions) == {*CONCEPT_FACTIONS, NEUTRAL} and built.map.max_players == n
        assert built.scenario.setup["artifacts"] == "draft" and built.scenario.phase_limits["setup"] == 300
    quad = build_ruleset(None, "quad4")
    assert quad.map.id == "core:mirror4" and quad.scenario.setup["unique_factions"]
    assert quad.scenario.factions[:5] == tuple(CONCEPT_FACTIONS)
    blitz = build_ruleset(None, "blitz_test")
    assert blitz.scenario.setup is None and blitz.scenario.factions == (NEUTRAL,)
    assert len(rules_text(quad)) <= 6000


def test_slice_scenarios_create_games_with_a_setup_phase(tmp_path):
    seats = [{"seat": "A", "nick": "Альфа", "kind": "agent"}, {"seat": "B", "nick": "Бета", "kind": "agent"}]
    config = make_config(game_id="s", scenario="core:slice2", seats=seats, seed=1)
    assert config["setup"]["artifacts"] == "draft" and config["phase_limits"]["setup"] == 300
    assert "setup" not in make_config(game_id="b", scenario="core:blitz_test", seats=seats, seed=1)
    assert main(["game", "create", "--scenario", "core:quad4", "--seat", "A:agent:Альфа", "--seat", "B:agent:Бета",
                 "--seat", "C:agent:Гамма", "--seat", "D:agent:Дельта", "--seed", "3", "--id", "q4",
                 "--games-dir", str(tmp_path)]) == 0
    assert GameDir(tmp_path / "q4").load_config()["setup"]["artifacts"] == "draft"


# ---------------------------------------------------------------- whole game with every faction


def scripted_orders(state, rs, seat, seed):
    """Research the first available tech; recruit, build and move at random among what is allowed."""
    rng = stream(seed, state.turn, "content_bot", seat)
    items: list[dict] = []
    view = techs_view(state, rs, seat)
    if view["active"] is None:
        available = [t["id"] for t in view["techs"] if t["status"] == "available"]
        if available:
            items.append({"kind": "research", "tech": rng.choice(available)})
    for prov in state.owned(seat):
        if any(rs.buildings[b].recruit for b in prov.buildings):
            units = [u for u in sorted(rs.units) if unit_problem(state, rs, seat, u, prov.id) is None]
            items.append({"kind": "recruit", "province": prov.id, "unit": rng.choice(units), "count": 1})
        if prov.units and rng.random() < 0.6:
            unit = rng.choice(sorted(prov.units))
            items.append({"kind": "move", "from": prov.id, "to": rng.choice(rs.map.adjacency[prov.id]),
                          "units": {unit: prov.units[unit]}})
        buildings = [b for b in sorted(rs.buildings) if b not in prov.buildings
                     and building_problem(state, rs, seat, b) is None]
        if buildings and rng.random() < 0.4:
            items.append({"kind": "build", "province": prov.id, "building": rng.choice(buildings)})
    return submit_orders(state, rs, seat, SeatOrders(), items, "actions",
                         id_factory=lambda: issue_order_id(state, seat)).book


def play_quad(seed):
    rs = build_ruleset(None, "quad4")
    offers = draft_offers(rs, rs.scenario.setup, list("ABCD"), seed)
    seats = [{"seat": s, "nick": f"Бот{s}", "faction": f, "artifacts": offers[s][:1]}
             for s, f in zip("ABCD", ["litany", "synclite", "growth", "captains"])]
    state = new_game_state(rs, seats, seed)
    hashes, completed = [], Counter()
    while state.phase != "game_over":
        books = {seat: scripted_orders(state, rs, seat, seed) for seat, p in state.players.items() if p.alive}
        before = state
        state, report = resolve(state, books, rs, seed)
        hashes.append(state_hash(state))
        json.dumps(report.to_dict())
        completed.update(e["tech"] for e in report.events if e["kind"] == "tech_completed")
        for seat in state.players:
            render_report_text(report, rs, state, seat)
            render_state_text(state, rs, seat, since_turn=before.turn, since_state=before, reports=[report])
    return state, hashes, completed


def test_quad4_game_with_factions_artifacts_and_research_is_deterministic():
    end1, h1, done = play_quad(11)
    end2, h2, _ = play_quad(11)
    assert h1 == h2 and end1.phase == "game_over" and end1.result is not None
    assert len(h1) == 12 or any(not p.alive for p in end1.players.values())
    assert sum(done.values()) >= 4                       # research progresses with capital and shrine creed
    assert all(math.isfinite(v) for p in end1.players.values() for v in p.resources.values())
