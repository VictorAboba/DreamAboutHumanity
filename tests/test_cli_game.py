"""CLI: game create, replay, report, goal-text, pack validate (a short game is driven directly through Game)."""

from __future__ import annotations

import json

import pytest

from dah.cli import main, parse_limits, parse_seat
from dah.game import Game
from dah.store import GameDir, hash_key, read_json, write_json


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def create(tmp_path, capsys, *extra: str) -> dict[str, str]:
    code = main(["game", "create", "--scenario", "core:slice3", "--seat", "A:swarm:Нико", "--seat", "B:agent:Соло",
                 "--seat", "C:bot-rush:Бот", "--seed", "7", "--limits", "20m,20m,10m", "--turns", "2",
                 "--id", "t1", "--games-dir", str(tmp_path), "--dt-session", "A=sess-1", *extra])
    assert code == 0
    out = capsys.readouterr().out
    return {line.split()[0]: line.split()[-1] for line in out.splitlines() if "dah_t1_" in line}


def play_to_end(gdir: GameDir) -> Game:
    clock = FakeClock()
    game = Game.load(gdir, clock)
    game.start()  # `dah game create` leaves the first phase to the server
    while not game.game_over:
        if game.phase == "actions":
            cap = next(p.id for p in game.state.owned("A") if p.type == "core:capital")
            game.submit("A", [{"kind": "recruit", "province": cap, "unit": "assault", "count": 1},
                              {"kind": "build", "province": cap, "building": "mine"}])
        clock.now += 3600
        game.tick()
    return game


def test_parsers():
    assert parse_limits("20m,5s,1h") == {"negotiation_1": 1200, "actions": 5, "negotiation_2": 3600}
    assert parse_seat("C:bot-rush:Бот") == {"seat": "C", "nick": "Бот", "kind": "bot", "bot": "rush"}
    with pytest.raises(ValueError):
        parse_limits("20m,20m")
    with pytest.raises(ValueError):
        parse_seat("A:swarm")


def test_game_create_writes_dir_and_prints_keys_once(tmp_path, capsys):
    keys = create(tmp_path, capsys)
    assert set(keys) == {"A", "B", "C"} and keys["A"].startswith("dah_t1_A_")
    gdir = GameDir(tmp_path / "t1")
    assert read_json(gdir.keys_path) == {hash_key(k): seat for seat, k in keys.items()}
    for path in (gdir.config_path, gdir.journal_path):
        text = path.read_text(encoding="utf-8")
        assert not any(k in text for k in keys.values())
    assert read_json(gdir.bot_keys_path) == {"C": {"key": keys["C"], "bot": "rush"}}
    config = gdir.load_config()
    assert config["turn_limit"] == 2 and config["phase_limits"]["negotiation_2"] == 600
    assert config["seats"][0]["dreamteam_session_id"] == "sess-1" and config["seats"][2]["kind"] == "bot"
    assert main(["game", "create", "--scenario", "core:slice3", "--seat", "A:swarm:X", "--id", "t1",
                 "--games-dir", str(tmp_path)]) == 2  # already exists
    assert main(["game", "create", "--scenario", "core:slice3", "--seat", "A:bot-nope:X", "--id", "t2",
                 "--games-dir", str(tmp_path)]) == 2


def test_replay_report_and_goal_text(tmp_path, capsys):
    keys = create(tmp_path, capsys)
    gdir = GameDir(tmp_path / "t1")
    game = play_to_end(gdir)
    assert game.state.result is not None

    assert main(["replay", "t1", "--games-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "ход 1: OK" in out and "ход 2: OK" in out and "расхождений 0" in out

    assert main(["report", "t1", "--games-dir", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"]["turn"] == 2 and len(report["turns"]) == 2
    assert report["totals"]["A"]["submits"] == 2 and report["totals"]["B"]["autopilot_turns"] == [1, 2]
    assert report["rules_chars"] > 1000 and report["phases"][0]["closed"] == "deadline"
    assert main(["report", "t1", "--games-dir", str(tmp_path)]) == 0
    assert "ОТЧЁТ ПАРТИИ t1" in capsys.readouterr().out

    assert main(["goal-text", "t1", "A", "--games-dir", str(tmp_path)]) == 0
    goal = capsys.readouterr().out
    assert "Нико" in goal and "dah.rules()" in goal and "game_over" in goal and "[DAH]" in goal
    assert not any(k in goal for k in keys.values())

    orders = gdir.turn_file("orders", 1)
    tampered = read_json(orders)
    tampered["A"]["orders"] = []
    write_json(orders, tampered)
    assert main(["replay", "t1", "--games-dir", str(tmp_path)]) == 1
    assert "РАСХОЖДЕНИЕ" in capsys.readouterr().out


def test_missing_game_and_pack_validate(tmp_path, capsys):
    assert main(["replay", "nope", "--games-dir", str(tmp_path)]) == 2
    assert main(["pack", "validate"]) == 0
    assert "0 ошибок" in capsys.readouterr().out


def test_game_create_leaves_first_phase_to_the_server(tmp_path, capsys):
    create(tmp_path, capsys)
    gdir = GameDir(tmp_path / "t1")
    types = [json.loads(line)["type"] for line in gdir.journal_path.read_text(encoding="utf-8").splitlines()]
    assert "game_created" in types and "phase_open" not in types
    assert not Game.load(gdir, FakeClock()).started


def test_replay_flags_changed_ruleset(tmp_path, capsys):
    create(tmp_path, capsys)
    gdir = GameDir(tmp_path / "t1")
    play_to_end(gdir)
    lines = gdir.journal_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["ruleset_hash"] = "0" * 64
    gdir.journal_path.write_text("\n".join([json.dumps(first, ensure_ascii=False), *lines[1:]]) + "\n",
                                 encoding="utf-8")
    assert main(["replay", "t1", "--games-dir", str(tmp_path)]) == 1
    assert "РАСХОЖДЕНИЕ ПРАВИЛ" in capsys.readouterr().out
