"""Stage-2 content: techs, branches, artifacts, new unit/building fields, schemas and validator (TECH_AND_SETUP.md)."""

from __future__ import annotations

import pytest

from dah.engine import ContentError, Profile, build_ruleset, default_pack_dirs, validate_packs
from stage2_helpers import CORE020, CORE020_HASHES, PACK, PACKS, edit_yaml, pack_copy, ruleset


def messages(issues) -> list[str]:
    return [f"{i.path}: {i.message}" for i in issues]


def test_fixture_pack_is_valid():
    errors, warnings = validate_packs(PACKS)
    assert errors == [] and warnings == []


def test_core_without_stage2_content_hashes_without_new_keys():
    rs = build_ruleset(CORE020, "slice3")
    data = rs.to_dict()
    assert not {"techs", "branches", "artifacts"} & set(data)
    assert all("move" not in u and "abilities" not in u for u in data["units"].values())
    assert "setup" not in data["scenario"]
    assert rs.scenario.setup is None and rs.research_resource is None
    for scenario, prefix in CORE020_HASHES.items():     # the hashes of core 0.2.0: old games replay with it
        assert build_ruleset(CORE020, scenario).hash.startswith(prefix)
    assert validate_packs(CORE020) == ([], [])
    assert validate_packs(default_pack_dirs())[0] == []


def test_stage2_entities_are_loaded():
    rs = ruleset()
    assert rs.roles["research"] == "t2:creed" and rs.research_resource == "t2:creed"
    assert [b.id for b in sorted(rs.branches.values(), key=lambda b: b.order)] == ["t2:war", "t2:economy"]
    assert rs.branches["t2:war"].name_en == "War" and rs.branches["t2:war"].color == "#aa3333"
    doctrine = rs.techs["t2:doctrine"]
    assert doctrine.requires_tier == {"tier": 1, "count": 2, "same_branch": True}
    banking = rs.techs["t2:banking"]
    assert banking.requires == ("t2:drills",) and banking.requires_any == ("t2:smithing", "t2:fortification")
    assert rs.techs["t2:smithing"].unlocks == ("t2:knight", "t2:smithy")
    # `effect:` (TECH_AND_SETUP.md) is the same key as `type:` (slice packs)
    assert rs.techs["t2:drills"].effects[0]["type"] == "modify"
    assert rs.techs["t2:drills"].effects[0]["target"] == "resource.t2:alloy.income_mul"
    assert rs.units["t2:ram"].move == 1 and rs.units["t2:ram"].abilities == {"ignore_fortify": {}, "siege": {"cap": 1.0}}
    assert rs.units["t2:knight"].requires_tech == ("t2:smithing",)
    assert rs.units["t2:knight"].requires_building == ("t2:smithy",)
    assert rs.units["t2:raider"].factions == ("t2:iron",)
    assert rs.units["t2:knight"].name == "Рыцарь"          # {ru, en} names: ru is shown
    citadel = rs.buildings["t2:citadel"]
    assert [m["target"] for m in citadel.province_mods] == ["province.defense_mul"]
    assert [m["target"] for m in citadel.owner_mods] == ["command_points"]
    assert rs.buildings["t2:temple"].income == {"t2:creed": 3}
    assert rs.factions["t2:iron"].lore.startswith("Железный Обет")      # lore read from lore/iron.md
    assert rs.artifacts["t2:seal"].exclusive_with == ("t2:cache",)
    assert rs.artifacts["t2:banner"].factions == ("t2:iron",)


def test_ability_and_move_stats_come_from_the_pack_with_registry_defaults():
    rs = ruleset()
    assert rs.stat("unit.t2:ram.move") == 1 and rs.stat("unit.t2:militia.move") == 2
    assert rs.stat("unit.t2:ram.ability.siege.on") == 1 and rs.stat("unit.t2:ram.ability.siege.cap") == 1.0
    assert rs.stat("unit.t2:guard.ability.garrison.defense") == 2
    assert rs.stat("unit.t2:raider.ability.fast.range") == 2          # default of the parameter
    assert rs.stat("unit.t2:militia.ability.fast.on") == 0
    assert rs.stat("province.defense_mul") == 1.0 and rs.stat("province.income_mul.t2:alloy") == 1.0


def test_tech_cost_uses_tech_and_branch_stats():
    rs = ruleset()
    assert rs.tech_cost("t2:smithing") == 6
    assert rs.tech_cost("t2:smithing", "t2:iron") == 3              # iron: war branch x0.5
    assert rs.tech_cost("t2:drills", "t2:iron") == 5
    assert rs.tech_cost("t2:smithing", "t2:ash") == 6


def test_profile_stats_add_tech_artifact_and_owner_building_modifiers():
    rs = ruleset()
    plain = Profile("t2:ash")
    assert rs.stat("command_points", plain) == rs.stat("command_points", "t2:ash") == 3
    full = Profile("t2:ash", techs=("t2:doctrine", "t2:drills"), artifacts=("t2:seal",),
                   buildings=(("t2:citadel", 2),))
    assert rs.stat("command_points", full) == 3 + 1 + 1 + 2
    assert rs.stat("resource.t2:alloy.income_mul", full) == pytest.approx(1.25 * 1.5)
    assert rs.passive_income(Profile("t2:ash", techs=("t2:banking",), artifacts=("t2:tithe",))) == {
        "t2:alloy": 2, "t2:creed": 2}
    assert rs.unlocks(Profile("t2:iron", artifacts=("t2:banner",))) == {"unit:t2:knight"}


def test_research_role_is_optional_but_unique(tmp_path):
    assert build_ruleset(CORE020, "slice2").research_resource is None
    pack = pack_copy(tmp_path)
    edit_yaml(pack / "resources" / "resources.yaml",
              lambda d: d + [{"id": "faith", "name": "Вера", "role": "research"}])
    errors, _ = validate_packs([pack])
    assert any("role 'research' takes at most one resource" in m for m in messages(errors))


def test_techs_without_research_role_warn(tmp_path):
    pack = pack_copy(tmp_path)
    edit_yaml(pack / "resources" / "resources.yaml", lambda d: [r for r in d if r["id"] != "creed"])
    for f in ("province_types/types.yaml",):
        edit_yaml(pack / f, lambda d: [{**t, "income": {k: v for k, v in t["income"].items() if k != "creed"}}
                                       for t in d])
    edit_yaml(pack / "buildings" / "buildings.yaml",
              lambda d: [b for b in d if b["id"] != "temple"])
    edit_yaml(pack / "techs" / "techs.yaml", lambda d: [t for t in d if t["id"] != "iron_rite"])
    edit_yaml(pack / "artifacts" / "artifacts.yaml", lambda d: [a for a in d if a["id"] != "tithe"])
    errors, warnings = validate_packs([pack])
    assert errors == []
    assert any("no resource has role 'research'" in m for m in messages(warnings))


@pytest.mark.parametrize("change, expected", [
    (lambda d: d + [{"id": "x", "name": "Икс", "branch": "nowhere", "tier": 1, "cost": 3}], "unknown branch"),
    (lambda d: d + [{"id": "a", "name": "А", "branch": "war", "tier": 1, "cost": 1, "requires": ["b"]},
                    {"id": "b", "name": "Б", "branch": "war", "tier": 1, "cost": 1, "requires": ["a"]}],
     "requirement loop"),
    (lambda d: d + [{"id": "s", "name": "С", "branch": "war", "tier": 1, "cost": 1, "requires": ["s"]}],
     "cannot require itself"),
    (lambda d: d + [{"id": "c", "name": "Ц", "branch": "war", "tier": 1, "cost": 5000}], "out of bounds"),
    (lambda d: d + [{"id": "e", "name": "Э", "branch": "war", "tier": 1, "cost": 1,
                     "effects": [{"effect": "add_resource", "resource": "alloy", "amount": 5}]}],
     "not allowed in tech"),
    (lambda d: d + [{"id": "u", "name": "У", "branch": "war", "tier": 1, "cost": 1,
                     "effects": [{"effect": "unlock", "kind": "unit", "id": "dragon"}]}], "unknown unit"),
    (lambda d: d + [{"id": "o", "name": "О", "branch": "war", "tier": 1, "cost": 1,
                     "effects": [{"effect": "unlock", "kind": "order", "id": "fly"}]}], "unknown order kind"),
    (lambda d: d + [{"id": "m", "name": "М", "branch": "war", "tier": 1, "cost": 1,
                     "effects": [{"effect": "modify", "target": "command_points", "op": "add", "value": 1,
                                  "scope": "owner"}]}], "scope is allowed only"),
])
def test_tech_errors(tmp_path, change, expected):
    pack = pack_copy(tmp_path)
    edit_yaml(pack / "techs" / "techs.yaml", change)
    errors, _ = validate_packs([pack])
    assert any(expected in m for m in messages(errors)), messages(errors)
    with pytest.raises(ContentError):
        build_ruleset([pack], "duel_draft")


@pytest.mark.parametrize("abilities, expected", [
    (["teleport"], "unknown ability"),
    ([{"id": "siege", "power": 2}], "has no parameter"),
    ([{"id": "garrison", "defense": 99}], "out of bounds"),
    (["fast", "fast"], "given twice"),
])
def test_ability_errors(tmp_path, abilities, expected):
    pack = pack_copy(tmp_path)
    edit_yaml(pack / "units" / "units.yaml",
              lambda d: [{**u, "abilities": abilities} if u["id"] == "militia" else u for u in d])
    errors, _ = validate_packs([pack])
    assert any(expected in m for m in messages(errors)), messages(errors)


@pytest.mark.parametrize("effect, expected", [
    ({"effect": "modify", "target": "province.defense_mul", "op": "add", "value": 0.5}, "needs scope"),
    ({"effect": "modify", "target": "command_points", "op": "add", "value": 1, "scope": "province"},
     "must be a province.* stat"),
    ({"effect": "modify", "target": "province.defense_mul", "op": "add", "value": 1, "scope": "world"},
     "needs scope"),
    ({"effect": "modify", "target": "province.income_mul.gold", "op": "mul", "value": 2, "scope": "province"},
     "unknown resource"),
])
def test_building_modify_scope_errors(tmp_path, effect, expected):
    pack = pack_copy(tmp_path)
    edit_yaml(pack / "buildings" / "buildings.yaml",
              lambda d: [{**b, "effects": [effect]} if b["id"] == "temple" else b for b in d])
    errors, _ = validate_packs([pack])
    assert any(expected in m for m in messages(errors)), messages(errors)


def test_unit_and_building_requirement_refs_are_checked(tmp_path):
    pack = pack_copy(tmp_path)
    edit_yaml(pack / "units" / "units.yaml",
              lambda d: [{**u, "requires": {"tech": ["alchemy"]}, "factions": ["elves"]} if u["id"] == "militia"
                         else u for u in d])
    errors, _ = validate_packs([pack])
    found = messages(errors)
    assert any("unknown tech 'alchemy'" in m for m in found) and any("unknown faction 'elves'" in m for m in found)


def test_artifact_power_is_checked_against_effects(tmp_path):
    pack = pack_copy(tmp_path)
    edit_yaml(pack / "artifacts" / "artifacts.yaml",
              lambda d: [{**a, "power": 9} if a["id"] == "warband" else a for a in d])
    errors, warnings = validate_packs([pack])
    assert errors == []
    assert any("power 9 looks off" in m for m in messages(warnings))


def test_artifact_schema_and_exclusive_refs(tmp_path):
    pack = pack_copy(tmp_path)
    edit_yaml(pack / "artifacts" / "artifacts.yaml",
              lambda d: d + [{"id": "bad", "name": "Плохой", "power": 1, "exclusive_with": ["bad", "nothing"],
                              "effects": [{"effect": "grant_tech", "tech": "nothing"}]}])
    found = messages(validate_packs([pack])[0])
    assert any("cannot exclude itself" in m for m in found)
    assert any("unknown artifact 'nothing'" in m for m in found)
    assert any("unknown tech 'nothing'" in m for m in found)


def test_missing_lore_file_warns(tmp_path):
    pack = pack_copy(tmp_path)
    (pack / "lore" / "iron.md").unlink()
    errors, warnings = validate_packs([pack])
    assert errors == [] and any("lore file" in m for m in messages(warnings))


def test_scenario_setup_parsing():
    rs = ruleset("duel_draft")
    assert rs.scenario.setup == {"unique_factions": True, "artifacts": "draft", "artifact_offer": 2,
                                 "artifact_points": 3, "artifact_pool": ()}
    assert rs.scenario.phase_limits["setup"] == 120
    assert ruleset("duel_points").scenario.setup["unique_factions"] is False
    assert ruleset("duel_plain").scenario.setup is None


def test_tech_content_is_part_of_the_ruleset_hash(tmp_path):
    pack = pack_copy(tmp_path)
    before = build_ruleset([pack], "duel_draft").hash
    assert before == ruleset().hash
    edit_yaml(pack / "techs" / "techs.yaml", lambda d: [{**t, "cost": 7} if t["id"] == "smithing" else t for t in d])
    assert build_ruleset([pack], "duel_draft").hash != before
    # the setup mode is a game setting, not a rule: changing it keeps the hash
    pack2 = pack_copy(tmp_path / "other")
    edit_yaml(pack2 / "scenarios" / "scenarios.yaml",
              lambda d: [{**s, "setup": {"artifacts": "points"}} if s["id"] == "duel_draft" else s for s in d])
    assert build_ruleset([pack2], "duel_draft").hash == before


def test_pack_dir_listing_is_unchanged_for_core():
    assert PACK.name == "t2" and all(p.name != "t2" for p in default_pack_dirs())
