"""Fixes after measurement m6 (docs/SLICE.md, «Реализация: уточнения», «Замер m6»): an eliminated seat is told once
that the game is over for it and is not woken again (its spend is watched as after game_over); phase() and get_state()
say so; the goal ends at game_over or at the seat's elimination; the report names idle resources as resource-turns
and gives stocks per turn and at the end; the tie rule and the lasting fortification are in rules()."""

from __future__ import annotations

from typing import Any

import httpx
from test_post_game import ScriptedDreamTeam
from test_review_stage2 import FakeClock
from test_waker_loop import KEY, SESSION, TOKEN, Clock, FakeGame

from dah.bridge.client import GameClient
from dah.cli import goal_text
from dah.engine import build_ruleset, rules_text, rules_topic
from dah.engine.content import LEGACY_PACKS_DIR
from dah.game import ELIMINATED_NOTE, Game, make_config
from dah.report import build_report, render_report
from dah.store import init_game_dir
from dah.waker.core import Waker
from dah.waker.text import eliminated_text

SEATS = [{"seat": "A", "nick": "Нико", "kind": "agent"}, {"seat": "B", "nick": "Соло", "kind": "agent"},
         {"seat": "C", "nick": "Третий", "kind": "agent"}]
OUT_AT = 3   # the seat is eliminated when the fourth phase opens (turn 2, P1): T0 + 3 * 600, ScriptedDreamTeam's end


class EliminatedGame(FakeGame):
    """Turns of three phases; from phase index OUT_AT on the seat is not alive (the game goes on). whoami: seat B."""

    def __init__(self, clock: Clock, out_at: int | None = OUT_AT, **kw: Any) -> None:
        super().__init__(clock, turns=4, **kw)
        self.out_at = out_at

    def _info(self) -> dict[str, Any]:
        info = super()._info()
        if self.out_at is not None and self._index() >= self.out_at and not info.get("game_over"):
            info["alive"] = False
            info["eliminated"] = True
        return info

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/whoami":
            return httpx.Response(200, json={"ok": True, "seat": "B", "nick": "Соло"})
        return super().__call__(request)


def make(game: FakeGame, dt: ScriptedDreamTeam, clock: Clock, **kw: Any) -> Waker:
    from dah.waker.dreamteam import DreamTeamClient
    return Waker(GameClient("http://game.test", KEY, transport=httpx.MockTransport(game)),
                 DreamTeamClient("http://dt.test", TOKEN, transport=httpx.MockTransport(dt)),
                 SESSION, clock=clock, sleep=clock.sleep, log=lambda message: None, **kw)


# ---------------------------------------------------------------- 3. the eliminated seat

def test_eliminated_seat_gets_one_notice_and_no_more_wakes():
    clock = Clock()
    game = EliminatedGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "running", "final": False, "spend": 0.05},
                                   {"state": "done", "final": True, "spend": 0.02}])
    waker = make(game, dt, clock)
    waker.run()
    texts = [text for _, text in dt.messages + dt.continues]
    notices = [x for x in texts if x.startswith("[DAH] Ты выбыл")]
    assert notices == [eliminated_text({"turn": 2}, "B")]
    assert "партия для тебя окончена" in notices[0] and "Заверши цель одним итоговым сообщением" in notices[0]
    # the three phases while alive were woken (open + reminder); nothing after the elimination but the notice
    phase_wakes = [x for x in texts if not x.startswith("[DAH] Ты выбыл")]
    assert len(phase_wakes) == 6 and all("Ход 1/" in x for x in phase_wakes)
    assert waker.eliminated and not any(x.startswith("[DAH] Партия окончена") for x in texts)
    # the post-game spend watch: figures as after_game_over, then post_game_finished marked eliminated
    after = [s for s in game.spend if s.get("phase") == "after_game_over"]
    assert after and after[-1]["spent_usd"] == dt.cost
    finished = game.detail("post_game_finished")
    assert len(finished) == 1 and finished[0]["eliminated"] is True and finished[0]["reason"] == "stopped"
    sent = [d for d in game.detail("wake_sent") if d.get("reason") == "eliminated"]
    assert len(sent) == 1 and sent[0]["phase"] == "eliminated"


def test_waker_started_after_the_elimination_sends_nothing():
    clock = Clock()
    clock.now += OUT_AT * 600 + 5
    game = EliminatedGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "done", "final": True, "spend": 0.0}])
    make(game, dt, clock).run()
    assert dt.messages == [] and dt.continues == []
    skipped = game.detail("continue_skipped")
    assert skipped and skipped[0]["reason"] == "started_after_eliminated"
    assert game.detail("post_game_finished")[0]["eliminated"] is True


def test_stop_on_game_over_stops_an_eliminated_session():
    clock = Clock()
    game = EliminatedGame(clock)
    dt = ScriptedDreamTeam(clock, [{"state": "stopped", "final": True, "spend": 0.0}])
    make(game, dt, clock, stop_on_game_over=True).run()
    assert len(dt.stops) == 1 and not any(t.startswith("[DAH] Ты выбыл") for _, t in dt.messages)


def test_phase_and_state_tell_an_eliminated_seat(tmp_path):
    config = make_config(game_id="m6", scenario="core:blitz_test", seats=SEATS, seed=3)
    gdir, _ = init_game_dir(tmp_path, config)
    game = Game.create(gdir, FakeClock())
    game.start()
    for prov in game.state.owned("B"):
        prov.owner = None
    game.state.players["B"].alive = False
    info = game.phase_info("B")
    assert info["alive"] is False and info["eliminated"] is True and info["hint"] == ELIMINATED_NOTE
    assert ELIMINATED_NOTE in info["text"] and "ты не готов" not in info["text"]
    assert "orders_hash" not in info
    assert game.state_text("B").startswith("ВЫБЫЛ: ")
    assert game.state_json("B")["eliminated"] == {"note": ELIMINATED_NOTE}
    alive = game.phase_info("A")
    assert "eliminated" not in alive and not game.state_text("A").startswith("ВЫБЫЛ")


def test_goal_ends_at_game_over_or_elimination(tmp_path):
    config = make_config(game_id="m6", scenario="core:blitz_test", seats=SEATS, seed=3)
    text = goal_text(config, "A")
    assert "ПОКА dah.phase() НЕ ВЕРНУЛ game_over ИЛИ ТЫ НЕ ВЫБЫЛ" in text
    assert "После game_over или своего выбывания" in text


# ---------------------------------------------------------------- 4. report: resource-turns and stocks

def test_report_names_idle_resource_turns_and_gives_stocks():
    seats = {"A": {"nick": "Нико", "kind": "agent"}, "B": {"nick": "Соло", "kind": "agent"}}
    events: list[dict[str, Any]] = [
        {"type": "game_created", "ts": 0.0, "config": {"game_id": "r", "scenario": "core:slice2",
                                                       "seats": [{"seat": s, **v} for s, v in seats.items()]}},
        {"type": "turn_resolved", "ts": 10.0, "turn": 1, "state_hash": "h1", "scores": {"A": 5, "B": 5},
         "idle_resources": {"A": {"core:alloy": 10}, "B": {"core:alloy": 0}}, "idle_units": {"A": 2, "B": 0},
         "resources": {"A": {"core:alloy": 22, "core:provisions": 7}, "B": {"core:alloy": 3}}},
        {"type": "turn_resolved", "ts": 20.0, "turn": 2, "state_hash": "h2", "scores": {"A": 6, "B": 4},
         "idle_resources": {"A": {"core:alloy": 12}, "B": {"core:alloy": 1}}, "idle_units": {"A": 0, "B": 1},
         "resources": {"A": {"core:alloy": 30, "core:provisions": 9}, "B": {"core:alloy": 4}}},
    ]
    report = build_report(events)
    a = report["totals"]["A"]
    assert a["idle_resource_turns"] == {"core:alloy": 22} and a["idle_resources"] == a["idle_resource_turns"]
    assert a["final_resources"] == {"core:alloy": 30, "core:provisions": 9}
    assert report["turns"][0]["resources"]["A"] == {"core:alloy": 22, "core:provisions": 7}
    text = render_report(report)
    assert "сумма по ходам (ресурс-ходы) alloy 22" in text and "запасы к концу партии alloy 30 provisions 9" in text
    assert "ход 1: Нико 5, Соло 5; запасы: Нико alloy 22 provisions 7; Соло alloy 3" in text
    # a journal written before m6 has no stocks: shown as unknown, never as zero
    old = build_report([{k: v for k, v in e.items() if k != "resources"} for e in events])
    assert old["totals"]["A"]["final_resources"] is None and "запасы к концу партии —" in render_report(old)


def test_turn_resolved_journals_the_stocks(tmp_path):
    from dah.journal import read_events
    from test_review_stage2 import _play_out
    config = make_config(game_id="m6", scenario="core:blitz_test", seats=SEATS, seed=3, turn_limit=2)
    gdir, _ = init_game_dir(tmp_path, config)
    clock = FakeClock()
    game = Game.create(gdir, clock)
    _play_out(game, clock)
    resolved = [e for e in read_events(gdir.journal_path) if e["type"] == "turn_resolved"]
    assert resolved and all(set(e["resources"]) == {"A", "B", "C"} for e in resolved)
    last = resolved[-1]["resources"]
    assert last == {s: p.resources for s, p in game.state.players.items()}
    report = build_report(read_events(gdir.journal_path))
    assert report["totals"]["A"]["final_resources"] == game.state.players["A"].resources


# ---------------------------------------------------------------- 1, 2. rules text

def test_rules_state_the_tie_and_the_lasting_fortification():
    rs = build_ruleset(None, "slice3")
    core = rules_text(rs)
    assert "держится, пока" in core and ("Равная сила — победа защитника" in core or "равенство — защитнику" in core)
    combat = rules_topic(rs, "combat")
    assert "строго большей силе" in combat and "равенство — победа защитника" in combat
    orders = rules_topic(rs, "orders")
    assert "Один приказ — и укрепление держится" in orders and "найм на месте его не снимает" in orders
    old = build_ruleset([str(LEGACY_PACKS_DIR / "core-0.4.0")], "slice3")
    assert "если из провинции никто не уходит" in rules_topic(old, "orders")
    assert "держится, пока" not in rules_text(old)
