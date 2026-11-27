"""Core 0.8.2 (decision 33, measurement m11): the frozen core 0.8.1, the hidden last turn (the game ends on one of the
turns 11-15 of quad4, drawn at game creation and told to no seat before game_over), the bound of a rival's hidden
score next to its visible «≥», and the after-game spend of an eliminated seat (m11 finding 4)."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from dah.bots.view import parse_state
from dah.cli import goal_text
from dah.engine import GameState, SeatOrders, build_ruleset, check_order, orders_hash
from dah.engine import espionage as esp
from dah.engine import registry
from dah.engine import scoring as sc
from dah.engine import trade as tr
from dah.engine.content import DEFAULT_PACKS_DIR, LEGACY_PACKS_DIR
from dah.engine.rules import VICTORY_CORE_LIMIT, rules_text, rules_topic
from dah.engine.rules import topics as rule_topics
from dah.game import Game, GameError, draw_end_turn, game_ruleset, make_config, parse_turn_limit, replay
from dah.journal import read_events, view_events
from dah.report import build_report, render_report
from dah.server import spectate
from dah.store import GameDir, init_game_dir
from dah.waker.text import wake_text

ROOT = Path(__file__).resolve().parents[1]
CORE = DEFAULT_PACKS_DIR / "core"
CORE081 = LEGACY_PACKS_DIR / "core-0.8.1"
# core 0.8.3 froze 0.8.2: the 0.8.1 -> 0.8.2 step is checked on the copy (test_core083.py checks the next one)
CORE082 = LEGACY_PACKS_DIR / "core-0.8.2"
# ruleset hashes of the frozen core 0.8.1, as core 0.8.1 itself built them at b6cf22d (measurement m11: quad4)
CORE081_HASHES = {"slice3": "ca89d9469d0c", "slice2": "9790719be215", "quad4": "5d0ca0d8a69f",
                  "blitz_test": "a138b8a8f75e", "blitz_spy": "1571531b7e03", "blitz_score": "8efc9ce96be1",
                  "blitz_finds": "0628f03c1dc7", "blitz_trade": "fc094d25eb65"}
RUNS = Path(r"C:\Users\kuzmi\DreamAboutHumanity-runs-2026-11-23")
SEATS3 = [{"seat": "A", "nick": "Альфа", "kind": "agent"}, {"seat": "B", "nick": "Бета", "kind": "agent"},
          {"seat": "C", "nick": "Гамма", "kind": "agent"}]


class Clock:
    def __call__(self) -> float:
        return 1_700_000_000.0


def ready_all(game: Game) -> None:
    for seat in game.alive_seats():
        game.set_ready(seat, None if game.phase in ("setup", "negotiation_1") else orders_hash(game._book(seat)))


def quad_game(tmp_path: Path, name: str = "g", seats=SEATS3, **kw) -> Game:
    config = make_config(game_id=name, scenario="core:quad4", seats=seats, seed=kw.pop("seed", 3),
                         objectives_salt=kw.pop("objectives_salt", 9), **kw)
    gdir, _ = init_game_dir(tmp_path / name, config)
    return Game.create(gdir, Clock())


def past_setup(game: Game) -> Game:
    while game.phase == "setup":
        ready_all(game)
    return game


def copy_game(src: Path, tmp_path: Path) -> Path:
    if not (src / "journal.jsonl").is_file():
        pytest.skip(f"measurement data {src} not present")
    dst = tmp_path / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("keys.json"))
    assert not (dst / "keys.json").exists()
    return dst


# ---------------------------------------------------------------- a) the freeze of core 0.8.1

def test_core_081_is_frozen_exactly_with_its_hashes():
    assert "version: 0.8.2" in (CORE082 / "pack.yaml").read_text(encoding="utf-8")
    head = (CORE081 / "pack.yaml").read_text(encoding="utf-8")
    assert "version: 0.8.1" in head and "Frozen copy of core 0.8.1" in head and "Do not edit" in head
    for sid, prefix in CORE081_HASHES.items():
        old = build_ruleset([str(CORE081)], sid)
        assert old.hash.startswith(prefix), (sid, old.hash[:12])
        assert old.packs == {"core": "0.8.1"} and not old.scenario.end_hidden


def test_the_frozen_copy_is_byte_equal_to_the_commit_of_core_081():
    try:
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "b6cf22d",
                                  "content/packs/core"], capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git history not available")
    files = {p.relative_to(CORE081).as_posix() for p in CORE081.rglob("*") if p.is_file()}
    assert files == {name.removeprefix("content/packs/core/") for name in listing}
    for name in listing:
        rel = name.removeprefix("content/packs/core/")
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"b6cf22d:{name}"], capture_output=True,
                              check=True).stdout
        data = (CORE081 / rel).read_bytes()
        if rel == "pack.yaml":          # the frozen header only
            data = b"".join(line for line in data.splitlines(keepends=True) if not line.startswith(b"#"))
        assert data == blob, rel


def test_only_the_version_and_the_scenarios_moved_from_the_frozen_copy():
    changed = {"pack.yaml", "scenarios/scenarios.yaml"}
    for path in CORE081.rglob("*"):
        rel = path.relative_to(CORE081).as_posix()
        if path.is_file() and rel not in changed:
            assert path.read_bytes().replace(b"\r\n", b"\n") == (CORE082 / rel).read_bytes().replace(b"\r\n", b"\n"), rel


@pytest.mark.parametrize("sid", sorted(CORE081_HASHES))
def test_the_scenarios_change_only_the_end_of_quad4(sid):
    new, old = build_ruleset([str(CORE082)], sid), build_ruleset([str(CORE081)], sid)
    a, b = old.to_dict(), new.to_dict()
    assert (a.pop("packs"), b.pop("packs")) == ({"core": "0.8.1"}, {"core": "0.8.2"})
    if sid == "quad4":
        assert (a["scenario"].pop("turn_limit"), b["scenario"].pop("turn_limit")) == (12, 15)
        assert b["scenario"].pop("end_turn_min") == 11 and "end_turn_min" not in a["scenario"]
        assert new.scenario.end_hidden and (new.scenario.end_min, new.scenario.turn_limit) == (11, 15)
    else:
        # the blitzes and the slices keep their fixed end: short deterministic games for tests and tools
        assert not new.scenario.end_hidden and "end_turn_min" not in b["scenario"]
    assert "end_turn" not in b["scenario"]
    assert a == b


@pytest.mark.parametrize("name,frozen", [("games-m12/m12", "core-0.8.2"), ("games-m11/m11", "core-0.8.1"),
                                         ("games-m10/m10", "core-0.8.0"),
                                         ("games-m9/m9", "core-0.7.0"), ("games-m8/m8", "core-0.6.0"),
                                         ("games-m7/m7", "core-0.5.0")])
def test_measured_games_replay_with_their_frozen_packs(tmp_path, name, frozen):
    rep = replay(GameDir(copy_game(RUNS / name, tmp_path)))
    assert rep.ok and rep.turns and rep.result_ok and sum(not t.ok for t in rep.turns) == 0
    assert all(t.ok for t in rep.recons)
    assert rep.pack_dirs and Path(rep.pack_dirs[0]).name == frozen


def test_m11_keeps_its_fixed_end_on_the_frozen_pack(tmp_path):
    game = Game.load(GameDir(copy_game(RUNS / "games-m11/m11", tmp_path)))
    assert game.ruleset.packs == {"core": "0.8.1"} and not game.ruleset.scenario.end_hidden
    assert "end_turns" not in game.phase_info("A") and game.state.result["reason"] == "turn_limit"


# ---------------------------------------------------------------- b) the hidden last turn: config and draw

def test_quad4_draws_a_hidden_end_between_11_and_15():
    cfg = make_config(game_id="g", scenario="core:quad4", seats=SEATS3, seed=3, objectives_salt=9)
    assert cfg["turn_limit"] == 15 and cfg["end_turn_min"] == 11 and 11 <= cfg["end_turn"] <= 15
    assert cfg["end_turn"] == draw_end_turn(3, 9, 11, 15)
    again = make_config(game_id="g", scenario="core:quad4", seats=SEATS3, seed=3, objectives_salt=9)
    assert again["end_turn"] == cfg["end_turn"]                     # deterministic from seed and salt
    drawn = Counter(draw_end_turn(seed, seed * 7 + 1, 11, 15) for seed in range(600))
    assert set(drawn) == {11, 12, 13, 14, 15} and min(drawn.values()) > 80
    # the salt matters (the seed drives seat-visible streams, FOG-1)
    assert len({draw_end_turn(5, salt, 11, 15) for salt in range(40)}) == 5


def test_the_turn_limit_override_fixes_or_hides_the_end():
    fixed = make_config(game_id="g", scenario="core:quad4", seats=SEATS3, seed=3, turn_limit=12)
    assert fixed["turn_limit"] == 12 and "end_turn" not in fixed and "end_turn_min" not in fixed
    rs = game_ruleset(fixed)
    assert not rs.scenario.end_hidden and rs.scenario.last_turn == 12
    for given in ("3-5", (3, 5), [3, 5], "3–5", "3..5"):
        cfg = make_config(game_id="g", scenario="core:blitz_score", seats=SEATS3, seed=3, turn_limit=given)
        assert (cfg["end_turn_min"], cfg["turn_limit"]) == (3, 5) and 3 <= cfg["end_turn"] <= 5
        hidden = game_ruleset(cfg)
        assert hidden.scenario.end_hidden and hidden.scenario.last_turn == cfg["end_turn"]
    assert parse_turn_limit("7") == (7, 7) and parse_turn_limit(" 11-15 ") == (11, 15)
    for bad in ("5-3", "x", "0-4", (1, 2, 3), True, "1-300"):
        with pytest.raises(ValueError):
            parse_turn_limit(bad)
    # blitz and slice scenarios keep their fixed end by default
    for sid in ("blitz_score", "slice3", "slice2", "blitz_trade"):
        cfg = make_config(game_id="g", scenario=f"core:{sid}", seats=SEATS3[:2], seed=3)
        assert "end_turn" not in cfg and "end_turn_min" not in cfg


def test_a_frozen_pack_game_keeps_its_fixed_end():
    cfg = make_config(game_id="g", scenario="core:quad4", seats=SEATS3, seed=3, pack_dirs=[str(CORE081)])
    assert cfg["turn_limit"] == 12 and "end_turn" not in cfg
    assert game_ruleset(cfg).scenario.last_turn == 12


def test_the_drawn_turn_never_reaches_the_hash():
    a = make_config(game_id="g", scenario="core:quad4", seats=SEATS3, seed=3, objectives_salt=9)
    b = copy.deepcopy(a)
    b["end_turn"] = 11 if a["end_turn"] != 11 else 15
    ra, rb = game_ruleset(a), game_ruleset(b)
    assert ra.hash == rb.hash == build_ruleset(None, "quad4").hash
    assert ra.to_dict() == rb.to_dict() and "end_turn" not in ra.to_dict()["scenario"]


def test_the_game_ends_on_the_drawn_turn_and_replays(tmp_path):
    game = past_setup(quad_game(tmp_path, turn_limit="2-4", seed=11))
    end = game.config["end_turn"]
    assert 2 <= end <= 4
    seen_last = []
    while not game.game_over:
        info = game.phase_info("A")
        assert "end_turn" not in info and info["end_turns"] == [2, 4]
        seen_last.append((info["turn"], info["may_be_last"]))
        ready_all(game)
    assert game.state.turn == end and game.state.result["reason"] == "turn_limit"
    assert dict(seen_last)[1] is False and all(last for turn, last in seen_last if turn >= 2)
    over = game.phase_info("A")
    assert over["end_turn"] == end and "может" not in over["text"] and f"Ход {end}, последний" in over["text"]
    rep = replay(game.gdir)
    assert rep.ok and rep.result_ok and len(rep.turns) == end


# ---------------------------------------------------------------- b) fog: nothing seat-facing tells the drawn turn

def _seat_outputs(game: Game, seat: str, topics: bool) -> dict:
    out = {"phase": game.phase_info(seat), "state": game.state_text(seat), "json": game.state_json(seat),
           "rules": game.rules(), "goal": goal_text(game.config, seat), "whoami": game.whoami(seat),
           "journal": view_events(read_events(game.gdir.journal_path), seat=seat)}
    out["wake"] = wake_text(out["phase"])
    if topics:
        out["topics"] = {t: game.rules(t) for t in rule_topics(game.ruleset, setup=game.setup)}
    for e in out["journal"]:
        e.pop("ts", None)
    return json.loads(json.dumps(out, ensure_ascii=False, default=str))


def test_two_games_that_differ_only_in_the_drawn_end_look_the_same_to_every_seat(tmp_path):
    config = make_config(game_id="g", scenario="core:quad4", seats=SEATS3, seed=21, objectives_salt=9,
                         turn_limit="3-5")
    games = []
    for name, end in (("early", 3), ("late", 5)):
        cfg = copy.deepcopy(config)
        cfg["end_turn"] = end
        gdir, _ = init_game_dir(tmp_path / name, cfg)
        games.append(Game.create(gdir, Clock()))
    early, late = games
    assert early.ruleset.scenario.last_turn == 3 and late.ruleset.scenario.last_turn == 5
    steps = 0
    while not early.game_over:
        for seat in "ABC":
            topics = steps % 4 == 0
            assert _seat_outputs(early, seat, topics) == _seat_outputs(late, seat, topics), (early.phase, seat)
        ready_all(early)
        ready_all(late)
        steps += 1
    assert early.state.turn == 3 and not late.game_over and late.state.turn == 4
    texts = json.dumps(_seat_outputs(late, "A", True), ensure_ascii=False)
    assert "может быть последним" in texts and "end_turn\"" not in texts


def test_the_journal_hides_the_drawn_turn_until_game_over(tmp_path):
    game = past_setup(quad_game(tmp_path, turn_limit="2-3", seed=5))
    created = next(e for e in view_events(read_events(game.gdir.journal_path), seat="A")
                   if e["type"] == "game_created")
    assert "end_turn" not in created["config"] and created["config"]["end_turn_min"] == 2
    admin = next(e for e in view_events(read_events(game.gdir.journal_path), admin=True)
                 if e["type"] == "game_created")
    assert admin["config"]["end_turn"] == game.config["end_turn"]


def test_seat_texts_name_the_range_and_from_turn_11_the_possible_end(tmp_path):
    game = past_setup(quad_game(tmp_path))
    info = game.phase_info("A")
    assert info["text"].startswith("Ход 1 (конец — один из ходов 11–15, скрыт)") and info["turn_limit"] == 15
    assert info["end_turns"] == [11, 15] and info["may_be_last"] is False
    assert wake_text(info).startswith("[DAH] Ход 1 (конец — один из ходов 11–15, скрыт), фаза")
    head = game.state_text("A").splitlines()[0]
    assert head.startswith("ХОД 1 (конец — один из ходов 11–15, скрыт) |")
    js = game.state_json("A")["state"] if "state" in game.state_json("A") else game.state_json("A")
    assert js["end_turns"] == [11, 15] and js["may_be_last"] is False and "end_turn" not in js
    rules = game.rules()
    assert "«Четверо у Предела», 11–15 ходов, какой последний — скрыто" in rules
    assert "больший итог после хода 11–15:" in rules
    victory = game.rules("victory")
    assert "- Конец: партия кончается после одного из ходов 11–15" in victory
    goal = goal_text(game.config, "A")
    assert "11–15 ходов: последний ход заранее не объявлен" in goal
    assert "после последнего хода (один из 11–15, какой — скрыто)" in goal
    game.state.turn = 11
    info = game.phase_info("A")
    assert info["may_be_last"] is True and "Ход 11 (может быть последним; конец — один из ходов 11–15" in info["text"]
    assert "Ход 11 (может быть последним" in wake_text(info)
    assert "ХОД 11 (может быть последним" in game.state_text("A").splitlines()[0]


def test_a_fixed_end_keeps_the_old_texts(tmp_path):
    game = past_setup(quad_game(tmp_path, turn_limit=12))
    info = game.phase_info("A")
    assert info["text"].startswith("Ход 1/12 |") and "end_turns" not in info and "may_be_last" not in info
    assert wake_text(info).startswith("[DAH] Ход 1/12, фаза")
    assert game.state_text("A").startswith("ХОД 1/12 |")
    assert "12 ходов, карта" in game.rules() and "после 12 хода" in game.rules()
    assert "(партия g, 12 ходов)" in goal_text(game.config, "A")


def test_turn_mark_wording():
    assert registry.turn_mark(7, 12, 0) == "7/12" and registry.turn_mark(7, 12, 12) == "7/12"
    assert registry.turn_mark(7, 15, 11) == "7 (конец — один из ходов 11–15, скрыт)"
    assert registry.turn_mark(11, 15, 11) == "11 (может быть последним; конец — один из ходов 11–15, скрыт)"
    assert registry.turn_mark(13, 15, 11, over=True) == "13, последний"
    assert registry.end_span(12, 0) == "12" and registry.end_span(15, 11) == "11–15"


@pytest.mark.parametrize("seats", [2, 3, 4])
@pytest.mark.parametrize("artifacts", ["none", "draft", "points"])
def test_rules_stay_within_the_gate_on_quad4(tmp_path, seats, artifacts):
    specs = [{"seat": "ABCD"[i], "nick": f"Ник{i}", "kind": "agent"} for i in range(seats)]
    gdir, _ = init_game_dir(tmp_path, make_config(game_id="g", scenario="core:quad4", seats=specs, seed=3,
                                                  artifacts=artifacts, objectives_salt=9,
                                                  systems={"espionage": True, "conquest": True,
                                                           "discoveries": True, "trade": True}))
    game = Game.create(gdir, Clock(), start=False)
    game.start()
    while True:
        text = game.rules()
        assert len(text) <= 5970 and "сокращено" not in text, (seats, artifacts, game.phase, len(text))
        if game.phase != "setup":
            break
        ready_all(game)
    victory = rules_text(game.ruleset).split("ПОБЕДА:", 1)[1].split("\n", 1)[0]
    assert len("ПОБЕДА:" + victory) <= VICTORY_CORE_LIMIT


def test_bots_plan_for_the_earliest_end():
    state = {"turn": 9, "turn_limit": 15, "end_turns": [11, 15], "phase": "actions",
             "you": {"seat": "A", "resources": {}, "command_points": 3}, "provinces": [], "players": []}
    me = {"A"}
    view = parse_state(state, me)
    assert view.turn_limit == 11 and view.turns_left() == 3
    view12 = parse_state({**state, "turn": 13}, me)
    assert view12.turns_left() == 1                              # past the earliest end: this turn may be the last
    assert parse_state({**state, "end_turns": None}, me).turn_limit == 15


def test_bounds_that_need_a_last_turn_use_the_latest_one(tmp_path):
    game = past_setup(quad_game(tmp_path))
    rs = game.ruleset
    assert esp.rounds_left(rs, 10, "actions") == 6               # 15 - 10 + 1: the drawn turn never shows
    assert game._trade_ctx().turn_limit == 15
    with pytest.raises(Exception) as err:
        tr.parse_later(game.state, rs, {"give": {}, "by_turn": 16}, 15)
    assert "1..15" in str(err.value)


def test_spectators_see_the_range_and_the_end_only_after_the_game(tmp_path):
    game = past_setup(quad_game(tmp_path, turn_limit="2-3", seed=8))
    cache = spectate.GameCache()
    for level in (spectate.DELAYED, spectate.LIVE):
        m = spectate.meta(game, "k", level)
        live = spectate.live(game, cache, "k", level)
        assert m["end_turns"] == live["end_turns"] == [2, 3] and "end_turn" not in m and "end_turn" not in live
    while not game.game_over:
        ready_all(game)
    live = spectate.live(game, spectate.GameCache(), "k", "delayed")
    assert live["end_turn"] == game.config["end_turn"] == game.state.turn


def test_the_report_names_the_hidden_end(tmp_path):
    game = past_setup(quad_game(tmp_path, turn_limit="2-3", seed=8))
    while not game.game_over:
        ready_all(game)
    report = build_report(read_events(game.gdir.journal_path))
    assert report["end_turn"] == game.config["end_turn"] and report["end_turns"] == [2, 3]
    assert f"Скрытый последний ход: {game.config['end_turn']} (из 2–3; местам не объявлен)" in render_report(report)


def test_an_early_last_standing_end_reports_the_drawn_turn_everywhere(tmp_path):
    """Review F2: after a last_standing finish before the drawn turn, `end_turn` of phase, state JSON and the
    spectator is the drawn turn, as the report says (not the turn the game stopped on)."""
    game = past_setup(quad_game(tmp_path, turn_limit="11-15", seed=11))
    drawn = game.config["end_turn"]
    game.state.players["B"].alive = False           # a shortcut for the eliminations
    game.state.players["C"].alive = False
    while not game.game_over:
        ready_all(game)
    assert game.state.result["reason"] == "last_standing" and game.state.turn < drawn
    info = game.phase_info("A")
    assert info["end_turn"] == drawn and "may_be_last" not in info
    assert f"Ход {game.state.turn}, последний" in info["text"]
    state = game.state_json("A")
    state = state.get("state", state)
    assert state["end_turn"] == drawn and state["end_turns"] == [11, 15]
    assert spectate._end_turns(game) == {"end_turns": [11, 15], "end_turn": drawn}
    live = spectate.live(game, spectate.GameCache(), "k", spectate.LIVE)
    assert live["end_turn"] == drawn
    report = build_report(read_events(game.gdir.journal_path))
    assert report["end_turn"] == drawn


def test_the_latest_possible_turn_reads_as_the_last():
    """Review F3: on turn 15 of 11-15 no later end exists; the seats read «последний», not «может быть»."""
    assert registry.turn_mark(15, 15, 11) == "15 (последний; конец — один из ходов 11–15)"
    assert registry.turn_mark(14, 15, 11).startswith("14 (может быть последним;")
    base = {"turn_limit": 15, "end_turns": [11, 15], "phase": "actions", "game_over": False}
    assert wake_text({**base, "turn": 15}).startswith("[DAH] Ход 15 (последний; конец — один из ходов 11–15)")
    assert "может быть последним" in wake_text({**base, "turn": 14})


def test_the_mission_term_names_the_latest_end_and_the_lost_rounds(tmp_path):
    """Review F4: the BAD_ROUNDS reply and the victory topic say the term counts to the latest end (15), the game may
    end before it and rounds after the end are lost with their price; a fixed end keeps «оставшихся ходов»."""
    rs = build_ruleset(None, "quad4")
    note = esp.rounds_note(rs)
    assert "хода 15" in note and "может кончиться раньше" in note and "пропадут" in note
    victory = rules_topic(rs, "victory")            # the «Конец» line (the spying topic is at its size gate)
    assert "Срок задания Тени — тоже не дольше хода 15; раунды после конца партии пропадают вместе с их ценой" \
        in victory
    fixed = build_ruleset(None, "slice3")
    assert esp.rounds_note(fixed) == "не больше оставшихся ходов"
    assert "Срок задания Тени" not in rules_topic(fixed, "victory")
    game = past_setup(quad_game(tmp_path, systems={"espionage": True}))
    st = copy.deepcopy(game.state)
    st.turn, st.phase = 12, "actions"
    target = sorted(pid for pid, p in st.provinces.items() if p.owner == "B")[0]
    st.players["A"].shades = [{"id": "s1", "xp": 0, "status": "idle", "hired_turn": 0, "mission": None}]
    check = check_order(st, game.ruleset, "A", SeatOrders(), {"kind": "spy", "shade": "s1", "mission": "observe",
                                                              "target": target, "rounds": 9}, "actions")
    assert check.reason == "BAD_ROUNDS", check
    assert "1..4 раундов (не больше ходов до самого позднего конца — хода 15;" in check.message, check.message


# ---------------------------------------------------------------- c) the bound of a rival's hidden score

def _simulate_module():
    spec = importlib.util.spec_from_file_location("dah_simulate", ROOT / "scripts" / "simulate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dah_simulate"] = module
    spec.loader.exec_module(module)
    return module


def _true_final(state: GameState, rs, seat: str) -> int:
    total = sum(sc.score_parts(state, rs)[seat].values())
    if sc.on(rs, "objectives") and state.players[seat].alive:
        total += rs.weight("objectives") * sc.objective_points(state, rs, seat)
    return total


@pytest.mark.parametrize("scenario,kinds", [("quad4", "rush,builder,diplomat,passive"),
                                            ("quad4", "builder,diplomat,rush"),
                                            ("slice3", "diplomat,rush,builder"),
                                            ("blitz_trade", "builder,diplomat,rush")])
def test_the_true_score_never_exceeds_the_visible_part_plus_the_bound(monkeypatch, scenario, kinds):
    sim = _simulate_module()
    rs = build_ruleset(None, scenario)
    checks, gaps = [], []
    original = sc.score_known

    def checked(state, ruleset, viewer, seat):
        known = original(state, ruleset, viewer, seat)
        if not known["exact"]:
            real = _true_final(state, ruleset, seat)
            assert real <= known["score"] + known["hidden_max"], (state.turn, state.phase, viewer, seat, real, known)
            checks.append(known["hidden_max"])
            gaps.append(real - known["score"])
        return known

    monkeypatch.setattr(sc, "score_known", checked)
    seats = {"ABCD"[i]: k for i, k in enumerate(kinds.split(","))}
    from dah.game import setup_config
    for game_seed in ("b1", "b2"):
        sim.play_game(rs, seats, game_seed, setup_config(rs, None, None, None))
    assert len(checks) > 200
    # not uselessly huge: on average within a few times the true hidden part, and the cap rules hold it down
    assert sum(checks) / len(checks) <= 3.5 * max(1.0, sum(gaps) / len(gaps)) + 20


def test_the_simulator_draws_a_hidden_end_per_game():
    sim = _simulate_module()
    rs = build_ruleset(None, "quad4")
    from dah.game import setup_config
    turns = {sim.play_game(rs, {"A": "passive", "B": "passive"}, seed, setup_config(rs, None, None, None))["turns"]
             for seed in ("e1", "e2", "e3", "e4", "e5", "e6")}
    assert turns <= set(range(11, 16)) and len(turns) > 1


def test_the_bound_reads_public_facts_only(tmp_path):
    game = past_setup(quad_game(tmp_path))
    st, rs = game.state, game.ruleset
    base = sc.hidden_bound(st, rs, "A", "B")
    assert set(base) == {"territory", "science", "war", "intel", "objectives"}
    assert base["war"] == 0 and base["intel"] == 0                      # nothing resolved yet
    # no tech researched yet; at most the branch bonus a gift of artifacts could complete
    assert base["science"] <= int(rs.stat("score.science.branch", st.players["B"].faction))
    other = copy.deepcopy(st)
    pool = [o for o in rs.objectives if o != other.players["B"].objectives[0]["id"]]
    other.players["B"].objectives = [{"id": pool[0]}]                   # another secret objective
    other.players["B"].resources = {r: 999 for r in rs.resources}       # hidden stock
    other.players["B"].researching = next(iter(rs.techs))               # hidden research
    assert sc.hidden_bound(other, rs, "A", "B") == base
    later = copy.deepcopy(st)
    later.turn, later.phase = 6, "negotiation_1"
    after = sc.hidden_bound(later, rs, "A", "B")
    assert after["war"] == int(rs.stat("score.war.province_total", st.players["B"].faction))
    assert after["intel"] == int(rs.stat("score.intel.cap", st.players["B"].faction))
    assert after["objectives"] == max(o.points for o in rs.objectives.values())


def test_only_factions_modify_score_stats_in_the_core():
    """The bound reads score.* stats for the rival's public faction: no tech, artifact or building may change them."""
    rs = build_ruleset(None, "quad4")
    for table in (rs.techs, rs.artifacts, rs.buildings):
        for entity in table.values():
            for eff in getattr(entity, "effects", ()) or ():
                assert not str(eff.get("target", "")).startswith("score."), (entity.id, eff)
            for mod in getattr(entity, "owner_mods", ()) or ():
                assert not str(mod.get("target", "")).startswith("score."), (entity.id, mod)


def test_the_score_line_shows_the_bound_within_its_budget(tmp_path):
    seats4 = [*SEATS3, {"seat": "D", "nick": "Дельта", "kind": "agent"}]
    game = past_setup(quad_game(tmp_path, seats=seats4))
    line = next(x for x in game.state_text("A").splitlines() if x.startswith("Счёт:"))
    assert len(line) <= sc.SCORE_LINE_LIMIT
    assert len(re.findall(r"≥\d+ ≤\d+", line)) == 3 and "чужой «≥» — только видимое" in line
    players = {p["seat"]: p for p in game.state_json("A")["state"]["players"]} \
        if "state" in game.state_json("A") else {p["seat"]: p for p in game.state_json("A")["players"]}
    assert "score_hidden_max" not in players["A"]
    known = sc.score_known(game.state, game.ruleset, "A", "B")
    assert players["B"]["score_hidden_max"] == known["hidden_max"] > 0
    assert f"Бета (" in line and f"≥{known['score']} ≤{known['score'] + known['hidden_max']}" in line
    # the fog topic and the goal explain it (the bound is the total now, not the final one)
    fog = game.rules("fog")
    assert "«≤N»: выше N чужой счёт сейчас быть не может" in fog and "ещё может вырасти" in fog
    assert "«≤N» рядом — выше N он сейчас быть не может" in goal_text(game.config, "A")


@pytest.mark.parametrize("nicks", [False, True])
def test_the_four_seat_score_line_keeps_the_own_breakdown_and_the_tags(nicks):
    """Review F1: with the bound the 4-seat quad4 line used to drop the own breakdown or every faction tag; now
    only the fog note's list of hidden sources gives way (the 0.8.1 content stays, plus «≤N» per rival)."""
    sim = _simulate_module()
    rs = build_ruleset(None, "quad4")
    kinds = {"A": "builder", "B": "diplomat", "C": "rush", "D": "builder"}
    names = {"A": "Альфа", "B": "Бета", "C": "Гамма", "D": "Дельта"}
    lines = []
    original = sim.resolve

    def resolve(state, books, rs_, seed, ops):
        st, rep = original(state, books, rs_, seed, ops)
        if st.phase != "game_over":
            if nicks:
                for s, p in st.players.items():
                    p.nick = names[s]
            tags = {s: rs_.factions[p.faction].name for s, p in st.players.items() if p.faction in rs_.factions}
            for viewer in sorted(st.players):
                if st.players[viewer].alive:
                    lines.append((viewer, sc.score_line(st, rs_, viewer, tags), tags, st))
        return st, rep

    sim.resolve = resolve
    assigned = sim.dealt_factions(list(rs.scenario.factions), sorted(kinds), 0)
    sim.play_game(rs, kinds, "x1", assigned=assigned)
    assert len(lines) > 30
    for viewer, line, tags, st in lines:
        assert len(line) <= sc.SCORE_LINE_LIMIT, line
        segs = line.removeprefix("Счёт: ").split(" | ")
        own = next(x for x in segs if x.startswith("ты"))
        assert own.startswith(f"ты ({tags[viewer]}) ") and "[зем " in own, line           # own breakdown kept
        alive = [s for s in st.players if s != viewer and st.players[s].alive]
        for s in alive:
            assert any(x.startswith(f"{st.players[s].nick} ({tags[s]}) ≥") for x in segs), (s, line)
        assert len(re.findall(r"≥\d+ ≤\d+", line)) == len(alive), line
        assert "чужой «≥» — только видимое" in line, line


def test_m11_turn_10_beta_would_have_seen_the_scale(tmp_path):
    """m11 finding 2: on turn 10 Бета read Альфа's «≥9» as its score; now the line says how much more is possible."""
    game = Game.load(GameDir(copy_game(RUNS / "games-m11/m11", tmp_path)))
    st = GameState.from_dict(game.gdir.load_turn("states", 10))
    st.phase = "negotiation_1"
    rs = game.ruleset
    known = sc.score_known(st, rs, "B", "A")
    real = _true_final(st, rs, "A")
    assert known["score"] == 9 and real - known["score"] >= 20
    assert real <= known["score"] + known["hidden_max"] <= known["score"] + 45
    assert f"Альфа (" in sc.score_line(st, rs, "B", {"A": "x"}) or "Альфа" in sc.score_line(st, rs, "B")
    assert f"≥9 ≤{9 + known['hidden_max']}" in sc.score_line(st, rs, "B")


# ---------------------------------------------------------------- d) m11 finding 4: an eliminated seat's spend

def test_an_eliminated_seat_may_post_after_game_spend(tmp_path):
    game = past_setup(quad_game(tmp_path))
    with pytest.raises(GameError) as err:
        game.spend("C", 0.4, "participant_program", phase="after_game_over")
    assert err.value.code == "INVALID" and "game_over" in err.value.message and "выбытия" in err.value.message
    game.state.players["C"].alive = False                          # eliminated; the game goes on
    game.spend("C", 0.5, "participant_program", phase="after_game_over")
    with pytest.raises(GameError):
        game.spend("A", 0.5, "participant_program", phase="after_game_over")
    spends = [e for e in read_events(game.gdir.journal_path) if e["type"] == "spend_report"]
    assert [(e["seat"], e["spent_usd"], e["for_phase"], e["at_phase"]) for e in spends] == [
        ("C", 0.5, "after_game_over", game.phase)]
    report = build_report(read_events(game.gdir.journal_path))
    assert report["totals"]["C"]["spent_usd"] == 0.5
