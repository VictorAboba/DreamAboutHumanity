"""Stage 4, step 3b: what players read about the score (docs/SCORING.md 8): the rules core and topics, the score
line, the objective in get_setup/get_state, the game-over texts, the bridge tool descriptions and the goal text.

Content-specific checks use the fixture pack tests/packs/t4; the rules budget gate runs on every core scenario."""

from __future__ import annotations

from pathlib import Path

import pytest

from dah.bridge.tools import TOOLS
from dah.cli import goal_text
from dah.engine import build_ruleset, rules_text, rules_topic
from dah.engine.rules import RULES_LIMIT
from dah.game import Game, make_config
from dah.store import init_game_dir

from stage2_helpers import FakeClock

ROOT = Path(__file__).resolve().parents[1]
PACKS = [str(ROOT / "tests" / "packs" / "t4")]
SEATS = [{"seat": s, "nick": f"Ник{s}", "kind": "agent", "faction": "iron"} for s in "ABC"]
SPECS = {t.name: t.description for t in TOOLS}


def _victory(text: str) -> str:
    return next(p for p in text.split("\n\n") if p.startswith("ПОБЕДА"))


def _ready_all(game: Game) -> None:
    from dah.engine import orders_hash
    for seat in game.alive_seats():
        game.set_ready(seat, orders_hash(game._book(seat)) if game.phase != "setup" else game.setup_hash(seat))


def _game(tmp_path: Path, scenario: str = "tri_score", **kw) -> Game:
    cfg = make_config(game_id="api", scenario=scenario, seats=SEATS, seed=5, pack_dirs=PACKS, **kw)
    gdir, _ = init_game_dir(tmp_path, cfg)
    return Game.create(gdir, FakeClock())


# ---------------------------------------------------------------- the gate: rules() on every core scenario

CORE = ["core:slice3", "core:slice2", "core:quad4", "core:blitz_test", "core:blitz_spy"]


def _core_scenarios() -> list[str]:
    from dah.engine import default_pack_dirs
    import yaml
    path = default_pack_dirs()[0] / "scenarios" / "scenarios.yaml"
    ids = [f"core:{s['id']}" for s in yaml.safe_load(path.read_text(encoding="utf-8"))]
    return sorted(set(CORE) | set(ids))


@pytest.mark.parametrize("scenario", _core_scenarios())
@pytest.mark.parametrize("artifacts", [None, "points"])
def test_core_rules_fit_with_every_system_on_and_are_never_cut(tmp_path, scenario, artifacts):
    """SCORING.md, gate of the content merge: the real rules() (with setup and СООБЩЕНИЯ) of every core scenario with
    espionage and conquest forced on fits RULES_LIMIT without the last-resort cut, and ПОБЕДА stays ≈150 chars."""
    rs = build_ruleset(None, scenario)
    n = 4 if rs.map.id.endswith("mirror4") else 2 if rs.map.id.endswith("mirror2") else 3
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(n)]
    if artifacts and not rs.scenario.setup:
        artifacts = None
    cfg = make_config(game_id="g", scenario=scenario, seats=specs, seed=1, artifacts=artifacts,
                      systems={"espionage": True, "conquest": True})
    gdir, _ = init_game_dir(tmp_path, cfg)
    game = Game.create(gdir, FakeClock())
    text = game.rules()
    assert len(text) <= RULES_LIMIT and "сокращено" not in text, (scenario, len(text))
    victory = _victory(text)
    assert len(victory) <= 150, victory
    if not game.ruleset.default_scoring:
        assert "rules('victory')" in victory
        topic = game.rules("victory")
        assert "очко за" in topic or "Земля:" in topic


def test_victory_core_lists_only_weighted_sources_and_the_topic_has_the_numbers():
    rs = build_ruleset(PACKS, "tri_score_all")
    core = _victory(rules_text(rs))
    assert core.startswith("ПОБЕДА: последний живой") and "земля+наука+стройки+война+тайная цель" in core
    assert len(core) <= 150
    topic = rules_topic(rs, "victory")
    for piece in ("1 очко за 30", "не больше 3 с одного соперника", "Шпиль Сна", "Хозяин кузниц",
                  "Счётная книга", "Где смотреть", "Равный итог"):
        assert piece in topic, piece
    for alias in ("score", "objectives", "цели", "счёт", "стройки"):
        assert rules_topic(rs, alias) == topic


def test_topics_mention_the_score_where_it_matters():
    rs = build_ruleset(PACKS, "tri_score")
    buildings = rules_topic(rs, "buildings")
    assert "великая стройка +3 очк. (только" in buildings and "не больше 1 у игрока" in buildings
    assert "у тебя уже есть N из N" in rules_topic(rs, "orders")
    assert "Счёт: свой — точно" in rules_topic(rs, "fog")
    assert "Счёт хода считается в конце расчёта" in rules_topic(rs, "combat")
    plain = build_ruleset(PACKS, "tri_plain")
    assert "великая" not in rules_topic(plain, "buildings")         # works off: the great works are hidden
    assert "Счёт хода считается" not in rules_topic(plain, "combat")
    assert "стройки — по публичным событиям" not in rules_topic(plain, "fog")


# ---------------------------------------------------------------- get_setup / get_state / history / phase

def test_objective_shown_to_its_owner_in_get_setup_and_get_state(tmp_path):
    game = _game(tmp_path, "tri_score_setup")
    setup = game.get_setup("A")
    mine = setup["objectives"][0]
    assert "Твоя тайная цель" in setup["text"] and mine["name"] in setup["text"]
    _ready_all(game)                                                 # the setup phase closes
    you = game.state_json("A")["you"]
    assert you["objectives"][0]["id"] == mine["id"] and "met_now" in you["objectives"][0]
    others = [p for p in game.state_json("A")["players"] if p["seat"] != "A"]
    assert all("objectives" not in p for p in others)
    line = next(x for x in game.state_text("A").splitlines() if x.startswith("Счёт:"))
    assert f"твоя цель «{mine['name']}»" in line and len(line) <= 300
    assert "ты " in line and "[зем " in line and "≥" in line and "нау ?" in line


def test_history_breakdown_in_source_order_and_final_texts(tmp_path):
    game = _game(tmp_path, "tri_score", turn_limit=2)
    for _ in range(3):
        _ready_all(game)
    hist = game.history("A")["text"]
    line = next(x for x in hist.splitlines() if x.startswith("Твой счёт после хода"))
    labels = [w for w in line.split("[")[1].split() if not w.rstrip("]").isdigit()]
    assert labels == ["зем", "нау", "стр", "вой"], line
    for _ in range(3):
        _ready_all(game)
    assert game.game_over
    final = game.state.result["scores"]
    text = game.state_text("B")
    line = next(x for x in text.splitlines() if x.startswith("Итог:"))
    assert line.startswith(f"Итог: НикA {final['A']}") or f"НикA {final['A']}" in line
    assert f"ты {final['B']}" in line and "цель" in line and "≥" not in line and len(line) <= 300
    assert "цели:" in line and ("✓" in line or "✗" in line)
    you = game.state_json("B")["you"]
    assert you["score"] == final["B"]
    phase = game.phase_info("B")
    assert "итог: " in phase["text"] and f"НикB {final['B']}" in phase["text"]
    assert {"scores", "score_parts", "objectives"} <= set(phase["result"])


def test_default_scoring_keeps_its_line_and_phase_shows_totals(tmp_path):
    game = _game(tmp_path, "tri_plain", turn_limit=1)
    line = next(x for x in game.state_text("A").splitlines() if x.startswith("Счёт:"))
    assert "[зем" not in line and "цель" not in line
    for _ in range(3):
        _ready_all(game)
    assert game.game_over and "итог: " in game.phase_info("A")["text"]


# ---------------------------------------------------------------- bridge and goal text

def test_tool_descriptions_explain_the_score():
    assert "«Счёт:»" in SPECS["get_state"] and "«≥»" in SPECS["get_state"] and "тайная цель" in SPECS["get_state"]
    assert "тайная цель" in SPECS["get_setup"]
    assert "очки науки" in SPECS["list_techs"]
    assert "тайная цель" in SPECS["describe"] and "великая стройка" in SPECS["describe"]
    assert "victory (очки" in SPECS["rules"]
    assert "score_parts" in SPECS["phase"]
    assert "счёт" in SPECS["history"]


def test_goal_text_follows_the_scoring(tmp_path):
    scored = _game(tmp_path / "a", "tri_score_setup").config
    text = goal_text(scored, "A")
    assert "земля + наука + великие стройки + война + твоя тайная цель" in text
    assert 'dah.rules(topic="victory")' in text and "dah.get_setup() и dah.get_state()" in text
    study = make_config(game_id="s", scenario="tri_study", seats=SEATS, seed=1, pack_dirs=PACKS)
    line = next(x for x in goal_text(study, "A").splitlines() if x.startswith("Цель игры"))
    assert "наука + великие стройки" in line and "земля" not in line and "тайная" not in line
    plain = make_config(game_id="p", scenario="tri_plain", seats=SEATS, seed=1, pack_dirs=PACKS)
    assert "1 за провинцию, +3 за каждую столицу" in goal_text(plain, "A")
