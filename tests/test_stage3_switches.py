"""Stage-3 switches, registry and content (docs/SPY_AND_CONQUEST.md 1.1-1.4, 3.3, 9, 10): hashes, hidden content,
dispositions, order phase rules, the game config, views, rules and replay of a conquest game."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from conquest_helpers import PACK, PACKS, board, book, dispose, mv, occupy, ruleset
from dah.engine import (ContentError, Order, SeatOrders, build_ruleset, check_order, describe, new_game_state,
                        render_state_text, rules_text, rules_topic, state_hash, view_state)
from dah.engine.model import ACTIONS
from dah.engine.registry import ORDER_KINDS, ORDER_PHASE_RULES, STATS, stat_spec
from dah.engine.rules import sections
from dah.game import Game, make_config, replay
from dah.journal import read_events
from dah.store import init_game_dir

ROOT = Path(__file__).resolve().parents[1]
# core 0.3.0 (frozen as content/legacy/core-0.3.0 once core moves on): without stage-3 content it hashes as before
CORE030 = ROOT / "content" / "legacy" / "core-0.3.0"
CORE030_DIRS = [str(CORE030)] if CORE030.is_dir() else None
CORE030_HASHES = {"slice3": "161f5a318dd5", "slice2": "60fff6e3a539", "quad4": "1591afb44f3c",
                  "blitz_test": "1ca403ad3e7d"}
T2_HASHES = {"duel_draft": "aaa9ee0b199a", "duel_points": "b4796cf375a6", "duel_plain": "a355f6608037"}


def pack_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "t3"
    shutil.copytree(PACK, dst)
    return dst


def edit(path: Path, change) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = change(data)
    path.write_text(yaml.safe_dump(out if out is not None else data, allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------- hashes and switches

def test_packs_without_stage3_content_hash_as_before():
    for scenario, prefix in CORE030_HASHES.items():
        assert build_ruleset(CORE030_DIRS, scenario).hash.startswith(prefix)
    t2 = [str(ROOT / "tests" / "packs" / "t2")]
    for scenario, prefix in T2_HASHES.items():
        assert build_ruleset(t2, scenario).hash.startswith(prefix)
        assert build_ruleset(t2, scenario, {"conquest": False, "espionage": False}).hash.startswith(prefix)


def test_systems_are_hashed_only_when_on():
    on, off = ruleset(), ruleset(systems={"conquest": False})
    assert on.system("conquest") and not off.system("conquest")
    assert on.to_dict()["scenario"]["systems"] == ["conquest"]
    data = off.to_dict()
    assert "systems" not in data["scenario"] and "dispositions" not in data
    assert all("conquest_value" not in t for t in data["province_types"].values())
    assert ruleset("tri_off").to_dict()["scenario"].get("systems") is None
    assert ruleset("tri_plain", {"conquest": True}).system("conquest")
    assert ruleset("tri_conquest", {"conquest": True, "espionage": None}).hash == on.hash
    assert ruleset("tri_conquest", {"conquest": None}).hash == on.hash


def test_system_without_content_and_unknown_system_are_errors(tmp_path):
    assert ruleset(systems={"espionage": True}).missions
    with pytest.raises(ContentError, match="unknown system"):
        ruleset(systems={"magic": True})
    pack = pack_copy(tmp_path)
    shutil.rmtree(pack / "espionage")
    with pytest.raises(ContentError, match="no missions"):
        build_ruleset([str(pack)], "tri_conquest", {"espionage": True})
    shutil.rmtree(pack / "dispositions")
    edit(pack / "factions" / "factions.yaml", lambda d: [{**f, "bonuses": [], "penalties": []} for f in d])
    edit(pack / "techs" / "techs.yaml", lambda d: [t for t in d if t["id"] != "plunder"])
    with pytest.raises(ContentError, match="no dispositions"):
        build_ruleset([str(pack)], "tri_conquest")
    assert not build_ruleset([str(pack)], "tri_plain").dispositions


@pytest.mark.parametrize("change, match", [
    (lambda d: [{**d[0], "behavior": "enslave"}, *d[1:]], "unknown behavior"),
    (lambda d: [d[0], {**d[1], "params": {"ruin_turns": 0}}, *d[2:]], "ruin_turns"),
    (lambda d: [d[0], {**d[1], "params": {"burn": 1}}, *d[2:]], "unknown parameter"),
])
def test_bad_dispositions_are_content_errors(tmp_path, change, match):
    pack = pack_copy(tmp_path)
    edit(pack / "dispositions" / "dispositions.yaml", change)
    with pytest.raises(ContentError, match=match):
        build_ruleset([str(pack)], "tri_conquest")


def test_conquest_value_bounds(tmp_path):
    pack = pack_copy(tmp_path)
    edit(pack / "province_types" / "types.yaml", lambda d: [{**d[0], "conquest_value": 6}, *d[1:]])
    with pytest.raises(ContentError):
        build_ruleset([str(pack)], "tri_conquest")
    rs = ruleset()
    assert rs.province_types["t3:capital"].conquest_value == 3 and rs.province_types["t3:forge"].conquest_value == 1


def test_requires_system_hides_content_and_effects_on_off_systems_drop():
    off, on = ruleset("tri_plain"), ruleset()
    assert "t3:spoils" not in off.branches and "t3:plunder" not in off.techs and "t3:spoils_hall" not in off.buildings
    assert "t3:banner" not in off.artifacts and "t3:chest" in off.artifacts
    assert {"t3:spoils", "t3:plunder", "t3:spoils_hall", "t3:banner"} <= {*on.branches, *on.techs, *on.buildings,
                                                                          *on.artifacts}
    drill_off = [e["target"] for e in off.techs["t3:drill"].effects]
    assert drill_off == ["command_points"]
    assert "conquest.claim_turns" in [e["target"] for e in on.techs["t3:drill"].effects]
    ash_off = describe(off, "faction", "t3:ash")["text"]
    ash_on = describe(on, "faction", "t3:ash")["text"]
    assert "судьбы" not in ash_off and "судьбы «Разграбить»" in ash_on
    assert on.stat("conquest.claim_turns", None) == 10


def test_registry_stage3():
    assert set(ORDER_PHASE_RULES) == set(ORDER_KINDS)
    for kind in ("move", "recruit", "build", "fortify", "research"):
        assert ORDER_PHASE_RULES[kind]["recon_consumed"] is False
    assert ORDER_PHASE_RULES["research"]["p2_edit"] == "locked" and ORDER_PHASE_RULES["fortify"]["p2_new"] == 1
    assert ORDER_PHASE_RULES["dispose"] == {"p2_new": 0, "p2_edit": "free", "recon_consumed": False}
    stage3 = [s for s in STATS if s.system]
    assert stage3 and all(not s.seeded for s in stage3)
    assert stat_spec("disposition.t3:raid.loot_mul").system == "conquest"
    assert stat_spec("spy.whisper").system == "espionage"


# ---------------------------------------------------------------- state and orders

def test_home_and_origins_only_with_a_system():
    rs = ruleset()
    st = new_game_state(rs, [{"seat": s, "nick": s} for s in "ABC"], 4)
    for seat, player in st.players.items():
        assert player.home is not None and st.provinces[player.home].type == "t3:capital"
        assert st.provinces[player.home].owner == seat and st.origins[player.home] == seat
    plain = ruleset("tri_plain")
    st = new_game_state(plain, [{"seat": s, "nick": s} for s in "ABC"], 4)
    assert all(p.home is None for p in st.players.values()) and not st.origins and not st.claims
    data = st.to_dict()
    assert state_hash(st) == state_hash(type(st).from_dict(data))


def test_order_dicts_of_older_kinds_are_unchanged():
    move = Order("o1", "move", ACTIONS, src="A1", dst="A2", units={"t3:militia": 1})
    assert set(move.to_dict()) == {"id", "kind", "phase_added", "from", "to", "units", "pact"}
    assert set(Order("o2", "fortify", province="A1").to_dict()) == {"id", "kind", "phase_added", "province"}
    d = Order("o3", "dispose", province="B2", disposition="t3:raid").to_dict()
    assert d == {"id": "o3", "kind": "dispose", "phase_added": ACTIONS, "province": "B2", "disposition": "t3:raid"}
    back = Order.from_dict({**d, "to": "C"})
    assert back.to == "C" and back.dst is None
    assert Order.from_dict(move.to_dict()) == move


def test_off_systems_refuse_their_orders_and_feint():
    rs = ruleset("tri_plain")
    st = board(rs)
    for item in ({"kind": "hire_shade"}, {"kind": "spy", "shade": "1"}, {"kind": "recall", "shade": "1"},
                 {"kind": "ward", "province": "A1"}, dispose("B1", "raid"), {"kind": "policy", "disposition": None}):
        assert check_order(st, rs, "A", SeatOrders(), item, "actions").reason == "NOT_AVAILABLE"
    feint = {**mv("A1", "A2", 1), "feint": True}
    assert check_order(st, rs, "A", SeatOrders(), feint, "actions").reason == "INVALID"
    assert check_order(st, rs, "A", SeatOrders(), {**mv("A1", "A2", 1), "feint": False}, "actions").ok


# ---------------------------------------------------------------- views and rules

def test_state_text_and_json_show_conquest():
    rs = ruleset()
    st = board(rs)
    occupy(st, "B2", "A", "B", since=1)
    st.claims = [{"seat": "B", "province": "B2", "until_turn": 11}]
    st.players["A"].conquest_policy = "t3:subjugate"
    text = render_state_text(st, rs, "A")
    assert "B2 Кузня Бета [ТЫ] кузн. [оккуп. НикB]" in text
    assert "ОККУПАЦИЯ (подчинить 5 Провиант×ценн. | истребить | разграбить | освободить): B2 с х1 ценн.1" in text
    assert "ПРИТЯЗАНИЯ: B2 — НикB до х11" in text and "ПОЛИТИКА: подчинить" in text
    view = view_state(st, rs, "C")
    assert view["claims"] == st.claims and view["you"]["conquest_policy"] is None      # others' policies stay hidden
    b2 = next(p for p in view["provinces"] if p["id"] == "B2")
    assert b2["status"] == {"occupied": {"by": "A", "from": "B", "since": 1}}
    plain = ruleset("tri_plain")
    pst = board(plain)
    assert "ОККУПАЦИЯ" not in render_state_text(pst, plain, "A") and "claims" not in view_state(pst, plain, "A")


def test_rules_conquest_section_only_when_on():
    rs = ruleset()
    sec = next(s for s in sections(rs) if s.key == "conquest")
    assert len(sec.core) <= 450 and len(sec.short) <= 200
    assert "ЗАВОЕВАНИЕ" in rules_text(rs) and len(rules_text(rs)) <= 6000
    assert "Притязание" in rules_topic(rs, "capture") and rules_topic(rs, "завоевание")
    plain = ruleset("tri_plain")
    assert rules_topic(plain, "conquest") is None and "ЗАВОЕВАНИЕ" not in rules_text(plain)


# ---------------------------------------------------------------- the game: config, journal, replay

SEATS = [{"seat": s, "nick": f"Ник{s}", "kind": "human", "faction": "iron"} for s in "ABC"]


def test_make_config_writes_switches_only_when_set_or_on():
    on = make_config(game_id="g", scenario="tri_conquest", seats=SEATS, seed=1, pack_dirs=PACKS)
    assert on["systems"] == {"espionage": False, "conquest": True}
    off = make_config(game_id="g", scenario="tri_conquest", seats=SEATS, seed=1, pack_dirs=PACKS,
                      systems={"conquest": False})
    assert off["systems"] == {"espionage": False, "conquest": False}
    plain = make_config(game_id="g", scenario="tri_plain", seats=SEATS, seed=1, pack_dirs=PACKS,
                        systems={"conquest": None, "espionage": None})
    assert "systems" not in plain


def test_cli_conquest_flag(tmp_path, capsys):
    from dah.cli import main
    code = main(["game", "create", "--scenario", "tri_conquest", "--packs", PACKS[0], "--seat", "A:human:Аа",
                 "--seat", "B:human:Бб", "--seat", "C:human:Вв", "--seed", "3", "--id", "cq1", "--conquest", "off",
                 "--games-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "завоевание выкл." in out and ".." not in out.split("Системы:")[1].splitlines()[0]
    config = json.loads((tmp_path / "cq1" / "config.json").read_text(encoding="utf-8"))
    assert config["systems"] == {"espionage": False, "conquest": False}


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


def _ready_all(game: Game) -> None:
    from dah.engine import orders_hash
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)))


def test_conquest_game_journals_and_replays(tmp_path):
    config = make_config(game_id="cq", scenario="tri_conquest", seats=SEATS, seed=5, pack_dirs=PACKS)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, Clock())
    created = next(e for e in read_events(gdir.journal_path) if e["type"] == "game_created")
    assert created["systems"] == ["conquest"]
    a_home = game.state.players["A"].home
    a_forge = next(p.id for p in game.state.owned("A") if p.id != a_home)
    b_forge = next(p.id for p in game.state.owned("B") if p.id != game.state.players["B"].home)
    _ready_all(game)                                                       # П1 -> Д
    game.submit("A", [mv(a_home, a_forge, 3), {"kind": "policy", "disposition": "subjugate"}])
    _ready_all(game)
    _ready_all(game)                                                       # П2 -> расчёт хода 1
    _ready_all(game)
    game.submit("A", [mv(a_forge, b_forge, 3), dispose(b_forge, "raid")])
    _ready_all(game)                                                       # Д -> П2
    order_id = next(o.id for o in game._book("A").orders if o.kind == "dispose")
    cp = game.orders("A")["cp_left"]
    out = game.amend("A", order_id, {"disposition": "subjugate"})
    assert out["cp_cost"] == 0 and out["cp_left"] == cp
    assert "dispose " + b_forge in game.orders("A")["text"]
    _ready_all(game)                                                       # расчёт хода 2
    assert game.state.provinces[b_forge].owner == "A" and not game.state.provinces[b_forge].status.get("occupied")
    types = [e["type"] for e in read_events(gdir.journal_path)]
    assert {"policy_set", "province_captured", "disposition", "claim_created"} <= set(types)
    fate = next(e for e in read_events(gdir.journal_path) if e["type"] == "disposition")
    assert fate["source"] == "directive" and fate["disposition"] == "t3:subjugate"
    text = game.state_text("B")
    assert "ПРИТЯЗАНИЯ:" in text
    result = replay(gdir)
    assert result.ok and len(result.turns) == 2
